"""Plane reachability self-silencing for the optional ticketing integration.

The TicketProvider abstraction already makes Plane *configurable* to off
(``PIPELINE_TICKET_PROVIDER=none``) and skips it when its env vars are absent
(``_plane_enabled``). What it did not do before this change is stay quiet when
Plane is *configured but down*: every ``set_state`` (dispatch -> in-progress,
advance/approve_merge -> done) fired ``PLANE_MAX_ATTEMPTS`` HTTP requests at a
dead endpoint, then wrote a "Plane sync ... failed" line to the plan's
notifications log - on every single orchestration tick, forever.

These tests pin the new contract: a *transport-level* failure (connection
refused, timeout - the server is not there) silences Plane for the rest of the
process after exactly one warning, while a *runtime* failure (the server IS
responding but returned an error) keeps the existing retry-then-notify
budget untouched. The distinction is ``httpx.TransportError`` (host down) vs
the ``RuntimeError`` ``plane_request`` raises for a non-2xx response (host
up, API error) - so the two paths cannot be confused.

Run with: cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q
"""

import httpx
import pytest

from pipeline import ticketing as pt


# ---------- Fixtures ----------
# Mirror the autouse fixtures in test_pipeline_mcp_server.py so this file is
# isolated from any module state left by other test files: plane "configured"
# (env vars present, as the real ~/.claude.json leaves it), caches cleared,
# and the process-level reachability verdict reset to "unknown" per test.
@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")


@pytest.fixture(autouse=True)
def _reset_ticketing_state():
    pt._state_cache.clear()
    pt._label_cache.clear()
    pt._plane_reachable = None
    yield
    pt._plane_reachable = None


# ---------- Transport failure (host down) -> self-silence ----------
def test_plane_set_state_silences_on_transport_error(monkeypatch, capsys):
    """A connection-refused (transport) error on the first set_state marks
    Plane unreachable and returns True (best-effort no-op success), rather
    than exhausting the retry budget and notifying the plan log."""
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")

    calls = []

    def _down(method, path, **kw):
        calls.append(path)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(pt, "plane_request", _down)

    assert pt._plane_set_state("S1", "started") is True
    # One transport failure is enough to know the host is not there: do NOT
    # burn the retry budget hammering a dead endpoint.
    assert len(calls) == 1
    assert pt._plane_reachable is False
    # The drop is surfaced once, not swallowed forever.
    assert "Warning" in capsys.readouterr().out


def test_plane_set_state_silenced_short_circuits_future_calls(monkeypatch, capsys):
    """Once Plane has been found unreachable, subsequent set_state calls must
    not fire any HTTP at all (no per-tick connection-refused spam) and must
    not notify the plan log (no per-tick notifications.log spam)."""
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")

    calls = []

    def _down(method, path, **kw):
        calls.append(path)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(pt, "plane_request", _down)

    # First call: one failed HTTP, one warning, mark down.
    assert pt._plane_set_state("A", "started") is True
    assert len(calls) == 1
    capsys.readouterr()  # drain the detection warning

    # Second + third calls: no HTTP, no warning, no notify - silent no-ops.
    notify = []
    monkeypatch.setattr(pt, "_notify_user",
                        lambda plan, msg, **kwargs: notify.append(msg),
                        raising=False)
    # _notify_user lives on the server module; _plane_set_state imports it
    # lazily, so patch the server attribute the lazy import resolves to.
    import pipeline.server as pserver
    monkeypatch.setattr(
        pserver, "_notify_user", lambda plan, msg, **kwargs: notify.append(msg)
    )

    assert pt._plane_set_state("B", "completed", plan_name="pl") is True
    assert pt._plane_set_state("C", "completed", plan_name="pl") is True
    assert len(calls) == 1  # unchanged - no further HTTP
    assert notify == []     # no plan-log spam
    assert capsys.readouterr().out == ""


def test_plane_set_state_transport_error_takes_priority_over_retry_budget(
    monkeypatch,
):
    """Even if PLANE_MAX_ATTEMPTS is large, a transport error stops after one
    attempt - retrying a dead host is pure latency, not a recovery strategy."""
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 10)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")

    calls = []

    def _down(method, path, **kw):
        calls.append(path)
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(pt, "plane_request", _down)

    assert pt._plane_set_state("S1", "completed") is True
    assert len(calls) == 1
    assert pt._plane_reachable is False


# ---------- Runtime failure (host up, API error) -> unchanged retry/notify ----------
def test_plane_set_state_runtime_error_still_retries_and_notifies(monkeypatch):
    """A non-transport error (RuntimeError from plane_request for a non-2xx
    response) means the server IS responding - it must NOT trigger
    silencing. The existing retry-then-notify budget applies unchanged.
    Regression guard: the transport-error fast path must not swallow the
    retryable-failure path that the main suite already pins."""
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("502 bad gateway")))

    notify = []
    import pipeline.server as pserver
    monkeypatch.setattr(
        pserver, "_notify_user", lambda plan, msg, **kwargs: notify.append(msg)
    )

    result = pt._plane_set_state("S1", "completed", plan_name="pl")
    # Existing behavior: exhaust the budget, return False, notify once.
    assert result is False
    assert len(notify) == 1
    assert "502 bad gateway" in notify[0]
    # A responding server must NOT be silenced.
    assert pt._plane_reachable is None


def test_plane_set_state_retries_then_succeeds_under_runtime_error(monkeypatch):
    """A transient RuntimeError (server responding, transient 502) is still
    retried within budget to success - the transport-only fast path does not
    change the retry semantics for reachable servers."""
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")

    calls = []

    def _flaky(method, path, **kw):
        calls.append(path)
        if len(calls) < 2:
            raise RuntimeError("502")
        return {}

    monkeypatch.setattr(pt, "plane_request", _flaky)
    assert pt._plane_set_state("S1", "started") is True
    assert len(calls) == 2
    assert pt._plane_reachable is None