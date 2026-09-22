"""OA2-02: a successful ``patch_acceptance`` recovery must clear the story's
``triage_attempts`` counter.

``TRIAGE_MAX_ATTEMPTS`` (2) caps how many times the triage sweep may act on a
story.  A story that is recovered by ``patch_acceptance`` (its born-broken
acceptance fixture is rewritten and its status flips back to ``todo``) has
demonstrably moved on: if it later relapses and parks again it must re-qualify
for the FULL triage budget, not stay permanently capped by the attempts it
burned before the recovery.

These tests drive the real production caller, ``_apply_ruling_for_mode``, with
the overlord, the oracle validators and the OPSA-8 lint/collection helpers
stubbed at their true boundaries - never real network, never a real pytest run.

Assertions are deliberately scoped to the single ``triage_attempts`` field:
the story dict is a cumulative runtime record (status, parked_reason,
acceptance, ...), so comparing it wholesale would be brittle and wrong.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import server, triage

PLAN = "plan-a"
STORY_KEY = "OA2-02"

_FIXTURE_PATH = "tests/unit/test_x.py"
_ORIGINAL_SOURCE = "def test_x():\n    assert False\n"
_REWRITTEN_SOURCE = "def test_x():\n    assert True\n"

_START = "===FIXTURE-START==="
_END = "===FIXTURE-END==="


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ruling() -> dict:
    return {"action": "patch_acceptance", "rationale": "born-broken oracle"}


def _overlord_reply(
    source: str = _REWRITTEN_SOURCE,
    diagnosis: str = "the fixture crashed at import",
) -> str:
    return f"DIAGNOSIS: {diagnosis}\n{_START}\n{source}{_END}\n"


def _source_of(story) -> str | None:
    """The acceptance source carried by whatever the executor hands the
    validators: a story dict, or the source string itself."""
    if isinstance(story, str):
        return story
    if isinstance(story, dict):
        for entry in story.get("acceptance") or []:
            if isinstance(entry, dict) and entry.get("source") is not None:
                return entry["source"]
    return None


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Isolate the executor: stub the overlord, the oracle validators, the
    OPSA-8 lint/collection helpers, and capture notify + decisions."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    calls = {"decisions": [], "notify": [], "overlord": []}
    state = {
        # classification of the ORIGINAL fixture at a clean baseline
        "baseline_state": "errors",
        "baseline_detail": "baseline failure output: SyntaxError in fixture",
        # classification of the REWRITTEN fixture at a clean baseline
        "rewritten_state": "fails_correctly",
        "rewritten_detail": "rewritten fixture fails as expected",
        "lint_kind": "clean",
        "lint_msg": None,
        "overlord_reply": _overlord_reply(),
        "role_config": {"overlord": {"model": "sentinel-model"}},
        "evidence": "EVIDENCE-SENTINEL-42",
    }

    def fake_validate(story, checkout=None, **kwargs):
        if isinstance(story, dict) and not (story.get("acceptance") or []):
            return {
                "state": "none",
                "detail": "story has no acceptance fixtures",
                "paths": [],
            }
        if _source_of(story) == _REWRITTEN_SOURCE:
            return {
                "state": state["rewritten_state"],
                "detail": state["rewritten_detail"],
                "paths": [_FIXTURE_PATH],
            }
        return {
            "state": state["baseline_state"],
            "detail": state["baseline_detail"],
            "paths": [_FIXTURE_PATH],
        }

    def fake_lint(story, repo_root=None, **kwargs):
        return (state["lint_kind"], state["lint_msg"])

    def fake_overlord(prompt, plan_role_config=None, **kwargs):
        calls["overlord"].append(
            {"prompt": prompt, "plan_role_config": plan_role_config}
        )
        return state["overlord_reply"]

    monkeypatch.setattr(triage, "_invoke_overlord", fake_overlord)
    monkeypatch.setattr(
        triage, "_plan_role_config", lambda plan_name: state["role_config"]
    )
    monkeypatch.setattr(
        triage, "_notify_user", lambda *a, **k: calls["notify"].append((a, k))
    )
    monkeypatch.setattr(
        triage, "_append_decision", lambda plan, rec: calls["decisions"].append(rec)
    )
    monkeypatch.setattr(
        triage,
        "collect_triage_evidence",
        lambda *a, **k: state["evidence"],
        raising=False,
    )
    monkeypatch.setattr(triage, "_load_policy", lambda: "")

    monkeypatch.setattr(triage, "validate_acceptance_fixtures", fake_validate)
    monkeypatch.setattr(triage, "_lint_acceptance_fixtures", fake_lint)
    monkeypatch.setattr(triage, "_pytest_acceptance_fixtures", fake_lint)

    # ``_apply_ruling_for_mode`` reads the mode lazily via
    # ``from .server import PIPELINE_AUTONOMY``.
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")

    return SimpleNamespace(
        worktree=str(worktree),
        calls=calls,
        state=state,
        manifest_path=tmp_path / "plan-a.manifest.json",
    )


def _story(harness, **over) -> dict:
    story = {
        "key": STORY_KEY,
        "story_key": STORY_KEY,
        "summary": "triage attempts reset",
        "status": "parked",
        "parked_reason": "acceptance fixture demonstrably broken at a clean baseline",
        "worktree": harness.worktree,
        "acceptance": [{"path": _FIXTURE_PATH, "source": _ORIGINAL_SOURCE}],
    }
    story.update(over)
    return story


def _manifest(story) -> dict:
    return {"stories": {story["key"]: story}}


def _apply(harness, story, manifest=None):
    """Drive the real production caller for a ``patch_acceptance`` ruling."""
    return triage._apply_ruling_for_mode(
        PLAN,
        story["key"],
        story,
        _ruling(),
        manifest if manifest is not None else _manifest(story),
        harness.manifest_path,
    )


# ---------------------------------------------------------------------------
# success path: the counter is cleared
# ---------------------------------------------------------------------------


def test_successful_patch_acceptance_resets_triage_attempts(harness):
    """A recovered story must not stay capped by pre-recovery attempts."""
    story = _story(harness, triage_attempts=triage.TRIAGE_MAX_ATTEMPTS)

    result = _apply(harness, story)

    assert result == "patch_acceptance", (
        f"expected the success path, got {result!r}; the harness must drive a "
        "successful patch_acceptance"
    )
    assert story.get("status") == "todo", (
        "precondition: a successful patch_acceptance flips the story back to todo"
    )
    assert story.get("triage_attempts", 0) == 0, (
        "a successful patch_acceptance recovery must reset triage_attempts so a "
        "later relapse re-qualifies for the full triage budget"
    )


def test_recovered_story_requalifies_after_relapse(harness):
    """After recovery, a relapse must make the story a triage candidate again."""
    story = _story(harness, triage_attempts=triage.TRIAGE_MAX_ATTEMPTS)
    assert triage.triage_allowed(story)[0] is False, (
        "precondition: a story at the cap is not a triage candidate"
    )

    _apply(harness, story)

    # Simulate the relapse the way production does: the park path increments
    # the counter through record_triage_attempt.
    triage.record_triage_attempt(story, "patch_acceptance")

    allowed, reason = triage.triage_allowed(story)
    assert allowed is True, (
        "a previously-recovered story that parks again must be eligible for "
        f"triage, not excluded by a stale cap (reason={reason!r})"
    )


# ---------------------------------------------------------------------------
# negative / boundary
# ---------------------------------------------------------------------------


def test_unsuccessful_patch_attempt_does_not_reset(harness):
    """A failed patch_acceptance must leave the counter untouched."""
    # The ruling mis-reads the story: the fixture is NOT broken at a clean
    # baseline, so the executor parks without rewriting anything.
    harness.state["baseline_state"] = "passes"
    harness.state["baseline_detail"] = "fixture passes at a clean baseline"
    story = _story(harness, triage_attempts=triage.TRIAGE_MAX_ATTEMPTS)

    result = _apply(harness, story)

    assert result != "patch_acceptance", (
        f"expected a fail-closed park, got {result!r}; the harness must drive an "
        "UNSUCCESSFUL patch_acceptance"
    )
    assert story.get("triage_attempts", 0) == triage.TRIAGE_MAX_ATTEMPTS, (
        "an unsuccessful patch_acceptance must not reset triage_attempts, or a "
        "story that keeps failing would get unlimited triage"
    )


def test_story_without_triage_attempts_unaffected(harness):
    """A story that never had the key stays uncapped and gains no stale value."""
    story = _story(harness)
    assert "triage_attempts" not in story, "precondition: no counter on the story"

    result = _apply(harness, story)

    assert result == "patch_acceptance", (
        f"expected the success path, got {result!r}"
    )
    assert story.get("triage_attempts", 0) == 0, (
        "a story that never had triage_attempts must remain uncapped"
    )
    assert triage.triage_allowed(story)[0] is True, (
        "a story with no triage_attempts must still be a triage candidate"
    )
