"""Tests for the patch APPLY engine (WAP-7).

Target API added to ``pipeline/worktree_patch.py``::

    pipeline.worktree_patch.apply_patch(
        plan_name, story_key, patch_id, confirmation_token
    ) -> dict

``apply_patch`` is the single orchestrating entry point of the APPLY half of
the patch pipeline.  Every refusal returns ``{"ok": False, "error": <short
reason>, "status_code": <int>}`` and NEVER touches the worktree; success
returns ``{"ok": True, "patch_id": ..., "applied": <paths>}``.

The steps, in order (earlier refusals win, so ordering is part of the
contract):

1. plan lock (``pipeline.concurrency._plan_lock``) -- the WHOLE body runs
   inside the ``with`` block, so the 60s scheduler tick can never redispatch
   mid-apply;
2. manifest read (lazy ``from .server import PLAN_DIR`` INSIDE the function,
   mirroring ``pipeline/checkpoint.py``, so a test that patches
   ``pipeline.server.PLAN_DIR`` is honoured);
3. stuck-only gate (``in_progress`` / ``running`` are refused);
4. patch record + HMAC confirmation token + single-use status;
5. worktree must be a directory;
6. every new-side hunk path through the strict write resolver (deny list,
   symlink refusal, escape refusal -- all fail closed) BEFORE any write;
7. ``git apply --check`` (argv list only, no shell, no ``--3way``, no fuzzy);
8. ``git apply``;
9. on success ONLY: flip the record to ``applied`` and stamp ``applied_at``.

Scope note: this file grades ONLY what WAP-7 adds.  ``__all__`` is checked by
MEMBERSHIP, never by exact contents, so a later story can extend it without
breaking these tests.

Hermeticity: the git fixture is a real repository under pytest's ``tmp_path``,
resolved first, because on macOS the raw temp path can run through
``/var -> /private/var`` and a symlinked root component would make the write
resolver reject every path for the wrong reason.
"""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import worktree_patch
from pipeline.worktree_patch import (
    apply_patch,
    create_patch_record,
    get_patch_record,
)

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PLAN_NAME = "wap7plan"
STORY_KEY = "WAP-7"

#: Captured at import time so the subprocess SPY installed by a test never
#: intercepts the test's own git plumbing.
_REAL_RUN = subprocess.run


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real git command in *repo* (never through the spy)."""
    return _REAL_RUN(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _status(repo: Path) -> str:
    """``git status --porcelain`` for *repo* (real git, never the spy)."""
    return _git(repo, "status", "--porcelain").stdout


def _status_all(repo: Path) -> str:
    """``git status --porcelain -uall``: lists untracked FILES, not dirs."""
    return _git(repo, "status", "--porcelain", "-uall").stdout


def _tracked_hashes(repo: Path) -> dict[str, str]:
    """sha256 of every TRACKED file in *repo*, keyed by relative path."""
    names = _git(repo, "ls-files").stdout.split()
    return {
        name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
        for name in names
    }


def _unified(rel_path: str, old_text: str, new_text: str) -> str:
    """A plain unified diff (no ``diff --git``/``index`` lines).

    ``parse_unified_diff`` recognizes only ``--- ``/``+++ ``/``@@`` blocks, so
    the diff must be built without git's extra headers; ``git apply`` accepts
    this format natively.
    """
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )


def _new_file_diff(rel_path: str, new_text: str) -> str:
    """A creation diff: ``--- /dev/null`` / ``+++ b/<rel_path>``."""
    return "".join(
        difflib.unified_diff(
            [],
            new_text.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )


def _delete_file_diff(rel_path: str, old_text: str) -> str:
    """A deletion-only diff: ``+++ /dev/null`` (no new-side path)."""
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            [],
            fromfile=f"a/{rel_path}",
            tofile="/dev/null",
            n=3,
        )
    )


def _write_manifest(plan_dir: Path, stories: dict, plan_name: str = PLAN_NAME) -> Path:
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps({"stories": stories}))
    return path


def _patch_store() -> dict:
    """The module-level patch-record store dict.

    The brief names it ``_PATCH_STORE``; the fallback discovery keeps this
    file working if a later refactor renames the private attribute.
    """
    store = getattr(worktree_patch, "_PATCH_STORE", None)
    if isinstance(store, dict):
        return store
    for name, value in vars(worktree_patch).items():
        if name.startswith("__") or not isinstance(value, dict):
            continue
        if any(isinstance(v, dict) and "patch_id" in v for v in value.values()):
            return value
    raise AssertionError("pipeline.worktree_patch exposes no patch-record store")


def _assert_refusal(result, *, error: str | None = None, status_code: int | None = None):
    """Every refusal has the same shape: ok/error/status_code."""
    assert isinstance(result, dict), f"refusal must be a dict, got {result!r}"
    assert result["ok"] is False
    assert isinstance(result["error"], str) and result["error"], result
    assert isinstance(result["status_code"], int), result
    if error is not None:
        assert result["error"] == error, result
    if status_code is not None:
        assert result["status_code"] == status_code, result


class _SubprocessSpy:
    """Records every ``subprocess.run`` call and fakes a git result.

    Also records whether the plan lock was held at call time, which is how the
    "the whole body runs inside the with-block" requirement is graded.
    """

    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        from pipeline import concurrency

        argv = args[0] if args else kwargs.get("args")
        self.calls.append(
            {
                "argv": list(argv) if argv is not None else None,
                "kwargs": dict(kwargs),
                "lock_held": PLAN_NAME
                in getattr(concurrency, "_process_plan_holders", set()),
            }
        )
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout=b"", stderr=self.stderr
        )

    @property
    def argvs(self) -> list:
        return [call["argv"] for call in self.calls]


def _install_spy(monkeypatch, spy: _SubprocessSpy) -> _SubprocessSpy:
    """Intercept ``subprocess.run`` however the module spells the call."""
    monkeypatch.setattr(subprocess, "run", spy)
    if hasattr(worktree_patch, "run"):
        monkeypatch.setattr(worktree_patch, "run", spy)
    return spy


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_patch_store():
    """Snapshot/restore the in-process patch store around every test."""
    store = _patch_store()
    snapshot = dict(store)
    yield
    store.clear()
    store.update(snapshot)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real, committed git repository to act as the story worktree."""
    root = (tmp_path / "worktree").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test Author")
    (root / "app.py").write_text("alpha\nbeta\ngamma\n")
    (root / "notes.txt").write_text("one\ntwo\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def manifest(plan_dir: Path, repo: Path) -> dict:
    """A manifest whose one story is STUCK (``failed``) with a real worktree."""
    stories = {STORY_KEY: {"status": "failed", "worktree": str(repo)}}
    _write_manifest(plan_dir, stories)
    return stories


def _record(diff_text: str, paths: list[str], added_lines: int = 1) -> dict:
    return create_patch_record(PLAN_NAME, STORY_KEY, diff_text, paths, added_lines)


# --------------------------------------------------------------------------
# export surface
# --------------------------------------------------------------------------


def test_apply_patch_is_exported():
    assert callable(worktree_patch.apply_patch)
    assert "apply_patch" in worktree_patch.__all__


#: Public module-level functions that existed BEFORE this story (WAP-4..WAP-6).
_BASELINE_PUBLIC_FUNCTIONS = {
    "is_denied_relative_path",
    "resolve_write_target",
    "parse_unified_diff",
    "validate_for_propose",
    "create_patch_record",
    "get_patch_record",
}


def test_this_story_adds_no_extra_public_function():
    """``apply_patch`` is the ONLY new public function this story adds.

    The brief caps the story at two new functions (``apply_patch`` plus the
    optional private ``_run_git_apply``), so no other PUBLIC name may appear.
    Private helpers are deliberately NOT constrained here: a later sibling
    story may legitimately add one.
    """
    public = {
        name
        for name, value in vars(worktree_patch).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and getattr(value, "__module__", None) == worktree_patch.__name__
    }
    assert public - _BASELINE_PUBLIC_FUNCTIONS <= {"apply_patch"}, (
        public - _BASELINE_PUBLIC_FUNCTIONS
    )


def test_optional_run_git_apply_helper_has_the_documented_signature():
    """``_run_git_apply(worktree, diff_text, check_only)`` is ALLOWED, not
    required -- but if it exists it must be callable with those three
    positional arguments."""
    helper = getattr(worktree_patch, "_run_git_apply", None)
    if helper is None:
        pytest.skip("_run_git_apply is optional and was not added")
    assert callable(helper)
    params = list(inspect.signature(helper).parameters)
    assert params[:3] == ["worktree", "diff_text", "check_only"], params


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_happy_path_applies_patch_and_flips_record(plan_dir, repo, manifest):
    new_text = "alpha\nBETA\ngamma\n"
    diff = _unified("app.py", (repo / "app.py").read_text(), new_text)
    rec = _record(diff, ["app.py"])

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert result["patch_id"] == rec["patch_id"]
    assert sorted(result["applied"]) == ["app.py"]
    assert (repo / "app.py").read_text() == new_text
    assert " M app.py" in _status(repo)

    stored = get_patch_record(rec["patch_id"])
    assert stored["status"] == "applied"
    assert isinstance(stored["applied_at"], str)
    datetime.fromisoformat(stored["applied_at"])  # parseable ISO-8601


def test_happy_path_returns_every_resolved_relative_path(plan_dir, repo, manifest):
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    diff += _new_file_diff("src/deep.py", "deep = True\n")
    rec = _record(diff, ["app.py", "src/deep.py"], added_lines=2)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert sorted(result["applied"]) == ["app.py", "src/deep.py"]
    assert (repo / "src" / "deep.py").read_text() == "deep = True\n"
    assert " M app.py" in _status(repo)
    assert "?? src/deep.py" in _status_all(repo)


def test_new_file_diff_creates_the_file(plan_dir, repo, manifest):
    diff = _new_file_diff("created.txt", "hello\n")
    rec = _record(diff, ["created.txt"])

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert sorted(result["applied"]) == ["created.txt"]
    assert (repo / "created.txt").read_text() == "hello\n"


def test_deletion_only_diff_skips_dev_null(plan_dir, repo, manifest):
    """``+++ /dev/null`` contributes NO new-side path: nothing to resolve."""
    diff = _delete_file_diff("notes.txt", (repo / "notes.txt").read_text())
    rec = _record(diff, [], added_lines=0)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert list(result["applied"]) == []
    assert not (repo / "notes.txt").exists()
    assert " D notes.txt" in _status(repo)


# --------------------------------------------------------------------------
# step 3: stuck-only gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["in_progress", "running"])
def test_active_story_refused_409_and_git_never_invoked(
    plan_dir, repo, monkeypatch, status
):
    _write_manifest(plan_dir, {STORY_KEY: {"status": status, "worktree": str(repo)}})
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))
    before = (repo / "app.py").read_bytes()

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="story is active", status_code=409)
    assert spy.calls == []
    assert (repo / "app.py").read_bytes() == before
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


@pytest.mark.parametrize(
    "status", ["parked", "failed", "changes_requested", "interrupted"]
)
def test_stuck_story_statuses_are_not_gated(plan_dir, repo, status):
    """Direct repair targets stuck stories by definition."""
    _write_manifest(plan_dir, {STORY_KEY: {"status": status, "worktree": str(repo)}})
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result


# --------------------------------------------------------------------------
# step 2: manifest / story lookup
# --------------------------------------------------------------------------


def test_unknown_plan_404(plan_dir, repo, monkeypatch):
    """No manifest file at all."""
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="unknown plan or story", status_code=404)
    assert spy.calls == []


def test_unknown_story_404(plan_dir, repo, monkeypatch):
    _write_manifest(plan_dir, {"OTHER-1": {"status": "failed", "worktree": str(repo)}})
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="unknown plan or story", status_code=404)
    assert spy.calls == []


def test_manifest_is_read_from_the_patched_server_plan_dir(plan_dir, repo, manifest):
    """The lazy ``from .server import PLAN_DIR`` must honour the fixture.

    The manifest lives ONLY in the patched ``pipeline.server.PLAN_DIR``; a
    module-load-time import of PLAN_DIR would look in the operator's real
    plans directory and refuse with 404.
    """
    assert (plan_dir / f"{PLAN_NAME}.manifest.json").exists()
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result


# --------------------------------------------------------------------------
# step 4: record, token, single-use
# --------------------------------------------------------------------------


def test_fabricated_patch_id_404(plan_dir, repo, manifest, monkeypatch):
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(PLAN_NAME, STORY_KEY, "wp-not-a-real-id", "whatever")

    _assert_refusal(result, status_code=404)
    assert spy.calls == []


def test_non_string_patch_id_fails_closed_404(plan_dir, repo, manifest, monkeypatch):
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(PLAN_NAME, STORY_KEY, None, "whatever")

    _assert_refusal(result, status_code=404)
    assert spy.calls == []


def test_expired_record_404(plan_dir, repo, manifest, monkeypatch):
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    stored = get_patch_record(rec["patch_id"])
    stored["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))
    before = (repo / "app.py").read_bytes()

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, status_code=404)
    assert spy.calls == []
    assert (repo / "app.py").read_bytes() == before


def test_token_from_a_different_patch_403_and_neither_consumed(
    plan_dir, repo, manifest, monkeypatch
):
    diff_a = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    diff_b = _unified("app.py", (repo / "app.py").read_text(), "alpha\nGAMMA\nbeta\n")
    rec_a = _record(diff_a, ["app.py"])
    rec_b = _record(diff_b, ["app.py"])
    assert rec_a["patch_id"] != rec_b["patch_id"]
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))
    before = (repo / "app.py").read_bytes()

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec_a["patch_id"], rec_b["confirmation_token"]
    )

    _assert_refusal(result, error="invalid confirmation token", status_code=403)
    assert spy.calls == []
    assert (repo / "app.py").read_bytes() == before
    assert get_patch_record(rec_a["patch_id"])["status"] == "pending"
    assert get_patch_record(rec_b["patch_id"])["status"] == "pending"


def test_token_is_bound_to_the_diff_hash(plan_dir, repo, manifest, monkeypatch):
    """A token minted for a DIFFERENT diff is refused."""
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    stored = get_patch_record(rec["patch_id"])
    stored["diff_hash"] = "0" * 64
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="invalid confirmation token", status_code=403)
    assert spy.calls == []
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


@pytest.mark.parametrize("token", ["", "not-a-token", "0" * 64])
def test_bad_token_403(plan_dir, repo, manifest, monkeypatch, token):
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(PLAN_NAME, STORY_KEY, rec["patch_id"], token)

    _assert_refusal(result, error="invalid confirmation token", status_code=403)
    assert spy.calls == []
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


def test_successful_apply_is_single_use(plan_dir, repo, manifest):
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])

    first = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )
    assert first["ok"] is True, first

    second = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(second, error="patch already applied", status_code=409)
    assert (repo / "app.py").read_text() == "alpha\nBETA\ngamma\n"


# --------------------------------------------------------------------------
# step 5: worktree must be a directory
# --------------------------------------------------------------------------


def test_worktree_not_a_directory_409(plan_dir, repo, tmp_path, monkeypatch):
    missing = tmp_path / "no-such-worktree"
    _write_manifest(
        plan_dir, {STORY_KEY: {"status": "failed", "worktree": str(missing)}}
    )
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, status_code=409)
    assert spy.calls == []
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


# --------------------------------------------------------------------------
# step 6: deny list / symlink refusal (before ANY write)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", [".git/config", "CLAUDE.md", "agent.log"])
def test_deny_list_refused_403_and_target_byte_identical(
    plan_dir, repo, manifest, monkeypatch, rel_path
):
    diff = _unified(rel_path, "old\n", "new\n")
    rec = _record(diff, [rel_path])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))
    target = repo / rel_path
    before = target.read_bytes() if target.exists() else None
    status_before = _status(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="patch target refused", status_code=403)
    assert spy.calls == []
    after = target.read_bytes() if target.exists() else None
    assert after == before
    assert _status(repo) == status_before
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


def test_deny_list_refuses_the_whole_patch_when_one_path_is_denied(
    plan_dir, repo, manifest, monkeypatch
):
    """The deny list runs on EVERY hunk path, before any write."""
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    diff += _unified(".git/config", "old\n", "new\n")
    rec = _record(diff, ["app.py", ".git/config"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))
    before = (repo / "app.py").read_bytes()

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="patch target refused", status_code=403)
    assert spy.calls == []
    assert (repo / "app.py").read_bytes() == before


def test_symlink_parent_outside_worktree_refused_403(
    plan_dir, repo, manifest, monkeypatch, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "linkdir").symlink_to(outside, target_is_directory=True)
    diff = _new_file_diff("linkdir/evil.txt", "pwned\n")
    rec = _record(diff, ["linkdir/evil.txt"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))
    status_before = _status(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="patch target refused", status_code=403)
    assert spy.calls == []
    assert not (outside / "evil.txt").exists()
    assert _status(repo) == status_before


def test_symlink_parent_inside_worktree_refused_403(
    plan_dir, repo, manifest, monkeypatch
):
    (repo / "sub").mkdir()
    (repo / "inlink").symlink_to(repo / "sub", target_is_directory=True)
    diff = _new_file_diff("inlink/new.txt", "x\n")
    rec = _record(diff, ["inlink/new.txt"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))
    status_before = _status(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="patch target refused", status_code=403)
    assert spy.calls == []
    assert not (repo / "sub" / "new.txt").exists()
    assert _status(repo) == status_before


def test_escape_path_refused_403(plan_dir, repo, manifest, monkeypatch):
    diff = _new_file_diff("../escaped.txt", "nope\n")
    rec = _record(diff, ["../escaped.txt"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="patch target refused", status_code=403)
    assert spy.calls == []
    assert not (repo.parent / "escaped.txt").exists()


# --------------------------------------------------------------------------
# step 7: context drift
# --------------------------------------------------------------------------


def test_context_drift_409_and_worktree_byte_identical(plan_dir, repo, manifest):
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    # Drift: the target changes AFTER the record was minted.
    (repo / "app.py").write_text("alpha\nDRIFTED\ngamma\n")
    status_before = _status(repo)
    hashes_before = _tracked_hashes(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(
        result, error="patch does not apply (context drift)", status_code=409
    )
    assert result["detail"], "the refusal must carry a stderr snippet"
    assert isinstance(result["detail"], (str, bytes))
    assert _status(repo) == status_before
    assert _tracked_hashes(repo) == hashes_before
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


def test_failed_apply_leaves_the_record_pending_and_retryable(plan_dir, repo, manifest):
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    (repo / "app.py").write_text("alpha\nDRIFTED\ngamma\n")

    drifted = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )
    _assert_refusal(drifted, status_code=409)
    assert get_patch_record(rec["patch_id"])["status"] == "pending"

    # Undo the drift: the SAME token now applies.
    (repo / "app.py").write_text("alpha\nbeta\ngamma\n")
    retried = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert retried["ok"] is True, retried
    assert (repo / "app.py").read_text() == "alpha\nBETA\ngamma\n"


def test_empty_diff_is_refused_and_worktree_untouched(plan_dir, repo, manifest):
    """Boundary: a record with no hunks must not touch the worktree.

    ``parse_unified_diff("")`` yields zero paths, so nothing is resolved and
    the refusal comes from ``git apply --check`` (real git: "No valid patches
    in input", rc 128) -- the worktree stays byte-identical.
    """
    rec = _record("", [], added_lines=0)
    status_before = _status(repo)
    hashes_before = _tracked_hashes(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(
        result, error="patch does not apply (context drift)", status_code=409
    )
    assert _status(repo) == status_before
    assert _tracked_hashes(repo) == hashes_before
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


# --------------------------------------------------------------------------
# step 1: the plan lock
# --------------------------------------------------------------------------


def test_plan_busy_409_when_another_thread_holds_the_lock(
    plan_dir, repo, manifest, monkeypatch
):
    from pipeline.concurrency import _plan_lock

    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))
    before = (repo / "app.py").read_bytes()

    acquired_evt = threading.Event()
    release_evt = threading.Event()

    def _holder():
        with _plan_lock(PLAN_NAME) as acquired:
            assert acquired, "the holder thread must acquire the plan lock"
            acquired_evt.set()
            release_evt.wait(15)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert acquired_evt.wait(15), "holder thread never acquired the plan lock"
    try:
        result = apply_patch(
            PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
        )
    finally:
        release_evt.set()
        holder.join(15)

    _assert_refusal(result, error="plan busy", status_code=409)
    assert spy.calls == []
    assert (repo / "app.py").read_bytes() == before
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


def test_git_apply_runs_while_the_plan_lock_is_held(
    plan_dir, repo, manifest, monkeypatch
):
    """The whole body runs inside the ``with _plan_lock(...)`` block."""
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert len(spy.calls) == 2
    assert all(call["lock_held"] for call in spy.calls)


# --------------------------------------------------------------------------
# step 7/8: the exact git invocation
# --------------------------------------------------------------------------


def test_argv_is_exact_and_never_uses_shell_or_3way(plan_dir, repo, manifest, monkeypatch):
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])
    spy = _install_spy(monkeypatch, _SubprocessSpy(returncode=0))

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert spy.argvs == [["git", "apply", "--check", "-"], ["git", "apply", "-"]]
    for call in spy.calls:
        argv = call["argv"]
        kwargs = call["kwargs"]
        assert argv[0] == "git"
        assert "--3way" not in argv
        assert "-3" not in argv
        assert "--fuzz" not in argv
        assert "--allow-empty" not in argv
        assert kwargs.get("shell") is not True
        assert "shell" not in kwargs or kwargs["shell"] is False
        assert kwargs.get("input") == diff.encode("utf-8")
        assert Path(kwargs.get("cwd")) == repo
        assert kwargs.get("capture_output") is True


def test_apply_failure_after_a_passing_check_is_409(plan_dir, repo, manifest, monkeypatch):
    """``git apply`` failing after ``--check`` passed -> 409 ``apply failed``."""
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])

    calls: list[list[str]] = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        rc = 0 if "--check" in argv else 1
        return subprocess.CompletedProcess(
            args=argv, returncode=rc, stdout=b"", stderr=b"boom\n"
        )

    monkeypatch.setattr(subprocess, "run", _run)
    if hasattr(worktree_patch, "run"):
        monkeypatch.setattr(worktree_patch, "run", _run)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="apply failed", status_code=409)
    assert calls == [["git", "apply", "--check", "-"], ["git", "apply", "-"]]
    assert get_patch_record(rec["patch_id"])["status"] == "pending"


# --------------------------------------------------------------------------
# exclusion: no production caller outside the (later) dashboard route
# --------------------------------------------------------------------------


def test_apply_patch_is_not_wired_into_server_dispatch_overlord_or_advance():
    root = Path(__file__).resolve().parents[2]
    for rel in (
        "pipeline/server.py",
        "pipeline/dispatch.py",
        "pipeline/overlord.py",
        "pipeline/advance.py",
    ):
        text = (root / rel).read_text()
        assert "apply_patch" not in text, f"{rel} must not reference apply_patch"
