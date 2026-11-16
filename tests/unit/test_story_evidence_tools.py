"""Redacted story log / journal MCP read tools."""

# ruff: noqa: I001
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline import server
from pipeline import server_tools_read, workspace_reader
from pipeline.remote_tool_policy import (
    PLAN_SCOPED_READ,
    REMOTE_READ,
    remote_tool_allowed,
    remote_tool_policy,
)
from pipeline.story_evidence import redact
from app.identity import Principal

TOOLS = ("get_story_log", "get_story_journal")


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "gho_" + "b" * 36,
        "ghs_" + "c" * 36,
        "github_pat_" + "d" * 40,
        "sk-" + "e" * 40,
        "sk-ant-api03-" + "f" * 40,
    ],
)
def test_should_mask_token_shapes(secret):
    out = redact(f"using {secret} now")
    assert secret not in out
    assert "[redacted]" in out


def test_should_mask_bearer_value_and_keep_scheme():
    out = redact("Authorization: Bearer abc.def-123")
    assert "abc.def-123" not in out
    assert out.startswith("Authorization: Bearer")


@pytest.mark.parametrize(
    "line,name",
    [
        ("GITHUB_TOKEN=abc123", "GITHUB_TOKEN"),
        ("CLIENT_SECRET: hunter2", "CLIENT_SECRET"),
        ("DB_PASSWORD=hunter2", "DB_PASSWORD"),
        ("OPENAI_API_KEY=zzz", "OPENAI_API_KEY"),
        ("SIGNING_KEY: zzz", "SIGNING_KEY"),
    ],
)
def test_should_mask_named_values_and_keep_name(line, name):
    out = redact(line)
    assert out.startswith(name)
    assert "hunter2" not in out and "abc123" not in out and "zzz" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize(
    "text",
    [
        "the sky is blue",
        "task-list and ask-me sk-short",
        "ghp_short",
        "TOKENIZER=fast",
        "monkey: banana",
        "PATH=/usr/bin",
    ],
)
def test_should_leave_lookalike_text_unchanged(text):
    assert redact(text) == text


def test_should_mask_pem_block_whole():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\nDEFghi\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact(f"before\n{pem}\nafter")
    assert "MIIabc" not in out and "DEFghi" not in out and "PRIVATE KEY" not in out
    assert out.startswith("before") and out.endswith("after")


@pytest.fixture
def fake_service(monkeypatch):
    service = MagicMock()
    service.get_manifest_or_none.return_value = {"stories": {"S-1": {}}}
    monkeypatch.setattr(server, "_service", service)
    return service


def test_should_register_tools_on_the_mcp_tool_manager():
    for name in TOOLS:
        assert server.mcp._tool_manager.get_tool(name).fn is getattr(server_tools_read, name)


@pytest.mark.parametrize("given,expected", [(1, 1), (500, 500), (0, 1), (-1, 1), (501, 500), (10**9, 500)])
def test_should_clamp_lines(fake_service, given, expected):
    fake_service.get_story_log.return_value = {"available": True, "lines": []}
    server_tools_read.get_story_log("p", "S-1", lines=given)
    assert fake_service.get_story_log.call_args.args[3] == expected


@pytest.mark.parametrize("bad", ["abc", None, 2.5, True])
def test_should_clamp_non_integer_lines_without_error(fake_service, bad):
    fake_service.get_story_log.return_value = {"available": True, "lines": []}
    server_tools_read.get_story_log("p", "S-1", lines=bad)
    assert 1 <= fake_service.get_story_log.call_args.args[3] <= 500


def test_should_redact_every_log_line(fake_service):
    fake_service.get_story_log.return_value = {
        "available": True,
        "lines": ["ok", "GITHUB_TOKEN=abc", "ghp_" + "a" * 36],
    }
    result = server_tools_read.get_story_log("p", "S-1")
    assert result["lines"][0] == "ok"
    assert all("abc" not in x and "ghp_" not in x for x in result["lines"][1:])


def test_should_mask_pem_spanning_log_lines(fake_service):
    fake_service.get_story_log.return_value = {
        "available": True,
        "lines": ["x", "-----BEGIN PRIVATE KEY-----", "MIIsecret", "-----END PRIVATE KEY-----", "y"],
    }
    result = server_tools_read.get_story_log("p", "S-1")
    assert "MIIsecret" not in "\n".join(result["lines"])
    assert result["lines"][0] == "x" and result["lines"][-1] == "y"


def test_should_return_unavailable_when_story_has_no_log(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_service", MagicMock(
        get_manifest_or_none=lambda _p: {"stories": {"S-1": {}}},
        get_story_log=lambda p, s, m, n: workspace_reader.local_workspace_reader.read_story_log(p, s, m, n),
    ))
    assert server_tools_read.get_story_log("p", "S-1") == {"available": False, "lines": []}


def test_should_match_the_existing_reader_for_an_unredacted_log(tmp_path, monkeypatch):
    log = tmp_path / "S-1.log"
    log.write_text("one\ntwo\nthree\n")
    monkeypatch.setattr(workspace_reader, "_story_log_root", lambda: tmp_path)
    manifest = {"stories": {"S-1": {"log": "S-1.log"}}}
    reader = workspace_reader.local_workspace_reader
    monkeypatch.setattr(server, "_service", MagicMock(
        get_manifest_or_none=lambda _p: manifest,
        get_story_log=lambda p, s, m, n: reader.read_story_log(p, s, m, n),
    ))
    assert server_tools_read.get_story_log("p", "S-1", lines=2) == reader.read_story_log(
        "p", "S-1", manifest, 2
    )


def test_should_answer_unknown_story_with_404_for_log_and_journal(fake_service):
    fake_service.get_manifest_or_none.return_value = None
    expected = {"ok": False, "status": 404, "error": "unknown plan/story"}
    assert server_tools_read.get_story_log("nope", "S-1") == expected
    assert server_tools_read.get_story_journal("nope", "S-1") == expected
    fake_service.get_manifest_or_none.return_value = {"stories": {}}
    assert server_tools_read.get_story_journal("p", "S-9") == expected


def test_should_redact_journal_fields_and_keep_shape(fake_service):
    fake_service.get_journal.return_value = (
        True,
        [{"step": "a", "summary": "set GITHUB_TOKEN=abc", "next_hint": None, "ts": "t", "n": 3,
          "extra": {"k": ["Authorization: Bearer xyz"]}}],
    )
    result = server_tools_read.get_story_journal("p", "S-1")
    entry = result["entries"][0]
    assert result["available"] is True
    assert entry["summary"].startswith("set GITHUB_TOKEN") and "abc" not in entry["summary"]
    assert entry["next_hint"] is None and entry["n"] == 3 and entry["ts"] == "t"
    assert "xyz" not in str(entry["extra"])


def test_should_return_same_journal_entries_when_nothing_to_redact(fake_service):
    entries = [{"step": "a", "summary": "did x", "next_hint": "do y", "ts": "t"}]
    fake_service.get_journal.return_value = (True, entries)
    assert server_tools_read.get_story_journal("p", "S-1") == {"available": True, "entries": entries}


def test_should_report_journal_unavailable_when_empty(fake_service):
    fake_service.get_journal.return_value = (False, [])
    assert server_tools_read.get_story_journal("p", "S-1") == {"available": False, "entries": []}


@pytest.mark.parametrize("name", TOOLS)
def test_should_classify_tools_as_plan_scoped_remote_reads(name):
    assert name in REMOTE_READ and name in PLAN_SCOPED_READ
    assert remote_tool_allowed(name) is True


class _Ctx:
    method = "tools/call"

    def __init__(self, params):
        self.params = params


@pytest.mark.parametrize("name", TOOLS)
def test_should_answer_hidden_and_missing_plan_identically(name, tmp_path, monkeypatch):
    import pipeline.persistence as ppers
    from pipeline.actor import bind_principal

    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    (plans / "alpha.manifest.json").write_text('{"plan": "alpha", "owner": "https://idp#alice"}')
    eve = Principal(subject="https://idp#eve", auth_method="oidc")

    async def call(plan):
        async def nxt(_ctx):
            return "REACHED"
        with bind_principal(eve):
            return await remote_tool_policy(
                _Ctx({"name": name, "arguments": {"plan_name": plan, "story_key": "S-1"}}), nxt
            )

    hidden = asyncio.run(call("alpha"))
    missing = asyncio.run(call("ghost"))
    assert hidden != "REACHED" and hidden["isError"] is True
    assert hidden == missing


def test_server_stays_under_the_line_cap():
    assert len(Path(server.__file__).read_text().splitlines()) < 1000
