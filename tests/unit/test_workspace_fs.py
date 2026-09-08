"""Tests for the workspace-scoped path resolution guard.

Threat model: a caller (e.g. a future file-read / directory-listing surface)
holds an already-validated workspace root and receives an UNTRUSTED relative
path from a user. ``resolve_within_workspace`` is the control that guarantees
the resulting path cannot escape that workspace root.

Contract pinned here:

1. Valid relative paths inside the workspace resolve to the expected absolute
   path (positive control — the guard must not over-block).
2. ``..`` traversal is rejected, in raw and percent-encoded spellings, before
   any filesystem access.
3. Absolute paths are rejected even when they contain no ``..``.
4. Non-string / empty input is rejected (deny by default).
5. A symlink inside the workspace that points OUTSIDE it is rejected when the
   relative path traverses it; a symlink pointing back INSIDE is allowed.
6. The workspace root itself (``"."``) is addressable, since a directory
   listing at the root needs it.
7. Every rejection raises ``WorkspaceSecurityError`` — the SAME exception
   class ``pipeline/workspace.py`` already raises, not a competing one — and
   the message never contains a resolved filesystem path.
"""

import os
import sys

import pytest

from pipeline import workspace_fs
from pipeline.workspace import WorkspaceSecurityError
from pipeline.workspace_fs import resolve_within_workspace

_WINDOWS = sys.platform.startswith("win")


@pytest.fixture
def workspace(tmp_path):
    """A realpath'd workspace root, so assertions are symlink-stable.

    ``tmp_path`` on macOS lives under ``/var`` -> ``/private/var``; the guard
    resolves symlinks, so the expected values must be resolved too.
    """
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


# ---------------------------------------------------------------------------
# 1. Positive control — valid paths resolve
# ---------------------------------------------------------------------------


class TestValidPathsResolve:
    def test_simple_relative_path_resolves(self, workspace):
        assert resolve_within_workspace(str(workspace), "file.txt") == (
            workspace / "file.txt"
        )

    def test_nested_multi_segment_path_resolves(self, workspace):
        assert resolve_within_workspace(str(workspace), "nested/valid/path.txt") == (
            workspace / "nested" / "valid" / "path.txt"
        )

    def test_existing_file_resolves(self, workspace):
        target = workspace / "real.txt"
        target.write_text("hello")
        assert resolve_within_workspace(str(workspace), "real.txt") == target

    def test_returns_absolute_path(self, workspace):
        assert resolve_within_workspace(str(workspace), "a/b.txt").is_absolute()

    def test_percent_encoding_is_joined_raw_not_decoded(self, workspace):
        # Decoding is detection only (docstring step 2): the RAW spelling is
        # joined, so "a%20b.txt" is a literal filename, not "a b.txt".
        assert resolve_within_workspace(str(workspace), "a%20b.txt") == (
            workspace / "a%20b.txt"
        )


# ---------------------------------------------------------------------------
# 2. Traversal rejection
# ---------------------------------------------------------------------------


class TestTraversalRejected:
    def test_single_parent_traversal_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "../escape.txt")

    def test_double_parent_traversal_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "../../etc/passwd")

    def test_traversal_in_middle_of_path_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "nested/../../escape.txt")

    def test_percent_encoded_traversal_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "%2e%2e/escape.txt")

    def test_double_percent_encoded_traversal_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "%252e%252e/escape.txt")

    def test_percent_encoded_separator_traversal_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "..%2fescape.txt")

    def test_traversal_that_returns_inside_is_still_rejected(self, workspace):
        # Structurally rejected before resolution: '..' is never acceptable
        # input, even in a spelling that would have landed back inside.
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "nested/../file.txt")

    def test_rejection_message_leaks_no_resolved_path(self, workspace):
        with pytest.raises(WorkspaceSecurityError) as excinfo:
            resolve_within_workspace(str(workspace), "../../etc/passwd")
        assert str(workspace) not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. Absolute paths rejected (no '..' required)
# ---------------------------------------------------------------------------


class TestAbsolutePathsRejected:
    def test_absolute_posix_path_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "/etc/passwd")

    def test_absolute_path_inside_workspace_still_rejected(self, workspace):
        # Even an absolute path that happens to be inside the workspace is
        # rejected: the parameter's contract is "relative", and accepting
        # absolutes hands the caller control of the root.
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), str(workspace / "file.txt"))

    def test_percent_encoded_absolute_path_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "%2fetc%2fpasswd")


# ---------------------------------------------------------------------------
# 4. Shape / deny-by-default
# ---------------------------------------------------------------------------


class TestInvalidShapeRejected:
    def test_empty_string_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "")

    def test_none_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), None)

    @pytest.mark.parametrize("bad", [123, 4.5, [], {}, object()])
    def test_non_string_rejected(self, workspace, bad):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), bad)

    def test_nul_byte_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "file\x00.txt")

    def test_control_character_rejected(self, workspace):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "file\n.txt")

    def test_empty_workspace_root_rejected(self):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace("", "file.txt")

    def test_none_workspace_root_rejected(self):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(None, "file.txt")

    def test_error_is_a_value_error_subclass(self, workspace):
        # WorkspaceSecurityError subclasses ValueError; callers that already
        # catch ValueError keep working.
        with pytest.raises(ValueError):
            resolve_within_workspace(str(workspace), "../escape.txt")


# ---------------------------------------------------------------------------
# 5. Symlink escapes
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_WINDOWS, reason="symlink creation is not reliable on Windows")
class TestSymlinkEscapes:
    def test_traversing_symlink_pointing_outside_is_rejected(self, tmp_path):
        workspace = (tmp_path / "ws").resolve()
        workspace.mkdir()
        outside = (tmp_path / "outside").resolve()
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        os.symlink(outside, workspace / "shim")

        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "shim/secret.txt")

    def test_symlink_final_component_pointing_outside_is_rejected(self, tmp_path):
        workspace = (tmp_path / "ws").resolve()
        workspace.mkdir()
        outside = (tmp_path / "outside").resolve()
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("secret")
        os.symlink(secret, workspace / "link.txt")

        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "link.txt")

    def test_symlink_pointing_inside_workspace_is_allowed(self, tmp_path):
        # Positive control: the guard blocks ESCAPES, not symlinks per se.
        workspace = (tmp_path / "ws").resolve()
        workspace.mkdir()
        real_dir = workspace / "real"
        real_dir.mkdir()
        (real_dir / "ok.txt").write_text("ok")
        os.symlink(real_dir, workspace / "alias")

        assert resolve_within_workspace(str(workspace), "alias/ok.txt") == (
            real_dir / "ok.txt"
        )

    def test_dangling_symlink_pointing_outside_is_rejected(self, tmp_path):
        workspace = (tmp_path / "ws").resolve()
        workspace.mkdir()
        os.symlink(tmp_path / "outside-nonexistent", workspace / "shim")

        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "shim/file.txt")

    def test_leave_and_reenter_chain_is_rejected(self, tmp_path):
        # The case the step-4 component walk catches that step-3 containment
        # alone misses: link -> OUTSIDE, and a second link OUTSIDE -> back
        # INSIDE the workspace. The final .resolve() lands inside the root,
        # so containment passes; the walk rejects the intermediate escape.
        # Without this test the walk could be deleted and the suite stays
        # green (verified by mutation: deleting the walk left every other
        # test passing).
        workspace = (tmp_path / "ws").resolve()
        workspace.mkdir()
        outside = (tmp_path / "outside").resolve()
        outside.mkdir()
        (workspace / "real.txt").write_text("ok")
        os.symlink(outside, workspace / "link")          # ws/link -> outside
        os.symlink(workspace / "real.txt", outside / "link2")  # outside/link2 -> ws/real.txt

        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "link/link2")

    def test_purely_inside_two_link_chain_is_allowed(self, tmp_path):
        # Positive control for the leave-and-reenter test: two hops that BOTH
        # stay inside the workspace resolve fine.
        workspace = (tmp_path / "ws").resolve()
        workspace.mkdir()
        (workspace / "real.txt").write_text("ok")
        (workspace / "inner").mkdir()
        os.symlink(workspace / "real.txt", workspace / "inner" / "link2")
        os.symlink(workspace / "inner", workspace / "link")

        assert resolve_within_workspace(str(workspace), "link/link2") == (
            workspace / "real.txt"
        )


# ---------------------------------------------------------------------------
# 6. The workspace root itself is addressable
# ---------------------------------------------------------------------------


class TestWorkspaceRootItself:
    def test_dot_resolves_to_workspace_root(self, workspace):
        # A future directory-listing caller needs to address the root.
        assert resolve_within_workspace(str(workspace), ".") == workspace

    def test_dot_slash_prefix_resolves_inside(self, workspace):
        assert resolve_within_workspace(str(workspace), "./file.txt") == (
            workspace / "file.txt"
        )


# ---------------------------------------------------------------------------
# 7. Fail closed
# ---------------------------------------------------------------------------


class TestDefenseInDepth:
    """Each layer must deny on its own when the layer above it is bypassed.

    Without these, the structural checks shadow the containment check in
    every other test, so nothing would prove the containment layer is live
    (verified by mutation: disabling it left the rest of the suite green).
    """

    @pytest.fixture
    def structural_checks_bypassed(self, monkeypatch):
        monkeypatch.setattr(
            workspace_fs, "_reject_unsafe_relative", lambda text: None
        )

    def test_containment_denies_traversal_when_structural_check_bypassed(
        self, workspace, structural_checks_bypassed
    ):
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "../escape.txt")

    def test_containment_denies_absolute_when_structural_check_bypassed(
        self, workspace, structural_checks_bypassed
    ):
        # pathlib's join lets an absolute right-hand side replace the root
        # entirely, so containment is what stops it once structure doesn't.
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "/etc/passwd")


class TestFailsClosed:
    def test_unexpected_oserror_denies_rather_than_returning(
        self, workspace, monkeypatch
    ):
        # An unexpected filesystem error must never fall through to a
        # returned path — the guard denies instead.
        def _boom(*args, **kwargs):
            raise OSError("filesystem exploded")

        monkeypatch.setattr(os.path, "realpath", _boom)
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "file.txt")

    def test_unexpected_islink_error_denies(self, workspace, monkeypatch):
        def _boom(*args, **kwargs):
            raise OSError("cannot stat")

        monkeypatch.setattr(os.path, "islink", _boom)
        with pytest.raises(WorkspaceSecurityError):
            resolve_within_workspace(str(workspace), "nested/file.txt")
