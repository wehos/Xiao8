from __future__ import annotations

import copy
import json

import pytest

from plugin.server.model_gateway.anthropic import (
    AnthropicStreamConverter,
    convert_response,
    prepare_request,
)
from plugin.server.model_gateway.errors import ModelGatewayError


pytestmark = pytest.mark.plugin_unit


def _request(**kwargs):
    return {
        "model": "claude-configured",
        "messages": [{"role": "user", "content": "Hello"}],
        **kwargs,
    }


def _response(**kwargs):
    return {
        "type": "message",
        "id": "msg_example",
        "role": "assistant",
        "model": "actual-model",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 2},
        **kwargs,
    }


def _start(**kwargs):
    return {
        "type": "message_start",
        "message": _response(content=[], stop_reason=None, **kwargs),
    }


def _block(index, block):
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index, **kwargs):
    return {"type": "content_block_delta", "index": index, "delta": kwargs}


def _stop(index):
    return {"type": "content_block_stop", "index": index}


def _end(reason="end_turn", **usage):
    return [
        {"type": "message_delta", "delta": {"stop_reason": reason}, "usage": usage},
        {"type": "message_stop"},
    ]


def test_request_preserves_interleaved_images_and_system_prefix():
    request = _request(
        max_completion_tokens=300,
        temperature=0.3,
        stop="END",
        messages=[
            {"role": "system", "content": "Be concise"},
            {"role": "developer", "content": [{"type": "text", "text": "Use Chinese"}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/a.png"},
                    },
                    {"type": "text", "text": "Second"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,aGVsbG8=",
                            "detail": "auto",
                        },
                    },
                ],
            },
        ],
    )
    original = copy.deepcopy(request)
    result = prepare_request(request)
    assert request == original
    assert result["model"] == "claude-configured"
    assert result["max_tokens"] == 300
    assert result["stop_sequences"] == ["END"]
    assert result["system"] == [
        {"type": "text", "text": "Be concise"},
        {"type": "text", "text": "Use Chinese"},
    ]
    assert result["messages"][0]["content"] == [
        {"type": "text", "text": "First"},
        {
            "type": "image",
            "source": {"type": "url", "url": "https://example.com/a.png"},
        },
        {"type": "text", "text": "Second"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
        },
    ]


def test_tools_and_contiguous_results_remain_associated():
    request = _request(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "look",
                    "description": "Inspect",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice="required",
        parallel_tool_calls=False,
        messages=[
            {"role": "user", "content": "Inspect both"},
            {
                "role": "assistant",
                "content": "Checking",
                "tool_calls": [
                    {
                        "id": "one",
                        "type": "function",
                        "function": {"name": "look", "arguments": '{"target":"第一"}'},
                    },
                    {
                        "id": "two",
                        "type": "function",
                        "function": {"name": "look", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "one", "content": "First result"},
            {"role": "tool", "tool_call_id": "two", "content": "Second result"},
            {"role": "user", "content": "Compare"},
        ],
    )
    result = prepare_request(request)
    assert result["tools"] == [
        {"name": "look", "description": "Inspect", "input_schema": {"type": "object"}}
    ]
    assert result["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert result["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "one",
        "name": "look",
        "input": {"target": "第一"},
    }
    assert result["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "one",
                "content": [{"type": "text", "text": "First result"}],
            },
            {
                "type": "tool_result",
                "tool_use_id": "two",
                "content": [{"type": "text", "text": "Second result"}],
            },
            {"type": "text", "text": "Compare"},
        ],
    }


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("none", {"type": "none"}),
        ("auto", {"type": "auto"}),
        (
            {"type": "function", "function": {"name": "look"}},
            {"type": "tool", "name": "look"},
        ),
    ],
)
def test_tool_choice_mapping(choice, expected):
    result = prepare_request(
        _request(
            tools=[{"type": "function", "function": {"name": "look"}}],
            tool_choice=choice,
        )
    )
    assert result["tool_choice"] == expected


@pytest.mark.parametrize(
    "overrides,param",
    [
        ({"response_format": {"type": "json_object"}}, "response_format"),
        ({"temperature": 1.5}, "temperature"),
        ({"n": 2}, "n"),
        ({"thinking": {"type": "enabled"}}, "thinking"),
        (
            {
                "tools": [
                    {"type": "function", "function": {"name": "test", "strict": True}}
                ]
            },
            "tools[0].function.strict",
        ),
        (
            {"messages": [{"role": "user", "name": "Alice", "content": "Hello"}]},
            "messages[0].name",
        ),
        (
            {
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "system", "content": "Later"},
                ]
            },
            "messages[1].role",
        ),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {}}],
                    }
                ]
            },
            "messages[0].content[0]",
        ),
    ],
)
def test_unsupported_parameters_are_not_silently_removed(overrides, param):
    with pytest.raises(ModelGatewayError) as exc:
        prepare_request(_request(**overrides))
    assert exc.value.code == "unsupported_parameter"
    assert exc.value.param == param


@pytest.mark.parametrize(
    "image",
    [
        {"url": "https://example.com/image", "detail": "high"},
        {"url": "file:///private/image.png"},
        {"url": "data:image/svg+xml;base64,aGVsbG8="},
        {"url": "data:image/png;base64,%%%"},
        {"url": "data:image/png;base64,"},
    ],
)
def test_unsupported_or_invalid_images_fail_before_transport(image):
    with pytest.raises(ModelGatewayError):
        prepare_request(
            _request(
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": image}],
                    }
                ]
            )
        )


@pytest.mark.parametrize("arguments", ["not JSON", "[]", "null", '{"value":NaN}'])
def test_tool_arguments_must_be_json_objects(arguments):
    with pytest.raises(ModelGatewayError) as exc:
        prepare_request(
            _request(
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "one",
                                "function": {"name": "look", "arguments": arguments},
                            }
                        ],
                    }
                ]
            )
        )
    assert exc.value.code == "invalid_request"


def test_response_preserves_tools_and_counts_all_input_tokens():
    result = convert_response(
        _response(
            content=[
                {"type": "thinking", "thinking": "private", "signature": "opaque"},
                {
                    "type": "text",
                    "text": "Checking",
                    "citations": [{"text": "private citation"}],
                },
                {
                    "type": "tool_use",
                    "id": "tool1",
                    "name": "look",
                    "input": {"target": "第一"},
                },
            ],
            stop_reason="tool_use",
            usage={
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 3,
            },
        ),
        model_alias="analysis",
    )
    assert result["model"] == "analysis"
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    message = result["choices"][0]["message"]
    assert message["content"] == "Checking"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {
        "target": "第一"
    }
    assert result["usage"] == {
        "prompt_tokens": 15,
        "completion_tokens": 2,
        "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 7},
    }
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("refusal", "content_filter"),
    ],
)
def test_finish_reasons(reason, expected):
    assert (
        convert_response(_response(stop_reason=reason), model_alias="analysis")[
            "choices"
        ][0]["finish_reason"]
        == expected
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"stop_reason": None},
        {"stop_reason": "pause_turn"},
        {"usage": "bad"},
        {"id": None},
        {"content": [{"type": "text", "text": 42}]},
        {"content": [{"type": "server_tool_use"}]},
        {"content": [{"type": "tool_use", "id": "one", "name": "look", "input": []}]},
        {"usage": {"input_tokens": -1, "output_tokens": 2}},
    ],
)
def test_invalid_responses_fail_safely(overrides):
    with pytest.raises(ModelGatewayError) as exc:
        convert_response(_response(**overrides), model_alias="analysis")
    assert exc.value.status_code == 502


def test_stream_interleaved_tools_use_tool_indices_and_final_cumulative_usage():
    converter = AnthropicStreamConverter("analysis")
    events = [
        _start(
            usage={"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 7}
        ),
        _block(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _delta(0, type="thinking_delta", thinking="private"),
        _delta(0, type="signature_delta", signature="private signature"),
        _stop(0),
        _block(1, {"type": "text", "text": "Checking "}),
        _delta(1, type="text_delta", text="both"),
        _stop(1),
        _block(2, {"type": "tool_use", "id": "one", "name": "look", "input": {}}),
        _block(3, {"type": "tool_use", "id": "two", "name": "look", "input": {}}),
        _delta(2, type="input_json_delta", partial_json='{"a":'),
        _delta(3, type="input_json_delta", partial_json=""),
        _delta(2, type="input_json_delta", partial_json="1}"),
        _stop(2),
        _stop(3),
        {
            "type": "message_delta",
            "delta": {"stop_reason": None},
            "usage": {"output_tokens": 3},
        },
        *_end("tool_use", output_tokens=8),
    ]
    chunks = [chunk for event in events for chunk in converter.feed(event)]
    converter.finish()
    assert converter.done
    assert "private" not in json.dumps(chunks)
    assert all(chunk["model"] == "analysis" for chunk in chunks)
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks if chunk["choices"]]
    assert "".join(delta.get("content", "") for delta in deltas) == "Checking both"
    tools = {}
    for delta in deltas:
        for call in delta.get("tool_calls", []):
            target = tools.setdefault(call["index"], {"arguments": ""})
            target["arguments"] += call["function"]["arguments"]
            if "id" in call:
                target["id"] = call["id"]
    assert tools == {
        0: {"id": "one", "arguments": '{"a":1}'},
        1: {"id": "two", "arguments": "{}"},
    }
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["choices"] == []
    assert (
        converter.usage
        == chunks[-1]["usage"]
        == {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 7},
        }
    )


def test_usage_remains_available_without_emitting_usage_chunks():
    converter = AnthropicStreamConverter("analysis", include_usage=False)
    chunks = [
        chunk
        for event in [_start(), *_end(output_tokens=9)]
        for chunk in converter.feed(event)
    ]
    converter.finish()
    assert all("usage" not in chunk for chunk in chunks)
    assert all(chunk["choices"] for chunk in chunks)
    assert converter.usage["completion_tokens"] == 9


def test_stream_with_missing_usage_does_not_claim_zero_tokens():
    converter = AnthropicStreamConverter("analysis")
    chunks = [
        chunk
        for event in [_start(usage={}), *_end()]
        for chunk in converter.feed(event)
    ]
    assert converter.usage is None
    assert chunks[-1]["usage"] is None


@pytest.mark.parametrize(
    "events",
    [
        [],
        [_start()],
        [_start(), *_end()[:-1]],
        [_start(), _block(0, {"type": "text", "text": ""})],
    ],
)
def test_truncated_stream_is_not_success(events):
    converter = AnthropicStreamConverter("analysis")
    for event in events:
        converter.feed(event)
    with pytest.raises(ModelGatewayError) as exc:
        converter.finish()
    assert exc.value.status_code == 502


@pytest.mark.parametrize(
    "events",
    [
        [_start(), _start()],
        [_start(), {"type": "message_stop"}],
        [_start(), _delta(0, type="text_delta", text="x")],
        [_start(), _block(0, {"type": "text", "text": ""}), *_end()],
        [_start(), _block(0, {"type": "server_tool_use"})],
        [_start(), *_end(), {"type": "message_stop"}],
    ],
)
def test_invalid_stream_order_is_rejected(events):
    converter = AnthropicStreamConverter("analysis")
    with pytest.raises(ModelGatewayError) as exc:
        for event in events:
            converter.feed(event)
    assert exc.value.status_code == 502


def test_stream_error_is_sanitized():
    converter = AnthropicStreamConverter("analysis")
    with pytest.raises(ModelGatewayError) as exc:
        converter.feed({"type": "error", "error": {"message": "secret provider key"}})
    assert "secret" not in str(exc.value)
    assert exc.value.code == "upstream_error"


@pytest.mark.parametrize("content", [None, "Explanation"])
def test_assistant_history_refusal_is_preserved_as_text(content):
    result = prepare_request(
        _request(
            messages=[
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": content, "refusal": "Cannot answer"},
                {"role": "user", "content": "Follow-up"},
            ]
        )
    )
    expected = [{"type": "text", "text": "Cannot answer"}]
    if content is not None:
        expected.insert(0, {"type": "text", "text": content})
    assert result["messages"][1] == {"role": "assistant", "content": expected}


@pytest.mark.parametrize("bad_type", [[], {}, None, True, 42])
def test_non_string_response_block_type_is_a_safe_error(bad_type):
    with pytest.raises(ModelGatewayError) as exc:
        convert_response(
            _response(content=[{"type": bad_type}]), model_alias="analysis"
        )
    assert exc.value.code == "invalid_upstream_response"
    assert exc.value.status_code == 502


@pytest.mark.parametrize("bad_type", [[], {}, None, True, 42])
@pytest.mark.parametrize("location", ["event", "block", "delta"])
def test_non_string_stream_types_are_safe_errors(bad_type, location):
    converter = AnthropicStreamConverter("analysis")
    converter.feed(_start())
    if location == "event":
        event = {"type": bad_type}
    elif location == "block":
        event = _block(0, {"type": bad_type})
    else:
        converter.feed(_block(0, {"type": "thinking"}))
        event = _delta(0, type=bad_type)
    with pytest.raises(ModelGatewayError) as exc:
        converter.feed(event)
    assert exc.value.code == "invalid_upstream_response"
    assert exc.value.status_code == 502


@pytest.mark.parametrize("bad_value", [[], {}, None, True, -1, 0.5])
def test_malformed_stream_indices_are_safe_errors(bad_value):
    converter = AnthropicStreamConverter("analysis")
    converter.feed(_start())
    with pytest.raises(ModelGatewayError) as exc:
        converter.feed(_block(bad_value, {"type": "text", "text": ""}))
    assert exc.value.status_code == 502


@pytest.mark.parametrize("bad_value", [[], {}, True, 42])
def test_malformed_stop_reasons_are_safe_errors(bad_value):
    with pytest.raises(ModelGatewayError) as exc:
        convert_response(_response(stop_reason=bad_value), model_alias="analysis")
    assert exc.value.status_code == 502
    converter = AnthropicStreamConverter("analysis")
    converter.feed(_start())
    with pytest.raises(ModelGatewayError) as exc:
        converter.feed(_end(bad_value)[0])
    assert exc.value.status_code == 502
