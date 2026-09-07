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

"""Service orchestration and main runtime for the root launcher facade."""

from __future__ import annotations

from .bootstrap import (
    IS_FROZEN,
    bundle_dir,
    os,
    sys,
    _configure_stdio_utf8,
    _get_project_venv_python,
    _maybe_reexec_into_project_venv,
)

# Runtime helpers historically resolved __file__ to the repository launcher.
__file__ = os.path.join(bundle_dir, 'launcher.py')

import subprocess
import socket
import time
import threading
import itertools
import ctypes
import atexit
import signal
import json
import logging
import uuid
import importlib
import multiprocessing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict
from multiprocessing import Process, freeze_support, Event

# ``plugin/`` is also used as an import root for user-plugin processes and it
# contains a sibling ``config`` package.  Keep the repository root first here,
# otherwise a long-lived test process (or an embedded plugin host) can resolve
# the launcher's top-level ``config`` imports to ``plugin.config`` instead.
_PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
while _PROJECT_ROOT in sys.path:
    sys.path.remove(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

import config as config_module
from config import APP_NAME, MAIN_SERVER_PORT, MEMORY_SERVER_PORT, TOOL_SERVER_PORT
from utils import parent_guard, single_instance
from utils.port_utils import (
    probe_neko_health,
    acquire_startup_lock,
    release_startup_lock,
    get_hyperv_excluded_ranges,
    is_port_in_excluded_range,
    set_port_probe_reuse,
)
from utils.root_state_lock import root_state_transaction
from utils.cloudsave_runtime import (
    CLOUDSAVE_DISABLED_ENV,
    CLOUDSAVE_DISABLED_LOCAL_STATE_UNAVAILABLE,
    ROOT_MODE_BOOTSTRAP_IMPORTING,
    ROOT_MODE_MAINTENANCE_READONLY,
    ROOT_MODE_NORMAL,
    bootstrap_local_cloudsave_environment,
    cloud_apply_fence,
    set_root_mode,
    should_write_root_mode_normal_after_startup,
)
from utils.cloudsave_autocloud import get_cloudsave_manager
from utils.config_manager import get_config_manager, reset_config_manager_cache
from utils.storage_layout import clear_storage_layout_env, export_storage_layout_to_env, resolve_storage_layout
from utils.storage_migration import run_pending_storage_migration
from utils.storage_policy import paths_equal


def _configure_multiprocessing_executable(project_dir: str) -> None:
    """Force macOS/Windows spawn children to reuse the project virtualenv."""
    if IS_FROZEN:
        return

    candidate = _get_project_venv_python(project_dir)
    if not candidate:
        return

    try:
        multiprocessing.set_executable(os.path.abspath(candidate))
    except Exception as exc:
        print(f"[Launcher] Warning: failed to pin multiprocessing executable: {exc}", flush=True)


# 本次 launcher 启动的唯一标识
LAUNCH_ID = ""
# 实例 ID：在显式启动路径中初始化，确保导入模块时不改动进程环境
INSTANCE_ID = ""

JOB_HANDLE = None
_cleanup_lock = threading.Lock()
_cleanup_done = False
_expected_launcher_shutdown = False
_existing_neko_services: set[str] = set()  # 已有 N.E.K.O 实例占用的端口键
_partial_or_mixed_existing_backend = False
DEFAULT_PORTS = {
    "MAIN_SERVER_PORT": MAIN_SERVER_PORT,
    "MEMORY_SERVER_PORT": MEMORY_SERVER_PORT,
    "TOOL_SERVER_PORT": TOOL_SERVER_PORT,
}
INTERNAL_DEFAULT_PORTS = {
    "USER_PLUGIN_SERVER_PORT": 48916,
    "AGENT_MQ_PORT": 48917,
    "MAIN_AGENT_EVENT_PORT": 48918,
    "ZMQ_SESSION_PUB_PORT": 48961,
    "ZMQ_AGENT_PUSH_PORT": 48962,
    "ZMQ_ANALYZE_PUSH_PORT": 48963,
}
# 该区间保留给 N.E.K.O 已知默认端口，避免 fallback 与伴生服务冲突。
AVOID_FALLBACK_PORTS = set(range(48911, 48919)) | {48961, 48962, 48963}

# 模块名到端口键的映射（用于判断已有 N.E.K.O 实例是否占用对应端口）
MODULE_TO_PORT_KEY: dict[str, str] = {
    "memory_server": "MEMORY_SERVER_PORT",
    "agent_server": "TOOL_SERVER_PORT",
    "main_server": "MAIN_SERVER_PORT",
}
SHUTDOWN_MODULE_ORDER = (
    "main_server",
    "memory_server",
    "agent_server",
)
MERGED_SERVER_READY_TIMEOUT = 30.0
MERGED_SERVER_READY_POLL_INTERVAL = 0.25
MERGED_SERVER_SHUTDOWN_ORDER = ("Main", "Memory", "Agent")

#: Set by ``run_merged_servers`` while the in-process servers are running, so an
#: owner-death teardown can drive the ordered shutdown instead of hard-exiting
#: past it. ``None`` in multi-process mode, where ``cleanup_servers`` covers it.
_merged_shutdown_request = None
_merged_shutdown_complete = threading.Event()


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_uint64),
        ('WriteOperationCount', ctypes.c_uint64),
        ('OtherOperationCount', ctypes.c_uint64),
        ('ReadTransferCount', ctypes.c_uint64),
        ('WriteTransferCount', ctypes.c_uint64),
        ('OtherTransferCount', ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', _IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


def _get_windows_job_api():
    """Load kernel32 with pointer-sized signatures for Job Object calls."""
    if sys.platform != "win32":
        raise RuntimeError("Windows Job Object API is only available on win32")

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32

#: Upper bound on how long owner death waits for the merged ordered shutdown.
#: Matches the watchdog inside ``_begin_merged_shutdown``: once that fires the
#: process is going down regardless, so waiting longer buys nothing.
OWNER_DEATH_MERGED_SHUTDOWN_BUDGET = 45.0
MERGED_SERVER_SHUTDOWN_TIMEOUTS = {
    "Main": 20.0,
    "Memory": 12.0,
    "Agent": 8.0,
}


def _sync_runtime_config_globals(
    selected_public: dict[str, int] | None = None,
    selected_internal: dict[str, int] | None = None,
) -> None:
    """Keep the already-imported ``config`` module aligned with launcher choices.

    On Linux, ``multiprocessing`` often defaults to ``fork`` while macOS/Windows
    commonly use ``spawn``. Either way, only writing ``os.environ`` is not enough:
    forked children can inherit the parent's already-imported ``config`` module
    object, and spawned children can still observe stale globals if imports happen
    before launcher-selected overrides are reloaded.

    Syncing the module globals here ensures forked children and modules imported
    after forking observe the negotiated runtime ports and shared instance id.
    """
    updates: dict[str, int | str] = {"INSTANCE_ID": INSTANCE_ID}
    if selected_public:
        updates.update(selected_public)
    if selected_internal:
        updates.update(selected_internal)

    for key, value in updates.items():
        setattr(config_module, key, value)


def _reload_runtime_config_from_env() -> None:
    """Reload ``config`` inside a child process and sync launcher globals.

    Even after the parent has updated ``config`` globals, a forked child can still
    inherit stale module state from any earlier imports. Reloading ``config`` from
    the negotiated ``NEKO_*`` environment variables gives each server process a
    fresh source of truth before importing its heavy application modules.
    """
    global INSTANCE_ID, MAIN_SERVER_PORT, MEMORY_SERVER_PORT, TOOL_SERVER_PORT

    # ``config`` is a compatibility facade that re-exports values from the cached
    # ``config.network`` submodule. Refresh the source module first so reloading
    # the facade cannot restore pre-negotiation ports over launcher fallbacks.
    network_config = importlib.import_module("config.network")
    importlib.reload(network_config)
    reloaded = importlib.reload(config_module)
    INSTANCE_ID = str(reloaded.INSTANCE_ID)
    MAIN_SERVER_PORT = int(reloaded.MAIN_SERVER_PORT)
    MEMORY_SERVER_PORT = int(reloaded.MEMORY_SERVER_PORT)
    TOOL_SERVER_PORT = int(reloaded.TOOL_SERVER_PORT)
    _sync_runtime_config_globals(
        {
            "MAIN_SERVER_PORT": MAIN_SERVER_PORT,
            "MEMORY_SERVER_PORT": MEMORY_SERVER_PORT,
            "TOOL_SERVER_PORT": TOOL_SERVER_PORT,
        },
        {
            "USER_PLUGIN_SERVER_PORT": int(reloaded.USER_PLUGIN_SERVER_PORT),
            "AGENT_MQ_PORT": int(reloaded.AGENT_MQ_PORT),
            "MAIN_AGENT_EVENT_PORT": int(reloaded.MAIN_AGENT_EVENT_PORT),
        },
    )


def _install_logging_brace_compat() -> None:
    if getattr(logging, "_neko_brace_compat_installed", False):
        return

    original_get_message = logging.LogRecord.getMessage

    def _compat_get_message(record: logging.LogRecord) -> str:
        try:
            return original_get_message(record)
        except TypeError:
            msg = str(record.msg)
            args = record.args
            if not args or "%" in msg or "{" not in msg or "}" not in msg:
                raise
            try:
                if isinstance(args, dict):
                    return msg.format(**args)
                if not isinstance(args, tuple):
                    args = (args,)
                return msg.format(*args)
            except Exception:
                return f"{msg} | args={record.args!r}"

    logging.LogRecord.getMessage = _compat_get_message
    logging._neko_brace_compat_installed = True


def _patch_http_user_agents() -> None:
    """全局 patch HTTP 客户端的默认 User-Agent，防止 CF 拦截自定义 API。"""
    try:
        from utils.http_client import patch_requests_default_user_agent
        patch_requests_default_user_agent()
    except Exception as exc:
        print(f"[Launcher] Warning: failed to patch HTTP User-Agent: {exc}", flush=True)


def _initialize_launcher_context() -> None:
    """Populate per-launch ids and env only during explicit launcher startup."""
    global LAUNCH_ID, INSTANCE_ID

    if not LAUNCH_ID:
        LAUNCH_ID = uuid.uuid4().hex

    if not INSTANCE_ID:
        INSTANCE_ID = os.environ.get("NEKO_INSTANCE_ID") or uuid.uuid4().hex
        os.environ.setdefault("NEKO_INSTANCE_ID", INSTANCE_ID)
        _sync_runtime_config_globals()

    # 确保本地服务间通信不走系统代理（防止 Clash/Surge 等代理软件拦截 localhost 请求）
    # httpx 优先读小写 no_proxy，因此大小写都需要设置
    # 使用精确 token 匹配，防止 "127.0.0.1" in "127.0.0.10" 这类子串误判
    for _key in ("NO_PROXY", "no_proxy"):
        _no_proxy_raw = os.environ.get(_key, "")
        _tokens = set(map(str.strip, filter(None, _no_proxy_raw.split(","))))
        for _host in ("127.0.0.1", "localhost"):
            _tokens.add(_host)
        os.environ[_key] = ",".join(_tokens)


def _bootstrap_launcher_runtime(project_dir: str) -> None:
    """Run launcher bootstrap only from the explicit startup path."""
    _configure_stdio_utf8()
    _maybe_reexec_into_project_venv(project_dir)
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    os.chdir(project_dir)
    _configure_multiprocessing_executable(project_dir)
    _install_logging_brace_compat()
    _initialize_launcher_context()
    # 全局 patch requests 库的默认 User-Agent，防止 CF 拦截
    _patch_http_user_agents()


def _show_error_dialog(message: str):
    """Show an error dialog in the Windows packaged scenario."""
    if sys.platform != 'win32':
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, f"{APP_NAME} 启动失败", 0x10)
    except Exception:
        pass


def emit_frontend_event(event_type: str, payload: dict | None = None):
    """Emit a machine-readable event to Electron via stdout.

    Every event carries a *launch_id* so the frontend can ignore events from stale
    (zombie) processes.
    """
    envelope = {
        "source": "neko_launcher",
        "event": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "launch_id": LAUNCH_ID,
        "payload": payload or {},
    }
    print(f"NEKO_EVENT {json.dumps(envelope, ensure_ascii=True, separators=(',', ':'))}", flush=True)


def _resolve_storage_layout_for_launch() -> dict:
    clear_storage_layout_env()
    reset_config_manager_cache()
    config_manager = get_config_manager(APP_NAME, migrate=False)

    try:
        migration_result = run_pending_storage_migration(config_manager)
    except Exception as exc:
        print(f"[Launcher] Warning: pending storage migration processing failed: {exc}", flush=True)
        migration_result = {
            "attempted": False,
            "completed": False,
            "error_message": str(exc),
        }

    reset_config_manager_cache()
    resolved_config_manager = get_config_manager(APP_NAME, migrate=False)
    layout = resolve_storage_layout(resolved_config_manager)
    export_storage_layout_to_env(layout)
    reset_config_manager_cache()
    return {
        "layout": layout,
        "migration_result": migration_result,
    }


def _build_launcher_relaunch_command() -> list[str]:
    if IS_FROZEN:
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, os.path.abspath(__file__), *sys.argv[1:]]


#: Set by an owner that will relaunch the runtime itself after a clean exit.
#: When present, a storage restart is a *request*, not a self-spawn.
OWNER_RELAUNCH_ENV = "NEKO_OWNER_RELAUNCH"

#: Marks the replacement launcher of a generation handoff so it can wait out the
#: outgoing launcher's single-instance lock instead of concluding "already running".
RESTART_HANDOFF_ENV = "NEKO_LAUNCHER_RESTART_HANDOFF"

#: How long a losing launcher waits for the winner to publish its record before
#: reporting the duplicate with an empty owner. Bounded: a winner whose record
#: write failed would otherwise hang us indefinitely.
DUPLICATE_RECORD_READ_BUDGET = 0.25

RESTART_HANDOFF_LOCK_RETRIES = 40
RESTART_HANDOFF_LOCK_RETRY_INTERVAL = 0.25


def _owner_will_relaunch() -> bool:
    return os.environ.get(OWNER_RELAUNCH_ENV, "").strip().lower() in ("1", "true", "yes")


def _relax_job_kill_on_close() -> None:
    """Stop our own Job Object from taking the replacement launcher with us.

    ``setup_job_object`` sets ``KILL_ON_JOB_CLOSE`` so our servers cannot outlive
    us. A replacement launcher spawned for a storage restart inherits the job
    (it must — breaking away would be the very detachment we forbid), so the
    flag has to be cleared before we exit. By this point ``cleanup_servers`` has
    already run, so nothing else depends on the flag.
    """
    if sys.platform != "win32" or not JOB_HANDLE:
        return
    try:
        kernel32 = _get_windows_job_api()
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0
        if not kernel32.SetInformationJobObject(
            JOB_HANDLE,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            print(
                f"[Launcher] Warning: failed to relax Job kill-on-close (err={_get_last_error()})",
                flush=True,
            )
    except Exception as exc:
        print(f"[Launcher] Warning: failed to relax Job kill-on-close: {exc}", flush=True)


def _spawn_restarted_launcher() -> None:
    """Start the next launcher generation *attached*, never detached.

    The previous implementation used ``DETACHED_PROCESS`` / ``start_new_session``
    so the replacement would survive this process's exit. That handed the owner a
    runtime it had never spawned and could not prove it owned — the single
    biggest reason downstream needed a process-group anchor and a persistent
    ownership lease at all. The replacement now stays in this process group and
    this Job, inherits the owner's stdio (so its ``NEKO_EVENT`` stream keeps
    flowing over the same pipe), and is told to watch the *owner*, not us.
    """
    command = _build_launcher_relaunch_command()
    relaunch_env = os.environ.copy()
    # ``main_server`` uses this marker only to suppress duplicate module-level
    # init within the *current* Python process tree (mainly Windows spawn).
    # A storage-location relaunch is a brand-new launcher instance and must
    # re-run full startup initialization, so we must not inherit the marker.
    relaunch_env.pop("_NEKO_MAIN_SERVER_INITIALIZED", None)
    # We are about to exit; the replacement's parent-death guard must track the
    # process that owns *both* of us rather than this soon-to-be-dead launcher.
    # The owner is *propagated* across generations rather than recomputed from
    # our direct parent: from the second handoff onwards our own parent is the
    # previous launcher, which has already exited, so os.getppid() would name
    # init (or a stale pid) and the next generation would watch nothing. The
    # guard already resolved the real owner once, at install time.
    guard = _parent_death_guard
    owner_pid = guard.parent_pid if guard is not None else 0
    if owner_pid <= 1:
        owner_pid = os.getppid() if hasattr(os, "getppid") else 0
    if owner_pid and owner_pid > 1:
        relaunch_env[parent_guard.PARENT_PID_ENV] = str(owner_pid)
        # Hand down the owner's start token as well. We could verify it against
        # the kernel while the owner was still our own parent; the replacement
        # cannot, and would otherwise record whatever process holds that pid by
        # the time it starts — matching a stranger forever after.
        token = guard.owner_start_token if guard is not None else ""
        if token:
            relaunch_env[parent_guard.OWNER_TOKEN_ENV] = token
        else:
            relaunch_env.pop(parent_guard.OWNER_TOKEN_ENV, None)
    else:
        relaunch_env.pop(parent_guard.PARENT_PID_ENV, None)
        relaunch_env.pop(parent_guard.OWNER_TOKEN_ENV, None)
    relaunch_env[RESTART_HANDOFF_ENV] = "1"

    # Relax the Job only once the replacement actually exists. Clearing
    # KILL_ON_JOB_CLOSE first means a failed spawn leaves us with a Job that no
    # longer reaps anything, so any server we did not record would survive our
    # exit — losing containment in exchange for a replacement that never
    # started. The replacement inherits the Job at spawn time, so clearing the
    # flag immediately afterwards is still in time to spare it.
    #
    # Known residual, POSIX: if this generation happens to lead its own process
    # group, the replacement inherits that pgid but is not its leader, so
    # _own_process_group_id() returns None for it and a later owner death skips
    # the group sweep. Only reachable where the *first* generation was a group
    # leader — a terminal job, or an owner that spawned us detached — since the
    # normal shape leaves us inside the owner's group, where no generation ever
    # had that anchor. Not fixed by giving the replacement its own group: that
    # means setpgid, which would leave the owner's group and defeat the owner's
    # own sweep, i.e. exactly the escape this branch exists to remove. Setting
    # NEKO_OWNER_RELAUNCH avoids the self-spawn path entirely.
    #
    # Known residual, Windows self-spawn path only: clearing the flag also
    # un-manages anything that outlived cleanup_servers() in the outgoing Job
    # (plugins, MCP servers, Chromium). The replacement's own setup_job_object()
    # creates a *different* Job and cannot recapture them. There is no cheap fix
    # — keeping the flag would kill the replacement along with us, since it is a
    # member. The owner-driven path does not have this gap at all: with
    # NEKO_OWNER_RELAUNCH set we exit cleanly, the Job closes with the flag
    # intact and reaps everything, and the owner spawns the next generation
    # fresh. That is why owner relaunch is the preferred branch and this is the
    # fallback.
    proc = subprocess.Popen(
        command,
        cwd=os.getcwd(),
        env=relaunch_env,
        close_fds=True,
    )
    _relax_job_kill_on_close()
    return proc


def _mark_expected_launcher_shutdown() -> None:
    global _expected_launcher_shutdown
    _expected_launcher_shutdown = True


def _is_expected_launcher_shutdown() -> bool:
    return bool(_expected_launcher_shutdown)


STARTUP_WAIT_RESULT_STORAGE_RESTART = "storage_restart_requested"


def _is_pending_storage_restart_request() -> bool:
    try:
        config_manager = get_config_manager(APP_NAME, migrate=False)
        load_root_state = getattr(config_manager, "load_root_state", None)
        if not callable(load_root_state):
            return False

        root_state = load_root_state()
        if not isinstance(root_state, dict):
            return False

        root_mode = str(root_state.get("mode") or "").strip()
        last_migration_result = str(root_state.get("last_migration_result") or "").strip()
        if root_mode != ROOT_MODE_MAINTENANCE_READONLY:
            return False

        return last_migration_result.startswith(("restart_pending:", "restart_rebind:"))
    except Exception as exc:
        print(f"[Launcher] Warning: failed to inspect storage restart intent: {exc}", flush=True)
        return False


def _maybe_schedule_storage_restart() -> bool:
    if _owner_death_in_progress:
        # The owner is gone, so this generation must die rather than deliver the
        # next one. On Windows the self-spawn path clears KILL_ON_JOB_CLOSE to
        # spare the replacement, which would strip the only containment anchor we
        # have left just as the teardown is about to exit — and the replacement
        # would immediately exit too, having no owner. The pending restart stays
        # in root_state and is applied by the next launcher the owner starts.
        return False

    pre_restart_root_state: dict[str, object] = {}
    try:
        config_manager = get_config_manager(APP_NAME, migrate=False)
        load_root_state = getattr(config_manager, "load_root_state", None)
        if callable(load_root_state):
            loaded_root_state = load_root_state()
            if isinstance(loaded_root_state, dict):
                pre_restart_root_state = loaded_root_state
    except Exception as exc:
        print(f"[Launcher] Warning: failed to inspect root_state before restart scheduling: {exc}", flush=True)

    storage_bootstrap = _resolve_storage_layout_for_launch()
    migration_result = storage_bootstrap.get("migration_result") or {}
    restart_reason = ""

    if bool(migration_result.get("attempted")):
        restart_reason = "migration"
    else:
        root_mode = str(pre_restart_root_state.get("mode") or "").strip()
        last_migration_result = str(pre_restart_root_state.get("last_migration_result") or "").strip()
        last_migration_source = str(pre_restart_root_state.get("last_migration_source") or "").strip()
        previous_current_root = str(pre_restart_root_state.get("current_root") or "").strip()
        layout = storage_bootstrap.get("layout") if isinstance(storage_bootstrap.get("layout"), dict) else {}
        resolved_selected_root = str(layout.get("selected_root") or "").strip()
        if (
            root_mode == ROOT_MODE_MAINTENANCE_READONLY
            and last_migration_result.startswith("restart_rebind:")
        ):
            restart_reason = "rebind_only"
        elif (
            resolved_selected_root
            and previous_current_root
            and last_migration_source
            and paths_equal(last_migration_source, resolved_selected_root)
            and not paths_equal(previous_current_root, resolved_selected_root)
        ):
            restart_reason = "rebind_only"

    if not restart_reason:
        return False

    owner_relaunch = _owner_will_relaunch()
    emit_frontend_event(
        "storage_migration_restart",
        {
            "completed": bool(migration_result.get("completed")) or restart_reason == "rebind_only",
            "error_code": str(migration_result.get("error_code") or ""),
            "error_message": str(migration_result.get("error_message") or ""),
            "layout": storage_bootstrap.get("layout") or {},
            "restart_reason": restart_reason,
            # 告诉属主由谁负责起下一代：属主重启是首选，自旋是无属主时的回退。
            "relaunch": "owner" if owner_relaunch else "self",
        },
    )
    if _owner_death_in_progress:
        # The owner died while we were resolving the storage layout. The entry
        # check above was true when we started; this is the commit point, and
        # past it we would release the lock and spawn a replacement for an owner
        # that no longer exists.
        return False

    release_single_instance_ownership()
    if owner_relaunch:
        # 前台进程不自己复活。属主声明了会重启我们，就干净退出，让下一代由
        # 属主 spawn —— 这样它天生就是被属主拥有、被属主监督的进程。
        print("[Launcher] Storage restart delegated to owner; exiting cleanly", flush=True)
        return True
    _spawn_restarted_launcher()
    return True


def _persist_post_startup_root_state(config_manager) -> None:
    # 判定和写进同一个锁内事务，理由同 app/memory_server/runtime.py 那处：
    # "中间没有 await"只挡得住同一循环上的协程，挡不住工作线程上的存储变更写。
    with root_state_transaction():
        current_root_state = config_manager.load_root_state()
        if should_write_root_mode_normal_after_startup(current_root_state):
            set_root_mode(
                config_manager,
                ROOT_MODE_NORMAL,
                current_root=str(config_manager.app_docs_dir),
                last_known_good_root=str(config_manager.app_docs_dir),
                last_successful_boot_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            return

    print(
        "[Launcher] Preserving non-normal root_state after startup: "
        f"{current_root_state.get('mode') or ROOT_MODE_NORMAL}",
        flush=True,
    )


def report_startup_failure(message: str, show_dialog: bool = True):
    """Uniformly report startup failure: terminal + (optional) dialog."""
    normalized_message = str(message or "").strip().lower()
    if _is_expected_launcher_shutdown() and normalized_message.startswith(("start failed", "startup failed", "startup timeout", "startup aborted")):
        print(f"[Launcher] Suppressed startup failure during expected shutdown: {message}", flush=True)
        return
    print(message, flush=True)
    emit_frontend_event("startup_failure", {"message": message})
    if show_dialog and IS_FROZEN:
        _show_error_dialog(message)


def _get_last_error() -> int:
    """Get the most recent Win32 error code."""
    if sys.platform != 'win32':
        return 0
    get_last_error = getattr(ctypes, "get_last_error", None)
    if callable(get_last_error):
        return int(get_last_error())
    return int(ctypes.windll.kernel32.GetLastError())


_child_graceful_stop_hooks: list = []

#: Set inside a child server: the launcher pid recorded before the fork, and the
#: event the launcher sets when it starts its own ordered shutdown. Together they
#: let a child tell "the launcher is driving this" from "nobody is".
_spawning_launcher_pid = 0
_launcher_shutdown_event = None

#: How long a child waits for the launcher's ordered shutdown to reach it before
#: stopping on its own. Bounded: if the launcher never gets there, ignoring the
#: signal outright would be the residency violation this branch exists to remove.
CHILD_DEFER_TO_LAUNCHER_SECONDS = 10.0


def register_launcher_shutdown_event(event) -> None:
    """Remember the launcher's ordered-shutdown event inside a child server."""
    global _launcher_shutdown_event
    _launcher_shutdown_event = event


def register_child_graceful_stop_hook(hook) -> None:
    """Register a callable that stops this child server gracefully on SIGTERM."""
    _child_graceful_stop_hooks.append(hook)


def _handle_child_termination_signal(signum, _frame):
    """Stop this child server gracefully instead of dying where it stands.

    Reached when the whole process group is signalled (owner death, forced
    fallback) rather than through the launcher's ordered shutdown. Running the
    server's own shutdown lifecycle here preserves the release/cleanup ordering
    that the launcher would otherwise have driven.
    """
    # Guarded, and it matters here more than anywhere: this line runs *before* the
    # hooks that set uvicorn's should_exit, and every path that reaches this
    # handler — the group TERM after owner death, cleanup's terminate(), Linux
    # pdeathsig — is a path on which the owner has already closed the read end of
    # this pipe. A raise here would skip Main's release/cloudsave lifecycle
    # entirely, and on the pdeathsig path nothing else would come along to finish
    # the job.
    _teardown_print(f"[Launcher] Child server received signal {signum}; stopping gracefully")

    # A group-wide TERM (kill -- -<pgid>, a process-manager stopping the group)
    # reaches us and the launcher at the same instant. Stopping right now would
    # close Memory's listener while Main is still calling it to release the
    # character — the ordering the launcher's cleanup_servers exists to enforce.
    # Before this branch os.setsid() hid children from such a broadcast; removing
    # it is what exposed them, so this is where that ordering is put back.
    #
    # Only defer while the launcher is demonstrably still there and driving:
    # bounded, and if its shutdown never arrives we stop anyway. Ignoring a
    # termination signal outright is the one outcome not on the table.
    event = _launcher_shutdown_event
    if (
        event is not None
        and not event.is_set()
        and _spawning_launcher_pid
        and os.name == "posix"
        and parent_guard._safe_getppid() == int(_spawning_launcher_pid)
    ):
        threading.Thread(
            target=_stop_child_after_launcher,
            args=(event,),
            name="neko-child-defer-stop",
            daemon=True,
        ).start()
        return

    _run_child_graceful_stop()


def _stop_child_after_launcher(event) -> None:
    if not event.wait(CHILD_DEFER_TO_LAUNCHER_SECONDS):
        _teardown_print(
            "[Launcher] Launcher did not start its ordered shutdown within "
            f"{CHILD_DEFER_TO_LAUNCHER_SECONDS}s; stopping on our own"
        )
    _run_child_graceful_stop()


def _run_child_graceful_stop() -> None:
    for hook in list(_child_graceful_stop_hooks):
        try:
            hook()
        except Exception as exc:
            _teardown_print(f"[Launcher] Warning: child graceful stop hook failed: {exc}")
    if not _child_graceful_stop_hooks:
        raise SystemExit(0)


def _apply_child_process_signal_policy() -> None:
    """Shield launcher-managed servers from terminal SIGINT *without detaching*.

    This used to call ``os.setsid()``. That did stop a terminal ``Ctrl+C`` from
    racing ``memory_server`` ahead of ``main_server``'s release sequence, but it
    also put every server in a brand-new session — outside the launcher's
    process group. The group therefore never actually contained the servers, so
    a group-level sweep by an owner (or by the launcher itself) reached only the
    launcher, and the servers were free to outlive everything. That is exactly
    the daemonization escape the foreground-residency invariant forbids.

    The ordering intent is preserved with signal dispositions instead:
    ``SIGINT`` is ignored so only the launcher reacts to ``Ctrl+C`` and drives
    the ordered shutdown, while ``SIGTERM`` runs this server's own graceful
    shutdown. Both keep the child inside the launcher's process group.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    # A ``fork``ed child inherits the launcher's single-instance lock fd and its
    # module state. Drop the reference so the child neither keeps the lock alive
    # past the launcher nor deletes the record on its own exit path.
    single_instance.drop_inherited_reference()

    # register_shutdown_hooks() runs before start_server(), so a forked child
    # inherits the launcher's atexit teardown. Running it here would walk the
    # launcher's SERVERS list with process handles that mean nothing in this
    # process.
    try:
        atexit.unregister(cleanup_servers)
    except Exception:
        pass

    # Every platform, not just POSIX. On Windows a source (non-frozen) run is
    # multi-process and the children share the launcher's console, so Ctrl+C is
    # delivered to all four processes; combined with the uvicorn signal handlers
    # this function disables, each child would raise KeyboardInterrupt on its own
    # instead of letting the launcher drive the one ordered shutdown. The
    # try/except already covers a platform that refuses the disposition.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception as e:
        print(f"[Launcher] Warning: failed to shield child from SIGINT: {e}", flush=True)

    for name in ("SIGTERM", "SIGBREAK"):
        handler_signal = getattr(signal, name, None)
        if handler_signal is None:
            continue
        try:
            signal.signal(handler_signal, _handle_child_termination_signal)
        except Exception as e:
            print(f"[Launcher] Warning: failed to install child {name} handler: {e}", flush=True)

    # multiprocessing records the launcher's pid before forking us, so this
    # survives the case where the launcher dies before we get here.
    global _spawning_launcher_pid
    _spawning_parent = multiprocessing.parent_process()
    _spawning_launcher_pid = _spawning_parent.pid if _spawning_parent is not None else 0
    parent_guard.install_child_guard(
        _spawning_parent.pid if _spawning_parent is not None else None
    )


def _own_process_group_id() -> int | None:
    """Return this process's group id when it is the group *leader*, else ``None``.

    Only a leader may sweep its own group: if the launcher was started inside
    somebody else's group (a terminal's foreground job), a group signal would
    reach that owner too.
    """
    if os.name != "posix":
        return None
    try:
        pgid = os.getpgid(0)
    except OSError:
        return None
    return pgid if pgid == os.getpid() else None


def _sweep_own_process_group(sig: int) -> bool:
    """Signal every process in our own group. No-op unless we lead the group.

    This is the POSIX counterpart of the Windows Job Object: the containment
    anchor lives *inside* the runtime, so no external holder has to keep one
    alive on its behalf.
    """
    pgid = _own_process_group_id()
    if pgid is None:
        return False
    previous = None
    try:
        # The group signal comes back to us as well, and it must not terminate
        # the process that is running the sweep — that would stop the escalation
        # at its first step and strand exactly the grandchildren it exists to
        # reap.
        #
        # signal.signal() only works on the main thread, and this runs on a
        # daemon thread for every mechanism except Linux pdeathsig. Off the main
        # thread the disposition therefore cannot be changed here at all; what
        # protects us instead is _owner_death_in_progress, which makes
        # _handle_termination_signal return instead of exiting. Setting the
        # disposition is still worth doing when we *are* on the main thread,
        # because it also covers the startup window before that handler exists.
        try:
            if threading.current_thread() is threading.main_thread():
                previous = signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            previous = None
        os.killpg(pgid, sig)
        return True
    except (OSError, ProcessLookupError):
        return False
    finally:
        if previous is not None:
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):
                pass


def _iter_servers_for_shutdown():
    order = {module_name: index for index, module_name in enumerate(SHUTDOWN_MODULE_ORDER)}
    return sorted(
        SERVERS,
        key=lambda server: (order.get(server.get("module", ""), len(order)), server.get("name", "")),
    )


def setup_job_object():
    """
    Create a Windows Job Object and add the current process to it.
    Sets the JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE flag so that when the main
    process is killed, the OS automatically terminates all child processes,
    preventing orphaned processes from lingering.
    """
    global JOB_HANDLE
    if sys.platform != 'win32':
        return None

    try:
        kernel32 = _get_windows_job_api()
        from ctypes import wintypes

        # Job Object 常量
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        # 先检查当前进程是否已在某个 Job 中（Steam 场景常见）
        is_in_job = wintypes.BOOL(0)
        current_process = kernel32.GetCurrentProcess()
        if not kernel32.IsProcessInJob(current_process, None, ctypes.byref(is_in_job)):
            print(f"[Launcher] Warning: IsProcessInJob failed (err={_get_last_error()})", flush=True)
            is_in_job.value = 0

        # 创建 Job Object
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            print(f"[Launcher] Warning: Failed to create Job Object (err={_get_last_error()})", flush=True)
            return None

        # 设置 Job Object 信息
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION 结构体
        # 我们只需要设置 BasicLimitInformation.LimitFlags
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        result = kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info)
        )
        if not result:
            print(f"[Launcher] Warning: Failed to set Job Object info (err={_get_last_error()})", flush=True)
            kernel32.CloseHandle(job)
            return None

        # 将当前进程加入 Job Object
        result = kernel32.AssignProcessToJobObject(job, current_process)
        if not result:
            err = _get_last_error()
            if is_in_job.value:
                print(
                    f"[Launcher] Warning: Process is already inside another Job; "
                    f"nested Job assignment failed (err={err}). "
                    "Will rely on explicit process-tree cleanup fallback.",
                    flush=True
                )
            else:
                print(f"[Launcher] Warning: Failed to assign process to Job Object (err={err})", flush=True)
            kernel32.CloseHandle(job)
            return None

        # 保持 handle 在进程生命周期内有效（模块级引用）
        # 进程退出时句柄会关闭，触发 KILL_ON_JOB_CLOSE
        JOB_HANDLE = job
        print("[Launcher] Job Object created - child processes will auto-terminate on exit", flush=True)
        return job

    except Exception as e:
        print(f"[Launcher] Warning: Job Object setup failed: {e}", flush=True)
        return None

# 服务器配置（按内存占用从轻到重排列，用于分步启动以降低峰值内存）
SERVERS = [
    {
        'name': 'Memory Server',
        'module': 'memory_server',
        'port': MEMORY_SERVER_PORT,
        'process': None,
        'ready_event': None,
        'shutdown_complete_event': None,
        'graceful_shutdown_timeout': 12,
    },
    {
        'name': 'Main Server',
        'module': 'main_server',
        'port': MAIN_SERVER_PORT,
        'process': None,
        'ready_event': None,
        'shutdown_complete_event': None,
        'graceful_shutdown_timeout': 20,
    },
    {
        'name': 'Agent Server',
        'module': 'agent_server',
        'port': TOOL_SERVER_PORT,
        'process': None,
        'ready_event': None,
        'shutdown_complete_event': None,
        'graceful_shutdown_timeout': 8,
    },
]

# 不再启动主程序，用户自己启动 lanlan_frd.exe


# ===== 合并进程模式 =====
# 打包时三个 FastAPI 服务跑在同一个进程里，共享 Python 运行时，
# 省掉 2 份 CPython + uvicorn + 共享库的重复加载（约 150-200 MB）。
# 每个服务仍然监听自己的端口，前端 / 服务间 HTTP 调用零改动。


def _merged_health_issues(
    apps: list[tuple[object, int, str]],
    health_by_port: dict[int, dict | None],
) -> list[str]:
    """Return sanitized signed-health mismatches for merged startup."""
    issues: list[str] = []
    for _app, port, name in apps:
        health = health_by_port.get(port)
        expected_service = name.lower()
        if not health:
            issues.append(f"{name}:{port}:unreachable")
            continue
        if health.get("service") != expected_service:
            issues.append(f"{name}:{port}:wrong_service")
            continue
        if INSTANCE_ID and health.get("instance_id") != INSTANCE_ID:
            issues.append(f"{name}:{port}:wrong_instance")
    return issues


def _completed_merged_server(tasks: dict[str, object]) -> str | None:
    """Describe the first uvicorn task that exited, without exposing internals."""
    for name, task in tasks.items():
        if not task.done():
            continue
        if task.cancelled():
            return f"{name} server task was cancelled"
        exc = task.exception()
        if exc is not None:
            return f"{name} server task failed: {type(exc).__name__}: {exc}"
        return f"{name} server task exited"
    return None


async def _wait_for_merged_servers_ready(
    apps: list[tuple[object, int, str]],
    tasks: dict[str, object],
    *,
    timeout: float = MERGED_SERVER_READY_TIMEOUT,
    poll_interval: float = MERGED_SERVER_READY_POLL_INTERVAL,
) -> None:
    """Wait for all three signed health endpoints from the current instance."""
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if completed := _completed_merged_server(tasks):
            raise RuntimeError(f"Merged startup failed: {completed}")

        health_results = await asyncio.gather(
            *(
                asyncio.to_thread(probe_neko_health, port, timeout=0.25)
                for _app, port, _name in apps
            )
        )
        health_by_port = {
            port: health
            for (_app, port, _name), health in zip(apps, health_results)
        }
        last_issues = _merged_health_issues(apps, health_by_port)
        if not last_issues:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                "Merged startup timed out before signed health was ready: "
                + ", ".join(last_issues)
            )
        await asyncio.sleep(poll_interval)


async def _shutdown_merged_servers_in_order(
    servers: dict[str, object],
    tasks: dict[str, object],
    *,
    timeouts: dict[str, float] | None = None,
) -> list[str]:
    """Preserve the multi-process main -> memory -> agent shutdown contract."""
    import asyncio

    failures: list[str] = []
    timeout_by_name = timeouts or MERGED_SERVER_SHUTDOWN_TIMEOUTS
    for name in MERGED_SERVER_SHUTDOWN_ORDER:
        server = servers[name]
        task = tasks[name]
        if not task.done():
            server.should_exit = True
            done, _pending = await asyncio.wait(
                {task},
                timeout=timeout_by_name[name],
            )
            if not done:
                failures.append(f"{name}:shutdown_timeout")
                task.cancel()
                # Do not let one stuck service prevent the remaining services
                # from receiving their shutdown request.
                await asyncio.sleep(0)
                continue
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, BaseException):
            failures.append(f"{name}:{type(result).__name__}:{result}")
    return failures


def _disable_uvicorn_signal_handlers(server: object) -> None:
    """Keep one launcher-owned signal handler across supported Uvicorn APIs."""
    from contextlib import contextmanager

    server.install_signal_handlers = lambda: None
    if hasattr(server, "capture_signals"):
        @contextmanager
        def _capture_no_signals():
            yield

        server.capture_signals = _capture_no_signals


async def _serve_merged_server(server: object, name: str) -> None:
    """Convert Uvicorn's bind-time SystemExit into a launcher failure."""
    try:
        await server.serve()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        raise RuntimeError(
            f"{name} server exited during startup/runtime (code={code})"
        ) from exc

def run_merged_servers() -> int:
    """Single-process merged mode: 3 uvicorn.Server instances share one asyncio event loop."""
    import asyncio
    import uvicorn

    _reload_runtime_config_from_env()

    # frozen 环境通用设置
    if IS_FROZEN:
        if hasattr(sys, '_MEIPASS'):
            os.chdir(sys._MEIPASS)
        else:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
        try:
            import typeguard
            _dummy = lambda func=None, **kw: func if func else (lambda f: f)
            typeguard.typechecked = _dummy
            if hasattr(typeguard, '_decorators'):
                typeguard._decorators.typechecked = _dummy
        except Exception:
            pass

    _behind_proxy = os.environ.get("NEKO_BEHIND_PROXY", "").strip().lower() in ("1", "true", "yes")
    _proxy_kw: dict = {}
    if _behind_proxy:
        _proxy_kw = {"proxy_headers": True, "forwarded_allow_ips": "*"}

    # 分步 import（控制峰值内存 & 提供进度反馈），逐段计时：三段 import 串行坐在
    # 端口就绪关键路径上，回归过一次没人发现（#1496 优化后被 openai 2.x 静默吃回
    # 3.6 倍），日志留数字才能在用户报告启动慢时定位是哪段涨了。
    _t_import = time.perf_counter()
    print("[Merged] Importing memory_server...", flush=True)
    from app import memory_server
    _t_memory = time.perf_counter()
    print(f"[Merged] memory_server imported in {(_t_memory - _t_import) * 1000:.0f} ms", flush=True)
    print("[Merged] Importing agent_server...", flush=True)
    from app import agent_server
    _t_agent = time.perf_counter()
    print(f"[Merged] agent_server imported in {(_t_agent - _t_memory) * 1000:.0f} ms", flush=True)
    print("[Merged] Importing main_server...", flush=True)
    from app import main_server
    _t_main = time.perf_counter()
    print(
        f"[Merged] main_server imported in {(_t_main - _t_agent) * 1000:.0f} ms "
        f"(total import {(_t_main - _t_import) * 1000:.0f} ms)",
        flush=True,
    )

    _apps = [
        (memory_server.app, MEMORY_SERVER_PORT, "Memory"),
        (agent_server.app,  TOOL_SERVER_PORT,   "Agent"),
        (main_server.app,   MAIN_SERVER_PORT,   "Main"),
    ]

    servers: list[uvicorn.Server] = []
    for _app, _port, _name in _apps:
        cfg = uvicorn.Config(
            app=_app, host="127.0.0.1", port=_port,
            log_level="error", **_proxy_kw,
        )
        servers.append(uvicorn.Server(cfg))
    servers_by_name = {
        name: server
        for (_app, _port, name), server in zip(_apps, servers)
    }

    # ── 信号处理 ──
    # 3 个 uvicorn.Server 各自 install_signal_handlers() 会互相覆盖
    # （最后一个赢），导致 Ctrl+C 只通知 1 个退出，其余卡死。
    # 禁用各自的处理器，统一安装一个全局处理器。
    for s in servers:
        _disable_uvicorn_signal_handlers(s)

    _exiting = False
    _shutdown_watchdog_started = False

    def _begin_merged_shutdown(*, reason: str = "signal") -> bool:
        nonlocal _exiting, _shutdown_watchdog_started
        if _exiting:
            return False
        _exiting = True
        _mark_expected_launcher_shutdown()
        # Ordered shutdown can legitimately consume the same 20 + 12 + 8 second
        # budgets as multi-process cleanup. Keep the watchdog as a final escape,
        # not as a shorter competing deadline.
        watchdog_timeout = 60 if reason == "storage_location_restart" else 45
        _teardown_print(
            f"\n[Merged] Shutting down... (reason={reason}, watchdog={watchdog_timeout}s)"
        )
        # Main must finish release/cloudsave work while Memory is still alive.
        # The async coordinator advances Memory and Agent in order afterwards.
        for name in MERGED_SERVER_SHUTDOWN_ORDER:
            server = servers_by_name[name]
            if not server.should_exit:
                server.should_exit = True
                break
        if not _shutdown_watchdog_started:
            def _watchdog(timeout=watchdog_timeout):
                time.sleep(timeout)
                # The owner-death teardown waits on this same budget, so on a hung
                # merged shutdown this fires first and used to exit without ever
                # reaching the group escalation. Run it here before leaving.
                _escalate_own_process_group()
                os._exit(1)

            threading.Thread(
                target=_watchdog, daemon=True, name="merged-shutdown-watchdog",
            ).start()
            _shutdown_watchdog_started = True
        return True

    def _on_exit_signal(_sig, _frame):
        nonlocal _exiting
        if _exiting:
            # 第二次 Ctrl+C → 强制退出（与多进程模式行为一致）
            print("\n[Merged] Force exit!", flush=True)
            os._exit(1)
        _begin_merged_shutdown(reason=f"signal:{_sig}")

    try:
        main_server.set_start_config(
            {
                "browser_mode_enabled": False,
                "browser_page": "",
                "shutdown_memory_server_on_exit": False,
                "request_runtime_shutdown": lambda *, reason="application_request": _begin_merged_shutdown(
                    reason=reason
                ),
                "server": None,
            }
        )
    except Exception as exc:
        print(f"[Merged] Warning: failed to install merged shutdown bridge: {exc}", flush=True)

    _prev_sigint = signal.getsignal(signal.SIGINT)
    _prev_sigterm = signal.getsignal(signal.SIGTERM)
    _prev_sigbreak = (
        signal.getsignal(signal.SIGBREAK)
        if hasattr(signal, "SIGBREAK")
        else None
    )
    signal.signal(signal.SIGINT, _on_exit_signal)
    signal.signal(signal.SIGTERM, _on_exit_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_exit_signal)

    # Owner death has to reach the same ordered shutdown. In merged mode the
    # three servers live in *this* process, so SERVERS carries no child handles
    # and cleanup_servers() has nothing to act on — without this hand-off,
    # _handle_owner_death would run straight to os._exit(0) and take Main's
    # release/cloudsave sequence with it.
    global _merged_shutdown_request
    _merged_shutdown_complete.clear()
    _merged_shutdown_request = _begin_merged_shutdown

    async def _serve_all() -> None:
        # 并发启动所有 uvicorn.Server
        tasks = {
            name: asyncio.create_task(
                _serve_merged_server(server, name),
                name=f"merged-{name.lower()}",
            )
            for name, server in servers_by_name.items()
        }
        startup_error: Exception | None = None
        unexpected_exit: str | None = None
        try:
            try:
                await _wait_for_merged_servers_ready(_apps, tasks)
            except Exception as exc:
                if not _exiting:
                    startup_error = exc
                    _begin_merged_shutdown(reason="startup_failure")

            if startup_error is None and not _exiting:
                print(f"[Merged] All servers ready "
                      f"(ports {MEMORY_SERVER_PORT}/{TOOL_SERVER_PORT}/{MAIN_SERVER_PORT})",
                      flush=True)
                try:
                    _config_manager = get_config_manager(APP_NAME)
                    _persist_post_startup_root_state(_config_manager)
                except Exception as e:
                    print(f"[Merged] Warning: failed to persist root_state boot success: {e}", flush=True)
                _publish_ready_runtime_record("merged")
                emit_frontend_event("startup_ready", {
                    "instance_id": INSTANCE_ID,
                    "selected": {
                        "MAIN_SERVER_PORT": MAIN_SERVER_PORT,
                        "MEMORY_SERVER_PORT": MEMORY_SERVER_PORT,
                        "TOOL_SERVER_PORT": TOOL_SERVER_PORT,
                    },
                })

                # A single service returning is either an intentional coordinated
                # shutdown or a topology-wide failure. Never leave two ports alive.
                await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
                if not _exiting:
                    unexpected_exit = _completed_merged_server(tasks) or "unknown server exit"
                    _begin_merged_shutdown(reason="server_exit")

            failures = await _shutdown_merged_servers_in_order(servers_by_name, tasks)
            if startup_error is not None:
                raise startup_error
            if unexpected_exit is not None:
                raise RuntimeError(f"Merged runtime stopped unexpectedly: {unexpected_exit}")
            if failures:
                raise RuntimeError("Merged shutdown failures: " + ", ".join(failures))
        finally:
            for server in servers:
                server.should_exit = True
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.wait(tasks.values(), timeout=1.0)

    try:
        asyncio.run(_serve_all())
    except KeyboardInterrupt:
        # 备用路径：如果自定义信号处理器未拦截到（理论上不会走到这里）
        if not _exiting:
            for s in servers:
                s.should_exit = True
    finally:
        signal.signal(signal.SIGINT, _prev_sigint)
        signal.signal(signal.SIGTERM, _prev_sigterm)
        if hasattr(signal, "SIGBREAK") and _prev_sigbreak is not None:
            signal.signal(signal.SIGBREAK, _prev_sigbreak)
        # The servers are down; anyone waiting on the ordered shutdown (an
        # owner-death teardown) may proceed.
        _merged_shutdown_request = None
        _merged_shutdown_complete.set()

    return 0


def run_memory_server(
    ready_event: Event,
    import_event: Event | None = None,
    shutdown_event: Event | None = None,
    shutdown_complete_event: Event | None = None,
):
    """Run the Memory Server"""
    try:
        _apply_child_process_signal_policy()
        _reload_runtime_config_from_env()
        # 确保工作目录正确
        if IS_FROZEN:
            if hasattr(sys, '_MEIPASS'):
                # PyInstaller
                os.chdir(sys._MEIPASS)
            else:
                # Nuitka
                os.chdir(os.path.dirname(os.path.abspath(__file__)))
            # 禁用 typeguard（子进程需要重新禁用）
            try:
                import typeguard
                def dummy_typechecked(func=None, **kwargs):
                    return func if func else (lambda f: f)
                typeguard.typechecked = dummy_typechecked
                if hasattr(typeguard, '_decorators'):
                    typeguard._decorators.typechecked = dummy_typechecked
            except: # noqa
                pass

        from app import memory_server
        import uvicorn
        if import_event:
            import_event.set()

        print(f"[Memory Server] Starting on port {MEMORY_SERVER_PORT}")

        _behind_proxy = os.environ.get("NEKO_BEHIND_PROXY", "").strip().lower() in ("1", "true", "yes")
        # 使用 Server 对象，在启动后通知父进程
        config = uvicorn.Config(
            app=memory_server.app,
            host="127.0.0.1",
            port=MEMORY_SERVER_PORT,
            log_level="error",
            proxy_headers=_behind_proxy,
            forwarded_allow_ips="*" if _behind_proxy else None,
        )
        server = uvicorn.Server(config)
        # uvicorn 在主线程运行时会覆盖 _apply_child_process_signal_policy 装好的
        # 处置，把 Ctrl+C 重新变成"每个子服务各自退出"。禁掉它，保持单一
        # launcher 拥有的信号策略（与合并模式同一处理）。
        _disable_uvicorn_signal_handlers(server)

        if shutdown_complete_event is not None:
            async def _notify_shutdown_complete() -> None:
                # Same ordering rule as _watch_shutdown: this is the event
                # cleanup_servers actually waits on, so it must not be gated
                # behind a write to a pipe whose reader has gone.
                shutdown_complete_event.set()
                _teardown_print("[Memory Server] Shutdown lifecycle complete")

            memory_server.app.add_event_handler("shutdown", _notify_shutdown_complete)

        # 组级信号（属主猝死、强制兜底）不走 launcher 的有序关闭，
        # 由本进程自己驱动 uvicorn 的优雅退出，保持释放/清理顺序。
        register_child_graceful_stop_hook(lambda: setattr(server, "should_exit", True))

        if shutdown_event is not None:
            register_launcher_shutdown_event(shutdown_event)

            def _watch_shutdown() -> None:
                shutdown_event.wait()
                # Set first, log second. The owner is gone by the time this fires,
                # so the pipe is usually broken and a print here would kill this
                # thread before should_exit was ever set — the launcher would then
                # wait out the full graceful budget for a request it never made.
                server.should_exit = True
                _teardown_print("[Memory Server] Shutdown requested by launcher")

            threading.Thread(target=_watch_shutdown, name="memory-shutdown-watch", daemon=True).start()

        # 在后台线程中运行服务器
        import asyncio

        async def run_with_notify():
            # 启动服务器
            await server.serve()

        # 启动线程来运行服务器，并在启动后通知
        def run_server():
            # 创建事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 添加启动完成的回调
            async def startup():
                print(f"[Memory Server] Running on port {MEMORY_SERVER_PORT}")
                ready_event.set()

            # 将 startup 添加到服务器的启动事件
            server.config.app.add_event_handler("startup", startup)

            # 运行服务器
            loop.run_until_complete(server.serve())

        run_server()

    except Exception as e:
        print(f"Memory Server error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if shutdown_complete_event is not None:
            shutdown_complete_event.set()

def run_agent_server(
    ready_event: Event,
    import_event: Event | None = None,
    shutdown_event: Event | None = None,
    shutdown_complete_event: Event | None = None,
):
    """Run the Agent Server (no need to wait for initialization)"""
    try:
        _apply_child_process_signal_policy()
        _reload_runtime_config_from_env()
        # 确保工作目录正确
        if IS_FROZEN:
            if hasattr(sys, '_MEIPASS'):
                # PyInstaller
                os.chdir(sys._MEIPASS)
            else:
                # Nuitka
                os.chdir(os.path.dirname(os.path.abspath(__file__)))
            # 禁用 typeguard（子进程需要重新禁用）
            try:
                import typeguard
                def dummy_typechecked(func=None, **kwargs):
                    return func if func else (lambda f: f)
                typeguard.typechecked = dummy_typechecked
                if hasattr(typeguard, '_decorators'):
                    typeguard._decorators.typechecked = dummy_typechecked
            except: # noqa
                pass

        from app import agent_server
        import uvicorn
        if import_event:
            import_event.set()

        print(f"[Agent Server] Starting on port {TOOL_SERVER_PORT}")

        # Agent Server 不需要等待，立即通知就绪
        ready_event.set()

        _behind_proxy = os.environ.get("NEKO_BEHIND_PROXY", "").strip().lower() in ("1", "true", "yes")
        config = uvicorn.Config(
            app=agent_server.app,
            host="127.0.0.1",
            port=TOOL_SERVER_PORT,
            log_level="error",
            proxy_headers=_behind_proxy,
            forwarded_allow_ips="*" if _behind_proxy else None,
        )
        server = uvicorn.Server(config)
        # uvicorn 在主线程运行时会覆盖 _apply_child_process_signal_policy 装好的
        # 处置，把 Ctrl+C 重新变成"每个子服务各自退出"。禁掉它，保持单一
        # launcher 拥有的信号策略（与合并模式同一处理）。
        _disable_uvicorn_signal_handlers(server)

        if shutdown_complete_event is not None:
            async def _notify_shutdown_complete() -> None:
                # Same ordering rule as _watch_shutdown: this is the event
                # cleanup_servers actually waits on, so it must not be gated
                # behind a write to a pipe whose reader has gone.
                shutdown_complete_event.set()
                _teardown_print("[Agent Server] Shutdown lifecycle complete")

            agent_server.app.add_event_handler("shutdown", _notify_shutdown_complete)

        # 组级信号（属主猝死、强制兜底）不走 launcher 的有序关闭，
        # 由本进程自己驱动 uvicorn 的优雅退出，保持释放/清理顺序。
        register_child_graceful_stop_hook(lambda: setattr(server, "should_exit", True))

        if shutdown_event is not None:
            register_launcher_shutdown_event(shutdown_event)

            def _watch_shutdown() -> None:
                shutdown_event.wait()
                # Set first, log second. The owner is gone by the time this fires,
                # so the pipe is usually broken and a print here would kill this
                # thread before should_exit was ever set — the launcher would then
                # wait out the full graceful budget for a request it never made.
                server.should_exit = True
                _teardown_print("[Agent Server] Shutdown requested by launcher")

            threading.Thread(target=_watch_shutdown, name="agent-shutdown-watch", daemon=True).start()

        server.run()
    except Exception as e:
        print(f"Agent Server error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if shutdown_complete_event is not None:
            shutdown_complete_event.set()

def run_main_server(
    ready_event: Event,
    import_event: Event | None = None,
    shutdown_event: Event | None = None,
    shutdown_complete_event: Event | None = None,
):
    """Run the Main Server"""
    try:
        _apply_child_process_signal_policy()
        _reload_runtime_config_from_env()
        # 确保工作目录正确
        if IS_FROZEN:
            if hasattr(sys, '_MEIPASS'):
                # PyInstaller
                os.chdir(sys._MEIPASS)
            else:
                # Nuitka
                os.chdir(os.path.dirname(os.path.abspath(__file__)))

        print("[Main Server] Importing main_server module...")
        from app import main_server
        import uvicorn
        if import_event:
            import_event.set()

        print(f"[Main Server] Starting on port {MAIN_SERVER_PORT}")

        _behind_proxy = os.environ.get("NEKO_BEHIND_PROXY", "").strip().lower() in ("1", "true", "yes")
        # 直接运行 FastAPI app，不依赖 main_server 的 __main__ 块
        config = uvicorn.Config(
            app=main_server.app,
            host="127.0.0.1",
            port=MAIN_SERVER_PORT,
            log_level="error",
            loop="asyncio",
            reload=False,
            proxy_headers=_behind_proxy,
            forwarded_allow_ips="*" if _behind_proxy else None,
        )
        server = uvicorn.Server(config)
        # uvicorn 在主线程运行时会覆盖 _apply_child_process_signal_policy 装好的
        # 处置，把 Ctrl+C 重新变成"每个子服务各自退出"。禁掉它，保持单一
        # launcher 拥有的信号策略（与合并模式同一处理）。
        _disable_uvicorn_signal_handlers(server)
        try:
            main_server.set_start_config(
                {
                    "browser_mode_enabled": False,
                    "browser_page": "",
                    "shutdown_memory_server_on_exit": False,
                    "request_runtime_shutdown": None,
                    "server": server,
                }
            )
        except Exception as exc:
            print(f"[Main Server] Warning: failed to install launcher shutdown bridge: {exc}", flush=True)

        if shutdown_complete_event is not None:
            async def _notify_shutdown_complete() -> None:
                # Same ordering rule as _watch_shutdown: this is the event
                # cleanup_servers actually waits on, so it must not be gated
                # behind a write to a pipe whose reader has gone.
                shutdown_complete_event.set()
                _teardown_print("[Main Server] Shutdown lifecycle complete")

            main_server.app.add_event_handler("shutdown", _notify_shutdown_complete)

        # 组级信号（属主猝死、强制兜底）不走 launcher 的有序关闭，
        # 由本进程自己驱动 uvicorn 的优雅退出，保持释放/清理顺序。
        register_child_graceful_stop_hook(lambda: setattr(server, "should_exit", True))

        if shutdown_event is not None:
            register_launcher_shutdown_event(shutdown_event)

            def _watch_shutdown() -> None:
                shutdown_event.wait()
                # Set first, log second. The owner is gone by the time this fires,
                # so the pipe is usually broken and a print here would kill this
                # thread before should_exit was ever set — the launcher would then
                # wait out the full graceful budget for a request it never made.
                server.should_exit = True
                _teardown_print("[Main Server] Shutdown requested by launcher")

            threading.Thread(target=_watch_shutdown, name="main-shutdown-watch", daemon=True).start()

        # 添加启动完成的回调
        async def startup():
            print(f"[Main Server] Running on port {MAIN_SERVER_PORT}")
            ready_event.set()

        # 将 startup 添加到服务器的启动事件
        main_server.app.add_event_handler("startup", startup)

        # 运行服务器
        server.run()
    except Exception as e:
        # 兜底崩溃日志：即使主日志系统未初始化，也能保留首个异常原因
        try:
            import traceback
            crash_file = os.path.join(os.getcwd(), "main_server_bootstrap_crash.log")
            with open(crash_file, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 80 + "\n")
                f.write(f"[{datetime.now().isoformat()}] Main Server bootstrap error: {e}\n")
                f.write(traceback.format_exc())
                f.write("\n")
        except Exception:
            pass
        print(f"Main Server error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if shutdown_complete_event is not None:
            shutdown_complete_event.set()

def check_port(port: int, timeout: float = 0.5) -> bool:
    """Check whether the port is open"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        return result == 0
    except: # noqa
        return False


def get_port_owners(port: int) -> list[int]:
    """Query the PIDs of processes listening on the given port (best-effort)."""
    pids: set[int] = set()
    try:
        if sys.platform == 'win32':
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                check=False,
            )
            needle = f":{port}"
            for raw in result.stdout.splitlines():
                line = raw.strip()
                if "LISTENING" not in line or needle not in line:
                    continue
                parts = line.split()
                if not parts:
                    continue
                pid_str = parts[-1]
                if pid_str.isdigit():
                    pids.add(int(pid_str))
        else:
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                check=False,
            )
            for line in result.stdout.splitlines():
                s = line.strip()
                if s.isdigit():
                    pids.add(int(s))
    except Exception:
        pass
    return sorted(pids)


def _is_port_bindable(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        set_port_probe_reuse(sock)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _pick_fallback_port(preferred_port: int, reserved: set[int]) -> int | None:
    # 1) Prefer nearby ports first
    for port in range(preferred_port + 1, min(preferred_port + 101, 65535)):
        if port in reserved or port in AVOID_FALLBACK_PORTS:
            continue
        if _is_port_bindable(port):
            return port
    # 2) Fallback to any OS-assigned free port
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        set_port_probe_reuse(sock)
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()
        if port not in reserved and port not in AVOID_FALLBACK_PORTS:
            return port
    except Exception:
        pass
    return None


def _classify_port_conflict(
    port: int,
    excluded_ranges: list[tuple[int, int]] | None = None,
) -> tuple[str, list]:
    """Classify why a port is unavailable.

    Returns ``(reason, owners)`` where reason is one of:
    - ``"neko"``            already taken by an existing N.E.K.O service
    - ``"hyperv_excluded"`` inside a Hyper-V / WSL reserved port range
    - ``"other_process"``   listened on by a non-N.E.K.O process
    - ``"unknown"``         cannot bind but the reason is unclear
    owners is the list of process IDs listening on the port.
    """
    health = probe_neko_health(port)
    if health is not None:
        return "neko", get_port_owners(port)
    # 将 excluded_ranges 解析一次，避免重复 netsh 子进程调用
    ranges = excluded_ranges if excluded_ranges is not None else get_hyperv_excluded_ranges()
    if is_port_in_excluded_range(port, ranges):
        return "hyperv_excluded", []
    owners = get_port_owners(port)
    if owners:
        return "other_process", owners
    return "unknown", []


def _validated_existing_backend_instance(
    health_by_key: dict[str, dict],
) -> str | None:
    """Accept attach only for one complete, correctly routed backend instance."""
    expected_services = {
        "MAIN_SERVER_PORT": "main",
        "MEMORY_SERVER_PORT": "memory",
        "TOOL_SERVER_PORT": "agent",
    }
    if set(health_by_key) != set(expected_services):
        return None
    instance_ids: set[str] = set()
    for key, expected_service in expected_services.items():
        health = health_by_key[key]
        instance_id = str(health.get("instance_id") or "")
        if health.get("service") != expected_service or not instance_id:
            return None
        instance_ids.add(instance_id)
    return instance_ids.pop() if len(instance_ids) == 1 else None


def apply_port_strategy() -> bool | str:
    """Prefer the default ports, automatically dodging conflicts when necessary.

    Return value:
        ``True``      port planning done; server startup can proceed.
        ``False``     fatal error; startup must abort.
        ``"attach"`` the default ports are fully owned by an existing N.E.K.O backend.

    Strategy:
    1. Attach only when every default port belongs to the expected service in
       one existing N.E.K.O instance.
    2. Otherwise, move every conflict to fallback ports and start one complete,
       isolated topology.
    """
    global MAIN_SERVER_PORT, MEMORY_SERVER_PORT, TOOL_SERVER_PORT
    global _existing_neko_services, _partial_or_mixed_existing_backend
    _partial_or_mixed_existing_backend = False
    chosen: dict[str, int] = {}
    chosen_internal: dict[str, int] = {}
    fallback_details: list[dict] = []
    internal_fallback_details: list[dict] = []
    existing_health_by_key: dict[str, dict] = {}
    existing_owners_by_key: dict[str, list] = {}
    reserved: set[int] = set()

    # Hyper-V/WSL 保留端口范围只有端口**已经**绑不上时才用得到，而查询它要
    # spawn 一个 `netsh interface ipv4 show excludedportrange` 子进程——本机实测
    # 122 / 49 / 50 ms。原来它无条件跑在所有 bind 探测**之前**，也就是每一次正常
    # 启动都白付一次子进程，而正常启动恰恰是没有端口冲突、结果用不上的那种。
    #
    # 改成用到时再查。仍然只查一次（结果缓存在闭包里），原来那句"避免重复子进程
    # 调用"照旧成立；那行 Detected 日志跟着挪到真的查过之后，免得报一个没查过的
    # 数字。
    _excluded_ranges_cache: list[list[tuple[int, int]]] = []

    def _excluded_ranges() -> list[tuple[int, int]]:
        if not _excluded_ranges_cache:
            ranges = get_hyperv_excluded_ranges()
            _excluded_ranges_cache.append(ranges)
            if ranges:
                print(
                    f"[Launcher] Detected {len(ranges)} Hyper-V/WSL excluded port range(s)",
                    flush=True,
                )
        return _excluded_ranges_cache[0]

    for key in ("MEMORY_SERVER_PORT", "TOOL_SERVER_PORT", "MAIN_SERVER_PORT"):
        preferred = int(DEFAULT_PORTS[key])
        if preferred not in reserved and _is_port_bindable(preferred):
            chosen[key] = preferred
            reserved.add(preferred)
            continue

        # 端口不可绑定，识别具体原因（同时获取 owners 避免重复查询）
        reason, owners = _classify_port_conflict(preferred, _excluded_ranges())

        if reason == "neko":
            # Defer the decision until all three public ports are inspected.
            # Only a complete, same-instance backend is safe to attach to.
            chosen[key] = preferred
            reserved.add(preferred)
            existing_health_by_key[key] = probe_neko_health(preferred) or {}
            existing_owners_by_key[key] = owners
            continue

        # 需要选择回退端口
        fallback = _pick_fallback_port(preferred, reserved)
        if fallback is None:
            report_startup_failure(
                f"Startup failed: no fallback port available for {key} "
                f"(preferred={preferred}, reason={reason}, owners={owners})"
            )
            return False

        chosen[key] = fallback
        reserved.add(fallback)
        fallback_details.append(
            {
                "port_key": key,
                "preferred": preferred,
                "selected": fallback,
                "reason": reason,
                "owners": owners,
            }
        )

    existing_instance = _validated_existing_backend_instance(existing_health_by_key)
    if existing_instance is not None:
        _existing_neko_services = set(DEFAULT_PORTS)
        for key, value in chosen.items():
            os.environ[f"NEKO_{key}"] = str(value)
        _sync_runtime_config_globals(chosen, {})
        for server in SERVERS:
            port_key = MODULE_TO_PORT_KEY.get(server["module"])
            if port_key:
                server["port"] = chosen[port_key]
        emit_frontend_event(
            "port_plan",
            {
                "instance_id": existing_instance,
                "launcher_instance_id": INSTANCE_ID,
                "defaults": DEFAULT_PORTS,
                "selected": chosen,
                "internal_defaults": INTERNAL_DEFAULT_PORTS,
                "internal_selected": {},
                "fallbacks": [],
                "internal_fallbacks": [],
                "fallback_applied": False,
            },
        )
        emit_frontend_event(
            "attach_existing",
            {
                "instance_id": existing_instance,
                "launcher_instance_id": INSTANCE_ID,
                "selected": chosen,
                "existing_instance_id": existing_instance,
                "message": "Validated complete N.E.K.O backend on default ports",
            },
        )
        print(
            "[Launcher] Validated one complete N.E.K.O backend on all default ports; attaching.",
            flush=True,
        )
        return "attach"

    # A partial or mismatched N.E.K.O footprint must not be spliced into a new
    # runtime instance. Move the complete public port set off the defaults and
    # start one topology with a fresh INSTANCE_ID and IPC port plan.
    _partial_or_mixed_existing_backend = bool(existing_health_by_key)
    _existing_neko_services = set()
    for key in ("MEMORY_SERVER_PORT", "TOOL_SERVER_PORT", "MAIN_SERVER_PORT"):
        preferred = int(DEFAULT_PORTS[key])
        if not _partial_or_mixed_existing_backend or chosen[key] != preferred:
            continue
        fallback = _pick_fallback_port(preferred, reserved)
        if fallback is None:
            health = existing_health_by_key.get(key, {})
            report_startup_failure(
                f"Startup failed: no isolated fallback port available for {key} "
                f"(preferred={preferred}, existing_service={health.get('service')})"
            )
            return False
        chosen[key] = fallback
        reserved.add(fallback)
        fallback_details.append(
            {
                "port_key": key,
                "preferred": preferred,
                "selected": fallback,
                "reason": (
                    "existing_neko_conflict"
                    if key in existing_health_by_key
                    else "isolate_from_existing_neko_backend"
                ),
                "owners": existing_owners_by_key.get(key, []),
            }
        )

    MAIN_SERVER_PORT = chosen["MAIN_SERVER_PORT"]
    MEMORY_SERVER_PORT = chosen["MEMORY_SERVER_PORT"]
    TOOL_SERVER_PORT = chosen["TOOL_SERVER_PORT"]

    for key, preferred in INTERNAL_DEFAULT_PORTS.items():
        if preferred not in reserved and _is_port_bindable(preferred):
            chosen_internal[key] = preferred
            reserved.add(preferred)
            continue

        owners = get_port_owners(preferred)
        fallback = _pick_fallback_port(preferred, reserved)
        if fallback is None:
            report_startup_failure(
                f"Startup failed: no fallback port available for {key} (preferred={preferred}, owners={owners})"
            )
            return False

        chosen_internal[key] = fallback
        reserved.add(fallback)
        internal_fallback_details.append(
            {
                "port_key": key,
                "preferred": preferred,
                "selected": fallback,
                "owners": owners,
            }
        )

    for key, value in chosen.items():
        os.environ[f"NEKO_{key}"] = str(value)
    for key, value in chosen_internal.items():
        os.environ[f"NEKO_{key}"] = str(value)

    _sync_runtime_config_globals(chosen, chosen_internal)

    for server in SERVERS:
        if server["module"] == "memory_server":
            server["port"] = MEMORY_SERVER_PORT
        elif server["module"] == "agent_server":
            server["port"] = TOOL_SERVER_PORT
        elif server["module"] == "main_server":
            server["port"] = MAIN_SERVER_PORT

    emit_frontend_event(
        "port_plan",
        {
            "instance_id": INSTANCE_ID,
            "defaults": DEFAULT_PORTS,
            "selected": chosen,
            "internal_defaults": INTERNAL_DEFAULT_PORTS,
            "internal_selected": chosen_internal,
            "fallbacks": fallback_details,
            "internal_fallbacks": internal_fallback_details,
            "fallback_applied": bool(fallback_details or internal_fallback_details),
        },
    )

    if fallback_details or internal_fallback_details:
        print(
            f"[Launcher] Port fallback applied: public={fallback_details}, internal={internal_fallback_details}",
            flush=True,
        )
    else:
        print("[Launcher] Preferred ports available; no fallback needed.", flush=True)
    return True

def show_spinner(stop_event: threading.Event, message: str = "正在启动服务器"):
    """Show a spinner animation"""
    spinner = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
    while not stop_event.is_set():
        sys.stdout.write(f'\r{message}... {next(spinner)} ')
        sys.stdout.flush()
        time.sleep(0.1)
    sys.stdout.write('\r' + ' ' * 60 + '\r')  # 清除动画行
    sys.stdout.write('\n')  # 换行，确保后续输出在新行
    sys.stdout.flush()

def start_server(server: Dict) -> bool:
    """Start a single server"""
    try:
        port = server.get('port')

        if isinstance(port, int) and check_port(port):
            owner_pids = get_port_owners(port)
            owner_suffix = f", owner_pids={owner_pids}" if owner_pids else ""
            report_startup_failure(f"Start failed: {server['name']} port {port} already in use{owner_suffix}")
            return False

        # 根据模块名选择启动函数
        if server['module'] == 'memory_server':
            target_func = run_memory_server
        elif server['module'] == 'agent_server':
            target_func = run_agent_server
        elif server['module'] == 'main_server':
            target_func = run_main_server
        else:
            report_startup_failure(f"Start failed: {server['name']} has unknown module")
            return False

        # 创建进程间同步事件
        server['ready_event'] = Event()
        server['import_event'] = Event()
        server['shutdown_event'] = Event()
        server['shutdown_complete_event'] = Event()

        # 使用 multiprocessing 启动服务器
        # 注意：不能设置 daemon=True，因为 main_server 自己会创建子进程
        server['process'] = Process(
            target=target_func,
            args=(
                server['ready_event'],
                server['import_event'],
                server['shutdown_event'],
                server['shutdown_complete_event'],
            ),
            daemon=False,
        )
        server['process'].start()

        print(f"✓ {server['name']} 已启动 (PID: {server['process'].pid})", flush=True)
        return True
    except Exception as e:
        report_startup_failure(f"Start failed: {server['name']} exception: {e}")
        return False

def wait_for_servers(timeout: int = 60) -> bool | str:
    """Wait for all servers to finish starting"""
    print("\n等待服务器准备就绪...", flush=True)

    # 启动动画线程
    stop_spinner = threading.Event()
    spinner_thread = threading.Thread(target=show_spinner, args=(stop_spinner, "检查服务器状态"))
    spinner_thread.daemon = True
    spinner_thread.start()

    start_time = time.time()
    all_ready = False

    # 第一步：等待所有端口就绪
    while time.time() - start_time < timeout:
        # 若某个子进程提前退出，立即报错而不是等到超时
        for server in SERVERS:
            proc = server.get('process')
            if proc is not None and not proc.is_alive() and not check_port(server['port']):
                if (
                    server.get("module") == "main_server"
                    and _is_pending_storage_restart_request()
                ):
                    _mark_expected_launcher_shutdown()
                    print(
                        "\n[Launcher] Detected intentional main_server shutdown during startup for storage restart",
                        flush=True,
                    )
                    stop_spinner.set()
                    spinner_thread.join()
                    return STARTUP_WAIT_RESULT_STORAGE_RESTART
                report_startup_failure(
                    f"Startup failed: {server['name']} exited early (exitcode={proc.exitcode})"
                )
                stop_spinner.set()
                spinner_thread.join()
                return False

        ready_count = 0
        for server in SERVERS:
            if check_port(server['port']):
                ready_count += 1

        if ready_count == len(SERVERS):
            break

        time.sleep(0.5)

    # 第二步：等待所有服务器的 ready_event（同步初始化完成）
    if ready_count == len(SERVERS):
        for server in SERVERS:
            remaining_time = timeout - (time.time() - start_time)
            if remaining_time > 0:
                if server['ready_event'].wait(timeout=remaining_time):
                    continue
                else:
                    # 超时
                    break
        else:
            # 所有服务器都就绪了
            all_ready = True

    # 停止动画
    stop_spinner.set()
    spinner_thread.join()

    if all_ready:
        print("\n", flush=True)
        print("=" * 60, flush=True)
        print("✓✓✓  所有服务器已准备就绪！  ✓✓✓", flush=True)
        print("=" * 60, flush=True)
        print("\n", flush=True)
        return True
    else:
        print("\n", flush=True)
        print("=" * 60, flush=True)
        print("✗ 服务器启动超时，请检查日志文件", flush=True)
        print("=" * 60, flush=True)
        print("\n", flush=True)
        report_startup_failure("Startup timeout: at least one service did not become ready")
        # 显示未就绪的服务器
        for server in SERVERS:
            if not server['ready_event'].is_set():
                print(f"  - {server['name']} 初始化未完成", flush=True)
            elif not check_port(server['port']):
                print(f"  - {server['name']} 端口 {server['port']} 未就绪", flush=True)
        return False


def cleanup_servers():
    """Clean up all server processes"""
    global _cleanup_done
    with _cleanup_lock:
        if _cleanup_done:
            return
        _cleanup_done = True

    try:
        _teardown_print("\n正在关闭服务器...")
        for server in _iter_servers_for_shutdown():
            proc = server.get('process')
            if not proc:
                continue

            try:
                shutdown_evt = server.get('shutdown_event')
                shutdown_complete_evt = server.get('shutdown_complete_event')
                graceful_timeout = float(server.get('graceful_shutdown_timeout') or 8)

                # 先请求子进程优雅退出
                if proc.is_alive():
                    if shutdown_evt is not None:
                        shutdown_evt.set()
                    if shutdown_complete_evt is not None:
                        try:
                            shutdown_complete_evt.wait(timeout=graceful_timeout)
                        except KeyboardInterrupt:
                            _teardown_print(f"[Launcher] {server['name']} shutdown wait interrupted, continuing cleanup")
                        try:
                            proc.join(timeout=2)
                        except KeyboardInterrupt:
                            _teardown_print(f"[Launcher] {server['name']} join interrupted, escalating shutdown")
                    else:
                        try:
                            proc.join(timeout=graceful_timeout)
                        except KeyboardInterrupt:
                            _teardown_print(f"[Launcher] {server['name']} join interrupted, escalating shutdown")

                # 第二步：仍存活则发送终止信号
                if proc.is_alive():
                    proc.terminate()
                    try:
                        proc.join(timeout=5)
                    except KeyboardInterrupt:
                        _teardown_print(f"[Launcher] {server['name']} terminate wait interrupted, forcing shutdown")

                # 第三步：仍存活则 kill
                if proc.is_alive():
                    proc.kill()
                    try:
                        proc.join(timeout=2)
                    except KeyboardInterrupt:
                        _teardown_print(f"[Launcher] {server['name']} kill wait interrupted, moving on")

                # 第四步：仅在父进程仍存活时兜底强杀整个进程树，避免 PID 复用误杀
                if proc.is_alive():
                    pid = proc.pid
                    if pid:
                        if sys.platform == 'win32':
                            subprocess.run(
                                ["taskkill", "/PID", str(pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                check=False
                            )
                        else:
                            # macOS / Linux 下兜底强杀整个进程树
                            try:
                                import psutil
                                try:
                                    parent = psutil.Process(pid)
                                    for child in parent.children(recursive=True):
                                        child.kill()
                                    parent.kill()
                                except psutil.NoSuchProcess:
                                    pass
                            except ImportError:
                                try:
                                    # 尽力而为的 pkill 兜底
                                    subprocess.run(
                                        ["pkill", "-9", "-P", str(pid)],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        check=False
                                    )
                                except Exception:
                                    pass

                _teardown_print(f"✓ {server['name']} 已关闭")
            except Exception as e:
                _teardown_print(f"✗ {server['name']} 关闭失败: {e}")

        # 不在这里关闭 Job handle。这个 Job 带 KILL_ON_JOB_CLOSE 且 launcher 自己
        # 就是成员，关掉最后一个句柄会连本进程一起终止——cleanup_servers 之后的
        # 一切（存储重启调度、单实例锁释放、退出码）都会变成死代码。句柄在进程
        # 退出时由内核关闭，那时终止整个 Job 正是我们要的语义。
    finally:
        _cleanup_complete.set()


def _safe_getppid_for_owner_check() -> int:
    try:
        return int(os.getppid())
    except Exception:
        return 0


def _handle_termination_signal(signum, _frame):
    """Handle termination signals, doing our best to ensure cleanup logic runs."""
    # 属主之死的清扫正在进行：这一发 TERM 就是我们自己那次组级清扫打回本进程的。
    # 此刻退出会把「TERM → 宽限 → 组级 KILL」停在第一步，正好放走它要收割的那些
    # 孙子进程。_handle_owner_death 必定以 os._exit 收尾，所以在它跑完之前忽略
    # 停止请求是正确语义，而不是把信号吞了。
    # Also yield once the guard has merely *fired*. Between fire() setting that
    # flag and _handle_owner_death setting _owner_death_in_progress there are a
    # few dozen bytecodes, and on Linux the pdeathsig callback runs on this very
    # thread — so a concurrent SIGTERM can land in that gap, take the ordinary
    # path, and raise SystemExit straight out of fire() before the callback runs.
    # The reverse interleaving is worse: SIGTERM arriving just *before*
    # fire() sets its flag ran the whole owner-death teardown twice, with two
    # finisher threads.
    guard = _parent_death_guard
    if _owner_death_in_progress or (guard is not None and guard.fired):
        return

    # 兜底：属主已经死了，但送达的是普通 SIGTERM 而不是 guard 的专用信号
    # （比如属主用组级 TERM 清扫）。属主之死需要组级清扫，而这条普通关闭路径
    # 没有——所以改走 _handle_owner_death，它不会返回。
    #
    # 只在属主就是直连父进程时才用这条判据。交接世代里属主是祖父，我们的父进程
    # 是那个已经按计划退出的上一代 launcher，于是「getppid() 不等于属主」在正常
    # 运行下恒为真——那样任何一次普通 SIGTERM 都会被误读成属主之死，进而触发组级
    # 清扫。guard 自己知道这个区别，问它。
    if (
        guard is not None
        and guard.parent_pid > 1
        and os.name == "posix"
        and guard.owner_is_direct_parent
        and _safe_getppid_for_owner_check() != guard.parent_pid
    ):
        _handle_owner_death("termination_signal_orphaned")
        return

    _mark_expected_launcher_shutdown()
    print(f"\n收到终止信号 ({signum})，正在关闭...", flush=True)
    cleanup_servers()
    raise SystemExit(0)


_parent_death_guard = None
_single_instance_handle = None

#: Set for the duration of _handle_owner_death. The group sweep it performs
#: signals this process too, and on every mechanism except Linux pdeathsig that
#: sweep runs on a daemon thread, where signal.signal() cannot change the
#: disposition. This flag is what keeps the returning signal from cutting the
#: escalation short.
_owner_death_in_progress = False

#: The thread running _finish_owner_death, so main() can wait for it. Making it
#: non-daemon is not enough on its own: merged mode ends at os._exit(), which
#: bypasses interpreter shutdown and therefore joins nothing.
_owner_death_finisher = None

#: Set when cleanup_servers() has run to completion. ``_cleanup_done`` only says
#: it *started*, so an owner death arriving mid-cleanup would otherwise sail past
#: an in-flight ordered shutdown and group-KILL Main during its release sequence.
_cleanup_complete = threading.Event()

#: Bounded grace between the group TERM and the group KILL on owner death.
OWNER_DEATH_GROUP_GRACE_SECONDS = 0.5


def _teardown_print(message: str) -> None:
    """print() that cannot abort the teardown it is narrating.

    Once the owner is gone the read end of our stdout pipe went with it, so every
    print on a shutdown path is a live BrokenPipeError risk — and raising there
    skips the rest of the teardown, i.e. loses the guarantee in order to log
    about it. Used by every function reachable from owner death, not only the
    owner-death functions themselves: the first unguarded print in a callee ends
    the sequence just as effectively.
    """
    try:
        print(message, flush=True)
    except Exception:
        # Nowhere left to report this: stdout is the thing that just failed.
        pass


def _handle_owner_death(mechanism: str) -> None:
    """Tear the whole topology down when the process that owns us disappears.

    This is the guarantee that lets an owner stop building external anchors: a
    runtime that cannot outlive its owner needs no supervising shell holding its
    process group, no externally owned Job holder, and no persistent lease to
    replay after a crash, because there is never anything left to recover.
    """
    global _owner_death_in_progress, _owner_death_finisher

    _owner_death_in_progress = True
    _mark_expected_launcher_shutdown()
    _teardown_print(f"[Launcher] Owner process is gone ({mechanism}); shutting down runtime")
    try:
        emit_frontend_event("owner_exit", {"mechanism": mechanism})
    except Exception:
        # 通知前端只是尽力而为：属主已死，stdout 管道很可能已断。任何 IPC
        # 失败都不能挡住下面的拆除流程与最终的 os._exit(0)。
        pass

    # 合并模式（打包默认）下三个 server 就在本进程里，SERVERS 里没有任何子进程
    # 句柄，cleanup_servers() 无事可做。必须先驱动有序关停，让 Main 在 Memory
    # 还活着的时候跑完 release/cloudsave，否则下面几行会直接 os._exit 把它连根
    # 切掉。收尾放到独立线程：Linux 的 pdeathsig 回调跑在主线程信号处理器里，
    # 而 asyncio 事件循环也在主线程——就地等待会让我们等的那个关停永远推进不了。
    requester = _merged_shutdown_request
    if requester is not None:
        try:
            requester(reason=f"owner_death:{mechanism}")
        except Exception as exc:
            _teardown_print(f"[Launcher] Warning: merged shutdown request failed: {exc}")
        # Deliberately NOT a daemon thread. run_merged_servers() returns as soon
        # as the ordered shutdown completes and main() then walks to its own
        # exit; a daemon would be cut off mid-teardown, losing the group sweep
        # and the very grandchildren it exists to reap. Non-daemon makes the
        # interpreter wait for it — and it ends in os._exit(0) regardless, so
        # the wait is bounded by the merged-shutdown budget above.
    # Always on its own thread, in every mode. On Linux this function runs as a
    # signal handler on the main thread, and the stack it interrupted may be the
    # cleanup it is about to wait for — waiting in place deadlocks against
    # ourselves, permanently if the signal landed inside _cleanup_lock. Handing
    # off lets the interrupted cleanup resume and finish the ordered shutdown,
    # which is what the teardown wanted from it in the first place.
    _owner_death_finisher = threading.Thread(
        target=_finish_owner_death,
        args=(mechanism, requester is not None),
        name="neko-owner-death-finish",
        daemon=False,
    )
    _owner_death_finisher.start()


def _finish_owner_death(mechanism: str, wait_for_merged: bool) -> None:
    """Second half of owner-death teardown: cleanup, release, group sweep, exit."""
    if wait_for_merged:
        # 有界等待：超时后照常收尾。_begin_merged_shutdown 自带的看门狗也会在
        # 同样的预算后 os._exit(1)，两道保险都不会让这里无限期挂住。
        if not _merged_shutdown_complete.wait(OWNER_DEATH_MERGED_SHUTDOWN_BUDGET):
            _teardown_print(
                "[Launcher] Warning: merged shutdown did not finish within "
                f"{OWNER_DEATH_MERGED_SHUTDOWN_BUDGET}s; continuing owner-death teardown"
            )

    try:
        cleanup_servers()
        # _cleanup_done only means "started". If another thread got there first,
        # returning now would group-KILL Main 0.5s later, mid release/cloudsave.
        # Bounded, and the event is set from a finally, so the sweep and the
        # os._exit(0) below stay reachable even if cleanup raises.
        # If our own call did the work its finally already set this, so the wait
        # below is skipped. Only an in-flight cleanup owned by another thread
        # leaves it clear, and that is exactly the case worth waiting out.
        if not _cleanup_complete.is_set() and not _cleanup_complete.wait(
            OWNER_DEATH_MERGED_SHUTDOWN_BUDGET
        ):
            _teardown_print(
                "[Launcher] Warning: in-flight cleanup did not finish within "
                f"{OWNER_DEATH_MERGED_SHUTDOWN_BUDGET}s; continuing owner-death teardown"
            )
    except Exception as exc:
        _teardown_print(f"[Launcher] Warning: cleanup after owner death failed: {exc}")

    # Deliberately no explicit release here. The lock is documented as held for
    # the owning process's whole lifetime, and everything below — the group TERM,
    # the grace period, the final KILL — happens while this process is still
    # alive. Releasing first opened a window where the lock was free but this
    # generation was not yet gone. The kernel drops it when we die, which is
    # exactly the semantics this module relies on everywhere else. The record
    # left behind is harmless: owner_status() proves liveness from the lock and
    # ignores any record whose lock it can take.

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        # 属主关掉管道后 flush 抛 BrokenPipeError 是常态；退出路径不能因此中断。
        pass

    # 最后一步：收掉 cleanup 未记录的孙子进程（插件、MCP、Chromium…）。
    #
    # 要如实说明它的适用范围：这一步只在 launcher **自己是进程组组长**时才发生。
    # 而最常见的拓扑恰恰不是——属主直接 spawn 我们、我们继承属主的组，此时
    # `_own_process_group_id()` 恒为 None，下面整段是死代码。所以它不是"POSIX 上
    # 对应 Windows Job 的收容锚点"（早先的注释这么写，是过度声称）：Windows 的 Job
    # 无条件生效，这一段有条件。终端直接跑、或属主以 detached 方式拉起时才有它。
    #
    # 非组长拓扑下真正在收容的是：cleanup_servers() 对仍存活的已记录 server 做
    # psutil 整树强杀，以及各 server 自己的优雅停机路径。裸露的只剩一类——被一个
    # **已经死掉**的 server 遗弃的后代：它们当场被内核改挂到 init，既不再是该
    # server 的后代，也不在我们能清扫的组里。那是 main 上的既有缺口（本 PR 只改了
    # 那段的缩进），要补需要按 create_time 记名跟踪后代，属于独立改动。
    _escalate_own_process_group()
    os._exit(0)


def _escalate_own_process_group() -> None:
    """TERM, a bounded grace, then a group KILL — where we lead the group.

    Only reached when this process is its own process-group leader. In the
    ordinary shape the owner spawns us into *its* group, so this does nothing:
    see the residual recorded on cleanup_servers().
    """
    if _own_process_group_id() is None:
        return
    if _sweep_own_process_group(signal.SIGTERM):
        time.sleep(OWNER_DEATH_GROUP_GRACE_SECONDS)
    try:
        os.killpg(os.getpid(), signal.SIGKILL)
    except OSError:
        # ESRCH/EPERM 意味着没有可收的组成员，交给调用方继续退出。
        pass


def install_parent_death_guard():
    """Arm foreground residency and report honestly what could be armed."""
    global _parent_death_guard

    guard = parent_guard.install(_handle_owner_death)
    _parent_death_guard = guard
    if guard.mechanisms:
        print(
            f"[Launcher] Foreground residency armed via {', '.join(guard.mechanisms)} "
            f"(owner pid {guard.parent_pid})",
            flush=True,
        )
    else:
        print(
            "[Launcher] Warning: no parent-death mechanism available; "
            "this runtime cannot self-clean if its owner dies",
            flush=True,
        )
    emit_frontend_event(
        "foreground_residency",
        {
            "owner_pid": guard.parent_pid,
            "mechanisms": list(guard.mechanisms),
            "guaranteed": bool(guard.mechanisms),
        },
    )
    return guard


def _single_instance_extra_fields() -> dict:
    guard = _parent_death_guard
    return {
        "owner_pid": guard.parent_pid if guard is not None else 0,
        "foreground_residency": list(guard.mechanisms) if guard is not None else [],
        "launch_mode": os.environ.get("NEKO_LAUNCH_MODE", "") or "",
    }


def _acquire_single_instance_ownership() -> bool:
    """Prove uniqueness with an OS lock and publish the authoritative record.

    Returns ``True`` when this process owns the runtime. Returns ``False`` when
    a live instance already exists — in which case the loser has already told
    the frontend *which* instance to attach to, instead of leaving it to rediscover
    the answer by probing ports.
    """
    global _single_instance_handle

    handoff = os.environ.get(RESTART_HANDOFF_ENV, "").strip().lower() in ("1", "true", "yes")
    try:
        handle = single_instance.acquire_single_instance(
            instance_id=INSTANCE_ID,
            launch_id=LAUNCH_ID,
            extra=_single_instance_extra_fields(),
            retries=RESTART_HANDOFF_LOCK_RETRIES if handoff else 0,
            retry_interval=RESTART_HANDOFF_LOCK_RETRY_INTERVAL,
        )
    except OSError as exc:
        # 拿不到锁文件不等于"已有实例在跑"（只读 HOME、磁盘满、权限）。这是
        # unknown，不能塌缩成"有人在跑"而拒绝启动，也不能塌缩成"没人在跑"而
        # 谎称唯一性成立——如实上报，照常启动。
        print(
            f"[Launcher] Warning: single-instance lock unavailable ({exc}); "
            "starting without a uniqueness proof",
            flush=True,
        )
        emit_frontend_event(
            "single_instance",
            {"role": "unverified", "reason": str(exc)},
        )
        return True

    if handle is None:
        # The winner takes the lock a moment before it publishes, so an immediate
        # read can legitimately come back empty. Give it a bounded moment rather
        # than telling the frontend "somebody else owns this" with no idea who.
        owner = {}
        deadline = time.monotonic() + DUPLICATE_RECORD_READ_BUDGET
        while True:
            status, record = single_instance.owner_status()
            if status == single_instance.OWNER_OWNED and record:
                owner = record
                break
            if status != single_instance.OWNER_OWNED or time.monotonic() >= deadline:
                # FREE is handled below; UNKNOWN cannot be improved by waiting;
                # and a winner that never publishes must not hang us forever.
                break
            time.sleep(0.02)
        if not owner and single_instance.owner_status()[0] == single_instance.OWNER_FREE:
            # The holder let go between our failed attempt and this read — it was
            # on its way out. There is no instance to attach to, so exiting as a
            # duplicate would leave the user with nothing running. Try once more;
            # arbitration is still the kernel lock alone, and losing again falls
            # through to the duplicate branch exactly as before.
            try:
                handle = single_instance.acquire_single_instance(
                    instance_id=INSTANCE_ID,
                    launch_id=LAUNCH_ID,
                    extra=_single_instance_extra_fields(),
                )
            except OSError:
                # Deliberately unlike the first acquisition, which fails open on
                # OSError. By here we already know somebody won the lock at least
                # once, so refusing to start is the safe direction rather than an
                # unclearable block.
                handle = None
            if handle is None:
                # A third launcher took it between our probe and this attempt, so
                # the empty record we are holding belongs to whoever just left.
                # Read once more; still bounded, still no arbitration by probing.
                owner = single_instance.read_owner_record() or {}

    if handle is None:
        msg = "Another N.E.K.O runtime already holds the single-instance lock"
        print(
            f"[Launcher] {msg} (pid={owner.get('pid')}, instance_id={owner.get('instance_id')})",
            flush=True,
        )
        emit_frontend_event(
            "single_instance",
            {"role": "duplicate", "owner": owner, "record_path": str(single_instance.record_path())},
        )
        # 保留既有事件名，老版本前端仍能识别"已有实例在启动"这一场景。
        emit_frontend_event("startup_in_progress", {"message": msg, "owner": owner})
        return False

    # Upgrade window. An older build resolved its lock path from XDG_RUNTIME_DIR
    # or the shared temp dir; this one resolves a stable per-user path, so a
    # runtime still holding one of those is invisible to the lock we just took.
    # Probing them can only make us refuse to start — it can never make us
    # wrongly claim uniqueness — and UNKNOWN deliberately does not block.
    legacy_status, legacy_owner = single_instance.legacy_owner_status()
    if legacy_status == single_instance.OWNER_OWNED:
        msg = "An older N.E.K.O runtime is still running from a previous state directory"
        print(f"[Launcher] {msg}", flush=True)
        handle.release()
        emit_frontend_event(
            "single_instance",
            {"role": "duplicate", "owner": legacy_owner or {}, "legacy": True},
        )
        emit_frontend_event("startup_in_progress", {"message": msg, "owner": legacy_owner or {}})
        return False

    _single_instance_handle = handle
    os.environ.pop(RESTART_HANDOFF_ENV, None)
    emit_frontend_event(
        "single_instance",
        {
            "role": "owner",
            "record": handle.record(),
            "record_path": str(handle.record_file),
            "lock_path": str(handle.lock_file),
        },
    )
    return True


def _publish_ready_runtime_record(launch_mode: str) -> None:
    """Freeze the final, authoritative runtime record once services are up.

    From this point an owner needs exactly one fact — this record — to know the
    instance id, the pid, and every negotiated port. It never has to reconstruct
    them by probing the three default ports and hoping the answers agree.
    """
    publish_single_instance_state(
        state=single_instance.STATE_READY,
        launch_mode=launch_mode,
        ports={
            "MAIN_SERVER_PORT": MAIN_SERVER_PORT,
            "MEMORY_SERVER_PORT": MEMORY_SERVER_PORT,
            "TOOL_SERVER_PORT": TOOL_SERVER_PORT,
        },
    )


def publish_single_instance_state(**fields) -> None:
    """Update the authoritative runtime record, if this process owns it."""
    handle = _single_instance_handle
    if handle is None or not handle.held:
        return
    try:
        handle.publish(**fields)
    except Exception as exc:
        print(f"[Launcher] Warning: failed to publish runtime record: {exc}", flush=True)


def release_single_instance_ownership() -> None:
    global _single_instance_handle

    handle = _single_instance_handle
    _single_instance_handle = None
    if handle is not None:
        try:
            handle.release()
        except Exception as exc:
            print(f"[Launcher] Warning: failed to release single-instance lock: {exc}", flush=True)


def register_shutdown_hooks():
    """Register shutdown hooks to cover more exit paths."""
    atexit.register(cleanup_servers)
    try:
        signal.signal(signal.SIGTERM, _handle_termination_signal)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "Failed to register SIGTERM shutdown hook: %s",
            exc,
        )
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _handle_termination_signal)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Failed to register SIGBREAK shutdown hook: %s",
                exc,
            )

def _ensure_playwright_browsers():
    """Auto-install Playwright Chromium if missing (needed by browser-use).

    Uses playwright's bundled driver binary directly, so it works inside
    a Nuitka standalone build where ``python -m playwright`` is unavailable.
    The ``install chromium`` command is idempotent – if the browser already
    exists it returns almost instantly.

    When running frozen (Nuitka/PyInstaller), PLAYWRIGHT_BROWSERS_PATH is set
    to the bundled ``playwright_browsers`` dir so that build-time cached
    Chromium is used and no on-site download is needed.
    """
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
    except ImportError:
        return

    try:
        if IS_FROZEN:
            if hasattr(sys, "_MEIPASS"):
                _bundle = sys._MEIPASS
            else:
                _bundle = os.path.dirname(os.path.abspath(__file__))
            _bundled_browsers = os.path.join(_bundle, "playwright_browsers")
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _bundled_browsers

            if os.path.isdir(_bundled_browsers) and os.listdir(_bundled_browsers):
                print("[Launcher] ✓ Playwright Chromium ready (bundled)", flush=True)
                emit_frontend_event("playwright_check", {"status": "ready"})
                return

        driver = str(compute_driver_executable())
        env = get_driver_env()
        print("[Launcher] Checking Playwright Chromium browser...", flush=True)
        emit_frontend_event("playwright_check", {"status": "checking"})

        result = subprocess.run(
            [driver, "install", "chromium"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

        if result.returncode == 0:
            print("[Launcher] ✓ Playwright Chromium ready", flush=True)
            emit_frontend_event("playwright_check", {"status": "ready"})
        else:
            msg = (result.stderr or "").strip()[:300]
            logging.getLogger(__name__).info("[Launcher] Playwright install warning: %s", msg)
            emit_frontend_event("playwright_check", {"status": "warning", "message": msg})
    except subprocess.TimeoutExpired:
        logging.getLogger(__name__).info("[Launcher] Playwright browser install timed out (300s)")
        emit_frontend_event("playwright_check", {"status": "timeout"})
    except Exception as e:
        logging.getLogger(__name__).info("[Launcher] Playwright browser check skipped: %s", e)
        emit_frontend_event("playwright_check", {"status": "skipped", "message": str(e)})


def _should_use_merged_mode() -> bool:
    """Choose merged vs multi-process mode from env override + runtime shape."""
    merged_env = os.environ.get("NEKO_MERGED", "").strip().lower()
    if merged_env in ("1", "true", "yes"):
        return True
    if merged_env in ("0", "false", "no"):
        return False
    return IS_FROZEN


def _select_launcher_mode() -> tuple[str, str]:
    """Select topology while keeping partial existing-service reuse safe."""
    if not _should_use_merged_mode():
        return "multi", "configured_multi"
    if _partial_or_mixed_existing_backend or _existing_neko_services:
        return "multi", "partial_existing_services"
    return "merged", "configured_merged"


def _prepare_cloudsave_runtime_for_launch() -> dict:
    """Bootstrap local cloudsave state and apply any staged snapshot before services start."""
    print("[Launcher] 初始化本地 cloudsave 基础设施...", flush=True)
    reset_config_manager_cache()
    config_manager = get_config_manager(APP_NAME, migrate=False)

    if not config_manager.ensure_local_state_directory():
        diagnostic = getattr(config_manager, "_last_local_state_directory_error", None)
        if diagnostic is not None:
            raise diagnostic
        raise OSError("failed to ensure local state directory")

    with cloud_apply_fence(
        config_manager,
        mode=ROOT_MODE_BOOTSTRAP_IMPORTING,
        reason="launcher_phase0_bootstrap",
    ):
        bootstrap_result = bootstrap_local_cloudsave_environment(config_manager)
        import_result = get_cloudsave_manager(config_manager).import_if_needed(
            reason="launcher_phase0_prelaunch_import",
            fence_already_active=True,
        )

    # 同上：判定和写不能被别的线程插进来，整段进锁。
    with root_state_transaction():
        load_root_state = getattr(config_manager, "load_root_state", None)
        current_root_state = load_root_state() if callable(load_root_state) else {"mode": ROOT_MODE_NORMAL}
        if should_write_root_mode_normal_after_startup(current_root_state):
            root_state = set_root_mode(
                config_manager,
                ROOT_MODE_NORMAL,
                current_root=str(config_manager.app_docs_dir),
                last_known_good_root=str(config_manager.app_docs_dir),
            )
        else:
            root_state = current_root_state
    root_mode = str(root_state.get("mode") or "")
    root_state_event_payload = {
        "mode": root_mode,
        "is_normal": root_mode == ROOT_MODE_NORMAL,
        "is_readonly": root_mode == ROOT_MODE_MAINTENANCE_READONLY,
    }
    import_payload_source = import_result if isinstance(import_result, dict) else {}
    sanitized_import_result = {
        "success": import_payload_source.get("success"),
        "action": str(import_payload_source.get("action") or ""),
        "requested_reason": str(import_payload_source.get("requested_reason") or ""),
    }
    event_payload = {
        "root_state": root_state_event_payload,
        "manifest_name": Path(config_manager.cloudsave_manifest_path).name,
        "manifest_exists": bool(Path(config_manager.cloudsave_manifest_path).exists()),
        "import_result": sanitized_import_result,
    }
    emit_frontend_event("cloudsave_bootstrap_ready", event_payload)
    return {
        "bootstrap_result": bootstrap_result,
        "import_result": import_result,
        "root_state": root_state,
        "event_payload": event_payload,
    }


def _is_local_state_directory_error(exc) -> bool:
    if bool(getattr(exc, "local_state_directory_error", False)):
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_local_state_directory_error(cause)
    context = getattr(exc, "__context__", None)
    if context is not None and context is not exc:
        return _is_local_state_directory_error(context)
    return False


def main():
    """Main entry point"""
    # 支持 multiprocessing 在 Windows 上的打包
    freeze_support()

    # ── 发送 startup_begin，便于前端绑定本次启动会话 ──
    emit_frontend_event("startup_begin", {"instance_id": INSTANCE_ID})
    os.environ["NEKO_LAUNCHER_PID"] = str(os.getpid())

    # ── 前台常驻：属主一死，整套拓扑自清理 ──────────────
    install_parent_death_guard()

    # ── 单实例自证：进程生命周期文件锁 + 权威运行时记录 ──
    if not _acquire_single_instance_ownership():
        return 0  # 非错误场景：前端应附加到已有实例

    restart_scheduled = False
    allow_storage_restart = False
    exit_code = 0
    try:
        port_result = apply_port_strategy()
        if port_result == "attach":
            # 已有 N.E.K.O 后端在运行，无需再次拉起。这里**不**写运行时记录：
            # 记录的寿命刻意绑定在锁的寿命上，而我们马上就要在 finally 里释放锁
            # 并删掉记录——写了也是死写入。属主要认的那个既有后端，
            # apply_port_strategy() 已经通过 port_plan / attach_existing 两条
            # NEKO_EVENT 把 instance_id 与端口推过去了，那才是它消费的通道。
            return 0
        if not port_result:
            return 1

        publish_single_instance_state(
            ports={
                "MAIN_SERVER_PORT": MAIN_SERVER_PORT,
                "MEMORY_SERVER_PORT": MEMORY_SERVER_PORT,
                "TOOL_SERVER_PORT": TOOL_SERVER_PORT,
            },
            internal_ports={
                key: int(os.environ.get(f"NEKO_{key}", value))
                for key, value in INTERNAL_DEFAULT_PORTS.items()
            },
        )

        register_shutdown_hooks()

        # 创建 Job Object，确保主进程被 kill 时子进程也会被终止
        setup_job_object()

        _resolve_storage_layout_for_launch()

        try:
            _prepare_cloudsave_runtime_for_launch()
        except Exception as e:
            if not _is_local_state_directory_error(e):
                try:
                    _config_manager = get_config_manager(APP_NAME)
                    set_root_mode(
                        _config_manager,
                        ROOT_MODE_MAINTENANCE_READONLY,
                        last_migration_result=f"launcher_phase0_bootstrap_failed:{e}",
                    )
                except Exception:
                    pass
                report_startup_failure(f"Startup failed: cloudsave bootstrap error: {e}")
                return 1
            os.environ[CLOUDSAVE_DISABLED_ENV] = CLOUDSAVE_DISABLED_LOCAL_STATE_UNAVAILABLE
            print(
                "[Launcher] Cloudsave disabled for this session because local state is unavailable: "
                f"{e}",
                flush=True,
            )

        # 自动安装 Playwright Chromium（browser-use 依赖）
        _ensure_playwright_browsers()

        print("=" * 60, flush=True)
        print("N.E.K.O. 服务器启动器", flush=True)
        print("=" * 60, flush=True)

        # ── 合并 / 多进程模式选择 ──
        # 打包环境默认合并（省内存），开发环境默认分离（方便调试）。
        # 可通过环境变量 NEKO_MERGED=1/0 强制覆盖。
        selected_mode, mode_reason = _select_launcher_mode()
        if mode_reason == "partial_existing_services":
            print(
                "[Launcher] Partial existing-service reuse requires multi-process mode; "
                "merged mode was not started.",
                flush=True,
            )
            emit_frontend_event(
                "topology_fallback",
                {
                    "selected_mode": "multi",
                    "reason": mode_reason,
                    "reused_port_keys": sorted(_existing_neko_services),
                },
            )
        if selected_mode == "merged":
            os.environ["NEKO_LAUNCH_MODE"] = "merged"
            allow_storage_restart = True
            print("\n[Launcher] 合并进程模式\n", flush=True)
            exit_code = int(run_merged_servers() or 0)
            return exit_code

        os.environ["NEKO_LAUNCH_MODE"] = "multi"

        # 1. 分步启动服务器（错开 import 阶段以降低内存峰值）
        #    Windows spawn 模式下每个子进程独立加载所有依赖，
        #    同时 import 会导致 3 个进程同时分配大量临时对象，
        #    在 <=4GB 内存的机器上容易 OOM。
        #    只需等 import 完成（内存稳定）即可放行下一个，
        #    后续 uvicorn 初始化很轻量，可并行。
        print("\n正在启动服务器...\n", flush=True)
        all_started = True
        import_timeout = 90  # 单个服务 import 阶段超时秒数
        for i, server in enumerate(SERVERS):
            if not start_server(server):
                all_started = False
                break
            if server.get("module") == "main_server":
                allow_storage_restart = True

            evt = server.get('import_event')
            is_last = (i == len(SERVERS) - 1)
            if evt and not is_last:
                print(f"  等待 {server['name']} 模块加载...", flush=True)
                proc = server.get('process')
                poll_interval = 2  # seconds
                remaining = import_timeout
                import_ok = False
                while remaining > 0:
                    if evt.wait(timeout=min(poll_interval, remaining)):
                        import_ok = True
                        break
                    remaining -= poll_interval
                    if proc and not proc.is_alive():
                        report_startup_failure(
                            f"Startup failed: {server['name']} exited early "
                            f"(exitcode={proc.exitcode})"
                        )
                        break
                if not import_ok:
                    if not (proc and not proc.is_alive()):
                        report_startup_failure(
                            f"Startup timeout: {server['name']} import not complete "
                            f"within {import_timeout}s"
                        )
                    all_started = False
                    break
                print(f"  ✓ {server['name']} 模块加载完成", flush=True)

        if not all_started:
            print("\n启动失败，正在清理...", flush=True)
            report_startup_failure("Startup aborted: at least one service failed to start", show_dialog=False)
            cleanup_servers()
            return 1

        # 2. 等待最后一个服务器也准备就绪
        wait_result = wait_for_servers()
        if wait_result is not True:
            if wait_result == STARTUP_WAIT_RESULT_STORAGE_RESTART:
                print("\n检测到启动期间触发的存储重启，正在清理当前实例...", flush=True)
                cleanup_servers()
                return 0
            print("\n启动失败，正在清理...", flush=True)
            report_startup_failure("Startup aborted: services did not become ready before timeout", show_dialog=False)
            cleanup_servers()
            return 1

        # 3. 服务器已启动，通知前端
        try:
            _config_manager = get_config_manager(APP_NAME)
            _persist_post_startup_root_state(_config_manager)
        except Exception as e:
            print(f"[Launcher] Warning: failed to persist root_state boot success: {e}", flush=True)

        _publish_ready_runtime_record("multi")
        emit_frontend_event("startup_ready", {
            "instance_id": INSTANCE_ID,
            "selected": {
                "MAIN_SERVER_PORT": MAIN_SERVER_PORT,
                "MEMORY_SERVER_PORT": MEMORY_SERVER_PORT,
                "TOOL_SERVER_PORT": TOOL_SERVER_PORT,
            },
        })
        allow_storage_restart = True

        print("", flush=True)
        print("=" * 60, flush=True)
        print("  🎉 所有服务器已启动完成！", flush=True)
        print("\n  现在你可以：", flush=True)
        print("  1. 启动 lanlan_frd.exe 使用系统", flush=True)
        print(f"  2. 在浏览器访问 http://localhost:{MAIN_SERVER_PORT}", flush=True)
        print("\n  按 Ctrl+C 关闭所有服务器", flush=True)
        print("=" * 60, flush=True)
        print("", flush=True)

        # 持续运行，监控服务器状态
        # agent_server 崩溃不应牵连 main/memory，仅记录日志。
        # 只有 main_server 或 memory_server 死亡才触发全局关闭。
        _CRITICAL_MODULES = {"memory_server", "main_server"}
        _reported_exits: set[str] = set()
        while True:
            time.sleep(5)
            started = [s for s in SERVERS if s.get('process') is not None]
            any_critical_dead = False
            for s in started:
                if not s['process'].is_alive() and s['name'] not in _reported_exits:
                    _reported_exits.add(s['name'])
                    module = s.get('module', '')
                    if module in _CRITICAL_MODULES:
                        print(f"\n检测到关键服务异常退出: {s['name']}！", flush=True)
                        any_critical_dead = True
                    else:
                        print(f"\n[Launcher] {s['name']} 已退出 (exitcode={s['process'].exitcode})，不影响核心服务", flush=True)
            if any_critical_dead:
                break
            # 对复用已有实例的服务进行健康探测
            reused = [s for s in SERVERS if s.get('process') is None and s.get('port')]
            for s in reused:
                if probe_neko_health(s['port']) is None:
                    print(f"\n复用的 {s['name']}(port {s['port']}) 已不可达！", flush=True)
                    break
            else:
                continue
            break

    except KeyboardInterrupt:
        _mark_expected_launcher_shutdown()
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        print("\n\n收到中断信号，准备优雅关闭子进程...", flush=True)

    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
        if exit_code != 0:
            print(f"\nLauncher exited with status {exit_code}", flush=True)
            report_startup_failure(
                f"Launcher/SystemExit propagated with status {exit_code}"
            )

    except Exception as e:
        exit_code = 1
        print(f"\n发生错误: {e}", flush=True)
        report_startup_failure(f"Launcher unhandled exception: {e}")
    finally:
        print("\n正在关闭所有进程...", flush=True)

        # 尝试优雅关闭
        cleanup_servers()

        # 等待一段时间，确认进程是否真的无法终止
        print("\n等待进程清理完成...", flush=True)

        # 检查是否还有存活的进程
        has_alive = any(
            server.get('process') and server['process'].is_alive()
            for server in SERVERS
        )

        if has_alive:
            print("\n检测到进程未能正常退出，尝试强制终止...", flush=True)

            try:
                if hasattr(os, 'killpg'):
                    # POSIX: 逐个终止子进程，避免向自身进程组发送 SIGKILL
                    for server in SERVERS:
                        proc = server.get('process')
                        if not proc or not proc.is_alive():
                            continue
                        pid = getattr(proc, 'pid', None)
                        if not pid:
                            continue
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    time.sleep(1)

                    for server in SERVERS:
                        proc = server.get('process')
                        if not proc or not proc.is_alive():
                            continue
                        pid = getattr(proc, 'pid', None)
                        if not pid:
                            continue
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    time.sleep(0.5)
                else:
                    # Windows: 使用 taskkill 强制杀死进程树
                    import subprocess
                    for server in SERVERS:
                        proc = server.get('process')
                        if not proc or not proc.is_alive():
                            continue
                        pid = getattr(proc, 'pid', None)
                        if not pid:
                            continue
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(0.5)
            except Exception as e:
                # 强制终止失败，忽略错误（进程可能已经退出）
                print(f"强制终止进程组时出错（可能进程已退出）: {e}", flush=True)

            # 强制终止后重新检查是否还有存活的进程
            has_alive = any(
                server.get('process') and server['process'].is_alive()
                for server in SERVERS
            )

        print("\n清理完成", flush=True)
        if allow_storage_restart:
            try:
                restart_scheduled = _maybe_schedule_storage_restart()
            except Exception as e:
                print(f"[Launcher] Warning: failed to schedule storage migration restart: {e}", flush=True)
                restart_scheduled = False

        # 交接路径已在 _maybe_schedule_storage_restart 里先释放过锁并把句柄置空；
        # 这里再判一次，是为了覆盖调度本身抛异常、锁仍在手上的分支。
        # Not while an owner-death teardown is running: its thread is still
        # sweeping the process group, and releasing here would reopen the same
        # window the teardown path just closed.
        if not restart_scheduled and not _owner_death_in_progress:
            release_single_instance_ownership()
        # 如果还有残留进程，使用非零退出码
        if has_alive:
            sys.exit(1)

        print("\n所有服务器已关闭", flush=True)
        print("再见！\n", flush=True)
        if os.environ.get("NEKO_LAUNCH_MODE", "").strip().lower() == "merged":
            # os._exit bypasses interpreter shutdown, so a non-daemon finisher
            # thread is not joined by it. Without this wait the main thread gets
            # here in milliseconds while the finisher is still inside its
            # TERM -> grace -> group KILL escalation, and the KILL never lands.
            #
            # Bounded on purpose: an unbounded join would let a stuck finisher
            # keep the runtime alive past its owner, which is the guarantee this
            # branch exists to provide. Worst case we exit a few seconds later.
            finisher = _owner_death_finisher
            if finisher is not None:
                finisher.join(OWNER_DEATH_GROUP_GRACE_SECONDS + 5.0)
            os._exit(exit_code)
    return exit_code


def start_launcher() -> int:
    """Launcher entrypoint with explicit runtime bootstrap."""
    _bootstrap_launcher_runtime(bundle_dir)
    return main()
