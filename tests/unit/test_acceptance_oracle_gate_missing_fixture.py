"""Acceptance oracle gate: validate_acceptance_fixtures must report "errors"
when a declared fixture file is not in the checkout, without running the test
command. A scoped runner matches no tests when the file is absent and exits 0,
which previously let a missing fixture pass the pre-dispatch gate.
"""
import types

from pipeline import oracle_gate

FIXTURE = "tests/unit/test_zz_missing_oracle.py"


def _story(paths):
    return {
        "summary": "s",
        "acceptance": [{"path": path, "source": "x"} for path in paths],
    }


def _checkout(tmp_path):
    # A pyproject.toml makes detect_test_command pick pytest, as in a real
    # worktree checkout (see test_acceptance_oracle_gate_run.py).
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    return tmp_path


def _recording_run(monkeypatch, returncode):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=returncode, stdout="E  assert 0", stderr="")

    monkeypatch.setattr(oracle_gate.subprocess, "run", fake_run)
    return calls


def test_should_report_errors_when_a_fixture_file_is_missing(tmp_path, monkeypatch):
    _recording_run(monkeypatch, returncode=0)

    result = oracle_gate.validate_acceptance_fixtures(_story([FIXTURE]), _checkout(tmp_path))

    assert result["state"] == "errors"
    assert result["detail"] == f"acceptance fixture missing: {FIXTURE}"
    assert result["paths"] == [FIXTURE]


def test_should_not_run_the_command_when_a_fixture_file_is_missing(tmp_path, monkeypatch):
    calls = _recording_run(monkeypatch, returncode=0)

    oracle_gate.validate_acceptance_fixtures(_story([FIXTURE]), _checkout(tmp_path))

    assert calls == []


def test_should_treat_a_directory_at_the_fixture_path_as_missing(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    (checkout / FIXTURE).mkdir(parents=True)
    calls = _recording_run(monkeypatch, returncode=0)

    result = oracle_gate.validate_acceptance_fixtures(_story([FIXTURE]), checkout)

    assert result["state"] == "errors"
    assert calls == []


def test_should_keep_fails_correctly_for_a_present_fixture_that_fails(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    fixture = checkout / FIXTURE
    fixture.parent.mkdir(parents=True)
    fixture.write_text("def test_x():\n    assert 0\n")
    _recording_run(monkeypatch, returncode=1)

    result = oracle_gate.validate_acceptance_fixtures(_story([FIXTURE]), checkout)

    assert result["state"] == "fails_correctly"
