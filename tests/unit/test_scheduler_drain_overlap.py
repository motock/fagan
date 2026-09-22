"""Regression tests for the wedged-drain overlap bug (SWH-03 review).

``SchedulerDaemon.run_once`` bounds its outbox drain with
``_DRAIN_JOIN_TIMEOUT_SECONDS`` and abandons the worker when the join times
out.  Abandoning it *without remembering it* lets the NEXT tick (default
interval 60s) start a second ``drain_outbox`` while the first is still
running, so overlap is the norm in the wedged case.

``notification_outbox._drain_one_outbox`` is not safe between two drainers:
its merge step assumes the snapshot is a prefix of the current file
(``appended_since_snapshot = current_lines[len(raw_lines):]``).  If drain A
rewrites the file shorter (``[r1]`` -> ``[]`` after accepting r1) and a
producer then appends r2, drain B's re-read sees ``current_lines == [r2]``
with ``len(raw_lines) == 1``, computes ``appended == []`` and rewrites
``[]`` -- silently dropping r2.  That contradicts the module's documented
"never a lost notification ... under concurrency too" contract and
REFERENCE.md's "a transient SMTP outage never loses a notification".

The fix must keep a reference to the abandoned drain worker and skip starting
a new drain (with a WARNING) while it is still alive.
"""
import json
import logging
import threading
import time

from pipeline import notification_email, notification_outbox
from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus

# The name ``run_once`` gives its drain worker; used only to wait for a worker
# to actually exit before asserting on the state it left behind.
_DRAIN_THREAD_NAME = "scheduler-drain-worker"


def _daemon():
    return mod.SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=lambda: None,
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
    )


def _wait_for_drain_workers_to_exit(timeout: float = 5.0) -> bool:
    """Block until no drain worker thread is alive (or ``timeout`` elapses)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [
            t
            for t in threading.enumerate()
            if t.name == _DRAIN_THREAD_NAME and t.is_alive()
        ]
        if not alive:
            return True
        time.sleep(0.01)
    return False


def _outbox_path(tmp_path):
    return tmp_path / f"p{notification_outbox.OUTBOX_SUFFIX}"


def _write_records(path, ids):
    with open(path, "w", encoding="utf-8") as fh:
        for record_id in ids:
            fh.write(json.dumps({"id": record_id}) + "\n")


def _append_record(path, record_id):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": record_id}) + "\n")


def _read_ids(path):
    if not path.exists():
        return []
    return [
        json.loads(line)["id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_tick_two_does_not_start_a_second_drain_while_the_first_is_wedged(
    monkeypatch, caplog
):
    """Tick 2 must not start a second concurrent drain over a wedged tick 1.

    The reviewer's required case: tick 1's drain worker is abandoned by the
    join timeout but is still alive; the next tick must skip the drain (and
    say so at WARNING) instead of starting a second drainer.
    """
    release = threading.Event()
    entered = threading.Event()
    entries = []
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def wedged_drain(*args, **kwargs):
        nonlocal active, max_active
        with state_lock:
            entries.append(time.monotonic())
            active += 1
            max_active = max(max_active, active)
        entered.set()
        try:
            release.wait(30)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(notification_outbox, "drain_outbox", wedged_drain)
    monkeypatch.setattr(mod, "_DRAIN_JOIN_TIMEOUT_SECONDS", 0.5)

    daemon = _daemon()
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        daemon.run_once()  # tick 1: the drain wedges; the join times out
        assert entered.wait(5), "the drain never started"
        caplog.clear()
        daemon.run_once()  # tick 2: tick 1's drain worker is still wedged
    release.set()
    _wait_for_drain_workers_to_exit()

    assert len(entries) == 1, (
        "tick 2 started a second concurrent drain while tick 1's drain worker "
        f"was still wedged (drain_outbox entered {len(entries)} times)"
    )
    assert max_active == 1, (
        f"{max_active} drain_outbox calls ran concurrently; drains must be "
        "serialized so the outbox merge cannot drop a notification"
    )

    tick2_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any(
        "drain" in message.lower() and "exceeded" not in message.lower()
        for message in tick2_warnings
    ), (
        "tick 2 skipped the drain without logging a WARNING about the still "
        f"alive drain worker; warnings seen: {tick2_warnings!r}"
    )


def test_a_notification_appended_while_the_drain_is_wedged_is_not_lost(
    monkeypatch, tmp_path
):
    """End-to-end consequence: r2 spooled during a wedged drain must survive.

    Buggy interleaving (two drainers): A accepts r1 and rewrites ``[]``, a
    producer appends r2, B re-reads ``[r2]`` with ``len(raw_lines) == 1``,
    computes ``appended == []`` and rewrites ``[]`` -- r2 is gone.
    """
    monkeypatch.setattr(notification_outbox, "PLAN_DIR", tmp_path)
    outbox_path = _outbox_path(tmp_path)
    _write_records(outbox_path, ["r1"])

    gate = threading.Event()
    first_call = threading.Event()
    calls = []

    def sender(record):
        calls.append(record)
        if len(calls) == 1:
            first_call.set()
            gate.wait(30)
        return True

    real_drain = notification_outbox.drain_outbox
    drain_done = threading.Event()

    def counting_drain(*args, **kwargs):
        try:
            return real_drain(*args, **kwargs)
        finally:
            drain_done.set()

    monkeypatch.setattr(notification_outbox, "drain_outbox", counting_drain)
    monkeypatch.setattr(notification_email, "send_notification_email", sender)
    monkeypatch.setattr(mod, "_DRAIN_JOIN_TIMEOUT_SECONDS", 0.5)

    daemon = _daemon()
    daemon.run_once()  # tick 1: the drain wedges inside sender(r1)
    assert first_call.wait(5), "the drain never reached the sender"

    # A producer spools a second notification while the drain is wedged.
    _append_record(outbox_path, "r2")

    daemon.run_once()  # tick 2: must NOT start a second, concurrent drain
    gate.set()

    assert drain_done.wait(5), "the drain never finished"
    _wait_for_drain_workers_to_exit()

    remaining = _read_ids(outbox_path)
    assert remaining == ["r2"], (
        "the notification spooled while the drain was wedged was silently "
        f"dropped by a concurrent drain; outbox now holds {remaining!r}"
    )


def test_drain_resumes_once_the_abandoned_worker_finishes(monkeypatch, tmp_path):
    """The abandoned-worker reference must not block drains forever.

    Tick 2 is skipped while tick 1's worker is alive; once that worker
    finishes, the next tick must start a fresh drain.
    """
    monkeypatch.setattr(notification_outbox, "PLAN_DIR", tmp_path)
    _write_records(_outbox_path(tmp_path), ["r1"])

    gate = threading.Event()
    first_call = threading.Event()
    drain_starts = []
    real_drain = notification_outbox.drain_outbox

    def counting_drain(*args, **kwargs):
        drain_starts.append(time.monotonic())
        return real_drain(*args, **kwargs)

    def sender(record):
        if not first_call.is_set():
            first_call.set()
            gate.wait(30)
        return True

    monkeypatch.setattr(notification_outbox, "drain_outbox", counting_drain)
    monkeypatch.setattr(notification_email, "send_notification_email", sender)
    monkeypatch.setattr(mod, "_DRAIN_JOIN_TIMEOUT_SECONDS", 0.5)

    daemon = _daemon()
    daemon.run_once()  # tick 1: the drain wedges
    assert first_call.wait(5), "the drain never reached the sender"

    daemon.run_once()  # tick 2: skipped while tick 1's worker is alive
    assert len(drain_starts) == 1, (
        "tick 2 started a second concurrent drain while tick 1's drain worker "
        "was still wedged"
    )

    gate.set()
    assert _wait_for_drain_workers_to_exit(), (
        "the abandoned drain worker never finished"
    )

    daemon.run_once()  # tick 3: the worker is gone, so a fresh drain must run
    assert len(drain_starts) == 2, (
        "the dead abandoned-worker reference blocked the next drain; drains "
        "must resume once the abandoned worker has exited"
    )
