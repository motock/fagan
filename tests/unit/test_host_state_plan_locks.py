"""FileHostState owns the plan lock and the heavy lock (flock on host-local files)."""

import threading
import time

import pytest

from pipeline import server
from pipeline.host_state import FileHostState


@pytest.fixture
def root(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", d)
    return d


def test_should_refuse_non_blocking_plan_lock_while_held(root):
    host = FileHostState()
    with host.plan_lock("alpha") as first:
        assert first.acquired is True
        assert (root / "alpha.lock").exists()
        with host.plan_lock("alpha") as second:
            assert second.acquired is False


def test_should_grant_plan_lock_after_bounded_wait_when_holder_releases(root):
    host = FileHostState()
    ready = threading.Event()
    release = threading.Event()

    def holder():
        with host.plan_lock("alpha") as held:
            assert held.acquired is True
            ready.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert ready.wait(5)
    threading.Timer(0.1, release.set).start()
    try:
        with host.plan_lock("alpha", wait_seconds=5.0) as waited:
            assert waited.acquired is True
    finally:
        thread.join(5)


def test_should_refuse_plan_lock_when_bounded_wait_expires(root):
    host = FileHostState()
    with host.plan_lock("alpha") as held:
        assert held.acquired is True
        started = time.monotonic()
        with host.plan_lock("alpha", wait_seconds=0.1) as waited:
            assert waited.acquired is False
        assert time.monotonic() - started < 2.0


def test_should_release_plan_lock_when_body_raises(root):
    host = FileHostState()
    with pytest.raises(RuntimeError, match="boom"), host.plan_lock("alpha") as held:
        assert held.acquired is True
        raise RuntimeError("boom")
    with host.plan_lock("alpha") as again:
        assert again.acquired is True


def test_should_exclude_second_heavy_lock_holder_until_first_releases(root):
    host = FileHostState()
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first():
        with host.heavy_lock():
            entered.set()
            release.wait(5)

    def second():
        with host.heavy_lock():
            second_entered.set()

    first_thread = threading.Thread(target=first)
    first_thread.start()
    assert entered.wait(5)
    second_thread = threading.Thread(target=second)
    second_thread.start()
    assert not second_entered.wait(0.2)
    release.set()
    first_thread.join(5)
    second_thread.join(5)
    assert second_entered.is_set()
    assert (root / "heavy.lock").exists()


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", "", "..", "x y", "a\x00b"])
def test_should_refuse_invalid_plan_name_before_creating_any_lock_file(
    root, tmp_path, bad_name
):
    with pytest.raises(ValueError), FileHostState().plan_lock(bad_name):
        pass
    assert list(root.iterdir()) == []
    assert not (tmp_path / "escape.lock").exists()
