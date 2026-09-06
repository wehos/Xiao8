from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from plugin.sdk.shared.core.context import SdkContext, ensure_sdk_context
from plugin.sdk.shared.core.models import PluginModels, _GatewayHttpClient
from plugin.sdk.shared.models.exceptions import CapabilityUnavailableError


pytestmark = pytest.mark.plugin_unit


def _models() -> PluginModels:
    return PluginModels(
        SimpleNamespace(
            _model_gateway_base_url="http://127.0.0.1:48916/api/models/v1",
            _model_gateway_token="plugin-instance-token",
        )
    )


@pytest.mark.asyncio
async def test_client_is_official_reusable_and_recreated_after_caller_close() -> None:
    models = _models()
    first = await models.get_client()
    assert isinstance(first, AsyncOpenAI)
    assert first is await models.get_client()
    assert first.max_retries == 0
    assert first.timeout == 360.0
    assert first._client.trust_env is False
    await first.close()
    second = await models.get_client()
    assert first is not second
    await models.aclose()
    assert second.is_closed()


@pytest.mark.asyncio
async def test_sdk_request_keeps_binding_alias_and_only_gateway_token(
    monkeypatch,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "analysis",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    original_init = _GatewayHttpClient.__init__

    def init(self, **kwargs):
        original_init(self, **kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(_GatewayHttpClient, "__init__", init)
    models = _models()
    try:
        client = await models.get_client()
        result = await client.chat.completions.create(
            model="analysis",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert result.choices[0].message.content == "done"
        assert str(requests[0].url) == (
            "http://127.0.0.1:48916/api/models/v1/chat/completions"
        )
        assert requests[0].headers["authorization"] == "Bearer plugin-instance-token"
        assert json.loads(requests[0].content)["model"] == "analysis"
    finally:
        await models.aclose()


@pytest.mark.asyncio
async def test_older_host_reports_capability_unavailable_only_on_use() -> None:
    context = SdkContext(SimpleNamespace(plugin_id="old"))
    assert context.plugin_id == "old"
    assert context.models is context.models
    with pytest.raises(CapabilityUnavailableError) as caught:
        await context.models.get_client()
    assert caught.value.code == "MODEL_GATEWAY_UNAVAILABLE"


def test_sdk_models_delegates_and_preserves_existing_duck_type_contract() -> None:
    from plugin.sdk.shared.core import context as context_module

    sentinel = object()
    assert SdkContext(SimpleNamespace(models=sentinel)).models is sentinel
    fake = SimpleNamespace()
    for name in context_module._SDK_CONTEXT_ATTR_NAMES:
        setattr(fake, name, None)
    for name in context_module._SDK_CONTEXT_METHOD_NAMES:
        setattr(fake, name, lambda: None)
    assert ensure_sdk_context(fake) is fake


def test_sequential_lifecycle_loops_have_distinct_clients() -> None:
    models = _models()

    async def lifecycle():
        client = await models.get_client()
        await models.aclose()
        return client

    first = asyncio.run(lifecycle())
    second = asyncio.run(lifecycle())
    assert first is not second
    assert first.is_closed() and second.is_closed()


@pytest.mark.asyncio
async def test_close_disables_recreation_and_awaited_cleanup_finishes() -> None:
    models = _models()
    client = await models.get_client()
    models.close()
    with pytest.raises(CapabilityUnavailableError):
        await models.get_client()
    await models.aclose()
    assert client.is_closed()
    models.close()


@pytest.mark.asyncio
async def test_cleanup_cancels_active_request() -> None:
    entered = asyncio.Event()

    async def respond(request):
        entered.set()
        await asyncio.Event().wait()

    client = _GatewayHttpClient(transport=httpx.MockTransport(respond))
    request = asyncio.create_task(client.get("http://gateway.test/chat"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    await client.aclose()
    await asyncio.gather(request, return_exceptions=True)
    assert request.cancelled()


@pytest.mark.asyncio
async def test_cleanup_cancels_active_stream_read() -> None:
    entered = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    client = _GatewayHttpClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
    )

    async def consume():
        async with client.stream("GET", "http://gateway.test/chat") as response:
            async for _ in response.aiter_bytes():
                pass

    request = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await client.aclose()
    await asyncio.gather(request, return_exceptions=True)
    assert request.cancelled()


@pytest.mark.asyncio
async def test_request_cancellation_can_close_models_again(monkeypatch) -> None:
    entered = asyncio.Event()

    async def respond(request):
        entered.set()
        await asyncio.Event().wait()

    original_init = _GatewayHttpClient.__init__

    def init(self, **kwargs):
        original_init(self, **kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(_GatewayHttpClient, "__init__", init)
    models = _models()
    client = await models.get_client()

    async def handler():
        try:
            await client.chat.completions.create(
                model="analysis",
                messages=[{"role": "user", "content": "hello"}],
            )
        finally:
            await models.aclose()

    request = asyncio.create_task(handler())
    await asyncio.wait_for(entered.wait(), timeout=1)
    models.close()
    await asyncio.wait_for(models.aclose(), timeout=1)
    await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), timeout=1)
    assert request.cancelled()


def test_close_from_another_thread_cleans_up_on_owner_loop() -> None:
    models = _models()
    ready = threading.Event()
    stopped = threading.Event()
    clients: list[AsyncOpenAI] = []
    errors: list[BaseException] = []

    async def worker():
        client = await models.get_client()
        clients.append(client)
        ready.set()
        while not client.is_closed():
            await asyncio.sleep(0.001)
        await models.aclose()

    def run():
        try:
            asyncio.run(asyncio.wait_for(worker(), timeout=3))
        except BaseException as exc:
            errors.append(exc)
        finally:
            stopped.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(timeout=2)
        models.close()
        assert stopped.wait(timeout=4)
        assert not errors
        assert clients[0].is_closed()
    finally:
        thread.join(timeout=4)


def test_client_cannot_be_reused_in_another_loop() -> None:
    first_loop = asyncio.new_event_loop()
    models = _models()
    client = first_loop.run_until_complete(models.get_client())

    async def misuse():
        with pytest.raises(RuntimeError, match="current event loop"):
            await client._client.get("http://gateway.test/chat")

    try:
        asyncio.run(misuse())
    finally:
        first_loop.run_until_complete(models.aclose())
        first_loop.close()
