"""Acceptance oracle: validate_acceptance_fixtures must actually RUN the story's
fixtures, scoped to the fixture paths, against a given checkout - not merely
inspect their source text.

Grades the integration: the command handed to subprocess must be the repo's
detected test runner scoped to the acceptance paths, and the classification
must come from classify_oracle_outcome.
"""
import types

from pipeline import oracle_gate


def _story(tmp_path):
    # A pyproject.toml is what detect_test_command uses to recognize a pytest
    # project (mirrors a real worktree checkout, which always has one) - an
    # empty tmp_path would fall back to its generic "npm test" default,
    # which cannot be scoped to specific paths.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    return {
        "summary": "s",
        "acceptance": [{"path": "tests/unit/test_zz_fake_oracle.py", "source": "x"}],
    }


def test_runs_a_command_scoped_to_the_acceptance_paths(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=1, stdout="E  assert 0", stderr="")

    monkeypatch.setattr(oracle_gate.subprocess, "run", fake_run)
    result = oracle_gate.validate_acceptance_fixtures(_story(tmp_path), tmp_path)

    assert any("test_zz_fake_oracle.py" in str(part) for part in captured["cmd"]), (
        "the run must be scoped to the story's acceptance fixture paths"
    )
    assert result["state"] == "fails_correctly"


def test_reports_a_broken_fixture_as_errors(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(
            returncode=2, stdout="E   xml.parsers.expat.ExpatError: nope", stderr=""
        )

    monkeypatch.setattr(oracle_gate.subprocess, "run", fake_run)
    result = oracle_gate.validate_acceptance_fixtures(_story(tmp_path), tmp_path)
    assert result["state"] == "errors"
    assert "Expat" in result["detail"] or "expat" in result["detail"]


def test_reports_an_already_satisfied_fixture_as_passes(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout="3 passed", stderr="")

    monkeypatch.setattr(oracle_gate.subprocess, "run", fake_run)
    assert oracle_gate.validate_acceptance_fixtures(_story(tmp_path), tmp_path)["state"] == "passes"


def test_story_without_acceptance_is_skipped(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not run a test command for a story with no oracle")

    monkeypatch.setattr(oracle_gate.subprocess, "run", boom)
    assert oracle_gate.validate_acceptance_fixtures({"summary": "s"}, tmp_path)["state"] == "none"


def test_subprocess_failure_is_reported_not_raised(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("no runner")

    monkeypatch.setattr(oracle_gate.subprocess, "run", boom)
    result = oracle_gate.validate_acceptance_fixtures(_story(tmp_path), tmp_path)
    assert result["state"] == "errors"


def test_returns_the_paths_it_graded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        oracle_gate.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="E assert 0", stderr=""),
    )
    result = oracle_gate.validate_acceptance_fixtures(_story(tmp_path), tmp_path)
    assert result["paths"] == ["tests/unit/test_zz_fake_oracle.py"]

