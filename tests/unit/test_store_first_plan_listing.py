"""The scan and the stale-checkout notice list plans through the store.

``watchers.scan_all_plans`` and ``scheduler_daemon``'s stale-checkout notice
used to glob ``paths.PLAN_DIR`` for ``*.manifest.json`` directly, so a
store-backed deployment swept whatever manifests sat on this host's disk. Both
now go through :func:`pipeline.manifest_source.read_plan_names` (names) and,
in the scan, :func:`pipeline.manifest_source.read_plan_manifest` (each manifest),
so the store's record view is the primary source and the host plan directory is
the fallback.

The store is a stand-in with a record view (``records().list_plans`` and
``records().get_manifest``), installed on ``pipeline.server._store``. Tests that
do not install one keep the legacy file store, whose record view is not
consulted (see ``manifest_source``'s module docstring).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline import persistence, server, watchers
from pipeline import scheduler_daemon as sd


class _Records:
    """A record view answering ``list_plans`` and ``get_manifest``."""

    def __init__(self, names=(), manifests=None, *, list_error=None, get_error=None):
        self._names = list(names)
        self._manifests = dict(manifests or {})
        self._list_error = list_error
        self._get_error = get_error

    def list_plans(self, namespace):
        if self._list_error is not None:
            raise self._list_error
        return list(self._names)

    def get_manifest(self, namespace, plan_name):
        if self._get_error is not None:
            raise self._get_error
        return self._manifests.get(plan_name)


class _Store:
    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


class _Bus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """An empty host plan directory, bound where the file fallback reads it."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def install_store(monkeypatch):
    """Install a record-view store on ``pipeline.server._store``."""

    def _install(records):
        monkeypatch.setattr(server, "_store", _Store(records))

    return _install


def _done_manifest(tmp_path, plan_name):
    """A manifest with one in-progress story whose worktree has a done marker."""
    worktree = tmp_path / f"{plan_name}-wt"
    worktree.mkdir()
    (worktree / ".agent_done").write_text(json.dumps({"status": "tests_passed"}))
    return {"stories": {"S1": {"status": "in_progress", "worktree": str(worktree)}}}


def _plan_names_from_events(events):
    return [event["plan"] for event in events]


# ---------------------------------------------------------------------------
# scan_all_plans
# ---------------------------------------------------------------------------


def test_scan_visits_every_store_plan_in_sorted_order(tmp_path, plan_dir, install_store):
    """Two plans known only to the store are both visited, sorted by name."""
    install_store(
        _Records(
            names=["beta", "alpha"],
            manifests={
                "alpha": _done_manifest(tmp_path, "alpha"),
                "beta": _done_manifest(tmp_path, "beta"),
            },
        )
    )
    bus = _Bus()

    events = watchers.scan_all_plans(bus)

    assert _plan_names_from_events(events) == ["alpha", "beta"]
    assert bus.published == events


def test_scan_skips_an_unreadable_manifest_with_a_warning(
    tmp_path, plan_dir, install_store, caplog
):
    """A plan whose manifest neither source can read is skipped and logged."""
    (plan_dir / "broken.manifest.json").write_text("{not json", encoding="utf-8")
    install_store(
        _Records(
            names=["alpha", "broken"],
            manifests={"alpha": _done_manifest(tmp_path, "alpha")},
        )
    )
    bus = _Bus()

    with caplog.at_level("WARNING", logger="pipeline.watchers"):
        events = watchers.scan_all_plans(bus)

    assert _plan_names_from_events(events) == ["alpha"]
    assert "Skipping unreadable manifest for plan broken" in caplog.text


def test_scan_publishes_nothing_for_an_empty_store_and_empty_directory(
    plan_dir, install_store
):
    """No plan in the store and none on disk is a quiet no-op, not an error."""
    install_store(_Records(names=[]))
    bus = _Bus()

    assert watchers.scan_all_plans(bus) == []
    assert bus.published == []


def test_scan_degrades_to_the_file_when_the_store_listing_raises(
    tmp_path, plan_dir, install_store
):
    """A store listing failure is logged at debug and the directory is swept."""
    (plan_dir / "gamma.manifest.json").write_text(
        json.dumps(_done_manifest(tmp_path, "gamma")), encoding="utf-8"
    )
    install_store(_Records(list_error=RuntimeError("store unavailable")))

    events = watchers.scan_all_plans(_Bus())

    assert _plan_names_from_events(events) == ["gamma"]


def test_scan_degrades_to_the_file_when_a_store_manifest_read_raises(
    tmp_path, plan_dir, install_store
):
    """A store manifest read failure falls back to the plan's file, not a crash."""
    (plan_dir / "delta.manifest.json").write_text(
        json.dumps(_done_manifest(tmp_path, "delta")), encoding="utf-8"
    )
    install_store(
        _Records(names=["delta"], get_error=RuntimeError("row locked"))
    )

    events = watchers.scan_all_plans(_Bus())

    assert _plan_names_from_events(events) == ["delta"]


# ---------------------------------------------------------------------------
# stale-checkout notice (scheduler_daemon)
# ---------------------------------------------------------------------------


class _FakeGit:
    """Answers the two git probes the stale-checkout notice makes."""

    def __call__(self, cmd, **kwargs):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="pipeline/x.py\n", stderr="")


@pytest.fixture(autouse=True)
def _clear_dedup_set():
    sd._STALE_CHECKOUT_NOTIFIED.clear()
    yield
    sd._STALE_CHECKOUT_NOTIFIED.clear()


@pytest.fixture
def notices(monkeypatch):
    calls = []

    def _fake(plan_name, message, **kwargs):
        calls.append(plan_name)

    monkeypatch.setattr(sd.subprocess, "run", _FakeGit())
    monkeypatch.setattr(persistence, "_notify_user", _fake)
    return calls


def _notify(health_behind=2):
    daemon = sd.SchedulerDaemon(reconcile_fn=lambda: None, scan_fn=lambda: None, bus=object())
    sd.SchedulerDaemon._notify_if_checkout_stale(
        daemon, {"checkout_behind_origin": health_behind}
    )


def test_notice_names_the_first_plan_the_store_knows(plan_dir, install_store, notices):
    """The notice is attributed to the store's first sorted plan, not the disk's."""
    install_store(_Records(names=["zeta", "alpha"]))

    _notify()

    assert notices == ["alpha"]


def test_notice_is_silent_when_the_store_and_directory_are_empty(
    plan_dir, install_store, notices
):
    """No plan to address means no notice, however the store answers."""
    install_store(_Records(names=[]))

    _notify()

    assert notices == []
    assert sd._STALE_CHECKOUT_NOTIFIED == set()


def test_notice_degrades_without_raising_when_the_store_listing_raises(
    plan_dir, install_store, notices
):
    """A store listing error must not escape the notice; the file names the plan."""
    (plan_dir / "demo.manifest.json").write_text("{}", encoding="utf-8")
    install_store(_Records(list_error=RuntimeError("store unavailable")))

    _notify()

    assert notices == ["demo"]
