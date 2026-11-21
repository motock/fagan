"""The command-wrap seam: ``pipeline.tenant_exec.wrap`` and ``is_configured``.

With ``PIPELINE_COMMAND_WRAP_PLUGIN`` unset or blank, ``wrap`` returns the very
list it was given. With it set, the plugin's ``wrap`` answer is the only
answer: any failure to load, call or validate it refuses the command with
``CommandWrapError('command wrap refused')`` and never falls back to the
unwrapped argv.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import tenant_exec
from pipeline.actor import bind_namespace
from pipeline.tenant_exec import CommandWrapError

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAP_VAR = "PIPELINE_COMMAND_WRAP_PLUGIN"
ALLOWLIST_VAR = "PIPELINE_PLUGIN_MODULES"
REFUSED = "command wrap refused"
SECRET_ARG = "tok-SECRET-argument"

_STATE: dict = {}


class _StubWrapper:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def wrap(self, argv, *, cwd, kind, namespace):
        self.calls.append(
            {"argv": argv, "cwd": cwd, "kind": kind, "namespace": namespace}
        )
        if self.raises is not None:
            raise self.raises
        if self.result is not None:
            return self.result
        return ["sandboxed", *argv]


def build():
    """Factory the seam loads through this module's allowlist entry."""
    _STATE["factory_calls"] = _STATE.get("factory_calls", 0) + 1
    if _STATE.get("factory_raises") is not None:
        raise _STATE["factory_raises"]
    return _STATE["wrapper"]


@pytest.fixture(autouse=True)
def _clean_cache():
    tenant_exec.reset_for_tests()
    _STATE.clear()
    yield
    tenant_exec.reset_for_tests()
    _STATE.clear()


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.delenv(WRAP_VAR, raising=False)
    monkeypatch.delenv(ALLOWLIST_VAR, raising=False)
    return monkeypatch


@pytest.fixture
def configured(monkeypatch):
    """Select the stub wrapper through the real allowlist and loader."""
    monkeypatch.setenv(WRAP_VAR, f"{__name__}:build")
    monkeypatch.setenv(ALLOWLIST_VAR, __name__)
    _STATE["wrapper"] = _StubWrapper()
    return _STATE["wrapper"]


class TestUnconfigured:
    def test_wrap_returns_the_identical_list_object(self, unset):
        argv = ["python", "-m", "agent"]

        assert tenant_exec.wrap(argv, cwd="/w", kind="agent") is argv

    @pytest.mark.parametrize("value", ["", "   ", "\t"])
    def test_blank_variable_behaves_as_unset(self, unset, value):
        unset.setenv(WRAP_VAR, value)
        argv = ["python", "-m", "agent"]

        assert tenant_exec.wrap(argv, cwd="/w", kind="agent") is argv
        assert tenant_exec.is_configured() is False

    def test_is_configured_is_false_when_unset(self, unset):
        assert tenant_exec.is_configured() is False

    def test_is_configured_is_true_when_set(self, unset):
        unset.setenv(WRAP_VAR, f"{__name__}:build")

        assert tenant_exec.is_configured() is True

    def test_unknown_kind_raises_value_error(self, unset):
        with pytest.raises(ValueError):
            tenant_exec.wrap(["x"], cwd="/w", kind="deploy")

    def test_unknown_kind_raises_value_error_even_when_configured(self, configured):
        with pytest.raises(ValueError):
            tenant_exec.wrap(["x"], cwd="/w", kind="deploy")
        assert configured.calls == []

    def test_unset_wrap_does_not_load_the_plugin(self, unset):
        unset.setenv(ALLOWLIST_VAR, __name__)
        _STATE["factory_calls"] = 0

        tenant_exec.wrap(["x"], cwd="/w", kind="agent")

        assert _STATE["factory_calls"] == 0


class TestConfiguredPlugin:
    def test_plugin_receives_argv_copy_cwd_kind_and_bound_namespace(self, configured):
        argv = ["python", "-m", "agent"]
        with bind_namespace("acme"):
            tenant_exec.wrap(argv, cwd="/w/story", kind="review")

        call = configured.calls[0]
        assert call["argv"] == argv
        assert call["argv"] is not argv
        assert call["cwd"] == "/w/story"
        assert call["kind"] == "review"
        assert call["namespace"] == "acme"

    def test_plugin_mutating_its_argument_does_not_change_the_callers_argv(
        self, configured
    ):
        argv = ["python", "-m", "agent"]
        configured.wrap = lambda a, **_: (a.append("tainted"), ["ok"])[1]

        tenant_exec.wrap(argv, cwd="/w", kind="agent")

        assert argv == ["python", "-m", "agent"]

    def test_plugin_result_is_returned(self, configured):
        configured.result = ["bwrap", "--", "python", "-m", "agent"]

        result = tenant_exec.wrap(["python", "-m", "agent"], cwd="/w", kind="agent")

        assert result == ["bwrap", "--", "python", "-m", "agent"]

    def test_plugin_is_built_once_per_process(self, configured):
        _STATE["factory_calls"] = 0

        tenant_exec.wrap(["a"], cwd="/w", kind="agent")
        tenant_exec.wrap(["b"], cwd="/w", kind="build")

        assert _STATE["factory_calls"] == 1

    def test_reset_for_tests_drops_the_cached_plugin(self, configured):
        _STATE["factory_calls"] = 0
        tenant_exec.wrap(["a"], cwd="/w", kind="agent")

        tenant_exec.reset_for_tests()
        tenant_exec.wrap(["a"], cwd="/w", kind="agent")

        assert _STATE["factory_calls"] == 2


class TestRefusals:
    def test_plugin_without_allowlist_refuses(self, monkeypatch):
        monkeypatch.setenv(WRAP_VAR, f"{__name__}:build")
        monkeypatch.delenv(ALLOWLIST_VAR, raising=False)
        _STATE["wrapper"] = _StubWrapper()

        with pytest.raises(CommandWrapError) as exc:
            tenant_exec.wrap(["x"], cwd="/w", kind="agent")

        assert str(exc.value) == REFUSED
        assert _STATE["wrapper"].calls == []

    def test_factory_raising_refuses(self, configured):
        _STATE["factory_raises"] = RuntimeError("boom")

        with pytest.raises(CommandWrapError) as exc:
            tenant_exec.wrap(["x"], cwd="/w", kind="agent")

        assert str(exc.value) == REFUSED

    def test_wrap_method_raising_refuses(self, configured):
        configured.raises = OSError("disk gone")

        with pytest.raises(CommandWrapError) as exc:
            tenant_exec.wrap(["x"], cwd="/w", kind="agent")

        assert str(exc.value) == REFUSED

    def test_empty_result_refuses(self, configured):
        configured.result = []

        with pytest.raises(CommandWrapError) as exc:
            tenant_exec.wrap(["x"], cwd="/w", kind="agent")

        assert str(exc.value) == REFUSED

    def test_non_string_element_refuses(self, configured):
        configured.result = ["bwrap", 3]

        with pytest.raises(CommandWrapError) as exc:
            tenant_exec.wrap(["x"], cwd="/w", kind="agent")

        assert str(exc.value) == REFUSED

    def test_non_list_result_refuses(self, configured):
        configured.result = "bwrap -- python"

        with pytest.raises(CommandWrapError) as exc:
            tenant_exec.wrap(["x"], cwd="/w", kind="agent")

        assert str(exc.value) == REFUSED

    def test_tuple_result_refuses(self, configured):
        configured.result = ("bwrap", "--")

        with pytest.raises(CommandWrapError) as exc:
            tenant_exec.wrap(["x"], cwd="/w", kind="agent")

        assert str(exc.value) == REFUSED

    def test_refusal_never_returns_the_unwrapped_argv(self, configured):
        configured.raises = RuntimeError("boom")
        argv = ["python", "-m", "agent"]

        with pytest.raises(CommandWrapError):
            tenant_exec.wrap(argv, cwd="/w", kind="agent")

    def test_refusal_logs_one_warning_with_kind_and_error_type_only(
        self, configured, caplog
    ):
        configured.raises = KeyError("missing")
        caplog.set_level(logging.WARNING)

        with pytest.raises(CommandWrapError):
            tenant_exec.wrap(["python", SECRET_ARG], cwd="/w", kind="test")

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        record = warnings[0]
        assert record.getMessage() == REFUSED
        assert record.wrap_kind == "test"
        assert record.error_type == "KeyError"
        assert SECRET_ARG not in repr(record.__dict__)

    def test_refusal_log_carries_no_argv_element_from_a_bad_result(
        self, configured, caplog
    ):
        configured.result = [SECRET_ARG, 7]
        caplog.set_level(logging.WARNING)

        with pytest.raises(CommandWrapError):
            tenant_exec.wrap(["python", SECRET_ARG], cwd="/w", kind="agent")

        assert SECRET_ARG not in repr(
            [r.__dict__ for r in caplog.records if r.levelno >= logging.WARNING]
        )


class TestColdImport:
    def test_module_imports_in_a_fresh_interpreter_without_the_server(self):
        code = (
            "import sys, pipeline.tenant_exec; print('pipeline.server' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"
