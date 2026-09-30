"""Prompt note for a transcript-resume rework whose brief was revised."""


def _revised_instructions_note(
    resume_via_transcript: bool,
    revised_instructions: str,
    prior_dispatched: str | None,
) -> str:
    # Detect an operator's patch_story edit to agent_instructions since
    # the story's last dispatch. A transcript-resume rework otherwise
    # hands the resumed agent only the reviewer's raw feedback appended
    # to the verbatim prior transcript - it never re-reads the current
    # agent_instructions field - so a corrected instruction (e.g. "delete
    # the redundant wrapper" instead of "add a new one") is silently
    # dropped and the agent re-derives its own, possibly wrong, fix
    # (root-caused live 2026-07-28). Diff against the snapshot the
    # dispatcher records on every dispatch (_dispatched_agent_instructions);
    # only surface a note when the instructions actually changed, so an
    # unchanged rework round adds no noise. Fails open (no note) when no
    # prior snapshot exists - the first rework after this feature shipped
    # had no baseline to diff against.
    if (
        resume_via_transcript
        and prior_dispatched is not None
        and revised_instructions != prior_dispatched
    ):
        return (
            "\n\n--- Revised instructions from your tech lead ---\n"
            "Your tech lead has REVISED your task instructions since "
            "your last attempt. These supersede the original "
            "instructions in your transcript above. Follow them when "
            "addressing the review feedback:\n"
            f"{revised_instructions}"
        )
    return ""
