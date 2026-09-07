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

"""Own per-character runtime state, sync tasks, and agent-event dispatch."""

import asyncio
import atexit
import base64
import ipaddress
import logging
import sys
import time
import traceback
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Optional
from urllib.parse import urlsplit

from PIL import Image

from config import MONITOR_SERVER_PORT, USER_NOTIFICATION_ERROR_MAX_CHARS
from main_logic import core, cross_server
from main_logic.agent_event_bus import notify_analyze_ack
from main_logic.proactive_delivery import (
    CALLBACK_EXPIRES_AT_KEY,
    CALLBACK_IMAGE_MAX_COUNT,
    CALLBACK_IMAGE_MAX_TOTAL_BYTES,
    approx_base64_decoded_bytes,
)
from plugin.sdk.shared.core.images import (
    MAX_SOURCE_IMAGE_PIXELS,
    normalize_image_to_jpeg,
)
from utils.config_manager import get_reserved
from utils.internal_http_client import get_internal_http_client
from utils.screenshot_utils import normalize_image_for_model

from ._shared import runtime

_IS_MAIN_PROCESS = runtime.is_main_process
_config_manager = runtime.config_manager
logger = runtime.logger

_PLUGIN_IMAGE_MAX_BYTES = 8 * 1024 * 1024
# Two, not four: each in-flight transfer is bounded only by the per-image
# ceiling, so the batch width IS the aggregate-budget overshoot while fetching.
_PLUGIN_IMAGE_FETCH_BATCH_SIZE = 2
# Per-push budgets. A single push carries an unbounded ``parts`` list, so
# without these one plugin can pin (count x 8 MiB) of decoded image bytes in
# this handler — and, for ``respond``, keep it pinned on the queued callback
# until the pacing manager releases it. The count cap also bounds how long the
# event handler blocks on fetches (ceil(count / batch) x the 2s per-fetch
# timeout). Budgets are in DECODED bytes, matching _PLUGIN_IMAGE_MAX_BYTES.
#
# The 8 images / 8 MiB figures are the contract PLUGIN_DEVELOPMENT_GUIDE.md
# already advertises to plugin authors ("单条消息最多向模型注入 8 张、合计
# 8 MiB 图片"). One push is one turn's worth, so share the constants with the
# per-turn budget in proactive_delivery rather than keeping a second spelling.
_PLUGIN_IMAGE_MAX_COUNT = CALLBACK_IMAGE_MAX_COUNT
_PLUGIN_IMAGE_TOTAL_MAX_BYTES = CALLBACK_IMAGE_MAX_TOTAL_BYTES
# The chat path is separate: URL-backed blocks cost the frontend a fetch (count
# only), while inline data: URLs ride the WebSocket frame itself (count+bytes).
_PLUGIN_CHAT_IMAGE_MAX_COUNT = 8
_PLUGIN_CHAT_INLINE_TOTAL_MAX_BYTES = 8 * 1024 * 1024
# Bounded base64 prefix that the dimension probe reads. Large enough for PNG
# and JPEG headers; formats Pillow cannot parse from a truncated stream fall
# back to a full decode.
_PLUGIN_CHAT_HEADER_PREFIX_B64_CHARS = 64 * 1024
# Frames are also bounded on COUNT, not only on cumulative pixels: thousands of
# 1x1 frames multiply to almost nothing yet still cost a decode and a timer tick
# each, so a pixel budget alone does not see them (Codex). Well above any real
# sticker, which is tens of frames.
_PLUGIN_CHAT_MAX_ANIMATION_FRAMES = 300

# Idle read timeout, and a TOTAL deadline over the whole fetch.
#
# httpx's timeout bounds ONE read, not the transfer: an endpoint sending a few
# bytes just inside each interval satisfies it forever while holding a
# connection and a slot in the bounded fetch pool (Codex). The dual of the same
# bound on the browser-facing /media route -- both fetch from the same store,
# and a defect fixed on one side belongs on the other.
_PLUGIN_IMAGE_FETCH_TOTAL_DEADLINE_S = 8.0

_approx_decoded_bytes = approx_base64_decoded_bytes


class _PushImageByteBudget:
    """How many decoded image bytes ONE push may still retain.

    Charged by the event handler in canonical part order, once a batch of
    resolutions is in -- never from inside the concurrent fetches themselves.
    That was tried and reverted: drawing mid-transfer bounds the aggregate but
    makes survival depend on which network read finishes first, so the set of
    parts reaching the model varies with timing (Codex P2). Bounding each
    transfer separately by the per-image ceiling and charging afterwards in
    order costs a wider in-flight peak and buys determinism.

    Single event loop, so no lock. There is no refund: a payload is charged
    once, at its retained size, after it is fully resolved.
    """

    __slots__ = ("remaining",)

    def __init__(self, total: int) -> None:
        self.remaining = int(total)

    def draw(self, count: int) -> bool:
        """Reserve ``count`` bytes, or refuse if the pool cannot cover them."""
        if count > self.remaining:
            return False
        self.remaining -= count
        return True



# Pillow's own format names for the raster types a chat bubble can render.
# Keyed off the PARSED bytes, never off the plugin's declared ``mime``.
# Formats whose frame count cannot be read from a truncated prefix: Pillow
# counts GIF/WebP frames by walking the stream, so a prefix always reports 1.
# PNG (APNG) carries acTL right after IHDR, and JPEG cannot animate, so both
# keep the fast path.
_FRAME_COUNT_NEEDS_FULL_PAYLOAD = {"GIF", "WEBP"}

_PILLOW_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
}


def _inline_image_data_url_mime(encoded: str) -> Optional[str]:
    """Return the MIME to publish for an inline payload, or None to reject it.

    Two jobs, both of which have to key off the bytes rather than the part:

    Bombs. The byte budget is structurally blind to them: a 20000x20000
    single-colour PNG compresses to ~1.2 MiB, sails through an 8 MiB cap, and
    costs the renderer ~1.6 GB to decode. Only a pixel count catches that.

    MIME. The result is interpolated into a ``data:`` URL, and a data URL's
    media type ends at the FIRST comma. A part declaring
    ``image/svg+xml,<svg ...></svg>#`` still satisfies a startswith("image/")
    test, but the browser would then treat the injected markup as the payload
    and never look at the bytes validated here. So the MIME comes from the
    format Pillow actually parsed, and anything outside the raster allowlist is
    refused rather than passed through.

    Dimensions are read from a bounded base64 prefix so the probe does not
    scale with payload size (~0.1 ms flat, against ~14 ms to base64-decode a
    full 8 MiB payload just to read its header). WebP is the one format Pillow
    cannot open from a truncated stream, so it takes the full-decode fallback --
    harmless, because a weaponized image is small by construction.

    Returns None when nothing can be established: bytes this host cannot
    inspect are bytes it should not hand to the renderer.
    """
    head = encoded[:_PLUGIN_CHAT_HEADER_PREFIX_B64_CHARS]
    head = head[: len(head) - (len(head) % 4)]
    for candidate in (head, encoded):
        if not candidate:
            continue
        try:
            raw = base64.b64decode(candidate)
        except Exception:
            return None
        try:
            with Image.open(BytesIO(raw)) as image:
                width, height = image.size
                detected = str(image.format or "").upper()
                # n_frames may itself need to walk the stream; a prefix that
                # cannot answer is treated as a single frame and re-checked on
                # the full-decode fallback below.
                try:
                    frames = max(1, int(getattr(image, "n_frames", 1)))
                except Exception:
                    frames = 1
        except Image.DecompressionBombError:
            # Already past Pillow's own ceiling; a full decode cannot help.
            return None
        except Exception:
            continue
        if width <= 0 or height <= 0:
            return None
        if candidate is head and detected in _FRAME_COUNT_NEEDS_FULL_PAYLOAD:
            # The prefix cannot answer for these; counting off it would let an
            # animation through as a single frame.
            try:
                with Image.open(BytesIO(base64.b64decode(encoded))) as full:
                    # NOT n_frames: reading it walks the entire animation
                    # before the ceiling below can reject it, so thousands of
                    # 1x1 frames -- which stay well inside the wire budget --
                    # cost a full walk on the event loop just to be refused
                    # (Codex P2). Stop one frame past the ceiling; that is
                    # already enough to know the image is over it, and it is
                    # all the ceiling needs to decide.
                    frames = 0
                    try:
                        while frames <= _PLUGIN_CHAT_MAX_ANIMATION_FRAMES:
                            full.seek(frames)
                            frames += 1
                    except EOFError:
                        pass
                    frames = max(1, frames)
            except Exception:
                return None
        # Animation multiplies the decode work the single-frame check bounds:
        # hundreds of tiny frames stay under both the wire budget and the
        # per-frame pixel cap while costing the renderer their SUM (Codex P2).
        # Budget the total against the same ceiling rather than inventing a
        # second number -- one animation may cost what one full-size still
        # costs. Deliberately tight: the SDK upload path flattens to JPEG, so
        # animation only reaches here through un-normalized inline bytes.
        if frames > _PLUGIN_CHAT_MAX_ANIMATION_FRAMES:
            return None
        if frames * width * height > MAX_SOURCE_IMAGE_PIXELS:
            return None
        return _PILLOW_FORMAT_TO_MIME.get(detected)
    return None


def _is_local_plugin_media_url(url: str) -> bool:
    """Accept temporary media on any loopback port selected by the plugin host."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        media_id = parsed.path.removeprefix("/media/")
        return (
            parsed.scheme == "http"
            and parsed.username is None
            and parsed.password is None
            and host is not None
            and ipaddress.ip_address(host).is_loopback
            and port is not None
            and not parsed.query
            and not parsed.fragment
            and parsed.path.startswith("/media/")
            and bool(media_id)
            and "/" not in media_id
        )
    except ValueError:
        return False


def _normalize_inline_image_to_jpeg_base64(encoded: str) -> str:
    """Re-encode an inline payload to jpeg so the declared mime is honest."""
    raw = base64.b64decode(encoded)
    return base64.b64encode(normalize_image_to_jpeg(raw)).decode("ascii")


def _normalize_inline_image_to_model_profile(encoded: str) -> str:
    """Re-encode an inline payload to jpeg AND to the model's size profile.

    Two steps rather than one because they answer two different questions and
    only one of them may be skipped.

    ``normalize_image_to_jpeg`` is the SDK's own guarded decode: 32 MiB source
    ceiling, 16 megapixel ceiling, EXIF orientation, alpha flattened onto
    white, a process-wide decode gate. It RAISES on anything it cannot read,
    which is load-bearing here -- the caller turns that into a dropped part, so
    bytes this host could not parse never reach a provider labelled jpeg.

    ``normalize_image_for_model`` then bounds the RESOLUTION. It cannot replace
    the step above (it returns the payload unchanged on failure, which would
    ship an unreadable part as jpeg) and the step above cannot replace it (the
    SDK bounds the long edge at 2048, so a 2048x1536 upload is jpeg, honest,
    and still far past the profile every other model path sends).

    Cost: an image that is already inside the profile pays nothing extra --
    ``normalize_image_for_model`` returns the same string object for a jpeg
    within both bounds. Only an oversized one pays a second decode+encode, and
    it needed a resample either way.
    """
    return normalize_image_for_model(_normalize_inline_image_to_jpeg_base64(encoded))


async def _resolve_plugin_model_image(part: dict[str, Any]) -> str:
    """Resolve one canonical image part to the model's base64 input.

    This is the MODEL half of the fork. The chat half is
    ``_build_plugin_chat_blocks``, and the two deliberately disagree about
    resolution: everything leaving here is bounded to the model profile
    (``MODEL_IMAGE_MAX_WIDTH`` x ``COMPRESS_TARGET_HEIGHT``, jpeg), while the
    chat copy keeps the plugin's original bytes. See the comment on that
    function for why the asymmetry is the point rather than an oversight.

    Bounds ONE transfer at the per-image ceiling and returns the bytes that
    would actually be retained. Budget accounting is deliberately NOT done
    here: the caller charges the pool in canonical part order, because drawing
    from a shared pool inside concurrent fetches made survival depend on which
    network read finished first rather than on part order (Codex P2).
    """
    encoded = part.get("binary_base64")
    if isinstance(encoded, str) and encoded:
        # Every model client DECLARES jpeg for callback images -- the offline
        # path builds ``data:image/jpeg;base64,`` and realtime Gemini sends
        # ``mime_type="image/jpeg"``. URL images are already jpeg because the
        # SDK normalizes at upload; inline bytes never pass through it, so
        # PNG/WebP would reach the provider mislabelled (Codex P2).
        #
        # Unlike the header probe this is a full decode+encode and Pillow's
        # codecs release the GIL, so a thread genuinely buys something.
        # Returning the NORMALIZED bytes is also what lets the caller charge
        # the budget on what is retained, since jpeg can expand a png.
        return await asyncio.to_thread(
            _normalize_inline_image_to_model_profile, encoded
        )
    url = part.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("plugin image part has no usable payload")
    fetched = await _fetch_plugin_image_base64(url)
    # 两条分支都必须落在同一个档位上——这是本函数的契约，不是内联分支的特权。
    # URL 图确实已经是 jpeg（SDK 上传时归一化过），所以在下游看来「已经处理过
    # 了」，但 SDK 只把长边压到 MAX_IMAGE_EDGE=2048：实测一张插件图到这里是
    # 2048x1536 / ~49 KiB，远在任何字节预算之下，于是一路原样送到模型，高度
    # 1536。字节预算看不见分辨率，所以只有这里能兜住它。
    #
    # 归一化器对已经合规的图返回同一个字符串对象，因此这一步对小图是零成本。
    return await asyncio.to_thread(normalize_image_for_model, fetched)


def _browser_media_url(url: str) -> str:
    """Map a validated plugin media URL to the path the BROWSER should load.

    The absolute form is minted for this process: the main server fetches it
    in-process on the same host, so 127.0.0.1 is correct there. It is wrong for
    the browser whenever the browser is elsewhere -- Docker, or another device
    on the network -- because 127.0.0.1 then means the viewer's own machine,
    and under HTTPS it is blocked as mixed content besides (Codex P2).

    The failure is asymmetric, which is what makes it expensive: the model
    fetch succeeds while the picture does not render, so the character
    describes an image the user cannot see.

    The caller has already run _is_local_plugin_media_url, which pins the path
    to a single /media/<id> segment, so the path is safe to hand over as-is.
    Internal and browser-facing URLs stay separate: only chat blocks are
    rewritten, and the model path keeps the absolute address it fetches.
    """
    return urlsplit(url).path


def _image_part_payloads_conflict(part: Any) -> bool:
    """True when one image part carries BOTH a url and inline bytes.

    The two consumers resolve such a part in OPPOSITE directions: the model
    path prefers ``binary_base64`` and falls back to ``url``, while the chat
    path prefers ``url`` and falls back to the bytes. A part carrying both can
    therefore show the user one image while the character is reasoning about a
    different one, and the reply reads as confidently wrong about what is on
    screen (Codex P2).

    Neither precedence is more correct, and the host cannot tell whether the
    two sources agree without fetching and comparing both. So the ambiguity is
    refused rather than silently resolved -- the same stance the inline MIME
    probe takes: input this host cannot pin down is input it should not act on.
    """
    if not isinstance(part, dict) or part.get("type") != "image":
        return False
    url = part.get("url")
    encoded = part.get("binary_base64")
    return bool(
        isinstance(url, str) and url.strip()
        and isinstance(encoded, str) and encoded.strip()
    )


def _drop_conflicting_image_parts(parts: list[Any]) -> list[Any]:
    """Remove dual-source image parts before EITHER consumer sees them.

    Applied once where the canonical list enters the host, because the model
    path and the chat path read from the same list and a per-consumer check
    would have to be repeated in both -- and stay repeated as entry points are
    added.
    """
    kept = [part for part in parts if not _image_part_payloads_conflict(part)]
    dropped = len(parts) - len(kept)
    if dropped:
        logger.warning(
            "[plugin-image] dropped %d image part(s) carrying both url and "
            "inline bytes; the model and the chat bubble would have resolved "
            "them to different images",
            dropped,
        )
    return kept


def _build_plugin_chat_blocks(
    parts: list[Any],
    *,
    include_text: bool,
) -> list[dict[str, str]]:
    """Project canonical plugin parts to the frontend's supported blocks.

    Images past the per-push budget are dropped; text blocks keep flowing so
    the surviving mix stays in canonical order rather than truncating the tail.

    THE CHAT COPY IS NOT DOWNSCALED, and that asymmetry against
    ``_resolve_plugin_model_image`` is deliberate. One plugin image forks here
    into two consumers with opposite needs:

    * The MODEL gets a jpeg bounded at 1280x720. Beyond that the extra pixels buy no
      comprehension a vision model can use, while every one of them is billed,
      rides ``_conversation_history`` for several more turns, and eats into a
      per-request byte ceiling that rejects the whole message when crossed.
    * The READER gets the resolution the plugin actually uploaded. A screenshot
      of a document, a chart, a code diff is exactly the material a person
      zooms into, and 720p is where small text stops being legible. Shrinking
      the picture on screen would save nothing that matters -- the URL branch
      below is a ``/media/<id>`` reference the browser fetches on its own, so
      those bytes never touch the model request at all, and inline blocks are
      already bounded on their own axis by ``_PLUGIN_CHAT_INLINE_TOTAL_MAX_BYTES``.

    So: bound the copy that is billed and re-sent, leave the copy that is
    merely looked at. Anyone tempted to "unify" the two paths is removing a
    distinction, not a duplication.
    """
    blocks: list[dict[str, str]] = []
    image_count = 0
    inline_bytes = 0
    dropped_images = 0
    bomb_images = 0
    for part in parts:
        if not isinstance(part, dict):
            continue
        if (
            include_text
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            blocks.append({"type": "text", "text": part["text"]})
            continue
        if part.get("type") != "image":
            continue
        if image_count >= _PLUGIN_CHAT_IMAGE_MAX_COUNT:
            dropped_images += 1
            continue
        url = part.get("url")
        if isinstance(url, str) and _is_local_plugin_media_url(url):
            blocks.append({"type": "image", "url": _browser_media_url(url)})
            image_count += 1
            continue
        encoded = part.get("binary_base64")
        mime = str(part.get("mime") or "").strip().lower()
        if isinstance(encoded, str) and encoded and mime.startswith("image/"):
            decoded_bytes = _approx_decoded_bytes(encoded)
            if inline_bytes + decoded_bytes > _PLUGIN_CHAT_INLINE_TOTAL_MAX_BYTES:
                dropped_images += 1
                continue
            safe_mime = _inline_image_data_url_mime(encoded)
            if safe_mime is None:
                # Bytes and pixels are independent axes; the budget above
                # cannot see a bomb, so this check is not redundant with it.
                # It also refuses a MIME we could not corroborate from bytes.
                bomb_images += 1
                continue
            inline_bytes += decoded_bytes
            # safe_mime, NOT the declared ``mime`` -- see the helper's docstring.
            blocks.append(
                {"type": "image", "url": f"data:{safe_mime};base64,{encoded}"}
            )
            image_count += 1
    if dropped_images:
        logger.warning(
            "[EventBus] %d chat image part(s) dropped: over the per-push budget "
            "(max %d images, %d inline bytes)",
            dropped_images,
            _PLUGIN_CHAT_IMAGE_MAX_COUNT,
            _PLUGIN_CHAT_INLINE_TOTAL_MAX_BYTES,
        )
    if bomb_images:
        logger.warning(
            "[EventBus] %d inline chat image(s) dropped: unreadable header or "
            "over the %d pixel decode limit",
            bomb_images,
            MAX_SOURCE_IMAGE_PIXELS,
        )
    return blocks


def _build_plugin_image_chat_blocks(media_parts: list[Any]) -> list[dict[str, str]]:
    """Synchronously validate plugin image parts and build frontend blocks."""
    return _build_plugin_chat_blocks(media_parts, include_text=False)


def _build_ordered_plugin_chat_blocks(parts: list[Any]) -> list[dict[str, str]]:
    """Build supported frontend blocks without changing canonical part order."""
    return _build_plugin_chat_blocks(parts, include_text=True)


def _ordered_plugin_chat_blocks(parts: list[Any], mgr: Any) -> list[dict[str, str]]:
    """Build ordered blocks and expand role placeholders in text."""
    blocks = _build_ordered_plugin_chat_blocks(parts)
    for block in blocks:
        if block["type"] == "text":
            block["text"] = core.apply_role_placeholders(
                block["text"],
                lanlan_name=getattr(mgr, "lanlan_name", "") or "",
                master_name=getattr(mgr, "master_name", "") or "",
            )
    return blocks


async def _fetch_plugin_image_base64(url: str) -> str:
    """Fetch one temporary plugin image without blocking the event loop.

    Bounded by the flat per-image ceiling. The aggregate per-push budget is
    applied by the caller, in canonical part order, once the batch is in.
    """
    if not _is_local_plugin_media_url(url):
        raise ValueError("image URL is not served by the local plugin media store")
    client = get_internal_http_client()
    async with asyncio.timeout(_PLUGIN_IMAGE_FETCH_TOTAL_DEADLINE_S), client.stream(
        "GET",
        url,
        timeout=2.0,
        follow_redirects=False,
    ) as response:
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "").lower()
        if not content_type.startswith("image/"):
            raise ValueError("plugin media store returned a non-image response")
        raw_content_length = str(response.headers.get("content-length") or "").strip()
        if raw_content_length:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                content_length = 0
            if content_length > _PLUGIN_IMAGE_MAX_BYTES:
                raise ValueError("plugin image exceeds the remaining model input budget")
        buffered = bytearray()
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            buffered.extend(chunk)
            if len(buffered) > _PLUGIN_IMAGE_MAX_BYTES:
                raise ValueError("plugin image exceeds the per-image transfer limit")
        content = bytes(buffered)
    if not content:
        raise ValueError("plugin media store returned an empty image")
    encoded = await asyncio.to_thread(base64.b64encode, content)
    return encoded.decode("ascii")


def _resolve_event_source(event: dict) -> tuple[str, str]:
    """Return (source_kind, source_name) for one agent event.

    Extracted so the chat render and the callback build cannot disagree about
    what a push is called: the render happens first and used to derive only
    plugin/system, which quietly relabelled computer-use and browser events.
    """
    channel = str(event.get("channel") or "unknown")
    kind = str(event.get("source_kind") or "").strip()
    name = str(event.get("source_name") or "").strip()
    if not kind:
        if channel == "user_plugin":
            kind = "plugin"
        elif channel in ("computer_use", "cu"):
            kind = "cu"
        elif channel in ("browser_use", "browser"):
            kind = "browser"
        elif channel.startswith("plugin:"):
            kind = "plugin"
            if not name:
                name = channel.split(":", 1)[1]
        else:
            kind = "system"
    return kind, name


def _resolve_callback_origin(event_type: str, event: dict, channel: str) -> str:
    """Resolve task-report vs neutral-event wording at the host boundary."""
    if event_type != "task_result":
        return "event"
    if (
        channel == "user_plugin"
        and bool(event.get("success", True))
        and event.get("result_kind") == "event"
    ):
        return "event"
    return "task_result"


def _resolve_callback_expiry(event: dict, origin: str) -> Optional[float]:
    if origin != "event":
        return None
    raw_expiry = event.get("expires_in_s")
    if isinstance(raw_expiry, bool):
        return None
    try:
        expiry_seconds = float(raw_expiry)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 < expiry_seconds < float("inf"):
        return None
    return time.monotonic() + expiry_seconds


class _SyncMessageQueue(asyncio.Queue):
    """``asyncio.Queue`` with sync ``put()`` aliased to ``put_nowait()``.

    ``sync_message_queue`` was historically a ``queue.Queue`` (thread-safe), with
    producers calling sync ``q.put(item)`` in 14+ places across core.py /
    system_router.py etc. After cross_server became an ``asyncio.Task`` on the main
    loop, message_queue switched to ``asyncio.Queue``. The native
    ``asyncio.Queue.put`` is a coroutine, so the old sync calls would become
    "un-awaited coroutines" — never enqueuing and raising RuntimeWarning.

    Overriding ``put`` as a sync alias of ``put_nowait`` keeps backward
    compatibility: every sync_message_queue is unbounded (no maxsize), so
    ``put_nowait`` can never raise for being full, making the replacement
    semantically equivalent.
    """

    def put(self, item):  # type: ignore[override]
        # 故意 sync override：原 asyncio.Queue.put 是 coroutine。
        self.put_nowait(item)


@dataclass
class RoleState:
    """Per-k runtime state container for a single catgirl.

    Merges what used to be 6 parallel module-global dicts (sync_message_queue /
    sync_shutdown_event / session_id / sync_process / websocket_locks /
    session_manager) into one record held uniformly by role_state[k], avoiding
    half-initialized states + scattered maintenance cost.
    See issue #857 / PR #855 review.

    Invariants:
    - sync_message_queue / websocket_lock are constructed once in
      _ensure_character_slots and **never replaced** afterwards. Especially
      websocket_lock — replacing it would leave coroutines already inside
      ``async with`` blocked on an orphaned old Lock; if any logic needs to
      rebuild role_state[k] wholesale, it must carry the old lock over as-is.
    - session_id / sync_task / session_manager start as None and are assigned
      later by websocket_router / _init_character_resources respectively.

    Legacy fields: ``sync_shutdown_event: ThreadEvent`` and ``sync_process:
    Thread`` are semantically gone since cross_server merged into the main event
    loop (no separate thread anymore). Lifecycle is now managed by ``sync_task:
    asyncio.Task``, with shutdown via ``task.cancel()``.

    However, ``main_routers/shared_state.py``'s ``_RoleStateFieldView`` still
    exposes dict-like views for ``sync_shutdown_event`` / ``sync_process``
    (the public router APIs ``get_sync_shutdown_event()`` /
    ``get_sync_process()``). The view's ``__getitem__`` uses
    ``getattr(rs, field)`` (no default) and would raise ``AttributeError`` if
    the field didn't exist. Keeping these two ``Optional[Any] = None``
    placeholder fields preserves the shim's "always-empty dict" semantics:
    ``__contains__`` sees None and returns False, ``__getitem__`` goes to
    ``raise KeyError``, and every caller gets a consistent empty state instead
    of a crash. The two fields are never assigned anymore; remove them once
    it's confirmed nothing external depends on them.
    """

    sync_message_queue: _SyncMessageQueue
    websocket_lock: asyncio.Lock
    session_id: Optional[str] = None
    sync_task: Optional[asyncio.Task] = None
    # 用 Any 而非 core.LLMSessionManager：避免 dataclass 运行时求值 annotation
    # 时踩到 forward-ref / 循环引用边界
    session_manager: Optional[Any] = None
    # 仅为 main_routers/shared_state.py 的 legacy field-view 提供占位；永远 None
    sync_shutdown_event: Optional[Any] = None
    sync_process: Optional[Any] = None


# 角色名 -> RoleState 的主存储；所有 per-k 同步资源都通过它访问
role_state: dict[str, RoleState] = {}


def _iter_sync_connector_tasks():
    """Iterate over all still-alive sync connector tasks (role_state is the source of truth)."""
    for name, rs in role_state.items():
        task = rs.sync_task
        if task is None:
            continue
        yield name, task


def _signal_sync_connectors_shutdown(*, log: bool = True) -> None:
    """Cancel all sync connector tasks. task.cancel() is synchronous, idempotent, and harmless
    after the loop is closed, so a second atexit invocation is safe."""
    if log:
        logger.info("正在关闭同步连接器 task...")
    for rs in role_state.values():
        try:
            task = rs.sync_task
            if task is not None and not task.done():
                task.cancel()
        except Exception as e:
            logger.debug(f"取消同步连接器 task 失败: {e}", exc_info=True)


async def join_sync_connector_tasks(timeout: float = 3.0) -> list[str]:
    """Await all sync connector tasks in parallel; return the role names that didn't finish within the timeout.

    Normally ``_signal_sync_connectors_shutdown`` has already cancelled them before
    this is called; here we just wait for each task to run its finally cleanup
    (closing ws/session/reader).
    """
    wait_timeout = max(0.0, float(timeout))
    targets = list(_iter_sync_connector_tasks())
    if not targets:
        return []

    async def _wait_one(name: str, task: asyncio.Task) -> str | None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_timeout)
        except asyncio.TimeoutError:
            return name
        except asyncio.CancelledError:
            # task 正常 cancel 走完 finally 后会 raise CancelledError
            return None
        except Exception as e:
            logger.debug(f"同步连接器 task {name} 退出时抛异常: {e}", exc_info=True)
            return None
        return None

    results = await asyncio.gather(
        *(_wait_one(name, task) for name, task in targets),
        return_exceptions=False,
    )
    pending = [name for name in results if name]

    if pending:
        logger.warning(
            "以下同步连接器 task 未在 %.1fs 内退出: %s",
            wait_timeout,
            ", ".join(pending),
        )
    return pending


# 兼容别名：旧名 join_sync_connector_threads 仍被 app/main_server/__init__.py 引用
# （上帝文件拆包时调用点搬走了，别名留在定义侧），删名字前先改那边
join_sync_connector_threads = join_sync_connector_tasks


def cleanup(*, log: bool = True):
    """Tell all sync connector tasks to stop. log=False suppresses duplicate logs when atexit fires a second time."""
    _signal_sync_connectors_shutdown(log=log)


def _reset_sync_connector_shutdown_events() -> None:
    """Now a no-op: the old version used ThreadEvent.clear() so the thread slot could be reused
    on the next start; in task mode there is no state to reset — a dead task is detected by
    ``_init_character_resources`` and simply restarted via ``asyncio.create_task``. The function
    name is kept to avoid touching the many call sites."""
    return


# 只在主进程中注册 cleanup 函数，防止子进程退出时执行清理
# log=False：on_shutdown 已经打印过 "正在清理资源..."，atexit 补一刀时不重复 log
if _IS_MAIN_PROCESS:
    atexit.register(cleanup, log=False)
# 角色数据全局变量（会在重载时更新）
master_name = None
her_name = None
master_basic_config = None
lanlan_basic_config = None
name_mapping = None
lanlan_prompt = None
time_store = None
setting_store = None
recent_log = None
catgirl_names = []


def _is_websocket_connected(ws) -> bool:
    """Check if a WebSocket is in CONNECTED state."""
    if not ws:
        return False
    if not hasattr(ws, "client_state"):
        return False
    try:
        return ws.client_state == ws.client_state.CONNECTED
    except Exception:
        return False


def _iter_session_managers():
    """Yield (name, session_manager) for every role with a live session_manager.

    Replaces the old ``session_manager.items()`` pattern after the per-k dicts
    were consolidated into ``role_state``.
    """
    for name, rs in role_state.items():
        if rs.session_manager is not None:
            yield name, rs.session_manager


def _get_session_manager(name):
    """Return ``role_state[name].session_manager`` or None — dict.get() equivalent."""
    if not name:
        return None
    rs = role_state.get(name)
    return rs.session_manager if rs is not None else None


def _get_explicit_session_user_language(name):
    """Return the live session locale only when the frontend declared it."""
    manager = _get_session_manager(name)
    if manager is None or not getattr(manager, "_user_language_explicit", False):
        return None
    return getattr(manager, "user_language", None)


def _get_session_render_language(name):
    """Return the current renderer locale without promoting it to a preference."""
    manager = _get_session_manager(name)
    if manager is None:
        return None
    return getattr(manager, "_conversation_render_language", None)


try:
    from main_logic.topic.delivery import register_topic_session_manager_getter

    register_topic_session_manager_getter(_get_session_manager)
except Exception:
    logger.warning("Failed to register topic session manager getter", exc_info=True)

def _select_fallback_session_manager():
    """Return a single connected session manager as a safe fallback, if unambiguous."""
    connected = []
    for name, mgr in _iter_session_managers():
        ws = getattr(mgr, "websocket", None)
        if _is_websocket_connected(ws):
            connected.append((name, mgr))
    if len(connected) == 1:
        return connected[0]
    return None, None


async def _broadcast_to_all_connected(event_payload: dict) -> int:
    """Broadcast an event to all connected WebSocket sessions in parallel.
    Can fire multiple times per second (agent status); serial awaits would let one slow ws drag down the other sessions."""
    # Take a snapshot to avoid RuntimeError from concurrent dict mutation
    targets = [
        (name, getattr(mgr, "websocket", None))
        for name, mgr in list(_iter_session_managers())
        if mgr
    ]
    targets = [
        (n, ws)
        for n, ws in targets
        if _is_websocket_connected(ws) and hasattr(ws, "send_json")
    ]

    async def _send_one(name, ws):
        try:
            await ws.send_json(event_payload)
            return True
        except Exception as e:
            logger.debug("[EventBus] broadcast to %s failed: %s", name, e)
            return False

    results = await asyncio.gather(
        *(_send_one(n, ws) for n, ws in targets), return_exceptions=False
    )
    return sum(1 for r in results if r is True)


async def _handle_agent_event(event: dict):
    """Receive agent_server events over ZeroMQ and dispatch them to core/websocket."""
    try:
        event_type = event.get("event_type")
        lanlan = event.get("lanlan_name")

        if event_type == "analyze_ack":
            logger.info(
                "[EventBus] analyze_ack received on main: event_id=%s lanlan=%s",
                event.get("event_id"),
                lanlan,
            )
            notify_analyze_ack(str(event.get("event_id") or ""))
            return

        if event_type == "voice_bridge_result":
            event_id = str(event.get("event_id") or "")
            logger.debug(
                "[EventBus] ignored voice_bridge_result: event_id=%s", event_id
            )
            return

        # Agent status updates may be broadcast (lanlan_name omitted).
        if event_type == "agent_status_update":
            snapshot = event.get("snapshot", {})
            payload = {
                "type": "agent_status_update",
                "snapshot": snapshot,
                "lanlan_name": lanlan or "",
            }
            mgr_for_status = _get_session_manager(lanlan)
            if isinstance(snapshot, dict):
                flags = snapshot.get("flags")
                if isinstance(flags, dict):
                    flags_for_sync = dict(flags)
                    if isinstance(snapshot.get("analyzer_enabled"), bool):
                        flags_for_sync["agent_enabled"] = bool(
                            snapshot.get("analyzer_enabled")
                        )
                    if lanlan and mgr_for_status is not None:
                        try:
                            mgr_for_status.update_agent_flags(flags_for_sync)
                        except Exception as e:
                            logger.debug(
                                "[EventBus] agent_status_update flag sync failed: %s", e
                            )
                    elif not lanlan:
                        for _, mgr in _iter_session_managers():
                            try:
                                mgr.update_agent_flags(flags_for_sync)
                            except Exception as e:
                                logger.debug(
                                    "[EventBus] agent_status_update broadcast flag sync failed: %s",
                                    e,
                                )
            if lanlan and mgr_for_status is not None:
                mgr = mgr_for_status
                ws = getattr(mgr, "websocket", None) if mgr else None
                if _is_websocket_connected(ws):
                    try:
                        await ws.send_json(payload)
                    except Exception as e:
                        logger.debug(
                            "[EventBus] agent_status_update send failed: %s", e
                        )
            elif not lanlan:
                # Only a target-less update (lanlan_name omitted) fans out to all
                # sessions; a targeted update whose session manager is missing must
                # NOT broadcast, or one character's status leaks into other sessions.
                await _broadcast_to_all_connected(payload)
            else:
                logger.info(
                    "[EventBus] agent_status_update dropped: no session_manager for lanlan=%s",
                    lanlan,
                )
            return

        # 免费版 Agent 每日配额耗尽：全局提示（与角色无关），广播成 status toast
        # 到所有已连接会话。上游 config_manager 已节流（≤每 10 秒一次），这里不会刷屏。
        # 前端已就绪：AGENT_QUOTA_EXCEEDED 在 criticalErrorCodes 里，配 i18n 文案
        # （{{used}}/{{limit}}）走 showStatusToast。
        if event_type == "agent_quota_exceeded":
            import json as _json

            status_message = _json.dumps(
                {
                    "code": "AGENT_QUOTA_EXCEEDED",
                    "details": {
                        "used": event.get("used", 0),
                        "limit": event.get("limit", 300),
                    },
                }
            )
            quota_payload = {"type": "status", "message": status_message}
            mgr_for_quota = _get_session_manager(lanlan)
            if lanlan and mgr_for_quota is not None:
                ws_for_quota = getattr(mgr_for_quota, "websocket", None)
                if _is_websocket_connected(ws_for_quota):
                    try:
                        await ws_for_quota.send_json(quota_payload)
                    except Exception as e:
                        logger.debug(
                            "[EventBus] agent_quota_exceeded send failed: %s", e
                        )
            else:
                await _broadcast_to_all_connected(quota_payload)
            return

        # Resolve target session manager; fallback to broadcast if lanlan is unknown
        mgr = _get_session_manager(lanlan)
        if not mgr and event_type == "task_update":
            # Broadcast task_update to all connected sessions when lanlan is unresolvable
            task_payload = {"type": "agent_task_update", "task": event.get("task", {})}
            delivered = await _broadcast_to_all_connected(task_payload)
            if delivered == 0:
                logger.warning(
                    "[EventBus] task_update broadcast: no connected WebSocket sessions"
                )
            return

        # --- Music Global Broadcasts (Must come before early 'if not mgr' returns) ---
        elif event_type == "music_allowlist_add":
            # Music allowlist is a global UI state, broadcast to all active sessions
            targets = [mgr] if mgr else [m for _, m in _iter_session_managers()]
            payload = {
                "type": "music_allowlist_add",
                "domains": event.get("domains")
                or event.get("metadata", {}).get("domains", []),
                "http_urls": event.get("http_urls")
                or event.get("metadata", {}).get("http_urls", []),
            }

            async def _send_allowlist(target_mgr):
                if (
                    target_mgr
                    and target_mgr.websocket
                    and hasattr(target_mgr.websocket, "send_json")
                ):
                    try:
                        await target_mgr.websocket.send_json(payload)
                    except Exception as e:
                        logger.debug(
                            "[EventBus] music_allowlist_add broadcast failed: %s", e
                        )

            await asyncio.gather(
                *(_send_allowlist(t) for t in targets), return_exceptions=True
            )
            if targets:
                logger.info(
                    "[EventBus] music_allowlist_add broadcasted to %d sessions",
                    len(targets),
                )
            return

        elif event_type == "music_play_url":
            # Music playback is a global UI action, broadcast to all active sessions
            targets = [mgr] if mgr else [m for _, m in _iter_session_managers()]
            payload = {
                "type": "music_play_url",
                "url": event.get("url"),
                "name": event.get("name") or "Plugin Music",
                "artist": event.get("artist") or "External",
            }

            async def _send_play(target_mgr):
                if (
                    target_mgr
                    and target_mgr.websocket
                    and hasattr(target_mgr.websocket, "send_json")
                ):
                    try:
                        await target_mgr.websocket.send_json(payload)
                    except Exception as e:
                        logger.debug(
                            "[EventBus] music_play_url broadcast failed: %s", e
                        )

            await asyncio.gather(
                *(_send_play(t) for t in targets), return_exceptions=True
            )
            if targets:
                logger.info(
                    "[EventBus] music_play_url broadcasted to %d sessions", len(targets)
                )
            return

        elif event_type == "jukebox_control":
            # Jukebox control mutates one local playback runtime. Unlike generic
            # music URL playback, an unscoped command must not fan out to every
            # connected character session.
            if not lanlan or not mgr:
                logger.info(
                    "[EventBus] jukebox_control dropped: no target session for lanlan=%s",
                    lanlan,
                )
                return
            targets = [mgr]
            action = str(event.get("action") or "").strip().lower()
            payload = {
                "type": "jukebox_control",
                "command": {
                    "action": action,
                    "query": event.get("query") or "",
                    "value": event.get("value"),
                    "mode": event.get("mode") or "",
                },
                "source": event.get("source") or "",
            }

            async def _send_jukebox_control(target_mgr):
                if (
                    target_mgr
                    and target_mgr.websocket
                    and hasattr(target_mgr.websocket, "send_json")
                ):
                    try:
                        await target_mgr.websocket.send_json(payload)
                    except Exception as e:
                        logger.debug(
                            "[EventBus] jukebox_control broadcast failed: %s", e
                        )

            await asyncio.gather(
                *(_send_jukebox_control(t) for t in targets), return_exceptions=True
            )
            if targets:
                logger.info(
                    "[EventBus] jukebox_control broadcasted to %d sessions", len(targets)
                )
            return
        if not mgr and event_type in ("proactive_message", "task_result"):
            fallback_name, fallback_mgr = _select_fallback_session_manager()
            if fallback_mgr is not None:
                mgr = fallback_mgr
                logger.warning(
                    "[EventBus] %s rerouted: lanlan=%s missing, fallback_session=%s",
                    event_type,
                    lanlan,
                    fallback_name,
                )
            else:
                # No target session found — drop the event entirely.
                # Do NOT broadcast text to other sessions to prevent cross-session leaks.
                logger.info(
                    "[EventBus] %s dropped: no target session for lanlan=%s, active_sessions=%s",
                    event_type,
                    lanlan,
                    [name for name, _ in _iter_session_managers()],
                )
                return
        if not mgr:
            logger.info(
                "[EventBus] %s dropped: no session_manager for lanlan=%s",
                event_type,
                lanlan,
            )
            return
        if event_type in ("task_result", "proactive_message"):
            raw_text = event.get("text") or ""
            # Why: the chat render must preserve verbatim whitespace (the
            # plugin authored the text, indentation included); only the
            # empty-check / log / callback paths use the stripped form.
            text = raw_text.strip()

            # v2 push_message: media parts (image/audio/video) ride on the
            # same proactive_message event. Image parts are either injected
            # into the active model session or retained on the callback that
            # owns their eventual text/voice delivery boundary.
            #
            # Audio / video aren't supported here — ``stream_audio`` is the
            # live-mic PCM pipeline (specific sample rate + RNNoise gate),
            # not a generic file injector, and we have no video API.
            # ai_behavior=blind suppresses injection entirely.
            ordered_parts = (
                event.get("parts") if isinstance(event.get("parts"), list) else None
            )
            # Sanitized ONCE here, before either consumer: the chat path reads
            # ordered_parts directly and media_parts is derived from it, so a
            # check placed in either alone would leave the other reachable.
            if ordered_parts is not None:
                ordered_parts = _drop_conflicting_image_parts(ordered_parts)
            media_parts = (
                [
                    part
                    for part in ordered_parts
                    if isinstance(part, dict)
                    and part.get("type") in ("image", "audio", "video")
                ]
                if ordered_parts is not None
                else _drop_conflicting_image_parts(
                    event.get("media_parts")
                    if isinstance(event.get("media_parts"), list)
                    else []
                )
            )
            ai_behavior_v2 = event.get("ai_behavior")
            # Images that must travel WITH a proactive (respond) callback so they
            # can be streamed at the moment the pacing manager releases the cue
            # (see LLMSessionManager._deliver_proactive_batch). Streaming them
            # here immediately would land the image in the previous/current turn
            # (or drop it when no session exists yet) while the text is held back
            # by the manager — the eventual proactive response would then lack
            # its matching visual context.
            deferred_callback_images: list[str] = []
            if media_parts and ai_behavior_v2 in ("respond", "read"):
                sess = getattr(mgr, "session", None)
                stream_image = getattr(sess, "stream_image", None) if sess else None
                image_indexes = [
                    index
                    for index, part in enumerate(media_parts)
                    if isinstance(part, dict) and part.get("type") == "image"
                ]
                if len(image_indexes) > _PLUGIN_IMAGE_MAX_COUNT:
                    logger.warning(
                        "[EventBus] plugin push carried %d images; only the first %d reach the model path",
                        len(image_indexes),
                        _PLUGIN_IMAGE_MAX_COUNT,
                    )
                    image_indexes = image_indexes[:_PLUGIN_IMAGE_MAX_COUNT]
                if ai_behavior_v2 == "read" and stream_image is None:
                    # ``read`` is documented as best-effort into the CURRENT
                    # session; with no session these are dropped at the inject
                    # site regardless. Resolving them first would spend network,
                    # a decode and event-handler latency on media guaranteed to
                    # be discarded -- background pushes can repeat that
                    # indefinitely (Codex P2). ``respond`` is unaffected: its
                    # images ride the callback and need no session yet.
                    logger.debug(
                        "[EventBus] %d read image(s) skipped: session=%s has no stream_image",
                        len(image_indexes),
                        type(sess).__name__ if sess else "None",
                    )
                    image_indexes = []
                elif ai_behavior_v2 == "read" and not getattr(
                    sess, "_supports_native_image", True
                ):
                    # ``read`` is NOT supported on a realtime provider without
                    # native vision (standard StepFun is the only one left).
                    #
                    # Such a session answers ``stream_image(cache_latest=False)``
                    # by RETURNING a VISION_MODEL description instead of putting
                    # anything in the conversation -- see _transport.stream_image.
                    # Delivering that description needs a conversation item bound
                    # to a delivery ticket, which is what _stream_cb_media builds
                    # for ``respond`` callbacks. ``read`` is best-effort input to
                    # whatever session happens to exist and owns no such ticket,
                    # so it has nowhere to put the description.
                    #
                    # Bail out HERE rather than at the inject site: reaching that
                    # site means the fetch, the decode and a paid VISION_MODEL
                    # call have already happened for a description that is then
                    # dropped on the floor. Skipping early is honest about the
                    # gap and costs nothing. Passing cache_latest=True instead
                    # would land the image in the ambient frame cache, where an
                    # unrelated prompt_ephemeral can resend it as a screenshot.
                    #
                    # ``respond`` is unaffected -- its images ride the callback
                    # and go through _stream_cb_media, which does handle this.
                    # Text mode is unaffected too: OmniOfflineClient has no
                    # _supports_native_image, so the getattr default keeps it in.
                    logger.warning(
                        "[EventBus] %d read image(s) dropped: session=%s has no native "
                        "vision, and ai_behavior='read' has no ticket-bound channel for "
                        "a VISION_MODEL description; use ai_behavior='respond' to reach "
                        "this provider",
                        len(image_indexes),
                        type(sess).__name__ if sess else "None",
                    )
                    image_indexes = []
                resolved_model_images: dict[int, str | BaseException] = {}
                # Fetch concurrently, but charge the pool in CANONICAL PART
                # ORDER once each batch is in. Drawing from a shared pool
                # inside the concurrent fetches bounded the aggregate but made
                # survival depend on which network read finished first, so a
                # fast later image could starve a slow earlier one and the set
                # reaching the model varied with timing (Codex P2). Ordering is
                # a contract this PR exists to keep; the round-trip saving is
                # not, so the accounting moved out of the fetches.
                #
                # Each transfer is still bounded on its own by the per-image
                # ceiling, so the in-flight worst case is the batch's worth --
                # halved to two to keep that overshoot modest against an 8 MiB
                # aggregate (CodeRabbit).
                _image_budget = _PushImageByteBudget(_PLUGIN_IMAGE_TOTAL_MAX_BYTES)
                for offset in range(0, len(image_indexes), _PLUGIN_IMAGE_FETCH_BATCH_SIZE):
                    if _image_budget.remaining <= 0:
                        logger.warning(
                            "[EventBus] plugin image byte budget (%d) exhausted; %d image(s) not fetched",
                            _PLUGIN_IMAGE_TOTAL_MAX_BYTES,
                            len(image_indexes) - offset,
                        )
                        break
                    batch = image_indexes[offset : offset + _PLUGIN_IMAGE_FETCH_BATCH_SIZE]
                    results = await asyncio.gather(
                        *(_resolve_plugin_model_image(media_parts[i]) for i in batch),
                        return_exceptions=True,
                    )
                    for index, result in zip(batch, results):
                        if isinstance(result, str):
                            # Charged on the RETAINED bytes (post-normalization)
                            # in part order, so the same input always yields the
                            # same surviving set.
                            if not _image_budget.draw(_approx_decoded_bytes(result)):
                                logger.warning(
                                    "[EventBus] plugin image dropped: over the %d byte per-push model budget",
                                    _PLUGIN_IMAGE_TOTAL_MAX_BYTES,
                                )
                                continue
                        # Exceptions are retained so the drop below logs the
                        # underlying resolve failure.
                        resolved_model_images[index] = result

                for index, mp in enumerate(media_parts):
                    if not isinstance(mp, dict):
                        continue
                    part_type = mp.get("type")
                    mime = mp.get("mime") or ""
                    if part_type != "image":
                        # ``audio`` / ``video`` need provider-specific transport
                        # we don't have today; drop with a one-line warning so
                        # plugin authors notice instead of silently losing
                        # frames.
                        logger.warning(
                            "[EventBus] media_part type=%s not yet supported (mime=%s); dropped",
                            part_type,
                            mime,
                        )
                        continue
                    resolved_b64 = resolved_model_images.get(index)
                    if isinstance(resolved_b64, BaseException):
                        logger.warning(
                            "[EventBus] plugin image resolve failed; dropped: %s",
                            resolved_b64,
                        )
                        continue
                    if not isinstance(resolved_b64, str) or not resolved_b64:
                        continue
                    if ai_behavior_v2 == "respond":
                        # Defer: stream when the manager releases this cue so
                        # the image shares the proactive response's context.
                        deferred_callback_images.append(resolved_b64)
                        continue
                    # ``read`` is best-effort input to the current session.
                    # It does not create a cross-session media inbox.
                    if stream_image is None:
                        logger.debug(
                            "[EventBus] read image dropped: session=%s has no stream_image",
                            type(sess).__name__ if sess else "None",
                        )
                        continue
                    try:
                        await stream_image(
                            resolved_b64,
                            bypass_rate_limit=True,
                            # Plugin-owned input, not an ambient frame. The
                            # realtime transport otherwise stores it as
                            # _latest_image_b64 and marks it unconsumed, so an
                            # unrelated prompt_ephemeral could resend it later
                            # as a screenshot -- after it was already inserted
                            # into the conversation (Codex P2).
                            cache_latest=False,
                            # Charged to the plugin quota, never the user's.
                            source="plugin",
                        )
                        logger.debug(
                            "[EventBus] image media_part injected (base64 len=%d, mime=%s)",
                            len(resolved_b64),
                            mime,
                        )
                    except Exception as e:
                        logger.warning(
                            "[EventBus] read image stream_image failed; dropped: %s",
                            e,
                        )

            # ONE chat-render path for every plugin push, regardless of
            # ai_behavior. Plugin content is rendered as a SYSTEM message: it
            # is neither the assistant speaking nor the user, and presenting it
            # as either is a lie the reader cannot detect.
            #
            # Before this, `blind` rendered through passthrough_to_chat_bubble
            # and therefore wore the assistant's avatar and name, while
            # `read`/`respond` rendered image-bearing pushes through
            # render_chat_blocks -- also as the assistant. The second was worse
            # than cosmetic: those images DO reach the model, and they reach it
            # on a user-role message, so the same content appeared to the
            # reader as the assistant's and to the model as the user's.
            #
            # `blind` content is not in the model's context at all, so an
            # assistant-looking bubble also created something the assistant has
            # no memory of saying. A source-labelled system bubble keeps the
            # plugin's own wording -- a plugin may still write in the
            # character's voice -- while making its origin visible.
            visibility = event.get("visibility")
            if (
                isinstance(visibility, list)
                and "chat" in visibility
                and hasattr(mgr, "render_chat_blocks")
            ):
                if ordered_parts is not None:
                    visible_blocks = _ordered_plugin_chat_blocks(ordered_parts, mgr)
                else:
                    visible_images = _build_plugin_image_chat_blocks(media_parts)
                    visible_blocks = []
                    if text:
                        visible_text = core.apply_role_placeholders(
                            raw_text,
                            lanlan_name=getattr(mgr, "lanlan_name", "") or "",
                            master_name=getattr(mgr, "master_name", "") or "",
                        )
                        visible_blocks.append({"type": "text", "text": visible_text})
                    visible_blocks.extend(visible_images)
                if visible_blocks:
                    visible_source, visible_source_name = _resolve_event_source(event)
                    # Display must not be able to cancel delivery. This runs
                    # BEFORE the callback is built, so an exception here would
                    # skip the model path entirely and lose both the deferred
                    # images and the text (CodeRabbit).
                    try:
                        await mgr.render_chat_blocks(
                            visible_blocks,
                            request_id=event.get("task_id") or None,
                            source=visible_source,
                            source_name=visible_source_name or None,
                        )
                    except Exception as e:
                        logger.warning(
                            "[EventBus] render_chat_blocks failed; continuing delivery: %s",
                            e,
                        )

            if text or deferred_callback_images:
                if event.get("direct_reply"):
                    detail_text = (event.get("detail") or text).strip()
                    # Plugin-supplied direct_reply text bypasses the LLM and
                    # speaks/types verbatim. Plugin authors may write
                    # ``{MASTER_NAME}``/``{LANLAN_NAME}`` placeholders since
                    # they don't know which session their text will route to;
                    # expand here so the placeholder doesn't reach TTS/UI
                    # literally. (See main_logic.core.apply_role_placeholders
                    # for the contract — same helper as the LLM-injection path
                    # so all plugin-text exits share one spelling.)
                    detail_text = core.apply_role_placeholders(
                        detail_text,
                        lanlan_name=getattr(mgr, "lanlan_name", "") or "",
                        master_name=getattr(mgr, "master_name", "") or "",
                    )
                    delivered = False
                    if detail_text and hasattr(mgr, "send_lanlan_response"):
                        try:
                            delivered = bool(
                                await mgr.send_lanlan_response(detail_text, True)
                            )
                        except Exception as e:
                            logger.warning(
                                "[EventBus] direct task_result reply failed: %s", e
                            )
                    if delivered and hasattr(mgr, "handle_proactive_complete"):
                        try:
                            await mgr.handle_proactive_complete()
                        except Exception as e:
                            logger.warning(
                                "[EventBus] direct task_result turn_end failed: %s", e
                            )
                    if delivered:
                        # detail_text 是面向用户的回复内容，不写 logger
                        logger.info(
                            "[EventBus] direct task_result reply delivered (detail_len=%d)",
                            len(detail_text),
                        )
                        return

                # Build structured callback and enqueue for LLM injection
                cb_status = event.get("status") or (
                    "completed" if event.get("success", True) else "failed"
                )
                # delivery_mode controls how the callback reaches the LLM:
                #   proactive (default): enqueue + immediately schedule trigger_agent_callbacks
                #   passive            : enqueue only (next user turn will drain)
                #   silent             : skip LLM channel entirely (frontend HUD still fires)
                delivery_mode = (event.get("delivery_mode") or "proactive").strip()
                if delivery_mode not in ("proactive", "passive", "silent"):
                    delivery_mode = "proactive"
                # Defensive: blind ai_behavior must NEVER reach the LLM channel,
                # even if delivery_mode arrives as "proactive" / "passive". The
                # plugin proactive_bridge already maps blind→silent, but this
                # is an indirect contract — a future direct emitter (or a bug
                # in another bridge) could violate it. Forcing silent here
                # locks the (blind ⇒ no LLM enqueue) invariant on the host
                # side regardless of caller-supplied delivery_mode.
                if (event.get("ai_behavior") or "").strip() == "blind":
                    delivery_mode = "silent"
                # Default source_kind from channel when caller didn't specify one.
                # Plugin emit sites already pass explicit source_kind/source_name.
                _channel = event.get("channel") or "unknown"
                source_kind, source_name = _resolve_event_source(event)
                event_metadata = (
                    event.get("metadata")
                    if isinstance(event.get("metadata"), dict)
                    else {}
                )
                # origin is host-derived from event_type plus the validated
                # user-plugin result contract:
                #   "task_result"      → real task completion (agent_server._emit_task_result):
                #                        Computer Use / Browser Use / plugin entry / MCP tool result
                #   "proactive_message" → plugin push_message stream (proactive_bridge):
                #                        danmaku / gift / external notification
                # A successful user_plugin task_result may explicitly downgrade
                # to result_kind="event" for receipts/read-only query results.
                # No other channel or event type can use that field to forge a
                # task result. _build_callback_instruction uses the resolved
                # origin to pick task "汇报" vs neutral event "回应" wording.
                origin = _resolve_callback_origin(event_type, event, _channel)
                # Absolute wall-clock values cross processes badly. Stamp an
                # internal deadline on the receiving host instead.
                expires_at_monotonic = _resolve_callback_expiry(event, origin)
                # Proactive-delivery hints from push_message (priority +
                # coalesce_key). HIGHER number = more important; a missing or
                # unparseable priority falls back to 0 = least important. The
                # manager does not rescale it (effective_priority just int()s
                # the value and sorts by (-priority, seq)).
                try:
                    # OverflowError: JSON Infinity/-Infinity → float → int() raises;
                    # must not let a malformed priority drop the whole callback.
                    cb_priority = int(event.get("priority", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    cb_priority = 0
                cb_coalesce_key = event.get("coalesce_key")
                if not isinstance(cb_coalesce_key, str):
                    cb_coalesce_key = ""
                callback = {
                    "event": "agent_task_callback",
                    "origin": origin,
                    "task_id": event.get("task_id") or "",
                    "channel": _channel,
                    "status": cb_status,
                    "success": bool(event.get("success", True)),
                    "summary": event.get("summary") or text,
                    "detail": event.get("detail") or text,
                    "error_message": event.get("error_message") or "",
                    "source_kind": source_kind,
                    "source_name": source_name,
                    "delivery_mode": delivery_mode,
                    "priority": cb_priority,
                    "coalesce_key": cb_coalesce_key,
                    # Respond images stream at manager release. Read images are
                    # best-effort input to the current session and are not queued.
                    "media_images": deferred_callback_images,
                    "timestamp": event.get("timestamp") or "",
                    "metadata": event_metadata,
                    "context_type": event_metadata.get("context_type") or "",
                    CALLBACK_EXPIRES_AT_KEY: expires_at_monotonic,
                }
                if delivery_mode != "silent":
                    if delivery_mode == "passive":
                        # Passive cues keep the direct enqueue-only path:
                        # they must NOT interrupt; the next user turn drains
                        # them. The pacing manager only governs proactive, but
                        # enqueue_agent_callback still honors coalesce_key so a
                        # passive stream can dedup queued snapshots by key.
                        mgr.enqueue_agent_callback(callback)
                        logger.info(
                            "[EventBus] %s enqueued callback (passive); next user turn will carry it",
                            event_type,
                        )
                    else:
                        # Proactive: hand to the delivery manager, which
                        # orders by priority, coalesces by key, and paces
                        # release on the frontend playback gate + min-gap.
                        logger.info(
                            "[EventBus] %s submitting proactive callback to delivery manager (priority=%s key=%r)",
                            event_type,
                            cb_priority,
                            cb_coalesce_key or "(source)",
                        )
                        mgr.submit_proactive_callback(
                            callback,
                            priority=cb_priority,
                            coalesce_key=cb_coalesce_key or None,
                        )
                else:
                    logger.info(
                        "[EventBus] %s delivery=silent: skipping LLM channel (frontend HUD still fires)",
                        event_type,
                    )

                # Visibility, read once for the HUD gate below. Chat
                # rendering of the plugin's own parts already happened
                # further up, for every ai_behavior; what is left here is the
                # orthogonal question of whether a HUD toast also fires, so
                # visibility=["chat","hud"] lights both sinks and
                # visibility=["chat"] only the chat one.
                #
                # ai_behavior is deliberately NOT read here. It used to be:
                # a local `_ai_behavior` fed a chat branch gated on
                # visibility=="chat" AND ai_behavior=="blind", which #2835
                # removed when chat rendering moved above and stopped caring
                # about ai_behavior. The variable outlived that branch as a
                # dead assignment and is now gone. If you find yourself adding
                # it back, check first whether the thing you want actually
                # belongs in the chat-render block above -- the HUD gate is
                # visibility-only by design, and reintroducing an ai_behavior
                # read here would silently re-couple two axes the v2 schema
                # defines as orthogonal.
                _vis_raw = event.get("visibility")
                _vis_present = isinstance(_vis_raw, list)
                _vis = _vis_raw if _vis_present else []
                # Plugin chat output is rendered above, as a system message,
                # for every ai_behavior. It used to go through
                # passthrough_to_chat_bubble here, which wore the assistant's
                # identity AND opened an assistant turn -- hence the turn-end
                # that used to follow. A system bubble opens no turn, so there
                # is none to close.

                # v2 visibility contract: HUD agent_notification fires only
                # when "hud" is in visibility. Why: visibility=["chat"] must
                # not double-render as both chat bubble AND HUD toast.
                # Legacy emitters that omit the visibility field entirely
                # (no v2 plumbing) keep the pre-v2 behavior of firing HUD
                # by default — checked via _vis_present, not via _vis truthiness,
                # so an explicit visibility=[] (v2 "no verbatim render") suppresses HUD.
                _hud_allowed = ("hud" in _vis) if _vis_present else True
                ws = getattr(mgr, "websocket", None)
                if not _hud_allowed:
                    logger.info(
                        "[EventBus] agent_notification suppressed by visibility=%s (no 'hud') for lanlan=%s",
                        _vis,
                        lanlan,
                    )
                elif not text:
                    # The HUD toast renders TEXT and nothing else -- it has no
                    # image sink. The gate above this block used to be
                    # ``if text:``; widening it to ``text or
                    # deferred_callback_images`` was for the LLM delivery
                    # channel, which genuinely treats images as payload. This
                    # branch shares that gate but not that property, so an
                    # image-only push started arriving here and emitting an
                    # agent_notification whose text is "".
                    #
                    # Restoring the precondition locally rather than narrowing
                    # the shared gate: the callback path must keep accepting
                    # image-only pushes. The image is not lost either way --
                    # it rides the proactive callback built above.
                    logger.debug(
                        "[EventBus] agent_notification skipped: push carried images but no text",
                    )
                elif _is_websocket_connected(ws):
                    try:
                        # HUD agent_notification renders verbatim to the user;
                        # expand role placeholders so plugin authors can write
                        # ``"通知 {MASTER_NAME}..."`` without the literal token
                        # showing up in the toast.
                        notif_text = core.apply_role_placeholders(
                            text,
                            lanlan_name=getattr(mgr, "lanlan_name", "") or "",
                            master_name=getattr(mgr, "master_name", "") or "",
                        )
                        notif = {
                            "type": "agent_notification",
                            "text": notif_text,
                            "source": "brain",
                            "status": cb_status,
                        }
                        err_msg = event.get("error_message") or ""
                        if err_msg:
                            notif["error_message"] = err_msg[
                                :USER_NOTIFICATION_ERROR_MAX_CHARS
                            ]
                        await ws.send_json(notif)
                        # text 是面向前端的通知正文，不写 logger
                        logger.info(
                            "[EventBus] agent_notification sent to frontend (text_len=%d)",
                            len(text),
                        )
                    except Exception as e:
                        logger.warning(
                            "[EventBus] agent_notification WS send failed: %s", e
                        )
                else:
                    logger.warning(
                        "[EventBus] agent_notification: WebSocket not connected for lanlan=%s",
                        lanlan,
                    )
        elif event_type == "agent_notification":
            ws = getattr(mgr, "websocket", None)
            if _is_websocket_connected(ws):
                try:
                    notif = {
                        "type": "agent_notification",
                        "text": event.get("text", ""),
                        "source": event.get("source", "brain"),
                        "status": event.get("status", "error"),
                    }
                    err_msg = event.get("error_message") or ""
                    if err_msg:
                        notif["error_message"] = err_msg[
                            :USER_NOTIFICATION_ERROR_MAX_CHARS
                        ]
                    await ws.send_json(notif)
                except Exception as e:
                    logger.debug("[EventBus] agent_notification send failed: %s", e)
            else:
                logger.debug(
                    "[EventBus] agent_notification: WebSocket not connected for lanlan=%s",
                    lanlan,
                )
        elif event_type == "task_update":
            task_payload = {"type": "agent_task_update", "task": event.get("task", {})}
            ws = getattr(mgr, "websocket", None)
            if _is_websocket_connected(ws):
                try:
                    await ws.send_json(task_payload)
                except Exception as e:
                    logger.warning(
                        "[EventBus] task_update send failed for lanlan=%s: %s",
                        lanlan,
                        e,
                    )
            else:
                logger.warning(
                    "[EventBus] task_update dropped: WebSocket not connected for lanlan=%s",
                    lanlan,
                )
    except Exception as exc:
        # 这个兜底 except 包住整个 agent event 分发，而 event payload 里带用户对话
        # 文本——异常消息很可能把它捎进来。所以 logger 只写异常类型；完整 traceback
        # 走 print（同 proactive 原文的处理方式），且只在 DEBUG 级下输出。
        #
        # 不能用 logger.debug(exc_info=True)：源码运行且 log_level<=DEBUG 时
        # setup_logging 会挂一个只收 DEBUG 的 RotatingFileHandler 落到 logs/
        # （utils/logger_config.py），那等于把隐私文本持久化了。仓库规则见
        # .agent/rules/neko-guide.md 与 docs/contributing/code-style.md：
        # 涉及用户隐私（原始对话）的 log 只能用 print，不得使用 logger。
        logger.warning(
            "[EventBus] handle_agent_event failed (error_type=%s)",
            type(exc).__name__,
        )
        if logger.isEnabledFor(logging.DEBUG):
            traceback.print_exc()


async def _refresh_character_globals():
    """Refresh character-related module globals (re-fetch aget_character_data from config).

    Every fast-path entry must go through this first, so that after operations like
    set_current_catgirl / update_catgirl, subsequent reads of her_name / lanlan_prompt /
    lanlan_basic_config see the latest values.
    """
    global master_name, her_name, master_basic_config, lanlan_basic_config
    global name_mapping, lanlan_prompt, time_store, setting_store, recent_log
    global catgirl_names
    (
        master_name,
        her_name,
        master_basic_config,
        lanlan_basic_config,
        name_mapping,
        lanlan_prompt,
        time_store,
        setting_store,
        recent_log,
    ) = await _config_manager.aget_character_data()
    catgirl_names = list(lanlan_prompt.keys())
    facade = sys.modules[__package__]
    facade.master_name = master_name
    facade.her_name = her_name
    facade.master_basic_config = master_basic_config
    facade.lanlan_basic_config = lanlan_basic_config
    facade.name_mapping = name_mapping
    facade.lanlan_prompt = lanlan_prompt
    facade.time_store = time_store
    facade.setting_store = setting_store
    facade.recent_log = recent_log
    facade.catgirl_names = catgirl_names


def _ensure_character_slots(k: str) -> bool:
    """Prepare the per-k sync resource slot for a single catgirl. Returns whether this is a newly created character (which decides whether to force-start the task afterwards).

    A purely in-memory atomic operation: either role_state[k] already exists (do
    nothing), or both the queue and websocket_lock are filled in at once. This avoids
    the half-initialization risk of the old code, where 6 dicts used two different
    sentinels (sync_message_queue vs websocket_locks) to independently decide
    "does this character already have a slot".

    Note: ``asyncio.Queue`` does not need a running loop at creation time on
    Python 3.10+; although this function is sync, its call chain comes from async
    contexts like ``initialize_character_data`` / ``_init_character_resources``,
    so a loop is available.
    """
    if k not in role_state:
        role_state[k] = RoleState(
            sync_message_queue=_SyncMessageQueue(),
            websocket_lock=asyncio.Lock(),
        )
        logger.info(f"为角色 {k} 初始化新资源")
        return True
    return False


async def _init_character_resources(k: str, is_new_character: bool):
    """Complete the session_manager update + sync connector task check/restart for a single catgirl.

    Depends on module globals: master_name, lanlan_prompt, lanlan_basic_config (the caller must refresh them first).
    Writes the per-k slots: role_state[k].session_manager / sync_task — no state is
    shared between different k, so this is safe to run in parallel.
    """
    rs = role_state.get(k)
    if rs is None:
        logger.info(f"{k} 的角色资源已被并发删除，跳过初始化")
        return
    # 更新或创建session manager（使用最新的prompt）
    # 使用锁保护websocket的preserve/restore操作，防止与cleanup()竞争
    async with rs.websocket_lock:
        if role_state.get(k) is not rs:
            logger.info(f"{k} 的角色资源已被并发删除，跳过初始化")
            return
        # 如果已存在且已有websocket连接，保留websocket引用
        old_websocket = None
        if rs.session_manager is not None and rs.session_manager.websocket:
            old_websocket = rs.session_manager.websocket
            logger.info(f"保留 {k} 的现有WebSocket连接")

        # 注意：不在这里清理旧session，因为：
        # 1. 切换当前角色音色时，已在API层面关闭了session
        # 2. 切换其他角色音色时，已跳过重新加载
        # 3. 其他场景不应该影响正在使用的session
        # 如果旧session_manager有活跃session，保留它，只更新配置相关的字段

        # 先检查会话状态（在锁内检查避免竞态条件）
        # 同时覆盖 "正在启动" 窗口：_starting_session_count>0 但 is_active=False
        # 的期间，start_session 协程仍持有对当前 manager 的引用；如果此时替换
        # 实例，旧 manager 会在后台完成启动并挂起 OmniRealtimeClient / TTS 线程 /
        # message_handler_task，永远没人调用 end_session — 造成资源泄漏。
        mgr = rs.session_manager
        has_active_session = mgr is not None and mgr.is_active
        has_starting_session = mgr is not None and mgr.is_starting and not mgr.is_active
        desired_bound_manager = None

        if has_active_session:
            # 有活跃session，不重新创建session_manager，只更新配置
            # 这是为了防止重新创建session_manager时破坏正在运行的session
            try:
                old_mgr = rs.session_manager
                # 更新prompt
                old_mgr.lanlan_prompt = (
                    lanlan_prompt[k]
                    .replace("{LANLAN_NAME}", k)
                    .replace("{MASTER_NAME}", master_name)
                )
                # 直接读 module global lanlan_basic_config，避免重复 load + deepcopy。
                # 经 read_legacy_voice_id 容忍 voice 的扁平串 / 结构对象两形态（惰性迁移）。
                from utils.voice_config import read_legacy_voice_id

                old_mgr.voice_id = read_legacy_voice_id(
                    get_reserved(
                        lanlan_basic_config[k],
                        "voice_id",
                        default="",
                        legacy_keys=("voice_id",),
                    )
                )
                logger.info(f"{k} 有活跃session，只更新配置，不重新创建session_manager")
            except Exception as e:
                logger.error(f"更新 {k} 的活跃session配置失败: {e}", exc_info=True)
                # 配置更新失败，但为了不影响正在运行的session，继续使用旧配置
                # 如果确实需要更新配置，可以考虑在下次session重启时再应用
        elif has_starting_session:
            # start_session 正在执行中：只保留实例避免孤儿泄漏，但绝对不热改
            # lanlan_prompt / voice_id — start_session 会在 core.py 内用
            # self.lanlan_prompt 拼装首帧 session prompt，并基于当前 self.voice_id
            # 计算音色/TTS 分支。本轮写入会让正在进行的启动拿到半旧半新配置
            # （用户侧看到启动出来的会话 prompt / 音色与最新配置不一致）。
            # 本轮的新 prompt / 音色由下一次 start_session 应用。
            logger.info(
                f"{k} session 正在启动中（is_starting），保留现有 session_manager，"
                "本轮不热更新 prompt/voice_id 以免污染 in-flight 启动"
            )
        else:
            # 没有活跃session，可以安全地重新创建session_manager
            # 旧 manager 持有的后台任务（如 idle session reset loop）必须显式
            # cancel，否则强引用 self 让旧 manager 永远不被 GC——多次 reload 后
            # 积累 N 份的 idle loop 各自 60s 醒一次。
            old_user_language = None
            old_user_language_explicit = False
            if rs.session_manager is not None:
                from .voice_identity_runtime import unregister_voice_identity_manager

                old_manager = rs.session_manager
                old_user_language = getattr(old_manager, "user_language", None)
                old_user_language_explicit = getattr(
                    old_manager,
                    "_user_language_explicit",
                    False,
                )
                await unregister_voice_identity_manager(old_manager)
                await _terminal_close_session_manager(old_manager, character_name=k)
            new_mgr = core.LLMSessionManager(
                rs.sync_message_queue,
                k,
                lanlan_prompt[k]
                .replace("{LANLAN_NAME}", k)
                .replace("{MASTER_NAME}", master_name),
            )

            # 将websocket锁存储到session manager中，供cleanup()使用
            new_mgr.websocket_lock = rs.websocket_lock
            if old_user_language_explicit:
                new_mgr.user_language = old_user_language
                new_mgr._user_language_explicit = True

            # 恢复websocket引用（如果存在）
            if old_websocket:
                new_mgr.websocket = old_websocket
                logger.info(f"已恢复 {k} 的WebSocket连接")

            # Bind the lazy desired voice configuration before any websocket
            # handler can obtain this manager. Idle registration creates no
            # model, but is still a required publication precondition.
            from .voice_identity_runtime import (
                register_voice_identity_manager,
                unregister_voice_identity_manager,
            )

            try:
                await register_voice_identity_manager(new_mgr)
            except BaseException:
                try:
                    await unregister_voice_identity_manager(new_mgr)
                finally:
                    await _terminal_close_session_manager(new_mgr, character_name=k)
                raise
            rs.session_manager = new_mgr
            desired_bound_manager = new_mgr

        from .voice_identity_runtime import register_voice_identity_manager

        if rs.session_manager is not None and rs.session_manager is not desired_bound_manager:
            await register_voice_identity_manager(rs.session_manager)

    # 检查并启动同步连接器 task
    # 如果是新角色，或者 task 不存在/已结束，需要启动
    need_start_task = False
    if is_new_character:
        need_start_task = True
    elif rs.sync_task is None or rs.sync_task.done():
        need_start_task = True

    if need_start_task:
        try:
            _char_name = k

            def _make_status_cb(char_name):
                def _cb(msg):
                    mgr = _get_session_manager(char_name)
                    if not mgr:
                        return
                    ws = mgr.websocket
                    if (
                        ws
                        and hasattr(ws, "client_state")
                        and ws.client_state == ws.client_state.CONNECTED
                    ):
                        import json as _json

                        data = _json.dumps({"type": "status", "message": msg})

                        # cross_server 现在和我们在同一个主 loop 上，回调
                        # 也是从主 loop 同步调用的——直接 create_task 即可，
                        # 不再需要 run_coroutine_threadsafe。
                        # done_callback 消化 task 的 exception，避免 ws 断开时
                        # asyncio 输出 "Task exception was never retrieved" 噪音；
                        # status 是 best-effort 降级路径，丢一条不影响主逻辑。
                        # cancelled 态下 task.exception() 自身会 raise CancelledError，
                        # 必须先用 task.cancelled() 早返回，否则 callback 自己又制造
                        # 一条 "exception was never retrieved" 噪音。
                        def _swallow_status_send_exc(_t):
                            if _t.cancelled():
                                return
                            exc = _t.exception()
                            if exc is not None:
                                logger.debug(
                                    "status 回调 ws.send_text 失败（已忽略）: %s", exc
                                )

                        try:
                            _t = asyncio.create_task(ws.send_text(data))
                            _t.add_done_callback(_swallow_status_send_exc)
                        except RuntimeError:
                            # 极端情况：当前没有 running loop（理论上不会发生
                            # 在 cross_server 调用路径上，但兜底）。回退到旧
                            # 跨 loop 路径。
                            loop = runtime.server_loop
                            if loop is not None and not loop.is_closed():
                                asyncio.run_coroutine_threadsafe(
                                    ws.send_text(data), loop
                                )

                return _cb

            _status_cb = _make_status_cb(_char_name)

            new_task = asyncio.create_task(
                cross_server.run_sync_connector(
                    rs.sync_message_queue,
                    k,
                    f"ws://127.0.0.1:{MONITOR_SERVER_PORT}",
                    {"bullet": False, "monitor": True},
                    _status_cb,
                    user_language_provider=(
                        lambda _name=k: _get_explicit_session_user_language(_name)
                    ),
                    render_language_provider=(
                        lambda _name=k: _get_session_render_language(_name)
                    ),
                ),
                name=f"SyncConnector-{k}",
            )
            rs.sync_task = new_task
            logger.info(f"✅ 已为角色 {k} 启动同步连接器 task ({new_task.get_name()})")
        except Exception as e:
            logger.error(f"❌ 启动角色 {k} 的同步连接器 task 失败: {e}", exc_info=True)


async def _stop_character_thread(k: str):
    """Stop a single catgirl's sync connector task (waiting up to 3s for cleanup). Dict cleanup is left to the caller to do in order.

    The ``_thread`` suffix in the name is kept to avoid touching the many call sites; the underlying mechanism is now an ``asyncio.Task``.
    """
    rs = role_state.get(k)
    if rs is None or rs.sync_task is None:
        return
    task = rs.sync_task
    try:
        logger.info(f"正在停止角色 {k} 的同步连接器 task...")
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ 同步连接器 task {k} 未能在 3s 内退出，放任其自行结束")
        except asyncio.CancelledError:
            # cancel 后 await 抛 CancelledError 是正常路径
            pass
        except Exception as e:
            logger.debug(f"同步连接器 task {k} 退出时异常: {e}", exc_info=True)
        else:
            logger.info(f"✅ 已停止角色 {k} 的同步连接器 task")
    except Exception as e:
        logger.warning(f"停止角色 {k} 的同步连接器 task 时出错: {e}")


def _cleanup_character_dicts(k: str):
    """Synchronously clean up a single catgirl's per-k slot. Make sure the corresponding task has stopped or timed out before calling."""
    rs = role_state.get(k)
    if rs is None:
        return
    # 清理队列（asyncio.Queue 也没有 close/join_thread 方法，drain 即可）
    try:
        while not rs.sync_message_queue.empty():
            rs.sync_message_queue.get_nowait()
    except asyncio.QueueEmpty:
        # while empty + get_nowait 本身是 racy idiom：另一线程可能先 drain 掉，
        # 导致 get_nowait 抛 Empty。这里 role_state[k] 即将被 del 掉，忽略无害。
        pass
    # 一次 del 原子清掉所有 6 个字段 —— 替代旧代码里 6 张 dict 分别 del 的对称清理
    del role_state[k]


async def _unregister_character_voice_identity_manager(k: str) -> None:
    rs = role_state.get(k)
    if rs is None:
        return
    async with rs.websocket_lock:
        if role_state.get(k) is not rs:
            return
        await _unregister_character_voice_identity_manager_locked(rs)


async def _unregister_character_voice_identity_manager_locked(rs) -> None:
    if rs.session_manager is None:
        return
    from .voice_identity_runtime import unregister_voice_identity_manager

    await unregister_voice_identity_manager(rs.session_manager)


async def _terminal_close_session_manager(
    manager,
    *,
    character_name: str,
) -> None:
    """Best-effort terminal close for one manager ownership boundary.

    Normal session teardown deliberately leaves the reusable ASR admission
    ingress alive.  Replacing or deleting the manager is the point where that
    ingress must be permanently closed before the last reference is dropped.
    Older/fake managers without the async hook keep the previous synchronous
    shutdown fallback; production ``LLMSessionManager`` instances use
    ``aclose()``.
    """

    try:
        close = getattr(manager, "aclose", None)
        if callable(close):
            await close()
            return
        shutdown = getattr(manager, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception as exc:
        # Do not replace/delete the slot after a failed terminal close: the
        # old manager still owns whatever did not settle.  The caller keeps
        # the slot reachable and can report/retry instead of orphaning it.
        logger.warning(
            "terminal close session_manager 失败 (%s): %s",
            character_name,
            exc,
            exc_info=True,
        )
        raise


async def _unregister_and_cleanup_character_slot(k: str) -> None:
    rs = role_state.get(k)
    if rs is None:
        return
    async with rs.websocket_lock:
        if role_state.get(k) is not rs:
            return
        await _stop_character_thread(k)
        await _unregister_character_voice_identity_manager_locked(rs)
        if rs.session_manager is not None:
            await _terminal_close_session_manager(
                rs.session_manager,
                character_name=k,
            )
        _cleanup_character_dicts(k)


async def initialize_character_data():
    """Full refresh: load config + run per-k init for every catgirl + clean up deleted ones.

    Cold path (startup / master-name edit / large bulk import). For per-catgirl edits
    use the fast paths: init_one_catgirl / remove_one_catgirl / switch_current_catgirl_fast.
    """
    logger.info("正在加载角色配置...")

    # 清理无效的voice_id引用；如果发现旧版 CosyVoice 音色，推入通知缓冲池等前端连接后弹出
    # cleanup_invalid_voice_ids 内部涉及同步 IO（load/save characters），offload 以免阻塞事件循环
    _cleaned, _legacy_names = await asyncio.to_thread(
        _config_manager.cleanup_invalid_voice_ids
    )
    if _legacy_names:
        core.enqueue_voice_migration_notice(_legacy_names)

    # 加载最新的角色数据（offload，避免同步 IO + deepcopy 阻塞事件循环）
    await _refresh_character_globals()

    # 为所有 catgirl 预备 per-k 同步资源槽位
    is_new_map: dict[str, bool] = {k: _ensure_character_slots(k) for k in catgirl_names}

    # 每个角色的初始化相互独立（只读共享 prompt / master_name，写各自的 session_manager[k] 等 per-key 槽位）。
    # 用 gather 并行，消除 O(N) × (thread roundtrip + 0.1s sleep) 的串行墙钟。
    # return_exceptions=True：某个角色初始化失败不应导致其它角色被取消。
    _init_results = await asyncio.gather(
        *[_init_character_resources(k, is_new_map[k]) for k in catgirl_names],
        return_exceptions=True,
    )
    for k, res in zip(catgirl_names, _init_results):
        if isinstance(res, BaseException):
            logger.error(f"❌ 初始化角色 {k} 失败: {res}", exc_info=res)

    # 清理已删除角色的资源
    removed_names = [k for k in role_state.keys() if k not in catgirl_names]

    for k in removed_names:
        logger.info(f"清理已删除角色 {k} 的资源")

    # 每个角色在同一 websocket_lock 事务内停止 connector、注销 manager 并删除 slot。
    # N 个 stop(timeout=3) 并行执行，最坏墙钟仍约为 3 秒。
    if removed_names:
        cleanup_results = await asyncio.gather(
            *[_unregister_and_cleanup_character_slot(k) for k in removed_names],
            return_exceptions=True,
        )
        for k, result in zip(removed_names, cleanup_results):
            if isinstance(result, BaseException):
                logger.error(f"❌ 清理已删除角色 {k} 失败: {result}", exc_info=result)

    logger.info(f"角色配置加载完成，当前角色: {catgirl_names}，主人: {master_name}")


# ─────────────────────────────────────────────────────────────
# Fast-path helpers — 只处理受影响的单个 catgirl，避免全量遍历
# ─────────────────────────────────────────────────────────────


async def switch_current_catgirl_fast():
    """Dedicated fast path for switching the current catgirl (change of the `current catgirl` field).

    Key premise: the switch only affects the single global `her_name`; per-k prompt /
    voice_id / thread state is completely unchanged. So this **only refreshes
    globals** and does no per-k work at all.

    Wall clock: one aget_character_data (~a few ms) and that's everything.
    """
    await _refresh_character_globals()
    logger.info(f"[fast-switch] 已刷新 globals，当前猫娘: {her_name}")


async def init_one_catgirl(name: str, *, is_new: bool = False):
    """Fast path for adding / editing a single catgirl.

    - is_new=True: addition; force-starts the sync connector thread
    - is_new=False: edit (prompt / voice_id etc.) — only refreshes the session_manager's
                    prompt/voice_id, does not restart the thread
    """
    await _refresh_character_globals()
    if name not in lanlan_prompt:
        logger.warning(f"[init-one] '{name}' 不在 config 中，跳过（可能是并发删除）")
        return
    slot_new = _ensure_character_slots(name)
    await _init_character_resources(name, is_new_character=is_new or slot_new)


async def remove_one_catgirl(name: str):
    """Fast path for deleting a single catgirl: stop the character's thread + clear dicts + refresh globals."""
    await _unregister_and_cleanup_character_slot(name)
    # config 文件已由调用方写入，这里刷新 globals 让 catgirl_names 等反映删除
    await _refresh_character_globals()
    logger.info(f"[fast-remove] 已移除角色 {name}")
