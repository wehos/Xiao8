"""Bounded plugin requests with one deadline, one fallback and one ledger entry."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import anyio

from plugin.logging_config import get_logger
from plugin.server.domain.model_config import ModelSlot

from .errors import ModelGatewayError
from .observation import AttemptObservation

_FALLBACK_ERRORS = frozenset({
    "upstream_connection_error", "upstream_timeout", "upstream_rate_limited", "upstream_error",
})
logger = get_logger("server.model_gateway.execution")


@dataclass(frozen=True)
class ResolvedModelCall:
    """One host-resolved snapshot; no configuration reads during execution."""

    plugin_id: str
    usage_id: str
    slot_id: str
    slot: ModelSlot
    fallback_slot_id: str | None = None
    fallback_slot: ModelSlot | None = None
    deadline: float | None = None


@dataclass
class _StreamState:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    delivered: bool = False
    terminal: bytes | None = None

    async def emit(self, chunk: bytes) -> None:
        if chunk == b"data: [DONE]\n\n":
            self.terminal = chunk
            return
        if chunk:
            await self.queue.put(chunk)
            self.ready.set()

    def discard_pending(self) -> None:
        self.terminal = None
        while not self.queue.empty():
            self.queue.get_nowait()


def _timeout_error() -> ModelGatewayError:
    return ModelGatewayError("gateway_timeout", "Plugin model request exceeded its total time budget", 504)


def _failure(exc: BaseException, *, expired: bool = False) -> tuple[str, str | None]:
    if expired or isinstance(exc, TimeoutError):
        return "timeout", "gateway_timeout"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled", "request_cancelled"
    if isinstance(exc, ModelGatewayError):
        return ("timeout" if exc.code in {"gateway_timeout", "upstream_timeout"} else "error"), exc.code
    return "error", "model_gateway_error"


async def _cancel_and_wait(task: asyncio.Task) -> None:
    # Do not inject a second cancellation while the task closes HTTP resources
    # and writes its ledger entry. ASGI may cancel the caller repeatedly.
    if not task.done() and not task.cancelling():
        task.cancel()
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with suppress(asyncio.CancelledError, Exception):
            task.result()


async def _record_safely(recorder, request: dict) -> None:
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(recorder.record_request(request))
        # A direct task.cancel() is not blocked by an AnyIO shield. Keep the one
        # write alive and wait for it rather than starting a replacement write.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except (asyncio.CancelledError, Exception) as exc:
            logger.warning("Plugin model accounting failed ({})", type(exc).__name__)


class ModelExecutor:
    def __init__(self, gateway, recorder, *, max_active: int = 4, max_waiting: int = 16):
        if type(max_active) is not int or max_active < 1 or type(max_waiting) is not int or max_waiting < 0:
            raise ValueError("Model concurrency limits must be positive active and nonnegative waiting integers")
        self.gateway = gateway
        self.recorder = recorder
        self._max_active = max_active
        self._capacity = max_active + max_waiting
        self._loop = None
        self._semaphore = None
        self._admitted = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def _start(self, call: ResolvedModelCall, body: object, stream: _StreamState | None = None) -> asyncio.Task:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(self._max_active)
        elif self._loop is not loop:
            raise RuntimeError("A model executor belongs to one event loop")
        # Capture time before scheduling: queueing is part of this request budget.
        started = loop.time()
        started_at = datetime.now(timezone.utc).timestamp()
        task = asyncio.create_task(self._run(call, body, started, started_at, stream))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if stream is not None:
            task.add_done_callback(lambda _: stream.ready.set())
        return task

    async def complete(self, call: ResolvedModelCall, body: object) -> dict:
        task = self._start(call, body)
        try:
            return await asyncio.shield(task)
        finally:
            await _cancel_and_wait(task)

    async def stream(self, call: ResolvedModelCall, body: object) -> AsyncIterator[bytes]:
        state = _StreamState()
        task = self._start(call, body, state)
        try:
            while True:
                # Reading and marking delivery have no intervening await. The
                # producer can safely discard unobserved primary chunks before
                # fallback, even when route prefetch and ASGI use different tasks.
                if not state.queue.empty():
                    chunk = state.queue.get_nowait()
                    state.delivered = True
                    yield chunk
                elif task.done():
                    await asyncio.shield(task)
                    # The SDK may immediately close after DONE. Deliver it only
                    # once the upstream and its ledger cleanup really completed.
                    if state.terminal is not None:
                        yield state.terminal
                    return
                else:
                    state.ready.clear()
                    await state.ready.wait()
        finally:
            await _cancel_and_wait(task)

    async def aclose(self) -> None:
        self._closed = True
        with anyio.CancelScope(shield=True):
            # Let scheduled runners enter their try/finally. Cancelling before
            # the first instruction would bypass accounting; these requests now
            # reject shutdown without starting any upstream work.
            await asyncio.sleep(0)
            tasks = list(self._tasks)
            # Cancel all upstreams before waiting for any individual cleanup.
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            for task in tasks:
                await _cancel_and_wait(task)

    async def _run(self, call, body, started, started_at, stream):
        loop = asyncio.get_running_loop()
        attempts = []
        request = {
            "request_id": uuid4().hex,
            "plugin_id": call.plugin_id,
            "usage_id": call.usage_id,
            "slot_id": call.slot_id,
            "started_at": started_at,
            "status": "success",
            "error_code": None,
            "attempts": attempts,
        }
        admitted = False
        try:
            if self._closed:
                raise ModelGatewayError("model_gateway_unavailable", "Plugin model gateway is shutting down", 503)
            if self._admitted >= self._capacity:
                raise ModelGatewayError("model_gateway_busy", "Plugin model gateway request capacity is full", 429)
            self._admitted += 1
            admitted = True
            deadline = asyncio.timeout_at(call.deadline if call.deadline is not None else started + call.slot.timeout_seconds)
            try:
                async with deadline:
                    async with self._semaphore:
                        return await self._attempts(call, body, attempts, deadline, stream)
            except TimeoutError as exc:
                raise _timeout_error() from exc
        except BaseException as exc:
            request["status"], request["error_code"] = _failure(exc)
            raise
        finally:
            if admitted:
                self._admitted -= 1
            request["duration_ms"] = round((loop.time() - started) * 1000, 3)
            # Metrics cannot change the model result or prevent resource release.
            # Each owned execution task reaches this once, including cancellation.
            await _record_safely(self.recorder, request)

    async def _attempts(self, call, body, attempts, deadline, stream):
        slot_id, slot = call.slot_id, call.slot
        for index in range(2):
            try:
                return await self._attempt(slot_id, slot, body, attempts, deadline, stream)
            except ModelGatewayError as exc:
                if (
                    index != 0 or exc.code not in _FALLBACK_ERRORS
                    or call.fallback_slot is None or call.fallback_slot_id is None
                    or (stream is not None and stream.delivered)
                ):
                    raise
                if not set(slot.capabilities).issubset(call.fallback_slot.capabilities):
                    raise ModelGatewayError(
                        "model_capability_mismatch", "Fallback slot does not meet the bound slot capabilities", 409, "model",
                    ) from None
                if stream is not None:
                    stream.discard_pending()
                # The lower service prepares the original request again against
                # the fallback slot, including protocol-specific validation.
                slot_id, slot = call.fallback_slot_id, call.fallback_slot

    async def _attempt(self, slot_id, slot, body, attempts, deadline, stream):
        loop = asyncio.get_running_loop()
        started = loop.time()
        observation = AttemptObservation()
        attempt = {
            "attempt_id": uuid4().hex,
            "slot_id": slot_id,
            "protocol": slot.protocol,
            "model": slot.model,
            "status": "success",
            "error_code": None,
        }
        attempts.append(attempt)
        try:
            if stream is None:
                return await self.gateway.complete(slot, body, observation=observation)
            upstream = self.gateway.stream(slot, body, observation=observation)
            try:
                async for chunk in upstream:
                    await stream.emit(chunk)
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
        except BaseException as exc:
            attempt["status"], attempt["error_code"] = _failure(exc, expired=deadline.expired())
            raise
        finally:
            attempt.update({
                "duration_ms": round((loop.time() - started) * 1000, 3),
                "upstream_started": observation.upstream_started,
                "usage_status": observation.usage_status,
                "usage": observation.usage,
            })
