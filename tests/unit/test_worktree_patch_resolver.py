"""Tests for the STRICT write-mode resolver (WAP-4B).

Target API (apply-side only)::

    pipeline.worktree_patch.resolve_write_target(worktree_root, relative_path) -> Path

The resolver is the write half of the path-security pair whose read half is
``pipeline.workspace_fs.resolve_within_workspace``.  It is deliberately
STRICTER than the read half: a symlink is refused even when every hop stays
inside the worktree, because a write through an in-worktree symlink is still a
write the patch author did not name.

Hermeticity: every filesystem fixture is built under pytest's ``tmp_path`` and
the pipeline's own ``REPO_ROOT`` / ``PLAN_DIR`` bindings are monkeypatched onto
temp fixtures, so no test ever reads the real ``~/.claude/plans``.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from pipeline import worktree_patch
from pipeline.workspace import WorkspaceSecurityError
from pipeline.workspace_fs import resolve_within_workspace
from pipeline.worktree_patch import PatchSecurityError, resolve_write_target

# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A small, symlink-free worktree tree.

    ``tmp_path`` is resolved first: on macOS the raw temp path can run through
    ``/var -> /private/var``, and a symlinked *root* component would make the
    resolver's component walk reject every path for the wrong reason.
    """
    root = (tmp_path / "worktree").resolve()
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "docs").mkdir()
    (root / "docs" / "readme.md").write_text("hi\n")
    return root


def _optional_module(name: str):
    """Import *name*, or return ``None`` when it is not importable."""
    try:
        return importlib.import_module(name)
    except ImportError:  # pragma: no cover - defensive, module may not exist
        return None


def _patch_pipeline_constants(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo_root: Path | None = None,
    plan_dir: Path | None = None,
) -> None:
    """Patch every plausible binding of ``REPO_ROOT`` / ``PLAN_DIR``.

    The implementation is expected to import these lazily from
    ``pipeline.server`` (the pattern used by ``pipeline.checkpoint``), but it
    may also bind them eagerly; patch all of them so the test stays hermetic
    whichever import style is used.
    """
    modules = [worktree_patch]
    for name in ("pipeline.paths", "pipeline.server"):
        module = _optional_module(name)
        if module is not None:
            modules.append(module)
    for module in modules:
        if repo_root is not None:
            monkeypatch.setattr(module, "REPO_ROOT", repo_root, raising=False)
            monkeypatch.setattr(
                module, "PIPELINE_SELF_REPO_ROOT", repo_root, raising=False
            )
        if plan_dir is not None:
            monkeypatch.setattr(module, "PLAN_DIR", plan_dir, raising=False)


# --------------------------------------------------------------------------
# export surface
# --------------------------------------------------------------------------


def test_resolver_is_exported_alongside_wap4a_symbols() -> None:
    assert "resolve_write_target" in worktree_patch.__all__
    assert callable(worktree_patch.resolve_write_target)
    # WAP-4A's symbols must survive the extension of the shared __all__.
    assert "PatchSecurityError" in worktree_patch.__all__
    assert "is_denied_relative_path" in worktree_patch.__all__


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_existing_target_resolves(worktree: Path) -> None:
    result = resolve_write_target(str(worktree), "src/app.py")

    assert isinstance(result, Path)
    assert result.is_absolute()
    assert result == (worktree / "src" / "app.py")


def test_nonexistent_deep_target_is_allowed(worktree: Path) -> None:
    """A patch may CREATE files: deep nonexistent paths with clean parents pass."""
    result = resolve_write_target(str(worktree), "src/new/deep/file.py")

    assert result == worktree / "src" / "new" / "deep" / "file.py"
    assert not result.exists()


def test_dot_resolves_to_the_worktree_root_itself(worktree: Path) -> None:
    """The root itself is a legal target (containment is root-or-descendant)."""
    assert resolve_write_target(str(worktree), ".") == worktree


# --------------------------------------------------------------------------
# read-half rejections propagate unchanged (WorkspaceSecurityError)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../escape",
        "../../etc/passwd",
        "src/../../escape",
        "src/../docs/readme.md",  # traversal that lands back inside is still refused
        "%2e%2e/escape",
        "..%2fescape",
    ],
)
def test_traversal_is_refused_by_the_read_half(worktree: Path, bad: str) -> None:
    with pytest.raises(WorkspaceSecurityError):
        resolve_write_target(str(worktree), bad)


@pytest.mark.parametrize("bad", ["/etc/passwd", "/tmp/evil", "C:/windows/system32"])
def test_absolute_paths_are_refused_by_the_read_half(worktree: Path, bad: str) -> None:
    with pytest.raises(WorkspaceSecurityError):
        resolve_write_target(str(worktree), bad)


# --------------------------------------------------------------------------
# symlink rejection -- STRICTER than the read half
# --------------------------------------------------------------------------


def test_symlinked_directory_inside_worktree_is_refused(worktree: Path) -> None:
    """The deliberate difference from the read half: in-worktree links are refused."""
    (worktree / "inside_link").symlink_to(worktree / "docs", target_is_directory=True)

    # Sanity: the READ half allows this hop (it never leaves the worktree)...
    assert resolve_within_workspace(
        str(worktree), "inside_link/readme.md"
    ) == worktree / "docs" / "readme.md"

    # ...the WRITE half must not.
    with pytest.raises(PatchSecurityError) as excinfo:
        resolve_write_target(str(worktree), "inside_link/readme.md")
    assert str(excinfo.value)


def test_symlinked_directory_inside_worktree_refused_for_nonexistent_target(
    worktree: Path,
) -> None:
    """An EXISTING symlinked component is refused even when the target is new."""
    (worktree / "inside_link").symlink_to(worktree / "docs", target_is_directory=True)

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), "inside_link/brand_new.md")


def test_symlinked_directory_outside_worktree_is_refused(
    worktree: Path, tmp_path: Path
) -> None:
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (worktree / "escape_link").symlink_to(outside, target_is_directory=True)

    # Either half may fire first here (the read half's containment check also
    # catches this); what matters is that the path is refused.
    with pytest.raises((PatchSecurityError, WorkspaceSecurityError)):
        resolve_write_target(str(worktree), "escape_link/secret.txt")


def test_existing_symlink_file_as_target_is_refused(worktree: Path) -> None:
    (worktree / "link.py").symlink_to(worktree / "src" / "app.py")

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), "link.py")


def test_symlink_file_pointing_outside_is_refused(
    worktree: Path, tmp_path: Path
) -> None:
    outside_file = (tmp_path / "outside.txt").resolve()
    outside_file.write_text("x")
    (worktree / "out_link.txt").symlink_to(outside_file)

    with pytest.raises((PatchSecurityError, WorkspaceSecurityError)):
        resolve_write_target(str(worktree), "out_link.txt")


# --------------------------------------------------------------------------
# deny list is checked FIRST, before any disk access
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "denied",
    [".git/config", "CLAUDE.md", ".claude/settings.json", "src/.git/config", ".mcp.json"],
)
def test_denied_paths_are_refused_before_any_disk_access(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, denied: str
) -> None:
    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError(f"filesystem access attempted for denied path {denied!r}")

    monkeypatch.setattr(os.path, "exists", _boom)
    monkeypatch.setattr(os.path, "islink", _boom)

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), denied)


def test_deny_check_precedes_the_read_half(worktree: Path) -> None:
    """A path that is BOTH deny-listed and traversing fails the deny check first."""
    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), ".git/../escape")


@pytest.mark.parametrize("degenerate", ["", "/", None, 123])
def test_degenerate_paths_fail_closed(worktree: Path, degenerate: object) -> None:
    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), degenerate)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# containment re-check against the pipeline's own REPO_ROOT / PLAN_DIR
# --------------------------------------------------------------------------


def test_target_inside_pipeline_repo_root_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = (tmp_path / "fake_repo").resolve()
    (repo_root / "pipeline").mkdir(parents=True)
    (repo_root / "pipeline" / "paths.py").write_text("x = 1\n")
    _patch_pipeline_constants(monkeypatch, repo_root=repo_root)

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(repo_root), "pipeline/paths.py")


def test_configured_repo_root_as_worktree_root_is_refused() -> None:
    """The pipeline's own checkout is never a legal patch target.

    Uses whatever ``REPO_ROOT`` the server is configured with (no skip): the
    resolver must refuse a target that lands inside it.
    """
    from pipeline.server import REPO_ROOT

    repo_root = Path(REPO_ROOT)
    assert repo_root.is_absolute()

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(repo_root), "pipeline/paths.py")


def test_target_inside_plan_dir_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A crafted worktree_root must not smuggle a write into PLAN_DIR."""
    plan_dir = (tmp_path / "plans").resolve()
    plan_dir.mkdir()
    (plan_dir / "demo.manifest.json").write_text("{}")
    _patch_pipeline_constants(monkeypatch, plan_dir=plan_dir)

    assert plan_dir.is_relative_to(tmp_path.resolve())  # hermetic: never ~/.claude/plans

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(plan_dir), "demo.manifest.json")


def test_target_inside_nested_plan_dir_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan_dir = (tmp_path / "plans").resolve()
    nested = plan_dir / "some_plan"
    nested.mkdir(parents=True)
    (nested / "story.manifest.json").write_text("{}")
    _patch_pipeline_constants(monkeypatch, plan_dir=plan_dir)

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(nested), "story.manifest.json")


# --------------------------------------------------------------------------
# fail closed on unexpected OS errors
# --------------------------------------------------------------------------


def test_oserror_during_symlink_walk_fails_closed(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
) -> None:
    """An unexpected OS error must never yield a returned path.

    ``os.path.islink`` is the primitive BOTH halves use (the read half's
    defense-in-depth walk calls it too), so either half may be the one that
    fires; the graded invariant is that no path is returned.
    """

    def _boom(*args: object, **kwargs: object) -> bool:
        raise OSError("simulated lstat failure")

    monkeypatch.setattr(os.path, "islink", _boom)

    with pytest.raises((PatchSecurityError, WorkspaceSecurityError)):
        resolve_write_target(str(worktree), "src/app.py")


def test_oserror_during_existence_check_fails_closed(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
) -> None:
    """The write half's own walk must convert OSError into PatchSecurityError.

    ``os.path.exists`` is the write half's existence primitive -- the read half
    uses ``os.path.lexists``/``os.path.realpath`` and never calls ``exists`` --
    so this isolates the write half's walk.  An unhandled OSError there would
    escape as a bare OSError (or, worse, a returned path) instead of a denial.
    """

    def _boom(*args: object, **kwargs: object) -> bool:
        raise OSError("simulated stat failure")

    monkeypatch.setattr(os.path, "exists", _boom)

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), "src/app.py")
