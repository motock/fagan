"""Hermeticity for the integration tier: isolate the usage meter.

The Claude backend's resource gate reads ``pipeline.paths.USAGE_STATE_PATH`` —
by default the developer's own ``~/.claude/usage_state.json``, which the real
usage poller rewrites about once a minute. When the live session usage sits at
or above ``PIPELINE_PAUSE_THRESHOLD``, ``_role_resource_ok("dispatch")``
returns ``(False, "Claude usage gate tripped")``; every scheduler tick then
returns ``dispatch_paused`` and records no dispatch decision, so a test that
advances a plan sees an empty audit stream and fails on a correct codebase.

That is a machine-local external resource, not the subject of any test in this
tier — none of them reference the usage state. The unit tier isolates exactly
this (``tests/unit/_isolate_usage_state``) for exactly this reason; this tier
has the same dependency and no isolation of its own, so a green suite on one
machine and a red one on another said nothing about the code under test.

The isolated state file is left absent, which the gate reads as "not paused" —
the fail-open behavior ``check_usage`` documents for missing or garbled state.
The gate itself stays real and unmodified; only the machine's live reading is
withheld.

``USAGE_STATE_PATH`` is set in the environment as well as on the modules: this
tier runs real child processes (``PipelineService().advance_all_plans()`` forks
one per plan) and a child re-imports ``pipeline.paths``, re-reading the env at
import time. Patching only the parent's already-bound names would leave the
children on the developer's live meter.
"""

import pytest

import pipeline.server as _server
from pipeline import paths as _paths
from pipeline import usage as _usage

#: Every module that binds ``USAGE_STATE_PATH`` at import time and is read
#: afterwards by the dispatch gate (the unit tier patches both of these too).
_USAGE_STATE_BINDINGS = (
    (_server, "USAGE_STATE_PATH"),
    (_paths, "USAGE_STATE_PATH"),
    (_usage, "USAGE_STATE_PATH"),
)


@pytest.fixture(autouse=True)
def _isolate_usage_state(tmp_path, monkeypatch):
    """Point the usage gate at an isolated, absent state file for every test."""
    state_path = tmp_path / "usage_state.json"
    # The env var covers child processes that re-import pipeline.paths.
    monkeypatch.setenv("USAGE_STATE_PATH", str(state_path))
    for module, name in _USAGE_STATE_BINDINGS:
        monkeypatch.setattr(module, name, state_path, raising=False)
    return state_path
