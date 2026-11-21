"""nq-02: the namespace travels through the advance workers and the wake hook.

A namespaced event must reach the queue and the workers with its namespace
attached, and the wake handlers must decide the precondition from that
namespace's manifest. A deployment whose only namespace is the default never
sends a namespace key, so the no-namespace call shapes are asserted here too.
"""

# ruff: noqa: I001  -- pipeline.server must be imported before any other
# pipeline module (pre-existing import cycle; see test_advance_workers.py).
from __future__ import annotations

import json
import os
import threading
import time

import pytest

import pipeline.server
from pipeline import actor, event_wiring
from pipeline import advance_workers
from pipeline import manifest_source


class _Proc:
    """Stands in for ``subprocess.Popen`` where the child is never spawned."""

    def poll(self):
        return None


def _pipe_child():
    """Return a ``_Child`` whose request pipe the test can read back."""
    read_fd, write_fd = os.pipe()
    child = advance_workers._Child(_Proc(), write_fd, None)
    return child, read_fd


def _read_request(read_fd: int) -> bytes:
    data = b""
    while not data.endswith(b"\n"):
        data += os.read(read_fd, 1)
    return data


# -- _Child.send ------------------------------------------------------------


def test_send_without_a_namespace_writes_the_plan_only_line() -> None:
    child, read_fd = _pipe_child()
    try:
        child.send("p")
        assert _read_request(read_fd) == b'{"plan": "p"}\n'
    finally:
        child.close()
        os.close(read_fd)


def test_send_with_a_namespace_adds_the_namespace_field() -> None:
    child, read_fd = _pipe_child()
    try:
        child.send("p", namespace="team-a")
        assert json.loads(_read_request(read_fd)) == {"plan": "p", "namespace": "team-a"}
    finally:
        child.close()
        os.close(read_fd)


# -- AdvanceWorkerPool with a fake child ------------------------------------


class _FakeChild:
    """A child that records each request and answers with a success line."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self._fail_with = fail_with
        self._answer: str | None = '{"ok": true}'

    def send(self, plan_name, namespace=None):
        self.requests.append((plan_name, namespace))
        if self._fail_with is not None:
            raise self._fail_with

    def read_line(self, timeout_s):
        return self._answer

    def poll(self):
        return None

    def close_input(self):
        pass

    def terminate(self):
        return 0

    def close(self):
        pass


class _NamespacedStubQueue:
    """Serves queued ``(namespace, plan)`` entries and records each finish."""

    def __init__(self, entries):
        self._entries = list(entries)
        self.finished: list[tuple[str, str | None]] = []
        self._lock = threading.Lock()

    def take_entry(self, timeout_s):
        with self._lock:
            if not self._entries:
                return None
            namespace, plan = self._entries.pop(0)
            return (namespace, plan)

    def finish(self, plan_name, *, namespace=None):
        with self._lock:
            self.finished.append((plan_name, namespace))

    def close(self):
        pass


def _run_pool_until_finished(queue, child, expected_finishes: int) -> None:
    pool = advance_workers.AdvanceWorkerPool(
        size=1, queue=queue, plan_timeout_s=lambda: 5.0
    )
    pool._spawn = lambda: child
    pool.start()
    try:
        for _ in range(500):
            if len(queue.finished) >= expected_finishes:
                break
            time.sleep(0.01)
    finally:
        pool.stop(timeout_s=5.0)


def test_pool_sends_the_namespace_to_the_child_and_finishes_the_same_pair() -> None:
    child = _FakeChild()
    queue = _NamespacedStubQueue([("team-a", "p")])

    _run_pool_until_finished(queue, child, expected_finishes=1)

    assert child.requests == [("p", "team-a")]
    assert queue.finished == [("p", "team-a")]


def test_pool_finishes_the_same_namespaced_pair_when_the_advance_raises() -> None:
    child = _FakeChild(fail_with=RuntimeError("boom"))
    queue = _NamespacedStubQueue([("team-a", "p")])

    _run_pool_until_finished(queue, child, expected_finishes=1)

    assert child.requests == [("p", "team-a")]
    assert queue.finished == [("p", "team-a")]


def test_pool_sends_no_namespace_for_the_default_namespace_entry() -> None:
    child = _FakeChild()
    queue = _NamespacedStubQueue([(None, "p")])

    _run_pool_until_finished(queue, child, expected_finishes=1)

    assert child.requests == [("p", None)]
    assert queue.finished == [("p", None)]


# -- wake_handler -----------------------------------------------------------


@pytest.fixture
def wake_seams(monkeypatch):
    """Stub the manifest read and the advance; never leave a hook installed."""
    event_wiring.set_wake_hook(None)
    seen: dict = {"namespaces": [], "advanced": []}

    def fake_read_plan_manifest(plan_name, *, fallback_on_error):
        seen["namespaces"].append(actor.current_namespace())
        return {"plan": plan_name, "stories": {"story-a": {"status": "in_progress"}}}

    def fake_advance(plan_name, *args, **kwargs):
        seen["advanced"].append((plan_name, actor.current_namespace()))
        return {"ok": True}

    monkeypatch.setattr(manifest_source, "read_plan_manifest", fake_read_plan_manifest)
    monkeypatch.setattr(pipeline.server, "advance_pipeline", fake_advance)
    yield seen
    event_wiring.set_wake_hook(None)


def _event(**extra):
    return {"type": "agent_done", "plan": "p", "story_key": "story-a", **extra}


class _RecordingHook:
    """A wake hook that records every call's positional and keyword arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_wake_handler_reads_the_manifest_inside_the_event_namespace(wake_seams) -> None:
    hook = _RecordingHook()
    event_wiring.set_wake_hook(hook)

    event_wiring.wake_handler(_event(namespace="team-a"))

    assert wake_seams["namespaces"] == ["team-a"]


def test_wake_handler_with_a_namespace_calls_the_hook_with_a_keyword(wake_seams) -> None:
    hook = _RecordingHook()
    event_wiring.set_wake_hook(hook)

    result = event_wiring.wake_handler(_event(namespace="team-a"))

    assert result == {"ok": True, "queued": "p"}
    assert hook.calls == [(("p",), {"namespace": "team-a"})]


def test_wake_handler_without_a_namespace_calls_the_hook_with_one_argument(wake_seams) -> None:
    hook = _RecordingHook()
    event_wiring.set_wake_hook(hook)

    result = event_wiring.wake_handler(_event())

    assert result == {"ok": True, "queued": "p"}
    assert hook.calls == [(("p",), {})]


def test_wake_handler_without_a_hook_advances_inside_the_event_namespace(wake_seams) -> None:
    result = event_wiring.wake_handler(_event(namespace="team-a"))

    assert result == {"ok": True, "woke": "p", "result": {"ok": True}}
    assert wake_seams["advanced"] == [("p", "team-a")]


@pytest.mark.parametrize("bad", [7, None, ["team-a"], {"name": "team-a"}])
def test_wake_handler_refuses_a_non_str_namespace_as_malformed(wake_seams, bad) -> None:
    hook = _RecordingHook()
    event_wiring.set_wake_hook(hook)

    result = event_wiring.wake_handler(_event(namespace=bad))

    assert result == {"ok": False, "skipped": "namespace_malformed"}
    assert hook.calls == []
    assert wake_seams["namespaces"] == []


# -- reconcile_handler ------------------------------------------------------


def test_reconcile_handler_without_a_namespace_calls_the_hook_with_one_argument() -> None:
    hook = _RecordingHook()
    event_wiring.set_wake_hook(hook)
    try:
        event_wiring.reconcile_handler({"type": "reconcile", "plan": "p"})
    finally:
        event_wiring.set_wake_hook(None)

    assert hook.calls == [(("p",), {})]


def test_reconcile_handler_with_a_namespace_calls_the_hook_with_a_keyword() -> None:
    hook = _RecordingHook()
    event_wiring.set_wake_hook(hook)
    try:
        event_wiring.reconcile_handler({"type": "reconcile", "plan": "p", "namespace": "team-a"})
    finally:
        event_wiring.set_wake_hook(None)

    assert hook.calls == [(("p",), {"namespace": "team-a"})]


@pytest.mark.parametrize("bad", [7, None, ["team-a"]])
def test_reconcile_handler_refuses_a_non_str_namespace_without_calling_the_hook(bad) -> None:
    hook = _RecordingHook()
    event_wiring.set_wake_hook(hook)
    try:
        event_wiring.reconcile_handler({"type": "reconcile", "plan": "p", "namespace": bad})
    finally:
        event_wiring.set_wake_hook(None)

    assert hook.calls == []
