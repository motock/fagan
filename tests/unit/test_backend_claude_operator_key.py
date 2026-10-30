"""An operator-supplied PIPELINE_ANTHROPIC_API_KEY reaches the `claude` CLI
as ANTHROPIC_API_KEY while every provider-redirect var stays stripped.

The operator key is a mounted, long-lived credential: it must be converted to
ANTHROPIC_API_KEY and *not* carried under its PIPELINE_ name, because the env
dict produced here is handed straight to `spawn_harness`, whose child env is
what a prompt-injectable agent can read (the SEC-2 leak class).
"""
import sys
import time

import pytest

from app.backend_claude import _first_party_claude_env
from pipeline import execution

# Env vars reset for every case. This is a clean-environment fixture, NOT a
# claim that every name here is stripped: only the ANTHROPIC_*/CLAUDE_CODE_USE_*
# redirect vars are stripped. PIPELINE_ANTHROPIC_API_KEY is converted (and
# removed), CLAUDE_CODE_OAUTH_TOKEN passes through, and
# PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV is the documented opt-out.
_ENV_VARS_TO_RESET = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "PIPELINE_ANTHROPIC_API_KEY", "PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

# Prints the child's environment *variable names only* -- never a value --
# so the assertion cannot ever echo a credential into the test log.
_DUMP_ENV_NAMES = (
    "import os; print('ENV_KEYS:' + ' '.join(sorted(os.environ)), flush=True)"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS_TO_RESET:
        monkeypatch.delenv(var, raising=False)


def test_should_pass_operator_key_as_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-test"


def test_should_strip_base_url_when_operator_key_set(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")

    env = _first_party_claude_env()

    assert "ANTHROPIC_BASE_URL" not in env


@pytest.mark.parametrize("var", [
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
])
def test_should_still_strip_redirect_var_when_operator_key_set(monkeypatch, var):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv(var, "redirect")

    env = _first_party_claude_env()

    assert var not in env


def test_should_strip_ambient_api_key_without_operator_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")

    env = _first_party_claude_env()

    assert "ANTHROPIC_API_KEY" not in env


def test_should_prefer_operator_key_over_ambient_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "op")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "op"


def test_should_strip_surrounding_whitespace_from_operator_key(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "  sk-test\n")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-test"


def test_should_not_add_api_key_for_blank_operator_key(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "   ")

    env = _first_party_claude_env()

    assert "ANTHROPIC_API_KEY" not in env


def test_should_pass_oauth_token_through_unchanged(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    env = _first_party_claude_env()

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"


# --- The operator secret must not reach the agent as PIPELINE_* -------------
# `_first_party_claude_env()`'s dict is passed verbatim as `env=` to
# `spawn_harness`, so a leftover PIPELINE_ANTHROPIC_API_KEY would hand every
# dispatched (prompt-injectable) agent the operator's long-lived key.

def test_should_not_expose_pipeline_operator_key_var(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")

    env = _first_party_claude_env()

    assert "PIPELINE_ANTHROPIC_API_KEY" not in env


def test_agent_secret_denylist_covers_pipeline_operator_key():
    """Defense in depth: even if some caller forwards the raw environment,
    spawn_harness's SEC-2 denylist drops the operator secret by name."""
    assert "PIPELINE_ANTHROPIC_API_KEY" in execution._AGENT_SECRET_ENV


def test_local_agent_env_keeps_converted_key_only(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")

    child_env = execution._local_agent_env(_first_party_claude_env())

    assert child_env["ANTHROPIC_API_KEY"] == "sk-test"
    assert "PIPELINE_ANTHROPIC_API_KEY" not in child_env


def test_spawn_harness_child_env_carries_converted_key_only(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("PIPELINE_EXEC_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_SANDBOX", "none")

    log_path = tmp_path / "agent.log"
    execution.spawn_harness(
        [sys.executable, "-c", _DUMP_ENV_NAMES],
        cwd=tmp_path,
        log_path=log_path,
        append=False,
        env=_first_party_claude_env(),
        role="dispatch",
    )

    names = set()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        if "ENV_KEYS:" in text:
            line = next(
                ln for ln in text.splitlines() if ln.startswith("ENV_KEYS:")
            )
            names = set(line.split(":", 1)[1].split())
            break
        time.sleep(0.05)

    assert names, "child process did not report its environment"
    assert "ANTHROPIC_API_KEY" in names
    assert "PIPELINE_ANTHROPIC_API_KEY" not in names


def test_should_leave_oauth_token_alone_when_api_key_also_set(monkeypatch):
    """Both credentials may be mounted on a standalone box; this layer does not
    rewrite either -- the CLI's own precedence decides the audit identity."""
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"


# --- A malformed mounted secret fails closed, loudly and without the value ---
# A secret file that still holds a trailing newline mid-value, a pasted
# "/run/secrets/..." path, or an unexpanded template would otherwise reach the
# CLI verbatim and surface only as a confusing 401. Refuse it instead, and
# never include the value in the error (no secret in logs/errors).

@pytest.mark.parametrize("bad_value", [
    "sk-test\nsk-other",         # newline mid-value
    "sk-test sk-other",          # internal whitespace
    "/run/secrets/anthropic_key",  # path remnant, not a key
    "${ANTHROPIC_API_KEY}",      # unexpanded template
    "sk-test/run/secrets",       # path remnant appended
])
def test_should_fail_closed_on_malformed_operator_key(monkeypatch, bad_value):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", bad_value)

    with pytest.raises(RuntimeError) as excinfo:
        _first_party_claude_env()

    message = str(excinfo.value)
    assert "PIPELINE_ANTHROPIC_API_KEY" in message
    # Never echo the secret itself into the error (logs, tracebacks, audit).
    assert bad_value.strip() not in message
    assert "sk-other" not in message


def test_should_not_raise_for_well_formed_operator_key(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", "sk-ant-api03-abcDEF123\n")

    env = _first_party_claude_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-abcDEF123"
