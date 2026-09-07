#!/usr/bin/env python3
"""Run repeatable 2-8 hour lifecycle soak probes for N.E.K.O.

All workload inputs are deterministic synthetic data. The JSON contains only
aggregate process/resource measurements, status codes, and exception types; it
never records prompts, responses, command lines, environment variables, or API
payloads. Chat is opt-in because it must target a separately started backend
whose storage isolation has already been verified by the operator.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import statistics
import time
import tracemalloc
from pathlib import Path
from typing import Any, Awaitable, Callable

from scripts.runtime_memory_baseline import (
    MIB,
    _in_process_checkpoint,
    _metadata,
    _register_embedding_service,
    _run_synthetic_chat,
    _write_json,
)


SYNTHETIC_EMBEDDING_TEXT = "Synthetic runtime lifecycle probe."
SYNTHETIC_CHAT_PROMPT = "Reply with exactly OK. This is a synthetic soak probe."
FEATURE_NAMES = (
    "audio",
    "embedding",
    "ocr",
    "browser-use",
    "plugin-reload",
    "chat",
)


class FeatureUnavailable(RuntimeError):
    """A feature cannot run in the current environment."""


def _checkpoint(
    args: argparse.Namespace,
    label: str,
    *,
    collect: bool = False,
    root_pids: list[int] | None = None,
) -> dict[str, Any]:
    checkpoint = _in_process_checkpoint(
        label,
        seconds=args.window,
        interval=args.interval,
        collect=collect,
        root_pids=root_pids,
    )
    if not args.retain_process_rows:
        checkpoint.pop("last_processes", None)
    return checkpoint


async def _released_checkpoint(
    args: argparse.Namespace,
    label: str,
) -> dict[str, Any]:
    if args.cooldown:
        await asyncio.sleep(args.cooldown)
    return _checkpoint(args, label, collect=True)


async def _audio_cycle(args: argparse.Namespace, cycle: int) -> dict[str, Any]:
    import numpy as np

    from utils.audio_processor import AudioProcessor

    # Fixed PCM ramp: no microphone input or conversation audio is read.
    synthetic_pcm = (
        ((np.arange(480 * args.audio_chunks, dtype=np.int32) % 2048) - 1024)
        .astype(np.int16)
        .tobytes()
    )
    processor = AudioProcessor(
        input_sample_rate=48_000,
        output_sample_rate=16_000,
        noise_reduce_enabled=True,
        agc_enabled=True,
        limiter_enabled=True,
    )
    native_denoiser = processor._denoiser is not None
    output_bytes = 0
    try:
        if not native_denoiser:
            raise FeatureUnavailable("native_denoiser_unavailable")
        for _ in range(2):
            output_bytes += len(processor.process_chunk(synthetic_pcm))
            processor.set_enabled(False)
            processor.set_enabled(True)
        active = _checkpoint(args, f"cycle_{cycle:04d}_audio_active")
    finally:
        processor.close()
    del processor, synthetic_pcm
    released = await _released_checkpoint(args, f"cycle_{cycle:04d}_audio_released")
    return {
        "status": "completed",
        "details": {
            "synthetic_input": True,
            "native_denoiser": native_denoiser,
            "output_bytes": output_bytes,
        },
        "checkpoints": [active, released],
    }


async def _embedding_cycle(args: argparse.Namespace, cycle: int) -> dict[str, Any]:
    from config import (
        VECTORS_EMBEDDING_DIM,
        VECTORS_MIN_RAM_GB,
        VECTORS_MODEL_PROFILE_ID,
        VECTORS_QUANTIZATION,
    )
    from memory.embeddings import EmbeddingService

    model_root = Path(args.embedding_root).resolve()
    if not model_root.is_dir():
        raise FeatureUnavailable("embedding_model_root_missing")
    service = EmbeddingService(
        model_dir=str(model_root),
        enabled=True,
        embedding_dim_setting=VECTORS_EMBEDDING_DIM,
        quantization_setting=VECTORS_QUANTIZATION,
        min_ram_gb=VECTORS_MIN_RAM_GB,
        profile_id=VECTORS_MODEL_PROFILE_ID,
    )
    _register_embedding_service(service)
    vector: list[float] | None = None
    try:
        if not await service.request_load():
            reason = service.disable_reason() or "embedding_load_unavailable"
            raise FeatureUnavailable(reason)
        vector = await service.embed(SYNTHETIC_EMBEDDING_TEXT)
        if vector is None:
            raise RuntimeError("embedding_inference_returned_none")
        active = _checkpoint(args, f"cycle_{cycle:04d}_embedding_active")
    finally:
        await service.close()
    dimensions = len(vector) if vector is not None else 0
    del vector, service
    released = await _released_checkpoint(args, f"cycle_{cycle:04d}_embedding_released")
    return {
        "status": "completed",
        "details": {"synthetic_input": True, "vector_dimensions": dimensions},
        "checkpoints": [active, released],
    }


async def _ocr_cycle(args: argparse.Namespace, cycle: int) -> dict[str, Any]:
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
    text = ""
    try:
        if not backend.is_available():
            raise FeatureUnavailable("rapidocr_unavailable")
        text = backend.extract_text(Image.new("RGB", (640, 360), "white"))
        active = _checkpoint(args, f"cycle_{cycle:04d}_ocr_active")
    finally:
        backend.close()
    synthetic_text_length = len(text)
    del text, backend
    released = await _released_checkpoint(args, f"cycle_{cycle:04d}_ocr_released")
    return {
        "status": "completed",
        "details": {
            "synthetic_input": True,
            "synthetic_text_length": synthetic_text_length,
        },
        "checkpoints": [active, released],
    }


async def _browser_cycle(args: argparse.Namespace, cycle: int) -> dict[str, Any]:
    from brain.browser_use_adapter import BrowserUseAdapter

    adapter = BrowserUseAdapter(headless=not args.headed)
    if not adapter._ready_import:
        raise FeatureUnavailable("browser_use_import_unavailable")
    session = await adapter._get_browser_session()
    try:
        await session.start()
        adapter._session_ever_started = True
        active = _checkpoint(args, f"cycle_{cycle:04d}_browser_use_active")
    finally:
        await adapter.close()
    del session, adapter
    released = await _released_checkpoint(
        args, f"cycle_{cycle:04d}_browser_use_released"
    )
    return {
        "status": "completed",
        "details": {"headless": not args.headed},
        "checkpoints": [active, released],
    }


async def _plugin_cycle(args: argparse.Namespace, cycle: int) -> dict[str, Any]:
    from plugin.core.host import PluginProcessHost

    config_path = Path(args.plugin_config).resolve()
    if not config_path.is_file():
        raise FeatureUnavailable("synthetic_plugin_config_missing")
    host = PluginProcessHost(
        plugin_id="runtime_memory_soak_fixture",
        entry_point=args.plugin_entry,
        config_path=config_path,
    )
    triggered = False
    try:
        await host.start(
            message_target_queue=asyncio.Queue(),
            startup_timeout=args.plugin_timeout,
            startup_failure="fail",
        )
        result = await host.trigger("late_entry", {}, timeout=args.plugin_timeout)
        triggered = isinstance(result, dict) and result.get("ok") is True
        if not triggered:
            raise RuntimeError("synthetic_plugin_trigger_failed")
        active = _checkpoint(args, f"cycle_{cycle:04d}_plugin_reload_active")
    finally:
        with contextlib.suppress(Exception):
            await host.shutdown(timeout=args.plugin_timeout)
    del host
    released = await _released_checkpoint(
        args, f"cycle_{cycle:04d}_plugin_reload_released"
    )
    return {
        "status": "completed",
        "details": {"synthetic_plugin": True, "triggered": triggered},
        "checkpoints": [active, released],
    }


async def _chat_cycle(args: argparse.Namespace, cycle: int) -> dict[str, Any]:
    if args.chat_port <= 0:
        raise FeatureUnavailable("verified_isolated_chat_endpoint_not_configured")
    if args.chat_pid <= 0:
        raise FeatureUnavailable("verified_isolated_chat_backend_pid_not_configured")
    checkpoints: list[dict[str, Any]] = []

    def _record_live_session() -> None:
        checkpoints.append(
            _checkpoint(
                args,
                f"cycle_{cycle:04d}_chat_active",
                root_pids=[args.chat_pid],
            )
        )

    result = await _run_synthetic_chat(
        port=args.chat_port,
        character=args.chat_character,
        prompt=SYNTHETIC_CHAT_PROMPT,
        timeout=args.chat_timeout,
        after_turn=_record_live_session,
    )
    if args.cooldown:
        await asyncio.sleep(args.cooldown)
    checkpoints.append(
        _checkpoint(
            args,
            f"cycle_{cycle:04d}_chat_released",
            collect=True,
            root_pids=[args.chat_pid],
        )
    )
    return {
        "status": "completed",
        "details": result,
        "checkpoints": checkpoints,
    }


FEATURE_RUNNERS: dict[
    str, Callable[[argparse.Namespace, int], Awaitable[dict[str, Any]]]
] = {
    "audio": _audio_cycle,
    "embedding": _embedding_cycle,
    "ocr": _ocr_cycle,
    "browser-use": _browser_cycle,
    "plugin-reload": _plugin_cycle,
    "chat": _chat_cycle,
}


def _parse_features(raw: str) -> list[str]:
    features: list[str] = []
    for item in raw.split(","):
        name = item.strip().lower()
        if not name:
            continue
        if name not in FEATURE_RUNNERS:
            raise argparse.ArgumentTypeError(
                f"unknown feature {name!r}; choose from {', '.join(FEATURE_NAMES)}"
            )
        if name not in features:
            features.append(name)
    if not features:
        raise argparse.ArgumentTypeError("at least one feature is required")
    return features


def _slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    denominator = sum((value - x_mean) ** 2 for value in xs)
    if denominator <= 0:
        return None
    slope_per_second = (
        sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    )
    return round(slope_per_second * 3600.0, 3)


def _series(points: list[tuple[float, float]]) -> dict[str, Any]:
    if not points:
        return {"samples": 0}
    values = [value for _elapsed, value in points]
    return {
        "samples": len(values),
        "first": round(values[0], 3),
        "last": round(values[-1], 3),
        "delta": round(values[-1] - values[0], 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "slope_per_hour": _slope_per_hour(points),
    }


def _released_checkpoints(payload: dict[str, Any]) -> list[dict[str, Any]]:
    released: list[dict[str, Any]] = []
    for cycle in payload.get("cycles", []):
        cycle_checkpoint = cycle.get("released_checkpoint")
        if isinstance(cycle_checkpoint, dict):
            released.append(cycle_checkpoint)
    return released


def _feature_released_checkpoints(
    payload: dict[str, Any], feature: str
) -> list[dict[str, Any]]:
    released: list[dict[str, Any]] = []
    for cycle in payload.get("cycles", []):
        result = cycle.get("features", {}).get(feature, {})
        for checkpoint in result.get("checkpoints", []):
            if str(checkpoint.get("label", "")).endswith("_released"):
                released.append(checkpoint)
    return released


def _checkpoint_series(
    checkpoints: list[dict[str, Any]], start_perf: float
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    metrics: dict[str, list[tuple[float, float]]] = {
        "rss_mib": [],
        "uss_mib": [],
        "threads": [],
        "handles": [],
        "onnx_map_count": [],
        "onnx_mapped_rss_mib": [],
        "traced_current_mib": [],
        "traced_peak_mib": [],
        "chromium_processes": [],
    }
    latest_resources: dict[str, Any] = {}
    for checkpoint in checkpoints:
        elapsed = float(checkpoint.get("captured_perf_counter", 0.0)) - start_perf
        total = checkpoint.get("total", {})
        traced = checkpoint.get("tracemalloc", {})
        chromium = checkpoint.get("categories", {}).get("chromium", {})
        values = {
            "rss_mib": total.get("median_rss_mib"),
            "uss_mib": total.get("median_uss_mib"),
            "threads": total.get("median_threads"),
            "handles": total.get("median_handles"),
            "onnx_map_count": total.get("median_onnx_map_count"),
            "onnx_mapped_rss_mib": total.get("median_onnx_mapped_rss_mib"),
            "traced_current_mib": traced.get("current_mib"),
            "traced_peak_mib": traced.get("peak_mib"),
            "chromium_processes": chromium.get("median_count", 0),
        }
        for name, value in values.items():
            if value is not None:
                metrics[name].append((elapsed, float(value)))
        latest_resources = checkpoint.get("resources", latest_resources)
    return {name: _series(points) for name, points in metrics.items()}, latest_resources


def _trend_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    start_perf = float(payload.get("started_perf_counter", 0.0))
    series, latest_resources = _checkpoint_series(
        _released_checkpoints(payload), start_perf
    )
    feature_series = {
        feature: _checkpoint_series(
            _feature_released_checkpoints(payload, feature), start_perf
        )[0]
        for feature in payload.get("contract", {}).get("features", [])
    }
    signals: list[str] = []
    uss = series["uss_mib"]
    traced = series["traced_current_mib"]
    handles = series["handles"]
    threads = series["threads"]
    chromium = series["chromium_processes"]
    onnx_maps = series["onnx_map_count"]
    if chromium.get("last", 0) > 0:
        signals.append("chromium_children_remain_after_release")
    if latest_resources.get("rapidocr_cache_owners", 0) > 0:
        signals.append("rapidocr_owner_remains_after_release")
    if latest_resources.get("embedding_session_refs", 0) > 0:
        signals.append("embedding_session_reference_remains_after_release")
    if int(uss.get("samples", 0)) >= 5:
        uss_slope = float(uss.get("slope_per_hour") or 0.0)
        traced_slope = float(traced.get("slope_per_hour") or 0.0)
        if uss_slope >= 16.0 and traced_slope >= 8.0:
            signals.append("sustained_python_and_process_growth_review_required")
        elif uss_slope >= 16.0 and abs(traced_slope) < 8.0:
            signals.append("native_or_allocator_retention_review_required")
    if float(handles.get("delta") or 0.0) >= 32:
        signals.append("handle_growth_review_required")
    if float(threads.get("delta") or 0.0) >= 4:
        signals.append("thread_growth_review_required")
    if onnx_maps.get("last", 0) > 0 and not any(
        latest_resources.get(name, 0)
        for name in ("embedding_session_refs", "rapidocr_cache_owners")
    ):
        signals.append("onnx_dll_or_model_mapping_residency_without_python_owner")
    return {
        "released_series": series,
        "feature_released_series": feature_series,
        "latest_resource_counts": latest_resources,
        "heuristic_signals": signals,
        "interpretation": (
            "Signals are triage hints only. Confirm a leak from sustained USS/resource "
            "growth across many released cycles; RSS-only growth can be allocator or mapping residency."
        ),
    }


def _allocation_growth(
    before: tracemalloc.Snapshot,
    after: tracemalloc.Snapshot,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    root = Path.cwd().resolve()
    for stat in after.compare_to(before, "lineno")[:limit]:
        frame = stat.traceback[0]
        path = Path(frame.filename)
        try:
            display_path = str(path.resolve().relative_to(root))
        except (OSError, ValueError):
            display_path = path.name
        rows.append(
            {
                "file": display_path,
                "line": frame.lineno,
                "size_diff_mib": round(stat.size_diff / MIB, 6),
                "count_diff": stat.count_diff,
            }
        )
    return rows


def _persist(output: Path, payload: dict[str, Any]) -> None:
    temporary = output.with_suffix(output.suffix + ".tmp")
    _write_json(temporary, payload, announce=False)
    temporary.replace(output)


async def _run_soak(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output).resolve()
    started_perf = time.perf_counter()
    deadline = started_perf + args.duration_hours * 3600.0
    payload: dict[str, Any] = {
        "metadata": _metadata(args),
        "scenario": "soak",
        "started_perf_counter": started_perf,
        "contract": {
            "duration_hours": args.duration_hours,
            "max_cycles": args.cycles,
            "features": args.features,
            "synthetic_inputs_only": True,
            "chat_requires_verified_isolated_endpoint": True,
            "raw_payloads_recorded": False,
        },
        "cycles": [],
        "disabled_features": {},
        "completed_cycles": 0,
    }
    disabled: dict[str, str] = {}
    before = tracemalloc.take_snapshot()
    cycle = 0
    try:
        while time.perf_counter() < deadline and (
            args.cycles <= 0 or cycle < args.cycles
        ):
            cycle += 1
            cycle_result: dict[str, Any] = {
                "cycle": cycle,
                "started_elapsed_s": round(time.perf_counter() - started_perf, 3),
                "features": {},
            }
            for feature in args.features:
                if feature in disabled:
                    cycle_result["features"][feature] = {
                        "status": "skipped",
                        "reason": disabled[feature],
                        "checkpoints": [],
                    }
                    continue
                runner = FEATURE_RUNNERS[feature]
                try:
                    cycle_result["features"][feature] = await runner(args, cycle)
                except FeatureUnavailable as exc:
                    reason = str(exc)[:160] or "feature_unavailable"
                    disabled[feature] = reason
                    cycle_result["features"][feature] = {
                        "status": "skipped",
                        "reason": reason,
                        "checkpoints": [],
                    }
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - preserve soak after one feature failure
                    cycle_result["features"][feature] = {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "checkpoints": [],
                    }
            cycle_result["released_checkpoint"] = await _released_checkpoint(
                args, f"cycle_{cycle:04d}_all_released"
            )
            cycle_result["elapsed_s"] = round(time.perf_counter() - started_perf, 3)
            payload["cycles"].append(cycle_result)
            payload["completed_cycles"] = cycle
            payload["disabled_features"] = dict(disabled)
            payload["analysis"] = _trend_analysis(payload)
            _persist(output, payload)
            summary = cycle_result["released_checkpoint"]["total"]
            print(
                f"cycle={cycle} elapsed_s={cycle_result['elapsed_s']} "
                f"rss_mib={summary.get('median_rss_mib')} "
                f"uss_mib={summary.get('median_uss_mib')}",
                flush=True,
            )
            if args.cycle_pause and time.perf_counter() < deadline:
                await asyncio.sleep(args.cycle_pause)
    finally:
        gc.collect()
        after = tracemalloc.take_snapshot()
        payload["completed_elapsed_s"] = round(time.perf_counter() - started_perf, 3)
        payload["disabled_features"] = dict(disabled)
        payload["analysis"] = _trend_analysis(payload)
        payload["tracemalloc_top_growth"] = _allocation_growth(before, after)
        _persist(output, payload)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Aggregate JSON output path")
    parser.add_argument("--duration-hours", type=float, default=2.0)
    parser.add_argument(
        "--cycles", type=int, default=0, help="Optional cycle cap; 0 is unlimited"
    )
    parser.add_argument("--features", type=_parse_features, default=list(FEATURE_NAMES))
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--window", type=float, default=0.5)
    parser.add_argument("--cooldown", type=float, default=1.0)
    parser.add_argument("--cycle-pause", type=float, default=0.0)
    parser.add_argument("--embedding-root", default="data/embedding_models")
    parser.add_argument("--audio-chunks", type=int, default=20)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--plugin-config",
        default="plugin/tests/fixtures/neko_plugin_cli/plugins/simple_plugin/plugin.toml",
    )
    parser.add_argument(
        "--plugin-entry",
        default="tests.fixtures.plugin_test_dynamic_entry_fixture:DynamicEntryFixturePlugin",
    )
    parser.add_argument("--plugin-timeout", type=float, default=5.0)
    parser.add_argument("--chat-port", type=int, default=0)
    parser.add_argument(
        "--chat-pid",
        type=int,
        default=0,
        help="PID of the verified isolated chat backend sampled with its descendants",
    )
    parser.add_argument("--chat-character", default="")
    parser.add_argument("--chat-timeout", type=float, default=90.0)
    parser.add_argument(
        "--retain-process-rows",
        action="store_true",
        help="Keep per-PID rows at every checkpoint (larger and self-retaining)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.duration_hours <= 0 or args.duration_hours > 8:
        raise SystemExit("--duration-hours must be greater than 0 and no more than 8")
    if args.cycles < 0:
        raise SystemExit("--cycles cannot be negative")
    if (
        args.interval <= 0
        or args.window < 0
        or args.cooldown < 0
        or args.cycle_pause < 0
    ):
        raise SystemExit(
            "interval must be positive; window/cooldown/pause cannot be negative"
        )
    if args.audio_chunks < 1 or args.plugin_timeout <= 0:
        raise SystemExit("--audio-chunks and --plugin-timeout must be positive")
    tracemalloc.start(10)
    asyncio.run(_run_soak(args))
    print(Path(args.output).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
