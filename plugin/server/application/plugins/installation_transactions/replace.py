from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import shutil
import stat
import tomllib

from plugin import settings
from plugin.core.plugin_layout import PluginLayout
from plugin.logging_config import get_logger
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.config_paths import ensure_plugin_layout_runtime_config
from plugin.settings import get_plugin_state_root

logger = get_logger("server.application.plugins.installation_transactions.replace")

_MANIFEST_ADJACENT_PROFILE_NAMES = {
    "profiles.toml": "profiles.toml",
    "profiles": "profiles",
}


@dataclass(frozen=True, slots=True)
class ReplacePluginResult:
    restarted: bool
    rollback_status: str
    install_result: dict[str, object]
    backup_dir: Path


class ReplacePluginError(RuntimeError):
    def __init__(self, *, stage: str, rollback_status: str, cause: Exception) -> None:
        super().__init__(f"{stage} failed: {cause}")
        self.stage = stage
        self.rollback_status = rollback_status
        self.cause = cause


async def _plugin_is_running(plugin_id: str) -> bool:
    if not plugin_id:
        return False
    try:
        from plugin.server.application.plugins.lifecycle_service import _plugin_is_running_sync

        return await asyncio.to_thread(_plugin_is_running_sync, plugin_id)
    except Exception as exc:  # pragma: no cover - defensive host-registry boundary
        logger.warning(
            "lifecycle running-state probe failed plugin_id={} err_type={}",
            plugin_id,
            type(exc).__name__,
        )
        raise


async def _stop_plugin(plugin_id: str) -> None:
    if not plugin_id:
        return
    from plugin.server.application.plugins.lifecycle_service import PluginLifecycleService

    try:
        await PluginLifecycleService().stop_plugin(plugin_id)
    except ServerDomainError as exc:
        if getattr(exc, "code", None) == "PLUGIN_NOT_RUNNING":
            return
        raise


async def _start_plugin(plugin_id: str) -> None:
    if not plugin_id:
        return
    from plugin.server.application.plugins.lifecycle_service import PluginLifecycleService

    # 这里原本要显式清一次元数据扫描缓存：替换/回滚刚动过盘，而缓存键（路径 +
    # mtime_ns + size）看不见目录外的依赖变化，回滚更可能把时间戳原样拷回来。
    # 缓存没有了——刷新每次都重读盘上的 manifest 和 plugin.meta.json——所以这一步
    # 连同它要防的那类陈旧结果一起消失了。
    try:
        await PluginLifecycleService().start_plugin(plugin_id)
        return
    except Exception as exc:
        logger.error(
            "lifecycle restart failed plugin_id={} err_type={}",
            plugin_id,
            type(exc).__name__,
        )
        raise


def backup_path_for(target_dir: Path, *, backup_root: Path | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%f")
    root = backup_root or target_dir.parent / ".upgrade-backups"
    return root / f"{target_dir.name}.bak.{timestamp}"


async def restore_directory(backup_dir: Path, target_dir: Path) -> None:
    if not backup_dir.exists():
        return
    await remove_directory(target_dir)
    await asyncio.to_thread(backup_dir.rename, target_dir)


async def remove_directory(target_dir: Path) -> None:
    if not target_dir.exists():
        return
    await asyncio.to_thread(shutil.rmtree, target_dir)


def _validate_installed_identity(layout: PluginLayout) -> None:
    manifest_path = layout.installed_dir / "plugin.toml"
    if manifest_path.resolve(strict=False) != layout.manifest_path.resolve(strict=False):
        raise ValueError("plugin layout manifest does not belong to the replacement target")
    try:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"installed plugin.toml not found: {manifest_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"installed plugin.toml is invalid TOML: {manifest_path}") from exc
    plugin_table = data.get("plugin")
    if not isinstance(plugin_table, dict):
        raise ValueError(f"installed plugin.toml missing [plugin] table: {manifest_path}")
    installed_plugin_id = plugin_table.get("id")
    if not isinstance(installed_plugin_id, str) or not installed_plugin_id.strip():
        raise ValueError(f"installed plugin.toml missing [plugin].id: {manifest_path}")
    if installed_plugin_id.strip() != layout.plugin_id:
        raise ValueError("installed plugin identity does not match the replacement target")


async def merge_directory_contents(source_dir: Path, target_dir: Path) -> None:
    if not source_dir.exists():
        return
    if not source_dir.is_dir():
        raise NotADirectoryError(source_dir)
    await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copytree, source_dir, target_dir, dirs_exist_ok=True)


def _assert_preserved_tree_has_no_links_or_reparse_points(source: Path) -> None:
    pending = [source]
    while pending:
        current = pending.pop()
        metadata = current.lstat()
        file_attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_attribute):
            raise OSError(
                f"links and reparse points are not supported for preserved plugin state: {current.name}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        with os.scandir(current) as entries:
            pending.extend(Path(entry.path) for entry in entries)


def _canonical_profile_sources(sources: list[Path]) -> dict[str, Path]:
    sources_by_name: dict[str, Path] = {}
    for source in sources:
        canonical_name = _MANIFEST_ADJACENT_PROFILE_NAMES.get(source.name.casefold())
        if canonical_name is None:
            continue
        if canonical_name in sources_by_name:
            raise OSError(f"multiple legacy profile paths map to {canonical_name}")
        sources_by_name[canonical_name] = source
    return sources_by_name


async def _restore_manifest_adjacent_profiles(backup_dir: Path, target_dir: Path) -> None:
    sources = await asyncio.to_thread(lambda: list(backup_dir.iterdir()))
    sources_by_name = _canonical_profile_sources(sources)

    for canonical_name, source in sources_by_name.items():
        await asyncio.to_thread(_assert_preserved_tree_has_no_links_or_reparse_points, source)
        target = target_dir / canonical_name
        if source.is_dir():
            await merge_directory_contents(source, target)
            continue
        if not source.is_file():
            raise OSError(f"unsupported profile path: {source}")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, source, target)


async def run_rollback(
    *,
    plugin_id: str,
    target_dir: Path,
    backup_dir: Path,
    restart: bool,
) -> bool:
    restored = True
    try:
        await restore_directory(backup_dir, target_dir)
        # 回滚同样是"树变了"。而且它比升级更容易骗过指纹：备份是拷回去的，时间戳
        # 完全可能原样保留。这条路不走上面那个 invalidate_cache 阶段，所以自己清。
        await asyncio.to_thread(_evict_replaced_plugin_modules, plugin_id)
    except Exception as exc:
        restored = False
        logger.error(
            "plugin directory rollback failed plugin_id={} err_type={}",
            plugin_id,
            type(exc).__name__,
        )
    if restart:
        try:
            await _start_plugin(plugin_id)
        except Exception as exc:
            restored = False
            logger.error(
                "plugin rollback restart failed plugin_id={} err_type={}",
                plugin_id,
                type(exc).__name__,
            )
    return restored


async def _rollback_targets(
    *,
    targets: tuple[Path, ...],
    backups: dict[Path, Path],
    preexisting_targets: frozenset[Path],
    remove_created_targets: bool,
) -> bool:
    restored = True
    for target in reversed(targets):
        backup = backups.get(target)
        if backup is None:
            if remove_created_targets and target not in preexisting_targets:
                try:
                    await remove_directory(target)
                except Exception as exc:
                    restored = False
                    logger.error(
                        "plugin replacement created-target cleanup failed target={} err_type={}",
                        target.name,
                        type(exc).__name__,
                    )
            continue
        try:
            await remove_directory(target)
            await restore_directory(backup, target)
        except Exception as exc:
            restored = False
            logger.error(
                "plugin replacement target rollback failed target={} err_type={}",
                target.name,
                type(exc).__name__,
            )
    return restored


def _notify_rollback_start(callback: Callable[[], None] | None) -> None:
    if callback is None:
        return
    try:
        callback()
    except Exception as exc:
        logger.warning(
            "plugin replacement rollback observer failed err_type={}",
            type(exc).__name__,
        )


def _evict_replaced_plugin_modules(plugin_id: str) -> None:
    from plugin.core.host import evict_cached_plugin_modules

    # 已导入的模块仍然要清：这个进程里可能残留着被换掉的那份代码。元数据扫描缓存
    # 曾经也在这里一起清，现在没有那个缓存了。
    evict_cached_plugin_modules(plugin_id)


def _path_is_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def _validate_replacement_targets(
    targets: tuple[Path, ...],
    *,
    state_root: Path | None = None,
) -> None:
    resolved_targets = tuple(target.resolve(strict=False) for target in targets)
    overlapping: set[Path] = set()
    for index, target in enumerate(resolved_targets):
        for other in resolved_targets[index + 1 :]:
            if target == other or target in other.parents or other in target.parents:
                overlapping.update((target, other))
    if overlapping:
        raise ValueError(
            "plugin replacement targets must be distinct and non-overlapping: "
            + ", ".join(str(path) for path in sorted(overlapping, key=str))
        )

    state_roots = {get_plugin_state_root().resolve(strict=False)}
    if state_root is not None:
        state_roots.add(state_root.resolve(strict=False))
    forbidden = [
        target
        for target in targets
        if any(
            _path_is_within(target, root) or _path_is_within(root, target)
            for root in state_roots
        )
    ]
    if forbidden:
        raise ValueError(
            "plugin persistent state paths cannot be replacement targets: "
            + ", ".join(str(path) for path in forbidden)
        )

    builtin_root = Path(settings.BUILTIN_PLUGIN_CONFIG_ROOT).resolve(strict=False)
    immutable = [
        target
        for target in targets
        if _path_is_within(target, builtin_root) or _path_is_within(builtin_root, target)
    ]
    if immutable:
        raise ValueError(
            "immutable builtin plugin paths cannot be replacement targets: "
            + ", ".join(str(path) for path in immutable)
        )


async def replace_plugin(
    *,
    layout: PluginLayout,
    install_new: Callable[[], Awaitable[dict[str, object]]],
    additional_targets: tuple[Path, ...] = (),
    preserve_targets: tuple[Path, ...] = (),
    initialize_runtime_config: bool = True,
    validate_backup: Callable[[Path], Awaitable[None]] | None = None,
    validate_channel_specific: Callable[[], Awaitable[None]] | None = None,
    on_rollback_start: Callable[[], None] | None = None,
) -> ReplacePluginResult:
    plugin_id = layout.plugin_id
    target_dir = layout.installed_dir
    if not plugin_id:
        raise ValueError("plugin replacement requires a plugin id")
    if not target_dir.is_dir():
        raise FileNotFoundError(f"installed plugin directory is missing: {target_dir.name}")
    targets = (target_dir, *additional_targets)
    if any(target not in targets for target in preserve_targets):
        raise ValueError("preserve targets must also be replacement targets")
    _validate_replacement_targets(
        targets,
        state_root=layout.data_dir.parent.parent,
    )

    if initialize_runtime_config:
        await asyncio.to_thread(
            ensure_plugin_layout_runtime_config,
            layout,
        )
    was_running = await _plugin_is_running(plugin_id)
    if was_running:
        await _stop_plugin(plugin_id)

    preexisting_targets = frozenset(target for target in targets if target.exists())
    backups: dict[Path, Path] = {}
    backup_dir = backup_path_for(target_dir)
    try:
        for target in targets:
            if not target.exists():
                continue
            if not target.is_dir():
                raise NotADirectoryError(target)
            backup = backup_dir if target == target_dir else backup_path_for(target)
            await asyncio.to_thread(backup.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.rename, backup)
            backups[target] = backup
    except Exception as exc:
        _notify_rollback_start(on_rollback_start)
        recovered = await _rollback_targets(
            targets=targets,
            backups=backups,
            preexisting_targets=preexisting_targets,
            remove_created_targets=False,
        )
        if was_running:
            try:
                await _start_plugin(plugin_id)
            except Exception as restart_exc:
                recovered = False
                logger.error(
                    "plugin restart after backup failure failed plugin_id={} err_type={}",
                    plugin_id,
                    type(restart_exc).__name__,
                )
        raise ReplacePluginError(
            stage="backup",
            rollback_status="completed" if recovered else "incomplete",
            cause=exc,
        ) from exc
    stage = "backup_validation"
    try:
        if validate_backup is not None:
            await validate_backup(backups[target_dir])
        stage = "install"
        install_result = await install_new()
        stage = "validate"
        await asyncio.to_thread(_validate_installed_identity, layout)
        if validate_channel_specific is not None:
            await validate_channel_specific()
        stage = "preserve"
        for target in preserve_targets:
            backup = backups.get(target)
            if backup is not None:
                await merge_directory_contents(backup, target)
        await _restore_manifest_adjacent_profiles(backup_dir, target_dir)
        stage = "invalidate_cache"
        await asyncio.to_thread(_evict_replaced_plugin_modules, plugin_id)
        if was_running:
            stage = "restart"
            await _start_plugin(plugin_id)
        stage = "cleanup"
        for backup in backups.values():
            try:
                await remove_directory(backup)
            except Exception as exc:  # cleanup must not roll back a valid replacement
                logger.warning(
                    "plugin backup cleanup failed plugin_id={} err_type={}",
                    plugin_id,
                    type(exc).__name__,
                )
        return ReplacePluginResult(
            restarted=was_running,
            rollback_status="not_needed",
            install_result=install_result,
            backup_dir=backup_dir,
        )
    except Exception as exc:
        _notify_rollback_start(on_rollback_start)
        restored = await _rollback_targets(
            targets=targets,
            backups=backups,
            preexisting_targets=preexisting_targets,
            remove_created_targets=True,
        )
        try:
            await asyncio.to_thread(_evict_replaced_plugin_modules, plugin_id)
        except Exception as eviction_exc:
            restored = False
            logger.error(
                "plugin rollback cache invalidation failed plugin_id={} err_type={}",
                plugin_id,
                type(eviction_exc).__name__,
            )
        if was_running:
            try:
                await _start_plugin(plugin_id)
            except Exception as restart_exc:
                restored = False
                logger.error(
                    "plugin rollback restart failed plugin_id={} err_type={}",
                    plugin_id,
                    type(restart_exc).__name__,
                )
        raise ReplacePluginError(
            stage=stage,
            rollback_status="completed" if restored else "incomplete",
            cause=exc,
        ) from exc
