"""Unit tests for pipeline.file_size_policy_dispatch: dispatch-time file_size_policy stamping.

The policy settings, the environment and the notification sink are the only
external boundaries stubbed here; the resolver itself runs for real.
"""

from datetime import datetime
from pathlib import Path

import pytest

from pipeline import file_size_policy_dispatch as fsp
from pipeline import server as p
from pipeline import test_scope_dispatch as tsd

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(fsp.FILE_SIZE_POLICY_ENV, raising=False)
    monkeypatch.delenv(tsd.TEST_SCOPE_ENV, raising=False)


@pytest.fixture
def notifications(monkeypatch):
    sent = []

    def _fake(plan_name, message, **kwargs):
        sent.append((plan_name, message, kwargs))

    monkeypatch.setattr(fsp, "_notify_user", _fake)
    return sent


def _stamp(settings=None, story=None):
    return fsp.stamp_file_size_policy(
        "plan-a",
        "S1",
        settings if settings is not None else {},
        story if story is not None else {},
    )


# ---------- stamping: resolution and source ----------
def test_stamps_advisory_default_when_no_layer_is_set(notifications):
    story = {}
    result = _stamp(story=story)
    assert (result["file_size_policy"], result["source"]) == ("advisory", "default")
    assert story["file_size_policy_resolution"] == result


def test_stamps_resolved_at_as_iso_timestamp(notifications):
    result = _stamp()
    assert datetime.fromisoformat(result["resolved_at"]).tzinfo is not None


def test_stamps_org_source_from_org_policy(notifications):
    result = _stamp(settings={"org": {"file_size_policy": "enforced"}})
    assert (result["file_size_policy"], result["source"]) == ("enforced", "org")


def test_stamps_env_source_from_environment(notifications, monkeypatch):
    monkeypatch.setenv(fsp.FILE_SIZE_POLICY_ENV, "enforced")
    result = _stamp()
    assert (result["file_size_policy"], result["source"]) == ("enforced", "env")


def test_unknown_env_value_stamps_advisory_and_records_invalid_layer(
    notifications,
    monkeypatch,
):
    monkeypatch.setenv(fsp.FILE_SIZE_POLICY_ENV, "strict")
    result = _stamp()
    assert result["file_size_policy"] == "advisory"
    assert result["invalid_layers"] == ["env"]


def test_settings_are_not_mutated_by_stamping(notifications):
    settings = {"org": {"file_size_policy": "enforced"}}
    _stamp(settings=settings)
    assert settings == {"org": {"file_size_policy": "enforced"}}


# ---------- lock rejection notification ----------
def test_notifies_once_with_lock_rejected_event_when_team_loosens_org_enforced(
    notifications,
):
    settings = {
        "org": {"file_size_policy": "enforced"},
        "team": {"file_size_policy": "advisory"},
    }
    result = _stamp(settings=settings)
    assert result["file_size_policy"] == "enforced"
    assert len(notifications) == 1
    plan_name, message, kwargs = notifications[0]
    assert plan_name == "plan-a"
    assert kwargs["event"] == "file_size_policy_lock_rejected"
    assert kwargs["story_key"] == "S1"
    assert kwargs["severity"] == "info"
    assert kwargs["dedup_key"] == "file_size_policy_lock_rejected:plan-a:S1"
    assert message == (
        "S1 file_size_policy request refused by policy: team->org, running enforced"
    )


def test_does_not_notify_when_no_request_was_refused(notifications):
    _stamp(settings={"org": {"file_size_policy": "enforced"}})
    assert notifications == []


def test_provider_error_without_env_request_does_not_notify(notifications):
    result = _stamp(settings={"error": "settings_provider_error"})
    assert result["file_size_policy"] == "enforced"
    assert notifications == []


def test_notification_message_carries_no_settings_values(notifications):
    settings = {
        "org": {"file_size_policy": "enforced", "secret_note": "hunter2"},
        "team": {"file_size_policy": "advisory"},
    }
    _stamp(settings=settings)
    assert "hunter2" not in notifications[0][1]


# ---------- file_size_policy_of ----------
@pytest.mark.parametrize(
    "story",
    [
        {},
        {"file_size_policy_resolution": "enforced"},
        {"file_size_policy_resolution": {"file_size_policy": "strict"}},
        {"file_size_policy_resolution": {"file_size_policy": "advisory"}},
    ],
)
def test_policy_of_is_advisory_unless_stamped_enforced(story):
    assert fsp.file_size_policy_of(story) == "advisory"


def test_policy_of_is_enforced_only_for_enforced_stamp():
    story = {"file_size_policy_resolution": {"file_size_policy": "enforced"}}
    assert fsp.file_size_policy_of(story) == "enforced"


# ---------- file_size_advisory_note ----------
def test_advisory_note_is_empty_without_a_stamp():
    assert fsp.file_size_advisory_note({}, "implementer") == ""
    assert fsp.file_size_advisory_note({}, "reviewer") == ""


def test_advisory_note_is_empty_for_non_dict_resolution():
    story = {"file_size_policy_resolution": "advisory"}
    assert fsp.file_size_advisory_note(story, "implementer") == ""


def test_advisory_note_is_empty_when_enforced():
    story = {"file_size_policy_resolution": {"file_size_policy": "enforced"}}
    assert fsp.file_size_advisory_note(story, "implementer") == ""
    assert fsp.file_size_advisory_note(story, "reviewer") == ""


@pytest.mark.parametrize("audience", ["implementer", "reviewer"])
def test_advisory_note_is_non_empty_for_each_audience_when_advisory(audience):
    story = {"file_size_policy_resolution": {"file_size_policy": "advisory"}}
    assert fsp.file_size_advisory_note(story, audience) != ""


def test_advisory_note_for_implementer_forbids_stopping_on_file_size():
    story = {"file_size_policy_resolution": {"file_size_policy": "advisory"}}
    note = fsp.file_size_advisory_note(story, "implementer")
    assert "never stop or park a story because of file size" in note


def test_advisory_note_for_reviewer_forbids_blocking_on_file_size():
    story = {"file_size_policy_resolution": {"file_size_policy": "advisory"}}
    note = fsp.file_size_advisory_note(story, "reviewer")
    assert "Never report file size as Blocking" in note


def test_advisory_note_is_empty_for_unknown_audience():
    story = {"file_size_policy_resolution": {"file_size_policy": "advisory"}}
    assert fsp.file_size_advisory_note(story, "other") == ""


@pytest.mark.parametrize("audience", ["implementer", "reviewer"])
def test_advisory_notes_do_not_start_with_a_bullet(audience):
    story = {"file_size_policy_resolution": {"file_size_policy": "advisory"}}
    assert not fsp.file_size_advisory_note(story, audience).startswith("- ")


# ---------- integration with stamp_test_scope ----------
def test_stamp_test_scope_result_is_unchanged_and_story_gains_file_size_stamp(
    monkeypatch,
    notifications,
):
    monkeypatch.setattr(tsd, "policy_settings", lambda namespace, team: {})
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    story = {}
    result = tsd.stamp_test_scope(
        "plan-a",
        "S1",
        {"repo_root": str(REPO_ROOT)},
        story,
    )
    assert (result["test_scope"], result["source"], result["lock_rejections"]) == (
        "full",
        "default",
        [],
    )
    assert story["test_scope_resolution"] == result
    assert story["file_size_policy_resolution"]["file_size_policy"] == "advisory"
    assert story["file_size_policy_resolution"]["source"] == "default"
