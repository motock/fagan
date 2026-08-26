"""Tests for the local dispatch agent loop (scripts/local_agent.py): restore_file, net-progress guard, scratchpad-maintenance nudge, and view_file range-aware repetition signature.

Split out of test_local_agent.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `la` module itself) moved to tests.unit._local_agent_test_helpers.
"""
import subprocess

import pytest

from tests.unit._local_agent_test_helpers import (
    _OFF_TASK_BRIEF,
    _FakeProc,
    _init_git_repo,
    _sequence_chat,
    la,
    lar,
)


def test_second_consecutive_syntax_rejection_same_path_carries_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE: resubmitting the same broken content for the same path
    must escalate from the second consecutive rejection onward, telling the
    model to regenerate from scratch rather than retry the same content."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    bad_content = "+def foo():\n+    return 1\n"
    first = la.run_tool("create_file", {"path": "escalate.py", "content": bad_content})
    assert "do not resubmit" not in first.lower()
    second = la.run_tool("create_file", {"path": "escalate.py", "content": bad_content})
    assert "do not resubmit the same content" in second.lower()
    assert "regenerate the entire file" in second.lower()


def test_second_consecutive_str_replace_rejection_on_large_file_suggests_anchored_edit(
    tmp_path, monkeypatch
):
    """SYNTAX-NUDGE (large file): a str_replace rejection on an existing
    file above the size threshold must NOT get the 'regenerate the entire
    file' nudge - regenerating a large file from scratch risks corrupting
    the untouched majority of it. It should get a smaller-anchored-edit
    nudge instead."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    lines = [f"x{i} = {i}\n" for i in range(600)]
    lines.append("def marker():\n    return 1\n")
    (tmp_path / "big.py").write_text("".join(lines))
    old_str = "def marker():\n    return 1"
    new_str = "+def marker():\n    return 1"
    first = la.run_tool("str_replace", {"path": "big.py", "old_str": old_str, "new_str": new_str})
    assert first.startswith("ERROR")
    second = la.run_tool("str_replace", {"path": "big.py", "old_str": old_str, "new_str": new_str})
    assert "do not resubmit the same content" in second.lower()
    assert "regenerate the entire file" not in second.lower()
    assert "smaller" in second.lower()


def test_rejection_for_different_path_does_not_inherit_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE boundary case: a rejection for a DIFFERENT path in
    between must not carry the escalation — the counter is per-path."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    bad_content = "+def foo():\n+    return 1\n"
    la.run_tool("create_file", {"path": "a.py", "content": bad_content})
    result_b = la.run_tool("create_file", {"path": "b.py", "content": bad_content})
    assert "do not resubmit" not in result_b.lower()
    result_a_again = la.run_tool("create_file", {"path": "a.py", "content": bad_content})
    assert "do not resubmit" in result_a_again.lower()


def test_successful_write_resets_syntax_rejection_counter(tmp_path, monkeypatch):
    """SYNTAX-NUDGE boundary case: a successful write to a path resets its
    consecutive-rejection counter, so a later rejection for that same path
    starts fresh (no escalation) instead of carrying over stale state."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    bad_content = "+def foo():\n+    return 1\n"
    la.run_tool("create_file", {"path": "reset.py", "content": bad_content})
    good = la.run_tool("create_file", {"path": "reset.py", "content": "def foo():\n    return 1\n"})
    assert good == "created reset.py"
    again = la.run_tool("str_replace", {
        "path": "reset.py",
        "old_str": "    return 1",
        "new_str": "+    return 1",
    })
    assert again.startswith("ERROR")
    assert "do not resubmit" not in again.lower()


def test_syntax_rejection_never_writes_file_even_with_escalation(tmp_path, monkeypatch):
    """SYNTAX-NUDGE regression guard: the rejection must still return the
    exact original ERROR-prefixed contract and must NEVER write the file —
    not the submitted content, not a repaired version — even once escalated.
    Guards against silently reintroducing auto-repair."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    bad_content = "+def foo():\n+    return 1\n"
    la.run_tool("create_file", {"path": "guard.py", "content": bad_content})
    result = la.run_tool("create_file", {"path": "guard.py", "content": bad_content})
    assert result.startswith("ERROR")
    assert "do not resubmit" in result.lower()
    assert not (tmp_path / "guard.py").exists() or not (tmp_path / "guard.py").read_text().strip()


def test_valid_python_writes_never_trigger_escalation_text(tmp_path, monkeypatch):
    """SYNTAX-NUDGE: valid .py content must remain entirely unaffected by the
    new rejection-message/escalation machinery."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    _fresh_syntax_reject_counts = {}
    monkeypatch.setattr(la, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    monkeypatch.setattr(lar, "_SYNTAX_REJECT_COUNTS", _fresh_syntax_reject_counts)
    result1 = la.run_tool("create_file", {"path": "ok.py", "content": "x = 1\n"})
    assert result1 == "created ok.py"
    result2 = la.run_tool("str_replace", {"path": "ok.py", "old_str": "x = 1", "new_str": "x = 2"})
    assert result2 == "edited ok.py"




@pytest.mark.parametrize("cmd", [
    "git reset --hard",
    "git reset --hard HEAD",
    "git reset --hard b5063c7",
    "git reset  --hard origin/master",
    "git clean -f",
    "git clean -fd",
    "git clean -fdx",
    "git clean --force",
    "git clean -xf",      # force, f not first — must still be caught
    "git clean -df",
    "git clean -xdf",
    "git checkout -- .",
    "git checkout -- src/app.py",
    "git checkout HEAD -- src/app.py",
    "git restore src/app.py",
    # Destructive op buried in a compound command must still be caught.
    "git status && git reset --hard b5063c7",
    "cd sub && git clean -fd && git status",
    "git status\ngit reset --hard b5063c7",   # destructive on its own line IS caught
])
def test_bash_blocks_destructive_git_ops(tmp_path, monkeypatch, cmd):
    """A destructive git op (one that discards the branch's WIP commits or
    working-tree changes) must be refused before subprocess.run is reached —
    a blind-rework agent running `git reset --hard <master>` once threw away
    its own tests-passed WIP."""
    monkeypatch.setattr(la, "CWD", tmp_path)

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must not be called for a destructive git op")

    monkeypatch.setattr(la.subprocess, "run", _boom)
    result = la.run_tool("bash", {"command": cmd})
    assert isinstance(result, str)
    assert result.startswith("ERROR")
    assert "blocked" in result.lower()


@pytest.mark.parametrize("cmd", [
    "git status",
    "git add -A",
    "git commit -m 'WIP'",
    "git diff",
    "git log --oneline",
    "git checkout feature-branch",   # branch switch is NOT a discard
    "git reset HEAD src/app.py",      # unstage only, no --hard
    "git checkout --theirs src/app.py",  # merge opt, not the pathsep `--`
    "pytest -q",
    "git stash",
    "git rebase origin/master",
    # Multi-line: a safe `git reset HEAD <path>` on one line must not be
    # attributed to a `--hard` token on an unrelated later line.
    "git reset HEAD src/app.py\necho --hard-done-here\ngit status",
])
def test_bash_passes_non_destructive_commands_through(tmp_path, monkeypatch, cmd):
    """Non-destructive git ops and ordinary commands must still run."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    seen = {}

    def _fake_run(c, **k):
        seen["cmd"] = c
        return _FakeProc()

    monkeypatch.setattr(la.subprocess, "run", _fake_run)
    result = la.run_tool("bash", {"command": cmd})
    assert seen["cmd"] == cmd          # actually dispatched
    assert result == "ok"              # ran and returned output


# ---------- restore_file tool (2026-07-22 harness-improvement plan) ----------
# The destructive-git-op guard correctly blocks `git reset --hard`/`git
# checkout -- <path>`, but observed live: a model that WANTS exactly that (its
# own edits to one file went wrong and it wants a clean slate) got blocked
# three times with no alternative it could actually use, and spent the rest
# of its step budget stuck. restore_file is the safe, scoped escape hatch:
# git checkout HEAD -- <path>, one file only, reachable directly (not through
# the blocked bash patterns).

def test_restore_file_reverts_to_last_commit(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    f = tmp_path / "a.py"
    f.write_text("original\n")
    subprocess.run(["git", "add", "a.py"], check=False, cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], check=False, cwd=tmp_path, capture_output=True)
    f.write_text("a mess the model made\n")

    result = la.run_tool("restore_file", {"path": "a.py"})

    assert not result.startswith("ERROR"), f"unexpected error: {result}"
    assert f.read_text() == "original\n"


def test_restore_file_leaves_other_files_untouched(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("original a\n")
    b.write_text("original b\n")
    subprocess.run(["git", "add", "-A"], check=False, cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], check=False, cwd=tmp_path, capture_output=True)
    a.write_text("messed up a\n")
    b.write_text("a real in-progress edit to b\n")

    la.run_tool("restore_file", {"path": "a.py"})

    assert a.read_text() == "original a\n"
    assert b.read_text() == "a real in-progress edit to b\n"


def test_restore_file_requires_path():
    result = la.run_tool("restore_file", {})
    assert result.startswith("ERROR")


def test_restore_file_reports_git_error(tmp_path, monkeypatch):
    """A path git can't resolve (no repo, no such path in history) must
    surface as a clear ERROR, not crash the loop."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.run_tool("restore_file", {"path": "nonexistent.py"})
    assert result.startswith("ERROR")


def test_destructive_git_op_error_points_to_restore_file(tmp_path, monkeypatch):
    """The blocked-op message must name restore_file as the alternative for
    exactly the intent it's blocking (discard my own edits to one file)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    result = la.run_tool("bash", {"command": "git reset --hard HEAD"})
    assert "restore_file" in result


# ---------- net-progress guard (2026-07-22) ----------
# The per-target and read-heavy guards both reset on any successful mutation,
# and the no-tool-call cap only counts CONSECUTIVE narration turns. A run
# that alternates one edit with long stretches of distinct, non-repeating
# inspection and isolated give-up narration evades both indefinitely -
# observed live: 50 of 60 steps with zero further edits after an early one,
# no guard ever fired. This guard tracks steps since the last successful
# mutation directly.

def test_net_progress_guard_parks_after_max_steps_with_no_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 3)
    # Large so the read-heavy guard doesn't also fire and confuse the signal.
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(10)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 3, f"expected net-progress park, got rc={rc}\noutput: {out!r}"
    assert "no successful edit in 3 steps" in out, f"output: {out!r}"
    assert len(calls) == 3, (  # steps 0,1,2 run; the check at step 3 parks before calling chat()
        f"expected exactly 3 chat() calls before parking, got {len(calls)}\noutput: {out!r}"
    )


def test_net_progress_guard_resets_on_successful_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 3)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    responses = (
        [("bash", {"command": "cat a"}), ("bash", {"command": "cat b"})]
        + [("create_file", {"path": "new.py", "content": "# real code\n"})]
        + [("bash", {"command": "cat c"})]
        + [("done", {"summary": "wrote the module"})]
    )
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 0, (
        f"the mutation at call 3 should reset the counter so the run "
        f"reaches done, not park; got rc={rc}\noutput: {out!r}"
    )
    assert "no successful edit" not in out, f"output: {out!r}"


def test_net_progress_guard_park_disabled_renudges_instead_of_terminating(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 2)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(8)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert out.count("no successful edit in") >= 2, (
        f"expected the guard to re-fire (not just once), output: {out!r}"
    )
    assert len(calls) > 3, (
        f"disabled parking should let the run continue past the first "
        f"trip, got {len(calls)} chat calls"
    )


# ---------- scratchpad-maintenance nudge guard (2026-08-26) ----------
# dispatch.py appends a ONE-TIME instruction to the initial prompt telling
# local-family-dispatched agents to keep .agent_scratchpad.md updated with a
# running PROGRESS: n/m line, but nothing in the step loop ever reinforces
# it again. A weak model drops that single early instruction over a long
# transcript even while still making real edits elsewhere, so
# NET_PROGRESS_MAX_STEPS's own counter (which only tracks "any successful
# mutation") never fires. This guard tracks touches to the scratchpad file
# specifically, and — unlike every other guard here — must NEVER park or
# return early: it only injects a reminder and lets the run continue.

def test_scratchpad_nudge_constants_have_expected_defaults():
    """SCRATCHPAD_NUDGE_STEPS defaults to 15 (env LOCAL_AGENT_SCRATCHPAD_NUDGE_STEPS)
    and SCRATCHPAD_ON defaults to True unless PIPELINE_DECOMPOSE_SCRATCHPAD is
    explicitly set to "off"."""
    assert la.SCRATCHPAD_NUDGE_STEPS == 15
    assert la.SCRATCHPAD_ON is True


def test_scratchpad_nudge_fires_after_threshold_steps_with_no_scratchpad_touch(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "SCRATCHPAD_NUDGE_STEPS", 3)
    monkeypatch.setattr(la, "SCRATCHPAD_ON", True)
    # Large so the other step-drift guards don't also fire and confuse the signal.
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 1000)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    responses = [
        ("create_file", {"path": f"other_{i}.py", "content": "# x"}) for i in range(5)
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    out = capsys.readouterr().out

    assert "[step 3] no scratchpad update in 3 steps; nudging" in out, f"output: {out!r}"

    # calls[i] all alias the SAME underlying transcript list once main()
    # returns (chat() is fed one continuously-mutated list, not a fresh one
    # per call) so any calls[i] reflects the final transcript here.
    messages = calls[-1]
    nudge_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "user"
        and "You haven't updated .agent_scratchpad.md" in (m.get("content") or "")
    ]
    assert len(nudge_indices) == 1, (
        f"expected exactly one scratchpad nudge (threshold=3, no scratchpad "
        f"touch across 5 distinct-file steps), got {len(nudge_indices)}: {messages!r}"
    )

    create_file_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant"
        and any(
            tc.get("function", {}).get("name") == "create_file"
            for tc in (m.get("tool_calls") or [])
        )
    ]
    assert len(create_file_indices) >= 4, f"expected at least 4 create_file turns: {messages!r}"

    # Boundary: must not fire on step 0 (last_scratchpad_step=0, step=0,
    # 0-0 < SCRATCHPAD_NUDGE_STEPS) -- the nudge must sit after at least the
    # first 3 create_file turns (steps 0,1,2), not before them.
    assert nudge_indices[0] > create_file_indices[2], (
        f"nudge fired before 3 scratchpad-free steps had elapsed "
        f"(fired too early, e.g. at step 0): {messages!r}"
    )


def test_scratchpad_nudge_resets_when_scratchpad_is_touched(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "SCRATCHPAD_NUDGE_STEPS", 3)
    monkeypatch.setattr(la, "SCRATCHPAD_ON", True)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 1000)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    # Step 1 touches the scratchpad, resetting its counter's baseline to 1.
    # Steps continue to step 3 -- past the ORIGINAL (baseline-0) threshold of
    # 3 -- then the script ends via "done" before step 4, which is where the
    # reset baseline's own next trip (4 - 1 >= 3) would legitimately fire
    # again. This isolates "did the touch suppress the stale threshold" from
    # "does the guard re-arm periodically" (a separate, expected behavior).
    responses = [
        ("create_file", {"path": "other_0.py", "content": "# x"}),
        ("create_file", {"path": ".agent_scratchpad.md", "content": "PROGRESS: 1/3\n"}),
        ("create_file", {"path": "other_2.py", "content": "# x"}),
        ("done", {"summary": "finished before the next scheduled nudge"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected a clean finish, got rc={rc}\noutput: {out!r}"
    assert "no scratchpad update" not in out, (
        f"the touch at step 1 must suppress the stale step-0-baseline nudge "
        f"that would otherwise fire at step 3; output: {out!r}"
    )
    messages = calls[-1]
    assert not any(
        m.get("role") == "user"
        and "You haven't updated .agent_scratchpad.md" in (m.get("content") or "")
        for m in messages
    ), f"no nudge message should be present: {messages!r}"


def test_scratchpad_nudge_suppressed_when_scratchpad_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "SCRATCHPAD_NUDGE_STEPS", 3)
    monkeypatch.setattr(la, "SCRATCHPAD_ON", False)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 1000)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    responses = [
        ("create_file", {"path": f"other_{i}.py", "content": "# x"}) for i in range(6)
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    out = capsys.readouterr().out

    assert "no scratchpad update" not in out, f"output: {out!r}"
    messages = calls[-1]
    assert not any(
        m.get("role") == "user"
        and "You haven't updated .agent_scratchpad.md" in (m.get("content") or "")
        for m in messages
    ), f"SCRATCHPAD_ON=False must suppress the nudge entirely: {messages!r}"


def test_scratchpad_nudge_never_parks(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "SCRATCHPAD_NUDGE_STEPS", 3)
    monkeypatch.setattr(la, "SCRATCHPAD_ON", True)
    monkeypatch.setattr(la, "NET_PROGRESS_MAX_STEPS", 1000)
    monkeypatch.setattr(la, "READ_HEAVY_WINDOW", 1000)
    # 8 distinct-file steps with threshold 3 crosses the guard's trip point
    # twice (steps 3 and 6) -- it must re-fire each time (mirroring the
    # net-progress guard's own re-nudge shape) without ever parking.
    responses = [
        ("create_file", {"path": f"other_{i}.py", "content": "# x"}) for i in range(8)
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 3, (
        f"the scratchpad nudge guard alone must never park/terminate the "
        f"run; got rc={rc}\noutput: {out!r}"
    )
    assert rc == 0, f"expected the run to finish cleanly, got rc={rc}\noutput: {out!r}"
    messages = calls[-1]
    nudge_count = sum(
        1 for m in messages
        if m.get("role") == "user"
        and "You haven't updated .agent_scratchpad.md" in (m.get("content") or "")
    )
    assert nudge_count >= 2, (
        f"expected the guard to re-fire on both trips (steps 3 and 6), "
        f"got {nudge_count}: {messages!r}"
    )


# ---------- view_file range-aware repetition signature (2026-07-22) ----------
# Reading several DIFFERENT regions of one large file (routine when orienting
# in a multi-hundred-line function) must not share a signature with
# re-reading the SAME region 3x - the guard's per-path-only key made both
# indistinguishable, so a story instructing the model to consult 6 different
# locations in one 2500-line file tripped a false-positive lockout almost
# immediately.

def test_view_file_different_ranges_do_not_trip_repetition_guard(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(la, "CWD", tmp_path)
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 3000)) + "\n")
    responses = [
        ("view_file", {"path": "big.py", "line_start": 1, "line_end": 50}),
        ("view_file", {"path": "big.py", "line_start": 500, "line_end": 550}),
        ("view_file", {"path": "big.py", "line_start": 1000, "line_end": 1050}),
        ("view_file", {"path": "big.py", "line_start": 1500, "line_end": 1550}),
        ("done", {"summary": "oriented"}),
    ]
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected clean finish, got rc={rc}\noutput: {out!r}"
    assert "[repetition nudge]" not in out, (
        f"4 different regions of one file must not look like repetition; output: {out!r}"
    )


def test_view_file_same_range_three_times_still_trips_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 3000)) + "\n")
    responses = [
        ("view_file", {"path": "big.py", "line_start": 100, "line_end": 150})
        for _ in range(4)
    ]
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" in out, (
        f"the SAME range 3x must still be caught as repetition; output: {out!r}"
    )


@pytest.mark.parametrize("content,expected_name", [
    ('```json\r\n{"name": "bash", "parameters": {"command": "ls"}}\r\n```', "bash"),
    ('{"name": "done", "arguments": {"summary": "ok"}}', "done"),
    ('[TOOL_CALLS]{"name":"view_file","arguments":{"path":"a"}}', "view_file"),
])
def test_recover_tool_calls_parses_text_formats(content, expected_name):
    out = la.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == expected_name


def test_recover_tool_calls_repairs_python_triple_quoted_arguments():
    """Weaker local models (observed: Qwen2.5-Coder-14B-4bit on mlx) emit a
    str_replace's multi-line code argument using Python triple-quote syntax
    with literal newlines, which is invalid JSON. The recovery path must
    salvage it so the edit is not silently dropped."""
    content = (
        '```json\n'
        '{\n'
        '  "name": "str_replace",\n'
        '  "arguments": {\n'
        '    "path": "rate_limiter.py",\n'
        '    "old_str": "# TODO",\n'
        '    "new_str": """\n'
        'class TokenBucket:\n'
        '    def __init__(self, capacity):\n'
        '        self.capacity = capacity\n'
        '"""\n'
        '  }\n'
        '}\n'
        '```'
    )
    out = la.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == "str_replace"
    args = out[0]["function"]["arguments"]
    assert args["path"] == "rate_limiter.py"
    assert args["new_str"].startswith("\nclass TokenBucket:")
    assert "def __init__(self, capacity):" in args["new_str"]


def test_recover_tool_calls_tolerates_raw_newlines_in_json_string():
    """A distinct malformation from the triple-quote case (observed live,
    2026-07-17, Qwen2.5-Coder-14B-4bit on mlx, interval_merge task): the model
    uses ordinary double-quoted JSON string syntax for a create_file's
    multi-line `content` argument, but embeds RAW literal newline bytes
    instead of escaping them as `\\n`. This is invalid per strict JSON (a
    literal control character inside a string is illegal), so a raw
    json.loads rejects it with "Invalid control character" - the
    triple-quote repair does not apply (there are no triple quotes here) so
    the tool call was silently dropped every retry, and the agent looped
    regenerating the same correct-but-unparseable content until it hit the
    wall-clock park with the real fix never landed on disk."""
    content = (
        '```json\n'
        '{\n'
        '  "name": "create_file",\n'
        '  "arguments": {\n'
        '    "path": "intervals.py",\n'
        '    "content": "def merge(x):\n'
        '    return x"\n'
        '  }\n'
        '}\n'
        '```'
    )
    out = la.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == "create_file"
    args = out[0]["function"]["arguments"]
    assert args["path"] == "intervals.py"
    assert "def merge(x):" in args["content"]
    assert "return x" in args["content"]


def test_recover_tool_calls_returns_none_on_non_toolcall_prose():
    """A repair pass must not manufacture a tool call out of ordinary prose
    (no name/JSON object present) - failing closed keeps the step loop from
    executing a phantom call."""
    assert la.recover_tool_calls("I think the tests pass now, nothing to do.") is None


# ---------------------------------------------------------------------------
# _expected_task_paths / _is_off_task_path - pure helpers for the off-task
# drift guard. These extract file paths named in a task brief (backtick-quoted
# or bold-markdown spans) and decide whether a mutating tool call's target
# matches one of them. A brief that names no files must fail open (never flag).
# ---------------------------------------------------------------------------

def test_expected_task_paths_extracts_backtick_quoted_paths():
    """Backtick-quoted `path/to/file.py` spans are the first naming convention
    agent_instructions use; both named paths must come back, leading './'
    stripped."""
    task = "Edit `pipeline/server.py` and `utils/helpers.py` to add logging."
    assert la._expected_task_paths(task) == {"pipeline/server.py", "utils/helpers.py"}


def test_expected_task_paths_extracts_bold_markdown_paths():
    """Bold-markdown **path/to/file.py** spans are the numbered-list convention
    this repo's real agent_instructions use; both named paths must come back."""
    task = "1. **pipeline/server.py**\n2. **utils/helpers.py**\n3. Run the tests."
    assert la._expected_task_paths(task) == {"pipeline/server.py", "utils/helpers.py"}


def test_expected_task_paths_returns_empty_set_for_no_paths_named():
    """A task string with no path-like tokens yields an empty set - the guard
    must then fail open rather than flag every edit as off-task."""
    assert la._expected_task_paths("Refactor the logging module for clarity.") == set()


def test_expected_task_paths_returns_empty_set_for_empty_task():
    """An empty task string yields an empty set (boundary: empty input)."""
    assert la._expected_task_paths("") == set()


def test_expected_task_paths_strips_leading_dot_slash():
    """A backtick-quoted `./pipeline/server.py` is normalized to
    `pipeline/server.py` so suffix/basename matching is consistent."""
    assert la._expected_task_paths("Edit `./pipeline/server.py`") == {"pipeline/server.py"}


def test_expected_task_paths_returns_a_set():
    """The return type is a set (dedupes repeated mentions); assert the type
    explicitly so a list/tuple return fails loudly."""
    out = la._expected_task_paths("Edit `a.py` then `a.py` again")
    assert isinstance(out, set)
    assert out == {"a.py"}


def test_is_off_task_path_false_for_exact_match():
    """An exact match against an expected path is on-task -> False."""
    assert la._is_off_task_path("pipeline/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_false_for_dot_slash_prefixed_relative_form():
    """A './'-prefixed relative form of an expected path is still on-task
    (path-suffix containment) -> False."""
    assert la._is_off_task_path("./pipeline/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_false_for_shared_basename():
    """A differently-rooted file sharing only the basename with an expected
    path is treated as on-task -> False (basename is the reliable signal)."""
    assert la._is_off_task_path("src/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_true_for_unrelated_file():
    """A path sharing no basename or suffix with any expected path is off-task
    -> True."""
    assert la._is_off_task_path("scripts/unrelated.py", {"pipeline/server.py"}) is True


def test_is_off_task_path_fails_open_when_expected_is_empty():
    """A brief that names no files gives the guard nothing to compare against;
    any path against an empty expected set must fail open -> False."""
    assert la._is_off_task_path("scripts/anything.py", set()) is False


def test_is_off_task_path_false_for_empty_path():
    """An empty path string is never flagged -> False regardless of expected."""
    assert la._is_off_task_path("", {"pipeline/server.py"}) is False


def test_is_off_task_path_returns_bool_not_truthy_value():
    """The contract is a real bool (not e.g. None/0/1); assert the type so a
    truthy-but-wrong-typed return fails loudly on both branches."""
    assert la._is_off_task_path("scripts/unrelated.py", {"pipeline/server.py"}) is True
    assert isinstance(la._is_off_task_path("scripts/unrelated.py", {"pipeline/server.py"}), bool)
    assert isinstance(la._is_off_task_path("pipeline/server.py", {"pipeline/server.py"}), bool)


def test_local_agent_main_writes_boot_line_before_first_chat(tmp_path, monkeypatch, capsys):
    """The startup heartbeat in main() must flush to stdout *before* the
    first LLM call. check_story_status relies on this: a 0-byte agent.log
    after dispatch means the process never reached main() (a genuine failed
    launch), while a log with [boot] and no further output means the agent
    is alive and queued on Ollama's -np 1 worker.

    We assert the [boot] line lands first by running main() with chat()
    replaced by a recording fake. After main() returns, the captured stdout
    must start with [boot] pid=... and the recorded chat calls must come
    after that line was emitted."""
    # Run the agent from inside tmp_path so its git/agent.log side effects
    # are isolated to the test, and so the boot line's working directory
    # claim is benign.
    monkeypatch.setattr(la, "CWD", tmp_path)
    # Done-on-first-step: a single valid tool call to `done` with a clean
    # worktree exits immediately after the heartbeat prints.
    chat_calls = []

    def _fake_chat(messages):
        chat_calls.append(messages)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done",
                                            "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)

    rc = la.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert chat_calls, "main() should have invoked chat() at least once"
    # The very first line of output is the heartbeat; the [step 0] line
    # (if any) comes after it. Splitting on the first non-boot newline and
    # asserting boot precedes everything else proves the heartbeat was
    # emitted *before* the LLM round-trip.
    first_line = out.split("\n", 1)[0]
    assert first_line.startswith("[boot] pid="), (
        f"expected [boot] pid= as first stdout line, got: {first_line!r}\n"
        f"full output: {out!r}"
    )
    # The recorded chat() call is the LLM round-trip; the [boot] line is
    # emitted *before* it. We can't time-order the two directly from the
    # captured output, but a missing or empty stdout would be a clear
    # regression: a process whose [boot] line never flushed (e.g. someone
    # removed flush=True) is exactly the bug this heartbeat defends
    # against.




def test_local_agent_read_heavy_loop_nudges_once_then_parks(tmp_path, monkeypatch, capsys):
    """Strict/wedging path: when devstral RE-READS an already-seen target
    without ever calling create_file / str_replace, the read-heavy guard
    must fire — one corrective nudge after READ_HEAVY_WINDOW reads, then
    park after another READ_HEAVY_WINDOW if the model ignores the nudge.

    Distinct-target exploration (reading many files once each) is covered
    by test_local_agent_read_heavy_distinct_exploration_reaches_an_edit —
    that is the lenient path. This test pins the strict path: a target
    repeated within the post-nudge window is re-reading (wedging), and
    parks at 2 * READ_HEAVY_WINDOW (default 12).

    We script 6 distinct reads (nudge), then a post-nudge window of 6
    reads where one target repeats. Each target appears <= 2 times total
    so the per-target repetition guard (seen >= 3) does not fire — this
    test is about the read-heavy guard, not the per-target one."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [
        # 6 distinct reads -> nudge at READ_HEAVY_WINDOW (6).
        ("bash", {"command": "cat a"}),
        ("bash", {"command": "cat b"}),
        ("bash", {"command": "cat c"}),
        ("bash", {"command": "cat d"}),
        ("bash", {"command": "cat e"}),
        ("bash", {"command": "cat f"}),
        # Post-nudge window: 'cat g' repeats once within the window
        # (re-reading a target = wedging, not exploration). Each target
        # is still <= 2 total, so the per-target guard (seen >= 3) stays
        # out of the way.
        ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat h"}),
        ("bash", {"command": "cat i"}),
        ("bash", {"command": "cat j"}),
        ("bash", {"command": "cat k"}),
        # Spare reads in case of an off-by-one; the park fires at call 12.
        ("bash", {"command": "cat spare1"}),
        ("bash", {"command": "cat spare2"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()

    out = capsys.readouterr().out
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    # The nudge fires once, the park fires once.
    assert out.count("[read-heavy nudge:") == 1, f"expected 1 nudge, output: {out!r}"
    assert out.count("[parking: read-heavy after nudge]") == 1, f"expected 1 park, output: {out!r}"
    # chat() called at most 12 times — definitely not the full 40 step cap.
    # The exact count depends on the iteration between nudge and park, but
    # the upper bound is 2 * READ_HEAVY_WINDOW (default 12).
    assert len(calls) <= 12, (
        f"guard should stop the run well before the 40-step cap, "
        f"got {len(calls)} chat calls"
    )


# ---------------------------------------------------------------------------
# Off-task-drift guard (Mode 31) wiring tests.
#
# These drive the real la.main() entrypoint end-to-end with chat() mocked at
# its true external boundary (the _sequence_chat fake), exactly like the
# read-heavy/repetition-guard tests above. The helpers _expected_task_paths
# and _is_off_task_path already exist in scripts/local_agent.py (added by a
# prior story); these tests verify they are actually WIRED INTO main()'s
# step loop — a single off-task mutating edit nudges once and does not park,
# a second DIFFERENT off-task edit after the nudge parks (return 3), editing
# only files named in the brief never nudges, and a brief naming no files at
# all fails open (never nudges).
# ---------------------------------------------------------------------------



def test_off_task_edit_nudges_once_and_does_not_park_on_a_single_file(
    tmp_path, monkeypatch, capsys
):
    """A single off-task mutating edit prints exactly one `[off-task nudge:`
    line and does NOT park (rc == 0). A single stray file must not terminate
    the run — the guard nudges once and lets the agent explain itself."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    responses = [
        ("create_file", {"path": "totally/unrelated/scratch.py", "content": "x = 1\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[off-task nudge:") == 1, (
        f"expected exactly one off-task nudge, output: {out!r}"
    )
    assert rc == 0, f"a single stray file must not park; got rc={rc}\noutput: {out!r}"
    assert "[parking: off-task drift" not in out, (
        f"a single off-task edit must not park; output: {out!r}"
    )


