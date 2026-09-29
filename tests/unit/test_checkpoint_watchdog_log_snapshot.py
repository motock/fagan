"""TDD spec: the dispatch watchdog keeps a copy of the tail of agent.log.

A story's worktree (and its agent.log) is deleted after merge, so every
watchdog kill was undiagnosable afterwards - the only surviving evidence was
one ``last_log_line`` in the journal. This story adds a ``log_snapshot``
field to the ``dispatch_watchdog_timeout`` journal entry pointing at a copy
of the last <=64 KiB of ``<worktree>/agent.log``, written into PLAN_DIR.

Contract graded here:
  * ``pipeline.checkpoint._snapshot_agent_log(plan_name, story_key, worktree)``
    exists, resolves PLAN_DIR lazily from ``pipeline.server`` (NOT
    persistence), returns None when ``<worktree>/agent.log`` is not a file,
    otherwise writes the last <=65536 bytes to
    ``PLAN_DIR / f"{plan_name}.{story_key}.watchdog-{stamp}.log"`` and
    returns that path as a str.
  * ``_terminate_and_checkpoint`` adds ``record["log_snapshot"]`` ONLY on the
    ``dispatch_watchdog_timeout`` step; the manual interrupt path never
    snapshots.
  * Fail-open: a snapshot failure must never change the termination outcome.

The journal entry is a CUMULATIVE artifact - these tests assert only the
``log_snapshot`` key (membership / absence), never the record's full
contents, so sibling stories can add their own keys.

Boundaries stubbed: ``pipeline.checkpoint._commit_wip`` (git) and ``os.kill``
(process). PLAN_DIR is always a tmp dir - never the operator's real plans
directory.
"""

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline.server as p
from pipeline import checkpoint, persistence

STUB_SHA = "c" * 40
PLAN_NAME = "plan1"
STORY_KEY = "story-1"
SNAPSHOT_CAP = 65536


@pytest.fixture
def watchdog_env(tmp_path, monkeypatch):
    """Isolated PLAN_DIR + stubbed git/process boundaries."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(persistence, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(checkpoint, "_commit_wip", lambda *a, **k: STUB_SHA)
    monkeypatch.setattr(os, "kill", lambda *a, **k: None)
    return SimpleNamespace(plan_dir=plan_dir, plan_name=PLAN_NAME, story_key=STORY_KEY)


def _make_story(worktree, *, log_bytes=None):
    worktree = Path(worktree)
    worktree.mkdir(parents=True, exist_ok=True)
    if log_bytes is not None:
        (worktree / "agent.log").write_bytes(log_bytes)
    return {"status": "in_progress", "worktree": str(worktree)}


def _terminate(env, story, *, step):
    manifest = {"name": env.plan_name, "stories": {env.story_key: story}}
    manifest_path = env.plan_dir / f"{env.plan_name}.manifest.json"
    return checkpoint._terminate_and_checkpoint(
        manifest, manifest_path, env.plan_name, env.story_key, story,
        pid=12345, step=step, summary="watchdog",
    )


def _journal_record(env):
    path = env.plan_dir / f"{env.plan_name}.{env.story_key}.journal.json"
    return json.loads(path.read_text())[-1]


def _snapshot_files(env):
    return sorted(env.plan_dir.glob(f"{env.plan_name}.{env.story_key}.watchdog-*.log"))


def test_should_snapshot_log_tail_on_watchdog_kill(watchdog_env, tmp_path):
    """Watchdog kill journals log_snapshot; the file lives under PLAN_DIR and
    its bytes equal the end of agent.log."""
    log_bytes = b"step 1 ok\nstep 2 ORACLE GREEN\n"
    story = _make_story(tmp_path / "wt", log_bytes=log_bytes)

    _terminate(watchdog_env, story, step="dispatch_watchdog_timeout")

    record = _journal_record(watchdog_env)
    assert "log_snapshot" in record, f"record: {record!r}"
    snapshot_path = Path(record["log_snapshot"])
    assert snapshot_path.is_file()
    assert snapshot_path.parent == watchdog_env.plan_dir
    assert re.fullmatch(
        rf"{re.escape(watchdog_env.plan_name)}\.{re.escape(watchdog_env.story_key)}"
        r"\.watchdog-\d{8}T\d{6}Z\.log",
        snapshot_path.name,
    ), f"unexpected snapshot name: {snapshot_path.name!r}"
    assert snapshot_path.read_bytes() == log_bytes


def test_should_cap_snapshot_at_64_KiB(watchdog_env, tmp_path):
    """A 200_000-byte agent.log yields exactly the last 65536 bytes."""
    log_bytes = b"0123456789" * 20_000
    assert len(log_bytes) == 200_000
    story = _make_story(tmp_path / "wt", log_bytes=log_bytes)

    _terminate(watchdog_env, story, step="dispatch_watchdog_timeout")

    record = _journal_record(watchdog_env)
    assert "log_snapshot" in record, f"record: {record!r}"
    data = Path(record["log_snapshot"]).read_bytes()
    assert len(data) == SNAPSHOT_CAP
    assert data == log_bytes[-SNAPSHOT_CAP:]


def test_should_not_snapshot_on_manual_interrupt(watchdog_env, tmp_path):
    """The manual interrupt path never snapshots."""
    story = _make_story(tmp_path / "wt", log_bytes=b"hung\n")

    _terminate(watchdog_env, story, step="manual_interrupt")

    record = _journal_record(watchdog_env)
    assert "log_snapshot" not in record, f"record: {record!r}"
    assert _snapshot_files(watchdog_env) == []


def test_should_omit_snapshot_when_log_missing(watchdog_env, tmp_path):
    """No agent.log -> no key, no error, story still interrupted."""
    story = _make_story(tmp_path / "wt", log_bytes=None)

    _terminate(watchdog_env, story, step="dispatch_watchdog_timeout")

    record = _journal_record(watchdog_env)
    assert "log_snapshot" not in record, f"record: {record!r}"
    assert story["status"] == "interrupted"
    assert _snapshot_files(watchdog_env) == []


def test_should_not_block_termination_when_snapshot_write_fails(
        watchdog_env, tmp_path, monkeypatch):
    """An unwritable PLAN_DIR (a regular file) must not change termination.

    Only pipeline.server.PLAN_DIR is broken: the snapshot resolves PLAN_DIR
    lazily from the server, while the journal is written through
    pipeline.persistence.PLAN_DIR (still a real tmp dir), so the journal
    write succeeds and the snapshot write fails.
    """
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(p, "PLAN_DIR", not_a_dir)
    story = _make_story(tmp_path / "wt", log_bytes=b"hung\n")

    _terminate(watchdog_env, story, step="dispatch_watchdog_timeout")

    assert story["status"] == "interrupted"
    record = _journal_record(watchdog_env)
    assert "log_snapshot" not in record, f"record: {record!r}"


def test_snapshot_helper_contract(watchdog_env, tmp_path):
    """_snapshot_agent_log: None when agent.log is absent or not a file,
    otherwise a str path under PLAN_DIR holding the log's bytes."""
    assert hasattr(checkpoint, "_snapshot_agent_log")

    # absent -> None
    absent = tmp_path / "absent"
    absent.mkdir()
    assert checkpoint._snapshot_agent_log(
        watchdog_env.plan_name, watchdog_env.story_key, str(absent)) is None

    # a directory named agent.log is not a file -> None
    not_a_file = tmp_path / "not_a_file"
    (not_a_file / "agent.log").mkdir(parents=True)
    assert checkpoint._snapshot_agent_log(
        watchdog_env.plan_name, watchdog_env.story_key, str(not_a_file)) is None

    # present -> str path under PLAN_DIR with the log's bytes
    present = tmp_path / "present"
    present.mkdir()
    (present / "agent.log").write_bytes(b"tail bytes\n")
    result = checkpoint._snapshot_agent_log(
        watchdog_env.plan_name, watchdog_env.story_key, str(present))
    assert isinstance(result, str)
    assert Path(result).parent == watchdog_env.plan_dir
    assert Path(result).read_bytes() == b"tail bytes\n"
