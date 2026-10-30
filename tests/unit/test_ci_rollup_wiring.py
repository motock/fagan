"""TDD tests for serving CI checks from the per-repo rollup and waking plans.

``GitHubForge.checks_snapshot`` used to spend one REST call per open PR.
With ``PIPELINE_CI_ROLLUP_MAX_AGE_SECONDS`` positive (default 90) it serves a
fresh entry of the per-repository rollup cache
(``pipeline.ci_rollup.cached_snapshot``) and only falls back to the REST call
when the entry is missing or aged. The scheduler refreshes that rollup at most
once per repository per ``PIPELINE_CI_ROLLUP_INTERVAL_SECONDS`` (default 30)
for repositories with a ``pr_open`` story carrying a ``ci_pending_sha``, and
wakes the plan exactly once when the sha's aggregate state stops being pending.

Everything here drives the real ``GitHubForge``, ``pipeline.ci_rollup`` and
``pipeline.watchers.scan_all_plans``; only the ``gh`` subprocess, the clock
seam and the rollup fetch/store boundary are patched.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

# pipeline.server is imported explicitly first: a pre-existing import cycle
# makes some pipeline modules unimportable cold in any other order.
import pipeline.server  # noqa: F401
from pipeline import ci_rollup, paths, watchers
from pipeline.forge import FAIL, PASS, PENDING, CISnapshot, CIStatusUnreadable
from pipeline.forge_github import GitHubForge

MAX_AGE_ENV = "PIPELINE_CI_ROLLUP_MAX_AGE_SECONDS"
INTERVAL_ENV = "PIPELINE_CI_ROLLUP_INTERVAL_SECONDS"
SHA = "a" * 40


class FakeBus:
    """A minimal bus that records every published event."""

    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


class FakeGh:
    """Records every ``gh`` argv and replays one recorded REST answer."""

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.argv: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, *args, **kwargs):
        self.argv.append(list(argv))
        return subprocess.CompletedProcess(
            args=list(argv),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr="",
        )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point the state root and every in-process cache at this test only."""
    state = tmp_path / "state"
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(state))
    monkeypatch.delenv(MAX_AGE_ENV, raising=False)
    monkeypatch.delenv(INTERVAL_ENV, raising=False)
    monkeypatch.setattr(watchers, "_LAST_ROLLUP_REFRESH", {}, raising=False)
    monkeypatch.setattr(watchers, "_PREVIOUS_CI_STATE", {}, raising=False)
    monkeypatch.setattr(watchers, "_PREVIOUS_STATUS", {}, raising=False)
    yield state
    ci_rollup._OWNER_CACHE.clear()


def _repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    return str(repo)


def _seed_cache(state_root: Path, repo_root: str, sha: str, states: dict, age_s: float):
    """Write one rollup cache entry whose ``fetched_at`` is *age_s* seconds old."""
    import time

    target = ci_rollup._cache_path(repo_root, state_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"fetched_at": time.time() - age_s, "shas": {sha: states}})
    )


def _rest_run(run_id="1", conclusion="failure"):
    return json.dumps({"name": "unit", "status": "completed", "conclusion": conclusion})


# ---------------------------------------------------------------------------
# checks_snapshot: the cache short-circuit
# ---------------------------------------------------------------------------


def test_checks_snapshot_fresh_cache_entry_is_returned_without_a_gh_call(
    tmp_path, monkeypatch, _isolated_state
):
    repo_root = _repo(tmp_path)
    _seed_cache(_isolated_state, repo_root, SHA, {"unit": PASS}, age_s=1)
    monkeypatch.setattr("pipeline.ci._scoped_repo_cwd", lambda: repo_root)
    gh = FakeGh(_rest_run(conclusion="failure"))
    monkeypatch.setattr("pipeline.forge_github.subprocess.run", gh)

    snapshot = GitHubForge().checks_snapshot("feature", SHA)

    assert snapshot == CISnapshot({"unit": PASS})
    assert gh.argv == []


def test_checks_snapshot_aged_cache_entry_falls_back_to_rest(tmp_path, monkeypatch, _isolated_state):
    repo_root = _repo(tmp_path)
    _seed_cache(_isolated_state, repo_root, SHA, {"unit": PASS}, age_s=10_000)
    monkeypatch.setattr("pipeline.ci._scoped_repo_cwd", lambda: repo_root)
    gh = FakeGh(_rest_run(conclusion="failure"))
    monkeypatch.setattr("pipeline.forge_github.subprocess.run", gh)

    snapshot = GitHubForge().checks_snapshot("feature", SHA)

    assert snapshot == CISnapshot({"unit": FAIL})
    assert len(gh.argv) == 1


def test_checks_snapshot_missing_cache_entry_falls_back_to_rest(tmp_path, monkeypatch, _isolated_state):
    repo_root = _repo(tmp_path)
    monkeypatch.setattr("pipeline.ci._scoped_repo_cwd", lambda: repo_root)
    gh = FakeGh(_rest_run(conclusion="success"))
    monkeypatch.setattr("pipeline.forge_github.subprocess.run", gh)

    snapshot = GitHubForge().checks_snapshot("feature", SHA)

    assert snapshot == CISnapshot({"unit": PASS})
    assert len(gh.argv) == 1


def test_checks_snapshot_max_age_zero_never_reads_the_cache(tmp_path, monkeypatch, _isolated_state):
    repo_root = _repo(tmp_path)
    # A fresh entry that says PASS: reading it would mask the failing REST
    # answer, so any read here is observable as a wrong pass.
    _seed_cache(_isolated_state, repo_root, SHA, {"unit": PASS}, age_s=0)
    monkeypatch.setenv(MAX_AGE_ENV, "0")
    monkeypatch.setattr("pipeline.ci._scoped_repo_cwd", lambda: repo_root)
    gh = FakeGh(_rest_run(conclusion="failure"))
    monkeypatch.setattr("pipeline.forge_github.subprocess.run", gh)

    snapshot = GitHubForge().checks_snapshot("feature", SHA)

    assert snapshot == CISnapshot({"unit": FAIL})
    assert len(gh.argv) == 1


def test_checks_snapshot_empty_sha_never_reads_the_cache(tmp_path, monkeypatch, _isolated_state):
    repo_root = _repo(tmp_path)
    _seed_cache(_isolated_state, repo_root, "", {"unit": PASS}, age_s=0)
    monkeypatch.setattr("pipeline.ci._scoped_repo_cwd", lambda: repo_root)
    gh = FakeGh(json.dumps([{"name": "unit", "bucket": "fail"}]))
    monkeypatch.setattr("pipeline.forge_github.subprocess.run", gh)

    snapshot = GitHubForge().checks_snapshot("feature", "")

    assert snapshot == CISnapshot({"unit": FAIL})
    assert gh.argv[0][:2] == ["gh", "pr"]


@pytest.mark.parametrize("raw", ["nonsense", "-5", "nan", "inf"])
def test_checks_snapshot_bad_max_age_logs_a_warning_and_uses_the_default(
    raw, tmp_path, monkeypatch, caplog, _isolated_state
):
    repo_root = _repo(tmp_path)
    _seed_cache(_isolated_state, repo_root, SHA, {"unit": PASS}, age_s=1)
    monkeypatch.setenv(MAX_AGE_ENV, raw)
    monkeypatch.setattr("pipeline.ci._scoped_repo_cwd", lambda: repo_root)
    gh = FakeGh(_rest_run(conclusion="failure"))
    monkeypatch.setattr("pipeline.forge_github.subprocess.run", gh)

    with caplog.at_level(logging.WARNING, logger=ci_rollup.__name__):
        snapshot = GitHubForge().checks_snapshot("feature", SHA)

    assert snapshot == CISnapshot({"unit": PASS})
    assert gh.argv == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


# ---------------------------------------------------------------------------
# scan_all_plans: refreshing the per-repo rollup on a CI change
# ---------------------------------------------------------------------------


def _manifest(plan: str, repo_root: str, *, sha: str | None = SHA) -> dict:
    story = {"status": "pr_open", "worktree": repo_root, "branch": f"sq/{plan}"}
    if sha is not None:
        story["ci_pending_sha"] = sha
    return {"plan": plan, "repo_root": repo_root, "stories": {"S-1": story}}


def _write_manifest(plan_dir: Path, plan: str, manifest: dict) -> None:
    (plan_dir / f"{plan}.manifest.json").write_text(json.dumps(manifest))


@pytest.fixture
def fetches(monkeypatch):
    """Patch the rollup fetch/store boundary and record what the scan did."""
    calls = {"fetch": [], "store": []}
    answers: list[dict] = []

    def fake_fetch(repo_root):
        calls["fetch"].append(repo_root)
        if answers:
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return {}

    def fake_store(repo_root, rollup, state_root):
        calls["store"].append(repo_root)

    monkeypatch.setattr(watchers.ci_rollup, "fetch_rollup", fake_fetch)
    monkeypatch.setattr(watchers.ci_rollup, "store_rollup", fake_store)
    calls["answers"] = answers
    return calls


@pytest.fixture
def clock(monkeypatch):
    now = {"value": 1_000.0}

    def fake_now():
        return now["value"]

    monkeypatch.setattr(watchers, "_ci_rollup_now", fake_now, raising=False)
    return now


def _ci_events(events):
    return [
        event
        for event in events
        if event.get("payload", {}).get("reason") == "ci_complete"
    ]


def test_scan_refreshes_a_pending_repo_once_per_interval(tmp_path, monkeypatch, fetches, clock):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    repo_root = _repo(tmp_path)
    _write_manifest(tmp_path, "alpha", _manifest("alpha", repo_root))

    for _ in range(3):
        watchers.scan_all_plans(FakeBus())
        clock["value"] += 1  # three scans well inside the 30s default interval

    assert fetches["fetch"] == [repo_root]

    clock["value"] += 30
    watchers.scan_all_plans(FakeBus())

    assert fetches["fetch"] == [repo_root, repo_root]


def test_scan_publishes_one_ci_reconcile_when_the_sha_stops_pending(
    tmp_path, monkeypatch, fetches, clock
):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    repo_root = _repo(tmp_path)
    _write_manifest(tmp_path, "alpha", _manifest("alpha", repo_root))
    fetches["answers"].extend(
        [
            {SHA: CISnapshot({"unit": PENDING})},
            {SHA: CISnapshot({"unit": PASS})},
        ]
    )

    bus = FakeBus()
    first = watchers.scan_all_plans(bus)
    clock["value"] += 60
    second = watchers.scan_all_plans(bus)

    assert _ci_events(first) == []
    ci_events = _ci_events(second)
    assert len(ci_events) == 1
    assert ci_events[0]["plan"] == "alpha"


def test_scan_publishes_nothing_while_the_sha_stays_pending(tmp_path, monkeypatch, fetches, clock):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    repo_root = _repo(tmp_path)
    _write_manifest(tmp_path, "alpha", _manifest("alpha", repo_root))
    fetches["answers"].extend(
        [
            {SHA: CISnapshot({"unit": PENDING})},
            {SHA: CISnapshot({"unit": PENDING})},
        ]
    )

    bus = FakeBus()
    events = watchers.scan_all_plans(bus)
    clock["value"] += 60
    events += watchers.scan_all_plans(bus)

    assert _ci_events(events) == []


def test_scan_publishes_once_for_a_sha_first_seen_already_final(
    tmp_path, monkeypatch, fetches, clock
):
    """After a restart the previous state is unknown: a final sha still wakes once."""
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    repo_root = _repo(tmp_path)
    _write_manifest(tmp_path, "alpha", _manifest("alpha", repo_root))
    fetches["answers"].extend(
        [
            {SHA: CISnapshot({"unit": PASS})},
            {SHA: CISnapshot({"unit": PASS})},
        ]
    )

    bus = FakeBus()
    first = watchers.scan_all_plans(bus)
    clock["value"] += 60
    second = watchers.scan_all_plans(bus)

    assert len(_ci_events(first)) == 1
    assert _ci_events(second) == []


def test_scan_logs_a_warning_when_the_fetch_raises_and_keeps_its_events(
    tmp_path, monkeypatch, fetches, clock, caplog
):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    repo_root = _repo(tmp_path)
    _write_manifest(tmp_path, "alpha", _manifest("alpha", repo_root))
    # A second plan whose marker must still be reported: the CI step may never
    # break the rest of the scan.
    worktree = tmp_path / "beta-wt"
    worktree.mkdir()
    (worktree / ".agent_done").write_text(json.dumps({"exit_code": 0}))
    _write_manifest(
        tmp_path,
        "beta",
        {"plan": "beta", "stories": {"B-1": {"status": "in_progress", "worktree": str(worktree)}}},
    )
    fetches["answers"].append(CIStatusUnreadable("boom"))

    with caplog.at_level(logging.WARNING, logger=watchers.__name__):
        events = watchers.scan_all_plans(FakeBus())

    assert [event["story_key"] for event in events] == ["B-1"]
    assert fetches["store"] == []
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "CIStatusUnreadable" in record.getMessage()
    ]
    assert warnings


def test_scan_never_fetches_a_repository_without_a_pending_story(
    tmp_path, monkeypatch, fetches, clock
):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    repo_root = _repo(tmp_path)
    _write_manifest(tmp_path, "alpha", _manifest("alpha", repo_root, sha=None))

    watchers.scan_all_plans(FakeBus())

    assert fetches["fetch"] == []


def test_scan_stores_the_fetched_rollup_under_the_configured_state_root(
    tmp_path, monkeypatch, fetches, clock, _isolated_state
):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    repo_root = _repo(tmp_path)
    _write_manifest(tmp_path, "alpha", _manifest("alpha", repo_root))
    fetches["answers"].append({SHA: CISnapshot({"unit": PENDING})})
    seen = []
    monkeypatch.setattr(
        watchers.ci_rollup,
        "store_rollup",
        lambda root, rollup, state_root: seen.append((root, rollup, state_root)),
    )

    watchers.scan_all_plans(FakeBus())

    assert seen == [(repo_root, {SHA: CISnapshot({"unit": PENDING})}, _isolated_state)]
