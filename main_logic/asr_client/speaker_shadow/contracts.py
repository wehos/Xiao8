"""Provider-neutral contracts for observation-only speaker scoring."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

SPEAKER_SHADOW_SAMPLE_RATE_HZ = 16_000
MAX_SPEAKER_SHADOW_CANDIDATE_AUDIO_MS = 4_000
MAX_SPEAKER_SHADOW_CANDIDATE_PCM_BYTES = (
    SPEAKER_SHADOW_SAMPLE_RATE_HZ * MAX_SPEAKER_SHADOW_CANDIDATE_AUDIO_MS // 1_000 * 2
)
# Lifecycle pre-roll may arrive as one payload, so its per-submit ceiling must
# match the candidate ceiling. The runtime still truncates to the configured
# candidate duration before retaining PCM.
MAX_SPEAKER_SHADOW_FRAME_AUDIO_MS = MAX_SPEAKER_SHADOW_CANDIDATE_AUDIO_MS
MAX_SPEAKER_SHADOW_FRAME_PCM_BYTES = MAX_SPEAKER_SHADOW_CANDIDATE_PCM_BYTES
MAX_SPEAKER_SHADOW_THRESHOLDS = 16
MAX_SPEAKER_SHADOW_CHECKPOINTS = 16
MAX_SPEAKER_SHADOW_QUEUE_CAPACITY = 512
MAX_SPEAKER_SHADOW_TERMINAL_QUEUE_CAPACITY = 1_024
MAX_SPEAKER_SHADOW_COMPLETION_QUEUE_CAPACITY = 1_024
MAX_SPEAKER_SHADOW_BUFFERED_CANDIDATES = 32
MAX_SPEAKER_SHADOW_FINALIZED_CANDIDATES = 4_096
# This independent global budget keeps aggregate retained PCM below 8 MiB even
# when individual queue items contain the full four-second pre-roll payload.
MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES = (
    8 * 1024 * 1024 - MAX_SPEAKER_SHADOW_CANDIDATE_PCM_BYTES
)
MAX_SPEAKER_SHADOW_BACKEND_LOAD_SECONDS = 30.0
MAX_SPEAKER_SHADOW_BACKEND_SCORE_SECONDS = 5.0
MAX_SPEAKER_SHADOW_BACKEND_CLOSE_SECONDS = 2.0
MAX_SPEAKER_SHADOW_PROCESS_TERMINATE_SECONDS = 2.0
MAX_SPEAKER_SHADOW_SHUTDOWN_GRACE_SECONDS = 2.0
MAX_SPEAKER_SHADOW_CALLBACK_TIMEOUT_SECONDS = 2.0

SpeakerShadowScope = Literal["provider_candidate", "smart_turn_turn"]
SpeakerShadowObservationKind = Literal[
    "checkpoint",
    "completion_confirmation",
]
SpeakerShadowTerminalReason = Literal[
    "scored",
    "insufficient",
    "dropped",
    "failed",
]
SpeakerShadowReconciliationStatus = Literal[
    "pending",
    "applied",
    "failed",
    "stale",
]
SpeakerShadowDeferredAnchorStatus = Literal[
    "pending",
    "applied",
    "failed",
    "stale",
]


class SpeakerShadowCaptureDisposition(StrEnum):
    """Whether ordered capture progressed, completed, or became unusable."""

    ACCEPTED = "accepted"
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"


class SpeakerShadowCaptureDecisionState(StrEnum):
    """Non-sensitive lifecycle of the candidate's score decision."""

    PENDING = "pending"
    SCORED = "scored"
    UNAVAILABLE = "unavailable"


class SpeakerShadowBackend(Protocol):
    """Blocking model adapter run exclusively outside the event loop."""

    def load(self) -> bool: ...

    def score(self, pcm16: bytes, sample_rate_hz: int) -> float: ...

    def close(self) -> None: ...


# A callable factory must be spawn-pickleable. Callable objects may expose an
# idempotent, non-blocking ``close()`` that wipes parent-owned profile material;
# the runtime invokes it exactly once because closing the spawned copy cannot
# clear the original object in parent memory.
SpeakerShadowBackendFactory = Callable[[], SpeakerShadowBackend]


@dataclass(frozen=True, slots=True)
class SpeakerShadowCandidateKey:
    """Identity private to the observer; it has no ASR execution authority."""

    detector_epoch: int
    shadow_generation: int
    scope: SpeakerShadowScope

    def __post_init__(self) -> None:
        if type(self.detector_epoch) is not int or self.detector_epoch < 0:
            raise ValueError("detector_epoch must be a non-negative integer")
        if type(self.shadow_generation) is not int or self.shadow_generation < 0:
            raise ValueError("shadow_generation must be a non-negative integer")
        if self.scope not in ("provider_candidate", "smart_turn_turn"):
            raise ValueError("scope must be a supported speaker-shadow scope")


@dataclass(frozen=True, slots=True)
class SpeakerShadowDeferredAnchorRequest:
    """Rebase one buffer-only candidate onto a canonical speech origin.

    Both sample counts are relative to the candidate's original deferred
    capture origin. The runtime must fail rather than silently trim a prefix
    that has already fallen outside its bounded rolling buffer.
    """

    candidate: SpeakerShadowCandidateKey
    expected_observed_sample_count: int
    discard_prefix_sample_count: int
    anchor_revision: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidate, SpeakerShadowCandidateKey)
            or type(self.expected_observed_sample_count) is not int
            or type(self.discard_prefix_sample_count) is not int
            or type(self.anchor_revision) is not int
            or self.expected_observed_sample_count < 0
            or self.discard_prefix_sample_count < 0
            or self.discard_prefix_sample_count > self.expected_observed_sample_count
            or self.anchor_revision <= 0
        ):
            raise ValueError("speaker-shadow deferred anchor request is invalid")


@dataclass(frozen=True, slots=True)
class SpeakerShadowDeferredAnchorReceipt:
    """Opaque receipt for one ordered deferred-candidate rebase."""

    runtime_generation: int
    operation_id: int
    candidate: SpeakerShadowCandidateKey
    anchor_revision: int
    observed_sample_count: int
    discarded_sample_count: int
    retained_sample_count: int
    _owner: object


@dataclass(frozen=True, slots=True)
class SpeakerShadowReconcileSource:
    """One synchronously fenced candidate slice consumed by a batch reconcile."""

    candidate: SpeakerShadowCandidateKey
    expected_sample_count: int
    keep_start_sample: int
    keep_end_sample: int

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SpeakerShadowCandidateKey):
            raise ValueError("candidate must be a SpeakerShadowCandidateKey")
        if (
            type(self.expected_sample_count) is not int
            or type(self.keep_start_sample) is not int
            or type(self.keep_end_sample) is not int
            or self.expected_sample_count < 0
            or self.keep_start_sample < 0
            or self.keep_end_sample < self.keep_start_sample
            or self.keep_end_sample > self.expected_sample_count
        ):
            raise ValueError("speaker-shadow reconcile source range is invalid")


@dataclass(frozen=True, slots=True)
class SpeakerShadowBatchReconcileRequest:
    """Atomic split/merge request for one exact Provider audio range.

    Empty source slices are wiped.  Non-empty slices are concatenated in order
    into ``target``.  A remainder after the last kept slice requires ``suffix``
    and is installed there as a deferred successor.
    """

    sources: tuple[SpeakerShadowReconcileSource, ...]
    target: SpeakerShadowCandidateKey
    suffix: SpeakerShadowCandidateKey | None = None
    finish_target: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.sources) is not tuple
            or not self.sources
            or not all(
                isinstance(source, SpeakerShadowReconcileSource)
                for source in self.sources
            )
            or not isinstance(self.target, SpeakerShadowCandidateKey)
            or (
                self.suffix is not None
                and not isinstance(self.suffix, SpeakerShadowCandidateKey)
            )
            or type(self.finish_target) is not bool
        ):
            raise ValueError("speaker-shadow batch reconcile request is invalid")


@dataclass(frozen=True, slots=True)
class SpeakerShadowBatchReconcileReceipt:
    """Opaque receipt for atomically reserved reconciliation ownership.

    Immediate batch reconciliation publishes its marker before returning;
    staged exact reconciliation returns the same receipt after reserving queue
    capacity but before publication.
    """

    runtime_generation: int
    batch_id: int
    target: SpeakerShadowCandidateKey
    suffix: SpeakerShadowCandidateKey | None
    target_sample_count: int
    suffix_sample_count: int
    _owner: object


@dataclass(frozen=True, slots=True)
class SpeakerShadowCaptureResult:
    """Detailed capture result exposed only through an optional capability.

    The result deliberately contains neither a score nor candidate identity.
    ``accepted_sample_count`` describes this call, while
    ``cumulative_sample_count`` describes all PCM admitted for the candidate.
    ``completed_window_sample_count`` is the exact longest scoring window that
    is complete or was scored; it never claims that the whole Provider range
    remains buffered.
    """

    disposition: SpeakerShadowCaptureDisposition
    accepted_sample_count: int
    cumulative_sample_count: int
    completed_window_sample_count: int
    decision_state: SpeakerShadowCaptureDecisionState

    def __post_init__(self) -> None:
        if (
            not isinstance(self.disposition, SpeakerShadowCaptureDisposition)
            or not isinstance(self.decision_state, SpeakerShadowCaptureDecisionState)
            or type(self.accepted_sample_count) is not int
            or type(self.cumulative_sample_count) is not int
            or type(self.completed_window_sample_count) is not int
            or self.accepted_sample_count < 0
            or self.cumulative_sample_count < 0
            or self.completed_window_sample_count < 0
            or self.accepted_sample_count > self.cumulative_sample_count
        ):
            raise ValueError("speaker-shadow capture result is invalid")
        if (self.decision_state is SpeakerShadowCaptureDecisionState.UNAVAILABLE) != (
            self.disposition is SpeakerShadowCaptureDisposition.UNAVAILABLE
        ):
            raise ValueError(
                "speaker-shadow capture unavailable decision and disposition "
                "must be reported together"
            )
        if (
            self.disposition is SpeakerShadowCaptureDisposition.ACCEPTED
            and self.decision_state is not SpeakerShadowCaptureDecisionState.PENDING
        ) or (
            self.disposition is SpeakerShadowCaptureDisposition.COMPLETE
            and self.decision_state
            not in {
                SpeakerShadowCaptureDecisionState.PENDING,
                SpeakerShadowCaptureDecisionState.SCORED,
            }
        ):
            raise ValueError("speaker-shadow capture state combination is invalid")


@dataclass(frozen=True, slots=True)
class SpeakerShadowTerminalCoverageRequest:
    """Exact Provider coverage proposed for one finalized scored candidate.

    Unlike batch reconciliation, this request never asks the runtime to
    reconstruct the target PCM.  The first source must be ``target`` and its
    retained scoring window must begin at sample zero and remain wholly inside
    the kept exact Provider range. ``provider_exact_*`` and
    ``scored_window_*`` are both half-open offsets in canonical 16 kHz samples
    relative to the target candidate's evidence origin; the runtime requires
    both starts to be exactly zero, so a length-preserving prefix trim cannot
    pass. Later live sources may be retired, and a final-source remainder may
    be assigned to a fresh deferred ``suffix``.
    """

    sources: tuple[SpeakerShadowReconcileSource, ...]
    target: SpeakerShadowCandidateKey
    provider_exact_start_sample: int
    provider_exact_end_sample: int
    scored_window_start_sample: int
    scored_window_end_sample: int
    suffix: SpeakerShadowCandidateKey | None = None

    def __post_init__(self) -> None:
        if (
            type(self.sources) is not tuple
            or not self.sources
            or not all(
                isinstance(source, SpeakerShadowReconcileSource)
                for source in self.sources
            )
            or not isinstance(self.target, SpeakerShadowCandidateKey)
            or type(self.provider_exact_start_sample) is not int
            or type(self.provider_exact_end_sample) is not int
            or type(self.scored_window_start_sample) is not int
            or type(self.scored_window_end_sample) is not int
            or self.provider_exact_start_sample < 0
            or self.provider_exact_end_sample <= self.provider_exact_start_sample
            or self.scored_window_start_sample < 0
            or self.scored_window_end_sample <= self.scored_window_start_sample
            or (
                self.suffix is not None
                and not isinstance(self.suffix, SpeakerShadowCandidateKey)
            )
        ):
            raise ValueError("speaker-shadow terminal coverage request is invalid")


@dataclass(frozen=True, slots=True)
class SpeakerShadowTerminalCoverageReceipt:
    """Opaque receipt for a strictly fenced finalized-verdict reservation."""

    runtime_generation: int
    batch_id: int
    target: SpeakerShadowCandidateKey
    suffix: SpeakerShadowCandidateKey | None
    retained_sample_count: int
    covered_sample_count: int
    terminal_preserved: bool
    _owner: object


SpeakerShadowReconciliationReceipt = (
    SpeakerShadowBatchReconcileReceipt | SpeakerShadowTerminalCoverageReceipt
)


@dataclass(frozen=True, slots=True)
class SpeakerShadowConfig:
    """Resource limits and evaluation thresholds for the shadow runtime."""

    enabled: bool = False
    similarity_thresholds: tuple[float, ...] = (0.40, 0.44, 0.48, 0.52, 0.55)
    minimum_audio_ms: int = 1_500
    maximum_audio_ms: int = 4_000
    observation_checkpoints_ms: tuple[int, ...] | None = None
    idle_unload_seconds: float = 60.0
    # A four-second candidate can contribute roughly 400 ten-millisecond
    # frames. Keep them queued while a checkpoint waits on a cold backend;
    # the independent retained-PCM budget remains the memory authority.
    queue_capacity: int = MAX_SPEAKER_SHADOW_QUEUE_CAPACITY
    buffered_candidate_capacity: int = 32
    finalized_candidate_capacity: int = 1_024
    load_retry_initial_seconds: float = 5.0
    load_retry_max_seconds: float = 60.0
    shutdown_grace_seconds: float = 0.1
    callback_timeout_seconds: float = 0.1
    backend_load_timeout_seconds: float = 15.0
    backend_score_timeout_seconds: float = 2.0
    backend_close_timeout_seconds: float = 1.0
    process_terminate_timeout_seconds: float = 1.0
    # Appended to preserve the positional order of the provider-neutral
    # configuration contract used before completion confirmation existed.
    completion_confirmation_scopes: tuple[SpeakerShadowScope, ...] = ()
    pending_observation_gate_scopes: tuple[SpeakerShadowScope, ...] = ()
    backend_prewarm_scopes: tuple[SpeakerShadowScope, ...] = ()
    # Terminal and callback delivery have independent bounded capacity so PCM
    # pressure cannot consume their reserved admission budget.
    terminal_queue_capacity: int = 512
    completion_queue_capacity: int = 512
    # Only Provider candidates waiting for an exact boundary retain their
    # original buffer after terminal scoring; this never retains a second copy.
    exact_boundary_pcm_retention_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (
            type(self.exact_boundary_pcm_retention_seconds) not in {int, float}
            or not math.isfinite(self.exact_boundary_pcm_retention_seconds)
            or not 0 <= self.exact_boundary_pcm_retention_seconds <= 2.0
        ):
            raise ValueError("exact_boundary_pcm_retention_seconds must be within [0, 2]")
        if (
            not self.similarity_thresholds
            or len(self.similarity_thresholds) > MAX_SPEAKER_SHADOW_THRESHOLDS
            or any(
                not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0
                for threshold in self.similarity_thresholds
            )
            or any(
                left >= right
                for left, right in zip(
                    self.similarity_thresholds,
                    self.similarity_thresholds[1:],
                )
            )
        ):
            raise ValueError(
                "similarity_thresholds must contain at most "
                f"{MAX_SPEAKER_SHADOW_THRESHOLDS} finite, unique, increasing "
                "values within [0, 1]"
            )
        if self.minimum_audio_ms <= 0:
            raise ValueError("minimum_audio_ms must be positive")
        if self.maximum_audio_ms < self.minimum_audio_ms:
            raise ValueError("maximum_audio_ms must be at least minimum_audio_ms")
        if self.maximum_audio_ms > MAX_SPEAKER_SHADOW_CANDIDATE_AUDIO_MS:
            raise ValueError(
                "maximum_audio_ms cannot exceed "
                f"{MAX_SPEAKER_SHADOW_CANDIDATE_AUDIO_MS}"
            )
        checkpoints = self.observation_checkpoints_ms
        if checkpoints is not None and (
            type(checkpoints) is not tuple
            or not checkpoints
            or len(checkpoints) > MAX_SPEAKER_SHADOW_CHECKPOINTS
            or any(
                type(checkpoint) is not int
                or checkpoint < self.minimum_audio_ms
                or checkpoint > self.maximum_audio_ms
                for checkpoint in checkpoints
            )
            or any(left >= right for left, right in zip(checkpoints, checkpoints[1:]))
        ):
            raise ValueError(
                "observation_checkpoints_ms must contain at most "
                f"{MAX_SPEAKER_SHADOW_CHECKPOINTS} unique, increasing integer "
                "values within [minimum_audio_ms, maximum_audio_ms]"
            )
        confirmation_scopes = self.completion_confirmation_scopes
        if (
            type(confirmation_scopes) is not tuple
            or any(
                scope not in ("provider_candidate", "smart_turn_turn")
                for scope in confirmation_scopes
            )
            or any(
                scope in confirmation_scopes[index + 1 :]
                for index, scope in enumerate(confirmation_scopes)
            )
        ):
            raise ValueError(
                "completion_confirmation_scopes must be a tuple of unique, "
                "supported speaker-shadow scopes"
            )
        if confirmation_scopes and (checkpoints is None or len(checkpoints) < 2):
            raise ValueError(
                "completion_confirmation_scopes requires at least two explicit "
                "observation_checkpoints_ms"
            )
        pending_gate_scopes = self.pending_observation_gate_scopes
        if (
            type(pending_gate_scopes) is not tuple
            or any(
                scope not in ("provider_candidate", "smart_turn_turn")
                for scope in pending_gate_scopes
            )
            or any(
                scope in pending_gate_scopes[index + 1 :]
                for index, scope in enumerate(pending_gate_scopes)
            )
        ):
            raise ValueError(
                "pending_observation_gate_scopes must be a tuple of unique, "
                "supported speaker-shadow scopes"
            )
        if any(scope not in confirmation_scopes for scope in pending_gate_scopes):
            raise ValueError(
                "pending_observation_gate_scopes must be a subset of "
                "completion_confirmation_scopes"
            )
        prewarm_scopes = self.backend_prewarm_scopes
        if (
            type(prewarm_scopes) is not tuple
            or any(
                scope not in ("provider_candidate", "smart_turn_turn")
                for scope in prewarm_scopes
            )
            or any(
                scope in prewarm_scopes[index + 1 :]
                for index, scope in enumerate(prewarm_scopes)
            )
        ):
            raise ValueError(
                "backend_prewarm_scopes must be a tuple of unique, supported "
                "speaker-shadow scopes"
            )
        if any(scope not in pending_gate_scopes for scope in prewarm_scopes):
            raise ValueError(
                "backend_prewarm_scopes must be a subset of "
                "pending_observation_gate_scopes"
            )
        if not math.isfinite(self.idle_unload_seconds) or self.idle_unload_seconds <= 0:
            raise ValueError("idle_unload_seconds must be positive")
        if not 0 < self.queue_capacity <= MAX_SPEAKER_SHADOW_QUEUE_CAPACITY:
            raise ValueError(
                "queue_capacity must be within "
                f"[1, {MAX_SPEAKER_SHADOW_QUEUE_CAPACITY}]"
            )
        self._validate_capacity(
            "terminal_queue_capacity",
            self.terminal_queue_capacity,
            MAX_SPEAKER_SHADOW_TERMINAL_QUEUE_CAPACITY,
        )
        self._validate_capacity(
            "completion_queue_capacity",
            self.completion_queue_capacity,
            MAX_SPEAKER_SHADOW_COMPLETION_QUEUE_CAPACITY,
        )
        if not (
            0
            < self.buffered_candidate_capacity
            <= MAX_SPEAKER_SHADOW_BUFFERED_CANDIDATES
        ):
            raise ValueError(
                "buffered_candidate_capacity must be within "
                f"[1, {MAX_SPEAKER_SHADOW_BUFFERED_CANDIDATES}]"
            )
        if self.finalized_candidate_capacity < self.queue_capacity:
            raise ValueError(
                "finalized_candidate_capacity must be at least queue_capacity"
            )
        if self.finalized_candidate_capacity > MAX_SPEAKER_SHADOW_FINALIZED_CANDIDATES:
            raise ValueError(
                "finalized_candidate_capacity cannot exceed "
                f"{MAX_SPEAKER_SHADOW_FINALIZED_CANDIDATES}"
            )
        if (
            not math.isfinite(self.load_retry_initial_seconds)
            or self.load_retry_initial_seconds <= 0
        ):
            raise ValueError("load_retry_initial_seconds must be positive")
        if (
            not math.isfinite(self.load_retry_max_seconds)
            or self.load_retry_max_seconds < self.load_retry_initial_seconds
        ):
            raise ValueError(
                "load_retry_max_seconds must be at least load_retry_initial_seconds"
            )
        self._validate_timeout(
            "shutdown_grace_seconds",
            self.shutdown_grace_seconds,
            MAX_SPEAKER_SHADOW_SHUTDOWN_GRACE_SECONDS,
        )
        self._validate_timeout(
            "callback_timeout_seconds",
            self.callback_timeout_seconds,
            MAX_SPEAKER_SHADOW_CALLBACK_TIMEOUT_SECONDS,
        )
        self._validate_timeout(
            "backend_load_timeout_seconds",
            self.backend_load_timeout_seconds,
            MAX_SPEAKER_SHADOW_BACKEND_LOAD_SECONDS,
        )
        self._validate_timeout(
            "backend_score_timeout_seconds",
            self.backend_score_timeout_seconds,
            MAX_SPEAKER_SHADOW_BACKEND_SCORE_SECONDS,
        )
        self._validate_timeout(
            "backend_close_timeout_seconds",
            self.backend_close_timeout_seconds,
            MAX_SPEAKER_SHADOW_BACKEND_CLOSE_SECONDS,
        )
        self._validate_timeout(
            "process_terminate_timeout_seconds",
            self.process_terminate_timeout_seconds,
            MAX_SPEAKER_SHADOW_PROCESS_TERMINATE_SECONDS,
        )

    @staticmethod
    def _validate_timeout(name: str, value: float, maximum: float) -> None:
        if not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError(f"{name} must be finite and within (0, {maximum}]")

    @staticmethod
    def _validate_capacity(name: str, value: int, maximum: int) -> None:
        if type(value) is not int or not 0 < value <= maximum:
            raise ValueError(f"{name} must be an integer within [1, {maximum}]")


@dataclass(frozen=True, slots=True)
class SpeakerShadowObservation:
    """Ephemeral score delivered only to an in-memory observer callback."""

    candidate: SpeakerShadowCandidateKey
    similarity: float
    would_block: tuple[tuple[float, bool], ...]
    audio_ms: int
    checkpoint_ms: int | None = None
    observation_kind: SpeakerShadowObservationKind = "checkpoint"
    # Per-candidate delivery sequence.  Zero is retained only for legacy test
    # fixtures; production runtime events always start at one and increment by
    # exactly one before the corresponding callback is scheduled.
    sequence_no: int = 0
    # False is an explicit fail-open placeholder emitted when scoring or
    # callback delivery cannot preserve the candidate's ordered evidence.
    evidence_available: bool = True


ObservationCallback = Callable[
    [SpeakerShadowObservation],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class SpeakerShadowCompletion:
    """Ordered terminal notice containing no ASR authority or private audio."""

    candidate: SpeakerShadowCandidateKey
    terminal_reason: SpeakerShadowTerminalReason
    last_checkpoint_ms: int | None
    # Highest observation sequence assigned before this ordered close.  A
    # consumer may trust the evidence only when every sequence through this
    # value was delivered and ``evidence_complete`` is true.
    through_sequence_no: int = 0
    evidence_complete: bool = True


CompletionCallback = Callable[
    [SpeakerShadowCompletion],
    Awaitable[None],
]

# Production authority uses one synchronous, non-blocking sink for both facts
# and close.  The serial speaker worker invokes it in candidate order, so no
# callback task lifecycle can reorder evidence.  The two async callbacks above
# remain observation-only compatibility seams.
SpeakerShadowEvidenceEvent = SpeakerShadowObservation | SpeakerShadowCompletion
EvidenceCallback = Callable[[SpeakerShadowEvidenceEvent], None]


@dataclass(slots=True)
class SpeakerShadowMetrics:
    """Aggregate counters only; no identity, PCM, embedding, or score data."""

    submitted_frame_count: int = 0
    submitted_audio_ms: int = 0
    started_candidate_count: int = 0
    finished_candidate_count: int = 0
    scored_candidate_count: int = 0
    insufficient_candidate_count: int = 0
    dropped_candidate_count: int = 0
    failed_candidate_count: int = 0
    evaluated_candidate_count: int = 0
    would_block_count: int = 0
    dropped_frame_count: int = 0
    dropped_audio_ms: int = 0
    stale_result_count: int = 0
    load_count: int = 0
    unload_count: int = 0
    load_failure_count: int = 0
    unload_failure_count: int = 0
    backend_timeout_count: int = 0
    backend_process_termination_count: int = 0
    inference_failure_count: int = 0
    callback_failure_count: int = 0
    completion_count: int = 0
    completion_before_first_checkpoint_count: int = 0
    completion_after_first_checkpoint_count: int = 0
    completion_callback_failure_count: int = 0
    terminal_queued_count: int = 0
    terminal_overflow_count: int = 0
    terminal_abandoned_count: int = 0
    reconciliation_batch_admitted_count: int = 0
    reconciliation_batch_applied_count: int = 0
    reconciliation_batch_failed_count: int = 0
    reconciliation_batch_revoked_count: int = 0
    completion_dispatched_count: int = 0
    completion_attempted_count: int = 0
    completion_overflow_count: int = 0
    completion_abandoned_count: int = 0
    completion_stall_count: int = 0
    pending_terminal_count: int = 0
    pending_completion_count: int = 0
    detached_callback_task_count: int = 0
    delivery_degraded_count: int = 0
    load_retry_suppressed_count: int = 0
    worker_start_failure_count: int = 0
    shutdown_timeout_count: int = 0
    load_ms: int = 0
    inference_ms: int = 0

    def snapshot(self) -> dict[str, int]:
        return asdict(self)


class SpeakerShadowObserver(Protocol):
    """Non-authoritative interface consumed by endpointing."""

    @property
    def enabled(self) -> bool: ...

    def submit(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool: ...

    def finish_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool: ...

    def snapshot(self) -> dict[str, int]: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class SpeakerShadowCaptureStatus(Protocol):
    """Optional single-submit capability with completion-aware disposition."""

    def submit_capture(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> SpeakerShadowCaptureResult: ...


@runtime_checkable
class SpeakerShadowCandidateLifecycleControl(Protocol):
    """Optional candidate-local retirement without resetting the observer.

    The caller has already revoked the external lease before invoking this
    control.  Implementations must synchronously fence queued or in-flight work
    for ``candidate`` so a late score cannot publish after the lease is
    abandoned.  No completion or unavailable fact is emitted: lifecycle
    abandonment is owned by the caller, not inferred from speaker evidence.
    """

    def abandon_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool: ...


@runtime_checkable
class SpeakerShadowDeferredCandidateStatus(Protocol):
    """Optional read-only capability query for deferred candidate buffering."""

    def supports_deferred_candidate(
        self,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool: ...


@runtime_checkable
class SpeakerShadowDeferredCandidateControl(Protocol):
    """Optional ordered control for candidate PCM admitted before scoring."""

    def defer_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool: ...

    def activate_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool: ...


@runtime_checkable
class SpeakerShadowDeferredAnchorControl(Protocol):
    """Optional ordered control for canonical deferred-candidate anchoring."""

    def anchor_deferred_candidate(
        self,
        request: SpeakerShadowDeferredAnchorRequest,
    ) -> SpeakerShadowDeferredAnchorReceipt | None: ...

    def deferred_anchor_status(
        self,
        receipt: SpeakerShadowDeferredAnchorReceipt,
    ) -> SpeakerShadowDeferredAnchorStatus: ...

    async def wait_deferred_anchor_settled(
        self,
        receipt: SpeakerShadowDeferredAnchorReceipt,
        *,
        deadline: float,
    ) -> SpeakerShadowDeferredAnchorStatus: ...


@runtime_checkable
class SpeakerShadowCandidateReconciliationControl(Protocol):
    """Optional sample-exact ownership transfer between ordered candidates.

    ``prefix_sample_count`` is measured in canonical 16 kHz samples from the
    beginning of ``source``.  A distinct ``target`` receives the covered
    prefix while ``source is target`` seals that candidate at the prefix.  If
    samples remain, ``suffix`` must name a fresh deferred candidate that owns
    them.  ``True`` means the ownership reservation and its same-queue control
    marker were accepted; it does not grant authority over ASR delivery.
    """

    def reconcile_candidate_prefix(
        self,
        *,
        source: SpeakerShadowCandidateKey,
        target: SpeakerShadowCandidateKey,
        prefix_sample_count: int,
        suffix: SpeakerShadowCandidateKey | None = None,
    ) -> bool: ...


@runtime_checkable
class SpeakerShadowBatchReconciliationControl(Protocol):
    """Optional all-or-nothing ownership reconcile for one exact boundary."""

    def reconcile_candidate_batch(
        self,
        request: SpeakerShadowBatchReconcileRequest,
    ) -> SpeakerShadowBatchReconcileReceipt | None: ...

    def reconciliation_status(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> SpeakerShadowReconciliationStatus: ...

    def revoke_reconciliation(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> None: ...


@runtime_checkable
class SpeakerShadowReconciliationCompletionControl(Protocol):
    """Optional retirement of an applied receipt after ownership transfer."""

    def complete_reconciliation(
        self,
        receipt: SpeakerShadowReconciliationReceipt,
        *,
        successor: SpeakerShadowCandidateKey | None,
    ) -> Literal["completed", "already_completed", "pending", "stale", "invalid"]: ...


@runtime_checkable
class SpeakerShadowExactIntervalScoreControl(Protocol):
    """Read-only qualification for splitting PCM into a new score identity."""

    def exact_interval_requires_fresh_target(
        self,
        source: SpeakerShadowReconcileSource,
    ) -> bool: ...


@runtime_checkable
class SpeakerShadowExactIntervalControl(Protocol):
    """Optional staged ownership transfer for one exact Provider interval.

    Preparation freezes the source and reserves all candidate and queue
    capacity, but does not publish work to the shadow worker.  The opaque
    receipt is intentionally consumed only by DetectorRuntime; callers must
    either commit or abort it exactly once.
    """

    def prepare_exact_interval(
        self,
        request: SpeakerShadowBatchReconcileRequest,
    ) -> SpeakerShadowBatchReconcileReceipt | None: ...

    def commit_exact_interval(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> bool: ...

    def abort_exact_interval(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> bool: ...


@runtime_checkable
class SpeakerShadowTerminalCoverageControl(Protocol):
    """Optional exact coverage for an already-finalized scored candidate."""

    def reconcile_finalized_candidate_coverage(
        self,
        request: SpeakerShadowTerminalCoverageRequest,
    ) -> SpeakerShadowTerminalCoverageReceipt | None: ...

    def terminal_coverage_status(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> SpeakerShadowReconciliationStatus: ...

    def revoke_terminal_coverage(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> None: ...


@runtime_checkable
class SpeakerShadowPreparedTerminalCoverageControl(Protocol):
    """Two-phase finalized coverage used by an upper atomic transaction."""

    def prepare_finalized_candidate_coverage(
        self,
        request: SpeakerShadowTerminalCoverageRequest,
    ) -> SpeakerShadowTerminalCoverageReceipt | None: ...

    def commit_finalized_candidate_coverage(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> bool: ...

    def abort_finalized_candidate_coverage(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> bool: ...


@runtime_checkable
class SpeakerShadowReconciliationSettlement(Protocol):
    """Event-driven wait using an absolute ``time.monotonic()`` deadline."""

    async def wait_reconciliation_settled(
        self,
        receipt: SpeakerShadowReconciliationReceipt,
        *,
        deadline: float,
    ) -> SpeakerShadowReconciliationStatus: ...


@runtime_checkable
class SpeakerShadowDecisionStatus(Protocol):
    """Optional read-only status with no authority over ASR execution."""

    def requires_provisional_decision(
        self,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool: ...
