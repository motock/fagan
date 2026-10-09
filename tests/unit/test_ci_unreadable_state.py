"""CIGATE-2: an unreadable CI status must park the merge, not read as "no CI".

``_ci_status`` / ``_ci_status_once`` return ``unreadable`` when ``gh`` fails
(or its output cannot be parsed) in a repo that declares workflows, and keep
``none`` for repos without workflows. Both merge gates refuse to merge on
``unreadable``. The CI state vocabulary is cumulative: these tests assert only
the behaviour of ``unreadable`` / ``none``, never the full set of states.
"""

import json

import pytest

import pipeline.ci as pci
import pipeline.server as p
from pipeline import merge
from pipeline.server import _advance_pipeline_locked


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def repo_with_ci(tmp_path, monkeypatch):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pci, "PIPELINE_MERGE_CI_GATE", True)
    return tmp_path


@pytest.fixture
def repo_without_ci(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pci, "PIPELINE_MERGE_CI_GATE", True)
    return tmp_path


def _stub_gh(monkeypatch, *, failure):
    def fake_run(cmd, **kwargs):
        if failure == "nonzero":
            return _Completed(returncode=1, stderr="HTTP 502 from gh")
        if failure == "oserror":
            raise FileNotFoundError("gh")
        if failure == "badjson":
            return _Completed(stdout="{not json")
        if failure == "empty":
            return _Completed(stdout="[]")
        raise AssertionError(failure)

    monkeypatch.setattr(pci.subprocess, "run", fake_run)


def _call(fn, sha):
    kwargs = {"timeout_s": 5} if fn is pci._ci_status else {}
    return fn("agent/x", sha=sha, **kwargs)


FUNCS = [pci._ci_status, pci._ci_status_once]
FAILURES = ["nonzero", "oserror", "badjson"]


@pytest.mark.parametrize("sha", ["", "abc123"])
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("fn", FUNCS)
def test_should_report_unreadable_when_gh_fails_and_workflows_exist(
    fn, failure, sha, repo_with_ci, monkeypatch
):
    _stub_gh(monkeypatch, failure=failure)
    ci = _call(fn, sha)
    assert ci["state"] == "unreadable"
    assert ci["error"]


@pytest.mark.parametrize("sha", ["", "abc123"])
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("fn", FUNCS)
def test_should_keep_none_when_gh_fails_and_no_workflows(
    fn, failure, sha, repo_without_ci, monkeypatch
):
    _stub_gh(monkeypatch, failure=failure)
    ci = _call(fn, sha)
    assert ci["state"] == "none"
    assert ci["error"]


def test_should_preserve_error_text_for_nonzero_exit(repo_with_ci, monkeypatch):
    _stub_gh(monkeypatch, failure="nonzero")
    assert pci._ci_status_once("b", sha="")["error"] == "HTTP 502 from gh"


def test_should_stay_pending_on_empty_result_with_workflows(repo_with_ci, monkeypatch):
    _stub_gh(monkeypatch, failure="empty")
    assert pci._ci_status_once("b", sha="")["state"] == "pending"


def test_should_stay_none_on_empty_result_without_workflows(
    repo_without_ci, monkeypatch
):
    _stub_gh(monkeypatch, failure="empty")
    assert pci._ci_status_once("b", sha="")["state"] == "none"


def test_should_stay_none_on_empty_result_without_workflows_blocking(
    repo_without_ci, monkeypatch
):
    _stub_gh(monkeypatch, failure="empty")
    assert pci._ci_status("b", sha="", timeout_s=5)["state"] == "none"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
def _manifest(plan_dir, stories):
    (plan_dir / "go.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories})
    )


def _read(plan_dir):
    return json.loads((plan_dir / "go.manifest.json").read_text())["stories"]["P1"]


def _story(**overrides):
    base = {
        "summary": "ready pr",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": "/nonexistent-worktree",
    }
    base.update(overrides)
    return base


@pytest.fixture
def gate_boundaries(monkeypatch):
    merged = []
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_reverify_acceptance", lambda s, w, k: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_build", lambda w: {"state": "pass"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_rebase_onto_master", lambda wt, br: {"ok": True, "error": ""})
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
    if hasattr(p, "_maybe_record_retro"):
        monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)
    return merged


def test_should_refuse_manual_merge_when_ci_unreadable(
    plan_dir, gate_boundaries, monkeypatch
):
    monkeypatch.setattr(
        p, "_ci_status", lambda *a, **k: {"state": "unreadable", "error": "boom"}
    )
    _manifest(plan_dir, {"P1": _story(status="parked")})

    result = merge._approve_merge_impl("go", "P1")

    assert result["ok"] is False
    assert result["error"].startswith("ci unreadable:")
    assert "boom" in result["error"]
    assert gate_boundaries == []


def test_should_merge_manually_when_ci_none(plan_dir, gate_boundaries, monkeypatch):
    monkeypatch.setattr(p, "_ci_status", lambda *a, **k: {"state": "none", "error": ""})
    _manifest(plan_dir, {"P1": _story(status="parked")})

    result = merge._approve_merge_impl("go", "P1")

    assert result.get("ok") is True
    assert gate_boundaries == ["P1"]


def test_should_park_scheduler_gate_attempt_when_ci_unreadable(
    plan_dir, gate_boundaries, monkeypatch
):
    monkeypatch.setattr(
        p, "_merge_gate_ci_status",
        lambda *a, **k: {"state": "unreadable", "error": "boom"},
    )
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    _manifest(plan_dir, {"P1": _story()})

    _advance_pipeline_locked("go")
    story = _read(plan_dir)

    assert gate_boundaries == []
    assert story["status"] != "done"
    assert story["merge_attempts"] == 1
    # Not a definitive CI failure: no rework routing even with rework enabled.
    assert story["status"] != "changes_requested"
    assert not story.get("ci_rework")


def test_should_record_unreadable_gate_error_when_attempts_exhausted(
    plan_dir, gate_boundaries, monkeypatch
):
    monkeypatch.setattr(
        p, "_merge_gate_ci_status",
        lambda *a, **k: {"state": "unreadable", "error": "boom"},
    )
    _manifest(plan_dir, {"P1": _story(merge_attempts=p.MERGE_MAX_ATTEMPTS - 1)})

    _advance_pipeline_locked("go")
    story = _read(plan_dir)

    assert story["status"] == "failed"
    assert story["merge_error"].startswith("ci unreadable:")
    assert gate_boundaries == []


def test_should_merge_scheduler_gate_when_ci_none(
    plan_dir, gate_boundaries, monkeypatch
):
    monkeypatch.setattr(
        p, "_merge_gate_ci_status", lambda *a, **k: {"state": "none", "error": ""}
    )
    _manifest(plan_dir, {"P1": _story()})

    _advance_pipeline_locked("go")

    assert gate_boundaries == ["P1"]
