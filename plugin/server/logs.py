"""
插件日志服务

提供插件日志和服务器日志的读取和查询功能。
支持 WebSocket 实时推送日志更新。

⚠ 历史教训：本模块过去用 `BUILTIN_PLUGIN_CONFIG_ROOT.parent.parent / "log"`
推测插件日志路径，AppImage 打包后 plugin/ 在只读 squashfs 下直接崩。
现在统一通过 utils.logger_config.RobustLoggerConfig(service_name=...)
读取真实落盘目录（Documents/N.E.K.O/logs/），不再做相对路径推算。
谁再加 loguru / cwd-based fallback —— 按维护者口径就把谁杀了。
"""
import re
import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from plugin.logging_config import get_logger

from plugin.settings import BUILTIN_PLUGIN_CONFIG_ROOT


logger = get_logger("server.logs")

_RUNTIME_ERRORS = (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError)

# 服务器日志的特殊 ID
SERVER_LOG_ID = "_server"


def _parse_log_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    candidates = (stripped, stripped.replace("T", " "))
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(stripped, fmt)
        except ValueError:
            continue
    return None


def _validate_plugin_id(plugin_id: str) -> None:
    """
    验证 plugin_id 是否安全（防止路径遍历攻击）
    
    Args:
        plugin_id: 插件ID
    
    Raises:
        HTTPException: 如果 plugin_id 不安全
    """
    # 服务器日志ID是特殊的，允许通过
    if plugin_id == SERVER_LOG_ID:
        return
    
    # 验证 plugin_id 只包含安全字符（防止路径遍历攻击）
    if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid plugin_id: '{plugin_id}'. Only alphanumeric characters, underscores, and hyphens are allowed."
        )
    
    # 额外检查：确保不包含路径遍历符号
    if '..' in plugin_id or '/' in plugin_id or '\\' in plugin_id:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid plugin_id: '{plugin_id}'. Path traversal characters are not allowed."
        )


def _resolve_robust_log_dir(service_name: str, log_subdir: str | None = None) -> Path:
    """通过本体 RobustLoggerConfig 拿落盘目录。

    AppImage 打包后 plugin 包在只读 squashfs 下，禁止用 cwd / __file__ 推
    日志路径。RobustLoggerConfig 会按 5 级回退（Documents → AppData → home
    → temp）选择可写目录。

    ``log_subdir`` 与 writer 侧保持一致：plugin 子进程写入 ``logs/plugin/``，
    server 主进程写入 ``logs/``。
    """
    try:
        from utils.logger_config import RobustLoggerConfig
    except (ImportError, ModuleNotFoundError):
        import importlib.util

        project_root = BUILTIN_PLUGIN_CONFIG_ROOT.parent.parent
        logger_config_path = project_root / "utils" / "logger_config.py"
        spec = importlib.util.spec_from_file_location("utils.logger_config", logger_config_path)
        if spec is None or spec.loader is None:
            raise
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        RobustLoggerConfig = getattr(mod, "RobustLoggerConfig")
    config = RobustLoggerConfig(service_name=service_name, log_subdir=log_subdir)
    log_dir = Path(config.get_log_directory_path())
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_plugin_log_dir(plugin_id: str) -> Path:
    """
    获取插件的日志目录。

    落盘分层（与 writer 侧 ``plugin/core/host.py`` / ``plugin/logging_config.py``
    保持对偶）：

      - 服务器日志：``<docs>/N.E.K.O/logs/N.E.K.O_PluginServer_YYYYMMDD.log``
      - 插件日志：  ``<docs>/N.E.K.O/logs/plugin/N.E.K.O_Plugin_{id}_YYYYMMDD.log``

    Args:
        plugin_id: 插件ID（或 SERVER_LOG_ID 表示服务器日志）

    Returns:
        日志目录路径

    Raises:
        HTTPException: 如果 plugin_id 不安全（含路径遍历字符）
    """
    _validate_plugin_id(plugin_id)

    if plugin_id == SERVER_LOG_ID:
        service_name = "PluginServer"
        log_subdir: str | None = None
    else:
        service_name = f"Plugin_{plugin_id}"
        log_subdir = "plugin"
    try:
        return _resolve_robust_log_dir(service_name, log_subdir=log_subdir)
    except HTTPException:
        raise
    except (
        ImportError,  # _resolve_robust_log_dir 会先 import utils.logger_config，
        ModuleNotFoundError,  #   失败时这两类异常会逃过原本的 _RUNTIME_ERRORS。
        *_RUNTIME_ERRORS,
    ) as exc:
        # 终极兜底：临时目录。绝不再走 plugin/ 包内目录（squashfs 只读）。
        # 把 log_subdir 也接到兜底路径上，保持 reader/writer 对偶 —— 否则
        # writer 正常写 ``.../logs/plugin/``、reader 走到这里去读
        # ``.../neko/logs/``，前端直接读空。
        logger.warning(f"Failed to resolve robust log dir for {service_name}: {exc}; falling back to temp")
        import tempfile
        fallback_dir = Path(tempfile.gettempdir()) / "neko" / "logs"
        if log_subdir:
            fallback_dir = fallback_dir / log_subdir
        fallback_dir.mkdir(parents=True, exist_ok=True)
        return fallback_dir


def _is_error_log_file(name: str) -> bool:
    """判断文件名是不是 RobustLoggerConfig 的 ``_error.log`` 系列。

    RobustLoggerConfig 会单独打开 ``N.E.K.O_<Service>_error.log``（以及
    rotation 后的 ``_error.log.1`` / ``_error.log.2025-04-21`` 等）作为
    "永久错误流" 文件。它的 mtime 可能晚于今天的常规日志（因为最近一次
    错误就在刚才），导致 "找最新文件" 的逻辑误选错误日志，让 tail/WebSocket
    feed 只剩下错误行——常规 INFO/DEBUG 整体消失。

    所有 "拿最新一条 log 文件" 的入口都要先用这个 helper 排掉错误文件。
    listing 端点 (``get_plugin_log_files``) 不需要，那里要让用户看到所有文件。
    """
    # ``_error.log`` 或者 rotation 后缀（_error.log.1 / _error.log.2025-04-21 …）
    return "_error.log" in name


def _list_plugin_log_files_for_tail(log_dir: Path, plugin_id: str) -> list[Path]:
    """按 mtime 倒序返回该 plugin 的常规日志文件（已剔除 ``_error.log`` 系列）。

    给 ``get_plugin_logs`` / ``LogFileWatcher`` 共享，避免 6 处 glob 各写各的、
    一个一个修。

    pattern 末尾带 ``*`` —— RotatingFileHandler 轮转后会写出
    ``N.E.K.O_<Service>_YYYYMMDD.log.1`` / ``.log.2026-04-21`` 等后缀；如果只
    匹配 ``*.log`` 会在目录里只剩下轮转文件时返回空，让 tail / WebSocket 误报
    "无日志"。``_is_error_log_file`` 已经覆盖 ``_error.log.1`` 这类后缀。
    """
    if plugin_id == SERVER_LOG_ID:
        pattern = "N.E.K.O_PluginServer_*.log*"
    else:
        pattern = f"N.E.K.O_Plugin_{plugin_id}_*.log*"

    files = [p for p in log_dir.glob(pattern) if not _is_error_log_file(p.name)]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files


def get_plugin_log_files(plugin_id: str) -> list[dict[str, object]]:
    """
    获取插件的日志文件列表
    
    Args:
        plugin_id: 插件ID（或 SERVER_LOG_ID 表示服务器日志）
    
    Returns:
        日志文件列表
    """
    log_dir = get_plugin_log_dir(plugin_id)
    
    if not log_dir.exists():
        return []
    
    log_files = []
    
    # 文件名由 RobustLoggerConfig 控制：N.E.K.O_{ServiceName}_YYYYMMDD.log
    if plugin_id == SERVER_LOG_ID:
        pattern = "N.E.K.O_PluginServer_*.log*"
    else:
        pattern = f"N.E.K.O_Plugin_{plugin_id}_*.log*"
    
    for log_file in log_dir.glob(pattern):
        try:
            stat = log_file.stat()
            log_files.append({
                "filename": log_file.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
        except OSError:
            continue
    
    # 按修改时间排序（最新的在前）
    log_files.sort(key=lambda x: x["modified"], reverse=True)
    return log_files


def list_plugin_log_files_for_export(log_dir: Path, plugin_id: str) -> list[Path]:
    """返回该 plugin 自己的全部日志文件，用于打包导出。

    与 ``_list_plugin_log_files_for_tail`` 的区别：导出要保留 ``_error.log``
    系列，用户排障时错误流往往才是关键。pattern 末尾的 ``*`` 覆盖轮转后缀。

    注意：
    1. plugin_id 必须先经过 sanitize，与实际写入日志时使用的 ID 一致
    2. 文件名匹配精确到插件边界，防止 foo 匹配到 foo_bar 的日志
    """
    from plugin.core.host import _sanitize_plugin_id
    import re

    # 与日志写入时保持一致：先 sanitize plugin_id
    safe_id = _sanitize_plugin_id(plugin_id, max_len=64)

    if plugin_id == SERVER_LOG_ID:
        pattern = "N.E.K.O_PluginServer_*.log*"
    else:
        # 精确匹配插件边界：_后必须是 8位日期(YYYYMMDD) 或 error.log
        # 正确匹配： N.E.K.O_Plugin_foo_20260906.log, N.E.K.O_Plugin_foo_error.log.1
        # 拒绝匹配： N.E.K.O_Plugin_foo_bar_20260906.log
        pattern = f"N.E.K.O_Plugin_{safe_id}_*.log*"

    candidates = [p for p in log_dir.glob(pattern) if p.is_file()]

    # 二次过滤：确保 _ 后是日期或 error，防止前缀误匹配
    if plugin_id != SERVER_LOG_ID:
        # 匹配 N.E.K.O_Plugin_{safe_id}_{YYYYMMDD}.log[.轮转后缀]
        # 或     N.E.K.O_Plugin_{safe_id}_error.log[.轮转后缀]
        # 轮转后缀：RotatingFileHandler 使用 .1, .2, ... 纯数字后缀
        #           TimedRotatingFileHandler 使用 .YYYY-MM-DD 日期后缀
        expected_prefix = f"N.E.K.O_Plugin_{safe_id}_"
        # YYYYMMDD.log 或 YYYYMMDD.log.1 或 YYYYMMDD.log.2026-04-21
        date_pattern = re.compile(r"^\d{8}\.log(?:\.\d+|\.\d{4}-\d{2}-\d{2})?$")
        # error.log 或 error.log.1 或 error.log.2026-04-21
        error_pattern = re.compile(r"^error\.log(?:\.\d+|\.\d{4}-\d{2}-\d{2})?$")

        filtered = []
        for p in candidates:
            # 提取 prefix 之后的部分
            if not p.name.startswith(expected_prefix):
                continue
            suffix = p.name[len(expected_prefix):]
            # 必须是 YYYYMMDD.log* 或 error.log*
            if date_pattern.match(suffix) or error_pattern.match(suffix):
                filtered.append(p)

        return filtered

    return candidates


def get_plugin_log_directory_path(plugin_id: str) -> str:
    """
    获取插件日志目录的绝对路径

    Args:
        plugin_id: 插件ID（或 SERVER_LOG_ID 表示服务器日志）

    Returns:
        日志目录的绝对路径字符串

    Raises:
        HTTPException: 如果 plugin_id 不安全
    """
    log_dir = get_plugin_log_dir(plugin_id)
    return str(log_dir.resolve())


def parse_log_line(line: str) -> dict[str, object] | None:
    """
    解析日志行
    
    支持多种日志格式：
    1. 插件日志格式: 2024-01-01 00:00:00 - [plugin.xxx] - INFO - file.py:123 - message
    2. 服务器日志格式: 2024-01-01 00:00:00,123 - user_plugin_server - INFO - message
    3. 标准日志格式: 2024-01-01 00:00:00 - INFO - message
    4. 管道分隔格式: 2024-01-01 00:00:00 | INFO | module:function:123 | message
    """
    line = line.strip()
    if not line:
        return None
    
    # 模式1: 插件日志格式 - 2024-01-01 00:00:00 - [plugin.xxx] - INFO - file.py:123 - message
    pattern1 = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - \[([^\]]+)\] - (\w+) - ([^:]+):(\d+) - (.+)'
    match = re.match(pattern1, line)
    if match:
        timestamp, name, level, file, line_num, message = match.groups()
        return {
            "timestamp": timestamp,
            "level": level,
            "file": file,
            "line": int(line_num),
            "message": message
        }
    
    # 模式2: 服务器日志格式 - 2024-01-01 00:00:00,123 - user_plugin_server - INFO - message
    pattern2 = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?) - ([^-]+) - (\w+) - (.+)'
    match = re.match(pattern2, line)
    if match:
        timestamp, name, level, message = match.groups()
        return {
            "timestamp": timestamp.strip(),
            "level": level.strip(),
            "file": name.strip(),
            "line": 0,
            "message": message.strip()
        }
    
    # 模式3: 标准日志格式 - 2024-01-01 00:00:00 - INFO - message
    pattern3 = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?) - (\w+) - (.+)'
    match = re.match(pattern3, line)
    if match:
        timestamp, level, message = match.groups()
        return {
            "timestamp": timestamp.strip(),
            "level": level.strip(),
            "file": "",
            "line": 0,
            "message": message.strip()
        }

    pattern3b = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(.+)'
    match = re.match(pattern3b, line)
    if match:
        timestamp, level, message = match.groups()
        return {
            "timestamp": timestamp.strip(),
            "level": level.strip(),
            "file": "",
            "line": 0,
            "message": message.strip(),
        }
    
    # 模式4: 管道分隔格式 - 2024-01-01 00:00:00 | INFO | module:function:123 | message
    # 支持格式: timestamp | level | location | message
    # location 可以是 module:function:line 或 module:function 或 module
    pattern4 = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*(\w+)\s*\|\s*([^|]+)\s*\|\s*(.+)'
    match = re.match(pattern4, line)
    if match:
        timestamp, level, location, message = match.groups()
        # 解析 location: 可能是 module:function:line 或 module:function 或 module
        location_parts = location.strip().split(':')
        if len(location_parts) >= 3:
            # module:function:line
            file = location_parts[0]
            line_num = int(location_parts[-1]) if location_parts[-1].isdigit() else 0
        elif len(location_parts) == 2:
            # module:function
            file = location_parts[0]
            line_num = 0
        else:
            # module
            file = location_parts[0] if location_parts else ""
            line_num = 0
        
        return {
            "timestamp": timestamp.strip(),
            "level": level.strip(),
            "file": file.strip(),
            "line": line_num,
            "message": message.strip()
        }

    # 模式5: loguru 管道分隔格式 - 2024-01-01 00:00:00 | INFO | [Proc-xxx] message
    # 支持格式: timestamp | level | message
    pattern5 = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*(\w+)\s*\|\s*(.+)'
    match = re.match(pattern5, line)
    if match:
        timestamp, level, message = match.groups()
        message = message.strip()

        file = ""
        # 尝试提取 loguru 前缀，如 [Proc-xxx] 或 [Plugin-xxx]
        prefix_match = re.match(r'^\[([^\]]+)\]\s*(.*)$', message)
        if prefix_match:
            file, message = prefix_match.groups()

        return {
            "timestamp": timestamp.strip(),
            "level": level.strip(),
            "file": (file or "").strip(),
            "line": 0,
            "message": (message or "").strip()
        }

    pattern6 = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*\|\s*(\w+)\s*\|\s*([^|]+?)\s*-\s*(.+)'
    match = re.match(pattern6, line)
    if match:
        ts, ms, level, location, message = match.groups()
        timestamp = ts.strip()
        if isinstance(ms, str) and ms:
            timestamp = f"{timestamp}.{ms}"
        location = (location or "").strip()
        message = (message or "").strip()
        file = ""
        line_num = 0
        try:
            parts = location.split(":")
            if len(parts) >= 3 and parts[-1].isdigit():
                line_num = int(parts[-1])
                file = ":".join(parts[:-2]) if len(parts) > 3 else parts[0]
            else:
                file = parts[0] if parts else location
        except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError):
            file = location
            line_num = 0
        return {
            "timestamp": timestamp,
            "level": level.strip(),
            "file": file.strip(),
            "line": line_num,
            "message": message,
        }
    
    # 如果格式不匹配，返回原始行
    return {
        "timestamp": "",
        "level": "UNKNOWN",
        "file": "",
        "line": 0,
        "message": line
    }


def read_log_file_tail(log_file: Path, lines: int = 100) -> list[dict[str, object]]:
    """
    读取日志文件的最后N行
    
    Args:
        log_file: 日志文件路径
        lines: 要读取的行数
    
    Returns:
        解析后的日志条目列表
    """
    if not log_file.exists():
        return []
    
    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            # 使用deque高效读取最后N行
            tail_lines = deque(maxlen=lines)
            for line in f:
                tail_lines.append(line)
            
            # 解析日志行
            parsed_logs = []
            for line in tail_lines:
                log_entry = parse_log_line(line)
                if log_entry:
                    parsed_logs.append(log_entry)
            
            return parsed_logs
    except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError):
        logger.exception(f"Failed to read log file {log_file}")
        return []


def filter_logs(
    logs: list[dict[str, object]],
    level: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    search: str | None = None
) -> list[dict[str, object]]:
    """
    过滤日志
    
    Args:
        logs: 日志列表
        level: 日志级别过滤
        start_time: 开始时间（ISO格式）
        end_time: 结束时间（ISO格式）
        search: 关键词搜索
    
    Returns:
        过滤后的日志列表
    """
    filtered = logs
    
    # 按级别过滤
    if level:
        level_upper = level.upper()
        filtered = [log for log in filtered if log.get("level") == level_upper]
    
    # 按时间过滤
    if start_time or end_time:
        start_dt = _parse_log_time(start_time) if isinstance(start_time, str) else None
        end_dt = _parse_log_time(end_time) if isinstance(end_time, str) else None
        if start_dt is not None or end_dt is not None:
            time_filtered: list[dict[str, object]] = []
            for log in filtered:
                ts = _parse_log_time(log.get("timestamp"))
                if ts is None:
                    continue
                if start_dt is not None and ts < start_dt:
                    continue
                if end_dt is not None and ts > end_dt:
                    continue
                time_filtered.append(log)
            filtered = time_filtered
    
    # 关键词搜索
    if search:
        search_lower = search.lower()
        filtered = [
            log for log in filtered
            if search_lower in log.get("message", "").lower()
        ]
    
    return filtered


def get_plugin_logs(
    plugin_id: str,
    lines: int = 100,
    level: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    search: str | None = None
) -> dict[str, object]:
    """
    获取插件日志或服务器日志
    
    Args:
        plugin_id: 插件ID（或 SERVER_LOG_ID 表示服务器日志）
        lines: 返回的行数
        level: 日志级别过滤
        start_time: 开始时间
        end_time: 结束时间
        search: 关键词搜索
    
    Returns:
        日志数据
    """
    log_dir = get_plugin_log_dir(plugin_id)
    
    if not log_dir.exists():
        return {
            "plugin_id": plugin_id,
            "logs": [],
            "total_lines": 0,
            "returned_lines": 0
        }
    
    # 找到最新的常规日志文件（剔除 _error.log 系列，避免 tail 误读错误流）
    try:
        log_files = _list_plugin_log_files_for_tail(log_dir, plugin_id)
    except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError):
        logger.exception(f"Failed to find log files in {log_dir} for plugin {plugin_id}")
        return {
            "plugin_id": plugin_id,
            "logs": [],
            "total_lines": 0,
            "returned_lines": 0,
            "error": "Failed to find log files"
        }

    if not log_files:
        logger.info(f"No regular log files found in {log_dir} for plugin {plugin_id}")
        return {
            "plugin_id": plugin_id,
            "logs": [],
            "total_lines": 0,
            "returned_lines": 0
        }
    
    latest_log = log_files[0]
    
    # 读取日志
    try:
        logs = read_log_file_tail(latest_log, lines)
    except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError):
        logger.exception(f"Failed to read log file {latest_log}")
        return {
            "plugin_id": plugin_id,
            "logs": [],
            "total_lines": 0,
            "returned_lines": 0,
            "error": "Failed to read log file"
        }
    
    # 过滤
    filtered_logs = filter_logs(logs, level, start_time, end_time, search)
    
    return {
        "plugin_id": plugin_id,
        "logs": filtered_logs,
        "total_lines": len(logs),
        "returned_lines": len(filtered_logs),
        "log_file": latest_log.name
    }


# ========== WebSocket 实时日志推送 ==========

# 全局日志监控器字典：{plugin_id: LogFileWatcher}
_log_watchers: dict[str, "LogFileWatcher"] = {}
# 保护 _log_watchers 的线程锁（用于异步环境下的并发访问）
_log_watchers_lock = threading.Lock()


def read_log_file_incremental(log_file: Path, last_position: int) -> tuple[list[dict[str, object]], int]:
    """
    从指定位置读取日志文件的增量内容
    
    Args:
        log_file: 日志文件路径
        last_position: 上次读取的位置（字节偏移）
    
    Returns:
        (新增的日志条目列表, 新的位置)
    """
    if not log_file.exists():
        return [], last_position
    
    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            # 移动到上次读取的位置
            f.seek(last_position)
            
            # 读取新增内容
            new_lines = f.readlines()
            new_position = f.tell()
            
            # 解析新增的日志行
            new_logs = []
            for line in new_lines:
                log_entry = parse_log_line(line)
                if log_entry:
                    new_logs.append(log_entry)
            
            return new_logs, new_position
    except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError):
        logger.exception(f"Failed to read incremental log from {log_file}")
        return [], last_position


class LogFileWatcher:
    """日志文件监控器，用于 WebSocket 实时推送"""
    
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self.clients: set[WebSocket] = set()
        self.last_position: int = 0
        self.current_log_file: Path | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._running = False
    
    def add_client(self, websocket: WebSocket):
        """添加 WebSocket 客户端"""
        self.clients.add(websocket)
        if not self._running:
            self._start_watching()
    
    def remove_client(self, websocket: WebSocket):
        """移除 WebSocket 客户端"""
        self.clients.discard(websocket)
        if not self.clients and self._running:
            self._stop_watching()
    
    def _start_watching(self):
        """开始监控日志文件"""
        if self._running:
            return
        
        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
    
    def _stop_watching(self):
        """停止监控日志文件"""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None
    
    async def _watch_loop(self):
        """监控循环：定期检查文件变化并推送新日志"""
        while self._running:
            try:
                # 获取最新的日志文件（同样剔除 _error.log，避免 stream 只剩错误行）
                log_dir = get_plugin_log_dir(self.plugin_id)
                log_files = _list_plugin_log_files_for_tail(log_dir, self.plugin_id)

                if not log_files:
                    await asyncio.sleep(1)  # 没有日志文件，等待
                    continue
                
                latest_log = log_files[0]
                
                # 如果日志文件切换了，重置位置
                if self.current_log_file != latest_log:
                    self.current_log_file = latest_log
                    self.last_position = 0
                
                # 读取增量日志
                new_logs, new_position = read_log_file_incremental(
                    latest_log, self.last_position
                )
                
                if new_logs:
                    self.last_position = new_position
                    # 推送新日志给所有客户端
                    await self._broadcast_logs(new_logs)
                
                # 等待 0.5 秒后再次检查
                await asyncio.sleep(0.5)
                
            except asyncio.CancelledError:
                break
            except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError):
                logger.exception(f"Error in log watcher loop for {self.plugin_id}")
                await asyncio.sleep(1)
    
    async def _broadcast_logs(self, logs: list[dict[str, object]]):
        """广播日志给所有连接的客户端"""
        if not logs or not self.clients:
            return
        
        disconnected = []
        for client in list(self.clients):
            try:
                # 检查连接状态
                if hasattr(client, 'client_state'):
                    from starlette.websockets import WebSocketState
                    if client.client_state != WebSocketState.CONNECTED:
                        disconnected.append(client)
                        continue
                
                await client.send_json({
                    "type": "append",
                    "logs": logs
                })
            except (WebSocketDisconnect, ConnectionError, RuntimeError) as e:
                # 连接已断开或关闭，记录但不抛出异常
                logger.debug(f"Failed to send logs to client (disconnected): {e}")
                disconnected.append(client)
            except (ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError) as e:
                # 其他错误，记录并标记为断开
                logger.debug(f"Failed to send logs to client: {e}")
                disconnected.append(client)
        
        # 移除断开的客户端
        for client in disconnected:
            self.clients.discard(client)
    
    async def send_initial_logs(self, websocket: WebSocket, lines: int = 100):
        """发送初始日志（最后 N 行）"""
        try:
            result = get_plugin_logs(self.plugin_id, lines=lines)
            await websocket.send_json({
                "type": "initial",
                "logs": result.get("logs", []),
                "log_file": result.get("log_file"),
                "total_lines": result.get("total_lines", 0)
            })
            
            # 记录当前日志文件和位置（剔除 _error.log，与 _watch_loop / get_plugin_logs 一致）
            log_dir = get_plugin_log_dir(self.plugin_id)
            log_files = _list_plugin_log_files_for_tail(log_dir, self.plugin_id)

            if log_files:
                self.current_log_file = log_files[0]
                # 获取文件当前大小作为起始位置
                self.last_position = self.current_log_file.stat().st_size
        except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError):
            logger.exception("Failed to send initial logs")


async def log_stream_endpoint(websocket: WebSocket, plugin_id: str):
    """
    WebSocket 端点：实时推送日志流
    
    Args:
        websocket: WebSocket 连接
        plugin_id: 插件ID（或 SERVER_LOG_ID 表示服务器日志）
    """
    await websocket.accept()
    
    # 获取或创建监控器（使用锁保护，避免并发访问问题）
    with _log_watchers_lock:
        if plugin_id not in _log_watchers:
            _log_watchers[plugin_id] = LogFileWatcher(plugin_id)
        watcher = _log_watchers[plugin_id]
    
    watcher.add_client(websocket)
    
    try:
        # 发送初始日志
        await watcher.send_initial_logs(websocket, lines=100)
        
        # 保持连接，等待客户端消息（可选）
        while True:
            try:
                # 接收客户端消息（如过滤条件变更等）
                _ = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # 可以处理客户端消息，比如更新过滤条件
                # 目前暂时忽略
            except asyncio.TimeoutError:
                # 发送心跳保持连接
                try:
                    await websocket.send_json({"type": "ping"})
                except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError):
                    # 如果发送失败，连接可能已关闭，退出循环
                    break
            except WebSocketDisconnect:
                break
            except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError) as e:
                # 其他异常，记录日志并退出
                logger.debug(f"WebSocket receive error for {plugin_id}: {e}")
                break
    except WebSocketDisconnect:
        pass
    except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError) as e:
        # 记录错误但不抛出，确保 finally 块执行
        logger.debug(f"Error in log stream endpoint for {plugin_id}: {e}")
    finally:
        # 确保清理客户端连接
        try:
            watcher.remove_client(websocket)
            # 如果没有客户端了，清理监控器（使用锁保护）
            with _log_watchers_lock:
                if not watcher.clients:
                    _log_watchers.pop(plugin_id, None)
        except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, OSError, TimeoutError) as e:
            logger.debug(f"Error cleaning up watcher for {plugin_id}: {e}")
