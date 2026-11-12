"""Agent-side full-suite done-gate honours LOCAL_AGENT_TEST_SCOPE.

``_full_suite_result_impl`` (scripts/local_agent_git.py and its oracle twin
scripts/local_agent_oracle_git.py) runs the repo's test command before a
rework round may reject or accept ``done``. When the dispatcher exports
``LOCAL_AGENT_TEST_SCOPE`` the command is scoped through
``pipeline.test_scope_run.agent_scoped_test_argv``; when it is unset, or the
spec is malformed, the detected command runs verbatim exactly as before.

Written FIRST (TDD). The subprocess boundary is mocked on the module, the
detect_* helpers are stubbed on a fake ``p``, and ``changed_test_paths`` is
stubbed so no real git call runs. No real test suite is executed.
"""

import json
import sys
from types import SimpleNamespace

import pytest

import scripts.local_agent_git as lag_git
import scripts.local_agent_oracle_git as lao_git
from pipeline import test_scope_run as tsr

MODULES = [lag_git, lao_git]

TEST_DIR = "/repo"
TEST_CMD = [sys.executable, "-m", "pytest", "-q"]
LINT_CMD = [sys.executable, "-m", "ruff", "check", "."]
ACCEPTANCE_REL = "tests/unit/test_acceptance_fixture.py"


class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _spec(**overrides):
    spec = {"acceptance": [ACCEPTANCE_REL], "ci_failures": [], "base_branch": "master"}
    spec.update(overrides)
    return json.dumps(spec)


def _install(monkeypatch, mod, tmp_path, *, lint_cmd, test_rc=0, lint_rc=0):
    """Wire the fake ``p`` and a recording subprocess.run onto ``mod``."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if list(argv) == LINT_CMD:
            return _R(lint_rc, out="lint failed")
        return _R(test_rc)

    fake_p = SimpleNamespace(
        detect_test_command=lambda cwd: (TEST_DIR, list(TEST_CMD)),
        detect_lint_command=lambda cwd: (TEST_DIR, list(LINT_CMD)) if lint_cmd else None,
        _is_heavy=lambda argv: False,
        failed_node_ids=lambda stdout: [],
    )
    monkeypatch.setattr(mod, "subprocess", SimpleNamespace(run=fake_run))
    origin = {"p": fake_p, "CWD": tmp_path}
    return origin, calls


@pytest.mark.parametrize("mod", MODULES)
def test_unset_scope_runs_detected_command_verbatim(mod, monkeypatch, tmp_path):
    monkeypatch.delenv("LOCAL_AGENT_TEST_SCOPE", raising=False)
    origin, calls = _install(monkeypatch, mod, tmp_path, lint_cmd=False)

    result = mod._full_suite_result_impl(origin)

    assert result == (True, "", None)
    assert calls == [TEST_CMD]


@pytest.mark.parametrize("mod", MODULES)
def test_valid_spec_with_acceptance_runs_scoped_pytest_command(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_AGENT_TEST_SCOPE", _spec())
    monkeypatch.setattr(tsr, "changed_test_paths", lambda worktree, base: [])
    origin, calls = _install(monkeypatch, mod, tmp_path, lint_cmd=False)

    result = mod._full_suite_result_impl(origin)

    assert result == (True, "", None)
    assert calls == [[*TEST_CMD, str(tmp_path / ACCEPTANCE_REL)]]


@pytest.mark.parametrize("mod", MODULES)
def test_nothing_in_scope_runs_no_tests_command(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_AGENT_TEST_SCOPE", _spec(acceptance=[]))
    monkeypatch.setattr(tsr, "changed_test_paths", lambda worktree, base: [])
    origin, calls = _install(monkeypatch, mod, tmp_path, lint_cmd=False)

    result = mod._full_suite_result_impl(origin)

    assert result == (True, "", None)
    assert calls == [tsr.NO_TESTS_CMD]


@pytest.mark.parametrize("mod", MODULES)
def test_nothing_in_scope_still_fails_on_lint(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_AGENT_TEST_SCOPE", _spec(acceptance=[]))
    monkeypatch.setattr(tsr, "changed_test_paths", lambda worktree, base: [])
    origin, calls = _install(monkeypatch, mod, tmp_path, lint_cmd=True, lint_rc=1)

    passed, tail, gate = mod._full_suite_result_impl(origin)

    assert (passed, gate) == (False, "lint")
    assert "lint failed" in tail
    assert calls == [tsr.NO_TESTS_CMD, LINT_CMD]


@pytest.mark.parametrize("mod", MODULES)
def test_malformed_spec_runs_detected_command_and_warns(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCAL_AGENT_TEST_SCOPE", "{")
    origin, calls = _install(monkeypatch, mod, tmp_path, lint_cmd=False)

    result = mod._full_suite_result_impl(origin)

    assert result == (True, "", None)
    assert calls == [TEST_CMD]
    assert "ignoring malformed LOCAL_AGENT_TEST_SCOPE" in capsys.readouterr().err
