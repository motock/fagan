"""Tests for the step-cap auto-done gate appended to scripts/local_agent_git.py.

Two new names are graded here:

- ``_changed_production_paths(paths)`` — pure filter: which of the paths a
  branch touched are *production* files (everything that is not a test file,
  not ``conftest.py``, and not one of the agent's dot-prefixed runtime
  artifacts).
- ``_step_cap_auto_done_impl(origin)`` — the gate itself. ``origin`` is the
  calling agent module's ``globals()`` dict (the same routing convention as
  every other ``*_impl`` in that file), so ``origin["git"]`` and
  ``origin["_full_suite_result"]`` are read at CALL time.

No real git or pytest subprocess is ever started: ``origin["git"]`` is a fake
that returns canned ``SimpleNamespace(returncode=..., stdout=...)`` objects
keyed off ``args[0]``, and ``origin["_full_suite_result"]`` is a stub that
records whether it was called.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.local_agent_git import (
    _TEST_FILE_PREFIXES,
    _changed_production_paths,
    _step_cap_auto_done_impl,
)

_REPO_ROOT = Path(__file__).parent.parent.parent

# The refs the impl must try, in this exact order, before giving up.
_BASE_REFS = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


def _proc(returncode: int = 0, stdout: str = "") -> SimpleNamespace:
    """Stand-in for subprocess.CompletedProcess: the impl reads .returncode
    and .stdout only."""
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _make_origin(base_ref, diff_stdout="", suite_result=(True, "", None),
                 diff_rc=0, merge_base_rc=0, merge_base_stdout="abc123\n",
                 git_exc=None):
    """Build a fake ``origin`` globals-dict plus a call recorder.

    ``base_ref`` is the single ref that resolves (returncode 0 with non-empty
    stdout); every other ref returns 128. ``None`` means no ref resolves.
    """
    calls = {"merge_base_refs": [], "diff": [], "suite": 0, "git": []}

    def fake_git(*args):
        calls["git"].append(args)
        if git_exc is not None:
            raise git_exc
        if args[0] == "merge-base":
            ref = args[2]
            calls["merge_base_refs"].append(ref)
            if ref == base_ref:
                return _proc(merge_base_rc, merge_base_stdout)
            return _proc(128, "")
        if args[0] == "diff":
            calls["diff"].append(args)
            return _proc(diff_rc, diff_stdout)
        raise AssertionError(f"unexpected git invocation: {args!r}")

    def fake_suite():
        calls["suite"] += 1
        return suite_result

    return {"git": fake_git, "_full_suite_result": fake_suite}, calls


# --------------------------------------------------------------------------
# module constant
# --------------------------------------------------------------------------

def test_test_file_prefixes_constant():
    """The one module constant the story adds, with its exact value."""
    assert _TEST_FILE_PREFIXES == ("test_",)


def test_new_names_are_appended_at_the_end_of_the_module():
    """The block is appended at the END: both new defs come after the last
    pre-existing top-level def, and the constant precedes the filter."""
    text = (_REPO_ROOT / "scripts" / "local_agent_git.py").read_text(encoding="utf-8")
    assert "def _baseline_only_failures" in text
    assert text.index("def _baseline_only_failures") < text.index(
        "def _changed_production_paths"
    )
    assert text.index("def _changed_production_paths") < text.index(
        "def _step_cap_auto_done_impl"
    )
    assert text.index("_TEST_FILE_PREFIXES =") < text.index(
        "def _changed_production_paths"
    )


def test_new_functions_have_docstrings():
    assert _changed_production_paths.__doc__
    assert _step_cap_auto_done_impl.__doc__


# --------------------------------------------------------------------------
# _changed_production_paths
# --------------------------------------------------------------------------

def test_changed_production_paths_keeps_production_files():
    assert _changed_production_paths(["pipeline/x.py", "README.md"]) == [
        "pipeline/x.py",
        "README.md",
    ]


def test_changed_production_paths_drops_non_production_files():
    dropped = [
        "tests/unit/test_x.py",
        "pkg/tests/helper.py",
        "test_top.py",
        "pkg/conftest.py",
        ".agent_scratchpad.md",
        "a/.hidden/b.py",
        "",
    ]
    assert _changed_production_paths(dropped) == []


def test_changed_production_paths_preserves_input_order():
    paths = [
        "tests/unit/test_x.py",
        "pipeline/z.py",
        ".agent_transcript.json",
        "README.md",
        "pkg/tests/helper.py",
        "src/a.py",
    ]
    assert _changed_production_paths(paths) == [
        "pipeline/z.py",
        "README.md",
        "src/a.py",
    ]


def test_changed_production_paths_empty_list():
    assert _changed_production_paths([]) == []


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/test_x.py",
        "tests/foo.py",
        "pkg/tests/helper.py",
        "a/b/tests/c.py",
        "test_top.py",
        "pkg/test_bar.py",
        "conftest.py",
        "pkg/conftest.py",
        ".agent_scratchpad.md",
        ".agent_transcript.json",
        ".dispatch_baseline_test_checked",
        "a/.hidden/b.py",
        "src/.cache/x.py",
        "",
    ],
)
def test_changed_production_paths_drops_each_non_production_shape(path):
    assert _changed_production_paths([path]) == []


@pytest.mark.parametrize(
    "path",
    [
        "pipeline/x.py",
        "README.md",
        "src/a/b/c.py",
        "pkg/v1.2/mod.py",  # a dot inside a component is fine
        "tests_helpers.py",  # prefix is "test_", not "tests"
        "pkg/conftest_extra.py",
        "contest.py",
    ],
)
def test_changed_production_paths_keeps_each_production_shape(path):
    assert _changed_production_paths([path]) == [path]


# --------------------------------------------------------------------------
# _step_cap_auto_done_impl — happy path
# --------------------------------------------------------------------------

def test_auto_done_true_when_base_resolves_and_suite_is_green():
    origin, calls = _make_origin(
        "origin/HEAD", diff_stdout="pipeline/x.py\n", suite_result=(True, "", None)
    )
    assert _step_cap_auto_done_impl(origin) is True
    assert calls["merge_base_refs"] == ["origin/HEAD"]
    assert calls["diff"] == [("diff", "--name-only", "abc123", "HEAD")]
    assert calls["suite"] == 1


def test_auto_done_true_ignores_a_nonempty_suite_tail():
    origin, calls = _make_origin(
        "origin/HEAD", diff_stdout="pipeline/x.py\n", suite_result=(True, "tail", None)
    )
    assert _step_cap_auto_done_impl(origin) is True
    assert calls["suite"] == 1


def test_auto_done_strips_whitespace_around_diff_lines():
    origin, calls = _make_origin(
        "origin/HEAD", diff_stdout="  pipeline/x.py  \n\n", suite_result=(True, "", None)
    )
    assert _step_cap_auto_done_impl(origin) is True
    assert calls["suite"] == 1


def test_auto_done_true_when_production_and_test_files_are_mixed():
    origin, calls = _make_origin(
        "origin/HEAD",
        diff_stdout="tests/unit/test_x.py\npipeline/x.py\n",
        suite_result=(True, "", None),
    )
    assert _step_cap_auto_done_impl(origin) is True
    assert calls["suite"] == 1


# --------------------------------------------------------------------------
# _step_cap_auto_done_impl — red suite / lint
# --------------------------------------------------------------------------

@pytest.mark.parametrize("gate", ["test", "lint"])
def test_auto_done_false_when_suite_reports_red(gate):
    origin, calls = _make_origin(
        "origin/HEAD",
        diff_stdout="pipeline/x.py\n",
        suite_result=(False, "tail", gate),
    )
    assert _step_cap_auto_done_impl(origin) is False
    assert calls["suite"] == 1


@pytest.mark.parametrize("falsy", [False, 0, None, ""])
def test_auto_done_false_for_any_falsy_suite_first_element(falsy):
    origin, calls = _make_origin(
        "origin/HEAD", diff_stdout="pipeline/x.py\n", suite_result=(falsy, "tail", "test")
    )
    assert _step_cap_auto_done_impl(origin) is False
    assert calls["suite"] == 1


# --------------------------------------------------------------------------
# _step_cap_auto_done_impl — no production change => suite never runs
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "diff_stdout",
    [
        "tests/unit/test_x.py\n",
        "tests/unit/test_x.py\npkg/tests/helper.py\n",
        ".agent_scratchpad.md\n",
        ".agent_scratchpad.md\n.agent_transcript.json\n",
        "",
        "\n",
        "   \n",
    ],
)
def test_auto_done_false_and_suite_never_called_without_production_change(diff_stdout):
    origin, calls = _make_origin(
        "origin/HEAD", diff_stdout=diff_stdout, suite_result=(True, "", None)
    )
    assert _step_cap_auto_done_impl(origin) is False
    assert calls["suite"] == 0


# --------------------------------------------------------------------------
# _step_cap_auto_done_impl — base resolution
# --------------------------------------------------------------------------

@pytest.mark.parametrize("base_ref", _BASE_REFS)
def test_auto_done_uses_the_first_resolving_ref(base_ref):
    origin, calls = _make_origin(
        base_ref, diff_stdout="pipeline/x.py\n", suite_result=(True, "", None)
    )
    assert _step_cap_auto_done_impl(origin) is True
    expected = list(_BASE_REFS[: _BASE_REFS.index(base_ref) + 1])
    assert calls["merge_base_refs"] == expected


def test_auto_done_falls_back_to_master_when_earlier_refs_are_128():
    origin, calls = _make_origin(
        "master", diff_stdout="pipeline/x.py\n", suite_result=(True, "", None)
    )
    assert _step_cap_auto_done_impl(origin) is True
    assert calls["merge_base_refs"] == list(_BASE_REFS)


def test_auto_done_false_when_no_ref_resolves():
    origin, calls = _make_origin(None, diff_stdout="pipeline/x.py\n")
    assert _step_cap_auto_done_impl(origin) is False
    assert calls["merge_base_refs"] == list(_BASE_REFS)
    assert calls["diff"] == []
    assert calls["suite"] == 0


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_auto_done_false_when_merge_base_stdout_is_blank(blank):
    """returncode 0 with empty/whitespace stdout is NOT a resolved base."""
    origin, calls = _make_origin(
        "origin/HEAD", diff_stdout="pipeline/x.py\n", merge_base_stdout=blank
    )
    assert _step_cap_auto_done_impl(origin) is False
    assert calls["merge_base_refs"] == list(_BASE_REFS)
    assert calls["suite"] == 0


# --------------------------------------------------------------------------
# _step_cap_auto_done_impl — git failures fail closed
# --------------------------------------------------------------------------

def test_auto_done_false_when_diff_returns_nonzero():
    origin, calls = _make_origin(
        "origin/HEAD", diff_stdout="pipeline/x.py\n", diff_rc=1
    )
    assert _step_cap_auto_done_impl(origin) is False
    assert calls["suite"] == 0


def test_auto_done_false_without_raising_when_git_raises_oserror():
    origin, calls = _make_origin("origin/HEAD", git_exc=OSError("boom"))
    assert _step_cap_auto_done_impl(origin) is False
    assert calls["suite"] == 0


def test_auto_done_false_without_raising_when_suite_stub_raises():
    origin, calls = _make_origin("origin/HEAD", diff_stdout="pipeline/x.py\n")

    def boom():
        calls["suite"] += 1
        raise RuntimeError("suite exploded")

    origin["_full_suite_result"] = boom
    assert _step_cap_auto_done_impl(origin) is False


# --------------------------------------------------------------------------
# _step_cap_auto_done_impl — origin is read at CALL time (routing convention)
# --------------------------------------------------------------------------

def test_auto_done_reads_origin_git_at_call_time():
    origin, calls = _make_origin(
        "origin/HEAD", diff_stdout="pipeline/x.py\n", suite_result=(True, "", None)
    )
    assert _step_cap_auto_done_impl(origin) is True

    # Re-bind origin["git"] the way monkeypatch.setattr(mod, "git", ...) would:
    # the second call must observe the NEW function, not a frozen copy.
    def all_128(*args):
        calls["git"].append(args)
        return _proc(128, "")

    origin["git"] = all_128
    assert _step_cap_auto_done_impl(origin) is False


def test_auto_done_reads_origin_suite_at_call_time():
    origin, _calls = _make_origin(
        "origin/HEAD", diff_stdout="pipeline/x.py\n", suite_result=(True, "", None)
    )
    assert _step_cap_auto_done_impl(origin) is True

    origin["_full_suite_result"] = lambda: (False, "tail", "test")
    assert _step_cap_auto_done_impl(origin) is False
