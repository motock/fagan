"""Tests for the watchdog WIP checkpoint deletion-guard (Mode 23).

ROOT CAUSE: the watchdog WIP checkpoint (pipeline/checkpoint.py's
``_terminate_and_checkpoint`` + the interrupt/dispatch-crash handler in
pipeline/server.py that calls it) used to do ``git add -A && git commit``
unconditionally on interrupt/crash.  When an agent is killed mid-create_file
(SIGTERM mid-write), the worktree can be left with a FILE DELETION and no
corresponding new write.  The watchdog then committed that deletion as WIP,
so a real file was lost and the next dispatch resumed from a corrupted
worktree.

These tests verify the guard: a PURE-DELETION uncommitted diff at watchdog
time must NOT be committed as WIP — the deleted file(s) must be preserved in
the worktree for the next dispatch.  A diff with additions/modifications is
real work-in-progress and must still be checkpointed normally.

The tests exercise the real git boundary (no subprocess mocking) so they
catch regressions in the actual ``git`` invocations, not just the Python
control flow.
"""

import json
import subprocess
from pathlib import Path

import pipeline.checkpoint as pcheckpoint
import pipeline.persistence as ppersist
from pipeline.checkpoint import _terminate_and_checkpoint
from pipeline.git_ops import _commit_wip


# ---------- helpers ----------

def _init_repo(tmp: Path) -> str:
    """Initialize a git repo with an initial commit and return the HEAD sha."""
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    (tmp / "foo.txt").write_text("original\n")
    (tmp / "bar.txt").write_text("barbody\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True, text=True,
    ).stdout.strip()


def _head_sha(tmp: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True, text=True,
    ).stdout.strip()


def _porcelain(tmp: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp, capture_output=True, text=True,
    ).stdout


def _make_manifest(plan_dir: Path, plan_name: str, story_key: str,
                   worktree: str) -> tuple[dict, Path]:
    """Build a minimal manifest + path matching what the server/checkpoint code reads."""
    manifest = {
        "stories": {
            story_key: {
                "worktree": worktree,
                "status": "running",
                "pid": 999999,  # nonexistent pid -> os.kill raises ProcessLookupError
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


# ---------- _commit_wip: pure deletion is NOT committed ----------

def test_commit_wip_guard_single_deletion_preserves_file(tmp_path: Path):
    """A killed-mid-create_file leaving a single deleted file: the watchdog
    checkpoint must NOT commit the deletion; the file must be restored."""
    _init_repo(tmp_path)
    (tmp_path / "foo.txt").unlink()
    assert not (tmp_path / "foo.txt").exists()

    sha = _commit_wip(str(tmp_path), "S1", "step", guard_against_deletion=True)

    # The file is back in the worktree with its original content.
    assert (tmp_path / "foo.txt").exists()
    assert (tmp_path / "foo.txt").read_text() == "original\n"
    # The commit did NOT record the deletion — the file is present at HEAD.
    out = subprocess.check_output(
        ["git", "show", f"{sha}:foo.txt"], cwd=tmp_path,
    ).decode()
    assert out.strip() == "original"
    # Worktree is clean (no uncommitted deletion left dangling).
    assert _porcelain(tmp_path).strip() == ""


def test_commit_wip_guard_multiple_deletions_all_preserved(tmp_path: Path):
    """A pure deletion of MULTIPLE files: none committed; all restored."""
    _init_repo(tmp_path)
    (tmp_path / "foo.txt").unlink()
    (tmp_path / "bar.txt").unlink()

    sha = _commit_wip(str(tmp_path), "S1", "step", guard_against_deletion=True)

    assert (tmp_path / "foo.txt").read_text() == "original\n"
    assert (tmp_path / "bar.txt").read_text() == "barbody\n"
    for name in ("foo.txt", "bar.txt"):
        out = subprocess.check_output(
            ["git", "show", f"{sha}:{name}"], cwd=tmp_path,
        ).decode()
        assert out.strip() in ("original", "barbody")
    assert _porcelain(tmp_path).strip() == ""


# ---------- _commit_wip: additions/modifications ARE committed (regression guard) ----------

def test_commit_wip_guard_modification_still_committed(tmp_path: Path):
    """A legitimate modification (status M) is real WIP and must be committed."""
    _init_repo(tmp_path)
    (tmp_path / "foo.txt").write_text("modified\n")

    sha = _commit_wip(str(tmp_path), "S1", "step", guard_against_deletion=True)

    assert (tmp_path / "foo.txt").read_text() == "modified\n"
    out = subprocess.check_output(
        ["git", "show", f"{sha}:foo.txt"], cwd=tmp_path,
    ).decode()
    assert out.strip() == "modified"
    assert _porcelain(tmp_path).strip() == ""


def test_commit_wip_guard_new_file_addition_still_committed(tmp_path: Path):
    """A brand-new file (status A) is real WIP and must be committed."""
    _init_repo(tmp_path)
    (tmp_path / "new.txt").write_text("brand new\n")

    sha = _commit_wip(str(tmp_path), "S1", "step", guard_against_deletion=True)

    out = subprocess.check_output(
        ["git", "show", f"{sha}:new.txt"], cwd=tmp_path,
    ).decode()
    assert out.strip() == "brand new"
    assert _porcelain(tmp_path).strip() == ""


def test_commit_wip_guard_deletion_plus_addition_is_real_wip(tmp_path: Path):
    """A deletion accompanied by a new file addition (a legitimate
    rename/replace in progress) has an A in the diff, so it is checkpointed as
    real WIP — NOT treated as a pure deletion to be skipped/restored."""
    _init_repo(tmp_path)
    (tmp_path / "foo.txt").unlink()          # deletion
    (tmp_path / "replacement.txt").write_text("replaced\n")  # addition

    sha = _commit_wip(str(tmp_path), "S1", "step", guard_against_deletion=True)

    # The new file is committed.
    out = subprocess.check_output(
        ["git", "show", f"{sha}:replacement.txt"], cwd=tmp_path,
    ).decode()
    assert out.strip() == "replaced"
    # The deletion is also committed (this is real WIP, not a mid-write artifact):
    # foo.txt should NOT be present at HEAD because the agent intentionally
    # deleted it as part of a rename-in-progress that also added a new file.
    try:
        subprocess.check_output(["git", "show", f"{sha}:foo.txt"], cwd=tmp_path)
        foo_at_head = True
    except subprocess.CalledProcessError:
        foo_at_head = False
    assert not foo_at_head, (
        "deletion+addition is real WIP; the deletion must be committed, not restored"
    )
    assert _porcelain(tmp_path).strip() == ""


# ---------- _commit_wip: empty worktree is a no-op, not an error ----------

def test_commit_wip_guard_empty_worktree_is_noop(tmp_path: Path):
    """An empty worktree (no uncommitted changes): checkpoint is a no-op that
    returns the current HEAD sha, not an error."""
    head = _init_repo(tmp_path)
    sha = _commit_wip(str(tmp_path), "S1", "step", guard_against_deletion=True)
    assert sha == head
    assert _porcelain(tmp_path).strip() == ""


def test_commit_wip_no_guard_empty_worktree_is_noop(tmp_path: Path):
    head = _init_repo(tmp_path)
    sha = _commit_wip(str(tmp_path), "S1", "step")
    assert sha == head


# ---------- _commit_wip: without the guard, deletion IS committed (documents the bug) ----------

def test_commit_wip_no_guard_commits_deletion(tmp_path: Path):
    """Without guard_against_deletion (the normal agent self-checkpoint path),
    a deletion IS committed — this documents the pre-fix behavior and ensures
    the guard is opt-in only for the watchdog path."""
    _init_repo(tmp_path)
    (tmp_path / "foo.txt").unlink()

    sha = _commit_wip(str(tmp_path), "S1", "step")  # default guard=False

    assert not (tmp_path / "foo.txt").exists()
    try:
        subprocess.check_output(["git", "show", f"{sha}:foo.txt"], cwd=tmp_path)
        found = True
    except subprocess.CalledProcessError:
        found = False
    assert not found


# ---------- _terminate_and_checkpoint: the watchdog path guards against deletion ----------

def test_terminate_and_checkpoint_pure_deletion_not_committed(
    tmp_path, monkeypatch,
):
    """The watchdog/interrupt path (_terminate_and_checkpoint) must NOT commit a
    pure-deletion diff.  The deleted file must be preserved in the worktree so
    the next dispatch resumes from an intact worktree."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)

    # Simulate a killed-mid-create_file: the file is deleted, nothing else.
    (worktree / "foo.txt").unlink()
    assert not (worktree / "foo.txt").exists()

    manifest, manifest_path = _make_manifest(
        plan_dir, "P1", "S1", str(worktree),
    )

    sha = _terminate_and_checkpoint(
        manifest, manifest_path, "P1", "S1",
        manifest["stories"]["S1"],
        pid=999999, step="dispatch_watchdog_timeout",
        summary="watchdog killed mid-write",
    )

    # The file is preserved in the worktree (restored, not committed-as-deleted).
    assert (worktree / "foo.txt").exists()
    assert (worktree / "foo.txt").read_text() == "original\n"
    # The checkpoint commit did NOT record the deletion.
    out = subprocess.check_output(
        ["git", "show", f"{sha}:foo.txt"], cwd=worktree,
    ).decode()
    assert out.strip() == "original"
    # Worktree is clean after the checkpoint.
    assert _porcelain(worktree).strip() == ""
    # Story is marked interrupted (resume-eligible).
    assert manifest["stories"]["S1"]["status"] == "interrupted"
    assert manifest["stories"]["S1"]["last_commit"] == sha


def test_terminate_and_checkpoint_real_wip_still_committed(
    tmp_path, monkeypatch,
):
    """The watchdog path with real additions/modifications still commits as
    WIP normally — the deletion guard must not break the legitimate interrupt
    checkpoint (regression guard)."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)

    # Real WIP: a modified file + a new file.
    (worktree / "foo.txt").write_text("changed\n")
    (worktree / "new.txt").write_text("new work\n")

    manifest, manifest_path = _make_manifest(
        plan_dir, "P1", "S2", str(worktree),
    )

    sha = _terminate_and_checkpoint(
        manifest, manifest_path, "P1", "S2",
        manifest["stories"]["S2"],
        pid=999999, step="interrupted",
        summary="manual interrupt with real WIP",
    )

    # Both changes are committed.
    out = subprocess.check_output(
        ["git", "show", f"{sha}:foo.txt"], cwd=worktree,
    ).decode()
    assert out.strip() == "changed"
    out = subprocess.check_output(
        ["git", "show", f"{sha}:new.txt"], cwd=worktree,
    ).decode()
    assert out.strip() == "new work"
    assert _porcelain(worktree).strip() == ""
    assert manifest["stories"]["S2"]["status"] == "interrupted"


def test_terminate_and_checkpoint_empty_worktree_noop(
    tmp_path, monkeypatch,
):
    """The watchdog path on an empty worktree (agent already committed, or
    never wrote) is a no-op that returns HEAD — not an error."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    head = _init_repo(worktree)

    manifest, manifest_path = _make_manifest(
        plan_dir, "P1", "S3", str(worktree),
    )

    sha = _terminate_and_checkpoint(
        manifest, manifest_path, "P1", "S3",
        manifest["stories"]["S3"],
        pid=999999, step="dispatch_watchdog_timeout",
        summary="watchdog on clean worktree",
    )

    assert sha == head
    assert _porcelain(worktree).strip() == ""
    assert manifest["stories"]["S3"]["status"] == "interrupted"


def test_terminate_and_checkpoint_multiple_deletions_all_preserved(
    tmp_path, monkeypatch,
):
    """The watchdog path with a pure deletion of MULTIPLE files preserves all
    of them — none committed as WIP."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)

    (worktree / "foo.txt").unlink()
    (worktree / "bar.txt").unlink()

    manifest, manifest_path = _make_manifest(
        plan_dir, "P1", "S4", str(worktree),
    )

    sha = _terminate_and_checkpoint(
        manifest, manifest_path, "P1", "S4",
        manifest["stories"]["S4"],
        pid=999999, step="dispatch_watchdog_timeout",
        summary="watchdog killed mid-write, multiple files",
    )

    assert (worktree / "foo.txt").read_text() == "original\n"
    assert (worktree / "bar.txt").read_text() == "barbody\n"
    for name in ("foo.txt", "bar.txt"):
        out = subprocess.check_output(
            ["git", "show", f"{sha}:{name}"], cwd=worktree,
        ).decode()
        assert out.strip() in ("original", "barbody")
    assert _porcelain(worktree).strip() == ""


def test_terminate_and_checkpoint_deletion_plus_addition_is_real_wip(
    tmp_path, monkeypatch,
):
    """The watchdog path with a deletion + a new file addition treats it as
    real WIP (the diff has an A), so it is checkpointed — the deletion is
    committed, not restored."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)

    (worktree / "foo.txt").unlink()
    (worktree / "replacement.txt").write_text("replaced\n")

    manifest, manifest_path = _make_manifest(
        plan_dir, "P1", "S5", str(worktree),
    )

    sha = _terminate_and_checkpoint(
        manifest, manifest_path, "P1", "S5",
        manifest["stories"]["S5"],
        pid=999999, step="interrupted",
        summary="interrupt mid-rename",
    )

    out = subprocess.check_output(
        ["git", "show", f"{sha}:replacement.txt"], cwd=worktree,
    ).decode()
    assert out.strip() == "replaced"
    try:
        subprocess.check_output(["git", "show", f"{sha}:foo.txt"], cwd=worktree)
        foo_at_head = True
    except subprocess.CalledProcessError:
        foo_at_head = False
    assert not foo_at_head
    assert _porcelain(worktree).strip() == ""


# ---------- resume path still works after a clean checkpoint ----------

def test_resume_after_clean_checkpoint(tmp_path, monkeypatch):
    """After a clean watchdog checkpoint (real WIP committed), the worktree is
    in a resumable state: HEAD has the WIP commit and the worktree is clean,
    so a subsequent dispatch can resume from it."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    _patch_plan_dir(monkeypatch, plan_dir)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)

    (worktree / "progress.txt").write_text("half done\n")

    manifest, manifest_path = _make_manifest(
        plan_dir, "P1", "S6", str(worktree),
    )

    sha = _terminate_and_checkpoint(
        manifest, manifest_path, "P1", "S6",
        manifest["stories"]["S6"],
        pid=999999, step="dispatch_watchdog_timeout",
        summary="watchdog with real progress",
    )

    # The worktree is clean and HEAD == the checkpoint sha — resumable.
    assert _head_sha(worktree) == sha
    assert _porcelain(worktree).strip() == ""
    # A "resume" can make further changes and commit on top.
    (worktree / "more.txt").write_text("more\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "resume"], cwd=worktree, check=True)
    out = subprocess.check_output(
        ["git", "show", "HEAD:more.txt"], cwd=worktree,
    ).decode()
    assert out.strip() == "more"