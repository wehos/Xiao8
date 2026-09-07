from __future__ import annotations

import asyncio
import copy
import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from plugin._types.exceptions import PluginLifecycleError
from plugin.core import registry as registry_module
from plugin.server.application.plugins import query_service as query_module
from plugin.server.application.plugins import lifecycle_service as module
from plugin.server.application.plugins.metadata_scanner import IsolatedPluginMetadata
from plugin.server.application.plugins import operation_lock as operation_lock_module
import plugin.server.application.plugins.installation_transactions.uninstall as uninstall_module
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure import runtime_overrides as runtime_overrides_module
from plugin.sdk.plugin.decorators import plugin_entry


class _FakeProcessHost:
    def __init__(
        self,
        plugin_id: str,
        entry_point: str,
        config_path: Path,
    ) -> None:
        self.plugin_id = plugin_id
        self.entry_point = entry_point
        self.config_path = config_path
        self.process = SimpleNamespace(is_alive=lambda: True, exitcode=None)
        self.started = False
        self.stopped = False

    async def start(self, message_target_queue: object, startup_timeout: float | None = None) -> None:
        self.startup_timeout = startup_timeout
        self.started = True

    async def shutdown(self, timeout: float = module.PLUGIN_SHUTDOWN_TIMEOUT) -> None:
        self.stopped = True

    def is_alive(self) -> bool:
        return True


class _FakeAdapterPlugin:
    @plugin_entry(id="list_servers", name="List Servers", description="List configured MCP servers")
    async def list_servers(self) -> dict[str, object]:
        return {"servers": []}


def _metadata_scan_for(plugin_cls: type):
    def _scan(**kwargs: object) -> IsolatedPluginMetadata:
        plugin_id = str(kwargs["plugin_id"])
        conf = kwargs.get("conf")
        pdata = kwargs.get("pdata")
        return IsolatedPluginMetadata(
            entries_preview=registry_module._extract_entries_preview(
                plugin_id,
                plugin_cls,
                conf if isinstance(conf, dict) else {},
                pdata if isinstance(pdata, dict) else {},
            ),
            handlers={},
            entry_methods={},
        )

    return _scan


class _CaptureLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.errors: list[str] = []

    def info(self, *_args, **_kwargs) -> None:
        return

    def debug(self, *_args, **_kwargs) -> None:
        return

    def warning(self, message, *args, **_kwargs) -> None:
        rendered = str(message)
        for arg in args:
            rendered = rendered.replace("{}", str(arg), 1)
        self.messages.append(rendered)

    def error(self, message, *args, **_kwargs) -> None:
        rendered = str(message)
        for arg in args:
            rendered = rendered.replace("{}", str(arg), 1)
        self.errors.append(rendered)


@pytest.mark.plugin_unit
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "stop"])
async def test_runtime_mutations_wait_for_plugin_operation_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    plugin_id = "serialized_plugin"
    host = _FakeProcessHost(
        plugin_id=plugin_id,
        entry_point="tests.fake:Plugin",
        config_path=tmp_path / plugin_id / "plugin.toml",
    )
    hosts_backup = dict(module.state.plugin_hosts)
    second_acquire_attempted = asyncio.Event()
    original_acquire = operation_lock_module._PROCESS_LOCK.acquire
    acquire_calls = 0

    async def _tracked_acquire(deadline: float | None = None) -> None:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 2:
            second_acquire_attempted.set()
        # 截止期照原样转发，别在替身里把它吞掉。
        await original_acquire(deadline)

    async def _clear_tools(_plugin_id: str) -> None:
        return None

    monkeypatch.setattr(
        operation_lock_module._PROCESS_LOCK,
        "acquire",
        _tracked_acquire,
    )
    monkeypatch.setattr(module, "clear_plugin_llm_tools", _clear_tools)

    try:
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts[plugin_id] = host

        service = module.PluginLifecycleService()
        async with operation_lock_module.plugin_operation_lock.hold():
            if operation == "start":
                task = asyncio.create_task(
                    service.start_plugin(plugin_id, refresh_registry=False)
                )
            else:
                task = asyncio.create_task(service.stop_plugin(plugin_id))
            await asyncio.wait_for(second_acquire_attempted.wait(), timeout=5)
            assert task.done() is False

        response = await asyncio.wait_for(task, timeout=5)
        assert response["success"] is True
    finally:
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)


class _FakeInstallSourceManager:
    def __init__(
        self,
        *,
        package_id: str,
        profile_dir: str = "",
        profile_installed: bool | None = None,
        channel: str = "imported",
        root_id: str = "user",
        plugin_id: str | None = None,
        active_package_ids: tuple[str, ...] = (),
        active_profile_dirs: tuple[str, ...] = (),
        active_root_ids: tuple[str, ...] = (),
        active_directory_names: tuple[str, ...] = (),
        active_channels: tuple[str, ...] = (),
        list_entries_error: Exception | None = None,
    ) -> None:
        self.package_id = package_id
        self.profile_dir = profile_dir
        self.profile_installed = profile_installed
        self.channel = channel
        self.root_id = root_id
        self.plugin_id = plugin_id
        self.active_package_ids = active_package_ids
        self.active_profile_dirs = active_profile_dirs
        self.active_root_ids = active_root_ids
        self.active_directory_names = active_directory_names
        self.active_channels = active_channels
        self.list_entries_error = list_entries_error
        self.marked_removed: list[Path] = []
        self.restored_entries: list[object] = []
        self.is_degraded = False

    def package_id_for_directory(
        self,
        _directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> str:
        del include_removed
        return self.package_id

    def mark_removed(self, *, directory_path: Path) -> None:
        self.marked_removed.append(directory_path)

    def restore_entry_for_rollback(self, entry: object) -> None:
        self.restored_entries.append(entry)

    def entry_for_directory(
        self,
        directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> SimpleNamespace:
        del include_removed
        return SimpleNamespace(
            package_id=self.package_id,
            plugin_id=self.plugin_id or directory_path.name,
            profile_dir=self.profile_dir,
            profile_installed=self.profile_installed,
            channel=self.channel,
            root_id=self.root_id,
            directory_name=directory_path.name,
            removed=False,
        )

    def profile_dir_for_directory(
        self,
        _directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> str:
        del include_removed
        return self.profile_dir

    def list_entries(self) -> list[SimpleNamespace]:
        if self.list_entries_error is not None:
            raise self.list_entries_error
        return [
            SimpleNamespace(
                package_id=package_id,
                profile_dir=(self.active_profile_dirs[index] if index < len(self.active_profile_dirs) else ""),
                profile_installed=None,
                channel=(
                    self.active_channels[index]
                    if index < len(self.active_channels)
                    else "imported"
                ),
                root_id=(self.active_root_ids[index] if index < len(self.active_root_ids) else "user"),
                directory_name=(
                    self.active_directory_names[index]
                    if index < len(self.active_directory_names)
                    else f"other_{index}"
                ),
            )
            for index, package_id in enumerate(self.active_package_ids)
        ]


@pytest.mark.plugin_unit
def test_get_plugin_config_path_returns_existing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    config_file = root / "demo" / "plugin.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("[plugin]\nid='demo'\n", encoding="utf-8")

    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

    resolved = module._get_plugin_config_path("demo")
    assert resolved == config_file.resolve()


@pytest.mark.plugin_unit
@pytest.mark.parametrize("plugin_id", ["../evil", "a/b", "", "  ", "demo..", "demo/"])
def test_get_plugin_config_path_rejects_invalid_plugin_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_id: str,
) -> None:
    root = tmp_path / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

    assert module._get_plugin_config_path(plugin_id) is None


@pytest.mark.plugin_unit
def test_get_plugin_config_path_returns_none_for_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

    assert module._get_plugin_config_path("demo") is None


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_already_running_reports_preference_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "running_plugin" / "plugin.toml"
    host = _FakeProcessHost(
        plugin_id="running_plugin",
        entry_point="tests.fake:Plugin",
        config_path=config_path,
    )
    hosts_backup = dict(module.state.plugin_hosts)

    monkeypatch.setattr(
        module,
        "set_runtime_override",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            runtime_overrides_module.RuntimeOverrideWriteError("disk full")
        ),
    )

    try:
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts["running_plugin"] = host

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin(
                "running_plugin",
                persist_user_intent=True,
            )

        assert exc_info.value.code == "PLUGIN_RUNTIME_PREFERENCE_PERSIST_FAILED"
        assert exc_info.value.details["runtime_state_changed"] is False
        with module.state.acquire_plugin_hosts_read_lock():
            assert module.state.plugin_hosts["running_plugin"] is host
    finally:
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)


@pytest.mark.plugin_unit
def test_parse_single_plugin_config_warns_on_directory_id_mismatch(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "bilibili_danmaku"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    config_path = plugin_dir / "plugin.toml"
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'bilibili-danmaku'",
                "entry = 'plugin.plugins.bilibili_danmaku:Plugin'",
            ]
        ),
        encoding="utf-8",
    )

    messages: list[str] = []

    class _Logger:
        def info(self, *_args, **_kwargs) -> None:
            return

        def debug(self, *_args, **_kwargs) -> None:
            return

        def warning(self, message, *args, **_kwargs) -> None:
            rendered = str(message)
            for arg in args:
                rendered = rendered.replace("{}", str(arg), 1)
            messages.append(rendered)

        def error(self, *_args, **_kwargs) -> None:
            return

    parsed = registry_module._parse_single_plugin_config(
        config_path,
        set(),
        _Logger(),
    )

    assert parsed is not None
    assert any("directory name" in message and "does not match declared plugin.id" in message for message in messages)


@pytest.mark.plugin_unit
def test_parse_single_plugin_config_rejects_removed_script_type(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "legacy_script"
    plugin_dir.mkdir(parents=True)
    config_path = plugin_dir / "plugin.toml"
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'legacy_script'",
                "type = 'script'",
                "entry = 'plugin.plugins.legacy_script:Plugin'",
            ]
        ),
        encoding="utf-8",
    )
    logger = _CaptureLogger()

    parsed = registry_module._parse_single_plugin_config(config_path, set(), logger)

    assert parsed is None
    assert any("unsupported type='script'" in message for message in logger.errors)


@pytest.mark.plugin_unit
def test_parse_single_plugin_config_warns_on_noncanonical_fields(
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    config_path = plugin_dir / "plugin.toml"
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'demo_plugin'",
                "entry = ' plugin.plugins.demo_plugin:Plugin '",
                "keywords = 'tag1'",
                "passive = 'yes'",
                "",
                "[plugin_runtime]",
                "enabled = 'true'",
                "auto_start = 'false'",
            ]
        ),
        encoding="utf-8",
    )

    messages: list[str] = []

    class _Logger:
        def info(self, *_args, **_kwargs) -> None:
            return

        def debug(self, *_args, **_kwargs) -> None:
            return

        def warning(self, message, *args, **_kwargs) -> None:
            rendered = str(message)
            for arg in args:
                rendered = rendered.replace("{}", str(arg), 1)
            messages.append(rendered)

        def error(self, *_args, **_kwargs) -> None:
            return

    parsed = registry_module._parse_single_plugin_config(
        config_path,
        set(),
        _Logger(),
    )

    assert parsed is not None
    assert any("[plugin].entry" in message and "leading/trailing whitespace" in message for message in messages)
    assert any("[plugin].keywords should be a string list" in message for message in messages)
    assert any("[plugin].passive" in message and "prefer true/false" in message for message in messages)
    assert any("[plugin_runtime].enabled" in message and "prefer true/false" in message for message in messages)


@pytest.mark.plugin_unit
def test_normalize_startup_failure_policy_defaults_none_to_warn() -> None:
    assert module._normalize_startup_failure_policy(None, plugin_id="demo") == "warn"


@pytest.mark.plugin_unit
@pytest.mark.parametrize("raw_value", ["", False, 0])
def test_normalize_startup_failure_policy_rejects_falsy_non_default_values(raw_value: object) -> None:
    with pytest.raises(ServerDomainError) as exc_info:
        module._normalize_startup_failure_policy(raw_value, plugin_id="demo")

    assert exc_info.value.code == "INVALID_PLUGIN_CONFIG"
    assert exc_info.value.details["error_type"] == "InvalidStartupFailurePolicy"
    assert "startup_failure" in exc_info.value.message


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_refreshes_registry_before_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "refresh_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'refresh_adapter'",
                "name = 'Refresh Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
            ]
        ),
        encoding="utf-8",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    refresh_calls: list[str] = []

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        async def _refresh_plugin(plugin_id: str) -> dict[str, object]:
            refresh_calls.append(plugin_id)
            return {"success": True, "plugin_id": plugin_id, "status": "added"}

        monkeypatch.setattr(module.plugin_registry_service, "refresh_plugin", _refresh_plugin)
        monkeypatch.setattr(module, "_get_plugin_config_path", lambda plugin_id: config_path)
        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _FakeProcessHost)
        monkeypatch.setattr(module, "scan_plugin_metadata_isolated", _metadata_scan_for(_FakeAdapterPlugin))
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        service = module.PluginLifecycleService()
        response = await service.start_plugin("refresh_adapter")

        assert response["success"] is True
        assert refresh_calls == ["refresh_adapter"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
@pytest.mark.parametrize("start_fails", [False, True])
async def test_delete_user_override_restores_running_builtin_and_preserves_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    start_fails: bool,
) -> None:
    exec_root = tmp_path / "exec" / "plugins"
    user_dir = exec_root / "study_companion"
    user_config = user_dir / "plugin.toml"
    user_dir.mkdir(parents=True)
    user_config.write_text("[plugin]\nid='study_companion'\n", encoding="utf-8")
    builtin_config = tmp_path / "builtin" / "study_companion" / "plugin.toml"
    builtin_config.parent.mkdir(parents=True)
    builtin_config.write_text("[plugin]\nid='study_companion'\n", encoding="utf-8")
    state_dir = tmp_path / "state" / "plugins" / "study_companion" / "data"
    state_files = {
        state_dir / "study.db": b"persistent-db",
        state_dir / "study.db-wal": b"persistent-wal",
        state_dir / "study.db-shm": b"persistent-shm",
    }
    for path, content in state_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    hashes_before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in state_files
    }

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    start_calls: list[str] = []
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["study_companion"] = {
                "id": "study_companion",
                "config_path": str(user_config),
                "effective_source": "user",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts["study_companion"] = object()

        async def stop_plugin(self, plugin_id: str, **_kwargs: object) -> dict[str, object]:
            with module.state.acquire_plugin_hosts_write_lock():
                module.state.plugin_hosts.pop(plugin_id, None)
            return {"success": True}

        async def start_plugin(self, plugin_id: str, **_kwargs: object) -> dict[str, object]:
            start_calls.append(plugin_id)
            if start_fails:
                raise ServerDomainError(
                    code="PLUGIN_START_FAILED",
                    message="builtin start failed",
                    status_code=500,
                )
            return {"success": True}

        async def refresh_registry() -> dict[str, object]:
            with module.state.acquire_plugins_write_lock():
                module.state.plugins["study_companion"] = {
                    "id": "study_companion",
                    "config_path": str(builtin_config),
                    "effective_source": "builtin",
                }
            return {"success": True}

        monkeypatch.setattr(
            uninstall_module, "get_user_plugin_exec_root", lambda: exec_root
        )
        monkeypatch.setattr(
            uninstall_module,
            "get_plugin_state_root",
            lambda: tmp_path / "state" / "plugins",
        )
        monkeypatch.setattr(
            uninstall_module,
            "PLUGIN_CONFIG_ROOTS",
            (exec_root, builtin_config.parent.parent),
        )
        monkeypatch.setattr(module.PluginLifecycleService, "stop_plugin", stop_plugin)
        monkeypatch.setattr(module.PluginLifecycleService, "start_plugin", start_plugin)
        monkeypatch.setattr(
            uninstall_module.plugin_registry_service,
            "refresh_registry",
            refresh_registry,
        )
        install_source_manager = _FakeInstallSourceManager(package_id="")
        monkeypatch.setattr(
            uninstall_module, "get_install_source_manager", lambda: install_source_manager
        )
        monkeypatch.setattr(
            uninstall_module,
            "_stage_orphaned_package_profile_sync",
            lambda _path, *, manager=None: None,
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda _event: None)

        response = await module.PluginLifecycleService().delete_plugin("study_companion")

        assert response["success"] is True
        assert response["restored_builtin"] is True
        assert response["restored_builtin_started"] is not start_fails
        assert response["preference_action"] == "preserved"
        assert response["filesystem_rollback"] == "not_needed"
        assert response["cleanup_pending"] is False
        restart_error = response["restored_builtin_restart_error"]
        if start_fails:
            assert restart_error == {
                "code": "PLUGIN_BUILTIN_RESTORE_START_FAILED",
                "message": "builtin start failed",
                "error_type": "ServerDomainError",
            }
        else:
            assert restart_error is None
        assert start_calls == ["study_companion"]
        assert user_dir.exists() is False
        assert builtin_config.is_file()
        assert {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in state_files
        } == hashes_before
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_user_override_preserves_disabled_preference_for_restored_builtin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    exec_root = tmp_path / "exec"
    user_dir = exec_root / "study_companion"
    user_config = user_dir / "plugin.toml"
    user_dir.mkdir(parents=True)
    user_config.write_text("[plugin]\nid='study_companion'\n", encoding="utf-8")
    builtin_config = tmp_path / "builtin" / "study_companion" / "plugin.toml"
    builtin_config.parent.mkdir(parents=True)
    builtin_config.write_text("[plugin]\nid='study_companion'\n", encoding="utf-8")
    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["study_companion"] = {
                "config_path": str(user_config),
                "effective_source": "user",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        runtime_overrides_module.set_runtime_override(
            "study_companion",
            False,
            auto_start=False,
        )

        async def refresh_registry() -> dict[str, object]:
            with module.state.acquire_plugins_write_lock():
                module.state.plugins["study_companion"] = {
                    "config_path": str(builtin_config),
                    "effective_source": "builtin",
                    "runtime_enabled": False,
                    "runtime_auto_start": False,
                }
            return {"success": True}

        monkeypatch.setattr(
            uninstall_module, "get_user_plugin_exec_root", lambda: exec_root
        )
        monkeypatch.setattr(
            uninstall_module, "get_plugin_state_root", lambda: tmp_path / "state"
        )
        monkeypatch.setattr(
            uninstall_module,
            "PLUGIN_CONFIG_ROOTS",
            (exec_root, builtin_config.parent.parent),
        )
        monkeypatch.setattr(
            uninstall_module.plugin_registry_service,
            "refresh_registry",
            refresh_registry,
        )
        install_source_manager = _FakeInstallSourceManager(package_id="")
        monkeypatch.setattr(
            uninstall_module, "get_install_source_manager", lambda: install_source_manager
        )
        monkeypatch.setattr(
            uninstall_module,
            "_stage_orphaned_package_profile_sync",
            lambda _path, *, manager=None: None,
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda _event: None)

        response = await module.PluginLifecycleService().delete_plugin("study_companion")

        assert response["restored_builtin"] is True
        assert response["restored_builtin_started"] is False
        assert response["preference_action"] == "preserved"
        assert _isolate_runtime_overrides == {
            "study_companion": {"enabled": False, "auto_start": False},
        }
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_manual_plugin_fails_before_runtime_or_filesystem_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "manual_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        "[plugin]\nid='manual_plugin'\nentry='tests.fake:Plugin'\n",
        encoding="utf-8",
    )
    manager = _FakeInstallSourceManager(package_id="", channel="manual")
    runtime_checks: list[str] = []

    monkeypatch.setattr(
        module,
        "_get_plugin_meta_sync",
        lambda _plugin_id: {"id": "manual_plugin"},
    )
    monkeypatch.setattr(
        module,
        "_resolve_plugin_config_path_sync",
        lambda _plugin_id, _meta: plugin_dir / "plugin.toml",
    )
    monkeypatch.setattr(uninstall_module, "get_install_source_manager", lambda: manager)
    monkeypatch.setattr(
        module,
        "_plugin_is_running_sync",
        lambda plugin_id: runtime_checks.append(plugin_id) or True,
    )

    with pytest.raises(ServerDomainError) as captured:
        await module.PluginLifecycleService().delete_plugin("manual_plugin")

    assert captured.value.code == "PLUGIN_MANUAL_NOT_MANAGED"
    assert captured.value.status_code == 409
    assert captured.value.details["plugin_id"] == "manual_plugin"
    assert captured.value.details["stage"] == "ownership"
    assert runtime_checks == []
    assert manager.marked_removed == []
    assert plugin_dir.is_dir()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_reloads_install_source_before_ownership_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "managed_plugin"
    plugin_dir.mkdir()
    config_path = plugin_dir / "plugin.toml"
    config_path.write_text(
        "[plugin]\nid='managed_plugin'\nentry='tests.fake:Plugin'\n",
        encoding="utf-8",
    )
    manager = _FakeInstallSourceManager(package_id="", channel="imported")
    load_calls = 0

    def reload_stale_snapshot() -> None:
        nonlocal load_calls
        load_calls += 1
        manager.channel = "manual"

    manager.load = reload_stale_snapshot  # type: ignore[attr-defined]
    runtime_checks: list[str] = []

    monkeypatch.setattr(
        module,
        "_get_plugin_meta_sync",
        lambda _plugin_id: {"id": "managed_plugin"},
    )
    monkeypatch.setattr(
        module,
        "_resolve_plugin_config_path_sync",
        lambda _plugin_id, _meta: config_path,
    )
    monkeypatch.setattr(uninstall_module, "get_install_source_manager", lambda: manager)
    monkeypatch.setattr(
        module,
        "_plugin_is_running_sync",
        lambda plugin_id: runtime_checks.append(plugin_id) or False,
    )

    with pytest.raises(ServerDomainError) as captured:
        await module.PluginLifecycleService().delete_plugin("managed_plugin")

    assert captured.value.code == "PLUGIN_MANUAL_NOT_MANAGED"
    assert load_calls == 1
    assert runtime_checks == []
    assert plugin_dir.is_dir()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["imported", "market"])
async def test_delete_runtime_alias_uses_declared_id_for_managed_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    channel: str,
) -> None:
    root = tmp_path / "plugins"
    plugin_dir = root / "demo_1"
    config_path = plugin_dir / "plugin.toml"
    plugin_dir.mkdir(parents=True)
    config_path.write_text(
        "[plugin]\nid='demo'\nentry='tests.fake:Plugin'\n",
        encoding="utf-8",
    )
    manager = _FakeInstallSourceManager(
        package_id="",
        channel=channel,
        plugin_id="demo",
    )
    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["demo_1"] = {
                "id": "demo_1",
                "config_path": str(config_path),
                "effective_source": "user",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        async def _refresh_registry() -> dict[str, object]:
            return {"success": True}

        monkeypatch.setattr(uninstall_module, "PLUGIN_CONFIG_ROOTS", (root,))
        monkeypatch.setattr(
            uninstall_module,
            "get_user_package_profiles_root",
            lambda: tmp_path / "profiles",
        )
        monkeypatch.setattr(uninstall_module, "get_install_source_manager", lambda: manager)
        monkeypatch.setattr(
            uninstall_module.plugin_registry_service,
            "refresh_registry",
            _refresh_registry,
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda _event: None)

        response = await module.PluginLifecycleService().delete_plugin("demo_1")

        assert response["success"] is True
        assert response["plugin_id"] == "demo_1"
        assert response["deleted_from_disk"] is True
        assert response["preference_action"] == "cleared"
        assert plugin_dir.exists() is False
        assert manager.marked_removed == [plugin_dir]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
def test_delete_path_guard_rejects_builtin_and_state_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exec_root = tmp_path / "exec"
    builtin_root = tmp_path / "builtin"
    state_root = tmp_path / "plugins"
    monkeypatch.setattr(uninstall_module, "get_user_plugin_exec_root", lambda: exec_root)
    monkeypatch.setattr(uninstall_module, "get_plugin_state_root", lambda: state_root)
    monkeypatch.setattr(uninstall_module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(
        uninstall_module, "PLUGIN_CONFIG_ROOTS", (exec_root, builtin_root)
    )

    assert uninstall_module._path_within_plugin_roots_sync(exec_root / "demo") is True
    assert uninstall_module._path_within_plugin_roots_sync(builtin_root / "demo") is False
    assert uninstall_module._path_within_plugin_roots_sync(state_root / "demo") is False

    monkeypatch.setattr(uninstall_module, "get_user_plugin_exec_root", lambda: state_root)
    monkeypatch.setattr(
        uninstall_module, "PLUGIN_CONFIG_ROOTS", (state_root, builtin_root)
    )
    assert uninstall_module._path_within_plugin_roots_sync(state_root / "demo") is False


@pytest.mark.plugin_unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persist_fails", "refreshed_plugin_id", "resolved_plugin_id"),
    [
        (False, "demo_plugin", "demo_plugin"),
        (True, "demo_plugin", "demo_plugin"),
        (False, "refreshed_plugin", "resolved_plugin"),
    ],
    ids=("success", "persistence-failure", "multi-stage-id-resolution"),
)
async def test_start_plugin_persists_intent_after_success_and_migrates_resolved_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
    persist_fails: bool,
    refreshed_plugin_id: str,
    resolved_plugin_id: str,
) -> None:
    config_path = tmp_path / "demo_plugin" / "plugin.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'demo_plugin'",
                "name = 'Demo Plugin'",
                "entry = 'tests.fake:Plugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
            ]
        ),
        encoding="utf-8",
    )

    seen: dict[str, object] = {}

    async def _refresh_plugin(plugin_id: str) -> dict[str, object]:
        seen["override_at_refresh"] = runtime_overrides_module.get_runtime_override(plugin_id)
        with module.state.acquire_plugins_write_lock():
            module.state.plugins[refreshed_plugin_id] = {
                "id": refreshed_plugin_id,
                "runtime_enabled": True,
                "runtime_auto_start": True,
            }
        return {"success": True, "plugin_id": refreshed_plugin_id}

    monkeypatch.setattr(module.plugin_registry_service, "refresh_plugin", _refresh_plugin)
    monkeypatch.setattr(module, "_get_plugin_config_path", lambda _plugin_id: config_path)
    monkeypatch.setattr(
        module,
        "resolve_plugin_config_from_path",
        lambda *args, **kwargs: {
            "effective_config": kwargs["base_config"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: resolved_plugin_id)
    monkeypatch.setattr(module, "_find_missing_python_requirements", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "PluginProcessHost", _FakeProcessHost)
    monkeypatch.setattr(module, "scan_plugin_metadata_isolated", _metadata_scan_for(type("Plugin", (), {})))
    monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()

        runtime_overrides_module.set_runtime_override("demo_plugin", False)
        if refreshed_plugin_id != "demo_plugin":
            runtime_overrides_module.set_runtime_override(
                refreshed_plugin_id,
                False,
                auto_start=False,
            )

        if persist_fails:
            def _fail_persist(*_args, **_kwargs) -> None:
                raise ServerDomainError(
                    code="PLUGIN_RUNTIME_PREFERENCE_PERSIST_FAILED",
                    message="PLUGIN_RUNTIME_PREFERENCE_PERSIST_FAILED",
                    status_code=500,
                    details={
                        "error_type": "RuntimeOverrideWriteError",
                        "runtime_state_changed": True,
                    },
                )

            monkeypatch.setattr(module, "_persist_user_runtime_intent", _fail_persist)

        response = await module.PluginLifecycleService().start_plugin("demo_plugin", persist_user_intent=True)

        assert response["success"] is True
        assert seen["override_at_refresh"] is False
        if persist_fails:
            assert _isolate_runtime_overrides == {"demo_plugin": False}
            assert response["partial_success"] is True
            assert response["preference_persisted"] is False
            assert response["runtime_state_changed"] is True
        else:
            assert _isolate_runtime_overrides == {
                resolved_plugin_id: {"enabled": True, "auto_start": True},
            }
            assert response["preference_persisted"] is True
        assert response["plugin_id"] == resolved_plugin_id
        if resolved_plugin_id == "demo_plugin":
            with module.state.acquire_plugins_read_lock():
                assert module.state.plugins["demo_plugin"]["runtime_auto_start"] is True
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_refresh_failure_does_not_persist_user_intent(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_runtime_overrides: dict,
) -> None:
    runtime_overrides_module.set_runtime_override(
        "failed_plugin",
        False,
        auto_start=False,
    )

    async def _fail_refresh(_plugin_id: str) -> dict[str, object]:
        raise ServerDomainError(
            code="PLUGIN_REGISTRY_CONFLICT",
            message="conflict",
            status_code=409,
        )

    monkeypatch.setattr(module.plugin_registry_service, "refresh_plugin", _fail_refresh)

    with pytest.raises(ServerDomainError) as exc_info:
        await module.PluginLifecycleService().start_plugin(
            "failed_plugin",
            persist_user_intent=True,
        )

    assert exc_info.value.code == "PLUGIN_REGISTRY_CONFLICT"
    assert _isolate_runtime_overrides == {
        "failed_plugin": {"enabled": False, "auto_start": False},
    }


@pytest.mark.plugin_unit
def test_persist_user_runtime_intent_migrates_resolved_plugin_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], str, bool, bool | None]] = []

    monkeypatch.setattr(module, "PLUGIN_SYNC_AUTO_START_ON_TOGGLE", True)
    monkeypatch.setattr(
        module,
        "migrate_runtime_override",
        lambda sources, target, enabled, *, auto_start=None: calls.append(
            (tuple(sources), target, enabled, auto_start)
        ),
    )

    module._persist_user_runtime_intent(
        "renamed_plugin",
        True,
        previous_plugin_ids=("original_plugin", "intermediate_plugin"),
        runtime_state_changed=True,
    )

    assert calls == [
        (
            ("original_plugin", "intermediate_plugin"),
            "renamed_plugin",
            True,
            True,
        )
    ]


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_checks_python_requirements_against_vendor_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "vendor_adapter" / "plugin.toml"
    vendor_dir = config_path.parent / "vendor"
    vendor_dir.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'vendor_adapter'",
                "name = 'Vendor Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
            ]
        ),
        encoding="utf-8",
    )
    (config_path.parent / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo-lib>=2"]\n',
        encoding="utf-8",
    )

    seen: dict[str, object] = {}

    def _fake_find_missing(requirements, *, search_paths=None):
        seen["requirements"] = list(requirements)
        seen["search_paths"] = list(search_paths or [])
        return []

    monkeypatch.setattr(module.plugin_registry_service, "refresh_plugin", lambda _plugin_id: {"success": True})
    monkeypatch.setattr(module, "_get_plugin_config_path", lambda _plugin_id: config_path)
    monkeypatch.setattr(
        module,
        "resolve_plugin_config_from_path",
        lambda *args, **kwargs: {
            "effective_config": kwargs["base_config"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
    monkeypatch.setattr(module, "_find_missing_python_requirements", _fake_find_missing)
    monkeypatch.setattr(module, "PluginProcessHost", _FakeProcessHost)
    monkeypatch.setattr(module, "scan_plugin_metadata_isolated", _metadata_scan_for(_FakeAdapterPlugin))
    monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()

        response = await module.PluginLifecycleService().start_plugin("vendor_adapter", refresh_registry=False)

        assert response["success"] is True
        assert seen["requirements"] == ["demo-lib>=2"]
        assert seen["search_paths"] == [vendor_dir]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_rejects_entry_directory_mismatch_before_creating_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "repo_file_manager" / "plugin.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'file_manager'",
                "name = 'File Manager'",
                "type = 'plugin'",
                "entry = 'plugins.file_manager:FileManagerPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
            ]
        ),
        encoding="utf-8",
    )
    host_created = False

    class _UnexpectedHost(_FakeProcessHost):
        def __init__(self, *args, **kwargs) -> None:
            nonlocal host_created
            host_created = True
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        module,
        "resolve_plugin_config_from_path",
        lambda *args, **kwargs: {
            "effective_config": kwargs["base_config"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(module, "PluginProcessHost", _UnexpectedHost)

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["file_manager"] = {
                "id": "file_manager",
                "config_path": str(config_path),
                "runtime_enabled": True,
                "runtime_auto_start": False,
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin("file_manager", refresh_registry=False)

        assert exc_info.value.code == "PLUGIN_ENTRY_DIRECTORY_MISMATCH"
        assert exc_info.value.status_code == 400
        assert "repo_file_manager" in exc_info.value.message
        assert host_created is False
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_clamps_its_startup_timeout_to_the_caller_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The clamp has to reach the host, not just exist as a helper.

    ``reload_all_plugins`` hands each start whatever is left of the round
    budget; if ``start_plugin`` accepted that number and then ignored it, the
    reload would still run past its advertised wall clock while every unit test
    on the helper kept passing. So this asserts on the value the host was
    actually started with (codex / CodeRabbit / Greptile).

    Mutation: delete the clamp from ``start_plugin`` and pass the configured
    timeout straight through.
    """
    config_path = tmp_path / "clamped_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'clamped_adapter'",
                "name = 'Clamped Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
            ]
        ),
        encoding="utf-8",
    )

    class _RecordingHost(_FakeProcessHost):
        instances: list["_RecordingHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            _RecordingHost.instances.append(self)

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["clamped_adapter"] = {
                "id": "clamped_adapter",
                "name": "Clamped Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(module, "PLUGIN_STARTUP_TIMEOUT", 10.0, raising=False)
        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _RecordingHost)
        inner_scan = _metadata_scan_for(_FakeAdapterPlugin)
        scan_timeouts: list[object] = []

        def _recording_scan(**kwargs):
            scan_timeouts.append(kwargs.get("timeout"))
            kwargs.pop("timeout", None)
            return inner_scan(**kwargs)

        monkeypatch.setattr(module, "scan_plugin_metadata_isolated", _recording_scan)
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        response = await module.PluginLifecycleService().start_plugin(
            "clamped_adapter",
            refresh_registry=False,
            start_deadline=time.monotonic() + 2.5,
        )

        assert response["success"] is True
        assert _RecordingHost.instances
        assert _RecordingHost.instances[0].startup_timeout == pytest.approx(2.5, abs=0.5), (
            f"剩余预算没有压到真正启动 host 的那一步：{_RecordingHost.instances[0].startup_timeout}"
        )
        # 扫描排在 host 之后，而它自己的上限是 10s——不一起钳住的话，预算只管住了
        # 这一次启动里的一步。
        assert scan_timeouts, "前提没成立：没有走到元数据扫描"
        assert scan_timeouts[0] == pytest.approx(2.5, abs=0.6), (
            f"元数据扫描没有按剩余预算收窄：{scan_timeouts}"
        )
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_uses_default_startup_timeout_when_runtime_timeout_omitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "default_timeout_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'default_timeout_adapter'",
                "name = 'Default Timeout Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
            ]
        ),
        encoding="utf-8",
    )

    class _RecordingHost(_FakeProcessHost):
        instances: list["_RecordingHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            _RecordingHost.instances.append(self)

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["default_timeout_adapter"] = {
                "id": "default_timeout_adapter",
                "name": "Default Timeout Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(module, "PLUGIN_STARTUP_TIMEOUT", 0.01, raising=False)
        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _RecordingHost)
        monkeypatch.setattr(
            module,
            "scan_plugin_metadata_isolated",
            _metadata_scan_for(_FakeAdapterPlugin),
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        response = await module.PluginLifecycleService().start_plugin(
            "default_timeout_adapter",
            refresh_registry=False,
        )

        assert response["success"] is True
        assert _RecordingHost.instances
        assert _RecordingHost.instances[0].startup_timeout == 0.01
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_literal", ["0", "300.1"])
async def test_start_plugin_rejects_invalid_runtime_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    timeout_literal: str,
) -> None:
    config_path = tmp_path / "invalid_timeout_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'invalid_timeout_adapter'",
                "name = 'Invalid Timeout Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                f"timeout = {timeout_literal}",
            ]
        ),
        encoding="utf-8",
    )

    class _RecordingHost(_FakeProcessHost):
        instances: list["_RecordingHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            _RecordingHost.instances.append(self)

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["invalid_timeout_adapter"] = {
                "id": "invalid_timeout_adapter",
                "name": "Invalid Timeout Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _RecordingHost)
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin(
                "invalid_timeout_adapter",
                refresh_registry=False,
            )

        assert exc_info.value.code == "INVALID_PLUGIN_CONFIG"
        assert "timeout" in exc_info.value.message
        assert _RecordingHost.instances == []
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_rejects_invalid_default_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid_default_timeout_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'invalid_default_timeout_adapter'",
                "name = 'Invalid Default Timeout Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
            ]
        ),
        encoding="utf-8",
    )

    class _RecordingHost(_FakeProcessHost):
        instances: list["_RecordingHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            _RecordingHost.instances.append(self)

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["invalid_default_timeout_adapter"] = {
                "id": "invalid_default_timeout_adapter",
                "name": "Invalid Default Timeout Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(module, "PLUGIN_STARTUP_TIMEOUT", 300.1, raising=False)
        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _RecordingHost)
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin(
                "invalid_default_timeout_adapter",
                refresh_registry=False,
            )

        assert exc_info.value.code == "INVALID_PLUGIN_CONFIG"
        assert exc_info.value.details["error_type"] == "InvalidStartupTimeout"
        assert "PLUGIN_STARTUP_TIMEOUT" in exc_info.value.message
        assert _RecordingHost.instances == []
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_defaults_startup_failure_to_warn_and_marks_degraded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "warn_startup_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'warn_startup_adapter'",
                "name = 'Warn Startup Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
            ]
        ),
        encoding="utf-8",
    )

    class _StartupWarningHost(_FakeProcessHost):
        instances: list["_StartupWarningHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            _StartupWarningHost.instances.append(self)

        async def start(
            self,
            message_target_queue: object,
            startup_timeout: float | None = None,
            startup_failure: str = "warn",
        ) -> dict[str, object]:
            self.startup_timeout = startup_timeout
            self.startup_failure = startup_failure
            self.started = True
            return {"status": "failed", "startup_error": "lifecycle.startup failed"}

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["warn_startup_adapter"] = {
                "id": "warn_startup_adapter",
                "name": "Warn Startup Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _StartupWarningHost)
        monkeypatch.setattr(
            module,
            "scan_plugin_metadata_isolated",
            _metadata_scan_for(_FakeAdapterPlugin),
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        response = await module.PluginLifecycleService().start_plugin(
            "warn_startup_adapter",
            refresh_registry=False,
        )

        assert response["success"] is True
        assert response["startup_degraded"] is True
        assert response["startup_error"] == "lifecycle.startup failed"
        assert _StartupWarningHost.instances[0].startup_failure == "warn"
        assert _StartupWarningHost.instances[0].stopped is False
        with module.state.acquire_plugins_read_lock():
            meta = module.state.plugins["warn_startup_adapter"]
        assert meta["runtime_startup_state"] == "degraded"
        assert meta["runtime_startup_error"] == "lifecycle.startup failed"
        with module.state.acquire_plugin_hosts_read_lock():
            assert "warn_startup_adapter" in module.state.plugin_hosts
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_startup_failure_fail_keeps_startup_error_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "fail_startup_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'fail_startup_adapter'",
                "name = 'Fail Startup Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "startup_failure = 'fail'",
            ]
        ),
        encoding="utf-8",
    )

    class _StrictStartupHost(_FakeProcessHost):
        instances: list["_StrictStartupHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            _StrictStartupHost.instances.append(self)

        async def start(
            self,
            message_target_queue: object,
            startup_timeout: float | None = None,
            startup_failure: str = "warn",
        ) -> dict[str, object]:
            self.startup_timeout = startup_timeout
            self.startup_failure = startup_failure
            self.started = True
            if startup_failure == "fail":
                raise PluginLifecycleError(self.plugin_id, "startup", "lifecycle.startup failed")
            return {"status": "failed", "startup_error": "lifecycle.startup failed"}

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["fail_startup_adapter"] = {
                "id": "fail_startup_adapter",
                "name": "Fail Startup Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _StrictStartupHost)
        monkeypatch.setattr(
            module,
            "scan_plugin_metadata_isolated",
            _metadata_scan_for(_FakeAdapterPlugin),
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin("fail_startup_adapter", refresh_registry=False)

        assert exc_info.value.code == "PLUGIN_START_FAILED"
        assert _StrictStartupHost.instances
        host = _StrictStartupHost.instances[0]
        assert host.startup_failure == "fail"
        assert host.stopped is True
        with module.state.acquire_plugin_hosts_read_lock():
            assert "fail_startup_adapter" not in module.state.plugin_hosts
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_does_not_map_startup_business_timeout_to_start_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "business_timeout_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'business_timeout_adapter'",
                "name = 'Business Timeout Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "startup_failure = 'fail'",
            ]
        ),
        encoding="utf-8",
    )

    class _BusinessTimeoutHost(_FakeProcessHost):
        instances: list["_BusinessTimeoutHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            _BusinessTimeoutHost.instances.append(self)

        async def start(
            self,
            message_target_queue: object,
            startup_timeout: float | None = None,
            startup_failure: str = "warn",
        ) -> None:
            self.startup_timeout = startup_timeout
            self.startup_failure = startup_failure
            self.started = True
            raise PluginLifecycleError(self.plugin_id, "startup", "database timeout while connecting")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["business_timeout_adapter"] = {
                "id": "business_timeout_adapter",
                "name": "Business Timeout Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _BusinessTimeoutHost)
        monkeypatch.setattr(
            module,
            "scan_plugin_metadata_isolated",
            _metadata_scan_for(_FakeAdapterPlugin),
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin("business_timeout_adapter", refresh_registry=False)

        assert exc_info.value.code == "PLUGIN_START_FAILED"
        assert exc_info.value.status_code == 500
        assert exc_info.value.details["error_type"] == "PluginLifecycleError"
        assert "database timeout while connecting" in exc_info.value.message
        assert _BusinessTimeoutHost.instances[0].stopped is True
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_applies_runtime_startup_timeout_to_legacy_host_and_cleans_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "slow_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'slow_adapter'",
                "name = 'Slow Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "timeout = 0.01",
            ]
        ),
        encoding="utf-8",
    )

    class _SlowProcessHost(_FakeProcessHost):
        instances: list["_SlowProcessHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.shutdown_timeout: float | None = None
            _SlowProcessHost.instances.append(self)

        async def start(self, message_target_queue: object) -> None:
            self.started = True
            await asyncio.sleep(0.05)

        async def shutdown(self, timeout: float = module.PLUGIN_SHUTDOWN_TIMEOUT) -> None:
            self.stopped = True
            self.shutdown_timeout = timeout

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["slow_adapter"] = {
                "id": "slow_adapter",
                "name": "Slow Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _SlowProcessHost)
        monkeypatch.setattr(
            module,
            "scan_plugin_metadata_isolated",
            _metadata_scan_for(_FakeAdapterPlugin),
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin("slow_adapter", refresh_registry=False)

        assert exc_info.value.code == "PLUGIN_START_TIMEOUT"
        assert _SlowProcessHost.instances
        assert _SlowProcessHost.instances[0].stopped is True
        with module.state.acquire_plugin_hosts_read_lock():
            assert "slow_adapter" not in module.state.plugin_hosts
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_lets_timeout_aware_host_own_startup_timeout_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "aware_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'aware_adapter'",
                "name = 'Aware Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "timeout = 0.01",
            ]
        ),
        encoding="utf-8",
    )

    class _TimeoutAwareHost(_FakeProcessHost):
        instances: list["_TimeoutAwareHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.cancelled = False
            self.startup_cleanup_ran = False
            _TimeoutAwareHost.instances.append(self)

        async def start(self, message_target_queue: object, startup_timeout: float | None = None) -> None:
            self.started = True
            self.startup_timeout = startup_timeout
            try:
                await asyncio.sleep(float(startup_timeout or 0.01) * 2)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.startup_cleanup_ran = True
            raise PluginLifecycleError(
                self.plugin_id,
                "startup",
                f"startup timed out after {startup_timeout}s",
            )

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["aware_adapter"] = {
                "id": "aware_adapter",
                "name": "Aware Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _TimeoutAwareHost)
        monkeypatch.setattr(
            module,
            "scan_plugin_metadata_isolated",
            _metadata_scan_for(_FakeAdapterPlugin),
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin("aware_adapter", refresh_registry=False)

        assert exc_info.value.code == "PLUGIN_START_TIMEOUT"
        assert _TimeoutAwareHost.instances
        host = _TimeoutAwareHost.instances[0]
        assert host.startup_timeout == 0.01
        assert host.cancelled is False
        assert host.startup_cleanup_ran is True
        assert host.stopped is True
        with module.state.acquire_plugin_hosts_read_lock():
            assert "aware_adapter" not in module.state.plugin_hosts
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_classifies_exponent_form_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "tiny_timeout_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'tiny_timeout_adapter'",
                "name = 'Tiny Timeout Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "timeout = 1e-6",
            ]
        ),
        encoding="utf-8",
    )

    class _ExponentTimeoutHost(_FakeProcessHost):
        instances: list["_ExponentTimeoutHost"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            _ExponentTimeoutHost.instances.append(self)

        async def start(self, message_target_queue: object, startup_timeout: float | None = None) -> None:
            self.started = True
            self.startup_timeout = startup_timeout
            raise PluginLifecycleError(
                self.plugin_id,
                "startup",
                f"startup timed out after {startup_timeout}s",
            )

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["tiny_timeout_adapter"] = {
                "id": "tiny_timeout_adapter",
                "name": "Tiny Timeout Adapter",
                "type": "adapter",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _ExponentTimeoutHost)
        monkeypatch.setattr(
            module,
            "scan_plugin_metadata_isolated",
            _metadata_scan_for(_FakeAdapterPlugin),
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin("tiny_timeout_adapter", refresh_registry=False)

        assert exc_info.value.code == "PLUGIN_START_TIMEOUT"
        assert exc_info.value.status_code == 504
        assert _ExponentTimeoutHost.instances
        host = _ExponentTimeoutHost.instances[0]
        assert host.startup_timeout == 1e-6
        assert host.stopped is True
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_persists_entries_preview_and_invalidates_stale_caches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'mcp_adapter'",
                "name = 'MCP Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "short_description = 'persist me'",
                "keywords = ['mcp']",
                "",
                "[plugin.sdk]",
                "supported = '>=0.1'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
            ]
        ),
        encoding="utf-8",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["mcp_adapter"] = {
                "id": "mcp_adapter",
                "name": "MCP Adapter",
                "type": "adapter",
                "description": "",
                "version": "0.1.0",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
                "short_description": "persist me",
                "keywords": ["mcp"],
                "sdk_supported": ">=0.1",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        now = time.time()
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache["plugins"] = {"data": {}, "timestamp": now}
            module.state._snapshot_cache["hosts"] = {"data": {}, "timestamp": now}
            module.state._snapshot_cache["handlers"] = {"data": {}, "timestamp": now}

        monkeypatch.setattr(module, "_get_plugin_config_path", lambda plugin_id: config_path)
        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _FakeProcessHost)
        monkeypatch.setattr(module, "scan_plugin_metadata_isolated", _metadata_scan_for(_FakeAdapterPlugin))
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        service = module.PluginLifecycleService()
        response = await service.start_plugin("mcp_adapter")

        assert response["success"] is True
        assert response["plugin_id"] == "mcp_adapter"

        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["mcp_adapter"])
        assert plugin_meta["runtime_enabled"] is True
        assert plugin_meta["runtime_auto_start"] is False
        assert [entry["id"] for entry in plugin_meta["entries_preview"]] == ["list_servers"]

        plugin_list = query_module._build_plugin_list_sync()
        plugin_info = next(item for item in plugin_list if item["id"] == "mcp_adapter")
        assert plugin_info["status"] == "running"
        assert [entry["id"] for entry in plugin_info["entries"]] == ["list_servers"]
        assert plugin_meta["short_description"] == "persist me"
        assert plugin_meta["keywords"] == ["mcp"]
        assert plugin_meta["sdk_supported"] == ">=0.1"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_logs_structured_config_warnings_from_resolver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "warn_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'warn_adapter'",
                "name = 'Warn Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
            ]
        ),
        encoding="utf-8",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    capture_logger = _CaptureLogger()

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(module, "_get_plugin_config_path", lambda plugin_id: config_path)
        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [
                    {
                        "code": "PLUGIN_ENTRY_WHITESPACE",
                        "field": "plugin.entry",
                        "message": "entry has leading/trailing whitespace",
                        "severity": "warning",
                        "source": "semantic",
                    }
                ],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _FakeProcessHost)
        monkeypatch.setattr(module, "scan_plugin_metadata_isolated", _metadata_scan_for(_FakeAdapterPlugin))
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)
        monkeypatch.setattr(module, "logger", capture_logger)

        service = module.PluginLifecycleService()
        response = await service.start_plugin("warn_adapter")

        assert response["success"] is True
        assert any(
            "Plugin config warning [PLUGIN_ENTRY_WHITESPACE] field=plugin.entry msg=entry has leading/trailing whitespace"
            in message
            for message in capture_logger.messages
        )
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_maps_resolver_http_exception_to_profile_domain_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "demo" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo'\nentry='demo:Plugin'\n", encoding="utf-8")

    monkeypatch.setattr(module, "_get_plugin_config_path", lambda plugin_id: config_path)

    def _raise_profile_error(*args, **kwargs):
        raise HTTPException(status_code=422, detail="profile merge failed")

    monkeypatch.setattr(module, "resolve_plugin_config_from_path", _raise_profile_error)

    service = module.PluginLifecycleService()

    with pytest.raises(ServerDomainError) as exc_info:
        await service.start_plugin("demo")

    assert exc_info.value.code == "PLUGIN_CONFIG_PROFILE_FAILED"
    assert exc_info.value.status_code == 422
    assert exc_info.value.message == "profile merge failed"
    assert exc_info.value.details["plugin_id"] == "demo"


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_allows_retry_for_load_failed_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "broken_adapter" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'broken_adapter'",
                "name = 'Broken Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
            ]
        ),
        encoding="utf-8",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["broken_adapter"] = {
                "id": "broken_adapter",
                "name": "Broken Adapter",
                "type": "adapter",
                "description": "",
                "version": "0.1.0",
                "sdk_version": "test",
                "config_path": str(config_path),
                "entry_point": "tests.fake_mcp:FakeAdapterPlugin",
                "runtime_load_state": "failed",
                "runtime_load_error_message": "Missing Python dependencies: ['demo-lib>=2']",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(module, "_get_plugin_config_path", lambda plugin_id: config_path)
        monkeypatch.setattr(
            module,
            "resolve_plugin_config_from_path",
            lambda *args, **kwargs: {
                "effective_config": kwargs["base_config"],
                "warnings": [],
            },
        )
        monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(module, "PluginProcessHost", _FakeProcessHost)
        monkeypatch.setattr(module, "scan_plugin_metadata_isolated", _metadata_scan_for(_FakeAdapterPlugin))
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        service = module.PluginLifecycleService()
        response = await service.start_plugin("broken_adapter")

        assert response["success"] is True
        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["broken_adapter"])
        assert plugin_meta["runtime_enabled"] is True
        assert "runtime_load_state" not in plugin_meta
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.plugin_unit
def test_parse_single_plugin_config_uses_resolver_effective_config_and_logs_warnings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    config_path = plugin_dir / "plugin.toml"
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'demo_plugin'",
                "entry = 'demo.module:Plugin'",
                "name = 'Base Name'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = true",
            ]
        ),
        encoding="utf-8",
    )

    capture_logger = _CaptureLogger()

    monkeypatch.setattr(
        registry_module,
        "resolve_plugin_config_from_path",
        lambda *args, **kwargs: {
            "effective_config": {
                "plugin": {
                    "id": "demo_plugin",
                    "entry": "demo.module:Plugin",
                    "name": "Overlay Name",
                    "description": "resolved by profile",
                },
                "plugin_runtime": {
                    "enabled": False,
                    "auto_start": False,
                },
            },
            "warnings": [
                {
                    "code": "PLUGIN_KEYWORDS_NON_LIST",
                    "field": "plugin.keywords",
                    "message": "keywords should be a string list",
                    "severity": "warning",
                    "source": "semantic",
                }
            ],
        },
    )

    parsed = registry_module._parse_single_plugin_config(
        config_path,
        set(),
        capture_logger,
    )

    assert parsed is not None
    assert parsed.pdata["name"] == "Overlay Name"
    assert parsed.pdata["description"] == "resolved by profile"
    assert parsed.enabled is False
    assert parsed.auto_start is False
    assert any(
        "Plugin config warning [PLUGIN_KEYWORDS_NON_LIST] field=plugin.keywords msg=keywords should be a string list"
        in message
        for message in capture_logger.messages
    )


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_plugin_removes_directory_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    config_path = plugin_dir / "plugin.toml"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo_plugin'\nentry='tests.fake:Plugin'\n", encoding="utf-8")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    refresh_calls: list[str] = []
    events: list[dict[str, object]] = []
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "default.toml").write_text("[profile]\n", encoding="utf-8")
    install_source_manager = _FakeInstallSourceManager(package_id="demo_package")

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["demo_plugin"] = {
                "id": "demo_plugin",
                "name": "Demo Plugin",
                "type": "plugin",
                "config_path": str(config_path),
                "entry_point": "tests.fake:Plugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers["demo_plugin.plugin_entry:run"] = object()

        async def _refresh_registry() -> dict[str, object]:
            refresh_calls.append("refresh")
            return {"success": True}

        monkeypatch.setattr(uninstall_module, "PLUGIN_CONFIG_ROOTS", (tmp_path,))
        monkeypatch.setattr(
            uninstall_module.plugin_registry_service,
            "refresh_registry",
            _refresh_registry,
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: events.append(dict(event)))
        monkeypatch.setattr(
            uninstall_module, "get_install_source_manager", lambda: install_source_manager
        )
        monkeypatch.setattr(
            uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
        )

        service = module.PluginLifecycleService()
        response = await service.delete_plugin("demo_plugin")

        assert response["success"] is True
        assert response["plugin_id"] == "demo_plugin"
        assert response["deleted_from_disk"] is True
        assert response["preference_action"] == "cleared"
        assert response["runtime_restart"] == "not_needed"
        assert refresh_calls == ["refresh"]
        assert plugin_dir.exists() is False
        assert profile_dir.exists() is False
        assert install_source_manager.marked_removed == [plugin_dir]
        with module.state.acquire_plugins_read_lock():
            assert "demo_plugin" not in module.state.plugins
        with module.state.acquire_event_handlers_read_lock():
            assert "demo_plugin.plugin_entry:run" not in module.state.event_handlers
        assert any(event.get("type") == "plugin_deleted" for event in events)
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_plugin_restores_profile_and_restarts_after_code_staging_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "running_plugin"
    config_path = plugin_dir / "plugin.toml"
    plugin_dir.mkdir(parents=True)
    config_path.write_text("[plugin]\nid='running_plugin'\nentry='tests.fake:Plugin'\n", encoding="utf-8")
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "running_package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "settings.toml").write_text("[settings]\nvalue='keep'\n", encoding="utf-8")
    install_source_manager = _FakeInstallSourceManager(package_id="running_package")
    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    restart_calls: list[str] = []

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["running_plugin"] = {
                "id": "running_plugin",
                "config_path": str(config_path),
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts["running_plugin"] = object()

        async def _stop_plugin(self, plugin_id: str, **_kwargs: object) -> dict[str, object]:
            assert plugin_id == "running_plugin"
            with module.state.acquire_plugin_hosts_write_lock():
                module.state.plugin_hosts.pop(plugin_id, None)
            return {"success": True}

        async def _start_plugin(self, plugin_id: str, **_kwargs: object) -> dict[str, object]:
            restart_calls.append(plugin_id)
            return {"success": True}

        def _fail_code_staging(path: Path) -> object:
            assert path == plugin_dir
            raise PermissionError("plugin is in use")

        monkeypatch.setattr(uninstall_module, "PLUGIN_CONFIG_ROOTS", (tmp_path,))
        monkeypatch.setattr(
            uninstall_module, "get_install_source_manager", lambda: install_source_manager
        )
        monkeypatch.setattr(
            uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
        )
        monkeypatch.setattr(module.PluginLifecycleService, "stop_plugin", _stop_plugin)
        monkeypatch.setattr(module.PluginLifecycleService, "start_plugin", _start_plugin)
        monkeypatch.setattr(
            uninstall_module, "_stage_plugin_code_sync", _fail_code_staging
        )

        with pytest.raises(ServerDomainError, match="Failed to delete plugin") as captured:
            await module.PluginLifecycleService().delete_plugin("running_plugin")

        assert captured.value.details["stage"] == "stage_code"
        assert captured.value.details["filesystem_rollback"] == "completed"
        assert captured.value.details["runtime_restart"] == "succeeded"
        assert profile_dir.is_dir()
        assert (profile_dir / "settings.toml").is_file()
        assert plugin_dir.is_dir()
        assert config_path.is_file()
        assert restart_calls == ["running_plugin"]
        assert install_source_manager.marked_removed == []
        assert install_source_manager.restored_entries == []
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)


@pytest.mark.plugin_unit
def test_delete_keeps_profile_shared_by_another_bundle_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "first_bundle_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "shared_bundle"
    profile_dir.mkdir(parents=True)
    (profile_dir / "default.toml").write_text("[profile]\n", encoding="utf-8")
    install_source_manager = _FakeInstallSourceManager(
        package_id="shared_bundle",
        active_package_ids=("shared_bundle",),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is None
    assert profile_dir.is_dir()
    assert install_source_manager.marked_removed == []


@pytest.mark.plugin_unit
def test_delete_uses_plugin_directory_name_for_legacy_empty_package_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "legacy_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_dir.name
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(package_id="")
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is not None
    assert staged_profile.original_dir == profile_dir
    assert profile_dir.exists() is False
    assert uninstall_module._finalize_staged_package_profile_sync(staged_profile) == profile_dir
    assert staged_profile.staged_dir.exists() is False


@pytest.mark.plugin_unit
def test_delete_legacy_empty_profile_dir_uses_explicit_config_root_sibling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom_exec_root = tmp_path / "custom" / "plugins"
    plugin_dir = custom_exec_root / "legacy_plugin"
    legacy_profiles_root = custom_exec_root.parent / ".neko-package-profiles"
    profile_dir = legacy_profiles_root / "legacy_package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "settings.toml").write_text("[settings]\nkeep = true\n", encoding="utf-8")
    install_source_manager = _FakeInstallSourceManager(
        package_id="legacy_package",
        profile_dir="",
    )
    monkeypatch.setenv("PLUGIN_CONFIG_ROOT", str(custom_exec_root))
    monkeypatch.delenv("PACKAGE_PROFILES_ROOT", raising=False)
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is not None
    assert staged_profile.original_dir == profile_dir.resolve()
    uninstall_module._restore_staged_package_profile_sync(staged_profile)
    assert (profile_dir / "settings.toml").is_file()


@pytest.mark.plugin_unit
def test_delete_skips_legacy_inference_when_another_legacy_row_is_installed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bundle predating package ids can hide a sibling that shares the profile.

    Rows written before package ids were tracked name no owner, and a bundle
    profile is named after the package rather than after any member plugin. The
    sibling therefore resolves to a different path and the sharing check cannot
    see it, so deleting the member whose id matches the bundle would take the
    sibling's configuration with it.
    """
    plugin_dir = tmp_path / "legacy_bundle_member"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_dir.name
    profile_dir.mkdir(parents=True)
    (profile_dir / "sibling_config.toml").write_text("[keep]\nme = true\n", encoding="utf-8")
    install_source_manager = _FakeInstallSourceManager(
        package_id="",
        active_package_ids=("",),
        active_directory_names=("legacy_bundle_sibling",),
        active_channels=("imported",),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    assert uninstall_module._stage_orphaned_package_profile_sync(plugin_dir) is None
    assert (profile_dir / "sibling_config.toml").is_file()


@pytest.mark.plugin_unit
def test_delete_skips_cleanup_when_a_sibling_row_lacks_a_package_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The unidentifiable row can be the sibling rather than the one deleted.

    A pre-package-id bundle whose members were reinstalled one at a time leaves
    mixed rows. Resolving our own profile works, but the legacy sibling still
    resolves to its plugin id, so the sharing check cannot tell that it shares
    this profile.
    """
    plugin_dir = tmp_path / "shared_pkg"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "shared_pkg"
    profile_dir.mkdir(parents=True)
    (profile_dir / "sibling_config.toml").write_text("[keep]\nme = true\n", encoding="utf-8")
    install_source_manager = _FakeInstallSourceManager(
        package_id="shared_pkg",
        profile_dir=str(profile_dir),
        profile_installed=True,
        active_package_ids=("",),
        active_directory_names=("shared_b",),
        active_channels=("imported",),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    assert uninstall_module._stage_orphaned_package_profile_sync(plugin_dir) is None
    assert (profile_dir / "sibling_config.toml").is_file()


@pytest.mark.plugin_unit
def test_delete_still_cleans_legacy_profile_when_other_rows_record_package_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The legacy guard must not block cleanup once siblings are identifiable."""
    plugin_dir = tmp_path / "legacy_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_dir.name
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(
        package_id="",
        active_package_ids=("some_other_package",),
        active_directory_names=("some_other_plugin",),
        active_channels=("imported",),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is not None
    assert profile_dir.exists() is False


@pytest.mark.plugin_unit
def test_delete_does_not_infer_profile_ownership_for_manual_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "manual_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_dir.name
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(package_id="", channel="manual")
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    assert uninstall_module._stage_orphaned_package_profile_sync(plugin_dir) is None
    assert profile_dir.is_dir()


@pytest.mark.plugin_unit
def test_delete_does_not_infer_profile_ownership_for_new_profileless_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "profileless_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "profileless_package"
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(
        package_id="profileless_package",
        profile_installed=False,
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    assert uninstall_module._stage_orphaned_package_profile_sync(plugin_dir) is None
    assert profile_dir.is_dir()


@pytest.mark.plugin_unit
def test_delete_keeps_profile_shared_by_same_named_plugin_in_another_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "same_name"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "shared_package"
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(
        package_id="shared_package",
        profile_dir=str(profile_dir),
        root_id="user",
        active_package_ids=("shared_package",),
        active_profile_dirs=(str(profile_dir),),
        active_root_ids=("builtin",),
        active_directory_names=(plugin_dir.name,),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    assert uninstall_module._stage_orphaned_package_profile_sync(plugin_dir) is None
    assert profile_dir.is_dir()


@pytest.mark.plugin_unit
def test_delete_does_not_treat_manual_plugin_as_package_profile_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "imported_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "shared_package"
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(
        package_id="shared_package",
        profile_dir=str(profile_dir),
        active_package_ids=("shared_package",),
        active_profile_dirs=(str(profile_dir),),
        active_channels=("manual",),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is not None
    assert uninstall_module._finalize_staged_package_profile_sync(staged_profile) == profile_dir
    assert profile_dir.exists() is False


@pytest.mark.plugin_unit
def test_delete_removes_profile_from_recorded_custom_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "custom_profile_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "custom" / "custom_package"
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(
        package_id="custom_package",
        profile_dir=str(profile_dir),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is not None
    assert uninstall_module._finalize_staged_package_profile_sync(staged_profile) == profile_dir
    assert profile_dir.exists() is False


@pytest.mark.plugin_unit
def test_delete_removes_recorded_profile_after_profile_root_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "root_changed_plugin"
    old_profiles_root = tmp_path / "old_profiles"
    current_profiles_root = tmp_path / "current_profiles"
    profile_dir = old_profiles_root / "root_changed_package"
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(
        package_id="root_changed_package",
        profile_dir=str(profile_dir),
        profile_installed=True,
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module,
        "get_user_package_profiles_root",
        lambda: current_profiles_root,
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is not None
    assert uninstall_module._finalize_staged_package_profile_sync(staged_profile) == profile_dir
    assert profile_dir.exists() is False


@pytest.mark.plugin_unit
def test_delete_preserves_recorded_profile_under_plugin_state_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "exec" / "legacy_plugin"
    state_root = tmp_path / "state" / "plugins"
    profile_dir = state_root / "legacy_package"
    state_file = profile_dir / "data" / "state.db"
    state_file.parent.mkdir(parents=True)
    state_file.write_bytes(b"persistent legacy state")
    install_source_manager = _FakeInstallSourceManager(
        package_id="legacy_package",
        profile_dir=str(profile_dir),
        profile_installed=True,
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: tmp_path / "profiles"
    )
    monkeypatch.setattr(uninstall_module, "get_plugin_state_root", lambda: state_root)

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is None
    assert state_file.read_bytes() == b"persistent legacy state"
    assert not list(state_root.glob(".*.deleting-*"))


@pytest.mark.plugin_unit
def test_delete_preserves_recorded_profile_under_builtin_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "exec" / "legacy_plugin"
    builtin_root = tmp_path / "builtin"
    profile_dir = builtin_root / "legacy_package"
    builtin_manifest = profile_dir / "plugin.toml"
    builtin_manifest.parent.mkdir(parents=True)
    builtin_manifest.write_bytes(b"immutable builtin")
    install_source_manager = _FakeInstallSourceManager(
        package_id="legacy_package",
        profile_dir=str(profile_dir),
        profile_installed=True,
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: tmp_path / "profiles"
    )
    monkeypatch.setattr(uninstall_module, "get_plugin_state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(uninstall_module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is None
    assert builtin_manifest.read_bytes() == b"immutable builtin"
    assert not list(builtin_root.glob(".*.deleting-*"))


@pytest.mark.plugin_unit
def test_delete_refuses_symlinked_recorded_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "symlinked_profile_plugin"
    profiles_root = tmp_path / "profiles"
    profile_link = profiles_root / "symlinked_package"
    install_source_manager = _FakeInstallSourceManager(
        package_id="symlinked_package",
        profile_dir=str(profile_link),
        profile_installed=True,
    )
    original_is_symlink = Path.is_symlink

    def _is_symlink(path: Path) -> bool:
        return path == profile_link or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    assert uninstall_module._stage_orphaned_package_profile_sync(plugin_dir) is None
    assert install_source_manager.marked_removed == []


@pytest.mark.plugin_unit
def test_delete_refuses_recorded_profile_below_symlinked_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "symlinked_ancestor_plugin"
    profiles_root = tmp_path / "profiles"
    symlinked_ancestor = tmp_path / "old_profiles"
    profile_dir = symlinked_ancestor / "symlinked_package"
    install_source_manager = _FakeInstallSourceManager(
        package_id="symlinked_package",
        profile_dir=str(profile_dir),
        profile_installed=True,
    )
    original_is_symlink = Path.is_symlink

    def _is_symlink(path: Path) -> bool:
        return path == symlinked_ancestor or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    assert uninstall_module._stage_orphaned_package_profile_sync(plugin_dir) is None
    assert install_source_manager.marked_removed == []
@pytest.mark.plugin_unit
def test_delete_removes_profile_not_shared_at_a_different_custom_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "first_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "first" / "shared_package"
    other_profile_dir = profiles_root / "second" / "shared_package"
    profile_dir.mkdir(parents=True)
    other_profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(
        package_id="shared_package",
        profile_dir=str(profile_dir),
        active_package_ids=("shared_package",),
        active_profile_dirs=(str(other_profile_dir),),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is not None
    assert uninstall_module._finalize_staged_package_profile_sync(staged_profile) == profile_dir
    assert profile_dir.exists() is False
    assert other_profile_dir.is_dir()


@pytest.mark.plugin_unit
def test_delete_ignores_install_source_listing_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(
        package_id="demo_package",
        list_entries_error=uninstall_module.InstallSourceError("lock_read_failed"),
    )
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    staged_profile = uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert staged_profile is None
    assert profile_dir.is_dir()
    assert install_source_manager.marked_removed == []


@pytest.mark.plugin_unit
def test_delete_propagates_profile_staging_failure_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    install_source_manager = _FakeInstallSourceManager(package_id="demo_package")
    monkeypatch.setattr(
        uninstall_module, "get_install_source_manager", lambda: install_source_manager
    )
    monkeypatch.setattr(
        uninstall_module, "get_user_package_profiles_root", lambda: profiles_root
    )

    def _fail_to_stage(self: Path, target: Path) -> Path:
        assert self == profile_dir
        assert target.name.startswith(".demo_package.deleting-")
        raise PermissionError("profile is in use")

    monkeypatch.setattr(uninstall_module.Path, "replace", _fail_to_stage)

    with pytest.raises(PermissionError, match="profile is in use"):
        uninstall_module._stage_orphaned_package_profile_sync(plugin_dir)

    assert install_source_manager.marked_removed == []
    assert profile_dir.is_dir()


@pytest.mark.plugin_unit
def test_deferred_profile_cleanup_is_persisted_and_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "package_profile_cleanup.json"
    staged_dir = tmp_path / "profiles" / ".demo_package.deleting-0123456789abcdef0123456789abcdef"
    staged_dir.mkdir(parents=True)
    (staged_dir / "config.toml").write_text("value = true\n", encoding="utf-8")
    staged_profile = uninstall_module._StagedPackageProfile(
        original_dir=tmp_path / "profiles" / "demo_package",
        staged_dir=staged_dir,
    )
    monkeypatch.setattr(
        uninstall_module,
        "_deferred_profile_cleanup_record_path_sync",
        lambda: record_path,
    )

    assert uninstall_module._record_deferred_profile_cleanup_sync(staged_profile) is True
    assert record_path.is_file()
    assert uninstall_module.retry_deferred_profile_cleanup_sync() == 1
    assert staged_dir.exists() is False
    assert record_path.exists() is False


@pytest.mark.plugin_unit
def test_deferred_profile_cleanup_retries_dotted_package_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``package_id`` may contain dots, so the staged name must still be recognised."""
    record_path = tmp_path / "package_profile_cleanup.json"
    staged_dir = (
        tmp_path / "profiles" / (".com.example.demo.deleting-" + "0123456789abcdef" * 2)
    )
    staged_dir.mkdir(parents=True)
    (staged_dir / "config.toml").write_text("value = true\n", encoding="utf-8")
    staged_profile = uninstall_module._StagedPackageProfile(
        original_dir=tmp_path / "profiles" / "com.example.demo",
        staged_dir=staged_dir,
    )
    monkeypatch.setattr(
        uninstall_module,
        "_deferred_profile_cleanup_record_path_sync",
        lambda: record_path,
    )

    assert uninstall_module._record_deferred_profile_cleanup_sync(staged_profile) is True
    assert uninstall_module.retry_deferred_profile_cleanup_sync() == 1
    assert staged_dir.exists() is False


@pytest.mark.plugin_unit
def test_deferred_profile_cleanup_migrates_legacy_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "stable" / "package_profile_cleanup.json"
    legacy_record_path = tmp_path / "legacy" / "package_profile_cleanup.json"
    staged_dir = tmp_path / "profiles" / (".demo.deleting-" + "a" * 32)
    staged_dir.mkdir(parents=True)
    legacy_record_path.parent.mkdir(parents=True)
    uninstall_module._save_deferred_profile_cleanup_paths_sync(
        legacy_record_path,
        [str(staged_dir)],
    )
    monkeypatch.setattr(
        uninstall_module,
        "_deferred_profile_cleanup_record_path_sync",
        lambda: record_path,
    )
    monkeypatch.setattr(
        uninstall_module,
        "_legacy_deferred_profile_cleanup_record_path_sync",
        lambda: legacy_record_path,
    )

    assert uninstall_module.retry_deferred_profile_cleanup_sync() == 1
    assert staged_dir.exists() is False
    assert legacy_record_path.exists() is False
    assert record_path.exists() is False


@pytest.mark.plugin_unit
def test_unreadable_deferred_cleanup_record_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A record we could not parse still names staging dirs nobody else tracks."""
    record_path = tmp_path / "package_profile_cleanup.json"
    record_path.write_text("{ not json", encoding="utf-8")
    staged_profile = uninstall_module._StagedPackageProfile(
        original_dir=tmp_path / "profiles" / "demo_package",
        staged_dir=tmp_path / "profiles" / (".demo_package.deleting-" + "a" * 32),
    )
    monkeypatch.setattr(
        uninstall_module,
        "_deferred_profile_cleanup_record_path_sync",
        lambda: record_path,
    )

    assert uninstall_module._load_deferred_profile_cleanup_paths_sync(record_path) is None
    assert uninstall_module._record_deferred_profile_cleanup_sync(staged_profile) is False
    assert uninstall_module.retry_deferred_profile_cleanup_sync() == 0
    assert record_path.read_text(encoding="utf-8") == "{ not json"


@pytest.mark.plugin_unit
def test_deferred_cleanup_recording_swallows_non_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Best-effort recording must not abort the deletion flow that calls it."""

    def _raise_runtime_error() -> Path:
        raise RuntimeError("plugin config root unavailable")

    monkeypatch.setattr(
        uninstall_module,
        "_deferred_profile_cleanup_record_path_sync",
        _raise_runtime_error,
    )
    staged_profile = uninstall_module._StagedPackageProfile(
        original_dir=tmp_path / "profiles" / "demo_package",
        staged_dir=tmp_path / "profiles" / (".demo_package.deleting-" + "a" * 32),
    )

    assert uninstall_module._record_deferred_profile_cleanup_sync(staged_profile) is False


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_plugin_stops_running_host_before_removing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_dir = tmp_path / "running_plugin"
    config_path = plugin_dir / "plugin.toml"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='running_plugin'\nentry='tests.fake:Plugin'\n", encoding="utf-8")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    stop_calls: list[str] = []

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["running_plugin"] = {
                "id": "running_plugin",
                "name": "Running Plugin",
                "type": "plugin",
                "config_path": str(config_path),
                "entry_point": "tests.fake:Plugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts["running_plugin"] = _FakeProcessHost(
                plugin_id="running_plugin",
                entry_point="tests.fake:Plugin",
                config_path=config_path,
            )
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        async def _refresh_registry() -> dict[str, object]:
            return {"success": True}

        original_stop_plugin = module.PluginLifecycleService.stop_plugin

        async def _tracked_stop(self, plugin_id: str) -> dict[str, object]:
            stop_calls.append(plugin_id)
            return await original_stop_plugin(self, plugin_id)

        monkeypatch.setattr(uninstall_module, "PLUGIN_CONFIG_ROOTS", (tmp_path,))
        monkeypatch.setattr(module.PluginLifecycleService, "stop_plugin", _tracked_stop)
        monkeypatch.setattr(
            uninstall_module.plugin_registry_service,
            "refresh_registry",
            _refresh_registry,
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)
        install_source_manager = _FakeInstallSourceManager(package_id="")
        monkeypatch.setattr(
            uninstall_module, "get_install_source_manager", lambda: install_source_manager
        )

        service = module.PluginLifecycleService()
        response = await service.delete_plugin("running_plugin")

        assert response["success"] is True
        assert stop_calls == ["running_plugin"]
        assert plugin_dir.exists() is False
        with module.state.acquire_plugin_hosts_read_lock():
            assert "running_plugin" not in module.state.plugin_hosts
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_plugin_clears_runtime_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    plugin_dir = tmp_path / "demo_plugin"
    config_path = plugin_dir / "plugin.toml"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[plugin]\nid='demo_plugin'\nentry='tests.fake:Plugin'\n",
        encoding="utf-8",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["demo_plugin"] = {
                "id": "demo_plugin",
                "name": "Demo",
                "type": "plugin",
                "config_path": str(config_path),
                "entry_point": "tests.fake:Plugin",
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        runtime_overrides_module.set_runtime_override("demo_plugin", False)
        assert _isolate_runtime_overrides == {"demo_plugin": False}

        async def _refresh_registry() -> dict[str, object]:
            return {"success": True}

        monkeypatch.setattr(uninstall_module, "PLUGIN_CONFIG_ROOTS", (tmp_path,))
        monkeypatch.setattr(
            uninstall_module.plugin_registry_service,
            "refresh_registry",
            _refresh_registry,
        )
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)
        install_source_manager = _FakeInstallSourceManager(package_id="")
        monkeypatch.setattr(
            uninstall_module, "get_install_source_manager", lambda: install_source_manager
        )

        service = module.PluginLifecycleService()
        await service.delete_plugin("demo_plugin")

        assert _isolate_runtime_overrides == {}
        assert runtime_overrides_module.get_runtime_override("demo_plugin") is None
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


def _seed_running_plugin(plugin_id: str, config_path: Path) -> None:
    with module.state.acquire_plugins_write_lock():
        module.state.plugins.clear()
        module.state.plugins[plugin_id] = {
            "id": plugin_id,
            "name": plugin_id,
            "type": "plugin",
            "config_path": str(config_path),
            "entry_point": "tests.fake:Plugin",
        }
    with module.state.acquire_plugin_hosts_write_lock():
        module.state.plugin_hosts.clear()
        module.state.plugin_hosts[plugin_id] = _FakeProcessHost(
            plugin_id=plugin_id,
            entry_point="tests.fake:Plugin",
            config_path=config_path,
        )
    with module.state.acquire_event_handlers_write_lock():
        module.state.event_handlers.clear()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_concurrent_stops_do_not_teardown_the_same_host_twice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    shutdown_calls = 0

    class _WaitingHost(_FakeProcessHost):
        async def shutdown(
            self, timeout: float = module.PLUGIN_SHUTDOWN_TIMEOUT
        ) -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1
            entered.set()
            await release.wait()

    # stop_plugin() pops the host and therefore calls
    # state.invalidate_snapshot_cache("hosts"), so the snapshot cache is
    # mutated global state this test has to hand back. pytest-randomly means
    # the victim of a leak is whatever happens to run next, so restore the
    # same four things every other test in this file restores.
    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts["demo_plugin"] = _WaitingHost(
                plugin_id="demo_plugin",
                entry_point="tests.fake:Plugin",
                config_path=tmp_path / "demo_plugin" / "plugin.toml",
            )
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()

        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        service = module.PluginLifecycleService()
        first = asyncio.create_task(service.stop_plugin("demo_plugin"))
        await entered.wait()
        second = asyncio.create_task(service.stop_plugin("demo_plugin"))
        await asyncio.sleep(0.05)

        assert shutdown_calls == 1

        release.set()
        assert (await first)["success"] is True
        with pytest.raises(ServerDomainError) as exc_info:
            await second
        assert exc_info.value.code == "PLUGIN_NOT_RUNNING"
    finally:
        release.set()
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_stop_plugin_persist_user_intent_writes_runtime_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    config_path = tmp_path / "demo_plugin" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo_plugin'\n", encoding="utf-8")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        _seed_running_plugin("demo_plugin", config_path)
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        service = module.PluginLifecycleService()
        await service.stop_plugin("demo_plugin", persist_user_intent=True)
        assert _isolate_runtime_overrides == {
            "demo_plugin": {"enabled": False, "auto_start": False},
        }
        assert runtime_overrides_module.get_runtime_override("demo_plugin") is False
        assert runtime_overrides_module.get_runtime_auto_start_override("demo_plugin") is False
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_stop_plugin_internal_call_does_not_touch_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    config_path = tmp_path / "demo_plugin" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo_plugin'\n", encoding="utf-8")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        _seed_running_plugin("demo_plugin", config_path)
        runtime_overrides_module.set_runtime_override("demo_plugin", True)
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        service = module.PluginLifecycleService()
        await service.stop_plugin("demo_plugin")
        assert _isolate_runtime_overrides == {"demo_plugin": True}
        assert runtime_overrides_module.get_runtime_override("demo_plugin") is True
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_stop_plugin_can_leave_auto_start_unchanged_when_sync_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    config_path = tmp_path / "demo_plugin" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo_plugin'\n", encoding="utf-8")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        _seed_running_plugin("demo_plugin", config_path)
        runtime_overrides_module.set_runtime_override(
            "demo_plugin",
            True,
            auto_start=True,
        )
        monkeypatch.setattr(module, "PLUGIN_SYNC_AUTO_START_ON_TOGGLE", False)
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

        await module.PluginLifecycleService().stop_plugin(
            "demo_plugin",
            persist_user_intent=True,
        )

        assert _isolate_runtime_overrides == {
            "demo_plugin": {"enabled": False, "auto_start": True},
        }
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_stop_plugin_returns_partial_success_on_preference_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "demo_plugin" / "plugin.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[plugin]\nid='demo_plugin'\n", encoding="utf-8")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    handlers_backup = dict(module.state.event_handlers)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        _seed_running_plugin("demo_plugin", config_path)
        monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)
        monkeypatch.setattr(
            module,
            "set_runtime_override",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                runtime_overrides_module.RuntimeOverrideWriteError("disk full")
            ),
        )

        response = await module.PluginLifecycleService().stop_plugin(
            "demo_plugin",
            persist_user_intent=True,
        )

        assert response["success"] is True
        assert response["partial_success"] is True
        assert response["preference_persisted"] is False
        assert response["runtime_state_changed"] is True
        assert response["preference_error"] == {
            "code": "PLUGIN_RUNTIME_PREFERENCE_PERSIST_FAILED",
            "error_type": "RuntimeOverrideWriteError",
        }
        with module.state.acquire_plugin_hosts_read_lock():
            assert "demo_plugin" not in module.state.plugin_hosts
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state.acquire_event_handlers_write_lock():
            module.state.event_handlers.clear()
            module.state.event_handlers.update(handlers_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_start_plugin_checks_python_requirements_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """依赖闸门是真磁盘 I/O，不能占着事件循环跑。

    列 vendor/ 下每个 dist-info 再逐个读 METADATA，在 Windows 上冷读实测 0.31s
    （200 个分发包）到 0.97s（600 个）。跑在循环线程上就是整台服务器这段时间不
    响应任何请求。这条守卫盯的是"在哪个线程跑"，不是"跑没跑"。
    """
    config_path = tmp_path / "vendor_adapter" / "plugin.toml"
    vendor_dir = config_path.parent / "vendor"
    vendor_dir.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'vendor_adapter'",
                "name = 'Vendor Adapter'",
                "type = 'adapter'",
                "entry = 'tests.fake_mcp:FakeAdapterPlugin'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
            ]
        ),
        encoding="utf-8",
    )
    (config_path.parent / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo-lib>=2"]\n',
        encoding="utf-8",
    )

    loop_thread = threading.current_thread()
    seen: dict[str, object] = {}

    def _fake_find_missing(requirements, *, search_paths=None):
        seen["thread"] = threading.current_thread()
        seen["requirements"] = list(requirements)
        seen["search_paths"] = list(search_paths or [])
        # 返回的名字**故意跟声明的不一样**：闸门读的必须是检查器的判定，
        # 不是插件自己声明的那张清单。两者都非空，所以只断言"抛了错"分不出
        # 解包顺序错位——错位之后错误信息会把"缺什么"报成声明清单，用户照着
        # 去装的就是本来就装好的那个包（变异存活验出来的）。
        return ["missing-pkg>=9"]

    monkeypatch.setattr(module.plugin_registry_service, "refresh_plugin", lambda _plugin_id: {"success": True})
    monkeypatch.setattr(module, "_get_plugin_config_path", lambda _plugin_id: config_path)
    monkeypatch.setattr(
        module,
        "resolve_plugin_config_from_path",
        lambda *args, **kwargs: {
            "effective_config": kwargs["base_config"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(module, "_resolve_plugin_id_conflict", lambda *args, **kwargs: args[0])
    monkeypatch.setattr(module, "_find_missing_python_requirements", _fake_find_missing)
    monkeypatch.setattr(module, "PluginProcessHost", _FakeProcessHost)
    monkeypatch.setattr(module, "scan_plugin_metadata_isolated", _metadata_scan_for(_FakeAdapterPlugin))
    monkeypatch.setattr(module, "emit_lifecycle_event", lambda event: None)

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()

        with pytest.raises(ServerDomainError) as exc_info:
            await module.PluginLifecycleService().start_plugin(
                "vendor_adapter", refresh_registry=False
            )

        assert exc_info.value.code == "PLUGIN_PYTHON_DEPENDENCIES_MISSING"
        assert "missing-pkg>=9" in str(exc_info.value)
        assert seen["requirements"] == ["demo-lib>=2"]
        assert seen["search_paths"] == [vendor_dir]
        assert seen["thread"] is not loop_thread
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
