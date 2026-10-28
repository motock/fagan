"""DK-02 contract tests: the scripted model backend speaks the driver's wire protocol.

``tests/smoke/scripted_model_server.py`` is a stdlib HTTP server that answers
the OpenAI chat-completions request a litellm-routed role makes, drawing each
answer from a JSON script keyed by role. These tests pin the wire contract
(endpoint path, response envelope, per-role cursors, error statuses) and prove
that every role's canned answer is consumed by the pipeline's OWN parser. That
second half is the point: a scripted backend only lets an end-to-end run skip a
real model if the bytes it emits are the bytes the parsers accept.

The parsers under test are pure functions, so no principal is bound here - the
backend is a transport seam, not an MCP tool, and it never takes an ``actor``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

# ``pipeline.server`` is imported first: a new test module must route its
# ``pipeline.*`` imports through it (pre-existing import cycle) - same idiom as
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401
from pipeline.parsers import _extract_json_block, _parse_ruling, _parse_verdict
from tests.smoke import scripted_model_server as scripted

# The registry pins each role to ``provider: litellm`` with a model whose tag
# carries the role name; the server resolves the role from the request's
# ``model`` field, which is the only role-bearing field on the wire.
ROLE_MODEL = {role: f"openai/scripted-{role}" for role in scripted.ROLE_NAMES}

DECOMPOSE_PLAN = {
    "epics": [
        {
            "title": "Smoke epic",
            "stories": [
                {
                    "key": "SMOKE-1",
                    "title": "One scripted story",
                    "acceptance": ["assert True"],
                }
            ],
        }
    ]
}

OVERLORD_RULING = (
    "RULING: APPROVED\n"
    "TIER: 1\n"
    "RISK: low\n"
    "RATIONALE: the scripted smoke run passed\n"
    "ACTION: mark_done\n"
    "NOTIFY_USER: no"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(content: str, *, tool_calls=None, finish_reason=None) -> dict:
    """One script entry: prose, tool calls, or both."""
    entry: dict = {}
    if content is not None:
        entry["content"] = content
    if tool_calls is not None:
        entry["tool_calls"] = tool_calls
    if finish_reason is not None:
        entry["finish_reason"] = finish_reason
    return entry


def _url(server) -> str:
    return server.base_url + scripted.ENDPOINT_PATH


def _post_raw(server, raw: bytes):
    """POST raw bytes; return (status, decoded JSON body or {})."""
    request = urllib.request.Request(
        _url(server),
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def _decode(raw: bytes):
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _post(server, model: str, *, messages=None, tools=None):
    body = {
        "model": model,
        "messages": messages or [{"role": "user", "content": "go"}],
        "stream": False,
    }
    if tools is not None:
        body["tools"] = tools
    return _post_raw(server, json.dumps(body).encode("utf-8"))


def _choice(payload: dict) -> dict:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    return choices[0]


def _message(payload: dict) -> dict:
    message = _choice(payload).get("message")
    return message if isinstance(message, dict) else {}


def _content(payload: dict):
    return _message(payload).get("content")


def _raw_arguments(payload: dict, index: int = 0):
    calls = _message(payload).get("tool_calls") or []
    if index >= len(calls) or not isinstance(calls[index], dict):
        return None
    function = calls[index].get("function")
    return function.get("arguments") if isinstance(function, dict) else None


def _first_tool(payload: dict):
    """(name, decoded arguments) for choices[0].message.tool_calls[0]."""
    calls = _message(payload).get("tool_calls") or []
    if not calls or not isinstance(calls[0], dict):
        return None, None
    function = calls[0].get("function")
    if not isinstance(function, dict):
        return None, None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = None
    return function.get("name"), arguments


@pytest.fixture
def serve():
    """Start scripted servers on an ephemeral port; always stop them."""
    started = []

    def _start(script):
        server = scripted.ScriptedModelServer(script, host="127.0.0.1", port=0)
        server.start()
        started.append(server)
        return server

    yield _start
    for server in started:
        server.stop()


# ---------------------------------------------------------------------------
# Envelope: the shape the litellm/ollama driver reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", scripted.ROLE_NAMES)
def test_each_role_answers_with_the_openai_chat_completions_envelope(serve, role):
    canned = f"canned answer for {role}"
    server = serve({role: [_entry(canned)]})

    status, payload = _post(server, ROLE_MODEL[role])

    assert status == 200
    assert payload.get("object") == "chat.completion"
    assert payload.get("model") == ROLE_MODEL[role]
    assert len(payload.get("choices") or []) == 1
    assert _message(payload).get("role") == "assistant"
    assert _content(payload) == canned
    assert _choice(payload).get("finish_reason") == "stop"
    usage = payload.get("usage") or {}
    assert isinstance(usage.get("prompt_tokens"), int)
    assert isinstance(usage.get("completion_tokens"), int)
    assert usage["prompt_tokens"] >= 0
    assert usage["completion_tokens"] >= 0


# ---------------------------------------------------------------------------
# Every role's canned answer goes through the pipeline's own parser
# ---------------------------------------------------------------------------


def test_decompose_answer_parses_with_the_plan_json_extractor(serve):
    content = "```json\n" + json.dumps(DECOMPOSE_PLAN) + "\n```"
    server = serve({"decompose": [_entry(content)]})

    status, payload = _post(server, ROLE_MODEL["decompose"])

    assert status == 200
    plan = json.loads(_extract_json_block(_content(payload) or ""))
    assert isinstance(plan.get("epics"), list)
    assert [epic["title"] for epic in plan["epics"]] == ["Smoke epic"]
    assert plan["epics"][0]["stories"][0]["key"] == "SMOKE-1"


@pytest.mark.parametrize(
    ("canned", "expected"),
    [
        ("Looks good.\nVERDICT: APPROVE", "APPROVE"),
        ("Still wrong.\nVERDICT: REQUEST_CHANGES", "REQUEST_CHANGES"),
    ],
)
def test_review_answer_parses_with_the_verdict_parser(serve, canned, expected):
    server = serve({"review": [_entry(canned)]})

    status, payload = _post(server, ROLE_MODEL["review"])

    assert status == 200
    assert _parse_verdict(_content(payload) or "") == expected


def test_overlord_answer_parses_with_the_ruling_parser(serve):
    server = serve({"overlord": [_entry(OVERLORD_RULING)]})

    status, payload = _post(server, ROLE_MODEL["overlord"])

    assert status == 200
    ruling = _parse_ruling(_content(payload) or "")
    assert ruling["ruling"] == "APPROVED"
    assert ruling["tier"] == "1"
    assert ruling["risk"] == "low"
    assert ruling["rationale"] == "the scripted smoke run passed"
    assert ruling["action"] == "mark_done"
    assert ruling["notify_user"] is False


def test_overlord_answer_without_an_action_parses_to_the_fail_closed_default(serve):
    canned = "RULING: APPROVED\nTIER: 1\nRISK: low\nRATIONALE: silent on the action"
    server = serve({"overlord": [_entry(canned)]})

    status, payload = _post(server, ROLE_MODEL["overlord"])

    assert status == 200
    assert _parse_ruling(_content(payload) or "")["action"] == "park_for_human"


# ---------------------------------------------------------------------------
# Dispatch: one write, one commit, finish
# ---------------------------------------------------------------------------


def test_default_dispatch_script_writes_one_file_commits_and_finishes(serve):
    server = serve(scripted.default_script())
    dispatch_messages = [{"role": "user", "content": "implement the story"}]

    status, payload = _post(server, ROLE_MODEL["dispatch"], messages=dispatch_messages)
    assert status == 200
    assert _choice(payload).get("finish_reason") == "tool_calls"
    assert isinstance(_raw_arguments(payload), str), "arguments must be a JSON string"
    name, arguments = _first_tool(payload)
    assert name == "create_file"
    assert isinstance(arguments, dict)
    assert isinstance(arguments.get("path"), str) and arguments["path"]
    assert not arguments["path"].startswith("/"), "written file must be repo-relative"
    assert isinstance(arguments.get("content"), str)
    written_path = arguments["path"]

    dispatch_messages.append(
        {"role": "assistant", "content": _content(payload), "tool_calls": _message(payload).get("tool_calls")}
    )
    dispatch_messages.append({"role": "tool", "tool_call_id": "call-1", "content": "wrote it"})
    status, payload = _post(server, ROLE_MODEL["dispatch"], messages=dispatch_messages)
    assert status == 200
    name, arguments = _first_tool(payload)
    assert name == "bash"
    assert isinstance(arguments, dict)
    command = arguments.get("command") or ""
    assert "git add" in command, "the commit step must stage the written file"
    assert "git commit" in command, "the commit step must commit"
    assert written_path in command, "the commit step must stage the file it wrote"

    dispatch_messages.append({"role": "tool", "tool_call_id": "call-2", "content": "committed"})
    status, payload = _post(server, ROLE_MODEL["dispatch"], messages=dispatch_messages)
    assert status == 200
    name, arguments = _first_tool(payload)
    assert name == "done"
    assert isinstance(arguments, dict)
    assert isinstance(arguments.get("summary"), str) and arguments["summary"]


def test_dispatch_tool_calls_carry_the_harness_names_and_schemas(serve):
    server = serve(scripted.default_script())
    harness_tools = [
        {"type": "function", "function": {"name": "create_file", "parameters": {}}},
        {"type": "function", "function": {"name": "bash", "parameters": {}}},
        {"type": "function", "function": {"name": "done", "parameters": {}}},
    ]

    status, payload = _post(
        server, ROLE_MODEL["dispatch"], tools=harness_tools
    )

    assert status == 200
    calls = _message(payload).get("tool_calls") or []
    assert len(calls) == 1
    call = calls[0]
    assert call.get("type") == "function"
    assert isinstance(call.get("id"), str) and call["id"]
    assert call["function"].get("name") == "create_file"


# ---------------------------------------------------------------------------
# Per-role cursors
# ---------------------------------------------------------------------------


def test_per_role_cursors_advance_independently(serve):
    server = serve(
        {
            "dispatch": [_entry("d0"), _entry("d1"), _entry("d2")],
            "decompose": [_entry("a0"), _entry("a1")],
        }
    )
    assert server.cursor_state() == {"dispatch": 0, "decompose": 0}

    # Read-then-increment: the first dispatch call returns d0, never d1.
    status, payload = _post(server, ROLE_MODEL["dispatch"])
    assert (status, _content(payload)) == (200, "d0")
    assert server.cursor_state()["dispatch"] == 1

    # A decompose call must not move the dispatch cursor.
    status, payload = _post(server, ROLE_MODEL["decompose"])
    assert (status, _content(payload)) == (200, "a0")
    assert server.cursor_state()["decompose"] == 1

    status, payload = _post(server, ROLE_MODEL["dispatch"])
    assert (status, _content(payload)) == (200, "d1")
    status, payload = _post(server, ROLE_MODEL["dispatch"])
    assert (status, _content(payload)) == (200, "d2")
    assert server.cursor_state()["dispatch"] == 3

    # Exhaustion is terminal: no replay of d0, no repeating d2 forever.
    status, payload = _post(server, ROLE_MODEL["dispatch"])
    assert status == 500
    status, payload = _post(server, ROLE_MODEL["dispatch"])
    assert status == 500
    assert server.cursor_state()["dispatch"] == 3

    status, payload = _post(server, ROLE_MODEL["decompose"])
    assert (status, _content(payload)) == (200, "a1")
    assert server.cursor_state() == {"dispatch": 3, "decompose": 2}


# ---------------------------------------------------------------------------
# Errors: status codes, and messages that name only action/role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["openai/mystery-model", ""])
def test_unresolvable_model_is_rejected_with_http_400(serve, model):
    server = serve({"dispatch": [_entry("d0")]})

    status, payload = _post(server, model)

    assert status == 400
    error = payload.get("error")
    assert isinstance(error, dict)
    assert "role" in (error.get("message") or "")
    assert "d0" not in json.dumps(payload)


def test_exhausted_role_returns_http_500_without_echoing_the_request(serve):
    server = serve({"dispatch": [_entry("d0")]})
    assert _post(server, ROLE_MODEL["dispatch"])[0] == 200

    status, payload = _post(
        server,
        ROLE_MODEL["dispatch"],
        messages=[{"role": "user", "content": "SENTINEL-PAYLOAD"}],
    )

    assert status == 500
    error = payload.get("error")
    assert isinstance(error, dict)
    assert "dispatch" in (error.get("message") or "")
    assert "SENTINEL-PAYLOAD" not in json.dumps(payload)


def test_role_absent_from_the_script_returns_http_500(serve):
    server = serve({"review": [_entry("VERDICT: APPROVE")]})

    status, payload = _post(server, ROLE_MODEL["overlord"])

    assert status == 500
    error = payload.get("error")
    assert isinstance(error, dict)
    assert "overlord" in (error.get("message") or "")


def test_malformed_request_body_returns_http_400(serve):
    server = serve({"dispatch": [_entry("d0")]})

    status, payload = _post_raw(server, b"{not json")

    assert status == 400
    error = payload.get("error")
    assert isinstance(error, dict)
    assert "not json" not in json.dumps(payload)


def test_get_on_the_chat_endpoint_is_rejected(serve):
    server = serve({"dispatch": [_entry("d0")]})
    request = urllib.request.Request(_url(server), method="GET")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code

    assert status == 405


# ---------------------------------------------------------------------------
# Script loading / validation boundaries, and the shipped script
# ---------------------------------------------------------------------------


def test_default_script_covers_every_role(serve):
    script = scripted.default_script()

    for role in scripted.ROLE_NAMES:
        assert isinstance(script.get(role), list), role
        assert script[role], f"role {role} needs at least one canned answer"
    dispatch_names = [
        (call.get("name") if isinstance(call, dict) else None)
        for call in script["dispatch"][0].get("tool_calls") or []
    ]
    assert dispatch_names == ["create_file"]
    assert (script["dispatch"][1].get("tool_calls") or [{}])[0].get("name") == "bash"
    assert (script["dispatch"][2].get("tool_calls") or [{}])[0].get("name") == "done"


def test_load_script_reads_a_json_script_keyed_by_role(tmp_path):
    script = {"review": [_entry("VERDICT: APPROVE")]}
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script), encoding="utf-8")

    assert scripted.load_script(path) == script


@pytest.mark.parametrize(
    "raw",
    [
        {"reviewer": [_entry("x")]},
        {"review": []},
        {"review": [{"unexpected": True}]},
        {"review": "not a list"},
    ],
)
def test_validate_script_rejects_unknown_roles_and_bad_entries(raw):
    with pytest.raises(ValueError):
        scripted.validate_script(raw)


def test_load_script_failure_names_the_action_not_the_path(tmp_path):
    missing = tmp_path / "missing-script.json"

    with pytest.raises(ValueError) as excinfo:
        scripted.load_script(missing)

    assert "script" in str(excinfo.value)
    assert "missing-script.json" not in str(excinfo.value)


def test_module_documents_registry_routing_for_every_role():
    doc = scripted.__doc__ or ""

    assert "provider: litellm" in doc
    assert "PIPELINE_LOCAL_ENDPOINT_LITELLM" in doc
    assert scripted.ENDPOINT_PATH in doc
    for role in scripted.ROLE_NAMES:
        assert role in doc
