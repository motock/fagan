"""Regression/edge-case companion to test_acceptance_transport_alias_readers.py.

The acceptance fixture grades the *behavior* (old vars inert, new vars
honored, defaults preserved, no dead reads, experiment runner migrated,
backend setter writes both names). This file grades the *non-behavioral*
parts of story 2's contract that a half-finished implementation can silently
violate while the acceptance suite stays green:

  1. ``TIMEOUT`` stays defined in scripts/local_agent.py. The previous attempt
     at this rename corrupted the file with a str_replace that deleted the
     neighbouring ``TIMEOUT = float(...)`` line; the module still imported,
     so nothing failed until 39 tests hit ``NameError: name 'TIMEOUT' is not
     defined`` at runtime. This guard fails at *import* time, before that
     class of regression can ship.
  2. The three renamed reads are gone with NO fallback to the old name
     (``or os.environ.get("LOCAL_AGENT_...")``). A fallback defeats the
     deprecation and the acceptance fixture's "old var is inert" assertion
     only checks the *primary* read - a hidden fallback would leak the old
     value through a second door.
  3. Each reader's module docstring advertises the NEW PIPELINE_TRANSPORT_*
     channel (including TEMPERATURE), so operators reading the source are not
     sent to the dead names.
  4. The authorized test edit in test_local_agent_persistence.py actually
     happened: the two renamed dict keys are PIPELINE_TRANSPORT_* (the old
     keys would go inert and silently break the trim-budget test).
  5. Boundary defaults are exact per-script (MAX_STEPS 40 vs 30) and the
     untouched LOCAL_AGENT_* names (TIMEOUT, PROVIDER, NET_PROGRESS_MAX_STEPS)
     are still read from their original names - the rename is scoped to
     exactly three vars.

These tests are RED until the implementation lands, then GREEN. They never
import the implementation modules under test in a way that would mask a
missing symbol: the TIMEOUT guard execs the script fresh and probes the
module namespace directly.
"""
import importlib.util
import os
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).parent
_LA_PATH = _REPO / "scripts" / "local_agent.py"
_ORACLE_PATH = _REPO / "scripts" / "local_agent_oracle.py"

_RENAMED = ("LOCAL_AGENT_NUM_CTX", "LOCAL_AGENT_MAX_STEPS", "LOCAL_AGENT_TEMPERATURE")
_NEW = ("PIPELINE_TRANSPORT_NUM_CTX", "PIPELINE_TRANSPORT_MAX_STEPS", "PIPELINE_TRANSPORT_TEMPERATURE")
# Names that must NOT be renamed - the scope is exactly three vars.
_UNTOUCHED = ("LOCAL_AGENT_TIMEOUT", "LOCAL_AGENT_PROVIDER", "LOCAL_AGENT_NET_PROGRESS_MAX_STEPS")


def _load(path, mod_name):
    os.environ["LOCAL_AGENT_MODEL"] = "test-model"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def _clean(monkeypatch):
    for k in _RENAMED + _NEW:
        monkeypatch.delenv(k, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. TIMEOUT stays defined (the corruption regression guard)
# ---------------------------------------------------------------------------

def test_local_agent_timeout_still_defined(_clean):
    """TIMEOUT must remain a module-level name in local_agent.py.

    A previous str_replace edit deleted the ``TIMEOUT = float(...)`` line.
    The module still imported, so collection passed, but 39 runtime tests
    blew up with NameError. Fail here at import time instead.
    """
    la = _load(_LA_PATH, "la_timeout_guard")
    assert hasattr(la, "TIMEOUT"), "scripts/local_agent.py lost its TIMEOUT definition"
    assert isinstance(la.TIMEOUT, float)
    assert la.TIMEOUT > 0


def test_local_agent_timeout_uses_unchanged_env_name(_clean):
    """TIMEOUT is NOT one of the three renamed vars - it must still read
    LOCAL_AGENT_TIMEOUT, not a PIPELINE_TRANSPORT_* name."""
    la = _load(_LA_PATH, "la_timeout_name")
    # default 900s per the existing source
    assert la.TIMEOUT == pytest.approx(900.0)


def test_oracle_timeout_still_defined(_clean):
    lao = _load(_ORACLE_PATH, "lao_timeout_guard")
    assert hasattr(lao, "TIMEOUT"), "scripts/local_agent_oracle.py lost its TIMEOUT definition"
    assert isinstance(lao.TIMEOUT, float)
    assert lao.TIMEOUT > 0


# ---------------------------------------------------------------------------
# 2. No fallback to the old name (the deprecation must be inert, not a back door)
# ---------------------------------------------------------------------------

def test_no_fallback_to_old_transport_name_in_local_agent():
    """The renamed reads must be a plain ``os.environ.get("PIPELINE_TRANSPORT_..")``
    with no ``or os.environ.get("LOCAL_AGENT_..")`` back door. A fallback
    would let the dead name leak its value and defeat the deprecation."""
    text = _LA_PATH.read_text()
    for old in _RENAMED:
        # any occurrence of the old name inside an environ.get / or-chain
        pattern = rf'environ\.get\(\s*["\']?{re.escape(old)}["\']?'
        assert not re.search(pattern, text), (
            f"local_agent.py still references {old} (fallback or leftover read)")


def test_no_fallback_to_old_transport_name_in_oracle():
    text = _ORACLE_PATH.read_text()
    for old in _RENAMED:
        pattern = rf'environ\.get\(\s*["\']?{re.escape(old)}["\']?'
        assert not re.search(pattern, text), (
            f"local_agent_oracle.py still references {old} (fallback or leftover read)")


# ---------------------------------------------------------------------------
# 3. Module docstrings advertise the new channel (including TEMPERATURE)
# ---------------------------------------------------------------------------

def test_local_agent_docstring_lists_new_transport_names():
    """The config docstring must mention the NEW PIPELINE_TRANSPORT_* names so
    operators are not sent to the dead LOCAL_AGENT_* names. TEMPERATURE is
    explicitly required - an earlier draft of the rename doc missed it."""
    doc = _LA_PATH.read_text().split('"""')[1]
    for new in _NEW:
        assert new in doc, f"local_agent.py docstring missing {new}"


def test_oracle_docstring_mentions_transport_channel():
    """The oracle docstring need not enumerate every var, but it must not
    advertise the three DEAD names as the config channel for NUM_CTX /
    MAX_STEPS / TEMPERATURE. We assert the dead names are absent from the
    docstring's config description (they may appear elsewhere describing the
    *acceptance* list, which is a different, untouched var)."""
    doc = _ORACLE_PATH.read_text().split('"""')[1]
    # The three renamed vars must not be presented as the transport channel.
    for old in _RENAMED:
        assert old not in doc, (
            f"local_agent_oracle.py docstring still advertises dead {old}")


# ---------------------------------------------------------------------------
# 4. Authorized test edit in test_local_agent_persistence.py
# ---------------------------------------------------------------------------

def test_persistence_test_uses_new_transport_keys():
    """test_main_trims_oversized_resumed_transcript_before_first_chat sets a
    tiny trim budget via env. After the rename the OLD keys go inert, so the
    authorized edit must switch the two dict KEYS to PIPELINE_TRANSPORT_*.
    Values and assertions are unchanged - we only check the keys moved."""
    text = (_REPO / "test_local_agent_persistence.py").read_text()
    # The two keys that force the tiny budget must be the NEW names.
    assert '"PIPELINE_TRANSPORT_NUM_CTX": "256"' in text, (
        "test_local_agent_persistence.py must set PIPELINE_TRANSPORT_NUM_CTX=256")
    assert '"PIPELINE_TRANSPORT_MAX_STEPS": "1"' in text, (
        "test_local_agent_persistence.py must set PIPELINE_TRANSPORT_MAX_STEPS=1")
    # The dead names must not be used to configure the trim budget anywhere
    # in that test's env dict.
    assert '"LOCAL_AGENT_NUM_CTX": "256"' not in text, "dead NUM_CTX key still present"
    assert '"LOCAL_AGENT_MAX_STEPS": "1"' not in text, "dead MAX_STEPS key still present"


# ---------------------------------------------------------------------------
# 5. Boundary defaults + scope (exactly three vars renamed, defaults differ)
# ---------------------------------------------------------------------------

def test_local_agent_defaults_preserved(_clean):
    la = _load(_LA_PATH, "la_defaults")
    assert la.NUM_CTX == 16384
    assert la.MAX_STEPS == 40
    assert la.TEMPERATURE == pytest.approx(0.3)


def test_oracle_defaults_preserved(_clean):
    lao = _load(_ORACLE_PATH, "lao_defaults")
    assert lao.NUM_CTX == 16384
    assert lao.MAX_STEPS == 30  # oracle differs from local_agent (40)
    assert lao.TEMPERATURE == pytest.approx(0.3)


def test_untouched_local_agent_names_still_read(_clean):
    """The rename is scoped to exactly NUM_CTX/MAX_STEPS/TEMPERATURE.
    TIMEOUT, PROVIDER and NET_PROGRESS_MAX_STEPS must still read their
    original LOCAL_AGENT_* names."""
    text = _LA_PATH.read_text()
    for name in _UNTOUCHED:
        assert f'environ.get("{name}"' in text or f"environ.get('{name}'" in text, (
            f"local_agent.py stopped reading untouched {name}")


def test_untouched_local_agent_names_still_read_oracle(_clean):
    text = _ORACLE_PATH.read_text()
    for name in _UNTOUCHED:
        assert f'environ.get("{name}"' in text or f"environ.get('{name}'" in text, (
            f"local_agent_oracle.py stopped reading untouched {name}")


# ---------------------------------------------------------------------------
# Boundary: new var set to edge values is honored (zero/one/min parsing)
# ---------------------------------------------------------------------------

def test_local_agent_num_ctx_one_is_honored(monkeypatch, _clean):
    monkeypatch.setenv("PIPELINE_TRANSPORT_NUM_CTX", "1")
    la = _load(_LA_PATH, "la_ctx_one")
    assert la.NUM_CTX == 1


def test_local_agent_max_steps_one_is_honored(monkeypatch, _clean):
    monkeypatch.setenv("PIPELINE_TRANSPORT_MAX_STEPS", "1")
    la = _load(_LA_PATH, "la_steps_one")
    assert la.MAX_STEPS == 1


def test_local_agent_temperature_zero_is_honored(monkeypatch, _clean):
    monkeypatch.setenv("PIPELINE_TRANSPORT_TEMPERATURE", "0.0")
    la = _load(_LA_PATH, "la_temp_zero")
    assert la.TEMPERATURE == pytest.approx(0.0)


def test_oracle_max_steps_one_is_honored(monkeypatch, _clean):
    monkeypatch.setenv("PIPELINE_TRANSPORT_MAX_STEPS", "1")
    lao = _load(_ORACLE_PATH, "lao_steps_one")
    assert lao.MAX_STEPS == 1


# ---------------------------------------------------------------------------
# Boundary: malformed new-var value raises (int()/float() parse error surfaces)
# ---------------------------------------------------------------------------

def test_local_agent_bad_num_ctx_raises(monkeypatch, _clean):
    monkeypatch.setenv("PIPELINE_TRANSPORT_NUM_CTX", "not-a-number")
    with pytest.raises(ValueError):
        _load(_LA_PATH, "la_bad_ctx")


def test_local_agent_bad_temperature_raises(monkeypatch, _clean):
    monkeypatch.setenv("PIPELINE_TRANSPORT_TEMPERATURE", "nan-str")
    with pytest.raises(ValueError):
        _load(_LA_PATH, "la_bad_temp")


def test_oracle_bad_max_steps_raises(monkeypatch, _clean):
    monkeypatch.setenv("PIPELINE_TRANSPORT_MAX_STEPS", "")
    with pytest.raises(ValueError):
        _load(_ORACLE_PATH, "lao_bad_steps")