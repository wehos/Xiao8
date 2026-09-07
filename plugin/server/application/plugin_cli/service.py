from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
import hashlib
import secrets
import shutil
import stat
import tomllib
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Literal

from plugin.core.plugin_layout import resolve_plugin_layout
from plugin.logging_config import get_logger
from plugin.neko_plugin_cli.core.install import PackageInstaller
from plugin.neko_plugin_cli.core.models import InstalledPlugin, InstallResult
from plugin.neko_plugin_cli.public import (
    analyze_bundle_plugins,
    inspect_package,
    build_bundle,
    build_plugin,
    install_package,
)
from plugin.server.application.install_source import (
    InstallSourceError,
    InstallSourceManager,
    LockEntry,
    classify_plugin_path,
    get_install_source_manager,
)
from plugin.server.infrastructure.autostart_approvals import (
    clear_autostart_pending,
    is_autostart_approved,
    mark_autostart_pending,
)
from plugin.server.application.plugin_cli.paths import PluginCliPathPolicy
from plugin.server.application.plugin_cli.install_plan import (
    REPLACEMENT_ACTIONS,
    PluginInstallPlan,
    build_install_plan,
    is_manifestless_state_directory,
)
from plugin.server.application.plugins.installation_transactions import (
    replace as replacement_transaction,
)
from plugin.server.application.plugins import source_switch
from plugin.server.application.plugins.registry_service import PluginRegistryService
from plugin.server.application.plugins.installation_transactions.manual_takeover import (
    is_manual_takeover_entry,
    local_manual_takeover_confirmation_token,
    manual_takeover_snapshot_sha256,
)
from plugin.server.application.plugins.source_switch import (
    SourceSwitchRequest,
    switch_builtin_source,
)
from plugin.server.application.plugins.operation_lock import serialized_plugin_operation
from plugin.server.application.plugin_cli.source_resolver import (
    PluginSourceResolver,
    ResolvedPluginSource,
)
from plugin.server.domain.errors import ServerDomainError
from plugin.settings import (
    BUILTIN_PLUGIN_CONFIG_ROOT,
    USER_PACKAGE_PROFILES_ROOT,
    USER_PLUGIN_CONFIG_ROOT,
    USER_PLUGIN_PACKAGES_ROOT,
)

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
# Deprecated compatibility anchors. Package-management code below resolves
# roots through PluginCliPathPolicy.from_settings() for each operation.
_RUNTIME_PLUGINS_ROOT = BUILTIN_PLUGIN_CONFIG_ROOT
_INSTALL_PLUGINS_ROOT = USER_PLUGIN_CONFIG_ROOT
_INSTALL_PROFILES_ROOT = USER_PACKAGE_PROFILES_ROOT
_TARGET_ROOT = USER_PLUGIN_PACKAGES_ROOT

# Allowed extensions for uploaded plugin packages
_ALLOWED_UPLOAD_SUFFIXES = frozenset({".neko-plugin", ".neko-bundle"})
# Maximum upload size (500 MiB)
_UPLOAD_MAX_BYTES = 500 * 1024 * 1024
_UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024

logger = get_logger("server.application.plugin_cli")
plugin_registry_service = PluginRegistryService()


async def _refresh_committed_market_install(plugin_id: str) -> str | None:
    """Refresh a fresh Market install without rolling back committed files.

    The source row is the commit point. Cancellation after it must wait for the
    one refresh already in flight, then propagate to the caller; refresh failure
    is surfaced as a warning while the committed installation remains intact.
    """

    try:
        # upload_and_install is wrapped by serialized_plugin_operation, which
        # shields the complete locked operation and waits for it before
        # propagating caller cancellation. Await directly here so no orphan
        # refresh task is created.
        await plugin_registry_service.refresh_plugin(plugin_id, force=True)
    except Exception as exc:  # noqa: BLE001 - committed install stays successful.
        logger.warning(
            "post-commit Market plugin refresh failed: plugin_id={}",
            plugin_id,
            exc_info=True,
        )
        return (
            f"plugin '{plugin_id}' was installed, but its registry refresh failed "
            f"({type(exc).__name__}: {exc}); refresh plugins or restart N.E.K.O"
        )
    return None

_PACKAGE_ERROR_PATTERNS = (
    (
        "PLUGIN_PACKAGE_NESTED_ROOT",
        (("extra parent folder",), ("manifest.toml is nested",)),
    ),
    (
        "PLUGIN_PACKAGE_MANIFEST_MISSING",
        (
            ("required file 'manifest.toml' not found",),
            ("package manifest.toml is missing",),
        ),
    ),
    (
        "PLUGIN_PACKAGE_PLUGIN_MANIFEST_MISSING",
        (("missing the required 'plugin.toml'",),),
    ),
    (
        "PLUGIN_PACKAGE_PLUGIN_MANIFEST_INVALID",
        (("plugin.toml", "invalid toml"),),
    ),
    (
        "PLUGIN_PACKAGE_IDENTITY_MISMATCH",
        (("does not match plugin.toml id",), ("plugin identity mismatch",)),
    ),
    (
        "PLUGIN_PACKAGE_HASH_MISMATCH",
        (("payload hash mismatch",), ("content verification hash",)),
    ),
)


def _is_link_or_reparse(path: Path) -> bool:
    """Return whether ``path`` is a symlink or Windows reparse point."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(file_attributes & reparse_attribute)


def _validate_existing_profile_ownership(
    *,
    profile_dir: Path,
    profiles_root: Path,
    package_id: str,
    plugin_ids: set[str],
) -> None:
    """Fail closed unless install-source history owns an existing profile.

    A profile directory name is selected by the package manifest and is not
    proof that the directory belongs to the incoming package. Removed entries
    remain in the source ledger, so a legitimate reinstall can reuse its
    retained profile without letting an unrelated package claim orphaned state.
    """

    manager = get_install_source_manager()
    if manager is None:
        raise ServerDomainError(
            code="INSTALL_SOURCE_NOT_READY",
            message="install source manager is not initialised",
            status_code=503,
            details={"hint": "wait for FastAPI lifespan startup to complete"},
        )
    owners = []
    resolved_profile = profile_dir.resolve(strict=False)
    for entry in manager.list_entries(include_removed=True):
        # Only an explicit modern ownership record is proof. ``None`` is
        # a legacy row whose profile ownership was never recorded.
        if entry.profile_installed is not True:
            continue
        recorded_key = entry.package_id
        if entry.profile_dir:
            recorded_profile = Path(entry.profile_dir).expanduser()
        elif recorded_key:
            recorded_profile = profiles_root / recorded_key
        else:
            continue
        if recorded_profile.resolve(strict=False) == resolved_profile:
            owners.append(entry)

    ownership_matches = bool(owners) and all(
        owner.plugin_id in plugin_ids and owner.package_id == package_id
        for owner in owners
    )
    if ownership_matches:
        return

    raise ServerDomainError(
        code="PLUGIN_PACKAGE_PROFILE_OWNERSHIP_CONFLICT",
        message="existing package profile ownership does not match the incoming package",
        status_code=409,
        details={
            "package_id": package_id,
            "plugin_ids": sorted(plugin_ids),
            "recorded_plugin_ids": sorted(
                {owner.plugin_id for owner in owners if owner.plugin_id}
            ),
        },
    )


def _classify_package_error(exc: Exception) -> str | None:
    if isinstance(exc, ServerDomainError) and exc.code.startswith("PLUGIN_PACKAGE_"):
        return exc.code
    if isinstance(exc, zipfile.BadZipFile):
        return "PLUGIN_PACKAGE_INVALID_ARCHIVE"

    message = str(exc).lower()
    for code, alternatives in _PACKAGE_ERROR_PATTERNS:
        if any(
            all(fragment in message for fragment in fragments)
            for fragments in alternatives
        ):
            return code
    if any(
        fragment in message
        for fragment in (
            "too many entries",
            "package archive expands to",
            "single-member limit",
            "compression ratio",
            "-byte read limit",
            "equivalent on common filesystems",
            "file/directory path conflict",
        )
    ):
        return "PLUGIN_PACKAGE_INVALID_ARCHIVE"
    return None


def _replacement_error_details(
    exc: replacement_transaction.ReplacePluginError,
) -> dict[str, object]:
    details: dict[str, object] = {
        "stage": exc.stage,
        "rollback_status": exc.rollback_status,
    }
    cause_code = _classify_package_error(exc.cause)
    if cause_code:
        details["cause_code"] = cause_code
    return details


@dataclass(frozen=True, slots=True)
class _StagedBuiltinOverride:
    result: InstallResult
    plugin_dir: Path
    profile_dir: Path | None


def _require_within(path: Path, root: Path, *, field: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{field} must be inside {root}") from exc
    return resolved


def _require_safe_directory_name(value: str, *, field: str) -> str:
    directory_name = value.strip()
    if (
        not directory_name
        or directory_name in {".", ".."}
        or "/" in directory_name
        or "\\" in directory_name
    ):
        raise ValueError(f"{field} must be a safe plugin directory name, got {value!r}")
    return directory_name


class PluginCliService:
    async def list_local_plugins(self) -> dict[str, object]:
        return await asyncio.to_thread(self._list_local_plugins_sync)

    async def list_local_packages(self) -> dict[str, object]:
        return await asyncio.to_thread(self._list_local_packages_sync)

    async def build(
        self,
        *,
        mode: str = "selected",
        plugin: str | None = None,
        plugins: list[str] | None = None,
        plugin_ref: dict[str, Any] | None = None,
        plugin_refs: list[dict[str, Any]] | None = None,
        out: str | None = None,
        target_dir: str | None = None,
        keep_staging: bool = False,
        bundle_id: str | None = None,
        package_name: str | None = None,
        package_description: str | None = None,
        version: str | None = None,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._build_sync,
            mode=mode,
            plugin=plugin,
            plugins=plugins,
            plugin_ref=plugin_ref,
            plugin_refs=plugin_refs,
            out=out,
            target_dir=target_dir,
            keep_staging=keep_staging,
            bundle_id=bundle_id,
            package_name=package_name,
            package_description=package_description,
            version=version,
        )

    async def inspect(self, *, package: str) -> dict[str, object]:
        return await asyncio.to_thread(self._inspect_sync, package=package)

    async def verify(self, *, package: str) -> dict[str, object]:
        return await asyncio.to_thread(self._verify_sync, package=package)

    async def plan_install(
        self,
        *,
        package: str,
        plugins_root: str | None = None,
        profiles_root: str | None = None,
        _allow_external_profiles_root: bool = False,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._plan_install_sync,
            package=package,
            plugins_root=plugins_root,
            profiles_root=profiles_root,
            _allow_external_profiles_root=_allow_external_profiles_root,
        )

    @serialized_plugin_operation
    async def install(
        self,
        *,
        package: str,
        plugins_root: str | None = None,
        profiles_root: str | None = None,
        on_conflict: str = "fail",
        use_staging: bool = True,
        forced_directory_name: str | None = None,
        install_source: Literal["imported"] | None = None,
        confirm_upgrade: bool = False,
        confirmation_token: str | None = None,
        _allow_external_profiles_root: bool = False,
    ) -> dict[str, object]:
        install_source_manager = get_install_source_manager()
        reload_install_source = getattr(install_source_manager, "load", None)
        if callable(reload_install_source):
            await asyncio.to_thread(reload_install_source)
        plan_dict = await self.plan_install(
            package=package,
            plugins_root=plugins_root,
            profiles_root=profiles_root,
            _allow_external_profiles_root=_allow_external_profiles_root,
        )
        action = str(plan_dict["action"])
        if action == "blocked":
            raise ServerDomainError(
                code="PLUGIN_INSTALL_BLOCKED",
                message="plugin package cannot be installed safely",
                status_code=409,
                details=plan_dict,
            )
        if action == "install":
            # 登记排在提升之前，和覆盖安装那条路同一个形状。装完再登记的话，写盘
            # 失败时插件已经在盘上了，只能报个 warning——而下次启动会把"没有待批准
            # 记录"当成已批准，第三方代码就在用户首次启动之前跑起来了（coderabbit
            # / greptile）。plan_dict 的 plugin_id 读自包内 manifest，提升之前就有。
            # bundle 的 plan.plugin_id 是**包** id，而注册表按包内每个插件自己的
            # manifest id 记批准状态——只登记包 id 等于对整组一个都没拦住
            # （coderabbit）。所以 bundle 用 bundle_plugin_ids，单插件用 plugin_id。
            bundle_ids = [
                str(item or "").strip()
                for item in (plan_dict.get("bundle_plugin_ids") or ())
                if str(item or "").strip()
            ]
            gate_plugin_ids = bundle_ids or [
                pid for pid in (str(plan_dict.get("plugin_id") or "").strip(),) if pid
            ]
            gate_restore: list[str] = []
            for gate_plugin_id in gate_plugin_ids:
                if await asyncio.to_thread(is_autostart_approved, gate_plugin_id):
                    gate_restore.append(gate_plugin_id)
                if not await asyncio.to_thread(mark_autostart_pending, gate_plugin_id):
                    # 拒绝之前先把这一轮已经登记的还原掉，别留半截状态。
                    for done in gate_restore:
                        if not await asyncio.to_thread(clear_autostart_pending, done):
                            logger.error(
                                "could not restore the autostart approval for "
                                "plugin_id={} while refusing the install; it must "
                                "be started once by hand",
                                done,
                            )
                    raise ServerDomainError(
                        code="PLUGIN_AUTOSTART_GATE_UNAVAILABLE",
                        message=(
                            "cannot record the plugin as awaiting approval; "
                            "refusing to install code that would autostart "
                            "unapproved"
                        ),
                        status_code=500,
                        details={"plugin_id": gate_plugin_id},
                    )
            try:
                result = await asyncio.to_thread(
                    self._install_sync,
                    package=package,
                    plugins_root=plugins_root,
                    profiles_root=profiles_root,
                    on_conflict=on_conflict,
                    use_staging=use_staging,
                    forced_directory_name=forced_directory_name,
                    _allow_external_profiles_root=_allow_external_profiles_root,
                )
            except BaseException:
                # 安装没成，把批准状态原样放回去——否则一次失败的安装会给这些 id
                # 留下待批准记录，将来同 id 的插件会被它误伤。
                gate_root = await asyncio.to_thread(
                    self._autostart_gate_root, plugins_root
                )
                for gate_plugin_id in gate_restore:
                    # 只为真的没留在盘上的那些还原。_install_via_staging_sync 用
                    # rmtree(ignore_errors=True) 收尾，占用/权限/坏盘都可能留下一份
                    # 可执行的残骸；无条件还原批准等于放它下次开机自己跑起来
                    # （codex）。和覆盖回滚同一条判据：看盘不看意图。
                    if gate_root is None or await asyncio.to_thread(
                        _plugin_directory_exists, gate_root, gate_plugin_id
                    ):
                        logger.error(
                            "install rollback could not clear plugin_id={} of "
                            "leftovers; keeping it gated so leftover code cannot "
                            "autostart",
                            gate_plugin_id,
                        )
                        continue
                    if not await asyncio.to_thread(
                        clear_autostart_pending, gate_plugin_id
                    ):
                        # 和覆盖回滚同一个判断：这里正在处理另一个异常，改抛会把
                        # 真正的失败原因换掉。记一笔，后果有界——这个 id 上留了一条
                        # 待批准记录，将来占用它的插件第一次要手动启动一次。
                        logger.error(
                            "install rollback could not restore the autostart "
                            "approval for plugin_id={}; whatever later takes that "
                            "id must be started once by hand",
                            gate_plugin_id,
                        )
                raise
            # 挂在 install() 的成功出口上，不是挂在某一条来源登记路径上。
            # 上传安装（upload_and_install）自己登记来源、不走
            # _record_requested_install_source，把钩子放在那里等于对上传来的
            # 插件完全不生效——而那正是最需要这道闸的一条路（greptile）。
            #
            # 再按安装结果里的 manifest id 登记一次。上面用的是 plan 的 id，而
            # 目录名和 [plugin].id 允许不一致；两次都是幂等的。这一次已经在盘上了，
            # 拒绝不了，所以失败时挂 warning 而不是抛。
            unrecorded = await asyncio.to_thread(
                _mark_new_install_awaiting_autostart, result
            )
            if unrecorded:
                result["autostart_gate_warning"] = (
                    "could not record as awaiting approval: "
                    + ", ".join(sorted(unrecorded))
                )
            return await self._record_requested_install_source(
                install_result=result,
                package=package,
                source=install_source,
            )

        if action == "override_builtin":
            raise ServerDomainError(
                code="PLUGIN_BUILTIN_OVERRIDE_MARKET_REQUIRED",
                message="builtin plugins can only be overridden by a SHA256-verified Market package",
                status_code=409,
                details=plan_dict,
            )

        if not confirm_upgrade or not confirmation_token:
            raise ServerDomainError(
                code="PLUGIN_UPGRADE_CONFIRMATION_REQUIRED",
                message="plugin replacement requires explicit confirmation",
                status_code=409,
                details=plan_dict,
            )
        if confirmation_token != str(plan_dict["confirmation_token"]):
            raise ServerDomainError(
                code="PLUGIN_UPGRADE_PLAN_CHANGED",
                message="installed plugin changed after replacement confirmation",
                status_code=409,
                details=plan_dict,
            )

        policy = self._path_policy()
        target_root = (
            _require_within(
                Path(plugins_root).expanduser().resolve(),
                policy.user_plugins_root,
                field="plugins_root",
            )
            if plugins_root
            else policy.user_plugins_root
        )
        directory_name = _require_safe_directory_name(
            str(plan_dict["directory_name"]),
            field="directory_name",
        )
        target_dir = target_root / directory_name
        profiles_root_path = (
            Path(profiles_root).expanduser().resolve()
            if profiles_root and _allow_external_profiles_root
            else (
                _require_within(
                    Path(profiles_root).expanduser().resolve(),
                    policy.package_profiles_root,
                    field="profiles_root",
                )
                if profiles_root
                else policy.package_profiles_root
            )
        )
        _require_safe_directory_name(
            str(plan_dict["package_id"]),
            field="package_id",
        )
        installed_package_id = _require_safe_directory_name(
            str(plan_dict["installed_package_id"] or plan_dict["package_id"]),
            field="installed_package_id",
        )
        profile_dir = profiles_root_path / installed_package_id
        package_path = self._resolve_package_path(package)
        plan = self._apply_installed_package_identity(
            build_install_plan(
                package_path=package_path,
                plugins_root=target_root,
                builtin_plugins_root=policy.builtin_plugins_root,
            ),
            package_path=package_path,
            target_root=target_root,
            profiles_root=profiles_root_path,
        )
        if (
            plan.action not in REPLACEMENT_ACTIONS
            or plan.confirmation_token != confirmation_token
        ):
            raise ServerDomainError(
                code="PLUGIN_UPGRADE_PLAN_CHANGED",
                message="installed plugin changed after replacement confirmation",
                status_code=409,
                details=asdict(plan),
            )

        # 目标目录里只有 config/data/cache、连 plugin.toml 都没有时，install_plan
        # 给的是 reinstall——而这条替换出口按定义不登记待批准。可那里从来没装过
        # 插件代码：这一次是**全新**把可执行代码放进去，默认 enabled/auto_start
        # 会让它在下次开机自己跑起来，用户一次都没启动过（codex）。所以按全新安装
        # 处理，闸设在提升之前。
        manifestless_gate_restore = False
        if plan.manifestless_state:
            manifestless_gate_restore = await asyncio.to_thread(
                is_autostart_approved, plan.plugin_id
            )
            if not await asyncio.to_thread(mark_autostart_pending, plan.plugin_id):
                raise ServerDomainError(
                    code="PLUGIN_AUTOSTART_GATE_UNAVAILABLE",
                    message=(
                        "cannot record the plugin as awaiting approval; refusing "
                        "to install code that would autostart unapproved"
                    ),
                    status_code=500,
                    details={"plugin_id": plan.plugin_id},
                )

        manual_manager: InstallSourceManager | None = None
        manual_entry: LockEntry | None = None
        expected_manual_snapshot = ""
        manual_package_has_profiles = False
        if plan.reason == "manual_takeover":
            manual_manager = self._require_install_source_manager()
            manual_entry = manual_manager.entry_for_directory(target_dir)
            if not is_manual_takeover_entry(manual_entry):
                raise ServerDomainError(
                    code="PLUGIN_UPGRADE_PLAN_CHANGED",
                    message="manual plugin ownership changed after replacement confirmation",
                    status_code=409,
                    details=asdict(plan),
                )
            expected_manual_snapshot = await asyncio.to_thread(
                manual_takeover_snapshot_sha256,
                entry=manual_entry,
                target_dir=target_dir,
            )
            rebound_token = await asyncio.to_thread(
                local_manual_takeover_confirmation_token,
                package_path=package_path,
                target_dir=target_dir,
                entry=manual_entry,
                snapshot_sha256=expected_manual_snapshot,
            )
            if not secrets.compare_digest(confirmation_token, rebound_token):
                raise ServerDomainError(
                    code="PLUGIN_UPGRADE_PLAN_CHANGED",
                    message="manual plugin changed after replacement confirmation",
                    status_code=409,
                    details=asdict(plan),
                )
            inspected = await asyncio.to_thread(inspect_package, package_path)
            manual_package_has_profiles = bool(getattr(inspected, "profile_names", ()))
            if manual_package_has_profiles and (
                profile_dir.exists() or profile_dir.is_symlink()
            ):
                raise ServerDomainError(
                    code="PLUGIN_PACKAGE_PROFILE_OWNERSHIP_CONFLICT",
                    message=(
                        "manual takeover cannot claim an existing package profile"
                    ),
                    status_code=409,
                    details={
                        "package_id": plan.package_id,
                        "plugin_id": plan.plugin_id,
                    },
                )

        async def validate_manifestless_backup(backup_dir: Path) -> None:
            if not await asyncio.to_thread(is_manifestless_state_directory, backup_dir):
                raise ValueError("manifest-less plugin state changed before installation")

        async def validate_manual_takeover_backup(backup_dir: Path) -> None:
            assert manual_entry is not None
            staged_snapshot = await asyncio.to_thread(
                manual_takeover_snapshot_sha256,
                entry=manual_entry,
                target_dir=backup_dir,
            )
            if not secrets.compare_digest(
                expected_manual_snapshot,
                staged_snapshot,
            ):
                raise ServerDomainError(
                    code="PLUGIN_UPGRADE_PLAN_CHANGED",
                    message="manual plugin changed while it was being stopped",
                    status_code=409,
                    details=asdict(plan),
                )

        source_write_attempted = False

        async def install_new() -> dict[str, object]:
            nonlocal source_write_attempted
            install_result = await asyncio.to_thread(
                self._install_sync,
                package=package,
                plugins_root=plugins_root,
                profiles_root=profiles_root,
                on_conflict="fail",
                use_staging=use_staging,
                forced_directory_name=forced_directory_name,
                _allow_external_profiles_root=_allow_external_profiles_root,
            )
            if manual_manager is not None:
                source_write_attempted = True
                await self._record_manual_takeover_source(
                    manager=manual_manager,
                    install_result=install_result,
                    package_path=package_path,
                )
            return install_result

        try:
            result = await replacement_transaction.replace_plugin(
                layout=resolve_plugin_layout(plan.plugin_id, target_dir),
                install_new=install_new,
                additional_targets=(
                    (profile_dir,)
                    if manual_manager is None or manual_package_has_profiles
                    else ()
                ),
                preserve_targets=(
                    ()
                    if manual_manager is not None
                    else (
                        (target_dir, profile_dir)
                        if plan.manifestless_state
                        else (profile_dir,)
                    )
                ),
                initialize_runtime_config=not plan.manifestless_state,
                validate_backup=(
                    validate_manual_takeover_backup
                    if manual_manager is not None
                    else validate_manifestless_backup
                    if plan.manifestless_state
                    else None
                ),
            )
        except replacement_transaction.ReplacePluginError as exc:
            if manifestless_gate_restore and not await asyncio.to_thread(
                (target_dir / "plugin.toml").exists
            ):
                # 和别处同一条判据：看盘不看意图。manifest 还在说明代码留在盘上了，
                # 那就保持拦截；真的回滚干净了才把批准位还回去。
                if not await asyncio.to_thread(
                    clear_autostart_pending, plan.plugin_id
                ):
                    logger.error(
                        "manifestless replacement rollback could not restore the "
                        "autostart approval for plugin_id={}; it must be started "
                        "once by hand",
                        plan.plugin_id,
                    )
            source_restored = True
            if manual_entry is not None and source_write_attempted:
                try:
                    assert manual_manager is not None
                    await asyncio.to_thread(
                        manual_manager.restore_entry_for_rollback,
                        manual_entry,
                    )
                except Exception as restore_exc:
                    source_restored = False
                    logger.error(
                        "manual takeover source rollback failed plugin_id={} err_type={}",
                        plan.plugin_id,
                        type(restore_exc).__name__,
                    )
            details = _replacement_error_details(exc)
            if not source_restored:
                details["rollback_status"] = "incomplete"
                details["source_rollback"] = "incomplete"
            raise ServerDomainError(
                code="PLUGIN_UPGRADE_ROLLED_BACK",
                message="plugin upgrade failed and rollback was attempted",
                status_code=500,
                details=details,
            ) from exc

        # 这条出口是替换/升级/接管，按定义不是全新安装，不登记待批准——升级把一个
        # 用户早就在用的插件的自启动资格收走，是把回归包装成安全特性。
        response = {
            **result.install_result,
            # Compatibility response for the existing Package Manager UI.
            # The shared file transaction itself is version-agnostic replace.
            "operation": plan.action,
            "restarted": result.restarted,
            "rollback_status": result.rollback_status,
        }
        if manual_manager is not None:
            return response
        return await self._record_requested_install_source(
            install_result=response,
            package=package,
            source=install_source,
        )

    @serialized_plugin_operation
    async def install_builtin_override(
        self,
        *,
        package: str,
        market_override: dict[str, Any],
    ) -> dict[str, object]:
        """Install one verified Market package as the effective user source."""

        policy = self._path_policy()
        policy.ensure_writable_layout()
        manager = self._require_install_source_manager()
        if manager.is_degraded:
            raise ServerDomainError(
                code="INSTALL_SOURCE_READ_ONLY",
                message="builtin override requires a writable install-source lock",
                status_code=503,
                details={"reason": manager.degrade_reason or "read_only_degrade"},
            )
        package_path = self._resolve_package_path(package)
        detail = dict(market_override.get("market_detail") or {})
        if market_override.get("channel") != "market" or market_override.get("mode") != "override_builtin":
            raise ValueError("builtin override requires Market source metadata")
        expected_plugin_id = str(detail.get("expected_plugin_toml_id") or "").strip()
        expected_sha256 = str(detail.get("package_sha256") or "").strip().lower()
        actual_sha256 = await asyncio.to_thread(self._sha256_file, package_path)
        if len(expected_sha256) != 64 or actual_sha256 != expected_sha256:
            raise ValueError("builtin override Market SHA256 does not match the saved package")

        plan_dict = await self.plan_install(package=str(package_path))
        if plan_dict.get("action") != "override_builtin":
            raise ServerDomainError(
                code="PLUGIN_BUILTIN_OVERRIDE_BLOCKED",
                message="builtin override plan is no longer valid",
                status_code=409,
                details=plan_dict,
            )
        plan = self._apply_installed_package_identity(
            build_install_plan(
                package_path=package_path,
                plugins_root=policy.user_plugins_root,
                builtin_plugins_root=policy.builtin_plugins_root,
            ),
            package_path=package_path,
            target_root=policy.user_plugins_root,
            profiles_root=policy.package_profiles_root,
        )
        if not expected_plugin_id or expected_plugin_id != plan.plugin_id:
            raise ValueError("Market plugin identity does not match the builtin override plan")
        expected_version = str(detail.get("version") or "").strip()
        if not expected_version or expected_version != plan.target_version:
            raise ValueError("Market plugin version does not match the builtin override package")
        confirmation = dict(market_override.get("override_confirmation") or {})
        expected_builtin_manifest_sha256 = str(
            confirmation.get("builtin_manifest_sha256") or ""
        ).strip().lower()
        builtin_manifest = policy.builtin_plugins_root / expected_plugin_id / "plugin.toml"
        try:
            actual_builtin_manifest_sha256 = hashlib.sha256(
                builtin_manifest.read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise ServerDomainError(
                code="OVERRIDE_CONFIRMATION_CHANGED",
                message="builtin override source changed after confirmation",
                status_code=409,
            ) from exc
        if (
            len(expected_builtin_manifest_sha256) != 64
            or not secrets.compare_digest(
                expected_builtin_manifest_sha256,
                actual_builtin_manifest_sha256,
            )
        ):
            raise ServerDomainError(
                code="OVERRIDE_CONFIRMATION_CHANGED",
                message="builtin override source changed after confirmation",
                status_code=409,
            )
        detail.pop("expected_plugin_toml_id", None)
        detail["package_sha256"] = actual_sha256

        staged = await asyncio.to_thread(
            self._stage_builtin_override_sync,
            package=package_path,
            plugins_root=policy.user_plugins_root,
            profiles_root=policy.package_profiles_root,
            plan=plan,
        )
        target_dir = policy.user_plugins_root / plan.directory_name
        target_profile_dir = (
            policy.package_profiles_root / plan.package_id
            if staged.profile_dir is not None
            else None
        )
        original_lock_entry = manager.entry_for_directory(target_dir, include_removed=True)
        lock_warnings: list[str] = []

        async def rebuild_plan() -> dict[str, object]:
            return await self.plan_install(package=str(package_path))

        async def read_lock_snapshot() -> object:
            return original_lock_entry

        async def commit_lock() -> object:
            entry, warnings = await asyncio.to_thread(
                manager.record_market_install,
                root_id="user",
                directory_name=plan.directory_name,
                plugin_id=plan.plugin_id,
                market_detail=detail,
                package_id=plan.package_id,
                profile_dir=str(target_profile_dir) if target_profile_dir is not None else "",
            )
            lock_warnings.extend(warnings)
            return entry

        async def restore_lock(_snapshot: object) -> None:
            # The transaction calls clear_user_source immediately afterwards;
            # that callback restores the exact old row or soft-removes the row
            # created by this attempt.
            return None

        async def clear_user_source() -> None:
            if original_lock_entry is not None:
                await asyncio.to_thread(manager.restore_entry_for_rollback, original_lock_entry)
            else:
                await asyncio.to_thread(
                    manager.mark_removed,
                    directory_path=target_dir,
                    reason="override_rollback",
                )

        async def refresh_registry() -> object:
            from plugin.server.application.plugins.lifecycle_service import plugin_registry_service

            # 换源/回滚之后这一次刷新会重读选中源的 manifest 和 plugin.meta.json。
            # 曾经要先清一次扫描缓存（缓存键只看插件目录内容，换源它看不见），
            # 那个缓存已经不存在了。
            return await plugin_registry_service.refresh_registry()

        async def validate_promoted_source() -> None:
            from plugin.server.application.plugins.lifecycle_service import plugin_registry_service

            await plugin_registry_service.validate_plugin_runtime_source(
                plugin_id=plan.plugin_id,
                config_path=target_dir / "plugin.toml",
            )

        # 登记必须排在切换**之前**。switch_builtin_source 在返回前就完成了提升、
        # 注册并可能启动第三方替换代码；登记放在它返回之后的话，进程若在这中间退出，
        # 那份代码已经跑过，而且下次开机会继承原插件的自启动资格
        # （greptile / coderabbit 各自独立指到这一处）。
        #
        # 代价是失败路径要还原：切换回滚到内置插件之后，这条待批准记录会把一个用户
        # 本来就在自启的内置插件拦下来。所以先记下原状态，失败时原样放回去。
        override_was_approved = await asyncio.to_thread(
            is_autostart_approved, plan.plugin_id
        )
        if not await asyncio.to_thread(mark_autostart_pending, plan.plugin_id):
            # 登记没落盘就不能往下走。切换会把第三方代码提升成有效源并可能启动它，
            # 而没有待批准记录的话它下次开机就自启——用户从没批准过（coderabbit）。
            # 这一步排在切换之前，所以拒绝是干净的：什么都还没动——除了暂存目录，
            # _stage_builtin_override_sync 已经把整个包解开并改名放进去了。清理挂在
            # 下面那个 try 的 finally 上，而这里 raise 在它之前，于是每次登记失败都
            # 留下一份完整的孤儿包，反复重试会越堆越大（codex）。
            await asyncio.to_thread(
                self._cleanup_builtin_override_staging_sync, staged
            )
            raise ServerDomainError(
                code="PLUGIN_AUTOSTART_GATE_UNAVAILABLE",
                message=(
                    "cannot record the override as awaiting approval; refusing to "
                    "promote third-party code that would autostart unapproved"
                ),
                status_code=500,
                details={"plugin_id": plan.plugin_id},
            )
        try:
            switched = await switch_builtin_source(
                SourceSwitchRequest(
                    plugin_id=plan.plugin_id,
                    staged_plugin_dir=staged.plugin_dir,
                    target_plugin_dir=target_dir,
                    confirmation_token=plan.confirmation_token,
                    staged_profile_dir=staged.profile_dir,
                    target_profile_dir=target_profile_dir,
                ),
                rebuild_plan=rebuild_plan,
                read_lock_snapshot=read_lock_snapshot,
                commit_lock=commit_lock,
                restore_lock=restore_lock,
                clear_user_source=clear_user_source,
                refresh_registry=refresh_registry,
                validate_promoted_source=validate_promoted_source,
                is_running=source_switch.plugin_is_running_for_source_switch,
                stop=source_switch.stop_plugin_for_source_switch,
                start=source_switch.start_plugin_for_source_switch,
            )
        except BaseException:
            # 只有覆盖真的没留在盘上时才恢复批准。回滚可能删不掉用户目录（占用、
            # 权限、坏盘），那种情况下第三方源还在，恢复批准等于让它在下次开机
            # 直接跑起来（greptile）。所以看盘不看意图：目录还在就保持拦截。
            override_removed = not await asyncio.to_thread(target_dir.exists)
            if override_was_approved and override_removed:
                if not await asyncio.to_thread(
                    clear_autostart_pending, plan.plugin_id
                ):
                    # 只能记一笔。这里已经在处理另一个异常，改抛"批准还原失败"会
                    # 把真正的失败原因换掉，而那才是用户要看的东西（greptile 建议
                    # 传播，我不采纳这一半）。后果有界且不涉安全：恢复出来的内置
                    # 插件这一轮不自启，用户手动启动一次就会重试这次写入。
                    logger.error(
                        "override rollback could not restore the autostart "
                        "approval for plugin_id={}; the restored builtin will not "
                        "autostart until it is started once by hand",
                        plan.plugin_id,
                    )
            raise
        finally:
            await asyncio.to_thread(self._cleanup_builtin_override_staging_sync, staged)

        staged_result = staged.result.model_dump(mode="json")
        staged_result.update(
            {
                "plugins_root": str(policy.user_plugins_root),
                "profiles_root": str(policy.package_profiles_root),
                "installed_plugins": [
                    {
                        "source_folder": plan.plugin_id,
                        "target_plugin_id": plan.plugin_id,
                        "target_dir": str(target_dir),
                        "renamed": False,
                    }
                ],
                "profile_dir": str(target_profile_dir) if target_profile_dir is not None else None,
                "operation": "override_builtin",
                "restarted": switched.restarted,
                "rollback_status": "not_needed",
                "previous_version": plan.current_version,
                "install_source_warning": "; ".join(lock_warnings) if lock_warnings else None,
            }
        )
        return staged_result

    async def _install_market_builtin_replacement(
        self,
        *,
        package: str,
        profiles_root: str | None,
        _allow_external_profiles_root: bool,
        forced_directory_name: str,
        market_detail: dict[str, Any],
        actual_sha256: str,
        manual_takeover_snapshot_sha256: str = "",
    ) -> dict[str, object]:
        """Restore a verified Market override while replace owns its directory.

        A Market upgrade temporarily moves the current user directory aside.
        During that window the normal install plan sees only the builtin copy
        and correctly classifies the package as a new builtin override. This
        narrow path accepts that transient plan only when the active lock is
        either the existing Market owner or the exact manual owner already
        bound to server-verified takeover evidence.
        """

        expected_sha256 = str(market_detail.get("package_sha256") or "").strip().lower()
        if len(expected_sha256) != 64 or expected_sha256 != actual_sha256:
            raise ValueError("Market replacement SHA256 does not match the saved package")

        plan_dict = await self.plan_install(
            package=package,
            profiles_root=profiles_root,
            _allow_external_profiles_root=_allow_external_profiles_root,
        )
        if plan_dict.get("action") != "override_builtin":
            raise ValueError("Market builtin replacement requires an override_builtin plan")

        directory_name = _require_safe_directory_name(
            forced_directory_name,
            field="directory_name",
        )
        expected_plugin_id = str(market_detail.get("expected_plugin_toml_id") or "").strip()
        package_id = str(plan_dict.get("package_id") or "")
        plugin_id = str(plan_dict.get("plugin_id") or "")
        if (
            not expected_plugin_id
            or expected_plugin_id != plugin_id
            or directory_name != str(plan_dict.get("directory_name") or "")
        ):
            raise ValueError("Market replacement identity does not match the builtin override plan")
        expected_version = str(market_detail.get("version") or "").strip()
        if (
            not expected_version
            or expected_version != str(plan_dict.get("target_version") or "").strip()
        ):
            raise ValueError("Market replacement version does not match the builtin override package")

        manager = self._require_install_source_manager()
        entry = manager.find_active_market_entry(expected_plugin_id)
        confirmed_manual_takeover = bool(
            is_manual_takeover_entry(entry)
            and len(manual_takeover_snapshot_sha256.strip()) == 64
        )
        if entry is None and len(manual_takeover_snapshot_sha256.strip()) == 64:
            user_entry_reader = getattr(manager, "find_active_user_entry", None)
            candidate = (
                user_entry_reader(expected_plugin_id)
                if callable(user_entry_reader)
                else None
            )
            if is_manual_takeover_entry(candidate):
                entry = candidate
                confirmed_manual_takeover = True
        installed_package_id = str(getattr(entry, "package_id", "") or plugin_id)
        if (
            entry is None
            or getattr(entry, "root_id", "") != "user"
            or getattr(entry, "directory_name", "") != directory_name
            or getattr(entry, "plugin_id", "") != plugin_id
            or (is_manual_takeover_entry(entry) and not confirmed_manual_takeover)
            or (not confirmed_manual_takeover and installed_package_id != package_id)
        ):
            raise ValueError("Market replacement does not match the active install-source lock")

        return await asyncio.to_thread(
            self._install_sync,
            package=package,
            plugins_root=None,
            profiles_root=profiles_root,
            on_conflict="fail",
            use_staging=True,
            forced_directory_name=directory_name,
            _allow_external_profiles_root=_allow_external_profiles_root,
        )

    async def _record_requested_install_source(
        self,
        *,
        install_result: dict[str, object],
        package: str,
        source: Literal["imported"] | None,
    ) -> dict[str, object]:
        if source is None:
            return install_result

        try:
            package_path = self._resolve_package_path(package)
            package_sha256 = await asyncio.to_thread(self._sha256_file, package_path)
        except Exception as exc:
            logger.warning(
                "prepare install source failed: err_type={}, err={}",
                type(exc).__name__,
                str(exc),
            )
            return {
                **install_result,
                "install_source_warning": f"install_source_prepare_failed: {exc}",
            }
        warning = await self._record_install_source_best_effort(
            install_result=install_result,
            package_filename=package_path.name,
            package_sha256=package_sha256,
            override=None,
        )
        if warning is None:
            return install_result
        return {**install_result, "install_source_warning": warning}

    async def _record_manual_takeover_source(
        self,
        *,
        manager: InstallSourceManager,
        install_result: dict[str, object],
        package_path: Path,
    ) -> None:
        """Commit a manual takeover source row inside replacement rollback."""

        package_sha256 = await asyncio.to_thread(self._sha256_file, package_path)
        await asyncio.to_thread(
            _record_install_source_for_install_result,
            manager,
            install_result,
            package_path.name,
            package_sha256,
            None,
        )

    async def analyze(
        self,
        *,
        plugins: list[str],
        plugin_refs: list[dict[str, Any]] | None = None,
        current_sdk_version: str | None = None,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._analyze_sync,
            plugins=plugins,
            plugin_refs=plugin_refs,
            current_sdk_version=current_sdk_version,
        )

    # ── Upload & Download ──────────────────────────────────────────────

    async def save_uploaded_package(self, *, filename: str, content: bytes) -> dict[str, object]:
        """Save an uploaded package file to the target directory.

        Returns metadata about the saved file including its server-side path,
        which can be passed to ``install`` or ``inspect``.
        """
        return await asyncio.to_thread(self._save_uploaded_package_sync, filename=filename, content=content)

    async def save_uploaded_file(self, *, filename: str, source_file: BinaryIO) -> dict[str, object]:
        """Stream an uploaded package into the managed artifacts directory."""
        return await asyncio.to_thread(
            self._save_uploaded_file_sync,
            filename=filename,
            source_file=source_file,
        )

    @serialized_plugin_operation
    async def discard_uploaded_package(self, *, package: str) -> dict[str, object]:
        """Remove one upload owned by an abandoned Plugin Center workflow."""
        return await asyncio.to_thread(self._discard_uploaded_package_sync, package=package)

    @serialized_plugin_operation
    async def upload_and_install(
        self,
        *,
        filename: str,
        content: bytes | None = None,
        package_path: str | None = None,
        profiles_root: str | None = None,
        _allow_external_profiles_root: bool = False,
        on_conflict: str = "fail",
        install_source_override: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        """Upload, unpack, and atomically record the install source (design §3.3).

        ``install_source_override`` lets the caller pin the lock entry to
        ``channel="market"`` and mode (``install`` / ``upgrade`` / ``reinstall``)
        in a single call. When ``None`` this method is exactly equivalent to
        :meth:`upload_and_unpack` (no lock write).

        ``install_source_override`` schema (design §3.3.1):

        ```
        {
            "channel": "market",
            "mode": "install" | "upgrade" | "reinstall",
            "market_detail": {
                "plugin_market_id": str,
                "version": str,
                "package_url": str,
                "channel": str,            # "stable" | "beta"
                "package_sha256": str,     # 64-hex from caller; we re-verify
                "payload_hash": str | None,
                "published_at": str,       # ISO 8601
            },
        }
        ```

        Returns a dict with ``upload`` / ``unpack`` / ``install`` keys; the
        ``install`` dict mirrors :class:`SourceDetailMarket` fields. When
        warnings accrue (e.g. mismatched sha256, missing market_detail
        keys, fall back to imported channel) they are joined into an
        ``install_source_warning`` string in the return value (Req 3.4 / R10.5).

        Failure semantics (Req 3.6 / design §10.1):

        * Any exception from the save / unpack / record steps cleans up
          the saved package file and the unpacked directory before
          re-raising. The lock is never left with a half-written entry.
        * ``record_market_*`` raising :class:`InstallSourceError` with
          ``code="lock_write_failed"`` propagates verbatim so the caller
          (Bridge ``_execute_install``) can map it to the right user-facing
          error code.
        """

        if content is None and package_path is None:
            raise ValueError("upload_and_install requires content or package_path")
        if content is not None and package_path is not None:
            raise ValueError("upload_and_install accepts content or package_path, not both")
        if (
            install_source_override is not None
            and install_source_override.get("channel") == "market"
        ):
            manager = get_install_source_manager()
            if manager is not None and manager.is_degraded:
                raise ServerDomainError(
                    code="INSTALL_SOURCE_READ_ONLY",
                    message="Market installation requires a writable install-source lock",
                    status_code=503,
                    details={"reason": manager.degrade_reason or "read_only_degrade"},
                )

        if install_source_override is None:
            owns_saved_package = content is not None or package_path is not None
            saved: dict[str, object] | None = None
            unpacked_target_dirs: list[Path] = []
            unpacked_profile_dirs: list[Path] = []
            if package_path is not None:
                saved = await asyncio.to_thread(
                    self._save_package_file_sync,
                    filename=filename,
                    package_path=package_path,
                )
                actual_sha256 = await asyncio.to_thread(
                    self._sha256_file,
                    str(saved["path"]),
                )
            else:
                saved = await self.save_uploaded_package(
                    filename=filename,
                    content=content or b"",
                )
                actual_sha256 = hashlib.sha256(content or b"").hexdigest().lower()
            try:
                install_result = await self.install(
                    package=str(saved["path"]),
                    profiles_root=profiles_root,
                    on_conflict=on_conflict,
                    use_staging=True,
                    _allow_external_profiles_root=_allow_external_profiles_root,
                )
                unpacked_target_dirs = self._extract_unpack_target_dirs(install_result)
                unpacked_profile_dirs = self._extract_unpack_profile_dirs(install_result)
                warning = await self._record_install_source_best_effort(
                    install_result=install_result,
                    package_filename=str(saved["name"]),
                    package_sha256=actual_sha256,
                    override=None,
                )
                payload: dict[str, object] = {
                    "upload": saved,
                    "install": install_result,
                }
                if warning is not None:
                    payload["install_source_warning"] = warning
                return payload
            except Exception:
                self._cleanup_after_failure(
                    saved=saved,
                    unpacked_target_dirs=unpacked_target_dirs,
                    unpacked_profile_dirs=unpacked_profile_dirs,
                    delete_saved_package=owns_saved_package,
                )
                raise

        channel = install_source_override.get("channel")
        if channel != "market":
            raise ValueError(
                f"unsupported install_source_override channel: {channel!r}"
            )

        warnings: list[str] = []
        saved: dict[str, object] | None = None
        unpack_result: dict[str, object] | None = None
        unpacked_target_dirs: list[Path] = []
        unpacked_profile_dirs: list[Path] = []
        owns_saved_package = False

        try:
            # Step 1 — materialise package bytes on disk when needed.
            if package_path is not None:
                saved = await asyncio.to_thread(
                    self._save_package_file_sync,
                    filename=filename,
                    package_path=package_path,
                )
                actual_sha256 = await asyncio.to_thread(
                    self._sha256_file,
                    str(saved["path"]),
                )
                owns_saved_package = True
            else:
                saved = await self.save_uploaded_package(
                    filename=filename,
                    content=content or b"",
                )
                owns_saved_package = True
                actual_sha256 = hashlib.sha256(content or b"").hexdigest().lower()

            # Step 2 — install/unpack into the user plugin root.
            saved_path = str(saved["path"])
            install_mode = install_source_override.get("mode") or "install"
            if install_mode == "override_builtin":
                market_detail = dict(install_source_override.get("market_detail") or {})
                expected_sha256 = str(market_detail.get("package_sha256") or "").lower()
                if expected_sha256 != actual_sha256:
                    raise ValueError("builtin override Market SHA256 does not match the saved package")
                unpack_result = await self.install_builtin_override(
                    package=saved_path,
                    market_override=install_source_override,
                )
                # The source-switch transaction has committed and owns its
                # rollback. Do not let the outer upload cleanup delete the
                # promoted executable/profile directories if response
                # composition fails after the commit.
                install_dict: dict[str, Any] = {
                    "channel": "market",
                    "directory_name": str(unpack_result["installed_plugins"][0]["target_plugin_id"]),
                    "plugin_id": str(unpack_result["installed_plugins"][0]["target_plugin_id"]),
                    "version": str(market_detail.get("version") or ""),
                    "package_sha256": actual_sha256,
                    "payload_hash": unpack_result.get("payload_hash"),
                    "published_at": str(market_detail.get("published_at") or ""),
                    "previous_version": unpack_result.get("previous_version"),
                }
                warning = unpack_result.get("install_source_warning")
                return self._compose_install_result(
                    saved=saved,
                    unpack_result=unpack_result,
                    install_dict=install_dict,
                    warnings=[str(warning)] if warning else [],
                )
            forced_directory_name = install_source_override.get("directory_name")
            use_staging = install_mode == "install" or isinstance(
                forced_directory_name,
                str,
            )
            market_detail_raw = install_source_override.get("market_detail") or {}
            market_detail = dict(market_detail_raw)
            install_plan = await self.plan_install(
                package=saved_path,
                profiles_root=profiles_root,
                _allow_external_profiles_root=_allow_external_profiles_root,
            )
            if (
                install_mode in ("upgrade", "reinstall")
                and install_plan.get("action") == "override_builtin"
                and isinstance(forced_directory_name, str)
            ):
                unpack_result = await self._install_market_builtin_replacement(
                    package=saved_path,
                    profiles_root=profiles_root,
                    _allow_external_profiles_root=_allow_external_profiles_root,
                    forced_directory_name=forced_directory_name,
                    market_detail=market_detail,
                    actual_sha256=actual_sha256,
                    manual_takeover_snapshot_sha256=str(
                        install_source_override.get(
                            "manual_takeover_snapshot_sha256"
                        )
                        or ""
                    ),
                )
            else:
                unpack_result = await self.install(
                    package=saved_path,
                    plugins_root=None,
                    profiles_root=profiles_root,
                    on_conflict=on_conflict,
                    use_staging=use_staging,
                    forced_directory_name=(
                        forced_directory_name
                        if isinstance(forced_directory_name, str)
                        else None
                    ),
                    _allow_external_profiles_root=_allow_external_profiles_root,
                )
            unpacked_target_dirs = self._extract_unpack_target_dirs(unpack_result)
            unpacked_profile_dirs = self._extract_unpack_profile_dirs(unpack_result)
            target_dir, _target_directory_plugin_id = self._extract_unpack_target(
                unpack_result
            )
            package_plugin_id = self._read_installed_plugin_toml_id(target_dir)

            # Step 4 — degrade to imported when market_detail is incomplete.
            required_keys = ("plugin_market_id", "version", "package_url")
            missing = [k for k in required_keys if not market_detail.get(k)]
            if missing:
                warnings.append(
                    f"market_detail missing required fields ({', '.join(missing)}); "
                    "falling back to imported channel"
                )
                install_dict = await self._record_imported_for_unpack(
                    target_dir=target_dir,
                    saved_filename=str(saved["name"]),
                    actual_sha256=actual_sha256,
                    package_id=str(unpack_result.get("package_id") or ""),
                    profile_dir=str(unpack_result.get("profile_dir") or ""),
                )
                if install_mode == "install":
                    refresh_warning = await _refresh_committed_market_install(
                        package_plugin_id
                    )
                    if refresh_warning is not None:
                        warnings.append(refresh_warning)
                return self._compose_install_result(
                    saved=saved,
                    unpack_result=unpack_result,
                    install_dict=install_dict,
                    warnings=warnings,
                )

            # Step 4b — plugin identity consistency check.
            # When Market tells us "this is plugin X" by passing
            # ``expected_plugin_toml_id`` (the Market plugin slug),
            # the unpacked package's plugin.toml [plugin].id is expected
            # to match. Fresh installs keep the historic soft-warning
            # behavior because Market may still publish legacy slugs, but
            # upgrade/reinstall must fail fast: the bridge rollback flow is
            # keyed to the original plugin id and directory.
            expected_toml_id = market_detail.get("expected_plugin_toml_id")
            if (
                isinstance(expected_toml_id, str)
                and expected_toml_id
                and package_plugin_id
                and expected_toml_id != package_plugin_id
            ):
                message = (
                    f"plugin identity mismatch: Market declared "
                    f"'{expected_toml_id}' but the package contains "
                    f"plugin id '{package_plugin_id}'"
                )
                if install_mode in ("upgrade", "reinstall"):
                    raise ValueError(message)
                warnings.append(
                    f"{message}; install proceeds but please verify the "
                    "package source"
                )
            # ``expected_plugin_toml_id`` is informational only — drop it
            # before passing market_detail to ISM so it does not leak into
            # the lock entry's source_detail.
            market_detail.pop("expected_plugin_toml_id", None)

            # Step 5 — overwrite hash fields with our own freshly-computed
            # values. Mismatches are warnings, not failures (R3.5 says
            # caller's value is informational; the bytes we hashed are what
            # actually landed on disk).
            caller_sha = (market_detail.get("package_sha256") or "").lower()
            if caller_sha and caller_sha != actual_sha256:
                warnings.append(
                    f"package_sha256 mismatch: market={caller_sha!r}, "
                    f"actual={actual_sha256!r}; recording actual"
                )
            market_detail["package_sha256"] = actual_sha256

            unpacked_payload_hash = unpack_result.get("payload_hash")
            if isinstance(unpacked_payload_hash, str) and unpacked_payload_hash:
                caller_payload = market_detail.get("payload_hash")
                if (
                    isinstance(caller_payload, str)
                    and caller_payload
                    and caller_payload.lower() != unpacked_payload_hash.lower()
                ):
                    warnings.append(
                        "payload_hash mismatch between market and unpacked package"
                    )
                market_detail["payload_hash"] = unpacked_payload_hash

            # Step 6 — record into ISM with the right semantic.
            mgr = self._require_install_source_manager()
            root_id, directory_name = classify_plugin_path(
                target_dir,
                builtin_root=mgr.builtin_root,
                user_root=mgr.user_root,
            )

            if install_mode in ("upgrade", "reinstall"):
                entry, ism_warnings = mgr.record_market_upgrade(
                    root_id=root_id,
                    directory_name=directory_name,
                    plugin_id=package_plugin_id,
                    market_detail=market_detail,
                    package_id=str(unpack_result.get("package_id") or ""),
                    profile_dir=str(unpack_result.get("profile_dir") or ""),
                )
            else:
                entry, ism_warnings = mgr.record_market_install(
                    root_id=root_id,
                    directory_name=directory_name,
                    plugin_id=package_plugin_id,
                    market_detail=market_detail,
                    package_id=str(unpack_result.get("package_id") or ""),
                    profile_dir=str(unpack_result.get("profile_dir") or ""),
                )
            warnings.extend(ism_warnings)
            if install_mode == "install":
                refresh_warning = await _refresh_committed_market_install(
                    package_plugin_id
                )
                if refresh_warning is not None:
                    warnings.append(refresh_warning)

            install_dict: dict[str, Any] = {
                "channel": entry.channel,
                "directory_name": entry.directory_name,
                "plugin_id": entry.plugin_id,
            }
            if entry.source_detail is not None and hasattr(
                entry.source_detail, "version"
            ):
                # Mirror SourceDetailMarket fields for the API response.
                install_dict.update(
                    {
                        "version": getattr(entry.source_detail, "version", ""),
                        "package_sha256": getattr(
                            entry.source_detail, "package_sha256", ""
                        ),
                        "payload_hash": getattr(
                            entry.source_detail, "payload_hash", None
                        ),
                        "published_at": getattr(
                            entry.source_detail, "published_at", ""
                        ),
                        "previous_version": getattr(
                            entry.source_detail, "previous_version", None
                        ),
                    }
                )

            return self._compose_install_result(
                saved=saved,
                unpack_result=unpack_result,
                install_dict=install_dict,
                warnings=warnings,
            )

        except InstallSourceError:
            # Lock write failed — fs cleanup still runs, but propagate the
            # structured error so Bridge can map it to ``lock_write_failed``.
            self._cleanup_after_failure(
                saved=saved,
                unpacked_target_dirs=unpacked_target_dirs,
                unpacked_profile_dirs=unpacked_profile_dirs,
                delete_saved_package=owns_saved_package,
            )
            raise
        except Exception:
            self._cleanup_after_failure(
                saved=saved,
                unpacked_target_dirs=unpacked_target_dirs,
                unpacked_profile_dirs=unpacked_profile_dirs,
                delete_saved_package=owns_saved_package,
            )
            raise

    @staticmethod
    def _extract_unpack_entries(unpack_result: dict[str, object]) -> list[dict[str, object]]:
        unpacked_plugins = unpack_result.get("unpacked_plugins")
        if unpacked_plugins is None:
            unpacked_plugins = unpack_result.get("installed_plugins")
        if not isinstance(unpacked_plugins, list) or not unpacked_plugins:
            raise ValueError("install returned no plugins")
        entries: list[dict[str, object]] = []
        for item in unpacked_plugins:
            if not isinstance(item, dict):
                raise ValueError("unpack returned malformed unpacked_plugins entry")
            entries.append(item)
        return entries

    @classmethod
    def _extract_unpack_target_dirs(cls, unpack_result: dict[str, object]) -> list[Path]:
        """Return every target dir created by the unpack operation."""

        target_dirs: list[Path] = []
        for entry in cls._extract_unpack_entries(unpack_result):
            target_dir_raw = entry.get("target_dir")
            if isinstance(target_dir_raw, str) and target_dir_raw:
                target_dirs.append(Path(target_dir_raw))
        return target_dirs

    @staticmethod
    def _extract_unpack_profile_dirs(unpack_result: dict[str, object]) -> list[Path]:
        """Return promoted profile dirs created by the unpack operation."""

        if unpack_result.get("profile_reused") is True:
            return []
        profile_dir_raw = unpack_result.get("profile_dir")
        if isinstance(profile_dir_raw, str) and profile_dir_raw:
            return [Path(profile_dir_raw)]
        return []

    @classmethod
    def _extract_unpack_target(
        cls,
        unpack_result: dict[str, object],
    ) -> tuple[Path, str]:
        """Pull the single Market plugin's target dir + plugin id from a dump.

        The CLI returns potentially many ``unpacked_plugins`` for bundles,
        but Market install-source metadata and rollback are single-plugin
        flows. Reject multi-plugin Market packages before recording any lock
        entry so extra unpacked plugins cannot become untracked installs.
        """

        unpacked_plugins = cls._extract_unpack_entries(unpack_result)
        if len(unpacked_plugins) != 1:
            raise ValueError(
                "Market packages must contain exactly one plugin; "
                f"got {len(unpacked_plugins)}"
            )
        first = unpacked_plugins[0]
        target_dir_raw = first.get("target_dir")
        if not isinstance(target_dir_raw, str) or not target_dir_raw:
            raise ValueError("unpack returned no target_dir for plugin")
        target_plugin_id = str(first.get("target_plugin_id", "")) or ""
        return Path(target_dir_raw), target_plugin_id

    @staticmethod
    def _read_installed_plugin_toml_id(target_dir: Path) -> str:
        plugin_toml = target_dir / "plugin.toml"
        try:
            data = tomllib.loads(plugin_toml.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"installed plugin.toml not found: {plugin_toml}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"installed plugin.toml is invalid TOML: {plugin_toml}") from exc

        plugin_table = data.get("plugin")
        if not isinstance(plugin_table, dict):
            raise ValueError(f"installed plugin.toml missing [plugin] table: {plugin_toml}")
        plugin_id = plugin_table.get("id")
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ValueError(f"installed plugin.toml missing [plugin].id: {plugin_toml}")
        return plugin_id.strip()

    def _compose_install_result(
        self,
        *,
        saved: dict[str, object],
        unpack_result: dict[str, object],
        install_dict: dict[str, Any],
        warnings: list[str],
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "upload": saved,
            "unpack": unpack_result,
            "install": install_dict,
        }
        if warnings:
            result["install_source_warning"] = "; ".join(warnings)
        return result

    async def _record_imported_for_unpack(
        self,
        *,
        target_dir: Path,
        saved_filename: str,
        actual_sha256: str,
        package_id: str,
        profile_dir: str,
    ) -> dict[str, Any]:
        """Fall back to recording the install as ``channel="imported"``.

        Used when ``market_detail`` lacks the required keys; the user
        still gets a working plugin and we still record source-truth, just
        without the Market-side evidence.
        """

        mgr = self._require_install_source_manager()

        def _record() -> None:
            mgr.record_import(
                directory_path=target_dir,
                package_filename=saved_filename,
                package_sha256=actual_sha256,
                package_id=package_id,
                profile_dir=profile_dir,
            )

        await asyncio.to_thread(_record)
        # Build a minimal install_dict mirroring the imported entry shape
        # (no version / channel for imported channel by design).
        return {
            "channel": "imported",
            "directory_name": target_dir.name,
            "plugin_id": target_dir.name,
            "package_filename": saved_filename,
            "package_sha256": actual_sha256,
        }

    def _cleanup_after_failure(
        self,
        *,
        saved: dict[str, object] | None,
        unpacked_target_dirs: list[Path] | None = None,
        unpacked_profile_dirs: list[Path] | None = None,
        delete_saved_package: bool = True,
    ) -> None:
        """Best-effort fs cleanup on upload_and_install failure (R3.6).

        Order is important: we delete the unpacked directory first (so a
        partial extract doesn't get adopted by the next reconcile pass)
        and then the saved archive. Both calls swallow OSError because
        the original exception is what we care about — cleanup failures
        get logged but don't shadow the real error.
        """

        for unpacked_target_dir in unpacked_target_dirs or []:
            self._cleanup_failed_unpack(unpacked_target_dir)
        for unpacked_profile_dir in unpacked_profile_dirs or []:
            self._cleanup_failed_unpack(unpacked_profile_dir)
        if delete_saved_package and saved is not None:
            saved_path_raw = saved.get("path")
            if isinstance(saved_path_raw, str) and saved_path_raw:
                try:
                    Path(saved_path_raw).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "upload_and_install: failed to clean up saved package "
                        "{}: {}",
                        saved_path_raw,
                        exc,
                    )

    @staticmethod
    def _cleanup_failed_unpack(target_dir: Path) -> None:
        """Recursively remove ``target_dir`` ignoring missing-path errors.

        Does NOT touch the lock file — fs rollback only. The caller is
        responsible for ensuring no partial lock entry exists (we never
        write one before unpack completes).
        """

        try:
            shutil.rmtree(target_dir, ignore_errors=True)
        except OSError as exc:  # pragma: no cover — ignore_errors=True suppresses
            logger.warning(
                "upload_and_install: _cleanup_failed_unpack({}) failed: {}",
                target_dir,
                exc,
            )

    @staticmethod
    def _require_install_source_manager() -> InstallSourceManager:
        """Resolve the global manager or raise a clear configuration error.

        The manager is published by ``StartupReconciler`` during FastAPI
        lifespan startup; if a caller hits the market install path before
        that has run we want a meaningful error rather than ``AttributeError``
        on ``None.record_market_install``.
        """

        mgr = get_install_source_manager()
        if mgr is None:
            raise ServerDomainError(
                code="INSTALL_SOURCE_NOT_READY",
                message="install source manager is not initialised",
                status_code=503,
                details={"hint": "wait for FastAPI lifespan startup to complete"},
            )
        return mgr

    def resolve_download_path(self, package: str) -> Path:
        """Resolve and validate a package path for download.

        Returns the absolute path to the package file.  Raises if the file
        does not exist or is outside the target directory.
        """
        try:
            return self._resolve_package_path(package)
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="download") from exc

    # ── Sync helpers ───────────────────────────────────────────────────

    @staticmethod
    def _path_policy() -> PluginCliPathPolicy:
        return PluginCliPathPolicy.from_settings()

    def _autostart_gate_root(self, plugins_root: str | None) -> Path | None:
        """The directory the install rollback checks for leftovers.

        ``None`` means the question could not be asked, and the caller keeps the
        id gated. Everything is inside the try on purpose: this runs from an
        except block that is already carrying the install's real failure, and a
        ``PluginCliPathPolicy.from_settings()`` error escaping here would both
        replace that cause and skip every restore in the loop (coderabbit).
        """
        try:
            policy = self._path_policy()
            if not plugins_root:
                return policy.user_plugins_root
            return _require_within(
                Path(plugins_root).expanduser().resolve(),
                policy.user_plugins_root,
                field="plugins_root",
            )
        except Exception as exc:
            # 越界的 root 本来就装不进去；能拿到策略就回落到真正的用户插件根去找
            # 残骸，拿不到就交给调用方按「查不了」处理。
            logger.error("cannot resolve the install rollback root: {}", exc)
            try:
                return self._path_policy().user_plugins_root
            except Exception:
                return None

    def _resolver(self) -> PluginSourceResolver:
        return PluginSourceResolver(self._path_policy())

    def _list_local_plugins_sync(self) -> dict[str, object]:
        try:
            sources = self._resolver().list_plugins()
            plugins = [source.directory_name for source in sources]
            plugin_refs = [
                {
                    "root_id": source.root_id,
                    "directory_name": source.directory_name,
                    "plugin_id": source.plugin_id,
                    "label": (
                        f"{source.plugin_id} ({source.root_id}/{source.directory_name})"
                        if source.plugin_id and source.plugin_id != source.directory_name
                        else f"{source.root_id}/{source.directory_name}"
                    ),
                }
                for source in sources
            ]
            return {"plugins": plugins, "plugin_refs": plugin_refs, "count": len(sources)}
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="list_plugins") from exc

    def _list_local_packages_sync(self) -> dict[str, object]:
        try:
            target_root = self._path_policy().package_artifacts_root
            items: list[dict[str, object]] = []
            package_paths = [
                path
                for path in target_root.glob("*")
                if path.is_file() and self._has_allowed_upload_suffix(path.name)
            ]
            for path in sorted(
                package_paths,
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            ):
                stat = path.stat()
                items.append(
                    {
                        "name": path.name,
                        "path": str(path.resolve()),
                        "suffix": path.suffix,
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
            return {"packages": items, "count": len(items), "target_dir": str(target_root)}
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="list_packages") from exc

    def _build_sync(
        self,
        *,
        mode: str,
        plugin: str | None,
        plugins: list[str] | None,
        plugin_ref: dict[str, Any] | None,
        plugin_refs: list[dict[str, Any]] | None,
        out: str | None,
        target_dir: str | None,
        keep_staging: bool,
        bundle_id: str | None,
        package_name: str | None,
        package_description: str | None,
        version: str | None,
    ) -> dict[str, object]:
        try:
            policy = self._path_policy()
            target_root = policy.package_artifacts_root
            sources = self._resolve_plugin_sources(
                mode=mode,
                plugin=plugin,
                plugins=plugins or [],
                plugin_ref=plugin_ref,
                plugin_refs=plugin_refs or [],
            )
            plugin_dirs = [source.plugin_dir for source in sources]
            resolved_target_dir = Path(target_dir).expanduser().resolve() if target_dir else target_root
            _require_within(resolved_target_dir, target_root, field="target_dir")
            resolved_target_dir.mkdir(parents=True, exist_ok=True)

            if out and mode != "bundle" and len(plugin_dirs) != 1:
                raise ValueError("'out' can only be used when building a single plugin")

            if mode == "bundle":
                resolved_bundle_id = bundle_id or "__".join(sorted(item.directory_name for item in sources))
                output_path = (
                    _require_within(Path(out).expanduser().resolve(), target_root, field="out")
                    if out
                    else _require_within(
                        (resolved_target_dir / f"{resolved_bundle_id}.neko-bundle").resolve(),
                        target_root,
                        field="out",
                    )
                )
                result = build_bundle(
                    plugin_dirs,
                    output_path,
                    bundle_id=resolved_bundle_id,
                    package_name=package_name,
                    package_description=package_description,
                    version=version or "0.1.0",
                    keep_staging=keep_staging,
                )
                built = [result.model_dump(mode="json")]
                return {
                    "built": built,
                    "built_count": len(built),
                    "failed": [],
                    "failed_count": 0,
                    "ok": True,
                }

            built: list[dict[str, object]] = []
            failed: list[dict[str, object]] = []
            output_stems = self._output_stems_for_sources(sources)
            for source, plugin_dir in zip(sources, plugin_dirs, strict=True):
                output_path = (
                    _require_within(Path(out).expanduser().resolve(), target_root, field="out")
                    if out
                    else resolved_target_dir / f"{output_stems[source]}.neko-plugin"
                )
                try:
                    result = build_plugin(
                        plugin_dir,
                        output_path,
                        keep_staging=keep_staging,
                    )
                    built.append(result.model_dump(mode="json"))
                except Exception as exc:
                    failed.append({"plugin": f"{source.root_id}/{source.directory_name}", "error": str(exc)})

            return {
                "built": built,
                "built_count": len(built),
                "failed": failed,
                "failed_count": len(failed),
                "ok": not failed,
            }
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="build") from exc

    def _inspect_sync(self, *, package: str) -> dict[str, object]:
        try:
            result = inspect_package(self._resolve_package_path(package))
            return result.model_dump(mode="json")
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="inspect") from exc

    def _verify_sync(self, *, package: str) -> dict[str, object]:
        try:
            result = inspect_package(self._resolve_package_path(package))
            payload_hash_verified = result.payload_hash_verified
            return {
                **result.model_dump(mode="json"),
                "ok": payload_hash_verified is True,
            }
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="verify") from exc

    def _plan_install_sync(
        self,
        *,
        package: str,
        plugins_root: str | None,
        profiles_root: str | None,
        _allow_external_profiles_root: bool = False,
    ) -> dict[str, object]:
        try:
            policy = self._path_policy()
            policy.ensure_writable_layout()
            target_root = (
                _require_within(
                    Path(plugins_root).expanduser().resolve(),
                    policy.user_plugins_root,
                    field="plugins_root",
                )
                if plugins_root
                else policy.user_plugins_root
            )
            profiles_root_path = (
                Path(profiles_root).expanduser().resolve()
                if profiles_root and _allow_external_profiles_root
                else (
                    _require_within(
                        Path(profiles_root).expanduser().resolve(),
                        policy.package_profiles_root,
                        field="profiles_root",
                    )
                    if profiles_root
                    else policy.package_profiles_root
                )
            )
            package_path = self._resolve_package_path(package)
            plan = self._apply_installed_package_identity(
                build_install_plan(
                    package_path=package_path,
                    plugins_root=target_root,
                    builtin_plugins_root=policy.builtin_plugins_root,
                ),
                package_path=package_path,
                target_root=target_root,
                profiles_root=profiles_root_path,
            )
            if plan.action == "override_builtin" or plan.reason == "manual_takeover":
                inspected = inspect_package(package_path)
                target_profile_dir = profiles_root_path / plan.package_id
                if getattr(inspected, "profile_names", ()) and (
                    target_profile_dir.exists() or target_profile_dir.is_symlink()
                ):
                    plan = replace(
                        plan,
                        action="blocked",
                        confirmation_token="",
                        reason=(
                            "manual_takeover_profile_target_exists"
                            if plan.reason == "manual_takeover"
                            else "override_profile_target_exists"
                        ),
                    )
            return asdict(plan)
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="install-plan") from exc

    def _apply_installed_package_identity(
        self,
        plan: PluginInstallPlan,
        *,
        package_path: Path,
        target_root: Path,
        profiles_root: Path,
    ) -> PluginInstallPlan:
        target_dir = target_root / plan.directory_name
        manager = get_install_source_manager()
        entry_reader = getattr(manager, "entry_for_directory", None)
        entry = entry_reader(target_dir) if callable(entry_reader) else None
        if (
            plan.action == "blocked"
            and plan.reason == "plugin_builtin_override_market_required"
            and is_manual_takeover_entry(entry)
            and entry.plugin_id == plan.plugin_id
            and entry.directory_name == plan.directory_name
        ):
            # A canonical builtin and its canonical user override are valid
            # peers. Rebuild only the user-side replacement plan after the
            # exact manual LockEntry proves this is an ownership transfer,
            # not an attempt to overwrite the builtin source.
            plan = build_install_plan(
                package_path=package_path,
                plugins_root=target_root,
                builtin_plugins_root=None,
            )
        if plan.action not in REPLACEMENT_ACTIONS:
            return plan

        if not plan.manifestless_state and entry is None:
            return replace(
                plan,
                action="blocked",
                confirmation_token="",
                reason="install_source_ownership_unknown",
                current_source="unknown",
                target_source="imported",
            )
        if is_manual_takeover_entry(entry):
            assert isinstance(entry, LockEntry)
            if bool(getattr(manager, "is_degraded", False)):
                return replace(
                    plan,
                    action="blocked",
                    confirmation_token="",
                    reason="install_source_read_only",
                    current_source="manual",
                    target_source="imported",
                )
            if entry.plugin_id != plan.plugin_id or entry.directory_name != plan.directory_name:
                return replace(
                    plan,
                    action="blocked",
                    confirmation_token="",
                    reason="manual_takeover_identity_mismatch",
                    current_source="manual",
                    target_source="imported",
                )
            return replace(
                plan,
                confirmation_token=local_manual_takeover_confirmation_token(
                    package_path=package_path,
                    target_dir=target_dir,
                    entry=entry,
                ),
                reason="manual_takeover",
                installed_package_id=plan.package_id,
                current_source="manual",
                target_source="imported",
            )
        installed_package_id = str(getattr(entry, "package_id", "") or "")
        if not installed_package_id and plan.manifestless_state:
            package_id_reader = getattr(manager, "package_id_for_directory", None)
            installed_package_id = (
                package_id_reader(target_dir) if callable(package_id_reader) else ""
            )
        if not installed_package_id:
            # Legacy rows predate package identity tracking. Directory
            # existence cannot prove ownership because stale or unrelated
            # profile trees may share the incoming name. Historical official
            # single-plugin packages used plugin_id as package_id, so use that
            # conservative baseline and fail closed on any ambiguous rename.
            installed_package_id = plan.plugin_id
        if installed_package_id != plan.package_id:
            return replace(
                plan,
                action="blocked",
                confirmation_token="",
                reason="package_id_change",
                installed_package_id=installed_package_id,
            )
        return replace(plan, installed_package_id=installed_package_id)

    def _install_sync(
        self,
        *,
        package: str,
        plugins_root: str | None,
        profiles_root: str | None,
        on_conflict: str,
        use_staging: bool = True,
        forced_directory_name: str | None = None,
        _allow_external_profiles_root: bool = False,
    ) -> dict[str, object]:
        try:
            policy = self._path_policy()
            policy.ensure_writable_layout()
            install_plugins_root = policy.user_plugins_root
            install_profiles_root = policy.package_profiles_root
            plugins_root_path = (
                _require_within(Path(plugins_root).expanduser().resolve(), install_plugins_root, field="plugins_root")
                if plugins_root
                else install_plugins_root
            )
            profiles_root_path = (
                Path(profiles_root).expanduser().resolve()
                if profiles_root and _allow_external_profiles_root
                else (
                    _require_within(
                        Path(profiles_root).expanduser().resolve(),
                        install_profiles_root,
                        field="profiles_root",
                    )
                    if profiles_root
                    else install_profiles_root
                )
            )
            package_path = self._resolve_package_path(package)
            if use_staging:
                result = self._install_via_staging_sync(
                    package=package_path,
                    plugins_root=plugins_root_path,
                    profiles_root=profiles_root_path,
                    on_conflict=on_conflict,
                    forced_directory_name=forced_directory_name,
                )
            elif forced_directory_name is not None:
                raise ValueError("forced_directory_name requires use_staging=True")
            else:
                result = install_package(
                    package_path,
                    plugins_root=plugins_root_path,
                    profiles_root=profiles_root_path,
                    on_conflict=on_conflict,
                )
            return result.model_dump(mode="json")
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="install") from exc

    def _install_via_staging_sync(
        self,
        *,
        package: Path,
        plugins_root: Path,
        profiles_root: Path,
        on_conflict: str,
        forced_directory_name: str | None = None,
    ) -> InstallResult:
        """Extract into a staging tree, then rename into place atomically."""

        forced_directory_name = (
            _require_safe_directory_name(forced_directory_name, field="forced_directory_name")
            if forced_directory_name is not None
            else None
        )
        staging_token = uuid.uuid4().hex
        staging_plugins = plugins_root / f".neko_staging_{staging_token}"
        staging_profiles = profiles_root / f".neko_staging_{staging_token}"
        staging_plugins.mkdir(parents=True, exist_ok=True)
        staging_profiles.mkdir(parents=True, exist_ok=True)
        installer = PackageInstaller()
        promoted_plugins: list[InstalledPlugin] = []
        promoted_profile: Path | None = None
        profile_reused = False

        try:
            staged = install_package(
                package,
                plugins_root=staging_plugins,
                profiles_root=staging_profiles,
                on_conflict="fail",
            )

            for item in staged.installed_plugins:
                source_dir = Path(item.target_dir)
                desired_name = forced_directory_name or item.target_plugin_id
                desired = plugins_root / desired_name
                final_dir = installer.resolve_plugin_target_dir(
                    desired,
                )
                if source_dir.resolve() != final_dir.resolve():
                    final_dir.parent.mkdir(parents=True, exist_ok=True)
                    source_dir.rename(final_dir)
                promoted_plugins.append(
                    InstalledPlugin(
                        source_folder=item.source_folder,
                        target_plugin_id=final_dir.name,
                        target_dir=final_dir,
                        renamed=(final_dir.name != item.source_folder),
                    )
                )
                if not (final_dir / "plugin.toml").is_file():
                    raise ValueError(f"promoted plugin is missing plugin.toml: {final_dir}")

            if staged.profile_dir is not None:
                source_profile = Path(staged.profile_dir)
                desired_profile = profiles_root / source_profile.name
                if _is_link_or_reparse(desired_profile):
                    raise ValueError(
                        "existing package profile path is a link or reparse point: "
                        f"{desired_profile.name}"
                    )
                if desired_profile.exists():
                    if not desired_profile.is_dir():
                        raise ValueError(
                            "existing package profile path is not a directory: "
                            f"{desired_profile.name}"
                        )
                    _validate_existing_profile_ownership(
                        profile_dir=desired_profile,
                        profiles_root=profiles_root,
                        package_id=staged.package_id,
                        plugin_ids={
                            self._read_installed_plugin_toml_id(Path(item.target_dir))
                            for item in promoted_plugins
                        },
                    )
                    # A verified prior install can leave its package profile
                    # behind after executable deletion. Reuse it byte-for-byte. The
                    # staged defaults are intentionally not merged here, so a
                    # failed fresh install never mutates legacy state.
                    promoted_profile = desired_profile.resolve()
                    profile_reused = True
                else:
                    desired_profile = installer.resolve_target_dir(
                        desired_profile,
                        on_conflict=on_conflict,
                    )
                    if source_profile.resolve() != desired_profile.resolve():
                        desired_profile.parent.mkdir(parents=True, exist_ok=True)
                        source_profile.rename(desired_profile)
                    promoted_profile = desired_profile

            return InstallResult(
                package_path=staged.package_path,
                package_type=staged.package_type,
                package_id=staged.package_id,
                plugins_root=plugins_root,
                profiles_root=profiles_root,
                installed_plugins=promoted_plugins,
                profile_dir=promoted_profile,
                profile_reused=profile_reused,
                metadata_found=staged.metadata_found,
                payload_hash=staged.payload_hash,
                payload_hash_verified=staged.payload_hash_verified,
                conflict_strategy=on_conflict,
            )
        except Exception:
            for item in promoted_plugins:
                shutil.rmtree(item.target_dir, ignore_errors=True)
            if promoted_profile is not None and not profile_reused:
                shutil.rmtree(promoted_profile, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging_plugins, ignore_errors=True)
            shutil.rmtree(staging_profiles, ignore_errors=True)

    def _stage_builtin_override_sync(
        self,
        *,
        package: Path,
        plugins_root: Path,
        profiles_root: Path,
        plan: PluginInstallPlan,
    ) -> _StagedBuiltinOverride:
        """Extract and validate an override without touching either live source."""

        staging_token = uuid.uuid4().hex
        unpack_plugins = plugins_root / f".neko_override_unpack_{staging_token}"
        unpack_profiles = profiles_root / f".neko_override_unpack_{staging_token}"
        staged_plugin_dir = plugins_root / f".neko_override_staging_{staging_token}"
        staged_profile_dir = profiles_root / f".neko_override_staging_{staging_token}"
        unpack_plugins.mkdir(parents=True, exist_ok=False)
        unpack_profiles.mkdir(parents=True, exist_ok=False)
        try:
            staged = install_package(
                package,
                plugins_root=unpack_plugins,
                profiles_root=unpack_profiles,
                on_conflict="fail",
            )
            if staged.package_type != "plugin" or len(staged.installed_plugins) != 1:
                raise ValueError("builtin override requires one plugin package")
            [installed] = staged.installed_plugins
            unpacked_plugin_dir = Path(installed.target_dir).resolve()
            if (
                staged.package_id != plan.package_id
                or installed.source_folder != plan.plugin_id
                or installed.target_plugin_id != plan.plugin_id
                or unpacked_plugin_dir.name != plan.directory_name
                or self._read_installed_plugin_toml_id(unpacked_plugin_dir) != plan.plugin_id
            ):
                raise ValueError("staged builtin override identity does not match the plan")
            if staged.payload_hash_verified is False:
                raise ValueError("builtin override package payload hash is not verified")
            unpacked_profile_dir = Path(staged.profile_dir).resolve() if staged.profile_dir else None
            if unpacked_profile_dir is not None and unpacked_profile_dir.name != plan.package_id:
                raise ValueError("staged builtin override profile identity does not match the package")
            unpacked_plugin_dir.rename(staged_plugin_dir)
            if unpacked_profile_dir is not None:
                unpacked_profile_dir.rename(staged_profile_dir)
            return _StagedBuiltinOverride(
                result=staged,
                plugin_dir=staged_plugin_dir,
                profile_dir=staged_profile_dir if unpacked_profile_dir is not None else None,
            )
        except Exception:
            shutil.rmtree(staged_plugin_dir, ignore_errors=True)
            shutil.rmtree(staged_profile_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(unpack_plugins, ignore_errors=True)
            shutil.rmtree(unpack_profiles, ignore_errors=True)

    @staticmethod
    def _cleanup_builtin_override_staging_sync(staged: _StagedBuiltinOverride) -> None:
        shutil.rmtree(staged.plugin_dir, ignore_errors=True)
        if staged.profile_dir is not None:
            shutil.rmtree(staged.profile_dir, ignore_errors=True)

    @staticmethod
    def _sha256_file(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()

    @staticmethod
    def _package_ref_from_path(*, filename: str, package_path: str) -> dict[str, object]:
        resolved = Path(package_path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"package file not found: {package_path}")
        return {
            "name": filename,
            "path": str(resolved),
            "size": resolved.stat().st_size,
        }

    def _analyze_sync(
        self,
        *,
        plugins: list[str],
        plugin_refs: list[dict[str, Any]] | None,
        current_sdk_version: str | None,
    ) -> dict[str, object]:
        try:
            plugin_dirs = [
                source.plugin_dir
                for source in self._resolver().resolve_many(
                    refs=plugin_refs or [],
                    specifiers=plugins,
                )
            ]
            result = analyze_bundle_plugins(
                plugin_dirs,
                current_sdk_version=current_sdk_version,
            )
            return result.model_dump(mode="json")
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="analyze") from exc

    @staticmethod
    def _has_allowed_upload_suffix(filename: str) -> bool:
        return filename.lower().endswith(tuple(_ALLOWED_UPLOAD_SUFFIXES))

    @staticmethod
    def _upload_filename_parts(filename: str) -> tuple[str, str, str]:
        safe_name = Path(filename).name
        if not safe_name:
            raise ValueError("Invalid filename")

        lower_name = safe_name.lower()
        for allowed_suffix in sorted(_ALLOWED_UPLOAD_SUFFIXES, key=len, reverse=True):
            if lower_name.endswith(allowed_suffix):
                return safe_name, safe_name[: -len(allowed_suffix)], allowed_suffix

        allowed = ", ".join(sorted(_ALLOWED_UPLOAD_SUFFIXES))
        raise ValueError(f"Unsupported file type. Allowed: {allowed}")

    @staticmethod
    def _upload_metadata(path: Path) -> dict[str, object]:
        stat = path.stat()
        return {
            "name": path.name,
            "path": str(path.resolve()),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }

    def _save_uploaded_package_sync(self, *, filename: str, content: bytes) -> dict[str, object]:
        try:
            target_root = self._path_policy().package_artifacts_root
            # Validate file size
            if len(content) > _UPLOAD_MAX_BYTES:
                raise ValueError(
                    f"File too large: {len(content)} bytes "
                    f"(max {_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB)"
                )

            safe_name, stem, suffix = self._upload_filename_parts(filename)

            # Ensure target directory exists
            target_root.mkdir(parents=True, exist_ok=True)

            # Exclusive create: if name collides (including concurrent uploads
            # racing on the same filename), pick a UUID-suffixed dest and retry.
            dest = target_root / safe_name
            while True:
                try:
                    with dest.open("xb") as file:
                        file.write(content)
                    break
                except FileExistsError:
                    unique = uuid.uuid4().hex[:8]
                    dest = target_root / f"{stem}_{unique}{suffix}"
                except Exception:
                    dest.unlink(missing_ok=True)
                    raise

            return self._upload_metadata(dest)
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="upload") from exc

    def _discard_uploaded_package_sync(self, *, package: str) -> dict[str, object]:
        """Remove one direct upload using the existing package-path policy."""
        try:
            target_root = self._path_policy().package_artifacts_root.resolve()
            target = self._resolve_package_path(package)
            if target.parent != target_root:
                raise ValueError("only a directly uploaded plugin package can be discarded")
            target.unlink()
            return {"success": True, "removed": True, "name": target.name}
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="discard-upload") from exc

    def _save_uploaded_file_sync(self, *, filename: str, source_file: BinaryIO) -> dict[str, object]:
        """Copy an incoming upload in bounded chunks and enforce the size limit."""
        try:
            target_root = self._path_policy().package_artifacts_root
            safe_name, stem, suffix = self._upload_filename_parts(filename)
            target_root.mkdir(parents=True, exist_ok=True)
            source_file.seek(0)

            dest = target_root / safe_name
            while True:
                try:
                    total_bytes = 0
                    with dest.open("xb") as target:
                        while chunk := source_file.read(_UPLOAD_COPY_CHUNK_BYTES):
                            total_bytes += len(chunk)
                            if total_bytes > _UPLOAD_MAX_BYTES:
                                raise ValueError(
                                    f"File too large: {total_bytes} bytes "
                                    f"(max {_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB)"
                                )
                            target.write(chunk)
                    break
                except FileExistsError:
                    unique = uuid.uuid4().hex[:8]
                    dest = target_root / f"{stem}_{unique}{suffix}"
                except Exception:
                    dest.unlink(missing_ok=True)
                    raise

            return self._upload_metadata(dest)
        except Exception as exc:
            raise self._domain_error_from_exception(exc, action="upload") from exc

    def _save_package_file_sync(self, *, filename: str, package_path: str) -> dict[str, object]:
        """Copy an existing package into the managed package artifacts root."""

        source = Path(package_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"package file not found: {package_path}")
        if source.stat().st_size > _UPLOAD_MAX_BYTES:
            raise ValueError(
                f"File too large: {source.stat().st_size} bytes "
                f"(max {_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB)"
            )

        safe_name, stem, suffix = self._upload_filename_parts(filename or source.name)

        target_root = self._path_policy().package_artifacts_root
        target_root.mkdir(parents=True, exist_ok=True)

        if source.parent == target_root.resolve() and source.name == safe_name:
            return self._upload_metadata(source)

        dest = target_root / safe_name
        while True:
            try:
                with source.open("rb") as src, dest.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                break
            except FileExistsError:
                unique = uuid.uuid4().hex[:8]
                dest = target_root / f"{stem}_{unique}{suffix}"
            except Exception:
                dest.unlink(missing_ok=True)
                raise

        return self._upload_metadata(dest)

    def _resolve_plugin_sources(
        self,
        *,
        mode: str,
        plugin: str | None,
        plugins: list[str],
        plugin_ref: dict[str, Any] | None,
        plugin_refs: list[dict[str, Any]],
    ) -> list[ResolvedPluginSource]:
        resolver = self._resolver()
        if mode == "all":
            sources = resolver.list_plugins()
            if not sources:
                roots = ", ".join(f"{root_id}={root}" for root_id, root in self._path_policy().build_source_roots)
                raise FileNotFoundError(f"No plugin.toml files found under builtin or user plugin roots ({roots})")
            return sources

        if mode == "single":
            if plugin_ref is not None:
                return [resolver.resolve_plugin_ref(plugin_ref)]
            if plugin:
                return [resolver.resolve_string(plugin)]
            raise ValueError("Please provide plugin_ref or plugin when mode=single")

        if mode in {"selected", "bundle"}:
            if plugin_refs:
                return [resolver.resolve_plugin_ref(item) for item in plugin_refs]
            if plugins:
                return [resolver.resolve_string(item) for item in plugins]
            raise ValueError(f"Please provide plugin_refs or plugins when mode={mode}")

        raise ValueError("Unsupported build mode")

    @staticmethod
    def _output_stems_for_sources(sources: list[ResolvedPluginSource]) -> dict[ResolvedPluginSource, str]:
        counts: dict[str, int] = {}
        for source in sources:
            counts[source.directory_name] = counts.get(source.directory_name, 0) + 1
        return {
            source: (
                source.directory_name
                if counts[source.directory_name] == 1
                else f"{source.root_id}_{source.directory_name}"
            )
            for source in sources
        }

    def _resolve_package_path(self, raw: str) -> Path:
        target_root = self._path_policy().package_artifacts_root

        def _accept(path: Path) -> bool:
            return path.is_file() and self._has_allowed_upload_suffix(path.name)

        candidate = Path(raw).expanduser()
        if candidate.exists():
            resolved = candidate.resolve()
            _require_within(resolved, target_root, field=f"package '{raw}'")
            if _accept(resolved):
                return resolved

        target_candidate = (target_root / raw).resolve()
        if target_candidate.exists():
            _require_within(target_candidate, target_root, field=f"package '{raw}'")
            if _accept(target_candidate):
                return target_candidate

        raise FileNotFoundError(f"package file not found: {raw}")

    async def _record_install_source_best_effort(
        self,
        *,
        install_result: dict,
        package_filename: str,
        package_sha256: str,
        override: dict | None,
    ) -> str | None:
        """Best-effort record the install source in the lock file (design §7.3).

        Returns ``None`` on success or a short human-readable warning
        string on failure (to be surfaced as ``install_source_warning``
        per Req 9.6 / 10.8). This helper intentionally never raises: a
        broken install-source subsystem must not mask a successful
        plugin install.
        """
        try:
            from plugin.server.application.install_source import (
                get_install_source_manager,
            )
        except Exception as exc:
            return f"install_source_import_failed: {exc}"

        mgr = get_install_source_manager()
        if mgr is None:
            return "install_source_manager_unavailable"
        if mgr.is_degraded:
            return f"install_source_manager_degraded: {mgr.degrade_reason}"

        try:
            await asyncio.to_thread(
                _record_install_source_for_install_result,
                mgr,
                install_result,
                package_filename,
                package_sha256,
                override,
            )
            return None
        except Exception as exc:
            logger.warning(
                "record_install_source failed: err_type={}, err={}",
                type(exc).__name__,
                str(exc),
            )
            # Design §13 Fix 12: for BUILTIN_CHANNEL_LOCKED errors,
            # surface a specifically-shaped warning so ops can grep for
            # internal bug triggers.
            try:
                from plugin.server.application.install_source import InstallSourceError

                if isinstance(exc, InstallSourceError):
                    if exc.code == "BUILTIN_CHANNEL_LOCKED":
                        details = exc.details
                        return (
                            "internal_error: attempted to mutate builtin channel, "
                            f"plugin_id={details.get('plugin_id', '')} "
                            f"directory={details.get('directory_name', '')}"
                        )
                    return f"{exc.code}: {exc.message}"
            except Exception:
                pass  # classification failed; use generic fallback below
            return f"unexpected: {exc}"

    def _domain_error_from_exception(self, exc: Exception, *, action: str) -> ServerDomainError:
        if isinstance(exc, ServerDomainError):
            return exc
        if getattr(exc, "code", "") == "PLUGIN_EXEC_STATE_ROOT_COLLISION":
            status_code = 409
            code = "PLUGIN_EXEC_STATE_ROOT_COLLISION"
        elif package_error_code := _classify_package_error(exc):
            status_code = 400
            code = package_error_code
        elif isinstance(exc, FileNotFoundError):
            status_code = 404
            code = "PLUGIN_CLI_NOT_FOUND"
        elif isinstance(exc, FileExistsError):
            status_code = 409
            code = "PLUGIN_CLI_CONFLICT"
        elif isinstance(exc, ValueError):
            status_code = 400
            code = "PLUGIN_CLI_INVALID_REQUEST"
        else:
            status_code = 500
            code = "PLUGIN_CLI_INTERNAL_ERROR"

        logger.warning(
            "plugin cli action failed: action={}, err_type={}, err={}",
            action,
            type(exc).__name__,
            str(exc),
        )
        return ServerDomainError(
            code=code,
            message=str(exc),
            status_code=status_code,
            details={"action": action, "error_type": type(exc).__name__},
        )


def _plugin_directory_exists(plugins_root: Path, plugin_id: str) -> bool:
    """Whether any directory under ``plugins_root`` still holds ``plugin_id``.

    Used by the install rollback to decide whether restoring the plugin's
    autostart approval is safe. Staging cleanup runs with
    ``ignore_errors=True``, so a failed install can leave a runnable copy
    behind; handing that copy its approval back lets it start itself at the
    next boot without the user ever having run it.

    Fails closed, unlike the manifest reader: every uncertainty here — an
    unlistable root, an unreadable manifest next to a same-named directory —
    returns ``True`` and keeps the id gated. The cost of a false positive is
    one manual start; the cost of a false negative is third-party code running
    unapproved.
    """
    wanted = str(plugin_id or "").strip()
    if not wanted:
        return False
    try:
        entries = list(plugins_root.iterdir())
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.error(
            "cannot list {} to check for install remnants of {}: {}",
            plugins_root,
            wanted,
            exc,
        )
        return True
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name.startswith("."):
            # 暂存目录（.neko_staging_*）不是安装产物，注册表也不扫它们。
            continue
        manifest_id = _installed_manifest_plugin_id(entry)
        if manifest_id == wanted:
            return True
        if not manifest_id and entry.name == wanted:
            # manifest 读不出来的同名目录：说不清它是不是这个插件，按在算。
            return True
    return False


def _installed_manifest_plugin_id(target_dir: object) -> str:
    """Read ``[plugin].id`` out of an installed plugin's manifest.

    Returns "" when it cannot be read. Every caller here fails open — an
    unreadable manifest means the plugin autostarts the way it did before this
    gate existed, which is the safe direction.
    """
    if not target_dir:
        return ""
    # 用模块顶层那个 tomllib。这里本来写了一段 try/except 回落到 tomli，但这个模块
    # 顶层就是无条件 import tomllib 的——那段回落既到不了，又暗示了一套本模块并不
    # 具备的 3.10 兼容性（github-code-quality）。
    config_path = Path(str(target_dir)) / "plugin.toml"
    try:
        with open(config_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return ""
    plugin_table = data.get("plugin")
    if not isinstance(plugin_table, dict):
        return ""
    return str(plugin_table.get("id") or "").strip()


def _mark_new_install_awaiting_autostart(install_result: dict) -> list[str]:
    """Withhold autostart from a plugin the user has installed but never run.

    Call this only from the fresh-install path. "Is this plugin new?" is decided
    by the install plan, which computes it before touching disk: ``install_plan``
    only says ``"install"`` when nothing is installed under that id, and says
    ``reinstall`` or ``blocked`` otherwise.

    It deliberately does *not* consult ``state.plugins``. That snapshot answers a
    different question — "has a refresh seen this yet" — and a refresh that
    overlaps the install can register the freshly written directory before this
    runs, at which point the plugin looks pre-existing and skips the gate
    entirely (greptile); a stale registry produces the mirror error (codex).
    """
    from plugin.server.infrastructure.autostart_approvals import mark_autostart_pending

    unrecorded: list[str] = []
    installed = install_result.get("installed_plugins")
    if not isinstance(installed, list):
        return unrecorded
    for item in installed:
        if not isinstance(item, dict):
            continue
        target_dir = item.get("target_dir")
        # 注册表按 manifest 里声明的 id 记插件，而安装结果只带目录名
        # （InstalledPlugin.target_plugin_id 就是 target_dir.name）。仓库允许目录名
        # 和 plugin.id 不一致，登记错 id 等于这道闸对该插件完全不生效
        # （coderabbit）。所以直接去读装出来的那份 manifest。
        plugin_id = _installed_manifest_plugin_id(target_dir)
        if not plugin_id:
            plugin_id = str(item.get("target_plugin_id") or "").strip()
        if not plugin_id and target_dir:
            plugin_id = Path(str(target_dir)).name
        if not plugin_id:
            continue
        if not mark_autostart_pending(plugin_id):
            unrecorded.append(plugin_id)
    return unrecorded


def _record_install_source_for_install_result(
    mgr,
    install_result: dict,
    package_filename: str,
    package_sha256: str,
    override: dict | None,
) -> None:
    """Walk ``install_result["installed_plugins"]`` and call the appropriate
    ``record_*`` method on ``mgr`` for each one (design §7.3).

    Raises :class:`InstallSourceError` with code ``"UNSUPPORTED_OVERRIDE"``
    when the caller supplies an ``override`` whose ``channel`` is not one
    of the supported values. Other ``InstallSourceError`` codes (e.g.
    ``PATH_OUTSIDE_ROOTS``, ``BUILTIN_CHANNEL_LOCKED``) propagate from
    the manager.
    """
    from plugin.server.application.install_source import InstallSourceError

    installed_plugins = install_result.get("installed_plugins", [])
    package_id = str(install_result.get("package_id") or "")
    profile_dir = str(install_result.get("profile_dir") or "")
    for installed in installed_plugins:
        target_dir = Path(installed["target_dir"])
        if override is None:
            mgr.record_import(
                directory_path=target_dir,
                package_filename=package_filename,
                package_sha256=package_sha256,
                package_id=package_id,
                profile_dir=profile_dir,
            )
        elif override.get("channel") == "market":
            detail = override.get("market_detail", {})
            mgr.record_market(
                directory_path=target_dir,
                plugin_market_id=detail.get("plugin_market_id", ""),
                version=detail.get("version", ""),
                package_url=detail.get("package_url", ""),
                package_id=package_id,
                profile_dir=profile_dir,
            )
        else:
            raise InstallSourceError(
                "UNSUPPORTED_OVERRIDE",
                f"unsupported override channel={override.get('channel')}",
                details={"override": override},
            )
