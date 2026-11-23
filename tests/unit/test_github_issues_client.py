import json
import logging

import httpx
import pytest

from app.credentials import CredentialMissing, CredentialRef
from pipeline import github_issues_client
from pipeline.github_issues_client import (
    GitHubIssuesClient,
    GitHubIssuesError,
    token_source_for,
)

OWNER = "acme"
REPO = "widgets"
TOKEN = "ghp_do-not-leak-123"
ISSUES_PATH = "/repos/acme/widgets/issues"


def token():
    return TOKEN


def make_client(handler, token_source=token):
    transport = httpx.MockTransport(handler)
    return GitHubIssuesClient(OWNER, REPO, token_source, transport=transport)


def respond(status=200, body=None, headers=None):
    if body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json=body, headers=headers)


def recording(responses):
    """Return (handler, seen): the handler records each request and pops one response."""
    seen = []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler, seen


def failing(exc):
    def handler(request):
        raise exc

    return handler


# ---------------------------------------------------------------------------
# list_open_issues
# ---------------------------------------------------------------------------


def test_list_open_issues_gets_open_issues_with_the_label_and_limit():
    handler, seen = recording([respond(body=[{"number": 1, "title": "a"}])])

    issues = make_client(handler).list_open_issues("fagan", 25)

    assert issues == [{"number": 1, "title": "a"}]
    assert seen[0].method == "GET"
    assert seen[0].url.host == "api.github.com"
    assert seen[0].url.path == ISSUES_PATH
    assert seen[0].url.params["state"] == "open"
    assert seen[0].url.params["labels"] == "fagan"
    assert seen[0].url.params["per_page"] == "25"


def test_list_open_issues_drops_pull_requests_the_api_lists_as_issues():
    body = [
        {"number": 1},
        {"number": 2, "pull_request": {"url": "https://api.github.com/repos/x"}},
    ]
    handler, _ = recording([respond(body=body)])

    assert make_client(handler).list_open_issues("fagan", 10) == [{"number": 1}]


def test_list_open_issues_sends_the_bearer_token_and_json_accept_header():
    handler, seen = recording([respond(body=[])])

    make_client(handler).list_open_issues("fagan", 10)

    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["Accept"] == "application/vnd.github+json"


@pytest.mark.parametrize("max_results", [0, 101, -1, True, "10", 10.0])
def test_list_open_issues_refuses_a_limit_outside_one_to_one_hundred(max_results):
    handler, seen = recording([respond(body=[])])

    with pytest.raises(ValueError):
        make_client(handler).list_open_issues("fagan", max_results)
    assert seen == []


@pytest.mark.parametrize("label", ["", "has space", "a/b", "x" * 51])
def test_list_open_issues_refuses_a_malformed_label_before_any_request(label):
    handler, seen = recording([respond(body=[])])

    with pytest.raises(ValueError):
        make_client(handler).list_open_issues(label, 10)
    assert seen == []


def test_list_open_issues_refuses_a_body_that_is_not_a_list():
    handler, _ = recording([respond(body={"message": "nope"})])

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler).list_open_issues("fagan", 10)
    assert raised.value.kind == "invalid"


# ---------------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------------


def test_get_issue_gets_the_issue_by_number():
    handler, seen = recording([respond(body={"number": 7, "title": "t"})])

    issue = make_client(handler).get_issue(7)

    assert issue == {"number": 7, "title": "t"}
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"{ISSUES_PATH}/7"


# ---------------------------------------------------------------------------
# add_labels / remove_label
# ---------------------------------------------------------------------------


def test_add_labels_posts_the_label_names_to_the_issue():
    handler, seen = recording([respond(body=[{"name": "fagan-planned"}])])

    make_client(handler).add_labels(7, ["fagan-planned", "fagan"])

    assert seen[0].method == "POST"
    assert seen[0].url.path == f"{ISSUES_PATH}/7/labels"
    assert json.loads(seen[0].content) == {"labels": ["fagan-planned", "fagan"]}


def test_add_labels_with_no_labels_sends_no_request():
    handler, seen = recording([respond(body=[])])

    make_client(handler).add_labels(7, [])

    assert seen == []


def test_add_labels_refuses_a_label_with_a_slash_before_any_request():
    handler, seen = recording([respond(body=[])])

    with pytest.raises(ValueError):
        make_client(handler).add_labels(7, ["a/b"])
    assert seen == []


def test_remove_label_deletes_the_label_from_the_issue():
    handler, seen = recording([respond(status=200, body=[])])

    make_client(handler).remove_label(7, "fagan")

    assert seen[0].method == "DELETE"
    assert seen[0].url.path == f"{ISSUES_PATH}/7/labels/fagan"


@pytest.mark.parametrize("label", ["..", ".", "a/b", ""])
def test_remove_label_refuses_a_label_that_could_rewrite_the_path(label):
    handler, seen = recording([respond(status=200, body=[])])

    with pytest.raises(ValueError):
        make_client(handler).remove_label(7, label)
    assert seen == []


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------


def test_add_comment_posts_the_text_and_returns_the_new_comment_id():
    handler, seen = recording([respond(status=201, body={"id": 99})])

    comment_id = make_client(handler).add_comment(7, "planned")

    assert comment_id == 99
    assert seen[0].method == "POST"
    assert seen[0].url.path == f"{ISSUES_PATH}/7/comments"
    assert json.loads(seen[0].content) == {"body": "planned"}


def test_add_comment_appends_an_https_link_paragraph_when_given():
    handler, seen = recording([respond(status=201, body={"id": 99})])

    make_client(handler).add_comment(7, "planned", link="https://fagan.example/plans/x")

    body = json.loads(seen[0].content)["body"]
    assert body == "planned\n\nhttps://fagan.example/plans/x"


@pytest.mark.parametrize(
    "link",
    [
        "http://fagan.example/x",
        "https://user@fagan.example/x",
        "https://fagan.example/a b",
    ],
)
def test_add_comment_refuses_a_link_that_is_not_a_plain_https_url(link):
    handler, seen = recording([respond(status=201, body={"id": 99})])

    with pytest.raises(ValueError):
        make_client(handler).add_comment(7, "planned", link=link)
    assert seen == []


def test_add_comment_refuses_a_response_without_a_comment_id():
    handler, _ = recording([respond(status=201, body={"body": "planned"})])

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler).add_comment(7, "planned")
    assert raised.value.kind == "invalid"


def test_update_comment_patches_the_comment_text():
    handler, seen = recording([respond(status=200, body={"id": 99})])

    make_client(handler).update_comment(99, "updated")

    assert seen[0].method == "PATCH"
    assert seen[0].url.path == "/repos/acme/widgets/issues/comments/99"
    assert json.loads(seen[0].content) == {"body": "updated"}


@pytest.mark.parametrize("comment_id", [0, -1, True, "99"])
def test_update_comment_refuses_a_comment_id_that_is_not_a_positive_int(comment_id):
    handler, seen = recording([respond(status=200, body={"id": 99})])

    with pytest.raises(ValueError):
        make_client(handler).update_comment(comment_id, "updated")
    assert seen == []


def test_list_comments_gets_the_comments_on_the_issue():
    handler, seen = recording([respond(body=[{"id": 1, "body": "a"}])])

    comments = make_client(handler).list_comments(7, 30)

    assert comments == [{"id": 1, "body": "a"}]
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"{ISSUES_PATH}/7/comments"
    assert seen[0].url.params["per_page"] == "30"


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "owner", ["acme/evil", "..", ".", "a b", "", "acme?x=1", "acme#f"]
)
def test_owner_with_a_slash_dot_dot_space_or_query_is_refused_before_any_request(owner):
    handler, seen = recording([respond(body={})])

    with pytest.raises(ValueError):
        GitHubIssuesClient(owner, REPO, token, transport=httpx.MockTransport(handler))
    assert seen == []


@pytest.mark.parametrize("repo", ["widgets/evil", "..", ".", "wid gets", ""])
def test_repo_with_a_slash_dot_dot_or_space_is_refused_before_any_request(repo):
    handler, seen = recording([respond(body={})])

    with pytest.raises(ValueError):
        GitHubIssuesClient(OWNER, repo, token, transport=httpx.MockTransport(handler))
    assert seen == []


@pytest.mark.parametrize("number", [0, -3, True, "7", 7.0, None])
def test_issue_number_that_is_not_a_positive_int_is_refused_before_any_request(number):
    handler, seen = recording([respond(body={})])

    with pytest.raises(ValueError):
        make_client(handler).get_issue(number)
    assert seen == []


def test_a_token_source_that_is_not_callable_is_refused():
    with pytest.raises(TypeError):
        GitHubIssuesClient(OWNER, REPO, "ghp_plain")


# ---------------------------------------------------------------------------
# token handling
# ---------------------------------------------------------------------------


def test_missing_token_raises_auth_with_zero_requests():
    handler, seen = recording([respond(body={})])

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler, token_source=lambda: None).get_issue(7)

    assert raised.value.kind == "auth"
    assert seen == []


def test_token_source_that_reports_missing_credential_raises_auth_with_zero_requests():
    def missing():
        raise CredentialMissing("credential_missing")

    handler, seen = recording([respond(body={})])

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler, token_source=missing).get_issue(7)

    assert raised.value.kind == "auth"
    assert seen == []


def test_token_source_is_called_once_per_request():
    calls = []

    def counting():
        calls.append(1)
        return TOKEN

    handler, seen = recording(
        [respond(body={"number": 7}), respond(body={"number": 7})]
    )
    client = make_client(handler, token_source=counting)

    client.get_issue(7)
    client.get_issue(7)

    assert len(calls) == 2
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# status and transport mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "kind"),
    [
        (401, None, "auth"),
        (403, None, "auth"),
        (403, {"X-RateLimit-Remaining": "0"}, "rate_limited"),
        (403, {"Retry-After": "60"}, "rate_limited"),
        (404, None, "not_found"),
        (422, None, "invalid"),
        (429, None, "rate_limited"),
        (500, None, "unavailable"),
        (503, None, "unavailable"),
    ],
)
def test_status_maps_to_the_error_kind(status, headers, kind):
    handler, _ = recording([respond(status=status, headers=headers)])

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler).get_issue(7)

    assert raised.value.kind == kind
    assert raised.value.status == status


def test_redirect_is_not_followed_to_another_host():
    handler, seen = recording(
        [respond(status=302, headers={"Location": "https://evil.example/steal"})]
    )

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler).get_issue(7)

    assert raised.value.kind == "invalid"
    assert len(seen) == 1
    assert seen[0].url.host == "api.github.com"


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["connect", "timeout"],
)
def test_transport_failure_raises_unavailable(exc):
    with pytest.raises(GitHubIssuesError) as raised:
        make_client(failing(exc)).get_issue(7)

    assert raised.value.kind == "unavailable"
    assert raised.value.status is None


def test_a_response_body_that_is_not_json_raises_invalid():
    handler = lambda request: httpx.Response(200, content=b"<html>")

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler).get_issue(7)

    assert raised.value.kind == "invalid"


def test_a_non_object_entry_in_the_issue_list_raises_invalid():
    handler, _ = recording([respond(body=[{"number": 1}, "not-a-dict"])])

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler).list_open_issues("fagan", 10)
    assert raised.value.kind == "invalid"


# ---------------------------------------------------------------------------
# the token never reaches an error, a repr or a log record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(401),
        lambda request: httpx.Response(500),
        failing(httpx.ConnectError(f"boom {TOKEN}")),
    ],
    ids=["unauthorized", "server", "transport"],
)
def test_token_never_appears_in_an_error_repr_message_or_log(handler, caplog):
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GitHubIssuesError) as raised:
        make_client(handler).get_issue(7)

    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(raised.value)
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in repr(record.__dict__)


def test_log_record_does_not_carry_the_repository_or_issue_number(caplog):
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GitHubIssuesError):
        make_client(lambda request: httpx.Response(404)).get_issue(7)

    text = " ".join(repr(record.__dict__) for record in caplog.records)
    assert "acme/widgets" not in text
    assert "issues/7" not in text


def test_error_kind_must_be_one_of_the_known_kinds():
    with pytest.raises(ValueError):
        GitHubIssuesError("teapot")


# ---------------------------------------------------------------------------
# token_source_for
# ---------------------------------------------------------------------------


def test_empty_token_ref_reads_the_namespace_forge_token(monkeypatch):
    seen = []

    def fake_resolve(ref):
        seen.append(ref)
        return TOKEN

    monkeypatch.setattr(github_issues_client, "resolve_for_injection", fake_resolve)

    source = token_source_for("team-a", "")

    assert source() == TOKEN
    assert seen == [CredentialRef("forge", "github_token", namespace="team-a")]


def test_empty_token_ref_returns_none_when_the_namespace_has_no_forge_token(
    monkeypatch,
):
    def missing(ref):
        raise CredentialMissing("credential_missing")

    monkeypatch.setattr(github_issues_client, "resolve_for_injection", missing)

    assert token_source_for("team-a", "")() is None


def test_secret_ref_named_github_issues_resolves_through_the_secret_seam(monkeypatch):
    seen = []

    def fake_resolve(ref):
        seen.append(ref)
        return TOKEN

    monkeypatch.setattr(github_issues_client, "resolve_for_injection", fake_resolve)

    source = token_source_for("team-a", "secret://file/github-issues-token")

    assert source() == TOKEN
    assert seen == [CredentialRef("secret", "file/github-issues-token")]


@pytest.mark.parametrize(
    "token_ref",
    [
        "secret://env/JIRA_API_TOKEN",
        "secret://env/GITHUB_TOKEN",
        "secret://file/forge-token",
        "secret://file/xgithub-issues",
    ],
)
def test_secret_ref_whose_name_does_not_start_with_github_issues_is_refused(
    monkeypatch, token_ref
):
    seen = []
    monkeypatch.setattr(
        github_issues_client,
        "resolve_for_injection",
        lambda ref: seen.append(ref) or TOKEN,
    )

    with pytest.raises(ValueError):
        token_source_for("team-a", token_ref)
    assert seen == []


@pytest.mark.parametrize("token_ref", ["ghp_plaintext", "https://github.com/acme"])
def test_token_ref_that_is_not_a_secret_reference_is_refused_without_echo(token_ref):
    with pytest.raises(ValueError) as raised:
        token_source_for("team-a", token_ref)
    assert token_ref not in str(raised.value)
