"""No-tool-call nudge for scripts/local_agent_oracle.py's step loop. Split
out purely to keep local_agent_oracle.py under the project's line-count
target. This mirrors scripts/local_agent_guards.py, local_agent.py's own
equivalent split, but the two are independently maintained, near-duplicate
files, not a shared import - see reference_benchmark_harness_gotchas. Unlike
local_agent.py, the oracle agent has no off-task-drift or edit-churn guards,
so this module is smaller.
"""
import re

_COMPLETION_PHRASES = ("all done", "i'm done", "i am done", "all finished", "finished")


def _no_tool_nudge(consecutive: int, content: str = "") -> str:
    """Nudge for an assistant turn that emitted no tool call.

    Early turns get the plain call-to-action (the model may simply have
    forgotten) - unless `content` itself narrates completion (e.g. "All
    done."), in which case it's directed to call the `done` tool specifically.
    From the third consecutive narration turn onward, escalate to behavioral
    guidance regardless of content: a weak model stuck looping on a failing
    self-test is usually chasing a phantom — its own test asserts behavior
    the correct implementation can never satisfy. Tell it to re-check the
    spec and fix the *test*, not the implementation, then call done.

    Kept in sync with scripts/local_agent.py's _no_tool_nudge (this file is a
    verbatim copy used for acceptance grading - see pipeline_mcp_server.py).
    """
    if consecutive < 3:
        lc = content.lower()
        for phrase in _COMPLETION_PHRASES:
            for match in re.finditer(r"\b" + re.escape(phrase) + r"\b", lc):
                preceding_words = lc[:match.start()].split()
                if preceding_words and preceding_words[-1] == "not":
                    continue
                return "You reported being done — call the done tool now to finish."
        return "Call a tool now (do not write prose)."
    return (
        "You have not called a tool for several turns. If you are stuck on a "
        "failing test that you wrote, that test may assert the wrong behavior — "
        "re-read the task spec. If your implementation already matches the spec, "
        "fix or delete the failing test rather than the implementation, then call "
        "done. Otherwise call a tool now (do not write prose)."
    )


__all__ = ["_no_tool_nudge"]
