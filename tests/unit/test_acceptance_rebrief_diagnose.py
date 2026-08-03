"""Acceptance oracle: diagnose_failure must turn failure evidence into a root
cause via a configurable role, and must FAIL OPEN (return None) on every error
path - a diagnosis is an optimization, never a gate.
"""
from pipeline import rebrief


def test_returns_the_role_output_as_the_diagnosis(monkeypatch):
    monkeypatch.setattr(
        rebrief, "_run_diagnosis_role", lambda *a, **k: "TEMPLATE_DIR came from --repo-root"
    )
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) == (
        "TEMPLATE_DIR came from --repo-root"
    )


def test_unconfigured_role_fails_open(monkeypatch):
    monkeypatch.setattr(rebrief, "_run_diagnosis_role", lambda *a, **k: None)
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_role_exception_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(rebrief, "_run_diagnosis_role", boom)
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_empty_role_output_fails_open(monkeypatch):
    monkeypatch.setattr(rebrief, "_run_diagnosis_role", lambda *a, **k: "   ")
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_empty_evidence_skips_the_role_entirely(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the role with no evidence")

    monkeypatch.setattr(rebrief, "_run_diagnosis_role", boom)
    assert rebrief.diagnose_failure("", {"summary": "s"}) is None


# --- local-backend default for an unconfigured diagnosis role ---
# The "diagnosis" role ships unconfigured (no model_registry entry, no env), so
# without a default _run_diagnosis_role returns None and every rebrief is a blind
# retry. The default reuses the story's OWN local backend+model — the model that
# ran the struggle is the cheapest sensible diagnoser, and since step-cap only
# fires on local backends this never surprises an operator with Claude spend.


class _RecordingDriver:
    def __init__(self):
        self.complete_kwargs = {}

    def complete(self, *, prompt, system, model):
        self.complete_kwargs = {"prompt": prompt, "system": system, "model": model}
        return "ROOT CAUSE: the guard rejects legitimate appends."


def _no_diagnosis_role(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_DIAGNOSIS", raising=False)
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})


def test_unconfigured_role_falls_back_to_story_local_backend(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend",
                        lambda role, name=None: driver)

    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    assert rebrief._run_diagnosis_role("evidence", story) == (
        "ROOT CAUSE: the guard rejects legitimate appends."
    )
    # The diagnoser is the story's own backend+concrete model, not a default tier.
    assert driver.complete_kwargs["model"] == "gpt-oss:20b"
    assert "evidence" in driver.complete_kwargs["prompt"]


def test_unconfigured_role_uses_declared_model_when_no_dispatched_model(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend",
                        lambda role, name=None: driver)

    story = {"summary": "s", "backend": "local", "model": "devstral"}
    assert rebrief._run_diagnosis_role("evidence", story) == "ROOT CAUSE: the guard rejects legitimate appends."
    assert driver.complete_kwargs["model"] == "devstral"


def test_unconfigured_role_fails_open_for_claude_backend(monkeypatch):
    """A claude-backend story must NOT silently spend Claude on a diagnosis by
    default — fail open (None) so the retry is a plain resume, not a surprise bill."""
    _no_diagnosis_role(monkeypatch)

    def boom(role, name=None):
        raise AssertionError("must not dispatch a Claude diagnosis by default")
    monkeypatch.setattr(rebrief.backend, "get_backend", boom)

    story = {"summary": "s", "backend": "claude", "dispatched_model": "sonnet"}
    assert rebrief._run_diagnosis_role("evidence", story) is None


def test_unconfigured_role_fails_open_for_auto_backend(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    monkeypatch.setattr(rebrief.backend, "get_backend",
                        lambda role, name=None: (_ for _ in ()).throw(
                            AssertionError("auto must be resolved by the caller")))
    assert rebrief._run_diagnosis_role("evidence", {"summary": "s", "backend": "auto"}) is None


def test_unconfigured_role_fails_open_when_no_model(monkeypatch):
    _no_diagnosis_role(monkeypatch)
    monkeypatch.setattr(rebrief.backend, "get_backend",
                        lambda role, name=None: (_ for _ in ()).throw(AssertionError("no model")))
    assert rebrief._run_diagnosis_role("evidence", {"summary": "s", "backend": "ollama"}) is None


def test_explicit_diagnosis_env_wins_over_story_backend_default(monkeypatch):
    """An operator who set PIPELINE_BACKEND_DIAGNOSIS keeps getting that provider,
    not the story-backend default."""
    monkeypatch.setenv("PIPELINE_BACKEND_DIAGNOSIS", "mlx")
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})
    from app.role_registry import RoleResolution
    monkeypatch.setattr(rebrief.role_registry, "resolve_role",
                        lambda role, **k: RoleResolution(provider="mlx", model="qwen:30b"))
    seen = {}

    def fake_get_backend(role, name=None):
        seen["name"] = name
        return _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", fake_get_backend)

    story = {"summary": "s", "backend": "ollama", "dispatched_model": "gpt-oss:20b"}
    rebrief._run_diagnosis_role("evidence", story)
    assert seen["name"] == "mlx"
