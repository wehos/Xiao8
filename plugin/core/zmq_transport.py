"""ZeroMQ transport for plugin host ↔ child process communication.

Replaces ``multiprocessing.Queue`` with four ZMQ PUSH/PULL channels:

* **Downlink** (host → child): commands, plugin-to-plugin responses
* **Control uplink** (child → host): size-bounded results, status, and
  plugin-to-plugin requests — message channels are refused on it
* **Message uplink** (child → host): size-bounded individual and batched
  plugin messages
* **Image uplink** (child → host): bounded raw image uploads

Downlink messages are serialised with :mod:`pickle` for compatibility with
existing host commands. Uplink messages, which cross from untrusted plugin code
into the host, use MessagePack so decoding cannot execute plugin-controlled
objects, and carry a per-host token the host checks before acting on them. Both
directions carry a *channel tag* so the receiver can demux.

The image uplink keeps its own framing — JSON metadata plus a separate raw
bytes frame — because re-packing megabytes through MessagePack would buy
nothing. The same token rides inside its metadata frame instead, so every
child → host channel is gated by one credential.

Channel tags
~~~~~~~~~~~~
- ``cmd``   – commands (downlink)
- ``res``   – request/response results (uplink)
- ``sts``   – status updates (uplink)
- ``msg``   — individual messages (message uplink)
- ``msg_batch`` — batched messages (message uplink)
- ``comm``  – plugin-to-plugin requests (uplink)
- ``resp``  – plugin-to-plugin responses (downlink)
"""
from __future__ import annotations

import asyncio
import json
import pickle
import os
import queue
import secrets
import sys
import threading
import time
from pathlib import PurePath
from typing import Any, Optional, Tuple

import ormsgpack
import zmq
import zmq.asyncio

from plugin.logging_config import logger

# ── Channel constants ──────────────────────────────────────────────
CH_CMD = "cmd"
CH_RES = "res"
CH_STS = "sts"
CH_MSG = "msg"
CH_MSG_BATCH = "msg_batch"
CH_COMM = "comm"
CH_RESP = "resp"

_LINGER_MS = 1000
# Which channels each child -> host socket is allowed to carry. The split
# belongs to the socket, not to the decoder: _decode_uplink still accepts the
# union by default, so a transport pair built without a dedicated message
# endpoint (see ChildTransport, where _msg_sock falls back to _ul_sock) keeps
# working against a host that reads both planes off one socket. What the split
# closes is the smuggling route into *this* host: HostTransport always binds
# both sockets, so a plugin holding the control endpoint must not be able to
# use it to push message frames around the message plane's own ceiling.
_CONTROL_UPLINK_CHANNELS = frozenset({CH_RES, CH_STS, CH_COMM})
_MESSAGE_UPLINK_CHANNELS = frozenset({CH_MSG, CH_MSG_BATCH})
_UPLINK_CHANNELS = _CONTROL_UPLINK_CHANNELS | _MESSAGE_UPLINK_CHANNELS
_UPLINK_PACK_OPTIONS = (
    ormsgpack.OPT_NON_STR_KEYS
    | ormsgpack.OPT_PASSTHROUGH_TUPLE
    | ormsgpack.OPT_SERIALIZE_NUMPY
    | ormsgpack.OPT_SERIALIZE_PYDANTIC
)

# 解包必须带上 OPT_NON_STR_KEYS，否则和打包侧不对称。
#
# 这不是可选的对齐：ormsgpack 用一个扩展类型来编码非字符串键的 map，读的时候
# 不开这个开关就会 `ValueError: invalid type U16`。于是一个返回
# ``{"by_year": {2024: 3}}`` 的 handler（int / bool / float 键都算）打包成功、
# 解包抛错，_consume_uplink 的 except 把它记下来 continue，CH_RES 到不了
# _dispatch_result，pending future 永远不 resolve——调用方在
# PLUGIN_TRIGGER_TIMEOUT 之后拿到一个不含任何原因的超时。
#
# 另外三个 option 是打包侧专用的（PASSTHROUGH_TUPLE / SERIALIZE_NUMPY /
# SERIALIZE_PYDANTIC 都只影响序列化），所以这里只需要这一个。
_UPLINK_UNPACK_OPTIONS = ormsgpack.OPT_NON_STR_KEYS


def _normalize_uplink_extension(value: object) -> object:
    if isinstance(value, PurePath):
        return str(value)
    raise TypeError(f"unsupported uplink value type: {type(value).__name__}")


def _encode_uplink(token: str, channel: str, payload: Any) -> bytes:
    if not token or channel not in _UPLINK_CHANNELS or not isinstance(payload, dict):
        raise TypeError("invalid uplink message")
    try:
        return ormsgpack.packb(
            [token, channel, payload],
            default=_normalize_uplink_extension,
            option=_UPLINK_PACK_OPTIONS,
        )
    except Exception as exc:
        if channel == CH_RES:
            req_id = payload.get("req_id")
            error_payload = {
                "req_id": req_id if isinstance(req_id, (str, int)) else "unknown",
                "success": False,
                "data": None,
                "error": "Plugin result is not MessagePack-serializable",
            }
            return ormsgpack.packb(
                [token, channel, error_payload],
                option=_UPLINK_PACK_OPTIONS,
            )
        raise TypeError("uplink payload must be MessagePack-serializable") from exc


def _decode_uplink(
    raw: bytes,
    *,
    expected_token: str,
    allowed_channels: frozenset = _UPLINK_CHANNELS,
) -> Tuple[str, dict]:
    try:
        decoded = ormsgpack.unpackb(raw, option=_UPLINK_UNPACK_OPTIONS)
    except Exception as exc:
        raise ValueError("invalid uplink payload") from exc
    if not isinstance(decoded, list) or len(decoded) != 3:
        raise ValueError("invalid uplink payload")
    supplied_token, channel, payload = decoded
    if (
        not isinstance(supplied_token, str)
        or not expected_token
        or not secrets.compare_digest(
            supplied_token.encode("utf-8"),
            expected_token.encode("utf-8"),
        )
    ):
        raise ValueError("invalid uplink credential")
    if channel not in _UPLINK_CHANNELS or not isinstance(payload, dict):
        raise ValueError("invalid uplink payload")
    if channel not in allowed_channels:
        raise ValueError("invalid uplink channel")
    return channel, payload


_IMAGE_HWM = 8
_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_IMAGE_AUTH_KEY = "_auth"

# ── Uplink frame size ceilings ─────────────────────────────────────
#
# RCVHWM bounds how MANY frames may queue on a PULL socket, never how large one
# frame is. Without a size ceiling the host receives and MessagePack-decodes
# whatever a plugin writes onto the socket, and push_message()'s local size
# check is no defence: a plugin that writes an authenticated frame onto the
# socket directly never runs it. The image uplink has carried MAXMSGSIZE from
# the start; both MessagePack uplinks carry one now too.
#
# The number is derived, not chosen. Downstream, ingest measures each delta
# item against MESSAGE_PLANE_PAYLOAD_MAX_BYTES and drops the whole item when it
# is over, so a payload above that cap cannot survive ingest no matter what the
# transport does with it. One frame here is either a single such payload
# (CH_MSG) or one batch of them (CH_MSG_BATCH), and the batcher flushes at
# PLUGIN_ZMQ_MESSAGE_PUSH_BATCH_SIZE items, so the largest frame legitimate
# traffic can produce is batch_size * payload_max. The headroom on top covers
# the msgpack envelope wrapped around those payloads -- the token, the channel
# tag, the "items" array header, tens of bytes in practice -- and is generous
# because the failure mode of a ceiling set too low is silent loss of real
# traffic (see below), while the cost of extra slack is bounded by the same
# per-frame allocation this limit exists to bound. Both settings are host-side,
# so plugin code cannot widen the ceiling by setting an env var.
#
# The control uplink used to borrow that same number. It no longer does, and
# the reason is worth keeping: payload_max * batch_max carries a batch
# multiplier, and the control channel is never batched -- borrowing it handed
# every control frame about 128 MiB, which with this socket's high-water mark
# is not a bound at all. ``_control_uplink_max_bytes`` below now derives its
# own from the widest legitimate CH_RES (tool images + an output allowance +
# envelope); see PLUGIN_ZMQ_CONTROL_UPLINK_MAX_BYTES in plugin/settings.py.
#
# The CH_COMM EXPORT_PUSH that used to justify the borrowed number -- base64 of
# at most EXPORT_INLINE_BINARY_MAX_BYTES, roughly 341 KiB on today's defaults --
# is an order of magnitude below that derivation, so it still clears easily; a
# test pins that. The ceiling, not the channel check, is what bounds the
# per-frame allocation: libzmq has already read the frame into memory by the
# time anything can look at its channel tag.
#
# What this does at runtime, because it is easy to expect the wrong thing:
# libzmq enforces MAXMSGSIZE in the receiving engine, not in the recv() call.
# An oversized frame is discarded there and the offending peer's connection is
# torn down (ZMTP 3.x). recv() does NOT raise, and the host never sees the
# bytes -- so there is no error path to write here. The observable behaviour is
# that the frame simply never arrives, and the child's PUSH socket reconnects
# with whatever it had in flight lost.
_MESSAGE_ENVELOPE_HEADROOM_BYTES = 64 * 1024


def _message_uplink_max_bytes() -> int:
    """Return the byte ceiling for one frame on the message uplink."""
    from plugin.settings import (
        MESSAGE_PLANE_PAYLOAD_MAX_BYTES,
        PLUGIN_ZMQ_MESSAGE_PUSH_BATCH_SIZE,
    )

    payload_max = max(1, int(MESSAGE_PLANE_PAYLOAD_MAX_BYTES))
    batch_max = max(1, int(PLUGIN_ZMQ_MESSAGE_PUSH_BATCH_SIZE))
    return payload_max * batch_max + _MESSAGE_ENVELOPE_HEADROOM_BYTES


def _control_uplink_max_bytes() -> int:
    """Return the byte ceiling for one frame on the control uplink.

    NOT the message plane's ceiling any more. That number is
    ``payload_max * batch_max``, and the batch multiplier is definitionally
    wrong for a channel that is never batched -- borrowing it handed every
    control frame about 128 MiB, which together with this socket's 5,000-message
    high-water mark is not a bound at all.

    Its own number instead, derived from the widest legitimate control frame.
    Today that is a CH_RES carrying tool images: at most ``_MAX_TOOL_IMAGES``
    of ``_MAX_TOOL_IMAGE_B64_BYTES`` each. The other candidate, a CH_COMM
    EXPORT_PUSH, is an order of magnitude smaller (~341 KiB base64). A test
    pins the relationship to those tool-image constants, so tightening this
    cannot silently start deleting real tool results -- which is exactly what
    the old comment refused to risk, and why it settled for the wrong number.
    """
    from plugin.settings import PLUGIN_ZMQ_CONTROL_UPLINK_MAX_BYTES

    return max(1, int(PLUGIN_ZMQ_CONTROL_UPLINK_MAX_BYTES))


def _refuse_oversized_uplink_frame(channel: str, data: bytes) -> None:
    """Fail loudly here rather than letting the receiving engine drop it.

    libzmq enforces MAXMSGSIZE in the receiver's engine, and an oversized frame
    does not merely get discarded — it takes the offending peer's connection
    with it (see the note above ``_control_uplink_max_bytes``). So a single
    valid-but-large tool result would tear down the plugin's control uplink,
    with nothing on either side saying why.

    The ceiling is a transport fact, not a contract: the SDK places no limit on
    what a tool may return, so this cannot be "the output limit". It is the
    point at which the caller has to be told, instead of the host waiting for a
    result that was destroyed in transit. Callers of ``ChannelSender.put`` all
    log around it, so this surfaces as a named failure with both numbers in it.
    """
    # 只管控制通道。消息那侧的丢弃语义是被测试钉住的设计（上游已经按
    # MESSAGE_PLANE_PAYLOAD_MAX_BYTES 逐条校验过，传输层的上限是最后一道，
    # 且那条用例明确断言"静默、但连接还活着"）。控制帧两样都没有：工具输出
    # 在 SDK 契约里没有上限，而它和图片共用同一帧。
    if channel in _MESSAGE_UPLINK_CHANNELS:
        return
    cap = _control_uplink_max_bytes()
    if len(data) > cap:
        raise ValueError(
            f"uplink frame too large for channel {channel}: "
            f"{len(data)} > {cap} bytes; the receiving socket would drop it "
            "and close the connection"
        )


def _authenticate_image_metadata(
    metadata: dict,
    *,
    expected_token: str,
) -> dict:
    """Strip and verify the uplink token carried by an image metadata frame.

    The image uplink does not go through ``_decode_uplink`` — re-packing
    megabytes of pixels through MessagePack would buy nothing — so the same
    credential check lives here instead. It is the only gate on this socket:
    without it, this would be the one child → host path any local process that
    can guess the port could write to.
    """
    supplied_token = metadata.pop(_IMAGE_AUTH_KEY, None)
    if (
        not isinstance(supplied_token, str)
        or not expected_token
        or not secrets.compare_digest(
            supplied_token.encode("utf-8"),
            expected_token.encode("utf-8"),
        )
    ):
        raise ValueError("invalid image uplink credential")
    return metadata


# ═══════════════════════════════════════════════════════════════════
# Host-side transport (runs in the user_plugin_server process)
# ═══════════════════════════════════════════════════════════════════

_IMG_LOCK_SHUTDOWN_WAIT_S = 2.0

# 放弃排空之后再等它退出的时间。只够跑完当前这一次 send，不是第二次排空预算。
_BATCHER_ABANDON_JOIN_S = 1.0

# send_uplink_nowait 等发送锁的上限。只够让开一次在途的 send，不是排队预算。
_UPLINK_NOWAIT_LOCK_WAIT_S = 0.05

# 关 uplink socket 之前等它那把发送锁的时间。等不到就交给 ctx.term() 打断持锁
# 者、由它自己关，所以这个值只是给调度留余量，不是等排空。
_UPLINK_CLOSE_LOCK_WAIT_S = 2.0


# 每个 host 一把 uplink token（见 HostTransport.__init__）。这些 token 全部挂在
# state.plugin_hosts 上，而插件进程是裸 multiprocessing.Process 起的，POSIX 上
# 就是 fork——启动插件 B 的那一刻，B 的子进程整份继承了这张表，里面有 A 的
# HostTransport、A 的 token、A 的 downlink/uplink 端点字符串。B 于是能拿 A 的
# 凭证造一个 ChildTransport，往 A 的 socket 上发合法的 CH_RES/CH_STS/CH_COMM，
# A 侧会当作 A 自己发的——本分支新加的 per-host 鉴权就这么绕过去了。
#
# plane_bridge 的 ingest token 已经有同款钩子，但它只重铸自己那一个模块级变量；
# 这里的 token 是每个 host 一份、挂在共享状态上的，要另外擦。
#
# ⚠️ 这是纵深防御，不是边界。fork 之后子进程的堆里仍然留着那些字符串的字节，
# 一个存心的插件仍可以去翻自己的内存。真正的结构性解法是让插件进程不用 fork
# （spawn/forkserver）——Windows 本来就是 spawn，所以插件代码其实已经在 spawn
# 下跑得通了。那是个有性能代价的架构改动，不在本次范围内。
def _scrub_inherited_host_credentials() -> None:
    """Drop other hosts' credentials from a freshly forked child."""
    # 用 sys.modules 而不是 import：这是 fork 之后的子进程，父进程可能正好有别的
    # 线程持着 import 锁，在钩子里触发一次真正的 import 就可能直接死锁。而且逻辑
    # 上也够——父进程没导入过 state，就没有可继承的东西。
    mod = sys.modules.get("plugin.core.state")
    state_obj = getattr(mod, "state", None) if mod is not None else None
    hosts = getattr(state_obj, "plugin_hosts", None)
    if not isinstance(hosts, dict):
        return
    for host in list(hosts.values()):
        if hasattr(host, "_model_gateway_token"):
            try:
                host._model_gateway_token = ""
            except Exception:
                pass
        transport = getattr(host, "transport", None)
        if transport is None:
            continue
        # 直接把值打掉，而不是只丢引用：子进程里可能还有别处引着这个对象。
        if hasattr(transport, "_uplink_token"):
            try:
                transport._uplink_token = ""
            except Exception:
                pass
    hosts.clear()


# 和 plane_bridge 一样，把"注册成功了没有"记成模块状态：两个 pytest job 都跑
# windows-latest，Windows 走 spawn，fork 行为本身在 CI 上一次都执行不到，
# 只有这个标志能让守卫在任何平台上断言接线还在。
_HOST_CREDENTIAL_FORK_HOOK_REGISTERED = False

if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_scrub_inherited_host_credentials)
    _HOST_CREDENTIAL_FORK_HOOK_REGISTERED = True


class HostTransport:
    """Async ZMQ transport for the host (main-process) side.

    Create in ``PluginHost.__init__`` — sockets are bound immediately so that
    the endpoint strings are available for the child process args.

    All public send/recv methods are *coroutines* and must be called from the
    event loop.
    """

    def __init__(self) -> None:
        self._ctx = zmq.asyncio.Context()
        self._uplink_token = secrets.token_urlsafe(32)

        # Downlink: host → child (PUSH/PULL)
        self._dl_sock = self._ctx.socket(zmq.PUSH)
        self._dl_sock.setsockopt(zmq.LINGER, _LINGER_MS)
        self._dl_sock.setsockopt(zmq.SNDHWM, 5000)
        self._dl_sock.bind("tcp://127.0.0.1:*")
        self.downlink_endpoint: str = self._dl_sock.getsockopt(zmq.LAST_ENDPOINT).decode()

        # Uplink: child → host (PUSH/PULL)
        self._ul_sock = self._ctx.socket(zmq.PULL)
        self._ul_sock.setsockopt(zmq.LINGER, 0)
        self._ul_sock.setsockopt(zmq.RCVHWM, 5000)
        # RCVHWM above is a frame count; this is the frame size. recv() below
        # refuses CH_MSG/CH_MSG_BATCH on this socket, but that refusal cannot
        # bound memory on its own: libzmq has already allocated the frame by
        # the time the channel tag can be read, so without a ceiling a plugin
        # could aim an arbitrarily large batch at the control endpoint and the
        # host would pay for it before rejecting it. See
        # _control_uplink_max_bytes for where the number comes from, and the
        # note above it for what libzmq does with a frame over it (drops the
        # frame AND the peer, silently -- recv never raises).
        self._ul_sock.setsockopt(zmq.MAXMSGSIZE, _control_uplink_max_bytes())
        self._ul_sock.bind("tcp://127.0.0.1:*")
        self.uplink_endpoint: str = self._ul_sock.getsockopt(zmq.LAST_ENDPOINT).decode()

        # Message uplink: physically separate plugin traffic from lifecycle,
        # tool, and status responses so slow message routing cannot create
        # head-of-line blocking on the control uplink.
        self._msg_sock = self._ctx.socket(zmq.PULL)
        self._msg_sock.setsockopt(zmq.LINGER, 0)
        self._msg_sock.setsockopt(zmq.RCVHWM, 5000)
        # RCVHWM above is a frame count; this is the frame size. See
        # _message_uplink_max_bytes for how the bound is derived and what
        # libzmq does with a frame that exceeds it (it drops the frame and
        # drops the peer -- recv never raises).
        self._msg_sock.setsockopt(zmq.MAXMSGSIZE, _message_uplink_max_bytes())
        self._msg_sock.bind("tcp://127.0.0.1:*")
        self.message_uplink_endpoint: str = self._msg_sock.getsockopt(
            zmq.LAST_ENDPOINT
        ).decode()

        # Bulk image uplink: isolated from status/result/control traffic so a
        # full media queue cannot head-of-line block the plugin control plane.
        self._img_sock = self._ctx.socket(zmq.PULL)
        self._img_sock.setsockopt(zmq.LINGER, 0)
        self._img_sock.setsockopt(zmq.RCVHWM, _IMAGE_HWM)
        self._img_sock.setsockopt(zmq.MAXMSGSIZE, _IMAGE_MAX_BYTES)
        self._img_sock.bind("tcp://127.0.0.1:*")
        self.image_uplink_endpoint: str = self._img_sock.getsockopt(zmq.LAST_ENDPOINT).decode()

        self._closed = False

    @property
    def uplink_token(self) -> str:
        return self._uplink_token

    # ── send helpers ─────────────────────────────────────────────

    async def send_command(self, msg: dict) -> None:
        """Send a command on the downlink."""
        await self._dl_sock.send(pickle.dumps((CH_CMD, msg)))

    async def send_response(self, msg: dict) -> None:
        """Send a plugin-to-plugin response on the downlink."""
        await self._dl_sock.send(pickle.dumps((CH_RESP, msg)))

    # ── recv helper ──────────────────────────────────────────────

    async def recv(self, timeout_ms: int = 1000) -> Optional[Tuple[str, dict]]:
        """Receive one ``(channel, payload)`` from the control uplink.

        Returns *None* on timeout. Message channels are refused here: they have
        their own socket with its own ceiling, and accepting them on this one
        would let a plugin route message traffic around that ceiling.
        """
        if await self._ul_sock.poll(timeout=timeout_ms):
            raw = await self._ul_sock.recv()
            return _decode_uplink(
                raw,
                expected_token=self._uplink_token,
                allowed_channels=_CONTROL_UPLINK_CHANNELS,
            )
        return None

    async def recv_message(
        self,
        timeout_ms: int = 1000,
    ) -> Optional[Tuple[str, dict]]:
        """Receive one authenticated message or message batch."""
        if await self._msg_sock.poll(timeout=timeout_ms):
            raw = await self._msg_sock.recv()
            return _decode_uplink(
                raw,
                expected_token=self._uplink_token,
                allowed_channels=_MESSAGE_UPLINK_CHANNELS,
            )
        return None

    async def recv_image(self, timeout_ms: int = 1000) -> Optional[Tuple[dict, bytes]]:
        """Receive one metadata/raw-bytes upload from the isolated media socket."""
        if await self._img_sock.poll(timeout=timeout_ms):
            frames = await self._img_sock.recv_multipart()
            if len(frames) != 2:
                raise ValueError("image upload must contain metadata and data frames")
            try:
                metadata = json.loads(frames[0].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("image upload metadata must be valid JSON") from exc
            if not isinstance(metadata, dict):
                raise TypeError("image upload metadata must be a dict")
            if not all(isinstance(key, str) for key in metadata):
                raise TypeError("image upload metadata keys must be strings")
            metadata = _authenticate_image_metadata(
                metadata,
                expected_token=self._uplink_token,
            )
            return metadata, bytes(frames[1])
        return None

    # ── lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sock in (
            self._dl_sock,
            self._ul_sock,
            self._msg_sock,
            self._img_sock,
        ):
            try:
                sock.close(linger=0)
            except Exception:
                pass
        try:
            self._ctx.term()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# Child-side transport (runs in the plugin child process)
# ═══════════════════════════════════════════════════════════════════

class ChildTransport:
    """Transport for the child (plugin-process) side.

    * **Downlink receive** uses ``zmq.asyncio`` for native ``await``.
    * **Uplink sends** use regular ``zmq.PUSH`` sockets guarded by
      :class:`threading.Lock` instances so that timer threads can safely call
      ``channel_sender(...).put_nowait(...)`` without conflicting with the
      event-loop thread.
    """

    def __init__(
        self,
        downlink_endpoint: str,
        uplink_endpoint: str,
        uplink_token: str,
        *,
        message_uplink_endpoint: str | None = None,
        image_uplink_endpoint: str | None = None,
    ) -> None:
        # Sync context — used for the uplink PUSH socket (thread-safe via lock)
        self._sync_ctx = zmq.Context()

        self._ul_sock = self._sync_ctx.socket(zmq.PUSH)
        self._ul_sock.setsockopt(zmq.LINGER, _LINGER_MS)
        self._ul_sock.setsockopt(zmq.SNDHWM, 5000)
        self._ul_sock.connect(uplink_endpoint)
        self._ul_lock = threading.Lock()

        self._msg_sock = self._ul_sock
        self._msg_lock = self._ul_lock
        if message_uplink_endpoint:
            self._msg_sock = self._sync_ctx.socket(zmq.PUSH)
            self._msg_sock.setsockopt(zmq.LINGER, _LINGER_MS)
            self._msg_sock.setsockopt(zmq.SNDHWM, 5000)
            self._msg_sock.connect(message_uplink_endpoint)
            self._msg_lock = threading.Lock()
        self._message_batcher = None
        self._message_batcher_init_lock = threading.Lock()

        # Async context — used for the downlink PULL socket (event-loop only)
        self._async_ctx = zmq.asyncio.Context()
        self._dl_sock = self._async_ctx.socket(zmq.PULL)
        self._dl_sock.setsockopt(zmq.LINGER, 0)
        self._dl_sock.connect(downlink_endpoint)

        self._img_sock: Any | None = None
        self._img_lock = threading.Lock()
        if image_uplink_endpoint:
            self._img_sock = self._sync_ctx.socket(zmq.PUSH)
            self._img_sock.setsockopt(zmq.LINGER, 0)
            self._img_sock.setsockopt(zmq.SNDHWM, _IMAGE_HWM)
            self._img_sock.connect(image_uplink_endpoint)

        self._downlink_endpoint = downlink_endpoint
        self._uplink_endpoint = uplink_endpoint
        self._message_uplink_endpoint = message_uplink_endpoint or uplink_endpoint
        self._uplink_token = uplink_token
        self._closed = False

    # ── downlink (async, event-loop only) ────────────────────────

    async def recv_downlink(self, timeout_ms: int = 1000) -> Optional[Tuple[str, dict]]:
        """Receive ``(channel, payload)`` from the downlink, or *None* on timeout."""
        if await self._dl_sock.poll(timeout=timeout_ms):
            raw = await self._dl_sock.recv()
            return pickle.loads(raw)  # type: ignore[return-value]
        return None

    async def send_image(
        self,
        request_id: str,
        *,
        mime: str,
        data: bytes,
        timeout: float,
    ) -> None:
        """Send raw image bytes without using the shared control uplink."""
        if self._img_sock is None:
            raise RuntimeError("image transport is not configured")
        payload = bytes(data)
        if len(payload) > _IMAGE_MAX_BYTES:
            raise ValueError(
                f"image payload exceeds the {_IMAGE_MAX_BYTES} byte transport limit"
            )
        metadata = json.dumps(
            {
                "type": "IMAGE_UPLOAD",
                "request_id": str(request_id),
                "mime": str(mime),
                # Same credential the MessagePack uplinks carry, in the frame
                # the host already parses: one answer to "who is allowed to
                # write into the host" across all uplinks.
                _IMAGE_AUTH_KEY: self._uplink_token,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await asyncio.to_thread(
            self._send_image_sync,
            metadata,
            payload,
            timeout,
        )

    def _send_image_sync(
        self,
        metadata: bytes,
        payload: bytes,
        timeout: float,
    ) -> None:
        """Bound one image send while serialising access across plugin threads."""
        if timeout <= 0:
            raise ValueError("image transport timeout must be positive")
        started_at = time.monotonic()
        if not self._img_lock.acquire(timeout=timeout):
            raise TimeoutError(f"image transport send timed out after {timeout}s")
        try:
            if self._closed or self._img_sock is None:
                raise RuntimeError("image transport is closed")
            remaining = timeout - (time.monotonic() - started_at)
            if remaining <= 0 or not self._img_sock.poll(
                timeout=max(1, int(remaining * 1000)),
                flags=zmq.POLLOUT,
            ):
                raise TimeoutError(f"image transport send timed out after {timeout}s")
            try:
                self._img_sock.send_multipart([metadata, payload], flags=zmq.NOBLOCK)
            except zmq.Again:
                raise TimeoutError(
                    f"image transport send timed out after {timeout}s"
                ) from None
        finally:
            # If a shutdown started while this send held the lock, the sender
            # is the last thread that will touch the socket, so the sender
            # closes it. libzmq sockets are not thread safe: closing one from
            # another thread while a send or poll is in progress is undefined
            # behaviour, not merely impolite (CodeRabbit).
            if self._closed:
                self._close_img_sock_locked()
            self._img_lock.release()

    # ── uplink (thread-safe, any thread) ─────────────────────────

    def send_uplink(self, channel: str, msg: Any, *, timeout: float = 10.0) -> None:
        """Thread-safe bounded send on the uplink.

        ``timeout`` used to be accepted and ignored: the send was a plain
        blocking ``sock.send``, so a host that stopped draining (or a socket
        sitting at its 5,000-message HWM) wedged the calling thread for good.
        That is the plugin event loop, and it is what a bounded shutdown waits
        on.

        It also held ``lock`` the whole time, which is what made
        ``send_uplink_nowait`` — the "non-blocking" one — block. Bounding this
        bounds that.

        Raises ``queue.Full`` rather than something transport-shaped:
        ``ChannelSender`` stands in for a ``multiprocessing.Queue``, and that
        is what ``Queue.put(timeout=...)`` raises when it cannot deliver.
        """
        data = _encode_uplink(self._uplink_token, channel, msg)
        _refuse_oversized_uplink_frame(channel, data)
        sock, lock = self._uplink_socket(channel)
        budget = max(0.0, float(timeout))
        deadline = time.monotonic() + budget
        if budget > 0:
            acquired = lock.acquire(timeout=budget)
        else:
            acquired = lock.acquire(blocking=False)
        if not acquired:
            raise queue.Full(f"uplink busy: {channel}")
        try:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            # poll before NOBLOCK rather than a plain send: the wait has to be
            # interruptible by the deadline, and it must happen under the lock
            # because libzmq sockets are not thread safe.
            if not sock.poll(remaining_ms, zmq.POLLOUT):
                raise queue.Full(f"uplink not writable within {budget}s: {channel}")
            sock.send(data, zmq.NOBLOCK)
        finally:
            # 关停期间是这个线程最后碰这个 socket，所以由它来关。和
            # _send_image_sync 的 finally 同一条规则。
            if self._closed:
                self._close_uplink_sock_locked(sock)
            lock.release()

    def send_uplink_nowait(self, channel: str, msg: Any) -> None:
        """Thread-safe non-blocking send on the uplink.

        The lock wait is capped rather than removed. Dropping on the first
        contended microsecond would lose messages that today only ever wait for
        one in-flight send; blocking on it without a cap is what made this
        method's name a lie while a bounded ``send_uplink`` held the lock.
        """
        data = _encode_uplink(self._uplink_token, channel, msg)
        _refuse_oversized_uplink_frame(channel, data)
        sock, lock = self._uplink_socket(channel)
        if not lock.acquire(timeout=_UPLINK_NOWAIT_LOCK_WAIT_S):
            raise queue.Full(f"uplink busy: {channel}")
        try:
            sock.send(data, zmq.NOBLOCK)
        finally:
            if self._closed:
                self._close_uplink_sock_locked(sock)
            lock.release()

    def _uplink_socket(self, channel: str):
        if channel in _MESSAGE_UPLINK_CHANNELS:
            return self._msg_sock, self._msg_lock
        return self._ul_sock, self._ul_lock

    def send_fast_message_nowait(self, msg: Any) -> None:
        if self._closed:
            raise RuntimeError("plugin transport is closed")
        batcher = self._message_batcher
        if batcher is None:
            with self._message_batcher_init_lock:
                if self._closed:
                    raise RuntimeError("plugin transport is closed")
                batcher = self._message_batcher
                if batcher is None:
                    from plugin.settings import (
                        MESSAGE_PLANE_PUSH_BATCHER_ENQUEUE_TIMEOUT_SECONDS,
                        MESSAGE_PLANE_PUSH_BATCHER_MAX_QUEUE,
                        MESSAGE_PLANE_PUSH_BATCHER_REJECT_RATIO,
                        PLUGIN_ZMQ_MESSAGE_PUSH_BATCH_SIZE,
                        PLUGIN_ZMQ_MESSAGE_PUSH_FLUSH_INTERVAL_MS,
                    )

                    batcher = _AuthenticatedMessageBatcher(
                        self,
                        batch_size=PLUGIN_ZMQ_MESSAGE_PUSH_BATCH_SIZE,
                        flush_interval_ms=PLUGIN_ZMQ_MESSAGE_PUSH_FLUSH_INTERVAL_MS,
                        max_queue=MESSAGE_PLANE_PUSH_BATCHER_MAX_QUEUE,
                        reject_ratio=MESSAGE_PLANE_PUSH_BATCHER_REJECT_RATIO,
                        enqueue_timeout_s=(
                            MESSAGE_PLANE_PUSH_BATCHER_ENQUEUE_TIMEOUT_SECONDS
                        ),
                    )
                    batcher.start()
                    self._message_batcher = batcher
        batcher.enqueue(msg)

    # ── channel senders (queue-compatible interface) ─────────────

    def channel_sender(self, channel: str) -> "ChannelSender":
        """Return a :class:`ChannelSender` that mimics ``mp.Queue.put`` / ``put_nowait``."""
        if channel == CH_MSG:
            return MessageChannelSender(self, channel)
        return ChannelSender(self, channel)

    # ── lifecycle ────────────────────────────────────────────────

    def _close_uplink_sock_locked(self, sock: Any) -> None:
        """Close one uplink socket. Callers decide whether they hold its lock."""
        if sock is not None:
            try:
                sock.close(linger=0)
            except Exception:
                pass

    def _close_img_sock_locked(self) -> None:
        """Close the media socket. Callers decide whether they hold _img_lock."""
        if self._img_sock is not None:
            try:
                self._img_sock.close(linger=0)
            except Exception:
                pass

    def close(self) -> None:
        # getattr guards throughout: unit tests build this object with
        # ``__new__`` and populate only the members they exercise, so close()
        # must not assume every field the real ``__init__`` sets.
        batcher_init_lock = getattr(self, "_message_batcher_init_lock", None)
        if batcher_init_lock is None:
            batcher_init_lock = threading.Lock()
        with batcher_init_lock:
            if self._closed:
                return
            self._closed = True
            batcher = getattr(self, "_message_batcher", None)
            self._message_batcher = None
        batcher_exited = True
        if batcher is not None:
            try:
                batcher_exited = bool(batcher.stop(timeout=2.0))
            except Exception:
                batcher_exited = False
            if not batcher_exited:
                logger.warning(
                    "authenticated message batcher still running at shutdown; "
                    "closing its socket under the send lock"
                )
        # 下行只有 recv 路径碰，没有并发的发送方，直接关。
        dl_sock = getattr(self, "_dl_sock", None)
        if dl_sock is not None:
            try:
                dl_sock.close(linger=0)
            except Exception:
                pass

        # 两条 uplink socket 都在自己的锁下关，做法和媒体 socket 一样（下面
        # 那段注释里量过的那个）：libzmq 的 socket 不是线程安全的，另一个线程
        # 正在 send/poll 时从这里关，是崩溃不是失礼。
        #
        # 拿不到锁就**不关**——这正是媒体那条路的做法，别改成"照样关"：
        # ctx.term() 会先用 ETERM 打断持锁者的 poll/send，它的 finally 看到
        # self._closed 就把 socket 关掉，然后 term 返回。所以漏关不会挂住终止。
        # getattr 成对取，不走 _uplink_socket：单测用 __new__ 造这个对象、只填
        # 自己要用的字段，close() 不能假设 __init__ 的每个字段都在（这条约定
        # 上面那段注释里就写着）。
        for label, sock_attr, lock_attr in (
            (CH_STS, "_ul_sock", "_ul_lock"),
            (CH_MSG, "_msg_sock", "_msg_lock"),
        ):
            sock = getattr(self, sock_attr, None)
            lock = getattr(self, lock_attr, None)
            if sock is None or lock is None:
                continue
            acquired = False
            try:
                acquired = lock.acquire(timeout=_UPLINK_CLOSE_LOCK_WAIT_S)
            except Exception:
                acquired = False
            if acquired:
                try:
                    self._close_uplink_sock_locked(sock)
                finally:
                    try:
                        lock.release()
                    except Exception:
                        pass
            else:
                logger.warning(
                    f"uplink send lock still held at shutdown ({label}); "
                    "leaving the close to the sender it interrupts"
                )
        # Bounded, and deliberately not unconditional. A handler inside
        # _send_image_sync holds this lock for its whole upload, so an
        # unconditional acquire made shutdown wait on an in-flight upload
        # (Codex P2). Closing the socket anyway is not the answer either:
        # libzmq sockets are not thread safe, so closing one while a send is in
        # progress is undefined behaviour -- a crash instead of a hang
        # (CodeRabbit).
        if self._img_lock.acquire(timeout=_IMG_LOCK_SHUTDOWN_WAIT_S):
            try:
                self._close_img_sock_locked()
            finally:
                self._img_lock.release()

        # Both contexts terminate here, including the one that may still own
        # an open media socket.
        #
        # This looks like it should block -- term() does wait for every socket
        # in the context to close. It does not, because zmq_ctx_term first
        # interrupts blocked calls in that context with ETERM and only then
        # waits. The in-flight sender's poll/send raises ContextTerminated at
        # once, its finally closes the media socket, and term returns.
        # Measured on pyzmq 27.1.0 / libzmq 4.3.5: a poll(30_000) blocked on a
        # full PUSH socket is interrupted in 0.000s and term returns
        # immediately.
        #
        # An earlier revision deferred this termination to the sender, on the
        # belief that terminating here would wait out the sender's full upload
        # timeout. That belief was wrong, and the deferral was worse than the
        # thing it avoided: the flag was written outside _img_lock and read
        # inside it, so a sender that finished first read a stale False and
        # nobody terminated the context -- and the designated hand-off thread
        # is a daemon the process never joins.
        for ctx in (self._async_ctx, self._sync_ctx):
            try:
                ctx.term()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════
# ChannelSender — drop-in for mp.Queue on the child side
# ═══════════════════════════════════════════════════════════════════

class ChannelSender:
    """Queue-like object that tags each message with a *channel* and sends it
    through the shared :class:`ChildTransport` uplink.

    Accepted by :class:`~plugin.core.context.PluginContext` in place of the
    old ``multiprocessing.Queue`` references (``status_queue``, ``message_queue``, etc.).
    """

    __slots__ = ("_transport", "_ch")

    def __init__(self, transport: ChildTransport, channel: str) -> None:
        self._transport = transport
        self._ch = channel

    def put(self, obj: Any, block: bool = True, timeout: float | None = None) -> None:
        self._transport.send_uplink(self._ch, obj, timeout=timeout or 10.0)

    def put_nowait(self, obj: Any) -> None:
        self._transport.send_uplink_nowait(self._ch, obj)

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        raise NotImplementedError("ChannelSender is send-only; use transport.recv_downlink() for reads")

    def get_nowait(self) -> Any:
        raise NotImplementedError("ChannelSender is send-only")

    # no-ops for mp.Queue compat
    def close(self) -> None:
        pass

    def cancel_join_thread(self) -> None:
        pass


class MessageChannelSender(ChannelSender):
    """Message sender with an authenticated, bounded batching fast path."""

    def put_fast_nowait(self, obj: Any) -> None:
        self._transport.send_fast_message_nowait(obj)


class _AuthenticatedMessageBatcher:
    def __init__(
        self,
        transport: ChildTransport,
        *,
        batch_size: int,
        flush_interval_ms: int,
        max_queue: int,
        reject_ratio: float,
        enqueue_timeout_s: float,
    ) -> None:
        self._transport = transport
        self._batch_size = max(1, int(batch_size))
        self._flush_interval_s = max(0.001, float(flush_interval_ms) / 1000.0)
        self._max_queue = int(max_queue)
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=self._max_queue)
        self._reject_ratio = min(1.0, max(0.0, float(reject_ratio)))
        self._enqueue_timeout_s = max(0.0, float(enqueue_timeout_s))
        self._dropped = 0
        self._stop = threading.Event()
        # 硬停：置位后 _run 立刻退出，不再排空。只有 stop() 在 join 超时后才
        # 会用它——正常关停仍然把队列发完。和 _stop 一样建在 __init__ 里：
        # _run 会被不经 start() 直接调用（测试就是这么用的）。
        self._abandon = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # 这是重启入口（上面那个 is_alive 判断就是为它写的），所以两个关停位都
        # 得在这里落下：不清的话新线程一进 _run 就撞上 _abandon / _stop 直接
        # 退出，队列一条不发，而且完全静默——batcher 本来就是 fire-and-forget。
        self._stop.clear()
        self._abandon.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="plugin-authenticated-message-batcher",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, item: Any) -> None:
        if self._stop.is_set() or not isinstance(item, dict):
            raise RuntimeError("authenticated message batcher unavailable")
        if (
            self._max_queue > 0
            and self._reject_ratio > 0
            and self._queue.qsize() >= self._queue.maxsize * self._reject_ratio
        ):
            raise queue.Full
        self._queue.put(item, timeout=self._enqueue_timeout_s)

    def stop(self, timeout: float = 1.0) -> bool:
        """Stop the worker. Returns whether it actually exited.

        The drain below is deliberately generous — ``_run`` keeps flushing
        while the queue is non-empty even after ``_stop`` — and the queue holds
        up to 100,000 items, so a loaded shutdown can outlast any timeout. The
        caller closes ``_msg_sock`` next, and libzmq sockets are not thread
        safe: closing one while this thread is inside ``send_uplink_nowait`` is
        undefined behaviour, not a lost batch.

        So the timeout escalates instead of being advisory: give up the
        remaining batches, then join again. The return value tells the caller
        whether touching the socket is safe.
        """
        self._stop.set()
        t = self._thread
        if t is None:
            return True
        budget = max(0.0, float(timeout))
        t.join(timeout=budget)
        if not t.is_alive():
            return True
        # 排空排不完就别排了。丢掉的批次本来也活不过这次关停。
        self._abandon.set()
        t.join(timeout=_BATCHER_ABANDON_JOIN_S)
        return not t.is_alive()

    def _run(self) -> None:
        batch: list[dict] = []
        deadline = time.monotonic() + self._flush_interval_s
        while not self._abandon.is_set() and (
            batch or not self._stop.is_set() or not self._queue.empty()
        ):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                item = None
            if item is not None:
                batch.append(item)
            now = time.monotonic()
            if not batch and item is None:
                deadline = now + self._flush_interval_s
                continue
            if batch and (
                len(batch) >= self._batch_size
                or now >= deadline
                or (self._stop.is_set() and self._queue.empty())
            ):
                try:
                    self._transport.send_uplink_nowait(
                        CH_MSG_BATCH,
                        {"items": batch},
                    )
                except Exception as exc:
                    self._dropped += len(batch)
                    logger.warning(
                        "Authenticated message batch dropped "
                        f"(items={len(batch)} total_dropped={self._dropped} "
                        f"error_type={type(exc).__name__})"
                    )
                batch = []
                deadline = now + self._flush_interval_s
