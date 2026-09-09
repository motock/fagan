"""Unit tests: ``_execute_tool`` must enrich signature-mismatch TypeErrors.

Story goal (2026-09-09): when a tool call's arguments do not match the tool's
signature, the model must get back an error it can act on - one that names
(a) the tool, (b) the argument names that were supplied, and (c) the tool's
declared params from ``TOOLS[name]``. Today ``_execute_tool`` returns
``str(exc)`` for every exception, so a mismatch surfaces to the model as
``<lambda>() missing 2 required positional arguments: ...`` - it names only
the anonymous lambda, leaving the model nothing to self-correct with.

Constraints these tests pin without weakening any existing test:

- The enrichment lives in ``_execute_tool`` only. Calling
  ``TOOLS[name]["execute"]`` directly must still raise a raw ``TypeError``
  whose message does not name the tool (tests/unit/test_chat_plan_tools.py
  pins the same raw-TypeError escape for decompose/save_plan).
- Non-signature exceptions keep the bare ``str(exc)`` message
  (tests/unit/test_chat_agent_loop.py::TestExecuteTool::
  test_tool_exception_is_caught pins ``{"error": "boom!"}`` exactly).
- The unknown-tool path keeps returning exactly
  ``{"error": "unknown tool: <name>"}`` and makes zero HTTP calls.
- A malformed call must not reach the network and must not be retried with
  guessed argument names.
- Registry entries keep exactly the three declared keys
  (tests/unit/test_chat_readonly_tools.py pins the entry shape) and no
  ``execute`` callable's signature changes.

These tests deliberately do NOT pin the enrichment's message format, nor
whether the dispatcher pre-validates args against ``params`` or detects the
mismatch from the caught ``TypeError`` - only the returned error's content.
"""

import inspect
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.chat import TOOLS, _execute_tool

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _NoHttpClient:
    """Fails loudly if a malformed call ever reaches the network."""

    def get(self, *args, **kwargs):
        raise AssertionError("no-http: get must not be called")

    def post(self, *args, **kwargs):
        raise AssertionError("no-http: post must not be called")


class _JsonResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestSignatureMismatchEnrichment:
    """A wrong-argument call returns an actionable error, not a raw TypeError."""

    def test_save_plan_wrong_arg_names_error_names_tool_declared_and_supplied(self):
        # The exact shape the chat model emitted in the live 2026-09-09 failure.
        result = _execute_tool(
            "save_plan",
            {"name": "x", "plan": {}},
            _NoHttpClient(),
            "http://test",
        )
        assert "error" in result
        assert "result" not in result
        message = result["error"]
        assert isinstance(message, str)
        assert "save_plan" in message
        assert "plan_name" in message
        assert "plan_json" in message
        for declared in TOOLS["save_plan"]["params"]:
            assert declared in message, f"missing declared param {declared!r}"
        for supplied in ("name", "plan"):
            assert supplied in message, f"missing supplied arg {supplied!r}"
        assert "no-http" not in message, "a malformed call must not reach HTTP"

    def test_get_plan_missing_all_args_error_names_tool_and_declared_param(self):
        result = _execute_tool("get_plan", {}, _NoHttpClient(), "http://test")
        assert "error" in result
        message = result["error"]
        assert "get_plan" in message
        assert "plan_name" in message
        assert "no-http" not in message, "a malformed call must not reach HTTP"

    def test_missing_one_of_several_params_names_tool_and_missing_param(self):
        result = _execute_tool(
            "dispatch_story", {"plan_name": "p"}, _NoHttpClient(), "http://test"
        )
        message = result["error"]
        assert "dispatch_story" in message
        assert "story_key" in message
        assert "plan_name" in message
        assert "no-http" not in message, "a malformed call must not reach HTTP"

    def test_error_names_supplied_args_disjoint_from_declared_params(self, monkeypatch):
        def _frobnicate(http_client, api_base_url, alpha_src, beta_dst, **kwargs):
            return {"ok": True}

        monkeypatch.setitem(
            TOOLS,
            "frobnicate",
            {
                "description": "Frobnicate.",
                "params": {"alpha_src": "str", "beta_dst": "str"},
                "execute": _frobnicate,
            },
        )
        result = _execute_tool(
            "frobnicate", {"zap": 1, "quux": 2}, _NoHttpClient(), "http://test"
        )
        message = result["error"]
        # The supplied names share no substring with the declared params, so
        # each assertion below is a real requirement, not a vacuous hit.
        assert "frobnicate" in message
        assert "alpha_src" in message
        assert "beta_dst" in message
        assert "zap" in message
        assert "quux" in message

    def test_unexpected_keyword_argument_error_names_tool_declared_and_supplied(
        self, monkeypatch
    ):
        def _strict(http_client, api_base_url, required):
            return {"got": required}

        monkeypatch.setitem(
            TOOLS,
            "strict",
            {
                "description": "Strict.",
                "params": {"required": "str"},
                "execute": _strict,
            },
        )
        result = _execute_tool(
            "strict", {"wrong_name": 1}, _NoHttpClient(), "http://test"
        )
        message = result["error"]
        assert "strict" in message
        assert "required" in message
        assert "wrong_name" in message

    def test_malformed_call_is_not_retried_with_guessed_argument_names(
        self, monkeypatch
    ):
        attempts = []

        def _mismatch(http_client, api_base_url, required):
            attempts.append({"required": required})
            raise TypeError(
                "_mismatch() missing 1 required positional argument: 'required'"
            )

        monkeypatch.setitem(
            TOOLS,
            "flaky",
            {
                "description": "Flaky.",
                "params": {"required": "str"},
                "execute": _mismatch,
            },
        )
        result = _execute_tool("flaky", {}, _NoHttpClient(), "http://test")
        assert len(attempts) <= 1, "a malformed call must not be retried"
        assert "error" in result
        assert "flaky" in result["error"]
        assert "required" in result["error"]


class TestNonSignatureExceptionsKeepBareMessage:
    def test_runtime_error_keeps_the_exact_bare_message(self, monkeypatch):
        def _boom(http_client, api_base_url, **kwargs):
            raise RuntimeError("boom!")

        monkeypatch.setitem(TOOLS, "explode", {"execute": _boom})
        result = _execute_tool("explode", {}, None, "http://test")
        assert result == {"error": "boom!"}

    def test_non_signature_type_error_keeps_the_bare_message(self, monkeypatch):
        # The args match the declared params, so this TypeError is not a
        # signature mismatch - the enrichment must not swallow its message.
        def _internal(http_client, api_base_url, required, **kwargs):
            raise TypeError("boom type")

        monkeypatch.setitem(
            TOOLS,
            "internal",
            {
                "description": "Internal.",
                "params": {"required": "str"},
                "execute": _internal,
            },
        )
        result = _execute_tool("internal", {"required": "v"}, None, "http://test")
        assert result == {"error": "boom type"}


class TestDefensiveParamsRead:
    def test_entry_without_params_key_does_not_raise_key_error(self, monkeypatch):
        def _hungry(http_client, api_base_url, required, **kwargs):
            return {"got": required}

        monkeypatch.setitem(TOOLS, "grabby", {"execute": _hungry})
        result = _execute_tool("grabby", {}, None, "http://test")
        assert "error" in result
        assert "grabby" in result["error"]
        assert "required" in result["error"]

    def test_entry_with_params_none_does_not_crash_the_dispatcher(self, monkeypatch):
        def _hungry_too(http_client, api_base_url, required, **kwargs):
            return {"got": required}

        monkeypatch.setitem(
            TOOLS, "grabby_too", {"params": None, "execute": _hungry_too}
        )
        result = _execute_tool("grabby_too", {}, None, "http://test")
        assert "error" in result
        assert "grabby_too" in result["error"]


class TestUnknownToolPathUnchanged:
    def test_unknown_tool_error_unchanged_and_makes_no_http_calls(self):
        result = _execute_tool("no_such_tool", {}, _NoHttpClient(), "http://test")
        assert result == {"error": "unknown tool: no_such_tool"}


class TestCorrectArgsStillWork:
    def test_save_plan_correct_args_return_result_and_perform_http_call(self):
        recorded = []

        class _Client:
            def post(self, url, **kwargs):
                recorded.append((url, kwargs.get("json")))
                return _JsonResp({"ok": True})

        result = _execute_tool(
            "save_plan",
            {"plan_name": "demo", "plan_json": '{"epics":[]}'},
            _Client(),
            "http://test",
        )
        assert result == {"result": {"ok": True}}
        assert recorded == [("/api/plans/demo/save", {"plan_json": '{"epics":[]}'})]

    def test_get_plan_correct_args_return_result_and_perform_http_call(self):
        recorded = []

        class _Client:
            def get(self, url):
                recorded.append(url)
                return _JsonResp({"name": "demo"})

        result = _execute_tool(
            "get_plan", {"plan_name": "demo"}, _Client(), "http://test"
        )
        assert result == {"result": {"name": "demo"}}
        assert recorded == ["/api/plans/demo"]

    def test_empty_string_param_value_is_still_forwarded(self):
        # Boundary: an empty-string value is a value, not a missing argument -
        # the enrichment must not fire for correctly-named-but-empty args.
        recorded = []

        class _Client:
            def get(self, url):
                recorded.append(url)
                return _JsonResp({})

        result = _execute_tool("get_plan", {"plan_name": ""}, _Client(), "http://test")
        assert "result" in result
        assert recorded == ["/api/plans/"]

    def test_extra_unknown_args_are_still_accepted(self, monkeypatch):
        def _loose(http_client, api_base_url, required, **kwargs):
            return {"got": required, "extra": dict(kwargs)}

        monkeypatch.setitem(
            TOOLS,
            "loose",
            {
                "description": "Loose.",
                "params": {"required": "str"},
                "execute": _loose,
            },
        )
        result = _execute_tool(
            "loose", {"required": "v", "unrelated": 1}, None, "http://test"
        )
        assert result == {"result": {"got": "v", "extra": {"unrelated": 1}}}


class TestEnrichmentLivesInExecuteToolOnly:
    @pytest.mark.parametrize("tool", ["save_plan", "decompose"])
    def test_execute_callable_still_raises_raw_type_error_when_called_directly(
        self, tool
    ):
        # Calling the execute callable directly (as
        # tests/unit/test_chat_plan_tools.py does) must still raise a raw
        # TypeError whose message does not name the tool - proving the
        # enrichment did not move into the execute callables.
        with pytest.raises(TypeError) as excinfo:
            TOOLS[tool]["execute"](_NoHttpClient(), "http://test")
        assert tool not in str(excinfo.value)

    @pytest.mark.parametrize("tool", ["save_plan", "get_plan", "decompose"])
    def test_execute_callables_keep_their_declared_signature(self, tool):
        sig = inspect.signature(TOOLS[tool]["execute"])
        names = list(sig.parameters)
        assert names[:2] == ["http_client", "api_base_url"]
        assert any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )

    def test_registry_entry_keeps_exactly_the_three_declared_keys(self):
        assert set(TOOLS["save_plan"]) == {"description", "params", "execute"}


class TestStoryGates:
    def test_acceptance_fixture_is_fully_green(self):
        fixture = _REPO_ROOT / "tests" / "acceptance_chat_tool_arg_error.py"
        assert fixture.is_file(), "the graded acceptance fixture is missing"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/acceptance_chat_tool_arg_error.py",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
        assert "13 passed" in proc.stdout

    def test_story_files_are_ruff_clean(self):
        ruff = shutil.which("ruff")
        if ruff is None:
            candidate = _REPO_ROOT / ".venv" / "bin" / "ruff"
            if not candidate.is_file():
                pytest.skip("ruff is not available in this environment")
            ruff = str(candidate)
        proc = subprocess.run(
            [ruff, "check", "app/chat.py", str(Path(__file__).resolve())],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr