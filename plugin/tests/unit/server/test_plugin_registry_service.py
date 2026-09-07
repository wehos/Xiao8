from __future__ import annotations

import copy
from pathlib import Path
import shutil

import pytest

from plugin.server.application.plugins import registry_service as module
from plugin.server.infrastructure import runtime_overrides


pytestmark = pytest.mark.plugin_unit


class _AliveHost:
    def is_alive(self) -> bool:
        return True


def _write_packaged_metadata_fixture(plugin_dir: Path, *, entry_ids: list[str]) -> None:
    """Write the ``plugin.meta.json`` a real ``neko-plugin build`` would ship."""
    import json

    from plugin.server.infrastructure import packaged_metadata

    payload = {
        "schema_version": packaged_metadata.PACKAGED_METADATA_SCHEMA_VERSION,
        "sdk_version": packaged_metadata.SDK_VERSION,
        "source_sha256": packaged_metadata.compute_source_sha256(plugin_dir),
        "source_files": packaged_metadata.source_file_names(plugin_dir)[0],
        "source_bytes": packaged_metadata.source_stat_summary(plugin_dir).total_bytes,
        "entries": [
            {
                "id": entry_id,
                "name": entry_id.capitalize(),
                "description": "",
                "input_schema": {"type": "object", "properties": {}},
            }
            for entry_id in entry_ids
        ],
        # v3 一定会写这三张表，缺哪张都算包坏了。
        "handlers": {},
        "entry_methods": {},
        "entries_config_sha256": packaged_metadata.entries_config_digest({}, {}),
    }
    (plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_plugin_fixture(tmp_path: Path, plugin_id: str) -> Path:
    root = tmp_path / "plugins"
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from plugin.sdk.plugin.decorators import plugin_entry",
                "",
                "class DemoPlugin:",
                "    @plugin_entry(id='ping', name='Ping', description='Ping tool')",
                "    async def ping(self):",
                "        return {'ok': True}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f"id = '{plugin_id}'",
                f"name = '{plugin_id}'",
                "type = 'plugin'",
                f"entry = 'plugins.{plugin_id}:DemoPlugin'",
                "version = '0.1.0'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    # 打包过的插件带着自己的元数据，宿主刷新时读的就是它。夹具不写这份文件的话，
    # 注册表里本来就看不到任何入口——刷新不再 import 插件去问。
    _write_packaged_metadata_fixture(plugin_dir, entry_ids=["ping"])
    return root


def _write_ordered_plugin_fixture(
    root: Path,
    plugin_id: str,
    *,
    dependencies_block: list[str] | None = None,
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f"id = '{plugin_id}'",
                f"name = '{plugin_id}'",
                "type = 'plugin'",
                f"entry = '{plugin_id}.module:Plugin'",
                "version = '0.1.0'",
                *(dependencies_block or []),
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return plugin_dir / "plugin.toml"


def _write_package_plugin_fixture(
    root: Path,
    directory_name: str,
    *,
    plugin_id: str | None = None,
    entry_package: str | None = None,
    source: str | None = None,
) -> Path:
    resolved_plugin_id = plugin_id or directory_name
    resolved_entry_package = entry_package or directory_name
    plugin_dir = root / directory_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(
        source
        or "\n".join(
            [
                "class DemoPlugin:",
                "    pass",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f"id = '{resolved_plugin_id}'",
                f"name = '{resolved_plugin_id}'",
                "type = 'plugin'",
                f"entry = 'plugins.{resolved_entry_package}:DemoPlugin'",
                "version = '0.1.0'",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return plugin_dir / "plugin.toml"


@pytest.mark.asyncio
async def test_refresh_registry_syncs_metadata_and_marks_missing_running_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "demo_plugin")

    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["stale_plugin"] = {
                "id": "stale_plugin",
                "name": "stale_plugin",
                "config_path": str((tmp_path / "plugins" / "stale_plugin" / "plugin.toml").resolve()),
            }
            module.state.plugins["running_removed"] = {
                "id": "running_removed",
                "name": "running_removed",
                "config_path": str((tmp_path / "plugins" / "running_removed" / "plugin.toml").resolve()),
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts["running_removed"] = _AliveHost()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        result = await service.refresh_registry()

        assert result["success"] is True
        assert result["added"] == ["demo_plugin"]
        assert result["removed"] == ["stale_plugin"]
        assert result["removed_running"] == ["running_removed"]

        with module.state.acquire_plugins_read_lock():
            demo_meta = dict(module.state.plugins["demo_plugin"])
            running_removed = dict(module.state.plugins["running_removed"])
            assert "demo_plugin_1" not in module.state.plugins

        assert demo_meta["runtime_enabled"] is True
        assert demo_meta["runtime_auto_start"] is False
        assert [entry["id"] for entry in demo_meta["entries_preview"]] == ["ping"]
        assert running_removed["runtime_source_missing"] is True
        assert "stale_plugin" not in module.state.plugins
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_applies_user_auto_start_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "remembered_plugin")
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        runtime_overrides.set_runtime_override(
            "remembered_plugin",
            True,
            auto_start=True,
        )
        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        await module.PluginRegistryService().refresh_registry()

        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["remembered_plugin"])
        assert plugin_meta["runtime_enabled"] is True
        assert plugin_meta["runtime_auto_start"] is True
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "load_overrides",
    [
        lambda: (_ for _ in ()).throw(
            runtime_overrides.RuntimeOverrideReadError("invalid json")
        ),
        lambda: runtime_overrides._coerce_overrides(
            {
                "manifest_plugin": {
                    "enabled": False,
                    "auto_start": "yes",
                }
            }
        ),
    ],
    ids=("unreadable-file", "invalid-plugin-entry"),
)
async def test_refresh_registry_uses_manifest_defaults_when_overrides_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    load_overrides,
) -> None:
    root = _write_plugin_fixture(tmp_path, "manifest_plugin")
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    monkeypatch.setattr(
        runtime_overrides,
        "_load_from_disk",
        load_overrides,
    )
    runtime_overrides.reset_cache_for_testing()
    monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

    try:
        await module.PluginRegistryService().refresh_registry()

        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["manifest_plugin"])
        assert plugin_meta["runtime_enabled"] is True
        assert plugin_meta["runtime_auto_start"] is False
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_plugin_returns_updated_status_for_existing_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "refresh_me")

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["refresh_me"] = {
                "id": "refresh_me",
                "name": "Old Name",
                "config_path": str((root / "refresh_me" / "plugin.toml").resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": True,
                "entries_preview": [],
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        payload = await service.refresh_plugin("refresh_me")

        assert payload["success"] is True
        assert payload["plugin_id"] == "refresh_me"
        assert payload["status"] == "updated"

        with module.state.acquire_plugins_read_lock():
            refreshed = dict(module.state.plugins["refresh_me"])
        assert refreshed["name"] == "refresh_me"
        assert [entry["id"] for entry in refreshed["entries_preview"]] == ["ping"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_plugin_checks_python_requirements_against_vendor_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "vendor_refresh")
    plugin_dir = root / "vendor_refresh"
    vendor_dir = plugin_dir / "vendor"
    vendor_dir.mkdir()
    (plugin_dir / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo-lib>=2"]\n',
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def _fake_find_missing(requirements, *, search_paths=None):
        seen["requirements"] = list(requirements)
        seen["search_paths"] = list(search_paths or [])
        return []

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["vendor_refresh"] = {
                "id": "vendor_refresh",
                "name": "Vendor Refresh",
                "config_path": str((plugin_dir / "plugin.toml").resolve()),
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))
        monkeypatch.setattr(module, "_find_missing_python_requirements", _fake_find_missing)

        payload = await module.PluginRegistryService().refresh_plugin("vendor_refresh")

        assert payload["success"] is True
        assert seen["requirements"] == ["demo-lib>=2"]
        assert seen["search_paths"] == [vendor_dir]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_keeps_existing_metadata_when_config_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    plugin_dir = root / "broken_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    config_path = plugin_dir / "plugin.toml"
    config_path.write_text("[plugin\nid='broken_plugin'\n", encoding="utf-8")

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["broken_plugin"] = {
                "id": "broken_plugin",
                "name": "Broken Plugin",
                "config_path": str(config_path.resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": False,
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        result = await service.refresh_registry()

        assert result["success"] is False
        assert result["removed"] == []
        assert result["removed_running"] == []
        assert len(result["failed"]) == 1
        assert result["failed"][0]["config_path"] == str(config_path.resolve())

        with module.state.acquire_plugins_read_lock():
            preserved = dict(module.state.plugins["broken_plugin"])
        assert preserved["name"] == "Broken Plugin"
        assert "runtime_source_missing" not in preserved
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_marks_syntax_error_plugin_failed_without_aborting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    _write_package_plugin_fixture(root, "healthy_plugin")
    _write_package_plugin_fixture(
        root,
        "broken_plugin",
        source="def broken(:\n    pass\n",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True
        with module.state.acquire_plugins_read_lock():
            healthy = dict(module.state.plugins["healthy_plugin"])
            broken = dict(module.state.plugins["broken_plugin"])

        # 刷新不再 import 插件，所以语法错误在刷新阶段是看不出来的：坏插件照常
        # 出现在注册表里，直到有人真的启动它才会失败。这是不 import 的直接代价。
        # 这条测试现在守的是它**仍然在列表里**——"发现不了坏插件"和"把坏插件从
        # 列表里漏掉"是两回事，后者用户会以为插件凭空消失了。
        assert healthy.get("runtime_load_state") != "failed"
        assert broken.get("runtime_load_state") != "failed", (
            "刷新阶段判定了插件代码坏没坏，说明它又去 import 插件了"
        )
        assert broken.get("id") == "broken_plugin"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_handles_import_stdout_without_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    _write_package_plugin_fixture(
        root,
        "noisy_plugin",
        source="\n".join(
            [
                "print('import noise', end='')",
                "",
                "class DemoPlugin:",
                "    pass",
                "",
            ]
        ),
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True
        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["noisy_plugin"])
        assert plugin_meta.get("runtime_load_state") != "failed"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_marks_entry_directory_mismatch_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    _write_package_plugin_fixture(
        root,
        "repo_file_manager",
        plugin_id="file_manager",
        entry_package="file_manager",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True
        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["file_manager"])

        assert plugin_meta["runtime_load_state"] == "failed"
        assert plugin_meta["runtime_load_error_type"] == "PluginEntryDirectoryMismatch"
        assert "repo_file_manager" in plugin_meta["runtime_load_error_message"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_prioritizes_entry_directory_mismatch_before_requirements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    config_path = _write_package_plugin_fixture(
        root,
        "repo_file_manager",
        plugin_id="file_manager",
        entry_package="file_manager",
    )
    (config_path.parent / "pyproject.toml").write_text(
        '[project]\ndependencies = ["definitely-missing-lib>=1"]\n',
        encoding="utf-8",
    )
    requirements_checked = False

    def _fake_find_missing(requirements, *, search_paths=None):
        nonlocal requirements_checked
        requirements_checked = True
        return ["definitely-missing-lib>=1"]

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))
        monkeypatch.setattr(module, "_find_missing_python_requirements", _fake_find_missing)

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True
        assert requirements_checked is False
        with module.state.acquire_plugins_read_lock():
            plugin_meta = dict(module.state.plugins["file_manager"])

        assert plugin_meta["runtime_load_state"] == "failed"
        assert plugin_meta["runtime_load_error_type"] == "PluginEntryDirectoryMismatch"
        assert plugin_meta["runtime_load_error_phase"] == "entry_validation"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_list_autostart_plugin_ids_uses_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    provider_config = _write_ordered_plugin_fixture(root, "provider")
    consumer_config = _write_ordered_plugin_fixture(
        root,
        "consumer",
        dependencies_block=[
            "",
            "dependencies = ['provider']",
        ],
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["consumer"] = {
                "id": "consumer",
                "type": "plugin",
                "config_path": str(consumer_config.resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": True,
            }
            module.state.plugins["provider"] = {
                "id": "provider",
                "type": "plugin",
                "config_path": str(provider_config.resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": True,
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        ordered = await service.list_autostart_plugin_ids()

        assert ordered == ["provider", "consumer"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_plugin_marks_missing_simple_plugin_dependency_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_plugin_fixture(tmp_path, "consumer")
    config_path = root / "consumer" / "plugin.toml"
    config_path.write_text(
        "\n".join(
            [
                "[plugin]",
                "id = 'consumer'",
                "name = 'consumer'",
                "type = 'plugin'",
                "entry = 'consumer_entry:DemoPlugin'",
                "version = '0.1.0'",
                "dependencies = ['missing_provider']",
                "",
                "[plugin_runtime]",
                "enabled = true",
                "auto_start = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins["consumer"] = {
                "id": "consumer",
                "name": "consumer",
                "config_path": str(config_path.resolve()),
                "runtime_enabled": True,
                "runtime_auto_start": False,
            }

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        payload = await module.PluginRegistryService().refresh_plugin("consumer")

        assert payload["success"] is True
        with module.state.acquire_plugins_read_lock():
            refreshed = dict(module.state.plugins["consumer"])
        assert refreshed["runtime_load_state"] == "failed"
        assert refreshed["runtime_load_error_type"] == "DependencyCheckFailed"
        assert refreshed["runtime_load_error_phase"] == "dependency_check"
        assert "missing_provider" in refreshed["runtime_load_error_message"]
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_refresh_registry_registers_duplicate_declared_plugin_ids_with_runtime_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    first_dir = root / "demo"
    second_dir = root / "demo_1"
    first_dir.mkdir(parents=True, exist_ok=True)
    second_dir.mkdir(parents=True, exist_ok=True)

    (tmp_path / "demo_entry.py").write_text(
        "\n".join(
            [
                "class DemoPlugin:",
                "    pass",
                "",
            ]
        ),
        encoding="utf-8",
    )
    for plugin_dir in (first_dir, second_dir):
        (plugin_dir / "plugin.toml").write_text(
            "\n".join(
                [
                    "[plugin]",
                    "id = 'demo'",
                    "name = 'demo'",
                    "type = 'plugin'",
                    "entry = 'demo_entry:DemoPlugin'",
                    "version = '0.1.0'",
                    "",
                    "[plugin_runtime]",
                    "enabled = true",
                    "auto_start = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)

    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()

        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (root,))

        service = module.PluginRegistryService()
        result = await service.refresh_registry()
        second_result = await service.refresh_registry()
        refreshed_duplicate = await service.refresh_plugin("demo_1")

        assert result["success"] is True
        assert result["failed"] == []
        assert result["added"] == ["demo", "demo_1"]
        assert second_result["success"] is True
        assert second_result["failed"] == []
        assert second_result["added"] == []
        assert second_result["unchanged"] == ["demo", "demo_1"]
        assert refreshed_duplicate["success"] is True
        assert refreshed_duplicate["plugin_id"] == "demo_1"
        assert refreshed_duplicate["status"] == "unchanged"

        with module.state.acquire_plugins_read_lock():
            first_meta = dict(module.state.plugins["demo"])
            second_meta = dict(module.state.plugins["demo_1"])

        assert Path(first_meta["config_path"]).parent.name == "demo"
        assert Path(second_meta["config_path"]).parent.name == "demo_1"
        assert first_meta["id"] == "demo"
        assert second_meta["id"] == "demo_1"
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_user_source_shadows_builtin_without_suffix_and_builtin_recovers_after_uninstall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builtin_root = tmp_path / "builtin" / "plugins"
    user_root = tmp_path / "user" / "plugins"
    (tmp_path / "shadow_entry.py").write_text(
        "class Plugin:\n    pass\n",
        encoding="utf-8",
    )

    def write_source(root: Path, version: str) -> Path:
        plugin_dir = root / "study_companion"
        plugin_dir.mkdir(parents=True)
        config = plugin_dir / "plugin.toml"
        config.write_text(
            "\n".join(
                [
                    "[plugin]",
                    "id = 'study_companion'",
                    "name = 'Study Companion'",
                    "type = 'plugin'",
                    "entry = 'shadow_entry:Plugin'",
                    f"version = '{version}'",
                    "",
                    "[plugin_runtime]",
                    "enabled = true",
                    "auto_start = false",
                ]
            ),
            encoding="utf-8",
        )
        return config

    builtin_config = write_source(builtin_root, "0.1.5")
    user_config = write_source(user_root, "0.1.6")
    hidden_staging = user_root / ".neko_override_staging_test"
    shutil.copytree(user_config.parent, hidden_staging)
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (user_root, builtin_root))

        service = module.PluginRegistryService()
        installed = await service.refresh_registry()

        assert installed["success"] is True
        assert installed["shadowed"] == [
            {
                "plugin_id": "study_companion",
                "config_path": str(builtin_config),
                "source": "builtin",
            }
        ]
        with module.state.acquire_plugins_read_lock():
            market_meta = dict(module.state.plugins["study_companion"])
            assert "study_companion_1" not in module.state.plugins
        assert market_meta["version"] == "0.1.6"
        assert market_meta["effective_source"] == "user"
        assert market_meta["builtin_version"] == "0.1.5"
        assert Path(market_meta["config_path"]) == user_config

        shutil.rmtree(user_config.parent)
        restored = await service.refresh_registry()

        assert restored["success"] is True
        with module.state.acquire_plugins_read_lock():
            builtin_meta = dict(module.state.plugins["study_companion"])
            assert "study_companion_1" not in module.state.plugins
        assert builtin_meta["version"] == "0.1.5"
        assert builtin_meta["effective_source"] == "builtin"
        assert Path(builtin_meta["config_path"]) == builtin_config
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_noncanonical_user_conflict_keeps_builtin_declared_id_and_runtime_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "demo"
    builtin_root = tmp_path / "builtin" / "plugins"
    user_root = tmp_path / "user" / "plugins"
    builtin_config = _write_package_plugin_fixture(builtin_root, plugin_id)
    user_config = _write_package_plugin_fixture(
        user_root,
        "old_name",
        plugin_id=plugin_id,
    )
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (user_root, builtin_root))

        result = await module.PluginRegistryService().refresh_registry()
        _contexts, contexts_by_id = module._collect_plugin_contexts_from_roots_sync(
            (user_root, builtin_root),
        )

        assert result["success"] is True, result
        assert result["shadowed"] == []
        with module.state.acquire_plugins_read_lock():
            builtin_meta = dict(module.state.plugins[plugin_id])
            legacy_meta = dict(module.state.plugins[f"{plugin_id}_1"])
        assert Path(builtin_meta["config_path"]) == builtin_config
        assert builtin_meta["effective_source"] == "builtin"
        assert Path(legacy_meta["config_path"]) == user_config
        assert legacy_meta["effective_source"] == "user"
        assert contexts_by_id[plugin_id].toml_path == builtin_config
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_noncanonical_user_conflict_cannot_replace_canonical_user_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "demo"
    builtin_root = tmp_path / "builtin" / "plugins"
    user_root = tmp_path / "user" / "plugins"
    builtin_config = _write_package_plugin_fixture(builtin_root, plugin_id)
    user_config = _write_package_plugin_fixture(user_root, plugin_id)
    legacy_config = _write_package_plugin_fixture(
        user_root,
        "old_name",
        plugin_id=plugin_id,
    )
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (user_root, builtin_root))

        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True, result
        assert result["shadowed"] == [
            {
                "plugin_id": plugin_id,
                "config_path": str(builtin_config),
                "source": "builtin",
            }
        ]
        with module.state.acquire_plugins_read_lock():
            effective = dict(module.state.plugins[plugin_id])
            legacy = dict(module.state.plugins[f"{plugin_id}_1"])
        assert Path(effective["config_path"]) == user_config
        assert effective["effective_source"] == "user"
        assert Path(legacy["config_path"]) == legacy_config
        assert legacy["effective_source"] == "user"
        assert "shadowed_builtin_path" not in legacy
        assert "builtin_version" not in legacy
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
async def test_canonical_user_override_precedes_earlier_legacy_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_id = "demo"
    builtin_root = tmp_path / "builtin" / "plugins"
    user_root = tmp_path / "user" / "plugins"
    builtin_config = _write_package_plugin_fixture(builtin_root, plugin_id)
    legacy_config = _write_package_plugin_fixture(
        user_root,
        "aaa_legacy",
        plugin_id=plugin_id,
    )
    user_config = _write_package_plugin_fixture(user_root, plugin_id)
    plugins_backup = copy.deepcopy(module.state.plugins)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
        monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (user_root, builtin_root))

        result = await module.PluginRegistryService().refresh_registry()
        _contexts, contexts_by_id = module._collect_plugin_contexts_from_roots_sync(
            (user_root, builtin_root),
        )

        assert result["success"] is True, result
        assert result["shadowed"] == [
            {
                "plugin_id": plugin_id,
                "config_path": str(builtin_config),
                "source": "builtin",
            }
        ]
        with module.state.acquire_plugins_read_lock():
            effective = dict(module.state.plugins[plugin_id])
            legacy = dict(module.state.plugins[f"{plugin_id}_1"])
        assert Path(effective["config_path"]) == user_config
        assert effective["effective_source"] == "user"
        assert Path(legacy["config_path"]) == legacy_config
        assert legacy["effective_source"] == "user"
        assert contexts_by_id[plugin_id].toml_path == user_config
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup


@pytest.mark.asyncio
@pytest.mark.parametrize("alias_running", [True, False])
async def test_override_refresh_preserves_only_running_config_path_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    alias_running: bool,
) -> None:
    plugin_id = "study_companion"
    alias_id = f"{plugin_id}_1"
    builtin_root = tmp_path / "builtin" / "plugins"
    user_root = tmp_path / "user" / "plugins"
    (tmp_path / "alias_entry.py").write_text(
        "class Plugin:\n    pass\n",
        encoding="utf-8",
    )

    def write_source(root: Path, version: str) -> Path:
        plugin_dir = root / plugin_id
        plugin_dir.mkdir(parents=True)
        config_path = plugin_dir / "plugin.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[plugin]",
                    f"id = '{plugin_id}'",
                    "name = 'Study Companion'",
                    "type = 'plugin'",
                    "entry = 'alias_entry:Plugin'",
                    f"version = '{version}'",
                    "",
                    "[plugin_runtime]",
                    "enabled = true",
                    "auto_start = false",
                ]
            ),
            encoding="utf-8",
        )
        return config_path

    builtin_config = write_source(builtin_root, "0.1.5")
    user_config = write_source(user_root, "0.1.6")
    plugins_backup = copy.deepcopy(module.state.plugins)
    hosts_backup = dict(module.state.plugin_hosts)
    cache_backup = copy.deepcopy(module.state._snapshot_cache)
    try:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins[plugin_id] = {
                "id": plugin_id,
                "name": "Builtin Plugin",
                "config_path": str(builtin_config.resolve()),
                "runtime_enabled": True,
            }
            module.state.plugins[alias_id] = {
                "id": alias_id,
                "name": "Legacy Alias",
                "config_path": str(user_config.resolve()),
                "runtime_enabled": True,
            }
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            if alias_running:
                module.state.plugin_hosts[alias_id] = _AliveHost()
        stale_snapshot = module.state.get_plugins_snapshot_cached(force=True)
        assert alias_id in stale_snapshot

        monkeypatch.setattr(module, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
        monkeypatch.setattr(module, "PLUGIN_CONFIG_ROOTS", (user_root, builtin_root))
        result = await module.PluginRegistryService().refresh_registry()

        assert result["success"] is True, result
        assert result["shadowed"] == [
            {
                "plugin_id": plugin_id,
                "config_path": str(builtin_config),
                "source": "builtin",
            }
        ]
        fresh_snapshot = module.state.get_plugins_snapshot_cached()
        canonical = fresh_snapshot[plugin_id]
        assert canonical["version"] == "0.1.6"
        assert canonical["effective_source"] == "user"
        assert Path(canonical["config_path"]) == user_config
        if alias_running:
            alias_meta = fresh_snapshot[alias_id]
            assert alias_meta["name"] == "Legacy Alias"
            assert alias_meta["runtime_source_missing"] is True
        else:
            assert alias_id not in fresh_snapshot
    finally:
        with module.state.acquire_plugins_write_lock():
            module.state.plugins.clear()
            module.state.plugins.update(plugins_backup)
        with module.state.acquire_plugin_hosts_write_lock():
            module.state.plugin_hosts.clear()
            module.state.plugin_hosts.update(hosts_backup)
        with module.state._snapshot_cache_lock:
            module.state._snapshot_cache = cache_backup
