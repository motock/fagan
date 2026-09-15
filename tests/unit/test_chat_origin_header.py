"""Unit tests for the unconditional chat-origin header (``X-Pipeline-Origin``).

Every internal tool call the chat service makes rides ONE ``httpx`` client
(``ChatService._http_client``), so the pipeline API cannot otherwise tell a
model-driven call from a human/dashboard one. The fix is that the client
itself carries a dedicated origin header, stamped UNCONDITIONALLY at
construction time - independent of the optional ``api_key`` truthiness gate
that guards the ``X-Pipeline-Api-Key`` header.

These tests pin:

* the module-level constants in ``app/auth.py`` (names, exact values, and
  that they live next to ``PIPELINE_KEY_PREFIX``);
* that ``app/chat.py`` imports those constants from ``app.auth`` (no cycle:
  ``app.auth`` has no app-internal imports);
* the header is written on a self-constructed client with NO api_key;
* the header is written on an INJECTED client that has ``.headers``, with
  pre-existing headers preserved;
* with an api_key, BOTH headers are present;
* the ``hasattr(self._http_client, "headers")`` guard: duck-typed fakes
  without ``.headers`` (the shape every existing test_chat_*.py fake uses)
  must not raise, with or without api_key;
* the existing api-key line is unchanged and still comes AFTER the origin
  line;
* the pre-existing invariant "no api_key => no X-Pipeline-Api-Key" holds.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

import app.auth as auth_mod
import app.chat as chat_mod
from app.auth import ORIGIN_CHAT, ORIGIN_HEADER, ORIGIN_UI
from app.chat import ChatService

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTH_SOURCE = (REPO_ROOT / "app" / "auth.py").read_text()
CHAT_SOURCE = (REPO_ROOT / "app" / "chat.py").read_text()

API_KEY_HEADER = "X-Pipeline-Api-Key"


class _StubDriver:
    model = "stub"

    def complete(self, *, prompt, system, model, cwd):  # pragma: no cover
        return "ok"


class _FakeClientWithHeaders:
    """Injected client with a plain-dict ``.headers`` (the injectable shape)."""

    def __init__(self) -> None:
        self.headers: dict = {}

    def post(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError("no HTTP expected in this unit test")


class _DuckClientNoHeaders:
    """The shape used by the ~29 existing test_chat_*.py fakes: no .headers."""

    def post(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError("no HTTP expected in this unit test")


def _close(service) -> None:
    client = getattr(service, "_http_client", None)
    if client is not None and hasattr(client, "close"):
        client.close()


# ---------------------------------------------------------------------------
# app/auth.py: the constants themselves
# ---------------------------------------------------------------------------


def test_origin_constants_have_exact_names_and_values():
    assert ORIGIN_HEADER == "X-Pipeline-Origin"
    assert ORIGIN_CHAT == "chat"
    assert ORIGIN_UI == "ui"


def test_origin_constants_are_module_level_strings_defined_near_pipeline_key_prefix():
    assert isinstance(auth_mod.ORIGIN_HEADER, str)
    assert isinstance(auth_mod.ORIGIN_CHAT, str)
    assert isinstance(auth_mod.ORIGIN_UI, str)
    # "near PIPELINE_KEY_PREFIX": the definition lines sit after it in the file.
    prefix_at = AUTH_SOURCE.index('PIPELINE_KEY_PREFIX = "k-"')
    header_at = AUTH_SOURCE.index('ORIGIN_HEADER = "X-Pipeline-Origin"')
    chat_at = AUTH_SOURCE.index('ORIGIN_CHAT = "chat"')
    ui_at = AUTH_SOURCE.index('ORIGIN_UI = "ui"')
    assert prefix_at < header_at < chat_at < ui_at


def test_auth_module_documents_the_origin_header_note():
    """The note may live in the module docstring or an adjacent comment."""
    lowered = AUTH_SOURCE.lower()
    assert "ORIGIN_CHAT" in AUTH_SOURCE
    assert "unconditional" in lowered or "always" in lowered
    assert "origin" in lowered


def test_auth_module_has_no_app_internal_imports():
    """app.auth must stay import-cycle-free for app.chat to import from it."""
    offenders = [
        line
        for line in AUTH_SOURCE.splitlines()
        if re.match(r"^\s*(from|import)\s+app\b", line)
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# app/chat.py: wiring
# ---------------------------------------------------------------------------


def test_chat_module_imports_origin_constants_from_app_auth():
    assert chat_mod.ORIGIN_HEADER is auth_mod.ORIGIN_HEADER
    assert chat_mod.ORIGIN_CHAT is auth_mod.ORIGIN_CHAT


def test_origin_header_line_is_guarded_and_precedes_the_api_key_line():
    origin_line = 'self._http_client.headers[ORIGIN_HEADER] = ORIGIN_CHAT'
    api_key_line = 'self._http_client.headers["X-Pipeline-Api-Key"] = api_key'
    assert origin_line in CHAT_SOURCE
    assert api_key_line in CHAT_SOURCE
    assert CHAT_SOURCE.index(origin_line) < CHAT_SOURCE.index(api_key_line)
    # The hasattr guard is load-bearing (duck-typed fakes without .headers).
    assert 'if hasattr(self._http_client, "headers"):' in CHAT_SOURCE


def test_existing_api_key_truthiness_line_is_unchanged():
    assert 'if api_key and hasattr(self._http_client, "headers"):' in CHAT_SOURCE


def test_tool_dispatch_helpers_still_present():
    """The story must not disturb TOOLS / _resolve_tool_url."""
    assert callable(chat_mod._resolve_tool_url)
    assert isinstance(chat_mod.TOOLS, dict) and chat_mod.TOOLS


def test_system_prompt_anchor_unchanged():
    """Anchor only: the story must not rewrite the system prompt."""
    assert chat_mod.SYSTEM_PROMPT.startswith(chat_mod._SYSTEM_PROMPT_PREFIX)
    assert chat_mod._FINAL_SENTENCE in chat_mod.SYSTEM_PROMPT


def test_origin_header_is_written_before_max_turns_validation():
    """Placement pin: the origin line sits directly after the client assignment,
    so it is applied even when the later max_turns check rejects the value."""
    fake = _FakeClientWithHeaders()
    with pytest.raises(ValueError, match="max_turns must be a positive integer"):
        ChatService(driver=_StubDriver(), http_client=fake, max_turns=0)
    assert fake.headers[ORIGIN_HEADER] == ORIGIN_CHAT


# ---------------------------------------------------------------------------
# Self-constructed client, no api_key
# ---------------------------------------------------------------------------


def test_self_constructed_client_gets_origin_header_without_api_key():
    svc = ChatService(driver=_StubDriver())
    try:
        assert svc._http_client.headers[ORIGIN_HEADER] == ORIGIN_CHAT
        assert svc._http_client.headers["X-Pipeline-Origin"] == "chat"
        assert API_KEY_HEADER not in svc._http_client.headers
    finally:
        _close(svc)


def test_self_constructed_client_gets_origin_header_with_explicit_none_api_key():
    svc = ChatService(driver=_StubDriver(), api_key=None)
    try:
        assert svc._http_client.headers[ORIGIN_HEADER] == ORIGIN_CHAT
        assert API_KEY_HEADER not in svc._http_client.headers
    finally:
        _close(svc)


def test_self_constructed_client_gets_both_headers_with_api_key():
    svc = ChatService(driver=_StubDriver(), api_key="secret-key")
    try:
        assert svc._http_client.headers[ORIGIN_HEADER] == ORIGIN_CHAT
        assert svc._http_client.headers[API_KEY_HEADER] == "secret-key"
    finally:
        _close(svc)


# ---------------------------------------------------------------------------
# Injected client with .headers
# ---------------------------------------------------------------------------


def test_injected_client_gets_origin_header_and_preserves_existing_headers():
    fake = _FakeClientWithHeaders()
    fake.headers["User-Agent"] = "unit-test"
    svc = ChatService(driver=_StubDriver(), http_client=fake)
    try:
        assert fake.headers[ORIGIN_HEADER] == ORIGIN_CHAT
        assert fake.headers["User-Agent"] == "unit-test"
        assert API_KEY_HEADER not in fake.headers
    finally:
        _close(svc)


def test_injected_client_gets_both_headers_when_api_key_set():
    fake = _FakeClientWithHeaders()
    fake.headers["User-Agent"] = "unit-test"
    svc = ChatService(driver=_StubDriver(), http_client=fake, api_key="secret-key")
    try:
        assert fake.headers[ORIGIN_HEADER] == ORIGIN_CHAT
        assert fake.headers[API_KEY_HEADER] == "secret-key"
        assert fake.headers["User-Agent"] == "unit-test"
    finally:
        _close(svc)


def test_injected_client_with_empty_string_api_key_gets_origin_only():
    fake = _FakeClientWithHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake, api_key="")
    try:
        assert fake.headers == {"X-Pipeline-Origin": "chat"}
    finally:
        _close(svc)


def test_origin_header_is_written_into_a_real_httpx_headers_mapping():
    fake = _FakeClientWithHeaders()
    fake.headers = httpx.Headers()
    svc = ChatService(driver=_StubDriver(), http_client=fake)
    try:
        assert fake.headers["X-Pipeline-Origin"] == "chat"
        # httpx.Headers is case-insensitive; the canonical name is what we set.
        assert fake.headers.get("x-pipeline-origin") == "chat"
    finally:
        _close(svc)


def test_origin_header_value_is_chat_not_ui():
    fake = _FakeClientWithHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake)
    try:
        assert fake.headers[ORIGIN_HEADER] == "chat"
        assert fake.headers[ORIGIN_HEADER] != ORIGIN_UI
    finally:
        _close(svc)


# ---------------------------------------------------------------------------
# hasattr guard: duck-typed clients without .headers must never raise
# ---------------------------------------------------------------------------


def test_duck_typed_client_without_headers_does_not_raise_without_api_key():
    fake = _DuckClientNoHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake)
    assert not hasattr(fake, "headers")
    _close(svc)


def test_duck_typed_client_without_headers_does_not_raise_with_api_key():
    fake = _DuckClientNoHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake, api_key="secret-key")
    assert not hasattr(fake, "headers")
    _close(svc)


@pytest.mark.parametrize("api_key", [None, "", "secret-key"])
def test_duck_typed_client_without_headers_never_raises(api_key):
    fake = _DuckClientNoHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake, api_key=api_key)
    assert not hasattr(fake, "headers")
    _close(svc)
