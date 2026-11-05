"""Structured logging for plan-lock holds (issue 8584d77e).

Every real ``_plan_lock`` acquire/release must emit exactly one DEBUG
``plan lock released`` record carrying the plan name, the hold duration, the
number and total duration of ``_released_plan_lock`` windows, plus a second
INFO record with the same fields when the hold minus the released windows
crosses ``PIPELINE_PLAN_LOCK_SLOW_SECONDS``. A failed non-blocking acquire
emits exactly one INFO ``plan lock contended``. The reentrant fast path logs
nothing.

All tests patch ``pipeline.concurrency.PLAN_DIR`` at a ``tmp_path`` (the
module reads it as a bare global at call time) so the flock never touches the
real plans directory.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time

import pytest

from pipeline import concurrency

LOGGER_NAME = "pipeline.concurrency"
RELEASE_MESSAGE = "plan lock released"
CONTENDED_MESSAGE = "plan lock contended"
SLOW_ENV = "PIPELINE_PLAN_LOCK_SLOW_SECONDS"

RELEASE_EXTRA_KEYS = frozenset(
    {"plan", "held_seconds", "released_windows", "released_seconds"}
)
CONTENDED_EXTRA_KEYS = frozenset({"plan", "cross_process"})

# Attributes the logging/caplog machinery itself puts on a LogRecord; anything
# else in ``record.__dict__`` came from the ``extra=`` payload under test.
_STANDARD_RECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def capture(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        yield caplog


def _records(caplog, message):
    return [
        record
        for record in caplog.records
        if record.name == LOGGER_NAME and record.getMessage() == message
    ]


def _released(caplog):
    return _records(caplog, RELEASE_MESSAGE)


def _contended(caplog):
    return _records(caplog, CONTENDED_MESSAGE)


def _custom_keys(record):
    return {key for key in record.__dict__ if key not in _STANDARD_RECORD_KEYS}


def _hold(name, seconds):
    with concurrency._plan_lock(name) as acquired:
        assert acquired is True
        time.sleep(seconds)


# (1) ---------------------------------------------------------------------
def test_short_hold_logs_one_debug_record_only(plan_dir, capture):
    _hold("alpha", 0.005)

    released = _released(capture)
    assert len(released) == 1
    record = released[0]
    assert record.levelno == logging.DEBUG
    assert record.plan == "alpha"
    assert record.released_windows == 0
    assert record.released_seconds == 0.0
    assert 0.0 <= record.held_seconds < 5.0
    assert [r for r in released if r.levelno >= logging.INFO] == []


# (2) ---------------------------------------------------------------------
def test_slow_hold_logs_info_with_held_seconds(plan_dir, capture, monkeypatch):
    monkeypatch.setenv(SLOW_ENV, "0.01")

    _hold("beta", 0.05)

    released = _released(capture)
    assert [record.levelno for record in released] == [logging.DEBUG, logging.INFO]
    info = released[1]
    assert info.plan == "beta"
    assert info.held_seconds >= 0.04
    assert info.released_windows == 0
    assert info.released_seconds == 0.0


# (3) ---------------------------------------------------------------------
def test_released_window_time_is_excluded_from_slow_figure(
    plan_dir, capture, monkeypatch
):
    monkeypatch.setenv(SLOW_ENV, "0.03")

    with concurrency._plan_lock("gamma") as acquired:
        assert acquired is True
        time.sleep(0.005)
        with concurrency._released_plan_lock("gamma"):
            time.sleep(0.06)

    released = _released(capture)
    assert len(released) == 1
    record = released[0]
    assert record.levelno == logging.DEBUG
    assert record.released_windows == 1
    assert record.released_seconds >= 0.05
    assert record.held_seconds - record.released_seconds < 0.03


def test_multiple_released_windows_accumulate(plan_dir, capture, monkeypatch):
    monkeypatch.setenv(SLOW_ENV, "0.03")

    with concurrency._plan_lock("gamma") as acquired:
        assert acquired is True
        with concurrency._released_plan_lock("gamma"):
            time.sleep(0.010)
        with concurrency._released_plan_lock("gamma"):
            time.sleep(0.020)

    released = _released(capture)
    assert len(released) == 1
    record = released[0]
    assert record.released_windows == 2
    assert record.released_seconds >= 0.028
    assert record.levelno == logging.DEBUG


def test_raising_released_window_still_accounted(plan_dir, capture, monkeypatch):
    monkeypatch.setenv(SLOW_ENV, "0.03")

    with concurrency._plan_lock("gamma") as acquired:
        assert acquired is True
        with pytest.raises(RuntimeError), concurrency._released_plan_lock("gamma"):
            time.sleep(0.05)
            raise RuntimeError("boom")

    released = _released(capture)
    assert len(released) == 1
    record = released[0]
    assert record.released_windows == 1
    assert record.released_seconds >= 0.04
    assert record.levelno == logging.DEBUG


# (4) ---------------------------------------------------------------------
def test_failed_acquire_logs_contended_once(plan_dir, capture):
    lock_path = plan_dir / "delta.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with concurrency._plan_lock("delta") as acquired:
            assert acquired is False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    contended = _contended(capture)
    assert len(contended) == 1
    record = contended[0]
    assert record.levelno == logging.INFO
    assert record.plan == "delta"
    assert record.cross_process is True
    assert _released(capture) == []


def test_failed_acquire_does_not_record_a_start_time(plan_dir, capture, monkeypatch):
    monkeypatch.setenv(SLOW_ENV, "0.5")
    lock_path = plan_dir / "iota.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with concurrency._plan_lock("iota") as acquired:
            assert acquired is False
        time.sleep(0.2)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    _hold("iota", 0.005)

    released = _released(capture)
    assert len(released) == 1
    assert released[0].levelno == logging.DEBUG
    assert released[0].held_seconds < 0.4


# (5) ---------------------------------------------------------------------
def test_reentrant_nested_call_logs_nothing_extra(plan_dir, capture):
    with concurrency._plan_lock("epsilon") as outer:
        assert outer is True
        time.sleep(0.01)
        with concurrency._plan_lock("epsilon") as inner:
            assert inner is True
            time.sleep(0.01)
        assert _released(capture) == []
        assert _contended(capture) == []

    released = _released(capture)
    assert len(released) == 1
    record = released[0]
    assert record.levelno == logging.DEBUG
    assert record.held_seconds >= 0.015
    assert record.held_seconds < 0.5
    assert record.released_windows == 0


def test_state_is_not_carried_into_the_next_hold(plan_dir, capture, monkeypatch):
    monkeypatch.setenv(SLOW_ENV, "0.03")

    with concurrency._plan_lock("kappa") as acquired:
        assert acquired is True
        with concurrency._released_plan_lock("kappa"):
            time.sleep(0.05)

    _hold("kappa", 0.005)

    released = _released(capture)
    assert len(released) == 2
    second = released[1]
    assert second.released_windows == 0
    assert second.released_seconds == 0.0
    assert second.held_seconds < 0.05


# (6) ---------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["abc", "0", "-1", "", "   "])
def test_malformed_or_non_positive_threshold_falls_back_to_five(
    plan_dir, capture, monkeypatch, raw
):
    monkeypatch.setenv(SLOW_ENV, raw)

    _hold("zeta", 0.05)

    released = _released(capture)
    assert [record.levelno for record in released] == [logging.DEBUG]


def test_threshold_is_read_per_call(plan_dir, capture, monkeypatch):
    monkeypatch.setenv(SLOW_ENV, "0.01")
    _hold("theta", 0.02)
    monkeypatch.setenv(SLOW_ENV, "abc")
    _hold("theta", 0.02)

    levels = [record.levelno for record in _released(capture)]
    assert levels == [logging.DEBUG, logging.INFO, logging.DEBUG]


# (7) ---------------------------------------------------------------------
def test_extra_keys_are_exactly_the_documented_ones(plan_dir, capture, monkeypatch):
    monkeypatch.setenv(SLOW_ENV, "0.001")

    with concurrency._plan_lock("eta") as acquired:
        assert acquired is True
        time.sleep(0.02)
        with concurrency._released_plan_lock("eta"):
            time.sleep(0.005)

    released = _released(capture)
    assert len(released) == 2
    for record in released:
        assert _custom_keys(record) == RELEASE_EXTRA_KEYS

    lock_path = plan_dir / "eta.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with concurrency._plan_lock("eta") as acquired:
            assert acquired is False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    contended = _contended(capture)
    assert len(contended) == 1
    assert _custom_keys(contended[0]) == CONTENDED_EXTRA_KEYS


def test_no_secrets_in_messages_or_extra(plan_dir, capture, monkeypatch):
    monkeypatch.setenv(SLOW_ENV, "0.001")
    monkeypatch.setenv("PIPELINE_PLAN_LOCK_SECRET", "super-secret-value")

    with concurrency._plan_lock("theta") as acquired:
        assert acquired is True
        with concurrency._released_plan_lock("theta"):
            time.sleep(0.01)

    assert capture.records
    for record in capture.records:
        assert "super-secret-value" not in record.getMessage()
        assert str(plan_dir) not in record.getMessage()
        for value in _custom_keys(record):
            assert "super-secret-value" not in str(getattr(record, value))
            assert str(plan_dir) not in str(getattr(record, value))
