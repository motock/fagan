"""Tests for the gpt-oss steering/harness hardening (scripts/local_agent.py).

Background: a 2026-07-20 gpt-oss:20b run on the always-on-checklist plan spent
52% of its 60-step budget re-reading one file (31 view_file calls) and never
ran tests, then died on a str_replace no-match loop against server.py. Root
cause was not a model capability wall but harness steering that pointed the
model INTO the failure modes:

  - the per-target repetition nudge said "Use view_file to read ... and
    re-read", i.e. it told the model to repeat the exact read that tripped
    the guard. With PARK_ENABLED=0 (the scheduler plist) the guard nudges
    but never parks, so the model re-read until step cap.
  - the str_replace "old_str not found" error gave no diagnostic, so the
    model guessed at whitespace and retried blind.
  - failing str_replace is excluded from every guard, so a no-match loop
    ran uncaught.
  - the create_file description pushed whole-file rewrites (catastrophic
    for a 2735-line server.py).

These tests pin the fixes. Imported as a module like test_local_agent.py.
"""
import importlib.util
import os
from pathlib import Path

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)


# --- helpers ---------------------------------------------------------------

def _init_git_repo(path):
    import subprocess
    subprocess.run(["git", "init"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   check=False, cwd=path, capture_output=True, text=True)


def _sequence_chat(responses):
    calls = []

    def _fake(messages):
        calls.append(messages)
        idx = len(calls) - 1
        if idx >= len(responses):
            fn, args = "done", {"summary": "out of scripted responses"}
        else:
            fn, args = responses[idx]
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": fn, "arguments": args}}]}

    return _fake, calls


# --- A. HARNESS_RULES: read-once + early-pytest guidance -------------------

def test_harness_rules_direct_read_once_not_per_edit():
    """The old rule 'Use view_file to read a file's real contents before
    editing it' was applied by gpt-oss literally per-edit (31 re-reads). The
    rule must now say to read each file ONCE and not re-read paths already
    read."""
    rules = la.HARNESS_RULES
    assert "ONCE" in rules or "once" in rules, (
        f"HARNESS_RULES must direct reading each file once; got: {rules!r}")
    # The counterproductive per-edit phrasing must be gone.
    assert "before editing it" not in rules, (
        f"HARNESS_RULES must drop the per-edit read phrasing; got: {rules!r}")


def test_harness_rules_direct_early_pytest():
    """The model ran zero pytest calls in 60 steps. HARNESS_RULES must direct
    running tests after the first edit, not at the end."""
    rules = la.HARNESS_RULES
    assert "pytest" in rules, (
        f"HARNESS_RULES must mention pytest; got: {rules!r}")
    assert "first edit" in rules or "after your first edit" in rules, (
        f"HARNESS_RULES must direct early test runs; got: {rules!r}")


# --- B. create_file description: prefer str_replace for large files --------

def test_create_file_description_no_longer_pushes_whole_file_rewrite():
    """The old description said 'a whole-file rewrite is preferred over
    str_replace for the file you are implementing' — catastrophic for a
    2735-line server.py (Mode 22: full-rewrite drops content). That phrasing
    must be gone, replaced by a size-bounded preference for str_replace."""
    desc = next(t["function"]["description"]
                for t in la.TOOLS if t["function"]["name"] == "create_file")
    assert "whole-file rewrite is preferred" not in desc, (
        f"create_file description must drop the whole-file-rewrite push; got: {desc!r}")
    assert "str_replace" in desc, (
        f"create_file description must steer to str_replace; got: {desc!r}")
    # A size bound must keep create_file off large existing files.
    assert "200" in desc or "100" in desc, (
        f"create_file description must carry a size bound; got: {desc!r}")


# --- C. Per-target repetition nudge steers AWAY from reading ---------------

def test_repetition_nudge_text_stops_reading_not_re_reads():
    """The nudge fired when a read-only action repeats 3x. The old text said
    'Use view_file to read the actual current file contents and re-read the
    error's file:line' — steering the model back into the read loop. It must
    instead tell the model to STOP reading and either edit or run tests, and
    must not contain 're-read'."""
    msg = la._repetition_nudge()
    assert "STOP" in msg or "stop" in msg, (
        f"repetition nudge must tell the model to stop; got: {msg!r}")
    assert "re-read" not in msg, (
        f"repetition nudge must not say 're-read'; got: {msg!r}")
    # It must point to a concrete non-reading action.
    assert "str_replace" in msg or "pytest" in msg or "create_file" in msg, (
        f"repetition nudge must steer to a concrete action; got: {msg!r}")


def test_repetition_nudge_fires_in_main_loop_and_does_not_say_re_read(
        tmp_path, monkeypatch, capsys):
    """Integration: 3 same-path view_file calls trip the per-target guard.
    The injected user nudge must be the new stop-reading text (no 're-read')."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    (tmp_path / "x.py").write_text("x = 1\n")
    responses = [("view_file", {"path": "x.py"}) for _ in range(6)]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    # Capture the nudge message appended to the transcript. Patch _repetition_nudge
    # to record it was called with the new text.
    captured = {}
    real = la._repetition_nudge

    def _spy():
        s = real()
        captured["msg"] = s
        return s

    monkeypatch.setattr(la, "_repetition_nudge", _spy)
    la.main()
    out = capsys.readouterr().out
    assert "[repetition nudge]" in out, (
        f"per-target guard must still fire; output: {out!r}")
    assert "re-read" not in captured.get("msg", ""), (
        f"injected nudge must not say re-read; got: {captured.get('msg')!r}")


# --- D. str_replace not-found error gives a whitespace diagnostic ----------

def test_str_replace_not_found_error_keeps_prefix_and_adds_diagnostic(
        tmp_path, monkeypatch):
    """The error must still start with 'ERROR: old_str not found' (so the
    success-gating that keys on the 'ERROR' prefix is unchanged) but must now
    include a diagnostic: a nearby line with line number and a whitespace
    marker, plus steering to replace_lines."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text(
        "def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    # old_str with wrong indentation (tabs vs spaces style mismatch).
    res = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "\treturn 1\n",   # tab-indented; file uses spaces
        "new_str": "    return 99\n",
    })
    assert res.startswith("ERROR: old_str not found"), (
        f"must keep the 'ERROR: old_str not found' prefix; got: {res!r}")
    # A line number from the file must appear so the model can target it.
    assert "2|" in res or "2 |" in res or "line" in res.lower(), (
        f"diagnostic must include a line number; got: {res!r}")
    # Steering toward the line-range tool so the model can sidestep the
    # byte-exact-match requirement.
    assert "replace_lines" in res, (
        f"diagnostic must steer to replace_lines; got: {res!r}")


def test_str_replace_not_found_diagnostic_shows_whitespace_marker(
        tmp_path, monkeypatch):
    """When the nearby line has leading whitespace, the diagnostic must make
    that whitespace visible (so the model can see why its old_str did not
    match) using a middle-dot / arrow marker."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def foo():\n    return 1\n")
    res = la.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "NOPE_NOT_HERE_AT_ALL",
        "new_str": "x",
    })
    assert res.startswith("ERROR: old_str not found"), res
    # The indented line '    return 1' must be shown with a whitespace marker.
    assert "·" in res or "→" in res, (
        f"diagnostic must mark whitespace; got: {res!r}")


# --- E. Failing-str_replace 2-strike nudge ---------------------------------

def test_failed_str_replace_loop_nudges_after_two_same_path_failures(
        tmp_path, monkeypatch, capsys):
    """str_replace is excluded from the per-target repetition guard, so a
        no-match loop on one file runs uncaught (the server.py wall). After 2
    consecutive FAILED str_replace calls on the same path, the loop must
    inject a steering nudge and log a distinct marker."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "MAX_STEPS", 20)
    (tmp_path / "srv.py").write_text("def dispatch():\n    pass\n")
    responses = [
        ("str_replace", {"path": "srv.py", "old_str": "NOPE1", "new_str": "x"}),
        ("str_replace", {"path": "srv.py", "old_str": "NOPE2", "new_str": "x"}),
        ("str_replace", {"path": "srv.py", "old_str": "NOPE3", "new_str": "x"}),
        ("done", {"summary": "done"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    out = capsys.readouterr().out
    assert "str_replace-fail" in out, (
        f"a 2nd failed str_replace on the same path must log a marker; "
        f"output: {out!r}")


def test_failed_str_replace_counter_resets_on_success(tmp_path, monkeypatch):
    """A successful str_replace to the same path must reset the failure
    counter, so a model that fails twice, succeeds, then fails twice again is
    not falsely nudged on the 3rd total failure."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "MAX_STEPS", 20)
    (tmp_path / "srv.py").write_text("TOKEN\n")
    responses = [
        ("str_replace", {"path": "srv.py", "old_str": "NOPE1", "new_str": "x"}),  # fail
        ("str_replace", {"path": "srv.py", "old_str": "NOPE2", "new_str": "x"}),  # fail -> nudge
        ("str_replace", {"path": "srv.py", "old_str": "TOKEN", "new_str": "OK"}),  # success -> reset
        ("str_replace", {"path": "srv.py", "old_str": "NOPE3", "new_str": "x"}),  # fail (counter=1)
        ("str_replace", {"path": "srv.py", "old_str": "NOPE4", "new_str": "x"}),  # fail (counter=2 -> nudge)
        ("done", {"summary": "done"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)
    la.main()  # must not raise


# --- F-K. replace_lines tool -----------------------------------------------

def test_replace_lines_in_tools_with_schema():
    """replace_lines must be declared in TOOLS with path/start/end/new_str."""
    names = {t["function"]["name"] for t in la.TOOLS}
    assert "replace_lines" in names, f"replace_lines missing from TOOLS: {names}"
    rl = next(t["function"] for t in la.TOOLS if t["function"]["name"] == "replace_lines")
    required = set(rl["parameters"]["required"])
    assert {"path", "start", "end", "new_str"} <= required, (
        f"replace_lines required params wrong: {required}")


def test_replace_lines_replaces_inclusive_range(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("a\nb\nc\nd\ne\n")
    res = la.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 4,
        "new_str": "B\nC\nD\n",
    })
    assert res.startswith("edited"), res
    assert (tmp_path / "mod.py").read_text() == "a\nB\nC\nD\ne\n"


def test_replace_lines_rejects_start_beyond_file(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("a\nb\n")
    res = la.run_tool("replace_lines", {
        "path": "mod.py", "start": 10, "end": 12, "new_str": "x\n"})
    assert res.startswith("ERROR"), res


def test_replace_lines_rejects_start_less_than_one(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("a\nb\n")
    res = la.run_tool("replace_lines", {
        "path": "mod.py", "start": 0, "end": 1, "new_str": "x\n"})
    assert res.startswith("ERROR"), res


def test_replace_lines_rejects_end_less_than_start(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("a\nb\nc\n")
    res = la.run_tool("replace_lines", {
        "path": "mod.py", "start": 3, "end": 1, "new_str": "x\n"})
    assert res.startswith("ERROR"), res


def test_replace_lines_rejects_nonexistent_file(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    res = la.run_tool("replace_lines", {
        "path": "nope.py", "start": 1, "end": 1, "new_str": "x\n"})
    assert res.startswith("ERROR"), res


def test_replace_lines_validates_python_syntax(tmp_path, monkeypatch):
    """Mirrors str_replace/create_file: a .py edit that produces invalid
    Python must be rejected, not written."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n")
    res = la.run_tool("replace_lines", {
        "path": "mod.py", "start": 1, "end": 2,
        "new_str": "def f( :\n",   # syntax error
    })
    assert res.startswith("ERROR"), res
    # File must be unchanged.
    assert (tmp_path / "mod.py").read_text() == "def f():\n    return 1\n"


def test_replace_lines_non_py_path_skips_syntax_check(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "README.md").write_text("line1\nline2\nline3\n")
    res = la.run_tool("replace_lines", {
        "path": "README.md", "start": 2, "end": 2, "new_str": "changed\n"})
    assert res.startswith("edited"), res
    assert (tmp_path / "README.md").read_text() == "line1\nchanged\nline3\n"