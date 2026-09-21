"""Acceptance: the oracle harness must not decide `done` for a
reviewer-feedback rework round on oracle-green alone.

`finish_if_green` ends the run the instant the acceptance oracle passes - "the
harness, not the model, decides done". That is right for a cold start (the
oracle is red until the implementation lands) and for a CI-fail rework (the
full-suite gate is added on top). It is wrong for a reviewer-feedback rework:
the oracle is usually ALREADY green when such a round starts, because the
first dispatch is exactly what the reviewer read. Terminating on it ends the
round after the agent's FIRST mutating step - committing whatever it happened
to touch and never the finding. Observed live 2026-09-21 on ASB-2: three of
four redispatch rounds ended this way, two of them with no commit at all, and
the story was then parked for "no new commit after 2 rework redispatches"
while the reviewer's one-line finding - a duplicated helper - was never
touched.

The first two tests reproduce that round end to end through `main()`: a
scripted model that only ever runs bash steps (it never calls `done`), with
the oracle stubbed green. With LOCAL_AGENT_REVIEW_FEEDBACK_REWORK=1 the loop
must keep working and must NOT exit 0; with it unset the old oracle-green bar
still ends the run at the first step.

The rest pin the done-bar contract directly, including the two cases that must
NOT regress: a cold start (unchanged) and a CI-fail rework (REWORK_FULL_SUITE
alone still terminates once the full suite is green).
"""
import importlib.util
import json
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_ORACLE = _SCRIPTS / "local_agent_oracle.py"

# A stable fragment of the one-shot nudge finish_if_green may append.
_NUDGE_MARK = "so it is NOT evidence that the review findings are addressed"

_ENV_KEYS = (
    "LOCAL_AGENT_REVIEW_FEEDBACK_REWORK",
    "LOCAL_AGENT_REWORK_FULL_SUITE",
    "LOCAL_AGENT_TRANSCRIPT_PATH",
    "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH",
)


def _load(monkeypatch, env):
    """Load a fresh oracle module with `env` applied - fresh, because these
    constants are read at import time."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "test-model")
    spec = importlib.util.spec_from_file_location("local_agent_oracle", str(_ORACLE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spy(mod, monkeypatch, *, oracle_ok, full_ok, dirty=True):
    """Stub the externals finish_if_green consults; return the recorders."""
    messages: list = []
    commits: list = []
    markers: list = []
    monkeypatch.setattr(mod, "oracle_result", lambda: (oracle_ok, "(stub)"))
    monkeypatch.setattr(
        mod, "_full_suite_result",
        lambda: (full_ok, "(stub tail)", None if full_ok else "test"),
    )
    monkeypatch.setattr(mod, "auto_commit", lambda reason: commits.append(reason))
    monkeypatch.setattr(mod, "worktree_dirty", lambda: dirty)
    monkeypatch.setattr(mod, "write_done_marker", lambda rc: markers.append(rc))
    return messages, commits, markers


def _bash_only_chat(calls):
    """A scripted model that only ever emits distinct bash steps - it never
    calls `done`, so the round can only end if the HARNESS decides it."""

    def _fake(messages):
        calls.append([dict(m) for m in messages])
        return {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "bash",
                          "arguments": {"command": f"true  # step {len(calls)}"}}},
        ]}

    return _fake


def _drive_round(tmp_path, monkeypatch, capsys, env):
    """Run the loop over a bash-only script and report how it ended."""
    mod = _load(monkeypatch, env)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "acceptance_fixture.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CWD", tmp_path)
    monkeypatch.setattr(mod, "ACCEPTANCE_PATHS", ["tests/acceptance_fixture.py"])
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    monkeypatch.setattr(mod, "oracle_result", lambda: (True, "(stub)"))
    calls: list = []
    monkeypatch.setattr(mod, "chat", _bash_only_chat(calls))
    rc = mod.main()
    marker = json.loads((tmp_path / ".agent_done").read_text(encoding="utf-8"))
    return rc, marker, calls, capsys.readouterr().out


def test_a_review_feedback_round_does_not_end_at_the_first_green_step(
    tmp_path, monkeypatch, capsys,
):
    rc, marker, calls, out = _drive_round(
        tmp_path, monkeypatch, capsys,
        {"LOCAL_AGENT_REVIEW_FEEDBACK_REWORK": "1"},
    )
    # The model never asked for done, so this round must not be `done`.
    assert rc != 0, f"round ended as done: rc={rc}\n{out}"
    assert marker["exit_code"] != 0, marker
    assert "acceptance tests pass" not in out, out
    assert "reviewer-feedback rework" in out, out
    # It kept working instead of stopping at step 1.
    assert len(calls) >= 2, len(calls)
    nudges = [m for m in calls[-1] if m["role"] == "user" and _NUDGE_MARK in m["content"]]
    assert len(nudges) == 1, f"nudge appended {len(nudges)} times"


def test_a_cold_start_still_ends_at_the_first_green_step(
    tmp_path, monkeypatch, capsys,
):
    """The negative control: without the signal, oracle-green still ends the
    run immediately - this is the behavior the change must not widen."""
    rc, marker, calls, out = _drive_round(tmp_path, monkeypatch, capsys, {})
    assert rc == 0, f"expected the old oracle-green bar, got rc={rc}\n{out}"
    assert marker["exit_code"] == 0, marker
    assert "acceptance tests pass" in out, out
    assert len(calls) == 1, len(calls)


def test_feedback_round_is_not_done_and_is_not_committed(monkeypatch):
    mod = _load(monkeypatch, {"LOCAL_AGENT_REVIEW_FEEDBACK_REWORK": "1"})
    messages, commits, markers = _spy(mod, monkeypatch, oracle_ok=True, full_ok=True)
    assert mod.finish_if_green(4, messages=messages) is False
    assert commits == [], commits
    assert markers == [], markers
    assert len(messages) == 1, messages


def test_feedback_nudge_is_appended_exactly_once(monkeypatch):
    mod = _load(monkeypatch, {"LOCAL_AGENT_REVIEW_FEEDBACK_REWORK": "1"})
    messages, _commits, _markers = _spy(mod, monkeypatch, oracle_ok=True, full_ok=True)
    assert mod.finish_if_green(1, messages=messages) is False
    assert mod.finish_if_green(2, messages=messages) is False
    assert mod.finish_if_green(3, messages=messages) is False
    assert len(messages) == 1, "finish_if_green runs after every mutating tool"


def test_cold_start_still_ends_and_commits(monkeypatch):
    mod = _load(monkeypatch, {})
    messages, commits, markers = _spy(mod, monkeypatch, oracle_ok=True, full_ok=True)
    assert mod.finish_if_green(5, messages=messages) is True
    assert len(commits) == 1, commits
    assert markers == [0], markers


def test_ci_fail_rework_still_ends_when_the_full_suite_is_green(monkeypatch):
    """REWORK_FULL_SUITE alone (a CI-fail rework) keeps its existing
    contract: oracle green + full suite green -> terminate."""
    mod = _load(monkeypatch, {"LOCAL_AGENT_REWORK_FULL_SUITE": "1"})
    messages, commits, markers = _spy(mod, monkeypatch, oracle_ok=True, full_ok=True)
    assert mod.finish_if_green(6, messages=messages) is True
    assert len(commits) == 1, commits
    assert markers == [0], markers


def test_a_feedback_round_never_reaches_the_suite_park_cap(monkeypatch):
    """A red suite on a reviewer-feedback round must not count toward the
    automatic-path park cap: that cap ends the round without ever letting the
    model work the findings - exactly the failure this change removes."""
    mod = _load(monkeypatch, {"LOCAL_AGENT_REVIEW_FEEDBACK_REWORK": "1"})
    messages, commits, markers = _spy(mod, monkeypatch, oracle_ok=True, full_ok=False)
    for step in range(5):
        assert mod.finish_if_green(step, messages=messages) is False
    assert mod.suite_reject_cap_reached() is False
    assert commits == [], commits
    assert markers == [], markers


def test_a_red_oracle_is_never_done_on_a_feedback_round(monkeypatch):
    mod = _load(monkeypatch, {"LOCAL_AGENT_REVIEW_FEEDBACK_REWORK": "1"})
    messages, commits, markers = _spy(mod, monkeypatch, oracle_ok=False, full_ok=True)
    assert mod.finish_if_green(1, messages=messages) is False
    assert messages == [], messages
    assert commits == [] and markers == []
