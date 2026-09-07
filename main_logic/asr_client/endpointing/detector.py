"""ASR detector contracts, queues, and Core-facing event dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Literal, TypeAlias, TypeVar

from main_logic.voice_turn.contracts import SpeechActivityEvent, TurnEvaluation

from ..lifecycle import VoiceIngressToken, VoiceTurnToken
from .throttle_policy import ThrottleAction


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DetectorIngressIdentity:
    """Identity assigned when a normalized PCM frame enters the detector."""

    ingress_token: VoiceIngressToken
    detector_epoch: int
    sequence_no: int


@dataclass(frozen=True, slots=True)
class DetectorCandidateKey:
    """Detector-worker-owned candidate identity within one external epoch."""

    detector_epoch: int
    candidate_generation: int


@dataclass(frozen=True, slots=True)
class SmartTurnCompletionFence:
    """Immutable detector boundary accepted with one SmartTurn completion."""

    detector_epoch: int
    candidate_generation: int
    through_sequence_no: int
    semantic_generation: int
    semantic_turn_id: int
    successor_candidate_generation: int
    successor_present: bool

    @property
    def candidate(self) -> DetectorCandidateKey:
        return DetectorCandidateKey(
            self.detector_epoch,
            self.candidate_generation,
        )


@dataclass(frozen=True, slots=True)
class ProviderCandidateFence:
    """Immutable detector boundary sealed by one Provider endpoint."""

    detector_epoch: int
    candidate_generation: int
    through_sequence_no: int
    boundary_exact: bool = False
    merged_resume_count: int = 0
    successor_present: bool = False


@dataclass(frozen=True, slots=True)
class ProviderSpeakerBoundarySnapshot:
    """Opaque PCM-free authority captured at one exact Provider boundary.

    The owner nonce prevents snapshots from being forged or replayed across
    detector instances.  Runtime code may retain this value by Provider key,
    but only the issuing detector can interpret or consume it.
    """

    detector_epoch: int
    candidate_generation: int
    through_sequence_no: int
    shadow_generation: int
    merged_resume_count: int
    successor_present: bool
    evidence_complete: bool
    _owner: object = field(repr=False)
    boundary_exact: bool = True


# The richer name describes how this opaque value is used: an exact snapshot
# or an unknown tombstone is produced before the ordered Provider endpoint and
# consumed exactly once while sealing that endpoint.  Keep the original name
# as the same runtime type so existing private callback shims remain compatible
# while the keyed runtime migrates to the pre-seal terminology.
ProviderSpeakerPresealVerdict = ProviderSpeakerBoundarySnapshot


@dataclass(frozen=True, slots=True)
class BoundDetectorTurn:
    """One-time binding between a detector candidate and a logical ASR turn."""

    candidate: DetectorCandidateKey
    turn_token: VoiceTurnToken


@dataclass(frozen=True, slots=True)
class DetectorActivityEvent:
    ingress: DetectorIngressIdentity
    candidate: DetectorCandidateKey
    activity: SpeechActivityEvent


@dataclass(frozen=True, slots=True)
class DetectorTurnEvent:
    ingress: DetectorIngressIdentity
    bound_turn: BoundDetectorTurn
    kind: Literal["complete", "inference_failed"]
    evaluation: TurnEvaluation | None = None


@dataclass(frozen=True, slots=True)
class DetectorPrewarmEvent:
    ingress: DetectorIngressIdentity
    candidate: DetectorCandidateKey
    kind: Literal["prewarm", "continuous"]


@dataclass(frozen=True, slots=True)
class DetectorTransportPrewarmEvent:
    """Request transport resources without binding or opening a logical turn."""

    ingress: DetectorIngressIdentity


@dataclass(frozen=True, slots=True)
class DetectorRuntimeEvent:
    ingress: DetectorIngressIdentity
    candidate: DetectorCandidateKey | None
    kind: Literal[
        "prepare_failed",
        "throttle_unavailable",
        "audio_backpressure",
        "control_lane_failed",
    ]


DetectorEvent: TypeAlias = (
    DetectorActivityEvent
    | DetectorTurnEvent
    | DetectorPrewarmEvent
    | DetectorTransportPrewarmEvent
    | DetectorRuntimeEvent
)


@dataclass(frozen=True, slots=True)
class DetectorAudioItem:
    identity: DetectorIngressIdentity
    pcm16: bytes
    duration_us: int

    @classmethod
    def from_pcm16(
        cls,
        pcm16: bytes,
        *,
        identity: DetectorIngressIdentity,
        sample_rate_hz: int,
    ) -> "DetectorAudioItem":
        if not isinstance(pcm16, bytes) or len(pcm16) % 2:
            raise ValueError("DETECTOR_INVALID_PCM: complete PCM16 bytes required")
        if not pcm16:
            raise ValueError("DETECTOR_INVALID_PCM: empty audio item")
        if sample_rate_hz <= 0:
            raise ValueError("DETECTOR_INVALID_SAMPLE_RATE")
        samples = len(pcm16) // 2
        duration_us = (samples * 1_000_000 + sample_rate_hz - 1) // sample_rate_hz
        return cls(identity=identity, pcm16=pcm16, duration_us=duration_us)


class DetectorSubmitStatus(Enum):
    ACCEPTED = "accepted"
    SKIPPED_QUIET = "skipped_quiet"
    BACKPRESSURE = "backpressure"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DetectorSubmitResult:
    status: DetectorSubmitStatus
    throttle_available: bool
    endpointing_available: bool
    identity: DetectorIngressIdentity | None
    throttle_action: ThrottleAction | None = None
    candidate: DetectorCandidateKey | None = None
    control_event_emitted: bool = False


_ControlItem = TypeVar("_ControlItem")
_AudioItem = TypeVar("_AudioItem")


@dataclass(frozen=True, slots=True)
class _QueuedAudio(Generic[_AudioItem]):
    value: _AudioItem
    duration_us: int


class DetectorDurationQueue(Generic[_AudioItem, _ControlItem]):
    """Preserve enqueue order while reserving capacity for control work.

    Audio is bounded by duration and frame count. Control items share ordering
    with audio but do not consume either audio budget, so an overflow result or
    invalidation barrier can always be delivered.
    """

    def __init__(self, *, capacity_us: int = 1_000_000, max_frames: int = 128) -> None:
        if capacity_us <= 0 or max_frames <= 0:
            raise ValueError("detector queue limits must be positive")
        self.capacity_us = capacity_us
        self.max_frames = max_frames
        self._audio_duration_us = 0
        self._audio_frames = 0
        self._items: deque[_QueuedAudio[_AudioItem] | _ControlItem] = deque()
        self._available = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._unfinished_tasks = 0

    @property
    def audio_duration_us(self) -> int:
        return self._audio_duration_us

    @property
    def audio_frames(self) -> int:
        return self._audio_frames

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def can_accept_audio(self, duration_us: int) -> bool:
        return bool(
            self._audio_frames < self.max_frames
            and self._audio_duration_us + duration_us <= self.capacity_us
        )

    def put_audio_nowait(
        self,
        item: _AudioItem,
        *,
        duration_us: int | None = None,
    ) -> None:
        if duration_us is None:
            if not isinstance(item, DetectorAudioItem):
                raise TypeError("duration_us is required for custom audio items")
            duration_us = item.duration_us
        if duration_us <= 0:
            raise ValueError("audio duration must be positive")
        if not self.can_accept_audio(duration_us):
            raise asyncio.QueueFull
        self._items.append(_QueuedAudio(item, duration_us))
        self._audio_frames += 1
        self._audio_duration_us += duration_us
        self._unfinished_tasks += 1
        self._idle.clear()
        self._available.set()

    def put_control_nowait(self, item: _ControlItem, *, priority: bool = False) -> None:
        if isinstance(item, (DetectorAudioItem, _QueuedAudio)):
            raise TypeError("audio must use put_audio_nowait")
        if priority:
            self._items.appendleft(item)
        else:
            self._items.append(item)
        self._unfinished_tasks += 1
        self._idle.clear()
        self._available.set()

    async def get(self) -> _AudioItem | _ControlItem:
        while not self._items:
            self._available.clear()
            if self._items:
                break
            await self._available.wait()
        queued = self._items.popleft()
        if isinstance(queued, _QueuedAudio):
            self._audio_frames -= 1
            self._audio_duration_us -= queued.duration_us
            item: _AudioItem | _ControlItem = queued.value
        else:
            item = queued
        if not self._items:
            self._available.clear()
        return item

    def get_nowait(self) -> _AudioItem | _ControlItem:
        if not self._items:
            raise asyncio.QueueEmpty
        queued = self._items.popleft()
        if isinstance(queued, _QueuedAudio):
            self._audio_frames -= 1
            self._audio_duration_us -= queued.duration_us
            item: _AudioItem | _ControlItem = queued.value
        else:
            item = queued
        if not self._items:
            self._available.clear()
        return item

    def task_done(self) -> None:
        if self._unfinished_tasks <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished_tasks -= 1
        if self._unfinished_tasks == 0:
            self._idle.set()

    async def join(self) -> None:
        if self._unfinished_tasks:
            await self._idle.wait()

    def discard_audio(self) -> int:
        """Discard queued PCM while preserving control barriers and results."""

        kept: deque[_QueuedAudio[_AudioItem] | _ControlItem] = deque()
        discarded = 0
        for item in self._items:
            if isinstance(item, _QueuedAudio):
                discarded += 1
            else:
                kept.append(item)
        self._items = kept
        self._audio_frames = 0
        self._audio_duration_us = 0
        self._unfinished_tasks -= discarded
        if self._unfinished_tasks == 0:
            self._idle.set()
        if self._items:
            self._available.set()
        else:
            self._available.clear()
        return discarded


@dataclass(frozen=True, slots=True)
class CoreDetectorEventEnvelope:
    event: DetectorEvent
    detector_ref: object
    lifecycle_ref: object
    session_epoch: int


class AsrDetectorDispatcher:
    def __init__(
        self,
        handler: Callable[[CoreDetectorEventEnvelope], Awaitable[None]],
        *,
        on_failure: Callable[
            [CoreDetectorEventEnvelope, BaseException], Awaitable[None]
        ],
        max_pending: int = 32,
    ) -> None:
        if max_pending <= 0:
            raise ValueError("detector event capacity must be positive")
        self._handler = handler
        self._on_failure = on_failure
        self._queue: asyncio.Queue[tuple[int, CoreDetectorEventEnvelope]] = (
            asyncio.Queue(maxsize=max_pending)
        )
        self._generation = 0
        self._failed = False
        self._worker: asyncio.Task[None] | None = None
        self._failure_tasks: set[asyncio.Task[None]] = set()

    def submit_nowait(self, envelope: CoreDetectorEventEnvelope) -> bool:
        if self._failed:
            return False
        self._ensure_worker()
        try:
            self._queue.put_nowait((self._generation, envelope))
        except asyncio.QueueFull:
            return False
        return True

    def invalidate_all(self) -> None:
        self._generation += 1
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()

    async def wait_idle(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        self.invalidate_all()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(), name="core-asr-detector-dispatcher"
            )

    async def _run(self) -> None:
        while True:
            generation, envelope = await self._queue.get()
            try:
                if generation == self._generation:
                    await self._handler(envelope)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if generation != self._generation:
                    continue
                self._failed = True
                self.invalidate_all()
                failure_task = asyncio.create_task(
                    self._deliver_failure(envelope, error),
                    name="core-asr-detector-fail-closed",
                )
                self._failure_tasks.add(failure_task)
                failure_task.add_done_callback(self._failure_tasks.discard)
            finally:
                self._queue.task_done()

    async def _deliver_failure(
        self,
        envelope: CoreDetectorEventEnvelope,
        error: BaseException,
    ) -> None:
        """Run the fail-closed callback outside the worker task it may tear down."""
        try:
            await self._on_failure(envelope, error)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ASR detector fail-closed callback failed")
