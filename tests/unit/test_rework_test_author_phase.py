"""Unit tests for the rework-cycle test-author phase helpers
(pipeline/planner.py): _rework_requires_new_tests and
_run_rework_test_author_phase.

Mirrors the existing _run_test_author_phase unit tests
(test_pipeline_mcp_server.py, "---------- _run_test_author_phase" section)
for mocking backend.get_backend / _wait_for_agent_exit / git detection,
and the existing _run_rework_planner tests for the bounded single-call
helpers. The integration proof (these helpers wired into the REAL
dispatch_story rework path) lives in
test_rework_test_author_wiring_acceptance.py.
"""

import subprocess

import pytest
from test_pipeline_mcp_server import (
    _already_reaped_pid,
    _FakePlannerBackend,
    _FakeTestAuthorBackend,
    _make_worktree_repo,
)

from app import backend
from pipeline import planner as ppl
from pipeline import server as p


# `agents_dir` is defined locally (mirroring test_pipeline_mcp_server.py /
# test_tdd_split_always_on.py) rather than imported, so the fixture name
# and the test-function parameter don't shadow each other (ruff F811).
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    from pipeline import persona as pper

    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


# ---------- _rework_requires_new_tests ----------


def test_rework_requires_new_tests_yes_response_returns_true(agents_dir, monkeypatch):
    """A YES verdict from the classifier means the rework's review feedback
    calls for at least one new test case - the wiring must run the rework
    test-author phase."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    fake = _FakePlannerBackend(response="YES")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    result = p._rework_requires_new_tests(
        "Add a regression test reproducing the off-by-one.",
        dispatch_backend="local",
        local_model="gpt-oss:20b",
    )
    assert result is True
    assert (
        fake.calls[0]["prompt"] == "Add a regression test reproducing the off-by-one."
    )
    assert fake.calls[0]["system"] == p._REWORK_NEEDS_NEW_TEST_SYSTEM


def test_rework_requires_new_tests_yes_with_surrounding_text_returns_true(
    agents_dir, monkeypatch
):
    """The classifier is prompted to answer one word, but be lenient: any
    response whose first token is YES (case-insensitive) counts, so a
    chatty model still signals correctly."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    monkeypatch.setattr(
        backend,
        "get_backend",
        lambda role, *, name=None: _FakePlannerBackend(
            response="  yes, a new test is needed  "
        ),
    )
    result = p._rework_requires_new_tests(
        "needs a new test",
        dispatch_backend="local",
        local_model="gpt-oss:20b",
    )
    assert result is True


def test_rework_requires_new_tests_no_response_returns_false(agents_dir, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    monkeypatch.setattr(
        backend,
        "get_backend",
        lambda role, *, name=None: _FakePlannerBackend(response="NO"),
    )
    result = p._rework_requires_new_tests(
        "rename the variable",
        dispatch_backend="local",
        local_model="gpt-oss:20b",
    )
    assert result is False


def test_rework_requires_new_tests_garbled_response_returns_false(
    agents_dir, monkeypatch
):
    """A non-YES/non-NO answer (a confused or refusal response) must fail
    open to False - never run the phase on an ambiguous signal."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    monkeypatch.setattr(
        backend,
        "get_backend",
        lambda role, *, name=None: _FakePlannerBackend(response="maybe?"),
    )
    result = p._rework_requires_new_tests(
        "unclear",
        dispatch_backend="local",
        local_model="gpt-oss:20b",
    )
    assert result is False


def test_rework_requires_new_tests_empty_response_returns_false(
    agents_dir, monkeypatch
):
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    monkeypatch.setattr(
        backend,
        "get_backend",
        lambda role, *, name=None: _FakePlannerBackend(response="   "),
    )
    result = p._rework_requires_new_tests(
        "anything",
        dispatch_backend="local",
        local_model="gpt-oss:20b",
    )
    assert result is False


def test_rework_requires_new_tests_exception_fails_open_false(agents_dir, monkeypatch):
    """A broken/unreachable classifier backend must fail open to False,
    never raise past this function - a false negative only costs the
    pre-existing monolithic-rework status quo."""
    monkeypatch.setattr(
        backend,
        "get_backend",
        lambda role, *, name=None: _FakePlannerBackend(raises=RuntimeError("down")),
    )
    result = p._rework_requires_new_tests(
        "needs a test",
        dispatch_backend="local",
        local_model="gpt-oss:20b",
    )
    assert result is False


# ---------- _rework_test_author_prompt ----------


def test_rework_test_author_prompt_embeds_review_feedback_and_forbids_fix():
    """The rework test-author prompt must hand the agent the review feedback
    to write tests against and explicitly forbid writing the fix (the fix is
    a separate, later dispatch by the weaker executor)."""
    prompt = p._rework_test_author_prompt(
        "Add a regression test for the null-ts crash.",
        fix_checklist=None,
    )
    assert "Add a regression test for the null-ts crash." in prompt
    assert "do NOT edit the implementation" in prompt
    # The committed-tests contract (mirrors _test_author_prompt's live fix).
    assert "commit" in prompt.lower()


def test_rework_test_author_prompt_includes_checklist_when_provided():
    prompt = p._rework_test_author_prompt(
        "feedback",
        fix_checklist="1. Fix X\n2. Add test Y",
    )
    assert "1. Fix X" in prompt
    assert "fix checklist" in prompt.lower()


def test_rework_test_author_prompt_omits_checklist_block_when_none():
    prompt = p._rework_test_author_prompt("feedback", fix_checklist=None)
    assert "fix checklist" not in prompt.lower()


# Oracle-conflict gap (found live 2026-07-29 on TRANSPORT-ALIAS-DEPRECATION):
# the rework test-author was handed ONLY the reviewer's prose. Acting on a
# finding that said "the rename broke a test that sets LOCAL_AGENT_NUM_CTX",
# it authored a test asserting the legacy var must still be HONORED - the
# exact opposite of the read-only acceptance fixture's "old var is inert"
# assertion. The two could not both pass, so the rework done-bar became
# unreachable and the executor looped to its step cap. The prompt must name
# the acceptance fixtures as authoritative and read-only.


def test_rework_test_author_prompt_names_acceptance_paths_as_authoritative():
    """When the story carries acceptance fixtures, the rework test-author must
    be told which files they are and that they are the authoritative spec -
    a new test may never contradict them."""
    prompt = p._rework_test_author_prompt(
        "The rename broke the NUM_CTX override.",
        fix_checklist=None,
        acceptance_paths=["test_acceptance_transport_alias.py"],
    )
    assert "test_acceptance_transport_alias.py" in prompt
    lowered = prompt.lower()
    assert "contradict" in lowered
    assert "read-only" in lowered or "read only" in lowered


def test_rework_test_author_prompt_omits_acceptance_block_when_no_fixtures():
    """A story with no acceptance block must get a byte-for-byte unchanged
    prompt - the oracle-conflict warning is scoped to oracle-graded stories."""
    prompt = p._rework_test_author_prompt("feedback", fix_checklist=None)
    assert "contradict" not in prompt.lower()
    assert prompt == p._rework_test_author_prompt(
        "feedback", fix_checklist=None, acceptance_paths=[]
    )


# ---------- _run_rework_test_author_phase ----------


def test_run_rework_test_author_phase_skips_when_role_unconfigured(
    monkeypatch, tmp_path
):
    """Mirrors test_run_test_author_phase_skips_when_role_unconfigured: with
    no test_author role configured, the phase must not reach a backend and
    must return False (fall open to the monolithic rework dispatch)."""
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)

    def _boom(*a, **k):
        raise AssertionError("dispatch must not be reached when unconfigured")

    monkeypatch.setattr(backend, "get_backend", _boom)
    result = p._run_rework_test_author_phase(
        {"agent_instructions": "Build it."},
        story_key="S1",
        worktree_path=tmp_path,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        review_feedback="Add a regression test for the crash.",
    )
    assert result is False


def test_run_rework_test_author_phase_returns_true_on_successful_commit(
    monkeypatch, tmp_path
):
    """When the resolved role differs from dispatch, the dispatch succeeds,
    and the agent branch has a new commit by the time it exits, the phase
    reports success - the committed test is what the executor builds on."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    (wt / "test_foo.py").write_text("def test_x(): assert True\n")
    subprocess.run(
        ["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "tests"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    )

    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p._run_rework_test_author_phase(
        {"agent_instructions": "Build it."},
        story_key="S1",
        worktree_path=wt,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        review_feedback="Add a regression test for the crash.",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is True
    assert fake.calls[0]["cwd"] == wt
    assert "Add a regression test for the crash." in fake.calls[0]["prompt"]
    assert "do NOT edit the implementation" in fake.calls[0]["prompt"]


def test_run_rework_test_author_phase_returns_false_when_no_new_commit(
    monkeypatch, tmp_path
):
    """The agent exited cleanly but committed nothing - the executor has
    nothing to build on, so fail open exactly like a timeout."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")

    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p._run_rework_test_author_phase(
        {"agent_instructions": "Build it."},
        story_key="S1",
        worktree_path=wt,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        review_feedback="Add a regression test for the crash.",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False


def test_run_rework_test_author_phase_returns_false_when_dispatch_raises(
    monkeypatch, tmp_path
):
    """A dispatch that raises must not raise past this function - fall open
    to False (monolithic rework dispatch)."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    fake = _FakeTestAuthorBackend(pid=0, raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    result = p._run_rework_test_author_phase(
        {"agent_instructions": "Build it."},
        story_key="S1",
        worktree_path=tmp_path,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        review_feedback="Add a regression test for the crash.",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False


def test_run_rework_test_author_phase_returns_false_on_timeout(monkeypatch, tmp_path):
    """A dispatch that times out must return False without checking for
    commits. _wait_for_agent_exit is a planner-module free var (imported at
    the top of planner.py), so the patch must land on planner's binding, not
    the server's re-export, for the bare-name call inside the function to
    see it."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(ppl, "_wait_for_agent_exit", lambda *a, **k: False)

    def _boom(*a, **k):
        raise AssertionError("must not check for commits when the dispatch timed out")

    monkeypatch.setattr(ppl, "_worktree_has_new_commits", _boom)

    result = p._run_rework_test_author_phase(
        {"agent_instructions": "Build it."},
        story_key="S1",
        worktree_path=tmp_path,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        review_feedback="Add a regression test for the crash.",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False
