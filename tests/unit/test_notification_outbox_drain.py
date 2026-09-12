"""Tests for the outbox drain step (``pipeline.notification_outbox.drain_outbox``)
and its wiring into the scheduler tick (``pipeline.scheduler_daemon.run_once``).

Written FIRST (TDD) for the drain story.  ``drain_outbox`` does not exist yet,
so every drain test below fails with ``AttributeError`` and the two wiring
tests fail at the ``monkeypatch.setattr`` of the not-yet-existing
``drain_outbox`` attribute -- that is the intended RED state for this
dispatch.

Contract under test (see REFERENCE.md sink rule 2 -- network I/O stays out of
the notification path):

* ``drain_outbox(plan_name: str, sender) -> int`` reads every spooled record
  from ``PLAN_DIR / f"{plan_name}.outbox.jsonl"`` and hands each parsed record
  to ``sender`` (a callable taking one record dict and returning a bool; in
  production ``pipeline.notification_email.send_notification_email``).
* ``sender`` is an EXPLICIT argument, never imported inside the function, so
  tests inject a fake without patching a module global.
* A missing outbox file returns 0 without calling the sender and without
  creating the file.  An empty file returns 0 too.
* A line that fails to parse is logged at WARNING and skipped; one corrupt
  line must not block the rest of the file.
* Records the sender returned True for are removed; records it returned False
  for are RETAINED for the next drain (a transient SMTP outage must not lose
  a notification).  The rewrite is atomic: retained lines go to a temp file
  in the SAME directory and ``os.replace`` moves it over the original.  When
  nothing is retained the file is rewritten EMPTY (this suite pins that
  choice; removal is the rejected alternative).
* A sender that RAISES is treated exactly like a False return: the record is
  retained, the exception is logged at ERROR, and the drain continues.
* ``drain_outbox`` never raises and returns the count of successfully sent
  records.
* ``run_once`` calls the drain once per tick as its OWN phase, after the
  existing scan and reconcile phases, wrapped in its own try/except that logs
  and swallows: a drain failure must not propagate and must not change the
  tick's existing ``scanned``/``reconciled`` accounting.  The daemon imports
  ``drain_outbox`` and ``send_notification_email`` lazily (function-local) so
  importing the daemon does not pull in smtplib.
* ``outbox_sink`` must still exist untouched on the same module.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import notification_outbox, paths, persistence
from pipeline import scheduler_daemon as daemon_mod
from pipeline.events import InProcessEventBus

OUTBOX_ENABLED_ENV = "PIPELINE_NOTIFY_OUTBOX_ENABLED"
OUTBOX_EVENTS_ENV = "PIPELINE_NOTIFY_OUTBOX_EVENTS"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _patch_plan_dir(monkeypatch, target):
    """Point ``PLAN_DIR`` at ``target`` everywhere the outbox may read it.

    ``persistence`` re-binds ``PLAN_DIR`` from ``.paths`` at import time and
    the outbox module re-binds it again; patch the canonical location, the
    re-bound name, and the outbox module's own binding (if present).
    """
    monkeypatch.setattr(paths, "PLAN_DIR", target)
    monkeypatch.setattr(persistence, "PLAN_DIR", target)
    monkeypatch.setattr(daemon_mod, "PLAN_DIR", target)
    if hasattr(notification_outbox, "PLAN_DIR"):
        monkeypatch.setattr(notification_outbox, "PLAN_DIR", target)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Isolated ``PLAN_DIR`` plus a clean notification-outbox environment."""
    _patch_plan_dir(monkeypatch, tmp_path)
    monkeypatch.delenv(OUTBOX_ENABLED_ENV, raising=False)
    monkeypatch.delenv(OUTBOX_EVENTS_ENV, raising=False)
    return tmp_path


def _outbox_path(plan_dir, plan="p1"):
    return Path(plan_dir) / f"{plan}.outbox.jsonl"


def _spool(path, records):
    """Append ``records`` to the JSONL outbox exactly like ``outbox_sink``."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(record) + "\n" for record in records)


def _write_raw(path, text):
    path.write_text(text, encoding="utf-8")


def _retained_records(path):
    """Parse the non-empty lines still in the outbox as JSON records."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _record(n):
    """A realistic spooled notification record."""
    return {
        "type": "notification",
        "plan": "p1",
        "payload": {"event": "plan_completed", "message": f"plan finished {n}"},
    }


class Sender:
    """Scriptable fake sender: one result per call; Exception results raise."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def __call__(self, record):
        self.calls.append(record)
        outcome = self.results.pop(0) if self.results else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    """A controllable monotonic clock for daemon tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


class Counter:
    """A callable that records call count and can be made to raise."""

    def __init__(self, raise_on_call: bool = False) -> None:
        self.calls = 0
        self.raise_on_call = raise_on_call

    def __call__(self):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("boom")


def make_daemon(interval_s=60, start=0.0):
    clock = FakeClock(start)
    reconcile_fn = Counter()
    scan_fn = Counter()
    bus = InProcessEventBus()
    daemon = daemon_mod.SchedulerDaemon(
        reconcile_fn=reconcile_fn,
        scan_fn=scan_fn,
        bus=bus,
        interval_s=interval_s,
        sleep_fn=lambda _s: None,
        clock=clock,
    )
    return daemon, clock, reconcile_fn, scan_fn


# ---------------------------------------------------------------------------
# drain_outbox -- signature / module contract
# ---------------------------------------------------------------------------

def test_drain_outbox_exists_with_documented_signature():
    """``drain_outbox(plan_name: str, sender) -> int``: two named parameters."""
    drain = notification_outbox.drain_outbox
    assert callable(drain)
    params = list(inspect.signature(drain).parameters)
    assert params == ["plan_name", "sender"]


def test_drain_outbox_takes_sender_as_argument_not_an_import():
    """``sender`` is injected: no import statement inside the function body."""
    source = inspect.getsource(notification_outbox.drain_outbox)
    tree = ast.parse(source)
    fn = tree.body[0]
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    imports_inside = [
        node for node in ast.walk(fn)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert imports_inside == [], (
        "drain_outbox must receive the sender as an explicit argument, not "
        "import it (or anything else) inside its own body"
    )


def test_outbox_sink_is_still_present_on_the_module():
    """The drain story must not clobber the existing ``outbox_sink``."""
    assert callable(notification_outbox.outbox_sink)


# ---------------------------------------------------------------------------
# drain_outbox -- happy paths
# ---------------------------------------------------------------------------

def test_drain_sends_all_records_in_file_order_and_empties_outbox(plan_dir):
    """Three spooled records, sender always True -> 3, in order, file empty."""
    path = _outbox_path(plan_dir)
    records = [_record(1), _record(2), _record(3)]
    _spool(path, records)
    sender = Sender()

    result = notification_outbox.drain_outbox("p1", sender)

    assert result == 3
    assert isinstance(result, int)
    assert sender.calls == records  # parsed dicts, in file order
    # Pinned choice: with nothing retained the file is rewritten EMPTY (it
    # still exists, with no non-empty lines) rather than removed.
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() == ""


def test_drain_single_record_is_sent_and_removed(plan_dir):
    """Boundary: exactly one spooled record -> sent once, file left empty."""
    path = _outbox_path(plan_dir)
    record = _record(1)
    _spool(path, [record])
    sender = Sender()

    assert notification_outbox.drain_outbox("p1", sender) == 1
    assert sender.calls == [record]
    assert path.read_text(encoding="utf-8").strip() == ""


def test_drain_removes_only_records_the_sender_accepted(plan_dir):
    """True/False/True -> returns 2 and the outbox retains exactly record 2."""
    path = _outbox_path(plan_dir)
    records = [_record(1), _record(2), _record(3)]
    _spool(path, records)
    sender = Sender(results=[True, False, True])

    result = notification_outbox.drain_outbox("p1", sender)

    assert result == 2
    assert sender.calls == [records[0], records[1], records[2]]
    assert _retained_records(path) == [records[1]]


# ---------------------------------------------------------------------------
# drain_outbox -- negative / boundary cases
# ---------------------------------------------------------------------------

def test_missing_outbox_file_returns_zero_without_touching_anything(plan_dir):
    """Absent file -> 0, sender never called, no raise, no file created."""
    path = _outbox_path(plan_dir)
    sender = Sender()

    result = notification_outbox.drain_outbox("p1", sender)

    assert result == 0
    assert sender.calls == []
    assert not path.exists(), "drain must not create a missing outbox file"


def test_empty_outbox_file_returns_zero(plan_dir):
    """Empty file -> 0, no raise, sender never called."""
    path = _outbox_path(plan_dir)
    path.write_text("", encoding="utf-8")
    sender = Sender()

    result = notification_outbox.drain_outbox("p1", sender)

    assert result == 0
    assert sender.calls == []


def test_malformed_line_is_skipped_with_warning_and_good_ones_still_sent(
    plan_dir, caplog
):
    """One corrupt line among three good ones: 3 sent, WARNING, drain goes on."""
    path = _outbox_path(plan_dir)
    records = [_record(1), _record(2), _record(3)]
    _write_raw(
        path,
        json.dumps(records[0]) + "\n"
        + "{this is not json}\n"
        + json.dumps(records[1]) + "\n"
        + json.dumps(records[2]) + "\n",
    )
    sender = Sender()

    with caplog.at_level(logging.WARNING):
        result = notification_outbox.drain_outbox("p1", sender)

    assert result == 3
    assert sender.calls == records
    assert any(r.levelno == logging.WARNING for r in caplog.records), (
        "a malformed JSON line must be logged at WARNING"
    )


def test_file_of_only_malformed_lines_returns_zero(plan_dir, caplog):
    """Every line corrupt -> 0, no raise, sender never called."""
    path = _outbox_path(plan_dir)
    _write_raw(path, "{nope}\n[also not\nnot json at all\n")
    sender = Sender()

    with caplog.at_level(logging.WARNING):
        result = notification_outbox.drain_outbox("p1", sender)

    assert result == 0
    assert sender.calls == []


def test_blank_lines_do_not_break_the_drain(plan_dir):
    """Boundary: stray blank lines around a good record are tolerated."""
    path = _outbox_path(plan_dir)
    record = _record(1)
    _write_raw(path, "\n" + json.dumps(record) + "\n\n")
    sender = Sender()

    result = notification_outbox.drain_outbox("p1", sender)

    assert result == 1
    assert sender.calls == [record]


def test_sender_exception_is_logged_record_retained_and_drain_continues(
    plan_dir, caplog
):
    """Sender raises on the 2nd of 3: 1st and 3rd attempted, 2nd retained."""
    path = _outbox_path(plan_dir)
    records = [_record(1), _record(2), _record(3)]
    _spool(path, records)
    sender = Sender(results=[True, RuntimeError("smtp down"), True])

    with caplog.at_level(logging.ERROR):
        result = notification_outbox.drain_outbox("p1", sender)

    assert result == 2
    assert sender.calls == records
    assert _retained_records(path) == [records[1]]
    assert any(r.levelno == logging.ERROR for r in caplog.records), (
        "a raising sender must be logged at ERROR"
    )


def test_sender_false_for_every_record_retains_the_file_byte_for_byte(plan_dir):
    """All-False sender -> 0 sent and the outbox content is unchanged."""
    path = _outbox_path(plan_dir)
    records = [_record(1), _record(2)]
    _spool(path, records)
    before = path.read_bytes()
    sender = Sender(results=[False, False])

    result = notification_outbox.drain_outbox("p1", sender)

    assert result == 0
    assert sender.calls == records
    assert path.read_bytes() == before, (
        "records the sender rejected must be retained byte-for-byte"
    )


def test_rewrite_is_atomic_original_survives_a_failed_replace(
    plan_dir, monkeypatch
):
    """If ``os.replace`` fails the original file still holds every record."""
    path = _outbox_path(plan_dir)
    records = [_record(1), _record(2), _record(3)]
    _spool(path, records)
    before = path.read_bytes()
    captured = {}

    def fake_replace(src, dst):
        captured["src"] = src
        captured["dst"] = dst
        raise OSError("replace failed on purpose")

    monkeypatch.setattr(notification_outbox.os, "replace", fake_replace)
    sender = Sender()

    try:
        notification_outbox.drain_outbox("p1", sender)
    except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
        pytest.fail(f"drain_outbox must never raise, but raised: {exc!r}")

    assert captured, "the drain must publish via os.replace"
    # The temp file must live in the SAME directory as the outbox so the
    # replace is atomic (same filesystem).
    assert Path(captured["src"]).parent == path.parent
    assert Path(captured["dst"]) == path
    assert path.read_bytes() == before, (
        "a failed replace must leave the original outbox intact"
    )
    assert _retained_records(path) == records


# ---------------------------------------------------------------------------
# run_once wiring -- the drain runs once per tick, guarded
# ---------------------------------------------------------------------------

def test_run_once_calls_the_drain(plan_dir, monkeypatch):
    """One tick drains the outbox at least once, with a str plan + callable."""
    (Path(plan_dir) / "p1.manifest.json").write_text("{}", encoding="utf-8")
    daemon, _clock, _reconcile_fn, scan_fn = make_daemon()
    calls = []

    def fake_drain(plan_name, sender):
        calls.append((plan_name, sender))
        return 0

    monkeypatch.setattr(notification_outbox, "drain_outbox", fake_drain)

    result = daemon.run_once()

    assert scan_fn.calls == 1  # the existing scan phase still ran
    assert len(calls) >= 1, "run_once must call the drain every tick"
    for plan_name, sender in calls:
        assert isinstance(plan_name, str)
        assert callable(sender)
    assert result == {"scanned": True, "reconciled": False}


def test_run_once_drain_failure_does_not_propagate_or_change_return_keys(
    plan_dir, monkeypatch, caplog
):
    """A raising drain is logged and swallowed; the tick's dict is unchanged."""
    (Path(plan_dir) / "p1.manifest.json").write_text("{}", encoding="utf-8")
    daemon, _clock, _reconcile_fn, scan_fn = make_daemon()

    def exploding_drain(plan_name, sender):
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(notification_outbox, "drain_outbox", exploding_drain)

    with caplog.at_level(logging.ERROR):
        result = daemon.run_once()

    assert result == {"scanned": True, "reconciled": False}
    assert scan_fn.calls == 1
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "a drain failure inside run_once must be logged"
    )


def test_importing_scheduler_daemon_does_not_pull_in_smtplib():
    """The daemon's drain imports must be lazy: no smtplib at import time."""
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    probe = (
        "import sys, pipeline.scheduler_daemon; "
        "sys.exit(0 if 'smtplib' not in sys.modules else 1)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        "importing pipeline.scheduler_daemon must not pull in smtplib "
        "(the drain imports must be function-local/lazy); stderr:\n"
        f"{proc.stderr}"
    )