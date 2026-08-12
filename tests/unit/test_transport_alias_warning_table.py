"""Test suite for story 1 of 2: backend.py must WRITE the new
PIPELINE_TRANSPORT_* transport keys AND extend the module-load operator-warning
table to name the three new vars (six entries total).

Scope of THIS dispatch: test authoring only. The implementation now exists on this branch, so every test here is expected to pass.

What these tests cover (the acceptance fixture already covers the dispatch-env
setter happy path; these tests cover the rest of the contract):

  1. The module-load warning table warns EXACTLY ONCE per triggered var, for
     all six transport names (three old LOCAL_AGENT_* + three new
     PIPELINE_TRANSPORT_*).
  2. Each warning message points the operator at the correct real knob
     (PIPELINE_LOCAL_*).
  3. The dispatch env build writes ONLY the new PIPELINE_TRANSPORT_* keys; the
     legacy LOCAL_AGENT_* keys are no longer written.
  4. Setting a new PIPELINE_TRANSPORT_* var triggers the warning (negative:
     operators who set the new transport name are also warned, because it is
     still a transport-only channel, not a knob).
"""
import importlib
import logging
import sys

import pytest

from app import backend as b

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
        monkeypatch.delitem(sys.modules, "app.backend", raising=False)
        # Also drop modules backend imports at load that hold state, so the
        # reload is clean. inference_providers is a leaf dependency.
        monkeypatch.delitem(sys.modules, "app.inference_providers", raising=False)
        importlib.invalidate_caches()
        mod = importlib.import_module("app.backend")
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
# Dispatch env build: ONLY the new PIPELINE_TRANSPORT_* keys are written; the
# legacy LOCAL_AGENT_* keys are no longer written.
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


def test_dispatch_writes_only_new_transport_keys_not_legacy(monkeypatch, tmp_path):
    """The dead legacy LOCAL_AGENT_* writes were removed; the dispatch env must
    carry only the PIPELINE_TRANSPORT_* names, never the old LOCAL_AGENT_* ones."""
    env = _dispatch_capture(monkeypatch, tmp_path, {
        "PIPELINE_LOCAL_MAX_STEPS": "7",
        "PIPELINE_LOCAL_NUM_CTX": "4096",
        "PIPELINE_LOCAL_TEMPERATURE": "0.2",
    })
    for old in _TRANSPORT_OLD:
        assert old not in env, f"legacy key {old} should no longer be written"
    for new in _TRANSPORT_NEW:
        assert new in env, f"missing new transport key {new}"


def test_dispatch_defaults_flow_through_when_real_knobs_unset(monkeypatch, tmp_path):
    """Boundary: when no PIPELINE_LOCAL_* knob is set, the dispatch env must
    still carry the new PIPELINE_TRANSPORT_* keys, populated from the
    constructor defaults (max_steps default 40, num_ctx default 16384,
    temperature default 0.3), and must not carry the legacy LOCAL_AGENT_* keys."""
    env = _dispatch_capture(monkeypatch, tmp_path, {})
    for old in _TRANSPORT_OLD:
        assert old not in env, f"legacy key {old} should no longer be written"
    assert env["PIPELINE_TRANSPORT_MAX_STEPS"] == "40"


def test_dispatch_max_steps_boundary_one(monkeypatch, tmp_path):
    """Boundary value: max_steps of 1 (minimum meaningful step cap) must round
    trip through the new transport name as the string '1'."""
    env = _dispatch_capture(monkeypatch, tmp_path, {"PIPELINE_LOCAL_MAX_STEPS": "1"})
    assert env["PIPELINE_TRANSPORT_MAX_STEPS"] == "1"
    assert "LOCAL_AGENT_MAX_STEPS" not in env


def test_dispatch_temperature_zero(monkeypatch, tmp_path):
    """Boundary value: temperature 0.0 must round trip through the new
    transport name. Zero is a valid (greedy) temperature and must not be
    dropped or coerced."""
    env = _dispatch_capture(monkeypatch, tmp_path, {"PIPELINE_LOCAL_TEMPERATURE": "0"})
    assert env["PIPELINE_TRANSPORT_TEMPERATURE"] == "0"
    assert "LOCAL_AGENT_TEMPERATURE" not in env


def test_dispatch_num_ctx_large(monkeypatch, tmp_path):
    """Boundary value: a large num_ctx must round trip through the new
    transport name unchanged (no truncation / int reformatting that loses the
    value)."""
    env = _dispatch_capture(monkeypatch, tmp_path, {"PIPELINE_LOCAL_NUM_CTX": "200000"})
    assert env["PIPELINE_TRANSPORT_NUM_CTX"] == "200000"
    assert "LOCAL_AGENT_NUM_CTX" not in env


# ---------------------------------------------------------------------------
# Out-of-scope guards: the story forbids touching unrelated LOCAL_AGENT_*
# keys (MODEL, SYSTEM, THINK) and the module-load warning table. These tests
# pin those so the implementer cannot accidentally remove them.
# ---------------------------------------------------------------------------

def test_dispatch_still_writes_other_local_agent_keys(monkeypatch, tmp_path):
    """The story removes ONLY the three transport legacy keys (MAX_STEPS,
    NUM_CTX, TEMPERATURE). The unrelated, still-real LOCAL_AGENT_* keys
    (MODEL, SYSTEM, THINK) must remain in the dispatch env untouched."""
    env = _dispatch_capture(
        monkeypatch, tmp_path, {"PIPELINE_LOCAL_THINK": "true"}
    )
    assert "LOCAL_AGENT_MODEL" in env, (
        "LOCAL_AGENT_MODEL is out of scope and must still be written"
    )
    assert "LOCAL_AGENT_SYSTEM" in env, (
        "LOCAL_AGENT_SYSTEM is out of scope and must still be written"
    )
    assert "LOCAL_AGENT_THINK" in env, (
        "LOCAL_AGENT_THINK is out of scope and must still be written"
    )


def test_warning_table_still_names_legacy_transport_vars(monkeypatch):
    """The module-load operator-warning table (a list of (old_name, real_name)
    pairs) is explicitly OUT OF SCOPE and must not be removed or altered. It
    must still warn when a legacy LOCAL_AGENT_* transport var is set directly
    in the environment, naming the real knob."""
    for old, real in (
        ("LOCAL_AGENT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
        ("LOCAL_AGENT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
        ("LOCAL_AGENT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ):
        _mod, records = _reload_backend_with_env(monkeypatch, {old: "x"})
        msg = next((m for m in records if old in m), None)
        assert msg is not None, (
            f"warning table must still warn for {old}; got {records}"
        )
        assert real in msg, (
            f"warning for {old} must still name real knob {real}; got {msg}"
        )


def test_backend_source_has_no_legacy_transport_env_writes():
    """Source-level guard: app/backend.py must no longer assign the three dead
    legacy transport keys into the dispatch env. This catches a partial revert
    or a copy-paste reintroduction directly in the source, independent of
    runtime behavior."""
    src = _backend_source()
    for old in _TRANSPORT_OLD:
        needle = f'env["{old}"]'
        assert needle not in src, (
            f"app/backend.py must not write the dead legacy key {old} into the "
            f"dispatch env; found {needle!r}"
        )


def test_backend_source_still_writes_new_transport_env_keys():
    """Source-level guard: app/backend.py must still assign the three
    PIPELINE_TRANSPORT_* keys into the dispatch env (the story removes only the
    legacy duplicates, not the real writes)."""
    src = _backend_source()
    for new in _TRANSPORT_NEW:
        needle = f'env["{new}"]'
        assert needle in src, (
            f"app/backend.py must still write {new} into the dispatch env; "
            f"missing {needle!r}"
        )


def test_backend_source_still_writes_other_local_agent_env_keys():
    """Source-level guard: the out-of-scope LOCAL_AGENT_* keys (MODEL, SYSTEM,
    THINK) must still be assigned into the dispatch env in app/backend.py.
    MODEL and SYSTEM are written via a dict literal; THINK via env[...]."""
    src = _backend_source()
    assert '"LOCAL_AGENT_MODEL":' in src, (
        "app/backend.py must still write LOCAL_AGENT_MODEL into the dispatch env"
    )
    assert '"LOCAL_AGENT_SYSTEM":' in src, (
        "app/backend.py must still write LOCAL_AGENT_SYSTEM into the dispatch env"
    )
    assert 'env["LOCAL_AGENT_THINK"]' in src, (
        "app/backend.py must still write LOCAL_AGENT_THINK into the dispatch env"
    )


def test_backend_source_warning_table_still_lists_legacy_pairs():
    """Semantic guard: the module-load operator-warning table must still cover
    the three legacy (old, real) pairs, wherever the canonical list now lives.
    Originally a source-level grep of app/backend.py's own text; updated
    2026-08-07 (w3a-effective-config-provenance, story f7fd39c4) when the
    canonical (legacy, real) pairs moved to a single shared definition,
    `pipeline.config_provenance.IGNORED_ENV_VARS`, imported by app/backend.py
    rather than duplicated inline - the guard's real intent (don't lose these
    pairs) is preserved by checking the pairs are still present on the
    object app/backend.py actually iterates, not by grepping raw source text
    that no longer contains them by design."""
    for old, real in (
        ("LOCAL_AGENT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
        ("LOCAL_AGENT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
        ("LOCAL_AGENT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ):
        assert (old, real) in b.IGNORED_ENV_VARS, (
            f"warning table must still cover legacy pair ({old}, {real})"
        )


def _backend_source():
    from pathlib import Path

    here = Path(__file__).resolve()
    # tests/unit/<file> -> repo root / app / backend_ollama.py (dispatch()'s
    # env writes live here, not app/backend.py, since the OllamaDriver
    # extraction)
    backend = here.parents[2] / "app" / "backend_ollama.py"
    return backend.read_text()
