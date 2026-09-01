"""Tests for pipeline.companion_server — the B5 companion MCP server.

The companion server exposes ONLY the adoptable subset of the pipeline: the
overlord decision path (``escalate_decision``) and the acceptance-oracle
helpers (``classify_oracle_outcome`` / ``acceptance_digests``). It must reuse
the real pipeline modules (no duplicated logic) and must NOT register any of
the main pipeline server's other tools.

Registration-path note: these tests assert the FastMCP registration itself
(``mcp._tool_manager._tools[name].fn is <declared function>``), not just that
a module-level callable exists — a tool silently dropped from the decorator
registration must fail here, not only at call time.

Seam note (mirrors tests/unit/test_pipeline_mcp_server.py): the companion
module is required to call the overlord via its own module reference
(``from pipeline import overlord`` then ``overlord._invoke_overlord(...)`` in
the tool body), so these tests patch ``pipeline.overlord._invoke_overlord``
and ``pipeline.overlord._load_policy`` directly. The oracle helpers are
patched at their home module (``pipeline.oracle_gate``) for the delegation
assertions and exercised for real in the unmocked behavior tests.
"""

import hashlib
import inspect
import re
import subprocess
import sys
from unittest import mock

import pytest

from pipeline import companion_server

COMPANION_TOOLS = ("escalate_decision", "classify_oracle_outcome", "acceptance_digests")


def _registered_tools():
    """The companion server's registered tool objects, by name."""
    return companion_server.mcp._tool_manager._tools


def _call_args_flat(call):
    """Forwarded arguments of a mock call as an ordered list of values,
    regardless of whether the callee passed them positionally or by keyword."""
    return list(call.args) + list(call.kwargs.values())


def _call_arg(call, index, name):
    """Fetch one forwarded argument whether passed positionally or by keyword."""
    if len(call.args) > index:
        return call.args[index]
    assert name in call.kwargs, f"expected kwarg {name!r}, got {call}"
    return call.kwargs[name]


class TestCompanionServerConstruction:
    """FastMCP construction must mirror pipeline/server.py's pattern."""

    def test_server_instance_is_named_pipeline_companion(self):
        assert companion_server.mcp.name == "pipeline-companion"

    def test_fastmcp_constructed_with_the_companion_name(self):
        source = inspect.getsource(companion_server)
        assert re.search(r"""FastMCP\(\s*['"]pipeline-companion['"]\s*\)""", source)

    def test_module_docstring_explains_b5_companion_purpose(self):
        doc = (inspect.getdoc(companion_server) or "").lower()
        # Purpose: piecemeal adoption of the two exported ideas; B5 item.
        assert "b5" in doc, "docstring must reference the B5 plan item"
        assert "companion" in doc
        assert "overlord" in doc
        assert "oracle" in doc
        # The brief requires noting that the real modules are imported
        # (no duplication of their logic).
        assert "no duplication" in doc

    def test_main_guard_calls_mcp_run_like_pipeline_server(self):
        source = inspect.getsource(companion_server)
        assert '__name__ == "__main__"' in source or "__name__ == '__main__'" in source
        assert re.search(r"mcp\.run\(\)", source)

    def test_logging_suppression_note_present_like_pipeline_server(self):
        source = inspect.getsource(companion_server)
        assert "logging.basicConfig" in source


class TestToolRegistration:
    """Each tool must be registered on the FastMCP instance AND bound to the
    declared module-level function (catches silent deregistration)."""

    @pytest.mark.parametrize("name", COMPANION_TOOLS)
    def test_tool_registered_and_bound_to_declared_function(self, name):
        tool = _registered_tools()[name]  # KeyError == not registered
        assert tool.fn is getattr(companion_server, name)

    def test_companion_exposes_exactly_the_three_adoptable_tools(self):
        assert set(_registered_tools()) == set(COMPANION_TOOLS)

    def test_pipeline_server_tools_are_not_exposed(self):
        names = set(_registered_tools())
        assert "ingest_plan" not in names
        assert "dispatch_story" not in names


class TestEscalateDecision:
    """escalate_decision mirrors request_decision's prompt assembly and calls
    pipeline.overlord._invoke_overlord with it (patched at pipeline.overlord)."""

    def test_assembles_prompt_with_question_options_context_and_policy(self):
        with mock.patch(
            "pipeline.overlord._invoke_overlord", return_value="RULING: option 2"
        ) as invoke, mock.patch(
            "pipeline.overlord._load_policy", return_value="POLICY MARKER 12345"
        ) as load_policy:
            result = companion_server.escalate_decision(
                "Which cache eviction policy?",
                ["LRU", "LFU", "random"],
                context="hot read loop over 10k keys",
            )

        assert result == "RULING: option 2"
        assert invoke.call_count == 1
        prompt = _call_arg(invoke.call_args, 0, "prompt")
        assert "Which cache eviction policy?" in prompt
        for option in ("LRU", "LFU", "random"):
            assert option in prompt
        assert "hot read loop over 10k keys" in prompt
        # Policy text from pipeline.overlord._load_policy is embedded, not
        # re-loaded by companion-local logic.
        assert "POLICY MARKER 12345" in prompt
        assert load_policy.call_count == 1

    def test_empty_options_list_still_assembles_valid_prompt(self):
        with mock.patch(
            "pipeline.overlord._invoke_overlord", return_value="RULING"
        ) as invoke, mock.patch("pipeline.overlord._load_policy", return_value=""):
            result = companion_server.escalate_decision("Ship now or wait?", [])

        assert result == "RULING"
        assert invoke.call_count == 1
        prompt = _call_arg(invoke.call_args, 0, "prompt")
        assert "Ship now or wait?" in prompt

    def test_single_option_boundary(self):
        with mock.patch(
            "pipeline.overlord._invoke_overlord", return_value="ok"
        ) as invoke, mock.patch("pipeline.overlord._load_policy", return_value=""):
            companion_server.escalate_decision("Adopt?", ["only-choice"])

        prompt = _call_arg(invoke.call_args, 0, "prompt")
        assert "Adopt?" in prompt
        assert "only-choice" in prompt

    def test_context_defaults_to_empty_without_none_leaking_into_prompt(self):
        with mock.patch(
            "pipeline.overlord._invoke_overlord", return_value="RULING"
        ) as invoke, mock.patch("pipeline.overlord._load_policy", return_value=""):
            companion_server.escalate_decision("Go or no-go?", ["go", "no-go"])

        prompt = _call_arg(invoke.call_args, 0, "prompt")
        assert "None" not in prompt

    def test_empty_question_boundary_still_invokes_overlord(self):
        with mock.patch(
            "pipeline.overlord._invoke_overlord", return_value="RULING"
        ) as invoke, mock.patch("pipeline.overlord._load_policy", return_value=""):
            result = companion_server.escalate_decision("", ["a"])

        assert result == "RULING"
        assert invoke.call_count == 1
        assert isinstance(_call_arg(invoke.call_args, 0, "prompt"), str)


class TestClassifyOracleOutcome:
    """Thin pass-through wrapper over pipeline.oracle_gate.classify_oracle_outcome."""

    def test_delegates_with_identical_arguments_and_returns_result(self):
        sentinel = {"state": "fails_correctly", "detail": "fixture fails as expected"}
        with mock.patch(
            "pipeline.oracle_gate.classify_oracle_outcome", return_value=sentinel
        ) as delegate:
            result = companion_server.classify_oracle_outcome(
                1, "AssertionError: boom", {"id": "B5-01"}
            )

        assert result == sentinel
        assert delegate.call_count == 1
        # The real oracle_gate.classify_oracle_outcome(returncode, output) takes
        # no story parameter: the wrapper forwards exactly these two values and
        # must not fabricate or forward the story argument.
        assert _call_args_flat(delegate.call_args) == [1, "AssertionError: boom"]

    def test_real_boundary_returncode_zero_classifies_passes(self):
        outcome = companion_server.classify_oracle_outcome(0, "all passed")
        assert outcome["state"] == "passes"
        assert isinstance(outcome["detail"], str)

    def test_real_boundary_returncode_five_classifies_empty(self):
        assert companion_server.classify_oracle_outcome(5, "")["state"] == "empty"

    def test_real_broken_oracle_marker_classifies_errors(self):
        outcome = companion_server.classify_oracle_outcome(1, "INTERNALERROR boom")
        assert outcome["state"] == "errors"

    def test_real_plain_failure_classifies_fails_correctly(self):
        outcome = companion_server.classify_oracle_outcome(1, "1 failed, 0 passed")
        assert outcome["state"] == "fails_correctly"


class TestAcceptanceDigests:
    """Thin pass-through wrapper over pipeline.oracle_gate.acceptance_digests."""

    def test_delegates_with_the_passed_story(self):
        story = {"acceptance": [{"path": "tests/unit/test_x.py", "source": "assert True"}]}
        sentinel = {"tests/unit/test_x.py": "cafebabe"}
        with mock.patch(
            "pipeline.oracle_gate.acceptance_digests", return_value=sentinel
        ) as delegate:
            result = companion_server.acceptance_digests(story)

        assert result == sentinel
        assert delegate.call_count == 1
        assert _call_args_flat(delegate.call_args) == [story]

    def test_real_digest_is_sha256_of_manifest_source(self):
        story = {
            "acceptance": [
                {"path": "tests/unit/test_x.py", "source": "assert app is True\n"}
            ]
        }
        result = companion_server.acceptance_digests(story)
        assert result == {
            "tests/unit/test_x.py": hashlib.sha256(b"assert app is True\n").hexdigest()
        }

    def test_real_empty_story_boundary_returns_empty_dict(self):
        assert companion_server.acceptance_digests({}) == {}

    def test_real_story_without_acceptance_key_returns_empty_dict(self):
        assert companion_server.acceptance_digests({"id": "B5-01"}) == {}

    def test_real_empty_acceptance_list_boundary_returns_empty_dict(self):
        assert companion_server.acceptance_digests({"acceptance": []}) == {}


class TestNoWholeServerImport:
    """The companion must import the real helper modules, not the whole
    pipeline server: a foreign harness adopting these tools should not pull
    in pipeline.server (and its heavy side effects) at module scope."""

    def test_importing_companion_does_not_import_pipeline_server(self):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import pipeline.companion_server; "
                    "print('pipeline.server' in sys.modules)"
                ),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.strip().endswith("False"), probe.stderr