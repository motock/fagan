"""Wiring tests: ``mark_story_done`` removes the story's worktree.

``pipeline.ci._mark_story_done_impl`` records the story as done and then
cleans up the story's worktree: the worktree directory and its local branch
are removed when the worktree is clean and lives inside ``WORKTREE_ROOT``.

These tests drive the real ``pipeline.ci._mark_story_done_impl`` against a
manifest whose story points at a real git worktree created under ``tmp_path``
and assert the observable outcome: the return value, the manifest status and
the state of the worktree on disk.

The return shape is part of the contract: it stays exactly ``{"ok": True}``
(or the plan-completed dict), and a failing cleanup never changes it.
"""

import json
import logging
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import ci as pci
from pipeline import paths as ppaths
from pipeline import persistence as ppers

PLAN = "p1"
STORY = "S1"

REPO = Path(__file__).resolve().parents[2]
REFERENCE_MD = REPO / "REFERENCE.md"

# The mark_story_done docstring had 21 lines before this story; the edit must
# not grow it.
DOCSTRING_BASE_LINES = 21


def _normalized(text: str) -> str:
    return " ".join(text.split()).lower()


def _reference_section_around(entry: str, *, include_following: bool = False) -> str:
    """The REFERENCE.md section that contains *entry* (heading to next heading).

    With *include_following* the section immediately after the entry's own
    section is appended too. The cleanup rule is stated in its own short
    section directly below the MCP tool list rather than inside it, because
    tests/unit/test_readme_reference_split.py pins that list byte-for-byte
    against the pre-split README; "next to the entry" therefore means the
    entry's section plus the section that follows it.
    """
    lines = REFERENCE_MD.read_text(encoding="utf-8").splitlines()
    index = next((i for i, line in enumerate(lines) if entry in line), None)
    assert index is not None, f"REFERENCE.md has no {entry!r} entry"
    start = index
    while start > 0 and not lines[start].startswith("#"):
        start -= 1
    end = index + 1
    while end < len(lines) and not lines[end].startswith("#"):
        end += 1
    if include_following:
        assert end < len(lines), f"REFERENCE.md has no section after {entry!r}"
        end += 1
        while end < len(lines) and not lines[end].startswith("#"):
            end += 1
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


class _NullProvider:
    def set_state(self, *args, **kwargs):
        return None


@pytest.fixture
def story_env(tmp_path, monkeypatch):
    """A real repo + worktree, a patched WORKTREE_ROOT and an isolated PLAN_DIR."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "Tester", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "chore: init", cwd=repo)

    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    worktree = worktree_root / "k1"
    _git("worktree", "add", "-b", "agent/s1", str(worktree), cwd=repo)

    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullProvider())

    # WORKTREE_ROOT is read live from the server binding (via _ServerRef /
    # LiveRef) but may also be imported at module scope - patch every binding
    # so the test does not depend on which one the implementation reads.
    monkeypatch.setattr(p, "WORKTREE_ROOT", worktree_root)
    monkeypatch.setattr(ppaths, "WORKTREE_ROOT", worktree_root)
    monkeypatch.setattr(pci, "WORKTREE_ROOT", worktree_root, raising=False)

    # The plan's repo: patched on the server (the implementation reads
    # _repo_root_for(plan_name) live) and recorded in the manifest too.
    monkeypatch.setattr(p, "_repo_root_for", lambda plan_name: repo)

    return {
        "repo": repo,
        "worktree_root": worktree_root,
        "worktree": worktree,
        "plan_dir": plan_dir,
    }


def _write_manifest(env, stories=None):
    stories = stories or {
        STORY: {
            "summary": "a story",
            "status": "in_progress",
            "worktree": str(env["worktree"]),
        },
        "S2": {"summary": "another story", "status": "todo"},
    }
    manifest = {"repo_root": str(env["repo"]), "stories": stories}
    (env["plan_dir"] / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))


def _read_manifest(env):
    return json.loads((env["plan_dir"] / f"{PLAN}.manifest.json").read_text())


def _patch_cleanup(monkeypatch, replacement):
    """Point every plausible binding of the cleanup helper at *replacement*."""
    from pipeline import worktree_cleanup as pwc

    monkeypatch.setattr(pwc, "remove_finished_worktree", replacement)
    monkeypatch.setattr(pci, "remove_finished_worktree", replacement, raising=False)
    monkeypatch.setattr(p, "remove_finished_worktree", replacement, raising=False)


def _record_subprocess_calls(monkeypatch, sampler=None):
    """Record every subprocess argv, tagged with ``sampler()`` at call time."""
    seen = []
    real_run = subprocess.run
    real_check_call = subprocess.check_call
    real_check_output = subprocess.check_output
    real_popen = subprocess.Popen

    def _rec(argv):
        if isinstance(argv, (list, tuple)):
            seen.append((list(argv), sampler() if sampler else None))
        elif isinstance(argv, str):
            seen.append((argv.split(), sampler() if sampler else None))

    def run(argv, *a, **kw):
        _rec(argv)
        return real_run(argv, *a, **kw)

    def check_call(argv, *a, **kw):
        _rec(argv)
        return real_check_call(argv, *a, **kw)

    def check_output(argv, *a, **kw):
        _rec(argv)
        return real_check_output(argv, *a, **kw)

    class Popen(real_popen):
        def __init__(self, argv, *a, **kw):
            _rec(argv)
            super().__init__(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_call", check_call)
    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    return seen


def _same_path(value, expected):
    return Path(str(value)) == Path(str(expected)) or (
        Path(str(value)).resolve() == Path(str(expected)).resolve()
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_a_clean_story_worktree_is_removed(story_env):
    """mark_story_done removes the story's clean worktree and local branch."""
    _write_manifest(story_env)

    result = pci._mark_story_done_impl(PLAN, STORY)

    assert result == {"ok": True}
    manifest = _read_manifest(story_env)
    assert manifest["stories"][STORY]["status"] == "done"
    assert not story_env["worktree"].exists(), "the worktree must be removed"
    branches = _git("branch", "--list", "agent/s1", cwd=story_env["repo"]).stdout.strip()
    assert branches == "", "the story's local branch must be deleted"


def test_the_cleanup_is_called_with_the_story_worktree_and_root(story_env, monkeypatch):
    """The cleanup receives the plan's repo, the story's worktree and the root."""
    _write_manifest(story_env)
    calls = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return "absent"

    _patch_cleanup(monkeypatch, _spy)

    pci._mark_story_done_impl(PLAN, STORY)

    assert calls, "the cleanup helper must be called"
    args, kwargs = calls[0]
    repo_root = args[0] if len(args) > 0 else kwargs.get("repo_root")
    worktree = args[1] if len(args) > 1 else kwargs.get("worktree")
    story_key = args[2] if len(args) > 2 else kwargs.get("story_key")
    assert _same_path(repo_root, story_env["repo"]), f"repo_root was {repo_root!r}"
    assert _same_path(worktree, story_env["worktree"]), f"worktree was {worktree!r}"
    assert story_key == STORY, f"story_key was {story_key!r}"
    worktree_root = kwargs.get("worktree_root")
    assert worktree_root is not None, "worktree_root must be passed"
    assert _same_path(worktree_root, story_env["worktree_root"])


def test_the_cleanup_outcome_is_logged_at_info(story_env, caplog):
    """The outcome token is logged at INFO together with the identifiers."""
    _write_manifest(story_env)

    with caplog.at_level(logging.INFO):
        pci._mark_story_done_impl(PLAN, STORY)

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert infos, "the cleanup outcome must be logged at INFO"
    blob = " ".join(f"{r.getMessage()} {r.__dict__}" for r in infos)
    assert "removed" in blob, f"the outcome token must be logged; got {blob!r}"
    assert STORY in blob, f"the story key must be logged; got {blob!r}"
    assert PLAN in blob, f"the plan name must be logged; got {blob!r}"


def test_a_failing_cleanup_does_not_break_mark_story_done(story_env, monkeypatch, caplog):
    """A cleanup that raises must not change the return value or the status."""
    _write_manifest(story_env)

    def _boom(*args, **kwargs):
        raise RuntimeError("cleanup exploded")

    _patch_cleanup(monkeypatch, _boom)

    with caplog.at_level(logging.WARNING):
        result = pci._mark_story_done_impl(PLAN, STORY)

    assert result == {"ok": True}
    assert _read_manifest(story_env)["stories"][STORY]["status"] == "done"
    assert story_env["worktree"].exists(), "a failed cleanup removes nothing"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a failing cleanup must log a WARNING"


def test_git_never_runs_while_the_plan_lock_is_held(story_env, monkeypatch):
    """The cleanup runs after the plan lock has been released."""
    _write_manifest(story_env)
    held = {"value": False}

    @contextmanager
    def _txn(name):
        held["value"] = True
        try:
            yield True
        finally:
            held["value"] = False

    monkeypatch.setattr(p._store, "transaction", _txn)

    seen = _record_subprocess_calls(monkeypatch, sampler=lambda: held["value"])

    result = pci._mark_story_done_impl(PLAN, STORY)

    assert result == {"ok": True}
    removals = [argv for argv, _ in seen if argv[:3] == ["git", "worktree", "remove"]]
    assert removals, "the worktree removal must actually run"
    git_under_lock = [argv for argv, was_held in seen if was_held and argv[:1] == ["git"]]
    assert not git_under_lock, f"git must not run under the plan lock: {git_under_lock!r}"


def test_the_locked_path_does_not_clean_the_worktree(story_env, monkeypatch):
    """skipped/locked is a no-op: no cleanup, no worktree removal."""
    _write_manifest(story_env)

    @contextmanager
    def _txn(name):
        yield False

    monkeypatch.setattr(p._store, "transaction", _txn)

    result = pci._mark_story_done_impl(PLAN, STORY)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    assert story_env["worktree"].exists(), "the locked path must not remove the worktree"
    assert _read_manifest(story_env)["stories"][STORY]["status"] == "in_progress"


def test_the_plan_completed_return_shape_is_unchanged(story_env):
    """A single-story plan still returns the plan-completed dict."""
    _write_manifest(
        story_env,
        stories={
            STORY: {
                "summary": "a story",
                "status": "in_progress",
                "worktree": str(story_env["worktree"]),
            }
        },
    )

    result = pci._mark_story_done_impl(PLAN, STORY)

    assert result == {"ok": True, "plan_completed": True, "stories": [STORY]}


# ---------------------------------------------------------------------------
# documentation: the server docstring and REFERENCE.md
# ---------------------------------------------------------------------------


def test_the_docstring_no_longer_says_it_only_records_the_work():
    """The stale 'it only records that the work is done' wording is gone."""
    doc = p.mark_story_done.__doc__ or ""

    assert "it only records that the work is done" not in _normalized(doc)


def test_the_docstring_describes_the_worktree_cleanup():
    """The docstring names the worktree cleanup, the root and the branch."""
    doc = _normalized(p.mark_story_done.__doc__ or "")

    assert "worktree" in doc, "the docstring must mention the worktree cleanup"
    assert "worktree_root" in doc, "the docstring must name WORKTREE_ROOT"
    assert "branch" in doc, "the docstring must mention the local branch"


def test_the_docstring_did_not_grow():
    """The docstring edit is net zero lines."""
    doc = p.mark_story_done.__doc__ or ""

    assert len(doc.splitlines()) <= DOCSTRING_BASE_LINES, (
        "the mark_story_done docstring must not grow beyond "
        f"{DOCSTRING_BASE_LINES} lines; got {len(doc.splitlines())}"
    )


def test_the_reference_states_the_cleanup_rule():
    """The cleanup rule is stated next to the tool-list entry."""
    section = _normalized(
        _reference_section_around("`mark_story_done(...)`", include_following=True)
    )

    assert "worktree" in section, "the entry must mention the worktree"
    assert "worktree_root" in section, "the entry must name WORKTREE_ROOT"
    assert "dirty" in section, "the entry must say a dirty worktree is kept"
    assert "branch" in section, "the entry must mention the branch"
    assert "remote" in section, "the entry must say the remote branch is untouched"


def test_the_reference_has_no_stale_only_records_wording():
    """The stale sentence is gone from the reference too (if it was there)."""
    text = REFERENCE_MD.read_text(encoding="utf-8")

    assert not re.search(
        r"it only records that the work is done", text, flags=re.IGNORECASE
    )
