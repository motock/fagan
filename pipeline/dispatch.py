import hashlib
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Any

from app import backend, role_registry  # noqa: F401

from .config import WORKTREE_SCOPE_RULE
from .dispatch_attempt import (
    _find_dead_new_functions,  # noqa: F401
    _rebrief_step_cap_struggle,  # noqa: F401
    _transcript_ends_with_done,
)
from .dispatch_baseline import (
    _BASELINE_TEST_TIMEOUT_S,  # noqa: F401
    _baseline_test_env,  # noqa: F401
    _run_baseline_test_snapshot,
)
from .dispatch_routing import (
    _dispatch_fallback_provider,  # noqa: F401
    _resolve_dispatch_backend,  # noqa: F401
    _resolve_dispatch_target,
)
from .dispatch_worktree import _create_fresh_worktree
from .service import _ServerRef

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
_exclude_worktree_logs_from_tracking = _ServerRef(
    "_exclude_worktree_logs_from_tracking"
)
_persona_requires_claude = _ServerRef("_persona_requires_claude")
_plan_role_config = _ServerRef("_plan_role_config")
_provision_worktree_venv = _ServerRef("_provision_worktree_venv")
_read_journal = _ServerRef("_read_journal")
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
_atomic_write_json = _ServerRef("_atomic_write_json")
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
def _dispatch_story_impl(plan_name: str, story_key: str) -> dict[str, Any]:
    _validate_key(plan_name)
    _validate_key(story_key)
    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "another dispatch/interrupt is in progress for this plan",
            }
        manifest_path = _store.manifest_path(plan_name)
        manifest = _store.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}

        # W4-logging slice: mint this story's correlation ID once, on its
        # first dispatch, and persist it immediately — before ANY subprocess
        # work (git fetch/worktree add, the agent launch itself) so a failed
        # launch still leaves the id on the manifest for the retry to reuse.
        # Rework/redispatch keeps the SAME id (the falsy guard).
        if not story.get("correlation_id"):
            story["correlation_id"] = uuid.uuid4().hex[:12]
            _atomic_write_json(manifest_path, manifest)

        branch = f"agent/{story_key.lower()}"
        worktree_path = WORKTREE_ROOT / story_key
        resuming = (
            story.get("status") in ("interrupted", "changes_requested")
            or worktree_path.exists()
        )
        journal = _read_journal(plan_name, story_key) if resuming else []

        if not resuming:
            # The fresh-dispatch git setup (fetch + `git worktree add`) and its
            # `except subprocess.CalledProcessError` handler - which returns the
            # structured "git setup failed" {"ok": False, "error": ...} dict
            # rather than letting the error escape - live in
            # pipeline.dispatch_worktree._create_fresh_worktree. A non-None
            # result is that failure dict and must be returned unchanged.
            setup_error = _create_fresh_worktree(plan_name, branch, worktree_path)
            if setup_error is not None:
                return setup_error
        else:
            # Resumed dispatch (interrupted / changes_requested / existing
            # worktree): the worktree was branched from origin/<default> at
            # some prior point. If origin's default branch has moved since
            # then (e.g. an urgent fix landed via a separate PR between the
            # original dispatch and this resume), the resumed agent's diff
            # would be based on a stale base and could spuriously revert that
            # fix and delete its regression tests - see retro
            # mcp-self-mod-notice_2026-07-31 (PR #213 incident). Detect that
            # here and notify; do NOT auto-rebase (separate, riskier
            # follow-up). Fail open: any git error (e.g. a network issue on
            # the fetch) is logged and swallowed so dispatch still proceeds -
            # this is an observability hook, never a gate. Stateless across
            # calls: no flag/cache is set, each resumed dispatch re-fetches
            # and re-checks independently. Only runs for a real git worktree
            # (a `.git` file/dir at the worktree root); a plain directory is
            # not a git worktree and the rev-list probe cannot run there.
            if (worktree_path / ".git").exists():
                try:
                    with _scoped_repo_root(plan_name) as repo_root:
                        with _try_acquire_git_lock(repo_root) as acquired:
                            if acquired:
                                subprocess.run(
                                    ["git", "fetch", "origin", _default_branch()],
                                    cwd=repo_root,
                                    check=True,
                                )
                        count_out = subprocess.run(
                            [
                                "git",
                                "rev-list",
                                "--count",
                                f"{branch}..origin/{_default_branch()}",
                            ],
                            cwd=repo_root,
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        behind = int(count_out.stdout.strip() or "0")
                        if behind > 0:
                            # The worktree base predates origin/<default>; a
                            # resumed agent would otherwise run on a stale base
                            # that could revert work merged since the worktree
                            # was created (live 2026-08-17). Actually rebase the
                            # worktree onto origin/<default> BEFORE the agent
                            # starts. Fail-secure on a rebase conflict (park,
                            # never run on the stale base); fail-open on any
                            # other git/infra failure (proceed, today's
                            # behavior). _rebase_onto_master never raises.
                            result = _rebase_onto_master(worktree_path, branch)
                            if result["ok"]:
                                _notify_user(
                                    plan_name,
                                    f"story {story_key} worktree base predates "
                                    f"origin/{_default_branch()} by {behind} "
                                    f"commit(s); rebased onto "
                                    f"origin/{_default_branch()} before resume.",
                                )
                                logging.getLogger("pipeline").info(
                                    "story %s worktree base predated "
                                    "origin/%s by %s commit(s); rebased onto "
                                    "origin/%s before resume",
                                    story_key, _default_branch(), behind,
                                    _default_branch(),
                                )
                                _sync = _sync_branch_remote(worktree_path, branch)
                                if not _sync.get("ok"):
                                    logging.getLogger("pipeline").warning(
                                        "remote sync for resumed story %s "
                                        "failed; dispatching anyway (fail "
                                        "open): %s",
                                        story_key, _sync.get("error"),
                                    )
                            elif result["conflict"]:
                                # Fail-secure: never dispatch an agent on a
                                # base that would revert merged work. Park the
                                # story for human resolution; a later resume
                                # can retry the rebase and proceed if it now
                                # succeeds (parked is a retryable state).
                                story["status"] = "parked"
                                story["parked_reason"] = (
                                    f"rebase conflict: {result['error']}; "
                                    f"worktree still behind "
                                    f"origin/{_default_branch()}"
                                )
                                _atomic_write_json(manifest_path, manifest)
                                _notify_user(
                                    plan_name,
                                    f"story {story_key} parked: rebase conflict "
                                    f"against origin/{_default_branch()} - "
                                    f"{result['error']}",
                                    event="story_parked",
                                    story_key=story_key,
                                    **(
                                        {"correlation_id": story["correlation_id"]}
                                        if story.get("correlation_id")
                                        else {}
                                    ),
                                )
                                logging.getLogger("pipeline").warning(
                                    "story %s parked: rebase conflict against "
                                    "origin/%s; worktree still behind",
                                    story_key, _default_branch(),
                                )
                                return {
                                    "status": "parked",
                                    "reason": "rebase_conflict",
                                    "parked_reason": story["parked_reason"],
                                }
                            else:
                                # Fail open on a non-conflict git/infra failure
                                # (e.g. git fetch timeout): notify and proceed
                                # with dispatch, exactly as before.
                                _notify_user(
                                    plan_name,
                                    f"story {story_key} rebase onto "
                                    f"origin/{_default_branch()} failed "
                                    f"(non-conflict); dispatching anyway "
                                    f"(fail open): {result['error']}",
                                )
                                logging.getLogger("pipeline").warning(
                                    "rebase for resumed story %s failed "
                                    "(non-conflict); dispatching anyway "
                                    "(fail open): %s",
                                    story_key, result["error"],
                                )
                except Exception:  # observability hook, never a gate
                    logging.getLogger("pipeline").warning(
                        "staleness check for resumed story %s failed; "
                        "dispatching anyway (fail open)", story_key,
                        exc_info=True,
                    )

        get_ticket_provider().set_state(story_key, LogicalState.IN_PROGRESS, plan_name)

        # Resolve concrete backend AND model for this story (shared with the
        # per-story dispatch gate in _advance_pipeline_locked). REG-1: both
        # now come from role_registry.resolve_role via
        # _resolve_dispatch_target, so a registry-pinned roles.dispatch
        # model actually runs instead of the driver's env default.
        dispatch_backend, dispatch_model = _resolve_dispatch_target(
            story, plan_role_config=_plan_role_config(plan_name)
        )
        # Persist so check_story_status and escalation see which backend ran.
        story["backend"] = dispatch_backend

        # BASELINE-1: on a story's FIRST dispatch, run the detected test
        # command ONCE against the freshly created, still-unmodified worktree
        # and remember whether it already failed. Observed live 2026-09-17:
        # 4 separate dispatched stories each independently rediscovered the
        # same pre-existing, out-of-scope failures (a "5 pre-existing ...
        # failures (httpx missing in subprocess interpreter)" pattern, named
        # almost verbatim in 4 separate journal entries across 2 plans) and
        # each spent part of its own step/rework budget investigating them
        # before concluding they were unrelated to its task. Recording the
        # baseline here lets the prompt below tell the agent up front which
        # failures predate its own changes. Gated exactly like the
        # test-author/planner phases further down:
        #   - a local-family backend (the same weak-local-executor rationale)
        #   - not resuming (a resumed dispatch acts on a worktree the agent
        #     has already been editing, so there is no meaningful "before"
        #     state left to snapshot)
        #   - no existing marker (belt-and-suspenders with `resuming`, and
        #     what stops a second run on a later fresh-looking call)
        # Never a gate: the helper is wrapped so any failure degrades to "no
        # baseline", and the marker is written unconditionally right after so
        # a raising snapshot is not retried on every subsequent dispatch.
        baseline_marker = worktree_path / ".dispatch_baseline_test_checked"
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and not resuming
            and not baseline_marker.exists()
        ):
            try:
                baseline = _run_baseline_test_snapshot(worktree_path)
            except Exception:  # noqa: BLE001 (observability hook, never a gate)
                baseline = None
            try:
                # The marker carries the baseline's failing node ids, not a
                # bare "ok": the agent-side full-suite done-gate reads them
                # back out of the worktree to tell a failure the story's own
                # change introduced from one that was already failing before
                # it touched the tree (mirroring the tick-side grade's own
                # baseline exemption). No recorded ids -> an empty list, which
                # exempts nothing.
                baseline_ids = (baseline or {}).get("failed_node_ids") or []
                baseline_marker.write_text(
                    json.dumps({"failed_node_ids": baseline_ids})
                )
            except OSError:
                pass
            if baseline is not None and baseline.get("returncode") != 0:
                story["baseline_test_check"] = baseline

        # A rework redispatch (changes_requested with stored review_feedback)
        # on the local Ollama driver can resume the prior dispatch's message
        # transcript instead of rebuilding a cold-start prompt via
        # _build_dispatch_command's rework_instruction - the transcript
        # already holds the full prior context, so only the reviewer's new
        # feedback needs to be appended. Guard on the transcript file actually
        # existing (backend.py writes it to cwd/.agent_transcript.json on
        # every dispatch): a story whose first dispatch predates this
        # feature, ran on a different backend, or had its transcript cleaned
        # up must fall back to the existing from-scratch rework prompt rather
        # than crash.
        review_feedback = story.get("review_feedback")
        transcript_path = worktree_path / ".agent_transcript.json"
        resume_via_transcript = (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and review_feedback
            and transcript_path.exists()
            and not _transcript_ends_with_done(transcript_path)
        )
        # Detect an operator's patch_story edit to agent_instructions since
        # the story's last dispatch. A transcript-resume rework otherwise
        # hands the resumed agent only the reviewer's raw feedback appended
        # to the verbatim prior transcript - it never re-reads the current
        # agent_instructions field - so a corrected instruction (e.g. "delete
        # the redundant wrapper" instead of "add a new one") is silently
        # dropped and the agent re-derives its own, possibly wrong, fix
        # (root-caused live 2026-07-28). Diff against the snapshot this
        # function records on every dispatch (_dispatched_agent_instructions,
        # written below); only surface a note when the instructions actually
        # changed, so an unchanged rework round adds no noise. Fails open
        # (no note) when no prior snapshot exists - the first rework after
        # this feature ships has no baseline to diff against.
        revised_instructions = story.get("agent_instructions", "")
        prior_dispatched = story.get("_dispatched_agent_instructions")
        revised_instructions_note = ""
        if (
            resume_via_transcript
            and prior_dispatched is not None
            and revised_instructions != prior_dispatched
        ):
            revised_instructions_note = (
                "\n\n--- Revised instructions from your tech lead ---\n"
                "Your tech lead has REVISED your task instructions since "
                "your last attempt. These supersede the original "
                "instructions in your transcript above. Follow them when "
                "addressing the review feedback:\n"
                f"{revised_instructions}"
            )

        spec = _build_dispatch_command(
            story,
            story_key,
            plan_name=plan_name,
            resume_journal=journal or None,
            review_feedback=None if resume_via_transcript else review_feedback,
        )
        # REG-1: a story with no explicit model runs on the model the
        # registry resolved for the dispatch role, not on the driver's env
        # default. spec["model"] already carries story["model"]/persona
        # precedence from _build_dispatch_command; only fill the gap when
        # nothing more specific won, and only when the resolved model
        # belongs to the backend that actually won (a claude dispatch is
        # never handed an ollama tag - _resolve_dispatch_target already
        # drops it, so dispatch_model is None there).
        if not story.get("model") and dispatch_model:
            spec["model"] = dispatch_model
        # HARDEN-1: the executor must be told its cwd is authoritative on
        # EVERY dispatch - fresh or resumed - before any plan-authored brief
        # (agent_instructions) it may contain. A brief once carried an
        # absolute path to the shared primary checkout and the agent ran
        # every command there, landing commits straight on master. Prepend
        # unconditionally so the rule survives an empty brief too.
        spec["prompt"] = f"{WORKTREE_SCOPE_RULE}\n\n{spec['prompt']}"
        # BASELINE-1: when the fresh-dispatch baseline snapshot above found
        # the test command ALREADY failing on the clean, unmodified worktree,
        # say so up front - before the plan-authored brief - so the agent
        # doesn't spend its own step/rework budget rediscovering (or trying to
        # "fix") a failure it did not cause. Only a non-zero baseline is worth
        # a note; a passing baseline (or none at all) adds nothing. Gated on
        # `not resuming` too: a resumed dispatch acts on a worktree the agent
        # has already been editing, so a baseline recorded on the first
        # dispatch is stale by then and re-emitting it would misdirect the
        # agent toward failures it may well have already fixed.
        baseline = story.get("baseline_test_check")
        if (
            baseline
            and baseline.get("returncode") not in (None, 0)
            and not resuming
        ):
            baseline_note = (
                "NOTE: the test command already fails on a clean, unmodified "
                "checkout of this worktree (exit code "
                f"{baseline['returncode']}), before you have changed anything. "
                "Do not spend time investigating or fixing a failure that is "
                "unrelated to your assigned scope below -- if a test you see "
                "failing looks unrelated to what you were asked to build, it "
                "was very likely already broken. Focus only on your own "
                "assigned files.\n\n"
            )
            spec["prompt"] = baseline_note + spec["prompt"]
        worktree_path.mkdir(parents=True, exist_ok=True)
        log_path = worktree_path / "agent.log"

        # Gap 7: surface multi-model concurrent-dispatch risk. MAX_CONCURRENT_AGENTS
        # is a process-count cap with no model/VRAM awareness, and Ollama's
        # `/api/ps` reports whatever's currently loaded. If a *different* model
        # is already in VRAM and we're about to dispatch a second story on a
        # different model, Ollama will swap the existing model out to make room
        # (or OOM-split if 24GB unified memory is tight). Warn, don't block:
        # same-model concurrency is safe, and even a swap is just slow.
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and MAX_CONCURRENT_AGENTS > 1
            and _count_in_progress_agents() > 0
        ):
            target_model = (
                story.get("model") or dispatch_model or spec.get("model")
            )
            if target_model:
                # spec["model"]/story["model"] may be an unresolved tier
                # name (e.g. "sonnet"), which never matches anything in
                # `loaded` (concrete Ollama tags) and would otherwise warn
                # on every dispatch regardless of what's actually loaded.
                target_model = backend._resolve_local_model(target_model)
            try:
                loaded = backend._ollama_loaded_models(
                    os.environ.get("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
                )
            except Exception:  # noqa: BLE001 (observability hook, never a gate)
                loaded = set()  # observability hook, never a gate
            if loaded and target_model and target_model not in loaded:
                msg = (
                    f"multi-model concurrent dispatch: {sorted(loaded)} already "
                    f"loaded, dispatching {story_key} on {target_model} may force "
                    f"a VRAM swap (set MAX_CONCURRENT_AGENTS=1 to silence)"
                )
                _notify_user(plan_name, msg)
                logging.getLogger("pipeline").warning(msg)

        # Mode 2 regression guard: OLLAMA_NUM_PARALLEL is set via
        # `launchctl setenv` and silently dropped whenever Ollama.app
        # auto-updates and relaunches (observed 2026-07-25, v0.32.4),
        # leaving every llama-server runner at -np 1. With
        # MAX_CONCURRENT_AGENTS>1 the pipeline then dispatches a second
        # agent that queues behind the first and hits the 180s
        # read-silence timeout. Probe the runner's actual -np at dispatch
        # time and warn loudly when configured concurrency exceeds what
        # Ollama can actually serve in parallel. Only fires on a 2nd+
        # concurrent local dispatch (same guard as the multi-model check
        # above): the first dispatch loads the model and there is no
        # queue yet. Observability hook, never a gate - a None probe
        # (no model loaded yet, ps unavailable) means "unknown", not "0".
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and MAX_CONCURRENT_AGENTS > 1
            and _count_in_progress_agents() > 0
        ):
            try:
                detected_np = backend._ollama_serving_parallelism()
            except Exception:  # noqa: BLE001 (observability hook, never a gate)
                detected_np = None
            if detected_np is not None and detected_np < MAX_CONCURRENT_AGENTS:
                msg = (
                    f"ollama serving parallelism ({detected_np}) is below "
                    f"MAX_CONCURRENT_AGENTS ({MAX_CONCURRENT_AGENTS}): a "
                    f"second concurrent dispatch will queue behind the "
                    f"first and may hit the 180s read-silence timeout. "
                    f"Re-apply `launchctl setenv OLLAMA_NUM_PARALLEL "
                    f"{MAX_CONCURRENT_AGENTS}` and restart Ollama, or set "
                    f"MAX_CONCURRENT_AGENTS=1."
                )
                _notify_user(plan_name, msg)
                logging.getLogger("pipeline").warning(msg)

        # Fix #1: if the story carries an `acceptance` block, materialize the
        # oracle files into the worktree BEFORE the backend launches so the local
        # harness can grade against them. The plan's acceptance source is
        # AUTHORITATIVE on a fresh dispatch and must always win — even when the
        # fixture path collides with a file a prior story already merged to the
        # base branch (the worktree inherits that file; failing to overwrite it
        # silently grades the stale, already-satisfied file and produces a false
        # green with zero implementation). Only a RESUMED run skips the write:
        # the oracle may have evolved the fixture mid-run into a committed WIP,
        # and overwriting would discard that evolution.
        acceptance = story.get("acceptance") or []
        acceptance_paths = _acceptance_rel_paths(story)
        for entry in acceptance:
            target = worktree_path / entry["path"]
            if resuming and target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry["source"])

        # Record a digest of each fixture's AUTHORITATIVE manifest source
        # (never the worktree file, which could already be rewritten) so a
        # later gate can prove the read-only oracle was not rewritten - see
        # pipeline.ci._acceptance_tampered. Recorded on EVERY dispatch,
        # including a resumed one, since the authoritative source hasn't
        # changed and re-recording also repairs a story dispatched before
        # this existed.
        story["acceptance_digests"] = acceptance_digests(story)

        # Pre-dispatch oracle gate: a fixture that cannot pass no matter what
        # is implemented (a broken helper, a bad CLI invocation) or that is
        # already satisfied with zero implementation burns an implementer's
        # entire step budget for no signal (observed live 2026-07-30,
        # LAUNCHD-PLIST-PORTABILITY). Skipped on a resumed run: the oracle
        # was already validated on the fresh dispatch, and the worktree may
        # legitimately carry WIP that changes the outcome.
        if not resuming:
            oracle_check = validate_acceptance_fixtures(story, worktree_path)
            if oracle_check["state"] in ("errors", "passes", "empty"):
                story["status"] = "blocked_oracle"
                story["oracle_gate"] = oracle_check
                _notify_user(
                    plan_name,
                    f"{story_key} not dispatched: acceptance oracle is "
                    f"unusable ({oracle_check['state']}) - {oracle_check['detail']}",
                )
                _atomic_write_json(manifest_path, manifest)
                return {
                    "status": "blocked_oracle",
                    "oracle_gate": oracle_check,
                }

        # TDD_SPLIT_PRODUCTION_PLAN.md: an ALWAYS-ON pre-executor
        # test-authoring dispatch (a full agent-loop, BLOCKING until it
        # exits - unlike the planner checklist above, this produces a real
        # commit the executor's worktree must already have) in THIS
        # worktree before the main executor starts. The global
        # PIPELINE_TDD_SPLIT on/off toggle AND the per-story `tdd_split`
        # opt-in are both removed; the phase now mirrors the planner's gate
        # below exactly - unconditional for local-family dispatch. Gated on:
        #   - a local-family backend (same rationale as the planner: the
        #     crutch exists for the weak local executor; Claude doesn't
        #     need it)
        #   - not resuming (a rework redispatch acts on the SAME tests it
        #     already has; it never gets a fresh test-authoring pass)
        #   - no existing test-author marker in the worktree (belt-and-
        #     suspenders with `resuming`, mirrors plan_path's own check
        #     below)
        # _run_test_author_phase never raises and a False return (role
        # unconfigured/refused, dispatch failure, timeout, or no commit
        # produced) falls open to today's unmodified monolithic dispatch -
        # never a gate (§2.5).
        test_author_marker = worktree_path / ".tdd_split_test_author_done"
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and not resuming
            and not test_author_marker.exists()
        ) and _run_test_author_phase(
            story,
            story_key=story_key,
            worktree_path=worktree_path,
            dispatch_backend=dispatch_backend,
            local_model=spec["model"],
            plan_name=plan_name,
            plan_role_config=_plan_role_config(plan_name),
        ):
            test_author_marker.write_text("ok\n")
            story["tdd_split"] = True

        # GUIDED_DECOMPOSITION_PLAN.md: a "tech lead" checklist is always on
        # for the weak local executor (the on/off toggle was removed; the
        # planner is now unconditionally enabled for local-family dispatch).
        # Gated on:
        #   - a local-family backend (the crutch exists for the weak local
        #     executor; Claude doesn't need it)
        #   - not resuming (plan once on the story's first dispatch; a
        #     rework must never spend a second planner call)
        #   - no plan already on disk (belt-and-suspenders with `resuming`)
        # The LLM call itself is best-effort (_run_planner fails open to
        # None) so a broken/slow/rate-limited planner never blocks or
        # corrupts dispatch - the story simply proceeds with no checklist,
        # exactly like an unconfigured (fail-open) planner.
        # H3 ablation (GUIDED_DECOMPOSITION_PLAN.md §4.1's G-cloud-noscratch
        # condition): default "on" ships the persistent scratchpad; "off"
        # tests whether the checklist alone accounts for the benefit,
        # independent of cross-step memory. Read once here because it now
        # feeds BOTH the planner call (so the scratchpad becomes a first-class
        # generated step) and the trailing-instruction backstop below.
        scratchpad_on = (
            os.environ.get("PIPELINE_DECOMPOSE_SCRATCHPAD", "on").strip().lower()
            != "off"
        )
        plan_path = worktree_path / ".agent_plan.md"
        plan_hash_path = worktree_path / ".agent_plan_src_hash"
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and not resuming
            and not plan_path.exists()
        ):
            # Ground the planner in the test-author's ACTUAL committed test
            # file(s) when the phase ran this branch (root-caused live
            # 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2's THIRD reset: the
            # prohibition-only tests_already_authored clause still let the
            # planner re-derive a "Write the test file" step with invented
            # test-case names, because it had no concrete grounding in
            # which file/tests exist). Detect the test_*.py files added on
            # this branch and their top-level test-case names, and hand
            # them to the planner so it can point the executor at READING
            # the real files. Fail open: if git detects nothing (or the
            # phase ran but committed no test_*.py), authored_test_files is
            # empty and the planner degrades to the prohibition-only clause.
            authored_test_files: list[tuple[str, list[str]]] | None = None
            if test_author_marker.exists():
                try:
                    added = _test_files_added_on_branch(
                        worktree_path,
                        _default_branch(),
                    )
                    authored_test_files = [
                        (path, _test_names_in_file(worktree_path, path))
                        for path in added
                    ]
                except Exception:  # noqa: BLE001 (best-effort grounding enrichment; a git hiccup here must degrade to the prohibition-only planner clause, not raise)
                    authored_test_files = []
            plan_text = _run_planner(
                story.get("agent_instructions", ""),
                dispatch_backend=dispatch_backend,
                local_model=spec["model"],
                include_scratchpad=scratchpad_on,
                plan_role_config=_plan_role_config(plan_name),
                tests_already_authored=test_author_marker.exists(),
                authored_test_files=authored_test_files,
                worktree=str(worktree_path),
            )
            if plan_text:
                plan_path.write_text(plan_text)
                plan_hash_path.write_text(
                    hashlib.sha256(
                        story.get("agent_instructions", "").encode()
                    ).hexdigest()
                )
        # Referencing an existing plan is independent of generating one, so
        # a resumed dispatch that rebuilds its prompt from scratch (no
        # transcript to resume) still sees the checklist from the story's
        # first dispatch, without spending a second planner call for it.
        # Referencing an existing plan is independent of generating one, so
        # a resumed dispatch that rebuilds its prompt from scratch (no
        # transcript to resume) still sees the checklist from the story's
        # first dispatch, without spending a second planner call for it.
        # Reuse requires BOTH a local-family backend (the crutch was never
        # meant for Claude -- see the generation guard above) AND a hash of
        # the CURRENT agent_instructions matching what the checklist was
        # generated from -- a patch_story rewrite of agent_instructions
        # (e.g. a corrected rework brief) must silently drop the now-stale
        # checklist rather than inject contradictory instructions.
        current_instructions_hash = hashlib.sha256(
            story.get("agent_instructions", "").encode()
        ).hexdigest()
        checklist_is_fresh = (
            plan_path.exists()
            and dispatch_backend in _LOCAL_BACKEND_NAMES
            and plan_hash_path.exists()
            and plan_hash_path.read_text().strip() == current_instructions_hash
        )
        if checklist_is_fresh:
            scratchpad_instruction = ""
            # Backstop to the planner-woven scratchpad steps above: even with
            # the clause folded into the checklist, keep the explicit trailing
            # reminder so a resumed dispatch (whose stored .agent_plan.md may
            # predate the clause) and any run whose planner under-emitted it
            # still get told to maintain the scratchpad.
            if scratchpad_on:
                scratchpad_instruction = (
                    " After finishing each step, keep .agent_scratchpad.md "
                    "up to date with a short running summary of what you've "
                    "done and which step is next (create_file for the first "
                    "note, str_replace to rewrite it after that) before "
                    "moving on to the next step. The FIRST line must be "
                    "PROGRESS: <done>/<total> showing how many checklist "
                    "items you've completed (e.g. PROGRESS: 2/5)."
                    " The scratchpad is gitignored by design: never `git add` "
                    "it - not even `git add -f` - and never commit it."
                )
            spec["prompt"] = (
                f"{spec['prompt']}\n\n"
                "--- Implementation checklist from your tech lead ---\n"
                f"{plan_path.read_text()}\n\n"
                f"Work through these steps in order.{scratchpad_instruction}"
            )
        elif scratchpad_on and dispatch_backend not in _LOCAL_BACKEND_NAMES:
            # Prompt-only parity for non-local-family backends (Claude): no
            # tech-lead checklist exists here (that phase stays local-only),
            # so there is no numbered-step total to report progress against
            # -- do not reuse the "PROGRESS: <done>/<total>" line from above.
            spec["prompt"] = (
                f"{spec['prompt']}\n\n"
                "As you work, keep .agent_scratchpad.md up to date with a "
                "short running summary of what you've done and what's next "
                "(create_file for the first note, str_replace to rewrite it "
                "after that)."
            )

        # Referencing the test-author marker is independent of the phase
        # having run THIS dispatch (mirrors plan_path.exists() above): a
        # resumed/rework redispatch that skipped re-running the phase must
        # still get the "don't touch tests" steering, since the tests it
        # must not touch are already committed on this branch.
        if test_author_marker.exists():
            spec["prompt"] = (
                f"{spec['prompt']}\n\n"
                "--- Tests already written by your tech lead ---\n"
                "The test file(s) for this task have already been written "
                "and committed to this branch by your tech lead. If the "
                "task description or checklist above says to write tests "
                "yourself first, DISREGARD that - it does not apply here; "
                "the tests already exist. Do not create, write, or modify "
                "any test file. "
                f"{_NEVER_TOUCH_TESTS_STEERING} Run them to see the current "
                "failures, then implement until they pass."
            )

        dispatch_kwargs: dict[str, Any] = {
            "prompt": spec["prompt"],
            "system": spec["system"],
            "model": spec["model"],
            "allowed_tools": spec["allowed_tools"],
            "cwd": worktree_path,
            "log_path": log_path,
            "append": resuming,
        }
        # Only the local driver accepts/uses `acceptance`; pass it through when
        # we're actually invoking that driver so Claude's signature stays clean.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and acceptance_paths:
            dispatch_kwargs["acceptance"] = acceptance_paths
        # L1 (REVIEWER_ESCALATION_PLAN.md): any rework redispatch - a
        # CI-triggered rework (story["ci_rework"]) OR a reviewer
        # REQUEST_CHANGES rework (story["review_feedback"]) - raises the
        # agent's done-bar to full-suite-green so it cannot declare done
        # while its own edit left the rest of the suite broken. The
        # reviewer's own pass is acceptance-scoped (see
        # _scope_test_cmd_to_acceptance in review.py), so a regression
        # outside the acceptance paths is otherwise invisible until the
        # merge gate - or, worse, never re-checked at all if `done` is
        # accepted on a broken tree (observed live 2026-07-22,
        # MODE-29-REVIEW-STORY-LOCK-GUARD: a rework redispatch's own edit
        # orphaned a function definition, the agent called done with 79
        # tests failing, and nothing rejected it because this gate was
        # only armed for ci_rework). Local-only: the env reaches the local
        # agent subprocess; Claude's dispatch signature stays clean.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and (
            story.get("ci_rework") or story.get("review_feedback")
        ):
            dispatch_kwargs["rework_full_suite"] = True
        # A reviewer-feedback rework gets its own signal, distinct from
        # REWORK_FULL_SUITE (which both kinds of rework share): on a CI-fail
        # rework the agent's own committed test is the defect and a green
        # suite settles it, but on a reviewer-feedback rework the acceptance
        # oracle is usually ALREADY green when the round starts - that first
        # dispatch is exactly what the reviewer read - so oracle-green cannot
        # stand for "the findings are addressed". The oracle harness needs to
        # tell the two apart to know which done-bar applies.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and story.get("review_feedback"):
            dispatch_kwargs["review_feedback_rework"] = True

        if resume_via_transcript:
            dispatch_kwargs["resume_transcript_path"] = transcript_path
            # Same tech-lead-decomposition logic as the initial checklist,
            # applied to review feedback: a reviewer's prose diagnosis is
            # itself a coarse brief for a weak executor. Re-run per rework
            # cycle (unlike the initial checklist, which plans once) since
            # each cycle's feedback is different. Fails open to the raw
            # feedback format on any planner failure - identical contract
            # to the initial-dispatch checklist.
            fix_checklist = None
            if dispatch_backend in _LOCAL_BACKEND_NAMES:
                fix_checklist = _run_rework_planner(
                    review_feedback,
                    dispatch_backend=dispatch_backend,
                    local_model=spec["model"],
                    plan_role_config=_plan_role_config(plan_name),
                    worktree=str(worktree_path),
                )
            # Rework test-author phase: when the reviewer's feedback itself
            # calls for NEW test(s) (e.g. a regression test reproducing a
            # named bug), author them via the dedicated test_author role
            # BEFORE the main executor redispatch, then steer the executor
            # to implement against those already-committed tests instead of
            # writing the tests itself - applying the tech-lead/weak-
            # executor split (TDD_SPLIT_PRODUCTION_PLAN.md) one rework cycle
            # later. Gated to local-family dispatch (Claude doesn't need the
            # crutch) and to a per-sha "already done" marker so a
            # retried/resumed dispatch on the same reviewer verdict never
            # re-runs the phase or re-asks the classifier. Fails open: on
            # any failure, unconfigured role, or "no new tests required",
            # rework_tests_note stays "" and the resumed prompt + manifest
            # are byte-for-byte identical to today's monolithic rework
            # dispatch.
            rework_tests_committed = False
            if dispatch_backend in _LOCAL_BACKEND_NAMES:
                already_done_sha = story.get("rework_test_author_done_for_sha")
                current_sha = story.get("last_reviewed_sha")
                if (
                    current_sha
                    and already_done_sha != current_sha
                    and _rework_requires_new_tests(
                        review_feedback,
                        dispatch_backend=dispatch_backend,
                        local_model=spec["model"],
                        plan_role_config=_plan_role_config(plan_name),
                    )
                ):
                    rework_tests_committed = _run_rework_test_author_phase(
                        story,
                        story_key=story_key,
                        worktree_path=worktree_path,
                        dispatch_backend=dispatch_backend,
                        local_model=spec["model"],
                        review_feedback=review_feedback,
                        fix_checklist=fix_checklist,
                        plan_role_config=_plan_role_config(plan_name),
                    )
                    if rework_tests_committed:
                        story["rework_test_author_done_for_sha"] = current_sha
            rework_tests_note = ""
            if rework_tests_committed:
                rework_tests_note = (
                    "\n\nThe regression test(s) reproducing this bug have already "
                    "been written and committed to this branch by your tech lead. "
                    "Do NOT create, write, or modify any test file. "
                    f"{_NEVER_TOUCH_TESTS_STEERING} Run them to see the current "
                    "failures, then implement until they pass."
                )
            rework_attempts = story.get("rework_attempts", 0)
            if rework_attempts >= 2:
                round_prefix = (
                    f"This is rework round {rework_attempts}. A previous attempt "
                    f"already redispatched on this same feedback and did not fully "
                    f"resolve it -- read the feedback below carefully rather than "
                    f"repeating the same partial fix. "
                )
            else:
                round_prefix = ""
            # Token dedup (measured 2026-09-04: each rework cycle re-injected
            # the full ~7-8K-char feedback into the SAME resumed transcript,
            # ~22K chars over 3 rounds). If this exact feedback is already in
            # the resumed transcript from a prior cycle, reference it instead
            # of re-embedding it. Fail OPEN to re-injection on any read/parse
            # problem — a token optimization must never break dispatch.
            feedback_already_seeded = False
            try:
                prior_msgs = json.loads(transcript_path.read_text())
                feedback_already_seeded = any(
                    isinstance(m.get("content"), str) and review_feedback in m["content"]
                    for m in prior_msgs
                )
            except Exception:  # noqa: BLE001 (fail open to re-injection; a token optimization must never break dispatch)
                feedback_already_seeded = False
            if feedback_already_seeded:
                feedback_note = (
                    "Original review feedback: unchanged from your previous "
                    "rework cycle — it is already in your transcript above; "
                    "re-read it there."
                )
                feedback_lead = (
                    "The code reviewer REQUESTED CHANGES on your previous attempt. "
                    "Address the review feedback already in your transcript above "
                    "(unchanged from your previous rework cycle)."
                )
            else:
                feedback_note = (
                    f"Original review feedback (for reference):\n{review_feedback}"
                )
                feedback_lead = (
                    "The code reviewer REQUESTED CHANGES on your previous attempt. "
                    f"Address this feedback:\n{review_feedback}"
                )
            if fix_checklist:
                dispatch_kwargs["resume_append_content"] = (
                    f"{round_prefix}"
                    "The code reviewer REQUESTED CHANGES on your previous "
                    "attempt. Your tech lead has translated the feedback "
                    f"into a fix checklist:\n{fix_checklist}\n\n"
                    f"{feedback_note}"
                    f"{revised_instructions_note}"
                    f"{rework_tests_note}"
                )
            else:
                dispatch_kwargs["resume_append_content"] = (
                    f"{round_prefix}"
                    f"{feedback_lead}"
                    f"{revised_instructions_note}"
                    f"{rework_tests_note}"
                )

        # W4-logging slice: hand the agent subprocess the story's correlation
        # ID (PIPELINE_CORRELATION_ID) so its own agent.log lines join up with
        # the orchestrator's events. Backend.dispatch has no env parameter and
        # the drivers build the subprocess env from os.environ at launch time
        # (app/backend_ollama.py's `{**os.environ, ...}` and
        # app/backend_claude.py's _first_party_claude_env), so the var is
        # staged in os.environ for exactly this synchronous launch call and
        # restored afterwards; dispatches are serialized per plan by the
        # _store transaction, so no concurrent launch observes a torn value.
        prev_correlation_env = os.environ.get("PIPELINE_CORRELATION_ID")
        if story.get("correlation_id"):
            os.environ["PIPELINE_CORRELATION_ID"] = story["correlation_id"]
        try:
            handle = backend.get_backend("dispatch", name=dispatch_backend).dispatch(
                **dispatch_kwargs
            )
        finally:
            if prev_correlation_env is None:
                os.environ.pop("PIPELINE_CORRELATION_ID", None)
            else:
                os.environ["PIPELINE_CORRELATION_ID"] = prev_correlation_env

        story["status"] = "in_progress"
        story["pid"] = handle.pid
        story["dispatched_at"] = datetime.now(timezone.utc).isoformat()
        story["worktree"] = str(worktree_path)
        story["log"] = str(log_path)
        # Record the concrete model the agent actually boots with (the local
        # backend resolves a logical tier like "sonnet" to e.g.
        # "minimax-m3:cloud"). The dashboard shows this instead of the plan's
        # declared story["model"] so what's displayed matches what ran. The
        # declared tier is left untouched (it's a routing hint).
        if getattr(handle, "model", None):
            story["dispatched_model"] = handle.model
        # Snapshot the agent_instructions this dispatch actually handed the
        # agent, so the next rework redispatch can diff against it to detect
        # an operator's patch_story edit (see revised_instructions_note
        # above). Recorded on every dispatch - cold or resumed, any backend -
        # so the baseline is always current regardless of how the next rework
        # is routed.
        story["_dispatched_agent_instructions"] = story.get("agent_instructions", "")
        _atomic_write_json(manifest_path, manifest)

        # W4-logging slice: stamp the success event with the story's
        # correlation ID (plus the role/model context already in scope here)
        # so dispatch's own events join the same trace as the agent
        # subprocess's PIPELINE_CORRELATION_ID. Published straight onto the
        # process bus — make_event stamps correlation_id at TOP level (the
        # W4L-01 kwarg). Lazy imports mirror persistence._notify_user so a
        # monkeypatched event_wiring.get_bus is honored; the wired bus has no
        # "agent_dispatched" handler, so this is a no-op there.
        from .event_wiring import get_bus
        from .events import make_event

        get_bus().publish(
            make_event(
                "agent_dispatched",
                plan_name,
                story_key=story_key,
                payload={
                    "role": story.get("role"),
                    "model": spec["model"],
                    "backend": dispatch_backend,
                    "attempt": story.get("dispatch_attempts", 0),
                    "pid": handle.pid,
                    "branch": branch,
                },
                correlation_id=story.get("correlation_id"),
            )
        )

        return {
            "ok": True,
            "story_key": story_key,
            "pid": handle.pid,
            "branch": branch,
            "resumed": resuming,
        }


# build_detect owns the single test-failure parser; dispatch imports it rather
# than carrying a second copy. This import sits at the end of the module because
# pipeline.build_detect eagerly imports pipeline.server, which imports back into
# pipeline.dispatch (via pipeline.story_status) - a top-of-file import would
# re-enter this half-initialized module and raise ImportError.
from .build_detect import detect_test_command, failed_node_ids  # noqa: F401
