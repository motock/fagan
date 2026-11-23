"""Tests for pipeline.github_issues_sync: a GitHub-issue plan's progress as one checklist.

The GitHub client is a fake that records each write, so nothing reaches the
network. The manifest and settings reads are patched because they are storage,
not the behaviour under test. Events are delivered straight to ``sync_event``
or through a bus built by ``build_bus``.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import github_issues_sync, manifest_source, settings_store
from pipeline.event_wiring import build_bus
from pipeline.github_issues_client import GitHubIssuesError

PLAN = "gh-widget-12"
REPO_URL = "https://github.com/acme/widget"
MARKER = "<!-- fagan-stories -->"
PARKED = "Parked: S1 needs a decision in Fagan."
DONE = "All Fagan stories are done. Ready for review."
PR_URL = "https://github.com/acme/widget/pull/7"
WAITING = f"{MARKER}\n- [ ] S1: Story one (waiting)\n- [ ] S2: Story two (waiting)"


class FakeGitHub:
    """Records writes. ``failures`` maps an operation name to the exception it raises."""

    def __init__(self) -> None:
        self.comments: list[dict] = []
        self.labels: list[str] = []
        self.failures: dict[str, BaseException] = {}
        self.posted: list[str] = []
        self._next_id = 100

    def _check(self, operation: str) -> None:
        if operation in self.failures:
            raise self.failures[operation]

    def list_comments(self, number: int, max_results: int) -> list[dict]:
        self._check("list_comments")
        return [dict(comment) for comment in self.comments]

    def add_comment(self, number: int, text: str, link: str | None = None) -> int:
        self._check("add_comment")
        self._next_id += 1
        self.comments.append({"id": self._next_id, "body": text})
        self.posted.append(text)
        return self._next_id

    def update_comment(self, comment_id: int, text: str) -> None:
        self._check("update_comment")
        for comment in self.comments:
            if comment["id"] == comment_id:
                comment["body"] = text

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._check("add_labels")
        self.labels.extend(labels)

    def marker_bodies(self) -> list[str]:
        return [c["body"] for c in self.comments if c["body"].startswith(MARKER)]


def _story(summary: str, status: str = "todo", **extra) -> dict:
    return {"summary": summary, "status": status, **extra}


def _approved(plan: str = PLAN) -> dict:
    return {"type": "notification", "plan": plan, "payload": {"event": "plan_approved"}}


def _event(event_type: str, story_key: str | None = None, plan: str = PLAN) -> dict:
    return {"type": event_type, "plan": plan, "story_key": story_key}


def _setting(**overrides) -> dict:
    value = {
        "enabled": True,
        "repo_url": REPO_URL,
        "label": "fagan",
        "poll_seconds": 300,
        "token_ref": "",
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def _reset_logged_failures():
    github_issues_sync._LOGGED.clear()
    yield
    github_issues_sync._LOGGED.clear()


@pytest.fixture()
def client() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, client: FakeGitHub):
    """A two-story plan whose team has GitHub issues enabled for this repository."""
    state = {
        "manifest": {
            "plan": PLAN,
            "stories": {"S1": _story("Story one"), "S2": _story("Story two")},
        },
        "setting": _setting(),
        "clients_built": 0,
    }

    def fake_setting(key, *, namespace, team, repo, org=""):
        assert key == "github_issues"
        return {"value": state["setting"], "source": "test", "team": team, "repo": repo}

    def fake_client_for(namespace, setting):
        state["clients_built"] += 1
        return client

    monkeypatch.setattr(
        manifest_source, "read_plan_manifest", lambda plan, **kwargs: state["manifest"]
    )
    monkeypatch.setattr(settings_store, "effective_setting", fake_setting)
    monkeypatch.setattr(github_issues_sync, "_client_for", fake_client_for)
    return state


def _set_story(world: dict, key: str, **fields) -> None:
    world["manifest"]["stories"][key].update(fields)


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


def test_build_bus_subscribes_the_sync_handler_when_enabled(monkeypatch):
    monkeypatch.setenv("PIPELINE_GITHUB_ISSUES_ENABLED", "1")
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)

    bus = build_bus()

    for event_type in github_issues_sync.SUBSCRIBED_EVENT_TYPES:
        assert github_issues_sync.sync_event in bus._handlers[event_type]


def test_build_bus_subscribes_nothing_new_when_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_GITHUB_ISSUES_ENABLED", raising=False)
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)

    bus = build_bus()

    for handlers in bus._handlers.values():
        assert github_issues_sync.sync_event not in handlers


@pytest.mark.parametrize("value", ["true", "0", "", " 1"])
def test_build_bus_subscribes_nothing_new_unless_exactly_one(monkeypatch, value):
    monkeypatch.setenv("PIPELINE_GITHUB_ISSUES_ENABLED", value)
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)

    bus = build_bus()

    for handlers in bus._handlers.values():
        assert github_issues_sync.sync_event not in handlers


# ---------------------------------------------------------------------------
# Checklist: approval, rewrites and idempotence
# ---------------------------------------------------------------------------


def test_plan_approved_posts_one_checklist_comment(world, client):
    github_issues_sync.sync_event(_approved())

    assert client.posted == [WAITING]


def test_agent_dispatched_rewrites_the_line_to_in_progress(world, client):
    github_issues_sync.sync_event(_approved())
    _set_story(world, "S1", status="in_progress")

    github_issues_sync.sync_event(_event("agent_dispatched", "S1"))

    assert client.marker_bodies() == [
        f"{MARKER}\n- [ ] S1: Story one (in progress)\n- [ ] S2: Story two (waiting)"
    ]


def test_pr_open_rewrites_the_line_to_in_review_with_the_pr_number(world, client):
    github_issues_sync.sync_event(_approved())
    _set_story(world, "S1", status="pr_open", pr_url=PR_URL)

    github_issues_sync.sync_event(_event("pr_open", "S1"))

    assert client.marker_bodies() == [
        f"{MARKER}\n- [ ] S1: Story one (in review, PR #7)\n- [ ] S2: Story two (waiting)"
    ]


def test_story_done_rewrites_the_line_as_a_checked_box(world, client):
    github_issues_sync.sync_event(_approved())
    _set_story(world, "S1", status="done")

    github_issues_sync.sync_event(_event("story_done", "S1"))

    assert client.marker_bodies() == [
        f"{MARKER}\n- [x] S1: Story one (done)\n- [ ] S2: Story two (waiting)"
    ]


def test_last_story_done_adds_the_label_and_comment_exactly_once(world, client):
    github_issues_sync.sync_event(_approved())
    _set_story(world, "S1", status="done")
    _set_story(world, "S2", status="done")

    github_issues_sync.sync_event(_event("story_done", "S2"))
    github_issues_sync.sync_event(_event("story_done", "S2"))

    assert client.labels == ["fagan-done"]
    assert client.posted.count(DONE) == 1


def test_last_story_done_does_not_close_or_label_while_a_story_remains(world, client):
    github_issues_sync.sync_event(_approved())
    _set_story(world, "S1", status="done")

    github_issues_sync.sync_event(_event("story_done", "S1"))

    assert client.labels == []
    assert DONE not in client.posted


def test_parked_adds_one_comment_naming_the_story(world, client):
    github_issues_sync.sync_event(_approved())
    _set_story(world, "S1", status="parked")

    github_issues_sync.sync_event(_event("parked", "S1"))
    github_issues_sync.sync_event(_event("parked", "S1"))

    assert client.posted.count(PARKED) == 1


def test_approval_replayed_twice_posts_no_second_checklist(world, client):
    github_issues_sync.sync_event(_approved())
    github_issues_sync.sync_event(_approved())

    assert client.posted == [WAITING]
    assert len(client.marker_bodies()) == 1


def test_missing_marker_comment_is_recreated_from_the_manifest(world, client):
    client.comments = [{"id": 5, "body": "An unrelated comment"}]
    _set_story(world, "S1", status="in_progress")

    github_issues_sync.sync_event(_event("agent_dispatched", "S1"))

    assert client.posted == [
        f"{MARKER}\n- [ ] S1: Story one (in progress)\n- [ ] S2: Story two (waiting)"
    ]


def test_summary_with_markdown_and_html_is_neutralised(world, client):
    world["manifest"]["stories"]["S1"]["summary"] = (
        "Use `code` <script>x</script> | pipe\nnext"
    )

    github_issues_sync.sync_event(_approved())

    assert client.posted == [
        f"{MARKER}\n- [ ] S1: Use code scriptx/script pipe next (waiting)\n- [ ] S2: Story two (waiting)"
    ]


def test_summary_is_capped_at_120_characters(world, client):
    world["manifest"]["stories"]["S1"]["summary"] = "a" * 200

    github_issues_sync.sync_event(_approved())

    assert f"- [ ] S1: {'a' * 120} (waiting)" in client.posted[0]
    assert "a" * 121 not in client.posted[0]


# ---------------------------------------------------------------------------
# Negative cases: what is ignored, and what must never escape
# ---------------------------------------------------------------------------


def test_non_approval_notification_makes_no_client_call(world, client):
    event = {"type": "notification", "plan": PLAN, "payload": {"event": "other"}}

    github_issues_sync.sync_event(event)

    assert world["clients_built"] == 0
    assert client.posted == []


def test_jira_plan_makes_no_client_call(world, client):
    github_issues_sync.sync_event(_approved(plan="jira-abc-12"))

    assert world["clients_built"] == 0


@pytest.mark.parametrize(
    "plan", ["gh-gadget-12", "gh-widget", "gh-widget-0", "gh-widget-12x", "gh--12", "gh-widget-012"]
)
def test_plan_not_matching_this_repository_makes_no_client_call(world, client, plan):
    github_issues_sync.sync_event(_approved(plan=plan))

    assert world["clients_built"] == 0


def test_team_with_github_issues_disabled_makes_no_client_call(world, client):
    world["setting"] = _setting(enabled=False)

    github_issues_sync.sync_event(_approved())

    assert world["clients_built"] == 0


def test_missing_manifest_makes_no_client_call(monkeypatch, world, client):
    monkeypatch.setattr(manifest_source, "read_plan_manifest", lambda plan, **kwargs: None)

    github_issues_sync.sync_event(_approved())

    assert world["clients_built"] == 0


def test_client_auth_error_is_swallowed_and_logged_without_the_repository(
    world, client, caplog
):
    client.failures["list_comments"] = GitHubIssuesError("auth", 401)

    with caplog.at_level(logging.WARNING, logger="pipeline.github_issues_sync"):
        github_issues_sync.sync_event(_approved())

    records = [r for r in caplog.records if r.name == "pipeline.github_issues_sync"]
    assert len(records) == 1
    assert records[0].error_kind == "auth"
    assert "widget" not in caplog.text
    assert "acme" not in caplog.text


def test_client_error_is_logged_once_per_plan_and_kind(world, client, caplog):
    client.failures["list_comments"] = GitHubIssuesError("unavailable", 503)

    with caplog.at_level(logging.WARNING, logger="pipeline.github_issues_sync"):
        github_issues_sync.sync_event(_approved())
        github_issues_sync.sync_event(_event("agent_dispatched", "S1"))

    records = [r for r in caplog.records if r.name == "pipeline.github_issues_sync"]
    assert len(records) == 1


def test_handler_failure_inside_the_client_never_propagates(world, client):
    client.failures["add_comment"] = RuntimeError("boom")

    assert github_issues_sync.sync_event(_approved()) is None


def test_failure_on_a_parked_comment_never_propagates(world, client):
    github_issues_sync.sync_event(_approved())
    client.failures["add_comment"] = RuntimeError("boom")

    assert github_issues_sync.sync_event(_event("parked", "S1")) is None
