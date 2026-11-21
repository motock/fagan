"""The queue-mode sweep covers every in-scope namespace, not only the default (nq-04).

``sweep_namespaces`` queues stale, active plans in the default namespace exactly
as the pre-queue sweep did, and in each other in-scope namespace through the
store's record view. Plan names are unique only inside a namespace, so the same
name can be queued once per namespace.
"""

from __future__ import annotations

import pytest

import pipeline.server as server_mod
from pipeline import advance_sweep, plan_activity
from pipeline.actor import current_namespace

STALE_SECONDS = 60.0
NOW = 1000.0
DEFAULT = current_namespace()


class FakeRecords:
    """A namespaced record view holding ``{namespace: {plan: manifest}}``.

    ``get_manifests`` is offered only when ``with_get_manifests`` is true, so
    the per-plan fallback can be exercised. Namespaces in ``failing`` raise on
    every listing call.
    """

    def __init__(self, plans, *, with_get_manifests=True, failing=()):
        self.plans = {namespace: dict(found) for namespace, found in plans.items()}
        self.failing = set(failing)
        self.get_manifests_calls = []
        self.get_manifest_calls = []
        if with_get_manifests:
            self.get_manifests = self._get_manifests

    def list_namespaces(self):
        return list(self.plans)

    def list_plans(self, namespace):
        if namespace in self.failing:
            raise RuntimeError("listing secret /var/lib/pipeline/worktrees/x")
        return sorted(self.plans.get(namespace, {}))

    def get_manifest(self, namespace, plan_name):
        self.get_manifest_calls.append((namespace, plan_name))
        return self.plans.get(namespace, {}).get(plan_name)

    def _get_manifests(self, namespace):
        self.get_manifests_calls.append(namespace)
        if namespace in self.failing:
            raise RuntimeError("listing secret /var/lib/pipeline/worktrees/x")
        return dict(self.plans.get(namespace, {}))


class FakeServerStore:
    """The default namespace's slice of ``server._store``.

    ``records`` is set only when a record view is given, as the real stores do.
    """

    def __init__(self, manifests, records=None):
        self.manifests = manifests
        if records is not None:
            self.records = lambda: records

    def list_manifests(self):
        return list(self.manifests)

    def get_manifest_or_none(self, plan_name):
        return self.manifests.get(plan_name)


class FakeQueue:
    """Records requests as ``(plan, reason, namespace)``; ``None`` is the default."""

    def __init__(self, last_advanced=None):
        self.requests = []
        self.last = dict(last_advanced or {})

    def request(self, plan_name, reason="", *, namespace=None):
        self.requests.append((plan_name, reason, namespace))
        return True

    def last_advanced(self, plan_name, *, namespace=None):
        return self.last.get((namespace, plan_name))


class FakePool:
    def stats(self):
        return {"size": 1, "idle": 1, "busy": 0}


def _sweep(queue, now=NOW):
    return advance_sweep.sweep_namespaces(queue, FakePool(), STALE_SECONDS, now)


@pytest.fixture(autouse=True)
def _no_scope_env(monkeypatch):
    monkeypatch.delenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", raising=False)
    monkeypatch.setattr(
        plan_activity, "is_inert", lambda manifest: manifest.get("inert") is True
    )


def _install(monkeypatch, server_store):
    monkeypatch.setattr(server_mod, "_store", server_store)


# ---------------------------------------------------------------------------
# staleness and the namespace key
# ---------------------------------------------------------------------------


def test_stale_plan_in_a_second_namespace_is_requested_with_its_namespace(monkeypatch):
    records = FakeRecords({"beta": {"stale": {"name": "stale"}}})
    _install(monkeypatch, FakeServerStore({}, records))
    queue = FakeQueue()

    result = _sweep(queue)

    assert queue.requests == [("stale", "sweep", "beta")]
    assert result["namespaces"] == {"beta": ["stale"]}


def test_fresh_plan_in_a_second_namespace_is_not_requested(monkeypatch):
    records = FakeRecords(
        {"beta": {"fresh": {"name": "fresh"}, "old": {"name": "old"}}}
    )
    _install(monkeypatch, FakeServerStore({}, records))
    queue = FakeQueue({("beta", "fresh"): NOW - 10.0, ("beta", "old"): NOW - 500.0})

    result = _sweep(queue)

    assert queue.requests == [("old", "sweep", "beta")]
    assert result["namespaces"] == {"beta": ["old"]}


def test_the_same_plan_name_stale_in_two_namespaces_is_requested_twice(monkeypatch):
    records = FakeRecords(
        {"beta": {"shared": {}}, "gamma": {"shared": {}}}
    )
    _install(monkeypatch, FakeServerStore({}, records))
    queue = FakeQueue()

    result = _sweep(queue)

    assert queue.requests == [("shared", "sweep", "beta"), ("shared", "sweep", "gamma")]
    assert result["namespaces"] == {"beta": ["shared"], "gamma": ["shared"]}


def test_default_plans_are_requested_without_a_namespace_argument(monkeypatch):
    _install(
        monkeypatch,
        FakeServerStore({"alpha": {"name": "alpha"}}, FakeRecords({})),
    )
    queue = FakeQueue()

    result = _sweep(queue)

    assert queue.requests == [("alpha", "sweep", None)]
    assert result["queued"] == ["alpha"]


def test_inert_plans_are_skipped_in_every_namespace(monkeypatch):
    records = FakeRecords(
        {
            "beta": {"idle": {"inert": True}, "live": {"name": "live"}},
            "gamma": {"also-idle": {"inert": True}},
        }
    )
    _install(monkeypatch, FakeServerStore({"default-idle": {"inert": True}}, records))
    queue = FakeQueue()

    result = _sweep(queue)

    assert queue.requests == [("live", "sweep", "beta")]
    assert result["queued"] == []
    assert result["namespaces"] == {"beta": ["live"]}


# ---------------------------------------------------------------------------
# scope: ONLY and SKIP, and an invalid scope refusing the whole pass
# ---------------------------------------------------------------------------


def test_only_scope_sweeps_just_the_named_namespaces(monkeypatch):
    records = FakeRecords({"beta": {"b": {}}, "gamma": {"g": {}}})
    _install(monkeypatch, FakeServerStore({}, records))
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", "beta")
    queue = FakeQueue()

    result = _sweep(queue)

    assert queue.requests == [("b", "sweep", "beta")]
    assert result["namespaces"] == {"beta": ["b"]}


def test_skip_scope_leaves_the_skipped_namespaces_unswept(monkeypatch):
    records = FakeRecords({"beta": {"b": {}}, "gamma": {"g": {}}})
    _install(monkeypatch, FakeServerStore({}, records))
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", "gamma")
    queue = FakeQueue()

    _sweep(queue)

    assert queue.requests == [("b", "sweep", "beta")]
    assert "gamma" not in records.get_manifests_calls


def test_skip_naming_the_default_namespace_leaves_its_plans_unqueued(monkeypatch):
    records = FakeRecords({"beta": {"b": {}}})
    _install(
        monkeypatch,
        FakeServerStore({"alpha": {"name": "alpha"}}, records),
    )
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", DEFAULT)
    queue = FakeQueue()

    result = _sweep(queue)

    assert queue.requests == [("b", "sweep", "beta")]
    assert result["queued"] == []


def test_both_scope_variables_refuse_the_whole_sweep(monkeypatch):
    records = FakeRecords({"beta": {"b": {}}})
    _install(monkeypatch, FakeServerStore({"alpha": {"name": "alpha"}}, records))
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", "beta")
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", "gamma")
    queue = FakeQueue()

    result = _sweep(queue)

    assert result == {"ok": False, "queued": [], "namespace_error": "scheduler_scope_invalid"}
    assert queue.requests == []


# ---------------------------------------------------------------------------
# stores without a record view, and the per-namespace read
# ---------------------------------------------------------------------------


def test_a_store_without_a_record_view_sweeps_the_default_namespace_only(monkeypatch):
    _install(monkeypatch, FakeServerStore({"alpha": {"name": "alpha"}}))
    queue = FakeQueue()

    result = _sweep(queue)

    assert queue.requests == [("alpha", "sweep", None)]
    assert "namespaces" not in result
    assert set(result) == {"ok", "queued", "workers"}


def test_get_manifests_is_called_once_per_namespace_when_present(monkeypatch):
    records = FakeRecords({"beta": {"b1": {}, "b2": {}}, "gamma": {"g1": {}}})
    _install(monkeypatch, FakeServerStore({}, records))
    queue = FakeQueue()

    _sweep(queue)

    assert records.get_manifests_calls == ["beta", "gamma"]
    assert records.get_manifest_calls == []


def test_per_plan_get_manifest_is_the_fallback_without_get_manifests(monkeypatch):
    records = FakeRecords({"beta": {"b1": {}}}, with_get_manifests=False)
    _install(monkeypatch, FakeServerStore({}, records))
    queue = FakeQueue()

    result = _sweep(queue)

    assert records.get_manifest_calls == [("beta", "b1")]
    assert queue.requests == [("b1", "sweep", "beta")]
    assert result["namespaces"] == {"beta": ["b1"]}


# ---------------------------------------------------------------------------
# a namespace that fails to list is skipped; the others still sweep
# ---------------------------------------------------------------------------


def test_a_namespace_whose_listing_raises_is_skipped_and_the_next_still_sweeps(
    monkeypatch, caplog
):
    records = FakeRecords(
        {"beta": {"b": {}}, "gamma": {"g": {}}}, failing={"beta"}
    )
    _install(monkeypatch, FakeServerStore({}, records))
    queue = FakeQueue()

    with caplog.at_level("ERROR"):
        result = _sweep(queue)

    assert queue.requests == [("g", "sweep", "gamma")]
    assert result["namespaces"] == {"gamma": ["g"]}
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert "/var/lib/pipeline" not in caplog.text


def test_a_failing_namespace_listing_without_get_manifests_is_also_skipped(
    monkeypatch,
):
    records = FakeRecords(
        {"beta": {"b": {}}, "gamma": {"g": {}}},
        with_get_manifests=False,
        failing={"beta"},
    )
    _install(monkeypatch, FakeServerStore({}, records))
    queue = FakeQueue()

    _sweep(queue)

    assert queue.requests == [("g", "sweep", "gamma")]


def test_namespaces_key_is_absent_when_no_other_namespace_queued_anything(monkeypatch):
    records = FakeRecords({"beta": {"fresh": {}}})
    _install(monkeypatch, FakeServerStore({}, records))
    queue = FakeQueue({("beta", "fresh"): NOW - 1.0})

    result = _sweep(queue)

    assert queue.requests == []
    assert "namespaces" not in result
