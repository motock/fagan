"""
Chat adapter module.

This module implements the ChatService class, which orchestrates a single turn of the chatbot. It is deliberately minimal for this story: it does not access the pipeline service layer directly; this is a no direct access constraint; that constraint is documented in the module docstring for future reference.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel


def _patch_story_execute(http_client, api_base_url, plan_name, story_key, fields, **kwargs):
    if "risk" in fields:
        return {"error": "risk field cannot be patched via chat"}
    return http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/patch"), json=fields).json()


def _seg(x):
    return quote(str(x), safe="")
# System prompt used for all chat turns. Assembled AFTER the TOOLS registry
# below (see the SYSTEM_PROMPT assignment following the TOOLS dict) so the
# "Available tools:" sentence enumerates every registered tool name instead
# of a frozen snapshot - a prior fixed sentence here only ever named the 3
# read-only tools from the story that first wrote it, silently drifting out
# of sync as later stories registered 20 more (root-caused 2026-08-20:
# 14 of 23 registered tools were never named anywhere in the prompt).
# It must contain the tool-call protocol tags and end with the exact
# _FINAL_SENTENCE required by the tests.
_SYSTEM_PROMPT_PREFIX = (
    "You are a helpful assistant. Your role is to author plans, control ops, and make decisions. "
    "When you need to call a tool, emit a JSON object with keys name and args, wrapped exactly in [TOOL_CALL] and [/TOOL_CALL] tags. "
    "When a tool returns a result, wrap it in [TOOL_RESULT name=...] and [/TOOL_RESULT] tags. "
    "If no tool calls are needed, simply answer in natural language. "
    "You can read plan and story status, journals, logs, and checklists. You can execute control actions (dispatch, interrupt, patch, review, advance, pause, resume, mark done). All actions go through the HTTP API and are subject to server-side gates - if a gate blocks an action, surface the rejection to the user; do NOT attempt to bypass it. "
    "To help the user author a plan, call decompose with their goal to get a first draft. Show the draft and ask if they want to iterate. When satisfied, call save_plan then ingest_plan. When calling save_plan, pass the decompose result plan JSON verbatim as plan_json (a JSON string) - do not rewrite, summarize, or re-derive it; keep its epics/stories fields exactly as decompose returned them. Always confirm with the user before calling ingest_plan - ingestion dispatches stories. "
    "You can surface decisions the overlord has ruled on by calling list_decisions. If the user wants to override or supplement a ruling, record their answer via answer_decision. Human answers are appended to the same decision log as overlord rulings, preserving the audit trail. "
)
_FINAL_SENTENCE = "Call tools to gather information, then provide a natural-language reply."



def _resolve_tool_url(http_client, api_base_url: str, path: str) -> str:
    if isinstance(http_client, httpx.Client) and not http_client.base_url.is_absolute_url:
        return f"{api_base_url}{path}"
    return path

# Read-only tool registry: list_plans, get_plan, health, decompose, save_plan, ingest_plan.
TOOLS: dict[str, dict] = {
    "list_plans": {
        "description": "List all pipeline plans.",
        "params": {},
        "execute": lambda http_client, api_base_url, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, "/api/plans")).json()
        ),
    },
    "set_workspace": {
        "description": "Set the current workspace.",
        "params": {"path": "str", "create": "bool"},
        "execute": lambda http_client, api_base_url, path, create=False, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, "/api/workspace"), json={"path": path, "create": create}).json()
        ),
    },
    "list_workspaces": {
        "description": "List all workspaces.",
        "params": {},
        "execute": lambda http_client, api_base_url, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, "/api/workspaces")).json()
        ),
    },
    "get_plan": {
        "description": "Get full detail for one plan by name.",
        "params": {"plan_name": "str"},
        "execute": lambda http_client, api_base_url, plan_name, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}" )).json()
        ),
    },
    "list_decisions": {
        "description": "List decisions for a plan.",
        "params": {"plan_name": "str"},
        "execute": lambda http_client, api_base_url, plan_name, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}" )).json()["decisions"]
        ),
    },
    "answer_decision": {
        "description": "Record a decision for a plan.",
        "params": {"plan_name": "str", "story_key": "str", "question": "str", "answer": "str", "context": "str | None"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, question, answer, context=None, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/decisions"),
                             json={"story_key": story_key, "question": question, "answer": answer, "decided_by": "chat", "context": context or ""}).json()
        ),
    },
    "health": {
        "description": "Check dashboard API health.",
        "params": {},
        "execute": lambda http_client, api_base_url, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, "/api/health")).json()
        ),
    },
    "decompose": {
        "description": "Decompose a goal into a plan.",
        "params": {"goal": "str"},
        "execute": lambda http_client, api_base_url, goal, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, "/api/decompose"), json={"request": goal}).json()
        ),
    },
    "save_plan": {
        "description": "Save a plan JSON to a named plan.",
        "params": {"plan_name": "str", "plan_json": "str"},
        "execute": lambda http_client, api_base_url, plan_name, plan_json, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/save"), json={"plan_json": plan_json}).json()
        ),
    },
    "ingest_plan": {
        "description": "Ingest a plan's epics and stories.",
        "params": {"plan_name": "str", "only_epics": "list[str] | None", "overwrite": "bool"},
        "execute": lambda http_client, api_base_url, plan_name, only_epics=None, overwrite=False, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/ingest"),
                             json={"only_epics": only_epics, "overwrite": overwrite}).json()
        ),
    },
    "dispatch_story": {
        "description": "Dispatch a story.",
        "params": {"plan_name": "str", "story_key": "str"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/dispatch")).json()
        ),
    },
    "interrupt_story": {
        "description": "Interrupt a story.",
        "params": {"plan_name": "str", "story_key": "str"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/interrupt")).json()
        ),
    },
    "patch_story": {
        "description": "Patch a story with fields.",
        "params": {"plan_name": "str", "story_key": "str", "fields": "dict"},
        "execute": _patch_story_execute,
    },
    "review_story": {
        "description": "Review a story.",
        "params": {"plan_name": "str", "story_key": "str"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/review")).json()
        ),
    },
    "mark_story_done": {
        "description": "Mark a story as done.",
        "params": {"plan_name": "str", "story_key": "str"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/done")).json()
        ),
    },
    "checkpoint": {
        "description": "Create a checkpoint for a story.",
        "params": {"plan_name": "str", "story_key": "str", "step": "str", "summary": "str", "next_hint": "str | None"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, step, summary, next_hint=None, **kwargs: (
            http_client.post(
                _resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/checkpoint"),
                json={"step": step, "summary": summary} if next_hint is None else {"step": step, "summary": summary, "next_hint": next_hint}
            ).json()
        ),
    },
    "advance_pipeline": {
        "description": "Advance a plan pipeline.",
        "params": {"plan_name": "str"},
        "execute": lambda http_client, api_base_url, plan_name, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/advance")).json()
        ),
    },
    "advance_all_plans": {
        "description": "Advance all plans.",
        "params": {},
        "execute": lambda http_client, api_base_url, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, "/api/plans/advance_all")).json()
        ),
    },
    "pause_plan": {
        "description": "Pause a plan.",
        "params": {"plan_name": "str"},
        "execute": lambda http_client, api_base_url, plan_name, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/pause")).json()
        ),
    },
    "resume_plan": {
        "description": "Resume a plan.",
        "params": {"plan_name": "str"},
        "execute": lambda http_client, api_base_url, plan_name, **kwargs: (
            http_client.post(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/resume")).json()
        ),
    },
    "get_story_journal": {
        "description": "Get story journal.",
        "params": {"plan_name": "str", "story_key": "str"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/journal")).json()
        ),
    },
    "get_story_log": {
        "description": "Get story log.",
        "params": {"plan_name": "str", "story_key": "str"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/log")).json()
        ),
    },
    "get_story_checklist": {
        "description": "Get story checklist.",
        "params": {"plan_name": "str", "story_key": "str"},
        "execute": lambda http_client, api_base_url, plan_name, story_key, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, f"/api/plans/{_seg(plan_name)}/stories/{_seg(story_key)}/checklist")).json()
        ),
    },
    "read_file": {
        "description": "Read a file from the active workspace by relative path.",
        "params": {"path": "str"},
        "execute": lambda http_client, api_base_url, path, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, "/api/workspace/file"), params={"path": path}).json()
        ),
    },
    "list_directory": {
        "description": "List the immediate contents of a directory in the active workspace by relative path (non-recursive).",
        "params": {"path": "str"},
        "execute": lambda http_client, api_base_url, path='', **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, "/api/workspace/files"), params={"path": path}).json()
        ),
    },
    "search_code": {
        "description": "Search the active workspace for a text pattern (grep-style) and return matching lines with file and line number.",
        "params": {"pattern": "str"},
        "execute": lambda http_client, api_base_url, pattern, **kwargs: (
            http_client.get(_resolve_tool_url(http_client, api_base_url, "/api/workspace/search"), params={"pattern": pattern}).json()
        ),
    },
}


def _available_tools_sentence() -> str:
    """Build the "Available tools: ..." sentence from every name currently
    registered in TOOLS, so the prompt can never drift out of sync with the
    registry the way the old frozen sentence did. Each entry also renders the
    registered params ("args: name: type, ...", or "args: none" when the tool
    takes none), so the model sees the real argument names instead of guessing
    them - the drift that made it invent wrong argument names for save_plan."""
    entries = []
    for name in sorted(TOOLS):
        params = TOOLS[name].get("params") or {}
        params_str = ", ".join(f"{k}: {v}" for k, v in params.items()) or "none"
        entries.append(f"{name} ({TOOLS[name]['description']}; args: {params_str})")
    return "Available tools: " + ", ".join(entries) + " "


SYSTEM_PROMPT = _SYSTEM_PROMPT_PREFIX + _available_tools_sentence() + _FINAL_SENTENCE

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


def _build_chat_prompt(message: str, history: list[dict] | None) -> str:
    """Render the current message plus any prior turns into the single
    prompt string the Backend.complete() interface accepts (it has no
    separate messages-list parameter). When *history* is falsy (None or
    empty), returns *message* unchanged - this is the compatibility path
    every existing call site (and every existing test) relies on."""
    if not history:
        return message
    lines = [f"{turn.get('role', 'user')}: {turn.get('content', '')}" for turn in history]
    lines.append(f"user: {message}")
    return "\n".join(lines)


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

    def __init__(self, *, driver=None, http_client=None, api_base_url=None, max_turns=None, api_key: str | None = None):
        self._driver = driver
        self._api_base_url = api_base_url or os.environ.get("PIPELINE_CHAT_API_BASE", "http://127.0.0.1:8000")
        # The decompose tool resolves synchronously over this client to a real
        # product-analyst LLM call (15-90s+); httpx's 5s default times out on
        # essentially every decompose turn. Injected clients are left untouched.
        self._http_client = http_client or httpx.Client(
            base_url=self._api_base_url,
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        if api_key and hasattr(self._http_client, "headers"):
            self._http_client.headers["X-Pipeline-Api-Key"] = api_key
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

        # model_fallback keeps /api/chat working on machines with no chat role
        # configured (e.g. the PP-01 provider-neutral registry, roles:{}), the
        # same degrade path _run_decompose already uses.
        from pipeline.config import DEFAULT_MODEL

        resolution = role_registry.resolve_role(
            "chat",
            registry=role_registry.load_registry(),
            model_fallback=lambda: DEFAULT_MODEL,
        )
        driver = backend.get_backend("chat", name=resolution.provider)
        self._resolved_driver = driver
        self._resolved_model = resolution.model
        return driver, resolution.model

    def execute_turn(self, message, *, plan_name=None, history=None, workspace=None) -> dict:
        driver, model_tag = self._resolve_driver()
        current_prompt = _build_chat_prompt(message, history)
        tool_calls_made: list[dict] = []
        turns = 0
        while turns < self._max_turns:
            turns += 1
            # cwd=tempfile.gettempdir() keeps this repo's own CLAUDE.md from auto-discovering and silently overriding chat.py's own SYSTEM_PROMPT and tool-confirmation policy, without the --bare flag's side effect of disabling OAuth/keychain auth (see ClaudeCliDriver.complete's --bare handling)
            system_prompt = SYSTEM_PROMPT
            if workspace:
                system_prompt += f"\nThe active workspace is {workspace}. Use this absolute path as repo_root when authoring a plan."
            response = driver.complete(prompt=current_prompt, system=system_prompt, model=model_tag, cwd=tempfile.gettempdir())
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

# ---------------------------------------------------------------------------
# FastAPI router for chat endpoint
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    plan_name: str | None = None
    workspace: str | None = None
    message: str
    history: list[dict] | None = None

class ChatResponse(BaseModel):
    reply: str
    tool_calls: list
    turns: int

chat_router = APIRouter()

@chat_router.post("/chat", response_model=ChatResponse)
def chat_endpoint(
    req: ChatRequest,
    x_pipeline_api_key: str | None = Header(default=None, alias="x-pipeline-api-key"),
) -> ChatResponse:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    svc = ChatService(api_key=x_pipeline_api_key)
    result = svc.execute_turn(req.message, plan_name=req.plan_name, history=req.history, workspace=req.workspace)
    return ChatResponse(**result)

"""
End of file
"""
