"""HR-05: a dispatch that died on an LLM transport failure before any tool
step is retried without charging the rework budget."""
import json

from pipeline import server as p
from pipeline.no_progress import classify_no_progress, is_transport_failure
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
)

BOOT = "[boot] model=x\n"
FAIL_429 = "[step 0] LLM call failed: Client error '429 Too Many Requests' for url 'http://h/x'\n"
TOOL_STEPS = "[step 1] bash: pytest -q\n[step 2] view_file: a.py\n"


def _setup(tmp_path, monkeypatch, log, **story_extra):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(log)
    story = {"summary": "s", "status": "tests_passed", "rework_attempts": 0, **story_extra}
    manifest = {"epics": {}, "stories": {"S1": story}}
    path = tmp_path / "plan.manifest.json"
    path.write_text(json.dumps(manifest))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: notes.append((a, k)))
    return story, manifest, path, worktree, notes


def _run(story, worktree, manifest, path):
    return classify_no_progress("plan", "S1", story, str(worktree), manifest, path)


def test_is_transport_failure_true_for_429_at_step_zero():
    assert is_transport_failure(BOOT + FAIL_429)


def test_is_transport_failure_matches_each_token():
    for tok in ("500 Internal", "503 Service", "timed out", "ConnectError", "ConnectionError"):
        assert is_transport_failure(f"{BOOT}[step 0] LLM call failed: {tok}\n")


def test_is_transport_failure_false_for_other_error():
    assert not is_transport_failure(BOOT + "[step 0] LLM call failed: bad json\n")


def test_is_transport_failure_false_when_tool_steps_follow():
    assert not is_transport_failure(BOOT + FAIL_429 + TOOL_STEPS)


def test_is_transport_failure_false_when_tool_steps_precede():
    assert not is_transport_failure(BOOT + TOOL_STEPS + FAIL_429)


def test_is_transport_failure_ignores_prior_segment_failure():
    assert not is_transport_failure(BOOT + FAIL_429 + BOOT + TOOL_STEPS)


def test_is_transport_failure_false_for_empty_log():
    assert not is_transport_failure("")


def test_retry_does_not_charge_rework(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, BOOT + FAIL_429)
    result = _run(story, wt, manifest, path)
    assert result == {"status": "changes_requested", "reason": "transport_failure"}
    assert story["rework_attempts"] == 0
    assert story["transport_retries"] == 1
    assert story["status"] == "changes_requested"
    assert json.loads(path.read_text())["stories"]["S1"]["transport_retries"] == 1


def test_retry_notifies_once_without_url(tmp_path, monkeypatch):
    story, manifest, path, wt, notes = _setup(tmp_path, monkeypatch, BOOT + FAIL_429)
    _run(story, wt, manifest, path)
    assert len(notes) == 1
    assert notes[0][1]["event"] == "transport_retry"
    assert "http" not in notes[0][0][1]


def test_real_steps_after_failure_not_transport(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, BOOT + FAIL_429 + TOOL_STEPS)
    assert _run(story, wt, manifest, path) is None
    assert "transport_retries" not in story


def test_prior_segment_failure_not_transport(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(
        tmp_path, monkeypatch, BOOT + FAIL_429 + BOOT + TOOL_STEPS
    )
    assert _run(story, wt, manifest, path) is None


def test_fifth_retry_parks_with_accurate_reason(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(
        tmp_path, monkeypatch, BOOT + FAIL_429, transport_retries=4
    )
    result = _run(story, wt, manifest, path)
    assert result == {"status": "parked", "reason": "transport_failure_exhausted"}
    assert story["status"] == "parked"
    assert story["parked_reason"] == (
        "transport failure: 5 consecutive dispatches never reached the model"
    )
    assert story["rework_attempts"] == 0


def test_scope_conflict_takes_precedence(tmp_path, monkeypatch):
    log = BOOT + FAIL_429 + "[step 4] DONE: SCOPE-CONFLICT: pipeline/a.py :: why\n"
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, log)
    assert _run(story, wt, manifest, path) == {"status": "parked", "reason": "scope_conflict"}
    assert "transport_retries" not in story


def test_clean_log_returns_none(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, BOOT + TOOL_STEPS)
    assert _run(story, wt, manifest, path) is None
