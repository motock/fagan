"""
Chat adapter module.

This module implements the ChatService class, which orchestrates a single turn of the chatbot. It is deliberately minimal for this story: it does not access the pipeline service layer directly; this is a no direct access constraint; that constraint is documented in the module docstring for future reference.
"""

from __future__ import annotations

import json
import os
import re

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

# Empty tool registry – populated by a later story.
TOOLS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _parse_tool_calls(text: str) -> list[dict]:
    """Return a list of tool call dicts extracted from *text*.

    Each call is a JSON object between the literal tags [TOOL_CALL] and
    [/TOOL_CALL].  Only calls that contain both a ``name`` key (str) and an
    ``args`` key (dict) are returned.  Malformed JSON or missing keys are
    silently skipped.
    """
    calls: list[dict] = []
    for raw in re.findall(r"\[TOOL_CALL\](.*?)\[/TOOL_CALL\]", text, re.DOTALL):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if "name" not in obj or "args" not in obj:
            continue
        if not isinstance(obj["name"], str) or not isinstance(obj["args"], dict):
            continue
        calls.append({"name": obj["name"], "args": obj["args"]})
    return calls


def _execute_tool(name: str, args: dict, http_client, api_base_url: str) -> dict:
    """Execute a tool by name.

    If the tool is unknown, return ``{"error": "unknown tool: <name>"}``.  On
    success, return ``{"result": <return value>}``.  Any exception raised by
    the tool's ``execute`` callable is caught and returned as an error dict.
    """
    if name not in TOOLS:
        return {"error": f"unknown tool: {name}"}
    try:
        result = TOOLS[name]["execute"](http_client, api_base_url, **args)
    except Exception as exc:  # pragma: no cover - exercised via tests  # noqa: BLE001
        return {"error": str(exc)}
    return {"result": result}


# ---------------------------------------------------------------------------
# ChatService
# ---------------------------------------------------------------------------
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
        Maximum number of turns for the chat loop.  A positive integer is required; a non‑positive value is rejected at construction.
    """

    def __init__(self, *, driver=None, http_client=None, api_base_url=None, max_turns=None):
        self._driver = driver
        self._api_base_url = api_base_url or os.environ.get("PIPELINE_CHAT_API_BASE", "http://127.0.0.1:8000")
        self._http_client = http_client or httpx.Client(base_url=self._api_base_url)
        raw_max = max_turns if max_turns is not None else int(os.environ.get("PIPELINE_CHAT_MAX_TURNS", "10"))
        if raw_max <= 0:
            raise ValueError(f"max_turns must be a positive integer, got {raw_max}")
        self._max_turns = raw_max
        self._resolved_driver = None
        self._resolved_model = None

    def _resolve_driver(self) -> tuple[object, str]:
        if self._driver is not None:
            model_tag = getattr(self._driver, "model", "") or ""
            return self._driver, model_tag
        if self._resolved_driver is not None:
            return self._resolved_driver, self._resolved_model
        from app import backend, role_registry

        resolution = role_registry.resolve_role("chat", registry=role_registry.load_registry())
        driver = backend.get_backend(resolution.provider)
        self._resolved_driver = driver
        self._resolved_model = resolution.model
        return driver, resolution.model

    def execute_turn(self, message, *, plan_name=None, history=None) -> dict:
        driver, model_tag = self._resolve_driver()
        current_prompt = message
        tool_calls_made: list[dict] = []
        turns = 0
        while turns < self._max_turns:
            turns += 1
            response = driver.complete(prompt=current_prompt, system=SYSTEM_PROMPT, model=model_tag)
            parsed = _parse_tool_calls(response)
            if not parsed:
                return {"reply": response, "tool_calls": tool_calls_made, "turns": turns}
            for call in parsed:
                result = _execute_tool(call["name"], call["args"], self._http_client, self._api_base_url)
                tool_calls_made.append({"name": call["name"], "args": call["args"], "result": result})
            result_blocks = []
            for call in tool_calls_made:
                block = f"[TOOL_RESULT name={call['name']}]" + json.dumps(call['result']) + "[/TOOL_RESULT]"
                result_blocks.append(block)
            current_prompt = "\n".join(result_blocks)
        return {"reply": response + "\n\n(turn cap reached)", "tool_calls": tool_calls_made, "turns": turns}
