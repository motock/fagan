"""Regression tests for review Blocking 1: ``/api/health`` plan_dir.

``app/dashboard.py``'s ``/api/health`` route returns ``str(PLAN_DIR)`` where
``PLAN_DIR`` is now a :class:`pipeline.live_ref.LiveRef`. Before LiveRef grew
``__str__``/``__repr__``/``__fspath__`` forwarding, ``str(ref)`` resolved
``object.__str__`` on the TYPE (implicit dunder lookup never consults
``__getattr__``, which only fires when normal lookup fails), so the endpoint
leaked ``"<pipeline.live_ref.LiveRef object at 0x...>"`` instead of the plan
directory path.

These tests deliberately patch ONLY ``pipeline.server.PLAN_DIR`` and leave the
dashboard's LiveRef binding in place: the shared ``plan_dir`` fixture from
``tests.unit._dashboard_helpers`` rebinds ``app.dashboard.PLAN_DIR`` to a plain
Path, which would shadow the ref and mask exactly this bug.

The re-patch halfway through proves the endpoint reads the ref's CURRENT
target on every call (no snapshot/memoization): state persists across calls,
so a follow-up call must see the re-patch.
"""
from fastapi.testclient import TestClient

import pipeline.server
from app import dashboard as d  # noqa: F401  (imported for parity with helpers)
from tests.unit._dashboard_helpers import client  # noqa: F401


def _plan_dir_from_health(client: TestClient) -> str:
    res = client.get("/api/health")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    return body["plan_dir"]


def test_health_plan_dir_is_the_path_not_the_liveref_repr(
    client, monkeypatch, tmp_path
):
    """GET /api/health must report the plan directory path, twice, live.

    Call 1 uses the canonical default binding (whatever ``pipeline.paths``
    expanded PLAN_DIR to). The ref is then re-patched -- via the same
    ``pipeline.server`` setattr mechanism the existing live-repatch tests use
    -- and a SECOND call must report the new path. A cached/snapshotted
    ``str()`` would keep returning the stale first value.
    """
    from pipeline import paths as pipeline_paths

    # --- Call 1: default binding, no patching. --------------------------
    plan_dir_first = _plan_dir_from_health(client)
    assert plan_dir_first == str(pipeline_paths.PLAN_DIR), (
        f"/api/health plan_dir must be the expanded plan directory "
        f"{str(pipeline_paths.PLAN_DIR)!r}; got {plan_dir_first!r}"
    )
    assert "LiveRef object" not in plan_dir_first, (
        "/api/health leaked the LiveRef object repr instead of the plan "
        f"directory path: {plan_dir_first!r}"
    )

    # --- Re-patch the canonical binding, then call AGAIN. ---------------
    first = tmp_path / "plans-a"
    second = tmp_path / "plans-b"
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", first)
    assert _plan_dir_from_health(client) == str(first), (
        "/api/health did not follow a live re-patch of "
        "pipeline.server.PLAN_DIR (it must resolve the ref per call, not "
        "cache the string)"
    )

    monkeypatch.setattr(pipeline.server, "PLAN_DIR", second)
    plan_dir_second = _plan_dir_from_health(client)
    assert plan_dir_second == str(second), (
        "a follow-up /api/health call after re-patching "
        f"pipeline.server.PLAN_DIR returned {plan_dir_second!r} instead of "
        f"{str(second)!r} -- the ref's str() is stale/cached"
    )
    assert "LiveRef object" not in plan_dir_second