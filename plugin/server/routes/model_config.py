"""Administrative model-slot settings. Model execution is added separately."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Body, HTTPException

from plugin.logging_config import get_logger
from plugin.server.application.model_config_service import ModelConfigService
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.auth import require_admin
from plugin.server.infrastructure.error_mapping import raise_http_from_domain

router = APIRouter(prefix="/api/model-config", tags=["plugin-models"])
logger = get_logger("server.routes.model_config")
service = ModelConfigService()


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
