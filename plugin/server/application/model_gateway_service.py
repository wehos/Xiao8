"""Internal model execution against one already-resolved plugin slot.

There is deliberately no HTTP route here. The authenticated plugin gateway will
resolve a binding before invoking this service. Total deadlines, fallback and
accounting are added around this single-attempt boundary in a later increment.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import anyio
import httpx

from plugin.server.domain.model_config import ModelSlot
from plugin.server.model_gateway import anthropic, openai
from plugin.server.model_gateway.errors import ModelGatewayError, upstream_error
from plugin.server.model_gateway.request import prepare_chat_request
from plugin.server.model_gateway.transport import decode_object, encode_sse, iter_sse_data, read_json_response
from utils.http_client import ensure_user_agent

MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _endpoint(slot: ModelSlot) -> str:
    base = slot.base_url.rstrip("/")
    if slot.protocol == "openai_chat":
        return base + "/chat/completions"
    # Anthropic's SDK uses /v1/messages; accept a base with or without /v1.
    return base + ("/messages" if base.endswith("/v1") else "/v1/messages")


def _headers(slot: ModelSlot) -> dict[str, str]:
    try:
        slot.api_key.encode("ascii")
    except UnicodeError as exc:
        raise ModelGatewayError("invalid_model_configuration", "Configured API key is not a valid HTTP credential", 500) from exc
    headers = ensure_user_agent({"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    if slot.protocol == "anthropic_messages":
        headers["anthropic-version"] = "2023-06-01"
        if slot.api_key:
            headers["x-api-key"] = slot.api_key
    elif slot.api_key:
        headers["Authorization"] = "Bearer " + slot.api_key
    return headers


def _check_status(response: httpx.Response) -> None:
    if not response.is_success:
        raise upstream_error(response.status_code)


def _prepare(slot: ModelSlot, body: object, *, streaming: bool) -> tuple[str, bytes, dict, bool]:
    """Validate, convert and encode once, off the HTTP event loop."""
    request = prepare_chat_request(slot, body)
    try:
        httpx.URL(_endpoint(slot))
    except (httpx.InvalidURL, ValueError, UnicodeError) as exc:
        raise ModelGatewayError("invalid_model_configuration", "Configured model endpoint is not a valid HTTP URL", 500) from exc
    if bool(request.get("stream")) != streaming:
        raise ModelGatewayError("invalid_request", "Request stream flag does not match the execution path", param="stream")
    include_usage = request.get("stream_options", {}).get("include_usage", False)
    if slot.protocol == "anthropic_messages":
        payload = anthropic.prepare_request(request)
    elif streaming:
        payload = {**request, "stream_options": {"include_usage": True}}
    else:
        payload = request
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ModelGatewayError("invalid_request", "Request is not valid UTF-8 JSON") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ModelGatewayError("request_too_large", "Model request exceeded the size limit", 413)
    return body["model"], encoded, _headers(slot), include_usage


class ModelGatewayService:
    def __init__(self, client_factory: Callable[[ModelSlot], httpx.AsyncClient] | None = None):
        self._client_factory = client_factory or self._make_client

    @staticmethod
    def _make_client(slot: ModelSlot) -> httpx.AsyncClient:
        # HTTP-level inactivity guard. A total request deadline is owned by the
        # execution policy, not by httpx's connect/read/write/pool timeouts.
        return httpx.AsyncClient(timeout=slot.timeout_seconds, follow_redirects=False)

    @asynccontextmanager
    async def _request(self, slot: ModelSlot, payload: bytes, headers: dict):
        client = self._client_factory(slot)
        response = None
        try:
            request = client.build_request("POST", _endpoint(slot), content=payload, headers=headers)
            response = await client.send(request, stream=True, follow_redirects=False)
            _check_status(response)
            yield response
        finally:
            # StreamingResponse uses an AnyIO cancel scope. Without shielding,
            # repeated cancellation can interrupt async HTTP close halfway.
            with anyio.CancelScope(shield=True):
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    await client.aclose()

    async def complete(self, slot: ModelSlot, body: object) -> dict:
        slot = slot.model_copy(deep=True)
        model_alias, payload, headers, _ = await asyncio.to_thread(_prepare, slot, body, streaming=False)
        try:
            async with self._request(slot, payload, headers) as response:
                result = await read_json_response(response)
            adapter = anthropic if slot.protocol == "anthropic_messages" else openai
            return await asyncio.to_thread(adapter.convert_response, result, model_alias=model_alias)
        except httpx.TimeoutException as exc:
            raise ModelGatewayError("upstream_timeout", "Model provider request timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError("upstream_connection_error", "Could not complete the model provider request", 502) from exc

    async def stream(self, slot: ModelSlot, body: object) -> AsyncIterator[bytes]:
        slot = slot.model_copy(deep=True)
        model_alias, payload, headers, include_usage = await asyncio.to_thread(_prepare, slot, body, streaming=True)
        is_anthropic = slot.protocol == "anthropic_messages"
        if is_anthropic:
            converter = anthropic.AnthropicStreamConverter(model_alias, include_usage=include_usage)
        else:
            converter = openai.OpenAIStreamConverter(model_alias, include_usage=include_usage)
        saw_done = False
        try:
            async with self._request(slot, payload, headers) as response:
                async for data in iter_sse_data(response):
                    if data.strip() == "[DONE]":
                        if is_anthropic:
                            raise ModelGatewayError("invalid_upstream_response", "Unexpected stream terminator", 502)
                        saw_done = True
                        break
                    for chunk in converter.feed(decode_object(data)):
                        yield encode_sse(chunk)
                    if is_anthropic and converter.done:
                        break
                converter.finish()
                if not is_anthropic and not saw_done:
                    raise ModelGatewayError("incomplete_upstream_stream", "Model stream ended without a terminator", 502)
            yield b"data: [DONE]\n\n"
        except httpx.TimeoutException as exc:
            raise ModelGatewayError("upstream_timeout", "Model provider stream timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError("upstream_connection_error", "Model provider stream disconnected", 502) from exc
