"""Tests for the pure Plane ticketing projections in ``pipeline.setup``.

``build_plane_settings`` and ``describe_plane_status`` are pure: no network,
no filesystem, no ``os.environ``.  These tests pin the opt-in contract (Plane
is off unless explicitly enabled), the all-at-once validation message, and the
rule that the API key never reaches user-facing text.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from pipeline.setup import integrations
from pipeline.setup.integrations import build_plane_settings, describe_plane_status

SENTINEL = "plane-key-sentinel"

PLANE_KEYS = ("PLANE_BASE", "PLANE_API_KEY", "PLANE_WORKSPACE", "PLANE_PROJECT")
ALL_KEYS = frozenset((*PLANE_KEYS, "PIPELINE_TICKET_PROVIDER"))

COMPLETE = {
    "enabled": True,
    "base": "https://plane.example.com",
    "api_key": SENTINEL,
    "workspace": "acme",
    "project": "pipeline",
    "ticket_provider": "plane",
}


# --------------------------------------------------------------------------
# Positive
# --------------------------------------------------------------------------


def test_disabled_returns_exact_key_set_with_empty_plane_values():
    settings = build_plane_settings(enabled=False)

    assert set(settings) == ALL_KEYS
    for key in PLANE_KEYS:
        assert settings[key] == ""
    assert settings["PIPELINE_TICKET_PROVIDER"] == "none"


def test_disabled_clears_previously_supplied_credentials():
    settings = build_plane_settings(
        enabled=False,
        base="https://plane.example.com",
        api_key=SENTINEL,
        workspace="acme",
        project="pipeline",
        ticket_provider="auto",
    )

    assert set(settings) == ALL_KEYS
    for key in PLANE_KEYS:
        assert settings[key] == ""
    assert settings["PIPELINE_TICKET_PROVIDER"] == "none"
    assert SENTINEL not in repr(settings)


def test_disabled_tolerates_partial_input():
    settings = build_plane_settings(enabled=False, base="https://plane.example.com")

    assert settings["PLANE_BASE"] == ""
    assert settings["PIPELINE_TICKET_PROVIDER"] == "none"


def test_enabled_complete_returns_values_and_strips_trailing_slash():
    settings = build_plane_settings(**{**COMPLETE, "base": "https://plane.example.com/"})

    assert settings == {
        "PLANE_BASE": "https://plane.example.com",
        "PLANE_API_KEY": SENTINEL,
        "PLANE_WORKSPACE": "acme",
        "PLANE_PROJECT": "pipeline",
        "PIPELINE_TICKET_PROVIDER": "plane",
    }


def test_enabled_complete_returns_api_key_verbatim():
    settings = build_plane_settings(**COMPLETE)

    assert settings["PLANE_API_KEY"] == SENTINEL


def test_provider_auto_with_empty_credentials_is_accepted():
    settings = build_plane_settings(enabled=False, ticket_provider="auto")

    assert settings["PIPELINE_TICKET_PROVIDER"] == "none"
    for key in PLANE_KEYS:
        assert settings[key] == ""


def test_provider_none_with_complete_credentials_stays_disabled():
    settings = build_plane_settings(**{**COMPLETE, "ticket_provider": "none"})

    assert settings["PIPELINE_TICKET_PROVIDER"] == "none"


def test_provider_is_lowercased():
    settings = build_plane_settings(**{**COMPLETE, "ticket_provider": "PLANE"})

    assert settings["PIPELINE_TICKET_PROVIDER"] == "plane"


def test_describe_plane_status_names_workspace_and_project_when_configured():
    settings = build_plane_settings(**COMPLETE)

    status = describe_plane_status(settings)

    assert status == "Plane configured for acme/pipeline (API key hidden)"


def test_describe_plane_status_says_disabled_when_provider_none():
    settings = build_plane_settings(**COMPLETE)
    settings["PIPELINE_TICKET_PROVIDER"] = "none"

    assert describe_plane_status(settings) == "Plane disabled"


def test_describe_plane_status_says_disabled_when_api_key_empty():
    settings = build_plane_settings(**COMPLETE)
    settings["PLANE_API_KEY"] = ""

    assert describe_plane_status(settings) == "Plane disabled"


def test_describe_plane_status_says_disabled_for_empty_mapping():
    assert describe_plane_status({}) == "Plane disabled"


# --------------------------------------------------------------------------
# Negative
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", PLANE_KEYS)
def test_enabled_with_one_missing_field_names_that_key(missing):
    payload = {key.lower().removeprefix("plane_"): value for key, value in COMPLETE.items()}
    payload[missing.lower().removeprefix("plane_")] = ""

    with pytest.raises(ValueError) as excinfo:
        build_plane_settings(**payload)

    message = str(excinfo.value)
    assert missing in message
    for other in PLANE_KEYS:
        if other != missing:
            assert other not in message


def test_enabled_with_every_field_missing_names_all_four_keys():
    with pytest.raises(ValueError) as excinfo:
        build_plane_settings(enabled=True)

    message = str(excinfo.value)
    for key in PLANE_KEYS:
        assert key in message


def test_enabled_with_two_missing_fields_names_both_keys():
    with pytest.raises(ValueError) as excinfo:
        build_plane_settings(enabled=True, base="https://plane.example.com", api_key=SENTINEL)

    message = str(excinfo.value)
    assert "PLANE_WORKSPACE" in message
    assert "PLANE_PROJECT" in message
    assert "PLANE_BASE" not in message
    assert "PLANE_API_KEY" not in message


@pytest.mark.parametrize("base", ["ftp://x", "plane.example.com", "//plane.example.com"])
def test_enabled_with_invalid_base_raises(base):
    with pytest.raises(ValueError) as excinfo:
        build_plane_settings(**{**COMPLETE, "base": base})

    assert "PLANE_BASE" in str(excinfo.value)


@pytest.mark.parametrize("field", ["api_key", "workspace", "project"])
def test_enabled_with_whitespace_only_field_raises(field):
    with pytest.raises(ValueError) as excinfo:
        build_plane_settings(**{**COMPLETE, field: "   "})

    assert f"PLANE_{field.upper()}" in str(excinfo.value)


def test_provider_plane_with_enabled_false_raises():
    with pytest.raises(ValueError) as excinfo:
        build_plane_settings(enabled=False, ticket_provider="plane")

    assert "plane" in str(excinfo.value).lower()


def test_provider_jira_raises_actionable_message():
    with pytest.raises(ValueError) as excinfo:
        build_plane_settings(**{**COMPLETE, "ticket_provider": "jira"})

    message = str(excinfo.value)
    assert "jira" in message.lower()
    assert "manual" in message.lower()


def test_provider_github_raises():
    with pytest.raises(ValueError) as excinfo:
        build_plane_settings(**{**COMPLETE, "ticket_provider": "github"})

    assert "github" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# Boundary
# --------------------------------------------------------------------------


def test_base_with_multiple_trailing_slashes_is_fully_stripped():
    settings = build_plane_settings(**{**COMPLETE, "base": "https://plane.example.com///"})

    assert settings["PLANE_BASE"] == "https://plane.example.com"


def test_http_base_is_accepted():
    settings = build_plane_settings(**{**COMPLETE, "base": "http://localhost:8080"})

    assert settings["PLANE_BASE"] == "http://localhost:8080"


def test_single_character_workspace_and_project_are_accepted():
    settings = build_plane_settings(**{**COMPLETE, "workspace": "a", "project": "b"})

    assert settings["PLANE_WORKSPACE"] == "a"
    assert settings["PLANE_PROJECT"] == "b"


def test_api_key_sentinel_never_appears_in_validation_errors():
    bad_payloads = [
        {**COMPLETE, "base": "ftp://x"},
        {**COMPLETE, "workspace": ""},
        {**COMPLETE, "project": ""},
        {**COMPLETE, "ticket_provider": "jira"},
        {**COMPLETE, "ticket_provider": "github"},
        {"enabled": True, "api_key": SENTINEL},
    ]

    for payload in bad_payloads:
        with pytest.raises(ValueError) as excinfo:
            build_plane_settings(**payload)
        assert SENTINEL not in str(excinfo.value)


def test_api_key_sentinel_never_appears_in_status_text():
    settings = build_plane_settings(**COMPLETE)

    status = describe_plane_status(settings)

    assert SENTINEL not in status
    assert "acme" in status
    assert "pipeline" in status


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def _module_tree() -> ast.Module:
    source = Path(inspect.getfile(integrations)).read_text(encoding="utf-8")
    return ast.parse(source)


def test_module_imports_no_network_libraries():
    forbidden = {"urllib", "requests", "socket", "http", "httpx", "aiohttp"}
    imported: set[str] = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not (imported & forbidden)


def test_module_never_reads_os_environ():
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Attribute):
            assert node.attr != "environ"
        if isinstance(node, ast.Name):
            assert node.id != "environ"
