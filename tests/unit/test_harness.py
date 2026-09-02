"""Tests for app/harness.py — the agent-harness seam (registry module).

app/harness.py is the AGENT HARNESS axis' registry: it owns the
HarnessRequest/HarnessCommand value types, the AgentHarness protocol, and the
cumulative _HARNESSES registry with register_harness()/get_harness(). The two
sibling stories that follow register 'claude' and 'local' adapters into it.

CUMULATIVE ARTIFACT RULE (stated here per the plan, binding this file):
_HARNESSES is a shared, cumulative registry that later sibling stories extend.
Every assertion below checks ONLY membership/behavior of what THIS story adds
(the 'fake' probe harness) — never the registry's total contents, an exact key
count, or the presence/absence of 'claude'/'local'. An autouse fixture
snapshots and restores the registry around each test so nothing leaks between
sibling test modules sharing one pytest process.

app/harness.py must import ONLY the stdlib + typing/dataclasses — never
app.backend, app.backend_claude, app.backend_ollama, or scripts.local_agent
(no import cycles, ever). test_harness_module_imports_only_stdlib enforces
this via AST; the human-readable grep equivalent, which must return nothing,
is:

    grep -nE "^[[:space:]]*(from|import)[[:space:]]+(app|pipeline|scripts)\\b" app/harness.py
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import sys
from pathlib import Path
from typing import ClassVar, Protocol

import pytest

from app import harness
from app.harness import (
    AgentHarness,
    HarnessCommand,
    HarnessRequest,
    get_harness,
    register_harness,
)

# ---------------------------------------------------------------------------
# Fakes: the registry probe this story tests with. Real harnesses ('claude',
# 'local') belong to later stories and are never referenced or asserted here.
# ---------------------------------------------------------------------------


class FakeHarness:
    """Passthrough AgentHarness double used as this story's registry probe.

    build_agent_command returns a fixed HarnessCommand(argv=['echo'], env={})
    and records the request it was handed so tests can assert the request
    flows through unchanged. ``env={}`` also encodes the HarnessCommand.env
    contract: ONLY the additional variables this harness requires — never a
    full inherited environment (the caller merges env over its own).
    """

    constructed: ClassVar[int] = 0
    seen: ClassVar[list] = []

    def __init__(self) -> None:
        type(self).constructed += 1

    def build_agent_command(self, request: HarnessRequest) -> HarnessCommand:
        type(self).seen.append(request)
        return HarnessCommand(argv=["echo"], env={})


class OtherHarness:
    """A DIFFERENT class, to prove duplicate-registration conflict detection."""

    def __init__(self) -> None:
        pass

    def build_agent_command(self, request: HarnessRequest) -> HarnessCommand:
        return HarnessCommand(argv=["true"], env={})


@pytest.fixture(autouse=True)
def _registry_snapshot():
    """Snapshot/restore the cumulative _HARNESSES registry around each test.

    Later sibling stories register 'claude' and 'local' into this same dict
    (possibly at their modules' import time). Restoring after every test keeps
    this file's registrations from leaking into them — and is what lets every
    assertion here stay membership-based instead of exact-contents-based.
    """
    saved = dict(harness._HARNESSES)
    FakeHarness.constructed = 0
    FakeHarness.seen.clear()
    yield
    harness._HARNESSES.clear()
    harness._HARNESSES.update(saved)
    FakeHarness.constructed = 0
    FakeHarness.seen.clear()


# ---------------------------------------------------------------------------
# HarnessRequest value type
# ---------------------------------------------------------------------------


def test_harness_request_is_a_dataclass_with_exact_field_order_and_defaults():
    assert dataclasses.is_dataclass(HarnessRequest)
    fields = dataclasses.fields(HarnessRequest)
    assert [f.name for f in fields] == [
        "prompt",
        "system",
        "model",
        "cwd",
        "acceptance",
        "options",
    ]
    # prompt/system/model/cwd are required; acceptance/options default to None.
    for field in fields[:4]:
        assert field.default is dataclasses.MISSING, field.name
        assert field.default_factory is dataclasses.MISSING, field.name
    for field in fields[4:]:
        assert field.default is None, field.name
        assert field.default_factory is dataclasses.MISSING, field.name


def test_harness_request_holds_its_fields():
    request = HarnessRequest(prompt="p", system=None, model="m", cwd="/tmp")
    assert request.prompt == "p"
    assert request.system is None
    assert request.model == "m"
    assert request.cwd == "/tmp"
    assert request.acceptance is None
    assert request.options is None


def test_harness_request_positional_construction_follows_field_order():
    request = HarnessRequest("p", None, "m", "/tmp", ["t1"], {"k": "v"})
    assert (request.prompt, request.system, request.model, request.cwd) == (
        "p",
        None,
        "m",
        "/tmp",
    )
    assert request.acceptance == ["t1"]
    assert request.options == {"k": "v"}


def test_harness_request_is_frozen():
    request = HarnessRequest(prompt="p", system=None, model="m", cwd="/tmp")
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.prompt = "mutated"
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.options = {"k": "v"}


def test_harness_request_missing_required_fields_raise_type_error():
    with pytest.raises(TypeError):
        HarnessRequest(prompt="p", system=None, model="m")  # cwd missing
    with pytest.raises(TypeError):
        HarnessRequest()  # nothing supplied


# ---------------------------------------------------------------------------
# HarnessCommand value type
# ---------------------------------------------------------------------------


def test_harness_command_is_a_dataclass_with_argv_and_env():
    assert dataclasses.is_dataclass(HarnessCommand)
    assert [f.name for f in dataclasses.fields(HarnessCommand)] == ["argv", "env"]
    command = HarnessCommand(argv=["echo", "hi"], env={"LOCAL_AGENT_X": "1"})
    assert command.argv == ["echo", "hi"]
    assert command.env == {"LOCAL_AGENT_X": "1"}


def test_harness_command_env_is_required_and_instance_is_frozen():
    with pytest.raises(TypeError):
        HarnessCommand(argv=["echo"])  # env is required, not defaulted
    command = HarnessCommand(argv=["echo"], env={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.argv = ["nope"]


# ---------------------------------------------------------------------------
# AgentHarness protocol
# ---------------------------------------------------------------------------


def test_agent_harness_is_a_protocol_with_build_agent_command():
    is_protocol = Protocol in AgentHarness.__mro__ or getattr(
        AgentHarness, "_is_protocol", False
    )
    assert is_protocol, "AgentHarness must be a typing.Protocol"
    assert any(
        "build_agent_command" in base.__dict__ for base in AgentHarness.__mro__
    ), "AgentHarness must declare build_agent_command"
    params = list(inspect.signature(AgentHarness.build_agent_command).parameters)
    assert params == ["self", "request"]


# ---------------------------------------------------------------------------
# Registry: register_harness / get_harness happy paths
# ---------------------------------------------------------------------------


def test_register_then_get_returns_instance_of_registered_class():
    assert register_harness("fake", FakeHarness) is None
    instance = get_harness("fake")
    assert isinstance(instance, FakeHarness)


def test_registry_maps_the_normalized_name_to_the_class():
    register_harness("Fake", FakeHarness)
    assert harness._HARNESSES.get("fake") is FakeHarness


def test_get_harness_constructs_the_class_with_no_args_on_every_call():
    register_harness("fake", FakeHarness)
    assert FakeHarness.constructed == 0
    first = get_harness("fake")
    second = get_harness("fake")
    assert FakeHarness.constructed == 2  # cls() per call, not a cached singleton
    assert isinstance(first, FakeHarness)
    assert isinstance(second, FakeHarness)


# ---------------------------------------------------------------------------
# Request -> command round trip through build_agent_command
# ---------------------------------------------------------------------------


def test_request_flows_through_build_agent_command_unchanged():
    register_harness("fake", FakeHarness)
    request = HarnessRequest(prompt="p", system=None, model="m", cwd="/tmp")
    command = get_harness("fake").build_agent_command(request)
    assert command.argv == ["echo"]
    assert command.env == {}
    assert FakeHarness.seen[-1] is request  # same object, untouched


def test_acceptance_and_options_flow_through_build_agent_command_unchanged():
    register_harness("fake", FakeHarness)
    acceptance = ["pytest -q tests/unit/test_harness.py"]
    options = {
        "python_executable": "/venv/bin/python",
        "agent_script": "scripts/agent.py",
    }
    request = HarnessRequest(
        prompt="p",
        system="sys",
        model="m",
        cwd="/tmp",
        acceptance=acceptance,
        options=options,
    )
    command = get_harness("fake").build_agent_command(request)
    assert command.argv == ["echo"]
    assert FakeHarness.seen[-1] is request
    assert FakeHarness.seen[-1].acceptance is acceptance
    assert FakeHarness.seen[-1].options is options


# ---------------------------------------------------------------------------
# Fail-closed lookups: unknown / empty / whitespace names, empty registry
# ---------------------------------------------------------------------------


def test_get_harness_unknown_name_raises_value_error_listing_registered_names():
    register_harness("fake", FakeHarness)
    with pytest.raises(ValueError) as excinfo:
        get_harness("nonexistent")
    message = str(excinfo.value)
    assert "nonexistent" in message
    assert "fake" in message  # the message lists the registered names


def test_get_harness_on_empty_registry_raises_instead_of_returning():
    harness._HARNESSES.clear()
    with pytest.raises(ValueError):
        get_harness("anything")
    with pytest.raises(ValueError):
        get_harness("")
    # Fail closed: no default harness is ever substituted for an unknown name,
    # and a failed lookup must not lazily repopulate the registry.
    assert harness._HARNESSES == {}


def test_get_harness_empty_and_whitespace_only_names_fail_closed():
    register_harness("fake", FakeHarness)
    for bad_name in ("", "   ", "\t", " \n "):
        with pytest.raises(ValueError):
            get_harness(bad_name)


# ---------------------------------------------------------------------------
# Name normalization: .strip().lower() on both register and get
# ---------------------------------------------------------------------------


def test_register_and_get_normalize_names_with_strip_and_lower():
    register_harness("  Fake-CASE  ", FakeHarness)
    assert harness._HARNESSES.get("fake-case") is FakeHarness
    for probe in ("fake-case", "FAKE-CASE", "  fake-case  ", "Fake-Case", "\tFAKE-case\n"):
        assert isinstance(get_harness(probe), FakeHarness), probe


def test_normalization_spec_example_claude_and_whitespace_padded():
    # The spec's literal example is 'Claude' / ' claude '. 'claude' is also the
    # name a LATER sibling story registers, so if this process already holds a
    # non-Fake 'claude', defer to the neutral-name normalization test above
    # rather than fighting the cumulative registry.
    existing = harness._HARNESSES.get("claude")
    if existing is not None and existing is not FakeHarness:
        pytest.skip(
            "'claude' already registered by a sibling story in this process; "
            "normalization is covered by "
            "test_register_and_get_normalize_names_with_strip_and_lower"
        )
    register_harness("Claude", FakeHarness)
    assert isinstance(get_harness(" claude "), FakeHarness)
    assert isinstance(get_harness("CLAUDE"), FakeHarness)


# ---------------------------------------------------------------------------
# Duplicate registration: idempotent for the same class, loud for a different one
# ---------------------------------------------------------------------------


def test_reregistering_the_identical_class_is_an_idempotent_no_op():
    register_harness("dup", FakeHarness)
    register_harness("dup", FakeHarness)  # must not raise
    assert harness._HARNESSES.get("dup") is FakeHarness


def test_reregistering_a_different_class_raises_value_error_naming_the_key():
    register_harness("dup", FakeHarness)
    with pytest.raises(ValueError) as excinfo:
        register_harness("dup", OtherHarness)
    assert "dup" in str(excinfo.value)
    assert harness._HARNESSES.get("dup") is FakeHarness  # original wins


def test_duplicate_detection_uses_the_normalized_name():
    register_harness("dup", FakeHarness)
    with pytest.raises(ValueError):
        register_harness(" DUP ", OtherHarness)
    with pytest.raises(ValueError):
        register_harness("Dup", OtherHarness)
    assert harness._HARNESSES.get("dup") is FakeHarness


# ---------------------------------------------------------------------------
# Module hygiene: stdlib-only imports, no backend/local_agent import cycle
# ---------------------------------------------------------------------------


def _imported_root_modules(source: str) -> list[str]:
    roots: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.append("<relative-import>")
            elif node.module:
                roots.append(node.module.split(".")[0])
    return roots


def test_harness_module_imports_only_the_stdlib():
    source = Path(harness.__file__).read_text(encoding="utf-8")
    roots = _imported_root_modules(source)
    assert roots, "app/harness.py must import dataclasses/typing"
    stdlib_roots = set(getattr(sys, "stdlib_module_names", ()) ) or {
        "__future__",
        "dataclasses",
        "typing",
    }
    for root in roots:
        assert root in stdlib_roots, (
            f"app/harness.py imports {root!r}; it may import ONLY the stdlib "
            "+ typing/dataclasses"
        )
    for root in ("app", "pipeline", "scripts"):
        assert root not in roots, (
            f"app/harness.py must never import {root!r}.* (no import cycles, ever)"
        )


def test_harness_module_never_imports_backend_or_local_agent_modules():
    source = Path(harness.__file__).read_text(encoding="utf-8")
    forbidden = (
        "app.backend",
        "backend_claude",
        "backend_ollama",
        "scripts.local_agent",
        "local_agent",
    )
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            text = ast.get_source_segment(source, node) or ""
            for token in forbidden:
                assert token not in text, (
                    f"app/harness.py must never import {token!r}; got {text!r}"
                )


def test_module_exports_the_documented_api():
    for name in (
        "HarnessRequest",
        "HarnessCommand",
        "AgentHarness",
        "register_harness",
        "get_harness",
        "_HARNESSES",
    ):
        assert hasattr(harness, name), f"app.harness is missing {name}"
    assert isinstance(harness._HARNESSES, dict)