"""Map the harness chat transcript to and from OpenAI's Responses API.

The harness (``app/backend_ollama.py`` and ``scripts/local_agent_chat.py``)
keeps an Ollama-shaped transcript: system/user/assistant messages, assistant
``tool_calls`` and bare ``tool`` results with no call id. The Responses API
instead takes a flat ``input`` list of role messages, ``function_call`` items
and ``function_call_output`` items that are paired by ``call_id``, and it
answers with an ``output`` list rather than a single message.

This module is the pure translation between the two. It does no I/O: no
HTTP, no ``os.environ``, no files. The provider that sends the request is
responsible for transport and credentials.

* :func:`responses_body` builds the request body. It never mutates the
  transcript or the tool list it is given, and it never sends sampling or
  token-limit keys (``temperature``, ``max_tokens``,
  ``max_completion_tokens``). ``max_output_tokens`` is a fixed reservation
  for reasoning plus output; the harness's ``num_ctx`` is a context size and
  is deliberately not used here.
* Call ids are deterministic. A tool call without an id gets
  ``call_<message_index>_<call_index>``, so the same transcript always yields
  the same request body.
* :func:`responses_envelope` maps a completed response back to the Ollama
  envelope the harness reads. A response that did not complete raises
  :class:`OpenAIResponseIncomplete`; its message carries the status and reason
  or error code only, never the payload's own text.
"""

import json

MAX_OUTPUT_TOKENS = 32768
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

_NO_RESULT = "(no result recorded)"


class OpenAIResponseIncomplete(RuntimeError):
    """A Responses API payload whose status is not ``completed``."""


def responses_body(messages, *, model, tools=None, reasoning_effort=None):
    """Build a Responses API request body from a harness transcript."""
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(
            f"unknown reasoning_effort {reasoning_effort!r}; "
            f"expected one of {', '.join(REASONING_EFFORTS)}"
        )

    body = {
        "model": model,
        "input": _input_items(messages),
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "store": False,
    }
    if tools:
        body["tools"] = [_flatten_tool(tool) for tool in tools]
    if reasoning_effort is not None:
        body["reasoning"] = {"effort": reasoning_effort}
    return body


def responses_envelope(payload):
    """Map a completed Responses API payload to the Ollama envelope."""
    status = payload.get("status")
    if status != "completed":
        raise OpenAIResponseIncomplete(_incomplete_message(payload, status))

    texts = []
    tool_calls = []
    for item in payload.get("output") or []:
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    texts.append(part.get("text") or "")
        elif item_type == "function_call":
            tool_calls.append({
                "id": item["call_id"],
                "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]},
            })

    message = {"role": "assistant", "content": "".join(texts)}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = payload.get("usage") or {}
    return {
        "message": message,
        "prompt_eval_count": usage.get("input_tokens") or 0,
        "eval_count": usage.get("output_tokens") or 0,
    }


def _incomplete_message(payload, status):
    detail = (payload.get("incomplete_details") or {}).get("reason") or (
        payload.get("error") or {}
    ).get("code")
    if detail:
        return f"OpenAI response status {status!r}: {detail}"
    return f"OpenAI response status {status!r}"


def _flatten_tool(tool):
    function = tool["function"]
    flat = {"type": "function", "name": function["name"]}
    if "description" in function:
        flat["description"] = function["description"]
    flat["parameters"] = function["parameters"]
    return flat


def _input_items(messages):
    items = []
    pending = []  # call_ids of the latest assistant turn still awaiting a result
    for message_index, message in enumerate(messages):
        role = message.get("role")
        if role == "tool":
            text = message.get("content") or ""
            if pending:
                items.append({
                    "type": "function_call_output",
                    "call_id": pending.pop(0),
                    "output": text,
                })
            else:
                items.append({"role": "user", "content": "Tool result:\n" + text})
            continue

        items.extend(_no_result_items(pending))
        text = message.get("content") or ""
        if role == "system":
            items.append({"role": "developer", "content": text})
        elif role == "user":
            items.append({"role": "user", "content": text})
        elif role == "assistant":
            if text:
                items.append({"role": "assistant", "content": text})
            for call_index, call in enumerate(message.get("tool_calls") or []):
                call_id = call.get("id") or f"call_{message_index}_{call_index}"
                pending.append(call_id)
                items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": call["function"]["name"],
                    "arguments": _arguments_text(call["function"]["arguments"]),
                })
        else:
            raise ValueError(f"unknown message role {role!r}")

    items.extend(_no_result_items(pending))
    return items


def _no_result_items(pending):
    items = [
        {"type": "function_call_output", "call_id": call_id, "output": _NO_RESULT}
        for call_id in pending
    ]
    pending.clear()
    return items


def _arguments_text(arguments):
    if isinstance(arguments, dict):
        return json.dumps(arguments)
    return arguments
