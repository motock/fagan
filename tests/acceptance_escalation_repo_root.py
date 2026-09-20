"""Acceptance: the escalate_model ladder must survive the installed scheduler.

ROOT CAUSE (observed live 2026-09-19, plan ``local-dispatch-90-t1``)
-------------------------------------------------------------------
``pipeline/escalation.py``'s ``_escalate_to_claude`` and
``_escalate_to_local_fallback_model`` ran their git teardown with
``cwd=REPO_ROOT`` - the PROCESS-GLOBAL repo root lazily imported from
``pipeline.server``. Under the installed LaunchAgent
(``com.fagan.pipeline.advance-scheduler.plist``) that global is set to the
sentinel ``/nonexistent-repo-root-set-per-plan-only`` ("set per plan only"),
so every ``subprocess.run(..., cwd=REPO_ROOT)`` raised::

    FileNotFoundError: [Errno 2] No such file or directory:
    PosixPath('/nonexistent-repo-root-set-per-plan-only')

``pipeline/advance.py::_advance_pipeline_locked`` runs ``run_triage_sweep``
BEFORE entering ``_scoped_repo_root(plan_name)``, so a triage escalate_model
ruling reached escalation with the sentinel still active;
``pipeline/triage.py`` caught the error and parked the story with
"escalate_model ruled but ladder exhausted: FileNotFoundError: ..." instead of
escalating. Escalation onto a stronger executor was silently dead for every
plan; two stories parked on it (LD90-W0-07, LD90-W0-07-split-1).

CONTRACT PINNED BY THIS FILE
----------------------------
* The plan's own ``manifest["repo_root"]`` is the repo the escalation teardown
  must run in - plans share one PLAN_DIR but each belongs to a different repo
  (the same precedence ``pipeline.server._repo_root_for`` already applies to
  dispatch and merge).
* ``REPO_ROOT`` remains only the documented fallback for manifests ingested
  before ``repo_root`` existed, and is never the primary source.
* An unusable global (the sentinel) must never be able to park a story that a
  valid ``repo_root`` would have escalated.

Every test stubs the git boundary (``pipeline.escalation.subprocess``) and sets
the global sentinel explicitly, so nothing here asserts against this machine's
real configuration.
"""
from __future__ import annotations

import subprocess as real_subprocess
from pathlib import Path

import pytest

from pipeline import escalation as esc
from pipeline import server

SENTINEL = Path("/nonexistent-repo-root-set-per-plan-only")


class _FakeSubprocess:
    """Stand-in for the ``subprocess`` module that records each git call's cwd."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        return real_subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def fake_git(monkeypatch):
    fake = _FakeSubprocess()
    monkeypatch.setattr(esc, "subprocess", fake)
    return fake


def _manifest(repo_root, story_key: str) -> dict:
    manifest: dict = {"stories": {story_key: {"worktree": "", "status": "failed"}}}
    if repo_root is not None:
        manifest["repo_root"] = repo_root
    return manifest


def test_claude_escalation_ignores_the_sentinel_global_repo_root(
    tmp_path, fake_git, monkeypatch
):
    monkeypatch.setattr(server, "REPO_ROOT", SENTINEL)
    manifest = _manifest(str(tmp_path), "ACC-1")
    story = manifest["stories"]["ACC-1"]

    esc._escalate_to_claude(
        manifest, "acc-plan", "ACC-1", tmp_path / "acc-plan.manifest.json"
    )

    assert fake_git.calls, "escalation must still run its git teardown"
    assert {str(call["cwd"]) for call in fake_git.calls} == {str(tmp_path)}
    assert story["status"] == "todo"
    assert story["escalated"] is True


def test_fallback_model_escalation_ignores_the_sentinel_global_repo_root(
    tmp_path, fake_git, monkeypatch
):
    monkeypatch.setattr(server, "REPO_ROOT", SENTINEL)
    manifest = _manifest(str(tmp_path), "ACC-2")
    story = manifest["stories"]["ACC-2"]

    esc._escalate_to_local_fallback_model(
        manifest,
        "acc-plan",
        "ACC-2",
        tmp_path / "acc-plan.manifest.json",
        "gpt-oss-20b-high:latest",
    )

    assert fake_git.calls, "escalation must still run its git teardown"
    assert {str(call["cwd"]) for call in fake_git.calls} == {str(tmp_path)}
    assert story["status"] == "todo"
    assert story["tried_fallback_model"] is True


def test_legacy_manifest_without_repo_root_falls_back_to_the_process_global(
    tmp_path, fake_git, monkeypatch
):
    monkeypatch.setattr(server, "REPO_ROOT", tmp_path)
    manifest = _manifest(None, "ACC-3")

    esc._escalate_to_claude(
        manifest, "acc-plan", "ACC-3", tmp_path / "acc-plan.manifest.json"
    )

    assert fake_git.calls
    assert {str(call["cwd"]) for call in fake_git.calls} == {str(tmp_path)}


def test_empty_repo_root_falls_back_instead_of_yielding_an_empty_cwd(
    tmp_path, fake_git, monkeypatch
):
    monkeypatch.setattr(server, "REPO_ROOT", tmp_path)
    manifest = _manifest("", "ACC-4")

    esc._escalate_to_claude(
        manifest, "acc-plan", "ACC-4", tmp_path / "acc-plan.manifest.json"
    )

    assert fake_git.calls
    assert {str(call["cwd"]) for call in fake_git.calls} == {str(tmp_path)}
