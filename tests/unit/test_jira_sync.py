"""Tests for pipeline.jira_sync: a Jira-originated plan's stories as sub-tasks.

HTTP is faked at the transport boundary with ``httpx.MockTransport``; the
manifest and settings reads are patched because they are storage, not the
behaviour under test. Events are delivered straight to ``sync_event`` or
through a bus built by ``build_bus``.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from pipeline import (
    jira_intake,
    jira_sync,
    manifest_source,
    notification_links,
    settings_store,
)
from pipeline.event_wiring import build_bus
from pipeline.events import make_event
from pipeline.jira_client import JiraClient

SITE = "https://acme.atlassian.net"
EMAIL = "svc-jira@example.com"
ACCOUNT_ID = "5b10ac8d82e05b22cc7d4ef5"
TOKEN_REF = "secret://env/JIRA_API_TOKEN"
TOKEN = "tok-XYZ-do-not-leak"
LINK_ORIGIN = "https://fagan.example.com"
PLAN = "jira-abc-12"
PARENT = "ABC-12"
AGENT_BRIEF = "AGENT-BRIEF-MUST-NOT-REACH-JIRA"
REPO_URL = "https://github.com/acme/widget"

BOARD_STATUS = {
    "in_progress": "In Progress",
    "in_review": "In Review",
    "done": "Done",
    "parent_review": "Parent Review",
}


def _board(enabled=True):
    return {
        "enabled": enabled,
        "project_key": "ABC",
        "repo_url": REPO_URL,
        "poll_seconds": 300,
        "status": BOARD_STATUS,
    }


def _site(site=SITE):
    return {"site": site, "email": EMAIL, "token_ref": TOKEN_REF, "account_id": ACCOUNT_ID}


def _story(summary, status="todo"):
    return {
        "summary": summary,
        "description": f"Description of {summary}",
        "agent_instructions": AGENT_BRIEF,
        "files": ["pipeline/secret_module.py"],
        "status": status,
    }


def _event(event_type, story_key=None, payload=None, plan=PLAN):
    return make_event(event_type, plan, story_key=story_key, payload=payload or {})


def _approved():
    return _event("notification", payload={"event": "plan_approved"})


class FakeJira:
    """An in-memory Jira Cloud: sub-task search, create, transitions and comments."""

    def __init__(self):
        self.subtasks: dict[str, dict] = {}
        self.created_bodies: list[dict] = []
        self.comments: dict[str, list[dict]] = {}
        self.moves: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.missing_statuses: set[str] = set()
        self.search_status: int | None = None
        self.comment_status: int | None = None
        self._next = 100

    def _reachable(self) -> list[str]:
        return [name for name in BOARD_STATUS.values() if name not in self.missing_statuses]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        parts = path.split("/")
        if path.endswith("/search/jql"):
            if self.search_status is not None:
                return httpx.Response(self.search_status)
            jql = json.loads(request.content)["jql"]
            issues = [
                {"key": key}
                for key, sub in self.subtasks.items()
                if f"parent = {sub['parent']} " in jql and f'"{sub["label"]}"' in jql
            ]
            return httpx.Response(200, json={"issues": issues, "isLast": True})
        if request.method == "POST" and path == "/rest/api/3/issue":
            fields = json.loads(request.content)["fields"]
            self.created_bodies.append(fields)
            key = f"ABC-{self._next}"
            self._next += 1
            label = next(name for name in fields["labels"] if name.startswith("fagan-story-"))
            self.subtasks[key] = {
                "parent": fields["parent"]["key"],
                "label": label,
            }
            return httpx.Response(201, json={"key": key})
        if path.endswith("/transitions") and request.method == "GET":
            names = self._reachable()
            transitions = [
                {"id": str(index), "to": {"name": name}}
                for index, name in enumerate(names, start=1)
            ]
            return httpx.Response(200, json={"transitions": transitions})
        if path.endswith("/transitions") and request.method == "POST":
            issue = parts[5]
            transition_id = json.loads(request.content)["transition"]["id"]
            name = self._reachable()[int(transition_id) - 1]
            self.moves.append((issue, name))
            return httpx.Response(204)
        if path.endswith("/comment") and request.method == "POST":
            if self.comment_status is not None:
                return httpx.Response(self.comment_status)
            issue = parts[5]
            self.comments.setdefault(issue, []).append(json.loads(request.content))
            return httpx.Response(201, json={"id": "1"})
        if request.method == "GET" and len(parts) == 6 and parts[4] == "issue":
            bodies = [entry["body"] for entry in self.comments.get(parts[5], [])]
            comments = [{"body": body} for body in bodies]
            return httpx.Response(200, json={"key": parts[5], "fields": {"comment": {"comments": comments}}})
        return httpx.Response(404)

    def issue_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if "/rest/api/3/" in r.url.path]


def _comment_text(body: dict) -> str:
    return json.dumps(body)


@pytest.fixture(autouse=True)
def _reset_missing_transition_log():
    jira_sync._MISSING_TRANSITIONS.clear()
    yield
    jira_sync._MISSING_TRANSITIONS.clear()


@pytest.fixture()
def jira(monkeypatch: pytest.MonkeyPatch) -> FakeJira:
    fake = FakeJira()
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.setenv("PIPELINE_JIRA_SITES", SITE)
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", LINK_ORIGIN)
    monkeypatch.delenv("PIPELINE_STORY_LINK_PATH", raising=False)
    client = JiraClient(
        SITE, EMAIL, TOKEN_REF, ACCOUNT_ID, transport=httpx.MockTransport(fake)
    )
    monkeypatch.setattr(jira_intake, "_make_client", lambda site: client)
    return fake


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch):
    """A two-story plan with the board and site resolved from test state."""
    state = {
        "manifest": {
            "plan": PLAN,
            "repo_url": REPO_URL,
            "stories": {
                "S1": _story("Story one"),
                "S2": _story("Story two"),
            },
        },
        "board": _board(),
        "site": _site(),
    }

    def fake_setting(key, *, namespace, team, repo, org=""):
        values = {"jira_board": state["board"], "jira_site": state["site"]}
        return {"value": values[key], "source": "test", "team": team, "repo": repo}

    monkeypatch.setattr(
        manifest_source, "read_plan_manifest", lambda plan, **kwargs: state["manifest"]
    )
    monkeypatch.setattr(settings_store, "effective_setting", fake_setting)
    return state


def _create_both_subtasks(jira: FakeJira) -> dict[str, str]:
    jira_sync.sync_event(_approved())
    return {jira.subtasks[key]["label"]: key for key in jira.subtasks}


# ---------------------------------------------------------------------------
# plan_approved: one sub-task per story
# ---------------------------------------------------------------------------


def test_plan_approved_creates_one_subtask_per_story_with_labels(jira, world):
    jira_sync.sync_event(_approved())

    assert len(jira.created_bodies) == 2
    by_label = {body["labels"][1]: body for body in jira.created_bodies}
    assert set(by_label) == {"fagan-story-S1", "fagan-story-S2"}
    for label, body in by_label.items():
        assert body["labels"] == ["fagan", label]
        assert body["parent"] == {"key": PARENT}
        assert body["project"] == {"key": "ABC"}
    assert by_label["fagan-story-S1"]["summary"] == "Story one"
    assert by_label["fagan-story-S1"]["description"]["content"][0]["content"][0]["text"] == (
        "Description of Story one"
    )


def test_plan_approved_never_copies_agent_instructions_or_files(jira, world):
    jira_sync.sync_event(_approved())

    for body in jira.created_bodies:
        text = _comment_text(body)
        assert AGENT_BRIEF not in text
        assert "secret_module.py" not in text


def test_plan_approved_twice_creates_nothing_the_second_time(jira, world):
    jira_sync.sync_event(_approved())
    created_after_first = len(jira.created_bodies)

    jira_sync.sync_event(_approved())

    assert created_after_first == 2
    assert len(jira.created_bodies) == 2


def test_plan_approved_skips_a_story_with_an_unsafe_key(jira, world):
    world["manifest"]["stories"]['S"1 OR 1=1'] = _story("Injected key")

    jira_sync.sync_event(_approved())

    assert len(jira.created_bodies) == 2
    assert all('"' not in body["labels"][1] for body in jira.created_bodies)


def test_non_approval_notification_makes_no_http_call(jira, world):
    jira_sync.sync_event(_event("notification", payload={"event": "ci_pending_stalled"}))

    assert jira.requests == []


# ---------------------------------------------------------------------------
# Lifecycle: status mapping
# ---------------------------------------------------------------------------


def test_agent_dispatched_moves_the_subtask_to_in_progress(jira, world):
    keys = _create_both_subtasks(jira)

    jira_sync.sync_event(_event("agent_dispatched", "S1"))

    assert jira.moves == [(keys["fagan-story-S1"], "In Progress")]


def test_pr_open_moves_the_subtask_to_in_review(jira, world):
    keys = _create_both_subtasks(jira)

    jira_sync.sync_event(_event("pr_open", "S2"))

    assert jira.moves == [(keys["fagan-story-S2"], "In Review")]


def test_story_done_moves_the_subtask_to_done(jira, world):
    keys = _create_both_subtasks(jira)

    jira_sync.sync_event(_event("story_done", "S1"))

    assert jira.moves == [(keys["fagan-story-S1"], "Done")]


def test_story_done_while_stories_remain_leaves_the_parent_alone(jira, world):
    world["manifest"]["stories"]["S1"]["status"] = "done"
    _create_both_subtasks(jira)

    jira_sync.sync_event(_event("story_done", "S1"))

    assert all(name != "Parent Review" for _, name in jira.moves)
    assert PARENT not in jira.comments


def test_last_story_done_moves_the_parent_and_comments(jira, world):
    world["manifest"]["stories"]["S1"]["status"] = "done"
    world["manifest"]["stories"]["S2"]["status"] = "done"
    _create_both_subtasks(jira)

    jira_sync.sync_event(_event("story_done", "S2"))

    assert (PARENT, "Parent Review") in jira.moves
    assert jira.comments[PARENT][-1]["body"]["content"][0]["content"][0]["text"] == (
        "All Fagan stories are done. Ready for review."
    )


def test_event_for_a_story_without_a_subtask_makes_no_transition(jira, world):
    jira_sync.sync_event(_event("agent_dispatched", "S1"))

    assert jira.moves == []


# ---------------------------------------------------------------------------
# parked
# ---------------------------------------------------------------------------


def test_parked_comments_on_the_subtask_with_the_story_link(jira, world):
    keys = _create_both_subtasks(jira)

    jira_sync.sync_event(_event("parked", "S1", payload={"reason": "scope_conflict"}))

    comments = jira.comments[keys["fagan-story-S1"]]
    assert len(comments) == 1
    text = _comment_text(comments[0])
    assert "Parked: needs a decision in Fagan." in text
    assert notification_links.story_link(PLAN, "S1") in text
    assert f"{LINK_ORIGIN}/?plan=jira-abc-12&story=S1" in text


def test_parked_comment_omits_the_parked_reason(jira, world):
    keys = _create_both_subtasks(jira)

    jira_sync.sync_event(_event("parked", "S1", payload={"reason": "scope_conflict"}))

    assert "scope_conflict" not in _comment_text(jira.comments[keys["fagan-story-S1"]][0])


def test_parked_without_a_public_url_comments_without_a_link(jira, world, monkeypatch):
    monkeypatch.delenv("PIPELINE_DASHBOARD_PUBLIC_URL")
    keys = _create_both_subtasks(jira)

    jira_sync.sync_event(_event("parked", "S1"))

    text = _comment_text(jira.comments[keys["fagan-story-S1"]][0])
    assert "Parked: needs a decision in Fagan." in text
    assert "https://" not in text


# ---------------------------------------------------------------------------
# Gates: no HTTP call when the event does not concern an enabled Jira board
# ---------------------------------------------------------------------------


def test_plan_not_named_jira_makes_no_http_call(jira, world):
    jira_sync.sync_event(_event("agent_dispatched", "S1", plan="plain-plan"))

    assert jira.requests == []


def test_disabled_board_makes_no_http_call(jira, world):
    world["board"] = _board(enabled=False)

    jira_sync.sync_event(_approved())
    jira_sync.sync_event(_event("story_done", "S1"))

    assert jira.requests == []


def test_site_outside_the_allowlist_makes_no_http_call(jira, world):
    world["site"] = _site("https://evil.example.com")

    jira_sync.sync_event(_approved())

    assert jira.requests == []


def test_plan_without_a_valid_issue_key_makes_no_http_call(jira, world):
    jira_sync.sync_event(_event("agent_dispatched", "S1", plan="jira-not-a-key"))

    assert jira.requests == []


# ---------------------------------------------------------------------------
# Failure handling: logged by type, never raised into the bus
# ---------------------------------------------------------------------------


def test_jira_error_is_logged_by_type_and_not_raised(jira, world, caplog):
    jira.search_status = 500
    caplog.set_level(logging.WARNING)

    jira_sync.sync_event(_event("agent_dispatched", "S1"))

    failures = [r for r in caplog.records if r.getMessage() == "jira sync failed"]
    assert len(failures) == 1
    assert failures[0].error_type == "JiraError"
    assert failures[0].story == "S1"
    assert TOKEN not in caplog.text
    assert SITE not in caplog.text


def test_missing_transition_logs_info_once_per_plan_and_status(jira, world, caplog):
    _create_both_subtasks(jira)
    jira.missing_statuses.add("In Progress")
    caplog.set_level(logging.INFO, logger="pipeline.jira_sync")

    jira_sync.sync_event(_event("agent_dispatched", "S1"))
    jira_sync.sync_event(_event("agent_dispatched", "S2"))

    unreachable = [
        r for r in caplog.records if r.getMessage() == "jira status has no transition from this issue"
    ]
    assert len(unreachable) == 1
    assert jira.moves == []


# ---------------------------------------------------------------------------
# Bus wiring: opt-in subscription
# ---------------------------------------------------------------------------

SYNC_EVENT_TYPES = ("notification", "agent_dispatched", "pr_open", "story_done", "parked")


def test_build_bus_subscribes_the_sync_handler_when_enabled(monkeypatch):
    monkeypatch.setenv("PIPELINE_JIRA_ENABLED", "1")
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)

    bus = build_bus()

    for event_type in SYNC_EVENT_TYPES:
        assert jira_sync.sync_event in bus._handlers[event_type]


def test_build_bus_subscribes_nothing_new_when_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_JIRA_ENABLED", raising=False)
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)

    bus = build_bus()

    for handlers in bus._handlers.values():
        assert jira_sync.sync_event not in handlers


@pytest.mark.parametrize("value", ["true", "0", "", " 1"])
def test_build_bus_subscribes_nothing_new_unless_exactly_one(monkeypatch, value):
    monkeypatch.setenv("PIPELINE_JIRA_ENABLED", value)
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)

    bus = build_bus()

    for handlers in bus._handlers.values():
        assert jira_sync.sync_event not in handlers



# ---------------------------------------------------------------------------
# plan_guidance_resolved: one comment on the parent issue, replay-safe
# ---------------------------------------------------------------------------

RESOLVED = "Questions answered in Fagan. The plan is waiting for approval."


def _resolved(plan: str = PLAN) -> dict:
    return _event("plan_guidance_resolved", plan=plan)


def test_resolved_guidance_comments_once_on_the_parent_issue(jira, world):
    jira_sync.sync_event(_resolved())

    assert len(jira.comments[PARENT]) == 1
    assert RESOLVED in _comment_text(jira.comments[PARENT][0]["body"])


def test_replayed_resolution_posts_no_second_comment(jira, world):
    jira_sync.sync_event(_resolved())
    jira_sync.sync_event(_resolved())

    assert len(jira.comments[PARENT]) == 1


def test_resolution_for_an_unrelated_plan_makes_no_request(jira, world):
    jira_sync.sync_event(_resolved(plan="gh-widget-12"))
    jira_sync.sync_event(_resolved(plan="plain-plan"))

    assert jira.requests == []


def test_comment_failure_is_logged_by_kind_and_not_raised(jira, world, caplog):
    jira.comment_status = 503
    caplog.set_level(logging.WARNING)

    jira_sync.sync_event(_resolved())

    failures = [r for r in caplog.records if r.getMessage() == "jira guidance comment failed"]
    assert len(failures) == 1
    assert failures[0].error_kind == "unavailable"
    assert RESOLVED not in caplog.text
