from __future__ import annotations

import asyncio
import copy
import json
import threading
from contextlib import nullcontext, suppress
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from openai import APIError, APIStatusError
from starlette.requests import ClientDisconnect

from plugin.config.schema import PluginModelRequirementSchema
from plugin.core.model_gateway_access import ModelGatewayAccessRegistry
from plugin.sdk.shared.core.context import SdkContext
from plugin.server.application.model_gateway_service import ModelGatewayService
from plugin.server.domain.model_config import ModelSlot, PluginModelsConfig
from plugin.server.model_gateway.execution import ModelExecutor
from plugin.server.routes import model_gateway as routes
from plugin.server.routes import model_usage as usage_routes
from plugin.server.infrastructure.model_usage_store import ModelUsageRecorder, USAGE_FILENAME
from utils.file_utils import atomic_write_json

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
    records = []

    class Recorder:
        async def record_request(self, record):
            records.append(copy.deepcopy(record))

        async def aclose(self):
            pass

    app.state.model_executor = ModelExecutor(routes.gateway_service, Recorder())
    yield SimpleNamespace(app=app, registry=registry, tokens=tokens, alive=alive,
                          config=config, calls=calls, clients=clients, upstream=state, records=records)
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


@pytest.fixture
async def policy_usage(setup_gateway, tmp_path, monkeypatch):
    from utils import cloudsave_runtime

    class LocalConfig:
        def get_runtime_config_path(self, name):
            assert name == USAGE_FILENAME
            return tmp_path / name

        def save_json_config(self, name, data):
            atomic_write_json(self.get_runtime_config_path(name), data)

    tracker_calls = []
    tracker = SimpleNamespace(record=lambda **kwargs: tracker_calls.append(kwargs),
                              _save_task=SimpleNamespace(done=lambda: False))
    monkeypatch.setattr(cloudsave_runtime, "cloudsave_writable_transaction", lambda *args, **kwargs: nullcontext())
    recorder = ModelUsageRecorder(LocalConfig(), tracker_getter=lambda: tracker)
    executor = ModelExecutor(routes.gateway_service, recorder, max_active=1, max_waiting=1)
    setup_gateway.app.state.model_executor = executor
    setup_gateway.app.include_router(usage_routes.router)
    monkeypatch.setattr(usage_routes, "recorder", recorder)
    yield SimpleNamespace(recorder=recorder, executor=executor, tracker_calls=tracker_calls,
                          path=tmp_path / USAGE_FILENAME)
    await executor.aclose()
    await recorder.aclose()


async def test_successful_http_call_is_persisted_and_counted_once(http_client, setup_gateway, policy_usage):
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
    assert response.status_code == 200
    stats = (await http_client.get("/api/model-config/usage")).json()
    assert stats["summary"]["logical_request_count"] == 1
    assert stats["summary"]["upstream_attempt_count"] == 1
    assert stats["summary"]["tokens"]["total_tokens"] == 4
    record = stats["requests"][0]
    assert record["plugin_id"] == "alpha" and record["usage_id"] == "analysis"
    assert record["attempts"][0]["usage_status"] == "reported"
    await policy_usage.recorder.record_request(record)
    assert len(policy_usage.tracker_calls) == 1
    assert policy_usage.tracker_calls[0]["call_type"] == "plugin_model"
    raw = policy_usage.path.read_text(encoding="utf-8")
    assert SECRET not in raw and setup_gateway.tokens["alpha"] not in raw
    assert "messages" not in raw and "https://" not in raw


def add_fallback(setup_gateway):
    fallback_id = "slot_" + "b" * 32
    setup_gateway.config.slots[fallback_id] = ModelSlot(
        name="Fallback", protocol="openai_chat", base_url="https://fallback.test/v1", model="fallback-model",
        api_key="fallback-private-key", capabilities=["text", "image_input", "streaming"], timeout_seconds=5,
    )
    setup_gateway.config.slots[SLOT_ID].fallback_slot_id = fallback_id
    return fallback_id


async def test_http_fallback_uses_one_snapshot_and_records_both_attempts(http_client, setup_gateway, policy_usage):
    fallback_id = add_fallback(setup_gateway)
    seen = []

    async def upstream(request):
        seen.append(request)
        if request.url.host == "upstream.test":
            # Editing saved settings during this call affects only new calls.
            setup_gateway.config.slots[fallback_id].api_key = "updated-private-key"
            return httpx.Response(500, json={"error": {"message": SECRET}, "usage": {
                "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3,
            }})
        assert request.headers["authorization"] == "Bearer fallback-private-key"
        assert json.loads(request.content)["model"] == "fallback-model"
        return httpx.Response(200, json=reply())

    setup_gateway.upstream.handler = upstream
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
    assert response.status_code == 200 and response.json()["model"] == "analysis"
    assert len(seen) == 2
    stats = (await http_client.get("/api/model-config/usage")).json()
    assert stats["summary"]["logical_request_count"] == 1
    assert stats["summary"]["upstream_attempt_count"] == 2
    assert stats["summary"]["tokens"]["total_tokens"] == 7
    assert [item["status"] for item in stats["requests"][0]["attempts"]] == ["error", "success"]
    assert len(policy_usage.tracker_calls) == 2
    fallback_stats = (await http_client.get("/api/model-config/usage", params={"slot_id": fallback_id})).json()
    assert fallback_stats["summary"]["upstream_attempt_count"] == 1
    assert fallback_stats["summary"]["tokens"]["total_tokens"] == 4


@pytest.mark.parametrize("upstream_status", [400, 401, 307])
async def test_nonretryable_http_failures_do_not_use_fallback(http_client, setup_gateway, policy_usage, upstream_status):
    add_fallback(setup_gateway)
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(upstream_status, text=SECRET)

    setup_gateway.upstream.handler = upstream
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
    assert response.status_code in (400, 502)
    assert len(seen) == 1
    record = (await policy_usage.recorder.get_usage())["requests"][0]
    assert len(record["attempts"]) == 1
    assert record["attempts"][0]["usage"] is None
    assert not policy_usage.tracker_calls


async def test_busy_and_queue_timeout_do_not_start_extra_upstreams(http_client, setup_gateway, policy_usage):
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def upstream(request):
        seen.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json=reply())

    setup_gateway.upstream.handler = upstream
    first = asyncio.create_task(http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body()))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        setup_gateway.config.slots[SLOT_ID].timeout_seconds = 0.1
        second = asyncio.create_task(http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body()))
        async with asyncio.timeout(2):
            while policy_usage.executor._admitted != 2:
                await asyncio.sleep(0)
        busy = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body())
        assert busy.status_code == 429
        assert busy.json()["error"]["code"] == "model_gateway_busy"
        queued = await asyncio.wait_for(second, 2)
        assert queued.status_code == 504
        assert queued.json()["error"]["code"] == "gateway_timeout"
        assert len(seen) == 1
    finally:
        release.set()
        await asyncio.gather(first, *( [second] if second is not None else []), return_exceptions=True)
    stats = await policy_usage.recorder.get_usage()
    assert stats["summary"]["logical_request_count"] == 3
    assert stats["summary"]["upstream_attempt_count"] == 1
    assert next(row for row in stats["requests"] if row["status"] == "timeout")["attempts"] == []


async def test_http_fallback_keeps_primary_total_deadline(http_client, setup_gateway, policy_usage):
    add_fallback(setup_gateway)
    setup_gateway.config.slots[SLOT_ID].timeout_seconds = 0.05
    closed = asyncio.Event()

    async def upstream(request):
        if request.url.host == "upstream.test":
            return httpx.Response(500, json={"error": {"message": "retryable"}})
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    setup_gateway.upstream.handler = upstream
    response = await asyncio.wait_for(http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body()), 2)
    assert response.status_code == 504 and closed.is_set()
    record = (await policy_usage.recorder.get_usage())["requests"][0]
    assert record["status"] == "timeout"
    assert [attempt["status"] for attempt in record["attempts"]] == ["error", "timeout"]


async def test_stream_deadline_preserves_partial_usage_and_sends_error(http_client, setup_gateway, policy_usage):
    setup_gateway.config.slots[SLOT_ID].protocol = "anthropic_messages"
    setup_gateway.config.slots[SLOT_ID].timeout_seconds = 0.05
    closed = asyncio.Event()

    class NativeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event({"type": "message_start", "message": {
                "id": "msg-partial", "role": "assistant", "content": [], "usage": {"input_tokens": 3, "output_tokens": 0},
            }})
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async def upstream(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=NativeStream())

    setup_gateway.upstream.handler = upstream
    response = await asyncio.wait_for(http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body(stream=True)), 2)
    assert response.status_code == 200
    assert '"code":"gateway_timeout"' in response.text and "[DONE]" not in response.text
    assert closed.is_set()
    stats = await policy_usage.recorder.get_usage()
    assert stats["requests"][0]["status"] == "timeout"
    assert stats["summary"]["usage_counts"]["partial"] == 1
    assert stats["summary"]["tokens"]["prompt_tokens"] == 3
    assert not policy_usage.tracker_calls


async def test_usage_hidden_from_stream_is_still_recorded(http_client, setup_gateway, policy_usage):
    async def upstream(request):
        assert json.loads(request.content)["stream_options"]["include_usage"] is True
        usage = {**chunk(), "choices": [], "usage": reply()["usage"]}
        data = event(chunk("hello")) + event(chunk(finish="stop")) + event(usage) + b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BytesStream(data))

    setup_gateway.upstream.handler = upstream
    response = await http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body(stream=True))
    assert response.status_code == 200 and "[DONE]" in response.text
    assert '"usage"' not in response.text
    record = (await policy_usage.recorder.get_usage())["requests"][0]
    assert record["status"] == "success" and record["attempts"][0]["usage_status"] == "reported"
    assert len(policy_usage.tracker_calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_stalled_http_send_uses_the_same_deadline(setup_gateway, monkeypatch, streaming, spec_version):
    setup_gateway.config.slots[SLOT_ID].timeout_seconds = 0.1
    monkeypatch.setattr(routes, "_ERROR_FLUSH_SECONDS", 5.0)
    sending = asyncio.Event()

    async def upstream(request):
        if not streaming:
            return httpx.Response(200, json=reply())
        data = b"".join(event(chunk("chunk")) for _ in range(20))
        data += event(chunk(finish="stop")) + b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BytesStream(data))

    setup_gateway.upstream.handler = upstream
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": json.dumps(body(stream=streaming)).encode(), "more_body": False})
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1", "method": "POST", "scheme": "http", "path": PREFIX + "/chat/completions",
        "query_string": b"", "headers": [(b"authorization", ("Bearer " + setup_gateway.tokens["alpha"]).encode()),
                                           (b"content-type", b"application/json")],
        "server": ("127.0.0.1", 48916), "client": ("127.0.0.1", 43210),
    }

    async def send(message):
        if message["type"] == "http.response.body":
            sending.set()
            await asyncio.Event().wait()  # Connected peer that never reads.

    task = asyncio.create_task(setup_gateway.app(scope, incoming.get, send))
    try:
        await asyncio.wait_for(sending.wait(), 2)
        done, pending = await asyncio.wait({task}, timeout=1)
        assert task in done and not pending, "Response send outlived the model deadline"
        with pytest.raises((TimeoutError, ClientDisconnect, ExceptionGroup)):
            await task
        assert setup_gateway.app.state.model_executor._admitted == 0
        assert len(setup_gateway.records) == 1
        assert all(client.is_closed for client in setup_gateway.clients)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stream_timeout_error_survives_http_middleware(http_client, setup_gateway):
    @setup_gateway.app.middleware("http")
    async def pass_through(request, call_next):
        return await call_next(request)

    setup_gateway.config.slots[SLOT_ID].timeout_seconds = 0.05

    class WaitingStream(BytesStream):
        async def __aiter__(self):
            yield event(chunk("partial"))
            await asyncio.Event().wait()

    async def upstream(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=WaitingStream(b""))

    setup_gateway.upstream.handler = upstream
    response = await asyncio.wait_for(http_client.post(PREFIX + "/chat/completions", headers=auth(setup_gateway), json=body(stream=True)), 2)
    assert response.status_code == 200
    assert '"code":"gateway_timeout"' in response.text
    assert "[DONE]" not in response.text


async def test_final_error_flush_window_is_bounded(setup_gateway):
    from plugin.server.model_gateway.errors import ModelGatewayError

    async def upstream():
        yield event(chunk("partial"))
        raise ModelGatewayError("gateway_timeout", "Request expired", 504)

    iterator = upstream()
    first = await anext(iterator)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    response = routes._ModelStreamResponse(first, iterator, request, setup_gateway.tokens["alpha"],
                                          asyncio.get_running_loop().time() + 0.02)

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if b'"error"' in message.get("body", b""):
            await asyncio.Event().wait()

    task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send))
    try:
        done, pending = await asyncio.wait({task}, timeout=0.5)
        assert task in done and not pending
        with pytest.raises((TimeoutError, ExceptionGroup)):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
