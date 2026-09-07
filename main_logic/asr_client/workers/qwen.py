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

"""Qwen-ASR Realtime worker for the China and international regions."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, TypeAlias

import websockets
from websockets.exceptions import ConnectionClosed

from .._infra import AsrSessionConfig, _AsrWorkerEvent, _AsrWorkerRequest
from .._provider_events import ProviderAudioRange
from ._shared import is_auth_rejection

_QWEN_MODEL = "qwen3-asr-flash-realtime"
_QWEN_CN_URL = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={_QWEN_MODEL}"
_QWEN_INTL_URL = (
    f"wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model={_QWEN_MODEL}"
)
_QWEN_FINISH_TIMEOUT_SECONDS = 3.0
# Server VAD publishes speech_stopped/committed as the logical endpoint of a
# turn, but the transcription completed event may be delayed or never arrive.
# An item that outlives this deadline after its endpoint is completed with an
# empty final so the upstream utterance lifecycle converges instead of waiting
# unboundedly. Mirrors the OpenAI worker's stalled-item deadline.
_QWEN_STALLED_ITEM_TIMEOUT_SECONDS = 30.0
_QWEN_ITEM_TOMBSTONE_LIMIT = 128
_QWEN_PROVISIONAL_COMMIT_LIMIT = 8
_QWEN_SYNTHETIC_ITEM_PREFIX = "__qwen_internal_fallback_"
_QWEN_SUPPORTED_LANGUAGES = frozenset(
    {
        "ar",
        "cs",
        "da",
        "de",
        "en",
        "es",
        "fi",
        "fil",
        "fr",
        "hi",
        "id",
        "is",
        "it",
        "ja",
        "ko",
        "ms",
        "no",
        "pl",
        "pt",
        "ru",
        "sv",
        "th",
        "tr",
        "uk",
        "vi",
        "yue",
        "zh",
    }
)

_ItemKey: TypeAlias = tuple[int, int, int]


@dataclass(slots=True)
class _QwenConnectionState:
    generation: int
    buffer_epoch: int
    next_utterance_id: int
    emit_ready: bool
    item_keys: dict[str, _ItemKey] = field(default_factory=dict)
    item_start_samples_16k: dict[str, int | None] = field(default_factory=dict)
    item_boundaries: dict[str, ProviderAudioRange | None] = field(default_factory=dict)
    provider_item_order: deque[str] = field(default_factory=deque)
    provider_item_aliases: dict[str, str] = field(default_factory=dict)
    ambiguous_provider_items: set[str] = field(default_factory=set)
    completing_provider_items: set[str] = field(default_factory=set)
    synthetic_provider_items: set[str] = field(default_factory=set)
    retired_item_ids: set[str] = field(default_factory=set)
    retired_item_order: deque[str] = field(default_factory=deque)
    next_synthetic_item_id: int = 1
    pending_manual_commits: deque[_ItemKey] = field(default_factory=deque)
    # Monotonic timestamps of provider endpoints whose transcription final is
    # still outstanding, keyed by item id (see _qwen_watch_stalled_items).
    item_deadlines: dict[str, float] = field(default_factory=dict)
    # ``committed`` is weaker than speech_stopped: it carries no trustworthy
    # audio range and may precede speech_started. Keep it private until a
    # stronger event proves that a public utterance exists.
    provisional_commits: dict[str, float] = field(default_factory=dict)
    anonymous_provisional_commits: deque[float] = field(default_factory=deque)
    stalled_deadline_armed: asyncio.Event = field(default_factory=asyncio.Event)
    configured: asyncio.Event = field(default_factory=asyncio.Event)
    finish_received: asyncio.Event = field(default_factory=asyncio.Event)
    intentional_close: asyncio.Event = field(default_factory=asyncio.Event)
    error_sent: asyncio.Event = field(default_factory=asyncio.Event)
    closed_sent: asyncio.Event = field(default_factory=asyncio.Event)
    last_utterance_id: int | None = None
    # Legacy DashScope domains can omit the documented ``item_id`` fields.
    # Their manual stream is ordered, so retain the head commit until final.
    legacy_manual_key: _ItemKey | None = None
    shutdown_request: _AsrWorkerRequest | None = None


def _qwen_event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def _qwen_language_code(language: str) -> str | None:
    normalized = language.strip().lower()
    if normalized == "auto":
        return None
    code = normalized.split("-", 1)[0]
    if code not in _QWEN_SUPPORTED_LANGUAGES:
        raise ValueError("unsupported Qwen ASR language")
    return code


def _qwen_is_auth_rejection(exc: BaseException) -> bool:
    return is_auth_rejection(exc)


def _qwen_session_update(
    config: AsrSessionConfig,
    language: str | None,
) -> dict[str, Any]:
    if config.endpointing_mode == "manual":
        turn_detection: dict[str, str] | None = None
    elif config.endpointing_mode == "provider":
        turn_detection = {"type": "server_vad"}
    else:
        raise ValueError("unsupported Qwen ASR endpointing mode")

    transcription: dict[str, str] = {}
    if language is not None:
        transcription["language"] = language
    return {
        "event_id": _qwen_event_id(),
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": 16000,
            "input_audio_transcription": transcription,
            "turn_detection": turn_detection,
        },
    }


async def _emit_qwen_error_once(
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    state: _QwenConnectionState,
    error_code: str,
    error_message: str,
    *,
    item_key: _ItemKey | None = None,
) -> None:
    if state.error_sent.is_set():
        return
    state.error_sent.set()
    generation, buffer_epoch, utterance_id = item_key or (
        state.generation,
        state.buffer_epoch,
        state.last_utterance_id,
    )
    await response_queue.put(
        _AsrWorkerEvent(
            kind="error",
            generation=generation,
            buffer_epoch=buffer_epoch,
            utterance_id=utterance_id,
            error_code=error_code,
            error_message=error_message,
        )
    )


def _qwen_arm_stalled_item_deadline(
    state: _QwenConnectionState,
    item_id: str,
    *,
    armed_at: float | None = None,
) -> None:
    if item_id and item_id in state.item_keys and item_id not in state.item_deadlines:
        state.item_deadlines[item_id] = (
            time.monotonic() if armed_at is None else armed_at
        )
        state.stalled_deadline_armed.set()


def _qwen_audio_ms_to_sample_16k(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value * 16


def _qwen_record_provisional_commit(
    state: _QwenConnectionState,
    raw_item_id: str,
) -> None:
    armed_at = time.monotonic()
    if raw_item_id:
        state.provisional_commits.setdefault(raw_item_id, armed_at)
    else:
        state.anonymous_provisional_commits.append(armed_at)

    while (
        len(state.provisional_commits)
        + len(state.anonymous_provisional_commits)
        > _QWEN_PROVISIONAL_COMMIT_LIMIT
    ):
        named_oldest = (
            min(state.provisional_commits.items(), key=lambda item: item[1])
            if state.provisional_commits
            else None
        )
        anonymous_oldest = (
            state.anonymous_provisional_commits[0]
            if state.anonymous_provisional_commits
            else None
        )
        if anonymous_oldest is not None and (
            named_oldest is None or anonymous_oldest <= named_oldest[1]
        ):
            state.anonymous_provisional_commits.popleft()
        elif named_oldest is not None:
            state.provisional_commits.pop(named_oldest[0], None)
    state.stalled_deadline_armed.set()


def _qwen_take_provisional_commit(
    state: _QwenConnectionState,
    raw_item_id: str,
    *,
    allow_anonymous: bool,
) -> float | None:
    if raw_item_id:
        armed_at = state.provisional_commits.pop(raw_item_id, None)
        if armed_at is not None:
            return armed_at
    if allow_anonymous and state.anonymous_provisional_commits:
        return state.anonymous_provisional_commits.popleft()
    return None


def _qwen_expire_provisional_commits(
    state: _QwenConnectionState,
    now: float,
) -> None:
    expired_named = tuple(
        item_id
        for item_id, armed_at in state.provisional_commits.items()
        if now - armed_at >= _QWEN_STALLED_ITEM_TIMEOUT_SECONDS
    )
    for item_id in expired_named:
        state.provisional_commits.pop(item_id, None)
    while (
        state.anonymous_provisional_commits
        and now - state.anonymous_provisional_commits[0]
        >= _QWEN_STALLED_ITEM_TIMEOUT_SECONDS
    ):
        state.anonymous_provisional_commits.popleft()


def _qwen_remember_retired_item(
    state: _QwenConnectionState,
    item_id: str,
) -> None:
    if not item_id or item_id in state.retired_item_ids:
        return
    state.retired_item_ids.add(item_id)
    state.retired_item_order.append(item_id)
    while len(state.retired_item_order) > _QWEN_ITEM_TOMBSTONE_LIMIT:
        expired = state.retired_item_order.popleft()
        state.retired_item_ids.discard(expired)


def _qwen_next_synthetic_item(state: _QwenConnectionState) -> str:
    while True:
        item_id = f"{_QWEN_SYNTHETIC_ITEM_PREFIX}{state.next_synthetic_item_id}"
        state.next_synthetic_item_id += 1
        if (
            item_id not in state.item_keys
            and item_id not in state.provider_item_aliases
            and item_id not in state.retired_item_ids
        ):
            return item_id


def _qwen_oldest_provider_item(state: _QwenConnectionState) -> str | None:
    while state.provider_item_order:
        item_id = state.provider_item_order[0]
        if item_id in state.item_keys:
            return item_id
        state.provider_item_order.popleft()
    return None


async def _qwen_open_provider_item(
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    state: _QwenConnectionState,
    item_id: str,
    *,
    start_sample_16k: int | None,
    ambiguous: bool,
    terminal: bool = False,
) -> str | None:
    if item_id and item_id in state.retired_item_ids:
        return None
    canonical_item_id = item_id or _qwen_next_synthetic_item(state)
    if canonical_item_id in state.item_keys:
        return canonical_item_id
    key = (
        state.generation,
        state.buffer_epoch,
        state.next_utterance_id,
    )
    state.next_utterance_id += 1
    state.last_utterance_id = key[2]
    state.item_keys[canonical_item_id] = key
    canonical_start = None if ambiguous else start_sample_16k
    state.item_start_samples_16k[canonical_item_id] = canonical_start
    state.provider_item_order.append(canonical_item_id)
    if item_id:
        state.provider_item_aliases[item_id] = canonical_item_id
    else:
        state.synthetic_provider_items.add(canonical_item_id)
    if ambiguous:
        state.ambiguous_provider_items.add(canonical_item_id)
    if terminal:
        # Claim terminal ownership before the first potentially blocking put;
        # the watchdog must never retire this item while completed is queued.
        state.completing_provider_items.add(canonical_item_id)
        state.item_deadlines.pop(canonical_item_id, None)
    await response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            generation=key[0],
            buffer_epoch=key[1],
            utterance_id=key[2],
            audio_start_sample_16k=canonical_start,
        )
    )
    return canonical_item_id


async def _qwen_mark_provider_item_ambiguous(
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    state: _QwenConnectionState,
    item_id: str,
) -> None:
    if item_id in state.ambiguous_provider_items:
        return
    state.ambiguous_provider_items.add(item_id)
    state.item_start_samples_16k[item_id] = None
    # Boundary revocation is the single ordered poison event for late start
    # conflicts. Emitting a second started event here would require two queue
    # slots atomically and can deadlock a bounded worker response queue.
    await _qwen_emit_provider_endpoint(response_queue, state, item_id, None)


async def _qwen_resolve_provider_item(
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    state: _QwenConnectionState,
    raw_item_id: str,
    *,
    terminal: bool = False,
) -> str | None:
    if raw_item_id:
        if raw_item_id in state.retired_item_ids:
            return None
        item_id = state.provider_item_aliases.get(raw_item_id)
        if item_id is not None and item_id in state.item_keys:
            if terminal:
                state.completing_provider_items.add(item_id)
                state.item_deadlines.pop(item_id, None)
            return item_id
    item_id = _qwen_oldest_provider_item(state)
    if item_id is None:
        item_id = await _qwen_open_provider_item(
            response_queue,
            state,
            raw_item_id,
            start_sample_16k=None,
            ambiguous=True,
            terminal=terminal,
        )
        if terminal and item_id is not None:
            state.completing_provider_items.add(item_id)
            state.item_deadlines.pop(item_id, None)
        return item_id
    if raw_item_id:
        state.provider_item_aliases[raw_item_id] = item_id
    if terminal:
        state.completing_provider_items.add(item_id)
        state.item_deadlines.pop(item_id, None)
    await _qwen_mark_provider_item_ambiguous(response_queue, state, item_id)
    return item_id if item_id in state.item_keys else None


def _qwen_retire_provider_item(
    state: _QwenConnectionState,
    item_id: str,
) -> _ItemKey | None:
    key = state.item_keys.pop(item_id, None)
    if key is None:
        return None
    state.item_deadlines.pop(item_id, None)
    state.item_start_samples_16k.pop(item_id, None)
    state.item_boundaries.pop(item_id, None)
    state.ambiguous_provider_items.discard(item_id)
    state.completing_provider_items.discard(item_id)
    is_synthetic = item_id in state.synthetic_provider_items
    state.synthetic_provider_items.discard(item_id)
    try:
        state.provider_item_order.remove(item_id)
    except ValueError:
        pass
    aliases = tuple(
        alias
        for alias, canonical_item_id in state.provider_item_aliases.items()
        if canonical_item_id == item_id
    )
    for alias in aliases:
        state.provider_item_aliases.pop(alias, None)
        state.provisional_commits.pop(alias, None)
        _qwen_remember_retired_item(state, alias)
    state.provisional_commits.pop(item_id, None)
    if not is_synthetic:
        _qwen_remember_retired_item(state, item_id)
    return key


async def _qwen_emit_provider_endpoint(
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    state: _QwenConnectionState,
    item_id: str,
    audio_range: ProviderAudioRange | None,
) -> None:
    key = state.item_keys.get(item_id)
    if key is None:
        return
    if item_id in state.item_boundaries:
        existing = state.item_boundaries[item_id]
        if existing is None or existing == audio_range:
            return
        audio_range = None
    state.item_boundaries[item_id] = audio_range
    await response_queue.put(
        _AsrWorkerEvent(
            kind="provider_endpoint",
            generation=key[0],
            buffer_epoch=key[1],
            utterance_id=key[2],
            boundary_quality="exact" if audio_range is not None else "unknown",
            audio_range=audio_range,
        )
    )


async def _qwen_expire_stalled_items(
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    state: _QwenConnectionState,
) -> None:
    now = time.monotonic()
    _qwen_expire_provisional_commits(state, now)
    expired_ids = [
        item_id
        for item_id, armed_at in state.item_deadlines.items()
        if item_id not in state.completing_provider_items
        and now - armed_at >= _QWEN_STALLED_ITEM_TIMEOUT_SECONDS
    ]
    for item_id in expired_ids:
        if (
            item_id not in state.item_deadlines
            or item_id in state.completing_provider_items
        ):
            continue
        if item_id not in state.item_boundaries:
            await _qwen_emit_provider_endpoint(response_queue, state, item_id, None)
        if (
            item_id not in state.item_deadlines
            or item_id in state.completing_provider_items
        ):
            continue
        # Retirement records every observed provider alias before releasing
        # the key, so a late start/completed pair cannot resurrect the turn.
        key = _qwen_retire_provider_item(state, item_id)
        if key is None:
            continue
        await response_queue.put(
            _AsrWorkerEvent(
                kind="final",
                generation=key[0],
                buffer_epoch=key[1],
                utterance_id=key[2],
                text="",
            )
        )


async def _qwen_watch_stalled_items(
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    state: _QwenConnectionState,
) -> None:
    # Runs beside the receiver because the receiver blocks on provider
    # frames; a provider that goes silent after server VAD reported the end
    # of speech would otherwise never trigger the sweep, leaving the
    # upstream turn open unboundedly.
    while True:
        deadline_values = (
            *state.item_deadlines.values(),
            *state.provisional_commits.values(),
            *state.anonymous_provisional_commits,
        )
        if not deadline_values:
            await state.stalled_deadline_armed.wait()
            state.stalled_deadline_armed.clear()
            continue
        earliest = min(deadline_values)
        remaining = earliest + _QWEN_STALLED_ITEM_TIMEOUT_SECONDS - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
            continue
        await _qwen_expire_stalled_items(response_queue, state)


async def _qwen_sender(
    ws: Any,
    request_queue: asyncio.Queue[_AsrWorkerRequest],
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    config: AsrSessionConfig,
    state: _QwenConnectionState,
) -> tuple[str, _AsrWorkerRequest | None]:
    await state.configured.wait()
    try:
        while True:
            request = await request_queue.get()
            try:
                if request.kind == "audio":
                    state.last_utterance_id = request.utterance_id
                    await ws.send(
                        json.dumps(
                            {
                                "event_id": _qwen_event_id(),
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(request.audio).decode(
                                    "ascii"
                                ),
                            }
                        )
                    )
                    continue

                if request.kind == "commit":
                    if config.endpointing_mode != "manual":
                        await _emit_qwen_error_once(
                            response_queue,
                            state,
                            "ASR_QWEN_PROTOCOL_ERROR",
                            "Qwen ASR received commit while server VAD is active",
                        )
                        return "error", request
                    if request.utterance_id is None:
                        await _emit_qwen_error_once(
                            response_queue,
                            state,
                            "ASR_QWEN_PROTOCOL_ERROR",
                            "Qwen ASR commit is missing an utterance identifier",
                        )
                        return "error", request
                    key = (
                        request.generation,
                        request.buffer_epoch,
                        request.utterance_id,
                    )
                    state.pending_manual_commits.append(key)
                    await ws.send(
                        json.dumps(
                            {
                                "event_id": _qwen_event_id(),
                                "type": "input_audio_buffer.commit",
                            }
                        )
                    )
                    continue

                if request.kind == "clear":
                    state.intentional_close.set()
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return "clear", request

                if request.kind == "shutdown":
                    state.shutdown_request = request
                    await ws.send(
                        json.dumps(
                            {
                                "event_id": _qwen_event_id(),
                                "type": "session.finish",
                            }
                        )
                    )
                    try:
                        await asyncio.wait_for(
                            state.finish_received.wait(),
                            timeout=_QWEN_FINISH_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        if not state.closed_sent.is_set():
                            state.closed_sent.set()
                            await response_queue.put(
                                _AsrWorkerEvent(
                                    kind="closed",
                                    generation=request.generation,
                                    buffer_epoch=request.buffer_epoch,
                                    utterance_id=request.utterance_id,
                                )
                            )
                    state.intentional_close.set()
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return "shutdown", request

                await _emit_qwen_error_once(
                    response_queue,
                    state,
                    "ASR_QWEN_PROTOCOL_ERROR",
                    "Qwen ASR received an unsupported command",
                )
                return "error", request
            finally:
                request_queue.task_done()
    except asyncio.CancelledError:
        raise
    except ConnectionClosed:
        if not state.intentional_close.is_set():
            await _emit_qwen_error_once(
                response_queue,
                state,
                "ASR_QWEN_CONNECTION_CLOSED",
                "Qwen ASR connection closed unexpectedly",
            )
        return "error", None
    except Exception:
        await _emit_qwen_error_once(
            response_queue,
            state,
            "ASR_QWEN_WORKER_FAILED",
            "Qwen ASR sender failed",
        )
        return "error", None


async def _qwen_receiver(
    ws: Any,
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    config: AsrSessionConfig,
    state: _QwenConnectionState,
) -> str:
    try:
        async for raw_message in ws:
            try:
                event = json.loads(raw_message)
            except (TypeError, ValueError):
                await _emit_qwen_error_once(
                    response_queue,
                    state,
                    "ASR_QWEN_PROTOCOL_ERROR",
                    "Qwen ASR returned an invalid event",
                )
                return "error"

            event_type = event.get("type")
            if event_type == "session.updated":
                if not state.configured.is_set():
                    state.configured.set()
                    if state.emit_ready:
                        await response_queue.put(
                            _AsrWorkerEvent(
                                kind="ready",
                                generation=state.generation,
                                buffer_epoch=state.buffer_epoch,
                            )
                        )
                continue

            if event_type in (
                "error",
                "conversation.item.input_audio_transcription.failed",
            ):
                if state.intentional_close.is_set():
                    return "closed"
                item_id = str(event.get("item_id") or "")
                await _emit_qwen_error_once(
                    response_queue,
                    state,
                    "ASR_QWEN_PROVIDER_ERROR",
                    "Qwen ASR provider reported an error",
                    item_key=(
                        state.item_keys.get(item_id)
                        if item_id
                        else state.legacy_manual_key
                    ),
                )
                return "error"

            if event_type == "conversation.item.created":
                if config.endpointing_mode != "manual":
                    continue
                item = event.get("item")
                item_id = str(item.get("id") or "") if isinstance(item, dict) else ""
                if not state.pending_manual_commits:
                    continue
                if item_id:
                    if item_id not in state.item_keys:
                        state.item_keys[item_id] = (
                            state.pending_manual_commits.popleft()
                        )
                elif state.legacy_manual_key is None:
                    state.legacy_manual_key = state.pending_manual_commits[0]
                continue

            if event_type == "input_audio_buffer.speech_started":
                if config.endpointing_mode != "provider":
                    continue
                raw_item_id = str(event.get("item_id") or "")
                start_sample_16k = _qwen_audio_ms_to_sample_16k(
                    event.get("audio_start_ms")
                )
                if raw_item_id and raw_item_id in state.retired_item_ids:
                    continue
                canonical_item_id = state.provider_item_aliases.get(raw_item_id)
                if (
                    canonical_item_id is not None
                    and canonical_item_id in state.item_keys
                ):
                    if (
                        state.item_start_samples_16k.get(canonical_item_id)
                        != start_sample_16k
                    ):
                        await _qwen_mark_provider_item_ambiguous(
                            response_queue,
                            state,
                            canonical_item_id,
                        )
                    continue
                provisional_armed_at = _qwen_take_provisional_commit(
                    state,
                    raw_item_id,
                    allow_anonymous=True,
                )
                opened_item_id = await _qwen_open_provider_item(
                    response_queue,
                    state,
                    raw_item_id,
                    start_sample_16k=start_sample_16k,
                    ambiguous=not raw_item_id,
                )
                if opened_item_id is not None and provisional_armed_at is not None:
                    _qwen_arm_stalled_item_deadline(
                        state,
                        opened_item_id,
                        armed_at=provisional_armed_at,
                    )
                continue

            if event_type == "input_audio_buffer.speech_stopped":
                if config.endpointing_mode != "provider":
                    continue
                # Server VAD sealed the turn; the transcription final is
                # still outstanding. Arm the stalled-item deadline so a
                # delayed or missing completed event cannot leave the
                # upstream turn open unboundedly.
                raw_item_id = str(event.get("item_id") or "")
                if raw_item_id and raw_item_id in state.retired_item_ids:
                    continue
                canonical_item_id = (
                    state.provider_item_aliases.get(raw_item_id)
                    if raw_item_id
                    else _qwen_oldest_provider_item(state)
                )
                provisional_armed_at: float | None = None
                if (
                    canonical_item_id is None
                    or canonical_item_id not in state.item_keys
                ):
                    provisional_armed_at = _qwen_take_provisional_commit(
                        state,
                        raw_item_id,
                        allow_anonymous=(
                            _qwen_oldest_provider_item(state) is None
                        ),
                    )
                if provisional_armed_at is not None:
                    item_id = await _qwen_open_provider_item(
                        response_queue,
                        state,
                        raw_item_id,
                        start_sample_16k=None,
                        ambiguous=True,
                    )
                else:
                    item_id = await _qwen_resolve_provider_item(
                        response_queue,
                        state,
                        raw_item_id,
                    )
                if item_id is None:
                    continue
                _qwen_arm_stalled_item_deadline(
                    state,
                    item_id,
                    armed_at=provisional_armed_at,
                )
                start_sample_16k = state.item_start_samples_16k.get(item_id)
                end_sample_16k = _qwen_audio_ms_to_sample_16k(event.get("audio_end_ms"))
                audio_range = None
                if (
                    item_id not in state.ambiguous_provider_items
                    and start_sample_16k is not None
                    and end_sample_16k is not None
                    and end_sample_16k > start_sample_16k
                ):
                    audio_range = ProviderAudioRange(
                        start_sample_16k=start_sample_16k,
                        end_sample_16k=end_sample_16k,
                    )
                await _qwen_emit_provider_endpoint(
                    response_queue,
                    state,
                    item_id,
                    audio_range,
                )
                continue

            if event_type == "input_audio_buffer.committed":
                raw_item_id = str(event.get("item_id") or "")
                if config.endpointing_mode == "provider":
                    if raw_item_id and raw_item_id in state.retired_item_ids:
                        continue
                    item_id = (
                        state.provider_item_aliases.get(raw_item_id)
                        if raw_item_id
                        else _qwen_oldest_provider_item(state)
                    )
                    if item_id is not None and item_id in state.item_keys:
                        _qwen_arm_stalled_item_deadline(state, item_id)
                    else:
                        _qwen_record_provisional_commit(state, raw_item_id)
                    continue
                if (
                    config.endpointing_mode == "manual"
                    and raw_item_id
                    and raw_item_id not in state.item_keys
                    and state.pending_manual_commits
                ):
                    state.item_keys[raw_item_id] = (
                        state.pending_manual_commits.popleft()
                    )
                elif (
                    config.endpointing_mode == "manual"
                    and not raw_item_id
                    and state.legacy_manual_key is None
                    and state.pending_manual_commits
                ):
                    state.legacy_manual_key = state.pending_manual_commits[0]
                continue

            if event_type == "conversation.item.input_audio_transcription.text":
                raw_item_id = str(event.get("item_id") or "")
                if config.endpointing_mode == "provider":
                    if raw_item_id and raw_item_id in state.retired_item_ids:
                        continue
                    canonical_item_id = (
                        state.provider_item_aliases.get(raw_item_id)
                        if raw_item_id
                        else _qwen_oldest_provider_item(state)
                    )
                    provisional_armed_at: float | None = None
                    if (
                        canonical_item_id is None
                        or canonical_item_id not in state.item_keys
                    ):
                        provisional_armed_at = _qwen_take_provisional_commit(
                            state,
                            raw_item_id,
                            allow_anonymous=(
                                _qwen_oldest_provider_item(state) is None
                            ),
                        )
                    if provisional_armed_at is not None:
                        item_id = await _qwen_open_provider_item(
                            response_queue,
                            state,
                            raw_item_id,
                            start_sample_16k=None,
                            ambiguous=True,
                        )
                        if item_id is not None:
                            _qwen_arm_stalled_item_deadline(
                                state,
                                item_id,
                                armed_at=provisional_armed_at,
                            )
                    else:
                        item_id = await _qwen_resolve_provider_item(
                            response_queue,
                            state,
                            raw_item_id,
                        )
                    key = state.item_keys.get(item_id) if item_id else None
                else:
                    item_id = raw_item_id
                    key = (
                        state.item_keys.get(item_id)
                        if item_id
                        else state.legacy_manual_key
                    )
                if key is not None:
                    if item_id in state.item_deadlines:
                        # Streaming text proves the transcription is alive;
                        # push the stalled-item deadline forward instead of
                        # expiring mid-stream.
                        state.item_deadlines[item_id] = time.monotonic()
                    text = str(event.get("text") or "") + str(event.get("stash") or "")
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="partial",
                            generation=key[0],
                            buffer_epoch=key[1],
                            utterance_id=key[2],
                            text=text,
                        )
                    )
                continue

            if event_type == "conversation.item.input_audio_transcription.completed":
                raw_item_id = str(event.get("item_id") or "")
                if config.endpointing_mode == "provider":
                    if raw_item_id and raw_item_id in state.retired_item_ids:
                        continue
                    canonical_item_id = (
                        state.provider_item_aliases.get(raw_item_id)
                        if raw_item_id
                        else _qwen_oldest_provider_item(state)
                    )
                    provisional_armed_at: float | None = None
                    if (
                        canonical_item_id is None
                        or canonical_item_id not in state.item_keys
                    ):
                        provisional_armed_at = _qwen_take_provisional_commit(
                            state,
                            raw_item_id,
                            allow_anonymous=(
                                _qwen_oldest_provider_item(state) is None
                            ),
                        )
                    if provisional_armed_at is not None:
                        item_id = await _qwen_open_provider_item(
                            response_queue,
                            state,
                            raw_item_id,
                            start_sample_16k=None,
                            ambiguous=True,
                            terminal=True,
                        )
                    else:
                        item_id = await _qwen_resolve_provider_item(
                            response_queue,
                            state,
                            raw_item_id,
                            terminal=True,
                        )
                    if item_id is None:
                        continue
                    if item_id not in state.item_boundaries:
                        await _qwen_emit_provider_endpoint(
                            response_queue,
                            state,
                            item_id,
                            None,
                        )
                    key = _qwen_retire_provider_item(state, item_id)
                else:
                    item_id = raw_item_id
                    if item_id:
                        state.item_deadlines.pop(item_id, None)
                    key = (
                        state.item_keys.pop(item_id, None)
                        if item_id
                        else state.legacy_manual_key
                    )
                if key is not None:
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="final",
                            generation=key[0],
                            buffer_epoch=key[1],
                            utterance_id=key[2],
                            text=str(event.get("transcript") or ""),
                        )
                    )
                    if config.endpointing_mode == "manual" and not item_id:
                        state.legacy_manual_key = None
                        if (
                            state.pending_manual_commits
                            and state.pending_manual_commits[0] == key
                        ):
                            state.pending_manual_commits.popleft()
                if config.endpointing_mode == "manual" and item_id:
                    state.item_start_samples_16k.pop(item_id, None)
                    state.item_boundaries.pop(item_id, None)
                continue

            if event_type == "session.finished":
                state.finish_received.set()
                if not state.closed_sent.is_set():
                    state.closed_sent.set()
                    request = state.shutdown_request
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="closed",
                            generation=(
                                request.generation if request else state.generation
                            ),
                            buffer_epoch=(
                                request.buffer_epoch if request else state.buffer_epoch
                            ),
                            utterance_id=(
                                request.utterance_id
                                if request
                                else state.last_utterance_id
                            ),
                        )
                    )
                return "closed"

        if not state.intentional_close.is_set() and not state.closed_sent.is_set():
            await _emit_qwen_error_once(
                response_queue,
                state,
                "ASR_QWEN_CONNECTION_CLOSED",
                "Qwen ASR connection closed unexpectedly",
            )
            return "error"
        return "closed"
    except asyncio.CancelledError:
        raise
    except ConnectionClosed:
        if not state.intentional_close.is_set() and not state.closed_sent.is_set():
            await _emit_qwen_error_once(
                response_queue,
                state,
                "ASR_QWEN_CONNECTION_CLOSED",
                "Qwen ASR connection closed unexpectedly",
            )
            return "error"
        return "closed"
    except Exception:
        if state.intentional_close.is_set():
            return "closed"
        await _emit_qwen_error_once(
            response_queue,
            state,
            "ASR_QWEN_WORKER_FAILED",
            "Qwen ASR receiver failed",
        )
        return "error"


async def qwen_asr_worker(
    request_queue: asyncio.Queue[_AsrWorkerRequest],
    response_queue: asyncio.Queue[_AsrWorkerEvent],
    api_key: str,
    config: AsrSessionConfig,
    *,
    region: str = "cn",
) -> None:
    """Stream normalized PCM to Qwen-ASR and normalize provider events."""

    generation = 0
    buffer_epoch = 0
    next_utterance_id = 1
    first_connection = True
    closed_sent = False
    active_state: _QwenConnectionState | None = None

    try:
        if region not in ("cn", "intl"):
            raise ValueError("unsupported Qwen ASR region")
        if not api_key:
            raise PermissionError("Qwen ASR credentials are missing")
        language = _qwen_language_code(config.language)
        session_update = _qwen_session_update(config, language)
        url = _QWEN_CN_URL if region == "cn" else _QWEN_INTL_URL

        while True:
            state = _QwenConnectionState(
                generation=generation,
                buffer_epoch=buffer_epoch,
                next_utterance_id=next_utterance_id,
                emit_ready=first_connection,
            )
            active_state = state
            ws: Any | None = None
            sender_task: asyncio.Task[tuple[str, _AsrWorkerRequest | None]] | None = (
                None
            )
            receiver_task: asyncio.Task[str] | None = None
            stalled_watch_task: asyncio.Task[None] | None = None
            outcome = "error"
            outcome_request: _AsrWorkerRequest | None = None
            try:
                ws = await websockets.connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {api_key}"},
                    close_timeout=0.5,
                )
                receiver_task = asyncio.create_task(
                    _qwen_receiver(ws, response_queue, config, state),
                    name="qwen-asr-receiver",
                )
                await ws.send(json.dumps(session_update))
                sender_task = asyncio.create_task(
                    _qwen_sender(
                        ws,
                        request_queue,
                        response_queue,
                        config,
                        state,
                    ),
                    name="qwen-asr-sender",
                )
                # The watchdog never finishes on its own and stays out of the
                # outcome wait; teardown below cancels it with its siblings.
                stalled_watch_task = asyncio.create_task(
                    _qwen_watch_stalled_items(response_queue, state),
                    name="qwen-asr-stalled-watch",
                )
                done, pending = await asyncio.wait(
                    {sender_task, receiver_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if sender_task in done:
                    outcome, outcome_request = await sender_task
                if (
                    receiver_task in done
                    and state.intentional_close.is_set()
                    and sender_task not in done
                ):
                    try:
                        outcome, outcome_request = await asyncio.wait_for(
                            asyncio.shield(sender_task), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        pass
                if receiver_task in done:
                    receiver_outcome = await receiver_task
                    if receiver_outcome == "error":
                        outcome = "error"
                    elif receiver_outcome == "closed" and outcome != "clear":
                        outcome = "shutdown"
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await _emit_qwen_error_once(
                    response_queue,
                    state,
                    (
                        "ASR_CREDENTIALS_REJECTED"
                        if _qwen_is_auth_rejection(exc)
                        else "ASR_QWEN_CONNECTION_FAILED"
                    ),
                    (
                        "Qwen ASR credentials were rejected"
                        if _qwen_is_auth_rejection(exc)
                        else "Qwen ASR connection or session setup failed"
                    ),
                )
                outcome = "error"
            finally:
                for task in (sender_task, receiver_task, stalled_watch_task):
                    if task is not None and not task.done():
                        task.cancel()
                pending_tasks = [
                    task
                    for task in (sender_task, receiver_task, stalled_watch_task)
                    if task is not None and not task.done()
                ]
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                if ws is not None:
                    state.intentional_close.set()
                    try:
                        await ws.close()
                    except Exception:
                        pass

            closed_sent = state.closed_sent.is_set()
            if outcome == "clear" and outcome_request is not None:
                generation = outcome_request.generation
                buffer_epoch = outcome_request.buffer_epoch
                next_utterance_id = outcome_request.utterance_id or 1
                first_connection = False
                continue
            if outcome_request is not None:
                generation = outcome_request.generation
                buffer_epoch = outcome_request.buffer_epoch
                next_utterance_id = outcome_request.utterance_id or next_utterance_id
            return
    except asyncio.CancelledError:
        raise
    except PermissionError:
        await response_queue.put(
            _AsrWorkerEvent(
                kind="error",
                generation=generation,
                buffer_epoch=buffer_epoch,
                error_code="ASR_CREDENTIALS_MISSING",
                error_message="Qwen ASR credentials are missing",
            )
        )
    except ValueError as exc:
        message = str(exc)
        code = (
            "ASR_LANGUAGE_NOT_SUPPORTED"
            if "language" in message
            else "ASR_INVALID_CONFIG"
        )
        await response_queue.put(
            _AsrWorkerEvent(
                kind="error",
                generation=generation,
                buffer_epoch=buffer_epoch,
                error_code=code,
                error_message="Qwen ASR configuration is not supported",
            )
        )
    finally:
        if active_state is not None:
            closed_sent = closed_sent or active_state.closed_sent.is_set()
        if not closed_sent:
            await response_queue.put(
                _AsrWorkerEvent(
                    kind="closed",
                    generation=generation,
                    buffer_epoch=buffer_epoch,
                    utterance_id=(
                        active_state.last_utterance_id if active_state else None
                    ),
                )
            )
