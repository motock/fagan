"""Tests for the restore_file per-path cap (RESTORE_FILE_MAX_PER_PATH).

Live evidence (2026-09-23): a run that keeps restoring the same file is
undoing its own work — ASB-2 and the board.js story each restored 3x and
ended with nothing landed. Both scripts/local_agent_tools.py and its
verbatim twin scripts/local_agent_oracle_tools.py must refuse the THIRD
restore of the same path within one run, pointing the model at anchored
str_replace edits instead. A failed restore (non-zero git exit) must NOT
count toward the cap, and the tool's description strings in the two config
modules must stay untouched.
"""
import ast
import subprocess
from pathlib import Path

import pytest

from scripts import local_agent_oracle_tools as laot
from scripts import local_agent_tools as lat

MODULES = [
    pytest.param(lat, id="dispatch"),
    pytest.param(laot, id="oracle"),
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The exact description text the brief forbids changing (identical in both
# config modules). Only this one entry is pinned — the surrounding tool list
# is a shared artifact later stories may extend.
RESTORE_FILE_DESCRIPTION = (
    "Discard your changes to ONE file and restore it to the last commit "
    "(git checkout HEAD -- <path>). Use this when your edits to a file have "
    "gone wrong and you want a clean slate for it specifically, instead of "
    "str_replace/replace_lines patches on top of a mess. Does not touch any "
    "other file."
)

CONFIG_MODULES = [
    pytest.param(REPO_ROOT / "scripts" / "local_agent_config.py", id="dispatch"),
    pytest.param(REPO_ROOT / "scripts" / "local_agent_oracle_config.py", id="oracle"),
]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )


def _make_repo(tmp_path):
    """A real one-commit git repo with two committed files (a.txt, b.txt)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("committed A\n")
    (repo / "b.txt").write_text("committed B\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _origin(repo):
    """A fresh per-run origin dict (fresh _RESTORES_THIS_RUN state)."""
    return {"CWD": repo}


def _restore(module, origin, path):
    return module.run_tool_impl(origin, "restore_file", {"path": path})


def _counts(origin):
    return origin.get("_RESTORES_THIS_RUN", {})


# --- the constant itself -------------------------------------------------

@pytest.mark.parametrize("module", MODULES)
def test_max_per_path_constant_is_two(module):
    assert module.RESTORE_FILE_MAX_PER_PATH == 2


# --- happy path: two restores per path per run ---------------------------

@pytest.mark.parametrize("module", MODULES)
def test_first_and_second_restore_succeed_and_restore_content(module, tmp_path):
    repo = _make_repo(tmp_path)
    origin = _origin(repo)

    (repo / "a.txt").write_text("dirty 1\n")
    out1 = _restore(module, origin, "a.txt")
    assert out1.startswith("restored a.txt to its last commit (HEAD)")
    assert (repo / "a.txt").read_text() == "committed A\n"

    (repo / "a.txt").write_text("dirty 2\n")
    out2 = _restore(module, origin, "a.txt")
    assert out2.startswith("restored a.txt to its last commit (HEAD)")
    assert (repo / "a.txt").read_text() == "committed A\n"


@pytest.mark.parametrize("module", MODULES)
def test_successful_restores_are_counted_in_origin_state(module, tmp_path):
    repo = _make_repo(tmp_path)
    origin = _origin(repo)

    (repo / "a.txt").write_text("dirty 1\n")
    _restore(module, origin, "a.txt")
    assert _counts(origin).get("a.txt") == 1

    (repo / "a.txt").write_text("dirty 2\n")
    _restore(module, origin, "a.txt")
    assert _counts(origin).get("a.txt") == 2


# --- boundary: the THIRD restore is refused and does nothing -------------

@pytest.mark.parametrize("module", MODULES)
def test_third_restore_is_refused_and_does_not_restore(module, tmp_path):
    repo = _make_repo(tmp_path)
    origin = _origin(repo)

    for i in range(2):
        (repo / "a.txt").write_text(f"dirty {i}\n")
        assert _restore(module, origin, "a.txt").startswith("restored a.txt")

    (repo / "a.txt").write_text("dirty 3\n")
    out = _restore(module, origin, "a.txt")
    assert out.startswith("ERROR: restore_file refused")
    assert "a.txt" in out
    assert "2 times this run" in out
    assert "Do not restore it again" in out
    assert "str_replace" in out
    # The refusal must NOT have run the checkout.
    assert (repo / "a.txt").read_text() == "dirty 3\n"
    # ...and must not have bumped the counter either.
    assert _counts(origin).get("a.txt") == 2


@pytest.mark.parametrize("module", MODULES)
def test_cap_is_per_path_not_global(module, tmp_path):
    repo = _make_repo(tmp_path)
    origin = _origin(repo)

    for i in range(2):
        (repo / "a.txt").write_text(f"dirty {i}\n")
        assert _restore(module, origin, "a.txt").startswith("restored a.txt")
    (repo / "a.txt").write_text("dirty 3\n")
    assert _restore(module, origin, "a.txt").startswith("ERROR: restore_file refused")

    # A different path is still restorable after a.txt is capped.
    (repo / "b.txt").write_text("dirty B\n")
    out = _restore(module, origin, "b.txt")
    assert out.startswith("restored b.txt to its last commit (HEAD)")
    assert (repo / "b.txt").read_text() == "committed B\n"


@pytest.mark.parametrize("module", MODULES)
def test_cap_is_per_run_fresh_origin_resets_it(module, tmp_path):
    repo = _make_repo(tmp_path)
    first_run = _origin(repo)
    for i in range(2):
        (repo / "a.txt").write_text(f"dirty {i}\n")
        assert _restore(module, first_run, "a.txt").startswith("restored a.txt")
    (repo / "a.txt").write_text("dirty 3\n")
    assert _restore(module, first_run, "a.txt").startswith("ERROR: restore_file refused")

    # A new run (fresh origin dict) starts with a clean slate.
    second_run = _origin(repo)
    assert _restore(module, second_run, "a.txt").startswith("restored a.txt")
    assert (repo / "a.txt").read_text() == "committed A\n"


# --- failed restores must not count --------------------------------------

@pytest.mark.parametrize("module", MODULES)
def test_failed_restore_does_not_count_toward_cap(module, tmp_path):
    repo = _make_repo(tmp_path)
    origin = _origin(repo)

    # late.txt exists in the working tree but not in HEAD -> git checkout fails.
    (repo / "late.txt").write_text("untracked\n")
    for _ in range(2):
        out = _restore(module, origin, "late.txt")
        assert out.startswith("ERROR: could not restore late.txt to HEAD")
    assert _counts(origin).get("late.txt", 0) == 0

    # Now make it real: commit it, then two successful restores.
    _git(repo, "add", "late.txt")
    _git(repo, "commit", "-q", "-m", "add late")
    for i in range(2):
        (repo / "late.txt").write_text(f"dirty {i}\n")
        assert _restore(module, origin, "late.txt").startswith("restored late.txt")
    assert _counts(origin).get("late.txt") == 2

    # The 5th call is refused ONLY because of the two successes.
    (repo / "late.txt").write_text("dirty 5\n")
    out = _restore(module, origin, "late.txt")
    assert out.startswith("ERROR: restore_file refused")
    assert (repo / "late.txt").read_text() == "dirty 5\n"


# --- empty / missing path still short-circuits ---------------------------

@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("args", [{}, {"path": ""}, {"path": None}], ids=["missing", "empty", "none"])
def test_empty_path_still_requires_a_path(module, tmp_path, args):
    repo = _make_repo(tmp_path)
    origin = _origin(repo)
    assert module.run_tool_impl(origin, "restore_file", args) == (
        "ERROR: restore_file requires a path."
    )
    # A rejected call must not consume a restore slot.
    assert _counts(origin).get("a.txt", 0) == 0


@pytest.mark.parametrize("module", MODULES)
def test_empty_path_does_not_consume_a_slot(module, tmp_path):
    repo = _make_repo(tmp_path)
    origin = _origin(repo)
    for _ in range(5):
        assert _restore(module, origin, "") == "ERROR: restore_file requires a path."
    for i in range(2):
        (repo / "a.txt").write_text(f"dirty {i}\n")
        assert _restore(module, origin, "a.txt").startswith("restored a.txt")


# --- the tool description strings must be untouched ----------------------

def _restore_file_description(config_path):
    tree = ast.parse(config_path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {
            k.value: v for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if pairs.get("name") is not None and getattr(pairs["name"], "value", None) == "restore_file":
            return pairs["description"].value
    raise AssertionError(f"no restore_file tool entry found in {config_path}")


@pytest.mark.parametrize("config_path", CONFIG_MODULES)
def test_restore_file_description_unchanged(config_path):
    assert _restore_file_description(config_path) == RESTORE_FILE_DESCRIPTION
