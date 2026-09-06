"""Administrative model-slot settings and explicit saved-slot connection tests."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

import anyio
from fastapi import APIRouter, Body, HTTPException, Request

from plugin.logging_config import get_logger
from plugin.server.application.model_config_service import ModelConfigService
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.auth import require_admin
from plugin.server.infrastructure.error_mapping import raise_http_from_domain
from plugin.server.model_gateway.errors import ModelGatewayError
from plugin.server.model_gateway.execution import ResolvedModelCall
from plugin.server.model_gateway.observation import normalize_usage
from plugin.server.routes.model_gateway import _get_executor

router = APIRouter(prefix="/api/model-config", tags=["plugin-models"])
logger = get_logger("server.routes.model_config")
service = ModelConfigService()
PROBE_IDENTITY = "@host:model_probe"  # Invalid as a plugin ID; never a registry entry.
PROBE_USAGE = "connection_test"
PROBE_MAX_SECONDS = 15.0


def _object_payload(payload: object) -> dict:
    # FastAPI's default dict validation echoes invalid input, including keys in
    # an accidentally submitted list. Keep credential-bearing errors generic.
    if not isinstance(payload, dict):
        raise HTTPException(422, "Expected a JSON object")
    return payload


async def _call(method: Callable, *args):
    from utils.cloudsave_runtime import MaintenanceModeError

    try:
        # JSON/config IO and storage transactions must stay off the HTTP loop.
        return await asyncio.to_thread(method, *args)
    except ServerDomainError as exc:
        raise_http_from_domain(exc, logger=logger, include_details=True)
    except MaintenanceModeError as exc:
        raise HTTPException(409, "Model settings cannot be changed during storage maintenance") from exc


@router.get("/slots")
async def list_slots(_: str = require_admin):
    return await _call(service.list_slots)


@router.post("/slots", status_code=201)
async def create_slot(payload: object = Body(...), _: str = require_admin):
    return await _call(service.create_slot, _object_payload(payload))


@router.get("/slots/{slot_id}")
async def get_slot(slot_id: str, _: str = require_admin):
    return await _call(service.get_slot, slot_id)


def _resolve_probe(slot_id: str) -> ResolvedModelCall:
    config = service.store.read()
    slot = service._slot(config, slot_id).model_copy(deep=True)
    slot.timeout_seconds = min(slot.timeout_seconds, PROBE_MAX_SECONDS)
    slot.fallback_slot_id = None
    return ResolvedModelCall(PROBE_IDENTITY, PROBE_USAGE, slot_id, slot)


async def _probe_disconnect(request: Request, stopped: asyncio.Event) -> None:
    while not stopped.is_set():
        if await request.is_disconnected():
            return
        try:
            await asyncio.wait_for(stopped.wait(), timeout=0.1)
        except TimeoutError:
            pass


@router.post("/slots/{slot_id}/test")
async def test_slot(slot_id: str, request: Request, _: str = require_admin):
    # Use only the saved snapshot. The test cannot supply a different URL/key,
    # inherit a plugin binding or report a fallback's success for this slot.
    call = await _call(_resolve_probe, slot_id)
    executor = _get_executor(request)
    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(executor.complete(call, {
        "model": PROBE_USAGE,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_completion_tokens": 16,
    }))
    stopped = asyncio.Event()
    watcher = asyncio.create_task(_probe_disconnect(request, stopped))
    try:
        done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if task not in done:
            raise asyncio.CancelledError
        result = await task
        usage = normalize_usage(result.get("usage"))
        return {
            "slot_id": slot_id,
            "status": "success",
            "duration_ms": round((asyncio.get_running_loop().time() - started) * 1000, 3),
            "usage_status": "reported" if usage is not None else "unknown",
            "usage": usage,
        }
    except ModelGatewayError as exc:
        raise HTTPException(
            exc.status_code, {"code": exc.code, "message": exc.message},
            headers={"X-Error-Code": exc.code},
        ) from None
    finally:
        stopped.set()
        watcher.cancel()
        if not task.done():
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(task, watcher, return_exceptions=True)


@router.patch("/slots/{slot_id}")
async def update_slot(slot_id: str, payload: object = Body(...), _: str = require_admin):
    return await _call(service.update_slot, slot_id, _object_payload(payload))


@router.delete("/slots/{slot_id}")
async def delete_slot(slot_id: str, _: str = require_admin):
    return await _call(service.delete_slot, slot_id)


@router.get("/plugins/{plugin_id}/bindings")
async def get_bindings(plugin_id: str, _: str = require_admin):
    return await _call(service.get_bindings, plugin_id)


@router.put("/plugins/{plugin_id}/bindings/{usage_id}")
async def set_binding(plugin_id: str, usage_id: str, payload: object = Body(...), _: str = require_admin):
    payload = _object_payload(payload)
    if set(payload) != {"slot_id"} or not isinstance(payload.get("slot_id"), str):
        raise HTTPException(422, "Expected a slot_id string")
    return await _call(service.set_binding, plugin_id, usage_id, payload["slot_id"])


@router.delete("/plugins/{plugin_id}/bindings/{usage_id}")
async def delete_binding(plugin_id: str, usage_id: str, _: str = require_admin):
    return await _call(service.delete_binding, plugin_id, usage_id)
