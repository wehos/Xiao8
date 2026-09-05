from __future__ import annotations

import ast
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from plugin.core import model_gateway_access as access_module
from plugin.core.model_gateway_access import (
    ModelGatewayAccessError,
    ModelGatewayAccessRegistry,
)


pytestmark = pytest.mark.plugin_unit


def test_tokens_identify_only_the_current_plugin_instance() -> None:
    registry = ModelGatewayAccessRegistry()
    old = registry.issue("alpha", lambda: True)
    other = registry.issue("beta", lambda: True)
    new = registry.issue("alpha", lambda: True)

    assert old != new
    assert len(new) >= 40
    assert registry.authenticate(new) == "alpha"
    assert registry.authenticate(other) == "beta"
    with pytest.raises(ModelGatewayAccessError):
        registry.authenticate(old)

    registry.revoke(old)
    assert registry.authenticate(new) == "alpha"
    registry.revoke(new)
    with pytest.raises(ModelGatewayAccessError):
        registry.authenticate(new)
    assert registry.authenticate(other) == "beta"


@pytest.mark.parametrize("alive", [False, RuntimeError("private-process-detail")])
def test_dead_or_uninspectable_process_is_revoked_without_secret_errors(alive) -> None:
    registry = ModelGatewayAccessRegistry()

    def is_alive() -> bool:
        if isinstance(alive, Exception):
            raise alive
        return alive

    token = registry.issue("alpha", is_alive)
    with pytest.raises(ModelGatewayAccessError) as exc:
        registry.authenticate(token)
    assert token not in repr(registry)
    assert token not in repr(exc.value)
    assert "private-process-detail" not in repr(exc.value)
    assert token not in registry._grants


def test_unknown_access_is_rejected() -> None:
    registry = ModelGatewayAccessRegistry()
    with pytest.raises(ModelGatewayAccessError):
        registry.authenticate("unknown-secret")
    registry.revoke("unknown-secret")


@pytest.mark.asyncio
async def test_cross_thread_revocation_cancels_on_the_task_event_loop() -> None:
    registry = ModelGatewayAccessRegistry()
    token = registry.issue("alpha", lambda: True)
    started = asyncio.Event()

    async def request() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(request())
    await started.wait()
    registry.track(token, task)
    registry.track(token, task)
    assert len(registry._grants[token].tasks) == 1
    await asyncio.to_thread(registry.revoke, token)

    with pytest.raises(asyncio.CancelledError):
        await task
    registry.untrack(token, task)


@pytest.mark.asyncio
async def test_new_instance_cancels_old_requests_without_cancelling_new_requests() -> None:
    registry = ModelGatewayAccessRegistry()
    old = registry.issue("alpha", lambda: True)
    old_task = asyncio.create_task(asyncio.Event().wait())
    registry.track(old, old_task)

    new = registry.issue("alpha", lambda: True)
    new_task = asyncio.create_task(asyncio.Event().wait())
    registry.track(new, new_task)
    registry.revoke(old)
    with pytest.raises(asyncio.CancelledError):
        await old_task
    assert not new_task.done()
    assert registry.authenticate(new) == "alpha"

    registry.revoke(new)
    with pytest.raises(asyncio.CancelledError):
        await new_task


@pytest.mark.asyncio
async def test_untracking_completed_request_excludes_it_from_revocation() -> None:
    registry = ModelGatewayAccessRegistry()
    token = registry.issue("alpha", lambda: True)
    task = asyncio.create_task(asyncio.Event().wait())
    registry.track(token, task)
    registry.untrack(token, task)
    registry.untrack(token, task)
    registry.revoke(token)
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_dead_process_authentication_cancels_already_active_requests() -> None:
    registry = ModelGatewayAccessRegistry()
    alive = True
    token = registry.issue("alpha", lambda: alive)
    task = asyncio.create_task(asyncio.Event().wait())
    registry.track(token, task)
    alive = False
    with pytest.raises(ModelGatewayAccessError):
        registry.authenticate(token)
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("operation", ["authenticate", "track"])
def test_liveness_callback_can_replace_instance_from_another_thread(operation) -> None:
    registry = ModelGatewayAccessRegistry()
    replacement: list[str] = []

    def replace() -> None:
        replacement.append(registry.issue("alpha", lambda: True))

    def is_alive() -> bool:
        # This fails rather than deadlocking forever if the callback is called
        # while holding the registry lock across the thread boundary.
        worker = threading.Thread(target=replace, daemon=True)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        return True

    old = registry.issue("alpha", is_alive)
    with pytest.raises(ModelGatewayAccessError):
        if operation == "track":
            registry.track(old, Mock())
        else:
            registry.authenticate(old)
    assert registry.authenticate(replacement[0]) == "alpha"
    assert not registry._grants[replacement[0]].tasks


def test_concurrent_issue_and_stale_revoke_leave_current_instance_usable() -> None:
    registry = ModelGatewayAccessRegistry()
    barrier = threading.Barrier(8)

    def restart() -> str:
        token = registry.issue("alpha", lambda: True)
        barrier.wait(timeout=3)
        registry.revoke(token)
        return token

    with ThreadPoolExecutor(max_workers=8) as executor:
        revoked = list(executor.map(lambda _: restart(), range(8)))
    current = registry.issue("alpha", lambda: True)
    for token in revoked:
        registry.revoke(token)
        with pytest.raises(ModelGatewayAccessError):
            registry.authenticate(token)
    assert registry.authenticate(current) == "alpha"


@pytest.mark.asyncio
async def test_revoke_all_cancels_requests_and_allows_fresh_service_start() -> None:
    registry = ModelGatewayAccessRegistry()
    tokens = [registry.issue(name, lambda: True) for name in ("alpha", "beta")]
    tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in tokens]
    for token, task in zip(tokens, tasks):
        registry.track(token, task)
    await asyncio.to_thread(registry.revoke_all)
    for token, task in zip(tokens, tasks):
        with pytest.raises(ModelGatewayAccessError):
            registry.authenticate(token)
        with pytest.raises(asyncio.CancelledError):
            await task
    registry.revoke_all()
    assert registry.authenticate(registry.issue("alpha", lambda: True)) == "alpha"


def test_closed_event_loop_does_not_break_revocation() -> None:
    registry = ModelGatewayAccessRegistry()
    token = registry.issue("alpha", lambda: True)
    task = Mock()
    task.get_loop.return_value.call_soon_threadsafe.side_effect = RuntimeError("closed")
    registry.track(token, task)
    registry.revoke(token)
    with pytest.raises(ModelGatewayAccessError):
        registry.authenticate(token)


def test_fork_scrub_clears_inherited_credentials_and_replaces_lock_without_cancelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ModelGatewayAccessRegistry()
    monkeypatch.setattr(access_module, "model_gateway_access", registry)
    token = registry.issue("alpha", lambda: True)
    task = Mock()
    registry.track(token, task)
    old_lock = registry._lock

    access_module._scrub_inherited_model_gateway_access()

    assert registry._lock is not old_lock
    assert not registry._grants
    assert not registry._plugin_tokens
    task.get_loop.assert_not_called()
    task.cancel.assert_not_called()
    with pytest.raises(ModelGatewayAccessError):
        registry.authenticate(token)
    assert registry.authenticate(registry.issue("beta", lambda: True)) == "beta"


def test_fork_hook_is_registered_even_when_tests_run_without_fork() -> None:
    tree = ast.parse(Path(access_module.__file__).read_text(encoding="utf-8"))
    registrations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_at_fork"
    ]
    assert any(
        keyword.arg == "after_in_child"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "_scrub_inherited_model_gateway_access"
        for call in registrations
        for keyword in call.keywords
    )
