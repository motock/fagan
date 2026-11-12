"""Unit tests for pipeline.test_scope_dispatch: dispatch-time test_scope stamping.

The policy provider and the environment are the only external boundaries
stubbed here; the resolver itself runs for real.
"""

from datetime import datetime
from pathlib import Path

import pytest

from pipeline import server as p
from pipeline import test_scope_dispatch as tsd

REPO_ROOT = Path(__file__).resolve().parents[2]


class _CiForgeStub:
    def has_ci_config(self, repo_root):
        return True


@pytest.fixture(autouse=True)
def _scoped_survives_dispatch(monkeypatch):
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(
        "pipeline.test_scope_dispatch.get_forge", lambda root: _CiForgeStub()
    )


@pytest.fixture
def settings_calls(monkeypatch):
    calls = []
    monkeypatch.delenv(tsd.TEST_SCOPE_ENV, raising=False)

    def _set(settings):
        def _fake(namespace, team):
            calls.append((namespace, team))
            return settings

        monkeypatch.setattr(tsd, "policy_settings", _fake)
        return calls

    return _set


@pytest.fixture
def notifications(monkeypatch):
    sent = []

    def _fake(plan_name, message, **kwargs):
        sent.append((plan_name, message, kwargs))

    monkeypatch.setattr(p, "_notify_user", _fake)
    return sent


def _stamp(manifest=None, story=None):
    manifest = {"repo_root": str(REPO_ROOT), **(manifest or {})}
    return tsd.stamp_test_scope("plan-a", "S1", manifest, story if story is not None else {})


# ---------- stamping: resolution and source ----------
def test_stamps_default_full_when_no_layer_is_set(settings_calls, notifications):
    settings_calls({})
    story = {}
    result = _stamp(story=story)
    assert (result["test_scope"], result["source"]) == ("full", "default")
    assert story["test_scope_resolution"] == result


def test_stamps_env_source_from_environment(settings_calls, notifications, monkeypatch):
    settings_calls({})
    monkeypatch.setenv(tsd.TEST_SCOPE_ENV, "scoped")
    result = _stamp()
    assert (result["test_scope"], result["source"]) == ("scoped", "env")


def test_stamps_plan_source_from_manifest(settings_calls, notifications):
    settings_calls({})
    result = _stamp(manifest={"test_scope": "scoped"})
    assert (result["test_scope"], result["source"]) == ("scoped", "plan")


def test_stamps_story_source_from_story(settings_calls, notifications):
    settings_calls({})
    result = _stamp(story={"test_scope": "scoped"})
    assert (result["test_scope"], result["source"]) == ("scoped", "story")


def test_stamps_org_source_from_org_policy(settings_calls, notifications):
    settings_calls({"org": {"test_scope": "scoped"}})
    result = _stamp()
    assert (result["test_scope"], result["source"]) == ("scoped", "org")


def test_stamps_team_source_from_team_policy(settings_calls, notifications):
    settings_calls({"team": {"test_scope": "scoped"}})
    result = _stamp()
    assert (result["test_scope"], result["source"]) == ("scoped", "team")


def test_stamps_resolved_at_as_iso_timestamp(settings_calls, notifications):
    settings_calls({})
    result = _stamp()
    assert datetime.fromisoformat(result["resolved_at"]).tzinfo is not None


# ---------- settings lookup: namespace and team ----------
def test_passes_manifest_team_and_default_namespace_to_settings(settings_calls, notifications):
    calls = settings_calls({})
    _stamp(manifest={"team": "payments"})
    assert calls == [("default", "payments")]


def test_passes_local_team_when_manifest_has_none(settings_calls, notifications):
    calls = settings_calls({})
    _stamp(manifest={})
    assert calls == [("default", "local")]


# ---------- lock rejection notification ----------
def test_notifies_once_with_lock_rejected_event_when_policy_refuses_scoped(
    settings_calls, notifications,
):
    settings_calls({"org": {"test_scope": "full"}})
    _stamp(manifest={"test_scope": "scoped"})
    assert len(notifications) == 1
    plan_name, message, kwargs = notifications[0]
    assert plan_name == "plan-a"
    assert kwargs["event"] == "test_scope_lock_rejected"
    assert kwargs["story_key"] == "S1"
    assert kwargs["dedup_key"] == "test_scope_lock_rejected:plan-a:S1"
    assert message == "S1 test_scope request refused by policy: plan->org; running full"


def test_does_not_notify_when_no_request_was_refused(settings_calls, notifications):
    settings_calls({"org": {"test_scope": "full"}})
    _stamp(manifest={"test_scope": "full"})
    assert notifications == []


def test_does_not_notify_when_scoped_is_allowed(settings_calls, notifications):
    settings_calls({})
    _stamp(manifest={"test_scope": "scoped"})
    assert notifications == []


def test_provider_error_with_scoped_request_stamps_full_and_notifies_once(
    settings_calls, notifications,
):
    settings_calls({"error": "settings_provider_error"})
    result = _stamp(manifest={"test_scope": "scoped"})
    assert (result["test_scope"], result["source"]) == ("full", "settings_provider_error")
    assert [kwargs["event"] for _, _, kwargs in notifications] == ["test_scope_lock_rejected"]


def test_notification_message_carries_no_settings_values(settings_calls, notifications):
    settings_calls({"org": {"test_scope": "full", "secret_note": "hunter2"}})
    _stamp(manifest={"test_scope": "scoped"})
    assert "hunter2" not in notifications[0][1]


# ---------- invalid environment value ----------
def test_bogus_env_value_resolves_full_and_records_invalid_layer(
    settings_calls, notifications, monkeypatch,
):
    settings_calls({})
    monkeypatch.setenv(tsd.TEST_SCOPE_ENV, "bogus")
    result = _stamp()
    assert result["test_scope"] == "full"
    assert result["invalid_layers"] == ["env"]


# ---------- event fields ----------
@pytest.mark.parametrize(
    ("story", "rework"),
    [
        ({}, "none"),
        ({"review_feedback": "rename this"}, "review"),
        ({"ci_rework": True}, "ci"),
    ],
)
def test_event_fields_report_rework_kind(story, rework):
    assert tsd.test_scope_event_fields(story)["rework"] == rework


def test_event_fields_for_story_without_resolution_are_full_defaults():
    assert tsd.test_scope_event_fields({}) == {
        "test_scope": "full",
        "test_scope_source": "default",
        "test_scope_lock_rejected": False,
        "rework": "none",
    }


def test_event_fields_for_stamped_story_are_bounded_values():
    story = {
        "test_scope_resolution": {
            "test_scope": "scoped",
            "source": "plan",
            "lock_rejections": [{"layer": "story", "requested": "scoped", "by": "org"}],
        },
    }
    assert tsd.test_scope_event_fields(story) == {
        "test_scope": "scoped",
        "test_scope_source": "plan",
        "test_scope_lock_rejected": True,
        "rework": "none",
    }


def test_event_fields_ignore_non_dict_resolution():
    assert tsd.test_scope_event_fields({"test_scope_resolution": "scoped"})["test_scope"] == "full"


# ---------- effective_test_scope (plan preview) ----------
def test_effective_test_scope_works_without_a_manifest(settings_calls, monkeypatch):
    settings_calls({})
    monkeypatch.setenv(tsd.TEST_SCOPE_ENV, "scoped")
    result = tsd.effective_test_scope(None)
    assert (result["test_scope"], result["source"], result["env"]) == (
        "scoped", "env", "PIPELINE_TEST_SCOPE",
    )


def test_effective_test_scope_reads_plan_layer_from_manifest(settings_calls, monkeypatch):
    calls = settings_calls({})
    monkeypatch.delenv(tsd.TEST_SCOPE_ENV, raising=False)
    result = tsd.effective_test_scope({"test_scope": "scoped", "team": "payments"})
    assert (result["test_scope"], result["source"]) == ("scoped", "plan")
    assert calls == [("default", "payments")]


def test_effective_test_scope_none_manifest_uses_default_team(settings_calls):
    calls = settings_calls({})
    tsd.effective_test_scope(None)
    assert calls == [("default", "local")]


# ---------- docs pins ----------
def test_reference_has_exactly_one_test_scope_section():
    text = (REPO_ROOT / "REFERENCE.md").read_text()
    assert text.count("## Test scope (`test_scope`)") == 1


def test_reference_documents_resolution_order():
    text = (REPO_ROOT / "REFERENCE.md").read_text()
    assert "Resolution, highest first: team override" in text


def test_reference_env_table_has_test_scope_row():
    text = (REPO_ROOT / "REFERENCE.md").read_text()
    assert (
        "| `PIPELINE_TEST_SCOPE` | `full` | `full` \\| `scoped` — the lowest-precedence "
        "`test_scope` request; see \"Test scope\". Unknown values resolve to `full`. |"
    ) in text
