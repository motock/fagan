"""Acceptance fixture (story 3, cleanup): finish the two pieces of story 2's
brief that a prior attempt left undone (merged green-but-incomplete as PR
#200 - see Mode 47 in project memory). Both pieces are non-behavioral (no
test failed before this story), which is exactly why they need their own
explicit assertions: a suite-gate cannot see an ungraded requirement.

On current master (post-#200) this file FAILS both tests:
  - the local_agent.py docstring still names the OLD LOCAL_AGENT_MAX_STEPS /
    LOCAL_AGENT_TEMPERATURE as the config channel (only NUM_CTX was updated)
  - test_local_agent_persistence.py still sets the dead
    "LOCAL_AGENT_MAX_STEPS" key (only the NUM_CTX key was migrated)
"""
from pathlib import Path

_REPO = Path(__file__).parent
_LA_PATH = _REPO / "scripts" / "local_agent.py"
_PERSISTENCE_PATH = _REPO / "test_local_agent_persistence.py"
_NEW = ("PIPELINE_TRANSPORT_NUM_CTX", "PIPELINE_TRANSPORT_MAX_STEPS", "PIPELINE_TRANSPORT_TEMPERATURE")


def test_local_agent_docstring_lists_all_new_transport_names():
    """The config docstring must name ALL THREE new PIPELINE_TRANSPORT_* vars,
    not just the one a prior attempt happened to migrate (NUM_CTX)."""
    doc = _LA_PATH.read_text().split('"""')[1]
    missing = [name for name in _NEW if name not in doc]
    assert not missing, f"local_agent.py docstring missing: {missing}"


def test_persistence_test_has_no_dead_transport_keys():
    """test_local_agent_persistence.py must not set the transport vars through
    any of the three OLD dict keys anymore - PIPELINE_TRANSPORT_NUM_CTX was
    migrated already; PIPELINE_TRANSPORT_MAX_STEPS and _TEMPERATURE (if
    present) must be migrated too, with no dead LOCAL_AGENT_* key left in the
    env dict that drives scripts/local_agent.py."""
    text = _PERSISTENCE_PATH.read_text()
    assert '"PIPELINE_TRANSPORT_NUM_CTX": "256"' in text, (
        "must still set PIPELINE_TRANSPORT_NUM_CTX=256 (do not regress this)")
    assert '"PIPELINE_TRANSPORT_MAX_STEPS": "1"' in text, (
        "must set PIPELINE_TRANSPORT_MAX_STEPS=1 (the dead LOCAL_AGENT_MAX_STEPS key must move)")
    assert '"LOCAL_AGENT_MAX_STEPS": "1"' not in text, (
        "dead LOCAL_AGENT_MAX_STEPS key still present")
    assert '"LOCAL_AGENT_NUM_CTX": "256"' not in text, (
        "dead LOCAL_AGENT_NUM_CTX key still present (regression check)")
