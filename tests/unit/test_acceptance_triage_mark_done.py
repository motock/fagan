"""Acceptance oracle: the ``mark_done`` triage action (OPSA-6).

The dominant historical park class (21 of 34 parks, ~19 wrongly labelled) is a
story whose live state contradicts its recorded ``parked_reason``. OPSA-6 turns
that into a first-class executable action:

* ``pipeline/parsers.py`` recognises ``mark_done`` (fail-closed normalization
  unchanged - an unknown ACTION still parks).
* ``pipeline/triage.py`` gains ``_execute_mark_done``, which is **fail-closed**:
  it corrects the story record only on corroborated LIVE evidence (new commits
  vs base, a merged ``pr_url``, or the suite passing at HEAD). An uncorroborated
  ``mark_done`` is the dangerous direction, so it parks loudly instead of
  trusting the overlord's word.
* ``overlord-policy.md`` publishes ``mark_done`` in the ACTION contract line and
  defines it in the Failure triage section.

The policy assertions here are deliberately membership/prefix based: the ACTION
contract line is a shared artifact that later sibling stories (``patch_acceptance``)
extend again, so this file must not pin its total contents.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import git_ops, parsers, server, triage

_POLICY = Path(__file__).resolve().parents[2] / "overlord-policy.md"

# The base contract every sibling story must keep as the line's prefix.
_BASE_CONTRACT = "ACTION: escalate_model | split_story | repo_issue | park_for_human"

# The exact fail-closed park reason the brief pins.
_PARK_REASON = "mark_done ruled but live evidence does not corroborate"

# Real-shaped strings from pipeline.triage._current_suite_state.
_SUITE_PASSES = "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."
_SUITE_FAILS = (
    "CURRENT STATE: full test suite FAILS at the worktree's current HEAD (rc=1):\nboom"
)

_EXECUTOR_PARAMS = (
    "plan_name",
    "story_key",
    "story",
    "ruling",
    "manifest",
    "manifest_path",
)

# OPSA-2's parked-story matrix + autonomy ladder: sibling content this story
# must not reword.
_SIBLING_ANCHORS = (
    "### Parked-story resolution",
    "Autonomy ladder:",
    "| stale bookkeeping",
    "| rework exhaustion with mechanical leftovers",
    "| repeated step-caps on oversized scope",
    "| acceptance fixture demonstrably broken at a clean baseline",
    "escalate_model",
    "split_story",
    "repo_issue",
    "park_for_human",
)


# ---------------------------------------------------------------------------
# policy helpers
# ---------------------------------------------------------------------------


def _policy_text() -> str:
    return _POLICY.read_text() if _POLICY.is_file() else ""


def _triage_section() -> str:
    return _policy_text().partition("## Failure triage")[2]


def _output_contract_section() -> str:
    return _policy_text().partition("## Output contract")[2]


def _action_contract_line() -> str:
    """The ``ACTION:`` line of the output-contract fenced block."""
    for line in _output_contract_section().splitlines():
        if line.strip().startswith("ACTION:"):
            return line.strip()
    return ""


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


# ---------------------------------------------------------------------------
# executor harness
# ---------------------------------------------------------------------------


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Isolate the executor: fake live probes, capture notify + decisions."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    calls = {"notify": [], "decisions": [], "commits": [], "suite": []}
    state = {"new_commits": False, "suite": ""}

    def fake_has_new_commits(worktree=None, story_key=None, base_branch=None, **kwargs):
        # Faithful to pipeline.git_ops._worktree_has_new_commits: no worktree or
        # no resolved base branch means no live evidence.
        calls["commits"].append(
            {"worktree": worktree, "story_key": story_key, "base_branch": base_branch}
        )
        if not worktree or not base_branch or str(worktree) in ("", "."):
            return False
        return state["new_commits"]

    def fake_suite_state(worktree=None, **kwargs):
        # Faithful to pipeline.triage._current_suite_state: silence on no worktree.
        calls["suite"].append(worktree)
        if not worktree or str(worktree) in ("", "."):
            return ""
        return state["suite"]

    monkeypatch.setattr(triage, "_worktree_has_new_commits", fake_has_new_commits)
    monkeypatch.setattr(triage, "_current_suite_state", fake_suite_state)
    monkeypatch.setattr(
        triage, "_notify_user", lambda *a, **k: calls["notify"].append((a, k))
    )
    monkeypatch.setattr(
        triage, "_append_decision", lambda plan, rec: calls["decisions"].append(rec)
    )

    # The base-branch resolver: patch the lazy-import source (pipeline.server)
    # and any module-level alias the implementation may add.
    monkeypatch.setattr(git_ops, "_worktree_has_new_commits", fake_has_new_commits)
    monkeypatch.setattr(server, "_default_branch", lambda: "master")
    if hasattr(triage, "_default_branch"):
        monkeypatch.setattr(triage, "_default_branch", lambda: "master")

    return SimpleNamespace(worktree=str(worktree), calls=calls, state=state)


def _story(harness, **over):
    story = {
        "key": "OPSA-6",
        "story_key": "OPSA-6",
        "summary": "mark_done executor",
        "status": "parked",
        "parked_reason": "no new commits vs master",
        "worktree": harness.worktree,
    }
    story.update(over)
    return story


def _run(harness, story, ruling=None, manifest=None, manifest_path=None):
    return triage._execute_mark_done(
        "plan-a",
        story["key"],
        story,
        ruling
        if ruling is not None
        else {"action": "mark_done", "rationale": "stale bookkeeping"},
        manifest if manifest is not None else {"stories": {story["key"]: story}},
        manifest_path
        if manifest_path is not None
        else Path("/tmp/plan-a.manifest.json"),
    )


def _probe_worktree(call):
    return call.get("worktree")


def _probe_base_branch(call):
    return call.get("base_branch")


# ---------------------------------------------------------------------------
# parsers: mark_done is a recognized action
# ---------------------------------------------------------------------------


def test_mark_done_is_a_recognized_triage_action():
    assert "mark_done" in parsers.TRIAGE_ACTIONS


def test_triage_actions_keep_the_existing_actions():
    # Membership, not equality: later sibling stories add patch_acceptance.
    assert parsers.TRIAGE_ACTIONS >= {
        "escalate_model",
        "split_story",
        "repo_issue",
        "park_for_human",
        "mark_done",
    }


def test_normalize_action_accepts_mark_done():
    assert parsers._normalize_action("mark_done") == "mark_done"
    assert parsers._normalize_action("  MARK_DONE  ") == "mark_done"


def test_parse_ruling_reads_a_mark_done_action():
    ruling = parsers._parse_ruling("ACTION: mark_done\nRATIONALE: stale bookkeeping")
    assert ruling["action"] == "mark_done"


def test_unknown_action_still_fails_closed_to_park_for_human():
    assert parsers._normalize_action("nonsense") == "park_for_human"
    assert parsers._normalize_action(None) == "park_for_human"
    assert parsers._parse_ruling("ACTION: nonsense")["action"] == "park_for_human"


# ---------------------------------------------------------------------------
# executor shape
# ---------------------------------------------------------------------------


def test_execute_mark_done_exists_with_the_executor_signature():
    assert hasattr(triage, "_execute_mark_done"), (
        "pipeline.triage must implement _execute_mark_done"
    )
    params = tuple(inspect.signature(triage._execute_mark_done).parameters)
    assert params == _EXECUTOR_PARAMS


def test_mark_done_is_not_a_deferred_action():
    assert "mark_done" not in triage.DEFERRED_ACTIONS


# ---------------------------------------------------------------------------
# corroborated -> done
# ---------------------------------------------------------------------------


def test_new_commits_vs_base_corroborates_and_marks_done(harness):
    harness.state["new_commits"] = True
    story = _story(harness, triage_deferred_action="mark_done")

    result = _run(harness, story)

    assert result == "mark_done"
    assert story["status"] == "done"
    assert "triage_deferred_action" not in story
    # The git probe is the live evidence: it must be consulted with the story's
    # worktree and a resolved base branch.
    assert harness.calls["commits"], "the new-commits probe must be consulted"
    assert str(_probe_worktree(harness.calls["commits"][0])) == harness.worktree
    assert _probe_base_branch(harness.calls["commits"][0]) == "master"


def test_pr_url_corroborates_and_marks_done(harness):
    harness.state["new_commits"] = False
    harness.state["suite"] = ""
    story = _story(harness, pr_url="https://github.com/o/r/pull/123")

    result = _run(harness, story)

    assert result == "mark_done"
    assert story["status"] == "done"


def test_suite_green_at_head_corroborates_and_marks_done(harness):
    harness.state["new_commits"] = False
    harness.state["suite"] = _SUITE_PASSES
    story = _story(harness)

    result = _run(harness, story)

    assert result == "mark_done"
    assert story["status"] == "done"
    assert harness.calls["suite"], "the live suite probe must be consulted"
    assert harness.calls["suite"][0] == harness.worktree


def test_mark_done_without_a_deferred_action_does_not_raise(harness):
    harness.state["new_commits"] = True
    story = _story(harness)
    assert "triage_deferred_action" not in story

    result = _run(harness, story)

    assert result == "mark_done"
    assert story["status"] == "done"


# ---------------------------------------------------------------------------
# uncorroborated -> park loudly (fail closed)
# ---------------------------------------------------------------------------


def test_uncorroborated_mark_done_parks_loudly(harness):
    harness.state["new_commits"] = False
    harness.state["suite"] = ""
    story = _story(harness)

    result = _run(harness, story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == _PARK_REASON
    assert harness.calls["notify"], "an uncorroborated mark_done must notify"


def test_suite_failing_at_head_does_not_corroborate(harness):
    harness.state["new_commits"] = False
    harness.state["suite"] = _SUITE_FAILS
    story = _story(harness)

    result = _run(harness, story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == _PARK_REASON


def test_empty_worktree_and_no_pr_url_parks(harness):
    harness.state["new_commits"] = True  # probe would corroborate if consulted
    harness.state["suite"] = _SUITE_PASSES
    story = _story(harness, worktree="")

    result = _run(harness, story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == _PARK_REASON


def test_missing_worktree_key_and_no_pr_url_parks(harness):
    story = _story(harness)
    del story["worktree"]

    result = _run(harness, story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == _PARK_REASON


def test_git_probe_exception_fails_closed_to_park(harness, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(triage, "_worktree_has_new_commits", boom)
    story = _story(harness)

    result = _run(harness, story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == _PARK_REASON


def test_suite_probe_exception_fails_closed_to_park(harness, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("suite exploded")

    monkeypatch.setattr(triage, "_current_suite_state", boom)
    story = _story(harness)

    result = _run(harness, story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == _PARK_REASON


# ---------------------------------------------------------------------------
# execution record (OPSA-3 seam)
# ---------------------------------------------------------------------------


def test_execution_record_captures_prior_state_before_mutation(harness, monkeypatch):
    harness.state["new_commits"] = True
    story = _story(harness, parked_reason="no new commits vs master")

    # The execution record is written by the production entry point
    # _apply_ruling_for_mode - which snapshots the priors BEFORE the mutation
    # and records with the real autonomy mode - not by the private executor
    # (recording in both places wrote two records per ruling).
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "gated", raising=False)
    triage._apply_ruling_for_mode(
        "plan-a",
        story["key"],
        story,
        {"action": "mark_done", "rationale": "stale bookkeeping"},
        {"stories": {story["key"]: story}},
        Path("/tmp/plan-a.manifest.json"),
    )

    assert len(harness.calls["decisions"]) == 1
    record = harness.calls["decisions"][0]
    assert record["story_key"] == "OPSA-6"
    assert record["action"] == "mark_done"
    assert record["mode"] == "gated"
    assert record["prior_status"] == "parked"
    assert record["prior_parked_reason"] == "no new commits vs master"
    # The snapshot is the state BEFORE the mutation.
    assert story["status"] == "done"


def test_execution_record_is_written_for_the_pr_url_path(harness, monkeypatch):
    harness.state["new_commits"] = False
    harness.state["suite"] = ""
    story = _story(harness, pr_url="https://github.com/o/r/pull/9")

    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "gated", raising=False)
    triage._apply_ruling_for_mode(
        "plan-a",
        story["key"],
        story,
        {"action": "mark_done", "rationale": "stale bookkeeping"},
        {"stories": {story["key"]: story}},
        Path("/tmp/plan-a.manifest.json"),
    )

    assert len(harness.calls["decisions"]) == 1
    assert harness.calls["decisions"][0]["prior_status"] == "parked"


# ---------------------------------------------------------------------------
# execute_ruling dispatch
# ---------------------------------------------------------------------------


def test_execute_ruling_routes_mark_done_to_the_executor(harness):
    harness.state["new_commits"] = True
    story = _story(harness)
    manifest = {"stories": {story["key"]: story}}

    result = triage.execute_ruling(
        "plan-a",
        story["key"],
        story,
        {"action": "mark_done", "rationale": "stale bookkeeping"},
        manifest,
        Path("/tmp/plan-a.manifest.json"),
    )

    assert result == "mark_done"
    assert story["status"] == "done"


def test_execute_ruling_does_not_bypass_the_corroboration_gate(harness):
    harness.state["new_commits"] = False
    harness.state["suite"] = ""
    story = _story(harness)
    manifest = {"stories": {story["key"]: story}}

    result = triage.execute_ruling(
        "plan-a",
        story["key"],
        story,
        {"action": "mark_done", "rationale": "stale bookkeeping"},
        manifest,
        Path("/tmp/plan-a.manifest.json"),
    )

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == _PARK_REASON


# ---------------------------------------------------------------------------
# overlord-policy.md
# ---------------------------------------------------------------------------


def test_policy_file_is_present():
    assert _POLICY.is_file()


def test_policy_action_contract_line_gains_mark_done():
    line = _action_contract_line()
    assert line, "the output contract must keep an ACTION: line"
    assert line.startswith(_BASE_CONTRACT), (
        "the ACTION contract line must keep the base actions in order"
    )
    assert "mark_done" in line, (
        "the ACTION contract line must be extended with mark_done"
    )


def test_policy_action_contract_line_no_longer_ends_at_park_for_human():
    assert _BASE_CONTRACT + "\n" not in _policy_text()


def test_policy_failure_triage_defines_mark_done_from_live_evidence():
    section = _triage_section()
    assert section, "overlord-policy.md must keep a '## Failure triage' section"
    candidates = [
        paragraph
        for paragraph in _paragraphs(section)
        if "mark_done" in paragraph
        and re.search(r"corroborat", paragraph, re.IGNORECASE)
        and re.search(r"parked[_ ]reason", paragraph, re.IGNORECASE)
        and re.search(r"\blive\b", paragraph, re.IGNORECASE)
    ]
    assert candidates, (
        "the Failure triage section must define mark_done: correct the record "
        "only when live git/suite evidence corroborates, never on parked_reason text"
    )


def test_policy_failure_triage_sibling_content_is_not_reworded():
    section = _triage_section()
    missing = [anchor for anchor in _SIBLING_ANCHORS if anchor not in section]
    assert missing == []
