"""Regression guards for the CodeQL-driven hardening of path containment and
URL redaction (story cqa-01).

Contract pinned here:

1. ``resolve_within_workspace`` keeps refusing every escape it refused before
   the containment check was rewritten on strings: ``..`` traversal, absolute
   paths, and symlinks that leave the root.
2. A sibling directory that shares the root's name as a prefix
   (``/x/repo`` vs ``/x/repo-evil``) is refused, not treated as inside.
3. The workspace root itself (``"."``) stays addressable.
4. ``_redact`` removes credentials from ``https://`` URLs, including userinfo
   longer than 256 characters, and runs in linear time on hostile input.
"""

import os
import time

import pytest

from pipeline import repo_clone
from pipeline.workspace_fs import WorkspaceSecurityError, resolve_within_workspace


def test_should_still_refuse_dot_dot_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    with pytest.raises(WorkspaceSecurityError):
        resolve_within_workspace(str(root), "../secret.txt")


def test_should_still_refuse_an_absolute_path(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    with pytest.raises(WorkspaceSecurityError):
        resolve_within_workspace(str(root), str(tmp_path / "secret.txt"))


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_should_still_refuse_a_symlink_that_leaves_the_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceSecurityError):
        resolve_within_workspace(str(root), "link/file.txt")


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_should_refuse_a_sibling_directory_sharing_the_root_prefix(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sibling = tmp_path / "repo-evil"
    sibling.mkdir()
    (root / "to_sibling").symlink_to(sibling, target_is_directory=True)

    with pytest.raises(WorkspaceSecurityError):
        resolve_within_workspace(str(root), "to_sibling/f")


def test_should_allow_the_root_itself_with_dot(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    resolved = resolve_within_workspace(str(root), ".")

    assert resolved == root.resolve()


def test_should_redact_credentials_in_an_https_url():
    line = "fatal: https://user:hunter2@github.com/o/r unreachable"

    redacted = repo_clone._redact(line)

    assert "hunter2" not in redacted
    assert redacted == "fatal: https://***@github.com/o/r unreachable"


def test_should_redact_long_userinfo_beyond_256_chars():
    secret = "s" * 300
    line = f"https://{secret}@github.com/o/r"

    redacted = repo_clone._redact(line)

    assert secret not in redacted
    assert redacted == "https://***@github.com/o/r"


def test_should_redact_a_hostile_input_in_linear_time():
    hostile = "https://" * 200_000

    started = time.perf_counter()
    redacted = repo_clone._redact(hostile)
    elapsed = time.perf_counter() - started

    assert redacted == hostile
    assert elapsed < 1.0
