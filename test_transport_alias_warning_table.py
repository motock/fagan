"""Test suite for story 1 of 2: backend.py must WRITE the new
PIPELINE_TRANSPORT_* transport keys AND extend the module-load operator-warning
table to name the three new vars (six entries total).

Scope of THIS dispatch: test authoring only. The implementation does not exist
yet on this branch, so every test here is expected to FAIL until a later
dispatch implements the two backend.py edits described in the story.

What these tests cover (the acceptance fixture already covers the dispatch-env
setter happy path; these tests cover the rest of the contract):

  1. The module-load warning table warns EXACTLY ONCE per triggered var, for
     all six transport names (three old LOCAL_AGENT_* + three new
     PIPELINE_TRANSPORT_*).
  2. Each warning message points the operator at the correct real knob
     (PIPELINE_LOCAL_*).
  3. The dispatch env build writes BOTH the old and the new keys for all three
     transports, including the boundary case where the real knobs are unset
     (defaults flow through).
  4. Setting a new PIPELINE_TRANSPORT_* var triggers the warning (negative:
     operators who set the new transport name are also warned, because it is
     still a transport-only channel, not a knob).
"""
import importlib
import logging
import sys

import pytest

import backend as b

_TRANSPORT_OLD = ("LOCAL_AGENT_MAX_STEPS", "LOCAL_AGENT_NUM_CTX", "LOCAL_AGENT_TEMPERATURE")
_TRANSPORT_NEW = ("PIPELINE_TRANSPORT_MAX_STEPS", "PIPELINE_TRANSPORT_NUM_CTX", "PIPELINE_TRANSPORT_TEMPERATURE")
_REAL = ("PIPELINE_LOCAL_MAX_STEPS", "PIPELINE_LOCAL_NUM_CTX", "PIPELINE_LOCAL_TEMPERATURE")

# Map each transport var -> the real knob the warning must name.
_TRANSPORT_TO_REAL = {
    "LOCAL_AGENT_MAX_STEPS": "PIPELINE_LOCAL_MAX_STEPS",
    "LOCAL_AGENT_NUM_CTX": "PIPELINE_LOCAL_NUM_CTX",
    "LOCAL_AGENT_TEMPERATURE": "PIPELINE_LOCAL_TEMPERATURE",
    "PIPELINE_TRANSPORT_MAX_STEPS": "PIPELINE_LOCAL_MAX_STEPS",
    "PIPELINE_TRANSPORT_NUM_CTX": "PIPELINE_LOCAL_NUM_CTX",
    "PIPELINE_TRANSPORT_TEMPERATURE": "PIPELINE_LOCAL_TEMPERATURE",
}


class _FakePopen:
    def __init__(self, pid):
        self.pid = pid


def _reload_backend_with_env(monkeypatch, env_overrides):
    """Reload backend.py fresh so its module-load warning table re-runs against
    the env we set. Returns the reloaded module and the list of captured
    'pipeline' logger warnings."""
    # Clean every transport + real var first so a prior test's env cannot leak.
    for k in _TRANSPORT_OLD + _TRANSPORT_NEW + _REAL:
        monkeypatch.delenv(k, raising=False)
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    logger = logging.getLogger("pipeline")
    logger.addHandler(handler)
    prev_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        # Drop any cached module so the top-level warning loop re-executes.
        monkeypatch.delitem(sys.modules, "backend", raising=False)
        # Also drop modules backend imports at load that hold state, so the
        # reload is clean. inference_providers is a leaf dependency.
        monkeypatch.delitem(sys.modules, "inference_providers", raising=False)
        importlib.invalidate_caches()
        mod = importlib.import_module("backend")
        return mod, records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)


# ---------------------------------------------------------------------------
# Warning table: six entries, each warns exactly once, names the real knob.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("transport_var", list(_TRANSPORT_TO_REAL))
def test_warning_table_warns_once_per_transport_var(monkeypatch, transport_var):
    """Each of the six transport names, when set alone, must produce EXACTLY ONE
    warning. The story is explicit: 'each triggered var must warn EXACTLY
    ONCE' - a duplicate _logger.warning call (e.g. from a copy-paste of the
    loop body) would make this fail."""
    _mod, records = _reload_backend_with_env(monkeypatch, {transport_var: "x"})
    matches = [m for m in records if transport_var in m]
    assert len(matches) == 1, (
        f"expected exactly one warning mentioning {transport_var}, got "
        f"{len(matches)}: {matches}"
    )


@pytest.mark.parametrize("transport_var", list(_TRANSPORT_TO_REAL))
def test_warning_table_names_the_real_knob(monkeypatch, transport_var):
    """The warning for a transport var must point the operator at the correct
    real knob (PIPELINE_LOCAL_*), not at a sibling transport var."""
    real = _TRANSPORT_TO_REAL[transport_var]
    _mod, records = _reload_backend_with_env(monkeypatch, {transport_var: "x"})
    msg = next(m for m in records if transport_var in m)
    assert real in msg, (
        f"warning for {transport_var} must name the real knob {real}; got: {msg}"
    )


def test_warning_table_has_six_entries(monkeypatch):
    """Setting all six transport vars at once must yield six distinct warnings
    (one per var). This guards against the table silently dropping the new
    PIPELINE_TRANSPORT_* entries."""
    env = {v: "x" for v in _TRANSPORT_TO_REAL}
    _mod, records = _reload_backend_with_env(monkeypatch, env)
    for var in _TRANSPORT_TO_REAL:
        matches = [m for m in records if var in m]
        assert len(matches) == 1, (
            f"{var}: expected exactly one warning, got {len(matches)}"
        )


def test_warning_table_silent_when_no_transport_var_set(monkeypatch):
    """With no transport var set, the warning table must emit nothing - the
    real knobs (PIPELINE_LOCAL_*) being set is the normal, non-warning path."""
    _mod, records = _reload_backend_with_env(
        monkeypatch,
        {r: "x" for r in _REAL},
    )
    transport_warnings = [m for m in records if any(v in m for v in _TRANSPORT_TO_REAL)]
    assert transport_warnings == [], (
        f"no transport var set, but got warnings: {transport_warnings}"
    )


# ---------------------------------------------------------------------------
# Dispatch env build: both old AND new keys present, defaults flow through.
# ---------------------------------------------------------------------------

def _dispatch_capture(monkeypatch, tmp_path, env_overrides):
    captured = {}
    monkeypatch.setattr(b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env) or _FakePopen(11))
    for k in _TRANSPORT_OLD + _TRANSPORT_NEW + _REAL:
        monkeypatch.delenv(k, raising=False)
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    b.OllamaDriver().dispatch("do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read", cwd=tmp_path,
        log_path=tmp_path / "agent.log", append=False)
    return captured["env"]


def test_dispatch_writes_both_old_and_new_keys_for_all_three(monkeypatch, tmp_path):
    """All three transports must be written under BOTH the legacy LOCAL_AGENT_*
    and the new PIPELINE_TRANSPORT_* names, with identical values."""
    env = _dispatch_capture(monkeypatch, tmp_path, {
        "PIPELINE_LOCAL_MAX_STEPS": "7",
        "PIPELINE_LOCAL_NUM_CTX": "4096",
        "PIPELINE_LOCAL_TEMPERATURE": "0.2",
    })
    pairs = [
        ("LOCAL_AGENT_MAX_STEPS", "PIPELINE_TRANSPORT_MAX_STEPS"),
        ("LOCAL_AGENT_NUM_CTX", "PIPELINE_TRANSPORT_NUM_CTX"),
        ("LOCAL_AGENT_TEMPERATURE", "PIPELINE_TRANSPORT_TEMPERATURE"),
    ]
    for old, new in pairs:
        assert old in env, f"missing legacy key {old}"
        assert new in env, f"missing new transport key {new}"
        assert env[old] == env[new], (
            f"{old}={env[old]!r} != {new}={env[new]!r}"
        )


def test_dispatch_defaults_flow_through_when_real_knobs_unset(monkeypatch, tmp_path):
    """Boundary: when no PIPELINE_LOCAL_* knob is set, the dispatch env must
    still carry both old and new keys, populated from the constructor defaults
    (max_steps default 40, num_ctx default 16384, temperature default 0.3)."""
    env = _dispatch_capture(monkeypatch, tmp_path, {})
    # Both names present and equal even on the default path.
    assert env["LOCAL_AGENT_MAX_STEPS"] == env["PIPELINE_TRANSPORT_MAX_STEPS"]
    assert env["LOCAL_AGENT_NUM_CTX"] == env["PIPELINE_TRANSPORT_NUM_CTX"]
    assert env["LOCAL_AGENT_TEMPERATURE"] == env["PIPELINE_TRANSPORT_TEMPERATURE"]
    # The default max_steps is 40 (see OllamaDriver.__init__).
    assert env["PIPELINE_TRANSPORT_MAX_STEPS"] == "40"
    assert env["LOCAL_AGENT_MAX_STEPS"] == "40"


def test_dispatch_max_steps_boundary_one(monkeypatch, tmp_path):
    """Boundary value: max_steps of 1 (minimum meaningful step cap) must round
    trip through both transport names as the string '1'."""
    env = _dispatch_capture(monkeypatch, tmp_path, {"PIPELINE_LOCAL_MAX_STEPS": "1"})
    assert env["PIPELINE_TRANSPORT_MAX_STEPS"] == "1"
    assert env["LOCAL_AGENT_MAX_STEPS"] == "1"


def test_dispatch_temperature_zero(monkeypatch, tmp_path):
    """Boundary value: temperature 0.0 must round trip through both names. Zero
    is a valid (greedy) temperature and must not be dropped or coerced."""
    env = _dispatch_capture(monkeypatch, tmp_path, {"PIPELINE_LOCAL_TEMPERATURE": "0"})
    assert env["PIPELINE_TRANSPORT_TEMPERATURE"] == "0"
    assert env["LOCAL_AGENT_TEMPERATURE"] == "0"


def test_dispatch_num_ctx_large(monkeypatch, tmp_path):
    """Boundary value: a large num_ctx must round trip through both names
    unchanged (no truncation / int reformatting that loses the value)."""
    env = _dispatch_capture(monkeypatch, tmp_path, {"PIPELINE_LOCAL_NUM_CTX": "200000"})
    assert env["PIPELINE_TRANSPORT_NUM_CTX"] == "200000"
    assert env["LOCAL_AGENT_NUM_CTX"] == "200000"