"""Tests for the out-of-process completion marker written by the local agent.

The agent's `main()` is a thin wrapper around the renamed `_main_impl()`: it
runs the loop, then atomically drops a `.agent_done` JSON marker into the
worktree so the orchestrator can detect completion without polling the pid.

These tests never run a real agent loop. They patch `_main_impl` to return a
chosen exit code and patch the module-level `CWD` constant to a tmp_path, so
the marker lands in an isolated directory and never dirties the repo tree.
"""
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")

SCRIPTS = Path(__file__).parent.parent.parent / "scripts"


def _load(name: str):
    """Load a scripts module under a unique module name so both can coexist."""
    spec = importlib.util.spec_from_file_location(
        f"agent_done_{name}", str(SCRIPTS / name)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


la = _load("local_agent.py")
oracle = _load("local_agent_oracle.py")


# ---------------------------------------------------------------------------
# main() returns _main_impl()'s exit code unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rc", [0, 1, 2, 3])
def test_main_returns_main_impl_exit_code_unchanged(rc, tmp_path, monkeypatch):
    """main() must return exactly what _main_impl() returned — the marker is
    written *after* the run and must never alter the exit code."""
    mod = la
    monkeypatch.setattr(mod, "CWD", tmp_path)
    monkeypatch.setattr(mod, "_main_impl", lambda: rc)
    assert mod.main() == rc


def test_main_returns_main_impl_exit_code_for_oracle(tmp_path, monkeypatch):
    """Same contract for the oracle variant."""
    monkeypatch.setattr(oracle, "CWD", tmp_path)
    monkeypatch.setattr(oracle, "_main_impl", lambda: 1)
    assert oracle.main() == 1


# ---------------------------------------------------------------------------
# marker contents for mapped exit codes
# ---------------------------------------------------------------------------

def _marker(tmp_path):
    p = tmp_path / ".agent_done"
    assert p.exists(), ".agent_done marker was not written"
    return json.loads(p.read_text(encoding="utf-8"))


def test_marker_after_zero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_main_impl", lambda: 0)
    la.main()
    m = _marker(tmp_path)
    assert m["reason"] == "done"
    assert m["exit_code"] == 0


def test_marker_reason_parked_for_exit_two(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_main_impl", lambda: 2)
    la.main()
    assert _marker(tmp_path)["reason"] == "parked"


def test_marker_reason_infra_failure_for_exit_three(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_main_impl", lambda: 3)
    la.main()
    assert _marker(tmp_path)["reason"] == "infra_failure"


def test_marker_reason_error_for_exit_one(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_main_impl", lambda: 1)
    la.main()
    assert _marker(tmp_path)["reason"] == "error"


# ---------------------------------------------------------------------------
# negative / boundary: unmapped exit code
# ---------------------------------------------------------------------------

def test_unmapped_exit_code_produces_error_not_raise(tmp_path, monkeypatch):
    """An exit code absent from _DONE_REASONS must fall back to 'error' rather
    than raising KeyError."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_main_impl", lambda: 9)
    assert la.main() == 9
    assert _marker(tmp_path)["reason"] == "error"
    assert _marker(tmp_path)["exit_code"] == 9


# ---------------------------------------------------------------------------
# ts parses as ISO8601
# ---------------------------------------------------------------------------

def test_marker_ts_parses_as_iso8601(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_main_impl", lambda: 0)
    la.main()
    ts = _marker(tmp_path)["ts"]
    # fromisoformat accepts the naive and aware forms datetime.isoformat emits;
    # the wrapper uses timezone-aware UTC, so this must round-trip.
    parsed = datetime.fromisoformat(ts)
    assert parsed is not None


# ---------------------------------------------------------------------------
# atomic write: no .agent_done.tmp remains
# ---------------------------------------------------------------------------

def test_no_tmp_remains_after_successful_write(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_main_impl", lambda: 0)
    la.main()
    assert (tmp_path / ".agent_done").exists()
    assert not (tmp_path / ".agent_done.tmp").exists(), (
        "a leftover .agent_done.tmp means the write was not atomic")


# ---------------------------------------------------------------------------
# marker-write failure never masks the exit code
# ---------------------------------------------------------------------------

def test_marker_failure_does_not_propagate(tmp_path, monkeypatch):
    """If os.replace raises, main() must still return _main_impl()'s exit code
    and must not propagate the exception."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_main_impl", lambda: 0)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(la.os, "replace", boom)
    assert la.main() == 0
    # Neither the final marker nor a leftover tmp should be present.
    assert not (tmp_path / ".agent_done").exists()


# ---------------------------------------------------------------------------
# exclude_runtime_artifacts writes the marker filenames
# ---------------------------------------------------------------------------

def test_exclude_runtime_artifacts_writes_agent_done(tmp_path, monkeypatch):
    """The marker files must be git-excluded so a rework/resume run doesn't
    start on a dirty tree. exclude_runtime_artifacts resolves the exclude path
    via `git rev-parse --git-path`, so point git at a tmp repo."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / ".git").mkdir()  # plain repo, not a worktree file
    info_exclude = tmp_path / ".git" / "info" / "exclude"

    def fake_git(*args, **kwargs):
        class R:
            stdout = "info/exclude\n"
            stderr = ""
            returncode = 0
        return R()

    monkeypatch.setattr(la, "git", fake_git)
    la.exclude_runtime_artifacts()
    text = info_exclude.read_text()
    assert ".agent_done" in text
    assert ".agent_done.tmp" in text
    assert ".agent_done.consumed" in text


# ---------------------------------------------------------------------------
# rename: _main_impl present, old direct def main body gone
# ---------------------------------------------------------------------------

def test_main_impl_rename_present_in_both_files():
    """The original `def main() -> int:` must be renamed to
    `def _main_impl() -> int:`; the old name must not survive as the loop
    body."""
    for name in ("local_agent.py", "local_agent_oracle.py"):
        src = (SCRIPTS / name).read_text()
        assert "def _main_impl() -> int:" in src, (
            f"{name}: main() must be renamed to _main_impl()")
        # The wrapper main() is defined separately; ensure the impl signature
        # is present exactly once.
        assert src.count("def _main_impl() -> int:") == 1


def test_wrapper_main_present_in_both_files():
    """A new module-level `def main() -> int:` wrapper must exist above the
    `if __name__ == "__main__":` block."""
    for name in ("local_agent.py", "local_agent_oracle.py"):
        src = (SCRIPTS / name).read_text()
        assert "def main() -> int:" in src, (
            f"{name}: wrapper main() must be defined")
        # The wrapper sits above the __main__ guard.
        main_idx = src.index("def main() -> int:")
        guard_idx = src.index('if __name__ == "__main__":')
        assert main_idx < guard_idx, (
            f"{name}: wrapper main() must sit above the __main__ guard")


# ---------------------------------------------------------------------------
# datetime import added to stdlib block
# ---------------------------------------------------------------------------

def test_datetime_import_present_in_both_files():
    for name in ("local_agent.py", "local_agent_oracle.py"):
        src = (SCRIPTS / name).read_text()
        assert "from datetime import datetime, timezone" in src, (
            f"{name}: must import datetime, timezone")


# ---------------------------------------------------------------------------
# _DONE_REASONS mapping defined identically in both files
# ---------------------------------------------------------------------------

def test_done_reasons_mapping_defined_in_both_files():
    expected = {0: "done", 1: "error", 2: "parked", 3: "infra_failure"}
    for mod, name in ((la, "local_agent.py"), (oracle, "local_agent_oracle.py")):
        assert hasattr(mod, "_DONE_REASONS"), (
            f"{name}: _DONE_REASONS mapping must be defined")
        assert mod._DONE_REASONS == expected, (
            f"{name}: _DONE_REASONS == {expected!r}, got {mod._DONE_REASONS!r}")


# ---------------------------------------------------------------------------
# the wrapper's marker-writing block is byte-identical across both files
# ---------------------------------------------------------------------------

def _extract_wrapper_block(src: str) -> str:
    """Extract the new wrapper `def main() -> int:` block (from its def line
    up to but not including the `if __name__ == "__main__":` guard)."""
    start = src.index("def main() -> int:")
    end = src.index('if __name__ == "__main__":', start)
    return src[start:end]


def test_wrapper_marker_block_byte_identical_across_files():
    """local_agent_oracle.py is a variant with ~1400 differing lines, but the
    added wrapper code must be byte-identical so the two copies cannot silently
    diverge. Follows the test_oracle_copy_stays_in_sync idiom."""
    a = (SCRIPTS / "local_agent.py").read_text()
    b = (SCRIPTS / "local_agent_oracle.py").read_text()
    block_a = _extract_wrapper_block(a)
    block_b = _extract_wrapper_block(b)
    assert block_a == block_b, (
        "wrapper marker-writing block differs between the two scripts:\n"
        f"--- local_agent.py ---\n{block_a}\n"
        f"--- local_agent_oracle.py ---\n{block_b}")


def test_wrapper_uses_module_CWD_not_path_cwd():
    """The marker directory MUST be the module-level CWD constant, never
    Path.cwd() — otherwise tests monkeypatching CWD would write into the repo
    root and dirty the working tree."""
    for name in ("local_agent.py", "local_agent_oracle.py"):
        src = (SCRIPTS / name).read_text()
        block = _extract_wrapper_block(src)
        assert "CWD /" in block, (
            f"{name}: wrapper must write via the CWD constant")
        assert "Path.cwd()" not in block, (
            f"{name}: wrapper must not call Path.cwd()")


def test_wrapper_has_no_story_key_field():
    """The agent process is never told its story key, so the marker must not
    carry a story_key field."""
    for name in ("local_agent.py", "local_agent_oracle.py"):
        src = (SCRIPTS / name).read_text()
        block = _extract_wrapper_block(src)
        assert "story_key" not in block, (
            f"{name}: marker must not include a story_key field")


def test_wrapper_uses_atomic_replace():
    """The write must be atomic: a tmp file plus os.replace, so a half-written
    marker is never observable."""
    for name in ("local_agent.py", "local_agent_oracle.py"):
        src = (SCRIPTS / name).read_text()
        block = _extract_wrapper_block(src)
        assert "os.replace" in block, (
            f"{name}: wrapper must use os.replace for an atomic write")
        assert ".agent_done.tmp" in block, (
            f"{name}: wrapper must write to a .agent_done.tmp staging file")