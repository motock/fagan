"""Tests for pipeline/self_modification.py — the MCP-self-source-change
detection helpers used to notify an operator that the running pipeline MCP
server does not hot-reload after a merge touches its own source.

Run with the project venv:
    .venv/bin/python -m pytest -q tests/unit/test_mcp_self_modification.py
"""

import subprocess

from pipeline import self_modification as sm


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _FakeSubprocess:
    """Stand-in for the `subprocess` module attribute used inside the helper."""

    def __init__(self, proc=None, exc=None):
        self._proc = proc
        self._exc = exc
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._proc


def _fake(stdout="", returncode=0):
    return _FakeSubprocess(proc=_Proc(returncode=returncode, stdout=stdout))


# ---------- module shape ----------


def test_module_has_module_docstring():
    assert sm.__doc__ is not None
    assert sm.__doc__.strip() != ""


def test_all_exports_exactly_the_three_names():
    assert sm.__all__ == [
        "MCP_SELF_SOURCE_FILES",
        "_mcp_self_source_touched",
        "_mcp_restart_notice",
    ]


def test_self_source_files_constant_exact_value():
    assert sm.MCP_SELF_SOURCE_FILES == (
        "pipeline/server.py",
        "app/pipeline_mcp_server.py",
    )


def test_self_source_files_is_a_tuple():
    assert isinstance(sm.MCP_SELF_SOURCE_FILES, tuple)


def test_module_imports_subprocess_as_module_attribute():
    # The fake-subprocess seam requires `subprocess` to be a swappable
    # module-level attribute, not e.g. only `from subprocess import run`.
    assert sm.subprocess is subprocess


def test_module_does_not_import_pipeline_server():
    # Importing pipeline.server from this module would reintroduce the
    # circular import the story explicitly calls out to avoid. Inspect the
    # source text directly so the check doesn't depend on whether some
    # other test module already imported pipeline.server first.
    import inspect

    source = inspect.getsource(sm)
    assert "pipeline.server" not in source
    assert "pipeline import server" not in source


# ---------- _mcp_self_source_touched: happy path ----------


def test_detects_pipeline_server_py_among_unrelated_files(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "subprocess", _fake("pipeline/server.py\nREADME.md\n"))
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == [
        "pipeline/server.py"
    ]


def test_detects_app_module_on_its_own(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "subprocess", _fake("app/pipeline_mcp_server.py\n"))
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == [
        "app/pipeline_mcp_server.py"
    ]


def test_both_files_reported_once_each_in_constant_order(monkeypatch, tmp_path):
    # Diff lists app/pipeline_mcp_server.py BEFORE pipeline/server.py; the
    # helper must still return them in MCP_SELF_SOURCE_FILES order.
    monkeypatch.setattr(
        sm,
        "subprocess",
        _fake("app/pipeline_mcp_server.py\npipeline/server.py\ndocs/x.md\n"),
    )
    touched = sm._mcp_self_source_touched(str(tmp_path), "origin/master")
    assert touched == ["pipeline/server.py", "app/pipeline_mcp_server.py"]
    assert len(touched) == 2


def test_neither_file_returns_empty_list(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sm,
        "subprocess",
        _fake("pipeline/rebase.py\ndocs/CHANGELOG.md\n"),
    )
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == []


def test_substring_match_on_test_file_does_not_count(monkeypatch, tmp_path):
    # tests/unit/test_pipeline_mcp_server.py contains "pipeline_mcp_server.py"
    # as a substring but must NOT be treated as a hit on
    # app/pipeline_mcp_server.py.
    monkeypatch.setattr(
        sm,
        "subprocess",
        _fake("pipeline/rebase.py\ntests/unit/test_pipeline_mcp_server.py\n"),
    )
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == []


def test_returns_a_plain_list_not_tuple(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "subprocess", _fake("pipeline/server.py\n"))
    result = sm._mcp_self_source_touched(str(tmp_path), "origin/master")
    assert isinstance(result, list)


def test_blank_lines_in_diff_output_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sm, "subprocess", _fake("\npipeline/server.py\n\n")
    )
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == [
        "pipeline/server.py"
    ]


def test_duplicate_diff_lines_still_report_file_once(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sm, "subprocess", _fake("pipeline/server.py\npipeline/server.py\n")
    )
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == [
        "pipeline/server.py"
    ]


# ---------- _mcp_self_source_touched: git invocation shape ----------


def test_invokes_git_diff_with_triple_dot_base_ref_range(monkeypatch, tmp_path):
    fake = _fake("pipeline/server.py\n")
    monkeypatch.setattr(sm, "subprocess", fake)
    sm._mcp_self_source_touched(str(tmp_path), "origin/master")
    assert len(fake.calls) == 1
    cmd, _kwargs = fake.calls[0]
    assert cmd == ["git", "diff", "--name-only", "origin/master...HEAD"]


def test_git_diff_invocation_uses_expected_subprocess_kwargs(monkeypatch, tmp_path):
    fake = _fake("pipeline/server.py\n")
    monkeypatch.setattr(sm, "subprocess", fake)
    sm._mcp_self_source_touched(str(tmp_path), "origin/master")
    _, kwargs = fake.calls[0]
    assert kwargs.get("check") is False
    assert kwargs.get("cwd") == str(tmp_path)
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True


def test_base_ref_is_interpolated_into_the_diff_range(monkeypatch, tmp_path):
    fake = _fake("pipeline/server.py\n")
    monkeypatch.setattr(sm, "subprocess", fake)
    sm._mcp_self_source_touched(str(tmp_path), "some/other-ref")
    cmd, _ = fake.calls[0]
    assert cmd == ["git", "diff", "--name-only", "some/other-ref...HEAD"]


# ---------- _mcp_self_source_touched: negative / boundary cases ----------


def test_git_nonzero_exit_fails_open_and_raises_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sm, "subprocess", _fake("pipeline/server.py\n", returncode=128)
    )
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == []


def test_git_nonzero_exit_returncode_one_also_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sm, "subprocess", _fake("app/pipeline_mcp_server.py\n", returncode=1)
    )
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == []


def test_oserror_from_subprocess_run_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sm, "subprocess", _FakeSubprocess(exc=OSError("git executable not found"))
    )
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == []


def test_missing_worktree_directory_never_shells_out(monkeypatch):
    fake = _fake("pipeline/server.py\n")
    monkeypatch.setattr(sm, "subprocess", fake)
    result = sm._mcp_self_source_touched("/nonexistent/worktree/path", "origin/master")
    assert result == []
    assert fake.calls == []


def test_empty_worktree_string_fails_open_without_shelling_out(monkeypatch):
    fake = _fake("pipeline/server.py\n")
    monkeypatch.setattr(sm, "subprocess", fake)
    assert sm._mcp_self_source_touched("", "origin/master") == []
    assert fake.calls == []


def test_none_worktree_fails_open_without_shelling_out(monkeypatch):
    fake = _fake("pipeline/server.py\n")
    monkeypatch.setattr(sm, "subprocess", fake)
    assert sm._mcp_self_source_touched(None, "origin/master") == []
    assert fake.calls == []


def test_worktree_pointing_at_a_file_not_a_directory_fails_open(monkeypatch, tmp_path):
    file_path = tmp_path / "not_a_dir.txt"
    file_path.write_text("x")
    fake = _fake("pipeline/server.py\n")
    monkeypatch.setattr(sm, "subprocess", fake)
    assert sm._mcp_self_source_touched(str(file_path), "origin/master") == []
    assert fake.calls == []


def test_empty_diff_output_returns_empty_list(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "subprocess", _fake(""))
    assert sm._mcp_self_source_touched(str(tmp_path), "origin/master") == []


# ---------- _mcp_restart_notice: happy path ----------


def test_notice_contains_reconnect_command_literal():
    msg = sm._mcp_restart_notice(["pipeline/server.py"])
    assert "/mcp reconnect" in msg


def test_notice_contains_every_touched_path_single_file():
    msg = sm._mcp_restart_notice(["pipeline/server.py"])
    assert "pipeline/server.py" in msg


def test_notice_contains_every_touched_path_both_files():
    msg = sm._mcp_restart_notice(
        ["pipeline/server.py", "app/pipeline_mcp_server.py"]
    )
    assert "pipeline/server.py" in msg
    assert "app/pipeline_mcp_server.py" in msg
    assert "/mcp reconnect" in msg


def test_notice_mentions_no_hot_reload_or_pre_merge_code():
    msg = sm._mcp_restart_notice(["pipeline/server.py"])
    lowered = msg.lower()
    assert "hot-reload" in lowered or "hot reload" in lowered
    assert "pre-merge" in lowered or "pre merge" in lowered


def test_notice_says_do_this_before_further_story_work():
    msg = sm._mcp_restart_notice(["pipeline/server.py"])
    lowered = msg.lower()
    assert "dispatch" in lowered
    assert "review" in lowered


def test_notice_returns_a_string():
    assert isinstance(sm._mcp_restart_notice(["pipeline/server.py"]), str)


# ---------- _mcp_restart_notice: negative / boundary cases ----------


def test_notice_with_empty_list_still_returns_a_string_with_reconnect_command():
    msg = sm._mcp_restart_notice([])
    assert isinstance(msg, str)
    assert "/mcp reconnect" in msg


def test_notice_with_single_file_list_omits_the_other_file():
    msg = sm._mcp_restart_notice(["pipeline/server.py"])
    assert "app/pipeline_mcp_server.py" not in msg
