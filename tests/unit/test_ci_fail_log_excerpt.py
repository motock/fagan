"""Tests for the failing-test log excerpt enrichment of `_ci_status` in
pipeline/ci.py.

CONTEXT
-------
`_ci_status`'s `error` field on a CI failure is a CLASSIFICATION only -- it
names which check failed and its bucket/conclusion (e.g. ``"Test Py3.12:
failure"``) but never the actual pytest output (no file:line, no assertion, no
actual-vs-expected).  This file pins the contract that, when ``state ==
"fail"``, `_ci_status` *additionally* captures a bounded excerpt of the failing
job's log via ``gh run view <run_id> --log-failed`` (falling back to ``--log``)
and appends it to the existing classification string -- without replacing it,
without changing the pass/fail/cancelled/pending classification, and without
ever raising.

These tests are written FIRST (TDD) and are expected to be RED until the
implementation in pipeline/ci.py is updated to enrich the fail-state `error`.
"""

import json
import subprocess

from pipeline import ci as p

# A distinctive pytest-shaped excerpt the implementation must surface verbatim
# (or as a tail slice of) into `error`.  We assert on a unique marker so the
# test is robust to whatever bounding/slicing the implementation chooses.
_EXCERPT_MARKER = "ZZZ_ASSERT_FAILED_MARKER_line_42_expected_True_got_False_ZZZ"

_PYTEST_LOG = (
    "============================= test session starts ==============================\n"
    "collected 3 items                                                              \n"
    "tests/unit/test_widget.py::test_widget_renders F                         [ 33%]\n"
    "tests/unit/test_widget.py::test_widget_renders\n"
    f"    assert rendered == True, {_EXCERPT_MARKER}\n"
    "E   AssertionError: assert False == True\n"
    "E    +  where False = widget.render()\n"
    "tests/unit/test_widget.py::test_widget_renders FAILED                     [100%]\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/unit/test_widget.py::test_widget_renders\n"
)


def _run(returncode=0, stdout="", stderr=""):
    class R:
        pass

    r = R()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _set_gate(monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)


# ---------------------------------------------------------------------------
# Helpers to build a fake subprocess.run that distinguishes the *poll* call
# (gh pr checks / gh api .../check-runs) from the *run-list* call
# (gh run list ... --json databaseId) and the *log* call (gh run view ...).
# ---------------------------------------------------------------------------

def _is_poll_call(argv):
    return (
        argv[:2] == ["gh", "api"]
        or argv[:3] == ["gh", "pr", "checks"]
    )


def _is_run_list_call(argv):
    return argv[:3] == ["gh", "run", "list"]


def _is_run_view_call(argv):
    return argv[:3] == ["gh", "run", "view"]


# ===========================================================================
# (a) Happy path: fail bucket -> error carries BOTH classification AND excerpt
# ===========================================================================

# ---------- SHA-scoped path ----------

def test_sha_scoped_fail_appends_pytest_excerpt(monkeypatch):
    """SHA-scoped fail: `error` contains the original classification AND a
    pytest-shaped excerpt captured from `gh run view --log-failed`."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(list(argv))
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 424242}]))
        if _is_run_view_call(argv):
            # --log-failed path returns the pytest log.
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    # Original classification preserved.
    assert "Test Py3.12" in result["error"]
    assert "failure" in result["error"]
    # Excerpt appended.
    assert _EXCERPT_MARKER in result["error"]
    # Classification comes BEFORE the excerpt (append, not replace).
    assert result["error"].index("Test Py3.12") < result["error"].index(_EXCERPT_MARKER)
    # A `gh run view` call was actually made.
    assert any(_is_run_view_call(c) for c in calls), (
        "expected a `gh run view` invocation on a fail state"
    )


# ---------- Branch-scoped path ----------

def test_branch_scoped_fail_appends_pytest_excerpt(monkeypatch):
    """Branch-scoped fail: `error` contains the original classification AND a
    pytest-shaped excerpt captured from `gh run view --log-failed`."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(list(argv))
        if _is_poll_call(argv):
            return _run(stdout=json.dumps([{"name": "Test Py3.12", "bucket": "fail"}]))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 7}]))
        if _is_run_view_call(argv):
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="")

    assert result["state"] == "fail"
    assert "Test Py3.12" in result["error"]
    assert "fail" in result["error"]
    assert _EXCERPT_MARKER in result["error"]
    assert result["error"].index("Test Py3.12") < result["error"].index(_EXCERPT_MARKER)
    assert any(_is_run_view_call(c) for c in calls)


# ===========================================================================
# (b) gh run view returns empty / errors -> fall back to classification-only
# ===========================================================================

def test_sha_scoped_fail_log_empty_falls_back_to_classification(monkeypatch):
    """`gh run view --log-failed` returns empty AND `--log` returns empty ->
    `error` falls back to the classification-only string (today's exact
    behavior), no exception raised."""
    classification = "Test Py3.12: failure"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 99}]))
        if _is_run_view_call(argv):
            # Both --log-failed and --log return empty stdout.
            return _run(stdout="")
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    # No excerpt appended -> exactly the classification (truncated to 300).
    assert result["error"] == classification[:300]


def test_branch_scoped_fail_log_empty_falls_back_to_classification(monkeypatch):
    """Branch-scoped: empty log -> classification-only fallback."""
    classification = "Test Py3.12: fail"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=json.dumps([{"name": "Test Py3.12", "bucket": "fail"}]))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 99}]))
        if _is_run_view_call(argv):
            return _run(stdout="")
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="")

    assert result["state"] == "fail"
    assert result["error"] == classification[:300]


def test_sha_scoped_fail_log_errors_falls_back_to_classification(monkeypatch):
    """`gh run view` returns a non-zero exit (errors) -> fall back to
    classification-only, no exception raised."""
    classification = "Test Py3.12: failure"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 99}]))
        if _is_run_view_call(argv):
            return _run(returncode=1, stdout="", stderr="could not view run")
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert result["error"] == classification[:300]


def test_sha_scoped_fail_run_list_errors_falls_back_to_classification(monkeypatch):
    """`gh run list` (run-id resolution) errors -> fall back to
    classification-only, no exception raised, no crash."""
    classification = "Test Py3.12: failure"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(returncode=1, stdout="", stderr="no runs")
        if _is_run_view_call(argv):
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert result["error"] == classification[:300]


def test_sha_scoped_fail_run_list_empty_falls_back_to_classification(monkeypatch):
    """`gh run list` returns an empty list (no run id) -> fall back to
    classification-only, no crash."""
    classification = "Test Py3.12: failure"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([]))
        if _is_run_view_call(argv):
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert result["error"] == classification[:300]


def test_sha_scoped_fail_run_list_unparseable_falls_back(monkeypatch):
    """`gh run list` returns non-JSON -> fall back to classification-only."""
    classification = "Test Py3.12: failure"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout="not-json-at-all{{{")
        if _is_run_view_call(argv):
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert result["error"] == classification[:300]


def test_sha_scoped_fail_log_oserror_falls_back(monkeypatch):
    """`gh run view` raises OSError -> fall back to classification-only, no
    exception propagates."""
    classification = "Test Py3.12: failure"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 99}]))
        if _is_run_view_call(argv):
            raise OSError("boom")
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert result["error"] == classification[:300]


# ===========================================================================
# (c) cancelled / pending states are unchanged (no enrichment attempted)
# ===========================================================================

def test_sha_scoped_cancelled_not_enriched(monkeypatch):
    """A cancelled state must NOT trigger a `gh run view` call and must keep
    its classification-only error."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(list(argv))
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Build","status":"completed","conclusion":"cancelled"}\n'
            ))
        # If enrichment is attempted on cancelled, this would be reached.
        if _is_run_view_call(argv):
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "cancelled"
    assert "Build" in result["error"]
    # No excerpt leaked in.
    assert _EXCERPT_MARKER not in result["error"]
    # No run-view call was made.
    assert not any(_is_run_view_call(c) for c in calls), (
        "cancelled state must not trigger `gh run view`"
    )


def test_branch_scoped_cancelled_not_enriched(monkeypatch):
    """Branch-scoped cancelled state must not be enriched."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(list(argv))
        if _is_poll_call(argv):
            return _run(stdout=json.dumps([{"name": "Build", "bucket": "cancelled"}]))
        if _is_run_view_call(argv):
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="")

    assert result["state"] == "cancelled"
    assert "Build" in result["error"]
    assert _EXCERPT_MARKER not in result["error"]
    assert not any(_is_run_view_call(c) for c in calls)


def test_sha_scoped_pass_not_enriched(monkeypatch):
    """A pass state must not trigger a `gh run view` call."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(list(argv))
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Lint","status":"completed","conclusion":"success"}\n'
            ))
        if _is_run_view_call(argv):
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result == {"state": "pass", "error": ""}
    assert not any(_is_run_view_call(c) for c in calls)


def test_timeout_pending_not_enriched(monkeypatch):
    """The timeout-pending fallback must remain byte-for-byte unchanged and
    must not attempt enrichment."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(list(argv))
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test","status":"in_progress","conclusion":null}\n'
            ))
        if _is_run_view_call(argv):
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef", timeout_s=0.05)

    assert result["state"] == "pending"
    assert result["error"] == "CI did not complete within timeout"
    assert _EXCERPT_MARKER not in result["error"]
    assert not any(_is_run_view_call(c) for c in calls)


# ===========================================================================
# (d) gh run view hangs past the new timeout -> graceful fallback, no hang
# ===========================================================================

def test_sha_scoped_fail_log_timeout_falls_back(monkeypatch):
    """`gh run view` raises subprocess.TimeoutExpired (simulating a hang past
    the new timeout) -> fall back to classification-only, no hang, no
    exception propagates."""
    classification = "Test Py3.12: failure"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 99}]))
        if _is_run_view_call(argv):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert result["error"] == classification[:300]


def test_branch_scoped_fail_log_timeout_falls_back(monkeypatch):
    """Branch-scoped: `gh run view` times out -> classification-only fallback."""
    classification = "Test Py3.12: fail"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=json.dumps([{"name": "Test Py3.12", "bucket": "fail"}]))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 99}]))
        if _is_run_view_call(argv):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="")

    assert result["state"] == "fail"
    assert result["error"] == classification[:300]


# ===========================================================================
# Bounding: a multi-page log must not be returned wholesale.
# ===========================================================================

def test_sha_scoped_fail_excerpt_is_bounded(monkeypatch):
    """A multi-page `gh run view` log must not be returned wholesale into
    `error`; the appended excerpt must be bounded to roughly 500-1000 chars
    (we assert an upper bound well below the full log size)."""
    # Build a log far larger than any reasonable excerpt bound.
    big_line = "x" * 200 + "\n"
    huge_log = big_line * 200  # 200 * 201 = 40200 chars
    # Embed the marker near the very tail so a tail-slice still catches it.
    huge_log = huge_log + _EXCERPT_MARKER + "\n"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 1}]))
        if _is_run_view_call(argv):
            return _run(stdout=huge_log)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert "Test Py3.12" in result["error"]
    # The marker (at the tail) must still be present -- proves a tail slice.
    assert _EXCERPT_MARKER in result["error"]
    # The whole error must be far smaller than the full 40k log.  We allow up
    # to 2000 chars (classification + a ~500-1000 char excerpt + slack) but
    # absolutely not the wholesale 40k.
    assert len(result["error"]) < 4000, (
        f"excerpt not bounded: error is {len(result['error'])} chars"
    )


def test_branch_scoped_fail_excerpt_is_bounded(monkeypatch):
    """Branch-scoped: a multi-page log must be bounded in the appended
    excerpt."""
    big_line = "y" * 200 + "\n"
    huge_log = big_line * 200 + _EXCERPT_MARKER + "\n"

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=json.dumps([{"name": "Test Py3.12", "bucket": "fail"}]))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 1}]))
        if _is_run_view_call(argv):
            return _run(stdout=huge_log)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="")

    assert result["state"] == "fail"
    assert "Test Py3.12" in result["error"]
    assert _EXCERPT_MARKER in result["error"]
    assert len(result["error"]) < 4000


# ===========================================================================
# --log-failed fallback to --log: when --log-failed errors/empty, --log is
# tried and its excerpt is used.
# ===========================================================================

def test_sha_scoped_fail_falls_back_from_log_failed_to_log(monkeypatch):
    """When `gh run view --log-failed` errors (non-zero) but `--log` succeeds,
    the excerpt from `--log` is used."""
    calls = []

    def _fake_run(argv, **_):
        calls.append(list(argv))
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 5}]))
        if _is_run_view_call(argv):
            if "--log-failed" in argv:
                return _run(returncode=1, stdout="", stderr="no failed logs")
            if "--log" in argv:
                return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert "Test Py3.12" in result["error"]
    assert _EXCERPT_MARKER in result["error"]
    # Both view variants were attempted.
    assert any("--log-failed" in c for c in calls if _is_run_view_call(c))
    assert any("--log" in c for c in calls if _is_run_view_call(c))


def test_sha_scoped_fail_falls_back_from_empty_log_failed_to_log(monkeypatch):
    """When `gh run view --log-failed` returns EMPTY stdout (but exit 0), the
    implementation falls back to `--log` and uses its excerpt."""
    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 5}]))
        if _is_run_view_call(argv):
            if "--log-failed" in argv:
                return _run(returncode=0, stdout="")
            if "--log" in argv:
                return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    result = p._ci_status("agent/x", sha="deadbeef")

    assert result["state"] == "fail"
    assert _EXCERPT_MARKER in result["error"]


# ===========================================================================
# run-id resolution: `gh run list --branch <branch> --limit 1 --json databaseId`
# ===========================================================================

def test_sha_scoped_fail_uses_gh_run_list_to_resolve_run_id(monkeypatch):
    """The implementation resolves the failed run id via `gh run list --branch
    <branch> --limit 1 --json databaseId` (or reuses an id already present in
    the poll response).  At minimum, a `gh run view <run_id>` call must carry
    the run id we returned from `gh run list`."""
    seen_run_view = []

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 123456}]))
        if _is_run_view_call(argv):
            seen_run_view.append(list(argv))
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    p._ci_status("agent/x", sha="deadbeef")

    assert seen_run_view, "expected a `gh run view` call"
    # The run id from `gh run list` (123456) must appear as an argument.
    assert any("123456" in c for c in seen_run_view), (
        f"`gh run view` did not carry the resolved run id: {seen_run_view}"
    )


def test_branch_scoped_fail_uses_gh_run_list_to_resolve_run_id(monkeypatch):
    """Branch-scoped path also resolves the run id via `gh run list`."""
    seen_run_view = []

    def _fake_run(argv, **_):
        if _is_poll_call(argv):
            return _run(stdout=json.dumps([{"name": "Test Py3.12", "bucket": "fail"}]))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 654321}]))
        if _is_run_view_call(argv):
            seen_run_view.append(list(argv))
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    p._ci_status("agent/x", sha="")

    assert seen_run_view
    assert any("654321" in c for c in seen_run_view)


# ===========================================================================
# Timeout on the new `gh run view` call: a timeout kwarg must be passed so a
# stuck `gh` cannot hang the merge gate.
# ===========================================================================

def test_sha_scoped_fail_run_view_passes_a_timeout(monkeypatch):
    """The new `gh run view` subprocess call must be invoked with a `timeout`
    kwarg (bounded, e.g. 15-30s) so a stuck `gh` cannot hang the gate."""
    view_kwargs = []

    def _fake_run(argv, **kw):
        if _is_poll_call(argv):
            return _run(stdout=(
                '{"name":"Test Py3.12","status":"completed","conclusion":"failure"}\n'
            ))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 1}]))
        if _is_run_view_call(argv):
            view_kwargs.append(kw)
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    p._ci_status("agent/x", sha="deadbeef")

    assert view_kwargs, "expected a `gh run view` call"
    timeouts = [kw.get("timeout") for kw in view_kwargs if kw.get("timeout") is not None]
    assert timeouts, "`gh run view` was called without a timeout kwarg"
    for t in timeouts:
        # Bounded to the 15-30s range described in the task (allow some slack).
        assert isinstance(t, (int, float))
        assert 1 <= t <= 60, f"timeout {t} not in a sane bounded range"


def test_branch_scoped_fail_run_view_passes_a_timeout(monkeypatch):
    """Branch-scoped path: `gh run view` must also pass a timeout kwarg."""
    view_kwargs = []

    def _fake_run(argv, **kw):
        if _is_poll_call(argv):
            return _run(stdout=json.dumps([{"name": "Test Py3.12", "bucket": "fail"}]))
        if _is_run_list_call(argv):
            return _run(stdout=json.dumps([{"databaseId": 1}]))
        if _is_run_view_call(argv):
            view_kwargs.append(kw)
            return _run(stdout=_PYTEST_LOG)
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _set_gate(monkeypatch)
    p._ci_status("agent/x", sha="")

    assert view_kwargs
    timeouts = [kw.get("timeout") for kw in view_kwargs if kw.get("timeout") is not None]
    assert timeouts
    for t in timeouts:
        assert isinstance(t, (int, float))
        assert 1 <= t <= 60