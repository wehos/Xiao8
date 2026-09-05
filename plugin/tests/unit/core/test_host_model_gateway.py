from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugin.core import host as host_module


pytestmark = pytest.mark.plugin_unit


@pytest.fixture
def host_factory(monkeypatch, tmp_path):
    issued: dict[str, tuple[str, object]] = {}
    revoked: list[str] = []

    def issue(plugin_id, is_alive):
        token = f"instance-secret-{len(issued)}"
        issued[token] = (plugin_id, is_alive)
        return token

    monkeypatch.setattr(
        host_module, "model_gateway_access",
        SimpleNamespace(issue=issue, revoke=revoked.append),
    )
    monkeypatch.setattr(
        host_module, "HostTransport",
        lambda: SimpleNamespace(
            downlink_endpoint="ipc://down", uplink_endpoint="ipc://up",
            close=lambda: None,
        ),
    )
    monkeypatch.setattr(host_module.multiprocessing, "Event", lambda: SimpleNamespace(set=lambda: None))
    monkeypatch.setattr(host_module, "_refresh_child_storage_layout_env", lambda _logger: None)
    monkeypatch.setattr(host_module.state, "register_downlink_sender", lambda *_args: None)
    monkeypatch.setattr(host_module.state, "remove_downlink_sender", lambda *_args: None)
    for name in ("plugin_response_map", "plugin_response_notify_event"):
        monkeypatch.setattr(type(host_module.state), name, property(lambda _self: None))

    class Process:
        pid = 321
        exitcode = None

        def __init__(self, **kwargs):
            self.alive = False
            self.runner_kwargs = kwargs["kwargs"]
            self.start_error = None
            self.exit_immediately = False

        def start(self):
            self.launched_options = dict(self.runner_kwargs["model_gateway_options"])
            token = self.launched_options["token"]
            # A startup request is valid even before Process.start returns.
            assert issued[token][1]() is True
            if self.start_error:
                raise self.start_error
            self.alive = not self.exit_immediately

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.alive = False

    monkeypatch.setattr(host_module.multiprocessing, "Process", Process)

    def make(plugin_id="demo"):
        comm = SimpleNamespace(
            send_plugin_response=lambda: None,
            start=AsyncMock(), shutdown=AsyncMock(), send_stop_command=AsyncMock(),
            prepare_startup_wait=AsyncMock(),
            wait_for_startup=AsyncMock(return_value={"status": "ready"}),
            send_freeze_command=AsyncMock(return_value={"success": True}),
        )
        monkeypatch.setattr(host_module, "PluginCommunicationResourceManager", lambda **_kwargs: comm)
        return host_module.PluginHost(plugin_id, "plugins.demo:Plugin", tmp_path / "plugin.toml")

    return SimpleNamespace(make=make, issued=issued, revoked=revoked, process_type=Process)


@pytest.mark.asyncio
async def test_launch_delivers_private_instance_credential_and_actual_port(host_factory, monkeypatch):
    monkeypatch.setenv("NEKO_USER_PLUGIN_SERVER_PORT", "49001")
    plugin_host = host_factory.make()
    assert plugin_host._model_gateway_token == ""

    await plugin_host.start()

    options = plugin_host.process.launched_options
    token = options["token"]
    assert options["base_url"] == "http://127.0.0.1:49001/api/models/v1"
    assert host_factory.issued[token][0] == "demo"
    assert plugin_host._model_gateway_options == {}
    assert token not in host_module.os.environ.values()
    await plugin_host.start()
    assert len(host_factory.issued) == 1
    assert plugin_host._model_gateway_token == token


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["shutdown", "shutdown_sync", "freeze", "_abort_startup_after_failure"])
async def test_stop_revokes_only_this_instance_even_after_plugin_restarts(host_factory, stop):
    old_host = host_factory.make()
    await old_host.start()
    old_token = old_host._model_gateway_token
    new_host = host_factory.make()
    await new_host.start()
    new_token = new_host._model_gateway_token

    if stop == "shutdown_sync":
        old_host.shutdown_sync(timeout=0.01)
    else:
        await getattr(old_host, stop)(timeout=0.01)

    assert host_factory.revoked == [old_token]
    assert old_host._model_gateway_token == ""
    assert new_host._model_gateway_token == new_token
    assert host_factory.issued[new_token][1]() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["spawn", "immediate_exit", "startup_error", "timeout", "cancel"])
async def test_failed_start_revokes_launch_credential(host_factory, failure):
    plugin_host = host_factory.make()
    if failure == "spawn":
        plugin_host.process.start_error = RuntimeError("spawn failed")
    elif failure == "immediate_exit":
        plugin_host.process.exit_immediately = True
    else:
        plugin_host.comm_manager.wait_for_startup.side_effect = {
            "startup_error": RuntimeError("startup failed"),
            "timeout": TimeoutError(),
            "cancel": asyncio.CancelledError(),
        }[failure]

    expected = asyncio.CancelledError if failure == "cancel" else Exception
    with pytest.raises(expected):
        await plugin_host.start(startup_timeout=0.01, startup_failure="fail")

    assert host_factory.revoked == [plugin_host.process.launched_options["token"]]
    assert plugin_host._model_gateway_token == ""
    assert plugin_host._model_gateway_options == {}
    assert plugin_host._model_gateway_starting is False


@pytest.mark.asyncio
@pytest.mark.parametrize("spawn_fails", [False, True])
async def test_cancel_during_spawn_keeps_launch_options_until_worker_finishes(
    host_factory, monkeypatch, spawn_fails,
):
    entered = threading.Event()
    release = threading.Event()
    revoked = asyncio.Event()
    events: list[str] = []

    class ControlledProcess(host_factory.process_type):
        def start(self):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test did not release spawn worker")
            try:
                super().start()
            finally:
                events.append("spawn_finished")

        def join(self, timeout):
            super().join(timeout)
            events.append("process_stopped")

    def revoke(token):
        host_factory.revoked.append(token)
        revoked.set()

    monkeypatch.setattr(host_module.multiprocessing, "Process", ControlledProcess)
    monkeypatch.setattr(host_module.model_gateway_access, "revoke", revoke)
    plugin_host = host_factory.make()
    plugin_host.transport.close = lambda: events.append("transport_closed")
    if spawn_fails:
        plugin_host.process.start_error = RuntimeError("spawn failed")
    task = asyncio.create_task(plugin_host.start())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        token = plugin_host._model_gateway_token
        task.cancel()
        await asyncio.wait_for(revoked.wait(), timeout=2)
        # A second caller cancellation must not cancel the protected teardown.
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert plugin_host._model_gateway_options["token"] == token
        assert events == []

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert plugin_host.process.launched_options["token"] == token
    assert plugin_host.process.is_alive() is False
    assert host_factory.revoked == [token]
    assert plugin_host._model_gateway_options == {}
    assert plugin_host._model_gateway_starting is False
    expected = ["spawn_finished", "transport_closed"]
    if not spawn_fails:
        expected.insert(1, "process_stopped")
    assert events == expected


@pytest.mark.asyncio
async def test_observing_crash_revokes_credential(host_factory):
    plugin_host = host_factory.make()
    await plugin_host.start()
    token = plugin_host._model_gateway_token
    plugin_host.process.alive = False
    plugin_host.process.exitcode = 1

    assert host_factory.issued[token][1]() is False
    assert plugin_host.is_alive() is False
    assert host_factory.revoked == [token]


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
def test_model_client_closes_on_its_owner_loop_before_loop_shutdown(failure):
    recorded = []

    async def close():
        recorded.append(("close", asyncio.get_running_loop()))

    async def work():
        recorded.append(("work", asyncio.get_running_loop()))
        if failure:
            raise failure()
        return 42

    ctx = SimpleNamespace(_models=SimpleNamespace(aclose=close))
    for _ in range(2):
        if failure:
            with pytest.raises(failure):
                asyncio.run(host_module._run_with_model_client_cleanup(ctx, work()))
        else:
            assert asyncio.run(host_module._run_with_model_client_cleanup(ctx, work())) == 42
    assert [event for event, _loop in recorded] == ["work", "close", "work", "close"]
    assert recorded[0][1] is recorded[1][1]
    assert recorded[2][1] is recorded[3][1]
    assert recorded[0][1] is not recorded[2][1]
    assert all(loop.is_closed() for _event, loop in recorded)


def test_model_client_cleanup_failure_preserves_plugin_error():
    async def close():
        raise RuntimeError("close failed")

    async def work():
        raise ValueError("plugin failed")

    ctx = SimpleNamespace(_models=SimpleNamespace(aclose=close))
    with pytest.raises(ValueError, match="plugin failed"):
        asyncio.run(host_module._run_with_model_client_cleanup(ctx, work()))
