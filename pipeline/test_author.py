"""TDD-split test-author role (TDD_SPLIT_PRODUCTION_PLAN.md).

A separate tech-lead dispatch writes the test suite before the (usually
weaker) main executor implements against it, for both the initial dispatch
(_run_test_author_phase) and a rework cycle whose review feedback calls for
a new regression test (_run_rework_test_author_phase). Split out of
pipeline/planner.py (which retains the guided-decomposition planner and
product-analyst decompose helpers) purely to keep both files under the
project's line-count target; behavior is unchanged.

Re-exported by pipeline/planner.py so pipeline.server's existing
`from .planner import (...)` import (and every `monkeypatch.setattr(p, ...)`
/ `monkeypatch.setattr(pplanner, ...)` in the test suite) continues to
resolve unchanged. No server-global free-var reads except this module's own
lazy import of _default_branch (circular-avoidance), mirroring the pattern
already used elsewhere in the pipeline package.
"""

import logging
import os
import signal
import time
from pathlib import Path

from app import backend, role_registry

from .git_ops import _worktree_has_non_wip_commits
from .persistence import _notify_user

# ---------- TDD-split test-author role (TDD_SPLIT_PRODUCTION_PLAN.md) ----------
_NEVER_TOUCH_TESTS_STEERING = (
    "fixes go in the implementation file named by the task; NEVER edit, "
    "rename, weaken, or delete the test files (anything matching "
    "test_*.py) to make a test pass - if a test fails, the bug is in the "
    "implementation, so fix it there."
)


def _resolve_test_author_backend(
    dispatch_backend: str,
    local_model: str,
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
            "test_author",
            plan_role_config=plan_role_config,
            registry=registry,
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


def _scaffolding_provider_mismatch_warning(
    *,
    dispatch_backend: str,
    role_config: dict | None,
    registry_roles: dict | None,
) -> str | None:
    """Non-blocking heuristic: warn when a plan dispatches to a local model
    but its "test_author"/"planner" scaffolding roles resolve to a DIFFERENT
    provider.

    Why this exists: exactly that configuration silently removed the
    TDD-split and tech-lead-checklist crutches from two stories on
    2026-07-30 (both plans overrode only "review", leaving test_author/
    planner on the registry default), and both then parked. The scaffolding
    exists for the weak local executor - see _resolve_test_author_backend -
    so a Claude executor needs no warning here.

    Returns None when `dispatch_backend` is "claude", or when neither role
    resolves to a provider that differs from it. Purely advisory - never
    blocks ingest.
    """
    if dispatch_backend == "claude":
        return None
    role_config = role_config or {}
    registry_roles = registry_roles or {}
    mismatched = []
    for role in ("test_author", "planner"):
        provider = (
            role_config.get(role, {}).get("provider")
            or registry_roles.get(role, {}).get("provider")
        )
        if provider is not None and provider != dispatch_backend:
            mismatched.append((role, provider))
    if not mismatched:
        return None
    detail = ", ".join(f"{role} resolves to {provider!r}" for role, provider in mismatched)
    return (
        f"local dispatch on {dispatch_backend!r} but {detail}: the test-first "
        f"split will not run on the same family as the executor"
    )


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


# Sentinel a plan author places in a story's agent_instructions to opt a
# behavior-preserving refactor out of the test-author phase. The phase exists
# to write a NEW failing test suite before the weak executor touches code; a
# pure physical move (e.g. W1a's "move a tool body onto PipelineService,
# existing tests already cover behavior") has no new test to write, and
# forcing the phase to invent one produced a redundant, unsatisfiable
# structural-assertion oracle that parked mid-write (Mode 51, live 2026-08-10
# on W1a-10). The opt-out falls open to monolithic dispatch -- the executor
# then runs against the existing suite, the correct grade for a
# behavior-preserving move. The literal bracketed token avoids matching
# free-text phrases like "no new tests" that appear in ordinary briefs.
_TEST_AUTHOR_OPT_OUT_MARKER = "[no-new-tests]"


def _story_opts_out_of_test_author(story: dict) -> bool:
    """True iff the story's agent_instructions explicitly opt out of the
    test-author phase via the ``[no-new-tests]`` sentinel."""
    return _TEST_AUTHOR_OPT_OUT_MARKER in story.get("agent_instructions", "")


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
        "message where meaningful). Then go back through the task above and "
        "make sure EVERY mechanically-checkable requirement it states has an "
        "assertion of its own - not just the headline behavior. Renames, "
        "removals of a now-dead name, and docstring/comment updates all "
        "count: they are trivially assertable (read the file, assert the new "
        "name is present and the old one is gone). Anything you leave "
        "ungraded WILL be skipped - the implementer makes the minimum edit "
        "that turns your tests green and stops, so an ungraded requirement "
        "ships incomplete with nothing to catch it. If a requirement genuinely "
        "cannot be tested, say so explicitly in your final message. EXCEPTION: "
        "if the brief says this story extends a shared artifact that later, "
        "separate stories will ALSO extend (a registry dict, a system-prompt "
        "string, an __all__ list, a dispatch table), grade only what THIS "
        "story adds - assert membership or ordering relative to a fixed "
        "anchor, never the artifact's total/exact contents. An exact-match "
        "assertion on a shared artifact blocks every later sibling story from "
        "extending it without breaking your test, and has driven local "
        "success as low as 33% on one plan versus 75% on a sibling plan that "
        "asserted membership instead. When the "
        "test file is written and "
        "confirmed red, commit it (`git add` the test file(s), then `git "
        "commit`) - the next dispatch builds on this same branch and needs "
        "your work committed to see it, exactly like it would need any "
        "other finished step committed. Do not push. Then say you are done "
        "- do not attempt the implementation yourself."
    )


def _rework_test_author_prompt(
    review_feedback: str,
    fix_checklist: str | None,
    acceptance_paths: list[str] | None = None,
) -> str:
    """Build the rework test-authoring dispatch's prompt: write ONLY the
    new regression test(s) the review feedback demands, never the fix.
    Mirrors _test_author_prompt's initial-dispatch contract one cycle later
    - a rework redispatch whose reviewer feedback (per
    _rework_requires_new_tests) calls for new tests gets the same
    tech-lead-writes-tests-before-the-weaker-executor-fixes split, applied
    one rework cycle later."""
    checklist_block = (
        f"\n\nYour tech lead's fix checklist for context:\n{fix_checklist}"
        if fix_checklist
        else ""
    )
    # Oracle-conflict guard (live failure 2026-07-29,
    # TRANSPORT-ALIAS-DEPRECATION): given only the reviewer's prose, the
    # test-author "fixed" a regression by asserting the OPPOSITE of the
    # read-only acceptance fixture. Both tests then could not pass at once,
    # the rework done-bar became unreachable, and the executor spun to its
    # step cap. The fixtures are the spec; the reviewer's prose is not.
    oracle_block = (
        (
            "\n\n--- The acceptance fixtures are the authoritative spec ---\n"
            "These files are the story's read-only acceptance oracle: "
            + ", ".join(acceptance_paths)
            + ".\nThey define the REQUIRED behavior and you must not edit "
            "them. Read them BEFORE writing anything. Your new test(s) must "
            "not contradict any assertion they make - if the reviewer's "
            "feedback appears to ask for behavior these fixtures forbid, the "
            "fixtures win: write the test to match the fixtures and note the "
            "conflict in your final message instead of encoding the "
            "reviewer's version. A test that cannot pass at the same time as "
            "the oracle makes the story unwinnable."
        )
        if acceptance_paths
        else ""
    )
    return (
        "The code reviewer REQUESTED CHANGES on this branch. Your ONLY "
        "job on this dispatch is to write the NEW regression test(s) "
        "that reproduce the bug(s) described below - never the fix "
        "itself. A separate, later dispatch (a different, weaker "
        f"engineer) will implement the fix against your test(s) next.\n\n"
        f"Review feedback:\n{review_feedback}"
        f"{checklist_block}"
        f"{oracle_block}\n\n"
        "--- Test-authoring scope for THIS dispatch ---\n"
        "Write ONLY the new test(s) needed to reproduce the bug(s) "
        "named above - do NOT edit the implementation file(s). Add "
        "them to the existing test file for this module (or add a new "
        "test_*.py file if none exists yet) alongside the existing "
        "tests - do not remove or modify any existing test. Run the "
        "test command to confirm the new test(s) FAIL for the right "
        "reason (reproducing the exact bug the reviewer described - "
        "e.g. the exact exception type/message), not because of a "
        "syntax or import error. When confirmed red, commit them "
        "(`git add` the test file(s), then `git commit`). Do not push. "
        "Then say you are done - do not attempt the implementation fix "
        "yourself."
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
    story: dict,
    *,
    story_key: str,
    worktree_path: Path,
    dispatch_backend: str,
    local_model: str,
    plan_name: str,
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

    Every False return also calls _notify_user: the fail-open silently
    dropped the weak-executor's TDD-split crutch with no operator-visible
    signal (observed live 2026-07-30 on two stories whose plan left
    test_author on a different provider than dispatch - both then parked).
    The fail-open itself is unchanged; only the silence is fixed.
    """
    # Escape hatch (Mode 51, live 2026-08-10 on W1a-10): a story whose brief
    # carries the [no-new-tests] sentinel is a behavior-preserving refactor
    # with no new test to write. Skip the phase before resolving/dispatching
    # -- forcing it to invent a redundant structural-assertion oracle is what
    # produced the unsatisfiable, parked-mid-write oracle. Fall open to
    # monolithic dispatch against the existing suite (the correct grade).
    if _story_opts_out_of_test_author(story):
        _notify_user(
            plan_name,
            f"{story_key} test-author phase skipped (story opts out via "
            "[no-new-tests]); dispatching monolithically against the "
            "existing test suite",
        )
        return False
    test_author_backend, test_author_model = _resolve_test_author_backend(
        dispatch_backend,
        local_model,
        plan_role_config=plan_role_config,
    )
    if not test_author_backend:
        _notify_user(
            plan_name,
            f"{story_key} test-author phase fell open (role unconfigured or "
            "resolves to the same backend as dispatch); dispatching "
            "monolithically without a test-first split",
        )
        return False
    log_path = worktree_path / "test_author.log"
    try:
        handle = backend.get_backend("dispatch", name=test_author_backend).dispatch(
            prompt=_test_author_prompt(story.get("agent_instructions", "")),
            system=_TEST_AUTHOR_SYSTEM,
            model=test_author_model,
            allowed_tools=_TEST_AUTHOR_ALLOWED_TOOLS,
            cwd=worktree_path,
            log_path=log_path,
            append=False,
        )
    except Exception:  # noqa: BLE001 (already logged below; dispatch/process-launch failures are unpredictable and must not raise past this function)
        logging.getLogger("pipeline").warning(
            f"test-author dispatch failed to start for {story_key}; "
            "falling back to monolithic dispatch"
        )
        _notify_user(
            plan_name,
            f"{story_key} test-author phase fell open (dispatch failed to "
            "start); dispatching monolithically without a test-first split",
        )
        return False
    exited = _wait_for_agent_exit(
        handle.pid,
        timeout
        if timeout is not None
        else float(os.environ.get("PIPELINE_TEST_AUTHOR_TIMEOUT_SECONDS", "5400")),
    )
    if not exited:
        logging.getLogger("pipeline").warning(
            f"test-author dispatch timed out for {story_key}; "
            "falling back to monolithic dispatch"
        )
        _notify_user(
            plan_name,
            f"{story_key} test-author phase fell open (dispatch timed out); "
            "dispatching monolithically without a test-first split",
        )
        return False
    # Lazy import: _default_branch lives in the server module (reads
    # REPO_ROOT, patched by tests via p.REPO_ROOT); the server imports this
    # module at top level, so a module-load import would cycle.
    from .server import _default_branch

    try:
        has_commits = _worktree_has_non_wip_commits(
            worktree_path, story_key, _default_branch())
    except Exception as exc:  # noqa: BLE001 (already logged below; git-detection failures are unpredictable and must not raise past this function)
        logging.getLogger("pipeline").warning(
            f"test-author dispatch failed to detect commits for {story_key}: {exc}"
        )
        _notify_user(
            plan_name,
            f"{story_key} test-author phase fell open (commit detection "
            "failed); dispatching monolithically without a test-first split",
        )
        return False
    if not has_commits:
        _notify_user(
            plan_name,
            f"{story_key} test-author phase fell open (no finished commit "
            "produced - only a WIP park checkpoint or nothing); dispatching "
            "monolithically without a test-first split",
        )
    return has_commits


def _run_rework_test_author_phase(
    story: dict,
    *,
    story_key: str,
    worktree_path: Path,
    dispatch_backend: str,
    local_model: str,
    review_feedback: str,
    fix_checklist: str | None = None,
    plan_role_config: dict | None = None,
    timeout: float | None = None,
) -> bool:
    """Like _run_test_author_phase, but for a REWORK cycle whose review
    feedback (per _rework_requires_new_tests) calls for at least one new
    test case. Dispatches a BLOCKING single test-authoring agent run in
    worktree_path BEFORE the main executor rework redispatch, so the
    executor implements against tests that already exist and already fail
    for the right reason - applying TDD_SPLIT_PRODUCTION_PLAN.md's
    tech-lead/weak-executor split one cycle later. Returns True iff the
    test-author produced a real new commit on the branch; False on ANY
    failure (role unconfigured, dispatch error, timeout, no new commit) -
    callers MUST treat False as "fall back to today's monolithic rework
    dispatch", identical to _run_test_author_phase's fail-open contract.
    Never raises.
    """
    test_author_backend, test_author_model = _resolve_test_author_backend(
        dispatch_backend,
        local_model,
        plan_role_config=plan_role_config,
    )
    if not test_author_backend:
        return False
    log_path = worktree_path / "rework_test_author.log"
    try:
        handle = backend.get_backend("dispatch", name=test_author_backend).dispatch(
            prompt=_rework_test_author_prompt(
                review_feedback,
                fix_checklist,
                acceptance_paths=[
                    entry["path"] for entry in (story.get("acceptance") or [])
                ],
            ),
            system=_TEST_AUTHOR_SYSTEM,
            model=test_author_model,
            allowed_tools=_TEST_AUTHOR_ALLOWED_TOOLS,
            cwd=worktree_path,
            log_path=log_path,
            append=False,
        )
    except Exception:  # noqa: BLE001 (mirrors _run_test_author_phase's contract)
        logging.getLogger("pipeline").warning(
            f"rework test-author dispatch failed to start for {story_key}; "
            "falling back to monolithic rework dispatch"
        )
        return False
    exited = _wait_for_agent_exit(
        handle.pid,
        timeout
        if timeout is not None
        else float(os.environ.get("PIPELINE_TEST_AUTHOR_TIMEOUT_SECONDS", "5400")),
    )
    if not exited:
        logging.getLogger("pipeline").warning(
            f"rework test-author dispatch timed out for {story_key}; "
            "falling back to monolithic rework dispatch"
        )
        return False
    # Lazy import: _default_branch lives in the server module (reads
    # REPO_ROOT, patched by tests via p.REPO_ROOT); the server imports this
    # module at top level, so a module-load import would cycle.
    from .server import _default_branch

    try:
        return _worktree_has_non_wip_commits(worktree_path, story_key, _default_branch())
    except Exception as exc:  # noqa: BLE001 (mirrors _run_test_author_phase's contract)
        logging.getLogger("pipeline").warning(
            f"rework test-author dispatch failed to detect commits for {story_key}: {exc}"
        )
        return False


__all__ = [
    "_NEVER_TOUCH_TESTS_STEERING",
    "_TEST_AUTHOR_ALLOWED_TOOLS",
    "_TEST_AUTHOR_OPT_OUT_MARKER",
    "_TEST_AUTHOR_SYSTEM",
    "_resolve_test_author_backend",
    "_rework_test_author_prompt",
    "_run_rework_test_author_phase",
    "_run_test_author_phase",
    "_scaffolding_provider_mismatch_warning",
    "_story_opts_out_of_test_author",
    "_test_author_prompt",
    "_wait_for_agent_exit",
]
