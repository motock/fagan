"""Tests for the ``split_story`` executor in ``pipeline/triage.py``.

Written FIRST (TDD) for the "split_story executor" story (story 5 of
``docs/plans/OVERLORD_PARKED_STORY_AUTONOMY_PLAN.md``).  Until the
implementation lands this file is expected to be RED for the right reason:
``pipeline.triage._execute_split_story`` does not exist yet and
``DEFERRED_ACTIONS`` still contains ``split_story``.

Contract under test
-------------------
* ``DEFERRED_ACTIONS`` no longer contains ``split_story``; ``repo_issue`` stays
  deferred and its park-loudly behaviour is unchanged.
* ``execute_ruling`` routes ``split_story`` to
  ``_execute_split_story(plan_name, story_key, story, ruling, manifest,
  manifest_path)``.
* Guards, in order:

    1. ``ruling["split"]`` must be exactly two non-empty summaries, else park
       loudly with reason ``split_story ruled but SPLIT payload invalid;
       parked for a human``;
    2. ``plan_triage_budget_exhausted(manifest)`` -> park loudly with reason
       ``plan triage budget exhausted``.

* On success: two children ``{key}-split-1`` / ``{key}-split-2`` carrying the
  payload summaries, the parent's ``agent_instructions`` plus a
  ``=== SPLIT FROM PRIOR STORY ===`` block (ruling rationale + the sibling
  note), ``persona``/``risk``/``backend`` copied from the parent, status
  ``todo``, **no** ``acceptance`` field and no inter-dependency; the parent is
  parked with ``split into <c1>, <c2> by triage``; ``triage_created_stories``
  is incremented by 2.
* The executor does NOT write the manifest file (``run_triage_sweep`` persists
  the mutated manifest after the tick).
* The OPSA-3 execution record's ``children`` placeholder carries the two child
  keys.
* ``overlord-policy.md``'s stale "split_story and repo_issue are currently
  recorded and then parked for a human" sentence is fixed; the ACTION contract
  line is untouched.

No real backend: ``_notify_user``, the escalation helpers and
``_append_decision`` are monkeypatched on ``pipeline.triage``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# Import pipeline.server FIRST: pipeline.triage imports pipeline.build_detect,
# which imports pipeline.server, which imports pipeline.triage.run_triage_sweep.
# Importing pipeline.triage first therefore trips a circular import; importing
# the server first lets that chain resolve (same reason the sibling triage test
# modules import server before triage).
import pipeline.server
import pipeline.triage
from pipeline import server
from pipeline import triage as triage_mod

REPO_ROOT = Path(pipeline.triage.__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "overlord-policy.md"

INVALID_PAYLOAD_REASON = (
    "split_story ruled but SPLIT payload invalid; parked for a human"
)
BUDGET_REASON = "plan triage budget exhausted"
SPLIT_BLOCK_MARKER = "=== SPLIT FROM PRIOR STORY ==="
SIBLING_NOTE = "this is one half of a split; the sibling story owns the other half"
ACTION_CONTRACT_LINE = "ACTION: escalate_model | split_story | repo_issue | park_for_human"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manifest():
    return {"stories": {}}


@pytest.fixture
def manifest_path(tmp_path):
    return tmp_path / "plan.manifest.json"


@pytest.fixture
def parent_story():
    """A story in the state triage hands to ``execute_ruling``."""
    return {
        "status": "failed",
        "model": "qwen2.5-coder",
        "backend": "local",
        "persona": "implementer",
        "risk": "medium",
        "agent_instructions": "Implement the widget.",
        "acceptance": ["the widget spins"],
    }


@pytest.fixture
def split_ruling():
    return {
        "action": "split_story",
        "rationale": "the story is too large for one implementer at this tier",
        "ruling": "split",
        "tier": "tier1",
        "risk": "low",
        "notify_user": True,
        "split": ["first half of the widget", "second half of the widget"],
    }


@pytest.fixture
def repo_ruling():
    return {
        "action": "repo_issue",
        "rationale": "the failure is environmental, not the story's fault",
        "ruling": "file issue",
        "tier": "tier1",
        "risk": "low",
        "notify_user": True,
    }


@pytest.fixture
def patched(monkeypatch):
    """Patch the notifier and escalation helpers; return the call log."""
    state = {"notify_calls": [], "claude_calls": [], "fallback_calls": []}

    def _fake_notify(plan_name, message, *args, **kwargs):
        state["notify_calls"].append(
            {"plan_name": plan_name, "message": message, "args": args, "kwargs": kwargs}
        )

    def _fake_escalate_to_claude(*args, **kwargs):
        state["claude_calls"].append((args, kwargs))

    def _fake_escalate_to_local_fallback_model(*args, **kwargs):
        state["fallback_calls"].append((args, kwargs))

    monkeypatch.setattr(triage_mod, "_notify_user", _fake_notify, raising=False)
    monkeypatch.setattr(
        triage_mod, "_escalate_to_claude", _fake_escalate_to_claude, raising=False
    )
    monkeypatch.setattr(
        triage_mod,
        "_escalate_to_local_fallback_model",
        _fake_escalate_to_local_fallback_model,
        raising=False,
    )
    monkeypatch.setattr(
        triage_mod, "_auto_escalation_enabled", lambda: True, raising=False
    )
    return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _triage_source() -> str:
    with open(pipeline.triage.__file__, encoding="utf-8") as fh:
        return fh.read()


def _deferred_actions_comment_block() -> str:
    """The contiguous comment lines immediately above the DEFERRED_ACTIONS
    assignment."""
    lines = _triage_source().splitlines()
    idx = None
    for i, line in enumerate(lines):
        if line.startswith("DEFERRED_ACTIONS"):
            idx = i
            break
    assert idx is not None, "DEFERRED_ACTIONS assignment not found in pipeline/triage.py"
    block: list[str] = []
    j = idx - 1
    while j >= 0 and lines[j].lstrip().startswith("#"):
        block.append(lines[j])
        j -= 1
    return "\n".join(reversed(block))


def _split(plan_name, story_key, story, ruling, manifest, manifest_path):
    """Call the executor under test (fails loudly if it does not exist)."""
    return triage_mod._execute_split_story(
        plan_name, story_key, story, ruling, manifest, manifest_path
    )


def _child_keys(story_key: str) -> tuple[str, str]:
    return f"{story_key}-split-1", f"{story_key}-split-2"


def _notify_messages(patched) -> list[str]:
    return [c["message"] for c in patched["notify_calls"]]


# ---------------------------------------------------------------------------
# DEFERRED_ACTIONS: split_story removed, repo_issue stays
# ---------------------------------------------------------------------------


class TestDeferredActionsConstant:
    def test_deferred_actions_is_a_frozenset(self):
        assert isinstance(triage_mod.DEFERRED_ACTIONS, frozenset)

    def test_split_story_is_no_longer_deferred(self):
        assert "split_story" not in triage_mod.DEFERRED_ACTIONS

    def test_repo_issue_is_still_deferred(self):
        assert "repo_issue" in triage_mod.DEFERRED_ACTIONS

    def test_comment_no_longer_claims_split_story_is_deferred(self):
        block = _deferred_actions_comment_block()
        assert "repo_issue" in block, (
            "the DEFERRED_ACTIONS comment must still name repo_issue as deferred"
        )
        assert "deferred" in block.lower()
        assert "split_story and repo_issue" not in block, (
            "the DEFERRED_ACTIONS comment must no longer pair split_story with "
            "repo_issue as deferred; repo_issue alone remains deferred"
        )


# ---------------------------------------------------------------------------
# The executor surface and the execute_ruling routing
# ---------------------------------------------------------------------------


class TestExecutorSurface:
    def test_execute_split_story_exists(self):
        assert hasattr(triage_mod, "_execute_split_story")

    def test_execute_split_story_signature(self):
        params = list(inspect.signature(triage_mod._execute_split_story).parameters)
        assert params == [
            "plan_name",
            "story_key",
            "story",
            "ruling",
            "manifest",
            "manifest_path",
        ]


class TestExecuteRulingRouting:
    def test_split_story_routes_to_the_executor(
        self, parent_story, split_ruling, manifest, manifest_path, patched, monkeypatch
    ):
        calls = []

        def _fake_execute(*args, **kwargs):
            calls.append((args, kwargs))
            return "split_story"

        monkeypatch.setattr(triage_mod, "_execute_split_story", _fake_execute, raising=False)

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", parent_story, split_ruling, manifest, manifest_path
        )

        assert len(calls) == 1, "execute_ruling must route split_story to _execute_split_story"
        args, kwargs = calls[0]
        assert args == ("cap1", "S1", parent_story, split_ruling, manifest, manifest_path) or (
            kwargs.get("plan_name") == "cap1"
            and kwargs.get("story_key") == "S1"
            and kwargs.get("story") is parent_story
            and kwargs.get("ruling") is split_ruling
            and kwargs.get("manifest") is manifest
            and kwargs.get("manifest_path") == manifest_path
        )
        assert result == "split_story"

    def test_repo_issue_does_not_route_to_the_split_executor(
        self, parent_story, repo_ruling, manifest, manifest_path, patched, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            triage_mod,
            "_execute_split_story",
            lambda *a, **k: calls.append((a, k)) or "split_story",
            raising=False,
        )

        pipeline.triage.execute_ruling(
            "cap1", "S2", parent_story, repo_ruling, manifest, manifest_path
        )

        assert calls == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSplitHappyPath:
    def test_creates_exactly_two_children_with_deterministic_keys(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert set(manifest["stories"]) == {"S1-split-1", "S1-split-2"}

    def test_child_summaries_come_from_the_split_payload(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert manifest["stories"]["S1-split-1"]["summary"] == "first half of the widget"
        assert manifest["stories"]["S1-split-2"]["summary"] == "second half of the widget"

    def test_children_are_todo(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert manifest["stories"]["S1-split-1"]["status"] == "todo"
        assert manifest["stories"]["S1-split-2"]["status"] == "todo"

    def test_children_have_no_acceptance_field(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        # The parent carries acceptance fixtures; the children must NOT inherit
        # them (they cannot be mechanically halved).
        assert parent_story["acceptance"]
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert "acceptance" not in manifest["stories"]["S1-split-1"]
        assert "acceptance" not in manifest["stories"]["S1-split-2"]

    def test_children_have_no_interdependency(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        c1 = manifest["stories"]["S1-split-1"]
        c2 = manifest["stories"]["S1-split-2"]
        assert not c1.get("dependencies")
        assert not c2.get("dependencies")
        assert "S1-split-2" not in (c1.get("dependencies") or [])
        assert "S1-split-1" not in (c2.get("dependencies") or [])

    def test_child_instructions_extend_the_parents(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        for key in _child_keys("S1"):
            instructions = manifest["stories"][key]["agent_instructions"]
            assert instructions.startswith(parent_story["agent_instructions"])
            assert SPLIT_BLOCK_MARKER in instructions

    def test_child_instructions_carry_rationale_and_sibling_note(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        for key in _child_keys("S1"):
            instructions = manifest["stories"][key]["agent_instructions"]
            assert split_ruling["rationale"] in instructions
            assert SIBLING_NOTE in instructions

    def test_child_instructions_when_parent_has_none(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        parent_story.pop("agent_instructions")
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        for key in _child_keys("S1"):
            instructions = manifest["stories"][key]["agent_instructions"]
            assert SPLIT_BLOCK_MARKER in instructions
            assert SIBLING_NOTE in instructions

    def test_persona_risk_backend_copied_from_parent(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        for key in _child_keys("S1"):
            child = manifest["stories"][key]
            assert child["persona"] == parent_story["persona"]
            assert child["risk"] == parent_story["risk"]
            assert child["backend"] == parent_story["backend"]

    def test_persona_risk_backend_absent_when_parent_lacks_them(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        for field in ("persona", "risk", "backend"):
            parent_story.pop(field)
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        for key in _child_keys("S1"):
            child = manifest["stories"][key]
            assert child.get("persona") is None
            assert child.get("risk") is None
            assert child.get("backend") is None

    def test_parent_parked_with_provenance_reason(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert parent_story["status"] == "parked"
        assert parent_story["parked_reason"] == "split into S1-split-1, S1-split-2 by triage"

    def test_triage_created_stories_incremented_by_two(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        manifest["triage_created_stories"] = 1
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert manifest["triage_created_stories"] == 3

    def test_triage_created_stories_missing_key_becomes_two(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        assert "triage_created_stories" not in manifest
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert manifest["triage_created_stories"] == 2

    def test_triage_created_stories_non_int_coerced_to_zero_then_plus_two(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        # Mirrors _coerce_int, which plan_triage_budget_exhausted reads.
        manifest["triage_created_stories"] = "not-a-number"
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert manifest["triage_created_stories"] == 2

    def test_manifest_file_is_not_written(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        assert not manifest_path.exists()
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert not manifest_path.exists(), (
            "the executor must not persist the manifest; run_triage_sweep does that"
        )

    def test_parent_is_not_marked_as_a_deferred_action(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert "triage_deferred_action" not in parent_story

    def test_split_story_invalid_payload_notifies_with_truncated_rationale(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        split_ruling["rationale"] = "A" * 5000
        split_ruling["split"] = []

        pipeline.triage.execute_ruling(
            "cap1", "S1", parent_story, split_ruling, manifest, manifest_path
        )

        msgs = _notify_messages(patched)
        assert any(("A" * 300) in m for m in msgs), msgs
        assert all(("A" * 301) not in m for m in msgs), msgs

    def test_returns_a_string(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        result = _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert isinstance(result, str)

    def test_execute_ruling_creates_the_children_end_to_end(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S1", parent_story, split_ruling, manifest, manifest_path
        )

        assert set(manifest["stories"]) == {"S1-split-1", "S1-split-2"}
        assert parent_story["status"] == "parked"
        assert manifest["triage_created_stories"] == 2


# ---------------------------------------------------------------------------
# Guard 1: invalid SPLIT payload
# ---------------------------------------------------------------------------


class TestSplitInvalidPayload:
    @pytest.mark.parametrize(
        "payload",
        [
            [],
            ["only one summary"],
            ["a", "b", "c"],
            ["a", ""],
            ["", "b"],
            ["a", "   "],
            ["   ", "b"],
            None,
            "not a list",
            {"child": "a"},
        ],
    )
    def test_invalid_payload_parks_loudly(
        self, parent_story, split_ruling, manifest, manifest_path, patched, payload
    ):
        split_ruling["split"] = payload

        result = _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert result == "park_for_human"
        assert parent_story["status"] == "parked"
        assert parent_story["parked_reason"] == INVALID_PAYLOAD_REASON

    def test_invalid_payload_notifies(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        split_ruling["split"] = []

        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        msgs = _notify_messages(patched)
        assert any("S1" in m and INVALID_PAYLOAD_REASON in m for m in msgs), (
            f"expected a loud notification naming the story and the reason, got {msgs}"
        )

    def test_invalid_payload_creates_nothing(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        split_ruling["split"] = ["only one summary"]

        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert manifest["stories"] == {}
        assert manifest.get("triage_created_stories", 0) == 0

    def test_missing_split_key_parks_loudly(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        split_ruling.pop("split")

        result = _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert result == "park_for_human"
        assert parent_story["parked_reason"] == INVALID_PAYLOAD_REASON
        assert manifest["stories"] == {}

    def test_invalid_payload_via_execute_ruling_parks_loudly(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        split_ruling["split"] = ["a", "b", "c"]

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", parent_story, split_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert parent_story["parked_reason"] == INVALID_PAYLOAD_REASON
        assert manifest["stories"] == {}


# ---------------------------------------------------------------------------
# Guard 2: plan triage budget exhausted
# ---------------------------------------------------------------------------


class TestSplitBudgetExhausted:
    def test_budget_exhausted_parks_loudly(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        manifest["triage_created_stories"] = triage_mod.TRIAGE_MAX_CREATED_STORIES
        assert triage_mod.plan_triage_budget_exhausted(manifest)

        result = _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert result == "park_for_human"
        assert parent_story["status"] == "parked"
        assert parent_story["parked_reason"] == BUDGET_REASON

    def test_budget_exhausted_notifies(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        manifest["triage_created_stories"] = triage_mod.TRIAGE_MAX_CREATED_STORIES

        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        msgs = _notify_messages(patched)
        assert any("S1" in m and BUDGET_REASON in m for m in msgs), (
            f"expected a loud notification naming the story and the reason, got {msgs}"
        )

    def test_budget_exhausted_creates_nothing(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        manifest["triage_created_stories"] = triage_mod.TRIAGE_MAX_CREATED_STORIES

        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert manifest["stories"] == {}
        assert manifest["triage_created_stories"] == triage_mod.TRIAGE_MAX_CREATED_STORIES

    def test_invalid_payload_is_checked_before_the_budget(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        manifest["triage_created_stories"] = triage_mod.TRIAGE_MAX_CREATED_STORIES
        split_ruling["split"] = []

        _split("cap1", "S1", parent_story, split_ruling, manifest, manifest_path)

        assert parent_story["parked_reason"] == INVALID_PAYLOAD_REASON

    def test_budget_exhausted_via_execute_ruling_parks_loudly(
        self, parent_story, split_ruling, manifest, manifest_path, patched
    ):
        manifest["triage_created_stories"] = triage_mod.TRIAGE_MAX_CREATED_STORIES

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", parent_story, split_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert parent_story["parked_reason"] == BUDGET_REASON
        assert manifest["stories"] == {}


# ---------------------------------------------------------------------------
# repo_issue: park-loudly behaviour unchanged
# ---------------------------------------------------------------------------


class TestRepoIssueUnchanged:
    def test_repo_issue_parks_and_marks_deferred(
        self, parent_story, repo_ruling, manifest, manifest_path, patched
    ):
        result = pipeline.triage.execute_ruling(
            "cap1", "S2", parent_story, repo_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert parent_story["status"] == "parked"
        assert parent_story["triage_deferred_action"] == "repo_issue"

    def test_repo_issue_parked_reason_names_action_not_implemented(
        self, parent_story, repo_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S2", parent_story, repo_ruling, manifest, manifest_path
        )

        reason = parent_story["parked_reason"]
        assert "repo_issue" in reason
        assert "not implemented" in reason.lower()
        assert "human" in reason.lower()

    def test_repo_issue_notifies_with_key_and_action(
        self, parent_story, repo_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S2", parent_story, repo_ruling, manifest, manifest_path
        )

        msgs = _notify_messages(patched)
        assert any("S2" in m and "repo_issue" in m for m in msgs)

    def test_repo_issue_creates_no_children(
        self, parent_story, repo_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S2", parent_story, repo_ruling, manifest, manifest_path
        )

        assert manifest["stories"] == {}
        assert manifest.get("triage_created_stories", 0) == 0

    def test_repo_issue_does_not_escalate(
        self, parent_story, repo_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S2", parent_story, repo_ruling, manifest, manifest_path
        )

        assert patched["claude_calls"] == []
        assert patched["fallback_calls"] == []


# ---------------------------------------------------------------------------
# OPSA-3 execution record: the children placeholder
# ---------------------------------------------------------------------------


def _capture_appends(monkeypatch):
    calls = []

    def fake_append(plan_name, record):
        calls.append((plan_name, record))

    monkeypatch.setattr(triage_mod, "_append_decision", fake_append, raising=False)
    return calls


class TestExecutionRecordChildren:
    def test_record_children_lists_the_two_child_keys(
        self, parent_story, split_ruling, manifest, manifest_path, patched, monkeypatch
    ):
        monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full", raising=False)
        calls = _capture_appends(monkeypatch)

        triage_mod._apply_ruling_for_mode(
            "cap1", "S1", parent_story, split_ruling, manifest, manifest_path
        )

        assert len(calls) == 1
        record = calls[0][1]
        assert record["children"] == ["S1-split-1", "S1-split-2"]

    def test_record_children_empty_for_repo_issue(
        self, parent_story, repo_ruling, manifest, manifest_path, patched, monkeypatch
    ):
        monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full", raising=False)
        calls = _capture_appends(monkeypatch)

        triage_mod._apply_ruling_for_mode(
            "cap1", "S2", parent_story, repo_ruling, manifest, manifest_path
        )

        assert len(calls) == 1
        assert calls[0][1]["children"] == []

    def test_record_still_carries_prior_state(
        self, parent_story, split_ruling, manifest, manifest_path, patched, monkeypatch
    ):
        parent_story["parked_reason"] = "prior reason"
        monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full", raising=False)
        calls = _capture_appends(monkeypatch)

        triage_mod._apply_ruling_for_mode(
            "cap1", "S1", parent_story, split_ruling, manifest, manifest_path
        )

        record = calls[0][1]
        assert record["prior_status"] == "failed"
        assert record["prior_parked_reason"] == "prior reason"
        assert record["action"] == "split_story"


# ---------------------------------------------------------------------------
# overlord-policy.md
# ---------------------------------------------------------------------------


class TestPolicyDocument:
    @pytest.fixture(scope="class")
    def policy_text(self):
        return POLICY_PATH.read_text(encoding="utf-8")

    def test_stale_sentence_is_gone(self, policy_text):
        assert (
            "`split_story` and `repo_issue` are currently recorded and then parked for a human"
            not in policy_text
        ), "the stale 'split_story and repo_issue ... parked for a human' sentence must be fixed"

    def test_repo_issue_alone_is_deferred_and_split_executes(self, policy_text):
        paragraphs = [
            block
            for block in policy_text.split("\n\n")
            if "parked for a human" in block
        ]
        assert paragraphs, "the policy must still describe repo_issue parking for a human"
        paragraph = "\n".join(paragraphs)
        assert "repo_issue" in paragraph
        assert "split_story" in paragraph
        assert "child" in paragraph.lower(), (
            "the fixed sentence must say split_story executes by creating child stories"
        )

    def test_action_contract_line_is_untouched(self, policy_text):
        assert ACTION_CONTRACT_LINE in policy_text

    def test_split_payload_line_is_untouched(self, policy_text):
        assert "SPLIT" in policy_text
        assert "||" in policy_text
