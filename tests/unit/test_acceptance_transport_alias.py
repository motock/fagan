"""Acceptance fixture (story 1 of 2): backend.py must WRITE the new
PIPELINE_TRANSPORT_* transport keys into the dispatch subprocess env, while
still writing the legacy LOCAL_AGENT_* keys for back-compat.

This story does NOT touch the reader scripts - they keep reading the old names
and keep working, because backend.py keeps writing both. That is what makes
this half independently shippable and lets story 2 land safely afterwards.

On current master these FAIL: backend.py sets only LOCAL_AGENT_*.
"""
import importlib.util
import os
from pathlib import Path

import pytest

from app import backend as b

_LA_PATH = str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
_ORACLE_PATH = str(Path(__file__).parent.parent.parent / "scripts" / "local_agent_oracle.py")
_TRANSPORT_OLD = ("LOCAL_AGENT_MAX_STEPS", "LOCAL_AGENT_NUM_CTX", "LOCAL_AGENT_TEMPERATURE")
_TRANSPORT_NEW = ("PIPELINE_TRANSPORT_MAX_STEPS", "PIPELINE_TRANSPORT_NUM_CTX", "PIPELINE_TRANSPORT_TEMPERATURE")
_REAL = ("PIPELINE_LOCAL_MAX_STEPS", "PIPELINE_LOCAL_NUM_CTX", "PIPELINE_LOCAL_TEMPERATURE")


def _load_local_agent():
    """Exec scripts/local_agent.py fresh against the current os.environ."""
    os.environ["LOCAL_AGENT_MODEL"] = "test-model"
    spec = importlib.util.spec_from_file_location("local_agent_acceptance", _LA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_oracle_agent():
    """Exec scripts/local_agent_oracle.py fresh against the current os.environ.

    The oracle script is a near-verbatim copy of local_agent.py launched by the
    SAME backend.py dispatch env (backend.py picks it via _AGENT_SCRIPT_ORACLE),
    so it must read the same transport channel. Its own defaults differ
    (MAX_STEPS 30, not 40) - assert against the oracle's real defaults, not
    local_agent.py's."""
    os.environ["LOCAL_AGENT_MODEL"] = "test-model"
    spec = importlib.util.spec_from_file_location("oracle_acceptance", _ORACLE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def _clean_transport_env(monkeypatch):
    for k in _TRANSPORT_OLD + _TRANSPORT_NEW + _REAL:
        monkeypatch.delenv(k, raising=False)
    yield


# --- setter end: backend.OllamaDriver.dispatch ---

class _FakePopen:
    def __init__(self, pid):
        self.pid = pid


def test_dispatch_sets_new_transport_max_steps(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env) or _FakePopen(11))
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "12")
    b.OllamaDriver().dispatch("do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read", cwd=tmp_path,
        log_path=tmp_path / "agent.log", append=False)
    assert captured["env"]["PIPELINE_TRANSPORT_MAX_STEPS"] == "12"


def test_dispatch_sets_new_transport_num_ctx_and_temperature(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env) or _FakePopen(12))
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "8192")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "0.5")
    b.OllamaDriver().dispatch("do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read", cwd=tmp_path,
        log_path=tmp_path / "agent.log", append=False)
    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "8192"
    assert captured["env"]["PIPELINE_TRANSPORT_TEMPERATURE"] == "0.5"
