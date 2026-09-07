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

"""Shared contracts and lifecycle machinery for realtime ASR workers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

import numpy as np
import soxr

from ._provider_events import (
    ProviderAudioRange,
    ProviderBoundaryQuality,
    ProviderEndpointNotification,
    ProviderFinalNotification,
    ProviderStartedSettlement,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from .provider_policy import AsrProviderPolicy
from .boundary_settlement import BoundarySettlement
from .transcript import SegmentAggregator


logger = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 10.0
_CALLBACK_DRAIN_TIMEOUT_SECONDS = 5.0
_WORKER_CLOSE_TIMEOUT_SECONDS = 5.0
# 活跃音频队列按时长限额；chunk 数量只保留为兼容常量，不参与容量判断。
_REQUEST_QUEUE_SIZE = 64
_ACTIVE_QUEUE_MAX_AUDIO_MS = 2_000
_ACTIVE_QUEUE_MAX_AUDIO_BYTES = _ACTIVE_QUEUE_MAX_AUDIO_MS * 16_000 * 2 // 1_000
_ACTIVE_QUEUE_MAX_AUDIO_ITEMS = 256
_REQUEST_BACKPRESSURE_TIMEOUT_SECONDS = 2.0
_RESPONSE_QUEUE_SIZE = 128
_CALLBACK_QUEUE_SIZE = 64
# Exact failed-key tombstones are bounded. Reaching the cap latches the whole
# Provider namespace fail-closed instead of evicting an old key that could
# later be replayed as a duplicate started event.
_PROVIDER_STARTED_TOMBSTONE_LIMIT = _CALLBACK_QUEUE_SIZE * 2
_MAX_PROVIDER_BOUNDARY_TASKS = 8
_PROVIDER_BOUNDARY_CALLBACK_TIMEOUT_SECONDS = 0.2
_PROVIDER_FINAL_ADMISSION_TIMEOUT_SECONDS = 0.2

_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_OMNI_ONLY_FIELDS = frozenset(
    {
        "audio",
        "beta_fields",
        "enable_search",
        "input_audio_format",
        "input_audio_noise_reduction",
        "input_audio_transcription",
        "instructions",
        "language_code",
        "modalities",
        "model",
        "output_audio_format",
        "output_modalities",
        "repetition_penalty",
        "session",
        "temperature",
        "tool_choice",
        "tools",
        "turn_detection",
        "type",
        "voice",
    }
)

_RequestKind: TypeAlias = Literal["audio", "commit", "clear", "shutdown"]
_EventKind: TypeAlias = Literal[
    "ready",
    "utterance_started",
    "provider_endpoint",
    "partial",
    "final",
    "error",
    "closed",
]
_UtteranceKey: TypeAlias = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class AsrSessionConfig:
    """Provider-neutral ASR settings frozen when the worker connects."""

    language: str = "zh"
    input_sample_rate_hz: Literal[16000, 48000] = 16000
    endpointing_mode: Literal["manual", "provider"] = "manual"

    def __post_init__(self) -> None:
        language = str(self.language).strip()
        if language.lower() == "auto":
            normalized_language = "auto"
        elif not _LANGUAGE_RE.fullmatch(language):
            raise ValueError("ASR_INVALID_CONFIG: invalid language")
        else:
            parts = language.split("-")
            normalized_parts = [parts[0].lower()]
            for part in parts[1:]:
                if len(part) == 2 and part.isalpha():
                    normalized_parts.append(part.upper())
                elif len(part) == 4 and part.isalpha():
                    normalized_parts.append(part.title())
                else:
                    normalized_parts.append(part)
            normalized_language = "-".join(normalized_parts)

        if self.input_sample_rate_hz not in (16000, 48000):
            raise ValueError(
                "ASR_INVALID_CONFIG: input_sample_rate_hz must be 16000 or 48000"
            )
        if self.endpointing_mode not in ("manual", "provider"):
            raise ValueError(
                "ASR_INVALID_CONFIG: endpointing_mode must be 'manual' or 'provider'"
            )
        object.__setattr__(self, "language", normalized_language)


@runtime_checkable
class RealtimeAsrSession(Protocol):
    """Stable session surface used by audio-producing callers."""

    @property
    def is_ready(self) -> bool: ...

    async def connect(
        self,
        instructions: str = "",
        native_audio: bool = False,
    ) -> None: ...

    async def update_session(self, config: Mapping[str, Any]) -> None: ...

    async def stream_audio(
        self,
        audio_chunk: bytes,
        *,
        sample_rate_hz: int | None = None,
    ) -> None: ...

    async def signal_user_activity_end(self) -> None: ...

    async def clear_audio_buffer(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _AsrWorkerRequest:
    """One normalized command sent from a session to its selected worker."""

    kind: _RequestKind
    generation: int
    buffer_epoch: int = 0
    utterance_id: int | None = None
    audio: bytes = b""


@dataclass(slots=True)
class _QueuedAudioHold:
    """Keep dequeued audio charged to the session backpressure budget."""

    queue: _AsrRequestQueue
    audio_bytes: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.queue._release_held_audio(self.audio_bytes)


class _AsrRequestQueue(asyncio.Queue[_AsrWorkerRequest]):
    """Unbounded control queue with bounded, explicitly held audio accounting."""

    def __init__(self) -> None:
        super().__init__()
        self._held_audio_bytes = 0
        self._held_audio_items = 0

    def hold_dequeued_audio(
        self,
        request: _AsrWorkerRequest,
    ) -> _QueuedAudioHold | None:
        if request.kind != "audio" or not request.audio:
            return None
        audio_bytes = len(request.audio)
        self._held_audio_bytes += audio_bytes
        self._held_audio_items += 1
        return _QueuedAudioHold(queue=self, audio_bytes=audio_bytes)

    async def get_with_audio_hold(
        self,
    ) -> tuple[_AsrWorkerRequest, _QueuedAudioHold | None]:
        """Atomically transfer dequeued audio into the held budget."""

        request = await super().get()
        return request, self.hold_dequeued_audio(request)

    def _release_held_audio(self, audio_bytes: int) -> None:
        if audio_bytes <= 0:
            raise ValueError("ASR_REQUEST_ACCOUNTING_INVALID_AUDIO")
        if self._held_audio_items <= 0 or audio_bytes > self._held_audio_bytes:
            raise RuntimeError("ASR_REQUEST_ACCOUNTING_UNDERFLOW")
        self._held_audio_bytes -= audio_bytes
        self._held_audio_items -= 1

    @property
    def waiting_audio_bytes(self) -> int:
        return self._queued_audio_bytes + self._held_audio_bytes

    @property
    def waiting_audio_items(self) -> int:
        return self._queued_audio_items + self._held_audio_items

    @property
    def held_audio_bytes(self) -> int:
        return self._held_audio_bytes

    @property
    def held_audio_items(self) -> int:
        return self._held_audio_items

    @property
    def _queued_audio_bytes(self) -> int:
        return sum(
            len(item.audio)
            for item in self._queue
            if isinstance(item, _AsrWorkerRequest) and item.kind == "audio"
        )

    @property
    def _queued_audio_items(self) -> int:
        return sum(
            1
            for item in self._queue
            if isinstance(item, _AsrWorkerRequest) and item.kind == "audio"
        )


@dataclass(frozen=True, slots=True)
class _AsrWorkerEvent:
    """One provider-neutral event returned by an ASR worker."""

    kind: _EventKind
    generation: int
    buffer_epoch: int = 0
    utterance_id: int | None = None
    # Epoch-local offset on the provider-neutral 16 kHz mono input timeline.
    # Workers may use a different transport sample rate after this boundary.
    audio_start_sample_16k: int | None = None
    boundary_quality: ProviderBoundaryQuality | None = None
    audio_range: ProviderAudioRange | None = None
    text: str = ""
    error_code: str = ""
    error_message: str = ""


AsrWorkerFn: TypeAlias = Callable[
    [
        asyncio.Queue[_AsrWorkerRequest],
        asyncio.Queue[_AsrWorkerEvent],
        str,
        AsrSessionConfig,
    ],
    Awaitable[None],
]
ProviderEndpointCallback: TypeAlias = Callable[
    [ProviderEndpointNotification], Awaitable[None]
]
ProviderUtteranceStartedCallback: TypeAlias = Callable[
    [ProviderUtteranceStartedNotification],
    Awaitable[ProviderStartedSettlement | None],
]
ProviderFinalCallback: TypeAlias = Callable[
    [ProviderUtteranceKey, str], Awaitable[None]
]
ProviderFinalNotificationCallback: TypeAlias = Callable[
    [ProviderFinalNotification], Awaitable[None]
]


class _VoiceTurnAdapterProtocol(Protocol):
    async def start(self) -> None: ...

    async def push_audio(
        self,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
        pcm16: bytes,
    ) -> None: ...

    async def reset(
        self,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None: ...

    async def wait_failure(self) -> Any: ...

    async def close(self) -> None: ...


VoiceTurnFactory: TypeAlias = Callable[
    [Callable[[int, int, int], Awaitable[None]]],
    _VoiceTurnAdapterProtocol,
]


class _SessionState(Enum):
    NEW = "new"
    CONNECTING = "connecting"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _PendingFinal:
    text: str
    received_at: float
    admission_deadline: float


@dataclass(frozen=True, slots=True)
class _CallbackItem:
    text: str
    generation: int
    buffer_epoch: int
    utterance_id: int | None = None
    # "provider_started" items carry Provider text-turn identity through the
    # same bounded FIFO as its later endpoint and transcript. "endpoint"
    # items carry the deferred keyless seal notification through
    # the same FIFO as transcripts so it reaches the runtime only after every
    # earlier turn's final has been delivered (and the runtime has activated
    # the turn this notification belongs to). "partial" items carry live
    # preview text through the same FIFO so a preview can never overtake an
    # earlier turn's final that is still queued for delivery.
    kind: Literal["transcript", "endpoint", "partial", "provider_started"] = (
        "transcript"
    )
    provider_started: ProviderUtteranceStartedNotification | None = None
    provider_endpoint: ProviderEndpointNotification | None = None
    final_received_at: float | None = None
    final_admission_deadline: float | None = None


class _RealtimeAsrSessionImpl:
    """Default asyncio session implementation shared by all ASR workers."""

    def __init__(
        self,
        *,
        worker_fn: AsrWorkerFn,
        api_key: str,
        config: AsrSessionConfig,
        on_input_transcript: Callable[[str], Awaitable[None]],
        on_connection_error: Callable[[str], Awaitable[None]],
        on_status_message: Callable[[str], Awaitable[None]] | None = None,
        on_turn_endpointed: Callable[[], Awaitable[None]] | None = None,
        on_provider_utterance_started: ProviderUtteranceStartedCallback | None = None,
        on_provider_endpoint: ProviderEndpointCallback | None = None,
        on_provider_final: ProviderFinalCallback | None = None,
        on_provider_final_ready: ProviderFinalNotificationCallback | None = None,
        voice_turn_factory: VoiceTurnFactory | None = None,
        provider_policy: AsrProviderPolicy | None = None,
    ) -> None:
        if not isinstance(config, AsrSessionConfig):
            raise TypeError("ASR_INVALID_CONFIG: config must be AsrSessionConfig")
        if not callable(worker_fn):
            raise TypeError("ASR_INVALID_CONFIG: worker_fn must be callable")
        if not callable(on_input_transcript) or not callable(on_connection_error):
            raise TypeError("ASR_INVALID_CONFIG: callbacks must be callable")
        if on_status_message is not None and not callable(on_status_message):
            raise TypeError("ASR_INVALID_CONFIG: status callback must be callable")
        if on_turn_endpointed is not None and not callable(on_turn_endpointed):
            raise TypeError("ASR_INVALID_CONFIG: endpoint callback must be callable")
        if on_provider_utterance_started is not None and not callable(
            on_provider_utterance_started
        ):
            raise TypeError(
                "ASR_INVALID_CONFIG: provider utterance-start callback must be callable"
            )
        if on_provider_endpoint is not None and not callable(on_provider_endpoint):
            raise TypeError(
                "ASR_INVALID_CONFIG: provider endpoint callback must be callable"
            )
        if on_provider_final is not None and not callable(on_provider_final):
            raise TypeError(
                "ASR_INVALID_CONFIG: provider final callback must be callable"
            )
        if on_provider_final_ready is not None and not callable(
            on_provider_final_ready
        ):
            raise TypeError(
                "ASR_INVALID_CONFIG: provider final-ready callback must be callable"
            )

        self._worker_fn = worker_fn
        self._api_key = api_key
        self._config = config
        self._on_input_transcript = on_input_transcript
        self._on_connection_error = on_connection_error
        self._on_status_message = on_status_message
        self._on_turn_endpointed = on_turn_endpointed
        self._on_provider_utterance_started = on_provider_utterance_started
        self._on_provider_endpoint = on_provider_endpoint
        self._on_provider_final = on_provider_final
        self._on_provider_final_ready = on_provider_final_ready
        self._voice_turn_factory = voice_turn_factory
        self._provider_policy = provider_policy
        self._voice_turn_adapter: _VoiceTurnAdapterProtocol | None = None
        self._voice_turn_watch_task: asyncio.Task[None] | None = None
        self._voice_turn_reset_task: asyncio.Task[None] | None = None

        self._state = _SessionState.NEW
        self._generation = 0
        self._buffer_epoch = 0
        self._utterance_id = 1
        self._utterance_has_audio = False
        self._input_sample_rate_hz: int | None = None
        self._resampler: soxr.ResampleStream | None = None
        self._active_utterance_keys: set[_UtteranceKey] = set()
        self._committed_utterance_keys: set[_UtteranceKey] = set()
        self._endpointed_turn_keys: set[_UtteranceKey] = set()
        self._utterance_order: deque[_UtteranceKey] = deque()
        self._pending_finals: dict[_UtteranceKey, _PendingFinal] = {}
        # Latest preview text per turn that is NOT the current ordered turn;
        # coalesced (one entry per key) and flushed by
        # _drain_ready_partials_locked when the turn becomes current.
        self._pending_partials: dict[_UtteranceKey, str] = {}
        self._provider_endpoints: dict[_UtteranceKey, ProviderEndpointNotification] = {}
        self._provider_started_audio_starts_16k: dict[
            _UtteranceKey,
            int | None,
        ] = {}
        self._provider_started_conflicted_keys: set[_UtteranceKey] = set()
        self._provider_started_settlements: dict[
            _UtteranceKey,
            asyncio.Future[bool],
        ] = {}
        self._provider_started_callback_tasks: dict[
            _UtteranceKey,
            asyncio.Task[None],
        ] = {}
        self._provider_started_retired_tasks: set[asyncio.Task[Any]] = set()
        self._provider_started_close_owner: asyncio.Task[Any] | None = None
        # A failed started callback is an identity/control failure. Keep the
        # key latched until the namespace is cleared so later endpoint,
        # partial, and final items cannot fall through the normal callback
        # path after their parent/child binding failed.
        self._provider_started_failed_keys: dict[_UtteranceKey, None] = {}
        self._provider_started_failed_namespace: tuple[int, int] | None = None
        self._provider_recovered_callback_items: deque[_CallbackItem] = deque()
        self._provider_boundary_tasks: dict[
            _UtteranceKey,
            asyncio.Task[bool | BoundarySettlement],
        ] = {}
        self._provider_boundary_deadlines: dict[_UtteranceKey, float] = {}
        self._provider_boundary_chain_namespace: tuple[int, int] | None = None
        self._provider_boundary_failed_namespace: tuple[int, int] | None = None
        self._provider_boundary_chain_tail: asyncio.Task[bool | BoundarySettlement] | None = None
        self._provider_boundary_chain_tasks: set[asyncio.Task[bool | BoundarySettlement]] = set()
        self._provider_boundary_retired_tasks: set[asyncio.Task[Any]] = set()
        self._logical_turn_id = 1
        self._physical_segment_audio_bytes = 0
        self._provider_wire_audio_bytes = 0
        self._provider_wire_epoch_base_bytes = 0
        # This cursor describes worker input, not provider wire encoding. Keep
        # it independent so a 24 kHz transport cannot skew canonical ranges.
        self._provider_canonical_audio_samples_16k = 0
        self._provider_canonical_epoch_base_samples_16k = 0
        self._segment_aggregator = SegmentAggregator()
        self._segment_aggregator.begin_turn(self._logical_turn_id)

        self._request_queue: asyncio.Queue[_AsrWorkerRequest] | None = None
        self._response_queue: asyncio.Queue[_AsrWorkerEvent] | None = None
        self._callback_queue: asyncio.Queue[_CallbackItem] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._response_task: asyncio.Task[None] | None = None
        self._callback_task: asyncio.Task[None] | None = None
        self._callback_close_waiter: asyncio.Task[Any] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._ready_future: asyncio.Future[None] | None = None

        self._operation_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._closing_event = asyncio.Event()
        self._callback_close_event = asyncio.Event()
        self._connection_error_reported = False

    @property
    def is_ready(self) -> bool:
        return self._state is _SessionState.READY

    @property
    def provider_wire_audio_ms(self) -> int:
        """Count audio that crossed the provider request boundary."""

        return self._provider_wire_audio_bytes * 1_000 // (16_000 * 2)

    async def connect(
        self,
        instructions: str = "",
        native_audio: bool = False,
    ) -> None:
        # Compatibility-only Omni arguments deliberately never reach a worker.
        _ = (instructions, native_audio)
        async with self._connect_lock:
            if self._state is _SessionState.READY:
                return
            if self._state is not _SessionState.NEW:
                raise RuntimeError(
                    f"ASR_SESSION_NOT_READY: cannot connect a {self._state.value} session"
                )

            self._state = _SessionState.CONNECTING
            self._request_queue = _AsrRequestQueue()
            self._response_queue = asyncio.Queue(maxsize=_RESPONSE_QUEUE_SIZE)
            self._callback_queue = asyncio.Queue(maxsize=_CALLBACK_QUEUE_SIZE)
            self._ready_future = asyncio.get_running_loop().create_future()
            self._callback_task = asyncio.create_task(
                self._dispatch_callbacks(), name="asr-callback-dispatch"
            )
            self._response_task = asyncio.create_task(
                self._consume_responses(), name="asr-response-consumer"
            )
            self._worker_task = asyncio.create_task(
                self._run_worker(), name="asr-worker"
            )

            await self._emit_status("ASR_CONNECTING")
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._ready_future),
                    timeout=_READY_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                if self._ready_future is not None and not self._ready_future.done():
                    self._ready_future.cancel()
                await self.close()
                raise
            except asyncio.TimeoutError as exc:
                if self._ready_future is not None and not self._ready_future.done():
                    self._ready_future.cancel()
                await self._fail(
                    "ASR_CONNECT_TIMEOUT",
                    "worker did not become ready within 10 seconds",
                )
                raise RuntimeError(
                    "ASR_CONNECT_TIMEOUT: worker did not become ready within 10 seconds"
                ) from exc
            except Exception:
                if self._state in (_SessionState.CLOSING, _SessionState.CLOSED):
                    raise
                if self._state is not _SessionState.FAILED:
                    await self._fail(
                        "ASR_WORKER_FAILED", "worker failed during connect"
                    )
                raise

            worker_task = self._worker_task
            if self._state is not _SessionState.READY or worker_task is None:
                raise RuntimeError("ASR_WORKER_FAILED: worker exited during connect")
            if worker_task.done():
                await self._fail(
                    "ASR_WORKER_FAILED",
                    "worker exited immediately after becoming ready",
                )
                raise RuntimeError("ASR_WORKER_FAILED: worker exited during connect")
            if self._voice_turn_factory is not None:
                adapter: _VoiceTurnAdapterProtocol | None = None
                try:
                    adapter = self._voice_turn_factory(self._handle_voice_turn_commit)
                    await adapter.start()
                except Exception as exc:
                    if adapter is not None:
                        await self._close_voice_turn_instance(
                            adapter,
                            context="after start failure",
                        )
                    await self._fail(
                        "ASR_VOICE_TURN_START_FAILED",
                        "voice turn adapter failed to start",
                    )
                    raise RuntimeError(
                        "ASR_VOICE_TURN_START_FAILED: "
                        "voice turn adapter failed to start"
                    ) from exc
                if self._state is not _SessionState.READY:
                    await self._close_voice_turn_instance(
                        adapter,
                        context="after concurrent session failure",
                    )
                    raise RuntimeError(
                        "ASR_WORKER_FAILED: worker failed while voice turn started"
                    )
                self._voice_turn_adapter = adapter
                self._voice_turn_watch_task = asyncio.create_task(
                    self._watch_voice_turn_failure(adapter),
                    name="asr-voice-turn-watch",
                )

    async def update_session(self, config: Mapping[str, Any]) -> None:
        if not isinstance(config, Mapping):
            raise TypeError("ASR_INVALID_CONFIG: session update must be a mapping")

        unknown_fields = (
            set(config)
            - {
                "language",
                "input_sample_rate_hz",
            }
            - _OMNI_ONLY_FIELDS
        )
        if unknown_fields:
            names = ", ".join(sorted(map(str, unknown_fields)))
            raise ValueError(f"ASR_INVALID_CONFIG: unknown session field(s): {names}")

        updates = {
            key: config[key]
            for key in ("language", "input_sample_rate_hz")
            if key in config
        }
        if not updates:
            return
        if self._state is not _SessionState.NEW:
            raise RuntimeError(
                "ASR_SESSION_CONFIG_LOCKED: session is already connecting"
            )
        self._config = replace(self._config, **updates)

    async def stream_audio(
        self,
        audio_chunk: bytes,
        *,
        sample_rate_hz: int | None = None,
    ) -> None:
        if not isinstance(audio_chunk, bytes):
            raise TypeError("ASR_INVALID_PCM: audio_chunk must be bytes")
        if not audio_chunk:
            return

        async with self._operation_lock:
            if self._state is not _SessionState.READY:
                raise RuntimeError("ASR_SESSION_NOT_READY: session is not ready")
            if len(audio_chunk) % 2:
                raise ValueError("ASR_INVALID_PCM: PCM16LE data has an odd byte length")

            effective_rate = (
                sample_rate_hz
                if sample_rate_hz is not None
                else self._config.input_sample_rate_hz
            )
            if effective_rate not in (16000, 48000):
                raise ValueError(
                    "ASR_INVALID_CONFIG: sample rate must be 16000 or 48000"
                )
            if len(audio_chunk) > effective_rate * 2:
                raise ValueError(
                    "ASR_AUDIO_CHUNK_TOO_LARGE: one chunk may contain at most one second"
                )
            if self._input_sample_rate_hz is None:
                self._input_sample_rate_hz = effective_rate
                self._resampler = self._make_resampler()
            elif effective_rate != self._input_sample_rate_hz:
                raise ValueError(
                    "ASR_SAMPLE_RATE_CHANGED: a session cannot mix input sample rates"
                )

            normalized_audio = self._convert_audio(audio_chunk)
            if normalized_audio and self._uses_segment_aggregation:
                await self._append_segmented_audio_locked(normalized_audio)
            elif normalized_audio:
                await self._enqueue_request(
                    _AsrWorkerRequest(
                        kind="audio",
                        generation=self._generation,
                        buffer_epoch=self._buffer_epoch,
                        utterance_id=self._utterance_id,
                        audio=normalized_audio,
                    )
                )
                self._provider_wire_audio_bytes += len(normalized_audio)
                self._provider_canonical_audio_samples_16k += len(normalized_audio) // 2
                await self._push_voice_turn_audio(
                    generation=self._generation,
                    buffer_epoch=self._buffer_epoch,
                    utterance_id=self._utterance_id,
                    pcm16=normalized_audio,
                )
            # Even if soxr is still buffering, valid input belongs to this turn.
            if not self._uses_segment_aggregation or not normalized_audio:
                self._utterance_has_audio = True

    async def signal_user_activity_end(self) -> None:
        async with self._operation_lock:
            if self._state is not _SessionState.READY:
                raise RuntimeError("ASR_SESSION_NOT_READY: session is not ready")
            if not self._utterance_has_audio and not (
                self._uses_segment_aggregation
                and self._segment_aggregator.has_segments(self._logical_turn_id)
            ):
                return
            await self._commit_current_utterance_locked()

    async def clear_audio_buffer(self) -> None:
        async with self._operation_lock:
            if self._state is not _SessionState.READY:
                raise RuntimeError("ASR_SESSION_NOT_READY: session is not ready")
            self._buffer_epoch += 1
            self._utterance_id += 1
            await self._cancel_provider_started_callbacks(clear_failures=True)
            self._utterance_has_audio = False
            self._active_utterance_keys.clear()
            self._committed_utterance_keys.clear()
            self._endpointed_turn_keys.clear()
            self._utterance_order.clear()
            self._pending_finals.clear()
            self._pending_partials.clear()
            self._provider_endpoints.clear()
            await self._cancel_provider_boundary_tasks()
            self._provider_wire_epoch_base_bytes = self._provider_wire_audio_bytes
            self._provider_canonical_epoch_base_samples_16k = (
                self._provider_canonical_audio_samples_16k
            )
            self._logical_turn_id += 1
            self._clear_segment_aggregation_state()
            self._reset_resampler()
            await self._enqueue_request(
                _AsrWorkerRequest(
                    kind="clear",
                    generation=self._generation,
                    buffer_epoch=self._buffer_epoch,
                    utterance_id=self._utterance_id,
                )
            )
            if self._voice_turn_adapter is not None:
                await self._reset_voice_turn_adapter(
                    self._voice_turn_adapter,
                    generation=self._generation,
                    buffer_epoch=self._buffer_epoch,
                    utterance_id=self._utterance_id,
                )

    async def close(self) -> None:
        current = asyncio.current_task()
        closes_from_provider_started = any(
            task is current for task in self._provider_started_callback_tasks.values()
        )
        if current is self._callback_task or closes_from_provider_started:
            # Provider-start callbacks run as owned child tasks so clear/close
            # can revoke their settlement without leaving the callback FIFO
            # blocked. Preserve the existing close-from-callback escape hatch
            # by excluding the dispatcher until this callback unwinds.
            self._callback_close_waiter = self._callback_task or current
            if closes_from_provider_started:
                self._provider_started_close_owner = current
            self._callback_close_event.set()

        if self._state is _SessionState.CLOSED:
            return

        close_task = self._close_task
        if close_task is None:
            # The state transition is synchronous so cancellation cannot leave
            # a READY session with a permanently-set closing event. Shielding
            # lets cleanup continue if the caller cancels its own wait.
            self._closing_event.set()
            self._state = _SessionState.CLOSING
            self._generation += 1
            if self._ready_future is not None and not self._ready_future.done():
                self._ready_future.set_exception(
                    RuntimeError("ASR_SESSION_NOT_READY: session was closed")
                )
            close_task = asyncio.create_task(
                self._close_impl(), name="asr-session-close"
            )
            self._close_task = close_task

        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        async with self._operation_lock:
            self._utterance_has_audio = False
            self._active_utterance_keys.clear()
            self._committed_utterance_keys.clear()
            self._endpointed_turn_keys.clear()
            self._utterance_order.clear()
            self._pending_finals.clear()
            self._pending_partials.clear()
            self._provider_endpoints.clear()
            await self._cancel_provider_started_callbacks(clear_failures=True)
            await self._cancel_provider_boundary_tasks()
            self._clear_segment_aggregation_state()
            self._reset_resampler()

            await self._unload_voice_turn_adapter(context="during close")

            if self._request_queue is not None:
                request = _AsrWorkerRequest(
                    kind="shutdown",
                    generation=self._generation,
                    buffer_epoch=self._buffer_epoch,
                    utterance_id=self._utterance_id,
                )
                try:
                    await asyncio.wait_for(
                        self._request_queue.put(request),
                        timeout=_WORKER_CLOSE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning("ASR shutdown command timed out")

            if self._worker_task is not None and not self._worker_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._worker_task),
                        timeout=_WORKER_CLOSE_TIMEOUT_SECONDS,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._worker_task.cancel()

            if self._callback_queue is not None:
                drain_task = asyncio.create_task(self._callback_queue.join())
                callback_close_task = asyncio.create_task(
                    self._callback_close_event.wait()
                )
                done, pending = await asyncio.wait(
                    {drain_task, callback_close_task},
                    timeout=_CALLBACK_DRAIN_TIMEOUT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    logger.warning("ASR callback drain timed out")
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            await self._shutdown()
            self._state = _SessionState.CLOSED
            await self._emit_status("ASR_CLOSED")

    async def _close_voice_turn_instance(
        self,
        adapter: _VoiceTurnAdapterProtocol,
        *,
        context: str,
    ) -> None:
        try:
            await adapter.close()
        except Exception:
            logger.exception("ASR voice turn adapter failed to close %s", context)

    async def _fail_voice_turn_operation(self, operation: str) -> RuntimeError:
        logger.warning("ASR voice turn %s failed", operation)
        await self._fail(
            "ASR_ENDPOINTING_FAILED",
            "required voice turn endpointing failed",
        )
        return RuntimeError(
            "ASR_ENDPOINTING_FAILED: required voice turn endpointing failed"
        )

    async def _push_voice_turn_audio(
        self,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
        pcm16: bytes,
    ) -> None:
        adapter = self._voice_turn_adapter
        if adapter is None:
            return
        try:
            await adapter.push_audio(
                generation=generation,
                buffer_epoch=buffer_epoch,
                utterance_id=utterance_id,
                pcm16=pcm16,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = await self._fail_voice_turn_operation("audio push")
            raise failure from exc

    async def _reset_voice_turn_adapter(
        self,
        adapter: _VoiceTurnAdapterProtocol,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        try:
            await adapter.reset(
                generation=generation,
                buffer_epoch=buffer_epoch,
                utterance_id=utterance_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = await self._fail_voice_turn_operation("reset")
            raise failure from exc

    def _schedule_voice_turn_reset(
        self,
        adapter: _VoiceTurnAdapterProtocol,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
        name: str,
    ) -> None:
        previous = self._voice_turn_reset_task

        async def run_reset() -> None:
            try:
                if previous is not None and previous is not asyncio.current_task():
                    await previous
                if (
                    adapter is not self._voice_turn_adapter
                    or self._state is not _SessionState.READY
                ):
                    return
                await self._reset_voice_turn_adapter(
                    adapter,
                    generation=generation,
                    buffer_epoch=buffer_epoch,
                    utterance_id=utterance_id,
                )
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                # _reset_voice_turn_adapter already moved the session to FAILED.
                return

        task = asyncio.create_task(run_reset(), name=name)
        self._voice_turn_reset_task = task

        def clear_finished_reset(finished: asyncio.Task[None]) -> None:
            if self._voice_turn_reset_task is finished:
                self._voice_turn_reset_task = None

        task.add_done_callback(clear_finished_reset)

    async def _unload_voice_turn_adapter(self, *, context: str) -> None:
        adapter, self._voice_turn_adapter = self._voice_turn_adapter, None
        watch_task, self._voice_turn_watch_task = (
            self._voice_turn_watch_task,
            None,
        )
        current_task = asyncio.current_task()
        reset_task, self._voice_turn_reset_task = (
            self._voice_turn_reset_task,
            None,
        )
        if reset_task is not None and reset_task is not current_task:
            try:
                await asyncio.wait_for(
                    asyncio.shield(reset_task),
                    timeout=_CALLBACK_DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                reset_task.cancel()
                await asyncio.gather(reset_task, return_exceptions=True)
        if watch_task is not None and watch_task is not current_task:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)
        if adapter is not None:
            await self._close_voice_turn_instance(adapter, context=context)

    async def _watch_voice_turn_failure(
        self,
        adapter: _VoiceTurnAdapterProtocol,
    ) -> None:
        try:
            failure = await adapter.wait_failure()
        except asyncio.CancelledError:
            return
        except Exception:
            failure = None
        if (
            adapter is not self._voice_turn_adapter
            or self._state is not _SessionState.READY
        ):
            return
        kind = getattr(failure, "kind", "runtime_error")
        stage = getattr(failure, "stage", "consumer")
        logger.warning(
            "ASR voice turn endpointing failed kind=%s stage=%s",
            kind if kind in ("unavailable", "runtime_error") else "runtime_error",
            (
                stage
                if stage in ("vad_load", "vad_feed", "smart_turn", "consumer")
                else "consumer"
            ),
        )
        await self._fail(
            "ASR_ENDPOINTING_FAILED",
            "required voice turn endpointing failed",
        )

    async def _handle_voice_turn_commit(
        self,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        async with self._operation_lock:
            if (
                self._state is not _SessionState.READY
                or generation != self._generation
                or buffer_epoch != self._buffer_epoch
                or utterance_id
                != (
                    self._logical_turn_id
                    if self._uses_segment_aggregation
                    else self._utterance_id
                )
                or (
                    not self._utterance_has_audio
                    and not self._segment_aggregator.has_segments(self._logical_turn_id)
                )
            ):
                return
            await self._notify_turn_endpointed_locked(
                (generation, buffer_epoch, utterance_id)
            )
            await self._commit_current_utterance_locked()

    async def _notify_turn_endpointed_locked(self, key: _UtteranceKey) -> None:
        """Publish one semantic seal event before transport commit or final."""

        if key in self._endpointed_turn_keys:
            return
        self._endpointed_turn_keys.add(key)
        callback = self._on_turn_endpointed
        if callback is None:
            return
        try:
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ASR turn endpoint callback failed")

    async def _commit_current_utterance_locked(self) -> None:
        if self._uses_segment_aggregation:
            await self._complete_segmented_logical_turn_locked()
            return
        generation = self._generation
        buffer_epoch = self._buffer_epoch
        utterance_id = self._utterance_id
        tail = self._flush_resampler()
        if tail:
            await self._enqueue_request(
                _AsrWorkerRequest(
                    kind="audio",
                    generation=generation,
                    buffer_epoch=buffer_epoch,
                    utterance_id=utterance_id,
                    audio=tail,
                )
            )
            self._provider_wire_audio_bytes += len(tail)
            self._provider_canonical_audio_samples_16k += len(tail) // 2
            await self._push_voice_turn_audio(
                generation=generation,
                buffer_epoch=buffer_epoch,
                utterance_id=utterance_id,
                pcm16=tail,
            )
        if self._config.endpointing_mode == "provider":
            self._utterance_has_audio = False
            self._reset_resampler()
            return
        await self._enqueue_request(
            _AsrWorkerRequest(
                kind="commit",
                generation=generation,
                buffer_epoch=buffer_epoch,
                utterance_id=utterance_id,
            )
        )
        self._utterance_id += 1
        self._utterance_has_audio = False
        self._reset_resampler()
        if self._voice_turn_adapter is not None:
            adapter = self._voice_turn_adapter
            next_generation = self._generation
            next_buffer_epoch = self._buffer_epoch
            next_utterance_id = self._utterance_id
            self._schedule_voice_turn_reset(
                adapter,
                generation=next_generation,
                buffer_epoch=next_buffer_epoch,
                utterance_id=next_utterance_id,
                name="asr-voice-turn-reset-after-commit",
            )
            # Let reset enqueue behind the audio that belongs to the completed
            # utterance without waiting for model/VAD work to drain. This keeps
            # explicit commit responsive while preserving FIFO identity order.
            await asyncio.sleep(0)

    @property
    def _uses_segment_aggregation(self) -> bool:
        policy = self._provider_policy
        return bool(
            policy is not None
            and policy.transport == "segmented"
            and policy.max_segment_ms is not None
        )

    @property
    def _segmented_max_audio_bytes(self) -> int:
        policy = self._provider_policy
        if policy is None or policy.max_segment_ms is None:
            return 0
        return (policy.max_segment_ms * 16_000 * 2 // 1_000) & ~1

    async def _append_segmented_audio_locked(self, audio: bytes) -> None:
        """Append PCM16 without allowing a physical request to exceed policy."""

        max_bytes = self._segmented_max_audio_bytes
        if max_bytes <= 0:
            raise RuntimeError("ASR_INVALID_CONFIG: segmented audio limit is invalid")
        offset = 0
        while offset < len(audio):
            remaining = max_bytes - self._physical_segment_audio_bytes
            if remaining <= 0:
                await self._commit_physical_segment_locked(logical_complete=False)
                remaining = max_bytes
            part_size = min(len(audio) - offset, remaining) & ~1
            if part_size <= 0:
                raise ValueError(
                    "ASR_INVALID_PCM: segmented PCM must be 2-byte aligned"
                )
            part = audio[offset : offset + part_size]
            await self._enqueue_request(
                _AsrWorkerRequest(
                    kind="audio",
                    generation=self._generation,
                    buffer_epoch=self._buffer_epoch,
                    utterance_id=self._utterance_id,
                    audio=part,
                )
            )
            await self._push_voice_turn_audio(
                generation=self._generation,
                buffer_epoch=self._buffer_epoch,
                utterance_id=self._logical_turn_id,
                pcm16=part,
            )
            self._provider_canonical_audio_samples_16k += part_size // 2
            self._physical_segment_audio_bytes += part_size
            self._utterance_has_audio = True
            offset += part_size
            if self._physical_segment_audio_bytes == max_bytes:
                await self._commit_physical_segment_locked(logical_complete=False)

    async def _commit_physical_segment_locked(
        self,
        *,
        logical_complete: bool,
    ) -> None:
        logical_turn_id = self._logical_turn_id
        generation = self._generation
        buffer_epoch = self._buffer_epoch
        utterance_id = self._utterance_id
        if logical_complete:
            tail = self._flush_resampler()
            if tail:
                await self._append_segmented_audio_locked(tail)
        if not self._utterance_has_audio:
            if logical_complete:
                self._segment_aggregator.complete_turn(logical_turn_id)
            return
        utterance_id = self._utterance_id
        key = (generation, buffer_epoch, utterance_id)
        await self._enqueue_request(
            _AsrWorkerRequest(
                kind="commit",
                generation=generation,
                buffer_epoch=buffer_epoch,
                utterance_id=utterance_id,
            )
        )
        self._provider_wire_audio_bytes += self._physical_segment_audio_bytes
        if not self._segment_aggregator.register_segment(logical_turn_id, key):
            raise RuntimeError("ASR_SEGMENT_STATE_INVALID: duplicate physical segment")
        if logical_complete:
            self._segment_aggregator.complete_turn(logical_turn_id)
            self._reset_resampler()
        self._utterance_id += 1
        self._utterance_has_audio = False
        self._physical_segment_audio_bytes = 0

    async def _complete_segmented_logical_turn_locked(self) -> None:
        completed_turn_id = self._logical_turn_id
        await self._commit_physical_segment_locked(logical_complete=True)
        if not self._segment_aggregator.has_segments(completed_turn_id):
            return
        self._segment_aggregator.complete_turn(completed_turn_id)
        ready_texts = self._collect_ready_segmented_transcripts_locked()
        ready_items: list[_CallbackItem] = [
            self._logical_final_callback_item(
                ready_text,
                generation=self._generation,
                buffer_epoch=self._buffer_epoch,
            )
            for ready_text in ready_texts
        ]
        ready_items.extend(self._drain_ready_partials_locked())
        if ready_items:
            assert self._callback_queue is not None
            for ready_item in ready_items:
                await self._callback_queue.put(ready_item)
        self._logical_turn_id += 1
        self._segment_aggregator.begin_turn(self._logical_turn_id)
        if self._voice_turn_adapter is None:
            return
        adapter = self._voice_turn_adapter
        next_generation = self._generation
        next_buffer_epoch = self._buffer_epoch
        next_logical_turn_id = self._logical_turn_id
        self._schedule_voice_turn_reset(
            adapter,
            generation=next_generation,
            buffer_epoch=next_buffer_epoch,
            utterance_id=next_logical_turn_id,
            name="asr-voice-turn-reset-after-logical-commit",
        )
        await asyncio.sleep(0)

    def _collect_ready_segmented_transcripts_locked(self) -> list[str]:
        ready_transcripts = self._segment_aggregator.collect_ready()
        for transcript in ready_transcripts:
            for key in transcript.segment_ids:
                self._active_utterance_keys.discard(key)
                self._committed_utterance_keys.discard(key)
                self._pending_finals.pop(key, None)
                self._pending_partials.pop(key, None)
                try:
                    self._utterance_order.remove(key)
                except ValueError:
                    pass
        return [transcript.text for transcript in ready_transcripts]

    @staticmethod
    def _logical_final_callback_item(
        text: str,
        *,
        generation: int,
        buffer_epoch: int,
    ) -> _CallbackItem:
        """Stamp one complete logical transcript at its first ready instant."""

        received_at = time.monotonic()
        return _CallbackItem(
            text=text,
            generation=generation,
            buffer_epoch=buffer_epoch,
            final_received_at=received_at,
            final_admission_deadline=(
                received_at + _PROVIDER_FINAL_ADMISSION_TIMEOUT_SECONDS
            ),
        )

    def _drain_ready_partials_locked(self) -> list[_CallbackItem]:
        """Flush queued previews whose turn became current; drop stale ones.

        Mirrors the deferred-endpoint handling: a queued partial belongs to
        a turn behind the head of ``_utterance_order``. Once the head
        advances, the newly-current turn's latest preview may flow, while
        previews for turns that already delivered a final (or were
        invalidated) must never resurface after that final.
        """

        if not self._pending_partials:
            return []
        for stale_key in [
            queued_key
            for queued_key in self._pending_partials
            if queued_key not in self._active_utterance_keys
            and queued_key not in self._committed_utterance_keys
        ]:
            del self._pending_partials[stale_key]
        if self._utterance_order:
            head_key = self._utterance_order[0]
            ready_keys = [head_key] if head_key in self._pending_partials else []
        else:
            # No turn awaits a final anymore; any surviving preview belongs
            # to a live utterance that has not entered the order yet.
            ready_keys = sorted(self._pending_partials)
        return [
            _CallbackItem(
                text=self._pending_partials.pop(ready_key),
                generation=ready_key[0],
                buffer_epoch=ready_key[1],
                utterance_id=ready_key[2],
                kind="partial",
            )
            for ready_key in ready_keys
        ]

    def _drain_ready_provider_finals_locked(self) -> list[_CallbackItem]:
        """Advance ordered Provider finals after receipt or exact-key retirement."""

        ready_items: list[_CallbackItem] = []
        while (
            self._utterance_order and self._utterance_order[0] in self._pending_finals
        ):
            ready_key = self._utterance_order.popleft()
            pending_final = self._pending_finals.pop(ready_key)
            if (
                self._config.endpointing_mode == "provider"
                and ready_key not in self._endpointed_turn_keys
            ):
                self._endpointed_turn_keys.add(ready_key)
                if (
                    self._on_provider_endpoint is not None
                    or self._on_turn_endpointed is not None
                ):
                    ready_items.append(
                        _CallbackItem(
                            text="",
                            generation=ready_key[0],
                            buffer_epoch=ready_key[1],
                            utterance_id=ready_key[2],
                            kind="endpoint",
                            provider_endpoint=(
                                self._take_ordered_provider_endpoint_locked(ready_key)
                            ),
                            final_received_at=pending_final.received_at,
                            final_admission_deadline=(pending_final.admission_deadline),
                        )
                    )
                else:
                    self._provider_endpoints.pop(ready_key, None)
            ready_items.append(
                _CallbackItem(
                    text=pending_final.text,
                    generation=ready_key[0],
                    buffer_epoch=ready_key[1],
                    utterance_id=ready_key[2],
                    final_received_at=pending_final.received_at,
                    final_admission_deadline=pending_final.admission_deadline,
                )
            )
            self._active_utterance_keys.discard(ready_key)
            self._committed_utterance_keys.discard(ready_key)
        return ready_items

    def _clear_segment_aggregation_state(self) -> None:
        self._physical_segment_audio_bytes = 0
        self._segment_aggregator.clear(next_turn_id=self._logical_turn_id)
        self._segment_aggregator.begin_turn(self._logical_turn_id)

    async def _run_worker(self) -> None:
        assert self._request_queue is not None
        assert self._response_queue is not None
        try:
            await self._worker_fn(
                self._request_queue,
                self._response_queue,
                self._api_key,
                self._config,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._state in (
                _SessionState.CLOSING,
                _SessionState.CLOSED,
                _SessionState.FAILED,
            ):
                return
            await self._response_queue.put(
                _AsrWorkerEvent(
                    kind="error",
                    generation=self._generation,
                    buffer_epoch=self._buffer_epoch,
                    utterance_id=self._utterance_id,
                    error_code="ASR_WORKER_FAILED",
                    error_message="worker raised an unexpected exception",
                )
            )
        else:
            # Workers normally emit ``closed`` themselves. This synthetic
            # event makes an accidental bare return terminal as well; a
            # duplicate closed event during shutdown is harmless.
            await self._response_queue.put(
                _AsrWorkerEvent(kind="closed", generation=self._generation)
            )

    async def _consume_responses(self) -> None:
        assert self._response_queue is not None
        while True:
            event = await self._response_queue.get()
            try:
                should_stop = await self._handle_event(event)
            finally:
                self._response_queue.task_done()
            if should_stop:
                return

    @staticmethod
    def _unknown_provider_endpoint(
        key: _UtteranceKey,
        *,
        phase: Literal["boundary", "ordered"],
    ) -> ProviderEndpointNotification:
        return ProviderEndpointNotification(
            phase=phase,
            generation=key[0],
            buffer_epoch=key[1],
            utterance_id=key[2],
            boundary_quality="unknown",
            audio_range=None,
        )

    def _record_provider_endpoint_locked(
        self,
        event: _AsrWorkerEvent,
        key: _UtteranceKey,
    ) -> ProviderEndpointNotification | None:
        """Validate one epoch-local worker range and record monotonic authority."""

        local_range = event.audio_range
        canonical_range: ProviderAudioRange | None = None
        if event.boundary_quality == "exact" and isinstance(
            local_range, ProviderAudioRange
        ):
            epoch_base_samples = self._provider_canonical_epoch_base_samples_16k
            epoch_canonical_samples = (
                self._provider_canonical_audio_samples_16k
                - self._provider_canonical_epoch_base_samples_16k
            )
            if (
                0
                <= local_range.start_sample_16k
                < local_range.end_sample_16k
                <= epoch_canonical_samples
            ):
                canonical_range = ProviderAudioRange(
                    start_sample_16k=(
                        epoch_base_samples + local_range.start_sample_16k
                    ),
                    end_sample_16k=(epoch_base_samples + local_range.end_sample_16k),
                )

        if canonical_range is None:
            incoming = self._unknown_provider_endpoint(key, phase="boundary")
        else:
            incoming = ProviderEndpointNotification(
                phase="boundary",
                generation=key[0],
                buffer_epoch=key[1],
                utterance_id=key[2],
                boundary_quality="exact",
                audio_range=canonical_range,
            )

        existing = self._provider_endpoints.get(key)
        if existing is None:
            self._provider_endpoints[key] = incoming
            return incoming
        if existing.boundary_quality == "unknown":
            return None
        if (
            incoming.boundary_quality == "exact"
            and incoming.audio_range == existing.audio_range
        ):
            return None

        invalidated = self._unknown_provider_endpoint(key, phase="boundary")
        self._provider_endpoints[key] = invalidated
        return invalidated

    def _record_provider_started_locked(
        self,
        event: _AsrWorkerEvent,
        key: _UtteranceKey,
    ) -> ProviderUtteranceStartedNotification | None:
        """Record monotonic canonical start authority for one Provider key."""

        local_start = event.audio_start_sample_16k
        epoch_base = self._provider_canonical_epoch_base_samples_16k
        canonical_start = (
            epoch_base + local_start
            if (
                isinstance(local_start, int)
                and not isinstance(local_start, bool)
                and local_start >= 0
            )
            else None
        )

        if key not in self._provider_started_audio_starts_16k:
            self._provider_started_audio_starts_16k[key] = canonical_start
            return ProviderUtteranceStartedNotification(
                generation=key[0],
                buffer_epoch=key[1],
                utterance_id=key[2],
                audio_start_sample_16k=canonical_start,
            )

        if key in self._provider_started_conflicted_keys:
            return None
        if self._provider_started_audio_starts_16k[key] == canonical_start:
            return None

        # Start authority is monotonic: a conflict can only revoke proof. It
        # must never replace an earlier start with a different exact value.
        self._provider_started_audio_starts_16k[key] = None
        self._provider_started_conflicted_keys.add(key)
        return ProviderUtteranceStartedNotification(
            generation=key[0],
            buffer_epoch=key[1],
            utterance_id=key[2],
            audio_start_sample_16k=None,
        )

    def _poison_conflicting_provider_start_locked(
        self,
        key: _UtteranceKey,
    ) -> ProviderEndpointNotification | None:
        """Revoke speaker proof without rebinding the Provider text key."""

        existing = self._provider_endpoints.get(key)
        if existing is not None and existing.boundary_quality == "unknown":
            return None
        poisoned = self._unknown_provider_endpoint(key, phase="boundary")
        self._provider_endpoints[key] = poisoned
        return poisoned

    def _ensure_unknown_provider_endpoint_locked(
        self,
        key: _UtteranceKey,
    ) -> ProviderEndpointNotification | None:
        if key in self._provider_endpoints:
            return None
        notification = self._unknown_provider_endpoint(key, phase="boundary")
        self._provider_endpoints[key] = notification
        return notification

    def _take_ordered_provider_endpoint_locked(
        self,
        key: _UtteranceKey,
    ) -> ProviderEndpointNotification:
        self._provider_started_audio_starts_16k.pop(key, None)
        self._provider_started_conflicted_keys.discard(key)
        boundary = self._provider_endpoints.pop(key, None)
        if boundary is None or boundary.boundary_quality == "unknown":
            return self._unknown_provider_endpoint(key, phase="ordered")
        return ProviderEndpointNotification(
            phase="ordered",
            generation=key[0],
            buffer_epoch=key[1],
            utterance_id=key[2],
            boundary_quality="exact",
            audio_range=boundary.audio_range,
        )

    async def _emit_provider_boundary(
        self,
        notification: ProviderEndpointNotification,
        *,
        deadline: float | None = None,
        settlement: BoundarySettlement | None = None,
    ) -> bool:
        callback = self._on_provider_endpoint
        if callback is None:
            return True
        callback_task: asyncio.Task[None] | None = None
        try:
            if deadline is None:
                await callback(notification)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                callback_task = asyncio.create_task(
                    settlement.invoke(callback, notification)
                    if settlement is not None else callback(notification),
                    name="asr-provider-boundary-settlement",
                )
                done, _ = await asyncio.wait(
                    {callback_task},
                    timeout=remaining,
                )
                if callback_task not in done:
                    self._retire_provider_boundary_task(callback_task)
                    return False
                await callback_task
                if settlement is not None and (
                    settlement.completed_at is None
                    or settlement.completed_at > deadline
                ):
                    return False
        except asyncio.CancelledError:
            if callback_task is not None and not callback_task.done():
                self._retire_provider_boundary_task(callback_task)
            raise
        except Exception:
            logger.exception("ASR provider boundary callback failed")
            return False
        return True

    def _schedule_provider_boundary_callback(
        self,
        key: _UtteranceKey,
        notification: ProviderEndpointNotification,
    ) -> None:
        """Append one control to the current Provider namespace chain."""

        if self._on_provider_endpoint is None:
            return
        if (
            key in self._provider_started_failed_keys
            or self._provider_started_failed_namespace == key[:2]
        ):
            return

        async def wait_for_started(
            started_key: _UtteranceKey,
            settlement: asyncio.Future[bool] | None,
            *,
            deadline: float,
        ) -> bool:
            if settlement is None:
                return bool(
                    self._state is _SessionState.READY
                    and started_key[0] == self._generation
                    and started_key[1] == self._buffer_epoch
                    and started_key not in self._provider_started_failed_keys
                    and self._provider_started_failed_namespace != started_key[:2]
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                started_ready = await asyncio.wait_for(
                    asyncio.shield(settlement),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return False
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return False
            return bool(
                started_ready
                and self._state is _SessionState.READY
                and started_key[0] == self._generation
                and started_key[1] == self._buffer_epoch
            )

        namespace = key[:2]
        if (
            self._provider_boundary_failed_namespace is not None
            and self._provider_boundary_failed_namespace != namespace
        ):
            self._provider_boundary_failed_namespace = None
        if (
            self._provider_boundary_chain_namespace is not None
            and self._provider_boundary_chain_namespace != namespace
        ):
            self._revoke_provider_boundary_chain()
        self._provider_boundary_chain_namespace = namespace
        if self._provider_boundary_failed_namespace == namespace:
            self._provider_endpoints[key] = self._unknown_provider_endpoint(
                key,
                phase="boundary",
            )
            return

        existing = self._provider_boundary_tasks.get(key)
        if (
            existing is None
            and len(self._provider_boundary_tasks) >= _MAX_PROVIDER_BOUNDARY_TASKS
        ):
            pending_keys = tuple(self._provider_boundary_tasks)
            pending_started = {
                pending_key: self._provider_started_settlements.get(pending_key)
                for pending_key in pending_keys
            }
            self._revoke_provider_boundary_chain()
            self._provider_boundary_failed_namespace = namespace
            notifications: list[ProviderEndpointNotification] = []
            for pending_key in pending_keys:
                unknown = self._unknown_provider_endpoint(
                    pending_key,
                    phase="boundary",
                )
                self._provider_endpoints[pending_key] = unknown
                notifications.append(unknown)
            notification = self._unknown_provider_endpoint(
                key,
                phase="boundary",
            )
            self._provider_endpoints[key] = notification

            async def emit_overflow_unknowns() -> bool:
                succeeded = True
                deadline = (
                    time.monotonic() + _PROVIDER_BOUNDARY_CALLBACK_TIMEOUT_SECONDS
                )
                for pending_key, unknown in zip(
                    pending_keys,
                    notifications,
                    strict=True,
                ):
                    if not await wait_for_started(
                        pending_key,
                        pending_started[pending_key],
                        deadline=deadline,
                    ):
                        succeeded = False
                        continue
                    succeeded = (
                        await self._emit_provider_boundary(
                            unknown,
                            deadline=deadline,
                        )
                        and succeeded
                    )
                return succeeded

            task = asyncio.create_task(
                emit_overflow_unknowns(),
                name="asr-provider-boundary-overflow",
            )
            self._provider_boundary_chain_namespace = namespace
            self._provider_boundary_chain_tail = task
            self._provider_boundary_chain_tasks.add(task)
            for pending_key in pending_keys:
                self._provider_boundary_tasks[pending_key] = task
            self._track_provider_boundary_chain_task(task)
            return

        predecessor = self._provider_boundary_chain_tail
        started_settlement = self._provider_started_settlements.get(key)
        deadline = time.monotonic() + _PROVIDER_BOUNDARY_CALLBACK_TIMEOUT_SECONDS
        settlement = BoundarySettlement.create(self, key, deadline)
        settlement.record("provider_boundary_scheduled")

        async def emit_after_predecessor() -> BoundarySettlement:
            try:
                if predecessor is not None:
                    # Chain waiting shares the same execution budget.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        settlement.outcome = "predecessor_timeout"
                        return settlement
                    try:
                        await asyncio.wait_for(asyncio.shield(predecessor), timeout=remaining)
                    except asyncio.CancelledError:
                        # A predecessor can lose its optional speaker proof
                        # without cancelling this independently keyed boundary.
                        # Clear/close and chain revocation still cancel us.
                        current = asyncio.current_task()
                        if current is not None and current.cancelling():
                            raise
                if not await wait_for_started(key, started_settlement, deadline=deadline):
                    settlement.outcome = "started_unavailable"
                    return settlement
                if (
                    asyncio.current_task() not in self._provider_boundary_chain_tasks
                    or key in self._provider_started_failed_keys
                    or self._provider_started_failed_namespace == key[:2]
                ):
                    settlement.outcome = "revoked_or_stale"
                    return settlement
                # The chain owns queued controls; the per-key map owns only
                # the latest final receipt. A later unknown must still follow
                # its earlier exact control in order, even for the same key.
                succeeded = await self._emit_provider_boundary(
                    notification, deadline=deadline, settlement=settlement,
                )
                settlement.outcome = (
                    "completed" if succeeded else
                    "callback_failed" if settlement.callback_outcome == "failed" else
                    "callback_cancelled" if settlement.callback_outcome == "cancelled" else
                    "callback_timeout"
                )
                return settlement
            except asyncio.TimeoutError:
                settlement.outcome = "predecessor_timeout"
                return settlement
            except asyncio.CancelledError:
                settlement.outcome = "cancelled"
                raise
            finally:
                settlement.settled_at = time.monotonic()
                settlement.record("provider_boundary_settled")

        task = asyncio.create_task(
            emit_after_predecessor(),
            name="asr-provider-boundary-callback",
        )
        self._provider_boundary_tasks[key] = task
        self._provider_boundary_deadlines[key] = deadline
        self._provider_boundary_chain_tail = task
        self._provider_boundary_chain_tasks.add(task)
        self._track_provider_boundary_chain_task(task)

    def _track_provider_boundary_chain_task(
        self,
        task: asyncio.Task[bool | BoundarySettlement],
    ) -> None:
        def release(done: asyncio.Task[bool | BoundarySettlement]) -> None:
            self._provider_boundary_chain_tasks.discard(done)
            if self._provider_boundary_chain_tail is done:
                self._provider_boundary_chain_tail = None
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(release)

    def _revoke_provider_boundary_chain(self) -> None:
        """Fail open every queued control in one physical namespace."""

        for pending_key in tuple(self._provider_boundary_tasks):
            if pending_key in self._provider_endpoints:
                self._provider_endpoints[pending_key] = self._unknown_provider_endpoint(
                    pending_key,
                    phase="boundary",
                )
        tasks = tuple(self._provider_boundary_chain_tasks)
        self._provider_boundary_tasks.clear()
        self._provider_boundary_deadlines.clear()
        self._provider_boundary_chain_tasks.clear()
        self._provider_boundary_chain_tail = None
        self._provider_boundary_chain_namespace = None
        for task in tasks:
            self._retire_provider_boundary_task(task)

    async def _wait_provider_boundary_callback(
        self,
        key: _UtteranceKey,
        *,
        not_after: float | None = None,
        final_received_at: float | None = None,
    ) -> bool:
        task = self._provider_boundary_tasks.get(key)
        deadline = self._provider_boundary_deadlines.get(key)
        if task is None or deadline is None:
            return False
        result = None
        disposition = "unavailable"
        try:
            if task.done():
                # Consume completion evidence before considering a wait budget.
                result = task.result()
            else:
                wait_deadline = min(deadline, not_after) if not_after is not None else deadline
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            current = bool(
                self._provider_boundary_tasks.get(key) is task
                and self._state is _SessionState.READY
                and key[:2] == (self._generation, self._buffer_epoch)
                and key not in self._provider_started_failed_keys
                and self._provider_started_failed_namespace != key[:2]
            )
            if not current:
                disposition = "revoked_or_stale"
                return False
            if not_after is not None and time.monotonic() > not_after:
                disposition = "final_deadline_expired"
                return False
            # Overflow tasks and bare booleans are not exact completion proofs.
            accepted = isinstance(result, BoundarySettlement) and bool(result)
            disposition = "accepted" if accepted else "callback_unavailable"
            return accepted
        except asyncio.TimeoutError:
            disposition = "wait_timeout"
            if self._provider_boundary_tasks.get(key) is task:
                self._revoke_provider_boundary_chain()
            return False
        except asyncio.CancelledError:
            disposition = "cancelled"
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            # Clear, close, conflict, and bounded overflow all revoke optional
            # speaker authority. The ordered unknown endpoint remains valid.
            return False
        finally:
            observation = result
            if not isinstance(observation, BoundarySettlement):
                # A cancelled/pending task has no completed receipt to read.
                # Report absence explicitly; never invent completion times.
                observation = BoundarySettlement.create(self, key, deadline)
                observation.scheduled_at = None
                observation.callback_outcome = "unknown"
                observation.outcome = "no_completed_receipt"
            observation.record("provider_boundary_consumed", consumed_at=time.monotonic(),
                final_received_at=final_received_at, disposition=disposition)
            if self._provider_boundary_tasks.get(key) is task:
                self._provider_boundary_tasks.pop(key, None)
                self._provider_boundary_deadlines.pop(key, None)

    def _retire_provider_boundary_task(
        self,
        task: asyncio.Task[Any],
    ) -> None:
        """Cancel optional speaker control without blocking transcript delivery."""

        if task.done():
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass
            return
        task.cancel()
        self._provider_boundary_retired_tasks.add(task)

        def reap(done: asyncio.Task[Any]) -> None:
            self._provider_boundary_retired_tasks.discard(done)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(reap)

    async def _cancel_provider_boundary_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(
            {
                task
                for task in (
                    *self._provider_boundary_tasks.values(),
                    *self._provider_boundary_chain_tasks,
                    *self._provider_boundary_retired_tasks,
                )
                if task is not None
                if task is not current and not task.done()
            }
        )
        self._provider_boundary_tasks.clear()
        self._provider_boundary_deadlines.clear()
        self._provider_boundary_chain_tasks.clear()
        self._provider_boundary_chain_tail = None
        self._provider_boundary_chain_namespace = None
        self._provider_boundary_failed_namespace = None
        for task in tasks:
            self._retire_provider_boundary_task(task)
        if tasks:
            # Well-behaved callbacks observe cancellation immediately. A
            # third-party callback may suppress CancelledError; it must not
            # hold clear/close or the ordered transcript FIFO indefinitely.
            _done, pending = await asyncio.wait(
                tasks,
                timeout=_PROVIDER_BOUNDARY_CALLBACK_TIMEOUT_SECONDS,
            )
            for task in pending:
                task.cancel()

    def _latch_failed_provider_started_key(self, key: _UtteranceKey) -> None:
        if self._provider_started_failed_namespace == key[:2]:
            return
        if len(self._provider_started_failed_keys) >= (
            _PROVIDER_STARTED_TOMBSTONE_LIMIT
        ):
            # Do not evict an exact tombstone and accidentally re-admit a late
            # duplicate started event. Pathological overflow fails the bounded
            # namespace closed until clear/reset advances the epoch.
            self._provider_started_failed_namespace = key[:2]
            return
        self._provider_started_failed_keys[key] = None

    def _flush_recovered_provider_callbacks(self) -> None:
        queue = self._callback_queue
        if queue is None:
            self._provider_recovered_callback_items.clear()
            return
        while self._provider_recovered_callback_items and not queue.full():
            queue.put_nowait(self._provider_recovered_callback_items.popleft())

    async def _retire_failed_provider_started_key(
        self,
        key: _UtteranceKey,
        settlement: asyncio.Future[bool],
    ) -> None:
        """Retire only the failed key and release ordered successors."""

        if self._provider_started_settlements.get(key) is not settlement:
            return
        self._latch_failed_provider_started_key(key)
        async with self._operation_lock:
            if (
                self._provider_started_settlements.get(key) is not settlement
                or self._state is not _SessionState.READY
                or key[0] != self._generation
                or key[1] != self._buffer_epoch
            ):
                return
            self._active_utterance_keys.discard(key)
            self._committed_utterance_keys.discard(key)
            self._endpointed_turn_keys.discard(key)
            self._pending_finals.pop(key, None)
            self._pending_partials.pop(key, None)
            self._provider_endpoints.pop(key, None)
            self._provider_started_audio_starts_16k.pop(key, None)
            self._provider_started_conflicted_keys.discard(key)
            try:
                self._utterance_order.remove(key)
            except ValueError:
                pass
            ready_items = self._drain_ready_provider_finals_locked()
            ready_items.extend(self._drain_ready_partials_locked())
        self._provider_recovered_callback_items.extend(ready_items)
        self._flush_recovered_provider_callbacks()

    def _settle_provider_started(
        self,
        key: _UtteranceKey,
        settlement: asyncio.Future[bool],
        *,
        succeeded: bool,
    ) -> bool:
        """Commit one started result only while this Future owns the key."""

        current = self._provider_started_settlements.get(key)
        if current is not settlement:
            return False
        self._provider_started_settlements.pop(key, None)
        if succeeded:
            self._provider_started_failed_keys.pop(key, None)
        else:
            self._latch_failed_provider_started_key(key)
        if not settlement.done():
            settlement.set_result(succeeded)
        return succeeded

    def _retire_provider_started_callback_task(
        self,
        task: asyncio.Task[Any],
        *,
        cancel: bool = True,
    ) -> None:
        """Cancel and reap a revoked started callback without blocking FIFO."""

        if task.done():
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass
            return
        if cancel:
            task.cancel()
        if task in self._provider_started_retired_tasks:
            return
        self._provider_started_retired_tasks.add(task)

        def reap(done: asyncio.Task[Any]) -> None:
            self._provider_started_retired_tasks.discard(done)
            if self._provider_started_close_owner is done:
                self._provider_started_close_owner = None
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(reap)

    async def _cancel_provider_started_callbacks(
        self,
        *,
        clear_failures: bool,
    ) -> None:
        """Revoke all started settlements and boundedly retire owned callbacks."""

        current = asyncio.current_task()
        settlements = tuple(self._provider_started_settlements.values())
        self._provider_started_settlements.clear()
        for settlement in settlements:
            if not settlement.done():
                # Boundary waiters and the callback FIFO share this Future;
                # False wakes both while preserving fail-closed semantics.
                settlement.set_result(False)

        tasks = tuple(
            {
                task
                for task in (
                    *self._provider_started_callback_tasks.values(),
                    *self._provider_started_retired_tasks,
                )
                if task is not current
                and task is not self._provider_started_close_owner
                and not task.done()
            }
        )
        self._provider_started_callback_tasks.clear()
        for task in tasks:
            self._retire_provider_started_callback_task(task)
        if tasks:
            _done, pending = await asyncio.wait(
                tasks,
                timeout=_PROVIDER_BOUNDARY_CALLBACK_TIMEOUT_SECONDS,
            )
            for task in pending:
                task.cancel()
        if clear_failures:
            self._provider_started_failed_keys.clear()
            self._provider_started_failed_namespace = None
            self._provider_started_audio_starts_16k.clear()
            self._provider_started_conflicted_keys.clear()
            self._provider_recovered_callback_items.clear()

    async def _dispatch_callbacks(self) -> None:
        assert self._callback_queue is not None
        while True:
            item = await self._callback_queue.get()
            self._flush_recovered_provider_callbacks()
            item_key = (
                (item.generation, item.buffer_epoch, item.utterance_id)
                if item.utterance_id is not None
                else None
            )
            started_key = item_key if item.kind == "provider_started" else None
            started_settlement = (
                self._provider_started_settlements.get(started_key)
                if started_key is not None
                else None
            )
            started_succeeded = False
            started_callback_task: (
                asyncio.Task[ProviderStartedSettlement | None] | None
            ) = None
            try:
                if (
                    item.generation == self._generation
                    and item.buffer_epoch == self._buffer_epoch
                    and (
                        item.kind == "provider_started"
                        or (
                            item_key not in self._provider_started_failed_keys
                            and self._provider_started_failed_namespace
                            != (item.generation, item.buffer_epoch)
                        )
                    )
                ):
                    if item.kind == "provider_started":
                        provider_started_callback = self._on_provider_utterance_started
                        if (
                            provider_started_callback is not None
                            and item.provider_started is not None
                            and started_key is not None
                            and (
                                started_settlement is None
                                or self._provider_started_settlements.get(started_key)
                                is started_settlement
                            )
                        ):
                            started_callback_task = asyncio.create_task(
                                provider_started_callback(item.provider_started),
                                name="asr-provider-started-settlement",
                            )
                            self._provider_started_callback_tasks[started_key] = (
                                started_callback_task
                            )
                            if started_settlement is not None:
                                done, _ = await asyncio.wait(
                                    {started_callback_task, started_settlement},
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                if (
                                    started_settlement in done
                                    and not started_settlement.result()
                                ):
                                    self._retire_provider_started_callback_task(
                                        started_callback_task,
                                        cancel=(
                                            started_callback_task
                                            is not self._provider_started_close_owner
                                        ),
                                    )
                                    continue
                            callback_outcome = await started_callback_task
                            if callback_outcome is not None and not isinstance(
                                callback_outcome,
                                ProviderStartedSettlement,
                            ):
                                raise TypeError(
                                    "ASR_PROVIDER_STARTED_SETTLEMENT_INVALID"
                                )
                            identity_succeeded = callback_outcome in {
                                None,
                                ProviderStartedSettlement.BOUND_EXACT_PENDING,
                                ProviderStartedSettlement.BOUND_SPEAKER_UNAVAILABLE,
                            }
                            started_succeeded = bool(
                                identity_succeeded
                                and self._state is _SessionState.READY
                                and item.generation == self._generation
                                and item.buffer_epoch == self._buffer_epoch
                                and (
                                    started_settlement is None
                                    or self._provider_started_settlements.get(
                                        started_key
                                    )
                                    is started_settlement
                                )
                            )
                    elif item.kind == "endpoint":
                        provider_callback = self._on_provider_endpoint
                        if (
                            provider_callback is not None
                            and item.provider_endpoint is not None
                        ):
                            boundary_ready = False
                            if item.utterance_id is not None:
                                boundary_ready = (
                                    await self._wait_provider_boundary_callback(
                                        (
                                            item.generation,
                                            item.buffer_epoch,
                                            item.utterance_id,
                                        ),
                                        not_after=item.final_admission_deadline,
                                        final_received_at=item.final_received_at,
                                    )
                                )
                            if (
                                self._state is _SessionState.READY
                                and item.generation == self._generation
                                and item.buffer_epoch == self._buffer_epoch
                            ):
                                ordered_endpoint = item.provider_endpoint
                                if (
                                    not boundary_ready
                                    and ordered_endpoint.boundary_quality == "exact"
                                ):
                                    ordered_endpoint = self._unknown_provider_endpoint(
                                        (
                                            item.generation,
                                            item.buffer_epoch,
                                            item.utterance_id,
                                        ),
                                        phase="ordered",
                                    )
                                ordered_deadline = item.final_admission_deadline
                                if ordered_deadline is None:
                                    ordered_deadline = (
                                        time.monotonic()
                                        + _PROVIDER_BOUNDARY_CALLBACK_TIMEOUT_SECONDS
                                    )
                                await self._emit_provider_boundary(
                                    ordered_endpoint,
                                    deadline=ordered_deadline,
                                )
                        else:
                            endpoint_callback = self._on_turn_endpointed
                            if endpoint_callback is not None:
                                await endpoint_callback()
                    elif item.kind == "partial":
                        partial_callback = getattr(self, "_on_partial_transcript", None)
                        if partial_callback is not None:
                            await partial_callback(item.text)
                    else:
                        provider_final_ready_callback = self._on_provider_final_ready
                        provider_final_callback = self._on_provider_final
                        if (
                            provider_final_ready_callback is not None
                            and item.final_received_at is not None
                            and item.final_admission_deadline is not None
                        ):
                            await provider_final_ready_callback(
                                ProviderFinalNotification(
                                    key=(
                                        ProviderUtteranceKey(
                                            generation=item.generation,
                                            buffer_epoch=item.buffer_epoch,
                                            utterance_id=item.utterance_id,
                                        )
                                        if item.utterance_id is not None
                                        else None
                                    ),
                                    text=item.text,
                                    received_at=item.final_received_at,
                                    admission_deadline=(item.final_admission_deadline),
                                )
                            )
                        elif (
                            provider_final_callback is not None
                            and item.utterance_id is not None
                        ):
                            await provider_final_callback(
                                ProviderUtteranceKey(
                                    generation=item.generation,
                                    buffer_epoch=item.buffer_epoch,
                                    utterance_id=item.utterance_id,
                                ),
                                item.text,
                            )
                        else:
                            await self._on_input_transcript(item.text)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    if (
                        started_callback_task is not None
                        and not started_callback_task.done()
                    ):
                        self._retire_provider_started_callback_task(
                            started_callback_task
                        )
                    raise
                logger.warning("ASR provider utterance-start callback was cancelled")
            except Exception:
                logger.exception(
                    "ASR provider utterance-start callback failed"
                    if item.kind == "provider_started"
                    else "ASR turn endpoint callback failed"
                    if item.kind == "endpoint"
                    else "ASR partial callback failed"
                    if item.kind == "partial"
                    else "ASR transcript callback failed"
                )
            finally:
                if started_key is not None and started_settlement is not None:
                    if (
                        not started_succeeded
                        and self._provider_started_settlements.get(started_key)
                        is started_settlement
                    ):
                        await self._retire_failed_provider_started_key(
                            started_key,
                            started_settlement,
                        )
                    self._settle_provider_started(
                        started_key,
                        started_settlement,
                        succeeded=started_succeeded,
                    )
                if started_key is not None and started_callback_task is not None:
                    current_callback_task = self._provider_started_callback_tasks.get(
                        started_key
                    )
                    if current_callback_task is started_callback_task:
                        self._provider_started_callback_tasks.pop(
                            started_key,
                            None,
                        )
                self._callback_queue.task_done()
                self._flush_recovered_provider_callbacks()
            if self._state is _SessionState.CLOSED:
                return

    async def _handle_event(self, event: _AsrWorkerEvent) -> bool:
        if not isinstance(event, _AsrWorkerEvent):
            await self._fail("ASR_WORKER_FAILED", "worker returned an invalid event")
            return True

        if event.kind == "ready":
            if (
                self._state is not _SessionState.CONNECTING
                or event.generation != self._generation
            ):
                return False
            self._state = _SessionState.READY
            if self._ready_future is not None and not self._ready_future.done():
                self._ready_future.set_result(None)
            await self._emit_status("ASR_READY")
            return False

        if event.generation != self._generation:
            return False
        if (
            self._config.endpointing_mode == "provider"
            and event.kind
            in ("utterance_started", "provider_endpoint", "partial", "final")
            and self._provider_started_failed_namespace
            == (event.generation, event.buffer_epoch)
        ):
            return False
        if event.kind == "utterance_started":
            if (
                self._config.endpointing_mode != "provider"
                or event.utterance_id is None
            ):
                return False
            key = (event.generation, event.buffer_epoch, event.utterance_id)
            started_item: _CallbackItem | None = None
            started_settlement: asyncio.Future[bool] | None = None
            conflicting_start = False
            conflicting_start_boundary: ProviderEndpointNotification | None = None
            async with self._operation_lock:
                if (
                    self._state is not _SessionState.READY
                    or event.generation != self._generation
                    or event.buffer_epoch != self._buffer_epoch
                    or key in self._provider_started_failed_keys
                    or key in self._endpointed_turn_keys
                ):
                    return False
                started_notification = self._record_provider_started_locked(
                    event,
                    key,
                )
                if started_notification is None:
                    return False
                # Repeated notifications with the same start are idempotent.
                # A conflict poisons boundary proof instead of scheduling a
                # second started callback without its own revocation Future.
                # This preserves the already-bound text identity and keeps
                # clear/close able to release the callback FIFO boundedly.
                conflicting_start = key in self._provider_started_conflicted_keys
                if conflicting_start:
                    conflicting_start_boundary = (
                        self._poison_conflicting_provider_start_locked(key)
                    )
                elif key not in self._active_utterance_keys:
                    self._active_utterance_keys.add(key)
                    self._utterance_order.append(key)
                    if self._on_provider_utterance_started is not None:
                        started_settlement = asyncio.get_running_loop().create_future()
                        self._provider_started_settlements[key] = started_settlement
                if (
                    not conflicting_start
                    and key in self._active_utterance_keys
                    and key not in self._endpointed_turn_keys
                    and self._on_provider_utterance_started is not None
                ):
                    started_item = _CallbackItem(
                        text="",
                        generation=event.generation,
                        buffer_epoch=event.buffer_epoch,
                        utterance_id=event.utterance_id,
                        kind="provider_started",
                        provider_started=started_notification,
                    )
            if conflicting_start_boundary is not None:
                self._schedule_provider_boundary_callback(
                    key,
                    conflicting_start_boundary,
                )
            if started_item is not None:
                assert self._callback_queue is not None
                try:
                    await self._callback_queue.put(started_item)
                except asyncio.CancelledError:
                    if started_settlement is not None:
                        self._settle_provider_started(
                            key,
                            started_settlement,
                            succeeded=False,
                        )
                    raise
            return False
        if event.kind == "provider_endpoint":
            if (
                self._config.endpointing_mode != "provider"
                or event.utterance_id is None
            ):
                return False
            key = (event.generation, event.buffer_epoch, event.utterance_id)
            async with self._operation_lock:
                if (
                    self._state is not _SessionState.READY
                    or event.generation != self._generation
                    or event.buffer_epoch != self._buffer_epoch
                    or key not in self._active_utterance_keys
                    or key in self._endpointed_turn_keys
                ):
                    return False
                notification = self._record_provider_endpoint_locked(event, key)
            if notification is not None:
                self._schedule_provider_boundary_callback(key, notification)
            return False
        if event.kind == "partial":
            text = event.text.strip()
            if (
                getattr(self, "_on_partial_transcript", None) is None
                or not text
                or self._state is not _SessionState.READY
                or event.buffer_epoch != self._buffer_epoch
            ):
                return False
            async with self._operation_lock:
                if (
                    self._state is not _SessionState.READY
                    or event.buffer_epoch != self._buffer_epoch
                ):
                    return False
                key = (event.generation, event.buffer_epoch, event.utterance_id)
                if (
                    self._config.endpointing_mode == "provider"
                    and event.utterance_id is not None
                    and key not in self._active_utterance_keys
                ):
                    return False
                if (
                    event.utterance_id is not None
                    and self._utterance_order
                    and key != self._utterance_order[0]
                ):
                    # The frontend keeps a single preview slot and every
                    # delivered final clears it, so a preview for a turn
                    # that is not the current ordered turn would display as
                    # the current turn and then be erased by the earlier
                    # turn's final. Coalesce to the latest text per key;
                    # _drain_ready_partials_locked flushes it once the key
                    # reaches the head of the order and drops it when the
                    # key is invalidated.
                    if (
                        key in self._active_utterance_keys
                        or key in self._committed_utterance_keys
                    ):
                        self._pending_partials[key] = text
                    return False
            # Route live previews through the ordered callback FIFO so a
            # current-turn partial can never overtake an earlier turn's
            # final that is still waiting for delivery.
            assert self._callback_queue is not None
            await self._callback_queue.put(
                _CallbackItem(
                    text=text,
                    generation=event.generation,
                    buffer_epoch=event.buffer_epoch,
                    utterance_id=event.utterance_id,
                    kind="partial",
                )
            )
            return False
        if event.kind == "final":
            if event.utterance_id is None:
                return False
            text = event.text.strip()
            key = (event.generation, event.buffer_epoch, event.utterance_id)
            immediate_provider_boundary: ProviderEndpointNotification | None = None
            async with self._operation_lock:
                if (
                    self._state is not _SessionState.READY
                    or event.generation != self._generation
                    or event.buffer_epoch != self._buffer_epoch
                ):
                    return False
                if key in self._pending_finals:
                    logger.warning(
                        "ASR worker returned a duplicate or conflicting final"
                    )
                    return False
                valid_keys = (
                    self._active_utterance_keys
                    if self._config.endpointing_mode == "provider"
                    else self._committed_utterance_keys
                )
                if key not in valid_keys:
                    logger.warning(
                        "ASR worker returned a final for an inactive utterance"
                    )
                    return False
                if (
                    self._config.endpointing_mode == "provider"
                    and not self._uses_segment_aggregation
                ):
                    immediate_provider_boundary = (
                        self._ensure_unknown_provider_endpoint_locked(key)
                    )
                if (
                    self._config.endpointing_mode == "provider"
                    and self._uses_segment_aggregation
                ):
                    # Segmented transport orders finals through the segment
                    # aggregator, so the seal notification stays inline here.
                    await self._notify_turn_endpointed_locked(key)
                if self._uses_segment_aggregation:
                    if not self._segment_aggregator.record_transcript(key, text):
                        logger.warning(
                            "ASR worker returned a duplicate or conflicting final"
                        )
                        return False
                    ready_texts = self._collect_ready_segmented_transcripts_locked()
                    ready_items = [
                        self._logical_final_callback_item(
                            ready_text,
                            generation=event.generation,
                            buffer_epoch=event.buffer_epoch,
                        )
                        for ready_text in ready_texts
                    ]
                else:
                    # Capture the absolute admission budget at the first
                    # valid Provider-final receipt. Out-of-order finals retain
                    # this timestamp while waiting behind earlier keys.
                    received_at = time.monotonic()
                    self._pending_finals[key] = _PendingFinal(
                        text=text,
                        received_at=received_at,
                        admission_deadline=(
                            received_at + _PROVIDER_FINAL_ADMISSION_TIMEOUT_SECONDS
                        ),
                    )
                    ready_items = self._drain_ready_provider_finals_locked()
                ready_items.extend(self._drain_ready_partials_locked())
            if immediate_provider_boundary is not None:
                self._schedule_provider_boundary_callback(
                    key,
                    immediate_provider_boundary,
                )
            assert self._callback_queue is not None
            for ready_item in ready_items:
                await self._callback_queue.put(ready_item)
            return False
        if event.kind == "error":
            if (
                event.utterance_id is not None
                and event.buffer_epoch != self._buffer_epoch
            ):
                return False
            await self._fail(
                event.error_code or "ASR_WORKER_FAILED",
                event.error_message or "worker reported a provider error",
            )
            return True
        if event.kind == "closed":
            if self._state is _SessionState.CLOSING:
                return True
            await self._fail("ASR_WORKER_FAILED", "worker closed unexpectedly")
            return True

        await self._fail("ASR_WORKER_FAILED", "worker returned an unknown event")
        return True

    async def _fail(self, error_code: str, message: str) -> None:
        if self._state in (_SessionState.FAILED, _SessionState.CLOSED):
            return
        self._state = _SessionState.FAILED
        self._generation += 1
        self._closing_event.set()
        await self._cancel_provider_started_callbacks(clear_failures=True)
        await self._unload_voice_turn_adapter(context="during failure")
        self._active_utterance_keys.clear()
        self._committed_utterance_keys.clear()
        self._utterance_order.clear()
        self._pending_finals.clear()
        self._pending_partials.clear()
        self._provider_endpoints.clear()
        await self._cancel_provider_boundary_tasks()
        self._clear_segment_aggregation_state()
        safe_code = (
            error_code
            if re.fullmatch(r"ASR_[A-Z0-9_]+", error_code or "")
            else "ASR_WORKER_FAILED"
        )
        safe_message = self._sanitize_error(message)
        error = f"{safe_code}: {safe_message}"
        if self._ready_future is not None and not self._ready_future.done():
            self._ready_future.set_exception(RuntimeError(error))
        if (
            self._worker_task is not None
            and self._worker_task is not asyncio.current_task()
        ):
            self._worker_task.cancel()
        try:
            await self._emit_connection_error_once(error)
        finally:
            if self._callback_queue is not None:
                try:
                    await asyncio.wait_for(
                        self._callback_queue.join(),
                        timeout=_CALLBACK_DRAIN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning("ASR callback drain timed out after failure")
            await self._shutdown()

    def _queued_audio_bytes(self) -> int:
        queue = self._request_queue
        if queue is None:
            return 0
        if isinstance(queue, _AsrRequestQueue):
            return queue.waiting_audio_bytes
        # asyncio.Queue 没有公开快照接口；这里仅在 session 自己的 operation
        # lock 内读取其 deque，不修改内部结构。
        queued = getattr(queue, "_queue", ())
        return sum(
            len(item.audio)
            for item in queued
            if isinstance(item, _AsrWorkerRequest) and item.kind == "audio"
        )

    def _queued_audio_ms(self) -> int:
        return self._queued_audio_bytes() * 1_000 // (16_000 * 2)

    def _queued_audio_items(self) -> int:
        queue = self._request_queue
        if queue is None:
            return 0
        if isinstance(queue, _AsrRequestQueue):
            return queue.waiting_audio_items
        queued = getattr(queue, "_queue", ())
        return sum(
            1
            for item in queued
            if isinstance(item, _AsrWorkerRequest) and item.kind == "audio"
        )

    async def _wait_for_audio_queue_capacity(
        self,
        request: _AsrWorkerRequest,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _REQUEST_BACKPRESSURE_TIMEOUT_SECONDS
        while (
            self._queued_audio_bytes() + len(request.audio)
            > _ACTIVE_QUEUE_MAX_AUDIO_BYTES
            or self._queued_audio_items() + 1 > _ACTIVE_QUEUE_MAX_AUDIO_ITEMS
        ):
            if self._closing_event.is_set() or self._state is not _SessionState.READY:
                raise RuntimeError("ASR_SESSION_NOT_READY: session is not ready")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    "ASR_STREAM_BACKPRESSURE: active audio queue exceeded "
                    f"{_ACTIVE_QUEUE_MAX_AUDIO_MS}ms"
                )
            try:
                await asyncio.wait_for(
                    self._closing_event.wait(),
                    timeout=min(0.01, remaining),
                )
            except asyncio.TimeoutError:
                continue
            raise RuntimeError("ASR_SESSION_NOT_READY: session is not ready")

    async def _enqueue_request(self, request: _AsrWorkerRequest) -> None:
        worker_task = self._worker_task
        if (
            self._state is not _SessionState.READY
            or self._request_queue is None
            or self._closing_event.is_set()
            or worker_task is None
            or worker_task.done()
        ):
            raise RuntimeError("ASR_SESSION_NOT_READY: session is not ready")

        if request.kind == "audio":
            await self._wait_for_audio_queue_capacity(request)

        key: _UtteranceKey | None = None
        added_active = False
        added_committed = False
        added_order = False
        if request.utterance_id is not None and request.kind in ("audio", "commit"):
            key = (
                request.generation,
                request.buffer_epoch,
                request.utterance_id,
            )
            if (
                self._config.endpointing_mode == "manual"
                and request.kind == "audio"
                and key not in self._active_utterance_keys
            ):
                self._active_utterance_keys.add(key)
                added_active = True
            if request.kind == "commit" and key not in self._committed_utterance_keys:
                self._committed_utterance_keys.add(key)
                added_committed = True
                self._utterance_order.append(key)
                added_order = True

        put_task = asyncio.create_task(self._request_queue.put(request))
        closing_task = asyncio.create_task(self._closing_event.wait())
        watched: set[asyncio.Task[Any]] = {put_task, closing_task, worker_task}
        try:
            done, _ = await asyncio.wait(
                watched,
                timeout=_REQUEST_BACKPRESSURE_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            put_task.cancel()
            closing_task.cancel()
            await asyncio.gather(put_task, closing_task, return_exceptions=True)
            if key is not None:
                if added_active:
                    self._active_utterance_keys.discard(key)
                if added_committed:
                    self._committed_utterance_keys.discard(key)
                if added_order:
                    try:
                        self._utterance_order.remove(key)
                    except ValueError:
                        pass
            raise

        if not done:
            put_task.cancel()
            closing_task.cancel()
            await asyncio.gather(put_task, closing_task, return_exceptions=True)
            if key is not None:
                if added_active:
                    self._active_utterance_keys.discard(key)
                if added_committed:
                    self._committed_utterance_keys.discard(key)
                if added_order:
                    try:
                        self._utterance_order.remove(key)
                    except ValueError:
                        pass
            raise RuntimeError(
                "ASR_STREAM_BACKPRESSURE: provider request queue remained full"
            )

        if put_task in done:
            try:
                await put_task
            except BaseException:
                closing_task.cancel()
                await asyncio.gather(closing_task, return_exceptions=True)
                if key is not None:
                    if added_active:
                        self._active_utterance_keys.discard(key)
                    if added_committed:
                        self._committed_utterance_keys.discard(key)
                    if added_order:
                        try:
                            self._utterance_order.remove(key)
                        except ValueError:
                            pass
                raise
            closing_task.cancel()
            await asyncio.gather(closing_task, return_exceptions=True)
            return

        put_task.cancel()
        closing_task.cancel()
        await asyncio.gather(put_task, closing_task, return_exceptions=True)
        if key is not None:
            if added_active:
                self._active_utterance_keys.discard(key)
            if added_committed:
                self._committed_utterance_keys.discard(key)
            if added_order:
                try:
                    self._utterance_order.remove(key)
                except ValueError:
                    pass
        raise RuntimeError("ASR_SESSION_NOT_READY: worker is no longer running")

    def _make_resampler(self) -> soxr.ResampleStream | None:
        if self._input_sample_rate_hz != 48000:
            return None
        return soxr.ResampleStream(
            48000,
            16000,
            1,
            dtype="float32",
            quality="HQ",
        )

    def _convert_audio(self, audio_chunk: bytes) -> bytes:
        if self._resampler is None:
            return audio_chunk
        samples = np.frombuffer(audio_chunk, dtype="<i2").astype(np.float32)
        samples /= 32768.0
        output = self._resampler.resample_chunk(samples)
        if len(output) == 0:
            return b""
        return (output * 32768.0).clip(-32768, 32767).astype("<i2").tobytes()

    def _flush_resampler(self) -> bytes:
        if self._resampler is None:
            return b""
        output = self._resampler.resample_chunk(
            np.empty(0, dtype=np.float32),
            last=True,
        )
        if len(output) == 0:
            return b""
        return (output * 32768.0).clip(-32768, 32767).astype("<i2").tobytes()

    def _reset_resampler(self) -> None:
        if self._resampler is not None:
            self._resampler.clear()
        self._resampler = self._make_resampler()

    async def _emit_status(self, status: str) -> None:
        if self._on_status_message is None:
            return
        try:
            await self._on_status_message(status)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ASR status callback failed")

    async def _emit_connection_error_once(self, error: str) -> None:
        if self._connection_error_reported:
            return
        self._connection_error_reported = True
        try:
            await self._on_connection_error(error)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ASR connection error callback failed")

    async def _shutdown(self) -> None:
        current = asyncio.current_task()
        await self._cancel_provider_started_callbacks(clear_failures=True)
        boundary_tasks = tuple(
            {
                task
                for task in (
                    *self._provider_boundary_tasks.values(),
                    *self._provider_boundary_chain_tasks,
                    *self._provider_boundary_retired_tasks,
                )
                if task is not None
                if task is not current and not task.done()
            }
        )
        self._provider_boundary_tasks.clear()
        self._provider_boundary_deadlines.clear()
        self._provider_boundary_chain_tasks.clear()
        self._provider_boundary_retired_tasks.clear()
        self._provider_boundary_chain_tail = None
        self._provider_boundary_chain_namespace = None
        self._provider_boundary_failed_namespace = None
        for task in boundary_tasks:
            task.cancel()
        if boundary_tasks:
            await asyncio.wait(
                boundary_tasks,
                timeout=_PROVIDER_BOUNDARY_CALLBACK_TIMEOUT_SECONDS,
            )
        tasks = [
            task
            for task in (
                self._worker_task,
                self._response_task,
                self._callback_task,
            )
            if (
                task is not None
                and task is not current
                and task is not self._callback_close_waiter
                and not task.done()
            )
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _validate_language(self, language: str) -> str:
        # Kept as a private seam for future worker-specific language mapping.
        return AsrSessionConfig(language=language).language

    def _sanitize_error(self, message: str) -> str:
        safe = str(message or "worker failed")
        if self._api_key:
            safe = safe.replace(self._api_key, "[REDACTED]")
        safe = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", safe)
        safe = re.sub(r"([?&](?:api_?key|token|key)=)[^&\s]+", r"\1[REDACTED]", safe)
        safe = re.sub(r"https?://[^\s?#]+[?][^\s]+", "[REDACTED_URL]", safe)
        safe = " ".join(safe.split())
        return safe[:300] or "worker failed"
