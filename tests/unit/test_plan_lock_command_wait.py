"""Bounded wait for the plan lock on operator commands.

The scheduler tick holds the plan flock for a whole tick, so an operator
command arriving mid-tick used to answer ``skipped: "locked"`` on its very
first attempt and the operator polled by hand. ``_plan_lock`` now takes an
opt-in ``wait_seconds`` budget; the scheduler's own callers keep the default
0 (single attempt, no sleeping).

Every threaded test joins with a timeout and asserts the thread is not alive,
so a deadlock or a retry loop that never expires shows up as a failure rather
than a hung suite.
"""

import ast
import threading
import time
from pathlib import Path

import pytest

from pipeline import concurrency
from pipeline import service_story_ops as story_ops
from pipeline.store import FileStore

PLAN = "p-lock"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point both the lock and the store at a throwaway plans directory."""
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)
    monkeypatch.setattr("pipeline.store.PLAN_DIR", tmp_path)
    monkeypatch.delenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, raising=False)
    return tmp_path


class _Holder:
    """A real flock holder in its own thread."""

    def __init__(self, plan_name: str, release_after: float | None):
        self.plan_name = plan_name
        self.release_after = release_after
        self.started = threading.Event()
        self.release = threading.Event()
        self.acquired = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with concurrency._plan_lock(self.plan_name) as acquired:
            self.acquired = acquired
            self.started.set()
            if self.release_after is None:
                self.release.wait(timeout=10)
            else:
                time.sleep(self.release_after)

    def __enter__(self):
        self.thread.start()
        assert self.started.wait(timeout=5), "holder thread never took the lock"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "holder thread did not exit"


def test_wait_zero_gives_up_immediately_while_held(plan_dir):
    with _Holder(PLAN, release_after=None) as holder:
        assert holder.acquired
        started = time.monotonic()
        with concurrency._plan_lock(PLAN, wait_seconds=0) as acquired:
            assert acquired is False
        elapsed = time.monotonic() - started
    # No budget: one attempt (plus the short in-process contention grace).
    assert elapsed < 0.5
    # No budget leak either: a free lock is taken at once, no inherited sleep.
    started = time.monotonic()
    with concurrency._plan_lock(PLAN, wait_seconds=0) as acquired:
        assert acquired is True
    assert time.monotonic() - started < 0.5


def test_positive_wait_acquires_after_holder_releases(plan_dir):
    with _Holder(PLAN, release_after=0.2):
        started = time.monotonic()
        with concurrency._plan_lock(PLAN, wait_seconds=2) as acquired:
            elapsed = time.monotonic() - started
            assert acquired is True
    assert 0.15 <= elapsed <= 1.5


def test_wait_times_out_against_a_holder_that_never_releases(plan_dir):
    with _Holder(PLAN, release_after=None):
        started = time.monotonic()
        with concurrency._plan_lock(PLAN, wait_seconds=0.1) as acquired:
            elapsed = time.monotonic() - started
            assert acquired is False
    assert 0.09 <= elapsed <= 0.6
    # No budget leak: the next caller must not inherit an expired deadline.
    started = time.monotonic()
    with concurrency._plan_lock(PLAN, wait_seconds=0) as acquired:
        assert acquired is True
    assert time.monotonic() - started < 0.5


def _fake_self():
    return object()


def _seed_plan(tmp_path: Path) -> None:
    (tmp_path / f"{PLAN}.manifest.json").write_text(
        '{"stories": {"s1": {"key": "s1", "status": "todo", "summary": "one"}}}'
    )


def _patch_story(monkeypatch, tmp_path: Path):
    """Call the real ``patch_story`` impl against tmp_path, gates stubbed out."""
    _seed_plan(tmp_path)
    store = FileStore()
    monkeypatch.setattr(story_ops, "_store", store, raising=False)
    monkeypatch.setattr(story_ops, "_validate_key", lambda key: None, raising=False)
    monkeypatch.setattr(story_ops, "_notify_user", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(story_ops, "approval_enabled", lambda: False)
    monkeypatch.setattr("pipeline.authorization.authorize", lambda *a, **k: None)
    return story_ops._patch_story_impl(
        _fake_self(), PLAN, "s1", {"summary": "edited while contending"}
    )


def test_patch_story_waits_for_a_holder_that_releases(plan_dir, monkeypatch):
    monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, "1")
    with _Holder(PLAN, release_after=0.2):
        result = _patch_story(monkeypatch, plan_dir)
    assert result.get("ok") is True
    assert "skipped" not in result
    assert result["story"]["summary"] == "edited while contending"


def test_patch_story_reports_retry_hint_when_the_wait_expires(plan_dir, monkeypatch):
    monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, "0.2")
    with _Holder(PLAN, release_after=None):
        result = _patch_story(monkeypatch, plan_dir)
    assert result["ok"] is True
    assert result["skipped"] == "locked"
    assert result["retry_after_seconds"] == pytest.approx(0.2)


def _transaction_calls(path: Path, operator_functions: set[str] = frozenset()):
    """Yield ``(enclosing function name, call)`` for every ``.transaction()``."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in operator_functions:
            # Operator commands are the only callers allowed to wait.
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "transaction"
            ):
                yield node.name, inner


def test_scheduler_internal_callers_never_ask_for_a_wait():
    root = Path(__file__).resolve().parents[2] / "pipeline"
    seen = 0
    for module, operators in (("service.py", {"interrupt_story"}), ("ingest.py", set())):
        for function, call in _transaction_calls(root / module, operators):
            seen += 1
            keywords = [kw.arg for kw in call.keywords]
            assert "wait_seconds" not in keywords, (
                f"{module}:{call.lineno} ({function}) asks the plan lock to wait "
                "— the scheduler must keep today's fail-fast behaviour"
            )
            assert not call.args[1:], (
                f"{module}:{call.lineno} ({function}) passes a positional wait "
                "— the scheduler must keep today's fail-fast behaviour"
            )
    assert seen >= 2, "no .transaction() call sites found to inspect"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0.0),
        (None, 30.0),
        ("-5", 30.0),
        ("500", 120.0),
        ("abc", 30.0),
        ("", 30.0),
        ("nan", 30.0),
    ],
)
def test_env_resolution(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, raw)
    assert concurrency.command_lock_wait_seconds() == pytest.approx(expected)
    # Re-read on every call: call #2 must not reuse a cached value from #1.
    monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, "0")
    assert concurrency.command_lock_wait_seconds() == 0.0
    monkeypatch.delenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR)
    assert concurrency.command_lock_wait_seconds() == pytest.approx(30.0)


def test_two_waiters_on_each_others_plan_both_time_out(plan_dir):
    """Lock ordering hazard: neither waiter may block forever."""
    barrier = threading.Barrier(2, timeout=5)
    results: dict[str, tuple[bool, float]] = {}

    def worker(mine: str, theirs: str) -> None:
        # Hold my own lock for the whole window in which the other thread is
        # waiting on it, so both waits are guaranteed to expire.
        with concurrency._plan_lock(mine) as acquired:
            assert acquired is True
            barrier.wait()
            started = time.monotonic()
            with concurrency._plan_lock(theirs, wait_seconds=0.2) as got:
                results[mine] = (got, time.monotonic() - started)
            time.sleep(0.3)

    a = threading.Thread(target=worker, args=("plan-a", "plan-b"), daemon=True)
    b = threading.Thread(target=worker, args=("plan-b", "plan-a"), daemon=True)
    a.start()
    b.start()
    a.join(timeout=5)
    b.join(timeout=5)
    assert not a.is_alive(), "waiter on plan-a hung instead of timing out"
    assert not b.is_alive(), "waiter on plan-b hung instead of timing out"
    for name in ("plan-a", "plan-b"):
        got, elapsed = results[name]
        assert got is False
        assert 0.15 <= elapsed <= 2.0


# -- the other two operator commands that must wait ------------------------
def _interrupt_story(monkeypatch, tmp_path: Path):
    from pipeline import service as service_mod

    _seed_plan(tmp_path)
    monkeypatch.setattr(service_mod, "_store", FileStore(), raising=False)
    monkeypatch.setattr(service_mod, "_validate_key", lambda key: None, raising=False)
    monkeypatch.setattr("pipeline.authorization.authorize", lambda *a, **k: None)
    return service_mod.PipelineService.interrupt_story(_fake_self(), PLAN, "s1")


def test_interrupt_story_waits_for_a_holder_that_releases(plan_dir, monkeypatch):
    monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, "1")
    with _Holder(PLAN, release_after=0.2):
        result = _interrupt_story(monkeypatch, plan_dir)
    # It got the lock and ran on: the seeded story is not dispatched yet.
    assert "skipped" not in result
    assert result == {"ok": False, "error": "Story not dispatched"}


def test_interrupt_story_reports_retry_hint_when_the_wait_expires(plan_dir, monkeypatch):
    monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, "0.2")
    with _Holder(PLAN, release_after=None):
        result = _interrupt_story(monkeypatch, plan_dir)
    assert result["ok"] is True
    assert result["skipped"] == "locked"
    assert result["retry_after_seconds"] == pytest.approx(0.2)


def _patch_plan(monkeypatch, tmp_path: Path):
    from pipeline import service_role_config as role_config

    _seed_plan(tmp_path)
    monkeypatch.setattr(role_config, "_store", FileStore(), raising=False)
    monkeypatch.setattr(role_config, "reset_if_changed", lambda manifest: False)
    return role_config._patch_plan_impl(_fake_self(), PLAN, {"role_config": {}})


def test_patch_plan_waits_for_a_holder_that_releases(plan_dir, monkeypatch):
    monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, "1")
    with _Holder(PLAN, release_after=0.2):
        result = _patch_plan(monkeypatch, plan_dir)
    assert result == {"ok": True, "plan_name": PLAN}


def test_patch_plan_reports_retry_hint_when_the_wait_expires(plan_dir, monkeypatch):
    monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, "0.2")
    with _Holder(PLAN, release_after=None):
        result = _patch_plan(monkeypatch, plan_dir)
    assert result["ok"] is True
    assert result["skipped"] == "locked"
    assert result["retry_after_seconds"] == pytest.approx(0.2)


# -- the SQLite store must honour the same contract ----------------------
def test_sqlite_transaction_wait_zero_is_still_one_attempt(tmp_path):
    from pipeline.store_sqlite import SqliteStore

    store = SqliteStore(str(tmp_path / "plans.db"))
    holder = threading.Thread(target=store._guard.acquire, daemon=True)
    holder.start()
    holder.join(timeout=5)
    try:
        started = time.monotonic()
        with store.transaction(PLAN, 0.0) as acquired:
            assert acquired is False
        assert time.monotonic() - started < 0.5
    finally:
        store._guard.release()


def test_sqlite_transaction_wait_acquires_after_the_holder_releases(tmp_path):
    from pipeline.store_sqlite import SqliteStore

    store = SqliteStore(str(tmp_path / "plans.db"))
    release = threading.Event()
    started_holding = threading.Event()

    def hold():
        with store.transaction(PLAN, 0.0) as acquired:
            assert acquired is True
            started_holding.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert started_holding.wait(timeout=5)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        with store.transaction(PLAN, 1.0) as acquired:
            assert acquired is True
    finally:
        timer.cancel()
        release.set()
        holder.join(timeout=5)
        assert not holder.is_alive()


def test_patch_story_with_a_sqlite_store_does_not_raise_typeerror(plan_dir, monkeypatch):
    from pipeline.store_sqlite import SqliteStore

    store = SqliteStore(str(plan_dir / "plans.db"))
    store.save_manifest(
        PLAN, {"stories": {"s1": {"key": "s1", "status": "todo", "summary": "one"}}}
    )
    monkeypatch.setattr(story_ops, "_store", store, raising=False)
    monkeypatch.setattr(story_ops, "_validate_key", lambda key: None, raising=False)
    monkeypatch.setattr(story_ops, "_notify_user", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(story_ops, "approval_enabled", lambda: False)
    monkeypatch.setattr("pipeline.authorization.authorize", lambda *a, **k: None)
    monkeypatch.setenv(concurrency.COMMAND_LOCK_WAIT_ENV_VAR, "0")
    result = story_ops._patch_story_impl(
        _fake_self(), PLAN, "s1", {"summary": "edited through sqlite"}
    )
    assert result.get("ok") is True, result
    assert result["story"]["summary"] == "edited through sqlite"
