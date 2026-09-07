"""
插件管理路由
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from plugin.logging_config import get_logger
from plugin.server.application.plugins import (
    PluginLifecycleService,
    PluginQueryService,
    PluginRegistryService,
)
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.auth import require_admin
from plugin.server.application.plugins.operation_lock import (
    PluginOperationBusy,
    bounded_operation_wait,
)
from plugin.server.infrastructure.error_mapping import raise_http_from_domain
from plugin.server.lifecycle import ensure_plugin_messaging_started

router = APIRouter()
logger = get_logger("server.routes.plugins")
query_service = PluginQueryService()
lifecycle_service = PluginLifecycleService()
registry_service = PluginRegistryService()


@router.get("/plugin/status")
async def plugin_status(plugin_id: Optional[str] = Query(default=None)) -> dict[str, object]:
    try:
        return await query_service.get_plugin_status(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)

@router.get("/plugins")
async def list_plugins(locale: Optional[str] = Query(default=None)) -> dict[str, object]:
    try:
        return await query_service.list_plugins(locale=locale)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


# 用户在等的那些插件操作，抢锁不能无限等。
#
# 前端 30s 就放弃，而放弃之后那次操作照样会落地（mutation 被 asyncio.shield 保
# 着），于是用户看到"失败"、插件其实被启停了。宁可在预算内立刻回 409 并说明是
# 谁占着。后台调用方（自启动对账、安装事务）不经过这里，行为不变。
# Env: NEKO_PLUGIN_OPERATION_WAIT_BUDGET
from plugin.server.application.plugins._env_budgets import env_seconds

_OPERATION_WAIT_BUDGET_SECONDS = env_seconds("NEKO_PLUGIN_OPERATION_WAIT_BUDGET", 20.0)


def _busy_response() -> HTTPException:
    """Shape this 409 like every other error this router emits.

    It went out as a dict ``detail`` with a hard-coded Simplified-Chinese string
    and no ``X-Error-Code`` header, while every other failure here goes through
    ``raise_http_from_domain`` — string detail, header set, message in English
    like the rest of the domain errors. A client keying on the header simply did
    not see this one (本轮对抗复审).
    """
    return HTTPException(
        status_code=409,
        detail="Another plugin operation is in progress; please retry shortly",
        headers={"X-Error-Code": "PLUGIN_OPERATION_BUSY"},
    )


@router.post("/plugin/{plugin_id}/start")
async def start_plugin_endpoint(plugin_id: str, _: str = require_admin) -> dict[str, object]:
    try:
        with bounded_operation_wait(_OPERATION_WAIT_BUDGET_SECONDS):
            await ensure_plugin_messaging_started()
            return await lifecycle_service.start_plugin(plugin_id, persist_user_intent=True)
    except PluginOperationBusy:
        raise _busy_response()
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.post("/plugin/{plugin_id}/refresh")
async def refresh_plugin_endpoint(plugin_id: str, _: str = require_admin) -> dict[str, object]:
    try:
        return await registry_service.refresh_plugin(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.post("/plugin/{plugin_id}/stop")
async def stop_plugin_endpoint(plugin_id: str, _: str = require_admin) -> dict[str, object]:
    try:
        with bounded_operation_wait(_OPERATION_WAIT_BUDGET_SECONDS):
            return await lifecycle_service.stop_plugin(plugin_id, persist_user_intent=True)
    except PluginOperationBusy:
        raise _busy_response()
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.delete("/plugin/{plugin_id}")
async def delete_plugin_endpoint(plugin_id: str, _: str = require_admin) -> dict[str, object]:
    try:
        with bounded_operation_wait(_OPERATION_WAIT_BUDGET_SECONDS):
            return await lifecycle_service.delete_plugin(plugin_id)
    except PluginOperationBusy:
        raise _busy_response()
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.post("/plugins/refresh")
async def refresh_plugins_endpoint(_: str = require_admin) -> dict[str, object]:
    try:
        return await registry_service.refresh_registry()
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.post("/plugin/{plugin_id}/reload")
async def reload_plugin_endpoint(plugin_id: str, _: str = require_admin) -> dict[str, object]:
    try:
        with bounded_operation_wait(_OPERATION_WAIT_BUDGET_SECONDS):
            return await lifecycle_service.reload_plugin(plugin_id)
    except PluginOperationBusy:
        raise _busy_response()
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.post("/plugins/reload")
async def reload_all_plugins_endpoint(_: str = require_admin) -> dict[str, object]:
    """
    重载所有插件
    
    停止所有运行中的插件，然后重新加载。
    用于前端全局重载按钮。
    """
    try:
        with bounded_operation_wait(_OPERATION_WAIT_BUDGET_SECONDS):
            return await lifecycle_service.reload_all_plugins()
    except PluginOperationBusy:
        raise _busy_response()
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
