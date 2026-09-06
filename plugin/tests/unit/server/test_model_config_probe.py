from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from plugin.server.application.model_config_service import ModelConfigService
from plugin.server.application.model_gateway_service import ModelGatewayService
from plugin.server.domain.model_config import ModelSlot, PluginModelsConfig
from plugin.server.infrastructure.auth import verify_admin_code
from plugin.server.model_gateway.execution import ModelExecutor
from plugin.server.routes import model_config

pytestmark = pytest.mark.plugin_unit
SLOT_ID = "slot_" + "a" * 32
FALLBACK_ID = "slot_" + "b" * 32
SECRET = "sk-private-probe-credential"
USAGE = {"prompt_tokens": 6, "completion_tokens": 1, "total_tokens": 7}


class ReadOnlyStore:
    def __init__(self, protocol="openai_chat", timeout_seconds=60.0):
        primary = ModelSlot(
            name="Saved model", protocol=protocol, base_url="https://saved.test/v1",
            model="saved-model", api_key=SECRET, timeout_seconds=timeout_seconds,
            fallback_slot_id=FALLBACK_ID,
        )
        backup = primary.model_copy(update={"model": "backup", "fallback_slot_id": None})
        self.config = PluginModelsConfig(slots={SLOT_ID: primary, FALLBACK_ID: backup})
        self.threads = []

    def read(self):
        self.threads.append(threading.get_ident())
        return self.config.model_copy(deep=True)


class Recorder:
    def __init__(self):
        self.requests = []

    async def record_request(self, value):
        self.requests.append(value)


def response_for(protocol, include_usage=True):
    if protocol == "anthropic_messages":
        return {
            "id": "msg-test", "type": "message", "role": "assistant", "model": "saved-model",
            "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 6, "output_tokens": 1},
        }
    response = {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "saved-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
    }
    if include_usage:
        response["usage"] = USAGE
    return response


@pytest.fixture
async def setup_probe(monkeypatch):
    executors = []

    def setup(handler=None, *, protocol="openai_chat", timeout_seconds=60.0):
        store = ReadOnlyStore(protocol, timeout_seconds)
        recorder = Recorder()
        seen = []
        clients = []

        async def upstream(request):
            seen.append(request)
            if handler is not None:
                return await handler(request)
            return httpx.Response(200, json=response_for(protocol))

        def make_client(slot):
            client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), timeout=slot.timeout_seconds)
            clients.append(client)
            return client

        executor = ModelExecutor(ModelGatewayService(make_client), recorder, max_active=1, max_waiting=0)
        executors.append(executor)
        app = FastAPI()
        app.state.model_executor = executor
        app.include_router(model_config.router)
        monkeypatch.setattr(model_config, "service", ModelConfigService(store))
        return app, store, recorder, seen, clients

    yield setup
    for executor in executors:
        await executor.aclose()


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_probe_uses_saved_credentials_and_shared_accounting(setup_probe, protocol):
    app, store, recorder, seen, clients = setup_probe(protocol=protocol)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        response = await client.post(f"/api/model-config/slots/{SLOT_ID}/test")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["slot_id"] == SLOT_ID
    assert result["status"] == "success"
    assert result["duration_ms"] >= 0
    assert result["usage_status"] == "reported"
    assert {key: result["usage"][key] for key in USAGE} == USAGE
    assert SECRET not in response.text
    assert len(seen) == 1
    sent = json.loads(seen[0].content)
    assert sent["model"] == "saved-model"
    if protocol == "openai_chat":
        assert sent["max_completion_tokens"] == 16
        assert "max_tokens" not in sent
        assert sent["messages"] == [{"role": "user", "content": "Reply with OK."}]
        assert seen[0].headers["Authorization"] == "Bearer " + SECRET
    else:
        assert sent["max_tokens"] == 16
        assert "max_completion_tokens" not in sent
        assert sent["messages"][0]["content"] == [{"type": "text", "text": "Reply with OK."}]
        assert seen[0].headers["x-api-key"] == SECRET
    assert clients[0].is_closed
    assert len(recorder.requests) == 1
    record = recorder.requests[0]
    assert record["plugin_id"] == model_config.PROBE_IDENTITY
    assert record["usage_id"] == model_config.PROBE_USAGE
    assert record["slot_id"] == SLOT_ID
    assert len(record["attempts"]) == 1
    assert record["attempts"][0]["usage_status"] == "reported"
    assert store.config.slots[SLOT_ID].timeout_seconds == 60
    assert store.config.slots[SLOT_ID].fallback_slot_id == FALLBACK_ID
    assert store.threads and threading.get_ident() not in store.threads


async def test_probe_cannot_override_saved_target_in_request_body(setup_probe):
    app, _, _, seen, _ = setup_probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        response = await client.post(f"/api/model-config/slots/{SLOT_ID}/test", json={
            "base_url": "https://untrusted.test", "api_key": "different", "model": "different", "max_tokens": 999,
        })
    assert response.status_code == 200
    assert seen[0].url.host == "saved.test"
    sent = json.loads(seen[0].content)
    assert sent["model"] == "saved-model" and sent["max_completion_tokens"] == 16


async def test_probe_supports_openai_models_rejecting_legacy_max_tokens(setup_probe):
    async def upstream(request):
        sent = json.loads(request.content)
        if "max_tokens" in sent:
            return httpx.Response(400, json={"error": {"message": "Use max_completion_tokens"}})
        assert sent["max_completion_tokens"] == 16
        return httpx.Response(200, json=response_for("openai_chat"))

    app, _, recorder, seen, _ = setup_probe(upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        response = await client.post(f"/api/model-config/slots/{SLOT_ID}/test")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert len(seen) == 1
    assert recorder.requests[0]["status"] == "success"


async def test_probe_without_usage_does_not_invent_zero_counters(setup_probe):
    async def upstream(_request):
        return httpx.Response(200, json=response_for("openai_chat", include_usage=False))

    app, _, recorder, _, _ = setup_probe(upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        response = await client.post(f"/api/model-config/slots/{SLOT_ID}/test")
    assert response.json()["usage_status"] == "unknown"
    assert response.json()["usage"] is None
    assert recorder.requests[0]["attempts"][0]["usage_status"] == "unknown"


async def test_probe_obeys_admin_dependency_before_reading_config(setup_probe):
    app, store, recorder, seen, _ = setup_probe()

    async def deny():
        raise HTTPException(403, "Denied")

    app.dependency_overrides[verify_admin_code] = deny
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        response = await client.post(f"/api/model-config/slots/{SLOT_ID}/test")
    assert response.status_code == 403
    assert not store.threads and not recorder.requests and not seen


async def test_missing_slot_fails_without_creating_a_request(setup_probe):
    app, _, recorder, seen, _ = setup_probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        response = await client.post("/api/model-config/slots/missing/test")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "MODEL_SLOT_NOT_FOUND"
    assert not recorder.requests and not seen


async def test_probe_reports_provider_failure_without_fallback_or_secret(setup_probe):
    async def upstream(_request):
        return httpx.Response(503, json={"error": {"message": SECRET}})

    app, _, recorder, seen, clients = setup_probe(upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        response = await client.post(f"/api/model-config/slots/{SLOT_ID}/test")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "upstream_error"
    assert SECRET not in response.text
    assert len(seen) == 1
    assert clients[0].is_closed
    assert recorder.requests[0]["status"] == "error"
    assert len(recorder.requests[0]["attempts"]) == 1


@pytest.mark.parametrize("slot_timeout, cap", [(0.04, 15.0), (60.0, 0.04)])
async def test_probe_uses_shorter_slot_or_probe_deadline(setup_probe, monkeypatch, slot_timeout, cap):
    async def upstream(_request):
        await asyncio.Event().wait()

    monkeypatch.setattr(model_config, "PROBE_MAX_SECONDS", cap)
    app, _, recorder, _, clients = setup_probe(upstream, timeout_seconds=slot_timeout)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        response = await client.post(f"/api/model-config/slots/{SLOT_ID}/test")
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "gateway_timeout"
    assert recorder.requests[0]["status"] == "timeout"
    assert recorder.requests[0]["duration_ms"] < 500
    assert clients[0].is_closed


async def test_probe_shares_capacity_with_other_model_requests(setup_probe):
    started = asyncio.Event()

    async def upstream(_request):
        started.set()
        await asyncio.Event().wait()

    app, _, recorder, seen, _ = setup_probe(upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local.test") as client:
        first = asyncio.create_task(client.post(f"/api/model-config/slots/{SLOT_ID}/test"))
        await started.wait()
        response = await client.post(f"/api/model-config/slots/{SLOT_ID}/test")
        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "model_gateway_busy"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert len(seen) == 1
    assert len(recorder.requests) == 2
    assert sorted(record["status"] for record in recorder.requests) == ["cancelled", "error"]


async def test_browser_disconnect_cancels_probe_and_closes_upstream(setup_probe):
    started = asyncio.Event()
    disconnected = asyncio.Event()

    async def upstream(_request):
        started.set()
        await asyncio.Event().wait()

    app, _, recorder, _, clients = setup_probe(upstream)
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        pass

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": f"/api/model-config/slots/{SLOT_ID}/test", "query_string": b"",
        "headers": [], "server": ("127.0.0.1", 48916), "client": ("127.0.0.1", 1234), "root_path": "",
    }
    request = asyncio.create_task(app(scope, receive, send))
    await started.wait()
    disconnected.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 1)
    assert clients[0].is_closed
    assert recorder.requests[0]["status"] == "cancelled"
