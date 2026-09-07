"""Transactional uninstall of one installer-owned plugin candidate."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
from typing import Literal
import uuid

from plugin.core.state import state
from plugin.logging_config import get_logger
from plugin.server.application.install_source import (
    InstallSourceError,
    InstallSourceManager,
    get_install_source_manager,
)
from plugin.server.application.install_source.models import LockEntry
from plugin.server.application.install_source.scanner import PluginDirectoryScanner
from plugin.server.application.plugins.operation_lock import serialized_plugin_operation
from plugin.server.application.plugins.registry_service import PluginRegistryService
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.autostart_approvals import (
    clear_autostart_pending,
    is_autostart_approved,
    mark_autostart_pending,
)
from plugin.server.infrastructure.runtime_overrides import (
    RuntimeOverride,
    clear_runtime_override,
    get_runtime_override_entry,
    restore_runtime_override,
)
from plugin.settings import (
    BUILTIN_PLUGIN_CONFIG_ROOT,
    PLUGIN_CONFIG_ROOTS,
    ensure_plugin_exec_state_roots_separated,
    get_plugin_state_root,
    get_user_package_profiles_root,
    get_user_plugin_config_root,
    get_user_plugin_exec_root,
)

from .ownership import UninstallOwnershipError, require_uninstall_ownership

logger = get_logger("server.application.plugins.installation_transactions.uninstall")
plugin_registry_service = PluginRegistryService()

_DEFERRED_PROFILE_CLEANUP_FILENAME = "package_profile_cleanup.json"
_CODE_BACKUP_ROOT_NAME = ".uninstall-backups"
_CODE_PAYLOAD_DIR_NAME = "payload"
_CODE_COMMIT_MARKER_FILENAME = "committed.json"
_CODE_TRANSACTION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
# ``package_id`` allows dots, so the staged name must too; the leading dot,
# the ``.deleting-<uuid4hex>`` suffix and full-name anchoring keep it exact.
_DEFERRED_PROFILE_STAGING_NAME_PATTERN = re.compile(
    r"^\.[A-Za-z0-9._-]+\.deleting-[0-9a-f]{32}$"
)

PreferenceAction = Literal["preserved", "cleared"]
FilesystemRollback = Literal["not_needed", "completed", "incomplete"]
RuntimeRestart = Literal["not_needed", "succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class UninstallPluginResult:
    plugin_id: str
    plugin_dir: Path
    deleted_from_disk: bool
    deleted_profile_dir: Path | None
    restored_builtin: bool
    preference_action: PreferenceAction
    filesystem_rollback: FilesystemRollback
    runtime_restart: RuntimeRestart
    cleanup_pending: bool
    runtime_restart_error: dict[str, str] | None = None


class UninstallPluginError(RuntimeError):
    """Stable uninstall failure with compensation outcomes kept separate."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        stage: str,
        filesystem_rollback: FilesystemRollback = "not_needed",
        runtime_restart: RuntimeRestart = "not_needed",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.stage = stage
        self.filesystem_rollback = filesystem_rollback
        self.runtime_restart = runtime_restart
        self.details = {
            **(details or {}),
            "stage": stage,
            "filesystem_rollback": filesystem_rollback,
            "runtime_restart": runtime_restart,
        }


@dataclass(frozen=True, slots=True)
class _StagedPackageProfile:
    original_dir: Path
    staged_dir: Path


@dataclass(frozen=True, slots=True)
class _StagedPluginCode:
    original_dir: Path
    staged_dir: Path
    transaction_id: str


@dataclass(frozen=True, slots=True)
class _CommittedPluginCodeCleanup:
    staged: _StagedPluginCode
    marker_path: Path


@dataclass(frozen=True, slots=True)
class _RuntimePreferenceSnapshot:
    entry: RuntimeOverride | None


@dataclass(frozen=True, slots=True)
class _RegistryRefreshTarget:
    runtime_plugin_id: str
    declared_plugin_id: str
    config_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _RollbackOutcome:
    filesystem_rollback: FilesystemRollback
    runtime_restart: RuntimeRestart
    preference_restored: bool


def _get_plugin_meta_sync(plugin_id: str) -> dict[str, object] | None:
    # Registration metadata stays owned by the lifecycle module; the
    # transaction resolves it lazily to avoid an import cycle.
    from plugin.server.application.plugins.lifecycle_service import (
        _get_plugin_meta_sync as get_meta,
    )

    return get_meta(plugin_id)


def _resolve_plugin_config_path_sync(
    plugin_id: str,
    plugin_meta: dict[str, object],
) -> Path | None:
    # Resolution is shared lifecycle knowledge, not a caller-supplied callback.
    from plugin.server.application.plugins.lifecycle_service import (
        _resolve_plugin_config_path_sync as resolve,
    )

    return resolve(plugin_id, plugin_meta)


def _plugin_is_running_sync(plugin_id: str) -> bool:
    from plugin.server.application.plugins.lifecycle_service import (
        _plugin_is_running_sync as is_running,
    )

    return is_running(plugin_id)


def _path_within_plugin_roots_sync(path: Path) -> bool:
    try:
        resolved_path = path.resolve()
    except Exception:
        resolved_path = path

    resolved_exec_root = get_user_plugin_exec_root().resolve(strict=False)
    resolved_builtin_root = BUILTIN_PLUGIN_CONFIG_ROOT.resolve(strict=False)
    resolved_state_root = get_plugin_state_root().resolve(strict=False)
    if resolved_exec_root == resolved_state_root:
        return False
    allowed_roots: set[Path] = set()
    if resolved_exec_root not in {resolved_builtin_root, resolved_state_root}:
        allowed_roots.add(resolved_exec_root)
    # Preserve injected/test roots while explicitly excluding both immutable
    # builtin code and the SDK-owned persistent state root.
    for root in PLUGIN_CONFIG_ROOTS:
        resolved_root = root.resolve(strict=False)
        if resolved_root not in {resolved_builtin_root, resolved_state_root}:
            allowed_roots.add(resolved_root)
    # Deletion owns one direct child installation only. In particular, the
    # builtin root and SDK state root are never acceptable lifecycle targets.
    return resolved_path.parent in allowed_roots


def _remove_runtime_metadata_sync(plugin_id: str) -> None:
    from plugin.server.application.plugins.lifecycle_service import (
        _pop_plugin_host_sync,
        _remove_event_handlers_sync,
    )

    _pop_plugin_host_sync(plugin_id)
    _remove_event_handlers_sync(plugin_id)
    removed = False
    with state.acquire_plugins_write_lock():
        if plugin_id in state.plugins:
            state.plugins.pop(plugin_id, None)
            removed = True
    if removed:
        state.invalidate_snapshot_cache("plugins")


async def _stop_plugin(plugin_id: str) -> None:
    from plugin.server.application.plugins.lifecycle_service import (
        PluginLifecycleService,
    )

    await PluginLifecycleService().stop_plugin(plugin_id)


async def _start_plugin(plugin_id: str) -> None:
    from plugin.server.application.plugins.lifecycle_service import (
        PluginLifecycleService,
    )

    await PluginLifecycleService().start_plugin(plugin_id, refresh_registry=False)


def _profile_path_from_entry_sync(entry: LockEntry, profiles_root: Path) -> Path | None:
    if entry.channel not in {"imported", "market"}:
        return None
    if entry.profile_installed is False:
        return None
    package_id = entry.package_id or entry.plugin_id
    if not package_id:
        return None
    candidate = (
        Path(entry.profile_dir).expanduser()
        if entry.profile_dir
        else profiles_root / package_id
    )
    if _path_has_symlink_ancestor(candidate):
        return None
    try:
        profile_dir = candidate.resolve()
    except Exception:
        return None
    if profile_dir.name != package_id:
        return None
    # A recorded profile location remains valid after the configured profile
    # root changes. Legacy fallback paths are still constrained to that root.
    if not entry.profile_dir and (
        profile_dir != profiles_root and profiles_root not in profile_dir.parents
    ):
        return None
    return profile_dir


def _path_has_symlink_ancestor(path: Path) -> bool:
    """Reject a path when resolving it would traverse a symlink."""
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def _has_other_entry_without_package_id(
    active_entries: Sequence[LockEntry],
    current_primary_key: tuple[str, str],
) -> bool:
    """Report whether another installed row also predates package id tracking."""
    for entry in active_entries:
        if entry.channel not in {"imported", "market"}:
            continue
        key = (entry.root_id, entry.directory_name)
        if key == current_primary_key:
            continue
        if not entry.package_id:
            return True
    return False


def _deferred_profile_cleanup_record_path_sync() -> Path:
    return (
        get_plugin_state_root().expanduser().resolve().parent
        / _DEFERRED_PROFILE_CLEANUP_FILENAME
    )


def _legacy_deferred_profile_cleanup_record_path_sync() -> Path:
    return (
        get_user_plugin_config_root().expanduser().resolve().parent
        / _DEFERRED_PROFILE_CLEANUP_FILENAME
    )


def _load_deferred_profile_cleanup_paths_sync(record_path: Path) -> list[str] | None:
    """Return the pending paths, or ``None`` when an existing record is unusable.

    Callers must not overwrite a record they could not read: the paths already
    queued in it would be dropped and their staging directories would then be
    retained forever.
    """
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError) as exc:
        logger.error(
            "uninstall: failed to read deferred profile cleanup record {}: {}",
            record_path,
            exc,
        )
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("staged_paths"), list):
        logger.error(
            "uninstall: invalid deferred profile cleanup record: {}", record_path
        )
        return None
    return [path for path in raw["staged_paths"] if isinstance(path, str) and path]


def _save_deferred_profile_cleanup_paths_sync(
    record_path: Path, paths: list[str]
) -> None:
    if not paths:
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass
        return
    record_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = record_path.with_name(
        f".{record_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps({"schema_version": 1, "staged_paths": paths}),
            encoding="utf-8",
        )
        temporary_path.replace(record_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _record_deferred_profile_cleanup_sync(
    staged_profile: _StagedPackageProfile,
) -> bool:
    try:
        record_path = _deferred_profile_cleanup_record_path_sync()
        paths = _load_deferred_profile_cleanup_paths_sync(record_path)
        if paths is None:
            return False
        staged_path = str(staged_profile.staged_dir)
        if staged_path not in paths:
            paths.append(staged_path)
        _save_deferred_profile_cleanup_paths_sync(record_path, paths)
        return True
    except Exception as exc:
        logger.error(
            "uninstall: failed to persist deferred profile cleanup for {}: {}",
            staged_profile.staged_dir,
            exc,
        )
        return False


def _is_safe_deferred_profile_cleanup_path(path: Path) -> bool:
    return (
        path.is_absolute()
        and _DEFERRED_PROFILE_STAGING_NAME_PATTERN.fullmatch(path.name) is not None
        and not _path_has_symlink_ancestor(path)
    )


def retry_deferred_profile_cleanup_sync() -> int:
    """Retry profile cleanup jobs persisted after transient deletion failures."""
    record_path = _deferred_profile_cleanup_record_path_sync()
    record_paths = [record_path]
    legacy_record_path = _legacy_deferred_profile_cleanup_record_path_sync()
    if legacy_record_path != record_path and legacy_record_path.exists():
        record_paths.append(legacy_record_path)

    paths: list[str] = []
    for candidate in record_paths:
        loaded = _load_deferred_profile_cleanup_paths_sync(candidate)
        if loaded is None:
            return 0
        for raw_path in loaded:
            if raw_path not in paths:
                paths.append(raw_path)
    if not paths:
        return 0

    remaining_paths: list[str] = []
    cleaned = 0
    for raw_path in paths:
        staged_path = Path(raw_path).expanduser()
        if not _is_safe_deferred_profile_cleanup_path(staged_path):
            logger.error(
                "uninstall: refusing unsafe deferred profile cleanup path: {}",
                staged_path,
            )
            remaining_paths.append(raw_path)
            continue
        try:
            shutil.rmtree(staged_path)
        except FileNotFoundError:
            cleaned += 1
        except OSError as exc:
            logger.warning(
                "uninstall: deferred profile cleanup still pending for {}: {}",
                staged_path,
                exc,
            )
            remaining_paths.append(raw_path)
        else:
            cleaned += 1
    try:
        _save_deferred_profile_cleanup_paths_sync(record_path, remaining_paths)
    except OSError as exc:
        logger.error(
            "uninstall: failed to update deferred profile cleanup record {}: {}",
            record_path,
            exc,
        )
    else:
        for legacy_path in record_paths[1:]:
            try:
                legacy_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "uninstall: failed to remove migrated cleanup record {}: {}",
                    legacy_path,
                    exc,
                )
    return cleaned


def _stage_orphaned_package_profile_sync(
    plugin_dir: Path,
    *,
    manager: InstallSourceManager | None = None,
) -> _StagedPackageProfile | None:
    """Stage an unshared package profile while deletion is in progress.

    Moving the profile out of its package location prevents a concurrent
    reinstall from seeing it, but preserves it until executable deletion has
    succeeded. This lets a failed executable deletion roll back without
    losing the plugin's persisted configuration.
    """
    manager = manager or get_install_source_manager()
    if manager is None:
        return None

    try:
        current_entry = manager.entry_for_directory(
            plugin_dir,
            include_removed=False,
        )
        active_entries = manager.list_entries()
    except InstallSourceError as exc:
        logger.warning(
            "uninstall: failed to inspect install source for plugin_dir={}: {}",
            plugin_dir,
            exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "uninstall: unexpected install-source cleanup failure for plugin_dir={}: {}",
            plugin_dir,
            exc,
        )
        return None

    # Only package installers own package profiles. A scanner-created manual
    # entry with no profile record must never infer ownership from a matching
    # directory name.
    if current_entry is None or current_entry.channel not in {"imported", "market"}:
        return None

    current_primary_key = (current_entry.root_id, current_entry.directory_name)
    recorded_package_id = current_entry.package_id
    if _has_other_entry_without_package_id(active_entries, current_primary_key):
        # Rows written before the package id was tracked do not say which
        # package owns their profile, and a bundle's profile is named after the
        # package rather than after any member plugin. Such a row may be a
        # sibling from the same bundle that still uses this profile, and the
        # sharing check below cannot see it: it can only infer that row's
        # profile from its plugin id, which is not where a bundle profile
        # lives. This holds whichever side is missing the package id, so the
        # guard looks at every other installed row rather than only at ours.
        #
        # Cost: one such row suppresses profile cleanup for every deletion
        # until it is itself removed or reinstalled, which degrades to the
        # pre-change behaviour (a stale profile blocks a reinstall). That is
        # strictly better than permanently deleting a sibling's configuration.
        logger.warning(
            "uninstall: skipping profile cleanup while an installation "
            "without a recorded package id may share this profile: {}",
            plugin_dir,
        )
        return None

    package_id = recorded_package_id or current_entry.plugin_id
    if not package_id:
        return None
    recorded_profile_dir = current_entry.profile_dir
    if current_entry.profile_installed is False:
        return None

    try:
        profiles_root = get_user_package_profiles_root().resolve()
        profile_candidate = (
            Path(recorded_profile_dir).expanduser()
            if recorded_profile_dir
            else profiles_root / package_id
        )
        if _path_has_symlink_ancestor(profile_candidate):
            logger.warning(
                "uninstall: refusing symlinked package profile path: {}",
                profile_candidate,
            )
            return None
        current_profile_dir = profile_candidate.resolve()
    except Exception as exc:
        logger.warning(
            "uninstall: failed to resolve package profile for plugin_dir={}: {}",
            plugin_dir,
            exc,
        )
        return None

    state_root = get_plugin_state_root().expanduser().resolve(strict=False)
    if (
        current_profile_dir == state_root
        or state_root in current_profile_dir.parents
        or current_profile_dir in state_root.parents
    ):
        logger.warning(
            "uninstall: refusing package profile overlapping the persistent "
            "state root: {}",
            current_profile_dir,
        )
        return None

    builtin_root = Path(BUILTIN_PLUGIN_CONFIG_ROOT).expanduser().resolve(strict=False)
    if (
        current_profile_dir == builtin_root
        or builtin_root in current_profile_dir.parents
        or current_profile_dir in builtin_root.parents
    ):
        logger.warning(
            "uninstall: refusing package profile overlapping the builtin "
            "plugin root: {}",
            current_profile_dir,
        )
        return None

    if current_profile_dir.name != package_id or (
        not recorded_profile_dir
        and (
            current_profile_dir != profiles_root
            and profiles_root not in current_profile_dir.parents
        )
    ):
        logger.warning(
            "uninstall: refusing unsafe package profile path: {}",
            current_profile_dir,
        )
        return None

    for entry in active_entries:
        if (entry.root_id, entry.directory_name) == current_primary_key:
            continue
        if _profile_path_from_entry_sync(entry, profiles_root) == current_profile_dir:
            return None

    staged_profile_dir = current_profile_dir.with_name(
        f".{current_profile_dir.name}.deleting-{uuid.uuid4().hex}"
    )
    try:
        current_profile_dir.replace(staged_profile_dir)
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception(
            "uninstall: failed to stage package profile {}", current_profile_dir
        )
        raise
    return _StagedPackageProfile(
        original_dir=current_profile_dir,
        staged_dir=staged_profile_dir,
    )


def _restore_staged_package_profile_sync(staged_profile: _StagedPackageProfile) -> None:
    """Restore a profile after executable deletion failed."""
    if not staged_profile.staged_dir.exists():
        return
    staged_profile.staged_dir.replace(staged_profile.original_dir)


def _finalize_staged_package_profile_sync(
    staged_profile: _StagedPackageProfile,
) -> Path | None:
    """Permanently remove a profile only after executable deletion succeeds."""
    try:
        shutil.rmtree(staged_profile.staged_dir)
    except FileNotFoundError:
        return None
    return staged_profile.original_dir


def _stage_plugin_code_sync(plugin_dir: Path) -> _StagedPluginCode:
    """Stage the code directory with a same-filesystem rename, never rmtree."""
    transaction_id = uuid.uuid4().hex
    backup_root = plugin_dir.parent / _CODE_BACKUP_ROOT_NAME
    transaction_dir = backup_root / transaction_id
    backup_root.mkdir(exist_ok=True)
    if (
        backup_root.is_symlink()
        or backup_root.resolve(strict=False).parent
        != plugin_dir.parent.resolve(strict=False)
    ):
        raise ValueError("uninstall code backup root is not a direct local directory")
    transaction_dir.mkdir(exist_ok=False)
    payload_dir = transaction_dir / _CODE_PAYLOAD_DIR_NAME
    payload_dir.mkdir()
    staged_dir = payload_dir / plugin_dir.name
    try:
        plugin_dir.replace(staged_dir)
    except BaseException:
        try:
            payload_dir.rmdir()
            transaction_dir.rmdir()
            backup_root.rmdir()
        except OSError:
            pass
        raise
    return _StagedPluginCode(plugin_dir, staged_dir, transaction_id)


def _restore_staged_plugin_code_sync(staged: _StagedPluginCode) -> None:
    if staged.staged_dir.exists():
        staged.staged_dir.replace(staged.original_dir)
    payload_dir = staged.staged_dir.parent
    transaction_dir = payload_dir.parent
    backup_root = transaction_dir.parent
    try:
        (transaction_dir / _CODE_COMMIT_MARKER_FILENAME).unlink()
    except FileNotFoundError:
        pass
    try:
        payload_dir.rmdir()
        transaction_dir.rmdir()
        backup_root.rmdir()
    except OSError:
        pass


def _commit_staged_plugin_code_sync(
    staged: _StagedPluginCode,
) -> _CommittedPluginCodeCleanup:
    marker_path = staged.staged_dir.parent.parent / _CODE_COMMIT_MARKER_FILENAME
    temporary_path = marker_path.with_name(
        f".{marker_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    marker = {
        "schema_version": 1,
        "transaction_id": staged.transaction_id,
        "original_dir": str(staged.original_dir),
        "staged_dir": str(staged.staged_dir),
    }
    try:
        temporary_path.write_text(json.dumps(marker), encoding="utf-8")
        temporary_path.replace(marker_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return _CommittedPluginCodeCleanup(staged=staged, marker_path=marker_path)


def _load_committed_plugin_code_cleanup_sync(
    transaction_dir: Path,
    *,
    backup_root: Path,
) -> _CommittedPluginCodeCleanup | None:
    if (
        transaction_dir.parent != backup_root
        or _CODE_TRANSACTION_ID_PATTERN.fullmatch(transaction_dir.name) is None
        or _path_has_symlink_ancestor(transaction_dir)
    ):
        return None
    marker_path = transaction_dir / _CODE_COMMIT_MARKER_FILENAME
    try:
        raw = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return None
    transaction_id = raw.get("transaction_id")
    original_dir_raw = raw.get("original_dir")
    staged_dir_raw = raw.get("staged_dir")
    if (
        transaction_id != transaction_dir.name
        or not isinstance(original_dir_raw, str)
        or not isinstance(staged_dir_raw, str)
    ):
        return None
    original_dir = Path(original_dir_raw)
    staged_dir = Path(staged_dir_raw)
    if (
        not original_dir.is_absolute()
        or not staged_dir.is_absolute()
        or original_dir.parent != backup_root.parent
        or staged_dir
        != transaction_dir / _CODE_PAYLOAD_DIR_NAME / original_dir.name
    ):
        return None
    staged = _StagedPluginCode(
        original_dir=original_dir,
        staged_dir=staged_dir,
        transaction_id=transaction_id,
    )
    return _CommittedPluginCodeCleanup(staged=staged, marker_path=marker_path)


def _finalize_committed_plugin_code_sync(
    committed: _CommittedPluginCodeCleanup,
) -> bool:
    """Remove code only from this transaction's own committed staging artifact."""
    staged = committed.staged
    backup_root = staged.original_dir.parent / _CODE_BACKUP_ROOT_NAME
    transaction_dir = backup_root / staged.transaction_id
    if (
        staged.staged_dir
        != transaction_dir / _CODE_PAYLOAD_DIR_NAME / staged.original_dir.name
        or committed.marker_path != transaction_dir / _CODE_COMMIT_MARKER_FILENAME
        or _load_committed_plugin_code_cleanup_sync(
            transaction_dir,
            backup_root=backup_root,
        )
        != committed
    ):
        raise ValueError(
            "uninstall code cleanup marker does not match staged directory"
        )
    # Keep the marker outside the recursive delete target.  ``rmtree`` may
    # partially remove a locked tree before raising; retaining the marker lets
    # startup authenticate and retry the remaining payload safely.
    try:
        shutil.rmtree(staged.staged_dir)
    except FileNotFoundError:
        pass
    try:
        staged.staged_dir.parent.rmdir()
    except FileNotFoundError:
        pass
    committed.marker_path.unlink(missing_ok=True)
    try:
        transaction_dir.rmdir()
    except FileNotFoundError:
        pass
    try:
        backup_root.rmdir()
    except OSError:
        pass
    return True


def retry_deferred_plugin_code_cleanup_sync() -> int:
    """Retry only committed code backups under transaction-owned roots."""
    candidate_roots = {get_user_plugin_exec_root(), *PLUGIN_CONFIG_ROOTS}
    builtin_root = BUILTIN_PLUGIN_CONFIG_ROOT.resolve(strict=False)
    state_root = get_plugin_state_root().resolve(strict=False)
    cleaned = 0
    for candidate_root in candidate_roots:
        root = candidate_root.resolve(strict=False)
        if root in {builtin_root, state_root}:
            continue
        backup_root = root / _CODE_BACKUP_ROOT_NAME
        try:
            transaction_dirs = tuple(backup_root.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            logger.warning(
                "uninstall: failed to inspect deferred code cleanup root {}: {}",
                backup_root,
                exc,
            )
            continue
        for transaction_dir in transaction_dirs:
            committed = _load_committed_plugin_code_cleanup_sync(
                transaction_dir,
                backup_root=backup_root,
            )
            if committed is None:
                logger.error(
                    "uninstall: refusing unmarked deferred code cleanup path: {}",
                    transaction_dir,
                )
                continue
            try:
                if _finalize_committed_plugin_code_sync(committed):
                    cleaned += 1
            except (OSError, ValueError) as exc:
                logger.warning(
                    "uninstall: deferred code cleanup still pending for {}: {}",
                    transaction_dir,
                    exc,
                )
    return cleaned


def _snapshot_runtime_preference(plugin_id: str) -> _RuntimePreferenceSnapshot:
    return _RuntimePreferenceSnapshot(
        entry=get_runtime_override_entry(plugin_id),
    )


def _restore_runtime_preference(
    plugin_id: str, snapshot: _RuntimePreferenceSnapshot
) -> None:
    restored = restore_runtime_override(
        plugin_id,
        snapshot.entry,
        expected_current=None,
    )
    if not restored:
        logger.info(
            "uninstall rollback kept a newer runtime preference plugin_id={}",
            plugin_id,
        )


def _registry_refresh_target(
    *,
    runtime_plugin_id: str,
    source_entry: LockEntry,
    plugin_meta: dict[str, object],
    config_path: Path,
) -> _RegistryRefreshTarget:
    declared_plugin_id = source_entry.plugin_id.strip()
    if not declared_plugin_id:
        declared_plugin_id = (
            PluginDirectoryScanner._load_plugin_id(config_path.parent)
            or runtime_plugin_id
        )

    config_paths = [config_path]
    shadowed_builtin_path = plugin_meta.get("shadowed_builtin_path")
    if isinstance(shadowed_builtin_path, str) and shadowed_builtin_path:
        config_paths.append(Path(shadowed_builtin_path))
    config_paths.append(
        BUILTIN_PLUGIN_CONFIG_ROOT / declared_plugin_id / config_path.name
    )
    return _RegistryRefreshTarget(
        runtime_plugin_id=runtime_plugin_id,
        declared_plugin_id=declared_plugin_id,
        config_paths=tuple(config_paths),
    )


def _registry_failure_matches_target(
    failure: object,
    *,
    target: _RegistryRefreshTarget,
) -> bool:
    if not isinstance(failure, dict):
        return False

    failure_config_path = failure.get("config_path")
    if isinstance(failure_config_path, str) and failure_config_path:
        try:
            resolved_failure_path = Path(failure_config_path).resolve(strict=False)
        except (OSError, RuntimeError):
            resolved_failure_path = Path(failure_config_path)
        for config_path in target.config_paths:
            try:
                resolved_target_path = config_path.resolve(strict=False)
            except (OSError, RuntimeError):
                resolved_target_path = config_path
            if resolved_failure_path == resolved_target_path:
                return True
        return False

    failure_plugin_id = failure.get("plugin_id")
    return isinstance(failure_plugin_id, str) and failure_plugin_id in {
        target.runtime_plugin_id,
        target.declared_plugin_id,
    }


async def _refresh_registry(
    target: _RegistryRefreshTarget,
) -> dict[str, object]:
    # 这里原本要先显式清一次元数据扫描缓存，因为缓存键只看得见插件目录内部、
    # 看不到共享 vendor/ 或装进 site-packages 的包。缓存没有了，刷新每次都重读
    # 盘面，所以直接刷新即可。
    result = await plugin_registry_service.refresh_registry()
    failed = result.get("failed")
    if isinstance(failed, list) and any(
        _registry_failure_matches_target(failure, target=target)
        for failure in failed
    ):
        raise RuntimeError("plugin registry refresh failed for uninstall target")
    return result


async def _restore_original_runtime(
    plugin_id: str, *, was_running: bool
) -> RuntimeRestart:
    if not was_running:
        return "not_needed"
    try:
        if await asyncio.to_thread(_plugin_is_running_sync, plugin_id):
            return "succeeded"
        await _start_plugin(plugin_id)
    except Exception:
        logger.exception(
            "uninstall rollback runtime restart failed plugin_id={}", plugin_id
        )
        return "failed"
    return "succeeded"


async def _rollback_precommit(
    *,
    plugin_id: str,
    manager: InstallSourceManager,
    source_entry: LockEntry,
    source_update_attempted: bool,
    staged_code: _StagedPluginCode | None,
    staged_profile: _StagedPackageProfile | None,
    preference_snapshot: _RuntimePreferenceSnapshot,
    preference_update_attempted: bool,
    was_running: bool,
    stop_attempted: bool,
    registry_target: _RegistryRefreshTarget,
) -> _RollbackOutcome:
    """Compensate in reverse stage order: source, code, profile, preference,
    registry, then the original runtime. Each step is best effort."""
    filesystem_changed = (
        source_update_attempted or staged_code is not None or staged_profile is not None
    )
    filesystem_complete = True
    if source_update_attempted:
        try:
            await asyncio.to_thread(manager.restore_entry_for_rollback, source_entry)
        except Exception:
            filesystem_complete = False
            logger.exception(
                "uninstall rollback failed to restore source entry plugin_id={}",
                plugin_id,
            )
    if staged_code is not None:
        try:
            await asyncio.to_thread(_restore_staged_plugin_code_sync, staged_code)
        except Exception:
            filesystem_complete = False
            logger.exception(
                "uninstall rollback failed to restore code plugin_id={}", plugin_id
            )
    if staged_profile is not None:
        try:
            await asyncio.to_thread(
                _restore_staged_package_profile_sync, staged_profile
            )
        except Exception:
            filesystem_complete = False
            logger.exception(
                "uninstall rollback failed to restore profile plugin_id={}",
                plugin_id,
            )
    preference_restored = True
    if preference_update_attempted:
        try:
            await asyncio.to_thread(
                _restore_runtime_preference, plugin_id, preference_snapshot
            )
        except Exception:
            preference_restored = False
            logger.exception(
                "uninstall rollback failed to restore preference plugin_id={}",
                plugin_id,
            )
    if filesystem_changed:
        try:
            await _refresh_registry(registry_target)
        except Exception:
            # A refresh failure does not undo the physical restores above;
            # the scan itself is not part of the filesystem rollback report.
            logger.exception(
                "uninstall rollback registry refresh failed plugin_id={}",
                plugin_id,
            )
    runtime_restart = await _restore_original_runtime(
        plugin_id,
        was_running=was_running and stop_attempted,
    )
    filesystem_rollback: FilesystemRollback = (
        "not_needed"
        if not filesystem_changed
        else ("completed" if filesystem_complete else "incomplete")
    )
    return _RollbackOutcome(
        filesystem_rollback=filesystem_rollback,
        runtime_restart=runtime_restart,
        preference_restored=preference_restored,
    )


@serialized_plugin_operation
async def uninstall_plugin(plugin_id: str) -> UninstallPluginResult:
    """Uninstall the exact managed user candidate with a pre-commit rollback."""

    stage = "preflight"
    try:
        await asyncio.to_thread(ensure_plugin_exec_state_roots_separated)
    except ValueError as exc:
        if getattr(exc, "code", "") == "PLUGIN_EXEC_STATE_ROOT_COLLISION":
            raise UninstallPluginError(
                code="PLUGIN_EXEC_STATE_ROOT_COLLISION",
                message=str(exc),
                status_code=409,
                stage=stage,
                details={"plugin_id": plugin_id, "error_type": type(exc).__name__},
            ) from exc
        raise

    plugin_meta = await asyncio.to_thread(_get_plugin_meta_sync, plugin_id)
    if plugin_meta is None:
        raise UninstallPluginError(
            code="PLUGIN_NOT_FOUND",
            message=f"Plugin '{plugin_id}' not found",
            status_code=404,
            stage=stage,
            details={"plugin_id": plugin_id, "error_type": "PluginNotFound"},
        )
    config_path = await asyncio.to_thread(
        _resolve_plugin_config_path_sync, plugin_id, plugin_meta
    )
    if config_path is None:
        raise UninstallPluginError(
            code="PLUGIN_CONFIG_NOT_FOUND",
            message=f"Plugin '{plugin_id}' configuration not found",
            status_code=404,
            stage=stage,
            details={"plugin_id": plugin_id, "error_type": "ConfigNotFound"},
        )
    plugin_dir = config_path.parent

    manager = get_install_source_manager()
    stage = "ownership"
    try:
        if manager is not None:
            reload_install_source = getattr(manager, "load", None)
            if callable(reload_install_source):
                await asyncio.to_thread(reload_install_source)
        source_entry = await asyncio.to_thread(
            require_uninstall_ownership,
            manager=manager,
            runtime_plugin_id=plugin_id,
            config_path=config_path,
        )
    except UninstallOwnershipError as exc:
        raise UninstallPluginError(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            stage=stage,
            details={**exc.details, "error_type": type(exc).__name__},
        ) from exc
    assert manager is not None

    registry_target = await asyncio.to_thread(
        _registry_refresh_target,
        runtime_plugin_id=plugin_id,
        source_entry=source_entry,
        plugin_meta=plugin_meta,
        config_path=config_path,
    )

    # The existing path guard stays an independent second check: it must not
    # mask the ownership refusal with a generic forbidden-path error.
    if not await asyncio.to_thread(_path_within_plugin_roots_sync, plugin_dir):
        raise UninstallPluginError(
            code="PLUGIN_DELETE_FORBIDDEN_PATH",
            message=f"Plugin '{plugin_id}' path is outside managed plugin roots",
            status_code=403,
            stage="preflight",
            details={"plugin_id": plugin_id, "error_type": "ForbiddenDeletePath"},
        )

    was_running = await asyncio.to_thread(_plugin_is_running_sync, plugin_id)
    preference_snapshot = await asyncio.to_thread(
        _snapshot_runtime_preference, plugin_id
    )
    stop_attempted = False
    staged_profile: _StagedPackageProfile | None = None
    staged_code: _StagedPluginCode | None = None
    source_update_attempted = False
    preference_update_attempted = False
    autostart_was_pending = False
    autostart_gate_rollback: str | None = None
    try:
        stage = "stop"
        if was_running:
            stop_attempted = True
            await _stop_plugin(plugin_id)

        stage = "stage_profile"
        staged_profile = await asyncio.to_thread(
            _stage_orphaned_package_profile_sync,
            plugin_dir,
            manager=manager,
        )
        stage = "stage_code"
        staged_code = await asyncio.to_thread(_stage_plugin_code_sync, plugin_dir)

        stage = "update_source"
        source_update_attempted = True
        await asyncio.to_thread(manager.mark_removed, directory_path=plugin_dir)

        stage = "refresh_and_preferences"
        await asyncio.to_thread(_remove_runtime_metadata_sync, plugin_id)
        await _refresh_registry(registry_target)
        restored_meta = await asyncio.to_thread(_get_plugin_meta_sync, plugin_id)
        restored_builtin = bool(
            restored_meta and restored_meta.get("effective_source") == "builtin"
        )
        # 卸载之后那条待批准记录一定是过时的：它是为被卸掉的那份代码记的。
        # 留着的话，恢复出来的内置插件（用户原本就在自启）会被它继续拦下来；
        # 插件被整个移除时，它也会挂在这个 id 上等着误伤将来的重装（codex）。
        #
        # 但清之前要记下原状态：这一步之后还有预提交步骤会失败，而
        # _rollback_precommit 会把插件文件和偏好都恢复回去，却不知道批准位被动过。
        # 一个从没被启动过的新插件在那种回滚之后会变成"已批准"，下次开机直接自启
        # （codex）。所以下面的 except 分支要把它放回去。
        autostart_was_pending = not await asyncio.to_thread(
            is_autostart_approved, plugin_id
        )
        if not await asyncio.to_thread(clear_autostart_pending, plugin_id):
            # 清不掉就不能报成功。盘上那条旧记录会继续拦住恢复出来的内置插件，
            # 而且同 id 的重装还会继承它（coderabbit）。抛出去交给既有的预提交
            # 回滚，把卸载恢复回去，比留下一个说不清的状态好。
            raise UninstallPluginError(
                code="PLUGIN_AUTOSTART_APPROVAL_PERSIST_FAILED",
                message=(
                    "uninstall could not clear the plugin's autostart approval "
                    "record; refusing to commit"
                ),
                status_code=500,
                stage=stage,
            )
        preference_action: PreferenceAction = (
            "preserved" if restored_builtin else "cleared"
        )
        if not restored_builtin:
            preference_update_attempted = True
            await asyncio.to_thread(clear_runtime_override, plugin_id)

        committed_code = await asyncio.to_thread(
            _commit_staged_plugin_code_sync,
            staged_code,
        )
    except BaseException as exc:
        rollback = await _rollback_precommit(
            plugin_id=plugin_id,
            manager=manager,
            source_entry=source_entry,
            source_update_attempted=source_update_attempted,
            staged_code=staged_code,
            staged_profile=staged_profile,
            preference_snapshot=preference_snapshot,
            preference_update_attempted=preference_update_attempted,
            was_running=was_running,
            stop_attempted=stop_attempted,
            registry_target=registry_target,
        )
        if autostart_was_pending and await asyncio.to_thread(plugin_dir.exists):
            # 回滚把插件文件放回去了，那条待批准记录也得跟着回去。少了它，一个
            # 用户从没启动过的插件在一次失败的卸载之后变成"已批准"，下次开机自己
            # 跑起来（codex）。和别处同一条判据：看盘不看意图——代码真的没了就
            # 别补记录，否则将来占用这个 id 的插件会被它误伤。
            if not await asyncio.to_thread(mark_autostart_pending, plugin_id):
                # 改抛会把真正的失败原因换掉，所以这里不抛；但也不能只写日志——
                # 补偿失败要跟着回滚结果一起交出去，和 preference_rollback 同一个
                # 形状（coderabbit）。coderabbit 还要求「持久化一条可恢复的阻止
                # 状态」，那一半我没做：能持久化的只有刚刚写失败的那个存储。
                autostart_gate_rollback = "incomplete"
                logger.error(
                    "uninstall rollback restored plugin {} but could not restore "
                    "its pending-approval record; it may autostart without having "
                    "been started by the user",
                    plugin_id,
                )
        gate_details = (
            {} if autostart_gate_rollback is None
            else {"autostart_gate_rollback": autostart_gate_rollback}
        )
        if isinstance(exc, UninstallPluginError):
            exc.details.update(gate_details)
        if not isinstance(exc, Exception) or isinstance(exc, UninstallPluginError):
            raise
        if isinstance(exc, ServerDomainError):
            # Structured domain failures (e.g. stop_plugin) keep their stable
            # public code; compensation outcomes ride along in details.
            raise UninstallPluginError(
                code=exc.code,
                message=exc.message,
                status_code=exc.status_code,
                stage=stage,
                filesystem_rollback=rollback.filesystem_rollback,
                runtime_restart=rollback.runtime_restart,
                details={
                    **exc.details,
                    "error_type": type(exc).__name__,
                    **(
                        {}
                        if rollback.preference_restored
                        else {"preference_rollback": "incomplete"}
                    ),
                    **gate_details,
                },
            ) from exc
        raise UninstallPluginError(
            code="PLUGIN_DELETE_FAILED",
            message=f"Failed to delete plugin '{plugin_id}'",
            status_code=500,
            stage=stage,
            filesystem_rollback=rollback.filesystem_rollback,
            runtime_restart=rollback.runtime_restart,
            details={
                "plugin_id": plugin_id,
                "error_type": type(exc).__name__,
                **(
                    {}
                    if rollback.preference_restored
                    else {"preference_rollback": "incomplete"}
                ),
                **gate_details,
            },
        ) from exc

    # ---- COMMIT: source record, registry facts and preference contract are
    # consistent and the original user path is no longer valid. Failures from
    # here on are reported, never presented as a rolled-back uninstall. ----
    runtime_restart: RuntimeRestart = "not_needed"
    runtime_restart_error: dict[str, str] | None = None
    if was_running and restored_builtin:
        try:
            await _start_plugin(plugin_id)
            runtime_restart = "succeeded"
        except Exception as exc:
            runtime_restart = "failed"
            runtime_restart_error = {
                "code": "PLUGIN_BUILTIN_RESTORE_START_FAILED",
                "message": str(exc),
                "error_type": type(exc).__name__,
            }
            logger.error(
                "uninstall committed but builtin restart failed plugin_id={} err_type={}",
                plugin_id,
                type(exc).__name__,
            )

    cleanup_pending = False
    deleted_profile_dir: Path | None = None
    try:
        await asyncio.to_thread(_finalize_committed_plugin_code_sync, committed_code)
    except Exception:
        cleanup_pending = True
        logger.exception(
            "uninstall committed but code cleanup is pending plugin_id={}", plugin_id
        )
    if staged_profile is not None:
        try:
            deleted_profile_dir = await asyncio.to_thread(
                _finalize_staged_package_profile_sync,
                staged_profile,
            )
        except Exception:
            cleanup_pending = True
            recorded = await asyncio.to_thread(
                _record_deferred_profile_cleanup_sync, staged_profile
            )
            logger.exception(
                "uninstall committed but profile cleanup is pending plugin_id={} persisted={}",
                plugin_id,
                recorded,
            )

    return UninstallPluginResult(
        plugin_id=plugin_id,
        plugin_dir=plugin_dir,
        deleted_from_disk=True,
        deleted_profile_dir=deleted_profile_dir,
        restored_builtin=restored_builtin,
        preference_action=preference_action,
        filesystem_rollback="not_needed",
        runtime_restart=runtime_restart,
        cleanup_pending=cleanup_pending,
        runtime_restart_error=runtime_restart_error,
    )
