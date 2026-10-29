"""TDD tests for the GraphQL CI rollup fetch and its on-disk cache.

``pipeline/ci_rollup.py`` collapses the per-sha REST call in
``GitHubForge.checks_snapshot`` into ONE ``gh api graphql`` round trip that
carries the check state of every open PR of a repository, plus a short-lived
cache keyed by PR head sha so repeated advance ticks do not re-fetch.

Everything here drives the REAL ``fetch_rollup`` / ``store_rollup`` /
``cached_snapshot`` with ``subprocess.run`` patched on
``pipeline.ci_rollup.subprocess``, so the argv the module issues and the JSON
it parses are both observable.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

# pipeline.server is imported explicitly first: a pre-existing import cycle
# makes some pipeline modules unimportable cold in any other order.
import pipeline.server  # noqa: F401
from pipeline import ci_rollup
from pipeline.forge import (
    FAIL,
    PASS,
    PENDING,
    CISnapshot,
    CIStatusUnreadable,
)
from pipeline.forge_github import GitHubForge


# --------------------------------------------------------------------------
# recorded ``gh`` fixtures
# --------------------------------------------------------------------------
def _completed(argv, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=list(argv), returncode=returncode, stdout=stdout, stderr=stderr
    )


def _check_run(name, conclusion="SUCCESS", status="COMPLETED"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


def _status_context(context, state):
    return {"__typename": "StatusContext", "context": context, "state": state}


def _pr(sha, *contexts, rollup_present=True):
    """One ``pullRequests.nodes`` entry for ``sha`` with ``contexts``."""
    rollup = {"contexts": {"nodes": list(contexts)}} if rollup_present else None
    return {
        "headRefOid": sha,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
    }


def _response(*prs):
    return {"data": {"repository": {"pullRequests": {"nodes": list(prs)}}}}


class FakeGh:
    """A stand-in for the ``gh`` CLI: records argv, returns recorded JSON."""

    def __init__(
        self,
        response=None,
        *,
        owner="octo",
        name="repo",
        graphql_returncode=0,
        graphql_stdout=None,
        graphql_stderr="",
        owner_failures=0,
        raise_timeout=False,
    ):
        self.response = _response() if response is None else response
        self.owner = owner
        self.name = name
        self.graphql_returncode = graphql_returncode
        self.graphql_stdout = graphql_stdout
        self.graphql_stderr = graphql_stderr
        self.owner_failures = owner_failures
        self.raise_timeout = raise_timeout
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[:3] == ["gh", "repo", "view"]:
            if self.owner_failures:
                self.owner_failures -= 1
                return _completed(argv, returncode=1, stderr="boom")
            payload = {"name": self.name, "owner": {"login": self.owner}}
            return _completed(argv, stdout=json.dumps(payload))
        if argv[:3] == ["gh", "api", "graphql"]:
            if self.raise_timeout:
                raise subprocess.TimeoutExpired(
                    cmd=argv, timeout=20, output="SECRET"
                )
            if self.graphql_stdout is not None:
                stdout = self.graphql_stdout
            else:
                stdout = json.dumps(self.response)
            return _completed(
                argv,
                stdout=stdout,
                returncode=self.graphql_returncode,
                stderr=self.graphql_stderr,
            )
        raise AssertionError(f"unexpected gh argv: {argv}")

    def graphql_calls(self):
        return [c for c in self.calls if c[0][:3] == ["gh", "api", "graphql"]]

    def owner_calls(self):
        return [c for c in self.calls if c[0][:3] == ["gh", "repo", "view"]]


def _install(monkeypatch, fake):
    monkeypatch.setattr(ci_rollup.subprocess, "run", fake)
    return fake


# --------------------------------------------------------------------------
# fetch_rollup
# --------------------------------------------------------------------------
def test_two_prs_match_checks_snapshot_states(monkeypatch, tmp_path):
    repo_root = str(tmp_path / "repo")
    fake = _install(
        monkeypatch,
        FakeGh(
            _response(
                _pr("a1", _check_run("build")),
                _pr(
                    "b2",
                    _check_run("build", conclusion="FAILURE"),
                    _status_context("lint", "PENDING"),
                ),
            )
        ),
    )

    rollup = ci_rollup.fetch_rollup(repo_root)

    assert set(rollup) == {"a1", "b2"}
    assert isinstance(rollup["a1"], CISnapshot)
    assert dict(rollup["a1"]) == {"build": PASS}
    assert dict(rollup["b2"]) == {"build": FAIL, "lint": PENDING}

    # The check-run half is classified by the SAME vocabulary the per-sha REST
    # path uses, so the two paths can never disagree about one check. The REST
    # payload carries the enum in lower case; the rollup normalises GraphQL's
    # upper case to it.
    rest = GitHubForge._classify_check_runs(
        json.dumps({"name": "build", "status": "completed", "conclusion": "success"})
    )
    assert dict(rollup["a1"]) == dict(rest)
    assert dict(rollup["a1"]) == {"build": PASS}

    calls = fake.graphql_calls()
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert kwargs["timeout"] == 20
    assert kwargs["cwd"] == repo_root
    joined = " ".join(argv)
    assert "pullRequests(states: OPEN, first: 100)" in joined
    assert "headRefOid" in joined
    assert "statusCheckRollup" in joined
    assert "contexts(first: 100)" in joined
    assert "owner=octo" in joined
    assert "name=repo" in joined


def test_check_runs_pending_and_cancelled_states(monkeypatch, tmp_path):
    rollup = None
    _install(
        monkeypatch,
        FakeGh(
            _response(
                _pr(
                    "a1",
                    _check_run("build", status="IN_PROGRESS", conclusion=None),
                    _check_run("deploy", conclusion="CANCELLED"),
                )
            )
        ),
    )

    rollup = ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    assert dict(rollup["a1"]) == {"build": PENDING, "deploy": "cancelled"}


def test_unknown_check_run_conclusion_omits_that_sha(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeGh(
            _response(
                _pr("a1", _check_run("build", conclusion="MYSTERY")),
                _pr("b2", _check_run("build")),
            )
        ),
    )

    rollup = ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    assert set(rollup) == {"b2"}
    assert dict(rollup["b2"]) == {"build": PASS}


def test_unknown_status_context_state_omits_only_that_sha(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeGh(
            _response(
                _pr("a1", _status_context("lint", "NOT_A_REAL_STATE")),
                _pr("b2", _status_context("lint", "SUCCESS")),
            )
        ),
    )

    rollup = ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    assert set(rollup) == {"b2"}
    assert dict(rollup["b2"]) == {"lint": PASS}


def test_status_context_vocabulary(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeGh(
            _response(
                _pr(
                    "a1",
                    _status_context("ok", "SUCCESS"),
                    _status_context("bad", "FAILURE"),
                    _status_context("old", "ERROR"),
                    _status_context("wait", "PENDING"),
                )
            )
        ),
    )

    rollup = ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    assert dict(rollup["a1"]) == {
        "ok": PASS,
        "bad": FAIL,
        "old": FAIL,
        "wait": PENDING,
    }


def test_pr_without_checks_yields_empty_snapshot(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeGh(
            _response(
                _pr("empty", rollup_present=False),
                _pr("noctx"),
            )
        ),
    )

    rollup = ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    assert set(rollup) == {"empty", "noctx"}
    assert rollup["empty"] == {}
    assert rollup["noctx"] == {}
    assert isinstance(rollup["empty"], CISnapshot)


def test_owner_is_resolved_once_per_repo_root(monkeypatch, tmp_path):
    fake = _install(
        monkeypatch,
        FakeGh(_response(_pr("a1", _check_run("build")))),
    )
    repo_root = str(tmp_path / "repo")

    ci_rollup.fetch_rollup(repo_root)
    ci_rollup.fetch_rollup(repo_root)

    assert len(fake.owner_calls()) == 1
    assert len(fake.graphql_calls()) == 2


def test_failed_owner_lookup_is_not_cached(monkeypatch, tmp_path):
    fake = _install(
        monkeypatch,
        FakeGh(_response(_pr("a1", _check_run("build"))), owner_failures=1),
    )
    repo_root = str(tmp_path / "repo")

    with pytest.raises(CIStatusUnreadable):
        ci_rollup.fetch_rollup(repo_root)

    rollup = ci_rollup.fetch_rollup(repo_root)

    assert set(rollup) == {"a1"}
    assert len(fake.owner_calls()) == 2


# --------------------------------------------------------------------------
# error secrecy
# --------------------------------------------------------------------------
def test_nonzero_exit_raises_without_echoing_output(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeGh(
            graphql_returncode=1,
            graphql_stdout="SECRET",
            graphql_stderr="SECRET stderr",
        ),
    )

    with pytest.raises(CIStatusUnreadable) as excinfo:
        ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    message = str(excinfo.value)
    assert "SECRET" not in message
    assert str(tmp_path) not in message


def test_timeout_raises_without_echoing_output(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeGh(raise_timeout=True),
    )

    with pytest.raises(CIStatusUnreadable) as excinfo:
        ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    assert "SECRET" not in str(excinfo.value)


def test_non_json_output_raises_without_echoing_output(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeGh(graphql_stdout="not json SECRET"),
    )

    with pytest.raises(CIStatusUnreadable) as excinfo:
        ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    assert "SECRET" not in str(excinfo.value)


def test_missing_response_structure_raises(monkeypatch, tmp_path):
    _install(
        monkeypatch,
        FakeGh({"data": {"repository": None}, "errors": [{"message": "SECRET"}]}),
    )

    with pytest.raises(CIStatusUnreadable) as excinfo:
        ci_rollup.fetch_rollup(str(tmp_path / "repo"))

    assert "SECRET" not in str(excinfo.value)


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
def _cache_file(state_root: Path, repo_root: str) -> Path:
    digest = hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:16]
    return state_root / "ci_rollup" / f"{digest}.json"


def test_store_rollup_then_cached_snapshot_round_trips(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_rollup.time, "time", lambda: 1000.0)
    state_root = tmp_path / "state"
    repo_root = "/srv/repo/root"

    ci_rollup.store_rollup(repo_root, {"abc": CISnapshot({"build": PASS})}, state_root)

    snapshot = ci_rollup.cached_snapshot(repo_root, "abc", state_root, 300)

    assert isinstance(snapshot, CISnapshot)
    assert dict(snapshot) == {"build": PASS}

    cache_file = _cache_file(state_root, repo_root)
    assert cache_file.exists()
    stored = json.loads(cache_file.read_text())
    assert stored["fetched_at"] == 1000
    assert stored["shas"]["abc"] == {"build": PASS}

    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache_file.parent.stat().st_mode) == 0o700


def test_cache_file_name_is_sha256_of_repo_root(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_rollup.time, "time", lambda: 1000.0)
    state_root = tmp_path / "state"
    repo_root = "/srv/another/repo"

    ci_rollup.store_rollup(repo_root, {"abc": CISnapshot()}, state_root)

    entries = list((state_root / "ci_rollup").iterdir())
    expected = hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:16] + ".json"
    assert [entry.name for entry in entries] == [expected]


def test_aged_file_returns_none_and_is_never_refreshed(monkeypatch, tmp_path):
    now = [1000.0]
    monkeypatch.setattr(ci_rollup.time, "time", lambda: now[0])
    state_root = tmp_path / "state"
    repo_root = "/srv/repo/root"

    ci_rollup.store_rollup(repo_root, {"abc": CISnapshot({"build": PASS})}, state_root)
    cache_file = _cache_file(state_root, repo_root)

    now[0] = 1299.0
    assert dict(ci_rollup.cached_snapshot(repo_root, "abc", state_root, 300)) == {
        "build": PASS
    }
    assert json.loads(cache_file.read_text())["fetched_at"] == 1000

    # "older than max_age_s" is a strict comparison: exactly 300s is fresh.
    now[0] = 1300.0
    assert dict(ci_rollup.cached_snapshot(repo_root, "abc", state_root, 300)) == {
        "build": PASS
    }

    now[0] = 1301.0
    assert ci_rollup.cached_snapshot(repo_root, "abc", state_root, 300) is None
    assert json.loads(cache_file.read_text())["fetched_at"] == 1000


def test_missing_sha_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_rollup.time, "time", lambda: 1000.0)
    state_root = tmp_path / "state"
    repo_root = "/srv/repo/root"
    ci_rollup.store_rollup(repo_root, {"abc": CISnapshot({"build": PASS})}, state_root)

    assert ci_rollup.cached_snapshot(repo_root, "other", state_root, 300) is None


def test_empty_check_map_is_stored_and_returned_as_empty_snapshot(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(ci_rollup.time, "time", lambda: 1000.0)
    state_root = tmp_path / "state"
    repo_root = "/srv/repo/root"

    ci_rollup.store_rollup(repo_root, {"empty": CISnapshot()}, state_root)

    snapshot = ci_rollup.cached_snapshot(repo_root, "empty", state_root, 300)

    assert snapshot is not None
    assert isinstance(snapshot, CISnapshot)
    assert dict(snapshot) == {}
    assert ci_rollup.cached_snapshot(repo_root, "other", state_root, 300) is None


def test_missing_cache_file_returns_none_and_creates_nothing(tmp_path):
    state_root = tmp_path / "state"

    assert ci_rollup.cached_snapshot("/srv/repo/root", "abc", state_root, 300) is None
    assert not (state_root / "ci_rollup").exists()
    assert not state_root.exists()


def test_unreadable_cache_file_returns_none_without_rewriting(tmp_path):
    state_root = tmp_path / "state"
    cache_dir = state_root / "ci_rollup"
    cache_dir.mkdir(parents=True)
    os.chmod(cache_dir, 0o755)
    cache_file = _cache_file(state_root, "/srv/repo/root")
    cache_file.write_bytes(b"\xff\xfe not json")

    assert ci_rollup.cached_snapshot("/srv/repo/root", "abc", state_root, 300) is None

    assert cache_file.read_bytes() == b"\xff\xfe not json"
    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o755


def test_store_rollup_tightens_preexisting_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_rollup.time, "time", lambda: 1000.0)
    state_root = tmp_path / "state"
    cache_dir = state_root / "ci_rollup"
    cache_dir.mkdir(parents=True)
    os.chmod(cache_dir, 0o755)

    ci_rollup.store_rollup("/srv/repo/root", {"abc": CISnapshot()}, state_root)

    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(_cache_file(state_root, "/srv/repo/root").stat().st_mode) == 0o600
