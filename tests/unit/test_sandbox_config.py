"""Tests for pipeline.sandbox (PIPELINE_SANDBOX resolution).

Written test-first (TDD): pipeline/sandbox.py does not exist yet on this
branch, so this module is intentionally RED (ImportError at collection)
until the implementation dispatch adds it.

Contract under test (mirrors how PIPELINE_EXEC_DISPATCH fails closed on
unknown values and how app/backend.py get_backend() raises on an unknown
driver name):

* Module constant ``SANDBOX_ENV_VAR == 'PIPELINE_SANDBOX'``.
* ``resolve_sandbox() -> str``:
  - unset / empty / whitespace-only env value -> ``'none'`` (secure
    default: sandboxing ships OFF; operators opt in).
  - ``'none'`` case-insensitive -> ``'none'``.
  - ``'docker'`` case-insensitive, surrounding whitespace tolerated ->
    ``'docker'``.
  - ANY other value -> ``ValueError`` whose message names
    ``PIPELINE_SANDBOX``, the offending value, and the allowed set
    ``['none', 'docker']``. Never a silent fallback to unsandboxed
    execution for a typo'd value.

Per .claude/rules/testing-config-gates.md every test stubs the env var
itself (monkeypatch.delenv for the default cases, monkeypatch.setenv for
the selections) and asserts against its own stub - never against whatever
the live host happens to have set. monkeypatch restores/cleans the env
between tests automatically.
"""

import inspect

import pytest

from pipeline import sandbox as sandbox_module

SANDBOX_ENV_VAR = sandbox_module.SANDBOX_ENV_VAR
resolve_sandbox = sandbox_module.resolve_sandbox

ENV_VAR = "PIPELINE_SANDBOX"


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_sandbox_env_var_constant_value(self):
        """The module must expose SANDBOX_ENV_VAR == 'PIPELINE_SANDBOX'."""
        assert SANDBOX_ENV_VAR == "PIPELINE_SANDBOX"
        assert sandbox_module.SANDBOX_ENV_VAR == "PIPELINE_SANDBOX"

    def test_resolve_sandbox_is_callable(self):
        assert callable(resolve_sandbox)

    def test_resolve_sandbox_takes_no_required_arguments(self):
        """resolve_sandbox() must be callable with no arguments (it reads the
        environment itself)."""
        sig = inspect.signature(resolve_sandbox)
        for name, param in sig.parameters.items():
            assert param.default is not inspect.Parameter.empty, (
                f"resolve_sandbox parameter {name!r} must have a default"
            )

    def test_returns_str(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        result = resolve_sandbox()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Secure default: unset / empty / whitespace-only -> 'none'
# ---------------------------------------------------------------------------


class TestSecureDefault:
    def test_unset_env_var_resolves_to_none(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert resolve_sandbox() == "none"

    def test_empty_string_resolves_to_none(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "")
        assert resolve_sandbox() == "none"

    def test_single_space_resolves_to_none(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, " ")
        assert resolve_sandbox() == "none"

    def test_whitespace_only_resolves_to_none(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, " \t\n ")
        assert resolve_sandbox() == "none"


# ---------------------------------------------------------------------------
# Opt-in selections
# ---------------------------------------------------------------------------


class TestDockerSelection:
    def test_lowercase_docker(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "docker")
        assert resolve_sandbox() == "docker"

    def test_uppercase_docker(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "DOCKER")
        assert resolve_sandbox() == "docker"

    def test_mixed_case_docker(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "Docker")
        assert resolve_sandbox() == "docker"

    def test_docker_with_surrounding_whitespace(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, " Docker ")
        assert resolve_sandbox() == "docker"

    def test_docker_with_tab_whitespace(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "\tDOCKER\t")
        assert resolve_sandbox() == "docker"


class TestNoneSelection:
    def test_lowercase_none(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "none")
        assert resolve_sandbox() == "none"

    def test_uppercase_none(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "NONE")
        assert resolve_sandbox() == "none"

    def test_mixed_case_none(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "None")
        assert resolve_sandbox() == "none"


# ---------------------------------------------------------------------------
# Fail closed on unknown values
# ---------------------------------------------------------------------------


class TestFailClosedOnUnknownValue:
    @pytest.mark.parametrize("bad_value", ["podman", "gvisor", "x"])
    def test_unknown_value_raises_value_error(self, monkeypatch, bad_value):
        monkeypatch.setenv(ENV_VAR, bad_value)
        with pytest.raises(ValueError):
            resolve_sandbox()

    @pytest.mark.parametrize("bad_value", ["podman", "gvisor", "x"])
    def test_error_message_names_env_var(self, monkeypatch, bad_value):
        monkeypatch.setenv(ENV_VAR, bad_value)
        with pytest.raises(ValueError) as excinfo:
            resolve_sandbox()
        assert ENV_VAR in str(excinfo.value), (
            f"ValueError message must name {ENV_VAR}, got: {excinfo.value!r}"
        )

    @pytest.mark.parametrize("bad_value", ["podman", "gvisor", "x"])
    def test_error_message_echoes_offending_value(self, monkeypatch, bad_value):
        monkeypatch.setenv(ENV_VAR, bad_value)
        with pytest.raises(ValueError) as excinfo:
            resolve_sandbox()
        assert bad_value in str(excinfo.value), (
            f"ValueError message must echo the offending value {bad_value!r}, "
            f"got: {excinfo.value!r}"
        )

    @pytest.mark.parametrize("bad_value", ["podman", "gvisor", "x"])
    def test_error_message_lists_allowed_set(self, monkeypatch, bad_value):
        monkeypatch.setenv(ENV_VAR, bad_value)
        with pytest.raises(ValueError) as excinfo:
            resolve_sandbox()
        message = str(excinfo.value)
        assert "none" in message and "docker" in message, (
            "ValueError message must list the allowed set ['none', 'docker'], "
            f"got: {excinfo.value!r}"
        )

    def test_typo_does_not_silently_fall_back_to_none(self, monkeypatch):
        """A near-miss typo must raise, never silently resolve to the
        unsandboxed default."""
        monkeypatch.setenv(ENV_VAR, "docke")
        with pytest.raises(ValueError):
            resolve_sandbox()

    def test_unknown_value_raises_even_when_env_var_was_previously_valid(
        self, monkeypatch
    ):
        """The env var is read at call time: a later invalid value still fails
        closed (no caching of an earlier good resolution)."""
        monkeypatch.setenv(ENV_VAR, "docker")
        assert resolve_sandbox() == "docker"
        monkeypatch.setenv(ENV_VAR, "podman")
        with pytest.raises(ValueError):
            resolve_sandbox()