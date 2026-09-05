"""Process-local credentials shared by the plugin host and model HTTP service.

No supplier credentials are stored here. Each token identifies one running
plugin instance; restarting that plugin invalidates its previous requests.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field


class ModelGatewayAccessError(Exception):
    """The requesting plugin instance no longer has model gateway access."""


@dataclass(slots=True, repr=False)
class _Grant:
    plugin_id: str
    is_alive: Callable[[], bool]
    tasks: set[asyncio.Task] = field(default_factory=set)


class ModelGatewayAccessRegistry:
    """Thread-safe runtime grants, independent of any server event loop."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._grants: dict[str, _Grant] = {}
        self._plugin_tokens: dict[str, str] = {}

    def issue(self, plugin_id: str, is_alive: Callable[[], bool]) -> str:
        """Replace the plugin's prior instance grant and cancel its requests."""
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ValueError("plugin_id must be a non-empty string")
        if not callable(is_alive):
            raise TypeError("is_alive must be callable")
        token = secrets.token_urlsafe(32)
        with self._lock:
            old_token = self._plugin_tokens.get(plugin_id)
            old_grant = self._grants.pop(old_token, None)
            self._grants[token] = _Grant(plugin_id, is_alive)
            self._plugin_tokens[plugin_id] = token
            tasks = tuple(old_grant.tasks) if old_grant is not None else ()
        self._cancel_tasks(tasks)
        return token

    def revoke(self, token: str) -> None:
        """Revoke precisely this instance, leaving a newer instance intact."""
        with self._lock:
            grant = self._grants.pop(token, None)
            if grant is None:
                return
            if self._plugin_tokens.get(grant.plugin_id) == token:
                del self._plugin_tokens[grant.plugin_id]
            tasks = tuple(grant.tasks)
        self._cancel_tasks(tasks)

    def authenticate(self, token: str) -> str:
        """Resolve an active instance without exposing credentials in errors."""
        return self._get_live_grant(token).plugin_id

    def track(self, token: str, task: asyncio.Task) -> None:
        """Register a request, rechecking revocation after its liveness check."""
        grant = self._get_live_grant(token)
        with self._lock:
            if self._grants.get(token) is not grant:
                raise ModelGatewayAccessError("Plugin model access is unavailable")
            grant.tasks.add(task)

    def untrack(self, token: str, task: asyncio.Task) -> None:
        with self._lock:
            grant = self._grants.get(token)
            if grant is not None:
                grant.tasks.discard(task)

    def revoke_all(self) -> None:
        """Cancel requests and discard credentials during service shutdown."""
        with self._lock:
            tasks = {task for grant in self._grants.values() for task in grant.tasks}
            self._grants.clear()
            self._plugin_tokens.clear()
        self._cancel_tasks(tasks)

    def _get_live_grant(self, token: str) -> _Grant:
        with self._lock:
            grant = self._grants.get(token)
        if grant is None:
            raise ModelGatewayAccessError("Plugin model access is unavailable")
        # Process.is_alive() and user callbacks must never run under the lock:
        # the plugin host and HTTP service may enter from different threads.
        try:
            alive = bool(grant.is_alive())
        except Exception:
            alive = False
        with self._lock:
            if self._grants.get(token) is not grant:
                raise ModelGatewayAccessError("Plugin model access is unavailable")
            if alive:
                return grant
            del self._grants[token]
            if self._plugin_tokens.get(grant.plugin_id) == token:
                del self._plugin_tokens[grant.plugin_id]
            tasks = tuple(grant.tasks)
        self._cancel_tasks(tasks)
        raise ModelGatewayAccessError("Plugin model access is unavailable")

    @staticmethod
    def _cancel_tasks(tasks: tuple[asyncio.Task, ...] | set[asyncio.Task]) -> None:
        for task in tasks:
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # Its event loop may already have been closed by shutdown.
                pass

    def _reset_after_fork(self) -> None:
        # A parent thread may have held the inherited lock when fork ran. Do
        # not acquire it, import modules, or cancel copied parent-loop tasks.
        self._lock = threading.RLock()
        self._grants.clear()
        self._plugin_tokens.clear()


model_gateway_access = ModelGatewayAccessRegistry()


def _scrub_inherited_model_gateway_access() -> None:
    model_gateway_access._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_scrub_inherited_model_gateway_access)


__all__ = [
    "ModelGatewayAccessError",
    "ModelGatewayAccessRegistry",
    "model_gateway_access",
]
