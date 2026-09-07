# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Main FastAPI server package with compatibility re-exports.

The import path ``app.main_server`` and all legacy top-level names remain
available while implementation domains live in sibling modules. Import
statements below intentionally follow the former file order so app setup,
router inclusion, middleware installation, and lifecycle hook registration
retain their startup side-effect sequence.
"""

import sys
import os

# Make the repo root importable when this package is run with
# ``python -m app.main_server``. Under launcher.py the path is already set up;
# the insert below is then a no-op.
_repo_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if sys.path[0:1] != [_repo_root]:
    sys.path.insert(0, _repo_root)

# Wire DI bindings (config._runtime resolvers ← utils.language_utils /
# utils.tokenize). Under launcher this is also done by app/__init__.py's
# side effect when ``from app import main_server`` runs. The explicit call is
# retained for frozen/package entry points and is idempotent.
from app.runtime_bindings import install_runtime_bindings as _install_runtime_bindings

_install_runtime_bindings()

# Windows multiprocessing 支持：确保子进程不会重复执行模块级初始化
from multiprocessing import freeze_support
import multiprocessing
from utils.port_utils import set_port_probe_reuse

freeze_support()

# 设置 multiprocessing 启动方法（确保跨进程共享结构的一致性）
# 在 Linux/macOS 上使用 fork，在 Windows 上使用 spawn（默认）
if sys.platform != "win32":
    try:
        multiprocessing.set_start_method("fork", force=False)
    except RuntimeError:
        # 启动方法已经设置过，忽略
        pass

# 检查是否需要执行初始化（用于防止 Windows spawn 方式创建的子进程重复初始化）
# 方案：首次导入时设置环境变量标记，子进程会继承这个标记从而跳过初始化
_INIT_MARKER = "_NEKO_MAIN_SERVER_INITIALIZED"
_IS_MAIN_PROCESS = _INIT_MARKER not in os.environ

if _IS_MAIN_PROCESS:
    # 立即设置标记，这样任何从此进程 spawn 的子进程都会继承此标记
    os.environ[_INIT_MARKER] = "1"


# 获取应用程序根目录（与 config_manager 保持一致）
def _get_app_root():
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            return sys._MEIPASS
        else:
            return os.path.dirname(sys.executable)
    else:
        # Source mode: this file lives at <repo>/app/main_server/__init__.py,
        # so the app root is three dirname() calls up.
        return os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )


# 仅在 Windows 上调整 DLL 搜索路径
if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    os.add_dll_directory(_get_app_root())

import mimetypes  # noqa

mimetypes.add_type("application/javascript", ".js")
import asyncio  # noqa
import importlib  # noqa
import inspect  # noqa
import logging  # noqa
import atexit  # noqa
import httpx  # noqa
import time  # noqa
import signal  # noqa
from datetime import datetime, timezone  # noqa
from config import (
    MAIN_SERVER_PORT,
    MONITOR_SERVER_PORT,
    USER_NOTIFICATION_ERROR_MAX_CHARS,
    USER_PLUGIN_BASE,
)  # noqa
from utils.cloudsave_autocloud import get_cloudsave_manager  # noqa
from utils.cloudsave_runtime import (
    CloudsaveDeadlineExceeded,
    MaintenanceModeError,
    ROOT_MODE_NORMAL,
    bootstrap_local_cloudsave_environment,
    is_cloudsave_disabled,
    is_write_fence_active,
    maintenance_error_payload,
    set_root_mode,
    should_write_root_mode_normal_after_startup,
)
from utils.config_manager import get_config_manager, get_reserved  # noqa
from utils.root_state_lock import root_state_transaction
from utils.storage_location_bootstrap import get_storage_startup_blocking_reason

# 将日志初始化提前，确保导入阶段异常也能落盘
from utils.logger_config import setup_logging  # noqa: E402
from utils.ssl_env_diagnostics import probe_ssl_environment, write_ssl_diagnostic  # noqa: E402
from utils.asyncio_executor import configure_default_executor  # noqa: E402
from utils.asgi_body_limit import InboundBodySizeLimitMiddleware  # noqa: E402
from utils.avatar_tool_store import AVATAR_TOOL_MAX_MULTIPART_BODY_BYTES  # noqa: E402
from utils.host_origin_guard import HostOriginGuardMiddleware  # noqa: E402

_main_log_level = getattr(
    logging, (os.environ.get("NEKO_LOG_LEVEL") or "INFO").upper(), logging.INFO
)
logger, log_config = setup_logging(
    service_name="Main", log_level=_main_log_level, silent=not _IS_MAIN_PROCESS
)
importlib.import_module(f"{__package__}._shared").runtime.logger = logger
importlib.import_module(
    f"{__package__}._shared"
).runtime.is_main_process = _IS_MAIN_PROCESS


def _resolve_user_plugin_base() -> str:
    """Delegates to config so this cannot drift from the process that mints
    plugin media URLs, or from the router that proxies them to the browser."""
    from config.network import resolve_user_plugin_base

    # Judge the raw value directly. Inferring "it was rejected" from the
    # resolved origin is wrong: USER_PLUGIN_BASE is itself derived from the
    # same variable, so a VALID port makes the two match and the warning fires
    # on correct input.
    raw_port = os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", "").strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            port = 0
        if not 0 < port <= 65535:
            logger.warning(
                "Invalid NEKO_USER_PLUGIN_SERVER_PORT value {!r}; using configured plugin base",
                raw_port,
            )
    return resolve_user_plugin_base()


if _IS_MAIN_PROCESS:
    _ssl_precheck = probe_ssl_environment()
    if not _ssl_precheck.get("ok", True):
        diag_dir = os.path.join(log_config.get_log_directory_path(), "diagnostics")
        diag_path = write_ssl_diagnostic(
            event="main_server_ssl_precheck_failed",
            output_dir=diag_dir,
            extra=_ssl_precheck,
        )
        logger.warning(
            "SSL environment precheck failed: %s%s",
            _ssl_precheck.get("error_message"),
            f" | diagnostic: {diag_path}" if diag_path else "",
        )

try:
    from fastapi import FastAPI, Request  # noqa
    from fastapi.responses import JSONResponse, Response  # noqa
    from fastapi.staticfiles import StaticFiles  # noqa
    from main_logic import core as core, cross_server as cross_server  # noqa
    from main_logic.agent_event_bus import (
        MainServerAgentBridge,
        notify_analyze_ack,
        set_main_bridge,
    )  # noqa
    from fastapi.templating import Jinja2Templates  # noqa
    from dataclasses import dataclass  # noqa
    from typing import Any, Optional  # noqa
except Exception as e:
    logger.exception(f"[Main] Module import failed during startup: {e}")
    raise

# 导入创意工坊工具模块
from utils.workshop_utils import (  # noqa
    get_workshop_root,
    get_workshop_path,
)

# 导入创意工坊路由中的函数
from main_routers.workshop_router import (
    get_subscribed_workshop_items,
    sync_workshop_character_cards,
    warmup_ugc_cache,
)  # noqa

# 确定 templates 目录位置（使用 _get_app_root）
template_dir = _get_app_root()

templates = Jinja2Templates(directory=template_dir)


def initialize_steamworks(*, quiet: bool = False):
    # quiet=True 供后台静默重试使用：无 Steam 环境（如远端服务器部署）下，
    # 前端轮询会每隔几秒触发一次重试，若按 ERROR/print 输出会无限刷屏。静默
    # 模式把进度与失败日志统一降到 DEBUG，只有首次启动的尝试保持可见。
    def _trace(msg: str) -> None:
        if quiet:
            if "logger" in globals():
                logger.debug(msg)
        else:
            print(msg)

    try:
        # 明确读取steam_appid.txt文件以获取应用ID
        app_id = None
        app_id_file = os.path.join(_get_app_root(), "steam_appid.txt")
        if os.path.exists(app_id_file):
            with open(app_id_file, "r", encoding="utf-8") as f:
                app_id = f.read().strip()
            _trace(f"从steam_appid.txt读取到应用ID: {app_id}")

        # 创建并初始化Steamworks实例
        from steamworks import STEAMWORKS

        steamworks = STEAMWORKS()
        # 显示Steamworks初始化过程的详细日志
        _trace("正在初始化Steamworks...")
        steamworks.initialize()
        steamworks.UserStats.RequestCurrentStats()
        # 初始化后再次获取应用ID以确认
        actual_app_id = steamworks.app_id
        _trace(f"Steamworks初始化完成，实际使用的应用ID: {actual_app_id}")

        # 检查全局logger是否已初始化，如果已初始化则记录成功信息
        if "logger" in globals():
            logger.info(f"Steamworks初始化成功，应用ID: {actual_app_id}")
            logger.info(f"Steam客户端运行状态: {steamworks.IsSteamRunning()}")
            try:
                logger.info(f"Steam覆盖层启用状态: {steamworks.IsOverlayEnabled()}")
            except Exception as overlay_error:
                logger.info("Steam覆盖层状态不可用，跳过覆盖层诊断: %s", overlay_error)

        return steamworks
    except Exception as e:
        # 检查全局logger是否已初始化，如果已初始化则记录错误，否则使用print
        error_msg = f"初始化Steamworks失败: {e}"
        if quiet:
            if "logger" in globals():
                logger.debug(error_msg)
        elif "logger" in globals():
            logger.error(error_msg)
        else:
            print(error_msg)
        return None


def ensure_steamworks_initialized():
    """Retry Steamworks initialization after Steam is opened post-startup."""
    global steamworks
    if steamworks is not None:
        return steamworks

    logger.debug("尝试重新初始化 Steamworks...")
    steamworks = initialize_steamworks(quiet=True)
    try:
        from main_routers.shared_state import set_steamworks

        set_steamworks(steamworks)
    except Exception as exc:
        logger.debug(
            "Steamworks shared-state update failed during retry: %s", exc, exc_info=True
        )

    if steamworks is not None:
        get_default_steam_info()
    return steamworks


def get_default_steam_info():
    global steamworks
    # 检查steamworks是否初始化成功
    if steamworks is None:
        print("Steamworks not initialized. Skipping Steam functionality.")
        if "logger" in globals():
            logger.info("Steamworks not initialized. Skipping Steam functionality.")
        return

    try:
        my_steam64 = steamworks.Users.GetSteamID()
        my_steam_level = steamworks.Users.GetPlayerSteamLevel()
        subscribed_apps = steamworks.Workshop.GetNumSubscribedItems()
        print(f"Subscribed apps: {subscribed_apps}")

        print(f"Logged on as {my_steam64}, level: {my_steam_level}")
        print("Is subscribed to current app?", steamworks.Apps.IsSubscribed())
    except Exception as e:
        print(f"Error accessing Steamworks API: {e}")
        if "logger" in globals():
            logger.error(f"Error accessing Steamworks API: {e}")


# Steamworks 初始化将在 @app.on_event("startup") 中延迟执行
# 这样可以避免在模块导入时就执行 DLL 加载等操作
steamworks = None
_server_loop: asyncio.AbstractEventLoop | None = None

_config_manager = get_config_manager()
_cloudsave_manager = get_cloudsave_manager(_config_manager)
importlib.import_module(
    f"{__package__}._shared"
).runtime.config_manager = _config_manager


def _cloudsave_action_supports_deadline(action) -> bool:
    try:
        signature = inspect.signature(action)
    except (TypeError, ValueError):
        return False
    if "deadline_monotonic" in signature.parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _cloudsave_action_supports_steamworks(action) -> bool:
    try:
        signature = inspect.signature(action)
    except (TypeError, ValueError):
        return False
    if "steamworks" in signature.parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


async def _run_cloudsave_manager_action(
    action_name: str,
    *,
    reason: str,
    budget_seconds: float | None = None,
    steamworks=None,
):
    action = getattr(_cloudsave_manager, action_name)
    kwargs = {"reason": reason}
    if (
        budget_seconds is not None
        and budget_seconds > 0
        and _cloudsave_action_supports_deadline(action)
    ):
        kwargs["deadline_monotonic"] = time.monotonic() + float(budget_seconds)
    if steamworks is not None and _cloudsave_action_supports_steamworks(action):
        kwargs["steamworks"] = steamworks
    return await asyncio.to_thread(action, **kwargs)


async def _request_memory_server_shutdown() -> None:
    """Request memory_server shutdown after main_server has finished its own cleanup."""
    try:
        from config import MEMORY_SERVER_PORT

        shutdown_url = f"http://127.0.0.1:{MEMORY_SERVER_PORT}/shutdown"
        async with httpx.AsyncClient(timeout=1, proxy=None, trust_env=False) as client:
            response = await client.post(shutdown_url)
        if response.status_code == 200:
            logger.info("已向memory_server发送关闭信号")
        else:
            logger.warning(
                f"向memory_server发送关闭信号失败，状态码: {response.status_code}"
            )
    except Exception as e:
        logger.warning(f"向memory_server发送关闭信号时出错: {e}")


class MemoryServerStartupBlocked(RuntimeError):
    def __init__(self, payload: dict):
        self.payload = dict(payload)
        self.blocking_reason = str(self.payload.get("blocking_reason") or "").strip()
        super().__init__(f"memory_server startup still blocked: {self.payload!r}")


async def _request_memory_server_continue_startup(reason: str = "") -> None:
    """Release memory_server from limited mode after the storage barrier is accepted."""
    try:
        from config import MEMORY_SERVER_PORT
        from utils.internal_http_client import get_internal_http_client

        client = get_internal_http_client()
        response = await client.post(
            f"http://127.0.0.1:{MEMORY_SERVER_PORT}/internal/storage/startup/continue",
            json={"reason": reason},
            timeout=60.0,
        )
        if response.status_code == 409:
            try:
                payload = response.json()
            except Exception:
                payload = {"ok": False, "blocking_reason": "", "error": response.text}
            if (
                isinstance(payload, dict)
                and payload.get("ok") is False
                and payload.get("blocking_reason")
            ):
                raise MemoryServerStartupBlocked(payload)
            response.raise_for_status()

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(
                f"memory_server continue-startup returned unexpected payload: {payload!r}"
            )
    except MemoryServerStartupBlocked:
        raise
    except Exception as e:
        raise RuntimeError(
            f"failed to release memory_server limited-mode startup: {e}"
        ) from e


async def _request_memory_server_block_startup(reason: str = "") -> None:
    """Return memory_server to limited mode when main_server cannot finish startup."""
    try:
        from config import MEMORY_SERVER_PORT
        from utils.internal_http_client import get_internal_http_client

        client = get_internal_http_client()
        response = await client.post(
            f"http://127.0.0.1:{MEMORY_SERVER_PORT}/internal/storage/startup/block",
            json={"reason": reason},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(
                f"memory_server block-startup returned unexpected payload: {payload!r}"
            )
    except Exception as e:
        raise RuntimeError(
            f"failed to restore memory_server limited-mode startup: {e}"
        ) from e


agent_event_bridge: MainServerAgentBridge | None = None
# Strong ownership for manager terminal-close wrappers that may outlive the
# bounded FastAPI shutdown wait.  A done callback removes each task and
# consumes its exception; this avoids weak-ref task loss while the event loop
# performs its final cancellation pass.
_manager_terminal_close_tasks: set[asyncio.Task] = set()

from .character_runtime import (  # noqa: F401
    RoleState,
    _SyncMessageQueue,
    _broadcast_to_all_connected,
    _cleanup_character_dicts,
    _ensure_character_slots,
    _get_session_manager,
    _handle_agent_event,
    _init_character_resources,
    _is_websocket_connected,
    _iter_session_managers,
    _iter_sync_connector_tasks,
    _refresh_character_globals,
    _reset_sync_connector_shutdown_events,
    _select_fallback_session_manager,
    _signal_sync_connectors_shutdown,
    _stop_character_thread,
    catgirl_names,
    cleanup,
    her_name,
    init_one_catgirl,
    initialize_character_data,
    join_sync_connector_tasks,
    join_sync_connector_threads,
    lanlan_basic_config,
    lanlan_prompt,
    master_basic_config,
    master_name,
    name_mapping,
    recent_log,
    remove_one_catgirl,
    role_state,
    setting_store,
    switch_current_catgirl_fast,
    time_store,
)

try:
    from .character_runtime import register_topic_session_manager_getter  # noqa: F401
except ImportError:
    pass

lock = asyncio.Lock()

# --- FastAPI App Setup ---
app = FastAPI()
importlib.import_module(f"{__package__}._shared").runtime.app = app
importlib.import_module(f"{__package__}._shared").runtime.get_app_root = _get_app_root
importlib.import_module(
    f"{__package__}._shared"
).runtime.resolve_user_plugin_base = _resolve_user_plugin_base

_main_runtime_limited_mode_enabled = False
_main_runtime_limited_mode_reason = ""
_MAIN_LIMITED_MODE_ALLOWED_EXACT_PATHS = {
    "/",
    "/api/card-drop/active-character",
    "/health",
    "/favicon.ico",
    "/api/beacon/shutdown",
    "/api/runtime/shutdown",
    "/api/config/steam_language",
    "/api/system/status",
}
_MAIN_LIMITED_MODE_ALLOWED_PAGE_PATHS = {
    "/l2d",
    "/model_manager",
    "/live2d_parameter_editor",
    "/soccer_demo",
    "/badminton_demo",
    "/live2d_emotion_manager",
    "/vrm_emotion_manager",
    "/mmd_emotion_manager",
    "/voice_clone",
    "/api_key",
    "/voice_identity",
    "/chara_manager",
    "/character_card_manager",
    "/cloudsave_manager",
    "/memory_browser",
    "/cookies_login",
    "/chat",
    "/web_chat_compact",
    "/subtitle",
    "/agenthud",
    "/card_maker",
    "/jukebox",
    "/jukebox/manager",
    "/toast",
}
_MAIN_LIMITED_MODE_ALLOWED_PREFIXES = (
    "/static/",
    "/api/storage/location/",
    # 诊断观测：limited-mode 本身就是要排查的故障形态之一（启动阻断），
    # 这时候反而最需要 /api/debug/health 能读到 ring + watchdog 落盘。
    "/api/debug/",
)


def _enable_main_storage_limited_mode(reason: str) -> None:
    global _main_runtime_limited_mode_enabled, _main_runtime_limited_mode_reason
    _main_runtime_limited_mode_enabled = True
    _main_runtime_limited_mode_reason = (
        str(reason or "runtime_initializing").strip() or "runtime_initializing"
    )


def _disable_main_storage_limited_mode() -> None:
    global _main_runtime_limited_mode_enabled, _main_runtime_limited_mode_reason
    _main_runtime_limited_mode_enabled = False
    _main_runtime_limited_mode_reason = ""


def _is_main_limited_mode_allowed_path(path: str, method: str) -> bool:
    if path in _MAIN_LIMITED_MODE_ALLOWED_EXACT_PATHS:
        return True
    if path in _MAIN_LIMITED_MODE_ALLOWED_PAGE_PATHS and method in {"GET", "HEAD"}:
        return True
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in _MAIN_LIMITED_MODE_ALLOWED_PREFIXES
    )


@app.middleware("http")
async def main_storage_limited_mode_guard(request: Request, call_next):
    if _runtime_startup_init_completed or not _main_runtime_limited_mode_enabled:
        return await call_next(request)

    if _is_main_limited_mode_allowed_path(request.url.path, request.method):
        return await call_next(request)

    blocking_reason = _main_runtime_limited_mode_reason or "runtime_initializing"
    logger.info(
        "[Main] limited-mode blocks request path=%r reason=%s",
        request.url.path,
        blocking_reason,
    )
    return JSONResponse(
        status_code=409,
        content={
            "ok": False,
            "error_code": "storage_startup_blocked",
            "blocking_reason": blocking_reason,
            "limited_mode": True,
            "error": "Main server 正处于存储受限启动状态，请等待存储位置选择、迁移或恢复完成。",
        },
    )


def _avatar_tool_multipart_preflight(scope):
    from main_routers.system_router._shared import (
        _is_loopback_request,
        _validate_local_mutation_request,
    )

    request = Request(scope)
    if not _is_loopback_request(request):
        return JSONResponse(
            status_code=403,
            content={"detail": "Forbidden: local access only"},
        )
    return _validate_local_mutation_request(request)


# 全局非 multipart 请求继续沿用 16 MiB Content-Length 守门。自定义道具
# POST/PUT 另外在 FastAPI 解析 multipart 前完成本地访问、CSRF/origin 和聚合
# 体积校验；receive 计数同时覆盖缺失或不可信的 Content-Length。
app.add_middleware(
    InboundBodySizeLimitMiddleware,
    multipart_path_prefix="/api/avatar-tools",
    multipart_methods=("POST", "PUT"),
    max_multipart_body_bytes=AVATAR_TOOL_MAX_MULTIPART_BODY_BYTES,
    multipart_preflight=_avatar_tool_multipart_preflight,
)
# Registered after the body guard so it is the outermost ASGI middleware and
# rejects DNS-rebinding Host values before any HTTP or WebSocket route runs.
app.add_middleware(HostOriginGuardMiddleware)


@app.exception_handler(MaintenanceModeError)
async def handle_maintenance_mode_error(_request, exc: MaintenanceModeError):
    return JSONResponse(status_code=409, content=maintenance_error_payload(exc))


from .web_app import (  # noqa: F401
    CustomStaticFiles,
    _active_character_cors_headers,
    _card_drop_active_character,
    _start_debug_health_watchdog,
    active_character_options,
    agent_router,
    avatar_drop_router,
    beacon_shutdown,
    capture_router,
    card_assist_router,
    card_drop_router,
    characters_router,
    cloudsave_router,
    config_router,
    cookies_login_router,
    debug_router,
    galgame_router,
    game_router,
    get_card_drop_active_character,
    health,
    icebreaker_router,
    init_shared_state,
    jukebox_router,
    live2d_router,
    memory_router,
    mmd_router,
    music_router,
    pages_router,
    pngtuber_router,
    proactive_router,
    proxy_user_plugin_market_bridge,
    set_steamworks_initializer,
    set_card_drop_active_character,
    static_dir,
    storage_location_router,
    system_router,
    tool_router,
    vrm_router,
    websocket_router,
    workshop_router,
)

_preload_task: asyncio.Task = None
_game_cleanup_task: asyncio.Task = None
_facts_sync_worker_task: asyncio.Task = None
_client_registration_task: asyncio.Task = None
_runtime_startup_init_lock = asyncio.Lock()
_runtime_startup_init_completed = False


from .preload import _background_preload, _sync_preload_modules  # noqa: F401


async def _sync_memory_server_after_startup_import(import_result):
    """Keep memory_server aligned when main_server applies a cloud snapshot on startup."""
    if not isinstance(import_result, dict) or import_result.get("action") != "imported":
        return

    try:
        from main_routers.characters_router import notify_memory_server_reload

        reloaded = await notify_memory_server_reload(
            reason="Steam Auto-Cloud startup import",
        )
        if not reloaded:
            logger.warning(
                "Steam Auto-Cloud startup import applied, but memory_server reload did not succeed"
            )
    except Exception as e:
        logger.warning(
            f"Steam Auto-Cloud startup import could not sync memory_server: {e}"
        )


def _start_neko_servers_integration_workers() -> None:
    """Start storage-backed integration workers after the startup barrier clears."""
    global _facts_sync_worker_task, _client_registration_task

    # The forge debit callback defaults to the production cloud with no feature
    # flag, so the matching client_id must be registered unconditionally here.
    # Leaving this to facts_sync (off by default) left every proof-bearing call
    # failing 403 against a client_id the cloud had never seen.
    if _client_registration_task is None or _client_registration_task.done():
        try:
            from main_logic.client_registration import ensure_client_registered

            _client_registration_task = asyncio.create_task(
                ensure_client_registered()
            )
        except Exception as exc:
            logger.warning("[client_registration] bootstrap failed: %s", exc)

    if _facts_sync_worker_task is None or _facts_sync_worker_task.done():
        try:
            from main_logic.facts_sync import start_facts_sync_worker

            _facts_sync_worker_task = asyncio.create_task(start_facts_sync_worker())
        except Exception as exc:
            logger.warning("[facts_sync] start worker failed: %s", exc)

async def _stop_neko_servers_integration_workers() -> None:
    """Cancel storage-backed integration workers during graceful shutdown."""
    global _facts_sync_worker_task, _client_registration_task

    await _cancel_task_if_running(
        _facts_sync_worker_task,
        name="facts sync worker",
        timeout=1.0,
    )
    _facts_sync_worker_task = None

    await _cancel_task_if_running(
        _client_registration_task,
        name="client registration bootstrap",
        timeout=1.0,
    )
    _client_registration_task = None


async def _cancel_task_if_running(
    task: asyncio.Task | None, *, name: str, timeout: float = 1.0
) -> None:
    if task is None:
        return
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug(
                "%s task finished with error during startup rollback: %s",
                name,
                exc,
                exc_info=True,
            )
        return

    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        logger.debug("%s task cancelled during startup rollback", name)
    except asyncio.TimeoutError:
        logger.warning(
            "%s task did not stop within %.1fs during startup rollback", name, timeout
        )
    except Exception as exc:
        logger.debug(
            "%s task cleanup failed during startup rollback: %s",
            name,
            exc,
            exc_info=True,
        )


async def _cancel_workshop_background_tasks(*, timeout: float) -> None:
    try:
        # Target the ugc submodule (not the workshop_router package facade):
        # the task handles are module globals there, and setattr on the facade
        # would not rebind them for cancel_background_tasks / route readers.
        _wr = importlib.import_module("main_routers.workshop_router.ugc")
    except Exception as exc:
        logger.debug("workshop task cleanup skipped: %s", exc, exc_info=True)
        return

    cancel_background_tasks = getattr(_wr, "cancel_background_tasks", None)
    if callable(cancel_background_tasks):
        await cancel_background_tasks(timeout=timeout)
        return

    for task_attr in ("_ugc_warmup_task", "_ugc_sync_task"):
        task = getattr(_wr, task_attr, None)
        await _cancel_task_if_running(
            task, name=f"workshop {task_attr}", timeout=timeout
        )
        if getattr(_wr, task_attr, None) is task:
            setattr(_wr, task_attr, None)


async def _cancel_workshop_background_tasks_for_startup_rollback() -> None:
    await _cancel_workshop_background_tasks(timeout=1.0)


async def _rollback_partial_main_runtime_startup() -> None:
    global steamworks, _preload_task, _game_cleanup_task, agent_event_bridge

    await _cancel_task_if_running(_preload_task, name="preload", timeout=1.0)
    _preload_task = None
    await _cancel_task_if_running(_game_cleanup_task, name="game cleanup", timeout=1.0)
    _game_cleanup_task = None

    await _cancel_workshop_background_tasks_for_startup_rollback()

    if agent_event_bridge is not None:
        bridge = agent_event_bridge
        agent_event_bridge = None
        try:
            await bridge.stop()
        except Exception as exc:
            logger.debug("Agent event bridge rollback failed: %s", exc, exc_info=True)
        try:
            set_main_bridge(None)
        except Exception as exc:
            logger.debug(
                "Agent event bridge reference rollback failed: %s", exc, exc_info=True
            )

    try:
        from main_routers.shared_state import set_steamworks

        set_steamworks(None)
    except Exception as exc:
        logger.debug("Steamworks shared-state rollback failed: %s", exc, exc_info=True)
    steamworks = None

    try:
        cleanup(log=False)
        await join_sync_connector_threads(1.0)
    except Exception as exc:
        logger.debug("Sync connector rollback failed: %s", exc, exc_info=True)
    finally:
        _reset_sync_connector_shutdown_events()


async def _ensure_main_server_runtime_initialized(*, reason: str) -> bool:
    global \
        steamworks, \
        _preload_task, \
        _game_cleanup_task, \
        agent_event_bridge, \
        _runtime_startup_init_completed

    if _runtime_startup_init_completed:
        return False

    async with _runtime_startup_init_lock:
        if _runtime_startup_init_completed:
            return False

        try:
            if is_cloudsave_disabled():
                logger.warning(
                    "Steam Auto-Cloud startup skipped because cloudsave is disabled for this session"
                )
                import_result = None
            else:
                bootstrap_local_cloudsave_environment(_config_manager)
                import_result = None
                try:
                    import_result = await _run_cloudsave_manager_action(
                        "import_if_needed",
                        reason="main_server_startup",
                        budget_seconds=10.0,
                    )
                    logger.info("Steam Auto-Cloud startup import: %s", import_result)
                except CloudsaveDeadlineExceeded:
                    logger.warning(
                        "Steam Auto-Cloud startup import exceeded 10.0s budget before applying runtime changes; continuing with local runtime state"
                    )
                except Exception as e:
                    logger.warning(f"Steam Auto-Cloud startup import failed: {e}")

            await initialize_character_data()
            await _sync_memory_server_after_startup_import(import_result)

            logger.info("正在初始化 Steamworks...")
            steamworks = initialize_steamworks()

            from main_routers.shared_state import set_steamworks

            set_steamworks(steamworks)
            get_default_steam_info()

            _preload_task = asyncio.create_task(_background_preload())
            # 启动游戏 session 超时清理后台任务
            from main_routers.game_router import cleanup_expired_sessions

            if _game_cleanup_task is None or _game_cleanup_task.done():
                _game_cleanup_task = asyncio.create_task(cleanup_expired_sessions())
            try:
                agent_event_bridge = MainServerAgentBridge(
                    on_agent_event=_handle_agent_event
                )
                await agent_event_bridge.start()
                set_main_bridge(agent_event_bridge)
            except Exception as e:
                logger.warning(f"Agent event bridge startup failed: {e}")

            # 创意工坊：目录挂载保持同步（开销小，且必须在 ready 前完成，
            # 否则 /workshop 静态资源在挂载窗口内会 404 —— 见 PR #1496 review）。
            # 真正慢的 UGC 缓存预热 + 角色卡网络同步仍后台化（与原始行为一致）。
            await _init_and_mount_workshop()
            _schedule_workshop_sync(steamworks)

            try:
                from utils.token_tracker import TokenTracker, install_hooks

                install_hooks()
                TokenTracker.get_instance().start_periodic_save()
                # process 字段进 session_start / session_end 维度，跨进程诊断必须区分
                TokenTracker.get_instance().record_app_start(process="main_server")
                logger.info("Token usage tracker initialized")
            except Exception as e:
                logger.warning(
                    f"Token tracker initialization failed (non-critical): {e}"
                )

            logger.info(
                "Startup 初始化完成，后台正在预加载音频模块... (reason=%s)", reason
            )

            try:
                from utils.language_utils import initialize_global_language

                global_lang = initialize_global_language()
                logger.info(f"全局语言初始化完成: {global_lang}")
            except Exception as e:
                logger.warning(f"全局语言初始化失败（不影响启动）: {e}")

            if is_cloudsave_disabled():
                logger.warning("跳过 ROOT_MODE_NORMAL 写入：cloudsave 已为本次会话禁用")
                current_root_state = None
            else:
                current_root_state = _config_manager.load_root_state()

            if current_root_state is None:
                if not is_cloudsave_disabled():
                    logger.warning(
                        "跳过 ROOT_MODE_NORMAL 写入：root_state 缺失或读取失败"
                    )
            elif should_write_root_mode_normal_after_startup(current_root_state):
                # 挪进工作线程有两个理由，缺一不可：
                # 1) set_root_mode 是同步落盘（mkstemp + fsync + os.replace）；
                # 2) 它拿 root_state 的写者锁，而 storage_location 那几条变更路由
                #    现在在工作线程里持同一把锁。受限启动期存储页跟这段是可以
                #    重叠的，留在循环上就等于把工作线程那次 fsync（撞上 Windows
                #    占用还要加最多 155ms 退避）接回循环。同一把锁的所有入口要么
                #    都在工作线程，要么都不在。
                #
                # ⚠️ 上面那次 should_write_root_mode_normal_after_startup 判定是在
                # 循环上做的，跟这次落盘之间隔了一个 await。job 排队期间，存储变更
                # 路由的工作线程完全可能刚提交 ROOT_MODE_MAINTENANCE_READONLY（用户
                # 正在发起重启迁移）——那时无脑写 NORMAL 就是把它的受限态踩掉。
                # 所以判定跟着写一起进锁内重做一遍，恢复"检查和写不可分割"这条原本
                # 靠"同在循环线程"隐式成立的性质。
                def _mark_startup_successful() -> bool:
                    with root_state_transaction():
                        state = _config_manager.load_root_state()
                        if not isinstance(state, dict):
                            return False
                        if not should_write_root_mode_normal_after_startup(state):
                            return False
                        set_root_mode(
                            _config_manager,
                            ROOT_MODE_NORMAL,
                            current_root=str(_config_manager.app_docs_dir),
                            last_known_good_root=str(_config_manager.app_docs_dir),
                            last_successful_boot_at=datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z"),
                        )
                        return True

                try:
                    if not await asyncio.to_thread(_mark_startup_successful):
                        logger.info(
                            "跳过 ROOT_MODE_NORMAL 写入：落盘前 root_state 已被改成阻断态"
                        )
                except Exception as e:
                    logger.error(
                        "写入 main_server 启动成功标记失败，启动不会标记为成功: %s", e
                    )
                    raise RuntimeError(
                        "main_server failed to persist ROOT_MODE_NORMAL state"
                    ) from e
            else:
                logger.info(
                    "跳过 ROOT_MODE_NORMAL 写入，当前仍处于阻断态: %s",
                    current_root_state.get("mode") or ROOT_MODE_NORMAL,
                )

            # GeoIP 预热必须在放开会话准入之前完成：会话的线路在 start_session 时
            # 定死，一旦 _runtime_startup_init_completed 置位、limited mode 解除，
            # 请求就能进来，此时若区域判定还没落地，首个会话会整场钉在大陆兜底线路。
            # 放在这里（而不是本函数末尾）正是为了堵住那个准入窗口。
            # 配置到这一步才最终成型（Cloud Save 快照已导入、Steamworks 已初始化），
            # 所以也不能再往前挪——早读会看到导入前的旧配置而得出「无需区域判定」。
            # 等待经 to_thread offload，不占事件循环；请求路径依然从不等探测。
            try:
                if not await _config_manager.awarmup_region_check():
                    logger.info("[GeoIP] 启动预热未拿到区域结论，后续调用按退避重试")
            except Exception:
                logger.debug("[GeoIP] 预热失败，留给后续调用重试", exc_info=True)

            _runtime_startup_init_completed = True
            _disable_main_storage_limited_mode()

            # runtime init 完成后再起后台预热：把已改 lazy 的重模块（genai+mcp /
            # translatepy / 功能路由依赖）提前 import 好，用户首次用到时不等。放在
            # 这里而非 on_startup 开头，是为了不在关键启动路径上和 runtime init 抢 GIL。
            try:
                from utils.module_warmup import (
                    MAIN_SERVER_WARMUP,
                    start_background_warmup,
                )

                start_background_warmup(MAIN_SERVER_WARMUP, label="main")
            except Exception as _warmup_exc:
                logger.debug(f"[warmup] main_server warmup not started: {_warmup_exc}")

            return True
        except Exception:
            _runtime_startup_init_completed = False
            if _main_runtime_limited_mode_enabled:
                _enable_main_storage_limited_mode("runtime_initialization_failed")
            await _rollback_partial_main_runtime_startup()
            raise


async def release_storage_startup_barrier(
    *, reason: str = "storage_selection_continue_current_session"
) -> dict[str, Any]:
    try:
        # The continue request has an ambiguous cancellation outcome: memory_server
        # may have applied it even when this client never receives the response.
        # Keep the request itself inside the compensation boundary so every failed
        # exit re-establishes both admission guards before storage state rolls back.
        await _request_memory_server_continue_startup(reason)
        initialized = await _ensure_main_server_runtime_initialized(reason=reason)
    except BaseException:
        # Once memory_server may have accepted continue-startup, every failed exit
        # must put its admission guard back before the storage router restores a
        # blocking root_state snapshot. CancelledError is a BaseException, so an
        # Exception-only handler leaves initialized memory writable against the
        # rolled-back storage state.
        _enable_main_storage_limited_mode("runtime_initialization_failed")

        # Re-blocking is compensating work, not part of the cancelled request.  A
        # second cancellation (for example server shutdown following a client
        # disconnect) must not interrupt it halfway through.  Keep the request in
        # this handler until the HTTP call has really finished, then re-raise the
        # original exception below.
        block_task = asyncio.ensure_future(
            _request_memory_server_block_startup(
                f"{reason}:main_server_init_failed"
            )
        )
        try:
            await asyncio.shield(block_task)
        except asyncio.CancelledError:
            while not block_task.done():
                try:
                    await asyncio.wait({block_task})
                except asyncio.CancelledError:
                    continue
        except Exception as revert_exc:
            logger.warning(
                "main_server 初始化失败后恢复 memory_server limited-mode 失败: %s",
                revert_exc,
                exc_info=True,
            )
            # ``shield`` already retrieved this exception; do not call result()
            # below and log the same failure a second time.
            block_task = None
        else:
            # ``shield`` returns the task result, so there is nothing left to
            # retrieve on the ordinary path.
            block_task = None

        if block_task is not None and block_task.done() and not block_task.cancelled():
            try:
                block_task.result()
            except Exception as revert_exc:
                logger.warning(
                    "main_server 初始化取消后恢复 memory_server limited-mode 失败: %s",
                    revert_exc,
                    exc_info=True,
                )
        raise
    _disable_main_storage_limited_mode()
    _start_neko_servers_integration_workers()
    return {
        "ok": True,
        "initialized": bool(initialized),
    }


# Startup 事件：延迟初始化 Steamworks 和全局语言
@app.on_event("startup")
async def on_startup():
    """Initialization performed at server startup"""
    if _IS_MAIN_PROCESS:
        global _server_loop
        _server_loop = asyncio.get_running_loop()
        importlib.import_module(
            f"{__package__}._shared"
        ).runtime.server_loop = _server_loop
        configure_default_executor(_server_loop, logger=logger)

        init_shared_state(
            role_state=role_state,
            steamworks=steamworks,
            templates=templates,
            config_manager=_config_manager,
            logger=logger,
            initialize_character_data=initialize_character_data,
            switch_current_catgirl_fast=switch_current_catgirl_fast,
            init_one_catgirl=init_one_catgirl,
            remove_one_catgirl=remove_one_catgirl,
            request_app_shutdown=lambda: asyncio.create_task(
                request_application_shutdown_async(reason="storage_location_restart")
            ),
            release_storage_startup_barrier=release_storage_startup_barrier,
        )
        set_steamworks_initializer(ensure_steamworks_initialized)
        try:
            from .voice_identity_runtime import initialize_voice_identity_runtime

            await initialize_voice_identity_runtime(_config_manager)
        except Exception as _e:
            # Voice identity is optional and fail-open. Startup and normal
            # speech must remain available even when secure storage or model
            # wiring cannot initialize.
            logger.warning("voice identity startup degraded: %s", _e)
        # GeoIP 预热已移到 _ensure_main_server_runtime_initialized 末尾——配置到那里
        # 才最终成型（Cloud Save 快照导入 + Steamworks 初始化完成）。
        # asyncio 的慢回调告警只在 loop debug 模式下输出。默认关闭，
        # 需要排查事件循环停顿时设 NEKO_DEBUG_ASYNC=1 启用（会略微增加每 callback 开销）。
        if os.environ.get("NEKO_DEBUG_ASYNC") == "1":
            _server_loop.set_debug(True)
            _server_loop.slow_callback_duration = 0.05
            logger.info("[asyncio] debug mode enabled (slow_callback_duration=0.05s)")

        # 事件循环心跳：每 200ms 打一个点。当 /new_dialog 或其他 async 操作
        # 莫名其妙变慢时，看心跳是否缺席 —— 缺席 = 事件循环被阻塞，这段时间
        # 内所有 async 都没机会跑。心跳日志仅在开启 NEKO_DEBUG_HEARTBEAT=1
        # 时输出到 stdout，避免日志刷屏。
        if os.environ.get("NEKO_DEBUG_HEARTBEAT") == "1":

            async def _event_loop_heartbeat():
                import time as _t

                _last = _t.perf_counter()
                while True:
                    await asyncio.sleep(0.2)
                    _now = _t.perf_counter()
                    _gap_ms = int((_now - _last) * 1000)
                    # 只打异常的心跳（间隔 > 300ms），正常跳过
                    if _gap_ms > 300:
                        print(
                            f"[{_t.strftime('%H:%M:%S')}] [heartbeat] stall {_gap_ms}ms (expected ~200ms)",
                            flush=True,
                        )
                    _last = _now

            asyncio.create_task(_event_loop_heartbeat())
            logger.info("[asyncio] heartbeat enabled (stalls > 300ms will be logged)")

        # 诊断观测 watchdog：5-min 周期采集 counter 写内存 ring buffer，
        # NEKO_DEBUG_HEALTH_LOG=1 时同时落盘 jsonl。详见 main_routers/debug_router.py。
        # 无条件启动 —— 单 task + 5-min 周期，开销远低于 heartbeat。
        try:
            _start_debug_health_watchdog()
        except Exception as _e:
            logger.debug(f"[debug_health] start watchdog failed: {_e}")

        blocking_reason = get_storage_startup_blocking_reason(_config_manager)
        if blocking_reason:
            _enable_main_storage_limited_mode(blocking_reason)
            logger.info(
                "检测到存储启动阻断态，main_server 先保持 limited-mode，等待网页端放行: %s",
                blocking_reason,
            )
            return

        await _ensure_main_server_runtime_initialized(reason="startup")
        _start_neko_servers_integration_workers()


@app.on_event("shutdown")
async def on_shutdown():
    """Clean up resources at server shutdown"""
    if _IS_MAIN_PROCESS:
        logger.info("正在清理资源...")
        try:
            from .voice_identity_runtime import close_voice_identity_runtime

            await close_voice_identity_runtime()
        except Exception as e:
            logger.debug(f"voice identity cleanup failed: {e}")

        # The voice-identity registry is closed first so it cannot install a
        # new verifier while managers cross their terminal ownership boundary.
        # Ordinary session teardown uses IndependentAsrRuntime.stop_session()
        # and intentionally preserves admission ingress; process shutdown is
        # where every manager must invoke its terminal ``aclose()`` before the
        # event loop is torn down.  All managers share one wall-clock budget so
        # shutdown remains bounded regardless of the character count.
        terminal_close_tasks: dict[asyncio.Task, str] = {}
        pending_terminal_close_tasks: set[asyncio.Task] = set()

        async def _terminal_close_manager(name: str, manager) -> None:
            close = getattr(manager, "aclose", None)
            if not callable(close):
                return
            try:
                await close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "terminal close session_manager 失败 (%s): %s",
                    name,
                    exc,
                    exc_info=True,
                )

        def _release_terminal_close_task(task: asyncio.Task) -> None:
            _manager_terminal_close_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        for manager_name, manager in list(_iter_session_managers()):
            task = asyncio.create_task(
                _terminal_close_manager(manager_name, manager),
                name=f"terminal-close-session-manager:{manager_name}",
            )
            _manager_terminal_close_tasks.add(task)
            task.add_done_callback(_release_terminal_close_task)
            terminal_close_tasks[task] = manager_name

        if terminal_close_tasks:
            _done, pending = await asyncio.wait(
                terminal_close_tasks,
                timeout=5.0,
            )
            if pending:
                pending_names = sorted(
                    terminal_close_tasks[task] for task in pending
                )
                logger.warning(
                    "session_manager terminal close exceeded initial 5.0s budget; "
                    "retaining tasks for final shutdown drain: %s",
                    ", ".join(pending_names),
                )
                pending_terminal_close_tasks = set(pending)
        cleanup()
        try:
            # join_sync_connector_threads 内部已经 gather 并行 join，直接 await
            await join_sync_connector_threads(3.0)
        except Exception as e:
            logger.debug(f"同步连接器线程清理失败: {e}", exc_info=True)

        # 等待预加载任务完成（如果还在运行）
        global _preload_task, _game_cleanup_task, agent_event_bridge
        if _preload_task:
            try:
                await asyncio.wait_for(_preload_task, timeout=1.0)
            except asyncio.TimeoutError:
                _preload_task.cancel()
                try:
                    await _preload_task
                except asyncio.CancelledError:
                    logger.debug("预加载任务清理时超时并已取消（正常关闭流程）")
            except asyncio.CancelledError:
                logger.debug("预加载任务清理时已取消（正常关闭流程）")
            except Exception as e:
                logger.debug(
                    f"预加载任务清理时出错（正常关闭流程）: {e}", exc_info=True
                )
            _preload_task = None

        await _cancel_task_if_running(
            _game_cleanup_task, name="game cleanup", timeout=1.0
        )
        _game_cleanup_task = None
        await _stop_neko_servers_integration_workers()

        # Clean up agent_event_bridge (ZMQ context/sockets/recv thread)
        if agent_event_bridge is not None:
            try:
                await agent_event_bridge.stop()
            except Exception as e:
                logger.debug(f"Agent event bridge cleanup failed: {e}", exc_info=True)

        # 释放 soxr ResampleStream（nanobind C 扩展），避免解释器退出时泄漏警告
        try:
            for _, mgr in _iter_session_managers():
                if hasattr(mgr, "audio_resampler"):
                    mgr.audio_resampler = None
        except Exception as e:
            logger.debug(f"soxr resampler cleanup failed: {e}")

        # 关闭翻译服务
        try:
            from utils import language_utils

            close_fn = getattr(language_utils, "aclose_translation_service", None)
            if callable(close_fn):
                await close_fn()
            else:
                logger.debug(
                    "Translation service cleanup skipped: function not implemented"
                )
        except Exception as e:
            logger.debug(f"Translation service cleanup failed: {e}")

        # 保存 Token 用量数据
        try:
            from utils.token_tracker import TokenTracker

            TokenTracker.get_instance().save()
        except Exception as e:
            logger.debug(f"Token usage save on shutdown failed: {e}")

        # 关闭音乐爬虫连接池
        try:
            from utils.music_crawlers import close_all_crawlers

            # 【核心修改】增加 1 秒超时兜底。如果 1 秒内关不完，直接抛弃，保障服务器顺利退出
            await asyncio.wait_for(close_all_crawlers(), timeout=1.0)

        except asyncio.TimeoutError:
            # 单独捕获超时异常，记录警告但放行
            logger.warning("音乐爬虫连接池清理超时，已强制跳过以保证服务正常退出。")
        except Exception as e:
            logger.debug(f"音乐爬虫清理失败: {e}", exc_info=True)

        # Steam Auto-Cloud: 预先释放 memory_server 句柄 + 上传 staged snapshot
        # 必须在关 http pool 之前，因为 release / upload 都依赖 internal_http_client
        any_release_failed = False
        failed_release_characters: list[str] = []
        try:
            from main_routers.characters_router import release_memory_server_character

            releasable_names = sorted(name for name, _mgr in _iter_session_managers())

            # 并发释放所有角色句柄：给整体一个 3s 总预算，而不是 N*1s 串行
            # memory_server 端是独立进程，/release_character 之间没有共享状态依赖，
            # 可以安全并发；单角色仍设 2.5s 上限避免慢调用拖尾
            async def _release_one(
                character_name: str,
            ) -> tuple[str, bool, Exception | None]:
                try:
                    released = await asyncio.wait_for(
                        release_memory_server_character(
                            character_name,
                            reason=f"Steam Auto-Cloud pre-shutdown release: {character_name}",
                        ),
                        timeout=2.5,
                    )
                    return character_name, bool(released), None
                except Exception as e:
                    return character_name, False, e

            if releasable_names:
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(
                            *(_release_one(n) for n in releasable_names),
                            return_exceptions=False,
                        ),
                        timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    any_release_failed = True
                    failed_release_characters = list(releasable_names)
                    logger.warning(
                        "Steam Auto-Cloud pre-shutdown release phase exceeded 3.0s budget; assuming all characters not fully released"
                    )
                    results = []

                for character_name, ok, err in results:
                    if ok:
                        continue
                    any_release_failed = True
                    failed_release_characters.append(character_name)
                    if err is None:
                        logger.warning(
                            "Steam Auto-Cloud pre-shutdown release failed for %s: returned False; uploaded snapshot may be stale/incomplete",
                            character_name,
                        )
                    else:
                        logger.warning(
                            "Steam Auto-Cloud pre-shutdown release failed for %s: %s; uploaded snapshot may be stale/incomplete",
                            character_name,
                            err,
                        )
        except Exception as e:
            any_release_failed = True
            failed_release_characters = ["<release_phase_error>"]
            logger.warning(
                f"Steam Auto-Cloud pre-shutdown release phase failed: {e}; uploaded snapshot may be stale/incomplete"
            )

        if any_release_failed:
            logger.warning(
                "Steam Auto-Cloud shutdown staged snapshot upload skipped because pre-shutdown release failed for: %s",
                ", ".join(sorted(set(failed_release_characters)))
                if failed_release_characters
                else "<unknown>",
            )
        else:
            try:
                upload_action_kwargs = {
                    "reason": "main_server_shutdown_remote_upload",
                    "budget_seconds": 5.0,
                }
                if steamworks is not None:
                    upload_action_kwargs["steamworks"] = steamworks
                remote_upload_result = await _run_cloudsave_manager_action(
                    "upload_existing_snapshot",
                    **upload_action_kwargs,
                )
                logger.info(
                    "Steam Auto-Cloud shutdown staged snapshot upload: %s",
                    remote_upload_result,
                )
            except CloudsaveDeadlineExceeded:
                logger.warning(
                    "Steam Auto-Cloud shutdown staged snapshot upload exceeded 5.0s budget; source launch may leave Steam remote snapshot unchanged"
                )
            except Exception as e:
                logger.warning(
                    f"Steam Auto-Cloud shutdown staged snapshot upload failed: {e}"
                )

        current_config = get_start_config()
        if current_config.get("shutdown_memory_server_on_exit"):
            current_config["shutdown_memory_server_on_exit"] = False
            await _request_memory_server_shutdown()

        # 关闭内部共享 httpx 连接池（必须在 release/upload 之后，因为它们依赖此 pool）
        try:
            from utils.internal_http_client import aclose_internal_http_client

            await asyncio.wait_for(aclose_internal_http_client(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("internal_http_client 清理超时，已强制跳过。")
        except Exception as e:
            logger.debug(f"internal_http_client 清理失败: {e}", exc_info=True)

        # 关闭外部共享 httpx 连接池
        try:
            from utils.external_http_client import aclose_external_http_client

            await asyncio.wait_for(aclose_external_http_client(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("external_http_client 清理超时，已强制跳过。")
        except Exception as e:
            logger.debug(f"external_http_client 清理失败: {e}", exc_info=True)

        # Terminal manager closes that missed the initial shared budget stayed
        # strongly owned while the remaining shutdown phases ran.  Give them a
        # final bounded drain before returning control to the event loop.  Do
        # not cancel the wrappers here: IndependentAsrRuntime.close() shields
        # its own terminal task, so cancelling only the wrapper would hide a
        # still-running cleanup from this owner.
        if pending_terminal_close_tasks:
            _settled, still_pending = await asyncio.wait(
                pending_terminal_close_tasks,
                timeout=0.5,
            )
            if still_pending:
                still_pending_names = sorted(
                    terminal_close_tasks[task] for task in still_pending
                )
                logger.error(
                    "session_manager terminal close still pending at event-loop "
                    "shutdown boundary (owned tasks retained): %s",
                    ", ".join(still_pending_names),
                )



# 使用 FastAPI 的 app.state 来管理启动配置
def get_start_config():
    """Get the startup config from app.state"""
    if hasattr(app.state, "start_config"):
        return app.state.start_config
    return {
        "browser_mode_enabled": False,
        "browser_page": "character_card_manager",
        "shutdown_memory_server_on_exit": False,
        "request_runtime_shutdown": None,
        "server": None,
    }


def set_start_config(config):
    """Store the startup config into app.state"""
    app.state.start_config = config


importlib.import_module(
    f"{__package__}._shared"
).runtime.get_start_config = get_start_config


async def request_application_shutdown_async(*, reason: str = "application_request"):
    """Request an application-level shutdown compatible with both launcher modes."""
    current_config = get_start_config()
    request_runtime_shutdown = current_config.get("request_runtime_shutdown")
    if callable(request_runtime_shutdown):
        try:
            await asyncio.sleep(0.5)
            result = request_runtime_shutdown(reason=reason)
            if inspect.isawaitable(result):
                await result
            return
        except Exception as exc:
            logger.error("触发运行时级应用关闭失败: %s", exc, exc_info=True)

    if current_config.get("server") is not None:
        await shutdown_server_async()
        return

    launcher_pid_raw = os.environ.get("NEKO_LAUNCHER_PID", "").strip()
    if os.name != "nt" and launcher_pid_raw:
        try:
            launcher_pid = int(launcher_pid_raw)
        except ValueError:
            launcher_pid = 0

        if launcher_pid > 0 and launcher_pid != os.getpid():
            loop = asyncio.get_running_loop()

            def _request_launcher_shutdown() -> None:
                try:
                    os.kill(launcher_pid, signal.SIGTERM)
                except Exception as exc:
                    logger.error("触发 launcher 级关闭失败: %s", exc)

            loop.call_later(1.0, _request_launcher_shutdown)
            return

    await shutdown_server_async()


from .workshop_runtime import _init_and_mount_workshop, _schedule_workshop_sync  # noqa: F401


async def shutdown_server_async():
    """Shut down the server asynchronously"""
    try:
        # 短暂延时，确保 beacon 响应有机会先发送
        await asyncio.sleep(0.5)
        logger.info("正在关闭服务器...")

        # 取消后台创意工坊任务，避免残留协程
        await _cancel_workshop_background_tasks(timeout=5.0)
        # HEAD: memory_server shutdown signal moved into on_shutdown via
        # _request_memory_server_shutdown() to share internal_http_client pool

        # 通知服务器退出
        current_config = get_start_config()
        current_config["shutdown_memory_server_on_exit"] = True
        if current_config["server"] is not None:
            current_config["server"].should_exit = True
    except Exception as e:
        logger.error(f"关闭服务器时出错: {e}")


importlib.import_module(
    f"{__package__}._shared"
).runtime.shutdown_server_async = shutdown_server_async
importlib.import_module(
    f"{__package__}._shared"
).runtime.request_application_shutdown_async = request_application_shutdown_async


# Steam 创意工坊管理相关API路由
# 确保这个路由被正确注册
if _IS_MAIN_PROCESS:
    logger.info("注册Steam创意工坊扫描API路由")


from .process_utils import (  # noqa: F401
    _format_size,
    _get_port_owners,
    _is_port_available,
    find_preview_image_in_folder,
    get_folder_size,
)
