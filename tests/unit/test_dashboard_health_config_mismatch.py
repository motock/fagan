"""``/api/health`` must answer "are the dashboard and the scheduler looking
at the same config?" — story CFG (dashboard side).

The scheduler daemon writes its health snapshot (including a ``config``
fingerprint object) to ``<plan_dir>/.scheduler_health.json`` — the CFG-B2
default path. The dashboard's ``/api/health`` must surface that fingerprint
and diff it against the dashboard's OWN resolved values, so one glance at
the endpoint answers whether the two processes agree on ``plan_dir`` and
``worktree_root``.

Contract under test (additive — the existing ``ok``/``plan_dir`` keys keep
their meanings; other callers depend on them):

* ``plan_dir``  — unchanged: ``str(PLAN_DIR)`` following the canonical
  binding (``pipeline.server.PLAN_DIR``, read live through the LiveRef).
* ``scheduler`` — the fingerprint file's ``config`` object, or ``null``
  when the file is absent / unreadable / empty / malformed / has no
  usable ``config`` object.
* ``config_mismatch`` — the list of field names whose values differ
  between the dashboard's resolved values and the scheduler fingerprint
  (``plan_dir`` and ``worktree_root`` at minimum); ``[]`` when they agree
  or when there is no fingerprint to compare.

FAIL SOFT: this is a health endpoint. Any problem with
``.scheduler_health.json`` must degrade to ``scheduler=null`` /
``config_mismatch=[]`` with HTTP 200 — never a raise, never a 500 — the
same degraded-report pattern ``_degraded_liveness_report`` already uses in
``app/dashboard.py``.

Like ``test_dashboard_health_plan_dir.py``, these tests patch the
canonical bindings (``pipeline.server.PLAN_DIR`` / ``WORKTREE_ROOT``) and
leave the dashboard's LiveRef bindings in place, so the endpoint is
exercised exactly the way production resolves the paths. ``pipeline.paths``
is patched to the same values too so the test does not depend on which
module the implementation reads the dashboard's own values from.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

import pipeline.config
import pipeline.paths
import pipeline.server
from tests.unit._dashboard_helpers import client  # noqa: F401

HEALTH_FILENAME = ".scheduler_health.json"


@pytest.fixture
def canonical_dirs(tmp_path, monkeypatch):
    """Patch the canonical PLAN_DIR / WORKTREE_ROOT bindings to tmp dirs.

    Both ``pipeline.server`` (what the dashboard's LiveRefs read) and
    ``pipeline.paths`` (the module-level defaults the scheduler fingerprint
    mirrors) are pointed at the same tmp directories, so the test is
    independent of which module the implementation resolves the dashboard's
    own values from. Returns ``(plan_dir, worktree_root)``.
    """
    plan_dir = tmp_path / "plans"
    worktree_root = tmp_path / "worktrees"
    plan_dir.mkdir()
    worktree_root.mkdir()
    for module in (pipeline.server, pipeline.paths):
        monkeypatch.setattr(module, "PLAN_DIR", plan_dir)
        monkeypatch.setattr(module, "WORKTREE_ROOT", worktree_root)
    return plan_dir, worktree_root


def _dashboard_resolved_config(plan_dir, worktree_root) -> dict:
    """The fingerprint values that AGREE with the dashboard's own resolution.

    Mirrors ``SchedulerDaemon.config_fingerprint()``'s shape. ``autonomy``
    and ``dispatch_backend`` are set to what the dashboard process itself
    would resolve, and ``pid`` to this process's pid, so the dict agrees
    under any field-by-field comparison, not just plan_dir/worktree_root.
    """
    return {
        "plan_dir": str(plan_dir),
        "worktree_root": str(worktree_root),
        "autonomy": pipeline.config.PIPELINE_AUTONOMY,
        "dispatch_backend": os.environ.get("PIPELINE_BACKEND_DISPATCH") or None,
        "pid": os.getpid(),
    }


def _write_fingerprint(
    plan_dir,
    worktree_root,
    *,
    config: dict | None = None,
    extra: dict | None = None,
    raw: str | None = None,
) -> None:
    """Write a scheduler health file into the (patched) plan dir.

    ``config=None`` writes the realistic agreeing fingerprint;
    ``raw`` bypasses JSON encoding entirely (for malformed payloads);
    ``config`` given as a non-dict writes that value under the ``config``
    key (for wrong-type payloads).
    """
    payload: dict = {
        "alive": True,
        "last_reconcile_ts": "2026-01-01T00:00:00+00:00",
        "last_scan_ts": "2026-01-01T00:00:00+00:00",
        "last_error": None,
        "reconcile_count": 1,
        "scan_count": 1,
    }
    if config is not None or extra is not None:
        payload["config"] = config
    if extra:
        payload.update(extra)
    text = raw if raw is not None else json.dumps(payload)
    (plan_dir / HEALTH_FILENAME).write_text(text)


def _get_health(client: TestClient) -> dict:
    res = client.get("/api/health")
    assert res.status_code == 200, (
        f"/api/health must never 500 (fail-soft health endpoint); got "
        f"{res.status_code}: {res.text}"
    )
    return res.json()


# --------------------------------------------------------------------------
# Happy path: agreement
# --------------------------------------------------------------------------


def test_health_reports_scheduler_fingerprint_when_it_agrees(
    client, canonical_dirs
):
    """A fingerprint whose plan_dir/worktree_root match the patched canonical
    values yields scheduler == the file's config object and no mismatches."""
    plan_dir, worktree_root = canonical_dirs
    config = _dashboard_resolved_config(plan_dir, worktree_root)
    _write_fingerprint(plan_dir, worktree_root, config=config)

    body = _get_health(client)

    assert "scheduler" in body, (
        f"/api/health must expose the scheduler fingerprint; keys were "
        f"{sorted(body)}"
    )
    assert body["scheduler"] == config, (
        f"/api/health scheduler must be the fingerprint file's 'config' "
        f"object verbatim; got {body['scheduler']!r}"
    )
    assert body["config_mismatch"] == [], (
        f"agreeing fingerprint must produce an empty config_mismatch; got "
        f"{body['config_mismatch']!r}"
    )


def test_config_mismatch_empty_when_only_plan_dir_and_worktree_root_agree(
    client, canonical_dirs
):
    """Extra fingerprint fields the dashboard does not resolve (the
    scheduler's own ``pid``) must not force a mismatch: the comparison is
    over the fields the dashboard resolves (plan_dir, worktree_root at
    minimum), not an all-key intersection."""
    plan_dir, worktree_root = canonical_dirs
    config = _dashboard_resolved_config(plan_dir, worktree_root)
    config["pid"] = 424242  # a foreign scheduler pid — not the dashboard's
    _write_fingerprint(plan_dir, worktree_root, config=config)

    body = _get_health(client)

    assert body["config_mismatch"] == [], (
        "an unresolvable extra fingerprint field (the scheduler's pid) must "
        f"not be reported as a config mismatch; got {body['config_mismatch']!r}"
    )


# --------------------------------------------------------------------------
# Divergence
# --------------------------------------------------------------------------


def test_config_mismatch_names_plan_dir_when_it_diverges(client, canonical_dirs):
    """A fingerprint whose plan_dir differs from the dashboard's resolved
    plan dir must list 'plan_dir' in config_mismatch."""
    plan_dir, worktree_root = canonical_dirs
    config = _dashboard_resolved_config(plan_dir, worktree_root)
    config["plan_dir"] = str(plan_dir.parent / "elsewhere-plans")
    _write_fingerprint(plan_dir, worktree_root, config=config)

    body = _get_health(client)

    assert "plan_dir" in body["config_mismatch"], (
        f"a diverged fingerprint plan_dir must appear in config_mismatch; "
        f"got {body['config_mismatch']!r} (scheduler={body['scheduler']!r})"
    )
    assert body["scheduler"] == config


def test_config_mismatch_names_worktree_root_when_it_diverges(
    client, canonical_dirs
):
    """worktree_root is part of the minimum comparison set: a fingerprint
    whose worktree_root differs must list 'worktree_root' in
    config_mismatch."""
    plan_dir, worktree_root = canonical_dirs
    config = _dashboard_resolved_config(plan_dir, worktree_root)
    config["worktree_root"] = str(worktree_root.parent / "elsewhere-worktrees")
    _write_fingerprint(plan_dir, worktree_root, config=config)

    body = _get_health(client)

    assert "worktree_root" in body["config_mismatch"], (
        f"a diverged fingerprint worktree_root must appear in "
        f"config_mismatch; got {body['config_mismatch']!r}"
    )


def test_config_mismatch_lists_every_diverged_field(client, canonical_dirs):
    """When both compared fields diverge, both field names are reported."""
    plan_dir, worktree_root = canonical_dirs
    config = _dashboard_resolved_config(plan_dir, worktree_root)
    config["plan_dir"] = "/somewhere/else/plans"
    config["worktree_root"] = "/somewhere/else/worktrees"
    _write_fingerprint(plan_dir, worktree_root, config=config)

    body = _get_health(client)

    assert "plan_dir" in body["config_mismatch"]
    assert "worktree_root" in body["config_mismatch"]


# --------------------------------------------------------------------------
# NEGATIVE / fail-soft: every malformed-fingerprint shape degrades to
# scheduler=None, config_mismatch=[], HTTP 200 — never a raise.
# --------------------------------------------------------------------------


def test_missing_fingerprint_file_degrades_to_null_scheduler(client, canonical_dirs):
    """No .scheduler_health.json at all: 200, scheduler None, no mismatch."""
    plan_dir, _ = canonical_dirs
    assert not (plan_dir / HEALTH_FILENAME).exists()

    body = _get_health(client)

    assert body["scheduler"] is None
    assert body["config_mismatch"] == []


def test_malformed_fingerprint_json_degrades_to_null_scheduler(
    client, canonical_dirs
):
    """Invalid JSON in .scheduler_health.json must not raise: 200,
    scheduler None, config_mismatch []."""
    plan_dir, _ = canonical_dirs
    _write_fingerprint(plan_dir, None, raw="{this is not json")

    body = _get_health(client)

    assert body["scheduler"] is None
    assert body["config_mismatch"] == []


def test_empty_fingerprint_file_degrades_to_null_scheduler(client, canonical_dirs):
    """A zero-byte .scheduler_health.json (crash mid-write) is malformed:
    200, scheduler None, config_mismatch []."""
    plan_dir, _ = canonical_dirs
    (plan_dir / HEALTH_FILENAME).write_text("")

    body = _get_health(client)

    assert body["scheduler"] is None
    assert body["config_mismatch"] == []


def test_unreadable_fingerprint_path_degrades_to_null_scheduler(
    client, canonical_dirs
):
    """A directory where the fingerprint file should be (unreadable path —
    IsADirectoryError/OSError on open) must degrade, not raise."""
    plan_dir, _ = canonical_dirs
    (plan_dir / HEALTH_FILENAME).mkdir()

    body = _get_health(client)

    assert body["scheduler"] is None
    assert body["config_mismatch"] == []


def test_fingerprint_without_config_key_degrades_to_null_scheduler(
    client, canonical_dirs
):
    """Valid JSON but no 'config' key (e.g. a pre-CFG scheduler health
    payload): there is no fingerprint to compare, so scheduler is None and
    config_mismatch is empty."""
    plan_dir, _ = canonical_dirs
    _write_fingerprint(plan_dir, None, config=None)

    body = _get_health(client)

    assert body["scheduler"] is None
    assert body["config_mismatch"] == []


def test_fingerprint_with_non_object_config_degrades_to_null_scheduler(
    client, canonical_dirs
):
    """A 'config' that is not a JSON object (corrupted write) is not a
    fingerprint: scheduler must be null and the comparison must not crash."""
    plan_dir, _ = canonical_dirs
    _write_fingerprint(plan_dir, None, config="oops")

    body = _get_health(client)

    assert body["scheduler"] is None
    assert body["config_mismatch"] == []


def test_null_json_fingerprint_file_degrades_to_null_scheduler(
    client, canonical_dirs
):
    """A file containing the JSON literal 'null' parses fine but carries no
    config object: scheduler None, config_mismatch [], HTTP 200."""
    plan_dir, _ = canonical_dirs
    _write_fingerprint(plan_dir, None, raw="null")

    body = _get_health(client)

    assert body["scheduler"] is None
    assert body["config_mismatch"] == []


# --------------------------------------------------------------------------
# Regression: the existing keys keep their existing meanings.
# --------------------------------------------------------------------------


def test_health_still_reports_ok_true_and_resolved_plan_dir(
    client, canonical_dirs
):
    """ok stays literally True and plan_dir stays str(PLAN_DIR) following
    the patched canonical binding (never the LiveRef repr, never a stale
    value)."""
    plan_dir, _ = canonical_dirs

    body = _get_health(client)

    assert body["ok"] is True, f"/api/health ok must stay True; got {body!r}"
    assert body["plan_dir"] == str(plan_dir), (
        f"/api/health plan_dir must remain the resolved plan directory "
        f"{str(plan_dir)!r}; got {body['plan_dir']!r}"
    )
    assert "LiveRef object" not in body["plan_dir"]


def test_health_exposes_all_four_keys_together(client, canonical_dirs):
    """The additive contract in one place: ok, plan_dir, scheduler and
    config_mismatch are all present on the same response (membership, not
    an exact key set — later stories may add more)."""
    plan_dir, worktree_root = canonical_dirs
    config = _dashboard_resolved_config(plan_dir, worktree_root)
    _write_fingerprint(plan_dir, worktree_root, config=config)

    body = _get_health(client)

    for key in ("ok", "plan_dir", "scheduler", "config_mismatch"):
        assert key in body, f"/api/health lost the {key!r} key; got {sorted(body)}"


# --------------------------------------------------------------------------
# Guard rails from the brief: /api/config and get_plan_metrics untouched.
# --------------------------------------------------------------------------


def test_api_config_route_still_responds(client, canonical_dirs):
    """The brief forbids modifying /api/config: it must still answer 200
    with its effective-config payload alongside the health changes."""
    res = client.get("/api/config")
    assert res.status_code == 200, (
        f"/api/config must be untouched by the /api/health changes; got "
        f"{res.status_code}: {res.text}"
    )
    assert isinstance(res.json(), dict)


def test_get_plan_metrics_route_still_behaves(client, canonical_dirs):
    """The brief forbids touching get_plan_metrics: an unknown plan still
    404s exactly as before the health changes."""
    res = client.get("/api/plans/no-such-plan/metrics")
    assert res.status_code == 404, (
        f"get_plan_metrics must be untouched: unknown plan still 404s; got "
        f"{res.status_code}: {res.text}"
    )