"""Tests for the harness-transcript <-> OpenAI Responses API mapping."""

import copy
import json

import pytest

from app.openai_responses import (
    MAX_OUTPUT_TOKENS,
    OpenAIResponseIncomplete,
    responses_body,
    responses_envelope,
)


def _call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


# --- responses_body: messages -------------------------------------------------


def test_system_message_maps_to_developer_role():
    body = responses_body([{"role": "system", "content": "be brief"}], model="m")

    assert body["input"] == [{"role": "developer", "content": "be brief"}]


def test_user_message_maps_to_user_role():
    body = responses_body([{"role": "user", "content": "hi"}], model="m")

    assert body["input"] == [{"role": "user", "content": "hi"}]


def test_assistant_text_maps_to_assistant_message():
    body = responses_body([{"role": "assistant", "content": "hello"}], model="m")

    assert body["input"] == [{"role": "assistant", "content": "hello"}]


def test_none_content_is_treated_as_empty_string():
    body = responses_body([{"role": "user", "content": None}], model="m")

    assert body["input"] == [{"role": "user", "content": ""}]


def test_two_calls_then_two_tool_messages_pair_call_ids_in_order():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            _call("c1", "read", "{}"),
            _call("c2", "write", "{}"),
        ]},
        {"role": "tool", "content": "r1"},
        {"role": "tool", "content": "r2"},
    ]

    body = responses_body(messages, model="m")

    assert body["input"] == [
        {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"},
        {"type": "function_call", "call_id": "c2", "name": "write", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "r1"},
        {"type": "function_call_output", "call_id": "c2", "output": "r2"},
    ]


def test_call_without_id_gets_positional_id_and_tool_output_reuses_it():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "content": "done"},
    ]

    body = responses_body(messages, model="m")

    assert body["input"][1] == {
        "type": "function_call", "call_id": "call_1_0", "name": "read", "arguments": "{}",
    }
    assert body["input"][2] == {
        "type": "function_call_output", "call_id": "call_1_0", "output": "done",
    }


def test_empty_id_is_replaced_with_positional_id():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            _call("", "read", "{}"),
        ]},
    ]

    body = responses_body(messages, model="m")

    assert body["input"][0]["call_id"] == "call_0_0"


def test_dict_arguments_are_json_encoded():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            _call("c1", "read", {"path": "a.txt", "n": 2}),
        ]},
    ]

    body = responses_body(messages, model="m")

    assert json.loads(body["input"][0]["arguments"]) == {"path": "a.txt", "n": 2}


def test_string_arguments_pass_through_unchanged():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            _call("c1", "read", '{"path": "a.txt"}'),
        ]},
    ]

    body = responses_body(messages, model="m")

    assert body["input"][0]["arguments"] == '{"path": "a.txt"}'


def test_tool_message_with_nothing_pending_becomes_user_tool_result():
    messages = [{"role": "tool", "content": "orphan"}]

    body = responses_body(messages, model="m")

    assert body["input"] == [{"role": "user", "content": "Tool result:\norphan"}]


def test_unanswered_call_before_next_user_message_gets_no_result_marker():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", "read", "{}")]},
        {"role": "user", "content": "next"},
    ]

    body = responses_body(messages, model="m")

    assert body["input"][1] == {
        "type": "function_call_output", "call_id": "c1", "output": "(no result recorded)",
    }
    assert body["input"][2] == {"role": "user", "content": "next"}


def test_unanswered_call_at_transcript_end_gets_no_result_marker():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", "read", "{}")]},
    ]

    body = responses_body(messages, model="m")

    assert body["input"][-1] == {
        "type": "function_call_output", "call_id": "c1", "output": "(no result recorded)",
    }


def test_empty_assistant_content_with_tool_calls_emits_no_message_item():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", "read", "{}")]},
    ]

    body = responses_body(messages, model="m")

    assert all(item.get("role") != "assistant" for item in body["input"])


def test_unknown_role_raises_value_error_naming_the_role():
    with pytest.raises(ValueError, match="narrator"):
        responses_body([{"role": "narrator", "content": "x"}], model="m")


# --- responses_body: tools, reasoning, fixed fields ---------------------------


def test_tools_are_flattened_to_responses_form():
    tools = [{
        "type": "function",
        "function": {"name": "read", "description": "Read a file", "parameters": {"type": "object"}},
    }]

    body = responses_body([], model="m", tools=tools)

    assert body["tools"] == [{
        "type": "function", "name": "read", "description": "Read a file",
        "parameters": {"type": "object"},
    }]


def test_tool_without_description_omits_description_key():
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]

    body = responses_body([], model="m", tools=tools)

    assert "description" not in body["tools"][0]


def test_tools_none_omits_tools_key():
    body = responses_body([], model="m", tools=None)

    assert "tools" not in body


def test_tools_empty_list_omits_tools_key():
    body = responses_body([], model="m", tools=[])

    assert "tools" not in body


def test_reasoning_omitted_when_effort_is_none():
    body = responses_body([], model="m", reasoning_effort=None)

    assert "reasoning" not in body


def test_reasoning_present_when_effort_is_given():
    body = responses_body([], model="m", reasoning_effort="high")

    assert body["reasoning"] == {"effort": "high"}


def test_unknown_reasoning_effort_raises_value_error_naming_the_value():
    with pytest.raises(ValueError, match="turbo"):
        responses_body([], model="m", reasoning_effort="turbo")


def test_body_never_carries_sampling_or_token_limit_keys():
    body = responses_body([{"role": "user", "content": "hi"}], model="m", reasoning_effort="low")

    for key in ("temperature", "max_tokens", "max_completion_tokens"):
        assert key not in body


def test_max_output_tokens_is_the_reserved_budget():
    body = responses_body([], model="m")

    assert body["max_output_tokens"] == MAX_OUTPUT_TOKENS == 32768


def test_store_is_disabled():
    body = responses_body([], model="m")

    assert body["store"] is False


def test_model_is_passed_through():
    body = responses_body([], model="gpt-x")

    assert body["model"] == "gpt-x"


def test_inputs_are_not_mutated():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "", "tool_calls": [
            _call("", "read", {"path": "a"}),
            _call("c2", "write", "{}"),
        ]},
        {"role": "tool", "content": "r"},
        {"role": "user", "content": "u"},
    ]
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
    messages_before = copy.deepcopy(messages)
    tools_before = copy.deepcopy(tools)

    responses_body(messages, model="m", tools=tools, reasoning_effort="low")

    assert messages == messages_before
    assert tools == tools_before


# --- responses_envelope -------------------------------------------------------


def _completed(output, usage=None):
    payload = {"status": "completed", "output": output}
    if usage is not None:
        payload["usage"] = usage
    return payload


def test_text_only_response_maps_to_assistant_message():
    payload = _completed(
        [{"type": "message", "content": [{"type": "output_text", "text": "Hel"},
                                          {"type": "output_text", "text": "lo"}]}],
        usage={"input_tokens": 7, "output_tokens": 3},
    )

    envelope = responses_envelope(payload)

    assert envelope == {
        "message": {"role": "assistant", "content": "Hello"},
        "prompt_eval_count": 7,
        "eval_count": 3,
    }


def test_function_call_response_maps_to_tool_calls():
    payload = _completed(
        [{"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"}],
        usage={"input_tokens": 1, "output_tokens": 2},
    )

    envelope = responses_envelope(payload)

    assert envelope["message"] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
    }


def test_reasoning_item_is_ignored():
    payload = _completed(
        [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
        ],
        usage={"input_tokens": 1, "output_tokens": 1},
    )

    envelope = responses_envelope(payload)

    assert envelope["message"]["content"] == "answer"
    assert "tool_calls" not in envelope["message"]


def test_missing_usage_counts_as_zero():
    envelope = responses_envelope(_completed([]))

    assert envelope["prompt_eval_count"] == 0
    assert envelope["eval_count"] == 0


def test_null_usage_counts_as_zero():
    envelope = responses_envelope({"status": "completed", "output": [], "usage": None})

    assert envelope["prompt_eval_count"] == 0
    assert envelope["eval_count"] == 0


def test_incomplete_status_raises_naming_the_reason():
    payload = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}

    with pytest.raises(OpenAIResponseIncomplete, match="max_output_tokens"):
        responses_envelope(payload)


def test_failed_status_raises_naming_the_error_code():
    payload = {"status": "failed", "error": {"code": "server_error", "message": "secret text"}, "output": []}

    with pytest.raises(OpenAIResponseIncomplete, match="server_error") as info:
        responses_envelope(payload)

    assert "secret text" not in str(info.value)


def test_incomplete_error_is_a_runtime_error():
    assert issubclass(OpenAIResponseIncomplete, RuntimeError)
