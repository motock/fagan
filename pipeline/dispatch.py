import hashlib
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import backend

from .config import WORKTREE_SCOPE_RULE
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
_module_level_function_names = _ServerRef("_module_level_function_names")
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
compose_attempt_facts = _ServerRef("compose_attempt_facts")
compose_rebriefed_instructions = _ServerRef("compose_rebriefed_instructions")
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
_rework_requires_new_tests = _ServerRef("_rework_requires_new_tests")
_run_planner = _ServerRef("_run_planner")
_run_rework_planner = _ServerRef("_run_rework_planner")
_run_rework_test_author_phase = _ServerRef("_run_rework_test_author_phase")
_run_test_author_phase = _ServerRef("_run_test_author_phase")
_test_files_added_on_branch = _ServerRef("_test_files_added_on_branch")
_test_names_in_file = _ServerRef("_test_names_in_file")
_validate_key = _ServerRef("_validate_key")
collect_attempt_facts = _ServerRef("collect_attempt_facts")
collect_failure_evidence = _ServerRef("collect_failure_evidence")
detect_unsatisfiable_signal = _ServerRef("detect_unsatisfiable_signal")
diagnose_failure = _ServerRef("diagnose_failure")
get_ticket_provider = _ServerRef("get_ticket_provider")


def _resolve_dispatch_backend(story: dict[str, Any], env_backend: str) -> str:
    """Resolve the concrete backend name for a story's dispatch.

    Shared by dispatch_story (which persists the result onto the story) and
    the per-story dispatch gate in _advance_pipeline_locked (which must gate
    each story by ITS OWN backend+model, not a blanket env-default gate).
    Priority order:
      1. story["backend"] already set (e.g. from an escalation flip)
      2. PIPELINE_BACKEND_DISPATCH=auto  → a-priori router
      3. PIPELINE_BACKEND_DISPATCH=local|claude  → that driver directly
    Then the persona-based and unwinnable-scope safety overrides (both only
    when the story had no explicit backend, so a prior escalation flip wins
    as-is and is never re-routed here).
    """
    dispatch_backend = story.get("backend") or (
        _route_dispatch_backend(story) if env_backend == "auto" else env_backend
    )
    # Persona-based safety override: a security persona always dispatches to
    # Claude, regardless of dispatch mode (auto/local/claude) - unless the
    # story already had an explicit backend (a prior escalation flip), which
    # wins as-is and is never re-routed here.
    if not story.get("backend") and _persona_requires_claude(story):
        dispatch_backend = "claude"
    # Unwinnable-as-scoped safety override: a repo-wide, unscoped lint/fix
    # sweep always dispatches to Claude too, for the same reason (Mode 40
    # retro #4) - see _story_has_unwinnable_local_scope's docstring.
    if not story.get("backend") and _story_has_unwinnable_local_scope(story):
        dispatch_backend = "claude"
    return dispatch_backend



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
            try:
                with _scoped_repo_root(plan_name) as repo_root:
                    with _try_acquire_git_lock(repo_root) as acquired:
                        if acquired:
                            subprocess.run(
                                ["git", "fetch", "origin", _default_branch()],
                                cwd=repo_root,
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                    subprocess.run(
                        [
                            "git",
                            "worktree",
                            "add",
                            "-b",
                            branch,
                            str(worktree_path),
                            f"origin/{_default_branch()}",
                        ],
                        cwd=repo_root,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
            except subprocess.CalledProcessError as e:
                # A fresh dispatch's git setup (fetch + worktree add) can fail
                # for reasons outside the story itself - e.g. repo_root has no
                # usable 'origin' remote, or git can't authenticate. Left
                # uncaught, this used to escape all the way to an unhandled
                # 500 with an EMPTY body (no JSON), which broke every caller's
                # response.json() with "Expecting value: line 1 column 1 (char
                # 0)" - reproduced live twice via chat's dispatch_story tool
                # call. Return a structured, actionable failure instead.
                stderr = (e.stderr or "").strip()
                detail = f": {stderr}" if stderr else ""
                return {
                    "ok": False,
                    "error": (
                        f"git setup failed for repo_root {str(repo_root)!r} "
                        f"(command {' '.join(e.cmd)!r}, exit {e.returncode})"
                        f"{detail}"
                    ),
                }
            _exclude_worktree_logs_from_tracking(Path(repo_root))
            # A fresh worktree has no .venv (gitignored) - give it its own
            # complete one now rather than let it fall back to (and
            # potentially mutate) the shared main-repo venv other
            # concurrently-dispatched stories may be using. See
            # _provision_worktree_venv's docstring for the failure mode
            # this closes (root-caused live on RUFF-016-ADOPTION).
            # No-ops for non-Python projects or ones without a
            # requirements file.
            _provision_worktree_venv(worktree_path)
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

        # Resolve concrete backend name for this story (shared with the
        # per-story dispatch gate in _advance_pipeline_locked).
        env_backend = (
            os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()
        )
        dispatch_backend = _resolve_dispatch_backend(story, env_backend)
        # Persist so check_story_status and escalation see which backend ran.
        story["backend"] = dispatch_backend

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
        # HARDEN-1: the executor must be told its cwd is authoritative on
        # EVERY dispatch - fresh or resumed - before any plan-authored brief
        # (agent_instructions) it may contain. A brief once carried an
        # absolute path to the shared primary checkout and the agent ran
        # every command there, landing commits straight on master. Prepend
        # unconditionally so the rule survives an empty brief too.
        spec["prompt"] = f"{WORKTREE_SCOPE_RULE}\n\n{spec['prompt']}"
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
            target_model = spec.get("model") or story.get("model")
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



def _find_dead_new_functions(worktree: Path, base_branch: str) -> list[str]:
    """Detect newly-added module-level functions (in .py files changed
    since base_branch, excluding test files) whose name appears NOWHERE
    else in the tracked worktree - i.e. defined but never called or
    referenced, not even from a different file (a new public entry point
    called only from elsewhere in the repo must not false-positive here).

    A cheap, conservative text-based heuristic, not a full call-graph
    analysis: a name occurring anywhere else in the worktree (even a
    comment, even in another file) is treated as "referenced", keeping
    false positives near zero. A name occurring ONLY on its own `def`
    line, repo-wide, is a strong, low-noise signal of dead code.

    Root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2: a
    correctly-implemented, correctly-unit-tested helper function
    (`_ci_rework_feedback`) was added but never wired into the production
    call path it was meant to replace - invisible to any test that only
    exercises the function in isolation, since the story's own tests
    called it directly rather than through the code path that was
    supposed to route to it. The real LLM reviewer caught it, but that
    spends a whole review cycle on something this cheap, static,
    pre-review check catches for free (see _run_lint_gate for the sibling
    pattern this mirrors).

    Best-effort: any git/IO failure returns [] (fail open - a quality
    signal, not a security boundary, must never block or corrupt a
    story's dispatch).
    """
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=AM", base_branch, "HEAD"],
            check=False,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if diff.returncode != 0:
        return []

    dead: list[str] = []
    for rel_path in diff.stdout.splitlines():
        rel_path = rel_path.strip()
        if not rel_path.endswith(".py"):
            continue
        base_name = Path(rel_path).name
        if base_name.startswith("test_") or base_name.endswith("_test.py"):
            continue
        full_path = worktree / rel_path
        if not full_path.is_file():
            continue
        try:
            source = full_path.read_text()
        except OSError:
            continue

        try:
            old_show = subprocess.run(
                ["git", "show", f"{base_branch}:{rel_path}"],
                check=False,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        old_names = (
            _module_level_function_names(old_show.stdout)
            if old_show.returncode == 0
            else set()
        )
        new_names = _module_level_function_names(source) - old_names

        for fn_name in sorted(new_names):
            if fn_name.startswith("__") and fn_name.endswith("__"):
                continue  # dunder - never a candidate
            try:
                grep = subprocess.run(
                    ["git", "grep", "--count", "-w", fn_name],
                    check=False,
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            # `git grep --count` prints "path:N" per matching tracked file
            # (exit 1, empty stdout, if no match anywhere - not an error).
            # The function's own def line contributes exactly 1; a repo-
            # wide total <= 1 means "only its own definition, nowhere else
            # in the tracked worktree" - not even a different file.
            total = sum(
                int(line.rsplit(":", 1)[-1])
                for line in grep.stdout.splitlines()
                if line.strip()
            )
            if total <= 1:
                dead.append(f"{rel_path}:{fn_name}")
    return dead



def _rebrief_step_cap_struggle(
    story: dict[str, Any], worktree: str, plan_role_config: dict | None = None,
    plan_name: str | None = None, story_key: str | None = None
) -> None:
    """CLAUDE.md Step 9: when an implementer hits the step cap, diagnose where
    it struggled and fold the root cause into agent_instructions so the resume
    isn't a blind retry. The resume path (_build_dispatch_command) re-reads
    agent_instructions, so the folded diagnosis reaches the next attempt.

    Fail-open by construction: a None/errored diagnosis leaves agent_instructions
    unchanged (compose_rebriefed_instructions is a no-op on None), so this can
    never block or worsen a retry. compose replaces (not stacks) any prior
    diagnosis block, so repeated step-caps keep the prompt bounded and refresh
    with the latest struggle. Must run while the worktree still exists - the
    evidence is the tail of its agent.log."""
    facts = collect_attempt_facts(worktree, story)
    evidence = collect_failure_evidence(worktree, story, facts=facts)
    try:
        unsat_reason = detect_unsatisfiable_signal(evidence)
        if unsat_reason is not None:
            _notify_user(
                plan_name,
                f"Story may be unsatisfiable as specified: {unsat_reason}. Story {story_key} may need re-planning rather than another retry.",
            )
    except Exception:
        logging.getLogger("pipeline").debug(
            "detect_unsatisfiable_signal/_notify_user failed during step-cap rebrief",
            exc_info=True)
    diagnosis = diagnose_failure(evidence, story, plan_role_config)
    story["agent_instructions"] = compose_rebriefed_instructions(
        story.get("agent_instructions", ""), diagnosis)
    # After the diagnosis, never before: composing a diagnosis truncates the
    # brief at DIAGNOSIS_HEADER, which would take a facts block appended ahead
    # of it with no replacement.
    story["agent_instructions"] = compose_attempt_facts(
        story.get("agent_instructions", ""), facts)


