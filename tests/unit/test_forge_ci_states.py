"""FORGE-S2: the CI state machine lives above the adapter, the snapshot below.

``pipeline/ci.py`` keeps the host-neutral poll loop and state machine; the
GitHub adapter owns the one-shot snapshot classifier (``checks_snapshot``) and
the three CI verbs (``has_ci_config``, ``failure_excerpt``, ``rerun_failed``).

Two invariants are asserted from both sides here:

* ``UNREADABLE`` never collapses into ``NONE``. ``NONE`` means "no CI
  configured" and permits merging without checks; ``UNREADABLE`` means "CI is
  configured but the status could not be read" and refuses the merge.
* ``PIPELINE_MERGE_CI_GATE=0`` returns ``PASS`` before any adapter is
  consulted.

The adapter's snapshot method is named ``checks_snapshot`` rather than
``checks_status`` because ``GitHubForge.checks_status`` is pinned by
``tests/unit/test_forge_github_pr.py`` to delegate to
``pipeline.ci._ci_status_once`` (the S1 direction). Keeping the snapshot under
its own name lets both contracts hold without a delegation cycle.
"""

import subprocess

import pytest

from pipeline import ci as p
from pipeline.forge import (
    CANCELLED,
    FAIL,
    NONE,
    PASS,
    PENDING,
    UNREADABLE,
    CIStatusUnreadable,
)
from pipeline.forge_github import GitHubForge

BRANCH = "agent/x-1"
SHA = "abc123"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeForge:
    """A forge double that records every method call.

    ``snapshots`` is a queue of one-shot snapshots; the last one repeats once
    the queue is exhausted. ``raises`` makes ``checks_snapshot`` raise.
    """

    def __init__(self, *, has_ci=True, snapshots=None, raises=None):
        self.calls = []
        self._has_ci = has_ci
        self._snapshots = list(snapshots or [])
        self._raises = raises

    def has_ci_config(self, repo_root):
        self.calls.append(("has_ci_config", repo_root))
        return self._has_ci

    def checks_snapshot(self, branch, sha):
        self.calls.append(("checks_snapshot", branch, sha))
        if self._raises is not None:
            raise self._raises
        if not self._snapshots:
            return {}
        if len(self._snapshots) == 1:
            return self._snapshots[0]
        return self._snapshots.pop(0)

    def rerun_failed(self, sha):
        self.calls.append(("rerun_failed", sha))
        return True

    def failure_excerpt(self, branch):
        self.calls.append(("failure_excerpt", branch))

    def method_calls(self, name):
        return [c for c in self.calls if c[0] == name]


class ExplodingForge:
    """A forge double that fails the test if the adapter is consulted at all."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError(f"adapter consulted: {name}")

        return _boom


class _Clock:
    """A monotonic clock that advances one tick per read."""

    def __init__(self):
        self.t = 0

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def fake_forge(monkeypatch):
    """Install a FakeForge as the adapter ``pipeline.ci`` resolves."""

    def _install(**kwargs):
        forge = FakeForge(**kwargs)
        monkeypatch.setattr(p, "get_forge", lambda repo_root: forge)
        monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
        return forge

    return _install


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(p.time, "sleep", lambda seconds: None)
    return monkeypatch


# ---------------------------------------------------------------------------
# The six canonical states through the adapter
# ---------------------------------------------------------------------------
def test_pass_state(fake_forge):
    fake_forge(has_ci=True, snapshots=[{"build": PASS}])
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == PASS


def test_fail_state(fake_forge):
    fake_forge(has_ci=True, snapshots=[{"build": FAIL}])
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == FAIL


def test_pending_state(fake_forge):
    fake_forge(has_ci=True, snapshots=[{"build": PENDING}])
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == PENDING


def test_cancelled_state(fake_forge, no_sleep):
    forge = fake_forge(has_ci=True, snapshots=[{"build": CANCELLED}])
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == CANCELLED

    # Going through the poll loop, a cancelled snapshot triggers one rerun.
    forge = fake_forge(
        has_ci=True, snapshots=[{"build": CANCELLED}, {"build": PASS}]
    )
    no_sleep.setattr(p.time, "monotonic", _Clock())
    assert p._ci_status(BRANCH, sha=SHA, timeout_s=10)["state"] == PASS
    assert len(forge.method_calls("rerun_failed")) == 1


def test_unreadable_state(fake_forge):
    fake_forge(has_ci=True, raises=CIStatusUnreadable("gh exploded"))
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == UNREADABLE


def test_none_state(fake_forge):
    # No CI configured and no checks registered -> NONE, never UNREADABLE.
    fake_forge(has_ci=False, snapshots=[{}])
    state = p._ci_status_once(BRANCH, sha=SHA)["state"]
    assert state == NONE
    assert state != UNREADABLE


# ---------------------------------------------------------------------------
# Adapter vocabulary: legacy commit-status buckets and check-runs
# ---------------------------------------------------------------------------
def _patch_run(monkeypatch, stdout):
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stderr = ""

        r = R()
        r.stdout = stdout
        return r

    monkeypatch.setattr(subprocess, "run", _fake_run)


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("pass", PASS),
        ("fail", FAIL),
        ("pending", PENDING),
        ("cancel", CANCELLED),
        ("skipping", PASS),
    ],
)
def test_github_bucket_vocabulary(monkeypatch, bucket, expected):
    import json

    _patch_run(
        monkeypatch,
        json.dumps([{"name": "build", "bucket": bucket}]),
    )
    snapshot = GitHubForge().checks_snapshot(BRANCH, "")
    assert snapshot["build"] == expected


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "success", PASS),
        ("completed", "failure", FAIL),
        ("in_progress", None, PENDING),
        ("queued", None, PENDING),
        ("completed", "cancelled", CANCELLED),
        ("completed", "timed_out", FAIL),
        ("completed", "skipped", PASS),
    ],
)
def test_check_runs_vocabulary(monkeypatch, status, conclusion, expected):
    import json

    _patch_run(
        monkeypatch,
        json.dumps({"name": "build", "status": status, "conclusion": conclusion})
        + "\n",
    )
    snapshot = GitHubForge().checks_snapshot(BRANCH, SHA)
    assert snapshot["build"] == expected


def test_completed_without_conclusion_is_unreadable(monkeypatch):
    import json

    _patch_run(
        monkeypatch,
        json.dumps({"name": "build", "status": "completed", "conclusion": None})
        + "\n",
    )
    with pytest.raises(CIStatusUnreadable):
        GitHubForge().checks_snapshot(BRANCH, SHA)


def test_unnamed_checks_do_not_collapse_into_one(monkeypatch):
    """Two unnamed checks must stay two entries.

    Keying both on ``""`` would let a cancelled check overwrite a passing one
    (or vice versa) and silently change the aggregate state.
    """
    import json

    _patch_run(
        monkeypatch,
        json.dumps([{"bucket": "cancelled"}, {"bucket": "pass"}]),
    )
    snapshot = GitHubForge().checks_snapshot(BRANCH, "")
    assert len(snapshot) == 2
    assert set(snapshot.values()) == {CANCELLED, PASS}


# ---------------------------------------------------------------------------
# UNREADABLE vs NONE: the inviolable distinction, asserted both directions
# ---------------------------------------------------------------------------
def test_unreadable_when_workflows_exist_but_status_unreadable(fake_forge):
    fake_forge(has_ci=True, raises=CIStatusUnreadable("HTTP 502"))
    state = p._ci_status_once(BRANCH, sha=SHA)["state"]
    assert state == UNREADABLE
    assert state != NONE


def test_none_when_no_workflows_and_no_checks(fake_forge):
    fake_forge(has_ci=False, snapshots=[{}])
    state = p._ci_status_once(BRANCH, sha=SHA)["state"]
    assert state == NONE
    assert state != PENDING


def test_unreadable_never_collapses_to_none(fake_forge):
    # (a) CI configured but unreadable -> UNREADABLE, never NONE.
    fake_forge(has_ci=True, raises=CIStatusUnreadable("boom"))
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == UNREADABLE

    # (b) No CI configured -> NONE, never UNREADABLE. Same call site, no cache.
    fake_forge(has_ci=False, snapshots=[{}])
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == NONE


def test_empty_checks_before_registration_is_pending(fake_forge):
    forge = fake_forge(has_ci=True, snapshots=[{}, {"build": PASS}])
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == PENDING
    # The empty snapshot leaves no sticky flag: the next poll can pass.
    assert p._ci_status_once(BRANCH, sha=SHA)["state"] == PASS
    assert len(forge.method_calls("checks_snapshot")) == 2


# ---------------------------------------------------------------------------
# The poll loop: timeout, gate short-circuit, one rerun per cancelled episode
# ---------------------------------------------------------------------------
def test_timeout_yields_pending(fake_forge, no_sleep):
    forge = fake_forge(has_ci=True, snapshots=[{"build": PENDING}])
    no_sleep.setattr(p.time, "monotonic", _Clock())
    result = p._ci_status(BRANCH, sha=SHA, timeout_s=3)
    assert result["state"] == PENDING
    assert len(forge.method_calls("checks_snapshot")) == 2


def test_gate_zero_short_circuits_to_pass_without_adapter(monkeypatch):
    forge = ExplodingForge()
    monkeypatch.setattr(p, "get_forge", lambda repo_root: forge)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", False)
    result = p._ci_status(BRANCH, sha=SHA, timeout_s=10)
    assert result["state"] == PASS
    assert forge.calls == []


def test_cancelled_triggers_exactly_one_rerun(fake_forge, no_sleep):
    # A cancellation that the rerun does not clear is reported as CANCELLED,
    # and the rerun is attempted exactly ONCE - never on every cancelled poll.
    forge = fake_forge(
        has_ci=True, snapshots=[{"build": CANCELLED}, {"build": CANCELLED}]
    )
    no_sleep.setattr(p.time, "monotonic", _Clock())
    assert p._ci_status(BRANCH, sha=SHA, timeout_s=10)["state"] == CANCELLED
    assert len(forge.method_calls("rerun_failed")) == 1

    # A cancellation the rerun DOES clear resumes polling and passes.
    forge = fake_forge(
        has_ci=True, snapshots=[{"build": CANCELLED}, {"build": PASS}]
    )
    no_sleep.setattr(p.time, "monotonic", _Clock())
    assert p._ci_status(BRANCH, sha=SHA, timeout_s=10)["state"] == PASS
    assert len(forge.method_calls("rerun_failed")) == 1

    # A second invocation is a new cancelled episode: the flag is local, so it
    # reruns once more - proving it is not a module-level global.
    forge = fake_forge(
        has_ci=True, snapshots=[{"build": CANCELLED}, {"build": PASS}]
    )
    no_sleep.setattr(p.time, "monotonic", _Clock())
    assert p._ci_status(BRANCH, sha=SHA, timeout_s=10)["state"] == PASS
    assert len(forge.method_calls("rerun_failed")) == 1
