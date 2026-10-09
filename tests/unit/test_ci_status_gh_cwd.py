import json

import pytest

from pipeline import ci as p
from pipeline import server
from pipeline.merge import _merge_gate_ci_status


def _result(returncode=0, stdout="", stderr=""):
    class R:
        pass

    r = R()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


_GREEN_SHA = '{"name":"Lint","status":"completed","conclusion":"success"}\n'
_GREEN_BRANCH = json.dumps([{"name": "Lint", "bucket": "pass"}])
_RED_BRANCH = json.dumps([{"name": "Lint", "bucket": "fail"}])
_RED_SHA = '{"name":"Lint","status":"completed","conclusion":"failure"}\n'


@pytest.fixture
def recorded(monkeypatch, tmp_path):
    """Patch the scoped root and record every (argv, cwd) passed to subprocess.run."""
    root = tmp_path / "scoped"
    root.mkdir()
    monkeypatch.setattr(server, "REPO_ROOT", root)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("cwd", "<missing>")))
        if argv[:2] == ["gh", "api"]:
            return _result(stdout=_GREEN_SHA)
        return _result(stdout=_GREEN_BRANCH)

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    return root, calls


def test_ci_status_branch_path_runs_gh_in_scoped_root(recorded):
    root, calls = recorded
    p._ci_status("agent/x", sha="")
    assert calls and all(cwd == str(root) for _, cwd in calls)
    assert calls[0][0][:3] == ["gh", "pr", "checks"]


def test_ci_status_sha_path_runs_gh_in_scoped_root(recorded):
    root, calls = recorded
    p._ci_status("agent/x", sha="abc")
    assert calls and all(cwd == str(root) for _, cwd in calls)
    assert calls[0][0][:2] == ["gh", "api"]


def test_ci_status_once_branch_path_runs_gh_in_scoped_root(recorded):
    root, calls = recorded
    p._ci_status_once("agent/x", sha="")
    assert calls and all(cwd == str(root) for _, cwd in calls)
    assert calls[0][0][:3] == ["gh", "pr", "checks"]


def test_ci_status_once_sha_path_runs_gh_in_scoped_root(recorded):
    root, calls = recorded
    p._ci_status_once("agent/x", sha="abc")
    assert calls and all(cwd == str(root) for _, cwd in calls)
    assert calls[0][0][:2] == ["gh", "api"]


def test_scoped_root_is_read_at_call_time(recorded, monkeypatch, tmp_path):
    _, calls = recorded
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setattr(server, "REPO_ROOT", second)
    p._ci_status_once("agent/x", sha="")
    assert calls[-1][1] == str(second)


def _foreign_root_stub(monkeypatch, tmp_path, *, sha_path):
    root = tmp_path / "foreign"
    root.mkdir()
    monkeypatch.setattr(server, "REPO_ROOT", root)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    red = _RED_SHA if sha_path else _RED_BRANCH

    def _fake_run(argv, **kwargs):
        if kwargs.get("cwd") == str(root):
            return _result(stdout=red)
        return _result(returncode=1, stderr="no pull requests found")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    return root


def test_merge_gate_reports_fail_for_red_checks_in_foreign_repo(monkeypatch, tmp_path):
    _foreign_root_stub(monkeypatch, tmp_path, sha_path=False)
    assert _merge_gate_ci_status("agent/x", sha="")["state"] == "fail"


def test_merge_gate_reports_fail_for_red_checks_in_foreign_repo_sha_path(
    monkeypatch, tmp_path
):
    _foreign_root_stub(monkeypatch, tmp_path, sha_path=True)
    assert _merge_gate_ci_status("agent/x", sha="abc")["state"] == "fail"


def test_stub_yields_none_when_cwd_is_not_scoped_root(monkeypatch, tmp_path):
    """Guards against a vacuous stub: a non-matching cwd must produce 'none'."""
    _foreign_root_stub(monkeypatch, tmp_path, sha_path=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(server, "REPO_ROOT", elsewhere)
    result = p._ci_status_once("agent/x", sha="")
    assert result["state"] == "none"
    assert "no pull requests found" in result["error"]
