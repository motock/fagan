import pytest
from pipeline import server as p

def test_ci_status_passes_with_sha(monkeypatch):
    sha = "deadbeef1234567890abcdef"
    def _fake_run(argv, **_):
        # ensure the command includes the SHA path
        assert f"/commits/{sha}/check-runs" in argv[1]
        class R:
            returncode = 0
            stdout = '{"name":"Lint","status":"completed","conclusion":"success"}\n'
            stderr = ""
        return R()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    result = p._ci_status("agent/x", sha=sha)
    assert result["state"] == "pass"

def test_ci_status_does_not_use_branch_only_query(monkeypatch):
    def _fake_run(argv, **_):
        # branch-only query would contain 'pr' and 'checks'
        assert not ("gh" in argv and "pr" in argv)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    p._ci_status("agent/x", sha="deadbeef")
