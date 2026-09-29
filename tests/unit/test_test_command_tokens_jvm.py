"""The static test-command token tuples must recognise the JVM lifecycle
commands (plus dotnet/pnpm) so the harness counts them as "the agent ran the
tests". Asserted by membership only - never the tuple's full contents.
"""

import json

import pytest

from pipeline import local_agent_common, rebrief

# Commands this story must make recognisable, per module. "gradle test" is
# already present in local_agent_common's markers, so it is only listed for
# rebrief.
REBRIEF_COMMANDS = (
    "mvn verify",
    "mvn -B verify",
    "./mvnw -B verify",
    "gradle check",
    "./gradlew check",
    "gradle test",
    "dotnet test",
    "pnpm test",
)
LOCAL_AGENT_COMMANDS = (
    "mvn verify",
    "mvn -B verify",
    "./mvnw -B verify",
    "gradle check",
    "./gradlew check",
    "dotnet test",
    "pnpm test",
)


def _recognised(command: str, tokens) -> bool:
    return any(token in command for token in tokens)


@pytest.mark.parametrize("command", REBRIEF_COMMANDS)
def test_rebrief_tokens_recognise_new_commands(command):
    assert _recognised(command, rebrief._TEST_COMMAND_TOKENS)


@pytest.mark.parametrize("command", LOCAL_AGENT_COMMANDS)
def test_local_agent_common_markers_recognise_new_commands(command):
    assert _recognised(command, local_agent_common._TEST_COMMAND_MARKERS)


def test_a_non_test_maven_goal_is_not_recognised():
    assert not _recognised("mvn package", rebrief._TEST_COMMAND_TOKENS)
    assert not _recognised("mvn package", local_agent_common._TEST_COMMAND_MARKERS)


def test_existing_entries_are_kept():
    assert "pytest" in rebrief._TEST_COMMAND_TOKENS
    assert "pytest" in local_agent_common._TEST_COMMAND_MARKERS


def _write_agent_log(worktree, command: str) -> None:
    (worktree / "agent.log").write_text(f"[boot] start\n[step 1] bash: {command}\n")


def test_rebrief_log_facts_counts_wrapper_verify_as_a_test_run(tmp_path):
    _write_agent_log(tmp_path, "./mvnw -B verify")
    facts = rebrief._log_facts(tmp_path)
    assert not any("NEVER RAN THE TESTS" in fact for fact in facts)


def test_rebrief_log_facts_still_flags_a_non_test_command(tmp_path):
    _write_agent_log(tmp_path, "ls -la")
    facts = rebrief._log_facts(tmp_path)
    assert any("NEVER RAN THE TESTS" in fact for fact in facts)


def _bash_block(command: str, result: str = "BUILD SUCCESS"):
    return [[
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "bash", "arguments": json.dumps({"command": command})}}
        ]},
        {"role": "tool", "content": result},
    ]]


def test_local_agent_common_digest_surfaces_wrapper_verify_result():
    digest = local_agent_common._dropped_span_digest(_bash_block("./mvnw -B verify"))
    assert "Last test result seen there" in digest


def test_local_agent_common_digest_ignores_non_test_commands():
    digest = local_agent_common._dropped_span_digest(_bash_block("ls -la"))
    assert "Last test result seen there" not in digest
