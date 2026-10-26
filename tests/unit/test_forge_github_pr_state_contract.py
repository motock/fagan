"""Regression guard: ``GitHubForge.pr_state``'s argv/timeout contract.

S1 (commit 9e79329) moved every ``gh`` argv out of ``pipeline/pr.py`` into
``GitHubForge`` under a "byte-identical argv" promise
(``docs/plans/FORGE_ABSTRACTION_PLAN.md``). ``pr_state`` silently drifted from
that promise in three tokens:

* ``--jq`` was shortened to ``-q`` (a different flag spelling than the
  pre-forge argv),
* the ``timeout=30`` bound was dropped, so a hung ``gh`` could block a
  dispatch forever,
* ``except OSError`` alone stopped covering ``subprocess.TimeoutExpired``,
  which is a ``SubprocessError`` and **not** an ``OSError`` - so the timeout
  the bound was meant to raise would have escaped as an uncaught exception.

The drift surfaced three stories later instead of at review time. This module
pins all three tokens so the next drift reds CI immediately.

``subprocess.run`` is the external boundary and is always mocked here - no test
ever shells out to a real ``gh``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from pipeline.forge_github import GitHubForge

# The documented fail-closed fallback for an unreadable PR state.
_FALLBACK = "UNKNOWN"

# The pre-forge argv, verbatim, as S1 promised to preserve it.
_EXPECTED_ARGV = ["gh", "pr", "view", "42", "--json", "state", "--jq", ".state"]


def _proc(stdout: str = "MERGED", returncode: int = 0) -> SimpleNamespace:
    """A fake ``gh pr view ... --jq .state`` result."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture
def gh(monkeypatch):
    """Mock the ``gh`` boundary; record every argv/kwargs it is called with.

    Patched through ``monkeypatch`` so the fake is torn down after each test
    and can never leak into the next one.
    """
    calls: list[dict] = []
    box: dict = {"result": _proc(), "exc": None}

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        if box["exc"] is not None:
            raise box["exc"]
        return box["result"]

    monkeypatch.setattr("pipeline.forge_github.subprocess.run", fake_run)
    return SimpleNamespace(calls=calls, box=box)


def test_argv_uses_long_jq_flag(gh):
    """The argv keeps the pre-forge ``--jq`` spelling, not the ``-q`` alias."""
    assert GitHubForge().pr_state("42") == "MERGED"

    assert len(gh.calls) == 1
    argv = gh.calls[0]["argv"]

    # Pin the exact spelling: asserting "either spelling is present" would
    # pass for both and pin nothing.
    assert "--jq" in argv
    assert "-q" not in argv

    # The jq filter must be the one that selects the state field.
    assert "--json" in argv
    assert argv[argv.index("--json") + 1] == "state"
    assert argv[argv.index("--jq") + 1] == ".state"

    # The handle must reach gh.
    assert "42" in argv
    assert argv == _EXPECTED_ARGV


def test_timeout_is_30(gh):
    """``gh`` is bounded by a 30s timeout so a hung CLI cannot block forever."""
    GitHubForge().pr_state("42")

    assert len(gh.calls) == 1
    assert gh.calls[0]["kwargs"].get("timeout") == 30


def test_timeout_expired_is_caught(gh):
    """``TimeoutExpired`` is a ``SubprocessError``, not an ``OSError``.

    Catching ``OSError`` alone lets the timeout escape; the except clause must
    name it explicitly.
    """
    gh.box["exc"] = subprocess.TimeoutExpired(cmd=_EXPECTED_ARGV, timeout=30)

    assert GitHubForge().pr_state("42") == _FALLBACK


def test_oserror_is_caught(gh):
    """A missing/unrunnable ``gh`` binary degrades to the fallback."""
    gh.box["exc"] = OSError("boom")

    assert GitHubForge().pr_state("42") == _FALLBACK
