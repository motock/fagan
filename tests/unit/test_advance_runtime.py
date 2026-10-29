"""Tests for ``pipeline.advance_runtime`` (ADR 0011 S1).

``PIPELINE_ADVANCE_MODE=queue`` moves plan advancement off the event-delivering
thread: ``wake_handler`` enqueues instead of advancing synchronously, a pool of
long-lived workers drains the queue, and the reconcile pass only enqueues plans
that have gone stale.
"""

import logging
from typing import ClassVar

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle: import first)
from pipeline import advance_runtime


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    """Every test starts from today's behaviour: no mode, no bound override."""
    monkeypatch.delenv("PIPELINE_ADVANCE_MODE", raising=False)
    monkeypatch.delenv("PIPELINE_ADVANCE_STALE_SECONDS", raising=False)


def test_queue_mode_unset_is_off():
    assert advance_runtime.queue_mode_enabled() is False


def test_queue_mode_pool_is_off(monkeypatch):
    monkeypatch.setenv("PIPELINE_ADVANCE_MODE", "pool")
    assert advance_runtime.queue_mode_enabled() is False


def test_queue_mode_queue_is_on(monkeypatch):
    monkeypatch.setenv("PIPELINE_ADVANCE_MODE", "queue")
    assert advance_runtime.queue_mode_enabled() is True


def test_queue_mode_is_read_on_every_call(monkeypatch):
    """The env read must not be cached in a module global at import time."""
    monkeypatch.setenv("PIPELINE_ADVANCE_MODE", "queue")
    assert advance_runtime.queue_mode_enabled() is True

    monkeypatch.delenv("PIPELINE_ADVANCE_MODE")
    assert advance_runtime.queue_mode_enabled() is False


def test_queue_mode_unknown_value_is_off_and_warns_without_echoing_it(
    monkeypatch, caplog
):
    monkeypatch.setenv("PIPELINE_ADVANCE_MODE", "bogus")
    with caplog.at_level(logging.WARNING):
        assert advance_runtime.queue_mode_enabled() is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "PIPELINE_ADVANCE_MODE" in message
    assert "pool" in message
    assert "queue" in message
    # The rejected value may be a path or a secret: never echo it.
    assert "bogus" not in message


# ---------------------------------------------------------------------------
# wake_handler: the queue is the wake hook
# ---------------------------------------------------------------------------

import json

import pipeline.server as server_mod
from pipeline import event_wiring


@pytest.fixture(autouse=True)
def _clear_wake_hook():
    """The wake hook is module-level state: never leak it across tests."""
    event_wiring.set_wake_hook(None)
    yield
    event_wiring.set_wake_hook(None)


def _write_manifest(tmp_path, monkeypatch, plan_name="alpha", status="in_progress"):
    manifest = {"name": plan_name, "stories": {"SQ-1": {"status": status}}}
    (tmp_path / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path)
    return {"plan": plan_name, "story_key": "SQ-1", "type": "agent_done"}


def test_wake_handler_with_hook_queues_and_does_not_advance(tmp_path, monkeypatch):
    queued = []
    event_wiring.set_wake_hook(queued.append)
    event = _write_manifest(tmp_path, monkeypatch)
    advanced = []
    monkeypatch.setattr(
        server_mod, "advance_pipeline", lambda plan: advanced.append(plan)
    )

    result = event_wiring.wake_handler(event)

    assert queued == ["alpha"]
    assert advanced == []
    assert result == {"ok": True, "queued": "alpha"}


def test_wake_handler_without_hook_advances_as_before(tmp_path, monkeypatch):
    event = _write_manifest(tmp_path, monkeypatch)
    advanced = []

    def fake_advance(plan):
        advanced.append(plan)
        return {"ok": True, "advanced": plan}

    monkeypatch.setattr(server_mod, "advance_pipeline", fake_advance)

    result = event_wiring.wake_handler(event)

    assert advanced == ["alpha"]
    assert result == {
        "ok": True,
        "woke": "alpha",
        "result": {"ok": True, "advanced": "alpha"},
    }


def test_wake_handler_stale_event_calls_neither(tmp_path, monkeypatch):
    queued = []
    event_wiring.set_wake_hook(queued.append)
    # The story already finished, so an agent_done event is a redelivery.
    event = _write_manifest(tmp_path, monkeypatch, status="done")
    advanced = []
    monkeypatch.setattr(
        server_mod, "advance_pipeline", lambda plan: advanced.append(plan)
    )

    result = event_wiring.wake_handler(event)

    assert queued == []
    assert advanced == []
    assert result["skipped"] == "precondition_not_met"


# ---------------------------------------------------------------------------
# build_runtime: today's behaviour when queue mode is off
# ---------------------------------------------------------------------------


class RecordingBus:
    """Minimal stand-in for an EventBus that records subscriptions."""

    def __init__(self):
        self.subscriptions = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))

    def publish(self, event):
        pass


def test_build_runtime_without_queue_mode_returns_the_serial_fn(monkeypatch):
    bus = RecordingBus()
    calls = []

    def serial_fn():
        calls.append(True)
        return {"ok": True, "serial": True}

    reconcile_fn, stop_fn = advance_runtime.build_runtime(bus, serial_fn)

    # Identity, not a wrapper: the daemon's reconcile_fn must stay the very
    # object tests and callers already hold.
    assert reconcile_fn is serial_fn
    assert stop_fn() is None
    assert stop_fn() is None  # idempotent, never raises
    assert calls == []
    assert bus.subscriptions == []


def test_build_runtime_pool_mode_registers_no_wake_hook(monkeypatch):
    monkeypatch.setenv("PIPELINE_ADVANCE_MODE", "pool")
    installed = []
    monkeypatch.setattr(
        event_wiring, "set_wake_hook", lambda hook: installed.append(hook)
    )

    _reconcile_fn, _stop_fn = advance_runtime.build_runtime(
        RecordingBus(), lambda: {"ok": True}
    )

    assert installed == []
    assert event_wiring._wake_hook is None


# ---------------------------------------------------------------------------
# build_runtime in queue mode: the queue, the pool and the wake hook
# ---------------------------------------------------------------------------


class FakeQueue:
    """Records requests and the last-advanced times the sweep reads."""

    def __init__(self, last_advanced=None):
        self.requests = []
        self.last_advanced_by_plan = dict(last_advanced or {})
        self.request = self._request  # bound, so ``is`` checks mean something

    def _request(self, plan_name, reason=""):
        self.requests.append((plan_name, reason))
        return True

    def last_advanced(self, plan_name):
        return self.last_advanced_by_plan.get(plan_name)


class FakePool:
    """Records lifecycle calls; ``stats()`` counts its calls."""

    def __init__(self, size, queue, plan_timeout_s):
        self.size = size
        self.queue = queue
        self.plan_timeout_s = plan_timeout_s
        self.started = 0
        self.stopped = 0
        self.stats_calls = 0

    def start(self):
        self.started += 1

    def stop(self, timeout_s=15.0):
        self.stopped += 1

    def stats(self):
        self.stats_calls += 1
        return {"size": self.size, "idle": self.size, "busy": 0}


@pytest.fixture
def queue_runtime(monkeypatch):
    """Queue mode with the queue, the pool and the worker count faked."""
    monkeypatch.setenv("PIPELINE_ADVANCE_MODE", "queue")
    from pipeline import advance_pool, advance_queue, advance_workers

    queue = FakeQueue()
    created = {"queues": [], "pools": []}

    def fake_queue_factory():
        created["queues"].append(queue)
        return queue

    def fake_pool_factory(size, queue_arg, plan_timeout_s):
        pool = FakePool(size, queue_arg, plan_timeout_s)
        created["pools"].append(pool)
        return pool

    monkeypatch.setattr(advance_queue, "AdvanceQueue", fake_queue_factory)
    monkeypatch.setattr(advance_workers, "AdvanceWorkerPool", fake_pool_factory)
    monkeypatch.setattr(advance_pool, "_advance_workers", lambda: 3)

    def plan_timeout_s():
        return 123.0

    monkeypatch.setattr(advance_pool, "_plan_timeout_seconds", plan_timeout_s)
    return {"queue": queue, "created": created, "plan_timeout_s": plan_timeout_s}


def test_build_runtime_queue_mode_constructs_and_starts_the_pool(queue_runtime):
    _reconcile_fn, _stop_fn = advance_runtime.build_runtime(
        RecordingBus(), lambda: {"ok": True}
    )

    created = queue_runtime["created"]
    assert created["queues"] == [queue_runtime["queue"]]
    assert len(created["pools"]) == 1
    pool = created["pools"][0]
    assert pool.size == 3
    assert pool.queue is queue_runtime["queue"]
    # The pool takes a callable: it reads the timeout per advance.
    assert pool.plan_timeout_s is queue_runtime["plan_timeout_s"]
    assert pool.started == 1


def test_build_runtime_queue_mode_registers_the_queue_request_as_wake_hook(
    queue_runtime,
):
    advance_runtime.build_runtime(RecordingBus(), lambda: {"ok": True})

    assert event_wiring._wake_hook == queue_runtime["queue"].request


# ---------------------------------------------------------------------------
# the sweep: request active stale plans, never advance, never block
# ---------------------------------------------------------------------------

import pipeline.plan_activity as plan_activity_mod


class FakeStore:
    """The slice of the store the sweep reads."""

    def __init__(self, manifests):
        self.manifests = manifests
        self.list_calls = 0

    def list_manifests(self):
        self.list_calls += 1
        return list(self.manifests)

    def get_manifest_or_none(self, plan_name):
        return self.manifests.get(plan_name)


@pytest.fixture
def sweep_env(monkeypatch, queue_runtime):
    """Three plans: two active (one fresh, one never advanced) and one inert."""
    store = FakeStore(
        {
            "alpha": {"name": "alpha", "stories": {"A-1": {"status": "todo"}}},
            "beta": {"name": "beta", "stories": {"B-1": {"status": "todo"}}},
            "gamma": {"name": "gamma", "stories": {"G-1": {"status": "done"}}},
        }
    )
    monkeypatch.setattr(server_mod, "_store", store)

    inert = {"gamma"}
    monkeypatch.setattr(
        plan_activity_mod, "is_inert", lambda manifest: manifest["name"] in inert
    )

    clock = {"now": 1000.0}
    monkeypatch.setattr(advance_runtime, "_now", lambda: clock["now"])

    serial_calls = []

    def serial_fn():
        serial_calls.append(True)
        return {"ok": True, "serial": True}

    reconcile_fn, stop_fn = advance_runtime.build_runtime(RecordingBus(), serial_fn)
    return {
        "sweep": reconcile_fn,
        "stop": stop_fn,
        "queue": queue_runtime["queue"],
        "pool": queue_runtime["created"]["pools"][0],
        "clock": clock,
        "serial_calls": serial_calls,
    }


def test_sweep_requests_only_active_plans_that_have_gone_stale(sweep_env):
    sweep_env["queue"].last_advanced_by_plan["alpha"] = 995.0

    result = sweep_env["sweep"]()

    # beta has never been advanced; alpha advanced 5 s ago is still fresh and
    # gamma is inert. Manifest order is preserved.
    assert result == {
        "ok": True,
        "queued": ["beta"],
        "workers": {"size": 3, "idle": 3, "busy": 0},
    }
    assert sweep_env["queue"].requests == [("beta", "sweep")]
    # Never advances synchronously and never waits for one.
    assert sweep_env["serial_calls"] == []
    assert sweep_env["pool"].stats_calls == 1


def test_sweep_re_requests_once_the_last_advance_passes_the_bound(sweep_env):
    queue = sweep_env["queue"]
    queue.last_advanced_by_plan["alpha"] = 995.0

    assert sweep_env["sweep"]()["queued"] == ["beta"]

    # A worker finished beta's advance at t=1003.2.
    queue.last_advanced_by_plan["beta"] = 1003.2
    queue.requests.clear()
    sweep_env["clock"]["now"] = 1010.0
    assert sweep_env["sweep"]()["queued"] == []
    assert queue.requests == []

    # Both ages now exceed the 60 s default: 70 and 61.8.
    sweep_env["clock"]["now"] = 1065.0
    assert sweep_env["sweep"]()["queued"] == ["alpha", "beta"]
    assert queue.requests == [("alpha", "sweep"), ("beta", "sweep")]


def test_sweep_retries_a_plan_whose_request_never_completed(sweep_env):
    """A rejected or failed advance leaves ``last_advanced`` untouched."""
    sweep_env["queue"].last_advanced_by_plan["alpha"] = 995.0
    sweep_env["clock"]["now"] = 1000.0
    assert sweep_env["sweep"]()["queued"] == ["beta"]

    # Nothing ran: beta is still unadvanced, so the very next pass retries it
    # instead of skipping it for a further staleness window.
    sweep_env["queue"].requests.clear()
    sweep_env["clock"]["now"] = 1005.0
    assert sweep_env["sweep"]()["queued"] == ["beta"]
    assert sweep_env["queue"].requests == [("beta", "sweep")]


@pytest.mark.parametrize("raw", ["abc", "inf", "-inf", "nan", "0", "-5", ""])
def test_sweep_falls_back_to_the_default_bound_when_it_cannot_be_used(
    sweep_env, monkeypatch, caplog, raw
):
    """A malformed, non-finite or non-positive bound must never disable re-queueing."""
    monkeypatch.setenv("PIPELINE_ADVANCE_STALE_SECONDS", raw)
    sweep_env["queue"].last_advanced_by_plan["alpha"] = 900.0  # age 100 > 60

    with caplog.at_level(logging.WARNING):
        result = sweep_env["sweep"]()

    assert result["queued"] == ["alpha", "beta"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "PIPELINE_ADVANCE_STALE_SECONDS" in warnings[0].getMessage()


def test_stale_bound_warning_never_echoes_a_rejected_value(sweep_env, monkeypatch, caplog):
    monkeypatch.setenv("PIPELINE_ADVANCE_STALE_SECONDS", "not-a-number-xyz")

    with caplog.at_level(logging.WARNING):
        sweep_env["sweep"]()

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 1
    assert "not-a-number-xyz" not in messages[0]


def test_sweep_reads_the_stale_bound_on_every_call(sweep_env, monkeypatch):
    queue = sweep_env["queue"]
    queue.last_advanced_by_plan["alpha"] = 995.0  # age 5

    assert sweep_env["sweep"]()["queued"] == ["beta"]

    # No rebuild: the runtime is the same object the daemon is already running.
    monkeypatch.setenv("PIPELINE_ADVANCE_STALE_SECONDS", "3")
    queue.requests.clear()
    assert sweep_env["sweep"]()["queued"] == ["alpha", "beta"]
    assert queue.requests == [("alpha", "sweep"), ("beta", "sweep")]


def test_stop_unregisters_the_hook_then_stops_the_pool(sweep_env):
    pool = sweep_env["pool"]
    assert event_wiring._wake_hook == sweep_env["queue"].request

    sweep_env["stop"]()

    assert event_wiring._wake_hook is None
    assert pool.stopped == 1


def test_after_stop_the_handler_advances_synchronously_again(
    sweep_env, tmp_path, monkeypatch
):
    """A stopped pool must never leave events answered with a dead ``queued``."""
    advanced = []
    monkeypatch.setattr(
        server_mod, "advance_pipeline", lambda plan: advanced.append(plan)
    )
    event = _write_manifest(tmp_path, monkeypatch, plan_name="delta")

    assert event_wiring.wake_handler(event) == {"ok": True, "queued": "delta"}

    sweep_env["stop"]()

    result = event_wiring.wake_handler(event)
    assert advanced == ["delta"]
    assert result == {"ok": True, "woke": "delta", "result": None}


# ---------------------------------------------------------------------------
# run_daemon wiring
# ---------------------------------------------------------------------------

from pipeline import scheduler_daemon as daemon_mod


class _FakeDaemon:
    """Captures the collaborators ``run_daemon`` wires, and never loops."""

    instances: ClassVar[list["_FakeDaemon"]] = []
    order: ClassVar[list[str]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeDaemon.instances.append(self)

    def run_forever(self, stop_event):
        _FakeDaemon.order.append("run_forever")


def test_run_daemon_runs_the_runtime_and_stops_it_after_the_loop(monkeypatch, tmp_path):
    _FakeDaemon.instances = []
    _FakeDaemon.order = []
    order = _FakeDaemon.order

    def fake_reconcile():
        return {"ok": True}

    def fake_stop():
        order.append("stop")

    monkeypatch.setattr(
        advance_runtime,
        "build_runtime",
        lambda bus, serial_fn: (fake_reconcile, fake_stop),
    )
    monkeypatch.setattr(daemon_mod, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "SchedulerDaemon", _FakeDaemon)
    monkeypatch.setattr(daemon_mod, "build_bus", lambda: RecordingBus())
    monkeypatch.setattr(daemon_mod, "scan_all_plans", lambda bus: None)
    monkeypatch.setattr(server_mod, "advance_all_plans", lambda: {"ok": True})
    monkeypatch.delenv("PIPELINE_SCHEDULER_INTERVAL_S", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_HEALTH_PATH", raising=False)

    assert daemon_mod.run_daemon() == 0

    assert len(_FakeDaemon.instances) == 1
    assert _FakeDaemon.instances[0].kwargs["reconcile_fn"] is fake_reconcile
    # The stop callable runs only after the loop has returned.
    assert order == ["run_forever", "stop"]


def test_now_uses_the_clock_the_queue_stamps_with():
    """``AdvanceQueue.last_advanced`` is monotonic; comparing clocks is a bug."""
    import time

    assert advance_runtime._now() == pytest.approx(time.monotonic(), abs=1.0)


def test_build_runtime_in_queue_mode_does_not_advance_at_construction(
    queue_runtime,
):
    bus = RecordingBus()
    advance_runtime.build_runtime(bus, lambda: {"ok": True})

    # The runtime only wires the queue and the scan's wake path up: nothing is
    # requested until an event arrives or the sweep runs. The ``reconcile``
    # subscription is wiring, not an advance -- its handler enqueues on the
    # delivering thread and never touches the pipeline.
    assert queue_runtime["queue"].requests == []
    subscribed = [event_type for event_type, _handler in bus.subscriptions]
    assert "reconcile" in subscribed
