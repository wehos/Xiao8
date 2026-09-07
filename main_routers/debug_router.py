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

"""Diagnostic observability: long-run health metric collection.

Background
----------
Occasional field reports say "after N.E.K.O has been running for two or three
days, CPU slowly climbs to 30%+". For this kind of multi-day, idle-triggered
leak, static code reading has a single-digit hit rate — we must capture
runtime counter curves **while it reproduces**. This router does two things:

1. ``GET /api/debug/health``: returns a snapshot of the key counters (asyncio
   task count, each lanlan core's conversation history length,
   agent_event_bus._ack_waiters size, proactive_chat_history size, process
   RSS, uptime).
2. Starts a background watchdog task on a 5-min cycle that writes the same
   snapshot into an in-memory ring buffer (keeping the last ~16 hours = 200
   entries). With ``NEKO_DEBUG_HEALTH_LOG=1`` it also persists to
   ``<user_data>/debug_health.jsonl`` so users can send the file back for
   curve plotting.

Design principles
-----------------
- **Zero intrusion**: every counter uses getattr / try-except; if this module
  breaks, main functionality is unaffected.
- **On by default**: the endpoint + in-memory ring buffer always run, at ~ms
  cost per pass; file logging is off by default, enabled explicitly via env.
  When a user later reports an issue, no new build is needed — the data is
  already there to collect.
- **No privacy capture**: snapshots only count sizes, never read contents;
  the jsonl contains no conversation text (per the CLAUDE.md rule that raw
  conversations may only be printed, never logged via logger).
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import math
import os
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter
from main_logic.voice_identity_service.diagnostics import (
    VOICE_IDENTITY_DIAGNOSTIC_COUNTERS,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# 模块级状态
# ---------------------------------------------------------------------------

_PROCESS_START_MONO = time.monotonic()
# Ring buffer：~16 小时（5min × 200）。重启即丢，这是诊断而非审计，可接受。
_HEALTH_RING: deque[dict[str, Any]] = deque(maxlen=200)
_WATCHDOG_TASK: asyncio.Task | None = None
_WATCHDOG_INTERVAL_SECONDS = 5 * 60  # 5 分钟
_VOICE_IDENTITY_DIAGNOSTICS_PROVIDER: Callable[[], dict[str, int]] | None = None
# 「Deep」字段（``gc.get_objects()`` 45ms + Windows ``num_threads()`` 8ms）
# 是 C 调用不释放 GIL，watchdog 跑就直接阻塞 event loop 50ms+。本来想降频
# 到 30min 一次缓和，但用户体感「致命」否决——索性 watchdog **不收**这俩，
# 只在按需 endpoint ``/api/debug/health?deep=1`` 触发。代价：ring/jsonl 没
# 有 gc 时序数据；用户排查内存问题时手动多次访问 endpoint 自己构建时序。

# psutil.Process 复用：cpu_percent(None) 必须**用同一个 Process 实例**多次调用，
# 第一次返回 0（建立基线），之后每次返回距上次的 CPU 利用率%。每次 new Process
# 都是新基线 → 永远 0%，等于没采。`None` 表示尚未尝试初始化或环境缺 psutil。
# ⚠️ 双 channel 隔离：cpu_percent baseline 在同实例上被共享——watchdog 和
# endpoint 共用一个 Process 时，任意 endpoint 调用都会重置 watchdog 的窗口
# 起点，导致下次 watchdog 拿到的 cpu_percent 不是真实 5min 窗口（可能短到几
# 秒）。所以维护**两个独立** Process 实例：``watchdog`` channel 给 5min 周期
# task 用，``endpoint`` channel 给按需 HTTP 用，两个 baseline 互不影响。
# 其他 psutil 调用（memory_info / num_handles / num_threads）都是无状态瞬时
# 查询，用哪个实例都一样——所以这俩 channel 在它们身上等价。
_PSUTIL_PROCESS_WATCHDOG: Any = None
_PSUTIL_PROCESS_ENDPOINT: Any = None
_PSUTIL_INIT_TRIED = False


# ---------------------------------------------------------------------------
# Snapshot 采集
# ---------------------------------------------------------------------------

def _get_psutil_process(channel: str = "watchdog") -> Any:
    """Lazily initialize + reuse ``psutil.Process``. One instance per channel.

    The first call tries to import psutil and primes both instances'
    ``cpu_percent`` (by convention the first call returns 0 to establish a
    baseline; only later readings are meaningful). On failure it permanently
    returns None and never retries — this avoids repeated import noise and
    makes psutil-less environments report cpu_percent/num_handles as null
    across the curve, clearly distinct from "an actual leak".

    ``channel="watchdog"`` / ``"endpoint"`` selects an independent baseline
    instance. Other stateless psutil calls (memory_info/num_handles etc.) also
    go through here; either channel works, defaulting to watchdog."""
    global _PSUTIL_PROCESS_WATCHDOG, _PSUTIL_PROCESS_ENDPOINT, _PSUTIL_INIT_TRIED
    if not _PSUTIL_INIT_TRIED:
        _PSUTIL_INIT_TRIED = True
        try:
            import psutil  # type: ignore
            w = psutil.Process()
            e = psutil.Process()
            # Prime 两个实例：首次调用约定回 0，立刻丢掉，下次才有真值。
            w.cpu_percent(interval=None)
            e.cpu_percent(interval=None)
            _PSUTIL_PROCESS_WATCHDOG = w
            _PSUTIL_PROCESS_ENDPOINT = e
        except Exception:
            _PSUTIL_PROCESS_WATCHDOG = None
            _PSUTIL_PROCESS_ENDPOINT = None
    return _PSUTIL_PROCESS_ENDPOINT if channel == "endpoint" else _PSUTIL_PROCESS_WATCHDOG


def _safe_rss_mb(channel: str = "watchdog") -> float | None:
    """Read the process's **current** RSS (MB); only when psutil is available, otherwise None.

    Historically ``resource.getrusage(...).ru_maxrss`` was used as a fallback,
    but that is the **lifetime peak** — once up, it never comes down. Using it
    for leak trends would permanently misread a one-off memory spike as a leak,
    more misleading than not having the field at all. So we **prefer returning
    None** over falling back to ru_maxrss. Packaged builds ship psutil by
    default, and source mode installs it via ``uv sync``."""
    proc = _get_psutil_process(channel)
    if proc is None:
        return None
    try:
        return proc.memory_info().rss / (1024 * 1024)
    except Exception:
        # 进程突然不存在 / 权限丢失等极端态：返 None 不挂诊断。
        return None


def _safe_psutil_extras(channel: str = "watchdog") -> dict[str, Any]:
    """Add the cheap psutil metrics: cpu%, Windows handles / POSIX fds.

    All calls take < 0.01 ms (measured on Windows). ``num_threads()`` is an
    ~8 ms syscall on Windows and was moved to ``_safe_psutil_heavy()`` on the
    deep tick.

    - cpu_percent: **the golden metric for the original problem** — the 31.9%
      in Task Manager is exactly this. ⚠️ Key normalization:
      ``proc.cpu_percent(None)`` uses UNIX semantics (one full core = 100;
      multi-core parallelism can exceed 100%), but Task Manager shows "percent
      of total CPU" (one core maxed out of 8 = 12.5%). To make the curve
      **directly match user screenshots**, divide by ``cpu_count``. Also
      report ``cpu_percent_raw`` with the UNIX raw value for "how many cores
      are busy" scenarios.
    - num_handles / num_fds: Windows handle / POSIX fd leaks. Most
      "restart fixes it" cases correlate with this. Try Windows num_handles
      first, then POSIX num_fds; None when neither is available.
    """
    out: dict[str, Any] = {
        "cpu_percent": None,       # 任务管理器规模（占总 CPU 百分比）
        "cpu_percent_raw": None,   # psutil 原始值（占单核百分比，多核可 > 100）
        "cpu_count": None,
        "num_handles": None,
    }
    proc = _get_psutil_process(channel)
    if proc is None:
        return out
    try:
        import psutil  # type: ignore
        cpu_count = psutil.cpu_count() or 1
        raw = proc.cpu_percent(interval=None)
        out["cpu_percent_raw"] = raw
        out["cpu_count"] = cpu_count
        # 归一化到任务管理器规模：raw / cpu_count
        out["cpu_percent"] = raw / cpu_count
    except Exception:
        # 故意吞：psutil 子调用（cpu_count / cpu_percent）失败留 None 即可，
        # 曲线上看到 cpu_percent=null 知道是 psutil 异常不是「真有 leak」。
        pass
    # Windows: num_handles; POSIX: num_fds. psutil 在错误平台抛 AttributeError。
    try:
        out["num_handles"] = proc.num_handles()
    except Exception:
        try:
            out["num_handles"] = proc.num_fds()
        except Exception:
            # 故意吞：两个平台 API 都拿不到（容器 / 罕见 OS），num_handles 留 None。
            pass
    return out


def _safe_psutil_heavy(channel: str = "watchdog") -> dict[str, Any]:
    """Slow psutil calls — ``num_threads()`` is an 8 ms syscall on Windows.

    Deep-tick only; not run on every watchdog cycle."""
    out: dict[str, Any] = {"num_threads": None}
    proc = _get_psutil_process(channel)
    if proc is None:
        return out
    try:
        out["num_threads"] = proc.num_threads()
    except Exception:
        # 故意吞：num_threads 拿不到留 None，零侵入语义保留。
        pass
    return out


def _safe_asyncio_task_top(n: int = 10) -> list[list[Any]] | None:
    """Count current asyncio tasks by name; return the top N.

    When ``asyncio.all_tasks()`` only yields a number, a long-run leak shows
    "task count grew" without saying which kind — this distribution pinpoints
    it instantly (e.g. "memory_recall_xxx" climbing steadily). Returns
    list[[name, count], ...] rather than a dict — preserves ordering, JSON friendly."""
    try:
        c: Counter[str] = Counter()
        for t in asyncio.all_tasks():
            try:
                c[t.get_name()] += 1
            except Exception:
                c["<unnamed>"] += 1
        return [[name, cnt] for name, cnt in c.most_common(n)]
    except Exception:
        return None


def _safe_gc_object_top(n: int = 10) -> list[list[Any]] | None:
    """Count all GC-tracked objects by type; return the top N.

    A single ``gc.get_objects()`` pass takes ~tens of ms on a mid-sized Python
    heap; entirely acceptable every 5 min. This is the golden signal for
    "**which objects are growing**" — when the RSS number climbs without saying
    who, the type top tells you directly whether ``HumanMessage`` / ``AIMessage``
    / ``Future`` / ``Task`` / ``dict`` is increasing monotonically."""
    try:
        c: Counter[str] = Counter()
        for obj in gc.get_objects():
            c[type(obj).__name__] += 1
        return [[name, cnt] for name, cnt in c.most_common(n)]
    except Exception:
        return None


def _safe_tts_queue_sizes() -> dict[str, int]:
    """Current length of each lanlan core's ``tts_request_queue``.

    When TTS stalls / the network flaps, queue buildup shows up before CPU —
    an early signal that "the TTS pipeline is in trouble".
    core.tts_request_queue is a ``queue.Queue``; qsize is an estimate on
    Windows but good enough."""
    out: dict[str, int] = {}
    try:
        from main_routers.shared_state import get_session_manager
        session_manager = get_session_manager()
        for name in list(session_manager.keys()):
            try:
                core = session_manager.get(name)
                q = getattr(core, "tts_request_queue", None)
                if q is not None and hasattr(q, "qsize"):
                    out[name] = q.qsize()
            except Exception:
                continue
    except Exception:
        return out
    return out


def _safe_is_responding_map() -> dict[str, bool]:
    """Each lanlan's ``session._is_responding`` state.

    An abnormal distribution (e.g. every lanlan stuck at True) is a strong
    signal of a deadlock / a response handler dropping messages."""
    out: dict[str, bool] = {}
    try:
        from main_routers.shared_state import get_session_manager
        session_manager = get_session_manager()
        for name in list(session_manager.keys()):
            try:
                core = session_manager.get(name)
                session = getattr(core, "session", None)
                if session is None:
                    continue
                v = getattr(session, "_is_responding", None)
                if isinstance(v, bool):
                    out[name] = v
            except Exception:
                continue
    except Exception:
        return out
    return out


def _safe_conv_history_lengths() -> dict[str, int]:
    """Enumerate the _conversation_history length of every lanlan core.

    Any lanlan we cannot reach is skipped — shared_state may not be ready during early startup."""
    out: dict[str, int] = {}
    try:
        from main_routers.shared_state import get_session_manager
        session_manager = get_session_manager()
        # session_manager 是 _RoleStateFieldView，dict-like
        for name in list(session_manager.keys()):
            try:
                core = session_manager.get(name)
                session = getattr(core, "session", None)
                history = getattr(session, "_conversation_history", None)
                if history is not None:
                    out[name] = len(history)
            except Exception:
                # 单 lanlan 失败不影响其他：可能正在 end_session / hot-swap，
                # core / session 暂态为 None，下一轮自然恢复。
                continue
    except Exception:
        # shared_state 启动早期可能还没 ready；故意吞，零侵入。
        return out
    return out


def _safe_ack_waiters_size() -> int | None:
    try:
        from main_logic.agent_event_bus import _ack_waiters
        return len(_ack_waiters)
    except Exception:
        # 故意吞：agent_event_bus 模块未加载 / 重构改名都允许优雅降级。
        return None


def _safe_proactive_history_size() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        from main_routers.system_router import _proactive_chat_history
        for name, dq in list(_proactive_chat_history.items()):
            try:
                out[name] = len(dq)
            except Exception:
                # 单条 deque 取 len 失败极小概率：跳过不让整轮废。
                continue
    except Exception:
        # 故意吞：system_router 未加载 / 内部命名变更都允许优雅降级。
        return out
    return out


def set_voice_identity_diagnostics_provider(
    provider: Callable[[], dict[str, int]] | None,
) -> None:
    """Install the app-owned aggregate diagnostics reader."""

    if provider is not None and not callable(provider):
        raise TypeError("provider must be callable or None")
    global _VOICE_IDENTITY_DIAGNOSTICS_PROVIDER
    _VOICE_IDENTITY_DIAGNOSTICS_PROVIDER = provider


def _safe_voice_identity_diagnostics() -> dict[str, int]:
    """Read aggregate voice-filter counters without biometric material."""

    provider = _VOICE_IDENTITY_DIAGNOSTICS_PROVIDER
    if provider is None:
        return {}
    try:
        raw = provider()
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        name: value
        for name, value in raw.items()
        if name in VOICE_IDENTITY_DIAGNOSTIC_COUNTERS
        and type(value) is int
        and value >= 0
    }


def _collect_snapshot(include_deep: bool = False, channel: str = "watchdog") -> dict[str, Any]:
    """Take a single snapshot. Every field is individually try-wrapped — one blowing up does not affect the others.

    Default ``include_deep=False`` — cheap-only sampling costs ~0.05ms of its
    own time; the watchdog can call it synchronously with virtually no impact
    on the event loop.

    ``include_deep=True`` runs the slow fields: ``gc.get_objects()`` 45ms
    (memory object type distribution — the golden signal for "which objects
    are growing") + Windows ``num_threads()`` 8ms (thread leak detection).
    Both are C calls that do not release the GIL and block the event loop
    50ms+, so the **watchdog never calls it** — reserved for on-demand
    endpoint triggering only.

    ``channel`` selects the psutil cpu_percent baseline: ``"watchdog"`` is
    dedicated to the 5-min periodic task, ``"endpoint"`` to on-demand HTTP;
    the two baselines are independent — a user hitting the endpoint cannot
    reset the watchdog's window start."""
    snap: dict[str, Any] = {
        "ts": time.time(),
        "uptime_sec": time.monotonic() - _PROCESS_START_MONO,
    }
    try:
        snap["asyncio_tasks"] = len(asyncio.all_tasks())
    except Exception:
        snap["asyncio_tasks"] = None
    snap["asyncio_task_top"] = _safe_asyncio_task_top()
    snap["rss_mb"] = _safe_rss_mb(channel)
    # psutil extras 一次 dict 展平进顶层，方便画曲线时直接索引同级 key。
    snap.update(_safe_psutil_extras(channel))
    snap["conv_history"] = _safe_conv_history_lengths()
    snap["tts_queue_size"] = _safe_tts_queue_sizes()
    snap["is_responding"] = _safe_is_responding_map()
    snap["ack_waiters"] = _safe_ack_waiters_size()
    snap["proactive_history"] = _safe_proactive_history_size()
    snap["voice_identity"] = _safe_voice_identity_diagnostics()
    try:
        from main_logic.widget_mode_runtime import widget_mode_coordinator

        snap["widget_mode"] = widget_mode_coordinator.snapshot()
    except Exception as exc:
        snap["widget_mode"] = {"error": str(exc)[:160]}
    if include_deep:
        # Deep 字段——~50 ms 阻塞但有 30 min 间隔，长跑曲线仍能画时序。
        snap["gc_object_top"] = _safe_gc_object_top()
        snap.update(_safe_psutil_heavy(channel))
    return snap


# ---------------------------------------------------------------------------
# 文件落盘（默认关）
# ---------------------------------------------------------------------------

def _resolve_log_path() -> Path | None:
    """Return the jsonl log path; None when disabled.

    Enabled when env ``NEKO_DEBUG_HEALTH_LOG`` is truthy.
    Path: the user config directory from config_manager / ``debug_health.jsonl``;
    falls back to the sys.executable directory when config_manager is unavailable."""
    if os.environ.get("NEKO_DEBUG_HEALTH_LOG", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        from main_routers.shared_state import get_config_manager
        cm = get_config_manager()
        config_dir = getattr(cm, "config_dir", None)
        if config_dir:
            return Path(config_dir) / "debug_health.jsonl"
    except Exception:
        # shared_state 没 ready / config_manager 未注入：落到下面 sys.argv[0]
        # 兜底路径。本身就是诊断文件，写哪里都比不写好。
        pass
    # 兜底：launcher 旁
    try:
        return Path(sys.argv[0]).resolve().parent / "debug_health.jsonl"
    except Exception:
        return None


# 单文件大小上限。超过则 rotate 到 .1（覆盖旧 .1），总占用硬封 ~20MB。
# 算下来：3 个 lanlan + client merged 行 ≈ 500B，10MB ≈ 21000 行 ≈ 73 天数据，
# 触发 rotation 后还能再写 73 天到新文件——对「报完问题忘关 env」场景完全够用。
_LOG_ROTATE_BYTES = 10 * 1024 * 1024


def _append_to_log(snap: dict[str, Any]) -> None:
    path = _resolve_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate：超阈值则 os.replace 到 .1。os.replace 在 Windows / POSIX 都
        # 原子，且会原子覆盖已有 .1——所以总占用 = current + .1 ≤ 2×10MB。
        # 用 path.name + ".1" 而不是 with_suffix('.1')——后者会把 .jsonl 替成
        # .1 得到 debug_health.1，丢了 .jsonl 后缀。
        try:
            if path.exists() and path.stat().st_size > _LOG_ROTATE_BYTES:
                os.replace(path, path.parent / (path.name + ".1"))
        except OSError as e:
            # rotation 失败不挂主路径，让 append 照旧写——大不了文件继续涨
            # 一阵子，下次 tick 还会再试。
            logger.debug("debug_health: rotate failed: %s", e)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    except Exception as e:
        # 文件写失败不抛——诊断功能不能拖垮主程序
        logger.debug("debug_health: append jsonl failed: %s", e)


# ---------------------------------------------------------------------------
# Watchdog 后台任务
# ---------------------------------------------------------------------------

def _absorb_recent_client_payload(server_snap: dict[str, Any]) -> None:
    """On a server tick, look back and absorb the most recent client-only entry (if within the window).

    Timing constraints: debug-health.js first POSTs at t=30s, the watchdog
    first ticks at t=60s, and both then run on a 5-min cadence — so a client
    POST usually lands ~30s **before** the "next" server tick. The client POST
    side appends a client-only entry as a stash; the server tick absorbs it
    here: merge the stashed client payload into the current server snapshot
    and pop the stashed entry off the ring (so the ring's retention is not
    halved).

    Multiple client-only entries: a single cycle may see several client POSTs
    (multiple tabs / beforeunload resend + periodic reporting). The while loop
    pops all trailing consecutive client-only entries to avoid leaving
    orphans; only the **latest** payload is merged into the server snapshot —
    latest = last to arrive = closest to the server tick, highest diagnostic
    value.

    If the client side is not enabled (user did not set localStorage), this
    does nothing; the ring stays all server-only entries, ~200 entries ≈ 16
    hours, unchanged."""
    absorbed_client: dict[str, Any] | None = None
    server_ts = float(server_snap.get("ts") or 0)
    # 倒序消化尾部连续 client-only 条目；保留最新（第一次循环取到的）payload。
    while _HEALTH_RING:
        last = _HEALTH_RING[-1]
        # 「client-only」标识：缺 asyncio_tasks 键（server snapshot 永远有）
        if "asyncio_tasks" in last:
            break
        # 超出吸收窗口的属于「上上轮残留」，不动它（也不吸收 payload）让 ring
        # 自然按 maxlen 排出，避免强行 pop 破坏历史顺序。
        if server_ts - float(last.get("ts") or 0) > _WATCHDOG_INTERVAL_SECONDS:
            break
        if absorbed_client is None and last.get("client") is not None:
            absorbed_client = last["client"]
        _HEALTH_RING.pop()
    if absorbed_client is not None:
        server_snap["client"] = absorbed_client


async def _watchdog_loop() -> None:
    """5-min periodic sampling. Any single-round exception is swallowed and the loop continues — a multi-day run must not drop out over one failure.

    ⚠️ Key point: ``_collect_snapshot()`` is synchronous; its
    ``gc.get_objects()`` scans 280k+ objects on N.E.K.O's actual heap taking
    ~55 ms, with ``psutil`` / file IO adding a few more. A direct
    ``snap = _collect_snapshot()`` would **block the event loop for 50-100 ms**
    — during which every async operation (voice chunk processing, TTS
    streaming, WS ping/pong) is delayed. So collect must run in the thread
    pool via ``asyncio.to_thread`` while the event loop keeps working. The
    append/log operations (≤1ms) stay on the loop."""
    # 启动后先睡一段，避开冷启动 noise（asyncio task 数在 startup 阶段会高一下）。
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        return
    while True:
        try:
            # Watchdog **不收 deep 字段**——cheap snapshot 自身 < 0.05ms，直接同
            # 步调反而比 to_thread 更轻（to_thread 有线程调度 overhead）。Deep
            # 字段（gc / num_threads）改成按需 endpoint，详见 _collect_snapshot
            # 注释。
            snap = _collect_snapshot(include_deep=False)
            _absorb_recent_client_payload(snap)
            _HEALTH_RING.append(snap)
            # 文件 IO 可能十几 ms（rotation 时 os.replace）：丢 thread 避免阻塞。
            await asyncio.to_thread(_append_to_log, snap)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("debug_health watchdog single tick error: %s", e)
        try:
            await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return


def start_watchdog() -> None:
    """Called from main_server startup. Idempotent: repeated calls do not create a second task."""
    global _WATCHDOG_TASK
    if _WATCHDOG_TASK is not None and not _WATCHDOG_TASK.done():
        return
    try:
        _WATCHDOG_TASK = asyncio.create_task(_watchdog_loop(), name="debug_health_watchdog")
        logger.info("debug_health watchdog started (interval=%ds, log_file=%s)",
                    _WATCHDOG_INTERVAL_SECONDS, _resolve_log_path())
    except RuntimeError:
        # 没有 running loop——startup 路径不该走到这里
        logger.warning("debug_health: no running loop, watchdog NOT started")


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------

@router.get("/api/debug/health")
async def debug_health(deep: bool = False) -> dict[str, Any]:
    """Return the current snapshot + the recent ring buffer.

    The ring buffer means users need not wait for the next 5-min tick — a
    request at any moment returns the last 16 hours of curves, fresh on
    refresh.

    ``deep=true`` triggers the slow fields (``gc.get_objects()`` 45ms memory
    object type distribution + Windows ``num_threads()`` 8ms thread count).
    The watchdog never calls them — when chasing a memory leak, hit
    ``?deep=1`` once manually for current data; call repeatedly for a time
    series.

    Implementation note: the cheap path costs ~0.13 ms of its own time and is
    called synchronously; the deep path's 50 ms block is the price of a
    **user-initiated** action — they can just wait. No to_thread fallback for
    this case: saves a layer of thread scheduling overhead, keeps the code
    direct.

    Passes ``channel="endpoint"`` for an independent psutil cpu_percent
    baseline that does not disturb the watchdog's 5-min window."""
    current = _collect_snapshot(include_deep=deep, channel="endpoint")
    # Resolve log path once instead of calling _resolve_log_path() twice
    _log_path = _resolve_log_path()
    return {
        "current": current,
        "ring": list(_HEALTH_RING),
        "ring_capacity": _HEALTH_RING.maxlen,
        "watchdog_interval_sec": _WATCHDOG_INTERVAL_SECONDS,
        "log_path": str(_log_path) if _log_path else None,
    }


# 端点接受的客户端 payload 白名单。HTTP 边界做这层约束有两个理由：
# (1) 协议契约——「只记计数」必须在边界强制而不是依赖前端自觉，否则任何调用方
#     都能往 ring/jsonl 写入大对象或敏感字段；
# (2) 文件占用——单次 payload bound 住，长跑 jsonl 不会被异常调用爆出 GB 级。
# 字段名跟 static/debug-health.js 的 ``collectSnapshot()`` 同步。新增字段时
# 两边一起改，未在白名单的会被静默丢弃。
_CLIENT_NUMERIC_FIELDS = frozenset({
    "ts", "live_intervals", "live_timeouts", "raf_fps_60s",
    "dom_nodes", "js_heap_mb", "ws_state",
    "proactive_backoff_level", "agent_task_map_size",
    # 新增（与 debug-health.js collectSnapshot 同步）
    "live_object_urls", "error_count", "unhandled_rejection_count",
})
_CLIENT_BOOL_FIELDS = frozenset({"proactive_running", "is_recording"})
_CLIENT_STRING_FIELDS_WITH_CAP = {"location": 128}


def _sanitize_client_payload(raw: Any) -> dict[str, Any]:
    """Trim the client payload to whitelisted fields; unexpected types / over-long strings are dropped or truncated."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _CLIENT_NUMERIC_FIELDS:
            if v is None:
                out[k] = v
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                # 拒 NaN / Infinity / 1e10000：stdlib json 默认会输出 "NaN" /
                # "Infinity" 字面量（非标准 JSON），前端 JSON.parse / 第三方
                # jsonl 工具直接挂。try-except 同时兜超大 int（10**500 等）：
                # math.isfinite 内部把 int 转 float 时会 OverflowError，那种
                # 数字在「只记计数」语义里也不该出现，一并丢。
                try:
                    if math.isfinite(v):
                        out[k] = v
                except (OverflowError, TypeError):
                    # 故意丢：超大 int / 异常 numeric subtype 进不来 isfinite。
                    # 「只记计数」语义里本来不该出现，丢弃即可，不影响其他字段。
                    pass
        elif k in _CLIENT_BOOL_FIELDS:
            if isinstance(v, bool):
                out[k] = v
        elif k in _CLIENT_STRING_FIELDS_WITH_CAP:
            if isinstance(v, str):
                cap = _CLIENT_STRING_FIELDS_WITH_CAP[k]
                out[k] = v[:cap]
        # 其余字段静默丢弃——「只记计数」契约由这里强制
    return out


@router.post("/api/debug/health/client")
async def debug_health_client(payload: dict[str, Any]) -> dict[str, Any]:
    """Browser-side snapshot POSTed by the frontend ``debug-health.js``.

    Flow:
    - Boundary whitelist trimming (``_sanitize_client_payload``) enforces the
      "counts only" contract.
    - Append a client-only entry to the in-memory ring as a stash — do **not**
      write jsonl immediately, so the next server tick's absorption cannot
      produce a "stash + merged" double line polluting the jsonl timeline.
    - Writing jsonl is entirely the watchdog's responsibility: a successful
      absorption yields a single merged line; a failed one (outside the window
      or server not started) is naturally dropped by the ring.

    Mismatch fix history: an earlier attempt searched backwards for a server
    entry to merge into here; codex pointed out it would bind the client
    sample to a server snapshot up to 4.5 min old, skewing the timeline by
    270s. Hence the "server tick absorbs backwards" design — see
    ``_absorb_recent_client_payload``."""
    try:
        sanitized = _sanitize_client_payload(payload)
        entry = {"ts": time.time(), "client": sanitized}
        _HEALTH_RING.append(entry)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
