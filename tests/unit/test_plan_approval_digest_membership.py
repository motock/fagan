"""The approval digest binds the plan's descriptions and the plan's team.

USER DECISION 2026-11-07: the plan-approval digest now covers *every* piece
of content a reviewer is asked to approve.  That means each story's
``description`` and the plan's ``team`` join the digest, and the digest
version is bumped so every version-1 approval fails closed instead of
staying in force against a digest it no longer matches.

Contract graded here (``pipeline/plan_approval.py`` unless named):

* ``canonical_digest(manifest)`` changes when *any* digest member changes:
  the plan-level ``repo_root``, ``role_config``, ``context`` and ``team``, and
  each story's authored fields plus its ``description``.  Adding, removing or
  renaming a story changes it too.
* It does NOT change when runtime/story state changes (``status``,
  ``attempts``, ``pr_url``, ``worktree``, ``review_verdict``,
  ``review_verdict_sha``, ``sizing_auto_routed``, ``log``), when the
  descriptive-but-not-plan-defining fields change (``epic`` and ``plan_key``
  on a story; ``plan_epics``, ``preflight_override`` and ``repo_url`` on the
  plan) or when the plan's non-authored fields change (``approval``,
  ``owner``, ``delegates``, ``ingested_by``, ``epics``).
* The set of digest members is a *superset* of the story fields the pipeline
  ingests as authored, so a field the pipeline treats as authored cannot be
  edited without resetting the approval.
* ``APPROVAL_DIGEST_VERSION`` is 2, and a record written under version 1
  (including one with no ``digest_version`` at all) is not activatable and
  not honoured: re-ingesting such a manifest yields ``pending_approval``.
* Changing only a story's ``description`` -- or only the plan's ``team`` --
  and re-ingesting resets the approval to ``pending_approval``, keeps the
  previous decision metadata and sends the ``plan_pending_approval`` notice.
* ``patch_story`` still refuses to edit ``description``.

``docs/specs/PLAN_APPROVAL.md`` pins the field lists, the digest version, the
reset rule and the review-payload fields; ``REFERENCE.md`` and
``CHANGELOG.md`` pin the user-visible change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import plan_approval
from pipeline.plan_approval import canonical_digest

ROOT = Path(__file__).resolve().parents[2]

#: The plan-level fields the digest covers.  A *superset* may be covered too:
#: a later story may add fields.  What must hold is that each of these is
#: covered.
PLAN_MEMBERS = ("repo_root", "role_config", "context", "team")

#: The story-level fields the digest covers.  Same superset rule.
STORY_MEMBERS = (
    "summary",
    "agent_instructions",
    "dependencies",
    "persona",
    "model",
    "acceptance",
    "risk",
    "backend",
    "tdd_split",
    "files",
    "blocked_by",
    "description",
)

#: Runtime / story-state fields that must never change the digest.
RUNTIME_FIELDS = (
    "status",
    "attempts",
    "pr_url",
    "worktree",
    "review_verdict",
    "review_verdict_sha",
    "sizing_auto_routed",
    "log",
)

#: Descriptive fields that stay OUT of the digest.
NON_MEMBER_STORY_FIELDS = ("epic", "plan_key")
NON_MEMBER_PLAN_FIELDS = ("plan_epics", "preflight_override", "repo_url")

#: Plan-level fields that are recorded on the manifest but are not authored
#: plan content.
NON_MEMBER_PLAN_STATE_FIELDS = ("owner", "delegates", "ingested_by", "epics")


def _story(**overrides) -> dict:
    """A story carrying every field the digest may look at."""
    story = {
        "summary": "ship the thing",
        "agent_instructions": "Do the work.\n",
        "dependencies": [],
        "persona": "software-engineer",
        "model": "sonnet",
        "acceptance": "tests pass",
        "risk": "low",
        "backend": "claude",
        "tdd_split": False,
        "files": ["src/a.py"],
        "blocked_by": [],
        "description": "A story description.",
        "epic": "epic-1",
        "plan_key": "plan-1",
    }
    story.update(overrides)
    return story


def _manifest(**overrides) -> dict:
    """A manifest with one story and every field the digest may look at."""
    manifest = {
        "repo_root": "/repo",
        "role_config": {"reviewer": "alice"},
        "context": {"notes": "hello"},
        "team": "core",
        "stories": {"story-1": _story()},
        "plan_epics": {"epic-1": "Epic one"},
        "preflight_override": "pytest -q",
        "repo_url": "https://example.invalid/repo.git",
        "owner": "alice",
        "delegates": ["bob"],
        "ingested_by": "alice",
        "epics": {"epic-1": {"summary": "Epic one"}},
    }
    manifest.update(overrides)
    return manifest


# --------------------------------------------------------------------------- #
# 1. Positive: every member changes the digest
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "field,old,new",
    [
        ("repo_root", "/repo", "/other-repo"),
        ("role_config", {"reviewer": "alice"}, {"reviewer": "bob"}),
        ("context", {"notes": "hello"}, {"notes": "goodbye"}),
        ("team", "core", "payments"),
    ],
)
def test_plan_level_member_change_changes_the_digest(field, old, new):
    manifest = _manifest(**{field: old})
    changed = _manifest(**{field: new})
    assert canonical_digest(manifest) != canonical_digest(changed)


@pytest.mark.parametrize(
    "field,old,new",
    [
        ("summary", "ship the thing", "ship another thing"),
        ("agent_instructions", "Do the work.\n", "Do other work.\n"),
        ("dependencies", [], ["story-2"]),
        ("persona", "software-engineer", "security-engineer"),
        ("model", "sonnet", "opus"),
        ("acceptance", "tests pass", "tests really pass"),
        ("risk", "low", "high"),
        ("backend", "claude", "codex"),
        ("tdd_split", False, True),
        ("files", ["src/a.py"], ["src/b.py"]),
        ("blocked_by", [], ["plan-0:story-9"]),
        ("description", "A story description.", "Another story description."),
    ],
)
def test_story_level_member_change_changes_the_digest(field, old, new):
    manifest = _manifest(stories={"story-1": _story(**{field: old})})
    changed = _manifest(stories={"story-1": _story(**{field: new})})
    assert canonical_digest(manifest) != canonical_digest(changed)


def test_adding_a_story_changes_the_digest():
    manifest = _manifest()
    changed = _manifest(
        stories={"story-1": _story(), "story-2": _story(summary="another")}
    )
    assert canonical_digest(manifest) != canonical_digest(changed)


def test_removing_a_story_changes_the_digest():
    manifest = _manifest(
        stories={"story-1": _story(), "story-2": _story(summary="another")}
    )
    changed = _manifest(stories={"story-1": _story()})
    assert canonical_digest(manifest) != canonical_digest(changed)


def test_renaming_a_story_key_changes_the_digest():
    manifest = _manifest(stories={"story-1": _story()})
    changed = _manifest(stories={"story-2": _story()})
    assert canonical_digest(manifest) != canonical_digest(changed)


def test_description_content_change_changes_the_digest():
    manifest = _manifest(stories={"story-1": _story(description="a")})
    changed = _manifest(stories={"story-1": _story(description="b")})
    assert canonical_digest(manifest) != canonical_digest(changed)


def test_absent_description_and_empty_description_are_different_digests():
    """A legacy manifest (no ``description``) and one with ``""`` differ.

    That is intended: the backfill lands before this story, so a legacy
    manifest and a backfilled one are not the same content.
    """
    without = _manifest(stories={"story-1": _story()})
    without["stories"]["story-1"].pop("description")
    empty = _manifest(stories={"story-1": _story(description="")})
    assert canonical_digest(without) != canonical_digest(empty)


def test_absent_team_and_explicit_team_are_different_digests():
    without = _manifest()
    without.pop("team")
    with_team = _manifest(team="core")
    assert canonical_digest(without) != canonical_digest(with_team)


# --------------------------------------------------------------------------- #
# 2. Negative: non-members do NOT change the digest
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("field", RUNTIME_FIELDS)
def test_runtime_story_state_does_not_change_the_digest(field):
    manifest = _manifest(stories={"story-1": _story()})
    changed = _manifest(stories={"story-1": _story(**{field: "runtime-value"})})
    assert canonical_digest(manifest) == canonical_digest(changed)


@pytest.mark.parametrize("field", NON_MEMBER_STORY_FIELDS)
def test_descriptive_story_fields_do_not_change_the_digest(field):
    manifest = _manifest(stories={"story-1": _story()})
    changed = _manifest(stories={"story-1": _story(**{field: "something-else"})})
    assert canonical_digest(manifest) == canonical_digest(changed)


@pytest.mark.parametrize("field", NON_MEMBER_PLAN_FIELDS)
def test_descriptive_plan_fields_do_not_change_the_digest(field):
    manifest = _manifest()
    changed = _manifest()
    changed[field] = {"other": "value"} if field == "plan_epics" else "other-value"
    assert canonical_digest(manifest) == canonical_digest(changed)


@pytest.mark.parametrize("field", NON_MEMBER_PLAN_STATE_FIELDS)
def test_plan_level_non_authored_fields_do_not_change_the_digest(field):
    manifest = _manifest()
    changed = _manifest()
    changed[field] = {"other": "value"} if field == "epics" else ["carol"]
    assert canonical_digest(manifest) == canonical_digest(changed)


def test_approval_block_does_not_change_the_digest():
    manifest = _manifest()
    changed = _manifest(
        approval={
            "status": "approved",
            "digest": canonical_digest(manifest),
            "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
            "decided_by": "alice",
        }
    )
    assert canonical_digest(manifest) == canonical_digest(changed)


# --------------------------------------------------------------------------- #
# 3. Parity with the pipeline's own notion of authored story fields
# --------------------------------------------------------------------------- #

def test_every_ingest_authored_story_field_is_a_digest_member():
    from pipeline import server

    for field in server._INGEST_AUTHORED_STORY_FIELDS:
        manifest = _manifest(stories={"story-1": _story()})
        changed = _manifest(stories={"story-1": _story(**{field: "mutated"})})
        assert canonical_digest(manifest) != canonical_digest(changed), (
            f"{field} is treated as authored by the pipeline but does not "
            f"change the approval digest"
        )


def test_the_descriptive_story_field_description_is_a_digest_member():
    """The manifest-fields story's descriptive tuple: only ``description``.

    ``epic`` and ``plan_key`` stay out (asserted above).
    """
    manifest = _manifest(stories={"story-1": _story()})
    changed = _manifest(stories={"story-1": _story(description="mutated")})
    assert canonical_digest(manifest) != canonical_digest(changed)


# --------------------------------------------------------------------------- #
# 4. Version: the bump makes every version-1 approval fail closed
# --------------------------------------------------------------------------- #

def test_the_digest_version_is_2():
    assert plan_approval.APPROVAL_DIGEST_VERSION == 2


def test_a_version_1_record_without_the_field_is_not_activatable(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "1")
    manifest = _manifest(
        approval={"status": "approved", "digest": canonical_digest(_manifest())}
    )
    assert plan_approval.is_activatable(manifest) is False


def test_a_version_1_record_with_an_explicit_version_is_not_activatable(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "1")
    manifest = _manifest(
        approval={
            "status": "approved",
            "digest": canonical_digest(_manifest()),
            "digest_version": 1,
        }
    )
    assert plan_approval.is_activatable(manifest) is False


def test_reingesting_a_version_1_record_yields_pending_approval(monkeypatch):
    """A version-1 digest cannot be honoured: the record resets to pending."""
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "1")
    original = _manifest()
    manifest = _manifest(
        approval={
            "status": "approved",
            "digest": canonical_digest(original),
            "decided_by": "alice",
        }
    )
    result = plan_approval.on_ingest(manifest)
    assert result["status"] == "pending_approval"
    assert result["digest_version"] == 2
    assert plan_approval.is_activatable(result) is False


def test_a_current_version_record_stays_approved(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "1")
    manifest = _manifest()
    manifest["approval"] = {
        "status": "approved",
        "digest": canonical_digest(manifest),
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
    }
    result = plan_approval.on_ingest(manifest)
    assert result["status"] == "approved"
    assert result["digest_version"] == 2


# --------------------------------------------------------------------------- #
# 5. Boundary cases of the digest itself
# --------------------------------------------------------------------------- #

def test_an_empty_stories_mapping_and_a_missing_one_hash_the_same():
    assert canonical_digest(_manifest(stories={})) == canonical_digest(
        _manifest(stories={})
    )
    manifest_without = _manifest()
    manifest_without.pop("stories")
    assert canonical_digest(manifest_without) == canonical_digest(_manifest(stories={}))


def test_a_non_dict_story_entry_is_ignored():
    manifest = _manifest(stories={"story-1": _story(), "broken": "not-a-story"})
    assert canonical_digest(manifest) == canonical_digest(
        _manifest(stories={"story-1": _story()})
    )


def test_canonical_digest_does_not_mutate_the_manifest():
    manifest = _manifest()
    before = json.dumps(manifest, sort_keys=True)
    canonical_digest(manifest)
    assert json.dumps(manifest, sort_keys=True) == before
