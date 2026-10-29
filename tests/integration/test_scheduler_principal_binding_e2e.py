"""Real-environment check: a scheduler tick runs as ``system:scheduler``.

The unit tests drive the daemon with fake phase callables; that proves the
binding happens but not that the grant set covers what a tick actually does.
This test closes that gap: it runs the *real* daemon loop for one iteration
(``SchedulerDaemon.run_once``) with the *real* phase callables ``run_daemon``
wires (``pipeline.server.advance_all_plans`` and ``scan_all_plans(bus)``)
against a scratch plan in a real temporary git repository, with policy
enforcement switched ON.

It then reads the real audit stream and asserts:

* zero ``authorization_denied`` records in the plan's notification sidecar --
  a missing grant would be silently denied every tick and the pipeline would
  stop;
* zero ``denied`` outcomes in the audit chains;
* the dispatch decision the tick made is attributed to ``system:scheduler``,
  not to an ambient operator principal.

Two consecutive iterations are run because each phase spawns a fresh worker
thread: the binding must be re-applied per tick, never inherited from the
previous one. The main thread's principal is a non-operator human throughout,
so a leaked binding or a main-thread-only binding both fail this test.

Nothing is mocked: real git repository, real plan store, real authorization,
real audit chain. The scratch story deliberately carries no scope declaration,
so the tick authorizes the dispatch and then parks it for the operator instead
of launching an agent -- which is asserted (no worktree is created).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

# isort: off
# pipeline.server is imported first on purpose: pre-existing import cycle.
import pipeline.server  # noqa: F401
import pipeline.server as p
from app.identity import Principal
from pipeline import paths as ppaths
from pipeline import persistence as ppers
from pipeline.actor import bind_principal, current_principal
from pipeline.events import InProcessEventBus
from pipeline.scheduler_daemon import SchedulerDaemon
from pipeline.watchers import scan_all_plans
# isort: on

PLAN_NAME = "scratch"
STORY_KEY = "S-1"

#: A non-operator principal, bound on the main thread the way the dashboard
#: middleware binds one (tests/unit/test_patch_plan_enforcement.py idiom).
HUMAN_ALICE = Principal(subject="human:alice", auth_method="token")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "pipeline test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "pipeline test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A real git repo, a real plan store and enforcement switched ON."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("scratch repository\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")

    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    manifest = {
        "plan_name": PLAN_NAME,
        "repo_root": str(repo),
        "stories": {
            STORY_KEY: {
                "key": STORY_KEY,
                "summary": "a scratch story the tick may dispatch",
                "status": "todo",
                "dependencies": [],
                "acceptance": [
                    {"path": "tests/test_scratch.py", "command": "true"},
                ],
                "agent_instructions": "do nothing",
                "persona": "author",
                "model": "gpt-oss:20b",
                "risk": "low",
            }
        },
    }
    (plans / f"{PLAN_NAME}.manifest.json").write_text(json.dumps(manifest, indent=2))

    # The store, the audit chain and the notification sidecar each resolve
    # PLAN_DIR at call time; patch every binding so all three land in tmp.
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(p, "WORKTREE_ROOT", worktrees)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(ppaths, "PLAN_DIR", plans)
    monkeypatch.setattr(ppaths, "WORKTREE_ROOT", worktrees)

    # Enforcement ON: with it off every action is allowed and the check below
    # would pass vacuously.
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "1")

    return plans, worktrees


def _audit_records(plans: Path) -> list[dict]:
    """Every record in the plan's chain plus the host-scoped chain."""
    records: list[dict] = []
    for path in (plans / f"{PLAN_NAME}.audit.jsonl", plans / ".audit.host.jsonl"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _denial_notifications(plans: Path) -> list[dict]:
    """Every ``authorization_denied`` record in the notification sidecar."""
    path = plans / f"{PLAN_NAME}.notifications.jsonl"
    if not path.exists():
        return []
    return [
        record
        for line in path.read_text().splitlines()
        if line.strip()
        for record in (json.loads(line),)
        if record.get("event") == "authorization_denied"
    ]


def test_real_daemon_tick_is_attributed_to_system_scheduler(scratch, capsys):
    plans, worktrees = scratch
    bus = InProcessEventBus()
    daemon = SchedulerDaemon(
        reconcile_fn=p.advance_all_plans,
        scan_fn=lambda: scan_all_plans(bus),
        bus=bus,
        interval_s=0,  # reconcile on every iteration
        sleep_fn=lambda _s: None,
        clock=time.monotonic,
    )

    with bind_principal(HUMAN_ALICE):
        for cycle in (1, 2):
            daemon.run_once()
            records = _audit_records(plans)
            denials = _denial_notifications(plans)
            denied = [r for r in records if r.get("outcome") == "denied"]
            dispatches = [
                r
                for r in records
                if r.get("action") == "plan:dispatch"
                and r.get("outcome") == "allowed"
            ]
            principals = sorted({r["detail"]["principal"] for r in dispatches})
            print(
                f"cycle {cycle}: authorization_denied={len(denials)} "
                f"denied={len(denied)} "
                f"dispatch records={len(dispatches)} "
                f"principal={principals}"
            )
            # A denial means the grant set does not cover what the tick does:
            # that action is refused on every tick and the pipeline stops.
            assert denials == []
            assert denied == []
            assert dispatches, "the tick made no dispatch decision to attribute"
            assert principals == ["system:scheduler"]
            # The main thread is still the human the caller bound, not the
            # system principal the worker used: the binding never leaks out of
            # the worker thread.
            assert current_principal() is HUMAN_ALICE

    # The scratch story has no scope declaration, so the tick parks it for the
    # operator rather than launching an agent: nothing is spawned, and the
    # check above really did run through the dispatch authorization path.
    assert list(worktrees.iterdir()) == []


def test_enforcement_is_actually_on(scratch):
    """Guard the check above: an unenforced run proves nothing."""
    from pipeline.authorization import enforcement_enabled

    assert enforcement_enabled() is True
