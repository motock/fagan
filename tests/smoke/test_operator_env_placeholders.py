"""Placeholder credentials must never start the standalone stack.

``deploy/standalone/.env.example`` is copied to ``.env`` by hand, so its values
are the defaults a careless install runs with. Compose only rejects an *empty*
required value (the ``:?`` guards in ``deploy/standalone/compose.yaml``); a
published non-empty constant such as ``change-me-mcp-client-secret`` satisfies
every guard and is then substituted verbatim into ``realm.json`` at import, so
the confidential MCP client would ship with a secret printed in this repository.
Keycloak's bootstrap admin password has the same problem, and a token minted
with that client secret grants the full remote MCP tool surface.

Two independent fail-closed checks are asserted here, because either one alone
leaves a gap:

- the example keeps the three credential values empty, so the compose ``:?``
  guard fires and the stack refuses to start until the installer fills them in;
- ``deploy/lib/operator_env.sh`` refuses a ``change-me`` placeholder it is
  handed through the environment, so a hand-edited ``.env`` (or a shared env
  file in another orchestrator) cannot start either.

The refusal names the variable, never its value: a log line is not a place for a
credential, and the shell code under test follows that rule (Secure by Design).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "deploy" / "entrypoint.sh"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "standalone" / ".env.example"

TIMEOUT_SECONDS = 60

#: Compose requires these through ``:?`` and realm.json substitutes them at
#: import, so a placeholder here is a working credential.
CREDENTIAL_VARS = (
    "KEYCLOAK_ADMIN_PASSWORD",
    "PIPELINE_DASHBOARD_CLIENT_SECRET",
    "PIPELINE_MCP_CLIENT_SECRET",
)

#: Source the library the entrypoint sources, then run the guard. Sourced from
#: the repository root so the relative path resolves in every runner.
GUARD = ". deploy/lib/operator_env.sh; assert_no_placeholder_credentials"


def _run_guard(**env: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped placeholder guard with ``env`` layered over a clean base."""
    base = {
        key: value
        for key, value in os.environ.items()
        if key not in CREDENTIAL_VARS
    }
    base["PATH"] = base.get("PATH", "/usr/bin:/bin")
    base.update(env)
    return subprocess.run(
        [shutil.which("sh") or "/bin/sh", "-c", GUARD],
        cwd=REPO_ROOT,
        check=False,
        env=base,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )


def _assignments(text: str, name: str) -> list[str]:
    """Values assigned to ``name`` (``NAME=`` lines only), unquoted."""
    values = []
    for match in re.finditer(rf"^\s*{name}=(.*)$", text, re.MULTILINE):
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values.append(value)
    return values


@pytest.mark.parametrize("name", CREDENTIAL_VARS)
def test_placeholder_credential_in_the_environment_is_refused(name: str) -> None:
    result = _run_guard(**{name: "change-me-something"})

    assert result.returncode != 0, (
        f"a published placeholder in {name} must abort the start; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert name in result.stderr, (
        f"the refusal must name {name} so the operator can find it: "
        f"{result.stderr!r}"
    )
    assert "change-me-something" not in result.stderr, (
        "the refusal must never echo the credential value"
    )


@pytest.mark.parametrize("name", CREDENTIAL_VARS)
def test_case_and_whitespace_placeholder_variants_are_refused(name: str) -> None:
    """A hand-edited ``.env`` is not required to match the example byte for byte."""
    for value in ("CHANGE-ME-uppercase", "  change-me-padded  ", "Change-Me-mixed"):
        result = _run_guard(**{name: value})

        assert result.returncode != 0, f"{value!r} in {name} must abort"
        assert name in result.stderr
        assert "change-me" not in result.stderr.lower().replace(name.lower(), "")


@pytest.mark.parametrize("name", CREDENTIAL_VARS)
def test_unset_or_empty_credential_is_left_to_compose(name: str) -> None:
    """The library does not own presence: compose's ``:?`` guard does.

    The example ships these empty, so an unset or empty value must reach the
    compose error ("set ... in .env") rather than a different, confusing one.
    """
    assert _run_guard().returncode == 0
    assert _run_guard(**{name: ""}).returncode == 0
    assert _run_guard(**{name: "   "}).returncode == 0


def test_generated_credentials_pass_the_guard() -> None:
    generated = {
        "KEYCLOAK_ADMIN_PASSWORD": "Zq7-tR2vXw9pLm4n",
        "PIPELINE_DASHBOARD_CLIENT_SECRET": "8f1c0b6e5a4d3c2b1a09",
        "PIPELINE_MCP_CLIENT_SECRET": "0a1b2c3d4e5f60718293",
    }

    result = _run_guard(**generated)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_env_example_ships_the_required_credentials_empty() -> None:
    env = ENV_EXAMPLE.read_text(encoding="utf-8")

    for name in CREDENTIAL_VARS:
        values = _assignments(env, name)
        assert values == [""], (
            f"deploy/standalone/.env.example must list {name}= with no value, so "
            f"compose's ':?' guard refuses to start until the installer generates "
            f"one; found {values!r}"
        )


def test_env_example_carries_no_placeholder_secret_value() -> None:
    env = ENV_EXAMPLE.read_text(encoding="utf-8")

    placeholders = [
        match.group(0)
        for match in re.finditer(
            r"^[A-Z0-9_]*(?:SECRET|PASSWORD|TOKEN)[A-Z0-9_]*=[ \t]*(\S+)[ \t]*$",
            env,
            re.MULTILINE,
        )
    ]
    assert placeholders == [], (
        "a non-empty secret value in the example is a published credential: "
        f"{placeholders!r}"
    )
    assert "change-me" not in env, (
        "drop the 'change-me' constants entirely: every copy-paste install would "
        "run with a repository-published secret"
    )


def test_entrypoint_runs_the_placeholder_guard_before_any_child_starts() -> None:
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")

    assert "assert_no_placeholder_credentials" in entrypoint, (
        "deploy/entrypoint.sh must call the placeholder guard; an uncalled guard "
        "is not a guard"
    )
    guard_at = entrypoint.index("assert_no_placeholder_credentials")
    for child in ("starting dashboard", "starting MCP transport"):
        assert guard_at < entrypoint.index(child), (
            f"the guard must run before {child!r}"
        )
