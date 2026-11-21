import base64
import json
import logging

import httpx
import pytest

from pipeline.jira_client import JiraClient, JiraError, adf_to_text
from pipeline.settings_registry import REGISTRY

SITE = "https://acme.atlassian.net"
EMAIL = "svc-jira@example.com"
ACCOUNT_ID = "5b10ac8d82e05b22cc7d4ef5"
TOKEN_REF = "secret://env/JIRA_API_TOKEN"
TOKEN = "tok-XYZ-do-not-leak"
SEARCH_PATH = "/rest/api/3/search/jql"


@pytest.fixture(autouse=True)
def _jira_token(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return JiraClient(SITE, EMAIL, TOKEN_REF, ACCOUNT_ID, transport=transport)


def respond(status=200, body=None):
    if body is None:
        return httpx.Response(status)
    return httpx.Response(status, json=body)


def recording(responses):
    """Return (handler, seen) where handler pops one response per request."""
    seen = []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler, seen


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_posts_the_jql_and_fields():
    handler, seen = recording([respond(body={"issues": [{"key": "ABC-1"}], "isLast": True})])

    issues = make_client(handler).search("project = ABC", ["summary", "status"])

    assert issues == [{"key": "ABC-1"}]
    assert seen[0].method == "POST"
    assert seen[0].url.path == SEARCH_PATH
    body = json.loads(seen[0].content)
    assert body["jql"] == "project = ABC"
    assert body["fields"] == ["summary", "status"]


def test_search_follows_next_page_token():
    handler, seen = recording(
        [
            respond(body={"issues": [{"key": "ABC-1"}], "nextPageToken": "t1"}),
            respond(body={"issues": [{"key": "ABC-2"}]}),
        ]
    )

    issues = make_client(handler).search("project = ABC", ["summary"])

    assert [issue["key"] for issue in issues] == ["ABC-1", "ABC-2"]
    assert json.loads(seen[1].content)["nextPageToken"] == "t1"


def test_search_stops_after_five_pages():
    always_more = {"issues": [{"key": "ABC-1"}], "nextPageToken": "next"}
    handler, seen = recording([respond(body=always_more)])

    issues = make_client(handler).search("project = ABC", ["summary"])

    assert len(seen) == 5
    assert len(issues) == 5


def test_search_refuses_an_empty_jql_without_a_request():
    handler, seen = recording([respond(body={"issues": []})])

    with pytest.raises(ValueError):
        make_client(handler).search("", ["summary"])
    assert seen == []


def test_search_refuses_a_response_without_an_issue_list():
    handler, _ = recording([respond(body={"issues": "not-a-list"})])

    with pytest.raises(JiraError) as excinfo:
        make_client(handler).search("project = ABC", ["summary"])
    assert excinfo.value.kind == "invalid"


# ---------------------------------------------------------------------------
# issues, sub-tasks, labels, comments
# ---------------------------------------------------------------------------


def test_get_issue_fetches_the_issue_by_key():
    handler, seen = recording([respond(body={"key": "ABC-9", "fields": {}})])

    issue = make_client(handler).get_issue("ABC-9")

    assert issue["key"] == "ABC-9"
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/rest/api/3/issue/ABC-9"


def test_get_issue_refuses_a_malformed_key_without_a_request():
    handler, seen = recording([respond(body={})])

    with pytest.raises(ValueError):
        make_client(handler).get_issue("ABC-9/../../admin")
    assert seen == []


def test_create_subtask_posts_parent_issuetype_and_adf_body_and_returns_key():
    handler, seen = recording([respond(201, {"key": "ABC-7"})])

    key = make_client(handler).create_subtask(
        "ABC-3", "ABC", "Write the plan", "first line\nsecond line", ["plan-ready"]
    )

    assert key == "ABC-7"
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/rest/api/3/issue"
    fields = json.loads(seen[0].content)["fields"]
    assert fields["parent"] == {"key": "ABC-3"}
    assert fields["project"] == {"key": "ABC"}
    assert fields["issuetype"] == {"name": "Sub-task"}
    assert fields["summary"] == "Write the plan"
    assert fields["labels"] == ["plan-ready"]
    assert fields["description"]["type"] == "doc"
    paragraphs = fields["description"]["content"]
    assert [p["content"][0]["text"] for p in paragraphs] == ["first line", "second line"]


def test_create_subtask_refuses_a_bad_parent_key_without_a_request():
    handler, seen = recording([respond(201, {"key": "ABC-7"})])

    with pytest.raises(ValueError):
        make_client(handler).create_subtask("abc", "ABC", "Summary", "body", [])
    assert seen == []


@pytest.mark.parametrize("summary", ["", "x" * 256])
def test_create_subtask_refuses_an_out_of_range_summary(summary):
    handler, seen = recording([respond(201, {"key": "ABC-7"})])

    with pytest.raises(ValueError):
        make_client(handler).create_subtask("ABC-3", "ABC", summary, "body", [])
    assert seen == []


def test_add_labels_sends_one_add_update_per_label():
    handler, seen = recording([respond(204)])

    make_client(handler).add_labels("ABC-3", ["plan-ready", "agent"])

    assert seen[0].method == "PUT"
    assert json.loads(seen[0].content) == {
        "update": {"labels": [{"add": "plan-ready"}, {"add": "agent"}]}
    }


def test_add_labels_refuses_a_label_with_whitespace():
    handler, seen = recording([respond(204)])

    with pytest.raises(ValueError):
        make_client(handler).add_labels("ABC-3", ["has space"])
    assert seen == []


def test_add_comment_posts_a_paragraph_without_link_marks():
    handler, seen = recording([respond(201, {"id": "1"})])

    make_client(handler).add_comment("ABC-3", "Plan is ready")

    body = json.loads(seen[0].content)["body"]
    assert body["content"][0]["content"][0]["text"] == "Plan is ready"
    assert "marks" not in body["content"][0]["content"][0]


def test_add_comment_adds_a_link_mark_when_a_link_is_given():
    handler, seen = recording([respond(201, {"id": "1"})])

    make_client(handler).add_comment(
        "ABC-3", "Plan is ready", link="https://plans.example.com/p/1"
    )

    nodes = json.loads(seen[0].content)["body"]["content"]
    marked = [n for n in nodes if n.get("type") == "paragraph"]
    link_nodes = [
        child
        for paragraph in marked
        for child in paragraph.get("content", [])
        if child.get("marks")
    ]
    assert link_nodes[0]["marks"] == [
        {"type": "link", "attrs": {"href": "https://plans.example.com/p/1"}}
    ]


@pytest.mark.parametrize(
    "link",
    ["http://plans.example.com/p/1", "https://user:pw@plans.example.com/p/1", "javascript:alert(1)"],
)
def test_add_comment_refuses_a_link_that_is_not_plain_https(link):
    handler, seen = recording([respond(201, {"id": "1"})])

    with pytest.raises(ValueError):
        make_client(handler).add_comment("ABC-3", "Plan is ready", link=link)
    assert seen == []


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------

TRANSITIONS = {
    "transitions": [
        {"id": "11", "to": {"name": "In Progress"}},
        {"id": "21", "to": {"name": "Done"}},
    ]
}


def test_transition_to_posts_the_transition_that_matches_the_status():
    handler, seen = recording([respond(200, TRANSITIONS), respond(204)])

    moved = make_client(handler).transition_to("ABC-3", "Done")

    assert moved is True
    assert seen[1].method == "POST"
    assert seen[1].url.path == "/rest/api/3/issue/ABC-3/transitions"
    assert json.loads(seen[1].content) == {"transition": {"id": "21"}}


def test_transition_to_matches_the_status_name_case_insensitively():
    handler, seen = recording([respond(200, TRANSITIONS), respond(204)])

    assert make_client(handler).transition_to("ABC-3", "in progress") is True
    assert json.loads(seen[1].content) == {"transition": {"id": "11"}}


def test_transition_to_returns_false_without_posting_when_none_matches():
    handler, seen = recording([respond(200, TRANSITIONS)])

    moved = make_client(handler).transition_to("ABC-3", "Parent Review")

    assert moved is False
    assert [request.method for request in seen] == ["GET"]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "auth"),
        (403, "auth"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "unavailable"),
        (503, "unavailable"),
        (400, "invalid"),
        (409, "invalid"),
    ],
)
def test_http_status_maps_to_the_error_kind(status, kind):
    handler, _ = recording([respond(status, {"errorMessages": ["secret detail"]})])

    with pytest.raises(JiraError) as excinfo:
        make_client(handler).get_issue("ABC-1")

    assert excinfo.value.kind == kind
    assert excinfo.value.status == status


def test_timeout_maps_to_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(JiraError) as excinfo:
        make_client(handler).get_issue("ABC-1")

    assert excinfo.value.kind == "unavailable"


def test_redirect_is_not_followed_and_maps_to_invalid():
    handler, seen = recording(
        [httpx.Response(302, headers={"Location": "https://evil.example/"})]
    )

    with pytest.raises(JiraError) as excinfo:
        make_client(handler).get_issue("ABC-1")

    assert excinfo.value.kind == "invalid"
    assert len(seen) == 1


def test_error_message_carries_kind_and_status_only():
    handler, _ = recording([respond(401, {"errorMessages": ["Bearer leaked"]})])

    with pytest.raises(JiraError) as excinfo:
        make_client(handler).get_issue("ABC-1")

    message = str(excinfo.value)
    assert "auth" in message
    assert "401" in message
    assert TOKEN not in message
    assert "leaked" not in message


def test_missing_token_fails_closed_as_auth_without_a_request(monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN")
    handler, seen = recording([respond(200, {})])

    with pytest.raises(JiraError) as excinfo:
        make_client(handler).get_issue("ABC-1")

    assert excinfo.value.kind == "auth"
    assert seen == []


def test_authorization_is_basic_built_from_the_resolved_token():
    handler, seen = recording([respond(200, {"key": "ABC-1"})])

    make_client(handler).get_issue("ABC-1")

    expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert seen[0].headers["Authorization"] == f"Basic {expected}"


def test_token_never_appears_in_logs_or_error_text(caplog):
    caplog.set_level(logging.DEBUG)
    handler, _ = recording([respond(500, {"errorMessages": ["boom"]})])

    with pytest.raises(JiraError) as excinfo:
        make_client(handler).get_issue("ABC-1")

    encoded = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert TOKEN not in caplog.text
    assert encoded not in caplog.text
    assert TOKEN not in str(excinfo.value)


@pytest.mark.parametrize(
    ("site", "email", "token_ref", "account_id"),
    [
        ("http://acme.atlassian.net", EMAIL, TOKEN_REF, ACCOUNT_ID),
        ("https://evil.com", EMAIL, TOKEN_REF, ACCOUNT_ID),
        ("https://acme.atlassian.net.evil.com", EMAIL, TOKEN_REF, ACCOUNT_ID),
        (SITE, EMAIL, "ATATT3xFfGF0-raw-token", ACCOUNT_ID),
        (SITE, "not-an-email", TOKEN_REF, ACCOUNT_ID),
        (SITE, EMAIL, TOKEN_REF, ""),
    ],
)
def test_client_refuses_an_invalid_site_email_ref_or_account(site, email, token_ref, account_id):
    with pytest.raises(ValueError):
        JiraClient(site, email, token_ref, account_id)


# ---------------------------------------------------------------------------
# adf_to_text
# ---------------------------------------------------------------------------


def paragraph(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def test_adf_to_text_joins_paragraphs_with_newlines():
    adf = {"type": "doc", "version": 1, "content": [paragraph("one"), paragraph("two")]}

    assert adf_to_text(adf) == "one\ntwo"


def test_adf_to_text_renders_nested_bullet_lists_with_indentation():
    inner = {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [paragraph("inner")]}],
    }
    outer = {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [paragraph("outer"), inner]},
            {"type": "listItem", "content": [paragraph("second")]},
        ],
    }

    assert adf_to_text({"type": "doc", "content": [outer]}) == "- outer\n  - inner\n- second"


def test_adf_to_text_renders_ordered_lists_with_numbers():
    ordered = {
        "type": "orderedList",
        "content": [
            {"type": "listItem", "content": [paragraph("first")]},
            {"type": "listItem", "content": [paragraph("second")]},
        ],
    }

    assert adf_to_text({"type": "doc", "content": [ordered]}) == "1. first\n2. second"


def test_adf_to_text_keeps_code_block_lines():
    code = {
        "type": "codeBlock",
        "content": [{"type": "text", "text": "x = 1\ny = 2"}],
    }

    assert adf_to_text({"type": "doc", "content": [code]}) == "x = 1\ny = 2"


def test_adf_to_text_drops_text_marks():
    marked = {
        "type": "paragraph",
        "content": [{"type": "text", "text": "bold", "marks": [{"type": "strong"}]}],
    }

    assert adf_to_text({"type": "doc", "content": [marked]}) == "bold"


def test_adf_to_text_keeps_the_text_of_an_unknown_node():
    panel = {"type": "panel", "content": [paragraph("inside panel")]}

    assert adf_to_text({"type": "doc", "content": [panel]}) == "inside panel"


def test_adf_to_text_caps_the_output_at_20000_characters():
    huge = {"type": "doc", "content": [paragraph("a" * 25000)]}

    assert len(adf_to_text(huge)) == 20000


def test_adf_to_text_stops_at_a_deep_nesting_without_raising():
    node = {"type": "text", "text": "deep"}
    for _ in range(200):
        node = {"type": "panel", "content": [node]}

    assert adf_to_text({"type": "doc", "content": [node]}) == ""


@pytest.mark.parametrize("value", [None, "plain string", ["list"], 42])
def test_adf_to_text_returns_empty_for_a_non_document(value):
    assert adf_to_text(value) == ""


# ---------------------------------------------------------------------------
# settings keys
# ---------------------------------------------------------------------------

VALID_SITE = {
    "site": SITE,
    "email": EMAIL,
    "token_ref": TOKEN_REF,
    "account_id": ACCOUNT_ID,
}

VALID_BOARD = {
    "enabled": True,
    "project_key": "ABC",
    "repo_url": "https://github.com/acme/app.git",
    "poll_seconds": 300,
    "status": {
        "in_progress": "In Progress",
        "in_review": "In Review",
        "done": "Done",
        "parent_review": "Parent Review",
    },
}


def test_jira_site_is_an_org_only_pref_with_an_empty_safe_value():
    key = REGISTRY["jira_site"]
    assert (key.kind, key.type, key.safe_value, key.allowed_layers) == (
        "pref",
        "jira_site",
        {},
        ("org",),
    )


def test_jira_board_is_a_team_only_pref_with_an_empty_safe_value():
    key = REGISTRY["jira_board"]
    assert (key.kind, key.type, key.safe_value, key.allowed_layers) == (
        "pref",
        "jira_board",
        {},
        ("team",),
    )


def test_jira_site_accepts_a_well_formed_site():
    assert REGISTRY["jira_site"].accepts(VALID_SITE) is True


def test_jira_site_accepts_its_empty_safe_value():
    assert REGISTRY["jira_site"].accepts({}) is True


@pytest.mark.parametrize(
    "site",
    [
        "http://x.atlassian.net",
        "https://evil.com",
        "https://a.atlassian.net.evil.com",
        "https://Acme.atlassian.net",
        "https://acme.atlassian.net/extra",
        "https://acme.atlassian.net.",
    ],
)
def test_jira_site_refuses_a_site_outside_atlassian_cloud(site):
    assert REGISTRY["jira_site"].accepts({**VALID_SITE, "site": site}) is False


def test_jira_site_refuses_a_token_value_in_place_of_a_ref():
    value = {**VALID_SITE, "token_ref": "ATATT3xFfGF0-raw-token"}
    assert REGISTRY["jira_site"].accepts(value) is False


@pytest.mark.parametrize("missing", ["site", "email", "token_ref", "account_id"])
def test_jira_site_refuses_a_missing_field(missing):
    value = {k: v for k, v in VALID_SITE.items() if k != missing}
    assert REGISTRY["jira_site"].accepts(value) is False


def test_jira_site_refuses_an_extra_field():
    assert REGISTRY["jira_site"].accepts({**VALID_SITE, "password": "x"}) is False


def test_jira_board_accepts_a_well_formed_board():
    assert REGISTRY["jira_board"].accepts(VALID_BOARD) is True


@pytest.mark.parametrize("poll_seconds", [60, 3600])
def test_jira_board_accepts_poll_seconds_at_the_bounds(poll_seconds):
    assert REGISTRY["jira_board"].accepts({**VALID_BOARD, "poll_seconds": poll_seconds}) is True


@pytest.mark.parametrize("poll_seconds", [59, 3601, True, "300"])
def test_jira_board_refuses_poll_seconds_outside_the_bounds_or_wrong_type(poll_seconds):
    assert REGISTRY["jira_board"].accepts({**VALID_BOARD, "poll_seconds": poll_seconds}) is False


@pytest.mark.parametrize("project_key", ["abc", "A", "ABCDEFGHIJK", "AB-C"])
def test_jira_board_refuses_a_malformed_project_key(project_key):
    assert REGISTRY["jira_board"].accepts({**VALID_BOARD, "project_key": project_key}) is False


@pytest.mark.parametrize("name", ["in_progress", "in_review", "done", "parent_review"])
def test_jira_board_refuses_a_missing_status_name(name):
    status = {k: v for k, v in VALID_BOARD["status"].items() if k != name}
    assert REGISTRY["jira_board"].accepts({**VALID_BOARD, "status": status}) is False


@pytest.mark.parametrize("status_name", ["", "x" * 61, 7])
def test_jira_board_refuses_an_out_of_range_status_name(status_name):
    status = {**VALID_BOARD["status"], "done": status_name}
    assert REGISTRY["jira_board"].accepts({**VALID_BOARD, "status": status}) is False


@pytest.mark.parametrize(
    "repo_url",
    [
        "http://github.com/acme/app.git",
        "https://user:pw@github.com/acme/app.git",
        "git@github.com:acme/app.git",
        "https://github.com/acme/app.git?token=1",
    ],
)
def test_jira_board_refuses_a_repo_url_that_is_not_plain_https(repo_url):
    assert REGISTRY["jira_board"].accepts({**VALID_BOARD, "repo_url": repo_url}) is False


def test_jira_board_refuses_a_non_boolean_enabled():
    assert REGISTRY["jira_board"].accepts({**VALID_BOARD, "enabled": "yes"}) is False


def test_jira_board_accepts_its_empty_safe_value():
    assert REGISTRY["jira_board"].accepts({}) is True
