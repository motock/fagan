"""Tests for the declarative guided-setup schema (``pipeline.setup.schema``).

The schema is the single source of truth for which pipeline configuration keys
the guided setup offers, what each one defaults to on a fresh install, and how
a raw string typed at a prompt is validated and normalized before it is
written to the env file.

Everything here is pure: no network, no subprocess in production code, no
filesystem access at import time. The one subprocess below exists only to
prove that last claim from a clean interpreter.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.setup import schema

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_DIR = REPO_ROOT / "pipeline" / "setup"

BACKEND_ROLES = (
    "OVERLORD",
    "PLANNER",
    "DISPATCH",
    "REVIEW",
    "DECOMPOSE",
    "TEST_AUTHOR",
    "DIAGNOSIS",
    "SECURITY",
    "CHAT",
)

REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "core": (
        "REPO_ROOT",
        "PLAN_DIR",
        "WORKTREE_ROOT",
        "AGENTS_DIR",
        "PIPELINE_MODEL_REGISTRY_PATH",
    ),
    "routing": tuple(f"PIPELINE_BACKEND_{role}" for role in BACKEND_ROLES),
    "policy": (
        "PIPELINE_AUTONOMY",
        "PIPELINE_RISK_THRESHOLD",
        "PIPELINE_MAX_CONCURRENT_AGENTS",
        "PIPELINE_DAILY_REQUEST_THRESHOLD",
        "PIPELINE_WEEKLY_REQUEST_THRESHOLD",
        "PIPELINE_PAUSE_THRESHOLD",
        "PIPELINE_RESUME_THRESHOLD",
        "PIPELINE_ESCALATION_BACKEND",
        "PIPELINE_ESCALATION_MODEL",
        "PIPELINE_SECURITY_AUDIT_EVERY_DAYS",
        "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS",
        "PIPELINE_AUTO_MERGE",
    ),
    "notifications": (
        "PIPELINE_NOTIFY_OUTBOX_ENABLED",
        "PIPELINE_NOTIFY_OUTBOX_EVENTS",
        "PIPELINE_NOTIFY_EMAIL_ENABLED",
        "PIPELINE_NOTIFY_EMAIL_HOST",
        "PIPELINE_NOTIFY_EMAIL_PORT",
        "PIPELINE_NOTIFY_EMAIL_USER",
        "PIPELINE_NOTIFY_EMAIL_PASSWORD",
        "PIPELINE_NOTIFY_EMAIL_FROM",
        "PIPELINE_NOTIFY_EMAIL_TO",
        "PIPELINE_NOTIFY_EMAIL_TIMEOUT",
    ),
    "integrations": (
        "PLANE_BASE",
        "PLANE_API_KEY",
        "PLANE_WORKSPACE",
        "PLANE_PROJECT",
        "PIPELINE_TICKET_PROVIDER",
    ),
}

ALL_REQUIRED_KEYS = tuple(name for names in REQUIRED_KEYS.values() for name in names)

# The exact fresh-install default table. ``None`` means "leave unset".
EXPECTED_DEFAULTS: dict[str, str | None] = {
    # core
    "REPO_ROOT": None,
    "PLAN_DIR": "~/.claude/plans",
    "WORKTREE_ROOT": "~/.claude/worktrees",
    "AGENTS_DIR": "~/.claude/agents",
    "PIPELINE_MODEL_REGISTRY_PATH": None,
    # routing: never override the code's own backend resolution
    **{f"PIPELINE_BACKEND_{role}": None for role in BACKEND_ROLES},
    # policy
    "PIPELINE_AUTONOMY": "dry-run",
    "PIPELINE_RISK_THRESHOLD": "low",
    "PIPELINE_MAX_CONCURRENT_AGENTS": "3",
    "PIPELINE_DAILY_REQUEST_THRESHOLD": "3000",
    "PIPELINE_WEEKLY_REQUEST_THRESHOLD": "15000",
    "PIPELINE_PAUSE_THRESHOLD": "90",
    "PIPELINE_RESUME_THRESHOLD": "70",
    "PIPELINE_ESCALATION_BACKEND": None,
    "PIPELINE_ESCALATION_MODEL": None,
    "PIPELINE_SECURITY_AUDIT_EVERY_DAYS": "0",
    "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS": "0",
    "PIPELINE_AUTO_MERGE": "1",
    # notifications
    "PIPELINE_NOTIFY_OUTBOX_ENABLED": "0",
    "PIPELINE_NOTIFY_OUTBOX_EVENTS": (
        "plan_completed,story_parked,dispatch_failed,tests_failed,agent_gave_up"
    ),
    "PIPELINE_NOTIFY_EMAIL_ENABLED": "0",
    "PIPELINE_NOTIFY_EMAIL_HOST": "",
    "PIPELINE_NOTIFY_EMAIL_PORT": "587",
    "PIPELINE_NOTIFY_EMAIL_USER": "",
    "PIPELINE_NOTIFY_EMAIL_PASSWORD": "",
    "PIPELINE_NOTIFY_EMAIL_FROM": "",
    "PIPELINE_NOTIFY_EMAIL_TO": "",
    "PIPELINE_NOTIFY_EMAIL_TIMEOUT": "20",
    # integrations
    "PLANE_BASE": "",
    "PLANE_API_KEY": "",
    "PLANE_WORKSPACE": "",
    "PLANE_PROJECT": "",
    "PIPELINE_TICKET_PROVIDER": "none",
}

SECRET_KEYS = {"PLANE_API_KEY", "PIPELINE_NOTIFY_EMAIL_PASSWORD"}


# --------------------------------------------------------------------------
# Catalogue shape
# --------------------------------------------------------------------------


def test_sections_are_the_documented_five():
    assert schema.SECTIONS == (
        "core",
        "routing",
        "policy",
        "notifications",
        "integrations",
    )


@pytest.mark.parametrize("section", sorted(REQUIRED_KEYS))
def test_required_keys_present_exactly_once_in_their_section(section):
    occurrences: dict[str, list[schema.KeySpec]] = {}
    for spec in schema.SCHEMA:
        occurrences.setdefault(spec.name, []).append(spec)

    for name in REQUIRED_KEYS[section]:
        assert name in occurrences, f"{name} missing from SCHEMA"
        assert len(occurrences[name]) == 1, f"{name} appears {len(occurrences[name])}x"
        assert occurrences[name][0].section == section, (
            f"{name} is in section {occurrences[name][0].section!r}, expected {section!r}"
        )


def test_schema_has_no_keys_outside_the_required_catalogue():
    assert {spec.name for spec in schema.SCHEMA} == set(ALL_REQUIRED_KEYS)


def test_every_section_has_at_least_one_key():
    for section in schema.SECTIONS:
        assert schema.keys_for_section(section), f"section {section!r} is empty"


def test_keys_for_section_preserves_schema_order():
    for section in schema.SECTIONS:
        expected = tuple(spec for spec in schema.SCHEMA if spec.section == section)
        assert schema.keys_for_section(section) == expected


def test_keys_for_section_unknown_section_raises_naming_valid_sections():
    with pytest.raises(ValueError) as excinfo:
        schema.keys_for_section("nope")
    message = str(excinfo.value)
    for section in schema.SECTIONS:
        assert section in message


def test_keyspec_is_frozen_with_the_documented_fields():
    field_names = tuple(field.name for field in dataclasses.fields(schema.KeySpec))
    assert field_names == (
        "name",
        "section",
        "default",
        "kind",
        "choices",
        "secret",
        "prompt",
    )
    spec = schema.SCHEMA[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "MUTATED"  # type: ignore[misc]


def test_only_the_two_secret_keys_are_flagged_secret():
    assert {spec.name for spec in schema.SCHEMA if spec.secret} == SECRET_KEYS


def test_pipeline_auto_merge_is_a_policy_bool_defaulting_to_one():
    spec = next(spec for spec in schema.SCHEMA if spec.name == "PIPELINE_AUTO_MERGE")
    assert spec.section == "policy"
    assert spec.kind == "bool"
    assert spec.default == "1"


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_DEFAULTS))
def test_default_for_matches_the_fresh_install_table(name):
    assert schema.default_for(name) == EXPECTED_DEFAULTS[name]


def test_default_for_returns_the_raw_unexpanded_default():
    assert schema.default_for("PLAN_DIR") == "~/.claude/plans"


def test_default_for_unknown_key_raises():
    with pytest.raises(ValueError):
        schema.default_for("NOT_A_SETUP_KEY")


@pytest.mark.parametrize(
    "spec", [spec for spec in schema.SCHEMA if spec.default is not None], ids=lambda s: s.name
)
def test_every_non_none_default_validates(spec):
    ok, normalized = schema.validate_value(spec.name, spec.default)
    assert ok, f"default {spec.default!r} for {spec.name} rejected: {normalized}"
    assert isinstance(normalized, str)


# --------------------------------------------------------------------------
# Positive validation / normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", "1"),
        ("TRUE", "1"),
        ("YES", "1"),
        ("on", "1"),
        ("1", "1"),
        ("false", "0"),
        ("off", "0"),
        ("No", "0"),
        ("0", "0"),
        ("  yes  ", "1"),
    ],
)
def test_bool_normalization(raw, expected):
    assert schema.validate_value("PIPELINE_AUTO_MERGE", raw) == (True, expected)
    assert schema.validate_value("PIPELINE_NOTIFY_OUTBOX_ENABLED", raw) == (True, expected)


def test_csv_normalization_strips_and_drops_empty_segments():
    assert schema.validate_value("PIPELINE_NOTIFY_OUTBOX_EVENTS", " a, b ,,c ") == (
        True,
        "a,b,c",
    )


def test_path_expands_tilde_under_monkeypatched_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ok, normalized = schema.validate_value("PLAN_DIR", "~/.claude")
    assert ok
    assert normalized == str(tmp_path / ".claude")
    assert os.path.isabs(normalized)


def test_absolute_path_is_returned_unchanged(tmp_path):
    target = str(tmp_path / "plans")
    assert schema.validate_value("PLAN_DIR", target) == (True, target)


def test_enum_is_case_and_whitespace_insensitive():
    assert schema.validate_value("PIPELINE_AUTONOMY", "  GATED ") == (True, "gated")
    assert schema.validate_value("PIPELINE_TICKET_PROVIDER", "PLANE") == (True, "plane")


def test_escalation_backend_accepts_empty_as_unset():
    assert schema.validate_value("PIPELINE_ESCALATION_BACKEND", "") == (True, "")


def test_int_strips_surrounding_whitespace():
    assert schema.validate_value("PIPELINE_MAX_CONCURRENT_AGENTS", " 4 ") == (True, "4")


# --------------------------------------------------------------------------
# Negative validation
# --------------------------------------------------------------------------


def test_unknown_key_returns_false_and_does_not_raise():
    ok, message = schema.validate_value("NOT_A_SETUP_KEY", "whatever")
    assert ok is False
    assert "unknown setup key" in message.lower()


@pytest.mark.parametrize(
    "name,raw",
    [
        ("PIPELINE_AUTONOMY", "yolo"),
        ("PIPELINE_RISK_THRESHOLD", "critical"),
        ("PIPELINE_MAX_CONCURRENT_AGENTS", "0"),
        ("PIPELINE_MAX_CONCURRENT_AGENTS", "two"),
        ("PIPELINE_PAUSE_THRESHOLD", "101"),
        ("PIPELINE_RESUME_THRESHOLD", "-1"),
        ("PIPELINE_NOTIFY_EMAIL_PORT", "0"),
        ("PIPELINE_NOTIFY_EMAIL_PORT", "65536"),
        ("PIPELINE_TICKET_PROVIDER", "github"),
        ("PLANE_BASE", "ftp://x"),
        ("PLANE_BASE", "plane.example.com"),
        ("PLAN_DIR", "relative/dir"),
        ("PIPELINE_NOTIFY_EMAIL_FROM", "no-at"),
        ("PIPELINE_NOTIFY_EMAIL_FROM", "a@b"),
        ("PIPELINE_NOTIFY_OUTBOX_EVENTS", "  "),
        ("PIPELINE_NOTIFY_OUTBOX_EVENTS", "Bad-Token"),
        ("PIPELINE_AUTO_MERGE", "maybe"),
        ("PIPELINE_ESCALATION_BACKEND", "gpt"),
        ("PIPELINE_BACKEND_DISPATCH", "gpt"),
        ("PIPELINE_SECURITY_AUDIT_EVERY_DAYS", "-1"),
        ("PIPELINE_DAILY_REQUEST_THRESHOLD", "0"),
        ("PIPELINE_WEEKLY_REQUEST_THRESHOLD", "0"),
        ("PIPELINE_NOTIFY_EMAIL_TIMEOUT", "0"),
    ],
)
def test_invalid_values_are_rejected_with_a_message(name, raw):
    ok, message = schema.validate_value(name, raw)
    assert ok is False
    assert isinstance(message, str) and message.strip(), "rejection needs an actionable message"


def test_rejection_message_names_the_key():
    ok, message = schema.validate_value("PIPELINE_AUTONOMY", "yolo")
    assert ok is False
    assert "PIPELINE_AUTONOMY" in message


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,raw,expected",
    [
        ("PIPELINE_PAUSE_THRESHOLD", "0", "0"),
        ("PIPELINE_PAUSE_THRESHOLD", "100", "100"),
        ("PIPELINE_RESUME_THRESHOLD", "0", "0"),
        ("PIPELINE_RESUME_THRESHOLD", "100", "100"),
        ("PIPELINE_MAX_CONCURRENT_AGENTS", "1", "1"),
        ("PIPELINE_SECURITY_AUDIT_EVERY_DAYS", "0", "0"),
        ("PIPELINE_SECURITY_AUDIT_EVERY_COMMITS", "0", "0"),
        ("PIPELINE_NOTIFY_EMAIL_PORT", "1", "1"),
        ("PIPELINE_NOTIFY_EMAIL_PORT", "65535", "65535"),
        ("PIPELINE_NOTIFY_EMAIL_TIMEOUT", "1", "1"),
        ("PIPELINE_DAILY_REQUEST_THRESHOLD", "1", "1"),
        ("PIPELINE_WEEKLY_REQUEST_THRESHOLD", "1", "1"),
    ],
)
def test_boundary_values_are_accepted(name, raw, expected):
    assert schema.validate_value(name, raw) == (True, expected)


def test_empty_string_is_allowed_for_an_optional_email_field():
    assert schema.validate_value("PIPELINE_NOTIFY_EMAIL_TO", "") == (True, "")


def test_huge_int_input_is_rejected_without_raising():
    ok, message = schema.validate_value("PIPELINE_MAX_CONCURRENT_AGENTS", "9" * 10_000)
    assert ok is False
    assert message


@pytest.mark.parametrize("name", ALL_REQUIRED_KEYS)
@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\x00", "\ud800", "x" * 10_000, "1e999", "-", "~", "a@b@c"],
)
def test_validate_value_never_raises_for_any_string(name, raw):
    result = schema.validate_value(name, raw)
    assert isinstance(result, tuple) and len(result) == 2
    ok, message = result
    assert isinstance(ok, bool)
    assert isinstance(message, str)


@pytest.mark.parametrize("name", sorted(SECRET_KEYS))
@pytest.mark.parametrize("raw", ["", "super-secret-value", "x" * 10_000])
def test_secret_values_never_appear_in_messages(name, raw):
    ok, message = schema.validate_value(name, raw)
    if not ok and raw:
        assert raw not in message


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


def test_setup_package_init_is_a_docstring_only():
    tree = ast.parse((SETUP_DIR / "__init__.py").read_text(encoding="utf-8"))
    assert len(tree.body) == 1
    node = tree.body[0]
    assert isinstance(node, ast.Expr)
    assert isinstance(node.value, ast.Constant)
    assert isinstance(node.value.value, str)


def test_schema_imports_only_stdlib_roots():
    allowed = set(sys.stdlib_module_names)
    for path in (SETUP_DIR / "__init__.py", SETUP_DIR / "schema.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= allowed, f"{path.name} imports non-stdlib roots: {roots - allowed}"


def test_importing_schema_in_a_temp_cwd_touches_no_file_and_no_environ(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    code = (
        "import json, os\n"
        "before = dict(os.environ)\n"
        "import pipeline.setup.schema as s\n"
        "after = dict(os.environ)\n"
        "print(json.dumps({'changed': before != after, 'sections': list(s.SECTIONS)}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["changed"] is False, "importing the schema mutated os.environ"
    assert payload["sections"] == list(schema.SECTIONS)
    assert list(tmp_path.iterdir()) == [], "importing the schema wrote into the cwd"
