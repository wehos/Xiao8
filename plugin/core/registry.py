from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import inspect
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Callable, Type, Optional, Iterable, cast

from plugin.logging_config import logger

_DEFAULT_LOGGER = logger


_pending_async_shutdown_tasks: set = set()


def _wrap_logger(logger: Any) -> Any:
    """向后兼容函数，现在直接返回 logger。"""
    return logger


def _import_plugin_entry_module(module_path: str, toml_path: Path, logger: Any) -> Any:
    """延迟使用插件宿主导入兜底，避免用户插件命名空间被内置 plugins 包遮挡。"""

    from plugin.core.host import _import_plugin_module

    return _import_plugin_module(module_path, toml_path, logger)


try:
    import tomllib  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from plugin._types.events import EventHandler, EventMeta, EVENT_META_ATTR
from plugin._types.version import SDK_VERSION
from plugin.server.infrastructure.config_resolver import resolve_plugin_config_from_path
from plugin.server.infrastructure.runtime_overrides import (
    get_runtime_auto_start_override,
    get_runtime_override,
)
from plugin.core.state import state
from plugin.core.entry_points import normalize_plugin_entry_point
from plugin._types.models import PluginMeta, PluginAuthor, PluginDependency
from plugin._types.plugin_types import (
    SUPPORTED_PLUGIN_TYPES,
    format_unsupported_plugin_type,
)
from plugin.core.ui_manifest import normalize_plugin_ui_manifest
from plugin.settings import (
    BUILTIN_PLUGIN_CONFIG_ROOT,
    PLUGIN_ENABLE_ID_CONFLICT_CHECK,
    PLUGIN_ENABLE_DEPENDENCY_CHECK,
)
from plugin.utils import parse_bool_config

# 从 dependency.py 导入依赖相关函数
from plugin.core.dependency import (
    _parse_specifier,
    _version_matches,
    _check_plugin_dependency,
    _parse_plugin_dependencies,
    _topological_sort_plugins,
)
from plugin.core.python_dependencies import (
    collect_project_python_requirements,
    find_missing_python_requirements,
    load_pyproject_toml,
    split_host_provided_requirements,
)

try:
    from packaging.version import Version, InvalidVersion
    from packaging.specifiers import SpecifierSet, InvalidSpecifier
except ImportError:  # pragma: no cover
    Version = None  # type: ignore
    InvalidVersion = Exception  # type: ignore
    SpecifierSet = None  # type: ignore
    InvalidSpecifier = Exception  # type: ignore


# SimpleEntryMeta 已删除，统一使用 sdk/events.py 中的 EventMeta


@dataclass
class PluginContext:
    """插件加载上下文，存储解析后的插件配置信息"""
    pid: str
    toml_path: Path
    conf: Dict[str, Any]
    pdata: Dict[str, Any]
    entry: str
    dependencies: List[PluginDependency]
    sdk_supported_str: Optional[str]
    sdk_recommended_str: Optional[str]
    sdk_untested_str: Optional[str]
    sdk_conflicts_list: List[str]
    enabled: bool
    auto_start: bool
    python_requirements: List[str] = field(default_factory=list)
    python_requirement_paths: List[Path] = field(default_factory=list)


def _extract_plugin_ui_config(conf: Dict[str, Any], *, plugin_id: str, logger: Any) -> Optional[Dict[str, Any]]:
    _ = logger
    return normalize_plugin_ui_manifest(conf, plugin_id=plugin_id)


# Mapping from (plugin_id, entry_id) -> actual python method name on the instance.
plugin_entry_method_map: Dict[tuple, str] = {}


def _collect_plugin_python_requirements(
    conf: Dict[str, Any],
    toml_path: Path,
    logger: Any,
    plugin_id: str,
) -> List[str]:
    """Collect plugin Python dependencies from pyproject.toml and legacy requirements.txt."""
    collected: List[str] = []
    seen: set[str] = set()

    def _add_req(item: Any, source: str) -> None:
        if not isinstance(item, str):
            logger.warning(
                "Plugin {}: python dependency in {} must be string, got {}; skipping",
                plugin_id,
                source,
                type(item).__name__,
            )
            return
        req_text = item.strip()
        if not req_text or req_text.startswith("#"):
            return
        key = req_text.lower()
        if key in seen:
            return
        seen.add(key)
        collected.append(req_text)

    try:
        python_requirements = collect_project_python_requirements(load_pyproject_toml(toml_path.parent))
        external_requirements, _host_requirements = split_host_provided_requirements(python_requirements)
        for dep in external_requirements:
            _add_req(dep, "pyproject.toml [project].dependencies")
    except Exception as exc:
        logger.warning(
            "Plugin {}: failed to read pyproject.toml Python dependencies: {}",
            plugin_id,
            exc,
        )

    req_file = toml_path.parent / "requirements.txt"
    if req_file.exists():
        try:
            for raw_line in req_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if not line:
                    continue
                _add_req(line, "requirements.txt")
        except OSError as exc:
            logger.warning(
                "Plugin {}: failed to read {}: {}",
                plugin_id,
                req_file,
                exc,
            )

    return collected


def _plugin_vendor_path(toml_path: Path) -> Path:
    return toml_path.parent / "vendor"


def _collect_plugin_python_requirement_paths(toml_path: Path) -> List[Path]:
    vendor_dir = _plugin_vendor_path(toml_path)
    if vendor_dir.is_dir():
        return [vendor_dir]
    return []


def _ensure_python_requirement_paths(paths: Iterable[Path | str], logger: Any, plugin_id: str) -> None:
    for path in paths:
        value = str(path)
        if not value or value in sys.path:
            continue
        sys.path.insert(0, value)
        logger.info("Plugin {}: added Python requirement path to sys.path: {}", plugin_id, value)


def _find_missing_python_requirements(
    requirements: List[str],
    search_paths: Iterable[Path | str] | None = None,
) -> List[str]:
    """Return unsatisfied requirement specs based on installed distributions."""
    try:
        return find_missing_python_requirements(requirements, search_paths=search_paths)
    except Exception as e:
        logger.warning(
            "Failed to enumerate installed distributions, dependency checks will be skipped: {}",
            e,
        )
        return []




def get_plugins() -> List[Dict[str, Any]]:
    """Return list of plugin dicts (in-process access)."""
    with state.acquire_plugins_read_lock():
        return list(state.plugins.values())


def _calculate_plugin_hash(config_path: Optional[Path] = None, entry_point: Optional[str] = None, plugin_data: Optional[Dict[str, Any]] = None) -> str:
    """
    计算插件的哈希值，用于比较插件内容是否相同
    
    注意：为了确保相同插件产生相同哈希值，路径会被规范化（resolve为绝对路径）
    
    Args:
        config_path: 插件配置文件路径
        entry_point: 插件入口点
        plugin_data: 插件配置数据（可选），应包含 id、name、version、entry 字段
    
    Returns:
        插件的哈希值（十六进制字符串）
    """
    hash_data = []
    
    # 添加配置文件路径（如果提供）- 规范化路径以确保一致性
    if config_path:
        try:
            # 使用 resolve() 获取绝对路径并规范化
            resolved_path = config_path.resolve()
            # 使用字符串表示，确保跨平台一致性
            hash_data.append(f"config_path:{str(resolved_path)}")
        except (OSError, RuntimeError):
            # 如果路径解析失败，使用原始路径的字符串表示
            hash_data.append(f"config_path:{str(config_path)}")
    
    # 添加入口点（如果提供）- 标准化格式
    if entry_point:
        hash_data.append(f"entry_point:{entry_point.strip()}")
    
    # 添加插件配置数据的关键字段（如果提供）
    if plugin_data:
        # 使用关键字段来标识插件，按固定顺序以确保一致性
        key_fields = ["id", "name", "version", "entry"]
        for field in key_fields:
            if field in plugin_data:
                value = plugin_data[field]
                # 确保值为字符串，None 转为空字符串
                if value is None:
                    value = ""
                else:
                    value = str(value).strip()
                hash_data.append(f"{field}:{value}")
    
    # 计算哈希值
    content = "|".join(hash_data)
    return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]  # 使用前16位作为简短标识


def _get_existing_plugin_info(plugin_id: str) -> Optional[Dict[str, Any]]:
    """
    获取已存在插件的信息
    
    Args:
        plugin_id: 插件 ID
    
    Returns:
        插件信息字典，包含 config_path、entry_point、plugin_meta 等，如果不存在则返回 None
    """
    result = {}
    
    # 锁顺序规范: plugins_lock -> plugin_hosts_lock -> event_handlers_lock
    # 先从 plugins 获取插件元数据
    with state.acquire_plugins_read_lock():
        if plugin_id in state.plugins:
            plugin_meta_raw = state.plugins[plugin_id]
            if isinstance(plugin_meta_raw, dict):
                result["plugin_meta"] = plugin_meta_raw
                meta_config_path = plugin_meta_raw.get("config_path")
                meta_entry_point = plugin_meta_raw.get("entry_point")
                if meta_config_path:
                    result["config_path"] = meta_config_path
                if meta_entry_point:
                    result["entry_point"] = meta_entry_point
            else:
                result["plugin_meta"] = plugin_meta_raw
                meta_config_path = getattr(plugin_meta_raw, "config_path", None)
                meta_entry_point = getattr(plugin_meta_raw, "entry_point", None)
                if meta_config_path:
                    result["config_path"] = meta_config_path
                if meta_entry_point:
                    result["entry_point"] = meta_entry_point
    
    # 再从 plugin_hosts 获取运行时信息（可能更新）
    with state.acquire_plugin_hosts_read_lock():
        if plugin_id in state.plugin_hosts:
            host = state.plugin_hosts[plugin_id]
            config_path = getattr(host, 'config_path', None)
            entry_point = getattr(host, 'entry_point', None)
            if config_path:
                result["config_path"] = config_path
            if entry_point:
                result["entry_point"] = entry_point
    
    # 如果获取到了任何信息，返回结果
    if result:
        return result
    
    return None


def _resolve_plugin_id_conflict(
    plugin_id: str,
    logger: Any,  # loguru.Logger or logging.Logger
    config_path: Optional[Path] = None,
    entry_point: Optional[str] = None,
    plugin_data: Optional[Dict[str, Any]] = None,
    *,
    purpose: str = "load",
    enable_rename: Optional[bool] = None,
) -> Optional[str]:
    """
    检测并解决插件 ID 冲突
    
    如果插件 ID 已存在（在 plugins 或 plugin_hosts 中），
    生成一个新的唯一 ID（添加数字后缀）并记录警告。
    如果两个插件的内容哈希值相同，会记录更详细的日志。
    
    Args:
        plugin_id: 原始插件 ID
        logger: 日志记录器
        config_path: 当前插件的配置文件路径（可选，用于哈希计算）
        entry_point: 当前插件的入口点（可选，用于哈希计算）
        plugin_data: 当前插件的配置数据（可选，用于哈希计算）
    
    Returns:
        解决冲突后的插件 ID（如果无冲突则返回原始 ID，如果是重复加载则返回 None）
    """
    logger = _wrap_logger(logger)
    _ = entry_point
    _ = plugin_data

    if enable_rename is None:
        enable_rename = bool(PLUGIN_ENABLE_ID_CONFLICT_CHECK)

    purpose_norm = str(purpose).strip().lower() if isinstance(purpose, str) else "load"
    if purpose_norm not in ("load", "register"):
        purpose_norm = "load"

    cur_path: Optional[Path] = None
    if config_path is not None:
        try:
            cur_path = Path(config_path).resolve()
        except (OSError, RuntimeError):
            cur_path = Path(config_path)

    with state.acquire_plugins_read_lock():
        plugins_snapshot = dict(state.plugins)
    with state.acquire_plugin_hosts_read_lock():
        hosts_snapshot = dict(state.plugin_hosts)

    def _resolve_existing_path(v: Any) -> Optional[Path]:
        if v is None:
            return None
        try:
            return Path(v).resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            try:
                return Path(v)
            except Exception:
                return None

    def _get_id_ref(pid: str) -> tuple[Optional[Path], Optional[str]]:
        host = hosts_snapshot.get(pid)
        if host is not None:
            hp = _resolve_existing_path(getattr(host, "config_path", None))
            he = getattr(host, "entry_point", None)
            return hp, str(he) if isinstance(he, str) and he else None
        meta = plugins_snapshot.get(pid)
        if isinstance(meta, dict):
            mp = _resolve_existing_path(meta.get("config_path"))
            me = meta.get("entry_point")
            return mp, str(me) if isinstance(me, str) and me else None
        return None, None

    def _find_id_by_path(p: Path) -> Optional[str]:
        for pid, host in hosts_snapshot.items():
            hp = _resolve_existing_path(getattr(host, "config_path", None))
            if hp is not None and hp == p:
                return str(pid)
        for pid, meta in plugins_snapshot.items():
            if not isinstance(meta, dict):
                continue
            mp = _resolve_existing_path(meta.get("config_path"))
            if mp is not None and mp == p:
                return str(pid)
        return None

    if cur_path is not None:
        existing_by_path = _find_id_by_path(cur_path)
        if isinstance(existing_by_path, str) and existing_by_path:
            if existing_by_path == str(plugin_id):
                if purpose_norm == "load" and str(plugin_id) in hosts_snapshot:
                    return None
                return str(plugin_id)
            return None

    desired = str(plugin_id)
    if desired not in plugins_snapshot and desired not in hosts_snapshot:
        return desired

    existing_path, _existing_entry = _get_id_ref(desired)
    if cur_path is not None and existing_path is not None and cur_path == existing_path:
        if purpose_norm == "load" and desired in hosts_snapshot:
            return None
        return desired

    if purpose_norm == "register" and desired in hosts_snapshot and desired not in plugins_snapshot:
        return desired

    if not bool(enable_rename):
        return None

    counter = 1
    new_id = f"{desired}_{counter}"
    while new_id in plugins_snapshot or new_id in hosts_snapshot:
        counter += 1
        new_id = f"{desired}_{counter}"
    logger.warning(
        "Plugin ID conflict detected: '{}' is already taken by a different plugin. Renaming to '{}'",
        desired,
        new_id,
    )
    return new_id


def register_plugin(
    plugin: PluginMeta,
    logger: Optional[Any] = None,  # loguru.Logger or logging.Logger
    config_path: Optional[Path] = None,
    entry_point: Optional[str] = None
) -> Optional[str]:
    """
    注册插件到注册表
    
    Args:
        plugin: 插件元数据
        logger: 日志记录器（可选，用于冲突检测）
        config_path: 插件配置文件路径（可选，用于哈希计算）
        entry_point: 插件入口点（可选，用于哈希计算）
    
    Returns:
        实际注册的插件 ID（如果发生冲突，返回重命名后的 ID）
    """
    logger_ = cast(Any, _wrap_logger(logger or _DEFAULT_LOGGER))

    plugin_type = getattr(plugin, "type", None)
    plugin_id = getattr(plugin, "id", "<unknown>")
    if not isinstance(plugin_type, str) or plugin_type not in SUPPORTED_PLUGIN_TYPES:
        logger_.error(format_unsupported_plugin_type(plugin_type, plugin_id=plugin_id))
        return None

    # 准备插件数据用于哈希计算
    plugin_data = {
        "id": plugin.id,
        "name": plugin.name,
        "version": plugin.version,
        "entry": entry_point or "",
    }

    # 检测并解决 ID 冲突
    resolved_id = _resolve_plugin_id_conflict(
        plugin.id,
        logger_,
        config_path=config_path,
        entry_point=entry_point,
        plugin_data=plugin_data,
        purpose="register",
        enable_rename=bool(PLUGIN_ENABLE_ID_CONFLICT_CHECK),
    )
    
    # 如果返回 None，说明是重复加载，不应该注册
    if resolved_id is None:
        logger_.warning(
            "Plugin {} is already loaded (duplicate detected), skipping registration",
            plugin.id
        )
        # 返回 None 作为特殊标记，表示这是重复加载
        return None
    
    # 如果 ID 被重命名，更新插件元数据
    if resolved_id != plugin.id:
        plugin = plugin.model_copy(update={"id": resolved_id})

    with state.acquire_plugins_write_lock():
        plugin_dump = plugin.model_dump()
        if config_path is not None:
            plugin_dump["config_path"] = str(config_path)
        if entry_point is not None:
            plugin_dump["entry_point"] = entry_point
        state.plugins[resolved_id] = plugin_dump
    state.invalidate_snapshot_cache("plugins")

    return resolved_id


def scan_static_metadata(pid: str, cls: type, conf: dict, pdata: dict) -> None:
    """
    在不实例化的情况下扫描类属性，提取 @EventHandler 元数据并填充全局表。
    """
    # 使用模块级 logger
    handlers_updated = False
    for name, member in inspect.getmembers(cls):
        event_meta = getattr(member, EVENT_META_ATTR, None)
        if event_meta is None and hasattr(member, "__wrapped__"):
            event_meta = getattr(member.__wrapped__, EVENT_META_ATTR, None)

        if event_meta:
            etype = getattr(event_meta, "event_type", None) or "plugin_entry"
            eid = getattr(event_meta, "id", name)
            handler_obj = EventHandler(meta=event_meta, handler=member)
            with state.acquire_event_handlers_write_lock():
                if etype == "plugin_entry":
                    state.event_handlers[f"{pid}.{eid}"] = handler_obj
                    state.event_handlers[f"{pid}:plugin_entry:{eid}"] = handler_obj
                else:
                    state.event_handlers[f"{pid}:{etype}:{eid}"] = handler_obj
            handlers_updated = True
            if etype == "plugin_entry":
                plugin_entry_method_map[(pid, str(eid))] = name
    if handlers_updated:
        state.invalidate_snapshot_cache("handlers")

    entries = _effective_entries(conf, pdata)
    for ent in entries:
        try:
            eid = ent.get("id") if isinstance(ent, dict) else str(ent)
            if not eid:
                continue
            try:
                handler_fn = getattr(cls, eid)
            except AttributeError:
                logger.warning(
                    "Entry id {} for plugin {} has no handler on class {}, skipping",
                    eid,
                    pid,
                    cls.__name__,
                )
                continue
            entry_meta = EventMeta(
                event_type="plugin_entry",
                id=eid,
                name=ent.get("name", "") if isinstance(ent, dict) else "",
                description=ent.get("description", "") if isinstance(ent, dict) else "",
                input_schema=ent.get("input_schema", {}) if isinstance(ent, dict) else {},
            )
            eh = EventHandler(meta=entry_meta, handler=handler_fn)
            with state.acquire_event_handlers_write_lock():
                state.event_handlers[f"{pid}.{eid}"] = eh
                state.event_handlers[f"{pid}:plugin_entry:{eid}"] = eh
        except (AttributeError, KeyError, TypeError) as e:
            logger.warning("Error parsing entry {} for plugin {}: {}", ent, pid, e, exc_info=True)
            # 继续处理其他条目，不中断整个插件加载


def _remove_scanned_metadata(pid: str) -> None:
    removed_any = False
    with state.acquire_event_handlers_write_lock():
        prefix_dot = f"{pid}."
        prefix_colon = f"{pid}:"
        keys_to_remove = [
            key
            for key in list(state.event_handlers.keys())
            if key.startswith(prefix_dot) or key.startswith(prefix_colon)
        ]
        for key in keys_to_remove:
            del state.event_handlers[key]
            removed_any = True
    for plugin_key in list(plugin_entry_method_map.keys()):
        if plugin_key[0] == pid:
            del plugin_entry_method_map[plugin_key]
            removed_any = True
    if removed_any:
        state.invalidate_snapshot_cache("handlers")


def _build_plugin_meta(
    pid: str,
    pdata: dict,
    *,
    sdk_supported_str: Optional[str] = None,
    sdk_recommended_str: Optional[str] = None,
    sdk_untested_str: Optional[str] = None,
    sdk_conflicts_list: Optional[List[str]] = None,
    dependencies: Optional[List[PluginDependency]] = None,
    input_schema: Optional[Dict[str, Any]] = None,
    plugin_ui: Optional[Dict[str, Any]] = None,
) -> PluginMeta:
    """统一构建 PluginMeta，消除 disabled / normal 两处重复。"""
    author_data = pdata.get("author")
    author = None
    if author_data and isinstance(author_data, dict):
        author = PluginAuthor(
            name=author_data.get("name"),
            email=author_data.get("email"),
        )

    # 读取 keywords (正则表达式列表) 和 short_description
    raw_keywords = pdata.get("keywords", [])
    keywords: list[str] = []
    if isinstance(raw_keywords, list):
        for kw in raw_keywords:
            if isinstance(kw, str) and kw.strip():
                keywords.append(kw.strip())
    short_desc = str(pdata.get("short_description", "") or "").strip()
    # Defensive cap on plugin manifest short_description. 200 tokens — same
    # as task_executor's downstream short_description LLM-prompt cap, so the
    # value is consistent across "plugin descriptive blurb" callsites.
    from utils.tokenize import count_tokens, truncate_to_tokens
    if count_tokens(short_desc) > 200:
        short_desc = truncate_to_tokens(short_desc, 200)
    passive = parse_bool_config(pdata.get("passive"), default=False)

    meta = PluginMeta(
        id=pid,
        name=pdata.get("name", pid),
        type=pdata.get("type", "plugin"),
        description=pdata.get("description", ""),
        short_description=short_desc,
        keywords=keywords,
        passive=passive,
        version=pdata.get("version", "0.1.0"),
        sdk_version=sdk_supported_str or SDK_VERSION,
        sdk_recommended=sdk_recommended_str,
        sdk_supported=sdk_supported_str,
        sdk_untested=sdk_untested_str,
        sdk_conflicts=sdk_conflicts_list or [],
        input_schema=input_schema or {"type": "object", "properties": {}},
        author=author,
        dependencies=dependencies or [],
    )
    if plugin_ui is not None:
        setattr(meta, "plugin_ui", plugin_ui)
    i18n_config = pdata.get("i18n")
    if not isinstance(i18n_config, dict):
        i18n_config = {}
    setattr(meta, "i18n", {
        "default_locale": str(i18n_config.get("default_locale") or "en"),
        "locales_dir": str(i18n_config.get("locales_dir") or "i18n"),
    })
    return meta


def _router_entry_preview(
    pid: str,
    eid: str,
    event_meta: Any,
    _to_dict: Callable[[Any], Dict[str, Any]],
    _to_string_list: Callable[[Any], List[str]],
) -> Dict[str, Any]:
    """Build the static preview dict for one router-declared entry.

    与 1) 分支保持一致：从 `event_meta` 读 return_message，避免 router 预览丢字段喵。
    """
    name_obj = getattr(event_meta, "name", None) or ""
    description_obj = getattr(event_meta, "description", None) or ""
    return_message_obj = getattr(event_meta, "return_message", None)
    if return_message_obj is None:
        return_message_obj = ""
    preview: Dict[str, Any] = {
        "id": eid,
        "name": name_obj if isinstance(name_obj, (str, dict)) else str(name_obj),
        "description": description_obj if isinstance(description_obj, (str, dict)) else str(description_obj),
        "event_key": f"{pid}.{eid}",
        "input_schema": _to_dict(getattr(event_meta, "input_schema", {}) or {}),
        "return_message": return_message_obj if isinstance(return_message_obj, (str, dict)) else str(return_message_obj),
        "event_type": "plugin_entry",
        "kind": str(getattr(event_meta, "kind", "action") or "action"),
        "auto_start": bool(getattr(event_meta, "auto_start", False)),
        "timeout": getattr(event_meta, "timeout", None),
        "model_validate": bool(getattr(event_meta, "model_validate", True)),
        "llm_result_fields": _to_string_list(getattr(event_meta, "llm_result_fields", None)),
        "llm_result_schema": _to_dict(getattr(event_meta, "llm_result_schema", {}) or {}),
        "metadata": _to_dict(getattr(event_meta, "metadata", {}) or {}),
    }
    meta_dict = getattr(event_meta, "metadata", None)
    if isinstance(meta_dict, dict) and "llm_result_fields" in meta_dict:
        preview["llm_result_fields"] = meta_dict["llm_result_fields"]
    return preview


def _effective_entries(conf: dict, pdata: dict) -> Any:
    """The ``entries`` table the effective configuration declares.

    Selected by key presence, not by truthiness. An overlay that sets
    ``entries = []`` is removing them, and ``deep_merge`` keeps that empty list;
    reading it as "no entries table here" falls through to the manifest and
    shows the user the very entries their configuration removed (coderabbit).
    """
    for table in (conf, pdata):
        if isinstance(table, dict) and "entries" in table:
            value = table["entries"]
            return value if isinstance(value, (list, dict)) else []
    return []


def _extract_entries_preview(pid: str, cls: type, conf: dict, pdata: dict) -> List[Dict[str, Any]]:
    """Extract entry metadata for UI visibility without registering event handlers.

    NOTE: This function must not touch state.event_handlers. It is intended for disabled
    plugins (visibility only) so that UI can still display entries.
    """
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _to_dict(v: Any) -> Dict[str, Any]:
        if isinstance(v, dict):
            return v
        try:
            if hasattr(v, "model_dump"):
                d = v.model_dump()
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
        return {}

    def _to_string_list(v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        out: List[str] = []
        seen: set[str] = set()
        for item in v:
            if not isinstance(item, str):
                continue
            field_name = item.strip()
            if not field_name or field_name in seen:
                continue
            seen.add(field_name)
            out.append(field_name)
        return out

    # 1) Decorator-based metadata (@plugin_entry / EVENT_META_ATTR)
    try:
        for name, member in inspect.getmembers(cls):
            event_meta = getattr(member, EVENT_META_ATTR, None)
            if event_meta is None and hasattr(member, "__wrapped__"):
                event_meta = getattr(member.__wrapped__, EVENT_META_ATTR, None)

            if not event_meta:
                continue
            etype = getattr(event_meta, "event_type", None) or "plugin_entry"
            if etype != "plugin_entry":
                continue

            eid = str(getattr(event_meta, "id", None) or name)
            if not eid or eid in seen:
                continue
            seen.add(eid)

            input_schema = _to_dict(getattr(event_meta, "input_schema", {}) or {})
            name_obj = getattr(event_meta, "name", None)
            description_obj = getattr(event_meta, "description", None)
            return_message_obj = getattr(event_meta, "return_message", None)
            if name_obj is None:
                name_obj = ""
            if description_obj is None:
                description_obj = ""
            if return_message_obj is None:
                return_message_obj = ""
            entry_preview: Dict[str, Any] = {
                    "id": eid,
                    "name": name_obj if isinstance(name_obj, (str, dict)) else str(name_obj),
                    "description": description_obj if isinstance(description_obj, (str, dict)) else str(description_obj),
                    "event_key": f"{pid}.{eid}",
                    "input_schema": input_schema,
                    "return_message": return_message_obj if isinstance(return_message_obj, (str, dict)) else str(return_message_obj),
                    "event_type": str(getattr(event_meta, "event_type", "plugin_entry") or "plugin_entry"),
                    "kind": str(getattr(event_meta, "kind", "action") or "action"),
                    "auto_start": bool(getattr(event_meta, "auto_start", False)),
                    "timeout": getattr(event_meta, "timeout", None),
                    "model_validate": bool(getattr(event_meta, "model_validate", True)),
                    "llm_result_fields": _to_string_list(getattr(event_meta, "llm_result_fields", None)),
                    "llm_result_schema": _to_dict(getattr(event_meta, "llm_result_schema", {}) or {}),
                    "metadata": _to_dict(getattr(event_meta, "metadata", {}) or {}),
                }
            meta_dict = getattr(event_meta, "metadata", None)
            if isinstance(meta_dict, dict) and "llm_result_fields" in meta_dict:
                entry_preview["llm_result_fields"] = meta_dict["llm_result_fields"]
            results.append(entry_preview)
    except Exception:
        # Best-effort: preview must never break plugin listing.
        pass

    # 1b) Router-decorated entries declared via `__routers__`.
    #     这些 entry 是通过 @plugin_entry 装饰在 PluginRouter 子类方法上的，
    #     静态 preview 之前只看 plugin class 本身的成员，所以 router 入口对
    #     UI/agent 列表完全不可见喵。这里额外扫描 `__routers__` 里声明的
    #     router 类（或已实例化的 router），和 1) 一样只抽元数据，不触发运行时。
    #
    #     如果声明的是已实例化的 router，优先用实例 `collect_entries()` 拿
    #     prefix 解析后的入口，避免 preview 的 id 与运行时不一致而被 seen 误去重喵。
    try:
        declared_routers = getattr(cls, "__routers__", None) or []
        for router_item in declared_routers:
            router_obj = router_item if not isinstance(router_item, type) else None
            router_cls = router_item if isinstance(router_item, type) else type(router_item)

            # 优先使用实例的已解析入口
            instance_handled = False
            if router_obj is not None and hasattr(router_obj, "collect_entries"):
                try:
                    collected = router_obj.collect_entries() or {}
                except Exception:
                    collected = {}
                if collected:
                    instance_handled = True
                    for resolved_id, handler in collected.items():
                        event_meta = getattr(handler, "meta", None)
                        if event_meta is None:
                            continue
                        etype = getattr(event_meta, "event_type", None) or "plugin_entry"
                        if etype != "plugin_entry":
                            continue
                        eid = str(resolved_id or getattr(event_meta, "id", "") or "")
                        if not eid or eid in seen:
                            continue
                        seen.add(eid)
                        results.append(_router_entry_preview(pid, eid, event_meta, _to_dict, _to_string_list))

            if instance_handled:
                continue

            for name, member in inspect.getmembers(router_cls):
                event_meta = getattr(member, EVENT_META_ATTR, None)
                if event_meta is None and hasattr(member, "__wrapped__"):
                    event_meta = getattr(member.__wrapped__, EVENT_META_ATTR, None)
                if not event_meta:
                    continue
                etype = getattr(event_meta, "event_type", None) or "plugin_entry"
                if etype != "plugin_entry":
                    continue
                eid = str(getattr(event_meta, "id", None) or name)
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                results.append(_router_entry_preview(pid, eid, event_meta, _to_dict, _to_string_list))
    except Exception:
        pass

    # 2) Config-specified entries (conf/pdata)
    entries = _effective_entries(conf, pdata)
    for ent in entries:
        try:
            if isinstance(ent, dict):
                eid = str(ent.get("id") or "")
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                results.append(
                    {
                        "id": eid,
                        "name": ent.get("name") if isinstance(ent.get("name"), (str, dict)) else str(ent.get("name") or ""),
                        "description": ent.get("description") if isinstance(ent.get("description"), (str, dict)) else str(ent.get("description") or ""),
                        "event_key": f"{pid}.{eid}",
                        "input_schema": _to_dict(ent.get("input_schema") or {}),
                        "return_message": "",
                        "event_type": "plugin_entry",
                        "kind": str(ent.get("kind") or "action"),
                        "auto_start": bool(ent.get("auto_start", False)),
                        "timeout": ent.get("timeout"),
                        "model_validate": bool(ent.get("model_validate", True)),
                        "llm_result_fields": _to_string_list(ent.get("llm_result_fields")),
                        "llm_result_schema": _to_dict(ent.get("llm_result_schema") or {}),
                        "metadata": _to_dict(ent.get("metadata") or {}),
                    }
                )
            else:
                eid = str(ent)
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                results.append(
                    {
                        "id": eid,
                        "name": "",
                        "description": "",
                        "event_key": f"{pid}.{eid}",
                        "input_schema": {},
                        "return_message": "",
                        "event_type": "plugin_entry",
                        "kind": "action",
                        "auto_start": False,
                        "timeout": None,
                        "model_validate": True,
                        "llm_result_fields": [],
                        "llm_result_schema": {},
                        "metadata": {},
                    }
                )
        except Exception:
            continue

    return results


# ============================================================================
# load_plugins_from_toml 辅助函数
# ============================================================================

def _check_sdk_compatibility(
    pid: str,
    sdk_config: Optional[Dict[str, Any]],
    logger: Any,
) -> tuple[bool, Optional[str], Optional[str], Optional[str], List[str]]:
    """
    检查插件的 SDK 版本兼容性。
    
    Args:
        pid: 插件 ID
        sdk_config: SDK 配置字典 (plugin.sdk)
        logger: 日志记录器
    
    Returns:
        (is_compatible, sdk_supported_str, sdk_recommended_str, sdk_untested_str, sdk_conflicts_list)
        如果不兼容，is_compatible 为 False
    """
    sdk_supported_str = None
    sdk_recommended_str = None
    sdk_untested_str = None
    sdk_conflicts_list: List[str] = []
    
    # 解析 SDK 配置
    if isinstance(sdk_config, dict):
        sdk_recommended_str = sdk_config.get("recommended")
        sdk_supported_str = sdk_config.get("supported") or sdk_config.get("compatible")
        sdk_untested_str = sdk_config.get("untested")
        raw_conflicts = sdk_config.get("conflicts") or []
        if isinstance(raw_conflicts, list):
            sdk_conflicts_list = [str(c) for c in raw_conflicts if c]
        elif isinstance(raw_conflicts, str) and raw_conflicts.strip():
            sdk_conflicts_list = [raw_conflicts.strip()]
    elif sdk_config is not None:
        logger.error(
            "Plugin {}: SDK configuration must be a dict (plugin.sdk block), got {}; skipping load",
            pid, type(sdk_config).__name__
        )
        return False, None, None, None, []
    
    # 版本检查
    host_version_obj: Optional[Any] = None
    if Version and SpecifierSet:
        try:
            host_version_obj = Version(SDK_VERSION)
        except InvalidVersion as e:
            logger.error("Invalid host SDK_VERSION {}: {}", SDK_VERSION, e)
            host_version_obj = None
    
    if host_version_obj:
        supported_spec = _parse_specifier(sdk_supported_str, logger)
        recommended_spec = _parse_specifier(sdk_recommended_str, logger)
        untested_spec = _parse_specifier(sdk_untested_str, logger)
        conflict_specs = [_parse_specifier(c, logger) for c in sdk_conflicts_list]
        
        # 验证 specifier 格式
        if sdk_supported_str and supported_spec is None:
            logger.error("Plugin {}: invalid supported SDK spec '{}'; skipping load", pid, sdk_supported_str)
            return False, None, None, None, []
        if sdk_untested_str and untested_spec is None:
            logger.error("Plugin {}: invalid untested SDK spec '{}'; skipping load", pid, sdk_untested_str)
            return False, None, None, None, []
        invalid_conflicts = [c for c, s in zip(sdk_conflicts_list, conflict_specs) if c and s is None]
        if invalid_conflicts:
            logger.error("Plugin {}: invalid conflict SDK spec(s) {}; skipping load", pid, invalid_conflicts)
            return False, None, None, None, []
        
        # 冲突检查
        if any(spec and _version_matches(spec, host_version_obj) for spec in conflict_specs):
            logger.error(
                "Plugin {} conflicts with host SDK {} (conflict ranges: {}); skipping load",
                pid, SDK_VERSION, sdk_conflicts_list
            )
            return False, None, None, None, []
        
        # 兼容性检查
        in_supported = _version_matches(supported_spec, host_version_obj)
        in_untested = _version_matches(untested_spec, host_version_obj)
        
        if supported_spec and not (in_supported or in_untested):
            logger.error(
                "Plugin {} requires SDK in {} (or untested {}) but host SDK is {}; skipping load",
                pid, sdk_supported_str, sdk_untested_str, SDK_VERSION
            )
            return False, None, None, None, []
        
        # 警告
        if recommended_spec and not _version_matches(recommended_spec, host_version_obj):
            logger.warning("Plugin {}: host SDK {} is outside recommended range {}", pid, SDK_VERSION, sdk_recommended_str)
        if in_untested and not in_supported:
            logger.warning("Plugin {}: host SDK {} is within untested range {}; proceed with caution", pid, SDK_VERSION, sdk_untested_str)
    else:
        # 回退到字符串比较
        if sdk_supported_str and sdk_supported_str != SDK_VERSION:
            logger.error("Plugin {} requires sdk_version {} but host SDK is {}; skipping load", pid, sdk_supported_str, SDK_VERSION)
            return False, None, None, None, []
    
    return True, sdk_supported_str, sdk_recommended_str, sdk_untested_str, sdk_conflicts_list


def _parse_single_plugin_config(
    toml_path: Path,
    processed_paths: set,
    logger: Any,
    *,
    apply_user_overlays: bool = True,
) -> Optional[PluginContext]:
    """
    解析单个插件的 TOML 配置文件。
    
    Args:
        toml_path: TOML 文件路径
        processed_paths: 已处理的路径集合（用于去重）
        logger: 日志记录器
    
    Returns:
        PluginContext 或 None（如果解析失败或应跳过）
    """
    logger.info("Processing plugin config: {}", toml_path)
    
    try:
        with toml_path.open("rb") as f:
            conf = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        logger.error("Failed to parse plugin config {}: {}", toml_path, e)
        return None
    
    pdata = conf.get("plugin") or {}
    pid = pdata.get("id")
    if not pid:
        logger.warning("Plugin config {} has no 'id' field, skipping", toml_path)
        return None
    
    # 应用用户配置覆盖。
    #
    # apply_user_overlays=False 是给打包用的：那条路要的是暂存目录里那份
    # plugin.toml 本身，而不是作者这台机器上的运行时配置和激活 profile。带着
    # 作者的私有覆盖导出去，装它的人会拿到一份按别人配置推出来的入口和 schema，
    # 而源码指纹还是对得上的（codex）。
    try:
        if apply_user_overlays and isinstance(conf, dict):
            resolved_conf = resolve_plugin_config_from_path(
                str(pid),
                config_path=toml_path,
                base_config=conf,
                include_effective_config=True,
                validate_schema=True,
            )
            effective = resolved_conf.get("effective_config")
            if isinstance(effective, dict):
                conf = cast(Dict[str, Any], effective)
                # Refresh pdata from post-overlay config so new fields (passive, keywords, etc.) pick up overrides
                pdata = conf.get("plugin") or pdata
            for warning in resolved_conf.get("warnings", []):
                if isinstance(warning, dict):
                    logger.warning(
                        "Plugin config warning [{}] field={} msg={}",
                        warning.get("code"),
                        warning.get("field"),
                        warning.get("message"),
                    )
                elif warning:
                    logger.warning("Plugin config warning: {}", warning)
    except Exception as e:
        logger.warning(
            "Plugin {}: failed to apply user config profile overlay: {}. Using base config only.",
            pid, e,
        )

    plugin_type = pdata.get("type", "plugin")
    if not isinstance(plugin_type, str) or plugin_type not in SUPPORTED_PLUGIN_TYPES:
        logger.error(format_unsupported_plugin_type(plugin_type, plugin_id=pid))
        return None
    
    logger.debug("Plugin ID: {}", pid)
    
    # 检查重复路径
    try:
        resolved_path = toml_path.resolve()
        if str(resolved_path) in processed_paths:
            logger.warning(
                "Plugin config file {} has already been processed in this scan, skipping duplicate",
                toml_path
            )
            return None
        processed_paths.add(str(resolved_path))
    except (OSError, RuntimeError) as e:
        logger.debug("Failed to resolve path for duplicate check: {}", e)
    
    # 验证入口点
    entry = pdata.get("entry")
    if not entry or ":" not in entry:
        logger.warning("Plugin {} has invalid entry point '{}', skipping", pid, entry)
        return None
    entry = normalize_plugin_entry_point(
        str(entry),
        config_path=toml_path,
        builtin_plugin_root=BUILTIN_PLUGIN_CONFIG_ROOT,
    )
    
    logger.debug("Plugin {} entry point: {}", pid, entry)
    
    # 解析运行时配置
    runtime_cfg = conf.get("plugin_runtime")
    enabled_val = True
    auto_start_val = True
    if isinstance(runtime_cfg, dict):
        enabled_val = parse_bool_config(runtime_cfg.get("enabled"), default=True)
        auto_start_val = parse_bool_config(runtime_cfg.get("auto_start"), default=True)

    # 应用用户级运行时开关覆盖（来自 plugin_runtime_overrides.json，
    # 由 plugin manager UI 的 disable/enable 按钮写入；与 manifest 默认值的
    # 关系是 manifest -> profile overlay -> user override，user override 最后生效）
    override = get_runtime_override(str(pid)) if apply_user_overlays else None
    if override is not None and override != enabled_val:
        logger.info(
            "Plugin {} runtime_enabled overridden by user preference: {} -> {}",
            pid,
            enabled_val,
            override,
        )
        enabled_val = override

    auto_start_override = (
        get_runtime_auto_start_override(str(pid)) if apply_user_overlays else None
    )
    if auto_start_override is not None and auto_start_override != auto_start_val:
        logger.info(
            "Plugin {} runtime_auto_start overridden by user preference: {} -> {}",
            pid,
            auto_start_val,
            auto_start_override,
        )
        auto_start_val = auto_start_override

    if not enabled_val:
        logger.info(
            "Plugin {} is disabled by plugin_runtime.enabled=false; will register for visibility only (no runtime load)",
            pid,
        )
    if not auto_start_val:
        logger.info(
            "Plugin {} has plugin_runtime.auto_start=false; treating as manual-start-only (will register metadata but skip auto process start)",
            pid,
        )
    
    # SDK 兼容性检查
    sdk_config = pdata.get("sdk")
    is_compatible, sdk_supported_str, sdk_recommended_str, sdk_untested_str, sdk_conflicts_list = \
        _check_sdk_compatibility(pid, sdk_config, logger)
    
    if not is_compatible:
        return None
    
    # 解析插件间依赖
    dependencies = _parse_plugin_dependencies(conf, logger, pid)
    # 解析 Python 运行时依赖（第三方包）
    python_requirements = _collect_plugin_python_requirements(conf, toml_path, logger, pid)
    python_requirement_paths = _collect_plugin_python_requirement_paths(toml_path)
    
    return PluginContext(
        pid=pid,
        toml_path=toml_path,
        conf=conf,
        pdata=pdata,
        entry=entry,
        dependencies=dependencies,
        sdk_supported_str=sdk_supported_str,
        sdk_recommended_str=sdk_recommended_str,
        sdk_untested_str=sdk_untested_str,
        sdk_conflicts_list=sdk_conflicts_list,
        enabled=enabled_val,
        auto_start=auto_start_val,
        python_requirements=python_requirements,
        python_requirement_paths=python_requirement_paths,
    )


def _collect_plugin_contexts(
    plugin_config_root: Path,
    logger: Any,
) -> tuple[List[PluginContext], Dict[str, PluginContext]]:
    """
    Phase 1: 收集和解析所有插件配置。
    
    Args:
        plugin_config_root: 插件配置根目录
        logger: 日志记录器
    
    Returns:
        (plugin_contexts, pid_to_context)
    """
    found_toml_files = list(plugin_config_root.glob("*/plugin.toml"))
    logger.info("Found {} plugin.toml files: {}", len(found_toml_files), [str(p) for p in found_toml_files])
    
    plugin_contexts: List[PluginContext] = []
    processed_paths: set = set()
    pid_to_context: Dict[str, PluginContext] = {}
    
    for toml_path in found_toml_files:
        try:
            ctx = _parse_single_plugin_config(toml_path, processed_paths, logger)
            if ctx is not None:
                if ctx.pid in pid_to_context:
                    logger.error(
                        "Duplicate plugin id '{}' found in '{}' and '{}'; skipping later config",
                        ctx.pid,
                        pid_to_context[ctx.pid].toml_path,
                        toml_path,
                    )
                    continue
                plugin_contexts.append(ctx)
                pid_to_context[ctx.pid] = ctx
        except Exception:
            logger.exception("Unexpected error processing config {}", toml_path)
            continue
    
    return plugin_contexts, pid_to_context


def _collect_plugin_contexts_from_roots(
    plugin_config_roots: Iterable[Path],
    logger: Any,
) -> tuple[List[PluginContext], Dict[str, PluginContext]]:
    """从多个插件根目录收集插件配置，并在统一命名空间中去重。"""
    plugin_contexts: List[PluginContext] = []
    pid_to_context: Dict[str, PluginContext] = {}
    processed_paths: set[Path] = set()

    for plugin_config_root in plugin_config_roots:
        try:
            root = plugin_config_root.resolve()
        except Exception:
            root = plugin_config_root

        if not root.exists():
            logger.info("No plugin config directory {}, skipping", root)
            continue

        found_toml_files = list(root.glob("*/plugin.toml"))
        logger.info("Found {} plugin.toml files in {}: {}", len(found_toml_files), root, [str(p) for p in found_toml_files])

        for toml_path in found_toml_files:
            try:
                ctx = _parse_single_plugin_config(toml_path, processed_paths, logger)
                if ctx is None:
                    continue
                if ctx.pid in pid_to_context:
                    logger.error(
                        "Duplicate plugin id '{}' found in '{}' and '{}'; skipping later config",
                        ctx.pid,
                        pid_to_context[ctx.pid].toml_path,
                        toml_path,
                    )
                    continue
                plugin_contexts.append(ctx)
                pid_to_context[ctx.pid] = ctx
            except Exception:
                logger.exception("Unexpected error processing config {}", toml_path)
                continue

    return plugin_contexts, pid_to_context


def _prepare_plugin_import_roots(plugin_config_roots: Iterable[Path], logger: Any) -> None:
    """Compatibility hook that intentionally avoids shared plugin roots.

    User plugins are loaded from their selected ``plugin.toml`` directory by
    :func:`plugin.core.host._import_plugin_module`.  Adding a shared root here
    would also expose every unselected sibling as a top-level Python module.
    """

    del plugin_config_roots, logger


def _shutdown_host_safely(host: Any, logger: Any, plugin_id: str) -> None:
    """
    安全关闭插件 host。
    
    Args:
        host: 插件 host 对象
        logger: 日志记录器
        plugin_id: 插件 ID（用于日志）
    """
    import asyncio
    
    try:
        if hasattr(host, "shutdown_sync"):
            host.shutdown_sync(timeout=1.0)
        elif hasattr(host, "shutdown"):
            if asyncio.iscoroutinefunction(host.shutdown):
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(host.shutdown(timeout=1.0))
                    _pending_async_shutdown_tasks.add(task)
                    
                    def _on_done(t: asyncio.Task) -> None:
                        _pending_async_shutdown_tasks.discard(t)
                        try:
                            _ = t.exception()
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                    
                    task.add_done_callback(_on_done)
                    logger.debug("Plugin {} scheduled async shutdown", plugin_id)
                except RuntimeError:
                    asyncio.run(host.shutdown(timeout=1.0))
            else:
                host.shutdown(timeout=1.0)
        elif hasattr(host, "process") and getattr(host, "process", None):
            host.process.terminate()
            host.process.join(timeout=1.0)
    except Exception as e:
        logger.debug("Error shutting down plugin {}: {}", plugin_id, e)


def _migrate_plugin_id(
    old_pid: str,
    new_pid: str,
    host: Any,
    logger: Any,
) -> None:
    """
    迁移插件 ID：更新所有相关映射。
    
    Args:
        old_pid: 原插件 ID
        new_pid: 新插件 ID
        host: 插件 host 对象
        logger: 日志记录器
    """
    logger.warning(
        "Plugin ID changed during registration from '{}' to '{}', updating plugin_hosts",
        old_pid, new_pid
    )
    
    # 更新 plugin_hosts
    with state.acquire_plugin_hosts_write_lock():
        if old_pid in state.plugin_hosts:
            existing_host = state.plugin_hosts.pop(old_pid)
            state.plugin_hosts[new_pid] = existing_host
            if hasattr(existing_host, 'plugin_id'):
                existing_host.plugin_id = new_pid
            logger.info("Plugin host moved from '{}' to '{}' in plugin_hosts", old_pid, new_pid)
        else:
            # old_pid not in plugin_hosts; register the passed-in host under new_pid
            if host is not None:
                state.plugin_hosts[new_pid] = host
                if hasattr(host, 'plugin_id'):
                    host.plugin_id = new_pid
                logger.warning("Plugin host for '{}' not found during migration; registered passed-in host under '{}'", old_pid, new_pid)
            else:
                logger.warning("Plugin host for '{}' not found during migration and passed-in host is None; skipping host registration for '{}'", old_pid, new_pid)
    
    # Migrate downlink sender
    with state._plugin_downlink_senders_lock:
        sender = state._plugin_downlink_senders.pop(old_pid, None)
        if sender is not None:
            state._plugin_downlink_senders[new_pid] = sender

    # 迁移 event handlers
    with state.acquire_event_handlers_write_lock():
        handlers_to_migrate = [
            k for k in list(state.event_handlers.keys())
            if k.startswith(f"{old_pid}.") or k.startswith(f"{old_pid}:")
        ]
        for old_key in handlers_to_migrate:
            if old_key.startswith(f"{old_pid}."):
                new_key = old_key.replace(f"{old_pid}.", f"{new_pid}.", 1)
            else:
                new_key = old_key.replace(f"{old_pid}:", f"{new_pid}:", 1)
            state.event_handlers[new_key] = state.event_handlers.pop(old_key)
    
    # 迁移 plugin_entry_method_map
    for (p, eid), method in list(plugin_entry_method_map.items()):
        if p == old_pid:
            plugin_entry_method_map[(new_pid, eid)] = method
            del plugin_entry_method_map[(p, eid)]


def _load_disabled_plugin(
    ctx: PluginContext,
    logger: Any,
) -> None:
    """
    加载禁用的插件（仅注册元数据，不启动进程）。
    
    Args:
        ctx: 插件上下文
        logger: 日志记录器
    """
    entries_preview = _extract_entries_preview(
        ctx.pid,
        cls=type("DisabledPluginStub", (), {}),
        conf=ctx.conf,
        pdata=ctx.pdata,
    )
    
    plugin_meta = _build_plugin_meta(
        ctx.pid, ctx.pdata,
        sdk_supported_str=ctx.sdk_supported_str,
        sdk_recommended_str=ctx.sdk_recommended_str,
        sdk_untested_str=ctx.sdk_untested_str,
        sdk_conflicts_list=ctx.sdk_conflicts_list,
        dependencies=ctx.dependencies,
        plugin_ui=_extract_plugin_ui_config(ctx.conf, plugin_id=ctx.pid, logger=logger),
    )
    
    resolved_id = register_plugin(
        plugin_meta,
        logger,
        config_path=ctx.toml_path,
        entry_point=ctx.entry,
    )
    
    if resolved_id is not None:
        with state.acquire_plugins_write_lock():
            meta = state.plugins.get(resolved_id)
            if isinstance(meta, dict):
                meta["runtime_enabled"] = False
                meta["runtime_auto_start"] = False
                meta["entries_preview"] = entries_preview
                state.plugins[resolved_id] = meta


def _register_failed_plugin(
    ctx: PluginContext,
    logger: Any,
    *,
    plugin_id: Optional[str],
    entries_preview: Optional[List[Dict[str, Any]]] = None,
    error_type: str,
    error_message: str,
    error_phase: str,
) -> None:
    """
    注册加载失败的插件元数据（仅可见，不可运行）。

    Args:
        ctx: 插件上下文
        logger: 日志记录器
        plugin_id: 注册用插件 ID（允许与 ctx.pid 不同，如冲突重命名后）
        entries_preview: 已提取的入口预览；若未提供则回退到 FailedPluginStub 提取
        error_type: 错误类型
        error_message: 错误信息
        error_phase: 失败阶段（dependency_check/import/start_process 等）
    """
    pid = plugin_id or ctx.pid
    existing_meta: Optional[Dict[str, Any]] = None
    with state.acquire_plugins_read_lock():
        raw_meta = state.plugins.get(pid)
        if isinstance(raw_meta, dict):
            existing_meta = dict(raw_meta)

    same_config_registered = False
    if existing_meta is not None:
        existing_config_path = existing_meta.get("config_path")
        if isinstance(existing_config_path, str) and existing_config_path:
            try:
                same_config_registered = Path(existing_config_path).resolve() == ctx.toml_path.resolve()
            except (OSError, RuntimeError):
                same_config_registered = existing_config_path == str(ctx.toml_path)

    if same_config_registered:
        with state.acquire_plugin_hosts_read_lock():
            existing_host = state.plugin_hosts.get(pid)
        if existing_host is not None and hasattr(existing_host, "is_alive") and existing_host.is_alive():
            logger.info(
                "Plugin {} from {} is already registered and running, skipping duplicate failed registration",
                pid, ctx.toml_path,
            )
            return

    provided_entries_preview = entries_preview
    if provided_entries_preview is None:
        provided_entries_preview = _extract_entries_preview(
            pid,
            cls=type("FailedPluginStub", (), {}),
            conf=ctx.conf,
            pdata=ctx.pdata,
        )

    plugin_meta = _build_plugin_meta(
        pid, ctx.pdata,
        sdk_supported_str=ctx.sdk_supported_str,
        sdk_recommended_str=ctx.sdk_recommended_str,
        sdk_untested_str=ctx.sdk_untested_str,
        sdk_conflicts_list=ctx.sdk_conflicts_list,
        dependencies=ctx.dependencies,
        plugin_ui=_extract_plugin_ui_config(ctx.conf, plugin_id=pid, logger=logger),
    )

    resolved_id = register_plugin(
        plugin_meta,
        logger,
        config_path=ctx.toml_path,
        entry_point=ctx.entry,
    )
    if resolved_id is None:
        return

    with state.acquire_plugins_write_lock():
        meta = state.plugins.get(resolved_id)
        if isinstance(meta, dict):
            meta["runtime_enabled"] = bool(ctx.enabled)
            meta["runtime_auto_start"] = bool(ctx.auto_start)
            meta["runtime_load_state"] = "failed"
            meta["runtime_load_error_type"] = str(error_type or "LoadFailed")
            meta["runtime_load_error_message"] = str(error_message or "Unknown load error")
            meta["runtime_load_error_phase"] = str(error_phase or "unknown")
            meta["runtime_load_error_time"] = datetime.now(timezone.utc).isoformat()
            meta["entries_preview"] = provided_entries_preview
            state.plugins[resolved_id] = meta
    state.invalidate_snapshot_cache("plugins")

    logger.warning(
        "Plugin {} registered as load_failed (phase={}, error_type={}): {}",
        resolved_id,
        error_phase,
        error_type,
        error_message,
    )


def _load_adapter_plugin(
    ctx: PluginContext,
    logger: Any,
    process_host_factory: Callable[..., Any],
    plugin_id: Optional[str] = None,
) -> Optional[Any]:
    """
    加载 Adapter 类型插件。
    
    Adapter 是一种特殊的插件类型，用于：
    1. 作为网关转发外部协议请求到 NEKO 插件
    2. 作为路由器直接处理外部请求
    3. 作为桥接器在不同协议间转换
    
    Adapter 作为独立进程运行，但具有更高的启动优先级。
    
    Args:
        ctx: 插件上下文
        logger: 日志记录器
        process_host_factory: 进程宿主工厂函数
    
    Returns:
        创建的 host 对象，或 None 如果加载失败
    """
    pid = plugin_id or ctx.pid
    conf = ctx.conf
    pdata = ctx.pdata
    toml_path = ctx.toml_path
    entry = ctx.entry
    
    # 解析 adapter 配置
    adapter_conf = ctx.conf.get("adapter", {})
    adapter_mode = adapter_conf.get("mode", "hybrid")
    entries_preview: List[Dict[str, Any]] = []
    scanned_metadata_registered = False
    scanned_class_name: Optional[str] = None
    module_path: Optional[str] = None
    class_name: Optional[str] = None
    
    logger.info(
        "Loading adapter '{}' (mode={})",
        pid, adapter_mode,
    )

    def _rollback_scanned_metadata() -> None:
        nonlocal scanned_metadata_registered
        if not scanned_metadata_registered:
            return
        logger.debug(
            "Adapter {}: removing scanned static metadata for entry='{}' class='{}'",
            pid,
            entry,
            scanned_class_name or class_name or "unknown",
        )
        _remove_scanned_metadata(pid)
        scanned_metadata_registered = False

    try:
        module_path, class_name = entry.split(":", 1)
        mod = _import_plugin_entry_module(module_path, toml_path, logger)
        cls = getattr(mod, class_name)
        if isinstance(cls, type):
            entries_preview = _extract_entries_preview(pid, cls, conf, pdata)
            scan_static_metadata(pid, cls, conf, pdata)
            scanned_metadata_registered = True
            scanned_class_name = cls.__name__
    except Exception:
        logger.debug("Adapter {}: failed to extract entries preview", pid, exc_info=True)
    
    # 构建插件元数据
    plugin_meta = _build_plugin_meta(
        pid, pdata,
        sdk_supported_str=ctx.sdk_supported_str,
        sdk_recommended_str=ctx.sdk_recommended_str,
        sdk_untested_str=ctx.sdk_untested_str,
        sdk_conflicts_list=ctx.sdk_conflicts_list,
        dependencies=ctx.dependencies,
        plugin_ui=_extract_plugin_ui_config(ctx.conf, plugin_id=pid, logger=logger),
    )
    
    # 创建进程宿主
    host = None
    try:
        logger.debug("Adapter {}: creating process host...", pid)
        host = process_host_factory(pid, entry, toml_path)
        logger.info(
            "Adapter {}: process host created successfully (pid: {}, alive: {})",
            pid,
            getattr(host.process, 'pid', 'N/A') if hasattr(host, 'process') and host.process else 'N/A',
            host.process.is_alive() if hasattr(host, 'process') and host.process else False
        )
        
        # 注册到 plugin_hosts
        with state.acquire_plugin_hosts_write_lock():
            state.plugin_hosts[pid] = host
        state.invalidate_snapshot_cache("hosts")
        
    except (OSError, RuntimeError) as e:
        logger.error("Failed to start adapter process for {}: {}", pid, e, exc_info=True)
        _rollback_scanned_metadata()
        _register_failed_plugin(
            ctx,
            logger,
            plugin_id=pid,
            entries_preview=entries_preview,
            error_type=type(e).__name__,
            error_message=str(e),
            error_phase="start_process",
        )
        return None
    except Exception:
        logger.exception("Unexpected error starting adapter process for {}", pid)
        _rollback_scanned_metadata()
        _register_failed_plugin(
            ctx,
            logger,
            plugin_id=pid,
            entries_preview=entries_preview,
            error_type="UnexpectedStartProcessError",
            error_message=f"Unexpected error starting adapter process for {pid}",
            error_phase="start_process",
        )
        return None
    
    # 注册插件元数据
    resolved_id = register_plugin(
        plugin_meta,
        logger,
        config_path=toml_path,
        entry_point=entry,
    )
    
    if resolved_id is None:
        # 重复加载，关闭 host
        if host is not None:
            _shutdown_host_safely(host, logger, pid)
            with state.acquire_plugin_hosts_write_lock():
                state.plugin_hosts.pop(pid, None)
            state.invalidate_snapshot_cache("hosts")
        _remove_scanned_metadata(pid)
        return None
    
    # 更新运行时状态
    with state.acquire_plugins_write_lock():
        meta = state.plugins.get(resolved_id)
        if isinstance(meta, dict):
            meta["runtime_enabled"] = ctx.enabled
            meta["runtime_auto_start"] = ctx.auto_start
            # type 是唯一权威字段；plugin_type 仅做兼容镜像。
            meta["type"] = "adapter"
            meta["plugin_type"] = meta["type"]
            meta["entries_preview"] = entries_preview
            meta["adapter_mode"] = adapter_mode
            state.plugins[resolved_id] = meta
    state.invalidate_snapshot_cache("plugins")

    if resolved_id != pid:
        _migrate_plugin_id(pid, resolved_id, host, logger)
    
    logger.info("Adapter '{}' loaded successfully", resolved_id)
    return host


def _check_plugin_already_loaded(
    pid: str,
    toml_path: Path,
    logger: Any,
) -> bool:
    """
    检查插件是否已经加载。
    
    Args:
        pid: 插件 ID
        toml_path: 配置文件路径
        logger: 日志记录器
    
    Returns:
        True 如果已加载，应跳过
    """
    with state.acquire_plugin_hosts_read_lock():
        if pid in state.plugin_hosts:
            existing_host = state.plugin_hosts[pid]
            existing_config_path = getattr(existing_host, 'config_path', None)
            if existing_config_path:
                try:
                    existing_resolved = Path(existing_config_path).resolve()
                    current_resolved = toml_path.resolve()
                    if existing_resolved == current_resolved:
                        logger.warning(
                            "Plugin {} from {} is already loaded (same config path), skipping duplicate load",
                            pid, toml_path
                        )
                        return True
                except (OSError, RuntimeError):
                    if str(existing_config_path) == str(toml_path):
                        logger.warning(
                            "Plugin {} from {} is already loaded (same config path), skipping duplicate load",
                            pid, toml_path
                        )
                        return True
    return False


def _check_plugin_already_registered(
    pid: str,
    toml_path: Path,
    logger: Any,
) -> bool:
    """
    检查插件是否已注册但未运行。
    
    Args:
        pid: 插件 ID
        toml_path: 配置文件路径
        logger: 日志记录器
    
    Returns:
        True 如果已注册，应跳过
    """
    with state.acquire_plugins_read_lock():
        plugin_already_registered = pid in state.plugins
    
    if plugin_already_registered:
        with state.acquire_plugin_hosts_read_lock():
            if pid in state.plugin_hosts:
                existing_host = state.plugin_hosts[pid]
                if hasattr(existing_host, 'is_alive') and existing_host.is_alive():
                    logger.info(
                        "Plugin {} from {} is already registered and running, skipping duplicate load",
                        pid, toml_path
                    )
                    return True
                else:
                    logger.info(
                        "Plugin {} from {} is already registered but not running, skipping duplicate load",
                        pid, toml_path
                    )
                    return True
            else:
                logger.warning(
                    "Plugin {} from {} is already registered in state.plugins but has no host in plugin_hosts. "
                    "This indicates the plugin was registered but the host creation was skipped or failed. "
                    "Please start the plugin manually via POST /plugin/{}/start",
                    pid, toml_path, pid
                )
                return True
    return False


def load_plugins_from_roots(
    plugin_config_roots: Iterable[Path],
    logger: Any,
    process_host_factory: Callable[..., Any],
) -> None:
    """
    扫描插件配置，启动子进程，并静态扫描元数据用于注册列表。
    process_host_factory 接收 (plugin_id, entry_point, config_path) 并返回宿主对象。
    
    加载过程分为三个阶段：
    1. 收集（Collect）：扫描所有 TOML 文件，解析配置和依赖。
    2. 排序（Sort）：根据插件依赖关系进行拓扑排序，确保依赖先加载。
    3. 加载（Load）：按顺序执行实际加载。
    """
    logger = _wrap_logger(logger)
    roots: list[Path] = []
    for plugin_config_root in plugin_config_roots:
        try:
            root = plugin_config_root.resolve()
        except Exception:
            root = plugin_config_root
        if root not in roots:
            roots.append(root)

    if not roots:
        logger.info("No plugin config roots provided, skipping")
        return

    logger.info("Loading plugins from roots: {}", [str(root) for root in roots])

    # plugin.toml 使用 ``plugin.plugins.xxx`` 作为规范入口；用户安装插件运行时会
    # 通过 normalize_plugin_entry_point 映射到顶层 ``plugins.xxx`` 导入命名空间。
    _prepare_plugin_import_roots(roots, logger)
    logger.info("Current working directory: {}", os.getcwd())
    logger.info("Python path (first 3): {}", sys.path[:3])
    
    # === Phase 1: Collect and Parse ===
    plugin_contexts, pid_to_context = _collect_plugin_contexts_from_roots(roots, logger)
    
    if not plugin_contexts:
        logger.info("No valid plugins found to load")
        return
    
    # === Phase 2: Topological Sort ===
    final_order = _topological_sort_plugins(plugin_contexts, pid_to_context, logger)
    
    # === Phase 3: Load ===
    # 加载每个插件
    for pid in final_order:
        ctx = pid_to_context.get(pid)
        if not ctx:
            continue
            
        toml_path = ctx.toml_path
        conf = ctx.conf
        pdata = ctx.pdata
        entry = ctx.entry
        dependencies = ctx.dependencies
        sdk_supported_str = ctx.sdk_supported_str
        sdk_recommended_str = ctx.sdk_recommended_str
        sdk_untested_str = ctx.sdk_untested_str
        sdk_conflicts_list = ctx.sdk_conflicts_list
        enabled_val = ctx.enabled
        auto_start_val = ctx.auto_start
        
        logger.info("Loading plugin: {}", pid)

        # disabled plugins: visibility only
        if not enabled_val:
            _load_disabled_plugin(ctx, logger)
            continue
        # 根据插件类型分发加载逻辑
        plugin_type = pdata.get("type", "plugin")
        
        # 依赖检查（可通过配置禁用）
        dependency_check_failed = False
        dependency_check_error: Optional[str] = None
        if PLUGIN_ENABLE_DEPENDENCY_CHECK and dependencies:
            logger.debug("Plugin {}: checking {} dependency(ies)...", pid, len(dependencies))
            for dep in dependencies:
                # 检查依赖（包括简化格式和完整格式）
                satisfied, error_msg = _check_plugin_dependency(dep, logger, pid)
                if not satisfied:
                    logger.error(
                        "Plugin {}: dependency check failed: {}; skipping load",
                        pid, error_msg
                    )
                    dependency_check_failed = True
                    dependency_check_error = str(error_msg) if error_msg else "dependency check failed"
                    break
                logger.debug("Plugin {}: dependency '{}' check passed", pid, getattr(dep, 'id', getattr(dep, 'entry', getattr(dep, 'custom_event', 'unknown'))))
            if not dependency_check_failed:
                logger.debug("Plugin {}: all dependencies satisfied", pid)
        elif not PLUGIN_ENABLE_DEPENDENCY_CHECK and dependencies:
            logger.warning(
                "Plugin {}: has {} dependency(ies), but dependency check is disabled. "
                "Loading plugin without dependency validation.",
                pid, len(dependencies)
            )
        else:
            logger.debug("Plugin {}: no dependencies to check", pid)
        
        if dependency_check_failed:
            logger.debug("Plugin {}: skipping due to failed dependency check", pid)
            _register_failed_plugin(
                ctx,
                logger,
                plugin_id=pid,
                error_type="DependencyCheckFailed",
                error_message=dependency_check_error or "Plugin dependency check failed",
                error_phase="dependency_check",
            )
            continue

        unsatisfied_python_requirements = _find_missing_python_requirements(
            ctx.python_requirements,
            search_paths=ctx.python_requirement_paths,
        )
        if unsatisfied_python_requirements:
            logger.error(
                "Plugin {}: unsatisfied Python dependencies: {}. "
                "Please install them into this plugin's vendor/ directory "
                "(declared in pyproject.toml [project].dependencies or legacy requirements.txt).",
                pid,
                unsatisfied_python_requirements,
            )
            _register_failed_plugin(
                ctx,
                logger,
                plugin_id=pid,
                error_type="MissingPythonDependencies",
                error_message=f"Unsatisfied Python dependencies: {unsatisfied_python_requirements}",
                error_phase="python_requirements",
            )
            continue

        # 检查插件是否已经加载
        if _check_plugin_already_loaded(pid, toml_path, logger):
            continue
        
        # 检测并解决插件 ID 冲突
        plugin_data_for_hash = {
            "id": pid,
            "name": pdata.get("name", pid),
            "version": pdata.get("version", "0.1.0"),
            "entry": entry or "",
        }
        original_pid = pid
        resolved_pid = _resolve_plugin_id_conflict(
            pid, logger,
            config_path=toml_path,
            entry_point=entry,
            plugin_data=plugin_data_for_hash,
            purpose="load",
            enable_rename=bool(PLUGIN_ENABLE_ID_CONFLICT_CHECK),
        )

        if resolved_pid is None:
            logger.info("Plugin {} from {} is already loaded (duplicate detected), skipping", original_pid, toml_path)
            continue

        pid = resolved_pid
        if pid != original_pid:
            logger.warning("Plugin {} from {}: ID changed from '{}' to '{}' due to conflict", original_pid, toml_path, original_pid, pid)

        # 检查插件是否已注册
        if _check_plugin_already_registered(pid, toml_path, logger):
            continue

        _ensure_python_requirement_paths(ctx.python_requirement_paths, logger, pid)

        # adapter 类型：在通过统一依赖和重复检查后，再走 adapter-specific 启动逻辑
        if plugin_type == "adapter":
            _load_adapter_plugin(ctx, logger, process_host_factory, plugin_id=pid)
            continue

        module_path, class_name = entry.split(":", 1)
        logger.debug("Plugin {}: importing {}:{}", pid, module_path, class_name)
        try:
            mod = _import_plugin_entry_module(module_path, toml_path, logger)
            cls: Type[Any] = getattr(mod, class_name)
        except (ImportError, ModuleNotFoundError) as e:
            logger.error("Failed to import module '{}' for plugin {}: {}", module_path, pid, e, exc_info=True)
            _register_failed_plugin(
                ctx,
                logger,
                plugin_id=pid,
                error_type=type(e).__name__,
                error_message=str(e),
                error_phase="import_module",
            )
            continue
        except AttributeError as e:
            logger.error("Class '{}' not found in module '{}' for plugin {}: {}", class_name, module_path, pid, e, exc_info=True)
            _register_failed_plugin(
                ctx,
                logger,
                plugin_id=pid,
                error_type="AttributeError",
                error_message=f"Class '{class_name}' not found in module '{module_path}'",
                error_phase="import_class",
            )
            continue
        except Exception:
            logger.exception("Unexpected error importing plugin class {} for plugin {}", entry, pid)
            _register_failed_plugin(
                ctx,
                logger,
                plugin_id=pid,
                error_type="UnexpectedImportError",
                error_message=f"Unexpected error importing plugin class {entry}",
                error_phase="import_module",
            )
            continue

        host = None
        if enabled_val and auto_start_val:
            try:
                logger.debug("Plugin {}: creating process host...", pid)
                host = process_host_factory(pid, entry, toml_path)
                logger.info(
                    "Plugin {}: process host created successfully (pid: {}, alive: {})",
                    pid,
                    getattr(host.process, 'pid', 'N/A') if hasattr(host, 'process') and host.process else 'N/A',
                    host.process.is_alive() if hasattr(host, 'process') and host.process else False
                )
                
                # 如果 ID 被重命名，更新 host 的 plugin_id（如果支持）
                if pid != original_pid and hasattr(host, 'plugin_id'):
                    host.plugin_id = pid
                    logger.debug("Updated host plugin_id to '{}'", pid)
                
                skip_register = False
                with state.acquire_plugin_hosts_write_lock():
                    # 检查是否已经存在（防止重复注册）
                    if pid in state.plugin_hosts:
                        existing_host = state.plugin_hosts[pid]
                        existing_config = getattr(existing_host, 'config_path', None)
                        if existing_config:
                            try:
                                if Path(existing_config).resolve() == toml_path.resolve():
                                    logger.warning(
                                        "Plugin {} from {} is already registered in plugin_hosts, skipping duplicate registration",
                                        pid, toml_path
                                    )
                                    skip_register = True
                            except (OSError, RuntimeError):
                                pass

                    if not skip_register:
                        # 注册 host
                        state.plugin_hosts[pid] = host
                        # 立即验证注册是否成功
                        registered_keys = list(state.plugin_hosts.keys())
                        logger.info(
                            "Plugin {}: registered in plugin_hosts. Current plugin_hosts keys: {}",
                            pid, registered_keys
                        )
                        # 在同一个锁内验证 host 是否还在（防止在注册后立即被其他代码移除）
                        if pid not in state.plugin_hosts:
                            logger.error(
                                "Plugin {} host was removed from plugin_hosts immediately after registration! "
                                "This should not happen. Current plugin_hosts keys: {}. "
                                "Re-registering host to continue...",
                                pid, list(state.plugin_hosts.keys())
                            )
                            # 重新注册 host（可能是被意外清空了）
                            state.plugin_hosts[pid] = host
                            logger.debug("Plugin {}: re-registered in plugin_hosts", pid)

                if skip_register:
                    _shutdown_host_safely(host, logger, pid)
                    continue
            except (OSError, RuntimeError) as e:
                logger.error("Failed to start process for plugin {}: {}", pid, e, exc_info=True)
                _register_failed_plugin(
                    ctx,
                    logger,
                    plugin_id=pid,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    error_phase="start_process",
                )
                continue
            except Exception:
                logger.exception("Unexpected error starting process for plugin {}", pid)
                _register_failed_plugin(
                    ctx,
                    logger,
                    plugin_id=pid,
                    error_type="UnexpectedStartProcessError",
                    error_message=f"Unexpected error starting process for plugin {pid}",
                    error_phase="start_process",
                )
                continue

        scan_static_metadata(pid, cls, conf, pdata)

        plugin_meta = _build_plugin_meta(
            pid, pdata,
            sdk_supported_str=sdk_supported_str,
            sdk_recommended_str=sdk_recommended_str,
            sdk_untested_str=sdk_untested_str,
            sdk_conflicts_list=sdk_conflicts_list,
            dependencies=dependencies,
            input_schema=getattr(cls, "input_schema", {}) or {"type": "object", "properties": {}},
            plugin_ui=_extract_plugin_ui_config(conf, plugin_id=pid, logger=logger),
        )
        
        # 在调用 register_plugin 之前，验证 host 是否还在 plugin_hosts 中。
        # 对于 manual-start-only 插件（auto_start=false），host 允许为 None，此时不应要求在 plugin_hosts 中存在。
        host_still_exists = False
        if host is not None:
            with state.acquire_plugin_hosts_read_lock():
                host_still_exists = pid in state.plugin_hosts
                if not host_still_exists:
                    logger.error(
                        "Plugin {} host was removed from plugin_hosts before register_plugin call! "
                        "This should not happen. Current plugin_hosts keys: {}",
                        pid, list(state.plugin_hosts.keys())
                    )
        
        resolved_id = register_plugin(
            plugin_meta,
            logger,
            config_path=toml_path,
            entry_point=entry
        )

        # Mark runtime flags for dependency/conflict filtering.
        if resolved_id is not None:
            with state.acquire_plugins_write_lock():
                meta = state.plugins.get(resolved_id)
                if isinstance(meta, dict):
                    meta["runtime_enabled"] = True
                    meta["runtime_auto_start"] = bool(auto_start_val)
                    state.plugins[resolved_id] = meta
        
        logger.debug(
            "Plugin {}: register_plugin returned resolved_id={}, original pid={}",
            pid, resolved_id, pid
        )
        
        # 验证 register_plugin 调用后 host 是否还在
        if host is not None:
            with state.acquire_plugin_hosts_read_lock():
                host_after_register = pid in state.plugin_hosts
                all_keys_after = list(state.plugin_hosts.keys())
                if host_still_exists and not host_after_register:
                    logger.error(
                        "Plugin {} host was removed from plugin_hosts during register_plugin call! "
                        "resolved_id={}, host_still_exists={}, host_after_register={}, "
                        "Current plugin_hosts keys: {}",
                        pid, resolved_id, host_still_exists, host_after_register, all_keys_after
                    )
                elif host_still_exists and host_after_register:
                    logger.debug(
                        "Plugin {} host still exists in plugin_hosts after register_plugin (resolved_id={})",
                        pid, resolved_id
                    )
        
        # 如果 register_plugin 返回 None，说明这是重复加载
        if resolved_id is None:
            logger.warning("Plugin {} from {} detected as duplicate in register_plugin, removing from plugin_hosts", pid, toml_path)
            existing_host = None
            with state.acquire_plugin_hosts_write_lock():
                if pid in state.plugin_hosts:
                    existing_host = state.plugin_hosts.pop(pid)
            if existing_host is not None:
                _shutdown_host_safely(existing_host, logger, pid)
            _remove_scanned_metadata(pid)
            logger.debug("Plugin {} removed from plugin_hosts due to duplicate detection", pid)
            continue
        
        # 如果 ID 被进一步重命名，迁移所有相关映射
        if resolved_id != pid:
            _migrate_plugin_id(pid, resolved_id, host, logger)
            pid = resolved_id

        logger.info("Loaded plugin {} (Process: {})", pid, getattr(host, "process", None))
        try:
            from plugin.server.messaging.lifecycle_events import emit_lifecycle_event
            from plugin.server.infrastructure.utils import now_iso

            emit_lifecycle_event({"type": "plugin_loaded", "plugin_id": pid, "time": now_iso()})
        except Exception:
            logger.debug("Failed to enqueue lifecycle event for plugin {}", pid, exc_info=True)


def load_plugins_from_toml(
    plugin_config_root: Path,
    logger: Any,
    process_host_factory: Callable[..., Any],
) -> None:
    """兼容旧调用：从单个插件根目录加载。"""
    load_plugins_from_roots((plugin_config_root,), logger, process_host_factory)
