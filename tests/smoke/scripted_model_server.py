"""Scripted model backend: an end-to-end pipeline run needs no real model.

This module is a stdlib-only HTTP server that answers the OpenAI
chat-completions request the pipeline's local-inference drivers make, drawing
each answer from a JSON script keyed by role. It exists so a smoke run can
exercise dispatch, review, overlord and decompose end to end without a vendor
model, an API key or network access.

Wire contract
-------------
``POST {base_url}/v1/chat/completions`` - the same path
``app/inference_providers.LMStudioProvider`` posts to, and the path the
litellm SDK reaches when ``api_base`` is ``{base_url}/v1``. ``{base_url}
/chat/completions`` is accepted too, because litellm's ``api_base`` may also be
the server root (the SDK appends ``/chat/completions`` itself). The request
body fields this server reads are ``model`` (the role carrier) and ``messages``
(counted for the usage block); everything else is ignored.

The response is the envelope the drivers translate - ``object``,
``choices[0].message.{role,content,tool_calls}``,
``choices[0].finish_reason`` and ``usage.{prompt_tokens,completion_tokens}`` -
where ``tool_calls[].function.arguments`` is a JSON-encoded *string*, exactly
as OpenAI's API and ``LiteLLMProvider``/``LMStudioProvider`` expect.

Registry routing
----------------
Every role is pinned to this server with ``provider: litellm`` (the
``litellm`` wire provider), so the litellm SDK becomes the transport and this
server is what it talks to. In
``model_registry.json`` (or an operator's ``model_registry.local.json``,
selected with ``PIPELINE_MODEL_REGISTRY_PATH``) declare one model per role
whose tag contains the role name, then pin each role to it::

    {
      "providers": {
        "litellm": {
          "models": {
            "scripted-dispatch": {"tag": "openai/scripted-dispatch"},
            "scripted-review": {"tag": "openai/scripted-review"},
            "scripted-overlord": {"tag": "openai/scripted-overlord"},
            "scripted-decompose": {"tag": "openai/scripted-decompose"}
          }
        }
      },
      "roles": {
        "dispatch": {"provider": "litellm", "model": "scripted-dispatch"},
        "review": {"provider": "litellm", "model": "scripted-review"},
        "overlord": {"provider": "litellm", "model": "scripted-overlord"},
        "planner": {"provider": "litellm", "model": "scripted-decompose"}
      }
    }

Point the litellm provider at this server with the base-URL env var
``PIPELINE_LOCAL_ENDPOINT_LITELLM`` (forwarded to litellm as ``api_base``),
e.g. ``PIPELINE_LOCAL_ENDPOINT_LITELLM=http://127.0.0.1:8080/v1``. The litellm
package must be installed (``pip install litellm``); it is an optional extra.
A placeholder upstream key is enough - this server never authenticates.

Role selection
--------------
The only role-bearing field on the wire is ``model``: the server picks the
script section by finding which role name (``decompose``, ``review``,
``overlord``, ``dispatch``) occurs in the request's model string, preferring
the longest match. A model that names no role is rejected with HTTP 400, so a
misrouted role fails loudly instead of silently replaying another role's
answers.

Script format
-------------
The script is a JSON object keyed by role; each value is a non-empty list of
entries consumed in order::

    {"dispatch": [
       {"content": "", "tool_calls": [
           {"name": "create_file", "arguments": {"path": "x.txt", "content": "hi"}}]},
       {"tool_calls": [{"name": "bash", "arguments": {"command": "git add x.txt"}}]},
       {"tool_calls": [{"name": "done", "arguments": {"summary": "done"}}]}
    ]}

An entry carries ``content`` (prose the role answers with), ``tool_calls``
(harness tool calls, each ``{"name", "arguments"}`` with a dict or JSON-string
``arguments``; an ``id`` is generated when absent) and an optional
``finish_reason`` (derived as ``tool_calls`` when the entry has tool calls,
else ``stop``). The shipped :func:`default_script` drives the local agent
harness through one write, one commit and ``done``, gives the reviewer an
``APPROVE`` verdict, gives the overlord a ``mark_done`` ruling and gives
decompose a one-epic/one-story plan.

Cursors and concurrency
-----------------------
Each role has its own read-then-increment cursor, so a role consumes its
entries in order and never borrows another role's position. Once a role's
entries are used up, every further request for that role fails with HTTP 500
and the cursor stays put: exhaustion is terminal, never a replay of the first
entry and never a repeat of the last. One lock guards the read-and-advance and
the response sequence it claims, so a cursor update can never race the
response read.

Logging names the action, the role and the status only - never a request
payload, a credential, a filesystem path or exception text.

CLI::

    python tests/smoke/scripted_model_server.py --port 8080 [--script PATH]

prints the base URL, then serves until interrupted.
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__all__ = [
    "ENDPOINT_PATH",
    "ROLE_NAMES",
    "ScriptedModelServer",
    "UnknownRoleError",
    "build_completion",
    "default_script",
    "load_script",
    "main",
    "resolve_role",
    "validate_script",
]

_LOGGER = logging.getLogger(__name__)

#: The canonical request path (``.../v1/chat/completions``).
ENDPOINT_PATH = "/v1/chat/completions"

#: Accepted paths: the canonical one plus the route a root ``api_base`` yields.
_ACCEPTED_PATHS = frozenset({ENDPOINT_PATH, "/chat/completions"})

#: Every role this backend can serve.
ROLE_NAMES = ("decompose", "review", "overlord", "dispatch")

_ENTRY_KEYS = frozenset({"content", "tool_calls", "finish_reason"})
_TOOL_CALL_KEYS = frozenset({"name", "arguments", "id"})

#: Upper bound on a request body; anything larger is rejected, not buffered.
MAX_REQUEST_BYTES = 8 * 1024 * 1024

#: Default bind port for the CLI.
DEFAULT_PORT = 8080


class ScriptError(ValueError):
    """A scripted script is malformed, unreadable, or names an unknown role."""


class UnknownRoleError(ValueError):
    """The request's ``model`` names none of :data:`ROLE_NAMES`."""


# ---------------------------------------------------------------------------
# The shipped script: one end-to-end run, no real model
# ---------------------------------------------------------------------------

_DECOMPOSE_PLAN = {
    "epics": [
        {
            "title": "Scripted smoke epic",
            "stories": [
                {
                    "key": "SMOKE-1",
                    "title": "Scripted smoke story",
                    "description": "Write one file, commit it, and finish.",
                    "acceptance": ["true"],
                }
            ],
        }
    ]
}


def default_script() -> dict:
    """Return the shipped script: one canned answer sequence per role.

    Dispatch answers with the three harness calls that make a story land -
    ``create_file`` (one repo-relative file), ``bash`` (``git add`` of that
    file plus ``git commit``) and ``done`` - so the local agent harness
    completes a real write/commit/finish cycle against no model at all.
    """
    return {
        "decompose": [
            {
                "content": "```json\n" + json.dumps(_DECOMPOSE_PLAN, indent=2) + "\n```",
                "finish_reason": "stop",
            }
        ],
        "review": [
            {
                "content": (
                    "Scripted review: the change writes one file and commits it, "
                    "with no unrelated edits.\nVERDICT: APPROVE"
                ),
                "finish_reason": "stop",
            }
        ],
        "overlord": [
            {
                "content": (
                    "RULING: APPROVED\n"
                    "TIER: 1\n"
                    "RISK: low\n"
                    "RATIONALE: the scripted smoke run completed its story\n"
                    "ACTION: mark_done\n"
                    "NOTIFY_USER: no"
                ),
                "finish_reason": "stop",
            }
        ],
        "dispatch": [
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "create_file",
                        "id": "call-scripted-write",
                        "arguments": {
                            "path": "scripted_smoke_output.txt",
                            "content": "scripted smoke run\n",
                        },
                    }
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "bash",
                        "id": "call-scripted-commit",
                        "arguments": {
                            "command": (
                                "git add scripted_smoke_output.txt && "
                                'git commit -m "test(smoke): add scripted smoke output"'
                            )
                        },
                    }
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "done",
                        "id": "call-scripted-done",
                        "arguments": {
                            "summary": (
                                "scripted smoke run: wrote scripted_smoke_output.txt, "
                                "committed it, finished"
                            )
                        },
                    }
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Script loading / validation (boundary: an operator-supplied JSON file)
# ---------------------------------------------------------------------------


def validate_script(raw) -> dict:
    """Normalize and validate a script; raise :class:`ScriptError` when malformed.

    Every rejection message names the action and the offending role/index
    only - never a filesystem path, a payload or exception text.
    """
    if not isinstance(raw, dict):
        raise ScriptError(
            "scripted model script must be a JSON object keyed by role "
            "(action=validate_script)"
        )
    script: dict = {}
    for role, entries in raw.items():
        if role not in ROLE_NAMES:
            raise ScriptError(
                f"unknown role {role!r} in the scripted model script; expected one "
                f"of {sorted(ROLE_NAMES)} (action=validate_script)"
            )
        if not isinstance(entries, list) or not entries:
            raise ScriptError(
                f"role {role!r} must map to a non-empty list of entries "
                "(action=validate_script)"
            )
        normalized: list = []
        for index, entry in enumerate(entries):
            _validate_entry(role, index, entry)
            normalized.append(dict(entry))
        script[role] = normalized
    return script


def _validate_entry(role: str, index: int, entry) -> None:
    if not isinstance(entry, dict):
        raise ScriptError(
            f"role {role!r} entry {index} must be an object (action=validate_script)"
        )
    unknown = set(entry) - _ENTRY_KEYS
    if unknown:
        raise ScriptError(
            f"role {role!r} entry {index} has unsupported keys {sorted(unknown)} "
            "(action=validate_script)"
        )
    if "content" not in entry and "tool_calls" not in entry:
        raise ScriptError(
            f"role {role!r} entry {index} needs content or tool_calls "
            "(action=validate_script)"
        )
    if "content" in entry and not isinstance(entry["content"], str):
        raise ScriptError(
            f"role {role!r} entry {index} content must be a string "
            "(action=validate_script)"
        )
    if "tool_calls" in entry:
        _validate_tool_calls(role, index, entry["tool_calls"])
    if "finish_reason" in entry and not isinstance(entry["finish_reason"], str):
        raise ScriptError(
            f"role {role!r} entry {index} finish_reason must be a string "
            "(action=validate_script)"
        )


def _validate_tool_calls(role: str, index: int, tool_calls) -> None:
    if not isinstance(tool_calls, list):
        raise ScriptError(
            f"role {role!r} entry {index} tool_calls must be a list "
            "(action=validate_script)"
        )
    for call_index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            raise ScriptError(
                f"role {role!r} entry {index} tool call {call_index} must be an "
                "object (action=validate_script)"
            )
        name = call.get("name")
        if not isinstance(name, str) or not name:
            raise ScriptError(
                f"role {role!r} entry {index} tool call {call_index} needs a name "
                "(action=validate_script)"
            )
        unknown = set(call) - _TOOL_CALL_KEYS
        if unknown:
            raise ScriptError(
                f"role {role!r} entry {index} tool call {call_index} has unsupported "
                f"keys {sorted(unknown)} (action=validate_script)"
            )
        arguments = call.get("arguments", {})
        if not isinstance(arguments, (dict, str)):
            raise ScriptError(
                f"role {role!r} entry {index} tool call {call_index} arguments must "
                "be an object or a JSON string (action=validate_script)"
            )
        if "id" in call and not isinstance(call["id"], str):
            raise ScriptError(
                f"role {role!r} entry {index} tool call {call_index} id must be a "
                "string (action=validate_script)"
            )


def load_script(path=None) -> dict:
    """Return the validated script: the shipped default when ``path`` is None."""
    if path is None:
        return validate_script(default_script())
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScriptError(
            "could not read the scripted model script (action=load_script)"
        ) from exc
    return validate_script(raw)


# ---------------------------------------------------------------------------
# Role selection
# ---------------------------------------------------------------------------


def resolve_role(model) -> str:
    """Resolve the script role from a request's ``model``; raise otherwise.

    Longest match wins, so a model tag carrying more than one role name
    resolves deterministically.
    """
    if not isinstance(model, str) or not model.strip():
        raise UnknownRoleError(
            "unresolvable role: the request names no model (action=chat.completions)"
        )
    normalized = model.strip().lower()
    matches = [role for role in ROLE_NAMES if role in normalized]
    if not matches:
        raise UnknownRoleError(
            "unresolvable role: the model names no scripted role "
            f"(action=chat.completions); expected one of {sorted(ROLE_NAMES)}"
        )
    return max(matches, key=len)


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------


def _tool_calls(entry: dict, sequence: int) -> list:
    calls = []
    for index, call in enumerate(entry.get("tool_calls") or []):
        arguments = call.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        calls.append(
            {
                "id": call.get("id") or f"call-scripted-{sequence}-{index + 1}",
                "type": "function",
                "function": {"name": call["name"], "arguments": arguments},
            }
        )
    return calls


def _usage(messages, message: dict) -> dict:
    prompt_chars = 0
    for item in messages if isinstance(messages, list) else []:
        if isinstance(item, dict):
            prompt_chars += len(str(item.get("content") or ""))
    completion_chars = len(message.get("content") or "") + len(
        json.dumps(message.get("tool_calls") or [])
    )
    prompt_tokens = prompt_chars // 4
    completion_tokens = completion_chars // 4
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def build_completion(model: str, entry: dict, messages, sequence: int) -> dict:
    """Build the OpenAI chat-completion envelope for one script entry."""
    tool_calls = _tool_calls(entry, sequence)
    message: dict = {"role": "assistant", "content": entry.get("content", "")}
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish_reason = entry.get("finish_reason") or (
        "tool_calls" if tool_calls else "stop"
    )
    return {
        "id": f"chatcmpl-scripted-{sequence}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": _usage(messages, message),
    }


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class ScriptedModelServer:
    """Threaded HTTP server answering each request from a per-role cursor."""

    def __init__(self, script=None, *, host: str = "127.0.0.1", port: int = 0):
        self._script = validate_script(default_script() if script is None else script)
        self._cursors = {role: 0 for role in self._script}
        self._sequence = 0
        self._lock = threading.Lock()
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                owner._dispatch(self, method="POST")

            def do_GET(self):
                owner._dispatch(self, method="GET")

            def do_PUT(self):
                owner._dispatch(self, method="PUT")

            def log_message(self, fmt, *args):
                # The default handler writes the request line to stderr; the
                # request path stays out of the logs entirely.
                return

        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def _log_listening(self) -> None:
        _LOGGER.info(
            "scripted model backend listening for roles=%s", ",".join(sorted(self._script))
        )

    def start(self) -> ScriptedModelServer:
        """Serve in a background thread (returns self)."""
        self._log_listening()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="scripted-model-server",
            daemon=True,
        )
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        """Serve in the calling thread until :meth:`stop` (or Ctrl-C)."""
        self._log_listening()
        self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def cursor_state(self) -> dict:
        """Snapshot of each scripted role's cursor (entries consumed so far)."""
        with self._lock:
            return dict(self._cursors)

    # -- request handling --------------------------------------------------

    def _dispatch(self, handler: BaseHTTPRequestHandler, *, method: str) -> None:
        path = urllib.parse.urlsplit(handler.path).path.rstrip("/") or "/"
        if path not in _ACCEPTED_PATHS:
            self._send_error(handler, 404, "unknown endpoint for action=chat.completions")
            return
        if method != "POST":
            self._send_error(
                handler, 405, "method not allowed for action=chat.completions"
            )
            return
        body = self._read_body(handler)
        if body is None:
            return

        try:
            role = resolve_role(body.get("model"))
        except UnknownRoleError as exc:
            self._send_error(handler, 400, str(exc))
            return

        try:
            entry, sequence = self._next_entry(role)
        except LookupError as exc:
            self._send_error(handler, 500, str(exc))
            return

        payload = build_completion(str(body.get("model")), entry, body.get("messages"),
                                   sequence)
        _LOGGER.info("scripted model response role=%s status=200", role)
        self._send_json(handler, 200, payload)

    def _next_entry(self, role: str) -> tuple:
        """Read-then-advance one role's cursor; raise ``LookupError`` when empty.

        The entry is reserved and its response sequence claimed under one lock,
        so a cursor update can never race the response read.
        """
        with self._lock:
            entries = self._script.get(role)
            if not entries:
                raise LookupError(f"no scripted answers for role={role}")
            cursor = self._cursors.get(role, 0)
            if cursor >= len(entries):
                raise LookupError(f"scripted answers exhausted for role={role}")
            self._cursors[role] = cursor + 1
            self._sequence += 1
            return dict(entries[cursor]), self._sequence

    def _read_body(self, handler: BaseHTTPRequestHandler) -> dict | None:
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_error(
                handler, 400, "invalid request body for action=chat.completions"
            )
            return None
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._send_error(
                handler, 413, "request body too large for action=chat.completions"
            )
            return None
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_error(
                handler, 400, "invalid request body for action=chat.completions"
            )
            return None
        if not isinstance(body, dict):
            self._send_error(
                handler, 400, "invalid request body for action=chat.completions"
            )
            return None
        return body

    # -- wire helpers ------------------------------------------------------

    def _send_error(
        self, handler: BaseHTTPRequestHandler, status: int, message: str
    ) -> None:
        _LOGGER.warning(
            "scripted model error action=chat.completions status=%d", status
        )
        self._send_json(handler, status, {"error": {"message": message, "code": status}})

    def _send_json(
        self, handler: BaseHTTPRequestHandler, status: int, payload: dict
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    """Serve the script until interrupted; print the base URL and endpoint."""
    parser = argparse.ArgumentParser(
        description="Serve the scripted OpenAI chat-completions backend."
    )
    parser.add_argument(
        "--script",
        default=None,
        help="JSON script keyed by role (default: the shipped script)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="bind port (0 picks a free one)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", '
        '"service": "scripted_model_server", "message": "%(message)s"}',
    )
    server = ScriptedModelServer(load_script(args.script), host=args.host, port=args.port)
    print(f"scripted model backend: {server.base_url}{ENDPOINT_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
