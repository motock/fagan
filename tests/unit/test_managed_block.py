"""TDD tests for ``pipeline.managed_block`` -- the fenced managed-block editor.

``pipeline.managed_block`` is a pure, stdlib-only, no-I/O helper that rewrites a
single fenced region of a text file, delimited by ``BEGIN_MARKER`` and
``END_MARKER``. Contract under test:

* no markers in ``existing`` -> the fenced block is appended after a blank line
  (an empty ``existing`` gets just the fenced block, no leading blank line);
* exactly one well-formed pair -> only the text between the markers is replaced;
* the result always ends with exactly one newline, and applying the same block
  twice is byte-identical to applying it once;
* every byte outside the markers is preserved verbatim (CRLF included);
* malformed input fails closed with ``ValueError``: BEGIN without END, END
  without BEGIN, END before BEGIN, more than one pair, and a ``block`` argument
  that itself contains either marker string.

These tests are RED until the implementation lands: ``pipeline.managed_block``
does not exist yet, so the import below raises ``ModuleNotFoundError``.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest
from pipeline.managed_block import BEGIN_MARKER, END_MARKER, apply_managed_block

from pipeline import managed_block

BLOCK = "## Managed\n\n- alpha\n- beta"
FENCED = "before\n\n" + BEGIN_MARKER + "\nOLD BODY\n" + END_MARKER + "\nafter\n"
CASES = ["", "hello\n", "hello", "hello\n\n\n", FENCED]


def _apply(existing: str, block: str = BLOCK) -> str:
    return apply_managed_block(existing, block)


def _tree() -> ast.Module:
    return ast.parse(Path(managed_block.__file__).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Module shape: exact constants, one public function, stdlib only, no I/O
# ---------------------------------------------------------------------------


def test_marker_constants_are_exact():
    assert BEGIN_MARKER == (
        "<!-- fagan:begin (managed block - edits inside are overwritten on "
        "update) -->"
    )
    assert END_MARKER == "<!-- fagan:end -->"


def test_exposes_one_public_function_with_the_documented_signature():
    assert callable(apply_managed_block)
    assert list(inspect.signature(apply_managed_block).parameters) == [
        "existing",
        "block",
    ]
    tree = _tree()
    public = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    assert public == ["apply_managed_block"]


def test_module_is_stdlib_only_and_performs_no_io():
    tree = _tree()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names)
    io_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"open", "print", "input"}
    ]
    assert io_calls == []


def test_empty_existing_gets_just_the_fenced_block():
    result = _apply("")
    assert result.startswith(BEGIN_MARKER)  # no leading blank line
    assert result.endswith(END_MARKER + "\n")
    assert BLOCK in result
    assert result.count(BEGIN_MARKER) == 1
    assert result.count(END_MARKER) == 1


@pytest.mark.parametrize("existing", ["hello\n", "hello"])
def test_no_markers_appends_after_a_blank_line(existing):
    result = _apply(existing)
    assert result.startswith("hello\n\n" + BEGIN_MARKER)
    assert result.endswith(END_MARKER + "\n")
    assert BLOCK in result


def test_trailing_blank_lines_still_leave_a_blank_line_before_the_block():
    result = _apply("hello\n\n\n")
    assert result.startswith("hello\n")
    begin = result.index(BEGIN_MARKER)
    assert result[begin - 2 : begin] == "\n\n"
    assert BLOCK in result
    assert result.endswith(END_MARKER + "\n")


def test_replaces_only_what_is_between_the_markers():
    result = _apply(FENCED)
    assert result.startswith("before\n\n" + BEGIN_MARKER)
    assert result.endswith(END_MARKER + "\nafter\n")
    assert "OLD BODY" not in result
    assert result.count(BEGIN_MARKER) == 1
    assert result.count(END_MARKER) == 1
    assert result.index(BEGIN_MARKER) < result.index(BLOCK) < result.index(END_MARKER)


def test_every_byte_outside_the_markers_is_preserved_including_crlf():
    existing = "a\r\nb\r\n\r\n" + BEGIN_MARKER + "\nOLD BODY\n" + END_MARKER + "\r\nc\n"
    prefix = existing[: existing.index(BEGIN_MARKER)]
    suffix = existing[existing.index(END_MARKER) + len(END_MARKER) :]
    result = _apply(existing)
    assert result.startswith(prefix)
    assert result.endswith(suffix)
    assert "OLD BODY" not in result
    assert BLOCK in result


@pytest.mark.parametrize("existing", CASES)
def test_applying_the_same_block_twice_is_byte_identical(existing):
    once = _apply(existing)
    assert _apply(once) == once


@pytest.mark.parametrize("existing", CASES)
def test_result_always_ends_with_exactly_one_newline(existing):
    result = _apply(existing)
    assert result.endswith("\n")
    assert not result.endswith("\n\n")


@pytest.mark.parametrize(
    "existing",
    [
        "x\n" + BEGIN_MARKER + "\nbody\n",  # BEGIN without END
        "x\n" + END_MARKER + "\n",  # END without BEGIN
        END_MARKER + "\nbody\n" + BEGIN_MARKER + "\n",  # END before BEGIN
        BEGIN_MARKER + "\na\n" + END_MARKER + "\nmid\n" + BEGIN_MARKER + "\nb\n" + END_MARKER + "\n",
        BEGIN_MARKER + "\na\n" + BEGIN_MARKER + "\nb\n" + END_MARKER + "\n",
    ],
)
def test_malformed_marker_layout_raises_value_error(existing):
    with pytest.raises(ValueError) as excinfo:
        _apply(existing)
    assert str(excinfo.value)


@pytest.mark.parametrize("block", ["body\n" + BEGIN_MARKER + "\n", "body\n" + END_MARKER + "\n"])
def test_block_containing_a_marker_raises_value_error(block):
    with pytest.raises(ValueError) as excinfo:
        apply_managed_block("hello\n", block)
    message = str(excinfo.value)
    assert message
    assert "marker" in message.lower() or BEGIN_MARKER in message or END_MARKER in message


@pytest.mark.parametrize(
    "existing",
    [
        "<!-- fagan:begin (managed block - edits inside are\noverwritten on update) -->\n",
        "<!-- fagan:end\n-->\n",
    ],
)
def test_marker_split_across_lines_is_not_a_marker(existing):
    result = _apply(existing)
    assert existing.strip("\n") in result  # marker-free -> appended, not replaced
    assert result.count(BEGIN_MARKER) == 1
    assert result.count(END_MARKER) == 1
    assert BLOCK in result
