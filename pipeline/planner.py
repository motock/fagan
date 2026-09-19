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

import os
from pathlib import Path

from app import backend, role_registry

from .config import DEFAULT_MODEL

# _worktree_has_new_commits is no longer called here (both test-author phases
# now use _worktree_has_non_wip_commits), but tests monkeypatch
# pipeline.planner._worktree_has_new_commits as a belt-and-suspenders boom on
# the timeout path (test_rework_test_author_phase.py); keep it importable.
from .git_ops import (  # noqa: F401
    _worktree_has_new_commits,
    _worktree_has_non_wip_commits,
)
from .persona import _persona_body, _persona_default_model

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
    "test written for THIS task fails, the bug is in the implementation "
    "file - fix it there, never change the test. If a PRE-EXISTING test "
    "(one this task did not add) fails and the change the task asks for is "
    "what breaks it, do NOT undo the task's required change to make that "
    "test pass - stop and report the conflict, naming the failing test and "
    "the task requirement it contradicts. "
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

# Spliced into _PLANNER_SYSTEM AFTER _TEST_AUTHOR_ALREADY_RAN_CLAUSE when the
# test-author phase committed specific test file(s) we could detect on the
# branch (git_ops._test_files_added_on_branch + _test_names_in_file). The
# prohibition clause above tells the planner not to emit a write-the-test
# step but gives it no grounding in WHICH file/tests exist - so even a
# strong planner re-derives a plausible-but-wrong "Write the test file" step
# with INVENTED test-case names from the story's own agent_instructions
# (root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2's THIRD reset:
# sonnet named a `test_lint_gate_error_gets_lint_instruction` case that the
# committed file did not contain - it named a different one). This clause
# hands the planner the EXACT file paths and test-case names so it can
# point the executor at READING them instead of re-deriving them.
_AUTHORED_TEST_FILES_CLAUSE_TEMPLATE = (
    " CONCRETE GROUNDING: the test-author phase has committed the "
    "following test file(s) to this branch - they are on disk and "
    "currently failing, and they ARE the spec the executor must "
    "implement to. {file_summary} "
    "Reference these files by their EXACT names above and use the exact "
    "test-case names listed; do NOT invent, rename, or re-derive test "
    "file names or test-case names from the task description. The "
    "checklist's FIRST implementation step must be to READ the file(s) "
    "named above to learn the exact required behavior (the test names "
    "and assertions there are the spec the implementation must satisfy), "
    "then implement until those tests pass. Every step after the read "
    "step must be implementation work that makes those tests pass - "
    "never a step to create, write, or modify a test file."
)


def _format_authored_test_files(
    authored_test_files: list[tuple[str, list[str]]],
) -> str:
    """Render the (file_path, [test_names]) pairs as a human-readable
    summary for the grounding clause, e.g.:
        `test_foo.py` (tests: test_a, test_b); `test_bar.py` (tests: test_c)
    """
    parts = []
    for path, names in authored_test_files:
        if names:
            parts.append(f"`{path}` (tests: {', '.join(names)})")
        else:
            parts.append(f"`{path}` (no test functions detected)")
    return "; ".join(parts) + "."


def _planner_system(
    *,
    include_scratchpad: bool = False,
    tests_already_authored: bool = False,
    authored_test_files: list[tuple[str, list[str]]] | None = None,
) -> str:
    """The planner's system prompt, optionally augmented with the scratchpad
    and/or test-author-already-ran clauses. Base prompt (_PLANNER_SYSTEM) is
    returned unchanged when both are off, preserving the H3 ablation and the
    by-reference tests.

    authored_test_files: the test-author phase's ACTUAL committed (file_path,
    [test_names]) pairs, detected on the branch. Only splices the grounding
    clause when tests_already_authored is True AND the list is non-empty -
    the two go together (grounding only applies to a split story whose
    test-author phase ran and produced detectable tests). An empty/None
    list with tests_already_authored=True falls back to the prohibition-only
    clause so a git-detection failure degrades gracefully."""
    system = _PLANNER_SYSTEM
    if tests_already_authored:
        system += _TEST_AUTHOR_ALREADY_RAN_CLAUSE
        if authored_test_files:
            system += _AUTHORED_TEST_FILES_CLAUSE_TEMPLATE.format(
                file_summary=_format_authored_test_files(authored_test_files),
            )
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
    registry roles.planner.provider -> PIPELINE_BACKEND_PLANNER ->
    default "ollama" (so a stock install resolves to ollama/glm-5.2:cloud
    with no env vars set, via the registry pin from PR #151; the env var is
    only the empty-state fallback, consulted when the registry has no
    roles.planner entry).
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
    include_scratchpad: bool = False,
    plan_role_config: dict | None = None,
    tests_already_authored: bool = False,
    authored_test_files: list[tuple[str, list[str]]] | None = None,
    worktree: str | None = None,
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

    authored_test_files: when tests_already_authored is True, the test-author
    phase's ACTUAL committed (file_path, [test_names]) pairs detected on the
    branch (git_ops._test_files_added_on_branch + _test_names_in_file). Splices
    a grounding clause naming the real file/tests so the planner points the
    executor at READING them instead of re-deriving (and inventing) test names.
    None/empty falls back to the prohibition-only clause; see
    _planner_system.

    Returns the raw checklist text, or None on any failure. Callers MUST
    treat None as "no plan" and fall open to the existing no-plan dispatch
    path - a broken, slow, or rate-limited planner call must never block or
    corrupt a story's dispatch. External boundary: delegates to the
    configured Backend. Tests mock this function.
    """
    try:
        backend_name, model = _resolve_planner_backend(
            dispatch_backend,
            local_model,
            plan_role_config=plan_role_config,
        )
        # Compute cell_dir for cache sidecar recording
        if worktree is not None and Path(worktree).parent.name == "worktrees":
            cell_dir = str(Path(worktree).resolve().parent)
        else:
            cell_dir = None

        text = backend.get_backend("planner", name=backend_name).complete(
            agent_instructions,
            system=_planner_system(
                include_scratchpad=include_scratchpad,
                tests_already_authored=tests_already_authored,
                authored_test_files=authored_test_files,
            ),
            model=model,
            cell_dir=cell_dir,
            role="planner",
        )
    except Exception:  # noqa: BLE001 (broad and intentional: this call must never be a gate, per the comment below)
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
    "delete the test files (anything matching test_*.py) to make a test pass. If a test written for THIS task fails, the bug is in the implementation, so fix it there. If a PRE-EXISTING test (one this task did not add) fails and the change the task asks for is what breaks it, do NOT undo the task's required change to make that test pass - stop and report the conflict, naming the failing test and the task requirement it contradicts. "
    "EDITING MECHANICS: this engineer reliably fails at surgical str_replace "
    "edits (cannot construct a unique matching old_str). Direct them to "
    "rewrite the WHOLE implementation file via create_file with every method "
    "fully fixed in one shot, not a sequence of str_replace patches. However, if the task is a small, targeted edit where most of the file's existing content must be preserved verbatim (e.g., adding a paragraph), direct the executor to use str_replace with an exact, verbatim anchor string copied from the current file. This avoids accidentally dropping unrelated sections when reconstructing via create_file. "
    "A THIRD case neither of the above fits: a targeted fix to ONE function that is itself large (roughly 50+ lines) inside a bigger file - e.g. wrapping its whole existing body in a new guard/lock/try-block. Do NOT direct an in-place re-indent of that function; re-indenting many lines through a truncated file-viewing tool is exactly where this engineer fails worst. Instead direct the rename-and-delegate shape: rename the function (e.g. `foo` to `_foo_impl`), then define a new, short `foo` that does the guard/setup and calls `_foo_impl(...)` - the function body itself is never re-indented. If an example of this exact pattern already exists elsewhere in the same file, name it in the checklist as the one to copy. The new short `foo` MUST carry over everything that was attached to the ORIGINAL function, not just the call: any decorator(s) (e.g. `@mcp.tool()`) move to the new `foo`, NOT to `_foo_impl` - a decorator binds to whichever function object owns the name at decoration time, so leaving it on the renamed `_foo_impl` (or dropping it) silently deregisters the real entrypoint even though a test that calls the bare module attribute still passes. The original function's docstring also moves to the new `foo`, not `_foo_impl` - external callers and any tool description read the outer function's docstring. And any argument validation that ran as the ORIGINAL function's first statement(s) must run in the new `foo` BEFORE the guard/lock setup, not only inside `_foo_impl` and not after the lock is acquired - validating late lets an invalid argument reach the lock/guard code first. "
    "Make the checklist's FIRST line the steering rule above, then the "
    "numbered fix steps. Output ONLY the checklist - the steering line, "
    "then the numbered steps - no other preamble, no closing remarks."
)

# Single-turn classifier: does a rework's review feedback call for at least
# one NEW test case (not just a code change against tests that already
# exist)? Used by _rework_requires_new_tests to decide whether to run the
# rework test-author phase before the main executor redispatch.
_REWORK_NEEDS_NEW_TEST_SYSTEM = (
    "You are assessing code review feedback for a pending rework. Read "
    "the review feedback below and answer ONLY: does properly "
    "addressing it require writing at least one NEW test case that "
    "does not already exist (e.g. a regression test reproducing a "
    "specific bug, an edge case the reviewer named that is not yet "
    "covered)? Answer with exactly one word, YES or NO, with no other "
    "text."
)


def _run_rework_planner(
    review_feedback: str,
    *,
    dispatch_backend: str,
    local_model: str,
    plan_role_config: dict | None = None,
    worktree: str | None = None,
) -> str | None:
    """Like _run_planner, but translates code-review feedback into an
    ordered fix-checklist instead of translating a coarse task into an
    implementation checklist. Same bounded single-call contract, same
    backend resolution, same fail-open-to-None contract - see _run_planner's docstring for the shared rationale.
    """
    try:
        backend_name, model = _resolve_planner_backend(
            dispatch_backend,
            local_model,
            plan_role_config=plan_role_config,
        )
        # Compute cell_dir for cache sidecar recording
        if worktree is not None and Path(worktree).parent.name == "worktrees":
            cell_dir = str(Path(worktree).resolve().parent)
        else:
            cell_dir = None

        text = backend.get_backend("planner", name=backend_name).complete(
            review_feedback,
            system=_REWORK_PLANNER_SYSTEM,
            model=model,
            cell_dir=cell_dir,
            role="rework_planner",
        )
    except Exception:  # noqa: BLE001 (deliberate fail-open-to-None contract, per this function's docstring)
        return None
    text = (text or "").strip()
    return text or None


def _rework_requires_new_tests(
    review_feedback: str,
    *,
    dispatch_backend: str,
    local_model: str,
    plan_role_config: dict | None = None,
) -> bool:
    """Bounded single-turn LLM call: does review_feedback's fix require
    at least one new test case, not just a code change against tests that
    already exist? Same bounded single-call contract as
    _run_planner/_run_rework_planner (one complete() call, never an agent
    loop) - see their docstrings. Fails open to False (skip the rework
    test-author phase entirely, today's unmodified behavior) on ANY
    exception or a non-YES response: a false negative here only costs the
    pre-existing status quo, never a broken build.
    """
    try:
        backend_name, model = _resolve_planner_backend(
            dispatch_backend,
            local_model,
            plan_role_config=plan_role_config,
        )
        text = backend.get_backend("planner", name=backend_name).complete(
            review_feedback,
            system=_REWORK_NEEDS_NEW_TEST_SYSTEM,
            model=model,
        )
    except Exception:  # noqa: BLE001 (fail-open, mirrors _run_rework_planner's contract)
        return False
    return (text or "").strip().upper().startswith("YES")


# TDD-split test-author role (TDD_SPLIT_PRODUCTION_PLAN.md) moved to
# pipeline/test_author.py to keep this file under the line-count target;
# re-exported here so pipeline.server's existing `from .planner import
# (...)` and every monkeypatch.setattr(p/pplanner, "<name>", ...) in the
# test suite continue to resolve unchanged.
from .test_author import (  # noqa: F401
    _NEVER_TOUCH_TESTS_STEERING,
    _TEST_AUTHOR_ALLOWED_TOOLS,
    _TEST_AUTHOR_OPT_OUT_MARKER,
    _TEST_AUTHOR_SYSTEM,
    _resolve_test_author_backend,
    _rework_test_author_prompt,
    _run_rework_test_author_phase,
    _run_test_author_phase,
    _scaffolding_provider_mismatch_warning,
    _story_opts_out_of_test_author,
    _test_author_prompt,
    _wait_for_agent_exit,
)

# ---------- Decompose (provider-configurable product-analyst) ----------

# Guidance appended to the product-analyst's system prompt so it can
# calibrate story splitting/detail to the implementer that will actually run
# the stories. _run_decompose cannot ask (single complete() call, no user
# turn) so this is resolved from config rather than requested interactively.
_STRENGTH_TIER_GUIDANCE = {
    "claude": (
        "\n\nTarget implementer: Claude-class. Keep stories well under ~400 "
        "changed lines and split on judgment; no special local-dispatch "
        "constraints apply."
    ),
    "cloud-oss": (
        "\n\nTarget implementer: cloud open-source model (e.g. glm). Same "
        "sizing as Claude-class, but every mechanically-checkable "
        "requirement in agent_instructions must be something a test-author "
        "can actually assert - an ungraded requirement gets silently "
        "dropped."
    ),
    "local": (
        "\n\nTarget implementer: local ~20B-class model (e.g. gpt-oss, "
        "devstral). Cap each story at two production files (test files "
        "don't count); split by file/concern rather than bundling. Prefer "
        "rename-and-delegate over prescribing an in-place re-indent of a "
        "large existing function, and anchored str_replace-style edits over "
        "line-number edits on files over ~1,000 lines. One concern per "
        "story."
    ),
}


def _dispatch_strength_tier(plan_role_config: dict | None = None) -> str:
    """Classify the resolved "dispatch" role into a strength tier: "claude",
    "cloud-oss", or "local". Provider name alone can't tell cloud from local -
    glm dispatches *through* the ollama provider but is a cloud-served model
    - so this keys off the ":cloud" tag suffix, the same convention backend.py
    already uses to recognize cloud-served tags. Fails open to "claude" (the
    same default resolve_role itself falls back to) on a misconfigured
    registry, mirroring _run_decompose's own fail-open contract.
    """
    try:
        resolution = role_registry.resolve_role(
            "dispatch",
            plan_role_config=plan_role_config,
            model_fallback=lambda: DEFAULT_MODEL,
        )
    except role_registry.RoleRegistryError:
        return "claude"
    if resolution.provider == "claude":
        return "claude"
    if resolution.model.endswith(":cloud"):
        return "cloud-oss"
    return "local"


def _run_decompose_detailed(
    request: str, *, plan_role_config: dict | None = None
) -> tuple[str | None, str | None]:
    """Decompose with the cause preserved: returns (text, error).

    Mirrors _run_decompose's resolution and fail-open behavior, but instead
    of silently discarding the backend's exception, returns it as a
    diagnostic string so callers (the /api/decompose route, the chat
    dashboard) can tell a claude-CLI auth/cap failure apart from a
    proxy model-not-found 404 or a slow-but-working call. (text, None) on
    success; (None, "<ExcType>: <detail>") on any backend failure.
    """
    resolution = role_registry.resolve_role(
        "decompose",
        plan_role_config=plan_role_config,
        model_fallback=lambda: _persona_default_model("product-analyst") or "opus",
    )
    tier = _dispatch_strength_tier(plan_role_config=plan_role_config)
    system = _persona_body("product-analyst") + _STRENGTH_TIER_GUIDANCE[tier]
    try:
        text = backend.get_backend("decompose", name=resolution.provider).complete(
            request,
            system=system,
            model=resolution.model,
            allowed_tools="Read",
        )
    except Exception as exc:  # noqa: BLE001 (deliberate fail-open, per contract above)
        return None, f"{type(exc).__name__}: {exc}"
    text = (text or "").strip()
    return (text or None), None


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

    The system prompt is the product-analyst persona plus strength-tier
    guidance resolved from the "dispatch" role (see _dispatch_strength_tier),
    so the story splitting/detail matches the implementer these stories will
    actually run on.

    Callers that need the underlying failure cause (for surfacing to a user)
    use _run_decompose_detailed instead.
    """
    return _run_decompose_detailed(request, plan_role_config=plan_role_config)[0]


__all__ = [
    "_AUTHORED_TEST_FILES_CLAUSE_TEMPLATE",
    "_NEVER_TOUCH_TESTS_STEERING",
    "_PLANNER_SCRATCHPAD_CLAUSE",
    "_PLANNER_SYSTEM",
    "_REWORK_NEEDS_NEW_TEST_SYSTEM",
    "_REWORK_PLANNER_SYSTEM",
    "_STRENGTH_TIER_GUIDANCE",
    "_TEST_AUTHOR_ALLOWED_TOOLS",
    "_TEST_AUTHOR_ALREADY_RAN_CLAUSE",
    "_TEST_AUTHOR_SYSTEM",
    "_dispatch_strength_tier",
    "_format_authored_test_files",
    "_planner_system",
    "_resolve_planner_backend",
    "_resolve_test_author_backend",
    "_rework_requires_new_tests",
    "_rework_test_author_prompt",
    "_run_decompose",
    "_run_decompose_detailed",
    "_run_planner",
    "_run_rework_planner",
    "_run_rework_test_author_phase",
    "_run_test_author_phase",
    "_test_author_prompt",
    "_wait_for_agent_exit",
]
