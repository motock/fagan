"""Planner / test-author dispatch helpers for the pipeline MCP server.

_run_planner / _run_rework_planner: bounded single-turn LLM calls that turn
a coarse task or review feedback into an ordered checklist for a weak local
executor. _run_decompose: single-turn product-analyst call that turns a raw
goal into epics/stories JSON.

_run_test_author_phase: the TDD-split pre-executor dispatch (a separate
tech-lead writes the test suite before the main executor implements against
it). See TDD_SPLIT_PRODUCTION_PLAN.md.

All are patched via p.<name> by tests; server call sites use bare names ->
re-export -> patch lands. No server-global free-var reads except
_run_test_author_phase's call to _default_branch (lazy-imported from the
server to avoid the circular import).
"""

import logging
import os
import signal
import time
from pathlib import Path

import backend
import role_registry
from .persona import _persona_body, _persona_default_model
from .git_ops import _worktree_has_new_commits


# ---------- Guided-decomposition planner ----------
_PLANNER_SYSTEM = (
    "You are a tech lead writing an implementation checklist for a junior "
    "engineer who will work alone and may not reason precisely through "
    "subtle edge cases unassisted. Read the task below and produce an "
    "ordered checklist of concrete sub-steps (as many as the task genuinely "
    "needs - typically 3-10; split a step further rather than bundling "
    "tricky reasoning into one line), each with a short, verifiable "
    "done-criterion. Preserve test-driven-development ordering: a failing "
    "test before the implementation that makes it pass. For any step "
    "involving timing, state mutation, or a behavior that is easy to get "
    "subtly wrong (e.g. what happens on a rejected/failed call, a "
    "backwards-moving clock, or a read that must not have side effects), "
    "include a concrete worked example with actual numbers showing the "
    "correct result, and name the specific mistake a less careful "
    "implementation would make there. If the edge case touches state that "
    "persists across calls (a clock, counter, or high-water mark), the "
    "worked example must not stop at that one call's return value - trace "
    "at least one follow-up call afterward and confirm the state left "
    "behind still produces the correct result for it. A common mistake: "
    "correctly computing the CURRENT call's result (e.g. clamping a "
    "rejected/backwards step to no-op) while still overwriting the tracked "
    "state with a value that corrupts a later comparison - getting the "
    "immediate return value right is not sufficient if it leaves the "
    "object in a bad state for what comes next. Do not invent scope beyond "
    "what the task describes. "
    "CRITICAL STEERING for the junior engineer: all implementation work goes "
    "in the ONE implementation file named by the task; NEVER edit, rename, "
    "weaken, or delete the test files (anything matching test_*.py). If a "
    "test fails, the bug is in the implementation file - fix it there, never "
    "change the test. "
    "EDITING MECHANICS: this engineer reliably fails at surgical str_replace "
    "edits - they cannot construct a unique, matching old_str (observed "
    "live: every str_replace in a stubs-then-edit loop is rejected as "
    "'old_str occurs N times' or 'old_str not found', so they never make "
    "progress past stubs). Direct them to write COMPLETE files via "
    "create_file in one shot instead of a stubs-then-surgically-edit "
    "sequence: each implementation step should produce the WHOLE file with "
    "every method fully implemented (no `raise NotImplementedError` stubs "
    "to be filled in later by str_replace). If a fix is needed after running "
    "tests, rewrite the whole file via create_file again, do not str_replace. However, if the task is a small, targeted edit where most of the file's existing content must be preserved verbatim (e.g., adding a paragraph), direct the executor to use str_replace with an exact, verbatim anchor string copied from the current file. This avoids accidentally dropping unrelated sections when reconstructing via create_file. "
    "A THIRD case neither of the above fits: a targeted change to ONE function that is itself large (roughly 50+ lines) inside a bigger file - e.g. wrapping its whole existing body in a new guard/lock/try-block. Do NOT direct an in-place re-indent of that function; re-indenting many lines through a truncated file-viewing tool is exactly where this engineer fails worst. Instead direct the rename-and-delegate shape: rename the function (e.g. `foo` to `_foo_impl`), then define a new, short `foo` that does the guard/setup and calls `_foo_impl(...)` - the function body itself is never re-indented. If an example of this exact pattern already exists elsewhere in the same file, name it in the checklist as the one to copy. The new short `foo` MUST carry over everything that was attached to the ORIGINAL function, not just the call: any decorator(s) (e.g. `@mcp.tool()`) move to the new `foo`, NOT to `_foo_impl` - a decorator binds to whichever function object owns the name at decoration time, so leaving it on the renamed `_foo_impl` (or dropping it) silently deregisters the real entrypoint even though a test that calls the bare module attribute still passes. The original function's docstring also moves to the new `foo`, not `_foo_impl` - external callers and any tool description read the outer function's docstring. And any argument validation that ran as the ORIGINAL function's first statement(s) must run in the new `foo` BEFORE the guard/lock setup, not only inside `_foo_impl` and not after the lock is acquired - validating late lets an invalid argument reach the lock/guard code first. "
    "Make the checklist's FIRST line the steering rule above (name the "
    "implementation file and say: do not edit the test files), then the "
    "numbered sub-steps. Output ONLY the checklist - the steering line, "
    "then the numbered steps - no other preamble, no closing remarks."
)

# Optional clause spliced into _PLANNER_SYSTEM when the H3 scratchpad is on.
_PLANNER_SCRATCHPAD_CLAUSE = (
    " SCRATCHPAD (state memory): the junior engineer keeps a running note "
    "of what's already been tried and what the next step should be. The "
    "checklist's FIRST step must be to create a .agent_scratchpad.md file "
    "in the worktree root (erasing any prior content) and each subsequent "
    "step must end by appending its progress and the next step's hint to "
    "that file, so an interrupted run can resume from where it left off."
)

# Spliced into _PLANNER_SYSTEM when the TDD-split test-author phase already
# ran and committed a test file to this branch before the planner is asked
# to produce a checklist for the (separate, later) executor dispatch.
# _PLANNER_SYSTEM's base instruction ("preserve test-driven-development
# ordering: a failing test before the implementation") is written for the
# unsplit case and is unconditional - without this override the planner
# faithfully includes a "write the test file" step even though one was
# already authored and committed, producing a checklist that contradicts
# check_story_status's later steering (which tells the executor "tests
# already written, do not touch them"). Root-caused live 2026-07-25 on
# MODE40-CI-REWORK-FEEDBACK-V2: the glm-authored checklist's step 3 told
# the gpt-oss:20b executor to "Create the NEW file test_ci_rework_feedback.py"
# - a file the test-author phase had already committed - handing the
# executor a genuinely contradictory brief (create vs. never-touch the same
# file) that a weak model has no reliable way to resolve.
_TEST_AUTHOR_ALREADY_RAN_CLAUSE = (
    " IMPORTANT OVERRIDE: a separate tech-lead dispatch has ALREADY written "
    "and committed the test file(s) for this task to this branch, before "
    "you are being asked to plan. Ignore the test-driven-development "
    "instruction above for this checklist - do NOT include ANY step that "
    "creates, writes, or modifies a test file (or restates 'write a failing "
    "test' as a step). The checklist must be implementation-only: the "
    "junior engineer's job is to read the existing (currently failing, "
    "already-committed) tests to understand the required behavior, then "
    "write or edit the implementation until those tests pass."
)


def _planner_system(
    *, include_scratchpad: bool = False, tests_already_authored: bool = False,
) -> str:
    """The planner's system prompt, optionally augmented with the scratchpad
    and/or test-author-already-ran clauses. Base prompt (_PLANNER_SYSTEM) is
    returned unchanged when both are off, preserving the H3 ablation and the
    by-reference tests."""
    system = _PLANNER_SYSTEM
    if tests_already_authored:
        system += _TEST_AUTHOR_ALREADY_RAN_CLAUSE
    if include_scratchpad:
        system += _PLANNER_SCRATCHPAD_CLAUSE
    return system


def _default_planner_model_tag(registry: dict) -> str:
    """Concrete ollama/glm tag for the planner's model_fallback.

    resolve_role returns model_fallback verbatim (it does not resolve it
    against providers.<p>.models), so it must already be a concrete tag, not
    the friendly name 'glm'. Falls back to a module constant if the registry
    has no ollama/glm entry.
    """
    try:
        return registry["providers"]["ollama"]["models"]["glm"]["tag"]
    except (KeyError, TypeError):
        return "glm-5.2:cloud"


def _resolve_planner_backend(
    dispatch_backend: str,
    local_model: str,
    plan_role_config: dict | None = None,
) -> tuple[str, str]:
    """Resolve (provider, model) for the planner role. Always-on; no mode.

    Provider priority: plan role_config.planner.provider ->
    PIPELINE_BACKEND_PLANNER -> registry roles.planner.provider ->
    default "ollama" (so a stock install resolves to ollama/glm-5.2:cloud
    with no env vars set, via the registry pin from PR #151).
    Model priority: plan role_config.planner.model -> registry
    roles.planner.model -> the concrete ollama/glm tag (model_fallback).
    PIPELINE_LOCAL_PLANNER_MODEL (mirroring review.py's
    PIPELINE_LOCAL_REVIEW_MODEL) is the top-priority *model* override and
    wins over both, but only when the resolved provider is local-family, so
    a bare Ollama tag never leaks into a Claude planner.

    A garbage/unknown provider fails closed here (RoleRegistryError) so
    _run_planner's fail-open except catches it and dispatch proceeds with
    no checklist rather than crashing on a bogus backend name.
    """
    from .config import _LOCAL_BACKEND_NAMES

    registry = role_registry.load_registry()
    resolution = role_registry.resolve_role(
        "planner",
        plan_role_config=plan_role_config,
        registry=registry,
        default_provider="ollama",
        model_fallback=lambda: _default_planner_model_tag(registry),
    )
    provider, model = resolution.provider, resolution.model

    # Fail closed on a garbage/unknown provider so _run_planner fails open.
    known_providers = set(registry.get("providers", {})) | _LOCAL_BACKEND_NAMES
    if provider not in known_providers:
        raise role_registry.RoleRegistryError(
            f"planner resolved to unknown provider {provider!r} "
            f"(not one of {sorted(known_providers)})"
        )

    # Top-priority local-family model override (mirrors review.py:70-72).
    if provider in _LOCAL_BACKEND_NAMES:
        env_model = os.environ.get("PIPELINE_LOCAL_PLANNER_MODEL")
        if env_model:
            model = env_model
    return provider, model


def _run_planner(
    agent_instructions: str,
    *,
    dispatch_backend: str,
    local_model: str,
    include_scratchpad: bool = False, plan_role_config: dict | None = None,
    tests_already_authored: bool = False,
) -> str | None:
    """Call a bounded, single-turn LLM to produce an ordered sub-step
    checklist for agent_instructions.

    This is one complete() call, never an agent loop - it must stay cheap
    relative to the story's own dispatch or the economics this feature
    exists for collapse (see GUIDED_DECOMPOSITION_PLAN.md §3.1/§4.5).

    tests_already_authored: True when the TDD-split test-author phase
    already ran for this dispatch (see dispatch_story's test_author_marker
    check) - the checklist must be implementation-only in that case; see
    _TEST_AUTHOR_ALREADY_RAN_CLAUSE.

    Returns the raw checklist text, or None on any failure. Callers MUST
    treat None as "no plan" and fall open to the existing no-plan dispatch
    path - a broken, slow, or rate-limited planner call must never block or
    corrupt a story's dispatch. External boundary: delegates to the
    configured Backend. Tests mock this function.
    """
    try:
        backend_name, model = _resolve_planner_backend(
            dispatch_backend, local_model, plan_role_config=plan_role_config,
        )
        text = backend.get_backend("planner", name=backend_name).complete(
            agent_instructions,
            system=_planner_system(
                include_scratchpad=include_scratchpad,
                tests_already_authored=tests_already_authored,
            ),
            model=model,
        )
    except Exception:
        # Broad and intentional: this call must never be a gate. Mirrors
        # the Gap-7 multi-model warning's "observability hook, never a
        # gate" except-Exception pattern elsewhere in dispatch_story.
        return None
    text = (text or "").strip()
    return text or None


# The same "too high-level for a jr model" problem that motivates the
# initial-dispatch checklist applies to rework: a reviewer's prose feedback
# (diagnosis + implicit fix reasoning) is itself a coarse brief. Translating
# it into an explicit fix-checklist before handing it to the weak executor
# is the same tech-lead-decomposition logic applied one step later in the
# story's lifecycle.
_REWORK_PLANNER_SYSTEM = (
    "You are a tech lead helping a junior engineer act on code review "
    "feedback; they may not reason precisely through subtle edge cases "
    "unassisted. Read the review feedback below and produce an ordered "
    "checklist of concrete fix steps: what is wrong, which file/lines are "
    "implicated, and how to verify the fix (e.g. a test to add or run). If "
    "the bug involves timing, state mutation, or another subtle edge case, "
    "include a concrete worked example with actual numbers showing the "
    "correct result, and name the specific mistake that produced the wrong "
    "one. If the edge case touches state that persists across calls (a "
    "clock, counter, or high-water mark), the worked example must not stop "
    "at that one call's return value - trace at least one follow-up call "
    "afterward and confirm the state left behind still produces the "
    "correct result for it; getting the immediate return value right is "
    "not sufficient if it leaves the object in a bad state for what comes "
    "next. Preserve test-driven-development ordering where it applies "
    "(reproduce the bug with a failing test before fixing it). Do not "
    "invent issues beyond what the feedback describes. "
    "CRITICAL STEERING for the junior engineer: fixes go in the "
    "implementation file named by the task; NEVER edit, rename, weaken, or "
    "delete the test files (anything matching test_*.py) to make a test "
    "pass - if a test fails, the bug is in the implementation, so fix it "
    "there. "
    "EDITING MECHANICS: this engineer reliably fails at surgical str_replace "
    "edits (cannot construct a unique matching old_str). Direct them to "
    "rewrite the WHOLE implementation file via create_file with every method "
    "fully fixed in one shot, not a sequence of str_replace patches. However, if the task is a small, targeted edit where most of the file's existing content must be preserved verbatim (e.g., adding a paragraph), direct the executor to use str_replace with an exact, verbatim anchor string copied from the current file. This avoids accidentally dropping unrelated sections when reconstructing via create_file. "
    "A THIRD case neither of the above fits: a targeted fix to ONE function that is itself large (roughly 50+ lines) inside a bigger file - e.g. wrapping its whole existing body in a new guard/lock/try-block. Do NOT direct an in-place re-indent of that function; re-indenting many lines through a truncated file-viewing tool is exactly where this engineer fails worst. Instead direct the rename-and-delegate shape: rename the function (e.g. `foo` to `_foo_impl`), then define a new, short `foo` that does the guard/setup and calls `_foo_impl(...)` - the function body itself is never re-indented. If an example of this exact pattern already exists elsewhere in the same file, name it in the checklist as the one to copy. The new short `foo` MUST carry over everything that was attached to the ORIGINAL function, not just the call: any decorator(s) (e.g. `@mcp.tool()`) move to the new `foo`, NOT to `_foo_impl` - a decorator binds to whichever function object owns the name at decoration time, so leaving it on the renamed `_foo_impl` (or dropping it) silently deregisters the real entrypoint even though a test that calls the bare module attribute still passes. The original function's docstring also moves to the new `foo`, not `_foo_impl` - external callers and any tool description read the outer function's docstring. And any argument validation that ran as the ORIGINAL function's first statement(s) must run in the new `foo` BEFORE the guard/lock setup, not only inside `_foo_impl` and not after the lock is acquired - validating late lets an invalid argument reach the lock/guard code first. "
    "Make the checklist's FIRST line the steering rule above, then the "
    "numbered fix steps. Output ONLY the checklist - the steering line, "
    "then the numbered steps - no other preamble, no closing remarks."
)


def _run_rework_planner(
    review_feedback: str, *, dispatch_backend: str, local_model: str,
    plan_role_config: dict | None = None,
) -> str | None:
    """Like _run_planner, but translates code-review feedback into an
    ordered fix-checklist instead of translating a coarse task into an
    implementation checklist. Same bounded single-call contract, same
    backend resolution, same fail-open-to-None contract - see _run_planner's docstring for the shared rationale.
    """
    try:
        backend_name, model = _resolve_planner_backend(
            dispatch_backend, local_model, plan_role_config=plan_role_config,
        )
        text = backend.get_backend("planner", name=backend_name).complete(
            review_feedback, system=_REWORK_PLANNER_SYSTEM, model=model,
        )
    except Exception:
        return None
    text = (text or "").strip()
    return text or None


# ---------- TDD-split test-author role (TDD_SPLIT_PRODUCTION_PLAN.md) ----------
_NEVER_TOUCH_TESTS_STEERING = (
    "fixes go in the implementation file named by the task; NEVER edit, "
    "rename, weaken, or delete the test files (anything matching "
    "test_*.py) to make a test pass - if a test fails, the bug is in the "
    "implementation, so fix it there."
)


def _resolve_test_author_backend(
    dispatch_backend: str, local_model: str,
    plan_role_config: dict | None = None,
) -> tuple[str | None, str | None]:
    """Resolve (provider, model) for the "test_author" role - the tech lead
    that writes the test suite before the (usually weaker) executor
    implements against it. See TDD_SPLIT_PRODUCTION_PLAN.md §2.2.

    Unlike _resolve_planner_backend, an unconfigured test_author role must
    NOT fall back to mirroring dispatch_backend/local_model: the isolated
    experiment this plan is based on (memory/project_tdd_split_experiment_
    result.md) found a same-model split (gpt-oss authoring its own tests)
    actively HARMS the implementer (read-loop park, no impl ever written)
    versus not splitting at all. So both "unconfigured" and "resolves to
    the same backend+model as dispatch" return (None, None) - callers MUST
    treat that as "skip the split, dispatch monolithically", exactly as if
    the split were not configured for this story.
    """
    plan_cfg = (plan_role_config or {}).get("test_author", {})
    registry = role_registry.load_registry()
    provider_override = (
        plan_cfg.get("provider")
        or os.environ.get("PIPELINE_BACKEND_TEST_AUTHOR")
        or registry.get("roles", {}).get("test_author", {}).get("provider")
    )
    if not provider_override:
        return None, None
    try:
        resolution = role_registry.resolve_role(
            "test_author", plan_role_config=plan_role_config, registry=registry,
            model_fallback=lambda: None,
        )
    except role_registry.RoleRegistryError as e:
        # Fail open: a misconfigured test_author role (typo'd model name,
        # provider/model mismatch) must not crash dispatch_story - it
        # degrades to no split, same as unconfigured (§2.5).
        logging.getLogger("pipeline").warning(
            f"test_author role misconfigured, skipping split: {e}"
        )
        return None, None
    if (resolution.provider, resolution.model) == (dispatch_backend, local_model):
        # Belt-and-suspenders (§2.2): compare RESOLVED values, not just the
        # config source, so an operator pointing PIPELINE_BACKEND_TEST_AUTHOR
        # at the same concrete model dispatch already uses (e.g. same Ollama
        # endpoint/tag via a different env var) still refuses, rather than
        # silently reproducing the harmful same-model variant.
        return None, None
    return resolution.provider, resolution.model


_TEST_AUTHOR_SYSTEM = (
    "You are a senior tech lead. Your ONLY job on this dispatch is to write "
    "the test suite for the task below - never the implementation. A "
    "separate, different (and likely less capable) engineer will implement "
    "against your tests in a later dispatch on this same branch, so the "
    "tests must be self-contained and must fail for the right reason "
    "(an import/attribute error because the implementation doesn't exist "
    "yet, not a bug in your own test logic) until that implementation "
    "exists."
)

_TEST_AUTHOR_ALLOWED_TOOLS = "Read,Write,Edit,Bash"


def _test_author_prompt(agent_instructions: str) -> str:
    """Build the test-authoring dispatch's prompt from the story's own
    agent_instructions (generalized from tests/benchmark/tdd_split_
    experiment.py's hardcoded phase1_prompt(), which could name one task's
    spec verbatim; production stories vary, so the scope suffix below is
    task-agnostic)."""
    return (
        f"{agent_instructions}\n\n"
        "--- Test-authoring scope for THIS dispatch ---\n"
        "Write ONLY the test file(s) required to verify the task above - do "
        "NOT create or edit the implementation file(s) it describes; a "
        "separate, later dispatch (a different, weaker engineer) will "
        "implement against your tests next, so they must stand alone and "
        "be runnable against code that doesn't exist yet. Run the test "
        "command to confirm the suite is currently RED (failing because "
        "the implementation is missing) - that is the correct state to "
        "leave it in, not an error to fix. Cover both the happy path and "
        "negative/boundary cases: invalid or malformed inputs, missing "
        "required fields, boundary values (zero, one, min, max, empty "
        "collections), and expected exceptions (assert both type and "
        "message where meaningful). When the test file is written and "
        "confirmed red, commit it (`git add` the test file(s), then `git "
        "commit`) - the next dispatch builds on this same branch and needs "
        "your work committed to see it, exactly like it would need any "
        "other finished step committed. Do not push. Then say you are done "
        "- do not attempt the implementation yourself."
    )


def _wait_for_agent_exit(pid: int, timeout: float, poll_interval: float = 1.0) -> bool:
    """Block the calling thread until the agent process at `pid` exits, or
    `timeout` seconds elapse (whichever first). Returns True iff the
    process exited/was reaped on its own; False if the timeout fired and
    the process had to be SIGTERM'd.

    Used only by the test-author phase (§2.1): unlike the main executor
    dispatch (always async - the MCP tool returns a pid immediately and
    check_story_status polls it later), the test-author phase must finish
    BEFORE the main executor starts, since the executor's prompt and
    worktree depend on what the test-author produced. Mirrors
    tests/benchmark/tdd_split_experiment.py's dispatch() poll loop:
    os.waitpid(pid, os.WNOHANG) is the portable way to reap our own child
    without a completion callback; ChildProcessError means the process is
    already gone (already reaped, or was never a child of this process),
    which counts as "exited".
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if reaped_pid != 0:
            return True
        time.sleep(poll_interval)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    return False


def _run_test_author_phase(
    story: dict, *, story_key: str, worktree_path: Path,
    dispatch_backend: str, local_model: str,
    plan_role_config: dict | None = None,
    timeout: float | None = None,
) -> bool:
    """Run the test-authoring pre-executor dispatch in `worktree_path`,
    BLOCKING until it exits, before the main executor dispatch starts. See
    TDD_SPLIT_PRODUCTION_PLAN.md §2.1/§2.5.

    Returns True iff the test-author produced a real commit on the story's
    branch for the executor to build on. Returns False on ANY failure (role
    unconfigured/refused, dispatch error, timeout, or no new commit) -
    callers MUST treat False as "fall back to today's monolithic dispatch,
    agent_instructions unmodified" per the fail-open contract that is this
    feature's single most safety-critical property. Never raises.
    """
    test_author_backend, test_author_model = _resolve_test_author_backend(
        dispatch_backend, local_model, plan_role_config=plan_role_config,
    )
    if not test_author_backend:
        return False
    log_path = worktree_path / "test_author.log"
    try:
        handle = backend.get_backend("dispatch", name=test_author_backend).dispatch(
            prompt=_test_author_prompt(story.get("agent_instructions", "")),
            system=_TEST_AUTHOR_SYSTEM, model=test_author_model,
            allowed_tools=_TEST_AUTHOR_ALLOWED_TOOLS,
            cwd=worktree_path, log_path=log_path, append=False,
        )
    except Exception:
        logging.getLogger("pipeline").warning(
            f"test-author dispatch failed to start for {story_key}; "
            "falling back to monolithic dispatch"
        )
        return False
    exited = _wait_for_agent_exit(
        handle.pid,
        timeout if timeout is not None else float(
            os.environ.get("PIPELINE_TEST_AUTHOR_TIMEOUT_SECONDS", "5400")
        ),
    )
    if not exited:
        logging.getLogger("pipeline").warning(
            f"test-author dispatch timed out for {story_key}; "
            "falling back to monolithic dispatch"
        )
        return False
    # Lazy import: _default_branch lives in the server module (reads
    # REPO_ROOT, patched by tests via p.REPO_ROOT); the server imports this
    # module at top level, so a module-load import would cycle.
    from .server import _default_branch
    try:
        return _worktree_has_new_commits(worktree_path, story_key, _default_branch())
    except Exception as exc:
        logging.getLogger("pipeline").warning(
            f"test-author dispatch failed to detect commits for {story_key}: {exc}"
        )
        return False

# ---------- Decompose (provider-configurable product-analyst) ----------
def _run_decompose(request: str, *, plan_role_config: dict | None = None) -> str | None:
    """Call a bounded, single-turn LLM (the product-analyst persona) to turn
    a raw goal/feature request into epics/stories JSON matching save_plan's
    schema.

    Structurally identical to _run_planner/_invoke_overlord: one complete()
    call, never an agent loop. Provider/model fall through role_registry
    (PIPELINE_BACKEND_DECOMPOSE / a plan's role_config / model_registry
    .json's "decompose" entry), falling back to Claude at the persona's
    declared tier when none of those apply. Fails open (returns None) on
    any exception - a broken/slow/rate-limited decompose call must never
    raise past this function, mirroring _run_planner's contract.
    """
    resolution = role_registry.resolve_role(
        "decompose", plan_role_config=plan_role_config,
        model_fallback=lambda: _persona_default_model("product-analyst") or "opus",
    )
    try:
        text = backend.get_backend("decompose", name=resolution.provider).complete(
            request, system=_persona_body("product-analyst"), model=resolution.model,
            allowed_tools="Read",
        )
    except Exception:
        return None
    text = (text or "").strip()
    return text or None


__all__ = [
    "_PLANNER_SYSTEM",
    "_PLANNER_SCRATCHPAD_CLAUSE",
    "_TEST_AUTHOR_ALREADY_RAN_CLAUSE",
    "_planner_system",
    "_resolve_planner_backend",
    "_run_planner",
    "_REWORK_PLANNER_SYSTEM",
    "_run_rework_planner",
    "_NEVER_TOUCH_TESTS_STEERING",
    "_resolve_test_author_backend",
    "_TEST_AUTHOR_SYSTEM",
    "_TEST_AUTHOR_ALLOWED_TOOLS",
    "_test_author_prompt",
    "_wait_for_agent_exit",
    "_run_test_author_phase",
    "_run_decompose",
]