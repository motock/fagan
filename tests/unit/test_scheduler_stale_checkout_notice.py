"""SSA-1: the running scheduler must announce a stale checkout.

A merged orchestration change is invisible to the daemon until it is
restarted: the process imports ``pipeline.*`` at start and never hot-reloads.
``health()`` already publishes ``checkout_behind_origin``; these tests pin the
advisory that turns that number into a one-per-upstream-revision notice, and
only when the missing commits actually touch code.
"""
import json
from types import SimpleNamespace

import pytest

from pipeline import paths, persistence
from pipeline import scheduler_daemon as sd


@pytest.fixture(autouse=True)
def _clear_dedup_set():
    """Start every test from an empty dedup set, preserving its identity."""
    sd._STALE_CHECKOUT_NOTIFIED.clear()
    yield
    sd._STALE_CHECKOUT_NOTIFIED.clear()


class _FakeGit:
    """Stand-in for ``subprocess.run`` that answers the two git probes."""

    def __init__(self, upstream_sha="abc1234", diff="pipeline/x.py\n", rc=0):
        self.upstream_sha = upstream_sha
        self.diff = diff
        self.rc = rc
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if "rev-parse" in cmd:
            return SimpleNamespace(
                returncode=self.rc, stdout=f"{self.upstream_sha}\n", stderr=""
            )
        return SimpleNamespace(returncode=self.rc, stdout=self.diff, stderr="")


@pytest.fixture
def fake_git(monkeypatch):
    fake = _FakeGit()
    monkeypatch.setattr(sd.subprocess, "run", fake)
    return fake


@pytest.fixture
def notify(monkeypatch):
    """Record every ``_notify_user`` call instead of writing a sidecar."""
    calls = []

    def _fake(plan_name, message, **kwargs):
        calls.append({"plan_name": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(persistence, "_notify_user", _fake)
    return calls


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    return directory


def _daemon():
    return sd.SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=lambda: None,
        bus=object(),
    )


def _helper():
    fn = getattr(sd.SchedulerDaemon, "_notify_if_checkout_stale", None)
    assert fn is not None, (
        "pipeline.scheduler_daemon.SchedulerDaemon._notify_if_checkout_stale() "
        "is missing: the daemon must notify when the checkout it runs from "
        "trails upstream by code commits"
    )
    return fn


def test_behind_zero_never_probes_git(monkeypatch, notify, plan_dir):
    """A current checkout must not spend a subprocess on every health write."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "subprocess.run must not be called when checkout_behind_origin is 0"
        )

    monkeypatch.setattr(sd.subprocess, "run", _boom)
    _helper()(_daemon(), {"checkout_behind_origin": 0})
    assert notify == []


def test_missing_behind_key_never_probes_git(monkeypatch, notify, plan_dir):
    """A health dict without the key (older payload) is treated as current."""

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called without a behind count")

    monkeypatch.setattr(sd.subprocess, "run", _boom)
    _helper()(_daemon(), {})
    assert notify == []


def test_code_commits_notify_once_per_upstream_revision(
    fake_git, notify, plan_dir
):
    """One notice per upstream SHA, with the reload guidance and dedup key."""
    (plan_dir / "demo.manifest.json").write_text("{}", encoding="utf-8")
    daemon = _daemon()

    _helper()(daemon, {"checkout_behind_origin": 2})

    assert len(notify) == 1, "a code-touching stale checkout must notify exactly once"
    call = notify[0]
    assert call["plan_name"] == "demo"
    assert call["event"] == "scheduler_reload_required"
    assert call["dedup_key"] == "scheduler_stale:abc1234"
    assert "scripts/reload_pipeline_daemon.sh" in call["message"]
    assert "2 commit(s) behind" in call["message"]
    assert "pipeline/x.py" in call["message"]
    assert sd._STALE_CHECKOUT_NOTIFIED == {"abc1234"}

    # The git probes must be bounded and non-raising.
    assert len(fake_git.calls) == 2
    for _cmd, kwargs in fake_git.calls:
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 10

    # Same revision again: still exactly one notice.
    _helper()(daemon, {"checkout_behind_origin": 2})
    assert len(notify) == 1, "the same upstream revision must not notify twice"
    assert sd._STALE_CHECKOUT_NOTIFIED == {"abc1234"}

    # A new upstream revision notifies again, and the set accumulates.
    fake_git.upstream_sha = "def5678"
    fake_git.diff = "app/y.py\n"
    _helper()(daemon, {"checkout_behind_origin": 3})
    assert len(notify) == 2, "a new upstream revision must notify again"
    assert notify[1]["dedup_key"] == "scheduler_stale:def5678"
    assert "3 commit(s) behind" in notify[1]["message"]
    assert "app/y.py" in notify[1]["message"]
    assert sd._STALE_CHECKOUT_NOTIFIED == {"abc1234", "def5678"}


def test_docs_only_diff_stays_silent(fake_git, notify, plan_dir):
    """Doc-only merges change nothing the daemon executes."""
    (plan_dir / "demo.manifest.json").write_text("{}", encoding="utf-8")
    fake_git.diff = "docs/x.md\n"

    _helper()(_daemon(), {"checkout_behind_origin": 2})

    assert notify == []
    assert sd._STALE_CHECKOUT_NOTIFIED == set()


def test_failed_git_probe_stays_silent(fake_git, notify, plan_dir):
    """A broken probe (no upstream, not a repo) must not raise or notify."""
    (plan_dir / "demo.manifest.json").write_text("{}", encoding="utf-8")
    fake_git.rc = 1

    _helper()(_daemon(), {"checkout_behind_origin": 2})

    assert notify == []
    assert sd._STALE_CHECKOUT_NOTIFIED == set()


def test_empty_plan_dir_stays_silent_and_does_not_suppress_later_notice(
    fake_git, notify, plan_dir
):
    """No plan to address means no notice — and no premature dedup entry."""
    daemon = _daemon()

    _helper()(daemon, {"checkout_behind_origin": 2})

    assert notify == []
    assert sd._STALE_CHECKOUT_NOTIFIED == set(), (
        "a skipped notice must not mark the revision as already notified"
    )

    (plan_dir / "demo.manifest.json").write_text("{}", encoding="utf-8")
    _helper()(daemon, {"checkout_behind_origin": 2})

    assert len(notify) == 1, "the same revision must still notify once a plan exists"
    assert notify[0]["plan_name"] == "demo"
    assert sd._STALE_CHECKOUT_NOTIFIED == {"abc1234"}


def test_failed_notification_does_not_mark_revision(monkeypatch, fake_git, plan_dir):
    """A raising ``_notify_user`` must leave the revision retryable."""
    (plan_dir / "demo.manifest.json").write_text("{}", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise RuntimeError("notification sink unavailable")

    monkeypatch.setattr(persistence, "_notify_user", _boom)

    with pytest.raises(RuntimeError):
        _helper()(_daemon(), {"checkout_behind_origin": 2})

    assert sd._STALE_CHECKOUT_NOTIFIED == set(), (
        "the SHA must be recorded only after the notification succeeds"
    )


def test_write_health_survives_a_raising_staleness_check(monkeypatch, tmp_path):
    """The advisory runs after the atomic write and never breaks it."""
    daemon = _daemon()
    monkeypatch.setattr(
        daemon,
        "health",
        lambda: {"alive": True, "checkout_behind_origin": 2},
    )
    monkeypatch.setattr(daemon, "config_fingerprint", lambda: {"plan_dir": "x"})

    def _boom(health):
        raise RuntimeError("staleness check exploded")

    monkeypatch.setattr(daemon, "_notify_if_checkout_stale", _boom)

    path = tmp_path / "health.json"
    daemon.write_health(str(path))

    assert path.exists(), "the health file must be durable before the advisory runs"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["checkout_behind_origin"] == 2
    assert data["config"] == {"plan_dir": "x"}
    assert not (tmp_path / "health.json.tmp").exists()
