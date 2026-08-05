"""Acceptance fixture: deprecate the LOCAL_AGENT_{MAX_STEPS,NUM_CTX,TEMPERATURE}
transport-var alias (maturity plan A2).

Grades BOTH ends of the parent->child transport rename, so a half-done change
(only the reader, or only the setter) fails:
  1. scripts/local_agent.py must honor the NEW internal PIPELINE_TRANSPORT_* vars
     and treat the OLD LOCAL_AGENT_* names as fully inert (not read).
  2. backend.OllamaDriver.dispatch must set the NEW PIPELINE_TRANSPORT_* keys in
     the child subprocess env (so PIPELINE_LOCAL_* knobs still reach the child
     through the new channel), while still setting LOCAL_AGENT_* for back-compat.
  3. scripts/local_agent_oracle.py -- the SECOND reader, launched by the same
     backend.py dispatch env -- must be renamed in lockstep. It is a near-verbatim
     copy of local_agent.py; leaving it on the old names silently diverges the
     oracle harness from the dispatch harness.
  4. The in-repo callers/tests that set these vars directly must be migrated too,
     so the full suite stays green (this is the "no existing test regressed" bar).

On current master these tests FAIL: local_agent.py reads LOCAL_AGENT_* (so the
"old var inert" assertions see the leaked value), and backend does not set
PIPELINE_TRANSPORT_* (KeyError on the setter assertions).
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


# --- reader end: scripts/local_agent.py ---

def test_old_max_steps_var_is_inert(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "999")
    la = _load_local_agent()
    assert la.MAX_STEPS != 999, "LOCAL_AGENT_MAX_STEPS still honored (alias not removed)"
    assert la.MAX_STEPS == 40


def test_new_max_steps_var_honored(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("PIPELINE_TRANSPORT_MAX_STEPS", "13")
    la = _load_local_agent()
    assert la.MAX_STEPS == 13


def test_old_num_ctx_var_is_inert(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("LOCAL_AGENT_NUM_CTX", "99999")
    la = _load_local_agent()
    assert la.NUM_CTX != 99999
    assert la.NUM_CTX == 16384


def test_new_num_ctx_var_honored(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("PIPELINE_TRANSPORT_NUM_CTX", "8192")
    la = _load_local_agent()
    assert la.NUM_CTX == 8192


def test_old_temperature_var_is_inert(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("LOCAL_AGENT_TEMPERATURE", "0.777")
    la = _load_local_agent()
    assert la.TEMPERATURE != pytest.approx(0.777)
    assert la.TEMPERATURE == pytest.approx(0.3)


def test_new_temperature_var_honored(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("PIPELINE_TRANSPORT_TEMPERATURE", "0.7")
    la = _load_local_agent()
    assert la.TEMPERATURE == pytest.approx(0.7)


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
    # back-compat: the old name is still set so existing capture tests stay green


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


# --- second reader end: scripts/local_agent_oracle.py ---
# Gap found live 2026-07-29: the first attempt renamed local_agent.py only.
# local_agent_oracle.py is launched by the SAME dispatch env (backend.py
# _AGENT_SCRIPT_ORACLE) so it silently kept reading the dead names - the
# oracle harness diverged from the dispatch harness with nothing to catch it.
# NOTE the oracle's own defaults: MAX_STEPS 30 (not 40), NUM_CTX 16384, TEMPERATURE 0.3.

def test_oracle_old_num_ctx_var_is_inert(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("LOCAL_AGENT_NUM_CTX", "99999")
    lao = _load_oracle_agent()
    assert lao.NUM_CTX != 99999, "local_agent_oracle.py still reads LOCAL_AGENT_NUM_CTX"
    assert lao.NUM_CTX == 16384


def test_oracle_new_num_ctx_var_honored(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("PIPELINE_TRANSPORT_NUM_CTX", "8192")
    lao = _load_oracle_agent()
    assert lao.NUM_CTX == 8192


def test_oracle_old_max_steps_var_is_inert(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "999")
    lao = _load_oracle_agent()
    assert lao.MAX_STEPS != 999, "local_agent_oracle.py still reads LOCAL_AGENT_MAX_STEPS"
    assert lao.MAX_STEPS == 30


def test_oracle_new_max_steps_var_honored(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("PIPELINE_TRANSPORT_MAX_STEPS", "13")
    lao = _load_oracle_agent()
    assert lao.MAX_STEPS == 13


def test_oracle_old_temperature_var_is_inert(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("LOCAL_AGENT_TEMPERATURE", "0.777")
    lao = _load_oracle_agent()
    assert lao.TEMPERATURE != pytest.approx(0.777)
    assert lao.TEMPERATURE == pytest.approx(0.3)


def test_oracle_new_temperature_var_honored(monkeypatch, _clean_transport_env):
    monkeypatch.setenv("PIPELINE_TRANSPORT_TEMPERATURE", "0.7")
    lao = _load_oracle_agent()
    assert lao.TEMPERATURE == pytest.approx(0.7)


# --- no dead references left behind ---
# The first attempt left in-repo callers/tests still setting the old names.
# Those are exactly what turned the suite red while this oracle stayed green,
# so grade them here: after the rename, no non-test source file and no test
# helper may still try to CONFIGURE the transport through the dead names.
# backend.py is exempt: it deliberately keeps writing LOCAL_AGENT_* for
# back-compat (graded by the setter tests above) and names them in its
# operator-warning table.

_REPO = Path(__file__).parent.parent.parent


def test_no_source_file_still_reads_the_dead_transport_names():
    """Every os.environ read of the three renamed vars must be gone from the
    scripts/ readers - a leftover read is a silently-dead knob."""
    offenders = []
    for rel in ("scripts/local_agent.py", "scripts/local_agent_oracle.py"):
        text = (_REPO / rel).read_text()
        for var in _TRANSPORT_OLD:
            if f'environ.get("{var}"' in text or f"environ.get('{var}'" in text:
                offenders.append(f"{rel} still reads {var}")
    assert not offenders, "dead transport reads remain: " + "; ".join(offenders)


def test_experiment_runner_sets_the_new_transport_names():
    """tests/experiments/local_oracle/run.py launches the oracle script
    directly, so it must hand it the NEW channel or the run silently uses
    defaults."""
    text = (_REPO / "tests" / "experiments" / "local_oracle" / "run.py").read_text()
    for old, new in zip(_TRANSPORT_OLD, _TRANSPORT_NEW):
        assert f'"{old}"' not in text, f"run.py still sets the dead {old}"
        assert f'"{new}"' in text, f"run.py must set {new}"
