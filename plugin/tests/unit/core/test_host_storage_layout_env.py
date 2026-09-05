from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.core import host as host_module
from plugin.sdk.shared.core.events import EventHandler, EventMeta
from utils import storage_layout as storage_layout_module


class _FakeCommManager:
    def send_plugin_response(self, *args, **kwargs) -> None:
        self.response = (args, kwargs)

    async def start(self, message_target_queue=None) -> None:
        self.message_target_queue = message_target_queue

    async def shutdown(self, timeout: float) -> None:
        self.shutdown_timeout = timeout

    async def send_stop_command(self) -> None:
        self.stop_sent = True


class _StartupErrorCommManager(_FakeCommManager):
    async def prepare_startup_wait(self) -> None:
        self.startup_wait_prepared = True

    async def wait_for_startup(self, timeout: float, allow_startup_error: bool = False) -> dict[str, object]:
        self.startup_timeout = timeout
        self.allow_startup_error = allow_startup_error
        if allow_startup_error:
            return {"status": "failed", "startup_error": "lifecycle.startup failed"}
        raise RuntimeError("lifecycle.startup failed")


class _FakeProcess:
    pid = 1234
    exitcode = None

    def __init__(self) -> None:
        self.started = False

    def is_alive(self) -> bool:
        return self.started

    def start(self) -> None:
        self.started = True


@pytest.mark.plugin_unit
def test_plugin_host_preinitializes_response_proxies_on_the_default_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    accessed_response_proxies: list[str] = []
    default_event = object()
    default_process = _FakeProcess()

    class _FakeTransport:
        downlink_endpoint = "ipc://down"
        uplink_endpoint = "ipc://up"

    def _reject_explicit_context(method: str) -> object:
        raise AssertionError(
            f"PluginHost must use the default start method, not {method!r}"
        )

    monkeypatch.setattr(host_module, "HostTransport", _FakeTransport)
    monkeypatch.setattr(host_module, "PluginCommunicationResourceManager", lambda **_kwargs: _FakeCommManager())
    monkeypatch.setattr(
        type(host_module.state),
        "plugin_response_map",
        property(lambda _self: accessed_response_proxies.append("map")),
    )
    monkeypatch.setattr(
        type(host_module.state),
        "plugin_response_notify_event",
        property(lambda _self: accessed_response_proxies.append("event")),
    )
    monkeypatch.setattr(host_module.multiprocessing, "get_context", _reject_explicit_context)
    monkeypatch.setattr(host_module.multiprocessing, "Event", lambda: default_event)
    monkeypatch.setattr(host_module.multiprocessing, "Process", lambda **_kwargs: default_process)

    plugin_host = host_module.PluginHost(
        plugin_id="demo",
        entry_point="plugins.demo:DemoPlugin",
        config_path=tmp_path / "demo" / "plugin.toml",
    )

    # Both proxies must be materialised in the parent: on a fork start method
    # a child that touches them first builds its own Manager proxies instead.
    assert accessed_response_proxies == ["map", "event"]
    assert plugin_host._process_stop_event is default_event
    assert plugin_host.process is default_process


class _FakeLogger:
    def info(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass

    def debug(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def exception(self, *_args, **_kwargs) -> None:
        pass


class _FakeResponseSender:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def put(self, payload: dict[str, object], timeout: float) -> None:
        self.payloads.append(payload)
        self.timeout = timeout


@pytest.mark.plugin_unit
def test_plugin_process_runner_rejects_missing_uplink_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "demo" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo'\ntype='plugin'\n", encoding="utf-8")
    monkeypatch.setattr(
        host_module,
        "_setup_plugin_logger",
        lambda *args, **kwargs: _FakeLogger(),
    )

    with pytest.raises(ValueError, match="uplink token"):
        host_module._plugin_process_runner(
            plugin_id="demo",
            entry_point="tests.fake:DemoPlugin",
            config_path=config_path,
            downlink_endpoint="ipc://down",
            uplink_endpoint="ipc://up",
        )


@pytest.mark.plugin_unit
def test_plugin_router_entry_closes_context_and_transport_before_returning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed: list[str] = []
    payloads: list[dict[str, object]] = []
    contexts: list[dict[str, object]] = []
    config_path = tmp_path / "demo" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo'\ntype='plugin'\n", encoding="utf-8")

    class _Router(host_module.PluginRouter):
        pass

    class _Sender:
        def put_nowait(self, payload: dict[str, object]) -> None:
            payloads.append(payload)

    class _ChildTransport:
        def __init__(
            self,
            downlink_endpoint: str,
            uplink_endpoint: str,
            uplink_token: str,
        ) -> None:
            del downlink_endpoint, uplink_endpoint, uplink_token

        def channel_sender(self, channel: str) -> _Sender:
            del channel
            return _Sender()

        def close(self) -> None:
            closed.append("transport")

    class _Context:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)
            contexts.append(kwargs)

        def close(self) -> None:
            closed.append("context")

    monkeypatch.setenv("NEKO_PLUGIN_ZMQ_IPC_ENABLED", "0")
    monkeypatch.setattr(host_module, "_setup_plugin_logger", lambda *args, **kwargs: _FakeLogger())
    monkeypatch.setattr(host_module, "_setup_logging_interception", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_plugin_import_roots", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_current_plugin_import_root", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_plugin_vendor_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_import_plugin_module", lambda *args, **kwargs: SimpleNamespace(DemoRouter=_Router))
    monkeypatch.setattr(host_module, "ChildTransport", _ChildTransport)
    monkeypatch.setattr(host_module, "PluginContext", _Context)

    model_gateway_options = {
        "base_url": "http://127.0.0.1:49001/api/models/v1",
        "token": "this-instance-token",
    }
    host_module._plugin_process_runner(
        plugin_id="demo",
        entry_point="tests.fake:DemoRouter",
        config_path=config_path,
        downlink_endpoint="ipc://down",
        uplink_endpoint="ipc://up",
        uplink_token="test-uplink-token",
        model_gateway_options=model_gateway_options,
    )

    assert closed == ["context", "transport"]
    assert payloads[-1]["status"] == "error"
    assert contexts[0]["_model_gateway_base_url"] == "http://127.0.0.1:49001/api/models/v1"
    assert contexts[0]["_model_gateway_token"] == "this-instance-token"
    assert model_gateway_options == {}


@pytest.mark.plugin_unit
def test_plugin_process_runner_sends_startup_ready_before_auto_custom_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    payloads: list[dict[str, object]] = []
    config_path = tmp_path / "demo" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo'\ntype='adapter'\n", encoding="utf-8")

    startup_meta = EventMeta(event_type="lifecycle", id="startup")
    auto_meta = EventMeta(
        event_type="custom_demo",
        id="boot",
        auto_start=True,
        extra={"trigger_method": "auto"},
    )

    async def _startup() -> None:
        order.append("startup")

    async def _auto_custom() -> None:
        order.append("auto_custom")

    setattr(_startup, host_module.EVENT_META_ATTR, startup_meta)
    setattr(_auto_custom, host_module.EVENT_META_ATTR, auto_meta)

    class _Plugin:
        def __init__(self, ctx) -> None:
            self.ctx = ctx
            self.config = SimpleNamespace(dump_effective_sync=lambda timeout=3.0: {})

        def collect_entries(self, wrap_with_hooks: bool = True) -> dict[str, EventHandler]:
            return {
                "startup": EventHandler(meta=startup_meta, handler=_startup),
                "boot": EventHandler(meta=auto_meta, handler=_auto_custom),
            }

        async def _on_command_loop_start(self) -> None:
            order.append(self.ctx.handler_ctx)

    class _Sender:
        def __init__(self, channel: str) -> None:
            self.channel = channel

        def put(self, payload: dict[str, object], block: bool = True, timeout: float | None = None) -> None:
            payloads.append(payload)
            if (
                self.channel == host_module.CH_RES
                and payload.get("req_id") == host_module.STARTUP_RESULT_REQ_ID
            ):
                order.append("ready")

        def put_nowait(self, payload: dict[str, object]) -> None:
            self.put(payload)

    class _ChildTransport:
        def __init__(
            self,
            downlink_endpoint: str,
            uplink_endpoint: str,
            uplink_token: str,
        ) -> None:
            del downlink_endpoint, uplink_endpoint, uplink_token
            self.stopped = False

        def channel_sender(self, channel: str) -> _Sender:
            return _Sender(channel)

        async def recv_downlink(self, timeout_ms: int = 1000):
            if not self.stopped:
                self.stopped = True
                return (host_module.CH_CMD, {"type": "STOP"})
            return None

        def close(self) -> None:
            return

    class _ImmediateThread:
        def __init__(self, target, args=(), kwargs=None, daemon: bool | None = None) -> None:
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self) -> None:
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(host_module, "_setup_plugin_logger", lambda *args, **kwargs: _FakeLogger())
    monkeypatch.setattr(host_module, "_setup_logging_interception", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_plugin_import_roots", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_current_plugin_import_root", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_plugin_vendor_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_import_plugin_module", lambda *args, **kwargs: SimpleNamespace(DemoPlugin=_Plugin))
    monkeypatch.setattr(host_module, "ChildTransport", _ChildTransport)
    monkeypatch.setattr(host_module.threading, "Thread", _ImmediateThread)

    host_module._plugin_process_runner(
        plugin_id="demo",
        entry_point="tests.fake:DemoPlugin",
        config_path=config_path,
        downlink_endpoint="ipc://down",
        uplink_endpoint="ipc://up",
        uplink_token="test-uplink-token",
    )

    assert order == [
        "startup",
        "ready",
        "auto_custom",
        "lifecycle.command_loop_start",
    ]
    startup_payload = next(payload for payload in payloads if payload.get("req_id") == host_module.STARTUP_RESULT_REQ_ID)
    assert startup_payload["success"] is True


@pytest.mark.plugin_unit
def test_plugin_process_runner_uses_timeout_when_reporting_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payloads: list[dict[str, object]] = []
    timeouts: list[float | None] = []
    config_path = tmp_path / "demo" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo'\ntype='adapter'\n", encoding="utf-8")

    class _Sender:
        def __init__(self, channel: str) -> None:
            self.channel = channel

        def put(self, payload: dict[str, object], block: bool = True, timeout: float | None = None) -> None:
            payloads.append(payload)
            timeouts.append(timeout)

        def put_nowait(self, payload: dict[str, object]) -> None:
            self.put(payload)

    class _ChildTransport:
        def __init__(
            self,
            downlink_endpoint: str,
            uplink_endpoint: str,
            uplink_token: str,
        ) -> None:
            del downlink_endpoint, uplink_endpoint, uplink_token

        def channel_sender(self, channel: str) -> _Sender:
            return _Sender(channel)

        def close(self) -> None:
            return

    monkeypatch.setattr(host_module, "_setup_plugin_logger", lambda *args, **kwargs: _FakeLogger())
    monkeypatch.setattr(host_module, "_setup_logging_interception", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_plugin_import_roots", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_current_plugin_import_root", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_plugin_vendor_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "ChildTransport", _ChildTransport)

    def _raise_import_error(*_args, **_kwargs):
        raise RuntimeError("import exploded")

    monkeypatch.setattr(host_module, "_import_plugin_module", _raise_import_error)

    with pytest.raises(RuntimeError, match="import exploded"):
        host_module._plugin_process_runner(
            plugin_id="demo",
            entry_point="tests.fake:DemoPlugin",
            config_path=config_path,
            downlink_endpoint="ipc://down",
            uplink_endpoint="ipc://up",
            uplink_token="test-uplink-token",
        )

    result_payloads = [
        payload
        for payload in payloads
        if payload.get("req_id") in {"CRASH", host_module.STARTUP_RESULT_REQ_ID}
    ]
    assert [payload["req_id"] for payload in result_payloads] == ["CRASH", host_module.STARTUP_RESULT_REQ_ID]
    assert timeouts == [10.0, 10.0]


@pytest.mark.plugin_unit
def test_plugin_process_runner_skips_auto_work_after_failed_startup_in_fail_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started_threads: list[str] = []
    payloads: list[dict[str, object]] = []
    config_path = tmp_path / "demo" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo'\ntype='adapter'\n", encoding="utf-8")

    startup_meta = EventMeta(event_type="lifecycle", id="startup")
    timer_meta = EventMeta(
        event_type="timer",
        id="tick",
        auto_start=True,
        extra={"mode": "interval", "seconds": 1},
    )
    auto_meta = EventMeta(
        event_type="custom_demo",
        id="boot",
        auto_start=True,
        extra={"trigger_method": "auto"},
    )

    async def _startup() -> None:
        raise RuntimeError("startup exploded")

    async def _timer() -> None:
        raise AssertionError("timer auto-start should not run")

    async def _auto_custom() -> None:
        raise AssertionError("custom auto-start should not run")

    setattr(_startup, host_module.EVENT_META_ATTR, startup_meta)
    setattr(_timer, host_module.EVENT_META_ATTR, timer_meta)
    setattr(_auto_custom, host_module.EVENT_META_ATTR, auto_meta)

    class _Plugin:
        def __init__(self, ctx) -> None:
            self.ctx = ctx
            self.config = SimpleNamespace(dump_effective_sync=lambda timeout=3.0: {})

        def collect_entries(self, wrap_with_hooks: bool = True) -> dict[str, EventHandler]:
            return {
                "startup": EventHandler(meta=startup_meta, handler=_startup),
                "tick": EventHandler(meta=timer_meta, handler=_timer),
                "boot": EventHandler(meta=auto_meta, handler=_auto_custom),
            }

    class _Sender:
        def __init__(self, channel: str) -> None:
            self.channel = channel

        def put(self, payload: dict[str, object], block: bool = True, timeout: float | None = None) -> None:
            payloads.append(payload)

        def put_nowait(self, payload: dict[str, object]) -> None:
            self.put(payload)

    class _ChildTransport:
        def __init__(
            self,
            downlink_endpoint: str,
            uplink_endpoint: str,
            uplink_token: str,
        ) -> None:
            del downlink_endpoint, uplink_endpoint, uplink_token

        def channel_sender(self, channel: str) -> _Sender:
            return _Sender(channel)

        async def recv_downlink(self, timeout_ms: int = 1000):
            return (host_module.CH_CMD, {"type": "STOP"})

        def close(self) -> None:
            return

    class _RecordingThread:
        def __init__(self, target, args=(), kwargs=None, daemon: bool | None = None) -> None:
            self.target = target

        def start(self) -> None:
            started_threads.append(getattr(self.target, "__name__", repr(self.target)))

    monkeypatch.setattr(host_module, "_setup_plugin_logger", lambda *args, **kwargs: _FakeLogger())
    monkeypatch.setattr(host_module, "_setup_logging_interception", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_plugin_import_roots", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_current_plugin_import_root", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_prepare_child_plugin_vendor_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(host_module, "_import_plugin_module", lambda *args, **kwargs: SimpleNamespace(DemoPlugin=_Plugin))
    monkeypatch.setattr(host_module, "ChildTransport", _ChildTransport)
    monkeypatch.setattr(host_module.threading, "Thread", _RecordingThread)

    host_module._plugin_process_runner(
        plugin_id="demo",
        entry_point="tests.fake:DemoPlugin",
        config_path=config_path,
        downlink_endpoint="ipc://down",
        uplink_endpoint="ipc://up",
        uplink_token="test-uplink-token",
        startup_options={"startup_failure": "fail"},
    )

    startup_payload = next(payload for payload in payloads if payload.get("req_id") == host_module.STARTUP_RESULT_REQ_ID)
    assert startup_payload["success"] is False
    assert "startup exploded" in str(startup_payload["error"])
    assert started_threads == []


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_plugin_process_start_refreshes_storage_layout_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selected_root = tmp_path / "selected-root"
    calls: list[dict[str, object]] = []

    monkeypatch.delenv("NEKO_STORAGE_SELECTED_ROOT", raising=False)
    monkeypatch.setattr(host_module.state, "register_downlink_sender", lambda *_args, **_kwargs: None)

    class _FakeTransport:
        downlink_endpoint = "ipc://down"
        uplink_endpoint = "ipc://up"

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(host_module, "HostTransport", _FakeTransport)
    monkeypatch.setattr(host_module, "PluginCommunicationResourceManager", lambda **_kwargs: _FakeCommManager())
    monkeypatch.setattr(host_module.multiprocessing, "Event", lambda: object())
    monkeypatch.setattr(host_module.multiprocessing, "Process", lambda **_kwargs: _FakeProcess())

    monkeypatch.setattr(
        host_module,
        "_resolve_current_storage_layout",
        lambda: {"selected_root": str(selected_root), "anchor_root": str(tmp_path / "anchor")},
    )

    def _export(layout: dict[str, object]) -> None:
        calls.append(layout)
        host_module.os.environ["NEKO_STORAGE_SELECTED_ROOT"] = str(layout["selected_root"])

    monkeypatch.setattr(storage_layout_module, "export_storage_layout_to_env", _export)

    plugin_host = host_module.PluginProcessHost(
        plugin_id="demo",
        entry_point="plugins.demo:DemoPlugin",
        config_path=tmp_path / "demo" / "plugin.toml",
    )

    await plugin_host.start()

    assert calls == [{"selected_root": str(selected_root), "anchor_root": str(tmp_path / "anchor")}]
    assert host_module.os.environ["NEKO_STORAGE_SELECTED_ROOT"] == str(selected_root)


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_plugin_process_start_removes_downlink_sender_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registered: list[str] = []
    removed: list[str] = []

    class _FakeTransport:
        downlink_endpoint = "ipc://down"
        uplink_endpoint = "ipc://up"

        def close(self) -> None:
            self.closed = True

    class _FailingProcess:
        pid = None
        exitcode = None

        def is_alive(self) -> bool:
            return False

        def start(self) -> None:
            raise RuntimeError("spawn boom")

    comm_manager = _FakeCommManager()
    monkeypatch.setattr(host_module, "HostTransport", _FakeTransport)
    monkeypatch.setattr(host_module, "PluginCommunicationResourceManager", lambda **_kwargs: comm_manager)
    monkeypatch.setattr(host_module.multiprocessing, "Event", lambda: SimpleNamespace(set=lambda: None))
    monkeypatch.setattr(host_module.multiprocessing, "Process", lambda **_kwargs: _FailingProcess())
    monkeypatch.setattr(host_module, "_refresh_child_storage_layout_env", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_module.state, "register_downlink_sender", lambda plugin_id, _sender: registered.append(plugin_id))
    monkeypatch.setattr(host_module.state, "remove_downlink_sender", lambda plugin_id: removed.append(plugin_id))

    plugin_host = host_module.PluginProcessHost(
        plugin_id="demo",
        entry_point="plugins.demo:DemoPlugin",
        config_path=tmp_path / "demo" / "plugin.toml",
    )

    with pytest.raises(RuntimeError, match="spawn boom"):
        await plugin_host.start()

    assert registered == ["demo"]
    assert removed == ["demo"]
    assert getattr(plugin_host.transport, "closed", False) is True
    assert comm_manager.shutdown_timeout == host_module.PLUGIN_SHUTDOWN_TIMEOUT


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_abort_startup_after_failure_removes_downlink_sender(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    removed: list[str] = []

    class _FakeTransport:
        downlink_endpoint = "ipc://down"
        uplink_endpoint = "ipc://up"

        def close(self) -> None:
            self.closed = True

    comm_manager = _FakeCommManager()
    monkeypatch.setattr(host_module, "HostTransport", _FakeTransport)
    monkeypatch.setattr(host_module, "PluginCommunicationResourceManager", lambda **_kwargs: comm_manager)
    monkeypatch.setattr(host_module.multiprocessing, "Event", lambda: SimpleNamespace(set=lambda: None))
    monkeypatch.setattr(host_module.multiprocessing, "Process", lambda **_kwargs: _FakeProcess())
    monkeypatch.setattr(host_module.state, "remove_downlink_sender", lambda plugin_id: removed.append(plugin_id))

    plugin_host = host_module.PluginProcessHost(
        plugin_id="demo",
        entry_point="plugins.demo:DemoPlugin",
        config_path=tmp_path / "demo" / "plugin.toml",
    )

    await plugin_host._abort_startup_after_failure(timeout=0.01)

    assert removed == ["demo"]
    assert comm_manager.stop_sent is True
    assert comm_manager.shutdown_timeout == 0.01
    assert getattr(plugin_host.transport, "closed", False) is True


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_plugin_process_start_keeps_running_on_startup_error_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    removed: list[str] = []

    class _FakeTransport:
        downlink_endpoint = "ipc://down"
        uplink_endpoint = "ipc://up"

        def close(self) -> None:
            self.closed = True

    comm_manager = _StartupErrorCommManager()
    monkeypatch.setattr(host_module, "HostTransport", _FakeTransport)
    monkeypatch.setattr(host_module, "PluginCommunicationResourceManager", lambda **_kwargs: comm_manager)
    monkeypatch.setattr(host_module.multiprocessing, "Event", lambda: SimpleNamespace(set=lambda: None))
    monkeypatch.setattr(host_module.multiprocessing, "Process", lambda **_kwargs: _FakeProcess())
    monkeypatch.setattr(host_module, "_refresh_child_storage_layout_env", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_module.state, "register_downlink_sender", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_module.state, "remove_downlink_sender", lambda plugin_id: removed.append(plugin_id))

    plugin_host = host_module.PluginProcessHost(
        plugin_id="demo",
        entry_point="plugins.demo:DemoPlugin",
        config_path=tmp_path / "demo" / "plugin.toml",
    )

    startup_result = await plugin_host.start(startup_timeout=1.0)

    assert startup_result == {"status": "failed", "startup_error": "lifecycle.startup failed"}
    assert comm_manager.allow_startup_error is True
    assert getattr(comm_manager, "stop_sent", False) is False
    assert getattr(comm_manager, "shutdown_timeout", None) is None
    assert getattr(plugin_host.transport, "closed", False) is False
    assert removed == []


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_plugin_process_start_aborts_on_startup_error_in_fail_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    removed: list[str] = []

    class _FakeTransport:
        downlink_endpoint = "ipc://down"
        uplink_endpoint = "ipc://up"

        def close(self) -> None:
            self.closed = True

    comm_manager = _StartupErrorCommManager()
    monkeypatch.setattr(host_module, "HostTransport", _FakeTransport)
    monkeypatch.setattr(host_module, "PluginCommunicationResourceManager", lambda **_kwargs: comm_manager)
    monkeypatch.setattr(host_module.multiprocessing, "Event", lambda: SimpleNamespace(set=lambda: None))
    monkeypatch.setattr(host_module.multiprocessing, "Process", lambda **_kwargs: _FakeProcess())
    monkeypatch.setattr(host_module, "_refresh_child_storage_layout_env", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_module.state, "register_downlink_sender", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_module.state, "remove_downlink_sender", lambda plugin_id: removed.append(plugin_id))

    plugin_host = host_module.PluginProcessHost(
        plugin_id="demo",
        entry_point="plugins.demo:DemoPlugin",
        config_path=tmp_path / "demo" / "plugin.toml",
    )

    with pytest.raises(host_module.PluginLifecycleError, match="lifecycle\\.startup failed"):
        await plugin_host.start(startup_timeout=1.0, startup_failure="fail")

    assert comm_manager.allow_startup_error is False
    assert comm_manager.stop_sent is True
    assert comm_manager.shutdown_timeout == host_module.PLUGIN_SHUTDOWN_TIMEOUT
    assert getattr(plugin_host.transport, "closed", False) is True
    assert removed == ["demo"]


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_plugin_process_start_passes_startup_failure_policy_to_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_startup_options: list[dict[str, object]] = []

    class _FakeTransport:
        downlink_endpoint = "ipc://down"
        uplink_endpoint = "ipc://up"

        def close(self) -> None:
            self.closed = True

    class _CapturingProcess(_FakeProcess):
        def __init__(self, **kwargs) -> None:
            super().__init__()
            self.args = kwargs["args"]

        def start(self) -> None:
            # Positional index on purpose. args[-1] is message_uplink_endpoint,
            # a str, so reading from the end would slip past the isinstance
            # check below and leave the assertion vacuously green.
            startup_options = self.args[7]
            captured_startup_options.append(
                dict(startup_options)
                if isinstance(startup_options, dict)
                else startup_options
            )
            super().start()

    comm_manager = _StartupErrorCommManager()
    monkeypatch.setattr(host_module, "HostTransport", _FakeTransport)
    monkeypatch.setattr(host_module, "PluginCommunicationResourceManager", lambda **_kwargs: comm_manager)
    monkeypatch.setattr(host_module.multiprocessing, "Event", lambda: SimpleNamespace(set=lambda: None))
    monkeypatch.setattr(host_module.multiprocessing, "Process", lambda **kwargs: _CapturingProcess(**kwargs))
    monkeypatch.setattr(host_module, "_refresh_child_storage_layout_env", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_module.state, "register_downlink_sender", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host_module.state, "remove_downlink_sender", lambda _plugin_id: None)

    plugin_host = host_module.PluginProcessHost(
        plugin_id="demo",
        entry_point="plugins.demo:DemoPlugin",
        config_path=tmp_path / "demo" / "plugin.toml",
    )

    with pytest.raises(host_module.PluginLifecycleError, match="lifecycle\\.startup failed"):
        await plugin_host.start(startup_timeout=1.0, startup_failure="fail")

    assert captured_startup_options == [{"startup_failure": "fail"}]


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_config_update_rolls_back_runtime_helpers_when_config_change_fails() -> None:
    class _Ctx:
        def __init__(self) -> None:
            self._effective_config = {"plugin": {"store": {"enabled": False}}}
            self.refreshed: list[dict[str, object]] = []
            self.handler_contexts: list[str] = []

        def _refresh_instance_runtime_config(self, effective_config: dict[str, object]) -> None:
            self.refreshed.append(host_module.copy.deepcopy(effective_config))

        @contextmanager
        def _handler_scope(self, handler_ctx: str):
            self.handler_contexts.append(handler_ctx)
            yield

    def _config_change(**_kwargs: object) -> None:
        raise RuntimeError("boom")

    ctx = _Ctx()
    sender = _FakeResponseSender()
    await host_module._handle_config_update_command(
        msg={
            "req_id": "req-1",
            "config": {"plugin": {"store": {"enabled": True}}},
            "mode": "temporary",
        },
        ctx=ctx,
        events_by_type={"lifecycle": {"config_change": _config_change}},
        plugin_id="demo",
        res_sender=sender,
        logger=_FakeLogger(),
    )

    assert ctx._effective_config == {"plugin": {"store": {"enabled": False}}}
    assert ctx.refreshed == [
        {"plugin": {"store": {"enabled": True}}},
        {"plugin": {"store": {"enabled": False}}},
    ]
    assert sender.payloads[-1]["success"] is False
    assert "config_change handler failed" in str(sender.payloads[-1]["error"])
    assert ctx.handler_contexts == ["lifecycle.config_change"]
