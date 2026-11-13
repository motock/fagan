"""Overlord executors ``retry_transient`` and ``rebrief`` (ov-2).

``retry_transient`` unparks a transient bookkeeping park only when live git
and forge evidence confirms the branch and an OPEN PR. ``rebrief`` records the
overlord's ``DIAGNOSIS:`` line in runtime state that ``canonical_digest`` does
not hash, so the next dispatch can be told what went wrong without an
authored-field edit that would reset plan approval.

Every test points ``PLAN_DIR`` at ``tmp_path`` (same patch as ``test_unpark.py``)
so ``FileStore`` never writes into the real ``plans/``. Git and forge calls are
stubbed at their module-level seams. Parks mutate the in-memory story (the
sweep persists it), so park assertions read that story; only the unpark path
writes through the store.
"""

from __future__ import annotations

import pytest

import pipeline.server as p
from pipeline import persistence as ppers
from pipeline import plan_approval, triage
from pipeline import triage_rebrief as tr
from pipeline import unpark as unpark_mod
from pipeline.branch_guard import BranchGuardError
from pipeline.store import FileStore

_PLAN = "ov"
_STORY = "ov-2"
_PR_URL = "https://github.com/motock/fagan/pull/9"
_BRANCH = "agent/ov-2"
_TRANSIENT_REASON = "branch head unavailable (got None)"
_NO_COMMIT_REASON = "no new commit after 2 rework redispatches"
_REBASE_REASON = "rebase conflict: CONFLICT (content): Merge conflict in pipeline/x.py"
_MISSING = object()


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every ``PLAN_DIR`` binding at ``tmp_path``."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


@pytest.fixture
def notices(monkeypatch):
    """Capture the executors' notifications; silence the park notifier."""
    sent: list[dict] = []

    def fake(plan_name, message, **kwargs):
        sent.append({"plan": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(tr, "_notify_user", fake)
    monkeypatch.setattr(triage, "_notify_user", lambda *args, **kwargs: None)
    return sent


@pytest.fixture
def live(monkeypatch):
    """Stub live evidence: branch on origin and the PR state. Defaults to confirmed."""
    evidence = {"branch": True, "pr_state": "OPEN"}
    monkeypatch.setattr(tr, "_branch_on_origin", lambda repo, branch: evidence["branch"])
    monkeypatch.setattr(tr, "_pr_state", lambda pr_url: evidence["pr_state"])
    return evidence


def _story(**extra) -> dict:
    story = {
        "status": "parked",
        "summary": "Retry a transient park",
        "branch": _BRANCH,
        "pr_url": _PR_URL,
        "agent_instructions": "Do the thing.",
        "files": ["pipeline/x.py"],
        "parked_reason": _TRANSIENT_REASON,
        "rework_attempts": 2,
        "triage_attempts": 1,
        "triage_actions": ["retry_transient"],
    }
    story.update(extra)
    return story


def _write(story: dict) -> dict:
    manifest = {"plan": _PLAN, "stories": {_STORY: story}}
    FileStore().save_manifest(_PLAN, manifest)
    return manifest


def _persisted() -> dict:
    return FileStore().get_manifest(_PLAN)["stories"][_STORY]


def _retry(story: dict) -> str:
    manifest = {"plan": _PLAN, "stories": {_STORY: story}}
    return tr.execute_retry_transient(_PLAN, _STORY, story, {"action": "retry_transient"}, manifest)


def _rebrief(story: dict, diagnosis=_MISSING, manifest: dict | None = None) -> str:
    manifest = manifest if manifest is not None else {"plan": _PLAN, "stories": {_STORY: story}}
    ruling = {"action": "rebrief"}
    if diagnosis is not _MISSING:
        ruling["diagnosis"] = diagnosis
    return tr.execute_rebrief(_PLAN, _STORY, story, ruling, manifest)


def _unparked(notices: list[dict]) -> list[dict]:
    return [n for n in notices if n.get("event") == "story_unparked_by_overlord"]


# ---------------------------------------------------------------------------
# retry_transient
# ---------------------------------------------------------------------------


def test_retry_transient_unparks_when_branch_and_pr_are_live(plan_dir, notices, live):
    story = _story()
    _write(story)

    result = _retry(story)

    assert result == "retry_transient"
    assert story["status"] == "tests_passed"
    assert "parked_reason" not in story
    persisted = _persisted()
    assert persisted["status"] == "tests_passed"
    assert "parked_reason" not in persisted


def test_retry_transient_records_one_unpark_event(plan_dir, notices, live):
    story = _story()
    _write(story)

    _retry(story)

    assert len(_unparked(notices)) == 1


def test_retry_transient_parks_with_a_reason_when_branch_is_gone(plan_dir, notices, live):
    live["branch"] = False
    story = _story()
    _write(story)

    result = _retry(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert "no longer on origin" in story["parked_reason"]
    assert _unparked(notices) == []
    assert _persisted()["status"] == "parked"


def test_retry_transient_parks_when_branch_query_fails(plan_dir, notices, monkeypatch):
    def raise_guard(repo, branch):
        raise BranchGuardError("could not query origin")

    monkeypatch.setattr(tr, "_branch_on_origin", raise_guard)
    monkeypatch.setattr(tr, "_pr_state", lambda pr_url: "OPEN")
    story = _story()
    _write(story)

    assert _retry(story) == "park_for_human"
    assert story["status"] == "parked"
    assert _unparked(notices) == []


@pytest.mark.parametrize("pr_state", ["CLOSED", "UNKNOWN"])
def test_retry_transient_does_not_retry_when_pr_is_not_open(plan_dir, notices, live, pr_state):
    live["pr_state"] = pr_state
    story = _story()
    _write(story)

    assert _retry(story) == "park_for_human"
    assert story["status"] == "parked"
    assert _unparked(notices) == []


def test_retry_transient_declines_merged_pr_and_leaves_it_for_mark_done(plan_dir, notices, live):
    """A ``branch head unavailable`` park on a MERGED PR is not retried.

    The reason is rewritten so it no longer classifies as transient, which is
    what lets ``mark_done`` be ruled on the story.
    """
    live["pr_state"] = "MERGED"
    story = _story(parked_reason=_TRANSIENT_REASON)
    _write(story)

    result = _retry(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert "MERGED" in story["parked_reason"]
    assert _unparked(notices) == []
    assert _persisted()["status"] == "parked"


def test_retry_transient_refuses_a_story_without_a_pr_url(plan_dir, notices, live):
    story = _story(pr_url=None)
    _write(story)

    assert _retry(story) == "park_for_human"
    assert story["status"] == "parked"
    assert _unparked(notices) == []


def test_retry_transient_refuses_a_park_outside_transient_bookkeeping(plan_dir, notices, live):
    story = _story(parked_reason=_REBASE_REASON)
    _write(story)

    assert _retry(story) == "park_for_human"
    assert story["status"] == "parked"
    assert _unparked(notices) == []


def test_retry_transient_allows_the_second_retry_and_counts_it(plan_dir, notices, live):
    story = _story(triage_transient_retries=1)
    _write(story)

    assert _retry(story) == "retry_transient"
    assert story["triage_transient_retries"] == 2


def test_third_retry_parks_for_a_human_without_unparking(plan_dir, notices, live):
    story = _story(triage_transient_retries=2)
    _write(story)

    result = _retry(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["triage_transient_retries"] == 2
    assert _unparked(notices) == []
    assert _persisted()["status"] == "parked"


# ---------------------------------------------------------------------------
# rebrief
# ---------------------------------------------------------------------------


def test_rebrief_stores_the_diagnosis_with_class_and_timestamp(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    _write(story)

    result = _rebrief(story, "The agent edited the wrong module. Point it at pipeline/x.py.")

    assert result == "rebrief"
    entry = story["triage_diagnosis"][-1]
    assert entry["text"] == "The agent edited the wrong module. Point it at pipeline/x.py."
    assert entry["class"] == "no_commit_after_rework"
    assert isinstance(entry["ts"], str) and entry["ts"]


def test_rebrief_sets_status_to_todo(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    _write(story)

    _rebrief(story, "Retry from the start.")

    assert story["status"] == "todo"


def test_rebrief_truncates_a_diagnosis_longer_than_600_characters(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    _write(story)

    _rebrief(story, "a" * 601)

    assert len(story["triage_diagnosis"][-1]["text"]) == 600
    assert any("truncated" in n["message"] for n in notices)


def test_rebrief_keeps_a_600_character_diagnosis_whole(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    _write(story)

    _rebrief(story, "b" * 600)

    assert story["triage_diagnosis"][-1]["text"] == "b" * 600
    assert not any("truncated" in n["message"] for n in notices)


def test_rebrief_caps_a_multi_paragraph_diagnosis_at_600_characters(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    _write(story)

    _rebrief(story, "First paragraph.\n\n" + "c" * 700 + "\n\nThird.")

    entry = story["triage_diagnosis"][-1]
    assert len(entry["text"]) == 600
    assert "\n" not in entry["text"]


def test_rebrief_keeps_only_the_last_three_diagnoses(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    _write(story)

    for text in ["one", "two", "three", "four"]:
        _rebrief(story, text)

    assert [entry["text"] for entry in story["triage_diagnosis"]] == ["two", "three", "four"]


def test_rebrief_resets_only_the_retry_counters(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON, ci_rework=1, merge_attempts=3)
    _write(story)

    _rebrief(story, "Retry.")

    assert "rework_attempts" not in story
    assert "ci_rework" not in story
    assert "merge_attempts" not in story
    assert story["triage_attempts"] == 1
    assert story["triage_actions"] == ["retry_transient"]


def test_rebrief_counter_reset_fields_are_all_unparkable():
    assert tr.REBRIEF_RESET_FIELDS <= unpark_mod.UNPARKABLE_FIELDS


def test_rebrief_never_writes_authored_fields_and_leaves_the_digest_unchanged(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    manifest = _write(story)
    digest_before = plan_approval.canonical_digest(manifest)
    instructions_before = story["agent_instructions"]
    files_before = list(story["files"])

    _rebrief(story, "Read the brief again and commit early.", manifest)

    assert story["agent_instructions"] == instructions_before
    assert story["files"] == files_before
    assert plan_approval.canonical_digest(manifest) == digest_before


def test_rebrief_diagnosis_persists_without_changing_the_digest(plan_dir, notices):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    manifest = _write(story)
    digest_before = plan_approval.canonical_digest(manifest)

    _rebrief(story, "Commit before running the suite.", manifest)
    FileStore().save_manifest(_PLAN, manifest)

    persisted = _persisted()
    assert persisted["triage_diagnosis"][-1]["text"] == "Commit before running the suite."
    assert plan_approval.canonical_digest(FileStore().get_manifest(_PLAN)) == digest_before


@pytest.mark.parametrize("diagnosis", [_MISSING, None, "", "   \n\n  ", 42])
def test_rebrief_parks_when_the_diagnosis_is_absent_or_unusable(plan_dir, notices, diagnosis):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    _write(story)

    result = _rebrief(story, diagnosis)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert "triage_diagnosis" not in story


def test_rebrief_refuses_a_park_class_that_does_not_allow_it(plan_dir, notices):
    story = _story(parked_reason=_REBASE_REASON)
    _write(story)

    result = _rebrief(story, "Retry.")

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert "triage_diagnosis" not in story


# ---------------------------------------------------------------------------
# diagnosis parsing and the dispatch block
# ---------------------------------------------------------------------------


def test_parse_diagnosis_reads_the_diagnosis_line():
    text = "RULING: retry\nDIAGNOSIS: Commit the WIP first.\nACTION: rebrief\n"

    assert tr.parse_diagnosis(text) == "Commit the WIP first."


@pytest.mark.parametrize("text", ["RULING: retry\nACTION: rebrief", "DIAGNOSIS:    \n", ""])
def test_parse_diagnosis_returns_none_when_absent_or_blank(text):
    assert tr.parse_diagnosis(text) is None


def test_parse_diagnosis_takes_the_first_line_only():
    text = "DIAGNOSIS: first\nDIAGNOSIS: second"

    assert tr.parse_diagnosis(text) == "first"


def test_diagnosis_block_is_empty_without_a_diagnosis():
    assert tr.diagnosis_block({"dispatch_attempts": 2}) == ""
    assert tr.diagnosis_block({"dispatch_attempts": 2, "triage_diagnosis": []}) == ""


def test_diagnosis_block_renders_the_latest_entry_with_the_attempt_number():
    story = {
        "dispatch_attempts": 3,
        "triage_diagnosis": [
            {"ts": "t1", "class": "no_commit_after_rework", "text": "old"},
            {"ts": "t2", "class": "no_commit_after_rework", "text": "Commit early."},
        ],
    }

    assert tr.diagnosis_block(story) == "OVERLORD DIAGNOSIS (attempt 3): Commit early.\n\n"


def test_diagnosis_block_ignores_a_malformed_entry():
    assert tr.diagnosis_block({"dispatch_attempts": 1, "triage_diagnosis": ["not a dict"]}) == ""


# ---------------------------------------------------------------------------
# shadow mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["retry_transient", "rebrief"])
def test_executors_are_inert_in_shadow_mode(plan_dir, notices, live, monkeypatch, action):
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    reason = _NO_COMMIT_REASON if action == "rebrief" else _TRANSIENT_REASON
    story = _story(parked_reason=reason)
    manifest = _write(story)
    before = dict(story)
    recorded: list[dict] = []
    monkeypatch.setattr(triage, "_append_decision", lambda plan, record: recorded.append(record))
    ruling = {"action": action, "diagnosis": "Retry.", "rationale": "shadow"}

    result = triage._apply_ruling_for_mode(_PLAN, _STORY, story, ruling, manifest)

    assert result == "shadow"
    assert story == before
    assert recorded[-1]["mode"] == "shadow"


# ---------------------------------------------------------------------------
# wiring: the registry and the live ruling path (execute mode)
# ---------------------------------------------------------------------------


@pytest.fixture
def execute_mode(monkeypatch):
    """Route rulings the way a live ``PIPELINE_TRIAGE_MODE=execute`` sweep does."""
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")


@pytest.fixture
def decisions(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(triage, "_append_decision", lambda plan, record: recorded.append(record))
    return recorded


def _overlord_raw(action: str, diagnosis: str | None = None) -> str:
    text = (
        "RULING: r\nTIER: hard\nRISK: low\nRATIONALE: why\nNOTIFY_USER: no\n"
        f"ACTION: {action}\n"
    )
    if diagnosis is not None:
        text += f"DIAGNOSIS: {diagnosis}\n"
    return text


def test_registry_maps_retry_transient_and_rebrief_to_the_real_executors():
    from pipeline import triage_executors

    assert triage_executors.EXECUTORS["retry_transient"] is tr.execute_retry_transient
    assert triage_executors.EXECUTORS["rebrief"] is tr.execute_rebrief


def test_execute_mode_retry_transient_unparks_through_the_ruling_path(
    plan_dir, notices, live, decisions, execute_mode
):
    story = _story()
    manifest = _write(story)

    result = triage._apply_ruling_for_mode(
        _PLAN, _STORY, story, {"action": "retry_transient", "rationale": "r"}, manifest
    )

    assert result == "retry_transient"
    assert _persisted()["status"] == "tests_passed"
    assert decisions[-1]["mode"] == "full"


def test_execute_mode_rebrief_stores_the_diagnosis_through_the_ruling_path(
    plan_dir, notices, decisions, execute_mode
):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    manifest = _write(story)
    ruling = {"action": "rebrief", "diagnosis": "The brief names the wrong file.", "rationale": "r"}

    result = triage._apply_ruling_for_mode(_PLAN, _STORY, story, ruling, manifest)

    assert result == "rebrief"
    assert story["status"] == "todo"
    assert story["triage_diagnosis"][-1]["text"] == "The brief names the wrong file."


def test_execute_mode_rebrief_without_a_diagnosis_parks_for_a_human(
    plan_dir, notices, decisions, execute_mode
):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    manifest = _write(story)

    result = triage._apply_ruling_for_mode(
        _PLAN, _STORY, story, {"action": "rebrief", "rationale": "r"}, manifest
    )

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert "triage_diagnosis" not in story


def test_shadow_mode_rebrief_does_not_store_a_diagnosis(plan_dir, notices, decisions, monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    story = _story(parked_reason=_NO_COMMIT_REASON)
    manifest = _write(story)
    ruling = {"action": "rebrief", "diagnosis": "Would be stored.", "rationale": "r"}

    result = triage._apply_ruling_for_mode(_PLAN, _STORY, story, ruling, manifest)

    assert result == "shadow"
    assert "triage_diagnosis" not in story
    assert story["status"] == "parked"


def test_ruling_parser_reads_the_diagnosis_line(plan_dir, monkeypatch):
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY")
    monkeypatch.setattr(
        triage, "_invoke_overlord",
        lambda prompt, **kwargs: _overlord_raw("rebrief", "The brief names pipeline/old.py."),
    )
    ruling = triage.rule_on_story(_PLAN, _STORY, _story(parked_reason=_NO_COMMIT_REASON), "evidence")

    assert ruling["diagnosis"] == "The brief names pipeline/old.py."


@pytest.mark.parametrize("diagnosis", [None, ""])
def test_ruling_parser_omits_the_diagnosis_when_absent_or_blank(plan_dir, monkeypatch, diagnosis):
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY")
    monkeypatch.setattr(
        triage, "_invoke_overlord",
        lambda prompt, **kwargs: _overlord_raw("rebrief", diagnosis),
    )
    ruling = triage.rule_on_story(_PLAN, _STORY, _story(parked_reason=_NO_COMMIT_REASON), "evidence")

    assert "diagnosis" not in ruling


def test_overlord_prompt_requests_the_diagnosis_line(plan_dir, monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY")

    def fake_overlord(prompt, **kwargs):
        prompts.append(prompt)
        return _overlord_raw("park_for_human")

    monkeypatch.setattr(triage, "_invoke_overlord", fake_overlord)
    triage.rule_on_story(_PLAN, _STORY, _story(parked_reason=_NO_COMMIT_REASON), "evidence")

    assert "\nDIAGNOSIS:" in prompts[0]


def test_overlord_diagnosis_line_reaches_the_rebrief_executor(
    plan_dir, notices, decisions, monkeypatch, execute_mode
):
    story = _story(parked_reason=_NO_COMMIT_REASON)
    manifest = _write(story)
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY")
    monkeypatch.setattr(
        triage, "_invoke_overlord",
        lambda prompt, **kwargs: _overlord_raw("rebrief", "The brief names the wrong file."),
    )

    ruling = triage.rule_on_story(_PLAN, _STORY, story, "evidence")
    result = triage._apply_ruling_for_mode(_PLAN, _STORY, story, ruling, manifest)

    assert result == "rebrief"
    assert story["triage_diagnosis"][-1]["text"] == "The brief names the wrong file."
