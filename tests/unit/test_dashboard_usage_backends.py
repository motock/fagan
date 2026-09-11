"""Tests for the per-backend gate rows on ``GET /api/usage`` (USE-02).

The endpoint must merge ``pipeline.usage.collect_backend_status()`` into BOTH
of its return paths under a new ``"backends"`` key, without renaming, removing
or reshaping any existing key (``static/app/usage.js`` and
``tests/unit/test_dashboard_api.py`` read them), and a failing backend probe
must never 500 the dashboard.

The collector is always stubbed here — these tests never probe a real backend.
"""

import copy
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from tests.unit._dashboard_helpers import (  # noqa: F401
    client,
    plan_dir,
)

# Synthetic collect_backend_status() rows. Shape is irrelevant to the endpoint
# (it must merge them verbatim), but keep them realistic and distinct.
BACKEND_ROWS = [
    {"backend": "local", "provider": "ollama", "reachable": True, "model": "qwen"},
    {"backend": "claude", "provider": "claude_cli", "reachable": False, "model": None},
]

# Every key the endpoint spread from the state file before this change.
ORIGINAL_STATE_KEYS = (
    "session_pct",
    "session_reset",
    "week_pct",
    "week_reset",
    "checked_at",
    "measured_at",
    "paused",
    "consecutive_parse_failures",
    "gate_blind",
    "stale",
)

FULL_STATE = {
    "session_pct": 91,
    "session_reset": "2026-06-27T09:00:00+00:00",
    "week_pct": 82,
    "week_reset": "2026-06-29T00:00:00+00:00",
    "checked_at": "2026-06-26T16:41:00+00:00",
    "measured_at": "2026-06-26T16:40:13+00:00",
    "paused": True,
    "consecutive_parse_failures": 0,
    "gate_blind": False,
    "stale": False,
}


@pytest.fixture
def usage_state_path(tmp_path, monkeypatch):
    """Point the dashboard at a per-test usage state file (same pattern as
    tests/unit/test_dashboard_api.py)."""
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(d, "USAGE_STATE_PATH", path)
    return path


@pytest.fixture
def collector_stub(monkeypatch):
    """Replace collect_backend_status with a settable fake at BOTH seams
    dashboard.py could reach it through: its own module namespace (a
    ``from pipeline.usage import collect_backend_status`` import binds the
    name there) and ``pipeline.usage`` itself (covers a
    ``from pipeline import usage`` / call-time import style). Tests set
    ``collector_stub["rows"]`` or ``collector_stub["exc"]`` to control it.
    """
    from pipeline import usage as pipeline_usage

    calls: list[dict] = []
    state: dict = {"rows": [], "exc": None}

    def _fake(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        if state["exc"] is not None:
            raise state["exc"]
        return copy.deepcopy(state["rows"])

    monkeypatch.setattr(d, "collect_backend_status", _fake, raising=False)
    monkeypatch.setattr(pipeline_usage, "collect_backend_status", _fake)
    state["calls"] = calls
    return state


# ---------------------------------------------------------------- positive


def test_usage_returns_backends_key_that_is_a_list(client, usage_state_path, collector_stub):
    collector_stub["rows"] = BACKEND_ROWS
    res = client.get("/api/usage")
    assert res.status_code == 200
    body = res.json()
    assert "backends" in body
    assert isinstance(body["backends"], list)


def test_usage_backends_contains_exactly_the_stubbed_rows(client, usage_state_path, collector_stub):
    collector_stub["rows"] = BACKEND_ROWS
    body = client.get("/api/usage").json()
    assert body["backends"] == BACKEND_ROWS
    assert len(body["backends"]) == 2


def test_usage_existing_state_keys_survive_alongside_backends(
    client, usage_state_path, collector_stub
):
    """The response is cumulative: every pre-existing key keeps its original
    value next to the new "backends" key. Asserted key-by-key on purpose —
    the shape is cumulative and later work may add more keys."""
    usage_state_path.write_text(json.dumps(FULL_STATE))
    collector_stub["rows"] = BACKEND_ROWS

    body = client.get("/api/usage").json()

    assert body["available"] is True
    for key in ORIGINAL_STATE_KEYS:
        assert key in body, f"original state key {key!r} missing from /api/usage response"
        assert body[key] == FULL_STATE[key], f"state key {key!r} was reshaped"
    assert body["backends"] == BACKEND_ROWS


def test_usage_backends_populated_even_when_no_state_file(
    client, usage_state_path, collector_stub
):
    """No Claude usage state file at all (provider-neutral install) must still
    surface the per-backend rows."""
    assert not usage_state_path.exists()
    collector_stub["rows"] = BACKEND_ROWS

    res = client.get("/api/usage")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["backends"] == BACKEND_ROWS
    assert len(body["backends"]) == 2


# ------------------------------------------------------- negative/boundary


def test_usage_backends_empty_when_collector_returns_empty_list(
    client, usage_state_path, collector_stub
):
    collector_stub["rows"] = []
    res = client.get("/api/usage")
    assert res.status_code == 200
    assert res.json()["backends"] == []


def test_usage_survives_collector_raising(client, usage_state_path, collector_stub):
    """A diagnostic banner must never 500 the dashboard: if the collector
    blows up the endpoint degrades to ``backends: []`` with a 200."""
    usage_state_path.write_text(json.dumps(FULL_STATE))
    collector_stub["exc"] = RuntimeError("backend probe exploded")

    res = client.get("/api/usage")
    assert res.status_code == 200
    body = res.json()
    assert body["backends"] == []
    # the state keys are unaffected by the collector failure
    assert body["available"] is True
    assert body["session_pct"] == 91


def test_usage_calls_collector_exactly_once_per_request(
    client, usage_state_path, collector_stub
):
    """The endpoint only calls the aggregation and merges the result — it
    must not re-implement or repeatedly re-probe it."""
    collector_stub["rows"] = BACKEND_ROWS
    client.get("/api/usage")
    assert len(collector_stub["calls"]) == 1


def test_usage_corrupt_state_file_behaviour_is_preserved(client, usage_state_path, collector_stub):
    """Preservation pin, per the brief: BEFORE this change a corrupt usage
    state file makes the endpoint raise ``json.JSONDecodeError`` (a bare
    ``json.loads`` on the state file), i.e. a 500 on a real server. That
    behaviour must be kept — only the ``collect_backend_status()`` call is
    wrapped in try/except, not the state-file parse."""
    usage_state_path.write_text("{not valid json")

    lenient = TestClient(d.app, raise_server_exceptions=False)
    assert lenient.get("/api/usage").status_code == 500

    with pytest.raises(
        json.JSONDecodeError, match="Expecting property name enclosed in double quotes"
    ):
        TestClient(d.app).get("/api/usage")


# --------------------------------------------------- source-level contracts


def test_dashboard_imports_collect_backend_status_from_pipeline_usage():
    """The aggregation must come from pipeline.usage (USE-01), imported at
    module level alongside the existing pipeline imports — not redefined and
    not imported twice."""
    src = Path(d.__file__).read_text()
    imports = re.findall(
        r"^from pipeline\.usage import collect_backend_status\b.*$",
        src,
        re.MULTILINE,
    )
    assert len(imports) == 1, (
        "app/dashboard.py must import collect_backend_status from pipeline.usage "
        "exactly once at module level"
    )
    # the aggregation lives in pipeline/usage.py; no new logic belongs here
    assert "def collect_backend_status" not in src


def test_usage_route_defined_exactly_once():
    src = Path(d.__file__).read_text()
    assert len(re.findall(r"^def usage\(", src, re.MULTILINE)) == 1
    hits = [r for r in d.app.routes if getattr(r, "path", None) == "/api/usage"]
    assert len(hits) == 1