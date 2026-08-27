"""Spec tests for story 3 (TRANSPORT-ALIAS-CLEANUP).

Story 2 (PR #200) migrated the *reads* in scripts/local_agent.py to
PIPELINE_TRANSPORT_* but left two stale pieces: the module docstring still
advertised the OLD LOCAL_AGENT_* names for the three transport channel vars,
and test_local_agent_persistence.py still set a dead ``LOCAL_AGENT_MAX_STEPS``
key in one env dict. This file grades EVERY mechanically-checkable piece of
that cleanup so a future implementer cannot do a minimum edit and stop.

These tests read source text only (no behavior) - they assert on the literal
contents of scripts/local_agent.py and test_local_agent_persistence.py. They
are intentionally redundant with test_acceptance_transport_alias_cleanup.py
but cover the requirements that fixture leaves ungraded:
  - the OLD transport names must be GONE from the docstring (not just the new
    ones present)
  - the six never-renamed LOCAL_AGENT_* vars must STILL be named in the
    docstring (an over-eager edit could delete them)
  - the production reads must keep using PIPELINE_TRANSPORT_* and must NOT
    read the old LOCAL_AGENT_* transport names via os.environ.get
"""
from pathlib import Path

_REPO = Path(__file__).parent.parent.parent
_LA_PATH = _REPO / "scripts" / "local_agent.py"
_LA_CONFIG_PATH = _REPO / "scripts" / "local_agent_config.py"
_PERSISTENCE_PATH = _REPO / "tests" / "unit" / "test_local_agent_persistence.py"

# The three transport-channel vars that story 2 renamed.
_NEW_TRANSPORT = (
    "PIPELINE_TRANSPORT_NUM_CTX",
    "PIPELINE_TRANSPORT_MAX_STEPS",
    "PIPELINE_TRANSPORT_TEMPERATURE",
)
_OLD_TRANSPORT = (
    "LOCAL_AGENT_NUM_CTX",
    "LOCAL_AGENT_MAX_STEPS",
    "LOCAL_AGENT_TEMPERATURE",
)
# Vars that were NEVER renamed and must remain in the docstring untouched.
_PRESERVED = (
    "LOCAL_AGENT_SYSTEM",
    "LOCAL_AGENT_TASK",
    "LOCAL_AGENT_MODEL",
    "LOCAL_AGENT_ENDPOINT",
    "LOCAL_AGENT_TIMEOUT",
    "LOCAL_AGENT_PROVIDER",
)


def _docstring() -> str:
    """Return the first (module) docstring of scripts/local_agent.py."""
    return _LA_PATH.read_text().split('"""')[1]


# --------------------------------------------------------------------------- #
# Docstring: new names present, old names gone, preserved names untouched.
# --------------------------------------------------------------------------- #


def test_docstring_names_all_three_new_transport_vars():
    doc = _docstring()
    missing = [name for name in _NEW_TRANSPORT if name not in doc]
    assert not missing, f"docstring missing new transport names: {missing}"


def test_docstring_no_longer_names_old_transport_vars():
    doc = _docstring()
    leftover = [name for name in _OLD_TRANSPORT if name in doc]
    assert not leftover, (
        f"docstring still names old transport vars (must be removed): {leftover}")


def test_docstring_still_names_preserved_local_agent_vars():
    """The six vars that were never renamed must remain in the docstring."""
    doc = _docstring()
    missing = [name for name in _PRESERVED if name not in doc]
    assert not missing, (
        f"docstring dropped a never-renamed var (must stay): {missing}")


# --------------------------------------------------------------------------- #
# Production reads: os.environ.get must use the new names, not the old.
# --------------------------------------------------------------------------- #


def test_production_reads_use_new_transport_names():
    # 2026-08-27: the transport constants moved from local_agent.py into the
    # sibling scripts/local_agent_config.py (part of splitting local_agent.py
    # under the 1,000-line guideline) - the read now lives there, imported
    # back into local_agent.py, so scan both files rather than just the one
    # this test originally graded.
    src = _LA_PATH.read_text() + _LA_CONFIG_PATH.read_text()
    for name in _NEW_TRANSPORT:
        assert f'os.environ.get("{name}"' in src or (
            f"os.environ.get('{name}'" in src), (
            f"local_agent.py (or its local_agent_config.py split) must read {name} via os.environ.get")


def test_production_does_not_read_old_transport_names():
    src = _LA_PATH.read_text()
    for name in _OLD_TRANSPORT:
        assert f'os.environ.get("{name}"' not in src, (
            f"local_agent.py must not read old {name} via os.environ.get")
        assert f"os.environ.get('{name}'" not in src, (
            f"local_agent.py must not read old {name} via os.environ.get")


# --------------------------------------------------------------------------- #
# Persistence test file: dead key migrated, no stale LOCAL_AGENT_* transport
# keys remain, value preserved.
# --------------------------------------------------------------------------- #


def test_persistence_sets_pipeline_transport_num_ctx_256():
    text = _PERSISTENCE_PATH.read_text()
    assert '"PIPELINE_TRANSPORT_NUM_CTX": "256"' in text


def test_persistence_sets_pipeline_transport_max_steps_1():
    text = _PERSISTENCE_PATH.read_text()
    assert '"PIPELINE_TRANSPORT_MAX_STEPS": "1"' in text


def test_persistence_has_no_dead_local_agent_max_steps():
    text = _PERSISTENCE_PATH.read_text()
    assert '"LOCAL_AGENT_MAX_STEPS": "1"' not in text, (
        "dead LOCAL_AGENT_MAX_STEPS key still present in persistence test")


def test_persistence_has_no_dead_local_agent_num_ctx():
    text = _PERSISTENCE_PATH.read_text()
    assert '"LOCAL_AGENT_NUM_CTX": "256"' not in text, (
        "dead LOCAL_AGENT_NUM_CTX key still present in persistence test")


def test_persistence_has_no_dead_local_agent_temperature():
    """If the persistence test ever set the temperature key it must use the
    new name too; assert no stale LOCAL_AGENT_TEMPERATURE dict key remains."""
    text = _PERSISTENCE_PATH.read_text()
    assert '"LOCAL_AGENT_TEMPERATURE"' not in text, (
        "dead LOCAL_AGENT_TEMPERATURE key still present in persistence test")