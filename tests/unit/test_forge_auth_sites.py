"""The rebase and merge pushes run with the bound team's forge environment.

A team namespace's ``git push`` carries an ``env`` with the credential seam's
token and never the process token; a default-namespace push passes no ``env``
at all. The credential seam is stubbed at ``resolve_for_injection``; no test
uses a real token value. The merge test runs the real ``git push`` against a
local bare origin, so the only thing that changes between namespaces is the
environment the push receives.
"""

import contextlib
import os
import subprocess

import pytest

from pipeline import authorization, concurrency, forge_auth, merge, rebase, server
from pipeline.actor import bind_namespace, current_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

TEAM = "acme/web"
TEAM_TOKEN = "team-token-for-tests"
PROCESS_TOKEN = "process-token-for-tests"
WORKTREE = "/some/worktree"
BRANCH = "agent/tfa-02"


class _Result:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


@pytest.fixture
def team_token(monkeypatch):
    monkeypatch.setattr(forge_auth, "resolve_for_injection", lambda ref: TEAM_TOKEN)


@pytest.fixture
def process_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", PROCESS_TOKEN)


@pytest.fixture
def recorded_pushes(monkeypatch):
    """Patch subprocess.run to succeed and record each ``git push``'s kwargs."""
    pushes = []

    def _fake_run(cmd, **kwargs):
        if list(cmd)[:2] == ["git", "push"]:
            pushes.append(kwargs)
        return _Result()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return pushes


def test_rebase_push_carries_the_team_token(recorded_pushes, team_token, process_token):
    with bind_namespace(TEAM):
        result = rebase._sync_branch_remote(WORKTREE, BRANCH)

    assert result["ok"] is True
    (kwargs,) = recorded_pushes
    assert kwargs["env"]["GH_TOKEN"] == TEAM_TOKEN
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_rebase_push_in_default_namespace_passes_no_env(recorded_pushes, team_token):
    with bind_namespace("default"):
        rebase._sync_branch_remote(WORKTREE, BRANCH)

    (kwargs,) = recorded_pushes
    assert "env" not in kwargs


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _init_plan_repo(tmp_path):
    """A bare ``origin`` plus a story repo on branch ``agent/s1``."""
    origin = tmp_path / "origin.git"
    _git("-c", "init.defaultBranch=master", "init", "--bare", "-q",
         str(origin), cwd=tmp_path)
    repo = tmp_path / "repo"
    _git("-c", "init.defaultBranch=master", "init", "-q", str(repo), cwd=tmp_path)
    _git("config", "user.email", "agent@local", cwd=repo)
    _git("config", "user.name", "agent", cwd=repo)
    (repo / "master.txt").write_text("m\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "m0", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "origin", "master", cwd=repo)
    _git("checkout", "-q", "-b", "agent/s1", cwd=repo)
    (repo / "story.txt").write_text("story\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "story work", cwd=repo)
    _git("push", "-q", "origin", "agent/s1", cwd=repo)
    return repo


def _fake_rebase_onto_master(worktree, branch):
    _git("commit", "-q", "--allow-empty", "-m", "rebased onto master", cwd=worktree)
    return {"ok": True}


class _RecordsFacade:
    """The record store the merge path reads and writes, for the bound namespace.

    A record store holds every partition, so a team namespace is accepted the
    way it is in production; the file layout would refuse it.
    """

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records

    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        return _always_acquired_lock(plan_name)

    def get_manifest(self, plan_name: str) -> dict:
        manifest = self._records.get_manifest(current_namespace(), plan_name)
        if manifest is None:
            raise FileNotFoundError(plan_name)
        return manifest

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        return self._records.get_manifest(current_namespace(), plan_name)

    def save_manifest(self, plan_name: str, manifest: dict) -> None:
        self._records.put_manifest(current_namespace(), plan_name, manifest)


@contextlib.contextmanager
def _always_acquired_lock(plan_name: str, wait_seconds: float = 0.0):
    yield True


def _merge_pushes_for(monkeypatch, tmp_path, namespace):
    """Run the manual merge path in ``namespace``; return the push kwargs seen.

    Every ``git push`` is recorded and then runs for real, so a rejected push
    fails the merge and the assertion on the result shows why.
    """
    repo = _init_plan_repo(tmp_path)
    records = InMemoryRecordStore()
    records.put_manifest(namespace, "go", {
        "epics": {},
        "repo_root": str(repo),
        "stories": {"S1": {
            "summary": "story on a plan-scoped repo",
            "status": "parked",
            "review_verdict": "APPROVE", "review_verdict_sha": "stub-head",
            "risk": "low", "worktree": str(repo),
        }},
    })
    pushes = []
    real_run = subprocess.run

    def _spy(cmd, *args, **kwargs):
        if list(cmd)[:2] == ["git", "push"]:
            pushes.append(kwargs)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(merge.subprocess, "run", _spy)
    monkeypatch.setattr(authorization, "authorize", lambda *args, **kwargs: None)
    monkeypatch.setattr(merge, "record_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(concurrency, "_plan_lock", _always_acquired_lock)
    monkeypatch.setattr(server, "_store", _RecordsFacade(records))
    monkeypatch.setattr(server, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(server, "REPO_ROOT", tmp_path / "decoy")
    monkeypatch.setattr(server, "_rebase_onto_master", _fake_rebase_onto_master)
    monkeypatch.setattr(server, "_default_branch", lambda: "master")
    monkeypatch.setattr(server, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ci_status", lambda branch, *, sha, settings=None: {
        "state": "success", "error": "",
    })
    monkeypatch.setattr(server, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(server, "_reverify_acceptance",
                        lambda story, wt, key, settings=None: {"state": "pass"})
    monkeypatch.setattr(server, "_reverify_build", lambda wt, settings=None: {"state": "pass"})
    monkeypatch.setattr(server, "_mcp_self_source_touched", lambda wt, base: "")
    monkeypatch.setattr(server, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(server, "_mark_plane_done", lambda *a, **k: None)

    with bind_namespace(namespace):
        result = merge._approve_merge_impl("go", "S1")
    assert result.get("ok") is True, f"manual merge failed: {result!r}"
    assert pushes, "the manual merge path must push the rebased branch"
    return pushes


def test_merge_push_carries_the_team_token(tmp_path, monkeypatch, team_token, process_token):
    pushes = _merge_pushes_for(monkeypatch, tmp_path, TEAM)

    env = pushes[0]["env"]
    assert env["GH_TOKEN"] == TEAM_TOKEN
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env.get("GH_TOKEN") != PROCESS_TOKEN
    assert os.environ["GH_TOKEN"] == PROCESS_TOKEN


def test_merge_push_in_default_namespace_passes_no_env(tmp_path, monkeypatch, team_token):
    pushes = _merge_pushes_for(monkeypatch, tmp_path, DEFAULT_NAMESPACE)

    assert "env" not in pushes[0]
