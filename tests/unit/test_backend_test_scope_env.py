"""OllamaDriver.dispatch() forwards the test-scope spec to the agent env.

A `test_scope_spec` kwarg is exported as LOCAL_AGENT_TEST_SCOPE (sorted-key
JSON). Absent kwarg leaves the env untouched so full-mode dispatches are
byte-for-byte unchanged.
"""
import json

from app import backend as b
from tests.unit._backend_helpers import _FakePopenResult


def _dispatch_env(tmp_path, monkeypatch, **kwargs):
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(60),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        acceptance=["tests/test_x.py"],
        **kwargs,
    )
    return captured["env"]


def test_dispatch_omits_test_scope_env_when_no_spec_passed(tmp_path, monkeypatch):
    """FULL-MODE PIN: no test_scope_spec kwarg must not set LOCAL_AGENT_TEST_SCOPE."""
    env = _dispatch_env(tmp_path, monkeypatch)
    assert "LOCAL_AGENT_TEST_SCOPE" not in env


def test_dispatch_exports_test_scope_spec_as_sorted_json(tmp_path, monkeypatch):
    spec = {"acceptance": ["tests/a.py"], "ci_failures": [], "base_branch": "main"}
    env = _dispatch_env(tmp_path, monkeypatch, test_scope_spec=spec)
    assert env["LOCAL_AGENT_TEST_SCOPE"] == json.dumps(spec, sort_keys=True)


def test_dispatch_exports_empty_test_scope_spec_as_empty_object(tmp_path, monkeypatch):
    """Boundary: an empty spec is still a scoped request, so it is exported, not dropped."""
    env = _dispatch_env(tmp_path, monkeypatch, test_scope_spec={})
    assert env["LOCAL_AGENT_TEST_SCOPE"] == "{}"
