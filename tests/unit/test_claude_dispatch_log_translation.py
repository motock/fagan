"""Tests for the LOG-05 dispatch wiring: ``ClaudeCliDriver.dispatch`` must pass
a translating ``line_filter`` into ``execution.spawn_harness`` so agent.log
gets canonical ``[step N] <tool>:`` / ``"] DONE:"`` lines while the CLI's raw
``stream-json`` output is preserved verbatim in a ``<log_path>.raw`` sidecar.

The translator itself is already graded by tests/unit/test_claude_log_translate*.py;
what is graded HERE is the wiring inside ``dispatch``:

- a non-None ``line_filter`` kwarg reaches ``spawn_harness`` (never a real
  ``claude`` process: ``spawn_harness`` is monkeypatched and the captured
  filter is driven directly with synthetic NDJSON lines);
- the filter delegates to ``pipeline.claude_log_translate.translate_line``
  with a state minted by ``new_state()`` PER DISPATCH CALL (two dispatches
  must never share a step counter);
- every raw line is appended verbatim to ``<log_path>.raw`` in the same
  append/truncate mode as the ``append`` flag, best-effort: an OSError on the
  sidecar write is swallowed and must never break the filter (mirroring
  ``record_token_usage``'s try/except OSError pattern);
- the ``claude`` argv is untouched (``--output-format stream-json --verbose``
  stays exactly as pinned by test_backend_claude_driver_misc.py) and the
  ``aider`` branch of ``dispatch`` stays filter-free;
- ``"*.log.raw"`` is a member of BOTH ignore lists (pipeline/paths.py's
  ``_WORKTREE_LOG_EXCLUDES`` and pipeline/local_agent_common.py's additions
  tuple) so the sidecar is never swept into a commit or a worktree diff --
  asserted as MEMBERSHIP, never equality, because both lists are cumulative
  and later stories extend them.

The pre-existing suites test_backend_claude_driver_misc.py and
test_claude_dispatch_no_wakeup.py must keep passing UNMODIFIED.
"""
from __future__ import annotations

import builtins
import fnmatch
import inspect
import json
import re
from types import SimpleNamespace

from app import backend as b
from app import backend_claude as bc
from pipeline import execution, local_agent_common, paths

# The consumer regexes the canonical lines must satisfy (see
# pipeline/rebrief.py _log_facts and pipeline/build_detect.py).
_STEP_LINE_RE = re.compile(r"^\[step (\d+)\] ([a-z_]+): ")
_DONE_MARKER = "] DONE:"


# --------------------------------------------------------------- helpers


def _assistant_tool_use_line(
    command="pytest -q tests/unit/test_parsers.py", name="Bash"
):
    """One synthetic stream-json ``assistant`` event carrying a tool_use."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": name,
                        "input": {"command": command},
                    }
                ]
            },
        }
    )


def _result_line(summary="Tightened the parser grammar and left the suite green"):
    """One synthetic stream-json ``result`` event (a successful turn)."""
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": summary}
    )


def _capture_spawn(monkeypatch, captured):
    """Replace ``execution.spawn_harness`` with a recorder.

    No real ``claude`` (or aider) process is ever spawned; the captured
    ``line_filter`` is what the tests drive directly.
    """

    def fake_spawn(cmd, cwd=None, log_path=None, append=False, env=None, **kwargs):
        captured["cmd"] = list(cmd)
        captured["log_path"] = log_path
        captured["append"] = append
        captured["kwargs"] = dict(kwargs)
        captured.setdefault("filters", []).append(kwargs.get("line_filter"))
        return SimpleNamespace(pid=4242, model=None)

    monkeypatch.setattr(execution, "spawn_harness", fake_spawn)


def _dispatch(tmp_path, monkeypatch, captured, *, append=False, log_name="agent.log"):
    """Run ClaudeCliDriver.dispatch on the native-claude path; return log_path."""
    monkeypatch.delenv("PIPELINE_AGENT_HARNESS", raising=False)
    _capture_spawn(monkeypatch, captured)
    log_path = tmp_path / log_name
    b.ClaudeCliDriver().dispatch(
        "implement the story",
        system="be careful",
        model="sonnet",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path,
        log_path=log_path,
        append=append,
    )
    return log_path


def _captured_filter(captured):
    flt = captured["filters"][0]
    assert flt is not None, (
        "dispatch must pass a non-None line_filter to execution.spawn_harness"
    )
    return flt


def _step_of(line):
    m = _STEP_LINE_RE.match(line)
    assert m, f"not a canonical step line: {line!r}"
    return int(m.group(1))


# --------------------------------------------------------------- positive


def test_dispatch_passes_non_none_line_filter_to_spawn_harness(tmp_path, monkeypatch):
    captured = {}
    log_path = _dispatch(tmp_path, monkeypatch, captured)
    flt = captured["kwargs"].get("line_filter")
    assert flt is not None, "dispatch must wire a translating line_filter"
    assert callable(flt)
    # The claude argv pin must survive the wiring untouched.
    cmd = captured["cmd"]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd
    # The spawn args are forwarded unchanged.
    assert captured["log_path"] == log_path
    assert captured["append"] is False


def test_filter_translates_assistant_tool_use_into_step_line(tmp_path, monkeypatch):
    captured = {}
    _dispatch(tmp_path, monkeypatch, captured)
    out = _captured_filter(captured)(_assistant_tool_use_line())
    assert isinstance(out, list), type(out)
    assert len(out) == 1, out
    assert _STEP_LINE_RE.match(out[0]), out


def test_filter_translates_result_into_done_line(tmp_path, monkeypatch):
    captured = {}
    _dispatch(tmp_path, monkeypatch, captured)
    out = _captured_filter(captured)(_result_line())
    assert isinstance(out, list), type(out)
    assert len(out) == 1, out
    assert _DONE_MARKER in out[0], out


def test_filter_appends_raw_line_verbatim_to_sidecar(tmp_path, monkeypatch):
    captured = {}
    log_path = _dispatch(tmp_path, monkeypatch, captured)
    flt = _captured_filter(captured)
    raw = _assistant_tool_use_line()
    flt(raw)
    sidecar = log_path.with_name(log_path.name + ".raw")
    assert sidecar.exists(), f"missing raw sidecar {sidecar}"
    content = sidecar.read_text()
    assert content.strip() == raw.strip(), content
    # A second driven line is appended, not truncated over the first.
    raw2 = _result_line()
    flt(raw2)
    content = sidecar.read_text()
    assert raw in content and raw2 in content, content


def test_filter_step_numbers_increment_across_calls_same_filter(tmp_path, monkeypatch):
    captured = {}
    _dispatch(tmp_path, monkeypatch, captured)
    flt = _captured_filter(captured)
    first = flt(_assistant_tool_use_line(command="pytest -q"))
    second = flt(_assistant_tool_use_line(command="git diff --stat"))
    s1 = _step_of(first[0])
    s2 = _step_of(second[0])
    assert s1 == 0, first
    assert s2 == s1 + 1, (first, second)


def test_separate_dispatches_have_independent_step_counters(tmp_path, monkeypatch):
    captured = {}
    _dispatch(tmp_path, monkeypatch, captured, log_name="agent.log")
    _dispatch(tmp_path, monkeypatch, captured, log_name="agent2.log")
    f1, f2 = captured["filters"]
    assert f1 is not None and f2 is not None
    out1 = f1(_assistant_tool_use_line())
    out2 = f2(_assistant_tool_use_line())
    # Both counters start at 0: the state is per-dispatch-call, never
    # module-level (a shared counter would make the second line [step 1]).
    assert _step_of(out1[0]) == 0, out1
    assert _step_of(out2[0]) == 0, out2


def test_sidecar_open_mode_matches_append_flag(tmp_path, monkeypatch):
    captured = {}
    _dispatch(tmp_path, monkeypatch, captured, append=True)
    flt = _captured_filter(captured)
    real_open = builtins.open
    modes = []

    def recording_open(file, mode="r", *args, **kwargs):
        if str(file).endswith(".raw"):
            modes.append(mode)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    flt(_assistant_tool_use_line())
    assert modes, "the filter never opened the .raw sidecar"
    assert modes[0].startswith("a"), modes

    # A fresh dispatch with append=False must TRUNCATE the sidecar instead.
    captured2 = {}
    _dispatch(tmp_path, monkeypatch, captured2, append=False, log_name="agent2.log")
    modes.clear()
    _captured_filter(captured2)(_assistant_tool_use_line())
    assert modes, "the filter never opened the .raw sidecar"
    assert modes[0].startswith("w"), modes


# ------------------------------------------------------- negative / boundary


def test_non_json_line_passes_through_unchanged(tmp_path, monkeypatch):
    captured = {}
    _dispatch(tmp_path, monkeypatch, captured)
    warning = "claude-cli: warning: rate limit approaching, retrying"
    out = _captured_filter(captured)(warning)
    assert out == [warning], out


def test_sidecar_oserror_is_swallowed_and_translation_survives(tmp_path, monkeypatch):
    captured = {}
    _dispatch(tmp_path, monkeypatch, captured)
    flt = _captured_filter(captured)
    real_open = builtins.open

    def exploding_open(file, mode="r", *args, **kwargs):
        if str(file).endswith(".raw"):
            raise OSError("sidecar unwritable")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", exploding_open)
    out = flt(_assistant_tool_use_line())  # must not raise
    assert isinstance(out, list) and len(out) == 1, out
    assert _STEP_LINE_RE.match(out[0]), out
    out2 = flt(_result_line())  # still no raise on the next line either
    assert _DONE_MARKER in out2[0], out2


def test_sidecar_unwritable_directory_still_translates(tmp_path, monkeypatch):
    """Mechanism-independent twin of the OSError test: a DIRECTORY sitting at
    the sidecar path makes every open() raise OSError, however the filter
    opens the file."""
    captured = {}
    log_path = _dispatch(tmp_path, monkeypatch, captured)
    log_path.with_name(log_path.name + ".raw").mkdir()
    out = _captured_filter(captured)(_assistant_tool_use_line())  # must not raise
    assert isinstance(out, list) and len(out) == 1, out
    assert _STEP_LINE_RE.match(out[0]), out


def test_filter_never_raises_on_malformed_input(tmp_path, monkeypatch):
    captured = {}
    _dispatch(tmp_path, monkeypatch, captured)
    flt = _captured_filter(captured)
    for raw in ("{", "", "null"):
        out = flt(raw)  # must not raise
        assert isinstance(out, list), (raw, type(out))
    assert flt("{") == ["{"], "non-JSON must pass through verbatim"


# --------------------------------------------------------- ignore-list pins


def test_worktree_log_excludes_include_raw_sidecar_glob():
    # Membership only: _WORKTREE_LOG_EXCLUDES is cumulative across stories.
    assert "*.log.raw" in paths._WORKTREE_LOG_EXCLUDES
    assert "*.log.ts" in paths._WORKTREE_LOG_EXCLUDES  # the mirrored anchor
    # The entry must be the GLOB, so every log's sidecar is covered.
    for name in ("agent.log.raw", "review.log.raw", "test_author.log.raw"):
        assert fnmatch.fnmatch(name, "*.log.raw"), name


def test_local_agent_common_additions_include_raw_sidecar_glob():
    src = inspect.getsource(local_agent_common)
    m = re.search(
        r"\(\s*\"agent\.log\",[^)]*\"\.agent_transcript\.json\"[^)]*\)",
        src,
        re.DOTALL,
    )
    assert m, "local_agent_common's git-exclude additions tuple not found"
    # Membership only: the tuple is cumulative across stories.
    assert '"*.log.raw"' in m.group(0), m.group(0)
    assert '"__pycache__/"' in m.group(0), m.group(0)  # matched the right tuple


# ----------------------------------------------------------- wiring pins


def test_dispatch_mints_a_fresh_translator_state_per_call():
    dispatch_src = inspect.getsource(b.ClaudeCliDriver.dispatch)
    assert "new_state()" in dispatch_src, (
        "dispatch must create the translate state per call, not module-level"
    )
    module_src = inspect.getsource(bc)
    assert "claude_log_translate" in module_src, (
        "backend_claude must import pipeline.claude_log_translate"
    )


def test_aider_branch_spawn_passes_no_line_filter(tmp_path, monkeypatch):
    """The aider branch of dispatch is out of scope for this story and must
    keep spawning exactly as before (no translating filter)."""
    monkeypatch.setenv("PIPELINE_AGENT_HARNESS", "aider")
    monkeypatch.setattr(bc, "aider_binary_available", lambda: (True, "ok"))
    captured = {}

    def fake_spawn(cmd, cwd=None, log_path=None, append=False, env=None, **kwargs):
        captured["kwargs"] = dict(kwargs)
        return SimpleNamespace(pid=1, model=None)

    monkeypatch.setattr(execution, "spawn_harness", fake_spawn)
    b.ClaudeCliDriver().dispatch(
        "implement the story",
        system=None,
        model="sonnet",
        allowed_tools="Bash",
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
        append=False,
    )
    assert captured["kwargs"].get("line_filter") is None