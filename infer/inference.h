#ifndef SORBET_INFERENCE_H
#define SORBET_INFERENCE_H

#include "cfg/CFG.h"
#include <memory>
#include <string>
#include <vector>

namespace sorbet::infer {
// Effects prototype (M0): one record per dispatch component of a send, describing whether
// Sorbet could tell which method the call goes to.
struct SendFact {
    enum class Kind : uint8_t {
        Resolved,
        UntypedReceiver,
        NoMethod,
    };
    Kind kind;
    core::MethodRef method;
    core::NameRef fun;
    core::LocOffsets loc;
};

class Inference final {
public:
    // Facts recorded by the most recent `run` on this thread. Only populated when
    // `GlobalState::collectSendFacts` is set.
    static std::vector<SendFact> takeSendFacts();
    static void recordSendFact(SendFact fact);

    static bool willRun(core::Context ctx, core::LocOffsets loc, core::MethodRef method);
    static std::unique_ptr<cfg::CFG> run(core::Context ctx, std::unique_ptr<cfg::CFG> cfg);
};
} // namespace sorbet::infer

#endif // SORBET_INFERENCE_H
