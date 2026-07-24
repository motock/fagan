"""Additional guard test for branch‑scoped pending sleep.

This test ensures that when the branch‑scoped path receives a non‑terminal
``bucket`` value (e.g. ``pending``) it sleeps before re‑polling, preventing a
tight busy‑loop.
"""

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


def test_branch_scoped_pending_sleep(monkeypatch):
    """Branch‑scoped path with a non‑terminal bucket triggers ``time.sleep(10)`` between polls."""
    calls = []
    sleeps = []

    def _fake_run(argv, **_):
        calls.append(argv)
        # first call returns pending bucket
        if len(calls) == 1:
            return _run(stdout=json.dumps([{"name": "Lint", "bucket": "pending"}]))
        else:
            return _run(stdout=json.dumps([{"name": "Lint", "bucket": "pass"}]))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="")
    assert result["state"] == "pass"
    assert len(sleeps) >= 1
