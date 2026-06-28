"""Tests for the oracle-variant local dispatch agent loop
(scripts/local_agent_oracle.py).

Imported as a module; it only requires LOCAL_AGENT_MODEL in the environment
at import time. These tests cover the read-heavy guard (Fix A — ported from
local_agent.py) and the str_replace-aware per-target guard (Fix B).
External boundaries (Ollama HTTP, pytest) are not exercised — these cover
the pure-logic helpers.
"""
import importlib.util
import os
from pathlib import Path

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent_oracle", str(Path(__file__).parent / "scripts" / "local_agent_oracle.py")
)
lao = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lao)


def _sequence_chat(responses):
    """Same shape as test_local_agent._sequence_chat: returns a fake chat()
    that yields tool-call responses from `responses` in order, plus a
    recording list."""
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


def test_oracle_constants_present():
    """Fix A: the read-heavy guard constants must be defined in the oracle
    harness (they were missing before PR #31, which is why every acceptance-
    backed story slipped past the guard)."""
    assert hasattr(lao, "READ_HEAVY_WINDOW")
    assert lao.READ_HEAVY_WINDOW == 6
    assert hasattr(lao, "MUTATING_TOOLS")
    assert lao.MUTATING_TOOLS == frozenset({"create_file", "str_replace"})


def test_oracle_read_heavy_loop_nudges_once_then_parks(tmp_path, monkeypatch, capsys):
    """Fix A: when devstral rotates across many distinct bash/view_file
    targets without ever calling create_file/str_replace, the per-target
    repetition guard misses it (every signature is unique), but the
    read-heavy guard must fire: one nudge after READ_HEAVY_WINDOW reads,
    then park after another READ_HEAVY_WINDOW if the model ignores it.

    Mirrors test_local_agent.test_local_agent_read_heavy_loop_nudges_once_then_parks
    but on the oracle harness — which previously had no such guard."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    # Oracle harness doesn't have a [boot] line, so we just need any tool
    # activity in the log. 15 distinct bash calls with no writes.
    responses = [("bash", {"command": f"cat nonexistent_{i}"}) for i in range(15)]
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
        return None

    monkeypatch.setattr(lao.subprocess, "run", _fake_run)
    ok, tail = lao.oracle_result()
    assert ok is True
    assert tail == "(no acceptance files configured)"
    assert called["run"] is False, "subprocess.run must not be called when ACCEPTANCE_PATHS is empty"
