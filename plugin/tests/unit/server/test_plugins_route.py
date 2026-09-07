from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.exceptions import register_exception_handlers
from plugin.server.routes import plugins as route_module


pytestmark = pytest.mark.plugin_unit


@pytest.fixture
def plugin_route_test_app() -> FastAPI:
    app = FastAPI(title="plugin-route-test-app")
    register_exception_handlers(app)
    app.include_router(route_module.router)
    return app


@pytest.mark.asyncio
async def test_plugins_refresh_routes_delegate_to_registry_service(
    plugin_route_test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_calls: list[str] = []

    async def _refresh_registry() -> dict[str, object]:
        refresh_calls.append("all")
        return {"success": True, "added": ["demo"], "updated": [], "removed": []}

    async def _refresh_plugin(plugin_id: str) -> dict[str, object]:
        refresh_calls.append(plugin_id)
        return {"success": True, "plugin_id": plugin_id, "status": "updated"}

    monkeypatch.setattr(route_module.registry_service, "refresh_registry", _refresh_registry)
    monkeypatch.setattr(route_module.registry_service, "refresh_plugin", _refresh_plugin)

    transport = ASGITransport(app=plugin_route_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        all_response = await client.post("/plugins/refresh")
        assert all_response.status_code == 200
        assert all_response.json()["added"] == ["demo"]
        # force 参数没有了：刷新不再有缓存可绕过，每次都重读盘面。签名里留一个
        # 恒为真的开关只会让人以为还存在一条"不重读"的路。
        assert refresh_calls == ["all"], f"刷新路由没有调到注册表：{refresh_calls}"

        one_response = await client.post("/plugin/demo/refresh")
        assert one_response.status_code == 200
        assert one_response.json()["plugin_id"] == "demo"
        assert refresh_calls == ["all", "demo"], (
            f"单插件刷新没有把插件 id 传下去：{refresh_calls}"
        )


@pytest.mark.asyncio
async def test_delete_plugin_route_delegates_to_lifecycle_service(
    plugin_route_test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _delete_plugin(plugin_id: str) -> dict[str, object]:
        return {"success": True, "plugin_id": plugin_id, "message": "deleted"}

    monkeypatch.setattr(route_module.lifecycle_service, "delete_plugin", _delete_plugin)

    transport = ASGITransport(app=plugin_route_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete("/plugin/demo")
        assert response.status_code == 200
        assert response.json()["plugin_id"] == "demo"


@pytest.mark.asyncio
async def test_delete_plugin_route_preserves_ownership_error_code(
    plugin_route_test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _delete_plugin(_plugin_id: str) -> dict[str, object]:
        raise ServerDomainError(
            code="PLUGIN_MANUAL_NOT_MANAGED",
            message="manual plugin is not managed",
            status_code=409,
        )

    monkeypatch.setattr(route_module.lifecycle_service, "delete_plugin", _delete_plugin)

    transport = ASGITransport(app=plugin_route_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete("/plugin/demo")

    assert response.status_code == 409
    assert response.headers["X-Error-Code"] == "PLUGIN_MANUAL_NOT_MANAGED"
    assert response.json() == {"detail": "manual plugin is not managed"}


@pytest.mark.asyncio
async def test_stop_plugin_route_persists_user_intent(
    plugin_route_test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []

    async def _stop_plugin(plugin_id: str, *, persist_user_intent: bool = False) -> dict[str, object]:
        calls.append((plugin_id, persist_user_intent))
        return {"success": True, "plugin_id": plugin_id, "message": "stopped"}

    monkeypatch.setattr(route_module.lifecycle_service, "stop_plugin", _stop_plugin)

    transport = ASGITransport(app=plugin_route_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/plugin/demo/stop")

    assert response.status_code == 200
    assert response.json()["plugin_id"] == "demo"
    assert calls == [("demo", True)]
