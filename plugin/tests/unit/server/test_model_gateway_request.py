"""Public request contract: fidelity, capability boundaries and safe validation."""
from __future__ import annotations

import copy

import pytest
from openai.types.chat import ChatCompletionMessage

from plugin.server.domain.model_config import ModelDefaults, ModelSlot
from plugin.server.model_gateway.errors import ModelGatewayError
from plugin.server.model_gateway.request import prepare_chat_request


@pytest.fixture
def slot() -> ModelSlot:
    return ModelSlot(
        name="Plugin analysis", protocol="openai_chat", base_url="https://example.com/v1",
        model="actual-model", api_key="upstream-secret",
        capabilities=["text", "image_input", "tool_calling", "streaming"],
    )


@pytest.fixture
def body() -> dict:
    return {"model": "analysis", "messages": [{"role": "user", "content": "Keep this prompt unchanged."}]}


def test_defaults_and_model_resolution_preserve_original(slot, body):
    original = copy.deepcopy(body)
    result = prepare_chat_request(slot, body)
    assert result["model"] == "actual-model"
    assert result["max_completion_tokens"] == 1024
    assert result["stream"] is False
    assert body == original
    result["messages"][0]["content"] = "mutated"
    assert body == original


def test_explicit_options_override_defaults_even_for_models_with_special_main_rules(slot, body):
    slot.model = "o3"
    slot.defaults = ModelDefaults(temperature=0.8, max_output_tokens=500)
    body.update(temperature=0, max_completion_tokens=123, stream=True, stream_options={"include_usage": False})
    result = prepare_chat_request(slot, body)
    assert result["temperature"] == 0
    assert result["max_completion_tokens"] == 123
    assert "max_tokens" not in result
    assert result["stream_options"] == {"include_usage": False}


def test_slot_defaults_are_applied(slot, body):
    slot.defaults = ModelDefaults(temperature=0.3, max_output_tokens=2048)
    result = prepare_chat_request(slot, body)
    assert result["temperature"] == 0.3
    assert result["max_completion_tokens"] == 2048


def test_ordered_images_and_text_are_preserved(slot, body):
    content = [
        {"type": "text", "text": "A"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "detail": "high"}},
        {"type": "text", "text": "B"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]
    body["messages"][0]["content"] = content
    result = prepare_chat_request(slot, body)
    assert result["messages"][0]["content"] == content
    assert result["messages"][0]["content"] is not content


def _call(call_id="call_1", arguments='{"location":"Paris"}') -> dict:
    return {"id": call_id, "type": "function", "function": {"name": "weather", "arguments": arguments}}


def _tool() -> dict:
    return {"type": "function", "function": {"name": "weather", "description": "Weather", "parameters": {"type": "object"}, "strict": True}}


def test_parallel_tool_history_preserves_ids_order_and_exact_arguments(slot, body):
    body["messages"] += [
        {"role": "assistant", "content": None, "tool_calls": [_call(), _call("call_2", '{ "location": "Tokyo" }')]},
        {"role": "tool", "tool_call_id": "call_2", "content": [{"type": "text", "text": "sunny"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "cloudy"},
    ]
    body.update(tools=[_tool()], tool_choice={"type": "function", "function": {"name": "weather"}}, parallel_tool_calls=False)
    result = prepare_chat_request(slot, body)
    assert result["messages"] == body["messages"]
    assert result["tools"] == body["tools"]
    assert result["parallel_tool_calls"] is False


@pytest.mark.parametrize("messages", [
    [{"role": "tool", "tool_call_id": "call_1", "content": "orphan"}],
    [{"role": "assistant", "tool_calls": [_call()]}],
    [{"role": "assistant", "tool_calls": [_call()]}, {"role": "user", "content": "skip tool"}],
    [{"role": "assistant", "tool_calls": [_call()]}, {"role": "tool", "tool_call_id": "wrong", "content": "bad"}],
    [{"role": "assistant", "tool_calls": [_call()]}, {"role": "tool", "tool_call_id": "call_1", "content": "ok"}, {"role": "tool", "tool_call_id": "call_1", "content": "duplicate"}],
    [{"role": "assistant", "tool_calls": [_call(), _call()]}],
])
def test_rejects_orphan_missing_duplicate_or_interrupted_tool_results(slot, body, messages):
    body["messages"] += messages
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


@pytest.mark.parametrize("arguments", ["not-json", "[]", '"string"', "null", '{"x":NaN}', '{"x":Infinity}'])
def test_tool_arguments_are_json_objects(slot, body, arguments):
    body["messages"] += [
        {"role": "assistant", "tool_calls": [_call(arguments=arguments)]},
        {"role": "tool", "tool_call_id": "call_1", "content": "done"},
    ]
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


@pytest.mark.parametrize("changes", [
    {"temperature": True}, {"temperature": float("nan")}, {"temperature": float("inf")},
    {"temperature": -0.1}, {"temperature": 2.1}, {"temperature": 10**1000}, {"top_p": 1.1}, {"top_p": "0.5"},
    {"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": 1.5}, {"max_tokens": 1_000_001},
    {"max_tokens": 100, "max_completion_tokens": 100}, {"max_completion_tokens": None},
    {"n": 2}, {"n": True}, {"stream": 1},
    {"stream_options": {"include_usage": True}},
    {"stream": True, "stream_options": {"include_usage": 1}},
    {"stream": True, "stream_options": {"unknown": True}},
    {"stop": ["a", "b", "c", "d", "e"]}, {"stop": [1]},
    {"parallel_tool_calls": True}, {"tool_choice": "required"},
    {"tool_choice": {"type": "function", "function": {"name": "missing"}}},
    {"tools": [{"type": "custom", "custom": {}}]},
    {"response_format": {"type": "json_schema", "json_schema": {"name": "answer"}}},
    {"response_format": {"type": "text", "json_schema": {}}},
])
def test_rejects_invalid_options(slot, body, changes):
    body.update(changes)
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


@pytest.mark.parametrize("field", ["base_url", "api_key", "headers", "extra_body", "audio", "modalities", "user", "metadata"])
def test_rejects_unexposed_options_without_reflecting_secrets(slot, body, field):
    body[field] = "supplied-secret"
    with pytest.raises(ModelGatewayError) as error:
        prepare_chat_request(slot, body)
    assert "supplied-secret" not in str(error.value)
    assert "upstream-secret" not in str(error.value)


@pytest.mark.parametrize("content", [
    [{"type": "input_audio", "input_audio": {"data": "secret", "format": "wav"}}],
    [{"type": "file", "file": {"file_id": "file_1"}}],
    [{"type": "video_url", "video_url": {"url": "https://example.com/video"}}],
    [{"type": "image_url", "image_url": {"url": "file:///private/image.png"}}],
    [{"type": "image_url", "image_url": {"url": "https://username:secret@example.com/image.png"}}],
    [{"type": "image_url", "image_url": {"url": "data:text/plain;base64,aGk="}}],
    [{"type": "image_url", "image_url": {"url": "data:image/png;base64,?"}}],
    [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}],
    [{"type": "image_url", "image_url": {"url": "https://example.com/image.png", "detail": "secret"}}],
    [{"type": "text", "text": "hello", "image_url": {"url": "https://example.com/image.png"}}],
])
def test_rejects_unsupported_or_invalid_media(slot, body, content):
    body["messages"][0]["content"] = content
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


@pytest.mark.parametrize("messages", [
    [],
    [{"role": "function", "content": "legacy role"}],
    [{"role": "assistant", "content": None}],
    [{"role": "assistant", "content": "text", "audio": {"id": "audio_1"}}],
    [{"role": "user", "content": "hello"}, {"role": "system", "content": "late system"}],
    [{"role": "user", "content": "hello"}, {"role": "developer", "content": "late developer"}],
    [{"role": "system", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}]}],
    [{"role": "user", "content": "hello", "tool_call_id": "call_1"}],
])
def test_rejects_invalid_message_roles_and_shapes(slot, body, messages):
    body["messages"] = messages
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


@pytest.mark.parametrize("changes,capability", [
    ({"stream": True}, "streaming"),
    ({"tools": [_tool()]}, "tool_calling"),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]}, "image_input"),
])
def test_requires_capability_for_actual_request(slot, body, changes, capability):
    slot.capabilities.remove(capability)
    body.update(changes)
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


def test_tool_history_requires_capability_without_new_tools(slot, body):
    slot.capabilities = ["text"]
    body["messages"] += [
        {"role": "assistant", "tool_calls": [_call()]},
        {"role": "tool", "tool_call_id": "call_1", "content": "cloudy"},
    ]
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


def test_empty_tools_need_no_tool_capability(slot, body):
    slot.capabilities = ["text"]
    body["tools"] = []
    assert prepare_chat_request(slot, body)["tools"] == []


@pytest.mark.parametrize("alias", ["actual-model", "gpt-5.2", "Analysis", "analysis/other", "a" * 65, "", "secret\n"])
def test_model_is_a_plugin_usage_identifier(slot, body, alias):
    body["model"] = alias
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


@pytest.mark.parametrize("response_format", [
    {"type": "text"}, {"type": "json_object"},
    {"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object", "properties": {"answer": {"type": "string"}}}, "strict": True}},
])
def test_structured_response_options_preserved_for_adapter_decision(slot, body, response_format):
    body["response_format"] = response_format
    assert prepare_chat_request(slot, body)["response_format"] == response_format


def test_preceding_system_and_developer_text_preserved(slot, body):
    body["messages"][:0] = [
        {"role": "system", "content": "system"},
        {"role": "developer", "content": [{"type": "text", "text": "developer"}]},
    ]
    assert prepare_chat_request(slot, body)["messages"] == body["messages"]


def test_sdk_message_dump_tool_result_continuation(slot, body):
    message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[_call()])
    body["messages"] += [
        message.model_dump(),
        {"role": "tool", "tool_call_id": "call_1", "content": "cloudy"},
    ]
    original = copy.deepcopy(body)
    result = prepare_chat_request(slot, body)
    assert result["messages"][1] == {"role": "assistant", "content": None, "tool_calls": [_call()]}
    assert result["messages"][2] == body["messages"][2]
    assert body == original


def test_sdk_message_dump_text_continuation(slot, body):
    body["messages"].append(ChatCompletionMessage(role="assistant", content="Earlier answer").model_dump())
    body["messages"].append({"role": "user", "content": "Explain further"})
    assert prepare_chat_request(slot, body)["messages"][1] == {"role": "assistant", "content": "Earlier answer"}


def test_sdk_message_dump_refusal_continuation(slot, body):
    body["messages"].append(ChatCompletionMessage(role="assistant", content=None, refusal="I cannot help with that request.").model_dump())
    body["messages"].append({"role": "user", "content": "Here is a different request."})
    assert prepare_chat_request(slot, body)["messages"][1] == {
        "role": "assistant", "content": None, "refusal": "I cannot help with that request.",
    }


def test_empty_standard_lists_are_removed(slot, body):
    body["messages"].append({"role": "assistant", "content": "answer", "annotations": [], "tool_calls": []})
    assert prepare_chat_request(slot, body)["messages"][1] == {"role": "assistant", "content": "answer"}


@pytest.mark.parametrize("extra", [
    {"audio": {"id": "audio_1"}}, {"audio": {}},
    {"function_call": {"name": "weather", "arguments": "{}"}},
    {"annotations": [{"type": "url_citation"}]}, {"annotations": {}},
    {"refusal": 1}, {"tool_calls": ""}, {"tool_calls": False},
    {"reasoning_content": None}, {"extra_content": None}, {"arbitrary": None},
])
def test_assistant_optional_fields_do_not_open_extra_features(slot, body, extra):
    body["messages"].append({"role": "assistant", "content": "answer", **extra})
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, body)


@pytest.mark.parametrize("invalid", [None, [], "secret", {}, {"model": "analysis"}])
def test_invalid_request_body_has_a_stable_safe_error(slot, invalid):
    with pytest.raises(ModelGatewayError):
        prepare_chat_request(slot, invalid)
