"""Tests for PermissionError handling in ``_terminate_and_checkpoint``.

ROOT CAUSE: pipeline/checkpoint.py::_terminate_and_checkpoint wraps
``os.kill(pid, signal.SIGTERM)`` in ``try: ... except ProcessLookupError: pass``.
On CI runners the dispatched pid can belong to ANOTHER USER (fixture pids
111/222 exist but are not ours), so ``os.kill`` raises PermissionError (EPERM),
which is NOT caught.  The whole interrupt-and-checkpoint path then crashes
BEFORE the WIP commit and journal entry — the resumable checkpoint is lost.

The fix: catch ``(ProcessLookupError, PermissionError)`` at that one site, so a
pid owned by another user is treated exactly like an already-dead pid: the
checkpoint (WIP commit + journal entry + interrupted status) still happens.

Stubbing pattern mirrors tests/unit/test_checkpoint_deletion_guard.py: a real
git worktree in tmp_path (no mocking of _commit_wip), PLAN_DIR patched on every
module that reads it as a free variable, and os.kill monkeypatched inside
pipeline.checkpoint to raise the simulated error.
"""

import inspect
import json
import signal
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import pipeline.checkpoint as pcheckpoint
import pipeline.persistence as ppersist
from pipeline.checkpoint import _terminate_and_checkpoint

# ---------- helpers (mirroring test_checkpoint_deletion_guard.py) ----------


def _init_repo(tmp: Path) -> str:
    """Initialize a git repo with an initial commit and return the HEAD sha."""
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    (tmp / "progress.txt").write_text("half done\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=False, cwd=tmp, capture_output=True, text=True,
    ).stdout.strip()


def _head_sha(tmp: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=False, cwd=tmp, capture_output=True, text=True,
    ).stdout.strip()


def _make_manifest(plan_dir: Path, plan_name: str, story_key: str,
                   worktree: str, pid: int) -> tuple[dict, Path]:
    """Minimal manifest + path for an in-progress (running) story, matching what
    the server/checkpoint code reads."""
    manifest = {
        "stories": {
            story_key: {
                "worktree": worktree,
                "status": "running",
                "pid": pid,
                "last_commit": "",
            },
        },
    }
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest, manifest_path


def _patch_plan_dir(monkeypatch, plan_dir: Path):
    """Patch PLAN_DIR on every module that reads it as a free variable."""
    import pipeline.server as pserver
    monkeypatch.setattr(pserver, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pcheckpoint, "PLAN_DIR", plan_dir, raising=False)
    monkeypatch.setattr(ppersist, "PLAN_DIR", plan_dir)


def _patch_kill(monkeypatch, exc: Exception) -> list[tuple[int, int]]:
    """Replace os.kill — the reference pipeline.checkpoint resolves at call
    time — with a stub that records the call and then raises ``exc``,
    simulating a pid owned by another user (PermissionError/EPERM) or already
    gone (ProcessLookupError/ESRCH)."""
    calls: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        raise exc

    monkeypatch.setattr(pcheckpoint.os, "kill", fake_kill)
    return calls


def _assert_checkpoint_outcome(manifest, manifest_path, plan_name, story_key,
                               sha, step, summary):
    """The full successful-checkpoint contract: interrupted status (in memory
    and on disk), a journal entry (0 -> 1) pointing at the WIP commit."""
    story = manifest["stories"][story_key]
    assert story["status"] == "interrupted"
    assert story["last_commit"] == sha
    assert story["interrupted_at"], "interrupted_at must be timestamped"
    datetime.fromisoformat(story["interrupted_at"])  # valid ISO-8601
    on_disk = json.loads(manifest_path.read_text())
    assert on_disk["stories"][story_key]["status"] == "interrupted"
    journal = ppersist._read_journal(plan_name, story_key)
    assert len(journal) == 1, "exactly one journal entry must be recorded"
    record = journal[0]
    assert record["step"] == step
    assert record["summary"] == summary
    assert record["commit"] == sha
    assert record["next_hint"] == ""  # interrupt records carry no next_hint
    assert record["ts"]
    datetime.fromisoformat(record["ts"])


# ---------- PermissionError (pid owned by another user, e.g. CI runners) ----------


def test_terminate_and_checkpoint_survives_permission_error(tmp_path, monkeypatch):
    """os.kill raising PermissionError (pid 111 exists but belongs to another
    user) must NOT crash the interrupt path: the WIP commit, journal entry, and
    interrupted status must all still be recorded."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)
    (worktree / "wip.txt").write_text("uncommitted work\n")

    manifest, manifest_path = _make_manifest(plan_dir, "P1", "S1", str(worktree), pid=111)
    calls = _patch_kill(monkeypatch, PermissionError(13, "Operation not permitted"))

    # Must not raise — before the fix, PermissionError propagated from here.
    sha = _terminate_and_checkpoint(
        manifest, manifest_path, "P1", "S1", manifest["stories"]["S1"],
        pid=111, step="dispatch_watchdog_timeout",
        summary="ci pid owned by another user",
    )

    assert sha, "checkpoint must still produce a WIP commit sha"
    assert calls == [(111, signal.SIGTERM)]
    _assert_checkpoint_outcome(
        manifest, manifest_path, "P1", "S1", sha,
        "dispatch_watchdog_timeout", "ci pid owned by another user",
    )
    # The WIP commit actually landed in the worktree (resumable state).
    assert _head_sha(worktree) == sha


def test_terminate_and_checkpoint_process_lookup_error_still_checkpoints(
    tmp_path, monkeypatch,
):
    """Pre-existing behavior preserved: os.kill raising ProcessLookupError (pid
    already dead) must still produce the same successful checkpoint outcome."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)
    (worktree / "wip.txt").write_text("uncommitted work\n")

    manifest, manifest_path = _make_manifest(plan_dir, "P1", "S2", str(worktree), pid=222)
    calls = _patch_kill(monkeypatch, ProcessLookupError(3, "No such process"))

    sha = _terminate_and_checkpoint(
        manifest, manifest_path, "P1", "S2", manifest["stories"]["S2"],
        pid=222, step="interrupted", summary="pid already gone",
    )

    assert sha
    assert calls == [(222, signal.SIGTERM)]
    _assert_checkpoint_outcome(
        manifest, manifest_path, "P1", "S2", sha, "interrupted", "pid already gone",
    )
    assert _head_sha(worktree) == sha


# ---------- malformed input: story missing its required worktree field ----------


def test_terminate_and_checkpoint_missing_worktree_raises_keyerror(
    tmp_path, monkeypatch,
):
    """A story dict missing the required 'worktree' field is malformed input:
    the function must fail loudly (KeyError naming the field), not silently."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)

    manifest, manifest_path = _make_manifest(plan_dir, "P1", "S9", str(worktree), pid=222)
    del manifest["stories"]["S9"]["worktree"]
    _patch_kill(monkeypatch, ProcessLookupError(3, "No such process"))

    with pytest.raises(KeyError, match="worktree"):
        _terminate_and_checkpoint(
            manifest, manifest_path, "P1", "S9", manifest["stories"]["S9"],
            pid=222, step="interrupted", summary="malformed story",
        )
    # Nothing was journaled for the failed checkpoint.
    assert ppersist._read_journal("P1", "S9") == []


# ---------- the fix itself: the except clause at the one os.kill site ----------


def test_except_clause_covers_permission_error_at_terminate_site():
    """The os.kill guard in _terminate_and_checkpoint must catch
    (ProcessLookupError, PermissionError): exactly one such clause in the
    module, at that site, with the old ProcessLookupError-only clause gone."""
    new_clause = "except (ProcessLookupError, PermissionError):"
    source = Path(pcheckpoint.__file__).read_text()
    assert source.count(new_clause) == 1, (
        "expected exactly one 'except (ProcessLookupError, PermissionError):' "
        "in pipeline/checkpoint.py"
    )
    assert "except ProcessLookupError:" not in source, (
        "the old ProcessLookupError-only clause must be widened, not duplicated"
    )
    fn_source = inspect.getsource(pcheckpoint._terminate_and_checkpoint)
    assert new_clause in fn_source, (
        "the widened except clause must be at the _terminate_and_checkpoint "
        "os.kill site"
    )


def test_sibling_os_kill_sites_still_handle_permission_error():
    """The fix is scoped to checkpoint.py: concurrency.py and advance.py already
    handle PermissionError at their own os.kill sites and must be left intact."""
    import pipeline.advance as padvance
    import pipeline.concurrency as pconcurrency

    assert "except PermissionError" in Path(pconcurrency.__file__).read_text()
    assert "except PermissionError" in Path(padvance.__file__).read_text()