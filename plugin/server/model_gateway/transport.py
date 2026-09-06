"""Bounded JSON/SSE transport helpers; never log upstream response bodies."""
from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator

import httpx

from .errors import ModelGatewayError

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SSE_EVENT_BYTES = 1024 * 1024


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON number")


def decode_object(data: str | bytes) -> dict:
    try:
        value = json.loads(data, parse_constant=_reject_constant)
        # JSON escapes can decode to lone surrogates, and 1e999 to infinity.
        # Reject both before a response serializer encounters them downstream.
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Non-finite JSON number")
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ModelGatewayError("invalid_upstream_response", "Model provider returned invalid JSON", 502) from exc
    if not isinstance(value, dict):
        raise ModelGatewayError("invalid_upstream_response", "Model provider returned a non-object response", 502)
    return value


async def read_json_response(response: httpx.Response) -> dict:
    chunks = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ModelGatewayError("upstream_response_too_large", "Model response exceeded the size limit", 502)
        chunks.append(chunk)
    return await asyncio.to_thread(decode_object, b"".join(chunks))


async def _bounded_lines(response: httpx.Response) -> AsyncIterator[str]:
    """Bound bytes before buffering a line, including peers that never send LF."""
    pending = bytearray()
    previous_cr = False
    total_size = 0
    async for chunk in response.aiter_bytes():
        total_size += len(chunk)
        if total_size > MAX_RESPONSE_BYTES:
            raise ModelGatewayError("upstream_response_too_large", "Model event stream exceeded the size limit", 502)
        for part in re.split(br"([\r\n])", chunk):
            if part not in (b"\r", b"\n"):
                if part:
                    previous_cr = False
                if len(pending) + len(part) > MAX_SSE_EVENT_BYTES:
                    raise ModelGatewayError("upstream_response_too_large", "Model event stream exceeded the size limit", 502)
                pending.extend(part)
                continue
            if part == b"\n" and previous_cr:
                previous_cr = False
                continue
            previous_cr = part == b"\r"
            try:
                line = pending.decode("utf-8")
            except UnicodeError as exc:
                raise ModelGatewayError("invalid_upstream_response", "Model stream is not valid UTF-8", 502) from exc
            pending.clear()
            yield line
    if pending:
        try:
            yield pending.decode("utf-8")
        except UnicodeError as exc:
            raise ModelGatewayError("invalid_upstream_response", "Model stream is not valid UTF-8", 502) from exc


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Decode SSE comments, multiline data and errors without buffering replies."""
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/event-stream":
        raise ModelGatewayError("invalid_upstream_response", "Model provider did not return an event stream", 502)
    data: list[str] = []
    event_type = ""
    event_size = 0
    first_line = True
    async for line in _bounded_lines(response):
        if first_line:
            line = line.removeprefix("\ufeff")
            first_line = False
        line_size = len(line.encode("utf-8")) + 1
        event_size += line_size
        if event_size > MAX_SSE_EVENT_BYTES:
            raise ModelGatewayError("upstream_response_too_large", "Model event stream exceeded the size limit", 502)
        if not line:
            if event_type == "error":
                raise ModelGatewayError("upstream_error", "Model provider reported a stream error", 502)
            if data:
                yield "\n".join(data)
            data = []
            event_type = ""
            event_size = 0
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "data":
            data.append(value)
        elif field == "event":
            event_type = value
    if event_type == "error":
        raise ModelGatewayError("upstream_error", "Model provider reported a stream error", 502)
    if data:
        yield "\n".join(data)


def encode_sse(chunk: dict) -> bytes:
    try:
        return ("data: " + json.dumps(chunk, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ModelGatewayError("invalid_upstream_response", "Model event could not be serialized", 502) from exc
