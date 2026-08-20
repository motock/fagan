"""
Chat adapter module.

This module implements the ChatService class, which orchestrates a single turn of the chatbot. It is deliberately minimal for this story: it does not access the pipeline service layer directly; this is a no direct access constraint; that constraint is documented in the module docstring for future reference.

The service resolves the appropriate LLM backend lazily on first use, so that tests can instantiate the class without triggering role resolution.
"""

from __future__ import annotations

import os
import httpx

# System prompt used for all chat turns.  It must contain the tool‑call
# protocol tags and end with the exact sentence required by the tests.
SYSTEM_PROMPT = (
    "You are a helpful assistant. Your role is to author plans, control ops, and make decisions. "
    "When you need to call a tool, emit a JSON object with keys name and args, wrapped exactly in [TOOL_CALL] and [/TOOL_CALL] tags. "
    "When a tool returns a result, wrap it in [TOOL_RESULT name=...] and [/TOOL_RESULT] tags. "
    "If no tool calls are needed, simply answer in natural language. "
    "Call tools to gather information, then provide a natural-language reply."
)


class ChatService:
    """Minimal chat service for a single turn.

    Parameters
    ----------
    driver:
        Optional backend driver.  If supplied, it is used directly and no
        role resolution occurs.  Tests inject a fake driver that records calls.
    http_client:
        Optional HTTP client.  If omitted, a new ``httpx.Client`` is
        constructed with ``base_url`` set to ``self._api_base_url``.
    api_base_url:
        Base URL for the HTTP client.  Defaults to the environment variable
        ``PIPELINE_CHAT_API_BASE`` or ``http://127.0.0.1:8000``.
    max_turns:
        Maximum number of turns for the chat loop.  Not used in this story
        but stored for future use.
    """

    def __init__(self, *, driver=None, http_client=None, api_base_url=None, max_turns=None):
        self._driver = driver
        self._api_base_url = api_base_url or os.environ.get("PIPELINE_CHAT_API_BASE", "http://127.0.0.1:8000")
        self._http_client = http_client or httpx.Client(base_url=self._api_base_url)
        self._max_turns = max_turns or int(os.environ.get("PIPELINE_CHAT_MAX_TURNS", "10"))
        self._resolved_driver = None
        self._resolved_model = None

    def _resolve_driver(self) -> tuple[object, str]:
        if self._driver is not None:
            model_tag = getattr(self._driver, "model", "") or ""
            return self._driver, model_tag
        if self._resolved_driver is not None:
            return self._resolved_driver, self._resolved_model
        from app import role_registry
        from app import backend

        resolution = role_registry.resolve_role("chat", registry=role_registry.load_registry())
        driver = backend.get_backend(resolution.provider)
        self._resolved_driver = driver
        self._resolved_model = resolution.model
        return driver, resolution.model

    def execute_turn(self, message, *, plan_name=None, history=None) -> dict:
        driver, model_tag = self._resolve_driver()
        reply = driver.complete(prompt=message, system=SYSTEM_PROMPT, model=model_tag)
        return {"reply": reply, "tool_calls": [], "turns": 1}
