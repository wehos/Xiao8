"""Session-level endpoint detector and Smart Turn adapter."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypeAlias

from main_logic.voice_turn.activity_evidence import RnnoiseEvidence
from main_logic.voice_turn.contracts import (
    EvaluationStatus,
    SpeechActivityEvent,
    TurnDecision,
)

from .._provider_events import ProviderAudioRange
from ..failure_diagnostics import AudioFailureContext
from .config import SmartTurnConfig
from .coordinator import CoordinatorState, TurnCoordinator
from .detector import (
    BoundDetectorTurn,
    DetectorActivityEvent,
    DetectorCandidateKey,
    DetectorDurationQueue,
    DetectorEvent,
    DetectorIngressIdentity,
    DetectorPrewarmEvent,
    DetectorSubmitResult,
    DetectorSubmitStatus,
    DetectorTransportPrewarmEvent,
    DetectorTurnEvent,
    ProviderCandidateFence,
    ProviderSpeakerBoundarySnapshot,
    ProviderSpeakerPresealVerdict,
    SmartTurnCompletionFence,
)
from .micro_event_policy import (
    ProviderMicroEventConfig,
    ProviderMicroEventDecision,
    ProviderMicroEventEvidence,
    ProviderMicroEventPolicy,
)
from .silero_vad import SileroActivityGate, SileroFeedResult, SileroVad
from .smart_turn_audio_evidence import create_smart_turn_audio_evidence_recorder
from .smart_turn_diagnostics import create_smart_turn_runtime_diagnostics
from .smart_turn_v3 import SmartTurnV3
from .throttle_policy import (
    ThrottleAction,
    ThrottleShadowMetrics,
    VoiceThrottlePolicy,
)
from ..lifecycle import VoiceIngressToken, VoiceTurnToken
from ..provider_policy import AsrProviderPolicy
from ..speaker_shadow.contracts import (
    SpeakerShadowBatchReconcileReceipt,
    SpeakerShadowBatchReconcileRequest,
    SpeakerShadowBatchReconciliationControl,
    SpeakerShadowCandidateLifecycleControl,
    SpeakerShadowCaptureDecisionState,
    SpeakerShadowCaptureDisposition,
    SpeakerShadowCaptureResult,
    SpeakerShadowCaptureStatus,
    SpeakerShadowCandidateKey,
    SpeakerShadowDecisionStatus,
    SpeakerShadowDeferredAnchorControl,
    SpeakerShadowDeferredAnchorReceipt,
    SpeakerShadowDeferredAnchorRequest,
    SpeakerShadowDeferredCandidateControl,
    SpeakerShadowDeferredCandidateStatus,
    SpeakerShadowExactIntervalControl,
    SpeakerShadowExactIntervalScoreControl,
    SpeakerShadowObserver,
    SpeakerShadowReconcileSource,
    SpeakerShadowReconciliationSettlement,
    SpeakerShadowPreparedTerminalCoverageControl,
    SpeakerShadowScope,
    SpeakerShadowTerminalCoverageControl,
    SpeakerShadowTerminalCoverageReceipt,
    SpeakerShadowTerminalCoverageRequest,
)


logger = logging.getLogger(__name__)


_Identity: TypeAlias = tuple[int, int, int]
_FallbackReason: TypeAlias = Literal["semantic_incomplete", "semantic_degraded"]
_COMMIT_DRAIN_ON_CLOSE_SECONDS = 0.5
_SPEAKER_SHADOW_REPLACEMENT_CLOSE_SECONDS = 2.0
_PROVIDER_SEGMENT_FIFO_LIMIT = 8
_PROVIDER_SEGMENT_EXPIRY_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class _VoiceTurnFailure:
    kind: Literal["unavailable", "runtime_error"]
    stage: Literal["vad_load", "vad_feed", "smart_turn", "consumer"]


@dataclass(frozen=True, slots=True)
class _AudioItem:
    identity: _Identity
    pcm16: bytes
    duration_us: int
    detector_identity: DetectorIngressIdentity | None = None
    deny_rearm_boundary: bool = False


@dataclass(frozen=True, slots=True)
class _ResetItem:
    identity: _Identity
    completed: asyncio.Future[None]
    requester: asyncio.Task[object] | None = None


@dataclass(frozen=True, slots=True)
class _DenyRearmItem:
    token: tuple[int, int, int]
    completed: asyncio.Future[bool]


@dataclass(frozen=True, slots=True)
class _CloseItem:
    completed: asyncio.Future[None]


@dataclass(frozen=True, slots=True)
class _EvaluationResultItem:
    identity: _Identity
    coordinator_generation: int
    activity_seq: int
    reason: Literal["candidate_pause", "periodic_no_vad", "strict_retry"]
    detector_identity: DetectorIngressIdentity | None = None
    evaluation_ms: int = 0
    result: object | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _PendingCompleteConfirmation:
    identity: _Identity
    detector_identity: DetectorIngressIdentity | None
    reason: Literal["candidate_pause", "periodic_no_vad", "strict_retry"]
    probability: float | None


@dataclass(frozen=True, slots=True)
class _CompleteConfirmationItem:
    pending: _PendingCompleteConfirmation


_ControlItem: TypeAlias = (
    _ResetItem
    | _DenyRearmItem
    | _CloseItem
    | _EvaluationResultItem
    | _CompleteConfirmationItem
)
_QueueItem: TypeAlias = _AudioItem | _ControlItem


class _VoiceTurnAdapter:
    """Serialize Silero and Smart Turn work outside the ASR audio producer."""

    def __init__(
        self,
        *,
        vad: SileroVad,
        gate: SileroActivityGate,
        coordinator: TurnCoordinator,
        on_commit: Callable[[int, int, int], Awaitable[None]],
        on_completion_fence: Callable[
            [int, int, int, DetectorIngressIdentity], _Identity
        ]
        | None = None,
        on_activity: Callable[[SpeechActivityEvent], Awaitable[None]] | None = None,
        on_scoped_commit: Callable[
            [int, int, int, DetectorIngressIdentity], Awaitable[None]
        ]
        | None = None,
        on_scoped_activity: Callable[
            [SpeechActivityEvent, DetectorIngressIdentity], Awaitable[None]
        ]
        | None = None,
        on_accepted_audio: Callable[[bytes, int, DetectorIngressIdentity | None], None]
        | None = None,
        on_candidate_complete: Callable[[DetectorIngressIdentity | None], None]
        | None = None,
        queue_maxsize: int = 128,
        queue_capacity_ms: int = 1_000,
        continuation_timeout_seconds: float = 2.0,
        max_endpoint_wait_seconds: float = 15.0,
        candidate_complete_confirmation_seconds: float = 0.0,
        strict_complete_confirmation_seconds: float = 0.6,
        smart_turn_required: bool = False,
        smart_turn_warm_seconds: float = 60.0,
        fallback_evaluation_interval_ms: int = 500,
    ) -> None:
        if queue_maxsize <= 0:
            raise ValueError("queue_maxsize must be positive")
        if queue_capacity_ms <= 0:
            raise ValueError("queue_capacity_ms must be positive")
        if continuation_timeout_seconds <= 0:
            raise ValueError("continuation_timeout_seconds must be positive")
        if max_endpoint_wait_seconds <= 0:
            raise ValueError("max_endpoint_wait_seconds must be positive")
        if max_endpoint_wait_seconds < continuation_timeout_seconds:
            raise ValueError(
                "max_endpoint_wait_seconds must not be shorter than continuation timeout"
            )
        if (
            not math.isfinite(candidate_complete_confirmation_seconds)
            or candidate_complete_confirmation_seconds < 0
        ):
            raise ValueError(
                "candidate complete confirmation delay must be finite and non-negative"
            )
        if (
            not math.isfinite(strict_complete_confirmation_seconds)
            or strict_complete_confirmation_seconds < 0
        ):
            raise ValueError(
                "strict complete confirmation delay must be finite and non-negative"
            )
        if smart_turn_warm_seconds <= 0:
            raise ValueError("smart_turn_warm_seconds must be positive")
        if fallback_evaluation_interval_ms <= 0:
            raise ValueError("fallback_evaluation_interval_ms must be positive")
        self._vad = vad
        self._gate = gate
        self._coordinator = coordinator
        self._smart_turn_diagnostics = create_smart_turn_runtime_diagnostics()
        self._smart_turn_audio_evidence = create_smart_turn_audio_evidence_recorder()
        self._on_commit = on_commit
        self._on_completion_fence = on_completion_fence
        self._on_activity = on_activity
        self._on_scoped_commit = on_scoped_commit
        self._on_scoped_activity = on_scoped_activity
        self._on_accepted_audio = on_accepted_audio
        self._on_candidate_complete = on_candidate_complete
        self._queue: DetectorDurationQueue[_AudioItem, _ControlItem] = (
            DetectorDurationQueue(
                capacity_us=queue_capacity_ms * 1_000,
                max_frames=queue_maxsize,
            )
        )
        queue_capacity_us = queue_capacity_ms * 1_000
        confirmation_capacity_us = 0
        if smart_turn_required:
            confirmation_capacity_us = math.ceil(
                max(
                    candidate_complete_confirmation_seconds,
                    strict_complete_confirmation_seconds,
                )
                * 1_000_000
            )
        # Retained audio can already occupy one queue-capacity window while an
        # evaluation is in flight. A configured confirmation then needs its own
        # full window plus one bounded queue window for frame granularity and
        # event-loop scheduling before the confirmation control item is handled.
        self._evaluation_tail_capacity_us = (
            queue_capacity_us
            + confirmation_capacity_us
            + (queue_capacity_us if confirmation_capacity_us else 0)
        )
        self._evaluation_tail: list[_AudioItem] = []
        self._evaluation_tail_duration_us = 0
        self._confirmation_tail: list[_AudioItem] = []
        self._confirmation_tail_duration_us = 0
        self._continuation_timeout_seconds = continuation_timeout_seconds
        self._max_endpoint_wait_seconds = max_endpoint_wait_seconds
        self._candidate_complete_confirmation_seconds = (
            candidate_complete_confirmation_seconds
        )
        self._strict_complete_confirmation_seconds = (
            strict_complete_confirmation_seconds
        )
        self._smart_turn_required = smart_turn_required
        self._smart_turn_warm_seconds = smart_turn_warm_seconds
        self._fallback_evaluation_interval_ms = fallback_evaluation_interval_ms
        self._consumer_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._fallback_task: asyncio.Task[None] | None = None
        self._smart_turn_unload_task: asyncio.Task[None] | None = None
        self._evaluation_task: asyncio.Task[None] | None = None
        self._reevaluation_requested = False
        self._reevaluation_reason: (
            Literal["candidate_pause", "periodic_no_vad", "strict_retry"] | None
        ) = None
        self._pending_complete_confirmation: _PendingCompleteConfirmation | None = None
        self._strict_endpoint_deadline: float | None = None
        self._latest_detector_identity: DetectorIngressIdentity | None = None
        self._smart_turn_evaluation_ms = 0
        self._smart_turn_stale_result_count = 0
        self._smart_turn_coalesced_evaluation_count = 0
        self._callback_tasks: set[asyncio.Task[None]] = set()
        self._identity: _Identity | None = None
        self._vad_load_attempted = False
        self._vad_available = False
        self._vad_degraded = False
        self._fallback_speech_started = False
        self._fallback_audio_bytes = 0
        self._semantic_degraded = False
        self._failed = False
        self._failure_future: asyncio.Future[_VoiceTurnFailure] | None = None
        self._failure: _VoiceTurnFailure | None = None
        self._resources_closed = False
        self._closed = False
        self._commit_dispatched: set[_Identity] = set()
        self._successor_audio_fence: tuple[_Identity, int, _Identity] | None = None
        self._smart_turn_pin_count = 0
        self._deny_rearm_token: tuple[int, int, int] | None = None

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("ASR_VOICE_TURN_CLOSED: adapter is closed")
        if self._failed:
            raise RuntimeError("ASR_VOICE_TURN_FAILED: adapter has failed")
        if self._failure_future is None:
            self._failure_future = asyncio.get_running_loop().create_future()
        if self._consumer_task is None:
            self._consumer_task = asyncio.create_task(
                self._consume(), name="asr-voice-turn"
            )

    async def wait_failure(self) -> _VoiceTurnFailure:
        failure_future = self._failure_future
        if failure_future is None:
            failure_future = asyncio.get_running_loop().create_future()
            self._failure_future = failure_future
        return await failure_future

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def failure(self) -> _VoiceTurnFailure | None:
        return self._failure

    @property
    def throttle_available(self) -> bool:
        return not self._vad_degraded

    @property
    def queued_audio_ms(self) -> int:
        return self._queue.audio_duration_us // 1_000

    @property
    def smart_turn_evaluation_ms(self) -> int:
        return self._smart_turn_evaluation_ms

    @property
    def smart_turn_stale_result_count(self) -> int:
        return self._smart_turn_stale_result_count

    @property
    def smart_turn_coalesced_evaluation_count(self) -> int:
        return self._smart_turn_coalesced_evaluation_count

    @property
    def deny_rearm_token(self) -> tuple[int, int, int] | None:
        return self._deny_rearm_token

    def consume_deny_rearm(self, token: tuple[int, int, int]) -> None:
        if self._deny_rearm_token == token:
            self._deny_rearm_token = None

    async def wait_idle(self) -> None:
        """Drain detector work for tests and shutdown; never use per PCM frame."""

        while True:
            await self._queue.join()
            evaluation_task = self._evaluation_task
            if evaluation_task is None:
                break
            await asyncio.gather(evaluation_task, return_exceptions=True)
        callbacks = tuple(self._callback_tasks)
        if callbacks:
            await asyncio.gather(*callbacks)

    def pin_smart_turn(self) -> None:
        self._ensure_running()
        self._smart_turn_pin_count += 1
        self._cancel_smart_turn_unload()

    def unpin_smart_turn(self) -> None:
        if self._smart_turn_pin_count <= 0:
            return
        self._smart_turn_pin_count -= 1
        if self._smart_turn_pin_count == 0 and self._identity is not None:
            self._schedule_smart_turn_unload(self._identity)

    async def push_audio(
        self,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
        pcm16: bytes,
        sample_rate_hz: int = 16_000,
        detector_identity: DetectorIngressIdentity | None = None,
        deny_rearm_boundary: bool = False,
    ) -> None:
        if len(pcm16) % 2:
            raise ValueError("ASR_INVALID_PCM: Voice Turn requires PCM16LE")
        if not pcm16:
            return
        if sample_rate_hz != 16_000:
            raise ValueError("ASR_INVALID_SAMPLE_RATE: Voice Turn requires 16 kHz")
        self._ensure_running()
        samples = len(pcm16) // 2
        duration_us = (samples * 1_000_000 + sample_rate_hz - 1) // sample_rate_hz
        pending = self._pending_complete_confirmation
        retains_for_confirmation = (
            pending is not None
            and pending.identity == (generation, buffer_epoch, utterance_id)
            and pending.detector_identity is not None
            and detector_identity is not None
            and detector_identity.detector_epoch
            == pending.detector_identity.detector_epoch
            and detector_identity.sequence_no > pending.detector_identity.sequence_no
        )
        if (
            (self._evaluation_task is not None or retains_for_confirmation)
            and self._evaluation_tail_duration_us
            + self._confirmation_tail_duration_us
            + self._queue.audio_duration_us
            + duration_us
            > self._evaluation_tail_capacity_us
        ):
            # Surface the shared duration budget at ingress so DetectorRuntime
            # can use its existing whole-candidate backpressure recovery. Once
            # an item reaches the consumer it must remain replayable if the
            # in-flight evaluation completes the predecessor turn.
            raise asyncio.QueueFull
        self._queue.put_audio_nowait(
            _AudioItem(
                (generation, buffer_epoch, utterance_id),
                pcm16,
                duration_us,
                detector_identity,
                deny_rearm_boundary,
            ),
            duration_us=duration_us,
        )

    async def reset(
        self,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        self._ensure_running()
        self._queue.discard_audio()
        completed = asyncio.get_running_loop().create_future()
        self._queue.put_control_nowait(
            _ResetItem(
                (generation, buffer_epoch, utterance_id),
                completed,
                asyncio.current_task(),
            ),
            priority=True,
        )
        consumer = self._consumer_task
        if consumer is None:
            raise RuntimeError("ASR_VOICE_TURN_CLOSED: adapter is not running")
        done, _pending = await asyncio.wait(
            {completed, consumer},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if completed in done:
            await completed
            return
        if not completed.done():
            completed.cancel()
        raise RuntimeError("ASR_VOICE_TURN_FAILED: adapter stopped during reset")

    async def prepare_deny_rearm(self, token: tuple[int, int, int]) -> bool:
        """Order a post-deny Silero boundary behind accepted detector audio."""

        self._ensure_running()
        completed = asyncio.get_running_loop().create_future()
        self._queue.put_control_nowait(_DenyRearmItem(token, completed))
        return bool(await asyncio.shield(completed))

    async def close(self) -> None:
        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._close_impl(),
                name="asr-voice-turn-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        if self._closed:
            return
        task = self._consumer_task
        if task is None:
            self._closed = True
            await self._close_resources()
            return
        if self._failed and not task.done():
            # SmartTurn 的端点等待任务也可能在 consumer 队列外宣告失败。
            # 此时 consumer 仍在等下一项，必须显式取消，避免关闭流程永久等待。
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if task.done():
            self._closed = True
            await asyncio.gather(task, return_exceptions=True)
            await self._close_resources()
            return
        completed = asyncio.get_running_loop().create_future()
        # Preserve FIFO for PCM that push_audio() already admitted. In
        # particular, continuation audio must be allowed to cancel a pending
        # provisional COMPLETE before shutdown resolves it.
        self._queue.put_control_nowait(_CloseItem(completed))
        await completed
        await task

    def _ensure_running(self) -> None:
        if self._failed:
            raise RuntimeError("ASR_VOICE_TURN_FAILED: adapter has failed")
        task = self._consumer_task
        if self._closed or task is None or task.done():
            raise RuntimeError("ASR_VOICE_TURN_CLOSED: adapter is not running")

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if isinstance(item, _AudioItem):
                    await self._process_audio(item)
                    if self._failed:
                        await self._drain_queue_on_failure_exit()
                        return
                    continue
                if isinstance(item, _ResetItem):
                    await self._process_reset(item.identity, requester=item.requester)
                    if not item.completed.done():
                        item.completed.set_result(None)
                    continue
                if isinstance(item, _DenyRearmItem):
                    prepared = await self._process_deny_rearm(item.token)
                    if not item.completed.done():
                        item.completed.set_result(prepared)
                    continue
                if isinstance(item, _EvaluationResultItem):
                    await self._process_evaluation_result(item)
                    if self._failed:
                        await self._drain_queue_on_failure_exit()
                        return
                    continue
                if isinstance(item, _CompleteConfirmationItem):
                    if self._fallback_task is not None and self._fallback_task.done():
                        self._fallback_task = None
                    await self._publish_pending_complete_confirmation(item.pending)
                    continue
                await self._process_close()
                if not item.completed.done():
                    item.completed.set_result(None)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                completed = getattr(item, "completed", None)
                if completed is not None and not completed.done():
                    completed.set_exception(exc)
                if not isinstance(item, _CloseItem):
                    self._report_failure("runtime_error", "consumer")
                    await self._drain_queue_on_failure_exit()
                    return
                raise
            finally:
                self._queue.task_done()

    async def _drain_queue_on_failure_exit(self) -> None:
        """Unblock join()/reset() waiters when a failure stops the consumer."""

        evaluation_task, self._evaluation_task = self._evaluation_task, None
        if evaluation_task is not None:
            evaluation_task.cancel()
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if isinstance(item, _CloseItem):
                    try:
                        await self._process_close()
                    except Exception:
                        logger.exception(
                            "ASR voice turn close failed after consumer failure"
                        )
                    if not item.completed.done():
                        item.completed.set_result(None)
                    continue
                completed = getattr(item, "completed", None)
                if completed is not None and not completed.done():
                    completed.set_exception(
                        RuntimeError("ASR_VOICE_TURN_FAILED: adapter has failed")
                    )
            finally:
                self._queue.task_done()

    async def _process_audio(self, item: _AudioItem) -> None:
        if self._identity is None:
            self._identity = item.identity
        if item.identity != self._identity:
            fence = self._successor_audio_fence
            if (
                fence is None
                or item.detector_identity is None
                or item.identity != fence[0]
                or self._identity != fence[2]
                or item.detector_identity.sequence_no <= fence[1]
            ):
                return
            item = _AudioItem(
                identity=self._identity,
                pcm16=item.pcm16,
                duration_us=item.duration_us,
                detector_identity=item.detector_identity,
                deny_rearm_boundary=item.deny_rearm_boundary,
            )
        if item.deny_rearm_boundary:
            await self._process_deny_rearm_audio(item)
            return
        if self._deny_rearm_token is not None:
            await asyncio.to_thread(self._gate.reset)
            self._deny_rearm_token = None
        defers_for_evaluation = self._evaluation_task is not None
        if defers_for_evaluation:
            self._evaluation_tail.append(item)
            self._evaluation_tail_duration_us += item.duration_us
        retained_for_confirmation = self._retain_confirmation_audio(item)
        if not defers_for_evaluation and not retained_for_confirmation:
            self._attribute_accepted_audio(item)
        self._latest_detector_identity = item.detector_identity
        self._coordinator.push_audio(item.pcm16)
        if self._vad_degraded:
            await self._process_without_vad(item)
            return
        if not self._vad_load_attempted:
            self._vad_load_attempted = True
            try:
                self._vad_available = await asyncio.to_thread(self._vad.load)
            except Exception:
                self._emit_pipeline("vad_load", item.identity, item.detector_identity, outcome="error")
                if self._smart_turn_required:
                    self._vad_degraded = True
                    await self._process_without_vad(item)
                else:
                    self._report_failure("runtime_error", "vad_load")
                return
        if not self._vad_available:
            if not self._vad_degraded:
                self._emit_pipeline("vad_load", item.identity, item.detector_identity, outcome="unavailable")
            if self._smart_turn_required:
                self._vad_degraded = True
                await self._process_without_vad(item)
            else:
                self._report_failure("unavailable", "vad_load")
            return

        try:
            events = await asyncio.to_thread(self._gate.feed, item.pcm16)
        except Exception:
            self._emit_pipeline("vad_feed", item.identity, item.detector_identity, outcome="error")
            if self._smart_turn_required:
                self._vad_degraded = True
                await self._process_without_vad(item)
            else:
                self._report_failure("runtime_error", "vad_feed")
            return
        for event in events:
            self._emit_pipeline("vad_activity", item.identity, item.detector_identity, reason=event.value)
            if self._on_activity is not None:
                await self._on_activity(event)
            if (
                self._on_scoped_activity is not None
                and item.detector_identity is not None
            ):
                await self._on_scoped_activity(event, item.detector_identity)
            await self._coordinator.on_activity_event(event)

        if any(
            event
            in (SpeechActivityEvent.SPEECH_STARTED, SpeechActivityEvent.SPEECH_RESUMED)
            for event in events
        ):
            self._cancel_smart_turn_unload()
            self._cancel_fallback()
            self._strict_endpoint_deadline = None

        if (
            SpeechActivityEvent.CANDIDATE_PAUSE not in events
            or self._coordinator.state is not CoordinatorState.PAUSE_CANDIDATE
        ):
            return

        if self._semantic_degraded:
            if self._smart_turn_required:
                self._report_failure("unavailable", "smart_turn")
                return
            self._schedule_fallback(item.identity, "semantic_degraded")
            return

        self._request_evaluation(
            item.identity,
            "candidate_pause",
            item.detector_identity,
        )

    async def _process_deny_rearm_audio(self, item: _AudioItem) -> None:
        """Produce only Silero activity for a post-deny boundary frame."""

        if self._vad_degraded:
            return
        if not self._vad_load_attempted:
            self._vad_load_attempted = True
            try:
                self._vad_available = bool(await asyncio.to_thread(self._vad.load))
            except Exception:
                self._vad_available = False
        if not self._vad_available:
            self._vad_degraded = True
            return
        try:
            events = await asyncio.to_thread(self._gate.feed, item.pcm16)
        except Exception:
            self._vad_degraded = True
            return
        for event in events:
            if self._on_activity is not None:
                await self._on_activity(event)
        if SpeechActivityEvent.CANDIDATE_PAUSE in events:
            self._deny_rearm_token = None

    async def _process_without_vad(self, item: _AudioItem) -> None:
        """Keep SmartTurn authoritative when Silero cannot provide candidates."""

        started_now = False
        if not self._fallback_speech_started:
            self._fallback_speech_started = True
            started_now = True
            event = SpeechActivityEvent.SPEECH_STARTED
            if self._on_activity is not None:
                await self._on_activity(event)
            if (
                self._on_scoped_activity is not None
                and item.detector_identity is not None
            ):
                await self._on_scoped_activity(event, item.detector_identity)
            await self._coordinator.on_activity_event(event)
        self._fallback_audio_bytes += len(item.pcm16)
        if started_now:
            return
        # 16 kHz PCM16 mono is 32 bytes per millisecond; accumulate bytes so
        # sub-millisecond frames still advance the periodic-evaluation clock.
        if self._fallback_audio_bytes < self._fallback_evaluation_interval_ms * 32:
            return
        self._fallback_audio_bytes = 0
        self._request_evaluation(
            item.identity,
            "periodic_no_vad",
            item.detector_identity,
        )

    def _request_evaluation(
        self,
        identity: _Identity,
        reason: Literal["candidate_pause", "periodic_no_vad", "strict_retry"],
        detector_identity: DetectorIngressIdentity | None = None,
    ) -> None:
        if self._closed or self._failed or identity != self._identity:
            return
        if self._evaluation_task is not None:
            self._smart_turn_coalesced_evaluation_count += 1
            self._reevaluation_requested = True
            self._reevaluation_reason = reason
            return
        coordinator_generation = int(getattr(self._coordinator, "generation", 0))
        activity_seq = int(getattr(self._coordinator, "activity_seq", 0))
        self._emit_pipeline("evaluation_requested", identity, detector_identity, reason=reason)
        self._smart_turn_diagnostics.candidate(reason=reason)

        async def evaluate() -> None:
            started_at = time.perf_counter()
            result: object | None = None
            error: BaseException | None = None
            try:
                result = await self._coordinator.evaluate_buffered()
            except asyncio.CancelledError:
                self._emit_pipeline("evaluation_result", identity, detector_identity, reason=reason, outcome="cancelled")
                return
            except BaseException as exc:
                error = exc
            self._queue.put_control_nowait(
                _EvaluationResultItem(
                    identity=identity,
                    coordinator_generation=coordinator_generation,
                    activity_seq=activity_seq,
                    reason=reason,
                    detector_identity=detector_identity,
                    evaluation_ms=int((time.perf_counter() - started_at) * 1_000),
                    result=result,
                    error=error,
                )
            )

        self._evaluation_task = asyncio.create_task(
            evaluate(), name="asr-smart-turn-evaluation"
        )

    async def _process_evaluation_result(self, item: _EvaluationResultItem) -> None:
        self._smart_turn_evaluation_ms = item.evaluation_ms
        self._evaluation_task = None
        evaluation_tail = tuple(self._evaluation_tail)
        self._evaluation_tail.clear()
        self._evaluation_tail_duration_us = 0
        reevaluate = self._reevaluation_requested
        reevaluation_reason = self._reevaluation_reason or item.reason
        self._reevaluation_requested = False
        self._reevaluation_reason = None
        identity_matches = item.identity == self._identity
        generation_matches = item.coordinator_generation == int(
            getattr(self._coordinator, "generation", item.coordinator_generation)
        )
        activity_matches = item.activity_seq == int(
            getattr(self._coordinator, "activity_seq", item.activity_seq)
        )
        result = item.result
        status = getattr(result, "status", None)
        decision = getattr(result, "decision", None)
        probability = getattr(result, "probability", None)
        if (
            self._closed
            or self._failed
            or not identity_matches
            or not generation_matches
        ):
            diagnostic_outcome = "discarded"
        elif not activity_matches:
            diagnostic_outcome = "superseded"
        elif item.error is not None:
            diagnostic_outcome = "error"
        elif status is EvaluationStatus.OK and decision is TurnDecision.COMPLETE:
            diagnostic_outcome = "complete"
        elif status is EvaluationStatus.OK and decision is TurnDecision.INCOMPLETE:
            diagnostic_outcome = "incomplete"
        elif status is EvaluationStatus.STALE:
            diagnostic_outcome = "stale"
        elif status is EvaluationStatus.UNAVAILABLE:
            diagnostic_outcome = "unavailable"
        elif status is EvaluationStatus.ERROR:
            diagnostic_outcome = "error"
        else:
            diagnostic_outcome = "unknown"
        self._smart_turn_diagnostics.evaluation(
            reason=item.reason,
            outcome=diagnostic_outcome,
            evaluation_ms=item.evaluation_ms,
            probability=probability,
            threshold=getattr(self._coordinator, "evaluation_threshold", None),
        )
        self._emit_pipeline(
            "evaluation_result", item.identity, item.detector_identity,
            reason=item.reason, outcome=diagnostic_outcome, evaluation_ms=item.evaluation_ms,
            identity_matches=identity_matches, generation_matches=generation_matches,
            activity_matches=activity_matches, probability=probability,
        )
        if (
            self._closed
            or self._failed
            or not identity_matches
            or not generation_matches
        ):
            if (
                reevaluate
                and identity_matches
                and not self._closed
                and not self._failed
            ):
                self._request_evaluation(
                    item.identity,
                    reevaluation_reason,
                    self._latest_detector_identity,
                )
            return
        if not activity_matches:
            self._observe_evaluation_tail(evaluation_tail)
            if reevaluate:
                self._request_evaluation(
                    item.identity,
                    reevaluation_reason,
                    self._latest_detector_identity,
                )
            return
        if item.error is not None:
            self._report_failure("runtime_error", "smart_turn")
            return
        if status is EvaluationStatus.STALE:
            self._observe_evaluation_tail(evaluation_tail)
            self._smart_turn_stale_result_count += 1
            if reevaluate:
                self._request_evaluation(
                    item.identity,
                    reevaluation_reason,
                    self._latest_detector_identity,
                )
            return
        if status is EvaluationStatus.OK and decision is TurnDecision.COMPLETE:
            confirmation_seconds = 0.0
            if self._smart_turn_required:
                if item.reason == "candidate_pause":
                    confirmation_seconds = self._candidate_complete_confirmation_seconds
                elif item.reason == "strict_retry":
                    confirmation_seconds = self._strict_complete_confirmation_seconds
            if confirmation_seconds > 0:
                self._emit_pipeline(
                    "confirmation_wait", item.identity, item.detector_identity,
                    reason=item.reason, confirmation_ms=int(confirmation_seconds * 1000),
                )
                if (
                    self._closed
                    or self._failed
                    or item.identity != self._identity
                    or self._evaluation_task is not None
                    or self._coordinator.state is not CoordinatorState.PAUSE_CANDIDATE
                ):
                    return
                self._schedule_complete_confirmation(
                    item.identity,
                    item.detector_identity,
                    item.reason,
                    probability=probability,
                    delay_seconds=confirmation_seconds,
                    evaluation_tail=evaluation_tail,
                )
                return
            await self._publish_complete_result(
                item.identity,
                item.detector_identity,
                item.reason,
                probability=probability,
                evaluation_tail=evaluation_tail,
            )
            return
        if status is EvaluationStatus.OK and decision is TurnDecision.INCOMPLETE:
            self._observe_evaluation_tail(evaluation_tail)
            if reevaluate:
                self._request_evaluation(
                    item.identity,
                    reevaluation_reason,
                    self._latest_detector_identity,
                )
                return
            if item.reason != "periodic_no_vad":
                if self._smart_turn_required and self._strict_endpoint_deadline is None:
                    self._strict_endpoint_deadline = (
                        asyncio.get_running_loop().time()
                        + self._max_endpoint_wait_seconds
                    )
                self._schedule_fallback(item.identity, "semantic_incomplete")
            return
        if self._smart_turn_required:
            failure_kind = (
                "unavailable"
                if status is EvaluationStatus.UNAVAILABLE
                else "runtime_error"
            )
            self._report_failure(failure_kind, "smart_turn")
            return
        self._enter_semantic_degraded()
        self._schedule_fallback(item.identity, "semantic_degraded")

    def _observe_accepted_audio(self, item: _AudioItem) -> None:
        callback = self._on_accepted_audio
        if callback is None:
            return
        try:
            callback(item.pcm16, 16_000, item.detector_identity)
        except Exception:
            return

    def _attribute_accepted_audio(self, item: _AudioItem) -> None:
        self._observe_accepted_audio(item)
        self._smart_turn_audio_evidence.accepted_audio(
            identity=item.identity,
            pcm16=item.pcm16,
        )

    def _observe_evaluation_tail(self, items: tuple[_AudioItem, ...]) -> None:
        for item in items:
            self._attribute_accepted_audio(item)

    def _retain_confirmation_audio(self, item: _AudioItem) -> bool:
        pending = self._pending_complete_confirmation
        candidate = pending.detector_identity if pending is not None else None
        incoming = item.detector_identity
        if (
            pending is None
            or candidate is None
            or incoming is None
            or item.identity != pending.identity
            or incoming.detector_epoch != candidate.detector_epoch
            or incoming.sequence_no <= candidate.sequence_no
        ):
            return False
        self._confirmation_tail.append(item)
        self._confirmation_tail_duration_us += item.duration_us
        return True

    def _clear_confirmation_audio(self) -> tuple[_AudioItem, ...]:
        items = tuple(self._confirmation_tail)
        self._confirmation_tail.clear()
        self._confirmation_tail_duration_us = 0
        return items

    def _complete_observed_candidate(
        self,
        detector_identity: DetectorIngressIdentity | None,
    ) -> None:
        callback = self._on_candidate_complete
        if callback is None:
            return
        try:
            callback(detector_identity)
        except Exception:
            return

    async def _process_reset(
        self,
        identity: _Identity,
        *,
        requester: asyncio.Task[object] | None = None,
    ) -> None:
        if self._identity is not None and identity < self._identity:
            # A newer reset already jumped the control queue and owns the
            # current turn; a stale one must not roll the identity back or
            # cancel the newer turn's work.
            return
        self._emit_pipeline("reset", self._identity, self._latest_detector_identity, outcome="retired")
        self._cancel_fallback(attribute_confirmation_tail=False)
        self._cancel_smart_turn_unload()
        # Invalidate callbacks before awaiting their cancellation. A callback
        # may suppress CancelledError, but it must still observe the new turn.
        self._identity = identity
        evaluation_task, self._evaluation_task = self._evaluation_task, None
        if evaluation_task is not None:
            evaluation_task.cancel()
        callback_tasks = tuple(
            task for task in self._callback_tasks if task is not requester
        )
        for task in callback_tasks:
            task.cancel()
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        self._reevaluation_requested = False
        self._reevaluation_reason = None
        self._strict_endpoint_deadline = None
        self._latest_detector_identity = None
        self._evaluation_tail.clear()
        self._evaluation_tail_duration_us = 0
        self._clear_confirmation_audio()
        self._successor_audio_fence = None
        self._smart_turn_audio_evidence.discard()
        await self._coordinator.reset()
        await asyncio.to_thread(self._gate.reset)
        self._deny_rearm_token = None
        self._commit_dispatched.clear()
        self._fallback_speech_started = False
        self._fallback_audio_bytes = 0
        if self._smart_turn_pin_count == 0:
            self._schedule_smart_turn_unload(identity)

    async def _process_deny_rearm(self, token: tuple[int, int, int]) -> bool:
        if self._closed or self._failed or self._vad_degraded:
            return False
        if self._deny_rearm_token == token:
            return True
        if not self._vad_load_attempted:
            self._vad_load_attempted = True
            try:
                self._vad_available = bool(await asyncio.to_thread(self._vad.load))
            except Exception:
                self._vad_available = False
        if not self._vad_available:
            self._vad_degraded = True
            return False
        prepare = getattr(self._gate, "prepare_post_deny_silence_boundary", None)
        if not callable(prepare):
            return False
        try:
            await asyncio.to_thread(prepare)
        except Exception:
            return False
        self._deny_rearm_token = token
        return True

    async def _process_close(self) -> None:
        await self._publish_pending_complete_before_close()
        self._closed = True
        self._cancel_fallback()
        await self._close_resources()

    async def _close_resources(self) -> None:
        if self._resources_closed:
            return
        self._resources_closed = True
        self._cancel_smart_turn_unload()
        evaluation_task, self._evaluation_task = self._evaluation_task, None
        if evaluation_task is not None:
            evaluation_task.cancel()
        await self._coordinator.close()
        await asyncio.to_thread(self._vad.close)
        for task in tuple(self._callback_tasks):
            task.cancel()
        if self._callback_tasks:
            await asyncio.gather(*self._callback_tasks, return_exceptions=True)
        if evaluation_task is not None:
            await asyncio.gather(evaluation_task, return_exceptions=True)
        await self._smart_turn_diagnostics.close()
        await self._smart_turn_audio_evidence.close()

    def _schedule_fallback(
        self,
        identity: _Identity,
        reason: _FallbackReason,
    ) -> None:
        self._emit_pipeline("fallback_wait", identity, self._latest_detector_identity, reason=reason)
        self._cancel_fallback()

        async def fallback() -> None:
            if reason == "semantic_incomplete" and self._smart_turn_required:
                await self._strict_incomplete_wait(identity)
                return
            await asyncio.sleep(self._continuation_timeout_seconds)
            state_matches = (
                self._coordinator.state is CoordinatorState.WAIT_CONTINUATION
                if reason == "semantic_incomplete"
                else self._semantic_degraded
                and self._coordinator.state is CoordinatorState.PAUSE_CANDIDATE
            )
            if (
                not self._closed
                and not self._failed
                and identity == self._identity
                and state_matches
            ):
                self._dispatch_commit(identity, self._latest_detector_identity)

        self._fallback_task = asyncio.create_task(
            fallback(), name="asr-voice-turn-fallback"
        )

    def _schedule_complete_confirmation(
        self,
        identity: _Identity,
        detector_identity: DetectorIngressIdentity | None,
        reason: Literal["candidate_pause", "periodic_no_vad", "strict_retry"],
        *,
        probability: float | None,
        delay_seconds: float,
        evaluation_tail: tuple[_AudioItem, ...] = (),
    ) -> None:
        self._cancel_fallback()
        pending = _PendingCompleteConfirmation(
            identity=identity,
            detector_identity=detector_identity,
            reason=reason,
            probability=probability,
        )
        self._pending_complete_confirmation = pending
        for item in evaluation_tail:
            if pending.detector_identity is None and item.detector_identity is None:
                # The internal ASR path has no completion fence that can move
                # these samples to a successor identity. Attribute them now so
                # the deferred evaluation interval cannot leave an evidence gap.
                self._attribute_accepted_audio(item)
            else:
                self._retain_confirmation_audio(item)

        async def confirm_complete() -> None:
            await asyncio.sleep(delay_seconds)
            self._queue.put_control_nowait(_CompleteConfirmationItem(pending))

        self._fallback_task = asyncio.create_task(
            confirm_complete(), name=f"asr-voice-turn-{reason}-complete-confirm"
        )

    async def _publish_pending_complete_before_close(self) -> None:
        pending = self._pending_complete_confirmation
        if pending is None:
            return
        task = self._fallback_task
        self._fallback_task = None
        if task is not None:
            task.cancel()
        await self._publish_pending_complete_confirmation(
            pending,
            require_pause_candidate=False,
            wait_for_commit=True,
        )

    async def _publish_pending_complete_confirmation(
        self,
        pending: _PendingCompleteConfirmation,
        *,
        require_pause_candidate: bool = True,
        wait_for_commit: bool = False,
    ) -> bool:
        if self._pending_complete_confirmation is not pending:
            return False
        current_task = asyncio.current_task()
        if current_task is self._fallback_task:
            self._fallback_task = None
        self._pending_complete_confirmation = None
        confirmation_tail = self._clear_confirmation_audio()
        if (
            self._closed
            or self._failed
            or pending.identity != self._identity
            or self._evaluation_task is not None
            or (
                require_pause_candidate
                and self._coordinator.state is not CoordinatorState.PAUSE_CANDIDATE
            )
        ):
            if (
                not self._closed
                and not self._failed
                and pending.identity == self._identity
                and self._evaluation_task is None
            ):
                self._observe_evaluation_tail(confirmation_tail)
            return False
        await self._publish_complete_result(
            pending.identity,
            pending.detector_identity,
            pending.reason,
            probability=pending.probability,
            evaluation_tail=confirmation_tail,
            wait_for_commit=wait_for_commit,
        )
        return True

    async def _strict_incomplete_wait(self, identity: _Identity) -> None:
        """Schedule one strict retry through the single SmartTurn lane."""

        await asyncio.sleep(self._continuation_timeout_seconds)
        if (
            self._closed
            or self._failed
            or identity != self._identity
            or self._coordinator.state is not CoordinatorState.WAIT_CONTINUATION
        ):
            return
        deadline = self._strict_endpoint_deadline
        if deadline is None or asyncio.get_running_loop().time() >= deadline:
            self._report_failure("unavailable", "smart_turn")
            return
        self._request_evaluation(
            identity,
            "strict_retry",
            self._latest_detector_identity,
        )

    async def _publish_complete_result(
        self,
        identity: _Identity,
        detector_identity: DetectorIngressIdentity | None,
        reason: Literal["candidate_pause", "periodic_no_vad", "strict_retry"],
        *,
        probability: float | None,
        evaluation_tail: tuple[_AudioItem, ...],
        wait_for_commit: bool = False,
    ) -> None:
        self._strict_endpoint_deadline = None
        self._emit_pipeline("completion", identity, detector_identity, reason=reason, outcome="complete")
        self._smart_turn_diagnostics.complete(reason=reason)
        self._complete_observed_candidate(detector_identity)
        active_identity = identity
        if self._on_completion_fence is not None and detector_identity is not None:
            active_identity = self._on_completion_fence(
                *identity,
                detector_identity,
            )
        if active_identity == identity:
            self._observe_evaluation_tail(evaluation_tail)
        self._smart_turn_audio_evidence.complete(
            identity=identity,
            reason=reason,
            probability=probability,
            threshold=getattr(self._coordinator, "evaluation_threshold", None),
        )
        if active_identity != identity:
            await self._process_reset(
                active_identity,
                requester=asyncio.current_task(),
            )
            self._successor_audio_fence = (
                identity,
                detector_identity.sequence_no,
                active_identity,
            )
        callback_tasks_before = tuple(self._callback_tasks)
        completion_published = self._dispatch_commit(
            identity,
            detector_identity,
            active_identity=active_identity,
        )
        if wait_for_commit and completion_published is not None:
            commit_tasks = tuple(
                task
                for task in self._callback_tasks
                if task not in callback_tasks_before
            )
            if commit_tasks:
                await asyncio.wait(
                    commit_tasks,
                    timeout=_COMMIT_DRAIN_ON_CLOSE_SECONDS,
                )
        elif completion_published is not None:
            _ = await completion_published
        if active_identity == identity:
            return
        for tail_item in evaluation_tail:
            await self._process_audio(
                _AudioItem(
                    identity=active_identity,
                    pcm16=tail_item.pcm16,
                    duration_us=tail_item.duration_us,
                    detector_identity=tail_item.detector_identity,
                )
            )

    def _cancel_fallback(self, *, attribute_confirmation_tail: bool = True) -> None:
        self._pending_complete_confirmation = None
        confirmation_tail = self._clear_confirmation_audio()
        if attribute_confirmation_tail:
            self._observe_evaluation_tail(confirmation_tail)
        task = self._fallback_task
        self._fallback_task = None
        if task is not None:
            task.cancel()

    def _schedule_smart_turn_unload(self, identity: _Identity) -> None:
        if self._smart_turn_pin_count > 0:
            return

        async def unload_after_warm_ttl() -> None:
            try:
                await asyncio.sleep(self._smart_turn_warm_seconds)
                if self._closed or self._failed or identity != self._identity:
                    return
                unload = getattr(self._coordinator, "unload_predictor", None)
                if callable(unload):
                    await unload()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("ASR SmartTurn idle unload failed")

        self._smart_turn_unload_task = asyncio.create_task(
            unload_after_warm_ttl(),
            name="asr-smart-turn-idle-unload",
        )

    def _cancel_smart_turn_unload(self) -> None:
        task = self._smart_turn_unload_task
        self._smart_turn_unload_task = None
        if task is not None:
            task.cancel()

    def _enter_semantic_degraded(self) -> None:
        if self._semantic_degraded:
            return
        self._semantic_degraded = True
        logger.warning(
            "ASR Smart Turn unavailable; using Silero-only endpointing for this session"
        )

    def _report_failure(
        self,
        kind: Literal["unavailable", "runtime_error"],
        stage: Literal["vad_load", "vad_feed", "smart_turn", "consumer"],
    ) -> None:
        if self._failed or self._closed:
            return
        self._failed = True
        self._failure = _VoiceTurnFailure(kind, stage)
        self._emit_pipeline(stage, self._identity, self._latest_detector_identity, outcome=kind)
        self._smart_turn_diagnostics.failure(kind=kind, stage=stage)
        self._cancel_fallback(attribute_confirmation_tail=False)
        self._cancel_smart_turn_unload()
        current_task = asyncio.current_task()
        for task in tuple(self._callback_tasks):
            if task is not current_task:
                task.cancel()
        failure_future = self._failure_future
        if failure_future is None:
            failure_future = asyncio.get_running_loop().create_future()
            self._failure_future = failure_future
        if not failure_future.done():
            failure_future.set_result(self._failure)

    def _emit_pipeline(self, phase, identity, detector_identity, *, probability=None, **fields) -> None:
        """Emit only immutable identities and model-decision metadata, never PCM."""
        try:
            callback = getattr(self, "_on_pipeline_diagnostic", None)
            if callback is None or identity is None or detector_identity is None:
                return
            from ..pipeline_diagnostics import safe_fields

            fields.update(
                phase=phase, semantic_generation=identity[0], semantic_buffer_epoch=identity[1],
                semantic_turn_id=identity[2], detector_epoch=detector_identity.detector_epoch,
                sequence_no=detector_identity.sequence_no, queue_audio_ms=self.queued_audio_ms,
                coalesced_count=self._smart_turn_coalesced_evaluation_count,
            )
            # SmartTurn completion probability is not a speaker similarity.
            for name, value in (("probability_milli", probability),
                                ("threshold_milli", getattr(self._coordinator, "evaluation_threshold", None))):
                if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
                    fields[name] = round(value * 1000)
            callback(safe_fields(fields), detector_identity.ingress_token)
        except Exception:
            pass

    def _dispatch_commit(
        self,
        identity: _Identity,
        detector_identity: DetectorIngressIdentity | None = None,
        *,
        active_identity: _Identity | None = None,
    ) -> asyncio.Future[None] | None:
        expected_identity = active_identity or identity
        if self._closed or expected_identity != self._identity:
            return None
        if identity in self._commit_dispatched:
            return None
        self._commit_dispatched.add(identity)
        completion_published = asyncio.get_running_loop().create_future()

        async def commit() -> None:
            try:
                if self._closed or self._failed or expected_identity != self._identity:
                    return
                if self._on_scoped_commit is not None and detector_identity is not None:
                    await self._on_scoped_commit(*identity, detector_identity)
                    if (
                        self._closed
                        or self._failed
                        or expected_identity != self._identity
                    ):
                        return
                if not completion_published.done():
                    completion_published.set_result(None)
                if self._closed or self._failed or expected_identity != self._identity:
                    return
                await self._on_commit(*identity)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._report_failure("runtime_error", "consumer")
            finally:
                if not completion_published.done():
                    completion_published.set_result(None)

        task = asyncio.create_task(commit(), name="asr-voice-turn-commit")
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)
        return completion_published


def _create_voice_turn_adapter(
    on_commit: Callable[[int, int, int], Awaitable[None]],
    *,
    on_activity: Callable[[SpeechActivityEvent], Awaitable[None]] | None = None,
    smart_turn_required: bool = False,
) -> _VoiceTurnAdapter:
    config = SmartTurnConfig(enabled=True)
    vad = SileroVad(
        enabled=True,
        inference_error_limit=config.inference_error_limit,
    )
    gate = SileroActivityGate(vad, config)
    predictor = SmartTurnV3(
        enabled=True,
        inference_error_limit=config.inference_error_limit,
    )
    coordinator = TurnCoordinator(predictor, config)
    return _VoiceTurnAdapter(
        vad=vad,
        gate=gate,
        coordinator=coordinator,
        on_commit=on_commit,
        on_activity=on_activity,
        candidate_complete_confirmation_seconds=(
            config.candidate_complete_confirmation_seconds
        ),
        smart_turn_required=smart_turn_required,
    )


@dataclass(frozen=True, slots=True)
class DetectorFeedResult:
    events: tuple[SpeechActivityEvent, ...]
    throttle_available: bool
    endpointing_available: bool = True
    throttle_action: ThrottleAction | None = None
    identity: DetectorIngressIdentity | None = None
    candidate: DetectorCandidateKey | None = None


@dataclass(frozen=True, slots=True)
class ProviderSpeakerEvidenceLease:
    """Opaque authority for one continuous Provider speaker-evidence stream.

    Provider utterance keys and physical boundary segments may rotate while
    this lease remains current.  The private owner fence prevents handles from
    another DetectorRuntime from being replayed here.
    """

    detector_epoch: int
    lease_generation: int
    candidate: SpeakerShadowCandidateKey
    _owner: object


class ProviderSpeakerEvidenceAnchorStatus(Enum):
    """Settlement of one canonical Provider speech-start anchor."""

    PENDING = "pending"
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ProviderSpeakerEvidenceAnchorResult:
    status: ProviderSpeakerEvidenceAnchorStatus
    lease: ProviderSpeakerEvidenceLease
    candidate: SpeakerShadowCandidateKey
    detector_epoch: int
    timeline_generation: int
    lease_generation: int
    anchor_revision: int
    anchor_start_sample_16k: int
    buffer_origin_sample_16k: int
    observed_through_sample_16k: int
    pcm_through_sequence_no: int | None
    shadow_runtime_generation: int


@dataclass(frozen=True, slots=True)
class ProviderExactSpeakerIntervalReservation:
    """Opaque, one-shot Detector reservation for one exact Provider range."""

    boundary: ProviderAudioRange
    target_candidate: SpeakerShadowCandidateKey
    suffix_candidate: SpeakerShadowCandidateKey | None
    detector_epoch: int
    timeline_generation: int
    lease_generation: int
    candidate_generation: int
    shadow_runtime_generation: int
    anchor_revision: int
    anchor_start_sample_16k: int
    provider_pcm_through_sequence_no: int
    _owner: object
    _token: object
    source_candidate: SpeakerShadowCandidateKey | None = None
    score_reusable: bool = True


@dataclass(frozen=True, slots=True)
class ProviderExactSpeakerIntervalCommitResult:
    """Committed exact snapshot and its optional continuing evidence owner."""

    snapshot: ProviderSpeakerBoundarySnapshot
    target_candidate: SpeakerShadowCandidateKey
    successor_evidence_lease: ProviderSpeakerEvidenceLease | None
    score_reusable: bool = True
    provider_pcm_through_sequence_no: int | None = None
    observed_through_sample_16k: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderSpeakerEvidenceUpdate:
    """One ordered capture result plus its no-progress clock position."""

    lease: ProviderSpeakerEvidenceLease
    capture: SpeakerShadowCaptureResult
    sequence_no: int
    last_progress_at: float


class ProviderSpeakerEvidenceSettlementStatus(Enum):
    RETIRED = "retired"
    ALREADY_RETIRED = "already_retired"
    LIVE = "live"
    CONFLICT = "conflict"
    UNPROVEN = "unproven"


@dataclass(frozen=True, slots=True)
class ProviderSpeakerEvidenceSettlement:
    """Detector-issued physical retirement; never an admission decision."""

    lease: ProviderSpeakerEvidenceLease
    detector_epoch: int
    timeline_generation: int
    operation_serial: int
    status: ProviderSpeakerEvidenceSettlementStatus
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderAudioAccountingReceipt:
    """Ordered local PCM observation, without speaker capture authority."""

    detector_epoch: int
    timeline_generation: int
    sequence_no: int
    start_sample_16k: int
    end_sample_16k: int
    evidence_settlement: ProviderSpeakerEvidenceSettlement | None = None


class SmartTurnReadiness(Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    UNLOADING = "unloading"


@dataclass(slots=True)
class SmartTurnLease:
    token: VoiceTurnToken
    _runtime: "DetectorRuntime"
    _lease_id: int
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        await self._runtime.release_endpointing(self.token, self._lease_id)
        self._released = True


@dataclass(frozen=True, slots=True)
class DetectorCandidateRejectionLease:
    """Revocable synchronous authority for one exact detector candidate."""

    candidate: DetectorCandidateKey
    shadow_candidate: SpeakerShadowCandidateKey
    turn_token: VoiceTurnToken
    _runtime: "DetectorRuntime"
    provider_fence: ProviderCandidateFence | None = None
    provider_preseal_verdict: ProviderSpeakerPresealVerdict | None = None

    def belongs_to(self, runtime: object) -> bool:
        """Return whether this lease was issued by ``runtime``."""

        return runtime is self._runtime

    def commit(self) -> bool:
        """Invalidate candidate authority without yielding the event loop."""

        return self._runtime._commit_candidate_rejection(self)

    async def commit_async(
        self,
        *,
        deadline: float | None = None,
    ) -> "DetectorCandidateRejectionCommitResult":
        """Revalidate and apply this stable authority under the detector lock."""

        return await self._runtime._commit_candidate_rejection_async(
            self,
            deadline=deadline,
        )


class DetectorCandidateRejectionCommitResult(Enum):
    """Phase reached by one stable Provider candidate rejection lease."""

    ACTIVE_APPLIED = "active_applied"
    PRESEAL_READY = "preseal_ready"
    SEALED_APPLIED = "sealed_applied"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class _SealedProviderCandidateRejection:
    """Exact post-seal speaker authority retained until Provider final."""

    provider_fence: ProviderCandidateFence
    candidate: DetectorCandidateKey
    shadow_candidate: SpeakerShadowCandidateKey
    turn_token: VoiceTurnToken
    rejection_ready: bool = False


@dataclass(slots=True)
class _ProviderSpeakerPresealEntry:
    """One detector-owned exact verdict or targeted unknown tombstone."""

    verdict: ProviderSpeakerPresealVerdict
    shadow_candidate: SpeakerShadowCandidateKey | None
    reconciliation: (
        SpeakerShadowBatchReconcileReceipt | SpeakerShadowTerminalCoverageReceipt | None
    )
    rejection_ready: bool = False
    revoked: bool = False
    successor_evidence_lease: ProviderSpeakerEvidenceLease | None = None
    completed: bool = False


@dataclass(slots=True)
class _ProviderSpeakerSegment:
    """PCM-free ownership record for one physical Provider segment."""

    candidate: SpeakerShadowCandidateKey | None
    detector_candidate: DetectorCandidateKey
    first_identity: DetectorIngressIdentity
    last_identity: DetectorIngressIdentity
    created_at: float
    ownership_complete: bool
    shadow_capture_state: "_ProviderShadowCaptureState"
    shadow_completed_window_sample_count: int
    last_progress_at: float
    deferred: bool
    deferred_accepted: bool
    start_sample_16k: int
    end_sample_16k: int
    tentative: bool
    ownership_ambiguous: bool = False

    @property
    def evidence_complete(self) -> bool:
        """Compatibility view; ownership and capture are stored independently."""

        return bool(
            self.ownership_complete
            and self.shadow_capture_state is not _ProviderShadowCaptureState.UNAVAILABLE
        )


class _ProviderShadowCaptureState(Enum):
    COLLECTING = "collecting"
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class _ProviderSpeakerEvidenceState:
    """Detector-owned mutable state behind one opaque evidence lease."""

    lease: ProviderSpeakerEvidenceLease
    timeline_generation: int
    start_sample_16k: int
    buffer_origin_sample_16k: int
    last_sequence_no: int | None = None
    last_progress_at: float | None = None
    cumulative_sample_count: int = 0
    completed_window_sample_count: int = 0
    capture_state: _ProviderShadowCaptureState = _ProviderShadowCaptureState.COLLECTING
    binding_published: bool = False
    anchor_revision: int = 0
    anchor_start_sample_16k: int | None = None
    pending_anchor_start_sample_16k: int | None = None
    anchor_observed_through_sample_16k: int | None = None
    anchor_pcm_through_sequence_no: int | None = None
    anchor_receipt: SpeakerShadowDeferredAnchorReceipt | None = None
    coverage_candidates: list["_ProviderSpeakerCoverageCandidate"] = field(
        default_factory=list
    )
    active_candidate: SpeakerShadowCandidateKey | None = None


@dataclass(slots=True)
class _ProviderSpeakerCoverageCandidate:
    candidate: SpeakerShadowCandidateKey
    start_sample_16k: int
    end_sample_16k: int


@dataclass(slots=True)
class _ProviderExactSpeakerIntervalRecord:
    reservation: ProviderExactSpeakerIntervalReservation
    state: _ProviderSpeakerEvidenceState
    shadow_control: SpeakerShadowExactIntervalControl | SpeakerShadowPreparedTerminalCoverageControl
    shadow_receipt: SpeakerShadowBatchReconcileReceipt | SpeakerShadowTerminalCoverageReceipt
    segment_fingerprint: tuple[tuple[int, ...], ...]
    coverage_fingerprint: tuple[tuple[object, int, int], ...]
    prepared_cursor_16k: int
    next_shadow_generation: int


@dataclass(slots=True)
class _ProviderMicroEventAggregate:
    """Probability-free evidence owned by one detector candidate generation."""

    candidate: DetectorCandidateKey
    window_count: int = 0
    onset_window_count: int = 0
    offset_window_count: int = 0
    ambiguous_window_count: int = 0
    first_onset_window_index: int | None = None
    last_onset_window_index: int | None = None
    post_confirmation_onset_window_count: int = 0
    speech_started_count: int = 0
    speech_resumed_count: int = 0
    candidate_pause_count: int = 0
    event_count: int = 0
    event_sequence_valid: bool = True
    last_event: SpeechActivityEvent | None = None
    silero_evidence_complete: bool = True
    rnnoise_evidence_complete: bool = True
    rnnoise_current_active_run_upper_bound_ms: int = 0
    rnnoise_longest_active_run_upper_bound_ms: int = 0
    candidate_local_start_kind: str | None = None

    def observe_silero(
        self,
        result: SileroFeedResult | None,
        events: tuple[SpeechActivityEvent, ...],
    ) -> None:
        normalize_initial_resume = bool(
            self.event_count == 0
            and events
            and events[0] is SpeechActivityEvent.SPEECH_RESUMED
        )
        if result is None:
            self.silero_evidence_complete = False
        else:
            integer_counts = (
                result.window_count,
                result.onset_window_count,
                result.offset_window_count,
                result.ambiguous_window_count,
                result.post_confirmation_onset_window_count,
            )
            indices_valid = (
                result.first_onset_window_index is None
                and result.last_onset_window_index is None
            ) or (
                type(result.first_onset_window_index) is int
                and type(result.last_onset_window_index) is int
            )
            if (
                any(type(value) is not int or value < 0 for value in integer_counts)
                or not indices_valid
            ):
                self.silero_evidence_complete = False
                result = None
        if result is not None:
            window_offset = self.window_count
            self.window_count += result.window_count
            self.onset_window_count += result.onset_window_count
            self.offset_window_count += result.offset_window_count
            self.ambiguous_window_count += result.ambiguous_window_count
            self.post_confirmation_onset_window_count += max(
                0,
                result.post_confirmation_onset_window_count
                - int(normalize_initial_resume),
            )
            if result.first_onset_window_index is not None:
                global_first = window_offset + result.first_onset_window_index
                global_last = window_offset + result.last_onset_window_index
                if self.first_onset_window_index is None:
                    self.first_onset_window_index = global_first
                self.last_onset_window_index = global_last

        for event in events:
            if self.event_count == 0:
                event_valid = event in {
                    SpeechActivityEvent.SPEECH_STARTED,
                    SpeechActivityEvent.SPEECH_RESUMED,
                }
                if event is SpeechActivityEvent.SPEECH_STARTED:
                    self.candidate_local_start_kind = "speech_started"
                elif event is SpeechActivityEvent.SPEECH_RESUMED:
                    self.candidate_local_start_kind = (
                        "speech_resumed_at_candidate_boundary"
                    )
            elif self.last_event in {
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.SPEECH_RESUMED,
            }:
                event_valid = event is SpeechActivityEvent.CANDIDATE_PAUSE
            else:
                event_valid = event is SpeechActivityEvent.SPEECH_RESUMED
            if not event_valid:
                self.event_sequence_valid = False
            self.event_count += 1
            self.last_event = event
            if event is SpeechActivityEvent.SPEECH_STARTED:
                self.speech_started_count += 1
            elif event is SpeechActivityEvent.SPEECH_RESUMED:
                self.speech_resumed_count += 1
            elif event is SpeechActivityEvent.CANDIDATE_PAUSE:
                self.candidate_pause_count += 1

    def observe_rnnoise(
        self,
        evidence: RnnoiseEvidence,
        *,
        onset_threshold: float,
        chunk_duration_ms: int,
    ) -> None:
        complete = bool(
            evidence.available
            and evidence.frame_count > 0
            and evidence.peak is not None
        )
        if not complete:
            self.rnnoise_evidence_complete = False
            self.rnnoise_current_active_run_upper_bound_ms = 0
            return
        if evidence.peak >= onset_threshold:
            self.rnnoise_current_active_run_upper_bound_ms += chunk_duration_ms
            self.rnnoise_longest_active_run_upper_bound_ms = max(
                self.rnnoise_longest_active_run_upper_bound_ms,
                self.rnnoise_current_active_run_upper_bound_ms,
            )
        else:
            self.rnnoise_current_active_run_upper_bound_ms = 0

    def freeze(self) -> ProviderMicroEventEvidence:
        candidate_local_micro_event = bool(
            self.event_sequence_valid
            and self.event_count == 2
            and self.speech_started_count + self.speech_resumed_count == 1
            and self.candidate_pause_count == 1
        )
        events: tuple[SpeechActivityEvent, ...] = (
            (
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.CANDIDATE_PAUSE,
            )
            if candidate_local_micro_event
            else ()
        )
        silero = None
        if self.silero_evidence_complete and self.window_count > 0:
            silero = SileroFeedResult(
                events=events,
                window_count=self.window_count,
                onset_window_count=self.onset_window_count,
                offset_window_count=self.offset_window_count,
                ambiguous_window_count=self.ambiguous_window_count,
                first_onset_window_index=self.first_onset_window_index,
                last_onset_window_index=self.last_onset_window_index,
                post_confirmation_onset_window_count=(
                    self.post_confirmation_onset_window_count
                ),
            )
        return ProviderMicroEventEvidence(
            silero=silero,
            rnnoise_evidence_complete=self.rnnoise_evidence_complete,
            rnnoise_longest_active_run_upper_bound_ms=(
                self.rnnoise_longest_active_run_upper_bound_ms
                if self.rnnoise_evidence_complete
                else None
            ),
            speech_started_count=self.speech_started_count,
            speech_resumed_count=self.speech_resumed_count,
            candidate_pause_count=self.candidate_pause_count,
            event_sequence_valid=self.event_sequence_valid,
            candidate_local_start_kind=self.candidate_local_start_kind,
        )


@dataclass(frozen=True, slots=True)
class _SealedProviderMicroEvent:
    provider_fence: ProviderCandidateFence
    evidence: ProviderMicroEventEvidence
    decision: ProviderMicroEventDecision


class DetectorRuntime:
    """Serialize Silero loading and inference without owning an ASR session."""

    def __init__(
        self,
        *,
        vad: SileroVad | None = None,
        gate: SileroActivityGate | None = None,
        rnnoise_onset_probability: float = 0.35,
        resource_optimization_enabled: bool = True,
        provider_policy: AsrProviderPolicy | None = None,
        coordinator: TurnCoordinator | None = None,
        throttle_policy: VoiceThrottlePolicy | None = None,
        provider_micro_event_config: ProviderMicroEventConfig | None = None,
        speaker_shadow: SpeakerShadowObserver | None = None,
        speaker_owner_generation: str | None = None,
        on_speaker_candidate_bound: (
            Callable[
                [SpeakerShadowCandidateKey, VoiceTurnToken, str | None],
                None,
            ]
            | None
        ) = None,
        on_turn_complete: Callable[[], Awaitable[None]] | None = None,
        on_endpointing_failure: Callable[[], Awaitable[None]] | None = None,
        on_event: Callable[[DetectorEvent], Awaitable[None]] | None = None,
    ) -> None:
        if not 0.0 <= rnnoise_onset_probability <= 1.0:
            raise ValueError("RNNoise onset probability must be within [0, 1]")
        if vad is None:
            config = SmartTurnConfig(enabled=True)
            vad = SileroVad(
                enabled=True,
                inference_error_limit=config.inference_error_limit,
            )
            gate = SileroActivityGate(vad, config)
        if gate is None:
            raise ValueError("DetectorRuntime gate is required with a custom VAD")
        self._vad = vad
        self._gate = gate
        self._lock = asyncio.Lock()
        self._provider_audio_observation_event = asyncio.Event()
        self._load_attempted = False
        self._available = True
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._rnnoise_onset_probability = rnnoise_onset_probability
        self._resource_optimization_enabled = bool(resource_optimization_enabled)
        self._throttle_policy = throttle_policy or VoiceThrottlePolicy(
            resource_optimization_enabled=self._resource_optimization_enabled,
            bootstrap_onset=rnnoise_onset_probability,
        )
        self._policy_event_candidate: DetectorCandidateKey | None = None
        self._speech_active = False
        self._events: list[SpeechActivityEvent] = []
        self._semantic_adapter: _VoiceTurnAdapter | None = None
        self._semantic_coordinator: TurnCoordinator | None = None
        self._semantic_started = False
        self._semantic_generation = 0
        self._deferred_completion_identity_advanced = False
        self._semantic_turn_id = 1
        self._on_endpointing_failure = on_endpointing_failure
        self._on_turn_complete = on_turn_complete
        self._on_event = on_event
        self._on_speaker_candidate_bound = on_speaker_candidate_bound
        self._defer_turn_complete = False
        self._deferred_turn_complete = False
        self._failure_watch_task: asyncio.Task[None] | None = None
        self._smart_turn_readiness = SmartTurnReadiness.UNLOADED
        self._smart_turn_token: VoiceTurnToken | None = None
        self._smart_turn_generation_sequence = 0
        self._smart_turn_generation: int | None = None
        self._smart_turn_lease_sequence = 0
        self._smart_turn_lease_ids: set[int] = set()
        self._prepare_task: asyncio.Task[bool] | None = None
        self._prepare_token: VoiceTurnToken | None = None
        self._prepare_epoch: int | None = None
        self._prepare_waiters: dict[asyncio.Task[bool], int] = {}
        self._prepare_generations: dict[asyncio.Task[bool], int] = {}
        self._overflow_reset_task: asyncio.Task[None] | None = None
        self._detector_epoch = 0
        self._sequence_no = 0
        self._ingress_token: VoiceIngressToken | None = None
        self._deny_rearm_token: tuple[int, int, int] | None = None
        self._deny_rearm_requested_token: tuple[int, int, int] | None = None
        self._candidate_open = False
        self._candidate_generation = 0
        self._bound_turns: dict[DetectorCandidateKey, BoundDetectorTurn] = {}
        self._deferred_completions: dict[
            DetectorCandidateKey, DetectorIngressIdentity
        ] = {}
        self._completion_fences: dict[
            tuple[int, int, int], SmartTurnCompletionFence
        ] = {}
        self._provider_candidate_fence: ProviderCandidateFence | None = None
        self._sealed_provider_candidate_rejection: (
            _SealedProviderCandidateRejection | None
        ) = None
        self._provider_micro_event_policy = ProviderMicroEventPolicy(
            provider_micro_event_config
        )
        self._provider_micro_event_enabled = (
            self._provider_micro_event_policy.config.mode != "off"
        )
        self._provider_micro_event_aggregate: _ProviderMicroEventAggregate | None = None
        self._sealed_provider_micro_event: _SealedProviderMicroEvent | None = None
        self._provider_speaker_segments: deque[_ProviderSpeakerSegment] = deque()
        self._provider_audio_sample_cursor_16k = 0
        self._provider_audio_timeline_generation = 0
        self._provider_boundary_snapshot_owner = object()
        self._provider_boundary_snapshots: dict[
            int, ProviderSpeakerBoundarySnapshot
        ] = {}
        self._provider_preseal_entries: dict[int, _ProviderSpeakerPresealEntry] = {}
        # Sealing consumes admission authority, but cleanup must retain the
        # original receipt until its explicit completion or revocation.
        self._provider_boundary_completion_entries: dict[
            int, _ProviderSpeakerPresealEntry
        ] = {}
        self._provider_segment_last_sequence_no: int | None = None
        self._provider_speaker_sealed_through_sequence_no: int | None = None
        self._provider_segment_ordered_mode = False
        self._provider_segment_deferred_support: (
            bool | Literal["unsupported", "error"] | None
        ) = None
        self._provider_legacy_segment_evidence_complete = True
        self._provider_segment_successor_evidence_incomplete = False
        self._provider_segment_alignment_lost = False
        self._provider_segment_expiry_task: asyncio.Task[None] | None = None
        self._provider_segment_retired_expiry_tasks: set[asyncio.Task[None]] = set()
        self._provider_speaker_evidence_owner = object()
        self._provider_speaker_evidence_generation = 0
        self._provider_speaker_evidence_settlement_serial = 0
        self._provider_speaker_evidence_settlements: dict[
            int, tuple[ProviderSpeakerEvidenceSettlement, ProviderSpeakerEvidenceSettlement]
        ] = {}
        self._provider_speaker_evidence_state: _ProviderSpeakerEvidenceState | None = (
            None
        )
        self._provider_exact_interval_owner = object()
        self._provider_exact_interval_records: dict[
            object, _ProviderExactSpeakerIntervalRecord
        ] = {}
        self._provider_micro_event_ambiguous_candidates: set[DetectorCandidateKey] = (
            set()
        )
        self._provider_discarded_through_sequence_no: int | None = None
        self._speaker_shadow = speaker_shadow
        self._speaker_owner_generation = speaker_owner_generation
        self._speaker_shadow_generation = 0
        self._speaker_shadow_candidate: SpeakerShadowCandidateKey | None = None
        self._speaker_candidate_turn_bindings: dict[
            SpeakerShadowCandidateKey,
            VoiceTurnToken,
        ] = {}
        self._speaker_candidate_owner_generations: dict[
            SpeakerShadowCandidateKey,
            str | None,
        ] = {}
        self._speaker_shadow_suppressed_candidate: (
            tuple[int, SpeakerShadowScope] | None
        ) = None
        self._speaker_rejection_prepare_diagnostics = {
            "rejection_prepare_type_mismatch_count": 0,
            "rejection_prepare_detector_closed_count": 0,
            "rejection_prepare_candidate_closed_count": 0,
            "rejection_prepare_closed_no_sealed_count": 0,
            "rejection_prepare_closed_fence_mismatch_count": 0,
            "rejection_prepare_closed_shadow_mismatch_count": 0,
            "rejection_seal_snapshot_created_count": 0,
            "rejection_seal_snapshot_missing_shadow_count": 0,
            "rejection_seal_snapshot_invalid_shadow_count": 0,
            "rejection_seal_snapshot_unbound_count": 0,
            "rejection_provisional_query_count": 0,
            "rejection_provisional_pending_count": 0,
            "rejection_provisional_stale_count": 0,
            "rejection_complete_cleared_snapshot_count": 0,
            "rejection_prepare_epoch_mismatch_count": 0,
            "rejection_prepare_shadow_mismatch_count": 0,
            "rejection_prepare_unbound_count": 0,
            "detector_feed_closed_count": 0,
            "detector_feed_unavailable_count": 0,
            "detector_feed_semantic_identity_omitted_count": 0,
            "detector_vad_load_unavailable_count": 0,
            "detector_vad_load_exception_count": 0,
            "detector_gate_exception_count": 0,
            "micro_event_candidate_count": 0,
            "micro_event_evidence_complete_count": 0,
            "micro_event_evidence_unavailable_count": 0,
            "micro_event_would_suppress_count": 0,
            "micro_event_fail_open_count": 0,
            "micro_event_stale_fence_count": 0,
            "micro_event_rnnoise_unavailable_count": 0,
            "provider_speaker_segment_split_count": 0,
            "provider_speaker_segment_deferred_count": 0,
            "provider_speaker_segment_activated_count": 0,
            "provider_speaker_segment_expired_count": 0,
            "provider_speaker_segment_sequence_stale_count": 0,
            "provider_speaker_segment_sequence_gap_count": 0,
            "provider_speaker_segment_overflow_fail_open_count": 0,
            "provider_speaker_segment_ownership_ambiguous_count": 0,
            "provider_speaker_segment_exact_snapshot_count": 0,
            "provider_speaker_segment_exact_reconcile_failed_count": 0,
            "provider_speaker_segment_unknown_retired_count": 0,
            "provider_speaker_segment_merged_resume_count": 0,
            "provider_speaker_segment_sample_split_count": 0,
            "provider_preseal_verdict_stored_count": 0,
            "provider_preseal_verdict_consumed_count": 0,
            "provider_preseal_verdict_stale_count": 0,
            "provider_rejection_ready_count": 0,
            "provider_rejection_applied_count": 0,
            "provider_rejection_fail_open_count": 0,
            "provider_targeted_retirement_count": 0,
            "provider_namespace_poison_count": 0,
            "provider_speaker_evidence_lease_opened_count": 0,
            "provider_speaker_evidence_lease_finished_count": 0,
            "provider_speaker_evidence_lease_abandoned_count": 0,
            "provider_speaker_evidence_lease_stale_count": 0,
        }
        if (
            provider_policy is not None
            and provider_policy.endpoint_authority == "smart_turn"
        ):
            if on_turn_complete is None and on_event is None:
                raise ValueError(
                    "SmartTurn DetectorRuntime requires a completion consumer"
                )
            config = SmartTurnConfig(enabled=True)
            semantic_coordinator = coordinator or TurnCoordinator(
                SmartTurnV3(
                    enabled=True,
                    inference_error_limit=config.inference_error_limit,
                ),
                config,
            )
            candidate_complete_confirmation_seconds = (
                config.candidate_complete_confirmation_seconds
                if isinstance(semantic_coordinator, TurnCoordinator)
                else 0.0
            )
            self._semantic_coordinator = semantic_coordinator

            def completion_fence(
                generation: int,
                buffer_epoch: int,
                turn_id: int,
                identity: DetectorIngressIdentity,
            ) -> _Identity:
                successor_present = self._sequence_no > identity.sequence_no
                fence = SmartTurnCompletionFence(
                    detector_epoch=identity.detector_epoch,
                    candidate_generation=self._candidate_generation,
                    through_sequence_no=identity.sequence_no,
                    semantic_generation=generation,
                    semantic_turn_id=turn_id,
                    successor_candidate_generation=self._candidate_generation + 1,
                    successor_present=successor_present,
                )
                self._completion_fences[(generation, buffer_epoch, turn_id)] = fence
                self._semantic_generation += 1
                self._semantic_turn_id += 1
                self._candidate_generation = fence.successor_candidate_generation
                self._policy_event_candidate = None
                if not successor_present:
                    self._candidate_open = False
                    self._throttle_policy.reset_candidate_activity()
                return (
                    self._semantic_generation,
                    buffer_epoch,
                    self._semantic_turn_id,
                )

            async def commit(generation: int, buffer_epoch: int, turn_id: int) -> None:
                fence = self._completion_fences.pop(
                    (generation, buffer_epoch, turn_id),
                    None,
                )
                if fence is None:
                    self._candidate_open = False
                    self._policy_event_candidate = None
                    self._throttle_policy.reset_candidate_activity()
                if self._defer_turn_complete:
                    self._deferred_turn_complete = True
                    self._deferred_completion_identity_advanced = fence is not None
                    return
                # 当前轮 seal 后立即把检测身份推进到下一轮。旧 provider final
                # 到达前，新语音只做本地语义判断，完成信号延迟发布。
                self._defer_turn_complete = True
                if fence is None:
                    self._semantic_generation += 1
                    self._semantic_turn_id += 1
                    self._candidate_generation += 1
                    adapter = self._semantic_adapter
                    if adapter is not None:
                        await adapter.reset(
                            generation=self._semantic_generation,
                            buffer_epoch=0,
                            utterance_id=self._semantic_turn_id,
                        )
                if on_turn_complete is not None:
                    await on_turn_complete()

            async def activity(event: SpeechActivityEvent) -> None:
                self._throttle_policy.observe_silero(event)
                self._events.append(event)

            async def scoped_activity(
                event: SpeechActivityEvent,
                identity: DetectorIngressIdentity,
            ) -> None:
                if (
                    self._on_event is None
                    or identity.detector_epoch != self._detector_epoch
                ):
                    return
                await self._on_event(
                    DetectorActivityEvent(
                        ingress=identity,
                        candidate=DetectorCandidateKey(
                            identity.detector_epoch,
                            self._candidate_generation,
                        ),
                        activity=event,
                    )
                )

            async def scoped_commit(
                generation: int,
                buffer_epoch: int,
                turn_id: int,
                identity: DetectorIngressIdentity,
            ) -> None:
                if (
                    self._on_event is None
                    or identity.detector_epoch != self._detector_epoch
                ):
                    return
                fence = self._completion_fences.get((generation, buffer_epoch, turn_id))
                candidate = (
                    fence.candidate
                    if fence is not None
                    else DetectorCandidateKey(
                        identity.detector_epoch,
                        self._candidate_generation,
                    )
                )
                if not await self._publish_bound_completion(candidate, identity):
                    self._deferred_completions[candidate] = identity

            self._semantic_adapter = _VoiceTurnAdapter(
                vad=self._vad,
                gate=self._gate,
                coordinator=semantic_coordinator,
                on_commit=commit,
                on_completion_fence=completion_fence,
                on_activity=activity,
                on_scoped_commit=scoped_commit,
                on_scoped_activity=scoped_activity,
                on_accepted_audio=self._observe_smart_turn_speaker_shadow,
                on_candidate_complete=self._finish_smart_turn_speaker_shadow,
                candidate_complete_confirmation_seconds=(
                    candidate_complete_confirmation_seconds
                ),
                smart_turn_required=True,
            )

    def set_pipeline_diagnostic_callback(self, callback) -> None:
        """Install the session-owned observer; provider endpointing needs no adapter."""
        if self._semantic_adapter is not None:
            self._semantic_adapter._on_pipeline_diagnostic = callback

    @property
    def smart_turn_readiness(self) -> SmartTurnReadiness:
        return self._smart_turn_readiness

    @property
    def detector_epoch(self) -> int:
        return self._detector_epoch

    @property
    def candidate_open(self) -> bool:
        return self._candidate_open

    @property
    def throttle_shadow_metrics(self) -> ThrottleShadowMetrics:
        return self._throttle_policy.shadow_metrics

    @property
    def queued_audio_ms(self) -> int:
        adapter = self._semantic_adapter
        return adapter.queued_audio_ms if adapter is not None else 0

    @property
    def smart_turn_evaluation_ms(self) -> int:
        adapter = self._semantic_adapter
        return adapter.smart_turn_evaluation_ms if adapter is not None else 0

    @property
    def smart_turn_stale_result_count(self) -> int:
        adapter = self._semantic_adapter
        return adapter.smart_turn_stale_result_count if adapter is not None else 0

    @property
    def smart_turn_coalesced_evaluation_count(self) -> int:
        adapter = self._semantic_adapter
        return (
            adapter.smart_turn_coalesced_evaluation_count if adapter is not None else 0
        )

    async def bind_candidate(
        self,
        candidate: DetectorCandidateKey,
        turn_token: VoiceTurnToken,
    ) -> BoundDetectorTurn | None:
        if (
            self._closed
            or candidate.detector_epoch != self._detector_epoch
            or (
                candidate.candidate_generation != self._candidate_generation
                and candidate not in self._deferred_completions
            )
        ):
            return None
        existing = self._bound_turns.get(candidate)
        if existing is not None:
            shadow_candidate = next(
                (
                    segment.candidate
                    for segment in self._provider_speaker_segments
                    if segment.detector_candidate == candidate
                    and segment.candidate is not None
                ),
                self._speaker_shadow_candidate,
            )
            if shadow_candidate is not None:
                self._publish_speaker_candidate_binding(
                    shadow_candidate,
                    candidate,
                )
            return existing if existing.turn_token == turn_token else None
        bound = BoundDetectorTurn(candidate, turn_token)
        self._bound_turns[candidate] = bound
        shadow_candidate = next(
            (
                segment.candidate
                for segment in self._provider_speaker_segments
                if segment.detector_candidate == candidate
                and segment.candidate is not None
            ),
            None,
        )
        if (
            shadow_candidate is None
            and candidate.candidate_generation == self._candidate_generation
        ):
            shadow_candidate = self._speaker_shadow_candidate
        if shadow_candidate is not None:
            self._publish_speaker_candidate_binding(
                shadow_candidate,
                candidate,
            )
        deferred = self._deferred_completions.pop(candidate, None)
        if deferred is not None:
            await self._publish_bound_completion(candidate, deferred)
        return bound

    def _publish_speaker_candidate_binding(
        self,
        shadow_candidate: SpeakerShadowCandidateKey,
        detector_candidate: DetectorCandidateKey,
    ) -> None:
        bound = self._bound_turns.get(detector_candidate)
        if bound is None:
            return
        existing = self._speaker_candidate_turn_bindings.get(shadow_candidate)
        if existing is not None:
            # A candidate identity is immutable. Never let a delayed Provider
            # split or stale detector candidate rebind it to another turn.
            return
        if shadow_candidate not in self._speaker_candidate_owner_generations:
            # Replacement/reset revoked this observer generation before its
            # delayed detector binding could publish. Never relabel it with
            # the currently installed verifier generation.
            return
        owner_generation = self._speaker_candidate_owner_generations[shadow_candidate]
        self._speaker_candidate_turn_bindings = {
            **self._speaker_candidate_turn_bindings,
            shadow_candidate: bound.turn_token,
        }
        callback = self._on_speaker_candidate_bound
        if callback is None:
            return
        try:
            callback(shadow_candidate, bound.turn_token, owner_generation)
        except Exception:
            # Binding remains available through the synchronous lookup seam.
            # An optional early publication failure must not break endpointing.
            return

    def _mark_provider_micro_event_ambiguous(
        self,
        candidate: DetectorCandidateKey,
    ) -> None:
        self._provider_micro_event_ambiguous_candidates.add(candidate)
        aggregate = self._provider_micro_event_aggregate
        if aggregate is not None and aggregate.candidate == candidate:
            aggregate.silero_evidence_complete = False
            aggregate.rnnoise_evidence_complete = False

    def _mark_provider_segments_incomplete(self) -> None:
        for segment in self._provider_speaker_segments:
            segment.ownership_complete = False
            self._mark_provider_segment_ownership_ambiguous(segment)
            self._mark_provider_micro_event_ambiguous(segment.detector_candidate)

    def _mark_provider_segment_ownership_ambiguous(
        self,
        segment: _ProviderSpeakerSegment,
    ) -> None:
        if segment.ownership_ambiguous:
            return
        segment.ownership_ambiguous = True
        self._speaker_rejection_prepare_diagnostics[
            "provider_speaker_segment_ownership_ambiguous_count"
        ] += 1

    def _provider_speaker_evidence_state_for(
        self,
        lease: ProviderSpeakerEvidenceLease,
    ) -> _ProviderSpeakerEvidenceState | None:
        state = self._provider_speaker_evidence_state
        if (
            type(lease) is not ProviderSpeakerEvidenceLease
            or lease._owner is not self._provider_speaker_evidence_owner
            or lease.detector_epoch != self._detector_epoch
            or lease.candidate.detector_epoch != self._detector_epoch
            or state is None
            or state.lease is not lease
        ):
            return None
        return state

    def _unavailable_provider_speaker_evidence_update(
        self,
        state: _ProviderSpeakerEvidenceState,
        *,
        sequence_no: int,
    ) -> ProviderSpeakerEvidenceUpdate:
        return ProviderSpeakerEvidenceUpdate(
            lease=state.lease,
            capture=SpeakerShadowCaptureResult(
                disposition=SpeakerShadowCaptureDisposition.UNAVAILABLE,
                accepted_sample_count=0,
                cumulative_sample_count=state.cumulative_sample_count,
                completed_window_sample_count=state.completed_window_sample_count,
                decision_state=SpeakerShadowCaptureDecisionState.UNAVAILABLE,
            ),
            sequence_no=sequence_no,
            last_progress_at=state.last_progress_at or 0.0,
        )

    def _abandon_provider_speaker_evidence_locked(
        self,
        state: _ProviderSpeakerEvidenceState,
        *,
        reason: str = "abandoned",
    ) -> bool:
        if self._provider_speaker_evidence_state is not state:
            return False
        # Revoke the Detector handle first.  A callback or cleanup failure in
        # the optional observer control cannot make the lease current again.
        self._provider_speaker_evidence_state = None
        self._record_provider_speaker_evidence_retirement(state, reason=reason)
        state.capture_state = _ProviderShadowCaptureState.UNAVAILABLE
        shadow = self._speaker_shadow
        retired = False
        if isinstance(shadow, SpeakerShadowCandidateLifecycleControl):
            for coverage in state.coverage_candidates or (
                _ProviderSpeakerCoverageCandidate(
                    state.lease.candidate,
                    state.start_sample_16k,
                    state.start_sample_16k,
                ),
            ):
                try:
                    retired = bool(shadow.abandon_candidate(coverage.candidate)) or retired
                except Exception:
                    continue
        self._speaker_rejection_prepare_diagnostics[
            "provider_speaker_evidence_lease_abandoned_count"
        ] += 1
        return retired

    def _record_provider_speaker_evidence_retirement(
        self, state: _ProviderSpeakerEvidenceState, *, reason: str,
    ) -> ProviderSpeakerEvidenceSettlement:
        self._provider_speaker_evidence_settlement_serial += 1
        values = dict(
            lease=state.lease, detector_epoch=state.lease.detector_epoch,
            timeline_generation=state.timeline_generation,
            operation_serial=self._provider_speaker_evidence_settlement_serial,
            reason=reason,
        )
        retired = ProviderSpeakerEvidenceSettlement(
            **values, status=ProviderSpeakerEvidenceSettlementStatus.RETIRED,
        )
        confirmed = ProviderSpeakerEvidenceSettlement(
            **values, status=ProviderSpeakerEvidenceSettlementStatus.ALREADY_RETIRED,
        )
        self._provider_speaker_evidence_settlements[state.lease.lease_generation] = (
            retired, confirmed,
        )
        while len(self._provider_speaker_evidence_settlements) > _PROVIDER_SEGMENT_FIFO_LIMIT:
            oldest = next(iter(self._provider_speaker_evidence_settlements))
            self._provider_speaker_evidence_settlements.pop(oldest)
        return retired

    def validate_provider_speaker_evidence_settlement(
        self, settlement: ProviderSpeakerEvidenceSettlement, *,
        lease: ProviderSpeakerEvidenceLease, timeline_generation: int | None = None,
    ) -> bool:
        """Accept retained issued objects only, fenced to this live timeline."""
        if (
            type(settlement) is not ProviderSpeakerEvidenceSettlement
            or type(lease) is not ProviderSpeakerEvidenceLease
            or type(lease.lease_generation) is not int
            or self._closed
            or settlement.lease is not lease
            or lease._owner is not self._provider_speaker_evidence_owner
            or settlement.detector_epoch != self._detector_epoch
            or settlement.timeline_generation != self._provider_audio_timeline_generation
            or (timeline_generation is not None
                and settlement.timeline_generation != timeline_generation)
        ):
            return False
        issued = self._provider_speaker_evidence_settlements.get(lease.lease_generation, ())
        return any(settlement is item for item in issued)

    async def confirm_provider_speaker_evidence_retirement(
        self, lease: ProviderSpeakerEvidenceLease,
    ) -> ProviderSpeakerEvidenceSettlement:
        """Confirm local retirement without acquiring an owner or replaying PCM."""
        async with self._lock:
            issued = self._provider_speaker_evidence_settlements.get(
                lease.lease_generation
                if type(lease) is ProviderSpeakerEvidenceLease
                and type(lease.lease_generation) is int else -1,
                (),
            )
            if issued and self.validate_provider_speaker_evidence_settlement(
                issued[1], lease=lease,
            ):
                return issued[1]
            state = self._provider_speaker_evidence_state
            status = ProviderSpeakerEvidenceSettlementStatus.UNPROVEN
            if (
                type(lease) is ProviderSpeakerEvidenceLease
                and lease._owner is self._provider_speaker_evidence_owner
                and lease.detector_epoch == self._detector_epoch
                and not self._closed
                and state is not None
            ):
                status = (
                    ProviderSpeakerEvidenceSettlementStatus.LIVE
                    if state.lease is lease else ProviderSpeakerEvidenceSettlementStatus.CONFLICT
                )
            return ProviderSpeakerEvidenceSettlement(
                lease=lease, detector_epoch=self._detector_epoch,
                timeline_generation=self._provider_audio_timeline_generation,
                operation_serial=0, status=status, reason="unconfirmed",
            )

    async def ensure_provider_speaker_evidence_lease(
        self,
    ) -> ProviderSpeakerEvidenceLease | None:
        """Return one stable Provider evidence lease until explicit settlement.

        This API is opt-in.  Existing Provider segment ownership keeps its
        legacy candidate-per-segment behavior until callers pass the returned
        handle back to :meth:`observe_provider_audio_ordered`.
        """

        async with self._lock:
            if self._closed or self._semantic_adapter is not None:
                return None
            current = self._provider_speaker_evidence_state
            if current is not None:
                return current.lease
            candidate = self._allocate_provider_segment_candidate()
            if candidate is None:
                return None
            shadow = self._speaker_shadow
            control = (
                shadow
                if isinstance(shadow, SpeakerShadowDeferredCandidateControl)
                else None
            )
            try:
                deferred = bool(control is not None and control.defer_candidate(candidate))
            except Exception:
                deferred = False
            if not deferred:
                self._speaker_candidate_owner_generations.pop(candidate, None)
                return None
            lease = ProviderSpeakerEvidenceLease(
                detector_epoch=self._detector_epoch,
                lease_generation=self._provider_speaker_evidence_generation,
                candidate=candidate,
                _owner=self._provider_speaker_evidence_owner,
            )
            self._provider_speaker_evidence_generation += 1
            state = _ProviderSpeakerEvidenceState(
                lease=lease,
                timeline_generation=self._provider_audio_timeline_generation,
                start_sample_16k=self._provider_audio_sample_cursor_16k,
                buffer_origin_sample_16k=self._provider_audio_sample_cursor_16k,
                # Empty evidence starts at the already-accounted canonical
                # cursor. Its fence names earlier PCM; it grants no samples.
                last_sequence_no=self._provider_segment_last_sequence_no,
                active_candidate=candidate,
            )
            state.coverage_candidates.append(
                _ProviderSpeakerCoverageCandidate(
                    candidate=candidate,
                    start_sample_16k=self._provider_audio_sample_cursor_16k,
                    end_sample_16k=self._provider_audio_sample_cursor_16k,
                )
            )
            self._provider_speaker_evidence_state = state
            self._speaker_rejection_prepare_diagnostics[
                "provider_speaker_evidence_lease_opened_count"
            ] += 1
            return lease

    @staticmethod
    def _provider_speaker_anchor_result(
        state: _ProviderSpeakerEvidenceState,
        *,
        status: ProviderSpeakerEvidenceAnchorStatus,
        anchor_start_sample_16k: int,
        observed_through_sample_16k: int,
        pcm_through_sequence_no: int | None,
        shadow_runtime_generation: int,
    ) -> ProviderSpeakerEvidenceAnchorResult:
        return ProviderSpeakerEvidenceAnchorResult(
            status=status,
            lease=state.lease,
            candidate=state.lease.candidate,
            detector_epoch=state.lease.detector_epoch,
            timeline_generation=state.timeline_generation,
            lease_generation=state.lease.lease_generation,
            anchor_revision=state.anchor_revision,
            anchor_start_sample_16k=anchor_start_sample_16k,
            buffer_origin_sample_16k=state.buffer_origin_sample_16k,
            observed_through_sample_16k=observed_through_sample_16k,
            pcm_through_sequence_no=pcm_through_sequence_no,
            shadow_runtime_generation=shadow_runtime_generation,
        )

    async def anchor_provider_speaker_evidence(
        self,
        lease: ProviderSpeakerEvidenceLease,
        *,
        audio_start_sample_16k: int,
        deadline: float,
    ) -> ProviderSpeakerEvidenceAnchorResult:
        """Align one deferred evidence lease before any checkpoint may score."""

        receipt: SpeakerShadowDeferredAnchorReceipt | None = None
        control: SpeakerShadowDeferredAnchorControl | None = None
        prepared_state: _ProviderSpeakerEvidenceState | None = None
        prepared_cursor = 0
        prepared_sequence: int | None = None
        if (
            type(audio_start_sample_16k) is not int
            or audio_start_sample_16k < 0
            or type(deadline) not in {int, float}
            or not math.isfinite(deadline)
        ):
            raise ValueError("provider speaker anchor arguments are invalid")

        async with self._lock:
            state = self._provider_speaker_evidence_state_for(lease)
            shadow = self._speaker_shadow
            control = (
                shadow
                if isinstance(shadow, SpeakerShadowDeferredAnchorControl)
                else None
            )
            cursor = self._provider_audio_sample_cursor_16k
            shadow_generation = getattr(shadow, "generation", -1)
            if state is None:
                return ProviderSpeakerEvidenceAnchorResult(
                    status=ProviderSpeakerEvidenceAnchorStatus.UNAVAILABLE,
                    lease=lease,
                    candidate=lease.candidate,
                    detector_epoch=lease.detector_epoch,
                    timeline_generation=self._provider_audio_timeline_generation,
                    lease_generation=lease.lease_generation,
                    anchor_revision=0,
                    anchor_start_sample_16k=audio_start_sample_16k,
                    buffer_origin_sample_16k=0,
                    observed_through_sample_16k=cursor,
                    pcm_through_sequence_no=self._provider_segment_last_sequence_no,
                    shadow_runtime_generation=shadow_generation,
                )
            if state.anchor_start_sample_16k is not None:
                status = (
                    ProviderSpeakerEvidenceAnchorStatus.IDEMPOTENT
                    if state.anchor_start_sample_16k == audio_start_sample_16k
                    else ProviderSpeakerEvidenceAnchorStatus.CONFLICT
                )
                result = self._provider_speaker_anchor_result(
                    state,
                    status=status,
                    anchor_start_sample_16k=audio_start_sample_16k,
                    observed_through_sample_16k=(
                        state.anchor_observed_through_sample_16k
                        if state.anchor_observed_through_sample_16k is not None
                        else cursor
                    ),
                    pcm_through_sequence_no=state.anchor_pcm_through_sequence_no,
                    shadow_runtime_generation=shadow_generation,
                )
                if status is ProviderSpeakerEvidenceAnchorStatus.CONFLICT:
                    self._abandon_provider_speaker_evidence_locked(state)
                return result
            if (
                state.pending_anchor_start_sample_16k is not None
                and state.pending_anchor_start_sample_16k != audio_start_sample_16k
            ):
                result = self._provider_speaker_anchor_result(
                    state,
                    status=ProviderSpeakerEvidenceAnchorStatus.CONFLICT,
                    anchor_start_sample_16k=audio_start_sample_16k,
                    observed_through_sample_16k=cursor,
                    pcm_through_sequence_no=state.last_sequence_no,
                    shadow_runtime_generation=shadow_generation,
                )
                self._abandon_provider_speaker_evidence_locked(state)
                return result
            if audio_start_sample_16k > cursor:
                state.pending_anchor_start_sample_16k = audio_start_sample_16k
                return self._provider_speaker_anchor_result(
                    state,
                    status=ProviderSpeakerEvidenceAnchorStatus.PENDING,
                    anchor_start_sample_16k=audio_start_sample_16k,
                    observed_through_sample_16k=cursor,
                    pcm_through_sequence_no=state.last_sequence_no,
                    shadow_runtime_generation=shadow_generation,
                )
            if control is None or audio_start_sample_16k < state.start_sample_16k:
                result = self._provider_speaker_anchor_result(
                    state,
                    status=ProviderSpeakerEvidenceAnchorStatus.UNAVAILABLE,
                    anchor_start_sample_16k=audio_start_sample_16k,
                    observed_through_sample_16k=cursor,
                    pcm_through_sequence_no=state.last_sequence_no,
                    shadow_runtime_generation=shadow_generation,
                )
                self._abandon_provider_speaker_evidence_locked(state)
                return result

            if state.anchor_receipt is None:
                state.anchor_revision += 1
                try:
                    receipt = control.anchor_deferred_candidate(
                        SpeakerShadowDeferredAnchorRequest(
                            candidate=state.lease.candidate,
                            expected_observed_sample_count=(
                                cursor - state.start_sample_16k
                            ),
                            discard_prefix_sample_count=(
                                audio_start_sample_16k - state.start_sample_16k
                            ),
                            anchor_revision=state.anchor_revision,
                        )
                    )
                except Exception:
                    receipt = None
                if receipt is None:
                    result = self._provider_speaker_anchor_result(
                        state,
                        status=ProviderSpeakerEvidenceAnchorStatus.UNAVAILABLE,
                        anchor_start_sample_16k=audio_start_sample_16k,
                        observed_through_sample_16k=cursor,
                        pcm_through_sequence_no=state.last_sequence_no,
                        shadow_runtime_generation=shadow_generation,
                    )
                    self._abandon_provider_speaker_evidence_locked(state)
                    return result
                state.anchor_receipt = receipt
                state.pending_anchor_start_sample_16k = audio_start_sample_16k
                state.anchor_observed_through_sample_16k = cursor
                # Zero denotes an empty anchored timeline, without imposing
                # a synthetic prior dispatch sequence on its first PCM frame.
                state.anchor_pcm_through_sequence_no = state.last_sequence_no or 0
            else:
                receipt = state.anchor_receipt
            prepared_state = state
            prepared_cursor = (
                state.anchor_observed_through_sample_16k
                if state.anchor_observed_through_sample_16k is not None
                else cursor
            )
            prepared_sequence = state.anchor_pcm_through_sequence_no

        assert prepared_state is not None and control is not None and receipt is not None
        try:
            status = await control.wait_deferred_anchor_settled(
                receipt,
                deadline=float(deadline),
            )
        except Exception:
            status = "failed"

        async with self._lock:
            state = self._provider_speaker_evidence_state_for(lease)
            if (
                state is not prepared_state
                or state.timeline_generation != self._provider_audio_timeline_generation
                or state.anchor_receipt is not receipt
                or state.pending_anchor_start_sample_16k != audio_start_sample_16k
            ):
                return self._provider_speaker_anchor_result(
                    prepared_state,
                    status=ProviderSpeakerEvidenceAnchorStatus.UNAVAILABLE,
                    anchor_start_sample_16k=audio_start_sample_16k,
                    observed_through_sample_16k=prepared_cursor,
                    pcm_through_sequence_no=prepared_sequence,
                    shadow_runtime_generation=receipt.runtime_generation,
                )
            if status == "pending":
                return self._provider_speaker_anchor_result(
                    state,
                    status=ProviderSpeakerEvidenceAnchorStatus.PENDING,
                    anchor_start_sample_16k=audio_start_sample_16k,
                    observed_through_sample_16k=prepared_cursor,
                    pcm_through_sequence_no=prepared_sequence,
                    shadow_runtime_generation=receipt.runtime_generation,
                )
            if status != "applied":
                result = self._provider_speaker_anchor_result(
                    state,
                    status=ProviderSpeakerEvidenceAnchorStatus.UNAVAILABLE,
                    anchor_start_sample_16k=audio_start_sample_16k,
                    observed_through_sample_16k=prepared_cursor,
                    pcm_through_sequence_no=prepared_sequence,
                    shadow_runtime_generation=receipt.runtime_generation,
                )
                self._abandon_provider_speaker_evidence_locked(state)
                return result

            state.anchor_start_sample_16k = audio_start_sample_16k
            state.pending_anchor_start_sample_16k = None
            state.start_sample_16k = audio_start_sample_16k
            state.cumulative_sample_count = (
                self._provider_audio_sample_cursor_16k - audio_start_sample_16k
            )
            if state.coverage_candidates:
                primary = state.coverage_candidates[0]
                primary.start_sample_16k = audio_start_sample_16k
                primary.end_sample_16k = self._provider_audio_sample_cursor_16k
            while self._provider_speaker_segments and (
                self._provider_speaker_segments[0].end_sample_16k
                <= audio_start_sample_16k
            ):
                self._provider_speaker_segments.popleft()
            if self._provider_speaker_segments:
                self._provider_speaker_segments[0].start_sample_16k = (
                    audio_start_sample_16k
                )
            return self._provider_speaker_anchor_result(
                state,
                status=ProviderSpeakerEvidenceAnchorStatus.APPLIED,
                anchor_start_sample_16k=audio_start_sample_16k,
                observed_through_sample_16k=prepared_cursor,
                pcm_through_sequence_no=prepared_sequence,
                shadow_runtime_generation=receipt.runtime_generation,
            )

    async def finish_provider_speaker_evidence_lease(
        self,
        lease: ProviderSpeakerEvidenceLease,
    ) -> bool:
        """Queue completion confirmation for one exact current lease."""

        async with self._lock:
            state = self._provider_speaker_evidence_state_for(lease)
            if state is None:
                self._speaker_rejection_prepare_diagnostics[
                    "provider_speaker_evidence_lease_stale_count"
                ] += 1
                return False
            shadow = self._speaker_shadow
            if shadow is None:
                self._abandon_provider_speaker_evidence_locked(state)
                return False
            try:
                accepted = bool(shadow.finish_candidate(lease.candidate))
            except Exception:
                accepted = False
            if not accepted:
                self._abandon_provider_speaker_evidence_locked(state)
                return False
            if isinstance(shadow, SpeakerShadowCandidateLifecycleControl):
                for coverage in state.coverage_candidates[1:]:
                    try:
                        shadow.abandon_candidate(coverage.candidate)
                    except Exception:
                        pass
            self._provider_speaker_evidence_state = None
            self._record_provider_speaker_evidence_retirement(state, reason="finished")
            self._speaker_rejection_prepare_diagnostics[
                "provider_speaker_evidence_lease_finished_count"
            ] += 1
            return True

    async def abandon_provider_speaker_evidence_lease(
        self,
        lease: ProviderSpeakerEvidenceLease,
    ) -> bool:
        """Revoke a current lease without manufacturing speaker evidence."""

        async with self._lock:
            state = self._provider_speaker_evidence_state_for(lease)
            if state is None:
                self._speaker_rejection_prepare_diagnostics[
                    "provider_speaker_evidence_lease_stale_count"
                ] += 1
                return False
            return self._abandon_provider_speaker_evidence_locked(state)

    @staticmethod
    def _provider_exact_interval_segment_fingerprint(
        segments: tuple[_ProviderSpeakerSegment, ...],
    ) -> tuple[tuple[int, ...], ...]:
        return tuple(
            (
                id(segment),
                segment.start_sample_16k,
                segment.end_sample_16k,
                segment.first_identity.sequence_no,
                segment.last_identity.sequence_no,
                int(segment.ownership_complete),
                int(segment.ownership_ambiguous),
                int(segment.shadow_capture_state.value == "unavailable"),
                int(segment.candidate is None),
            )
            for segment in segments
        )

    def _abort_provider_exact_interval_record(
        self,
        record: _ProviderExactSpeakerIntervalRecord,
    ) -> bool:
        token = record.reservation._token
        if self._provider_exact_interval_records.get(token) is not record:
            return False
        self._provider_exact_interval_records.pop(token, None)
        try:
            if type(record.shadow_receipt) is SpeakerShadowTerminalCoverageReceipt:
                aborted = bool(
                    record.shadow_control.abort_finalized_candidate_coverage(
                        record.shadow_receipt
                    )
                )
            else:
                aborted = bool(
                    record.shadow_control.abort_exact_interval(record.shadow_receipt)
                )
        except Exception:
            aborted = False
        if not aborted:
            self._abandon_provider_speaker_evidence_locked(record.state)
        state_candidate = record.state.lease.candidate
        for candidate in (
            record.reservation.target_candidate,
            record.reservation.suffix_candidate,
        ):
            if candidate is not None and candidate != state_candidate:
                self._speaker_candidate_owner_generations.pop(candidate, None)
                self._speaker_candidate_turn_bindings.pop(candidate, None)
        return aborted

    def _provider_exact_interval_segments_still_current(
        self,
        record: _ProviderExactSpeakerIntervalRecord,
        segments: tuple[_ProviderSpeakerSegment, ...],
    ) -> bool:
        original = record.segment_fingerprint
        if not original or len(segments) < len(original):
            return False
        for index, expected in enumerate(original):
            segment = segments[index]
            current = self._provider_exact_interval_segment_fingerprint((segment,))[0]
            if current[0:2] != expected[0:2] or current[3] != expected[3]:
                return False
            if index < len(original) - 1:
                if current != expected:
                    return False
            elif (
                current[2] < expected[2]
                or current[4] < expected[4]
                or current[5:] != expected[5:]
            ):
                return False
        for index, segment in enumerate(segments):
            if (
                segment.candidate is not None
                or not segment.evidence_complete
                or segment.ownership_ambiguous
                or segment.end_sample_16k <= segment.start_sample_16k
                or (
                    index > 0
                    and segments[index - 1].end_sample_16k
                    != segment.start_sample_16k
                )
            ):
                return False
        current_coverage = record.state.coverage_candidates
        if len(current_coverage) != len(record.coverage_fingerprint):
            return False
        for index, (candidate, start_sample, end_sample) in enumerate(
            record.coverage_fingerprint
        ):
            current = current_coverage[index]
            if current.candidate != candidate or current.start_sample_16k != start_sample:
                return False
            if (
                index < len(record.coverage_fingerprint) - 1
                and current.end_sample_16k != end_sample
            ) or (
                index == len(record.coverage_fingerprint) - 1
                and current.end_sample_16k < end_sample
            ):
                return False
        return bool(
            segments[0].start_sample_16k == record.state.start_sample_16k
            and segments[-1].end_sample_16k
            == self._provider_audio_sample_cursor_16k
            and self._provider_audio_sample_cursor_16k >= record.prepared_cursor_16k
            and record.state.cumulative_sample_count
            == self._provider_audio_sample_cursor_16k
            - record.state.start_sample_16k
        )

    async def prepare_provider_exact_speaker_interval(
        self,
        boundary: ProviderAudioRange,
        *,
        speaker_evidence_lease: ProviderSpeakerEvidenceLease,
    ) -> ProviderExactSpeakerIntervalReservation | None:
        """Reserve an exact live-evidence interval without publishing work."""

        async with self._lock:
            self._prune_completed_provider_preseals()
            for record in tuple(self._provider_exact_interval_records.values()):
                if (
                    record.reservation.detector_epoch == self._detector_epoch
                    and record.state is self._provider_speaker_evidence_state
                ):
                    return None
                self._abort_provider_exact_interval_record(record)
            if (
                self._closed
                or self._semantic_adapter is not None
                or type(boundary) is not ProviderAudioRange
            ):
                return None
            state = self._provider_speaker_evidence_state_for(
                speaker_evidence_lease
            )
            shadow = self._speaker_shadow
            live_control = (
                shadow
                if isinstance(shadow, SpeakerShadowExactIntervalControl)
                else None
            )
            terminal_control = (
                shadow
                if isinstance(shadow, SpeakerShadowPreparedTerminalCoverageControl)
                else None
            )
            segments = tuple(self._provider_speaker_segments)
            cursor = self._provider_audio_sample_cursor_16k
            if (
                state is None
                or (live_control is None and terminal_control is None)
                or state.capture_state is _ProviderShadowCaptureState.UNAVAILABLE
                or state.anchor_start_sample_16k is None
                or state.pending_anchor_start_sample_16k is not None
                or state.anchor_revision <= 0
                or state.timeline_generation
                != self._provider_audio_timeline_generation
                or not self._provider_segment_ordered_mode
                or self._provider_segment_alignment_lost
                or not segments
                or state.start_sample_16k < 0
                or boundary.start_sample_16k != state.anchor_start_sample_16k
                or boundary.end_sample_16k > cursor
                or state.cumulative_sample_count
                != cursor - state.start_sample_16k
                or len(self._provider_preseal_entries)
                >= _PROVIDER_SEGMENT_FIFO_LIMIT
                or len(self._provider_boundary_completion_entries)
                >= _PROVIDER_SEGMENT_FIFO_LIMIT
            ):
                return None
            if (
                segments[0].start_sample_16k != state.start_sample_16k
                or segments[-1].end_sample_16k != cursor
                or not state.coverage_candidates
                or state.coverage_candidates[0].candidate != state.lease.candidate
                or state.coverage_candidates[0].start_sample_16k
                != state.start_sample_16k
            ):
                return None
            for index, segment in enumerate(segments):
                if (
                    segment.candidate is not None
                    or not segment.evidence_complete
                    or segment.ownership_ambiguous
                    or segment.end_sample_16k <= segment.start_sample_16k
                    or (
                        index > 0
                        and segments[index - 1].end_sample_16k
                        != segment.start_sample_16k
                    )
                ):
                    return None
            for index, coverage in enumerate(state.coverage_candidates):
                if (
                    coverage.end_sample_16k <= coverage.start_sample_16k
                    or (
                        index > 0
                        and state.coverage_candidates[index - 1].end_sample_16k
                        != coverage.start_sample_16k
                    )
                ):
                    return None

            expected_generation = self._candidate_generation
            while expected_generation in self._provider_preseal_entries:
                expected_generation += 1
            if expected_generation != self._candidate_generation:
                return None
            source_candidate = state.lease.candidate
            target_candidate = source_candidate
            allocated: list[SpeakerShadowCandidateKey] = []
            # Always reserve a successor. Provider PCM may arrive while the
            # upper Admission transaction is between prepare and commit; the
            # shadow reservation stages that post-boundary PCM without
            # relabelling the still-current evidence lease.
            suffix_candidate = self._allocate_provider_segment_candidate()
            if suffix_candidate is None:
                for candidate in allocated:
                    self._speaker_candidate_owner_generations.pop(candidate, None)
                return None
            allocated.append(suffix_candidate)
            reconcile_sources: list[SpeakerShadowReconcileSource] = []
            for coverage in state.coverage_candidates:
                if coverage.start_sample_16k >= boundary.end_sample_16k:
                    break
                keep_end = min(
                    coverage.end_sample_16k,
                    boundary.end_sample_16k,
                ) - coverage.start_sample_16k
                reconcile_sources.append(
                    SpeakerShadowReconcileSource(
                        candidate=coverage.candidate,
                        expected_sample_count=(
                            coverage.end_sample_16k - coverage.start_sample_16k
                        ),
                        keep_start_sample=0,
                        keep_end_sample=keep_end,
                    )
                )
                if coverage.end_sample_16k >= boundary.end_sample_16k:
                    break
            if (
                not reconcile_sources
                or sum(
                    source.keep_end_sample - source.keep_start_sample
                    for source in reconcile_sources
                )
                != boundary.end_sample_16k - boundary.start_sample_16k
            ):
                for candidate in allocated:
                    self._speaker_candidate_owner_generations.pop(candidate, None)
                return None

            score_reusable = True
            if (
                live_control is not None
                and len(reconcile_sources) == 1
                and isinstance(shadow, SpeakerShadowExactIntervalScoreControl)
                and shadow.exact_interval_requires_fresh_target(reconcile_sources[0])
            ):
                # An immutable checkpoint extending beyond this exact prefix
                # cannot be relabelled. Split its real PCM into a fresh target
                # instead; minimum duration and score-range guards still apply.
                target_candidate = self._allocate_provider_segment_candidate()
                if target_candidate is None:
                    for candidate in allocated:
                        self._speaker_candidate_owner_generations.pop(candidate, None)
                    return None
                allocated.append(target_candidate)
                score_reusable = False
                # A late boundary can precede an already-completed score and
                # several later buffer-only coverage owners. Retain the full
                # current cursor in one ordered split; post-boundary sources
                # contribute only to the suffix, never to target scoring.
                reconcile_sources = [
                    SpeakerShadowReconcileSource(
                        candidate=coverage.candidate,
                        expected_sample_count=coverage.end_sample_16k - coverage.start_sample_16k,
                        keep_start_sample=0,
                        keep_end_sample=max(0, min(
                            coverage.end_sample_16k, boundary.end_sample_16k,
                        ) - coverage.start_sample_16k),
                    )
                    for coverage in state.coverage_candidates
                ]

            receipt: SpeakerShadowBatchReconcileReceipt | SpeakerShadowTerminalCoverageReceipt | None = None
            control: SpeakerShadowExactIntervalControl | SpeakerShadowPreparedTerminalCoverageControl | None = None
            if (
                terminal_control is not None
                and state.completed_window_sample_count > 0
                and score_reusable
            ):
                try:
                    receipt = terminal_control.prepare_finalized_candidate_coverage(
                        SpeakerShadowTerminalCoverageRequest(
                            sources=tuple(reconcile_sources),
                            target=target_candidate,
                            provider_exact_start_sample=0,
                            provider_exact_end_sample=(
                                boundary.end_sample_16k - boundary.start_sample_16k
                            ),
                            scored_window_start_sample=0,
                            scored_window_end_sample=(
                                state.completed_window_sample_count
                            ),
                            suffix=suffix_candidate,
                        )
                    )
                except Exception:
                    receipt = None
                if type(receipt) is SpeakerShadowTerminalCoverageReceipt:
                    control = terminal_control
            if receipt is None and live_control is not None and (
                len(reconcile_sources) == 1 or not score_reusable
            ):
                try:
                    receipt = live_control.prepare_exact_interval(
                        SpeakerShadowBatchReconcileRequest(
                            sources=tuple(reconcile_sources),
                            target=target_candidate,
                            suffix=suffix_candidate,
                            finish_target=True,
                        )
                    )
                except Exception:
                    receipt = None
                if type(receipt) is SpeakerShadowBatchReconcileReceipt:
                    control = live_control
            expected_target_samples = (
                boundary.end_sample_16k - boundary.start_sample_16k
            )
            expected_suffix_samples = cursor - boundary.end_sample_16k
            receipt_valid = bool(
                (
                    type(receipt) is SpeakerShadowBatchReconcileReceipt
                    and receipt.target_sample_count == expected_target_samples
                    and receipt.suffix_sample_count == expected_suffix_samples
                )
                or (
                    type(receipt) is SpeakerShadowTerminalCoverageReceipt
                    and receipt.covered_sample_count == expected_target_samples
                    and receipt.terminal_preserved
                )
            )
            if not (
                receipt_valid
                and receipt is not None
                and receipt.target == target_candidate
                and receipt.suffix == suffix_candidate
                and control is not None
            ):
                if type(receipt) is SpeakerShadowBatchReconcileReceipt:
                    try:
                        live_control.abort_exact_interval(receipt)
                    except Exception:
                        pass
                elif type(receipt) is SpeakerShadowTerminalCoverageReceipt:
                    try:
                        terminal_control.abort_finalized_candidate_coverage(receipt)
                    except Exception:
                        pass
                for candidate in allocated:
                    self._speaker_candidate_owner_generations.pop(candidate, None)
                return None

            reservation_token = object()
            reservation = ProviderExactSpeakerIntervalReservation(
                boundary=boundary,
                target_candidate=target_candidate,
                suffix_candidate=suffix_candidate,
                detector_epoch=self._detector_epoch,
                timeline_generation=self._provider_audio_timeline_generation,
                lease_generation=state.lease.lease_generation,
                candidate_generation=expected_generation,
                shadow_runtime_generation=receipt.runtime_generation,
                anchor_revision=state.anchor_revision,
                anchor_start_sample_16k=state.anchor_start_sample_16k,
                provider_pcm_through_sequence_no=state.last_sequence_no or 0,
                _owner=self._provider_exact_interval_owner,
                _token=reservation_token,
                source_candidate=source_candidate,
                score_reusable=score_reusable,
            )
            self._provider_exact_interval_records[reservation_token] = (
                _ProviderExactSpeakerIntervalRecord(
                    reservation=reservation,
                    state=state,
                    shadow_control=control,
                    shadow_receipt=receipt,
                    segment_fingerprint=(
                        self._provider_exact_interval_segment_fingerprint(segments)
                    ),
                    coverage_fingerprint=tuple(
                        (
                            coverage.candidate,
                            coverage.start_sample_16k,
                            coverage.end_sample_16k,
                        )
                        for coverage in state.coverage_candidates
                    ),
                    prepared_cursor_16k=cursor,
                    next_shadow_generation=self._speaker_shadow_generation,
                )
            )
            return reservation

    def abort_provider_exact_speaker_interval(
        self,
        reservation: ProviderExactSpeakerIntervalReservation,
    ) -> bool:
        """Abort one unpublished exact reservation without yielding."""

        if (
            type(reservation) is not ProviderExactSpeakerIntervalReservation
            or reservation._owner is not self._provider_exact_interval_owner
        ):
            return False
        record = self._provider_exact_interval_records.get(reservation._token)
        if record is None or record.reservation is not reservation:
            return False
        return self._abort_provider_exact_interval_record(record)

    def commit_provider_exact_speaker_interval(
        self,
        reservation: ProviderExactSpeakerIntervalReservation,
    ) -> ProviderExactSpeakerIntervalCommitResult | None:
        """Commit one prepared exact interval at an await-free linearization."""

        if (
            type(reservation) is not ProviderExactSpeakerIntervalReservation
            or reservation._owner is not self._provider_exact_interval_owner
        ):
            return None
        record = self._provider_exact_interval_records.get(reservation._token)
        if record is None or record.reservation is not reservation:
            return None
        state = record.state
        segments = tuple(self._provider_speaker_segments)
        current = bool(
            not self._closed
            and self._semantic_adapter is None
            and self._provider_speaker_evidence_state is state
            and self._provider_speaker_evidence_state_for(state.lease) is state
            and reservation.detector_epoch == self._detector_epoch
            and reservation.timeline_generation
            == self._provider_audio_timeline_generation
            == state.timeline_generation
            and reservation.lease_generation == state.lease.lease_generation
            and reservation.candidate_generation == self._candidate_generation
            and reservation.shadow_runtime_generation
            == record.shadow_receipt.runtime_generation
            and reservation.anchor_revision == state.anchor_revision
            and reservation.anchor_start_sample_16k
            == state.anchor_start_sample_16k
            and reservation.provider_pcm_through_sequence_no
            <= (state.last_sequence_no or 0)
            and record.next_shadow_generation == self._speaker_shadow_generation
            and record.shadow_control is self._speaker_shadow
            and state.capture_state is not _ProviderShadowCaptureState.UNAVAILABLE
            and self._provider_exact_interval_segments_still_current(record, segments)
            and reservation.candidate_generation
            not in self._provider_preseal_entries
        )
        if not current:
            self._abort_provider_exact_interval_record(record)
            return None

        boundary = reservation.boundary
        last_segment = segments[-1]
        successor_lease: ProviderSpeakerEvidenceLease | None = None
        successor_state: _ProviderSpeakerEvidenceState | None = None
        survivor: _ProviderSpeakerSegment | None = None
        successor_sample_count = 0
        if reservation.suffix_candidate is not None:
            successor_sample_count = (
                self._provider_audio_sample_cursor_16k - boundary.end_sample_16k
            )
            successor_lease = ProviderSpeakerEvidenceLease(
                detector_epoch=self._detector_epoch,
                lease_generation=self._provider_speaker_evidence_generation,
                candidate=reservation.suffix_candidate,
                _owner=self._provider_speaker_evidence_owner,
            )
            successor_state = _ProviderSpeakerEvidenceState(
                lease=successor_lease,
                timeline_generation=state.timeline_generation,
                start_sample_16k=boundary.end_sample_16k,
                buffer_origin_sample_16k=boundary.end_sample_16k,
                last_sequence_no=state.last_sequence_no,
                last_progress_at=state.last_progress_at,
                cumulative_sample_count=(
                    successor_sample_count
                ),
                completed_window_sample_count=0,
                capture_state=_ProviderShadowCaptureState.COLLECTING,
                active_candidate=reservation.suffix_candidate,
            )
            successor_state.coverage_candidates.append(
                _ProviderSpeakerCoverageCandidate(
                    candidate=reservation.suffix_candidate,
                    start_sample_16k=boundary.end_sample_16k,
                    end_sample_16k=self._provider_audio_sample_cursor_16k,
                )
            )
            if successor_sample_count > 0:
                survivor = _ProviderSpeakerSegment(
                    candidate=None,
                    detector_candidate=DetectorCandidateKey(
                        self._detector_epoch,
                        reservation.candidate_generation + 1,
                    ),
                    first_identity=last_segment.first_identity,
                    last_identity=last_segment.last_identity,
                    created_at=last_segment.created_at,
                    ownership_complete=True,
                    shadow_capture_state=_ProviderShadowCaptureState.COLLECTING,
                    shadow_completed_window_sample_count=0,
                    last_progress_at=last_segment.last_progress_at,
                    deferred=False,
                    deferred_accepted=False,
                    start_sample_16k=boundary.end_sample_16k,
                    end_sample_16k=self._provider_audio_sample_cursor_16k,
                    tentative=False,
                )

        snapshot = ProviderSpeakerBoundarySnapshot(
            detector_epoch=self._detector_epoch,
            candidate_generation=reservation.candidate_generation,
            through_sequence_no=record.segment_fingerprint[-1][4],
            shadow_generation=reservation.target_candidate.shadow_generation,
            merged_resume_count=max(0, len(segments) - 1),
            successor_present=successor_sample_count > 0,
            evidence_complete=True,
            _owner=self._provider_boundary_snapshot_owner,
            boundary_exact=True,
        )
        try:
            if type(record.shadow_receipt) is SpeakerShadowTerminalCoverageReceipt:
                committed = bool(
                    record.shadow_control.commit_finalized_candidate_coverage(
                        record.shadow_receipt
                    )
                )
            else:
                committed = bool(
                    record.shadow_control.commit_exact_interval(record.shadow_receipt)
                )
        except Exception:
            committed = False
        if not committed:
            self._abort_provider_exact_interval_record(record)
            return None

        self._provider_exact_interval_records.pop(reservation._token, None)
        self._provider_speaker_evidence_state = successor_state
        self._record_provider_speaker_evidence_retirement(state, reason="exact_transfer")
        # This commit consumes g exactly once.  The published suffix and the
        # next prepare must agree on g+1 before binding callbacks can run.
        self._candidate_generation = reservation.candidate_generation + 1
        if successor_lease is not None:
            self._provider_speaker_evidence_generation += 1
        self._retire_provider_segment_expiry_task()
        self._provider_speaker_segments = deque(
            (survivor,) if survivor is not None else ()
        )
        self._schedule_provider_segment_expiry()
        self._provider_preseal_entries[reservation.candidate_generation] = (
            _ProviderSpeakerPresealEntry(
                verdict=snapshot,
                shadow_candidate=reservation.target_candidate,
                reconciliation=record.shadow_receipt,
                successor_evidence_lease=successor_lease,
            )
        )
        self._provider_boundary_completion_entries[reservation.candidate_generation] = (
            self._provider_preseal_entries[reservation.candidate_generation]
        )
        self._provider_boundary_snapshots[reservation.candidate_generation] = snapshot
        self._publish_speaker_candidate_binding(
            reservation.target_candidate,
            DetectorCandidateKey(
                self._detector_epoch,
                reservation.candidate_generation,
            ),
        )
        self._speaker_rejection_prepare_diagnostics[
            "provider_preseal_verdict_stored_count"
        ] += 1
        self._speaker_rejection_prepare_diagnostics[
            "provider_speaker_segment_merged_resume_count"
        ] += snapshot.merged_resume_count
        if successor_lease is not None:
            self._speaker_rejection_prepare_diagnostics[
                "provider_speaker_segment_sample_split_count"
            ] += 1
        return ProviderExactSpeakerIntervalCommitResult(
            snapshot=snapshot,
            target_candidate=reservation.target_candidate,
            successor_evidence_lease=successor_lease,
            score_reusable=reservation.score_reusable,
            provider_pcm_through_sequence_no=state.last_sequence_no or 0,
            observed_through_sample_16k=self._provider_audio_sample_cursor_16k,
        )

    def _allocate_provider_segment_candidate(
        self,
    ) -> SpeakerShadowCandidateKey | None:
        suppressed = self._speaker_shadow_suppressed_candidate
        if suppressed is not None:
            if suppressed[0] != self._detector_epoch:
                self._speaker_shadow_suppressed_candidate = None
            elif suppressed[1] == "provider_candidate":
                return None
        shadow = self._speaker_shadow
        if shadow is None:
            return None
        try:
            if not shadow.enabled:
                return None
        except Exception:
            return None
        candidate = SpeakerShadowCandidateKey(
            detector_epoch=self._detector_epoch,
            shadow_generation=self._speaker_shadow_generation,
            scope="provider_candidate",
        )
        self._speaker_candidate_owner_generations[candidate] = (
            self._speaker_owner_generation
        )
        # Ordered Provider segments may coexist. Reserve identity at creation,
        # rather than at finish, so a deferred tail can never reuse its head's
        # private observer key.
        self._speaker_shadow_generation += 1
        return candidate

    def _finish_provider_segment(
        self,
        segment: _ProviderSpeakerSegment,
        *,
        activate_deferred: bool = True,
    ) -> bool:
        candidate = segment.candidate
        shadow = self._speaker_shadow
        if candidate is None or shadow is None:
            return False
        accepted = True
        if segment.deferred:
            if not activate_deferred or not segment.deferred_accepted:
                accepted = False
            elif not isinstance(shadow, SpeakerShadowDeferredCandidateControl):
                accepted = False
            else:
                try:
                    accepted = bool(shadow.activate_candidate(candidate))
                except Exception:
                    accepted = False
                if accepted:
                    self._speaker_rejection_prepare_diagnostics[
                        "provider_speaker_segment_activated_count"
                    ] += 1
        try:
            finished = bool(shadow.finish_candidate(candidate))
        except Exception:
            finished = False
        return bool(accepted and finished)

    def _expire_provider_segments(self, now: float) -> None:
        evidence_state = self._provider_speaker_evidence_state
        if (
            evidence_state is not None
            and evidence_state.last_progress_at is not None
            and now - evidence_state.last_progress_at
            >= _PROVIDER_SEGMENT_EXPIRY_SECONDS
        ):
            self._abandon_provider_speaker_evidence_locked(evidence_state)
        expired = False
        while self._provider_speaker_segments and (
            now - self._provider_speaker_segments[0].last_progress_at
            >= _PROVIDER_SEGMENT_EXPIRY_SECONDS
        ):
            segment = self._provider_speaker_segments.popleft()
            segment.ownership_complete = False
            segment.shadow_capture_state = _ProviderShadowCaptureState.UNAVAILABLE
            self._mark_provider_micro_event_ambiguous(segment.detector_candidate)
            self._finish_provider_segment(
                segment,
                activate_deferred=False,
            )
            self._speaker_rejection_prepare_diagnostics[
                "provider_speaker_segment_expired_count"
            ] += 1
            expired = True
        if expired:
            self._provider_segment_alignment_lost = True
            # Once one physical endpoint is missing, FIFO position no longer
            # proves ownership for any surviving segment.
            self._mark_provider_segments_incomplete()

    def _schedule_provider_segment_expiry(self) -> None:
        task = self._provider_segment_expiry_task
        if task is not None and not task.done():
            return
        self._provider_segment_expiry_task = None
        if self._closed or not self._provider_speaker_segments:
            return
        deadline = (
            self._provider_speaker_segments[0].last_progress_at
            + _PROVIDER_SEGMENT_EXPIRY_SECONDS
        )
        delay = max(0.0, deadline - time.monotonic())
        self._provider_segment_expiry_task = asyncio.create_task(
            self._expire_provider_segments_after(delay),
            name="provider-speaker-segment-expiry",
        )

    async def _expire_provider_segments_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._closed:
                    return
                self._expire_provider_segments(time.monotonic())
                self._provider_segment_expiry_task = None
                self._schedule_provider_segment_expiry()
        except asyncio.CancelledError:
            return

    def _retire_provider_segment_expiry_task(self) -> None:
        task = self._provider_segment_expiry_task
        self._provider_segment_expiry_task = None
        if task is None or task is asyncio.current_task() or task.done():
            return
        task.cancel()
        self._provider_segment_retired_expiry_tasks.add(task)
        task.add_done_callback(self._provider_segment_retired_expiry_tasks.discard)

    async def _drain_provider_segment_expiry_tasks(self) -> None:
        tasks = tuple(self._provider_segment_retired_expiry_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)

    def _clear_provider_segment_state(
        self,
        *,
        preserve_ordered_mode: bool = False,
        preserve_last_sequence: bool = False,
        preserve_audio_cursor: bool = False,
    ) -> None:
        self._retire_provider_segment_expiry_task()
        while self._provider_speaker_segments:
            self._finish_provider_segment(
                self._provider_speaker_segments.popleft(),
                activate_deferred=False,
            )
        for entry in tuple(self._provider_preseal_entries.values()):
            self._revoke_provider_preseal_reconciliation_locked(entry)
        for entry in tuple(self._provider_boundary_completion_entries.values()):
            self._revoke_provider_preseal_reconciliation_locked(entry)
        self._provider_preseal_entries.clear()
        self._provider_boundary_completion_entries.clear()
        self._provider_boundary_snapshots.clear()
        self._provider_micro_event_ambiguous_candidates.clear()
        if not preserve_ordered_mode:
            self._provider_segment_ordered_mode = False
            self._provider_segment_deferred_support = None
        self._provider_legacy_segment_evidence_complete = True
        self._provider_segment_successor_evidence_incomplete = False
        self._provider_segment_alignment_lost = False
        if not preserve_last_sequence:
            self._provider_segment_last_sequence_no = None
        if not preserve_audio_cursor:
            self._provider_audio_sample_cursor_16k = 0
            self._provider_audio_timeline_generation += 1
            self._signal_provider_audio_observation()

    def _signal_provider_audio_observation(self) -> None:
        """Wake every cursor waiter without requiring the detector lock.

        The SmartTurn QueueFull path clears Provider state outside ``_lock``.
        Swapping a one-shot Event keeps that legacy lock order intact while
        preventing lost wakeups for waiters that already captured the event.
        """

        observed = self._provider_audio_observation_event
        self._provider_audio_observation_event = asyncio.Event()
        observed.set()

    def _batch_reconciliation_control(
        self,
    ) -> SpeakerShadowBatchReconciliationControl | None:
        shadow = self._speaker_shadow
        return (
            shadow
            if isinstance(shadow, SpeakerShadowBatchReconciliationControl)
            else None
        )

    def _terminal_coverage_control(
        self,
    ) -> SpeakerShadowTerminalCoverageControl | None:
        shadow = self._speaker_shadow
        return (
            shadow if isinstance(shadow, SpeakerShadowTerminalCoverageControl) else None
        )

    def _provider_preseal_reconciliation_status_locked(
        self,
        entry: _ProviderSpeakerPresealEntry,
    ) -> str:
        receipt = entry.reconciliation
        if entry.revoked or receipt is None:
            return "stale"
        try:
            if type(receipt) is SpeakerShadowTerminalCoverageReceipt:
                control = self._terminal_coverage_control()
                return (
                    control.terminal_coverage_status(receipt)
                    if control is not None
                    else "stale"
                )
            if type(receipt) is SpeakerShadowBatchReconcileReceipt:
                control = self._batch_reconciliation_control()
                return (
                    control.reconciliation_status(receipt)
                    if control is not None
                    else "stale"
                )
        except Exception:
            return "stale"
        return "stale"

    def _revoke_provider_preseal_reconciliation_locked(
        self,
        entry: _ProviderSpeakerPresealEntry,
    ) -> None:
        receipt = entry.reconciliation
        if entry.revoked or receipt is None:
            entry.revoked = True
            return
        self._revoke_provider_reconciliation_receipt(receipt)
        entry.revoked = True

    def _revoke_provider_reconciliation_receipt(
        self,
        receipt: (
            SpeakerShadowBatchReconcileReceipt | SpeakerShadowTerminalCoverageReceipt
        ),
    ) -> None:
        """Revoke one receipt and retire only a suffix lease it actually killed."""

        try:
            status = "stale"
            if type(receipt) is SpeakerShadowTerminalCoverageReceipt:
                control = self._terminal_coverage_control()
                if control is not None:
                    control.revoke_terminal_coverage(receipt)
                    status = control.terminal_coverage_status(receipt)
            elif type(receipt) is SpeakerShadowBatchReconcileReceipt:
                control = self._batch_reconciliation_control()
                if control is not None:
                    control.revoke_reconciliation(receipt)
                    status = control.reconciliation_status(receipt)
            state = self._provider_speaker_evidence_state
            if (
                state is not None
                and state.lease.candidate == receipt.suffix
                and status == "failed"
            ):
                # Both exact representations can own a continuing suffix.
                # Completed receipts stay applied and have no revoke authority.
                self._abandon_provider_speaker_evidence_locked(state)
        except Exception:
            pass

    def _unknown_provider_preseal_verdict_locked(
        self,
        candidate_generation: int,
    ) -> ProviderSpeakerPresealVerdict:
        return ProviderSpeakerBoundarySnapshot(
            detector_epoch=self._detector_epoch,
            candidate_generation=candidate_generation,
            through_sequence_no=self._sequence_no,
            shadow_generation=0,
            merged_resume_count=0,
            successor_present=False,
            evidence_complete=False,
            _owner=self._provider_boundary_snapshot_owner,
            boundary_exact=False,
        )

    def _unauthorized_unknown_provider_preseal_verdict_locked(
        self,
        candidate_generation: int,
    ) -> ProviderSpeakerPresealVerdict:
        """Return a stale-safe unknown value that cannot authorize sealing."""

        return ProviderSpeakerBoundarySnapshot(
            detector_epoch=self._detector_epoch,
            candidate_generation=candidate_generation,
            through_sequence_no=self._sequence_no,
            shadow_generation=0,
            merged_resume_count=0,
            successor_present=False,
            evidence_complete=False,
            _owner=object(),
            boundary_exact=False,
        )

    def _retire_unclaimed_provider_segments_locked(self) -> bool:
        had_segments = bool(self._provider_speaker_segments)
        self._retire_provider_segment_expiry_task()
        while self._provider_speaker_segments:
            self._finish_provider_segment(
                self._provider_speaker_segments.popleft(),
                activate_deferred=False,
            )
        self._finish_speaker_shadow_candidate(expected_scope="provider_candidate")
        self._provider_segment_alignment_lost = False
        self._provider_segment_successor_evidence_incomplete = False
        return had_segments

    def _downgrade_provider_preseal_entry_locked(
        self,
        entry: _ProviderSpeakerPresealEntry,
    ) -> ProviderSpeakerPresealVerdict:
        old_verdict = entry.verdict
        if not old_verdict.boundary_exact:
            return old_verdict
        self._revoke_provider_preseal_reconciliation_locked(entry)
        verdict = self._unknown_provider_preseal_verdict_locked(
            old_verdict.candidate_generation
        )
        entry.verdict = verdict
        entry.shadow_candidate = None
        entry.reconciliation = None
        entry.rejection_ready = False
        self._provider_boundary_snapshots.pop(
            old_verdict.candidate_generation,
            None,
        )
        self._mark_provider_micro_event_ambiguous(
            DetectorCandidateKey(
                self._detector_epoch,
                old_verdict.candidate_generation,
            )
        )
        self._speaker_rejection_prepare_diagnostics[
            "provider_speaker_segment_unknown_retired_count"
        ] += 1
        self._speaker_rejection_prepare_diagnostics[
            "provider_rejection_fail_open_count"
        ] += 1
        return verdict

    def _reserve_provider_unknown_preseal_locked(
        self,
    ) -> ProviderSpeakerPresealVerdict:
        self._prune_completed_provider_preseals()
        candidate_generation = self._candidate_generation
        while candidate_generation in self._provider_preseal_entries:
            candidate_generation += 1
        had_segments = self._retire_unclaimed_provider_segments_locked()
        verdict = self._unknown_provider_preseal_verdict_locked(candidate_generation)
        if len(self._provider_preseal_entries) < _PROVIDER_SEGMENT_FIFO_LIMIT:
            self._provider_preseal_entries[candidate_generation] = (
                _ProviderSpeakerPresealEntry(
                    verdict=verdict,
                    shadow_candidate=None,
                    reconciliation=None,
                )
            )
            self._speaker_rejection_prepare_diagnostics[
                "provider_preseal_verdict_stored_count"
            ] += 1
        self._mark_provider_micro_event_ambiguous(
            DetectorCandidateKey(self._detector_epoch, candidate_generation)
        )
        if had_segments:
            self._speaker_rejection_prepare_diagnostics[
                "provider_speaker_segment_unknown_retired_count"
            ] += 1
        return verdict

    def _prune_completed_provider_preseals(self) -> None:
        """Make one slot without evicting pending proof or revoke authority."""
        for generation, entry in tuple(self._provider_preseal_entries.items()):
            if len(self._provider_preseal_entries) < _PROVIDER_SEGMENT_FIFO_LIMIT:
                break
            if (
                entry.completed and entry.revoked and entry.reconciliation is None
                and entry.shadow_candidate is None
                and generation not in self._provider_boundary_completion_entries
                and generation not in self._provider_boundary_snapshots
            ):
                self._provider_preseal_entries.pop(generation)

    async def complete_provider_speaker_boundary(
        self,
        verdict: ProviderSpeakerPresealVerdict,
        *,
        successor_evidence_lease: ProviderSpeakerEvidenceLease | None,
        deadline: float | None = None,
    ) -> Literal[
        "completed", "already_completed", "pending", "stale", "invalid", "unsupported"
    ]:
        """Retire an applied proof without revoking its transferred successor.

        The exact commit stored the *same* successor lease returned to Runtime.
        A later completion may outlive that successor's own next commit; it
        proves the original transfer, not continued ownership of the suffix.
        """

        if deadline is not None:
            if type(deadline) not in {int, float} or not math.isfinite(deadline):
                return "invalid"
            try:
                async with asyncio.timeout_at(deadline):
                    result = await self.complete_provider_speaker_boundary(
                        verdict, successor_evidence_lease=successor_evidence_lease,
                    )
                    if result != "pending":
                        return result
                    async with self._lock:
                        entry = self._provider_boundary_completion_entries.get(
                            verdict.candidate_generation
                        ) or self._provider_preseal_entries.get(verdict.candidate_generation)
                        if (
                            entry is None or entry.verdict is not verdict
                            or entry.successor_evidence_lease is not successor_evidence_lease
                            or entry.revoked
                        ):
                            return "stale"
                        receipt = entry.reconciliation
                        shadow = self._speaker_shadow
                        if not isinstance(shadow, SpeakerShadowReconciliationSettlement):
                            return "unsupported"
                    await shadow.wait_reconciliation_settled(receipt, deadline=deadline)
                    # Re-enter all owner/epoch/receipt checks after the wait.
                    # Timeout and cancellation never turn completion into revoke.
                    return await self.complete_provider_speaker_boundary(
                        verdict, successor_evidence_lease=successor_evidence_lease,
                    )
            except TimeoutError:
                return "pending"

        async with self._lock:
            if (
                self._closed
                or self._semantic_adapter is not None
                or type(verdict) is not ProviderSpeakerBoundarySnapshot
                or verdict._owner is not self._provider_boundary_snapshot_owner
                or verdict.detector_epoch != self._detector_epoch
            ):
                return "stale"
            entry = self._provider_boundary_completion_entries.get(
                verdict.candidate_generation
            ) or self._provider_preseal_entries.get(verdict.candidate_generation)
            if entry is None or entry.verdict is not verdict:
                return "stale"
            if entry.successor_evidence_lease is not successor_evidence_lease:
                return "invalid"
            if entry.completed:
                return "already_completed"
            if entry.revoked:
                return "stale"
            receipt = entry.reconciliation
            if type(receipt) not in {
                SpeakerShadowBatchReconcileReceipt, SpeakerShadowTerminalCoverageReceipt,
            }:
                return "unsupported"
            complete = getattr(self._speaker_shadow, "complete_reconciliation", None)
            if not callable(complete):
                return "unsupported"
            result = complete(
                receipt,
                successor=(
                    successor_evidence_lease.candidate
                    if successor_evidence_lease is not None else None
                ),
            )
            if result not in {"completed", "already_completed"}:
                return result if result in {"pending", "stale", "invalid"} else "invalid"
            # No await separates the ownership check, Shadow completion, and
            # retirement of Detector's old revoke capability.
            entry.completed = True
            entry.revoked = True
            entry.reconciliation = None
            entry.shadow_candidate = None
            entry.rejection_ready = False
            self._provider_boundary_snapshots.pop(verdict.candidate_generation, None)
            self._provider_boundary_completion_entries.pop(
                verdict.candidate_generation, None
            )
            return "completed"

    async def retire_provider_speaker_boundary_unknown(
        self,
        verdict: ProviderSpeakerPresealVerdict | None = None,
    ) -> ProviderSpeakerPresealVerdict | None:
        """Create or target one unknown tombstone without revoking other keys."""

        async with self._lock:
            if self._closed or self._semantic_adapter is not None:
                return None
            if verdict is None:
                self._speaker_rejection_prepare_diagnostics[
                    "provider_targeted_retirement_count"
                ] += 1
                unknown = self._reserve_provider_unknown_preseal_locked()
            elif type(verdict) is ProviderSpeakerBoundarySnapshot:
                if (
                    verdict._owner is not self._provider_boundary_snapshot_owner
                    or verdict.detector_epoch != self._detector_epoch
                ):
                    # A forged or cross-epoch value cannot identify a safe
                    # target. Poison only this unresolved namespace.
                    self._clear_provider_segment_state(
                        preserve_ordered_mode=True,
                        preserve_last_sequence=True,
                        preserve_audio_cursor=True,
                    )
                    self._speaker_rejection_prepare_diagnostics[
                        "provider_namespace_poison_count"
                    ] += 1
                    unknown = self._reserve_provider_unknown_preseal_locked()
                else:
                    entry = self._provider_preseal_entries.get(
                        verdict.candidate_generation
                    ) or self._provider_boundary_completion_entries.get(
                        verdict.candidate_generation
                    )
                    if entry is None:
                        # The generation was already consumed or evicted. A
                        # late boundary task has no authority over newer keys.
                        unknown = (
                            self._unauthorized_unknown_provider_preseal_verdict_locked(
                                verdict.candidate_generation
                            )
                        )
                        self._speaker_rejection_prepare_diagnostics[
                            "provider_preseal_verdict_stale_count"
                        ] += 1
                    elif entry.verdict is verdict:
                        self._speaker_rejection_prepare_diagnostics[
                            "provider_targeted_retirement_count"
                        ] += 1
                        unknown = self._downgrade_provider_preseal_entry_locked(entry)
                        self._provider_boundary_completion_entries.pop(
                            verdict.candidate_generation, None
                        )
                    elif not entry.verdict.boundary_exact:
                        # Repeating the original exact verdict after its
                        # targeted downgrade is idempotent.
                        unknown = entry.verdict
                    else:
                        # Two distinct owner-valid exact values for one live
                        # generation indicate an internal mapping conflict;
                        # no single entry can be trusted as the target.
                        self._clear_provider_segment_state(
                            preserve_ordered_mode=True,
                            preserve_last_sequence=True,
                            preserve_audio_cursor=True,
                        )
                        self._speaker_rejection_prepare_diagnostics[
                            "provider_namespace_poison_count"
                        ] += 1
                        unknown = self._reserve_provider_unknown_preseal_locked()
            else:
                # A non-contract value cannot carry target identity.
                self._clear_provider_segment_state(
                    preserve_ordered_mode=True,
                    preserve_last_sequence=True,
                    preserve_audio_cursor=True,
                )
                self._speaker_rejection_prepare_diagnostics[
                    "provider_namespace_poison_count"
                ] += 1
                unknown = self._reserve_provider_unknown_preseal_locked()
        await self._drain_provider_segment_expiry_tasks()
        return unknown

    async def reset_provider_audio_timeline(self) -> bool:
        """Reset only state whose coordinates belong to one Provider session.

        A transport-only reconnect preserves local VAD and turn identity, but
        the replacement Provider session starts a fresh audio-buffer timeline.
        Keeping the old canonical cursor or sealed speaker authority would make
        exact ranges from the replacement session refer to the wrong PCM.
        """

        async with self._lock:
            if self._closed or self._semantic_adapter is not None:
                return False
            evidence_state = self._provider_speaker_evidence_state
            if evidence_state is not None:
                self._abandon_provider_speaker_evidence_locked(evidence_state)
            self._provider_candidate_fence = None
            self._sealed_provider_candidate_rejection = None
            self._provider_micro_event_aggregate = None
            self._sealed_provider_micro_event = None
            self._provider_speaker_sealed_through_sequence_no = None
            self._provider_discarded_through_sequence_no = None
            self._clear_provider_segment_state()
            # Rotate the private owner as defense in depth: even if a stale
            # PCM-free snapshot escapes its bounded table, it cannot authorize
            # speaker suppression in the replacement Provider timeline.
            self._provider_boundary_snapshot_owner = object()
            self._finish_speaker_shadow_candidate(expected_scope="provider_candidate")
            if self._speaker_shadow_suppressed_candidate == (
                self._detector_epoch,
                "provider_candidate",
            ):
                self._speaker_shadow_suppressed_candidate = None
        await self._drain_provider_segment_expiry_tasks()
        return True

    async def wait_provider_audio_observed_through(
        self,
        end_sample_16k: int,
    ) -> bool:
        """Wait until ordered observation reaches one canonical boundary.

        The caller owns the timeout. A Provider timeline reset or detector
        close revokes the wait instead of allowing coordinates from two
        physical sessions to be compared.
        """

        if type(end_sample_16k) is not int or end_sample_16k <= 0:
            return False
        timeline_generation: int | None = None
        while True:
            async with self._lock:
                if timeline_generation is None:
                    timeline_generation = self._provider_audio_timeline_generation
                if (
                    self._closed
                    or self._semantic_adapter is not None
                    or self._provider_audio_timeline_generation != timeline_generation
                ):
                    return False
                if self._provider_audio_sample_cursor_16k >= end_sample_16k:
                    return True
                observed = self._provider_audio_observation_event
            await observed.wait()

    async def wait_provider_speaker_preseal(
        self,
        verdict: ProviderSpeakerPresealVerdict,
        *,
        deadline: float,
    ) -> bool:
        """Wait event-driven for one exact receipt, then revalidate its owner."""

        if (
            type(verdict) is not ProviderSpeakerBoundarySnapshot
            or isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            return False
        receipt: (
            SpeakerShadowBatchReconcileReceipt
            | SpeakerShadowTerminalCoverageReceipt
            | None
        ) = None
        settlement: SpeakerShadowReconciliationSettlement | None = None
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0.0:
            return False
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=remaining)
            except TimeoutError:
                return False
            acquired = True
            entry = self._provider_preseal_entries.get(verdict.candidate_generation)
            if (
                self._closed
                or not verdict.boundary_exact
                or verdict._owner is not self._provider_boundary_snapshot_owner
                or verdict.detector_epoch != self._detector_epoch
            ):
                return False
            if entry is None:
                # Ordered seal atomically consumes the pre-seal entry before
                # Runtime reuses its stable lease. Preserve the same proof
                # across that one-way phase transition when the sealed exact
                # capability still identifies the candidate. The caller is
                # itself the rejection request, so the sealed capability does
                # not need a pre-existing rejection-ready bit.
                sealed = self._sealed_provider_candidate_rejection
                fence = self._provider_candidate_fence
                return bool(
                    time.monotonic() < float(deadline)
                    and sealed is not None
                    and fence is not None
                    and fence.boundary_exact
                    and sealed.provider_fence == fence
                    and sealed.candidate
                    == DetectorCandidateKey(
                        verdict.detector_epoch,
                        verdict.candidate_generation,
                    )
                    and sealed.shadow_candidate.shadow_generation
                    == verdict.shadow_generation
                )
            if entry.revoked or entry.verdict is not verdict:
                return False
            if self._provider_preseal_reconciliation_status_locked(entry) == "applied":
                return time.monotonic() < float(deadline)
            receipt = entry.reconciliation
            shadow = self._speaker_shadow
            settlement = (
                shadow
                if isinstance(shadow, SpeakerShadowReconciliationSettlement)
                else None
            )
        finally:
            if acquired:
                self._lock.release()
        if receipt is None or settlement is None:
            if receipt is not None:
                self._revoke_provider_reconciliation_receipt(receipt)
            return False
        if time.monotonic() >= float(deadline):
            self._revoke_provider_reconciliation_receipt(receipt)
            return False
        try:
            status = await settlement.wait_reconciliation_settled(
                receipt,
                deadline=float(deadline),
            )
        except asyncio.CancelledError:
            self._revoke_provider_reconciliation_receipt(receipt)
            raise
        except Exception:
            status = "stale"
        if status != "applied":
            self._revoke_provider_reconciliation_receipt(receipt)
            return False

        remaining = float(deadline) - time.monotonic()
        if remaining <= 0.0:
            self._revoke_provider_reconciliation_receipt(receipt)
            return False
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=remaining)
            except TimeoutError:
                self._revoke_provider_reconciliation_receipt(receipt)
                return False
            except asyncio.CancelledError:
                self._revoke_provider_reconciliation_receipt(receipt)
                raise
            acquired = True
            entry = self._provider_preseal_entries.get(verdict.candidate_generation)
            if entry is None:
                sealed = self._sealed_provider_candidate_rejection
                fence = self._provider_candidate_fence
                if (
                    time.monotonic() < float(deadline)
                    and sealed is not None
                    and fence is not None
                    and fence.boundary_exact
                    and sealed.provider_fence == fence
                    and sealed.candidate
                    == DetectorCandidateKey(
                        verdict.detector_epoch,
                        verdict.candidate_generation,
                    )
                    and sealed.shadow_candidate.shadow_generation
                    == verdict.shadow_generation
                ):
                    return True
            if (
                time.monotonic() >= float(deadline)
                or self._closed
                or entry is None
                or entry.revoked
                or entry.verdict is not verdict
                or entry.reconciliation is not receipt
                or self._provider_preseal_reconciliation_status_locked(entry)
                != "applied"
            ):
                self._revoke_provider_reconciliation_receipt(receipt)
                if entry is not None and entry.reconciliation is receipt:
                    entry.revoked = True
                return False
            return True
        finally:
            if acquired:
                self._lock.release()

    async def prepare_candidate_rejection(
        self,
        shadow_candidate: SpeakerShadowCandidateKey,
    ) -> DetectorCandidateRejectionLease | None:
        """Map one private speaker observation to current detector authority.

        This is phase one only: it captures a revocable lease under the
        detector lock but changes no ASR state. The runtime later commits it
        synchronously inside its final-serialization boundary.
        """

        if type(shadow_candidate) is not SpeakerShadowCandidateKey:
            self._speaker_rejection_prepare_diagnostics[
                "rejection_prepare_type_mismatch_count"
            ] += 1
            return None
        async with self._lock:
            if self._closed:
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_prepare_detector_closed_count"
                ] += 1
                return None
            if self._provider_segment_ordered_mode:
                self._expire_provider_segments(time.monotonic())
            sealed = self._sealed_provider_candidate_rejection
            if (
                sealed is not None
                and sealed.provider_fence == self._provider_candidate_fence
                and shadow_candidate == sealed.shadow_candidate
            ):
                return DetectorCandidateRejectionLease(
                    candidate=sealed.candidate,
                    shadow_candidate=sealed.shadow_candidate,
                    turn_token=sealed.turn_token,
                    _runtime=self,
                    provider_fence=sealed.provider_fence,
                )
            if self._provider_segment_ordered_mode:
                pending_entry = self._provider_preseal_entries.get(
                    self._candidate_generation
                )
                pending_snapshot = (
                    pending_entry.verdict if pending_entry is not None else None
                )
                pending_status = (
                    self._provider_preseal_reconciliation_status_locked(pending_entry)
                    if pending_entry is not None
                    else "stale"
                )
                if (
                    pending_snapshot is not None
                    and pending_snapshot.boundary_exact
                    and pending_status in {"pending", "applied"}
                    and pending_snapshot._owner
                    is self._provider_boundary_snapshot_owner
                    and pending_snapshot.shadow_generation
                    == shadow_candidate.shadow_generation
                    and shadow_candidate.detector_epoch == self._detector_epoch
                    and shadow_candidate.scope == "provider_candidate"
                ):
                    candidate = DetectorCandidateKey(
                        pending_snapshot.detector_epoch,
                        pending_snapshot.candidate_generation,
                    )
                    bound = self._bound_turns.get(candidate)
                    if bound is not None:
                        return DetectorCandidateRejectionLease(
                            candidate=candidate,
                            shadow_candidate=shadow_candidate,
                            turn_token=bound.turn_token,
                            _runtime=self,
                            provider_preseal_verdict=pending_snapshot,
                        )
                segment = next(
                    (
                        item
                        for item in self._provider_speaker_segments
                        if item.candidate == shadow_candidate
                    ),
                    None,
                )
                head = (
                    self._provider_speaker_segments[0]
                    if self._provider_speaker_segments
                    else None
                )
                if (
                    segment is None
                    or segment is not head
                    or len(self._provider_speaker_segments) != 1
                    or not segment.evidence_complete
                    or segment.ownership_ambiguous
                    or segment.deferred
                    or shadow_candidate.detector_epoch != self._detector_epoch
                ):
                    self._speaker_rejection_prepare_diagnostics[
                        "rejection_prepare_shadow_mismatch_count"
                    ] += 1
                    return None
                bound = self._bound_turns.get(segment.detector_candidate)
                if bound is None:
                    self._speaker_rejection_prepare_diagnostics[
                        "rejection_prepare_unbound_count"
                    ] += 1
                    return None
                return DetectorCandidateRejectionLease(
                    candidate=segment.detector_candidate,
                    shadow_candidate=shadow_candidate,
                    turn_token=bound.turn_token,
                    _runtime=self,
                )
            if not self._candidate_open:
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_prepare_candidate_closed_count"
                ] += 1
                if sealed is None:
                    self._speaker_rejection_prepare_diagnostics[
                        "rejection_prepare_closed_no_sealed_count"
                    ] += 1
                elif sealed.provider_fence != self._provider_candidate_fence:
                    self._speaker_rejection_prepare_diagnostics[
                        "rejection_prepare_closed_fence_mismatch_count"
                    ] += 1
                elif shadow_candidate != sealed.shadow_candidate:
                    self._speaker_rejection_prepare_diagnostics[
                        "rejection_prepare_closed_shadow_mismatch_count"
                    ] += 1
                return None
            if shadow_candidate.detector_epoch != self._detector_epoch:
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_prepare_epoch_mismatch_count"
                ] += 1
                return None
            if shadow_candidate != self._speaker_shadow_candidate:
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_prepare_shadow_mismatch_count"
                ] += 1
                return None
            candidate = DetectorCandidateKey(
                self._detector_epoch,
                self._candidate_generation,
            )
            bound = self._bound_turns.get(candidate)
            if bound is None:
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_prepare_unbound_count"
                ] += 1
                return None
            return DetectorCandidateRejectionLease(
                candidate=candidate,
                shadow_candidate=shadow_candidate,
                turn_token=bound.turn_token,
                _runtime=self,
            )

    def _bound_turn_token_for_speaker_candidate(
        self,
        shadow_candidate: SpeakerShadowCandidateKey,
    ) -> VoiceTurnToken | None:
        """Resolve the already-bound turn without yielding the callback loop.

        Speaker observation callbacks use this only to reserve their FIFO
        position before returning.  It grants no rejection authority; the
        separately locked ``prepare_candidate_rejection`` remains the sole
        capability issuer.
        """

        if (
            type(shadow_candidate) is not SpeakerShadowCandidateKey
            or self._closed
            or shadow_candidate.detector_epoch != self._detector_epoch
        ):
            return None
        return self._speaker_candidate_turn_bindings.get(shadow_candidate)

    def release_speaker_candidate_binding(
        self,
        shadow_candidate: SpeakerShadowCandidateKey,
        turn_token: VoiceTurnToken,
    ) -> bool:
        """Retire one terminal speaker binding by exact immutable identity.

        Provider boundary completion only closes capture; the ordered speaker
        worker may still publish a score and ``CaptureClosed`` afterwards.
        The admission owner calls this after accepting that terminal notice.
        Reset and close continue to revoke every outstanding binding at once.
        """

        if (
            type(shadow_candidate) is not SpeakerShadowCandidateKey
            or type(turn_token) is not VoiceTurnToken
            or self._closed
            or shadow_candidate.detector_epoch != self._detector_epoch
            or self._speaker_candidate_turn_bindings.get(shadow_candidate) != turn_token
        ):
            return False
        bindings = dict(self._speaker_candidate_turn_bindings)
        bindings.pop(shadow_candidate, None)
        self._speaker_candidate_turn_bindings = bindings
        owner_generations = dict(self._speaker_candidate_owner_generations)
        owner_generations.pop(shadow_candidate, None)
        self._speaker_candidate_owner_generations = owner_generations
        return True

    def speaker_rejection_diagnostics_snapshot(self) -> dict[str, int]:
        """Return aggregate-only rejection preparation counters."""

        diagnostics = dict(self._speaker_rejection_prepare_diagnostics)
        shadow = self._speaker_shadow
        if shadow is not None:
            try:
                shadow_metrics = shadow.snapshot()
            except Exception:
                shadow_metrics = {}
            if isinstance(shadow_metrics, dict):
                for name in (
                    "reconciliation_batch_admitted_count",
                    "reconciliation_batch_applied_count",
                    "reconciliation_batch_failed_count",
                    "reconciliation_batch_revoked_count",
                ):
                    value = shadow_metrics.get(name)
                    if type(value) is int and value >= 0:
                        diagnostics[name] = value
        return diagnostics

    def _observe_provider_micro_event(
        self,
        candidate: DetectorCandidateKey,
        *,
        silero: SileroFeedResult | None,
        events: tuple[SpeechActivityEvent, ...],
        rnnoise: RnnoiseEvidence,
        onset_threshold: float,
        chunk_duration_ms: int,
    ) -> None:
        if not self._provider_micro_event_enabled:
            return
        aggregate = self._provider_micro_event_aggregate
        if aggregate is None or aggregate.candidate != candidate:
            aggregate = _ProviderMicroEventAggregate(candidate)
            self._provider_micro_event_aggregate = aggregate
        if candidate in self._provider_micro_event_ambiguous_candidates:
            aggregate.silero_evidence_complete = False
            aggregate.rnnoise_evidence_complete = False
        aggregate.observe_silero(silero, events)
        aggregate.observe_rnnoise(
            rnnoise,
            onset_threshold=onset_threshold,
            chunk_duration_ms=chunk_duration_ms,
        )

    def _seal_provider_micro_event(
        self,
        candidate: DetectorCandidateKey,
        fence: ProviderCandidateFence,
    ) -> None:
        self._sealed_provider_micro_event = None
        if not self._provider_micro_event_enabled:
            self._provider_micro_event_aggregate = None
            return

        diagnostics = self._speaker_rejection_prepare_diagnostics
        diagnostics["micro_event_candidate_count"] += 1
        aggregate = self._provider_micro_event_aggregate
        if (
            candidate in self._provider_micro_event_ambiguous_candidates
            or aggregate is None
            or aggregate.candidate != candidate
        ):
            evidence = ProviderMicroEventEvidence(None, False, None)
        else:
            evidence = aggregate.freeze()
        self._provider_micro_event_aggregate = None

        if not evidence.rnnoise_evidence_complete:
            diagnostics["micro_event_rnnoise_unavailable_count"] += 1

        try:
            decision = self._provider_micro_event_policy.decide(evidence)
        except Exception:
            decision = ProviderMicroEventDecision(
                would_suppress=False,
                suppress=False,
                reason="policy_error",
                fail_open=True,
            )
        if decision.fail_open:
            diagnostics["micro_event_evidence_unavailable_count"] += 1
            diagnostics["micro_event_fail_open_count"] += 1
        else:
            diagnostics["micro_event_evidence_complete_count"] += 1
        if decision.would_suppress:
            diagnostics["micro_event_would_suppress_count"] += 1
        self._sealed_provider_micro_event = _SealedProviderMicroEvent(
            provider_fence=fence,
            evidence=evidence,
            decision=decision,
        )

    def sealed_provider_micro_event_decision(
        self,
        fence: ProviderCandidateFence,
    ) -> ProviderMicroEventDecision | None:
        """Read the frozen decision for one exact Provider candidate fence."""

        sealed = self._sealed_provider_micro_event
        if not self._provider_micro_event_enabled:
            return None
        if (
            self._closed
            or self._semantic_adapter is not None
            or type(fence) is not ProviderCandidateFence
            or fence != self._provider_candidate_fence
            or sealed is None
            or sealed.provider_fence != fence
        ):
            self._speaker_rejection_prepare_diagnostics[
                "micro_event_stale_fence_count"
            ] += 1
            return None
        return sealed.decision

    def _commit_candidate_rejection(
        self,
        lease: DetectorCandidateRejectionLease,
    ) -> bool:
        """Apply phase two only while no detector coroutine owns its lock."""

        if type(lease) is not DetectorCandidateRejectionLease:
            return False
        # A detector coroutine may hold the lock across an await. Mutating its
        # authority concurrently would violate the lease proof, so ambiguity
        # always forwards the candidate.
        if self._lock.locked():
            return False
        candidate = lease.candidate
        provider_fence = lease.provider_fence
        provider_preseal = lease.provider_preseal_verdict
        if provider_preseal is not None:
            entry = self._provider_preseal_entries.get(
                provider_preseal.candidate_generation
            )
            status = (
                self._provider_preseal_reconciliation_status_locked(entry)
                if entry is not None
                else "stale"
            )
            bound = self._bound_turns.get(candidate)
            if (
                lease._runtime is not self
                or self._closed
                or entry is None
                or entry.revoked
                or entry.verdict is not provider_preseal
                or not provider_preseal.boundary_exact
                or provider_preseal._owner is not self._provider_boundary_snapshot_owner
                or provider_preseal.detector_epoch != self._detector_epoch
                or candidate
                != DetectorCandidateKey(
                    provider_preseal.detector_epoch,
                    provider_preseal.candidate_generation,
                )
                or entry.shadow_candidate != lease.shadow_candidate
                or status != "applied"
                or bound is None
                or bound.turn_token != lease.turn_token
            ):
                return False
            # This is deliberately only a pre-seal fact.  The ordered endpoint
            # upgrades it to a fence-bound rejection atomically; it must not
            # consume the detector candidate or touch ASR lifecycle early.
            if not entry.rejection_ready:
                self._speaker_rejection_prepare_diagnostics[
                    "provider_rejection_ready_count"
                ] += 1
            entry.rejection_ready = True
            return True
        if provider_fence is not None:
            sealed = self._sealed_provider_candidate_rejection
            bound = self._bound_turns.get(candidate)
            if (
                lease._runtime is not self
                or self._closed
                or sealed is None
                or provider_fence != self._provider_candidate_fence
                or provider_fence != sealed.provider_fence
                or candidate
                != DetectorCandidateKey(
                    provider_fence.detector_epoch,
                    provider_fence.candidate_generation,
                )
                or candidate != sealed.candidate
                or candidate.detector_epoch != self._detector_epoch
                or lease.shadow_candidate != sealed.shadow_candidate
                or lease.turn_token != sealed.turn_token
                or bound is None
                or bound.turn_token != sealed.turn_token
            ):
                return False
            self._sealed_provider_candidate_rejection = None
            self._speaker_rejection_prepare_diagnostics[
                "provider_rejection_applied_count"
            ] += 1
            return True

        bound = self._bound_turns.get(candidate)
        ordered_segment: _ProviderSpeakerSegment | None = None
        if self._provider_segment_ordered_mode:
            if len(self._provider_speaker_segments) != 1:
                return False
            ordered_segment = self._provider_speaker_segments[0]
            if (
                ordered_segment.detector_candidate != candidate
                or ordered_segment.candidate != lease.shadow_candidate
                or not ordered_segment.evidence_complete
                or ordered_segment.ownership_ambiguous
                or ordered_segment.deferred
            ):
                return False
        if (
            lease._runtime is not self
            or self._closed
            or not self._candidate_open
            or candidate.detector_epoch != self._detector_epoch
            or candidate.candidate_generation != self._candidate_generation
            or (
                not self._provider_segment_ordered_mode
                and lease.shadow_candidate != self._speaker_shadow_candidate
            )
            or bound is None
            or bound.turn_token != lease.turn_token
        ):
            return False

        self._bound_turns.pop(candidate, None)
        self._deferred_completions.pop(candidate, None)
        self._candidate_generation += 1
        self._candidate_open = False
        self._policy_event_candidate = None
        self._provider_candidate_fence = None
        self._sealed_provider_candidate_rejection = None
        self._provider_micro_event_aggregate = None
        self._sealed_provider_micro_event = None
        self._throttle_policy.reset_candidate_activity()
        if ordered_segment is not None:
            self._provider_speaker_segments.popleft()
            self._finish_provider_segment(ordered_segment)
            if not self._provider_speaker_segments:
                self._retire_provider_segment_expiry_task()
        else:
            self._finish_speaker_shadow_candidate(
                expected_scope=lease.shadow_candidate.scope,
            )
        return True

    async def _commit_candidate_rejection_async(
        self,
        lease: DetectorCandidateRejectionLease,
        *,
        deadline: float | None = None,
    ) -> DetectorCandidateRejectionCommitResult:
        """Apply one stable lease at its current Provider lifecycle phase."""

        stale = DetectorCandidateRejectionCommitResult.STALE
        if type(lease) is not DetectorCandidateRejectionLease:
            return stale
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            return stale
        provider_preseal = lease.provider_preseal_verdict
        if provider_preseal is not None and deadline is not None:
            # A second-low may arrive while the exact ownership batch is still
            # queued.  Pending is not stale: wait event-driven within the one
            # caller-owned absolute admission budget, then revalidate again
            # under the detector lock before publishing rejection readiness.
            try:
                settled = await self.wait_provider_speaker_preseal(
                    provider_preseal,
                    deadline=float(deadline),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                settled = False
            if not settled:
                return stale
        acquired = False
        try:
            if deadline is None:
                await self._lock.acquire()
                acquired = True
            else:
                remaining = float(deadline) - time.monotonic()
                if remaining <= 0.0:
                    return stale
                try:
                    await asyncio.wait_for(self._lock.acquire(), timeout=remaining)
                except TimeoutError:
                    return stale
                acquired = True
                if time.monotonic() >= float(deadline):
                    return stale
            return self._commit_candidate_rejection_stable_locked(lease)
        finally:
            if acquired:
                self._lock.release()

    def _commit_candidate_rejection_stable_locked(
        self,
        lease: DetectorCandidateRejectionLease,
    ) -> DetectorCandidateRejectionCommitResult:
        """Revalidate ``lease`` against active, preseal, or sealed authority."""

        stale = DetectorCandidateRejectionCommitResult.STALE
        candidate = lease.candidate
        if (
            lease._runtime is not self
            or self._closed
            or candidate.detector_epoch != self._detector_epoch
            or lease.shadow_candidate.detector_epoch != self._detector_epoch
        ):
            return stale
        bound = self._bound_turns.get(candidate)
        if bound is None or bound.turn_token != lease.turn_token:
            return stale

        sealed = self._sealed_provider_candidate_rejection
        provider_fence = self._provider_candidate_fence
        if (
            sealed is not None
            and provider_fence is not None
            and sealed.provider_fence == provider_fence
            and sealed.candidate == candidate
            and sealed.shadow_candidate == lease.shadow_candidate
            and sealed.turn_token == lease.turn_token
        ):
            self._sealed_provider_candidate_rejection = None
            self._speaker_rejection_prepare_diagnostics[
                "provider_rejection_applied_count"
            ] += 1
            return DetectorCandidateRejectionCommitResult.SEALED_APPLIED

        entry = self._provider_preseal_entries.get(candidate.candidate_generation)
        if entry is not None:
            verdict = entry.verdict
            if (
                not entry.revoked
                and verdict.boundary_exact
                and verdict._owner is self._provider_boundary_snapshot_owner
                and verdict.detector_epoch == self._detector_epoch
                and verdict.candidate_generation == candidate.candidate_generation
                and entry.shadow_candidate == lease.shadow_candidate
                and self._provider_preseal_reconciliation_status_locked(entry)
                == "applied"
            ):
                if not entry.rejection_ready:
                    self._speaker_rejection_prepare_diagnostics[
                        "provider_rejection_ready_count"
                    ] += 1
                entry.rejection_ready = True
                return DetectorCandidateRejectionCommitResult.PRESEAL_READY
            return stale

        ordered_segment: _ProviderSpeakerSegment | None = None
        if self._provider_segment_ordered_mode:
            if len(self._provider_speaker_segments) != 1:
                return stale
            ordered_segment = self._provider_speaker_segments[0]
            if (
                ordered_segment.detector_candidate != candidate
                or ordered_segment.candidate != lease.shadow_candidate
                or not ordered_segment.evidence_complete
                or ordered_segment.ownership_ambiguous
                or ordered_segment.deferred
            ):
                return stale
        if (
            not self._candidate_open
            or candidate.candidate_generation != self._candidate_generation
            or (
                not self._provider_segment_ordered_mode
                and lease.shadow_candidate != self._speaker_shadow_candidate
            )
        ):
            return stale

        self._bound_turns.pop(candidate, None)
        self._deferred_completions.pop(candidate, None)
        self._candidate_generation += 1
        self._candidate_open = False
        self._policy_event_candidate = None
        self._provider_candidate_fence = None
        self._sealed_provider_candidate_rejection = None
        self._provider_micro_event_aggregate = None
        self._sealed_provider_micro_event = None
        self._throttle_policy.reset_candidate_activity()
        if ordered_segment is not None:
            self._provider_speaker_segments.popleft()
            self._finish_provider_segment(ordered_segment)
            if not self._provider_speaker_segments:
                self._retire_provider_segment_expiry_task()
        else:
            self._finish_speaker_shadow_candidate(
                expected_scope=lease.shadow_candidate.scope,
            )
        return DetectorCandidateRejectionCommitResult.ACTIVE_APPLIED

    async def _publish_bound_completion(
        self,
        candidate: DetectorCandidateKey,
        identity: DetectorIngressIdentity,
    ) -> bool:
        bound_turn = self._bound_turns.pop(candidate, None)
        if bound_turn is None:
            return False
        if self._on_event is not None:
            await self._on_event(
                DetectorTurnEvent(
                    ingress=identity,
                    bound_turn=bound_turn,
                    kind="complete",
                )
            )
        return True

    async def force_speech_started(
        self,
        identity: DetectorIngressIdentity,
    ) -> bool:
        """Open continuous-upload mode without changing SmartTurn authority."""

        if (
            self._on_event is None
            or self._closed
            or identity.detector_epoch != self._detector_epoch
        ):
            return False
        await self._on_event(
            DetectorActivityEvent(
                ingress=identity,
                candidate=DetectorCandidateKey(
                    identity.detector_epoch,
                    self._candidate_generation,
                ),
                activity=SpeechActivityEvent.SPEECH_STARTED,
            )
        )
        return True

    async def prepare_endpointing(
        self,
        token: VoiceTurnToken,
    ) -> SmartTurnLease | None:
        """Load and pin SmartTurn before any provider wire audio is allowed."""

        adapter = self._semantic_adapter
        coordinator = self._semantic_coordinator
        if self._closed or adapter is None or coordinator is None:
            self._smart_turn_readiness = SmartTurnReadiness.FAILED
            return None
        await self._ensure_semantic_started(adapter)
        prepare_task: asyncio.Task[bool] | None = None
        while True:
            stale_task: asyncio.Task[bool] | None = None
            async with self._lock:
                if self._closed or adapter.failed:
                    self._smart_turn_readiness = SmartTurnReadiness.FAILED
                    return None
                if (
                    self._smart_turn_token == token
                    and self._smart_turn_readiness is SmartTurnReadiness.READY
                ):
                    return self._acquire_endpointing_lease_locked(token)
                if self._smart_turn_token is not None:
                    return None
                if (
                    self._smart_turn_readiness is SmartTurnReadiness.READY
                    and self._prepare_task is None
                ):
                    adapter.pin_smart_turn()
                    self._smart_turn_generation_sequence += 1
                    self._smart_turn_generation = self._smart_turn_generation_sequence
                    self._smart_turn_token = token
                    return self._acquire_endpointing_lease_locked(token)
                if self._prepare_task is not None:
                    if self._prepare_token == token:
                        prepare_task = self._prepare_task
                        self._prepare_waiters[prepare_task] = (
                            self._prepare_waiters.get(prepare_task, 0) + 1
                        )
                    elif self._prepare_token is not None:
                        return None
                    else:
                        # _prepare_task alive with _prepare_token cleared means
                        # a reset/release/invalidate orphaned an in-flight
                        # model load.  Wait for its cleanup outside the lock
                        # and retry instead of failing the new turn; the task
                        # stays registered so close() can still cancel it.
                        stale_task = self._prepare_task
                else:
                    self._smart_turn_readiness = SmartTurnReadiness.LOADING
                    self._prepare_token = token
                    self._prepare_epoch = self._detector_epoch
                    adapter.pin_smart_turn()
                    self._smart_turn_generation_sequence += 1
                    prepare_generation = self._smart_turn_generation_sequence
                    prepare_task = asyncio.create_task(
                        self._prepare_endpointing_task(
                            adapter,
                            coordinator,
                            token,
                            self._detector_epoch,
                            prepare_generation,
                        ),
                        name="detector-runtime-smart-turn-prepare",
                    )
                    self._prepare_task = prepare_task
                    self._prepare_waiters[prepare_task] = 1
                    self._prepare_generations[prepare_task] = prepare_generation
            if stale_task is None:
                break
            await asyncio.gather(stale_task, return_exceptions=True)
        if prepare_task is None:
            return None
        prepared = False
        try:
            prepared = await asyncio.shield(prepare_task)
        finally:
            lease = await self._finish_prepare_waiter(
                prepare_task,
                token,
                acquire=prepared,
            )
        return lease

    def _acquire_endpointing_lease_locked(
        self,
        token: VoiceTurnToken,
    ) -> SmartTurnLease:
        self._smart_turn_lease_sequence += 1
        lease_id = self._smart_turn_lease_sequence
        self._smart_turn_lease_ids.add(lease_id)
        return SmartTurnLease(token, self, lease_id)

    async def _finish_prepare_waiter(
        self,
        prepare_task: asyncio.Task[bool],
        token: VoiceTurnToken,
        *,
        acquire: bool,
    ) -> SmartTurnLease | None:
        async with self._lock:
            waiter_count = self._prepare_waiters.get(prepare_task, 0)
            prepare_generation = self._prepare_generations.get(prepare_task)
            if waiter_count <= 1:
                self._prepare_waiters.pop(prepare_task, None)
                self._prepare_generations.pop(prepare_task, None)
            else:
                self._prepare_waiters[prepare_task] = waiter_count - 1
            if acquire and self.endpointing_ready(token):
                return self._acquire_endpointing_lease_locked(token)
            if (
                self._prepare_waiters.get(prepare_task, 0) == 0
                and not self._smart_turn_lease_ids
                and self._smart_turn_token == token
                and self._smart_turn_generation == prepare_generation
            ):
                self._smart_turn_token = None
                self._smart_turn_generation = None
                self._smart_turn_readiness = SmartTurnReadiness.UNLOADED
                adapter = self._semantic_adapter
                if adapter is not None:
                    adapter.unpin_smart_turn()
            return None

    async def _prepare_endpointing_task(
        self,
        adapter: _VoiceTurnAdapter,
        coordinator: TurnCoordinator,
        token: VoiceTurnToken,
        detector_epoch: int,
        prepare_generation: int,
    ) -> bool:
        loaded = False
        prepare_error: BaseException | None = None
        try:
            loaded = await coordinator.prepare_predictor()
        except asyncio.CancelledError as exc:
            prepare_error = exc
        except Exception as exc:
            prepare_error = exc
        prepared = False
        async with self._lock:
            current_task = asyncio.current_task()
            owns_prepare = self._prepare_task is current_task
            if owns_prepare:
                self._prepare_task = None
            valid_state = bool(
                owns_prepare
                and not self._closed
                and not adapter.failed
                and self._prepare_token == token
                and self._prepare_epoch == detector_epoch
                and self._detector_epoch == detector_epoch
            )
            has_waiters = bool(
                current_task is not None
                and self._prepare_waiters.get(current_task, 0) > 0
            )
            if owns_prepare:
                # Only the registered single-flight owner may clear these; a
                # detached task must not clobber a successor's fields.
                self._prepare_token = None
                self._prepare_epoch = None
            if valid_state and has_waiters and loaded and prepare_error is None:
                self._smart_turn_token = token
                self._smart_turn_generation = prepare_generation
                self._smart_turn_readiness = SmartTurnReadiness.READY
                prepared = True
            else:
                adapter.unpin_smart_turn()
            if valid_state and not prepared:
                self._smart_turn_readiness = (
                    SmartTurnReadiness.FAILED
                    if has_waiters
                    else SmartTurnReadiness.UNLOADED
                )
        if isinstance(prepare_error, asyncio.CancelledError):
            raise prepare_error
        return prepared

    async def _ensure_semantic_started(self, adapter: _VoiceTurnAdapter) -> None:
        if self._semantic_started:
            return
        await adapter.start()
        self._semantic_started = True
        self._failure_watch_task = asyncio.create_task(
            self._watch_semantic_failure(adapter),
            name="detector-runtime-smart-turn-watch",
        )

    def endpointing_ready(self, token: VoiceTurnToken) -> bool:
        adapter = self._semantic_adapter
        return bool(
            not self._closed
            and adapter is not None
            and not adapter.failed
            and self._smart_turn_readiness is SmartTurnReadiness.READY
            and self._smart_turn_token == token
        )

    async def release_endpointing(
        self,
        token: VoiceTurnToken,
        lease_id: int,
    ) -> None:
        async with self._lock:
            if lease_id not in self._smart_turn_lease_ids:
                return
            self._smart_turn_lease_ids.remove(lease_id)
            if self._smart_turn_lease_ids:
                return
            if self._smart_turn_token != token:
                return
            self._smart_turn_token = None
            self._smart_turn_generation = None
            self._smart_turn_readiness = SmartTurnReadiness.UNLOADED
            adapter = self._semantic_adapter
            if adapter is not None:
                adapter.unpin_smart_turn()

    async def invalidate(self, token: VoiceTurnToken) -> None:
        speaker_shadow: SpeakerShadowObserver | None = None
        async with self._lock:
            if self._smart_turn_token != token and self._prepare_token != token:
                return
            self._smart_turn_token = None
            self._smart_turn_generation = None
            self._smart_turn_lease_ids.clear()
            self._prepare_token = None
            self._prepare_epoch = None
            self._detector_epoch += 1
            self._candidate_generation = 0
            self._candidate_open = False
            self._policy_event_candidate = None
            self._throttle_policy.reset_candidate_activity()
            self._ingress_token = None
            self._bound_turns.clear()
            self._deferred_completions.clear()
            self._completion_fences.clear()
            self._provider_candidate_fence = None
            self._sealed_provider_candidate_rejection = None
            self._provider_micro_event_aggregate = None
            self._sealed_provider_micro_event = None
            self._clear_provider_segment_state()
            self._provider_speaker_sealed_through_sequence_no = None
            self._deferred_completion_identity_advanced = False
            self._provider_discarded_through_sequence_no = None
            # A deferred completion belongs to the invalidated epoch; keeping
            # the flags would let release_deferred_turn spuriously advance the
            # fresh epoch's first candidate.
            self._defer_turn_complete = False
            self._deferred_turn_complete = False
            self._reset_speaker_shadow_identity()
            speaker_shadow = self._speaker_shadow
            adapter = self._semantic_adapter
            if adapter is not None:
                adapter.unpin_smart_turn()
            self._smart_turn_readiness = SmartTurnReadiness.UNLOADED
        await self._reset_speaker_shadow(speaker_shadow)
        await self._drain_provider_segment_expiry_tasks()

    def _active_deny_rearm_token(self) -> tuple[int, int, int] | None:
        token = self._deny_rearm_token
        adapter = self._semantic_adapter
        if adapter is not None:
            adapter_token = getattr(adapter, "deny_rearm_token", None)
            token = (
                adapter_token
                if adapter_token == self._deny_rearm_requested_token
                else None
            )
        if token is None or token[2] != self._detector_epoch:
            return None
        return token

    def _consume_deny_rearm_token(self, token: tuple[int, int, int]) -> None:
        if self._deny_rearm_token == token:
            self._deny_rearm_token = None
        if self._deny_rearm_requested_token == token:
            self._deny_rearm_requested_token = None
        adapter = self._semantic_adapter
        if adapter is not None:
            adapter.consume_deny_rearm(token)

    async def prepare_deny_rearm(
        self,
        *,
        cleanup_generation: int,
        cutoff_sequence: int,
        expected_detector_epoch: int,
    ) -> bool:
        """Prepare one ordered, Silero-only post-deny boundary proof."""

        values = (cleanup_generation, cutoff_sequence, expected_detector_epoch)
        if any(type(value) is not int or value < 0 for value in values):
            return False
        token = values
        adapter: _VoiceTurnAdapter | None = None
        direct_result: bool | None = None
        cancelled: asyncio.CancelledError | None = None
        async with self._lock:
            if (
                self._closed
                or expected_detector_epoch != self._detector_epoch
                or not self._available
            ):
                return False
            active = self._active_deny_rearm_token()
            if active == token:
                return True
            requested = self._deny_rearm_requested_token
            if (
                requested is not None
                and requested[2] == expected_detector_epoch
                and token[:2] < requested[:2]
            ):
                return False
            self._deny_rearm_requested_token = token
            adapter = self._semantic_adapter
            if adapter is None:
                if not self._load_attempted:
                    load_task = asyncio.create_task(
                        asyncio.to_thread(self._vad.load),
                        name="detector-runtime-deny-rearm-vad-load",
                    )
                    try:
                        loaded = bool(await asyncio.shield(load_task))
                    except asyncio.CancelledError as exc:
                        cancelled = exc
                        try:
                            loaded = bool(await asyncio.shield(load_task))
                        except Exception:
                            loaded = False
                    except Exception:
                        loaded = False
                    self._load_attempted = True
                    self._available = loaded
                if not self._available:
                    direct_result = False
                else:
                    prepare = getattr(
                        self._gate,
                        "prepare_post_deny_silence_boundary",
                        None,
                    )
                    if callable(prepare):
                        try:
                            prepare()
                        except Exception:
                            direct_result = False
                        else:
                            direct_result = bool(
                                self._deny_rearm_requested_token == token
                            )
                            if direct_result:
                                self._deny_rearm_token = token
                    else:
                        direct_result = False
            elif adapter.failed:
                return False

        if adapter is None:
            if cancelled is not None:
                raise cancelled
            return bool(direct_result)

        try:
            await self._ensure_semantic_started(adapter)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        async with self._lock:
            if (
                self._closed
                or adapter is not self._semantic_adapter
                or expected_detector_epoch != self._detector_epoch
                or self._deny_rearm_requested_token != token
            ):
                return False

        operation = asyncio.create_task(
            adapter.prepare_deny_rearm(token),
            name="detector-runtime-deny-rearm",
        )
        cancelled = None
        try:
            prepared = bool(await asyncio.shield(operation))
        except asyncio.CancelledError as exc:
            cancelled = exc
            try:
                prepared = bool(await asyncio.shield(operation))
            except Exception:
                prepared = False
        except Exception:
            prepared = False

        async with self._lock:
            valid = bool(
                prepared
                and not self._closed
                and adapter is self._semantic_adapter
                and expected_detector_epoch == self._detector_epoch
                and self._deny_rearm_requested_token == token
                and adapter.deny_rearm_token == token
            )
            if valid:
                self._deny_rearm_token = token
        if cancelled is not None:
            raise cancelled
        return valid

    async def feed(
        self,
        pcm16: bytes,
        *,
        speech_probability: float | None = None,
        rnnoise_available: bool | None = None,
        rnnoise_evidence: RnnoiseEvidence | None = None,
        ingress_token: VoiceIngressToken | None = None,
        allow_baseline_update: bool = False,
    ) -> DetectorFeedResult:
        if not isinstance(pcm16, bytes) or len(pcm16) % 2:
            raise ValueError("DetectorRuntime requires complete PCM16 bytes")
        if not pcm16:
            return DetectorFeedResult((), self._available)
        if speech_probability is not None and not 0.0 <= speech_probability <= 1.0:
            raise ValueError("speech_probability must be within [0, 1]")
        if rnnoise_available is None:
            rnnoise_available = speech_probability is not None
        adapter = self._semantic_adapter
        if adapter is not None:
            deny_rearm_token = self._active_deny_rearm_token()
            self._events.clear()
            effective_ingress = (
                ingress_token
                or self._ingress_token
                or VoiceIngressToken(
                    session_epoch=0,
                    connection_id="detector-feed-compat",
                    lease_generation=0,
                    route_generation=0,
                    audio_generation=0,
                )
            )
            submitted = await self.submit_audio(
                pcm16,
                ingress_token=effective_ingress,
                sample_rate_hz=16_000,
                speech_probability=speech_probability,
                rnnoise_available=bool(rnnoise_available),
                rnnoise_evidence=rnnoise_evidence,
                allow_baseline_update=allow_baseline_update,
            )
            if submitted.status is DetectorSubmitStatus.SKIPPED_QUIET:
                return DetectorFeedResult(
                    (),
                    submitted.throttle_available,
                    throttle_action=submitted.throttle_action,
                )
            if submitted.status is not DetectorSubmitStatus.ACCEPTED:
                return DetectorFeedResult(
                    (),
                    submitted.throttle_available,
                    endpointing_available=submitted.endpointing_available,
                    throttle_action=submitted.throttle_action,
                )
            await adapter.wait_idle()
            if adapter.failed:
                failure = adapter.failure
                endpointing_available = getattr(failure, "stage", None) not in {
                    "smart_turn",
                    "consumer",
                }
                return DetectorFeedResult(
                    (),
                    False,
                    endpointing_available=endpointing_available,
                    throttle_action=submitted.throttle_action,
                )
            events = tuple(self._events)
            if (
                deny_rearm_token is not None
                and SpeechActivityEvent.CANDIDATE_PAUSE in events
            ):
                async with self._lock:
                    self._consume_deny_rearm_token(deny_rearm_token)
            if any(
                event
                in {
                    SpeechActivityEvent.SPEECH_STARTED,
                    SpeechActivityEvent.SPEECH_RESUMED,
                }
                for event in events
            ):
                self._speech_active = True
            self._speaker_rejection_prepare_diagnostics[
                "detector_feed_semantic_identity_omitted_count"
            ] += 1
            return DetectorFeedResult(
                events,
                adapter.throttle_available,
                throttle_action=submitted.throttle_action,
            )
        async with self._lock:
            if self._closed:
                self._speaker_rejection_prepare_diagnostics[
                    "detector_feed_closed_count"
                ] += 1
                return DetectorFeedResult((), False)
            if not self._available:
                self._speaker_rejection_prepare_diagnostics[
                    "detector_feed_unavailable_count"
                ] += 1
                return DetectorFeedResult((), False)
            if (
                self._active_deny_rearm_token() is None
                and self._deny_rearm_token is not None
            ):
                self._gate.reset()
                self._deny_rearm_token = None
            effective_ingress = ingress_token or VoiceIngressToken(
                session_epoch=0,
                connection_id="detector-feed-compat",
                lease_generation=0,
                route_generation=0,
                audio_generation=0,
            )
            if self._ingress_token is None:
                self._ingress_token = effective_ingress
            elif self._ingress_token != effective_ingress:
                return DetectorFeedResult((), False, endpointing_available=False)
            evidence = rnnoise_evidence or RnnoiseEvidence.from_legacy_probability(
                speech_probability,
                available=bool(rnnoise_available),
            )
            candidate_admission_open = bool(
                self._candidate_open
                or self._provider_candidate_fence is not None
                or self._active_deny_rearm_token() is not None
            )
            deny_rearm_token = self._active_deny_rearm_token()
            throttle = self._throttle_policy.decide(
                evidence,
                candidate_open=candidate_admission_open,
                allow_baseline_update=allow_baseline_update,
            )
            if throttle.action is ThrottleAction.SKIP_IDLE_PCM:
                return DetectorFeedResult(
                    (),
                    True,
                    throttle_action=throttle.action,
                )
            if not self._load_attempted:
                self._load_attempted = True
                try:
                    self._available = bool(await asyncio.to_thread(self._vad.load))
                except Exception:
                    self._available = False
                    self._speaker_rejection_prepare_diagnostics[
                        "detector_vad_load_exception_count"
                    ] += 1
                if not self._available:
                    self._provider_micro_event_aggregate = None
                    self._sealed_provider_micro_event = None
                    self._clear_provider_segment_state()
                    if not self._speaker_rejection_prepare_diagnostics[
                        "detector_vad_load_exception_count"
                    ]:
                        self._speaker_rejection_prepare_diagnostics[
                            "detector_vad_load_unavailable_count"
                        ] += 1
                    return DetectorFeedResult(
                        (),
                        False,
                        throttle_action=throttle.action,
                    )
            silero_result: SileroFeedResult | None = None
            try:
                feed_with_evidence = getattr(
                    self._gate,
                    "feed_with_evidence",
                    None,
                )
                if self._provider_micro_event_enabled and callable(feed_with_evidence):
                    raw_silero_result = await asyncio.to_thread(
                        feed_with_evidence,
                        pcm16,
                    )
                    if type(raw_silero_result) is SileroFeedResult:
                        silero_result = raw_silero_result
                        events = tuple(raw_silero_result.events)
                    else:
                        events = tuple(getattr(raw_silero_result, "events", ()))
                else:
                    events = tuple(await asyncio.to_thread(self._gate.feed, pcm16))
            except Exception:
                self._available = False
                self._provider_micro_event_aggregate = None
                self._sealed_provider_micro_event = None
                self._clear_provider_segment_state()
                self._speaker_rejection_prepare_diagnostics[
                    "detector_gate_exception_count"
                ] += 1
                return DetectorFeedResult(
                    (),
                    False,
                    throttle_action=throttle.action,
                )
            if deny_rearm_token is None:
                self._candidate_open = True
            self._sequence_no += 1
            identity = DetectorIngressIdentity(
                ingress_token=effective_ingress,
                detector_epoch=self._detector_epoch,
                sequence_no=self._sequence_no,
            )
            candidate = DetectorCandidateKey(
                self._detector_epoch,
                self._candidate_generation,
            )
            if deny_rearm_token is None:
                self._observe_provider_micro_event(
                    candidate,
                    silero=silero_result,
                    events=events,
                    rnnoise=throttle.evidence.rnnoise,
                    onset_threshold=throttle.onset_threshold,
                    chunk_duration_ms=(len(pcm16) * 1_000 + 31_999) // 32_000,
                )
                if (
                    throttle.action is ThrottleAction.PREWARM
                    and self._on_event is not None
                    and self._policy_event_candidate != candidate
                ):
                    self._policy_event_candidate = candidate
                    await self._on_event(DetectorTransportPrewarmEvent(identity))
                if any(
                    event
                    in {
                        SpeechActivityEvent.SPEECH_STARTED,
                        SpeechActivityEvent.SPEECH_RESUMED,
                    }
                    for event in events
                ):
                    self._speech_active = True
                for event in events:
                    self._throttle_policy.observe_silero(event)
            if (
                deny_rearm_token is not None
                and SpeechActivityEvent.CANDIDATE_PAUSE in events
            ):
                self._consume_deny_rearm_token(deny_rearm_token)
        return DetectorFeedResult(
            events,
            True,
            throttle_action=throttle.action,
            identity=identity,
            candidate=candidate,
        )

    def observe_provider_audio(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
    ) -> None:
        """Observe Provider PCM only after the ASR dispatcher admits it."""

        if (
            self._closed
            or self._semantic_adapter is not None
            or not isinstance(pcm16, bytes)
            or not pcm16
            or len(pcm16) % 2
            or sample_rate_hz <= 0
        ):
            return
        if self._provider_segment_ordered_mode:
            # Mixing the legacy un-fenced entry point with ordered ownership
            # makes the physical segment assignment unknowable. Keep ASR
            # running, but revoke all speaker and micro-event evidence.
            self._mark_provider_segments_incomplete()
            candidate_key = DetectorCandidateKey(
                self._detector_epoch,
                self._candidate_generation,
            )
            self._mark_provider_micro_event_ambiguous(candidate_key)
            return
        self._observe_provider_audio_legacy(
            pcm16,
            sample_rate_hz=sample_rate_hz,
        )

    def _observe_provider_audio_legacy(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
    ) -> None:
        candidate = self._speaker_shadow_candidate
        if candidate is None:
            candidate = self._open_speaker_shadow_candidate("provider_candidate")
        if candidate is None or candidate.scope != "provider_candidate":
            return
        self._submit_speaker_shadow(
            pcm16,
            sample_rate_hz=sample_rate_hz,
            candidate=candidate,
        )

    def _submit_provider_evidence_capture_locked(
        self,
        state: _ProviderSpeakerEvidenceState,
        pcm16: bytes,
        *,
        sample_start_16k: int,
    ) -> SpeakerShadowCaptureResult:
        """Capture all canonical PCM, rotating buffer-only coverage owners."""

        shadow = self._speaker_shadow
        if not isinstance(shadow, SpeakerShadowCaptureStatus):
            return self._unavailable_provider_speaker_evidence_update(
                state,
                sequence_no=state.last_sequence_no or 0,
            ).capture
        input_samples = len(pcm16) // 2
        offset = 0
        last_capture: SpeakerShadowCaptureResult | None = None
        while offset < input_samples:
            candidate = state.active_candidate or state.lease.candidate
            try:
                capture = shadow.submit_capture(
                    bytes(memoryview(pcm16)[offset * 2 :]),
                    sample_rate_hz=16_000,
                    candidate=candidate,
                )
            except Exception:
                capture = None
            if (
                type(capture) is not SpeakerShadowCaptureResult
                or capture.disposition is SpeakerShadowCaptureDisposition.UNAVAILABLE
            ):
                return SpeakerShadowCaptureResult(
                    disposition=SpeakerShadowCaptureDisposition.UNAVAILABLE,
                    accepted_sample_count=0,
                    cumulative_sample_count=state.cumulative_sample_count,
                    completed_window_sample_count=state.completed_window_sample_count,
                    decision_state=SpeakerShadowCaptureDecisionState.UNAVAILABLE,
                )
            accepted = capture.accepted_sample_count
            if accepted > input_samples - offset:
                return SpeakerShadowCaptureResult(
                    disposition=SpeakerShadowCaptureDisposition.UNAVAILABLE,
                    accepted_sample_count=0,
                    cumulative_sample_count=state.cumulative_sample_count,
                    completed_window_sample_count=state.completed_window_sample_count,
                    decision_state=SpeakerShadowCaptureDecisionState.UNAVAILABLE,
                )
            if candidate == state.lease.candidate:
                state.completed_window_sample_count = max(
                    state.completed_window_sample_count,
                    capture.completed_window_sample_count,
                )
                if capture.disposition is SpeakerShadowCaptureDisposition.COMPLETE:
                    state.capture_state = _ProviderShadowCaptureState.COMPLETE
                    state.completed_window_sample_count = max(
                        state.completed_window_sample_count,
                        capture.cumulative_sample_count,
                    )
            if accepted:
                entry = next(
                    (
                        item
                        for item in reversed(state.coverage_candidates)
                        if item.candidate == candidate
                    ),
                    None,
                )
                if entry is None:
                    return SpeakerShadowCaptureResult(
                        disposition=SpeakerShadowCaptureDisposition.UNAVAILABLE,
                        accepted_sample_count=0,
                        cumulative_sample_count=state.cumulative_sample_count,
                        completed_window_sample_count=state.completed_window_sample_count,
                        decision_state=SpeakerShadowCaptureDecisionState.UNAVAILABLE,
                    )
                entry.end_sample_16k += accepted
                offset += accepted
                last_capture = capture
            if offset >= input_samples:
                break
            if accepted == 0 and capture.disposition is not SpeakerShadowCaptureDisposition.COMPLETE:
                return SpeakerShadowCaptureResult(
                    disposition=SpeakerShadowCaptureDisposition.UNAVAILABLE,
                    accepted_sample_count=0,
                    cumulative_sample_count=state.cumulative_sample_count,
                    completed_window_sample_count=state.completed_window_sample_count,
                    decision_state=SpeakerShadowCaptureDecisionState.UNAVAILABLE,
                )

            continuation = self._allocate_provider_segment_candidate()
            defer_coverage = getattr(shadow, "defer_coverage_candidate", None)
            try:
                deferred = bool(
                    continuation is not None
                    and callable(defer_coverage)
                    and defer_coverage(continuation)
                )
            except Exception:
                deferred = False
            if continuation is None or not deferred:
                if continuation is not None:
                    self._speaker_candidate_owner_generations.pop(continuation, None)
                return SpeakerShadowCaptureResult(
                    disposition=SpeakerShadowCaptureDisposition.UNAVAILABLE,
                    accepted_sample_count=0,
                    cumulative_sample_count=state.cumulative_sample_count,
                    completed_window_sample_count=state.completed_window_sample_count,
                    decision_state=SpeakerShadowCaptureDecisionState.UNAVAILABLE,
                )
            continuation_start = sample_start_16k + offset
            state.active_candidate = continuation
            state.coverage_candidates.append(
                _ProviderSpeakerCoverageCandidate(
                    candidate=continuation,
                    start_sample_16k=continuation_start,
                    end_sample_16k=continuation_start,
                )
            )

        if last_capture is None:
            return SpeakerShadowCaptureResult(
                disposition=SpeakerShadowCaptureDisposition.UNAVAILABLE,
                accepted_sample_count=0,
                cumulative_sample_count=state.cumulative_sample_count,
                completed_window_sample_count=state.completed_window_sample_count,
                decision_state=SpeakerShadowCaptureDecisionState.UNAVAILABLE,
            )
        total_cumulative = (
            sample_start_16k + input_samples - state.start_sample_16k
        )
        return SpeakerShadowCaptureResult(
            disposition=(
                SpeakerShadowCaptureDisposition.COMPLETE
                if state.capture_state is _ProviderShadowCaptureState.COMPLETE
                else SpeakerShadowCaptureDisposition.ACCEPTED
            ),
            accepted_sample_count=input_samples,
            cumulative_sample_count=total_cumulative,
            completed_window_sample_count=state.completed_window_sample_count,
            # COMPLETE means the primary scoring window is fully captured;
            # only the ordered evidence callback may publish a score fact.
            decision_state=SpeakerShadowCaptureDecisionState.PENDING,
        )

    def _account_provider_audio_without_evidence_locked(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        identity: DetectorIngressIdentity,
        sequence_no: int,
        speaker_evidence_lease: ProviderSpeakerEvidenceLease | None,
        failure_context: AudioFailureContext | None = None,
    ) -> ProviderAudioAccountingReceipt | None:
        """Advance a valid ordered timeline without allocating a candidate.

        An optional retired lease identifies the unavailable evidence only.
        It cannot retire a different live owner or authorize any scoring.
        Caller validation and this operation run under the same Detector lock.
        """

        state = self._provider_speaker_evidence_state
        if speaker_evidence_lease is not None and (
            speaker_evidence_lease._owner is not self._provider_speaker_evidence_owner
            or speaker_evidence_lease.detector_epoch != self._detector_epoch
            or type(speaker_evidence_lease.lease_generation) is not int
        ):
            self._record_provider_audio_failure(failure_context, "accounting_lease_identity")
            return None
        if state is not None and state.lease is not speaker_evidence_lease:
            self._record_provider_audio_failure(failure_context, "accounting_other_owner")
            return None
        previous_sequence = self._provider_segment_last_sequence_no
        if previous_sequence is not None and sequence_no != previous_sequence + 1:
            diagnostic = (
                "provider_speaker_segment_sequence_stale_count"
                if sequence_no <= previous_sequence
                else "provider_speaker_segment_sequence_gap_count"
            )
            self._speaker_rejection_prepare_diagnostics[diagnostic] += 1
            self._record_provider_audio_failure(failure_context, "accounting_sequence")
            return None
        sample_count, remainder = divmod(len(pcm16) // 2 * 16_000, sample_rate_hz)
        if sample_count <= 0 or remainder:
            self._record_provider_audio_failure(failure_context, "accounting_sample_alignment")
            return None

        settlement = None
        if state is not None:
            self._abandon_provider_speaker_evidence_locked(state, reason="evidence_unavailable")
            settlement = self._provider_speaker_evidence_settlements[state.lease.lease_generation][0]
        elif speaker_evidence_lease is not None:
            issued = self._provider_speaker_evidence_settlements.get(
                speaker_evidence_lease.lease_generation, ()
            )
            if not issued or not self.validate_provider_speaker_evidence_settlement(
                issued[1], lease=speaker_evidence_lease,
            ):
                self._record_provider_audio_failure(failure_context, "accounting_retirement_unproven")
                return None
            settlement = issued[1]
        self._mark_provider_segments_incomplete()
        self._provider_legacy_segment_evidence_complete = False
        self._provider_segment_successor_evidence_incomplete = True
        self._mark_provider_micro_event_ambiguous(
            DetectorCandidateKey(identity.detector_epoch, self._candidate_generation)
        )
        start_sample_16k = self._provider_audio_sample_cursor_16k
        self._provider_segment_last_sequence_no = sequence_no
        self._provider_audio_sample_cursor_16k += sample_count
        self._signal_provider_audio_observation()
        return ProviderAudioAccountingReceipt(
            detector_epoch=self._detector_epoch,
            timeline_generation=self._provider_audio_timeline_generation,
            sequence_no=sequence_no,
            start_sample_16k=start_sample_16k,
            end_sample_16k=self._provider_audio_sample_cursor_16k,
            evidence_settlement=settlement,
        )

    def _record_provider_audio_failure(
        self, context: AudioFailureContext | None, check: str,
    ) -> None:
        if context is not None:
            state = self._provider_speaker_evidence_state
            context.fail(check, actual={
                "detector_epoch": self._detector_epoch,
                "timeline_generation": self._provider_audio_timeline_generation,
                "sequence_no": self._provider_segment_last_sequence_no,
                "sample_cursor_16k": self._provider_audio_sample_cursor_16k,
                "lease_generation": state.lease.lease_generation if state is not None else None,
            })

    async def observe_provider_audio_ordered(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        identity: DetectorIngressIdentity,
        sequence_no: int,
        split_before_audio: bool,
        evidence_complete: bool = True,
        speaker_evidence_lease: ProviderSpeakerEvidenceLease | None = None,
        accounting_only: bool = False,
        expected_timeline_generation: int | None = None,
        failure_context: AudioFailureContext | None = None,
    ) -> ProviderSpeakerEvidenceUpdate | ProviderAudioAccountingReceipt | None:
        """Assign admitted Provider PCM to a physical-segment FIFO.

        Detector identity fences the source. ``sequence_no`` is the separate
        Provider-dispatch admission sequence, so pre-roll and detector-only
        quiet frames cannot manufacture gaps.
        """

        if (
            not isinstance(pcm16, bytes)
            or not pcm16
            or len(pcm16) % 2
            or sample_rate_hz <= 0
            or type(identity) is not DetectorIngressIdentity
            or type(sequence_no) is not int
            or sequence_no <= 0
            or type(split_before_audio) is not bool
            or type(evidence_complete) is not bool
            or type(accounting_only) is not bool
            or (accounting_only and evidence_complete)
            or (
                expected_timeline_generation is not None
                and type(expected_timeline_generation) is not int
            )
            or (
                speaker_evidence_lease is not None
                and type(speaker_evidence_lease) is not ProviderSpeakerEvidenceLease
            )
        ):
            self._record_provider_audio_failure(failure_context, "audio_argument_invalid")
            return
        async with self._lock:
            if (
                self._closed
                or self._semantic_adapter is not None
                or identity.detector_epoch != self._detector_epoch
                or identity.ingress_token != self._ingress_token
                or identity.sequence_no > self._sequence_no
                or (
                    expected_timeline_generation is not None
                    and expected_timeline_generation
                    != self._provider_audio_timeline_generation
                )
            ):
                check = (
                    "detector_closed" if self._closed else
                    "detector_semantic_owner" if self._semantic_adapter is not None else
                    "detector_epoch_changed" if identity.detector_epoch != self._detector_epoch else
                    "detector_ingress_changed" if identity.ingress_token != self._ingress_token else
                    "detector_sequence_ahead" if identity.sequence_no > self._sequence_no else
                    "audio_timeline_changed"
                )
                self._record_provider_audio_failure(failure_context, check)
                return
            if accounting_only:
                return self._account_provider_audio_without_evidence_locked(
                    pcm16,
                    sample_rate_hz=sample_rate_hz,
                    identity=identity,
                    sequence_no=sequence_no,
                    speaker_evidence_lease=speaker_evidence_lease,
                    failure_context=failure_context,
                )
            evidence_state = (
                self._provider_speaker_evidence_state_for(speaker_evidence_lease)
                if speaker_evidence_lease is not None
                else None
            )
            if speaker_evidence_lease is not None and evidence_state is None:
                self._speaker_rejection_prepare_diagnostics[
                    "provider_speaker_evidence_lease_stale_count"
                ] += 1
                state = self._provider_speaker_evidence_state
                check = (
                    "speaker_lease_foreign_owner"
                    if speaker_evidence_lease._owner is not self._provider_speaker_evidence_owner else
                    "speaker_lease_epoch_changed"
                    if speaker_evidence_lease.detector_epoch != self._detector_epoch
                    or speaker_evidence_lease.candidate.detector_epoch != self._detector_epoch else
                    "speaker_lease_retired" if state is None else "speaker_lease_other_owner"
                )
                self._record_provider_audio_failure(failure_context, check)
                return None
            previous_sequence = self._provider_segment_last_sequence_no
            evidence_previous_sequence = (
                evidence_state.last_sequence_no if evidence_state is not None else None
            )
            if (previous_sequence is not None and sequence_no <= previous_sequence) or (
                evidence_previous_sequence is not None
                and sequence_no <= evidence_previous_sequence
            ):
                self._speaker_rejection_prepare_diagnostics[
                    "provider_speaker_segment_sequence_stale_count"
                ] += 1
                self._record_provider_audio_failure(failure_context, "audio_sequence_stale")
                return
            self._provider_segment_last_sequence_no = sequence_no
            sequence_gap = bool(
                previous_sequence is not None and sequence_no != previous_sequence + 1
            ) or bool(
                evidence_previous_sequence is not None
                and sequence_no != evidence_previous_sequence + 1
            )
            if sequence_gap:
                self._speaker_rejection_prepare_diagnostics[
                    "provider_speaker_segment_sequence_gap_count"
                ] += 1

            if not self._provider_segment_ordered_mode and evidence_state is not None:
                # Stable evidence owns one candidate across physical Provider
                # segments, so deferred per-segment scoring is unnecessary.
                self._provider_segment_deferred_support = True
                self._provider_segment_ordered_mode = True
            elif not self._provider_segment_ordered_mode:
                support = self._provider_segment_deferred_support
                if support is None:
                    shadow = self._speaker_shadow
                    if isinstance(
                        shadow,
                        SpeakerShadowDeferredCandidateStatus,
                    ):
                        probe = SpeakerShadowCandidateKey(
                            detector_epoch=self._detector_epoch,
                            shadow_generation=self._speaker_shadow_generation,
                            scope="provider_candidate",
                        )
                        try:
                            support = bool(shadow.supports_deferred_candidate(probe))
                        except Exception:
                            support = "error"
                    else:
                        support = "unsupported"
                    self._provider_segment_deferred_support = support
                if support is not True:
                    if support == "error":
                        self._provider_legacy_segment_evidence_complete = False
                        self._mark_provider_micro_event_ambiguous(
                            DetectorCandidateKey(
                                self._detector_epoch,
                                self._candidate_generation,
                            )
                        )
                    self._observe_provider_audio_legacy(
                        pcm16,
                        sample_rate_hz=sample_rate_hz,
                    )
                    return
                self._provider_segment_ordered_mode = True

            input_sample_count = len(pcm16) // 2
            canonical_sample_numerator = input_sample_count * 16_000
            canonical_sample_count, canonical_remainder = divmod(
                canonical_sample_numerator,
                sample_rate_hz,
            )
            if canonical_sample_count <= 0 or canonical_remainder:
                self._record_provider_audio_failure(failure_context, "audio_sample_alignment")
                self._provider_segment_alignment_lost = True
                self._mark_provider_segments_incomplete()
                self._mark_provider_micro_event_ambiguous(
                    DetectorCandidateKey(
                        self._detector_epoch,
                        self._candidate_generation,
                    )
                )
                return
            sample_start_16k = self._provider_audio_sample_cursor_16k
            sample_end_16k = sample_start_16k + canonical_sample_count
            self._provider_audio_sample_cursor_16k = sample_end_16k
            self._signal_provider_audio_observation()

            sealed_through = self._provider_speaker_sealed_through_sequence_no
            if sealed_through is not None and identity.sequence_no <= sealed_through:
                self._record_provider_audio_failure(failure_context, "audio_after_sealed_fence")
                fence = self._provider_candidate_fence
                if (
                    fence is not None
                    and identity.sequence_no <= fence.through_sequence_no
                ):
                    self._sealed_provider_candidate_rejection = None
                    self._sealed_provider_micro_event = None
                return

            observed_at = time.monotonic()
            if (
                evidence_state is not None
                and evidence_state.last_progress_at is not None
                and observed_at - evidence_state.last_progress_at
                >= _PROVIDER_SEGMENT_EXPIRY_SECONDS
            ):
                unavailable = self._unavailable_provider_speaker_evidence_update(
                    evidence_state,
                    sequence_no=sequence_no,
                )
                self._abandon_provider_speaker_evidence_locked(evidence_state)
                self._expire_provider_segments(observed_at)
                return unavailable
            self._expire_provider_segments(observed_at)
            if sequence_gap:
                self._mark_provider_segments_incomplete()
                self._mark_provider_micro_event_ambiguous(
                    DetectorCandidateKey(
                        self._detector_epoch,
                        self._candidate_generation,
                    )
                )
                if evidence_state is not None:
                    unavailable = self._unavailable_provider_speaker_evidence_update(
                        evidence_state,
                        sequence_no=sequence_no,
                    )
                    self._abandon_provider_speaker_evidence_locked(evidence_state)
                    return unavailable
            if evidence_state is not None and not evidence_complete:
                self._mark_provider_segments_incomplete()
                unavailable = self._unavailable_provider_speaker_evidence_update(
                    evidence_state,
                    sequence_no=sequence_no,
                )
                self._abandon_provider_speaker_evidence_locked(evidence_state)
                return unavailable
            segment = (
                self._provider_speaker_segments[-1]
                if self._provider_speaker_segments
                else None
            )
            inherited_progress_at = (
                segment.last_progress_at
                if segment is not None
                else observed_at - _PROVIDER_SEGMENT_EXPIRY_SECONDS
            )
            create_segment = segment is None or split_before_audio
            overlap = bool(split_before_audio and segment is not None)
            if create_segment:
                if len(self._provider_speaker_segments) >= (
                    _PROVIDER_SEGMENT_FIFO_LIMIT
                ):
                    self._record_provider_audio_failure(failure_context, "audio_segment_capacity")
                    self._speaker_rejection_prepare_diagnostics[
                        "provider_speaker_segment_overflow_fail_open_count"
                    ] += 1
                    self._provider_segment_alignment_lost = True
                    self._mark_provider_segments_incomplete()
                    self._mark_provider_micro_event_ambiguous(
                        DetectorCandidateKey(
                            self._detector_epoch,
                            self._candidate_generation,
                        )
                    )
                    return
                if overlap:
                    detector_candidate = DetectorCandidateKey(
                        self._detector_epoch,
                        segment.detector_candidate.candidate_generation + 1,
                    )
                else:
                    detector_candidate = DetectorCandidateKey(
                        self._detector_epoch,
                        self._candidate_generation,
                    )
                candidate = (
                    None
                    if evidence_state is not None
                    else self._allocate_provider_segment_candidate()
                )
                shadow = self._speaker_shadow
                control = (
                    shadow
                    if isinstance(shadow, SpeakerShadowDeferredCandidateControl)
                    else None
                )
                segment_complete = bool(
                    evidence_complete
                    and (candidate is not None or evidence_state is not None)
                    and not sequence_gap
                    and not self._provider_segment_alignment_lost
                    and not self._provider_segment_successor_evidence_incomplete
                )
                carried_incomplete = (
                    self._provider_segment_successor_evidence_incomplete
                )
                self._provider_segment_successor_evidence_incomplete = False
                deferred = bool(overlap and evidence_state is None)
                deferred_accepted = False
                if candidate is not None and deferred:
                    if control is not None:
                        try:
                            deferred_accepted = bool(control.defer_candidate(candidate))
                        except Exception:
                            deferred_accepted = False
                    segment_complete = bool(segment_complete and deferred_accepted)
                segment = _ProviderSpeakerSegment(
                    candidate=candidate,
                    detector_candidate=detector_candidate,
                    first_identity=identity,
                    last_identity=identity,
                    created_at=observed_at,
                    ownership_complete=segment_complete,
                    shadow_capture_state=(
                        _ProviderShadowCaptureState.COLLECTING
                        if segment_complete
                        else _ProviderShadowCaptureState.UNAVAILABLE
                    ),
                    shadow_completed_window_sample_count=0,
                    last_progress_at=(
                        observed_at if segment_complete else inherited_progress_at
                    ),
                    deferred=deferred,
                    deferred_accepted=deferred_accepted,
                    start_sample_16k=sample_start_16k,
                    end_sample_16k=sample_end_16k,
                    tentative=overlap,
                )
                self._provider_speaker_segments.append(segment)
                if candidate is not None:
                    self._publish_speaker_candidate_binding(
                        candidate,
                        detector_candidate,
                    )
                if overlap:
                    self._speaker_rejection_prepare_diagnostics[
                        "provider_speaker_segment_split_count"
                    ] += 1
                    if deferred_accepted:
                        self._speaker_rejection_prepare_diagnostics[
                            "provider_speaker_segment_deferred_count"
                        ] += 1
                elif carried_incomplete:
                    self._mark_provider_micro_event_ambiguous(detector_candidate)
            else:
                segment.last_identity = identity
                if segment.end_sample_16k != sample_start_16k:
                    segment.ownership_complete = False
                    self._provider_segment_alignment_lost = True
                    self._mark_provider_segment_ownership_ambiguous(segment)
                segment.end_sample_16k = sample_end_16k

            if segment is None:
                return
            segment.last_identity = identity
            if not sequence_gap and evidence_complete and segment.ownership_complete:
                segment.last_progress_at = observed_at
            if not evidence_complete:
                segment.ownership_complete = False
                if not split_before_audio:
                    self._provider_segment_successor_evidence_incomplete = True
                self._mark_provider_micro_event_ambiguous(segment.detector_candidate)
            candidate = segment.candidate
            if evidence_state is not None:
                candidate = (
                    evidence_state.active_candidate
                    or evidence_state.lease.candidate
                )
                if not evidence_state.binding_published:
                    self._publish_speaker_candidate_binding(
                        candidate,
                        segment.detector_candidate,
                    )
                    evidence_state.binding_published = bool(
                        candidate in self._speaker_candidate_turn_bindings
                    )
            shadow = self._speaker_shadow
            may_submit = bool(not segment.deferred or segment.deferred_accepted)
            if candidate is not None and shadow is not None and may_submit:
                try:
                    if isinstance(shadow, SpeakerShadowCaptureStatus):
                        capture = (
                            self._submit_provider_evidence_capture_locked(
                                evidence_state,
                                pcm16,
                                sample_start_16k=sample_start_16k,
                            )
                            if evidence_state is not None
                            and sample_rate_hz == 16_000
                            else shadow.submit_capture(
                                pcm16,
                                sample_rate_hz=sample_rate_hz,
                                candidate=candidate,
                            )
                        )
                        if type(capture) is not SpeakerShadowCaptureResult:
                            capture_state = _ProviderShadowCaptureState.UNAVAILABLE
                        elif (
                            capture.disposition
                            is SpeakerShadowCaptureDisposition.COMPLETE
                        ):
                            capture_state = _ProviderShadowCaptureState.COMPLETE
                            segment.shadow_completed_window_sample_count = max(
                                segment.shadow_completed_window_sample_count,
                                capture.completed_window_sample_count,
                            )
                        elif (
                            capture.disposition
                            is SpeakerShadowCaptureDisposition.ACCEPTED
                        ):
                            capture_state = _ProviderShadowCaptureState.COLLECTING
                        else:
                            capture_state = _ProviderShadowCaptureState.UNAVAILABLE
                    else:
                        submitted = bool(
                            shadow.submit(
                                pcm16,
                                sample_rate_hz=sample_rate_hz,
                                candidate=candidate,
                            )
                        )
                        capture_state = (
                            _ProviderShadowCaptureState.COLLECTING
                            if submitted
                            else _ProviderShadowCaptureState.UNAVAILABLE
                        )
                except Exception:
                    capture_state = _ProviderShadowCaptureState.UNAVAILABLE
                if capture_state is _ProviderShadowCaptureState.UNAVAILABLE:
                    segment.shadow_capture_state = capture_state
                    self._mark_provider_micro_event_ambiguous(
                        segment.detector_candidate
                    )
                elif (
                    segment.shadow_capture_state
                    is not _ProviderShadowCaptureState.UNAVAILABLE
                ):
                    if (
                        capture_state is _ProviderShadowCaptureState.COMPLETE
                        or segment.shadow_capture_state
                        is not _ProviderShadowCaptureState.COMPLETE
                    ):
                        segment.shadow_capture_state = capture_state
            else:
                segment.shadow_capture_state = _ProviderShadowCaptureState.UNAVAILABLE
                self._mark_provider_micro_event_ambiguous(segment.detector_candidate)
                capture_state = _ProviderShadowCaptureState.UNAVAILABLE
            if evidence_state is not None:
                if capture_state is _ProviderShadowCaptureState.UNAVAILABLE:
                    unavailable = self._unavailable_provider_speaker_evidence_update(
                        evidence_state,
                        sequence_no=sequence_no,
                    )
                    self._abandon_provider_speaker_evidence_locked(evidence_state)
                    self._schedule_provider_segment_expiry()
                    return unavailable
                evidence_state.last_sequence_no = sequence_no
                evidence_state.last_progress_at = observed_at
                evidence_state.capture_state = capture_state
                if isinstance(shadow, SpeakerShadowCaptureStatus):
                    assert type(capture) is SpeakerShadowCaptureResult
                    evidence_state.cumulative_sample_count = capture.cumulative_sample_count
                    evidence_state.completed_window_sample_count = max(
                        evidence_state.completed_window_sample_count,
                        capture.completed_window_sample_count,
                    )
                    capture_result = capture
                else:
                    accepted_samples = len(pcm16) // 2
                    evidence_state.cumulative_sample_count += accepted_samples
                    capture_result = SpeakerShadowCaptureResult(
                        disposition=SpeakerShadowCaptureDisposition.ACCEPTED,
                        accepted_sample_count=accepted_samples,
                        cumulative_sample_count=(
                            evidence_state.cumulative_sample_count
                        ),
                        completed_window_sample_count=0,
                        decision_state=SpeakerShadowCaptureDecisionState.PENDING,
                    )
                # The lease clock follows the most recent legal Provider PCM,
                # not the creation time of the oldest physical segment.
                for retained_segment in self._provider_speaker_segments:
                    retained_segment.last_progress_at = observed_at
                self._schedule_provider_segment_expiry()
                return ProviderSpeakerEvidenceUpdate(
                    lease=evidence_state.lease,
                    capture=capture_result,
                    sequence_no=sequence_no,
                    last_progress_at=observed_at,
                )
            self._schedule_provider_segment_expiry()

    def _fail_provider_exact_reconcile_locked(
        self,
    ) -> ProviderSpeakerPresealVerdict:
        self._speaker_rejection_prepare_diagnostics[
            "provider_speaker_segment_exact_reconcile_failed_count"
        ] += 1
        return self._reserve_provider_unknown_preseal_locked()

    async def reconcile_provider_endpoint(
        self,
        boundary: ProviderAudioRange,
    ) -> ProviderSpeakerPresealVerdict | None:
        """Atomically reserve one exact speaker range before ordered sealing."""

        async with self._lock:
            self._prune_completed_provider_preseals()
            if self._closed or self._semantic_adapter is not None:
                return None
            if type(boundary) is not ProviderAudioRange:
                return self._fail_provider_exact_reconcile_locked()
            self._expire_provider_segments(time.monotonic())
            segments = list(self._provider_speaker_segments)
            batch_control = self._batch_reconciliation_control()
            terminal_control = self._terminal_coverage_control()
            if (
                not self._provider_segment_ordered_mode
                or self._provider_segment_alignment_lost
                or not segments
                or (batch_control is None and terminal_control is None)
                or boundary.end_sample_16k > self._provider_audio_sample_cursor_16k
            ):
                return self._fail_provider_exact_reconcile_locked()
            if len(self._provider_preseal_entries) >= _PROVIDER_SEGMENT_FIFO_LIMIT:
                # Preserve all earlier keyed verdicts.  The overflow key gets
                # a standalone unknown tombstone; ordered seal accepts that
                # owner-fenced value without putting it into the bounded table.
                self._retire_unclaimed_provider_segments_locked()
                overflow_generation = self._candidate_generation
                while overflow_generation in self._provider_preseal_entries:
                    overflow_generation += 1
                self._speaker_rejection_prepare_diagnostics[
                    "provider_speaker_segment_exact_reconcile_failed_count"
                ] += 1
                return self._unknown_provider_preseal_verdict_locked(
                    overflow_generation
                )

            for previous, current in zip(segments, segments[1:]):
                if (
                    previous.end_sample_16k != current.start_sample_16k
                    or previous.end_sample_16k <= previous.start_sample_16k
                ):
                    return self._fail_provider_exact_reconcile_locked()
            if segments[-1].end_sample_16k <= segments[-1].start_sample_16k:
                return self._fail_provider_exact_reconcile_locked()

            target_index = next(
                (
                    index
                    for index, segment in enumerate(segments)
                    if segment.end_sample_16k > boundary.start_sample_16k
                ),
                None,
            )
            if target_index is None:
                return self._fail_provider_exact_reconcile_locked()
            target_segment = segments[target_index]
            if (
                boundary.start_sample_16k < target_segment.start_sample_16k
                or boundary.end_sample_16k <= boundary.start_sample_16k
            ):
                return self._fail_provider_exact_reconcile_locked()
            terminal_target = (
                target_segment.shadow_capture_state
                is _ProviderShadowCaptureState.COMPLETE
            )
            scored_window_sample_count = (
                target_segment.shadow_completed_window_sample_count
            )
            if terminal_target and (
                scored_window_sample_count <= 0
                or boundary.start_sample_16k != target_segment.start_sample_16k
                or boundary.end_sample_16k
                < target_segment.start_sample_16k + scored_window_sample_count
            ):
                return self._fail_provider_exact_reconcile_locked()

            covered: list[_ProviderSpeakerSegment] = []
            for segment in segments[target_index:]:
                if segment.start_sample_16k >= boundary.end_sample_16k:
                    break
                if (
                    segment.candidate is None
                    or not segment.evidence_complete
                    or segment.ownership_ambiguous
                    or (segment.deferred and not segment.deferred_accepted)
                ):
                    return self._fail_provider_exact_reconcile_locked()
                covered.append(segment)
            if (
                not covered
                or covered[0] is not target_segment
                or covered[-1].end_sample_16k < boundary.end_sample_16k
            ):
                return self._fail_provider_exact_reconcile_locked()

            consumed = segments[: target_index + len(covered)]
            source_requests: list[SpeakerShadowReconcileSource] = []
            for index, segment in enumerate(consumed):
                candidate = segment.candidate
                if (
                    candidate is None
                    or not segment.evidence_complete
                    or segment.ownership_ambiguous
                ):
                    return self._fail_provider_exact_reconcile_locked()
                sample_count = segment.end_sample_16k - segment.start_sample_16k
                if index < target_index:
                    keep_start = 0
                    keep_end = 0
                else:
                    keep_start = (
                        max(
                            boundary.start_sample_16k,
                            segment.start_sample_16k,
                        )
                        - segment.start_sample_16k
                    )
                    keep_end = (
                        min(
                            boundary.end_sample_16k,
                            segment.end_sample_16k,
                        )
                        - segment.start_sample_16k
                    )
                source_requests.append(
                    SpeakerShadowReconcileSource(
                        candidate=candidate,
                        expected_sample_count=sample_count,
                        keep_start_sample=keep_start,
                        keep_end_sample=keep_end,
                    )
                )

            first_kept = source_requests[target_index]
            target_candidate = target_segment.candidate
            if first_kept.keep_start_sample:
                target_candidate = self._allocate_provider_segment_candidate()
            if target_candidate is None:
                return self._fail_provider_exact_reconcile_locked()
            target_was_deferred = bool(
                target_segment.deferred and target_candidate == target_segment.candidate
            )

            last_segment = covered[-1]
            suffix_sample_count = last_segment.end_sample_16k - boundary.end_sample_16k
            suffix_candidate = (
                self._allocate_provider_segment_candidate()
                if suffix_sample_count > 0
                else None
            )
            if suffix_sample_count > 0 and suffix_candidate is None:
                return self._fail_provider_exact_reconcile_locked()

            expected_target_samples = (
                boundary.end_sample_16k - boundary.start_sample_16k
            )
            receipt: (
                SpeakerShadowBatchReconcileReceipt
                | SpeakerShadowTerminalCoverageReceipt
                | None
            ) = None
            if terminal_target and terminal_control is not None:
                terminal_request = SpeakerShadowTerminalCoverageRequest(
                    sources=tuple(source_requests[target_index:]),
                    target=target_candidate,
                    provider_exact_start_sample=0,
                    provider_exact_end_sample=expected_target_samples,
                    scored_window_start_sample=0,
                    scored_window_end_sample=scored_window_sample_count,
                    suffix=suffix_candidate,
                )
                try:
                    terminal_receipt = (
                        terminal_control.reconcile_finalized_candidate_coverage(
                            terminal_request
                        )
                    )
                except Exception:
                    terminal_receipt = None
                if (
                    type(terminal_receipt) is SpeakerShadowTerminalCoverageReceipt
                    and terminal_receipt.target == target_candidate
                    and terminal_receipt.suffix == suffix_candidate
                    and terminal_receipt.retained_sample_count
                    == scored_window_sample_count
                    and terminal_receipt.covered_sample_count == expected_target_samples
                    and terminal_receipt.terminal_preserved
                ):
                    receipt = terminal_receipt
                elif type(terminal_receipt) is SpeakerShadowTerminalCoverageReceipt:
                    # A typed receipt means the runtime already admitted one
                    # terminal marker.  If its immutable coordinates do not
                    # match this exact request, revoke that marker before
                    # failing open; falling back to a second batch admission
                    # would leave the first operation free to settle late.
                    self._revoke_provider_reconciliation_receipt(terminal_receipt)
                    return self._fail_provider_exact_reconcile_locked()
                elif terminal_receipt is not None:
                    return self._fail_provider_exact_reconcile_locked()

            if receipt is None and batch_control is not None:
                request = SpeakerShadowBatchReconcileRequest(
                    sources=tuple(source_requests),
                    target=target_candidate,
                    suffix=suffix_candidate,
                    finish_target=True,
                )
                try:
                    batch_receipt = batch_control.reconcile_candidate_batch(request)
                except Exception:
                    batch_receipt = None
                if (
                    type(batch_receipt) is SpeakerShadowBatchReconcileReceipt
                    and batch_receipt.target == target_candidate
                    and batch_receipt.suffix == suffix_candidate
                    and batch_receipt.target_sample_count == expected_target_samples
                    and batch_receipt.suffix_sample_count == suffix_sample_count
                ):
                    receipt = batch_receipt
                elif type(batch_receipt) is SpeakerShadowBatchReconcileReceipt:
                    # Admission precedes receipt validation.  A malformed
                    # typed receipt must be revoked even though it cannot be
                    # installed as exact Detector authority.
                    self._revoke_provider_reconciliation_receipt(batch_receipt)
                    return self._fail_provider_exact_reconcile_locked()
                elif batch_receipt is not None:
                    return self._fail_provider_exact_reconcile_locked()

            if receipt is None:
                return self._fail_provider_exact_reconcile_locked()

            terminal_preserved = type(receipt) is SpeakerShadowTerminalCoverageReceipt
            if terminal_preserved:
                for previous in segments[:target_index]:
                    self._finish_provider_segment(
                        previous,
                        activate_deferred=False,
                    )

            expected_generation = self._candidate_generation
            while expected_generation in self._provider_preseal_entries:
                expected_generation += 1
            merged_resume_count = len(covered) - 1
            last_covered_index = target_index + len(covered) - 1
            survivors: list[_ProviderSpeakerSegment] = []
            if suffix_candidate is not None:
                survivors.append(
                    _ProviderSpeakerSegment(
                        candidate=suffix_candidate,
                        detector_candidate=DetectorCandidateKey(
                            self._detector_epoch,
                            expected_generation + 1,
                        ),
                        first_identity=last_segment.first_identity,
                        last_identity=last_segment.last_identity,
                        created_at=last_segment.created_at,
                        ownership_complete=last_segment.ownership_complete,
                        shadow_capture_state=(
                            _ProviderShadowCaptureState.UNAVAILABLE
                            if terminal_preserved
                            else last_segment.shadow_capture_state
                        ),
                        shadow_completed_window_sample_count=(
                            0
                            if terminal_preserved
                            else last_segment.shadow_completed_window_sample_count
                        ),
                        last_progress_at=last_segment.last_progress_at,
                        deferred=True,
                        deferred_accepted=True,
                        start_sample_16k=boundary.end_sample_16k,
                        end_sample_16k=last_segment.end_sample_16k,
                        tentative=False,
                    )
                )
                self._speaker_rejection_prepare_diagnostics[
                    "provider_speaker_segment_sample_split_count"
                ] += 1
            survivors.extend(segments[last_covered_index + 1 :])
            for offset, segment in enumerate(survivors, start=1):
                segment.detector_candidate = DetectorCandidateKey(
                    self._detector_epoch,
                    expected_generation + offset,
                )
                segment.tentative = offset > 1
            if terminal_preserved and suffix_candidate is not None and survivors:
                self._mark_provider_micro_event_ambiguous(
                    survivors[0].detector_candidate
                )
            self._retire_provider_segment_expiry_task()
            self._provider_speaker_segments = deque(survivors)
            self._schedule_provider_segment_expiry()

            verdict = ProviderSpeakerBoundarySnapshot(
                detector_epoch=self._detector_epoch,
                candidate_generation=expected_generation,
                through_sequence_no=covered[-1].last_identity.sequence_no,
                shadow_generation=target_candidate.shadow_generation,
                merged_resume_count=merged_resume_count,
                successor_present=bool(survivors),
                evidence_complete=True,
                _owner=self._provider_boundary_snapshot_owner,
                boundary_exact=True,
            )
            self._provider_preseal_entries[expected_generation] = (
                _ProviderSpeakerPresealEntry(
                    verdict=verdict,
                    shadow_candidate=target_candidate,
                    reconciliation=receipt,
                )
            )
            self._speaker_rejection_prepare_diagnostics[
                "provider_preseal_verdict_stored_count"
            ] += 1
            self._provider_boundary_snapshots[expected_generation] = verdict
            self._speaker_rejection_prepare_diagnostics[
                "provider_speaker_segment_merged_resume_count"
            ] += merged_resume_count
            if target_was_deferred:
                self._speaker_rejection_prepare_diagnostics[
                    "provider_speaker_segment_activated_count"
                ] += 1
            return verdict

    async def seal_provider_candidate(
        self,
        turn_token: VoiceTurnToken | None = None,
        *,
        speaker_snapshot: ProviderSpeakerBoundarySnapshot | None = None,
        deadline: float | None = None,
    ) -> ProviderCandidateFence | None:
        """Seal local detector activity after a streaming Provider endpoint."""

        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            return None
        waited_snapshot_owner: object | None = None
        if (
            deadline is not None
            and type(speaker_snapshot) is ProviderSpeakerBoundarySnapshot
            and speaker_snapshot.boundary_exact
        ):
            # Do not retire a still-pending exact receipt merely because the
            # ordered endpoint reached seal first. Both lanes share the same
            # caller-owned absolute admission deadline; settlement failure or
            # timeout still falls through to the existing unknown downgrade.
            if (
                speaker_snapshot._owner is self._provider_boundary_snapshot_owner
                and speaker_snapshot.detector_epoch == self._detector_epoch
            ):
                waited_snapshot_owner = speaker_snapshot._owner
            await self.wait_provider_speaker_preseal(
                speaker_snapshot,
                deadline=float(deadline),
            )
        acquired = False
        try:
            if deadline is None:
                await self._lock.acquire()
            else:
                remaining = float(deadline) - time.monotonic()
                if remaining <= 0.0:
                    return None
                try:
                    await asyncio.wait_for(
                        self._lock.acquire(),
                        timeout=remaining,
                    )
                except TimeoutError:
                    return None
            acquired = True
            if deadline is not None and time.monotonic() >= float(deadline):
                return None
            if self._closed or self._semantic_adapter is not None:
                return None
            if waited_snapshot_owner is not None and (
                waited_snapshot_owner is not self._provider_boundary_snapshot_owner
                or speaker_snapshot is None
                or speaker_snapshot.detector_epoch != self._detector_epoch
            ):
                return None
            if turn_token is not None and type(turn_token) is not VoiceTurnToken:
                return None
            existing = self._provider_candidate_fence
            if existing is not None:
                sealed = self._sealed_provider_candidate_rejection
                if (
                    turn_token is not None
                    and sealed is not None
                    and sealed.turn_token != turn_token
                ):
                    # One physical endpoint cannot authorize two logical
                    # owners. Preserve Provider fence equality but revoke all
                    # optional suppression authority.
                    self._sealed_provider_candidate_rejection = None
                    self._sealed_provider_micro_event = None
                return existing
            candidate = DetectorCandidateKey(
                self._detector_epoch,
                self._candidate_generation,
            )
            entry = self._provider_preseal_entries.get(self._candidate_generation)
            entry_matches = bool(
                entry is not None
                and (
                    entry.verdict is speaker_snapshot
                    or (speaker_snapshot is None and not entry.verdict.boundary_exact)
                )
                and entry.verdict._owner is self._provider_boundary_snapshot_owner
                and entry.verdict.detector_epoch == self._detector_epoch
                and entry.verdict.candidate_generation == self._candidate_generation
            )
            standalone_unknown = bool(
                type(speaker_snapshot) is ProviderSpeakerBoundarySnapshot
                and not speaker_snapshot.boundary_exact
                and speaker_snapshot._owner is self._provider_boundary_snapshot_owner
                and speaker_snapshot.detector_epoch == self._detector_epoch
                and speaker_snapshot.candidate_generation == self._candidate_generation
            )
            reconciliation_status = (
                self._provider_preseal_reconciliation_status_locked(entry)
                if entry_matches and entry is not None
                else "stale"
            )
            snapshot_exact = bool(
                entry_matches
                and entry is not None
                and entry.verdict.boundary_exact
                and entry.verdict.evidence_complete
                and reconciliation_status == "applied"
                and 0 <= entry.verdict.through_sequence_no <= self._sequence_no
                and entry.shadow_candidate is not None
                and entry.shadow_candidate.detector_epoch == self._detector_epoch
            )
            if snapshot_exact:
                assert entry is not None
                speaker_snapshot = entry.verdict
                self._provider_boundary_snapshots.pop(
                    self._candidate_generation,
                    None,
                )
                boundary_merged_resume_count = speaker_snapshot.merged_resume_count
                boundary_successor_present = speaker_snapshot.successor_present
                shadow_candidate = entry.shadow_candidate
                fence_through_sequence_no = speaker_snapshot.through_sequence_no
            else:
                if entry is not None:
                    self._downgrade_provider_preseal_entry_locked(entry)
                elif self._provider_segment_ordered_mode and not standalone_unknown:
                    unknown = self._reserve_provider_unknown_preseal_locked()
                    entry = self._provider_preseal_entries.get(
                        unknown.candidate_generation
                    )
                boundary_merged_resume_count = 0
                boundary_successor_present = False
                shadow_candidate = None
                fence_through_sequence_no = self._sequence_no
            consumed_entry = self._provider_preseal_entries.pop(
                self._candidate_generation,
                None,
            )
            if consumed_entry is not None:
                self._speaker_rejection_prepare_diagnostics[
                    "provider_preseal_verdict_consumed_count"
                ] += 1
                self._provider_boundary_snapshots.pop(
                    self._candidate_generation,
                    None,
                )
            fence = ProviderCandidateFence(
                detector_epoch=self._detector_epoch,
                candidate_generation=self._candidate_generation,
                through_sequence_no=fence_through_sequence_no,
                boundary_exact=snapshot_exact,
                merged_resume_count=boundary_merged_resume_count,
                successor_present=boundary_successor_present,
            )
            self._provider_candidate_fence = fence
            self._provider_speaker_sealed_through_sequence_no = max(
                self._provider_speaker_sealed_through_sequence_no or 0,
                fence.through_sequence_no,
            )
            bound = self._bound_turns.get(candidate)
            if turn_token is not None:
                if bound is None:
                    bound = BoundDetectorTurn(candidate, turn_token)
                    self._bound_turns[candidate] = bound
                elif bound.turn_token != turn_token:
                    bound = None

            if self._provider_segment_ordered_mode:
                if not snapshot_exact:
                    self._mark_provider_micro_event_ambiguous(candidate)
            else:
                shadow_candidate = (
                    self._speaker_shadow_candidate
                    if self._provider_legacy_segment_evidence_complete
                    else None
                )

            if shadow_candidate is None:
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_seal_snapshot_missing_shadow_count"
                ] += 1
                sealed_rejection = None
            elif (
                shadow_candidate.scope != "provider_candidate"
                or shadow_candidate.detector_epoch != self._detector_epoch
            ):
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_seal_snapshot_invalid_shadow_count"
                ] += 1
                sealed_rejection = None
            elif bound is None:
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_seal_snapshot_unbound_count"
                ] += 1
                sealed_rejection = None
            else:
                sealed_rejection = _SealedProviderCandidateRejection(
                    provider_fence=fence,
                    candidate=candidate,
                    shadow_candidate=shadow_candidate,
                    turn_token=bound.turn_token,
                    rejection_ready=bool(
                        consumed_entry is not None and consumed_entry.rejection_ready
                    ),
                )
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_seal_snapshot_created_count"
                ] += 1
                if snapshot_exact:
                    self._speaker_rejection_prepare_diagnostics[
                        "provider_speaker_segment_exact_snapshot_count"
                    ] += 1
            self._sealed_provider_candidate_rejection = sealed_rejection
            self._seal_provider_micro_event(candidate, fence)
            self._provider_micro_event_ambiguous_candidates.discard(candidate)
            self._provider_discarded_through_sequence_no = None
            self._candidate_generation += 1
            self._candidate_open = False
            self._speech_active = False
            self._policy_event_candidate = None
            self._throttle_policy.reset_candidate_activity()
            if not self._provider_segment_ordered_mode:
                self._finish_speaker_shadow_candidate(
                    expected_scope="provider_candidate"
                )
                self._provider_legacy_segment_evidence_complete = True
            elif self._speaker_shadow_suppressed_candidate == (
                self._detector_epoch,
                "provider_candidate",
            ):
                self._speaker_shadow_suppressed_candidate = None
            return fence
        finally:
            if acquired:
                self._lock.release()

    def ready_provider_speaker_rejection(
        self,
        provider_fence: ProviderCandidateFence,
    ) -> SpeakerShadowCandidateKey | None:
        """Return the exact sealed candidate whose pre-seal reject is ready."""

        sealed = self._sealed_provider_candidate_rejection
        if (
            self._closed
            or self._semantic_adapter is not None
            or type(provider_fence) is not ProviderCandidateFence
            or not provider_fence.boundary_exact
            or provider_fence != self._provider_candidate_fence
            or sealed is None
            or not sealed.rejection_ready
            or sealed.provider_fence != provider_fence
            or sealed.candidate
            != DetectorCandidateKey(
                provider_fence.detector_epoch,
                provider_fence.candidate_generation,
            )
            or sealed.candidate.detector_epoch != self._detector_epoch
            or sealed.shadow_candidate.scope != "provider_candidate"
            or sealed.shadow_candidate.detector_epoch != self._detector_epoch
        ):
            return None
        bound = self._bound_turns.get(sealed.candidate)
        if bound is None or bound.turn_token != sealed.turn_token:
            return None
        return sealed.shadow_candidate

    def pending_provider_speaker_candidate(
        self,
        provider_fence: ProviderCandidateFence,
    ) -> SpeakerShadowCandidateKey | None:
        """Return one exact sealed candidate whose first decision is pending.

        This is a non-authoritative, aggregate-free status query. The caller
        still has to acquire a revocable rejection lease and revalidate every
        ASR fence before installing a Provider-final gate.
        """

        self._speaker_rejection_prepare_diagnostics[
            "rejection_provisional_query_count"
        ] += 1
        sealed = self._sealed_provider_candidate_rejection
        shadow = self._speaker_shadow
        if (
            self._closed
            or self._semantic_adapter is not None
            or type(provider_fence) is not ProviderCandidateFence
            or provider_fence != self._provider_candidate_fence
            or sealed is None
            or sealed.provider_fence != provider_fence
            or sealed.shadow_candidate.scope != "provider_candidate"
            or not isinstance(shadow, SpeakerShadowDecisionStatus)
        ):
            self._speaker_rejection_prepare_diagnostics[
                "rejection_provisional_stale_count"
            ] += 1
            return None
        try:
            pending = shadow.requires_provisional_decision(sealed.shadow_candidate)
        except Exception:
            pending = False
        if not pending:
            self._speaker_rejection_prepare_diagnostics[
                "rejection_provisional_stale_count"
            ] += 1
            return None
        self._speaker_rejection_prepare_diagnostics[
            "rejection_provisional_pending_count"
        ] += 1
        return sealed.shadow_candidate

    async def discard_provider_successor(
        self,
        fence: ProviderCandidateFence,
    ) -> bool:
        """Discard only successor activity while preserving the sealed fence."""

        async with self._lock:
            if (
                self._closed
                or self._semantic_adapter is not None
                or fence != self._provider_candidate_fence
                or fence.detector_epoch != self._detector_epoch
            ):
                return False
            await asyncio.to_thread(self._gate.reset)
            self._provider_discarded_through_sequence_no = self._sequence_no
            self._provider_speaker_sealed_through_sequence_no = max(
                self._provider_speaker_sealed_through_sequence_no or 0,
                self._sequence_no,
            )
            self._candidate_generation += 1
            self._provider_micro_event_aggregate = None
            self._clear_provider_segment_state(
                preserve_ordered_mode=True,
                preserve_last_sequence=True,
                preserve_audio_cursor=True,
            )
            self._candidate_open = False
            self._speech_active = False
            self._policy_event_candidate = None
            self._throttle_policy.reset_candidate_activity()
            self._finish_speaker_shadow_candidate(expected_scope="provider_candidate")
            if self._speaker_shadow_suppressed_candidate == (
                self._detector_epoch,
                "provider_candidate",
            ):
                self._speaker_shadow_suppressed_candidate = None
        await self._drain_provider_segment_expiry_tasks()
        return True

    async def complete_provider_candidate(
        self,
        fence: ProviderCandidateFence,
    ) -> bool | None:
        """Consume one Provider fence and report whether successor PCM exists."""

        async with self._lock:
            if (
                self._closed
                or fence != self._provider_candidate_fence
                or fence.detector_epoch != self._detector_epoch
            ):
                return None
            self._provider_candidate_fence = None
            if self._sealed_provider_candidate_rejection is not None:
                self._speaker_rejection_prepare_diagnostics[
                    "rejection_complete_cleared_snapshot_count"
                ] += 1
            self._sealed_provider_candidate_rejection = None
            self._sealed_provider_micro_event = None
            successor_floor = max(
                fence.through_sequence_no,
                self._provider_discarded_through_sequence_no
                or fence.through_sequence_no,
            )
            exact_successor_retained = bool(
                fence.successor_present
                and self._provider_discarded_through_sequence_no is None
            )
            self._provider_discarded_through_sequence_no = None
            successor_present = bool(
                exact_successor_retained or self._sequence_no > successor_floor
            )
            successor_confirmed = successor_present and self._speech_active
            if not successor_confirmed:
                self._provider_micro_event_aggregate = None
                self._candidate_open = False
                self._speech_active = False
                self._policy_event_candidate = None
                self._throttle_policy.reset_candidate_activity()
            return successor_present

    async def submit_audio(
        self,
        pcm16: bytes,
        *,
        ingress_token: VoiceIngressToken,
        sample_rate_hz: int,
        speech_probability: float | None,
        rnnoise_available: bool,
        rnnoise_evidence: RnnoiseEvidence | None = None,
        allow_baseline_update: bool = False,
    ) -> DetectorSubmitResult:
        """Validate and enqueue one frame without waiting for detector inference."""

        if not isinstance(pcm16, bytes) or len(pcm16) % 2:
            raise ValueError("DetectorRuntime requires complete PCM16 bytes")
        if sample_rate_hz <= 0:
            raise ValueError("DetectorRuntime sample rate must be positive")
        if speech_probability is not None and not 0.0 <= speech_probability <= 1.0:
            raise ValueError("speech_probability must be within [0, 1]")
        adapter = self._semantic_adapter
        if self._closed:
            return DetectorSubmitResult(
                DetectorSubmitStatus.CLOSED,
                False,
                False,
                None,
            )
        overflow_reset_task = self._overflow_reset_task
        if overflow_reset_task is not None and not overflow_reset_task.done():
            return DetectorSubmitResult(
                DetectorSubmitStatus.BACKPRESSURE,
                adapter.throttle_available if adapter is not None else False,
                True,
                None,
            )
        if self._smart_turn_readiness is SmartTurnReadiness.FAILED:
            return DetectorSubmitResult(
                DetectorSubmitStatus.FAILED,
                adapter.throttle_available if adapter is not None else False,
                False,
                None,
            )
        if adapter is None or adapter.failed:
            return DetectorSubmitResult(
                DetectorSubmitStatus.FAILED,
                False,
                False,
                None,
            )
        if not pcm16:
            return DetectorSubmitResult(
                DetectorSubmitStatus.SKIPPED_QUIET,
                adapter.throttle_available,
                True,
                None,
            )
        if self._ingress_token is None:
            self._ingress_token = ingress_token
        elif self._ingress_token != ingress_token:
            return DetectorSubmitResult(
                DetectorSubmitStatus.FAILED,
                adapter.throttle_available,
                True,
                None,
            )
        evidence = rnnoise_evidence or RnnoiseEvidence.from_legacy_probability(
            speech_probability,
            available=rnnoise_available,
        )
        deny_rearm_token = self._active_deny_rearm_token()
        throttle = self._throttle_policy.decide(
            evidence,
            candidate_open=bool(self._candidate_open or deny_rearm_token is not None),
            allow_baseline_update=allow_baseline_update,
        )
        if throttle.action is ThrottleAction.SKIP_IDLE_PCM:
            return DetectorSubmitResult(
                DetectorSubmitStatus.SKIPPED_QUIET,
                adapter.throttle_available,
                True,
                None,
                throttle.action,
            )
        if deny_rearm_token is None:
            self._candidate_open = True
        await self._ensure_semantic_started(adapter)
        next_sequence = self._sequence_no + 1
        identity = DetectorIngressIdentity(
            ingress_token=ingress_token,
            detector_epoch=self._detector_epoch,
            sequence_no=next_sequence,
        )
        try:
            await adapter.push_audio(
                generation=self._semantic_generation,
                buffer_epoch=0,
                utterance_id=self._semantic_turn_id,
                pcm16=pcm16,
                sample_rate_hz=sample_rate_hz,
                detector_identity=identity,
                deny_rearm_boundary=deny_rearm_token is not None,
            )
        except asyncio.QueueFull:
            self._detector_epoch += 1
            self._reset_speaker_shadow_identity()
            speaker_shadow = self._speaker_shadow
            self._candidate_generation = 0
            self._candidate_open = False
            self._policy_event_candidate = None
            self._throttle_policy.reset_candidate_activity()
            self._ingress_token = None
            self._bound_turns.clear()
            self._deferred_completion_identity_advanced = False
            self._deferred_completions.clear()
            self._completion_fences.clear()
            self._provider_candidate_fence = None
            self._sealed_provider_candidate_rejection = None
            self._provider_micro_event_aggregate = None
            self._sealed_provider_micro_event = None
            self._clear_provider_segment_state()
            self._provider_speaker_sealed_through_sequence_no = None
            self._provider_discarded_through_sequence_no = None
            self._defer_turn_complete = False
            self._deferred_turn_complete = False
            self._semantic_generation += 1
            self._semantic_turn_id += 1
            overflow_reset_task = asyncio.create_task(
                self._reset_after_overflow(
                    adapter,
                    self._semantic_generation,
                    self._semantic_turn_id,
                    speaker_shadow,
                ),
                name="detector-runtime-overflow-reset",
            )
            self._overflow_reset_task = overflow_reset_task
            return DetectorSubmitResult(
                DetectorSubmitStatus.BACKPRESSURE,
                adapter.throttle_available,
                True,
                None,
            )
        self._sequence_no = next_sequence
        candidate = DetectorCandidateKey(
            identity.detector_epoch,
            self._candidate_generation,
        )
        control_event_emitted = False
        if (
            deny_rearm_token is None
            and self._on_event is not None
            and self._policy_event_candidate != candidate
            and throttle.action in {ThrottleAction.PREWARM, ThrottleAction.PROCESS_PCM}
        ):
            self._policy_event_candidate = candidate
            await self._on_event(
                DetectorPrewarmEvent(
                    ingress=identity,
                    candidate=candidate,
                    kind=(
                        "continuous"
                        if throttle.action is ThrottleAction.PROCESS_PCM
                        else "prewarm"
                    ),
                )
            )
            control_event_emitted = True
        return DetectorSubmitResult(
            DetectorSubmitStatus.ACCEPTED,
            adapter.throttle_available,
            True,
            identity,
            throttle.action,
            candidate,
            control_event_emitted,
        )

    async def _reset_after_overflow(
        self,
        adapter: _VoiceTurnAdapter,
        generation: int,
        utterance_id: int,
        speaker_shadow: SpeakerShadowObserver | None,
    ) -> None:
        failed = False
        try:
            await adapter.reset(
                generation=generation,
                buffer_epoch=0,
                utterance_id=utterance_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
        finally:
            try:
                await self._reset_speaker_shadow(speaker_shadow)
            finally:
                try:
                    await self._drain_provider_segment_expiry_tasks()
                finally:
                    async with self._lock:
                        if self._overflow_reset_task is asyncio.current_task():
                            self._overflow_reset_task = None
                        if failed and not self._closed:
                            self._smart_turn_readiness = SmartTurnReadiness.FAILED

    async def reset(self) -> None:
        overflow_reset_task = self._overflow_reset_task
        if (
            overflow_reset_task is not None
            and overflow_reset_task is not asyncio.current_task()
        ):
            await asyncio.gather(overflow_reset_task, return_exceptions=True)
        adapter: _VoiceTurnAdapter | None = None
        semantic_identity: tuple[int, int, int] | None = None
        speaker_shadow: SpeakerShadowObserver | None = None
        try:
            async with self._lock:
                if self._closed:
                    return
                self._detector_epoch += 1
                self._reset_speaker_shadow_identity()
                speaker_shadow = self._speaker_shadow
                self._candidate_generation = 0
                self._sequence_no = 0
                self._ingress_token = None
                self._candidate_open = False
                self._policy_event_candidate = None
                self._throttle_policy.reset_candidate_activity()
                self._bound_turns.clear()
                self._speaker_candidate_turn_bindings = {}
                self._deferred_completions.clear()
                self._completion_fences.clear()
                self._provider_candidate_fence = None
                self._sealed_provider_candidate_rejection = None
                self._provider_micro_event_aggregate = None
                self._sealed_provider_micro_event = None
                self._clear_provider_segment_state()
                self._provider_speaker_sealed_through_sequence_no = None
                self._provider_discarded_through_sequence_no = None
                self._speech_active = False
                self._prepare_token = None
                self._prepare_epoch = None
                self._smart_turn_generation = None
                self._smart_turn_lease_ids.clear()
                self._smart_turn_readiness = SmartTurnReadiness.UNLOADED
                if self._semantic_adapter is not None and self._semantic_started:
                    if self._smart_turn_token is not None:
                        self._smart_turn_token = None
                        self._semantic_adapter.unpin_smart_turn()
                    self._defer_turn_complete = False
                    self._deferred_turn_complete = False
                    self._deferred_completion_identity_advanced = False
                    self._semantic_generation += 1
                    self._semantic_turn_id += 1
                    adapter = self._semantic_adapter
                    semantic_identity = (
                        self._semantic_generation,
                        0,
                        self._semantic_turn_id,
                    )
                else:
                    # feed() runs gate.feed in a thread while holding self._lock;
                    # the gate counters are unlocked, so the reset must run under
                    # the same lock to avoid interleaving with an in-flight feed.
                    await asyncio.to_thread(self._gate.reset)
            if adapter is not None and semantic_identity is not None:
                await adapter.reset(
                    generation=semantic_identity[0],
                    buffer_epoch=semantic_identity[1],
                    utterance_id=semantic_identity[2],
                )
        finally:
            try:
                await self._reset_speaker_shadow(speaker_shadow)
            finally:
                await self._drain_provider_segment_expiry_tasks()

    async def replace_speaker_verifier(
        self,
        new_shadow: SpeakerShadowObserver | None,
        *,
        owner_generation: str | None,
        operation=None,
    ) -> None:
        """Atomically replace speaker observation for the next candidate.

        An active candidate is deliberately left unobserved after the swap. Its
        old observations are invalidated by generation, and the replacement is
        admitted only after the authoritative provider/SmartTurn boundary.
        Calling this method transfers ownership of ``new_shadow`` to Detector,
        including when cancellation wins before the detector lock is acquired.
        Closing the detached observer happens outside the detector lock and is
        bounded so verifier cleanup cannot stall endpointing or ASR.
        """

        from ..speaker_verifier_contracts import (
            SpeakerVerifierInstallOutcome as Outcome,
            SpeakerVerifierOwnershipState as Ownership,
        )

        if operation is not None:
            operation.ownership_state = Ownership.DETECTOR

        async def bounded_close(shadow: SpeakerShadowObserver) -> None:
            if operation is not None:
                # Typed ownership needs an actual close result; the legacy
                # helper intentionally swallows failures and is not a receipt.
                task = asyncio.create_task(shadow.close())
                operation.cleanup_tasks.append(task)
                done, _ = await asyncio.wait(
                    {task}, timeout=_SPEAKER_SHADOW_REPLACEMENT_CLOSE_SECONDS
                )
                operation.cleanup_pending = not done or task.cancelled()
                if done and not task.cancelled() and task.exception() is not None:
                    operation.cleanup_pending = True
                return
            try:
                await asyncio.wait_for(
                    self._close_speaker_shadow(shadow),
                    timeout=_SPEAKER_SHADOW_REPLACEMENT_CLOSE_SECONDS,
                )
            except TimeoutError:
                return

        detached_shadow: SpeakerShadowObserver | None = None
        rejected_shadow: SpeakerShadowObserver | None = None
        cleanup_task: asyncio.Task[None] | None = None
        installed = False
        try:
            async with self._lock:
                if self._closed:
                    if operation is not None:
                        operation.outcome = Outcome.STALE
                    if new_shadow is not self._speaker_shadow:
                        rejected_shadow = new_shadow
                elif new_shadow is self._speaker_shadow:
                    # Repeated installation retains the observer's original
                    # authority. ``None`` has no callbacks, so its empty-owner
                    # generation may advance without relabelling evidence.
                    if new_shadow is None:
                        self._speaker_owner_generation = owner_generation
                    installed = True
                    if operation is not None:
                        operation.outcome = Outcome.INSTALLED
                    return
                else:
                    self._sealed_provider_candidate_rejection = None
                    self._sealed_provider_micro_event = None
                    self._clear_provider_segment_state()
                    # The detached observer cannot publish authoritative
                    # terminal facts into the replacement verifier generation.
                    self._speaker_candidate_turn_bindings = {}
                    self._speaker_candidate_owner_generations = {}
                    self._mark_provider_micro_event_ambiguous(
                        DetectorCandidateKey(
                            self._detector_epoch,
                            self._candidate_generation,
                        )
                    )
                    suppressed = self._speaker_shadow_suppressed_candidate
                    if suppressed is not None and suppressed[0] != self._detector_epoch:
                        self._speaker_shadow_suppressed_candidate = None
                    candidate = self._speaker_shadow_candidate
                    if candidate is not None:
                        self._speaker_shadow_suppressed_candidate = (
                            self._detector_epoch,
                            candidate.scope,
                        )
                    elif (
                        self._candidate_open
                        and self._speaker_shadow_suppressed_candidate is None
                    ):
                        scope: SpeakerShadowScope = (
                            "smart_turn_turn"
                            if self._semantic_adapter is not None
                            else "provider_candidate"
                        )
                        self._speaker_shadow_suppressed_candidate = (
                            self._detector_epoch,
                            scope,
                        )
                    self._speaker_shadow_generation += 1
                    self._speaker_shadow_candidate = None
                    self._provider_speaker_evidence_generation += 1
                    self._provider_speaker_evidence_state = None
                    detached_shadow, self._speaker_shadow = (
                        self._speaker_shadow,
                        new_shadow,
                    )
                    self._speaker_owner_generation = owner_generation
                    installed = True
                    if operation is not None:
                        operation.outcome = Outcome.INSTALLED

            cleanup_shadow = (
                detached_shadow if detached_shadow is not None else rejected_shadow
            )
            if cleanup_shadow is None:
                return
            cleanup_task = asyncio.create_task(
                bounded_close(cleanup_shadow),
                name="detector-speaker-verifier-replacement-cleanup",
            )
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # Invocation transfers ``new_shadow`` ownership immediately. If
            # cancellation wins before installation, Detector must close it;
            # after installation the new observer remains authoritative while
            # only the detached old observer is cleaned up.
            if (
                cleanup_task is None
                and not installed
                and new_shadow is not None
                and new_shadow is not self._speaker_shadow
            ):
                cleanup_task = asyncio.create_task(
                    bounded_close(new_shadow),
                    name="detector-speaker-verifier-cancel-cleanup",
                )
            while cleanup_task is not None and not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            raise
        except Exception:
            if operation is not None and not installed and new_shadow is not None:
                # Acceptance preceded the first await. Ordinary pre-swap
                # exceptions have the same cleanup owner as cancellation.
                cleanup_task = asyncio.create_task(bounded_close(new_shadow))
                await asyncio.shield(cleanup_task)
            raise
        finally:
            await self._drain_provider_segment_expiry_tasks()

    async def release_deferred_turn(self) -> None:
        """Release a deferred SmartTurn completion after the prior final."""

        callback: Callable[[], Awaitable[None]] | None = None
        async with self._lock:
            if self._closed or self._semantic_adapter is None:
                return
            self._defer_turn_complete = False
            if self._deferred_turn_complete:
                self._deferred_turn_complete = False
                self._defer_turn_complete = True
                identity_advanced = self._deferred_completion_identity_advanced
                self._deferred_completion_identity_advanced = False
                if not identity_advanced:
                    self._semantic_generation += 1
                    self._semantic_turn_id += 1
                    await self._semantic_adapter.reset(
                        generation=self._semantic_generation,
                        buffer_epoch=0,
                        utterance_id=self._semantic_turn_id,
                    )
                callback = self._on_turn_complete
        if callback is not None:
            # 不持有 detector lock 调用 Core，避免 Core 清理时反向 reset 死锁。
            await callback()

    async def close(self) -> None:
        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._close_impl(),
                name="detector-runtime-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        adapter: _VoiceTurnAdapter | None = None
        vad = None
        prepare_task: asyncio.Task[bool] | None = None
        overflow_reset_task: asyncio.Task[None] | None = None
        speaker_shadow: SpeakerShadowObserver | None = None
        try:
            async with self._lock:
                if self._closed:
                    return
                self._closed = True
                self._detector_epoch += 1
                self._reset_speaker_shadow_identity()
                speaker_shadow = self._speaker_shadow
                self._candidate_generation = 0
                self._candidate_open = False
                self._policy_event_candidate = None
                self._throttle_policy.reset_candidate_activity()
                self._ingress_token = None
                self._bound_turns.clear()
                self._deferred_completions.clear()
                self._completion_fences.clear()
                self._provider_candidate_fence = None
                self._sealed_provider_candidate_rejection = None
                self._provider_micro_event_aggregate = None
                self._sealed_provider_micro_event = None
                self._clear_provider_segment_state()
                self._provider_speaker_sealed_through_sequence_no = None
                self._provider_discarded_through_sequence_no = None
                watch_task, self._failure_watch_task = self._failure_watch_task, None
                if watch_task is not None:
                    watch_task.cancel()
                if self._semantic_adapter is not None:
                    overflow_reset_task = self._overflow_reset_task
                    self._smart_turn_token = None
                    self._smart_turn_generation = None
                    self._smart_turn_lease_ids.clear()
                    self._prepare_token = None
                    self._prepare_epoch = None
                    prepare_task, self._prepare_task = self._prepare_task, None
                    if prepare_task is not None:
                        prepare_task.cancel()
                    self._smart_turn_readiness = SmartTurnReadiness.UNLOADING
                    adapter = self._semantic_adapter
                else:
                    vad = self._vad
            if adapter is not None:
                if overflow_reset_task is not None:
                    await asyncio.gather(overflow_reset_task, return_exceptions=True)
                await adapter.close()
                if prepare_task is not None:
                    await asyncio.gather(prepare_task, return_exceptions=True)
                self._smart_turn_readiness = SmartTurnReadiness.UNLOADED
            else:
                await asyncio.to_thread(vad.close)
        finally:
            try:
                await self._close_speaker_shadow(speaker_shadow)
            finally:
                await self._drain_provider_segment_expiry_tasks()

    def _observe_smart_turn_speaker_shadow(
        self,
        pcm16: bytes,
        sample_rate_hz: int,
        detector_identity: DetectorIngressIdentity | None,
    ) -> None:
        if (
            detector_identity is None
            or detector_identity.detector_epoch != self._detector_epoch
            or detector_identity.ingress_token != self._ingress_token
            or detector_identity.sequence_no > self._sequence_no
        ):
            return
        candidate = self._speaker_shadow_candidate
        if candidate is None:
            candidate = self._open_speaker_shadow_candidate("smart_turn_turn")
        if candidate is None or candidate.scope != "smart_turn_turn":
            return
        self._submit_speaker_shadow(
            pcm16,
            sample_rate_hz=sample_rate_hz,
            candidate=candidate,
        )

    def _finish_smart_turn_speaker_shadow(
        self,
        detector_identity: DetectorIngressIdentity | None,
    ) -> None:
        if (
            detector_identity is None
            or detector_identity.detector_epoch != self._detector_epoch
        ):
            return
        self._finish_speaker_shadow_candidate(expected_scope="smart_turn_turn")

    def _open_speaker_shadow_candidate(
        self,
        scope: Literal["provider_candidate", "smart_turn_turn"],
    ) -> SpeakerShadowCandidateKey | None:
        suppressed = self._speaker_shadow_suppressed_candidate
        if suppressed is not None:
            suppressed_epoch, suppressed_scope = suppressed
            if suppressed_epoch != self._detector_epoch or suppressed_scope != scope:
                self._speaker_shadow_suppressed_candidate = None
            else:
                return None
        shadow = self._speaker_shadow
        if shadow is None:
            return None
        try:
            if not shadow.enabled:
                return None
        except Exception:
            return None
        candidate = SpeakerShadowCandidateKey(
            detector_epoch=self._detector_epoch,
            shadow_generation=self._speaker_shadow_generation,
            scope=scope,
        )
        self._speaker_candidate_owner_generations[candidate] = (
            self._speaker_owner_generation
        )
        self._speaker_shadow_candidate = candidate
        self._publish_speaker_candidate_binding(
            candidate,
            DetectorCandidateKey(
                self._detector_epoch,
                self._candidate_generation,
            ),
        )
        return candidate

    def _submit_speaker_shadow(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> None:
        shadow = self._speaker_shadow
        if shadow is None:
            return
        try:
            shadow.submit(
                pcm16,
                sample_rate_hz=sample_rate_hz,
                candidate=candidate,
            )
        except Exception:
            return

    def _finish_speaker_shadow_candidate(
        self,
        *,
        expected_scope: Literal["provider_candidate", "smart_turn_turn"],
    ) -> None:
        candidate = self._speaker_shadow_candidate
        self._speaker_shadow_candidate = None
        self._speaker_shadow_generation += 1
        suppressed = self._speaker_shadow_suppressed_candidate
        if suppressed is not None and (
            suppressed[0] != self._detector_epoch or suppressed[1] == expected_scope
        ):
            self._speaker_shadow_suppressed_candidate = None
        if candidate is None or candidate.scope != expected_scope:
            return
        shadow = self._speaker_shadow
        if shadow is None:
            return
        try:
            shadow.finish_candidate(candidate)
        except Exception:
            return

    def _reset_speaker_shadow_identity(self) -> None:
        self._speaker_shadow_generation += 1
        self._speaker_shadow_candidate = None
        self._provider_speaker_evidence_generation += 1
        self._provider_speaker_evidence_state = None
        # The paired shadow reset/close revokes its staged PCM reservation.
        # Drop Detector's receipts at the same synchronous identity fence so
        # an old caller cannot retain or later consume authority from the
        # retired epoch while that asynchronous cleanup is still running.
        self._provider_exact_interval_records.clear()
        self._speaker_candidate_turn_bindings = {}
        self._speaker_candidate_owner_generations = {}

    @staticmethod
    async def _reset_speaker_shadow(
        shadow: SpeakerShadowObserver | None,
    ) -> None:
        if shadow is None:
            return
        try:
            await shadow.reset()
        except Exception:
            return

    @staticmethod
    async def _close_speaker_shadow(
        shadow: SpeakerShadowObserver | None,
    ) -> None:
        if shadow is None:
            return
        try:
            await shadow.close()
        except Exception:
            return

    async def _watch_semantic_failure(self, adapter: _VoiceTurnAdapter) -> None:
        try:
            async with self._lock:
                if self._closed or adapter is not self._semantic_adapter:
                    return
                watched_epoch = self._detector_epoch
                watched_generation = self._semantic_generation
            failure = await adapter.wait_failure()
            speaker_shadow: SpeakerShadowObserver | None = None
            failure_epoch: int | None = None
            async with self._lock:
                if (
                    self._closed
                    or adapter is not self._semantic_adapter
                    or self._detector_epoch != watched_epoch
                    or self._semantic_generation != watched_generation
                ):
                    return
                if getattr(failure, "stage", None) in {"vad_load", "vad_feed"}:
                    self._available = False
                    return
                self._detector_epoch += 1
                self._reset_speaker_shadow_identity()
                self._candidate_generation = 0
                self._candidate_open = False
                self._policy_event_candidate = None
                self._throttle_policy.reset_candidate_activity()
                self._ingress_token = None
                self._bound_turns.clear()
                self._deferred_completions.clear()
                self._completion_fences.clear()
                self._provider_candidate_fence = None
                self._sealed_provider_candidate_rejection = None
                self._provider_micro_event_aggregate = None
                self._sealed_provider_micro_event = None
                self._clear_provider_segment_state()
                self._provider_speaker_sealed_through_sequence_no = None
                self._provider_discarded_through_sequence_no = None
                self._smart_turn_readiness = SmartTurnReadiness.FAILED
                speaker_shadow = self._speaker_shadow
                failure_epoch = self._detector_epoch
            await self._reset_speaker_shadow(speaker_shadow)
            await self._drain_provider_segment_expiry_tasks()
            async with self._lock:
                if (
                    self._closed
                    or adapter is not self._semantic_adapter
                    or self._detector_epoch != failure_epoch
                ):
                    return
                callback = self._on_endpointing_failure
            if callback is not None:
                await callback()
        except asyncio.CancelledError:
            return
