"""Tests for AiderHarness — the 'aider' adapter registered in app/harness.py.

Every flag asserted here is copied verbatim from docs/specs/AIDER_HARNESS.md
(the aider-chat 0.86.2 --help capture and its contract table) — never from
memory.

CUMULATIVE ARTIFACT RULE (binding this file, per the plan): ``_HARNESSES`` and
``__all__`` in app/harness.py are cumulative artifacts that later stories in
this epic extend. Every assertion below checks ONLY membership/behavior of
what THIS story adds ('aider' / AiderHarness) — never the registry's total
contents, an exact key count, a sorted key list, ``__all__`` equality, or any
hash of app/harness.py. The autouse fixture snapshots and restores the
process-global registry around each test so nothing this suite mutates leaks
into sibling test modules sharing the pytest process.
"""
from __future__ import annotations

import pytest

from app import harness
from app.harness import (
    AiderHarness,
    ClaudeCliHarness,
    HarnessRequest,
    get_harness,
)

# Exact spellings transcribed from docs/specs/AIDER_HARNESS.md.
AIDER_BINARY = "aider"
MSG_FLAG = "--message"
AUTO_CONFIRM_FLAG = "--yes-always"
MODEL_FLAG = "--model"
NO_AUTO_COMMITS_FLAG = "--no-auto-commits"
NO_PRETTY_FLAG = "--no-pretty"


@pytest.fixture(autouse=True)
def _restore_harness_registry():
    """Snapshot and restore the process-global harness registry per test.

    app/harness.py's registry is module state that is never cleared; without
    this fixture a test that registered a probe harness (or a lookup that
    wrongly wrote a junk key) would leak into sibling modules in the same
    pytest process.
    """
    snapshot = dict(harness._HARNESSES)
    yield
    harness._HARNESSES.clear()
    harness._HARNESSES.update(snapshot)


# ---------------------------------------------------------------------------
# Registry + name normalization
# ---------------------------------------------------------------------------


def test_aider_key_is_registered():
    assert "aider" in harness._HARNESSES


def test_get_harness_aider_returns_aider_harness():
    assert isinstance(get_harness("aider"), AiderHarness)


def test_get_harness_normalizes_case_and_whitespace():
    # "AIDER " -> .strip() -> "AIDER" -> .lower() -> "aider" -> hit.
    assert isinstance(get_harness("AIDER "), AiderHarness)


def test_all_exports_aider_harness():
    assert "AiderHarness" in harness.__all__


def test_normalized_lookup_leaves_registry_intact():
    # The registry is process-global persistent state: a normalized lookup
    # must resolve without writing a junk "AIDER " key or disturbing the
    # pre-existing registrations.
    assert isinstance(get_harness("AIDER "), AiderHarness)
    assert isinstance(get_harness("claude"), ClaudeCliHarness)
    assert "AIDER " not in harness._HARNESSES
    # A second identical lookup still resolves (no state was consumed).
    assert isinstance(get_harness("AIDER "), AiderHarness)


# ---------------------------------------------------------------------------
# Representative command
# ---------------------------------------------------------------------------


def test_representative_command():
    command = AiderHarness().build_agent_command(
        HarnessRequest(
            prompt="fix the bug in foo.py",
            system=None,
            model="glm-4.6",
            cwd=".",
            acceptance=["pytest -q tests/unit/test_aider_harness.py"],
            options=None,
        )
    )
    assert command.argv[0] == AIDER_BINARY
    i = command.argv.index(MSG_FLAG)
    assert "fix the bug in foo.py" in command.argv[i + 1]
    assert command.argv[command.argv.index(MODEL_FLAG) + 1] == "glm-4.6"


def test_model_rides_as_its_own_argv_element():
    command = AiderHarness().build_agent_command(
        HarnessRequest(
            prompt="fix the bug in foo.py",
            system=None,
            model="glm-4.6",
            cwd=".",
            options=None,
        )
    )
    j = command.argv.index(MODEL_FLAG)
    assert command.argv[j + 1] == "glm-4.6"


# ---------------------------------------------------------------------------
# Contract flags (exact spellings from docs/specs/AIDER_HARNESS.md)
# ---------------------------------------------------------------------------


def test_auto_confirm_flag_present():
    command = AiderHarness().build_agent_command(
        HarnessRequest(prompt="do it", system=None, model="glm-4.6", cwd=".")
    )
    assert AUTO_CONFIRM_FLAG in command.argv


def test_no_auto_commits_flag_present():
    command = AiderHarness().build_agent_command(
        HarnessRequest(prompt="do it", system=None, model="glm-4.6", cwd=".")
    )
    assert NO_AUTO_COMMITS_FLAG in command.argv


def test_no_pretty_flag_present():
    command = AiderHarness().build_agent_command(
        HarnessRequest(prompt="do it", system=None, model="glm-4.6", cwd=".")
    )
    assert NO_PRETTY_FLAG in command.argv


# ---------------------------------------------------------------------------
# system handling: Aider has no system-prompt flag, so the text is prepended
# to the --message element with a "\n\n" separator (documented compromise).
# ---------------------------------------------------------------------------


def test_system_is_prepended_to_prompt_in_one_element():
    command = AiderHarness().build_agent_command(
        HarnessRequest(
            prompt="fix foo.py",
            system="Always use type hints.",
            model="glm-4.6",
            cwd=".",
        )
    )
    i = command.argv.index(MSG_FLAG)
    assert command.argv[i + 1] == "Always use type hints.\n\nfix foo.py"


def test_no_system_means_prompt_element_is_exactly_the_prompt():
    command = AiderHarness().build_agent_command(
        HarnessRequest(
            prompt="fix foo.py",
            system=None,
            model="glm-4.6",
            cwd=".",
        )
    )
    i = command.argv.index(MSG_FLAG)
    assert command.argv[i + 1] == "fix foo.py"


# ---------------------------------------------------------------------------
# Security: API keys never ride in argv (world-readable via ps); they may
# appear only in HarnessCommand.env, which carries ONLY additional variables.
# ---------------------------------------------------------------------------


def test_api_key_never_appears_in_argv_only_in_env():
    command = AiderHarness().build_agent_command(
        HarnessRequest(
            prompt="do it",
            system=None,
            model="glm-4.6",
            cwd=".",
            options={"openai_api_key": "sk-secret-123"},
        )
    )
    assert not any("sk-secret-123" in element for element in command.argv)
    assert command.env == {"OPENAI_API_KEY": "sk-secret-123"}
    assert "PATH" not in command.env


def test_second_call_without_key_has_no_key_state_left_behind():
    adapter = AiderHarness()
    adapter.build_agent_command(
        HarnessRequest(
            prompt="do it",
            system=None,
            model="glm-4.6",
            cwd=".",
            options={"openai_api_key": "sk-secret-123"},
        )
    )
    command_b = adapter.build_agent_command(
        HarnessRequest(prompt="again", system=None, model="glm-4.6", cwd=".")
    )
    assert "sk-secret-123" not in command_b.env.values()
    assert "OPENAI_API_KEY" not in command_b.env
    assert command_b.env == {}


def test_anthropic_key_maps_to_provider_native_env_var():
    command = AiderHarness().build_agent_command(
        HarnessRequest(
            prompt="do it",
            system=None,
            model="glm-4.6",
            cwd=".",
            options={"anthropic_api_key": "sk-ant-secret"},
        )
    )
    assert not any("sk-ant-secret" in element for element in command.argv)
    assert command.env == {"ANTHROPIC_API_KEY": "sk-ant-secret"}


def test_env_holds_only_additions_never_inherited_environment():
    command = AiderHarness().build_agent_command(
        HarnessRequest(prompt="do it", system=None, model="glm-4.6", cwd=".")
    )
    assert "PATH" not in command.env
    assert command.env == {}


# ---------------------------------------------------------------------------
# Negative: empty/whitespace-only prompt or model fails loudly.
# ---------------------------------------------------------------------------


def test_empty_prompt_raises_value_error_naming_the_field():
    with pytest.raises(ValueError, match="prompt"):
        AiderHarness().build_agent_command(
            HarnessRequest(prompt="", system=None, model="glm-4.6", cwd=".")
        )


def test_whitespace_prompt_raises_value_error_naming_the_field():
    with pytest.raises(ValueError, match="prompt"):
        AiderHarness().build_agent_command(
            HarnessRequest(prompt="   ", system=None, model="glm-4.6", cwd=".")
        )


def test_rejected_call_leaves_no_state_behind():
    adapter = AiderHarness()
    with pytest.raises(ValueError, match="prompt"):
        adapter.build_agent_command(
            HarnessRequest(prompt="  ", system=None, model="glm-4.6", cwd=".")
        )
    command = adapter.build_agent_command(
        HarnessRequest(prompt="fix it", system=None, model="glm-4.6", cwd=".")
    )
    assert command.argv[0] == AIDER_BINARY
    i = command.argv.index(MSG_FLAG)
    assert command.argv[i + 1] == "fix it"


def test_empty_model_raises_value_error_naming_the_field():
    with pytest.raises(ValueError, match="model"):
        AiderHarness().build_agent_command(
            HarnessRequest(prompt="do it", system=None, model="", cwd=".")
        )


def test_whitespace_model_raises_value_error_naming_the_field():
    with pytest.raises(ValueError, match="model"):
        AiderHarness().build_agent_command(
            HarnessRequest(prompt="do it", system=None, model="  ", cwd=".")
        )


def test_unknown_but_nonempty_model_passes_through_unchanged():
    # The harness is a pure argv builder: validating vendor model names is not
    # its job, and the registry-name normalization (.strip().lower()) must
    # never be applied to the model — "GLM-4" silently becoming "glm-4" would
    # request the wrong vendor model.
    command = AiderHarness().build_agent_command(
        HarnessRequest(
            prompt="do it",
            system=None,
            model="Qwen2.5-Coder-7B",
            cwd=".",
        )
    )
    assert command.argv[command.argv.index(MODEL_FLAG) + 1] == "Qwen2.5-Coder-7B"


def test_aider_probe_argv_matches_spec_probe_order():
    # Exact-equality pin of the FULL argv in the spec's probe order (all 8
    # contract flags, copied verbatim and in order from
    # docs/specs/AIDER_HARNESS.md — its "Exit behavior" probe plus the
    # --no-fancy-input contract-table row). Equality, not membership, is
    # what gives this test its teeth: a dropped flag fails on length
    # (7 != 8), a swapped pair fails at the first differing index, and an
    # acceptance leak (--test/--test-cmd) fails on length (9 != 8) — none of
    # which per-flag membership assertions can see. The request carries a
    # non-None acceptance on purpose: Aider has no oracle concept, so the
    # expected argv contains no --test/--test-cmd, pinning the
    # acceptance-ignored contract in the same assertion.
    command = AiderHarness().build_agent_command(
        HarnessRequest(
            prompt="fix the bug in foo.py",
            system=None,
            model="glm-4.6",
            cwd=".",
            acceptance=["pytest -q tests/unit/test_aider_harness.py"],
            options=None,
        )
    )
    expected = [
        "aider",
        "--model", "glm-4.6",
        "--message", "fix the bug in foo.py",
        "--yes-always",
        "--no-auto-commits",
        "--no-dirty-commits",
        "--no-pretty",
        "--no-stream",
        "--no-fancy-input",
    ]
    assert command.argv == expected
