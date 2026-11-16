"""A story log or journal must never hand a credential back to the caller.

The two read tools return the tail of a story log and a story journal. Every
value they carry may hold a credential, so the masking has to catch the
shapes a credential really arrives in -- lower case, quoted, hyphenated and
inside a JSON body -- while leaving ordinary prose alone.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.identity import Principal
from pipeline import server, server_tools_read, workspace_reader
from pipeline.remote_tool_policy import PLAN_SCOPED_READ, REMOTE_READ
from pipeline.story_evidence import redact

REDACTED = "[redacted]"

#: (text, the name that must survive the masking, the value that must not).
NAMED_SECRETS = [
    ("password=hunter2", "password", "hunter2"),
    ('"password": "hunter2"', "password", "hunter2"),
    ("'token': 'hunter2'", "token", "hunter2"),
    ("token: hunter2", "token", "hunter2"),
    ('{"api_key":"hunter2"}', "api_key", "hunter2"),
    ("x-api-key: hunter2", "x-api-key", "hunter2"),
    ("KEY=hunter2", "KEY", "hunter2"),
]

#: Words that only look like a secret name, and must survive untouched.
PLAIN_WORDS = ["monkey=1", "keyboard: x", "author: bob", "ghp_short", "sk-short"]


@pytest.fixture
def fake_service(monkeypatch):
    """A service that knows one story, and records the tail it is asked for."""
    service = MagicMock()
    service.get_manifest_or_none.return_value = {"stories": {"S-1": {}}}
    monkeypatch.setattr(server, "_service", service)
    return service


def _requested_lines(service):
    """The tail length the tool handed to the service."""
    call = service.get_story_log.call_args
    return call.kwargs["lines"] if "lines" in call.kwargs else call.args[3]


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "gho_" + "b" * 36,
        "ghs_" + "c" * 36,
        "github_pat_" + "d" * 40,
        "sk-" + "e" * 40,
    ],
)
def test_should_mask_a_provider_token(secret):
    out = redact(f"using {secret} now")
    assert secret not in out
    assert REDACTED in out


def test_should_mask_a_bearer_value_and_keep_the_scheme():
    out = redact("Authorization: Bearer abc.def-123")
    assert "abc.def-123" not in out
    assert out.startswith("Authorization: Bearer")


@pytest.mark.parametrize("text,name,value", NAMED_SECRETS)
def test_should_mask_a_named_secret_and_keep_its_name(text, name, value):
    out = redact(text)
    assert value not in out
    assert name in out
    assert REDACTED in out


@pytest.mark.parametrize("text", PLAIN_WORDS)
def test_should_not_mask_a_plain_word(text):
    assert redact(text) == text


def test_should_mask_a_pem_block_in_a_log_whole(fake_service):
    fake_service.get_story_log.return_value = {
        "available": True,
        "lines": [
            "before",
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIsecretbody",
            "-----END RSA PRIVATE KEY-----",
            "after",
        ],
    }
    result = server_tools_read.get_story_log("p", "S-1")
    joined = "\n".join(result["lines"])
    assert "MIIsecretbody" not in joined
    assert "PRIVATE KEY" not in joined
    assert result["lines"][0] == "before"
    assert result["lines"][-1] == "after"


def test_should_redact_every_log_line(fake_service):
    fake_service.get_story_log.return_value = {
        "available": True,
        "lines": ["ok", "password=hunter2", "token: hunter3"],
    }
    result = server_tools_read.get_story_log("p", "S-1")
    assert result["lines"][0] == "ok"
    assert all("hunter" not in line for line in result["lines"][1:])


def test_should_mask_a_journal_value_whose_key_names_a_secret(fake_service):
    fake_service.get_journal.return_value = (
        True,
        [
            {
                "step": "a",
                "summary": "did x",
                "next_hint": None,
                "ts": "t",
                "password": "hunter2",
                "api_key": "hunter3",
                "author": "bob",
            }
        ],
    )
    entry = server_tools_read.get_story_journal("p", "S-1")["entries"][0]
    assert entry["password"] == REDACTED
    assert entry["api_key"] == REDACTED
    assert entry["author"] == "bob"
    assert entry["step"] == "a" and entry["summary"] == "did x"


def test_should_mask_secrets_in_nested_journal_fields(fake_service):
    fake_service.get_journal.return_value = (
        True,
        [
            {
                "step": "a",
                "summary": "password=hunter2",
                "next_hint": None,
                "ts": "t",
                "extra": {"token": "hunter3"},
                "items": ["Authorization: Bearer hunter4"],
            }
        ],
    )
    entry = server_tools_read.get_story_journal("p", "S-1")["entries"][0]
    assert "hunter2" not in entry["summary"]
    assert entry["extra"]["token"] == REDACTED
    assert "hunter4" not in entry["items"][0]


@pytest.mark.parametrize(
    "given,expected", [(1, 1), (500, 500), (0, 1), (-1, 1), (501, 500), (10**9, 500)]
)
def test_should_clamp_the_tail_length(fake_service, given, expected):
    fake_service.get_story_log.return_value = {"available": True, "lines": []}
    server_tools_read.get_story_log("p", "S-1", lines=given)
    assert _requested_lines(fake_service) == expected


@pytest.mark.parametrize("bad", ["not-a-number", None, 2.5, 0, -1, 501])
def test_should_clamp_a_bad_tail_length_without_echoing_it(fake_service, bad):
    fake_service.get_story_log.return_value = {"available": True, "lines": []}
    result = server_tools_read.get_story_log("p", "S-1", lines=bad)
    assert 1 <= _requested_lines(fake_service) <= 500
    assert str(bad) not in str(result)


def test_should_answer_an_unknown_plan_or_story_with_the_same_404(fake_service):
    fake_service.get_manifest_or_none.return_value = None
    unknown_plan = server_tools_read.get_story_log("ghost", "S-1")
    assert unknown_plan["ok"] is False
    assert unknown_plan["status"] == 404
    fake_service.get_manifest_or_none.return_value = {"stories": {}}
    assert server_tools_read.get_story_log("p", "S-9") == unknown_plan
    assert server_tools_read.get_story_journal("ghost", "S-1") == unknown_plan
    assert server_tools_read.get_story_journal("p", "S-9") == unknown_plan


def test_should_report_a_story_without_a_log_as_unavailable(fake_service, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_reader, "_story_log_root", lambda: tmp_path)
    reader = workspace_reader.local_workspace_reader
    fake_service.get_story_log.side_effect = lambda p, s, m, n: reader.read_story_log(p, s, m, n)
    assert server_tools_read.get_story_log("p", "S-1") == {"available": False, "lines": []}


def test_should_return_the_same_lines_the_reader_returns_for_a_clean_log(
    fake_service, tmp_path, monkeypatch
):
    log = tmp_path / "S-1.log"
    log.write_text("one\ntwo\nthree\n")
    monkeypatch.setattr(workspace_reader, "_story_log_root", lambda: tmp_path)
    manifest = {"stories": {"S-1": {"log": "S-1.log"}}}
    fake_service.get_manifest_or_none.return_value = manifest
    reader = workspace_reader.local_workspace_reader
    fake_service.get_story_log.side_effect = lambda p, s, m, n: reader.read_story_log(p, s, m, n)
    assert server_tools_read.get_story_log("p", "S-1", lines=2) == reader.read_story_log(
        "p", "S-1", manifest, 2
    )


def test_should_return_the_same_log_the_dashboard_returns(fake_service, tmp_path, monkeypatch):
    from app import dashboard_routes

    log = tmp_path / "S-1.log"
    log.write_text("one\ntwo\nthree\n")
    monkeypatch.setattr(workspace_reader, "_story_log_root", lambda: tmp_path)
    manifest = {"stories": {"S-1": {"log": "S-1.log"}}}
    fake_service.get_manifest_or_none.return_value = manifest
    reader = workspace_reader.local_workspace_reader
    fake_service.get_story_log.side_effect = lambda p, s, m, n: reader.read_story_log(p, s, m, n)
    monkeypatch.setattr(dashboard_routes, "_service", fake_service)
    assert server_tools_read.get_story_log("p", "S-1", lines=2) == dashboard_routes.get_story_log(
        "p", "S-1", lines=2
    )


def test_should_return_the_journal_the_configured_store_holds(monkeypatch):
    entries = [{"step": "a", "summary": "did x", "next_hint": None, "ts": "t"}]
    store = MagicMock()
    store.get_manifest_or_none.return_value = {"stories": {"S-1": {}}}
    store.get_journal.return_value = (True, entries)
    monkeypatch.setattr(server, "_store", store)
    assert server_tools_read.get_story_journal("p", "S-1") == {"available": True, "entries": entries}
    store.get_journal.assert_called_once_with("p", "S-1")


class _Ctx:
    method = "tools/call"

    def __init__(self, params):
        self.params = params


@pytest.mark.parametrize("name", ("get_story_log", "get_story_journal"))
def test_should_answer_a_plan_the_caller_may_not_see_like_a_missing_plan(name, monkeypatch):
    from pipeline import remote_tool_policy as policy
    from pipeline.actor import bind_principal

    monkeypatch.setattr(policy, "append_host_audit", lambda **kwargs: None)
    monkeypatch.setattr(
        policy.authorization,
        "_load_manifest",
        lambda plan: {"owner": "https://idp#owner"} if plan == "alpha" else {},
    )
    monkeypatch.setattr(policy, "can_view", lambda principal, resource: False)
    reached = []

    async def call_next(_ctx):
        reached.append(True)
        return "REACHED"

    async def call(plan):
        with bind_principal(Principal(subject="https://idp#eve", auth_method="oidc")):
            return await policy.remote_tool_policy(
                _Ctx({"name": name, "arguments": {"plan_name": plan, "story_key": "S-1"}}),
                call_next,
            )

    hidden = asyncio.run(call("alpha"))
    missing = asyncio.run(call("ghost"))
    assert hidden == missing
    assert hidden["isError"] is True
    assert reached == []


@pytest.mark.parametrize("name", ("get_story_log", "get_story_journal"))
def test_should_classify_the_tools_as_plan_scoped_remote_reads(name):
    assert name in REMOTE_READ
    assert name in PLAN_SCOPED_READ
    assert server.mcp._tool_manager.get_tool(name).fn is getattr(server_tools_read, name)
