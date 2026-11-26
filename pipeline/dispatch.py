import json  # noqa: F401
import logging
import os  # noqa: F401
import subprocess  # noqa: F401
import uuid
from datetime import UTC, datetime  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any

from app import backend, role_registry  # noqa: F401
from pipeline import forge_auth  # noqa: F401

from . import access_hooks, bound_records, cross_plan, plan_approval, triage_rebrief  # noqa: F401
from .branch_guard import remote_branch_exists  # noqa: F401
from .branch_names import record_story_branch, worktree_or_refusal
from .build_detect import detect_test_command, failed_node_ids  # noqa: F401
from .concurrency import _plan_lock
from .config import WORKTREE_SCOPE_RULE  # noqa: F401
from .dispatch_attempt import (
    _find_dead_new_functions,  # noqa: F401
    _rebrief_step_cap_struggle,  # noqa: F401
    _transcript_ends_with_done,  # noqa: F401
)
from .dispatch_baseline import (
    _BASELINE_TEST_TIMEOUT_S,  # noqa: F401
    _baseline_test_env,  # noqa: F401
    _run_baseline_test_snapshot,  # noqa: F401
    baseline_snapshot_for,  # noqa: F401
)

# Context resolution and the remote-branch guard live in their own modules to
# keep this file under the 1000-line limit.
from .dispatch_context import _step_resolve_context  # noqa: F401
from .dispatch_fence import executor_fence  # noqa: F401
from .dispatch_fresh_worktree import prepare_worktree
from .dispatch_phases import _apply_planner_checklist, _step_detached_test_author  # noqa: F401
from .dispatch_prompt_build import build_prompt
from .dispatch_revised_note import _revised_instructions_note  # noqa: F401
from .dispatch_routing import (
    _dispatch_fallback_provider,  # noqa: F401
    _resolve_dispatch_backend,  # noqa: F401
    _resolve_dispatch_target,  # noqa: F401
)
from .dispatch_spawn import spawn_agent
from .dispatch_transcript_resume import apply_transcript_resume
from .dispatch_worktree import _create_fresh_worktree, _refuse_if_remote_branch_taken  # noqa: F401
from .rebase_conflict_feedback import conflict_handoff_notice, route_resumed_conflict  # noqa: F401
from .role_routing_scope import layer_routing_for_plan  # noqa: F401
from .service import _ServerRef
from .store_records import InvalidRecordKeyError  # noqa: F401
from .story_fields import record_dispatch_baseline  # noqa: F401
from .test_scope_dispatch import stamp_test_scope, test_scope_event_fields  # noqa: F401
from .test_scope_run import local_scope_kwargs  # noqa: F401
from .trace_context import agent_traceparent  # noqa: F401

logger = logging.getLogger("pipeline")

# Server-sourced members the moved functions reference as free variables. Each
# resolves to the live ``pipeline.server`` binding at call time so
# ``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands (the ~50+
# tests patch these names on pipeline.server). This mirrors the ``_ServerRef``
# pattern already used by pipeline/service.py and pipeline/ingest.py.
_LOCAL_BACKEND_NAMES = _ServerRef("_LOCAL_BACKEND_NAMES")
_NEVER_TOUCH_TESTS_STEERING = _ServerRef("_NEVER_TOUCH_TESTS_STEERING")
MAX_CONCURRENT_AGENTS = _ServerRef("MAX_CONCURRENT_AGENTS")
WORKTREE_ROOT = _ServerRef("WORKTREE_ROOT")
LogicalState = _ServerRef("LogicalState")
_acceptance_rel_paths = _ServerRef("_acceptance_rel_paths")
_build_dispatch_command = _ServerRef("_build_dispatch_command")
_exclude_worktree_logs_from_tracking = _ServerRef("_exclude_worktree_logs_from_tracking")
_persona_requires_claude = _ServerRef("_persona_requires_claude")
_plan_role_config = _ServerRef("_plan_role_config")
_provision_worktree_venv = _ServerRef("_provision_worktree_venv")
_route_dispatch_backend = _ServerRef("_route_dispatch_backend")
_scoped_repo_root = _ServerRef("_scoped_repo_root")
_store = _ServerRef("_store")
_story_has_unwinnable_local_scope = _ServerRef("_story_has_unwinnable_local_scope")
_try_acquire_git_lock = _ServerRef("_try_acquire_git_lock")
acceptance_digests = _ServerRef("acceptance_digests")
validate_acceptance_fixtures = _ServerRef("validate_acceptance_fixtures")


# Server-sourced functions the moved code calls as free variables. Same
# _ServerRef pattern as the block above — resolves the live pipeline.server
# binding at call time so monkeypatch.setattr(pipeline.server, "NAME", ...)
# still lands. _ServerRef.__call__ delegates to the live attribute, so it is
# a drop-in for callables too, not just plain values.
_notify_user = _ServerRef("_notify_user")
_count_in_progress_agents = _ServerRef("_count_in_progress_agents")
_default_branch = _ServerRef("_default_branch")
_rebase_onto_master = _ServerRef("_rebase_onto_master")
_sync_branch_remote = _ServerRef("_sync_branch_remote")
_rework_requires_new_tests = _ServerRef("_rework_requires_new_tests")
_run_planner = _ServerRef("_run_planner")
_run_rework_planner = _ServerRef("_run_rework_planner")
_run_rework_test_author_phase = _ServerRef("_run_rework_test_author_phase")
_run_test_author_phase = _ServerRef("_run_test_author_phase")
_test_files_added_on_branch = _ServerRef("_test_files_added_on_branch")
_test_names_in_file = _ServerRef("_test_names_in_file")
_validate_key = _ServerRef("_validate_key")
get_ticket_provider = _ServerRef("get_ticket_provider")


# Bound on the baseline snapshot's test run. The snapshot runs synchronously
# in the dispatch path, so it must never hold a dispatch open indefinitely; a
# suite that outruns this fails open (no baseline recorded) rather than
# blocking. Generous enough for a real full suite (the grading gate's own
# full-suite runs are ~19 min on the pipeline repo, but the snapshot only
# needs to be long enough to observe an ALREADY-failing suite).
# Statuses a direct dispatch_story call must refuse: the story's work is
# already merged or awaiting merge, so a fresh worktree would be cut from a
# base that may already contain it. set_story_status is the explicit way back.
_NON_DISPATCHABLE_STATUSES = frozenset({"done", "pr_open"})


def _dispatch_story_impl(plan_name: str, story_key: str) -> dict[str, Any]:
    _validate_key(plan_name)
    _validate_key(story_key)
    from .authorization import authorize

    refusal = authorize("plan:dispatch", plan_name=plan_name, story_key=story_key)
    if refusal is not None:
        return refusal
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "another dispatch/interrupt is in progress for this plan",
            }
        manifest = _store.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}
        # ENT-LC-04: the tick is not the only entry point - the MCP and
        # dashboard tools call dispatch_story directly. Refuse an unapproved
        # plan before any worktree, git or agent work. Re-read on every call
        # (no cached negative), so approving takes effect immediately.
        if not plan_approval.is_activatable(manifest):
            return {"ok": False, "error": "plan is not approved", "plan": plan_name, **plan_approval.outdated_reason(manifest)}
        if story.get("status") in _NON_DISPATCHABLE_STATUSES:
            return {
                "ok": False,
                "error": (
                    f"{story_key} is {story['status']}; refusing to re-dispatch it. "
                    "Use set_story_status first if a re-run is really intended."
                ),
            }

        if (refusal := access_hooks.refuse_dispatch(_store, plan_name, story_key, manifest)) is not None:
            return refusal
        # W4-logging slice: mint the correlation ID once (the falsy guard keeps
        # it across redispatch) and record the branch, persisting both before
        # ANY subprocess work so a failed launch leaves them for the retry.
        try:
            branch = record_story_branch(story, story_key)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "story_key": story_key}
        worktree_path, refused = worktree_or_refusal(WORKTREE_ROOT, story, story_key)
        if refused:
            return {"ok": False, "error": refused, "story_key": story_key}
        if not story.get("correlation_id"):
            story["correlation_id"] = uuid.uuid4().hex[:12]
        bound_records.put_manifest(_store, plan_name, manifest)
        # Branch and worktree derive from key and namespace: a key another plan
        # still holds would adopt its worktree and push its branch. Refuse first.
        collisions = cross_plan.dispatch_collisions(
            story_key, story.get("status"),
            cross_plan.neighbour_manifests(_store, plan_name), worktree_path.parent,
        )
        if collisions:
            other = collisions[0]
            logging.getLogger("pipeline").warning(
                "story key collides with another plan",
                extra={"plan": plan_name, "story_key": story_key,
                       "other_plan": other["plan"]},
            )
            _notify_user(
                plan_name,
                f"Refused to dispatch {story_key}: plan {other['plan']} still "
                f"holds story key {other['other_key']}",
                story_key=story_key,
                severity="warning",
                event="story_key_conflict",
                dedup_key=f"story_key_conflict:{story_key}",
            )
            return {
                "ok": False,
                "error": "story key collides with another plan",
                "plan": other["plan"],
                "other_key": other["other_key"],
            }
        resuming = (
            story.get("status") in ("interrupted", "changes_requested")
            or worktree_path.exists()
        )
        # The story's acceptance fixtures are materialized into the worktree
        # below as plain untracked files, so a `git add -A` would sweep the
        # read-only grading oracle into the story commit. Exclude them here -
        # this writes the repo-level .git/info/exclude, so it needs no
        # worktree and must simply run before the first commit.
        _acceptance_paths = _acceptance_rel_paths(story)
        if _acceptance_paths:
            with _scoped_repo_root(plan_name) as _oracle_repo_root:
                _exclude_worktree_logs_from_tracking(
                    _oracle_repo_root, _acceptance_paths
                )
        early = prepare_worktree(
            plan_name, story_key, story, manifest, branch, worktree_path,
            resuming=resuming,
        )
        if early is not None:
            return early

        built = build_prompt(
            plan_name, story_key, story, manifest, worktree_path, resuming=resuming
        )
        if built.early is not None:
            return built.early
        if built.resume_via_transcript:
            apply_transcript_resume(
                story,
                built.dispatch_kwargs,
                plan_name=plan_name,
                story_key=story_key,
                worktree_path=worktree_path,
                dispatch_backend=built.dispatch_backend,
                local_model=built.spec["model"],
                review_feedback=built.review_feedback,
                transcript_path=built.transcript_path,
                revised_instructions_note=built.revised_instructions_note,
            )
        return spawn_agent(
            plan_name, story_key, story, manifest, branch, worktree_path,
            built.log_path, built.dispatch_backend, built.dispatch_kwargs,
            spec_model=built.spec["model"],
            executor_is_resume=built.executor_is_resume,
        )
