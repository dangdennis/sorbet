#!/usr/bin/env python3
"""M0 measurement for the effects prototype.

Reads the output of `sorbet --print=effect-facts:<file>` and reports how much of a codebase an
effect analysis could see: the share of calls Sorbet resolves to a method, and the share of
methods that would be Unknown because they contain, or reach, a call it could not resolve.

    sorbet --print=effect-facts:facts.jsonl <paths>
    effects/m0/summarize.py facts.jsonl [--exclude test/] [--depth N] [--top N] [--methods]

Terms used in the report:
  resolved   Sorbet picked a specific method for the call.
  untyped    the receiver is T.untyped, so there is no method to pick.
  nomethod   the receiver is typed but Sorbet found no such method (usually a type error).
  dynamic    resolved, but to a method whose job is to call something chosen at runtime
             (send, eval, define_method, Proc#call, ...).
  splat      a call with `*args` whose length Sorbet does not know, so it gives up on the
             call without looking up the method. Recoverable: the receiver is still typed.
  unanalysed resolved, but to a method defined in a file Sorbet does not run inference on
             (`typed: false` or `typed: ignore`), so nothing is known about its body.
  Unknown    a method with at least one call of the kinds above other than resolved (local), or
             one that calls such a method through resolved calls (transitive).

Calls into the standard library and into gems that only have RBI files count as resolved here.
They have no body to analyse, so they are what the seed table has to cover.
"""
import argparse
import collections
import json
import os
import re
import sys

# Methods that hand control to code chosen at runtime. A call that resolves to one of these is
# as opaque to the analysis as an untyped receiver.
DYNAMIC_FUNS = {
    "send", "__send__", "public_send", "try", "try!",
    "eval", "instance_eval", "instance_exec", "class_eval", "class_exec", "module_eval", "module_exec",
    "define_method", "define_singleton_method", "method_missing", "method", "public_method",
    "instance_method", "const_get", "const_set", "instance_variable_get", "instance_variable_set",
    "class_variable_get", "class_variable_set", "binding", "remove_method", "undef_method",
    "alias_method", "constantize", "safe_constantize",
}
DYNAMIC_OWNER_FUNS = {
    ("Proc", "call"), ("Proc", "()"), ("Proc", "[]"), ("Proc", "yield"), ("Proc", "==="),
    ("Method", "call"), ("Method", "[]"), ("Method", "==="), ("UnboundMethod", "bind_call"),
}

# Sorbet's internal stand-ins for `recv.fun(*args)`. One of these survives as the callee only
# when Sorbet gave up before dispatching the real call.
SPLAT_FUNS = {"<call-with-splat>", "<call-with-splat-and-block-pass>"}

STRICT_NAMES = {2: "ignore", 3: "false", 4: "true", 5: "strict", 6: "strong"}


def pct(n, d):
    return "   n/a" if d == 0 else f"{100.0 * n / d:5.1f}%"


def load(path):
    defs, bodies = {}, {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec["t"] == "def":
                defs[rec["m"]] = rec
            else:
                prev = bodies.get(rec["m"])
                if prev is None:
                    bodies[rec["m"]] = rec
                else:
                    # A class body reopened in several places shares one <static-init> symbol.
                    prev["n"] = [a + b for a, b in zip(prev["n"], rec["n"])]
                    prev["calls"] = sorted(set(prev["calls"]) | set(rec["calls"]))
                    prev["unres"] += rec["unres"]
    return defs, bodies


def fun_name(d):
    # Overloaded methods are named like "send (overload.1)".
    return re.sub(r" \(overload\.\d+\)$", "", d["fun"])


def is_dynamic(d):
    if d is None:
        return False
    owner = d["owner"]
    if owner.startswith("T.class_of(") and owner.endswith(")"):
        owner = owner[len("T.class_of("):-1]
    # `T.proc` types dispatch to Proc0, Proc1, ... depending on arity.
    owner = re.sub(r"^Proc\d+$", "Proc", owner)
    fun = fun_name(d)
    if (owner, fun) in DYNAMIC_OWNER_FUNS:
        return True
    return fun in DYNAMIC_FUNS and d["src"] != "source"


def is_lost_splat(d):
    return d is not None and "<Magic>" in d["owner"] and d["fun"] in SPLAT_FUNS


def callee_kind(mid, defs, bodies):
    d = defs.get(mid)
    if d is None:
        return "other"
    if "<Magic>" in d["owner"] or d["owner"].startswith("T.class_of(Sorbet::Private"):
        return "sorbet-internal"
    if mid in bodies:
        return "has a body here"
    if d["abstract"]:
        return "abstract (needs implementations)"
    if d["src"] == "payload":
        return "standard library (needs a seed)"
    if d["src"] == "rbi":
        return "RBI only (needs a seed)"
    return "no body (attr, struct prop, synthesized)"


def directory(path, depth):
    parts = path.split("/")
    parts = [p for p in parts if p not in (".", "")]
    return "/".join(parts[:depth]) if len(parts) > depth else "/".join(parts[:-1]) or "."


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("facts")
    ap.add_argument("--depth", type=int, default=2, help="directory depth for the per-directory table")
    ap.add_argument("--top", type=int, default=15, help="rows in each 'top' table")
    ap.add_argument("--exclude", action="append", default=[], metavar="PREFIX",
                    help="leave out method bodies in files under this path prefix (repeatable), e.g. test/")
    ap.add_argument("--methods", action="store_true", help="print the status of every method (for fixtures)")
    ap.add_argument("--check", metavar="DIR", help="compare against the `# known` / `# unknown (...)` comments "
                    "above each method in the fixture files under DIR; exit 1 on any mismatch")
    args = ap.parse_args()

    defs, bodies = load(args.facts)
    if args.exclude:
        bodies = {m: b for m, b in bodies.items() if not any(b["file"].lstrip("./").startswith(x) for x in args.exclude)}
    analysed_files = {b["file"] for b in bodies.values()}

    # ---- per-call numbers -------------------------------------------------------------------
    sends = collections.Counter()
    kinds = collections.Counter()
    seeds = collections.Counter()
    dynamic_callees = collections.Counter()
    unanalysed_callees = collections.Counter()
    untyped_funs = collections.Counter()
    local_unknown = {}  # method id -> list of reasons
    edges = collections.defaultdict(set)  # callee -> callers, followed edges only

    for mid, b in bodies.items():
        resolved, untyped, nomethod = b["n"]
        sends["untyped"] += untyped
        sends["nomethod"] += nomethod
        reasons = [f"{kind} `{fun}` (line {line})" for kind, fun, line in b["unres"]]
        for kind, fun, _ in b["unres"]:
            if kind == "untyped":
                untyped_funs[fun] += 1
        dyn_here = 0
        splat_here = 0
        unanalysed_here = 0
        for callee in b["calls"]:
            d = defs.get(callee)
            if is_lost_splat(d):
                splat_here += 1
                reasons.append("splat of unknown length")
                continue
            if is_dynamic(d):
                dyn_here += 1
                dynamic_callees[d["name"]] += 1
                reasons.append(f"dynamic `{d['name']}`")
                continue
            if d is not None and d["src"] == "source" and callee not in bodies and d["file"] not in analysed_files \
                    and not any(d["file"].lstrip("./").startswith(x) for x in args.exclude):
                unanalysed_here += 1
                unanalysed_callees[d["name"]] += 1
                reasons.append(f"calls `{d['name']}` in a file with no inference")
                continue
            kind = callee_kind(callee, defs, bodies)
            kinds[kind] += 1
            if kind == "has a body here":
                edges[callee].add(mid)
            elif "needs a seed" in kind:
                seeds[d["name"]] += 1
        # `calls` is de-duplicated per method, so dynamic calls are counted once per callee here.
        sends["dynamic"] += dyn_here
        sends["splat"] += splat_here
        sends["unanalysed"] += unanalysed_here
        sends["resolved"] += resolved - dyn_here - splat_here - unanalysed_here
        if reasons:
            local_unknown[mid] = reasons

    # ---- transitive Unknown -----------------------------------------------------------------
    unknown_via = {}  # method id -> the callee that made it Unknown
    work = list(local_unknown)
    spread = collections.Counter()  # locally-unknown root -> methods it makes Unknown
    root_of = {m: m for m in local_unknown}
    while work:
        m = work.pop()
        for caller in edges.get(m, ()):
            if caller in local_unknown or caller in unknown_via:
                continue
            unknown_via[caller] = m
            root_of[caller] = root_of[m]
            spread[root_of[m]] += 1
            work.append(caller)

    def name(mid):
        d = defs.get(mid)
        return d["name"] if d else f"<method {mid}>"

    def status(mid):
        if mid in local_unknown:
            return "unknown (local)"
        if mid in unknown_via:
            return "unknown (transitive)"
        return "known"

    total_sends = sum(sends.values())
    print("== Calls ==")
    print(f"  total                {total_sends:>9}")
    for k in ("resolved", "untyped", "nomethod", "dynamic", "splat", "unanalysed"):
        print(f"  {k:<20} {sends[k]:>9}  {pct(sends[k], total_sends)}")

    print("\n== Where resolved calls go (distinct callees per method) ==")
    total_kinds = sum(kinds.values())
    for k, n in kinds.most_common():
        print(f"  {k:<42} {n:>9}  {pct(n, total_kinds)}")

    def method_table(title, key):
        groups = collections.defaultdict(lambda: [0, 0, 0])
        for mid, b in bodies.items():
            g = groups[key(mid, b)]
            g[0] += 1
            g[1] += mid in local_unknown
            g[2] += mid in unknown_via
        print(f"\n== Methods {title} ==")
        print(f"  {'':<40} {'bodies':>8} {'known':>7} {'unk local':>10} {'unk transitive':>15}")
        rows = sorted(groups.items(), key=lambda kv: -kv[1][0])
        for label, (n, loc, tra) in rows[: args.top if title != "overall" else None]:
            print(f"  {str(label)[:40]:<40} {n:>8} {pct(n - loc - tra, n):>7} {pct(loc, n):>10} {pct(tra, n):>15}")

    method_table("overall", lambda mid, b: "static-init (class bodies)" if "<static-init>" in name(mid) else "methods")
    method_table("by typed: sigil", lambda mid, b: "typed: " + STRICT_NAMES.get(b["strict"], str(b["strict"])))
    method_table(f"by directory (depth {args.depth})", lambda mid, b: directory(b["file"], args.depth))

    def top(title, counter):
        if not counter:
            return
        print(f"\n== {title} ==")
        for k, n in counter.most_common(args.top):
            print(f"  {n:>7}  {k}")

    top("Most common method names called on an untyped receiver", untyped_funs)
    top("Most common dynamic callees", dynamic_callees)
    top("Most-called methods in files with no inference", unanalysed_callees)
    top("Most-called methods with no body (the seed worklist)", seeds)
    top("Locally-unknown methods that make the most callers Unknown",
        collections.Counter({name(m): n for m, n in spread.items()}))

    if args.methods:
        print("\n== Every method ==")
        for mid in sorted(bodies, key=lambda m: (bodies[m]["file"], bodies[m]["line"])):
            b = bodies[mid]
            why = ""
            if mid in local_unknown:
                why = "  <- " + "; ".join(local_unknown[mid][:3])
            elif mid in unknown_via:
                why = "  <- calls " + name(unknown_via[mid])
            print(f"  {b['file']}:{b['line']:<4} {name(mid):<40} {status(mid)}{why}")

    if args.check:
        bad = 0
        checked = 0
        for mid, b in bodies.items():
            path = os.path.join(args.check, os.path.basename(b["file"]))
            if not os.path.exists(path) or b["line"] == 0:
                continue
            lines = open(path).read().split("\n")
            expected = None
            # Walk up from the `def` over its sig to the comment block above it.
            for i in range(b["line"] - 2, max(b["line"] - 8, -1), -1):
                m = re.match(r"\s*# (known|unknown \((?:local|transitive)\))", lines[i])
                if m:
                    expected = m.group(1)
                    break
                if re.match(r"\s*(def |end\b|class |module )", lines[i]):
                    break
            if expected is None:
                continue
            checked += 1
            if expected != status(mid):
                bad += 1
                print(f"MISMATCH {name(mid)}: expected {expected}, got {status(mid)}")
        print(f"\nfixture check: {checked - bad}/{checked} methods match")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
