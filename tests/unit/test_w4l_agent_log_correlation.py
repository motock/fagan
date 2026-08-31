"""TDD spec: agent-side correlation-id propagation into the agent.log writers.

GOAL (story brief): the dispatched agent's own output must be joinable to
orchestrator events by the correlation ID minted by the previous story in the
chain.  The orchestrator exports ``PIPELINE_CORRELATION_ID``; the agent's
structured ``[step N] ...`` lines (the stdout stream Popen redirects into the
worktree's ``agent.log``) must carry it.

REQUIRED IMPLEMENTATION CONTRACT (additive only; no renames, no moves):

In ``scripts/local_agent.py`` (a ``local_agent_common.py`` sibling module that
``local_agent`` re-exports from is also accepted):

1. ``read_correlation_id() -> str``
       return os.environ.get("PIPELINE_CORRELATION_ID", "")
   -- reads the env var; empty/missing both yield "".

2. ``emit_step_line(step: int, message: str, correlation_id: str = "") -> str``
   -- the extracted writer for the ``[step N] ...`` structured lines (the
   lines that today are printed inline as ``print(f"[step {step}] ...",
   flush=True)"`` all over the agent loop, notably the ``"] DONE:"`` emitter).
   It prints the line to stdout with ``flush=True`` and returns it.  When
   ``correlation_id`` is non-empty the line gains a trailing ``[cid=<id>]``
   suffix; when it is "" the line is byte-identical to today's output.

3. ``correlation_id`` is an optional parameter defaulting to "" so every
   existing call site and test keeps working (backward compat).

The log lines are plain text, so per the brief the id is emitted as the
bracketed suffix ``[cid=<id>]`` on the structured lines (there is no
JSON-records writer in this family to prefer over them, and no new log file
may be invented).
"""

import importlib.util
import inspect
import itertools
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
_LOCAL_AGENT_PY = _SCRIPTS / "local_agent.py"
_COMMON_PY = _SCRIPTS / "local_agent_common.py"

_counter = itertools.count()


def _exec_module(path: Path, modname: str):
    spec = importlib.util.spec_from_file_location(modname, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


def _writer(monkeypatch, env=None):
    """Load the agent module fresh under a private name with `env` applied.

    Mirrors tests/unit/test_local_agent_persistence.py: scripts/local_agent.py
    reads env at import time, so correlation tests must pin the environment
    before the module executes.  Returns SimpleNamespace(read=..., emit=...)
    where read is read_correlation_id() and emit is emit_step_line().
    """
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "test-model")
    for key, value in (env or {}).items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    modname = f"_local_agent_cid_test_{next(_counter)}"
    module = _exec_module(_LOCAL_AGENT_PY, modname)

    read = getattr(module, "read_correlation_id", None)
    emit = getattr(module, "emit_step_line", None)
    # The brief prefers a common writer module; accept the helper living in
    # scripts/local_agent_common.py as long as it exists there.
    if (read is None or emit is None) and _COMMON_PY.exists():
        common = _exec_module(
            _COMMON_PY, f"_local_agent_common_cid_test_{next(_counter)}"
        )
        read = read or getattr(common, "read_correlation_id", None)
        emit = emit or getattr(common, "emit_step_line", None)
    if read is None or emit is None:
        pytest.fail(
            "scripts/local_agent.py (or scripts/local_agent_common.py) must "
            "define read_correlation_id() and "
            "emit_step_line(step, message, correlation_id='') -- the extracted "
            "writer for the [step N] agent.log lines. See the module docstring "
            "of this test file for the exact contract."
        )
    return SimpleNamespace(read=read, emit=emit, module=module)


# ---------------------------------------------------------------------------
# Requirement 1: env set -> structured lines carry [cid=<id>]
# ---------------------------------------------------------------------------


def test_env_set_appends_cid_suffix_to_structured_line(monkeypatch, capsys):
    w = _writer(monkeypatch, {"PIPELINE_CORRELATION_ID": "cid-abc-123"})
    assert w.read() == "cid-abc-123"

    line = w.emit(7, "DONE: shipped the thing", correlation_id=w.read())

    assert line == "[step 7] DONE: shipped the thing [cid=cid-abc-123]"
    # The writer emits to stdout (the stream Popen redirects into agent.log).
    captured = capsys.readouterr()
    assert captured.out == "[step 7] DONE: shipped the thing [cid=cid-abc-123]\n"


def test_done_line_specifically_carries_cid(monkeypatch, capsys):
    # The join point the story exists for: the "] DONE:" event line.
    w = _writer(monkeypatch, {"PIPELINE_CORRELATION_ID": "orch-evt-42"})
    line = w.emit(12, "DONE: all checks green", correlation_id=w.read())
    assert line.endswith("[cid=orch-evt-42]")
    assert "[step 12] DONE: all checks green [cid=orch-evt-42]" == line
    assert "orch-evt-42" in capsys.readouterr().out


def test_cid_emitted_verbatim_for_unusual_but_plausible_ids(monkeypatch):
    w = _writer(monkeypatch)
    for cid in ("a", "run:abc-123_01", "id with spaces"):
        line = w.emit(1, "DONE: x", correlation_id=cid)
        assert line == f"[step 1] DONE: x [cid={cid}]"


def test_cid_suffix_on_boundary_step_numbers(monkeypatch):
    w = _writer(monkeypatch, {"PIPELINE_CORRELATION_ID": "c"})
    assert w.emit(0, "boot", correlation_id="c") == "[step 0] boot [cid=c]"
    assert w.emit(999, "boot", correlation_id="c") == "[step 999] boot [cid=c]"


def test_empty_message_still_carries_cid(monkeypatch):
    w = _writer(monkeypatch)
    line = w.emit(4, "", correlation_id="c")
    assert line.startswith("[step 4]")
    assert "[cid=c]" in line


# ---------------------------------------------------------------------------
# Requirement 2: env unset -> NO cid suffix anywhere (backward compat)
# ---------------------------------------------------------------------------


def test_env_unset_produces_no_cid_suffix(monkeypatch, capsys):
    monkeypatch.delenv("PIPELINE_CORRELATION_ID", raising=False)
    w = _writer(monkeypatch, {})
    assert w.read() == ""

    line = w.emit(0, "DONE: summary", correlation_id=w.read())

    assert line == "[step 0] DONE: summary"
    assert "[cid=" not in line
    assert "[cid=" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Requirement 3: env set to empty string is treated as unset
# ---------------------------------------------------------------------------


def test_env_empty_string_treated_as_unset(monkeypatch, capsys):
    w = _writer(monkeypatch, {"PIPELINE_CORRELATION_ID": ""})
    assert w.read() == ""

    line = w.emit(2, "DONE: x", correlation_id=w.read())

    assert line == "[step 2] DONE: x"
    assert "[cid=" not in line
    assert "[cid=" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Requirement 4: optional-parameter signature keeps existing callers working
# ---------------------------------------------------------------------------


def test_signature_correlation_id_is_optional_with_empty_default(monkeypatch):
    w = _writer(monkeypatch)
    params = inspect.signature(w.emit).parameters
    assert "correlation_id" in params, (
        "emit_step_line must accept a correlation_id parameter"
    )
    param = params["correlation_id"]
    assert param.default == "", "correlation_id must default to ''"
    assert param.kind is not inspect.Parameter.POSITIONAL_ONLY


def test_omitting_correlation_id_kwarg_stays_suffix_free_even_when_env_set(
    monkeypatch, capsys
):
    # Default is "" (not "read the env"): existing call sites that do not pass
    # the kwarg must keep producing today's exact output.
    w = _writer(monkeypatch, {"PIPELINE_CORRELATION_ID": "cid-xyz"})
    line = w.emit(3, "bash: pwd")
    assert line == "[step 3] bash: pwd"
    assert "[cid=" not in line
    assert "[cid=" not in capsys.readouterr().out


def test_correlation_id_accepted_positionally_and_by_keyword(monkeypatch):
    w = _writer(monkeypatch)
    assert w.emit(5, "m", "kw-pos") == "[step 5] m [cid=kw-pos]"
    assert w.emit(5, "m", correlation_id="kw-named") == "[step 5] m [cid=kw-named]"


# ---------------------------------------------------------------------------
# No renames / no removals in the local_agent family (additive change only)
# ---------------------------------------------------------------------------


def test_existing_family_functions_survive_untouched(monkeypatch):
    w = _writer(monkeypatch, {"PIPELINE_CORRELATION_ID": "c"})
    module = w.module
    # Pre-existing names in scripts/local_agent.py must still be importable
    # under their original names (no move/rename allowed by the brief).
    assert callable(getattr(module, "recover_from_oversized_5xx", None))
    assert callable(getattr(module, "_reject_done_for_suite", None))


def test_no_new_log_file_is_invented(tmp_path, monkeypatch, capsys):
    # "Do NOT invent a new log file; extend the existing writers only": the
    # writer goes to stdout (redirected to the existing agent.log), and must
    # not create any file of its own.
    w = _writer(monkeypatch, {"PIPELINE_CORRELATION_ID": "c"})
    before = {p.name for p in tmp_path.iterdir()}
    w.emit(1, "DONE: x", correlation_id="c")
    capsys.readouterr()
    assert {p.name for p in tmp_path.iterdir()} == before


# ---------------------------------------------------------------------------
# Requirement 5 (review-directed): the REAL DONE call site in main()'s loop
# ---------------------------------------------------------------------------
#
# The 12 tests above all call emit_step_line()/read_correlation_id() directly
# with the correlation_id kwarg supplied by the test itself, so none of them
# would notice if the DONE emitter at scripts/local_agent.py:685-689 stopped
# passing correlation_id=read_correlation_id().  These two tests drive the real
# agent loop (la.main()) through the established harness from
# tests/unit/test_local_agent_persistence.py:311 -- module loaded with pinned
# env, chat monkeypatched to return a `done` tool call, worktree_dirty and
# exclude_runtime_artifacts mocked -- and assert on the stdout line the loop
# actually emits.  emit_step_line's default is the literal "" with NO env
# fallback, so a kwarg-dropping regression prints a bare DONE line even with
# PIPELINE_CORRELATION_ID staged; only a main()-level test can see that.


def _drive_main_to_done(monkeypatch, tmp_path, capsys):
    """Load scripts/local_agent.py fresh (pinned env) and drive main() to the
    DONE emit, mirroring test_local_agent_persistence.py:311.  Returns
    (module, stdout_text).  The caller must have set/cleared
    PIPELINE_CORRELATION_ID BEFORE calling this: read_correlation_id() reads
    the env at call time, but the module also snapshots env at import."""
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "test-model")
    monkeypatch.setenv("LOCAL_AGENT_TASK", "ship the thing")
    monkeypatch.setenv("PIPELINE_TRANSPORT_MAX_STEPS", "1")
    monkeypatch.delenv("LOCAL_AGENT_RESUME_TRANSCRIPT_PATH", raising=False)
    la = _exec_module(_LOCAL_AGENT_PY, f"_local_agent_main_cid_{next(_counter)}")
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(la, "MAX_STEPS", 1)

    def _fake_chat(messages):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done",
                                             "arguments": {"summary": "all checks green"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    monkeypatch.setattr(la, "exclude_runtime_artifacts", lambda: None)

    rc = la.main()
    assert rc == 0, "fake agent called done on a clean tree; main() must return 0"
    return la, capsys.readouterr().out


def test_main_done_emitter_passes_read_correlation_id(monkeypatch, tmp_path, capsys):
    """The story's actual deliverable: the DONE call site at
    scripts/local_agent.py:685-689 passes correlation_id=read_correlation_id().
    With PIPELINE_CORRELATION_ID staged (12-hex, the shape pipeline/dispatch.py
    mints), the DONE stdout line -- the one Popen redirects into agent.log --
    must end with [cid=<id>].  A kwarg-dropping regression prints a bare line
    here because emit_step_line's default is literal "" with no env fallback;
    the 12 helper-level tests above cannot catch that."""
    cid = "9c41f7a2b8de"
    monkeypatch.setenv("PIPELINE_CORRELATION_ID", cid)
    _la, out = _drive_main_to_done(monkeypatch, tmp_path, capsys)

    done_lines = [ln for ln in out.splitlines() if "DONE:" in ln]
    assert done_lines, f"main() must emit a DONE line; stdout was: {out!r}"
    done_line = done_lines[-1]
    assert done_line.endswith("[cid=9c41f7a2b8de]"), (
        "the DONE emitter must pass correlation_id=read_correlation_id() so the "
        f"agent.log DONE record joins to the orchestrator; got: {done_line!r}"
    )
    # The orchestrator stages the var around the synchronous launch and only
    # RESTORES it afterwards: the agent reads it, it never pops it.
    assert os.environ.get("PIPELINE_CORRELATION_ID") == cid


def test_main_done_emitter_reads_env_at_call_time_not_cached(monkeypatch, tmp_path, capsys):
    """Call-time read, two calls, real state carried across: call 1 with the
    var staged (production shape), call 2 with it unset -- the exact state
    pipeline/dispatch.py's restore leaves behind.  If the implementation cached
    the id at import time, or popped the env var during call 1, call 2 (or the
    post-call-1 state check) exposes it."""
    monkeypatch.setenv("PIPELINE_CORRELATION_ID", "9c41f7a2b8de")
    _la1, out1 = _drive_main_to_done(monkeypatch, tmp_path, capsys)
    assert out1.splitlines()[-1].endswith("[cid=9c41f7a2b8de]")
    assert os.environ.get("PIPELINE_CORRELATION_ID") == "9c41f7a2b8de", (
        "call 1 must leave the orchestrator's staged value in place: the agent "
        "reads PIPELINE_CORRELATION_ID, it never pops it"
    )

    monkeypatch.delenv("PIPELINE_CORRELATION_ID", raising=False)
    _la2, out2 = _drive_main_to_done(monkeypatch, tmp_path, capsys)
    done_lines = [ln for ln in out2.splitlines() if "DONE:" in ln]
    assert done_lines, f"main() must emit a DONE line; stdout was: {out2!r}"
    assert "[cid=" not in done_lines[-1], (
        "with the env unset (the orchestrator's post-restore state) the DONE "
        f"line must be suffix-free; got: {done_lines[-1]!r}"
    )
