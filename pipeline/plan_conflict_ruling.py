"""Plan-conflict rulings: ask the overlord role to rule on a plan conflict.

A *plan conflict* is a red grade whose only failing tests are pre-existing
tests the story never touched: the brief and those tests contradict each
other.  This module owns the ruling machinery for that situation:

* :func:`_parse_plan_conflict_reply` -- PURE parser for the overlord's reply.
  It fails SECURE: anything malformed becomes ``{"ruling": "PARK", "reason":
  ...}`` naming the defect, never a silent pre-authorization.
* :func:`rule_on_plan_conflict` -- builds the prompt (brief + conflicting
  files + failing node ids + the last 4000 chars of test output), invokes the
  ``overlord`` role exactly the way ``triage.rule_on_story`` does, and parses
  the reply.  A backend exception returns ``None`` (infrastructure is never
  charged to the story; the caller keeps today's path).
* :func:`apply_plan_conflict_ruling` -- mutates the story in place and returns
  the outcome string.  It never writes the manifest (the caller owns the
  write) and never touches the rework counters.

The seams (``_notify_user``, ``_invoke_overlord``) resolve through
``pipeline.service._ServerRef`` bindings, exactly as ``pipeline/ingest.py``
does, so tests can patch them on ``pipeline.server``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .build_detect import failed_node_ids
from .plan_conflict import branch_file_sets, classify_plan_conflict, failing_test_files
from .review import _first_review_base
from .service import _ServerRef

logger = logging.getLogger("pipeline")

PLAN_CONFLICT_HEADER = "=== PLAN-CONFLICT RULING (pre-authorized test edit) ==="

_notify_user = _ServerRef("_notify_user")
_invoke_overlord = _ServerRef("_invoke_overlord")
_atomic_write_json = _ServerRef("_atomic_write_json")

__all__ = [
    "PLAN_CONFLICT_HEADER",
    "_parse_plan_conflict_reply",
    "apply_plan_conflict_ruling",
    "rule_on_plan_conflict",
]

# The reply contract published in the prompt, verbatim.  The overlord must
# answer in exactly this shape; the parser below is its only consumer.
_REPLY_FORMAT = """\
RULING: PREAUTHORIZE_TEST_EDIT | PARK | REGRESSION
FILE: <repo-relative test path>
TEST: <test function name>
BEFORE:
<<<
<exact existing text>
>>>
AFTER:
<<<
<exact replacement text>
>>>
REPLACEMENT_ASSERTION:
<<<
<a new assertion that grades the story's real deliverable>
>>>
JUSTIFICATION: <one line for the commit message>"""

_RULINGS = ("PREAUTHORIZE_TEST_EDIT", "PARK", "REGRESSION")

# Fields a PREAUTHORIZE_TEST_EDIT ruling must carry.  BLOCK fields are fenced
# with <<< >>> so multi-line text survives; the rest are single lines.
_BLOCK_FIELDS = ("BEFORE", "AFTER", "REPLACEMENT_ASSERTION")
_LINE_FIELDS = ("FILE", "TEST", "JUSTIFICATION")


def _parse_plan_conflict_reply(reply: str, conflict_files: list[str]) -> dict:
    """Parse the overlord's reply into a ruling dict.  PURE, fail secure.

    Returns ``{"ruling": "PREAUTHORIZE_TEST_EDIT", "file", "test", "before",
    "after", "replacement_assertion", "justification"}`` for a well-formed
    pre-authorization, ``{"ruling": "PARK", "reason"}`` or ``{"ruling":
    "REGRESSION", "rationale"}`` otherwise.  Every malformed reply degrades to
    PARK with a reason naming the defect; this never raises and never returns
    a partial PREAUTHORIZE dict.
    """
    if not isinstance(reply, str):
        return {"ruling": "PARK", "reason": "reply was not text; ruling line missing"}

    # --- RULING line -------------------------------------------------------
    ruling = None
    for line in reply.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("RULING:"):
            value = stripped.split(":", 1)[1].strip()
            ruling = value.upper() if value else None
            break
    if ruling is None:
        return {"ruling": "PARK", "reason": "reply has no RULING line (missing ruling)"}
    if ruling not in _RULINGS:
        return {"ruling": "PARK", "reason": f"unknown ruling: {ruling}"}

    if ruling == "PARK":
        return {"ruling": "PARK", "reason": _labelled_line(reply, "REASON") or "overlord parked the story"}
    if ruling == "REGRESSION":
        return {
            "ruling": "REGRESSION",
            "rationale": _labelled_line(reply, "RATIONALE") or "overlord called the failure a regression",
        }

    # --- PREAUTHORIZE_TEST_EDIT: every field present and non-blank ---------
    fields: dict[str, str] = {}
    for name in _BLOCK_FIELDS:
        fields[name] = _fenced_block(reply, name)
    for name in _LINE_FIELDS:
        fields[name] = _labelled_line(reply, name)

    for name, value in fields.items():
        if value is None:
            return {"ruling": "PARK", "reason": f"missing required field: {name}"}
        if not value.strip():
            return {"ruling": "PARK", "reason": f"blank required field: {name}"}

    file_path = fields["FILE"].strip()
    if file_path not in conflict_files:
        return {
            "ruling": "PARK",
            "reason": (
                f"FILE {file_path} is not one of the conflicting pre-existing "
                f"tests ({', '.join(conflict_files) or 'none'}); a ruling may "
                "only authorize edits to the conflicting tests"
            ),
        }

    before = fields["BEFORE"]
    after = fields["AFTER"]
    if before == after:
        return {"ruling": "PARK", "reason": "BEFORE and AFTER are identical (no edit proposed)"}

    return {
        "ruling": "PREAUTHORIZE_TEST_EDIT",
        "file": file_path,
        "test": fields["TEST"].strip(),
        "before": before,
        "after": after,
        "replacement_assertion": fields["REPLACEMENT_ASSERTION"],
        "justification": fields["JUSTIFICATION"].strip(),
    }


def _labelled_line(reply: str, label: str) -> str | None:
    """Return the text after ``LABEL:`` on its own line, or None if absent.

    The value is everything up to the end of that line; a blank value is
    returned as ``""`` so the caller can distinguish missing from blank.
    """
    for line in reply.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(f"{label}:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _fenced_block(reply: str, label: str) -> str | None:
    """Return the text between the ``<<<``/``>>>`` fences under ``LABEL:``.

    Multi-line text is preserved verbatim.  Returns None when the label or a
    fence is missing, and "" when the fences enclose nothing.
    """
    lines = reply.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().upper() != f"{label}:":
            continue
        # The opening fence must be the next non-empty line.
        cursor = idx + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor >= len(lines) or lines[cursor].strip() != "<<<":
            return None
        cursor += 1
        body: list[str] = []
        while cursor < len(lines) and lines[cursor].strip() != ">>>":
            body.append(lines[cursor])
            cursor += 1
        if cursor >= len(lines):
            return None
        return "\n".join(body)
    return None


def rule_on_plan_conflict(
    story: dict,
    conflict_files: list[str],
    failing_node_ids: list[str],
    test_output_tail: str,
    plan_role_config: dict | None,
) -> dict | None:
    """Ask the overlord role to rule on a plan conflict.

    The prompt carries the story's brief, the conflicting pre-existing test
    files, the failing node ids and the last 4000 chars of the test output.
    It states the three rulings and the reply format verbatim, and states that
    an edit may never tell the executor to revert the brief's required change.

    Returns the parsed ruling dict, or ``None`` when the backend call raises
    (infrastructure is never charged to the story; the caller keeps today's
    path).  A malformed reply is NOT infrastructure: it parses to PARK.
    """
    brief = story.get("agent_instructions") or ""
    output_tail = (test_output_tail or "")[-4000:]
    prompt = (
        "PLAN CONFLICT: a story's grade fails only in pre-existing tests the "
        "story never touched, so the brief and those tests contradict each "
        "other. Rule on the conflict.\n\n"
        "THE BRIEF (agent_instructions):\n"
        f"{brief}\n\n"
        "CONFLICTING PRE-EXISTING TEST FILES:\n"
        + "".join(f"- {path}\n" for path in conflict_files)
        + "\nFAILING TEST NODE IDS:\n"
        + "".join(f"- {node}\n" for node in failing_node_ids)
        + "\nLAST 4000 CHARS OF TEST OUTPUT:\n"
        f"{output_tail}\n\n"
        "CHOOSE ONE RULING:\n"
        "- PREAUTHORIZE_TEST_EDIT: pre-authorize a minimal edit to one of the "
        "conflicting pre-existing tests so it grades the story's real "
        "deliverable.\n"
        "- PARK: park the story for a human; the conflict needs a person.\n"
        "- REGRESSION: the failure is a regression the story introduced; treat "
        "it as an ordinary failure.\n\n"
        "CONSTRAINT: a pre-authorized edit may never tell the executor to "
        "revert the brief's required change - the edit re-points the test at "
        "the deliverable, it does not undo the work the brief demands.\n\n"
        "Respond with the following format exactly:\n"
        f"{_REPLY_FORMAT}"
    )
    try:
        raw = _invoke_overlord(prompt, plan_role_config=plan_role_config)
    except Exception as exc:  # noqa: BLE001 - fail open: infra is never charged
        logger.warning("plan-conflict overlord call failed: %s", type(exc).__name__)
        return None
    return _parse_plan_conflict_reply(raw if isinstance(raw, str) else (raw or ""), conflict_files)


def apply_plan_conflict_ruling(
    plan_name: str,
    story_key: str,
    story: dict,
    ruling: dict,
    conflict_files: list[str],
) -> str:
    """Apply a plan-conflict ruling to ``story`` in place; return the outcome.

    Never writes the manifest (the caller owns the write) and never touches
    the rework counters: neither a pre-authorization nor a park charges the
    story's rework budget.
    """
    kind = ruling.get("ruling")
    ts = datetime.now(timezone.utc).isoformat()

    if kind == "PREAUTHORIZE_TEST_EDIT":
        file_path = ruling["file"]
        test_name = ruling["test"]
        # Replace, never stack: discard any existing plan-conflict block.
        base = (story.get("agent_instructions") or "").split(PLAN_CONFLICT_HEADER, 1)[0].rstrip()
        block = (
            f"{PLAN_CONFLICT_HEADER}\n"
            f"FILE: {file_path}\n"
            f"TEST: {test_name}\n"
            "BEFORE (exact existing text to replace):\n<<<\n"
            f"{ruling['before']}\n>>>\n"
            "AFTER (exact replacement text):\n<<<\n"
            f"{ruling['after']}\n>>>\n"
            "REPLACEMENT_ASSERTION (add this assertion for the real deliverable):\n<<<\n"
            f"{ruling['replacement_assertion']}\n>>>\n"
            f"JUSTIFICATION (the commit message must carry this): {ruling['justification']}"
        )
        story["agent_instructions"] = f"{base}\n\n{block}" if base else block
        story["status"] = "changes_requested"
        story["review_feedback"] = (
            f"Plan conflict: a pre-authorized test edit is recorded in the "
            f"{PLAN_CONFLICT_HEADER} block at the end of agent_instructions."
        )
        story["plan_conflict_ruling"] = {
            "files": list(conflict_files),
            "ruling": "PREAUTHORIZE_TEST_EDIT",
            "ts": ts,
        }
        _notify_user(
            plan_name,
            f"{story_key} plan conflict: pre-authorized edit to {file_path}::{test_name}",
            event="plan_conflict_preauthorized",
            story_key=story_key,
        )
        return "preauthorized"

    if kind == "PARK":
        reason = ruling.get("reason") or "unspecified"
        story["status"] = "parked"
        story["park_reason"] = (
            "plan conflict: pre-existing tests "
            + ", ".join(conflict_files)
            + " contradict the brief - "
            + reason
        )
        story["plan_conflict_ruling"] = {
            "files": list(conflict_files),
            "ruling": "PARK",
            "ts": ts,
        }
        _notify_user(
            plan_name,
            f"{story_key} plan conflict: parked; pre-existing tests "
            + ", ".join(conflict_files)
            + " contradict the brief",
            event="story_parked",
            story_key=story_key,
        )
        return "parked"

    if kind == "REGRESSION":
        story["plan_conflict_ruling"] = {
            "files": list(conflict_files),
            "ruling": "REGRESSION",
            "ts": ts,
        }
        return "regression"

    return "regression"


def _plan_conflict_intercept(
    plan_name: str,
    story_key: str,
    story: dict,
    test_result,
    worktree: str,
    manifest: dict,
    manifest_path,
    pid: int,
) -> dict | None:
    """Rule on a red grade that is a plan conflict, or return ``None``.

    A red grade whose only failing tests are pre-existing files the branch
    never touched is a PLAN CONFLICT: the brief and those tests contradict
    each other, so the overlord role rules on it rather than the story being
    charged a failed rework attempt.  Returns the verdict dict the caller
    should return (this function owns the manifest write), or ``None`` to
    keep today's path.

    Fail open: any unexpected exception is logged at WARNING with the story
    key only -- never the test output or the brief -- and returns ``None``,
    so a defect here can never abort a grade.
    """
    try:
        node_ids = failed_node_ids(test_result.stdout or "")
        files = failing_test_files(node_ids)
        if not files:
            # Nothing to classify, so do not shell out to git for an empty set.
            return None
        base_ref = _first_review_base(worktree)
        if base_ref is None:
            return None
        sets = branch_file_sets(worktree, base_ref)
        if sets is None:
            return None
        conflict = classify_plan_conflict(files, *sets)
        if conflict is None:
            return None
        # Already ruled on this exact conflict: today's path takes over, so a
        # repeat grade can never loop through the overlord role.  Keyed on the
        # conflict file list, never a blanket latch, so a genuinely new
        # conflict is still ruled on.
        if story.get("plan_conflict_ruling", {}).get("files") == conflict:
            return None
        ruling = rule_on_plan_conflict(
            story,
            conflict,
            node_ids,
            test_result.stdout or "",
            manifest.get("role_config"),
        )
        if ruling is None:
            return None
        outcome = apply_plan_conflict_ruling(
            plan_name, story_key, story, ruling, conflict
        )
        if outcome == "regression":
            # The ruling is recorded on the story, but a regression is an
            # ordinary failure: the caller keeps today's path.
            return None
        _atomic_write_json(manifest_path, manifest)
        return {"status": story["status"], "pid": pid, "plan_conflict": outcome}
    except Exception as exc:  # noqa: BLE001 - fail open: never abort a grade
        logger.warning(
            "plan-conflict intercept failed for %s (%s); keeping today's path",
            story_key,
            type(exc).__name__,
        )
        return None