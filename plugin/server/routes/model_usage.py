"""Read-only plugin model usage within the retained local request window."""
from __future__ import annotations

from fastapi import APIRouter, Query

from plugin.logging_config import get_logger
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.auth import require_admin
from plugin.server.infrastructure.error_mapping import raise_http_from_domain
from plugin.server.infrastructure.model_usage_store import MAX_RECORDS, ModelUsageRecorder

router = APIRouter(prefix="/api/model-config", tags=["plugin-models"])
logger = get_logger("server.routes.model_usage")
recorder = ModelUsageRecorder()


@router.get("/usage")
async def get_usage(
    plugin_id: str | None = Query(None, max_length=256),
    slot_id: str | None = Query(None, max_length=256),
    limit: int = Query(100, ge=1, le=MAX_RECORDS),
    _: str = require_admin,
):
    try:
        return await recorder.get_usage(plugin_id=plugin_id, slot_id=slot_id, limit=limit)
    except ServerDomainError as exc:
        raise_http_from_domain(exc, logger=logger, include_details=True)
