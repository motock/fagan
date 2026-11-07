"""Failure paths of the attempt helpers in ``pipeline.dispatch_attempt``.

Both helpers are documented to fail open: ``_find_dead_new_functions`` is a
quality signal that must never block or corrupt a dispatch when git hangs or
errors, and ``_transcript_ends_with_done`` must degrade a bad transcript to the
existing resume behaviour. Git is the external boundary here, so the tests
build real repos and inject failures only into ``subprocess.run``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline import dispatch as pd
from pipeline import server as p

DEAD_FUNCTION = "_ci_rework_feedback"
BASE_SOURCE = "def existing_helper():\n    return 'hi'\n"
FEATURE_SOURCE = BASE_SOURCE + f"\n\ndef {DEAD_FUNCTION}(gate_error):\n    return gate_error\n"

_real_run = subprocess.run


def _git(*args: str, cwd: Path) -> None:
    _real_run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose `feature` branch adds one function that nothing calls."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "master", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "mod.py").write_text(BASE_SOURCE)
    _git("add", "mod.py", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    _git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "mod.py").write_text(FEATURE_SOURCE)
    _git("add", "mod.py", cwd=root)
    _git("commit", "-q", "-m", "feature", cwd=root)
    return root


def _fail_git_subcommand(monkeypatch: pytest.MonkeyPatch, subcommand: str, error: Exception) -> None:
    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", subcommand]:
            raise error
        return _real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# _find_dead_new_functions
# ---------------------------------------------------------------------------


def test_should_report_the_dead_function_when_no_git_command_fails(repo: Path) -> None:
    assert p._find_dead_new_functions(repo, "master") == [f"mod.py:{DEAD_FUNCTION}"]


@pytest.mark.parametrize("subcommand", ["diff", "show", "grep"])
@pytest.mark.parametrize(
    "error",
    [OSError("git unavailable"), subprocess.TimeoutExpired(cmd="git", timeout=15)],
    ids=["os-error", "timeout"],
)
def test_should_fail_open_when_a_git_command_errors_or_hangs(
    repo: Path, monkeypatch: pytest.MonkeyPatch, subcommand: str, error: Exception
) -> None:
    _fail_git_subcommand(monkeypatch, subcommand, error)

    assert p._find_dead_new_functions(repo, "master") == []


@pytest.mark.parametrize(
    "error",
    [OSError("git unavailable"), subprocess.TimeoutExpired(cmd="git", timeout=15)],
    ids=["os-error", "timeout"],
)
def test_should_fall_back_to_the_local_base_when_the_origin_probe_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _fail_git_subcommand(monkeypatch, "rev-parse", error)

    assert p._find_dead_new_functions(repo, "master") == [f"mod.py:{DEAD_FUNCTION}"]


def test_should_skip_a_changed_file_that_is_missing_from_the_worktree(repo: Path) -> None:
    (repo / "mod.py").unlink()

    assert p._find_dead_new_functions(repo, "master") == []


def test_should_skip_a_changed_file_that_cannot_be_read(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read_text = Path.read_text

    def read_text(self: Path, *args, **kwargs):
        if self.name == "mod.py":
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert p._find_dead_new_functions(repo, "master") == []


# ---------------------------------------------------------------------------
# _transcript_ends_with_done
# ---------------------------------------------------------------------------


def _transcript(tmp_path: Path, messages: object) -> Path:
    path = tmp_path / ".agent_transcript.json"
    path.write_text(json.dumps(messages))
    return path


def _tool_call(name: str) -> dict:
    return {"function": {"name": name}}


def test_should_detect_a_transcript_whose_last_turn_calls_done(tmp_path: Path) -> None:
    messages = [{"role": "assistant", "tool_calls": [_tool_call("done")]}]

    assert pd._transcript_ends_with_done(_transcript(tmp_path, messages)) is True


def test_should_detect_done_among_several_tool_calls_in_the_last_turn(tmp_path: Path) -> None:
    messages = [
        {"role": "assistant", "tool_calls": [_tool_call("view_file"), _tool_call("done")]}
    ]

    assert pd._transcript_ends_with_done(_transcript(tmp_path, messages)) is True


def test_should_not_detect_done_when_the_last_turn_calls_another_tool(tmp_path: Path) -> None:
    messages = [{"role": "assistant", "tool_calls": [_tool_call("view_file")]}]

    assert pd._transcript_ends_with_done(_transcript(tmp_path, messages)) is False


def test_should_only_consider_the_last_turn(tmp_path: Path) -> None:
    messages = [
        {"role": "assistant", "tool_calls": [_tool_call("done")]},
        {"role": "tool", "content": "rejected: tests still failing"},
    ]

    assert pd._transcript_ends_with_done(_transcript(tmp_path, messages)) is False


def test_should_not_detect_done_when_the_last_turn_has_no_tool_calls(tmp_path: Path) -> None:
    messages = [{"role": "assistant", "content": "Working on it."}]

    assert pd._transcript_ends_with_done(_transcript(tmp_path, messages)) is False


@pytest.mark.parametrize(
    "messages",
    [
        ["done"],  # last turn is not an object
        [{"role": "assistant", "tool_calls": ["done"]}],  # tool call is not an object
        [{"role": "assistant", "tool_calls": [{"function": None}]}],  # no function body
        [{"role": "assistant", "tool_calls": None}],
    ],
    ids=["turn-not-object", "call-not-object", "function-null", "tool-calls-null"],
)
def test_should_not_detect_done_in_a_malformed_transcript(tmp_path: Path, messages: object) -> None:
    assert pd._transcript_ends_with_done(_transcript(tmp_path, messages)) is False


def test_should_fail_open_on_unreadable_transcript_text(tmp_path: Path) -> None:
    path = tmp_path / ".agent_transcript.json"
    path.write_text("{not json")

    assert pd._transcript_ends_with_done(path) is False


def test_should_fail_open_on_a_missing_transcript_file(tmp_path: Path) -> None:
    assert pd._transcript_ends_with_done(tmp_path / "absent.json") is False


@pytest.mark.parametrize("payload", [[], {"messages": []}], ids=["empty-list", "object"])
def test_should_not_detect_done_when_the_transcript_is_not_a_nonempty_list(
    tmp_path: Path, payload: object
) -> None:
    assert pd._transcript_ends_with_done(_transcript(tmp_path, payload)) is False


# ---------------------------------------------------------------------------
# _rebrief_step_cap_struggle: a failing unsatisfiable-signal check must not
# block the retry
# ---------------------------------------------------------------------------


def test_should_still_rebrief_when_the_unsatisfiable_signal_check_raises(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def detector_raises(_evidence):
        raise RuntimeError("detector crashed")

    monkeypatch.setattr(p, "detect_unsatisfiable_signal", detector_raises)
    monkeypatch.setattr(p, "diagnose_failure", lambda *args, **kwargs: None)
    story = {"summary": "s", "agent_instructions": "ORIGINAL BRIEF"}

    p._rebrief_step_cap_struggle(story, str(repo), None, "plan", "KEY-1")

    assert "ORIGINAL BRIEF" in story["agent_instructions"]


def test_should_keep_the_rebriefed_instructions_when_the_notification_raises(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def notify_raises(*args, **kwargs):
        raise RuntimeError("notification channel down")

    monkeypatch.setattr(pd, "_notify_user", notify_raises)
    monkeypatch.setattr(p, "diagnose_failure", lambda *args, **kwargs: None)
    story = {"summary": "s", "agent_instructions": "ORIGINAL BRIEF"}

    p._rebrief_step_cap_struggle(story, str(repo), None, "plan", "KEY-1")

    assert story["agent_instructions"].startswith("ORIGINAL BRIEF")
    assert story["agent_instructions"] != "ORIGINAL BRIEF"
