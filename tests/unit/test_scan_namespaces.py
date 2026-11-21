"""nq-03: namespace-stamped scheduler events and wake markers, and the per-namespace scan.

Plan names are unique only inside a namespace, so the scheduler's wake markers
and its scan must say which namespace they belong to:

* ``make_event`` stamps a top-level ``namespace`` key only when one is given;
  an event built without it keeps the legacy key set.
* ``request_wake`` / ``claim_wakes`` scope markers to the bound namespace. The
  default namespace keeps its bare ``<plan>`` marker name; any other namespace
  uses ``<org>@<team>@<plan>``.
* ``scan_all_namespaces`` runs the existing scan for the default namespace and
  then once per other in-scope namespace, under that namespace's binding.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import pipeline.server
from pipeline import scheduler_scan_namespaces, wake, watchers
from pipeline.actor import bind_namespace, current_namespace
from pipeline.events import make_event
from pipeline.scheduler_scope import ONLY_VAR, SKIP_VAR
from pipeline.store_records import DEFAULT_NAMESPACE

_SCAN_LOGGER = "pipeline.scheduler_scan_namespaces"


class FakeBus:
    """Records the events published by the scan."""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


@pytest.fixture()
def host_root(tmp_path, monkeypatch):
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr("pipeline.server.PLAN_DIR", root)
    return root


def _write_marker(host_root, name):
    wake_dir = host_root / ".wake"
    wake_dir.mkdir(mode=0o700, exist_ok=True)
    (wake_dir / name).write_text("{}")


def _remaining(host_root):
    return sorted(p.name for p in (host_root / ".wake").iterdir())


# --------------------------------------------------------------------------- #
# make_event: the optional namespace stamp.
# --------------------------------------------------------------------------- #


def test_make_event_without_namespace_has_no_namespace_key():
    event = make_event("reconcile", "alpha")

    assert "namespace" not in event


def test_make_event_with_namespace_stamps_a_top_level_key_last():
    event = make_event("reconcile", "alpha", namespace="acme/web")

    assert event["namespace"] == "acme/web"
    assert list(event)[-1] == "namespace"


@pytest.mark.parametrize("bad", ["", "A/B", "acme/web/x", "../etc", "acme@web", 7])
def test_make_event_rejects_an_invalid_namespace(bad):
    with pytest.raises(ValueError):
        make_event("reconcile", "alpha", namespace=bad)


# --------------------------------------------------------------------------- #
# request_wake: the marker name carries the bound namespace.
# --------------------------------------------------------------------------- #


def test_request_wake_under_default_writes_the_bare_plan_name(host_root):
    wake.request_wake("alpha", "approved")

    assert (host_root / ".wake" / "alpha").is_file()


def test_request_wake_under_a_namespace_writes_org_team_prefixed_name(host_root):
    with bind_namespace("acme/web"):
        wake.request_wake("alpha", "approved")

    assert (host_root / ".wake" / "acme@web@alpha").is_file()
    assert not (host_root / ".wake" / "alpha").exists()


# --------------------------------------------------------------------------- #
# claim_wakes: only the bound namespace's markers, returned as plan names.
# --------------------------------------------------------------------------- #


def test_claim_under_default_returns_only_the_default_marker(host_root):
    _write_marker(host_root, "alpha")
    _write_marker(host_root, "acme@web@alpha")

    assert wake.claim_wakes() == ["alpha"]
    assert _remaining(host_root) == ["acme@web@alpha"]


def test_claim_under_a_namespace_returns_its_plan_once_and_leaves_others(host_root):
    _write_marker(host_root, "alpha")
    _write_marker(host_root, "acme@web@alpha")
    _write_marker(host_root, "beta@api@alpha")

    with bind_namespace("acme/web"):
        assert wake.claim_wakes() == ["alpha"]
        assert wake.claim_wakes() == []

    assert _remaining(host_root) == ["alpha", "beta@api@alpha"]


@pytest.mark.parametrize("malformed", ["a@b", "a@b@c@d", "A@b@p"])
def test_malformed_namespaced_marker_is_skipped_and_kept(host_root, caplog, malformed):
    _write_marker(host_root, malformed)

    with caplog.at_level(logging.WARNING, logger="pipeline.wake"):
        assert wake.claim_wakes() == []
        assert wake.claim_wakes() == []

    assert _remaining(host_root) == [malformed]
    warnings = [r for r in caplog.records if r.getMessage() == "skipping invalid wake marker"]
    assert warnings
    for record in warnings:
        assert not hasattr(record, "plan")
        assert malformed not in record.getMessage()


def test_malformed_namespaced_marker_is_kept_under_a_namespace_too(host_root):
    _write_marker(host_root, "a@b@c@d")

    with bind_namespace("acme/web"):
        assert wake.claim_wakes() == []

    assert _remaining(host_root) == ["a@b@c@d"]


# --------------------------------------------------------------------------- #
# scan_all_plans: wake events carry the namespace of the bound scan.
# --------------------------------------------------------------------------- #


def test_namespaced_wake_event_carries_its_namespace(host_root, monkeypatch):
    monkeypatch.setattr(watchers, "scan_done_markers", lambda *args: [])
    monkeypatch.setattr(watchers.manifest_source, "read_plan_names", lambda **_: [])
    bus = FakeBus()

    with bind_namespace("acme/web"):
        wake.request_wake("alpha", "approve")
        watchers.scan_all_plans(bus)

    reconciles = [e for e in bus.events if e["type"] == "reconcile"]
    assert [e["plan"] for e in reconciles] == ["alpha"]
    assert reconciles[0]["namespace"] == "acme/web"


def test_default_wake_event_has_no_namespace_key(host_root, monkeypatch):
    monkeypatch.setattr(watchers, "scan_done_markers", lambda *args: [])
    monkeypatch.setattr(watchers.manifest_source, "read_plan_names", lambda **_: [])
    bus = FakeBus()

    wake.request_wake("alpha", "approve")
    watchers.scan_all_plans(bus)

    reconciles = [e for e in bus.events if e["type"] == "reconcile"]
    assert [e["plan"] for e in reconciles] == ["alpha"]
    assert "namespace" not in reconciles[0]


def test_agent_done_event_is_stamped_only_outside_the_default(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".agent_done").write_text('{"ok": true}')
    manifest = {"stories": {"S-1": {"status": "in_progress", "worktree": str(worktree)}}}
    bus = FakeBus()

    with bind_namespace("acme/web"):
        (namespaced,) = watchers.scan_done_markers(manifest, "alpha", bus)

    assert namespaced["namespace"] == "acme/web"

    (worktree / ".agent_done").write_text('{"ok": true}')
    (default,) = watchers.scan_done_markers(manifest, "alpha", bus)

    assert "namespace" not in default


# --------------------------------------------------------------------------- #
# scan_all_namespaces: the default scan, then each other in-scope namespace.
# --------------------------------------------------------------------------- #


class _View:
    """A fake record view: ``{namespace: {plan: manifest}}``."""

    def __init__(self, plans_by_namespace):
        self._plans = plans_by_namespace

    def list_namespaces(self):
        return sorted(self._plans)

    def list_plans(self, namespace):
        return list(self._plans.get(namespace, {}))


@pytest.fixture()
def three_namespaces(monkeypatch):
    view = _View(
        {
            DEFAULT_NAMESPACE: {"alpha": {}},
            "acme/web": {"alpha": {}},
            "beta/api": {"gamma": {}},
        }
    )
    fake_server = SimpleNamespace(_store=SimpleNamespace(records=lambda: view))
    monkeypatch.setattr(pipeline.server, "_store", fake_server._store)
    monkeypatch.delenv(ONLY_VAR, raising=False)
    monkeypatch.delenv(SKIP_VAR, raising=False)
    return view


@pytest.fixture()
def scans(monkeypatch):
    """Record each ``scan_all_plans`` call with the namespace bound during it."""
    calls = []

    def _fake_scan(bus):
        calls.append(current_namespace())
        return [{"scanned": current_namespace()}]

    monkeypatch.setattr(watchers, "scan_all_plans", _fake_scan)
    return calls


def test_scan_runs_default_then_each_other_namespace_under_its_binding(three_namespaces, scans):
    default_calls = []

    def _scan_default():
        default_calls.append(current_namespace())
        return [{"scanned": "default"}]

    events = scheduler_scan_namespaces.scan_all_namespaces(_scan_default, FakeBus())

    assert default_calls == [DEFAULT_NAMESPACE]
    assert scans == ["acme/web", "beta/api"]
    assert [e["scanned"] for e in events] == ["default", "acme/web", "beta/api"]


def test_only_scope_scans_just_the_named_namespaces(three_namespaces, scans, monkeypatch):
    monkeypatch.setenv(ONLY_VAR, "beta/api")
    default_calls = []

    scheduler_scan_namespaces.scan_all_namespaces(
        lambda: default_calls.append(1) or [], FakeBus()
    )

    assert default_calls == []
    assert scans == ["beta/api"]


def test_skip_scope_excludes_the_named_namespaces(three_namespaces, scans, monkeypatch):
    monkeypatch.setenv(SKIP_VAR, "beta/api")

    scheduler_scan_namespaces.scan_all_namespaces(list, FakeBus())

    assert scans == ["acme/web"]


def test_a_failing_namespace_does_not_stop_the_others(three_namespaces, monkeypatch, caplog):
    seen = []

    def _scan(bus):
        seen.append(current_namespace())
        if current_namespace() == "acme/web":
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(watchers, "scan_all_plans", _scan)

    with caplog.at_level(logging.WARNING, logger=_SCAN_LOGGER):
        events = scheduler_scan_namespaces.scan_all_namespaces(list, FakeBus())

    assert seen == ["acme/web", "beta/api"]
    assert events == []
    failures = [r for r in caplog.records if r.name == _SCAN_LOGGER and r.levelno == logging.WARNING]
    assert len(failures) == 1
    assert "RuntimeError" in failures[0].getMessage()
    assert "boom" not in failures[0].getMessage()


def test_both_scope_variables_scan_only_the_default_and_warn_once(
    three_namespaces, scans, monkeypatch, caplog
):
    monkeypatch.setenv(ONLY_VAR, "acme/web")
    monkeypatch.setenv(SKIP_VAR, "beta/api")
    default_calls = []

    with caplog.at_level(logging.WARNING, logger=_SCAN_LOGGER):
        scheduler_scan_namespaces.scan_all_namespaces(
            lambda: default_calls.append(1) or [], FakeBus()
        )

    assert default_calls == [1]
    assert scans == []
    warnings = [r for r in caplog.records if r.name == _SCAN_LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --------------------------------------------------------------------------- #
# request_wake: the final marker name is validated before any file is written.
# A bound namespace or plan name that would yield a path-like marker is refused.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "prefix",
    ["..@x@", "a..@x@", ".hid@x@", "a/b@x@"],
)
def test_request_wake_refuses_a_path_like_namespaced_marker(host_root, monkeypatch, prefix):
    monkeypatch.setattr(wake, "_scope_prefix", lambda namespace: prefix)

    with bind_namespace("acme/web"):
        wake.request_wake("alpha", "approved")

    assert not (host_root / ".wake").exists() or _remaining(host_root) == []


@pytest.mark.parametrize("plan_name", ["..", ".hidden", "a/b", "a..b"])
def test_request_wake_refuses_a_path_like_plan_name_past_key_validation(
    host_root, monkeypatch, plan_name
):
    monkeypatch.setattr(wake, "_validate_key", lambda name: None)

    with bind_namespace("acme/web"):
        wake.request_wake(plan_name, "approved")

    assert not (host_root / ".wake").exists() or _remaining(host_root) == []


def test_request_wake_refusal_logs_without_the_marker_name(host_root, monkeypatch, caplog):
    monkeypatch.setattr(wake, "_scope_prefix", lambda namespace: ".hid@x@")

    with caplog.at_level(logging.WARNING, logger="pipeline.wake"), bind_namespace("acme/web"):
        wake.request_wake("alpha", "approved")

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "hid@x@" not in messages
    assert "alpha" not in messages


def test_request_wake_still_writes_a_valid_dotted_default_plan(host_root):
    wake.request_wake("v1.2.3", "approved")

    assert (host_root / ".wake" / "v1.2.3").is_file()
