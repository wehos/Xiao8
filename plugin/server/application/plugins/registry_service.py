from __future__ import annotations

import asyncio
import re
import threading
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from plugin.core.dependency import _topological_sort_plugins
from plugin.core.entry_points import describe_plugin_entry_directory_mismatch
from plugin.core.registry import (
    PluginContext,
    _build_plugin_meta,
    _check_plugin_dependency,
    _extract_entries_preview,
    _extract_plugin_ui_config,
    _find_missing_python_requirements,
    _parse_single_plugin_config,
    _prepare_plugin_import_roots,
    _resolve_plugin_id_conflict,
    register_plugin,
)
from plugin.server.infrastructure.autostart_approvals import (
    clear_autostart_pending,
    is_autostart_approved,
    mark_autostart_pending,
)
from plugin.server.infrastructure.packaged_metadata import (
    PLACEHOLDER_INPUT_SCHEMA,
    PackagedPluginMetadata,
    entries_config_digest,
    read_packaged_metadata,
)
from plugin.core.state import state
from plugin.logging_config import get_logger
from plugin.server.domain.errors import ServerDomainError
from plugin.settings import BUILTIN_PLUGIN_CONFIG_ROOT, PLUGIN_CONFIG_ROOTS

logger = get_logger("server.application.plugins.registry")
_PLUGIN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

_MANAGED_META_KEYS = {
    "id",
    "name",
    "type",
    "plugin_type",
    "description",
    "short_description",
    "keywords",
    "passive",
    "version",
    "sdk_version",
    "sdk_recommended",
    "sdk_supported",
    "sdk_untested",
    "sdk_conflicts",
    "input_schema",
    "author",
    "dependencies",
    "i18n",
    "plugin_ui",
    "config_path",
    "entry_point",
    "runtime_enabled",
    "runtime_auto_start",
    "runtime_load_state",
    "runtime_load_error_type",
    "runtime_load_error_message",
    "runtime_load_error_phase",
    "entries_preview",
    "adapter_mode",
    "runtime_source_missing",
    "source",
    "effective_source",
    "builtin_version",
    "shadowed_builtin_path",
}


@dataclass(slots=True)
class PluginDiscoveryRecord:
    plugin_id: str
    original_plugin_id: str
    config_path: Path
    entry_point: str
    plugin_type: str
    enabled: bool
    auto_start: bool
    meta_payload: dict[str, object]


@dataclass(slots=True)
class PluginDiscoveryFailure:
    plugin_id: str | None
    config_path: Path
    error: str


@dataclass(slots=True)
class PluginDiscoverySnapshot:
    records: list[PluginDiscoveryRecord]
    failures: list[PluginDiscoveryFailure]
    config_paths: set[Path]
    shadowed: list[PluginDiscoveryRecord]


def _get_registered_plugin_snapshot_sync() -> dict[str, dict[str, object]]:
    with state.acquire_plugins_read_lock():
        snapshot: dict[str, dict[str, object]] = {}
        for plugin_id, meta in state.plugins.items():
            if isinstance(plugin_id, str) and isinstance(meta, dict):
                snapshot[plugin_id] = dict(meta)
        return snapshot


def _list_running_plugin_ids_sync() -> set[str]:
    running: set[str] = set()
    with state.acquire_plugin_hosts_read_lock():
        for plugin_id, host_obj in state.plugin_hosts.items():
            if not isinstance(plugin_id, str):
                continue
            try:
                if hasattr(host_obj, "is_alive") and host_obj.is_alive():
                    running.add(plugin_id)
            except Exception:
                continue
    return running


def _remap_entries_preview_plugin_id(
    entries_preview: list[dict[str, object]],
    *,
    plugin_id: str,
) -> list[dict[str, object]]:
    remapped: list[dict[str, object]] = []
    for item in entries_preview:
        entry_copy = dict(item)
        entry_id_obj = entry_copy.get("id")
        if isinstance(entry_id_obj, str) and entry_id_obj:
            entry_copy["event_key"] = f"{plugin_id}.{entry_id_obj}"
        remapped.append(entry_copy)
    return remapped


def _select_managed_fields(meta: dict[str, object]) -> dict[str, object]:
    return {
        key: meta[key]
        for key in _MANAGED_META_KEYS
        if key in meta
    }


def _find_plugin_config_path(plugin_id: str, roots: tuple[Path, ...]) -> Path | None:
    normalized_plugin_id = plugin_id.strip()
    if not _PLUGIN_ID_PATTERN.fullmatch(normalized_plugin_id):
        return None

    # Roots are declared in effective-source priority order (user, builtin).
    for root in roots:
        resolved_root = root.resolve()
        config_file = (resolved_root / normalized_plugin_id / "plugin.toml").resolve()
        if resolved_root not in config_file.parents:
            continue
        if config_file.exists():
            return config_file
    return None


def _source_for_config_path(config_path: Path) -> str:
    builtin_root = _resolve_config_path(BUILTIN_PLUGIN_CONFIG_ROOT)
    return "builtin" if config_path.parent.parent == builtin_root else "user"


def _select_effective_records(
    records: list[PluginDiscoveryRecord],
    roots: tuple[Path, ...],
) -> tuple[list[PluginDiscoveryRecord], list[PluginDiscoveryRecord]]:
    """Apply the sole supported same-ID source precedence rule.

    Only canonical ``<root>/<id>/plugin.toml`` installations across distinct
    roots form a builtin/user override. Other duplicate declarations remain
    real conflicts and continue through the legacy ``_1`` resolution path.
    """
    grouped: dict[str, list[PluginDiscoveryRecord]] = {}
    order: list[str] = []
    for record in records:
        if record.plugin_id not in grouped:
            grouped[record.plugin_id] = []
            order.append(record.plugin_id)
        grouped[record.plugin_id].append(record)

    selected: list[PluginDiscoveryRecord] = []
    shadowed: list[PluginDiscoveryRecord] = []
    for plugin_id in order:
        group = grouped[plugin_id]
        canonical = [record for record in group if record.config_path.parent.name == plugin_id]
        sources = {_source_for_config_path(record.config_path) for record in canonical}
        if not {"builtin", "user"}.issubset(sources):
            # This is a real legacy ID conflict, not a supported source
            # override. Preserve the historical builtin-first winner even
            # though discovery roots are now ordered user-first.
            winners = sorted(
                group,
                key=lambda record: _source_for_config_path(record.config_path) != "builtin",
            )
            hidden: list[PluginDiscoveryRecord] = []
        else:
            winners = sorted(
                (
                    record
                    for record in group
                    if record not in canonical
                    or _source_for_config_path(record.config_path) == "user"
                ),
                key=lambda record: record not in canonical,
            )
            hidden = [record for record in canonical if record not in winners]

        builtin_hidden = next(
            (record for record in hidden if _source_for_config_path(record.config_path) == "builtin"),
            None,
        )
        for record in winners:
            source = _source_for_config_path(record.config_path)
            record.meta_payload["source"] = source
            record.meta_payload["effective_source"] = source
            if source == "builtin":
                record.meta_payload["builtin_version"] = str(record.meta_payload.get("version", ""))
            elif builtin_hidden is not None and record in canonical:
                record.meta_payload["builtin_version"] = str(
                    builtin_hidden.meta_payload.get("version", "")
                )
                record.meta_payload["shadowed_builtin_path"] = str(builtin_hidden.config_path)
        selected.extend(winners)
        shadowed.extend(hidden)
    return selected, shadowed


def _resolve_meta_config_path(meta: dict[str, object] | None) -> Path | None:
    if not isinstance(meta, dict):
        return None

    config_path_obj = meta.get("config_path")
    if not isinstance(config_path_obj, str) or not config_path_obj:
        return None

    try:
        return Path(config_path_obj).resolve()
    except Exception:
        return Path(config_path_obj)


def _resolve_config_path(path: Path) -> Path:
    try:
        return path.resolve()
    except Exception:
        return path


def _config_path_belongs_to_roots(config_path: Path, roots: tuple[Path, ...]) -> bool:
    resolved_path = _resolve_config_path(config_path)
    return any(
        _resolve_config_path(root) in resolved_path.parents
        for root in roots
    )


def _find_existing_runtime_plugin_id_by_config_path(
    config_path: Path,
    existing_snapshot: dict[str, dict[str, object]],
) -> str | None:
    resolved_config_path = _resolve_config_path(config_path)
    for plugin_id, meta in existing_snapshot.items():
        meta_config_path = _resolve_meta_config_path(meta)
        if meta_config_path is not None and meta_config_path == resolved_config_path:
            return plugin_id
    return None


def _declared_id_taken_by_another_plugin(
    declared_plugin_id: str, config_path: Path
) -> bool:
    """Whether some *other* plugin is live under ``declared_plugin_id`` right now.

    Reads the registry rather than the refresh's opening snapshot. That snapshot
    is taken once per round and never updated, so two plugins declaring the same
    id and first seen in the same round both looked unclaimed — the second one's
    gate move would then take the first one's record and hand a never-started
    plugin its autostart back (coderabbit).

    "Other" is by config path: a plugin re-registering under its own id is not
    competing with itself.
    """
    if not declared_plugin_id:
        return False
    resolved = _resolve_config_path(config_path)
    with state.acquire_plugins_read_lock():
        meta = state.plugins.get(declared_plugin_id)
        if not isinstance(meta, dict):
            return False
        owner = _resolve_meta_config_path(meta)
    return owner is not None and owner != resolved


def _collect_plugin_contexts_from_roots_sync(
    roots: tuple[Path, ...],
) -> tuple[list[PluginContext], dict[str, PluginContext]]:
    # Dependency ordering must use the same effective source as registration.
    candidates: dict[str, list[tuple[PluginContext, str, bool]]] = {}
    pid_to_context: dict[str, PluginContext] = {}
    context_order: list[str] = []
    processed_paths: set[Path] = set()

    for root in roots:
        try:
            resolved_root = root.resolve()
        except Exception:
            resolved_root = root

        if not resolved_root.exists():
            continue

        for config_path in sorted(resolved_root.glob("*/plugin.toml")):
            if config_path.parent.name.startswith("."):
                continue
            try:
                ctx = _parse_single_plugin_config(config_path, processed_paths, logger)
            except Exception as exc:
                logger.debug(
                    "plugin context collection skipped failed config {}: err_type={}, err={}",
                    config_path,
                    type(exc).__name__,
                    str(exc),
                )
                continue

            if ctx is None:
                continue
            if ctx.pid not in candidates:
                context_order.append(ctx.pid)
            candidates.setdefault(ctx.pid, []).append(
                (
                    ctx,
                    _source_for_config_path(config_path),
                    config_path.parent.name == ctx.pid,
                )
            )

    for plugin_id in context_order:
        group = candidates[plugin_id]
        canonical_user = next(
            (ctx for ctx, source, canonical in group if canonical and source == "user"),
            None,
        )
        canonical_builtin = next(
            (ctx for ctx, source, canonical in group if canonical and source == "builtin"),
            None,
        )
        if canonical_user is not None and canonical_builtin is not None:
            winner = canonical_user
        else:
            winner = next(
                (ctx for ctx, source, _canonical in group if source == "builtin"),
                group[0][0],
            )
        pid_to_context[plugin_id] = winner
        for ctx, _source, _canonical in group:
            if ctx is winner:
                continue
            logger.debug(
                "duplicate plugin id '{}' ignored while building runtime plan",
                plugin_id,
            )

    plugin_contexts = [pid_to_context[plugin_id] for plugin_id in context_order]
    return plugin_contexts, pid_to_context


def _build_ordered_plugin_ids_sync(candidate_plugin_ids: set[str] | None = None) -> list[str]:
    roots = tuple(PLUGIN_CONFIG_ROOTS)
    plugin_contexts, pid_to_context = _collect_plugin_contexts_from_roots_sync(roots)
    registered_snapshot = _get_registered_plugin_snapshot_sync()
    if not registered_snapshot:
        return []

    target_ids = set(candidate_plugin_ids) if candidate_plugin_ids is not None else set(registered_snapshot.keys())
    if not target_ids:
        return []

    config_path_to_plugin_id: dict[Path, str] = {}
    for plugin_id, meta in registered_snapshot.items():
        resolved_config_path = _resolve_meta_config_path(meta)
        if resolved_config_path is not None:
            config_path_to_plugin_id[resolved_config_path] = plugin_id

    ordered: list[str] = []
    seen: set[str] = set()
    if plugin_contexts:
        for declared_plugin_id in _topological_sort_plugins(plugin_contexts, pid_to_context, logger):
            ctx = pid_to_context.get(declared_plugin_id)
            if ctx is None:
                continue

            try:
                ctx_config_path = ctx.toml_path.resolve()
            except Exception:
                ctx_config_path = ctx.toml_path
            runtime_plugin_id = config_path_to_plugin_id.get(ctx_config_path, declared_plugin_id)
            if runtime_plugin_id not in target_ids or runtime_plugin_id in seen:
                continue
            if runtime_plugin_id not in registered_snapshot:
                continue
            ordered.append(runtime_plugin_id)
            seen.add(runtime_plugin_id)

    for plugin_id in sorted(target_ids):
        if plugin_id in seen or plugin_id not in registered_snapshot:
            continue
        ordered.append(plugin_id)
        seen.add(plugin_id)

    return ordered


# 注册表刷新的互斥锁。
#
# 这里原本是一套"票号排序"：每次刷新开工前领号、发布前认号、号旧的整轮作废，外加
# 按插件的号表、两张缓存盲区表和一个事务屏障，一共七个全局量。它存在的唯一理由是
# 两次刷新可能重叠而完成顺序不定；而重叠之所以从偶然变成常态，是因为一次命中缓存
# 的刷新 0.14s、一次冷扫描 3.3s，后开始的经常先结束。
#
# 刷新不再导入任何插件（只读盘上的 plugin.meta.json）之后，整轮刷新是毫秒级的纯
# 读，那个不对称消失了。于是换回最朴素的做法：整段刷新互斥。从票号排序里长出来的
# 那些缺陷——空手而归的 force 仍享最高优先级、carry-forward 拿的是开工前的快照、
# force 不让位于真扫完的普通刷新、单插件刷新的目标分不到扫描预算——全部随之消失，
# 因为它们都是"两次刷新重叠"的衍生物，不是各自独立的 bug。
#
# 可重入：refresh_plugin 和 refresh_registry 都在这把锁里跑，而安装类事务会先后
# 调到它们两个。
_REGISTRY_REFRESH_LOCK = threading.RLock()


def _build_discovery_record_safely(
    config_path: Path,
    ctx: PluginContext,
) -> tuple[PluginDiscoveryRecord | None, PluginDiscoveryFailure | None]:
    """Build one record, turning any failure into a value.

    Returned rather than raised because discovery order is load-bearing
    downstream: ``_select_effective_records`` builds its group ordering from
    first appearance, so one bad plugin must not shift the others.
    """
    try:
        return _build_discovery_record_from_context(ctx), None
    except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop discovery
        logger.warning(
            "plugin discovery payload failed for {}: err_type={}, err={}",
            config_path,
            type(exc).__name__,
            str(exc),
        )
        return None, PluginDiscoveryFailure(
            plugin_id=ctx.pid or config_path.parent.name or None,
            config_path=config_path,
            error=str(exc),
        )


def _discover_registry_snapshot_sync(
    roots: tuple[Path, ...],
) -> PluginDiscoverySnapshot:
    processed_paths: set[Path] = set()
    pending: list[tuple[Path, PluginContext]] = []
    records: list[PluginDiscoveryRecord] = []
    failures: list[PluginDiscoveryFailure] = []
    config_paths: set[Path] = set()

    for root in roots:
        try:
            resolved_root = root.resolve()
        except Exception:
            resolved_root = root

        if not resolved_root.exists():
            logger.info("No plugin config directory {}, skipping", resolved_root)
            continue

        found_toml_files = [
            path
            for path in sorted(resolved_root.glob("*/plugin.toml"))
            if not path.parent.name.startswith(".")
        ]
        logger.info(
            "Found {} plugin.toml files in {}: {}",
            len(found_toml_files),
            resolved_root,
            [str(path) for path in found_toml_files],
        )

        for config_path in found_toml_files:
            config_paths.add(config_path.resolve())
            try:
                ctx = _parse_single_plugin_config(config_path, processed_paths, logger)
            except Exception as exc:
                logger.warning(
                    "plugin discovery failed for {}: err_type={}, err={}",
                    config_path,
                    type(exc).__name__,
                    str(exc),
                )
                failures.append(
                    PluginDiscoveryFailure(
                        plugin_id=config_path.parent.name or None,
                        config_path=config_path,
                        error=str(exc),
                    )
                )
                continue

            if ctx is None:
                failures.append(
                    PluginDiscoveryFailure(
                        plugin_id=config_path.parent.name or None,
                        config_path=config_path,
                        error="plugin config could not be parsed or validated",
                    )
                )
                continue

            pending.append((config_path, ctx))

    # 曾经这里是一个线程池，因为每个插件都要起一个子进程 import 它。现在每一项
    # 都只是读一份 JSON，串行走完就行——并行化一堆文件读没有意义，而线程池连带
    # 需要总预算、单项超时、以及"预算耗尽算不算插件坏了"那一整套判据。
    for config_path, ctx in pending:
        record, failure = _build_discovery_record_safely(config_path, ctx)
        if record is not None:
            records.append(record)
        elif failure is not None:
            failures.append(failure)

    effective_records, shadowed = _select_effective_records(records, roots)
    return PluginDiscoverySnapshot(
        records=effective_records,
        failures=failures,
        config_paths={_resolve_config_path(record.config_path) for record in effective_records},
        shadowed=shadowed,
    )


def _normalize_entry_input_schema(entry: Mapping[str, object]) -> dict[str, object]:
    """Make "we do not know the parameters" explicit instead of an empty dict.

    ⚠️ The placeholder must not carry a ``properties`` key, not even an empty
    one. The plugin manager decides whether to render a generated form with
    ``!!(schema?.properties && typeof schema.properties === 'object')`` and
    ``!!{}`` is true in JavaScript, so an empty ``properties`` renders a form
    with zero fields, submits ``{}``, and takes away the raw-JSON box the user
    would otherwise get. An entry that really takes no parameters keeps the
    ``properties: {}`` the packager derived and renders that empty form
    correctly — the two cases must stay distinguishable.
    """
    result = dict(entry)
    schema = result.get("input_schema")
    if isinstance(schema, Mapping) and "properties" in schema:
        return result
    result["input_schema"] = dict(PLACEHOLDER_INPUT_SCHEMA)
    return result


def config_overrides_packaged_entries(
    conf: object, pdata: object, packaged: PackagedPluginMetadata
) -> bool:
    """Whether this machine's configuration changed the plugin's entry table.

    Packaging reads the staged ``plugin.toml``; it cannot see the user's runtime
    configuration or the profile they activated. When those change ``entries``,
    the packaged list describes a different plugin than the one this machine
    would run (codex).

    The comparison is against what the package was built from, not against the
    mere presence of a table. Presence was wrong in both directions: a plugin
    that declares ``entries`` in its own manifest looked permanently overridden
    and lost its build-time schemas, and an overlay setting ``entries = []`` to
    remove them looked like no overlay at all (codex).

    Lives here rather than in the lifecycle service because both the discovery
    preview and the start path need it, and the import only goes one way.
    """
    return entries_config_digest(conf, pdata) != packaged.entries_config_sha256


def _packaged_entries_preview(
    ctx: PluginContext, plugin_id: str
) -> list[dict[str, object]]:
    """One plugin's entry previews, read off disk — never by importing it.

    Preference order: the schema derived on the author's machine at packaging
    time, then whatever the manifest declares statically, then a placeholder
    that says "unknown" rather than "none".
    """
    packaged = read_packaged_metadata(ctx.toml_path.parent)
    if (
        packaged is not None
        and packaged.entries
        and not config_overrides_packaged_entries(ctx.conf, ctx.pdata, packaged)
    ):
        return [_normalize_entry_input_schema(entry) for entry in packaged.entries]
    # 没有打包期元数据时，manifest 里静态声明的 entries 仍是一条完整通路——它只是
    # 拿不到从处理函数签名推出来的那部分 input_schema。这条通路一直都在：禁用的
    # 插件走的就是它。
    declared = _extract_entries_preview(
        plugin_id,
        cls=type("UnscannedPluginStub", (), {}),
        conf=ctx.conf,
        pdata=ctx.pdata,
    )
    return [_normalize_entry_input_schema(entry) for entry in declared]


def _build_discovery_payload(
    ctx: PluginContext,
    *,
    plugin_id: str,
) -> dict[str, object]:
    plugin_type = str(ctx.pdata.get("type", "plugin") or "plugin")
    error_type: str | None = None
    error_message: str | None = None
    error_phase: str | None = None

    if not ctx.enabled:
        entries_preview = _extract_entries_preview(
            plugin_id,
            cls=type("DisabledPluginStub", (), {}),
            conf=ctx.conf,
            pdata=ctx.pdata,
        )
    else:
        entries_preview: list[dict[str, object]]
        entry_mismatch = describe_plugin_entry_directory_mismatch(
            ctx.entry,
            config_path=ctx.toml_path,
        )
        if entry_mismatch:
            error_type = "PluginEntryDirectoryMismatch"
            error_message = entry_mismatch
            error_phase = "entry_validation"
            entries_preview = _extract_entries_preview(
                plugin_id,
                cls=type("FailedPluginStub", (), {}),
                conf=ctx.conf,
                pdata=ctx.pdata,
            )
        else:
            dependency_errors: list[str] = []
            for dep in ctx.dependencies:
                satisfied, dep_error = _check_plugin_dependency(dep, logger, plugin_id)
                if not satisfied:
                    dependency_errors.append(str(dep_error or "dependency check failed"))
                    break
            if dependency_errors:
                error_type = "DependencyCheckFailed"
                error_message = dependency_errors[0]
                error_phase = "dependency_check"
                entries_preview = _extract_entries_preview(
                    plugin_id,
                    cls=type("FailedPluginStub", (), {}),
                    conf=ctx.conf,
                    pdata=ctx.pdata,
                )
            else:
                missing_requirements = _find_missing_python_requirements(
                    ctx.python_requirements,
                    search_paths=ctx.python_requirement_paths,
                )
                if missing_requirements:
                    error_type = "MissingPythonDependencies"
                    error_message = f"Unsatisfied Python dependencies: {missing_requirements}"
                    error_phase = "python_requirements"
                    entries_preview = _extract_entries_preview(
                        plugin_id,
                        cls=type("FailedPluginStub", (), {}),
                        conf=ctx.conf,
                        pdata=ctx.pdata,
                    )
                else:
                    entries_preview = _packaged_entries_preview(ctx, plugin_id)

    plugin_meta = _build_plugin_meta(
        plugin_id,
        ctx.pdata,
        sdk_supported_str=ctx.sdk_supported_str,
        sdk_recommended_str=ctx.sdk_recommended_str,
        sdk_untested_str=ctx.sdk_untested_str,
        sdk_conflicts_list=ctx.sdk_conflicts_list,
        dependencies=ctx.dependencies,
        plugin_ui=_extract_plugin_ui_config(ctx.conf, plugin_id=plugin_id, logger=logger),
    )
    payload = plugin_meta.model_dump(mode="python")
    payload["config_path"] = str(ctx.toml_path)
    payload["entry_point"] = ctx.entry
    payload["runtime_enabled"] = bool(ctx.enabled)
    payload["runtime_auto_start"] = bool(ctx.auto_start)
    payload["entries_preview"] = entries_preview
    payload["plugin_type"] = plugin_type
    if plugin_type == "adapter":
        adapter_conf = ctx.conf.get("adapter")
        if isinstance(adapter_conf, dict):
            payload["adapter_mode"] = str(adapter_conf.get("mode", "hybrid") or "hybrid")

    # 这里原本还有一条"瞬时扫描失败"的分支：扫描超时或预算耗尽时不进 failed 状态、
    # 并把上一次扫出来的条目接回去（runtime_scan_deferred）。刷新不再扫描之后这两
    # 件事都没有了——读一份 JSON 不会超时，也没有预算可耗尽，剩下的失败（依赖不满足、
    # 入口目录不匹配、Python 依赖缺失）全都是关于这个插件本身的，本来就该进 failed。
    if error_type and error_message and error_phase:
        payload["runtime_load_state"] = "failed"
        payload["runtime_load_error_type"] = error_type
        payload["runtime_load_error_message"] = error_message
        payload["runtime_load_error_phase"] = error_phase
    else:
        payload.pop("runtime_load_state", None)
        payload.pop("runtime_load_error_type", None)
        payload.pop("runtime_load_error_message", None)
        payload.pop("runtime_load_error_phase", None)

    payload.pop("runtime_source_missing", None)
    return payload


def _build_discovery_record_from_context(
    ctx: PluginContext,
) -> PluginDiscoveryRecord:
    payload = _build_discovery_payload(ctx, plugin_id=ctx.pid)
    return PluginDiscoveryRecord(
        plugin_id=ctx.pid,
        original_plugin_id=ctx.pid,
        config_path=ctx.toml_path,
        entry_point=ctx.entry,
        plugin_type=str(ctx.pdata.get("type", "plugin") or "plugin"),
        enabled=bool(ctx.enabled),
        auto_start=bool(ctx.auto_start),
        meta_payload=payload,
    )


def _validate_plugin_runtime_source_sync(plugin_id: str, config_path: Path) -> None:
    """Validate one selected source even when its manifest disables runtime loading.

    This one *does* import the plugin, in the isolated worker, exactly once.
    That is deliberate and is not the thing discovery gave up: the caller is a
    user switching a plugin's source, for that one plugin, and the whole point
    of the step is to find out whether the promoted copy actually loads before
    committing to it. Rolling back on a broken source is only possible if
    something tried it (see the builtin-override rollback path).

    Discovery, by contrast, runs for every plugin on the machine whenever
    anything refreshes, which is why it reads packaged metadata instead.
    """
    from plugin.server.application.plugins.metadata_scanner import (
        PluginMetadataScanError,
        scan_plugin_metadata_isolated,
    )

    resolved_config_path = _resolve_config_path(config_path)
    ctx = _parse_single_plugin_config(resolved_config_path, set(), logger)
    if ctx is None or ctx.pid != plugin_id:
        raise RuntimeError("promoted plugin configuration could not be validated")

    payload = _build_discovery_payload(
        replace(ctx, enabled=True),
        plugin_id=plugin_id,
    )
    if payload.get("runtime_load_state") == "failed":
        error_type = str(payload.get("runtime_load_error_type") or "unknown")
        error_phase = str(payload.get("runtime_load_error_phase") or "unknown")
        raise RuntimeError(
            "promoted plugin runtime validation failed "
            f"({error_type} during {error_phase})"
        )

    entry = str(ctx.entry or "")
    if ":" not in entry:
        raise RuntimeError(
            "promoted plugin runtime validation failed "
            f"(malformed entry point {entry!r} during entry_validation)"
        )
    module_path, class_name = entry.split(":", 1)
    try:
        scan_plugin_metadata_isolated(
            plugin_id=plugin_id,
            module_path=module_path,
            class_name=class_name,
            config_path=resolved_config_path,
            conf=ctx.conf,
            pdata=ctx.pdata,
            python_requirement_paths=ctx.python_requirement_paths,
        )
    except PluginMetadataScanError as exc:
        phase = "import_class" if exc.error_type == "AttributeError" else "import_module"
        raise RuntimeError(
            "promoted plugin runtime validation failed "
            f"({exc.error_type} during {phase})"
        ) from exc


def _apply_discovery_record_sync(
    record: PluginDiscoveryRecord,
    *,
    existing_snapshot: dict[str, dict[str, object]] | None = None,
    preferred_runtime_plugin_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    target_plugin_id = preferred_runtime_plugin_id
    if target_plugin_id is None and existing_snapshot is not None:
        target_plugin_id = _find_existing_runtime_plugin_id_by_config_path(
            record.config_path,
            existing_snapshot,
        )
    if target_plugin_id is None:
        target_plugin_id = record.plugin_id

    existing_target_meta = (existing_snapshot or {}).get(target_plugin_id)
    existing_target_path = _resolve_meta_config_path(existing_target_meta)
    source_replacement = (
        target_plugin_id == record.plugin_id
        and existing_target_path is not None
        and existing_target_path != _resolve_config_path(record.config_path)
        and (
            bool(record.meta_payload.get("shadowed_builtin_path"))
            or not existing_target_path.exists()
        )
    )

    runtime_plugin_id = target_plugin_id if source_replacement else _resolve_plugin_id_conflict(
        target_plugin_id,
        logger,
        config_path=record.config_path,
        entry_point=record.entry_point,
        plugin_data=record.meta_payload,
        purpose="register",
        enable_rename=True,
    )
    if runtime_plugin_id is None:
        raise ServerDomainError(
            code="PLUGIN_REGISTRY_CONFLICT",
            message=f"Plugin '{record.plugin_id}' could not be registered due to an ID conflict",
            status_code=409,
            details={"plugin_id": record.plugin_id},
        )

    _move_autostart_gate_to_runtime_id(
        record.plugin_id,
        runtime_plugin_id,
        declared_id_is_taken=_declared_id_taken_by_another_plugin(
            record.plugin_id, record.config_path
        ),
    )

    plugin_meta = _build_plugin_meta(
        runtime_plugin_id,
        {
            "name": record.meta_payload.get("name", runtime_plugin_id),
            "type": record.meta_payload.get("type", record.plugin_type),
            "description": record.meta_payload.get("description", ""),
            "short_description": record.meta_payload.get("short_description", ""),
            "keywords": record.meta_payload.get("keywords", []),
            "passive": record.meta_payload.get("passive", False),
            "version": record.meta_payload.get("version", "0.1.0"),
            "author": record.meta_payload.get("author"),
        },
        sdk_supported_str=record.meta_payload.get("sdk_supported") if isinstance(record.meta_payload.get("sdk_supported"), str) else None,
        sdk_recommended_str=record.meta_payload.get("sdk_recommended") if isinstance(record.meta_payload.get("sdk_recommended"), str) else None,
        sdk_untested_str=record.meta_payload.get("sdk_untested") if isinstance(record.meta_payload.get("sdk_untested"), str) else None,
        sdk_conflicts_list=record.meta_payload.get("sdk_conflicts") if isinstance(record.meta_payload.get("sdk_conflicts"), list) else None,
        dependencies=record.meta_payload.get("dependencies") if isinstance(record.meta_payload.get("dependencies"), list) else None,
        plugin_ui=record.meta_payload.get("plugin_ui") if isinstance(record.meta_payload.get("plugin_ui"), dict) else None,
    )
    if source_replacement:
        resolved_id = runtime_plugin_id
        with state.acquire_plugins_write_lock():
            replacement_dump = plugin_meta.model_dump(mode="python")
            replacement_dump["config_path"] = str(record.config_path)
            replacement_dump["entry_point"] = record.entry_point
            state.plugins[resolved_id] = replacement_dump
        state.invalidate_snapshot_cache("plugins")
    else:
        resolved_id = register_plugin(
            plugin_meta,
            logger,
            config_path=record.config_path,
            entry_point=record.entry_point,
        )
    if resolved_id is None:
        raise ServerDomainError(
            code="PLUGIN_REGISTRY_CONFLICT",
            message=f"Plugin '{record.plugin_id}' could not be registered due to an ID conflict",
            status_code=409,
            details={"plugin_id": record.plugin_id},
        )

    payload = dict(record.meta_payload)
    if resolved_id != record.plugin_id:
        payload["id"] = resolved_id
        preview_obj = payload.get("entries_preview")
        if isinstance(preview_obj, list):
            payload["entries_preview"] = _remap_entries_preview_plugin_id(
                [item for item in preview_obj if isinstance(item, dict)],
                plugin_id=resolved_id,
            )

    with state.acquire_plugins_write_lock():
        current_meta = state.plugins.get(resolved_id)
        merged = dict(current_meta) if isinstance(current_meta, dict) else {}
        for key in _MANAGED_META_KEYS:
            if key in payload:
                merged[key] = payload[key]
            else:
                merged.pop(key, None)
        state.plugins[resolved_id] = merged
    state.invalidate_snapshot_cache("plugins")
    return resolved_id, payload


def _remove_config_path_aliases_sync(config_path: Path, *, keep_plugin_id: str) -> list[str]:
    resolved_path = _resolve_config_path(config_path)
    running_ids = _list_running_plugin_ids_sync()
    removed: list[str] = []
    kept_running: list[str] = []
    with state.acquire_plugins_write_lock():
        for plugin_id, raw_meta in list(state.plugins.items()):
            if plugin_id == keep_plugin_id or not isinstance(raw_meta, dict):
                continue
            if _resolve_meta_config_path(raw_meta) != resolved_path:
                continue
            if plugin_id in running_ids:
                preserved = dict(raw_meta)
                preserved["runtime_source_missing"] = True
                state.plugins[plugin_id] = preserved
                kept_running.append(plugin_id)
                continue
            state.plugins.pop(plugin_id, None)
            removed.append(plugin_id)
    if removed or kept_running:
        state.invalidate_snapshot_cache("plugins")
    return removed


def _remove_stale_plugin_metadata_sync(
    stale_ids: set[str],
    *,
    running_ids: set[str],
) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    kept_running: list[str] = []
    with state.acquire_plugins_write_lock():
        for plugin_id in sorted(stale_ids):
            raw_meta = state.plugins.get(plugin_id)
            if not isinstance(raw_meta, dict):
                continue
            if plugin_id in running_ids:
                raw_meta["runtime_source_missing"] = True
                state.plugins[plugin_id] = raw_meta
                kept_running.append(plugin_id)
                continue
            state.plugins.pop(plugin_id, None)
            removed.append(plugin_id)
    if removed or kept_running:
        state.invalidate_snapshot_cache("plugins")
    return removed, kept_running


def _collect_missing_plugin_ids_sync(existing_snapshot: dict[str, dict[str, object]]) -> set[str]:
    missing_ids: set[str] = set()
    for plugin_id, meta in existing_snapshot.items():
        config_path_obj = meta.get("config_path")
        if not isinstance(config_path_obj, str) or not config_path_obj:
            continue
        try:
            config_path = Path(config_path_obj).resolve()
        except Exception:
            config_path = Path(config_path_obj)
        if not config_path.exists():
            missing_ids.add(plugin_id)
    return missing_ids


def _move_autostart_gate_to_runtime_id(
    declared_plugin_id: str,
    runtime_plugin_id: str,
    *,
    declared_id_is_taken: bool = False,
) -> None:
    """Re-key a pending approval when the registry renames a plugin.

    The install gate can only write the id the manifest declares; the runtime id
    is decided here, and a second plugin declaring an id that is already taken
    gets a suffix (``demo`` -> ``demo_1``). The autostart check asks about the
    runtime id, so the record written at install time missed it entirely and the
    freshly installed code was free to start itself (codex).

    Moving rather than copying: the store is keyed by id, so a copy would leave
    a record under ``demo`` that belongs to nobody — it would hold back whichever
    plugin owns that id (one it may have earned long ago), and clearing it by
    starting that plugin would silently approve this one. After the move each
    record belongs to exactly one runtime plugin.
    """
    if not declared_plugin_id or declared_plugin_id == runtime_plugin_id:
        return
    if is_autostart_approved(declared_plugin_id):
        return
    if not mark_autostart_pending(runtime_plugin_id):
        # 记不上就不能把这条改名记录发布出去。留在声明 id 上等于没拦住：注册用的
        # 和自启动筛选看的都是运行时 id，那边没有记录就是"已批准"，这份从没被启动
        # 过的新代码会在下次开机自己跑起来（coderabbit）。抛出去，让这一个插件这轮
        # 注册失败——刷新循环按记录逐个兜底，其它插件不受影响。
        logger.error(
            "could not move the pending approval from {} to its runtime id {}; "
            "refusing to register the renamed plugin",
            declared_plugin_id,
            runtime_plugin_id,
        )
        raise ServerDomainError(
            code="PLUGIN_AUTOSTART_GATE_UNAVAILABLE",
            message=(
                "cannot record the renamed plugin as awaiting approval; refusing "
                "to register code that would autostart unapproved"
            ),
            status_code=500,
            details={
                "plugin_id": declared_plugin_id,
                "runtime_plugin_id": runtime_plugin_id,
            },
        )
    if declared_id_is_taken:
        # 声明 id 已经是另一个插件的运行时 id，那条记录可能是**它**的——它自己也
        # 可能是装上了还没被启动过的（codex）。搬走等于顺手批准了它。这种情况下
        # 只复制：两个插件各有一条记录，都拦着，谁被启动谁的那条被清掉。
        return
    if not clear_autostart_pending(declared_plugin_id):
        logger.error(
            "pending approval moved to {} but the record under {} could not be "
            "cleared; the plugin holding that id may need one manual start",
            runtime_plugin_id,
            declared_plugin_id,
        )


def _get_autostart_plugin_ids_sync() -> list[str]:
    candidates: set[str] = set()
    with state.acquire_plugins_read_lock():
        for plugin_id, raw_meta in state.plugins.items():
            if not isinstance(plugin_id, str) or not isinstance(raw_meta, dict):
                continue
            if raw_meta.get("runtime_enabled") is False:
                continue
            if raw_meta.get("runtime_auto_start") is False:
                continue
            if raw_meta.get("runtime_load_state") == "failed":
                continue
            if raw_meta.get("runtime_source_missing") is True:
                continue
            if not is_autostart_approved(plugin_id):
                # 装上和跑起来是两件事，只有后一件是用户做的。manifest 里的
                # auto_start 默认为真，所以刚装上的插件会在下一次开机自己跑起来，
                # 而用户从没启动过它。只有装上之后还没被用户启动过的插件会被拦，
                # 存量插件没有记录、照常自启。
                continue
            candidates.add(plugin_id)
    return _build_ordered_plugin_ids_sync(candidates)


class PluginRegistryService:
    async def refresh_registry(self) -> dict[str, object]:
        """Rebuild the registry from what is on disk.

        There is no ``force`` any more because there is no cache to bypass: a
        refresh reads each plugin's manifest and packaged metadata every time.
        The flag used to mean "re-import the plugins instead of trusting the
        memoised scan", and refreshing never imports anything now.
        """
        return await asyncio.to_thread(self._refresh_registry_sync)

    async def refresh_plugin(self, plugin_id: str) -> dict[str, object]:
        """Rebuild one plugin's registry entry from what is on disk."""
        return await asyncio.to_thread(self._refresh_plugin_sync, plugin_id)

    async def validate_plugin_runtime_source(
        self,
        *,
        plugin_id: str,
        config_path: Path,
    ) -> None:
        await asyncio.to_thread(
            _validate_plugin_runtime_source_sync,
            plugin_id,
            config_path,
        )

    async def list_autostart_plugin_ids(self) -> list[str]:
        return await asyncio.to_thread(_get_autostart_plugin_ids_sync)

    async def order_plugin_ids(self, plugin_ids: list[str]) -> list[str]:
        return await asyncio.to_thread(self._order_plugin_ids_sync, plugin_ids)

    def _refresh_registry_sync(self) -> dict[str, object]:
        roots = tuple(PLUGIN_CONFIG_ROOTS)
        _prepare_plugin_import_roots(roots, logger)

        added: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        refreshed_ids: set[str] = set()
        # 读盘、读现有快照、发布，全都在锁里。只把发布圈进来是不够的：两次重叠的
        # 刷新可以各自在锁外读到同一份旧快照，然后先后进锁，后进的那次拿着过时的
        # existing_snapshot 做增删对账，把前一次刚写进去的记录当成"多出来的"删掉
        # （codex）。读盘现在只有毫秒级，圈进锁里不需要付什么代价。
        with _REGISTRY_REFRESH_LOCK:
            existing_snapshot = _get_registered_plugin_snapshot_sync()
            running_ids = _list_running_plugin_ids_sync()
            snapshot = _discover_registry_snapshot_sync(roots)
            failed = [
                {
                    "plugin_id": item.plugin_id or "",
                    "config_path": str(item.config_path),
                    "error": item.error,
                }
                for item in snapshot.failures
            ]

            for record in snapshot.records:
                try:
                    previous_runtime_plugin_id = _find_existing_runtime_plugin_id_by_config_path(
                        record.config_path,
                        existing_snapshot,
                    )
                    if record.meta_payload.get("shadowed_builtin_path"):
                        # A valid user override always owns the declared ID. Clean
                        # up aliases left by the legacy conflict renamer instead of
                        # perpetuating ``study_companion_1``.
                        previous_runtime_plugin_id = record.plugin_id
                    previous_plugin_id = previous_runtime_plugin_id or record.plugin_id
                    previous_managed = _select_managed_fields(existing_snapshot.get(previous_plugin_id, {}))
                    resolved_id, payload = _apply_discovery_record_sync(
                        record,
                        existing_snapshot=existing_snapshot,
                        preferred_runtime_plugin_id=previous_runtime_plugin_id,
                    )
                    if record.meta_payload.get("shadowed_builtin_path"):
                        _remove_config_path_aliases_sync(record.config_path, keep_plugin_id=resolved_id)
                    refreshed_ids.add(resolved_id)
                    current_managed = _select_managed_fields(payload)
                    if resolved_id not in existing_snapshot:
                        added.append(resolved_id)
                    elif previous_managed == current_managed:
                        unchanged.append(resolved_id)
                    else:
                        updated.append(resolved_id)
                except ServerDomainError as exc:
                    failed.append(
                        {
                            "plugin_id": record.plugin_id,
                            "config_path": str(record.config_path),
                            "error": exc.message,
                        }
                    )
                except Exception as exc:
                    logger.warning(
                        "refresh_registry failed for plugin {}: err_type={}, err={}",
                        record.plugin_id,
                        type(exc).__name__,
                        str(exc),
                    )
                    failed.append(
                        {
                            "plugin_id": record.plugin_id,
                            "config_path": str(record.config_path),
                            "error": str(exc),
                        }
                    )

            missing_ids = _collect_missing_plugin_ids_sync(existing_snapshot) - refreshed_ids
            removed, removed_running = _remove_stale_plugin_metadata_sync(missing_ids, running_ids=running_ids)
            return {
                "success": not failed,
                "added": added,
                "updated": updated,
                "removed": removed,
                "removed_running": removed_running,
                "unchanged": unchanged,
                "failed": failed,
                "shadowed": [
                    {
                        "plugin_id": record.plugin_id,
                        "config_path": str(record.config_path),
                        "source": _source_for_config_path(record.config_path),
                    }
                    for record in snapshot.shadowed
                ],
                "scanned_count": len(snapshot.records) + len(snapshot.failures),
            }

    def _refresh_plugin_sync(self, plugin_id: str) -> dict[str, object]:
        normalized_plugin_id = plugin_id.strip()
        if not _PLUGIN_ID_PATTERN.fullmatch(normalized_plugin_id):
            raise ServerDomainError(
                code="PLUGIN_INVALID_ID",
                message="Invalid plugin id",
                status_code=400,
                details={"plugin_id": plugin_id},
            )

        # 读盘、读现有快照、发布，全都在一次持锁里完成。单插件刷新原本只把发布
        # 圈进锁里，existing_snapshot 在锁外就读走了——它决定 previous_runtime_
        # plugin_id、previous_managed，以及要不要走 source_replacement。中间只要
        # 有一次全量刷新发布完成，这份快照就已经过时（coderabbit / codex）。而
        # start_plugin 调 refresh_plugin、reload_all_plugins 调 refresh_registry，
        # 两条路同时发生并不罕见。
        with _REGISTRY_REFRESH_LOCK:
            roots = tuple(PLUGIN_CONFIG_ROOTS)
            existing_snapshot = _get_registered_plugin_snapshot_sync()
            _prepare_plugin_import_roots(roots, logger)
            existing_config_path = _resolve_meta_config_path(existing_snapshot.get(normalized_plugin_id))
            record: PluginDiscoveryRecord | None = None
            if (
                existing_config_path is not None
                and existing_config_path.exists()
                and not _config_path_belongs_to_roots(existing_config_path, roots)
            ):
                ctx = _parse_single_plugin_config(existing_config_path, set(), logger)
                if ctx is not None:
                    record = _build_discovery_record_from_context(ctx)
            else:
                discovery = _discover_registry_snapshot_sync(roots)
                record = next(
                    (
                        item
                        for item in discovery.records
                        if existing_config_path is not None
                        and _resolve_config_path(item.config_path) == existing_config_path
                    ),
                    None,
                )
                if record is None:
                    record = next(
                        (item for item in discovery.records if item.plugin_id == normalized_plugin_id),
                        None,
                    )
            config_path = record.config_path if record is not None else None
            if config_path is None:
                raise ServerDomainError(
                    code="PLUGIN_CONFIG_NOT_FOUND",
                    message=f"Plugin '{normalized_plugin_id}' configuration not found",
                    status_code=404,
                    details={"plugin_id": normalized_plugin_id},
                )

            previous_runtime_plugin_id = _find_existing_runtime_plugin_id_by_config_path(
                config_path,
                existing_snapshot,
            )
            if record.meta_payload.get("shadowed_builtin_path"):
                previous_runtime_plugin_id = record.plugin_id
            previous_plugin_id = previous_runtime_plugin_id or normalized_plugin_id
            previous_managed = _select_managed_fields(existing_snapshot.get(previous_plugin_id, {}))
            resolved_id, payload = _apply_discovery_record_sync(
                record,
                existing_snapshot=existing_snapshot,
                preferred_runtime_plugin_id=previous_runtime_plugin_id,
            )
            if record.meta_payload.get("shadowed_builtin_path"):
                _remove_config_path_aliases_sync(config_path, keep_plugin_id=resolved_id)
            current_managed = _select_managed_fields(payload)
            status = "added"
            if previous_plugin_id in existing_snapshot:
                status = "unchanged" if previous_managed == current_managed else "updated"

            return {
                "success": True,
                "plugin_id": resolved_id,
                "original_plugin_id": normalized_plugin_id,
                "status": status,
                "config_path": str(config_path),
            }

    def _order_plugin_ids_sync(self, plugin_ids: list[str]) -> list[str]:
        return _build_ordered_plugin_ids_sync({plugin_id for plugin_id in plugin_ids if isinstance(plugin_id, str)})
