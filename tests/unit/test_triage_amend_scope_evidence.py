"""amend_scope acting on scope-gate and guard-demands-path evidence (ov-4).

The executor accepts a recorded ``scope_conflict`` (covered by
``test_triage_scope_actions.py``) and, additionally, the live facts of the
``scope_gate`` and ``guard_demands_path`` park classes. These tests pin the
new evidence paths and their guardrails.
"""

from __future__ import annotations

import pytest

from pipeline import plan_approval, server, triage
from pipeline import triage_scope_actions as tsa
from pipeline.triage_scope_actions import _execute_amend_scope, check_scope_amendment

HEAD = "a" * 40
PLAN = "plan"
KEY = "S"
DECLARED = ["pipeline/forge.py", "docs/guide.md"]

# The h-2 shape: the brief names both paths that the scope gate blocks.
H2_BRIEF = (
    "Implement the GitHub adapter in pipeline/forge_github.py and move the "
    "fetch helper into pipeline/forge_fetch.py."
)
H2_REVIEW = (
    "## Review\n"
    "- Blocking: pipeline/forge_github.py: outside this story's `files` scope\n"
    "- Blocking: pipeline/forge_fetch.py: outside this story's `files` scope\n"
)
GUARD_OUTPUT = (
    "FAILED tests/unit/test_forge_guard.py::test_listed\n"
    "guard: pipeline/forge_cache.py must be listed in files\n"
)


def _story(**over):
    story = {
        "status": "parked",
        "risk": "low",
        "files": list(DECLARED),
        "worktree": "/tmp/does-not-matter",
        "agent_instructions": H2_BRIEF,
        "review_feedback": H2_REVIEW,
        "parked_reason": "scope gate blocked review feedback",
    }
    story.update(over)
    return story


def _guard_story(**over):
    story = _story(
        agent_instructions="Implement the cache in pipeline/forge.py.",
        review_feedback="",
        last_test_check={"returncode": 1, "sha": HEAD, "stdout_tail": GUARD_OUTPUT},
    )
    story.update(over)
    return story


@pytest.fixture
def env(monkeypatch):
    notes = []
    monkeypatch.setattr(
        tsa, "_notify_user", lambda plan, msg, **kw: notes.append((msg, kw)), raising=False
    )
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(tsa, "_worktree_head", lambda story: HEAD)
    return notes


def _run(story, rationale="because"):
    manifest = {"stories": {KEY: story}}
    result = _execute_amend_scope(
        PLAN, KEY, story, {"action": "amend_scope", "rationale": rationale}, manifest
    )
    return result, manifest


# ---- scope_gate evidence ---------------------------------------------------


def test_should_widen_files_and_unpark_for_h2_scope_gate_fixture(env):
    story = _story()

    result, _ = _run(story)

    assert result == "amend_scope"
    assert story["files"] == [*DECLARED, "pipeline/forge_github.py", "pipeline/forge_fetch.py"]
    assert story["status"] == "todo"
    assert "parked_reason" not in story
    assert "review_feedback" not in story
    assert [e["evidence"] for e in story["scope_amendments"]] == ["scope_gate", "scope_gate"]


def test_should_refuse_scope_gate_path_that_brief_and_guard_do_not_name(env):
    story = _story(
        review_feedback="- Blocking: pipeline/forge_extra.py: outside this story's `files` scope\n"
    )

    result, _ = _run(story)

    assert result == "park_for_human"
    assert story["files"] == DECLARED
    assert "scope_amendments" not in story
    assert "unnamed_path" in story["parked_reason"]
    assert "forge_extra" not in story["parked_reason"]


def test_should_not_treat_a_longer_filename_as_naming_the_path(env):
    story = _story(
        agent_instructions="Edit pipeline/forge_github.pyc only.",
        review_feedback="- Blocking: pipeline/forge_github.py: outside this story's `files` scope\n",
    )

    result, _ = _run(story)

    assert result == "park_for_human"
    assert "unnamed_path" in story["parked_reason"]


def test_should_accept_exactly_three_paths_and_refuse_four(env):
    three = _story(
        agent_instructions="Touch pipeline/a_one.py, pipeline/a_two.py and pipeline/a_three.py.",
        review_feedback=(
            "- Blocking: pipeline/a_one.py: outside this story's `files` scope\n"
            "- Blocking: pipeline/a_two.py: outside this story's `files` scope\n"
            "- Blocking: pipeline/a_three.py: outside this story's `files` scope\n"
        ),
    )
    four = _story(
        agent_instructions=(
            "Touch pipeline/a_one.py, pipeline/a_two.py, pipeline/a_three.py and pipeline/a_four.py."
        ),
        review_feedback=(
            three["review_feedback"]
            + "- Blocking: pipeline/a_four.py: outside this story's `files` scope\n"
        ),
    )

    assert _run(three)[0] == "amend_scope"
    result, _ = _run(four)
    assert result == "park_for_human"
    assert four["files"] == DECLARED
    assert "too_many_paths" in four["parked_reason"]


@pytest.mark.parametrize(
    "path",
    [
        "pipeline/overlord-authority.json",
        "pipeline/security.py",
        "pipeline/secrets.py",
        "pipeline/keys.py",
        "pipeline/uv.lock",
        "pipeline/package-lock.json",
    ],
)
def test_should_refuse_deny_listed_paths_named_by_the_brief(path):
    allowed, refused = check_scope_amendment(
        _story(), [path], evidence="scope_gate", named_text=path
    )

    assert allowed == []
    assert refused == {path: "denied_path"}


@pytest.mark.parametrize("path", ["../pipeline/x.py", "/pipeline/x.py", "pipeline\\x.py"])
def test_should_refuse_traversal_absolute_and_non_posix_for_new_evidence(path):
    allowed, refused = check_scope_amendment(
        _story(), [path], evidence="scope_gate", named_text=path
    )

    assert allowed == []
    assert refused == {path: "malformed_path"}


def test_should_never_add_a_test_file(env):
    allowed, refused = check_scope_amendment(
        _story(),
        ["pipeline/test_forge_fetch.py"],
        evidence="scope_gate",
        named_text="pipeline/test_forge_fetch.py",
    )

    assert allowed == []
    assert refused == {"pipeline/test_forge_fetch.py": "test_path"}


def test_should_refuse_unnamed_path_for_new_evidence_but_not_for_scope_conflict():
    allowed, _ = check_scope_amendment(_story(), ["pipeline/bar.py"], evidence="scope_gate")
    assert allowed == []

    allowed, refused = check_scope_amendment(_story(), ["pipeline/bar.py"])
    assert allowed == ["pipeline/bar.py"]
    assert refused == {}


def test_should_refuse_third_new_evidence_amendment(env):
    prior = [
        {"path": "pipeline/a.py", "evidence": "scope_gate", "at": "2026-11-01T00:00:00+00:00"},
        {"path": "pipeline/b.py", "evidence": "guard_demands_path", "at": "2026-11-02T00:00:00+00:00"},
    ]
    story = _story(scope_amendments=prior)
    allowed, refused = check_scope_amendment(
        story, ["pipeline/forge_github.py"], evidence="scope_gate", named_text=H2_BRIEF
    )

    assert allowed == []
    assert refused == {"pipeline/forge_github.py": "amendment_limit"}


def test_should_allow_second_new_evidence_amendment(env):
    prior = [{"path": "pipeline/a.py", "evidence": "scope_gate", "at": "2026-11-01T00:00:00+00:00"}]
    allowed, refused = check_scope_amendment(
        _story(scope_amendments=prior),
        ["pipeline/forge_github.py"],
        evidence="scope_gate",
        named_text=H2_BRIEF,
    )

    assert allowed == ["pipeline/forge_github.py"]
    assert refused == {}


def test_should_refuse_when_no_scope_evidence_is_recorded(env):
    story = _story(agent_instructions="", review_feedback="")

    result, _ = _run(story)

    assert result == "park_for_human"
    assert story["parked_reason"] == "amend_scope ruled but no scope conflict is recorded"


def test_should_refuse_high_risk_scope_gate_story(env):
    story = _story(risk="high")

    result, _ = _run(story)

    assert result == "park_for_human"
    assert story["files"] == DECLARED


# ---- guard_demands_path evidence -------------------------------------------


def test_should_widen_files_for_path_named_by_failing_guard(env):
    story = _guard_story()

    result, _ = _run(story)

    assert result == "amend_scope"
    assert story["files"][-1] == "pipeline/forge_cache.py"
    assert story["scope_amendments"][0]["evidence"] == "guard_demands_path"


def test_should_refuse_guard_path_that_is_a_test_file(env):
    story = _guard_story(
        last_test_check={
            "returncode": 1,
            "sha": HEAD,
            "stdout_tail": "guard: pipeline/test_forge_cache.py must be listed in files\n",
        }
    )

    result, _ = _run(story)

    assert result == "park_for_human"
    assert story["files"] == DECLARED


def test_should_prefer_scope_conflict_over_derived_evidence(env):
    story = _story(scope_conflict={"paths": ["pipeline/forge_github.py"], "reasons": {}})

    result, _ = _run(story)

    assert result == "amend_scope"
    assert story["files"] == [*DECLARED, "pipeline/forge_github.py"]
    assert story["scope_amendments"][0]["evidence"] == "scope_conflict"


# ---- approval digest -------------------------------------------------------


def _approved_manifest(story, status="approved", digest=None):
    manifest = {"stories": {KEY: story}}
    manifest["approval"] = {
        "status": status,
        "digest": digest if digest is not None else plan_approval.canonical_digest(manifest),
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
        "decided_by": "alice",
    }
    return manifest


@pytest.fixture
def approval_on(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")


def _execute_with(manifest, story):
    return _execute_amend_scope(
        PLAN, KEY, story, {"action": "amend_scope", "rationale": "x"}, manifest
    )


def test_should_restamp_and_keep_approval_after_additions_only_amendment(env, approval_on):
    story = _story()
    manifest = _approved_manifest(story)
    digest_before = manifest["approval"]["digest"]

    result = _execute_with(manifest, story)

    assert result == "amend_scope"
    approval = manifest["approval"]
    assert approval["status"] == "approved"
    assert approval["decided_by"] == "alice"
    assert approval["digest"] == plan_approval.canonical_digest(manifest)
    assert approval["digest"] != digest_before
    assert approval["digest_version"] == plan_approval.APPROVAL_DIGEST_VERSION


def test_should_record_digest_before_and_after_on_each_amendment(env, approval_on):
    story = _story()
    manifest = _approved_manifest(story)
    digest_before = manifest["approval"]["digest"]

    _execute_with(manifest, story)

    entries = story["scope_amendments"]
    assert {e["digest_before"] for e in entries} == {digest_before}
    assert {e["digest_after"] for e in entries} == {plan_approval.canonical_digest(manifest)}


def test_should_reset_to_pending_when_plan_was_not_approved_before(env, approval_on):
    story = _story()
    manifest = _approved_manifest(story, status="pending_approval")

    _execute_with(manifest, story)

    assert manifest["approval"]["status"] == "pending_approval"
    assert manifest["approval"]["digest"] == plan_approval.canonical_digest(manifest)


def test_should_reset_to_pending_when_approval_digest_was_already_stale(env, approval_on):
    story = _story()
    manifest = _approved_manifest(story, digest="stale")

    _execute_with(manifest, story)

    assert manifest["approval"]["status"] == "pending_approval"
    assert "decided_by" not in manifest["approval"]


def test_should_not_restamp_when_a_path_is_removed(approval_on):
    manifest = {"stories": {KEY: _story()}}
    manifest["approval"] = {"status": "approved", "digest": "stale", "digest_version": 2}

    tsa._settle_approval(manifest, was_approved=True, additions_only=False)

    assert manifest["approval"]["status"] == "pending_approval"


def test_should_leave_approval_untouched_while_gate_is_disabled(env, monkeypatch):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    story = _story()
    manifest = {"stories": {KEY: story}}

    _execute_with(manifest, story)

    assert "approval" not in manifest


# ---- dry-run ---------------------------------------------------------------


def test_dry_run_leaves_guard_evidence_story_untouched(monkeypatch):
    recorded = []
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "dry-run")
    monkeypatch.setattr(triage, "_append_decision", lambda plan, rec: recorded.append(rec))
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        triage, "_execute_amend_scope", lambda *a: pytest.fail("must not execute")
    )
    story = _guard_story()

    result = triage._apply_ruling_for_mode(
        PLAN, KEY, story, {"action": "amend_scope", "rationale": "x"}, {}
    )

    assert result == "dry-run"
    assert story["files"] == DECLARED
    assert story["status"] == "parked"
