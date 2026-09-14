# ruff: noqa
"""
This module implements the failure‑triage layer used by the scheduler.

All public functions in this module are fails open: on any error they
return a minimal, well‑formed prompt that contains the TRIAGE QU
and the park‑and‑notify invariant.
"""

import os
import re
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import logging
import json
from .overlord import _invoke_overlord, _load_policy
from .persistence import _notify_user
from .parsers import _parse_ruling
from .parsers import _atomic_write_json
from .persistence import _append_decision, _plan_role_config
from .build_detect import detect_test_command
from .build_detect import _lint_acceptance_fixtures, _pytest_acceptance_fixtures
from .oracle_gate import acceptance_digests, validate_acceptance_fixtures
from .concurrency import _heavy_lock, _is_heavy
from .config import STEP_CAP_FALLBACK_THRESHOLD
from .rebrief import collect_failure_evidence
from .escalation import (_auto_escalation_enabled, _escalate_to_claude, _escalate_to_local_fallback_model)
from .escalation import (_escalate_to_claude as _orig_escalate_to_claude, _escalate_to_local_fallback_model as _orig_escalate_to_local_fallback_model)
from .repo_health import format_findings, classify_repo_health
from .git_ops import _worktree_has_new_commits

SIBLING_NOTE = "this is one half of a split; the sibling story owns the other half"
TRIAGE_MAX_PER_TICK = 1

# E6/E7: repo_issue alone remains deferred until implemented; split_story is
# implemented by _execute_split_story (see
# docs/plans/OVERLORD_FAILURE_TRIAGE_PLAN.md for implementation details).
# Exported via __all__; handled in execute_ruling.
DEFERRED_ACTIONS = frozenset({"repo_issue"})
# ---------------------------------------------------------------------------
# Triage executor helpers
# ---------------------------------------------------------------------------

# from .persistence import _notify_user


def _park(plan_name, story_key, story, reason) -> str:
    """Park a story and notify the user.

    The function sets ``story['status']`` to ``'parked'`` and records the
    ``reason`` in ``story['parked_reason']``.  It then attempts to notify the
    user via :func:`pipeline.persistence._notify_user`.  Any exception raised by
    the notification is swallowed so that the park operation never fails.

    Parameters
    ----------
    plan_name: str
        The name of the plan the story belongs to.
    story_key: str
        The unique key of the story.
    story: dict
        The mutable story dictionary.
    reason: str
        Human‑readable reason for parking.

    Returns
    -------
    str
        ``"park_for_human"`` – the marker used by the scheduler.
    """
    # Mutate the story first – this must happen regardless of notification
    story["status"] = "parked"
    story["parked_reason"] = reason
    try:
        _notify_user(
            plan_name,
            f"{story_key} triage: {reason}",
            event="story_parked",
        )
    except Exception:  # pragma: no cover – notification failures are ignored
        pass
    return "park_for_human"


def _execute_split_story(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Execute a ``split_story`` ruling by creating two child stories.

    Guards, in order:

    1. ``ruling['split']`` must carry exactly two non-empty summaries, else the
       story is parked loudly for a human (invalid SPLIT payload).
    2. :func:`plan_triage_budget_exhausted` – the plan's triage-created-story
       budget is spent, so nothing is created and the story is parked loudly.

    On success two children ``{story_key}-split-1`` / ``{story_key}-split-2``
    are appended to ``manifest['stories']``: each carries the payload summary,
    the parent's ``agent_instructions`` extended with a
    ``=== SPLIT FROM PRIOR STORY ===`` block (the ruling's rationale plus the
    sibling note), ``persona``/``risk``/``backend`` copied from the parent when
    present, and status ``todo``.  The parent's ``acceptance`` fixtures cannot
    be mechanically halved, so the children carry no ``acceptance`` field –
    agent-authored tests carry the bar.  The children are independent (no
    dependency between them).  The parent is parked with a provenance reason
    naming the children and ``manifest['triage_created_stories']`` is
    incremented by 2.

    The manifest file is NOT written here: :func:`run_triage_sweep` persists
    the mutated manifest after the tick.
    """
    action = ruling.get("action", "split_story")
    rationale = ruling.get("rationale", "")[:300]

    # Guard 1: the SPLIT payload must be exactly two non-empty summaries.
    split = ruling.get("split")
    summaries = [s for s in split if isinstance(s, str) and s.strip()] if isinstance(split, list) else []
    if len(summaries) != 2 or len(split if isinstance(split, list) else []) != 2:
        story["triage_deferred_action"] = action
        reason = "split_story ruled but SPLIT payload invalid; parked for a human"
        result = _park(plan_name, story_key, story, reason)
        try:
            _notify_user(plan_name, f"{story_key} triage: {action} – {rationale}")
        except Exception:
            pass
        return result

    # Guard 2: the plan's triage-created-story budget.
    if plan_triage_budget_exhausted(manifest):
        story["triage_deferred_action"] = action
        reason = "plan triage budget exhausted"
        result = _park(plan_name, story_key, story, reason)
        try:
            _notify_user(plan_name, f"{story_key} triage: {action} – {rationale}")
        except Exception:
            pass
        return result

    child1_key = f"{story_key}-split-1"
    child2_key = f"{story_key}-split-2"
    split_block = (
        "\n\n=== SPLIT FROM PRIOR STORY ===\n"
        f"rationale: {rationale}\n"
        f"{SIBLING_NOTE}\n"
    )
    parent_instructions = story.get("agent_instructions") or ""

    for child_key, child_summary in ((child1_key, summaries[0]), (child2_key, summaries[1])):
        child = {
            "key": child_key,
            "summary": child_summary,
            "status": "todo",
            "agent_instructions": parent_instructions + split_block,
        }
        for field in ("persona", "risk", "backend"):
            if field in story:
                child[field] = story[field]
        manifest["stories"][child_key] = child

    story["status"] = "parked"
    story["parked_reason"] = f"split into {child1_key}, {child2_key} by triage"
    _park(plan_name, story_key, story, story["parked_reason"])

    manifest["triage_created_stories"] = _coerce_int(
        manifest.get("triage_created_stories", 0)
    ) + 2

    story["_split_children"] = [child1_key, child2_key]
    return "split_story"


# The exact fail-closed park reason for an uncorroborated mark_done ruling.
_MARK_DONE_UNCORROBORATED_REASON = (
    "mark_done ruled but live evidence does not corroborate"
)
# The PASS sentinel returned by _current_suite_state (compared by equality,
# never by truthiness - the FAIL/empty shapes are also truthy strings).
_SUITE_PASSES_SENTINEL = (
    "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."
)


def _execute_mark_done(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Execute a ``mark_done`` ruling by correcting the story record.

    Fail-closed corroboration comes FIRST, before any mutation: an
    uncorroborated ``mark_done`` is the dangerous direction, so the executor
    acts only on corroborated LIVE evidence - never on the overlord's word
    alone and never on ``parked_reason`` text. The live predicates mirror the
    facts :func:`_current_git_state` surfaces:

      1. the story's branch has NEW COMMITS vs the base branch, via
         :func:`pipeline.git_ops._worktree_has_new_commits` with the base
         resolved the way its existing callers do (lazy
         :func:`pipeline.server._default_branch` import);
      2. the story carries a ``pr_url`` (a merged PR);
      3. :func:`_current_suite_state` reports the suite PASSES at HEAD
         (equality against the PASS sentinel, not truthiness).

    Uncorroborated -> the story is parked loudly with
    ``_MARK_DONE_UNCORROBORATED_REASON`` (status ``parked`` + notify) and
    ``triage_deferred_action`` is left untouched, so a re-dispatch takes the
    same fail-closed path instead of silently skipping.

    Corroborated -> ``story['status'] = 'done'`` and
    ``triage_deferred_action`` is cleared. This executor deliberately does
    NOT append the OPSA-3 execution record itself: the only production
    caller, :func:`_apply_ruling_for_mode`, snapshots
    ``prior_status``/``prior_parked_reason`` before the mutation and records
    the outcome with the real autonomy mode once :func:`execute_ruling`
    returns (recording here as well wrote two records per ruling, the inner
    one hardcoding ``mode='full'``).

    The manifest file is NOT written here: :func:`run_triage_sweep` persists
    the mutated manifest after the tick.
    """
    action = ruling.get("action", "mark_done")
    rationale = ruling.get("rationale", "")[:300]

    # (a) Corroborate FIRST - no story mutation before this point.
    worktree = story.get("worktree")
    has_new_commits = False
    suite_green = False
    if isinstance(worktree, str) and worktree:
        base = ""
        try:
            # Lazy import: pipeline.server imports this module at module
            # level, so a module-level import here would be circular. This is
            # how the existing callers resolve the base branch.
            from .server import _default_branch

            base = _default_branch()
        except Exception:  # noqa: BLE001 - unresolved base fails closed
            base = ""
        if base:
            try:
                has_new_commits = bool(
                    _worktree_has_new_commits(
                        Path(worktree),
                        str(story.get("story_key") or story.get("key") or ""),
                        base,
                    )
                )
            except Exception:  # noqa: BLE001 - probe failure fails closed
                has_new_commits = False
        try:
            suite_green = _current_suite_state(worktree) == _SUITE_PASSES_SENTINEL
        except Exception:  # noqa: BLE001 - probe failure fails closed
            suite_green = False
    has_merged_pr = bool(story.get("pr_url"))
    corroborated = has_new_commits or has_merged_pr or suite_green

    # (b) Not corroborated -> park loudly; the record stays as-is.
    if not corroborated:
        result = _park(plan_name, story_key, story, _MARK_DONE_UNCORROBORATED_REASON)
        try:
            _notify_user(plan_name, f"{story_key} triage: {action} – {rationale}")
        except Exception:  # pragma: no cover – notification failures are ignored
            pass
        return result

    # (c) Corroborated -> correct the record. No execution record here: the
    # only production caller, _apply_ruling_for_mode, already snapshots the
    # prior state and appends the OPSA-3 record with the real autonomy mode
    # once execute_ruling returns (matching _execute_split_story, which also
    # does not record). Recording here too wrote TWO records per ruling and
    # hardcoded mode="full", misstating PIPELINE_AUTONOMY="gated" runs.
    story["status"] = "done"
    story.pop("triage_deferred_action", None)
    return "mark_done"


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


def execute_ruling(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Execute a ruling in this slice.

    The current implementation is intentionally minimal – every action
    (recognized or not) results in parking the story.  The rationale is
    truncated to 300 characters to keep the notification concise.
    """
    action = ruling.get("action", "unknown")
    rationale = ruling.get("rationale", "")[:300]
    if action == "split_story":
        return _execute_split_story(
            plan_name, story_key, story, ruling, manifest, manifest_path
        )
    if action == "mark_done":
        return _execute_mark_done(
            plan_name, story_key, story, ruling, manifest, manifest_path
        )
    if action == "patch_acceptance":
        return _execute_patch_acceptance(
            plan_name, story_key, story, ruling, manifest, manifest_path
        )
    if action in DEFERRED_ACTIONS:
        story["triage_deferred_action"] = action
        reason = f"triage ruled {action}, which is not implemented yet; parked for a human"
        _park(plan_name, story_key, story, reason)
        try:
            _notify_user(plan_name, f"{story_key} triage: {action} – {rationale}")
        except Exception:
            pass
        return "park_for_human"
    if action == "escalate_model":
        try:
            fallback = manifest.get("local_model_fallback")
            if (
                fallback
                and story.get("model") != fallback
                and not story.get("tried_fallback_model")
                and story.get("backend", "local") == "local"
            ):
                _escalate_to_local_fallback_model(
                    manifest, plan_name, story_key, manifest_path, fallback
                )
                return "escalate_model"
            if _auto_escalation_enabled() and not story.get("escalated"):
                _escalate_to_claude(
                    manifest, plan_name, story_key, manifest_path
                )
                return "escalate_model"
        except Exception as exc:
            reason = f"escalate_model ruled but ladder exhausted: {type(exc).__name__}"
            return _park(plan_name, story_key, story, reason)
        # ladder exhausted
        reason = f"escalate_model ruled but ladder exhausted: {rationale}"
        return _park(plan_name, story_key, story, reason)
    reason = f"unhandled ruling action '{action}': {rationale}"
    return _park(plan_name, story_key, story, reason)
    return _park(plan_name, story_key, story, reason)

def _record_execution(plan_name, story_key, action, result, mode,
                      prior_status, prior_parked_reason, **extra) -> None:
    """Append an execution-outcome record to the plan's decisions log.

    The record captures what the triage executor did (or would do, in
    ``dry-run`` mode) together with the prior story state it acted on, so every
    autonomous action is explainable and undoable post-hoc.  Later sibling
    stories pass extra fields (e.g. ``children``, acceptance digests) via
    ``extra``; they are merged over the base record.

    The append is wrapped in try/except: an audit write failure must never
    raise out of the executor path.  The warning names only the story key.
    """
    record = {
        "story_key": story_key,
        "question": "failure triage execution",
        "action": action,
        "result": result,
        "mode": mode,
        "children": [],
        "prior_status": prior_status,
        "prior_parked_reason": prior_parked_reason,
        "decided_by": "overlord-triage",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    record.update(extra)
    try:
        _append_decision(plan_name, record)
    except Exception:  # noqa: BLE001 - audit write must never break execution
        logging.getLogger("pipeline").warning(
            "failed to append triage execution record for %s", story_key
        )


def _apply_ruling_for_mode(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Dispatch a ruling based on the current autonomy mode.

    * ``dry-run`` – the executor is disabled.  The story is left untouched and a
      notification is sent with the recommended action and rationale.
    * ``gated`` or ``full`` – the executor is enabled and the ruling is
      executed via :func:`execute_ruling`.

    In both modes an execution-outcome record is appended to the plan's
    decisions log (see :func:`_record_execution`) with the prior story state
    snapshotted before any mutation.
    """
    # Lazy import to avoid circular dependency
    from .server import PIPELINE_AUTONOMY

    # Snapshot the prior state BEFORE any executor mutates the story dict.
    prior_status = story.get("status")
    prior_parked_reason = story.get("parked_reason")
    action = ruling.get("action", "unknown")
    if PIPELINE_AUTONOMY == "dry-run":
        # Notify but do not act
        rationale = ruling.get("rationale", "")
        _notify_user(plan_name, f"{story_key} triage dry-run: {action} – {rationale}")
        _record_execution(plan_name, story_key, action, "dry-run", "dry-run",
                          prior_status, prior_parked_reason)
        return "dry-run"
    # Any other mode – execute the ruling
    result = execute_ruling(plan_name, story_key, story, ruling, manifest, manifest_path)
    extra = {}
    children = story.pop("_split_children", None)
    if children:
        extra["children"] = children
    # patch_acceptance records itself (it alone knows the prior acceptance
    # digests); pop the sentinel so the ruling is never recorded twice.
    if story.pop("_patch_acceptance_recorded", None):
        return result
    _record_execution(plan_name, story_key, action, result, PIPELINE_AUTONOMY,
                      prior_status, prior_parked_reason, **extra)
    return result


# expose subprocess.run for monkeypatching
globals()["subprocess.run"] = subprocess.run

# ---------------------------------------------------------------------------
# Triage loop‑breaker constants and helpers
# ---------------------------------------------------------------------------
TRIAGE_MAX_ATTEMPTS = 2
TRIAGE_MAX_CREATED_STORIES = 3


def _coerce_int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) else default


def triage_allowed(story: dict) -> tuple[bool, str]:
    attempts = _coerce_int(story.get("triage_attempts", 0))
    if attempts < TRIAGE_MAX_ATTEMPTS:
        return True, ""
    return False, f"triage attempts ({attempts}) at or above cap ({TRIAGE_MAX_ATTEMPTS})"


def action_already_tried(story: dict, action: str) -> bool:
    actions = story.get("triage_actions", [])
    if not isinstance(actions, list):
        return False
    return action in actions


def record_triage_attempt(story: dict, action: str) -> None:
    attempts = _coerce_int(story.get("triage_attempts", 0))
    story["triage_attempts"] = attempts + 1
    actions = story.get("triage_actions")
    if not isinstance(actions, list):
        actions = []
        story["triage_actions"] = actions
    if action not in actions:
        actions.append(action)


def plan_triage_budget_exhausted(manifest: dict) -> bool:
    """Ceiling ships together with the loop breaker so the guard can never be forgotten later.
    The actions that create stories (split_story, repo_issue) are deliberately out of scope.
    """
    created = _coerce_int(manifest.get("triage_created_stories", 0))
    return created >= TRIAGE_MAX_CREATED_STORIES

__all__ = [
    "_auto_triage_enabled",
    "_current_suite_state",
    "_current_git_state",
    "TRIAGE_MAX_ATTEMPTS",
    "TRIAGE_MAX_CREATED_STORIES",
    "TRIAGE_MAX_PER_TICK",
    "run_triage_sweep",
    "action_already_tried",
    "collect_triage_evidence",
    "plan_triage_budget_exhausted",
    "record_triage_attempt",
    "execute_ruling",
    "_apply_ruling_for_mode",
    "rule_on_story",
    "triage_allowed",
    "triage_candidates",
    "DEFERRED_ACTIONS",
    ]
def run_triage_sweep(plan_name: str) -> dict:
    if not _auto_triage_enabled():
        return {"ok": True, "skipped": "disabled"}
    try:
        from .server import PLAN_DIR
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            return {"ok": True, "skipped": "no_manifest"}
        if manifest.get("paused"):
            return {"ok": True, "skipped": "plan_paused"}
        candidates = triage_candidates(manifest.get("stories", {}))
        if not candidates:
            return {"ok": True, "triaged": []}
        triaged_keys = []
        actions = {}
        changed = False
        for key in candidates[:TRIAGE_MAX_PER_TICK]:
            story = manifest["stories"][key]
            allowed, reason = triage_allowed(story)
            if not allowed:
                _park(plan_name, key, story, reason)
                changed = True
                continue
            if plan_triage_budget_exhausted(manifest):
                _park(plan_name, key, story, "plan triage budget exhausted")
                changed = True
                continue
            try:
                findings = classify_repo_health(story, story.get("worktree") or ".")
            except Exception:
                findings = []
            evidence = collect_triage_evidence(story.get("worktree", ""), story, findings)
            ruling = rule_on_story(plan_name, key, story, evidence)
            if action_already_tried(story, ruling["action"]):
                ruling = {"action": "park_for_human", "rationale": f"action {ruling['action']} already tried"}
            record_triage_attempt(story, ruling["action"])
            action = _apply_ruling_for_mode(plan_name, key, story, ruling, manifest, manifest_path)
            triaged_keys.append(key)
            actions[key] = ruling["action"]
            changed = True
        if changed:
            _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "triaged": triaged_keys, "actions": actions}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}

def _auto_triage_enabled() -> bool:
    """Whether the scheduler's failure-triage sweep is enabled.

    Mirrors :func:`pipeline.escalation._auto_escalation_enabled`'s shape so an
    operator who knows one knob knows the other: a truthy value in
    ``("1", "true", "yes", "on")`` enables it, a falsy value in
    ``("0", "false", "no", "off")`` disables it, and any unrecognized value
    fails closed to disabled.

    The one deliberate difference from escalation: escalation falls back to a
    legacy rule (``PIPELINE_BACKEND_DISPATCH == "auto"``) when its flag is
    unset, because that behavior predates the flag and must be preserved.
    Triage has no legacy behavior to preserve, so per CLAUDE.md 'Secure by
    Design / Secure defaults' new features ship disabled and the operator opts
    in: unset means OFF. This knob is independent of both
    ``PIPELINE_BACKEND_DISPATCH`` (dispatch routing) and
    ``PIPELINE_AUTO_ESCALATE`` (the escalation ladder below triage).
    """
    override = os.environ.get("PIPELINE_AUTO_TRIAGE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return False


def triage_candidates(stories: dict) -> list[str]:
    """Return the sorted list of story keys the triage sweep should consider.

    A story is a candidate if its ``status`` is ``"parked"`` or ``"failed"``,
    OR its ``step_cap_streak`` is at or above
    ``STEP_CAP_FALLBACK_THRESHOLD``. A missing or non‑integer
    ``step_cap_streak`` counts as 0, and a story dict with no ``status`` key
    is treated as having no status (never raises).

    ``interrupted`` is deliberately NOT a trigger by itself: it is already in
    the scheduler's ready list (``("todo", "interrupted", "changes_requested")``)
    and auto‑resumes on the next tick, so triaging it would fire continuously
    during normal operation. The step‑cap STREAK is the interrupted‑adjacent
    signal worth acting on, and it is a streak, not a single interrupt.

    The result is ``sorted(...)`` so the sweep is deterministic.
    """
    candidates = []
    for key, story in stories.items():
        status = story.get("status")
        if status in ("parked", "failed"):
            candidates.append(key)
            continue
        streak = story.get("step_cap_streak", 0)
        if not isinstance(streak, int):
            streak = 0
        if streak >= STEP_CAP_FALLBACK_THRESHOLD:
            candidates.append(key)
    return sorted(candidates)

# ---------------------------------------------------------------------------
# Helper: run the real test suite against the worktree's CURRENT HEAD
# ---------------------------------------------------------------------------

def _current_suite_state(worktree: str) -> str:
    """Run the REAL test suite against the worktree's CURRENT HEAD.

    This runs the full test suite against the worktree's current HEAD (unlike
    :func:`collect_failure_evidence`, which only reads cached/stale state) and
    is the fix for the gap found live on story 30e5f9fc-3681-40db-ae54-dd95387dd1e7,
    where a story sat parked with an already‑passing suite because nothing
    ever re‑checked. Never raises. Fails open to ``""`` (silence) on any problem - silence preserves today's behavior exactly, it never asserts something false.
    """
    if not worktree:
        return ""
    raw_timeout = os.environ.get("PIPELINE_TRIAGE_SUITE_TIMEOUT", "").strip()
    try:
        timeout_s = int(raw_timeout)
    except (TypeError, ValueError):
        timeout_s = 240
    if timeout_s <= 0:
        return ""
    try:
        test_dir, test_cmd = detect_test_command(Path(worktree))
        if not test_cmd:
            return ""
        needs_heavy = bool(test_cmd) and _is_heavy(test_cmd)
        if needs_heavy:
            with _heavy_lock():
                r = globals()["subprocess.run"](
                    test_cmd,
                    cwd=test_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_s,
                )
        else:
            r = globals()["subprocess.run"](
                test_cmd,
                cwd=test_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        if r.returncode == 0:
            return "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."
        if r.returncode == 5:
            return ""
        return f"CURRENT STATE: full test suite FAILS at the worktree's current HEAD (rc={r.returncode}):\n{(r.stdout + r.stderr)[-500:]}"
    except Exception:  # noqa: BLE001
        return ""

def _current_git_state(worktree: str, story: dict) -> str:
    """Return a live ``GIT STATE:`` section for the story's worktree.

    Mirrors :func:`_current_suite_state`'s fail-open shape: an empty or
    non-string ``worktree`` returns ``""`` immediately (no subprocess is
    spawned), and ANY exception — from the HEAD probe, the base-branch
    resolver or the new-commits helper — returns ``""``. The function never
    raises.

    It reports three facts the overlord must never have to guess from stale
    ``parked_reason`` text (21 of 34 historical parked stories were parked on
    a now-stale "no new commits vs master" reason that live git state
    contradicted):

      (1) the worktree's current HEAD sha;
      (2) whether the story's branch has NEW COMMITS vs the base branch,
          reusing :func:`pipeline.git_ops._worktree_has_new_commits` and
          resolving the base branch the way its existing callers do, via
          :func:`pipeline.server._default_branch`. If the base branch cannot
          be resolved, that fact is reported rather than guessing a name;
      (3) the story's ``pr_url``, when present.
    """
    if not isinstance(worktree, str) or not worktree:
        return ""
    try:
        r = globals()["subprocess.run"](
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        head_sha = (r.stdout or "").strip()
        lines = [f"GIT STATE: HEAD {head_sha}" if head_sha else "GIT STATE: HEAD unknown"]
        base = ""
        try:
            # Lazy import: pipeline.server imports this module at module
            # level, so a module-level import here would be circular.
            from .server import _default_branch

            base = _default_branch()
        except Exception:  # noqa: BLE001
            base = ""
        if base:
            has_new = _worktree_has_new_commits(
                Path(worktree), str(story.get("story_key") or story.get("key") or ""), base,
            )
            lines.append(
                f"BRANCH HAS NEW COMMITS vs {base}: yes"
                if has_new
                else f"BRANCH HAS NO NEW COMMITS vs {base} (0 new commits beyond base)"
            )
        else:
            lines.append("base branch unresolved; new-commits check skipped")
        pr_url = story.get("pr_url")
        if pr_url:
            lines.append(f"PR: {pr_url}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""

# ---------------------------------------------------------------------------
# Main triage evidence collection
# ---------------------------------------------------------------------------

def collect_triage_evidence(worktree: str, story: dict, findings: list | None = None, limit: int = 8000) -> str:
    triage_question = f"TRIAGE QUESTION: this story is terminal (status={story.get('status', '?')}). Decide what to do about it."
    story_state = "\n".join(
        f"{k}: {story.get(k, '-') }"
        for k in [
            "status",
            "parked_reason",
            "backend",
            "model",
            "dispatched_model",
            "escalated",
            "risk",
            "persona",
            "dispatch_attempts",
            "rework_attempts",
            "review_inconclusive_count",
            "step_cap_streak",
            "merge_attempts",
            "triage_attempts",
            "triage_actions",
        ]
    )
    # Section (b2) – current suite state: a LIVE check against the
    # worktree's current HEAD, not cached/stale state (see
    # _current_suite_state's docstring for why this exists).
    current_state_section = ""
    try:
        current_state_section = _current_suite_state(worktree)
    except Exception:  # noqa: BLE001
        current_state_section = ""
    # Section (b3) – live git state: HEAD sha, new-commits-vs-base and the
    # story's pr_url. 21 of 34 historical parked stories were parked on a
    # now-stale "no new commits vs master" reason that live git state
    # contradicted, so the overlord gets the live facts alongside the
    # (possibly stale) parked_reason text.
    git_state_section = ""
    try:
        git_state_section = _current_git_state(worktree, story)
    except Exception:  # noqa: BLE001
        git_state_section = ""
    # Section (c) – repo‑health findings
    findings_section = ""
    if findings:
        try:
            findings_section = format_findings(findings)
        except Exception:  # noqa: BLE001
            findings_section = ""
    # Compute the length of the fixed sections
    fixed_parts = [triage_question, "STORY STATE:\n" + story_state]
    if current_state_section:
        fixed_parts.append(current_state_section)
    if git_state_section:
        fixed_parts.append(git_state_section)
    if findings_section:
        fixed_parts.append(findings_section)
    fixed_text = "\n".join(fixed_parts)
    remaining = max(0, limit - len(fixed_text))
    try:
        failure_evidence = collect_failure_evidence(worktree, story, limit=remaining)
    except Exception:  # noqa: BLE001
        failure_evidence = ""
    if failure_evidence:
        result = (fixed_text + "\n" + failure_evidence)[:limit]
    else:
        result = fixed_text[:limit]
    return result

# ---------------------------------------------------------------------------
# Rule on story – the core triage decision logic
# ---------------------------------------------------------------------------

def rule_on_story(plan_name: str, story_key: str, story: dict, evidence: str) -> dict:
    """Return a ruling for a terminal story.

    The function builds a prompt consisting of the policy text, a triage framing
    that states the story is terminal and asks what to do about it, the supplied
    evidence, and a closing instruction to rule now using the output contract
    exactly, including the ACTION field, and to choose the honest action even if
    it is one the pipeline cannot execute yet.

    The function is fail‑open: any exception during policy load, overlord call
    or parsing results in a default ruling that parks the story for a human
    and logs a warning containing only the exception type.
    """
    try:
        policy = _load_policy()
        # Build prompt
        prompt = f"{policy}\nTRIAGE QUESTION: this story is terminal (status={story.get('status', '?')}). Decide what to do about it.\nEVIDENCE:\n{evidence}\nPlease respond with the following format:\nRULING: ...\nTIER: ...\nRISK: ...\nRATIONALE: ...\nNOTIFY_USER: yes/no\nACTION: ..."
        # Call overlord
        raw = _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
        # Parse ruling
        ruling = _parse_ruling(raw or "")
    except Exception as exc:  # pragma: no cover - fail open
        ruling = {
            "ruling": "",
            "tier": "",
            "risk": "",
            "rationale": f"triage failed open: {type(exc).__name__}",
            "notify_user": True,
            "action": "park_for_human",
            "failed_open": True,
        }
        logging.getLogger("pipeline").warning(type(exc).__name__)
    else:
        ruling["failed_open"] = False
    # Append decision record
    record = {
        "story_key": story_key,
        "question": "failure triage",
        **ruling,
        "decided_by": "overlord-triage",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _append_decision(plan_name, record)
    except Exception:  # pragma: no cover - persistence failure should not crash
        logging.getLogger("pipeline").warning("Failed to append decision for story %s", story_key)
    return ruling
