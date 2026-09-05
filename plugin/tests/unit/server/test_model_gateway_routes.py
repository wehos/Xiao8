from __future__ import annotations

import asyncio
import json
import threading
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from openai import APIError, APIStatusError

from plugin.config.schema import PluginModelRequirementSchema
from plugin.core.model_gateway_access import ModelGatewayAccessRegistry
from plugin.sdk.shared.core.context import SdkContext
from plugin.server.application.model_gateway_service import ModelGatewayService
from plugin.server.domain.model_config import ModelSlot, PluginModelsConfig
from plugin.server.routes import model_gateway as routes

PREFIX = "/api/models/v1"
SLOT_ID = "slot_" + "a" * 32
SECRET = "only-the-host-knows-this-provider-key"


def reply():
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "real-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def chunk(content="", finish=None):
    return {
        "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1, "model": "real-model",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
    }


def event(data):
    return ("data: " + json.dumps(data) + "\n\n").encode()


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield self.data

    async def aclose(self):
        self.closed.set()


@pytest.fixture
def setup_gateway(monkeypatch):
    registry = ModelGatewayAccessRegistry()
    alive = {"alpha": True, "beta": True}
    tokens = {name: registry.issue(name, lambda name=name: alive[name]) for name in alive}
    config = PluginModelsConfig(slots={SLOT_ID: ModelSlot(
        name="Private model", protocol="openai_chat", base_url="https://upstream.test/v1", model="real-model",
        api_key=SECRET, capabilities=["text", "image_input", "streaming"],
    )}, bindings={"alpha": {"analysis": SLOT_ID}, "beta": {"beta_only": SLOT_ID}})
    loop_thread = threading.get_ident()
    calls = []
    clients = []

    def requirements(plugin_id):
        assert threading.get_ident() != loop_thread
        return {"beta_only" if plugin_id == "beta" else "analysis": PluginModelRequirementSchema(label="Analysis")}

    def read_config():
        assert threading.get_ident() != loop_thread
        return config.model_copy(deep=True)

    async def upstream(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + SECRET
        assert all(token not in str(request.headers) for token in tokens.values())
        payload = json.loads(request.content)
        assert payload["model"] == "real-model"
        if payload.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=BytesStream(event(chunk("hello")) + event(chunk(finish="stop")) + b"data: [DONE]\n\n"))
        return httpx.Response(200, json=reply())

    state = SimpleNamespace(handler=upstream)

    async def dispatch(request):
        return await state.handler(request)

    def client_factory(_slot):
        client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch), trust_env=False)
        clients.append(client)
        return client

    monkeypatch.setattr(routes, "model_gateway_access", registry)
    monkeypatch.setattr(routes, "config_store", SimpleNamespace(read=read_config))
    monkeypatch.setattr(routes, "load_model_requirements", requirements)
    monkeypatch.setattr(routes, "gateway_service", ModelGatewayService(client_factory))
    app = FastAPI()
    app.include_router(routes.router)
    yield SimpleNamespace(app=app, registry=registry, tokens=tokens, alive=alive,
                          config=config, calls=calls, clients=clients, upstream=state)
    registry.revoke_all()


@pytest.fixture
async def http_client(setup_gateway):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup_gateway.app), base_url="http://gateway.test") as client:
        yield client


def body(**updates):
    return {"model": "analysis", "messages": [{"role": "user", "content": "hello"}], **updates}


def auth(state, plugin="alpha"):
    return {"Authorization": "Bearer " + state.tokens[plugin]}


@pytest.mark.parametrize("authorization", [None, "", "Basic secret", "Bearer unknown", "Bearer " + "x" * 300])
async def test_access_denied_before_parsing_body(http_client, setup_gateway, authorization):
    response = await http_client.post(PREFIX + "/chat/completions", content=b"invalid secret body",
                                      headers={} if authorization is None else {"Authorization": authorization})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "plugin_model_access_denied"
    assert "secret" not in response.text
    assert not setup_gateway.calls


@pytest.mark.parametrize("payload", [body(model=SLOT_ID), body(model="beta_only"), body(plugin_id="beta")])
async def test_plugin_cannot_select_another_identity_or_arbitrary_slot(http_client, setup_gateway, payload):
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=payload)
    assert response.status_code in (400, 403)
    assert not setup_gateway.calls


async def test_unbound_usage_and_updated_requirement_fail_without_upstream(http_client, setup_gateway, monkeypatch):
    setup_gateway.config.bindings["alpha"].clear()
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_usage_not_bound"
    setup_gateway.config.bindings["alpha"]["analysis"] = SLOT_ID
    monkeypatch.setattr(routes, "load_model_requirements", lambda plugin_id: {
        "analysis": PluginModelRequirementSchema(label="Analysis", capabilities=["tool_calling"]),
    })
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
    assert response.status_code == 409
    assert not setup_gateway.calls


async def test_restarted_and_dead_instances_lose_access(http_client, setup_gateway):
    old_token = setup_gateway.tokens["alpha"]
    new_token = setup_gateway.registry.issue("alpha", lambda: setup_gateway.alive["alpha"])
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
    assert response.status_code == 401
    setup_gateway.registry.revoke(old_token)
    setup_gateway.tokens["alpha"] = new_token
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
    assert response.status_code == 200
    setup_gateway.alive["alpha"] = False
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
    assert response.status_code == 401
    assert len(setup_gateway.calls) == 1


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_public_sdk_client_calls_authenticated_gateway(setup_gateway, monkeypatch, protocol):
    from plugin.sdk.shared.core import models as sdk_models

    if protocol == "anthropic_messages":
        setup_gateway.config.slots[SLOT_ID].protocol = protocol

        async def anthropic(request):
            setup_gateway.calls.append(request)
            assert request.headers["x-api-key"] == SECRET
            assert "authorization" not in request.headers
            payload = json.loads(request.content)
            assert payload["model"] == "real-model"
            result = {"id": "msg-test", "type": "message", "role": "assistant", "model": "real-model",
                      "content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn",
                      "usage": {"input_tokens": 3, "output_tokens": 1}}
            if not payload.get("stream"):
                return httpx.Response(200, json=result)
            events = [
                {"type": "message_start", "message": {**result, "content": [], "stop_reason": None}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=BytesStream(b"".join(event(item) for item in events)))

        setup_gateway.upstream.handler = anthropic

    original = sdk_models._GatewayHttpClient

    class TestHttpClient(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.ASGITransport(app=setup_gateway.app), **kwargs)

    monkeypatch.setattr(sdk_models, "_GatewayHttpClient", TestHttpClient)
    ctx = SdkContext(SimpleNamespace(
        _model_gateway_base_url="http://gateway.test" + PREFIX,
        _model_gateway_token=setup_gateway.tokens["alpha"],
    ))
    client = await ctx.models.get_client()
    try:
        result = await client.chat.completions.create(**body())
        assert result.model == "analysis"
        assert result.choices[0].message.content == "hello"
        stream = await client.chat.completions.create(**body(stream=True))
        pieces = [item async for item in stream]
        assert "".join(item.choices[0].delta.content or "" for item in pieces) == "hello"
        assert pieces[-1].choices[0].finish_reason == "stop"
        with pytest.raises(APIStatusError) as error:
            await client.chat.completions.create(**body(model="beta_only"))
        assert error.value.status_code == 403

        async def broken(request):
            data = event(chunk("partial")) if protocol == "openai_chat" else event({
                "type": "message_start", "message": {"id": "msg-test", "role": "assistant", "type": "message", "content": []},
            })
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=BytesStream(data + b"data: not-json-secret\n\n"))

        setup_gateway.upstream.handler = broken
        broken_stream = await client.chat.completions.create(**body(stream=True))
        with pytest.raises(APIError) as error:
            _ = [item async for item in broken_stream]
        assert "not-json-secret" not in str(error.value)
    finally:
        await ctx.models.aclose()
    assert all(item.is_closed for item in setup_gateway.clients)


@pytest.mark.parametrize("payload", [[{"api_key": SECRET}], "secret", {}, body(model=12)])
async def test_invalid_json_shape_does_not_echo_input(http_client, setup_gateway, payload):
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=payload)
    assert response.status_code == 400
    assert SECRET not in response.text
    assert not setup_gateway.calls


async def test_body_limit_and_json_error(http_client, setup_gateway, monkeypatch):
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), content='{"key":"' + SECRET)
    assert response.status_code == 400 and SECRET not in response.text
    monkeypatch.setattr(routes, "MAX_REQUEST_BYTES", 32)
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), content="x" * 100)
    assert response.status_code == 413
    assert not setup_gateway.calls


async def test_stream_error_before_headers_is_http_error(http_client, setup_gateway):
    async def rejected(request):
        return httpx.Response(401, text=SECRET)

    setup_gateway.upstream.handler = rejected
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body(stream=True))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_authentication_failed"
    assert SECRET not in response.text
    assert all(item.is_closed for item in setup_gateway.clients)


async def test_stream_error_after_headers_has_no_done(http_client, setup_gateway):
    async def broken(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=BytesStream(event(chunk("partial")) + b"data: not-json-secret\n\n"))

    setup_gateway.upstream.handler = broken
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body(stream=True))
    assert response.status_code == 200
    assert '"error"' in response.text
    assert "[DONE]" not in response.text
    assert "not-json-secret" not in response.text
    assert all(item.is_closed for item in setup_gateway.clients)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stop_kind", ["revoke", "process_death"])
async def test_stopping_plugin_cancels_active_upstream(http_client, setup_gateway, monkeypatch, streaming, stop_kind):
    started = asyncio.Event()
    closed = asyncio.Event()
    monkeypatch.setattr(routes, "_LIVENESS_INTERVAL", 0.01)

    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(chunk("partial"))
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async def hanging(request):
        if streaming:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=HangingStream())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    setup_gateway.upstream.handler = hanging
    task = asyncio.create_task(http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body(stream=streaming)))
    await asyncio.wait_for(started.wait(), timeout=2)
    if stop_kind == "revoke":
        setup_gateway.registry.revoke(setup_gateway.tokens["alpha"])
    else:
        setup_gateway.alive["alpha"] = False
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert closed.is_set()
    assert all(item.is_closed for item in setup_gateway.clients)


async def test_prefetch_handoff_revocation_closes_stream(setup_gateway):
    closed = asyncio.Event()

    async def upstream():
        try:
            yield event(chunk("partial"))
            await asyncio.Event().wait()
        finally:
            closed.set()

    stream = upstream()
    first = await anext(stream)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    response = routes._ModelStreamResponse(first, stream, request, setup_gateway.tokens["alpha"])
    setup_gateway.registry.revoke(setup_gateway.tokens["alpha"])
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    assert sent[0]["status"] == 401
    assert closed.is_set()


async def test_response_send_failure_closes_prefetched_upstream(setup_gateway):
    closed = asyncio.Event()

    async def upstream():
        try:
            yield event(chunk("partial"))
        finally:
            closed.set()

    stream = upstream()
    first = await anext(stream)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    response = routes._ModelStreamResponse(first, stream, request, setup_gateway.tokens["alpha"])

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        raise OSError("client disconnected")

    with pytest.raises((OSError, ExceptionGroup)) as error:
        await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    if isinstance(error.value, ExceptionGroup):
        assert error.value.subgroup(OSError) is not None
    assert closed.is_set()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_asgi_disconnect_cancels_call_and_finishes_async_close(setup_gateway, monkeypatch, streaming, spec_version):
    started = asyncio.Event()
    closed = asyncio.Event()
    monkeypatch.setattr(routes, "_LIVENESS_INTERVAL", 0.01)

    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(chunk("partial"))
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            # Requires protection from StreamingResponse's AnyIO cancel scope.
            await asyncio.sleep(0)
            closed.set()

    async def hanging(request):
        if streaming:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=HangingStream())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    setup_gateway.upstream.handler = hanging
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": json.dumps(body(stream=streaming)).encode(), "more_body": False})
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1", "method": "POST", "scheme": "http", "path": PREFIX + "/chat/completions",
        "query_string": b"", "headers": [(b"authorization", ("Bearer " + setup_gateway.tokens["alpha"]).encode()),
                                           (b"content-type", b"application/json")],
        "server": ("127.0.0.1", 48916), "client": ("127.0.0.1", 43210),
    }
    sent = []

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(setup_gateway.app(scope, incoming.get, send))
    await asyncio.wait_for(started.wait(), timeout=2)
    incoming.put_nowait({"type": "http.disconnect"})
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert closed.is_set()
    assert all(client.is_closed for client in setup_gateway.clients)
    assert b"[DONE]" not in b"".join(message.get("body", b"") for message in sent)


@pytest.mark.parametrize("disconnected", [False, True])
async def test_guard_exit_handles_disconnect_probe_consuming_cancel(setup_gateway, monkeypatch, disconnected):
    probing = asyncio.Event()
    monkeypatch.setattr(routes, "_LIVENESS_INTERVAL", 0.001)

    async def is_disconnected():
        probing.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Mirrors the race with Request.is_disconnected's cancelled scope.
            return disconnected

    async def finish_request():
        request = SimpleNamespace(is_disconnected=is_disconnected)
        async with routes._RequestGuard(request, setup_gateway.tokens["alpha"]) as guard:
            guard.watch_disconnect = True
            await probing.wait()
        return "finished"

    assert await asyncio.wait_for(finish_request(), timeout=2) == "finished"
