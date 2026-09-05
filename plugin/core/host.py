from __future__ import annotations

import asyncio
import copy
import importlib
import importlib.machinery
import importlib.util
import inspect
import json
import math
import multiprocessing
import os
import sys
import threading
import time
import hashlib
import types
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Type

from plugin.logging_config import logger

from plugin._types.events import EVENT_META_ATTR
from plugin.core.entry_points import normalize_plugin_entry_point
from plugin.core.model_gateway_access import model_gateway_access
from plugin.sdk import PERSIST_ATTR
from plugin.core.state import state
from plugin.core.context import PluginContext
from plugin.core.communication import PluginCommunicationResourceManager, STARTUP_RESULT_REQ_ID
from plugin._types.models import HealthCheckResponse
from plugin._types.exceptions import (
    PluginLifecycleError,
    PluginEntryNotFoundError,
    PluginError,
)
from plugin.settings import (
    PLUGIN_TRIGGER_TIMEOUT,
    PLUGIN_SHUTDOWN_TIMEOUT,
    QUEUE_GET_TIMEOUT,
    PROCESS_SHUTDOWN_TIMEOUT,
    PROCESS_TERMINATE_TIMEOUT,
)
from plugin.sdk.shared.core.entry_runtime import prepare_entry_kwargs, resolve_entry_timeout
from plugin.sdk.shared.core.finish import normalize_structured_data
from plugin.sdk.shared.core.result_contract import model_schema_from_type
from plugin.sdk.shared.core.router import PluginRouter
from plugin.sdk.plugin.ui import UI_ACTION_META_ATTR, UI_CONTEXT_META_ATTR
from plugin.core.bus.types import dispatch_bus_change
from plugin.core.zmq_transport import (
    HostTransport, ChildTransport, CH_CMD, CH_RES, CH_STS, CH_MSG, CH_COMM, CH_RESP,
)


def _sanitize_plugin_id(raw: Any, max_len: int = 64) -> str:
    s = str(raw)
    safe = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in s)
    safe = safe.strip("_-")
    if not safe:
        safe = hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]
    if len(safe) > max_len:
        digest = hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:12]
        safe = f"{safe[:max_len - 13]}_{digest}"
    return safe


def _resolve_current_storage_layout() -> dict[str, Any]:
    from utils.config_manager import get_config_manager
    from utils.storage_layout import resolve_storage_layout

    return resolve_storage_layout(get_config_manager())


def _refresh_child_storage_layout_env(logger_obj: Any) -> None:
    try:
        from utils.storage_layout import export_storage_layout_to_env

        layout = _resolve_current_storage_layout()
        export_storage_layout_to_env(layout)
        logger_obj.debug(
            "Plugin child storage layout env refreshed: selected_root={}, anchor_root={}, source={}",
            layout.get("selected_root"),
            layout.get("anchor_root"),
            layout.get("source"),
        )
    except Exception as exc:
        logger_obj.warning(
            "Failed to refresh plugin child storage layout env; plugin data root may use fallback: err_type={}, err={}",
            type(exc).__name__,
            str(exc),
        )


_TIMEOUT_UNSET = object()


# ============================================================================
# _plugin_process_runner 辅助函数
# ============================================================================

def _setup_plugin_logger(plugin_id: str, project_root: Path) -> Any:
    """配置插件子进程的 logger（走本体 RobustLoggerConfig，落到 我的文档/N.E.K.O/logs/）。

    NOTE: 不要再引入 loguru / 不要再用 cwd 推日志目录 / 不要再造 FileHandler。
    见 plugin/logging_config.py 顶部警告。

    Args:
        plugin_id: 插件 ID
        project_root: 项目根目录（用于注入 sys.path）

    Returns:
        PluginLoggerAdapter，已绑定 plugin_id。
    """
    safe_pid = _sanitize_plugin_id(plugin_id)
    # 让 plugin/logging_config.get_logger() 能算出正确的 service namespace。
    os.environ["NEKO_PLUGIN_SERVICE_NAME"] = f"Plugin_{safe_pid}"

    # 子进程要先把项目根加到 sys.path，否则下面 import utils.logger_config 会失败。
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    try:
        from utils.logger_config import setup_logging as _bootstrap_setup_logging
        _bootstrap_setup_logging(
            service_name=f"Plugin_{safe_pid}",
            silent=True,
            # plugin 子进程日志统一收纳到 logs/plugin/，别跟 PluginServer /
            # Main / Memory / Agent 等宿主进程日志混在 logs/ 顶层。
            log_subdir="plugin",
        )
    except Exception:
        # RobustLoggerConfig 自己会做多级 fallback；这里只兜底，不要遮蔽根因。
        pass

    from plugin.logging_config import get_logger
    return get_logger(f"plugin.{safe_pid}").bind(plugin_id=plugin_id)


def _setup_logging_interception(logger: Any, project_root: Path) -> None:
    """兼容性 no-op：本体即 stdlib logging，不需要拦截。

    保留函数签名以避免老调用者破坏。仅做一件事：确保 project_root 在 sys.path。
    """
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


# 父进程等 UI context 回复的默认预算，与 CommManager.get_ui_context 对齐。
_UI_CONTEXT_DEFAULT_BUDGET = 5.0
# 留给「收手 -> 序列化 -> IPC 回程」的余量：子进程必须赶在父进程超时之前把
# 降级结果（actions + context_error）送到，否则父进程先炸，降级分支够不着。
_UI_CONTEXT_REPLY_HEADROOM = 0.75
# 预算小到扣不出上面那个余量时改按比例让，绝不能用一个固定下限反超调用方。
_UI_CONTEXT_MIN_REPLY_SHARE = 0.5
# 一次 IPC 往返都不够的预算是荒谬输入，跟 nan / inf 一样退回默认值。这同时
# 挡掉了浮点下溢：让份额乘法有意义的最小预算远在非规格化数之上。
_UI_CONTEXT_MIN_SENSIBLE_BUDGET = 0.001


def _ui_context_provider_budget(requested: object) -> float:
    """Provider budget that always leaves the caller room to receive the reply.

    Invariant, for every input: the result is finite, positive, and strictly
    less than the budget it resolved to. Subtracting a fixed headroom alone
    does not get there — short budgets would be floored above the caller's,
    and budgets large enough to round the subtraction away come back
    unchanged — so both cases fall back to yielding a share of the budget.
    Budgets too small to survive that share (or to fit an IPC round trip at
    all) are nonsense input and resolve to the default instead.
    """
    try:
        budget = float(requested)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        # OverflowError is neither: an int past the float range raises it.
        budget = _UI_CONTEXT_DEFAULT_BUDGET
    # nan slips past every comparison and inf survives the subtraction, so
    # neither may reach asyncio.wait_for as a deadline.
    if not math.isfinite(budget) or budget < _UI_CONTEXT_MIN_SENSIBLE_BUDGET:
        budget = _UI_CONTEXT_DEFAULT_BUDGET
    resolved = max(budget - _UI_CONTEXT_REPLY_HEADROOM, budget * _UI_CONTEXT_MIN_REPLY_SHARE)
    if not resolved < budget:
        resolved = budget * _UI_CONTEXT_MIN_REPLY_SHARE
    return resolved


def _discard_abandoned_task_result(task: asyncio.Future) -> None:
    """Retrieve an abandoned task's outcome so asyncio stops complaining about it."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def _await_within_budget(awaitable: Any, budget: float) -> tuple[bool, Any]:
    """Wait at most *budget* seconds. Returns ``(completed, result)``.

    Deliberately not ``asyncio.wait_for``: that cancels the inner task and then
    *awaits* it, so a coroutine which swallows ``CancelledError`` (or blocks in
    a ``finally``) delays — or never triggers — the timeout. The whole point of
    this budget is to answer before the caller's own deadline, so abandon the
    task rather than wait on it.
    """
    task = asyncio.ensure_future(awaitable)
    done, _pending = await asyncio.wait({task}, timeout=budget)
    if task in done:
        try:
            return True, task.result()
        except asyncio.CancelledError as exc:
            # 这个 task 只在本函数局部存在，外面 cancel 不到它——走到这里只可能是
            # awaitable 自己漏出了 CancelledError。它继承 BaseException，会穿透
            # 调用方的 except Exception 把 success=False 送回去，所以换成普通异常。
            # 只包 result() 这一行：外层协作式取消是从上面那个 await 抛出来的，
            # 包进去会把真正的关停信号也吞掉。
            raise RuntimeError("awaited task was cancelled") from exc
    task.cancel()
    task.add_done_callback(_discard_abandoned_task_result)
    return False, None


def _find_project_root(config_path: Path) -> Path:
    """
    从配置文件路径向上探测项目根目录。
    
    Args:
        config_path: 插件配置文件路径
    
    Returns:
        项目根目录路径
    """
    cur = config_path.resolve()
    try:
        if cur.is_file():
            cur = cur.parent
    except Exception:
        pass
    
    for _ in range(10):
        try:
            candidate = cur
            # Repo root should contain both plugin/ and utils/
            if (candidate / "plugin").is_dir() and (candidate / "utils").is_dir():
                return candidate
        except Exception:
            pass
        if cur.parent == cur:
            break
        cur = cur.parent
    
    try:
        logger.debug(
            "[Plugin Process] Could not find project root via exploration from %s; using host-relative root",
            config_path,
        )
    except Exception:
        pass

    # 走到这里说明 config_path 不在仓库树里。用户插件装在
    # `我的文档/{APP_NAME}/plugins/<id>/plugin.toml`，按 plugin/plugins/<id>/
    # 的老布局往上数四层会数到用户的「我的文档」本身，而这个返回值会被
    # 插入子进程的 sys.path[0]——用户随手放在文档里的 types.py / utils/
    # 就抢在 stdlib 和宿主模块前面被 import。宿主自己的位置才是可靠的：
    # plugin/core/host.py -> plugin/core -> plugin -> 仓库根。
    # 判据只能看 plugin/：打包版用 --include-package=utils 把 utils/ 编进
    # 可执行文件，dist 里根本没有这个目录，跟着 utils/ 一起校验会让每个出货包
    # 的每个插件子进程都掉到下面那条逃生口去。plugin/ 两种布局下都真实存在
    # （dist 里是 plugin/plugins/）。
    try:
        host_relative_root = Path(__file__).resolve().parents[2]
        if (host_relative_root / "plugin").is_dir():
            return host_relative_root
    except Exception:
        pass

    # 最后兜底：插件目录本身。宁可少一个导入根，也不要把一个无关目录
    # 推到 sys.path 最前面。
    try:
        return config_path.parent.resolve()
    except Exception:
        return config_path.parent


def _prepare_child_plugin_import_roots(logger: Any) -> None:
    """Expose Core itself without exposing shared plugin candidate roots."""

    try:
        from plugin.settings import BUILTIN_PLUGIN_CONFIG_ROOT, PLUGIN_CONFIG_ROOTS
    except Exception as exc:
        logger.debug("[Plugin Process] Failed to load plugin config roots: {}", exc)
        return

    try:
        builtin_root = BUILTIN_PLUGIN_CONFIG_ROOT.resolve()
    except Exception:
        builtin_root = BUILTIN_PLUGIN_CONFIG_ROOT

    # A child process loads only the plugin selected by its config path. Remove
    # any inherited candidate roots so sibling user/builtin plugins cannot
    # shadow standard-library or third-party top-level modules.
    for plugin_config_root in PLUGIN_CONFIG_ROOTS:
        try:
            import_root = plugin_config_root.resolve().parent
        except Exception:
            import_root = plugin_config_root.parent
        value = str(import_root)
        while value in sys.path:
            sys.path.remove(value)

    repo_root = builtin_root.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _prepare_child_current_plugin_import_root(config_path: Path, logger: Any) -> None:
    """Prepare an isolated namespace for the selected plugin."""

    try:
        plugin_root = config_path.resolve().parent.parent
    except Exception as exc:
        logger.debug("[Plugin Process] Failed to resolve current plugin import root: {}", exc)
        return

    _ensure_plugins_namespace(plugin_root, logger)


def _prepare_child_plugin_vendor_path(config_path: Path, logger: Any) -> None:
    """Add the current plugin's vendor/ directory before importing its entry."""

    vendor_dir = config_path.parent / "vendor"
    if not vendor_dir.is_dir():
        return

    value = str(vendor_dir)
    if value in sys.path:
        return
    sys.path.insert(0, value)
    logger.info("[Plugin Process] Added plugin vendor path to sys.path: {}", vendor_dir)


def _is_target_module_missing(exc: ModuleNotFoundError, module_path: str) -> bool:
    """判断缺失的是目标插件模块本身，而不是插件内部依赖。"""

    missing_name = getattr(exc, "name", None)
    return bool(missing_name and (missing_name == module_path or module_path.startswith(f"{missing_name}.")))


def _ensure_plugins_namespace(plugin_root: Path, logger: Any) -> None:
    """Create a namespace without exposing sibling installations."""

    existing = sys.modules.get("plugins")
    if existing is None or getattr(existing, "__path__", None) is None:
        module = types.ModuleType("plugins")
        module.__package__ = "plugins"
        module.__path__ = []
        module.__spec__ = importlib.machinery.ModuleSpec("plugins", loader=None, is_package=True)
        if module.__spec__ is not None:
            module.__spec__.submodule_search_locations = module.__path__
        sys.modules["plugins"] = module
        logger.info("[Plugin Process] Created plugins namespace for: {}", plugin_root)
        return

    existing.__path__ = []
    spec = getattr(existing, "__spec__", None)
    if spec is None:
        spec = importlib.machinery.ModuleSpec("plugins", loader=None, is_package=True)
        existing.__spec__ = spec
    spec.submodule_search_locations = existing.__path__
    logger.debug("[Plugin Process] Cleared inherited plugins namespace search paths")

def _evict_plugin_module_tree(plugin_module_path: str) -> None:
    """Remove one synthetic plugin package without disturbing its siblings."""

    loaded_module = sys.modules.get(plugin_module_path)
    module_prefix = f"{plugin_module_path}."
    for loaded_name in tuple(sys.modules):
        if loaded_name == plugin_module_path or loaded_name.startswith(module_prefix):
            sys.modules.pop(loaded_name, None)

    parent_name, _, child_name = plugin_module_path.rpartition(".")
    parent_module = sys.modules.get(parent_name)
    if parent_module is None:
        return
    bound_child = getattr(parent_module, child_name, None)
    if bound_child is not None and (
        bound_child is loaded_module
        or getattr(bound_child, "__name__", None) == plugin_module_path
    ):
        delattr(parent_module, child_name)


def evict_cached_plugin_modules(plugin_id: str) -> None:
    """Invalidate both import aliases for one plugin after its files change."""

    if not plugin_id or "." in plugin_id:
        return
    _evict_plugin_module_tree(f"plugins.{plugin_id}")
    _evict_plugin_module_tree(f"plugin.plugins.{plugin_id}")
    importlib.invalidate_caches()


def _module_is_loaded_from_plugin_dir(module: Any, plugin_dir: Path) -> bool:
    """Return whether an imported package already represents the selected source."""

    loaded_file = getattr(module, "__file__", None)
    if isinstance(loaded_file, str) and loaded_file:
        try:
            Path(loaded_file).resolve().relative_to(plugin_dir)
            return True
        except (OSError, ValueError):
            pass
    for loaded_path in getattr(module, "__path__", ()):
        try:
            if Path(loaded_path).resolve() == plugin_dir:
                return True
        except (OSError, TypeError, ValueError):
            continue
    return False


def _evict_cached_plugin_source(module_path: str, config_path: Path, logger: Any) -> None:
    """Remove an inherited same-ID package before importing the effective source."""

    parts = module_path.split(".")
    if len(parts) >= 2 and parts[0] == "plugins":
        plugin_id = parts[1]
    elif len(parts) >= 3 and parts[:2] == ["plugin", "plugins"]:
        plugin_id = parts[2]
    else:
        return
    try:
        plugin_dir = config_path.resolve().parent
    except OSError as exc:
        logger.debug("[Plugin Process] Failed to resolve plugin directory for cache eviction: {}", exc)
        return
    if plugin_id != plugin_dir.name:
        return

    package_prefixes = (f"plugins.{plugin_id}", f"plugin.plugins.{plugin_id}")
    loaded_tree = [
        (name, loaded)
        for name, loaded in tuple(sys.modules.items())
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in package_prefixes)
    ]
    if loaded_tree and all(
        _module_is_loaded_from_plugin_dir(loaded, plugin_dir) for _name, loaded in loaded_tree
    ):
        return

    evicted = [name for name, _loaded in loaded_tree]
    for name in evicted:
        sys.modules.pop(name, None)
    if evicted:
        logger.info(
            "[Plugin Process] Evicted cached plugin package before source import: {}",
            ", ".join(package_prefixes),
        )


def _import_current_plugin_from_config(module_path: str, config_path: Path, logger: Any) -> Any | None:
    """在命名空间导入失败时，按当前 plugin.toml 同目录直接加载插件包。"""

    parts = module_path.split(".")
    if len(parts) < 2 or parts[0] != "plugins":
        return None

    try:
        plugin_dir = config_path.resolve().parent
    except Exception as exc:
        logger.debug("[Plugin Process] Failed to resolve plugin directory for import fallback: {}", exc)
        return None

    if parts[1] != plugin_dir.name:
        logger.debug(
            "[Plugin Process] Import fallback skipped because module '{}' does not match plugin dir '{}'",
            module_path,
            plugin_dir.name,
        )
        return None

    source_file = plugin_dir / "__init__.py"

    plugin_root = plugin_dir.parent
    _ensure_plugins_namespace(plugin_root, logger)
    importlib.invalidate_caches()

    plugin_module_path = ".".join(parts[:2])
    existing = sys.modules.get(plugin_module_path)
    existing_file = getattr(existing, "__file__", None)
    existing_matches = False
    if isinstance(existing_file, str) and existing_file:
        try:
            Path(existing_file).resolve().relative_to(plugin_dir)
            existing_matches = True
        except (OSError, ValueError):
            pass
    if existing is not None and not existing_matches:
        for existing_path in getattr(existing, "__path__", ()):
            try:
                if Path(existing_path).resolve() == plugin_dir:
                    existing_matches = True
                    break
            except (OSError, TypeError, ValueError):
                continue
    if existing is not None and not existing_matches:
        _evict_plugin_module_tree(plugin_module_path)

    if plugin_module_path not in sys.modules:
        if source_file.is_file():
            spec = importlib.util.spec_from_file_location(
                plugin_module_path,
                source_file,
                submodule_search_locations=[str(plugin_dir)],
            )
        else:
            spec = importlib.machinery.ModuleSpec(
                plugin_module_path,
                loader=None,
                is_package=True,
            )
            spec.submodule_search_locations = [str(plugin_dir)]
        if spec is None:
            logger.debug("[Plugin Process] Import could not create spec for: {}", plugin_dir)
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[plugin_module_path] = module
        setattr(sys.modules["plugins"], parts[1], module)
        legacy_parent = importlib.import_module("plugin.plugins")
        legacy_module_path = f"plugin.{plugin_module_path}"
        _evict_plugin_module_tree(legacy_module_path)
        sys.modules[legacy_module_path] = module
        setattr(legacy_parent, parts[1], module)
        try:
            if spec.loader is not None:
                spec.loader.exec_module(module)
        except Exception:
            _evict_plugin_module_tree(plugin_module_path)
            _evict_plugin_module_tree(legacy_module_path)
            raise

    if len(parts) > 2:
        try:
            return importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            if _is_target_module_missing(exc, module_path):
                return None
            raise
    return sys.modules[plugin_module_path]


def _import_plugin_module(module_path: str, config_path: Path | None, logger: Any) -> Any:
    """导入插件模块，并在用户插件命名空间失效时使用当前插件目录兜底。

    *config_path* 为空时（例如运行时启用扩展却拿不到 plugin.toml 路径），跳过兜底，
    退化为普通 ``import_module`` 行为。
    """

    if config_path is not None and (
        module_path.startswith("plugins.") or module_path.startswith("plugin.plugins.")
    ):
        _evict_cached_plugin_source(module_path, config_path, logger)
    if config_path is not None and module_path.startswith("plugins."):
        configured_module = _import_current_plugin_from_config(
            module_path,
            config_path,
            logger,
        )
        if configured_module is not None:
            return configured_module

    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if config_path is None or not _is_target_module_missing(exc, module_path):
            raise
        fallback_module = _import_current_plugin_from_config(module_path, config_path, logger)
        if fallback_module is None:
            raise
        return fallback_module


async def _handle_config_update_command(
    msg: dict,
    ctx: Any,
    events_by_type: dict,
    plugin_id: str,
    res_sender: Any,
    logger: Any,
) -> None:
    """处理 CONFIG_UPDATE 命令 - 配置热更新。

    ``res_sender`` must expose ``.put(obj, timeout=...)`` — either a
    :class:`ChannelSender` or legacy ``mp.Queue``.
    """
    req_id = msg.get("req_id", "unknown")
    new_config = msg.get("config", {})
    mode = msg.get("mode", "temporary")  # temporary | permanent
    profile_name = msg.get("profile")
    
    ret_payload = {"req_id": req_id, "success": False, "data": None, "error": None}
    
    try:
        logger.info(
            "[Plugin Process] Received CONFIG_UPDATE: plugin_id={}, mode={}, req_id={}",
            plugin_id, mode, req_id,
        )
        
        # 保存旧配置用于回调（深拷贝，避免嵌套结构共享引用）
        old_config = {}
        if hasattr(ctx, '_effective_config'):
            old_config = copy.deepcopy(ctx._effective_config) if ctx._effective_config else {}
        
        # 更新进程内配置缓存
        if hasattr(ctx, '_effective_config') and ctx._effective_config is not None:
            # 合并配置（深度合并）
            _deep_merge(ctx._effective_config, new_config)
            logger.debug("[Plugin Process] Config cache updated")
        else:
            ctx._effective_config = new_config

        refresh_runtime_config = getattr(ctx, "_refresh_instance_runtime_config", None)
        if callable(refresh_runtime_config) and isinstance(ctx._effective_config, dict):
            refresh_runtime_config(ctx._effective_config)
        
        # 触发 config_change 生命周期事件（如果存在）
        lifecycle_events = events_by_type.get("lifecycle", {})
        config_change_handler = lifecycle_events.get("config_change")
        
        if config_change_handler:
            logger.debug("[Plugin Process] Triggering config_change lifecycle event")
            try:
                with ctx._handler_scope("lifecycle.config_change"):
                    result = config_change_handler(
                        old_config=old_config,
                        new_config=ctx._effective_config,
                        mode=mode,
                    )
                    if inspect.isawaitable(result):
                        await result
                logger.info("[Plugin Process] config_change handler executed successfully")
            except Exception as e:
                logger.exception("[Plugin Process] config_change handler failed")
                # 回滚配置到变更前状态
                ctx._effective_config = old_config
                if callable(refresh_runtime_config) and isinstance(old_config, dict):
                    refresh_runtime_config(old_config)
                logger.debug("[Plugin Process] Config rolled back after handler failure")
                ret_payload["error"] = f"config_change handler failed: {e}"
                res_sender.put(ret_payload, timeout=10.0)
                return
        
        if mode == "permanent":
            logger.warning(
                "[Plugin Process] CONFIG_UPDATE permanent mode requested but persistence is not implemented: plugin_id={}, profile={} req_id={}",
                plugin_id, profile_name, req_id,
            )
            ret_payload["success"] = False
            ret_payload["error"] = "permanent mode persistence is not implemented"
            ret_payload["data"] = {
                "mode": mode,
                "config_applied": True,
                "handler_called": config_change_handler is not None,
                "permanent_not_implemented": True,
            }
            try:
                res_sender.put(ret_payload, timeout=10.0)
            except Exception:
                logger.exception("[Plugin Process] Failed to send CONFIG_UPDATE response")
            return

        ret_payload["success"] = True
        ret_payload["data"] = {
            "mode": mode,
            "config_applied": True,
            "handler_called": config_change_handler is not None,
        }
        
        logger.info("[Plugin Process] CONFIG_UPDATE completed successfully, mode={}", mode)
        
    except Exception as e:
        logger.exception("[Plugin Process] CONFIG_UPDATE failed")
        ret_payload["error"] = str(e)
    
    try:
        res_sender.put(ret_payload, timeout=10.0)
    except Exception:
        logger.exception("[Plugin Process] Failed to send CONFIG_UPDATE response")


def _deep_merge(base: dict, updates: dict) -> None:
    """
    深度合并字典，将 updates 合并到 base 中。
    
    Args:
        base: 基础字典（会被修改）
        updates: 更新字典
    """
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


async def _run_with_model_client_cleanup(ctx: PluginContext, awaitable: Any) -> Any:
    """Release this loop's HTTP client before an asyncio.run loop closes."""
    try:
        return await awaitable
    finally:
        models = getattr(ctx, "_models", None)
        if models is not None:
            try:
                await models.aclose()
            except Exception as exc:
                # Cleanup must not replace a plugin result or hide its error.
                logger.warning("[Plugin Process] Model client cleanup failed: {}", type(exc).__name__)


def _plugin_process_runner(
    plugin_id: str,
    entry_point: str,
    config_path: Path,
    downlink_endpoint: str,
    uplink_endpoint: str,
    uplink_token: str = "",
    stop_event: Any | None = None,
    startup_options: dict[str, object] | None = None,
    message_uplink_endpoint: str = "",
    image_uplink_endpoint: str | None = None,
    *,
    model_gateway_options: dict[str, str] | None = None,
) -> None:
    """独立进程中的运行函数。通过 ZMQ 与宿主进程通信。"""
    uplink_token = str(uplink_token or "").strip()
    if not uplink_token:
        raise ValueError("Plugin child process requires an uplink token")

    # 保存进程级 stop event
    process_stop_event = stop_event
    startup_failure_policy = str((startup_options or {}).get("startup_failure") or "warn").strip().lower()
    
    # 初始化：探测项目根目录、配置 logger
    project_root = _find_project_root(config_path)
    logger = _setup_plugin_logger(plugin_id, project_root)
    
    # 设置 logging 拦截
    try:
        _setup_logging_interception(logger, project_root)
    except Exception as e:
        logger.warning("[Plugin Process] Failed to setup logging interception: {}", e)
    
    # ── ZMQ child-side transport ─────────────────────────────────
    child_transport_kwargs: dict[str, object] = {}
    if message_uplink_endpoint:
        child_transport_kwargs["message_uplink_endpoint"] = (
            message_uplink_endpoint
        )
    if image_uplink_endpoint:
        child_transport_kwargs["image_uplink_endpoint"] = image_uplink_endpoint
    child_transport = ChildTransport(
        downlink_endpoint,
        uplink_endpoint,
        uplink_token,
        **child_transport_kwargs,
    )
    res_sender = child_transport.channel_sender(CH_RES)
    status_sender = child_transport.channel_sender(CH_STS)
    message_sender = child_transport.channel_sender(CH_MSG)
    comm_sender = child_transport.channel_sender(CH_COMM)

    try:
        _prepare_child_plugin_import_roots(logger)
        _prepare_child_current_plugin_import_root(config_path, logger)
        _prepare_child_plugin_vendor_path(config_path, logger)
        try:
            from plugin.settings import BUILTIN_PLUGIN_CONFIG_ROOT
            entry_point = normalize_plugin_entry_point(
                entry_point,
                config_path=config_path,
                builtin_plugin_root=BUILTIN_PLUGIN_CONFIG_ROOT,
            )
        except Exception as e:
            logger.debug("[Plugin Process] Failed to normalize entry point: {}", e)

        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
            logger.info("[Plugin Process] Added project root to sys.path: {}", project_root)
        
        logger.info("[Plugin Process] Starting plugin '{}' from {}", plugin_id, entry_point)
        
        module_path, class_name = entry_point.split(":", 1)
        logger.debug("[Plugin Process] Importing module: {}", module_path)
        mod = _import_plugin_module(module_path, config_path, logger)
        cls = getattr(mod, class_name)
        logger.debug("[Plugin Process] Class loaded: {}", cls.__name__)

        ctx = PluginContext(
            plugin_id=plugin_id,
            logger=logger,
            config_path=config_path,
            status_queue=status_sender,
            message_queue=message_sender,
            _plugin_comm_queue=comm_sender,
            _zmq_ipc_client=None,
            _cmd_queue=None,
            _res_queue=None,
            _response_queue=None,
            _response_pending={},
            _image_transport=child_transport,
            _entry_map=None,
            _instance=None,
            _model_gateway_base_url=(model_gateway_options or {}).get("base_url", ""),
            _model_gateway_token=(model_gateway_options or {}).get("token", ""),
        )
        # The context owns this instance's credential from now on.
        if model_gateway_options is not None:
            model_gateway_options.clear()
        # Cleared until the downlink loop starts reading. Uploads launched by
        # timer / custom-event handlers before that point would have no reader
        # for their reply, so they wait here instead of timing out (Codex).
        ctx._downlink_ready = threading.Event()

        try:
            from plugin.settings import PLUGIN_ZMQ_IPC_ENABLED, PLUGIN_ZMQ_IPC_ENDPOINT

            enabled = os.getenv("NEKO_PLUGIN_ZMQ_IPC_ENABLED")
            if enabled is None:
                use_zmq = bool(PLUGIN_ZMQ_IPC_ENABLED)
            else:
                use_zmq = str(enabled).lower() in ("true", "1", "yes", "on")

            if use_zmq:
                from plugin.utils.zeromq_ipc import ZmqIpcClient
                endpoint = os.getenv("NEKO_PLUGIN_ZMQ_IPC_ENDPOINT", PLUGIN_ZMQ_IPC_ENDPOINT)
                ctx._zmq_ipc_client = ZmqIpcClient(plugin_id=plugin_id, endpoint=endpoint)
                try:
                    logger.info("[Plugin Process] ZeroMQ IPC enabled: {}", endpoint)
                except Exception:
                    pass
        except Exception:
            try:
                logger.warning("[Plugin Process] ZeroMQ IPC enabled but client init failed")
            except Exception:
                pass
            pass

        # Router 是普通插件内部的组合单元，不能作为独立插件入口启动。
        if isinstance(cls, type) and issubclass(cls, PluginRouter):
            logger.error(
                "[Plugin Process] Entry class '{}' is a PluginRouter subclass, not a NekoPluginBase. "
                "Mount the router from a normal plugin with include_router(). "
                "/ 入口类是 PluginRouter；请在普通插件中用 include_router() 挂载。"
                "/ エントリークラスは PluginRouter です。通常プラグインから include_router() でマウントしてください。"
                "Aborting process for plugin '{}'.",
                cls.__name__, plugin_id,
            )
            status_sender.put_nowait({
                "type": "plugin_status",
                "plugin_id": plugin_id,
                "status": "error",
                "error": f"Plugin '{plugin_id}' entry is a PluginRouter, not a NekoPluginBase. "
                         "Mount it from a normal plugin with include_router(). "
                         "/ 请在普通插件中用 include_router() 挂载。"
                         "/ 通常プラグインから include_router() でマウントしてください。",
            })
            try:
                ctx.close()
            except Exception as e:
                logger.debug("[Plugin Process] Context close failed after invalid router entry: {}", e)
            try:
                child_transport.close()
            except Exception:
                pass
            return

        instance = cls(ctx)

        # 获取 freezable 属性列表和持久化模式
        freezable_keys = getattr(instance, "__freezable__", []) or []
        # 优先级：effective config [plugin_state].persist_mode > 类属性 __persist_mode__ > __freeze_mode__(兼容) > 默认 "off"
        persist_mode = getattr(instance, "__persist_mode__", None)
        if persist_mode is None:
            persist_mode = getattr(instance, "__freeze_mode__", "off")  # 向后兼容
        # 从 effective config 读取 persist_mode（包含 profile 覆写）
        try:
            effective_cfg = instance.config.dump_effective_sync(timeout=3.0)
            # 新配置项 [plugin_state]
            state_cfg = effective_cfg.get("plugin_state", {})
            if isinstance(state_cfg, dict):
                cfg_persist_mode = state_cfg.get("persist_mode")
                if cfg_persist_mode in ("auto", "manual", "off"):
                    persist_mode = cfg_persist_mode
                    logger.debug("[Plugin Process] persist_mode from effective config: {}", persist_mode)
            # 向后兼容：旧配置项 [plugin_checkpoint]
            if persist_mode == "off":
                checkpoint_cfg = effective_cfg.get("plugin_checkpoint", {})
                if isinstance(checkpoint_cfg, dict):
                    cfg_freeze_mode = checkpoint_cfg.get("freeze_mode")
                    if cfg_freeze_mode in ("auto", "manual", "off"):
                        persist_mode = cfg_freeze_mode
                        logger.debug("[Plugin Process] persist_mode from legacy plugin_checkpoint config: {}", persist_mode)
        except Exception as e:
            logger.debug("[Plugin Process] Could not read plugin_state from effective config: {}", e)
        # 标记是否从冻结状态恢复（用于触发 unfreeze 生命周期事件）
        ctx._restored_from_freeze = False
        
        if freezable_keys:
            logger.debug("[Plugin Process] Freezable attributes: {}, mode: {}", freezable_keys, persist_mode)
            # 如果有保存的状态，尝试恢复
            state_persistence = getattr(instance, "_state_persistence", None) or getattr(instance, "_freeze_checkpoint", None)
            if state_persistence:
                try:
                    def _unwrap_state_result(op_name: str, result_obj: object) -> object:
                        is_ok = getattr(result_obj, "is_ok", None)
                        if callable(is_ok):
                            if is_ok():
                                return getattr(result_obj, "value", None)
                            error_obj = getattr(result_obj, "error", None)
                            logger.warning(
                                "[Plugin Process] State persistence {} failed: {}",
                                op_name,
                                error_obj,
                            )
                            raise RuntimeError(f"state persistence {op_name} failed: {error_obj}")
                        return result_obj

                    has_saved_state_result = asyncio.run(state_persistence.has_saved_state())
                    has_saved_state = _unwrap_state_result("has_saved_state", has_saved_state_result)
                    if bool(has_saved_state):
                        logger.debug("[Plugin Process] Restoring saved state...")
                        load_result_obj = asyncio.run(state_persistence.load(instance))
                        load_result = _unwrap_state_result("load", load_result_obj)
                        if bool(load_result):
                            clear_result_obj = asyncio.run(state_persistence.clear())  # 恢复后清除
                            clear_result = _unwrap_state_result("clear", clear_result_obj)
                            if not bool(clear_result):
                                raise RuntimeError("state persistence clear returned falsy result")
                            ctx._restored_from_freeze = True  # 标记为从冻结恢复
                except Exception as e:
                    logger.warning("[Plugin Process] Failed to restore saved state: {}", e)
        
        def _should_persist(method) -> bool:
            """判断是否应该保存状态"""
            if not freezable_keys or persist_mode == "off":
                return False
            # 检查方法级别的 persist 配置
            method_persist = getattr(method, PERSIST_ATTR, None)
            if method_persist is not None:
                return method_persist  # 方法显式指定
            # 遵循类级别配置
            return persist_mode == "auto"

        entry_map: Dict[str, Any] = {}
        entry_meta_map: Dict[str, Any] = {}  # 存储 EventMeta 用于获取自定义配置（如 timeout）
        events_by_type: Dict[str, Dict[str, Any]] = {}
        ui_context_map: Dict[str, Any] = {}

        def _get_ui_context_meta(member: Any) -> dict[str, Any] | None:
            candidates: list[Any] = [member]
            if hasattr(member, "__func__"):
                candidates.append(member.__func__)
            current = getattr(member, "__wrapped__", None)
            seen: set[int] = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                candidates.append(current)
                if hasattr(current, "__func__"):
                    candidates.append(current.__func__)
                current = getattr(current, "__wrapped__", None)
            for candidate in candidates:
                meta = getattr(candidate, UI_CONTEXT_META_ATTR, None)
                if isinstance(meta, dict):
                    return dict(meta)
            return None

        def _rebuild_ui_context_map() -> None:
            ui_context_map.clear()
            for _name, member in inspect.getmembers(instance, predicate=callable):
                meta = _get_ui_context_meta(member)
                if not meta:
                    continue
                context_id = str(meta.get("id") or "main").strip() or "main"
                ui_context_map[context_id] = member

        def _serialize_ui_context_result(result: Any) -> Dict[str, Any]:
            schema: Dict[str, Any] | None = None
            data: Any
            model_dump = getattr(result, "model_dump", None)
            if callable(model_dump):
                # normalize_structured_data 自己带 model_dump 的 mode 回退（手写的
                # 实现未必收 mode 关键字），并递归把 Mapping / dataclass / tuple
                # 摊成基础结构。别再手搓一份。
                data = normalize_structured_data(result)
                schema = model_schema_from_type(type(result))
            elif isinstance(result, (dict, list)):
                data = normalize_structured_data(result)
            else:
                data = {"value": normalize_structured_data(result)}
            # 在子进程就压成 JSON-safe：这个值要先过 pickle 送回父进程，再进
            # FastAPI 的响应体。锁、socket、async generator 之类会让整条回复
            # （连同 actions 白名单）发不出去，父进程只能干等到超时；父进程
            # import 不到的自定义类同样会在 pickle.loads 侧炸。在这里失败则由
            # 调用点降级成一次普通的 context_error，跟 provider 抛错同构。
            data = json.loads(json.dumps(data, default=str, ensure_ascii=False))
            return {"state": data, "state_schema": schema}

        def _get_ui_action_meta(member: Any) -> dict[str, Any] | None:
            candidates: list[Any] = [member]
            if hasattr(member, "__func__"):
                candidates.append(member.__func__)
            current = getattr(member, "__wrapped__", None)
            seen: set[int] = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                candidates.append(current)
                if hasattr(current, "__func__"):
                    candidates.append(current.__func__)
                current = getattr(current, "__wrapped__", None)
            for candidate in candidates:
                meta = getattr(candidate, UI_ACTION_META_ATTR, None)
                if isinstance(meta, dict):
                    return dict(meta)
            return None

        def _collect_ui_actions() -> list[dict[str, Any]]:
            actions: list[dict[str, Any]] = []
            for entry_id, handler in entry_map.items():
                # 逐条容错：一个 entry 的元数据坏掉只该丢掉这一个 action，
                # 不能让整张授权白名单空掉——空白名单会让面板上每个按钮变成 403。
                try:
                    ui_meta = _get_ui_action_meta(handler)
                    if not ui_meta:
                        continue
                    entry_meta = entry_meta_map.get(entry_id)
                    action_id = str(ui_meta.get("id") or entry_id)
                    raw_schema = getattr(entry_meta, "input_schema", None)
                    actions.append({
                        "id": action_id,
                        "entry_id": entry_id,
                        "label": ui_meta.get("label") or getattr(entry_meta, "name", entry_id),
                        "description": getattr(entry_meta, "description", ""),
                        # input_schema 是插件写的，可能是字符串之类的非 mapping。
                        "input_schema": dict(raw_schema) if isinstance(raw_schema, dict) else {},
                        "icon": ui_meta.get("icon"),
                        "tone": ui_meta.get("tone") or "default",
                        "group": ui_meta.get("group"),
                        "order": int(ui_meta.get("order") or 0),
                        "confirm": ui_meta.get("confirm") or False,
                        "refresh_context": bool(ui_meta.get("refresh_context", True)),
                    })
                except Exception:
                    logger.exception("Skipping malformed UI action metadata for entry '{}'", entry_id)
            actions.sort(key=lambda item: (str(item.get("group") or ""), int(item.get("order") or 0), str(item.get("label") or item.get("id"))))
            # label / confirm 等字段是插件写的，可能是任意对象。整条回复要先过
            # pickle 送回父进程、再进 HTTP 响应体，这里压平就不会有「白名单本身
            # 发不出去」的情况。先过 normalize_structured_data：confirm 的声明
            # 类型是 Mapping，非 dict 的 Mapping 直接交给 json.dumps 会被
            # default=str 拍成一句没用的字符串。
            return json.loads(
                json.dumps(normalize_structured_data(actions), default=str, ensure_ascii=False)
            )

        def _rebuild_entry_map() -> None:
            """重建 entry_map 与 events_by_type。"""
            collected = instance.collect_entries(wrap_with_hooks=True)
            entry_map.clear()
            entry_meta_map.clear()
            events_by_type.clear()
            for eid, eh in collected.items():
                entry_map[eid] = eh.handler
                entry_meta_map[eid] = eh.meta
                etype = getattr(eh.meta, "event_type", "plugin_entry")
                events_by_type.setdefault(etype, {})
                events_by_type[etype][eid] = eh.handler
            ctx._entry_map = entry_map

        # 优先使用 collect_entries() 获取入口点（支持 Hook 包装）
        if hasattr(instance, "collect_entries") and callable(instance.collect_entries):
            try:
                collected = instance.collect_entries(wrap_with_hooks=True)
                for eid, event_handler in collected.items():
                    entry_map[eid] = event_handler.handler
                    entry_meta_map[eid] = event_handler.meta
                    etype = getattr(event_handler.meta, "event_type", "plugin_entry")
                    events_by_type.setdefault(etype, {})
                    events_by_type[etype][eid] = event_handler.handler
                logger.info("Plugin entries collected via collect_entries(): {}", list(entry_map.keys()))
            except Exception as e:
                logger.warning("Failed to collect entries via collect_entries(): {}, falling back to scan", e)
                entry_map.clear()
                entry_meta_map.clear()
                events_by_type.clear()
        
        # 如果 collect_entries 失败或不存在，回退到扫描方法
        if not entry_map:
            for name, member in inspect.getmembers(instance, predicate=callable):
                if name.startswith("_") and not hasattr(member, EVENT_META_ATTR):
                    continue
                event_meta = getattr(member, EVENT_META_ATTR, None)
                if not event_meta:
                    wrapped = getattr(member, "__wrapped__", None)
                    if wrapped is not None:
                        event_meta = getattr(wrapped, EVENT_META_ATTR, None)

                if event_meta:
                    eid = getattr(event_meta, "id", name)
                    entry_map[eid] = member
                    entry_meta_map[eid] = event_meta  # 存储 EventMeta 用于获取自定义配置
                    etype = getattr(event_meta, "event_type", "plugin_entry")
                    events_by_type.setdefault(etype, {})
                    events_by_type[etype][eid] = member
                else:
                    entry_map[name] = member
            logger.info("Plugin instance created. Mapped entries: {}", list(entry_map.keys()))
        
        ctx._entry_map = entry_map
        ctx._instance = instance
        _rebuild_ui_context_map()
        if ui_context_map:
            logger.info("Plugin UI contexts collected: {}", list(ui_context_map.keys()))

        # asyncio.Queue fed from the downlink for plugin-to-plugin responses
        _response_inbox: asyncio.Queue = asyncio.Queue()
        ctx._response_queue = _response_inbox
        _startup_pending_downlink: list[tuple[str, dict]] = []

        async def _route_response(msg: Any) -> None:
            dispatch_direct = getattr(ctx, "_dispatch_direct_response", None)
            if callable(dispatch_direct):
                try:
                    if dispatch_direct(msg):
                        return
                except Exception:
                    logger.exception("Failed to dispatch SDK-owned response")
            await _response_inbox.put(msg)

        async def _startup_downlink_pump(stop_event: asyncio.Event) -> None:
            poll_ms = int(QUEUE_GET_TIMEOUT * 1000)
            while not stop_event.is_set():
                result = await child_transport.recv_downlink(timeout_ms=poll_ms)
                if result is None:
                    continue
                ch, msg = result
                if ch == CH_RESP:
                    await _response_inbox.put(msg)
                    continue
                if ch == CH_CMD and isinstance(msg, dict) and msg.get("type") == "STOP":
                    stop_event.set()
                    break
                if isinstance(msg, dict):
                    _startup_pending_downlink.append((ch, msg))

        async def _run_startup_with_downlink(startup_callable: Any) -> None:
            stop_event = asyncio.Event()
            pump_task = asyncio.create_task(_startup_downlink_pump(stop_event), name="startup-downlink-pump")
            try:
                await startup_callable()
            finally:
                stop_event.set()
                pump_task.cancel()
                try:
                    await pump_task
                except asyncio.CancelledError:
                    pass

        # 生命周期：startup
        lifecycle_events = events_by_type.get("lifecycle", {})

        def _send_startup_result(startup_error: str | None) -> None:
            try:
                startup_success = startup_error is None
                startup_data: dict[str, Any] = {
                    "status": "ready" if startup_success else "failed",
                }
                if startup_error is not None:
                    startup_data["startup_error"] = startup_error
                res_sender.put(
                    {
                        "req_id": STARTUP_RESULT_REQ_ID,
                        "success": startup_success,
                        "data": startup_data,
                        "error": startup_error,
                    },
                    timeout=10.0,
                )
            except Exception:
                logger.exception("[Plugin Process] Failed to send startup result")

        startup_fn = lifecycle_events.get("startup")
        startup_error: str | None = None
        if startup_fn:
            try:
                with ctx._handler_scope("lifecycle.startup"):
                    asyncio.run(_run_with_model_client_cleanup(ctx, _run_startup_with_downlink(startup_fn)))
            except (KeyboardInterrupt, SystemExit):
                # 系统级中断，直接抛出
                raise
            except Exception as e:
                error_msg = f"Error in lifecycle.startup: {str(e)}"
                logger.exception(error_msg)
                startup_error = error_msg
                # 记录错误但不中断进程启动
                # 如果启动失败是致命的，可以在这里 raise PluginLifecycleError
        
        # 生命周期：unfreeze（如果是从冻结状态恢复）
        # 通过检查是否有状态被恢复来判断是否是从冻结恢复
        _restored_from_freeze = False
        if freezable_keys:
            state_persistence = getattr(instance, "_state_persistence", None) or getattr(instance, "_freeze_checkpoint", None)
            # 检查 ctx 中是否有恢复标记（由状态恢复逻辑设置）
            _restored_from_freeze = getattr(ctx, "_restored_from_freeze", False)
        
        unfreeze_fn = lifecycle_events.get("unfreeze")
        if unfreeze_fn and _restored_from_freeze:
            try:
                logger.info("[Plugin Process] Executing unfreeze lifecycle (restored from frozen state)...")
                with ctx._handler_scope("lifecycle.unfreeze"):
                    asyncio.run(_run_with_model_client_cleanup(ctx, unfreeze_fn()))
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                error_msg = f"Error in lifecycle.unfreeze: {str(e)}"
                logger.exception(error_msg)

        _send_startup_result(startup_error)
        if startup_error is not None and startup_failure_policy == "fail":
            logger.info("[Plugin Process] Startup failed with policy='fail'; skipping auto-start work")
            try:
                ctx.close()
            except Exception as e:
                logger.debug("[Plugin Process] Context close failed after startup failure: {}", e)
            try:
                child_transport.close()
            except Exception:
                pass
            return

        # 定时任务：timer auto_start interval
        def _run_timer_interval(fn, interval_seconds: int, fn_name: str, stop_event: threading.Event):
            while not stop_event.is_set():
                try:
                    with ctx._handler_scope(f"timer.{fn_name}"):
                        asyncio.run(_run_with_model_client_cleanup(ctx, fn()))
                except (KeyboardInterrupt, SystemExit):
                    # 系统级中断，停止定时任务
                    logger.info("Timer '{}' interrupted, stopping", fn_name)
                    break
                except Exception:
                    logger.exception("Timer '{}' failed", fn_name)
                    # 定时任务失败不应中断循环，继续执行
                stop_event.wait(interval_seconds)

        timer_events = events_by_type.get("timer", {})
        timer_stop_events: list[threading.Event] = []
        for eid, fn in timer_events.items():
            meta = getattr(fn, EVENT_META_ATTR, None)
            if not meta or not getattr(meta, "auto_start", False):
                continue
            mode = getattr(meta, "extra", {}).get("mode")
            if mode == "interval":
                seconds = getattr(meta, "extra", {}).get("seconds", 0)
                if seconds > 0:
                    timer_stop_event = threading.Event()
                    timer_stop_events.append(timer_stop_event)
                    t = threading.Thread(
                        target=_run_timer_interval,
                        args=(fn, seconds, eid, timer_stop_event),
                        daemon=True,
                    )
                    t.start()
                    logger.info("Started timer '{}' every {}s", eid, seconds)

        # 处理自定义事件：自动启动
        def _run_custom_event_auto(fn, fn_name: str, event_type: str):
            """执行自动启动的自定义事件"""
            try:
                with ctx._handler_scope(f"{event_type}.{fn_name}"):
                    asyncio.run(_run_with_model_client_cleanup(ctx, fn()))
            except (KeyboardInterrupt, SystemExit):
                logger.info("Custom event '{}' (type: {}) interrupted", fn_name, event_type)
            except Exception:
                logger.exception("Custom event '{}' (type: {}) failed", fn_name, event_type)

        # 扫描所有自定义事件类型
        for event_type, events in events_by_type.items():
            if event_type in ("plugin_entry", "lifecycle", "message", "timer"):
                continue  # 跳过标准类型
            
            # 这是自定义事件类型
            logger.info("Found custom event type: {} with {} handlers", event_type, len(events))
            for eid, fn in events.items():
                meta = getattr(fn, EVENT_META_ATTR, None)
                if not meta:
                    continue
                
                # 处理自动启动的自定义事件
                if getattr(meta, "auto_start", False):
                    trigger_method = getattr(meta, "extra", {}).get("trigger_method", "auto")
                    if trigger_method == "auto":
                        # 在独立线程中启动
                        t = threading.Thread(
                            target=_run_custom_event_auto,
                            args=(fn, eid, event_type),
                            daemon=True,
                        )
                        t.start()
                        logger.info("Started auto custom event '{}' (type: {})", eid, event_type)

        # ────────────────────────────────────────────────────
        #  Async command loop  (replaces the old sync while-loop)
        # ────────────────────────────────────────────────────

        _STALL_THRESHOLD = 5.0

        async def _run_with_watchdog(coro, label: str, timeout_seconds):
            """Run *coro* with a stall-detection watchdog and timeout."""
            task = asyncio.ensure_future(coro)

            async def _watchdog():
                while not task.done():
                    t0 = time.monotonic()
                    await asyncio.sleep(1.0)
                    wall = time.monotonic() - t0
                    if wall > _STALL_THRESHOLD and not task.done():
                        logger.warning(
                            "[Plugin Process] Event-loop stall: sleep(1s) took {:.1f}s "
                            "in '{}'. Likely blocking I/O in async code.",
                            wall, label,
                        )

            wd = asyncio.create_task(_watchdog())
            try:
                return await asyncio.wait_for(task, timeout=timeout_seconds)
            finally:
                wd.cancel()

        def _resolve_timeout(entry_id: str, requested_timeout: Any = _TIMEOUT_UNSET):
            # _TIMEOUT_UNSET 做 default → 区分 "entry 没配 timeout" 和 "entry 显式 timeout<=0 → None"
            entry_timeout = resolve_entry_timeout(entry_meta_map.get(entry_id), _TIMEOUT_UNSET)
            entry_has_timeout = entry_timeout is not _TIMEOUT_UNSET

            if requested_timeout is not _TIMEOUT_UNSET:
                if requested_timeout is None:
                    # 调用者传 None → 交给 entry 决定
                    return entry_timeout if entry_has_timeout else None
                try:
                    explicit_timeout = float(requested_timeout)
                except (TypeError, ValueError):
                    pass
                else:
                    if explicit_timeout <= 0:
                        return entry_timeout if entry_has_timeout else None
                    # entry 显式禁用 timeout (None) → 尊重，不限时
                    if entry_has_timeout and entry_timeout is None:
                        return None
                    # entry 配了正数 timeout → 取较小值
                    if entry_has_timeout:
                        return min(explicit_timeout, entry_timeout)
                    return explicit_timeout

            if entry_has_timeout:
                return entry_timeout
            return PLUGIN_TRIGGER_TIMEOUT

        async def _save_plugin_state(reason: str) -> None:
            sp = getattr(instance, "_state_persistence", None) or getattr(instance, "_freeze_checkpoint", None)
            if not sp:
                return

            save_method = getattr(sp, "save", None)
            if not callable(save_method):
                return

            try:
                save_result = save_method(instance)
            except TypeError:
                save_result = save_method(instance, freezable_keys, reason=reason)

            if inspect.isawaitable(save_result):
                await save_result

        # run_id → asyncio.Task – used by CANCEL_RUN to propagate cancellation
        _run_tasks: Dict[str, asyncio.Task] = {}

        async def _handle_trigger(msg: dict):
            entry_id = msg.get("entry_id")
            args = msg.get("args", {})
            req_id = msg.get("req_id", "unknown")

            if not entry_id or not isinstance(args, dict):
                res_sender.put(
                    {"req_id": req_id, "success": False, "data": None,
                     "error": "Invalid TRIGGER payload: 'entry_id' required, 'args' must be dict"},
                    timeout=10.0,
                )
                return

            logger.info("[Plugin Process] TRIGGER entry='{}' req_id={}", entry_id, req_id)

            method = (
                entry_map.get(entry_id)
                or getattr(instance, entry_id, None)
                or getattr(instance, f"entry_{entry_id}", None)
            )
            if method is None and hasattr(instance, "collect_entries") and callable(instance.collect_entries):
                try:
                    _rebuild_entry_map()
                    method = (
                        entry_map.get(entry_id)
                        or getattr(instance, entry_id, None)
                        or getattr(instance, f"entry_{entry_id}", None)
                    )
                    if method is not None:
                        logger.info(
                            "[Plugin Process] Rebuilt dynamic entry map and resolved entry='{}' req_id={}",
                            entry_id,
                            req_id,
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to rebuild entry map while resolving '{}' req_id={}: {}",
                        entry_id,
                        req_id,
                        e,
                    )
            ret = {"req_id": req_id, "success": False, "data": None, "error": None}

            run_id = None
            try:
                ctx_obj = args.get("_ctx") if isinstance(args, dict) else None
                if isinstance(ctx_obj, dict):
                    run_id = ctx_obj.get("run_id")
                    lanlan_name = ctx_obj.get("lanlan_name")
                    if lanlan_name:
                        ctx._current_lanlan = str(lanlan_name)
            except Exception:
                pass

            try:
                if not method:
                    raise PluginEntryNotFoundError(plugin_id, entry_id)

                if not (asyncio.iscoroutinefunction(method) or inspect.iscoroutinefunction(method)):
                    ret["error"] = f"Entry '{entry_id}' must be 'async def'. Sync entries are not supported."
                    return

                requested_timeout = msg["timeout"] if "timeout" in msg else _TIMEOUT_UNSET
                timeout_seconds = _resolve_timeout(entry_id, requested_timeout)
                call_args = prepare_entry_kwargs(
                    plugin_id=plugin_id,
                    entry_id=entry_id,
                    handler=method,
                    meta=entry_meta_map.get(entry_id),
                    args=args,
                )

                with ctx._handler_scope(f"plugin_entry.{entry_id}"), ctx._run_scope(run_id):
                    result = await _run_with_watchdog(
                        method(**call_args), entry_id, timeout_seconds,
                    )

                if hasattr(result, "is_ok") and callable(result.is_ok):
                    if result.is_ok():
                        ret["success"] = True
                        ret["data"] = result.value
                    else:
                        ret["success"] = False
                        err = result.error
                        ret["error"] = str(err) if err is not None else "Unknown error"
                else:
                    ret["success"] = True
                    ret["data"] = result

                if _should_persist(method):
                    try:
                        await _save_plugin_state("auto")
                    except Exception:
                        pass

            except asyncio.CancelledError:
                ret["error"] = "Execution cancelled"
            except asyncio.TimeoutError:
                requested_timeout = msg["timeout"] if "timeout" in msg else _TIMEOUT_UNSET
                resolved_timeout = _resolve_timeout(entry_id, requested_timeout)
                logger.error("Entry '{}' timed out after {}s", entry_id, resolved_timeout)
                ret["error"] = f"Execution timed out after {resolved_timeout}s"
            except PluginError as e:
                logger.warning("Plugin error executing '{}': {}", entry_id, e)
                ret["error"] = str(e)
            except Exception as e:
                logger.exception("Unexpected error executing '{}'", entry_id)
                ret["error"] = f"Unexpected error: {e}"
            finally:
                if run_id:
                    _run_tasks.pop(run_id, None)
                try:
                    res_sender.put(ret, timeout=10.0)
                except Exception:
                    logger.exception("Failed to send response for req_id={}", req_id)

        async def _handle_trigger_custom(msg: dict):
            event_type = msg.get("event_type")
            event_id = msg.get("event_id")
            args = msg.get("args", {})
            req_id = msg.get("req_id", "unknown")

            logger.info("[Plugin Process] TRIGGER_CUSTOM {}.{} req_id={}", event_type, event_id, req_id)

            try:
                ctx_obj = args.get("_ctx") if isinstance(args, dict) else None
                if isinstance(ctx_obj, dict):
                    lanlan_name = ctx_obj.get("lanlan_name")
                    if lanlan_name:
                        ctx._current_lanlan = str(lanlan_name)
            except Exception:
                pass

            custom_events = events_by_type.get(event_type, {})
            method = custom_events.get(event_id)
            ret = {"req_id": req_id, "success": False, "data": None, "error": None}

            try:
                if not method:
                    ret["error"] = f"Custom event '{event_type}.{event_id}' not found"
                    return

                if not asyncio.iscoroutinefunction(method):
                    ret["error"] = f"Custom event '{event_type}.{event_id}' must be 'async def'."
                    return

                with ctx._handler_scope(f"{event_type}.{event_id}"):
                    result = await _run_with_watchdog(
                        method(**args),
                        f"{event_type}.{event_id}",
                        PLUGIN_TRIGGER_TIMEOUT,
                    )

                if hasattr(result, "is_ok") and callable(result.is_ok):
                    if result.is_ok():
                        ret["success"] = True
                        ret["data"] = result.value
                    else:
                        ret["success"] = False
                        err = result.error
                        ret["error"] = str(err) if err is not None else "Unknown error"
                else:
                    ret["success"] = True
                    ret["data"] = result

            except asyncio.CancelledError:
                ret["error"] = "Execution cancelled"
            except asyncio.TimeoutError:
                logger.error("Custom event {}.{} timed out", event_type, event_id)
                ret["error"] = f"Custom event timed out after {PLUGIN_TRIGGER_TIMEOUT}s"
            except Exception as e:
                logger.exception("Error executing custom event {}.{}", event_type, event_id)
                ret["error"] = str(e)
            finally:
                try:
                    res_sender.put(ret, timeout=10.0)
                except Exception:
                    logger.exception("Failed to send response for req_id={}", req_id)

        async def _handle_ui_context(msg: dict):
            req_id = msg.get("req_id", "unknown")
            context_id = str(msg.get("context_id") or "main")
            ret = {"req_id": req_id, "success": False, "data": None, "error": None}
            # 在 try 之外绑定：下面任何一步炸了，兜底分支和 finally 都还要用它。
            actions: list[dict[str, Any]] = []
            context_payload: Dict[str, Any] = {"state": {}, "state_schema": None}
            context_error: str | None = None
            try:
                # actions 只从 @ui.action 的装饰器元数据推导，不跑插件的 provider。
                # 它必须独立于插件自己写的 @ui.context：provider 缺失、抛错或超时
                # 时，宿主仍然要拿得到 api.call 的授权白名单，否则面板上每个按钮
                # 都会返回一条与真实原因无关的 500。
                actions = _collect_ui_actions()
                provider_budget = _ui_context_provider_budget(msg.get("timeout"))

                provider = ui_context_map.get(context_id)
                if provider is None:
                    # rebuild 会 inspect.getmembers 插件实例，可能触发插件自己的
                    # __getattr__/property 抛错。失败等同于「找不到 provider」，
                    # 不该把已经算好的白名单一起赔进去。
                    try:
                        _rebuild_ui_context_map()
                    except Exception:
                        logger.exception("Failed to rebuild UI context map for '{}'", context_id)
                    provider = ui_context_map.get(context_id)

                if provider is None:
                    context_error = f"UI context '{context_id}' not found"
                    logger.warning(
                        "UI context '{}' not found; serving actions without state", context_id,
                    )
                else:
                    try:
                        result = provider()
                        completed = True
                        if inspect.isawaitable(result):
                            completed, result = await _await_within_budget(result, provider_budget)
                        if completed:
                            context_payload = _serialize_ui_context_result(result)
                        else:
                            logger.warning(
                                "UI context '{}' timed out after {}s", context_id, provider_budget,
                            )
                            context_error = (
                                f"UI context '{context_id}' timed out after {provider_budget}s"
                            )
                    except Exception as e:
                        logger.exception("Failed to execute UI context '{}'", context_id)
                        context_error = str(e) or type(e).__name__

                ret["success"] = True
                ret["data"] = {
                    **context_payload,
                    "actions": actions,
                    "context_error": context_error,
                }
            except Exception as e:
                logger.exception("Failed to build UI context '{}'", context_id)
                reason = str(e) or type(e).__name__
                if actions:
                    # 白名单已经算出来了，就别让它陪葬——面板还能用，原因走 warning。
                    ret["success"] = True
                    ret["data"] = {
                        "state": {},
                        "state_schema": None,
                        "actions": actions,
                        "context_error": reason,
                    }
                else:
                    # 一个 action 都没拿到时才判失败：发一张空白名单只会让每个
                    # 按钮变成 403，比 500 更难查。
                    ret["error"] = reason
            finally:
                try:
                    res_sender.put(ret, timeout=10.0)
                except Exception:
                    logger.exception("Failed to send UI context response for req_id={}", req_id)

        async def _async_command_loop():
            poll_ms = int(QUEUE_GET_TIMEOUT * 1000)
            on_command_loop_start = getattr(instance, "_on_command_loop_start", None)
            if callable(on_command_loop_start):
                try:
                    with ctx._handler_scope("lifecycle.command_loop_start"):
                        result = on_command_loop_start()
                        if inspect.isawaitable(result):
                            await result
                except Exception:
                    logger.exception("[Plugin Process] _on_command_loop_start failed")

            # Downlink is live from here: everything below reads replies. Set
            # AFTER _on_command_loop_start, because that hook can await for an
            # arbitrary time and uploads launched during it would still have no
            # reader. Timer and custom-event threads started earlier block on
            # this instead of sending into a void (Codex).
            try:
                ctx._downlink_ready.set()
            except Exception:
                logger.exception("[Plugin Process] failed to mark downlink ready")

            while True:
                try:
                    if process_stop_event is not None and process_stop_event.is_set():
                        break
                except Exception:
                    pass

                try:
                    if _startup_pending_downlink:
                        result = _startup_pending_downlink.pop(0)
                    else:
                        result = await child_transport.recv_downlink(timeout_ms=poll_ms)
                except asyncio.CancelledError:
                    logger.info("[Plugin Process] Command loop cancelled, shutting down")
                    break
                if result is None:
                    continue
                ch, msg = result

                # Plugin-to-plugin responses arrive on the downlink tagged CH_RESP
                if ch == CH_RESP:
                    await _route_response(msg)
                    continue

                if ch != CH_CMD or not isinstance(msg, dict):
                    continue
                msg_type = msg.get("type")
                if not msg_type:
                    continue

                # ── STOP ──
                if msg_type == "STOP":
                    break

                # ── CANCEL_RUN ──
                if msg_type == "CANCEL_RUN":
                    run_id = msg.get("run_id")
                    task = _run_tasks.get(run_id) if run_id else None
                    if task and not task.done():
                        task.cancel()
                        logger.info("[Plugin Process] Cancel sent for run_id={}", run_id)
                    continue

                # ── FREEZE ──
                if msg_type == "FREEZE":
                    req_id = msg.get("req_id", "unknown")
                    logger.info("[Plugin Process] FREEZE req_id={}", req_id)
                    ctx._image_uploads_blocked = True
                    ret = {"req_id": req_id, "success": False, "data": None, "error": None}
                    try:
                        freeze_fn = lifecycle_events.get("freeze")
                        if freeze_fn:
                            with ctx._handler_scope("lifecycle.freeze"):
                                await freeze_fn()
                        if freezable_keys:
                            await _save_plugin_state("freeze")
                        ret["success"] = True
                        ret["data"] = {"frozen": True, "freezable_keys": freezable_keys}
                    except Exception as e:
                        logger.exception("[Plugin Process] Freeze failed")
                        ret["error"] = str(e)
                    res_sender.put(ret, timeout=10.0)
                    if ret["success"]:
                        break
                    ctx._image_uploads_blocked = False
                    continue

                # ── BUS_CHANGE ──
                if msg_type == "BUS_CHANGE":
                    try:
                        dispatch_bus_change(
                            sub_id=str(msg.get("sub_id") or ""),
                            bus=str(msg.get("bus") or ""),
                            op=str(msg.get("op") or ""),
                            delta=msg.get("delta") if isinstance(msg.get("delta"), dict) else None,
                        )
                    except Exception as e:
                        logger.debug("Failed to dispatch bus change: {}", e)
                    continue

                # ── CONFIG_UPDATE ──
                if msg_type == "CONFIG_UPDATE":
                    # The loop awaits this handler INLINE, so it cannot route
                    # anyone else's IMAGE_UPLOAD_RESULT until it returns. A
                    # timer or custom-event upload running concurrently would
                    # otherwise send into a loop that is not reading and could
                    # only time out (Codex P2) -- the same failure as the
                    # startup window, from the same cause.
                    #
                    # So the flag means "responses are being routed right now",
                    # not "the loop started once". Cleared for the duration and
                    # restored in a finally, so a handler that raises cannot
                    # leave uploads blocked for the rest of the process.
                    ctx._downlink_ready.clear()
                    try:
                        await _handle_config_update_command(
                            msg=msg, ctx=ctx, events_by_type=events_by_type,
                            plugin_id=plugin_id, res_sender=res_sender,
                            logger=logger,
                        )
                    finally:
                        ctx._downlink_ready.set()
                    continue

                # ── TRIGGER_CUSTOM ──
                if msg_type == "TRIGGER_CUSTOM":
                    req_id = str(msg.get("req_id") or uuid.uuid4())
                    task_key = f"custom:{req_id}"
                    task = asyncio.create_task(_handle_trigger_custom(msg))
                    _run_tasks[task_key] = task
                    task.add_done_callback(lambda _t, key=task_key: _run_tasks.pop(key, None))
                    continue

                # ── UI_CONTEXT ──
                if msg_type == "UI_CONTEXT":
                    req_id = str(msg.get("req_id") or uuid.uuid4())
                    task_key = f"ui_context:{req_id}"
                    task = asyncio.create_task(_handle_ui_context(msg))
                    _run_tasks[task_key] = task
                    task.add_done_callback(lambda _t, key=task_key: _run_tasks.pop(key, None))
                    continue

                # ── TRIGGER ──
                if msg_type == "TRIGGER":
                    run_id = None
                    try:
                        ctx_obj = msg.get("args", {}).get("_ctx")
                        if isinstance(ctx_obj, dict):
                            run_id = ctx_obj.get("run_id")
                    except Exception:
                        pass
                    task = asyncio.create_task(_handle_trigger(msg))
                    if run_id:
                        _run_tasks[run_id] = task
                    continue

        async def _command_loop_with_cleanup():
            try:
                await _async_command_loop()
            finally:
                # Also clean up when transport/handler dispatch raises. Close
                # model clients only after their request tasks have unwound.
                tasks = list(_run_tasks.values())
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    _run_tasks.clear()

        asyncio.run(_run_with_model_client_cleanup(ctx, _command_loop_with_cleanup()))

        # 触发生命周期：shutdown（尽力而为），并停止所有定时任务
        try:
            for ev in timer_stop_events:
                try:
                    ev.set()
                except Exception:
                    pass
        except Exception:
            pass

        shutdown_fn = lifecycle_events.get("shutdown")
        if shutdown_fn:
            try:
                with ctx._handler_scope("lifecycle.shutdown"):
                    result = shutdown_fn()
                    if asyncio.iscoroutine(result):
                        asyncio.run(_run_with_model_client_cleanup(ctx, result))
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                logger.exception("Error in lifecycle.shutdown: {}", e)

        try:
            ctx.close()
        except Exception as e:
            logger.debug("[Plugin Process] Context close failed during shutdown: {}", e)

        child_transport.close()

    except (KeyboardInterrupt, SystemExit):
        logger.info("Plugin process {} interrupted", plugin_id)
        try:
            for ev in timer_stop_events:
                try:
                    ev.set()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            shutdown_fn = lifecycle_events.get("shutdown")
            if shutdown_fn:
                with ctx._handler_scope("lifecycle.shutdown"):
                    result = shutdown_fn()
                    if asyncio.iscoroutine(result):
                        asyncio.run(_run_with_model_client_cleanup(ctx, result))
        except BaseException:
            pass
        try:
            ctx.close()
        except Exception:
            pass
        try:
            child_transport.close()
        except Exception:
            pass
    except Exception as e:
        # 进程崩溃，记录详细信息
        logger.exception("Plugin process {} crashed", plugin_id)
        # 尝试发送错误信息到结果队列（如果可能）
        try:
            res_sender.put({
                "req_id": "CRASH",
                "success": False,
                "data": None,
                "error": f"Process crashed: {str(e)}"
            }, timeout=10.0)
            res_sender.put({
                "req_id": STARTUP_RESULT_REQ_ID,
                "success": False,
                "data": None,
                "error": f"Process crashed: {str(e)}"
            }, timeout=10.0)
        except Exception:
            pass  # 如果队列也坏了，只能放弃
        raise  # 重新抛出，让进程退出


class PluginHost:
    """
    插件进程宿主
    
    负责管理插件进程的完整生命周期：
    - 进程的启动、停止、监控（直接实现）
    - 进程间通信（通过 PluginCommunicationResourceManager）
    """

    def __init__(self, plugin_id: str, entry_point: str, config_path: Path):
        self.plugin_id = plugin_id
        self.entry_point = entry_point
        self.config_path = config_path
        self.logger = logger.bind(plugin_id=plugin_id, host=True)

        # ZMQ transport: 4 PUSH/PULL socket channels replace 5 mp.Queues.
        # 只有 child → host 的三条上行带 uplink token；下行不带凭证。
        self.transport = HostTransport()

        self._process_stop_event: Any = multiprocessing.Event()
        self._startup_options: dict[str, object] = {"startup_failure": "warn"}
        self._model_gateway_token = ""
        self._model_gateway_starting = False
        # Mutated only for launch; no credentials enter environment/config/logs.
        self._model_gateway_options: dict[str, str] = {}

        # Shared response notification primitives must be initialized before
        # forking, otherwise each child creates its own Manager proxies.
        try:
            _ = state.plugin_response_map
        except Exception as e:
            logger.warning(
                "Failed to pre-initialize plugin_response_map for plugin {}: {}",
                plugin_id, e
            )
        try:
            _ = state.plugin_response_notify_event
        except Exception as e:
            logger.warning(
                "Failed to pre-initialize plugin_response_notify_event for plugin {}: {}",
                plugin_id, e
            )

        self.process = multiprocessing.Process(
            target=_plugin_process_runner,
            args=(
                plugin_id,
                entry_point,
                config_path,
                self.transport.downlink_endpoint,
                self.transport.uplink_endpoint,
                getattr(self.transport, "uplink_token", ""),
                self._process_stop_event,
                self._startup_options,
                getattr(self.transport, "message_uplink_endpoint", ""),
            ),
            kwargs={
                "image_uplink_endpoint": getattr(
                    self.transport,
                    "image_uplink_endpoint",
                    None,
                ),
                "model_gateway_options": self._model_gateway_options,
            },
            # Plugin code may spawn subprocesses/Managers; daemon process would forbid that.
            daemon=False,
        )

        self.comm_manager = PluginCommunicationResourceManager(
            plugin_id=plugin_id,
            transport=self.transport,
        )
    
    async def start(
        self,
        message_target_queue=None,
        startup_timeout: float | None = None,
        startup_failure: str = "warn",
    ) -> Any:
        """
        启动后台任务（需要在异步上下文中调用）
        
        Args:
            message_target_queue: 主进程的消息队列，用于接收插件推送的消息
        """
        # Register this plugin's downlink sender so plugin_router can
        # route plugin-to-plugin responses back to the child process.
        state.register_downlink_sender(self.plugin_id, self.comm_manager.send_plugin_response)

        await self.comm_manager.start(message_target_queue=message_target_queue)
        should_wait_for_startup = startup_timeout is not None and startup_timeout > 0
        startup_failure_policy = str(startup_failure or "warn").strip().lower()
        self._startup_options["startup_failure"] = startup_failure_policy

        if self.process.is_alive():
            self.logger.debug(
                "Plugin {} process already running (pid: {})",
                self.plugin_id,
                self.process.pid,
            )
            return

        if should_wait_for_startup:
            await self.comm_manager.prepare_startup_wait()

        start_task: asyncio.Task | None = None
        try:
            from config.network import resolve_user_plugin_base

            self._model_gateway_starting = True
            self._model_gateway_token = model_gateway_access.issue(
                self.plugin_id, self._model_gateway_is_alive,
            )
            self._model_gateway_options.update({
                "base_url": f"{resolve_user_plugin_base()}/api/models/v1",
                "token": self._model_gateway_token,
            })
            _refresh_child_storage_layout_env(self.logger)
            start_task = asyncio.create_task(asyncio.to_thread(self.process.start))
            await asyncio.shield(start_task)
        except asyncio.CancelledError:
            self._revoke_model_gateway_access()
            # Canceling to_thread does not stop spawn or its argument pickling.
            # Keep launch options and transport intact until the worker exits,
            # then stop any child it created. Repeated caller cancellation must
            # not cancel this cleanup or clear arguments underneath the worker.
            cleanup_task = asyncio.create_task(self._finish_cancelled_start(start_task))
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            cleanup_task.result()
            raise
        except BaseException:
            self._revoke_model_gateway_access()
            self.logger.error(
                "Plugin {} process failed to start, shutting down comm_manager",
                self.plugin_id,
            )
            state.remove_downlink_sender(self.plugin_id)
            try:
                self.transport.close()
            except Exception:
                pass
            await self.comm_manager.shutdown(timeout=PLUGIN_SHUTDOWN_TIMEOUT)
            raise
        finally:
            self._model_gateway_starting = False
            # multiprocessing has copied these arguments into the child. Do not
            # leave launch credentials for future forked sibling plugins.
            self._model_gateway_options.clear()
        self.logger.info("Plugin {} process started (pid: {})", self.plugin_id, self.process.pid)

        # 验证进程状态
        if not self.process.is_alive():
            self._revoke_model_gateway_access()
            exitcode = self.process.exitcode
            self.logger.error(
                "Plugin {} process is not alive after startup (exitcode: {})",
                self.plugin_id,
                exitcode,
            )
            state.remove_downlink_sender(self.plugin_id)
            try:
                self.transport.close()
            except Exception:
                pass
            await self.comm_manager.shutdown(timeout=PLUGIN_SHUTDOWN_TIMEOUT)
            raise PluginLifecycleError(
                self.plugin_id,
                "startup",
                f"process exited immediately (exitcode={exitcode})",
            )
        else:
            self.logger.info(
                "Plugin {} process is alive and running (pid: {})",
                self.plugin_id,
                self.process.pid,
            )

        if should_wait_for_startup:
            try:
                startup_result = await self.comm_manager.wait_for_startup(
                    timeout=float(startup_timeout),
                    allow_startup_error=startup_failure_policy != "fail",
                )
                if isinstance(startup_result, dict) and startup_result.get("startup_error"):
                    self.logger.warning(
                        "Plugin {} startup completed with warning: {}",
                        self.plugin_id,
                        startup_result["startup_error"],
                    )
                return startup_result
            except asyncio.CancelledError:
                await self._abort_startup_after_failure(timeout=PLUGIN_SHUTDOWN_TIMEOUT)
                raise
            except TimeoutError as exc:
                self.logger.error(
                    "Plugin {} startup timed out after {}s",
                    self.plugin_id,
                    startup_timeout,
                )
                await self._abort_startup_after_failure(timeout=min(float(startup_timeout), PLUGIN_SHUTDOWN_TIMEOUT))
                raise PluginLifecycleError(
                    self.plugin_id,
                    "startup",
                    f"startup timed out after {startup_timeout}s",
                ) from exc
            except Exception as exc:
                self.logger.error(
                    "Plugin {} startup failed: {}",
                    self.plugin_id,
                    exc,
                )
                await self._abort_startup_after_failure(timeout=PLUGIN_SHUTDOWN_TIMEOUT)
                raise PluginLifecycleError(
                    self.plugin_id,
                    "startup",
                    str(exc),
                ) from exc

    async def _finish_cancelled_start(self, start_task: asyncio.Task | None) -> None:
        if start_task is not None:
            try:
                await start_task
            except Exception:
                # The original caller remains cancelled whether spawn finishes
                # successfully or fails. Both paths still require teardown.
                pass
        await self._abort_startup_after_failure(timeout=PLUGIN_SHUTDOWN_TIMEOUT)

    async def _abort_startup_after_failure(self, timeout: float) -> None:
        self._revoke_model_gateway_access()
        try:
            if getattr(self, "_process_stop_event", None) is not None:
                self._process_stop_event.set()
        except Exception:
            pass
        state.remove_downlink_sender(self.plugin_id)
        try:
            await self.comm_manager.send_stop_command()
        except Exception:
            pass
        try:
            await self.comm_manager.shutdown(timeout=timeout)
        except Exception:
            pass
        await asyncio.to_thread(self._shutdown_process, timeout)
        try:
            self.transport.close()
        except Exception:
            pass
    
    async def shutdown(self, timeout: float = PLUGIN_SHUTDOWN_TIMEOUT) -> None:
        """
        优雅关闭插件
        
        按顺序关闭：
        1. 发送停止命令
        2. 关闭通信资源
        3. 关闭进程
        """
        self.logger.info(f"Shutting down plugin {self.plugin_id}")
        self._revoke_model_gateway_access()

        # Set out-of-band stop event first so the child can exit promptly.
        try:
            if getattr(self, "_process_stop_event", None) is not None:
                self._process_stop_event.set()
        except Exception:
            pass
        
        # 1. 发送停止命令
        await self.comm_manager.send_stop_command()

        # 2. 关闭通信资源
        await self.comm_manager.shutdown(timeout=timeout)

        # 3. Unregister downlink sender & close ZMQ transport
        state.remove_downlink_sender(self.plugin_id)
        self.transport.close()

        # 4. 关闭进程
        success = await asyncio.to_thread(self._shutdown_process, timeout)
        
        if success:
            self.logger.info(f"Plugin {self.plugin_id} shutdown successfully")
        else:
            self.logger.warning(f"Plugin {self.plugin_id} shutdown with issues")
    
    def shutdown_sync(self, timeout: float = PLUGIN_SHUTDOWN_TIMEOUT) -> None:
        """
        同步版本的关闭方法（用于非异步上下文）
        
        注意：这个方法不会等待异步任务完成，建议使用 shutdown()
        """
        self._revoke_model_gateway_access()
        try:
            if getattr(self, "_process_stop_event", None) is not None:
                self._process_stop_event.set()
        except Exception:
            pass

        # 尽量通知通信管理器停止（即使不等待）
        if getattr(self, "comm_manager", None) is not None:
            try:
                _ev = getattr(self.comm_manager, "_shutdown_event", None)
                if _ev is not None:
                    _ev.set()
            except Exception:
                pass

        state.remove_downlink_sender(self.plugin_id)
        self.transport.close()

        self._shutdown_process(timeout=timeout)
    
    async def trigger(self, entry_id: str, args: dict, timeout: float | None = PLUGIN_TRIGGER_TIMEOUT) -> Any:
        """
        触发插件入口点执行
        
        Args:
            entry_id: 入口点 ID
            args: 参数字典
            timeout: 超时时间
        
        Returns:
            插件返回的结果
        """
        self.logger.debug(
            "[PluginHost] Trigger called: plugin_id={}, entry_id={}",
            self.plugin_id,
            entry_id,
        )
        # Parameter values may contain schema-declared sensitive data. Keep only
        # structural diagnostics here; execution still receives the original args.
        self.logger.debug(
            "[PluginHost] Args: type={}, keys={}",
            type(args),
            list(args.keys()) if isinstance(args, dict) else "N/A",
        )
        # 发送 TRIGGER 命令到子进程并等待结果
        # 委托给通信资源管理器处理
        return await self.comm_manager.trigger(entry_id, args, timeout)

    async def cancel_run(self, run_id: str) -> None:
        """Propagate a run cancellation to the child process.

        Fire-and-forget: the child process will set the cancel_event for
        the given *run_id*, causing the running entry to be cancelled if it
        supports cancellation (async entries).
        """
        await self.comm_manager.send_cancel_run(run_id)
    
    async def trigger_custom_event(
        self,
        event_type: str,
        event_id: str,
        args: dict,
        timeout: float = PLUGIN_TRIGGER_TIMEOUT
    ) -> Any:
        """
        触发自定义事件执行
        
        Args:
            event_type: 自定义事件类型（例如 "file_change", "user_action"）
            event_id: 事件ID
            args: 参数字典
            timeout: 超时时间
        
        Returns:
            事件处理器返回的结果
        
        Raises:
            PluginError: 如果事件不存在或执行失败
        """
        self.logger.info(
            "[PluginHost] Trigger custom event: plugin_id={}, event_type={}, event_id={}",
            self.plugin_id,
            event_type,
            event_id,
        )
        return await self.comm_manager.trigger_custom_event(event_type, event_id, args, timeout)

    async def push_bus_change(self, *, sub_id: str, bus: str, op: str, delta: Dict[str, Any] | None = None) -> None:
        await self.comm_manager.push_bus_change(sub_id=sub_id, bus=bus, op=op, delta=delta)

    async def send_config_update(
        self,
        config: Dict[str, Any],
        mode: str = "temporary",
        profile: str | None = None,
        timeout: float = 10.0
    ) -> Dict[str, Any]:
        """
        向子进程发送 CONFIG_UPDATE 命令（配置热更新）。
        
        Args:
            config: 新配置（完整或部分）
            mode: "temporary" | "permanent"
            profile: profile 名称（permanent 模式）
            timeout: 超时时间
        
        Returns:
            {
                "success": bool,
                "config_applied": bool,
                "handler_called": bool,
            }
        """
        req_id = str(uuid.uuid4())
        cmd = {
            "type": "CONFIG_UPDATE",
            "req_id": req_id,
            "config": config,
            "mode": mode,
            "profile": profile,
        }
        return await self.comm_manager._send_command_and_wait(req_id, cmd, timeout, "CONFIG_UPDATE")

    async def get_ui_context(self, context_id: str = "main", timeout: float = 5.0) -> Any:
        return await self.comm_manager.get_ui_context(context_id=context_id, timeout=timeout)

    def is_alive(self) -> bool:
        """检查进程是否存活"""
        alive = self.process.is_alive() and self.process.exitcode is None
        if not alive and not getattr(self, "_model_gateway_starting", False):
            self._revoke_model_gateway_access()
        return alive

    def _model_gateway_is_alive(self) -> bool:
        # A spawned child can make its startup request just before start()
        # returns and multiprocessing publishes the parent-side process handle.
        return self._model_gateway_starting or (
            self.process.is_alive() and self.process.exitcode is None
        )

    def _revoke_model_gateway_access(self) -> None:
        token = getattr(self, "_model_gateway_token", "")
        self._model_gateway_token = ""
        if token:
            model_gateway_access.revoke(token)
    
    def health_check(self) -> HealthCheckResponse:
        """执行健康检查，返回详细状态"""
        alive = self.is_alive()
        exitcode = self.process.exitcode
        pid = self.process.pid if self.process.is_alive() else None
        
        if alive:
            status = "running"
        elif exitcode is None:
            status = "not_started"
        elif exitcode == 0:
            status = "stopped"
        else:
            status = "crashed"
        
        return HealthCheckResponse(
            alive=alive,
            exitcode=exitcode,
            pid=pid,
            status=status,
            communication={
                "pending_requests": len(self.comm_manager._pending_futures),
                "consumer_running": (
                    self.comm_manager._uplink_consumer_task is not None
                    and not self.comm_manager._uplink_consumer_task.done()
                ),
            },
        )
    
    async def freeze(self, timeout: float = PLUGIN_TRIGGER_TIMEOUT) -> Dict[str, Any]:
        """
        冻结插件：保存状态到文件，然后停止进程
        
        Args:
            timeout: 超时时间
        
        Returns:
            冻结结果，包含 frozen 状态和 freezable_keys
        """
        self.logger.info(f"[PluginHost] Freezing plugin {self.plugin_id}")
        
        # 发送 FREEZE 命令并等待结果
        result = await self.comm_manager.send_freeze_command(timeout=timeout)
        
        if result.get("success"):
            self._revoke_model_gateway_access()
            await asyncio.to_thread(self._shutdown_process, timeout)
            await self.comm_manager.shutdown(timeout=timeout)
            state.remove_downlink_sender(self.plugin_id)
            self.transport.close()
            self.logger.info(f"[PluginHost] Plugin {self.plugin_id} frozen successfully")
        else:
            self.logger.error(f"[PluginHost] Plugin {self.plugin_id} freeze failed: {result.get('error')}")
        
        return result
    
    def _shutdown_process(self, timeout: float = PROCESS_SHUTDOWN_TIMEOUT) -> bool:
        """
        优雅关闭进程
        
        Args:
            timeout: 等待进程退出的超时时间（秒）
        
        Returns:
            True 如果成功关闭，False 如果超时或出错
        """
        self._revoke_model_gateway_access()
        if not self.process.is_alive():
            self.logger.info(f"Plugin {self.plugin_id} process already stopped")
            return True
        
        try:
            # 先尝试优雅关闭（进程会从队列读取 STOP 命令后退出）
            self.process.join(timeout=timeout)
            
            if self.process.is_alive():
                self.logger.warning(
                    f"Plugin {self.plugin_id} didn't stop gracefully within {timeout}s, terminating"
                )
                self.process.terminate()
                self.process.join(timeout=PROCESS_TERMINATE_TIMEOUT)
                
                if self.process.is_alive():
                    self.logger.error(f"Plugin {self.plugin_id} failed to terminate, killing")
                    self.process.kill()
                    self.process.join(timeout=PROCESS_TERMINATE_TIMEOUT)
                    return False
            
            self.logger.info(f"Plugin {self.plugin_id} process shutdown successfully")
            return True
            
        except Exception:
            self.logger.exception("Error while shutting down plugin {}", self.plugin_id)
            return False


# Backwards-compatible alias
PluginProcessHost = PluginHost
