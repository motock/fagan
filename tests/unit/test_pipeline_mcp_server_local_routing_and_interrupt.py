"""Tests for the pipeline MCP server: the interrupt path, harness-owned acceptance oracle, role_config propagation regression, and the start of guided decomposition / decompose-role tests.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess
import time

from app import (
    backend,
    role_registry,
)
from pipeline import server as p
from pipeline import test_author as ptest_author
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _already_reaped_pid,
    _clear_caches,
    _FakePlannerBackend,
    _FakeTestAuthorBackend,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _make_worktree_repo,
    _plane_configured,
    agents_dir,
)

# ---------- Guided decomposition (GUIDED_DECOMPOSITION_PLAN.md) ----------
# A "tech lead" planner call that turns a coarse story into an ordered
# sub-step checklist for the weak local executor to follow within a single
# worktree/transcript. _run_planner() is a bounded, single complete() call
# (never an agent loop); dispatch_story() gates it behind PIPELINE_DECOMPOSE
# (default "off") and only for local-family backends.



def test_run_planner_routes_to_claude_backend_and_passes_system_prompt(
    agents_dir, monkeypatch,
):
    """_run_planner routes to the provider _resolve_planner_backend picks
    (here claude, via PIPELINE_BACKEND_PLANNER) and passes the agent
    instructions as the prompt with _PLANNER_SYSTEM as the system prompt.
    The model is registry/env-driven (covered in test_always_on_planner.py);
    this test pins the call shape, not the model tag."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    fake = _FakePlannerBackend(response="1. Write a failing test\n2. Implement it")
    calls = []

    def _fake_get_backend(role, *, name=None):
        calls.append({"role": role, "name": name})
        return fake

    monkeypatch.setattr(backend, "get_backend", _fake_get_backend)

    result = p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert result == "1. Write a failing test\n2. Implement it"
    assert calls == [{"role": "planner", "name": "claude"}]
    assert fake.calls[0]["prompt"] == "Add a rate limiter."
    assert fake.calls[0]["system"] == p._PLANNER_SYSTEM


def test_run_planner_returns_none_on_backend_failure(agents_dir, monkeypatch):
    """A broken/unreachable planner backend must fail open, not raise -
    dispatch_story must be able to proceed with no plan."""
    fake = _FakePlannerBackend(raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    result = p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert result is None


def test_run_planner_returns_none_on_empty_response(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(response="   \n  ")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    result = p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert result is None


def test_planner_system_steers_away_from_editing_test_files():
    """The tech-lead checklist must steer the weak executor to put all
    implementation work in the implementation file and never edit the test
    files. Without this, a weak model that lands an incomplete stub spends
    its whole budget editing the test file instead of completing the
    implementation (observed live: lru_cache t4, temp 1.0, 56 consecutive
    str_replace calls on test_lru_cache.py, never implementing get/put/size).
    The steering rule reaches the agent verbatim because _PLANNER_SYSTEM
    instructs the planner to make it the checklist's first line."""
    assert "implementation file" in p._PLANNER_SYSTEM
    assert "test_" in p._PLANNER_SYSTEM
    # The rule must forbid editing tests AND direct fixes to the impl, or a
    # weak model will keep mutating tests to make them pass.
    assert "NEVER" in p._PLANNER_SYSTEM or "never" in p._PLANNER_SYSTEM.lower()
    # The weak model reliably fails surgical str_replace (observed live in
    # t2/t5: every str_replace in a stubs-then-edit loop is rejected as
    # 'old_str occurs N times' / 'not found'). The checklist must direct the
    # executor to write COMPLETE files via create_file instead of
    # stubs-then-surgically-edit, or the model never gets past stubs.
    assert "create_file" in p._PLANNER_SYSTEM
    assert "NotImplementedError" in p._PLANNER_SYSTEM

def test_planner_system_exception_for_small_edits():
    """The planner should allow str_replace for small targeted edits."""
    assert "str_replace" in p._PLANNER_SYSTEM
    assert "preserve" in p._PLANNER_SYSTEM


def test_planner_system_prescribes_delegate_wrapper_for_large_function_edits():
    """Root cause diagnosed live (2026-07-22, MODE-29-REVIEW-STORY-LOCK-GUARD,
    8 failed dispatch attempts): the story asked the executor to wrap a
    ~300-line existing function's ENTIRE body in a new `with` block - an
    in-place mass re-indent through a truncated view_file/create_file tool,
    the mechanically hardest edit shape for this engine. The identical
    pattern (guard + delegate to a renamed `_foo_impl`) already existed 350
    lines away in the same file for exactly this situation, but nothing
    steered the executor (or the story author) toward it - one attempt tried
    it anyway and botched the split (duplicate defs, orphaned fragments) from
    getting no guidance on the mechanics. Neither existing EDITING MECHANICS
    branch (whole-file create_file rewrite, or str_replace for a small
    preserve-most edit) fits a large-function in-place wrap; the checklist
    must name the rename-and-delegate shape as the correct move for it."""
    assert "_impl" in p._PLANNER_SYSTEM
    assert "delegate" in p._PLANNER_SYSTEM.lower()


def test_planner_system_delegate_wrapper_specifies_what_to_preserve():
    """Root cause diagnosed live (2026-07-22/23, MODE-29-REVIEW-STORY-LOCK-GUARD
    redispatch): the rename-and-delegate recipe told the executor to rename
    `foo` to `_foo_impl` and define a new short `foo` that delegates, but
    never said what the new `foo` must carry over from the original. Every
    Blocking finding across two full review cycles traced to this gap - the
    `@mcp.tool()` decorator was left on the renamed `_foo_impl` (silently
    deregistering the real MCP entrypoint even though tests calling the bare
    module attribute passed), the docstring moved with it (emptying the
    tool's client-facing description), and argument validation ended up
    running inside `_foo_impl` - after the new wrapper's lock/guard setup
    instead of before it, opening a path-traversal window. The recipe must
    name all three explicitly."""
    text = p._PLANNER_SYSTEM.lower()
    assert "decorator" in text
    assert "docstring" in text
    assert "valid" in text and "before" in text


def test_planner_system_worked_examples_must_verify_persisted_state():
    """Live-discovered bug (2026-07-16, production-config benchmark run,
    token_bucket via glm-5.2:cloud/Ollama planner + mlx implementer): the
    planner's own worked example for a backward-clock edge case correctly
    computed the CURRENT call's return value (no refill, return False) but
    then instructed unconditionally overwriting the tracked clock/high-water
    mark with the backward value - which corrupts a LATER call's elapsed-time
    computation (a rate-limit-bypass bug). The implementer followed this
    worked example exactly and failed the hidden acceptance oracle's
    multi-call high-water-mark test as a direct result. _PLANNER_SYSTEM must
    instruct the planner to trace a follow-up call, not just the edge case's
    own immediate return value, whenever the edge case touches state that
    persists across calls."""
    for prompt in (p._PLANNER_SYSTEM, p._REWORK_PLANNER_SYSTEM):
        assert "persist" in prompt.lower()
        assert "follow-up" in prompt.lower() or "subsequent" in prompt.lower()


def test_run_planner_include_scratchpad_augments_system_prompt(agents_dir, monkeypatch):
    """When include_scratchpad=True, the planner's system prompt must direct it
    to weave .agent_scratchpad.md updates into the GENERATED checklist as
    first-class steps - not left to a trailing aside the executor ignores.
    Root cause (GUIDED_DECOMPOSITION_PLAN.md, 2026-07-16): across 22 guided
    runs the scratchpad was consumed only twice (9%) because the checklist the
    model actually follows never mentioned it. The clause must reach the
    backend's system arg."""
    fake = _FakePlannerBackend(response="1. Step one.")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b", include_scratchpad=True,
    )

    system = fake.calls[0]["system"]
    # The base steering must still be present ...
    assert "implementation file" in system
    # ... plus the scratchpad clause, naming the file and asking for it as a
    # per-step checklist item rather than an afterthought.
    assert ".agent_scratchpad.md" in system
    assert system != p._PLANNER_SYSTEM


def test_run_planner_omits_scratchpad_by_default(agents_dir, monkeypatch):
    """include_scratchpad defaults to False (the H3 ablation "off" arm and any
    caller that doesn't opt in): the system prompt must be exactly
    _PLANNER_SYSTEM, unchanged, so the existing by-reference assertions and the
    scratchpad-off behavior both hold."""
    fake = _FakePlannerBackend(response="1. Step one.")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert fake.calls[0]["system"] == p._PLANNER_SYSTEM
    assert ".agent_scratchpad.md" not in fake.calls[0]["system"]


# ---------- planner independently routable (always-on; see test_always_on_planner.py) ----------
def test_resolve_planner_backend_local_mode_honors_env_var_independent_of_dispatch(
    monkeypatch,
):
    """PIPELINE_BACKEND_PLANNER must route the planner to a provider
    independent of whatever dispatch_backend is - this is the gap fix:
    the planner no longer mirrors dispatch_backend."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "mlx")
    backend_name, _model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
    )
    assert backend_name == "mlx"


def test_resolve_planner_backend_local_mode_honors_plan_role_config(monkeypatch):
    """plan_role_config's model value is a friendly registry key (like the
    registry's own roles.* entries), validated/resolved against
    model_registry.json's real "mlx" provider - "qwen" is the repo-root
    registry's declared mlx model."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    backend_name, model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"planner": {"provider": "mlx", "model": "qwen"}},
    )
    assert backend_name == "mlx"
    assert model == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


# ---------- test_author role resolution (TDD_SPLIT_PRODUCTION_PLAN.md §2.2) ----------
def test_resolve_test_author_backend_unconfigured_returns_none_none(monkeypatch):
    """Unlike the planner, an unconfigured test_author role must NOT mirror
    dispatch_backend/local_model - that would reproduce the experiment's
    harmful same-model variant A. (None, None) is the explicit "skip the
    split" signal callers must fail open on. Registry mocked to {} so this
    genuinely tests the unconfigured case regardless of model_registry.json's
    real on-disk contents (which now configures test_author=ollama/glm in
    production, per the validated stronger-author-split experiment)."""
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})
    result = p._resolve_test_author_backend("ollama", "gpt-oss:20b")
    assert result == (None, None)


def test_resolve_test_author_backend_honors_env_var(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    backend_name, model = p._resolve_test_author_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert backend_name == "mlx"
    assert model == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


def test_resolve_test_author_backend_honors_plan_role_config(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    backend_name, model = p._resolve_test_author_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"test_author": {"provider": "mlx", "model": "qwen"}},
    )
    assert backend_name == "mlx"
    assert model == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


def test_resolve_test_author_backend_refuses_same_model_as_dispatch(monkeypatch):
    """Belt-and-suspenders (§2.2): even when a provider IS configured, if it
    resolves to the exact same backend+model dispatch is already using,
    refuse - comparing RESOLVED values catches an operator accidentally
    pointing the test-author at the same concrete model dispatch uses
    (e.g. same Ollama endpoint/tag via a different env var)."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "ollama")
    result = p._resolve_test_author_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"test_author": {"model": "gpt-oss"}},
    )
    assert result == (None, None)


def test_resolve_test_author_backend_fails_open_on_malformed_registry_model(monkeypatch):
    """A typo'd model name for test_author must degrade to "no split", not
    crash dispatch_story - this role is a bonus, never a gate (§2.5)."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    result = p._resolve_test_author_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"test_author": {"model": "no-such-model"}},
    )
    assert result == (None, None)


# ---------- _wait_for_agent_exit (blocking poll used only by the test-author
# phase - unlike the main executor dispatch, this must finish before the
# executor starts, since the executor's prompt/worktree depend on it) ----------
def test_wait_for_agent_exit_returns_true_when_already_reaped():
    """A process that already exited (and was reaped) before the poll loop
    even starts must be treated as "exited", not hang for the full timeout.
    os.waitpid on an already-reaped pid raises ChildProcessError - that's
    the signal, not a bug to guard against."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    result = p._wait_for_agent_exit(proc.pid, timeout=5, poll_interval=0.02)
    assert result is True


def test_wait_for_agent_exit_returns_false_and_kills_on_timeout():
    proc = subprocess.Popen(["sleep", "5"])
    start = time.monotonic()
    result = p._wait_for_agent_exit(proc.pid, timeout=0.3, poll_interval=0.05)
    elapsed = time.monotonic() - start
    try:
        assert result is False
        assert elapsed < 2, "must not block for the full unkilled duration"
    finally:
        try:
            proc.wait(timeout=2)
        except (ChildProcessError, subprocess.TimeoutExpired):
            pass


# ---------- _run_test_author_phase (TDD_SPLIT_PRODUCTION_PLAN.md §2.1/§2.5) ----------
def test_test_author_prompt_instructs_committing_the_test_file():
    """Live validation run (2026-07-18, disposable sandbox repo): Claude
    Sonnet wrote a genuinely correct 35-test suite, confirmed it RED, and
    said 'Done' - but never ran `git commit`, because this prompt never
    told it to. _worktree_has_new_commits then saw zero commits and
    _run_test_author_phase reported failure even though the test-authoring
    itself succeeded, leaving an uncommitted test file in the worktree that
    then confused the executor into a repetition-guard park. The executor's
    own prompt (_build_dispatch_command) already ends with "commit your
    work, push the branch, and exit" - the test-author prompt needs the
    equivalent instruction."""
    prompt = p._test_author_prompt("Build a widget.")
    assert "commit" in prompt.lower()


def test_run_test_author_phase_skips_when_role_unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    monkeypatch.setattr(ptest_author, "_notify_user", lambda *a, **k: None)

    def _boom(*a, **k):
        raise AssertionError("dispatch must not be reached when unconfigured")

    monkeypatch.setattr(backend, "get_backend", _boom)
    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=tmp_path, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
    )
    assert result is False


def test_run_test_author_phase_returns_true_on_successful_commit(monkeypatch, tmp_path):
    """When the resolved role differs from dispatch, the dispatch succeeds,
    and the agent branch has a new commit by the time it exits, the phase
    reports success - the commit is what the executor will build on."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    (wt / "test_foo.py").write_text("def test_x(): assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "tests"], cwd=wt,
                   capture_output=True, text=True, check=True)

    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(ptest_author, "_notify_user", lambda *a, **k: None)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=wt, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is True
    assert fake.calls[0]["cwd"] == wt
    assert "Build it." in fake.calls[0]["prompt"]


def test_run_test_author_phase_returns_false_when_no_new_commit(monkeypatch, tmp_path):
    """The agent exited cleanly but never committed anything (e.g. it wrote
    no test file, or wrote one but didn't commit) - the executor has
    nothing to build on, so this must fail open exactly like a timeout."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")

    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(ptest_author, "_notify_user", lambda *a, **k: None)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=wt, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False


def test_run_test_author_phase_returns_false_when_dispatch_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    fake = _FakeTestAuthorBackend(pid=0, raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(ptest_author, "_notify_user", lambda *a, **k: None)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=tmp_path, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False


def test_run_test_author_phase_returns_false_on_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_wait_for_agent_exit", lambda *a, **k: False)
    monkeypatch.setattr(ptest_author, "_notify_user", lambda *a, **k: None)

    def _boom(*a, **k):
        raise AssertionError("must not check for commits when the dispatch timed out")

    monkeypatch.setattr(p, "_worktree_has_new_commits", _boom)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=tmp_path, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False


def test_run_rework_planner_routes_to_claude_and_passes_feedback_as_prompt(
    agents_dir, monkeypatch,
):
    """The rework planner translates review feedback into a fix checklist -
    same bounded-call/backend-resolution machinery as _run_planner, but a
    distinct system prompt and the review feedback (not agent_instructions)
    as the input."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    fake = _FakePlannerBackend(response="1. Fix the double-count bug\n2. Add a regression test")
    calls = []

    def _fake_get_backend(role, *, name=None):
        calls.append({"role": role, "name": name})
        return fake

    monkeypatch.setattr(backend, "get_backend", _fake_get_backend)

    result = p._run_rework_planner(
        "allow() double-counts refill on every call.",
        dispatch_backend="local", local_model="gpt-oss:20b",
    )

    assert result == "1. Fix the double-count bug\n2. Add a regression test"
    assert calls == [{"role": "planner", "name": "claude"}]
    assert fake.calls[0]["prompt"] == "allow() double-counts refill on every call."
    assert fake.calls[0]["system"] == p._REWORK_PLANNER_SYSTEM
    assert fake.calls[0]["system"] != p._PLANNER_SYSTEM

def test_rework_planner_exception_for_small_edits():
    """The rework planner should allow str_replace for small targeted edits."""
    assert "str_replace" in p._REWORK_PLANNER_SYSTEM
    assert "preserve" in p._REWORK_PLANNER_SYSTEM
    assert p._REWORK_PLANNER_SYSTEM != p._PLANNER_SYSTEM


def test_rework_planner_system_prescribes_delegate_wrapper_for_large_function_edits():
    """Mirrors test_planner_system_prescribes_delegate_wrapper_for_large_function_edits
    - a rework cycle's fix checklist needs the same edit-shape guidance as
    the initial checklist, since a review's requested fix can land inside
    the same kind of large existing function."""
    assert "_impl" in p._REWORK_PLANNER_SYSTEM
    assert "delegate" in p._REWORK_PLANNER_SYSTEM.lower()


def test_rework_planner_system_delegate_wrapper_specifies_what_to_preserve():
    """Mirrors test_planner_system_delegate_wrapper_specifies_what_to_preserve
    - a rework cycle's fix checklist needs the same completed rename-and-
    delegate recipe as the initial checklist, since a reviewer's requested
    fix can land inside the same large-function-wrap shape (this is exactly
    where it recurred live: the story's own rework cycle re-applied the
    same incomplete recipe and reproduced the same two Blocking findings)."""
    text = p._REWORK_PLANNER_SYSTEM.lower()
    assert "decorator" in text
    assert "docstring" in text
    assert "valid" in text and "before" in text


def test_run_rework_planner_returns_none_on_backend_failure(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    result = p._run_rework_planner(
        "bug description", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert result is None


# ---------- decompose role (provider-configurable product-analyst) ----------
def test_extract_json_block_strips_json_fence():
    text = '```json\n{"epics": []}\n```'
    assert p._extract_json_block(text) == '{"epics": []}'


def test_extract_json_block_strips_bare_fence_without_json_tag():
    text = '```\n{"epics": []}\n```'
    assert p._extract_json_block(text) == '{"epics": []}'


def test_extract_json_block_returns_text_unchanged_when_no_fence():
    text = '{"epics": []}'
    assert p._extract_json_block(text) == '{"epics": []}'


def test_run_decompose_calls_registry_resolved_backend_with_product_analyst_persona(
    agents_dir, monkeypatch,
):
    """_run_decompose routes through role_registry.resolve_role("decompose"),
    so it resolves to whatever roles.decompose says in the registry - stubbed
    here (a synthetic "acme"/"widget" entry) so the test doesn't depend on
    which provider/model model_registry.json's decompose entry currently
    configures - and seeds the product-analyst persona body."""
    fake_registry = {
        "providers": {"acme": {"models": {"widget": {"tag": "widget-v1"}}}},
        "roles": {"decompose": {"provider": "acme", "model": "widget"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: fake_registry)
    fake = _FakePlannerBackend(response='{"epics": []}')
    calls = []

    def _fake_get_backend(role, *, name=None):
        calls.append({"role": role, "name": name})
        return fake

    monkeypatch.setattr(backend, "get_backend", _fake_get_backend)

    result = p._run_decompose("Build a CLI todo app.")

    assert result == '{"epics": []}'
    assert calls == [{"role": "decompose", "name": "acme"}]
    assert fake.calls[0]["prompt"] == "Build a CLI todo app."
    assert "Analyst body." in fake.calls[0]["system"]
    # registry roles.decompose.model is "widget" (resolved to its tag).
    assert fake.calls[0]["model"] == "widget-v1"


def test_run_decompose_routes_to_registry_configured_provider(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(response='{"epics": []}')
    registry = {
        "providers": {"ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}}},
        "roles": {"decompose": {"provider": "ollama", "model": "gpt-oss"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    calls = []

    def _fake_get_backend(role, *, name=None):
        calls.append({"role": role, "name": name})
        return fake

    monkeypatch.setattr(backend, "get_backend", _fake_get_backend)

    p._run_decompose("Build a CLI todo app.")

    assert calls == [{"role": "decompose", "name": "ollama"}]
    assert fake.calls[0]["model"] == "gpt-oss:20b"


def test_run_decompose_appends_claude_tier_guidance_by_default(agents_dir, monkeypatch):
    """No dispatch override configured -> resolves to the "claude" tier and
    that guidance (not the local/cloud-oss variants) is appended after the
    persona body."""
    fake = _FakePlannerBackend(response='{"epics": []}')
    registry = {"providers": {}, "roles": {}}
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_decompose("Build a CLI todo app.")

    system = fake.calls[0]["system"]
    assert "Analyst body." in system
    assert "Target implementer: Claude-class" in system
    assert "local ~20B-class" not in system


def test_run_decompose_appends_local_tier_guidance_for_local_dispatch(
    agents_dir, monkeypatch,
):
    """A dispatch role resolved to a non-claude provider with a plain
    (non-":cloud") model tag is treated as a local ~20B-class implementer,
    and the corresponding splitting guidance is appended."""
    fake = _FakePlannerBackend(response='{"epics": []}')
    registry = {
        "providers": {"ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}}},
        "roles": {"dispatch": {"provider": "ollama", "model": "gpt-oss"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_decompose("Build a CLI todo app.")

    assert "Target implementer: local ~20B-class" in fake.calls[0]["system"]


def test_run_decompose_appends_cloud_oss_tier_guidance_for_cloud_tagged_dispatch(
    agents_dir, monkeypatch,
):
    """A dispatch role resolved to a ":cloud"-suffixed tag (e.g. glm served
    through ollama) is a cloud open-source implementer, not local - provider
    name alone can't tell these apart."""
    fake = _FakePlannerBackend(response='{"epics": []}')
    registry = {
        "providers": {"ollama": {"models": {"glm": {"tag": "glm-5.2:cloud"}}}},
        "roles": {"dispatch": {"provider": "ollama", "model": "glm"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_decompose("Build a CLI todo app.")

    system = fake.calls[0]["system"]
    assert "Target implementer: cloud open-source" in system
    assert "local ~20B-class" not in system


def test_dispatch_strength_tier_fails_open_to_claude_on_registry_error(monkeypatch):
    def _raise(*a, **k):
        raise role_registry.RoleRegistryError("bad registry")

    monkeypatch.setattr(role_registry, "resolve_role", _raise)

    assert p._dispatch_strength_tier() == "claude"


def test_run_decompose_returns_none_on_backend_failure(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    assert p._run_decompose("Build a CLI todo app.") is None


def test_run_decompose_returns_none_on_empty_response(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(response="   \n  ")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    assert p._run_decompose("Build a CLI todo app.") is None


def test_decompose_plan_happy_path_parses_fenced_json(agents_dir, monkeypatch):
    plan_json = json.dumps({"epics": [{"summary": "E1", "stories": []}]})
    monkeypatch.setattr(p, "_run_decompose_detailed", lambda request, **k: (f"```json\n{plan_json}\n```", None))

    result = p.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is True
    assert result["plan"]["epics"][0]["summary"] == "E1"


