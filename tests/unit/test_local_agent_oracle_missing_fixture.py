"""The acceptance oracle must never report green while an acceptance fixture
file is absent from the working copy.

Incident 2026-11-27 (Maven repository): the job checkout dropped the acceptance
test class, the scoped runner matched no tests and exited 0, and the run
committed "ORACLE GREEN" before any code change. The oracle now checks the
fixture files themselves instead of trusting the runner's exit code.

Shared fixtures/helpers (including the loaded `lao` module) live in
tests.unit._local_agent_oracle_test_helpers.
"""
import types

import pytest

from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401
    _isolate_environ,
    _sequence_chat,
    lao,
)

FIXTURE = "tests/test_acceptance.py"


class _RecordingRun:
    """Stands in for subprocess.run: records each argv and exits with a fixed code."""

    def __init__(self, returncode):
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return types.SimpleNamespace(returncode=self.returncode, stdout="3 passed", stderr="")


@pytest.fixture
def run(monkeypatch, tmp_path):
    """A pytest project rooted at tmp_path whose test command exits 0."""
    fake = _RecordingRun(returncode=0)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest"]))
    monkeypatch.setattr(lao.subprocess, "run", fake)
    return fake


def _write_fixture(tmp_path, rel):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_acceptance():\n    assert True\n")


def test_should_fail_when_configured_fixture_is_missing(monkeypatch, tmp_path, run):
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [FIXTURE])

    ok, _ = lao.oracle_result()

    assert ok is False


def test_should_name_the_missing_fixture_in_the_message(monkeypatch, tmp_path, run):
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [FIXTURE])

    _, message = lao.oracle_result()

    assert message == f"acceptance fixture missing from the working copy: {FIXTURE}"


def test_should_not_run_the_test_command_when_fixture_is_missing(monkeypatch, tmp_path, run):
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [FIXTURE])

    lao.oracle_result()

    assert run.calls == []


def test_should_name_only_the_missing_fixture_when_one_of_two_is_missing(monkeypatch, tmp_path, run):
    present = "tests/test_present.py"
    _write_fixture(tmp_path, present)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [present, FIXTURE])

    ok, message = lao.oracle_result()

    assert ok is False
    assert message.endswith(f": {FIXTURE}")
    assert present not in message


def test_should_treat_a_directory_at_the_fixture_path_as_missing(monkeypatch, tmp_path, run):
    (tmp_path / FIXTURE).mkdir(parents=True)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [FIXTURE])

    ok, message = lao.oracle_result()

    assert ok is False
    assert FIXTURE in message


def test_should_return_true_when_every_fixture_is_present_and_command_exits_zero(
    monkeypatch, tmp_path, run
):
    _write_fixture(tmp_path, FIXTURE)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [FIXTURE])

    ok, _ = lao.oracle_result()

    assert ok is True
    assert len(run.calls) == 1


def test_should_return_true_when_no_acceptance_paths_are_configured(monkeypatch, tmp_path, run):
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])

    assert lao.oracle_result() == (True, "(no acceptance files configured)")


def test_finish_if_green_should_not_commit_when_fixture_is_missing(monkeypatch, tmp_path, run):
    commits = []
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [FIXTURE])
    monkeypatch.setattr(lao, "worktree_dirty", lambda: True)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: commits.append(reason))

    assert lao.finish_if_green(0) is False
    assert commits == []


def test_boot_check_should_print_missing_fixture_line_and_skip_the_model(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [FIXTURE])
    monkeypatch.setattr(lao, "worktree_dirty", lambda: True)
    commits = []
    monkeypatch.setattr(lao, "auto_commit", lambda reason: commits.append(reason))
    fake, calls = _sequence_chat([("create_file", {"path": "x.py", "content": "x = 1\n"})])
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao._main_impl()
    out = capsys.readouterr().out

    assert f"[boot] acceptance fixture missing from the working copy: {FIXTURE}" in out.splitlines()
    assert calls == []
    assert commits == []
    assert rc == 1
