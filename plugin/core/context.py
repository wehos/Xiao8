"""
插件上下文模块

提供插件运行时上下文，包括状态更新和消息推送功能。
"""
import contextlib
import contextvars
import asyncio
import base64
import copy
import queue
import threading
import time
try:
    import tomllib
except ImportError:
    import tomli as tomllib
import uuid
import functools

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, cast

try:
    import ormsgpack
except ImportError:  # pragma: no cover
    ormsgpack = None  # type: ignore[assignment]

try:
    import zmq
except ImportError:  # pragma: no cover
    zmq = None

from fastapi import FastAPI

from plugin.core.state import state
from plugin.settings import (
    EXPORT_INLINE_BINARY_MAX_BYTES,
    PLUGIN_LOG_CTX_MESSAGE_PUSH,
    PLUGIN_LOG_CTX_STATUS_UPDATE,
    PLUGIN_LOG_SYNC_CALL_WARNINGS,
    SYNC_CALL_IN_HANDLER_POLICY,
)

if TYPE_CHECKING:
    from plugin.core.bus.types import BusHubProtocol
    from plugin.core.bus.events import EventClient
    from plugin.core.bus.lifecycle import LifecycleClient
    from plugin.core.bus.memory import MemoryClient
    from plugin.core.bus.messages import MessageClient
    from plugin.core.bus.conversations import ConversationClient
    from plugin.core.bus.frames import FrameClient
    from plugin.sdk.shared.core.types import PushMessageRejected, PushMessageResult
    # ⚠ 严禁 import loguru。logger 字段实际类型是 plugin.logging_config.PluginLoggerAdapter。
    from plugin.logging_config import PluginLoggerAdapter as LoguruLogger


_IN_HANDLER: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("plugin_in_handler", default=None)

_CURRENT_RUN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("plugin_current_run_id", default=None)


def _is_submission_backpressure(error: BaseException) -> bool:
    """Return whether a non-blocking local submission path was full."""
    if isinstance(error, (asyncio.QueueFull, queue.Full)):
        return True
    again_type = getattr(zmq, "Again", None) if zmq is not None else None
    return isinstance(again_type, type) and isinstance(error, again_type)


# base64 spends 4 wire bytes for every 3 raw bytes, and the rest of the
# envelope is scalars plus whatever text parts ride along -- a few hundred
# bytes at most.  So this ratio is what turns MESSAGE_PLANE_PAYLOAD_MAX_BYTES
# into the raw-bytes budget an author can actually aim at, and it is the number
# the rejection log prints.  It only holds while an inline payload rides the
# wire ONCE: the envelope used to carry a raw duplicate in the legacy
# ``binary_data`` field as well, which put the real ratio at ~2.34x.
_INLINE_BASE64_WIRE_RATIO = 4.0 / 3.0

# Label for the deprecated top-level ``binary_data`` field in the rejection
# log.  It is not a part type and is deliberately not mapped onto one: it only
# survives translation when the caller passed it next to an explicit ``parts=``
# list, and in that shape nothing in the payload says what those bytes are.
_LEGACY_BINARY_CARRIER = "binary_data"


def _inline_binary_carriers(
    parts: Any, legacy_binary_data: Any
) -> tuple[tuple[str, int], ...]:
    """Return ``(carrier label, wire bytes)`` per inline payload, in wire order.

    This used to double as a gate: an empty tuple skipped the size probe
    entirely, on the theory that only inline bytes can realistically blow
    MESSAGE_PLANE_PAYLOAD_MAX_BYTES.  That gate is gone.  The host measures the
    WHOLE envelope, so an oversized text or metadata push was dropped there
    while push_message() had already answered submitted=True -- and the cost it
    was avoiding turned out to be 0.19us per typical cue, measured.  Every
    payload is probed now, and an empty tuple here means only "nothing travels
    inline", never "skip the check".

    The labels and sizes exist so the rejection can name the payload that
    actually blew the cap.  ``ctx.images.upload()`` is the remedy for an image
    and for nothing else -- there is no audio or video upload helper today --
    so a rejection that always pointed there sent the author of an inline
    audio part hunting for an API that does not exist (Codex).

    Both carriers are reported because they can appear independently:
    ``parts[].binary_base64`` is the canonical one, while ``binary_data`` is
    the legacy field that :func:`translate_push_message` leaves untranslated
    when a caller passes v2 ``parts`` and the deprecated ``binary_data=``
    kwarg together.  Either one alone is enough to reach the cap.
    """
    carriers: list[tuple[str, int]] = []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            blob = part.get("binary_base64")
            if not isinstance(blob, str) or not blob:
                continue
            part_type = part.get("type")
            label = part_type if isinstance(part_type, str) and part_type else "unknown"
            carriers.append((label, len(blob)))
    if isinstance(legacy_binary_data, (bytes, bytearray)) and legacy_binary_data:
        carriers.append((_LEGACY_BINARY_CARRIER, len(legacy_binary_data)))
    return tuple(carriers)


def _inline_carrier_totals(
    carriers: tuple[tuple[str, int], ...]
) -> list[tuple[str, int]]:
    """Aggregate carriers per label, biggest total first.

    Aggregating before ranking is what makes "which one blew the cap" answer
    the question the author is actually asking: ten thumbnails that together
    outweigh one voice clip are the thing to fix, even though the clip is the
    single largest part.  The label breaks ties so the log line is stable.
    """
    totals: Dict[str, int] = {}
    for label, size in carriers:
        totals[label] = totals.get(label, 0) + size
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def _synthesize_legacy_message_type(canonical: Dict[str, Any]) -> str:
    """Best-effort legacy ``message_type`` for v1 consumers.

    Inspects ``canonical['parts']`` for ui_action shapes that map onto the
    deprecated music_* discriminators; otherwise classifies the call by
    visibility/ai_behavior so query_service still has a non-empty type.
    """
    parts = canonical.get("parts") or []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "ui_action":
            action = p.get("action")
            if action == "media_play_url":
                return "music_play_url"
            if action == "media_allowlist_add":
                return "music_allowlist_add"
    if canonical.get("ai_behavior") in ("respond", "read"):
        return "proactive_notification"
    return "text"


def _synthesize_legacy_content(parts: list) -> Optional[str]:
    """Concatenate text parts so query_service has a non-empty ``content``."""
    pieces: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            t = p.get("text")
            if isinstance(t, str) and t:
                pieces.append(t)
    return "\n".join(pieces) if pieces else None


class _BusHub:
    def __init__(self, ctx: "PluginContext"):
        self._ctx = ctx

    @functools.cached_property
    def memory(self) -> "MemoryClient":
        from plugin.core.bus.memory import MemoryClient

        return MemoryClient(self._ctx)

    @functools.cached_property
    def messages(self) -> "MessageClient":
        from plugin.core.bus.messages import MessageClient

        return MessageClient(self._ctx)

    @functools.cached_property
    def events(self) -> "EventClient":
        from plugin.core.bus.events import EventClient

        return EventClient(self._ctx)

    @functools.cached_property
    def lifecycle(self) -> "LifecycleClient":
        from plugin.core.bus.lifecycle import LifecycleClient

        return LifecycleClient(self._ctx)

    @functools.cached_property
    def conversations(self) -> "ConversationClient":
        from plugin.core.bus.conversations import ConversationClient

        return ConversationClient(self._ctx)

    @functools.cached_property
    def frames(self) -> "FrameClient":
        from plugin.core.bus.frames import FrameClient

        return FrameClient(self._ctx)


# 宿主在写 message plane 之前会对记录做规范化，所以它打包出来的字节比 SDK 在
# push_message() 里量到的多。这段余量被 _reject_if_payload_too_large 扣掉，好让
# "刚好卡在上限下沿"的推送被同步拒掉，而不是拿到 submitted=True 之后在 ingest
# 那边被静默丢弃。
#
# **推导出来，不是挑出来的**——挑过一版 128，实测被打脸：宿主盖的 plugin_id
# 最长 128 字符，光它一项就能把漂移推到 222 字节。三项贡献：
#
#   plugin_id   宿主按认证身份盖上，key + 值（≤ _HOST_PLUGIN_ID_MAX_CHARS）
#   message_id  缺失时补 uuid4 字符串（36 字符）
#   time        缺失或是 float（fast_mode）时换成 ISO 串（28 字符）
#
# 每项再算上 msgpack 的 key 与长度头。放模块级而不是埋在函数里，是因为守卫要
# 读它——守卫按**最坏形状**（两者都缺 + 最长 plugin_id）实测，所以这个数不够
# 会红，而不是悄悄把窗口放回来。
_HOST_PLUGIN_ID_MAX_CHARS = 128
_HOST_ENVELOPE_HEADROOM_BYTES = (
    (4 + len("plugin_id") + _HOST_PLUGIN_ID_MAX_CHARS)
    + (4 + len("message_id") + 36)
    + (4 + len("time") + 28)
)


@dataclass
class PluginContext:
    """插件运行时上下文"""
    plugin_id: str
    config_path: Path
    logger: "LoguruLogger"
    status_queue: Any
    message_queue: Any = None  # 消息推送队列
    app: Optional[FastAPI] = None
    _plugin_comm_queue: Optional[Any] = None  # 插件间通信队列（主进程提供）
    _zmq_ipc_client: Optional[Any] = None
    _cmd_queue: Optional[Any] = None  # 命令队列（用于在等待期间处理命令）
    _res_queue: Optional[Any] = None  # 结果队列（用于在等待期间处理响应）
    _response_queue: Optional[Any] = None
    _response_pending: Optional[Dict[str, Any]] = None
    _direct_response_waiters: Optional[Dict[str, Any]] = None
    _direct_response_lock: Any = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _image_transport: Optional[Any] = None
    _images: Optional[Any] = None
    _model_gateway_base_url: str = field(default="", repr=False)
    _model_gateway_token: str = field(default="", repr=False)
    _model_gateway_closed: bool = field(default=False, init=False, repr=False)
    _models: Optional[Any] = field(default=None, init=False, repr=False)
    _image_uploads_blocked: bool = False
    _entry_map: Optional[Dict[str, Any]] = None  # 入口映射（用于处理命令）
    _entry_meta_map: Optional[Dict[str, Any]] = None  # entry_id -> EventMeta
    _instance: Optional[Any] = None  # 插件实例（用于处理命令）
    _bus_hub: Optional[Any] = None
    _restored_from_freeze: bool = False  # 标记是否从冻结状态恢复
    _effective_config: Optional[Dict[str, Any]] = None
    _effective_config_uncertain: bool = False
    _current_lanlan: Optional[str] = None

    @property
    def bus(self) -> "BusHubProtocol":
        hub = self._bus_hub
        if hub is None:
            hub = _BusHub(self)
            self._bus_hub = hub
        return cast("BusHubProtocol", hub)

    @property
    def images(self) -> Any:
        images = self._images
        if images is None:
            from plugin.sdk.shared.core.images import PluginImages

            images = PluginImages(self)
            self._images = images
        return images

    @property
    def models(self) -> Any:
        with self._direct_response_lock:
            models = self._models
            if models is None:
                from plugin.sdk.shared.core.models import PluginModels

                models = PluginModels(self)
                self._models = models
            return models

    async def _upload_image(
        self,
        data: bytes,
        *,
        mime: str,
        deadline: float | None = None,
        timeout: float,
    ) -> dict[str, object]:
        """Upload one image within ``timeout`` TOTAL.

        ``deadline`` is a monotonic instant established by the caller before it
        began any work on this upload. Without it the legs each got a fresh
        ``timeout`` — send, then wait — so an upload could take past twice its
        advertised budget and overrun a timer or entry handler's own deadline.
        The decode gate widened that further, since queueing for a slot happens
        before the transport is even touched (Codex).
        """
        self._ensure_image_upload_available()
        transport = self._image_transport
        if transport is None:
            raise RuntimeError("temporary image transport is not available")
        if timeout <= 0:
            raise ValueError("image upload timeout must be positive")

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._direct_response_lock:
            waiters = self._direct_response_waiters
            if waiters is None:
                waiters = {}
                self._direct_response_waiters = waiters
            waiters[request_id] = (loop, future)
        try:
            def _remaining() -> float:
                if deadline is None:
                    return timeout
                return deadline - asyncio.get_running_loop().time()

            # The host starts timer and custom-event handler threads BEFORE the
            # downlink loop begins reading, and _on_command_loop_start can await
            # for as long as it likes in between. An upload launched in that
            # window is sent to nobody: the reply has no reader, so it can only
            # time out (Codex).
            #
            # Waiting is the honest answer rather than refusing, because those
            # handlers are legitimate uploaders the moment the loop is up -- a
            # refusal keyed on handler NAME would also reject them afterwards.
            # The wait is charged to the SAME deadline, so it is never a second
            # budget stacked on the caller's: a plugin that asked for three
            # seconds still gets an answer within three seconds.
            ready = getattr(self, "_downlink_ready", None)
            while ready is not None and not ready.is_set():
                remaining = _remaining()
                if remaining <= 0:
                    raise TimeoutError(
                        f"image upload timed out after {timeout}s "
                        "(plugin downlink not ready)"
                    )
                await asyncio.sleep(min(0.02, remaining))

            send_budget = _remaining()
            if send_budget <= 0:
                raise TimeoutError(f"image upload timed out after {timeout}s")
            await transport.send_image(
                request_id,
                mime=mime,
                data=data,
                timeout=send_budget,
            )
            wait_budget = _remaining()
            if wait_budget <= 0:
                raise TimeoutError(f"image upload timed out after {timeout}s")
            try:
                response = await asyncio.wait_for(future, timeout=wait_budget)
            except asyncio.TimeoutError:
                raise TimeoutError(f"image upload timed out after {timeout}s") from None
            return self._unwrap_image_upload_response(response)
        finally:
            with self._direct_response_lock:
                waiters.pop(request_id, None)

    def _ensure_image_upload_available(self) -> None:
        if self._image_uploads_blocked:
            raise RuntimeError("ctx.images.upload() is not available while the plugin is freezing")

    def _dispatch_direct_response(self, response: Any) -> bool:
        """Resolve SDK-owned response futures before the legacy shared inbox."""
        if not isinstance(response, dict) or response.get("type") != "IMAGE_UPLOAD_RESULT":
            return False
        request_id = response.get("request_id")
        with self._direct_response_lock:
            waiters = self._direct_response_waiters
            waiter = waiters.get(request_id) if waiters and request_id else None
        if waiter is not None:
            loop, future = waiter
            try:
                loop.call_soon_threadsafe(
                    self._resolve_direct_response,
                    future,
                    response,
                )
            except RuntimeError:
                pass
        # A late image result is owned by this path too; don't leak it into the
        # plugin-to-plugin response inbox where it can confuse correlation.
        return True

    @staticmethod
    def _resolve_direct_response(
        future: asyncio.Future[Any],
        response: dict[str, Any],
    ) -> None:
        if not future.done():
            future.set_result(response)

    @staticmethod
    def _cancel_direct_response(future: asyncio.Future[Any]) -> None:
        if not future.done():
            future.cancel()

    @staticmethod
    def _unwrap_image_upload_response(response: dict[str, Any]) -> dict[str, object]:
        error = response.get("error")
        if error:
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or "image upload failed"
            else:
                message = str(error)
            raise RuntimeError(str(message))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("image upload returned no image part")
        return dict(result)

    def close(self) -> None:
        """Release resources owned directly by this context.

        This is safe to call multiple times.
        """
        self._model_gateway_closed = True
        models = getattr(self, "_models", None)
        if models is not None:
            models.close()
        with self._direct_response_lock:
            waiters = getattr(self, "_direct_response_waiters", None)
            pending = tuple(waiters.values()) if waiters else ()
            if waiters:
                waiters.clear()
        for loop, future in pending:
            try:
                loop.call_soon_threadsafe(self._cancel_direct_response, future)
            except RuntimeError:
                pass

        zmq_client = getattr(self, "_zmq_ipc_client", None)
        if zmq_client is not None:
            try:
                close_fn = getattr(zmq_client, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                pass
            try:
                self._zmq_ipc_client = None
            except Exception:
                pass

    def __del__(self) -> None:  # pragma: no cover - best-effort safety net
        try:
            self.close()
        except Exception:
            pass

    def _refresh_instance_runtime_config(self, effective_config: Dict[str, Any]) -> None:
        instance = getattr(self, "_instance", None)
        refresh = getattr(instance, "refresh_runtime_config", None)
        if not callable(refresh):
            return
        try:
            refresh(effective_config)
        except Exception as exc:
            try:
                self.logger.warning(
                    "[PluginContext] Failed to refresh runtime config: plugin_id={}, err_type={}, err={}",
                    self.plugin_id,
                    type(exc).__name__,
                    str(exc),
                )
            except Exception:
                pass

    def _set_effective_config_cache(self, config_obj: object) -> Dict[str, Any] | None:
        if not isinstance(config_obj, dict):
            self._effective_config = None
            self._effective_config_uncertain = False
            return None
        config_copy = copy.deepcopy(config_obj)
        self._effective_config = config_copy
        self._effective_config_uncertain = False
        self._refresh_instance_runtime_config(config_copy)
        return config_copy

    @staticmethod
    def _merge_config_copy(base: object, updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = copy.deepcopy(base) if isinstance(base, dict) else {}
        for key, value in updates.items():
            if not isinstance(key, str):
                continue
            if value == "__DELETE__":
                merged.pop(key, None)
                continue
            if isinstance(value, Mapping):
                value_mapping = {
                    nested_key: nested_value
                    for nested_key, nested_value in value.items()
                    if isinstance(nested_key, str)
                }
                if value_mapping.get("__replace__") is True:
                    merged[key] = {
                        nested_key: copy.deepcopy(nested_value)
                        for nested_key, nested_value in value_mapping.items()
                        if nested_key != "__replace__"
                    }
                    continue
                current = merged.get(key)
                if isinstance(current, Mapping):
                    current_mapping = {
                        nested_key: nested_value
                        for nested_key, nested_value in current.items()
                        if isinstance(nested_key, str)
                    }
                    merged[key] = (
                        PluginContext._merge_config_copy(current_mapping, value_mapping)
                        if value_mapping
                        else {}
                    )
                else:
                    merged[key] = copy.deepcopy(value_mapping)
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    def _get_sync_call_in_handler_policy(self) -> str:
        """获取同步调用策略，优先使用插件自身配置，其次使用全局配置。

        有效值："warn" / "reject"。任何非法值都会回退到全局策略。
        """
        try:
            st = self.config_path.stat()
            cache_mtime = getattr(self, "_a1_policy_mtime", None)
            cache_value = getattr(self, "_a1_policy_value", None)
            if cache_mtime == st.st_mtime and isinstance(cache_value, str):
                return cache_value

            with self.config_path.open("rb") as f:
                conf = tomllib.load(f)
            policy = (
                conf.get("plugin", {})
                .get("safety", {})
                .get("sync_call_in_handler")
            )
            if policy not in ("warn", "reject"):
                policy = SYNC_CALL_IN_HANDLER_POLICY
            self._a1_policy_mtime = st.st_mtime
            self._a1_policy_value = policy
            return policy
        except Exception:
            return SYNC_CALL_IN_HANDLER_POLICY

    def _enforce_sync_call_policy(self, method_name: str) -> None:
        handler_ctx = _IN_HANDLER.get()
        if handler_ctx is None:
            return
        # If no event loop is running on the current thread, we are in a
        # thread-pool thread (e.g. asyncio.to_thread).  Sync IPC calls are
        # safe there — they only block that worker thread, not the loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        policy = self._get_sync_call_in_handler_policy()
        msg = (
            f"Sync call '{method_name}' invoked inside handler ({handler_ctx}). "
            "This may block the event loop and cause deadlocks/timeouts. "
            "Use the async variant or wrap in asyncio.to_thread()."
        )
        if policy == "reject":
            raise RuntimeError(msg)
        if PLUGIN_LOG_SYNC_CALL_WARNINGS:
            self.logger.warning(msg)

    @contextlib.contextmanager
    def _handler_scope(self, handler_ctx: str):
        token = _IN_HANDLER.set(handler_ctx)
        try:
            yield
        finally:
            _IN_HANDLER.reset(token)

    @contextlib.contextmanager
    def _run_scope(self, run_id: Optional[str]):
        token = _CURRENT_RUN_ID.set(run_id if isinstance(run_id, str) and run_id.strip() else None)
        try:
            yield
        finally:
            _CURRENT_RUN_ID.reset(token)

    @property
    def handler_ctx(self) -> Optional[str]:
        return _IN_HANDLER.get()

    @property
    def current_entry_id(self) -> Optional[str]:
        handler_ctx = self.handler_ctx
        if not isinstance(handler_ctx, str):
            return None
        prefix = "plugin_entry."
        if not handler_ctx.startswith(prefix):
            return None
        entry_id = handler_ctx[len(prefix):].strip()
        return entry_id or None

    def get_current_entry_meta(self) -> Optional[Any]:
        entry_id = self.current_entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return None

        entry_meta_map = getattr(self, "_entry_meta_map", None)
        if isinstance(entry_meta_map, dict):
            entry_meta = entry_meta_map.get(entry_id)
            if entry_meta is not None:
                return entry_meta

        instance = getattr(self, "_instance", None)
        collect_entries = getattr(instance, "collect_entries", None)
        if callable(collect_entries):
            try:
                collected = collect_entries(wrap_with_hooks=True)
            except Exception:
                return None
            if isinstance(collected, dict):
                handler_obj = collected.get(entry_id)
                return getattr(handler_obj, "meta", None)
        return None

    @property
    def run_id(self) -> Optional[str]:
        return _CURRENT_RUN_ID.get()

    def require_run_id(self) -> str:
        rid = self.run_id
        if not isinstance(rid, str) or not rid.strip():
            raise RuntimeError("run_id is required (this entry may not be triggered via /runs)")
        return rid

    def _is_in_event_loop(self) -> bool:
        """检测当前是否在事件循环中运行。
        
        Returns:
            True 如果当前在事件循环中，False 如果在无事件循环环境
        """
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def _run_coro_sync(self, coro: Any, *, operation: str) -> Any:
        """Run a coroutine from sync context.

        This is a convenience wrapper (e.g. run_update_sync) and is intentionally
        strict: it refuses to run when an event loop is already running.

        NOTE: ``asyncio.run()`` creates a **new** Context, which does NOT inherit
        the calling thread's contextvars.  We capture a snapshot of the current
        context and re-apply it inside the new event loop via a thin wrapper
        coroutine.
        """

        self._enforce_sync_call_policy(operation)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            ctx_snapshot = contextvars.copy_context()

            async def _with_ctx():
                # Re-apply every contextvar from the snapshot into the
                # asyncio.run()-created context so that require_run_id() etc.
                # see the correct values.
                for var in ctx_snapshot:
                    try:
                        var.set(ctx_snapshot[var])
                    except Exception:
                        pass
                return await coro

            return asyncio.run(_with_ctx())
        raise RuntimeError(f"{operation}_sync cannot be used inside a running event loop; use 'await {operation}(...)' instead")

    def update_status(self, status: Dict[str, Any]) -> None:
        """
        子进程 / 插件内部调用：把原始 status 丢到主进程的队列里，由主进程统一整理。
        """
        try:
            payload = {
                "type": "STATUS_UPDATE",
                "plugin_id": self.plugin_id,
                "data": status,
                "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            self.status_queue.put_nowait(payload)
            if PLUGIN_LOG_CTX_STATUS_UPDATE:
                self.logger.info(f"Plugin {self.plugin_id} status updated: {payload}")
        except (AttributeError, RuntimeError) as e:
            # 队列操作错误
            self.logger.warning(f"Queue error updating status for plugin {self.plugin_id}: {e}")
        except Exception:
            # 其他未知异常
            self.logger.exception(f"Unexpected error updating status for plugin {self.plugin_id}")

    # ==================== Unified Export Push ====================

    async def _export_push_async(
        self,
        *,
        export_type: str,
        run_id: Optional[str] = None,
        text: Optional[str] = None,
        json_data: Optional[Dict[str, Any]] = None,
        url: Optional[str] = None,
        binary_data: Optional[bytes] = None,
        binary_url: Optional[str] = None,
        mime: Optional[str] = None,
        description: Optional[str] = None,
        label: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Unified core implementation for all export types."""
        rid = run_id if isinstance(run_id, str) and run_id.strip() else self.require_run_id()
        request_data: Dict[str, Any] = {
            "run_id": rid,
            "export_type": export_type,
            "description": description,
            "metadata": metadata or {},
        }
        if label is not None:
            request_data["label"] = label

        if export_type == "text":
            request_data["text"] = text
        elif export_type == "json":
            request_data["json"] = json_data
        elif export_type == "url":
            request_data["url"] = url
        elif export_type == "binary_url":
            request_data["binary_url"] = binary_url
            request_data["mime"] = mime
        elif export_type == "binary":
            if not isinstance(binary_data, (bytes, bytearray)):
                raise TypeError("binary_data must be bytes")
            data = bytes(binary_data)
            limit = int(EXPORT_INLINE_BINARY_MAX_BYTES) if EXPORT_INLINE_BINARY_MAX_BYTES is not None else 0
            if limit > 0 and len(data) > limit:
                raise ValueError("binary_data too large")
            request_data["binary_base64"] = base64.b64encode(data).decode("ascii")
            request_data["mime"] = mime
        else:
            raise ValueError(f"unsupported export_type: {export_type}")

        return await self._send_request_and_wait_async(
            method_name=f"export_push_{export_type}",
            request_type="EXPORT_PUSH",
            request_data=request_data,
            timeout=float(timeout),
            wrap_result=True,
        )

    def export_push(
        self,
        *,
        export_type: str,
        run_id: Optional[str] = None,
        text: Optional[str] = None,
        json_data: Optional[Dict[str, Any]] = None,
        url: Optional[str] = None,
        binary_data: Optional[bytes] = None,
        binary_url: Optional[str] = None,
        mime: Optional[str] = None,
        description: Optional[str] = None,
        label: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ):
        """Unified export push (smart sync/async proxy)."""
        coro = self._export_push_async(
            export_type=export_type, run_id=run_id, text=text, json_data=json_data,
            url=url, binary_data=binary_data, binary_url=binary_url, mime=mime,
            description=description, label=label, metadata=metadata, timeout=timeout,
        )
        if self._is_in_event_loop():
            return coro
        return self._run_coro_sync(coro, operation=f"export_push_{export_type}")

    async def export_push_async(
        self,
        *,
        export_type: str,
        run_id: Optional[str] = None,
        text: Optional[str] = None,
        json_data: Optional[Dict[str, Any]] = None,
        url: Optional[str] = None,
        binary_data: Optional[bytes] = None,
        binary_url: Optional[str] = None,
        mime: Optional[str] = None,
        description: Optional[str] = None,
        label: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Unified export push (explicit async)."""
        return await self._export_push_async(
            export_type=export_type, run_id=run_id, text=text, json_data=json_data,
            url=url, binary_data=binary_data, binary_url=binary_url, mime=mime,
            description=description, label=label, metadata=metadata, timeout=timeout,
        )

    # ==================== Convenience export wrappers ====================

    async def _export_push_text_async(self, *, run_id: Optional[str] = None, text: str, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._export_push_async(export_type="text", run_id=run_id, text=text, description=description, metadata=metadata, timeout=timeout)

    def export_push_text(self, *, run_id: Optional[str] = None, text: str, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0):
        """Push text export (smart sync/async proxy)."""
        return self.export_push(export_type="text", run_id=run_id, text=text, description=description, metadata=metadata, timeout=timeout)

    async def export_push_text_async(self, *, run_id: Optional[str] = None, text: str, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._export_push_text_async(run_id=run_id, text=text, description=description, metadata=metadata, timeout=timeout)

    def export_push_text_sync(self, *, run_id: Optional[str] = None, text: str, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return self._run_coro_sync(self._export_push_text_async(run_id=run_id, text=text, description=description, metadata=metadata, timeout=timeout), operation="export_push_text")

    async def _export_push_binary_async(self, *, run_id: Optional[str] = None, binary_data: bytes, mime: Optional[str] = None, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._export_push_async(export_type="binary", run_id=run_id, binary_data=binary_data, mime=mime, description=description, metadata=metadata, timeout=timeout)

    def export_push_binary(self, *, run_id: Optional[str] = None, binary_data: bytes, mime: Optional[str] = None, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0):
        """Push binary export (smart sync/async proxy)."""
        return self.export_push(export_type="binary", run_id=run_id, binary_data=binary_data, mime=mime, description=description, metadata=metadata, timeout=timeout)

    async def export_push_binary_async(self, *, run_id: Optional[str] = None, binary_data: bytes, mime: Optional[str] = None, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._export_push_binary_async(run_id=run_id, binary_data=binary_data, mime=mime, description=description, metadata=metadata, timeout=timeout)

    def export_push_binary_sync(self, *, run_id: Optional[str] = None, binary_data: bytes, mime: Optional[str] = None, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return self._run_coro_sync(self._export_push_binary_async(run_id=run_id, binary_data=binary_data, mime=mime, description=description, metadata=metadata, timeout=timeout), operation="export_push_binary")

    async def _export_push_url_async(self, *, run_id: Optional[str] = None, url: str, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._export_push_async(export_type="url", run_id=run_id, url=url, description=description, metadata=metadata, timeout=timeout)

    def export_push_url(self, *, run_id: Optional[str] = None, url: str, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0):
        """Push URL export (smart sync/async proxy)."""
        return self.export_push(export_type="url", run_id=run_id, url=url, description=description, metadata=metadata, timeout=timeout)

    async def export_push_url_async(self, *, run_id: Optional[str] = None, url: str, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._export_push_url_async(run_id=run_id, url=url, description=description, metadata=metadata, timeout=timeout)

    def export_push_url_sync(self, *, run_id: Optional[str] = None, url: str, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return self._run_coro_sync(self._export_push_url_async(run_id=run_id, url=url, description=description, metadata=metadata, timeout=timeout), operation="export_push_url")

    async def _export_push_binary_url_async(self, *, run_id: Optional[str] = None, binary_url: str, mime: Optional[str] = None, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._export_push_async(export_type="binary_url", run_id=run_id, binary_url=binary_url, mime=mime, description=description, metadata=metadata, timeout=timeout)

    def export_push_binary_url(self, *, run_id: Optional[str] = None, binary_url: str, mime: Optional[str] = None, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0):
        """Push binary URL export (smart sync/async proxy)."""
        return self.export_push(export_type="binary_url", run_id=run_id, binary_url=binary_url, mime=mime, description=description, metadata=metadata, timeout=timeout)

    async def export_push_binary_url_async(self, *, run_id: Optional[str] = None, binary_url: str, mime: Optional[str] = None, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._export_push_binary_url_async(run_id=run_id, binary_url=binary_url, mime=mime, description=description, metadata=metadata, timeout=timeout)

    def export_push_binary_url_sync(self, *, run_id: Optional[str] = None, binary_url: str, mime: Optional[str] = None, description: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        return self._run_coro_sync(self._export_push_binary_url_async(run_id=run_id, binary_url=binary_url, mime=mime, description=description, metadata=metadata, timeout=timeout), operation="export_push_binary_url")

    async def _run_update_async(
        self,
        *,
        run_id: Optional[str] = None,
        progress: Optional[float] = None,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        step: Optional[int] = None,
        step_total: Optional[int] = None,
        eta_seconds: Optional[float] = None,
        metrics: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        rid = run_id if isinstance(run_id, str) and run_id.strip() else self.require_run_id()
        data: Dict[str, Any] = {
            "run_id": rid,
        }
        if progress is not None:
            data["progress"] = progress
        if stage is not None:
            data["stage"] = stage
        if message is not None:
            data["message"] = message
        if step is not None:
            data["step"] = step
        if step_total is not None:
            data["step_total"] = step_total
        if eta_seconds is not None:
            data["eta_seconds"] = eta_seconds
        if metrics is not None:
            data["metrics"] = metrics

        return await self._send_request_and_wait_async(
            method_name="run_update",
            request_type="RUN_UPDATE",
            request_data=data,
            timeout=float(timeout),
            wrap_result=True,
        )

    def run_update(
        self,
        *,
        run_id: Optional[str] = None,
        progress: Optional[float] = None,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        step: Optional[int] = None,
        step_total: Optional[int] = None,
        eta_seconds: Optional[float] = None,
        metrics: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ):
        """智能代理：自动检测执行环境，选择同步或异步执行方式。"""
        coro = self._run_update_async(
            run_id=run_id,
            progress=progress,
            stage=stage,
            message=message,
            step=step,
            step_total=step_total,
            eta_seconds=eta_seconds,
            metrics=metrics,
            timeout=timeout,
        )
        if self._is_in_event_loop():
            return coro
        return self._run_coro_sync(coro, operation="run_update")

    async def run_update_async(
        self,
        *,
        run_id: Optional[str] = None,
        progress: Optional[float] = None,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        step: Optional[int] = None,
        step_total: Optional[int] = None,
        eta_seconds: Optional[float] = None,
        metrics: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        return await self._run_update_async(
            run_id=run_id,
            progress=progress,
            stage=stage,
            message=message,
            step=step,
            step_total=step_total,
            eta_seconds=eta_seconds,
            metrics=metrics,
            timeout=timeout,
        )

    def run_update_sync(
        self,
        *,
        run_id: Optional[str] = None,
        progress: Optional[float] = None,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        step: Optional[int] = None,
        step_total: Optional[int] = None,
        eta_seconds: Optional[float] = None,
        metrics: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        return self._run_coro_sync(
            self._run_update_async(
                run_id=run_id,
                progress=progress,
                stage=stage,
                message=message,
                step=step,
                step_total=step_total,
                eta_seconds=eta_seconds,
                metrics=metrics,
                timeout=timeout,
            ),
            operation="run_update",
        )

    async def _run_progress_async(
        self,
        *,
        run_id: Optional[str] = None,
        progress: float,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        return await self._run_update_async(
            run_id=run_id,
            progress=float(progress),
            stage=stage,
            message=message,
            timeout=float(timeout),
        )

    def run_progress(
        self,
        *,
        run_id: Optional[str] = None,
        progress: float,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        timeout: float = 5.0,
    ):
        """智能代理：自动检测执行环境，选择同步或异步执行方式。"""
        coro = self._run_progress_async(
            run_id=run_id, progress=progress, stage=stage, message=message, timeout=timeout
        )
        if self._is_in_event_loop():
            return coro
        return self._run_coro_sync(coro, operation="run_progress")

    async def run_progress_async(
        self,
        *,
        run_id: Optional[str] = None,
        progress: float = 0.0,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        return await self._run_progress_async(run_id=run_id, progress=progress, stage=stage, message=message, timeout=timeout)

    def run_progress_sync(
        self,
        *,
        run_id: Optional[str] = None,
        progress: float = 0.0,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        return self._run_coro_sync(
            self._run_progress_async(run_id=run_id, progress=progress, stage=stage, message=message, timeout=timeout),
            operation="run_progress",
        )

    def push_message(
        self,
        source: str = "",
        message_type: Optional[str] = None,
        description: Optional[str] = None,
        priority: int = 0,
        content: Optional[str] = None,
        binary_data: Optional[bytes] = None,
        binary_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        unsafe: bool = False,
        fast_mode: bool = False,
        target_lanlan: Optional[str] = None,
        *,
        # ── v2 schema (preferred; see push_message_schema.py) ─────────
        visibility: Optional[list] = None,
        ai_behavior: Optional[str] = None,
        parts: Optional[list] = None,
        # Optional proactive-delivery coalescing key (keyword-only, OPT-IN).
        # Queued proactive cues sharing the SAME key collapse to the newest;
        # unset = never coalesce. Use distinct keys per cue category.
        coalesce_key: Optional[str] = None,
        # ── v1 legacy aliases — emit DeprecationWarning on use ────────
        mime: Optional[str] = None,
        delivery: Any = None,
        reply: Optional[bool] = None,
    ) -> "PushMessageResult":
        """Push a message from a plugin to the host.

        The v2 (canonical) parameters are ``visibility`` (list of
        ``"chat"`` / ``"hud"`` channels where the user sees the parts
        verbatim), ``ai_behavior`` (one of ``"respond"`` / ``"read"`` /
        ``"blind"``), and ``parts`` (ordered list of content parts).
        See :mod:`plugin.sdk.shared.core.push_message_schema` for the full
        schema and example part shapes.

        All other parameters (``message_type``, ``content``, ``binary_data``,
        ``binary_url``, ``mime``, ``delivery``, ``reply``, ``description``,
        ``unsafe``, ``fast_mode``) are deprecated.  They still work for the
        deprecation window but emit ``DeprecationWarning`` and are scheduled
        for removal in v0.9 (see ``docs/changelog``).

        The returned ``submitted`` flag only reports whether the SDK's
        authoritative local submission path accepted responsibility for the
        payload.  It does not acknowledge host consumption, model generation,
        or playback.
        """
        from plugin.sdk.shared.core.push_message_schema import (
            translate_push_message,
        )

        canonical = translate_push_message(
            visibility=visibility,
            ai_behavior=ai_behavior,
            parts=parts,
            message_type=message_type,
            description=description,
            content=content,
            binary_data=binary_data,
            binary_url=binary_url,
            mime=mime,
            delivery=delivery,
            reply=reply,
            unsafe=unsafe if unsafe else None,
            fast_mode=fast_mode if fast_mode else None,
            source=source,
            metadata=metadata,
            target_lanlan=target_lanlan,
            priority=priority,
            coalesce_key=coalesce_key,
        )
        # Stamp target_lanlan into metadata too — proactive_bridge and
        # main_server's session router still read ``metadata.target_lanlan``
        # and we keep that contract through the deprecation window.
        canonical_metadata = dict(canonical.get("metadata") or {})
        if target_lanlan and "target_lanlan" not in canonical_metadata:
            canonical_metadata["target_lanlan"] = target_lanlan
        # Synthesize legacy fields for downstream readers that haven't
        # migrated to v2 yet (notably plugin/server/application/messages/
        # query_service.py).  These are derived, not authoritative — the
        # v2 fields (parts/visibility/ai_behavior) own the real meaning.
        legacy_message_type = (
            message_type
            if isinstance(message_type, str) and message_type
            else _synthesize_legacy_message_type(canonical)
        )
        legacy_content = content if isinstance(content, str) else _synthesize_legacy_content(canonical.get("parts") or [])
        legacy_binary_url: Optional[str] = binary_url if isinstance(binary_url, str) else None
        # The deprecated top-level ``binary_data`` reaches the wire ONLY when the
        # caller passed it next to an explicit ``parts=`` list.  That is the one
        # shape translate_push_message leaves untranslated, so those bytes ride
        # in no part and dropping them here would be silent data loss.  Every
        # other shape is a duplicate of what ``parts[].binary_base64`` already
        # carries: either translate_push_message built the part FROM
        # ``binary_data`` (``parts=None``), or the loop below used to decode the
        # part's base64 back into raw bytes purely to re-attach them.  Carrying
        # both put one image on the wire at ~2.34x its raw size, which is what
        # made a 100 KiB screenshot blow a 256 KiB cap; the base64 copy alone is
        # ~1.34x, so the cap now means roughly what it says.  query_service is
        # the only reader of the field and decodes the canonical part on demand
        # instead.
        legacy_binary_data: Optional[bytes] = (
            bytes(binary_data)
            if isinstance(binary_data, (bytes, bytearray)) and parts is not None
            else None
        )
        legacy_mime: Optional[str] = mime if isinstance(mime, str) else None
        if legacy_binary_url is None or legacy_mime is None:
            for part in canonical.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") not in ("image", "audio", "video"):
                    continue
                if legacy_binary_url is None:
                    url_obj = part.get("url")
                    if isinstance(url_obj, str) and url_obj:
                        legacy_binary_url = url_obj
                if legacy_mime is None:
                    mime_obj = part.get("mime")
                    if isinstance(mime_obj, str) and mime_obj:
                        legacy_mime = mime_obj
                if legacy_binary_url is not None and legacy_mime is not None:
                    break
        # ``description`` has no role in v2 (no semantic consumer; only
        # surfaces as a human label in legacy log lines and the
        # query_service messages-bus response).  Synthesised here purely
        # so v1 readers don't see ``None`` during the deprecation window.
        # Sources, in priority order:
        #   1. explicit ``description=`` kwarg (legacy v1 callers)
        #   2. ``metadata["description"]`` (already-migrated v2 callers
        #      that moved the label there during the migration)
        #   3. empty string (native v2 callers that never set a label)
        # TODO(v0.9): drop this field, the kwarg, the metadata fallback,
        # and the query_service fallback together; new callers should put
        # any human label in ``metadata`` if they want it surfaced.
        if isinstance(description, str):
            legacy_description = description
        else:
            md_description = canonical_metadata.get("description")
            legacy_description = md_description if isinstance(md_description, str) else ""
        # Resolve the legacy delivery/reply pair from the v2 axes so v1
        # consumers that branch on these fields still see a coherent value
        # during the deprecation window.
        if canonical["ai_behavior"] == "respond":
            legacy_delivery = "proactive"
        elif canonical["ai_behavior"] == "read":
            legacy_delivery = "passive"
        else:
            legacy_delivery = "silent"
        legacy_reply = legacy_delivery != "silent"

        def _build_wire_payload(*, message_id: str, ts: Any) -> Dict[str, Any]:
            """Construct the authenticated message envelope.

            Legacy compatibility fields keep downstream readers stable while
            the canonical v2 schema crosses the dedicated message uplink.
            """
            return {
                "type": "MESSAGE_PUSH",
                "message_id": message_id,
                "plugin_id": self.plugin_id,
                "time": ts,
                # v2 schema (canonical):
                "schema": canonical["schema"],
                "source": canonical["source"],
                "priority": canonical["priority"],
                "coalesce_key": canonical.get("coalesce_key", ""),
                "visibility": canonical["visibility"],
                "ai_behavior": canonical["ai_behavior"],
                "parts": canonical["parts"],
                "metadata": canonical_metadata,
                "target_lanlan": canonical.get("target_lanlan"),
                # Legacy compat fields for downstream consumers that have not
                # migrated to v2 yet (query_service, _types/models.py, etc.).
                # All derived from the canonical v2 payload so a v2-only
                # caller still surfaces something meaningful here.
                "message_type": legacy_message_type,
                "content": legacy_content,
                "binary_data": legacy_binary_data,
                "binary_url": legacy_binary_url,
                "mime": legacy_mime,
                "description": legacy_description,
                "unsafe": bool(unsafe),
                "delivery": legacy_delivery,
                "reply": legacy_reply,
            }

        def _reject_if_payload_too_large(payload: Dict[str, Any]) -> Optional["PushMessageRejected"]:
            """Refuse a push the host's ingest server would discard whole.

            The host measures ``len(ormsgpack.packb(payload))`` of each delta
            item -- the payload dict, NOT the batch envelope around it -- against
            MESSAGE_PLANE_PAYLOAD_MAX_BYTES, and on overflow it records
            ``payload_too_big`` and drops the entire item, text parts included.
            That verdict lands in the host process, long after push_message() has
            already returned ``{"submitted": True}``, so the author's only trace
            is a throttled line in someone else's log.  Measuring the same
            expression here turns that into a synchronous verdict the caller can
            branch on.  Both processes import the constant from plugin.settings,
            so the CONSTANT cannot drift apart -- but the OBJECT being measured
            can, and does: the host normalizes the record before it writes the
            plane (``_forward_message`` fills ``message_id`` / rewrites a
            non-string ``time``, and ``fast_mode`` sends a float timestamp the
            host swaps for a 28-character ISO string). Measured, that is up to
            ~19 bytes on the fast path. ``_HOST_ENVELOPE_HEADROOM_BYTES`` is
            held back here so a push sized into that window is refused
            synchronously rather than accepted and then dropped at ingest.
            A test packs both shapes and asserts the real drift stays inside
            the headroom, so a future host-side field cannot reopen the window
            unnoticed.

            The check is deliberately skipped when the host is not validating
            (MESSAGE_PLANE_VALIDATE_PAYLOAD_BYTES off): rejecting locally what
            the host would happily accept would be the SDK inventing a limit of
            its own, and this function exists precisely to agree with the host.
            A pack failure is likewise not our verdict to make -- the host's
            own ``payload_pack_error`` path owns it, and swallowing the
            exception here keeps a msgpack quirk from turning into a push that
            never even reaches the transport.

            Returns the rejection dict to hand back to the caller, or ``None``
            when the push may proceed.
            """
            carriers = _inline_binary_carriers(
                canonical.get("parts"), legacy_binary_data
            )
            # Every payload is measured, not just the ones carrying inline
            # bytes. This used to skip out when ``carriers`` was empty, to keep
            # a second msgpack pack off the high-frequency text cue path -- but
            # the host measures the WHOLE envelope, so an oversized text or
            # metadata push was still dropped there as payload_too_big while
            # push_message() had already answered submitted=True. That is the
            # exact invisible non-delivery this guard exists to end, left open
            # for the cheapest possible payload to walk through (CodeRabbit).
            #
            # The cost that justified the skip does not survive measurement:
            # ormsgpack.packb on a typical text cue (248 B) is 0.19 us, and
            # 50 us on a 200 KB one. Paying a fifth of a microsecond per cue to
            # close a silent-loss hole is not a trade that needs thinking about.
            if ormsgpack is None:
                return None
            from plugin.settings import (
                MESSAGE_PLANE_PAYLOAD_MAX_BYTES,
                MESSAGE_PLANE_VALIDATE_PAYLOAD_BYTES,
            )

            if not bool(MESSAGE_PLANE_VALIDATE_PAYLOAD_BYTES):
                return None
            try:
                size = len(ormsgpack.packb(payload))
            except Exception:
                return None
            # 减去宿主规范化会追加的那点字节，见上面的说明。夹到 >=1，免得
            # 有人把上限配成比余量还小的值时这里变成"全拒"。
            limit = max(
                1,
                int(MESSAGE_PLANE_PAYLOAD_MAX_BYTES) - _HOST_ENVELOPE_HEADROOM_BYTES,
            )
            if size <= limit:
                return None
            totals = _inline_carrier_totals(carriers)
            labels = [label for label, _size in totals]
            dominant = totals[0][0] if totals else ""
            # 内联载体解释得了这次超限吗？把它们全部拿掉之后还剩多少。
            #
            # 只看 totals 排序会把锅永远扣在内联载体上，哪怕它根本不是元凶：
            # 600 KiB 的 metadata 配一张 1 字节的图，dominant 仍是 "image"，
            # 于是作者被告知去 ctx.images.upload() —— 照做之后依然超限，因为
            # 那张图本来就不占地方。建议给错方向比不给建议更糟，它让人以为
            # 自己已经改对了。
            carrier_bytes = sum(size_b for _label, size_b in totals)
            non_inline = max(0, size - carrier_bytes)
            if totals and non_inline > limit:
                # 卸掉全部内联仍然过不去：真正撑爆的是文本或 metadata。
                remedy = (
                    "The inline payloads are not what blew this cap: even with "
                    f"all of them removed the push is about {non_inline}B, still "
                    f"over the {limit}B limit. The text parts or the metadata "
                    "are what to shrink here; offloading the attachments alone "
                    "will not get this push through."
                )
            elif not totals:
                # No inline carrier at all: the text parts or the metadata are
                # what spent the budget. The base64 explanation below would be
                # actively misleading here -- there is nothing base64-encoded to
                # blame, the payload is simply that big -- so this branch gets
                # its own wording and the log line drops the ratio arithmetic.
                remedy = (
                    "Nothing in this push travels inline, so the text parts or "
                    "the metadata are what spent the budget: shorten them, or "
                    "move the bulk into a file or a URL the host can fetch and "
                    "reference it from a shorter message."
                )
            elif dominant == "image":
                remedy = (
                    "Send large images as a URL part instead: "
                    "`part = await ctx.images.upload(data, mime=...)` returns a "
                    "push-ready image part that does not travel inline."
                )
            else:
                # There is no upload helper for audio/video today, so naming one
                # would send the author after an API that is not there. Say what
                # IS true: make the payload smaller, or host it and reference it.
                remedy = (
                    f"There is no upload helper for an inline {dominant} payload "
                    "today, so the options are to shrink the payload itself "
                    "(shorter clip, lower bitrate or resolution) or to host it "
                    "and push the same part with `url=` instead of `data=`."
                )
                if "image" in labels:
                    remedy += (
                        " The image part in this push can also be offloaded with "
                        "`part = await ctx.images.upload(data, mime=...)`."
                    )
            try:
                # The author needs four things to act: how far over they are,
                # what the ceiling is, WHICH payload spent the budget, and why
                # their "300 KiB screenshot" blew a 512 KiB cap. The last one is
                # the non-obvious part -- inline bytes travel base64, 4/3 of the
                # raw size -- and without it the arithmetic looks broken and the
                # fix looks arbitrary. The per-carrier breakdown is what keeps
                # the remedy honest when a push carries more than one inline
                # part: the advice follows the payload that spent the budget.
                if totals:
                    self.logger.error(
                        "[PluginContext] push_message rejected: reason=payload_too_large "
                        "plugin_id={} size={}B limit={}B inline={}. Inline bytes travel "
                        "base64-encoded in parts[].binary_base64, about {}x their raw "
                        "size, so the effective raw-bytes ceiling for one inline payload "
                        "is about {}B. {}",
                        self.plugin_id,
                        int(size),
                        limit,
                        " ".join(f"{label}={size_b}B" for label, size_b in totals),
                        f"{_INLINE_BASE64_WIRE_RATIO:.2f}",
                        int(limit / _INLINE_BASE64_WIRE_RATIO),
                        remedy,
                    )
                else:
                    # No base64 arithmetic here: quoting a ratio and an
                    # "effective raw-bytes ceiling" for a push that carries no
                    # inline bytes would send the author hunting for an
                    # attachment that does not exist. The size and the limit are
                    # the whole story.
                    self.logger.error(
                        "[PluginContext] push_message rejected: reason=payload_too_large "
                        "plugin_id={} size={}B limit={}B inline=none. {}",
                        self.plugin_id,
                        int(size),
                        limit,
                        remedy,
                    )
            except Exception:
                # Diagnostic only. A logging failure (rotation race, bad
                # formatting arg) must not convert a clean local rejection into
                # an exception the plugin author never asked for.
                pass
            return {
                "ok": False,
                "submitted": False,
                "reason": "payload_too_large",
            }

        # Plugin-originated messages cross the authenticated per-host uplink.
        # The host binds plugin_id to that transport before any shared-state
        # write, so plugin code cannot self-assert another plugin's identity by
        # opening the public message-plane ingest socket directly.
        if self.message_queue is not None:
            try:
                use_fast_uplink = bool(fast_mode)
                authenticated_payload = _build_wire_payload(
                    message_id=str(uuid.uuid4()),
                    ts=(
                        time.time()
                        if use_fast_uplink
                        else datetime.now(timezone.utc).isoformat().replace(
                            "+00:00",
                            "Z",
                        )
                    ),
                )
                # Before the send, so the rejection is authoritative rather
                # than a report about a payload already on its way to be
                # discarded. #2999 put this probe on all three exits that
                # existed then; the authenticated uplink is now the only one,
                # and the cap is a property of the payload rather than of the
                # transport carrying it, so the one exit still gets it.
                oversized = _reject_if_payload_too_large(authenticated_payload)
                if oversized is not None:
                    return oversized
                fast_put = getattr(
                    self.message_queue,
                    "put_fast_nowait",
                    None,
                )
                if use_fast_uplink and callable(fast_put):
                    fast_put(authenticated_payload)
                else:
                    self.message_queue.put_nowait(authenticated_payload)
                return {"submitted": True}
            except Exception as e:
                try:
                    self.logger.warning(
                        "[PluginContext] authenticated message uplink failed ({})",
                        type(e).__name__,
                    )
                except Exception:
                    pass
                return {
                    "ok": False,
                    "submitted": False,
                    "reason": (
                        "backpressure"
                        if _is_submission_backpressure(e)
                        else "transport_error"
                    ),
                }

        try:
            self.logger.error(
                "[PluginContext] push_message failed: authenticated message "
                "uplink unavailable"
            )
        except Exception:
            pass
        return {
            "ok": False,
            "submitted": False,
            "reason": "transport_unavailable",
        }

    async def push_message_async(self, *args: Any, **kwargs: Any) -> "PushMessageResult":
        """异步版本的 push_message，使用 asyncio.to_thread 包装同步调用。

        Note: 底层 ZMQ socket 是同步的，此方法通过线程池实现非阻塞。新签名见
        :meth:`push_message`。本方法仅做参数透传，不在此处做兼容翻译。
        """
        return await asyncio.to_thread(self.push_message, *args, **kwargs)

    def _send_request_and_wait(
        self,
        *,
        method_name: str,
        request_type: str,
        request_data: Dict[str, Any],
        timeout: float,
        wrap_result: bool = True,
        send_log_template: Optional[str] = None,
        error_log_template: Optional[str] = None,
        warn_on_orphan_response: bool = False,
        orphan_warning_template: Optional[str] = None,
    ) -> Any:
        self._enforce_sync_call_policy(method_name)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._send_request_and_wait_async(
                    method_name=method_name,
                    request_type=request_type,
                    request_data=request_data,
                    timeout=timeout,
                    wrap_result=wrap_result,
                    send_log_template=send_log_template,
                    error_log_template=error_log_template,
                    warn_on_orphan_response=warn_on_orphan_response,
                    orphan_warning_template=orphan_warning_template,
                )
            )
        raise RuntimeError(
            f"Sync call '{method_name}' cannot be used inside a running event loop. "
            "Use _send_request_and_wait_async(...) instead."
        )

    async def _send_request_and_wait_async(
        self,
        *,
        method_name: str,
        request_type: str,
        request_data: Dict[str, Any],
        timeout: float,
        wrap_result: bool = True,
        send_log_template: Optional[str] = None,
        error_log_template: Optional[str] = None,
        warn_on_orphan_response: bool = False,
        orphan_warning_template: Optional[str] = None,
    ) -> Any:
        _ = method_name

        def _error_to_message(error: Any) -> str:
            if isinstance(error, str):
                return error
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                details = error.get("details")
                parts = []
                if isinstance(code, str) and code:
                    parts.append(f"[{code}]")
                if isinstance(message, str) and message:
                    parts.append(message)
                if details is not None:
                    parts.append(f"details={details}")
                if parts:
                    return " ".join(parts)
            return str(error)

        plugin_comm_queue = self._plugin_comm_queue
        if plugin_comm_queue is None:
            raise RuntimeError(
                f"Plugin communication queue not available for plugin {self.plugin_id}. "
                "This method can only be called from within a plugin process."
            )

        request_id = str(uuid.uuid4())
        payload = dict(request_data or {})
        for _k in ("type", "from_plugin", "request_id", "timeout"):
            payload.pop(_k, None)
        request: Dict[str, Any] = {
            **payload,
            "type": request_type,
            "from_plugin": self.plugin_id,
            "request_id": request_id,
            "timeout": timeout,
        }

        deadline = time.time() + timeout
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: plugin_comm_queue.put(request, timeout=max(0.0, deadline - time.time())),
            )
            if send_log_template:
                try:
                    self.logger.debug(
                        send_log_template.format(
                            request_id=request_id,
                            from_plugin=self.plugin_id,
                            **payload,
                        )
                    )
                except Exception:
                    pass
        except Exception as e:
            if error_log_template:
                try:
                    self.logger.exception(error_log_template.format(error=e))
                except Exception:
                    pass
            raise RuntimeError(f"Failed to send {request_type} request: {e}") from e

        response_queue = getattr(self, "_response_queue", None)
        pending = getattr(self, "_response_pending", None)
        if pending is None:
            pending = {}
            try:
                object.__setattr__(self, "_response_pending", pending)
            except Exception:
                self._response_pending = pending

        if isinstance(pending, dict) and request_id in pending:
            response = pending.pop(request_id)
            if isinstance(response, dict) and response.get("error"):
                raise RuntimeError(_error_to_message(response.get("error")))
            result = response.get("result") if isinstance(response, dict) else None
            if wrap_result:
                return result if isinstance(result, dict) else {"result": result}
            return result

        if response_queue is not None:
            while time.time() < deadline:
                response = state.get_plugin_response(request_id)
                if isinstance(response, dict):
                    if response.get("error"):
                        raise RuntimeError(_error_to_message(response.get("error")))
                    result = response.get("result")
                    if wrap_result:
                        return result if isinstance(result, dict) else {"result": result}
                    return result

                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(
                        response_queue.get(), timeout=min(0.05, remaining),
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("request_id")
                if rid == request_id:
                    if msg.get("error"):
                        raise RuntimeError(_error_to_message(msg.get("error")))
                    result = msg.get("result")
                    if wrap_result:
                        return result if isinstance(result, dict) else {"result": result}
                    return result
                if isinstance(pending, dict) and rid:
                    if len(pending) > 1024:
                        raise RuntimeError(
                            f"_response_pending overflow (size={len(pending)}, rid={rid}); "
                            "upstream response correlation/backpressure bug must be fixed."
                        )
                    pending[str(rid)] = msg

        check_interval = 0.01
        while time.time() < deadline:
            response = state.get_plugin_response(request_id)
            if not isinstance(response, dict):
                await asyncio.sleep(check_interval)
                continue

            if response.get("error"):
                raise RuntimeError(_error_to_message(response.get("error")))

            result = response.get("result")
            if wrap_result:
                return result if isinstance(result, dict) else {"result": result}
            return result

        orphan_response = None
        try:
            orphan_response = state.peek_plugin_response(request_id)
        except Exception:
            orphan_response = None
        if warn_on_orphan_response and orphan_response is not None:
            try:
                state.get_plugin_response(request_id)
            except Exception:
                pass
            if orphan_warning_template:
                try:
                    self.logger.warning(
                        orphan_warning_template.format(
                            request_id=request_id,
                            from_plugin=self.plugin_id,
                            **payload,
                        )
                    )
                except Exception:
                    pass
        raise TimeoutError(f"{request_type} timed out after {timeout}s")
    
    def trigger_plugin_event_sync(
        self,
        target_plugin_id: str,
        event_type: str,
        event_id: str,
        params: Dict[str, Any],
        timeout: float = 10.0
    ) -> Dict[str, Any]:
        """同步版本:触发其他插件的自定义事件（插件间通信）
        
        Args:
            target_plugin_id: 目标插件ID
            event_type: 自定义事件类型
            event_id: 事件ID
            params: 参数字典
            timeout: 超时时间（秒）
            
        Returns:
            事件处理器的返回结果
        """
        try:
            return self._send_request_and_wait(
                method_name="trigger_plugin_event",
                request_type="PLUGIN_TO_PLUGIN",
                request_data={
                    "to_plugin": target_plugin_id,
                    "event_type": event_type,
                    "event_id": event_id,
                    "args": params,  # 内部协议仍使用 args
                },
                timeout=timeout,
                wrap_result=False,
                send_log_template=(
                    "[PluginContext] Sent plugin communication request: {from_plugin} -> {to_plugin}, "
                    "event={event_type}.{event_id}, req_id={request_id}"
                ),
                error_log_template="Failed to send plugin communication request: {error}",
                warn_on_orphan_response=True,
                orphan_warning_template=(
                    "[PluginContext] Timeout reached, but response was found (likely delayed). "
                    "Cleaned up orphan response for req_id={request_id}"
                ),
            )
        except TimeoutError as e:
            raise TimeoutError(
                f"Plugin {target_plugin_id} event {event_type}.{event_id} timed out after {timeout}s"
            ) from e
    
    async def trigger_plugin_event_async(
        self,
        target_plugin_id: str,
        event_type: str,
        event_id: str,
        params: Dict[str, Any],
        timeout: float = 10.0
    ) -> Dict[str, Any]:
        """异步版本:触发其他插件的自定义事件（插件间通信）"""
        try:
            return await self._send_request_and_wait_async(
                method_name="trigger_plugin_event",
                request_type="PLUGIN_TO_PLUGIN",
                request_data={
                    "to_plugin": target_plugin_id,
                    "event_type": event_type,
                    "event_id": event_id,
                    "args": params,  # 内部协议仍使用 args
                },
                timeout=timeout,
                wrap_result=False,
                send_log_template=(
                    "[PluginContext] Sent plugin communication request: {from_plugin} -> {to_plugin}, "
                    "event={event_type}.{event_id}, req_id={request_id}"
                ),
                error_log_template="Failed to send plugin communication request: {error}",
                warn_on_orphan_response=True,
                orphan_warning_template=(
                    "[PluginContext] Timeout reached, but response was found (likely delayed). "
                    "Cleaned up orphan response for req_id={request_id}"
                ),
            )
        except TimeoutError as e:
            raise TimeoutError(
                f"Plugin {target_plugin_id} event {event_type}.{event_id} timed out after {timeout}s"
            ) from e
    
    def trigger_plugin_event(
        self,
        target_plugin_id: str,
        event_type: str,
        event_id: str,
        params: Dict[str, Any],
        timeout: float = 10.0
    ):
        """智能版本:自动检测执行环境,选择同步或异步执行方式
        
        Returns:
            在事件循环中返回协程,否则返回结果字典
        """
        if self._is_in_event_loop():
            return self.trigger_plugin_event_async(
                target_plugin_id=target_plugin_id,
                event_type=event_type,
                event_id=event_id,
                params=params,
                timeout=timeout,
            )
        return self.trigger_plugin_event_sync(
            target_plugin_id=target_plugin_id,
            event_type=event_type,
            event_id=event_id,
            params=params,
            timeout=timeout,
        )

    def query_plugins_sync(self, filters: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        """同步版本:查询插件列表"""
        try:
            return self._send_request_and_wait(
                method_name="query_plugins",
                request_type="PLUGIN_QUERY",
                request_data={"filters": filters or {}},
                timeout=timeout,
                wrap_result=True,
                send_log_template="[PluginContext] Sent plugin query request: from={from_plugin}, req_id={request_id}",
                error_log_template="Failed to send plugin query request: {error}",
            )
        except TimeoutError as e:
            raise TimeoutError(f"Plugin query timed out after {timeout}s") from e
    
    async def query_plugins_async(self, filters: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        """异步版本:查询插件列表"""
        try:
            return await self._send_request_and_wait_async(
                method_name="query_plugins",
                request_type="PLUGIN_QUERY",
                request_data={"filters": filters or {}},
                timeout=timeout,
                wrap_result=True,
                send_log_template="[PluginContext] Sent plugin query request: from={from_plugin}, req_id={request_id}",
                error_log_template="Failed to send plugin query request: {error}",
            )
        except TimeoutError as e:
            raise TimeoutError(f"Plugin query timed out after {timeout}s") from e
    
    def query_plugins(self, filters: Optional[Dict[str, Any]] = None, timeout: float = 5.0):
        """智能版本:自动检测执行环境,选择同步或异步执行方式
        
        Returns:
            在事件循环中返回协程,否则返回结果字典
        """
        if self._is_in_event_loop():
            return self.query_plugins_async(filters=filters, timeout=timeout)
        return self.query_plugins_sync(filters=filters, timeout=timeout)

    async def _get_local_config_payload(
        self,
        *,
        payload_type: str,
        profile_name: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Optional[Dict[str, Any]]:
        try:
            from plugin.server.application.config import ConfigQueryService

            service = ConfigQueryService()
            if payload_type == "config":
                cached = getattr(self, "_effective_config", None)
                uncertain = getattr(self, "_effective_config_uncertain", False)
                if isinstance(cached, dict) and not uncertain:
                    return {
                        "plugin_id": self.plugin_id,
                        "config": copy.deepcopy(cached),
                        "config_path": str(self.config_path),
                    }
                payload = await asyncio.wait_for(
                    service.get_plugin_config(plugin_id=self.plugin_id),
                    timeout=timeout,
                )
                config_obj = payload.get("config")
                if isinstance(config_obj, dict):
                    self._set_effective_config_cache(config_obj)
                return payload
            if payload_type == "base":
                return await asyncio.wait_for(
                    service.get_plugin_base_config(plugin_id=self.plugin_id),
                    timeout=timeout,
                )
            if payload_type == "profiles":
                return await asyncio.wait_for(
                    service.get_plugin_profiles_state(plugin_id=self.plugin_id),
                    timeout=timeout,
                )
            if payload_type == "profile":
                if not isinstance(profile_name, str) or not profile_name.strip():
                    return None
                return await asyncio.wait_for(
                    service.get_plugin_profile_config(
                        plugin_id=self.plugin_id,
                        profile_name=profile_name.strip(),
                    ),
                    timeout=timeout,
                )
            if payload_type == "effective":
                return await asyncio.wait_for(
                    service.get_plugin_effective_config(
                        plugin_id=self.plugin_id,
                        profile_name=profile_name.strip() if isinstance(profile_name, str) and profile_name.strip() else None,
                    ),
                    timeout=timeout,
                )
        except Exception as exc:
            try:
                self.logger.debug(
                    "[PluginContext] local config read fallback failed: plugin_id={}, payload_type={}, err_type={}, err={}",
                    self.plugin_id,
                    payload_type,
                    type(exc).__name__,
                    str(exc),
                )
            except Exception:
                pass
        return None

    async def get_own_config(self, timeout: float = 5.0) -> Dict[str, Any]:
        local_payload = await self._get_local_config_payload(payload_type="config", timeout=timeout)
        if isinstance(local_payload, dict):
            return local_payload
        try:
            return await self._send_request_and_wait_async(
                method_name="get_own_config",
                request_type="PLUGIN_CONFIG_GET",
                request_data={"plugin_id": self.plugin_id},
                timeout=timeout,
                wrap_result=True,
                error_log_template=None,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Plugin config get timed out after {timeout}s") from e

    async def get_own_base_config(self, timeout: float = 5.0) -> Dict[str, Any]:
        local_payload = await self._get_local_config_payload(payload_type="base", timeout=timeout)
        if isinstance(local_payload, dict):
            return local_payload
        try:
            return await self._send_request_and_wait_async(
                method_name="get_own_base_config",
                request_type="PLUGIN_CONFIG_BASE_GET",
                request_data={"plugin_id": self.plugin_id},
                timeout=timeout,
                wrap_result=True,
                error_log_template=None,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Plugin base config get timed out after {timeout}s") from e

    async def get_own_profiles_state(self, timeout: float = 5.0) -> Dict[str, Any]:
        local_payload = await self._get_local_config_payload(payload_type="profiles", timeout=timeout)
        if isinstance(local_payload, dict):
            return local_payload
        try:
            return await self._send_request_and_wait_async(
                method_name="get_own_profiles_state",
                request_type="PLUGIN_CONFIG_PROFILES_GET",
                request_data={"plugin_id": self.plugin_id},
                timeout=timeout,
                wrap_result=True,
                error_log_template=None,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Plugin profiles state get timed out after {timeout}s") from e

    async def get_own_profile_config(self, profile_name: str, timeout: float = 5.0) -> Dict[str, Any]:
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise ValueError("profile_name must be a non-empty string")
        local_payload = await self._get_local_config_payload(
            payload_type="profile",
            profile_name=profile_name,
            timeout=timeout,
        )
        if isinstance(local_payload, dict):
            return local_payload
        try:
            return await self._send_request_and_wait_async(
                method_name="get_own_profile_config",
                request_type="PLUGIN_CONFIG_PROFILE_GET",
                request_data={
                    "plugin_id": self.plugin_id,
                    "profile_name": profile_name.strip(),
                },
                timeout=timeout,
                wrap_result=True,
                error_log_template=None,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Plugin profile config get timed out after {timeout}s") from e

    async def get_own_effective_config(
        self,
        profile_name: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Get effective config.

        - profile_name is None: returns active profile overlay (same as get_own_config).
        - profile_name is a string: returns base + that profile overlay.
        """

        request_data: Dict[str, Any] = {
            "plugin_id": self.plugin_id,
        }
        if isinstance(profile_name, str) and profile_name.strip():
            request_data["profile_name"] = profile_name.strip()

        local_payload = await self._get_local_config_payload(
            payload_type="effective",
            profile_name=profile_name,
            timeout=timeout,
        )
        if isinstance(local_payload, dict):
            effective_obj = local_payload.get("config")
            if isinstance(effective_obj, dict) and profile_name is None:
                self._set_effective_config_cache(effective_obj)
            return local_payload

        try:
            return await self._send_request_and_wait_async(
                method_name="get_own_effective_config",
                request_type="PLUGIN_CONFIG_EFFECTIVE_GET",
                request_data=request_data,
                timeout=timeout,
                wrap_result=True,
                error_log_template=None,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Plugin effective config get timed out after {timeout}s") from e

    async def get_system_config(self, timeout: float = 5.0) -> Dict[str, Any]:
        try:
            return await self._send_request_and_wait_async(
                method_name="get_system_config",
                request_type="PLUGIN_SYSTEM_CONFIG_GET",
                request_data={},
                timeout=timeout,
                wrap_result=True,
                error_log_template=None,
            )
        except TimeoutError as e:
            raise TimeoutError(f"System config get timed out after {timeout}s") from e

    def query_memory_sync(self, lanlan_name: str, query: str, timeout: float = 5.0) -> Dict[str, Any]:
        """同步版本:查询内存数据"""
        try:
            return self._send_request_and_wait(
                method_name="query_memory",
                request_type="MEMORY_QUERY",
                request_data={
                    "lanlan_name": lanlan_name,
                    "query": query,
                },
                timeout=timeout,
                wrap_result=True,
                error_log_template=None,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Memory query timed out after {timeout}s") from e
    
    async def query_memory_async(self, lanlan_name: str, query: str, timeout: float = 5.0) -> Dict[str, Any]:
        """异步版本:查询内存数据"""
        try:
            return await self._send_request_and_wait_async(
                method_name="query_memory",
                request_type="MEMORY_QUERY",
                request_data={
                    "lanlan_name": lanlan_name,
                    "query": query,
                },
                timeout=timeout,
                wrap_result=True,
                error_log_template=None,
            )
        except TimeoutError as e:
            raise TimeoutError(f"Memory query timed out after {timeout}s") from e
    
    def query_memory(self, lanlan_name: str, query: str, timeout: float = 5.0):
        """智能版本:自动检测执行环境,选择同步或异步执行方式
        
        Returns:
            在事件循环中返回协程,否则返回结果字典
        """
        if self._is_in_event_loop():
            return self.query_memory_async(lanlan_name=lanlan_name, query=query, timeout=timeout)
        return self.query_memory_sync(lanlan_name=lanlan_name, query=query, timeout=timeout)

    async def update_own_config(self, updates: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict")
        old_effective_config = copy.deepcopy(getattr(self, "_effective_config", None))
        old_effective_config_uncertain = getattr(
            self,
            "_effective_config_uncertain",
            False,
        )
        optimistic_config = self._merge_config_copy(old_effective_config, updates)
        self._set_effective_config_cache(optimistic_config)
        # Keep config writes from blocking plugin actions; timeouts fall back to the optimistic in-memory config.
        request_timeout = min(float(timeout), 4.5)
        request_deadline = time.monotonic() + request_timeout
        try:
            payload = await self._send_request_and_wait_async(
                method_name="update_own_config",
                request_type="PLUGIN_CONFIG_UPDATE",
                request_data={
                    "plugin_id": self.plugin_id,
                    "updates": updates,
                    "_request_deadline_monotonic": request_deadline,
                },
                timeout=request_timeout,
                wrap_result=True,
                error_log_template=None,
            )
            config_obj = payload.get("config") if isinstance(payload, dict) else None
            if isinstance(config_obj, dict):
                if payload.get("persisted") is False:
                    self._set_effective_config_cache(optimistic_config)
                    payload = dict(payload)
                    payload["config"] = copy.deepcopy(optimistic_config)
                else:
                    self._set_effective_config_cache(config_obj)
            return payload
        except TimeoutError:
            self._effective_config_uncertain = True
            return {
                "success": False,
                "plugin_id": self.plugin_id,
                "config": copy.deepcopy(optimistic_config),
                "requires_reload": False,
                "persisted": None,
                "message": "Config persistence response timed out; final persistence status is unknown",
            }
        except asyncio.CancelledError:
            # The request may already have crossed the atomic commit point.
            # Keep the optimistic view instead of restoring a known-stale one.
            self._effective_config_uncertain = True
            raise
        except Exception:
            if isinstance(old_effective_config, dict):
                self._set_effective_config_cache(old_effective_config)
            else:
                self._effective_config = None
                self._refresh_instance_runtime_config({})
            self._effective_config_uncertain = old_effective_config_uncertain
            raise

    async def replace_own_config(self, config: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        if not isinstance(config, dict):
            raise TypeError("config must be a dict")
        old_effective_config = copy.deepcopy(getattr(self, "_effective_config", None))
        old_effective_config_uncertain = getattr(
            self,
            "_effective_config_uncertain",
            False,
        )
        optimistic_config = copy.deepcopy(config)
        self._set_effective_config_cache(optimistic_config)
        request_timeout = float(timeout)
        request_deadline = time.monotonic() + request_timeout
        try:
            payload = await self._send_request_and_wait_async(
                method_name="replace_own_config",
                request_type="PLUGIN_CONFIG_REPLACE",
                request_data={
                    "plugin_id": self.plugin_id,
                    "config": config,
                    "_request_deadline_monotonic": request_deadline,
                },
                timeout=request_timeout,
                wrap_result=True,
                error_log_template=None,
            )
            config_obj = payload.get("config") if isinstance(payload, dict) else None
            if isinstance(config_obj, dict):
                self._set_effective_config_cache(config_obj)
            return payload
        except TimeoutError:
            # No response does not prove that the atomic replace lost its
            # deadline race.  Keep the requested view; the next successful
            # config read will replace it with the persisted effective config.
            self._effective_config_uncertain = True
            raise
        except asyncio.CancelledError:
            self._effective_config_uncertain = True
            raise
        except Exception:
            if isinstance(old_effective_config, dict):
                self._set_effective_config_cache(old_effective_config)
            else:
                self._effective_config = None
                self._refresh_instance_runtime_config({})
            self._effective_config_uncertain = old_effective_config_uncertain
            raise
