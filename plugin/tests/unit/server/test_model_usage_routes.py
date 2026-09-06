from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.auth import verify_admin_code
from plugin.server.routes import model_usage

pytestmark = pytest.mark.plugin_unit


class StubRecorder:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def get_usage(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return {"requests": [], "summary": {"window": "recent_retained"}}


@pytest.fixture
async def client_env(monkeypatch):
    recorder = StubRecorder()
    monkeypatch.setattr(model_usage, "recorder", recorder)
    app = FastAPI()
    app.include_router(model_usage.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield client, recorder, app


async def test_usage_query_forwards_filters_and_default_limit(client_env):
    client, recorder, _ = client_env
    response = await client.get("/api/model-config/usage", params={"plugin_id": "alpha", "slot_id": "slot-a"})
    assert response.status_code == 200
    assert response.json()["summary"]["window"] == "recent_retained"
    assert recorder.calls == [{"plugin_id": "alpha", "slot_id": "slot-a", "limit": 100}]


@pytest.mark.parametrize("limit", [0, -1, 1001, "invalid"])
async def test_usage_query_rejects_invalid_limits(client_env, limit):
    client, recorder, _ = client_env
    response = await client.get("/api/model-config/usage", params={"limit": limit})
    assert response.status_code == 422
    assert recorder.calls == []


async def test_usage_route_uses_existing_admin_policy(client_env):
    client, recorder, app = client_env

    async def deny():
        raise HTTPException(403, "Forbidden")

    app.dependency_overrides[verify_admin_code] = deny
    response = await client.get("/api/model-config/usage")
    assert response.status_code == 403
    assert recorder.calls == []


async def test_safe_storage_errors_use_standard_domain_mapping(client_env):
    client, recorder, _ = client_env
    recorder.failure = ServerDomainError("MODEL_USAGE_READ_FAILED", "Plugin model usage could not be read", 500)
    response = await client.get("/api/model-config/usage")
    assert response.status_code == 500
    assert response.headers["x-error-code"] == "MODEL_USAGE_READ_FAILED"
    assert response.json()["detail"]["message"] == "Plugin model usage could not be read"


async def test_usage_route_is_read_only(client_env):
    client, recorder, _ = client_env
    response = await client.post("/api/model-config/usage", json={"requests": []})
    assert response.status_code == 405
    assert recorder.calls == []
