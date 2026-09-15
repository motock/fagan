"""Unit tests for :mod:`pipeline.worktree_patch` (WAP-4).

``pipeline.worktree_patch`` is the APPLY-side, write-mode path resolver for
server-side patch records against a stuck story's worktree.  These tests pin
its security contract:

* the deny list is pure and is evaluated before any disk access;
* the read-half resolver
  (:func:`pipeline.workspace_fs.resolve_within_workspace`) still owns
  traversal / absolute-path rejection, and its
  :class:`~pipeline.workspace.WorkspaceSecurityError` propagates unchanged;
* writes are STRICTER than reads: every symlink component -- pointing inside
  the worktree or outside it -- is refused, and so is a symlinked final
  target;
* containment is re-checked against the pipeline repo's own ``REPO_ROOT`` and
  ``PLAN_DIR``;
* the resolver fails closed on any unexpected ``OSError``.

Written before the implementation exists (TDD): every test in this file is
expected to fail with an ``ImportError`` until ``pipeline/worktree_patch.py``
lands.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.workspace import WorkspaceSecurityError
from pipeline.workspace_fs import resolve_within_workspace
from pipeline.worktree_patch import (
    PatchSecurityError,
    is_denied_relative_path,
    resolve_write_target,
)

# Targets the deny list must refuse, exactly as enumerated by the story.
DENIED_TARGETS = [
    ".git/config",
    ".git/hooks/pre-commit",
    ".claude/settings.json",
    "CLAUDE.md",
    "src/CLAUDE.md",
    ".mcp.json",
    "agent.log",
    ".agent_log.20260914",
    ".agent_transcript.json",
    ".agent_plan.md",
    ".agent_scratchpad.md",
]


# --------------------------------------------------------------------------
# helpers / fixtures
# --------------------------------------------------------------------------


def _patch_path_constants(monkeypatch, *, repo_root: Path, plan_dir: Path) -> None:
    """Point every plausible binding of REPO_ROOT / PLAN_DIR at temp dirs.

    ``pipeline.worktree_patch`` is expected to follow the house pattern of a
    lazy ``from .server import PLAN_DIR, REPO_ROOT`` inside the function body
    (see ``pipeline/checkpoint.py`` and ``pipeline/escalation.py``), so the
    authoritative bindings live on ``pipeline.server``.  The other two
    modules are patched too (``raising=False``) so the test stays hermetic
    whichever import spelling the implementation picks.
    """
    import pipeline.paths as paths_mod
    import pipeline.server as server_mod
    import pipeline.worktree_patch as wp_mod

    for mod in (server_mod, paths_mod, wp_mod):
        monkeypatch.setattr(mod, "REPO_ROOT", repo_root, raising=False)
        monkeypatch.setattr(mod, "PLAN_DIR", plan_dir, raising=False)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A real temp tree: a worktree, a fake pipeline repo and a fake PLAN_DIR.

    ``tmp_path`` is realpath'd first: on macOS the pytest temp root can itself
    sit behind a symlinked component (``/var`` -> ``/private/var``), and the
    write resolver deliberately refuses a symlinked worktree root.
    """
    base = Path(os.path.realpath(str(tmp_path)))
    repo_root = base / "pipeline_repo"
    plan_dir = base / "plans"
    worktree = base / "wt"
    for directory in (repo_root, plan_dir, worktree):
        directory.mkdir(parents=True, exist_ok=True)

    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("print('hi')\n")
    (worktree / "pkg").mkdir()
    (worktree / "pkg" / "mod.py").write_text("x = 1\n")
    (worktree / "README.md").write_text("readme\n")

    _patch_path_constants(monkeypatch, repo_root=repo_root, plan_dir=plan_dir)
    return SimpleNamespace(
        base=base,
        repo_root=repo_root,
        plan_dir=plan_dir,
        worktree=worktree,
    )


def _message_of(call) -> str:
    with pytest.raises(PatchSecurityError) as excinfo:
        call()
    return str(excinfo.value)


def _exploding_islink(real_islink, targets):
    """An ``os.path.islink`` replacement that raises OSError for *targets*."""
    target_reals = {os.path.realpath(str(t)) for t in targets}

    def _islink(path):
        if os.path.realpath(str(path)) in target_reals:
            raise OSError("simulated islink failure")
        return real_islink(path)

    return _islink


# --------------------------------------------------------------------------
# module surface
# --------------------------------------------------------------------------


def test_module_docstring_states_the_security_invariants():
    import pipeline.worktree_patch as wp_mod

    doc = (wp_mod.__doc__ or "").lower()
    assert doc.strip(), "pipeline.worktree_patch must carry a module docstring"
    assert "deny-by-default" in doc
    assert "human-confirmed" in doc
    assert "worktree" in doc


def test_patch_security_error_is_a_value_error():
    assert issubclass(PatchSecurityError, ValueError)


def test_resolve_write_target_signature_names_the_worktree_root():
    params = list(inspect.signature(resolve_write_target).parameters)
    assert params == ["worktree_root", "relative_path"]


def test_is_denied_relative_path_signature():
    params = list(inspect.signature(is_denied_relative_path).parameters)
    assert params == ["relative_path"]


# --------------------------------------------------------------------------
# deny list (pure)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("relative_path", DENIED_TARGETS)
def test_deny_list_refuses_each_listed_target(relative_path):
    assert is_denied_relative_path(relative_path) is True


@pytest.mark.parametrize(
    "relative_path",
    [
        ".git",
        ".claude",
        "src/.git/config",
        "a/b/.claude/settings.json",
        ".agent_log",
        ".agent_log.20260914",
        "nested/.agent_log.1",
    ],
)
def test_deny_list_refuses_any_denied_component(relative_path):
    assert is_denied_relative_path(relative_path) is True


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/app.py",
        "README.md",
        "docs/notes.md",
        ".gitignore",
        "src/.gitignore",
        "agent.logs",
        "claude.md",
    ],
)
def test_deny_list_allows_ordinary_paths(relative_path):
    assert is_denied_relative_path(relative_path) is False


@pytest.mark.parametrize("relative_path", ["", "."])
def test_deny_list_boundary_spellings_are_not_denied(relative_path):
    assert is_denied_relative_path(relative_path) is False


def test_deny_list_matches_only_the_final_component_for_artifact_names():
    # ".git"/".claude" are denied as ANY component; the artifact names are
    # denied only as the FINAL component.
    assert is_denied_relative_path("logs/agent.log") is True
    assert is_denied_relative_path("agent.log/sub.txt") is False


def test_deny_list_returns_a_real_bool():
    assert isinstance(is_denied_relative_path("src/app.py"), bool)
    assert isinstance(is_denied_relative_path(".git/config"), bool)


def test_deny_list_is_pure_and_never_touches_the_disk(monkeypatch):
    def exploding_islink(path):  # pragma: no cover - must never be called
        raise OSError("is_denied_relative_path must not touch the disk")

    monkeypatch.setattr(os.path, "islink", exploding_islink)
    assert is_denied_relative_path("src/app.py") is False
    assert is_denied_relative_path(".git/config") is True


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------


def test_existing_clean_path_resolves_under_the_worktree(sandbox):
    result = resolve_write_target(str(sandbox.worktree), "src/app.py")
    assert isinstance(result, Path)
    assert result == sandbox.worktree / "src" / "app.py"
    assert result.is_relative_to(sandbox.worktree)


def test_nonexistent_deep_path_with_clean_parents_is_allowed(sandbox):
    result = resolve_write_target(str(sandbox.worktree), "a/b/c/new.txt")
    assert result == sandbox.worktree / "a" / "b" / "c" / "new.txt"
    assert result.is_relative_to(sandbox.worktree)


def test_root_spelling_resolves_to_the_worktree_root(sandbox):
    result = resolve_write_target(str(sandbox.worktree), ".")
    assert result == sandbox.worktree


# --------------------------------------------------------------------------
# read-half rejections propagate unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative_path",
    ["../escape", "src/../../escape", "%2e%2e/escape", "/etc/passwd"],
)
def test_traversal_and_absolute_paths_are_refused_by_the_read_half(
    sandbox, relative_path
):
    with pytest.raises(WorkspaceSecurityError):
        resolve_write_target(str(sandbox.worktree), relative_path)


# --------------------------------------------------------------------------
# symlinks: writes are stricter than reads
# --------------------------------------------------------------------------


def test_symlinked_directory_inside_the_worktree_is_refused(sandbox):
    worktree = sandbox.worktree
    (worktree / "linkdir").symlink_to(worktree / "pkg", target_is_directory=True)

    # Sanity: the READ half deliberately allows an in-worktree symlink ...
    assert resolve_within_workspace(str(worktree), "linkdir/mod.py") == (
        worktree / "pkg" / "mod.py"
    )
    # ... the WRITE half must not carry that rule over.
    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), "linkdir/mod.py")


def test_symlinked_directory_outside_the_worktree_is_refused(sandbox):
    worktree = sandbox.worktree
    outside = sandbox.base / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_text("nope\n")
    (worktree / "outdir").symlink_to(outside, target_is_directory=True)

    with pytest.raises((WorkspaceSecurityError, PatchSecurityError)):
        resolve_write_target(str(worktree), "outdir/payload.txt")


def test_existing_symlink_file_as_the_target_is_refused(sandbox):
    worktree = sandbox.worktree
    (worktree / "linkfile").symlink_to(worktree / "README.md")

    # The read half resolves it happily (the link stays inside the worktree).
    assert resolve_within_workspace(str(worktree), "linkfile") == (
        worktree / "README.md"
    )
    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), "linkfile")


def test_symlink_file_pointing_outside_the_worktree_is_refused(sandbox):
    worktree = sandbox.worktree
    outside_file = sandbox.base / "outside.txt"
    outside_file.write_text("nope\n")
    (worktree / "outfile").symlink_to(outside_file)

    with pytest.raises((WorkspaceSecurityError, PatchSecurityError)):
        resolve_write_target(str(worktree), "outfile")


def test_symlink_deep_in_the_path_is_refused(sandbox):
    worktree = sandbox.worktree
    (worktree / "src" / "inner").symlink_to(worktree / "pkg", target_is_directory=True)

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), "src/inner/new.py")


# --------------------------------------------------------------------------
# deny list is enforced by the resolver, before any disk access
# --------------------------------------------------------------------------


@pytest.mark.parametrize("relative_path", DENIED_TARGETS)
def test_resolver_refuses_every_deny_listed_target(sandbox, relative_path):
    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(sandbox.worktree), relative_path)


def test_deny_list_is_checked_before_any_disk_access(sandbox, monkeypatch):
    worktree = sandbox.worktree
    baseline = _message_of(
        lambda: resolve_write_target(str(worktree), ".git/config")
    )

    def exploding_islink(path):  # pragma: no cover - must never be called
        raise OSError("the deny list must be evaluated before touching the disk")

    monkeypatch.setattr(os.path, "islink", exploding_islink)
    with pytest.raises(PatchSecurityError) as excinfo:
        resolve_write_target(str(worktree), ".git/config")
    # Same rule, same fixed message: the denial came from the deny list and
    # not from the fail-closed OSError handler.
    assert str(excinfo.value) == baseline


# --------------------------------------------------------------------------
# containment re-check against the pipeline repo's own roots
# --------------------------------------------------------------------------


def test_path_resolving_inside_the_pipeline_repo_root_is_refused(sandbox):
    inside_repo = sandbox.repo_root / "wt"
    inside_repo.mkdir()
    (inside_repo / "src").mkdir()
    (inside_repo / "src" / "app.py").write_text("x\n")

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(inside_repo), "src/app.py")


def test_path_resolving_inside_plan_dir_is_refused(sandbox):
    inside_plans = sandbox.plan_dir / "wt"
    inside_plans.mkdir()
    (inside_plans / "src").mkdir()
    (inside_plans / "src" / "app.py").write_text("x\n")

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(inside_plans), "src/app.py")


def test_worktree_outside_repo_root_and_plan_dir_is_allowed(sandbox):
    # Guard against an over-broad containment check: the sandbox worktree is a
    # sibling of both protected roots and must stay writable.
    assert not sandbox.worktree.is_relative_to(sandbox.repo_root)
    assert not sandbox.worktree.is_relative_to(sandbox.plan_dir)
    assert resolve_write_target(str(sandbox.worktree), "src/app.py") == (
        sandbox.worktree / "src" / "app.py"
    )


# --------------------------------------------------------------------------
# fail closed
# --------------------------------------------------------------------------


def test_fail_closed_when_islink_raises_oserror(sandbox, monkeypatch):
    worktree = sandbox.worktree
    real_islink = os.path.islink
    # The worktree root itself and the (nonexistent) final target are the two
    # components the read half never probes, so an OSError there can only come
    # from the write half's own walk.
    monkeypatch.setattr(
        os.path,
        "islink",
        _exploding_islink(
            real_islink, [worktree, worktree / "src" / "sub" / "new_file.py"]
        ),
    )

    with pytest.raises(PatchSecurityError):
        resolve_write_target(str(worktree), "src/sub/new_file.py")


def test_fail_closed_never_returns_an_unverified_path(sandbox, monkeypatch):
    def exploding_islink(path):
        raise OSError("simulated islink failure")

    monkeypatch.setattr(os.path, "islink", exploding_islink)
    with pytest.raises((PatchSecurityError, WorkspaceSecurityError)):
        resolve_write_target(str(sandbox.worktree), "src/new_file.py")


# --------------------------------------------------------------------------
# fixed message per rule
# --------------------------------------------------------------------------


def test_patch_security_error_carries_a_fixed_message_per_rule(sandbox):
    worktree = sandbox.worktree
    (worktree / "linkdir").symlink_to(worktree / "pkg", target_is_directory=True)

    deny_msg = _message_of(
        lambda: resolve_write_target(str(worktree), ".git/config")
    )
    symlink_msg = _message_of(
        lambda: resolve_write_target(str(worktree), "linkdir/mod.py")
    )

    inside_repo = sandbox.repo_root / "wt"
    inside_repo.mkdir()
    containment_msg = _message_of(
        lambda: resolve_write_target(str(inside_repo), "src/app.py")
    )

    for message in (deny_msg, symlink_msg, containment_msg):
        assert message.strip(), "each rule must raise a non-empty message"

    # Distinct rules must be distinguishable by their message ...
    assert len({deny_msg, symlink_msg, containment_msg}) == 3
    # ... and the message for a rule must be stable across calls.
    assert (
        _message_of(lambda: resolve_write_target(str(worktree), ".git/config"))
        == deny_msg
    )
    assert (
        _message_of(lambda: resolve_write_target(str(worktree), "linkdir/mod.py"))
        == symlink_msg
    )
