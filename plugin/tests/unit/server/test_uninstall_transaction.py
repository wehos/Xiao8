"""Deterministic fault-injection tests for the uninstall transaction."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from plugin.server.application.install_source.manager import InstallSourceError
from plugin.server.application.install_source.models import LockEntry
from plugin.server.application.plugins import lifecycle_service as lifecycle_module
from plugin.server.application.plugins import operation_lock as operation_lock_module
import plugin.server.application.plugins.installation_transactions.uninstall as uninstall_module
from plugin.server.application.plugins.installation_transactions.uninstall import (
    UninstallPluginError,
    uninstall_plugin,
)
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure import runtime_overrides as runtime_overrides_module


@pytest.fixture(autouse=True)
def _isolate_operation_file_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Never let unit tests contend with a running user's Core process."""

    monkeypatch.setenv(
        "NEKO_PLUGIN_OPERATION_LOCK_PATH",
        str(tmp_path / "operation.lock"),
    )


def _entry(
    *,
    plugin_id: str = "demo",
    directory_name: str = "demo",
    channel: str = "imported",
) -> LockEntry:
    return LockEntry(
        root_id="user",  # type: ignore[arg-type]
        directory_name=directory_name,
        plugin_id=plugin_id,
        channel=channel,  # type: ignore[arg-type]
        reason="user_requested",
        installed_at="2026-08-29T00:00:00.000000Z",
        updated_at="2026-08-29T00:00:00.000000Z",
        last_seen_at="2026-08-29T00:00:00.000000Z",
    )


class _FakeManager:
    """Minimal install-source manager double keyed by directory name."""

    def __init__(
        self,
        plugin_names: tuple[str, ...] = ("demo",),
        *,
        mark_removed_error: Exception | None = None,
    ) -> None:
        self.plugin_names = tuple(plugin_names)
        self.is_degraded = False
        self.mark_removed_error = mark_removed_error
        self.marked_removed: list[Path] = []
        self.restored_entries: list[LockEntry] = []
        self.load_calls = 0
        self.entries = {
            name: _entry(plugin_id=name, directory_name=name) for name in plugin_names
        }

    def load(self) -> None:
        self.load_calls += 1

    def entry_for_directory(
        self,
        directory_path: Path,
        *,
        include_removed: bool = False,
    ) -> LockEntry | None:
        entry = self.entries.get(directory_path.name)
        if entry is None or (entry.removed and not include_removed):
            return None
        return entry

    def list_entries(self) -> list[LockEntry]:
        return [entry for entry in self.entries.values() if not entry.removed]

    def mark_removed(self, *, directory_path: Path) -> None:
        if self.mark_removed_error is not None:
            raise self.mark_removed_error
        self.marked_removed.append(directory_path)
        entry = self.entries[directory_path.name]
        self.entries[directory_path.name] = replace(
            entry,
            removed=True,
            removed_at="2026-08-30T00:00:00.000000Z",
        )

    def restore_entry_for_rollback(self, entry: LockEntry) -> None:
        self.restored_entries.append(entry)
        self.entries[entry.directory_name] = entry


class _Harness:
    """Build managed user plugins with isolated roots, fakes and a run log."""

    def __init__(
        self, tmp_path: Path, *, plugin_names: tuple[str, ...] = ("demo",)
    ) -> None:
        self.tmp_path = tmp_path
        self.plugin_names = plugin_names
        self.exec_root = tmp_path / "exec"
        self.builtin_root = tmp_path / "builtin"
        self.state_root = tmp_path / "state" / "plugins"
        self.profiles_root = tmp_path / "profiles"
        self.exec_root.mkdir(parents=True)
        self.builtin_root.mkdir(parents=True)
        for name in plugin_names:
            self.make_plugin(name)
        self.config_dir = tmp_path / "persistent" / "config" / "demo"
        self.config_dir.mkdir(parents=True)
        self.config_file = self.config_dir / "settings.toml"
        self.config_file.write_text("value = 1\n", encoding="utf-8")
        self.data_file = self.state_root / "demo" / "data" / "data.db"
        self.data_file.parent.mkdir(parents=True)
        self.data_file.write_bytes(b"persistent-data")
        self.cache_file = tmp_path / "persistent" / "cache" / "demo" / "blob"
        self.cache_file.parent.mkdir(parents=True)
        self.cache_file.write_bytes(b"cached-blob")
        self.manager = _FakeManager(plugin_names)
        self.running: set[str] = set()
        self.stop_calls: list[str] = []
        self.start_calls: list[str] = []
        self.refresh_calls = 0
        self.refresh_error: Exception | None = None
        self.refresh_result: dict[str, object] = {"success": True}
        self.stop_error: Exception | None = None
        self.builtin_after_refresh = False
        self.log: list[str] = []

    def make_plugin(self, name: str) -> None:
        plugin_dir = self.exec_root / name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "plugin.toml").write_text(
            f"[plugin]\nid='{name}'\n", encoding="utf-8"
        )

    @property
    def plugin_dir(self) -> Path:
        return self.exec_root / "demo"

    @property
    def config_path(self) -> Path:
        return self.plugin_dir / "plugin.toml"

    @property
    def builtin_config(self) -> Path:
        return self.builtin_root / "demo" / "plugin.toml"

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            uninstall_module, "get_user_plugin_exec_root", lambda: self.exec_root
        )
        monkeypatch.setattr(
            uninstall_module, "get_plugin_state_root", lambda: self.state_root
        )
        monkeypatch.setattr(
            uninstall_module,
            "get_user_package_profiles_root",
            lambda: self.profiles_root,
        )
        monkeypatch.setattr(
            uninstall_module, "BUILTIN_PLUGIN_CONFIG_ROOT", self.builtin_root
        )
        monkeypatch.setattr(
            uninstall_module,
            "PLUGIN_CONFIG_ROOTS",
            (self.exec_root, self.builtin_root),
        )
        monkeypatch.setattr(
            uninstall_module,
            "get_install_source_manager",
            lambda: self.manager,
        )
        monkeypatch.setattr(
            lifecycle_module,
            "_get_plugin_meta_sync",
            self._get_plugin_meta_sync,
        )
        monkeypatch.setattr(
            lifecycle_module,
            "_plugin_is_running_sync",
            self._plugin_is_running_sync,
        )
        monkeypatch.setattr(
            uninstall_module.plugin_registry_service,
            "refresh_registry",
            self._refresh_registry,
        )
        monkeypatch.setattr(
            lifecycle_module.PluginLifecycleService,
            "stop_plugin",
            self._stop_plugin,
        )
        monkeypatch.setattr(
            lifecycle_module.PluginLifecycleService,
            "start_plugin",
            self._start_plugin,
        )

    def _get_plugin_meta_sync(self, plugin_id: str) -> dict[str, object]:
        source = (
            "builtin"
            if self.builtin_after_refresh and self.refresh_calls > 0
            else "user"
        )
        return {
            "id": plugin_id,
            "config_path": str(self.exec_root / plugin_id / "plugin.toml"),
            "effective_source": source,
        }

    def _plugin_is_running_sync(self, plugin_id: str) -> bool:
        return plugin_id in self.running

    async def _refresh_registry(self) -> dict[str, object]:
        self.refresh_calls += 1
        self.log.append("refresh")
        if self.refresh_error is not None:
            raise self.refresh_error
        return dict(self.refresh_result)

    async def _stop_plugin(
        self, plugin_id: str, **_kwargs: object
    ) -> dict[str, object]:
        self.log.append(f"stop:{plugin_id}")
        if self.stop_error is not None:
            raise self.stop_error
        self.stop_calls.append(plugin_id)
        self.running.discard(plugin_id)
        return {"success": True}

    async def _start_plugin(
        self, plugin_id: str, **_kwargs: object
    ) -> dict[str, object]:
        self.start_calls.append(plugin_id)
        self.running.add(plugin_id)
        return {"success": True}

    def persistent_hashes(self) -> dict[Path, str]:
        return {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.config_file, self.data_file, self.cache_file)
        }


def _package_entry_fakes(
    monkeypatch: pytest.MonkeyPatch, harness: _Harness, package_id: str = "demo_package"
) -> None:
    """Make the fake manager describe one package-owned profile for demo."""

    def _entry_for_directory(
        self: _FakeManager, directory_path: Path, *, include_removed: bool = False
    ) -> SimpleNamespace:
        del include_removed
        return SimpleNamespace(
            package_id=package_id,
            plugin_id=directory_path.name,
            profile_dir="",
            profile_installed=None,
            channel="imported",
            root_id="user",
            directory_name=directory_path.name,
            removed=False,
        )

    def _list_entries(
        self: _FakeManager, include_removed: bool = False
    ) -> list[SimpleNamespace]:
        del include_removed
        return [_entry_for_directory(self, Path(name)) for name in self.plugin_names]

    monkeypatch.setattr(
        type(harness.manager), "entry_for_directory", _entry_for_directory
    )
    monkeypatch.setattr(type(harness.manager), "list_entries", _list_entries)


def _details_of(exc: BaseException) -> dict[str, object]:
    assert isinstance(exc, UninstallPluginError)
    return exc.details


def _signal_second_operation_lock_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> asyncio.Event:
    """Signal once the second transaction has actually attempted the lock."""
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

    monkeypatch.setattr(
        operation_lock_module._PROCESS_LOCK,
        "acquire",
        _tracked_acquire,
    )
    return second_acquire_attempted


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_success_reports_commit_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.running.add("demo")
    harness.install(monkeypatch)
    runtime_overrides_module.set_runtime_override("demo", False)

    result = await uninstall_plugin("demo")

    assert result.deleted_from_disk is True
    assert result.plugin_dir == harness.plugin_dir
    assert result.deleted_profile_dir is None
    assert result.restored_builtin is False
    assert result.preference_action == "cleared"
    assert result.filesystem_rollback == "not_needed"
    assert result.runtime_restart == "not_needed"
    assert result.cleanup_pending is False
    assert harness.manager.load_calls == 1
    assert harness.stop_calls == ["demo"]
    assert harness.start_calls == []
    assert harness.manager.marked_removed == [harness.plugin_dir]
    assert harness.refresh_calls == 1
    assert harness.plugin_dir.exists() is False
    assert not (harness.exec_root / ".uninstall-backups").exists()
    assert _isolate_runtime_overrides == {}


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_stop_failure_keeps_original_runtime_and_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.running.add("demo")
    harness.stop_error = ServerDomainError(
        code="PLUGIN_STOP_FAILED",
        message="stop failed",
        status_code=500,
    )
    harness.install(monkeypatch)

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.code == "PLUGIN_STOP_FAILED"
    assert captured.value.stage == "stop"
    assert captured.value.filesystem_rollback == "not_needed"
    # The original runtime was never stopped, so it is still up.
    assert captured.value.runtime_restart == "succeeded"
    assert harness.manager.marked_removed == []
    assert harness.refresh_calls == 0
    assert harness.plugin_dir.is_dir()
    assert harness.config_path.is_file()
    assert _isolate_runtime_overrides == {}


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_profile_staging_failure_keeps_code_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)

    def _fail_stage(plugin_dir: Path, *, manager: object = None) -> None:
        del plugin_dir, manager
        raise PermissionError("profile is in use")

    monkeypatch.setattr(
        uninstall_module, "_stage_orphaned_package_profile_sync", _fail_stage
    )

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.stage == "stage_profile"
    assert captured.value.filesystem_rollback == "not_needed"
    assert captured.value.runtime_restart == "not_needed"
    assert harness.plugin_dir.is_dir()
    assert harness.manager.marked_removed == []
    assert harness.manager.restored_entries == []


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_code_staging_failure_restores_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    profile_dir = harness.profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "settings.toml").write_text("value = 1\n", encoding="utf-8")
    _package_entry_fakes(monkeypatch, harness)

    def _fail_code_stage(plugin_dir: Path) -> None:
        del plugin_dir
        raise PermissionError("code is in use")

    monkeypatch.setattr(uninstall_module, "_stage_plugin_code_sync", _fail_code_stage)

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.stage == "stage_code"
    # The profile was already staged, so the rollback genuinely restored it.
    assert captured.value.filesystem_rollback == "completed"
    assert captured.value.runtime_restart == "not_needed"
    assert profile_dir.is_dir()
    assert (profile_dir / "settings.toml").is_file()
    assert harness.plugin_dir.is_dir()
    assert harness.manager.marked_removed == []
    assert harness.manager.restored_entries == []


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_source_update_failure_restores_code_and_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    profile_dir = harness.profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "settings.toml").write_text("value = 1\n", encoding="utf-8")
    _package_entry_fakes(monkeypatch, harness)
    harness.manager.mark_removed_error = InstallSourceError(
        "LOCK_WRITE_FAILED",
        "lock write failed",
    )

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.stage == "update_source"
    assert captured.value.filesystem_rollback == "completed"
    assert captured.value.runtime_restart == "not_needed"
    assert harness.plugin_dir.is_dir()
    assert harness.config_path.is_file()
    assert profile_dir.is_dir()
    assert harness.manager.marked_removed == []
    # The soft-delete was attempted, so rollback best-effort restores the row.
    assert harness.manager.restored_entries == [
        harness.manager.entry_for_directory(harness.plugin_dir)
    ]


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_source_write_then_raise_restores_exact_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    """A source write that persists and then raises must restore the exact row."""
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    source_entry = harness.manager.entry_for_directory(harness.plugin_dir)
    assert source_entry is not None

    real_mark_removed = type(harness.manager).mark_removed

    def _mark_then_raise(self: _FakeManager, *, directory_path: Path) -> None:
        real_mark_removed(self, directory_path=directory_path)
        written_entry = self.entry_for_directory(
            directory_path,
            include_removed=True,
        )
        assert written_entry is not None and written_entry.removed is True
        raise InstallSourceError("SAVE_CRASHED", "crashed after write")

    monkeypatch.setattr(type(harness.manager), "mark_removed", _mark_then_raise)

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.stage == "update_source"
    assert captured.value.filesystem_rollback == "completed"
    assert harness.manager.marked_removed == [harness.plugin_dir]
    assert harness.manager.restored_entries == [source_entry]
    assert harness.manager.entry_for_directory(harness.plugin_dir) == source_entry
    assert harness.plugin_dir.is_dir()
    assert harness.config_path.is_file()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_registry_refresh_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    harness.refresh_error = RuntimeError("scan crashed")

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.stage == "refresh_and_preferences"
    assert captured.value.filesystem_rollback == "completed"
    assert captured.value.runtime_restart == "not_needed"
    # One failing refresh in the stage and one best-effort refresh in rollback.
    assert harness.refresh_calls == 2
    assert harness.manager.restored_entries == [
        harness.manager.entry_for_directory(harness.plugin_dir)
    ]
    assert harness.plugin_dir.is_dir()
    assert harness.config_path.is_file()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_registry_reported_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    harness.refresh_result = {
        "success": False,
        "failed": [{"plugin_id": "demo", "error": "scan failed"}],
    }

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.stage == "refresh_and_preferences"
    assert captured.value.filesystem_rollback == "completed"
    assert harness.refresh_calls == 2
    assert harness.manager.entry_for_directory(harness.plugin_dir) is not None
    assert harness.plugin_dir.is_dir()
    assert harness.config_path.is_file()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_ignores_unrelated_registry_scan_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    unrelated_config = harness.exec_root / "broken" / "plugin.toml"
    harness.refresh_result = {
        "success": False,
        "failed": [
            {
                "plugin_id": "broken",
                "config_path": str(unrelated_config),
                "error": "invalid manifest",
            }
        ],
    }

    result = await uninstall_plugin("demo")

    assert result.deleted_from_disk is True
    assert result.restored_builtin is False
    assert result.preference_action == "cleared"
    assert harness.refresh_calls == 1
    assert harness.manager.entry_for_directory(harness.plugin_dir) is None
    assert harness.plugin_dir.exists() is False


@pytest.mark.plugin_unit
def test_registry_failure_path_disambiguates_runtime_alias_declared_id(
    tmp_path: Path,
) -> None:
    target_config = tmp_path / "user" / "demo" / "plugin.toml"
    builtin_config = tmp_path / "builtin" / "demo" / "plugin.toml"
    target = uninstall_module._RegistryRefreshTarget(
        runtime_plugin_id="demo_1",
        declared_plugin_id="demo",
        config_paths=(target_config, builtin_config),
    )

    assert uninstall_module._registry_failure_matches_target(
        {
            "plugin_id": "demo",
            "config_path": str(tmp_path / "other" / "demo" / "plugin.toml"),
        },
        target=target,
    ) is False
    assert uninstall_module._registry_failure_matches_target(
        {"plugin_id": "different", "config_path": str(builtin_config)},
        target=target,
    ) is True
    assert uninstall_module._registry_failure_matches_target(
        {"plugin_id": "demo"},
        target=target,
    ) is True


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_restores_then_propagates_system_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    refresh_calls = 0

    async def _interrupt_once() -> dict[str, object]:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            raise SystemExit(17)
        return {"success": True, "failed": []}

    monkeypatch.setattr(
        uninstall_module.plugin_registry_service,
        "refresh_registry",
        _interrupt_once,
    )

    with pytest.raises(SystemExit) as captured:
        # Run the preserved transaction body in this task.  ``asyncio`` treats
        # SystemExit raised by a child Task as an event-loop shutdown signal,
        # which would bypass pytest's local exception assertion.
        await uninstall_plugin.__wrapped__("demo")

    assert captured.value.code == 17
    assert refresh_calls == 2
    assert harness.manager.entry_for_directory(harness.plugin_dir) is not None
    assert harness.plugin_dir.is_dir()
    assert harness.config_path.is_file()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_preference_clear_failure_restores_preference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    runtime_overrides_module.set_runtime_override("demo", True, auto_start=True)

    real_clear = uninstall_module.clear_runtime_override

    def _clear_then_fail(plugin_id: str) -> None:
        real_clear(plugin_id)
        assert runtime_overrides_module.get_runtime_override(plugin_id) is None
        raise OSError("override disk full")

    monkeypatch.setattr(uninstall_module, "clear_runtime_override", _clear_then_fail)

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.stage == "refresh_and_preferences"
    assert captured.value.filesystem_rollback == "completed"
    # The pre-commit preference restore ran successfully, so no extra detail.
    assert "preference_rollback" not in _details_of(captured.value)
    assert _isolate_runtime_overrides == {"demo": {"enabled": True, "auto_start": True}}
    assert harness.plugin_dir.is_dir()
    assert harness.manager.restored_entries != []


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_commit_failure_restores_auto_start_only_preference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    _isolate_runtime_overrides["demo"] = {"auto_start": True}
    runtime_overrides_module.reset_cache_for_testing()

    def _fail_commit(_staged: object) -> None:
        raise OSError("marker write failed")

    monkeypatch.setattr(
        uninstall_module,
        "_commit_staged_plugin_code_sync",
        _fail_commit,
    )

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.stage == "refresh_and_preferences"
    assert captured.value.filesystem_rollback == "completed"
    assert "preference_rollback" not in _details_of(captured.value)
    assert _isolate_runtime_overrides == {"demo": {"auto_start": True}}
    assert harness.manager.entry_for_directory(harness.plugin_dir) is not None
    assert harness.plugin_dir.is_dir()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_rollback_keeps_newer_concurrent_preference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    runtime_overrides_module.set_runtime_override("demo", False)
    commit_entered = threading.Event()
    release_commit = threading.Event()

    def _fail_commit_after_concurrent_write(_staged: object) -> None:
        commit_entered.set()
        release_commit.wait()
        raise OSError("marker write failed")

    monkeypatch.setattr(
        uninstall_module,
        "_commit_staged_plugin_code_sync",
        _fail_commit_after_concurrent_write,
    )

    task = asyncio.create_task(uninstall_plugin("demo"))
    entered = False
    try:
        entered = await asyncio.to_thread(commit_entered.wait, 5)
        if entered:
            runtime_overrides_module.set_runtime_override("demo", True)
    finally:
        release_commit.set()

    with pytest.raises(UninstallPluginError) as captured:
        await task

    assert entered is True
    assert captured.value.filesystem_rollback == "completed"
    assert "preference_rollback" not in _details_of(captured.value)
    assert _isolate_runtime_overrides == {"demo": True}
    assert harness.manager.entry_for_directory(harness.plugin_dir) is not None
    assert harness.plugin_dir.is_dir()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_rollback_restores_running_runtime_and_reports_separately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.running.add("demo")
    harness.install(monkeypatch)
    harness.refresh_error = RuntimeError("scan crashed")

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.filesystem_rollback == "completed"
    assert captured.value.runtime_restart == "succeeded"
    assert harness.start_calls == ["demo"]
    assert harness.plugin_dir.is_dir()
    assert harness.manager.restored_entries != []


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_rollback_does_not_start_plugin_that_was_not_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    harness.refresh_error = RuntimeError("scan crashed")

    with pytest.raises(UninstallPluginError) as captured:
        await uninstall_plugin("demo")

    assert captured.value.filesystem_rollback == "completed"
    assert captured.value.runtime_restart == "not_needed"
    assert harness.stop_calls == []
    assert harness.start_calls == []


def _patch_builtin_restore(monkeypatch: pytest.MonkeyPatch, harness: _Harness) -> None:
    """Make the harness flip demo to builtin source after refresh."""

    harness.builtin_after_refresh = True


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_restore_builtin_keeps_preferences(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    runtime_overrides_module.set_runtime_override("demo", False, auto_start=False)
    _patch_builtin_restore(monkeypatch, harness)

    result = await uninstall_plugin("demo")

    assert result.restored_builtin is True
    assert result.preference_action == "preserved"
    assert result.runtime_restart == "not_needed"
    assert _isolate_runtime_overrides == {
        "demo": {"enabled": False, "auto_start": False}
    }
    assert harness.plugin_dir.exists() is False


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_builtin_runtime_restore_failure_is_not_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.running.add("demo")
    harness.install(monkeypatch)
    _patch_builtin_restore(monkeypatch, harness)

    async def _fail_start(_self: object, plugin_id: str, **_kwargs: object) -> None:
        del plugin_id
        raise ServerDomainError(
            code="PLUGIN_START_FAILED",
            message="builtin start failed",
            status_code=500,
        )

    monkeypatch.setattr(
        lifecycle_module.PluginLifecycleService, "start_plugin", _fail_start
    )

    result = await uninstall_plugin("demo")

    assert result.filesystem_rollback == "not_needed"
    assert result.runtime_restart == "failed"
    assert result.cleanup_pending is False
    assert result.runtime_restart_error == {
        "code": "PLUGIN_BUILTIN_RESTORE_START_FAILED",
        "message": "builtin start failed",
        "error_type": "ServerDomainError",
    }
    assert harness.plugin_dir.exists() is False


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_committed_code_cleanup_failure_is_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)

    real_finalize = uninstall_module._finalize_committed_plugin_code_sync
    finalize_calls = 0

    def _fail_finalize_once(
        committed: uninstall_module._CommittedPluginCodeCleanup,
    ) -> bool:
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise OSError("staged tree locked")
        return real_finalize(committed)

    monkeypatch.setattr(
        uninstall_module,
        "_finalize_committed_plugin_code_sync",
        _fail_finalize_once,
    )

    result = await uninstall_plugin("demo")

    assert result.deleted_from_disk is True
    assert result.cleanup_pending is True
    assert result.filesystem_rollback == "not_needed"
    backup_root = harness.exec_root / ".uninstall-backups"
    transaction_dirs = list(backup_root.iterdir())
    assert len(transaction_dirs) == 1
    assert (transaction_dirs[0] / "committed.json").is_file()
    assert (
        transaction_dirs[0]
        / uninstall_module._CODE_PAYLOAD_DIR_NAME
        / "demo"
        / "plugin.toml"
    ).is_file()

    assert uninstall_module.retry_deferred_plugin_code_cleanup_sync() == 1
    assert backup_root.exists() is False


@pytest.mark.plugin_unit
def test_committed_code_cleanup_keeps_marker_after_partial_payload_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    staged = uninstall_module._stage_plugin_code_sync(harness.plugin_dir)
    committed = uninstall_module._commit_staged_plugin_code_sync(staged)
    real_rmtree = uninstall_module.shutil.rmtree
    failed_once = False

    def _partially_remove_then_fail(path: Path) -> None:
        nonlocal failed_once
        if Path(path) == staged.staged_dir and not failed_once:
            failed_once = True
            (staged.staged_dir / "plugin.toml").unlink()
            raise OSError("staged payload locked")
        real_rmtree(path)

    monkeypatch.setattr(uninstall_module.shutil, "rmtree", _partially_remove_then_fail)

    with pytest.raises(OSError, match="staged payload locked"):
        uninstall_module._finalize_committed_plugin_code_sync(committed)

    assert committed.marker_path.is_file()
    assert staged.staged_dir.is_dir()
    assert uninstall_module.retry_deferred_plugin_code_cleanup_sync() == 1
    assert committed.marker_path.parent.exists() is False


@pytest.mark.plugin_unit
def test_code_marker_cannot_collide_with_valid_plugin_directory_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, plugin_names=("committed.json",))
    harness.install(monkeypatch)
    plugin_dir = harness.exec_root / "committed.json"

    staged = uninstall_module._stage_plugin_code_sync(plugin_dir)
    committed = uninstall_module._commit_staged_plugin_code_sync(staged)

    assert staged.staged_dir.is_dir()
    assert staged.staged_dir.name == "committed.json"
    assert committed.marker_path.is_file()
    assert committed.marker_path != staged.staged_dir
    assert uninstall_module._finalize_committed_plugin_code_sync(committed) is True


@pytest.mark.plugin_unit
def test_deferred_code_cleanup_refuses_unmarked_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    transaction_dir = harness.exec_root / ".uninstall-backups" / ("a" * 32)
    staged_dir = transaction_dir / uninstall_module._CODE_PAYLOAD_DIR_NAME / "demo"
    staged_dir.mkdir(parents=True)
    (staged_dir / "plugin.toml").write_text(
        "[plugin]\nid='demo'\n",
        encoding="utf-8",
    )

    assert uninstall_module.retry_deferred_plugin_code_cleanup_sync() == 0
    assert (staged_dir / "plugin.toml").is_file()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_startup_cleanup_retries_profile_and_committed_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        lifecycle_module,
        "retry_deferred_profile_cleanup_sync",
        lambda: calls.append("profile") or 2,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "retry_deferred_plugin_code_cleanup_sync",
        lambda: calls.append("code") or 3,
    )

    cleaned = await lifecycle_module.PluginLifecycleService().retry_deferred_profile_cleanup()

    assert cleaned == 2
    assert calls == ["profile", "code"]


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_committed_profile_cleanup_failure_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    profile_dir = harness.profiles_root / "demo_package"
    profile_dir.mkdir(parents=True)
    (profile_dir / "settings.toml").write_text("value = 1\n", encoding="utf-8")
    _package_entry_fakes(monkeypatch, harness)

    def _fail_finalize(staged_profile: object) -> Path | None:
        del staged_profile
        raise OSError("profile tree locked")

    monkeypatch.setattr(
        uninstall_module, "_finalize_staged_package_profile_sync", _fail_finalize
    )
    record_path = tmp_path / "persistent" / "package_profile_cleanup.json"
    monkeypatch.setattr(
        uninstall_module,
        "_deferred_profile_cleanup_record_path_sync",
        lambda: record_path,
    )

    result = await uninstall_plugin("demo")

    assert result.deleted_from_disk is True
    assert result.deleted_profile_dir is None
    assert result.cleanup_pending is True
    assert result.filesystem_rollback == "not_needed"
    assert record_path.is_file()
    staged = [
        path
        for path in harness.profiles_root.iterdir()
        if path.name.startswith(".demo_package.deleting-")
    ]
    assert len(staged) == 1


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_never_touches_config_data_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path)
    harness.install(monkeypatch)
    hashes_before = harness.persistent_hashes()

    result = await uninstall_plugin("demo")

    assert result.deleted_from_disk is True
    assert harness.persistent_hashes() == hashes_before
    assert harness.config_file.is_file()
    assert harness.data_file.is_file()
    assert harness.cache_file.is_file()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_serializes_concurrent_transactions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    harness = _Harness(tmp_path, plugin_names=("demo", "beta"))
    harness.running.add("demo")
    harness.install(monkeypatch)
    second_acquire_attempted = _signal_second_operation_lock_acquire(monkeypatch)
    demo_refresh_entered = asyncio.Event()
    release_demo_refresh = asyncio.Event()
    original_refresh = harness._refresh_registry

    async def _gated_refresh() -> dict[str, object]:
        result = await original_refresh()
        if "demo" in harness.stop_calls and not demo_refresh_entered.is_set():
            demo_refresh_entered.set()
            await release_demo_refresh.wait()
        return result

    monkeypatch.setattr(
        uninstall_module.plugin_registry_service, "refresh_registry", _gated_refresh
    )

    demo_task = asyncio.create_task(uninstall_plugin("demo"))
    await demo_refresh_entered.wait()
    beta_task = asyncio.create_task(uninstall_plugin("beta"))
    await second_acquire_attempted.wait()

    assert "stop:beta" not in harness.log
    release_demo_refresh.set()
    demo_result = await demo_task
    beta_result = await beta_task

    assert demo_result.deleted_from_disk is True
    assert beta_result.deleted_from_disk is True
    # demo was running, beta was not: only demo stops, and beta's transaction
    # could not even reach its stop stage until demo's refresh gate opened.
    assert harness.log == ["stop:demo", "refresh", "refresh"]
    assert (harness.exec_root / "demo").exists() is False
    assert (harness.exec_root / "beta").exists() is False


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_uninstall_cancelled_waiter_propagates_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate_runtime_overrides: dict,
) -> None:
    """Cancelling a lock waiter aborts only the waiter; the holder finishes."""
    harness = _Harness(tmp_path, plugin_names=("demo", "beta"))
    harness.running.add("demo")
    harness.install(monkeypatch)
    second_acquire_attempted = _signal_second_operation_lock_acquire(monkeypatch)
    demo_refresh_entered = asyncio.Event()
    release_demo_refresh = asyncio.Event()
    original_refresh = harness._refresh_registry

    async def _gated_refresh() -> dict[str, object]:
        result = await original_refresh()
        if "demo" in harness.stop_calls and not demo_refresh_entered.is_set():
            demo_refresh_entered.set()
            await release_demo_refresh.wait()
        return result

    monkeypatch.setattr(
        uninstall_module.plugin_registry_service, "refresh_registry", _gated_refresh
    )

    demo_task = asyncio.create_task(uninstall_plugin("demo"))
    await demo_refresh_entered.wait()
    beta_task = asyncio.create_task(uninstall_plugin("beta"))
    await second_acquire_attempted.wait()

    beta_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await beta_task

    # The waiting transaction never mutated beta's files.
    assert "stop:beta" not in harness.log
    assert (harness.exec_root / "beta" / "plugin.toml").is_file()

    release_demo_refresh.set()
    demo_result = await demo_task
    assert demo_result.deleted_from_disk is True
    assert (harness.exec_root / "demo").exists() is False
    assert (harness.exec_root / "beta" / "plugin.toml").is_file()


@pytest.mark.plugin_unit
@pytest.mark.asyncio
async def test_delete_plugin_cancellation_finishes_event_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Once acquired, cancellation waits for mutation and lifecycle event."""
    entered = asyncio.Event()
    release = asyncio.Event()
    mutation_finished = asyncio.Event()
    events: list[dict[str, object]] = []

    async def _gated_uninstall(plugin_id: str) -> SimpleNamespace:
        entered.set()
        await release.wait()
        mutation_finished.set()
        return SimpleNamespace(
            plugin_id=plugin_id,
            plugin_dir=tmp_path / plugin_id,
            deleted_from_disk=True,
            deleted_profile_dir=None,
            restored_builtin=False,
            preference_action="cleared",
            filesystem_rollback="not_needed",
            runtime_restart="not_needed",
            cleanup_pending=False,
            runtime_restart_error=None,
        )

    monkeypatch.setattr(lifecycle_module, "uninstall_plugin", _gated_uninstall)
    monkeypatch.setattr(
        lifecycle_module,
        "_emit_lifecycle_event",
        lambda **kwargs: events.append(kwargs),
    )

    task = asyncio.create_task(
        lifecycle_module.PluginLifecycleService().delete_plugin("demo")
    )
    await entered.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert mutation_finished.is_set()
    assert [event["event_type"] for event in events] == ["plugin_deleted"]
