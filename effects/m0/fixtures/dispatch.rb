# typed: true
# M0 fixture for the dispatch cases that later milestones have to handle.

module Notifier
  extend T::Sig
  extend T::Helpers
  interface!

  sig { abstract.params(msg: String).void }
  def notify(msg); end
end

class EmailNotifier
  extend T::Sig
  include Notifier

  # known
  sig { override.params(msg: String).void }
  def notify(msg)
    msg.upcase
  end
end

class WeirdNotifier
  extend T::Sig
  include Notifier

  # unknown (local): untyped receiver
  sig { override.params(msg: String).void }
  def notify(msg)
    T.unsafe(msg).explode
  end
end

class Service
  extend T::Sig

  # known today, because the call resolves to the abstract Notifier#notify, which has no body.
  # Should become unknown once M2 takes the union of implementations (WeirdNotifier is unknown).
  sig { params(n: Notifier).void }
  def via_interface(n)
    n.notify("hi")
  end

  # known: block passed with &
  sig { params(xs: T::Array[String]).returns(T::Array[String]) }
  def block_pass(xs)
    xs.map(&:upcase)
  end

  # known: splat call
  sig { params(xs: T::Array[Integer]).returns(Integer) }
  def splat(xs)
    add(*T.unsafe(xs))
  end

  sig { params(a: Integer, b: Integer).returns(Integer) }
  def add(a = 0, b = 0)
    a + b
  end

  # unknown (local): stored proc
  sig { params(cb: T.proc.returns(Integer)).returns(Integer) }
  def stored_proc(cb)
    cb.call
  end

  # known: union receiver, both sides resolve
  sig { params(x: T.any(Integer, String)).returns(String) }
  def union(x)
    x.to_s
  end
end
