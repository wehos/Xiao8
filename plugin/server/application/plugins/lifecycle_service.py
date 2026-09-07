from __future__ import annotations

import asyncio
import inspect
import math
import re
import time as time_module
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from collections.abc import Mapping
from functools import partial
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import HTTPException

from plugin._types.exceptions import PluginError, PluginLifecycleError
from plugin.core.host import PluginProcessHost
from plugin.core.registry import (
    _collect_plugin_python_requirements,
    _collect_plugin_python_requirement_paths,
    _check_plugin_dependency,
    _find_missing_python_requirements,
    _parse_plugin_dependencies,
    _resolve_plugin_id_conflict,
)
from plugin.core.entry_points import (
    describe_plugin_entry_directory_mismatch,
    normalize_plugin_entry_point,
)
from plugin.core.state import state
from plugin.logging_config import get_logger
from plugin.server.domain import IO_RUNTIME_ERRORS, RUNTIME_ERRORS
from plugin.server.domain.errors import ServerDomainError
from plugin.server.application.plugins.operation_lock import (
    bounded_operation_wait,
    PluginOperationBusy,
    serialized_plugin_operation,
)
from plugin.server.application.plugins.registry_service import (
    PluginRegistryService,
    config_overrides_packaged_entries,
)
from plugin.server.application.plugins.installation_transactions import (
    UninstallOwnershipError,
    UninstallPluginError,
    require_uninstall_ownership,
    retry_deferred_plugin_code_cleanup_sync,
    retry_deferred_profile_cleanup_sync,
    uninstall_plugin,
)
from plugin.server.application.plugins.metadata_scanner import (
    _DEFAULT_SCAN_TIMEOUT_SECONDS as _DEFAULT_METADATA_SCAN_TIMEOUT,
    _handler_key_belongs_to_plugin,
    IsolatedPluginMetadata,
    install_isolated_plugin_metadata,
    scan_plugin_metadata_isolated,
)
from plugin.server.infrastructure.packaged_metadata import read_packaged_metadata
from plugin.server.application.install_source import (
    InstallSourceError,
    get_install_source_manager,
)
from plugin.server.infrastructure.config_resolver import resolve_plugin_config_from_path
from plugin.server.infrastructure.runtime_overrides import (
    RuntimeOverridePersistenceError,
    get_runtime_auto_start_override,
    get_runtime_override,
    migrate_runtime_override,
    set_runtime_override,
)
from plugin.server.messaging.lifecycle_events import emit_lifecycle_event
from plugin.server.messaging.llm_tool_registry import (
    clear_plugin_tools as clear_plugin_llm_tools,
)
from plugin.settings import (
    BUILTIN_PLUGIN_CONFIG_ROOT,
    PLUGIN_CONFIG_ROOTS,
    PLUGIN_SHUTDOWN_TIMEOUT,
    PLUGIN_STARTUP_TIMEOUT,
    PLUGIN_SYNC_AUTO_START_ON_TOGGLE,
)
from plugin.server.infrastructure.autostart_approvals import clear_autostart_pending
from plugin.utils import parse_bool_config

logger = get_logger("server.application.plugins.lifecycle")
_PLUGIN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_PLUGIN_STARTUP_TIMEOUT_MAX = 300.0
# 被整轮预算压缩后，一步至少还能拿到这么久。
#
# 没有下界的话，预算见底时算出来的是 0 或负数，那等于"直接判这个插件启动失败"
# 而不是"抓紧试一次"——而走到启动阶段的插件都是我们刚亲手停掉的，判它失败就是
# 把它留在停止状态；关停侧则会退化成直接杀进程而不是先请它自己收尾。
#
# 启动和关停各有各的下界。它们曾经共用一个数，而把启动的下界抬上去会连带把关停
# 的最坏墙钟一起抬高——那是两件无关的事，一次改动不该同时动到。
#
# 启动 3.0 而不是 1.0：下界的意思是"至少给它一次真正的尝试"，而 1 秒买不到一次
# 尝试。光是起子进程加导入框架，本机实测就要 0.74s（其中 0.38s 是 fastapi），插件
# 自己的导入还没开始算。给一个必然超时的窗口，等于把健康插件在超支的那一轮记成
# 启动失败。代价只落在已经超支的病态路径上：一轮 8 个插件最坏多花 16 秒，而正常
# 一轮根本走不到这里。
_MIN_CLAMPED_START_TIMEOUT = 3.0
# 关停保持 1.0：这一步不起进程，它只是给插件一个说"我收好了"的机会。
# 清理远端工具表这一步在没有预算约束时愿意花的时间（它自己内部还有更细的超时）。
_CLEAR_TOOLS_BUDGET_SECONDS = 2.0
# 预算见底时仍然留给它的一小段时间。不是"启动尝试"那种下界（那是
# _MIN_CLAMPED_START_TIMEOUT），
# 只是让这次幂等的远端清除**发得出去**——跳过的代价是永久的幽灵工具，而这
# 一小段的代价只在 main_server 真的卡住时才付。
_MIN_TOOL_CLEANUP_TIMEOUT = 0.25


def _resolve_python_requirements(
    conf: Any,
    config_path: Path,
    plugin_id: str,
) -> tuple[list[str], list[Path], list[str]]:
    """Read a plugin's declared Python deps and check them against its vendor dir.

    三步全是磁盘 I/O：读 pyproject.toml、列出 vendor/ 下每个 dist-info、逐个读它们
    的 METADATA。合成一个函数只是为了让调用方能一次 to_thread 掉，见 start_plugin。
    """
    requirements = _collect_plugin_python_requirements(conf, config_path, logger, plugin_id)
    paths = _collect_plugin_python_requirement_paths(config_path)
    missing = _find_missing_python_requirements(requirements, search_paths=paths)
    return requirements, paths, missing


_MIN_CLAMPED_STOP_TIMEOUT = 1.0


def _read_packaged_isolated_metadata(
    config_path: Path,
    plugin_id: str,
    *,
    conf: object = None,
    pdata: object = None,
) -> IsolatedPluginMetadata | None:
    """Reuse the packaging-time metadata instead of importing the plugin again.

    Starting a plugin already imports it once — inside the plugin process. The
    metadata worker was a *second* import of the same code, for a result the
    package already carries (codex). When the package has valid metadata, read
    it; otherwise fall back to the worker so a hand-dropped or dev-mode plugin
    still gets its entries.

    ⚠️ Handler keys embed the plugin id (``"<pid>.<entry>"``), and the id a
    plugin runs under is not always the one its manifest declares — an id
    conflict renames it. ``install_isolated_plugin_metadata`` drops every key
    that does not belong to the runtime id, so handing it packaged keys minted
    under a different id registers *nothing*: the plugin starts, reports
    success, and exposes no entries at all (coderabbit). Fall back to the
    worker in that case, since it mints keys under the id we pass it.

    An empty ``handlers`` mapping is an answer, not a gap: a background-only
    plugin registers no entries, and schema v3 always writes the key. Treating
    empty as "no metadata" sent exactly those plugins back through the worker —
    one import for the scan, one for the host, so any module-level side effect
    (writing state, sending a notification, launching a helper) happened twice
    (codex). There is no older package to protect: the version gate above only
    accepts v3, and v1/v2 were never released.

    Returns ``None`` when there is no usable metadata at all.
    """
    packaged = read_packaged_metadata(Path(config_path).parent)
    if packaged is None:
        return None
    if config_overrides_packaged_entries(conf, pdata, packaged):
        # 打包期读的是暂存目录那份 plugin.toml，看不到用户的运行时配置/激活
        # profile。生效配置一旦改过 entries 表，包里那份 handler 就不是这台机器上
        # 该注册的那一套了（codex）。这种插件回落到真扫一次。
        return None
    if not packaged.built_in_this_environment:
        # 这一份是别的机器上 import 出来的结果。插件完全可以按 sys.platform 或
        # Python 版本条件注册入口，那样的话包里那套 handler 描述的是打包机的能力
        # 集，不是这台机器的（codex）。展示用的 entries 可以将就，但注册进
        # state.event_handlers 的这份是权威能力集——它错了，模型会去调一个这台机器
        # 上根本不存在的入口。回落到真扫一次，代价就是本 PR 之前的原样。
        logger.info(
            "packaged metadata was produced in a different environment; "
            "rescanning so the registered entries match this machine: "
            "plugin_id={}",
            plugin_id,
        )
        return None
    if not all(
        _handler_key_belongs_to_plugin(key, plugin_id) for key in packaged.handlers
    ):
        logger.info(
            "packaged handler keys were minted under a different plugin id; "
            "rescanning so they match the runtime id: plugin_id={}",
            plugin_id,
        )
        return None
    return IsolatedPluginMetadata(
        entries_preview=list(packaged.entries),
        handlers=dict(packaged.handlers),
        entry_methods=dict(packaged.entry_methods),
    )


def _clamp_step_timeout(
    configured: float,
    budget: float | None,
    *,
    floor: float,
) -> float:
    """Fit one step of a stop or a start inside what is left of a round budget.

    Never *widens*: a plugin that declared a 0.5 s timeout of its own keeps it
    however generous the budget is, and however low the floor is. The floor
    raises a squeezed budget, it does not raise the configured value.

    One helper for both phases on purpose. The stop side had no floor at all,
    so a spent budget handed ``shutdown_timeout≈0`` down and every remaining
    plugin was killed outright instead of being asked to shut down.
    """
    if budget is None:
        return configured
    return min(configured, max(floor, budget))


def _remaining_step_budget(deadline: float | None) -> float | None:
    """Seconds left before ``deadline``, or ``None`` when the step is unbounded.

    A start is several sequential expensive steps, not one. Handing the whole
    call a single duration bounds only the step it is applied to and lets every
    other step run past the round's wall clock; recomputing against an absolute
    deadline is what makes the budget cover the call rather than one line of it
    (CodeRabbit).
    """
    return None if deadline is None else deadline - time_module.monotonic()
plugin_registry_service = PluginRegistryService()
def _persist_user_runtime_intent(
    plugin_id: str,
    enabled: bool,
    *,
    previous_plugin_ids: tuple[str, ...] = (),
    runtime_state_changed: bool = False,
) -> None:
    try:
        auto_start = enabled if PLUGIN_SYNC_AUTO_START_ON_TOGGLE else None
        if previous_plugin_ids:
            migrate_runtime_override(
                previous_plugin_ids,
                plugin_id,
                enabled,
                auto_start=auto_start,
            )
        else:
            set_runtime_override(
                plugin_id,
                enabled,
                auto_start=auto_start,
            )
    except RuntimeOverridePersistenceError as exc:
        raise ServerDomainError(
            code="PLUGIN_RUNTIME_PREFERENCE_PERSIST_FAILED",
            message="PLUGIN_RUNTIME_PREFERENCE_PERSIST_FAILED",
            status_code=500,
            details={
                "plugin_id": plugin_id,
                "enabled": enabled,
                "auto_start": (
                    enabled if PLUGIN_SYNC_AUTO_START_ON_TOGGLE else None
                ),
                "error_type": type(exc).__name__,
                "runtime_state_changed": runtime_state_changed,
            },
            log_level="error",
        ) from exc

    if enabled:
        # 清在偏好写盘**之后**。写盘失败会抛上去、只被报成 partial_success，而这台
        # 机器上就没有用户 override 了——重启后注册表回落到 manifest 默认值
        # （enabled/auto_start 都是 true）。先清的话，等于凭一个没落地的意图永久发出
        # 了自启动批准（greptile）。
        #
        # 这是 autostart_approvals 那条"一切失败都朝着照常自启"原则的例外，而且不
        # 冲突：那条原则说的是**读**不出记录时别把用户现有的自启动关掉；这里是**写**，
        # 而待批准记录只存在于新装插件上——它们本来就没自启过，写失败时不批准，
        # 回到的正是安装前的状态。
        persisted = clear_autostart_pending(plugin_id)
        # 改名前的那些 id 一起清。安装时按 manifest 声明的 id 记待批准，而插件可能
        # 因为 id 冲突以另一个运行时 id 注册；只清运行时 id 的话，等冲突消失、它又
        # 用回声明 id 时，那条残留记录会继续挡着它自启（coderabbit）。
        for previous_plugin_id in previous_plugin_ids:
            persisted = clear_autostart_pending(previous_plugin_id) and persisted
        if not persisted:
            # 批准没落地就不能报成"偏好已保存"。运行时偏好那一半确实写成了，但插件
            # 仍然留在待批准集合里，重启后自启动筛选会再一次静默把它拦下来，而用户
            # 手上没有任何线索（greptile）。走和偏好写失败同一条上报通道：调用方把它
            # 降级成 partial_success，而不是让这次启动失败。
            raise ServerDomainError(
                code="PLUGIN_AUTOSTART_APPROVAL_PERSIST_FAILED",
                message="PLUGIN_AUTOSTART_APPROVAL_PERSIST_FAILED",
                status_code=500,
                details={
                    "plugin_id": plugin_id,
                    "error_type": "AutostartApprovalPersistenceError",
                    "runtime_state_changed": runtime_state_changed,
                },
                log_level="error",
            )


def _mark_preference_persistence_failure(
    response: dict[str, object],
    error: ServerDomainError,
) -> None:
    details = error.details if isinstance(error.details, dict) else {}
    response["partial_success"] = True
    response["preference_persisted"] = False
    response["preference_error"] = {
        "code": error.code,
        "error_type": str(details.get("error_type", "RuntimeOverridePersistenceError")),
    }
    response["runtime_state_changed"] = bool(details.get("runtime_state_changed", False))


async def _persist_changed_runtime_intent(
    response: dict[str, object],
    plugin_id: str,
    enabled: bool,
    *,
    previous_plugin_ids: tuple[str, ...] = (),
) -> None:
    try:
        await asyncio.to_thread(
            _persist_user_runtime_intent,
            plugin_id,
            enabled,
            previous_plugin_ids=previous_plugin_ids,
            runtime_state_changed=True,
        )
        response["preference_persisted"] = True
    except ServerDomainError as exc:
        logger.error(
            "plugin runtime state changed but user preference could not be persisted: plugin_id={}, enabled={}, err_type={}",
            plugin_id,
            enabled,
            type(exc).__name__,
        )
        _mark_preference_persistence_failure(response, exc)


@runtime_checkable
class PluginHostContract(Protocol):
    async def start(
        self,
        message_target_queue: object,
        startup_timeout: float | None = None,
        startup_failure: str = "warn",
    ) -> object: ...

    async def shutdown(self, timeout: float = PLUGIN_SHUTDOWN_TIMEOUT) -> None: ...

    def is_alive(self) -> bool: ...


@dataclass(slots=True, frozen=True)
class _ReloadOutcome:
    plugin_id: str
    success: bool
    error: str | None = None


def _normalize_mapping(raw: Mapping[object, object], *, context: str) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ServerDomainError(
                code="INVALID_DATA_SHAPE",
                message=f"{context} contains non-string key",
                status_code=500,
                details={"key_type": type(key).__name__},
            )
        normalized[key] = value
    return normalized


def _detail_to_message(detail: object, *, default_message: str) -> str:
    if isinstance(detail, str) and detail:
        return detail
    return default_message


def _to_domain_error(
    *,
    code: str,
    message: str,
    status_code: int,
    plugin_id: str | None,
    error_type: str,
) -> ServerDomainError:
    return ServerDomainError(
        code=code,
        message=message,
        status_code=status_code,
        details={
            "plugin_id": plugin_id or "",
            "error_type": error_type,
        },
    )


def _get_plugin_host_sync(plugin_id: str) -> object | None:
    with state.acquire_plugin_hosts_read_lock():
        return state.plugin_hosts.get(plugin_id)


def _pop_plugin_host_sync(plugin_id: str) -> object | None:
    with state.acquire_plugin_hosts_write_lock():
        popped = state.plugin_hosts.pop(plugin_id, None)
    if popped is not None:
        state.invalidate_snapshot_cache("hosts")
    return popped


def _plugin_is_running_sync(plugin_id: str) -> bool:
    with state.acquire_plugin_hosts_read_lock():
        return plugin_id in state.plugin_hosts


def _list_running_plugin_ids_sync() -> list[str]:
    with state.acquire_plugin_hosts_read_lock():
        return [plugin_id for plugin_id in state.plugin_hosts.keys()]


def _remove_event_handlers_sync(plugin_id: str) -> None:
    removed_any = False
    with state.acquire_event_handlers_write_lock():
        target_prefix_dot = f"{plugin_id}."
        target_prefix_colon = f"{plugin_id}:"
        keys_to_remove = [
            key
            for key in list(state.event_handlers.keys())
            if key.startswith(target_prefix_dot) or key.startswith(target_prefix_colon)
        ]
        for key in keys_to_remove:
            del state.event_handlers[key]
            removed_any = True
    if removed_any:
        state.invalidate_snapshot_cache("handlers")


def _get_plugin_meta_sync(plugin_id: str) -> dict[str, object] | None:
    with state.acquire_plugins_read_lock():
        raw_meta = state.plugins.get(plugin_id)
    if not isinstance(raw_meta, dict):
        return None

    normalized: dict[str, object] = {}
    for key, value in raw_meta.items():
        if isinstance(key, str):
            normalized[key] = value
    return normalized


def _set_plugin_runtime_enabled_sync(plugin_id: str, enabled: bool) -> None:
    with state.acquire_plugins_write_lock():
        raw_meta = state.plugins.get(plugin_id)
        if not isinstance(raw_meta, dict):
            return
        raw_meta["runtime_enabled"] = enabled
        state.plugins[plugin_id] = raw_meta
    state.invalidate_snapshot_cache("plugins")


def _set_plugin_runtime_metadata_sync(
    plugin_id: str,
    *,
    runtime_enabled: bool,
    runtime_auto_start: bool,
    entries_preview: list[dict[str, object]] | None = None,
    startup_state: str | None = None,
    startup_error: str | None = None,
) -> None:
    with state.acquire_plugins_write_lock():
        raw_meta = state.plugins.get(plugin_id)
        if not isinstance(raw_meta, dict):
            return
        raw_meta["runtime_enabled"] = runtime_enabled
        raw_meta["runtime_auto_start"] = runtime_auto_start
        if entries_preview is not None:
            raw_meta["entries_preview"] = entries_preview
        if startup_state is not None:
            raw_meta["runtime_startup_state"] = startup_state
        else:
            raw_meta.pop("runtime_startup_state", None)
        if startup_error:
            raw_meta["runtime_startup_error"] = startup_error
        else:
            raw_meta.pop("runtime_startup_error", None)
        raw_meta.pop("runtime_load_state", None)
        raw_meta.pop("runtime_load_error_type", None)
        raw_meta.pop("runtime_load_error_message", None)
        raw_meta.pop("runtime_load_error_phase", None)
        raw_meta.pop("runtime_load_error_time", None)
        raw_meta.pop("runtime_source_missing", None)
        state.plugins[plugin_id] = raw_meta
    state.invalidate_snapshot_cache("plugins")


def _get_plugin_config_path(plugin_id: str) -> Path | None:
    normalized_plugin_id = plugin_id.strip()
    if not _PLUGIN_ID_PATTERN.fullmatch(normalized_plugin_id):
        return None

    for root in PLUGIN_CONFIG_ROOTS:
        resolved_root = root.resolve()
        config_file = (resolved_root / normalized_plugin_id / "plugin.toml").resolve()
        if resolved_root not in config_file.parents:
            continue
        if config_file.exists():
            return config_file
    return None


def _resolve_plugin_config_path_sync(
    plugin_id: str,
    plugin_meta: dict[str, object] | None,
) -> Path | None:
    config_path = _resolve_registered_config_path_sync(plugin_meta)
    if config_path is None:
        config_path = _get_plugin_config_path(plugin_id)
    if config_path is None:
        return None
    try:
        return config_path.resolve()
    except Exception:
        return config_path


def _register_or_replace_host_sync(plugin_id: str, host: PluginHostContract) -> int:
    with state.acquire_plugin_hosts_write_lock():
        if plugin_id in state.plugin_hosts:
            existing_host = state.plugin_hosts.get(plugin_id)
            if existing_host is not None and existing_host is not host:
                logger.warning("Plugin {} already exists in plugin_hosts, replacing host", plugin_id)
        state.plugin_hosts[plugin_id] = host
        current_count = len(state.plugin_hosts)
    state.invalidate_snapshot_cache("hosts")
    return current_count


def _read_plugin_config_sync(config_path: Path) -> dict[str, object]:
    with config_path.open("rb") as file_obj:
        raw_conf = tomllib.load(file_obj)
    if not isinstance(raw_conf, Mapping):
        raise ValueError("plugin config root must be an object")
    return _normalize_mapping(raw_conf, context=f"plugin_config[{config_path}]")


def _resolve_registered_config_path_sync(plugin_meta: dict[str, object] | None) -> Path | None:
    if not isinstance(plugin_meta, dict):
        return None

    config_path_obj = plugin_meta.get("config_path")
    if not isinstance(config_path_obj, str) or not config_path_obj:
        return None

    try:
        return Path(config_path_obj).resolve()
    except Exception:
        return Path(config_path_obj)


def _registered_load_failure_error(plugin_id: str, plugin_meta: dict[str, object] | None) -> ServerDomainError | None:
    if not isinstance(plugin_meta, dict) or plugin_meta.get("runtime_load_state") != "failed":
        return None

    error_type_obj = plugin_meta.get("runtime_load_error_type")
    error_message_obj = plugin_meta.get("runtime_load_error_message")
    error_phase_obj = plugin_meta.get("runtime_load_error_phase")
    error_type = str(error_type_obj or "PluginLoadFailed")
    if error_type not in {"PluginEntryDirectoryMismatch", "SyntaxError"}:
        return None

    error_message = str(error_message_obj or "Plugin failed to load during registry refresh")
    error_phase = str(error_phase_obj or "unknown")
    code = "PLUGIN_ENTRY_DIRECTORY_MISMATCH" if error_type == "PluginEntryDirectoryMismatch" else "PLUGIN_LOAD_FAILED"
    return _to_domain_error(
        code=code,
        message=(
            f"Plugin '{plugin_id}' cannot be started because its entry failed during "
            f"registry phase '{error_phase}': {error_type}: {error_message}"
        ),
        status_code=400,
        plugin_id=plugin_id,
        error_type=error_type,
    )


async def _cleanup_started_host(plugin_id: str, host: PluginHostContract) -> None:
    removed = await asyncio.to_thread(_pop_plugin_host_sync, plugin_id)
    target_host = host
    if isinstance(removed, PluginHostContract):
        target_host = removed

    try:
        await target_host.shutdown(timeout=1.0)
    except PluginError as exc:
        logger.warning(
            "cleanup shutdown failed with PluginError: plugin_id={}, err_type={}, err={}",
            plugin_id,
            type(exc).__name__,
            str(exc),
        )
    except RUNTIME_ERRORS as exc:
        logger.warning(
            "cleanup shutdown failed: plugin_id={}, err_type={}, err={}",
            plugin_id,
            type(exc).__name__,
            str(exc),
        )


def _emit_lifecycle_event(
    *,
    event_type: str,
    plugin_id: str | None = None,
    data: Mapping[str, object] | None = None,
) -> None:
    event: dict[str, object] = {
        "type": event_type,
    }
    if plugin_id is not None:
        event["plugin_id"] = plugin_id
    if data is not None:
        event["data"] = dict(data)
    emit_lifecycle_event(event)


def _normalize_runtime_timeout(
    raw_value: object,
    *,
    plugin_id: str,
    setting_label: str = "[plugin_runtime].timeout",
) -> float:
    message = (
        f"Plugin '{plugin_id}' {setting_label} must be a number "
        f"in range 0 < timeout <= {_PLUGIN_STARTUP_TIMEOUT_MAX:g}"
    )
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise _to_domain_error(
            code="INVALID_PLUGIN_CONFIG",
            message=message,
            status_code=400,
            plugin_id=plugin_id,
            error_type="InvalidStartupTimeout",
        )
    timeout = float(raw_value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > _PLUGIN_STARTUP_TIMEOUT_MAX:
        raise _to_domain_error(
            code="INVALID_PLUGIN_CONFIG",
            message=message,
            status_code=400,
            plugin_id=plugin_id,
            error_type="InvalidStartupTimeout",
        )
    return timeout


def _normalize_startup_failure_policy(raw_value: object, *, plugin_id: str) -> str:
    if raw_value is None:
        return "warn"
    policy = str(raw_value).strip().lower()
    if policy in {"warn", "fail", "ignore"}:
        return policy
    raise _to_domain_error(
        code="INVALID_PLUGIN_CONFIG",
        message=f"Plugin '{plugin_id}' [plugin_runtime].startup_failure must be one of: warn, fail, ignore",
        status_code=400,
        plugin_id=plugin_id,
        error_type="InvalidStartupFailurePolicy",
    )


def _start_method_accepts_kwarg(start_method: object, name: str) -> bool:
    try:
        signature = inspect.signature(start_method)
    except (TypeError, ValueError):
        return False
    return (
        name in signature.parameters
        or any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
    )


def _extract_startup_error(start_result: object) -> str | None:
    if not isinstance(start_result, Mapping):
        return None
    raw_error = start_result.get("startup_error")
    if isinstance(raw_error, str) and raw_error:
        return raw_error
    data = start_result.get("data")
    if isinstance(data, Mapping):
        raw_error = data.get("startup_error")
        if isinstance(raw_error, str) and raw_error:
            return raw_error
    return None


def _is_startup_timeout_error(exc: PluginLifecycleError) -> bool:
    reason = str(getattr(exc, "reason", "") or "").lower()
    return getattr(exc, "event_type", None) == "startup" and bool(
        re.fullmatch(r"startup timed out after \d+(?:\.\d+)?(?:e[+-]?\d+)?s", reason)
    )


def _startup_timeout_domain_error(
    *,
    plugin_id: str,
    startup_timeout: float,
) -> ServerDomainError:
    return _to_domain_error(
        code="PLUGIN_START_TIMEOUT",
        message=f"Plugin '{plugin_id}' startup timed out after {startup_timeout}s",
        status_code=504,
        plugin_id=plugin_id,
        error_type="StartupTimeout",
    )


async def _start_host_with_timeout(
    *,
    plugin_id: str,
    host_obj: PluginHostContract,
    message_target_queue: object,
    startup_timeout: float | None,
    startup_failure: str,
) -> object:
    start_method = host_obj.start
    kwargs: dict[str, object] = {"message_target_queue": message_target_queue}
    if _start_method_accepts_kwarg(start_method, "startup_failure"):
        kwargs["startup_failure"] = startup_failure
    if startup_timeout is not None and _start_method_accepts_kwarg(start_method, "startup_timeout"):
        kwargs["startup_timeout"] = startup_timeout
        try:
            return await start_method(**kwargs)
        except PluginLifecycleError as exc:
            if _is_startup_timeout_error(exc):
                raise _startup_timeout_domain_error(
                    plugin_id=plugin_id,
                    startup_timeout=startup_timeout,
                ) from exc
            raise

    start_coro = start_method(**kwargs)
    if startup_timeout is None:
        return await start_coro

    try:
        return await asyncio.wait_for(start_coro, timeout=startup_timeout)
    except asyncio.TimeoutError as exc:
        raise _startup_timeout_domain_error(
            plugin_id=plugin_id,
            startup_timeout=startup_timeout,
        ) from exc
    except PluginLifecycleError as exc:
        if _is_startup_timeout_error(exc):
            raise _startup_timeout_domain_error(
                plugin_id=plugin_id,
                startup_timeout=startup_timeout,
            ) from exc
        raise


# reload-all 停止阶段的墙钟预算。
#
# 每个插件的 stop 都要独立抢一次跨进程锁（见下面 reload_all_plugins 里的说明：
# 这一段无法真并行），所以耗时随插件数线性增长，而前端只等 30s。超预算就停下，
# 已经停掉的照常汇报，剩下的留在原地——比让整个请求超时、而操作又在后台继续
# 落地要好。
# Env: NEKO_PLUGIN_RELOAD_ALL_BUDGET
from plugin.server.application.plugins._env_budgets import env_seconds

_RELOAD_ALL_BUDGET_SECONDS = env_seconds("NEKO_PLUGIN_RELOAD_ALL_BUDGET", 20.0)


class PluginLifecycleService:
    @serialized_plugin_operation
    async def start_plugin(
        self,
        plugin_id: str,
        restore_state: bool = False,
        *,
        refresh_registry: bool = True,
        persist_user_intent: bool = False,
        start_deadline: float | None = None,
    ) -> dict[str, object]:
        start_time = time_module.perf_counter()
        original_plugin_id = plugin_id
        current_plugin_id = plugin_id
        resolved_plugin_ids = [plugin_id]

        existing_host_obj = await asyncio.to_thread(_get_plugin_host_sync, current_plugin_id)
        if isinstance(existing_host_obj, PluginHostContract):
            if existing_host_obj.is_alive():
                if persist_user_intent:
                    await asyncio.to_thread(
                        _persist_user_runtime_intent,
                        current_plugin_id,
                        True,
                        runtime_state_changed=False,
                    )
                _emit_lifecycle_event(event_type="plugin_start_skipped", plugin_id=current_plugin_id)
                return {
                    "success": True,
                    "plugin_id": current_plugin_id,
                    "message": "Plugin is already running",
                }
            # Stale host (process dead) — remove so re-start can proceed
            await asyncio.to_thread(_pop_plugin_host_sync, current_plugin_id)
            logger.info("removed stale host for plugin_id={} (process no longer alive)", current_plugin_id)

        if state.is_plugin_frozen(current_plugin_id) and not restore_state:
            raise _to_domain_error(
                code="PLUGIN_FROZEN",
                message=f"Plugin '{current_plugin_id}' is frozen. Use unfreeze_plugin to restore it.",
                status_code=409,
                plugin_id=current_plugin_id,
                error_type="PluginFrozen",
            )

        if refresh_registry:
            try:
                refresh_payload = await plugin_registry_service.refresh_plugin(current_plugin_id)
                refreshed_plugin_id = refresh_payload.get("plugin_id")
                if isinstance(refreshed_plugin_id, str) and refreshed_plugin_id:
                    if refreshed_plugin_id != current_plugin_id:
                        resolved_plugin_ids.append(refreshed_plugin_id)
                    current_plugin_id = refreshed_plugin_id
            except ServerDomainError as exc:
                if exc.code == "PLUGIN_CONFIG_NOT_FOUND":
                    logger.warning(
                        "registry refresh skipped for plugin_id={} because config lookup disagreed with lifecycle path resolution",
                        current_plugin_id,
                    )
                else:
                    raise _to_domain_error(
                        code=exc.code,
                        message=exc.message,
                        status_code=exc.status_code,
                        plugin_id=current_plugin_id,
                        error_type=str(exc.details.get("error_type", "RegistryRefreshFailed")) if isinstance(exc.details, dict) else "RegistryRefreshFailed",
                    ) from exc

        registered_meta = await asyncio.to_thread(_get_plugin_meta_sync, current_plugin_id)
        config_path = await asyncio.to_thread(_resolve_registered_config_path_sync, registered_meta)
        if config_path is None:
            config_path = _get_plugin_config_path(current_plugin_id)
        if config_path is None:
            raise _to_domain_error(
                code="PLUGIN_CONFIG_NOT_FOUND",
                message=f"Plugin '{current_plugin_id}' configuration not found",
                status_code=404,
                plugin_id=current_plugin_id,
                error_type="ConfigNotFound",
            )
        registered_load_error = _registered_load_failure_error(current_plugin_id, registered_meta)
        if registered_load_error is not None:
            raise registered_load_error

        host_obj: PluginHostContract | None = None
        registered_plugin_id: str | None = None

        try:
            conf = await asyncio.to_thread(_read_plugin_config_sync, config_path)
            logger.info(
                "start_plugin config loaded: plugin_id={}, elapsed={:.3f}s",
                current_plugin_id,
                time_module.perf_counter() - start_time,
            )

            try:
                resolved_conf = await asyncio.to_thread(
                    resolve_plugin_config_from_path,
                    str(current_plugin_id),
                    config_path=config_path,
                    base_config=conf,
                    include_effective_config=True,
                    validate_schema=True,
                )
                warnings_obj = resolved_conf.get("warnings")
                if isinstance(warnings_obj, list):
                    for warning in warnings_obj:
                        if isinstance(warning, Mapping):
                            logger.warning(
                                "Plugin config warning [{}] field={} msg={}",
                                warning.get("code"),
                                warning.get("field"),
                                warning.get("message"),
                            )
                conf = resolved_conf.get("effective_config")
            except HTTPException as exc:
                raise _to_domain_error(
                    code="PLUGIN_CONFIG_PROFILE_FAILED",
                    message=_detail_to_message(exc.detail, default_message="Failed to resolve plugin config"),
                    status_code=exc.status_code,
                    plugin_id=current_plugin_id,
                    error_type="HTTPException",
                ) from exc
            except IO_RUNTIME_ERRORS as exc:
                logger.warning(
                    "resolve plugin config failed: plugin_id={}, err_type={}, err={}",
                    current_plugin_id,
                    type(exc).__name__,
                    str(exc),
                )
            if not isinstance(conf, Mapping):
                raise _to_domain_error(
                    code="INVALID_PLUGIN_CONFIG",
                    message=f"Plugin '{current_plugin_id}' config is invalid after profile overlay",
                    status_code=500,
                    plugin_id=current_plugin_id,
                    error_type="InvalidConfigAfterProfile",
                )
            conf = _normalize_mapping(conf, context=f"plugin_config[{current_plugin_id}]")

            plugin_obj = conf.get("plugin")
            if not isinstance(plugin_obj, Mapping):
                raise _to_domain_error(
                    code="INVALID_PLUGIN_CONFIG",
                    message=f"Plugin '{current_plugin_id}' has invalid [plugin] section",
                    status_code=400,
                    plugin_id=current_plugin_id,
                    error_type="InvalidPluginSection",
                )
            pdata = _normalize_mapping(plugin_obj, context=f"plugin_config[{current_plugin_id}].plugin")

            runtime_obj = conf.get("plugin_runtime")
            enabled_value = True
            auto_start_value = True
            startup_timeout_value: float | None = _normalize_runtime_timeout(
                PLUGIN_STARTUP_TIMEOUT,
                plugin_id=current_plugin_id,
                setting_label="PLUGIN_STARTUP_TIMEOUT",
            )
            startup_failure_policy = "warn"
            if isinstance(runtime_obj, Mapping):
                runtime_cfg = _normalize_mapping(runtime_obj, context=f"plugin_config[{current_plugin_id}].plugin_runtime")
                enabled_value = parse_bool_config(runtime_cfg.get("enabled"), default=True)
                auto_start_value = parse_bool_config(runtime_cfg.get("auto_start"), default=True)
                if "timeout" in runtime_cfg:
                    startup_timeout_value = _normalize_runtime_timeout(
                        runtime_cfg.get("timeout"),
                        plugin_id=current_plugin_id,
                    )
                if "startup_failure" in runtime_cfg:
                    startup_failure_policy = _normalize_startup_failure_policy(
                        runtime_cfg.get("startup_failure"),
                        plugin_id=current_plugin_id,
                    )
            enabled_override = await asyncio.to_thread(
                get_runtime_override,
                current_plugin_id,
            )
            if enabled_override is not None:
                enabled_value = enabled_override
            auto_start_override = await asyncio.to_thread(
                get_runtime_auto_start_override,
                current_plugin_id,
            )
            if auto_start_override is not None:
                auto_start_value = auto_start_override
            if persist_user_intent:
                # An explicit start request is the new enabled intent. Apply it
                # in-memory for this attempt, but do not persist until startup
                # succeeds; otherwise validation/start failures become durable
                # auto-start preferences.
                enabled_value = True
                if PLUGIN_SYNC_AUTO_START_ON_TOGGLE:
                    auto_start_value = True
            if not enabled_value:
                raise _to_domain_error(
                    code="PLUGIN_DISABLED",
                    message=f"Plugin '{current_plugin_id}' is disabled by plugin_runtime.enabled and cannot be started",
                    status_code=400,
                    plugin_id=current_plugin_id,
                    error_type="PluginDisabled",
                )

            entry_obj = pdata.get("entry")
            if not isinstance(entry_obj, str) or ":" not in entry_obj:
                raise _to_domain_error(
                    code="INVALID_PLUGIN_ENTRY",
                    message=f"Invalid entry point for plugin '{current_plugin_id}'",
                    status_code=400,
                    plugin_id=current_plugin_id,
                    error_type="InvalidEntryPoint",
                )
            entry = normalize_plugin_entry_point(
                entry_obj,
                config_path=config_path,
                builtin_plugin_root=BUILTIN_PLUGIN_CONFIG_ROOT,
            )
            entry_mismatch = describe_plugin_entry_directory_mismatch(entry, config_path=config_path)
            if entry_mismatch:
                raise _to_domain_error(
                    code="PLUGIN_ENTRY_DIRECTORY_MISMATCH",
                    message=entry_mismatch,
                    status_code=400,
                    plugin_id=current_plugin_id,
                    error_type="PluginEntryDirectoryMismatch",
                )

            resolved_id = _resolve_plugin_id_conflict(
                current_plugin_id,
                logger,
                config_path=config_path,
                entry_point=entry,
                plugin_data=pdata,
                purpose="load",
            )
            if resolved_id is None:
                raise _to_domain_error(
                    code="PLUGIN_ALREADY_LOADED",
                    message=f"Plugin '{current_plugin_id}' is already loaded (duplicate detected)",
                    status_code=409,
                    plugin_id=current_plugin_id,
                    error_type="DuplicatePlugin",
                )
            current_plugin_id = resolved_id
            # 这一步是真正的磁盘 I/O，而且原本直接跑在事件循环线程上：一个 200
            # 个分发包的 vendor 目录冷读实测 0.31s、600 个 0.97s（Windows 本机；
            # 热读分别是 45ms / 184ms），整个服务器在这期间不响应任何请求
            # （Greptile）。挪进线程——它周围每一步本来就是这么做的（建 host、
            # 元数据扫描、运行时元数据落盘）。
            #
            # 不给它套超时。这是一道**前置闸门**，不是可以缩短的步骤：超时之后
            # 只剩两条路，蒙着头启动（缺依赖的进程起来就死，代价是一次完整的子
            # 进程 spawn，更贵），或者判它依赖缺失（把好插件误报成硬失败，最坏）。
            # 它花掉的时间本来就落在预算里——_remaining_step_budget 在它**之后**
            # 才取，所以后面每一步的上限已经被它扣减过了。
            (
                python_requirements,
                python_requirement_paths,
                unsatisfied_python_requirements,
            ) = await asyncio.to_thread(
                _resolve_python_requirements,
                conf,
                config_path,
                current_plugin_id,
            )
            if unsatisfied_python_requirements:
                raise _to_domain_error(
                    code="PLUGIN_PYTHON_DEPENDENCIES_MISSING",
                    message=(
                        f"Plugin '{current_plugin_id}' has unsatisfied Python dependencies: "
                        f"{unsatisfied_python_requirements}. Install compatible packages into the plugin vendor/ directory."
                    ),
                    status_code=400,
                    plugin_id=current_plugin_id,
                    error_type="MissingPythonDependencies",
                )

            _emit_lifecycle_event(event_type="plugin_start_requested", plugin_id=current_plugin_id)
            created_host = await asyncio.to_thread(
                PluginProcessHost,
                plugin_id=current_plugin_id,
                entry_point=entry,
                config_path=config_path,
            )
            if not isinstance(created_host, PluginHostContract):
                raise _to_domain_error(
                    code="INVALID_HOST_OBJECT",
                    message=f"Plugin '{current_plugin_id}' host object is invalid",
                    status_code=500,
                    plugin_id=current_plugin_id,
                    error_type=type(created_host).__name__,
                )
            host_obj = created_host

            dependencies = _parse_plugin_dependencies(conf, logger, current_plugin_id)
            for dep in dependencies:
                satisfied, error_message = _check_plugin_dependency(dep, logger, current_plugin_id)
                if not satisfied:
                    raise _to_domain_error(
                        code="PLUGIN_DEPENDENCY_CHECK_FAILED",
                        message=f"Plugin dependency check failed for plugin '{current_plugin_id}': {error_message}",
                        status_code=400,
                        plugin_id=current_plugin_id,
                        error_type="DependencyCheckFailed",
                    )

            # 元数据在 host 起来**之前**取。取法有两种：包里带了就直接读，没有才
            # 起一次隔离 worker 去 import。
            #
            # ⚠️ 顺序是承重的。放在 host 起来之后的话，那次 import 和插件进程自己
            # 那次是并发的：模块级代码里拿文件锁、绑端口、起单例的插件会在第二次
            # import 上失败，于是生命周期清理把一个健康的 host 杀掉、把这次启动报成
            # 失败；没有直接冲突的插件也会把 import 期副作用执行两遍（codex）。
            # 本 PR 之前这条路是安全的，因为扫描发生在 refresh_plugin 里、早于
            # host 启动——刷新不再扫描之后，得在这里把那个顺序还回来。
            #
            # 上限按剩余预算收窄：扫描自己的上限是 10s，只钳住 host 启动的话，一次
            # 冷扫描就能把整轮 reload 的墙钟顶穿（CodeRabbit）。
            module_path, class_name = entry.split(":", 1)
            isolated_metadata = await asyncio.to_thread(
                partial(
                    _read_packaged_isolated_metadata,
                    config_path,
                    current_plugin_id,
                    conf=conf,
                    pdata=pdata,
                )
            )
            if isolated_metadata is None:
                # 预算在读完包内元数据之后才算。读那一步自己可能要哈希一整棵改过的
                # 树，然后才回落——在它前面算出来的上限是过期快照，worker 还会拿到
                # 接近 10s 的额度，整轮 reload 就会超出对外承诺的墙钟（codex）。
                # 和下面 startup_timeout_value 的重算是同一条判据。
                scan_timeout = _clamp_step_timeout(
                    _DEFAULT_METADATA_SCAN_TIMEOUT,
                    _remaining_step_budget(start_deadline),
                    floor=_MIN_CLAMPED_START_TIMEOUT,
                )
                isolated_metadata = await asyncio.to_thread(
                    scan_plugin_metadata_isolated,
                    plugin_id=current_plugin_id,
                    module_path=module_path,
                    class_name=class_name,
                    config_path=config_path,
                    conf=conf,
                    pdata=pdata,
                    python_requirement_paths=python_requirement_paths,
                    timeout=scan_timeout,
                )

            if start_deadline is not None and startup_timeout_value is not None:
                # reload-all 把本轮的截止期压进来。只在启动**开始前**检查一次是不
                # 够的：一个在截止期前一瞬开始的启动，之后仍会一路等到它自己的
                # startup timeout，于是整轮 reload 照样冲破对外承诺的墙钟，前端早已
                # 放弃而插件状态还在被改（codex / CodeRabbit / Greptile）。
                #
                # 压进去而不是套 asyncio.wait_for：start_plugin 带
                # @serialized_plugin_operation，那个包装器拿到锁之后会屏蔽取消，
                # 外面套超时只会把一次真实结果报成超时（见 stop 那边的说明）。
                #
                # ⚠️ 必须算在取元数据**之后**。取元数据现在排在 host 启动前面，它自己
                # 最多要花一个 scan_timeout；在它前面算出来的上限，等真正调
                # _start_host_with_timeout 时已经是过期快照，于是启动阶段的墙钟会比
                # 设计值多出"每个插件一次扫描"——正是这段钳位本来要防的那件事
                # （coderabbit）。_remaining_step_budget 按绝对截止期算，挪到这里重算
                # 就是对的。
                startup_timeout_value = _clamp_step_timeout(
                    startup_timeout_value,
                    _remaining_step_budget(start_deadline),
                    floor=_MIN_CLAMPED_START_TIMEOUT,
                )

            startup_result = await _start_host_with_timeout(
                plugin_id=current_plugin_id,
                host_obj=host_obj,
                message_target_queue=state.message_queue,
                startup_timeout=startup_timeout_value,
                startup_failure=startup_failure_policy,
            )
            startup_error = _extract_startup_error(startup_result)
            startup_degraded = bool(startup_error) and startup_failure_policy == "warn"

            process_obj = getattr(created_host, "process", None)
            if process_obj is not None and hasattr(process_obj, "is_alive"):
                if not process_obj.is_alive():
                    exitcode_obj = getattr(process_obj, "exitcode", None)
                    exitcode_text = str(exitcode_obj) if exitcode_obj is not None else "unknown"
                    raise _to_domain_error(
                        code="PLUGIN_PROCESS_DIED_IMMEDIATELY",
                        message=(
                            f"Plugin '{current_plugin_id}' process died immediately after startup "
                            f"(exitcode: {exitcode_text})"
                        ),
                        status_code=500,
                        plugin_id=current_plugin_id,
                        error_type="ProcessDiedImmediately",
                    )

            await asyncio.to_thread(
                install_isolated_plugin_metadata,
                current_plugin_id,
                isolated_metadata,
            )
            entries_preview = isolated_metadata.entries_preview
            await asyncio.to_thread(
                _set_plugin_runtime_metadata_sync,
                current_plugin_id,
                runtime_enabled=True,
                runtime_auto_start=auto_start_value,
                entries_preview=entries_preview,
                startup_state="degraded" if startup_degraded else "ready",
                startup_error=startup_error if startup_degraded else None,
            )

            await asyncio.to_thread(_register_or_replace_host_sync, current_plugin_id, host_obj)
            registered_plugin_id = current_plugin_id

            _emit_lifecycle_event(event_type="plugin_started", plugin_id=current_plugin_id)
            response: dict[str, object] = {
                "success": True,
                "plugin_id": current_plugin_id,
                "message": "Plugin started successfully",
            }
            if startup_degraded:
                response["startup_degraded"] = True
                response["startup_error"] = startup_error
                response["message"] = "Plugin started with startup warning"
            if current_plugin_id != original_plugin_id:
                response["original_plugin_id"] = original_plugin_id
                if startup_degraded:
                    response["message"] = (
                        f"Plugin started with startup warning (renamed from '{original_plugin_id}' to "
                        f"'{current_plugin_id}' due to ID conflict)"
                    )
                else:
                    response["message"] = (
                        f"Plugin started successfully (renamed from '{original_plugin_id}' to "
                        f"'{current_plugin_id}' due to ID conflict)"
                    )
            if persist_user_intent:
                stale_plugin_ids = tuple(
                    plugin_id
                    for plugin_id in resolved_plugin_ids
                    if plugin_id != current_plugin_id
                )
                await _persist_changed_runtime_intent(
                    response,
                    current_plugin_id,
                    True,
                    previous_plugin_ids=stale_plugin_ids,
                )
            return response
        except ServerDomainError:
            if host_obj is not None:
                cleanup_plugin_id = registered_plugin_id if registered_plugin_id is not None else current_plugin_id
                await _cleanup_started_host(cleanup_plugin_id, host_obj)
            raise
        except HTTPException as exc:
            if host_obj is not None:
                cleanup_plugin_id = registered_plugin_id if registered_plugin_id is not None else current_plugin_id
                await _cleanup_started_host(cleanup_plugin_id, host_obj)
            raise _to_domain_error(
                code="PLUGIN_START_FAILED",
                message=_detail_to_message(exc.detail, default_message="start_plugin failed"),
                status_code=exc.status_code,
                plugin_id=current_plugin_id,
                error_type="HTTPException",
            ) from exc
        except PluginError as exc:
            if host_obj is not None:
                cleanup_plugin_id = registered_plugin_id if registered_plugin_id is not None else current_plugin_id
                await _cleanup_started_host(cleanup_plugin_id, host_obj)
            raise _to_domain_error(
                code="PLUGIN_START_FAILED",
                message=str(exc),
                status_code=500,
                plugin_id=current_plugin_id,
                error_type=type(exc).__name__,
            ) from exc
        except (ImportError, ModuleNotFoundError) as exc:
            if host_obj is not None:
                cleanup_plugin_id = registered_plugin_id if registered_plugin_id is not None else current_plugin_id
                await _cleanup_started_host(cleanup_plugin_id, host_obj)
            raise _to_domain_error(
                code="PLUGIN_IMPORT_FAILED",
                message=f"Failed to import plugin '{current_plugin_id}' module",
                status_code=500,
                plugin_id=current_plugin_id,
                error_type=type(exc).__name__,
            ) from exc
        except RUNTIME_ERRORS as exc:
            if host_obj is not None:
                cleanup_plugin_id = registered_plugin_id if registered_plugin_id is not None else current_plugin_id
                await _cleanup_started_host(cleanup_plugin_id, host_obj)
            raise _to_domain_error(
                code="PLUGIN_START_FAILED",
                message="start_plugin failed",
                status_code=500,
                plugin_id=current_plugin_id,
                error_type=type(exc).__name__,
            ) from exc

    @serialized_plugin_operation
    async def stop_plugin(
        self,
        plugin_id: str,
        *,
        persist_user_intent: bool = False,
        stop_deadline: float | None = None,
    ) -> dict[str, object]:
        host_obj = await asyncio.to_thread(_get_plugin_host_sync, plugin_id)
        if host_obj is None:
            raise _to_domain_error(
                code="PLUGIN_NOT_RUNNING",
                message=f"Plugin '{plugin_id}' is not running",
                status_code=404,
                plugin_id=plugin_id,
                error_type="PluginNotRunning",
            )

        if not isinstance(host_obj, PluginHostContract):
            raise _to_domain_error(
                code="INVALID_HOST_OBJECT",
                message=f"Plugin '{plugin_id}' host object is invalid",
                status_code=500,
                plugin_id=plugin_id,
                error_type=type(host_obj).__name__,
            )

        try:
            _emit_lifecycle_event(event_type="plugin_stop_requested", plugin_id=plugin_id)
            # 剩余预算在这里算，不在调用方那边算。这个函数体是在
            # @serialized_plugin_operation 拿到锁**之后**才跑的，所以此刻的"还剩
            # 多少"才是真的；在外面算的话，一次等了 19s 锁的关停照样会拿到按 20s
            # 算出来的上限，停止阶段就此冲破对外承诺的墙钟（codex）。和启动侧收
            # start_deadline 是同一个形状。
            await host_obj.shutdown(
                timeout=(
                    PLUGIN_SHUTDOWN_TIMEOUT
                    if stop_deadline is None
                    else _clamp_step_timeout(
                        PLUGIN_SHUTDOWN_TIMEOUT,
                        _remaining_step_budget(stop_deadline),
                        floor=_MIN_CLAMPED_STOP_TIMEOUT,
                    )
                )
            )
            await asyncio.to_thread(_pop_plugin_host_sync, plugin_id)
            await asyncio.to_thread(_remove_event_handlers_sync, plugin_id)
            # Clear any LLM tools the plugin had registered with
            # ``main_server``. Best-effort: a transient HTTP failure
            # here shouldn't block the rest of plugin teardown — the
            # registration helper logs the error itself. Without this
            # call, a stopped plugin's tools would linger in
            # main_server's registry until process restart, and the
            # model could still pick them only to hit a 404 on
            # dispatch.
            try:
                # 这一步也在锁里，也在停止阶段的预算里。它自己那个 2s 超时是
                # 独立的，所以一次卡住的 main_server 能让关停在预算之外再多花
                # 两秒，而锁一直握着（codex）。按剩余预算收窄。
                #
                # 但**不能**用 _clamp_step_timeout：那个下界是给"启动"用的，因为
                # 一个被我们停掉的插件必须拿到一次真正的尝试。这里是尽力而为的
                # 远端清理，预算见底还硬给它 1s，就是每个插件都在锁上多压一秒
                # （CodeRabbit）。
                #
                # 也**不能**在预算见底时干脆跳过——我上一版就是那么写的，是错的。
                # 全仓只有这一处清理远端工具注册，没有任何对账或重试兜底：跳过之后
                # host 已经摘掉，而 main_server 那边的工具还在向模型公布，模型选中
                # 它只会拿到"插件没在跑"，并且永远不会自愈（Greptile）。
                #
                # 所以给一个很小的下界，让它至少发得出去。这比跳过**严格更好**：
                # 失败了也不过回到跳过的状态（这个 POST 是按 source 整体清除、幂等，
                # 重发无害），成功了就少一批幽灵工具。而正常情况下这是一次本机
                # POST、毫秒级返回，下界根本不会生效。
                cleanup_budget = _remaining_step_budget(stop_deadline)
                cleanup_result = await clear_plugin_llm_tools(
                    plugin_id,
                    timeout=(
                        None
                        if cleanup_budget is None
                        else min(
                            _CLEAR_TOOLS_BUDGET_SECONDS,
                            max(_MIN_TOOL_CLEANUP_TIMEOUT, cleanup_budget),
                        )
                    ),
                )
                if isinstance(cleanup_result, dict) and not cleanup_result.get("ok"):
                    # 提到 warning。清理本身是尽力而为，但"没清掉"的后果是模型看得见
                    # 一个调不通的工具、而没有任何东西会重试；debug 级别等于没留痕。
                    logger.warning(
                        "plugin stopped but its LLM tools may still be advertised: "
                        "plugin_id={}, reason={}",
                        plugin_id,
                        cleanup_result.get("error") or cleanup_result.get("status_code"),
                    )
            except Exception as exc:
                # 和上面 ok=False 那条同一句话、同一个级别：两条路的后果一模一样
                # ——工具可能还挂在 main_server 上，而没有任何东西会重试。
                #
                # 抛出这条尤其隐蔽：clear_plugin_tools 内部只挡 httpx.HTTPError 和
                # asyncio.TimeoutError，一个 content-type 声明是 JSON、正文却坏掉的
                # 响应会让 resp.json() 抛 ValueError 一路冒到这里（CodeRabbit）。留在
                # debug 的话，这条路上的幽灵工具照样无从追查。
                logger.warning(
                    "plugin stopped but its LLM tools may still be advertised: "
                    "plugin_id={}, err_type={}, err={}",
                    plugin_id,
                    type(exc).__name__,
                    str(exc),
                )
            _emit_lifecycle_event(event_type="plugin_stopped", plugin_id=plugin_id)
            response: dict[str, object] = {
                "success": True,
                "plugin_id": plugin_id,
                "message": "Plugin stopped successfully",
            }
            if persist_user_intent:
                await _persist_changed_runtime_intent(
                    response,
                    plugin_id,
                    False,
                )
            return response
        except PluginError as exc:
            logger.error(
                "stop_plugin failed with PluginError: plugin_id={}, err_type={}, err={}",
                plugin_id,
                type(exc).__name__,
                str(exc),
            )
            raise _to_domain_error(
                code="PLUGIN_STOP_FAILED",
                message=str(exc),
                status_code=500,
                plugin_id=plugin_id,
                error_type=type(exc).__name__,
            ) from exc
        except RUNTIME_ERRORS as exc:
            logger.error(
                "stop_plugin failed: plugin_id={}, err_type={}, err={}",
                plugin_id,
                type(exc).__name__,
                str(exc),
            )
            raise _to_domain_error(
                code="PLUGIN_STOP_FAILED",
                message="stop_plugin failed",
                status_code=500,
                plugin_id=plugin_id,
                error_type=type(exc).__name__,
            ) from exc

    @serialized_plugin_operation
    async def reload_plugin(self, plugin_id: str) -> dict[str, object]:
        _emit_lifecycle_event(event_type="plugin_reload_requested", plugin_id=plugin_id)

        is_running = await asyncio.to_thread(_plugin_is_running_sync, plugin_id)
        if is_running:
            try:
                await self.stop_plugin(plugin_id)
            except ServerDomainError as error:
                if error.status_code != 404:
                    raise

        # reload 是用户按的按钮，而前端在插件停着的时候也给这个按钮。用它把一个
        # 待批准的插件启动起来，和用 start 启动是同一件事，批准位一样要清掉——否则
        # 那个插件永远启动得起来、却永远不自启（codex）。
        result = await self.start_plugin(plugin_id, persist_user_intent=True)
        _emit_lifecycle_event(event_type="plugin_reloaded", plugin_id=plugin_id)
        return result

    async def reload_all_plugins(self) -> dict[str, object]:
        start_time = time_module.perf_counter()
        _emit_lifecycle_event(event_type="plugins_reload_all_requested")

        try:
            await plugin_registry_service.refresh_registry()
        except ServerDomainError as exc:
            raise _to_domain_error(
                code=exc.code,
                message=exc.message,
                status_code=exc.status_code,
                plugin_id=None,
                error_type="RegistryRefreshFailed",
            ) from exc

        running_plugin_ids = await asyncio.to_thread(_list_running_plugin_ids_sync)
        if not running_plugin_ids:
            return {
                "success": True,
                "reloaded": [],
                "failed": [],
                "skipped": [],
                "message": "No running plugins to reload",
            }

        # 顺序，不是 gather。
        #
        # 这里原本是 asyncio.gather，但它一点并发都买不到：每个
        # _safe_stop_for_reload 内层的 stop_plugin 自己带
        # @serialized_plugin_operation，而那把锁的重入是按 asyncio.Task 认的
        # （_OPERATION_OWNER 存的是任务对象）。gather 给每个协程新建一个 Task，
        # 子任务的 current_task 必然不等于持锁那个，于是重入判定失败，N 个 stop
        # 严格排队。这个"按任务认"是刻意的——它防的正是无关任务蹭别人的锁——
        # 所以不能靠改重入来让它真并行。
        #
        # 写成顺序循环是为了让代码说实话：它本来就是顺序的。同时顺带能在中途
        # 检查预算，gather 做不到这件事。
        stop_outcomes = []
        skipped_over_budget: list[str] = []
        stop_deadline = time_module.monotonic() + _RELOAD_ALL_BUDGET_SECONDS
        for index, plugin_id in enumerate(running_plugin_ids):
            if time_module.monotonic() > stop_deadline:
                # 剩下的记进 skipped 再返回，不能让它们既不在成功里也不在失败里
                # ——那样调用方看到的是一份"少了几个插件"的结果，而没有任何东西
                # 说它们为什么不见了。
                skipped_over_budget = list(running_plugin_ids[index:])
                logger.warning(
                    "reload_all stop phase over budget after {}s, {} plugin(s) skipped",
                    _RELOAD_ALL_BUDGET_SECONDS,
                    len(skipped_over_budget),
                )
                break
            # 这一次 stop 也要受剩余预算约束：只在开始前检查的话，一个慢关停
            # （或者调大了的 NEKO_PLUGIN_SHUTDOWN_TIMEOUT）就能让整个阶段冲破
            # 对外承诺的墙钟上限（codex）。
            #
            # 但不能用 asyncio.wait_for 包在外面。stop_plugin 带
            # @serialized_plugin_operation，而那个包装器一旦拿到锁就屏蔽取消、
            # 等内层跑完再抛 CancelledError（operation_lock 里的 shield 循环）。
            # 于是请求照样阻塞整个关停时长，然后把一次**已经成功**的停止报成超时，
            # 插件被排除在重启名单外，最后停着没起来（codex）。
            #
            # 把预算送进去，而不是套在外面：等锁那段由 bounded_operation_wait
            # 管，真正关停那段由 shutdown_timeout 管，两段都在预算内结束，返回的
            # 也是真实结果。
            # 等锁和关停各自按"此刻还剩多少"算，不能共用一个快照。共用的话，一次
            # 等满 remaining 的抢锁之后，关停又拿到一份完整的 remaining，一轮就能
            # 花掉两倍预算——这跟两层锁各起一份截止期是同一个错误，只是换了个地方
            # （本轮对抗复审）。
            remaining = max(0.0, stop_deadline - time_module.monotonic())
            with bounded_operation_wait(remaining):
                stop_outcomes.append(
                    await self._safe_stop_for_reload(
                        plugin_id, stop_deadline=stop_deadline
                    )
                )

        plugins_to_start: list[str] = []
        failed: list[dict[str, object]] = []
        for outcome in stop_outcomes:
            if outcome.success:
                plugins_to_start.append(outcome.plugin_id)
                continue
            failed.append({"plugin_id": outcome.plugin_id, "error": outcome.error or "Stop failed"})

        # 也进 skipped：既有契约里 skipped 是"没被尝试"的意思，而这些插件正是
        # 没被尝试。只放进 failed 会让调用方分不清"停失败了"和"根本没轮到"。
        for plugin_id in skipped_over_budget:
            failed.append(
                {
                    "plugin_id": plugin_id,
                    "error": (
                        "skipped: reload exceeded its "
                        f"{_RELOAD_ALL_BUDGET_SECONDS:g}s budget"
                    ),
                }
            )

        reloaded: list[str] = []
        ordered_plugin_ids = await plugin_registry_service.order_plugin_ids(plugins_to_start)
        # 启动阶段有**自己**的预算，不吃停止阶段剩下的。
        #
        # 启动阶段确实也需要上限：start_plugin 通常比 stop 慢得多（读配置、拉子
        # 进程、扫元数据），只管住停止阶段的话整轮 reload 照样能冲破前端的 30s
        # （CodeRabbit）。但两个阶段不能共用一份预算：走到这里的插件都是**已经被
        # 我们停掉**的，停一个插件就欠它一次启动。共用预算时，一个慢关停就能把
        # 剩下的额度吃光，于是 reload 悄悄变成 stop——插件全下线了，而调用方看到
        # 的只是一行 "over budget"。宁可整轮多花一份预算，也不能把用户的插件留在
        # 停止状态。
        #
        # 而且这个循环**不会**因为预算耗尽而中途退出——这一点和停止阶段刻意不对称。
        # 停止阶段跳过一个插件是安全的：没轮到的插件还好好跑着。启动阶段跳过一个
        # 插件，等于把一个我们刚亲手停掉的插件永久留在停止状态：自启动只在服务器
        # 启动时跑一次，没有任何周期性对账会把它捡回来，用户只能手动启动或者重启
        # 整个服务器（Greptile）。所以每个被停掉的插件都必须拿到一次启动尝试；
        # 预算见底之后它们各自拿下界那么长，够不够是另一回事，但"根本没试"不行。
        #
        # 代价说清楚：启动阶段的墙钟上限因此是 预算 + 剩余插件数 x 下界，而不是
        # 一个硬预算。健康路径根本碰不到——实测启动很快，预算压根用不完。
        start_deadline = time_module.monotonic() + _RELOAD_ALL_BUDGET_SECONDS
        for plugin_id in ordered_plugin_ids:
            # 启动这半边同样把等锁和启动本身都封在剩余预算里——和上面的 stop
            # 对称，否则预算只管住了两个阶段中的一个。
            #
            # 但等锁那段和步骤超时用同一个下界，不能压到 0：预算见底时
            # bounded_operation_wait(0.0) 等于"一次都不等"，此刻只要有别的插件操作
            # 握着进程锁，start_plugin 立刻抛 PluginOperationBusy，而这个插件是刚被
            # 我们停掉的——它会就这么一直停着（CodeRabbit）。刚去掉超预算 break 就是
            # 为了不让这种事发生，零等待等于把它从后门放回来。
            #
            # 停止侧不需要这个下界，而且那是刻意的：停止侧等不到锁，插件还好好跑着。
            remaining = max(
                _MIN_CLAMPED_START_TIMEOUT, start_deadline - time_module.monotonic()
            )
            with bounded_operation_wait(remaining):
                outcome = await self._safe_start_for_reload(
                    plugin_id, start_deadline=start_deadline
                )
            if outcome.success:
                reloaded.append(outcome.plugin_id)
                continue
            failed.append({"plugin_id": outcome.plugin_id, "error": outcome.error or "Start failed"})

        elapsed = time_module.perf_counter() - start_time
        success = len(failed) == 0
        message: str
        if success:
            message = f"Successfully reloaded {len(reloaded)} plugins (took {elapsed:.3f}s)"
        else:
            message = f"Reloaded {len(reloaded)} plugins, {len(failed)} failed (took {elapsed:.3f}s)"

        _emit_lifecycle_event(
            event_type="plugins_reload_all_completed",
            data={
                "reloaded_count": len(reloaded),
                "failed_count": len(failed),
                "duration_seconds": round(elapsed, 3),
            },
        )

        return {
            "success": success,
            "reloaded": reloaded,
            "failed": failed,
            "skipped": list(skipped_over_budget),
            "message": message,
        }

    @serialized_plugin_operation
    async def delete_plugin(self, plugin_id: str) -> dict[str, object]:
        """Invoke the uninstall transaction and preserve the public response."""
        try:
            result = await uninstall_plugin(plugin_id)
        except UninstallPluginError as exc:
            raise ServerDomainError(
                code=exc.code,
                message=exc.message,
                status_code=exc.status_code,
                details=dict(exc.details),
            ) from exc

        restored_builtin_started = (
            result.restored_builtin and result.runtime_restart == "succeeded"
        )

        _emit_lifecycle_event(
            event_type="plugin_deleted",
            plugin_id=plugin_id,
            data={
                "plugin_dir": str(result.plugin_dir),
                "deleted_from_disk": result.deleted_from_disk,
                "deleted_profile_dir": (
                    str(result.deleted_profile_dir)
                    if result.deleted_profile_dir
                    else None
                ),
                "restored_builtin": result.restored_builtin,
                "restored_builtin_started": restored_builtin_started,
                "restored_builtin_restart_error": result.runtime_restart_error,
                "preference_action": result.preference_action,
                "filesystem_rollback": result.filesystem_rollback,
                "runtime_restart": result.runtime_restart,
                "cleanup_pending": result.cleanup_pending,
            },
        )
        response: dict[str, object] = {
            "success": True,
            "plugin_id": plugin_id,
            "plugin_dir": str(result.plugin_dir),
            "deleted_from_disk": result.deleted_from_disk,
            "restored_builtin": result.restored_builtin,
            "restored_builtin_started": restored_builtin_started,
            "restored_builtin_restart_error": result.runtime_restart_error,
            "preference_action": result.preference_action,
            "filesystem_rollback": result.filesystem_rollback,
            "runtime_restart": result.runtime_restart,
            "cleanup_pending": result.cleanup_pending,
            "message": "Plugin deleted successfully",
        }
        return response

    async def retry_deferred_profile_cleanup(self) -> int:
        """Retry persisted uninstall cleanup jobs during server startup."""
        cleaned_profiles = await asyncio.to_thread(
            retry_deferred_profile_cleanup_sync
        )
        await asyncio.to_thread(retry_deferred_plugin_code_cleanup_sync)
        return cleaned_profiles

    async def _safe_stop_for_reload(
        self, plugin_id: str, *, stop_deadline: float | None = None
    ) -> _ReloadOutcome:
        try:
            await self.stop_plugin(plugin_id, stop_deadline=stop_deadline)
            return _ReloadOutcome(plugin_id=plugin_id, success=True)
        except PluginOperationBusy as error:
            return _ReloadOutcome(plugin_id=plugin_id, success=False, error=str(error))
        except ServerDomainError as error:
            if error.status_code == 404:
                return _ReloadOutcome(plugin_id=plugin_id, success=True)
            return _ReloadOutcome(plugin_id=plugin_id, success=False, error=error.message)

    async def _safe_start_for_reload(
        self, plugin_id: str, *, start_deadline: float | None = None
    ) -> _ReloadOutcome:
        try:
            await self.start_plugin(
                plugin_id,
                refresh_registry=False,
                start_deadline=start_deadline,
            )
            return _ReloadOutcome(plugin_id=plugin_id, success=True)
        except PluginOperationBusy as error:
            return _ReloadOutcome(plugin_id=plugin_id, success=False, error=str(error))
        except ServerDomainError as error:
            return _ReloadOutcome(plugin_id=plugin_id, success=False, error=error.message)
