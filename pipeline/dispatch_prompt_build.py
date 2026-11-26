"""Brief, context and environment assembly carved out of ``pipeline.dispatch``.

Holds the stretch of ``_dispatch_story_impl`` between the worktree phase and
the process spawn: resolve context and backend, snapshot the baseline, build
the prompt spec, materialize the acceptance fixtures, run the pre-executor
phases (oracle gate, test author, planner) and assemble the driver kwargs.

The caller holds ``_plan_lock``; nothing here touches it. Names tests patch on
``pipeline.dispatch`` are reached through that module at call time.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PromptBuild:
    """Outcome of :func:`build_prompt`.

    ``early`` is a result the caller must return as-is (blocked, pending or
    gated dispatch); when it is set no other field is meaningful.
    """

    early: dict[str, Any] | None = None
    spec: dict[str, Any] = field(default_factory=dict)
    dispatch_backend: str = ""
    dispatch_kwargs: dict[str, Any] = field(default_factory=dict)
    executor_is_resume: bool = False
    resume_via_transcript: bool = False
    transcript_path: Path = Path()
    review_feedback: str = ""
    revised_instructions_note: str = ""
    log_path: Path = Path()


def build_prompt(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    worktree_path: Path,
    *,
    resuming: bool,
) -> PromptBuild:
    """Assemble the dispatch spec and driver kwargs for one story."""
    from . import dispatch as _home

    # Resolve external context BEFORE the ticket moves to in-progress so a
    # required-source failure leaves the story's status untouched.
    context_block, blocked = _home._step_resolve_context(
        plan_name, story_key, story, manifest
    )
    if blocked is not None:
        return PromptBuild(early=blocked)

    _home.get_ticket_provider().set_state(story_key, _home.LogicalState.IN_PROGRESS, plan_name)

    # Resolve concrete backend AND model for this story (shared with the
    # per-story dispatch gate in _advance_pipeline_locked). REG-1: both
    # now come from role_registry.resolve_role via
    # _resolve_dispatch_target, so a registry-pinned roles.dispatch
    # model actually runs instead of the driver's env default.
    dispatch_backend, dispatch_model = _home._resolve_dispatch_target(
        story,
        plan_role_config=_home._plan_role_config(plan_name),
        layer_routing=_home.layer_routing_for_plan(plan_name),
    )
    # Persist so check_story_status and escalation see which backend ran.
    story["backend"] = dispatch_backend
    _home.stamp_test_scope(plan_name, story_key, manifest, story)
    _record_baseline(plan_name, story_key, story, worktree_path, dispatch_backend, resuming)

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
    resume_via_transcript = bool(
        dispatch_backend in _home._LOCAL_BACKEND_NAMES
        and review_feedback
        and transcript_path.exists()
        and not _home._transcript_ends_with_done(transcript_path)
    )
    revised_instructions = story.get("agent_instructions", "")
    prior_dispatched = story.get("_dispatched_agent_instructions")
    revised_instructions_note = _home._revised_instructions_note(
        resume_via_transcript, revised_instructions, prior_dispatched
    )

    spec = _assemble_spec(
        plan_name, story_key, story, resuming, resume_via_transcript,
        review_feedback, context_block, dispatch_model,
    )
    worktree_path.mkdir(parents=True, exist_ok=True)
    log_path = worktree_path / "agent.log"

    _warn_on_concurrent_local_dispatch(
        plan_name, story_key, story, dispatch_backend, dispatch_model, spec
    )
    acceptance_paths = _materialize_acceptance(
        plan_name, story_key, story, worktree_path, resuming
    )
    oracle_block = _check_oracle(plan_name, story_key, story, manifest, worktree_path, resuming)
    if oracle_block is not None:
        return PromptBuild(early=oracle_block)

    test_author_marker = worktree_path / ".tdd_split_test_author_done"
    detached_outcome = _run_test_author(
        plan_name, story_key, story, manifest, worktree_path,
        dispatch_backend, spec, test_author_marker, resuming,
    )
    if detached_outcome == "pending":
        return PromptBuild(early={"ok": True, "pending": "test_author", "story_key": story_key})
    executor_is_resume = resuming and detached_outcome not in (
        "authored",
        "fell_open",
    )

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
    _home._apply_planner_checklist(
        story,
        spec,
        dispatch_backend=dispatch_backend,
        resuming=executor_is_resume,
        worktree_path=worktree_path,
        test_author_marker=test_author_marker,
        plan_name=plan_name,
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
            f"{_home._NEVER_TOUCH_TESTS_STEERING} Run them to see the current "
            "failures, then implement until they pass."
        )

    dispatch_kwargs = _driver_kwargs(
        story, spec, dispatch_backend, worktree_path, log_path,
        acceptance_paths, executor_is_resume,
    )
    return PromptBuild(
        spec=spec,
        dispatch_backend=dispatch_backend,
        dispatch_kwargs=dispatch_kwargs,
        executor_is_resume=executor_is_resume,
        resume_via_transcript=resume_via_transcript,
        transcript_path=transcript_path,
        review_feedback=review_feedback or "",
        revised_instructions_note=revised_instructions_note,
        log_path=log_path,
    )


def _record_baseline(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    worktree_path: Path,
    dispatch_backend: str,
    resuming: bool,
) -> None:
    """BASELINE-1: snapshot the test command's state on a story's first dispatch.

    On a story's FIRST dispatch, run the detected test
    command ONCE against the freshly created, still-unmodified worktree
    and remember whether it already failed. Observed live 2026-09-17:
    4 separate dispatched stories each independently rediscovered the
    same pre-existing, out-of-scope failures (a "5 pre-existing ...
    failures (httpx missing in subprocess interpreter)" pattern, named
    almost verbatim in 4 separate journal entries across 2 plans) and
    each spent part of its own step/rework budget investigating them
    before concluding they were unrelated to its task. Recording the
    baseline here lets the prompt below tell the agent up front which
    failures predate its own changes. Gated exactly like the
    test-author/planner phases further down:
      - a local-family backend (the same weak-local-executor rationale)
      - not resuming (a resumed dispatch acts on a worktree the agent
        has already been editing, so there is no meaningful "before"
        state left to snapshot)
      - no existing marker (belt-and-suspenders with `resuming`, and
        what stops a second run on a later fresh-looking call)
    Never a gate: the helper is wrapped so any failure degrades to "no
    baseline", and the marker is written unconditionally right after so
    a raising snapshot is not retried on every subsequent dispatch.
    A marker already present means the snapshot ran for this worktree:
    the story is recorded from it instead (see record_dispatch_baseline).
    """
    from . import dispatch as _home

    if dispatch_backend in _home._LOCAL_BACKEND_NAMES:
        _home.record_dispatch_baseline(
            _home._store,
            plan_name,
            story_key,
            story,
            worktree_path,
            marker=worktree_path / ".dispatch_baseline_test_checked",
            snapshot=_home.baseline_snapshot_for(story, _home._run_baseline_test_snapshot),
            rerun=not resuming,
        )


def _assemble_spec(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    resuming: bool,
    resume_via_transcript: bool,
    review_feedback: str | None,
    context_block: Any,
    dispatch_model: str | None,
) -> dict[str, Any]:
    from . import dispatch as _home

    # The resume journal comes from the record store; the read is
    # fail-soft: no journal for a malformed file or a rejected key.
    journal: list[dict] = []
    if resuming:
        try:
            journal = _home.bound_records.get_journal(_home._store, plan_name, story_key)
        except _home.InvalidRecordKeyError:
            _home.logger.warning("no resume journal for %s/%s", plan_name, story_key)

    spec = _home._build_dispatch_command(
        story,
        story_key,
        plan_name=plan_name,
        resume_journal=journal or None,
        review_feedback=None if resume_via_transcript else review_feedback,
        context_block=context_block,
    )
    # REG-1: dispatch_model is the resolved model for this story - the
    # story's own pin translated to a concrete tag, else the registry's
    # dispatch model - and it always wins over the raw spec value
    # (spec["model"] carries story["model"]/persona precedence from
    # _build_dispatch_command). It is None when the resolved model does
    # not belong to the backend that actually won (a claude dispatch is
    # never handed an ollama tag - _resolve_dispatch_target already
    # drops it), so the guard keeps spec["model"] intact there.
    if dispatch_model:
        spec["model"] = dispatch_model
    # HARDEN-1: the executor must be told its cwd is authoritative on
    # EVERY dispatch - fresh or resumed - before any plan-authored brief
    # (agent_instructions) it may contain. A brief once carried an
    # absolute path to the shared primary checkout and the agent ran
    # every command there, landing commits straight on master. Prepend
    # unconditionally so the rule survives an empty brief too.
    spec["prompt"] = (
        f"{_home.WORKTREE_SCOPE_RULE}\n\n"
        f"{_home.triage_rebrief.diagnosis_block(story)}{spec['prompt']}"
    )
    # BASELINE-1: when the fresh-dispatch baseline snapshot found
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
    if baseline and baseline.get("returncode") not in (None, 0) and not resuming:
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
    return spec


def _warn_on_concurrent_local_dispatch(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    dispatch_backend: str,
    dispatch_model: str | None,
    spec: dict[str, Any],
) -> None:
    from . import dispatch as _home

    # Gap 7: surface multi-model concurrent-dispatch risk. MAX_CONCURRENT_AGENTS
    # is a process-count cap with no model/VRAM awareness, and Ollama's
    # `/api/ps` reports whatever's currently loaded. If a *different* model
    # is already in VRAM and we're about to dispatch a second story on a
    # different model, Ollama will swap the existing model out to make room
    # (or OOM-split if 24GB unified memory is tight). Warn, don't block:
    # same-model concurrency is safe, and even a swap is just slow.
    if (
        dispatch_backend in _home._LOCAL_BACKEND_NAMES
        and _home.MAX_CONCURRENT_AGENTS > 1
        and _home._count_in_progress_agents() > 0
    ):
        target_model = story.get("model") or dispatch_model or spec.get("model")
        if target_model:
            # spec["model"]/story["model"] may be an unresolved tier
            # name (e.g. "sonnet"), which never matches anything in
            # `loaded` (concrete Ollama tags) and would otherwise warn
            # on every dispatch regardless of what's actually loaded.
            target_model = _home.backend._resolve_local_model(target_model)
        try:
            loaded = _home.backend._ollama_loaded_models(
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
            _home._notify_user(plan_name, msg)
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
        dispatch_backend in _home._LOCAL_BACKEND_NAMES
        and _home.MAX_CONCURRENT_AGENTS > 1
        and _home._count_in_progress_agents() > 0
    ):
        try:
            detected_np = _home.backend._ollama_serving_parallelism()
        except Exception:  # noqa: BLE001 (observability hook, never a gate)
            detected_np = None
        if detected_np is not None and detected_np < _home.MAX_CONCURRENT_AGENTS:
            msg = (
                f"ollama serving parallelism ({detected_np}) is below "
                f"MAX_CONCURRENT_AGENTS ({_home.MAX_CONCURRENT_AGENTS}): a "
                f"second concurrent dispatch will queue behind the "
                f"first and may hit the 180s read-silence timeout. "
                f"Re-apply `launchctl setenv OLLAMA_NUM_PARALLEL "
                f"{_home.MAX_CONCURRENT_AGENTS}` and restart Ollama, or set "
                f"MAX_CONCURRENT_AGENTS=1."
            )
            _home._notify_user(plan_name, msg)
            logging.getLogger("pipeline").warning(msg)


def _materialize_acceptance(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    worktree_path: Path,
    resuming: bool,
) -> Any:
    """Write the acceptance fixtures into the worktree; return their rel paths.

    Fix #1: if the story carries an `acceptance` block, materialize the
    oracle files into the worktree BEFORE the backend launches so the local
    harness can grade against them. The plan's acceptance source is
    AUTHORITATIVE on every dispatch that is not a status-based resume
    (interrupted / changes_requested) and must always win — even when the
    fixture path collides with a file a prior story already merged to the
    base branch (the worktree inherits that file; failing to overwrite it
    silently grades the stale, already-satisfied file and produces a false
    green with zero implementation). Only a genuine resume skips the
    write: the oracle may have evolved the fixture mid-run into a
    committed WIP, and overwriting would discard that evolution. The skip
    is gated on the story's STATUS, never on worktree existence — a
    leftover worktree from a dead earlier attempt is not a resume.
    """
    from . import dispatch as _home

    acceptance = story.get("acceptance") or []
    acceptance_paths = _home._acceptance_rel_paths(story)
    resumed_story = story.get("status") in ("interrupted", "changes_requested")
    for entry in acceptance:
        rel = Path(str(entry.get("path", "")))
        if rel.is_absolute() or ".." in rel.parts:
            _home._notify_user(
                plan_name,
                f"{story_key}: acceptance fixture path outside the worktree rejected: {rel!s}",
            )
            continue
        target = worktree_path / rel
        if resumed_story and target.exists():
            logging.getLogger("pipeline").info(
                "acceptance fixture %s preserved: resumed run", rel,
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["source"])
        logging.getLogger("pipeline").info(
            "acceptance fixture %s materialized (resuming=%s)", rel, resuming,
        )

    # Record a digest of each fixture's AUTHORITATIVE manifest source
    # (never the worktree file, which could already be rewritten) so a
    # later gate can prove the read-only oracle was not rewritten - see
    # pipeline.ci._acceptance_tampered. Recorded on EVERY dispatch,
    # including a resumed one, since the authoritative source hasn't
    # changed and re-recording also repairs a story dispatched before
    # this existed.
    story["acceptance_digests"] = _home.acceptance_digests(story)
    return acceptance_paths


def _check_oracle(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    worktree_path: Path,
    resuming: bool,
) -> dict[str, Any] | None:
    """Pre-dispatch oracle gate; return the blocked result or None.

    A fixture that cannot pass no matter what
    is implemented (a broken helper, a bad CLI invocation) or that is
    already satisfied with zero implementation burns an implementer's
    entire step budget for no signal (observed live 2026-07-30,
    LAUNCHD-PLIST-PORTABILITY). Skipped on a resumed run: the oracle
    was already validated on the fresh dispatch, and the worktree may
    legitimately carry WIP that changes the outcome.
    """
    from . import dispatch as _home

    if resuming:
        return None
    oracle_check = _home.validate_acceptance_fixtures(story, worktree_path)
    if oracle_check["state"] not in ("errors", "passes", "empty"):
        return None
    story["status"] = "blocked_oracle"
    story["oracle_gate"] = oracle_check
    _home._notify_user(
        plan_name,
        f"{story_key} not dispatched: acceptance oracle is "
        f"unusable ({oracle_check['state']}) - {oracle_check['detail']}",
    )
    _home.bound_records.put_manifest(_home._store, plan_name, manifest)
    return {
        "status": "blocked_oracle",
        "oracle_gate": oracle_check,
    }


def _run_test_author(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    worktree_path: Path,
    dispatch_backend: str,
    spec: dict[str, Any],
    test_author_marker: Path,
    resuming: bool,
) -> str:
    """Run the pre-executor test-author phase; return the detached outcome.

    TDD_SPLIT_PRODUCTION_PLAN.md: an ALWAYS-ON pre-executor
    test-authoring dispatch (a full agent-loop, BLOCKING until it
    exits - unlike the planner checklist below, this produces a real
    commit the executor's worktree must already have) in THIS
    worktree before the main executor starts. The phase is
    unconditional for local-family dispatch. Gated on:
      - a local-family backend (same rationale as the planner: the
        crutch exists for the weak local executor; Claude doesn't
        need it)
      - not resuming (a rework redispatch acts on the SAME tests it
        already has; it never gets a fresh test-authoring pass)
      - no existing test-author marker in the worktree (belt-and-
        suspenders with `resuming`, mirrors plan_path's own check
        in the planner)
    _run_test_author_phase never raises and a False return (role
    unconfigured/refused, dispatch failure, timeout, or no commit
    produced) falls open to today's unmodified monolithic dispatch -
    never a gate (§2.5).
    """
    from . import dispatch as _home

    detached_outcome = _home._step_detached_test_author(
        story,
        story_key=story_key,
        worktree_path=worktree_path,
        dispatch_backend=dispatch_backend,
        local_model=spec["model"],
        plan_name=plan_name,
        plan_role_config=_home._plan_role_config(plan_name),
        manifest=manifest,
        marker_path=test_author_marker,
        resuming=resuming,
    )
    if detached_outcome == "not_applicable" and (
        dispatch_backend in _home._LOCAL_BACKEND_NAMES
        and not resuming
        and not test_author_marker.exists()
    ) and _home._run_test_author_phase(
        story,
        story_key=story_key,
        worktree_path=worktree_path,
        dispatch_backend=dispatch_backend,
        local_model=spec["model"],
        plan_name=plan_name,
        plan_role_config=_home._plan_role_config(plan_name),
    ):
        test_author_marker.write_text("ok\n")
        story["tdd_split"] = True
    return detached_outcome


def _driver_kwargs(
    story: dict[str, Any],
    spec: dict[str, Any],
    dispatch_backend: str,
    worktree_path: Path,
    log_path: Path,
    acceptance_paths: Any,
    executor_is_resume: bool,
) -> dict[str, Any]:
    from . import dispatch as _home

    dispatch_kwargs: dict[str, Any] = {
        "prompt": spec["prompt"],
        "system": spec["system"],
        "model": spec["model"],
        "allowed_tools": spec["allowed_tools"],
        "cwd": worktree_path,
        "log_path": log_path,
        "append": executor_is_resume,
    }
    # Only the local driver accepts/uses `acceptance`; pass it through when
    # we're actually invoking that driver so Claude's signature stays clean.
    if dispatch_backend in _home._LOCAL_BACKEND_NAMES and acceptance_paths:
        dispatch_kwargs["acceptance"] = acceptance_paths
        dispatch_kwargs["story_summary"] = story.get("summary", "")
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
    if dispatch_backend in _home._LOCAL_BACKEND_NAMES and (
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
    if dispatch_backend in _home._LOCAL_BACKEND_NAMES and story.get("review_feedback"):
        dispatch_kwargs["review_feedback_rework"] = True

    dispatch_kwargs.update(
        _home.local_scope_kwargs(story, dispatch_backend, worktree_path)
    )
    return dispatch_kwargs
