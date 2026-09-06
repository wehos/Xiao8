from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from starlette.responses import JSONResponse, StreamingResponse

from plugin.server.application.model_gateway_service import ModelGatewayService
from plugin.server.domain.model_config import ModelSlot
from plugin.server.model_gateway.errors import ModelGatewayError
from plugin.server.model_gateway.observation import AttemptObservation
from plugin.server.model_gateway import transport as transport_module


pytestmark = pytest.mark.plugin_unit

SECRET = "upstream-test-secret"
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def make_slot(protocol="openai_chat", **updates):
    return ModelSlot.model_validate({
        "name": "Test model",
        "model": "actual-upstream-model",
        "protocol": protocol,
        "base_url": "https://provider.test/v1",
        "api_key": SECRET,
        "capabilities": ["text", "image_input", "tool_calling", "streaming"],
        **updates,
    })


def request_body(**updates):
    return {"model": "analysis", "messages": [{"role": "user", "content": "Hello"}], **updates}


def openai_response():
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
        "model": "actual-upstream-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
        "usage": USAGE,
    }


def anthropic_response():
    return {
        "id": "msg-test", "type": "message", "role": "assistant", "model": "actual-upstream-model",
        "content": [{"type": "text", "text": "你好"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }


def openai_events():
    base = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1, "model": "actual-upstream-model"}
    return [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**base, "choices": [], "usage": USAGE},
    ]


def anthropic_events():
    return [
        {"type": "message_start", "message": {"id": "msg-test", "type": "message", "role": "assistant",
         "model": "actual-upstream-model", "content": [], "usage": {"input_tokens": 7, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes, fragment=7):
        self.data = data
        self.fragment = fragment
        self.closed = False

    async def __aiter__(self):
        for index in range(0, len(self.data), self.fragment):
            yield self.data[index:index + self.fragment]

    async def aclose(self):
        self.closed = True


def event_bytes(events, done=False):
    # CRLF and UTF-8 split across HTTP chunks exercise the wire decoder.
    data = b": keepalive\r\n\r\n" + b"".join(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode()
        for event in events
    )
    return data + (b"data: [DONE]\r\n\r\n" if done else b"")


def gateway_for(handler):
    clients = []

    def factory(slot):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=slot.timeout_seconds, trust_env=False)
        clients.append(client)
        return client

    return ModelGatewayService(factory), clients


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_real_openai_sdk_consumes_both_backends(protocol):
    seen_requests = []
    streams = []

    async def upstream(request):
        payload = json.loads(request.content)
        seen_requests.append(payload)
        assert payload["model"] == "actual-upstream-model"
        assert SECRET not in request.content.decode()
        if protocol == "openai_chat":
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer " + SECRET
            assert "x-api-key" not in request.headers
        else:
            assert request.url.path == "/v1/messages"
            assert request.headers["x-api-key"] == SECRET
            assert "Authorization" not in request.headers
        if payload.get("stream"):
            events = openai_events() if protocol == "openai_chat" else anthropic_events()
            stream = ByteStream(event_bytes(events, done=protocol == "openai_chat"))
            streams.append(stream)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        result = openai_response() if protocol == "openai_chat" else anthropic_response()
        return httpx.Response(200, json=result)

    gateway, clients = gateway_for(upstream)
    slot = make_slot(protocol)
    app = FastAPI()

    # Test-only endpoint: production HTTP access is not exposed in commit 2.
    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        if body.get("stream"):
            return StreamingResponse(gateway.stream(slot, body), media_type="text/event-stream")
        return JSONResponse(await gateway.complete(slot, body))

    async with AsyncOpenAI(
        api_key="host-token", base_url="http://gateway.test/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False),
    ) as sdk:
        result = await sdk.chat.completions.create(**request_body())
        assert result.model == "analysis"
        assert result.choices[0].message.content == "你好"
        assert result.usage.total_tokens == 10
        stream = await sdk.chat.completions.create(**request_body(stream=True, stream_options={"include_usage": True}))
        chunks = [chunk async for chunk in stream]
        assert all(chunk.model == "analysis" for chunk in chunks)
        assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices) == "你好"
        assert chunks[-1].usage.total_tokens == 10
    assert len(seen_requests) == 2
    assert all(client.is_closed for client in clients)
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_upstream_usage_requested_but_not_exposed_unless_requested(protocol):
    async def upstream(request):
        if protocol == "openai_chat":
            assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        events = openai_events() if protocol == "openai_chat" else anthropic_events()
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=ByteStream(event_bytes(events, done=protocol == "openai_chat")))

    gateway, _ = gateway_for(upstream)
    parts = [part async for part in gateway.stream(make_slot(protocol), request_body(stream=True))]
    assert parts[-1] == b"data: [DONE]\n\n"
    for part in parts[:-1]:
        chunk = json.loads(part.decode()[6:])
        assert chunk.get("usage") is None
        assert chunk["choices"]


@pytest.mark.parametrize("status,code,expected_status", [
    (401, "upstream_authentication_failed", 502),
    (403, "upstream_authentication_failed", 502),
    (429, "upstream_rate_limited", 429),
    (400, "upstream_request_rejected", 400),
    (500, "upstream_error", 502),
    (504, "upstream_timeout", 504),
    (307, "upstream_redirect_rejected", 502),
])
async def test_upstream_failures_are_safe_and_never_retried(status, code, expected_status):
    requests = []

    async def upstream(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.test/v1"}, content=SECRET)

    gateway, clients = gateway_for(upstream)
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(), request_body())
    assert error.value.code == code
    assert error.value.status_code == expected_status
    assert SECRET not in json.dumps(error.value.to_dict())
    assert len(requests) == 1
    assert clients[0].is_closed


@pytest.mark.parametrize("error_type,code", [
    (httpx.ReadTimeout, "upstream_timeout"), (httpx.ConnectError, "upstream_connection_error"),
])
async def test_transport_errors_are_sanitized(error_type, code):
    async def upstream(request):
        raise error_type(SECRET, request=request)

    gateway, clients = gateway_for(upstream)
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(), request_body())
    assert error.value.code == code
    assert SECRET not in str(error.value)
    assert clients[0].is_closed


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_early_stream_close_releases_upstream_client(protocol):
    events = openai_events() if protocol == "openai_chat" else anthropic_events()
    stream = ByteStream(event_bytes(events, done=protocol == "openai_chat"))
    gateway, clients = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream))
    iterator = gateway.stream(make_slot(protocol), request_body(stream=True))
    assert (await anext(iterator)).startswith(b"data: ")
    await iterator.aclose()
    assert stream.closed
    assert clients[0].is_closed


async def test_stream_cancellation_releases_upstream_client():
    waiting = asyncio.Event()

    class HangingStream(ByteStream):
        async def __aiter__(self):
            yield event_bytes(openai_events()[:1])
            waiting.set()
            await asyncio.Event().wait()

    stream = HangingStream(b"")
    gateway, clients = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream))
    iterator = gateway.stream(make_slot(), request_body(stream=True))
    await anext(iterator)
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert clients[0].is_closed


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_truncated_stream_has_no_success_terminator(protocol):
    events = openai_events() if protocol == "openai_chat" else anthropic_events()[:-1]
    gateway, _ = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ByteStream(event_bytes(events))))
    parts = []
    with pytest.raises(ModelGatewayError):
        async for part in gateway.stream(make_slot(protocol), request_body(stream=True)):
            parts.append(part)
    assert b"data: [DONE]\n\n" not in parts


@pytest.mark.parametrize("data", [b"data: {bad json}\n\n", b"event: error\ndata: secret\n\n"])
async def test_malformed_and_error_events_are_rejected(data):
    gateway, clients = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ByteStream(data)))
    with pytest.raises(ModelGatewayError) as error:
        _ = [part async for part in gateway.stream(make_slot(), request_body(stream=True))]
    assert error.value.status_code == 502
    assert "secret" not in str(error.value)
    assert clients[0].is_closed


async def test_non_sse_response_is_rejected():
    gateway, _ = gateway_for(lambda request: httpx.Response(200, text=SECRET))
    with pytest.raises(ModelGatewayError, match="event stream"):
        _ = [part async for part in gateway.stream(make_slot(), request_body(stream=True))]


@pytest.mark.parametrize("streaming", [False, True])
async def test_response_size_limits(streaming, monkeypatch):
    monkeypatch.setattr(transport_module, "MAX_RESPONSE_BYTES", 32)
    monkeypatch.setattr(transport_module, "MAX_SSE_EVENT_BYTES", 32)
    data = b"data: " + b"x" * 50 + b"\n\n" if streaming else b"x" * 50
    gateway, clients = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ByteStream(data)))
    with pytest.raises(ModelGatewayError) as error:
        if streaming:
            _ = [part async for part in gateway.stream(make_slot(), request_body(stream=True))]
        else:
            await gateway.complete(make_slot(), request_body())
    assert error.value.code == "upstream_response_too_large"
    assert clients[0].is_closed


async def test_invalid_request_never_opens_upstream_client():
    def forbidden(slot):
        pytest.fail("Invalid input must be rejected before any network client is created")

    gateway = ModelGatewayService(forbidden)
    with pytest.raises(ModelGatewayError):
        await gateway.complete(make_slot(), request_body(api_key=SECRET))
    with pytest.raises(ModelGatewayError):
        await gateway.complete(make_slot("anthropic_messages"), request_body(response_format={"type": "json_object"}))


@pytest.mark.parametrize("base,path", [
    ("https://provider.test", "/v1/messages"),
    ("https://provider.test/v1", "/v1/messages"),
    ("https://provider.test/coding", "/coding/v1/messages"),
    ("https://provider.test/proxy/v1", "/proxy/v1/messages"),
])
async def test_anthropic_base_url_and_empty_credential(base, path):
    async def upstream(request):
        assert request.url.path == path
        assert "x-api-key" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, json=anthropic_response())

    gateway, _ = gateway_for(upstream)
    await gateway.complete(make_slot("anthropic_messages", base_url=base, api_key=""), request_body())


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_sdk_tool_message_roundtrips_into_next_turn(protocol):
    requests = []

    async def upstream(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            if protocol == "openai_chat":
                result = openai_response()
                result["choices"][0].update(finish_reason="tool_calls", message={
                    "role": "assistant", "content": None, "refusal": None,
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"key":"value"}'}}],
                })
            else:
                result = anthropic_response()
                result.update(stop_reason="tool_use", content=[
                    {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"key": "value"}},
                ])
            return httpx.Response(200, json=result)
        if protocol == "openai_chat":
            assert payload["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "found"}
        else:
            assert payload["messages"][-2]["content"][0]["type"] == "tool_use"
            assert payload["messages"][-1]["content"][0] == {
                "type": "tool_result", "tool_use_id": "call_1", "content": [{"type": "text", "text": "found"}],
            }
        return httpx.Response(200, json=openai_response() if protocol == "openai_chat" else anthropic_response())

    gateway, _ = gateway_for(upstream)
    body = request_body(tools=[{"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object", "properties": {"key": {"type": "string"}}},
    }}])
    first = ChatCompletion.model_validate(await gateway.complete(make_slot(protocol), body))
    body["messages"].extend([
        first.choices[0].message.model_dump(),
        {"role": "tool", "tool_call_id": "call_1", "content": "found"},
    ])
    final = await gateway.complete(make_slot(protocol), body)
    assert final["choices"][0]["message"]["content"] == "你好"
    assert len(requests) == 2


async def test_nonterminated_sse_line_is_bounded_before_reading_entire_stream(monkeypatch):
    monkeypatch.setattr(transport_module, "MAX_SSE_EVENT_BYTES", 1024)

    class UnbrokenLine(ByteStream):
        reads = 0

        async def __aiter__(self):
            for _ in range(40):
                self.reads += 1
                yield b"x" * 512

    stream = UnbrokenLine(b"")
    gateway, clients = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream))
    with pytest.raises(ModelGatewayError) as error:
        _ = [part async for part in gateway.stream(make_slot(), request_body(stream=True))]
    assert error.value.code == "upstream_response_too_large"
    assert stream.reads == 3
    assert stream.closed and clients[0].is_closed


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_upstream_invalid_unicode_returns_safe_error(streaming, protocol):
    if streaming:
        events = openai_events() if protocol == "openai_chat" else anthropic_events()
        if protocol == "openai_chat":
            events[1]["choices"][0]["delta"]["content"] = "\ud800"
        else:
            events[2]["delta"]["text"] = "\ud800"
        data = b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)
    else:
        result = openai_response() if protocol == "openai_chat" else anthropic_response()
        if protocol == "openai_chat":
            result["choices"][0]["message"]["content"] = "\ud800"
        else:
            result["content"][0]["text"] = "\ud800"
        data = json.dumps(result).encode()
    gateway, clients = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ByteStream(data)))
    with pytest.raises(ModelGatewayError) as error:
        if streaming:
            _ = [part async for part in gateway.stream(make_slot(protocol), request_body(stream=True))]
        else:
            await gateway.complete(make_slot(protocol), request_body())
    assert error.value.code == "invalid_upstream_response"
    assert clients[0].is_closed


@pytest.mark.parametrize("updates", [{"base_url": "https://☃.example"}, {"api_key": "无效凭证"}])
async def test_invalid_http_configuration_is_reported_before_client_creation(updates):
    def forbidden(slot):
        pytest.fail("Invalid configuration must not create a network client")

    with pytest.raises(ModelGatewayError) as error:
        await ModelGatewayService(forbidden).complete(make_slot(**updates), request_body())
    assert error.value.code == "invalid_model_configuration"


async def test_request_encoding_and_size_errors_are_safe(monkeypatch):
    from plugin.server.application import model_gateway_service as service_module

    def forbidden(slot):
        pytest.fail("Invalid request must not create a network client")

    gateway = ModelGatewayService(forbidden)
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(), request_body(messages=[{"role": "user", "content": "\ud800"}]))
    assert error.value.code == "invalid_request"
    monkeypatch.setattr(service_module, "MAX_REQUEST_BYTES", 32)
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(), request_body())
    assert error.value.code == "request_too_large"


@pytest.mark.parametrize("separator", [b"\n", b"\r", b"\r\n"])
async def test_sse_bom_multiline_data_and_split_line_endings(separator):
    event = openai_events()[0]
    encoded = json.dumps(event)
    split = encoded.index(', "object"')
    data = (b"\xef\xbb\xbfdata: " + encoded[:split + 1].encode() + separator
            + b"data: " + encoded[split + 1:].encode() + separator + separator)
    stream = ByteStream(data, fragment=1)
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    try:
        decoded = [json.loads(item) async for item in transport_module.iter_sse_data(response)]
    finally:
        await response.aclose()
    assert decoded == [event]


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_observation_collects_usage_independently_of_public_result(protocol, streaming):
    observation = AttemptObservation()

    def upstream(request):
        assert observation.upstream_started
        if streaming:
            events = openai_events() if protocol == "openai_chat" else anthropic_events()
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ByteStream(event_bytes(events, done=protocol == "openai_chat")))
        result = openai_response() if protocol == "openai_chat" else anthropic_response()
        return httpx.Response(200, json=result)

    gateway, clients = gateway_for(upstream)
    if streaming:
        parts = [part async for part in gateway.stream(
            make_slot(protocol), request_body(stream=True, stream_options={"include_usage": False}), observation=observation)]
        assert all("usage" not in json.loads(part.decode()[6:]) for part in parts[:-1])
    else:
        await gateway.complete(make_slot(protocol), request_body(), observation=observation)
    assert {key: observation.usage[key] for key in USAGE} == USAGE
    assert observation.usage_status == "reported"
    assert clients[0].is_closed


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_complete_observes_cache_usage_before_response_validation(protocol):
    observation = AttemptObservation()
    if protocol == "openai_chat":
        usage = {**USAGE, "prompt_tokens_details": {"cached_tokens": 4, "audio_tokens": 0, "secret": SECRET},
                 "completion_tokens_details": {"reasoning_tokens": 2}, "private": SECRET}
        expected = {**USAGE, "prompt_tokens_details": {"cached_tokens": 4, "audio_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 2}}
    else:
        usage = {"input_tokens": 1, "output_tokens": 3, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 2,
                 "private": SECRET}
        expected = {**USAGE, "prompt_tokens_details": {"cached_tokens": 4}}
    gateway, clients = gateway_for(lambda request: httpx.Response(200, json={"usage": usage, "error": SECRET}))
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(protocol), request_body(), observation=observation)
    assert error.value.code == "invalid_upstream_response"
    assert observation.usage == expected
    assert observation.usage_status == "reported"
    assert SECRET not in json.dumps(observation.usage)
    assert SECRET not in str(error.value)
    assert clients[0].is_closed


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_http_failure_retains_reported_usage_without_exposing_error_body(protocol, streaming):
    observation = AttemptObservation()
    usage = USAGE if protocol == "openai_chat" else {"input_tokens": 7, "output_tokens": 3}
    gateway, clients = gateway_for(lambda request: httpx.Response(429, json={"usage": usage, "error": SECRET}))
    with pytest.raises(ModelGatewayError) as error:
        if streaming:
            _ = [part async for part in gateway.stream(make_slot(protocol), request_body(stream=True), observation=observation)]
        else:
            await gateway.complete(make_slot(protocol), request_body(), observation=observation)
    assert error.value.code == "upstream_rate_limited"
    assert error.value.status_code == 429
    assert observation.usage_status == "reported"
    assert {key: observation.usage[key] for key in USAGE} == USAGE
    assert SECRET not in json.dumps(error.value.to_dict())
    assert clients[0].is_closed


@pytest.mark.parametrize("content", [SECRET.encode(), b'{"usage": {"prompt_tokens": -1}}', b"[1, 2, 3]"])
async def test_error_body_parse_failure_preserves_http_status(content):
    observation = AttemptObservation()
    gateway, clients = gateway_for(lambda request: httpx.Response(401, content=content))
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(), request_body(), observation=observation)
    assert error.value.code == "upstream_authentication_failed"
    assert observation.usage is None and observation.usage_status == "unknown"
    assert observation.upstream_started
    assert clients[0].is_closed


async def test_error_body_usage_read_is_bounded(monkeypatch):
    from plugin.server.application import model_gateway_service as service_module

    monkeypatch.setattr(service_module, "MAX_ERROR_USAGE_BYTES", 32)
    stream = ByteStream(json.dumps({"secret": SECRET, "usage": USAGE}).encode())
    observation = AttemptObservation()
    gateway, clients = gateway_for(lambda request: httpx.Response(500, stream=stream))
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(), request_body(), observation=observation)
    assert error.value.code == "upstream_error"
    assert observation.usage is None
    assert stream.closed and clients[0].is_closed


async def test_error_body_read_failure_preserves_original_http_error():
    class FailedBody(ByteStream):
        async def __aiter__(self):
            yield b'{"usage":'
            raise httpx.ReadTimeout(SECRET)

    stream = FailedBody(b"")
    observation = AttemptObservation()
    gateway, clients = gateway_for(lambda request: httpx.Response(429, stream=stream))
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(), request_body(), observation=observation)
    assert error.value.code == "upstream_rate_limited"
    assert observation.usage is None
    assert stream.closed and clients[0].is_closed


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_interrupted_stream_retains_latest_cumulative_usage(protocol):
    events = openai_events() if protocol == "openai_chat" else anthropic_events()[:-1]
    if protocol == "openai_chat":
        events[0]["usage"] = {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7}
    observation = AttemptObservation()
    stream = ByteStream(event_bytes(events))
    gateway, clients = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream))
    with pytest.raises(ModelGatewayError):
        _ = [part async for part in gateway.stream(make_slot(protocol), request_body(stream=True), observation=observation)]
    assert {key: observation.usage[key] for key in USAGE} == USAGE
    assert observation.usage_status == "partial"
    assert stream.closed and clients[0].is_closed


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
@pytest.mark.parametrize("invalid", [True, -1, "3", None])
async def test_malformed_stream_usage_preserves_last_valid_snapshot(protocol, invalid):
    if protocol == "openai_chat":
        events = openai_events()
        events[0]["usage"] = {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7}
        events[-1]["usage"] = {**USAGE, "completion_tokens": invalid}
    else:
        events = anthropic_events()
        events[-2]["usage"]["output_tokens"] = invalid
    observation = AttemptObservation()
    gateway, _ = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ByteStream(event_bytes(events, done=protocol == "openai_chat"))))
    with pytest.raises(ModelGatewayError):
        _ = [part async for part in gateway.stream(make_slot(protocol), request_body(stream=True), observation=observation)]
    assert observation.usage["prompt_tokens"] == 7
    assert observation.usage["completion_tokens"] == 0
    assert observation.usage["total_tokens"] == 7
    assert observation.usage_status == "partial"


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_stream_close_and_cancellation_preserve_partial_usage(protocol, cancel):
    waiting = asyncio.Event()
    events = openai_events() if protocol == "openai_chat" else anthropic_events()
    if protocol == "openai_chat":
        events[0]["usage"] = {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7}

    class HangingStream(ByteStream):
        async def __aiter__(self):
            yield event_bytes(events[:1])
            waiting.set()
            await asyncio.Event().wait()

    stream = HangingStream(b"")
    observation = AttemptObservation()
    gateway, clients = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream))
    iterator = gateway.stream(make_slot(protocol), request_body(stream=True), observation=observation)
    await anext(iterator)
    assert observation.usage_status == "partial"
    if cancel:
        task = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await iterator.aclose()
    assert observation.usage["prompt_tokens"] == 7
    assert observation.usage_status == "partial"
    assert stream.closed and clients[0].is_closed


async def test_anthropic_message_stop_is_reported_even_if_consumer_closes_on_final_chunk():
    observation = AttemptObservation()
    stream = ByteStream(event_bytes(anthropic_events()))
    gateway, _ = gateway_for(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream))
    iterator = gateway.stream(make_slot("anthropic_messages"), request_body(stream=True), observation=observation)
    async for part in iterator:
        chunk = json.loads(part.decode()[6:])
        if chunk["choices"][0]["finish_reason"] == "stop":
            break
    await iterator.aclose()
    assert observation.usage_status == "reported"
    assert observation.usage["total_tokens"] == 10
    assert stream.closed


async def test_preparation_error_never_marks_attempt_started():
    observation = AttemptObservation()
    gateway, clients = gateway_for(lambda request: pytest.fail("must not send invalid request"))
    with pytest.raises(ModelGatewayError):
        await gateway.complete(make_slot(), request_body(api_key=SECRET), observation=observation)
    assert not observation.upstream_started
    assert observation.usage is None and observation.usage_status == "unknown"
    assert clients == []


async def test_connection_failure_marks_started_without_inventing_usage():
    def upstream(request):
        raise httpx.ConnectError(SECRET)

    observation = AttemptObservation()
    gateway, _ = gateway_for(upstream)
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete(make_slot(), request_body(), observation=observation)
    assert error.value.code == "upstream_connection_error"
    assert observation.upstream_started
    assert observation.usage is None and observation.usage_status == "unknown"


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_success_without_usage_remains_unknown(protocol, streaming):
    if streaming:
        if protocol == "openai_chat":
            events = openai_events()[:-1]
        else:
            events = anthropic_events()
            events[0]["message"].pop("usage")
            events[-2].pop("usage")
        response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ByteStream(event_bytes(events, done=protocol == "openai_chat")))
    else:
        result = openai_response() if protocol == "openai_chat" else anthropic_response()
        result.pop("usage")
        response = httpx.Response(200, json=result)
    observation = AttemptObservation()
    gateway, _ = gateway_for(lambda request: response)
    if streaming:
        _ = [part async for part in gateway.stream(make_slot(protocol), request_body(stream=True), observation=observation)]
    else:
        await gateway.complete(make_slot(protocol), request_body(), observation=observation)
    assert observation.upstream_started
    assert observation.usage is None and observation.usage_status == "unknown"


def test_observation_detaches_counters_and_rejects_malformed_replacements():
    observation = AttemptObservation()
    usage = {**USAGE, "prompt_tokens_details": {"cached_tokens": 4}}
    observation.observe(usage)
    usage["prompt_tokens_details"]["cached_tokens"] = 99
    assert observation.usage["prompt_tokens_details"]["cached_tokens"] == 4
    for replacement in (
        None, [], {}, {**USAGE, "prompt_tokens": True}, {**USAGE, "total_tokens": -1},
        {**USAGE, "completion_tokens_details": {"reasoning_tokens": SECRET}},
        {**USAGE, "prompt_tokens_details": []},
    ):
        observation.observe(replacement, reported=True)
        assert observation.usage == {**USAGE, "prompt_tokens_details": {"cached_tokens": 4}}
        assert observation.usage_status == "partial"
