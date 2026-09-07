#!/usr/bin/env python3
"""Collect reproducible runtime-memory checkpoints for N.E.K.O.

The probe deliberately keeps two measurement scopes separate:

* ``stack`` samples real child processes with psutil. It is suitable for the
  launcher, Electron, and their descendants.
* ``scenario`` runs one lazy feature transition in this process so tracemalloc
  can distinguish traced Python allocations from total process USS.

The JSON output never includes command lines, environment variables, API
payloads, or response text. Synthetic chat runs retain only message types and
status codes. ``stack`` subprocess logs can contain application data, so treat
them as sensitive diagnostics and remove them after extracting aggregate data.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import time
import tracemalloc
import urllib.error
import urllib.parse
import urllib.request
import uuid
import weakref
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Iterable

import psutil


MIB = 1024 * 1024
DEFAULT_SAMPLE_INTERVAL = 0.25
HEALTH_APP_SIGNATURE = "N.E.K.O"
DEFAULT_SERVICE_ROLES = ("main", "memory", "agent")
_PROBED_EMBEDDING_SERVICES: weakref.WeakSet[Any] = weakref.WeakSet()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_LOCAL_PROBE_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirectHandler(),
)


def _mib(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / MIB, 3)


def _process_category(process: psutil.Process) -> str:
    try:
        name = process.name().lower()
    except (psutil.Error, OSError):
        return "unknown"

    try:
        command = [str(item).lower() for item in process.cmdline()]
    except (psutil.Error, OSError):
        command = []

    if "electron" in name or name == "n.e.k.o.exe":
        if any(item.startswith("--type=") for item in command):
            return "electron_chromium_child"
        return "electron_main"
    if "chrome" in name or "chromium" in name:
        return "chromium"
    if "python" in name:
        return "python"
    if name in {"uv.exe", "uv"}:
        return "uv_wrapper"
    if "node" in name:
        return "node"
    return "other"


def _process_row(process: psutil.Process) -> dict[str, Any] | None:
    try:
        basic = process.memory_info()
        try:
            full = process.memory_full_info()
            uss = getattr(full, "uss", None)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            uss = None
        try:
            thread_count = process.num_threads()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            thread_count = None
        try:
            handle_count = (
                process.num_handles() if hasattr(process, "num_handles") else None
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            handle_count = None

        onnx_map_count: int | None = 0
        onnx_mapped_rss: int | None = 0
        try:
            for memory_map in process.memory_maps(grouped=True):
                path = str(getattr(memory_map, "path", "") or "").lower()
                if "onnxruntime" not in path and not path.endswith(".onnx"):
                    continue
                onnx_map_count += 1
                onnx_mapped_rss += int(getattr(memory_map, "rss", 0) or 0)
        except (
            psutil.AccessDenied,
            psutil.NoSuchProcess,
            OSError,
            NotImplementedError,
        ):
            # Preserve an unknown result when maps are unsupported or inaccessible.
            onnx_map_count = None
            onnx_mapped_rss = None
        return {
            "pid": process.pid,
            "ppid": process.ppid(),
            "name": process.name(),
            "category": _process_category(process),
            "rss_mib": _mib(basic.rss),
            "uss_mib": _mib(uss),
            "threads": thread_count,
            "handles": handle_count,
            "onnx_map_count": onnx_map_count,
            "onnx_mapped_rss_mib": _mib(onnx_mapped_rss),
        }
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return None


def _tree_processes(root_pids: Iterable[int]) -> list[psutil.Process]:
    found: dict[int, psutil.Process] = {}
    for pid in root_pids:
        try:
            root = psutil.Process(pid)
            found[root.pid] = root
            for child in root.children(recursive=True):
                found[child.pid] = child
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    return list(found.values())


def _sample(
    root_pids: Iterable[int],
    *,
    observed_processes: dict[tuple[int, float], psutil.Process] | None = None,
) -> dict[str, Any]:
    processes = _tree_processes(root_pids)
    if observed_processes is not None:
        for process in processes:
            try:
                observed_processes[(process.pid, process.create_time())] = process
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue
    rows = [row for process in processes if (row := _process_row(process))]
    categories: dict[str, dict[str, float | int | None]] = {}
    for row in rows:
        entry = categories.setdefault(
            row["category"],
            {
                "count": 0,
                "rss_mib": 0.0,
                "uss_mib": 0.0,
                "uss_processes": 0,
                "threads": 0,
                "thread_processes": 0,
                "handles": 0,
                "handle_processes": 0,
                "onnx_map_count": 0,
                "onnx_map_processes": 0,
                "onnx_mapped_rss_mib": 0.0,
                "onnx_mapped_rss_processes": 0,
            },
        )
        entry["count"] = int(entry["count"]) + 1
        entry["rss_mib"] = float(entry["rss_mib"]) + float(row["rss_mib"] or 0.0)
        if row["uss_mib"] is not None:
            entry["uss_mib"] = float(entry["uss_mib"]) + float(row["uss_mib"])
            entry["uss_processes"] = int(entry["uss_processes"]) + 1
        if row["threads"] is not None:
            entry["threads"] = int(entry["threads"]) + int(row["threads"])
            entry["thread_processes"] = int(entry["thread_processes"]) + 1
        if row["handles"] is not None:
            entry["handles"] = int(entry["handles"]) + int(row["handles"])
            entry["handle_processes"] = int(entry["handle_processes"]) + 1
        if row["onnx_map_count"] is not None:
            entry["onnx_map_count"] = int(entry["onnx_map_count"]) + int(
                row["onnx_map_count"]
            )
            entry["onnx_map_processes"] = int(entry["onnx_map_processes"]) + 1
        if row["onnx_mapped_rss_mib"] is not None:
            entry["onnx_mapped_rss_mib"] = float(entry["onnx_mapped_rss_mib"]) + float(
                row["onnx_mapped_rss_mib"]
            )
            entry["onnx_mapped_rss_processes"] = (
                int(entry["onnx_mapped_rss_processes"]) + 1
            )
    for entry in categories.values():
        if entry["uss_processes"] == 0:
            entry["uss_mib"] = None
        if entry["thread_processes"] == 0:
            entry["threads"] = None
        if entry["handle_processes"] == 0:
            entry["handles"] = None
        if entry["onnx_map_processes"] == 0:
            entry["onnx_map_count"] = None
        if entry["onnx_mapped_rss_processes"] == 0:
            entry["onnx_mapped_rss_mib"] = None

    total_rss = sum(float(row["rss_mib"] or 0.0) for row in rows)
    total_uss_values = [
        float(row["uss_mib"]) for row in rows if row["uss_mib"] is not None
    ]
    total_threads = [int(row["threads"]) for row in rows if row["threads"] is not None]
    total_handles = [int(row["handles"]) for row in rows if row["handles"] is not None]
    total_onnx_maps = [
        int(row["onnx_map_count"]) for row in rows if row["onnx_map_count"] is not None
    ]
    total_onnx_rss = [
        float(row["onnx_mapped_rss_mib"])
        for row in rows
        if row["onnx_mapped_rss_mib"] is not None
    ]
    return {
        "elapsed_s": round(time.perf_counter(), 6),
        "categories": categories,
        "total": {
            "count": len(rows),
            "rss_mib": round(total_rss, 3),
            "uss_mib": round(sum(total_uss_values), 3) if total_uss_values else None,
            "uss_processes": len(total_uss_values),
            "threads": sum(total_threads) if total_threads else None,
            "thread_processes": len(total_threads),
            "handles": sum(total_handles) if total_handles else None,
            "handle_processes": len(total_handles),
            "onnx_map_count": sum(total_onnx_maps) if total_onnx_maps else None,
            "onnx_map_processes": len(total_onnx_maps),
            "onnx_mapped_rss_mib": round(sum(total_onnx_rss), 3)
            if total_onnx_rss
            else None,
            "onnx_mapped_rss_processes": len(total_onnx_rss),
        },
        "processes": rows,
    }


def _series_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    category_names = sorted(
        {name for sample in samples for name in sample.get("categories", {})}
    )
    categories: dict[str, dict[str, float | int | None]] = {}
    for name in category_names:
        rss_values = [
            float(sample["categories"].get(name, {}).get("rss_mib", 0.0))
            for sample in samples
        ]
        uss_values: list[float] = []
        for sample in samples:
            category = sample["categories"].get(name)
            if category is None:
                uss_values.append(0.0)
                continue
            uss_mib = category.get("uss_mib")
            if uss_mib is not None:
                uss_values.append(float(uss_mib))
        count_values = [
            int(sample["categories"].get(name, {}).get("count", 0))
            for sample in samples
        ]
        thread_values = [
            int(value)
            for sample in samples
            if (value := sample["categories"].get(name, {}).get("threads")) is not None
        ]
        handle_values = [
            int(value)
            for sample in samples
            if (value := sample["categories"].get(name, {}).get("handles")) is not None
        ]
        onnx_map_values = [
            int(value)
            for sample in samples
            if (value := sample["categories"].get(name, {}).get("onnx_map_count"))
            is not None
        ]
        onnx_rss_values = [
            float(value)
            for sample in samples
            if (value := sample["categories"].get(name, {}).get("onnx_mapped_rss_mib"))
            is not None
        ]
        categories[name] = {
            "median_count": int(statistics.median(count_values)),
            "max_count": max(count_values),
            "median_rss_mib": round(statistics.median(rss_values), 3),
            "peak_rss_mib": round(max(rss_values), 3),
            "median_uss_mib": round(statistics.median(uss_values), 3)
            if uss_values
            else None,
            "peak_uss_mib": round(max(uss_values), 3) if uss_values else None,
            "median_threads": round(statistics.median(thread_values), 3)
            if thread_values
            else None,
            "max_threads": max(thread_values) if thread_values else None,
            "median_handles": round(statistics.median(handle_values), 3)
            if handle_values
            else None,
            "max_handles": max(handle_values) if handle_values else None,
            "median_onnx_map_count": round(statistics.median(onnx_map_values), 3)
            if onnx_map_values
            else None,
            "max_onnx_map_count": max(onnx_map_values) if onnx_map_values else None,
            "median_onnx_mapped_rss_mib": round(statistics.median(onnx_rss_values), 3)
            if onnx_rss_values
            else None,
            "peak_onnx_mapped_rss_mib": round(max(onnx_rss_values), 3)
            if onnx_rss_values
            else None,
        }

    total_rss = [float(sample["total"]["rss_mib"] or 0.0) for sample in samples]
    total_uss = [
        float(sample["total"]["uss_mib"] or 0.0)
        for sample in samples
        if sample["total"]["uss_mib"] is not None
    ]
    total_threads = [
        int(value)
        for sample in samples
        if (value := sample["total"].get("threads")) is not None
    ]
    total_handles = [
        int(value)
        for sample in samples
        if (value := sample["total"].get("handles")) is not None
    ]
    total_onnx_maps = [
        int(value)
        for sample in samples
        if (value := sample["total"].get("onnx_map_count")) is not None
    ]
    total_onnx_rss = [
        float(value)
        for sample in samples
        if (value := sample["total"].get("onnx_mapped_rss_mib")) is not None
    ]
    return {
        "sample_count": len(samples),
        "categories": categories,
        "total": {
            "median_rss_mib": round(statistics.median(total_rss), 3)
            if total_rss
            else 0.0,
            "peak_rss_mib": round(max(total_rss), 3) if total_rss else 0.0,
            "median_uss_mib": round(statistics.median(total_uss), 3)
            if total_uss
            else None,
            "peak_uss_mib": round(max(total_uss), 3) if total_uss else None,
            "median_threads": round(statistics.median(total_threads), 3)
            if total_threads
            else None,
            "max_threads": max(total_threads) if total_threads else None,
            "median_handles": round(statistics.median(total_handles), 3)
            if total_handles
            else None,
            "max_handles": max(total_handles) if total_handles else None,
            "median_onnx_map_count": round(statistics.median(total_onnx_maps), 3)
            if total_onnx_maps
            else None,
            "max_onnx_map_count": max(total_onnx_maps) if total_onnx_maps else None,
            "median_onnx_mapped_rss_mib": round(statistics.median(total_onnx_rss), 3)
            if total_onnx_rss
            else None,
            "peak_onnx_mapped_rss_mib": round(max(total_onnx_rss), 3)
            if total_onnx_rss
            else None,
        },
        "last_processes": samples[-1]["processes"] if samples else [],
    }


def _sample_window(
    label: str,
    root_pids: Iterable[int],
    *,
    seconds: float,
    interval: float,
    traced_python_pid: int | None = None,
    observed_processes: dict[tuple[int, float], psutil.Process] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(seconds, 0.0)
    samples: list[dict[str, Any]] = []
    while True:
        samples.append(_sample(root_pids, observed_processes=observed_processes))
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)

    result = {"label": label, **_series_summary(samples)}
    if traced_python_pid is not None and tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        traced_uss = None
        for row in result["last_processes"]:
            if row["pid"] == traced_python_pid:
                traced_uss = row["uss_mib"]
                break
        current_mib = _mib(current)
        result["tracemalloc"] = {
            "pid": traced_python_pid,
            "current_mib": current_mib,
            "peak_mib": _mib(peak),
            "uss_minus_traced_current_mib": (
                round(float(traced_uss) - float(current_mib), 3)
                if traced_uss is not None and current_mib is not None
                else None
            ),
        }
    return result


def _register_embedding_service(service: Any) -> None:
    """Track direct probe services without extending their lifetime."""
    _PROBED_EMBEDDING_SERVICES.add(service)


def _runtime_resource_counts() -> dict[str, Any]:
    """Return reference counts for heavy runtimes without importing them.

    The process sampler reports mapped ONNX files and Chromium descendants.
    These counters complement it with the owners that are visible in Python so
    a released owner can be distinguished from a still-referenced model.
    """
    result: dict[str, Any] = {
        "embedding_service_instances": 0,
        "embedding_session_refs": 0,
        "embedding_tokenizer_refs": 0,
        "rapidocr_cache_entries": 0,
        "rapidocr_cache_owners": 0,
    }

    services = list(_PROBED_EMBEDDING_SERVICES)
    embeddings = sys.modules.get("memory.embeddings")
    if embeddings is not None:
        service = getattr(embeddings, "_SERVICE", None)
        if service is not None and all(service is not item for item in services):
            services.append(service)
    result["embedding_service_instances"] = len(services)
    result["embedding_session_refs"] = sum(
        int(getattr(service, "_session", None) is not None) for service in services
    )
    result["embedding_tokenizer_refs"] = sum(
        int(getattr(service, "_tokenizer", None) is not None) for service in services
    )

    seen_caches: set[int] = set()
    for module_name in (
        "plugin.plugins._shared.rapidocr.ocr_runtime_types",
        "plugin.plugins.galgame_plugin.ocr_runtime_types",
    ):
        runtime_types = sys.modules.get(module_name)
        if runtime_types is None:
            continue
        cache = getattr(runtime_types, "_RAPIDOCR_RUNTIME_CACHE", None)
        owners = getattr(runtime_types, "_RAPIDOCR_RUNTIME_CACHE_OWNERS", None)
        if not isinstance(cache, dict) or id(cache) in seen_caches:
            continue
        seen_caches.add(id(cache))
        result["rapidocr_cache_entries"] += len(cache)
        if isinstance(owners, dict):
            result["rapidocr_cache_owners"] += sum(
                max(0, int(value or 0)) for value in owners.values()
            )
    return result


def _in_process_checkpoint(
    label: str,
    *,
    seconds: float,
    interval: float,
    collect: bool = False,
    root_pids: Iterable[int] | None = None,
) -> dict[str, Any]:
    if collect:
        gc.collect()
    sample_root_pids = list(root_pids) if root_pids is not None else [os.getpid()]
    checkpoint = _sample_window(
        label,
        sample_root_pids,
        seconds=seconds,
        interval=interval,
        traced_python_pid=os.getpid() if os.getpid() in sample_root_pids else None,
    )
    checkpoint["captured_perf_counter"] = round(time.perf_counter(), 6)
    checkpoint["resources"] = _runtime_resource_counts()
    checkpoint["gc_counts"] = list(gc.get_count())
    return checkpoint


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _git_provenance(path: Path) -> dict[str, Any]:
    """Record enough source state to attribute a benchmark without copying diffs."""
    try:
        root = Path(
            subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            stderr=subprocess.DEVNULL,
        )
        tracked_diff = subprocess.check_output(
            ["git", "-C", str(root), "diff", "--binary", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "commit": "",
            "dirty": None,
            "status_sha256": None,
            "tracked_diff_sha256": None,
            "uv_lock_sha256": _sha256_file(path / "uv.lock"),
        }
    return {
        "commit": commit,
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest(),
        "uv_lock_sha256": _sha256_file(root / "uv.lock"),
    }


def _metadata(args: argparse.Namespace) -> dict[str, Any]:
    command = getattr(args, "command", None)
    benchmark_source = _git_provenance(Path.cwd())
    backend_source = (
        _git_provenance(Path(args.backend_cwd).resolve())
        if command == "stack"
        else benchmark_source
    )
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_commit": backend_source["commit"],
        "source": {
            "benchmark": benchmark_source,
            "backend": backend_source,
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
        "logical_cpu_count": psutil.cpu_count(),
        "ram_gib": round(psutil.virtual_memory().total / (1024**3), 3),
        "psutil": psutil.__version__,
        "sample_interval_s": args.interval,
        "sample_window_s": args.window,
        "stack": (
            {
                "settle_s": args.settle,
                "startup_timeout_s": args.timeout,
                "shutdown_timeout_s": args.shutdown_timeout,
                "topology": _parse_env(args.env).get("NEKO_MERGED", "auto"),
            }
            if command == "stack"
            else None
        ),
    }


def _write_json(path: Path, payload: dict[str, Any], *, announce: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if announce:
        print(path.resolve())


async def _embedding_scenario(args: argparse.Namespace) -> dict[str, Any]:
    checkpoints = [
        _in_process_checkpoint(
            "python_baseline",
            seconds=args.window,
            interval=args.interval,
        )
    ]

    from config import (
        VECTORS_EMBEDDING_DIM,
        VECTORS_MIN_RAM_GB,
        VECTORS_MODEL_PROFILE_ID,
        VECTORS_QUANTIZATION,
    )
    from memory.embeddings import EmbeddingService

    checkpoints.append(
        _in_process_checkpoint(
            "embedding_imported",
            seconds=args.window,
            interval=args.interval,
        )
    )
    service = EmbeddingService(
        model_dir=str(Path(args.embedding_root).resolve()),
        enabled=True,
        embedding_dim_setting=VECTORS_EMBEDDING_DIM,
        quantization_setting=VECTORS_QUANTIZATION,
        min_ram_gb=VECTORS_MIN_RAM_GB,
        profile_id=VECTORS_MODEL_PROFILE_ID,
    )
    _register_embedding_service(service)
    ready = await service.request_load()
    if not ready:
        raise RuntimeError(
            f"embedding service did not become READY: {service.disable_reason()}"
        )
    checkpoints.append(
        _in_process_checkpoint(
            "embedding_ready",
            seconds=args.window,
            interval=args.interval,
        )
    )
    vector = await service.embed("N.E.K.O runtime memory baseline")
    if vector is None:
        raise RuntimeError(
            f"embedding inference failed after READY: {service.disable_reason()}"
        )
    checkpoints.append(
        _in_process_checkpoint(
            "embedding_first_inference",
            seconds=args.window,
            interval=args.interval,
        )
    )
    model_id = service.model_id()
    await service.close()
    vector_dimensions = len(vector)
    del vector, service
    checkpoints.append(
        _in_process_checkpoint(
            "embedding_released",
            seconds=args.window,
            interval=args.interval,
            collect=True,
        )
    )
    return {
        "scenario": "embedding",
        "embedding": {
            "ready": ready,
            "model_id": model_id,
            "model_root": str(Path(args.embedding_root).resolve()),
            "vector_dimensions": vector_dimensions,
        },
        "checkpoints": checkpoints,
    }


async def _ocr_scenario(args: argparse.Namespace) -> dict[str, Any]:
    checkpoints = [
        _in_process_checkpoint(
            "python_baseline",
            seconds=args.window,
            interval=args.interval,
        )
    ]
    from PIL import Image
    from plugin.plugins._shared.rapidocr.ocr_rapidocr_backend import RapidOcrBackend
    from plugin.plugins._shared.rapidocr.rapidocr_support import (
        DEFAULT_RAPIDOCR_ENGINE_TYPE,
        DEFAULT_RAPIDOCR_LANG_TYPE,
        DEFAULT_RAPIDOCR_MODEL_TYPE,
        DEFAULT_RAPIDOCR_OCR_VERSION,
    )

    backend = RapidOcrBackend(
        install_target_dir_raw="",
        engine_type=DEFAULT_RAPIDOCR_ENGINE_TYPE,
        lang_type=DEFAULT_RAPIDOCR_LANG_TYPE,
        model_type=DEFAULT_RAPIDOCR_MODEL_TYPE,
        ocr_version=DEFAULT_RAPIDOCR_OCR_VERSION,
        plugin_id="runtime_memory_benchmark",
    )
    available = backend.is_available()
    checkpoints.append(
        _in_process_checkpoint(
            "ocr_imported",
            seconds=args.window,
            interval=args.interval,
        )
    )
    if not available:
        raise RuntimeError("RapidOCR backend is not available")
    text = backend.extract_text(Image.new("RGB", (640, 360), "white"))
    checkpoints.append(
        _in_process_checkpoint(
            "ocr_ready_after_first_inference",
            seconds=args.window,
            interval=args.interval,
        )
    )
    synthetic_text_length = len(text)
    backend.close()
    del text, backend
    checkpoints.append(
        _in_process_checkpoint(
            "ocr_released",
            seconds=args.window,
            interval=args.interval,
            collect=True,
        )
    )
    return {
        "scenario": "ocr",
        "ocr": {"available": available, "synthetic_text_length": synthetic_text_length},
        "checkpoints": checkpoints,
    }


async def _browser_scenario(args: argparse.Namespace) -> dict[str, Any]:
    checkpoints = [
        _in_process_checkpoint(
            "python_baseline",
            seconds=args.window,
            interval=args.interval,
        )
    ]
    from brain.browser_use_adapter import BrowserUseAdapter

    adapter = BrowserUseAdapter(headless=not args.headed)
    checkpoints.append(
        _in_process_checkpoint(
            "browser_use_imported",
            seconds=args.window,
            interval=args.interval,
        )
    )
    session = await adapter._get_browser_session()
    try:
        await session.start()
        adapter._session_ever_started = True
        checkpoints.append(
            _in_process_checkpoint(
                "browser_use_playwright_started",
                seconds=args.window,
                interval=args.interval,
            )
        )
    finally:
        await adapter.close()
    del session, adapter
    checkpoints.append(
        _in_process_checkpoint(
            "browser_use_released",
            seconds=args.window,
            interval=args.interval,
            collect=True,
        )
    )
    return {
        "scenario": "browser_use",
        "browser": {"headless": not args.headed},
        "checkpoints": checkpoints,
    }


def _probe_health(port: int, *, timeout: float = 0.25) -> dict[str, Any] | None:
    try:
        with _LOCAL_PROBE_OPENER.open(
            f"http://127.0.0.1:{port}/health",
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("app") != HEALTH_APP_SIGNATURE or payload.get("status") != "ok":
        return None
    return payload


def _service_health_state(
    ports: list[int],
    roles: list[str],
) -> tuple[bool, dict[str, dict[str, Any]]]:
    state: dict[str, dict[str, Any]] = {}
    instance_ids: set[str] = set()
    for port, expected_role in zip(ports, roles):
        health = _probe_health(port)
        actual_role = str((health or {}).get("service") or "")
        instance_id = str((health or {}).get("instance_id") or "")
        role_ready = bool(health and actual_role == expected_role and instance_id)
        state[expected_role] = {
            "port": port,
            "ready": role_ready,
            "actual_service": actual_role or None,
        }
        if role_ready:
            instance_ids.add(instance_id)
    all_ready = all(item["ready"] for item in state.values())
    same_instance = all_ready and len(instance_ids) == 1
    return same_instance, state


def _probe_http_paths(port: int, paths: list[str]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for raw_path in paths:
        path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
        try:
            with _LOCAL_PROBE_OPENER.open(
                f"http://127.0.0.1:{port}{path}",
                timeout=5.0,
            ) as response:
                body = response.read()
                results[path] = {
                    "status": int(response.status),
                    "content_type": response.headers.get_content_type(),
                    "body_bytes": len(body),
                }
        except urllib.error.HTTPError as exc:
            body = exc.read()
            results[path] = {
                "status": int(exc.code),
                "content_type": exc.headers.get_content_type(),
                "body_bytes": len(body),
                "error": type(exc).__name__,
            }
        except (OSError, urllib.error.URLError) as exc:
            results[path] = {
                "status": None,
                "content_type": None,
                "body_bytes": 0,
                "error": type(exc).__name__,
            }
    return results


def _http_probe_validation_errors(
    probes: dict[str, dict[str, Any]],
) -> list[str]:
    failed_paths = [
        path for path, probe in probes.items() if probe.get("status") != 200
    ]
    if not failed_paths:
        return []
    return ["HTTP route probes failed: " + ", ".join(failed_paths)]


def _assert_ports_available(ports: list[int]) -> None:
    busy: list[int] = []
    for port in ports:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
        except OSError:
            busy.append(port)
        finally:
            probe.close()
    if busy:
        raise RuntimeError(
            "benchmark ports must be free before startup; refusing launcher "
            f"fallback because it would invalidate readiness attribution: {busy}"
        )


def _port_released(port: int) -> bool:
    """Require both no listener and immediate exclusive re-bindability."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return False
    except OSError:
        pass

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _capture_process_tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return []
    try:
        return [root, *root.children(recursive=True)]
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return [root]


def _process_is_alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return False


def _alive_process_pids(processes: Iterable[psutil.Process]) -> list[int]:
    return sorted(process.pid for process in processes if _process_is_alive(process))


def _terminate_process_trees(
    processes: Iterable[subprocess.Popen[Any]],
    observed_processes: Iterable[psutil.Process],
    timeout: float = 12.0,
) -> tuple[list[int], list[int]]:
    targets: dict[tuple[int, float], psutil.Process] = {}
    for target in observed_processes:
        try:
            targets[(target.pid, target.create_time())] = target
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    for process in processes:
        try:
            root = psutil.Process(process.pid)
            current = [root, *root.children(recursive=True)]
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
        for target in current:
            try:
                targets[(target.pid, target.create_time())] = target
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue
    target_list = [target for target in targets.values() if _process_is_alive(target)]
    forced_pids = sorted(target.pid for target in target_list)
    # Teardown is best-effort because processes can exit between discovery and action.
    for target in reversed(target_list):
        with suppress(psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            target.terminate()
    _, alive = psutil.wait_procs(target_list, timeout=timeout)
    for target in alive:
        with suppress(psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            target.kill()
    _gone, residual = psutil.wait_procs(alive, timeout=2.0)
    return forced_pids, sorted(target.pid for target in residual)


async def _run_synthetic_chat(
    *,
    port: int,
    character: str,
    prompt: str,
    timeout: float,
    after_turn: Callable[[], None] | None = None,
) -> dict[str, Any]:
    import websockets

    if not character:

        def _read_current_character() -> dict[str, Any]:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/characters/current_catgirl", timeout=5
            ) as response:
                return json.loads(response.read().decode("utf-8"))

        current = await asyncio.to_thread(_read_current_character)
        character = str(current.get("current_catgirl") or "").strip()
    if not character:
        raise RuntimeError("no character available for synthetic chat")

    request_id = f"memory-baseline-{uuid.uuid4().hex}"
    uri = f"ws://127.0.0.1:{port}/ws/{urllib.parse.quote(character, safe='')}"
    counts: Counter[str] = Counter()
    status_codes: Counter[str] = Counter()
    started_at = time.perf_counter()
    async with websockets.connect(uri, max_size=32 * MIB) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "action": "start_session",
                    "input_type": "text",
                    "new_session": True,
                    "language": "en",
                }
            )
        )
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            message = json.loads(raw)
            message_type = str(message.get("type") or "unknown")
            counts[message_type] += 1
            if message_type == "status":
                try:
                    status = json.loads(message.get("message") or "{}")
                except (TypeError, json.JSONDecodeError):
                    status = {}
                code = str(status.get("code") or "unknown")
                status_codes[code] += 1
            if message_type == "session_failed":
                raise RuntimeError("synthetic chat session failed")
            if message_type == "session_started":
                break

        await websocket.send(
            json.dumps(
                {
                    "action": "stream_data",
                    "input_type": "text",
                    "data": prompt,
                    "request_id": request_id,
                    "source": "runtime_memory_baseline",
                }
            )
        )
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            message = json.loads(raw)
            message_type = str(message.get("type") or "unknown")
            counts[message_type] += 1
            if message_type == "status":
                try:
                    status = json.loads(message.get("message") or "{}")
                except (TypeError, json.JSONDecodeError):
                    status = {}
                status_codes[str(status.get("code") or "unknown")] += 1
            if (
                message_type == "system"
                and str(message.get("data") or "").startswith("turn end")
                and message.get("request_id") == request_id
            ):
                break

        turn_elapsed_s = round(time.perf_counter() - started_at, 3)
        try:
            if after_turn is not None:
                await asyncio.to_thread(after_turn)
        finally:
            await websocket.send(json.dumps({"action": "end_session"}))

    return {
        "elapsed_s": turn_elapsed_s,
        "message_type_counts": dict(sorted(counts.items())),
        "status_code_counts": dict(sorted(status_codes.items())),
        "request_id_prefix": "memory-baseline-",
        "prompt_recorded": False,
    }


def _decode_command(raw: str) -> list[str]:
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        command = [item.strip() for item in raw.split("|")]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise ValueError("command must be a JSON array or a pipe-delimited string")
    return command


def _decode_ports(raw: str) -> list[int]:
    items = [item.strip() for item in raw.split(",")]
    if not items or any(not item for item in items):
        raise ValueError("--ports must be a comma-separated list of port numbers")
    try:
        ports = [int(item) for item in items]
    except ValueError as exc:
        raise ValueError(
            "--ports must be a comma-separated list of port numbers"
        ) from exc
    if any(port < 1 or port > 65535 for port in ports):
        raise ValueError("--ports values must be between 1 and 65535")
    if len(set(ports)) != len(ports):
        raise ValueError("--ports values must be unique")
    return ports


def _parse_env(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise ValueError(f"invalid environment override: {item!r}")
        result[key] = value
    return result


def _request_graceful_shutdown(
    process: subprocess.Popen[Any],
    *,
    ports: list[int],
    timeout: float,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    signal_name = "SIGTERM"
    tracked_processes = _capture_process_tree(process.pid)
    tracked_pids = sorted(target.pid for target in tracked_processes)
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            signal_name = "CTRL_BREAK_EVENT"
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGTERM)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "requested": True,
            "signal": signal_name,
            "graceful": False,
            "elapsed_s": round(time.perf_counter() - started_at, 3),
            "exit_code": process.poll(),
            "ports_closed": False,
            "tracked_pids": tracked_pids,
            "alive_pids": _alive_process_pids(tracked_processes),
            "error": f"{type(exc).__name__}: {exc}",
        }

    deadline = time.monotonic() + timeout
    ports_closed = False
    while time.monotonic() < deadline:
        ports_closed = all(_port_released(port) for port in ports)
        if process.poll() is not None and ports_closed:
            break
        time.sleep(0.1)
    alive_pids = _alive_process_pids(tracked_processes)
    return {
        "requested": True,
        "signal": signal_name,
        "graceful": process.poll() == 0 and ports_closed and not alive_pids,
        "elapsed_s": round(time.perf_counter() - started_at, 3),
        "exit_code": process.poll(),
        "ports_closed": ports_closed,
        "tracked_pids": tracked_pids,
        "alive_pids": alive_pids,
        "error": None,
    }


def _spawn(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    log_path: Path,
) -> tuple[subprocess.Popen[Any], Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    return process, log_file


def _stack(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output).resolve()
    env = os.environ.copy()
    env.update(_parse_env(args.env))
    backend_command = _decode_command(args.backend_command)
    electron_command = (
        _decode_command(args.electron_command) if args.electron_command else None
    )
    ports = _decode_ports(args.ports)
    roles = [item.strip().lower() for item in args.services.split(",") if item.strip()]
    if len(roles) != len(ports) or len(set(roles)) != len(roles):
        raise ValueError("--services must contain one unique role for each --ports entry")
    _assert_ports_available(ports)
    processes: list[tuple[str, subprocess.Popen[Any], Any]] = []
    checkpoints: list[dict[str, Any]] = []
    startup_samples: list[dict[str, Any]] = []
    roots: list[int] = []
    observed_processes: dict[tuple[int, float], psutil.Process] = {}
    started_at = time.perf_counter()
    ports_ready_elapsed_s: float | None = None
    service_ready_elapsed_s: dict[str, float] = {}
    final_health_state: dict[str, dict[str, Any]] = {}
    shutdown_result: dict[str, Any] | None = None
    http_probes: dict[str, dict[str, Any]] = {}
    validation_errors: list[str] = []
    result_payload: dict[str, Any] | None = None

    try:
        backend, backend_log = _spawn(
            backend_command,
            cwd=args.backend_cwd,
            env=env,
            log_path=output.with_suffix(".backend.log"),
        )
        processes.append(("backend", backend, backend_log))
        roots.append(backend.pid)
        deadline = time.monotonic() + args.timeout
        while True:
            services_ready, health_state = _service_health_state(ports, roles)
            elapsed = time.perf_counter() - started_at
            for role, state in health_state.items():
                if state["ready"] and role not in service_ready_elapsed_s:
                    service_ready_elapsed_s[role] = round(elapsed, 3)
            final_health_state = health_state
            if services_ready:
                break
            startup_samples.append(
                _sample(roots, observed_processes=observed_processes)
            )
            if backend.poll() is not None:
                raise RuntimeError(
                    f"backend exited before ready with code {backend.returncode}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"ports did not become ready: {ports}")
            time.sleep(args.interval)
        ports_ready_elapsed_s = time.perf_counter() - started_at
        startup_samples.append(_sample(roots, observed_processes=observed_processes))
        time.sleep(args.settle)
        checkpoints.append(
            _sample_window(
                "cold_start_ready",
                roots,
                seconds=args.window,
                interval=args.interval,
                observed_processes=observed_processes,
            )
        )

        if electron_command is not None:
            electron, electron_log = _spawn(
                electron_command,
                cwd=args.electron_cwd,
                env=env,
                log_path=output.with_suffix(".electron.log"),
            )
            processes.append(("electron", electron, electron_log))
            roots.append(electron.pid)
            time.sleep(args.electron_settle)
            if electron.poll() is not None:
                raise RuntimeError(
                    f"Electron exited early with code {electron.returncode}"
                )
            checkpoints.append(
                _sample_window(
                    "electron_attached",
                    roots,
                    seconds=args.window,
                    interval=args.interval,
                    observed_processes=observed_processes,
                )
            )

        chat_result = None
        if args.synthetic_chat:
            if "main" not in roles:
                raise ValueError("--synthetic-chat requires a main role in --services")

            def _record_live_first_chat() -> None:
                time.sleep(args.settle)
                checkpoints.append(
                    _sample_window(
                        "first_chat_complete_live",
                        roots,
                        seconds=args.window,
                        interval=args.interval,
                        observed_processes=observed_processes,
                    )
                )

            chat_result = asyncio.run(
                _run_synthetic_chat(
                    port=ports[roles.index("main")],
                    character=args.chat_character,
                    prompt=args.chat_prompt,
                    timeout=args.chat_timeout,
                    after_turn=_record_live_first_chat,
                )
            )

        if args.probe_path:
            if "main" not in roles:
                raise ValueError("--probe-path requires a main role in --services")
            http_probes = _probe_http_paths(
                ports[roles.index("main")],
                args.probe_path,
            )
            validation_errors.extend(
                _http_probe_validation_errors(http_probes)
            )

        if args.graceful_shutdown:
            shutdown_result = _request_graceful_shutdown(
                backend,
                ports=ports,
                timeout=args.shutdown_timeout,
            )
            if not shutdown_result["graceful"]:
                validation_errors.append(
                    "graceful shutdown did not exit cleanly with every port released"
                )

        result_payload = {
            "scenario": "stack",
            "ready_contract": "signed_health_same_instance",
            "ports_ready_elapsed_s": round(ports_ready_elapsed_s, 3),
            "services_ready_elapsed_s": service_ready_elapsed_s,
            "service_health": final_health_state,
            "measurement_elapsed_s": round(time.perf_counter() - started_at, 3),
            "ports": ports,
            "startup_window": _series_summary(startup_samples),
            "checkpoints": checkpoints,
            "synthetic_chat": chat_result,
            "http_probes": http_probes,
            "shutdown": shutdown_result,
            "validation_errors": validation_errors,
            "logs": {
                "backend": str(output.with_suffix(".backend.log")),
                "electron": str(output.with_suffix(".electron.log"))
                if electron_command
                else None,
            },
        }
        return result_payload
    finally:
        forced_pids, residual_pids = _terminate_process_trees(
            (process for _name, process, _log_file in processes),
            observed_processes.values(),
        )
        residual_ports = [port for port in ports if not _port_released(port)]
        if result_payload is not None:
            shutdown_tracked_pids = set((shutdown_result or {}).get("tracked_pids", []))
            shutdown_forced_pids = [
                pid for pid in forced_pids if pid in shutdown_tracked_pids
            ]
            result_payload["forced_cleanup"] = {
                "forced_pids": forced_pids,
                "residual_pids": residual_pids,
                "residual_ports": residual_ports,
            }
            if shutdown_result is not None:
                shutdown_result["forced_pids"] = shutdown_forced_pids
            if args.graceful_shutdown and shutdown_forced_pids:
                validation_errors.append(
                    "graceful shutdown required forced process cleanup: "
                    + ", ".join(str(pid) for pid in shutdown_forced_pids)
                )
            if residual_pids or residual_ports:
                validation_errors.append(
                    "forced cleanup left residual processes or bound ports"
                )
        for _name, _process, log_file in processes:
            log_file.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="JSON output path")
    parser.add_argument("--interval", type=float, default=DEFAULT_SAMPLE_INTERVAL)
    parser.add_argument("--window", type=float, default=3.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scenario = subparsers.add_parser(
        "scenario", help="Run an in-process lazy feature scenario"
    )
    scenario.add_argument("name", choices=("embedding", "ocr", "browser-use"))
    scenario.add_argument("--embedding-root", default="data/embedding_models")
    scenario.add_argument("--headed", action="store_true")

    stack = subparsers.add_parser(
        "stack", help="Measure a launcher/Electron process tree"
    )
    stack.add_argument(
        "--backend-command",
        required=True,
        help="JSON array or a pipe-delimited command",
    )
    stack.add_argument("--backend-cwd", default=str(Path.cwd()))
    stack.add_argument(
        "--electron-command", help="JSON array or a pipe-delimited command"
    )
    stack.add_argument("--electron-cwd", default=str(Path.cwd()))
    stack.add_argument("--ports", default="48911,48912,48915")
    stack.add_argument("--services", default=",".join(DEFAULT_SERVICE_ROLES))
    stack.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    stack.add_argument("--timeout", type=float, default=150.0)
    stack.add_argument("--settle", type=float, default=5.0)
    stack.add_argument("--electron-settle", type=float, default=12.0)
    stack.add_argument("--graceful-shutdown", action="store_true")
    stack.add_argument("--shutdown-timeout", type=float, default=40.0)
    stack.add_argument("--probe-path", action="append", default=[])
    stack.add_argument("--synthetic-chat", action="store_true")
    stack.add_argument("--chat-character", default="")
    stack.add_argument(
        "--chat-prompt",
        default="Reply with exactly OK. This is a runtime memory benchmark.",
    )
    stack.add_argument("--chat-timeout", type=float, default=90.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.interval <= 0 or args.window < 0:
        raise SystemExit("--interval must be positive and --window cannot be negative")

    metadata = _metadata(args)
    if args.command == "scenario":
        tracemalloc.start(10)
        if args.name == "embedding":
            result = asyncio.run(_embedding_scenario(args))
        elif args.name == "ocr":
            result = asyncio.run(_ocr_scenario(args))
        else:
            result = asyncio.run(_browser_scenario(args))
    else:
        result = _stack(args)

    payload = {"metadata": metadata, **result}
    _write_json(Path(args.output), payload)
    if result.get("validation_errors"):
        print("benchmark validation failed: " + "; ".join(result["validation_errors"]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
