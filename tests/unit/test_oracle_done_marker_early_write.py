"""PLANREFRESH-2: ``finish_if_green`` must leave proof of a finished run.

Post-mortem background (2026-09-11, PLANREFRESH-1): the agent printed the
ORACLE GREEN line, every commit was present and the full suite was green -
but ``.agent_done`` was never written because the process hung between that
print and ``main()``'s marker write, and the completed work was discarded as
``interrupted``. These tests pin the window-closing fix:

* ``finish_if_green`` writes the marker itself, via a module-level helper
  ``write_done_marker(rc)`` that ``main()`` also calls on the way out,
* a run that did NOT finish (oracle red, full suite red, suite-reject cap)
  writes NO marker,
* a second helper call is idempotent-safe and can never downgrade an
  already-written ``done`` marker,
* a marker write failure never changes the run's outcome.
"""

import inspect
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401
    _isolate_environ,
    lao,
)

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
ORACLE_SOURCE_PATH = SCRIPTS_DIR / "local_agent_oracle.py"
MARKER_NAME = ".agent_done"
EXPECTED_DONE_REASONS = {0: "done", 1: "error", 2: "parked", 3: "infra_failure"}


@pytest.fixture(scope="module")
def oracle():
    """The shared, already-loaded scripts/local_agent_oracle.py module."""
    return lao


@pytest.fixture
def oracle_cwd(monkeypatch, tmp_path, oracle):
    """Point the oracle's marker directory at a fresh tmp dir."""
    monkeypatch.setattr(oracle, "CWD", tmp_path)
    return tmp_path


@pytest.fixture
def green_oracle(monkeypatch, oracle):
    """Oracle green, no rework gate, clean worktree, no real git calls."""
    monkeypatch.setattr(oracle, "oracle_result", lambda: (True, "oracle ok"))
    monkeypatch.setattr(oracle, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(oracle, "worktree_dirty", lambda: False)
    monkeypatch.setattr(oracle, "auto_commit", lambda reason: None)


@pytest.fixture
def marker_spy(monkeypatch, oracle):
    """Record every write_done_marker call without writing anything.

    Left unarmed (calls stays unusable) if the PLANREFRESH-2 helper does not
    exist yet; each spy test asserts its presence first so those cases FAIL
    with a clear message instead of ERRORing in fixture setup."""
    calls = []
    if hasattr(oracle, "write_done_marker"):
        monkeypatch.setattr(oracle, "write_done_marker", calls.append)
    return calls


def _require_write_done_marker(oracle):
    assert hasattr(oracle, "write_done_marker"), (
        "scripts/local_agent_oracle.py must define a module-level "
        "write_done_marker(rc) helper (PLANREFRESH-2)")


def _read_marker(cwd):
    path = cwd / MARKER_NAME
    assert path.exists(), (
        "finish_if_green must write the .agent_done marker itself, before "
        "main() ever gets there - a hung process must still leave proof "
        "that the run genuinely finished (PLANREFRESH-2)")
    return json.loads(path.read_text(encoding="utf-8"))


# --- positive ---------------------------------------------------------------

def test_finish_if_green_writes_done_marker_with_exit_code_zero(
        oracle, oracle_cwd, green_oracle):
    messages = []
    assert oracle.finish_if_green(7, messages) is True
    marker = _read_marker(oracle_cwd)
    assert marker["reason"] == "done"
    assert marker["exit_code"] == 0
    assert messages == []  # the green path feeds nothing back


def test_marker_shape_matches_what_main_writes(oracle, tmp_path, monkeypatch,
                                               green_oracle):
    finish_cwd = tmp_path / "finish"
    finish_cwd.mkdir()
    monkeypatch.setattr(oracle, "CWD", finish_cwd)
    assert oracle.finish_if_green(1, []) is True
    finish_marker = _read_marker(finish_cwd)

    main_cwd = tmp_path / "main"
    main_cwd.mkdir()
    monkeypatch.setattr(oracle, "CWD", main_cwd)
    monkeypatch.setattr(oracle, "_main_impl", lambda: 0)
    assert oracle.main() == 0
    main_marker = _read_marker(main_cwd)

    assert set(finish_marker) == set(main_marker)
    assert set(finish_marker) == {"reason", "exit_code", "ts"}
    for marker in (finish_marker, main_marker):
        assert marker["reason"] == "done"
        assert marker["exit_code"] == 0
        assert isinstance(marker["exit_code"], int)
        parsed = datetime.fromisoformat(marker["ts"])
        assert parsed.tzinfo is not None  # ts parses as ISO-8601


def test_second_marker_write_with_rc_zero_is_idempotent(
        oracle, oracle_cwd, green_oracle, monkeypatch):
    assert oracle.finish_if_green(3, []) is True
    first = _read_marker(oracle_cwd)
    assert first["reason"] == "done" and first["exit_code"] == 0

    # main() calls the same helper again on the way out with the same rc.
    monkeypatch.setattr(oracle, "_main_impl", lambda: 0)
    assert oracle.main() == 0
    second = _read_marker(oracle_cwd)
    assert second["reason"] == "done"
    assert second["exit_code"] == 0
    assert set(second) == {"reason", "exit_code", "ts"}
    datetime.fromisoformat(second["ts"])  # still a well-formed marker


# --- negative / boundary ----------------------------------------------------

def test_oracle_red_writes_no_marker(oracle, oracle_cwd, monkeypatch,
                                     marker_spy):
    _require_write_done_marker(oracle)
    monkeypatch.setattr(oracle, "oracle_result",
                        lambda: (False, "acceptance failed"))
    monkeypatch.setattr(oracle, "REWORK_FULL_SUITE", False)
    messages = []
    assert oracle.finish_if_green(2, messages) is False
    assert marker_spy == []
    assert not (oracle_cwd / MARKER_NAME).exists()
    assert messages == []


def test_full_suite_red_writes_no_marker(oracle, oracle_cwd, monkeypatch,
                                         marker_spy):
    _require_write_done_marker(oracle)
    monkeypatch.setattr(oracle, "oracle_result", lambda: (True, "oracle ok"))
    monkeypatch.setattr(oracle, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(oracle, "REWORK_SUITE_REJECT_CAP", 3)
    monkeypatch.setattr(oracle, "_SUITE_REJECTIONS", 0)  # cap NOT reached
    monkeypatch.setattr(oracle, "_full_suite_result",
                        lambda: (False, "ruff check . failed", "lint"))
    messages = []
    assert oracle.finish_if_green(4, messages) is False
    assert marker_spy == []
    assert not (oracle_cwd / MARKER_NAME).exists()
    assert len(messages) == 1  # the failing excerpt is fed back instead


def test_suite_reject_cap_writes_no_marker(oracle, oracle_cwd, monkeypatch,
                                           marker_spy):
    _require_write_done_marker(oracle)
    monkeypatch.setattr(oracle, "oracle_result", lambda: (True, "oracle ok"))
    monkeypatch.setattr(oracle, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(oracle, "REWORK_SUITE_REJECT_CAP", 1)
    monkeypatch.setattr(oracle, "_SUITE_REJECTIONS", 0)  # +1 parks
    monkeypatch.setattr(oracle, "_full_suite_result",
                        lambda: (False, "still red", "gate"))
    monkeypatch.setattr(oracle, "worktree_dirty", lambda: False)
    monkeypatch.setattr(oracle, "auto_commit", lambda reason: None)
    assert oracle.finish_if_green(5, []) is False
    assert marker_spy == []
    assert not (oracle_cwd / MARKER_NAME).exists()


def test_done_marker_is_never_downgraded_by_nonzero_rc(oracle, oracle_cwd):
    oracle.write_done_marker(0)
    original = _read_marker(oracle_cwd)
    assert original["reason"] == "done" and original["exit_code"] == 0

    oracle.write_done_marker(2)  # a later parked/error path must not downgrade
    after = _read_marker(oracle_cwd)
    assert after["reason"] == "done", \
        "an already-written done marker must never be downgraded"
    assert after["exit_code"] == 0


def test_nonzero_marker_may_be_overwritten_by_rc_zero(oracle, oracle_cwd):
    oracle.write_done_marker(2)
    assert _read_marker(oracle_cwd)["reason"] == "parked"
    oracle.write_done_marker(0)
    marker = _read_marker(oracle_cwd)
    assert marker["reason"] == "done"
    assert marker["exit_code"] == 0


def test_unknown_rc_maps_to_error_reason(oracle, oracle_cwd):
    oracle.write_done_marker(99)
    marker = _read_marker(oracle_cwd)
    assert marker["reason"] == "error"
    assert marker["exit_code"] == 99


def test_marker_tmp_write_failure_is_swallowed_on_green(
        oracle, tmp_path, monkeypatch, green_oracle, capsys):
    monkeypatch.setattr(oracle, "CWD", tmp_path / "does-not-exist")
    messages = []
    assert oracle.finish_if_green(9, messages) is True  # outcome unchanged
    out = capsys.readouterr().out
    assert "ORACLE GREEN" in out
    assert "[warn] .agent_done marker not written" in out


def test_marker_replace_failure_is_swallowed_on_green(
        oracle, oracle_cwd, monkeypatch, green_oracle, capsys):
    def boom(src, dst):
        raise OSError("simulated atomic-rename failure")

    monkeypatch.setattr(os, "replace", boom)
    assert oracle.finish_if_green(1, []) is True
    out = capsys.readouterr().out
    assert "ORACLE GREEN" in out
    assert "[warn] .agent_done marker not written" in out
    assert not (oracle_cwd / MARKER_NAME).exists()


def test_main_still_writes_parked_marker_on_step_cap(oracle, oracle_cwd,
                                                     monkeypatch):
    monkeypatch.setattr(oracle, "_main_impl", lambda: 2)
    rc = oracle.main()
    assert rc == 2
    marker = _read_marker(oracle_cwd)
    assert marker["reason"] == "parked"
    assert marker["exit_code"] == 2


# --- structural contract ----------------------------------------------------

def test_write_done_marker_exists_once_at_module_level(oracle):
    source = ORACLE_SOURCE_PATH.read_text(encoding="utf-8")
    assert source.count("def write_done_marker") == 1
    fn = oracle.write_done_marker
    assert inspect.isfunction(fn)
    assert fn.__qualname__ == "write_done_marker"  # module-level, not nested
    assert list(inspect.signature(fn).parameters) == ["rc"]


def test_finish_if_green_writes_marker_after_green_print_before_return(
        oracle):
    src = inspect.getsource(oracle.finish_if_green)
    assert "write_done_marker(0)" in src
    assert src.count("return True") == 1  # single green exit
    assert src.index("write_done_marker(0)") > src.index("ORACLE GREEN")
    assert src.index("write_done_marker(0)") < src.rindex("return True")
    # the success message other code and tests key off is untouched
    assert "ORACLE GREEN" in src
    assert "acceptance tests pass; committed & done." in src


def test_main_delegates_marker_write_to_helper(oracle):
    main_src = inspect.getsource(oracle.main)
    assert "write_done_marker(" in main_src
    assert "agent_done.tmp" not in main_src  # no longer inlined in main()
    helper_src = inspect.getsource(oracle.write_done_marker)
    assert "agent_done.tmp" in helper_src  # tmp-file-then-rename preserved
    assert "os.replace" in helper_src
    assert "_DONE_REASONS" in helper_src
    assert "isoformat" in helper_src
    assert "[warn] .agent_done marker not written" in helper_src


def test_done_reasons_table_unchanged(oracle):
    assert oracle._DONE_REASONS == EXPECTED_DONE_REASONS