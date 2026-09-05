"""Authenticated Chat Completions for running plugin instances."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress

from fastapi import APIRouter, Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, StreamingResponse

from plugin.core.model_gateway_access import ModelGatewayAccessError, model_gateway_access
from plugin.server.application.model_config_service import load_model_requirements
from plugin.server.application.model_gateway_service import MAX_REQUEST_BYTES, ModelGatewayService
from plugin.server.domain.errors import ServerDomainError
from plugin.server.domain.model_config import ModelSlot
from plugin.server.infrastructure.model_config_store import ModelConfigStore
from plugin.server.model_gateway.errors import ModelGatewayError
from plugin.server.model_gateway.transport import encode_sse

router = APIRouter(prefix="/api/models/v1", tags=["plugin-model-gateway"])
gateway_service = ModelGatewayService()
config_store = ModelConfigStore()
_USAGE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LIVENESS_INTERVAL = 0.25


def _error_response(error: ModelGatewayError) -> JSONResponse:
    return JSONResponse(
        error.to_dict(), status_code=error.status_code,
        headers={"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None,
    )


def _access_error() -> ModelGatewayError:
    return ModelGatewayError("plugin_model_access_denied", "Plugin model access is unavailable", 401)


def _authenticate(request: Request) -> tuple[str, str]:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 256:
        raise ModelGatewayAccessError("Plugin model access is unavailable")
    return token, model_gateway_access.authenticate(token)


def _decode_request(data: bytes) -> dict:
    try:
        body = json.loads(data)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ModelGatewayError("invalid_request", "Expected a JSON request object") from exc
    if not isinstance(body, dict):
        raise ModelGatewayError("invalid_request", "Expected a JSON request object")
    return body


async def _read_request(request: Request) -> dict:
    parts = []
    size = 0
    async for part in request.stream():
        size += len(part)
        if size > MAX_REQUEST_BYTES:
            raise ModelGatewayError("request_too_large", "Model request exceeded the size limit", 413)
        parts.append(part)
    return await asyncio.to_thread(_decode_request, b"".join(parts))


def _resolve_slot(plugin_id: str, usage_id: object) -> ModelSlot:
    if not isinstance(usage_id, str) or not _USAGE_ID.fullmatch(usage_id):
        raise ModelGatewayError("invalid_request", "model must be a declared plugin usage name", param="model")
    try:
        requirements = load_model_requirements(plugin_id)
        requirement = requirements.get(usage_id)
        if requirement is None:
            raise ModelGatewayError("model_usage_not_declared", "Plugin has not declared this model usage", 403, "model")
        config = config_store.read()
        slot_id = config.bindings.get(plugin_id, {}).get(usage_id)
        slot = config.slots.get(slot_id) if slot_id else None
        if slot is None:
            raise ModelGatewayError("model_usage_not_bound", "Bind a model slot to this plugin usage first", 403, "model")
        if not set(requirement.capabilities).issubset(slot.capabilities):
            raise ModelGatewayError("model_capability_mismatch", "Bound slot no longer meets plugin requirements", 409, "model")
        return slot.model_copy(deep=True)
    except ServerDomainError as exc:
        # Translate before crossing asynchronous/context-manager boundaries;
        # the shared domain exception is a frozen dataclass.
        raise ModelGatewayError(exc.code.lower(), exc.message, exc.status_code) from None


class _RequestGuard:
    """Track instance revocation and detect process death while a call is active."""

    def __init__(self, request: Request, token: str):
        self.request = request
        self.token = token
        self.watch_disconnect = False
        self.task = None
        self.watcher = None
        self.stopping = False

    async def __aenter__(self):
        self.task = asyncio.current_task()
        model_gateway_access.track(self.token, self.task)
        self.watcher = asyncio.create_task(self._watch())
        return self

    async def _watch(self):
        while not self.stopping:
            await asyncio.sleep(_LIVENESS_INTERVAL)
            if self.stopping:
                return
            try:
                model_gateway_access.authenticate(self.token)
            except ModelGatewayAccessError:
                self.task.cancel()
                return
            # Only one consumer may read ASGI receive: enable this after the
            # request body is read, and leave SSE disconnects to StreamingResponse.
            if self.watch_disconnect and await self.request.is_disconnected():
                if not self.stopping:
                    self.task.cancel()
                return

    async def __aexit__(self, *_args):
        # Request.is_disconnected() uses its own cancelled AnyIO scope and may
        # consume task.cancel() in a race. The explicit flag still ends polling.
        self.stopping = True
        self.watcher.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await self.watcher
        finally:
            model_gateway_access.untrack(self.token, self.task)


class _ModelStreamResponse(StreamingResponse):
    def __init__(self, first: bytes, upstream, request: Request, token: str):
        self.upstream = upstream
        self.request = request
        self.token = token

        async def forward():
            yield first
            try:
                async for chunk in upstream:
                    yield chunk
            except ModelGatewayError as exc:
                # After headers, report a standard SDK stream error, never DONE.
                yield encode_sse(exc.to_dict())

        super().__init__(forward(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def __call__(self, scope, receive, send):
        try:
            # Re-authenticate across the response handoff: it may have been
            # revoked after prefetch. This also covers process death mid-stream.
            async with _RequestGuard(self.request, self.token) as guard:
                # ASGI 2.4 StreamingResponse relies on send errors instead of a
                # receive listener. Poll disconnects while upstream is stalled.
                spec = tuple(map(int, scope.get("asgi", {}).get("spec_version", "2.0").split(".")))
                guard.watch_disconnect = spec >= (2, 4)
                await super().__call__(scope, receive, send)
        except ModelGatewayAccessError:
            await _error_response(_access_error())(scope, receive, send)
        finally:
            # Close even if headers fail or the body iterator never starts.
            await self.upstream.aclose()
            await self.body_iterator.aclose()


@router.post("/chat/completions")
async def create_chat_completion(request: Request):
    try:
        token, plugin_id = _authenticate(request)
        async with _RequestGuard(request, token) as guard:
            body = await _read_request(request)
            guard.watch_disconnect = True
            slot = await asyncio.to_thread(_resolve_slot, plugin_id, body.get("model"))
            if body.get("stream") is True:
                upstream = gateway_service.stream(slot, body)
                try:
                    first = await anext(upstream)
                    return _ModelStreamResponse(first, upstream, request, token)
                except BaseException:
                    await upstream.aclose()
                    raise
            return JSONResponse(await gateway_service.complete(slot, body))
    except ModelGatewayAccessError:
        return _error_response(_access_error())
    except ModelGatewayError as exc:
        return _error_response(exc)
    except ClientDisconnect:
        raise asyncio.CancelledError from None
