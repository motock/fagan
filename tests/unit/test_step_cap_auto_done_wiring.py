"""Wiring tests for the step-cap auto-done path (scripts/local_agent.py).

A local run that reaches the step cap now commits any uncommitted work and
then asks ``scripts/local_agent_git._step_cap_auto_done_impl`` whether the
branch already landed production changes with a green full suite + lint. When
it has, the run exits 0 (done -> review) instead of being parked (rc 2).

These tests grade the REAL exit path: ``scripts.local_agent._main_impl()`` is
driven with ``MAX_STEPS`` monkeypatched to 1 and the chat layer stubbed at its
boundary, so the single step never calls ``done`` and the loop falls through
to the step-cap block (same harness shape as test_local_agent_done_bar.py).
"""
import os
import subprocess
from pathlib import Path

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")

import scripts.local_agent as la
import scripts.local_agent_git as lag

_ROOT = Path(__file__).resolve().parent.parent.parent
_LOCAL_AGENT_SRC = _ROOT / "scripts" / "local_agent.py"
_REFERENCE = _ROOT / "REFERENCE.md"

_STEP_CAP_LINE = "[ended without done — step cap reached]"
_DONE_PREFIX = "[step 1] DONE: step cap reached"


def _init_git_repo(path):
    subprocess.run(["git", "init"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=False, cwd=path, capture_output=True, text=True)


def _view_file_chat(messages):
    """A chat stub that never calls `done`: one harmless view_file per step."""
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "view_file",
                                         "arguments": {"path": "widget.py"}}}]}


def _drive_step_cap(monkeypatch, tmp_path, auto_done, *, dirty=False, cid=""):
    """Run the real _main_impl() to the step cap with MAX_STEPS=1."""
    _init_git_repo(tmp_path)
    (tmp_path / "widget.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "MAX_STEPS", 1)
    monkeypatch.setattr(la, "chat", _view_file_chat)
    monkeypatch.setattr(la, "worktree_dirty", lambda: dirty)
    monkeypatch.setattr(la, "auto_wip_commit", lambda reason: None)
    monkeypatch.setattr(la, "_step_cap_auto_done", lambda: auto_done)
    if cid:
        monkeypatch.setenv("PIPELINE_CORRELATION_ID", cid)
    else:
        monkeypatch.delenv("PIPELINE_CORRELATION_ID", raising=False)
    return la._main_impl()


# --------------------------------------------------------------------------
# The real exit path
# --------------------------------------------------------------------------

def test_auto_done_exits_zero_and_never_prints_step_cap_line(
        tmp_path, monkeypatch, capsys):
    """_step_cap_auto_done() True -> rc 0 and NO step-cap line at all."""
    rc = _drive_step_cap(monkeypatch, tmp_path, True)
    out = capsys.readouterr().out

    assert rc == 0, f"auto-done must exit as done (0); rc={rc}\n{out!r}"
    assert "step cap reached]" not in out, out


def test_not_auto_done_exits_two_with_step_cap_line_last(
        tmp_path, monkeypatch, capsys):
    """_step_cap_auto_done() False -> rc 2 and the step-cap line is the LAST
    non-empty stdout line (pipeline/story_status.py matches it as such)."""
    rc = _drive_step_cap(monkeypatch, tmp_path, False)
    out = capsys.readouterr().out

    assert rc == 2, f"not-done must stay parked (2); rc={rc}\n{out!r}"
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, out
    assert lines[-1] == _STEP_CAP_LINE, out


def test_auto_done_emits_done_line_at_max_steps_with_correlation_id(
        tmp_path, monkeypatch, capsys):
    """The auto-done path emits the DONE step line at MAX_STEPS, carrying the
    correlation id, with the brief's exact wording."""
    rc = _drive_step_cap(monkeypatch, tmp_path, True, cid="cid-abc123")
    out = capsys.readouterr().out

    assert rc == 0, out
    assert _DONE_PREFIX in out, out
    assert "production changes present and the full suite and lint pass" in out, out
    assert "[cid=cid-abc123]" in out, out


def test_not_auto_done_does_not_emit_done_line(tmp_path, monkeypatch, capsys):
    """Boundary: the parked path must not claim done."""
    rc = _drive_step_cap(monkeypatch, tmp_path, False)
    out = capsys.readouterr().out

    assert rc == 2, out
    assert "DONE: step cap reached" not in out, out


def test_commit_happens_before_the_auto_done_check(tmp_path, monkeypatch):
    """Ordering: the auto-done check diffs HEAD, so uncommitted work must be
    committed FIRST (commit -> check)."""
    _init_git_repo(tmp_path)
    (tmp_path / "widget.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "MAX_STEPS", 1)
    monkeypatch.setattr(la, "chat", _view_file_chat)
    monkeypatch.setattr(la, "worktree_dirty", lambda: True)

    order = []
    monkeypatch.setattr(la, "auto_wip_commit",
                        lambda reason: order.append(("commit", reason)))

    def _check():
        order.append(("check", None))
        return True

    monkeypatch.setattr(la, "_step_cap_auto_done", _check)

    rc = la._main_impl()

    assert rc == 0, order
    assert [kind for kind, _ in order] == ["commit", "check"], order
    assert order[0][1] == "step cap reached", order


def test_not_auto_done_still_commits_dirty_worktree(tmp_path, monkeypatch):
    """The parked path is unchanged: a dirty worktree is still WIP-committed."""
    _init_git_repo(tmp_path)
    (tmp_path / "widget.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "MAX_STEPS", 1)
    monkeypatch.setattr(la, "chat", _view_file_chat)
    monkeypatch.setattr(la, "worktree_dirty", lambda: True)

    commits = []
    monkeypatch.setattr(la, "auto_wip_commit", lambda reason: commits.append(reason))
    monkeypatch.setattr(la, "_step_cap_auto_done", lambda: False)

    rc = la._main_impl()

    assert rc == 2, commits
    assert commits == ["step cap reached"], commits


# --------------------------------------------------------------------------
# The delegating wrapper
# --------------------------------------------------------------------------

def test_wrapper_delegates_to_git_impl_with_module_globals(monkeypatch):
    """_step_cap_auto_done() forwards its module globals() to the git impl."""
    seen = []

    def _stub(origin):
        seen.append(origin)
        return True

    monkeypatch.setattr(lag, "_step_cap_auto_done_impl", _stub)

    assert la._step_cap_auto_done() is True
    assert len(seen) == 1, seen
    origin = seen[0]
    assert isinstance(origin, dict), type(origin)
    assert "_full_suite_result" in origin, sorted(origin)
    assert origin["_full_suite_result"] is la._full_suite_result


def test_wrapper_returns_impl_false(monkeypatch):
    """Boundary: a False from the impl is propagated verbatim."""
    monkeypatch.setattr(lag, "_step_cap_auto_done_impl", lambda origin: False)
    assert la._step_cap_auto_done() is False


def test_wrapper_docstring_points_at_the_impl():
    doc = la._step_cap_auto_done.__doc__ or ""
    assert "local_agent_git" in doc, doc
    assert "_step_cap_auto_done_impl" in doc, doc


# --------------------------------------------------------------------------
# Source-shape requirements
# --------------------------------------------------------------------------

def test_wrapper_defined_immediately_after_full_suite_result():
    src = _LOCAL_AGENT_SRC.read_text(encoding="utf-8")
    i_full = src.index("def _full_suite_result()")
    i_next = src.index("\ndef ", i_full + 1)
    assert src[i_next + 1:].startswith("def _step_cap_auto_done()"), src[i_next:i_next + 80]


def test_wrapper_body_mirrors_full_suite_result_shape():
    src = _LOCAL_AGENT_SRC.read_text(encoding="utf-8")
    i = src.index("def _step_cap_auto_done()")
    body = src[i:i + 400]
    assert "from scripts.local_agent_git import _step_cap_auto_done_impl" in body, body
    assert "return _step_cap_auto_done_impl(globals())" in body, body


def test_main_impl_commits_then_checks_before_printing_step_cap_line():
    src = _LOCAL_AGENT_SRC.read_text(encoding="utf-8")
    i_commit = src.index('auto_wip_commit("step cap reached")')
    i_check = src.index("if _step_cap_auto_done():")
    i_print = src.index('print("[ended without done — step cap reached]", flush=True)')
    assert i_commit < i_check < i_print, (i_commit, i_check, i_print)


# --------------------------------------------------------------------------
# Documentation
# --------------------------------------------------------------------------

def test_reference_documents_step_cap_auto_done_bullet():
    ref = _REFERENCE.read_text(encoding="utf-8")
    first = "   - **Step-cap auto-done:** when a local run reaches the step cap, the agent"
    assert ref.count(first) == 1, ref.count(first)
    assert "production file (tests and dotfiles do not count)" in ref
    assert "merge-base with the default branch" in ref
    assert "full suite and lint pass" in ref
    assert "Otherwise the step-cap path below runs unchanged." in ref

    lines = ref.splitlines()
    i_anchor = next(
        i for i, ln in enumerate(lines)
        if ln.strip().startswith("- **On repeated step-cap interrupts:**")
    )
    prev = lines[i_anchor - 1].strip()
    assert prev.startswith("for a step-cap rebrief."), prev
    assert prev.endswith("Otherwise the step-cap path below runs unchanged."), prev
