"""Dead-code gate: decorated module-level functions and conftest.py.

A new module-level function whose only reference is its own decorator
registration (pytest fixture, route, click command, atexit.register) is not
dead code: the decorator is what wires it into the framework. Likewise a
function added to a `conftest.py` is picked up by pytest by convention, so it
must not be reported either. NTF-01 went `failed` on an autouse fixture in
tests/conftest.py.

The `_git` / `_init_repo_with_feature_branch` helpers below are copies of the
ones in test_dead_code_gate.py - test modules do not import from each other.
"""

import subprocess

from pipeline import server as p


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo_with_feature_branch(tmp_path, base_content, feature_content,
                                    base_filename="mod.py",
                                    extra_feature_files=None):
    """A repo with `base_content` committed on master, then a `feature`
    branch (checked out from master) adding `feature_content` as a new
    commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "master", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / base_filename).write_text(base_content)
    _git("add", base_filename, cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / base_filename).write_text(feature_content)
    files_to_add = [base_filename]
    for name, content in (extra_feature_files or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
        files_to_add.append(name)
    _git("add", *files_to_add, cwd=repo)
    _git("commit", "-q", "-m", "feature work", cwd=repo)
    return repo


def test_pytest_fixture_only_referenced_by_its_decorator_is_not_dead(tmp_path):
    """An autouse fixture is wired in by pytest via its decorator alone."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content=(
            "import pytest\n\n\n"
            "def existing_helper():\n"
            "    return 'hi'\n\n\n"
            "@pytest.fixture(autouse=True)\n"
            "def _story_fixture():\n"
            "    return None\n"
        ),
    )
    result = p._find_dead_new_functions(repo, "master")
    assert result == []


def test_route_handler_only_referenced_by_its_decorator_is_not_dead(tmp_path):
    """A route handler is registered by its decorator, never called by name."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content=(
            "from flask import Flask\n\n"
            "app = Flask(__name__)\n\n\n"
            "def existing_helper():\n"
            "    return 'hi'\n\n\n"
            "@app.route('/x')\n"
            "def view():\n"
            "    return 'ok'\n"
        ),
    )
    result = p._find_dead_new_functions(repo, "master")
    assert result == []


def test_negative_undecorated_unreferenced_function_is_still_dead(tmp_path):
    """The gate keeps its teeth: no decorator, no reference -> still dead."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content=(
            "def existing_helper():\n"
            "    return 'hi'\n\n\n"
            "def helper():\n"
            "    return 42\n"
        ),
    )
    result = p._find_dead_new_functions(repo, "master")
    assert "mod.py:helper" in result


def test_function_in_conftest_is_not_dead(tmp_path):
    """pytest collects conftest.py by convention, so nothing there is dead."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content="def existing_helper():\n    return 'hi'\n",
        extra_feature_files={
            "tests/conftest.py": "def _cleanup():\n    pass\n",
        },
    )
    result = p._find_dead_new_functions(repo, "master")
    assert result == []


def test_negative_same_function_in_a_normal_module_is_dead(tmp_path):
    """The conftest.py skip is path-specific: a normal package still reports."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content="def existing_helper():\n    return 'hi'\n",
        extra_feature_files={
            "pkg/normal.py": "def _cleanup():\n    pass\n",
        },
    )
    result = p._find_dead_new_functions(repo, "master")
    assert "pkg/normal.py:_cleanup" in result


def test_syntax_error_file_does_not_raise(tmp_path):
    """A new file with a syntax error must not crash the gate."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content="def existing_helper():\n    return 'hi'\n",
        extra_feature_files={
            "bad.py": "def broken(:\n    pass\n",
        },
    )
    result = p._find_dead_new_functions(repo, "master")
    assert isinstance(result, list)
