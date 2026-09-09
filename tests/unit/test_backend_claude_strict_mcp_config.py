"""--strict-mcp-config must be on EVERY ClaudeCliDriver.complete() argv.

Per `claude --help`: "--strict-mcp-config: Only use MCP servers from
--mcp-config, ignoring all other MCP configurations". complete() never passes
--mcp-config, so adding this flag unconditionally means the single-shot
completion subprocess sees ZERO MCP servers - regardless of what the invoking
user's global ~/.claude.json configures. A headless completion subprocess
recursively gaining this repo's own pipeline-control MCP tools (dispatch_story,
ingest_plan, approve_merge, ...) is a least-privilege gap for every role that
calls complete() (chat, planner, overlord, review, test-author), so the flag
is NOT role-conditional: it belongs to the argv built at the top of complete()
(the `cmd = ["claude", "-p", prompt, "--model", model]` line and the appends
that follow it), before any role-specific branching.

These tests reuse the repo's existing subprocess-fake pattern
(tests.unit._backend_helpers._FakeCompletedProcess + monkeypatching
app.backend.subprocess.run) - no real `claude` process is spawned.
"""
import json
from pathlib import Path

import pytest

import app.backend_claude as bc
from app import backend as b
from tests.unit._backend_helpers import (
    _claude_usage_payload,
    _FakeCompletedProcess,
)

FLAG = "--strict-mcp-config"


def _capture_complete_argv(monkeypatch, *, stdout="ok", returncode=0):
    """Install the standard subprocess.run fake (same shape as
    _claude_complete_monkeypatch) and return the dict it records cmd/cwd into."""
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, env=None):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        return _FakeCompletedProcess(stdout=stdout, returncode=returncode)

    monkeypatch.setattr(b.subprocess, "run", _fake_run)
    return captured


def _assert_base_argv_intact(cmd, prompt, model):
    """The flag must be ADDED to the argv, not displace the pinned base shape
    ["claude", "-p", prompt, "--model", model]."""
    assert cmd[0] == "claude"
    assert cmd[cmd.index("-p") + 1] == prompt
    assert cmd[cmd.index("--model") + 1] == model


# ---------- (1) plain complete(): the chat-role shape ----------


def test_complete_plain_chat_shape_includes_strict_mcp_config(monkeypatch):
    """A plain complete() call - no allowed_tools, no cwd, no system, no
    cell_dir (the chat-role shape) - must construct an argv containing
    --strict-mcp-config, so the subprocess sees zero MCP servers."""
    captured = _capture_complete_argv(monkeypatch)

    out = b.ClaudeCliDriver().complete("hi", model="sonnet")

    cmd = captured["cmd"]
    assert FLAG in cmd
    _assert_base_argv_intact(cmd, "hi", "sonnet")
    # Happy path unchanged: the text path still returns the subprocess stdout.
    assert out == "ok"


# ---------- (2) review-style shape: the flag is unconditional ----------


def test_complete_review_shape_includes_strict_mcp_config_unconditionally(
    monkeypatch, tmp_path,
):
    """The review-style shape (allowed_tools + cwd) must ALSO carry
    --strict-mcp-config: the flag is unconditional, not role-conditional. The
    pre-existing conditional flags must survive alongside it, and cwd must
    still be forwarded to subprocess.run."""
    captured = _capture_complete_argv(monkeypatch)

    b.ClaudeCliDriver().complete(
        "review the branch", system="reviewer body", model="sonnet",
        allowed_tools="Bash,Read", cwd=str(tmp_path),
    )

    cmd = captured["cmd"]
    assert FLAG in cmd
    _assert_base_argv_intact(cmd, "review the branch", "sonnet")
    assert cmd[cmd.index("--allowedTools") + 1] == "Bash,Read"
    assert cmd[cmd.index("--append-system-prompt") + 1] == "reviewer body"
    assert captured["cwd"] == str(tmp_path)


_COMPLETE_SHAPES = [
    pytest.param({}, id="chat-plain"),
    pytest.param({"bare": True}, id="chat-bare"),
    pytest.param({"system": "be careful"}, id="chat-system"),
    pytest.param({"max_tokens": 512}, id="max-tokens-accepted-ignored"),
    pytest.param({"allowed_tools": "Bash,Read", "cwd": "WT"}, id="review-tools-cwd"),
    pytest.param({"cell_dir": "CELL"}, id="planner-cell-dir-structured"),
]


@pytest.mark.parametrize("extra", _COMPLETE_SHAPES)
def test_strict_mcp_config_present_in_every_complete_shape(
    monkeypatch, tmp_path, extra,
):
    """Unconditionality, mechanically: every keyword shape complete() accepts
    must produce an argv containing --strict-mcp-config. No role may get an
    MCP-visible subprocess."""
    extra = dict(extra)
    if extra.get("cwd") == "WT":
        extra["cwd"] = str(tmp_path)
    if extra.get("cell_dir") == "CELL":
        extra["cell_dir"] = str(tmp_path)
    captured = _capture_complete_argv(
        monkeypatch, stdout=json.dumps(_claude_usage_payload())
    )

    b.ClaudeCliDriver().complete("hi", model="sonnet", **extra)

    assert FLAG in captured["cmd"]


# ---------- argv shape details ----------


def test_strict_mcp_config_appears_exactly_once_in_argv(monkeypatch):
    """A duplicated flag is at best noise and at worst a CLI parse error; the
    append must happen exactly once per complete() call, even when several
    conditional flags are also appended."""
    captured = _capture_complete_argv(monkeypatch)

    b.ClaudeCliDriver().complete("hi", model="sonnet", bare=True, system="s")

    assert captured["cmd"].count(FLAG) == 1


def test_strict_mcp_config_is_a_standalone_argv_element(monkeypatch):
    """The flag must be its own argv token - the exact string per
    `claude --help` - not fused into another token or passed as
    --strict-mcp-config=..."""
    captured = _capture_complete_argv(monkeypatch)

    b.ClaudeCliDriver().complete("hi", model="sonnet")

    assert FLAG in captured["cmd"]
    assert not any(
        token.startswith(FLAG + "=") for token in captured["cmd"]
    )


def test_strict_mcp_config_present_on_structured_cell_dir_path(
    monkeypatch, tmp_path,
):
    """The cell_dir path rewrites the argv (--output-format json for the usage
    sidecar) but must keep the flag; structured result extraction is
    unchanged."""
    captured = _capture_complete_argv(
        monkeypatch, stdout=json.dumps(_claude_usage_payload())
    )

    out = b.ClaudeCliDriver().complete("hi", model="sonnet", cell_dir=str(tmp_path))

    assert FLAG in captured["cmd"]
    assert "--output-format" in captured["cmd"]
    assert out == "the answer"


# ---------- boundary / negative cases ----------


def test_complete_empty_prompt_still_carries_flag(monkeypatch):
    """Boundary (zero-length prompt): the empty -p value must not skip the
    flag (and must not skip the -p plumbing either)."""
    captured = _capture_complete_argv(monkeypatch)

    b.ClaudeCliDriver().complete("", model="sonnet")

    cmd = captured["cmd"]
    assert FLAG in cmd
    assert cmd[cmd.index("-p") + 1] == ""


def test_complete_empty_allowed_tools_omits_allowedtools_but_keeps_flag(
    monkeypatch,
):
    """Boundary (empty collection): allowed_tools="" is falsy so
    --allowedTools stays omitted (pre-existing behavior) while the
    unconditional flag is still present."""
    captured = _capture_complete_argv(monkeypatch)

    b.ClaudeCliDriver().complete("hi", model="sonnet", allowed_tools="")

    assert "--allowedTools" not in captured["cmd"]
    assert FLAG in captured["cmd"]


def test_complete_explicit_cwd_none_still_carries_flag(monkeypatch):
    """cwd=None (explicitly, not just omitted) must not suppress the flag and
    must be forwarded to subprocess.run as None."""
    captured = _capture_complete_argv(monkeypatch)

    b.ClaudeCliDriver().complete("hi", model="sonnet", cwd=None)

    assert FLAG in captured["cmd"]
    assert captured["cwd"] is None


def test_complete_missing_required_model_kwarg_raises_typeerror(monkeypatch):
    """model is a required keyword-only parameter; a malformed call must raise
    TypeError naming the missing parameter BEFORE any subprocess is spawned."""
    captured = _capture_complete_argv(monkeypatch)

    with pytest.raises(TypeError) as excinfo:
        b.ClaudeCliDriver().complete("hi")

    assert "model" in str(excinfo.value)
    # The malformed call must never reach subprocess.run.
    assert "cmd" not in captured


# ---------- read-the-file guard: the flag lives in complete()'s argv build ----------


def test_flag_literal_is_in_complete_method_argv_construction():
    """The literal must be appended to the argv built at the top of
    ClaudeCliDriver.complete() (the `cmd = ["claude", "-p", ...]` line and the
    appends that follow it) - not only inside dispatch() or a role-specific
    branch outside complete()."""
    src = Path(bc.__file__).read_text()
    start = src.index("def complete(")
    ends = [
        i for i in (src.find("\n    def ", start), src.find("\nclass ", start))
        if i != -1
    ]
    end = min(ends) if ends else len(src)
    complete_body = src[start:end]

    assert '"--strict-mcp-config"' in complete_body
    # ...and it must sit in the argv-construction half of the body, before the
    # subprocess.run call that consumes cmd.
    run_at = complete_body.index("subprocess.run(")
    assert complete_body.index('"--strict-mcp-config"') < run_at