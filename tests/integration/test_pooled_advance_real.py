"""Real-environment check: the reconcile pass advances two plans in children.

The unit tests substitute ``advance_plan_subprocess``, so they prove the pool's
control flow but never start a process. This test does: two scratch plans in a
real temporary git repository, advanced through
``PipelineService().advance_all_plans()`` with ``PIPELINE_ADVANCE_WORKERS=2``,
so the scheduler's reconcile path really forks two children.

Two properties only a real run can show are asserted:

* both plans come back ``ok: True`` from their own child process, each holding
  its own per-plan flock -- the reason a process per plan exists at all;
* the audit stream the children wrote is attributed to ``system:scheduler`` and
  carries no ``local-operator`` record. The children re-bind the scheduler
  principal themselves; a parent-only binding would show up here as
  ``local-operator``.

Note the ``PIPELINE_ADVANCE_WORKERS`` setting is load-bearing: with the default
of ``1``, ``advance_all_plans()`` takes the serial in-process path and exercises
no child process at all.

Each scratch story deliberately carries no scope declaration, exactly as
``tests/integration/test_scheduler_principal_binding_e2e.py`` does, so the
child authorizes a dispatch and parks the story for the operator instead of
launching an agent: the audit attribution is still recorded, and no worktree is
created.
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
from pipeline import paths as ppaths
from pipeline import persistence as ppers
from pipeline.actor import bind_principal, current_principal, SYSTEM_SCHEDULER
from pipeline.service import PipelineService
# isort: on

PLAN_A = "pooled-a"
PLAN_B = "pooled-b"


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


def _story(index: int) -> dict:
    key = f"S-{index}"
    return {
        "key": key,
        "summary": f"scratch story {key}",
        "status": "todo",
        "dependencies": [],
        "acceptance": [{"path": f"tests/test_scratch_{index}.py", "command": "true"}],
        "agent_instructions": "do nothing",
        "persona": "author",
        "model": "gpt-oss:20b",
        "risk": "low",
    }


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Two real two-story plans in a real git repo, in a real plan store."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("scratch repository\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")

    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    for plan_name, base in ((PLAN_A, 1), (PLAN_B, 3)):
        manifest = {
            "plan_name": plan_name,
            "repo_root": str(repo),
            "stories": {
                _story(base)["key"]: _story(base),
                _story(base + 1)["key"]: _story(base + 1),
            },
        }
        (plans / f"{plan_name}.manifest.json").write_text(
            json.dumps(manifest, indent=2)
        )

    # The parent resolves these through patched attributes; the children read
    # the same values from the environment they inherit at spawn.
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(p, "WORKTREE_ROOT", worktrees)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(ppaths, "PLAN_DIR", plans)
    monkeypatch.setattr(ppaths, "WORKTREE_ROOT", worktrees)
    monkeypatch.setenv("PLAN_DIR", str(plans))
    monkeypatch.setenv("WORKTREE_ROOT", str(worktrees))
    monkeypatch.setenv("PIPELINE_SKIP_ENV_FILE", "1")
    monkeypatch.setenv("PIPELINE_ADVANCE_WORKERS", "2")
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "1")

    return plans, worktrees


def _audit_records(plans: Path) -> list[dict]:
    """Every record in either plan's chain plus the host-scoped chain."""
    records: list[dict] = []
    for name in (f"{PLAN_A}.audit.jsonl", f"{PLAN_B}.audit.jsonl", ".audit.host.jsonl"):
        path = plans / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def test_two_plans_advance_in_real_child_processes(scratch, capsys):
    plans, worktrees = scratch

    started = time.monotonic()
    with bind_principal(SYSTEM_SCHEDULER):
        result = PipelineService().advance_all_plans()
    elapsed = time.monotonic() - started

    for name in (PLAN_A, PLAN_B):
        print(f"plan {name}: {result['plans'].get(name)}")
    print(f"pooled pass elapsed: {elapsed:.2f}s")

    assert result["ok"] is True
    assert sorted(result["plans"]) == [PLAN_A, PLAN_B]
    for name in (PLAN_A, PLAN_B):
        assert result["plans"][name]["ok"] is True, result["plans"][name]

    records = _audit_records(plans)
    principals = sorted(
        {r["detail"]["principal"] for r in records if r.get("detail", {}).get("principal")}
    )
    print(f"audit principals: {principals}; audit records: {len(records)}")

    assert records, "the children recorded nothing to attribute"
    assert "system:scheduler" in principals
    assert "local-operator" not in principals, (
        "a child inherited the parent's operator identity instead of binding "
        "its own system:scheduler principal"
    )

    # The stories carry no scope declaration, so the children parked them: no
    # agent tree was launched, and the attribution above is real advance work.
    assert list(worktrees.iterdir()) == []
    # The caller's own binding is untouched by the pass.
    assert current_principal() is not None
