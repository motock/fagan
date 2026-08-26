"""Tests for the oracle-variant local dispatch agent loop (scripts/local_agent_oracle.py): view_file range-aware repetition signature and oracle-file tamper protection.

Split out of test_local_agent_oracle.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `lao` module itself) moved to tests.unit._local_agent_oracle_test_helpers.
"""
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401
    _FakeResp,
    _isolate_environ,
    _sequence_chat,
    _status_error,
    lao,
)

# ---------- view_file range-aware repetition signature (ported, 2026-07-22) ----------

def test_oracle_view_file_different_ranges_do_not_trip_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
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
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected clean finish, got rc={rc}\noutput: {out!r}"
    assert "[repetition nudge]" not in out, f"output: {out!r}"


def test_oracle_view_file_same_range_three_times_still_trips_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line {i}" for i in range(1, 3000)) + "\n")
    responses = [
        ("view_file", {"path": "big.py", "line_start": 100, "line_end": 150})
        for _ in range(4)
    ]
    fake, _calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    lao.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" in out, f"output: {out!r}"


def test_oracle_read_heavy_loop_nudges_once_then_parks(tmp_path, monkeypatch, capsys):
    """Strict/wedging path on the oracle harness: when devstral RE-READS an
    already-seen target without ever calling create_file/str_replace, the
    read-heavy guard must fire — one nudge after READ_HEAVY_WINDOW reads,
    then park after another READ_HEAVY_WINDOW if the model ignores it.

    Mirrors test_local_agent.test_local_agent_read_heavy_loop_nudges_once_then_parks
    but on the oracle harness — which previously had no such guard. Uses
    the re-reading scenario (a target repeated within the post-nudge
    window, each target <= 2 total so the per-target guard stays out of
    the way) so this pins the strict park at 2 * READ_HEAVY_WINDOW.
    Distinct-exploration leniency is covered by
    test_oracle_read_heavy_distinct_exploration_reaches_an_edit."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    # Oracle harness doesn't have a [boot] line, so we just need any tool
    # activity in the log. 6 distinct reads (nudge) + a post-nudge window
    # of 6 where 'cat g' repeats once (re-reading = wedging).
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
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 3, f"expected parking exit 3, got {rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, (
        f"expected exactly 1 read-heavy nudge, output: {out!r}"
    )
    assert "[parking: read-heavy after nudge]" in out, (
        f"expected parking after nudge, output: {out!r}"
    )
    # chat() called at most 2 * READ_HEAVY_WINDOW times (12 default).
    assert len(calls) <= 12, (
        f"guard should stop the run well before MAX_STEPS=30; got {len(calls)}"
    )


def test_oracle_park_disabled_continues_past_strict_park(tmp_path, monkeypatch, capsys):
    """PARK_ENABLED=False on the oracle harness: the read-heavy guard still
    nudges and still prints the park line, but does NOT terminate (return 3).
    Mirrors test_local_agent.test_local_agent_park_disabled_continues_past_strict_park
    so the kill-switch is pinned on both harnesses (acceptance-bearing stories
    use the oracle). Same re-reading scenario as
    test_oracle_read_heavy_loop_nudges_once_then_parks, which parks at call 12
    by default; disabled parking must progress past it."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "PARK_ENABLED", False)
    responses = [
        ("bash", {"command": "cat a"}), ("bash", {"command": "cat b"}),
        ("bash", {"command": "cat c"}), ("bash", {"command": "cat d"}),
        ("bash", {"command": "cat e"}), ("bash", {"command": "cat f"}),
        ("bash", {"command": "cat g"}), ("bash", {"command": "cat g"}),
        ("bash", {"command": "cat h"}), ("bash", {"command": "cat i"}),
        ("bash", {"command": "cat j"}), ("bash", {"command": "cat k"}),
        ("bash", {"command": "cat spare1"}), ("bash", {"command": "cat spare2"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc != 3, f"PARK_ENABLED=False must not terminate; got rc={rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, f"nudge must still fire, output: {out!r}"
    assert "[parking: read-heavy after nudge]" in out, (
        f"detection is unchanged; the park line should still print, output: {out!r}"
    )
    assert len(calls) > 12, (
        f"disabled parking should let the run continue past the strict-park "
        f"point, got {len(calls)} chat calls"
    )


def test_oracle_read_heavy_distinct_exploration_reaches_an_edit(tmp_path, monkeypatch, capsys):
    """Lenient path on the oracle harness: distinct-target exploration
    (reading many files once each) that reaches an edit must NOT park at
    2 * READ_HEAVY_WINDOW. Mirrors the base-harness test of the same name.

    Pre-fix (flat 12-read cutoff) this parked at step 12 before the
    create_file, returning 3. Post-fix it reaches the edit and the empty
    acceptance oracle accepts done -> exit 0."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(16)]
    responses.append(("create_file", {"path": "new_module.rs", "content": "// real code\n"}))
    responses.append(("done", {"summary": "wrote the module"}))
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out
    assert rc == 0, (
        f"distinct exploration that reaches an edit must finish, got rc={rc}\noutput: {out!r}"
    )
    assert "[parking: read-heavy" not in out, (
        f"all-distinct exploration must not park before the bounded cap; output: {out!r}"
    )


def test_oracle_read_heavy_distinct_exploration_is_bounded(tmp_path, monkeypatch, capsys):
    """The leniency for distinct exploration is BOUNDED on the oracle
    harness too: a run that keeps reading distinct targets with NO
    eventual mutation still parks, at ~24 reads (not the flat 12). Mirrors
    the base-harness test of the same name."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    responses = [("bash", {"command": f"cat distinct_{i}"}) for i in range(30)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out
    assert rc == 3, f"unbounded distinct reading must still park, got rc={rc}\noutput: {out!r}"
    assert out.count("[read-heavy nudge:") == 1, f"expected 1 nudge, output: {out!r}"
    assert "distinct windows" in out, (
        f"distinct-cap park must log a distinct-windows message; output: {out!r}"
    )
    assert len(calls) > 12, (
        f"distinct exploration must get more than the flat 12 reads; got {len(calls)}"
    )
    assert len(calls) <= 24, (
        f"distinct exploration must be bounded (<= 24 reads); got {len(calls)}"
    )


def test_oracle_read_heavy_distinct_constants():
    """The exploration-aware guard's leniency constant must be defined on
    the oracle harness too (the Mode 3a lesson: a loop-guard change in the
    base harness silently regressed the oracle variant when only the base
    was patched)."""
    assert lao.READ_HEAVY_WINDOW == 6
    assert hasattr(lao, "READ_HEAVY_DISTINCT_WINDOWS")
    assert lao.READ_HEAVY_DISTINCT_WINDOWS == 3


def test_oracle_does_not_nudge_with_regular_writes(tmp_path, monkeypatch, capsys):
    """Fix A: a model that interleaves one mutating call per window must
    NOT be flagged. The deque mixes reads and writes, so the all-non-
    mutating check never holds.

    Mirrors the equivalent test in test_local_agent.py for the oracle
    harness."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    responses = [
        ("bash", {"command": "echo 1 > /dev/null"}),
        ("view_file", {"path": "fake.py"}),
        ("bash", {"command": "echo 2 > /dev/null"}),
        ("view_file", {"path": "fake2.py"}),
        ("bash", {"command": "echo 3 > /dev/null"}),
        # Write in the middle of the deque so the all-non-mutating
        # check never holds.
        ("create_file", {"path": "new_module.rs", "content": "// real code\n"}),
        ("bash", {"command": "echo 4 > /dev/null"}),
        ("view_file", {"path": "fake3.py"}),
        ("bash", {"command": "echo 5 > /dev/null"}),
        ("view_file", {"path": "fake4.py"}),
        ("bash", {"command": "echo 6 > /dev/null"}),
        # done gets rejected (worktree dirty), auto-WIP commits on retry.
        ("done", {"summary": "wrote the module"}),
        ("done", {"summary": "wrote the module"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert "[read-heavy nudge:" not in out, (
        f"nudge must NOT fire when at least one mutating call sits in the "
        f"sliding window; output: {out!r}"
    )
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_oracle_str_replace_repetitions_do_not_fire_per_target_guard(
    tmp_path, monkeypatch, capsys,
):
    """Fix B on the oracle harness: str_replace calls to the same path
    don't trip the per-target guard, even though the path is repeated.
    Mirrors test_local_agent.test_local_agent_str_replace_repetitions_do_not_fire_per_target_guard
    but for the oracle variant.

    Note: the oracle harness auto-commits via auto_commit (named
    differently from the base harness's auto_wip_commit)."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "pow.rs").write_text("// stub\nfn x() {}\n")
    responses = [
        ("bash", {"command": "echo a > /dev/null"}),
        ("bash", {"command": "echo b > /dev/null"}),
        ("bash", {"command": "echo c > /dev/null"}),
        # Six str_replace calls to the SAME path. Pre-fix code would
        # park at the 4th. Post-fix code ignores str_replace in the
        # per-target counter. The oracle harness auto-commits on done
        # once the worktree is dirty, so a single done() call here
        # succeeds (after the first done, worktree_dirty() is True,
        # but the oracle's done handler runs the oracle grading and
        # returns 0 once pytest passes — we stub pytest out via the
        # empty ACCEPTANCE_PATHS, which short-circuits oracle_result
        # to True at line 165-167).
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub", "new_str": "// stub 1",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 1", "new_str": "// stub 2",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 2", "new_str": "// stub 3",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 3", "new_str": "// stub 4",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 4", "new_str": "// stub 5",
        }),
        ("str_replace", {
            "path": "pow.rs", "old_str": "// stub 5", "new_str": "// stub 6",
        }),
        ("done", {"summary": "implemented"}),
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" not in out, (
        f"per-target guard should ignore str_replace repetitions; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" not in out, (
        f"per-target guard should not park on str_replace; output: {out!r}"
    )
    # The oracle harness accepts done() when worktree_dirty() is True
    # AND the oracle_result() returns True (which it does in this test
    # because ACCEPTANCE_PATHS is empty -> line 165 short-circuits).
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_oracle_repeated_create_file_on_existing_path_trips_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    """Mirrors test_local_agent.test_local_agent_repeated_create_file_on_
    existing_path_trips_repetition_guard for the oracle harness: create_file's
    own membership in MUTATING_TOOLS made `if fn in MUTATING_TOOLS:
    seen.clear()` wipe its own signature's count on every call, so repeated
    create_file attempts against an existing path could never accumulate
    past 1 and the per-target guard was permanently inert for this pattern."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "lru_cache.py").write_text("class LRUCache:\n    pass\n")
    responses = [("create_file", {"path": "lru_cache.py", "content": "class LRUCache:\n    x = 1\n"})
                 for _ in range(6)]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
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


def test_oracle_interleaved_failed_mutations_still_trip_repetition_guard(
    tmp_path, monkeypatch, capsys,
):
    """Mirrors test_local_agent's version: reproduces the real 2026-07-15
    lru_cache incident where failed str_replace/bash calls interleaved
    between failed create_file attempts kept wiping create_file's
    accumulating failure count (clearing was gated on tool identity, not
    outcome), making the guard inert for this exact pattern."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "lru_cache.py").write_text("class LRUCache:\n    pass\n")
    responses = [
        ("create_file", {"path": "lru_cache.py", "content": "content1"}),
        ("str_replace", {"path": "lru_cache.py", "old_str": "NOPE", "new_str": "x"}),
        ("bash", {"command": "true"}),
        ("create_file", {"path": "lru_cache.py", "content": "content2"}),
        ("str_replace", {"path": "lru_cache.py", "old_str": "NOPE2", "new_str": "x"}),
        ("bash", {"command": "true"}),
        ("create_file", {"path": "lru_cache.py", "content": "content3"}),
        ("create_file", {"path": "lru_cache.py", "content": "content4"}),
    ]
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
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


def test_oracle_edit_between_reads_resets_per_target_repetition_counter(
    tmp_path, monkeypatch, capsys,
):
    """Mirrors test_local_agent.test_local_agent_edit_between_reads_resets_
    per_target_repetition_counter for the oracle harness: the per-target
    guard's counter must reset on a real edit (str_replace/create_file), not
    stay a lifetime cumulative count across the whole run. Without this,
    view_file(X), view_file(X), str_replace(X), view_file(X), view_file(X)
    hits the >=3 threshold on the second post-edit view from stale pre-edit
    reads, even though a real edit happened in between."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
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
    ]
    fake, _ = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)

    rc = lao.main()
    out = capsys.readouterr().out

    assert "[repetition nudge]" not in out, (
        f"a real edit between reads must reset the per-target counter; output: {out!r}"
    )
    assert "[parking: repeated action after nudge]" not in out, (
        f"a real edit between reads must not lead to a park; output: {out!r}"
    )
    # Same accept path as the str_replace test above: done() on a dirty
    # worktree succeeds because ACCEPTANCE_PATHS is empty here.
    assert rc == 0, f"expected done exit 0, got {rc}\noutput: {out!r}"


def test_oracle_detects_pytest_when_pyproject_present(tmp_path, monkeypatch):
    """When the project has pyproject.toml, detect_test_command returns
    ['pytest']; the oracle must append ACCEPTANCE_PATHS + the quiet flags."""
    # ACCEPTANCE_PATHS is read at module import time, so we set it directly
    # on the module instead of via the env var.
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_x.py"])
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 't'\n")

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        # Pretend pytest ran and the test passed.
        class _R:
            returncode = 0
            stdout = "1 passed"
            stderr = ""
        return _R()

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)

    ok, tail = lao.oracle_result()
    assert ok is True
    assert tail.endswith("1 passed"), f"expected tail to include pytest stdout; got {tail!r}"
    argv = captured["argv"]
    # detect_test_command returns ['pytest'] for pyproject; oracle appends
    # acceptance paths + quiet flags.
    assert argv[0] == "pytest", f"expected pytest as argv[0], got {argv!r}"
    assert "tests/test_x.py" in argv, f"acceptance path should be appended; got {argv!r}"
    assert "-q" in argv and "--no-header" in argv and "no:cacheprovider" in argv


def test_oracle_detects_cargo_when_cargo_toml_present(tmp_path, monkeypatch):
    """When the project has Cargo.toml, detect_test_command returns
    ['cargo', 'test']; the oracle must run it bare (no pytest flags) so the
    project's [[test]] wiring picks up the acceptance file."""
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/acceptance.rs"])
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 't'\n")

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        class _R:
            returncode = 0
            stdout = "test result: ok"
            stderr = ""
        return _R()

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)

    ok, tail = lao.oracle_result()
    assert ok is True
    assert "ok" in tail
    argv = captured["argv"]
    # cargo test runs as-is — no pytest paths, no -q flag, no
    # no:cacheprovider. The cargo test runner discovers the .rs file via
    # the project's own [[test]] entries in Cargo.toml.
    assert argv[:2] == ["cargo", "test"], f"expected ['cargo', 'test'], got {argv!r}"
    assert "-q" not in argv, f"cargo does not want pytest flags; got {argv!r}"
    assert "tests/acceptance.rs" not in argv, (
        f"for cargo we run the bare command, not the acceptance path; got {argv!r}"
    )


def test_oracle_uses_npm_test_when_package_json_present(tmp_path, monkeypatch):
    """When the project has package.json, detect_test_command returns
    ['npm', 'test']; the oracle runs it bare."""
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test/acceptance.test.js"])
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "package.json").write_text('{"name": "t"}\n')

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        class _R:
            returncode = 0
            stdout = "1 passing"
            stderr = ""
        return _R()

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)

    ok, _ = lao.oracle_result()
    assert ok is True
    argv = captured["argv"]
    assert argv[:2] == ["npm", "test"], f"expected ['npm', 'test'], got {argv!r}"
    assert "-q" not in argv


def test_oracle_returns_true_with_short_circuit_when_no_acceptance_paths(tmp_path, monkeypatch):
    """Regression guard: empty ACCEPTANCE_PATHS still short-circuits
    regardless of the project's test framework. Prevents the
    detect_test_command path from running when the oracle was launched
    without an acceptance list (the existing 'fall back to model `done`'
    warning path in main())."""
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "CWD", tmp_path)
    # pyproject.toml present — without the short-circuit, detect_test_command
    # would run and pytest would be invoked.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 't'\n")
    called = {"run": False}

    def _fake_run(*a, **kw):
        called["run"] = True

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)
    ok, tail = lao.oracle_result()
    assert ok is True
    assert tail == "(no acceptance files configured)"
    assert called["run"] is False, "subprocess.run must not be called when ACCEPTANCE_PATHS is empty"


# ---------- oracle-file tamper protection ----------
# create_file/str_replace are blocked from touching the acceptance path
# directly (is_oracle_path), but that guard is a no-op for the bash tool,
# which can rm/overwrite/sed-in-place the file with no such check. A model
# could dodge the guard entirely via shell. These lock in both the existing,
# previously-untested create_file/str_replace guard and the new bash-tamper
# detection (snapshot-and-restore, since arbitrary shell syntax can't be
# reliably pattern-matched).

def test_create_file_on_oracle_path_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    (tmp_path / "test_acceptance.py").write_text("def test_x(): assert True\n")

    result = lao.run_tool("create_file", {"path": "test_acceptance.py", "content": "def test_x(): pass\n"})

    assert "must NOT be modified" in result
    assert (tmp_path / "test_acceptance.py").read_text() == "def test_x(): assert True\n"


def test_str_replace_on_oracle_path_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    (tmp_path / "test_acceptance.py").write_text("def test_x(): assert True\n")

    result = lao.run_tool("str_replace", {
        "path": "test_acceptance.py", "old_str": "assert True", "new_str": "assert False",
    })

    assert "must NOT be modified" in result
    assert (tmp_path / "test_acceptance.py").read_text() == "def test_x(): assert True\n"


def test_bash_deleting_oracle_file_is_restored(tmp_path, monkeypatch):
    """The concrete gap: is_oracle_path only guards create_file/str_replace,
    so a model could `rm` the acceptance file via bash to dodge it entirely.
    The snapshot-and-restore check must put it back and warn, regardless of
    what shell command was used to remove it."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    original = "def test_x(): assert True\n"
    (tmp_path / "test_acceptance.py").write_text(original)
    lao._capture_oracle_snapshot()

    result = lao.run_tool("bash", {"command": "rm test_acceptance.py"})

    assert (tmp_path / "test_acceptance.py").exists()
    assert (tmp_path / "test_acceptance.py").read_text() == original
    assert "restored" in result.lower()


def test_bash_overwriting_oracle_file_is_restored(tmp_path, monkeypatch):
    """Same gap, different shell technique: overwriting via redirection
    instead of deleting. Must be caught the same way."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    original = "def test_x(): assert True\n"
    (tmp_path / "test_acceptance.py").write_text(original)
    lao._capture_oracle_snapshot()

    result = lao.run_tool("bash", {"command": "echo 'def test_x(): pass' > test_acceptance.py"})

    assert (tmp_path / "test_acceptance.py").read_text() == original
    assert "restored" in result.lower()


def test_bash_not_touching_oracle_file_is_unaffected(tmp_path, monkeypatch):
    """The restore check must not false-positive on ordinary bash calls that
    never touch the oracle file at all."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["test_acceptance.py"])
    (tmp_path / "test_acceptance.py").write_text("def test_x(): assert True\n")
    lao._capture_oracle_snapshot()

    result = lao.run_tool("bash", {"command": "echo hello"})

    assert "restored" not in result.lower()
    assert "hello" in result


def test_oracle_script_importable_from_non_pipeline_cwd(tmp_path):
    """Regression guard for the PR #32 fix: the oracle harness must
    import pipeline_mcp_server at module load (reused for _heavy_lock +
    _is_heavy + _checkpoint_impl), and the import requires sys.path to
    contain the pipeline repo. local_agent.py has the sys.path.insert at
    line 56; the oracle variant was missing it until PR #32. Without the
    insert, every oracle subprocess failed at import with
    ModuleNotFoundError — the agent never reached main(), so check_story_status
    saw an empty agent.log and treated it as a failed launch.

    We assert by running `python -c "import scripts.local_agent_oracle"`
    with cwd=tmp_path (the worktree) and PYTHONPATH pointing at the
    pipeline repo. The script's own sys.path.insert handles the
    pipeline_mcp_server import, so this should succeed silently."""
    import sys
    repo = str(Path(__file__).resolve().parent.parent.parent)
    # Prefer the repo's venv python so the test reflects the real launch path;
    # fall back to the active interpreter (sys.executable) in environments
    # without a checked-in .venv — notably CI runners, which install deps into
    # the active interpreter rather than a project venv.
    venv_python = str(Path(repo) / ".venv" / "bin" / "python3")
    if not Path(venv_python).exists():
        venv_python = sys.executable
    r = subprocess.run(
        [venv_python, "-c",
         ("import importlib.util, pathlib; "
         f"spec = importlib.util.spec_from_file_location('lao', {str(Path(repo) / 'scripts' / 'local_agent_oracle.py')!r}); "
         "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
         "assert hasattr(m, 'p'), 'pipeline_mcp_server alias not bound'; "
         "assert hasattr(m.p, '_heavy_lock'), 'heavy_lock helper missing'; "
         "assert hasattr(m.p, '_is_heavy'), 'is_heavy helper missing'")],
        check=False, cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, (
        f"oracle import from non-pipeline cwd failed:\n"
        f"  stdout: {r.stdout}\n  stderr: {r.stderr}"
    )


# ---------- chat() streaming + retry (2026-06-28 timeout incident) ----------
# The original chat() was a single blocking httpx.post(stream=False, timeout=900s)
# with NO retry. One transient Ollama queue stall killed the whole run after 7-11
# steps of real progress (all 3 e2e agents died at "LLM call failed: timed out"
# mid-iteration). The new chat() streams (per-chunk silence timeout) and retries
# transient failures. These tests pin that contract by mocking _stream_one_turn
# (the per-attempt seam) and httpx.stream (the streaming seam).





def test_chat_omits_think_by_default(monkeypatch):
    """Mirrors local_agent.py's THINK behavior: unset LOCAL_AGENT_THINK must
    not add a `think` key to the payload."""
    monkeypatch.setattr(lao, "THINK", "")
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(lao, "_stream_one_turn", _capture)
    lao.chat([{"role": "user", "content": "hi"}])
    assert "think" not in captured


def test_chat_think_bool_is_passed_through(monkeypatch):
    monkeypatch.setattr(lao, "THINK", "false")
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(lao, "_stream_one_turn", _capture)
    lao.chat([{"role": "user", "content": "hi"}])
    assert captured["think"] is False


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_chat_think_level_is_passed_through(monkeypatch, level):
    """Same graded-reasoning level support as local_agent.py's copy - kept
    in sync per this file's own convention."""
    monkeypatch.setattr(lao, "THINK", level)
    captured = {}

    def _capture(payload):
        captured.update(payload)
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(lao, "_stream_one_turn", _capture)
    lao.chat([{"role": "user", "content": "hi"}])
    assert captured["think"] == level


def test_chat_retries_on_timeout_then_succeeds(monkeypatch):
    """A transient read timeout must not kill the run: chat() retries and
    returns the message once the stall clears."""
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)  # no real backoff in tests
    calls = {"n": 0}

    def _flaky(payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TimeoutException("read timed out")
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(lao, "_stream_one_turn", _flaky)
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3, f"expected 3 attempts (2 timeouts + success), got {calls['n']}"
    assert msg["content"] == "done"


def test_chat_raises_after_max_attempts_on_persistent_timeout(monkeypatch):
    """If every attempt times out, chat() exhausts CHAT_MAX_ATTEMPTS then
    re-raises — main()'s except then commits WIP and returns 1. This matches
    the old terminal behavior, but only after genuinely trying."""
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    monkeypatch.setattr(lao, "CHAT_MAX_ATTEMPTS", 3)
    calls = {"n": 0}

    def _always_timeout(payload):
        calls["n"] += 1
        raise httpx.TimeoutException("read timed out")

    monkeypatch.setattr(lao, "_stream_one_turn", _always_timeout)
    try:
        lao.chat([{"role": "user", "content": "hi"}])
        assert False, "expected TimeoutException after exhausting attempts"
    except httpx.TimeoutException:
        pass
    assert calls["n"] == 3, f"expected exactly CHAT_MAX_ATTEMPTS=3 attempts, got {calls['n']}"


def test_chat_does_not_retry_on_4xx(monkeypatch):
    """4xx is a bad request — retrying is pointless and just burns time.
    chat() must raise immediately on the first 4xx."""
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _bad_request(payload):
        calls["n"] += 1
        raise _status_error(404)

    monkeypatch.setattr(lao, "_stream_one_turn", _bad_request)
    try:
        lao.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError(404)"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1, f"4xx must NOT be retried; got {calls['n']} attempts"


def test_chat_retries_on_5xx_then_succeeds(monkeypatch):
    """5xx is a transient server error (Ollama model-not-loaded, OOM) —
    chat() retries it like a transport error."""
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky_5xx(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(503)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(lao, "_stream_one_turn", _flaky_5xx)
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


# ---------- chat() provider routing (LOCAL_AGENT_PROVIDER, S3) ----------
# Mirrors test_local_agent.py's provider-routing tests: ollama (default)
# keeps the streaming _stream_one_turn path untouched; any other provider
# (lmstudio, mlx) goes through the blocking _provider_chat_turn seam,
# delegating to inference_providers.get_local_provider().chat().

def test_chat_default_provider_is_ollama():
    assert lao.PROVIDER == "ollama"


def test_chat_uses_stream_one_turn_when_provider_is_ollama(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "ollama")

    def _boom(messages):
        raise AssertionError("_provider_chat_turn must not be called for ollama")

    monkeypatch.setattr(lao, "_provider_chat_turn", _boom)
    monkeypatch.setattr(
        lao, "_stream_one_turn",
        lambda payload: {"role": "assistant", "content": "ok"},
    )
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok"


def test_chat_routes_through_provider_when_not_ollama(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "lmstudio")

    def _boom(payload):
        raise AssertionError("_stream_one_turn must not be called for lmstudio")

    monkeypatch.setattr(lao, "_stream_one_turn", _boom)
    monkeypatch.setattr(
        lao, "_provider_chat_turn",
        lambda messages: {"role": "assistant", "content": "from lmstudio",
                           "tool_calls": [{"function": {"name": "done", "arguments": "{}"}}]},
    )
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "from lmstudio"
    assert msg["tool_calls"][0]["function"]["name"] == "done"


def test_provider_chat_turn_extracts_message_from_envelope(monkeypatch):
    captured = {}

    class _FakeProvider:
        def chat(self, messages, *, model, num_ctx, temperature, tools, endpoint, timeout):
            captured.update(model=model, num_ctx=num_ctx, temperature=temperature,
                             tools=tools, endpoint=endpoint, timeout=timeout)
            return {"message": {"role": "assistant", "content": "hi"},
                    "prompt_eval_count": 3, "eval_count": 5}

    monkeypatch.setattr(lao.inference_providers, "get_local_provider", lambda: _FakeProvider())
    msg = lao._provider_chat_turn([{"role": "user", "content": "hi"}])
    assert msg == {"role": "assistant", "content": "hi"}
    assert captured["model"] == lao.MODEL
    assert captured["tools"] == lao.TOOLS


