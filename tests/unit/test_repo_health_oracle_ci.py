import inspect
import subprocess
from pathlib import Path

from pipeline import oracle_gate, repo_health

# ---------------------------------------------------------------------------
# Module wiring
# ---------------------------------------------------------------------------

def test_module_imports_validate_acceptance_fixtures_at_module_level():
    # Must be the exact same object as pipeline.oracle_gate.validate_acceptance_fixtures,
    # imported at module level (not re-implemented or lazily imported).
    assert repo_health.validate_acceptance_fixtures is oracle_gate.validate_acceptance_fixtures


def test_all_exports_oracle_finding_and_ci_finding():
    assert "oracle_finding" in repo_health.__all__
    assert "ci_finding" in repo_health.__all__


def test_repo_health_does_not_import_ci_or_server_modules():
    # ci_finding must be pure: pipeline/ci.py lazily imports pipeline.server, so
    # repo_health.py must not import from .ci (or .server) at all - that is what
    # lets pipeline/server.py import pipeline/repo_health.py at module level
    # without a cycle.
    source = inspect.getsource(repo_health)
    assert "from .ci import" not in source
    assert "from .ci " not in source
    assert "import pipeline.ci" not in source
    assert "from .server" not in source
    assert "import pipeline.server" not in source


def test_ci_finding_docstring_records_purity_rationale():
    doc = (repo_health.ci_finding.__doc__ or "").lower()
    assert doc, "ci_finding must have a docstring explaining why it is pure"
    assert "pipeline.ci" in doc or ".ci" in doc
    assert "cycle" in doc or "circular" in doc


# ---------------------------------------------------------------------------
# oracle_finding
# ---------------------------------------------------------------------------

def _story(paths=None):
    paths = paths if paths is not None else ["tests/acceptance_foo.py"]
    return {"acceptance": [{"path": p, "source": "# fixture"} for p in paths]}


def test_oracle_finding_calls_validate_with_story_and_path(monkeypatch):
    captured = {}

    def fake_validate(story, checkout):
        captured["story"] = story
        captured["checkout"] = checkout
        return {"state": "none", "detail": "story has no acceptance fixtures", "paths": []}

    monkeypatch.setattr(repo_health, "validate_acceptance_fixtures", fake_validate)
    story = _story()
    repo_health.oracle_finding(story, "/tmp/checkout")

    assert captured["story"] is story
    assert captured["checkout"] == Path("/tmp/checkout")
    assert isinstance(captured["checkout"], Path)


def test_oracle_finding_state_none_returns_none(monkeypatch):
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {"state": "none", "detail": "no fixtures", "paths": []},
    )
    assert repo_health.oracle_finding(_story(), "/tmp/repo") is None


def test_oracle_finding_state_fails_correctly_returns_none(monkeypatch):
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {
            "state": "fails_correctly",
            "detail": "fixture fails as expected",
            "paths": ["tests/acceptance_foo.py"],
        },
    )
    assert repo_health.oracle_finding(_story(), "/tmp/repo") is None


def test_oracle_finding_state_errors_returns_oracle_broken(monkeypatch):
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {
            "state": "errors",
            "detail": "oracle is broken (ModuleNotFoundError): boom",
            "paths": ["tests/acceptance_foo.py"],
        },
    )
    result = repo_health.oracle_finding(_story(), "/tmp/repo")
    assert result is not None
    assert result["kind"] == "oracle_broken"
    assert result["detail"] == "oracle is broken (ModuleNotFoundError): boom"
    assert result["paths"] == ["tests/acceptance_foo.py"]


def test_oracle_finding_state_errors_detail_truncated_to_800(monkeypatch):
    long_detail = "x" * 5000
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {"state": "errors", "detail": long_detail, "paths": []},
    )
    result = repo_health.oracle_finding(_story(), "/tmp/repo")
    assert result["detail"] == long_detail[-800:]
    assert len(result["detail"]) == 800


def test_oracle_finding_state_errors_missing_paths_defaults_empty(monkeypatch):
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {"state": "errors", "detail": "broken"},
    )
    result = repo_health.oracle_finding(_story(), "/tmp/repo")
    assert result["paths"] == []


def test_oracle_finding_state_passes_returns_oracle_already_passes(monkeypatch):
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {
            "state": "passes",
            "detail": "acceptance fixture already passes",
            "paths": ["tests/acceptance_foo.py"],
        },
    )
    result = repo_health.oracle_finding(_story(), "/tmp/repo")
    assert result is not None
    assert result["kind"] == "oracle_already_passes"
    assert result["detail"] == "acceptance fixture already passes"
    assert result["paths"] == ["tests/acceptance_foo.py"]


def test_oracle_finding_state_empty_returns_oracle_empty(monkeypatch):
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {
            "state": "empty",
            "detail": "no tests were collected",
            "paths": ["tests/acceptance_foo.py"],
        },
    )
    result = repo_health.oracle_finding(_story(), "/tmp/repo")
    assert result is not None
    assert result["kind"] == "oracle_empty"
    assert result["detail"] == "no tests were collected"


def test_oracle_finding_unrecognized_state_returns_none(monkeypatch):
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {"state": "some_future_state", "detail": "?", "paths": []},
    )
    assert repo_health.oracle_finding(_story(), "/tmp/repo") is None


def test_oracle_finding_absent_state_key_returns_none(monkeypatch):
    monkeypatch.setattr(
        repo_health,
        "validate_acceptance_fixtures",
        lambda story, checkout: {"detail": "no state key at all"},
    )
    assert repo_health.oracle_finding(_story(), "/tmp/repo") is None


def test_oracle_finding_validate_raises_returns_oracle_probe_failed(monkeypatch):
    def raiser(story, checkout):
        raise RuntimeError("validate_acceptance_fixtures blew up")

    monkeypatch.setattr(repo_health, "validate_acceptance_fixtures", raiser)
    result = repo_health.oracle_finding(_story(), "/tmp/repo")
    assert result is not None
    assert result["kind"] == "oracle_probe_failed"
    assert result["detail"] == "RuntimeError: validate_acceptance_fixtures blew up"
    assert result["paths"] == []


def test_oracle_finding_validate_raises_arbitrary_exception_type(monkeypatch):
    def raiser(story, checkout):
        raise ValueError("bad story shape")

    monkeypatch.setattr(repo_health, "validate_acceptance_fixtures", raiser)
    result = repo_health.oracle_finding(_story(), "/tmp/repo")
    assert result["kind"] == "oracle_probe_failed"
    assert result["detail"].startswith("ValueError:")
    assert "bad story shape" in result["detail"]


# ---------------------------------------------------------------------------
# ci_finding
# ---------------------------------------------------------------------------

def test_ci_finding_none_returns_none():
    assert repo_health.ci_finding(None) is None


def test_ci_finding_non_dict_returns_none():
    assert repo_health.ci_finding("not a dict") is None
    assert repo_health.ci_finding(["state", "pass"]) is None
    assert repo_health.ci_finding(42) is None


def test_ci_finding_empty_dict_returns_none_without_keyerror():
    # A missing 'state' key must not raise KeyError.
    assert repo_health.ci_finding({}) is None


def test_ci_finding_state_pass_returns_none():
    assert repo_health.ci_finding({"state": "pass"}) is None


def test_ci_finding_state_pending_returns_none():
    assert repo_health.ci_finding({"state": "pending"}) is None


def test_ci_finding_state_none_returns_ci_unavailable():
    result = repo_health.ci_finding({"state": "none", "error": "gh: billing"})
    assert result is not None
    assert result["kind"] == "ci_unavailable"
    assert "gh: billing" in result["detail"]


def test_ci_finding_state_fail_returns_ci_red():
    result = repo_health.ci_finding({"state": "fail", "error": "lint job red"})
    assert result is not None
    assert result["kind"] == "ci_red"
    assert "lint job red" in result["detail"]


def test_ci_finding_unrecognized_state_returns_none():
    assert repo_health.ci_finding({"state": "bogus", "error": "?"}) is None


def test_ci_finding_state_fail_missing_error_key_defaults_empty_string():
    result = repo_health.ci_finding({"state": "fail"})
    assert result is not None
    assert result["kind"] == "ci_red"
    assert result["detail"] == ""


def test_ci_finding_detail_truncated_to_800_chars():
    long_error = "e" * 5000
    result = repo_health.ci_finding({"state": "fail", "error": long_error})
    assert len(result["detail"]) == 800
    assert result["detail"] == long_error[:800]


def test_ci_finding_does_not_call_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ci_finding must not invoke subprocess.run")

    monkeypatch.setattr(subprocess, "run", boom)
    result = repo_health.ci_finding({"state": "fail", "error": "lint job red"})
    assert result["kind"] == "ci_red"

    result_pass = repo_health.ci_finding({"state": "pass"})
    assert result_pass is None
