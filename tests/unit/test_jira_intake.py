"""Jira intake: plan the Jira stories assigned to the Fagan service account.

The Jira HTTP boundary is an ``httpx.MockTransport``; the decomposer's model
call and the git clone are the only other stubs. The plan save path runs for
real, the ingest is stubbed the way the repo-url tests stub it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
import pytest

from pipeline import jira_intake, persistence, repo_clone, server, service
from pipeline.jira_client import JiraClient
from pipeline.workspace import ALLOWED_REPO_ROOTS_ENV

ENABLE_ENV = "PIPELINE_JIRA_ENABLED"
APPROVAL_ENV = "PIPELINE_PLAN_APPROVAL"
SITES_ENV = "PIPELINE_JIRA_SITES"
SITE = "https://acme.atlassian.net"
EMAIL = "svc-jira@example.com"
ACCOUNT_ID = "5b10ac8d82e05b22cc7d4ef5"
TOKEN_REF = "secret://env/JIRA_API_TOKEN"
TOKEN = "tok-XYZ-do-not-leak"
REPO_URL = "https://github.com/acme/widget"
LINK_ORIGIN = "https://fagan.example.com"

BOARD_STATUS = {
    "in_progress": "In Progress",
    "in_review": "In Review",
    "done": "Done",
    "parent_review": "Parent Review",
}


def _board(project_key="ABC", enabled=True, poll_seconds=300):
    return {
        "enabled": enabled,
        "project_key": project_key,
        "repo_url": REPO_URL,
        "poll_seconds": poll_seconds,
        "status": BOARD_STATUS,
    }


def _site():
    return {
        "site": SITE,
        "email": EMAIL,
        "token_ref": TOKEN_REF,
        "account_id": ACCOUNT_ID,
    }


def _issue(key, summary="Add login", description=None):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "status": {"name": "To Do"},
            "labels": [],
        },
    }


PLAN_JSON = {
    "epics": [
        {
            "summary": "Login",
            "stories": [
                {"summary": "Form", "agent_instructions": "Build the form."},
                {"summary": "API", "agent_instructions": "Build the API."},
            ],
        }
    ]
}


class FakeJira:
    """Routes Jira REST calls by path and records every request."""

    def __init__(self, issues_by_project=None, search_status=None):
        self.issues_by_project = issues_by_project or {}
        self.search_status = search_status or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/rest/api/3/search/jql":
            body = json.loads(request.content)
            project = re.search(r'project = "([A-Z0-9_]+)"', body["jql"]).group(1)
            if project in self.search_status:
                return httpx.Response(self.search_status[project])
            issues = self.issues_by_project.get(project, [])
            return httpx.Response(200, json={"issues": issues, "isLast": True})
        if path.endswith("/transitions") and request.method == "GET":
            return httpx.Response(
                200, json={"transitions": [{"id": "31", "to": {"name": "In Progress"}}]}
            )
        if path.endswith("/transitions"):
            return httpx.Response(204)
        if path.endswith("/comment"):
            return httpx.Response(201, json={})
        return httpx.Response(204)

    def writes(self) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method in ("PUT", "POST") and "/search/" not in r.url.path
        ]

    def comments(self) -> list[str]:
        out = []
        for request in self.requests:
            if request.url.path.endswith("/comment"):
                out.append(json.dumps(json.loads(request.content)))
        return out


@pytest.fixture(autouse=True)
def _reset_poll_clock():
    jira_intake._LAST_RUN.clear()
    yield
    jira_intake._LAST_RUN.clear()


@pytest.fixture()
def intake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated plan dir, enabled integration, stubbed decomposer, clone and ingest."""
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(persistence, "PLAN_DIR", plans)
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(APPROVAL_ENV, "1")
    monkeypatch.setenv(SITES_ENV, SITE)
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", LINK_ORIGIN)
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv(ALLOWED_REPO_ROOTS_ENV, str(root))
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    monkeypatch.setenv("PIPELINE_REPO_URL_HOSTS", "github.com")

    managed = root / "widget"
    managed.mkdir()
    monkeypatch.setattr(repo_clone, "ensure_clone", lambda url, *a, **k: managed)

    decompose_calls: list[str] = []

    def _decompose(request_text, repo_root=None, layer_routing=None):
        decompose_calls.append(request_text)
        return json.dumps(PLAN_JSON), None

    monkeypatch.setattr(service, "_run_decompose_detailed", _decompose)

    ingested: list[str] = []

    def _fake_ingest(name, only_epics=None, overwrite=False):
        ingested.append(name)
        return {"ok": True}

    monkeypatch.setattr(service, "_ingest_plan_impl", _fake_ingest)
    monkeypatch.setattr(service.wake, "after", lambda name, reason, result: result)

    return {
        "records": server._store.records(),
        "decompose": decompose_calls,
        "ingested": ingested,
    }


def _seed_team(records, team, board=None):
    records.put_settings(
        "default", "team", team, "", {"jira_board": board or _board()}, [], "ops", None
    )


def _seed_org(records, site=None):
    records.put_settings(
        "default", "org", "acme", "", {"jira_site": site or _site()}, [], "ops", None
    )


def _use_fake(monkeypatch, fake: FakeJira) -> None:
    def _client(site):
        return JiraClient(
            site["site"],
            site["email"],
            site["token_ref"],
            site["account_id"],
            transport=httpx.MockTransport(fake),
        )

    monkeypatch.setattr(jira_intake, "_make_client", _client)


# ---------------------------------------------------------------------------
# opt-in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "true", "0", ""])
def test_disabled_intake_reads_nothing_and_makes_no_request(
    intake_env, monkeypatch, value
):
    if value is None:
        monkeypatch.delenv(ENABLE_ENV, raising=False)
    else:
        monkeypatch.setenv(ENABLE_ENV, value)
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")

    class _NoReads:
        def records(self):
            raise AssertionError("the record store must not be read while disabled")

    def _must_not_build(site):
        raise AssertionError("no Jira client may be built while disabled")

    monkeypatch.setattr(jira_intake, "_make_client", _must_not_build)
    monkeypatch.setattr(jira_intake, "_store", _NoReads())

    assert jira_intake.run_jira_intake(1000.0) == {}
    assert intake_env["decompose"] == []


# ---------------------------------------------------------------------------
# intake of one assigned issue
# ---------------------------------------------------------------------------


def test_assigned_issue_becomes_a_plan_then_gets_label_transition_and_comment(
    intake_env, monkeypatch
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-123", "Add login")]})
    _use_fake(monkeypatch, fake)

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["planned"] == 1
    assert intake_env["ingested"] == ["jira-abc-123"]
    manifest_saved = json.loads((server.PLAN_DIR / "jira-abc-123.json").read_text())
    assert manifest_saved["team"] == "alpha"
    assert manifest_saved["repo_url"] == REPO_URL
    assert "Jira ABC-123: Add login" in intake_env["decompose"][0]

    paths = [(r.method, r.url.path) for r in fake.writes()]
    assert paths == [
        ("PUT", "/rest/api/3/issue/ABC-123"),
        ("POST", "/rest/api/3/issue/ABC-123/transitions"),
        ("POST", "/rest/api/3/issue/ABC-123/comment"),
    ]
    label_body = json.loads(fake.writes()[0].content)
    assert label_body == {"update": {"labels": [{"add": "fagan-planned"}]}}
    comment = fake.comments()[0]
    assert "Fagan planned this story into 2 stories" in comment
    assert f"{LINK_ORIGIN}/?plan=jira-abc-123" in comment


def test_issue_text_is_flattened_from_adf_into_the_request(intake_env, monkeypatch):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Users log in."}],
            }
        ],
    }
    _use_fake(monkeypatch, FakeJira({"ABC": [_issue("ABC-7", "Login", adf)]}))

    jira_intake.run_jira_intake(1000.0)

    assert intake_env["decompose"][0] == "Jira ABC-7: Login\n\nUsers log in."


# ---------------------------------------------------------------------------
# existing plan, disabled board, missing site
# ---------------------------------------------------------------------------


def test_issue_whose_plan_exists_only_gets_the_label(intake_env, monkeypatch):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    intake_env["records"].put_manifest("default", "jira-abc-123", {"team": "alpha"})
    fake = FakeJira({"ABC": [_issue("ABC-123")]})
    _use_fake(monkeypatch, fake)

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["existing"] == 1
    assert intake_env["decompose"] == []
    assert intake_env["ingested"] == []
    assert [(r.method, r.url.path) for r in fake.writes()] == [
        ("PUT", "/rest/api/3/issue/ABC-123"),
    ]


def test_disabled_board_is_skipped_without_a_request(intake_env, monkeypatch):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha", board=_board(enabled=False))
    fake = FakeJira({"ABC": [_issue("ABC-1")]})
    _use_fake(monkeypatch, fake)

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 0
    assert fake.requests == []


def test_missing_jira_site_is_skipped_without_a_request(intake_env, monkeypatch):
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-1")]})
    _use_fake(monkeypatch, fake)

    jira_intake.run_jira_intake(1000.0)

    assert fake.requests == []
    assert intake_env["decompose"] == []


# ---------------------------------------------------------------------------
# failure paths
# ---------------------------------------------------------------------------


def test_decompose_failure_adds_the_failed_label_and_a_comment_without_exception_text(
    intake_env, monkeypatch, caplog
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-9")]})
    _use_fake(monkeypatch, fake)

    def _explode(request_text, repo_root=None, layer_routing=None):
        raise RuntimeError("model endpoint leaked SECRET-MARKER-42")

    monkeypatch.setattr(service, "_run_decompose_detailed", _explode)
    caplog.set_level(logging.WARNING)

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["failed"] == 1
    assert intake_env["ingested"] == []
    label_bodies = [json.loads(r.content) for r in fake.writes() if r.method == "PUT"]
    assert label_bodies == [{"update": {"labels": [{"add": "fagan-intake-failed"}]}}]
    comment = fake.comments()[0]
    assert "Fagan could not plan this story" in comment
    assert "remove the label fagan-intake-failed to retry" in comment
    assert "SECRET-MARKER-42" not in comment
    assert "SECRET-MARKER-42" not in caplog.text
    planned_failures = [
        r for r in caplog.records if r.getMessage() == "jira story could not be planned"
    ]
    assert [r.error_type for r in planned_failures] == ["RuntimeError"]


def test_failed_decompose_result_is_treated_as_an_intake_failure(
    intake_env, monkeypatch
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-10")]})
    _use_fake(monkeypatch, fake)
    monkeypatch.setattr(
        service, "_run_decompose_detailed", lambda r, repo_root=None, layer_routing=None: ("", "boom")
    )

    assert jira_intake.run_jira_intake(1000.0)["failed"] == 1
    assert intake_env["ingested"] == []


# ---------------------------------------------------------------------------
# poll window, auth stop, bounds
# ---------------------------------------------------------------------------


def test_poll_seconds_window_is_respected(intake_env, monkeypatch):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha", board=_board(poll_seconds=300))
    fake = FakeJira({"ABC": []})
    _use_fake(monkeypatch, fake)

    jira_intake.run_jira_intake(1000.0)
    searches_after_first = len(fake.requests)
    jira_intake.run_jira_intake(1100.0)
    assert len(fake.requests) == searches_after_first

    jira_intake.run_jira_intake(1300.0)
    assert len(fake.requests) == searches_after_first + 1


def test_auth_error_stops_that_team_and_another_team_still_runs(
    intake_env, monkeypatch, caplog
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha", board=_board(project_key="ALPHA"))
    _seed_team(intake_env["records"], "beta", board=_board(project_key="BETA"))
    fake = FakeJira(
        {"BETA": [_issue("BETA-1")]},
        search_status={"ALPHA": 401},
    )
    _use_fake(monkeypatch, fake)
    caplog.set_level(logging.WARNING)

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["planned"] == 1
    assert intake_env["ingested"] == ["jira-beta-1"]
    stopped = [r for r in caplog.records if r.getMessage() == "jira team run stopped"]
    assert [(r.team, r.error_kind) for r in stopped] == [("alpha", "auth")]
    assert TOKEN not in caplog.text


def test_at_most_five_issues_per_run(intake_env, monkeypatch):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    issues = [_issue(f"ABC-{n}") for n in range(1, 8)]
    _use_fake(monkeypatch, FakeJira({"ABC": issues}))

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["planned"] == 5
    assert len(intake_env["ingested"]) == 5


def test_jql_selects_assignee_and_excludes_both_labels(intake_env, monkeypatch):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": []})
    _use_fake(monkeypatch, fake)

    jira_intake.run_jira_intake(1000.0)

    body = json.loads(fake.requests[0].content)
    assert body["jql"] == (
        f'project = "ABC" AND issuetype = Story AND assignee = "{ACCOUNT_ID}" '
        "AND labels not in (fagan-planned, fagan-intake-failed) ORDER BY created ASC"
    )


# ---------------------------------------------------------------------------
# REWORK 1 (a): fail closed on the plan-approval gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "0", "off", "false"])
def test_approval_gate_off_runs_no_search_decompose_or_ingest(
    intake_env, monkeypatch, caplog, value
):
    if value is None:
        monkeypatch.delenv(APPROVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(APPROVAL_ENV, value)
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-1")]})
    _use_fake(monkeypatch, fake)
    caplog.set_level(logging.WARNING)

    assert jira_intake.run_jira_intake(1000.0) == {}

    assert fake.requests == []
    assert intake_env["decompose"] == []
    assert intake_env["ingested"] == []
    skipped = [
        r
        for r in caplog.records
        if r.getMessage() == "jira intake skipped: plan approval gate is off"
    ]
    assert len(skipped) == 1
    assert skipped[0].levelno == logging.WARNING
    assert "Jira ABC-1" not in caplog.text


def test_approval_gate_on_runs_intake(intake_env, monkeypatch):
    monkeypatch.setenv(APPROVAL_ENV, "on")
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    _use_fake(monkeypatch, FakeJira({"ABC": [_issue("ABC-2")]}))

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 1
    assert intake_env["ingested"] == ["jira-abc-2"]


def test_unrecognised_approval_value_fails_closed(intake_env, monkeypatch):
    monkeypatch.setenv(APPROVAL_ENV, "yes-please")
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-3")]})
    _use_fake(monkeypatch, fake)

    assert jira_intake.run_jira_intake(1000.0) == {}
    assert fake.requests == []
    assert intake_env["ingested"] == []


# ---------------------------------------------------------------------------
# REWORK 1 (b): deny-by-default site allowlist and token-name rule
# ---------------------------------------------------------------------------


def test_site_not_in_allowlist_is_skipped_without_a_request(
    intake_env, monkeypatch, caplog
):
    monkeypatch.setenv(SITES_ENV, "https://other.atlassian.net")
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-1")]})
    _use_fake(monkeypatch, fake)
    caplog.set_level(logging.WARNING)

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 0

    assert fake.requests == []
    skipped = [r for r in caplog.records if r.getMessage() == "jira team skipped"]
    assert [(r.team, r.reason) for r in skipped] == [("alpha", "site_not_allowed")]
    assert SITE not in caplog.text


def test_unset_site_allowlist_runs_nothing(intake_env, monkeypatch):
    monkeypatch.delenv(SITES_ENV, raising=False)
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-1")]})
    _use_fake(monkeypatch, fake)

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 0
    assert fake.requests == []


def test_empty_site_allowlist_runs_nothing(intake_env, monkeypatch):
    monkeypatch.setenv(SITES_ENV, " , ")
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-1")]})
    _use_fake(monkeypatch, fake)

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 0
    assert fake.requests == []


def test_malformed_allowlist_entry_is_ignored_and_the_listed_site_runs(
    intake_env, monkeypatch
):
    monkeypatch.setenv(SITES_ENV, f"not a url, http://evil.example, {SITE}")
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    _use_fake(monkeypatch, FakeJira({"ABC": [_issue("ABC-4")]}))

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 1


def test_listed_site_with_a_non_jira_token_name_is_skipped(
    intake_env, monkeypatch, caplog
):
    _seed_org(
        intake_env["records"], site={**_site(), "token_ref": "secret://env/GH_TOKEN"}
    )
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-1")]})
    _use_fake(monkeypatch, fake)
    caplog.set_level(logging.WARNING)

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 0

    assert fake.requests == []
    skipped = [r for r in caplog.records if r.getMessage() == "jira team skipped"]
    assert [(r.team, r.reason) for r in skipped] == [("alpha", "token_ref_not_allowed")]
    assert "GH_TOKEN" not in caplog.text


def test_listed_site_with_a_jira_token_name_runs(intake_env, monkeypatch):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    _use_fake(monkeypatch, FakeJira({"ABC": [_issue("ABC-5")]}))

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 1


# ---------------------------------------------------------------------------
# REWORK 1 (c): project_key is re-validated before it reaches the JQL
# ---------------------------------------------------------------------------


def test_stored_project_key_with_jql_breakout_is_refused_without_a_search(
    intake_env, monkeypatch, caplog
):
    _seed_org(intake_env["records"])
    _seed_team(
        intake_env["records"],
        "alpha",
        board=_board(project_key='ABC" OR project = "X'),
    )
    fake = FakeJira({"ABC": [_issue("ABC-1")]})
    _use_fake(monkeypatch, fake)
    caplog.set_level(logging.WARNING)

    assert jira_intake.run_jira_intake(1000.0)["planned"] == 0

    assert fake.requests == []
    skipped = [r for r in caplog.records if r.getMessage() == "jira team skipped"]
    assert [(r.team, r.reason) for r in skipped] == [("alpha", "invalid_board")]
    assert "OR project" not in caplog.text


# ---------------------------------------------------------------------------
# REWORK 1 (d): the per-run cap is the search's maxResults
# ---------------------------------------------------------------------------


def test_search_asks_for_no_more_issues_than_the_per_run_cap(intake_env, monkeypatch):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": []})
    _use_fake(monkeypatch, fake)

    jira_intake.run_jira_intake(1000.0)

    body = json.loads(fake.requests[0].content)
    assert body["maxResults"] == jira_intake.MAX_ISSUES_PER_RUN == 5


# ---------------------------------------------------------------------------
# open questions from the decomposer: guidance instead of a plain plan
# ---------------------------------------------------------------------------

QUESTION_TEXT = "Which payment provider should refunds use?"


def _decompose_with(monkeypatch, plan_extra):
    """Stub the decomposer to return PLAN_JSON plus extra top-level keys."""

    def _decompose(request_text, repo_root=None, layer_routing=None):
        return json.dumps({**PLAN_JSON, **plan_extra}), None

    monkeypatch.setattr(service, "_run_decompose_detailed", _decompose)


@pytest.fixture()
def notices(monkeypatch, intake_env):
    """Record every notice, and make a successful ingest write the manifest.

    The real notifier is never invoked. The ingest stub writes the plan's
    manifest the way a real ingest does, so ``raise_guidance`` finds the plan.
    """
    records: list[dict] = []

    def recorder(plan_name, message, **_kwargs):
        records.append(
            {"plan": plan_name, "message": message, "event": _kwargs.get("event")}
        )

    def _ingest_writes_manifest(name, only_epics=None, overwrite=False):
        intake_env["ingested"].append(name)
        saved = json.loads((server.PLAN_DIR / f"{name}.json").read_text())
        intake_env["records"].put_manifest("default", name, saved)
        return {"ok": True}

    monkeypatch.setattr(server, "_notify_user", recorder)
    monkeypatch.setattr(service, "_ingest_plan_impl", _ingest_writes_manifest)
    return records


def test_plan_with_open_questions_is_saved_without_the_key_and_needs_guidance(
    intake_env, monkeypatch, notices
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-123")]})
    _use_fake(monkeypatch, fake)
    _decompose_with(
        monkeypatch, {"open_questions": [QUESTION_TEXT, "Who approves refunds?"]}
    )

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["planned"] == 1
    saved = json.loads((server.PLAN_DIR / "jira-abc-123.json").read_text())
    assert "open_questions" not in saved
    manifest = intake_env["records"].get_manifest("default", "jira-abc-123")
    guidance = manifest["guidance"]
    assert guidance["status"] == "needed"
    assert guidance["questions"] == [QUESTION_TEXT, "Who approves refunds?"]
    assert guidance["source"] == {"kind": "jira", "ref": "ABC-123"}
    assert [n["event"] for n in notices] == ["plan_needs_guidance"]


def test_guidance_comment_replaces_the_planned_comment_and_the_label_is_kept(
    intake_env, monkeypatch, notices
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-123")]})
    _use_fake(monkeypatch, fake)
    _decompose_with(monkeypatch, {"open_questions": [QUESTION_TEXT]})

    jira_intake.run_jira_intake(1000.0)

    assert [(r.method, r.url.path) for r in fake.writes()] == [
        ("PUT", "/rest/api/3/issue/ABC-123"),
        ("POST", "/rest/api/3/issue/ABC-123/transitions"),
        ("POST", "/rest/api/3/issue/ABC-123/comment"),
    ]
    assert json.loads(fake.writes()[0].content) == {
        "update": {"labels": [{"add": "fagan-planned"}]}
    }
    comment = fake.comments()[0]
    assert (
        "Fagan drafted this story into 2 stories and has 1 questions "
        "before it can be approved. Answer them in Fagan."
    ) in comment
    assert "waiting for team lead approval" not in comment
    assert f"{LINK_ORIGIN}/?plan=jira-abc-123" in comment
    assert QUESTION_TEXT not in comment


def test_plan_without_questions_takes_todays_path_byte_for_byte(
    intake_env, monkeypatch, notices
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-123")]})
    _use_fake(monkeypatch, fake)
    _decompose_with(monkeypatch, {"open_questions": []})

    jira_intake.run_jira_intake(1000.0)

    manifest = intake_env["records"].get_manifest("default", "jira-abc-123")
    assert "guidance" not in manifest
    assert notices == []
    link = f"{LINK_ORIGIN}/?plan=jira-abc-123"
    assert json.loads(fake.comments()[0]) == {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "Fagan planned this story into 2 stories. "
                            "It is waiting for team lead approval.",
                        }
                    ],
                },
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": link,
                            "marks": [{"type": "link", "attrs": {"href": link}}],
                        }
                    ],
                },
            ],
        }
    }


@pytest.mark.parametrize(
    "bad_value",
    ["Which provider?", {"question": "Which provider?"}, [1, 2, 3]],
)
def test_malformed_open_questions_is_ignored_and_the_issue_is_planned_normally(
    intake_env, monkeypatch, notices, bad_value
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-123")]})
    _use_fake(monkeypatch, fake)
    _decompose_with(monkeypatch, {"open_questions": bad_value})

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["planned"] == 1
    assert counts["failed"] == 0
    assert intake_env["ingested"] == ["jira-abc-123"]
    manifest = intake_env["records"].get_manifest("default", "jira-abc-123")
    assert "guidance" not in manifest
    assert notices == []
    assert "Fagan planned this story into 2 stories" in fake.comments()[0]


def test_nine_questions_are_capped_to_five_in_the_guidance_record(
    intake_env, monkeypatch, notices
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    _use_fake(monkeypatch, FakeJira({"ABC": [_issue("ABC-123")]}))
    questions = [f"Question {n}?" for n in range(9)]
    _decompose_with(monkeypatch, {"open_questions": questions})

    jira_intake.run_jira_intake(1000.0)

    manifest = intake_env["records"].get_manifest("default", "jira-abc-123")
    assert manifest["guidance"]["questions"] == questions[:5]


def test_overlong_question_is_truncated_and_the_plan_still_needs_guidance(
    intake_env, monkeypatch, notices
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    _use_fake(monkeypatch, FakeJira({"ABC": [_issue("ABC-123")]}))
    _decompose_with(monkeypatch, {"open_questions": ["q" * 1000]})

    jira_intake.run_jira_intake(1000.0)

    manifest = intake_env["records"].get_manifest("default", "jira-abc-123")
    assert manifest["guidance"]["questions"] == ["q" * 300]


def test_refused_guidance_leaves_a_normally_planned_issue(
    intake_env, monkeypatch, notices, caplog
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-123")]})
    _use_fake(monkeypatch, fake)
    _decompose_with(monkeypatch, {"open_questions": [QUESTION_TEXT]})
    monkeypatch.setattr(
        jira_intake,
        "raise_guidance",
        lambda *args: {"ok": False, "error": "already_needed"},
    )
    caplog.set_level(logging.WARNING)

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["planned"] == 1
    assert counts["failed"] == 0
    assert "Fagan planned this story into 2 stories" in fake.comments()[0]
    assert "Answer them in Fagan" not in fake.comments()[0]
    refusals = [
        r.reason for r in caplog.records if r.getMessage() == "jira guidance not raised"
    ]
    assert refusals == ["already_needed"]


def test_raising_guidance_leaves_a_normally_planned_issue_and_logs_no_text(
    intake_env, monkeypatch, notices, caplog
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    fake = FakeJira({"ABC": [_issue("ABC-123")]})
    _use_fake(monkeypatch, fake)
    _decompose_with(monkeypatch, {"open_questions": [QUESTION_TEXT]})

    def _explode(*args):
        raise RuntimeError(f"store leaked {QUESTION_TEXT}")

    monkeypatch.setattr(jira_intake, "raise_guidance", _explode)
    caplog.set_level(logging.DEBUG)

    counts = jira_intake.run_jira_intake(1000.0)

    assert counts["planned"] == 1
    assert counts["failed"] == 0
    assert intake_env["ingested"] == ["jira-abc-123"]
    assert "Fagan planned this story into 2 stories" in fake.comments()[0]
    assert QUESTION_TEXT not in caplog.text


def test_question_text_and_excerpt_reach_no_log_record(
    intake_env, monkeypatch, notices, caplog
):
    _seed_org(intake_env["records"])
    _seed_team(intake_env["records"], "alpha")
    _use_fake(monkeypatch, FakeJira({"ABC": [_issue("ABC-123", "Refund flow")]}))
    _decompose_with(monkeypatch, {"open_questions": [QUESTION_TEXT]})
    caplog.set_level(logging.DEBUG)

    jira_intake.run_jira_intake(1000.0)

    assert QUESTION_TEXT not in caplog.text
    assert "Refund flow" not in caplog.text
