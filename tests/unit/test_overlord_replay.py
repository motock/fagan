"""Tests for the read-only overlord ruling replay (``pipeline.overlord_replay``).

Every recorded ruling carries an ``input`` snapshot of the question the
overlord was asked (``pipeline.ruling_snapshot``), so the question can be asked
again. These tests replay synthetic records against a stubbed overlord, a stubbed
decisions log and a stubbed snapshot loader: no model, store or decisions file
is touched. Scoring reuses ``pipeline.triage_readiness``'s rules, so the cases
mirror that module's tests.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from pipeline import (
    decision_log,
    overlord,
    review,
    server,
    triage,
)
from pipeline import overlord_replay as rp

_CLASS = "guard_demands_path"
_REBASE_CLASS = "rebase_conflict"
_MERGE_CLASS = "high_risk_merge"
_T0 = "2026-11-01T00:00:00+00:00"
_TRIAGE_EVIDENCE = "\nTRIAGE QUESTION: this story is terminal. Decide what to do about it.\n"
_MERGE_EVIDENCE = (
    "MERGE ADJUDICATION: rule proceed or park.\n"
    "STORY: s1\n"
    "Respond in this exact format:\n"
    "RULING: proceed|park\n"
    "RATIONALE: <one line>\n"
)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _input(evidence: str = _TRIAGE_EVIDENCE, **overrides) -> dict:
    snapshot = {
        "evidence": evidence,
        "truncated": False,
        "prompt_sha256": None,
        "policy_sha256": None,
        "context_digests": [],
        "backend": "claude",
        "model": "opus",
        "park_class": _CLASS,
    }
    snapshot.update(overrides)
    return snapshot


def _ruling(key: str, *, action: str = "rebrief", decided_at: str = _T0, **overrides) -> dict:
    record = {
        "story_key": key,
        "question": "failure triage",
        "action": action,
        "park_class": _CLASS,
        "decided_at": decided_at,
        "input": _input(),
    }
    record.update(overrides)
    return record


def _merge(key: str, *, ruling: str = "proceed", decided_at: str = _T0, input_=None) -> dict:
    return {
        "story_key": key,
        "question": "high-risk merge adjudication",
        "ruling": ruling,
        "decided_at": decided_at,
        "input": input_ if input_ is not None else _input(_MERGE_EVIDENCE, park_class="high_risk_merge"),
    }


def _resolution(key: str, ruling_ref: str, human_action: str) -> dict:
    return {
        "story_key": key,
        "question": "human resolution",
        "ruling_ref": ruling_ref,
        "human_action": human_action,
        "decided_by": "human",
        "decided_at": "2026-11-02T00:00:00+00:00",
    }


class _Overlord:
    """Stub for ``overlord._invoke_overlord``: replies in order, repeating the last."""

    def __init__(self, replies: list[str] | None = None, *, error: Exception | None = None):
        self._replies = list(replies or ["ACTION: rebrief\nRATIONALE: ok"])
        self._error = error
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *args, **kwargs) -> str:
        self.prompts.append(prompt)
        if self._error is not None:
            raise self._error
        return self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]


@pytest.fixture
def overlord_stub(monkeypatch):
    def install(replies=None, *, error=None) -> _Overlord:
        stub = _Overlord(replies, error=error)
        monkeypatch.setattr(overlord, "_invoke_overlord", stub)
        return stub

    monkeypatch.setattr(overlord, "_load_policy", lambda: "POLICY\n")
    return install


@pytest.fixture
def decisions(monkeypatch):
    def install(records: list[dict], plan: str = "p1") -> None:
        monkeypatch.setattr(
            decision_log, "read_decisions", lambda name: records if name == plan else []
        )

    return install


@pytest.fixture
def snapshots(monkeypatch):
    """Stub the recorded-snapshot loader with a digest -> text mapping."""

    def install(mapping: dict[str, str]) -> None:
        monkeypatch.setattr(review, "_read_recorded_snapshot", lambda rec: mapping.get(rec["digest"]))

    install({})
    return install


def _report(capsys, argv: list[str]) -> dict:
    assert rp.main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


# --- scoring ---------------------------------------------------------------


def test_triage_ruling_agreeing_with_paired_human_resolution_is_agree(
    overlord_stub, decisions, capsys
):
    overlord_stub(["ACTION: rebrief\nRATIONALE: ok"])
    decisions([_ruling("s1", action="rebrief"), _resolution("s1", _T0, "rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["paired"] == 1
    assert tally["agree"] == 1
    assert tally["false_park"] == 0
    assert tally["false_proceed"] == 0


def test_replayed_park_against_human_rebrief_is_false_park(overlord_stub, decisions, capsys):
    overlord_stub(["ACTION: park_for_human\nRATIONALE: unsure"])
    decisions([_ruling("s1", action="rebrief"), _resolution("s1", _T0, "rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["false_park"] == 1
    assert tally["agree"] == 0


def test_replayed_rebrief_against_human_park_is_false_proceed(overlord_stub, decisions, capsys):
    overlord_stub(["ACTION: rebrief\nRATIONALE: ok"])
    decisions([_ruling("s1", action="rebrief"), _resolution("s1", _T0, "park")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["false_proceed"] == 1
    assert tally["agree"] == 0


def test_merge_proceed_against_human_merge_held_is_agree(overlord_stub, decisions, capsys):
    overlord_stub(["RULING: proceed\nRATIONALE: tests green"])
    decisions([_merge("s1", ruling="proceed"), _resolution("s1", _T0, "merge_held")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_MERGE_CLASS]

    assert tally["agree"] == 1
    assert tally["false_proceed"] == 0


def test_merge_proceed_against_other_human_answer_is_false_proceed(overlord_stub, decisions, capsys):
    overlord_stub(["RULING: proceed\nRATIONALE: tests green"])
    decisions([_merge("s1", ruling="proceed"), _resolution("s1", _T0, "rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_MERGE_CLASS]

    assert tally["false_proceed"] == 1


def test_replayed_merge_park_against_human_merge_held_is_false_park(overlord_stub, decisions, capsys):
    overlord_stub(["RULING: park\nRATIONALE: risky"])
    decisions([_merge("s1", ruling="park"), _resolution("s1", _T0, "merge_held")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_MERGE_CLASS]

    assert tally["false_park"] == 1
    assert tally["agree"] == 0


def test_replayed_merge_park_against_other_human_answer_is_agreement(
    overlord_stub, decisions, capsys
):
    overlord_stub(["RULING: park\nRATIONALE: risky"])
    decisions([_merge("s1", ruling="proceed"), _resolution("s1", _T0, "rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_MERGE_CLASS]

    assert tally["agree"] == 1
    assert tally["false_park"] == 0
    assert tally["flips"] == 1


def test_unpaired_replay_is_counted_but_not_scored(overlord_stub, decisions, capsys):
    overlord_stub(["ACTION: park_for_human\nRATIONALE: unsure"])
    decisions([_ruling("s1", action="rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["replayed"] == 1
    assert tally["paired"] == 0
    assert tally["false_park"] == 0


# --- flips and instability ---------------------------------------------------


def test_replayed_action_differing_from_recorded_is_one_flip(overlord_stub, decisions, capsys):
    overlord_stub(["ACTION: park_for_human\nRATIONALE: unsure"])
    decisions([_ruling("s1", action="rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["flips"] == 1


def test_replayed_action_matching_recorded_is_no_flip(overlord_stub, decisions, capsys):
    overlord_stub(["ACTION: rebrief\nRATIONALE: ok"])
    decisions([_ruling("s1", action="rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["flips"] == 0


def test_replayed_action_the_class_does_not_allow_is_scored_as_park(overlord_stub, decisions, capsys):
    overlord_stub(["ACTION: rebrief\nRATIONALE: not allowed for rebase_conflict"])
    record = _ruling("s1", action="resolve_rebase", input=_input(park_class=_REBASE_CLASS))
    decisions([record, _resolution("s1", _T0, "rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_REBASE_CLASS]

    assert tally["false_park"] == 1
    assert tally["flips"] == 1


def test_runs_that_disagree_with_each_other_are_unstable(overlord_stub, decisions, capsys):
    stub = overlord_stub(
        ["ACTION: rebrief\nRATIONALE: a", "ACTION: park_for_human\nRATIONALE: b", "ACTION: rebrief\nRATIONALE: c"]
    )
    decisions([_ruling("s1", action="rebrief")])

    tally = _report(capsys, ["--plan", "p1", "--runs", "3"])["p1"][_CLASS]

    assert len(stub.prompts) == 3
    assert tally["unstable"] == 1
    assert tally["flips"] == 0


def test_single_run_is_never_unstable(overlord_stub, decisions, capsys):
    overlord_stub(["ACTION: rebrief\nRATIONALE: a"])
    decisions([_ruling("s1", action="rebrief")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["unstable"] == 0


def test_failed_call_counts_as_error_and_holds_without_exception_text(
    overlord_stub, decisions, capsys
):
    overlord_stub(error=RuntimeError("boom-secret-token"))
    decisions([_ruling("s1", action="rebrief")])

    assert rp.main(["--plan", "p1", "--json"]) == 0
    out = capsys.readouterr()
    tally = json.loads(out.out)["p1"][_CLASS]

    assert tally["errors"] == 1
    assert tally["flips"] == 1
    assert "boom-secret-token" not in out.out + out.err


def test_unparseable_merge_reply_counts_as_unparseable_and_holds(overlord_stub, decisions, capsys):
    overlord_stub(["no ruling line at all"])
    decisions([_merge("s1", ruling="proceed"), _resolution("s1", _T0, "merge_held")])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_MERGE_CLASS]

    assert tally["unparseable"] == 1
    assert tally["flips"] == 1
    assert tally["false_park"] == 1


# --- prompt substitution -----------------------------------------------------


def test_policy_file_replaces_default_policy_and_brackets_evidence(
    overlord_stub, decisions, tmp_path, capsys
):
    stub = overlord_stub()
    policy = tmp_path / "candidate-policy.md"
    policy.write_text("CANDIDATE POLICY TEXT\n")
    decisions([_ruling("s1")])

    assert rp.main(["--plan", "p1", "--policy", str(policy)]) == 0

    prompt = stub.prompts[0]
    assert prompt.startswith("CANDIDATE POLICY TEXT\n")
    assert prompt.endswith(_TRIAGE_EVIDENCE)
    assert "POLICY\n" not in prompt


def test_default_policy_is_used_without_policy_flag(overlord_stub, decisions, capsys):
    stub = overlord_stub()
    decisions([_ruling("s1")])

    assert rp.main(["--plan", "p1"]) == 0

    assert stub.prompts[0] == "POLICY\n" + _TRIAGE_EVIDENCE


def test_context_dir_files_render_between_policy_and_evidence_in_name_order(
    overlord_stub, decisions, tmp_path, capsys
):
    stub = overlord_stub()
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "b.txt").write_text("BRAVO CONTEXT")
    (context / "a.md").write_text("ALPHA CONTEXT")
    decisions([_ruling("s1")])

    assert rp.main(["--plan", "p1", "--context", str(context)]) == 0

    prompt = stub.prompts[0]
    assert prompt.index("ALPHA CONTEXT") < prompt.index("BRAVO CONTEXT")
    assert "## Context source: a (" in prompt
    assert "## Context source: b (" in prompt
    assert prompt.index("POLICY\n") < prompt.index("ALPHA CONTEXT")
    assert prompt.index("BRAVO CONTEXT") < prompt.index(_TRIAGE_EVIDENCE.strip())
    assert prompt.endswith(_TRIAGE_EVIDENCE)


def test_context_dir_ignores_subdirectories(overlord_stub, decisions, tmp_path, capsys):
    stub = overlord_stub()
    context = tmp_path / "ctx"
    (context / "nested").mkdir(parents=True)
    (context / "nested" / "hidden.md").write_text("NESTED SECRET ORDER")
    decisions([_ruling("s1")])

    assert rp.main(["--plan", "p1", "--context", str(context)]) == 0

    assert "NESTED SECRET ORDER" not in stub.prompts[0]


def test_merge_context_lands_before_response_format_line(overlord_stub, decisions, tmp_path, capsys):
    stub = overlord_stub(["RULING: proceed\nRATIONALE: ok"])
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "rules.md").write_text("MERGE CONTEXT TEXT")
    decisions([_merge("s1")])

    assert rp.main(["--plan", "p1", "--context", str(context)]) == 0

    prompt = stub.prompts[0]
    marker = "Respond in this exact format:"
    assert prompt.index("MERGE CONTEXT TEXT") < prompt.index(marker)
    assert prompt.startswith(_MERGE_EVIDENCE.split(marker)[0])
    assert prompt.endswith(marker + "\nRULING: proceed|park\nRATIONALE: <one line>\n")


def test_merge_without_context_sends_evidence_unchanged(overlord_stub, decisions, capsys):
    stub = overlord_stub(["RULING: proceed\nRATIONALE: ok"])
    decisions([_merge("s1")])

    assert rp.main(["--plan", "p1"]) == 0

    assert stub.prompts[0] == _MERGE_EVIDENCE


def test_recorded_digests_are_rendered_when_no_context_dir_is_given(
    overlord_stub, decisions, snapshots, capsys
):
    stub = overlord_stub()
    text = "RECORDED SNAPSHOT TEXT"
    digest = _digest(text)
    snapshots({digest: text})
    decisions([_ruling("s1", input=_input(context_digests=[digest]))])

    assert rp.main(["--plan", "p1"]) == 0

    assert "RECORDED SNAPSHOT TEXT" in stub.prompts[0]
    assert stub.prompts[0].index("RECORDED SNAPSHOT TEXT") < stub.prompts[0].index(_TRIAGE_EVIDENCE.strip())


# --- skips --------------------------------------------------------------------


def test_record_without_input_is_skipped_as_no_input(overlord_stub, decisions, capsys):
    stub = overlord_stub()
    record = _ruling("s1")
    del record["input"]
    decisions([record])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["skipped"]["no_input"] == 1
    assert tally["replayed"] == 0
    assert stub.prompts == []


def test_truncated_record_is_skipped_as_truncated(overlord_stub, decisions, capsys):
    stub = overlord_stub()
    decisions([_ruling("s1", input=_input(truncated=True))])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["skipped"]["truncated"] == 1
    assert stub.prompts == []


def test_missing_recorded_snapshot_is_skipped_as_context_unavailable(
    overlord_stub, decisions, snapshots, capsys
):
    stub = overlord_stub()
    snapshots({})
    decisions([_ruling("s1", input=_input(context_digests=[_digest("gone")]))])

    tally = _report(capsys, ["--plan", "p1"])["p1"][_CLASS]

    assert tally["skipped"]["context_unavailable"] == 1
    assert stub.prompts == []


def test_context_unavailable_is_not_checked_when_context_dir_is_given(
    overlord_stub, decisions, snapshots, tmp_path, capsys
):
    stub = overlord_stub()
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "a.md").write_text("ALPHA")
    decisions([_ruling("s1", input=_input(context_digests=[_digest("gone")]))])

    tally = _report(capsys, ["--plan", "p1", "--context", str(context)])["p1"][_CLASS]

    assert tally["replayed"] == 1
    assert len(stub.prompts) == 1


def test_non_ruling_questions_are_not_replayed(overlord_stub, decisions, capsys):
    stub = overlord_stub()
    decisions([_resolution("s1", _T0, "rebrief"), {"question": "failure triage execution", "story_key": "s1"}])

    report = _report(capsys, ["--plan", "p1"])

    assert report["p1"] == {}
    assert stub.prompts == []


def test_unparseable_merge_record_with_empty_ruling_is_not_replayed(overlord_stub, decisions, capsys):
    stub = overlord_stub()
    decisions([_merge("s1", ruling="")])

    report = _report(capsys, ["--plan", "p1"])

    assert report["p1"] == {}
    assert stub.prompts == []


# --- limit ---------------------------------------------------------------------


def test_limit_one_replays_only_the_newest_ruling(overlord_stub, decisions, capsys):
    stub = overlord_stub()
    decisions(
        [
            _ruling("oldest", decided_at="2026-11-01T00:00:00+00:00"),
            _ruling("newest", decided_at="2026-11-03T00:00:00+00:00"),
            _ruling("middle", decided_at="2026-11-02T00:00:00+00:00"),
        ]
    )

    tally = _report(capsys, ["--plan", "p1", "--limit", "1"])["p1"][_CLASS]

    assert len(stub.prompts) == 1
    assert tally["replayed"] == 1


def test_limit_zero_is_refused(capsys):
    with pytest.raises(SystemExit) as refused:
        rp.main(["--plan", "p1", "--limit", "0"])

    assert refused.value.code == 2


def test_runs_above_five_are_refused(capsys):
    with pytest.raises(SystemExit) as refused:
        rp.main(["--plan", "p1", "--runs", "6"])

    assert refused.value.code == 2


# --- read-only ----------------------------------------------------------------


def _forbid(*args, **kwargs):
    raise AssertionError("replay must not write")


class _ReadOnlyStore:
    """Lists one plan; every write method fails the test if reached."""

    def list_manifests(self) -> list[str]:
        return ["p1"]

    def put_manifest(self, *args, **kwargs):
        _forbid()

    def append_decision(self, *args, **kwargs):
        _forbid()

    def append_notification(self, *args, **kwargs):
        _forbid()


def test_full_replay_of_all_plans_writes_nothing(
    monkeypatch, overlord_stub, decisions, capsys
):
    overlord_stub(["ACTION: park_for_human\nRATIONALE: unsure"])
    decisions([_ruling("s1", action="rebrief"), _resolution("s1", _T0, "rebrief")])
    monkeypatch.setattr(server, "_store", _ReadOnlyStore())
    monkeypatch.setattr(decision_log, "append_decision", _forbid)
    monkeypatch.setattr(triage, "execute_ruling", _forbid)
    monkeypatch.setattr(triage, "_apply_ruling_for_mode", _forbid)

    assert rp.main(["--all", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["p1"][_CLASS]["false_park"] == 1


# --- CLI ----------------------------------------------------------------------


def test_invalid_plan_name_exits_one_with_generic_message(capsys):
    assert rp.main(["--plan", "../escape"]) == 1

    captured = capsys.readouterr()
    assert captured.err == "error: invalid plan name\n"
    assert captured.out == ""


def test_unreadable_policy_exits_one_without_leaking_the_path(decisions, capsys, tmp_path):
    decisions([_ruling("s1")])
    missing = tmp_path / "secret-candidate-name.md"

    assert rp.main(["--plan", "p1", "--policy", str(missing)]) == 1

    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "secret-candidate-name" not in err
    assert str(tmp_path) not in err


def test_unreadable_context_dir_exits_one_without_leaking_the_path(decisions, capsys, tmp_path):
    decisions([_ruling("s1")])
    missing = tmp_path / "secret-context-dir"

    assert rp.main(["--plan", "p1", "--context", str(missing)]) == 1

    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "secret-context-dir" not in err


def test_unreadable_decisions_log_exits_one_with_generic_message(monkeypatch, capsys):
    def broken(plan):
        raise OSError("/private/path/p1.decisions.json denied")

    monkeypatch.setattr(decision_log, "read_decisions", broken)

    assert rp.main(["--plan", "p1"]) == 1

    err = capsys.readouterr().err
    assert err == "error: a decisions log could not be read\n"
