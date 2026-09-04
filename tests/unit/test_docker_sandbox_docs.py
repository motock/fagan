"""Tests for docs/specs/DOCKER_SANDBOX.md, the doc-only deliverable of the
'Wire the docker sandbox into spawn_harness's local branch' follow-up story.

This story is documentation-only: it must not touch pipeline/sandbox.py,
pipeline/execution.py, or any other production code. These tests assert the
CONTENT of docs/specs/DOCKER_SANDBOX.md against the ACTUAL behavior already
implemented in pipeline/sandbox.py and pipeline/execution.py (read directly
below as ground truth) — never against what was merely planned.

The doc file does not exist yet, so every test in this module is RED until
the doc is written. That is the correct starting state for this dispatch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO_ROOT / "docs" / "specs" / "DOCKER_SANDBOX.md"
SANDBOX_SRC_PATH = REPO_ROOT / "pipeline" / "sandbox.py"
EXECUTION_SRC_PATH = REPO_ROOT / "pipeline" / "execution.py"
REMOTE_EXECUTION_DOC = "docs/specs/REMOTE_EXECUTION.md"
REMOTE_EXECUTION_DOC_PATH = REPO_ROOT / "docs" / "specs" / "REMOTE_EXECUTION.md"
TESTING_CONFIG_GATES_RULE = ".claude/rules/testing-config-gates.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc_text() -> str:
    assert DOC_PATH.exists(), f"docs/specs/DOCKER_SANDBOX.md missing: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def _remote_execution_doc_text() -> str:
    assert REMOTE_EXECUTION_DOC_PATH.exists(), (
        f"{REMOTE_EXECUTION_DOC_PATH} missing: docs/specs/DOCKER_SANDBOX.md "
        "§8 links to it as a real spec, but it does not exist in the repo"
    )
    return REMOTE_EXECUTION_DOC_PATH.read_text(encoding="utf-8")


def _sandbox_src() -> str:
    return SANDBOX_SRC_PATH.read_text(encoding="utf-8")


def _execution_src() -> str:
    return EXECUTION_SRC_PATH.read_text(encoding="utf-8")


def _lines_mentioning(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


# ---------------------------------------------------------------------------
# File exists (baseline)
# ---------------------------------------------------------------------------

class TestDocExists:
    def test_doc_file_exists(self):
        _doc_text()

    def test_doc_is_not_empty(self):
        assert len(_doc_text().strip()) > 0


# ---------------------------------------------------------------------------
# Ground truth sanity: confirm the code this doc must describe hasn't moved
# out from under us (guards against a stale/misdiagnosed dispatch, not the
# doc itself).
# ---------------------------------------------------------------------------

class TestGroundTruthCodeShape:
    def test_sandbox_module_defines_resolve_sandbox(self):
        assert "def resolve_sandbox()" in _sandbox_src()

    def test_sandbox_module_defines_docker_binary_available(self):
        assert "def docker_binary_available()" in _sandbox_src()

    def test_sandbox_module_defines_build_docker_command(self):
        assert "def build_docker_command(" in _sandbox_src()

    def test_execution_module_wires_sandbox_into_spawn_harness(self):
        src = _execution_src()
        assert "resolve_sandbox" in src
        assert "docker_binary_available" in src
        assert "build_docker_command" in src
        assert "def spawn_harness(" in src


# ---------------------------------------------------------------------------
# Point 1 — the opt-in contract
# ---------------------------------------------------------------------------

class TestOptInContract:
    def test_doc_names_the_env_var(self):
        assert "PIPELINE_SANDBOX" in _doc_text()

    def test_doc_states_allowed_values_none_and_docker(self):
        text = _doc_text().lower()
        assert "none" in text
        assert "docker" in text

    def test_doc_states_default_is_none(self):
        text = _doc_text().lower()
        assert re.search(r"default[^.\n]{0,40}\bnone\b", text), (
            "doc must state the default value of PIPELINE_SANDBOX is 'none'"
        )

    def test_doc_states_sandboxing_ships_off(self):
        text = _doc_text().lower()
        assert "off" in text, (
            "doc must state sandboxing ships OFF by default"
        )

    def test_doc_matches_code_default_none_behavior(self):
        # Ground truth: resolve_sandbox() returns "none" when unset/empty.
        src = _sandbox_src()
        assert 'return "none"' in src
        assert "PIPELINE_SANDBOX" in _doc_text()


# ---------------------------------------------------------------------------
# Point 2 — PIPELINE_SANDBOX_IMAGE required, fail-closed ValueError
# ---------------------------------------------------------------------------

class TestSandboxImageRequired:
    def test_doc_names_the_image_env_var(self):
        assert "PIPELINE_SANDBOX_IMAGE" in _doc_text()

    def test_doc_states_image_required_when_docker_selected(self):
        text = _doc_text().lower()
        assert "required" in text, (
            "doc must state PIPELINE_SANDBOX_IMAGE is required when docker "
            "is selected"
        )

    def test_doc_pairs_missing_image_with_value_error(self):
        text = _doc_text()
        idx = text.find("PIPELINE_SANDBOX_IMAGE")
        assert idx != -1
        window = text[max(0, idx - 400) : idx + 400]
        assert "ValueError" in window, (
            "doc must state that a missing PIPELINE_SANDBOX_IMAGE raises "
            "ValueError near where the variable is discussed"
        )

    def test_doc_matches_code_image_error_behavior(self):
        # Ground truth: build_docker_command raises ValueError when the
        # image env var is unset/empty.
        src = _sandbox_src()
        assert "PIPELINE_SANDBOX_IMAGE" in src
        assert "raise ValueError" in src


# ---------------------------------------------------------------------------
# Point 3 — container shape
# ---------------------------------------------------------------------------

class TestContainerShape:
    def test_doc_describes_worktree_volume_mount(self):
        text = _doc_text().lower()
        assert "volume" in text or "-v" in _doc_text(), (
            "doc must describe the worktree volume mount"
        )

    def test_doc_states_identical_host_path(self):
        text = _doc_text().lower()
        assert "identical" in text or "same path" in text or "same host path" in text, (
            "doc must state the worktree is mounted at its identical host "
            "path inside the container"
        )

    def test_doc_mentions_workdir_flag(self):
        assert "--workdir" in _doc_text(), (
            "doc must mention the --workdir flag is set to the worktree path"
        )

    def test_doc_states_argv_appended_verbatim(self):
        text = _doc_text().lower()
        assert "verbatim" in text, (
            "doc must state the agent argv is appended verbatim"
        )

    def test_doc_matches_code_command_shape(self):
        # Ground truth shape from build_docker_command's docstring/impl.
        src = _sandbox_src()
        assert '"docker"' in src
        assert '"run"' in src
        assert '"--rm"' in src
        assert '"-v"' in src
        assert '"--workdir"' in src


# ---------------------------------------------------------------------------
# Point 4 — env passthrough policy
# ---------------------------------------------------------------------------

class TestEnvPassthroughPolicy:
    def test_doc_states_deny_by_default(self):
        text = _doc_text().lower()
        assert "deny-by-default" in text or "deny by default" in text, (
            "doc must state the env passthrough policy is deny-by-default"
        )

    def test_doc_names_local_agent_prefix(self):
        assert "LOCAL_AGENT_" in _doc_text(), (
            "doc must name the LOCAL_AGENT_ forwarded prefix exactly"
        )

    def test_doc_names_pipeline_prefix(self):
        assert "PIPELINE_" in _doc_text(), (
            "doc must name the PIPELINE_ forwarded prefix exactly"
        )

    def test_doc_states_everything_else_not_forwarded(self):
        text = _doc_text().lower()
        assert "not forwarded" in text or "never forwarded" in text, (
            "doc must state that every other host env var is deliberately "
            "not forwarded"
        )

    def test_doc_mentions_credentials_not_forwarded(self):
        text = _doc_text().lower()
        assert "credential" in text, (
            "doc must call out credentials specifically as NOT forwarded, "
            "per the data-minimization framing"
        )

    def test_doc_states_operator_must_add_other_vars_explicitly(self):
        text = _doc_text().lower()
        assert "explicit" in text, (
            "doc must state which vars an operator must add explicitly if "
            "a harness needs them outside the LOCAL_AGENT_/PIPELINE_ prefixes"
        )

    def test_doc_matches_code_prefix_tuple(self):
        # Ground truth: build_docker_command and spawn_harness both gate on
        # this exact prefix tuple.
        src = _sandbox_src() + _execution_src()
        assert '"LOCAL_AGENT_"' in src
        assert '"PIPELINE_"' in src


# ---------------------------------------------------------------------------
# Point 5 — fail-closed behavior matrix
# ---------------------------------------------------------------------------

class TestFailClosedBehaviorMatrix:
    def test_unknown_sandbox_value_raises_value_error(self):
        text = _doc_text()
        idx = text.find("PIPELINE_SANDBOX")
        assert idx != -1
        # Search the whole doc for a ValueError mention tied to an unknown
        #/invalid PIPELINE_SANDBOX value.
        lowered = text.lower()
        assert "valueerror" in lowered
        assert "unknown" in lowered or "invalid" in lowered, (
            "doc must describe an unknown/invalid PIPELINE_SANDBOX value "
            "raising ValueError"
        )

    def test_docker_binary_absent_raises_runtime_error(self):
        text = _doc_text()
        assert "RuntimeError" in text, (
            "doc must state that an absent docker binary raises RuntimeError"
        )

    def test_docker_binary_absent_dispatch_refused(self):
        text = _doc_text().lower()
        assert "refus" in text, (
            "doc must state dispatch is refused when the docker binary is "
            "absent"
        )

    def test_docker_binary_absent_no_unsandboxed_fallback(self):
        text = _doc_text().lower()
        assert re.search(r"no[^.\n]{0,40}unsandboxed[^.\n]{0,40}fallback", text) or (
            "unsandboxed" in text and "fallback" in text and "no" in text
        ), (
            "doc must explicitly state there is NO unsandboxed fallback "
            "when the docker binary is absent"
        )

    def test_image_unset_raises_value_error(self):
        # Already covered in TestSandboxImageRequired, but the matrix
        # section itself must also carry this row explicitly.
        text = _doc_text()
        assert "PIPELINE_SANDBOX_IMAGE" in text
        assert "ValueError" in text

    def test_matrix_matches_code_raise_types(self):
        # Ground truth: exact exception types raised by the code.
        sandbox_src = _sandbox_src()
        execution_src = _execution_src()
        assert "raise ValueError" in sandbox_src
        assert "raise RuntimeError" in execution_src
        assert "REFUSED" in execution_src


# ---------------------------------------------------------------------------
# Point 6 — what is NOT isolated
# ---------------------------------------------------------------------------

class TestWhatIsNotIsolated:
    def test_doc_has_a_not_isolated_section(self):
        text = _doc_text().lower()
        assert "not isolated" in text, (
            "doc must have an explicit section on what is NOT isolated"
        )

    def test_doc_calls_out_network_access(self):
        assert "network" in _doc_text().lower()

    def test_doc_calls_out_host_resources(self):
        text = _doc_text().lower()
        assert "host resource" in text or "cpu" in text or "memory" in text, (
            "doc must call out host resource access (CPU/memory/etc.) as "
            "not isolated"
        )

    def test_doc_states_only_worktree_mount_is_scoped(self):
        text = _doc_text().lower()
        assert "outside the worktree" in text or "beyond the worktree" in text, (
            "doc must state that anything outside the worktree mount is "
            "not isolated"
        )

    def test_doc_warns_against_treating_it_as_a_hard_security_boundary(self):
        text = _doc_text().lower()
        assert "security boundary" in text, (
            "doc must explicitly warn readers not to mistake this for a "
            "hard security boundary"
        )


# ---------------------------------------------------------------------------
# Point 7 — live-host validation note
# ---------------------------------------------------------------------------

class TestLiveHostValidationNote:
    def test_doc_references_testing_config_gates_rule(self):
        assert TESTING_CONFIG_GATES_RULE in _doc_text(), (
            "doc must reference .claude/rules/testing-config-gates.md as "
            "the source of the live-host validation requirement"
        )

    def test_doc_states_suite_mocks_docker_binary(self):
        text = _doc_text().lower()
        assert "mock" in text, (
            "doc must state the test suite mocks the docker binary"
        )

    def test_doc_gives_docker_absent_one_time_check(self):
        text = _doc_text().lower()
        assert "docker absent" in text or "without docker" in text or (
            "docker" in text and "absent" in text
        ), "doc must describe the one-time check with docker absent"
        assert "runtimeerror" in text

    def test_doc_states_confirm_nothing_was_exec_d(self):
        text = _doc_text().lower()
        assert "nothing" in text and ("exec" in text), (
            "doc must state the operator confirms nothing was exec'd when "
            "docker is absent"
        )

    def test_doc_gives_docker_present_one_time_check(self):
        text = _doc_text().lower()
        assert "container starts" in text or "container start" in text, (
            "doc must describe the one-time check with docker present: "
            "the container starts"
        )

    def test_doc_states_agent_works_in_mounted_worktree(self):
        text = _doc_text().lower()
        assert "mounted worktree" in text, (
            "doc must state the agent works in the mounted worktree during "
            "the live-host check"
        )

    def test_doc_states_print_measured_docker_version(self):
        text = _doc_text().lower()
        assert "docker version" in text or "docker --version" in text, (
            "doc must instruct the operator to print the measured docker "
            "version next to the expectation"
        )
        assert "measured" in text, (
            "doc must use language consistent with measuring a real value, "
            "per testing-config-gates.md's gate-validation guidance"
        )


# ---------------------------------------------------------------------------
# Point 8 — relationship to remote execution
# ---------------------------------------------------------------------------

class TestRelationshipToRemoteExecution:
    def test_doc_states_local_spawn_branch_only(self):
        text = _doc_text().lower()
        assert "local" in text
        assert "spawn" in text or "local branch" in text, (
            "doc must state docker sandboxing applies to the local spawn "
            "branch only"
        )

    def test_doc_references_remote_execution_doc(self):
        assert REMOTE_EXECUTION_DOC in _doc_text(), (
            "doc must point readers to docs/specs/REMOTE_EXECUTION.md for "
            "remote/ssh execution rather than duplicating that content"
        )

    def test_doc_does_not_duplicate_remote_exec_dispatch_var(self):
        # The remote-exec seam's own env var (PIPELINE_EXEC_DISPATCH) may be
        # referenced by name for context, but this doc must not attempt to
        # redefine/duplicate its resolution semantics with a competing
        # 'defaults to' claim.
        text = _doc_text()
        # Not asserting absence of the var name (a passing mention/pointer
        # is fine and expected), only that the doc doesn't claim ownership
        # of its default-resolution semantics.
        assert not re.search(
            r"PIPELINE_EXEC_DISPATCH[^.\n]{0,60}defaults to", text, re.IGNORECASE
        ), (
            "doc must not restate PIPELINE_EXEC_DISPATCH's own default "
            "resolution semantics — that belongs to the remote-exec plan's "
            "docs, not this one"
        )


# ---------------------------------------------------------------------------
# Success criteria — every env var named in the doc matches the code exactly
# ---------------------------------------------------------------------------

class TestEnvVarNamesMatchCode:
    @pytest.mark.parametrize(
        "var_name",
        ["PIPELINE_SANDBOX", "PIPELINE_SANDBOX_IMAGE"],
    )
    def test_var_appears_in_both_doc_and_sandbox_module(self, var_name):
        assert var_name in _doc_text(), f"{var_name} must appear in the doc"
        assert var_name in _sandbox_src(), (
            f"{var_name} must appear in pipeline/sandbox.py (ground truth "
            "check — if this fails, the code moved and the doc brief is "
            "stale, not a doc bug)"
        )

    @pytest.mark.parametrize("prefix", ["LOCAL_AGENT_", "PIPELINE_"])
    def test_prefix_appears_in_both_doc_and_code(self, prefix):
        assert prefix in _doc_text(), f"{prefix} prefix must appear in the doc"
        combined_src = _sandbox_src() + _execution_src()
        assert prefix in combined_src, (
            f"{prefix} prefix must appear in pipeline/sandbox.py or "
            "pipeline/execution.py (ground truth check)"
        )


# ---------------------------------------------------------------------------
# Review regression — Blocking #1: §4 documents an env-passthrough escape
# hatch that the code does not implement. spawn_harness (pipeline/
# execution.py) filters the caller's `env` through the identical
# ("LOCAL_AGENT_", "PIPELINE_") prefix tuple *before* build_docker_command
# filters again — a non-prefixed key added to the `env` dict passed to
# spawn_harness is silently dropped by both layers. There is no channel to
# forward it, so the doc's current instruction to "add it explicitly to the
# `env` dictionary passed to `spawn_harness`" is false.
# ---------------------------------------------------------------------------

class TestEnvPassthroughEscapeHatchAccuracy:
    def test_doc_states_the_allowlist_is_absolute(self):
        text = _doc_text().lower()
        assert (
            "allowlist is absolute" in text
            or "no other escape hatch" in text
            or "silently dropped" in text
        ), (
            "doc §4 must state that the LOCAL_AGENT_/PIPELINE_ prefix "
            "allowlist is absolute: the only way to forward a variable "
            "into the sandboxed container is to name it with a "
            "LOCAL_AGENT_ or PIPELINE_ prefix. There is no escape hatch "
            "via the `env` dict passed to spawn_harness — spawn_harness "
            "re-applies the identical prefix filter before "
            "build_docker_command filters again"
        )

    def test_doc_no_longer_claims_env_dict_addition_forwards_the_var(self):
        text = _doc_text()
        assert not re.search(
            r"must add it explicitly to the `env` dictionary passed to"
            r"\s*\n?\s*`spawn_harness`",
            text,
        ), (
            "doc §4 must not instruct operators to add a non-allowlisted "
            "variable to the `env` dictionary passed to `spawn_harness` as "
            "a way to forward it into the container — spawn_harness "
            "silently drops any key that doesn't start with "
            "LOCAL_AGENT_/PIPELINE_ before it ever reaches "
            "build_docker_command, so following this instruction produces "
            "a silent failure on a security control"
        )

    def test_spawn_harness_actually_drops_non_allowlisted_env_vars(
        self, monkeypatch, tmp_path
    ):
        """Ground truth proving §4's documented workaround does not work:
        exercise the real spawn_harness -> build_docker_command path with
        a non-allowlisted variable added to the `env` dict, exactly as §4
        currently instructs operators to do, and confirm it never reaches
        the built docker command.
        """
        import pipeline.execution as execution_module

        monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
        monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", "test-image")
        monkeypatch.setattr(
            execution_module, "docker_binary_available", lambda: True
        )

        captured = {}

        class _FakeProc:
            pid = 4242

        def _fake_popen(cmd, cwd, env, stdout, stderr):
            captured["cmd"] = cmd
            return _FakeProc()

        monkeypatch.setattr(execution_module.subprocess, "Popen", _fake_popen)

        log_path = tmp_path / "harness.log"
        execution_module.spawn_harness(
            ["echo", "hi"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            env={"NOT_ALLOWLISTED_SECRET": "leak-me"},
        )

        built_cmd = captured["cmd"]
        assert not any("NOT_ALLOWLISTED_SECRET" in part for part in built_cmd), (
            "spawn_harness must silently drop a non-allowlisted variable "
            "added to its `env` dict rather than forwarding it via `-e` — "
            "this is the exact workaround docs/specs/DOCKER_SANDBOX.md §4 "
            "currently instructs operators to use, and it does not work"
        )


# ---------------------------------------------------------------------------
# Review regression — Blocking #2: §8 links to docs/specs/REMOTE_EXECUTION.md,
# which does not exist anywhere in the repo. pipeline/execution.py shows ssh
# mode genuinely raises NotImplementedError — the missing spec must exist as
# a stub stating that plainly.
# ---------------------------------------------------------------------------

class TestRemoteExecutionStubDoc:
    def test_remote_execution_doc_exists(self):
        assert REMOTE_EXECUTION_DOC_PATH.exists(), (
            f"{REMOTE_EXECUTION_DOC_PATH} must exist: "
            "docs/specs/DOCKER_SANDBOX.md §8 links to it as a real spec, "
            "but it is missing from the repo"
        )

    def test_remote_execution_doc_is_not_empty(self):
        assert len(_remote_execution_doc_text().strip()) > 0

    def test_remote_execution_doc_states_not_implemented(self):
        text = _remote_execution_doc_text().lower()
        assert "not implemented" in text, (
            "docs/specs/REMOTE_EXECUTION.md must plainly state that "
            "remote/ssh execution is not yet implemented"
        )

    def test_remote_execution_doc_quotes_the_exact_not_implemented_error_message(self):
        text = _remote_execution_doc_text()
        # Ground truth: the exact message spawn_harness raises for ssh mode
        # (pipeline/execution.py's _SSH_NOT_IMPLEMENTED_MSG).
        assert "ssh execution is not implemented yet (B1 later story)" in text, (
            "docs/specs/REMOTE_EXECUTION.md must quote the exact "
            "NotImplementedError message raised by spawn_harness for ssh "
            "mode, so the stub stays accurate as ground truth"
        )

    def test_remote_execution_doc_names_the_trigger_env_var_pattern(self):
        text = _remote_execution_doc_text()
        assert "PIPELINE_EXEC_" in text, (
            "docs/specs/REMOTE_EXECUTION.md must name the "
            "PIPELINE_EXEC_{ROLE} trigger that resolves to ssh mode via "
            "resolve_execution_mode (pipeline/execution.py)"
        )

    def test_remote_execution_doc_does_not_duplicate_dispatch_default_claim(self):
        # Mirrors TestRelationshipToRemoteExecution's non-duplication guard
        # on the sandbox doc: the stub must not restate
        # PIPELINE_EXEC_DISPATCH's own default-resolution semantics with a
        # competing 'defaults to' claim.
        text = _remote_execution_doc_text()
        assert not re.search(
            r"PIPELINE_EXEC_DISPATCH[^.\n]{0,60}defaults to", text, re.IGNORECASE
        ), (
            "docs/specs/REMOTE_EXECUTION.md must not restate "
            "PIPELINE_EXEC_DISPATCH's own default-resolution semantics"
        )
