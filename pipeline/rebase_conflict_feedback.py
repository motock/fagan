"""The rebase-conflict rework brief (RC-01).

When the merge gate's rebase onto the default branch conflicts it aborts, and
without a brief the story simply retries the identical merge until it parks
(CL-12/CL-20). ``_try_auto_resolve_conflict`` only handles pure additive-import
conflicts, so a real conflict needs an agent told exactly what to do - the
conflict analogue of ``pipeline.ci._ci_rework_feedback``.

Everything here is pure: no git, no filesystem, no logging.
"""
from __future__ import annotations

import os
import re

# git prints one of these per conflicted path, e.g.
#   CONFLICT (content): Merge conflict in src/app.py
#   CONFLICT (add/add): Merge conflict in docs/new guide.md
# The "Merge conflict in " anchor is required: the modify/delete form
# ("CONFLICT (modify/delete): legacy/old.py deleted in HEAD and modified in
# 1234abc. ...") names no single path and must not be captured as one.
_CONFLICT_LINE = re.compile(r"^CONFLICT \([^)]*\): Merge conflict in (.+)$")

_UNRESOLVED_PREFIX = "REBASE-CONFLICT-UNRESOLVED:"

# Hard cap on the rendered brief. The instructions (items 3-6, including the
# no-push rule and the unresolved marker) are never truncated; only the file
# list is, so this cap can be met without losing the rules that matter.
_MAX_CHARS = 2000

# Room kept for the "... and N more conflicted files" overflow line.
_OVERFLOW_RESERVE = 60

# Cap on the machine-readable unresolved reason.
_MAX_UNRESOLVED_CHARS = 300

# RC-02: default cap on rebase-conflict rework rounds. Overridable with
# PIPELINE_CONFLICT_REWORK_MAX; an unparseable/negative value falls back to
# this default rather than silently disabling the route.
CONFLICT_REWORK_MAX_DEFAULT = 2

# Header this module prepends when it carries an earlier feedback round
# forward across a CI-fail rework (see _merge_ci_rework_feedback).
_PRIOR_FEEDBACK_HEADER = (
    "Earlier review feedback on this branch (still relevant - address it too):"
)
# First line of every _ci_rework_feedback string; used to drop a stale CI block
# from the carried-forward text so repeat rounds do not stack.
_CI_FEEDBACK_MARKER = "The merge-gate CI check failed on your submitted branch"


def _has_control_char(text: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def parse_conflict_files(error: str) -> list[str]:
    """Return the repo-relative conflicted paths named in git's output.

    Paths are de-duplicated in first-seen order. Anything that is not a
    plausible repo-relative path - absolute, containing ``..``, or carrying a
    control character - is dropped, as is the modify/delete form that names no
    single path.
    """
    files: list[str] = []
    seen: set[str] = set()
    for line in (error or "").splitlines():
        match = _CONFLICT_LINE.match(line)
        if match is None:
            continue
        path = match.group(1).strip()
        if not path:
            continue
        if path.startswith("/"):
            continue
        if ".." in path:
            continue
        if _has_control_char(path):
            continue
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def conflict_rework_feedback(
    error: str,
    attempt: int,
    max_attempts: int,
    default_branch: str,
) -> str:
    """Render the brief handed to the implementer after a rebase conflict.

    ``attempt``/``max_attempts`` mirror the merge gate's retry budget so the
    agent can see how many rounds are left. The file list is truncated to fit
    the 2000-character cap; the numbered instructions never are.
    """
    files = parse_conflict_files(error)

    head = (
        f"REBASE CONFLICT - attempt {attempt}/{max_attempts}\n\n"
        f"The merge gate could not rebase your branch onto origin/{default_branch}, "
        "so it aborted the rebase and handed the conflict back to you. Resolve it "
        "in your worktree, in this order:\n\n"
        "1. Run `git fetch origin && git rebase "
        f"origin/{default_branch}` in the worktree.\n"
        "2. For each conflicted file, read BOTH sides "
        f"(`git log origin/{default_branch} -3 -- <file>`) and keep the intent of "
        "both: never drop master's change, never revert your own.\n"
    )
    if files:
        head += "   Conflicted files:\n"
    else:
        head += (
            "   No conflicted paths were parsed from git's output - run the "
            "rebase and resolve whatever git reports.\n"
        )

    tail = (
        "\n3. `git add` each resolved file, then `git rebase --continue`.\n"
        "4. Run the full test suite; it must be green before you finish.\n"
        "5. Do NOT `git push` (the merge gate pushes) and do NOT merge master "
        "into the branch.\n"
        "6. If a conflict cannot be resolved preserving both intents, end the "
        "turn with a line `REBASE-CONFLICT-UNRESOLVED: <path> :: <why>` and "
        "make no commit.\n"
    )

    budget = _MAX_CHARS - len(head) - len(tail) - _OVERFLOW_RESERVE
    listed: list[str] = []
    used = 0
    overflow = 0
    for index, path in enumerate(files):
        line = f"   - {path}\n"
        remaining = len(files) - index - 1
        reserve = _OVERFLOW_RESERVE if remaining else 0
        if used + len(line) + reserve > budget:
            overflow = len(files) - index
            break
        listed.append(line)
        used += len(line)

    body = "".join(listed)
    if overflow:
        body += (
            f"   - ... and {overflow} more conflicted files "
            "(see git status)\n"
        )

    return (head + body + tail)[:_MAX_CHARS]


def parse_unresolved(summary: str) -> str | None:
    """Return the reason from the LAST ``REBASE-CONFLICT-UNRESOLVED:`` line.

    The marker must start its line; a mention mid-sentence is not a marker.
    Control characters are removed before stripping so a trailing control
    character cannot leave a trailing space behind. The result is capped at
    300 characters; an empty reason yields ``None``.
    """
    found: str | None = None
    for line in (summary or "").splitlines():
        if line.startswith(_UNRESOLVED_PREFIX):
            found = line[len(_UNRESOLVED_PREFIX) :]
    if found is None:
        return None
    cleaned = "".join(
        c for c in found if ord(c) >= 0x20 and ord(c) != 0x7F
    ).strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_UNRESOLVED_CHARS]


def conflict_handoff_pending(story: dict) -> bool:
    """True when a rebase conflict was explicitly handed to the implementer.

    Both handoff sites (the merge gate and overlord triage) set the marker to
    the literal ``True``. Anything else - a missing key, or a truthy non-bool -
    is not a handoff, so a resumed conflict still parks.
    """
    return story.get("conflict_handoff") is True


def conflict_handoff_notice(story_key: str, default_branch: str) -> str:
    """The notification text for a conflict handed to the implementer."""
    return (
        f"story {story_key} rebase conflict against origin/{default_branch} "
        "handed to the agent to resolve; not parked"
    )


def _conflict_rework_max() -> int:
    """Return the conflict-rework cap (default 2).

    ``PIPELINE_CONFLICT_REWORK_MAX`` is read at call time so an operator can
    change it without a restart. An unparseable or negative value falls back
    to the default rather than silently disabling the route; ``0`` is a valid
    explicit "never route" setting.
    """
    raw = os.environ.get("PIPELINE_CONFLICT_REWORK_MAX", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return CONFLICT_REWORK_MAX_DEFAULT
    if value < 0:
        return CONFLICT_REWORK_MAX_DEFAULT
    return value


def _merge_ci_rework_feedback(prior: str | None, ci_feedback: str) -> str:
    """Return ``ci_feedback`` with any earlier review feedback preserved.

    The merge-gate CI-fail rework used to replace ``review_feedback`` outright,
    discarding findings from an earlier review round (Mode 50, root cause 3):
    the implementer then fixed only the CI error and could re-break what the
    reviewer had asked for. Keep the prior text in front of the CI failure,
    unless the prior text is itself only a previous CI block (stale - its gate
    error is superseded by this one) or empty.
    """
    prior = (prior or "").strip()
    if not prior:
        return ci_feedback
    marker_at = prior.find(_CI_FEEDBACK_MARKER)
    if marker_at != -1:
        prior = prior[:marker_at].rstrip()
    if prior.startswith(_PRIOR_FEEDBACK_HEADER):
        prior = prior[len(_PRIOR_FEEDBACK_HEADER):].strip()
    if not prior:
        return ci_feedback
    return f"{_PRIOR_FEEDBACK_HEADER}\n{prior}\n\n{ci_feedback}"


def route_resumed_conflict(story: dict, error: str, default_branch: str) -> bool:
    """Hand a resumed story's pre-dispatch rebase conflict to the agent.

    A pending ``conflict_handoff`` (merge gate or overlord triage) is already a
    handoff, so it returns True without counting a second round. Otherwise,
    while ``PIPELINE_REWORK_ON_CONFLICT`` is not ``0`` and ``conflict_reworks``
    is below the cap, it counts the round the same way the merge gate does
    (``conflict_reworks``, ``ci_rework``, a REBASE CONFLICT brief merged into
    ``review_feedback``) and returns True. Otherwise it leaves the story
    untouched and returns False, so the caller parks it.

    It never sets ``conflict_handoff``: this route is its own handoff.
    """
    if conflict_handoff_pending(story):
        return True
    if os.environ.get("PIPELINE_REWORK_ON_CONFLICT", "1") == "0":
        return False
    max_attempts = _conflict_rework_max()
    used = story.get("conflict_reworks", 0)
    if used >= max_attempts:
        return False
    attempt = used + 1
    story["conflict_reworks"] = attempt
    story["ci_rework"] = True
    story["review_feedback"] = _merge_ci_rework_feedback(
        story.get("review_feedback"),
        conflict_rework_feedback(error, attempt, max_attempts, default_branch),
    )
    return True
