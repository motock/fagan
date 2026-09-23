# ruff: noqa
"""Triage's patch_acceptance executor, moved verbatim from pipeline/triage.py.

Every name this code reads from pipeline.triage is bound as a _ModuleRef,
resolved at call time, so monkeypatch.setattr(pipeline.triage, NAME, ...)
keeps landing on the moved code. pipeline.triage re-exports the moved names.
"""

import re
from pathlib import Path

from .module_ref import _ModuleRef

_invoke_overlord = _ModuleRef("pipeline.triage", "_invoke_overlord")
_lint_acceptance_fixtures = _ModuleRef("pipeline.triage", "_lint_acceptance_fixtures")
_load_policy = _ModuleRef("pipeline.triage", "_load_policy")
_park = _ModuleRef("pipeline.triage", "_park")
_plan_role_config = _ModuleRef("pipeline.triage", "_plan_role_config")
_pytest_acceptance_fixtures = _ModuleRef("pipeline.triage", "_pytest_acceptance_fixtures")
_record_execution = _ModuleRef("pipeline.triage", "_record_execution")
acceptance_digests = _ModuleRef("pipeline.triage", "acceptance_digests")
collect_triage_evidence = _ModuleRef("pipeline.triage", "collect_triage_evidence")
validate_acceptance_fixtures = _ModuleRef("pipeline.triage", "validate_acceptance_fixtures")


# The exact fixture markers the overlord must wrap the corrected source in.
# Published in overlord-policy.md's "patch_acceptance output format" section;
# the prompt and this parser must agree character-for-character.
_FIXTURE_START_MARKER = "===FIXTURE-START==="
_FIXTURE_END_MARKER = "===FIXTURE-END==="


def _story_checkout(story: dict) -> Path:
    """Resolve the checkout a story's acceptance fixtures are graded against."""
    # Same idiom as the other call sites (e.g. run_triage_sweep): an absent or
    # blank worktree resolves to the repo root, never silently to cwd.
    return Path(story.get("worktree") or ".")


def _parse_fixture_rewrite(reply):
    """Parse the overlord's ``patch_acceptance`` response.

    The expected shape is published in overlord-policy.md: a one-line
    ``DIAGNOSIS:`` plus the complete corrected fixture source between
    ``===FIXTURE-START===`` and ``===FIXTURE-END===`` markers, each marker
    exactly once. Returns ``(diagnosis, source)`` or ``None`` when the
    response is unparseable - the caller parks fail-closed.
    """
    if not isinstance(reply, str) or not reply.strip():
        return None
    if (
        reply.count(_FIXTURE_START_MARKER) != 1
        or reply.count(_FIXTURE_END_MARKER) != 1
    ):
        return None
    diagnosis = re.search(r"^\s*DIAGNOSIS:\s*(.+?)\s*$", reply, re.MULTILINE)
    if diagnosis is None:
        return None
    start = reply.index(_FIXTURE_START_MARKER) + len(_FIXTURE_START_MARKER)
    end = reply.index(_FIXTURE_END_MARKER)
    body = reply[start:end]
    # Strip only the newlines that belong to the marker lines themselves, so
    # the captured source is byte-for-byte what the overlord emitted.
    if body.startswith("\n"):
        body = body[1:]
    if body.endswith("\n"):
        body = body[:-1]
    if not body.strip():
        return None
    return diagnosis.group(1).strip(), body + "\n"


def _acceptance_with_source(acceptance, source) -> list:
    """Return a copy of ``acceptance`` with the first sourced entry rewritten.

    The corrected fixture replaces the born-broken entry's authoritative
    ``source``; sibling entries are preserved untouched.
    """
    rewritten = []
    replaced = False
    for entry in acceptance or []:
        if isinstance(entry, dict):
            entry = dict(entry)
            if not replaced and entry.get("source") is not None:
                entry["source"] = source
                replaced = True
        rewritten.append(entry)
    # No entry carries a source: leave the result unsourced rather than
    # fabricating one on the last entry.
    return rewritten


def _execute_patch_acceptance(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Execute a ``patch_acceptance`` ruling by rewriting a born-broken
    acceptance fixture through validation.

    The fixture is a read-only oracle for the dispatched agent, so a
    born-broken one (it cannot pass no matter what an implementer writes)
    burns an implementer's whole step budget for no usable signal. The
    executor:

      1. gates on ``pipeline.oracle_gate``'s EXISTING classification - the
         fixture must be demonstrably broken (state ``errors``) at a clean
         baseline; anything else means the ruling mis-reads the story, which
         parks loudly with the manifest untouched;
      2. re-invokes the overlord (same call shape as :func:`rule_on_story`)
         with a dedicated prompt carrying the fixture source, the failure
         output and the triage evidence, and parses the corrected source from
         the ``===FIXTURE-START===`` / ``===FIXTURE-END===`` markers plus the
         one-line ``DIAGNOSIS:``; an unparseable response parks loudly;
      3. VALIDATES BEFORE WRITING: the rewrite must pass OPSA-8's
         lint/collection helpers AND must still FAIL at a clean baseline (a
         rewrite that passes with no implementation is isolation-only and is
         rejected). Any violation parks loudly naming it and leaves the
         manifest's acceptance source untouched;
      4. on success, snapshots the PREVIOUS acceptance digests
         (:func:`pipeline.oracle_gate.acceptance_digests`) into the OPSA-3
         execution record BEFORE overwriting the manifest (so the change is
         undoable), rewrites the story's acceptance entries, sets ``status``
         back to ``todo`` for a fresh dispatch, and records the execution.

    The executor writes its own OPSA-3 record on the SUCCESS path only, because
    it alone knows the prior acceptance digests; every fail-closed park outcome
    is recorded by :func:`_apply_ruling_for_mode`, which pops the
    ``_patch_acceptance_recorded`` sentinel (set here only after the success
    record is written) so the ruling is never recorded twice. The manifest file
    is NOT written here: :func:`run_triage_sweep` persists the mutated manifest
    after the tick.
    """
    action = ruling.get("action", "patch_acceptance")
    rationale = ruling.get("rationale", "")[:300]

    acceptance = story.get("acceptance")
    if not acceptance:
        return _park(
            plan_name,
            story_key,
            story,
            "patch_acceptance ruled but the story has no acceptance fixtures "
            "to rewrite; parked for a human",
        )

    # (1) Born-broken gate: reuse oracle_gate's classification, never
    # re-derive the broken-ness test here.
    checkout = _story_checkout(story)
    baseline = validate_acceptance_fixtures(story, checkout)
    baseline_state = baseline.get("state")
    baseline_detail = str(baseline.get("detail") or "")
    if baseline_state != "errors":
        return _park(
            plan_name,
            story_key,
            story,
            f"patch_acceptance ruled but the acceptance fixture is not "
            f"born-broken at a clean baseline (state={baseline_state!r}: "
            f"{baseline_detail}); parked for a human",
        )

    # (2) Re-invoke the overlord for a corrected fixture.
    try:
        evidence = collect_triage_evidence(str(checkout), story) or ""
    except Exception:  # noqa: BLE001 - evidence is best-effort, fail open
        evidence = ""
    fixture_blocks = "\n".join(
        f"--- {entry.get('path') or 'fixture'} ---\n{entry.get('source') or ''}"
        for entry in acceptance
        if isinstance(entry, dict) and entry.get("source") is not None
    )
    try:
        policy = _load_policy() or ""
    except Exception:  # noqa: BLE001 - policy text is best-effort, fail open
        policy = ""
    prompt = (
        f"{policy}\n\n"
        "TRIAGE QUESTION: this story's acceptance fixture is demonstrably "
        "born-broken at a clean baseline - it cannot pass no matter what an "
        "implementer writes, so the fixture itself is the defect. Rewrite the "
        "fixture so it tests the story's acceptance criteria correctly.\n"
        f"STORY: {story_key}\n"
        f"RATIONALE: {rationale}\n"
        f"BROKEN FIXTURE SOURCE:\n{fixture_blocks or '(no fixture source recorded)'}\n"
        f"CLEAN-BASELINE FAILURE OUTPUT:\n{baseline_detail}\n"
        f"TRIAGE EVIDENCE:\n{evidence}\n"
        "Respond with a one-line diagnosis and the COMPLETE corrected fixture "
        "source in this exact format:\n"
        "DIAGNOSIS: <one line naming the defect that makes the fixture born-broken>\n"
        f"{_FIXTURE_START_MARKER}\n"
        "<complete corrected fixture source>\n"
        f"{_FIXTURE_END_MARKER}\n"
    )
    try:
        raw = _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
    except Exception as exc:  # noqa: BLE001 - overlord failure fails closed
        return _park(
            plan_name,
            story_key,
            story,
            f"patch_acceptance ruled but the overlord could not be "
            f"re-invoked: {type(exc).__name__}; parked for a human",
        )
    parsed = _parse_fixture_rewrite(raw or "")
    if parsed is None:
        return _park(
            plan_name,
            story_key,
            story,
            "patch_acceptance ruled but the overlord's rewrite was "
            "unparseable (expected a DIAGNOSIS: line and the corrected source "
            f"between {_FIXTURE_START_MARKER} and {_FIXTURE_END_MARKER}); "
            "parked for a human",
        )
    diagnosis, rewritten_source = parsed

    # (3) VALIDATE BEFORE WRITING - the manifest is untouched until both
    # checks pass.
    candidate = {"acceptance": _acceptance_with_source(acceptance, rewritten_source)}
    lint_kind, lint_msg = _lint_acceptance_fixtures(candidate, repo_root=str(checkout))
    if lint_kind == "finding":
        return _park(
            plan_name,
            story_key,
            story,
            f"patch_acceptance rejected the rewrite: ingest lint findings: "
            f"{lint_msg}; the manifest acceptance source is untouched; "
            "parked for a human",
        )
    collect_kind, collect_msg = _pytest_acceptance_fixtures(
        candidate, repo_root=str(checkout)
    )
    if collect_kind == "finding":
        return _park(
            plan_name,
            story_key,
            story,
            f"patch_acceptance rejected the rewrite: fixture collection "
            f"findings: {collect_msg}; the manifest acceptance source is "
            "untouched; parked for a human",
        )
    recheck = validate_acceptance_fixtures(candidate, checkout)
    recheck_state = recheck.get("state")
    recheck_detail = str(recheck.get("detail") or "")
    if recheck_state == "passes":
        return _park(
            plan_name,
            story_key,
            story,
            "patch_acceptance rejected the rewrite: it PASSES at a clean "
            "baseline (isolation-only - the fixture no longer tests the "
            "feature); the manifest acceptance source is untouched; "
            "parked for a human",
        )
    if recheck_state != "fails_correctly":
        return _park(
            plan_name,
            story_key,
            story,
            f"patch_acceptance rejected the rewrite: expected it to still "
            f"FAIL at a clean baseline, got state={recheck_state!r}: "
            f"{recheck_detail}; the manifest acceptance source is untouched; "
            "parked for a human",
        )

    # (4) Success - snapshot BEFORE the write so the change is undoable.
    manifest_story = story
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if isinstance(stories, dict) and isinstance(stories.get(story_key), dict):
        manifest_story = stories[story_key]
    prior_digests = acceptance_digests(manifest_story)
    prior_status = story.get("status")
    prior_parked_reason = story.get("parked_reason")

    new_acceptance = _acceptance_with_source(
        manifest_story.get("acceptance"), rewritten_source
    )
    manifest_story["acceptance"] = new_acceptance
    if story is not manifest_story:
        story["acceptance"] = [e for e in new_acceptance]
    story["status"] = "todo"
    # The story is being re-dispatched: a stale parked_reason would mislead the
    # next triage pass and the parked-story matrix.
    story.pop("parked_reason", None)
    if story is not manifest_story:
        manifest_story.pop("parked_reason", None)
    # The recovery is a fresh start: clear the triage loop-breaker counter so a
    # story that relapses later re-qualifies for the full triage budget instead
    # of staying permanently capped by the attempts it burned before recovery.
    # Only the SUCCESS path resets it -- a failed patch_acceptance must keep the
    # counter, or a story that keeps failing would get unlimited triage.
    story["triage_attempts"] = 0
    if story is not manifest_story:
        manifest_story["triage_attempts"] = 0

    try:
        from .server import PIPELINE_AUTONOMY as mode
    except Exception:  # noqa: BLE001 - unresolved mode falls back to gated
        mode = "gated"
    _record_execution(
        plan_name,
        story_key,
        action,
        "patch_acceptance",
        mode,
        prior_status,
        prior_parked_reason,
        prior_acceptance_digests=prior_digests,
        diagnosis=diagnosis,
    )
    # The executor owns the record on the SUCCESS path only: set the sentinel
    # here (after the record is actually written) so _apply_ruling_for_mode
    # does not append a second one. Park outcomes leave the sentinel unset, so
    # _apply_ruling_for_mode records them itself.
    story["_patch_acceptance_recorded"] = True
    return "patch_acceptance"
