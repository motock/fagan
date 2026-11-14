"""Tests for pipeline.test_edit_judge: the mechanical judge of existing-test edits (ov-5).

Written FIRST (TDD). Every test builds a real git repository in ``tmp_path``,
commits a base state, commits a head state, and judges ``base..head``. The
judge reads git itself, so nothing here is mocked.

The judge passes an edit only when every condition holds, and a deleted test
function is never a pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline import test_edit_judge as judge_module
from pipeline.test_edit_judge import judge

BASE_APP = 'PLAN_DIR = "plans"\n\n\ndef plan_dir():\n    return PLAN_DIR\n'
HEAD_APP = 'PLAN_ROOT = "plans"\n\n\ndef plan_dir():\n    return PLAN_ROOT\n'

BASE_TEST = (
    "from pipeline import app\n\n\n"
    "def test_reads_plan_dir_override(monkeypatch):\n"
    '    monkeypatch.setattr(app, "PLAN_DIR", "/tmp/plans")\n'
    '    assert app.plan_dir() == "/tmp/plans"\n\n\n'
    "def test_default_plan_dir():\n"
    '    assert app.plan_dir() == "plans"\n'
)
APP_PATH = "pipeline/app.py"
TEST_PATH = "tests/unit/test_app.py"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(repo: Path, changes: dict, message: str) -> str:
    """Apply ``changes`` (path -> text, bytes, or None for a deletion) and commit."""
    for path, content in changes.items():
        target = repo / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "t@example.com")
    _git(repo_dir, "config", "user.name", "t")
    return repo_dir


def _scenario(repo: Path, base: dict, head: dict) -> tuple[str, str]:
    base_sha = _commit(repo, base, "base")
    head_sha = _commit(repo, head, "head")
    return base_sha, head_sha


def _rename_scenario(repo: Path, test_head: str | None = None) -> tuple[str, str]:
    """The c3-2 shape: the production name is removed, the test renames its patch."""
    head_test = test_head if test_head is not None else BASE_TEST.replace(
        'app, "PLAN_DIR"', 'app, "PLAN_ROOT"'
    )
    return _scenario(
        repo,
        {APP_PATH: BASE_APP, TEST_PATH: BASE_TEST},
        {APP_PATH: HEAD_APP, TEST_PATH: head_test},
    )


# --- passing edits ---------------------------------------------------------


def test_a_rename_of_a_patched_attribute_passes(repo):
    base_sha, head_sha = _rename_scenario(repo)

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "pass"
    assert verdict.reasons == []


def test_a_pass_names_the_existing_test_file_it_judged(repo):
    base_sha, head_sha = _rename_scenario(repo)

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.test_files == [TEST_PATH]


# --- condition (i): assertions never decrease -------------------------------


def test_removing_an_assertion_fails_condition_one(repo):
    head_test = BASE_TEST.replace('    assert app.plan_dir() == "plans"\n', "    pass\n")
    base_sha, head_sha = _rename_scenario(repo, head_test)

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"
    assert any("test_default_plan_dir" in reason and "assertions 1 -> 0" in reason for reason in verdict.reasons)


def test_removing_a_pytest_raises_block_fails_condition_one(repo):
    base_test = (
        "import pytest\n"
        "from pipeline import app\n\n\n"
        "def test_rejects_bad_dir():\n"
        "    with pytest.raises(ValueError):\n"
        "        app.plan_dir(None)\n"
    )
    head_test = "import pytest\nfrom pipeline import app\n\n\ndef test_rejects_bad_dir():\n    app.plan_dir(None)\n"
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: BASE_APP, TEST_PATH: base_test},
        {APP_PATH: HEAD_APP, TEST_PATH: head_test},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"
    assert any("test_rejects_bad_dir" in reason and "assertions 1 -> 0" in reason for reason in verdict.reasons)


def test_moving_an_assertion_to_another_test_fails_condition_one(repo):
    head_test = (
        "from pipeline import app\n\n\n"
        "def test_reads_plan_dir_override(monkeypatch):\n"
        '    monkeypatch.setattr(app, "PLAN_ROOT", "/tmp/plans")\n\n\n'
        "def test_default_plan_dir():\n"
        '    assert app.plan_dir() == "plans"\n'
    )
    base_sha, head_sha = _rename_scenario(repo, head_test)

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"
    assert any("test_reads_plan_dir_override" in reason for reason in verdict.reasons)


# --- condition (ii): no added skip, xfail or importorskip -------------------


@pytest.mark.parametrize(
    ("anchor", "added"),
    [
        ("def test_default_plan_dir", "@pytest.mark.skip(reason='later')\n"),
        ("def test_default_plan_dir", "@pytest.mark.skipif(True, reason='later')\n"),
        ("def test_default_plan_dir", "@pytest.mark.xfail\n"),
        ('    assert app.plan_dir() == "plans"\n', "    pytest.importorskip('yaml')\n"),
    ],
)
def test_an_added_skip_xfail_or_importorskip_fails_condition_two(repo, anchor, added):
    head_test = BASE_TEST.replace(anchor, added + anchor)
    base_sha, head_sha = _rename_scenario(repo, head_test)

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"
    assert any("skip" in reason or "xfail" in reason or "importorskip" in reason for reason in verdict.reasons)


def test_removing_a_skip_marker_is_not_reported_as_an_added_skip(repo):
    base_test = BASE_TEST.replace("def test_default_plan_dir", "@pytest.mark.skip\ndef test_default_plan_dir")
    head_test = BASE_TEST.replace('app, "PLAN_DIR"', 'app, "PLAN_ROOT"')
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: BASE_APP, TEST_PATH: base_test},
        {APP_PATH: HEAD_APP, TEST_PATH: head_test},
    )

    verdict = judge(base_sha, head_sha, repo)

    # The removed decorator names no removed production name, so condition (iv)
    # still refuses it; the point here is that condition (ii) stays silent.
    assert not any("skip" in reason for reason in verdict.reasons)


# --- condition (iii): an expected literal may change only when the brief names it


def _limit_scenario(repo, head_assert: str):
    base_test = "from pipeline import app\n\n\ndef test_limit():\n    assert app.LIMIT == 2\n"
    head_test = f"from pipeline import app\n\n\ndef test_limit():\n    assert app.LIMIT == {head_assert}\n"
    return _scenario(
        repo,
        {APP_PATH: "LIMIT = 2\n", TEST_PATH: base_test},
        {APP_PATH: "LIMIT = 3\n", TEST_PATH: head_test},
    )


def test_a_changed_expected_number_fails_condition_three_without_a_brief(repo):
    base_sha, head_sha = _limit_scenario(repo, "3")

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"
    assert any("expected value" in reason for reason in verdict.reasons)


def test_a_changed_expected_number_passes_when_the_brief_names_both_values(repo):
    base_sha, head_sha = _limit_scenario(repo, "3")

    verdict = judge(base_sha, head_sha, repo, brief="LIMIT goes from 2 to 3.")

    assert verdict.outcome == "pass"


def test_a_brief_naming_only_the_new_value_does_not_approve_the_old_one(repo):
    base_sha, head_sha = _limit_scenario(repo, "3")

    verdict = judge(base_sha, head_sha, repo, brief="LIMIT is now 3.")

    assert verdict.outcome == "fail"


def test_a_number_inside_a_longer_number_in_the_brief_does_not_count(repo):
    base_sha, head_sha = _limit_scenario(repo, "3")

    verdict = judge(base_sha, head_sha, repo, brief="LIMIT goes from 20 to 30.")

    assert verdict.outcome == "fail"


def test_a_changed_expected_string_fails_condition_three(repo):
    base_test = 'from pipeline import app\n\n\ndef test_name():\n    assert app.NAME == "old"\n'
    head_test = 'from pipeline import app\n\n\ndef test_name():\n    assert app.NAME == "new"\n'
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: 'NAME = "old"\n', TEST_PATH: base_test},
        {APP_PATH: 'NAME = "new"\n', TEST_PATH: head_test},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"


def test_a_changed_literal_on_a_non_assertion_line_is_not_condition_three(repo):
    base_test = BASE_TEST.replace('"PLAN_DIR", "/tmp/plans"', '"PLAN_DIR", "/tmp/one"')
    head_test = BASE_TEST.replace('"PLAN_DIR", "/tmp/plans"', '"PLAN_ROOT", "/tmp/two"')
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: BASE_APP, TEST_PATH: base_test},
        {APP_PATH: HEAD_APP, TEST_PATH: head_test},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "pass"


# --- condition (iv): every removed line references a removed production name -


def test_an_edit_unrelated_to_the_removed_name_fails_condition_four(repo):
    base_test = (
        "from pipeline import app\n\n\n"
        "def test_ready():\n"
        "    assert app.is_ready() is True\n"
    )
    head_test = (
        "from pipeline import app\n\n\n"
        "def test_ready():\n"
        "    assert app.is_ready() is not False\n"
    )
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: BASE_APP, TEST_PATH: base_test},
        {APP_PATH: HEAD_APP, TEST_PATH: head_test},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"
    assert any("does not reference a removed name" in reason for reason in verdict.reasons)


def test_a_test_only_edit_has_no_removed_production_name_to_reference(repo):
    base_test = (
        "from pipeline import app\n\n\n"
        "def test_ready():\n"
        "    old_helper = 1\n"
        "    assert app.plan_dir() == old_helper\n"
    )
    head_test = (
        "from pipeline import app\n\n\n"
        "def test_ready():\n"
        "    assert app.plan_dir() == 1\n"
    )
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: BASE_APP, TEST_PATH: base_test},
        {APP_PATH: BASE_APP, TEST_PATH: head_test},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"


# --- a deleted test function is never a pass --------------------------------


def test_a_deleted_test_function_is_deletion_of_pin_and_never_a_pass(repo):
    head_test = BASE_TEST.replace(
        'app, "PLAN_DIR"', 'app, "PLAN_ROOT"'
    ).replace(
        "\n\n\ndef test_default_plan_dir():\n    assert app.plan_dir() == \"plans\"\n", "\n"
    )
    base_sha, head_sha = _rename_scenario(repo, head_test)

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "deletion_of_pin"
    assert verdict.outcome != "pass"
    assert verdict.deleted_tests == ["test_default_plan_dir"]
    assert any("test_default_plan_dir" in reason for reason in verdict.reasons)


def test_a_deleted_test_file_is_deletion_of_pin_naming_its_tests(repo):
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: BASE_APP, TEST_PATH: BASE_TEST},
        {APP_PATH: HEAD_APP, TEST_PATH: None},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "deletion_of_pin"
    assert set(verdict.deleted_tests) == {"test_reads_plan_dir_override", "test_default_plan_dir"}


def test_a_renamed_test_function_counts_as_a_deletion(repo):
    head_test = BASE_TEST.replace("def test_default_plan_dir", "def test_plan_dir_default")
    base_sha, head_sha = _rename_scenario(repo, head_test)

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "deletion_of_pin"
    assert "test_default_plan_dir" in verdict.deleted_tests


def test_a_new_test_function_alongside_the_old_ones_is_not_a_deletion(repo):
    head_test = BASE_TEST.replace('app, "PLAN_DIR"', 'app, "PLAN_ROOT"') + (
        "\n\ndef test_added_later():\n    assert True\n"
    )
    base_sha, head_sha = _rename_scenario(repo, head_test)

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "pass"
    assert verdict.deleted_tests == []


# --- negative and boundary inputs never raise ------------------------------


def test_an_empty_diff_is_not_a_pass(repo):
    sha = _commit(repo, {APP_PATH: BASE_APP, TEST_PATH: BASE_TEST}, "base")

    verdict = judge(sha, sha, repo)

    assert verdict.outcome == "fail"
    assert any("no existing test file" in reason for reason in verdict.reasons)


def test_a_new_test_file_alone_is_not_an_existing_test_edit(repo):
    base_sha = _commit(repo, {APP_PATH: BASE_APP}, "base")
    head_sha = _commit(repo, {TEST_PATH: BASE_TEST}, "head")

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"


def test_a_binary_change_to_an_existing_test_file_fails_without_raising(repo):
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: BASE_APP, "tests/data/blob.bin": b"\x00\x01\x02"},
        {APP_PATH: HEAD_APP, "tests/data/blob.bin": b"\xff\x00\xfe"},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"


def test_an_unreadable_ref_fails_without_raising(repo):
    verdict = judge("0" * 40, "f" * 40, repo)

    assert verdict.outcome == "fail"
    assert verdict.reasons


def test_a_test_file_that_does_not_parse_fails_without_raising(repo):
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: BASE_APP, TEST_PATH: BASE_TEST},
        {APP_PATH: HEAD_APP, TEST_PATH: "def test_broken(:\n"},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.outcome == "fail"


def test_reasons_carry_no_file_contents(repo):
    base_test = "from pipeline import app\n\n\ndef test_limit():\n    assert app.LIMIT == 2\n"
    head_test = "from pipeline import app\n\n\ndef test_limit():\n    assert app.LIMIT == 3\n"
    base_sha, head_sha = _scenario(
        repo,
        {APP_PATH: "LIMIT = 2\n", TEST_PATH: base_test},
        {APP_PATH: "LIMIT = 3\n", TEST_PATH: head_test},
    )

    verdict = judge(base_sha, head_sha, repo)

    assert verdict.reasons
    for reason in verdict.reasons:
        assert "\n" not in reason
        assert "app.LIMIT" not in reason
        assert "==" not in reason


def test_the_judge_module_exposes_the_three_outcomes_as_constants():
    assert judge_module.PASS == "pass"
    assert judge_module.FAIL == "fail"
    assert judge_module.DELETION_OF_PIN == "deletion_of_pin"
