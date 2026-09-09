"""Tests for pipeline.env_file (CFG-D1 follow-up: pure .pipeline.env parser).

The production scheduler is launched by launchd as `python -m
pipeline.scheduler_daemon`, so nothing sources scripts/pipeline-env.sh for it.
Python must be able to read the same shell-style env file itself.

Contract under test (see the story brief):
- parse_env_file(path) -> dict[str, str]: pure parser, NO side effects.
  Handles blank lines, full-line # comments, a leading `export ` prefix,
  single- or double-quoted values (exactly ONE matching pair stripped),
  values containing '=' (split on the FIRST '=' only), trailing whitespace,
  and CRLF line endings. A line with no '=' is skipped, not an error. A
  missing or unreadable file (e.g. a directory path) returns {} rather than
  raising.
- find_env_file(repo_root) -> Path | None: <repo_root>/.pipeline.env if it
  exists, else None.
- Purity: parsing must NEVER write os.environ (the module must be import-safe
  from pipeline/__init__.py, so it has no pipeline imports at all and never
  touches os.environ).
"""
import os
from pathlib import Path

import pytest

from pipeline import env_file
from pipeline.env_file import find_env_file, parse_env_file

# ---------------------------------------------------------------------------
# parse_env_file: happy path
# ---------------------------------------------------------------------------

def test_simple_key_value_line(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("KEY=value\n", encoding="utf-8")

    parsed = parse_env_file(env_file_path)

    assert parsed == {"KEY": "value"}
    assert isinstance(parsed, dict)
    assert all(isinstance(v, str) for v in parsed.values())


def test_multiple_lines_parse_to_one_dict(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text(
        "MODEL=glm-5.3-flash:cloud\nPLAN_DIR=/tmp/plans\n", encoding="utf-8"
    )

    assert parse_env_file(env_file_path) == {
        "MODEL": "glm-5.3-flash:cloud",
        "PLAN_DIR": "/tmp/plans",
    }


def test_export_prefix_is_stripped(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("export KEY=value\n", encoding="utf-8")

    assert parse_env_file(env_file_path) == {"KEY": "value"}


def test_export_prefix_with_quotes_is_stripped(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text('export KEY="quoted value"\n', encoding="utf-8")

    assert parse_env_file(env_file_path) == {"KEY": "quoted value"}


def test_double_quoted_value_has_exactly_one_quote_pair_stripped(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text('KEY="value"\n', encoding="utf-8")

    parsed = parse_env_file(env_file_path)

    assert parsed == {"KEY": "value"}
    assert parsed["KEY"] == "value"  # no leading/trailing " left behind
    assert '"' not in parsed["KEY"]


def test_single_quoted_value_has_exactly_one_quote_pair_stripped(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("KEY='value'\n", encoding="utf-8")

    parsed = parse_env_file(env_file_path)

    assert parsed == {"KEY": "value"}
    assert "'" not in parsed["KEY"]


def test_value_containing_equals_splits_on_first_equals_only(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("MODEL=glm-5.3-flash:cloud=x\n", encoding="utf-8")

    parsed = parse_env_file(env_file_path)

    assert parsed == {"MODEL": "glm-5.3-flash:cloud=x"}
    assert list(parsed.keys()) == ["MODEL"]


def test_trailing_whitespace_is_stripped_from_unquoted_value(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("KEY=value   \n", encoding="utf-8")

    assert parse_env_file(env_file_path) == {"KEY": "value"}


def test_trailing_whitespace_after_quoted_value_is_stripped(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text('KEY="value"   \n', encoding="utf-8")

    assert parse_env_file(env_file_path) == {"KEY": "value"}


def test_crlf_line_endings_leave_no_trailing_carriage_return(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_bytes(b"KEY=value\r\nOTHER=thing\r\n")

    parsed = parse_env_file(env_file_path)

    assert parsed == {"KEY": "value", "OTHER": "thing"}
    assert all("\r" not in v for v in parsed.values())
    assert all("\r" not in k for k in parsed)


def test_only_one_matching_pair_is_stripped(tmp_path):
    # Strip ONE matching pair: quotes of the *other* kind inside the value
    # must survive.
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text('KEY="\'inner\'"\n', encoding="utf-8")
    assert parse_env_file(env_file_path) == {"KEY": "'inner'"}

    env_file_path.write_text("OTHER='\"inner\"'\n", encoding="utf-8")
    assert parse_env_file(env_file_path) == {"OTHER": '"inner"'}


def test_quoted_value_containing_equals_keeps_the_equals(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text('KEY="a=b"\n', encoding="utf-8")

    assert parse_env_file(env_file_path) == {"KEY": "a=b"}


def test_unbalanced_quote_does_not_raise(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text('KEY="value\n', encoding="utf-8")

    parsed = parse_env_file(env_file_path)  # must not raise

    assert "KEY" in parsed
    assert "value" in parsed["KEY"]


def test_empty_value_parses_to_empty_string(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("KEY=\n", encoding="utf-8")

    assert parse_env_file(env_file_path) == {"KEY": ""}


# ---------------------------------------------------------------------------
# parse_env_file: lines that must be ignored
# ---------------------------------------------------------------------------

def test_comment_and_blank_lines_are_ignored(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text(
        "# a full-line comment\n"
        "\n"
        "KEY=value\n"
        "# another comment\n"
        "\n",
        encoding="utf-8",
    )

    assert parse_env_file(env_file_path) == {"KEY": "value"}


def test_whitespace_only_line_is_treated_as_blank(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("   \nKEY=value\n\t\n", encoding="utf-8")

    assert parse_env_file(env_file_path) == {"KEY": "value"}


def test_line_without_equals_is_skipped_not_an_error(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text(
        "JUST_A_TOKEN\nKEY=value\n", encoding="utf-8"
    )

    parsed = parse_env_file(env_file_path)

    assert parsed == {"KEY": "value"}


def test_bare_export_without_equals_is_skipped_not_an_error(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("export FOO\nKEY=value\n", encoding="utf-8")

    parsed = parse_env_file(env_file_path)

    assert parsed == {"KEY": "value"}


def test_empty_file_parses_to_empty_dict(tmp_path):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text("", encoding="utf-8")

    assert parse_env_file(env_file_path) == {}


# ---------------------------------------------------------------------------
# parse_env_file: negative / unreadable inputs
# ---------------------------------------------------------------------------

def test_missing_file_returns_empty_dict_and_does_not_raise(tmp_path):
    missing = tmp_path / "does_not_exist.env"

    parsed = parse_env_file(missing)

    assert parsed == {}


def test_directory_path_returns_empty_dict_and_does_not_raise(tmp_path):
    # A directory is stat-able but unreadable as a file: must not raise
    # IsADirectoryError/PermissionError.
    parsed = parse_env_file(tmp_path)

    assert parsed == {}


# ---------------------------------------------------------------------------
# find_env_file
# ---------------------------------------------------------------------------

def test_find_env_file_returns_path_when_present(tmp_path):
    expected = tmp_path / ".pipeline.env"
    expected.write_text("KEY=value\n", encoding="utf-8")

    found = find_env_file(tmp_path)

    assert found == expected
    assert isinstance(found, Path)
    assert found.name == ".pipeline.env"
    assert found.parent == tmp_path
    assert found.exists()


def test_find_env_file_returns_none_when_absent(tmp_path):
    assert find_env_file(tmp_path) is None


def test_find_env_file_returns_none_for_nonexistent_repo_root(tmp_path):
    assert find_env_file(tmp_path / "no_such_root") is None


def test_find_env_file_ignores_other_dotfiles(tmp_path):
    (tmp_path / "pipeline.env").write_text("KEY=value\n", encoding="utf-8")  # no leading dot
    (tmp_path / ".pipeline.env.bak").write_text("KEY=value\n", encoding="utf-8")

    assert find_env_file(tmp_path) is None


# ---------------------------------------------------------------------------
# Purity: no os.environ mutation, no pipeline imports
# ---------------------------------------------------------------------------

def test_parsing_never_mutates_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAN_DIR", "/before/parse")
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text(
        "PLAN_DIR=/should/not/leak\nOTHER_KEY=other\n", encoding="utf-8"
    )

    parsed = parse_env_file(env_file_path)

    assert parsed == {"PLAN_DIR": "/should/not/leak", "OTHER_KEY": "other"}
    assert os.environ["PLAN_DIR"] == "/before/parse"
    assert "OTHER_KEY" not in os.environ


def test_module_has_no_pipeline_imports():
    # env_file must be import-safe from pipeline/__init__.py: it may not
    # import anything from the pipeline package (no pipeline.paths,
    # pipeline.config, pipeline.server, ...).
    source = Path(env_file.__file__).read_text(encoding="utf-8")

    assert "from pipeline" not in source
    assert "import pipeline" not in source


def test_module_never_references_os_environ():
    # The story's hard rule: NO side effects, this module must never write
    # (or read) os.environ.
    source = Path(env_file.__file__).read_text(encoding="utf-8")

    assert "os.environ" not in source


def test_public_surface_is_exactly_the_two_functions():
    import pipeline.env_file as module

    public = {name for name in dir(module) if not name.startswith("_")}
    # The story mandates exactly two public functions. dir() also picks up
    # whatever stdlib names the implementation imports (os, Path, ...) and
    # `annotations` when `from __future__ import annotations` is used - those
    # are implementation details, not API, so they're tolerated; a THIRD
    # public function/constant is not.
    assert {"parse_env_file", "find_env_file"} <= public
    assert public - {"parse_env_file", "find_env_file"} <= {
        "os", "Path", "annotations",
    }


def test_module_defines_both_functions_as_callables():
    import pipeline.env_file as module

    assert callable(module.parse_env_file)
    assert callable(module.find_env_file)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("KEY=value\n", {"KEY": "value"}),
        ("export KEY=value\n", {"KEY": "value"}),
        ('KEY="v"\n', {"KEY": "v"}),
        ("KEY='v'\n", {"KEY": "v"}),
        ("A=1=B\n", {"A": "1=B"}),
        ("# comment\n\nKEY=v\n", {"KEY": "v"}),
        ("KEY=value\r\n", {"KEY": "value"}),
        ("NO_EQUALS_SIGN\nKEY=v\n", {"KEY": "v"}),
    ],
)
def test_parse_matrix(tmp_path, raw, expected):
    env_file_path = tmp_path / ".pipeline.env"
    env_file_path.write_text(raw, encoding="utf-8")

    assert parse_env_file(env_file_path) == expected