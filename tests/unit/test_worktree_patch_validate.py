"""Tests for the propose-side unified-diff parser and validator (WAP-5).

Target API (propose-side only), added to ``pipeline/worktree_patch.py``::

    pipeline.worktree_patch.PatchFormatError(ValueError)
    pipeline.worktree_patch.parse_unified_diff(diff_text) -> parsed structure
    pipeline.worktree_patch.validate_for_propose(diff_text, worktree_root) -> dict

``validate_for_propose`` is the PROPOSE half of the patch pipeline: it decides
whether a model-authored diff may even be shown to a human for confirmation.
It is deliberately weaker than the apply half -- the deny list
(:func:`pipeline.worktree_patch.is_denied_relative_path`) and the strict write
resolver (:func:`pipeline.worktree_patch.resolve_write_target`) are APPLY-side
only, so a ``.git``-touching patch is accepted here (the human gets to inspect
it) and refused later at apply time.

Hermeticity: every worktree fixture is built under pytest's ``tmp_path`` and
resolved first, because on macOS the raw temp path can run through
``/var -> /private/var`` and a symlinked root component would make the read-half
resolver reject every path for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.workspace import WorkspaceSecurityError
from pipeline.worktree_patch import (
    PatchFormatError,
    parse_unified_diff,
    validate_for_propose,
)

# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

MAX_BYTES = 65536
MAX_FILES = 5
MAX_ADDED_LINES = 400


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A small, symlink-free worktree tree."""
    root = (tmp_path / "worktree").resolve()
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "docs").mkdir()
    (root / "docs" / "readme.md").write_text("hi\n")
    return root


def _hunk(old_start: int, old_count: int, new_start: int, new_count: int) -> str:
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n"


def _file_block(path: str, body: str, *, old_path: str | None = None) -> str:
    """A one-hunk file block for *path*; *old_path* defaults to ``a/<path>``."""
    old = old_path if old_path is not None else f"a/{path}"
    return f"--- {old}\n+++ b/{path}\n{body}"


def _new_file_block(path: str, added: int) -> str:
    """A pure-addition new-file block with *added* ``+`` lines."""
    body = _hunk(0, 0, 1, added) + "".join(f"+line{i}\n" for i in range(added))
    return _file_block(path, body, old_path="/dev/null")


def _context_only_block(path: str, context: int) -> str:
    """A block whose single hunk is *context* context lines and nothing else."""
    body = _hunk(1, context, 1, context) + " ctx\n" * context
    return _file_block(path, body)


def _padded_block(target_bytes: int) -> tuple[str, str]:
    """Return ``(path, block)`` where the block is EXACTLY *target_bytes* long.

    The block is a single hunk of ``n`` context lines plus one deletion and one
    addition, so it is a well-formed, accepted diff (context lines present, so
    the whole-file-replacement rule does not fire) whose only interesting
    property is its exact byte size.
    """
    for n in range(1, 40000):
        old_count = n + 1
        header = _hunk(1, old_count, 1, old_count)
        body = " ctx\n" * n + "-old\n+new\n"
        for extra in range(80):
            name = "pad" + "x" * extra + ".py"
            block = _file_block(name, header + body)
            if len(block.encode("utf-8")) == target_bytes:
                return name, block
    raise AssertionError(f"no block of exactly {target_bytes} bytes")  # pragma: no cover


def _get(obj, name: str):
    """Read *name* from a parsed structure that may be a dict OR a dataclass.

    The task allows ``parse_unified_diff`` to return "a small dataclass or
    dict", so the tests accept either shape.  The FIELD NAMES are pinned by
    this suite: top level ``paths`` / ``added_lines`` / ``hunks``; per hunk
    ``path`` / ``context_lines`` / ``deletions`` / ``additions``.
    """
    if isinstance(obj, dict):
        return obj[name]
    return getattr(obj, name)


def _assert_refused(diff_text: str, worktree: Path, *needles: str) -> PatchFormatError:
    """Assert ``validate_for_propose`` refuses *diff_text* with *needles*."""
    with pytest.raises(PatchFormatError) as excinfo:
        validate_for_propose(diff_text, str(worktree))
    message = str(excinfo.value)
    for needle in needles:
        assert needle in message, f"{needle!r} not in refusal message {message!r}"
    return excinfo.value


# --------------------------------------------------------------------------
# fixtures: literal diffs
# --------------------------------------------------------------------------

# Two files, three hunks, 4 added lines in total (count the '+' lines by hand:
# src/app.py hunk 1 adds 2, src/app.py hunk 2 adds 1, docs/readme.md adds 1).
VALID_MULTI_HUNK_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "-x = 1\n"
    "+x = 2\n"
    "+y = 3\n"
    " print(x)\n"
    "@@ -4,2 +5,3 @@\n"
    " def main():\n"
    "+    return 0\n"
    "     pass\n"
    "--- a/docs/readme.md\n"
    "+++ b/docs/readme.md\n"
    "@@ -1,2 +1,3 @@\n"
    " # Title\n"
    "+new paragraph\n"
    " body\n"
)

# A brand-new file: no old content to anchor, so the whole-file-replacement
# rule must NOT fire.  3 added lines.
NEW_FILE_DIFF = (
    "--- /dev/null\n"
    "+++ b/new_file.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+import os\n"
    "+import sys\n"
    "+print(os, sys)\n"
)

# A hunk WITH context lines that both deletes and adds: accepted.
CONTEXT_ANCHORED_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,4 +1,4 @@\n"
    " import os\n"
    "-x = 1\n"
    "+x = 2\n"
    " print(x)\n"
    " done()\n"
)

# The no-anchor delete-all-add-all payload: one hunk, zero context lines,
# 3 deletions and 3 additions against an EXISTING file.
WHOLE_FILE_REPLACEMENT_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-x = 1\n"
    "-y = 2\n"
    "-z = 3\n"
    "+a = 1\n"
    "+b = 2\n"
    "+c = 3\n"
)


# --------------------------------------------------------------------------
# parse_unified_diff: happy path
# --------------------------------------------------------------------------


def test_parse_multi_hunk_diff_paths_and_added_lines():
    parsed = parse_unified_diff(VALID_MULTI_HUNK_DIFF)

    assert list(_get(parsed, "paths")) == ["src/app.py", "docs/readme.md"]
    assert _get(parsed, "added_lines") == 4


def test_parse_multi_hunk_diff_per_hunk_counts():
    parsed = parse_unified_diff(VALID_MULTI_HUNK_DIFF)

    hunks = list(_get(parsed, "hunks"))
    assert len(hunks) == 3

    first, second, third = hunks
    assert (
        _get(first, "context_lines"),
        _get(first, "deletions"),
        _get(first, "additions"),
    ) == (2, 1, 2)
    assert (
        _get(second, "context_lines"),
        _get(second, "deletions"),
        _get(second, "additions"),
    ) == (2, 0, 1)
    assert (
        _get(third, "context_lines"),
        _get(third, "deletions"),
        _get(third, "additions"),
    ) == (2, 0, 1)

    assert _get(first, "path") == "src/app.py"
    assert _get(second, "path") == "src/app.py"
    assert _get(third, "path") == "docs/readme.md"


def test_parse_strips_b_prefix_and_keeps_dev_null():
    parsed = parse_unified_diff(NEW_FILE_DIFF)

    assert list(_get(parsed, "paths")) == ["new_file.py"]
    assert _get(parsed, "added_lines") == 3


def test_parse_deletion_only_file_is_not_a_new_side_path():
    """``+++ /dev/null`` is a deletion-only file: no new-side path."""
    diff = (
        "--- a/src/app.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-x = 1\n"
        "-y = 2\n"
    )
    parsed = parse_unified_diff(diff)

    assert list(_get(parsed, "paths")) == []
    assert _get(parsed, "added_lines") == 0
    assert _get(next(iter(_get(parsed, "hunks"))), "deletions") == 2


def test_parse_ignores_no_newline_marker():
    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " import os\n"
        "-x = 1\n"
        "\\ No newline at end of file\n"
        "+x = 2\n"
        "\\ No newline at end of file\n"
    )
    parsed = parse_unified_diff(diff)

    assert _get(parsed, "added_lines") == 1
    hunk = next(iter(_get(parsed, "hunks")))
    assert _get(hunk, "deletions") == 1
    assert _get(hunk, "context_lines") == 1


def test_parse_empty_diff_is_empty():
    parsed = parse_unified_diff("")

    assert list(_get(parsed, "paths")) == []
    assert _get(parsed, "added_lines") == 0
    assert list(_get(parsed, "hunks")) == []


# --------------------------------------------------------------------------
# parse_unified_diff: malformed input
# --------------------------------------------------------------------------


def test_parse_rejects_plus_header_without_minus_header():
    diff = "+++ b/src/app.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    with pytest.raises(PatchFormatError):
        parse_unified_diff(diff)


def test_parse_rejects_unparseable_hunk_range():
    diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,x +1,1 @@\n-x\n+y\n"
    with pytest.raises(PatchFormatError):
        parse_unified_diff(diff)


def test_parse_rejects_body_line_before_any_hunk_header():
    diff = "--- a/src/app.py\n+++ b/src/app.py\n-x = 1\n+x = 2\n"
    with pytest.raises(PatchFormatError):
        parse_unified_diff(diff)


def test_parse_rejects_hunk_counts_contradicting_declared_ranges():
    # Declares 5 old-side lines but supplies 3.
    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,5 +1,5 @@\n"
        " a\n"
        " b\n"
        " c\n"
    )
    with pytest.raises(PatchFormatError):
        parse_unified_diff(diff)


def test_parse_rejects_hunk_with_too_many_body_lines():
    # Declares 2 old-side lines but supplies 3.
    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        " b\n"
        " c\n"
    )
    with pytest.raises(PatchFormatError):
        parse_unified_diff(diff)


# --------------------------------------------------------------------------
# validate_for_propose: happy paths
# --------------------------------------------------------------------------


def test_validate_accepts_valid_multi_hunk_diff(worktree: Path):
    result = validate_for_propose(VALID_MULTI_HUNK_DIFF, str(worktree))

    assert result == {"paths": ["src/app.py", "docs/readme.md"], "added_lines": 4}


def test_validate_accepts_new_file_diff(worktree: Path):
    result = validate_for_propose(NEW_FILE_DIFF, str(worktree))

    assert result["paths"] == ["new_file.py"]
    assert result["added_lines"] == 3


def test_validate_accepts_context_anchored_delete_and_add(worktree: Path):
    result = validate_for_propose(CONTEXT_ANCHORED_DIFF, str(worktree))

    assert result["paths"] == ["src/app.py"]
    assert result["added_lines"] == 1


def test_validate_accepts_empty_diff(worktree: Path):
    assert validate_for_propose("", str(worktree)) == {"paths": [], "added_lines": 0}


def test_validate_accepts_git_touching_patch_at_propose(worktree: Path):
    """The deny list is APPLY-side only: propose must let a human inspect it."""
    diff = (
        "--- a/.git/config\n"
        "+++ b/.git/config\n"
        "@@ -1,2 +1,3 @@\n"
        " [core]\n"
        "+\tbare = false\n"
        " \tfilemode = true\n"
    )
    result = validate_for_propose(diff, str(worktree))

    assert result["paths"] == [".git/config"]
    assert result["added_lines"] == 1


# --------------------------------------------------------------------------
# validate_for_propose: byte-size boundary
# --------------------------------------------------------------------------


def test_validate_refuses_diff_over_byte_limit(worktree: Path):
    _, diff = _padded_block(MAX_BYTES + 1)
    assert len(diff.encode("utf-8")) == MAX_BYTES + 1

    _assert_refused(diff, worktree, "diff too large")


def test_validate_accepts_diff_exactly_at_byte_limit(worktree: Path):
    name, diff = _padded_block(MAX_BYTES)
    assert len(diff.encode("utf-8")) == MAX_BYTES

    result = validate_for_propose(diff, str(worktree))

    assert result["paths"] == [name]
    assert result["added_lines"] == 1


# --------------------------------------------------------------------------
# validate_for_propose: file-count boundary
# --------------------------------------------------------------------------


def test_validate_accepts_exactly_five_files(worktree: Path):
    diff = "".join(_new_file_block(f"f{i}.py", 1) for i in range(MAX_FILES))

    result = validate_for_propose(diff, str(worktree))

    assert result["paths"] == [f"f{i}.py" for i in range(MAX_FILES)]
    assert result["added_lines"] == MAX_FILES


def test_validate_refuses_six_files(worktree: Path):
    diff = "".join(_new_file_block(f"f{i}.py", 1) for i in range(MAX_FILES + 1))

    _assert_refused(diff, worktree, "too many files")


def test_validate_counts_distinct_paths_not_blocks(worktree: Path):
    """Six blocks touching ONE path is one distinct path: accepted."""
    diff = "".join(_new_file_block("same.py", 1) for _ in range(MAX_FILES + 1))

    result = validate_for_propose(diff, str(worktree))

    assert len(set(result["paths"])) == 1
    assert result["added_lines"] == MAX_FILES + 1


# --------------------------------------------------------------------------
# validate_for_propose: added-line boundary
# --------------------------------------------------------------------------


def test_validate_accepts_exactly_four_hundred_added_lines(worktree: Path):
    diff = _new_file_block("big.py", MAX_ADDED_LINES)

    result = validate_for_propose(diff, str(worktree))

    assert result["added_lines"] == MAX_ADDED_LINES
    assert result["paths"] == ["big.py"]


def test_validate_refuses_four_hundred_and_one_added_lines(worktree: Path):
    diff = _new_file_block("big.py", MAX_ADDED_LINES + 1)

    _assert_refused(diff, worktree, "too many added lines")


# --------------------------------------------------------------------------
# validate_for_propose: path resolution (read half only)
# --------------------------------------------------------------------------


def test_validate_refuses_parent_escape_path(worktree: Path):
    diff = _new_file_block("../escape", 1)

    with pytest.raises(WorkspaceSecurityError):
        validate_for_propose(diff, str(worktree))


def test_validate_refuses_absolute_path(worktree: Path):
    diff = _new_file_block("/etc/passwd", 1)

    with pytest.raises(WorkspaceSecurityError):
        validate_for_propose(diff, str(worktree))


def test_validate_does_not_apply_the_deny_list_at_propose(worktree: Path):
    """A denied-at-apply path must still be accepted at propose."""
    diff = _new_file_block(".git/hooks/pre-commit", 1)

    result = validate_for_propose(diff, str(worktree))

    assert result["paths"] == [".git/hooks/pre-commit"]


def test_validate_does_not_call_the_strict_write_resolver(worktree: Path, monkeypatch):
    """Propose uses the READ half only; the write resolver must not be called."""
    from pipeline import worktree_patch

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("resolve_write_target must not be called at propose")

    monkeypatch.setattr(worktree_patch, "resolve_write_target", _boom)

    result = validate_for_propose(VALID_MULTI_HUNK_DIFF, str(worktree))

    assert result["paths"] == ["src/app.py", "docs/readme.md"]


def test_validate_does_not_call_the_deny_list_predicate(worktree: Path, monkeypatch):
    """The deny list is apply-side only; propose must not consult it."""
    from pipeline import worktree_patch

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("is_denied_relative_path must not be called at propose")

    monkeypatch.setattr(worktree_patch, "is_denied_relative_path", _boom)

    result = validate_for_propose(VALID_MULTI_HUNK_DIFF, str(worktree))

    assert result["added_lines"] == 4


# --------------------------------------------------------------------------
# validate_for_propose: whole-file-replacement shape
# --------------------------------------------------------------------------


def test_validate_refuses_whole_file_replacement_shape(worktree: Path):
    _assert_refused(WHOLE_FILE_REPLACEMENT_DIFF, worktree)


def test_validate_refuses_whole_file_replacement_without_context(worktree: Path):
    """The rule is per-hunk: one anchored hunk does not excuse a bare one."""
    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " import os\n"
        "-x = 1\n"
        "+x = 2\n"
        "@@ -10,3 +10,3 @@\n"
        "-a\n"
        "-b\n"
        "-c\n"
        "+d\n"
        "+e\n"
        "+f\n"
    )
    _assert_refused(diff, worktree)


def test_validate_accepts_pure_deletion_hunk(worktree: Path):
    """Zero additions is a deletion, not a whole-file replacement."""
    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,3 +1,1 @@\n"
        "-x = 1\n"
        "-y = 2\n"
        " z = 3\n"
    )
    result = validate_for_propose(diff, str(worktree))

    assert result["added_lines"] == 0
    assert result["paths"] == ["src/app.py"]


def test_validate_accepts_pure_addition_hunk_against_existing_file(worktree: Path):
    """Zero deletions is an insertion, not a whole-file replacement."""
    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,1 +1,3 @@\n"
        " x = 1\n"
        "+y = 2\n"
        "+z = 3\n"
    )
    result = validate_for_propose(diff, str(worktree))

    assert result["added_lines"] == 2
    assert result["paths"] == ["src/app.py"]


# --------------------------------------------------------------------------
# validate_for_propose: malformed input is refused, not crashed on
# --------------------------------------------------------------------------


def test_validate_refuses_missing_plus_header(worktree: Path):
    diff = "--- a/src/app.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    _assert_refused(diff, worktree)


def test_validate_refuses_unparseable_hunk_range(worktree: Path):
    diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,x +1,1 @@\n-x\n+y\n"
    _assert_refused(diff, worktree)


def test_validate_refuses_body_line_before_hunk_header(worktree: Path):
    diff = "--- a/src/app.py\n+++ b/src/app.py\n-x = 1\n+x = 2\n"
    _assert_refused(diff, worktree)


def test_validate_refuses_hunk_counts_contradicting_declared_ranges(worktree: Path):
    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,5 +1,5 @@\n"
        " a\n"
        " b\n"
        " c\n"
    )
    _assert_refused(diff, worktree)


# --------------------------------------------------------------------------
# validate_for_propose: check ORDER (the brief pins a specific order)
# --------------------------------------------------------------------------


def test_byte_size_check_precedes_parsing(worktree: Path):
    """A too-large diff that is ALSO malformed reports the size refusal."""
    diff = "--- a/x.py\n+++ b/x.py\n-" + "z" * 70000 + "\n"
    assert len(diff.encode("utf-8")) > MAX_BYTES

    _assert_refused(diff, worktree, "diff too large")


def test_file_count_check_precedes_added_line_check(worktree: Path):
    """6 files x 100 added lines: the file-count refusal wins."""
    diff = "".join(_new_file_block(f"f{i}.py", 100) for i in range(MAX_FILES + 1))

    _assert_refused(diff, worktree, "too many files")


def test_added_line_check_precedes_path_resolution(worktree: Path):
    """401 added lines in an escaping path: the line-count refusal wins."""
    diff = _new_file_block("../escape", MAX_ADDED_LINES + 1)

    _assert_refused(diff, worktree, "too many added lines")


def test_file_count_check_precedes_whole_file_replacement_check(worktree: Path):
    """6 files, one of them a bare replace: the file-count refusal wins."""
    diff = "".join(_new_file_block(f"f{i}.py", 1) for i in range(MAX_FILES))
    diff += WHOLE_FILE_REPLACEMENT_DIFF

    _assert_refused(diff, worktree, "too many files")


def test_path_resolution_precedes_whole_file_replacement_check(worktree: Path):
    """An escaping path in a bare-replace hunk is refused by the resolver."""
    diff = (
        "--- a/../escape\n"
        "+++ b/../escape\n"
        "@@ -1,3 +1,3 @@\n"
        "-a\n"
        "-b\n"
        "-c\n"
        "+d\n"
        "+e\n"
        "+f\n"
    )

    with pytest.raises(WorkspaceSecurityError):
        validate_for_propose(diff, str(worktree))


# --------------------------------------------------------------------------
# exception contract
# --------------------------------------------------------------------------


def test_patch_format_error_is_a_value_error():
    assert issubclass(PatchFormatError, ValueError)


def test_patch_format_error_is_distinct_from_patch_security_error():
    from pipeline.worktree_patch import PatchSecurityError

    assert not issubclass(PatchFormatError, PatchSecurityError)
    assert not issubclass(PatchSecurityError, PatchFormatError)


# --------------------------------------------------------------------------
# module contract
# --------------------------------------------------------------------------


def test_module_exports_the_new_names():
    from pipeline import worktree_patch

    for name in ("PatchFormatError", "parse_unified_diff", "validate_for_propose"):
        assert name in worktree_patch.__all__, f"{name} missing from __all__"
        assert hasattr(worktree_patch, name)


def test_module_docstring_documents_the_propose_apply_boundary():
    from pipeline import worktree_patch

    doc = worktree_patch.__doc__ or ""
    assert "propose" in doc.lower()
    assert "apply" in doc.lower()
