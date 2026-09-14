"""Regression tests for the ``patch_acceptance`` executor's OPSA-3 audit trail.

Review regression (blocking)
----------------------------
``_execute_patch_acceptance`` set the ``_patch_acceptance_recorded`` sentinel at
the TOP of the function - before it knew whether it would actually write a
record.  Every fail-closed park path (``return _park(...)``) therefore left the
sentinel set while writing no record, and ``_apply_ruling_for_mode`` popped the
(lying) sentinel and returned early, skipping its own ``_record_execution``.
Net effect: NO decisions-log record was appended for any fail-closed
``patch_acceptance`` outcome - exactly the paths a human must inspect - and the
executor's docstring ("writes its own OPSA-3 record on every exit path") was
false.

These tests drive the real production caller, ``_apply_ruling_for_mode``, and
assert the decisions log grows by exactly one record per call: on the park paths
(where ``_apply_ruling_for_mode`` owns the record) and on the success path
(where the executor owns it and the sentinel must still suppress the duplicate).

Secondary regressions covered here
----------------------------------
* stale ``parked_reason`` left on a story reset to ``todo``;
* ``_story_checkout`` silently falling back to ``Path.cwd()``;
* ``_acceptance_with_source`` fabricating a ``source`` for an unsourced entry;
* the sentinel leaking onto the story when ``execute_ruling`` runs directly.

The overlord, the oracle validators and the OPSA-8 lint/collection helpers are
stubbed at their true boundaries - never real network, never a real pytest run.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import server, triage

PLAN = "plan-a"
STORY_KEY = "OPSA-7"

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
            # Mirrors the real validate_acceptance_fixtures: no fixtures -> "none".
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

    # The overlord boundary: never real network.
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
        triage, "collect_triage_evidence", lambda *a, **k: state["evidence"], raising=False
    )
    monkeypatch.setattr(triage, "_load_policy", lambda: "")

    # oracle_gate's existing classification/validation must be REUSED, not
    # re-derived.
    monkeypatch.setattr(triage, "validate_acceptance_fixtures", fake_validate)
    # OPSA-8's lint/collection helpers (one implementation, two callers).
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
        "summary": "patch_acceptance executor",
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
# the blocking regression: every exit path is auditable
# ---------------------------------------------------------------------------


def test_park_path_appends_a_decisions_log_record(harness):
    """A fail-closed park outcome must still be auditable.

    "No acceptance fixtures" is the cheapest park path: the executor returns
    ``_park(...)`` before writing any record, so ``_apply_ruling_for_mode`` must
    append the record itself.
    """
    story = _story(harness, acceptance=[])
    before = len(harness.calls["decisions"])

    _apply(harness, story)

    assert story["status"] == "parked"
    assert len(harness.calls["decisions"]) == before + 1, (
        "a fail-closed patch_acceptance park outcome must append exactly one "
        "decisions-log record; the sentinel must not suppress it"
    )


def test_second_park_path_on_the_same_story_still_appends(harness):
    """The sentinel is per-call state: a follow-up park must also be recorded.

    Traces the review's worked example: call A parks with no acceptance
    fixtures, call B parks on a lint finding.  Each must append its own record.
    """
    story = _story(harness, acceptance=[])

    _apply(harness, story)
    assert len(harness.calls["decisions"]) == 1, (
        "the first park outcome must be recorded"
    )

    # Follow-up call on the same story: the rewrite trips the ingest lint gate.
    story["acceptance"] = [{"path": _FIXTURE_PATH, "source": _ORIGINAL_SOURCE}]
    harness.state["lint_kind"] = "finding"
    harness.state["lint_msg"] = "E501 line too long"

    _apply(harness, story)

    assert story["status"] == "parked"
    assert len(harness.calls["decisions"]) == 2, (
        "each fail-closed park outcome must append its own record; a stale "
        "sentinel must not suppress the second one"
    )


def test_success_path_appends_exactly_one_record(harness):
    """On success the executor records once and the sentinel suppresses the
    duplicate record from ``_apply_ruling_for_mode``."""
    story = _story(harness)

    _apply(harness, story)

    assert story["status"] == "todo"
    records = harness.calls["decisions"]
    assert len(records) == 1, (
        "the success path must append exactly one record: the executor writes "
        "it and the sentinel stops _apply_ruling_for_mode double-recording"
    )
    assert records[0].get("result") == "patch_acceptance"


def test_execute_ruling_directly_leaves_no_sentinel_on_a_park_path(harness):
    """The internal sentinel must not leak onto the story/manifest when
    ``execute_ruling`` runs without ``_apply_ruling_for_mode``."""
    story = _story(harness, acceptance=[])
    manifest = _manifest(story)

    triage.execute_ruling(
        PLAN, story["key"], story, _ruling(), manifest, harness.manifest_path
    )

    assert "_patch_acceptance_recorded" not in story, (
        "the internal sentinel must not leak onto the story/manifest"
    )
    assert "_patch_acceptance_recorded" not in manifest["stories"][story["key"]], (
        "the internal sentinel must not leak into the serialized manifest"
    )


def test_executor_docstring_no_longer_claims_a_record_on_every_exit_path():
    """The docstring claimed the executor records on every exit path; park
    outcomes are actually recorded by ``_apply_ruling_for_mode``."""
    doc = (triage._execute_patch_acceptance.__doc__ or "").lower()
    assert "every exit path" not in doc, (
        "the docstring must not claim the executor writes a record on every "
        "exit path - park outcomes are recorded by _apply_ruling_for_mode"
    )


# ---------------------------------------------------------------------------
# secondary regressions
# ---------------------------------------------------------------------------


def test_success_clears_the_stale_parked_reason(harness):
    """A story reset to ``todo`` must not keep a stale ``parked_reason``."""
    story = _story(harness)
    assert story["parked_reason"]

    _apply(harness, story)

    assert story["status"] == "todo"
    assert not story.get("parked_reason"), (
        "a story reset to todo must not keep a stale parked_reason"
    )


def test_story_checkout_falls_back_to_dot_like_other_call_sites():
    """``_story_checkout`` must use the same ``or "."`` idiom as the other
    call sites instead of silently falling back to ``Path.cwd()``."""
    assert triage._story_checkout({"worktree": None}) == Path(".")
    assert triage._story_checkout({}) == Path(".")
    assert triage._story_checkout({"worktree": ""}) == Path(".")

    story = {"worktree": None}
    assert triage._story_checkout(story) == Path(story.get("worktree") or ".")


def test_acceptance_with_source_does_not_fabricate_a_source():
    """When no entry carries a ``source`` key, the corrected source must not be
    stamped onto the last entry."""
    entries = [{"path": "a.py"}, {"path": "b.py"}]

    try:
        result = triage._acceptance_with_source(entries, "NEW-SOURCE")
    except Exception:  # noqa: BLE001 - raising a clear error is an acceptable contract
        return

    for entry in result:
        assert entry.get("source") != "NEW-SOURCE", (
            "an entry with no source key must not be stamped with a fabricated "
            "source"
        )
