"""Tests for the local dispatch agent loop (scripts/local_agent.py): off-task-drift brief scoping and the start of chat() streaming + retry (5xx/status-error fakes).

Split out of test_local_agent.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `la` module itself) moved to tests.unit._local_agent_test_helpers.
"""

import httpx

from tests.unit._local_agent_test_helpers import (  # noqa: F401
    _OFF_TASK_BRIEF,
    _FakeResp,
    _sequence_chat,
    _status_error,
    la,
)


def test_off_task_edits_on_two_distinct_paths_park_after_the_nudge(
    tmp_path, monkeypatch, capsys
):
    """Two off-task mutating edits on two DIFFERENT unrelated paths: the first
    nudges, the second (a distinct target after the nudge) parks the run with
    exit code 3. This is the Mode 31 failure mode — a dispatched agent that
    abandons its task and drifts onto unrelated files."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    responses = [
        ("create_file", {"path": "totally/unrelated/scratch.py", "content": "x = 1\n"}),
        ("create_file", {"path": "elsewhere/other.py", "content": "y = 2\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert out.count("[off-task nudge:") == 1, (
        f"expected exactly one nudge (only the first distinct target nudges), "
        f"output: {out!r}"
    )
    assert "[parking: off-task drift" in out, (
        f"expected a parking line for the second distinct off-task target, "
        f"output: {out!r}"
    )
    assert rc == 3, f"expected parking exit 3, got rc={rc}\noutput: {out!r}"


def test_on_task_edits_never_nudge(tmp_path, monkeypatch, capsys):
    """Editing a file named in the brief must never trip the off-task guard.
    Create the named file with real content, then a str_replace that actually
    matches and edits it — no nudge, clean finish (rc == 0)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", _OFF_TASK_BRIEF)
    # Pre-create the on-task file so str_replace has something to match.
    (tmp_path / "pipeline" / "server.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pipeline" / "server.py").write_text("OLD = 1\n")
    responses = [
        ("str_replace", {
            "path": "pipeline/server.py",
            "old_str": "OLD = 1",
            "new_str": "NEW = 2",
        }),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[off-task nudge:" not in out, (
        f"on-task edit must never nudge; output: {out!r}"
    )
    assert rc == 0, f"expected clean finish, got rc={rc}\noutput: {out!r}"


def test_brief_naming_no_paths_never_nudges(tmp_path, monkeypatch, capsys):
    """A brief that names no files at all (plain prose, no backtick/bold paths)
    must fail open: the off-task guard never nudges, because there is nothing
    reliable to compare against. Editing any path is allowed."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_TASK", "Make the service more robust and add tests.")
    responses = [
        ("create_file", {"path": "any/random/path.py", "content": "z = 3\n"}),
        ("done", {"summary": "done"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[off-task nudge:" not in out, (
        f"a brief naming no paths must fail open and never nudge; output: {out!r}"
    )
    assert rc == 0, f"expected clean finish, got rc={rc}\noutput: {out!r}"


def test_local_agent_park_disabled_continues_past_strict_park(tmp_path, monkeypatch, capsys):
    """PARK_ENABLED=False is the kill-switch for a capable model that re-reads
    aggressively (e.g. minimax-m3:cloud re-viewing a file before editing): the
    guards still nudge but never terminate (return 3), so the step cap becomes
    the only bound and the run keeps its full budget to reach a first edit.

    Same read-heavy-repetition scenario as
    test_local_agent_read_heavy_loop_nudges_once_then_parks, which parks at
    call 12 (rc=3) with PARK_ENABLED at its default. With PARK_ENABLED=False
    the park message still prints (the detection is unchanged) but the run
    must NOT terminate there — it progresses well past call 12."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    responses = [
        ("bash", {"command": "cat a"}), ("bash", {"command": "cat b"}),
        ("bash", {"command": "cat c"}), ("bash", {"command": "cat d"}),
        ("bash", {"command": "cat e"}), ("bash", {"command": "cat f"}),
        # Post-nudge window with a repeat (cat g twice) -> strict park signal.
        ("bash", {"command": "cat g"}), ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat h"}), ("bash", {"command": "cat i"}),
        ("bash", {"command": "cat j"}), ("bash", {"command": "cat k"}),
        ("bash", {"command": "cat spare1"}), ("bash", {"command": "cat spare2"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    # The detection still fires — nudge and park message both print — but the
    # run does NOT exit 3; it continues until the scripted responses run out
    # (_sequence_chat then yields `done`, rc=0).
    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, f"nudge must still fire, output: {out!r}"
    assert "[parking: read-heavy after nudge]" in out, (
        f"detection is unchanged; the park line should still print, output: {out!r}"
    )
    # The default-PARK test parks at <= 12 calls; disabled parking must
    # progress past that point (the whole point of the switch).
    assert len(calls) > 12, (
        f"disabled parking should let the run continue past the strict-park "
        f"point, got {len(calls)} chat calls"
    )


def test_local_agent_park_disabled_continues_past_per_target_park(tmp_path, monkeypatch, capsys):
    """PARK_ENABLED=False on the per-target repetition site (the third park
    site, distinct from the read-heavy one above): re-viewing the same file
    3x nudges, a 4th would park (return 3) by default. With parking disabled
    the run must continue past that point. Covers the site the
    read-heavy-repetition test doesn't; the distinct-windows site is
    mechanically identical (same `if not PARK_ENABLED: recent_tools.clear();
    break` shape) and verified by reading."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    # view_file the same path repeatedly -> per-target seen >= 3 nudges, then
    # parks on the next. Not enough reads to trip the read-heavy window (6),
    # so this isolates the per-target guard.
    responses = [("view_file", {"path": "static/style.css"}) for _ in range(6)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert "[repetition nudge]" in out, f"per-target nudge must still fire, output: {out!r}"
    assert "[parking: repeated action after nudge]" in out, (
        f"detection is unchanged; the park line should still print, output: {out!r}"
    )
    # Default parks at the 4th same-target call; disabled parking continues
    # past it (the run only ends when scripted responses exhaust -> done).
    assert len(calls) > 4, (
        f"disabled parking should let the run continue past the per-target "
        f"park point, got {len(calls)} chat calls"
    )


def test_local_agent_repeated_per_target_park_never_leaves_tool_call_unanswered(
    tmp_path, monkeypatch, capsys,
):
    """Bug found live 2026-07-22 (Mode 33, MODE-29-REVIEW-STORY-LOCK-GUARD):
    with PARK_ENABLED=False (the scheduler plist), only the FIRST trip of the
    per-target repetition guard appended anything to the conversation (the
    nudge, as a `user`-role message). Every trip after that for the rest of
    the run silently dropped the tool call -- `break` with nothing appended
    -- leaving the triggering assistant message's `tool_calls` entry with no
    `tool`-role answer at all. Inspecting the live `.agent_transcript.json`
    confirmed this directly: two consecutive `assistant` messages with no
    intervening `tool` message, appearing immediately before gpt-oss:20b's
    Harmony-format output started leaking raw special tokens
    (`<|start|>assistant<|channel|>...`) and the run eventually collapsed
    into narration-only turns and parked.

    Fix: every trip of the guard, not just the first, must append a
    `tool`-role response for the triggering call -- this keeps the
    transcript well-formed (no orphaned tool_calls) AND re-delivers the
    corrective guidance every time instead of going silent after one
    warning."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    # Same script as test_local_agent_park_disabled_continues_past_per_target_park:
    # 6 identical view_file calls -> seen=1,2 pass through normally, seen=3..6
    # each trip the guard (first trip nudges, the other 3 previously parked
    # silently).
    responses = [("view_file", {"path": "static/style.css"}) for _ in range(6)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    capsys.readouterr()

    messages = calls[-1]
    for i, m in enumerate(messages[:-1]):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            nxt = messages[i + 1]
            assert nxt.get("role") == "tool", (
                f"assistant tool_calls at index {i} has no tool-role answer "
                f"(orphaned tool call); next message is {nxt!r}"
            )

    # 4 trips of the guard fire (seen reaches 3, 4, 5, 6) -- each must
    # re-deliver the corrective guidance, not just the first.
    nudge_msgs = [
        m for m in messages
        if m.get("role") == "tool" and "STOP reading" in (m.get("content") or "")
    ]
    assert len(nudge_msgs) == 4, (
        f"expected the corrective guidance re-delivered on every one of the "
        f"4 guard trips, got {len(nudge_msgs)}: {messages!r}"
    )


def test_local_agent_read_heavy_park_disabled_renudges_every_window(tmp_path, monkeypatch, capsys):
    """Same bug class as test_local_agent_repeated_per_target_park_never_leaves_tool_call_unanswered
    (Mode 33), applied to the read-heavy guard's `has_repetition` branch:
    with PARK_ENABLED=False, only the first read-heavy window that trips
    the guard got a corrective message; every later window that also showed
    repetition went completely silent (a bare `break`). This guard doesn't
    orphan a tool_calls entry (the real tool response for the triggering
    call already landed before this check runs), but it shares the "nudge
    once, then silence for the rest of the run" defect. Fix: append a fresh
    corrective message on every window that trips, not just the first.

    Three windows of 6 non-mutating calls: window 1 is all-distinct (fires
    the initial nudge), windows 2 and 3 each repeat one target within the
    window (wedging, not exploration) -> the has_repetition branch should
    fire twice more, each time appending the renewed guidance."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "PARK_ENABLED", False)
    responses = (
        [("bash", {"command": f"cat {c}"}) for c in "abcdef"]
        + [("bash", {"command": "cat g"}), ("bash", {"command": "cat g"}),
           ("bash", {"command": "cat h"}), ("bash", {"command": "cat i"}),
           ("bash", {"command": "cat j"}), ("bash", {"command": "cat k"})]
        + [("bash", {"command": "cat l"}), ("bash", {"command": "cat l"}),
           ("bash", {"command": "cat m"}), ("bash", {"command": "cat n"}),
           ("bash", {"command": "cat o"}), ("bash", {"command": "cat p"})]
    )
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    capsys.readouterr()

    messages = calls[-1]
    renudge_msgs = [
        m for m in messages
        if m.get("role") == "user" and "still re-reading targets" in (m.get("content") or "")
    ]
    assert len(renudge_msgs) == 2, (
        f"expected the read-heavy re-nudge on both post-initial-nudge "
        f"windows that showed repetition, got {len(renudge_msgs)}: {messages!r}"
    )


def test_local_agent_read_heavy_distinct_exploration_reaches_an_edit(tmp_path, monkeypatch, capsys):
    """Lenient path: a multi-file bug fix legitimately reads many DISTINCT
    targets (each file once) before its first edit. The exploration-aware
    read-heavy guard must NOT park such a run at 2 * READ_HEAVY_WINDOW —
    it nudges once (pushing the agent to act) then lets all-distinct
    exploration continue up to a bounded cap, so a run that reaches an
    edit finishes cleanly.

    Pre-fix (flat 12-read cutoff) this parked at step 12 before the
    create_file, returning 3. Post-fix it reaches the edit and exits 0.
    This is the regression that parked both pipeline-fix agents
    (3e44b5a7, 900c765b) during fresh bug-fix exploration on 2026-06-28."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(16)]
    # 16 distinct reads -> nudge at 6, then all-distinct post-nudge windows
    # (leniency), never reaching the 24-read distinct cap. A mutating call
    # then resets the streak and the run finishes.
    responses.append(("create_file", {"path": "new_module.rs", "content": "// real code\n"}))
    responses.append(("done", {"summary": "wrote the module"}))
    responses.append(("done", {"summary": "wrote the module"}))
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out
    assert rc == 0, (
        f"distinct exploration that reaches an edit must finish, got rc={rc}\noutput: {out!r}"
    )
    assert "[parking: read-heavy" not in out, (
        f"all-distinct exploration must not park before the bounded cap; output: {out!r}"
    )


def test_local_agent_read_heavy_distinct_exploration_is_bounded(tmp_path, monkeypatch, capsys):
    """The leniency for distinct exploration is BOUNDED, not disabled: a
    run that keeps reading distinct targets with NO eventual mutation is
    still wedging, just a slower kind. It must park after
    READ_HEAVY_WINDOW + READ_HEAVY_DISTINCT_WINDOWS * READ_HEAVY_WINDOW
    reads (6 + 3*6 = 24 by default) — not the flat 12, but still bounded.

    Pre-fix this parked at 12 (flat cutoff). Post-fix it parks at ~24 with
    a distinct-windows park message. Asserting the call count is > 12 pins
    the leniency; asserting it is <= 24 pins the bound."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(30)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out
    assert rc == 3, f"unbounded distinct reading must still park, got rc={rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, f"expected 1 nudge, output: {out!r}"
    assert "distinct windows" in out, (
        f"distinct-cap park must log a distinct-windows message; output: {out!r}"
    )
    # Leniency: parks later than the flat 12. Bound: no later than 24.
    assert len(calls) > 12, (
        f"distinct exploration must get more than the flat 12 reads; got {len(calls)}"
    )
    assert len(calls) <= 24, (
        f"distinct exploration must be bounded (<= 24 reads); got {len(calls)}"
    )


def test_local_agent_read_heavy_distinct_constants():
    """The exploration-aware guard adds a bounded-leniency constant. It
    must be defined and the nudge threshold stays 6 so the early 'push to
    act' nudge is preserved (the leniency only relaxes the PARK, not the
    nudge)."""
    assert la.READ_HEAVY_WINDOW == 6
    assert hasattr(la, "READ_HEAVY_DISTINCT_WINDOWS")
    assert la.READ_HEAVY_DISTINCT_WINDOWS == 3


def test_local_agent_does_not_nudge_with_regular_writes(tmp_path, monkeypatch, capsys):
    """The sliding window only fires when the LAST READ_HEAVY_WINDOW calls
    are all non-mutating. A model that interleaves one write per window
    (e.g. reads 5, writes 1, reads 5, writes 1, ...) must NOT be flagged —
    it's making forward progress, just slowly.

    Sequence: read, read, read, read, read, create_file, repeated. The
    write sits in the deque so the all-non-mutating check never holds.

    Each bash command is unique to dodge the per-target repetition guard,
    which is testing a different signal and isn't what this test is about."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [
        ("bash", {"command": "echo 1 > /dev/null"}),
        ("view_file", {"path": "fake.py"}),
        ("bash", {"command": "echo 2 > /dev/null"}),
        ("view_file", {"path": "fake2.py"}),
        ("bash", {"command": "echo 3 > /dev/null"}),
        # write in the middle — keeps the deque mixed.
        ("create_file", {"path": "new_module.rs", "content": "// real code\n"}),
        # Continue the pattern. Deque after step 9: [bash, view_file, bash,
        # view_file, bash, create_file] — one mutating in the window, so
        # the all-non-mutating check fails.
        ("bash", {"command": "echo 4 > /dev/null"}),
        ("view_file", {"path": "fake3.py"}),
        ("bash", {"command": "echo 5 > /dev/null"}),
        ("view_file", {"path": "fake4.py"}),
        ("bash", {"command": "echo 6 > /dev/null"}),
        # done once rejected, then auto-WIP-commits and accepts.
        ("done", {"summary": "wrote the module"}),
        ("done", {"summary": "wrote the module"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()

    out = capsys.readouterr().out
    assert "[read-heavy nudge:" not in out, (
        f"nudge must NOT fire when at least one mutating call sits in "
        f"the sliding window; output: {out!r}"
    )
    # After 1 done rejection, the harness auto-WIP-commits and accepts.
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_local_agent_read_heavy_park_does_not_wip_commit_when_clean(tmp_path, monkeypatch, capsys):
    """When the read-heavy guard parks the run on the strict (re-reading)
    path, it should not spuriously WIP-commit if the worktree is clean.
    (A read-only run by definition hasn't edited any files, so
    worktree_dirty() is False — auto_wip_commit is a no-op, but the branch
    must be reachable.) Uses the same re-reading scenario as
    test_local_agent_read_heavy_loop_nudges_once_then_parks so the strict
    park fires; the reads don't create files so the worktree stays clean."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    responses = [
        ("bash", {"command": "cat a"}),
        ("bash", {"command": "cat b"}),
        ("bash", {"command": "cat c"}),
        ("bash", {"command": "cat d"}),
        ("bash", {"command": "cat e"}),
        ("bash", {"command": "cat f"}),
        ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat h"}),
        ("bash", {"command": "cat i"}),
        ("bash", {"command": "cat j"}),
        ("bash", {"command": "cat k"}),
        ("bash", {"command": "cat spare1"}),
        ("bash", {"command": "cat spare2"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)
    # Sanity: tmp_path is empty, so worktree_dirty() returns False.
    assert not la.worktree_dirty()

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 3
    assert "[parking: read-heavy after nudge]" in out
    # No spurious commit messages in the log.
    assert "WIP" not in out, f"no commit should be made on a clean worktree; output: {out!r}"


def test_local_agent_str_replace_repetitions_do_not_fire_per_target_guard(
    tmp_path, monkeypatch, capsys,
):
    """Fix B: str_replace calls to the same path are NOT counted by the
    per-target repetition guard, because each one produces a different file
    state and the next edit's old_str would differ (or run_tool would
    reject it as 'not found'). A model iterating to fix build errors is
    making forward progress, not repeating.

    Without Fix B this script would park at the 4th str_replace (3rd
    repetition), killing the agent mid-fix (which is exactly what killed
    421b308b in the post-PR #30 rerun). With Fix B the agent runs to done
    and exits cleanly."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "pow.rs").write_text("// stub\nfn x() {}\n")
    responses = [
        # Three non-mutating calls (no per-target trip — all unique).
        ("bash", {"command": "echo a > /dev/null"}),
        ("bash", {"command": "echo b > /dev/null"}),
        ("bash", {"command": "echo c > /dev/null"}),
        # Six str_replace calls to the SAME path. Pre-fix code would
        # park at the 4th. Post-fix code ignores str_replace in the
        # per-target counter.
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub",
            "new_str": "// stub 1",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 1",
            "new_str": "// stub 2",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 2",
            "new_str": "// stub 3",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 3",
            "new_str": "// stub 4",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 4",
            "new_str": "// stub 5",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 5",
            "new_str": "// stub 6",
        }),
        # Done once rejected because worktree is dirty, auto-WIP commits
        # on second attempt.
        ("done", {"summary": "implemented"}),
        ("done", {"summary": "implemented"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    # Per-target repetition nudge must NOT fire for str_replace calls.
    assert "[repetition nudge]" not in out, (
        f"per-target guard should ignore str_replace repetitions; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" not in out, (
        f"per-target guard should not park on str_replace; output: {out!r}"
    )
    # After 1 done rejection, the harness auto-WIP-commits and accepts.
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_local_agent_repeated_create_file_on_existing_path_trips_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    """The per-target repetition guard must catch a model stuck resubmitting
    create_file against a path that already exists (non-destructive-editor
    rejects each one with "already exists" - it should use str_replace
    instead). Observed live 2026-07-15: create_file's own membership in
    MUTATING_TOOLS made `if fn in MUTATING_TOOLS: seen.clear()` wipe out its
    OWN signature's count on every single call, so seen[sig] could never
    accumulate past 1 - the guard was permanently inert for this exact
    pattern, and a real trial burned 34 consecutive create_file calls (its
    entire step budget) with no nudge ever firing."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "lru_cache.py").write_text("class LRUCache:\n    pass\n")
    responses = [("create_file", {"path": "lru_cache.py", "content": "class LRUCache:\n    x = 1\n"})
                 for _ in range(6)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" in out, (
        f"repeated create_file on an existing path must trip the per-target "
        f"guard; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" in out, (
        f"ignoring the nudge must park the run; output: {out!r}"
    )
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    assert len(calls) <= 6, (
        f"guard should stop well before burning the whole scripted sequence, "
        f"got {len(calls)} chat calls"
    )


def test_local_agent_interleaved_failed_mutations_still_trip_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    """Reproduces the real 2026-07-15 lru_cache incident precisely: a model
    alternates create_file (rejected: already exists) with str_replace
    (rejected: old_str not found) and the occasional successful bash check,
    never making real progress. Pre-fix, `if fn in MUTATING_TOOLS:
    seen.clear()` ran on every mutating call REGARDLESS OF OUTCOME, so each
    failed str_replace wiped out create_file's accumulating failure count
    before it could ever reach the threshold - the guard was inert for this
    exact interleaved pattern (34 consecutive calls burned live with zero
    nudges). The fix must gate clearing on the mutation actually SUCCEEDING,
    not merely being attempted, so failed str_replace/bash calls in between
    do not reset create_file's accumulating failure count."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "lru_cache.py").write_text("class LRUCache:\n    pass\n")
    responses = [
        ("create_file", {"path": "lru_cache.py", "content": "content1"}),  # fails: exists
        ("str_replace", {"path": "lru_cache.py", "old_str": "NOPE", "new_str": "x"}),  # fails: not found
        ("bash", {"command": "true"}),  # succeeds, not a mutating tool
        ("create_file", {"path": "lru_cache.py", "content": "content2"}),  # fails: exists
        ("str_replace", {"path": "lru_cache.py", "old_str": "NOPE2", "new_str": "x"}),  # fails: not found
        ("bash", {"command": "true"}),  # succeeds
        ("create_file", {"path": "lru_cache.py", "content": "content3"}),  # 3rd failure -> nudge
        ("create_file", {"path": "lru_cache.py", "content": "content4"}),  # ignored nudge -> park
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" in out, (
        f"interleaved failed mutations must still trip the per-target "
        f"guard; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" in out, (
        f"ignoring the nudge must park the run; output: {out!r}"
    )
    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    assert len(calls) <= 8, (
        f"guard should stop well before burning the whole scripted sequence, "
        f"got {len(calls)} chat calls"
    )


def test_local_agent_edit_between_reads_resets_per_target_repetition_counter(
    tmp_path, monkeypatch, capsys,
):
    """The per-target repetition guard's counter must not be a lifetime
    cumulative count of how many times a path was EVER viewed in the run — it
    must reset on real progress (a str_replace/create_file edit), since a
    model re-checking a file it just edited is not "repeating with no
    progress" just because it also happened to view that same path earlier.

    Without this fix, view_file(X), view_file(X), str_replace(X),
    view_file(X), view_file(X) hits the per-target threshold (seen >= 3) on
    the second post-edit view, purely from the pre-edit reads still counting
    toward the same lifetime total — nudging and then parking a run that is
    actually making progress. This reproduces the 2026-07-04 gpt-oss
    false-positive parks on ratelimiter_inspect's RLI-2 (both trials parked
    re-viewing rate_limiter.py a 3rd/4th time across the whole run, with real
    edits and test runs in between each view)."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "rate_limiter.py").write_text("class TokenBucket:\n    pass\n")
    responses = [
        ("view_file", {"path": "rate_limiter.py"}),
        ("view_file", {"path": "rate_limiter.py"}),
        ("str_replace", {
            "path": "rate_limiter.py",
            "old_str": "class TokenBucket:\n    pass\n",
            "new_str": "class TokenBucket:\n    def __init__(self):\n        pass\n",
        }),
        ("view_file", {"path": "rate_limiter.py"}),
        ("view_file", {"path": "rate_limiter.py"}),
        ("done", {"summary": "implemented"}),
        ("done", {"summary": "implemented"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" not in out, (
        f"a real edit between reads must reset the per-target counter; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" not in out, (
        f"a real edit between reads must not lead to a park; output: {out!r}"
    )
    # After 1 done rejection (worktree dirty from the str_replace), the
    # harness auto-WIP-commits and accepts, same as the str_replace test above.
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


# ---------- chat() streaming + retry (2026-06-28 timeout incident) ----------
# Mirrors the oracle-harness tests: a single transient Ollama stall must not
# kill the run. chat() streams and retries. The base harness got the same
# rewrite as the oracle, so we pin its retry contract here too.









def test_chat_retries_on_timeout_then_succeeds(monkeypatch):
    """A transient read timeout must not kill the run (2026-06-28 incident:
    all 3 e2e agents died at "LLM call failed: timed out" mid-iteration)."""
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky(payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TimeoutException("read timed out")
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(la, "_stream_one_turn", _flaky)
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3
    assert msg["content"] == "done"


def test_chat_does_not_retry_on_4xx(monkeypatch):
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _bad_request(payload):
        calls["n"] += 1
        raise _status_error(404)

    monkeypatch.setattr(la, "_stream_one_turn", _bad_request)
    try:
        la.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError(404)"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1, "4xx must NOT be retried"


# ---------- main() trims and retries once on a persistent 5xx (2026-07-22) ----------
# Found live on MODE-29-REVIEW-STORY-LOCK-GUARD: chat()'s own CHAT_MAX_ATTEMPTS
# retry sends the IDENTICAL payload every attempt, so a 5xx caused by an
# oversized transcript (Ollama/llama.cpp returns 500 rather than a clean 4xx
# for a context-window overflow) fails identically every time - confirmed via
# the real failing transcript, ~190K chars / ~47.6K estimated tokens against a
# 32768-token context window. Retrying alone can never help; main() must
# shrink the request. These pin main()'s new recovery: on a 5xx that survives
# chat()'s own retries, trim the transcript (reusing _trim_resumed_transcript)
# and retry chat() exactly once more before giving up.

def test_local_agent_trims_transcript_and_retries_once_on_persistent_5xx(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    # A tiny NUM_CTX means genuine post-head content (grown by the two
    # successful reads below) already exceeds the trim budget, so trimming
    # reliably triggers without needing to hand-construct a huge transcript.
    # _trim_resumed_transcript always preserves messages[:2] (system+task) as
    # the head and only ever drops content BEYOND it, so the failing call
    # must not be the very first one - there must be real history to trim.
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected the trim-and-retry to recover, got rc={rc}\noutput: {out!r}"
    assert calls["n"] == 4, (
        f"expected exactly 4 chat() calls (2 reads, fail once, succeed on "
        f"the trim-retry), got {calls['n']}\noutput: {out!r}"
    )
    assert "escalating trim and retrying" in out, f"expected the escalation trim log line, output: {out!r}"


def test_local_agent_gives_up_when_trim_retry_also_fails(tmp_path, monkeypatch, capsys):
    """Escalation is bounded, not an open-ended loop: if chat() still fails
    after every escalation round, main() must give up (return 1) rather
    than retrying indefinitely.

    Call count relaxed from 4 to the bounded round count on 2026-08-07:
    an unshrinkable payload now retries unchanged after a backoff instead
    of bailing on the first round (see recover_from_oversized_5xx). The
    property under test - terminates, returns 1 - is unchanged."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        raise _status_error(500)

    monkeypatch.setattr(la, "chat", _fake_chat)
    monkeypatch.setattr(la.time, "sleep", lambda _s: None)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 1, f"expected give-up after the trim-retry also fails, got rc={rc}\noutput: {out!r}"
    assert 4 <= calls["n"] <= 6, (
        f"expected the 2 reads plus a bounded escalation (<=3 rounds), "
        f"got {calls['n']}\noutput: {out!r}"
    )
    assert "LLM call failed after trim-retry" in out, f"output: {out!r}"


def test_local_agent_does_not_trim_on_4xx(tmp_path, monkeypatch, capsys):
    """A 4xx is a bad request, not a context-overflow signature - trimming
    and retrying would just mask a real bug in the request shape. Must fail
    immediately, same as before this fix, with no trim attempt."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _bad_request(messages):
        calls["n"] += 1
        raise _status_error(400)

    monkeypatch.setattr(la, "chat", _bad_request)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 1, f"expected immediate give-up on a 4xx, got rc={rc}\noutput: {out!r}"
    assert calls["n"] == 1, f"a 4xx must not trigger a trim-retry, got {calls['n']} calls"
    assert "trimming and retrying" not in out, f"output: {out!r}"


# ---------- 5xx escalation must not crash the agent loop (2026-07-30) ----------
# recover_from_oversized_5xx only catches httpx.HTTPStatusError, so a 4xx it
# re-raises or a non-HTTP backend failure (TransportError, RateLimitedError)
# propagates out of the helper. It is called from inside main()'s
# except-HTTPStatusError handler, and a sibling except-Exception does NOT catch
# exceptions raised from within another except body - so without the guard
# restored at the call site such a failure escapes main() and kills the run,
# regressing the original "must not crash the agent loop" invariant.

def test_local_agent_does_not_crash_on_transport_error_during_5xx_escalation(
    tmp_path, monkeypatch, capsys,
):
    """A TransportError raised by chat() during an escalation round must give up
    (return 1), not propagate out of main() and crash the agent loop."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)  # enter the 5xx escalation handler
        raise httpx.TransportError("connection reset")  # escalation round failure

    monkeypatch.setattr(la, "chat", _fake_chat)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 1, (
        f"expected graceful give-up on a TransportError during escalation, got "
        f"rc={rc}\noutput: {out!r}"
    )
    assert calls["n"] == 4, (
        f"expected 4 chat() calls (2 reads + 5xx + one escalation round), got "
        f"{calls['n']}\noutput: {out!r}"
    )
    assert "LLM call failed during 5xx escalation" in out, f"output: {out!r}"


def test_local_agent_does_not_crash_on_4xx_during_5xx_escalation(
    tmp_path, monkeypatch, capsys,
):
    """A 4xx re-raised by the escalation helper must give up (return 1), not
    escape main() and crash the agent loop - matching the original trim-retry
    path, which caught every failure and returned 1."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(la, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)  # enter the 5xx escalation handler
        raise _status_error(400)  # escalation round re-raises a 4xx

    monkeypatch.setattr(la, "chat", _fake_chat)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 1, (
        f"expected graceful give-up on a 4xx during escalation, got rc={rc}\n"
        f"output: {out!r}"
    )
    assert calls["n"] == 4, (
        f"expected 4 chat() calls (2 reads + 5xx + one escalation round), got "
        f"{calls['n']}\noutput: {out!r}"
    )
    assert "LLM call failed during 5xx escalation" in out, f"output: {out!r}"


# ---------- chat() provider routing (LOCAL_AGENT_PROVIDER, S3) ----------
# Ollama (PROVIDER == "ollama", the default) keeps the streaming
# _stream_one_turn path untouched. Any other provider (lmstudio, mlx) goes
# through the blocking _provider_chat_turn seam instead, which delegates to
# inference_providers.get_local_provider().chat(). These tests pin that
# branch and its retry contract without a real LM Studio/MLX server.

def test_chat_default_provider_is_ollama():
    assert la.PROVIDER == "ollama"


