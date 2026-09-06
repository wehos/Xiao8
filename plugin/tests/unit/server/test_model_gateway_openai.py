from __future__ import annotations

from copy import deepcopy

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from plugin.server.model_gateway.errors import ModelGatewayError
from plugin.server.model_gateway.openai import OpenAIStreamConverter, convert_response


pytestmark = pytest.mark.plugin_unit


USAGE = {
    "prompt_tokens": 7,
    "completion_tokens": 3,
    "total_tokens": 10,
    "prompt_tokens_details": {"cached_tokens": 4},
}


def response(**updates):
    return {
        "id": "chatcmpl-test",
        "created": 1,
        "object": "chat.completion",
        "model": "private-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
        "usage": deepcopy(USAGE),
        **updates,
    }


def chunk(delta=None, *, finish_reason=None, **updates):
    return {
        "id": "chatcmpl-test",
        "created": 1,
        "object": "chat.completion.chunk",
        "model": "private-model",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
        **updates,
    }


def test_nonstream_preserves_openai_fields_and_detaches_original():
    body = response(system_fingerprint="fp-test", service_tier="default")
    body["choices"][0]["logprobs"] = {"content": [{"token": "hello", "logprob": -0.1, "bytes": [104], "top_logprobs": []}]}
    body["choices"][0]["message"]["refusal"] = None
    original = deepcopy(body)
    result = convert_response(body, model_alias="analysis")
    parsed = ChatCompletion.model_validate(result)
    assert parsed.model == "analysis"
    assert parsed.choices[0].message.content == "hello"
    assert parsed.usage.prompt_tokens_details.cached_tokens == 4
    assert parsed.choices[0].logprobs.content[0].logprob == -0.1
    assert result == {**original, "model": "analysis"}
    result["usage"]["prompt_tokens"] = 999
    assert body == original


def test_nonstream_preserves_parallel_tools_and_refusal():
    calls = [
        {"id": "call-a", "type": "function", "function": {"name": "lookup", "arguments": '{"key":"a"}'}},
        {"id": "call-b", "type": "function", "function": {"name": "lookup", "arguments": '{"key":"b"}'}},
    ]
    body = response()
    body["choices"][0].update(message={"role": "assistant", "content": None, "tool_calls": calls}, finish_reason="tool_calls")
    result = convert_response(body, model_alias="analysis")
    assert ChatCompletion.model_validate(result).choices[0].message.tool_calls[1].id == "call-b"
    assert result["choices"][0]["message"]["tool_calls"] == calls
    refusal = response()
    refusal["choices"][0]["message"] = {"role": "assistant", "content": None, "refusal": "Cannot answer"}
    assert convert_response(refusal, model_alias="analysis")["choices"][0]["message"]["refusal"] == "Cannot answer"


def test_missing_usage_is_not_reported_as_zero():
    body = response()
    body.pop("usage")
    assert "usage" not in convert_response(body, model_alias="analysis")
    converter = OpenAIStreamConverter("analysis")
    converter.feed(chunk({"content": "hello"}, finish_reason="stop"))
    converter.finish()
    assert converter.usage is None


@pytest.mark.parametrize("body", [
    {"error": {"message": "private-prompt-and-secret"}},
    response(id=None),
    response(created=True),
    response(object="unexpected"),
    response(choices=[]),
    response(choices=[{"index": 0, "message": {"role": "user", "content": "private-prompt-and-secret"}, "finish_reason": "stop"}]),
    response(choices=[{"index": 0, "message": {"role": "assistant", "content": []}, "finish_reason": "stop"}]),
    response(choices=[{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "invalid"}]),
    response(choices=[{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": "tool_calls"}]),
    response(usage={"prompt_tokens": -1, "completion_tokens": 3, "total_tokens": 2}),
    response(usage={"prompt_tokens": 1}),
])
def test_nonstream_rejects_bad_responses_without_echoing_provider(body):
    with pytest.raises(ModelGatewayError) as exc:
        convert_response(body, model_alias="analysis")
    assert exc.value.status_code == 502
    assert "private-prompt-and-secret" not in str(exc.value.to_dict())


@pytest.mark.parametrize("call", [
    {"type": "function", "function": {"name": "lookup", "arguments": "{}"}},
    {"id": "a", "type": "function", "function": {"name": "lookup", "arguments": {}}},
    {"id": "a", "type": "function", "function": {"arguments": "{}"}},
    {"id": "a", "type": "custom", "custom": {"name": "lookup", "input": "{}"}},
])
def test_nonstream_rejects_invalid_tool_calls(call):
    body = response()
    body["choices"][0]["message"]["tool_calls"] = [call]
    with pytest.raises(ModelGatewayError):
        convert_response(body, model_alias="analysis")


def test_stream_forwards_chunks_and_usage_without_accumulating_text():
    converter = OpenAIStreamConverter("analysis")
    incoming = [
        chunk({"role": "assistant", "content": ""}, usage=None),
        chunk({"content": "hello"}),
        chunk({"content": " world"}),
        chunk(finish_reason="stop"),
        chunk(choices=[], usage=deepcopy(USAGE)),
    ]
    original = deepcopy(incoming)
    outgoing = [item for event in incoming for item in converter.feed(event)]
    converter.finish()
    assert incoming == original
    assert [item["model"] for item in outgoing] == ["analysis"] * 5
    assert [ChatCompletionChunk.model_validate(item).id for item in outgoing] == ["chatcmpl-test"] * 5
    assert outgoing[1]["choices"][0]["delta"]["content"] == "hello"
    assert outgoing[2]["choices"][0]["delta"]["content"] == " world"
    assert converter.usage == USAGE
    assert converter.finished


def test_stream_keeps_interleaved_tool_indices_and_json_fragments():
    converter = OpenAIStreamConverter("analysis")
    deltas = [
        {"tool_calls": [{"index": 0, "id": "call-a", "type": "function", "function": {"name": "lookup", "arguments": '{"key":'}}]},
        {"tool_calls": [{"index": 1, "id": "call-b", "type": "function", "function": {"name": "lookup", "arguments": '{"key":'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]},
        {"tool_calls": [{"index": 1, "function": {"arguments": '"b"}'}}]},
    ]
    for delta in deltas:
        result = converter.feed(chunk(delta))
        assert result[0]["choices"][0]["delta"] == delta
        ChatCompletionChunk.model_validate(result[0])
    converter.feed(chunk(finish_reason="tool_calls"))
    converter.finish()


def test_stream_suppresses_unrequested_usage_but_still_records_it():
    converter = OpenAIStreamConverter("analysis", include_usage=False)
    result = converter.feed(chunk({"content": "hello"}, usage=deepcopy(USAGE)))
    assert "usage" not in result[0]
    converter.feed(chunk(finish_reason="stop"))
    usage_event = chunk(choices=[], usage=deepcopy(USAGE))
    assert converter.feed(usage_event) == []
    converter.finish()
    assert converter.usage == USAGE
    usage_event["usage"]["prompt_tokens"] = 500
    assert converter.usage["prompt_tokens"] == 7


def test_stream_preserves_function_name_fragments():
    converter = OpenAIStreamConverter("analysis")
    first = {"tool_calls": [{"index": 0, "id": "call-a", "function": {"name": "look", "arguments": ""}}]}
    second = {"tool_calls": [{"index": 0, "function": {"name": "up", "arguments": "{}"}}]}
    assert converter.feed(chunk(first))[0]["choices"][0]["delta"] == first
    assert converter.feed(chunk(second))[0]["choices"][0]["delta"] == second
    converter.feed(chunk(finish_reason="tool_calls"))
    converter.finish()


def test_stream_can_fill_later_envelopes_without_changing_identity():
    converter = OpenAIStreamConverter("analysis")
    converter.feed(chunk({"role": "assistant"}))
    result = converter.feed({"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]})
    assert result[0]["id"] == "chatcmpl-test"
    assert result[0]["created"] == 1
    assert ChatCompletionChunk.model_validate(result[0]).model == "analysis"
    converter.finish()


@pytest.mark.parametrize("delta", [
    {"reasoning_content": "Considering the question"},
    {"content": "hello", "extra_content": {"provider": {"signature": "opaque-value"}}},
    {"audio": {"data": "opaque-audio-chunk"}},
    {"function_call": {"name": "legacy", "arguments": "{}"}},
])
def test_stream_preserves_opaque_extensions_for_official_sdk(delta):
    original = deepcopy(delta)
    converter = OpenAIStreamConverter("analysis")
    result = converter.feed(chunk(delta))[0]
    parsed = ChatCompletionChunk.model_validate(result)
    assert parsed.model == "analysis"
    assert parsed.choices[0].delta.model_dump(exclude_unset=True) == original
    assert result["choices"][0]["delta"] == original
    result["choices"][0]["delta"]["opaque_new_field"] = "changed"
    assert delta == original
    converter.feed(chunk(finish_reason="stop"))
    converter.finish()


@pytest.mark.parametrize("event", [
    chunk(id="changed-id"),
    chunk(created=2),
    chunk(object="chat.completion"),
    chunk({"content": ["bad"]}),
    chunk({"content": ["bad"], "reasoning_content": "Valid extension does not bypass validation"}),
    chunk({"role": "user"}),
    chunk(choices=[]),
    chunk(choices=[], usage=USAGE),
    chunk(choices=[{"index": 1, "delta": {}, "finish_reason": None}]),
    chunk({"tool_calls": [{"index": -1, "function": {"arguments": "{}"}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": {}}}]}),
    {"error": {"message": "private-prompt-and-secret"}},
])
def test_stream_rejects_malformed_or_unexpected_events(event):
    converter = OpenAIStreamConverter("analysis")
    converter.feed(chunk({"role": "assistant"}))
    with pytest.raises(ModelGatewayError) as exc:
        converter.feed(event)
    assert exc.value.status_code == 502
    assert "private-prompt-and-secret" not in str(exc.value.to_dict())


def test_stream_requires_finish_reason_and_rejects_content_after_finish():
    converter = OpenAIStreamConverter("analysis")
    converter.feed(chunk({"content": "partial"}))
    with pytest.raises(ModelGatewayError) as exc:
        converter.finish()
    assert exc.value.code == "incomplete_upstream_stream"
    converter.feed(chunk(finish_reason="stop"))
    with pytest.raises(ModelGatewayError):
        converter.feed(chunk({"content": "late"}))
    with pytest.raises(ModelGatewayError):
        converter.feed(chunk())


def test_stream_rejects_unidentified_tools_at_completion():
    converter = OpenAIStreamConverter("analysis")
    converter.feed(chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}))
    with pytest.raises(ModelGatewayError):
        converter.feed(chunk(finish_reason="tool_calls"))


def test_stream_rejects_changed_or_reused_tool_ids():
    converter = OpenAIStreamConverter("analysis")
    converter.feed(chunk({"tool_calls": [{"index": 0, "id": "call-a", "function": {"name": "lookup"}}]}))
    with pytest.raises(ModelGatewayError):
        converter.feed(chunk({"tool_calls": [{"index": 0, "id": "changed"}]}))
    with pytest.raises(ModelGatewayError):
        converter.feed(chunk({"tool_calls": [{"index": 1, "id": "call-a"}]}))
