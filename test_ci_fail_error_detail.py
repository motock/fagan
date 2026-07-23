"""Tests for the `error` detail populated by `_ci_status` in pipeline/ci.py.

The merge gate's `_ci_status` historically returned an EMPTY `error` string on
definitive CI failures and cancellations, even though the parsed check list
was already in scope and contained each failing/cancelled check's name. The
rework feedback an operator sees literally read `ci fail: ` with nothing after
it. These tests pin the contract that the `error` field now carries the
failing/cancelled check names (and conclusions), truncated to 300 chars, while
the `pass` and timeout-`pending` returns stay byte-for-byte unchanged.

These tests are written FIRST (TDD) and are expected to be RED until the
implementation in pipeline/ci.py is updated to populate `error`.
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


# ---------- SHA-scoped path ----------

def test_sha_scoped_single_fail_names_check_and_conclusion(monkeypatch):
    """Case 1: one check fails -> error contains that check's name and its
    conclusion, and state is fail."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Lint","status":"completed","conclusion":"failure"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef")
    assert result["state"] == "fail"
    assert "Lint" in result["error"]
    assert "failure" in result["error"]


def test_sha_scoped_two_fails_names_both_present(monkeypatch):
    """Case 2: two checks fail -> both names appear in error."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Lint","status":"completed","conclusion":"failure"}\n'
            '{"name":"Test","status":"completed","conclusion":"timed_out"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef")
    assert result["state"] == "fail"
    assert "Lint" in result["error"]
    assert "Test" in result["error"]
    assert "timed_out" in result["error"]


def test_sha_scoped_cancelled_only_names_cancelled_check(monkeypatch):
    """Case 3: one cancelled, none failed -> state cancelled, error names the
    cancelled check."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Build","status":"completed","conclusion":"cancelled"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef")
    assert result["state"] == "cancelled"
    assert "Build" in result["error"]


def test_sha_scoped_all_pass_unchanged(monkeypatch):
    """Case 4: all pass -> state pass, error empty (regression bar)."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Lint","status":"completed","conclusion":"success"}\n'
            '{"name":"Test","status":"completed","conclusion":"success"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef")
    assert result == {"state": "pass", "error": ""}


# ---------- Branch-scoped fallback path ----------

def test_branch_scoped_requests_name_and_bucket(monkeypatch):
    """Case 5a: the branch-scoped fallback (empty sha) issues `gh pr checks
    ... --json name,bucket` (not just `--json bucket`)."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(argv)
        return _run(stdout=json.dumps([{"name": "Lint", "bucket": "pass"}]))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    p._ci_status("agent/x", sha="")
    assert calls, "expected at least one gh invocation"
    assert calls[0] == ["gh", "pr", "checks", "agent/x", "--json", "name,bucket"]


def test_branch_scoped_fail_bucket_names_check(monkeypatch):
    """Case 5b: a fail bucket in the branch-scoped path yields an error
    containing the check name."""
    def _fake_run(argv, **_):
        return _run(stdout=json.dumps([{"name": "Lint", "bucket": "fail"}]))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="")
    assert result["state"] == "fail"
    assert "Lint" in result["error"]


def test_branch_scoped_cancelled_bucket_names_check(monkeypatch):
    """Branch-scoped cancelled bucket names the cancelled check."""
    def _fake_run(argv, **_):
        return _run(stdout=json.dumps([{"name": "Build", "bucket": "cancelled"}]))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="")
    assert result["state"] == "cancelled"
    assert "Build" in result["error"]


def test_branch_scoped_all_pass_unchanged(monkeypatch):
    """Branch-scoped all-pass stays byte-for-byte unchanged."""
    def _fake_run(argv, **_):
        return _run(stdout=json.dumps([{"name": "Lint", "bucket": "pass"}]))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="")
    assert result == {"state": "pass", "error": ""}


# ---------- Truncation ----------

def test_error_truncated_at_300_chars(monkeypatch):
    """Case 6: a pathological many-failing-checks payload cannot bloat the
    manifest - the built error string is truncated to 300 chars."""
    # Fabricate 200 failing checks with long names; the naive join would be
    # far longer than 300 chars.
    lines = "\n".join(
        f'{{"name":"check-{i:04d}-long-name-padding","status":"completed","conclusion":"failure"}}'
        for i in range(200)
    ) + "\n"

    def _fake_run(argv, **_):
        return _run(stdout=lines)

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef")
    assert result["state"] == "fail"
    assert len(result["error"]) <= 300


# ---------- Timeout-pending regression guard ----------

def test_timeout_pending_error_message_byte_for_byte(monkeypatch):
    """Case 7: the final timeout-pending fallback still returns the exact
    `CI did not complete within timeout` message. This is the regression
    guard for the attempt-2 mistake of blanking it out."""
    def _fake_run(argv, **_):
        # A check that never completes -> polls until the deadline expires.
        return _run(stdout='{"name":"Test","status":"in_progress","conclusion":null}\n')

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef", timeout_s=0.05)
    assert result["state"] == "pending"
    assert result["error"] == "CI did not complete within timeout"