"""The watchdog agent-log tail is written under the host story-log root.

``pipeline.checkpoint._snapshot_agent_log`` keeps the same file name
(``<plan>.<key>.watchdog-<stamp>.log``) and returns its path, but the file is
host-local and lives under ``HostState.story_log_root()``. Graded here:

* the tail is capped at 65536 bytes (the last bytes of agent.log),
* a missing agent.log returns None and writes nothing,
* a traversal plan name or story key is refused before anything is written.
"""

import re
from pathlib import Path

import pytest

import pipeline.server as server_mod
from pipeline import checkpoint

TAIL_CAP = 65536


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", plans)
    return plans


def _worktree(tmp_path, log_bytes):
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    if log_bytes is not None:
        (worktree / "agent.log").write_bytes(log_bytes)
    return worktree


def test_should_cap_the_tail_at_65536_bytes_keeping_the_end_of_the_log(plans_dir, tmp_path):
    log = b"a" * 10 + b"Z" * TAIL_CAP
    worktree = _worktree(tmp_path, log)

    path = checkpoint._snapshot_agent_log("plan", "STORY-1", str(worktree))

    written = Path(path).read_bytes()
    assert len(written) == TAIL_CAP
    assert written == log[-TAIL_CAP:]


def test_should_keep_a_small_log_whole(plans_dir, tmp_path):
    worktree = _worktree(tmp_path, b"step 1\nstep 2\n")

    path = checkpoint._snapshot_agent_log("plan", "STORY-1", str(worktree))

    assert Path(path).read_bytes() == b"step 1\nstep 2\n"


def test_should_return_none_when_agent_log_is_missing(plans_dir, tmp_path):
    worktree = _worktree(tmp_path, None)

    assert checkpoint._snapshot_agent_log("plan", "STORY-1", str(worktree)) is None
    assert list(plans_dir.glob("*watchdog-*")) == []


def test_should_write_under_the_host_story_log_root_with_the_same_name(plans_dir, tmp_path):
    worktree = _worktree(tmp_path, b"x")

    path = checkpoint._snapshot_agent_log("plan", "STORY-1", str(worktree))

    assert path is not None
    assert Path(path).parent == plans_dir
    assert re.fullmatch(r"plan\.STORY-1\.watchdog-\d{8}T\d{6}Z\.log", Path(path).name)


@pytest.mark.parametrize(
    ("plan_name", "story_key"),
    [
        ("../escape", "STORY-1"),
        ("plan", "../escape"),
        ("plan/../../escape", "STORY-1"),
        ("plan", "x/../../escape"),
    ],
)
def test_should_refuse_a_traversal_plan_or_key_before_writing(
    plans_dir, tmp_path, plan_name, story_key
):
    worktree = _worktree(tmp_path, b"secret tail")

    result = checkpoint._snapshot_agent_log(plan_name, story_key, str(worktree))

    assert result is None
    assert list(tmp_path.rglob("*watchdog-*")) == []
