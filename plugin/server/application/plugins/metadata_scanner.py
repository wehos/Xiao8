"""Read a plugin's metadata by importing its entry class in a throwaway worker.

Reading metadata means executing an untrusted plugin's module-level code: it
may raise, block forever, spawn helpers, or leak descriptors and threads. Doing
that inside the agent process would take the host down with it, so the import
runs in a subprocess we can time out and kill along with everything it spawned,
and only a JSON result crosses back.
"""

from __future__ import annotations

import json
import math
import os
import queue
import selectors
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping

import psutil

from plugin._types.events import EventHandler, EventMeta
from plugin.core import registry as registry_module
from plugin.core.state import state


_RESULT_PREFIX = "NEKO_PLUGIN_METADATA_RESULT:"
_MAX_METADATA_RESULT_BYTES = 1024 * 1024
_PROCESS_CLEANUP_TIMEOUT = 0.5
_WORKER_BOOTSTRAP = (
    "import os,sys;"
    "_stdout_fd=sys.stdout.fileno();"
    "_protocol_fd=os.dup(_stdout_fd);"
    "_devnull_fd=os.open(os.devnull,os.O_WRONLY);"
    "os.dup2(_devnull_fd,_stdout_fd);"
    "os.dup2(_devnull_fd,sys.stderr.fileno());"
    "os.close(_devnull_fd);"
    "from plugin.server.application.plugins.metadata_scanner "
    "import _worker_main;_worker_main(_protocol_fd)"
)


# 单个插件扫描的上限。
#
# 从 30s 降下来：实测本机 17 个真实插件里最慢的 1.41s、中位 0.97s，所以 10s 仍有
# 约 7 倍余量，冷盘或杀软首次逐个扫解释器时也够。30s 的问题是它乘以插件数——
# 一个卡住的插件能把整轮 discovery 拖到分钟级，而前端只等 30s。
#
# 注意单项上限本身不足以封顶：17 个插件按 5 并发是 4 波，4×10s 仍然超前端预算。
# 真正封顶的是 registry_service 那边的总预算，这里只负责让单个坏插件早点放手。
# Env: NEKO_PLUGIN_METADATA_SCAN_TIMEOUT
from plugin.server.application.plugins._env_budgets import env_seconds

_DEFAULT_SCAN_TIMEOUT_SECONDS = env_seconds("NEKO_PLUGIN_METADATA_SCAN_TIMEOUT", 10.0)

# 这里曾经有一个全局信号量，限制同时活着的元数据解释器数量，因为 discovery 会
# 并行强扫十几个插件、每个常驻约 66 MB。discovery 不再扫描之后扇出没有了：唯一
# 的调用方是 start_plugin，而它跑在插件操作锁里，一次只可能有一个。
#
# 连带删掉的还有"等槽位超过 50ms 就把超时改判成预算问题"那条判据——它的存在
# 前提就是有人要排队等槽位。


def _metadata_worker_command() -> list[str]:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return [sys.executable, "--neko-plugin-metadata-worker"]
    return [sys.executable, "-c", _WORKER_BOOTSTRAP]


def _handler_key_belongs_to_plugin(key: str, plugin_id: str) -> bool:
    return key.startswith(f"{plugin_id}.") or key.startswith(f"{plugin_id}:")


def _terminate_processes(processes: list[psutil.Process]) -> None:
    for process in processes:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    try:
        _, alive = psutil.wait_procs(
            processes,
            timeout=_PROCESS_CLEANUP_TIMEOUT,
        )
    except (psutil.Error, OSError, RuntimeError, ValueError):
        alive = processes
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if alive:
        try:
            psutil.wait_procs(alive, timeout=_PROCESS_CLEANUP_TIMEOUT)
        except (psutil.Error, OSError, RuntimeError, ValueError):
            pass


def _terminate_worker_tree(
    process: subprocess.Popen[bytes],
    cleanup_lock: threading.RLock | None = None,
) -> None:
    lock = cleanup_lock or threading.RLock()
    with lock:
        if process.returncode is not None:
            return
        try:
            parent = psutil.Process(process.pid)
        except (psutil.Error, OSError, RuntimeError, ValueError):
            parent = None
            descendants = []
        else:
            try:
                descendants = parent.children(recursive=True)
            except (psutil.Error, OSError, RuntimeError, ValueError):
                descendants = []

        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass

        processes = [*descendants, parent] if parent is not None else descendants
        _terminate_processes(processes)

        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                # 进程在 poll() 和 kill() 之间自己退了，或者句柄已经无效——
                # 目的（它不再运行）已经达成，没有可补救的。
                pass


def _terminate_and_reap_worker(
    process: subprocess.Popen[bytes],
    cleanup_lock: threading.RLock,
) -> None:
    with cleanup_lock:
        _terminate_worker_tree(process, cleanup_lock)
        try:
            process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                # 同理：已经 kill 过的进程通常立刻可收，但这一步无界等待没有
                # 任何东西护着，收不掉就放手，别把关停卡在这儿。
                process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass


_STDERR_TAIL_BYTES = 1000
_STDERR_READ_TIMEOUT_SECONDS = 2.0


def _read_worker_stderr(process: subprocess.Popen[bytes]) -> str:
    """Read whatever diagnostics the worker left, without trusting the pipe.

    ``stream.read(n)`` on a pipe returns only at n bytes or EOF, and EOF needs
    every write handle closed — including any a grandchild inherited. Today the
    worker redirects fd 2 to devnull before it imports plugin code, in both the
    ``-c`` bootstrap and the frozen ``--neko-plugin-metadata-worker`` path, so
    plugin-spawned processes never hold this pipe and the read returns promptly
    (verified against a plugin that spawns a 30 s child at import: 0.75 s).

    That makes this read safe by an invariant maintained in two other places,
    which is a thin thing for an unbounded blocking call to rest on — and it
    sits after every timeout timer has been cancelled, so nothing would
    interrupt it. Bounded here instead: a background thread, and after the
    deadline we give up on the diagnostics rather than on the scan.
    """
    stream = process.stderr
    if stream is None:
        return ""

    collected: list[bytes] = []

    def _drain() -> None:
        try:
            collected.append(stream.read(_STDERR_TAIL_BYTES))
        except Exception:  # noqa: BLE001 - diagnostics only
            pass

    reader = threading.Thread(target=_drain, daemon=True, name="plugin-scan-stderr")
    reader.start()
    reader.join(timeout=_STDERR_READ_TIMEOUT_SECONDS)
    if reader.is_alive() or not collected:
        return ""
    return collected[0].decode("utf-8", errors="replace")


def _read_protocol_output_blocking(stream: BinaryIO) -> tuple[bytes, bool]:
    output = bytearray()
    result_prefix = _RESULT_PREFIX.encode("utf-8")
    while len(output) <= _MAX_METADATA_RESULT_BYTES:
        chunk = stream.readline(
            _MAX_METADATA_RESULT_BYTES + 1 - len(output)
        )
        if not chunk:
            break
        output.extend(chunk)
        if chunk.startswith(result_prefix):
            break
    return bytes(output), len(output) > _MAX_METADATA_RESULT_BYTES


def _read_protocol_output(
    stream: BinaryIO,
    *,
    timeout_event: threading.Event | None = None,
) -> tuple[bytes, bool]:
    """Read the worker protocol without letting an inherited fd defeat timeout.

    POSIX pipes are polled directly so a detached descendant holding the write
    end open cannot leave this process blocked in ``readline()``.  The fallback
    covers streams without a selectable descriptor (including Windows pipes)
    with a daemon reader and keeps the caller bounded by ``timeout_event``.
    """
    if timeout_event is None:
        return _read_protocol_output_blocking(stream)

    if os.name != "nt":
        try:
            fd = stream.fileno()
            selector = selectors.DefaultSelector()
            selector.register(fd, selectors.EVENT_READ)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        else:
            output = bytearray()
            result_prefix = _RESULT_PREFIX.encode("utf-8")
            line_start = 0
            try:
                while len(output) <= _MAX_METADATA_RESULT_BYTES:
                    if timeout_event.is_set():
                        raise TimeoutError("metadata protocol read timed out")
                    if not selector.select(timeout=0.05):
                        continue
                    chunk = os.read(
                        fd,
                        _MAX_METADATA_RESULT_BYTES + 1 - len(output),
                    )
                    if not chunk:
                        break
                    output.extend(chunk)
                    while True:
                        line_end = output.find(b"\n", line_start)
                        if line_end < 0:
                            break
                        line = output[line_start:line_end]
                        if line.endswith(b"\r"):
                            line = line[:-1]
                        line_start = line_end + 1
                        if line.startswith(result_prefix):
                            return bytes(output[:line_start]), False
                return bytes(output), len(output) > _MAX_METADATA_RESULT_BYTES
            finally:
                selector.close()

    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def _blocking_reader() -> None:
        try:
            result_queue.put((True, _read_protocol_output_blocking(stream)))
        except BaseException as exc:
            result_queue.put((False, exc))

    reader = threading.Thread(
        target=_blocking_reader,
        name="plugin-metadata-protocol-reader",
        daemon=True,
    )
    reader.start()
    while True:
        try:
            succeeded, result = result_queue.get(timeout=0.05)
        except queue.Empty:
            if timeout_event.is_set():
                raise TimeoutError("metadata protocol read timed out")
            continue
        if succeeded:
            return result  # type: ignore[return-value]
        raise result  # type: ignore[misc]


class PluginMetadataScanError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(slots=True)
class IsolatedPluginMetadata:
    entries_preview: list[dict[str, object]]
    handlers: dict[str, dict[str, object]]
    entry_methods: dict[str, str]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _event_meta_payload(meta: object) -> dict[str, object]:
    raw = getattr(meta, "__dict__", None)
    if isinstance(raw, dict):
        normalized = _json_safe(raw)
        if isinstance(normalized, dict):
            return normalized

    return {
        "event_type": str(getattr(meta, "event_type", "plugin_entry") or "plugin_entry"),
        "id": str(getattr(meta, "id", "") or ""),
        "name": _json_safe(getattr(meta, "name", "")),
        "description": _json_safe(getattr(meta, "description", "")),
        "input_schema": _json_safe(getattr(meta, "input_schema", None)),
        "kind": str(getattr(meta, "kind", "action") or "action"),
        "auto_start": bool(getattr(meta, "auto_start", False)),
        "enabled": bool(getattr(meta, "enabled", True)),
        "dynamic": bool(getattr(meta, "dynamic", False)),
        "metadata": _json_safe(getattr(meta, "metadata", None)),
    }


def _scan_in_worker(request: Mapping[str, object]) -> dict[str, object]:
    # Imports stay inside the worker: importing an untrusted plugin module runs
    # its module-level code, which must not be able to crash, hang or leak in
    # the agent/plugin-server process.
    from plugin.core.host import _import_plugin_module
    from plugin.core.registry import (
        _ensure_python_requirement_paths,
        _extract_entries_preview,
        scan_static_metadata,
    )
    from plugin.logging_config import get_logger

    plugin_id = str(request["plugin_id"])
    module_path = str(request["module_path"])
    class_name = str(request["class_name"])
    config_path = Path(str(request["config_path"]))
    conf_obj = request.get("conf")
    pdata_obj = request.get("pdata")
    conf = dict(conf_obj) if isinstance(conf_obj, Mapping) else {}
    pdata = dict(pdata_obj) if isinstance(pdata_obj, Mapping) else {}
    requirement_paths_obj = request.get("python_requirement_paths")
    requirement_paths = (
        [Path(str(item)) for item in requirement_paths_obj]
        if isinstance(requirement_paths_obj, list)
        else []
    )
    logger = get_logger("server.application.plugins.metadata_worker")

    _ensure_python_requirement_paths(requirement_paths, logger, plugin_id)
    module_obj = _import_plugin_module(module_path, config_path, logger)
    cls_obj = getattr(module_obj, class_name)
    if not isinstance(cls_obj, type):
        raise TypeError(
            f"Plugin '{plugin_id}' entry class '{class_name}' is invalid"
        )

    scan_static_metadata(plugin_id, cls_obj, conf, pdata)
    entries_preview = _extract_entries_preview(plugin_id, cls_obj, conf, pdata)

    prefix_dot = f"{plugin_id}."
    prefix_colon = f"{plugin_id}:"
    handlers: dict[str, dict[str, object]] = {}
    with state.acquire_event_handlers_read_lock():
        for key, handler in state.event_handlers.items():
            if isinstance(key, str) and (
                key.startswith(prefix_dot) or key.startswith(prefix_colon)
            ):
                handlers[key] = _event_meta_payload(handler.meta)

    entry_methods = {
        str(entry_id): str(method_name)
        for (mapped_plugin_id, entry_id), method_name in registry_module.plugin_entry_method_map.items()
        if mapped_plugin_id == plugin_id
    }
    return {
        "ok": True,
        "entries_preview": _json_safe(entries_preview),
        "handlers": handlers,
        "entry_methods": entry_methods,
    }


def _worker_main(protocol_fd: int | None = None) -> None:
    # Reserve a private duplicate of the protocol pipe, then redirect the
    # process-wide stdout/stderr descriptors before importing plugin code.
    # This prevents untrusted import output from being buffered without bound
    # by the parent and keeps it off the result channel. os._exit below also
    # prevents plugin-registered atexit hooks from appending a forged record.
    raw_close = os.close
    raw_dup = os.dup
    raw_dup2 = os.dup2
    raw_open = os.open
    raw_read = os.read
    raw_write = os.write
    immediate_exit = os._exit
    trusted_json_dumps = json.dumps
    result_prefix = _RESULT_PREFIX
    max_result_bytes = _MAX_METADATA_RESULT_BYTES
    control_fd = raw_dup(sys.stdin.fileno())
    main_module = sys.modules.get("__main__")
    if main_module is not None:
        vars(main_module).pop("_protocol_fd", None)
    if protocol_fd is None:
        stdout_fd = sys.stdout.fileno()
        stderr_fd = sys.stderr.fileno()
        protocol_fd = raw_dup(stdout_fd)
        devnull_fd = raw_open(os.devnull, os.O_WRONLY)
        raw_dup2(devnull_fd, stdout_fd)
        raw_dup2(devnull_fd, stderr_fd)
        raw_close(devnull_fd)
    try:
        request_obj = json.loads(sys.stdin.readline())
        if not isinstance(request_obj, dict):
            raise TypeError("metadata scan request must be an object")
        result = _scan_in_worker(request_obj)
    except BaseException as exc:
        result = {
            "ok": False,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    encoded_result = (
        "\n"
        + result_prefix
        + trusted_json_dumps(result, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(encoded_result) > max_result_bytes:
        result = {
            "ok": False,
            "error_type": "MetadataResultTooLarge",
            "message": (
                "Plugin metadata result exceeds the "
                f"{max_result_bytes}-byte protocol limit"
            ),
        }
        encoded_result = (
            "\n"
            + result_prefix
            + trusted_json_dumps(result, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    remaining = memoryview(encoded_result)
    while remaining:
        try:
            written = raw_write(protocol_fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("metadata worker result pipe closed")
        remaining = remaining[written:]
    raw_close(protocol_fd)
    try:
        raw_read(control_fd, 1)
    except OSError:
        pass
    raw_close(control_fd)
    immediate_exit(0)


def _scan_plugin_metadata_uncached(
    *,
    plugin_id: str,
    module_path: str,
    class_name: str,
    config_path: Path,
    conf: Mapping[str, object],
    pdata: Mapping[str, object],
    python_requirement_paths: list[Path] | tuple[Path, ...] = (),
    timeout: float = _DEFAULT_SCAN_TIMEOUT_SECONDS,
) -> IsolatedPluginMetadata:
    if timeout <= 0:
        # 总预算已经用完：连进程都不要起。调用方拿到的是和"扫描超时"同一种
        # 错误，于是插件照样出现在列表里（标成扫描失败），而不是整批中断。
        raise PluginMetadataScanError(
            "ScanBudgetExhausted",
            "Plugin metadata scan skipped: discovery time budget exhausted",
        )
    request = {
        "plugin_id": plugin_id,
        "module_path": module_path,
        "class_name": class_name,
        "config_path": str(config_path),
        "conf": _json_safe(conf),
        "pdata": _json_safe(pdata),
        "python_requirement_paths": [str(path) for path in python_requirement_paths],
    }
    project_root = Path(__file__).resolve().parents[4]

    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            _metadata_worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(project_root),
            **popen_kwargs,
        )
    except OSError as exc:
        raise PluginMetadataScanError(type(exc).__name__, str(exc)) from exc

    timed_out = threading.Event()
    cleanup_lock = threading.RLock()

    def _expire_worker() -> None:
        timed_out.set()
        _terminate_worker_tree(process, cleanup_lock)

    timeout_timer = threading.Timer(timeout, _expire_worker)
    timeout_timer.daemon = True
    timeout_timer.start()
    try:
        if process.stdin is None or process.stdout is None:
            raise OSError("metadata worker pipes are unavailable")
        process.stdin.write(
            (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        )
        process.stdin.flush()
        stdout_bytes, output_too_large = _read_protocol_output(
            process.stdout,
            timeout_event=timed_out,
        )
        timeout_timer.cancel()
        _terminate_and_reap_worker(process, cleanup_lock)
    except (OSError, TimeoutError) as exc:
        timeout_timer.cancel()
        _terminate_and_reap_worker(process, cleanup_lock)
        if timed_out.is_set():
            raise PluginMetadataScanError(
                "TimeoutExpired",
                f"Plugin metadata scan timed out after {timeout:g}s",
            ) from exc
        raise PluginMetadataScanError(type(exc).__name__, str(exc)) from exc
    finally:
        timeout_timer.cancel()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass

    if timed_out.is_set():
        raise PluginMetadataScanError(
            "TimeoutExpired",
            f"Plugin metadata scan timed out after {timeout:g}s",
        )
    if output_too_large:
        raise PluginMetadataScanError(
            "MetadataResultTooLarge",
            "Plugin metadata worker output exceeds the "
            f"{_MAX_METADATA_RESULT_BYTES}-byte protocol limit",
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = _read_worker_stderr(process)

    payload: dict[str, object] | None = None
    for line in reversed(stdout.splitlines()):
        if not line.startswith(_RESULT_PREFIX):
            continue
        try:
            decoded = json.loads(line[len(_RESULT_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break

    if payload is None:
        stderr = stderr.strip()
        detail = stderr[-1000:] if stderr else f"worker exited with code {process.returncode}"
        raise PluginMetadataScanError("MetadataWorkerFailed", detail)
    if payload.get("ok") is not True:
        raise PluginMetadataScanError(
            str(payload.get("error_type") or "MetadataScanFailed"),
            str(payload.get("message") or "Plugin metadata scan failed"),
        )

    entries_obj = payload.get("entries_preview")
    handlers_obj = payload.get("handlers")
    methods_obj = payload.get("entry_methods")
    entries_preview = [dict(item) for item in entries_obj if isinstance(item, dict)] if isinstance(entries_obj, list) else []
    handlers = (
        {str(key): dict(value) for key, value in handlers_obj.items() if isinstance(value, dict)}
        if isinstance(handlers_obj, dict)
        else {}
    )
    invalid_handler_keys = [
        key
        for key in handlers
        if not _handler_key_belongs_to_plugin(key, plugin_id)
    ]
    if invalid_handler_keys:
        raise PluginMetadataScanError(
            "InvalidMetadataResult",
            f"Metadata worker returned handler keys outside plugin '{plugin_id}'",
        )
    entry_methods = (
        {str(key): str(value) for key, value in methods_obj.items()}
        if isinstance(methods_obj, dict)
        else {}
    )
    return IsolatedPluginMetadata(
        entries_preview=entries_preview,
        handlers=handlers,
        entry_methods=entry_methods,
    )


def install_isolated_plugin_metadata(
    plugin_id: str,
    metadata: IsolatedPluginMetadata,
) -> None:
    prefix_dot = f"{plugin_id}."
    prefix_colon = f"{plugin_id}:"
    reconstructed: dict[str, EventHandler] = {}
    base_fields = {
        "event_type",
        "id",
        "name",
        "description",
        "input_schema",
        "kind",
        "auto_start",
        "enabled",
        "dynamic",
        "metadata",
    }

    for key, raw_meta in metadata.handlers.items():
        if not _handler_key_belongs_to_plugin(key, plugin_id):
            continue
        event_meta = EventMeta(
            event_type=str(raw_meta.get("event_type") or "plugin_entry"),
            id=str(raw_meta.get("id") or ""),
            name=raw_meta.get("name", ""),  # type: ignore[arg-type]
            description=raw_meta.get("description", ""),  # type: ignore[arg-type]
            input_schema=(
                raw_meta.get("input_schema")
                if isinstance(raw_meta.get("input_schema"), dict)
                else None
            ),
            kind=str(raw_meta.get("kind") or "action"),  # type: ignore[arg-type]
            auto_start=bool(raw_meta.get("auto_start", False)),
            enabled=bool(raw_meta.get("enabled", True)),
            dynamic=bool(raw_meta.get("dynamic", False)),
            metadata=(
                raw_meta.get("metadata")
                if isinstance(raw_meta.get("metadata"), dict)
                else None
            ),
        )
        for field_name, value in raw_meta.items():
            if field_name not in base_fields:
                setattr(event_meta, field_name, value)
        reconstructed[key] = EventHandler(
            meta=event_meta,
            handler=lambda *_args, **_kwargs: None,
        )

    with state.acquire_event_handlers_write_lock():
        runtime_handlers: dict[str, EventHandler] = {}
        for key, handler in state.event_handlers.items():
            if not (key.startswith(prefix_dot) or key.startswith(prefix_colon)):
                continue
            event_meta = getattr(handler, "meta", None)
            handler_metadata = getattr(event_meta, "metadata", None)
            if (
                getattr(event_meta, "dynamic", False) is True
                and isinstance(handler_metadata, dict)
                and handler_metadata.get("_dynamic") is True
                and handler_metadata.get("_registered_via_ipc") is True
            ):
                runtime_handlers[key] = handler
        for key in list(state.event_handlers):
            if key.startswith(prefix_dot) or key.startswith(prefix_colon):
                del state.event_handlers[key]
        state.event_handlers.update(reconstructed)
        # The host may have received ENTRY_UPDATE registrations while the
        # isolated worker was scanning static metadata. Runtime state wins on
        # collisions because it reflects the live plugin process.
        state.event_handlers.update(runtime_handlers)

    for key in list(registry_module.plugin_entry_method_map):
        if key[0] == plugin_id:
            del registry_module.plugin_entry_method_map[key]
    for entry_id, method_name in metadata.entry_methods.items():
        registry_module.plugin_entry_method_map[(plugin_id, entry_id)] = method_name
    state.invalidate_snapshot_cache("handlers")


def scan_plugin_metadata_isolated(
    *,
    plugin_id: str,
    module_path: str,
    class_name: str,
    config_path: Path,
    conf: Mapping[str, object],
    pdata: Mapping[str, object],
    python_requirement_paths: list[Path] | tuple[Path, ...] = (),
    timeout: float = _DEFAULT_SCAN_TIMEOUT_SECONDS,
) -> IsolatedPluginMetadata:
    """Import one plugin in a throwaway worker and read its metadata back.

    On-demand only. The sole caller is ``start_plugin``, for the one plugin the
    user just asked to run. Registry discovery reads packaged metadata off disk
    and imports nothing — see
    :mod:`plugin.server.infrastructure.packaged_metadata`.

    There is no result cache and no concurrency gate here any more. Both existed
    to make a fan-out of seventeen simultaneous scans survivable; discovery no
    longer scans, and ``start_plugin`` runs under the plugin operation lock, so
    scans are serialised by construction.
    """
    return _scan_plugin_metadata_uncached(
        plugin_id=plugin_id,
        module_path=module_path,
        class_name=class_name,
        config_path=config_path,
        conf=conf,
        pdata=pdata,
        python_requirement_paths=python_requirement_paths,
        timeout=timeout,
    )
