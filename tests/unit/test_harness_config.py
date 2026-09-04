"""Tests for app/harness.py's resolve_harness_name — env-driven harness
selection with fail-closed unknown-value handling.

resolve_harness_name(default) mirrors the repo's established fail-closed env
resolution pattern (app/backend.py's get_backend unknown-driver
NotImplementedError; the PIPELINE_EXEC_DISPATCH fail-closed pattern):

- reads os.environ.get('PIPELINE_AGENT_HARNESS', ''), .strip().lower()s it
- empty/unset -> returns ``default`` unchanged
- a value that IS a registered harness name (membership in _HARNESSES) ->
  returns that normalized value
- ANY other non-empty value -> raises ValueError naming the env var, the
  offending value, and sorted(_HARNESSES)

Per .claude/rules/testing-config-gates.md, every test here stubs the config
source (the PIPELINE_AGENT_HARNESS env var, via monkeypatch, auto-reverted)
and never asserts against whatever the live host happens to have set.

CUMULATIVE ARTIFACT RULE: this story reads app.harness._HARNESSES, a
registry a prior story populated with 'claude'/'local' and later stories may
extend further. Tests that need a registered name register their OWN fake
under a fresh, unique key and snapshot/restore the registry around every
test (mirroring tests/unit/test_harness.py's fixture) — never asserting the
registry's total contents, count, or exact key set. Where the error-message
"lists sorted(_HARNESSES)" requirement is checked, it is checked by
membership and relative ordering of fakes this file itself registers, never
by matching the full formatted list.

RED STATE: resolve_harness_name does not exist yet in app/harness.py as of
this commit. Every test below is expected to fail at collection/call time
with an ImportError or AttributeError until a later dispatch adds it — that
is the correct state to leave this file in.
"""
from __future__ import annotations

import pytest

from app import backend as b
from app import harness
from app.harness import register_harness, resolve_harness_name
from tests.unit._backend_helpers import _FakePopenResult

ENV_VAR = "PIPELINE_AGENT_HARNESS"


class _FakeHarness:
    """Minimal registrable double — resolve_harness_name only checks
    membership in _HARNESSES, so this never needs to build a command."""


@pytest.fixture(autouse=True)
def _registry_snapshot():
    """Snapshot/restore the cumulative _HARNESSES registry around each test
    so fakes registered here never leak into sibling test modules."""
    saved = dict(harness._HARNESSES)
    yield
    harness._HARNESSES.clear()
    harness._HARNESSES.update(saved)


# ---------------------------------------------------------------------------
# Unset / missing env var -> default unchanged
# ---------------------------------------------------------------------------


def test_var_unset_returns_claude_default_unchanged(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_harness_name("claude") == "claude"


def test_var_unset_returns_local_default_unchanged(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_harness_name("local") == "local"


# ---------------------------------------------------------------------------
# Empty / whitespace-only value -> treated as unset, per repo convention
# ---------------------------------------------------------------------------


def test_empty_string_env_value_treated_as_unset(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "")
    assert resolve_harness_name("claude") == "claude"


def test_whitespace_only_env_value_treated_as_unset(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "   ")
    assert resolve_harness_name("local") == "local"


# ---------------------------------------------------------------------------
# A registered name (with normalization) overrides the default
# ---------------------------------------------------------------------------


def test_var_set_to_registered_name_overrides_default(monkeypatch):
    register_harness("probe-exact", _FakeHarness)
    monkeypatch.setenv(ENV_VAR, "probe-exact")
    assert resolve_harness_name("claude") == "probe-exact"


def test_var_set_to_mixed_case_padded_value_is_normalized(monkeypatch):
    register_harness("probe-norm", _FakeHarness)
    monkeypatch.setenv(ENV_VAR, "  PROBE-NORM ")
    assert resolve_harness_name("claude") == "probe-norm"


# ---------------------------------------------------------------------------
# Unregistered, non-empty value -> fail closed with ValueError
# ---------------------------------------------------------------------------


def test_unregistered_value_raises_value_error(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "codex")
    with pytest.raises(ValueError):
        resolve_harness_name("claude")


def test_unregistered_value_never_silently_falls_back_to_default(monkeypatch):
    """Fail-closed: an unknown value must error, not ride the default."""
    monkeypatch.setenv(ENV_VAR, "goose")
    try:
        resolve_harness_name("claude")
    except ValueError:
        pass
    else:
        pytest.fail(
            "resolve_harness_name silently returned instead of raising "
            "ValueError for an unregistered, non-empty env value"
        )


def test_unregistered_value_error_names_the_env_var(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "goose")
    with pytest.raises(ValueError, match="PIPELINE_AGENT_HARNESS"):
        resolve_harness_name("claude")


def test_unregistered_value_error_names_the_offending_value(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "goose")
    with pytest.raises(ValueError) as excinfo:
        resolve_harness_name("claude")
    assert "goose" in str(excinfo.value)


def test_unregistered_value_error_lists_registered_names_in_sorted_order(monkeypatch):
    """The error message must include sorted(_HARNESSES) — checked here by
    membership + relative ordering of two fakes this test itself registers,
    never by asserting the registry's full/exact contents."""
    register_harness("zzz-late", _FakeHarness)
    register_harness("aaa-early", _FakeHarness)
    monkeypatch.setenv(ENV_VAR, "unknown-driver")
    with pytest.raises(ValueError) as excinfo:
        resolve_harness_name("claude")
    message = str(excinfo.value)
    assert "aaa-early" in message
    assert "zzz-late" in message
    assert message.index("aaa-early") < message.index("zzz-late")


def test_another_unregistered_value_also_raises_value_error(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "")
    monkeypatch.setenv(ENV_VAR, "nonexistent-harness-xyz")
    with pytest.raises(ValueError) as excinfo:
        resolve_harness_name("local")
    assert "nonexistent-harness-xyz" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ClaudeCliDriver.dispatch() / OllamaDriver.dispatch() must actually enforce
# resolve_harness_name('claude') / resolve_harness_name('local') as a guard
# before spawning anything: PIPELINE_AGENT_HARNESS unset or matching the
# driver's own harness proceeds; a cross-harness (mismatched, but registered)
# value must fail closed with NotImplementedError naming both the env var
# and the offending value. Per code review on this branch, this behavior
# previously had zero coverage — resolve_harness_name itself was tested in
# isolation above, but no test ever called a driver's dispatch() with the
# env var set.
# ---------------------------------------------------------------------------


def test_claude_dispatch_proceeds_when_harness_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(101),
    )

    handle = b.ClaudeCliDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 101


def test_claude_dispatch_proceeds_when_harness_env_matches(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "claude")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(102),
    )

    handle = b.ClaudeCliDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 102


def test_claude_dispatch_raises_on_cross_harness_value(tmp_path, monkeypatch):
    """A cross-harness value (registered, but not 'claude') must fail closed
    rather than silently dispatching via ClaudeCliDriver anyway."""
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(103),
    )

    with pytest.raises(NotImplementedError) as excinfo:
        b.ClaudeCliDriver().dispatch(
            "do it", system=None, model="opus", allowed_tools="Bash,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )

    message = str(excinfo.value)
    assert ENV_VAR in message
    assert "local" in message


def test_ollama_dispatch_proceeds_when_harness_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(201),
    )

    handle = b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 201


def test_ollama_dispatch_proceeds_when_harness_env_matches(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(202),
    )

    handle = b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 202


def test_ollama_dispatch_raises_on_cross_harness_value(tmp_path, monkeypatch):
    """A cross-harness value (registered, but not 'local') must fail closed
    rather than silently dispatching via OllamaDriver anyway."""
    monkeypatch.setenv(ENV_VAR, "claude")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(203),
    )

    with pytest.raises(NotImplementedError) as excinfo:
        b.OllamaDriver().dispatch(
            "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )

    message = str(excinfo.value)
    assert ENV_VAR in message
    assert "claude" in message
