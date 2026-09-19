"""Tests for the OLLAMA_NUM_PARALLEL login LaunchAgent.

``launchd/com.fagan.ollama-num-parallel.plist`` is a one-shot login agent
whose only job is to put ``OLLAMA_NUM_PARALLEL`` into the user's launchd
session environment before Ollama.app (a Squirrel login item) inherits it.
``launchctl setenv`` from a shell does not survive a reboot, so without this
agent the slot count silently reverts to Ollama's default on the next restart.

The value the agent sets is not arbitrary: a runner's total KV reservation is
``num_ctx`` x ``OLLAMA_NUM_PARALLEL``, and that product has to fit the host's
GPU (see ``app/ollama_prompt_utils.py``'s tuning-entry comment). These tests
pin the agent's shape, the fact that it sets exactly one variable, and that
its slot count still fits the shipped per-model ``num_ctx``.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

from app import ollama_prompt_utils as tuning

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLIST = _REPO_ROOT / "launchd" / "com.fagan.ollama-num-parallel.plist"

_LABEL = "com.fagan.ollama-num-parallel"
_LOCAL_MODEL = "gpt-oss-20b-high:latest"

# The total KV budget the 24GB M4 host fits at 100% GPU, in tokens across all
# slots. The per-slot num_ctx times the slot count must not exceed this.
_KV_TOKEN_CEILING = 163840

# plistlib (expat-backed) rejects "--" inside XML comments; strip them the way
# tests/unit/test_generate_launchd_plists.py does.
_XML_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)

_SETENV_RE = re.compile(
    r"^launchctl setenv ([A-Z][A-Z0-9_]*) ([0-9]+)$"
)


def _plist(path: Path | None = None) -> dict:
    target = path or _PLIST
    assert target.is_file(), f"missing {target}"
    raw = _XML_COMMENT_RE.sub(b"", target.read_bytes())
    return plistlib.loads(raw)


def _setenv_command(document: dict) -> str:
    return document["ProgramArguments"][-1]


def _kv_total_tokens(slots: int, num_ctx: int) -> int:
    """Total KV tokens a runner reserves: num_ctx per slot x slot count."""
    return slots * num_ctx


# ---------------------------------------------------------------------------
# Positive: the agent exists and has the shape a one-shot login hook needs
# ---------------------------------------------------------------------------

class TestAgentShape:
    def test_plist_file_exists(self):
        assert _PLIST.is_file(), f"missing {_PLIST}"

    def test_plist_is_parseable(self):
        assert isinstance(_plist(), dict)

    def test_label_matches_the_filename(self):
        assert _plist()["Label"] == _LABEL
        assert _PLIST.name == f"{_LABEL}.plist", (
            "the installed agent's filename must match its Label, or "
            "launchctl load cannot resolve the job"
        )

    def test_runs_at_load(self):
        assert _plist()["RunAtLoad"] is True, (
            "the agent must run when the login session loads its environment"
        )

    def test_is_one_shot_not_kept_alive(self):
        assert _plist()["KeepAlive"] is False, (
            "the agent sets one env var and exits; KeepAlive would re-run it "
            "in a loop forever"
        )

    def test_invokes_a_shell_with_exactly_one_command(self):
        args = _plist()["ProgramArguments"]
        assert args[0] == "/bin/sh"
        assert args[1] == "-c"
        assert len(args) == 3, f"expected a single -c command, got {args!r}"

    def test_carries_no_environment_variables_block(self):
        """It sets the session env via launchctl, not via its own env block.

        A plist EnvironmentVariables block would only affect this agent's own
        process, which exits immediately - it would not reach Ollama.app.
        """
        assert "EnvironmentVariables" not in _plist()


# ---------------------------------------------------------------------------
# Positive: it sets OLLAMA_NUM_PARALLEL, and nothing else
# ---------------------------------------------------------------------------

class TestSetenvCommand:
    def test_sets_ollama_num_parallel_via_launchctl_setenv(self):
        command = _setenv_command(_plist())
        match = _SETENV_RE.match(command)
        assert match, (
            "the agent must run exactly `launchctl setenv NAME <digits>`; "
            f"got {command!r}"
        )
        assert match.group(1) == "OLLAMA_NUM_PARALLEL"

    def test_slot_count_is_a_positive_integer(self):
        match = _SETENV_RE.match(_setenv_command(_plist()))
        assert int(match.group(2)) >= 1, (
            "OLLAMA_NUM_PARALLEL must be >= 1; 0 would be invalid for Ollama"
        )

    def test_does_not_touch_anything_else(self):
        """Scope guard: this agent owns one variable and no others."""
        command = _setenv_command(_plist())
        assert "PIPELINE_" not in command, (
            "pipeline knobs belong in the advance-scheduler plist, not here"
        )
        assert "OLLAMA_CONTEXT_LENGTH" not in command, (
            "OLLAMA_CONTEXT_LENGTH is ignored when a request carries an "
            "explicit num_ctx, and the tuning table always supplies one"
        )
        assert command.count("setenv") == 1


# ---------------------------------------------------------------------------
# Positive: the shipped pairing still fits the host's KV budget
# ---------------------------------------------------------------------------

class TestSlotCountFitsTheContextBudget:
    def test_shipped_slots_times_tuned_num_ctx_fits_the_ceiling(self):
        slots = int(_SETENV_RE.match(_setenv_command(_plist())).group(2))
        num_ctx = tuning._tuned_num_ctx(_LOCAL_MODEL, 16384)
        total = _kv_total_tokens(slots, num_ctx)
        assert total <= _KV_TOKEN_CEILING, (
            f"{slots} slots x {num_ctx} per slot = {total} tokens, which "
            f"exceeds the {_KV_TOKEN_CEILING}-token 100%-GPU budget - Ollama "
            "would fall back to CPU_REPACK"
        )


# ---------------------------------------------------------------------------
# Negative / boundary cases
# ---------------------------------------------------------------------------

class TestNegativeCases:
    def test_missing_plist_is_reported_not_silently_passing(self, tmp_path):
        with pytest.raises(AssertionError, match="missing"):
            _plist(tmp_path / "absent.plist")

    def test_zero_slots_is_rejected(self):
        match = _SETENV_RE.match("launchctl setenv OLLAMA_NUM_PARALLEL 0")
        assert match, "the shape check matches bare digits"
        assert not int(match.group(2)) >= 1

    def test_a_non_setenv_command_is_rejected(self):
        assert not _SETENV_RE.match("export OLLAMA_NUM_PARALLEL=2")
        assert not _SETENV_RE.match("launchctl setenv OLLAMA_NUM_PARALLEL two")
        assert not _SETENV_RE.match("launchctl setenv OLLAMA_NUM_PARALLEL")

    def test_budget_helper_rejects_the_old_oversized_pairing(self):
        """The pairing that made the host unusable on 2026-09-18: 4 slots of
        the then-pinned 131072 is 3.2x the GPU budget."""
        total = _kv_total_tokens(4, 131072)
        assert total == 524288
        assert total > _KV_TOKEN_CEILING

    def test_budget_helper_rejects_one_extra_slot_above_the_shipped_pairing(self):
        """Boundary: the shipped pairing is exactly at the ceiling, so one
        more slot crosses it."""
        slots = int(_SETENV_RE.match(_setenv_command(_plist())).group(2))
        num_ctx = tuning._tuned_num_ctx(_LOCAL_MODEL, 16384)
        assert _kv_total_tokens(slots, num_ctx) <= _KV_TOKEN_CEILING
        assert _kv_total_tokens(slots + 1, num_ctx) > _KV_TOKEN_CEILING
