import json

from pipeline import ci as p


def _run(returncode=0, stdout="", stderr=""):
    class R:
        pass
    r = R()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def test_ci_status_queries_the_sha_scoped_check_runs_endpoint(monkeypatch):
    sha = "deadbeef1234567890abcdef"
    calls = []

    def _fake_run(argv, **_):
        calls.append(argv)
        return _run(stdout='{"name":"Lint","status":"completed","conclusion":"success"}\n')

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha=sha)
    assert result == {"state": "pass", "error": ""}
    assert len(calls) == 1
    assert calls[0][:2] == ["gh", "api"]
    assert f"commits/{sha}/check-runs" in calls[0][2]


def test_ci_status_never_falls_back_to_branch_only_query_when_sha_present(monkeypatch):
    def _fake_run(argv, **_):
        assert not (argv[:3] == ["gh", "pr", "checks"]), (
            "must not issue a branch-only query when a SHA is available - "
            "that's the exact stale-read race Mode 26 closes"
        )
        return _run(stdout='{"name":"Lint","status":"completed","conclusion":"success"}\n')

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    p._ci_status("agent/x", sha="deadbeef")


def test_ci_status_falls_back_to_branch_query_when_sha_empty(monkeypatch):
    """No local worktree to read a fresher commit from -> the pre-Mode-26
    branch-scoped query is an acceptable degraded fallback (see docstring)."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(argv)
        return _run(stdout=json.dumps([{"bucket": "pass"}]))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="")
    assert result == {"state": "pass", "error": ""}
    assert calls[0] == ["gh", "pr", "checks", "agent/x", "--json", "bucket"]


def test_ci_status_sha_scoped_fail_bucket(monkeypatch):
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Test","status":"completed","conclusion":"success"}\n'
            '{"name":"Lint","status":"completed","conclusion":"failure"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    assert p._ci_status("agent/x", sha="deadbeef")["state"] == "fail"


def test_ci_status_sha_scoped_cancelled_bucket(monkeypatch):
    def _fake_run(argv, **_):
        return _run(stdout='{"name":"Lint","status":"completed","conclusion":"cancelled"}\n')

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    assert p._ci_status("agent/x", sha="deadbeef")["state"] == "cancelled"


def test_ci_status_sha_scoped_fail_wins_over_cancelled(monkeypatch):
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Test","status":"completed","conclusion":"cancelled"}\n'
            '{"name":"Lint","status":"completed","conclusion":"failure"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    assert p._ci_status("agent/x", sha="deadbeef")["state"] == "fail"


def test_ci_status_sha_scoped_still_running_polls_then_pending_at_timeout(monkeypatch):
    def _fake_run(argv, **_):
        return _run(stdout='{"name":"Test","status":"in_progress","conclusion":null}\n')

    sleeps = []
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef", timeout_s=0.05)
    assert result["state"] == "pending"
    assert sleeps  # confirms it actually polled at least once, not fast-pathed


def test_ci_status_sha_scoped_empty_and_no_ci_configured_returns_none(monkeypatch):
    monkeypatch.setattr(p.subprocess, "run", lambda argv, **_: _run(stdout=""))
    monkeypatch.setattr(p, "_repo_has_ci_configured", lambda: False)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef")
    assert result["state"] == "none"


def test_ci_rerun_queries_the_sha_scoped_actions_runs_endpoint(monkeypatch):
    sha = "deadbeef1234567890abcdef"
    calls = []

    def _fake_run(argv, **_):
        calls.append(argv)
        if argv[:2] == ["gh", "api"]:
            return _run(stdout="12345\n")
        return _run(returncode=0)

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_rerun(sha) is True
    assert calls[0][:2] == ["gh", "api"]
    assert f"head_sha={sha}" in calls[0][2]
    assert calls[1] == ["gh", "run", "rerun", "12345", "--failed"]


def test_ci_rerun_never_uses_branch_scoped_run_list(monkeypatch):
    def _fake_run(argv, **_):
        assert not (argv[:3] == ["gh", "run", "list"]), (
            "must resolve the run to rerun by SHA, never by branch name - "
            "the stale-run race Mode 26 closes applies here too"
        )
        return _run(stdout="12345\n")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    p._ci_rerun("deadbeef")


def test_ci_rerun_returns_false_when_no_run_found_for_sha(monkeypatch):
    monkeypatch.setattr(p.subprocess, "run", lambda argv, **_: _run(stdout=""))
    assert p._ci_rerun("deadbeef") is False
