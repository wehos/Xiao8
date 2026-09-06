"""OpenAI SDK access to the host-managed plugin model gateway."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx

from plugin.sdk.shared.models.exceptions import CapabilityUnavailableError

if TYPE_CHECKING:
    from openai import AsyncOpenAI


class _TrackedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, client: _GatewayHttpClient):
        self._stream = stream
        self._client = client

    async def __aiter__(self):
        with self._client._request_scope():
            async for chunk in self._stream:
                yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _GatewayHttpClient(httpx.AsyncClient):
    """Track SDK I/O so context shutdown also cancels active streams."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._loop = asyncio.get_running_loop()
        self._requests: Counter[asyncio.Task[Any] | None] = Counter()

    @contextmanager
    def _request_scope(self):
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "Get a model client from ctx.models in the current event loop"
            )
        task = asyncio.current_task()
        self._requests[task] += 1
        try:
            yield
        finally:
            self._requests[task] -= 1
            if not self._requests[task]:
                del self._requests[task]

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        with self._request_scope():
            response = await super().send(request, **kwargs)
            if not response.is_closed:
                response.stream = _TrackedStream(response.stream, self)
            return response

    async def aclose(self) -> None:
        current = asyncio.current_task()
        pending = [
            task for task in self._requests if task is not None and task is not current
        ]
        for task in pending:
            task.cancel()
        # Do not join arbitrary plugin handlers: their cancellation cleanup may
        # itself call models.aclose(), which would create a shutdown deadlock.
        await super().aclose()


class PluginModels:
    """Provide one reusable official ``AsyncOpenAI`` client per event loop.

    ``model`` is a declared plugin usage name, resolved by the host to its
    bound slot. The client only holds an instance-scoped gateway token, never
    a provider credential. Host lifecycle wrappers must await ``aclose()``
    before closing their event loop; ``close()`` ends the whole capability.
    """

    def __init__(self, host_ctx: object) -> None:
        self._host_ctx = host_ctx
        self._clients: dict[asyncio.AbstractEventLoop, AsyncOpenAI] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._closing: set[asyncio.Task[Any]] = set()

    async def get_client(self) -> AsyncOpenAI:
        """Return a client for ``client.chat.completions.create(...)``.

        Network/HTTP errors use the official OpenAI SDK exception types.
        Missing host support and a closed plugin context fail locally with
        ``CapabilityUnavailableError``. No IPC requests are necessary.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            base_url = getattr(self._host_ctx, "_model_gateway_base_url", "")
            token = getattr(self._host_ctx, "_model_gateway_token", "")
            if (
                self._closed
                or getattr(self._host_ctx, "_model_gateway_closed", False)
                or not isinstance(base_url, str)
                or not base_url
                or not isinstance(token, str)
                or not token
            ):
                raise CapabilityUnavailableError(
                    "The plugin model gateway is unavailable for this context",
                    code="MODEL_GATEWAY_UNAVAILABLE",
                )
            client = self._clients.get(loop)
            if client is None or client.is_closed():
                from openai import AsyncOpenAI

                # The host owns the <=300s slot deadline. A larger transport
                # timeout lets its structured timeout response reach the SDK.
                client = AsyncOpenAI(
                    base_url=base_url,
                    api_key=token,
                    timeout=360.0,
                    max_retries=0,
                    http_client=_GatewayHttpClient(trust_env=False, timeout=360.0),
                )
                self._clients[loop] = client
            return client

    async def aclose(self) -> None:
        """Release this loop's client without disabling later lifecycle loops."""
        with self._lock:
            client = self._clients.pop(asyncio.get_running_loop(), None)
        if client is not None:
            await client.close()
        current = asyncio.current_task()
        if current in self._closing:
            return
        pending = [
            task
            for task in tuple(self._closing)
            if task is not current and task.get_loop() is asyncio.get_running_loop()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def close(self) -> None:
        """Disable new clients and schedule cleanup on each owning live loop.

        This is a synchronous shutdown safety net. Normal host cleanup awaits
        ``aclose()`` before asyncio.run returns, so no client is left attached
        to a stopped or closed event loop.
        """
        with self._lock:
            self._closed = True
            clients = tuple(self._clients.items())
        for loop, client in clients:
            if client.is_closed() or loop.is_closed():
                continue

            def schedule() -> None:
                task = asyncio.create_task(self.aclose())
                self._closing.add(task)
                task.add_done_callback(self._close_done)

            try:
                loop.call_soon_threadsafe(schedule)
            except RuntimeError:
                # A loop may finish between the liveness check and scheduling.
                # Do not close its async transport from a different loop.
                continue

    def _close_done(self, task: asyncio.Task[Any]) -> None:
        self._closing.discard(task)
        if not task.cancelled():
            task.exception()  # Consume cleanup failures in the shutdown fallback.


__all__ = ["PluginModels"]
