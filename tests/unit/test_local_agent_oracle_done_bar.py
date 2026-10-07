"""Tests for the FULL_SUITE_DONE_BAR flag on the oracle harness
(scripts/local_agent_oracle.py).

Mirrors tests/unit/test_local_agent_done_bar.py for the base harness: with
LOCAL_AGENT_FULL_SUITE_DONE_BAR=1, oracle-green alone must not end a run -
the full suite must also be green, on both the finish_if_green path and the
model's direct `done` call. Default OFF.

The flag is set with raising=False so that, before the implementation exists,
each case fails on its behavioral assertion rather than on a missing attribute.
"""
import pytest

from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401
    _finish_if_green_spy,
    _isolate_environ,
    _sequence_chat,
    lao,
    load_oracle_module_with_env,
)

DONE_REJECTED = "done rejected — full test suite still fails"


@pytest.fixture
def no_rework(monkeypatch):
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(lao, "REVIEW_FEEDBACK_REWORK", False)
    monkeypatch.setattr(lao, "write_done_marker", lambda rc: None)


def _arm(monkeypatch, value):
    monkeypatch.setattr(lao, "FULL_SUITE_DONE_BAR", value, raising=False)


# --- configuration ----------------------------------------------------------

def test_flag_is_on_when_env_var_is_one():
    mod = load_oracle_module_with_env({"LOCAL_AGENT_FULL_SUITE_DONE_BAR": "1"})
    assert mod.FULL_SUITE_DONE_BAR is True


def test_flag_is_off_when_env_var_unset():
    mod = load_oracle_module_with_env({"LOCAL_AGENT_FULL_SUITE_DONE_BAR": None})
    assert mod.FULL_SUITE_DONE_BAR is False


def test_flag_is_off_when_env_var_is_not_exactly_one():
    mod = load_oracle_module_with_env({"LOCAL_AGENT_FULL_SUITE_DONE_BAR": "true"})
    assert mod.FULL_SUITE_DONE_BAR is False


# --- finish_if_green --------------------------------------------------------

def test_finish_if_green_rejects_when_flag_armed_and_suite_red(monkeypatch, no_rework):
    _arm(monkeypatch, True)
    excerpt = "FAILED test_widget.py::test_frobnicate - assert 1 == 2"
    _, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail=excerpt)
    messages: list = []

    assert lao.finish_if_green(1, messages) is False
    assert commits == []
    assert len(full_calls) == 1
    assert messages[-1]["role"] == "user"
    assert excerpt in messages[-1]["content"]


def test_finish_if_green_accepts_when_flag_armed_and_suite_green(monkeypatch, no_rework):
    _arm(monkeypatch, True)
    _, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=True)

    assert lao.finish_if_green(1, []) is True
    assert len(commits) == 1
    assert len(full_calls) == 1


def test_finish_if_green_skips_suite_when_flag_unarmed(monkeypatch, no_rework):
    _arm(monkeypatch, False)
    _, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail="would fail")

    assert lao.finish_if_green(1, []) is True
    assert len(commits) == 1
    assert full_calls == []


def test_finish_if_green_does_not_consult_suite_when_oracle_red(monkeypatch, no_rework):
    _arm(monkeypatch, True)
    _, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=False, full_ok=True)

    assert lao.finish_if_green(1, []) is False
    assert commits == []
    assert full_calls == []


# --- the model's direct `done` call -----------------------------------------

@pytest.fixture
def done_loop(monkeypatch, no_rework):
    """Drive _main_impl() with a scripted `done`; oracle green, tree clean."""
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/acceptance_x.py"])
    monkeypatch.setattr(lao, "_capture_oracle_snapshot", lambda: None)
    monkeypatch.setattr(lao, "exclude_runtime_artifacts", lambda: None)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, "oracle ok"))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: None)
    monkeypatch.setattr(lao, "MAX_STEPS", 3)
    monkeypatch.setattr(lao, "REWORK_SUITE_REJECT_CAP", 99)
    fake, calls = _sequence_chat([("done", {"summary": "finished"})])
    monkeypatch.setattr(lao, "chat", fake)
    return calls


def test_done_rejected_when_flag_armed_and_suite_red(
        monkeypatch, done_loop, capsys):
    _arm(monkeypatch, True)
    excerpt = "FAILED test_widget.py::test_frobnicate"
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (False, excerpt, "test"))

    rc = lao._main_impl()
    out = capsys.readouterr().out

    assert rc != 0, out
    assert DONE_REJECTED in out
    last_user = [m for m in done_loop[1] if m["role"] == "user"][-1]
    assert excerpt in last_user["content"]


def test_done_accepted_when_flag_armed_and_suite_green(monkeypatch, done_loop):
    _arm(monkeypatch, True)
    monkeypatch.setattr(lao, "_full_suite_result", lambda: (True, "", None))

    assert lao._main_impl() == 0


def test_done_accepted_without_suite_when_flag_unarmed(monkeypatch, done_loop):
    _arm(monkeypatch, False)
    suite_calls: list = []
    monkeypatch.setattr(
        lao, "_full_suite_result",
        lambda: suite_calls.append(True) or (False, "would fail", "test"))

    assert lao._main_impl() == 0
    assert suite_calls == []


def test_both_flags_set_consults_suite_once_per_done(monkeypatch, done_loop):
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    _arm(monkeypatch, True)
    suite_calls: list = []
    monkeypatch.setattr(
        lao, "_full_suite_result",
        lambda: suite_calls.append(True) or (True, "", None))

    assert lao._main_impl() == 0
    assert len(suite_calls) == 1
