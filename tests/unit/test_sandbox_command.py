"""Tests for pipeline.sandbox's docker command construction seam.

Written test-first (TDD): this story APPENDS ``docker_binary_available``
and ``build_docker_command`` to the existing pipeline/sandbox.py (which
already ships ``resolve_sandbox`` — that contract is covered by
test_sandbox_config.py and is NOT re-graded here). The two new names do
not exist yet, so this module is intentionally RED (AttributeError at
collection) until the implementation dispatch adds them.

Contract under test:

* ``docker_binary_available() -> bool`` — a pure probe returning
  ``shutil.which('docker') is not None``. No version negotiation, no
  side effects; the lookup happens at call time, not import time.

* ``build_docker_command(worktree, argv, env=None) -> list[str]`` — a
  pure function returning the container argv, in exactly this shape::

      ['docker', 'run', '--rm', '-v', f'{worktree}:{worktree}',
       '--workdir', worktree, *env_flags, image, *argv]

  - The worktree is volume-mounted AT ITS HOST PATH (identical path
    inside the container), so cwd-relative logic needs no remapping.
  - Env passthrough is DENY-BY-DEFAULT: only keys whose name starts
    with ``'LOCAL_AGENT_'`` or ``'PIPELINE_'`` (the two prefixes this
    repo's agent contract actually uses) are forwarded, each as
    ``['-e', f'{key}={value}']``. Every other host env var (PATH, HOME,
    ...) is deliberately NOT forwarded — data minimization, Secure by
    Design.
  - The image comes from ``os.environ['PIPELINE_SANDBOX_IMAGE']``; when
    that var is unset or empty, raise ``ValueError`` whose message
    names the var — fail closed rather than guessing an image.

Mocking policy (per the story brief): mock ONLY at the true external
boundary — monkeypatch ``shutil.which`` for the binary probe and
monkeypatch ``os.environ`` for the image var. No test in this module
requires docker to be installed and none invokes a real docker binary.

Note on ``PIPELINE_SANDBOX``: tests/unit/conftest.py strips every
PIPELINE_*/LOCAL_AGENT_* var before each test, so the happy paths below
run with sandbox mode resolving to ``'none'`` — and must STILL build the
full docker argv. Command construction is unconditional; only the
missing-image failure is graded (and those tests set
``PIPELINE_SANDBOX=docker`` explicitly, so an implementation that gates
the raise on sandbox mode passes too). Because conftest also strips
``PIPELINE_SANDBOX_IMAGE`` itself, every success-path test stubs the
image var via ``_set_image`` — never asserting against the live host's
value (per .claude/rules/testing-config-gates.md).
"""

import inspect
import os
import shutil

import pytest

from pipeline import sandbox as sandbox_module

# AttributeError at collection until the implementation dispatch appends
# these two functions to pipeline/sandbox.py — the intended RED state.
docker_binary_available = sandbox_module.docker_binary_available
build_docker_command = sandbox_module.build_docker_command

IMAGE_VAR = "PIPELINE_SANDBOX_IMAGE"
TEST_IMAGE = "registry.example/acme/sandbox:v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_image(monkeypatch, value=TEST_IMAGE):
    """Stub the image var itself — never assert against the live host's
    value (per .claude/rules/testing-config-gates.md)."""
    if value is None:
        monkeypatch.delenv(IMAGE_VAR, raising=False)
    else:
        monkeypatch.setenv(IMAGE_VAR, value)


def _patch_which(monkeypatch, result):
    """Point ``shutil.which`` at a stub and record how it was probed.

    Patches the attribute on the shutil module itself (plus a module-level
    ``which`` name if the implementation bound one via ``from shutil import
    which``), so the stub is honored whichever import style is used.
    Returns the list of probe names, which also proves the lookup happened
    at call time through the patched boundary (a version-negotiating or
    import-time-cached implementation never records a call).
    """
    calls = []

    def fake_which(name, *args, **kwargs):
        calls.append(name)
        return result

    monkeypatch.setattr(shutil, "which", fake_which)
    if hasattr(sandbox_module, "which"):
        monkeypatch.setattr(sandbox_module, "which", fake_which)
    return calls


def _docker_prefix(worktree):
    """The fixed flag block that must open every built argv."""
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{worktree}:{worktree}",
        "--workdir",
        worktree,
    ]


def _env_flags(result):
    """Extract each 'K=V' value that follows a '-e' flag, in argv order."""
    pairs = []
    i = 0
    while i < len(result):
        if result[i] == "-e":
            assert i + 1 < len(result), "dangling '-e' flag with no value"
            pairs.append(result[i + 1])
            i += 2
        else:
            i += 1
    return pairs


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_docker_binary_available_takes_no_required_arguments(self):
        """docker_binary_available() must be callable with no arguments (it
        probes shutil.which itself)."""
        sig = inspect.signature(docker_binary_available)
        for name, param in sig.parameters.items():
            assert param.default is not inspect.Parameter.empty, (
                f"docker_binary_available parameter {name!r} must have a default"
            )

    def test_build_docker_command_signature(self):
        """build_docker_command(worktree, argv, env=None) — env optional and
        defaulting to None."""
        sig = inspect.signature(build_docker_command)
        assert list(sig.parameters) == ["worktree", "argv", "env"]
        assert sig.parameters["env"].default is None


# ---------------------------------------------------------------------------
# docker_binary_available — pure probe over shutil.which('docker')
# ---------------------------------------------------------------------------


class TestDockerBinaryAvailable:
    def test_true_when_which_finds_docker(self, monkeypatch):
        calls = _patch_which(monkeypatch, "/usr/bin/docker")
        assert docker_binary_available() is True
        assert calls == ["docker"]

    def test_false_when_which_returns_none(self, monkeypatch):
        calls = _patch_which(monkeypatch, None)
        assert docker_binary_available() is False
        assert calls == ["docker"]

    def test_returns_real_bool_not_truthiness(self, monkeypatch):
        _patch_which(monkeypatch, "/usr/bin/docker")
        assert isinstance(docker_binary_available(), bool)
        _patch_which(monkeypatch, None)
        assert isinstance(docker_binary_available(), bool)

    def test_empty_string_from_which_still_counts_as_available(self, monkeypatch):
        """The contract is ``shutil.which('docker') is not None`` — presence,
        not truthiness."""
        _patch_which(monkeypatch, "")
        assert docker_binary_available() is True

    def test_probe_is_repeatable_and_side_effect_free(self, monkeypatch):
        calls = _patch_which(monkeypatch, None)
        before = dict(os.environ)
        first = docker_binary_available()
        second = docker_binary_available()
        assert first is False
        assert second is False
        assert dict(os.environ) == before
        assert calls == ["docker", "docker"]


# ---------------------------------------------------------------------------
# build_docker_command — happy path / exact argv shape
# ---------------------------------------------------------------------------


class TestBuildDockerCommandHappyPath:
    def test_exact_argv_success_criteria(self, monkeypatch):
        """The story's success-criteria example, asserted element for
        element: PATH and OTHER are NOT forwarded."""
        _set_image(monkeypatch)
        result = build_docker_command(
            "/wt/x",
            ["python", "agent.py"],
            {"LOCAL_AGENT_MODEL": "m", "PATH": "/usr/bin", "OTHER": "v"},
        )
        assert result == [
            "docker",
            "run",
            "--rm",
            "-v",
            "/wt/x:/wt/x",
            "--workdir",
            "/wt/x",
            "-e",
            "LOCAL_AGENT_MODEL=m",
            TEST_IMAGE,
            "python",
            "agent.py",
        ]

    def test_env_none_produces_zero_e_flags(self, monkeypatch):
        _set_image(monkeypatch)
        result = build_docker_command("/wt/x", ["python", "agent.py"])
        assert result == _docker_prefix("/wt/x") + [TEST_IMAGE, "python", "agent.py"]
        assert "-e" not in result

    def test_env_empty_dict_produces_zero_e_flags(self, monkeypatch):
        _set_image(monkeypatch)
        result = build_docker_command("/wt/x", ["python", "agent.py"], {})
        assert result == _docker_prefix("/wt/x") + [TEST_IMAGE, "python", "agent.py"]
        assert "-e" not in result

    def test_image_lands_immediately_before_wrapped_argv(self, monkeypatch):
        _set_image(monkeypatch)
        argv = ["python", "agent.py", "--flag"]
        result = build_docker_command("/wt/x", argv)
        assert result[-len(argv) :] == argv
        assert result[-len(argv) - 1] == TEST_IMAGE

    def test_image_read_verbatim_from_env_var(self, monkeypatch):
        image = "ghcr.io/acme/sandbox:v2@sha256:deadbeef"
        _set_image(monkeypatch, image)
        result = build_docker_command("/wt/x", ["python", "agent.py"])
        assert result.count(image) == 1
        assert result[-3] == image

    def test_result_is_a_list_of_str(self, monkeypatch):
        _set_image(monkeypatch)
        result = build_docker_command("/wt/x", ["python"], {"LOCAL_AGENT_M": "1"})
        assert isinstance(result, list)
        assert all(isinstance(element, str) for element in result)


# ---------------------------------------------------------------------------
# build_docker_command — DENY-BY-DEFAULT env passthrough
# ---------------------------------------------------------------------------


class TestEnvPassthroughDenyByDefault:
    def test_only_prefixed_keys_forwarded(self, monkeypatch):
        _set_image(monkeypatch)
        env = {
            "LOCAL_AGENT_MODEL": "m1",
            "PIPELINE_STORY": "s-9",
            "PATH": "/usr/bin",
            "HOME": "/root",
            "USER": "agent",
            "SHELL": "/bin/bash",
            "LANG": "en_US.UTF-8",
            "VIRTUAL_ENV": "/venv",
        }
        result = build_docker_command("/wt/x", ["python", "agent.py"], env)
        # Order among multiple -e flags is not fixed by the contract, so
        # compare as sorted pairs; the count proves nothing extra leaks.
        assert sorted(_env_flags(result)) == sorted(
            ["LOCAL_AGENT_MODEL=m1", "PIPELINE_STORY=s-9"]
        )
        assert result.count("-e") == 2

    def test_disallowed_keys_absent_from_argv(self, monkeypatch):
        _set_image(monkeypatch)
        env = {"PATH": "/usr/bin", "OTHER": "v", "LOCAL": "nope", "PIPELINEY": "nope"}
        result = build_docker_command("/wt/x", ["python", "agent.py"], env)
        assert "-e" not in result
        assert "PATH=/usr/bin" not in result
        assert "OTHER=v" not in result
        assert result == _docker_prefix("/wt/x") + [TEST_IMAGE, "python", "agent.py"]

    def test_env_with_disallowed_keys_only_has_no_e_flags(self, monkeypatch):
        _set_image(monkeypatch)
        env = {"PATH": "/usr/bin", "HOME": "/root", "TERM": "xterm"}
        result = build_docker_command("/wt/x", ["python", "agent.py"], env)
        assert "-e" not in result
        assert _env_flags(result) == []

    def test_both_prefixes_forwarded(self, monkeypatch):
        _set_image(monkeypatch)
        env = {"LOCAL_AGENT_MODEL": "llama3", "PIPELINE_STORY_ID": "s-9"}
        result = build_docker_command("/wt/x", ["python"], env)
        assert sorted(_env_flags(result)) == sorted(
            ["LOCAL_AGENT_MODEL=llama3", "PIPELINE_STORY_ID=s-9"]
        )

    def test_prefix_must_start_at_key_start(self, monkeypatch):
        _set_image(monkeypatch)
        env = {
            "SOMELOCAL_AGENT_MODEL": "x",
            "MY_PIPELINETOKEN": "y",
            "XLOCAL_AGENT": "z",
        }
        result = build_docker_command("/wt/x", ["python"], env)
        assert _env_flags(result) == []
        assert "-e" not in result

    def test_prefix_match_is_case_sensitive(self, monkeypatch):
        _set_image(monkeypatch)
        env = {"local_agent_model": "x", "pipeline_story": "y"}
        result = build_docker_command("/wt/x", ["python"], env)
        assert _env_flags(result) == []
        assert "-e" not in result

    def test_value_preserved_verbatim_including_equals_and_spaces(self, monkeypatch):
        _set_image(monkeypatch)
        env = {"LOCAL_AGENT_FLAGS": "--a=b=c", "PIPELINE_NOTE": "hello world"}
        result = build_docker_command("/wt/x", ["python"], env)
        assert sorted(_env_flags(result)) == sorted(
            ["LOCAL_AGENT_FLAGS=--a=b=c", "PIPELINE_NOTE=hello world"]
        )

    def test_empty_value_still_forwarded(self, monkeypatch):
        _set_image(monkeypatch)
        result = build_docker_command("/wt/x", ["python"], {"LOCAL_AGENT_X": ""})
        assert _env_flags(result) == ["LOCAL_AGENT_X="]


# ---------------------------------------------------------------------------
# build_docker_command — argv wrapping
# ---------------------------------------------------------------------------


class TestArgvWrapping:
    def test_argv_forwarded_in_order_unmodified(self, monkeypatch):
        _set_image(monkeypatch)
        argv = [
            "python",
            "agent.py",
            "--model",
            "gpt 4 turbo",
            "--flag=value with spaces",
            "-",
        ]
        result = build_docker_command("/wt/x", argv)
        assert result[-len(argv) :] == argv
        assert result[: len(result) - len(argv)] == _docker_prefix("/wt/x") + [
            TEST_IMAGE
        ]

    def test_empty_string_args_survive_wrapping(self, monkeypatch):
        _set_image(monkeypatch)
        argv = ["python", "", "agent.py", ""]
        result = build_docker_command("/wt/x", argv)
        assert result[-len(argv) :] == argv
        assert result.count("") == 2

    def test_empty_argv_boundary(self, monkeypatch):
        _set_image(monkeypatch)
        assert build_docker_command("/wt/x", []) == _docker_prefix("/wt/x") + [
            TEST_IMAGE
        ]

    def test_single_element_argv_boundary(self, monkeypatch):
        _set_image(monkeypatch)
        assert build_docker_command("/wt/x", ["agent.py"]) == _docker_prefix(
            "/wt/x"
        ) + [TEST_IMAGE, "agent.py"]

    def test_worktree_with_spaces_mounted_at_its_host_path(self, monkeypatch):
        """The worktree is mounted AT ITS HOST PATH — identical path inside
        the container, no remapping."""
        _set_image(monkeypatch)
        result = build_docker_command("/wt/my project", ["python"])
        assert result[:7] == [
            "docker",
            "run",
            "--rm",
            "-v",
            "/wt/my project:/wt/my project",
            "--workdir",
            "/wt/my project",
        ]


# ---------------------------------------------------------------------------
# build_docker_command — fail closed on missing image var
# ---------------------------------------------------------------------------


class TestFailClosedOnMissingImage:
    def test_unset_image_var_raises_valueerror_naming_the_var(self, monkeypatch):
        _set_image(monkeypatch, None)
        monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
        with pytest.raises(ValueError, match="PIPELINE_SANDBOX_IMAGE"):
            build_docker_command("/wt/x", ["python", "agent.py"])

    def test_empty_image_var_raises_valueerror_naming_the_var(self, monkeypatch):
        _set_image(monkeypatch, "")
        monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
        with pytest.raises(ValueError, match="PIPELINE_SANDBOX_IMAGE"):
            build_docker_command("/wt/x", ["python", "agent.py"])

    def test_raised_error_is_valueerror_with_var_in_message(self, monkeypatch):
        _set_image(monkeypatch, None)
        monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
        with pytest.raises(ValueError) as excinfo:
            build_docker_command("/wt/x", ["python", "agent.py"])
        assert "PIPELINE_SANDBOX_IMAGE" in str(excinfo.value)


# ---------------------------------------------------------------------------
# build_docker_command — purity
# ---------------------------------------------------------------------------


class TestPurity:
    def test_inputs_not_mutated_and_result_is_a_new_list(self, monkeypatch):
        _set_image(monkeypatch)
        argv = ["python", "agent.py"]
        env = {"LOCAL_AGENT_MODEL": "m", "PATH": "/usr/bin"}
        argv_snapshot = list(argv)
        env_snapshot = dict(env)
        result = build_docker_command("/wt/x", argv, env)
        assert argv == argv_snapshot
        assert env == env_snapshot
        assert result is not argv

    def test_deterministic_across_repeat_calls(self, monkeypatch):
        _set_image(monkeypatch)
        first = build_docker_command("/wt/x", ["python"], {"LOCAL_AGENT_M": "1"})
        second = build_docker_command("/wt/x", ["python"], {"LOCAL_AGENT_M": "1"})
        assert first == second

    def test_build_does_not_probe_for_the_docker_binary(self, monkeypatch):
        """build_docker_command is a pure argv builder; binary availability
        is docker_binary_available's job, not theirs to conflate."""
        _set_image(monkeypatch)

        def boom(name, *args, **kwargs):
            raise AssertionError(
                "build_docker_command must not probe for the docker binary"
            )

        monkeypatch.setattr(shutil, "which", boom)
        if hasattr(sandbox_module, "which"):
            monkeypatch.setattr(sandbox_module, "which", boom)
        result = build_docker_command("/wt/x", ["python", "agent.py"])
        assert result[-2:] == ["python", "agent.py"]