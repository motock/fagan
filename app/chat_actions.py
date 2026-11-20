"""Chat action proposals: the model proposes a state change, the person runs it.

The chat model reads agent-written logs, journals and diffs that may carry
injected instructions, so it must never execute a state-changing action itself.
A call to one of ``ACTION_TOOLS`` is therefore turned into a proposal by
:func:`propose`. The proposal is shown to the person, who presses Run; the
console then calls the existing HTTP route with the person's own credentials
and gates. Nothing in this module performs I/O.
"""

from __future__ import annotations

ACTION_TOOLS: frozenset[str] = frozenset({
    "ingest_plan",
    "approve_merge",
    "approve_plan",
    "dispatch_story",
    "interrupt_story",
    "patch_story",
    "review_story",
    "mark_story_done",
    "checkpoint",
    "advance_pipeline",
    "advance_all_plans",
    "pause_plan",
    "resume_plan",
    "answer_decision",
})

# Actions the chat may only ever propose. They are deliberately NOT in the chat's
# ``TOOLS`` registry: no code in the chat can send them, so the console's Run
# button, acting as the person, is the only way they happen.
PROPOSAL_ONLY_TOOLS: dict[str, dict] = {
    "approve_merge": {
        "description": "Approve a story's merge. Proposed only: the user must press Run",
        "params": {"plan_name": "str", "story_key": "str"},
    },
    "approve_plan": {
        "description": "Approve a plan. Proposed only: the user reviews and approves it on the plan page",
        "params": {"plan_name": "str"},
    },
}

_NOT_RUN_NOTE = (
    "This action has NOT run. It is shown to the user, who must press Run to carry it out."
)


def propose(name: str, args: dict, seq: int) -> tuple[dict, dict]:
    """Return ``(proposal, tool_result)`` for one action call.

    ``seq`` numbers the proposals within one chat turn, starting at 1. The
    proposal is what the console shows; the tool result is what the model is
    told, so it never believes the action ran.
    """
    action_id = f"pa-{seq}"
    proposal = {"id": action_id, "name": name, "args": args}
    tool_result = {
        "result": {
            "proposed": True,
            "id": action_id,
            "status": "awaiting_user",
            "note": _NOT_RUN_NOTE,
        }
    }
    return proposal, tool_result
