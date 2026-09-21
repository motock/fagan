"""OA2-07: ``request_decision`` must fail open when the overlord backend errors.

Today an ``_invoke_overlord`` exception propagates to the calling agent as a raw
tool error. The required behaviour mirrors ``triage.rule_on_story``'s fail-open:
on exception, (1) append a decisions-log record with ``action == "park_for_human"``
and a SHORT exception summary, (2) park the story with a ``parked_reason`` naming
the overlord failure, (3) return an actionable single-line message instead of
raising.

Security: the decisions record and ``parked_reason`` must carry only the
exception class name - never a traceback, file path or payload data.
"""
import json

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
)
from tests.unit._pipeline_mcp_server_test_helpers import (
    agents_dir as _agents_dir_fixture,
)
from tests.unit._pipeline_mcp_server_test_helpers import (
    plan_dir as _plan_dir_fixture,
)

# Re-export the shared fixtures under their canonical names so pytest registers
# them for this module. Assigning (rather than importing) the names keeps ruff's
# F811 quiet: it flags an *imported* name reused as a test-function parameter
# (see the per-file-ignores note in pyproject.toml), which is the standard
# fixture-sharing false positive.
plan_dir = _plan_dir_fixture
agents_dir = _agents_dir_fixture

PLAN = "oa2plan"
STORY = "S-1"
MESSAGE = "decision escalated to human: story parked, see decisions log"

CANNED = (
    "RULING: Use the existing http client; do not add a new dependency.\n"
    "TIER: routine\n"
    "RISK: low\n"
    "RATIONALE: The stack already includes httpx; adding requests duplicates it.\n"
    "NOTIFY_USER: no\n"
)


def _raiser(exc):
    def _boom(prompt, **kwargs):
        raise exc
    return _boom


def _seed_story(pdir, status="in_review", **extra):
    story = {"summary": "s", "status": status, **extra}
    _write_manifest(pdir, PLAN, {STORY: story})


def _decisions(pdir):
    path = pdir / f"{PLAN}.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


# ---------- (a) overlord raises -> park + record + message, no exception ----------
def test_overlord_error_parks_story_and_returns_message(plan_dir, agents_dir, monkeypatch):
    _seed_story(plan_dir)
    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError("overlord timeout after 30s")))

    result = p.request_decision(PLAN, STORY, "q", ["a", "b"])

    assert isinstance(result, str)
    assert result == MESSAGE
    story = _read_manifest(plan_dir, PLAN)["stories"][STORY]
    assert story["status"] == "parked"
    assert story["parked_reason"] == "overlord failure: RuntimeError"
    log = _decisions(plan_dir)
    assert len(log) == 1
    assert log[0]["story_key"] == STORY
    assert log[0]["action"] == "park_for_human"
    assert log[0]["summary"] == "RuntimeError"


# ---------- (b) success path is completely unaffected ----------
def test_success_path_return_and_record_unchanged(plan_dir, agents_dir, monkeypatch):
    _seed_story(plan_dir)
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: CANNED)

    result = p.request_decision(PLAN, STORY, "Should I add requests?", ["add", "reuse"])

    decided_at = result.pop("decided_at")
    assert isinstance(decided_at, str) and decided_at
    assert result == {
        "story_key": STORY,
        "question": "Should I add requests?",
        "options": ["add", "reuse"],
        "ruling": "Use the existing http client; do not add a new dependency.",
        "tier": "routine",
        "risk": "low",
        "rationale": "The stack already includes httpx; adding requests duplicates it.",
        "action": "park_for_human",
        "notify_user": False,
        "split": [],
        "decided_by": "overlord",
    }
    log = _decisions(plan_dir)
    assert len(log) == 1
    assert log[0] == {**result, "decided_at": decided_at}
    # The success path must not park the story.
    assert _read_manifest(plan_dir, PLAN)["stories"][STORY]["status"] == "in_review"


# ---------- (c) security: no traceback / paths / payload in the record ----------
def test_fail_open_record_leaks_no_sensitive_data(plan_dir, agents_dir, monkeypatch):
    _seed_story(plan_dir)
    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError("/secret/path/payload.json")))

    p.request_decision(PLAN, STORY, "q", ["a"])

    story = _read_manifest(plan_dir, PLAN)["stories"][STORY]
    blob = json.dumps(_decisions(plan_dir))
    for text in (blob, story["parked_reason"]):
        assert "RuntimeError" in text
        for leak in ("/secret/path", "payload.json", "Traceback", 'File "', ".py"):
            assert leak not in text


# ---------- (d) already-parked story does not crash the fail-open path ----------
def test_already_parked_story_does_not_crash_fail_open(plan_dir, agents_dir, monkeypatch):
    _seed_story(plan_dir, status="parked", parked_reason="prior human park")
    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError()))

    result = p.request_decision(PLAN, STORY, "q", ["a"])

    assert result == MESSAGE
    story = _read_manifest(plan_dir, PLAN)["stories"][STORY]
    assert story["status"] == "parked"
    log = _decisions(plan_dir)
    assert log[-1]["action"] == "park_for_human"
    assert log[-1]["summary"] == "RuntimeError"


# ---------- (e) message is a single short line ----------
def test_fail_open_message_is_single_short_line(plan_dir, agents_dir, monkeypatch):
    _seed_story(plan_dir)
    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError("x")))

    result = p.request_decision(PLAN, STORY, "q", ["a"])

    assert isinstance(result, str)
    assert "\n" not in result
    assert len(result) < 120


# ---------- (f) repeated failures append, never wipe, the decisions log ----------
def test_second_failure_appends_and_keeps_story_parked(plan_dir, agents_dir, monkeypatch):
    _seed_story(plan_dir)
    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError("overlord timeout after 30s")))
    assert p.request_decision(PLAN, STORY, "q", ["a"]) == MESSAGE

    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError()))
    assert p.request_decision(PLAN, STORY, "q", ["a"]) == MESSAGE

    log = _decisions(plan_dir)
    assert len(log) == 2
    assert [r["action"] for r in log] == ["park_for_human", "park_for_human"]
    assert log[1]["summary"] == "RuntimeError"
    story = _read_manifest(plan_dir, PLAN)["stories"][STORY]
    assert story["status"] == "parked"
    assert story["parked_reason"] == "overlord failure: RuntimeError"
