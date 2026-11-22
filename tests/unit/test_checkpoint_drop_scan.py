"""Control-plane side of the file-dropped checkpoint: ingest and scan
journal a drop only in file mode, only for in-progress stories, once each,
and the wiring in the watcher loop and the git WIP commit.
"""

import json
import subprocess
from pathlib import Path

import pytest

from pipeline import checkpoint_drop, watchers
from pipeline.git_ops import _commit_wip

DROP = ".agent_checkpoint"
PLAN = "alpha"


@pytest.fixture
def journal(monkeypatch):
    """Record journal appends through the seam existing tests use."""
    calls: list[tuple[str, str, dict]] = []

    def fake_append(plan_name, story_key, record):
        calls.append((plan_name, story_key, record))

    monkeypatch.setattr("pipeline.checkpoint._append_journal", fake_append)
    return calls


@pytest.fixture
def file_mode(monkeypatch):
    monkeypatch.setenv(checkpoint_drop.MODE_ENV, "file")


def _worktree(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def _drop(worktree: Path, step: str = "build") -> None:
    payload = {"step": step, "summary": "did it", "next_hint": "", "commit": None}
    (worktree / DROP).write_text(json.dumps(payload))


def _manifest(**stories) -> dict:
    return {"stories": stories}


# ---------- file mode off ----------


def test_ingest_is_a_no_op_when_file_mode_is_off(tmp_path, monkeypatch, journal):
    monkeypatch.delenv(checkpoint_drop.MODE_ENV, raising=False)
    worktree = _worktree(tmp_path, "wt")
    _drop(worktree)
    before = (worktree / DROP).read_bytes()

    result = checkpoint_drop.ingest(PLAN, "S1", {"worktree": str(worktree), "status": "in_progress"})

    assert result is None
    assert journal == []
    assert (worktree / DROP).read_bytes() == before


def test_scan_is_a_no_op_when_file_mode_is_off(tmp_path, monkeypatch, journal):
    monkeypatch.setenv(checkpoint_drop.MODE_ENV, "tool")
    worktree = _worktree(tmp_path, "wt")
    _drop(worktree)

    count = checkpoint_drop.scan(
        _manifest(S1={"worktree": str(worktree), "status": "in_progress"}), PLAN
    )

    assert count == 0
    assert journal == []
    assert (worktree / DROP).exists()


# ---------- file mode on ----------


def test_scan_journals_an_in_progress_drop_exactly_once(tmp_path, file_mode, journal):
    worktree = _worktree(tmp_path, "wt")
    _drop(worktree, step="halfway")
    manifest = _manifest(S1={"worktree": str(worktree), "status": "in_progress"})

    first = checkpoint_drop.scan(manifest, PLAN)
    second = checkpoint_drop.scan(manifest, PLAN)

    assert first == 1
    assert second == 0
    assert len(journal) == 1
    plan_name, story_key, record = journal[0]
    assert (plan_name, story_key) == (PLAN, "S1")
    assert record["step"] == "halfway"
    assert record["summary"] == "did it"
    assert "ts" in record


def test_scan_skips_stories_in_other_statuses_and_missing_worktrees(
    tmp_path, file_mode, journal
):
    live = _worktree(tmp_path, "live")
    _drop(live, step="live-step")
    done = _worktree(tmp_path, "done")
    _drop(done, step="done-step")
    manifest = _manifest(
        LIVE={"worktree": str(live), "status": "in_progress"},
        DONE={"worktree": str(done), "status": "done"},
        GONE={"worktree": str(tmp_path / "missing"), "status": "in_progress"},
    )

    count = checkpoint_drop.scan(manifest, PLAN)

    assert count == 1
    assert [key for _, key, _ in journal] == ["LIVE"]
    assert (done / DROP).exists()


def test_one_story_whose_journal_append_raises_does_not_stop_the_next(
    tmp_path, monkeypatch, file_mode, caplog
):
    first = _worktree(tmp_path, "first")
    _drop(first)
    second = _worktree(tmp_path, "second")
    _drop(second, step="second-step")
    journalled: list[str] = []

    def flaky_append(plan_name, story_key, record):
        if story_key == "A":
            raise RuntimeError("store unavailable")
        journalled.append(story_key)

    monkeypatch.setattr("pipeline.checkpoint._append_journal", flaky_append)
    manifest = _manifest(
        A={"worktree": str(first), "status": "in_progress"},
        B={"worktree": str(second), "status": "in_progress"},
    )

    count = checkpoint_drop.scan(manifest, PLAN)

    assert count == 1
    assert journalled == ["B"]
    assert "RuntimeError" in caplog.text
    assert "store unavailable" not in caplog.text


# ---------- watcher loop ordering ----------


def test_watcher_loop_runs_the_drop_scan_before_the_done_marker_scan(
    tmp_path, monkeypatch
):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(watchers.manifest_source, "read_plan_names", lambda **_: [PLAN])
    monkeypatch.setattr(
        watchers.manifest_source,
        "read_plan_manifest",
        lambda plan_name, **_: {"stories": {}},
    )
    calls: list[str] = []
    monkeypatch.setattr(
        checkpoint_drop,
        "scan",
        lambda manifest, plan_name: calls.append("drop") or 0,
    )
    monkeypatch.setattr(
        watchers,
        "scan_done_markers",
        lambda manifest, plan_name, bus: calls.append("done") or [],
    )

    class _Bus:
        def publish(self, event):
            pass

    watchers.scan_all_plans(_Bus())

    assert calls[:2] == ["drop", "done"]


# ---------- WIP commit excludes the drop ----------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def test_commit_wip_leaves_an_untracked_drop_out_of_the_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Runner")
    (repo / "code.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    (repo / "code.py").write_text("x = 2\n")
    (repo / DROP).write_text(json.dumps({"step": "s", "summary": "", "next_hint": ""}))
    (repo / (DROP + ".claimed")).write_text("{}")

    _commit_wip(str(repo), "S1", "halfway")

    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["code.py"]
    status = _git(repo, "status", "--porcelain")
    assert f"?? {DROP}" in status
    assert f"?? {DROP}.claimed" in status
