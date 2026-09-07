"""Provider-neutral independent-ASR runtime with explicit Core callbacks."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from main_logic.asr_client import (
    _attach_partial_callback,
    _create_asr_session_from_selection,
    _resolve_asr_selection,
)
from main_logic.voice_turn.contracts import (
    AsrFailureEvent,
    AsrLifecycleNotification,
    AsrStatusEvent,
    AsrSubmitResult,
    AsrSubmitStatus,
    SpeechActivityEvent,
    VoicePartialEvent,
    VoiceTranscriptEvent,
    VoiceTurnToken,
)
from main_logic.voice_turn.audio_input import ProcessedVoiceFrame

from utils.logger_config import get_module_logger
from ._infra import logger, _READY_TIMEOUT_SECONDS
from .diagnostic_logging import submit_resolution_log
from .failure_diagnostics import AudioFailureContext, CleanupTrace, utc_now
from .speaker_diagnostics import diagnostic_context, speaker_diagnostic_scalars
from .speaker_shadow.diagnostics import SpeakerShadowDiagnostic
from ._provider_events import (
    ProviderEndpointNotification,
    ProviderFinalNotification,
    ProviderStartedSettlement,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from .audio import AsrAudioDispatcher
from .speaker_verifier_installation import SpeakerVerifierInstallation
from .speaker_verifier_contracts import (
    SpeakerVerifierHealthEvent,
    SpeakerVerifierInstallIdentity,
    SpeakerVerifierInstallReceipt,
    SpeakerVerifierSpec,
)
from .admission.contracts import (
    AbortProviderTransport,
    AdmissionDisposition,
    AdmissionEffect,
    AdmissionOperationTicket,
    AdmissionResolutionTicket,
    ApplyRejection,
    BoundaryExact,
    BoundaryProof,
    BoundaryUnknown,
    CandidateBindingState,
    CandidateBound,
    CapabilityRevokeFailed,
    CapabilityRevoked,
    CaptureState,
    CaptureClosed,
    Close,
    ConstrainRejectionDeadline,
    CoreSettled,
    CountDiagnostic,
    ExactIntervalActivationReceipt,
    ExactIntervalOutcome,
    ExactIntervalPromotionReceipt,
    ExactIntervalPromotionResult,
    ExactIntervalPromotionScope,
    ExactIntervalTransitionReceipt,
    EvidenceState,
    EvidenceHoldRequested,
    EvidenceHoldResolved,
    EvidenceDeadlineExpired,
    ScheduleEvidenceDeadline,
    FinalDeadlineExpired,
    LifecycleSettled,
    MicroEventPending,
    MicroEventUnavailable,
    PendingProviderFinal,
    PoisonSpeakerAuthorityNamespace,
    ProviderBound,
    ProviderBindingState,
    ProviderFinalReceived,
    RejectionApplied,
    RejectionCapability,
    RejectionCapabilityKind,
    RejectionFailed,
    RejectionStale,
    Reset,
    ResolveReserved,
    RevokeRejectionCapability,
    RouteReplaced,
    ScheduleFinalDeadline,
    SettlePartial,
    SpeakerAuthorityPending,
    SpeakerAuthorityUnarmed,
    SpeakerAuthorityUnavailable,
    SpeakerCheckpointKind,
    SpeakerCaptureLeaseToken,
    SpeakerHigh,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseAbandoned,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseEvent,
    SpeakerLeaseState,
    SpeakerLeaseTerminalClaim,
    SpeakerLeaseTransitionOutcome,
    SpeakerLeaseTransitionReceipt,
    SpeakerLeaseUnavailable,
    SpeakerLow,
    SpeakerUnavailable,
    SpeakerAuthorityNamespacePoisonFailed,
    SpeakerAuthorityNamespacePoisoned,
    TransportSettled,
    TurnSealed,
)
from .admission.coordinator import AdmissionBulkResult, VoiceTurnAdmissionCoordinator
from .speaker_evidence import (
    AudioRangeReference, EvidenceWindow, ProviderEvidenceBinding, EvidenceProof,
    EvidenceObservationRegistry,
)
from .admission.ingress import (
    AdmissionIngressCapacityError,
    AdmissionIngressClosedError,
    AdmissionIngressLane,
)
from .admission.provider_turns import (
    ProviderAliasConflictError,
    ProviderBoundaryResult,
    ProviderTurnCorrelator,
)
from ._registry_meta import (
    AsrProviderAvailability,
    AsrSpeakerExactIntervalCapability,
)
from .endpointing.detector import (
    AsrDetectorDispatcher,
    CoreDetectorEventEnvelope,
    DetectorActivityEvent,
    DetectorCandidateKey,
    DetectorIngressIdentity,
    DetectorPrewarmEvent,
    DetectorRuntimeEvent,
    DetectorTransportPrewarmEvent,
    DetectorSubmitStatus,
    DetectorTurnEvent,
    ProviderCandidateFence,
    ProviderSpeakerBoundarySnapshot,
)
from .endpointing.detector_runtime import (
    ProviderAudioAccountingReceipt,
    DetectorCandidateRejectionCommitResult,
    DetectorCandidateRejectionLease,
    DetectorRuntime,
    ProviderExactSpeakerIntervalReservation,
    ProviderSpeakerEvidenceLease,
    ProviderSpeakerEvidenceSettlement,
    ProviderSpeakerEvidenceUpdate,
    SmartTurnLease,
)
from .endpointing.micro_event_policy import (
    ProviderMicroEventConfig,
)
from .endpointing.throttle_policy import ThrottleAction
from .lifecycle import (
    AudioDisposition,
    FinalKey,
    VoiceIngressToken,
    VoiceInputLifecycleController,
    VoiceLifecycleEvent,
    VoiceLifecycleState,
    VoiceRouteMode,
    VoiceTransportToken,
)
from .provider_policy import resolve_provider_policy
from .speaker_shadow.contracts import (
    SpeakerShadowCaptureDisposition,
    SpeakerShadowCandidateKey,
    SpeakerShadowObserver,
)
from .transcript import (
    TranscriptDispatcher,
    TranscriptEnvelope,
    TranscriptResolutionOutcome,
    TranscriptResolutionReceipt,
    TranscriptTerminalSettlement,
)


asr_diagnostic_logger = get_module_logger("asr_diagnostics", "Main")

# The frontend gives a voice start this long before it cancels and fires
# end_session (app-buttons.js, and the automatic-restart path in
# app-websocket.js use the same value). Mirrored here because
# _start_session_activate awaits the ASR connect loop BEFORE sending
# session_started: any retry budget that outlives this deadline cannot produce
# a verdict the client will still be listening for.
_FRONTEND_START_DEADLINE_SECONDS = 15.0

# Aggregate ceiling for the whole connect-and-retry phase. Deliberately under
# the deadline above, leaving room for the rest of the start (the ack send and
# the pending-input flush that follow it) so the fail-closed verdict lands
# BEFORE the client gives up rather than a second after.
_CONNECT_TOTAL_BUDGET_SECONDS = 12.0

# Public alias. The dedupe reroute in core/lifecycle.py runs a whole extra
# connect phase AFTER already spending part of the frontend deadline waiting,
# so it has to know this ceiling to tell whether its verdict can still land
# before the client gives up.
ASR_CONNECT_TOTAL_BUDGET_SECONDS = _CONNECT_TOTAL_BUDGET_SECONDS
_CANDIDATE_REJECTION_WATCHDOG_SECONDS = 10.0
_CANDIDATE_REJECTION_RECOVERY_STEP_TIMEOUT_SECONDS = 1.0
_CANDIDATE_REJECTION_REINSTALL_ATTEMPTS = 2
_SPEAKER_CANDIDATE_DECISION_TIMEOUT_SECONDS = 0.2
_PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS = 0.2
_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS = 1.0
_EXACT_LIFECYCLE_NOTIFICATION_TIMEOUT_SECONDS = 0.2
# Core's normal response cancellation can itself take 3s. Preparation needs
# its own larger budget; the display-only 200ms limit is not suitable here.
_EXACT_PENDING_PREPARE_TIMEOUT_SECONDS = 5.0
# Core may reserve this much additional ingress while the serial PCM consumer
# waits for an exact handoff. Keep storage and the operation's budget coupled.
ASR_HANDOFF_BUFFER_RESERVE_US = 6_000_000
_EXACT_PENDING_HANDOFF_TIMEOUT_SECONDS = ASR_HANDOFF_BUFFER_RESERVE_US / 1_000_000
_EXACT_HANDOFF_FAILURE_NOTIFICATION_TIMEOUT_SECONDS = 1.0
_ASR_TERMINAL_HARD_CLOSE_RESERVE_SECONDS = 0.6
_ASR_TERMINAL_CLOSE_JOIN_SLICE_SECONDS = 0.1
_MAX_BUFFERED_PROVIDER_SPEAKER_SPANS = 8
_MAX_PROVIDER_BOUNDARY_SNAPSHOTS = 8
_MAX_DEFERRED_PROVIDER_SPEAKER_LEASE_EVENTS = 8
_MAX_PROVIDER_PROVISIONAL_SPEAKER_EVENTS = 16
_MAX_PROVIDER_EXACT_TRANSACTION_EVENTS = 32
_MAX_SPEAKER_EVIDENCE_BRIDGE_RECORDS = 256
_ASR_REASON_CODE_RE = re.compile(r"^(ASR_[A-Z0-9_]{1,60})(?::|$)")
_ASR_REASON_CODE_FULL_RE = re.compile(r"^ASR_[A-Z0-9_]{1,60}$")
_PROVIDER_MICRO_EVENT_SHADOW_CONFIG = ProviderMicroEventConfig(
    mode="shadow",
    calibration_revision=None,
    maximum_silero_span_ms=384,
    maximum_post_start_onset_windows=4,
    maximum_rnnoise_active_run_upper_bound_ms=160,
)
_SPEAKER_FAILURE_REASON_CATEGORIES = (
    "gap",
    "overflow",
    "anchor",
    "prepare",
    "identity",
    "sequence",
    "proof",
)
_SPEAKER_REJECTION_METRIC_NAMES = (
    "speaker_deny_latched_count",
    "speaker_deny_final_dropped_count",
    "speaker_deny_cleanup_failed_count",
    "speaker_late_fact_stale_count",
    "speaker_partial_quarantined_count",
    "rejection_request_failed_count",
    "rejection_task_scheduled_count",
    "rejection_task_applied_count",
    "rejection_task_stale_count",
    "rejection_stale_initial_count",
    "rejection_stale_prepare_count",
    "rejection_stale_runtime_fence_count",
    "rejection_stale_candidate_fence_count",
    "rejection_stale_smart_turn_count",
    "rejection_stale_commit_count",
    "rejection_task_cleanup_degraded_count",
    "rejection_task_failure_count",
    "rejection_task_cancelled_count",
    "admission_terminal_forward_count",
    "admission_terminal_drop_count",
    "admission_terminal_abandon_count",
    "admission_deadline_forward_count",
    "admission_rejection_applied_active_count",
    "admission_rejection_applied_sealed_count",
    "admission_core_settlement_degraded_count",
    "admission_transport_settlement_degraded_count",
    "admission_lifecycle_settlement_degraded_count",
    "admission_boundary_proof_retired_count",
    "admission_boundary_proof_overflow_count",
    "admission_late_boundary_ignored_count",
    "admission_late_fact_ignored_count",
    "admission_stale_turn_opened_count",
    "admission_provider_alias_conflict_count",
    "admission_candidate_alias_conflict_count",
    "admission_stale_speaker_fact_count",
    "admission_stale_capture_close_count",
    "admission_speaker_sequence_gap_count",
    "admission_late_exact_after_unknown_count",
    "admission_foreign_capability_ignored_count",
    "admission_capability_conflict_count",
    "admission_stale_boundary_unknown_count",
    "admission_revoked_operation_applied_late_count",
    "admission_late_operation_ignored_count",
    "admission_rejection_kind_mismatch_count",
    "admission_revoked_operation_settled_count",
    "admission_late_revoke_ack_ignored_count",
    "admission_late_revoke_failure_ignored_count",
    "admission_late_namespace_poison_ack_count",
    "admission_late_namespace_poison_failure_count",
    "admission_final_after_retirement_ignored_count",
    "admission_stale_provider_final_count",
    "admission_conflicting_provider_final_count",
    "admission_late_deadline_ignored_count",
    "admission_stale_core_settlement_count",
    "admission_stale_transport_settlement_count",
    "admission_stale_lifecycle_settlement_count",
    "provider_candidate_bind_missing_identity_count",
    "provider_candidate_bind_missing_candidate_count",
    "provider_candidate_bind_identity_rejected_count",
    "provider_candidate_bind_deferred_count",
    "provider_candidate_bind_state_skipped_count",
    "provider_candidate_bind_attempt_count",
    "provider_candidate_bind_success_count",
    "provider_candidate_bind_empty_count",
    "provider_candidate_bind_failed_count",
    "provider_boundary_preseal_started_count",
    "provider_boundary_exact_ready_count",
    "provider_boundary_unknown_ready_count",
    "provider_boundary_conflict_count",
    "provider_boundary_overflow_count",
    "provider_boundary_stale_count",
    "provider_boundary_ordered_jit_unknown_count",
    "provider_preseal_rejection_consumed_count",
    "provider_preseal_rejection_stale_count",
    "speaker_anchor_deferred_count",
    "speaker_anchor_success_count",
    "speaker_anchor_evicted_count",
    "speaker_anchor_conflict_count",
    "speaker_provisional_fact_count",
    "speaker_pre_anchor_fact_ignored_count",
    "speaker_ledger_poisoned_count",
    *(
        f"speaker_ledger_poisoned_reason_{reason}_count"
        for reason in _SPEAKER_FAILURE_REASON_CATEGORIES
    ),
    "speaker_exact_prepare_count",
    "speaker_exact_commit_count",
    "speaker_exact_abort_count",
    "speaker_unavailable_count",
    *(
        f"speaker_unavailable_reason_{reason}_count"
        for reason in _SPEAKER_FAILURE_REASON_CATEGORIES
    ),
    "unsupported_asr_route_count",
    "micro_event_suppressed_count",
    "micro_event_shadow_forward_count",
)


def _new_speaker_rejection_metrics() -> dict[str, int]:
    return {name: 0 for name in _SPEAKER_REJECTION_METRIC_NAMES}


def _speaker_failure_reason_category(reason: str) -> str:
    """Collapse internal failures into a bounded, content-free dimension."""

    normalized = str(reason).upper()
    if "GAP" in normalized:
        return "gap"
    if "CAPACITY" in normalized or "OVERFLOW" in normalized:
        return "overflow"
    if "ANCHOR" in normalized or "CANONICAL_START" in normalized:
        return "anchor"
    if (
        "EXACT" in normalized
        or "BOUNDARY" in normalized
        or "PREPARE" in normalized
        or "PRESEAL" in normalized
    ):
        return "prepare"
    if (
        "IDENTITY" in normalized
        or "PROVIDER_KEY" in normalized
        or "CANDIDATE_MISMATCH" in normalized
    ):
        return "identity"
    if (
        "SEQUENCE" in normalized
        or "REORDER" in normalized
        or "DUPLICATE" in normalized
        or "AFTER_CLOSE" in normalized
    ):
        return "sequence"
    return "proof"


def _speaker_factory_enforces_admission(
    factory: SpeakerShadowFactory | None,
) -> bool:
    """Accept only an explicit internal admission-enforcement declaration."""

    return getattr(factory, "enforces_admission", False) is True


def _uses_smart_turn_endpointing(provider_policy: Any) -> bool:
    """Honor the endpoint authority independently of transport shape."""

    return bool(provider_policy.endpoint_authority == "smart_turn")


def _extract_asr_reason_code(value: Any, *, fallback: Any) -> str:
    """Return only a bounded ASR code, never provider error text."""

    try:
        candidate = str(value).strip()
    except Exception:
        candidate = ""
    match = _ASR_REASON_CODE_RE.match(candidate)
    if match is not None:
        return match.group(1)
    try:
        fallback_code = str(fallback).strip()
    except Exception:
        fallback_code = ""
    if _ASR_REASON_CODE_FULL_RE.fullmatch(fallback_code) is not None:
        return fallback_code
    return "ASR_INDEPENDENT_FAILED"


class AsrStartStatus(Enum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AsrStartResult:
    status: AsrStartStatus
    provider: str | None = None
    failure_code: str | None = None
    session_epoch: int = -1


@dataclass(frozen=True, slots=True)
class AsrRuntimeCallbacks:
    display_name: Callable[[], str]
    on_prepare_turn: Callable[[VoiceTurnToken], Awaitable[bool]]
    on_partial: Callable[[VoicePartialEvent], Awaitable[None]]
    on_final: Callable[[VoiceTranscriptEvent], Awaitable[None]]
    on_turn_abandoned: Callable[[VoiceTurnToken], Awaitable[None]]
    on_failure: Callable[[AsrFailureEvent], Awaitable[None]]
    on_status: Callable[[AsrStatusEvent], Awaitable[None]]
    on_lifecycle: Callable[[AsrLifecycleNotification], Awaitable[None]]


SpeakerShadowFactory = Callable[[], SpeakerShadowObserver | None]


class _SpeakerArmingStatus(Enum):
    ARMED = "armed"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    STALE = "stale"
    INVARIANT_FAILURE = "invariant_failure"


@dataclass(frozen=True, slots=True)
class _SpeakerArmingResult:
    status: _SpeakerArmingStatus
    owner_generation: str | None = None
    reason_code: str | None = None

    def __bool__(self) -> bool:
        return self.status in {
            _SpeakerArmingStatus.ARMED,
            _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE,
        }


@dataclass(frozen=True, slots=True)
class _AsrRuntimeIdentity:
    start_generation: int
    session_epoch: int
    audio_generation: int
    lifecycle: VoiceInputLifecycleController | None
    transport_generation: int | None
    detector: DetectorRuntime | None
    session: Any
    provider: str | None
    session_factory: Any
    transport_selection: Any
    transport_task: asyncio.Task[None] | None
    ingress_token: VoiceIngressToken | None = None
    turn_token: VoiceTurnToken | None = None


@dataclass(frozen=True, slots=True)
class _ProviderBoundaryCompletion:
    snapshot: ProviderSpeakerBoundarySnapshot
    successor_evidence_lease: ProviderSpeakerEvidenceLease | None
    detector: DetectorRuntime


@dataclass(frozen=True, slots=True)
class _PendingTurnHandoff:
    """One exact settlement owns activation until buffered PCM is queued."""

    identity: _AsrRuntimeIdentity
    completion: asyncio.Future[bool]


class _PendingTurnPreparationError(RuntimeError):
    """A required successor preparation could not finish safely."""


@dataclass(frozen=True, slots=True)
class _SpeakerEvidenceDegradation:
    identity: _AsrRuntimeIdentity
    activation_generation: str
    reason_code: str
    incident_id: str


@dataclass(slots=True)
class _BufferedProviderSpeakerSpan:
    """One PCM-free ordered span inside lifecycle-owned buffered audio."""

    start_byte: int
    end_byte: int
    first_identity: DetectorIngressIdentity | None
    last_identity: DetectorIngressIdentity | None
    split_before_audio: bool
    evidence_complete: bool


@dataclass(slots=True)
class _BufferedProviderSpeakerObservation:
    """Bounded span metadata for PCM retained only by the lifecycle."""

    total_bytes: int
    spans: list[_BufferedProviderSpeakerSpan]
    overflowed: bool = False


@dataclass(slots=True)
class _AdmissionCapabilityOwner:
    capability: RejectionCapability
    lease: DetectorCandidateRejectionLease
    detector: DetectorRuntime
    runtime_identity: _AsrRuntimeIdentity
    revoked: bool = False


@dataclass(slots=True)
class _AdmissionFinalContext:
    turn_token: VoiceTurnToken
    final_key: FinalKey
    epoch: int
    provider: str
    provider_key: ProviderUtteranceKey | None
    lifecycle: VoiceInputLifecycleController
    detector: DetectorRuntime | None
    correlator: ProviderTurnCorrelator | None
    sealed_token: VoiceTransportToken
    provider_fence: ProviderCandidateFence | None
    runtime_identity: _AsrRuntimeIdentity
    has_pending_turn: bool
    audio_handoff_completed: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass(slots=True)
class _AdmissionResolutionExecution:
    ticket: AdmissionResolutionTicket
    core_settled: bool = False
    transport_settled: bool = False
    lifecycle_settled: bool = False
    core_resolution_succeeded: bool | None = None
    late_context: _AdmissionFinalContext | None = None
    owner_done: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass(slots=True)
class _AdmissionRejectionExecution:
    ticket: AdmissionOperationTicket
    absolute_deadline: float | None
    deadline_changed: asyncio.Event = field(
        default_factory=asyncio.Event,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class _ProviderTurnSealTransaction:
    lifecycle: VoiceInputLifecycleController
    turn_token: VoiceTurnToken
    sealed_token: VoiceTransportToken
    final_key: FinalKey
    identity: _AsrRuntimeIdentity


@dataclass(slots=True)
class _ProviderExactIntervalTransaction:
    provider_key: ProviderUtteranceKey
    turn_token: VoiceTurnToken
    parent_lease_token: SpeakerCaptureLeaseToken
    parent_candidate: SpeakerShadowCandidateKey
    target_candidate: SpeakerShadowCandidateKey
    successor_candidate: SpeakerShadowCandidateKey | None
    successor_evidence_lease: ProviderSpeakerEvidenceLease | None
    detector: DetectorRuntime
    reservation: ProviderExactSpeakerIntervalReservation
    promotion: ExactIntervalPromotionReceipt
    activation: ExactIntervalActivationReceipt
    proof: BoundaryProof
    snapshot: ProviderSpeakerBoundarySnapshot
    lifecycle: VoiceInputLifecycleController
    correlator: ProviderTurnCorrelator
    session: Any
    ingress_token: VoiceIngressToken
    runtime_identity: _AsrRuntimeIdentity
    sealed_token: VoiceTransportToken | None = None
    evidence_binding: ProviderEvidenceBinding | None = None
    observation_binding: ProviderEvidenceBinding | None = None
    audio_handoff_task: asyncio.Task[bool] | None = field(default=None, repr=False)
    resolved_disposition: AdmissionDisposition | None = None
    drop_tombstone_succeeded: bool | None = None
    event_queue: deque["_ProviderExactIntervalQueueItem"] = field(
        default_factory=deque,
        repr=False,
    )
    drain_task: asyncio.Task[None] | None = field(default=None, repr=False)
    accepted_events: set[Any] = field(default_factory=set, repr=False)
    completed_events: dict[Any, ExactIntervalTransitionReceipt | None] = field(
        default_factory=dict,
        repr=False,
    )
    queue_poisoned: bool = False


@dataclass(slots=True)
class _ProviderExactIntervalQueueItem:
    event: SpeakerLeaseEvent | ProviderFinalReceived
    waiters: list[asyncio.Future[ExactIntervalTransitionReceipt | None]] = field(
        default_factory=list,
        repr=False,
    )


class _ProviderSpeakerLedgerState(Enum):
    UNANCHORED_DEFERRED = "unanchored_deferred"
    ANCHORED_SCORING = "anchored_scoring"
    EXACT_PREPARING = "exact_preparing"
    EXACT_DRAINING = "exact_draining"
    UNAVAILABLE = "unavailable"
    RESOLVED = "resolved"


@dataclass(slots=True)
class _ProviderSpeakerProvisionalLedger:
    evidence_lease: ProviderSpeakerEvidenceLease
    runtime_identity: _AsrRuntimeIdentity
    activation_generation: str
    state: _ProviderSpeakerLedgerState = (
        _ProviderSpeakerLedgerState.UNANCHORED_DEFERRED
    )
    provider_key: ProviderUtteranceKey | None = None
    turn_token: VoiceTurnToken | None = None
    lease_token: SpeakerCaptureLeaseToken | None = None
    detector_epoch: int = -1
    timeline_generation: int = -1
    lease_generation: int = -1
    anchor_revision: int = -1
    anchor_start_sample_16k: int | None = None
    buffer_origin_sample_16k: int = -1
    observed_through_sample_16k: int = -1
    pcm_sequence_fence: int = 0
    last_pcm_sequence_no: int = 0
    last_speaker_sequence_no: int = 0
    events: deque[SpeakerLeaseEvent] = field(default_factory=deque, repr=False)
    event_by_sequence: dict[int, SpeakerLeaseEvent] = field(
        default_factory=dict,
        repr=False,
    )
    close_event: SpeakerLeaseCaptureClosed | None = None
    poisoned_reason: str | None = None
    # Physical retirement does not settle the Provider text/admission owner.
    # In particular, unanchored unavailable audio still belongs to this turn.
    evidence_turn_token: VoiceTurnToken | None = None

    @property
    def candidate(self) -> SpeakerShadowCandidateKey:
        return self.evidence_lease.candidate

    def poison(self, reason: str) -> bool:
        if self.poisoned_reason is not None:
            return False
        self.poisoned_reason = reason
        self.state = _ProviderSpeakerLedgerState.UNAVAILABLE
        return True


@dataclass(slots=True)
class _ProviderExactIntervalPending:
    boundary: Any
    completion: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    deferred: deque[Any] = field(default_factory=deque, repr=False)
    conflicted: bool = False


class _ProviderStartedOutcome(Enum):
    BOUND_ACTIVE = "bound_active"
    BOUND_PENDING = "bound_pending"
    BOUND_SPEAKER_UNAVAILABLE = "bound_speaker_unavailable"
    DENIED_SETTLED = "denied_settled"
    STALE = "stale"
    FAILED = "failed"

    @property
    def accepted(self) -> bool:
        return self in {
            _ProviderStartedOutcome.BOUND_ACTIVE,
            _ProviderStartedOutcome.BOUND_PENDING,
            _ProviderStartedOutcome.BOUND_SPEAKER_UNAVAILABLE,
            _ProviderStartedOutcome.DENIED_SETTLED,
        }


class _ProviderTurnOwnershipState(Enum):
    PROVISIONAL = "provisional"
    CHILD_BOUND = "child_bound"
    CORE_PREPARING = "core_preparing"
    CORE_READY = "core_ready"
    RESOLVED = "resolved"
    RETIRED = "retired"


class DenyTransportState(Enum):
    """Transport-scoped lifecycle after an authoritative speaker denial."""

    OPEN = "open"
    DENY_FENCED = "deny_fenced"
    RETIRING = "retiring"
    WAIT_SILENCE = "wait_silence"
    ARMED = "armed"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class DenyCleanupContext:
    """Immutable identity captured at the DENY fence linearization point."""

    lease_token: SpeakerCaptureLeaseToken
    candidate_key: SpeakerShadowCandidateKey
    session_epoch: int
    transport_generation: int
    start_generation: int
    detector_epoch: int
    session_ref: Any
    capture_cutoff_sequence: int
    terminal_speaker_sequence: int


@dataclass(frozen=True, slots=True)
class _ProviderCallbackTicket:
    session_epoch: int
    transport_generation: int
    ticket_no: int
    session_ref: Any


@dataclass(slots=True)
class _ProviderTurnOwnership:
    turn_token: VoiceTurnToken
    provider_key: ProviderUtteranceKey
    speaker_lease_token: SpeakerCaptureLeaseToken | None
    final_key: FinalKey
    transcript_dispatcher: TranscriptDispatcher
    lifecycle: VoiceInputLifecycleController
    correlator: ProviderTurnCorrelator
    session: Any
    runtime_identity: _AsrRuntimeIdentity
    state: _ProviderTurnOwnershipState = _ProviderTurnOwnershipState.PROVISIONAL
    child_published: bool = False


@dataclass(slots=True)
class _SpeakerDenyCleanupOperation:
    lease_token: SpeakerCaptureLeaseToken
    generation: int
    context: DenyCleanupContext
    runtime_identity: _AsrRuntimeIdentity
    lifecycle: VoiceInputLifecycleController | None
    session: Any
    correlator: ProviderTurnCorrelator | None
    namespace: tuple[int, int] | None
    detector: DetectorRuntime | None
    evidence_lease: ProviderSpeakerEvidenceLease | None
    audio_dispatcher: AsrAudioDispatcher
    audio_transport_generation: int
    frozen_children: tuple[Any, ...] = ()
    tickets: dict[VoiceTurnToken, AdmissionResolutionTicket] = field(
        default_factory=dict
    )
    provisional_reservations: dict[FinalKey, TranscriptDispatcher] = field(
        default_factory=dict
    )
    settled_tickets: set[AdmissionResolutionTicket] = field(default_factory=set)
    owner_task: asyncio.Task[Any] | None = None
    text_safe: bool = False
    transport_safe: bool = False
    fully_settled: bool = False
    failure_reason: str | None = None
    incident_id: str | None = None
    degraded: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class IndependentAsrRuntime:
    """Own one independent ASR session without reading Core manager state."""

    def __init__(
        self, callbacks: AsrRuntimeCallbacks, *, evidence_hold_enabled: bool = False,
        evidence_observation_enabled: bool = False,
    ) -> None:
        if type(evidence_hold_enabled) is not bool:
            raise TypeError("evidence_hold_enabled must be bool")
        self._asr_evidence_hold_enabled = evidence_hold_enabled
        if type(evidence_observation_enabled) is not bool:
            raise TypeError("evidence_observation_enabled must be bool")
        self._asr_evidence_observation_enabled = evidence_observation_enabled
        self._asr_evidence_observer: EvidenceObservationRegistry | None = None
        self._asr_evidence_observer_scope: tuple[int, int, int] | None = None
        self._callbacks = callbacks
        self._init_asr_runtime_state()

    @property
    def display_name(self) -> str:
        return self._callbacks.display_name()

    def _get_speaker_verifier_installation(self) -> SpeakerVerifierInstallation:
        component = getattr(self, "_speaker_verifier_installation", None)
        if component is None:
            component = SpeakerVerifierInstallation(self)
            self._speaker_verifier_installation = component
        return component

    def create_speaker_verifier_install_identity(
        self, manager_identity: int, route_generation: int, activation_revision: str
    ) -> SpeakerVerifierInstallIdentity:
        return self._get_speaker_verifier_installation().create_speaker_verifier_install_identity(
            manager_identity, route_generation, activation_revision
        )

    async def install_speaker_verifier(
        self, spec: SpeakerVerifierSpec | None, identity: SpeakerVerifierInstallIdentity
    ) -> SpeakerVerifierInstallReceipt:
        return await self._get_speaker_verifier_installation().install_speaker_verifier(
            spec, identity
        )

    def retire_speaker_verifier_authority(self) -> None:
        self._get_speaker_verifier_installation().retire_speaker_verifier_authority()

    def speaker_verifier_installation_permits_evidence(
        self, identity: SpeakerVerifierInstallIdentity
    ) -> bool:
        return self._get_speaker_verifier_installation().speaker_verifier_installation_permits_evidence(
            identity
        )

    def _speaker_install_identity_current(
        self, identity: SpeakerVerifierInstallIdentity
    ) -> bool:
        return self._get_speaker_verifier_installation()._speaker_install_identity_current(
            identity
        )

    def _accept_speaker_verifier_health(self, event: SpeakerVerifierHealthEvent) -> None:
        self._get_speaker_verifier_installation()._accept_speaker_verifier_health(event)

    def _speaker_exact_installation_is_current(self, transaction) -> bool:
        return self._get_speaker_verifier_installation()._speaker_exact_installation_is_current(
            transaction
        )

    async def _install_speaker_verifier_locked(
        self, spec: SpeakerVerifierSpec, identity: SpeakerVerifierInstallIdentity
    ) -> SpeakerVerifierInstallReceipt:
        return await self._get_speaker_verifier_installation()._install_speaker_verifier_locked(
            spec, identity
        )

    def _speaker_verifier_route_supported(self) -> bool:
        """Report whether the active route can enforce speaker admission.

        Smart/local endpointing retains its existing ownership model. A
        Provider-owned endpoint route is eligible only when its contract can
        publish a canonical 16 kHz start and an exact end interval.
        """

        self._ensure_asr_runtime_state()
        lifecycle = self._asr_lifecycle
        if lifecycle is None:
            return False
        policy = lifecycle.provider_policy
        return bool(
            policy.endpoint_authority != "provider"
            or policy.speaker_exact_interval_capability
            is AsrSpeakerExactIntervalCapability.CANONICAL_16K_EXACT_INTERVAL
        )

    def _record_unsupported_speaker_route(self) -> None:
        """Count one rejected verifier installation without route details."""

        self._ensure_asr_runtime_state()
        self._speaker_rejection_metrics["unsupported_asr_route_count"] += 1

    async def close(self) -> None:
        """Permanently dispose this runtime and its admission ingress."""

        self._ensure_asr_runtime_state()
        close_task = self._asr_terminal_close_task
        if close_task is None:
            self._asr_terminal_close_requested = True
            self._begin_asr_start_operation()
            close_task = asyncio.create_task(
                self._finish_terminal_asr_close(),
                name="independent-asr-terminal-close",
            )
            # stop_session() snapshots the ordinary owned-cleanup registry, so
            # the terminal owner must stay outside it to avoid waiting itself.
            close_task.add_done_callback(self._log_asr_background_task_failure)
            self._asr_terminal_close_task = close_task
        await asyncio.shield(close_task)

    async def stop_session(self) -> None:
        """Stop one ASR session while keeping the admission lane reusable."""

        self._ensure_asr_runtime_state()
        close_task = self._asr_runtime_close_task
        if close_task is None:
            # A session stop owns a different operation from start's detached
            # predecessor cleanup. Invalidate the in-flight start before
            # awaiting either cleanup, then wait for both under one explicit
            # latch so cancellation/retry retains the same owner.
            operation_generation = self._begin_asr_start_operation()
            predecessor_cleanups = tuple(self._asr_owned_cleanup_tasks)
            cleanup = self._detach_independent_asr(
                operation_generation=operation_generation,
            )
            cleanup_task = (
                self._schedule_owned_cleanup(
                    cleanup,
                    name="independent-asr-stop-session-detached",
                )
                if cleanup is not None
                else None
            )
            close_task = self._schedule_owned_cleanup(
                self._finish_explicit_asr_close(
                    predecessor_cleanups,
                    cleanup_task,
                ),
                name="independent-asr-stop-session",
            )
            self._asr_runtime_close_task = close_task
        await asyncio.shield(close_task)

    async def _finish_terminal_asr_close(self) -> None:
        """Bounded terminal drain followed by permanent hard-close fences."""

        deadline = time.monotonic() + _ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS
        drain_deadline = deadline - _ASR_TERMINAL_HARD_CLOSE_RESERVE_SECONDS
        stop_waiter = asyncio.create_task(
            self.stop_session(),
            name="independent-asr-terminal-stop-session",
        )
        stop_pending = await self._bounded_terminal_task_join(
            {stop_waiter},
            deadline=drain_deadline,
            label="session stop",
            cancel_first=False,
        )
        if (
            stop_pending
            or stop_waiter.cancelled()
            or any(not task.done() for task in self._asr_owned_cleanup_tasks)
        ):
            owned_cleanups = set(self._asr_owned_cleanup_tasks)
            await self._bounded_terminal_task_join(
                owned_cleanups,
                deadline=drain_deadline,
                label="owned cleanup",
                cancel_first=True,
            )

        await self._quiesce_terminal_admission_tasks(drain_deadline)

        transcript_dispatcher = self._asr_transcript_dispatcher
        detector_dispatcher = self._asr_detector_dispatcher
        audio_dispatcher = self._asr_audio_dispatcher
        transcript_dispatcher.invalidate_all()
        detector_dispatcher.invalidate_all()
        audio_dispatcher.abort()
        dispatcher_closes = {
            asyncio.create_task(
                detector_dispatcher.close(),
                name="independent-asr-terminal-detector-dispatcher-close",
            ),
            asyncio.create_task(
                audio_dispatcher.close(),
                name="independent-asr-terminal-audio-dispatcher-close",
            ),
        }
        self._track_terminal_close_tasks(dispatcher_closes)
        await self._bounded_terminal_task_join(
            dispatcher_closes,
            deadline=deadline,
            label="idle dispatcher close",
            cancel_first=False,
            cancel_on_timeout=False,
        )

        async def close_speaker_factory() -> None:
            async with self._speaker_verifier_lock:
                factory = self._speaker_verifier_factory
                self._speaker_verifier_factory = None
                self._speaker_verifier_activation_generation = None
                self._speaker_verifier_degraded = False
                if factory is not None:
                    self._close_speaker_verifier_factory(factory)

        speaker_close = asyncio.create_task(
            close_speaker_factory(),
            name="independent-asr-terminal-speaker-factory-close",
        )
        self._track_terminal_close_tasks({speaker_close})
        await self._bounded_terminal_task_join(
            {speaker_close},
            deadline=deadline,
            label="speaker factory close",
            cancel_first=False,
            cancel_on_timeout=False,
        )

        # A settlement may have scheduled one final producer before observing
        # the terminal generation. Re-snapshot without consuming hard-close
        # reserve, then publish the lane's permanent closing fence.
        await self._quiesce_terminal_admission_tasks(drain_deadline)
        if self._asr_admission_ingress_started:
            ingress_close = asyncio.create_task(
                self._asr_admission_ingress.close(),
                name="independent-asr-terminal-admission-ingress-close",
            )
            self._track_terminal_close_tasks({ingress_close})

            def finish_ingress_close(done: asyncio.Task[Any]) -> None:
                if not done.cancelled() and done.exception() is None:
                    self._asr_admission_ingress_started = False

            ingress_close.add_done_callback(finish_ingress_close)
            ingress_pending = await self._bounded_terminal_task_join(
                {ingress_close},
                deadline=deadline,
                label="admission ingress close",
                cancel_first=False,
                cancel_on_timeout=False,
            )
            if (
                not ingress_pending
                and not ingress_close.cancelled()
                and ingress_close.exception() is None
            ):
                self._asr_admission_ingress_started = False
            else:
                logger.warning(
                    "[%s] admission ingress terminal owner remains active",
                    self.display_name,
                )

    def _track_terminal_close_tasks(
        self,
        tasks: set[asyncio.Task[Any]],
    ) -> None:
        """Retain timed-out hard-close owners until their actual completion."""

        for task in tasks:
            self._asr_close_tasks.add(task)

            def reap(done: asyncio.Task[Any]) -> None:
                self._asr_close_tasks.discard(done)
                self._log_asr_background_task_failure(done)

            task.add_done_callback(reap)

    async def _bounded_terminal_task_join(
        self,
        tasks: set[asyncio.Task[Any]],
        *,
        deadline: float,
        label: str,
        cancel_first: bool,
        cancel_on_timeout: bool = True,
    ) -> set[asyncio.Task[Any]]:
        """Join one owned task set within the absolute terminal deadline."""

        current = asyncio.current_task()
        pending = {task for task in tasks if task is not current and not task.done()}
        if not pending:
            return set()
        owned = set(pending)
        if cancel_first:
            for task in pending:
                if task not in self._asr_terminal_cancel_requested_tasks:
                    self._asr_terminal_cancel_requested_tasks.add(task)
                    task.cancel()

        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0:
            _, pending = await asyncio.wait(
                pending,
                timeout=min(
                    remaining / 2,
                    _ASR_TERMINAL_CLOSE_JOIN_SLICE_SECONDS,
                ),
            )
        else:
            # This schedules fresh hard-close tasks once so their synchronous
            # ownership fence can publish before timeout cancellation.
            _, pending = await asyncio.wait(pending, timeout=0)
        if pending and not cancel_first and cancel_on_timeout:
            for task in pending:
                if task not in self._asr_terminal_cancel_requested_tasks:
                    self._asr_terminal_cancel_requested_tasks.add(task)
                    task.cancel()
        remaining = max(0.0, deadline - time.monotonic())
        if pending and remaining > 0:
            _, pending = await asyncio.wait(
                pending,
                timeout=min(
                    remaining,
                    _ASR_TERMINAL_CLOSE_JOIN_SLICE_SECONDS,
                ),
            )
        elif pending:
            _, pending = await asyncio.wait(pending, timeout=0)
        if pending:
            logger.warning(
                "[%s] independent ASR terminal %s exceeded %.1fs deadline",
                self.display_name,
                label,
                _ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS,
            )
        for task in owned - pending:
            self._log_asr_background_task_failure(task)
        return pending

    async def _quiesce_terminal_admission_tasks(self, deadline: float) -> None:
        """Cancel producers, then allow settlement owners to finish."""

        current = asyncio.current_task()
        producers = {
            task
            for task in (
                *tuple(self._asr_admission_candidate_owned_tasks),
                *tuple(self._asr_admission_deadline_tasks.values()),
            )
            if task is not current and not task.done()
        }
        pending_producers = await self._bounded_terminal_task_join(
            producers,
            deadline=deadline,
            label="admission producer drain",
            cancel_first=True,
        )
        completed_producers = producers - pending_producers
        self._asr_admission_candidate_owned_tasks.difference_update(completed_producers)
        for candidate, task in tuple(self._asr_admission_candidate_tasks.items()):
            if task in completed_producers:
                self._asr_admission_candidate_tasks.pop(candidate, None)
        for ticket, task in tuple(self._asr_admission_deadline_tasks.items()):
            if task in completed_producers:
                self._asr_admission_deadline_tasks.pop(ticket, None)

        settlements = {
            task
            for task in tuple(self._asr_admission_effect_tasks)
            if task is not current and not task.done()
        }
        pending_settlements = await self._bounded_terminal_task_join(
            settlements,
            deadline=deadline,
            label="admission settlement drain",
            cancel_first=False,
        )
        completed_settlements = settlements - pending_settlements
        self._asr_admission_effect_tasks.difference_update(completed_settlements)
        for task in completed_settlements:
            self._asr_admission_effect_task_turns.pop(task, None)

        late_producers = {
            task
            for task in (
                *tuple(self._asr_admission_candidate_owned_tasks),
                *tuple(self._asr_admission_deadline_tasks.values()),
            )
            if task is not current and not task.done()
        }
        pending_late_producers = await self._bounded_terminal_task_join(
            late_producers,
            deadline=deadline,
            label="late admission producer drain",
            cancel_first=True,
        )
        completed_late_producers = late_producers - pending_late_producers
        self._asr_admission_candidate_owned_tasks.difference_update(
            completed_late_producers
        )
        for candidate, task in tuple(self._asr_admission_candidate_tasks.items()):
            if task in completed_late_producers:
                self._asr_admission_candidate_tasks.pop(candidate, None)
        for ticket, task in tuple(self._asr_admission_deadline_tasks.items()):
            if task in completed_late_producers:
                self._asr_admission_deadline_tasks.pop(ticket, None)

    @staticmethod
    async def _finish_explicit_asr_close(
        predecessor_cleanups: tuple[asyncio.Task[Any], ...],
        cleanup_task: "asyncio.Task[Any] | None",
    ) -> None:
        """Join both teardowns; ``cleanup_task`` is already running."""

        if predecessor_cleanups:
            await asyncio.gather(
                *predecessor_cleanups,
                return_exceptions=True,
            )
        if cleanup_task is not None:
            # Awaited last but NOT started last, and awaited bare so its
            # failure still reaches the owned-cleanup logger.
            await cleanup_task

    async def set_speaker_verifier_factory(
        self,
        factory: SpeakerShadowFactory | None,
        *,
        activation_generation: str,
    ) -> bool:
        """Hot-replace Owner verification without restarting independent ASR."""

        if factory is not None and not callable(factory):
            raise TypeError("factory must be callable or None")
        if type(activation_generation) is not str or not activation_generation.strip():
            raise ValueError("activation_generation must be a non-empty string")
        self._ensure_asr_runtime_state()
        async with self._speaker_verifier_lock:
            if self._asr_terminal_close_requested:
                if factory is not None:
                    return False
                old_factory = self._speaker_verifier_factory
                self._speaker_verifier_factory = None
                self._speaker_verifier_activation_generation = activation_generation
                self._speaker_verifier_enforces_admission = False
                self._speaker_verifier_degraded = False
                if old_factory is not None:
                    self._close_speaker_verifier_factory(old_factory)
                return True
            return await self._set_speaker_verifier_factory_locked(
                factory,
                activation_generation=activation_generation,
            )

    async def _set_speaker_verifier_factory_locked(
        self,
        factory: SpeakerShadowFactory | None,
        *,
        activation_generation: str,
    ) -> bool:
        """Compatibility adapter; production uses typed installation receipts."""
        from .speaker_verifier_contracts import (
            SpeakerVerifierAuthority, SpeakerVerifierInstallOutcome,
            SpeakerVerifierSpec,
        )

        authority = SpeakerVerifierAuthority()
        authority.commit()
        identity = self.create_speaker_verifier_install_identity(
            id(self), self._asr_start_generation, activation_generation
        )
        spec = SpeakerVerifierSpec(
            activation_generation, activation_generation, factory is not None,
            _speaker_factory_enforces_admission(factory), authority,
            (lambda runtime, installation: factory) if factory is not None else None,
        )
        receipt = await self._install_speaker_verifier_locked(spec, identity)
        return receipt.outcome in {
            SpeakerVerifierInstallOutcome.INSTALLED,
            SpeakerVerifierInstallOutcome.REVOKED,
        }

    async def _revoke_runtime_speaker_authority_for_verifier_change(self) -> None:
        """Fence every optional speaker capability without revising text."""

        if not self._speaker_verifier_degraded:
            self._speaker_verifier_health_generation += 1
        self._speaker_verifier_degraded = True
        candidate_turns = tuple(self._asr_admission_candidate_turns.items())
        pending_turns = tuple(self._asr_speaker_authority_pending_turns.items())
        exact_turns = {
            transaction.turn_token
            for transaction in self._asr_provider_exact_intervals.values()
        }
        if self._asr_admission_ingress_started:
            for candidate, turn_token in candidate_turns:
                if turn_token in exact_turns:
                    continue
                try:
                    future = self._asr_admission_ingress.post_nowait(
                        turn_token,
                        SpeakerAuthorityUnavailable(candidate),
                    )
                except (AdmissionIngressClosedError, AdmissionIngressCapacityError):
                    continue
                self._consume_admission_future(turn_token, future)
            for turn_token, owner_generation in pending_turns:
                if turn_token in exact_turns:
                    continue
                try:
                    future = self._asr_admission_ingress.post_nowait(
                        turn_token,
                        SpeakerAuthorityUnarmed(owner_generation),
                    )
                except (AdmissionIngressClosedError, AdmissionIngressCapacityError):
                    continue
                self._consume_admission_future(turn_token, future)
        self._asr_admission_capability_generation += 1
        for owner in self._asr_admission_capabilities.values():
            owner.revoked = True
        self._asr_admission_capabilities.clear()
        self._asr_admission_candidate_turns.clear()
        self._asr_speaker_authority_pending_turns.clear()
        for turn_token in await self._asr_admission.live_turn_tokens():
            if turn_token in exact_turns:
                continue
            try:
                await self._post_admission_event(
                    turn_token,
                    BoundaryUnknown(),
                )
            except (AdmissionIngressClosedError, KeyError):
                continue

    def _accept_speaker_candidate_binding(
        self,
        candidate: SpeakerShadowCandidateKey,
        turn_token: VoiceTurnToken,
        *,
        detector: DetectorRuntime,
        activation_generation: str,
    ) -> bool:
        """Publish one stable candidate lease before any score can arrive."""

        lifecycle = self._asr_lifecycle
        sealed_token = self._asr_sealed_turn_token
        turn_is_current = bool(
            lifecycle is not None
            and lifecycle.snapshot.state
            in {VoiceLifecycleState.ACTIVE, VoiceLifecycleState.DRAINING}
            and (
                lifecycle.current_turn_token == turn_token
                or (sealed_token is not None and sealed_token.turn == turn_token)
            )
        )
        if (
            self._asr_terminal_close_requested
            or not self._speaker_verifier_enforces_admission
            or detector is not self._asr_detector
            or not self._asr_admission_ingress_started
            or activation_generation != self._speaker_verifier_activation_generation
            or candidate.detector_epoch != detector.detector_epoch
            or not self._ingress_token_matches(turn_token.ingress)
            or not turn_is_current
        ):
            return False
        provider_owns_turns = bool(
            lifecycle is not None
            and lifecycle.provider_policy.endpoint_authority == "provider"
        )
        if provider_owns_turns:
            lease_token = self._asr_admission_candidate_leases.get(candidate)
            return bool(
                lease_token is not None
                and self._asr_current_speaker_lease == lease_token
                and self._asr_current_speaker_candidate == candidate
                and self._asr_provider_speaker_evidence_lease is not None
                and self._asr_provider_speaker_evidence_lease.candidate == candidate
            )
        existing = self._asr_admission_candidate_turns.get(candidate)
        if existing is not None:
            if existing != turn_token:
                self._speaker_rejection_metrics[
                    "admission_candidate_alias_conflict_count"
                ] += 1
                return False
            return True
        self._asr_admission_candidate_turns[candidate] = turn_token
        self._asr_speaker_authoritative_turns.add(turn_token)
        self._asr_speaker_authority_pending_turns[turn_token] = activation_generation
        try:
            pending = self._asr_admission_ingress.post_nowait(
                turn_token,
                SpeakerAuthorityPending(activation_generation),
            )
        except (AdmissionIngressClosedError, AdmissionIngressCapacityError):
            if self._asr_admission_candidate_turns.get(candidate) == turn_token:
                self._asr_admission_candidate_turns.pop(candidate, None)
            if (
                self._asr_speaker_authority_pending_turns.get(turn_token)
                == activation_generation
            ):
                self._asr_speaker_authority_pending_turns.pop(turn_token, None)
            return False
        self._consume_admission_future(turn_token, pending)
        try:
            future = self._asr_admission_ingress.post_nowait(
                turn_token,
                CandidateBound(candidate, activation_generation),
            )
        except (AdmissionIngressClosedError, AdmissionIngressCapacityError):
            if self._asr_admission_candidate_turns.get(candidate) == turn_token:
                self._asr_admission_candidate_turns.pop(candidate, None)
            if (
                self._asr_speaker_authority_pending_turns.get(turn_token)
                == activation_generation
            ):
                self._asr_speaker_authority_pending_turns.pop(turn_token, None)
            try:
                unarmed = self._asr_admission_ingress.post_nowait(
                    turn_token,
                    SpeakerAuthorityUnarmed(activation_generation),
                )
            except (AdmissionIngressClosedError, AdmissionIngressCapacityError):
                return False
            self._consume_admission_future(turn_token, unarmed)
            return False
        self._consume_admission_future(turn_token, future)
        if (
            self._asr_speaker_authority_pending_turns.get(turn_token)
            == activation_generation
        ):
            self._asr_speaker_authority_pending_turns.pop(turn_token, None)
        self._schedule_speaker_admission_item(
            candidate,
            self._ensure_speaker_admission_capability(
                candidate,
                turn_token,
                activation_generation,
            ),
        )
        return True

    def _poison_provider_speaker_ledger(
        self,
        ledger: _ProviderSpeakerProvisionalLedger,
        reason: str,
    ) -> None:
        if ledger.poison(reason):
            self._speaker_rejection_metrics["speaker_ledger_poisoned_count"] += 1
            category = _speaker_failure_reason_category(reason)
            self._speaker_rejection_metrics[
                f"speaker_ledger_poisoned_reason_{category}_count"
            ] += 1
            self._speaker_rejection_metrics[
                f"speaker_unavailable_reason_{category}_count"
            ] += 1
            self._schedule_speaker_evidence_unavailable(
                ledger.runtime_identity,
                reason,
                activation_generation=ledger.activation_generation,
            )

    def _schedule_speaker_evidence_unavailable(
        self,
        identity: _AsrRuntimeIdentity,
        reason: str | None,
        *,
        activation_generation: str | None,
    ) -> None:
        """Notify evidence degradation once without changing ASR admission."""

        if (
            activation_generation is None
            or activation_generation != self._speaker_verifier_activation_generation
            or not self._speaker_verifier_enforces_admission
            or self._asr_terminal_close_requested
            or not self._runtime_identity_matches(identity)
        ):
            return
        previous = self._asr_speaker_degradation_incident
        if (
            previous is not None
            and previous.activation_generation == activation_generation
            and self._runtime_identity_matches(previous.identity)
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        reason_code = (reason or "ASR_SPEAKER_EVIDENCE_UNAVAILABLE").upper()
        if not reason_code.startswith("ASR_"):
            reason_code = "ASR_" + reason_code
        if _ASR_REASON_CODE_FULL_RE.fullmatch(reason_code) is None:
            reason_code = "ASR_SPEAKER_EVIDENCE_UNAVAILABLE"
        incident = _SpeakerEvidenceDegradation(
            identity, activation_generation, reason_code,
            f"asr-failure-{uuid.uuid4().hex}",
        )
        self._asr_speaker_degradation_incident = incident
        self._schedule_asr_incident_log(
            incident_id=incident.incident_id,
            reason_code=incident.reason_code,
            stage="evidence_unavailable",
            source_session_epoch=identity.session_epoch,
        )

        async def notify() -> None:
            if (
                self._asr_speaker_degradation_incident is not incident
                or activation_generation != self._speaker_verifier_activation_generation
                or not self._runtime_identity_matches(identity)
            ):
                return
            await self._send_asr_status(
                "ASR_SPEAKER_EVIDENCE_UNAVAILABLE",
                identity.provider or "unknown",
                session_epoch=identity.session_epoch,
                expected_identity=identity,
                reason_code=incident.reason_code,
                incident_id=incident.incident_id,
            )

        task = loop.create_task(notify(), name="asr-speaker-evidence-degraded")
        self._asr_owned_cleanup_tasks.add(task)
        task.add_done_callback(self._owned_cleanup_done)

    def _record_provider_provisional_speaker_event(
        self,
        ledger: _ProviderSpeakerProvisionalLedger,
        event: SpeakerLeaseEvent,
    ) -> bool:
        """Record one pre-exact fact without granting Admission authority."""

        if ledger.state in {
            _ProviderSpeakerLedgerState.EXACT_DRAINING,
            _ProviderSpeakerLedgerState.RESOLVED,
        }:
            return False
        if ledger.poisoned_reason is not None:
            return True
        if (
            ledger.state is _ProviderSpeakerLedgerState.UNANCHORED_DEFERRED
            and isinstance(event, (SpeakerLeaseLow, SpeakerLeaseHigh))
        ):
            # A score emitted before canonical started cannot prove anything
            # about the Provider utterance.  Acknowledge and discard it so an
            # obsolete pre-roll score cannot consume the post-anchor sequence
            # or become a formal Admission fact after exact promotion.
            self._speaker_rejection_metrics[
                "speaker_pre_anchor_fact_ignored_count"
            ] += 1
            return True
        if isinstance(event, SpeakerLeaseUnavailable):
            self._poison_provider_speaker_ledger(
                ledger,
                "speaker_evidence_unavailable",
            )
            return True
        if isinstance(event, SpeakerLeaseCaptureClosed):
            existing_close = ledger.close_event
            if existing_close is not None:
                if existing_close == event:
                    return True
                self._poison_provider_speaker_ledger(
                    ledger,
                    "conflicting_capture_close",
                )
                return True
            if event.candidate != ledger.candidate:
                self._poison_provider_speaker_ledger(
                    ledger,
                    "candidate_mismatch",
                )
                return True
            if event.through_sequence_no != ledger.last_speaker_sequence_no:
                self._poison_provider_speaker_ledger(
                    ledger,
                    "capture_sequence_gap",
                )
                return True
            ledger.close_event = event
            return True
        if not isinstance(event, (SpeakerLeaseLow, SpeakerLeaseHigh)):
            self._poison_provider_speaker_ledger(ledger, "invalid_event")
            return True
        if event.candidate != ledger.candidate:
            self._poison_provider_speaker_ledger(ledger, "candidate_mismatch")
            return True
        if ledger.close_event is not None:
            self._poison_provider_speaker_ledger(ledger, "fact_after_close")
            return True
        existing = ledger.event_by_sequence.get(event.sequence_no)
        if existing is not None:
            if existing != event:
                self._poison_provider_speaker_ledger(
                    ledger,
                    "conflicting_duplicate",
                )
            return True
        expected = ledger.last_speaker_sequence_no + 1
        if event.sequence_no != expected:
            self._poison_provider_speaker_ledger(
                ledger,
                "speaker_sequence_gap"
                if event.sequence_no > expected
                else "speaker_sequence_reorder",
            )
            return True
        if len(ledger.events) >= _MAX_PROVIDER_PROVISIONAL_SPEAKER_EVENTS:
            self._poison_provider_speaker_ledger(ledger, "ledger_capacity")
            return True
        ledger.events.append(event)
        ledger.event_by_sequence[event.sequence_no] = event
        ledger.last_speaker_sequence_no = event.sequence_no
        self._speaker_rejection_metrics["speaker_provisional_fact_count"] += 1
        return True

    def _accept_speaker_evidence_fact(
        self,
        fact: SpeakerLow | SpeakerHigh | SpeakerUnavailable,
        *,
        activation_generation: str,
        enforce: bool,
    ) -> bool:
        """Queue one ordered speaker fact; only the coordinator may resolve final."""

        self._ensure_asr_runtime_state()
        if self._asr_terminal_close_requested:
            return False
        if (
            not isinstance(fact, (SpeakerLow, SpeakerHigh, SpeakerUnavailable))
            or activation_generation != self._speaker_verifier_activation_generation
            or not self._speaker_verifier_enforces_admission
            or type(enforce) is not bool
        ):
            return False
        if not enforce:
            return True
        detector = self._asr_detector
        if detector is None or not self._asr_admission_ingress_started:
            return False
        candidate = fact.candidate
        exact = self._asr_provider_exact_candidates.get(candidate)
        if exact is not None:
            if exact.resolved_disposition is not None:
                return True
            self._schedule_speaker_fact_diagnostic(fact, exact)
            exact_fact: SpeakerLeaseEvent = (
                SpeakerLeaseLow(candidate, fact.sequence_no, fact.checkpoint_kind)
                if isinstance(fact, SpeakerLow)
                else SpeakerLeaseHigh(candidate, fact.sequence_no)
                if isinstance(fact, SpeakerHigh)
                else SpeakerLeaseUnavailable(candidate, fact.sequence_no)
            )
            self._schedule_exact_interval_event(exact, exact_fact)
            return True
        ledger = self._asr_provider_speaker_ledgers.get(candidate)
        if ledger is not None:
            self._schedule_speaker_fact_diagnostic(fact, ledger)
            provisional_fact: SpeakerLeaseEvent = (
                SpeakerLeaseLow(candidate, fact.sequence_no, fact.checkpoint_kind)
                if isinstance(fact, SpeakerLow)
                else SpeakerLeaseHigh(candidate, fact.sequence_no)
                if isinstance(fact, SpeakerHigh)
                else SpeakerLeaseUnavailable(candidate, fact.sequence_no)
            )
            return self._record_provider_provisional_speaker_event(
                ledger,
                provisional_fact,
            )
        lease_token = self._asr_admission_candidate_leases.get(candidate)
        if lease_token is not None:
            lease_fact = (
                SpeakerLeaseLow(
                    candidate,
                    fact.sequence_no,
                    fact.checkpoint_kind,
                )
                if isinstance(fact, SpeakerLow)
                else SpeakerLeaseHigh(candidate, fact.sequence_no)
                if isinstance(fact, SpeakerHigh)
                else SpeakerLeaseUnavailable(candidate, fact.sequence_no)
            )
            return self._post_or_defer_provider_speaker_lease_event(
                lease_token,
                lease_fact,
            )
        turn_token = self._asr_admission_candidate_turns.get(candidate)
        if turn_token is None:
            turn_token = detector._bound_turn_token_for_speaker_candidate(candidate)
            if turn_token is None or not self._accept_speaker_candidate_binding(
                candidate,
                turn_token,
                detector=detector,
                activation_generation=activation_generation,
            ):
                return False
        try:
            future = self._asr_admission_ingress.post_nowait(turn_token, fact)
        except AdmissionIngressClosedError:
            return False
        self._schedule_pipeline_event(
            "speaker_fact_observed", turn_token.ingress, turn_id=turn_token.turn_id,
            outcome="low" if isinstance(fact, SpeakerLow) else "high" if isinstance(fact, SpeakerHigh) else "unavailable",
            sequence_no=fact.sequence_no,
        )
        self._consume_admission_future(turn_token, future)
        return True

    def _close_speaker_evidence(
        self,
        closed: CaptureClosed,
        *,
        activation_generation: str,
        enforce: bool,
        evidence_complete: bool,
    ) -> bool:
        """Queue capture close behind every observation for this candidate."""

        self._ensure_asr_runtime_state()
        if (
            type(closed) is not CaptureClosed
            or activation_generation != self._speaker_verifier_activation_generation
            or not self._speaker_verifier_enforces_admission
            or type(enforce) is not bool
            or type(evidence_complete) is not bool
        ):
            return False
        if not enforce:
            return True
        exact = self._asr_provider_exact_candidates.get(closed.candidate)
        if exact is not None:
            if exact.resolved_disposition is not None:
                return True
            self._schedule_exact_interval_event(
                exact,
                SpeakerLeaseCaptureClosed(
                    closed.candidate,
                    closed.through_sequence_no,
                ),
            )
            return True
        ledger = self._asr_provider_speaker_ledgers.get(closed.candidate)
        if ledger is not None:
            return self._record_provider_provisional_speaker_event(
                ledger,
                SpeakerLeaseCaptureClosed(
                    closed.candidate,
                    closed.through_sequence_no,
                ),
            )
        lease_token = self._asr_admission_candidate_leases.get(closed.candidate)
        if lease_token is not None:
            if not self._asr_admission_ingress_started:
                return False
            return self._post_or_defer_provider_speaker_lease_event(
                lease_token,
                SpeakerLeaseCaptureClosed(
                    closed.candidate,
                    closed.through_sequence_no,
                ),
            )
        turn_token = self._asr_admission_candidate_turns.get(closed.candidate)
        if turn_token is None or not self._asr_admission_ingress_started:
            return False
        try:
            future = self._asr_admission_ingress.post_nowait(turn_token, closed)
        except AdmissionIngressClosedError:
            return False
        self._consume_admission_future(turn_token, future)
        detector = self._asr_detector
        if detector is not None:
            detector.release_speaker_candidate_binding(
                closed.candidate,
                turn_token,
            )
        if self._asr_admission_candidate_turns.get(closed.candidate) == turn_token:
            self._asr_admission_candidate_turns.pop(closed.candidate, None)
        return True

    def _post_or_defer_provider_speaker_lease_event(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        event: SpeakerLeaseEvent,
    ) -> bool:
        """Fence decisive DENY before publishing it to the coordinator."""

        decisive_deny = bool(
            isinstance(event, SpeakerLeaseLow)
            and event.checkpoint_kind
            in {
                SpeakerCheckpointKind.SECOND,
                SpeakerCheckpointKind.COMPLETION_CONFIRMATION,
            }
        )
        cleanup_owner: _SpeakerDenyCleanupOperation | None = None
        if decisive_deny:
            try:
                cleanup_owner = self._begin_speaker_deny_cleanup(
                    lease_token,
                    (),
                    candidate_key=event.candidate,
                    terminal_sequence=event.sequence_no,
                )
            except Exception:
                self._schedule_speaker_deny_cleanup_failure(
                    lease_token,
                    "ASR_DENY_CLEANUP_IDENTITY_MISMATCH",
                )
                return False

        lifecycle = self._asr_lifecycle
        provider_parent_without_child = bool(
            lifecycle is not None
            and lifecycle.provider_policy.endpoint_authority == "provider"
            and lease_token not in self._asr_admission_turn_leases.values()
        )
        publish_deferred_drop = False
        if provider_parent_without_child and not decisive_deny:
            if lease_token in self._asr_deferred_provider_speaker_lease_overflow:
                return False
            pending = self._asr_deferred_provider_speaker_lease_events.setdefault(
                lease_token,
                deque(),
            )
            if pending and pending[-1] == event:
                return True
            if len(pending) >= _MAX_DEFERRED_PROVIDER_SPEAKER_LEASE_EVENTS:
                pending.clear()
                self._asr_deferred_provider_speaker_lease_overflow.add(lease_token)
                return False
            pending.append(event)
            publish_deferred_drop = (
                self._provider_speaker_lease_has_deferred_drop_event(
                    lease_token
                )
            )
            if not publish_deferred_drop:
                return True
        if decisive_deny or publish_deferred_drop:
            pending = self._asr_deferred_provider_speaker_lease_events.pop(
                lease_token,
                deque(),
            )
            while pending:
                prior = pending.popleft()
                try:
                    prior_future = (
                        self._asr_admission_ingress.prepare_speaker_lease_transition_nowait(
                            lease_token,
                            prior,
                        )
                    )
                except (
                    AdmissionIngressClosedError,
                    AdmissionIngressCapacityError,
                    KeyError,
                ):
                    self._schedule_speaker_deny_cleanup_failure(
                        lease_token,
                        "ASR_DENY_CLEANUP_ADMISSION_PUBLISH_FAILED",
                    )
                    return False
                self._consume_speaker_lease_future(
                    lease_token,
                    prior_future,
                    expected_identity=(
                        None
                        if cleanup_owner is not None
                        else self._capture_runtime_identity()
                    ),
                    cleanup_owner=cleanup_owner,
                    requires_terminal=False,
                )
            if publish_deferred_drop:
                return True
        expected_identity = (
            None if cleanup_owner is not None else self._capture_runtime_identity()
        )
        try:
            future = (
                self._asr_admission_ingress.prepare_speaker_lease_transition_nowait(
                    lease_token,
                    event,
                )
            )
        except (
            AdmissionIngressClosedError,
            AdmissionIngressCapacityError,
            KeyError,
        ):
            if cleanup_owner is not None:
                self._schedule_speaker_deny_cleanup_failure(
                    lease_token,
                    "ASR_DENY_CLEANUP_ADMISSION_PUBLISH_FAILED",
                )
            return False
        self._consume_speaker_lease_future(
            lease_token,
            future,
            expected_identity=expected_identity,
            cleanup_owner=cleanup_owner,
            requires_terminal=cleanup_owner is not None,
        )
        return True

    def _schedule_exact_interval_event(
        self,
        transaction: _ProviderExactIntervalTransaction,
        event: SpeakerLeaseEvent,
    ) -> None:
        self._enqueue_exact_interval_event(transaction, event)

    @staticmethod
    def _exact_interval_event_sequence(
        event: SpeakerLeaseEvent | ProviderFinalReceived,
    ) -> tuple[str, int] | None:
        if isinstance(event, ProviderFinalReceived):
            return ("final", 0)
        if isinstance(event, SpeakerLeaseCaptureClosed):
            # A close carries the last fact sequence it covers; it is not a
            # second fact at that sequence. Keep its idempotence/conflict key
            # separate from LOW/HIGH/UNAVAILABLE.
            return ("close", event.through_sequence_no)
        sequence = getattr(
            event,
            "sequence_no",
            getattr(event, "through_sequence_no", None),
        )
        if type(sequence) is int:
            return ("speaker", sequence)
        return None

    def _enqueue_exact_interval_event(
        self,
        transaction: _ProviderExactIntervalTransaction,
        event: SpeakerLeaseEvent | ProviderFinalReceived,
        *,
        waiter: asyncio.Future[ExactIntervalTransitionReceipt | None] | None = None,
    ) -> bool:
        """Append to one bounded exact FIFO and elect exactly one drain owner."""

        if (
            transaction.resolved_disposition is not None
            or not self._exact_interval_evidence_owner_is_current(transaction)
        ):
            if waiter is not None and not waiter.done():
                waiter.set_result(None)
            return False
        completed = transaction.completed_events.get(event)
        if event in transaction.completed_events:
            if waiter is not None and not waiter.done():
                waiter.set_result(completed)
            return True
        for item in transaction.event_queue:
            if item.event == event:
                if waiter is not None:
                    item.waiters.append(waiter)
                return True
        sequence_key = self._exact_interval_event_sequence(event)
        if sequence_key is not None:
            for accepted in transaction.accepted_events:
                if (
                    self._exact_interval_event_sequence(accepted) == sequence_key
                    and accepted != event
                ):
                    transaction.queue_poisoned = True
                    break
        if len(transaction.event_queue) >= _MAX_PROVIDER_EXACT_TRANSACTION_EVENTS:
            transaction.queue_poisoned = True
            if waiter is not None and not waiter.done():
                waiter.set_result(None)
            return False
        transaction.accepted_events.add(event)
        item = _ProviderExactIntervalQueueItem(event)
        if waiter is not None:
            item.waiters.append(waiter)
        transaction.event_queue.append(item)
        owner = transaction.drain_task
        if owner is None or owner.done():
            owner = asyncio.create_task(
                self._drain_exact_interval_events(transaction),
                name=(
                    "provider-exact-interval-drain-"
                    f"{transaction.provider_key.utterance_id}"
                ),
            )
            transaction.drain_task = owner
            self._track_admission_effect_task(owner, transaction.turn_token)
            owner.add_done_callback(self._admission_effect_done)
        return True

    async def _drain_exact_interval_events(
        self,
        transaction: _ProviderExactIntervalTransaction,
    ) -> None:
        """Serialize exact facts, close, and final under one task owner."""

        item: _ProviderExactIntervalQueueItem | None = None
        try:
            while transaction.event_queue:
                item = transaction.event_queue.popleft()
                if (
                    not isinstance(item.event, (EvidenceDeadlineExpired, FinalDeadlineExpired))
                    and not self._speaker_exact_installation_is_current(transaction)
                ):
                    receipt = None
                    fail_open_finals = [
                        queued.event
                        for queued in transaction.event_queue
                        if isinstance(queued.event, ProviderFinalReceived)
                    ]
                    if isinstance(item.event, ProviderFinalReceived):
                        fail_open_finals.insert(0, item.event)
                    failed_open = await self._fail_exact_interval_unavailable(
                        transaction,
                        "ASR_EXACT_INTERVAL_QUEUE_POISONED",
                    )
                    if failed_open:
                        # Retiring the exact aliases clears the remaining FIFO.
                        # A final may already have been accepted behind the
                        # poisoned fact; replay it through the now-ordinary
                        # UNAVAILABLE child so its reserved transcript and
                        # settlement event cannot be orphaned.
                        for final_event in fail_open_finals[:1]:
                            await self._post_admission_event(
                                transaction.turn_token,
                                final_event,
                            )
                else:
                    receipt = await self._apply_exact_interval_event(
                        transaction,
                        item.event,
                    )
                transaction.completed_events[item.event] = receipt
                for waiter in item.waiters:
                    if not waiter.done():
                        waiter.set_result(receipt)
                if transaction.resolved_disposition is not None:
                    break
        finally:
            if item is not None:
                # None means settlement was interrupted, never permission to
                # replay an input that Admission may already have applied.
                transaction.completed_events.setdefault(item.event, None)
                for waiter in item.waiters:
                    if not waiter.done():
                        waiter.set_result(None)
            while transaction.event_queue:
                item = transaction.event_queue.popleft()
                for waiter in item.waiters:
                    if not waiter.done():
                        waiter.set_result(None)

    async def _fail_exact_interval_unavailable(
        self,
        transaction: _ProviderExactIntervalTransaction,
        reason_code: str,
    ) -> bool:
        """Fail open an exact hold while preserving every formal DENY."""

        try:
            result = await self._asr_admission_ingress.fail_exact_interval_unavailable(
                transaction.activation
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            result = None
        if (
            result is not None
            and result.outcome is ExactIntervalOutcome.ABORTED
            and self._exact_interval_runtime_is_current(transaction)
        ):
            transaction.resolved_disposition = AdmissionDisposition.FORWARD
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            category = _speaker_failure_reason_category(reason_code)
            self._speaker_rejection_metrics[
                f"speaker_unavailable_reason_{category}_count"
            ] += 1
            partial_settlement = asyncio.create_task(
                self._execute_admission_effect(
                    SettlePartial(
                        turn_token=transaction.turn_token,
                        record_generation=(
                            transaction.promotion.scope.child_record_generation
                        ),
                        disposition=AdmissionDisposition.FORWARD,
                    )
                ),
                name=(
                    "provider-exact-unavailable-partial-"
                    f"{transaction.provider_key.utterance_id}"
                ),
            )
            self._track_exact_callback_task(partial_settlement)
            try:
                await self._wait_exact_callback_task(partial_settlement)
            except TimeoutError:
                # Preview delivery is optional; its timeout cannot revoke the
                # already established unavailable disposition of this turn.
                pass
            finally:
                self._retire_exact_interval_runtime_aliases(transaction)
            return True
        if self._exact_interval_runtime_is_current(transaction):
            await self._fail_exact_interval_group(transaction, reason_code)
        return False

    async def _fail_exact_interval_group(
        self,
        transaction: _ProviderExactIntervalTransaction,
        reason_code: str,
        *,
        ticket: AdmissionResolutionTicket | None = None,
    ) -> None:
        if not self._exact_interval_runtime_is_current(transaction):
            return
        self._start_exact_parent_cleanup(
            transaction.parent_lease_token,
            reason_code,
            turn_token=transaction.turn_token,
            ticket=ticket,
        )

    def _start_exact_parent_cleanup(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        reason_code: str,
        *,
        turn_token: VoiceTurnToken | None = None,
        ticket: AdmissionResolutionTicket | None = None,
    ) -> None:
        try:
            cleanup = self._begin_speaker_deny_cleanup(
                lease_token,
                (),
                candidate_key=self._asr_current_speaker_candidate,
                terminal_sequence=max(1, self._asr_provider_speaker_sequence),
            )
        except Exception:
            self._schedule_speaker_deny_cleanup_failure(
                lease_token,
                reason_code,
            )
            return
        if ticket is not None and turn_token is not None:
            cleanup.tickets.setdefault(turn_token, ticket)
        if cleanup.owner_task is None and not cleanup.settled.is_set():
            cleanup.owner_task = asyncio.create_task(
                self._finish_speaker_deny_cleanup(cleanup),
                name=(
                    "provider-exact-fallback-"
                    f"{lease_token.lease_nonce}"
                ),
            )
            self._track_admission_effect_task(cleanup.owner_task, None)
            cleanup.owner_task.add_done_callback(self._admission_effect_done)

    async def _post_exact_interval_event(
        self,
        transaction: _ProviderExactIntervalTransaction,
        event: SpeakerLeaseEvent | ProviderFinalReceived,
    ) -> ExactIntervalTransitionReceipt | None:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ExactIntervalTransitionReceipt | None] = (
            loop.create_future()
        )
        if not self._enqueue_exact_interval_event(
            transaction,
            event,
            waiter=waiter,
        ):
            return None
        return await asyncio.shield(waiter)

    async def _apply_exact_interval_event(
        self,
        transaction: _ProviderExactIntervalTransaction,
        event: SpeakerLeaseEvent | ProviderFinalReceived,
    ) -> ExactIntervalTransitionReceipt | None:
        if (
            transaction.resolved_disposition is not None
            or not self._exact_interval_evidence_owner_is_current(transaction)
        ):
            return None
        try:
            future = self._asr_admission_ingress.post_exact_interval_nowait(
                transaction.activation,
                event,
                authority_is_current=lambda: self._speaker_exact_installation_is_current(transaction),
            )
        except Exception:
            await self._fail_exact_interval_group(
                transaction,
                "ASR_EXACT_INTERVAL_POST_FAILED",
            )
            return None
        cancelled: asyncio.CancelledError | None = None
        try:
            receipt = await asyncio.shield(future)
        except asyncio.CancelledError as exc:
            cancelled = exc
            try:
                receipt = await asyncio.shield(future)
            except Exception:
                receipt = None
        except Exception:
            receipt = None
        if not self._exact_interval_evidence_owner_is_current(transaction):
            if cancelled is not None:
                raise cancelled
            return None
        if not isinstance(receipt, ExactIntervalTransitionReceipt) or receipt.outcome in {
            ExactIntervalOutcome.STALE,
            ExactIntervalOutcome.CONFLICT,
        }:
            await self._fail_exact_interval_group(
                transaction,
                "ASR_EXACT_INTERVAL_TRANSITION_CONFLICT",
            )
            if cancelled is not None:
                raise cancelled
            return None
        if receipt.outcome is ExactIntervalOutcome.HELD:
            await self._execute_exact_admission_effects(receipt.effects)
            if (
                not isinstance(event, (EvidenceDeadlineExpired, FinalDeadlineExpired))
                and not self._speaker_exact_installation_is_current(transaction)
            ):
                pending_finals = (
                    [event] if isinstance(event, ProviderFinalReceived) else [
                        queued.event for queued in transaction.event_queue
                        if isinstance(queued.event, ProviderFinalReceived)
                    ]
                )
                failed_open = await self._fail_exact_interval_unavailable(
                    transaction, "ASR_SPEAKER_INSTALLATION_RETIRED"
                )
                if failed_open:
                    for final_event in pending_finals[:1]:
                        await self._post_admission_event(transaction.turn_token, final_event)
            if cancelled is not None:
                raise cancelled
            return receipt
        if (
            receipt.outcome is not ExactIntervalOutcome.RESOLVED
            or receipt.disposition
            not in {AdmissionDisposition.FORWARD, AdmissionDisposition.DROP}
        ):
            await self._fail_exact_interval_group(
                transaction,
                "ASR_EXACT_INTERVAL_TRANSITION_CONFLICT",
            )
            if cancelled is not None:
                raise cancelled
            return None

        try:
            await self._execute_exact_admission_effects(receipt.effects)
        except Exception:
            await self._fail_exact_interval_group(
                transaction,
                "ASR_EXACT_INTERVAL_EFFECT_FAILED",
            )
            if cancelled is not None:
                raise cancelled
            return None
        if cancelled is not None:
            raise cancelled
        return receipt

    async def _execute_exact_admission_effects(
        self, effects: tuple[AdmissionEffect, ...],
    ) -> None:
        """Attribute a released ordering barrier to each effect's own key."""
        for effect in effects:
            transaction = None
            ownership = None
            if isinstance(effect, ResolveReserved):
                transaction = next(
                    (item for item in self._asr_provider_exact_intervals.values()
                     if item.turn_token == effect.ticket.turn_token), None,
                )
                if transaction is not None:
                    if transaction.activation.child_record_generation != effect.ticket.record_generation:
                        continue
                    record = await self._asr_admission.get_record(effect.ticket.turn_token)
                    if (
                        not self._exact_interval_evidence_owner_is_current(transaction)
                        or record is None or record.resolution_ticket != effect.ticket
                    ):
                        continue
                    ownership = self._asr_provider_turn_ownerships.get(transaction.turn_token)
            try:
                execution = await self._execute_admission_effect(effect)
            except asyncio.CancelledError:
                raise
            except Exception:
                if transaction is None:
                    raise
                await self._fail_exact_interval_group(
                    transaction, "ASR_EXACT_INTERVAL_EFFECT_FAILED", ticket=effect.ticket,
                )
                continue
            if transaction is None:
                continue
            # Completion may legitimately remove old correlator/started aliases,
            # but never authorizes an old receipt against a replacement owner.
            if (
                not self._runtime_identity_matches(replace(transaction.runtime_identity, turn_token=None))
                or self._asr_provider_exact_intervals.get(transaction.provider_key) is not transaction
                or execution is None or execution.ticket != effect.ticket
                or not execution.owner_done or not execution.core_resolution_succeeded
            ):
                continue
            transaction.resolved_disposition = effect.disposition
            if effect.disposition is AdmissionDisposition.DROP:
                if transaction.drop_tombstone_succeeded is not True:
                    transaction.resolved_disposition = None
                    await self._fail_exact_interval_group(
                        transaction, "ASR_EXACT_INTERVAL_DROP_UNSAFE", ticket=effect.ticket,
                    )
                    continue
                if (
                    ownership is not None
                    and self._asr_provider_turn_ownerships.get(transaction.turn_token) is ownership
                ):
                    self._retire_provider_turn_ownership(ownership)
            self._retire_exact_interval_runtime_aliases(transaction)

    def _provider_speaker_lease_has_deferred_terminal_event(
        self,
        lease_token: SpeakerCaptureLeaseToken,
    ) -> bool:
        if lease_token in self._asr_provider_speaker_terminal_leases:
            return True
        saw_high = False
        saw_first_low = False
        for event in self._asr_deferred_provider_speaker_lease_events.get(
            lease_token,
            (),
        ):
            if isinstance(
                event,
                (SpeakerLeaseUnavailable, SpeakerLeaseCaptureClosed),
            ):
                return True
            if isinstance(event, SpeakerLeaseHigh):
                if saw_first_low:
                    return True
                saw_high = True
                continue
            if not isinstance(event, SpeakerLeaseLow):
                continue
            if saw_high:
                return True
            if event.checkpoint_kind in {
                SpeakerCheckpointKind.SECOND,
                SpeakerCheckpointKind.COMPLETION_CONFIRMATION,
            }:
                return True
            saw_first_low = True
        return False

    def _provider_speaker_lease_has_deferred_drop_event(
        self,
        lease_token: SpeakerCaptureLeaseToken,
    ) -> bool:
        saw_high = False
        saw_first_low = False
        for event in self._asr_deferred_provider_speaker_lease_events.get(
            lease_token,
            (),
        ):
            if isinstance(event, SpeakerLeaseHigh):
                if saw_first_low:
                    return True
                saw_high = True
                continue
            if not isinstance(event, SpeakerLeaseLow):
                continue
            if saw_high:
                return True
            if event.checkpoint_kind is SpeakerCheckpointKind.FIRST:
                saw_first_low = True
                continue
            if saw_first_low:
                return True
        return False

    def _consume_admission_future(
        self,
        turn_token: VoiceTurnToken,
        future: asyncio.Future[tuple[AdmissionEffect, ...]],
        *,
        suppress_terminal_errors: bool = True,
    ) -> asyncio.Task[tuple[AdmissionEffect, ...]]:
        """Execute effects owned by one synchronously queued ingress item."""

        async def consume() -> tuple[AdmissionEffect, ...]:
            try:
                effects = await asyncio.shield(future)
            except (AdmissionIngressClosedError, KeyError):
                if suppress_terminal_errors:
                    return ()
                raise

            async def execute_effects() -> None:
                await self._execute_exact_admission_effects(effects)

            if effects:
                task = asyncio.create_task(
                    execute_effects(),
                    name="voice-turn-admission-effects",
                )
                self._track_admission_effect_task(task, turn_token)
                task.add_done_callback(self._admission_effect_done)
            try:
                await self._asr_admission_ingress.retire_turn(turn_token)
            except AdmissionIngressClosedError:
                if not suppress_terminal_errors:
                    raise
            return effects

        task = asyncio.create_task(
            consume(),
            name=f"voice-turn-admission-ingress-{turn_token.turn_id}",
        )
        self._track_admission_effect_task(task, turn_token)
        task.add_done_callback(self._admission_effect_done)
        return task

    def _consume_speaker_lease_future(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        future: asyncio.Future[Any],
        *,
        expected_identity: _AsrRuntimeIdentity | None = None,
        cleanup_owner: _SpeakerDenyCleanupOperation | None = None,
        requires_terminal: bool = False,
    ) -> asyncio.Task[Any]:
        """Execute one lease-control result without retiring a child turn."""

        async def consume() -> Any:
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError:
                if cleanup_owner is not None:
                    self._schedule_speaker_deny_cleanup_failure(
                        lease_token,
                        "ASR_DENY_CLEANUP_ADMISSION_PUBLISH_FAILED",
                    )
                raise
            except Exception:
                if cleanup_owner is not None:
                    await self._fail_speaker_deny_cleanup(
                        cleanup_owner,
                        "ASR_DENY_CLEANUP_ADMISSION_PUBLISH_FAILED",
                    )
                return None
            return await self._apply_prepared_speaker_lease_transition(
                lease_token,
                result,
                expected_identity=expected_identity,
                cleanup_owner=cleanup_owner,
                requires_terminal=requires_terminal,
            )

        task = asyncio.create_task(
            consume(),
            name=f"speaker-capture-lease-{lease_token.lease_nonce}",
        )
        self._track_admission_effect_task(task, None)
        task.add_done_callback(self._admission_effect_done)
        return task

    async def _apply_prepared_speaker_lease_transition(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        prepared: Any,
        *,
        expected_identity: _AsrRuntimeIdentity | None,
        cleanup_owner: _SpeakerDenyCleanupOperation | None,
        requires_terminal: bool = False,
    ) -> SpeakerLeaseTransitionReceipt | None:
        if isinstance(prepared, SpeakerLeaseTransitionReceipt):
            await self._apply_speaker_lease_result(lease_token, prepared)
            if cleanup_owner is not None and requires_terminal and not (
                prepared.lease_token == lease_token
                and prepared.after_state
                in {
                    SpeakerLeaseState.DENY_LATCHED,
                    SpeakerLeaseState.MIXED_DENY_LATCHED,
                }
                and prepared.outcome
                in {
                    SpeakerLeaseTransitionOutcome.APPLIED,
                    SpeakerLeaseTransitionOutcome.IDEMPOTENT,
                }
            ):
                await self._fail_speaker_deny_cleanup(
                    cleanup_owner,
                    "ASR_DENY_CLEANUP_TERMINAL_CONFLICT",
                )
                return None
            return prepared
        if not isinstance(prepared, SpeakerLeaseTerminalClaim):
            if cleanup_owner is not None:
                await self._fail_speaker_deny_cleanup(
                    cleanup_owner,
                    "ASR_DENY_CLEANUP_TERMINAL_CONFLICT",
                )
            return None

        event = prepared.event
        candidate = getattr(event, "candidate", None)
        terminal_sequence = getattr(
            event,
            "through_sequence_no",
            getattr(event, "sequence_no", None),
        )
        cleanup = cleanup_owner
        if cleanup is None:
            if (
                expected_identity is None
                or not self._runtime_identity_matches(expected_identity)
                or self._asr_current_speaker_lease != lease_token
            ):
                return None
            try:
                cleanup = self._begin_speaker_deny_cleanup(
                    lease_token,
                    (),
                    candidate_key=candidate,
                    terminal_sequence=terminal_sequence,
                )
            except Exception:
                self._schedule_speaker_deny_cleanup_failure(
                    lease_token,
                    "ASR_DENY_CLEANUP_IDENTITY_MISMATCH",
                )
                return None
        else:
            try:
                cleanup = self._begin_speaker_deny_cleanup(
                    lease_token,
                    (),
                    candidate_key=candidate,
                    terminal_sequence=terminal_sequence,
                )
            except Exception:
                await self._fail_speaker_deny_cleanup(
                    cleanup,
                    "ASR_DENY_CLEANUP_TERMINAL_CONFLICT",
                )
                return None

        try:
            commit_future = (
                self._asr_admission_ingress.commit_speaker_lease_terminal_claim_nowait(
                    prepared
                )
            )
        except Exception:
            await self._fail_speaker_deny_cleanup(
                cleanup,
                "ASR_DENY_CLEANUP_ADMISSION_PUBLISH_FAILED",
            )
            return None
        try:
            result = await asyncio.shield(commit_future)
        except asyncio.CancelledError:
            self._schedule_speaker_deny_cleanup_failure(
                lease_token,
                "ASR_DENY_CLEANUP_ADMISSION_PUBLISH_FAILED",
            )
            raise
        except Exception:
            await self._fail_speaker_deny_cleanup(
                cleanup,
                "ASR_DENY_CLEANUP_ADMISSION_PUBLISH_FAILED",
            )
            return None
        if (
            not isinstance(result, SpeakerLeaseTransitionReceipt)
            or result.lease_token != lease_token
            or result.after_state
            not in {
                SpeakerLeaseState.DENY_LATCHED,
                SpeakerLeaseState.MIXED_DENY_LATCHED,
            }
            or result.outcome
            not in {
                SpeakerLeaseTransitionOutcome.APPLIED,
                SpeakerLeaseTransitionOutcome.IDEMPOTENT,
            }
        ):
            await self._fail_speaker_deny_cleanup(
                cleanup,
                "ASR_DENY_CLEANUP_TERMINAL_CONFLICT",
            )
            return None
        await self._apply_speaker_lease_result(lease_token, result)
        return result

    async def _apply_speaker_lease_result(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        result: Any,
    ) -> None:
        if not isinstance(result, SpeakerLeaseTransitionReceipt):
            return
        bulk_results = result.child_results
        formal_deny = bool(
            result.lease_token == lease_token
            and result.after_state
            in {
                SpeakerLeaseState.DENY_LATCHED,
                SpeakerLeaseState.MIXED_DENY_LATCHED,
            }
            and result.outcome
            in {
                SpeakerLeaseTransitionOutcome.APPLIED,
                SpeakerLeaseTransitionOutcome.IDEMPOTENT,
            }
        )
        effects = (
            *result.diagnostics,
            *(effect for item in bulk_results for effect in item.effects),
        )
        if formal_deny:
            aborts = tuple(
                effect for effect in effects if isinstance(effect, AbortProviderTransport)
            )
            cleanup = self._asr_speaker_deny_cleanups.get(lease_token)
            if cleanup is None:
                cleanup = self._begin_speaker_deny_cleanup(
                    lease_token,
                    aborts,
                    terminal_sequence=result.terminal_sequence_no,
                )
            if (
                result.terminal_sequence_no
                != cleanup.context.terminal_speaker_sequence
            ):
                await self._fail_speaker_deny_cleanup(
                    cleanup,
                    "ASR_DENY_CLEANUP_TERMINAL_CONFLICT",
                )
                return
            cleanup.frozen_children = result.frozen_children
            for effect in aborts:
                cleanup.tickets.setdefault(effect.turn_token, effect.ticket)
            self._asr_provider_speaker_terminal_leases.add(lease_token)
            for stale_lease in tuple(self._asr_provider_speaker_terminal_leases):
                if (
                    len(self._asr_provider_speaker_terminal_leases)
                    <= _MAX_PROVIDER_BOUNDARY_SNAPSHOTS
                ):
                    break
                if stale_lease != lease_token:
                    self._asr_provider_speaker_terminal_leases.discard(stale_lease)
            if lease_token not in self._asr_speaker_deny_counted_leases:
                self._asr_speaker_deny_counted_leases.add(lease_token)
                self._speaker_rejection_metrics["speaker_deny_latched_count"] += 1
            for effect in effects:
                if isinstance(effect, (ResolveReserved, AbortProviderTransport)):
                    continue
                if isinstance(effect, CountDiagnostic) and effect.name in {
                    "speaker_deny_latched_count",
                    "speaker_lease_deny_latched_count",
                    "speaker_lease_mixed_deny_latched_count",
                }:
                    continue
                try:
                    await self._execute_admission_effect(effect)
                except Exception:
                    cleanup.degraded = True
            if cleanup.owner_task is None and not cleanup.settled.is_set():
                cleanup.owner_task = asyncio.create_task(
                    self._finish_speaker_deny_cleanup(cleanup),
                    name=f"speaker-deny-cleanup-{lease_token.lease_nonce}",
                )
                self._track_admission_effect_task(cleanup.owner_task, None)
                cleanup.owner_task.add_done_callback(self._admission_effect_done)
        elif result.outcome is SpeakerLeaseTransitionOutcome.CONFLICT:
            cleanup = self._asr_speaker_deny_cleanups.get(lease_token)
            if cleanup is not None:
                await self._fail_speaker_deny_cleanup(
                    cleanup,
                    "ASR_DENY_CLEANUP_TERMINAL_CONFLICT",
                )
        else:
            for effect in effects:
                if isinstance(effect, CountDiagnostic) and effect.name in {
                    "speaker_deny_latched_count",
                    "speaker_lease_deny_latched_count",
                    "speaker_lease_mixed_deny_latched_count",
                }:
                    # Child records only mirror their parent's formal
                    # verdict; fan-out must not multiply deny metrics.
                    continue
                await self._execute_admission_effect(effect)
        await self._execute_exact_admission_effects(result.successor_effects)
        record = await self._asr_admission.get_speaker_lease(lease_token)
        if record is not None and record.state in {
            SpeakerLeaseState.ALLOW,
            SpeakerLeaseState.DENY_LATCHED,
            SpeakerLeaseState.MIXED_DENY_LATCHED,
            SpeakerLeaseState.UNAVAILABLE,
        }:
            self._asr_provider_speaker_terminal_leases.add(lease_token)
        if (
            not formal_deny
            and
            record is not None
            and record.state
            in {
                SpeakerLeaseState.DENY_LATCHED,
                SpeakerLeaseState.MIXED_DENY_LATCHED,
            }
            and lease_token not in self._asr_speaker_deny_counted_leases
        ):
            self._asr_speaker_deny_counted_leases.add(lease_token)
            self._speaker_rejection_metrics["speaker_deny_latched_count"] += 1

    async def _flush_deferred_provider_speaker_lease_events(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        *,
        expected_identity: _AsrRuntimeIdentity,
        lifecycle: VoiceInputLifecycleController,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        if lease_token in self._asr_deferred_provider_speaker_lease_overflow:
            return False
        while True:
            pending = self._asr_deferred_provider_speaker_lease_events.get(lease_token)
            if not pending:
                self._asr_deferred_provider_speaker_lease_events.pop(lease_token, None)
                return True
            event = pending[0]
            try:
                prepared = await (
                    self._asr_admission_ingress.prepare_speaker_lease_transition(
                        lease_token,
                        event,
                    )
                )
                result = await self._apply_prepared_speaker_lease_transition(
                    lease_token,
                    prepared,
                    expected_identity=expected_identity,
                    cleanup_owner=None,
                    requires_terminal=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            if result is None:
                return False
            if (
                not self._runtime_identity_matches(expected_identity)
                or self._asr_lifecycle is not lifecycle
                or self._asr_current_speaker_lease != lease_token
                or self._asr_current_speaker_candidate != candidate
                or self._asr_admission_candidate_leases.get(candidate) != lease_token
            ):
                return False
            current = self._asr_deferred_provider_speaker_lease_events.get(lease_token)
            if current is None or not current or current[0] != event:
                return False
            current.popleft()

    async def _attach_provider_turn_to_speaker_lease(
        self,
        turn_token: VoiceTurnToken,
        provider_key: ProviderUtteranceKey,
        lease_token: SpeakerCaptureLeaseToken,
        candidate: SpeakerShadowCandidateKey,
        *,
        expected_identity: _AsrRuntimeIdentity,
        lifecycle: VoiceInputLifecycleController,
        correlator: ProviderTurnCorrelator,
    ) -> bool:
        existing = self._asr_admission_turn_leases.get(turn_token)
        if existing is not None:
            if existing != lease_token:
                return False
            record = await self._asr_admission.get_record(turn_token)
            lease_record = await self._asr_admission.get_speaker_lease(lease_token)
            return bool(
                self._runtime_identity_matches(expected_identity)
                and self._asr_lifecycle is lifecycle
                and self._asr_provider_correlator is correlator
                and self._provider_started_turn_is_current(lifecycle, turn_token)
                and record is not None
                and record.provider_binding_state is ProviderBindingState.BOUND
                and record.candidate_binding_state is CandidateBindingState.BOUND
                and record.provider_key == provider_key
                and record.speaker_lease_token == lease_token
                and record.speaker_candidate == candidate
                and lease_record is not None
                and lease_record.candidate == candidate
                and any(
                    child.provider_key == provider_key
                    and child.turn_token == turn_token
                    for child in lease_record.child_bindings
                )
            )
        attached = False
        future: asyncio.Future[Any] | None = None
        try:
            future = self._asr_admission_ingress.attach_turn_to_speaker_lease_nowait(
                turn_token,
                lease_token,
                provider_key,
            )
            record = await asyncio.shield(future)
            attached = True
        except asyncio.CancelledError:
            if future is not None:
                try:
                    await asyncio.shield(future)
                except Exception:
                    pass
                else:
                    await self._detach_provider_turn_from_speaker_lease(
                        turn_token,
                        lease_token,
                        provider_key,
                    )
            raise
        except Exception:
            await self._detach_provider_turn_from_speaker_lease(
                turn_token,
                lease_token,
                provider_key,
            )
            return False
        if (
            not self._runtime_identity_matches(expected_identity)
            or self._asr_lifecycle is not lifecycle
            or self._asr_provider_correlator is not correlator
            or not self._provider_started_turn_is_current(lifecycle, turn_token)
            or self._asr_current_speaker_lease != lease_token
            or self._asr_current_speaker_candidate != candidate
            or self._asr_admission_candidate_leases.get(candidate) != lease_token
            or record.turn_token != turn_token
            or record.provider_binding_state is not ProviderBindingState.BOUND
            or record.candidate_binding_state is not CandidateBindingState.BOUND
            or record.provider_key != provider_key
            or record.speaker_lease_token != lease_token
            or record.speaker_candidate != candidate
        ):
            if attached:
                await self._detach_provider_turn_from_speaker_lease(
                    turn_token,
                    lease_token,
                    provider_key,
                )
            return False
        ownership = self._asr_provider_turn_ownerships.get(turn_token)
        if ownership is None or ownership.provider_key != provider_key:
            await self._detach_provider_turn_from_speaker_lease(
                turn_token,
                lease_token,
                provider_key,
            )
            return False
        # Publish every Runtime alias before replaying deferred speaker facts.
        # The replay may synchronously produce DENY_LATCHED and resolve the
        # reservation, so no post-replay write may be required for cleanup.
        ownership.speaker_lease_token = lease_token
        ownership.child_published = True
        ownership.state = _ProviderTurnOwnershipState.CHILD_BOUND
        self._asr_admission_turn_leases[turn_token] = lease_token
        self._asr_speaker_authoritative_turns.add(turn_token)
        self._asr_provider_started_turns[provider_key] = turn_token
        if (
            not self._runtime_identity_matches(expected_identity)
            or self._asr_lifecycle is not lifecycle
            or self._asr_provider_correlator is not correlator
            or not self._provider_started_turn_is_current(lifecycle, turn_token)
        ):
            return False
        return True

    async def _detach_provider_turn_from_speaker_lease(
        self,
        turn_token: VoiceTurnToken,
        lease_token: SpeakerCaptureLeaseToken,
        provider_key: ProviderUtteranceKey,
    ) -> bool:
        try:
            return bool(
                await self._asr_admission.detach_turn_from_speaker_lease(
                    turn_token,
                    lease_token,
                    provider_key,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _provider_started_turn_is_current(
        lifecycle: VoiceInputLifecycleController,
        turn_token: VoiceTurnToken,
    ) -> bool:
        return bool(
            lifecycle.current_turn_token == turn_token
            or lifecycle.pending_turn_token == turn_token
        )

    async def _ensure_speaker_admission_capability(
        self,
        candidate: SpeakerShadowCandidateKey,
        turn_token: VoiceTurnToken,
        activation_generation: str,
    ) -> None:
        if activation_generation != self._speaker_verifier_activation_generation:
            return
        detector = self._asr_detector
        if detector is None:
            return
        try:
            lease = await detector.prepare_candidate_rejection(candidate)
        except asyncio.CancelledError:
            raise
        except Exception:
            lease = None
        if lease is None or lease.turn_token != turn_token:
            return
        if candidate.scope == "smart_turn_turn":
            capability = self._register_admission_capability(
                lease,
                kind=RejectionCapabilityKind.ACTIVE,
            )
            if capability is not None:
                await self._post_admission_event(
                    turn_token,
                    BoundaryExact(capability),
                )

    def _mark_speaker_evidence_backend_degraded(
        self,
        *,
        activation_generation: str,
    ) -> None:
        if activation_generation != self._speaker_verifier_activation_generation:
            return
        self._mark_speaker_verifier_degraded()

    def _mark_speaker_evidence_backend_healthy(
        self,
        *,
        activation_generation: str,
    ) -> None:
        if activation_generation != self._speaker_verifier_activation_generation:
            return
        self._mark_speaker_verifier_healthy()

    def _schedule_speaker_admission_item(
        self,
        candidate: SpeakerShadowCandidateKey,
        awaitable: Awaitable[None],
    ) -> bool:
        if self._asr_terminal_close_requested:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            return False
        predecessor = self._asr_admission_candidate_tasks.get(candidate)

        async def run() -> None:
            if predecessor is not None:
                try:
                    await asyncio.shield(predecessor)
                except (asyncio.CancelledError, Exception):
                    pass
            await awaitable

        task = loop.create_task(run(), name="voice-turn-speaker-admission")
        self._asr_admission_candidate_tasks[candidate] = task
        self._asr_admission_candidate_owned_tasks.add(task)

        def reap(done: asyncio.Task[None]) -> None:
            self._asr_admission_candidate_owned_tasks.discard(done)
            if self._asr_admission_candidate_tasks.get(candidate) is done:
                self._asr_admission_candidate_tasks.pop(candidate, None)
            self._log_asr_background_task_failure(done)

        task.add_done_callback(reap)
        return True

    def _speaker_verifier_diagnostics(self) -> dict[str, int]:
        """Return aggregate-only verifier diagnostics for local debugging."""

        metrics = dict(getattr(self, "_speaker_rejection_metrics", {}))
        factory = getattr(self, "_speaker_verifier_factory", None)
        snapshot = getattr(factory, "diagnostics_snapshot", None)
        if callable(snapshot):
            try:
                factory_metrics = snapshot()
            except Exception:
                factory_metrics = {}
            if not isinstance(factory_metrics, dict):
                factory_metrics = {}
            for name, value in factory_metrics.items():
                if type(name) is str and type(value) is int and value >= 0:
                    metrics[name] = value
        detector = getattr(self, "_asr_detector", None)
        detector_snapshot = getattr(
            detector,
            "speaker_rejection_diagnostics_snapshot",
            None,
        )
        if callable(detector_snapshot):
            try:
                detector_metrics = detector_snapshot()
            except Exception:
                detector_metrics = {}
            if isinstance(detector_metrics, dict):
                for name, value in detector_metrics.items():
                    if type(name) is str and type(value) is int and value >= 0:
                        metrics[name] = value
        metrics["verifier_installed_count"] = int(factory is not None)
        metrics["verifier_degraded_count"] = int(
            bool(getattr(self, "_speaker_verifier_degraded", False))
        )
        metrics["rejection_task_pending_count"] = len(
            getattr(self, "_asr_admission_rejection_executions", ())
        )
        metrics["rejection_in_progress_count"] = int(
            bool(getattr(self, "_asr_admission_rejection_executions", ()))
        )
        metrics.update({
            f"speaker_installation_{name}_count": value
            for name, value in getattr(self, "_speaker_installation_diagnostics", {}).items()
        })
        metrics["speaker_retired_cleanup_pending_count"] = len(
            getattr(self, "_speaker_retired_cleanup", ())
        )
        return metrics

    def _mark_speaker_verifier_degraded(
        self,
        *,
        preserve_reject_requested: bool = False,
    ) -> None:
        """Record backend health; ordered UNAVAILABLE facts revoke authority."""

        del preserve_reject_requested
        self._ensure_asr_runtime_state()
        if not self._speaker_verifier_degraded:
            self._speaker_verifier_health_generation += 1
        self._speaker_verifier_degraded = True

    def _mark_speaker_verifier_healthy(self) -> None:
        """Clear transient Owner verifier health degradation after recovery."""

        self._ensure_asr_runtime_state()
        if self._speaker_verifier_degraded:
            self._speaker_verifier_health_generation += 1
        self._speaker_verifier_degraded = False

    @staticmethod
    def _close_speaker_verifier_factory(factory: SpeakerShadowFactory) -> None:
        close = getattr(factory, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            return

    def _begin_asr_start_operation(self) -> int:
        self._asr_start_generation += 1
        return self._asr_start_generation

    def _deny_transport_blocks_provider_egress(self) -> bool:
        return self._asr_deny_transport_state in {
            DenyTransportState.DENY_FENCED,
            DenyTransportState.RETIRING,
            DenyTransportState.WAIT_SILENCE,
            DenyTransportState.ARMED,
            DenyTransportState.QUARANTINED,
        }

    def _provider_callback_scope(self) -> tuple[int, int] | None:
        lifecycle = self._asr_lifecycle
        if lifecycle is None:
            return None
        return (
            self._asr_session_epoch,
            lifecycle.snapshot.transport_generation,
        )

    def _acquire_provider_callback_ticket(
        self,
        *,
        session_ref: Any,
        session_epoch: int,
    ) -> _ProviderCallbackTicket | None:
        """Fence callback entry against DENY and retain pre-fence callbacks."""

        if (
            self._asr_deny_transport_state is not DenyTransportState.OPEN
            or session_epoch != self._asr_session_epoch
            or self._asr_session is not session_ref
        ):
            return None
        scope = self._provider_callback_scope()
        if scope is None or scope[0] != session_epoch:
            return None
        self._asr_provider_callback_ticket_sequence += 1
        idle = self._asr_provider_callback_idle.setdefault(scope, asyncio.Event())
        idle.clear()
        self._asr_provider_callback_inflight[scope] = (
            self._asr_provider_callback_inflight.get(scope, 0) + 1
        )
        return _ProviderCallbackTicket(
            session_epoch=scope[0],
            transport_generation=scope[1],
            ticket_no=self._asr_provider_callback_ticket_sequence,
            session_ref=session_ref,
        )

    def _release_provider_callback_ticket(
        self,
        ticket: _ProviderCallbackTicket,
    ) -> None:
        scope = (ticket.session_epoch, ticket.transport_generation)
        count = self._asr_provider_callback_inflight.get(scope, 0)
        if count <= 1:
            self._asr_provider_callback_inflight.pop(scope, None)
            self._asr_provider_callback_idle.setdefault(scope, asyncio.Event()).set()
            return
        self._asr_provider_callback_inflight[scope] = count - 1

    async def _wait_provider_callback_tickets(
        self,
        context: DenyCleanupContext,
    ) -> bool:
        scope = (context.session_epoch, context.transport_generation)
        if self._asr_provider_callback_inflight.get(scope, 0) == 0:
            return True
        idle = self._asr_provider_callback_idle.setdefault(scope, asyncio.Event())
        try:
            await asyncio.wait_for(
                idle.wait(),
                timeout=_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return False
        return self._asr_provider_callback_inflight.get(scope, 0) == 0

    async def _run_provider_callback(
        self,
        *,
        session_ref: Any,
        session_epoch: int,
        callback: Callable[[], Awaitable[Any]],
    ) -> tuple[bool, Any]:
        ticket = self._acquire_provider_callback_ticket(
            session_ref=session_ref,
            session_epoch=session_epoch,
        )
        if ticket is None:
            return False, None
        try:
            return True, await callback()
        finally:
            self._release_provider_callback_ticket(ticket)

    def _asr_start_operation_matches(self, operation_generation: int) -> bool:
        return operation_generation == self._asr_start_generation

    def _invalidate_asr_start(self) -> None:
        self._begin_asr_start_operation()

    def capture_ingress_token(
        self,
        *,
        connection_id: str,
        lease_generation: int,
        route_generation: int,
    ) -> VoiceIngressToken:
        return VoiceIngressToken(
            session_epoch=self._asr_session_epoch,
            connection_id=connection_id,
            lease_generation=lease_generation,
            route_generation=route_generation,
            audio_generation=self._asr_audio_generation,
        )

    async def suspend(self, reason: str) -> None:
        lifecycle = self._asr_lifecycle
        if lifecycle is not None and lifecycle.snapshot.state not in {
            VoiceLifecycleState.OFF,
            VoiceLifecycleState.BLOCKED,
            VoiceLifecycleState.SUSPENDED,
        }:
            lifecycle.transition(VoiceLifecycleEvent.GAME_TAKEOVER)
        await self.abort(reason)

    async def resume(self, reason: str) -> None:
        del reason
        epoch = self._asr_session_epoch
        provider = self._asr_provider or "unknown"
        lifecycle = self._asr_lifecycle
        if lifecycle is not None and (
            lifecycle.snapshot.state is VoiceLifecycleState.SUSPENDED
        ):
            lifecycle.transition(VoiceLifecycleEvent.GAME_RELEASED)
            identity = self._capture_runtime_identity()
            await self._send_asr_lifecycle_state(
                lifecycle.snapshot.state,
                provider=provider,
                session_epoch=epoch,
                expected_identity=identity,
            )

    def _asr_runtime_refs_match(
        self,
        epoch: int,
        lifecycle: VoiceInputLifecycleController | None,
        detector: DetectorRuntime | None,
    ) -> bool:
        return bool(
            epoch == self._asr_session_epoch
            and self._asr_lifecycle is lifecycle
            and self._asr_detector is detector
        )

    def _capture_runtime_identity(
        self,
        *,
        ingress_token: VoiceIngressToken | None = None,
        turn_token: VoiceTurnToken | None = None,
    ) -> _AsrRuntimeIdentity:
        lifecycle = self._asr_lifecycle
        return _AsrRuntimeIdentity(
            start_generation=self._asr_start_generation,
            session_epoch=self._asr_session_epoch,
            audio_generation=self._asr_audio_generation,
            lifecycle=lifecycle,
            transport_generation=(
                lifecycle.snapshot.transport_generation
                if lifecycle is not None
                else None
            ),
            detector=self._asr_detector,
            session=self._asr_session,
            provider=self._asr_provider,
            session_factory=self._asr_session_factory,
            transport_selection=self._asr_transport_selection,
            transport_task=self._asr_transport_task,
            ingress_token=ingress_token,
            turn_token=turn_token,
        )

    def _runtime_identity_matches(
        self,
        identity: _AsrRuntimeIdentity,
    ) -> bool:
        lifecycle = self._asr_lifecycle
        if (
            identity.start_generation != self._asr_start_generation
            or identity.session_epoch != self._asr_session_epoch
            or identity.audio_generation != self._asr_audio_generation
            or lifecycle is not identity.lifecycle
            or self._asr_detector is not identity.detector
            or self._asr_session is not identity.session
            or self._asr_provider != identity.provider
            or self._asr_session_factory is not identity.session_factory
            or self._asr_transport_selection is not identity.transport_selection
            or self._asr_transport_task is not identity.transport_task
        ):
            return False
        transport_generation = (
            lifecycle.snapshot.transport_generation if lifecycle is not None else None
        )
        if transport_generation != identity.transport_generation:
            return False
        if identity.ingress_token is not None and (
            self._asr_current_ingress_token != identity.ingress_token
            or not self._ingress_token_matches(identity.ingress_token)
        ):
            return False
        if identity.turn_token is not None and (
            lifecycle is None
            or identity.turn_token.ingress != identity.ingress_token
            or lifecycle.snapshot.turn_id != identity.turn_token.turn_id
        ):
            return False
        return True

    async def abort(self, reason: str) -> None:
        if reason == "ingress_backpressure":
            token = self._asr_current_ingress_token
            if token is not None and self._ingress_token_matches(token):
                await self._handle_audio_ingress_backpressure(token)
                return
        epoch = self._asr_session_epoch
        lifecycle = self._asr_lifecycle
        detector = self._asr_detector
        provider = self._asr_provider or "unknown"
        if lifecycle is not None:
            lifecycle.invalidate_audio()
        post_detach = await self._abort_transport(reason)
        if not self._runtime_identity_matches(
            post_detach
        ) or not self._asr_runtime_refs_match(epoch, lifecycle, detector):
            return
        if reason == "ingress_backpressure":
            await self._send_asr_status(
                "ASR_INGRESS_BACKPRESSURE",
                provider,
                session_epoch=epoch,
                expected_identity=post_detach,
            )
        if detector is not None:
            try:
                await detector.reset()
            except Exception:
                logger.warning(
                    "[%s] detector reset failed during voice abort",
                    self.display_name,
                )
            if not self._runtime_identity_matches(
                post_detach
            ) or not self._asr_runtime_refs_match(epoch, lifecycle, detector):
                return
        if lifecycle is not None:
            await self._send_asr_lifecycle_state(
                lifecycle.snapshot.state,
                provider=provider,
                session_epoch=epoch,
                expected_identity=post_detach,
            )

    async def wait_transcript_idle(self) -> None:
        await self._asr_transcript_dispatcher.wait_idle()

    def has_pending_transcript_delivery(self) -> bool:
        """Return whether an accepted final has not finished Core dispatch."""

        return self._asr_transcript_dispatcher.has_pending_delivery

    def _init_asr_runtime_state(self) -> None:
        self._asr_session = None
        self._asr_session_epoch = 0
        self._asr_start_generation = 0
        self._asr_provider = None
        self._asr_turn_prepared = False
        self._asr_final_lock = asyncio.Lock()
        self._asr_pending_turn_handoff: _PendingTurnHandoff | None = None
        self._asr_admission = VoiceTurnAdmissionCoordinator(
            capacity=8, evidence_hold_enabled=getattr(self, "_asr_evidence_hold_enabled", False),
        )
        self._asr_admission_ingress = AdmissionIngressLane(
            self._asr_admission,
            data_capacity=64,
        )
        self._asr_admission_ingress_started = False
        self._asr_admission_capability_sequence = 0
        self._asr_admission_capability_generation = 0
        self._asr_admission_capabilities: dict[
            int,
            _AdmissionCapabilityOwner,
        ] = {}
        self._asr_admission_candidate_turns: dict[
            SpeakerShadowCandidateKey,
            VoiceTurnToken,
        ] = {}
        self._asr_admission_candidate_leases: dict[
            SpeakerShadowCandidateKey,
            SpeakerCaptureLeaseToken,
        ] = {}
        self._asr_admission_turn_leases: dict[
            VoiceTurnToken,
            SpeakerCaptureLeaseToken,
        ] = {}
        self._asr_speaker_lease_nonce = 0
        self._asr_speaker_deny_counted_leases: set[SpeakerCaptureLeaseToken] = set()
        self._asr_current_speaker_lease: SpeakerCaptureLeaseToken | None = None
        self._asr_current_speaker_candidate: SpeakerShadowCandidateKey | None = None
        self._asr_deferred_provider_speaker_lease_events: dict[
            SpeakerCaptureLeaseToken,
            deque[SpeakerLeaseEvent],
        ] = {}
        self._asr_deferred_provider_speaker_lease_overflow: set[
            SpeakerCaptureLeaseToken
        ] = set()
        self._asr_provider_speaker_terminal_leases: set[
            SpeakerCaptureLeaseToken
        ] = set()
        self._asr_admission_candidate_tasks: dict[
            SpeakerShadowCandidateKey,
            asyncio.Task[None],
        ] = {}
        self._asr_admission_candidate_owned_tasks: set[asyncio.Task[None]] = set()
        self._asr_admission_deadline_tasks: dict[
            AdmissionOperationTicket,
            asyncio.Task[None],
        ] = {}
        self._asr_admission_effect_tasks: set[asyncio.Task[Any]] = set()
        self._asr_exact_callback_tasks: set[asyncio.Task[Any]] = set()
        self._asr_admission_effect_task_turns: dict[
            asyncio.Task[Any],
            VoiceTurnToken | None,
        ] = {}
        self._asr_admission_rejection_executions: dict[
            AdmissionOperationTicket,
            _AdmissionRejectionExecution,
        ] = {}
        self._asr_admission_rejection_deadlines: dict[
            AdmissionOperationTicket,
            float,
        ] = {}
        self._asr_admission_turn_sealed_events: dict[
            VoiceTurnToken,
            asyncio.Event,
        ] = {}
        self._asr_admission_final_contexts: dict[
            VoiceTurnToken,
            _AdmissionFinalContext,
        ] = {}
        self._asr_admission_resolutions: dict[
            FinalKey,
            _AdmissionResolutionExecution,
        ] = {}
        self._asr_admission_reservation_dispatchers: dict[
            FinalKey,
            TranscriptDispatcher,
        ] = {}
        self._asr_provider_turn_ownerships: dict[
            VoiceTurnToken,
            _ProviderTurnOwnership,
        ] = {}
        self._asr_deny_cleanup_generation = 0
        self._asr_deny_cleanup_active = False
        self._asr_deny_transport_state = DenyTransportState.OPEN
        self._asr_last_ingress_sequence = 0
        self._asr_last_captured_at = 0.0
        self._asr_rearm_cutoff_sequence = 0
        self._asr_rearm_last_sequence = 0
        self._asr_rearm_last_captured_at = 0.0
        self._asr_provider_callback_ticket_sequence = 0
        self._asr_provider_callback_inflight: dict[tuple[int, int], int] = {}
        self._asr_provider_callback_idle: dict[tuple[int, int], asyncio.Event] = {}
        self._asr_lifecycle_notification_revision = 0
        self._asr_speaker_deny_cleanups: dict[
            SpeakerCaptureLeaseToken,
            _SpeakerDenyCleanupOperation,
        ] = {}
        self._asr_quarantined_partials: dict[VoiceTurnToken, str] = {}
        self._asr_partial_settlements: dict[
            VoiceTurnToken,
            tuple[int, AdmissionDisposition],
        ] = {}
        self._asr_speaker_authority_pending_turns: dict[
            VoiceTurnToken,
            str,
        ] = {}
        self._asr_speaker_authoritative_turns: set[VoiceTurnToken] = set()
        self._asr_speaker_authority_unarming_tasks: dict[
            tuple[VoiceTurnToken, str],
            asyncio.Task[None],
        ] = {}
        self._asr_provider_correlator: ProviderTurnCorrelator | None = None
        self._asr_provider_correlator_namespace: tuple[int, int] | None = None
        self._asr_provider_started_turns: dict[
            ProviderUtteranceKey,
            VoiceTurnToken,
        ] = {}
        self._asr_deferred_provider_started_keys: deque[ProviderUtteranceKey] = deque()
        self._asr_provider_boundary_proof_sequence = 0
        self._asr_provider_boundary_proofs: dict[
            int,
            ProviderSpeakerBoundarySnapshot,
        ] = {}
        self._asr_provider_boundary_completions: dict[
            BoundaryProof, _ProviderBoundaryCompletion
        ] = {}
        self._asr_provider_exact_intervals: dict[
            ProviderUtteranceKey,
            _ProviderExactIntervalTransaction,
        ] = {}
        self._asr_provider_exact_pending: dict[
            ProviderUtteranceKey,
            _ProviderExactIntervalPending,
        ] = {}
        self._asr_provider_exact_candidates: dict[
            SpeakerShadowCandidateKey,
            _ProviderExactIntervalTransaction,
        ] = {}
        self._asr_provider_speaker_ledgers: dict[
            SpeakerShadowCandidateKey,
            _ProviderSpeakerProvisionalLedger,
        ] = {}
        self._asr_provider_speaker_key_ledgers: dict[
            ProviderUtteranceKey,
            _ProviderSpeakerProvisionalLedger,
        ] = {}
        self._asr_audio_bytes = 0
        self._asr_received_audio = False
        self._asr_close_tasks: set[asyncio.Task[None]] = set()
        self._asr_owned_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._asr_runtime_close_task: asyncio.Task[None] | None = None
        self._asr_terminal_close_requested = False
        self._asr_terminal_close_task: asyncio.Task[None] | None = None
        self._asr_terminal_cancel_requested_tasks: weakref.WeakSet[
            asyncio.Task[Any]
        ] = weakref.WeakSet()
        self._asr_lifecycle: VoiceInputLifecycleController | None = None
        self._asr_detector: DetectorRuntime | None = None
        self._asr_smart_turn_lease: SmartTurnLease | None = None
        self._asr_smart_turn_prepare_lock = asyncio.Lock()
        self._asr_smart_turn_prepare_scope: tuple[int, int, int] | None = None
        self._asr_session_factory = None
        self._asr_transport_selection = None
        self._asr_transport_task: asyncio.Task[None] | None = None
        self._asr_transport_lock = asyncio.Lock()
        self._asr_warm_expiry_task: asyncio.Task[None] | None = None
        self._asr_final_watchdog_task: asyncio.Task[None] | None = None
        self._asr_pending_speech_confirmed = False
        self._asr_pending_speech_onset_at = None
        self._asr_pending_detector_candidate = None
        self._asr_overlap_onset_token: VoiceIngressToken | None = None
        # 重叠发声的真实开口时刻。重放发生在「上一轮延迟 final 到达」之后，比用户
        # 实际开口晚得多；不把这一刻带过去，重放时取到的 onset 会把中间那段全算成
        # 「开口之后」，后继发声在重放前拍的帧就全被排除了。
        self._asr_overlap_onset_at: float | None = None
        self._asr_overlap_completed_token: VoiceIngressToken | None = None
        # 每张 credit 一个开口时刻：多个 onset+pause 周期可以在同一条延迟 final
        # 后面排队，用单个槽位会让所有重放共用最后那个时刻。
        self._asr_overlap_completed_onsets: deque[float] = deque()
        self._asr_overlap_completed_turns = 0
        self._asr_sealed_turn_token: VoiceTransportToken | None = None
        self._asr_provider_candidate_fence: ProviderCandidateFence | None = None
        self._asr_sealed_provider_key: ProviderUtteranceKey | None = None
        # Exact Provider text remains admissible when the advisory Detector
        # seal cannot acquire its lock inside the ordered/final 200 ms budget.
        # This key never grants speaker authority; it only lets the matching
        # transcript complete the already reserved Core turn fail-open.
        self._asr_provider_authority_reset_task: asyncio.Task[bool] | None = None
        self._asr_provider_exact_session: Any | None = None
        self._asr_audio_sequence = 0
        self._asr_provider_speaker_sequence = 0
        self._asr_provider_speaker_evidence_lease: (
            ProviderSpeakerEvidenceLease | None
        ) = None
        self._asr_provider_speaker_arming_tasks: dict[
            VoiceTurnToken,
            asyncio.Task[_SpeakerArmingResult],
        ] = {}
        self._asr_buffered_provider_speaker_observation: (
            _BufferedProviderSpeakerObservation | None
        ) = None
        self._asr_audio_generation = 0
        self._asr_current_ingress_token: VoiceIngressToken | None = None
        self._asr_partial_turn_token: VoiceTurnToken | None = None
        self._speaker_verifier_factory: SpeakerShadowFactory | None = None
        self._speaker_verifier_activation_generation: str | None = None
        self._speaker_verifier_enforces_admission = False
        self._speaker_verifier_degraded = False
        self._speaker_verifier_health_generation = 0
        self._asr_speaker_degradation_incident: _SpeakerEvidenceDegradation | None = None
        self._speaker_verifier_lock = asyncio.Lock()
        self._speaker_rejection_metrics = _new_speaker_rejection_metrics()
        self._asr_transcript_dispatcher = self._new_asr_transcript_dispatcher()
        self._asr_detector_dispatcher = AsrDetectorDispatcher(
            self._dispatch_asr_detector_event,
            on_failure=self._handle_asr_detector_dispatcher_failure,
        )
        self._asr_audio_dispatcher = AsrAudioDispatcher(
            validator=self._asr_audio_command_is_valid,
            on_wire_audio=self._record_asr_dispatcher_wire_audio,
            on_failure=self._handle_asr_audio_dispatcher_failure,
        )
        self._asr_last_provider_wire_audio_ms = 0
        self._asr_turn_audio_started_at: float | None = None
        # 语义上的「用户开口时刻」。与上面那个的区别是**打点位置**：这个钉在
        # SPEECH_CONFIRMED 转换那一行，不跨 _send_asr_lifecycle_state() 的投递
        # await；上面那个在两条路径上是投递完成之后才打的，喂延迟指标够用，但拿
        # 来当视觉所有权的起点会把投递窗口里拍的帧判成"不属于这段发声"。
        self._asr_turn_onset_at: float | None = None
        # 语音已经检测到、但 ASR session 还没就绪（要等重连）时先把这一刻记下来。
        # 真正 SPEECH_CONFIRMED 要等 connect() 成功之后才发得出去，用那时的时钟
        # 当"用户开口时刻"会把整段重连等待算进去，重连期间拍的帧全被判成不属于
        # 这段发声。
        self._asr_pending_speech_onset_at: float | None = None
        # 上一回合还在排空（DRAINING）时用户就接着说了：pending turn 的真实开口时刻
        # 是 mark_pending_turn_speech() 那一刻，不是后面 begin_pending_turn() 激活的
        # 时刻。lifecycle 硬要求 DRAINING 才能标记，所以这个值必然晚于上一轮封口。
        self._asr_pending_turn_onset_at: float | None = None
        self._asr_turn_endpointed_at: float | None = None
        # 与上面那个一样在封口时刻打点，但**不在 PROVIDER_FINAL 时清掉**。Core 要
        # 到 transcript 派发之后才冻结多模态回合，那时上面那个已经是 None 了；
        # 消费方靠"这个时刻是否晚于本回合起点"排除上一轮的残值。
        self._asr_last_turn_endpointed_at: float | None = None
        # 上面那个保留副本**属于哪一轮**。时间戳分不清"上一轮的封口"和"本轮的封
        # 口"：monotonic 在 Windows 上是 ~15ms 粒度，两者都可能与后继 record 的注
        # 册时刻相等，往任一个方向猜都会错（猜"归上一轮"会丢掉本轮自己的截止点，
        # 猜"归本轮"会把上一轮的封口盖到后继头上）。带上身份就不用猜。
        self._asr_last_turn_endpointed_key: str | None = None
        self._asr_first_partial_recorded = False
        self._voice_input_resource_optimization_enabled = True

    def _schedule_owned_cleanup(
        self,
        awaitable: Awaitable[Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        """Keep teardown running when its caller is cancelled."""

        task = asyncio.create_task(awaitable, name=name)
        self._asr_owned_cleanup_tasks.add(task)
        task.add_done_callback(self._owned_cleanup_done)
        return task

    def _owned_cleanup_done(self, task: asyncio.Task[Any]) -> None:
        self._asr_owned_cleanup_tasks.discard(task)
        self._log_asr_background_task_failure(task)

    def _ensure_asr_runtime_state(self) -> None:
        # A number of focused unit tests intentionally construct the manager via
        # __new__. Keep those narrow lifecycle doubles compatible.
        if not hasattr(self, "_asr_session_epoch"):
            self._init_asr_runtime_state()
        elif not hasattr(self, "_asr_transcript_dispatcher"):
            self._asr_transcript_dispatcher = self._new_asr_transcript_dispatcher()
        if not hasattr(self, "_asr_detector_dispatcher"):
            self._asr_detector_dispatcher = AsrDetectorDispatcher(
                self._dispatch_asr_detector_event,
                on_failure=self._handle_asr_detector_dispatcher_failure,
            )
        if not hasattr(self, "_asr_audio_dispatcher"):
            self._asr_audio_dispatcher = AsrAudioDispatcher(
                validator=self._asr_audio_command_is_valid,
                on_wire_audio=self._record_asr_dispatcher_wire_audio,
                on_failure=self._handle_asr_audio_dispatcher_failure,
            )
            self._asr_audio_sequence = 0
            self._asr_pending_detector_candidate = None
        if not hasattr(self, "_asr_admission"):
            self._asr_admission = VoiceTurnAdmissionCoordinator(
                capacity=8, evidence_hold_enabled=getattr(self, "_asr_evidence_hold_enabled", False),
            )
            self._asr_admission_ingress = AdmissionIngressLane(
                self._asr_admission,
                data_capacity=64,
            )
            self._asr_admission_ingress_started = False
            self._asr_admission_capability_sequence = 0
            self._asr_admission_capability_generation = 0
            self._asr_admission_capabilities = {}
            self._asr_admission_candidate_turns = {}
            self._asr_admission_candidate_leases = {}
            self._asr_admission_turn_leases = {}
            self._asr_speaker_lease_nonce = 0
            self._asr_speaker_deny_counted_leases = set()
            self._asr_current_speaker_lease = None
            self._asr_current_speaker_candidate = None
            self._asr_deferred_provider_speaker_lease_events = {}
            self._asr_deferred_provider_speaker_lease_overflow = set()
            self._asr_provider_speaker_terminal_leases = set()
            self._asr_admission_candidate_tasks = {}
            self._asr_admission_candidate_owned_tasks = set()
            self._asr_admission_deadline_tasks = {}
            self._asr_admission_effect_tasks = set()
            self._asr_admission_effect_task_turns = {}
            self._asr_admission_rejection_executions = {}
            self._asr_admission_rejection_deadlines = {}
            self._asr_admission_turn_sealed_events = {}
            self._asr_admission_final_contexts = {}
            self._asr_admission_resolutions = {}
            self._asr_admission_reservation_dispatchers = {}
            self._asr_provider_turn_ownerships = {}
            self._asr_deny_cleanup_generation = 0
            self._asr_deny_cleanup_active = False
            self._asr_deny_transport_state = DenyTransportState.OPEN
            self._asr_last_ingress_sequence = 0
            self._asr_last_captured_at = 0.0
            self._asr_rearm_cutoff_sequence = 0
            self._asr_rearm_last_sequence = 0
            self._asr_rearm_last_captured_at = 0.0
            self._asr_provider_callback_ticket_sequence = 0
            self._asr_provider_callback_inflight = {}
            self._asr_provider_callback_idle = {}
            self._asr_lifecycle_notification_revision = 0
            self._asr_speaker_deny_cleanups = {}
            self._asr_quarantined_partials = {}
            self._asr_partial_settlements = {}
            self._asr_speaker_authority_pending_turns = {}
            self._asr_speaker_authoritative_turns = set()
            self._asr_speaker_authority_unarming_tasks = {}
            self._asr_provider_correlator = None
            self._asr_provider_correlator_namespace = None
            self._asr_provider_started_turns = {}
            self._asr_deferred_provider_started_keys = deque()
            self._asr_provider_boundary_proof_sequence = 0
            self._asr_provider_boundary_proofs = {}
        elif not hasattr(self, "_asr_admission_ingress"):
            self._asr_admission_ingress = AdmissionIngressLane(
                self._asr_admission,
                data_capacity=64,
            )
            self._asr_admission_ingress_started = False
            self._asr_admission_rejection_executions = {}
            self._asr_admission_rejection_deadlines = {}
            self._asr_admission_turn_sealed_events = {}
            self._asr_admission_reservation_dispatchers = {}
            self._asr_provider_turn_ownerships = {}
        if not hasattr(self, "_asr_provider_turn_ownerships"):
            self._asr_provider_turn_ownerships = {}
        if not hasattr(self, "_asr_provider_boundary_completions"):
            self._asr_provider_boundary_completions = {}
        if not hasattr(self, "_asr_deny_cleanup_generation"):
            self._asr_deny_cleanup_generation = 0
            self._asr_deny_cleanup_active = False
            self._asr_speaker_deny_cleanups = {}
        if not hasattr(self, "_asr_deny_transport_state"):
            self._asr_deny_transport_state = DenyTransportState.OPEN
            self._asr_last_ingress_sequence = 0
            self._asr_last_captured_at = 0.0
            self._asr_rearm_cutoff_sequence = 0
            self._asr_rearm_last_sequence = 0
            self._asr_rearm_last_captured_at = 0.0
            self._asr_provider_callback_ticket_sequence = 0
            self._asr_provider_callback_inflight = {}
            self._asr_provider_callback_idle = {}
            self._asr_lifecycle_notification_revision = 0
        if not hasattr(self, "_asr_admission_effect_task_turns"):
            self._asr_admission_effect_task_turns = {}
        if not hasattr(self, "_asr_admission_candidate_leases"):
            self._asr_admission_candidate_leases = {}
            self._asr_admission_turn_leases = {}
            self._asr_speaker_lease_nonce = 0
            self._asr_current_speaker_lease = None
            self._asr_current_speaker_candidate = None
        if not hasattr(self, "_asr_speaker_deny_counted_leases"):
            self._asr_speaker_deny_counted_leases = set()
        if not hasattr(self, "_asr_deferred_provider_speaker_lease_events"):
            self._asr_deferred_provider_speaker_lease_events = {}
        if not hasattr(self, "_asr_deferred_provider_speaker_lease_overflow"):
            self._asr_deferred_provider_speaker_lease_overflow = set()
        if not hasattr(self, "_asr_provider_speaker_terminal_leases"):
            self._asr_provider_speaker_terminal_leases = set()
        if not hasattr(self, "_asr_provider_started_turns"):
            self._asr_provider_started_turns = {}
            self._asr_deferred_provider_started_keys = deque()
        if not hasattr(self, "_asr_quarantined_partials"):
            self._asr_quarantined_partials = {}
        if not hasattr(self, "_asr_partial_settlements"):
            self._asr_partial_settlements = {}
        if not hasattr(self, "_asr_speaker_authority_pending_turns"):
            self._asr_speaker_authority_pending_turns = {}
        if not hasattr(self, "_asr_speaker_authoritative_turns"):
            self._asr_speaker_authoritative_turns = set()
        if not hasattr(self, "_asr_speaker_authority_unarming_tasks"):
            self._asr_speaker_authority_unarming_tasks = {}
        if not hasattr(self, "_asr_admission_candidate_owned_tasks"):
            self._asr_admission_candidate_owned_tasks = set(
                self._asr_admission_candidate_tasks.values()
            )
        if not hasattr(self, "_asr_provider_speaker_sequence"):
            self._asr_provider_speaker_sequence = 0
        if not hasattr(self, "_asr_provider_speaker_evidence_lease"):
            self._asr_provider_speaker_evidence_lease = None
        if not hasattr(self, "_asr_provider_speaker_arming_tasks"):
            self._asr_provider_speaker_arming_tasks = {}
        if not hasattr(self, "_asr_buffered_provider_speaker_observation"):
            self._asr_buffered_provider_speaker_observation = None
        if not hasattr(self, "_asr_overlap_onset_token"):
            self._asr_overlap_onset_token = None
        if not hasattr(self, "_asr_overlap_onset_at"):
            self._asr_overlap_onset_at = None
        if not hasattr(self, "_asr_overlap_completed_onsets"):
            self._asr_overlap_completed_onsets = deque()
        if not hasattr(self, "_asr_partial_turn_token"):
            self._asr_partial_turn_token = None
        if not hasattr(self, "_asr_overlap_completed_token"):
            self._asr_overlap_completed_token = None
            self._asr_overlap_completed_turns = 0
        if not hasattr(self, "_asr_start_generation"):
            self._asr_start_generation = 0
        if not hasattr(self, "_asr_provider_candidate_fence"):
            self._asr_provider_candidate_fence = None
        if not hasattr(self, "_asr_sealed_provider_key"):
            self._asr_sealed_provider_key = None
        if not hasattr(self, "_asr_provider_authority_reset_task"):
            self._asr_provider_authority_reset_task = None
        if not hasattr(self, "_asr_provider_exact_session"):
            self._asr_provider_exact_session = None
        if not hasattr(self, "_asr_provider_exact_intervals"):
            self._asr_provider_exact_intervals = {}
        if not hasattr(self, "_asr_exact_callback_tasks"):
            self._asr_exact_callback_tasks = set()
        if not hasattr(self, "_asr_provider_exact_pending"):
            self._asr_provider_exact_pending = {}
        if not hasattr(self, "_asr_provider_exact_candidates"):
            self._asr_provider_exact_candidates = {}
        if not hasattr(self, "_asr_provider_speaker_ledgers"):
            self._asr_provider_speaker_ledgers = {}
        if not hasattr(self, "_asr_provider_speaker_key_ledgers"):
            self._asr_provider_speaker_key_ledgers = {}
        if not hasattr(self, "_asr_owned_cleanup_tasks"):
            self._asr_owned_cleanup_tasks = set()
        if not hasattr(self, "_asr_runtime_close_task"):
            self._asr_runtime_close_task = None
        if not hasattr(self, "_asr_terminal_close_requested"):
            self._asr_terminal_close_requested = False
        if not hasattr(self, "_asr_terminal_close_task"):
            self._asr_terminal_close_task = None
        if not hasattr(self, "_asr_terminal_cancel_requested_tasks"):
            self._asr_terminal_cancel_requested_tasks = weakref.WeakSet()
        if not hasattr(self, "_asr_smart_turn_prepare_lock"):
            self._asr_smart_turn_prepare_lock = asyncio.Lock()
            self._asr_smart_turn_prepare_scope = None
        if not hasattr(self, "_speaker_verifier_factory"):
            self._speaker_verifier_factory = None
            self._speaker_verifier_activation_generation = None
            self._speaker_verifier_enforces_admission = False
            self._speaker_verifier_degraded = False
            self._speaker_verifier_health_generation = 0
        elif not hasattr(self, "_speaker_verifier_degraded"):
            self._speaker_verifier_degraded = False
        if not hasattr(self, "_speaker_verifier_enforces_admission"):
            self._speaker_verifier_enforces_admission = (
                _speaker_factory_enforces_admission(self._speaker_verifier_factory)
            )
        if not hasattr(self, "_speaker_verifier_health_generation"):
            self._speaker_verifier_health_generation = 0
        if not hasattr(self, "_asr_speaker_degradation_incident"):
            self._asr_speaker_degradation_incident = None
        if not hasattr(self, "_speaker_verifier_lock"):
            self._speaker_verifier_lock = asyncio.Lock()

    def _capture_turn_token(
        self,
        lifecycle: VoiceInputLifecycleController,
    ) -> VoiceTurnToken:
        current_turn_token = lifecycle.current_turn_token
        if current_turn_token is not None:
            return current_turn_token
        ingress_token = self._asr_current_ingress_token
        if ingress_token is None or not self._ingress_token_matches(ingress_token):
            raise RuntimeError("ASR_INGRESS_TOKEN_REQUIRED")
        return lifecycle.bind_current_turn_token(ingress_token)

    async def _post_admission_event(
        self,
        turn_token: VoiceTurnToken,
        event: object,
        *,
        now: float | None = None,
    ) -> tuple[AdmissionEffect, ...]:
        """Reduce one fact, then execute every effect outside coordinator lock."""

        if not self._asr_admission_ingress_started:
            await self._asr_admission_ingress.start()
            self._asr_admission_ingress_started = True
        try:
            future = self._asr_admission_ingress.post_nowait(
                turn_token,
                event,  # type: ignore[arg-type]
                now=now,
            )
        except AdmissionIngressCapacityError:
            if isinstance(event, BoundaryExact):
                owner = self._asr_admission_capabilities.pop(
                    event.capability.capability_id,
                    None,
                )
                if owner is not None:
                    owner.revoked = True
                future = self._asr_admission_ingress.post_nowait(
                    turn_token,
                    BoundaryUnknown(event.capability.provider_key),
                    now=now,
                )
            else:
                raise
        consumer = self._consume_admission_future(
            turn_token,
            future,
            suppress_terminal_errors=False,
        )
        return await asyncio.shield(consumer)

    def _track_admission_effect_task(
        self,
        task: asyncio.Task[Any],
        turn_token: VoiceTurnToken | None,
    ) -> None:
        self._asr_admission_effect_tasks.add(task)
        self._asr_admission_effect_task_turns[task] = turn_token

    def _track_exact_callback_task(self, task: asyncio.Task[None]) -> None:
        """Own callbacks separately from effects that invalidation must drain."""

        self._asr_exact_callback_tasks.add(task)
        # A callback may itself await stop/start. Do not put it in the retired
        # turn's effect join, which would then wait for its own caller.
        self._track_admission_effect_task(task, None)

        def done(completed: asyncio.Task[None]) -> None:
            self._asr_exact_callback_tasks.discard(completed)
            self._admission_effect_done(completed)

        task.add_done_callback(done)

    async def _wait_exact_callback_task(self, task: asyncio.Task[None]) -> None:
        """Allow accepted callbacks to finish, with a bounded cancellation grace."""

        deadline = time.monotonic() + _ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
            if not done:
                task.cancel()
                raise TimeoutError("ASR_EXACT_CALLBACK_TIMEOUT")
            task.result()
        except asyncio.CancelledError:
            try:
                if not task.done():
                    await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
            finally:
                if not task.done():
                    task.cancel()
            raise

    def _admission_effect_done(self, task: asyncio.Task[Any]) -> None:
        self._asr_admission_effect_tasks.discard(task)
        self._asr_admission_effect_task_turns.pop(task, None)
        self._log_asr_background_task_failure(task)

    async def _finish_admission_invalidation(
        self,
        future: asyncio.Future[tuple[Any, ...]],
        transcript_dispatcher: TranscriptDispatcher,
        correlator: ProviderTurnCorrelator | None,
        namespace: tuple[int, int] | None,
        detector: DetectorRuntime | None,
        *,
        on_settled: Callable[[asyncio.Task[None]], None] | None = None,
    ) -> None:
        """Bound the wait while retaining the actual reservation cleanup owner."""

        async def finish_owned() -> None:
            try:
                bulk_results = await asyncio.shield(future)
                retired_turns = {result.turn_token for result in bulk_results}
                for result in bulk_results:
                    for effect in result.effects:
                        await self._execute_admission_effect(effect)
                while True:
                    pending_effects = tuple(
                        task
                        for task, turn_token in tuple(
                            self._asr_admission_effect_task_turns.items()
                        )
                        if turn_token in retired_turns
                        and task is not asyncio.current_task()
                        and not task.done()
                    )
                    if not pending_effects:
                        break
                    await asyncio.gather(
                        *pending_effects,
                        return_exceptions=True,
                    )
                if correlator is not None and namespace is not None:
                    retired = correlator.retire_namespace(namespace)
                    await self._retire_admission_boundary_proofs(
                        retired.retired_proofs,
                        detector,
                    )
            finally:
                transcript_dispatcher.invalidate_all()

        cleanup = asyncio.create_task(
            finish_owned(),
            name="voice-turn-admission-invalidation-owner",
        )
        self._track_terminal_close_tasks({cleanup})
        if on_settled is not None:
            def report_settled(done: asyncio.Task[None]) -> None:
                try:
                    on_settled(done)
                except Exception:
                    logger.warning("Admission settlement observer failed")

            cleanup.add_done_callback(report_settled)
        deadline = time.monotonic() + _ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS

        async def supervise() -> None:
            done, _ = await asyncio.wait(
                {cleanup}, timeout=max(0.0, deadline - time.monotonic()),
            )
            if not done:
                # Cancellation is a request, never evidence of completion.
                # A resistant owner remains tracked until its actual done
                # callback; neither this timeout nor caller cancellation can
                # publish on_settled early or cancel the ingress future.
                cleanup.cancel()
                raise TimeoutError("ASR_ADMISSION_INVALIDATION_TIMEOUT")
            cleanup.result()

        supervisor = asyncio.create_task(
            supervise(), name="voice-turn-admission-invalidation-deadline",
        )
        self._track_terminal_close_tasks({supervisor})
        # Keep the absolute deadline alive even if the caller is cancelled.
        # Cancellation propagates immediately instead of doing a second,
        # potentially unbounded shielded join of the hidden owner.
        await asyncio.shield(supervisor)

    def _register_admission_capability(
        self,
        lease: DetectorCandidateRejectionLease,
        *,
        kind: RejectionCapabilityKind,
        provider_key: ProviderUtteranceKey | None = None,
    ) -> RejectionCapability | None:
        detector = self._asr_detector
        if detector is None or not lease.belongs_to(detector):
            return None
        existing = next(
            (
                owner.capability
                for owner in self._asr_admission_capabilities.values()
                if not owner.revoked
                and owner.lease == lease
                and owner.capability.kind is kind
                and owner.capability.provider_key == provider_key
            ),
            None,
        )
        if existing is not None:
            return existing
        self._asr_admission_capability_sequence += 1
        capability = RejectionCapability(
            capability_id=self._asr_admission_capability_sequence,
            owner_generation=self._asr_admission_capability_generation,
            kind=kind,
            turn_token=lease.turn_token,
            candidate=lease.shadow_candidate,
            provider_key=provider_key,
        )
        self._asr_admission_capabilities[capability.capability_id] = (
            _AdmissionCapabilityOwner(
                capability=capability,
                lease=lease,
                detector=detector,
                runtime_identity=self._capture_runtime_identity(
                    ingress_token=lease.turn_token.ingress,
                    turn_token=lease.turn_token,
                ),
            )
        )
        return capability

    async def _execute_admission_effect(
        self, effect: AdmissionEffect,
    ) -> _AdmissionResolutionExecution | None:
        if isinstance(effect, CountDiagnostic):
            metric_name = (
                effect.name
                if effect.name.endswith("_count")
                else f"{effect.name}_count"
            )
            self._speaker_rejection_metrics[metric_name] = (
                self._speaker_rejection_metrics.get(metric_name, 0) + 1
            )
            return
        if isinstance(effect, (ScheduleFinalDeadline, ScheduleEvidenceDeadline)):
            if not self._ingress_token_matches(effect.ticket.turn_token.ingress):
                return
            timer_identity = self._capture_runtime_identity(
                ingress_token=effect.ticket.turn_token.ingress,
            )
            timer_record = await self._asr_admission.get_record(effect.ticket.turn_token)
            if (
                not self._runtime_identity_matches(timer_identity)
                or timer_record is None
                or timer_record.record_generation != effect.ticket.record_generation
                or timer_record.terminal_disposition is not None
            ):
                return
            if isinstance(effect, ScheduleEvidenceDeadline):
                hold = timer_record.evidence_hold
                if (
                    hold is None or hold.ticket != effect.ticket
                    or hold.absolute_deadline != effect.absolute_deadline
                ):
                    return
            elif timer_record.deadline_operation_nonce != effect.ticket.operation_nonce:
                return
            old = self._asr_admission_deadline_tasks.get(effect.ticket)
            if old is not None:
                return

            async def expire() -> None:
                try:
                    while (remaining := effect.absolute_deadline - time.monotonic()) > 0:
                        await asyncio.sleep(remaining)
                    if not self._runtime_identity_matches(timer_identity):
                        return
                    event = (
                        EvidenceDeadlineExpired if isinstance(effect, ScheduleEvidenceDeadline)
                        else FinalDeadlineExpired
                    )(ticket=effect.ticket, deadline=effect.absolute_deadline)
                    transaction = next(
                        (item for item in self._asr_provider_exact_intervals.values()
                         if item.turn_token == effect.ticket.turn_token), None,
                    )
                    if transaction is not None:
                        await self._post_exact_interval_event(transaction, event)
                    else:
                        await self._post_admission_event(effect.ticket.turn_token, event)
                except (asyncio.CancelledError, KeyError):
                    return
                finally:
                    if self._asr_admission_deadline_tasks.get(effect.ticket) is asyncio.current_task():
                        self._asr_admission_deadline_tasks.pop(effect.ticket, None)

            self._asr_admission_deadline_tasks[effect.ticket] = asyncio.create_task(
                expire(),
                name="voice-turn-admission-deadline",
            )
            return
        if isinstance(effect, ConstrainRejectionDeadline):
            current = self._asr_admission_rejection_deadlines.get(effect.ticket)
            if current is None or effect.absolute_deadline < current:
                self._asr_admission_rejection_deadlines[effect.ticket] = (
                    effect.absolute_deadline
                )
            execution = self._asr_admission_rejection_executions.get(effect.ticket)
            if execution is not None and (
                execution.absolute_deadline is None
                or effect.absolute_deadline < execution.absolute_deadline
            ):
                execution.absolute_deadline = effect.absolute_deadline
                execution.deadline_changed.set()
            return
        if isinstance(effect, ApplyRejection):
            owner = self._asr_admission_capabilities.get(
                effect.capability.capability_id
            )
            if (
                owner is None
                or owner.revoked
                or owner.capability != effect.capability
                or not self._runtime_identity_matches(owner.runtime_identity)
            ):
                await self._post_admission_event(
                    effect.ticket.turn_token,
                    RejectionStale(effect.ticket),
                )
                return
            constrained = self._asr_admission_rejection_deadlines.get(effect.ticket)
            deadline = effect.absolute_deadline
            if constrained is not None:
                deadline = (
                    constrained if deadline is None else min(deadline, constrained)
                )
            execution = _AdmissionRejectionExecution(
                ticket=effect.ticket,
                absolute_deadline=deadline,
            )
            existing = self._asr_admission_rejection_executions.setdefault(
                effect.ticket,
                execution,
            )
            if existing is not execution:
                return
            sealed_wait_event: asyncio.Event | None = None
            try:
                while True:
                    execution.deadline_changed.clear()
                    deadline = execution.absolute_deadline
                    if deadline is not None and time.monotonic() >= deadline:
                        result = DetectorCandidateRejectionCommitResult.STALE
                        break
                    commit_task = asyncio.create_task(
                        owner.lease.commit_async(deadline=deadline)
                    )
                    constraint_task = asyncio.create_task(
                        execution.deadline_changed.wait()
                    )
                    remaining = (
                        None
                        if deadline is None
                        else max(0.0, deadline - time.monotonic())
                    )
                    done, pending = await asyncio.wait(
                        {commit_task, constraint_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for waiter in pending:
                        waiter.cancel()
                    if commit_task in done:
                        result = await commit_task
                    elif constraint_task in done:
                        await asyncio.gather(commit_task, return_exceptions=True)
                        continue
                    else:
                        await asyncio.gather(
                            commit_task,
                            constraint_task,
                            return_exceptions=True,
                        )
                        result = DetectorCandidateRejectionCommitResult.STALE
                        break
                    if (
                        result
                        is not DetectorCandidateRejectionCommitResult.PRESEAL_READY
                    ):
                        break
                    sealed_wait_event = (
                        self._asr_admission_turn_sealed_events.setdefault(
                            effect.ticket.turn_token,
                            asyncio.Event(),
                        )
                    )
                    waiters = {
                        asyncio.create_task(sealed_wait_event.wait()),
                        asyncio.create_task(execution.deadline_changed.wait()),
                    }
                    remaining = (
                        None
                        if execution.absolute_deadline is None
                        else max(
                            0.0,
                            execution.absolute_deadline - time.monotonic(),
                        )
                    )
                    done, pending = await asyncio.wait(
                        waiters,
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for waiter in pending:
                        waiter.cancel()
                    if not done:
                        result = DetectorCandidateRejectionCommitResult.STALE
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._post_admission_event(
                    effect.ticket.turn_token,
                    RejectionFailed(effect.ticket),
                )
                return
            finally:
                self._asr_admission_rejection_executions.pop(
                    effect.ticket,
                    None,
                )
                self._asr_admission_rejection_deadlines.pop(
                    effect.ticket,
                    None,
                )
                if (
                    sealed_wait_event is not None
                    and self._asr_admission_turn_sealed_events.get(
                        effect.ticket.turn_token
                    )
                    is sealed_wait_event
                ):
                    self._asr_admission_turn_sealed_events.pop(
                        effect.ticket.turn_token,
                        None,
                    )
            applied_kind = {
                DetectorCandidateRejectionCommitResult.ACTIVE_APPLIED: (
                    RejectionCapabilityKind.ACTIVE
                ),
                DetectorCandidateRejectionCommitResult.SEALED_APPLIED: (
                    RejectionCapabilityKind.SEALED
                ),
            }.get(result)
            if applied_kind is None or applied_kind is not effect.capability.kind:
                await self._post_admission_event(
                    effect.ticket.turn_token,
                    RejectionStale(effect.ticket),
                )
                return
            await self._post_admission_event(
                effect.ticket.turn_token,
                RejectionApplied(effect.ticket, applied_kind),
            )
            return
        if isinstance(effect, ResolveReserved):
            return await self._resolve_admission_reservation(effect)
        if isinstance(effect, SettlePartial):
            await self._settle_admission_partial(effect)
            return
        if isinstance(effect, RevokeRejectionCapability):
            owner = self._asr_admission_capabilities.pop(
                effect.capability.capability_id,
                None,
            )
            if owner is not None:
                owner.revoked = True
            if effect.ticket is not None:
                await self._post_admission_event(
                    effect.ticket.turn_token,
                    CapabilityRevoked(effect.ticket),
                )
            return
        if isinstance(effect, PoisonSpeakerAuthorityNamespace):
            self._asr_admission_capability_generation += 1
            for owner in self._asr_admission_capabilities.values():
                owner.revoked = True
            self._asr_admission_capabilities.clear()
            if effect.ticket is not None:
                await self._post_admission_event(
                    effect.ticket.turn_token,
                    SpeakerAuthorityNamespacePoisoned(effect.ticket),
                )
            return
        if isinstance(effect, AbortProviderTransport):
            await self._abort_admission_transport(effect)

    def _retire_admission_record_deadlines(self, ticket: AdmissionResolutionTicket) -> None:
        """Retire only this terminal record's local timers, without self-cancel."""
        current = asyncio.current_task()
        for operation, task in tuple(self._asr_admission_deadline_tasks.items()):
            if (
                operation.turn_token != ticket.turn_token
                or operation.record_generation != ticket.record_generation
                or task is current
                or self._asr_admission_deadline_tasks.get(operation) is not task
            ):
                continue
            self._asr_admission_deadline_tasks.pop(operation, None)
            if not task.done():
                task.cancel()
                self._track_terminal_close_tasks({task})

    async def _resolve_admission_reservation(
        self,
        effect: ResolveReserved,
        *,
        cleanup_owner: _SpeakerDenyCleanupOperation | None = None,
    ) -> _AdmissionResolutionExecution | None:
        final_key = FinalKey.from_turn(effect.turn_token)
        envelope = None
        if effect.disposition is AdmissionDisposition.FORWARD:
            final = effect.final
            if final is None:
                return
            envelope = TranscriptEnvelope(
                turn_token=effect.turn_token,
                provider=final.provider,
                text=final.text.strip(),
            )
        execution = _AdmissionResolutionExecution(effect.ticket)
        existing = self._asr_admission_resolutions.setdefault(final_key, execution)
        if existing.ticket == effect.ticket:
            self._retire_admission_record_deadlines(effect.ticket)
        if existing is not execution:
            late_context = self._asr_admission_final_contexts.pop(
                effect.turn_token,
                None,
            )
            if existing.ticket != effect.ticket:
                if late_context is not None:
                    late_context.settled.set()
                return
            if late_context is not None:
                if existing.late_context is None:
                    existing.late_context = late_context
                else:
                    late_context.settled.set()
            if existing.owner_done and cleanup_owner is None:
                await self._settle_late_admission_context(existing)
            if cleanup_owner is None:
                return existing if existing.owner_done and existing.core_resolution_succeeded else None
        else:
            context = self._asr_admission_final_contexts.pop(effect.turn_token, None)
            existing.late_context = context
            self._schedule_asr_resolution_log(effect, stage="admission_decision")
        ownership = self._asr_provider_turn_ownerships.get(effect.turn_token)
        ownership_cleanup = (
            self._asr_speaker_deny_cleanups.get(ownership.speaker_lease_token)
            if ownership is not None and ownership.speaker_lease_token is not None
            else None
        )
        active_cleanup = cleanup_owner
        if active_cleanup is not None and (
            active_cleanup.settled.is_set()
            or active_cleanup.failure_reason is not None
            or self._asr_speaker_deny_cleanups.get(active_cleanup.lease_token)
            is not active_cleanup
            or active_cleanup.generation != self._asr_deny_cleanup_generation
            or active_cleanup.tickets.get(effect.turn_token) != effect.ticket
            or active_cleanup.context.session_epoch
            != effect.turn_token.ingress.session_epoch
            or (
                ownership is not None
                and not self._deny_cleanup_owns_provider_turn(
                    active_cleanup,
                    ownership,
                )
            )
        ):
            active_cleanup = None
        if active_cleanup is None and cleanup_owner is None:
            active_cleanup = self._active_deny_cleanup_for_resolution(
                effect,
                ownership,
            )
        if effect.disposition is AdmissionDisposition.DROP and cleanup_owner is None:
            if (
                ownership_cleanup is not None
                and not ownership_cleanup.settled.is_set()
                and active_cleanup is None
            ):
                await self._fail_speaker_deny_cleanup(
                    ownership_cleanup,
                    "ASR_DENY_CLEANUP_TRANSCRIPT_CONFLICT",
                )
                return
            if active_cleanup is None:
                dispatcher = self._asr_admission_reservation_dispatchers.pop(
                    final_key,
                    None,
                )
            else:
                dispatcher = self._asr_admission_reservation_dispatchers.get(final_key)
                if dispatcher is None and ownership is not None:
                    dispatcher = ownership.transcript_dispatcher
                if not self._handoff_deny_cleanup_reservation(
                    active_cleanup,
                    final_key,
                    dispatcher,
                    ownership=ownership,
                ):
                    await self._fail_speaker_deny_cleanup(
                        active_cleanup,
                        "ASR_DENY_CLEANUP_TRANSCRIPT_CONFLICT",
                    )
                return
        if cleanup_owner is not None:
            if (
                active_cleanup is not cleanup_owner
                or effect.disposition is not AdmissionDisposition.DROP
            ):
                existing.core_resolution_succeeded = False
                existing.owner_done = True
                return
            dispatcher = cleanup_owner.provisional_reservations.get(final_key)
            if not self._handoff_deny_cleanup_reservation(
                cleanup_owner,
                final_key,
                dispatcher,
                ownership=ownership,
            ):
                existing.core_resolution_succeeded = False
                existing.owner_done = True
                return
        else:
            if effect.disposition is not AdmissionDisposition.DROP:
                dispatcher = self._asr_admission_reservation_dispatchers.pop(
                    final_key,
                    None,
                )
            if ownership is not None and ownership.final_key == final_key:
                ownership.state = _ProviderTurnOwnershipState.RESOLVED
        receipt: TranscriptResolutionReceipt | None = None
        exact_transaction = next(
            (
                transaction
                for transaction in self._asr_provider_exact_intervals.values()
                if transaction.turn_token == effect.turn_token
            ),
            None,
        )
        try:
            if dispatcher is not None:
                receipt = dispatcher.resolve_reserved(
                    final_key,
                    effect.disposition,
                    envelope=envelope,
                )
        except Exception:
            receipt = None
        if cleanup_owner is not None:
            resolved = bool(
                receipt is not None
                and receipt.requested is AdmissionDisposition.DROP
                and (
                    receipt.outcome is TranscriptResolutionOutcome.APPLIED
                    or (
                        receipt.outcome
                        is TranscriptResolutionOutcome.ALREADY_SAME
                        and receipt.existing is AdmissionDisposition.DROP
                    )
                )
            )
        else:
            resolved = bool(
                receipt is not None
                and receipt.requested is effect.disposition
                and (
                    receipt.outcome is TranscriptResolutionOutcome.APPLIED
                    or (
                        receipt.outcome
                        is TranscriptResolutionOutcome.ALREADY_SAME
                        and (
                            exact_transaction is None
                            or receipt.existing is effect.disposition
                        )
                    )
                )
            )
        existing.core_resolution_succeeded = resolved
        self._schedule_asr_resolution_log(
            effect,
            stage="transcript_resolution",
            outcome=receipt.outcome.value if receipt is not None else "missing_receipt",
            applied=resolved,
        )
        if (
            effect.disposition is AdmissionDisposition.DROP
            and exact_transaction is not None
        ):
            exact_transaction.drop_tombstone_succeeded = resolved
        if not resolved:
            if effect.disposition is AdmissionDisposition.DROP:
                try:
                    await self._post_admission_event(
                        effect.turn_token,
                        CoreSettled(effect.ticket, degraded=True),
                    )
                    existing.core_settled = True
                finally:
                    existing.owner_done = True
                    context = existing.late_context
                    existing.late_context = None
                    if context is not None:
                        context.settled.set()
                return
            try:
                await self._post_admission_event(
                    effect.turn_token,
                    CoreSettled(effect.ticket, degraded=True),
                )
                await self._post_admission_event(
                    effect.turn_token,
                    TransportSettled(effect.ticket, degraded=True),
                )
                await self._post_admission_event(
                    effect.turn_token,
                    LifecycleSettled(effect.ticket, degraded=True),
                )
                existing.core_settled = True
                existing.transport_settled = True
                existing.lifecycle_settled = True
            finally:
                existing.settled.set()
                existing.owner_done = True
                context = existing.late_context
                existing.late_context = None
                if context is not None:
                    context.settled.set()
            self._asr_admission_resolutions.pop(final_key, None)
            if ownership is not None:
                self._retire_provider_turn_ownership(ownership)
            return
        context = existing.late_context
        existing.late_context = None
        if context is not None:
            try:
                await self._settle_admission_final(effect.ticket, context)
            except Exception:
                for settlement in (
                    TransportSettled(effect.ticket, degraded=True),
                    LifecycleSettled(effect.ticket, degraded=True),
                ):
                    try:
                        await self._post_admission_event(
                            effect.turn_token,
                            settlement,
                        )
                    except (AdmissionIngressClosedError, KeyError):
                        pass
            finally:
                context.settled.set()
            existing.transport_settled = True
            existing.lifecycle_settled = True
        elif effect.disposition is AdmissionDisposition.ABANDON:
            await self._post_admission_event(
                effect.turn_token,
                TransportSettled(effect.ticket),
            )
            await self._post_admission_event(
                effect.turn_token,
                LifecycleSettled(effect.ticket),
            )
            existing.transport_settled = True
            existing.lifecycle_settled = True
        elif effect.final is not None:
            await self._post_admission_event(
                effect.turn_token,
                TransportSettled(effect.ticket, degraded=True),
            )
            await self._post_admission_event(
                effect.turn_token,
                LifecycleSettled(effect.ticket, degraded=True),
            )
            existing.transport_settled = True
            existing.lifecycle_settled = True
        elif effect.disposition is not AdmissionDisposition.DROP:
            await self._post_admission_event(
                effect.turn_token,
                TransportSettled(effect.ticket, degraded=True),
            )
            await self._post_admission_event(
                effect.turn_token,
                LifecycleSettled(effect.ticket, degraded=True),
            )
            existing.transport_settled = True
            existing.lifecycle_settled = True
        if existing.transport_settled and existing.lifecycle_settled:
            existing.settled.set()
        existing.owner_done = True
        if existing.late_context is not None:
            await self._settle_late_admission_context(existing)
            if (
                cleanup_owner is not None
                and self._asr_admission_resolutions.get(final_key) is None
            ):
                self._asr_admission_resolutions[final_key] = existing
        if existing.core_settled and cleanup_owner is None:
            self._asr_admission_resolutions.pop(final_key, None)
        if (
            ownership is not None
            and effect.disposition is not AdmissionDisposition.DROP
        ):
            self._retire_provider_turn_ownership(ownership)
        return existing

    def _active_deny_cleanup_for_resolution(
        self,
        effect: ResolveReserved,
        ownership: _ProviderTurnOwnership | None,
    ) -> _SpeakerDenyCleanupOperation | None:
        cleanup = (
            self._asr_speaker_deny_cleanups.get(ownership.speaker_lease_token)
            if ownership is not None and ownership.speaker_lease_token is not None
            else self._asr_speaker_deny_cleanups.get(
                self._asr_current_speaker_lease
            )
        )
        if (
            cleanup is None
            or cleanup.settled.is_set()
            or cleanup.failure_reason is not None
            or self._asr_speaker_deny_cleanups.get(cleanup.lease_token) is not cleanup
            or cleanup.generation != self._asr_deny_cleanup_generation
            or cleanup.tickets.get(effect.turn_token) != effect.ticket
            or cleanup.context.session_epoch
            != effect.turn_token.ingress.session_epoch
        ):
            return None
        if ownership is not None and not self._deny_cleanup_owns_provider_turn(
            cleanup,
            ownership,
        ):
            return None
        return cleanup

    def _deny_cleanup_owns_provider_turn(
        self,
        cleanup: _SpeakerDenyCleanupOperation,
        ownership: _ProviderTurnOwnership,
    ) -> bool:
        return bool(
            ownership.speaker_lease_token == cleanup.lease_token
            and ownership.final_key.turn_token == ownership.turn_token
            and cleanup.context.session_ref is ownership.session
            and cleanup.session is ownership.session
            and cleanup.context.session_epoch
            == ownership.runtime_identity.session_epoch
            and cleanup.lifecycle is ownership.lifecycle
            and cleanup.correlator is ownership.correlator
        )

    def _handoff_deny_cleanup_reservation(
        self,
        cleanup: _SpeakerDenyCleanupOperation,
        final_key: FinalKey,
        dispatcher: TranscriptDispatcher | None,
        *,
        ownership: _ProviderTurnOwnership | None,
    ) -> bool:
        if dispatcher is None:
            return False
        if (
            ownership is not None
            and (
                self._asr_provider_turn_ownerships.get(ownership.turn_token)
                is not ownership
                or ownership.final_key != final_key
                or ownership.transcript_dispatcher is not dispatcher
                or not self._deny_cleanup_owns_provider_turn(cleanup, ownership)
            )
        ):
            return False
        registered_dispatcher = self._asr_admission_reservation_dispatchers.get(
            final_key
        )
        if registered_dispatcher is not None and registered_dispatcher is not dispatcher:
            return False
        if registered_dispatcher is None and ownership is None:
            return False
        transferred = cleanup.provisional_reservations.setdefault(
            final_key,
            dispatcher,
        )
        return transferred is dispatcher

    async def _settle_late_admission_context(
        self,
        execution: _AdmissionResolutionExecution,
    ) -> None:
        """Attach one late final context to its exact completed ticket."""

        context = execution.late_context
        if context is None:
            return
        execution.late_context = None
        if execution.core_resolution_succeeded is not True:
            context.settled.set()
            return
        try:
            await self._settle_admission_final(execution.ticket, context)
        except Exception:
            for settlement in (
                TransportSettled(execution.ticket, degraded=True),
                LifecycleSettled(execution.ticket, degraded=True),
            ):
                try:
                    await self._post_admission_event(
                        execution.ticket.turn_token,
                        settlement,
                    )
                except (AdmissionIngressClosedError, KeyError):
                    pass
        finally:
            context.settled.set()
        execution.transport_settled = True
        execution.lifecycle_settled = True
        if execution.core_settled:
            execution.settled.set()
            final_key = FinalKey.from_turn(execution.ticket.turn_token)
            if self._asr_admission_resolutions.get(final_key) is execution:
                self._asr_admission_resolutions.pop(final_key, None)

    def _partial_turn_is_current(self, turn_token: VoiceTurnToken) -> bool:
        lifecycle = self._asr_lifecycle
        sealed = self._asr_sealed_turn_token
        return bool(
            lifecycle is not None
            and self._asr_partial_turn_token == turn_token
            and self._asr_turn_prepared
            and self._ingress_token_matches(turn_token.ingress)
            and lifecycle.snapshot.turn_id == turn_token.turn_id
            and self._asr_audio_dispatcher.active_turn == turn_token
            and (
                lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
                or (
                    lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
                    and sealed is not None
                    and sealed.turn == turn_token
                    and self._transport_token_matches(sealed, lifecycle)
                )
            )
        )

    async def _deliver_independent_asr_preview(
        self,
        turn_token: VoiceTurnToken,
        text: str,
    ) -> None:
        """Deliver one already-admitted display-only partial."""

        if not text or not self._partial_turn_is_current(turn_token):
            return
        lifecycle = self._asr_lifecycle
        assert lifecycle is not None
        if (
            not self._asr_first_partial_recorded
            and self._asr_turn_audio_started_at is not None
        ):
            lifecycle.metrics.first_partial_latency_ms = int(
                (time.monotonic() - self._asr_turn_audio_started_at) * 1_000
            )
            self._asr_first_partial_recorded = True
        try:
            await self._callbacks.on_partial(
                VoicePartialEvent(turn_token=turn_token, text=text)
            )
        except Exception:
            logger.debug(
                "[%s] independent ASR preview delivery failed",
                self.display_name,
            )

    async def _settle_admission_partial(self, effect: SettlePartial) -> None:
        """Apply one reducer-owned terminal verdict to the latest partial."""

        existing = self._asr_partial_settlements.get(effect.turn_token)
        if existing is not None and existing[0] >= effect.record_generation:
            return
        cached = self._asr_quarantined_partials.pop(effect.turn_token, None)
        if not self._partial_turn_is_current(effect.turn_token):
            self._asr_partial_settlements.pop(effect.turn_token, None)
            return
        self._asr_partial_settlements[effect.turn_token] = (
            effect.record_generation,
            effect.disposition,
        )
        if (
            effect.disposition is not AdmissionDisposition.FORWARD
            or effect.turn_token in self._asr_admission_final_contexts
            or cached is None
        ):
            return
        await self._deliver_independent_asr_preview(effect.turn_token, cached)

    def _begin_speaker_deny_cleanup(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        aborts: tuple[AbortProviderTransport, ...],
        *,
        candidate_key: SpeakerShadowCandidateKey | None = None,
        terminal_sequence: int | None = None,
    ) -> _SpeakerDenyCleanupOperation:
        cleanup = self._asr_speaker_deny_cleanups.get(lease_token)
        if cleanup is not None:
            if (
                candidate_key is not None
                and candidate_key != cleanup.context.candidate_key
            ) or (
                terminal_sequence is not None
                and terminal_sequence
                != cleanup.context.terminal_speaker_sequence
            ):
                raise RuntimeError("ASR_DENY_CLEANUP_IDENTITY_MISMATCH")
            for effect in aborts:
                cleanup.tickets.setdefault(effect.turn_token, effect.ticket)
            return cleanup

        candidate = candidate_key or self._asr_current_speaker_candidate
        lifecycle = self._asr_lifecycle
        detector = self._asr_detector
        session = self._asr_session
        if (
            candidate is None
            or lifecycle is None
            or detector is None
            or self._asr_current_speaker_lease != lease_token
            or self._asr_admission_candidate_leases.get(candidate) != lease_token
            or self._asr_deny_transport_state is not DenyTransportState.OPEN
        ):
            raise RuntimeError("ASR_DENY_CLEANUP_IDENTITY_MISMATCH")
        speaker_sequence = (
            terminal_sequence
            if terminal_sequence is not None
            else self._asr_provider_speaker_sequence
        )
        if speaker_sequence <= 0:
            raise RuntimeError("ASR_DENY_CLEANUP_SEQUENCE_INVALID")

        runtime_identity = self._capture_runtime_identity()
        old_transport_generation = lifecycle.snapshot.transport_generation
        context = DenyCleanupContext(
            lease_token=lease_token,
            candidate_key=candidate,
            session_epoch=self._asr_session_epoch,
            transport_generation=old_transport_generation,
            start_generation=self._asr_start_generation,
            detector_epoch=detector.detector_epoch,
            session_ref=session,
            capture_cutoff_sequence=self._asr_last_ingress_sequence,
            terminal_speaker_sequence=speaker_sequence,
        )

        # This await-free block is the DENY linearization point. From here on,
        # no new callback ticket or audio command can target the old transport.
        self._asr_deny_cleanup_generation += 1
        self._asr_deny_transport_state = DenyTransportState.DENY_FENCED
        self._asr_deny_cleanup_active = True  # compatibility mirror only
        self._begin_asr_start_operation()
        self._asr_session = None
        self._asr_provider_exact_session = None
        lifecycle.invalidate_transport()
        audio_dispatcher = self._asr_audio_dispatcher
        audio_dispatcher.abort()
        for task_name in (
            "_asr_transport_task",
            "_asr_warm_expiry_task",
            "_asr_final_watchdog_task",
        ):
            task = getattr(self, task_name, None)
            setattr(self, task_name, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()

        cleanup = _SpeakerDenyCleanupOperation(
            lease_token=lease_token,
            generation=self._asr_deny_cleanup_generation,
            context=context,
            runtime_identity=runtime_identity,
            lifecycle=lifecycle,
            session=session,
            correlator=self._asr_provider_correlator,
            namespace=self._asr_provider_correlator_namespace,
            detector=detector,
            evidence_lease=self._asr_provider_speaker_evidence_lease,
            audio_dispatcher=audio_dispatcher,
            audio_transport_generation=audio_dispatcher.transport_generation,
        )
        self._asr_speaker_deny_cleanups[lease_token] = cleanup
        for effect in aborts:
            cleanup.tickets.setdefault(effect.turn_token, effect.ticket)
        return cleanup

    def _schedule_speaker_deny_cleanup_failure(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        reason_code: str,
    ) -> None:
        cleanup = self._asr_speaker_deny_cleanups.get(lease_token)
        if cleanup is None:
            self._asr_deny_transport_state = DenyTransportState.QUARANTINED
            self._asr_deny_cleanup_active = True
            return
        task = asyncio.create_task(
            self._fail_speaker_deny_cleanup(cleanup, reason_code),
            name=f"speaker-deny-quarantine-{lease_token.lease_nonce}",
        )
        self._track_admission_effect_task(task, None)
        task.add_done_callback(self._admission_effect_done)

    async def _fail_speaker_deny_cleanup(
        self,
        cleanup: _SpeakerDenyCleanupOperation,
        reason_code: str,
    ) -> None:
        if cleanup.failure_reason is not None:
            return
        cleanup.failure_reason = reason_code
        cleanup.degraded = True
        cleanup.incident_id = (
            f"asr-deny-{cleanup.context.session_epoch}-"
            f"{cleanup.context.transport_generation}-"
            f"{cleanup.generation}"
        )
        self._asr_deny_transport_state = DenyTransportState.QUARANTINED
        self._asr_deny_cleanup_active = True
        self._speaker_rejection_metrics["speaker_deny_cleanup_failed_count"] += 1
        lifecycle = cleanup.lifecycle
        if lifecycle is not None:
            try:
                lifecycle.block(
                    reason_code="ASR_DENY_CLEANUP_FAILED",
                    incident_id=cleanup.incident_id,
                )
            except Exception:
                pass
            identity = self._capture_runtime_identity()
            await self._send_asr_lifecycle_state(
                VoiceLifecycleState.BLOCKED,
                provider=self._asr_provider or "unknown",
                session_epoch=self._asr_session_epoch,
                expected_identity=identity,
                reason_code="ASR_DENY_CLEANUP_FAILED",
                incident_id=cleanup.incident_id,
            )
            await self._send_asr_status(
                "ASR_DENY_CLEANUP_FAILED",
                self._asr_provider or "unknown",
                session_epoch=self._asr_session_epoch,
                expected_identity=identity,
                reason_code="ASR_DENY_CLEANUP_FAILED",
                incident_id=cleanup.incident_id,
            )
        cleanup.settled.set()

    async def _settle_speaker_deny_ticket(
        self,
        cleanup: _SpeakerDenyCleanupOperation,
        ticket: AdmissionResolutionTicket,
    ) -> None:
        if ticket in cleanup.settled_tickets:
            return
        for settlement in (
            TransportSettled(ticket, degraded=cleanup.degraded),
            LifecycleSettled(ticket, degraded=cleanup.degraded),
        ):
            try:
                await self._post_admission_event(ticket.turn_token, settlement)
            except Exception:
                cleanup.degraded = True
        cleanup.settled_tickets.add(ticket)
        final_key = FinalKey.from_turn(ticket.turn_token)
        execution = self._asr_admission_resolutions.get(final_key)
        if execution is not None and execution.ticket == ticket:
            execution.transport_settled = True
            execution.lifecycle_settled = True
            execution.settled.set()
            if execution.core_settled:
                self._asr_admission_resolutions.pop(final_key, None)

    async def _finish_speaker_deny_cleanup(
        self,
        cleanup: _SpeakerDenyCleanupOperation,
    ) -> None:
        self._asr_deny_transport_state = DenyTransportState.RETIRING
        failure: str | None = None
        retired_proofs: tuple[BoundaryProof, ...] = ()

        async def close_captured_session() -> None:
            if cleanup.session is not None:
                await cleanup.session.close()

        try:
            receipt = await asyncio.wait_for(
                cleanup.audio_dispatcher.abort_and_join(
                    close_session=close_captured_session,
                    transport_generation=cleanup.audio_transport_generation,
                ),
                timeout=_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS,
            )
            cleanup.transport_safe = bool(
                receipt.active_writer_joined and receipt.session_closed
            )
            if not cleanup.transport_safe:
                failure = "ASR_DENY_CLEANUP_TRANSPORT_NOT_RETIRED"
        except Exception:
            failure = "ASR_DENY_CLEANUP_TRANSPORT_NOT_RETIRED"

        if failure is None and not await self._wait_provider_callback_tickets(
            cleanup.context
        ):
            failure = "ASR_DENY_CLEANUP_CALLBACKS_INFLIGHT"

        # Seal membership only after pre-fence callbacks have left. A callback
        # that reserved before the fence remains visible in this registry even
        # when terminal parent attachment was rejected.
        ownerships = tuple(
            ownership
            for ownership in self._asr_provider_turn_ownerships.values()
            if ownership.speaker_lease_token == cleanup.lease_token
            or (
                ownership.runtime_identity.session is cleanup.session
                and ownership.runtime_identity.session_epoch
                == cleanup.context.session_epoch
            )
        )
        for ownership in ownerships:
            if not self._handoff_deny_cleanup_reservation(
                cleanup,
                ownership.final_key,
                ownership.transcript_dispatcher,
                ownership=ownership,
            ):
                failure = failure or "ASR_DENY_CLEANUP_TRANSCRIPT_CONFLICT"
        for binding in cleanup.frozen_children:
            final_key = FinalKey.from_turn(binding.turn_token)
            dispatcher = self._asr_admission_reservation_dispatchers.get(final_key)
            if dispatcher is not None:
                transferred = cleanup.provisional_reservations.setdefault(
                    final_key,
                    dispatcher,
                )
                if transferred is not dispatcher:
                    failure = failure or "ASR_DENY_CLEANUP_TRANSCRIPT_CONFLICT"
        for final_key, dispatcher in tuple(
            self._asr_admission_reservation_dispatchers.items()
        ):
            if (
                final_key.turn_token.ingress.session_epoch
                == cleanup.context.session_epoch
            ):
                transferred = cleanup.provisional_reservations.setdefault(
                    final_key,
                    dispatcher,
                )
                if transferred is not dispatcher:
                    failure = failure or "ASR_DENY_CLEANUP_TRANSCRIPT_CONFLICT"

        drop_receipts: dict[FinalKey, TranscriptResolutionReceipt] = {}
        for final_key, dispatcher in tuple(cleanup.provisional_reservations.items()):
            ticket = cleanup.tickets.get(final_key.turn_token)
            if ticket is not None:
                await self._resolve_admission_reservation(
                    ResolveReserved(ticket=ticket, final=None),
                    cleanup_owner=cleanup,
                )
                execution = self._asr_admission_resolutions.get(final_key)
                if (
                    execution is None
                    or execution.core_resolution_succeeded is not True
                ):
                    failure = failure or "ASR_DENY_CLEANUP_TRANSCRIPT_UNSAFE"
                else:
                    drop_receipts[final_key] = TranscriptResolutionReceipt(
                        final_key,
                        AdmissionDisposition.DROP,
                        TranscriptResolutionOutcome.APPLIED,
                    )
                continue
            resolution: TranscriptResolutionReceipt | None = None
            try:
                resolution = dispatcher.resolve_reserved(
                    final_key,
                    AdmissionDisposition.DROP,
                )
            except Exception:
                resolution = None
            if resolution is None or not (
                resolution.outcome is TranscriptResolutionOutcome.APPLIED
                or (
                    resolution.outcome is TranscriptResolutionOutcome.ALREADY_SAME
                    and resolution.existing is AdmissionDisposition.DROP
                )
            ):
                failure = failure or "ASR_DENY_CLEANUP_TRANSCRIPT_UNSAFE"
                continue
            drop_receipts[final_key] = resolution
            if (
                resolution.existing is not None
                and resolution.existing is not AdmissionDisposition.DROP
            ):
                failure = failure or "ASR_DENY_CLEANUP_TRANSCRIPT_CONFLICT"
        cleanup.text_safe = bool(
            failure is None
            and len(drop_receipts) == len(cleanup.provisional_reservations)
        )

        correlator = cleanup.correlator
        if correlator is not None and cleanup.namespace is not None:
            try:
                retired = correlator.retire_namespace(cleanup.namespace)
                retired_proofs = retired.retired_proofs
            except Exception:
                failure = failure or "ASR_DENY_CLEANUP_NAMESPACE_RETIRE_FAILED"
        if retired_proofs:
            try:
                await self._retire_admission_boundary_proofs(
                    retired_proofs,
                    cleanup.detector,
                )
            except Exception:
                failure = failure or "ASR_DENY_CLEANUP_BOUNDARY_RETIRE_FAILED"
        abandon = getattr(
            cleanup.detector,
            "abandon_provider_speaker_evidence_lease",
            None,
        )
        if callable(abandon) and cleanup.evidence_lease is not None:
            try:
                await abandon(cleanup.evidence_lease)
            except Exception:
                failure = failure or "ASR_DENY_CLEANUP_EVIDENCE_RETIRE_FAILED"

        child_turns = tuple(
            dict.fromkeys(
                (
                    *cleanup.tickets,
                    *(
                        item.turn_token
                        for item in ownerships
                        if item.child_published
                    ),
                )
            )
        )
        unpublished_ownerships = tuple(
            item
            for item in ownerships
            if not item.child_published and item.turn_token not in cleanup.tickets
        )
        lifecycle = cleanup.lifecycle
        if failure is None and lifecycle is not None:
            try:
                if lifecycle.snapshot.state in {
                    VoiceLifecycleState.PREWARMING,
                    VoiceLifecycleState.ACTIVE,
                    VoiceLifecycleState.DRAINING,
                    VoiceLifecycleState.WARM_IDLE,
                }:
                    lifecycle.transition(VoiceLifecycleEvent.TURN_DENIED)
                identity = self._capture_runtime_identity()
                await self._send_asr_lifecycle_state(
                    VoiceLifecycleState.LOCAL_LISTEN,
                    provider=self._asr_provider or "unknown",
                    session_epoch=self._asr_session_epoch,
                    expected_identity=identity,
                )
            except Exception:
                failure = "ASR_DENY_CLEANUP_LIFECYCLE_FAILED"

        for ticket in tuple(cleanup.tickets.values()):
            if failure is not None:
                break
            try:
                await self._settle_speaker_deny_ticket(cleanup, ticket)
            except Exception:
                failure = "ASR_DENY_CLEANUP_SETTLEMENT_FAILED"

        # Core DROP acknowledgements are generated by the transcript worker;
        # quiescence is only a join here, never the proof. Flags below are the
        # authoritative per-child acknowledgements.
        for dispatcher in set(cleanup.provisional_reservations.values()):
            if failure is not None:
                break
            try:
                await asyncio.wait_for(
                    dispatcher.wait_idle(),
                    timeout=_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS,
                )
            except Exception:
                failure = "ASR_DENY_CLEANUP_CORE_SETTLEMENT_FAILED"
        for turn_token, ticket in cleanup.tickets.items():
            execution = self._asr_admission_resolutions.get(
                FinalKey.from_turn(turn_token)
            )
            if execution is not None and not (
                execution.core_settled
                and execution.transport_settled
                and execution.lifecycle_settled
            ):
                failure = failure or "ASR_DENY_CLEANUP_SETTLEMENT_FAILED"

        if failure is not None or not cleanup.text_safe or not cleanup.transport_safe:
            await self._fail_speaker_deny_cleanup(
                cleanup,
                failure or "ASR_DENY_CLEANUP_UNPROVEN",
            )
            return

        self._asr_provider_correlator = None
        self._asr_provider_correlator_namespace = None
        self._asr_deferred_provider_started_keys.clear()
        self._asr_turn_prepared = False
        for turn_token in child_turns:
            self._asr_quarantined_partials.pop(turn_token, None)
            self._asr_partial_settlements.pop(turn_token, None)
            self._asr_speaker_authority_pending_turns.pop(turn_token, None)
            self._asr_speaker_authoritative_turns.discard(turn_token)
            self._asr_admission_turn_leases.pop(turn_token, None)
            ownership = self._asr_provider_turn_ownerships.pop(turn_token, None)
            if ownership is not None:
                ownership.state = _ProviderTurnOwnershipState.RETIRED
            try:
                retired_child = await self._asr_admission_ingress.retire_turn(
                    turn_token
                )
                if (
                    not retired_child
                    and await self._asr_admission.get_record(turn_token)
                    is not None
                ):
                    raise RuntimeError("ASR_DENY_CLEANUP_CHILD_RETIRE_FAILED")
            except Exception:
                await self._fail_speaker_deny_cleanup(
                    cleanup,
                    "ASR_DENY_CLEANUP_CHILD_RETIRE_FAILED",
                )
                return
        for ownership in unpublished_ownerships:
            if (
                self._asr_provider_turn_ownerships.get(ownership.turn_token)
                is ownership
            ):
                self._retire_provider_turn_ownership(ownership)
        self._asr_provider_started_turns = {
            key: turn
            for key, turn in self._asr_provider_started_turns.items()
            if turn not in child_turns
        }
        self._asr_partial_turn_token = None
        self._asr_sealed_turn_token = None
        self._asr_provider_candidate_fence = None
        self._asr_sealed_provider_key = None
        self._asr_provider_speaker_evidence_lease = None
        candidate = cleanup.context.candidate_key
        self._asr_admission_candidate_leases.pop(candidate, None)
        self._asr_admission_candidate_turns.pop(candidate, None)
        self._asr_current_speaker_lease = None
        self._asr_current_speaker_candidate = None
        try:
            retired = await self._asr_admission_ingress.retire_speaker_lease(
                cleanup.lease_token
            )
            if not retired:
                raise RuntimeError("ASR_DENY_CLEANUP_LEASE_RETIRE_FAILED")
        except Exception:
            await self._fail_speaker_deny_cleanup(
                cleanup,
                "ASR_DENY_CLEANUP_LEASE_RETIRE_FAILED",
            )
            return
        for final_key, dispatcher in cleanup.provisional_reservations.items():
            if not dispatcher.retire_resolution(
                final_key,
                retired_transport=final_key.turn_token.ingress,
            ):
                await self._fail_speaker_deny_cleanup(
                    cleanup,
                    "ASR_DENY_CLEANUP_TOMBSTONE_RETIRE_FAILED",
                )
                return
            if (
                self._asr_admission_reservation_dispatchers.get(final_key)
                is dispatcher
            ):
                self._asr_admission_reservation_dispatchers.pop(final_key, None)

        cleanup.fully_settled = True
        cleanup.settled.set()
        self._asr_rearm_cutoff_sequence = self._asr_last_ingress_sequence
        self._asr_rearm_last_sequence = self._asr_rearm_cutoff_sequence
        self._asr_rearm_last_captured_at = self._asr_last_captured_at
        self._asr_deny_transport_state = DenyTransportState.WAIT_SILENCE
        self._asr_deny_cleanup_active = False
        self._asr_speaker_deny_cleanups.pop(cleanup.lease_token, None)

    async def _abort_admission_transport(
        self,
        effect: AbortProviderTransport,
        *,
        cleanup: _SpeakerDenyCleanupOperation | None = None,
    ) -> None:
        lease_token = effect.speaker_lease_token
        if lease_token is None:
            ownership = self._asr_provider_turn_ownerships.get(effect.turn_token)
            lifecycle = self._asr_lifecycle
            if lifecycle is not None and (
                lifecycle.current_turn_token == effect.turn_token
                or lifecycle.pending_turn_token == effect.turn_token
            ):
                lifecycle.invalidate_transport()
                self._asr_audio_dispatcher.abort(effect.turn_token)
            correlator = self._asr_provider_correlator
            degraded = False
            if correlator is not None:
                try:
                    retired = correlator.abandon_turn(effect.turn_token)
                    await self._retire_admission_boundary_proofs(
                        retired.retired_proofs,
                        self._asr_detector,
                    )
                except Exception:
                    degraded = True
            for settlement in (
                TransportSettled(effect.ticket, degraded=degraded),
                LifecycleSettled(effect.ticket, degraded=degraded),
            ):
                try:
                    await self._post_admission_event(effect.turn_token, settlement)
                except (AdmissionIngressClosedError, KeyError):
                    pass
            if ownership is not None:
                self._retire_provider_turn_ownership(ownership)
            return
        cleanup = cleanup or self._begin_speaker_deny_cleanup(lease_token, (effect,))
        cleanup.tickets.setdefault(effect.turn_token, effect.ticket)
        if cleanup.settled.is_set():
            await self._settle_speaker_deny_ticket(cleanup, effect.ticket)
            return
        current = asyncio.current_task()
        if cleanup.owner_task is None:
            cleanup.owner_task = current
            await self._finish_speaker_deny_cleanup(cleanup)
            return
        if cleanup.owner_task is current:
            return
        await asyncio.shield(cleanup.settled.wait())

    async def _retire_admission_boundary_proofs(
        self,
        proofs: tuple[BoundaryProof, ...],
        detector: DetectorRuntime | None,
        *,
        completion: bool = False,
    ) -> None:
        if detector is None:
            for proof in proofs:
                self._asr_provider_boundary_completions.pop(proof, None)
                snapshot = self._asr_provider_boundary_proofs.pop(
                    proof.proof_id,
                    None,
                )
                if snapshot is not None:
                    self._speaker_rejection_metrics[
                        "admission_boundary_proof_retired_count"
                    ] += 1
            return
        identity = self._capture_runtime_identity()
        owned_proofs = tuple(
            (
                proof,
                self._asr_provider_boundary_proofs.get(proof.proof_id),
                self._asr_provider_boundary_completions.get(proof),
            )
            for proof in proofs
        )

        async def retire_unsettled_completion(reason_code: str) -> None:
            # The correlator has already consumed these proofs. Move their
            # cleanup responsibility to one tracked session retirement before
            # yielding; repeated callers cannot schedule unbounded retries.
            claimed = False
            for old_proof, old_snapshot, old_owner in owned_proofs:
                if (
                    old_snapshot is None
                    or (old_owner is not None and old_owner.detector is not detector)
                    or self._asr_provider_boundary_proofs.get(old_proof.proof_id)
                    is not old_snapshot
                    or self._asr_provider_boundary_completions.get(old_proof)
                    is not old_owner
                ):
                    continue
                self._asr_provider_boundary_proofs.pop(old_proof.proof_id, None)
                self._asr_provider_boundary_completions.pop(old_proof, None)
                self._speaker_rejection_metrics[
                    "admission_boundary_proof_retired_count"
                ] += 1
                claimed = True
            if (
                not claimed
                or identity.detector is not detector
                or not self._runtime_identity_matches(identity)
            ):
                return
            self._asr_audio_dispatcher.abort()
            task = asyncio.create_task(
                self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    status_code="ASR_AUDIO_ORDERING_FAILED",
                    reason_code=reason_code,
                    expected_identity=identity,
                ),
                name="asr-boundary-completion-retirement",
            )
            self._asr_owned_cleanup_tasks.add(task)
            task.add_done_callback(self._owned_cleanup_done)
            # The failure handler installs its epoch fence before its first
            # await. Do not join it here: invalidation may itself join this
            # admission effect (or the effect awaiting this operation).
            await asyncio.sleep(0)

        for proof in proofs:
            snapshot = self._asr_provider_boundary_proofs.get(
                proof.proof_id,
                None,
            )
            if snapshot is not None:
                owner = self._asr_provider_boundary_completions.get(proof)
                if owner is not None and (
                    owner.snapshot is not snapshot or owner.detector is not detector
                ):
                    continue
                if completion and owner is not None:
                    complete = getattr(
                        detector, "complete_provider_speaker_boundary", None
                    )
                    if not callable(complete):
                        await retire_unsettled_completion(
                            "ASR_BOUNDARY_COMPLETION_UNSUPPORTED"
                        )
                        return
                    try:
                        result = await asyncio.wait_for(
                            complete(
                                snapshot,
                                successor_evidence_lease=owner.successor_evidence_lease,
                                deadline=(
                                    time.monotonic()
                                    + _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS
                                ),
                            ),
                            timeout=_PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS,
                        )
                    except asyncio.CancelledError:
                        await retire_unsettled_completion(
                            "ASR_BOUNDARY_COMPLETION_CANCELLED"
                        )
                        raise
                    except Exception:
                        await retire_unsettled_completion(
                            "ASR_BOUNDARY_COMPLETION_FAILED"
                        )
                        return
                    if result not in {"completed", "already_completed", "stale"}:
                        # A pending receipt is not a completed proof. Retire the
                        # physical session instead of revoking a live successor
                        # and continuing with inconsistent evidence ownership.
                        await retire_unsettled_completion(
                            "ASR_BOUNDARY_COMPLETION_UNSETTLED"
                        )
                        return
                else:
                    await self._retire_provider_speaker_boundary_unknown(
                        detector,
                        identity,
                        snapshot,
                    )
                if self._asr_provider_boundary_proofs.get(proof.proof_id) is not snapshot:
                    continue
                self._asr_provider_boundary_proofs.pop(proof.proof_id, None)
                if self._asr_provider_boundary_completions.get(proof) is owner:
                    self._asr_provider_boundary_completions.pop(proof, None)
                self._speaker_rejection_metrics[
                    "admission_boundary_proof_retired_count"
                ] += 1

    async def _settle_admission_final(
        self,
        ticket: AdmissionResolutionTicket,
        context: _AdmissionFinalContext,
    ) -> None:
        transaction = self._asr_provider_exact_intervals.get(context.provider_key)
        if not (
            transaction is not None
            and transaction.turn_token == context.turn_token
            and transaction.runtime_identity == context.runtime_identity
            and self._runtime_identity_matches(context.runtime_identity)
            and context.lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
        ):
            await self._settle_admission_final_owned(ticket, context)
            return
        # Publish before the first await and before exposing WARM_IDLE/ACTIVE.
        # Submit owns no new queue: it waits before interpreting lifecycle or
        # dispatching live PCM, so the reserved pending prefix stays first.
        handoff = _PendingTurnHandoff(
            self._capture_runtime_identity(ingress_token=context.turn_token.ingress),
            asyncio.get_running_loop().create_future(),
        )
        self._asr_pending_turn_handoff = handoff
        completed = False
        failure_reason = "ASR_PENDING_TURN_HANDOFF_FAILED"
        try:
            async with asyncio.timeout(_EXACT_PENDING_HANDOFF_TIMEOUT_SECONDS):
                await self._settle_admission_final_owned(ticket, context)
            completed = self._runtime_identity_matches(handoff.identity)
        except _PendingTurnPreparationError as exc:
            failure_reason = str(exc)
            raise
        finally:
            failed_current = not completed and self._runtime_identity_matches(handoff.identity)
            if failed_current:
                # Error invalidation joins this effect: never await it while
                # holding the final lock. The failed gate blocks new PCM until
                # the owned cleanup installs its generation fence and resets it.
                cleanup = asyncio.create_task(
                    self._handle_independent_asr_error(
                        context.epoch, context.provider,
                        status_code="ASR_CORE_TRANSCRIPT_BACKPRESSURE",
                        reason_code=failure_reason,
                        expected_identity=handoff.identity,
                        failed_operation="final_audio_handoff",
                        failed_check="pending_turn_unsettled",
                        notification_timeout_seconds=(
                            _EXACT_HANDOFF_FAILURE_NOTIFICATION_TIMEOUT_SECONDS
                        ),
                    ),
                    name="asr-pending-turn-handoff-failure",
                )
                self._asr_owned_cleanup_tasks.add(cleanup)
                cleanup.add_done_callback(self._owned_cleanup_done)
            if not failed_current and self._asr_pending_turn_handoff is handoff:
                self._asr_pending_turn_handoff = None
            if not handoff.completion.done():
                handoff.completion.set_result(completed)

    def has_pending_turn_handoff(self, ingress_token: VoiceIngressToken) -> bool:
        """Authorize bounded Core ingress storage for this physical owner only."""

        handoff = getattr(self, "_asr_pending_turn_handoff", None)
        return bool(
            handoff is not None
            and not handoff.completion.done()
            and handoff.identity.ingress_token == ingress_token
            and self._runtime_identity_matches(handoff.identity)
        )

    async def _await_pending_turn_handoff(self, identity: _AsrRuntimeIdentity) -> bool:
        """Wait without cancelling the activation owner or replaying input."""

        handoff = getattr(self, "_asr_pending_turn_handoff", None)
        if handoff is None:
            return True
        done, _ = await asyncio.wait(
            {handoff.completion}, timeout=_EXACT_PENDING_HANDOFF_TIMEOUT_SECONDS,
        )
        completed = bool(done and handoff.completion.result())
        return bool(completed and self._runtime_identity_matches(identity))

    async def _settle_admission_final_owned(
        self,
        ticket: AdmissionResolutionTicket,
        context: _AdmissionFinalContext,
    ) -> None:
        """Settle Provider and lifecycle after disposition is tombstoned."""

        degraded = False
        exact_transaction = self._asr_provider_exact_intervals.get(context.provider_key)
        bounded_exact_notification = bool(
            exact_transaction is not None
            and exact_transaction.turn_token == context.turn_token
            and exact_transaction.runtime_identity == context.runtime_identity
        )
        owned_lease_token = self._asr_admission_turn_leases.get(context.turn_token)
        successor_present = False
        detector = context.detector
        fence = context.provider_fence
        if detector is not None and fence is not None:
            try:
                completed = await detector.complete_provider_candidate(fence)
            except Exception:
                completed = None
            if completed is None:
                degraded = True
            else:
                successor_present = completed
        lifecycle = context.lifecycle
        owns_current_turn = (
            not context.audio_handoff_completed
            and self._runtime_identity_matches(context.runtime_identity)
            and self._asr_lifecycle is lifecycle
            and lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
            and self._asr_sealed_turn_token == context.sealed_token
            and (
                context.provider_key is None
                or self._asr_sealed_provider_key == context.provider_key
            )
        )
        has_pending_turn = context.has_pending_turn
        if owns_current_turn:
            pending_token = lifecycle.pending_turn_token
            has_pending_turn = bool(
                has_pending_turn
                or lifecycle.has_pending_turn
                or (
                    lifecycle.provider_policy.endpoint_authority == "provider"
                    and lifecycle.has_pending_turn_identity
                    and pending_token is not None
                    and pending_token in self._asr_provider_started_turns.values()
                )
            )
        if owns_current_turn:
            lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
            self._asr_turn_prepared = False
            self._asr_received_audio = False
            self._asr_sealed_turn_token = None
            self._asr_provider_candidate_fence = None
            if context.provider_key is not None:
                self._asr_sealed_provider_key = None
            self._asr_turn_endpointed_at = None
            if self._asr_partial_turn_token == context.turn_token:
                self._asr_partial_turn_token = None
            self._asr_quarantined_partials.pop(context.turn_token, None)
            self._asr_partial_settlements.pop(context.turn_token, None)
            self._asr_speaker_authority_pending_turns.pop(
                context.turn_token,
                None,
            )
            self._asr_speaker_authoritative_turns.discard(context.turn_token)
            if successor_present and not has_pending_turn:
                lifecycle.preserve_unconfirmed_pending_audio()
            if not has_pending_turn:
                self._schedule_transport_warm_expiry(
                    context.epoch,
                    expected_state=VoiceLifecycleState.WARM_IDLE,
                )
        elif not context.audio_handoff_completed:
            degraded = True
        lease = self._asr_smart_turn_lease
        if (
            owns_current_turn
            and lease is not None
            and lease.token == context.turn_token
        ):
            self._asr_smart_turn_lease = None
            try:
                await lease.release()
            except Exception:
                degraded = True
        correlator = context.correlator
        if context.provider_key is not None:
            if correlator is None:
                degraded = True
            else:
                try:
                    completion = correlator.complete(context.provider_key, ticket)
                    await self._retire_admission_boundary_proofs(
                        completion.retired_proofs,
                        detector,
                        completion=True,
                    )
                    if not completion.completed:
                        degraded = True
                except Exception:
                    degraded = True
            if (
                self._asr_provider_correlator is correlator
                and self._asr_provider_started_turns.get(context.provider_key)
                == context.turn_token
            ):
                self._asr_provider_started_turns.pop(context.provider_key, None)
        notify_lifecycle = (
            self._send_exact_lifecycle_state
            if bounded_exact_notification else self._send_asr_lifecycle_state
        )
        delivered = context.audio_handoff_completed or bool(
            owns_current_turn and await notify_lifecycle(
                VoiceLifecycleState.WARM_IDLE,
                provider=context.provider,
                session_epoch=context.epoch,
                expected_identity=context.runtime_identity,
            )
        )
        if not delivered:
            degraded = True
        if not context.audio_handoff_completed and has_pending_turn and (
            delivered
            or (
                bounded_exact_notification
                and owns_current_turn
                and self._runtime_identity_matches(context.runtime_identity)
            )
        ):
            # A display timeout is not a failed final. Only the same physical
            # owner may activate its pending turn after this optional await.
            if bounded_exact_notification:
                await self._activate_pending_independent_turn(
                    context.epoch, bounded_notification=True,
                )
            else:
                await self._activate_pending_independent_turn(context.epoch)
        if detector is not None and fence is not None:
            try:
                await detector.release_deferred_turn()
            except Exception:
                degraded = True
        await self._post_admission_event(
            context.turn_token,
            TransportSettled(ticket, degraded=degraded),
        )
        await self._post_admission_event(
            context.turn_token,
            LifecycleSettled(ticket, degraded=degraded),
        )
        lease_token = None
        if (
            owned_lease_token is not None
            and self._asr_admission_turn_leases.get(context.turn_token)
            == owned_lease_token
        ):
            lease_token = self._asr_admission_turn_leases.pop(
                context.turn_token,
                None,
            )
        if lease_token is not None and lease_token not in (
            self._asr_admission_turn_leases.values()
        ):
            try:
                await self._asr_admission_ingress.retire_speaker_lease(lease_token)
            except (AdmissionIngressClosedError, KeyError):
                pass
            for candidate, bound_lease in tuple(
                self._asr_admission_candidate_leases.items()
            ):
                if bound_lease == lease_token:
                    self._asr_admission_candidate_leases.pop(candidate, None)
                    if (
                        self._asr_current_speaker_candidate == candidate
                        and self._asr_current_speaker_lease == lease_token
                    ):
                        self._asr_current_speaker_candidate = None
                        self._asr_current_speaker_lease = None

    def _capture_transport_token(
        self,
        lifecycle: VoiceInputLifecycleController,
    ) -> VoiceTransportToken:
        return VoiceTransportToken(
            turn=self._capture_turn_token(lifecycle),
            transport_generation=lifecycle.snapshot.transport_generation,
        )

    def _ingress_token_matches(self, token: VoiceIngressToken) -> bool:
        return bool(
            token.session_epoch == self._asr_session_epoch
            and token.audio_generation == self._asr_audio_generation
        )

    def _transport_token_matches(
        self,
        token: VoiceTransportToken,
        lifecycle: VoiceInputLifecycleController,
    ) -> bool:
        snapshot = lifecycle.snapshot
        return bool(
            self._asr_lifecycle is lifecycle
            and self._ingress_token_matches(token.turn.ingress)
            and token.turn.turn_id == snapshot.turn_id
            and token.transport_generation == snapshot.transport_generation
        )

    def _asr_audio_command_is_valid(
        self,
        turn_token: VoiceTurnToken,
        session_ref: Any,
    ) -> bool:
        lifecycle = self._asr_lifecycle
        detector = self._asr_detector
        return bool(
            lifecycle is not None
            and detector is not None
            and self._asr_session is session_ref
            and self._ingress_token_matches(turn_token.ingress)
            and lifecycle.snapshot.turn_id == turn_token.turn_id
            and self._asr_endpointing_ready(lifecycle, detector, turn_token)
        )

    def _asr_endpointing_ready(
        self,
        lifecycle: VoiceInputLifecycleController,
        detector: DetectorRuntime | None,
        turn_token: VoiceTurnToken,
    ) -> bool:
        """Accept provider authority without manufacturing a SmartTurn lease."""

        if detector is None:
            return False
        if not _uses_smart_turn_endpointing(lifecycle.provider_policy):
            return True
        return detector.endpointing_ready(turn_token)

    async def _record_asr_dispatcher_wire_audio(
        self,
        turn_token: VoiceTurnToken,
        session_ref: Any,
        byte_count: int,
    ) -> None:
        if byte_count <= 0:
            return
        self._observe_pipeline_audio(
            "provider_audio_written", turn_token.ingress, byte_count // 2,
            turn_id=turn_token.turn_id, transport_current=self._asr_session is session_ref,
        )
        self._sync_provider_wire_metrics(
            session_ref,
            fallback_audio_bytes=byte_count,
        )
        if self._asr_session is session_ref:
            self._asr_received_audio = True
            self._asr_audio_bytes += byte_count
            lifecycle = self._asr_lifecycle
            if lifecycle is not None:
                lifecycle.metrics.provider_wire_sequence = (
                    self._asr_audio_dispatcher.provider_wire_sequence
                )
                lifecycle.metrics.asr_audio_command_queue_ms = (
                    self._asr_audio_dispatcher.asr_audio_command_queue_ms
                )

    async def _handle_asr_audio_dispatcher_failure(
        self,
        turn_token: VoiceTurnToken,
        error: BaseException,
    ) -> None:
        if not self._ingress_token_matches(turn_token.ingress):
            return
        identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=turn_token,
        )
        status_code = (
            "ASR_STREAM_BACKPRESSURE"
            if "BACKPRESSURE" in str(error)
            else "ASR_INDEPENDENT_STREAM_FAILED"
        )
        await self._handle_independent_asr_error(
            identity.session_epoch,
            identity.provider or "unknown",
            status_code=status_code,
            expected_identity=identity,
        )

    async def _handle_asr_detector_dispatcher_failure(
        self,
        envelope: CoreDetectorEventEnvelope,
        error: BaseException,
    ) -> None:
        detector = self._asr_detector
        lifecycle = self._asr_lifecycle
        event = envelope.event
        if (
            envelope.session_epoch != self._asr_session_epoch
            or detector is not envelope.detector_ref
            or lifecycle is not envelope.lifecycle_ref
            or detector is None
            or lifecycle is None
            or event.ingress.detector_epoch != detector.detector_epoch
            or not self._ingress_token_matches(event.ingress.ingress_token)
        ):
            return
        logger.error(
            "[%s] detector event dispatcher failed epoch=%s",
            self.display_name,
            envelope.session_epoch,
            exc_info=(type(error), error, error.__traceback__),
        )
        identity = self._capture_runtime_identity(
            ingress_token=event.ingress.ingress_token,
        )
        await self._handle_independent_asr_error(
            identity.session_epoch,
            identity.provider or "unknown",
            status_code="ASR_ENDPOINTING_FAILED",
            expected_identity=identity,
        )

    def _detector_envelope_is_current(
        self,
        envelope: CoreDetectorEventEnvelope,
    ) -> bool:
        detector = self._asr_detector
        lifecycle = self._asr_lifecycle
        event = envelope.event
        return bool(
            envelope.session_epoch == self._asr_session_epoch
            and detector is envelope.detector_ref
            and lifecycle is envelope.lifecycle_ref
            and detector is not None
            and lifecycle is not None
            and event.ingress.detector_epoch == detector.detector_epoch
            and self._ingress_token_matches(event.ingress.ingress_token)
        )

    async def _dispatch_asr_detector_event(
        self,
        envelope: CoreDetectorEventEnvelope,
    ) -> None:
        event = envelope.event
        detector = self._asr_detector
        lifecycle = self._asr_lifecycle
        if not self._detector_envelope_is_current(envelope):
            stale_metrics = getattr(envelope.lifecycle_ref, "metrics", None)
            if stale_metrics is not None:
                stale_metrics.detector_stale_event_count += 1
            return
        assert detector is not None
        assert lifecycle is not None
        lifecycle.metrics.smart_turn_inference_ms = detector.smart_turn_evaluation_ms
        lifecycle.metrics.smart_turn_stale_result_count = (
            detector.smart_turn_stale_result_count
        )
        lifecycle.metrics.smart_turn_coalesced_evaluation_count = (
            detector.smart_turn_coalesced_evaluation_count
        )
        if isinstance(event, DetectorRuntimeEvent):
            identity = self._capture_runtime_identity(
                ingress_token=event.ingress.ingress_token,
            )
            await self._handle_independent_asr_error(
                envelope.session_epoch,
                identity.provider or "unknown",
                status_code=(
                    "ASR_INGRESS_BACKPRESSURE"
                    if event.kind == "audio_backpressure"
                    else "ASR_ENDPOINTING_FAILED"
                ),
                expected_identity=identity,
            )
            return
        if isinstance(event, DetectorTransportPrewarmEvent):
            await self._handle_transport_prewarm_event(
                event,
                detector,
                lifecycle,
                envelope.session_epoch,
            )
            return
        if isinstance(event, DetectorPrewarmEvent):
            await self._handle_detector_prewarm_event(
                event,
                detector,
                lifecycle,
                envelope.session_epoch,
            )
            return
        if isinstance(event, DetectorActivityEvent):
            await self._handle_independent_asr_activity(
                event.activity,
                envelope.session_epoch,
            )
            if not self._detector_envelope_is_current(envelope):
                return
            lifecycle = self._asr_lifecycle
            assert lifecycle is envelope.lifecycle_ref
            if event.activity not in {
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.SPEECH_RESUMED,
            }:
                return
            if lifecycle.snapshot.state is VoiceLifecycleState.DRAINING:
                self._asr_pending_detector_candidate = event.candidate
                return
            if lifecycle.snapshot.state not in {
                VoiceLifecycleState.PREWARMING,
                VoiceLifecycleState.ACTIVE,
            }:
                return
            turn_token = self._capture_turn_token(lifecycle)
            bound = await detector.bind_candidate(event.candidate, turn_token)
            if bound is None:
                return
            if not self._detector_envelope_is_current(envelope):
                return
            if lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE:
                await self._activate_asr_audio_dispatcher(lifecycle, turn_token)
            return
        if not isinstance(event, DetectorTurnEvent):
            return
        turn_token = event.bound_turn.turn_token
        if (
            not self._ingress_token_matches(turn_token.ingress)
            or lifecycle.snapshot.turn_id != turn_token.turn_id
            or not detector.endpointing_ready(turn_token)
        ):
            return
        await self._handle_independent_asr_endpoint(envelope.session_epoch)
        if not self._detector_envelope_is_current(envelope):
            return
        session_ref = self._asr_session
        if session_ref is None:
            return
        if not self._asr_audio_dispatcher.seal(
            turn_token,
            session_ref,
            after_sequence=self._asr_audio_sequence,
        ):
            identity = self._capture_runtime_identity(
                ingress_token=turn_token.ingress,
                turn_token=turn_token,
            )
            await self._handle_independent_asr_error(
                envelope.session_epoch,
                identity.provider or "unknown",
                status_code="ASR_AUDIO_ORDERING_FAILED",
                expected_identity=identity,
            )

    async def _handle_detector_prewarm_event(
        self,
        event: DetectorPrewarmEvent,
        detector: DetectorRuntime,
        lifecycle: VoiceInputLifecycleController,
        epoch: int,
    ) -> None:
        """Prepare segmented endpointing and transport without final authority."""

        # 用户开口的时刻是**进这个处理函数**的时刻，不是底下 prewarm / transport
        # gather 跑完的时刻。视觉所有权拿 onset 当下界，晚打点会把整段 prewarm+
        # 重连等待算成「用户开口之后」，期间拍的帧全被判成不属于这段发声。
        detected_at = time.monotonic()

        def event_is_current() -> bool:
            return bool(
                epoch == self._asr_session_epoch
                and detector is self._asr_detector
                and lifecycle is self._asr_lifecycle
                and event.ingress.detector_epoch == detector.detector_epoch
                and self._ingress_token_matches(event.ingress.ingress_token)
            )

        if not event_is_current():
            return
        state = lifecycle.snapshot.state
        if state is VoiceLifecycleState.DRAINING:
            if event.kind == "continuous":
                lifecycle.mark_pending_turn_speech(event.ingress.ingress_token)
                if self._asr_pending_turn_onset_at is None:
                    self._asr_pending_turn_onset_at = detected_at
                self._asr_pending_detector_candidate = event.candidate
            return
        if state in {
            VoiceLifecycleState.LOCAL_LISTEN,
            VoiceLifecycleState.WARM_IDLE,
            VoiceLifecycleState.DEEP_SLEEP,
        }:
            warm_task = self._asr_warm_expiry_task
            if warm_task is not None:
                warm_task.cancel()
                self._asr_warm_expiry_task = None
            if state is VoiceLifecycleState.WARM_IDLE:
                lifecycle.metrics.warm_hit_count += 1
            lifecycle.open_turn(event.ingress.ingress_token)
            prewarm_identity = self._capture_runtime_identity(
                ingress_token=event.ingress.ingress_token,
            )
            await self._send_asr_lifecycle_state(
                VoiceLifecycleState.PREWARMING,
                provider=prewarm_identity.provider or "unknown",
                session_epoch=prewarm_identity.session_epoch,
                expected_identity=prewarm_identity,
            )
            if not event_is_current():
                return
        if lifecycle.snapshot.state not in {
            VoiceLifecycleState.PREWARMING,
            VoiceLifecycleState.ACTIVE,
        }:
            return

        turn_token = self._capture_turn_token(lifecycle)
        bound = await detector.bind_candidate(event.candidate, turn_token)
        if bound is None or not event_is_current():
            return
        if lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE:
            await self._activate_asr_audio_dispatcher(lifecycle, turn_token)
            if event.kind == "continuous":
                await self._prepare_independent_asr_turn(epoch)
            return

        smart_turn_task = asyncio.create_task(
            self._ensure_smart_turn_ready(lifecycle, epoch),
            name="independent-asr-prewarm-smart-turn",
        )
        transport_task = asyncio.create_task(
            self._restart_transport(),
            name="independent-asr-prewarm-transport",
        )
        smart_turn_ready, _transport_result = await asyncio.gather(
            smart_turn_task,
            transport_task,
            return_exceptions=True,
        )
        if (
            smart_turn_ready is not True
            or not event_is_current()
            or lifecycle.snapshot.state is not VoiceLifecycleState.PREWARMING
        ):
            return
        if event.kind != "continuous":
            self._schedule_transport_warm_expiry(
                epoch,
                expected_state=VoiceLifecycleState.PREWARMING,
            )
            return
        session_ref = self._asr_session
        if session_ref is None or not getattr(session_ref, "is_ready", True):
            self._asr_pending_speech_confirmed = True
            if self._asr_pending_speech_onset_at is None:
                self._asr_pending_speech_onset_at = detected_at
            return
        lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
        # session 先未就绪、随后又 ready 时，真实开口时刻是当初记下的那个
        # pending onset —— 直接用当前时钟会把整段重连等待算成「开口之后」，
        # 期间拍的帧全被排除在本回合外（CodeRabbit Major）。
        self._asr_turn_onset_at = (
            self._asr_pending_speech_onset_at
            if self._asr_pending_speech_onset_at is not None
            else detected_at
        )
        # 直接确认这一路同样要把待确认状态清干净：session 在标记 pending 之后
        # 才 ready 时，直接路径可能先完成确认，旧 flag / 旧 onset 会留到下一轮
        # 被复用（CodeRabbit Major）。
        self._asr_pending_speech_confirmed = False
        self._asr_pending_speech_onset_at = None
        active_identity = self._capture_runtime_identity(
            ingress_token=event.ingress.ingress_token,
        )
        await self._send_asr_lifecycle_state(
            VoiceLifecycleState.ACTIVE,
            provider=active_identity.provider or "unknown",
            session_epoch=active_identity.session_epoch,
            expected_identity=active_identity,
        )
        if not event_is_current():
            return
        self._asr_turn_audio_started_at = time.monotonic()
        self._asr_first_partial_recorded = False
        await self._activate_asr_audio_dispatcher(lifecycle, turn_token)
        await self._prepare_independent_asr_turn(epoch)

    async def _handle_transport_prewarm_event(
        self,
        event: DetectorTransportPrewarmEvent,
        detector: DetectorRuntime,
        lifecycle: VoiceInputLifecycleController,
        epoch: int,
    ) -> None:
        """Preconnect a streaming transport without opening a logical turn."""

        def event_is_current() -> bool:
            return bool(
                epoch == self._asr_session_epoch
                and detector is self._asr_detector
                and lifecycle is self._asr_lifecycle
                and event.ingress.detector_epoch == detector.detector_epoch
                and self._ingress_token_matches(event.ingress.ingress_token)
            )

        if not event_is_current():
            return
        state = lifecycle.snapshot.state
        if state is VoiceLifecycleState.DRAINING:
            return
        if state in {
            VoiceLifecycleState.LOCAL_LISTEN,
            VoiceLifecycleState.WARM_IDLE,
            VoiceLifecycleState.DEEP_SLEEP,
        }:
            warm_task = self._asr_warm_expiry_task
            if warm_task is not None:
                warm_task.cancel()
                self._asr_warm_expiry_task = None
            if state is VoiceLifecycleState.WARM_IDLE:
                lifecycle.metrics.warm_hit_count += 1
            lifecycle.open_turn(event.ingress.ingress_token)
            prewarm_identity = self._capture_runtime_identity(
                ingress_token=event.ingress.ingress_token,
            )
            await self._send_asr_lifecycle_state(
                VoiceLifecycleState.PREWARMING,
                provider=prewarm_identity.provider or "unknown",
                session_epoch=prewarm_identity.session_epoch,
                expected_identity=prewarm_identity,
            )
            if not event_is_current():
                return
        if lifecycle.snapshot.state is not VoiceLifecycleState.PREWARMING:
            return
        session_ref = self._asr_session
        if session_ref is None or not getattr(session_ref, "is_ready", True):
            await self._restart_transport()
        if (
            not event_is_current()
            or lifecycle.snapshot.state is not VoiceLifecycleState.PREWARMING
        ):
            return
        self._schedule_transport_warm_expiry(
            epoch,
            expected_state=VoiceLifecycleState.PREWARMING,
        )

    async def _bind_provider_detector_candidate(
        self,
        lifecycle: VoiceInputLifecycleController,
        detector: DetectorRuntime,
        *,
        detector_identity: DetectorIngressIdentity | None,
        candidate: DetectorCandidateKey | None,
        expected_identity: _AsrRuntimeIdentity,
        pending_speech_confirmed: bool = False,
    ) -> bool:
        """Bind advisory Provider identity and report whether the runtime is current."""

        if detector_identity is None:
            self._speaker_rejection_metrics[
                "provider_candidate_bind_missing_identity_count"
            ] += 1
            return self._runtime_identity_matches(expected_identity)
        if candidate is None:
            self._speaker_rejection_metrics[
                "provider_candidate_bind_missing_candidate_count"
            ] += 1
            return self._runtime_identity_matches(expected_identity)
        if (
            not self._runtime_identity_matches(expected_identity)
            or expected_identity.lifecycle is not lifecycle
            or expected_identity.detector is not detector
            or expected_identity.ingress_token is None
            or detector_identity.ingress_token != expected_identity.ingress_token
            or detector_identity.detector_epoch != detector.detector_epoch
            or candidate.detector_epoch != detector_identity.detector_epoch
        ):
            self._speaker_rejection_metrics[
                "provider_candidate_bind_identity_rejected_count"
            ] += 1
            # Speaker identity is a soft filter. Ambiguous authority never
            # blocks the independent-ASR hard route.
            return self._runtime_identity_matches(expected_identity)

        state = lifecycle.snapshot.state
        if state is VoiceLifecycleState.DRAINING:
            self._speaker_rejection_metrics[
                "provider_candidate_bind_deferred_count"
            ] += 1
            if pending_speech_confirmed or lifecycle.has_pending_turn:
                self._asr_pending_detector_candidate = candidate
            return True
        if state not in {
            VoiceLifecycleState.PREWARMING,
            VoiceLifecycleState.ACTIVE,
        }:
            self._speaker_rejection_metrics[
                "provider_candidate_bind_state_skipped_count"
            ] += 1
            return True

        turn_token = self._capture_turn_token(lifecycle)
        bind_identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=turn_token,
        )
        self._speaker_rejection_metrics["provider_candidate_bind_attempt_count"] += 1
        try:
            bound = await detector.bind_candidate(candidate, turn_token)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._speaker_rejection_metrics["provider_candidate_bind_failed_count"] += 1
            # Binding is advisory for Provider endpoint authority. The later
            # speaker verdict fails open when no exact detector turn exists.
            return self._runtime_identity_matches(bind_identity)
        if bound is None:
            self._speaker_rejection_metrics["provider_candidate_bind_empty_count"] += 1
        else:
            self._speaker_rejection_metrics[
                "provider_candidate_bind_success_count"
            ] += 1
        return self._runtime_identity_matches(bind_identity)

    async def _ensure_continuous_provider_wake(
        self,
        lifecycle: VoiceInputLifecycleController,
        epoch: int,
        *,
        detector_identity: DetectorIngressIdentity | None = None,
        candidate: DetectorCandidateKey | None = None,
        expected_identity: _AsrRuntimeIdentity | None = None,
    ) -> bool:
        """Open a provider-owned streaming turn without fabricating VAD activity."""

        # 同 _handle_detector_prewarm_event：onset 取进函数的时刻，不取底下各段
        # await 跑完的时刻。
        detected_at = time.monotonic()
        detector = self._asr_detector
        ingress_token = self._asr_current_ingress_token

        def wake_is_current() -> bool:
            return bool(
                epoch == self._asr_session_epoch
                and lifecycle is self._asr_lifecycle
                and detector is self._asr_detector
                and ingress_token is not None
                and self._ingress_token_matches(ingress_token)
            )

        if not wake_is_current():
            return False
        if expected_identity is None:
            expected_identity = self._capture_runtime_identity(
                ingress_token=ingress_token,
            )
        state = lifecycle.snapshot.state
        provider_owns_turns = lifecycle.provider_policy.endpoint_authority == "provider"
        if state is VoiceLifecycleState.DRAINING:
            if provider_owns_turns:
                # Only Provider utterance_started can mint the successor text
                # turn. Local VAD audio may continue feeding the transport,
                # but it cannot create pending-turn identity or rotate speaker
                # evidence while the previous child is draining.
                return wake_is_current()
            lifecycle.mark_pending_turn_speech(ingress_token)
            if self._asr_pending_turn_onset_at is None:
                self._asr_pending_turn_onset_at = detected_at
            return wake_is_current() and await self._bind_provider_detector_candidate(
                lifecycle,
                detector,
                detector_identity=detector_identity,
                candidate=candidate,
                expected_identity=expected_identity,
                pending_speech_confirmed=True,
            )
        if state in {
            VoiceLifecycleState.LOCAL_LISTEN,
            VoiceLifecycleState.WARM_IDLE,
            VoiceLifecycleState.DEEP_SLEEP,
        }:
            warm_task = self._asr_warm_expiry_task
            if warm_task is not None:
                warm_task.cancel()
                self._asr_warm_expiry_task = None
            if state is VoiceLifecycleState.WARM_IDLE:
                lifecycle.metrics.warm_hit_count += 1
            lifecycle.open_turn(ingress_token)
            prewarm_identity = self._capture_runtime_identity(
                ingress_token=ingress_token,
            )
            delivered = await self._send_asr_lifecycle_state(
                VoiceLifecycleState.PREWARMING,
                provider=prewarm_identity.provider or "unknown",
                session_epoch=prewarm_identity.session_epoch,
                expected_identity=prewarm_identity,
            )
            if not delivered or not wake_is_current():
                return False
        if not provider_owns_turns:
            if not await self._bind_provider_detector_candidate(
                lifecycle,
                detector,
                detector_identity=detector_identity,
                candidate=candidate,
                expected_identity=expected_identity,
            ):
                return False
        if lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE:
            return True
        if lifecycle.snapshot.state is not VoiceLifecycleState.PREWARMING:
            return False
        session_ref = self._asr_session
        if session_ref is None or not getattr(session_ref, "is_ready", True):
            self._asr_pending_speech_confirmed = True
            if self._asr_pending_speech_onset_at is None:
                self._asr_pending_speech_onset_at = detected_at
            self._ensure_transport_restart_task()
            return wake_is_current()
        turn_token = self._capture_turn_token(lifecycle)
        if not self._asr_endpointing_ready(lifecycle, detector, turn_token):
            identity = self._capture_runtime_identity(
                ingress_token=ingress_token,
                turn_token=turn_token,
            )
            await self._handle_independent_asr_error(
                epoch,
                identity.provider or "unknown",
                status_code="ASR_BLOCKED_ENDPOINTING",
                expected_identity=identity,
            )
            return False
        lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
        # session 先未就绪、随后又 ready 时，真实开口时刻是当初记下的那个
        # pending onset —— 直接用当前时钟会把整段重连等待算成「开口之后」，
        # 期间拍的帧全被排除在本回合外（CodeRabbit Major）。
        self._asr_turn_onset_at = (
            self._asr_pending_speech_onset_at
            if self._asr_pending_speech_onset_at is not None
            else detected_at
        )
        self._asr_pending_speech_confirmed = False
        self._asr_pending_speech_onset_at = None
        active_identity = self._capture_runtime_identity(
            ingress_token=ingress_token,
            turn_token=turn_token,
        )
        delivered = await self._send_asr_lifecycle_state(
            VoiceLifecycleState.ACTIVE,
            provider=active_identity.provider or "unknown",
            session_epoch=active_identity.session_epoch,
            expected_identity=active_identity,
        )
        if not delivered or not wake_is_current():
            return False
        self._asr_turn_audio_started_at = time.monotonic()
        self._asr_first_partial_recorded = False
        if lifecycle.provider_policy.endpoint_authority == "provider":
            if not self._asr_admission_ingress_started:
                await self._asr_admission_ingress.start()
                self._asr_admission_ingress_started = True
                if not wake_is_current():
                    return False
        else:
            await self._prepare_independent_asr_turn(epoch)
            if not wake_is_current():
                return False
        return await self._activate_asr_audio_dispatcher(
            lifecycle,
            turn_token,
        )

    async def _activate_asr_audio_dispatcher(
        self,
        lifecycle: VoiceInputLifecycleController,
        turn_token: VoiceTurnToken,
        *,
        buffered_pcm16: bytes | None = None,
    ) -> bool:
        physical_identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
        )
        failure_context = self._new_audio_failure_context("dispatcher_activation", physical_identity)
        detector = self._asr_detector
        session_ref = self._asr_session
        if (
            session_ref is None
            or detector is None
            or not getattr(session_ref, "is_ready", True)
            or not self._asr_endpointing_ready(lifecycle, detector, turn_token)
        ):
            return False
        if self._asr_audio_dispatcher.active_turn == turn_token:
            return True
        self._asr_audio_sequence = 0
        buffered_observation = self._asr_buffered_provider_speaker_observation
        self._asr_buffered_provider_speaker_observation = None
        payload = (
            lifecycle.drain_active_start_audio()
            if buffered_pcm16 is None
            else buffered_pcm16
        )
        if payload:
            arming = await self._arm_speaker_authority_for_provider_audio(
                turn_token
            )
            if (
                self._asr_session is not session_ref
                or self._asr_detector is not detector
                or self._asr_lifecycle is not lifecycle
                or lifecycle.current_turn_token != turn_token
                or not self._ingress_token_matches(turn_token.ingress)
            ):
                return False
            if (
                self._speaker_verifier_enforces_admission
                and (
                    not arming
                    or arming.owner_generation
                    != self._speaker_verifier_activation_generation
                )
            ):
                if arming.status is _SpeakerArmingStatus.INVARIANT_FAILURE:
                    await self._handle_independent_asr_error(
                        self._asr_session_epoch,
                        self._asr_provider or "unknown",
                        status_code="ASR_AUDIO_ORDERING_FAILED",
                        reason_code=arming.reason_code,
                        failed_operation="dispatcher_activation",
                        failed_check="speaker_arming_invariant",
                        expected_identity=self._capture_runtime_identity(
                            ingress_token=turn_token.ingress,
                            turn_token=turn_token,
                        ),
                    )
                return False
            if arming.status is _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE:
                self._schedule_speaker_evidence_unavailable(
                    physical_identity,
                    arming.reason_code,
                    activation_generation=arming.owner_generation,
                )
        spans = (
            buffered_observation.spans
            if buffered_observation is not None
            and buffered_observation.total_bytes == len(payload)
            and buffered_observation.spans
            else None
        )
        if spans is not None:
            expected_start = 0
            for span in spans:
                if (
                    span.start_byte != expected_start
                    or span.end_byte <= span.start_byte
                    or span.end_byte > len(payload)
                ):
                    spans = None
                    break
                expected_start = span.end_byte
            if expected_start != len(payload):
                spans = None
        if payload:
            if spans is None:
                if not await self._observe_admitted_provider_audio(
                    lifecycle,
                    detector,
                    payload,
                    sample_rate_hz=16_000,
                    identity=None,
                    split_before_audio=False,
                    evidence_complete=False,
                    turn_token=turn_token,
                    failure_context=failure_context,
                    speaker_evidence_unavailable=(
                        arming.status is _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE
                    ),
                ):
                    await self._retire_partial_provider_audio(physical_identity, failure_context=failure_context)
                    return False
            else:
                for span in spans:
                    span_payload = payload[span.start_byte : span.end_byte]
                    if not await self._observe_admitted_provider_audio(
                        lifecycle,
                        detector,
                        span_payload,
                        sample_rate_hz=16_000,
                        identity=span.last_identity,
                        split_before_audio=span.split_before_audio,
                        evidence_complete=bool(
                            not buffered_observation.overflowed
                            and span.evidence_complete
                        ),
                        turn_token=turn_token,
                        failure_context=failure_context,
                        speaker_evidence_unavailable=(
                            arming.status is _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE
                        ),
                    ):
                        await self._retire_partial_provider_audio(physical_identity, failure_context=failure_context)
                        return False
            if (
                self._asr_session is not session_ref
                or self._asr_detector is not detector
                or self._asr_lifecycle is not lifecycle
                or lifecycle.current_turn_token != turn_token
                or not self._ingress_token_matches(turn_token.ingress)
            ):
                await self._retire_partial_provider_audio(physical_identity, failure_context=failure_context)
                return False
        try:
            activated = self._asr_audio_dispatcher.activate(
                turn_token,
                session_ref,
                payload,
                sample_rate_hz=16_000,
            )
        except Exception as error:
            failure_context.fail("dispatcher_activation_exception", actual=self._audio_failure_scalars(), error=error)
            if payload:
                await self._retire_partial_provider_audio(physical_identity, failure_context=failure_context)
            raise
        if not activated and payload:
            failure_context.fail("dispatcher_activation_rejected", actual=self._audio_failure_scalars())
            await self._retire_partial_provider_audio(physical_identity, failure_context=failure_context)
        return activated

    async def _retire_partial_provider_audio(
        self,
        identity: _AsrRuntimeIdentity,
        *,
        failure_context: AudioFailureContext | None = None,
    ) -> None:
        """Fence uncertain local accounting without replaying accepted PCM."""

        if not self._runtime_identity_matches(identity):
            return
        self._asr_audio_dispatcher.abort()
        task = asyncio.create_task(
            self._handle_independent_asr_error(
                identity.session_epoch,
                identity.provider or "unknown",
                status_code="ASR_AUDIO_ORDERING_FAILED",
                reason_code="ASR_AUDIO_ADMISSION_PARTIAL",
                expected_identity=identity,
                failure_context=failure_context,
                failed_operation="audio_retirement",
                failed_check="partial_accounting",
            ),
            name="asr-partial-audio-retirement",
        )
        self._asr_owned_cleanup_tasks.add(task)
        task.add_done_callback(self._owned_cleanup_done)
        if asyncio.current_task() in self._asr_admission_effect_task_turns:
            # Invalidation joins admission effects. Let this effect unwind
            # after the failure task has installed its epoch fence.
            await asyncio.sleep(0)
        else:
            await asyncio.shield(task)

    def _turn_has_speaker_candidate(self, turn_token: VoiceTurnToken) -> bool:
        return turn_token in self._asr_admission_turn_leases or any(
            candidate_turn == turn_token
            for candidate_turn in self._asr_admission_candidate_turns.values()
        )

    def _unavailable_provider_speaker_ledger_for_turn(
        self,
        turn_token: VoiceTurnToken,
    ) -> _ProviderSpeakerProvisionalLedger | None:
        """Keep degraded turn ownership after its physical handle is retired."""

        for ledger in self._asr_provider_speaker_ledgers.values():
            if (
                ledger.state is _ProviderSpeakerLedgerState.UNAVAILABLE
                and turn_token in {ledger.turn_token, ledger.evidence_turn_token}
                and ledger.activation_generation
                == self._speaker_verifier_activation_generation
                and self._runtime_identity_matches(ledger.runtime_identity)
            ):
                return ledger
        return None

    def _consume_provider_speaker_evidence_settlement(
        self,
        settlement: ProviderSpeakerEvidenceSettlement | None,
        *,
        lease: ProviderSpeakerEvidenceLease,
        detector: DetectorRuntime,
        identity: _AsrRuntimeIdentity,
        owner_generation: str | None,
        turn_token: VoiceTurnToken | None,
        timeline_generation: int | None = None,
    ) -> bool:
        """CAS current physical aliases without retiring logical final owners."""

        validate = getattr(detector, "validate_provider_speaker_evidence_settlement", None)
        if (
            not self._runtime_identity_matches(identity)
            or self._speaker_verifier_activation_generation != owner_generation
            or detector is not self._asr_detector
            or not callable(validate)
            or not validate(settlement, lease=lease, timeline_generation=timeline_generation)
        ):
            return False
        ledger = self._asr_provider_speaker_ledgers.get(lease.candidate)
        if ledger is not None and ledger.evidence_lease is lease:
            if ledger.state is _ProviderSpeakerLedgerState.UNAVAILABLE:
                ledger.evidence_turn_token = (
                    ledger.evidence_turn_token or ledger.turn_token or turn_token
                )
        if self._asr_provider_speaker_evidence_lease is lease:
            self._asr_provider_speaker_evidence_lease = None
            if self._asr_current_speaker_candidate == lease.candidate:
                self._asr_current_speaker_candidate = None
                logical_lease = self._asr_admission_candidate_leases.get(lease.candidate)
                if self._asr_current_speaker_lease == logical_lease:
                    self._asr_current_speaker_lease = None
        return True

    async def _confirm_provider_speaker_evidence_retirement(
        self,
        lease: ProviderSpeakerEvidenceLease,
        *,
        detector: DetectorRuntime,
        identity: _AsrRuntimeIdentity,
        owner_generation: str | None,
        turn_token: VoiceTurnToken | None,
        deadline: float | None = None,
    ) -> bool:
        """Confirm an earlier local retirement; never repeat Provider audio."""

        confirm = getattr(detector, "confirm_provider_speaker_evidence_retirement", None)
        if not callable(confirm):
            return False
        timeout = _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            return False
        try:
            settlement = await asyncio.wait_for(
                confirm(lease), timeout=timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return self._consume_provider_speaker_evidence_settlement(
            settlement,
            lease=lease,
            detector=detector,
            identity=identity,
            owner_generation=owner_generation,
            turn_token=turn_token,
        )

    async def _arm_speaker_authority_for_provider_audio(
        self,
        turn_token: VoiceTurnToken,
    ) -> _SpeakerArmingResult:
        """Publish HOLD authority before the first Provider PCM can escape."""

        owner_generation = self._speaker_verifier_activation_generation
        if not self._speaker_verifier_enforces_admission:
            return _SpeakerArmingResult(
                _SpeakerArmingStatus.ARMED, owner_generation
            )
        if owner_generation is None:
            return _SpeakerArmingResult(
                _SpeakerArmingStatus.INVARIANT_FAILURE,
                reason_code="ASR_SPEAKER_OWNER_MISSING",
            )
        lifecycle = self._asr_lifecycle
        if (
            lifecycle is None
            or lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
            or lifecycle.current_turn_token != turn_token
            or not self._ingress_token_matches(turn_token.ingress)
        ):
            return _SpeakerArmingResult(_SpeakerArmingStatus.STALE)
        if lifecycle.provider_policy.endpoint_authority == "provider":
            unavailable = self._unavailable_provider_speaker_ledger_for_turn(turn_token)
            if unavailable is not None:
                return _SpeakerArmingResult(
                    _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE,
                    owner_generation,
                    unavailable.poisoned_reason,
                )
            evidence_lease = self._asr_provider_speaker_evidence_lease
            candidate = (
                evidence_lease.candidate if evidence_lease is not None else None
            )
            ledger = (
                self._asr_provider_speaker_ledgers.get(candidate)
                if candidate is not None
                else None
            )
            lease_token = (
                self._asr_admission_candidate_leases.get(candidate)
                if candidate is not None
                else None
            )
            if (
                candidate is not None
                and self._asr_current_speaker_candidate == candidate
                and ledger is not None
                and ledger.evidence_lease == evidence_lease
                and ledger.activation_generation == owner_generation
                and self._runtime_identity_matches(ledger.runtime_identity)
                and ledger.turn_token in {None, turn_token}
                and ledger.state not in {
                    _ProviderSpeakerLedgerState.EXACT_DRAINING,
                    _ProviderSpeakerLedgerState.RESOLVED,
                }
                and (
                    lease_token is None
                    or (
                        self._asr_current_speaker_lease == lease_token
                        and (
                            self._asr_admission_candidate_turns.get(candidate)
                            == turn_token
                            or self._asr_admission_turn_leases.get(turn_token)
                            == lease_token
                        )
                    )
                )
            ):
                return _SpeakerArmingResult(
                    _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE
                    if ledger.state is _ProviderSpeakerLedgerState.UNAVAILABLE
                    else _SpeakerArmingStatus.ARMED,
                    owner_generation,
                    ledger.poisoned_reason,
                )
            return await self._await_provider_speaker_parent_lease(
                lifecycle,
                turn_token,
                owner_generation,
            )
        if self._turn_has_speaker_candidate(turn_token):
            return _SpeakerArmingResult(
                _SpeakerArmingStatus.ARMED, owner_generation
            )
        identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=turn_token,
        )
        if (
            self._asr_speaker_authority_pending_turns.get(turn_token)
            == owner_generation
        ):
            return _SpeakerArmingResult(
                _SpeakerArmingStatus.ARMED, owner_generation
            )
        self._asr_speaker_authority_pending_turns[turn_token] = owner_generation
        self._asr_speaker_authoritative_turns.add(turn_token)
        try:
            await self._post_admission_event(
                turn_token,
                SpeakerAuthorityPending(owner_generation),
            )
        except (AdmissionIngressClosedError, AdmissionIngressCapacityError, KeyError):
            if (
                self._asr_speaker_authority_pending_turns.get(turn_token)
                == owner_generation
            ):
                self._asr_speaker_authority_pending_turns.pop(turn_token, None)
            if not self._turn_has_speaker_candidate(turn_token):
                self._asr_speaker_authoritative_turns.discard(turn_token)
            return _SpeakerArmingResult(
                _SpeakerArmingStatus.INVARIANT_FAILURE,
                owner_generation,
                "ASR_SPEAKER_AUTHORITY_UNAVAILABLE",
            )
        if (
            not self._speaker_verifier_enforces_admission
            or self._speaker_verifier_activation_generation != owner_generation
            or not self._runtime_identity_matches(identity)
            or self._asr_lifecycle is not lifecycle
            or lifecycle.current_turn_token != turn_token
        ):
            await self._unarm_speaker_authority_after_observation(
                turn_token,
                owner_generation,
            )
            return _SpeakerArmingResult(_SpeakerArmingStatus.STALE)
        return _SpeakerArmingResult(_SpeakerArmingStatus.ARMED, owner_generation)

    async def _await_provider_speaker_parent_lease(
        self,
        lifecycle: VoiceInputLifecycleController,
        turn_token: VoiceTurnToken,
        owner_generation: str,
    ) -> _SpeakerArmingResult:
        """Settle the physical Provider lease before any matching PCM is queued."""

        unavailable = self._unavailable_provider_speaker_ledger_for_turn(turn_token)
        if unavailable is not None:
            return _SpeakerArmingResult(
                _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE,
                owner_generation,
                unavailable.poisoned_reason,
            )
        existing = self._asr_provider_speaker_arming_tasks.get(turn_token)
        if existing is not None:
            return await asyncio.shield(existing)
        identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=(
                turn_token
                if lifecycle.current_turn_token == turn_token
                else None
            ),
        )
        detector = identity.detector
        if detector is None:
            return _SpeakerArmingResult(_SpeakerArmingStatus.STALE)
        physical_identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
        )

        async def establish() -> _SpeakerArmingResult:
            evidence_lease: ProviderSpeakerEvidenceLease | None = None
            committed = False
            cancelled_error: asyncio.CancelledError | None = None
            try:
                if not self._provider_speaker_arming_operation_is_current(
                    turn_token,
                    owner_generation,
                    identity,
                    lifecycle,
                    detector,
                ):
                    return _SpeakerArmingResult(_SpeakerArmingStatus.STALE)
                ensure_evidence_lease = getattr(
                    detector,
                    "ensure_provider_speaker_evidence_lease",
                    None,
                )
                if not callable(ensure_evidence_lease):
                    return _SpeakerArmingResult(
                        _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE,
                        owner_generation,
                        "ASR_SPEAKER_LEASE_UNSUPPORTED",
                    )
                previous_evidence = self._asr_provider_speaker_evidence_lease
                if previous_evidence is not None:
                    await self._confirm_provider_speaker_evidence_retirement(
                        previous_evidence,
                        detector=detector,
                        identity=identity,
                        owner_generation=owner_generation,
                        turn_token=turn_token,
                    )
                    if not self._provider_speaker_arming_operation_is_current(
                        turn_token, owner_generation, identity, lifecycle, detector
                    ):
                        return _SpeakerArmingResult(_SpeakerArmingStatus.STALE)
                evidence = await asyncio.wait_for(
                    ensure_evidence_lease(),
                    timeout=_PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS,
                )
                # Remember the exact acquired resource before the post-await
                # fence, so a stale operation can retire only its own handle.
                if type(evidence) is ProviderSpeakerEvidenceLease:
                    evidence_lease = evidence
                if not self._provider_speaker_arming_operation_is_current(
                    turn_token,
                    owner_generation,
                    identity,
                    lifecycle,
                    detector,
                ):
                    return _SpeakerArmingResult(_SpeakerArmingStatus.STALE)
                if evidence is None and (
                    self._asr_provider_speaker_evidence_lease is None
                    and self._asr_current_speaker_candidate is None
                ):
                    return _SpeakerArmingResult(
                        _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE,
                        owner_generation,
                        "ASR_SPEAKER_LEASE_UNAVAILABLE",
                    )
                if type(evidence) is not ProviderSpeakerEvidenceLease:
                    return _SpeakerArmingResult(
                        _SpeakerArmingStatus.INVARIANT_FAILURE,
                        owner_generation,
                        "ASR_SPEAKER_LEASE_INVALID",
                    )
                evidence_lease = evidence
                candidate = evidence_lease.candidate
                if candidate.detector_epoch != detector.detector_epoch:
                    return _SpeakerArmingResult(_SpeakerArmingStatus.STALE)
                current_evidence = self._asr_provider_speaker_evidence_lease
                if current_evidence not in {None, evidence_lease}:
                    return _SpeakerArmingResult(
                        _SpeakerArmingStatus.INVARIANT_FAILURE,
                        owner_generation,
                        "ASR_SPEAKER_LEASE_OWNER_CONFLICT",
                    )
                current_candidate = self._asr_current_speaker_candidate
                if current_candidate not in {None, candidate}:
                    return _SpeakerArmingResult(
                        _SpeakerArmingStatus.INVARIANT_FAILURE,
                        owner_generation,
                        "ASR_SPEAKER_CANDIDATE_OWNER_CONFLICT",
                    )

                # Provider PCM is buffer-only until a canonical started anchor
                # arrives.  In particular, do not open an Admission parent and
                # do not publish any LOW/HIGH fact from the provisional prefix.
                ledger = self._asr_provider_speaker_ledgers.get(candidate)
                if ledger is None:
                    ledger = _ProviderSpeakerProvisionalLedger(
                        evidence_lease=evidence_lease,
                        runtime_identity=physical_identity,
                        activation_generation=owner_generation,
                        detector_epoch=evidence_lease.detector_epoch,
                        lease_generation=evidence_lease.lease_generation,
                        evidence_turn_token=turn_token,
                    )
                    self._asr_provider_speaker_ledgers[candidate] = ledger
                    self._speaker_rejection_metrics[
                        "speaker_anchor_deferred_count"
                    ] += 1
                elif (
                    ledger.evidence_lease != evidence_lease
                    or ledger.runtime_identity != physical_identity
                    or ledger.activation_generation != owner_generation
                    or ledger.turn_token not in {None, turn_token}
                    or ledger.state in {
                        _ProviderSpeakerLedgerState.EXACT_DRAINING,
                        _ProviderSpeakerLedgerState.RESOLVED,
                    }
                ):
                    return _SpeakerArmingResult(
                        _SpeakerArmingStatus.INVARIANT_FAILURE,
                        owner_generation,
                        "ASR_SPEAKER_LEDGER_OWNER_CONFLICT",
                    )
                # Publish only after both physical ownership and logical
                # binding have passed; no await may split this adoption.
                self._asr_provider_speaker_evidence_lease = evidence_lease
                self._asr_current_speaker_candidate = candidate
                committed = True
                return _SpeakerArmingResult(
                    _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE
                    if ledger.state is _ProviderSpeakerLedgerState.UNAVAILABLE
                    else _SpeakerArmingStatus.ARMED,
                    owner_generation,
                    ledger.poisoned_reason,
                )
            except asyncio.CancelledError as exc:
                # Reset/close owns cancellation. Finish exact uncommitted cleanup
                # without letting the old operation mutate its replacement, then
                # preserve cancellation for the owner awaiting this task.
                cancelled_error = exc
            except Exception:
                return _SpeakerArmingResult(
                    _SpeakerArmingStatus.INVARIANT_FAILURE,
                    owner_generation,
                    "ASR_SPEAKER_ARMING_FAILED",
                )
            finally:
                evidence_was_adopted = bool(
                    evidence_lease is not None
                    and self._asr_provider_speaker_evidence_lease == evidence_lease
                    and self._asr_provider_speaker_ledgers.get(
                        evidence_lease.candidate
                    )
                    is not None
                )
                if (
                    not committed
                    and evidence_lease is not None
                    and not evidence_was_adopted
                ):
                    abandon = getattr(
                        detector,
                        "abandon_provider_speaker_evidence_lease",
                        None,
                    )
                    if callable(abandon):
                        try:
                            await asyncio.wait_for(
                                abandon(evidence_lease),
                                timeout=_PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS,
                            )
                        except asyncio.CancelledError as exc:
                            cancelled_error = cancelled_error or exc
                        except Exception:
                            pass
                if cancelled_error is not None:
                    raise cancelled_error

        task = asyncio.create_task(
            establish(),
            name=f"provider-speaker-parent-{turn_token.turn_id}",
        )
        self._asr_provider_speaker_arming_tasks[turn_token] = task
        self._asr_owned_cleanup_tasks.add(task)

        def finish(done: asyncio.Task[_SpeakerArmingResult]) -> None:
            if self._asr_provider_speaker_arming_tasks.get(turn_token) is done:
                self._asr_provider_speaker_arming_tasks.pop(turn_token, None)
            self._owned_cleanup_done(done)

        task.add_done_callback(finish)
        return await asyncio.shield(task)

    def _provider_speaker_arming_operation_is_current(
        self,
        turn_token: VoiceTurnToken,
        owner_generation: str,
        identity: _AsrRuntimeIdentity,
        lifecycle: VoiceInputLifecycleController,
        detector: DetectorRuntime,
    ) -> bool:
        current_task = asyncio.current_task()
        return bool(
            current_task is not None
            and self._asr_provider_speaker_arming_tasks.get(turn_token) is current_task
            and not self._asr_terminal_close_requested
            and self._speaker_verifier_enforces_admission
            and self._speaker_verifier_activation_generation == owner_generation
            and self._runtime_identity_matches(identity)
            and self._asr_lifecycle is lifecycle
            and (
                (
                    lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
                    and lifecycle.current_turn_token == turn_token
                )
                or (
                    lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
                    and lifecycle.pending_turn_token == turn_token
                )
            )
            and self._asr_detector is detector
        )

    def _cancel_provider_speaker_arming_tasks(self) -> None:
        """Invalidate and cancel only the Runtime-owned Provider arming tasks."""

        tasks = tuple(self._asr_provider_speaker_arming_tasks.values())
        self._asr_provider_speaker_arming_tasks.clear()
        current = asyncio.current_task()
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()

    async def _unarm_speaker_authority_after_observation(
        self,
        turn_token: VoiceTurnToken,
        owner_generation: str | None,
    ) -> None:
        """Fail open one ARMING turn when observation produced no candidate."""

        if (
            owner_generation is None
            or self._asr_speaker_authority_pending_turns.get(turn_token)
            != owner_generation
            or self._turn_has_speaker_candidate(turn_token)
        ):
            return
        operation = (turn_token, owner_generation)
        task = self._asr_speaker_authority_unarming_tasks.get(operation)
        if task is None:

            async def settle_unarmed() -> None:
                try:
                    await self._post_admission_event(
                        turn_token,
                        SpeakerAuthorityUnarmed(owner_generation),
                    )
                except (
                    AdmissionIngressClosedError,
                    AdmissionIngressCapacityError,
                    KeyError,
                ):
                    return
                if (
                    self._asr_speaker_authority_pending_turns.get(turn_token)
                    == owner_generation
                ):
                    self._asr_speaker_authority_pending_turns.pop(
                        turn_token,
                        None,
                    )

            task = asyncio.create_task(
                settle_unarmed(),
                name="speaker-authority-unarmed-settlement",
            )
            self._asr_speaker_authority_unarming_tasks[operation] = task
            self._track_admission_effect_task(task, turn_token)

            def done(done_task: asyncio.Task[None]) -> None:
                if (
                    self._asr_speaker_authority_unarming_tasks.get(operation)
                    is done_task
                ):
                    self._asr_speaker_authority_unarming_tasks.pop(operation, None)
                self._admission_effect_done(done_task)

            task.add_done_callback(done)
        await asyncio.shield(task)

    def _record_buffered_provider_speaker_observation(
        self,
        *,
        identity: DetectorIngressIdentity | None,
        byte_count: int,
        split_before_audio: bool,
        evidence_complete: bool,
    ) -> None:
        if byte_count <= 0:
            return
        buffered = self._asr_buffered_provider_speaker_observation
        if buffered is None:
            self._asr_buffered_provider_speaker_observation = (
                _BufferedProviderSpeakerObservation(
                    total_bytes=byte_count,
                    spans=[
                        _BufferedProviderSpeakerSpan(
                            start_byte=0,
                            end_byte=byte_count,
                            first_identity=identity,
                            last_identity=identity,
                            split_before_audio=bool(split_before_audio),
                            evidence_complete=bool(
                                evidence_complete and identity is not None
                            ),
                        )
                    ],
                )
            )
            return
        start_byte = buffered.total_bytes
        end_byte = start_byte + byte_count
        buffered.total_bytes = end_byte
        if buffered.overflowed:
            collapsed = buffered.spans[0]
            collapsed.end_byte = end_byte
            collapsed.last_identity = identity
            collapsed.evidence_complete = False
            return

        previous = buffered.spans[-1]
        previous_identity = previous.last_identity
        compatible = bool(
            previous_identity is not None
            and identity is not None
            and previous_identity.ingress_token == identity.ingress_token
            and previous_identity.detector_epoch == identity.detector_epoch
            and previous_identity.sequence_no < identity.sequence_no
        )
        if split_before_audio:
            if len(buffered.spans) >= _MAX_BUFFERED_PROVIDER_SPEAKER_SPANS:
                first = buffered.spans[0]
                buffered.spans[:] = [
                    _BufferedProviderSpeakerSpan(
                        start_byte=0,
                        end_byte=end_byte,
                        first_identity=first.first_identity,
                        last_identity=identity,
                        split_before_audio=False,
                        evidence_complete=False,
                    )
                ]
                buffered.overflowed = True
                return
            buffered.spans.append(
                _BufferedProviderSpeakerSpan(
                    start_byte=start_byte,
                    end_byte=end_byte,
                    first_identity=identity,
                    last_identity=identity,
                    split_before_audio=True,
                    evidence_complete=bool(
                        evidence_complete and identity is not None and compatible
                    ),
                )
            )
            return
        previous.end_byte = end_byte
        previous.last_identity = identity
        previous.evidence_complete = bool(
            previous.evidence_complete
            and evidence_complete
            and identity is not None
            and compatible
        )

    async def _observe_admitted_provider_audio(
        self,
        lifecycle: VoiceInputLifecycleController,
        detector: DetectorRuntime,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        identity: DetectorIngressIdentity | None,
        split_before_audio: bool,
        evidence_complete: bool,
        turn_token: VoiceTurnToken,
        speaker_evidence_unavailable: bool = False,
        failure_context: AudioFailureContext | None = None,
    ) -> bool:
        if not pcm16:
            return True
        owner_generation = self._asr_speaker_authority_pending_turns.get(turn_token)
        observation_identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=turn_token,
        )
        physical_identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
        )
        ordered_observation_started = False
        activation_generation = self._speaker_verifier_activation_generation

        def failed(
            check: str, error: BaseException | None = None, *, actual: dict | None = None,
        ) -> bool:
            if failure_context is not None:
                snapshot = self._audio_failure_scalars()
                snapshot.update(actual or {})
                failure_context.fail(
                    check, actual=snapshot, error=error,
                )
            return False

        try:
            if _uses_smart_turn_endpointing(lifecycle.provider_policy):
                self._observe_provider_speaker_shadow(
                    detector,
                    pcm16,
                    sample_rate_hz=sample_rate_hz,
                )
                return True
            observe_ordered = getattr(
                detector,
                "observe_provider_audio_ordered",
                None,
            )
            if identity is not None and callable(observe_ordered):
                evidence_lease = self._asr_provider_speaker_evidence_lease
                ledger = (
                    self._asr_provider_speaker_ledgers.get(
                        evidence_lease.candidate
                    )
                    if evidence_lease is not None
                    else None
                )
                if ledger is None:
                    ledger = self._unavailable_provider_speaker_ledger_for_turn(turn_token)
                accounting_only = bool(
                    speaker_evidence_unavailable
                    or (
                        ledger is not None
                        and ledger.state is _ProviderSpeakerLedgerState.UNAVAILABLE
                    )
                )
                if self._speaker_verifier_enforces_admission:
                    candidate = (
                        evidence_lease.candidate
                        if evidence_lease is not None
                        else None
                    )
                    if not accounting_only and (
                        candidate is None
                        or ledger is None
                        or ledger.evidence_lease != evidence_lease
                        or self._asr_current_speaker_candidate != candidate
                    ):
                        return failed("lease_alias_mismatch")
                # Number ordered-observer dispatch attempts only. Explicit
                # fallback revokes incomplete evidence directly.
                self._asr_provider_speaker_sequence += 1
                sequence_no = self._asr_provider_speaker_sequence
                if failure_context is not None:
                    failure_context.expected.update(sequence_no=sequence_no, payload_samples=len(pcm16) // 2, detector_epoch=identity.detector_epoch)
                ordered_kwargs = {
                    "sample_rate_hz": sample_rate_hz,
                    "identity": identity,
                    "sequence_no": sequence_no,
                    "split_before_audio": split_before_audio,
                    "evidence_complete": evidence_complete,
                }
                if type(detector) is DetectorRuntime:
                    ordered_kwargs["failure_context"] = failure_context
                if evidence_lease is not None:
                    ordered_kwargs["speaker_evidence_lease"] = evidence_lease
                if accounting_only:
                    ordered_kwargs["accounting_only"] = True
                    ordered_kwargs["evidence_complete"] = False
                    if ledger is not None and ledger.timeline_generation >= 0:
                        ordered_kwargs["expected_timeline_generation"] = (
                            ledger.timeline_generation
                        )
                ordered_observation_started = True
                update = await observe_ordered(pcm16, **ordered_kwargs)
                if not self._runtime_identity_matches(observation_identity):
                    return failed("observation_owner_changed")
                if accounting_only:
                    sample_count, remainder = divmod(
                        len(pcm16) // 2 * 16_000, sample_rate_hz
                    )
                    accounted = bool(
                        type(update) is ProviderAudioAccountingReceipt
                        and update.detector_epoch == identity.detector_epoch
                        and (
                            "expected_timeline_generation" not in ordered_kwargs
                            or update.timeline_generation
                            == ordered_kwargs["expected_timeline_generation"]
                        )
                        and update.sequence_no == sequence_no
                        and not remainder
                        and update.end_sample_16k - update.start_sample_16k
                        == sample_count
                    )
                    if not accounted:
                        if type(update) is not ProviderAudioAccountingReceipt:
                            return failed("accounting_receipt_type_invalid")
                        if update.detector_epoch != identity.detector_epoch:
                            check = "accounting_receipt_detector_mismatch"
                        elif (
                            "expected_timeline_generation" in ordered_kwargs
                            and update.timeline_generation != ordered_kwargs["expected_timeline_generation"]
                        ):
                            check = "accounting_receipt_timeline_mismatch"
                        elif update.sequence_no != sequence_no:
                            check = "accounting_receipt_sequence_mismatch"
                        else:
                            check = "accounting_receipt_samples_mismatch"
                        return failed(check, actual={
                            "detector_epoch": update.detector_epoch,
                            "timeline_generation": update.timeline_generation,
                            "sequence_no": update.sequence_no,
                            "sample_cursor_16k": update.end_sample_16k,
                            "payload_samples": update.end_sample_16k - update.start_sample_16k,
                        })
                    if evidence_lease is not None:
                        settled = self._consume_provider_speaker_evidence_settlement(
                            update.evidence_settlement,
                            lease=evidence_lease,
                            detector=detector,
                            identity=observation_identity,
                            owner_generation=activation_generation,
                            turn_token=turn_token,
                            timeline_generation=update.timeline_generation,
                        )
                        return settled or failed("evidence_retirement_unconfirmed")
                    return bool(
                        self._speaker_verifier_activation_generation == activation_generation
                        and self._asr_provider_speaker_evidence_lease is None
                    ) or failed("retired_alias_replaced")
                if self._speaker_verifier_enforces_admission and (
                    type(update) is not ProviderSpeakerEvidenceUpdate
                    or update.lease != evidence_lease
                ):
                    if ledger is not None:
                        self._poison_provider_speaker_ledger(
                            ledger,
                            "provider_pcm_receipt_missing",
                        )
                    # No receipt means ordered accounting itself is unknown.
                    # Retire this ASR timeline rather than sending across a gap.
                    if type(update) is not ProviderSpeakerEvidenceUpdate:
                        return failed("speaker_receipt_type_invalid")
                    return failed("speaker_receipt_lease_mismatch", actual={
                        "lease_generation": getattr(update.lease, "lease_generation", None),
                        "detector_epoch": getattr(update.lease, "detector_epoch", None),
                        "sequence_no": update.sequence_no,
                    })
                if (
                    type(update) is ProviderSpeakerEvidenceUpdate
                    and update.lease == evidence_lease
                ):
                    assert ledger is not None
                    if (
                        ledger.last_pcm_sequence_no > 0
                        and update.sequence_no
                        != ledger.last_pcm_sequence_no + 1
                    ):
                        self._poison_provider_speaker_ledger(
                            ledger,
                            "provider_pcm_sequence_gap",
                        )
                    elif update.sequence_no <= 0:
                        self._poison_provider_speaker_ledger(
                            ledger,
                            "provider_pcm_sequence_invalid",
                        )
                    else:
                        ledger.last_pcm_sequence_no = update.sequence_no
                    if (
                        update.capture.disposition
                        is SpeakerShadowCaptureDisposition.UNAVAILABLE
                    ):
                        self._poison_provider_speaker_ledger(
                            ledger,
                            "speaker_capture_unavailable",
                        )
            else:
                if self._speaker_verifier_enforces_admission:
                    # Missing ingress provenance cannot justify exact evidence
                    # or advance the canonical Provider timeline.
                    return failed("ingress_identity_missing")
                self._observe_provider_speaker_shadow(
                    detector,
                    pcm16,
                    sample_rate_hz=sample_rate_hz,
                )
            return self._runtime_identity_matches(observation_identity) or failed(
                "observation_owner_changed"
            )
        except asyncio.CancelledError:
            if ordered_observation_started:
                if failure_context is not None:
                    failure_context.fail("observation_cancelled", actual=self._audio_failure_scalars())
                await self._retire_partial_provider_audio(physical_identity, failure_context=failure_context)
            raise
        except Exception as error:
            if not self._runtime_identity_matches(physical_identity):
                return failed("physical_owner_changed", error)
            evidence_lease = self._asr_provider_speaker_evidence_lease
            ledger = (
                self._asr_provider_speaker_ledgers.get(
                    evidence_lease.candidate
                )
                if evidence_lease is not None
                else None
            )
            if ledger is not None:
                self._poison_provider_speaker_ledger(
                    ledger,
                    "provider_pcm_observation_failed",
                )
                return failed("ordered_observation_exception", error)
            return bool(
                not ordered_observation_started
                and not self._speaker_verifier_enforces_admission
                and self._runtime_identity_matches(observation_identity)
            ) or failed("observation_exception", error)
        finally:
            try:
                await self._unarm_speaker_authority_after_observation(
                    turn_token,
                    owner_generation,
                )
            except asyncio.CancelledError:
                if ordered_observation_started:
                    if failure_context is not None:
                        failure_context.fail("unarm_cancelled", actual=self._audio_failure_scalars())
                    await self._retire_partial_provider_audio(physical_identity, failure_context=failure_context)
                raise

    @staticmethod
    def _observe_provider_speaker_shadow(
        detector: DetectorRuntime,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
    ) -> None:
        try:
            detector.observe_provider_audio(
                pcm16,
                sample_rate_hz=sample_rate_hz,
            )
        except Exception:
            # Observation never participates in ASR acceptance or failure.
            return

    async def _ensure_smart_turn_ready(
        self,
        lifecycle: VoiceInputLifecycleController,
        epoch: int,
    ) -> bool:
        if epoch != self._asr_session_epoch or self._asr_lifecycle is not lifecycle:
            return False
        if not _uses_smart_turn_endpointing(lifecycle.provider_policy):
            return True
        turn_token = self._capture_turn_token(lifecycle)
        detector = self._asr_detector
        identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=turn_token,
        )
        if detector is None:
            await self._handle_independent_asr_error(
                epoch,
                identity.provider or "unknown",
                status_code="ASR_BLOCKED_ENDPOINTING",
                expected_identity=identity,
            )
            return False
        prepare_scope = (epoch, id(lifecycle), id(detector))
        if self._asr_smart_turn_prepare_scope != prepare_scope:
            self._asr_smart_turn_prepare_scope = prepare_scope
            self._asr_smart_turn_prepare_lock = asyncio.Lock()
        prepare_lock = self._asr_smart_turn_prepare_lock
        async with prepare_lock:
            if not self._runtime_identity_matches(identity):
                return False
            return await self._ensure_smart_turn_ready_for_identity(
                detector,
                turn_token,
                identity,
                epoch=epoch,
            )

    async def _ensure_smart_turn_ready_for_identity(
        self,
        detector: DetectorRuntime,
        turn_token: VoiceTurnToken,
        identity: _AsrRuntimeIdentity,
        *,
        epoch: int,
    ) -> bool:
        lease = self._asr_smart_turn_lease
        if (
            lease is not None
            and lease.token == turn_token
            and detector.endpointing_ready(turn_token)
        ):
            return True
        if lease is not None:
            await lease.release()
            if self._asr_smart_turn_lease is not lease:
                return False
            self._asr_smart_turn_lease = None
            if not self._runtime_identity_matches(identity):
                return False
        lease = await detector.prepare_endpointing(turn_token)
        if (
            not self._runtime_identity_matches(identity)
            or self._asr_smart_turn_lease is not None
        ):
            if lease is not None:
                await lease.release()
            return False
        if lease is None or not detector.endpointing_ready(turn_token):
            if lease is not None:
                await lease.release()
                if not self._runtime_identity_matches(identity):
                    return False
            await self._handle_independent_asr_error(
                epoch,
                identity.provider or "unknown",
                status_code="ASR_BLOCKED_ENDPOINTING",
                expected_identity=identity,
            )
            return False
        self._asr_smart_turn_lease = lease
        return True

    async def _handle_audio_ingress_backpressure(
        self,
        token: VoiceIngressToken,
        *,
        observed_state: VoiceLifecycleState | None = None,
    ) -> None:
        """Invalidate a whole candidate/turn instead of dropping middle PCM."""

        lifecycle = self._asr_lifecycle
        if lifecycle is None or not self._ingress_token_matches(token):
            return
        epoch = self._asr_session_epoch
        detector = self._asr_detector
        provider = self._asr_provider or "unknown"
        state = observed_state or lifecycle.snapshot.state
        if state is VoiceLifecycleState.DRAINING and not _uses_smart_turn_endpointing(
            lifecycle.provider_policy
        ):
            discard_failed = False
            discard_handled = False
            final_completed_before_discard = False
            async with self._asr_final_lock:
                if (
                    self._asr_lifecycle is not lifecycle
                    or self._asr_detector is not detector
                    or epoch != self._asr_session_epoch
                    or not self._ingress_token_matches(token)
                ):
                    return
                state = lifecycle.snapshot.state
                lifecycle.discard_pending_turn()
                self._asr_pending_turn_onset_at = None
                self._asr_pending_speech_confirmed = False
                self._asr_pending_speech_onset_at = None
                self._asr_pending_detector_candidate = None
                if state is VoiceLifecycleState.DRAINING:
                    sealed_token = self._asr_sealed_turn_token
                    provider_fence = self._asr_provider_candidate_fence
                    if (
                        detector is None
                        or sealed_token is None
                        or provider_fence is None
                        or not self._transport_token_matches(
                            sealed_token,
                            lifecycle,
                        )
                    ):
                        discard_failed = True
                    else:
                        try:
                            discard_handled = await detector.discard_provider_successor(
                                provider_fence
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.warning(
                                "[%s] provider successor discard failed",
                                self.display_name,
                            )
                        discard_failed = not discard_handled
                elif state is VoiceLifecycleState.WARM_IDLE:
                    final_completed_before_discard = True
            if discard_failed:
                identity = self._capture_runtime_identity(ingress_token=token)
                await self._handle_independent_asr_error(
                    epoch,
                    provider,
                    status_code="ASR_ENDPOINTING_FAILED",
                    expected_identity=identity,
                )
                return
            if discard_handled:
                identity = self._capture_runtime_identity(ingress_token=token)
                await self._send_asr_status(
                    "ASR_INGRESS_BACKPRESSURE",
                    provider,
                    session_epoch=epoch,
                    expected_identity=identity,
                )
                return
            if final_completed_before_discard:
                if detector is not None and detector is self._asr_detector:
                    try:
                        await detector.reset()
                    except Exception:
                        logger.warning(
                            "[%s] detector reset failed after pending overflow",
                            self.display_name,
                        )
                identity = self._capture_runtime_identity(ingress_token=token)
                await self._send_asr_status(
                    "ASR_INGRESS_BACKPRESSURE",
                    provider,
                    session_epoch=epoch,
                    expected_identity=identity,
                )
                return
            if state is VoiceLifecycleState.ACTIVE:
                await self._asr_transcript_dispatcher.wait_idle()
        if state is VoiceLifecycleState.DRAINING:
            lifecycle.discard_pending_turn()
            self._asr_pending_turn_onset_at = None
            self._asr_pending_speech_confirmed = False
            self._asr_pending_speech_onset_at = None
            self._asr_pending_detector_candidate = None
            if detector is not None:
                identity = self._capture_runtime_identity(ingress_token=token)
                await detector.reset()
                if not self._runtime_identity_matches(
                    identity
                ) or not self._asr_runtime_refs_match(
                    epoch,
                    lifecycle,
                    detector,
                ):
                    return
            identity = self._capture_runtime_identity(ingress_token=token)
            await self._send_asr_status(
                "ASR_INGRESS_BACKPRESSURE",
                provider,
                session_epoch=epoch,
                expected_identity=identity,
            )
            return
        if state in {
            VoiceLifecycleState.LOCAL_LISTEN,
            VoiceLifecycleState.WARM_IDLE,
            VoiceLifecycleState.DEEP_SLEEP,
        }:
            self._asr_audio_generation += 1
            lifecycle.invalidate_audio()
            if detector is not None:
                identity = self._capture_runtime_identity()
                try:
                    await detector.reset()
                except Exception:
                    logger.warning(
                        "[%s] detector reset failed after ingress backpressure",
                        self.display_name,
                    )
                if not self._runtime_identity_matches(
                    identity
                ) or not self._asr_runtime_refs_match(
                    epoch,
                    lifecycle,
                    detector,
                ):
                    return
            identity = self._capture_runtime_identity()
            await self._send_asr_status(
                "ASR_INGRESS_BACKPRESSURE",
                provider,
                session_epoch=epoch,
                expected_identity=identity,
            )
            return
        if state in {
            VoiceLifecycleState.PREWARMING,
            VoiceLifecycleState.BACKOFF,
            VoiceLifecycleState.ACTIVE,
        }:
            abandoned_turn = (
                self._capture_turn_token(lifecycle)
                if state is VoiceLifecycleState.ACTIVE and self._asr_turn_prepared
                else None
            )
            try:
                lifecycle.invalidate_audio()
                post_detach = await self._abort_transport("detector_audio_backpressure")
                if not self._runtime_identity_matches(
                    post_detach
                ) or not self._asr_runtime_refs_match(
                    epoch,
                    lifecycle,
                    detector,
                ):
                    return
                if detector is not None:
                    await detector.reset()
                    if not self._runtime_identity_matches(
                        post_detach
                    ) or not self._asr_runtime_refs_match(
                        epoch,
                        lifecycle,
                        detector,
                    ):
                        return
                await self._send_asr_status(
                    "ASR_INGRESS_BACKPRESSURE",
                    provider,
                    session_epoch=epoch,
                    expected_identity=post_detach,
                )
                if not self._runtime_identity_matches(post_detach):
                    return
                await self._send_asr_lifecycle_state(
                    VoiceLifecycleState.LOCAL_LISTEN,
                    provider=provider,
                    session_epoch=epoch,
                    expected_identity=post_detach,
                )
                return
            finally:
                if abandoned_turn is not None:
                    await self._notify_asr_turn_abandoned(abandoned_turn)
        identity = self._capture_runtime_identity()
        await self._send_asr_status(
            "ASR_INGRESS_BACKPRESSURE",
            provider,
            session_epoch=epoch,
            expected_identity=identity,
        )

    async def start(
        self,
        *,
        route_key: str,
        resource_optimization_enabled: bool,
        user_language: str | None = None,
        speaker_shadow_factory: SpeakerShadowFactory | None = None,
    ) -> AsrStartResult:
        """Resolve and start one independent-ASR route.

        ``user_language`` is the caller's normalized language preference; the
        session factory maps it onto each provider's accepted hints and falls
        back to automatic detection when it is unknown or unsupported.
        """

        self._ensure_asr_runtime_state()
        if self._asr_terminal_close_requested:
            return AsrStartResult(
                AsrStartStatus.FAILED,
                failure_code="ASR_START_STALE",
                session_epoch=self._asr_session_epoch,
            )
        self._asr_runtime_close_task = None
        operation_generation = self._begin_asr_start_operation()
        if not self._asr_admission_ingress_started:
            await self._asr_admission_ingress.start()
            self._asr_admission_ingress_started = True
        if self._asr_terminal_close_requested or not self._asr_start_operation_matches(
            operation_generation
        ):
            return AsrStartResult(
                AsrStartStatus.FAILED,
                failure_code="ASR_START_STALE",
                session_epoch=self._asr_session_epoch,
            )
        cleanup = self._detach_independent_asr(
            operation_generation=operation_generation,
        )
        if cleanup is not None:
            cleanup_task = self._schedule_owned_cleanup(
                cleanup,
                name="independent-asr-start-predecessor-close",
            )
            await asyncio.shield(cleanup_task)
        if not self._asr_start_operation_matches(operation_generation):
            return AsrStartResult(
                AsrStartStatus.FAILED,
                failure_code="ASR_START_STALE",
            )
        if self._asr_deny_transport_state is DenyTransportState.QUARANTINED:
            # start() is reached only after the explicit microphone restart
            # has completed predecessor teardown. That is the sole recovery
            # path from a cleanup quarantine.
            self._asr_speaker_deny_cleanups.clear()
            self._asr_deny_transport_state = DenyTransportState.OPEN
            self._asr_deny_cleanup_active = False
            self._asr_rearm_cutoff_sequence = 0
            self._asr_rearm_last_sequence = 0
            self._asr_rearm_last_captured_at = 0.0
        epoch = self._asr_session_epoch
        audio_generation = self._asr_audio_generation

        def operation_is_current() -> bool:
            return bool(
                not self._asr_terminal_close_requested
                and self._asr_start_operation_matches(operation_generation)
                and epoch == self._asr_session_epoch
                and audio_generation == self._asr_audio_generation
            )

        def stale_result(provider: str | None = None) -> AsrStartResult:
            return AsrStartResult(
                AsrStartStatus.FAILED,
                provider=provider,
                failure_code="ASR_START_STALE",
                session_epoch=epoch,
            )

        self._asr_audio_bytes = 0
        self._voice_input_resource_optimization_enabled = bool(
            resource_optimization_enabled
        )
        core_type = str(route_key or "").strip().lower()

        try:
            # The resolver reads core config synchronously from disk; keep
            # that blocking read off the event loop.
            selection = await asyncio.to_thread(_resolve_asr_selection, core_type)
            selected_provider = getattr(selection, "provider_key", None)
            if not isinstance(selected_provider, str) or not selected_provider.strip():
                raise ValueError("invalid ASR provider selection")
            provider = selected_provider.strip().lower()
            endpointing_mode = getattr(selection, "endpointing_mode", None)
            if endpointing_mode not in {"manual", "provider"}:
                raise ValueError("invalid ASR endpointing selection")
            availability = getattr(
                selection,
                "availability",
                AsrProviderAvailability.IMPLEMENTED,
            )
            if availability is not AsrProviderAvailability.IMPLEMENTED:
                if not operation_is_current():
                    return stale_result(provider)
                failure_code = "ASR_INDEPENDENT_PROVIDER_UNAVAILABLE"
                status_identity = self._capture_runtime_identity()
                delivered = await self._send_asr_status(
                    failure_code,
                    provider,
                    session_epoch=epoch,
                    expected_identity=status_identity,
                )
                if not delivered or not operation_is_current():
                    return stale_result(provider)
                return AsrStartResult(
                    AsrStartStatus.UNAVAILABLE,
                    provider=provider,
                    failure_code=failure_code,
                    session_epoch=epoch,
                )
            policy = resolve_provider_policy(provider, endpointing_mode)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Configuration errors must not abort the already-started Core
            # session. Keep the microphone fail-closed and report only the
            # fixed status code/provider category.
            if not operation_is_current():
                return stale_result()
            self._asr_session = None
            self._asr_provider = None
            failure_code = "ASR_INDEPENDENT_FAILED"
            status_identity = self._capture_runtime_identity()
            delivered = await self._send_asr_status(
                failure_code,
                core_type or "unknown",
                session_epoch=epoch,
                expected_identity=status_identity,
            )
            if not delivered or not operation_is_current():
                return stale_result()
            return AsrStartResult(
                AsrStartStatus.FAILED,
                failure_code=failure_code,
                session_epoch=epoch,
            )

        # Provider selection is immutable for this session epoch. Expose the
        # selected provider during connect retries, then clear it only if the
        # startup attempt ultimately fails.
        if not operation_is_current():
            return stale_result(provider)
        self._asr_provider = provider

        def create_candidate(candidate_selection: Any) -> Any:
            """Create one startup candidate with callbacks bound to its identity."""

            candidate_provider = candidate_selection.provider_key
            candidate_endpointing = candidate_selection.endpointing_mode
            candidate_policy = resolve_provider_policy(
                candidate_provider,
                candidate_endpointing,
            )
            candidate_session = None

            def is_adopted_candidate() -> bool:
                return (
                    candidate_session is not None
                    and self._asr_session is candidate_session
                    and epoch == self._asr_session_epoch
                )

            async def on_final(text: str) -> None:
                if not is_adopted_candidate():
                    return
                await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=lambda: self._handle_independent_asr_final(
                        text, epoch, candidate_provider
                    ),
                )

            async def on_provider_final_ready(
                notification: ProviderFinalNotification,
            ) -> None:
                if not is_adopted_candidate():
                    return

                async def handle() -> None:
                    if (
                        candidate_policy.endpoint_authority == "provider"
                        and notification.key is not None
                    ):
                        await self._handle_provider_final(
                            notification.key,
                            notification.text,
                            epoch,
                            candidate_provider,
                            received_at=notification.received_at,
                            admission_deadline=notification.admission_deadline,
                        )
                    else:
                        await self._handle_independent_asr_final(
                            notification.text,
                            epoch,
                            candidate_provider,
                            received_at=notification.received_at,
                            deadline=notification.admission_deadline,
                        )

                await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=handle,
                )

            async def on_error(message: str) -> None:
                if not is_adopted_candidate():
                    return
                reason_code = _extract_asr_reason_code(
                    message,
                    fallback="ASR_INDEPENDENT_FAILED",
                )
                await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=lambda: self._handle_independent_asr_error(
                        epoch,
                        candidate_provider,
                        reason_code=reason_code,
                    ),
                )

            async def on_status(_message: str) -> None:
                # Provider status strings are intentionally not forwarded verbatim.
                if not is_adopted_candidate():
                    return

                async def ignore_status() -> None:
                    return None

                await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=ignore_status,
                )

            async def on_activity(event: SpeechActivityEvent) -> None:
                if not is_adopted_candidate():
                    return
                await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=lambda: self._handle_independent_asr_activity(
                        event, epoch
                    ),
                )

            async def on_endpoint() -> None:
                if not is_adopted_candidate():
                    return
                await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=lambda: self._handle_independent_asr_endpoint(epoch),
                )

            async def on_provider_endpoint(
                notification: ProviderEndpointNotification,
            ) -> None:
                if not is_adopted_candidate():
                    return
                await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=lambda: self._handle_provider_endpoint_notification(
                        notification,
                        epoch,
                    ),
                )

            async def on_provider_utterance_started(
                notification: ProviderUtteranceStartedNotification,
            ) -> ProviderStartedSettlement:
                if not is_adopted_candidate():
                    return ProviderStartedSettlement.FAILED_IDENTITY
                granted, outcome = await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=lambda: self._bind_provider_utterance_started(
                        notification,
                        epoch,
                    ),
                )
                if not granted:
                    return ProviderStartedSettlement.FAILED_IDENTITY
                if outcome in {
                    _ProviderStartedOutcome.FAILED,
                    _ProviderStartedOutcome.STALE,
                }:
                    return ProviderStartedSettlement.FAILED_IDENTITY
                if outcome is _ProviderStartedOutcome.BOUND_SPEAKER_UNAVAILABLE:
                    return ProviderStartedSettlement.BOUND_SPEAKER_UNAVAILABLE
                return ProviderStartedSettlement.BOUND_EXACT_PENDING

            async def on_partial(text: str) -> None:
                if not is_adopted_candidate():
                    return
                await self._run_provider_callback(
                    session_ref=candidate_session,
                    session_epoch=epoch,
                    callback=lambda: self._send_independent_asr_preview(text, epoch),
                )

            candidate_session = _create_asr_session_from_selection(
                core_type,
                selection=candidate_selection,
                on_input_transcript=on_final,
                on_connection_error=on_error,
                on_status_message=on_status,
                on_speech_activity=on_activity,
                on_turn_endpointed=on_endpoint,
                on_provider_utterance_started=(
                    on_provider_utterance_started
                    if candidate_policy.endpoint_authority == "provider"
                    else None
                ),
                on_provider_endpoint=(
                    on_provider_endpoint
                    if candidate_policy.endpoint_authority == "provider"
                    else None
                ),
                on_provider_final_ready=on_provider_final_ready,
                external_endpointing_runtime=(
                    _uses_smart_turn_endpointing(candidate_policy)
                ),
                user_language=user_language,
            )
            _attach_partial_callback(candidate_session, on_partial)
            return candidate_session

        asr_session = None
        detector_ref: DetectorRuntime | None = None
        connect_started_at = time.monotonic()
        try:
            max_attempts = policy.connect_max_attempts
            for attempt in range(max_attempts):
                if not operation_is_current():
                    return stale_result(provider)
                asr_session = create_candidate(selection)
                try:
                    await asr_session.connect()
                    if not operation_is_current():
                        await self._close_asr_session(asr_session)
                        asr_session = None
                        return stale_result(provider)
                    break
                except asyncio.CancelledError:
                    try:
                        await asr_session.close()
                    except Exception:
                        pass
                    asr_session = None
                    raise
                except Exception:
                    try:
                        await asr_session.close()
                    except Exception:
                        pass
                    asr_session = None
                    if not operation_is_current():
                        return stale_result(provider)
                    if attempt + 1 >= max_attempts:
                        raise
                    backoff = min(
                        policy.connect_retry_cap_seconds,
                        policy.connect_retry_base_seconds * (2**attempt),
                    )
                    # Aggregate retry budget (Codex P1). Each attempt can burn
                    # _READY_TIMEOUT_SECONDS before ASR_CONNECT_TIMEOUT, and
                    # _start_session_activate awaits this whole loop before it
                    # sends session_started -- while the frontend cancels the
                    # start and fires end_session at
                    # _FRONTEND_START_DEADLINE_SECONDS. So on a sustained
                    # provider outage a second attempt could not finish in time
                    # no matter what: the frontend always tore the session down
                    # mid-retry, and the user saw a generic start timeout
                    # instead of the fail-closed ASR verdict this code exists to
                    # produce. Only start another attempt when its worst case
                    # still fits.
                    elapsed = time.monotonic() - connect_started_at
                    if (
                        elapsed + backoff + _READY_TIMEOUT_SECONDS
                        > _CONNECT_TOTAL_BUDGET_SECONDS
                    ):
                        logger.warning(
                            "[asr] connect retry budget exhausted after %.1fs "
                            "(provider=%s attempt=%d/%d); failing closed so the "
                            "verdict reaches the client before its start deadline",
                            elapsed,
                            provider,
                            attempt + 1,
                            max_attempts,
                        )
                        raise
                    await asyncio.sleep(backoff)
                    if not operation_is_current():
                        return stale_result(provider)
            if asr_session is None:
                raise RuntimeError("ASR_CONNECT_FAILED")
            if not operation_is_current():
                await self._close_asr_session(asr_session)
                return stale_result(provider)
            self._asr_session = asr_session
            self._asr_last_provider_wire_audio_ms = 0
            self._asr_provider = provider
            self._asr_lifecycle = VoiceInputLifecycleController(
                provider_policy=policy,
                shadow_mode=False,
                resource_optimization_enabled=(
                    self._voice_input_resource_optimization_enabled
                ),
            )
            self._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
            self._asr_lifecycle.metrics.connect_latency_ms = int(
                (time.monotonic() - connect_started_at) * 1_000
            )
            lifecycle_ref = self._asr_lifecycle

            async def on_detector_endpointing_failure() -> None:
                if not self._asr_runtime_refs_match(
                    epoch,
                    lifecycle_ref,
                    detector_ref,
                ):
                    return
                identity = self._capture_runtime_identity(
                    ingress_token=self._asr_current_ingress_token,
                )
                await self._handle_independent_asr_error(
                    epoch,
                    provider,
                    status_code="ASR_ENDPOINTING_FAILED",
                    expected_identity=identity,
                )

            async def on_detector_event(event) -> None:
                current_lifecycle_ref = self._asr_lifecycle
                if (
                    detector_ref is None
                    or current_lifecycle_ref is None
                    or epoch != self._asr_session_epoch
                ):
                    return
                accepted = self._asr_detector_dispatcher.submit_nowait(
                    CoreDetectorEventEnvelope(
                        event=event,
                        detector_ref=detector_ref,
                        lifecycle_ref=current_lifecycle_ref,
                        session_epoch=epoch,
                    )
                )
                if not accepted:
                    raise RuntimeError("ASR_DETECTOR_CONTROL_BACKPRESSURE")

            def on_speaker_candidate_bound(
                candidate: SpeakerShadowCandidateKey,
                turn_token: VoiceTurnToken,
                speaker_owner_generation: str | None,
            ) -> None:
                if (
                    detector_ref is None
                    or detector_ref is not self._asr_detector
                    or epoch != self._asr_session_epoch
                    or speaker_owner_generation is None
                    or speaker_owner_generation
                    != self._speaker_verifier_activation_generation
                ):
                    return
                self._accept_speaker_candidate_binding(
                    candidate,
                    turn_token,
                    detector=detector_ref,
                    activation_generation=speaker_owner_generation,
                )

            async with self._speaker_verifier_lock:
                if not operation_is_current():
                    return stale_result(provider)
                # Only an explicitly supplied legacy observer belongs to this
                # start. Core reconciles its desired spec after route startup.
                current_factory = speaker_shadow_factory
                factory_activation = getattr(
                    current_factory,
                    "activation_generation",
                    None,
                )
                if (
                    self._speaker_verifier_activation_generation is None
                    and type(factory_activation) is str
                    and factory_activation
                ):
                    self._speaker_verifier_activation_generation = factory_activation
                self._speaker_verifier_enforces_admission = (
                    _speaker_factory_enforces_admission(current_factory)
                )
                speaker_shadow = self._create_speaker_shadow(current_factory)
                if speaker_shadow is None:
                    # A declared policy without an installed observer has no
                    # authority. Preserve the existing fail-open contract for
                    # partials and finals until an explicit hot replacement.
                    self._speaker_verifier_enforces_admission = False
                try:
                    detector_ref = DetectorRuntime(
                        resource_optimization_enabled=(
                            self._voice_input_resource_optimization_enabled
                        ),
                        provider_policy=policy,
                        on_endpointing_failure=(
                            on_detector_endpointing_failure
                            if _uses_smart_turn_endpointing(policy)
                            else None
                        ),
                        on_event=on_detector_event,
                        speaker_shadow=speaker_shadow,
                        speaker_owner_generation=(
                            self._speaker_verifier_activation_generation
                            if speaker_shadow is not None
                            else None
                        ),
                        on_speaker_candidate_bound=on_speaker_candidate_bound,
                        provider_micro_event_config=(
                            None
                            if _uses_smart_turn_endpointing(policy)
                            else _PROVIDER_MICRO_EVENT_SHADOW_CONFIG
                        ),
                    )
                except Exception:
                    await self._close_created_speaker_shadow(speaker_shadow)
                    raise
                self._asr_detector = detector_ref
                try:
                    install_diagnostics = getattr(detector_ref, "set_pipeline_diagnostic_callback", None)
                    if callable(install_diagnostics):
                        install_diagnostics(
                            lambda fields, ingress: self._observe_endpoint_diagnostic(
                                fields, ingress, source=detector_ref, epoch=epoch,
                            )
                        )
                except Exception:
                    pass
                # The startup detector and Provider session share a fresh
                # physical audio timeline. Reconnects earn this capability
                # only after reset_provider_audio_timeline() succeeds.
                self._asr_provider_exact_session = (
                    asr_session if policy.endpoint_authority == "provider" else None
                )
            self._asr_session_factory = create_candidate
            self._asr_transport_selection = selection
            self._schedule_transport_warm_expiry(
                epoch,
                expected_state=VoiceLifecycleState.LOCAL_LISTEN,
            )
            start_identity = self._capture_runtime_identity()
            delivered = await self._send_asr_lifecycle_state(
                VoiceLifecycleState.LOCAL_LISTEN,
                provider=provider,
                session_epoch=epoch,
                expected_identity=start_identity,
            )
            if (
                not delivered
                or not operation_is_current()
                or not self._runtime_identity_matches(start_identity)
            ):
                return stale_result(provider)
            delivered = await self._send_asr_status(
                "ASR_INDEPENDENT_READY",
                provider,
                session_epoch=epoch,
                expected_identity=start_identity,
            )
            if (
                not delivered
                or not operation_is_current()
                or not self._runtime_identity_matches(start_identity)
            ):
                return stale_result(provider)
            return AsrStartResult(
                AsrStartStatus.READY,
                provider=provider,
                session_epoch=epoch,
            )
        except asyncio.CancelledError:
            if detector_ref is not None and self._asr_detector is detector_ref:
                self._asr_detector = None
                try:
                    await detector_ref.close()
                except Exception:
                    pass
            if asr_session is not None:
                await self._close_asr_session(asr_session)
            raise
        except Exception as exc:
            if detector_ref is not None and self._asr_detector is detector_ref:
                self._asr_detector = None
                try:
                    await detector_ref.close()
                except Exception:
                    pass
            if asr_session is not None:
                await self._close_asr_session(asr_session)
            if operation_is_current():
                self._asr_session = None
                self._asr_provider = None
                failure_code = (
                    "ASR_INDEPENDENT_PROVIDER_UNAVAILABLE"
                    if policy.connect_max_attempts > 1
                    else "ASR_INDEPENDENT_FAILED"
                )
                reason_code = _extract_asr_reason_code(
                    exc,
                    fallback=failure_code,
                )
                incident_id = f"asr-failure-{uuid.uuid4().hex}"
                failure_identity = self._capture_runtime_identity()
                delivered = await self._send_asr_status(
                    failure_code,
                    provider,
                    session_epoch=epoch,
                    expected_identity=failure_identity,
                    reason_code=reason_code,
                    incident_id=incident_id,
                )
                if not delivered or not operation_is_current():
                    return stale_result(provider)
                return AsrStartResult(
                    AsrStartStatus.UNAVAILABLE
                    if policy.connect_max_attempts > 1
                    else AsrStartStatus.FAILED,
                    provider=provider,
                    failure_code=failure_code,
                    session_epoch=epoch,
                )
            return stale_result(provider)

    def _create_speaker_shadow(
        self,
        factory: SpeakerShadowFactory | None,
    ) -> SpeakerShadowObserver | None:
        """Construct one lightweight observer without risking ASR startup."""

        if factory is None:
            return None
        try:
            # Model/process creation remains lazy inside the observer's first
            # accepted submission.
            shadow = factory()
        except Exception:
            self._speaker_verifier_degraded = True
            logger.warning(
                "[%s] speaker shadow factory failed; continuing without observer",
                self.display_name,
            )
            return None
        if shadow is None:
            self._speaker_verifier_degraded = True
            return None
        return shadow

    @staticmethod
    async def _close_created_speaker_shadow(
        shadow: SpeakerShadowObserver | None,
    ) -> None:
        if shadow is None:
            return
        try:
            await shadow.close()
        except Exception:
            return

    def _reset_asr_provider_transport_namespace(
        self,
        *,
        retire_owned_proofs: bool = False,
    ) -> None:
        """Detach private state keyed to one physical Provider session.

        Boundary snapshots stay in the proof registry until their owning
        correlator returns the corresponding proof for retirement.  Clearing
        the registry here would race the asynchronous Detector cleanup and
        leave the old speaker authority live.
        """

        # Exact aliases remain published until every admission effect and
        # settlement has completed. A recorded disposition alone is not a
        # takeover boundary: reset must reject adoption for as long as either
        # a staged or committed transaction remains in these maps.
        unsettled_exact = bool(
            self._asr_provider_exact_pending
            or self._asr_provider_exact_intervals
        )
        if retire_owned_proofs and unsettled_exact:
            # A reconnect candidate must not take over while the old
            # Admission/Detector split is still live. Raising here happens
            # before namespace retirement and before candidate adoption; the
            # restart loop closes that unadopted candidate and the terminal
            # invalidation path owns the old exact hold.
            for pending in self._asr_provider_exact_pending.values():
                pending.conflicted = True
                pending.completion.set()
            raise RuntimeError("ASR_EXACT_INTERVAL_RESET_UNSETTLED")
        correlator = self._asr_provider_correlator
        namespace = self._asr_provider_correlator_namespace
        if retire_owned_proofs and correlator is not None and namespace is not None:
            try:
                retired = correlator.retire_namespace(namespace)
            except ProviderAliasConflictError:
                retired = None
            if retired is not None and retired.retired_proofs:
                task = asyncio.create_task(
                    self._retire_admission_boundary_proofs(
                        retired.retired_proofs,
                        self._asr_detector,
                    ),
                    name="provider-boundary-namespace-reset",
                )
                self._track_admission_effect_task(task, None)
                task.add_done_callback(self._admission_effect_done)
        for pending in self._asr_provider_exact_pending.values():
            pending.conflicted = True
            pending.completion.set()
        for task in tuple(self._asr_exact_callback_tasks):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        for transaction in tuple(self._asr_provider_exact_intervals.values()):
            self._retire_exact_interval_runtime_aliases(transaction)
        self._asr_evidence_observer = None
        self._asr_evidence_observer_scope = None
        self._asr_provider_correlator = None
        self._asr_provider_correlator_namespace = None
        self._asr_provider_started_turns.clear()
        self._asr_deferred_provider_started_keys.clear()
        self._asr_provider_exact_intervals.clear()
        self._asr_provider_exact_pending.clear()
        self._asr_provider_exact_candidates.clear()
        self._asr_provider_speaker_ledgers.clear()
        self._asr_provider_speaker_key_ledgers.clear()
        self._asr_sealed_provider_key = None
        self._asr_provider_exact_session = None

    def _reset_asr_turn_state(self) -> None:
        """Reset per-turn bookkeeping shared by close/abort/error teardown."""

        # Both deadlines belong to the detached route. Keep cancelled owners
        # tracked until they unwind; a new route gets a fresh ticket map.
        deadline_tasks = self._asr_admission_deadline_tasks
        self._asr_admission_deadline_tasks = {}
        current = asyncio.current_task()
        for task in deadline_tasks.values():
            if task is not current and not task.done():
                task.cancel()
                self._track_terminal_close_tasks({task})
        handoff = getattr(self, "_asr_pending_turn_handoff", None)
        self._asr_pending_turn_handoff = None
        if handoff is not None and not handoff.completion.done():
            handoff.completion.set_result(False)
        self._cancel_provider_speaker_arming_tasks()
        self._asr_turn_prepared = False
        self._asr_received_audio = False
        self._asr_pending_speech_confirmed = False
        self._asr_pending_speech_onset_at = None
        self._asr_pending_detector_candidate = None
        self._asr_overlap_onset_token = None
        self._asr_overlap_onset_at = None
        self._asr_overlap_completed_token = None
        self._asr_overlap_completed_onsets.clear()
        self._asr_overlap_completed_turns = 0
        self._asr_audio_sequence = 0
        self._asr_provider_speaker_evidence_lease = None
        self._asr_current_ingress_token = None
        self._asr_partial_turn_token = None
        self._asr_admission_candidate_turns.clear()
        self._asr_admission_candidate_leases.clear()
        self._asr_admission_turn_leases.clear()
        self._asr_speaker_deny_counted_leases.clear()
        self._asr_deferred_provider_speaker_lease_events.clear()
        self._asr_deferred_provider_speaker_lease_overflow.clear()
        self._asr_provider_speaker_terminal_leases.clear()
        self._asr_current_speaker_lease = None
        self._asr_current_speaker_candidate = None
        self._asr_speaker_authority_pending_turns.clear()
        self._asr_speaker_authoritative_turns.clear()
        self._asr_quarantined_partials.clear()
        self._asr_partial_settlements.clear()
        self._asr_sealed_turn_token = None
        self._asr_provider_candidate_fence = None
        self._reset_asr_provider_transport_namespace()
        self._asr_turn_endpointed_at = None
        self._asr_turn_audio_started_at = None
        self._asr_turn_onset_at = None
        self._asr_pending_turn_onset_at = None
        self._asr_first_partial_recorded = False

    async def _notify_asr_turn_abandoned(
        self,
        turn_token: VoiceTurnToken,
    ) -> None:
        """Release the Core-side pause keyed to an abandoned prepared turn."""

        try:
            await self._callbacks.on_turn_abandoned(turn_token)
        except Exception:
            logger.debug(
                "[%s] independent ASR turn abandonment callback failed",
                self.display_name,
            )

    async def _settle_asr_transcript_terminal(
        self,
        settlement: TranscriptTerminalSettlement,
    ) -> None:
        """Settle Core ownership without revising the admission tombstone."""

        if type(settlement.admission_disposition) is not AdmissionDisposition:
            raise TypeError("ASR_TRANSCRIPT_DISPOSITION_INVALID")
        degraded = False
        try:
            await self._notify_asr_turn_abandoned(settlement.final_key.turn_token)
        except Exception:
            degraded = True
        execution = self._asr_admission_resolutions.get(settlement.final_key)
        if execution is not None and not execution.core_settled:
            execution.core_settled = True
            try:
                await self._post_admission_event(
                    settlement.final_key.turn_token,
                    CoreSettled(execution.ticket, degraded=degraded),
                )
            except KeyError:
                pass
            if execution.settled.is_set():
                self._asr_admission_resolutions.pop(
                    settlement.final_key,
                    None,
                )

    def _new_asr_transcript_dispatcher(self) -> TranscriptDispatcher:
        """Construct the production dispatcher with mandatory settlement."""

        return TranscriptDispatcher(
            self._dispatch_asr_transcript_envelope,
            settle_terminal=self._settle_asr_transcript_terminal,
            require_terminal_settlement=True,
        )

    async def _close_independent_asr(
        self,
        *,
        operation_generation: int | None = None,
    ) -> None:
        """Invalidate callbacks first, then release the detached provider session."""

        cleanup = self._detach_independent_asr(
            operation_generation=operation_generation,
        )
        if cleanup is not None:
            await cleanup

    def _schedule_provider_authority_reset(
        self,
        detector: DetectorRuntime,
        identity: _AsrRuntimeIdentity,
        *,
        activation_ready: asyncio.Future[bool],
    ) -> asyncio.Task[bool]:
        """Retire a timed-out Provider candidate before any successor feed."""

        existing = self._asr_provider_authority_reset_task
        if existing is not None and not existing.done():
            return existing

        async def reset_authority() -> bool:
            try:
                await asyncio.wait_for(
                    detector.reset(),
                    timeout=_SPEAKER_CANDIDATE_DECISION_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._runtime_identity_matches(identity):
                    await self._handle_independent_asr_error(
                        identity.session_epoch,
                        identity.provider or "unknown",
                        status_code="ASR_ENDPOINTING_FAILED",
                        expected_identity=identity,
                    )
                return False
            if not self._runtime_identity_matches(identity):
                return False
            should_activate = await activation_ready
            if not self._runtime_identity_matches(identity):
                return False
            if should_activate:
                await self._activate_pending_independent_turn(
                    identity.session_epoch,
                )
            return self._asr_runtime_refs_match(
                identity.session_epoch,
                identity.lifecycle,
                detector,
            )

        task = asyncio.create_task(
            reset_authority(),
            name="provider-authority-fail-open-reset",
        )
        self._asr_provider_authority_reset_task = task
        return task

    def _detach_independent_asr(
        self,
        *,
        operation_generation: int | None = None,
    ) -> Awaitable[None] | None:
        """Synchronously seize one generation and return its owned cleanup."""

        self._ensure_asr_runtime_state()
        if operation_generation is None:
            operation_generation = self._begin_asr_start_operation()
        elif not self._asr_start_operation_matches(operation_generation):
            return None
        self._asr_session_epoch += 1
        self._asr_audio_generation += 1
        transcript_dispatcher = self._asr_transcript_dispatcher
        detector_dispatcher = self._asr_detector_dispatcher
        audio_dispatcher = self._asr_audio_dispatcher
        admission_cleanup_task: asyncio.Task[None] | None = None
        if self._asr_admission_ingress_started:
            admission_future = self._asr_admission_ingress.invalidate_all_nowait(
                RouteReplaced()
            )
            admission_cleanup_task = asyncio.create_task(
                self._finish_admission_invalidation(
                    admission_future,
                    transcript_dispatcher,
                    self._asr_provider_correlator,
                    self._asr_provider_correlator_namespace,
                    self._asr_detector,
                ),
                name="voice-turn-admission-route-replaced",
            )
        else:
            transcript_dispatcher.invalidate_all()
        detector_dispatcher.invalidate_all()
        audio_dispatcher.abort()
        self._asr_transcript_dispatcher = self._new_asr_transcript_dispatcher()
        self._asr_detector_dispatcher = AsrDetectorDispatcher(
            self._dispatch_asr_detector_event,
            on_failure=self._handle_asr_detector_dispatcher_failure,
        )
        self._asr_audio_dispatcher = AsrAudioDispatcher(
            validator=self._asr_audio_command_is_valid,
            on_wire_audio=self._record_asr_dispatcher_wire_audio,
            on_failure=self._handle_asr_audio_dispatcher_failure,
        )
        asr_session, self._asr_session = self._asr_session, None
        lifecycle, self._asr_lifecycle = self._asr_lifecycle, None
        detector, self._asr_detector = self._asr_detector, None
        lease, self._asr_smart_turn_lease = self._asr_smart_turn_lease, None
        detached_tasks: list[asyncio.Task[Any]] = []
        authority_reset_task = self._asr_provider_authority_reset_task
        self._asr_provider_authority_reset_task = None
        if (
            authority_reset_task is not None
            and authority_reset_task is not asyncio.current_task()
        ):
            authority_reset_task.cancel()
            detached_tasks.append(authority_reset_task)
        for task_name in (
            "_asr_transport_task",
            "_asr_warm_expiry_task",
            "_asr_final_watchdog_task",
        ):
            task = getattr(self, task_name, None)
            setattr(self, task_name, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                detached_tasks.append(task)
        close_tasks = tuple(self._asr_close_tasks)
        self._asr_close_tasks = set()
        self._asr_provider = None
        if lifecycle is not None:
            lifecycle.stop()
        self._asr_provider_speaker_sequence = 0
        self._asr_buffered_provider_speaker_observation = None
        exact_callbacks = {
            task for task in self._asr_exact_callback_tasks
            if task is not asyncio.current_task() and not task.done()
        }
        self._reset_asr_turn_state()
        self._asr_session_factory = None
        self._asr_transport_selection = None

        async def finish_detached_cleanup() -> None:
            await self._bounded_terminal_task_join(
                exact_callbacks,
                deadline=time.monotonic() + _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS,
                label="exact callback stop",
                cancel_first=False,
            )
            if admission_cleanup_task is not None:
                await asyncio.gather(
                    admission_cleanup_task,
                    return_exceptions=True,
                )
            if detector is not None:
                try:
                    await detector.close()
                except Exception:
                    logger.warning(
                        "[%s] detector close failed during ASR close",
                        self.display_name,
                    )
            if lease is not None:
                try:
                    await lease.release()
                except Exception:
                    logger.warning(
                        "[%s] SmartTurn lease release failed during ASR close",
                        self.display_name,
                    )
            if asr_session is not None:
                try:
                    await asr_session.close()
                except Exception:
                    logger.warning(
                        "[%s] independent ASR close failed",
                        self.display_name,
                    )
            wait_tasks = (
                *detached_tasks,
                *close_tasks,
            )
            if wait_tasks:
                await asyncio.gather(*wait_tasks, return_exceptions=True)
            await detector_dispatcher.close()
            await audio_dispatcher.close()

        return finish_detached_cleanup()

    def _capture_frame_identity_is_new(self, frame: ProcessedVoiceFrame) -> bool:
        sequence = frame.ingress_sequence
        captured_at = frame.captured_at
        valid = bool(
            type(sequence) is int
            and sequence > 0
            and isinstance(captured_at, (int, float))
            and not isinstance(captured_at, bool)
            and captured_at > 0
            and sequence > self._asr_last_ingress_sequence
            and captured_at > self._asr_last_captured_at
        )
        if valid:
            self._asr_last_ingress_sequence = sequence
            self._asr_last_captured_at = float(captured_at)
        return valid

    def _deny_rearm_identity_matches(
        self,
        *,
        cleanup_generation: int,
        session_epoch: int,
        detector: DetectorRuntime,
        detector_epoch: int,
        lifecycle: VoiceInputLifecycleController,
        ingress_token: VoiceIngressToken,
        cutoff_sequence: int,
        state: DenyTransportState,
        last_sequence: int,
        last_captured_at: float,
    ) -> bool:
        """Confirm that one post-DENY detector operation still owns re-arm."""

        return bool(
            self._asr_deny_cleanup_generation == cleanup_generation
            and self._asr_session_epoch == session_epoch
            and self._asr_detector is detector
            and detector.detector_epoch == detector_epoch
            and self._asr_lifecycle is lifecycle
            and self._asr_current_ingress_token == ingress_token
            and self._ingress_token_matches(ingress_token)
            and self._asr_rearm_cutoff_sequence == cutoff_sequence
            and self._asr_deny_transport_state is state
            and self._asr_rearm_last_sequence == last_sequence
            and self._asr_rearm_last_captured_at == last_captured_at
        )

    async def _submit_deny_rearm_frame(
        self,
        frame: ProcessedVoiceFrame,
        *,
        ingress_token: VoiceIngressToken,
        identity_valid: bool,
    ) -> bool:
        """Use only contiguous post-cleanup Silero facts to re-arm egress."""

        sequence = frame.ingress_sequence
        captured_at = frame.captured_at
        captured_at_valid = bool(
            isinstance(captured_at, (int, float))
            and not isinstance(captured_at, bool)
            and captured_at > 0
        )
        effective_captured_at = float(captured_at) if captured_at_valid else 0.0
        previous_sequence = self._asr_rearm_last_sequence
        previous_captured_at = self._asr_rearm_last_captured_at
        contiguous = bool(
            identity_valid
            and sequence > self._asr_rearm_cutoff_sequence
            and sequence == previous_sequence + 1
            and effective_captured_at > previous_captured_at
        )
        if not contiguous:
            valid_sequence = (
                sequence if type(sequence) is int and sequence > 0 else 0
            )
            cutoff_sequence = max(
                self._asr_rearm_cutoff_sequence,
                self._asr_rearm_last_sequence,
                self._asr_last_ingress_sequence,
                valid_sequence,
            )
            self._asr_rearm_cutoff_sequence = cutoff_sequence
            self._asr_rearm_last_sequence = cutoff_sequence
            self._asr_rearm_last_captured_at = max(
                self._asr_rearm_last_captured_at,
                self._asr_last_captured_at,
                effective_captured_at if identity_valid else 0.0,
            )
            self._asr_deny_transport_state = DenyTransportState.WAIT_SILENCE
            return False
        detector = self._asr_detector
        lifecycle = self._asr_lifecycle
        if detector is None or lifecycle is None:
            return False
        cleanup_generation = self._asr_deny_cleanup_generation
        session_epoch = self._asr_session_epoch
        detector_epoch = detector.detector_epoch
        cutoff_sequence = self._asr_rearm_cutoff_sequence
        state = self._asr_deny_transport_state
        if state not in {
            DenyTransportState.WAIT_SILENCE,
            DenyTransportState.ARMED,
        }:
            return False

        if state is DenyTransportState.WAIT_SILENCE:
            try:
                prepared = await detector.prepare_deny_rearm(
                    cleanup_generation=cleanup_generation,
                    cutoff_sequence=cutoff_sequence,
                    expected_detector_epoch=detector_epoch,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            if not prepared or not self._deny_rearm_identity_matches(
                cleanup_generation=cleanup_generation,
                session_epoch=session_epoch,
                detector=detector,
                detector_epoch=detector_epoch,
                lifecycle=lifecycle,
                ingress_token=ingress_token,
                cutoff_sequence=cutoff_sequence,
                state=state,
                last_sequence=previous_sequence,
                last_captured_at=previous_captured_at,
            ):
                return False
        try:
            result = await detector.feed(
                frame.pcm16,
                speech_probability=frame.speech_probability,
                rnnoise_available=frame.rnnoise_available,
                rnnoise_evidence=frame.rnnoise_evidence,
                ingress_token=ingress_token,
                allow_baseline_update=True,
            )
        except Exception:
            return False
        if not self._deny_rearm_identity_matches(
            cleanup_generation=cleanup_generation,
            session_epoch=session_epoch,
            detector=detector,
            detector_epoch=detector_epoch,
            lifecycle=lifecycle,
            ingress_token=ingress_token,
            cutoff_sequence=cutoff_sequence,
            state=state,
            last_sequence=previous_sequence,
            last_captured_at=previous_captured_at,
        ):
            return False
        if not result.endpointing_available:
            return False
        self._asr_rearm_last_sequence = sequence
        self._asr_rearm_last_captured_at = effective_captured_at
        events = tuple(result.events)
        if state is DenyTransportState.WAIT_SILENCE:
            if SpeechActivityEvent.CANDIDATE_PAUSE not in events:
                return False
            self._asr_deny_transport_state = DenyTransportState.ARMED
            # TURN_DENIED already cleared every pre-DENY/pre-cleanup buffer.
            return False
        onset = next(
            (
                event
                for event in events
                if event
                in {
                    SpeechActivityEvent.SPEECH_STARTED,
                    SpeechActivityEvent.SPEECH_RESUMED,
                }
            ),
            None,
        )
        if onset is None:
            # Only post-silence PCM may populate the next turn's pre-roll.
            lifecycle.accept_audio(
                frame.pcm16,
                sample_rate_hz=frame.sample_rate_hz,
            )
            return False
        self._asr_deny_transport_state = DenyTransportState.OPEN
        self._asr_deny_cleanup_active = False
        await self._handle_independent_asr_activity(
            onset,
            session_epoch,
        )
        return bool(
            self._asr_deny_cleanup_generation == cleanup_generation
            and self._asr_session_epoch == session_epoch
            and self._asr_detector is detector
            and detector.detector_epoch == detector_epoch
            and self._asr_lifecycle is lifecycle
            and self._asr_current_ingress_token == ingress_token
            and self._ingress_token_matches(ingress_token)
            and self._asr_rearm_cutoff_sequence == cutoff_sequence
            and self._asr_deny_transport_state is DenyTransportState.OPEN
        )

    async def submit(
        self,
        frame: ProcessedVoiceFrame,
        *,
        ingress_token: VoiceIngressToken,
    ) -> AsrSubmitResult:
        """Submit one normalized frame to the independent-ASR hard route."""

        self._ensure_asr_runtime_state()
        handoff_identity = self._capture_runtime_identity(ingress_token=ingress_token)
        self._observe_pipeline_audio("audio_received", ingress_token, len(frame.pcm16) // 2)


        if not await self._await_pending_turn_handoff(handoff_identity):
            return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'handoff_stale'))
        if self._asr_deny_transport_state in {
            DenyTransportState.DENY_FENCED,
            DenyTransportState.RETIRING,
            DenyTransportState.QUARANTINED,
        }:
            return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_fenced'))
        if self._asr_lifecycle is None:
            return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'missing_lifecycle'))
        if not self._ingress_token_matches(ingress_token):
            return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'ingress_stale'))
        previous_ingress_token = self._asr_current_ingress_token
        deny_rearm_pending = self._asr_deny_transport_state in {
            DenyTransportState.WAIT_SILENCE,
            DenyTransportState.ARMED,
        }
        ingress_identity_changed = bool(
            deny_rearm_pending
            and previous_ingress_token is not None
            and previous_ingress_token != ingress_token
        )
        if ingress_identity_changed and previous_ingress_token is not None:
            previous_order = (
                previous_ingress_token.route_generation,
                previous_ingress_token.lease_generation,
            )
            incoming_order = (
                ingress_token.route_generation,
                ingress_token.lease_generation,
            )
            if incoming_order <= previous_order:
                # A late socket may share the current session/audio epochs.
                # Reject it before its sequence can poison the new route.
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'older_route'))
        self._asr_current_ingress_token = ingress_token
        identity_valid = self._capture_frame_identity_is_new(frame)
        if ingress_identity_changed:
            identity_valid = False
        deny_cleanup_generation = self._asr_deny_cleanup_generation
        if self._asr_deny_transport_state in {
            DenyTransportState.WAIT_SILENCE,
            DenyTransportState.ARMED,
        }:
            rearmed = await self._submit_deny_rearm_frame(
                frame,
                ingress_token=ingress_token,
                identity_valid=identity_valid,
            )
            if not rearmed:
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_wait_silence'))
            if (
                self._asr_deny_transport_state is not DenyTransportState.OPEN
                or self._asr_deny_cleanup_generation != deny_cleanup_generation
                or self._asr_current_ingress_token != ingress_token
                or not self._ingress_token_matches(ingress_token)
            ):
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_rearm_changed'))
        authority_reset_task = self._asr_provider_authority_reset_task
        if authority_reset_task is not None:
            reset_succeeded = False
            try:
                reset_succeeded = bool(
                    await asyncio.wait_for(
                        asyncio.shield(authority_reset_task),
                        timeout=_SPEAKER_CANDIDATE_DECISION_TIMEOUT_SECONDS,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                reset_succeeded = False
            if self._asr_provider_authority_reset_task is authority_reset_task:
                if authority_reset_task.done():
                    self._asr_provider_authority_reset_task = None
                elif not reset_succeeded:
                    authority_reset_task.cancel()
            if not reset_succeeded:
                identity = self._capture_runtime_identity(
                    ingress_token=ingress_token,
                )
                await self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    status_code="ASR_ENDPOINTING_FAILED",
                    expected_identity=identity,
                )
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'authority_reset_failed'))
            if (
                self._asr_deny_transport_state is not DenyTransportState.OPEN
                or self._asr_deny_cleanup_generation != deny_cleanup_generation
            ):
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
        identity = self._capture_runtime_identity(ingress_token=ingress_token)
        failure_context = self._new_audio_failure_context("submit", identity)

        pcm16 = frame.pcm16
        sample_rate_hz = frame.sample_rate_hz
        speech_probability = frame.speech_probability
        rnnoise_available = frame.rnnoise_available
        rnnoise_evidence = frame.rnnoise_evidence
        provider_detector_identity: DetectorIngressIdentity | None = None
        split_before_provider_audio = False
        uses_smart_turn = False

        try:
            lifecycle = identity.lifecycle
            detector = identity.detector

            def ingress_is_current() -> bool:
                return self._runtime_identity_matches(identity)

            def deny_cleanup_is_current() -> bool:
                return bool(
                    self._asr_deny_transport_state is DenyTransportState.OPEN
                    and self._asr_deny_cleanup_generation == deny_cleanup_generation
                )

            if lifecycle is not None and detector is not None:
                submit_audio = getattr(detector, "submit_audio", None)
                uses_smart_turn = _uses_smart_turn_endpointing(
                    lifecycle.provider_policy
                )
                if uses_smart_turn and callable(submit_audio):
                    detector_submit_started_at = time.perf_counter()
                    submitted = await submit_audio(
                        pcm16,
                        ingress_token=ingress_token,
                        sample_rate_hz=sample_rate_hz,
                        speech_probability=speech_probability,
                        rnnoise_available=bool(rnnoise_available),
                        rnnoise_evidence=rnnoise_evidence,
                        allow_baseline_update=(
                            lifecycle.snapshot.state
                            in {
                                VoiceLifecycleState.LOCAL_LISTEN,
                                VoiceLifecycleState.WARM_IDLE,
                            }
                        ),
                    )
                    if not deny_cleanup_is_current():
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
                    if not ingress_is_current():
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'ingress_stale'))
                    self._observe_detector_audio_result(submitted, ingress_token, len(pcm16) // 2)
                    lifecycle.metrics.detector_submit_latency_ms = int(
                        (time.perf_counter() - detector_submit_started_at) * 1_000
                    )
                    lifecycle.metrics.detector_queue_audio_ms = detector.queued_audio_ms
                    lifecycle.metrics.detector_queue_high_water_ms = max(
                        lifecycle.metrics.detector_queue_high_water_ms,
                        detector.queued_audio_ms,
                    )
                    lifecycle.metrics.smart_turn_inference_ms = (
                        detector.smart_turn_evaluation_ms
                    )
                    lifecycle.metrics.smart_turn_stale_result_count = (
                        detector.smart_turn_stale_result_count
                    )
                    lifecycle.metrics.smart_turn_coalesced_evaluation_count = (
                        detector.smart_turn_coalesced_evaluation_count
                    )
                    if submitted.status is DetectorSubmitStatus.SKIPPED_QUIET:
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'quiet_skipped'))
                    if submitted.status is DetectorSubmitStatus.BACKPRESSURE:
                        lifecycle.metrics.detector_overflow_count += 1
                        await self._handle_audio_ingress_backpressure(
                            ingress_token,
                            observed_state=lifecycle.snapshot.state,
                        )
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'detector_backpressure'))
                    if (
                        submitted.status
                        in {DetectorSubmitStatus.CLOSED, DetectorSubmitStatus.FAILED}
                        or not submitted.endpointing_available
                    ):
                        await self._handle_independent_asr_error(
                            identity.session_epoch,
                            identity.provider or "unknown",
                            status_code="ASR_ENDPOINTING_FAILED",
                            expected_identity=identity,
                        )
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'endpointing_unavailable'))
                    if not submitted.throttle_available:
                        lifecycle.enable_independent_asr_fail_open()
                        if (
                            not submitted.control_event_emitted
                            and submitted.identity is not None
                            and submitted.candidate is not None
                        ):
                            accepted = self._asr_detector_dispatcher.submit_nowait(
                                CoreDetectorEventEnvelope(
                                    event=DetectorPrewarmEvent(
                                        ingress=submitted.identity,
                                        candidate=submitted.candidate,
                                        kind="continuous",
                                    ),
                                    detector_ref=detector,
                                    lifecycle_ref=lifecycle,
                                    session_epoch=identity.session_epoch,
                                )
                            )
                            if not accepted:
                                await self._handle_independent_asr_error(
                                    identity.session_epoch,
                                    identity.provider or "unknown",
                                    status_code="ASR_ENDPOINTING_FAILED",
                                    expected_identity=identity,
                                )
                                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'control_backpressure'))
                else:
                    detector_result = await detector.feed(
                        pcm16,
                        speech_probability=speech_probability,
                        rnnoise_available=rnnoise_available,
                        rnnoise_evidence=rnnoise_evidence,
                        ingress_token=ingress_token,
                        allow_baseline_update=(
                            lifecycle.snapshot.state
                            in {
                                VoiceLifecycleState.LOCAL_LISTEN,
                                VoiceLifecycleState.WARM_IDLE,
                            }
                        ),
                    )
                    if not deny_cleanup_is_current():
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
                    if not ingress_is_current():
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'ingress_stale'))
                    self._observe_detector_audio_result(detector_result, ingress_token, len(pcm16) // 2)
                    if not detector_result.endpointing_available:
                        await self._handle_independent_asr_error(
                            identity.session_epoch,
                            identity.provider or "unknown",
                            status_code="ASR_ENDPOINTING_FAILED",
                            expected_identity=identity,
                        )
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'endpointing_unavailable'))
                    if detector_result.throttle_action is ThrottleAction.SKIP_IDLE_PCM:
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'idle_skipped'))
                    if not detector_result.throttle_available:
                        lifecycle.enable_independent_asr_fail_open()
                    provider_owns_turns = bool(
                        lifecycle.provider_policy.endpoint_authority == "provider"
                    )
                    if detector_result.throttle_available and not provider_owns_turns:
                        provider_detector_identity = detector_result.identity
                        for event in detector_result.events:
                            split_before_provider_audio = bool(
                                await self._handle_independent_asr_activity(
                                    event,
                                    identity.session_epoch,
                                )
                                or split_before_provider_audio
                            )
                            if not ingress_is_current():
                                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'activity_stale'))
                    elif provider_owns_turns:
                        # Provider authority owns logical utterance identity.
                        # Local Silero remains an idle/throttle signal only: it
                        # may decide that this PCM should reach the transport,
                        # but its start/resume/pause events cannot mint turns,
                        # rotate speaker evidence, or request a split.
                        provider_detector_identity = detector_result.identity
                        split_before_provider_audio = False
                    pending_speech_confirmed = bool(
                        not provider_owns_turns
                        and lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
                        and any(
                            event
                            in {
                                SpeechActivityEvent.SPEECH_STARTED,
                                SpeechActivityEvent.SPEECH_RESUMED,
                            }
                            for event in detector_result.events
                        )
                    )
                    continuous_provider_wake = bool(
                        provider_owns_turns
                        or not detector_result.throttle_available
                        or not self._voice_input_resource_optimization_enabled
                    )
                    if continuous_provider_wake:
                        if not await self._await_pending_turn_handoff(identity):
                            return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'handoff_stale'))
                        if not await self._ensure_continuous_provider_wake(
                            lifecycle,
                            identity.session_epoch,
                            detector_identity=detector_result.identity,
                            candidate=detector_result.candidate,
                            expected_identity=identity,
                        ):
                            if not ingress_is_current():
                                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'transport_open_stale'))
                            return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'transport_open_failed'))
                        if not deny_cleanup_is_current():
                            return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
                    elif not await self._bind_provider_detector_candidate(
                        lifecycle,
                        detector,
                        detector_identity=detector_result.identity,
                        candidate=detector_result.candidate,
                        expected_identity=identity,
                        pending_speech_confirmed=pending_speech_confirmed,
                    ):
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'turn_activation_stale'))
                    if not deny_cleanup_is_current():
                        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
            if lifecycle is not None and not ingress_is_current():
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'ingress_stale'))
            if not deny_cleanup_is_current():
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
            if not await self._await_pending_turn_handoff(identity):
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'handoff_stale'))
            decision = (
                lifecycle.accept_audio(pcm16, sample_rate_hz=sample_rate_hz)
                if lifecycle is not None
                else None
            )
            if decision is not None and decision.disposition is AudioDisposition.BLOCK:
                if decision.backpressure:
                    await self._handle_audio_ingress_backpressure(
                        ingress_token,
                        observed_state=lifecycle.snapshot.state,
                    )
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'lifecycle_blocked'))
            if decision is not None and decision.disposition in {
                AudioDisposition.BUFFER,
                AudioDisposition.SUPPRESS,
            }:
                if (
                    decision.disposition is AudioDisposition.BUFFER
                    and not uses_smart_turn
                ):
                    self._record_buffered_provider_speaker_observation(
                        identity=provider_detector_identity,
                        byte_count=len(pcm16),
                        split_before_audio=split_before_provider_audio,
                        evidence_complete=(provider_detector_identity is not None),
                    )
                if (
                    lifecycle is not None
                    and lifecycle.snapshot.state
                    in {
                        VoiceLifecycleState.PREWARMING,
                        VoiceLifecycleState.BACKOFF,
                    }
                    and (
                        self._asr_session is None
                        or not getattr(self._asr_session, "is_ready", True)
                    )
                ):
                    self._ensure_transport_restart_task()
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'lifecycle_buffered' if decision.disposition is AudioDisposition.BUFFER else 'lifecycle_suppressed'))
            if lifecycle is None or detector is None:
                await self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    status_code="ASR_BLOCKED_ENDPOINTING",
                    expected_identity=identity,
                )
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'endpointing_blocked'))
            turn_token = self._capture_turn_token(lifecycle)
            if (
                lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
                or not self._asr_endpointing_ready(lifecycle, detector, turn_token)
            ):
                await self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    status_code="ASR_BLOCKED_ENDPOINTING",
                    expected_identity=identity,
                )
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'endpointing_blocked'))
            asr_session = self._asr_session
            if asr_session is None or not getattr(asr_session, "is_ready", True):
                self._ensure_transport_restart_task()
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'transport_restarting'))
            payload = (
                decision.pre_roll
                if decision is not None
                and decision.disposition is AudioDisposition.FORWARD_WITH_PRE_ROLL
                else pcm16
            )
            if not payload:
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'empty_payload'))
            if not ingress_is_current():
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'ingress_stale'))
            if self._asr_audio_dispatcher.active_turn != turn_token:
                if not await self._activate_asr_audio_dispatcher(
                    lifecycle,
                    turn_token,
                ):
                    await self._handle_independent_asr_error(
                        identity.session_epoch,
                        identity.provider or "unknown",
                        status_code="ASR_AUDIO_ORDERING_FAILED",
                        expected_identity=identity,
                        failed_operation="submit",
                        failed_check="dispatcher_activation_rejected",
                    )
                    return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'dispatcher_activation_failed'))
            arming = await self._arm_speaker_authority_for_provider_audio(
                turn_token
            )
            if not deny_cleanup_is_current():
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
            if (
                not ingress_is_current()
                or self._asr_session is not asr_session
                or self._asr_detector is not detector
                or self._asr_lifecycle is not lifecycle
                or lifecycle.current_turn_token != turn_token
            ):
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'speaker_arming_stale'))
            if (
                self._speaker_verifier_enforces_admission
                and (
                    not arming
                    or arming.owner_generation
                    != self._speaker_verifier_activation_generation
                )
            ):
                if arming.status is _SpeakerArmingStatus.STALE:
                    return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'speaker_arming_stale'))
                await self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    status_code="ASR_AUDIO_ORDERING_FAILED",
                    reason_code=arming.reason_code or "ASR_SPEAKER_ARMING_FAILED",
                    failed_operation="submit",
                    failed_check="speaker_arming_invariant",
                    expected_identity=identity,
                )
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'speaker_arming_failed'))
            if arming.status is _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE:
                self._schedule_speaker_evidence_unavailable(
                    identity,
                    arming.reason_code,
                    activation_generation=arming.owner_generation,
                )
            split_payload_is_ambiguous = bool(
                split_before_provider_audio
                and decision is not None
                and decision.disposition is AudioDisposition.FORWARD_WITH_PRE_ROLL
            )
            pre_roll_ownership_is_ambiguous = bool(
                decision is not None
                and decision.disposition is AudioDisposition.FORWARD_WITH_PRE_ROLL
                and payload != pcm16
            )
            if not await self._observe_admitted_provider_audio(
                lifecycle,
                detector,
                payload,
                sample_rate_hz=sample_rate_hz,
                identity=provider_detector_identity,
                split_before_audio=bool(
                    split_before_provider_audio and not split_payload_is_ambiguous
                ),
                evidence_complete=not (
                    split_payload_is_ambiguous or pre_roll_ownership_is_ambiguous
                ),
                turn_token=turn_token,
                failure_context=failure_context,
                speaker_evidence_unavailable=(
                    arming.status is _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE
                ),
            ):
                if not ingress_is_current():
                    return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'observation_stale'))
                await self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    status_code="ASR_AUDIO_ORDERING_FAILED",
                    expected_identity=identity,
                    failure_context=failure_context,
                )
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'audio_observation_failed'))
            if not deny_cleanup_is_current():
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
            if (
                not ingress_is_current()
                or self._asr_session is not asr_session
                or self._asr_detector is not detector
                or self._asr_lifecycle is not lifecycle
                or lifecycle.current_turn_token != turn_token
            ):
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'observation_stale'))
            self._asr_audio_sequence += 1
            if not deny_cleanup_is_current():
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'deny_cleanup_changed'))
            if not self._asr_audio_dispatcher.enqueue_audio(
                turn_token,
                asr_session,
                payload,
                sample_rate_hz=sample_rate_hz,
                sequence_no=self._asr_audio_sequence,
            ):
                failure_context.fail("enqueue_rejected", actual=self._audio_failure_scalars())
                await self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    status_code="ASR_AUDIO_ORDERING_FAILED",
                    expected_identity=identity,
                    failure_context=failure_context,
                )
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'enqueue_failed'))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_context.fail("submit_exception", actual=self._audio_failure_scalars(), error=exc, send_state="unknown")
            if not self._runtime_identity_matches(identity):
                return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.STALE, ingress_token, frame, 'exception_stale'))
            self._asr_received_audio = True
            status_code = (
                "ASR_STREAM_BACKPRESSURE"
                if str(exc).startswith("ASR_STREAM_BACKPRESSURE:")
                else "ASR_INDEPENDENT_STREAM_FAILED"
            )
            if (
                status_code == "ASR_STREAM_BACKPRESSURE"
                and identity.lifecycle is not None
            ):
                identity.lifecycle.metrics.queue_backpressure_count += 1
            await self._handle_independent_asr_error(
                identity.session_epoch,
                identity.provider or "unknown",
                status_code=status_code,
                expected_identity=identity,
                failure_context=failure_context,
            )
            return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.UNAVAILABLE, ingress_token, frame, 'submit_exception'))

        return AsrSubmitResult(self._pipeline_audio_receipt(AsrSubmitStatus.ACCEPTED, ingress_token, frame, 'enqueued'))

    def _ensure_transport_restart_task(self) -> None:
        task = self._asr_transport_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._restart_transport(),
            name="independent-asr-transport-restart",
        )
        task.add_done_callback(self._log_asr_background_task_failure)
        self._asr_transport_task = task

    def _log_asr_background_task_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "[%s] independent ASR background task %s failed",
                self.display_name,
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _restart_transport(self, *, max_attempts: int | None = None) -> None:
        if max_attempts is not None and max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        async with self._asr_transport_lock:
            lifecycle = self._asr_lifecycle
            if lifecycle is None:
                return
            existing = self._asr_session
            if existing is not None and getattr(existing, "is_ready", True):
                return
            if existing is not None:
                self._asr_session = None
                self._asr_provider_exact_session = None
                detached_identity = self._capture_runtime_identity()
                await self._close_asr_session(existing)
                if not self._runtime_identity_matches(detached_identity):
                    return
            lifecycle = self._asr_lifecycle
            factory = self._asr_session_factory
            selection = self._asr_transport_selection
            identity = self._capture_runtime_identity()
            if factory is None or selection is None or lifecycle is None:
                await self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    expected_identity=identity,
                )
                return
            # Mirror initial startup: the active provider policy decides the
            # attempt budget and backoff ladder unless the caller overrides it.
            policy = lifecycle.provider_policy
            if max_attempts is None:
                max_attempts = policy.connect_max_attempts

            for attempt in range(max_attempts):
                if not self._runtime_identity_matches(identity):
                    return
                if lifecycle.snapshot.state is VoiceLifecycleState.BACKOFF:
                    lifecycle.transition(VoiceLifecycleEvent.RETRY)
                    lifecycle.metrics.reconnect_count += 1
                    identity = self._capture_runtime_identity()
                    await self._send_asr_lifecycle_state(
                        VoiceLifecycleState.PREWARMING,
                        provider=identity.provider or "unknown",
                        session_epoch=identity.session_epoch,
                        expected_identity=identity,
                    )
                    if not self._runtime_identity_matches(identity):
                        return
                candidate = None
                try:
                    connect_started_at = time.monotonic()
                    candidate = factory(selection)
                    await candidate.connect()
                    if not self._runtime_identity_matches(identity):
                        try:
                            await candidate.close()
                        except Exception:
                            pass
                        return
                    detector = self._asr_detector
                    reset_provider_timeline = getattr(
                        detector,
                        "reset_provider_audio_timeline",
                        None,
                    )
                    exact_timeline_ready = False
                    if policy.endpoint_authority == "provider" and callable(
                        reset_provider_timeline
                    ):
                        try:
                            exact_timeline_ready = bool(await reset_provider_timeline())
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # A speaker-only reset failure must not turn a
                            # connected replacement into an ASR outage.
                            exact_timeline_ready = False
                    if not self._runtime_identity_matches(identity):
                        try:
                            await candidate.close()
                        except Exception:
                            pass
                        return
                    # No await is allowed between namespace retirement and
                    # adoption. Provider callbacks are accepted only after
                    # _asr_session points at this candidate.
                    self._reset_asr_provider_transport_namespace(
                        retire_owned_proofs=True
                    )
                    self._asr_provider_exact_session = (
                        candidate
                        if policy.endpoint_authority == "provider"
                        and exact_timeline_ready
                        else None
                    )
                    self._asr_session = candidate
                    self._asr_last_provider_wire_audio_ms = 0
                    lifecycle.invalidate_transport()
                    connected_identity = self._capture_runtime_identity()
                    lifecycle.metrics.connect_latency_ms = int(
                        (time.monotonic() - connect_started_at) * 1_000
                    )
                    if (
                        self._asr_pending_speech_confirmed
                        and lifecycle.snapshot.state is VoiceLifecycleState.PREWARMING
                    ):
                        detector = self._asr_detector
                        turn_token = self._capture_turn_token(lifecycle)
                        if detector is None or not self._asr_endpointing_ready(
                            lifecycle,
                            detector,
                            turn_token,
                        ):
                            await self._handle_independent_asr_error(
                                connected_identity.session_epoch,
                                connected_identity.provider or "unknown",
                                status_code="ASR_BLOCKED_ENDPOINTING",
                                expected_identity=connected_identity,
                            )
                            return
                        lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
                        self._asr_turn_onset_at = (
                            self._asr_pending_speech_onset_at
                            if self._asr_pending_speech_onset_at is not None
                            else time.monotonic()
                        )
                        self._asr_pending_speech_confirmed = False
                        self._asr_pending_speech_onset_at = None
                        self._asr_turn_audio_started_at = time.monotonic()
                        self._asr_first_partial_recorded = False
                        await self._send_asr_lifecycle_state(
                            VoiceLifecycleState.ACTIVE,
                            provider=connected_identity.provider or "unknown",
                            session_epoch=connected_identity.session_epoch,
                            expected_identity=connected_identity,
                        )
                        if not self._runtime_identity_matches(connected_identity):
                            return
                        payload = lifecycle.drain_active_start_audio()
                        await self._prepare_independent_asr_turn(
                            connected_identity.session_epoch
                        )
                        if not self._runtime_identity_matches(connected_identity):
                            return
                        if not await self._activate_asr_audio_dispatcher(
                            lifecycle,
                            turn_token,
                            buffered_pcm16=payload,
                        ):
                            await self._handle_independent_asr_error(
                                connected_identity.session_epoch,
                                connected_identity.provider or "unknown",
                                status_code="ASR_AUDIO_ORDERING_FAILED",
                                failed_operation="restart",
                                failed_check="dispatcher_activation_rejected",
                                expected_identity=connected_identity,
                            )
                            return
                    return
                except asyncio.CancelledError:
                    if candidate is not None and self._asr_session is candidate:
                        adopted_identity = self._capture_runtime_identity()
                        await self._handle_independent_asr_error(
                            adopted_identity.session_epoch,
                            adopted_identity.provider or "unknown",
                            status_code="ASR_INDEPENDENT_FAILED",
                            expected_identity=adopted_identity,
                        )
                    elif candidate is not None:
                        try:
                            await candidate.close()
                        except Exception:
                            pass
                    raise
                except Exception:
                    if candidate is not None and self._asr_session is candidate:
                        adopted_identity = self._capture_runtime_identity()
                        await self._handle_independent_asr_error(
                            adopted_identity.session_epoch,
                            adopted_identity.provider or "unknown",
                            status_code="ASR_INDEPENDENT_FAILED",
                            expected_identity=adopted_identity,
                        )
                        return
                    if candidate is not None:
                        try:
                            await candidate.close()
                        except Exception:
                            pass
                    if not self._runtime_identity_matches(identity):
                        return
                    if lifecycle.snapshot.state is VoiceLifecycleState.PREWARMING:
                        lifecycle.transition(VoiceLifecycleEvent.CONNECT_FAILED)
                        identity = self._capture_runtime_identity()
                        await self._send_asr_lifecycle_state(
                            VoiceLifecycleState.BACKOFF,
                            provider=identity.provider or "unknown",
                            session_epoch=identity.session_epoch,
                            expected_identity=identity,
                        )
                        if not self._runtime_identity_matches(identity):
                            return
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(
                            min(
                                policy.connect_retry_cap_seconds,
                                policy.connect_retry_base_seconds * (2**attempt),
                            )
                        )
                        if not self._runtime_identity_matches(identity):
                            return
                        continue
            if not self._runtime_identity_matches(identity):
                return
            if lifecycle.snapshot.state is VoiceLifecycleState.BACKOFF:
                lifecycle.transition(VoiceLifecycleEvent.RETRIES_EXHAUSTED)
            await self._handle_independent_asr_error(
                identity.session_epoch,
                identity.provider or "unknown",
                status_code="ASR_INDEPENDENT_FAILED",
                expected_identity=identity,
            )

    async def _abort_transport(
        self,
        reason: str,
    ) -> _AsrRuntimeIdentity:
        """Invalidate provider I/O before closing a live transport."""

        self._begin_asr_start_operation()
        self._asr_audio_generation += 1
        transcript_dispatcher = self._asr_transcript_dispatcher
        admission_cleanup = None
        if self._asr_admission_ingress_started:
            admission_cleanup = self._finish_admission_invalidation(
                self._asr_admission_ingress.invalidate_all_nowait(RouteReplaced()),
                transcript_dispatcher,
                self._asr_provider_correlator,
                self._asr_provider_correlator_namespace,
                self._asr_detector,
            )
        else:
            transcript_dispatcher.invalidate_all()
        # Seize the physical resources and register their close owner before
        # the first suspension. Admission must finish before transcript
        # teardown, but it must not retain the old Provider connection when
        # this caller is cancelled. Turn bookkeeping still settles afterward,
        # under the post-detach identity fence.
        lease, self._asr_smart_turn_lease = self._asr_smart_turn_lease, None
        for task_name in (
            "_asr_transport_task",
            "_asr_warm_expiry_task",
            "_asr_final_watchdog_task",
        ):
            task = getattr(self, task_name, None)
            setattr(self, task_name, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        asr_session, self._asr_session = self._asr_session, None
        lifecycle = self._asr_lifecycle
        if lifecycle is not None:
            lifecycle.invalidate_transport()
        post_detach = self._capture_runtime_identity()

        async def settle_abort() -> None:
            if admission_cleanup is not None:
                await admission_cleanup
            if not self._runtime_identity_matches(post_detach):
                return
            self._asr_detector_dispatcher.invalidate_all()
            self._asr_audio_dispatcher.abort()
            self._asr_provider_speaker_sequence = 0
            self._asr_buffered_provider_speaker_observation = None
            self._reset_asr_turn_state()
            if lifecycle is not None:
                lifecycle.metrics.asr_abort_discarded_command_count = (
                    self._asr_audio_dispatcher.asr_abort_discarded_command_count
                )

        async def finish_abort() -> None:
            try:
                if lease is not None:
                    async with asyncio.timeout(_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS):
                        await lease.release()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "[%s] SmartTurn lease release failed during ASR abort",
                    self.display_name,
                )
            finally:
                if asr_session is not None:
                    try:
                        async with asyncio.timeout(_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS):
                            await asr_session.close()
                    except Exception:
                        logger.warning(
                            "[%s] independent ASR abort failed reason=%s",
                            self.display_name,
                            reason,
                        )

        admission_cleanup_task = self._schedule_owned_cleanup(
            settle_abort(),
            name="independent-asr-abort-admission",
        )
        cleanup_task = self._schedule_owned_cleanup(
            finish_abort(),
            name="independent-asr-abort-transport",
        )
        await asyncio.shield(admission_cleanup_task)
        await asyncio.shield(cleanup_task)
        return post_detach

    async def _close_transport_only(self) -> None:
        """Enter deep sleep while preserving microphone detection."""

        epoch = self._asr_session_epoch
        provider = self._asr_provider or "unknown"
        warm_task = self._asr_warm_expiry_task
        if warm_task is not None and warm_task is not asyncio.current_task():
            warm_task.cancel()
        self._asr_warm_expiry_task = None
        asr_session, self._asr_session = self._asr_session, None
        self._asr_provider_exact_session = None
        session_close_task = None
        if asr_session is not None:

            async def close_transport() -> None:
                try:
                    await asr_session.close()
                except Exception:
                    logger.warning(
                        "[%s] independent ASR transport-only close failed",
                        self.display_name,
                    )

            session_close_task = self._schedule_owned_cleanup(
                close_transport(),
                name="independent-asr-transport-close",
            )
        lifecycle = self._asr_lifecycle
        if lifecycle is not None:
            lifecycle.invalidate_transport()
            if lifecycle.snapshot.state in {
                VoiceLifecycleState.LOCAL_LISTEN,
                VoiceLifecycleState.WARM_IDLE,
            }:
                lifecycle.transition(VoiceLifecycleEvent.WARM_EXPIRED)
                identity = self._capture_runtime_identity()
                await self._send_asr_lifecycle_state(
                    VoiceLifecycleState.DEEP_SLEEP,
                    provider=provider,
                    session_epoch=epoch,
                    expected_identity=identity,
                )
        if session_close_task is not None:
            await asyncio.shield(session_close_task)

    def _schedule_transport_warm_expiry(
        self,
        epoch: int,
        *,
        expected_state: VoiceLifecycleState,
    ) -> None:
        task = self._asr_warm_expiry_task
        if task is not None:
            task.cancel()
        lifecycle = self._asr_lifecycle
        if lifecycle is None or not self._voice_input_resource_optimization_enabled:
            return
        if expected_state is VoiceLifecycleState.WARM_IDLE:
            ttl_ms = lifecycle.provider_policy.warm_transport_ms
        elif expected_state in {
            VoiceLifecycleState.LOCAL_LISTEN,
            VoiceLifecycleState.PREWARMING,
        }:
            ttl_ms = lifecycle.config.default_warm_transport_ms
        else:
            raise ValueError(
                "transport expiry requires local-listen, prewarming, or warm-idle"
            )
        session_ref = self._asr_session
        detector_ref = self._asr_detector
        transport_generation = lifecycle.snapshot.transport_generation

        def timer_is_current() -> bool:
            return bool(
                epoch == self._asr_session_epoch
                and self._asr_lifecycle is lifecycle
                and self._asr_session is session_ref
                and self._asr_detector is detector_ref
                and lifecycle.snapshot.transport_generation == transport_generation
            )

        async def expire() -> None:
            try:
                await asyncio.sleep(ttl_ms / 1_000)
                if (
                    not timer_is_current()
                    or lifecycle.snapshot.state is not expected_state
                ):
                    return
                if expected_state is VoiceLifecycleState.PREWARMING:
                    lease, self._asr_smart_turn_lease = (
                        self._asr_smart_turn_lease,
                        None,
                    )
                    if lease is not None:
                        await lease.release()
                    if not timer_is_current():
                        return
                    if detector_ref is not None:
                        await detector_ref.reset()
                    if (
                        not timer_is_current()
                        or lifecycle.snapshot.state
                        is not VoiceLifecycleState.PREWARMING
                    ):
                        return
                    lifecycle.transition(VoiceLifecycleEvent.PREWARM_EXPIRED)
                    self._asr_pending_speech_confirmed = False
                    self._asr_pending_speech_onset_at = None
                    self._asr_pending_detector_candidate = None
                    identity = self._capture_runtime_identity()
                    delivered = await self._send_asr_lifecycle_state(
                        VoiceLifecycleState.LOCAL_LISTEN,
                        provider=identity.provider or "unknown",
                        session_epoch=identity.session_epoch,
                        expected_identity=identity,
                    )
                    if (
                        not delivered
                        or not timer_is_current()
                        or lifecycle.snapshot.state
                        is not VoiceLifecycleState.LOCAL_LISTEN
                    ):
                        return
                await self._close_transport_only()
            except asyncio.CancelledError:
                return
            finally:
                if self._asr_warm_expiry_task is asyncio.current_task():
                    self._asr_warm_expiry_task = None

        warm_task = asyncio.create_task(
            expire(),
            name="independent-asr-warm-expiry",
        )
        warm_task.add_done_callback(self._log_asr_background_task_failure)
        self._asr_warm_expiry_task = warm_task

    def _schedule_provider_final_watchdog(
        self,
        epoch: int,
        lifecycle: VoiceInputLifecycleController,
        sealed_token: VoiceTransportToken,
    ) -> None:
        task = self._asr_final_watchdog_task
        if task is not None:
            task.cancel()
        timeout_ms = lifecycle.provider_policy.provider_final_timeout_ms

        async def expire() -> None:
            try:
                await asyncio.sleep(timeout_ms / 1_000)
                if (
                    epoch != self._asr_session_epoch
                    or self._asr_lifecycle is not lifecycle
                    or self._asr_sealed_turn_token != sealed_token
                    or lifecycle.snapshot.state is not VoiceLifecycleState.DRAINING
                ):
                    return
                await self._handle_independent_asr_error(
                    epoch,
                    self._asr_provider or "unknown",
                    status_code="ASR_PROVIDER_FINAL_TIMEOUT",
                )
            except asyncio.CancelledError:
                return

        watchdog_task = asyncio.create_task(
            expire(),
            name="independent-asr-provider-final-watchdog",
        )
        watchdog_task.add_done_callback(self._log_asr_background_task_failure)
        self._asr_final_watchdog_task = watchdog_task

    def _sync_provider_wire_metrics(
        self,
        asr_session: Any,
        *,
        fallback_audio_bytes: int = 0,
    ) -> None:
        lifecycle = self._asr_lifecycle
        if lifecycle is None:
            return
        cumulative_ms = getattr(asr_session, "provider_wire_audio_ms", None)
        if isinstance(cumulative_ms, int) and not isinstance(cumulative_ms, bool):
            delta_ms = max(0, cumulative_ms - self._asr_last_provider_wire_audio_ms)
            self._asr_last_provider_wire_audio_ms = max(
                self._asr_last_provider_wire_audio_ms,
                cumulative_ms,
            )
            if delta_ms:
                lifecycle.record_provider_wire_audio(delta_ms)
            return
        if (
            lifecycle.provider_policy.transport == "streaming"
            and fallback_audio_bytes > 0
        ):
            lifecycle.record_provider_wire_audio(
                fallback_audio_bytes * 1_000 // (16_000 * 2)
            )

    async def _handle_independent_asr_activity(
        self,
        event: SpeechActivityEvent,
        epoch: int,
    ) -> bool:
        # 同上：onset 是收到这个语音活动事件的时刻。
        detected_at = time.monotonic()
        if epoch != self._asr_session_epoch:
            return False
        provider = self._asr_provider or "unknown"
        lifecycle = self._asr_lifecycle
        if (
            lifecycle is not None
            and lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
            and event
            in {
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.SPEECH_RESUMED,
            }
        ):
            ingress_token = self._asr_current_ingress_token
            if ingress_token is None or not self._ingress_token_matches(ingress_token):
                return False
            lifecycle.mark_pending_turn_speech(ingress_token)
            if self._asr_pending_turn_onset_at is None:
                self._asr_pending_turn_onset_at = detected_at
            return False
        if (
            lifecycle is not None
            and lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
            and lifecycle.has_pending_turn
            and event
            in {
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.SPEECH_RESUMED,
            }
        ):
            # The DRAINING path already confirmed this pending turn. Re-marking
            # it after PROVIDER_FINAL reaches WARM_IDLE violates the lifecycle
            # guard and can fail the replacement turn during activation.
            return False
        if lifecycle is not None and event in {
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.SPEECH_RESUMED,
        }:
            ingress_token = self._asr_current_ingress_token
            if ingress_token is None or not self._ingress_token_matches(ingress_token):
                # An idle ingress-backpressure bump keeps the provider session
                # adopted, so a trailing session-side speech event can still
                # reach this handler with a stale audio generation. The wake
                # path below cannot mint a turn token without a current
                # ingress token, so drop the stale event cleanly instead of
                # raising into the provider adapter. Genuinely new speech
                # re-arms the current token through submit() first.
                return False
            previous_state = lifecycle.snapshot.state
            state = previous_state
            if state in {
                VoiceLifecycleState.LOCAL_LISTEN,
                VoiceLifecycleState.DEEP_SLEEP,
                VoiceLifecycleState.WARM_IDLE,
            }:
                warm_task = self._asr_warm_expiry_task
                if warm_task is not None:
                    warm_task.cancel()
                    self._asr_warm_expiry_task = None
                if state is VoiceLifecycleState.WARM_IDLE:
                    lifecycle.metrics.warm_hit_count += 1
                lifecycle.open_turn(ingress_token)
                state = lifecycle.snapshot.state
            if state is VoiceLifecycleState.PREWARMING:
                if not await self._ensure_smart_turn_ready(lifecycle, epoch):
                    return False
                asr_session = self._asr_session
                if asr_session is not None and getattr(asr_session, "is_ready", True):
                    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
                    # session 先未就绪、随后又 ready 时，真实开口时刻是当初记下的那个
                    # pending onset —— 直接用当前时钟会把整段重连等待算成「开口之后」，
                    # 期间拍的帧全被排除在本回合外（CodeRabbit Major）。
                    self._asr_turn_onset_at = (
                        self._asr_pending_speech_onset_at
                        if self._asr_pending_speech_onset_at is not None
                        else detected_at
                    )
                    self._asr_pending_speech_confirmed = False
                    self._asr_pending_speech_onset_at = None
                else:
                    self._asr_pending_speech_confirmed = True
                    if self._asr_pending_speech_onset_at is None:
                        self._asr_pending_speech_onset_at = detected_at
            if lifecycle.snapshot.state is not previous_state:
                identity = self._capture_runtime_identity(
                    ingress_token=self._asr_current_ingress_token,
                )
                delivered = await self._send_asr_lifecycle_state(
                    lifecycle.snapshot.state,
                    provider=provider,
                    session_epoch=epoch,
                    expected_identity=identity,
                )
                if not delivered:
                    return False
            if (
                lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
                and previous_state is not VoiceLifecycleState.ACTIVE
            ):
                self._asr_turn_audio_started_at = time.monotonic()
                self._asr_first_partial_recorded = False
        if event not in {
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.SPEECH_RESUMED,
        }:
            if event is SpeechActivityEvent.CANDIDATE_PAUSE:
                # Once local VAD observes a pause, a later provider final may
                # simply be the current utterance ending, so replaying the
                # remembered onset at that final would wake a ghost turn. The
                # onset must not be dropped outright either: when the pause
                # closes a genuine overlapping utterance, its provider endpoint
                # and final are still queued in the ordered FIFO behind the
                # previous turn's final. Convert the onset into a
                # completed-overlap credit; only a provider endpoint arriving
                # in WARM_IDLE proves a queued turn exists and redeems it.
                onset_token = self._asr_overlap_onset_token
                onset_at = self._asr_overlap_onset_at
                self._asr_overlap_onset_token = None
                self._asr_overlap_onset_at = None
                if onset_token is not None:
                    # 一张 credit 配一个时刻，按兑付顺序排队。
                    self._asr_overlap_completed_onsets.append(
                        onset_at if onset_at is not None else detected_at
                    )
                    if onset_token == self._asr_overlap_completed_token:
                        # Each additional onset+pause cycle observed while the
                        # first turn stays ACTIVE queues one more provider
                        # endpoint/final pair, so count credits per cycle.
                        self._asr_overlap_completed_turns += 1
                    else:
                        # 换了 ingress 身份：旧队列作废，只留这一张。
                        last = self._asr_overlap_completed_onsets.pop()
                        self._asr_overlap_completed_onsets.clear()
                        self._asr_overlap_completed_onsets.append(last)
                        self._asr_overlap_completed_token = onset_token
                        self._asr_overlap_completed_turns = 1
            return False
        if self._asr_turn_prepared:
            if (
                lifecycle is not None
                and lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
                and lifecycle.provider_policy.endpoint_authority == "provider"
            ):
                # Provider-VAD endpoints ride the ordered callback FIFO right
                # before their own final, so a genuine next-turn onset can
                # reach Core while the previous turn is still ACTIVE and
                # prepared. Remember the onset (ingress-fenced) so the delayed
                # final can replay it instead of dropping the next turn.
                self._asr_overlap_onset_token = self._asr_current_ingress_token
                self._asr_overlap_onset_at = detected_at
                return event is SpeechActivityEvent.SPEECH_RESUMED
            return False
        if (
            lifecycle is not None
            and lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
        ):
            return False

        await self._prepare_independent_asr_turn(epoch)
        return False

    async def _prepare_independent_asr_turn(self, epoch: int) -> None:
        """Reserve Core and admission ownership before observations can arrive."""

        if epoch != self._asr_session_epoch or self._asr_turn_prepared:
            return
        lifecycle = self._asr_lifecycle
        if (
            lifecycle is None
            or lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
        ):
            return
        turn_token = self._capture_turn_token(lifecycle)
        identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=turn_token,
        )
        operation_generation = self._asr_start_generation
        transcript_dispatcher = self._asr_transcript_dispatcher

        def preparation_is_current() -> bool:
            return bool(
                not self._asr_terminal_close_requested
                and self._asr_start_operation_matches(operation_generation)
                and self._runtime_identity_matches(identity)
                and self._asr_transcript_dispatcher is transcript_dispatcher
            )

        try:
            if not self._asr_admission_ingress_started:
                await self._asr_admission_ingress.start()
                self._asr_admission_ingress_started = True
                if not preparation_is_current():
                    return
            if turn_token not in self._asr_admission_turn_leases:
                await self._asr_admission_ingress.open_turn(turn_token)
        except Exception:
            await self._handle_independent_asr_error(
                epoch,
                self._asr_provider or "unknown",
                status_code="ASR_CORE_TRANSCRIPT_BACKPRESSURE",
                expected_identity=identity,
            )
            return
        if not preparation_is_current():
            # Detach already queued RouteReplaced on this lane; it owns stale
            # record retirement. Never reserve into the replacement dispatcher.
            return
        final_key = FinalKey.from_turn(turn_token)
        ownership = self._asr_provider_turn_ownerships.get(turn_token)
        reserved_dispatcher = self._asr_admission_reservation_dispatchers.get(final_key)
        if reserved_dispatcher is None:
            if not transcript_dispatcher.try_reserve(final_key):
                await self._post_admission_event(turn_token, Reset())
                await self._handle_independent_asr_error(
                    epoch,
                    self._asr_provider or "unknown",
                    status_code="ASR_CORE_TRANSCRIPT_BACKPRESSURE",
                    expected_identity=identity,
                )
                return
            self._asr_admission_reservation_dispatchers[final_key] = transcript_dispatcher
        elif reserved_dispatcher is not transcript_dispatcher:
            return
        if ownership is not None:
            if (
                ownership.final_key != final_key
                or ownership.transcript_dispatcher is not transcript_dispatcher
                or ownership.lifecycle is not lifecycle
            ):
                return
            ownership.state = _ProviderTurnOwnershipState.CORE_PREPARING
        self._asr_turn_prepared = True

        async def abandon_preparation() -> None:
            if (
                self._runtime_identity_matches(identity)
                and self._asr_transcript_dispatcher is transcript_dispatcher
            ):
                self._asr_turn_prepared = False
                if self._asr_partial_turn_token == turn_token:
                    self._asr_partial_turn_token = None
            try:
                await self._post_admission_event(turn_token, Reset())
            except (AdmissionIngressClosedError, KeyError):
                pass

        try:
            accepted = await self._callbacks.on_prepare_turn(turn_token)
        except asyncio.CancelledError:
            await abandon_preparation()
            raise
        except Exception:
            accepted = False
            if self._runtime_identity_matches(identity):
                logger.warning(
                    "[%s] independent ASR turn preparation failed",
                    self.display_name,
                )
        if accepted and preparation_is_current():
            self._asr_partial_turn_token = turn_token
            if ownership is not None:
                ownership.state = _ProviderTurnOwnershipState.CORE_READY
            return
        await abandon_preparation()

    def _consume_overlap_completed_credit(self) -> None:
        """Retire one redeemed completed-overlap credit and its onset."""

        self._asr_overlap_completed_turns -= 1
        if self._asr_overlap_completed_onsets:
            self._asr_overlap_completed_onsets.popleft()
        if self._asr_overlap_completed_turns == 0:
            self._asr_overlap_completed_token = None
            self._asr_overlap_completed_onsets.clear()

    @staticmethod
    def _provider_key_namespace(
        key: ProviderUtteranceKey,
    ) -> tuple[int, int]:
        return key.generation, key.buffer_epoch

    def _accept_provider_timeline(self, key: ProviderUtteranceKey) -> bool:
        """Accept one current/new Provider namespace and reject stale epochs."""

        namespace = self._provider_key_namespace(key)
        current = self._asr_provider_correlator_namespace
        if current is not None and namespace < current:
            return False
        if current != namespace:
            previous = self._asr_provider_correlator
            if previous is not None and current is not None:
                try:
                    retired = previous.retire_namespace(current)
                except ProviderAliasConflictError:
                    retired = None
                if retired is not None and retired.retired_proofs:
                    task = asyncio.create_task(
                        self._retire_admission_boundary_proofs(
                            retired.retired_proofs,
                            self._asr_detector,
                        ),
                        name="provider-boundary-namespace-retirement",
                    )
                    self._track_admission_effect_task(task, None)
                    task.add_done_callback(self._admission_effect_done)
            self._asr_provider_correlator_namespace = namespace
            self._asr_provider_correlator = ProviderTurnCorrelator(
                namespace=namespace,
                proof_capacity=_MAX_PROVIDER_BOUNDARY_SNAPSHOTS,
            )
        return True

    def _provider_key_timeline_is_current(
        self,
        key: ProviderUtteranceKey,
    ) -> bool:
        return self._asr_provider_correlator_namespace == self._provider_key_namespace(
            key
        )

    def _reserve_provider_turn_ownership(
        self,
        key: ProviderUtteranceKey,
        turn_token: VoiceTurnToken,
        *,
        lease_token: SpeakerCaptureLeaseToken | None,
        lifecycle: VoiceInputLifecycleController,
        correlator: ProviderTurnCorrelator,
        expected_identity: _AsrRuntimeIdentity,
    ) -> _ProviderTurnOwnership | None:
        """Reserve the physical transcript slot before publishing a child."""

        existing = self._asr_provider_turn_ownerships.get(turn_token)
        if existing is not None:
            return (
                existing
                if existing.provider_key == key
                and existing.lifecycle is lifecycle
                and existing.correlator is correlator
                and existing.runtime_identity == expected_identity
                and existing.state is not _ProviderTurnOwnershipState.RETIRED
                else None
            )
        dispatcher = self._asr_transcript_dispatcher
        final_key = FinalKey.from_turn(turn_token)
        if not dispatcher.try_reserve(final_key):
            return None
        ownership = _ProviderTurnOwnership(
            turn_token=turn_token,
            provider_key=key,
            speaker_lease_token=lease_token,
            final_key=final_key,
            transcript_dispatcher=dispatcher,
            lifecycle=lifecycle,
            correlator=correlator,
            session=expected_identity.session,
            runtime_identity=expected_identity,
        )
        self._asr_admission_reservation_dispatchers[final_key] = dispatcher
        self._asr_provider_turn_ownerships[turn_token] = ownership
        return ownership

    def _release_unpublished_provider_turn_ownership(
        self,
        ownership: _ProviderTurnOwnership,
    ) -> None:
        """Release only a provisional slot that Admission never published."""

        if ownership.child_published or ownership.state in {
            _ProviderTurnOwnershipState.RESOLVED,
            _ProviderTurnOwnershipState.RETIRED,
        }:
            return
        if self._asr_provider_turn_ownerships.get(ownership.turn_token) is ownership:
            self._asr_provider_turn_ownerships.pop(ownership.turn_token, None)
        if (
            self._asr_admission_reservation_dispatchers.get(ownership.final_key)
            is ownership.transcript_dispatcher
        ):
            self._asr_admission_reservation_dispatchers.pop(ownership.final_key, None)
            ownership.transcript_dispatcher.release(ownership.final_key)
        ownership.state = _ProviderTurnOwnershipState.RETIRED

    def _retire_provider_turn_ownership(
        self,
        ownership: _ProviderTurnOwnership,
    ) -> None:
        """Forget a resolved ownership without releasing its tombstone."""

        if self._asr_provider_turn_ownerships.get(ownership.turn_token) is ownership:
            self._asr_provider_turn_ownerships.pop(ownership.turn_token, None)
            ledger = self._asr_provider_speaker_key_ledgers.get(ownership.provider_key)
            if (
                ledger is not None
                and ledger.turn_token == ownership.turn_token
                and ledger.candidate not in self._asr_provider_exact_candidates
                and self._asr_provider_speaker_evidence_lease is not ledger.evidence_lease
                and self._asr_current_speaker_candidate != ledger.candidate
            ):
                ledger.state = _ProviderSpeakerLedgerState.RESOLVED
                self._asr_provider_speaker_key_ledgers.pop(ownership.provider_key, None)
                if self._asr_provider_speaker_ledgers.get(ledger.candidate) is ledger:
                    self._asr_provider_speaker_ledgers.pop(ledger.candidate, None)
        ownership.state = _ProviderTurnOwnershipState.RETIRED

    async def _wait_provider_turn_effects(
        self,
        turn_token: VoiceTurnToken,
    ) -> None:
        """Join exact admission effects emitted while compensating started."""

        deadline = time.monotonic() + _ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS
        while True:
            pending = tuple(
                task
                for task, owner in tuple(
                    self._asr_admission_effect_task_turns.items()
                )
                if owner == turn_token
                and task is not asyncio.current_task()
                and not task.done()
            )
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            _, still_pending = await asyncio.wait(
                pending,
                timeout=remaining,
            )
            if still_pending:
                return

    async def _settle_published_provider_turn_ownership(
        self,
        ownership: _ProviderTurnOwnership,
        *,
        denied: bool,
    ) -> None:
        """Tombstone a child that Admission has already observed."""

        lease_token = ownership.speaker_lease_token
        cleanup = (
            self._asr_speaker_deny_cleanups.get(lease_token)
            if denied and lease_token is not None
            else None
        )
        if cleanup is not None and not cleanup.settled.is_set():
            if (
                cleanup.failure_reason is not None
                or cleanup.generation != self._asr_deny_cleanup_generation
                or not self._deny_cleanup_owns_provider_turn(cleanup, ownership)
                or not self._handoff_deny_cleanup_reservation(
                    cleanup,
                    ownership.final_key,
                    ownership.transcript_dispatcher,
                    ownership=ownership,
                )
            ):
                await self._fail_speaker_deny_cleanup(
                    cleanup,
                    "ASR_DENY_CLEANUP_TRANSCRIPT_CONFLICT",
                )
            # The active cleanup is the sole DROP/tombstone owner. This old
            # callback must neither settle nor wait on the operation that is
            # waiting for its provider callback ticket to exit.
            return
        if denied and cleanup is not None:
            # A failed/quarantined cleanup keeps ownership fenced for explicit
            # microphone restart; stale callbacks cannot release its slots.
            return

        if not ownership.child_published:
            if denied:
                if cleanup is None:
                    if self._asr_deny_transport_state not in {
                        DenyTransportState.WAIT_SILENCE,
                        DenyTransportState.ARMED,
                    }:
                        return
                    receipt = ownership.transcript_dispatcher.resolve_reserved(
                        ownership.final_key,
                        AdmissionDisposition.DROP,
                    )
                    if receipt.outcome not in {
                        TranscriptResolutionOutcome.APPLIED,
                        TranscriptResolutionOutcome.ALREADY_SAME,
                        TranscriptResolutionOutcome.NOT_RESERVED,
                    }:
                        self._asr_deny_transport_state = (
                            DenyTransportState.QUARANTINED
                        )
                        self._asr_deny_cleanup_active = True
                        return
                    self._asr_admission_reservation_dispatchers.pop(
                        ownership.final_key,
                        None,
                    )
                    self._retire_provider_turn_ownership(ownership)
                    return
            self._release_unpublished_provider_turn_ownership(ownership)
            return
        parent_abandoned = False
        if not denied and lease_token is not None:
            try:
                future = self._asr_admission_ingress.post_speaker_lease_nowait(
                    lease_token,
                    SpeakerLeaseAbandoned(),
                )
                result = await self._consume_speaker_lease_future(
                    lease_token,
                    future,
                )
                parent_abandoned = bool(result)
            except (AdmissionIngressClosedError, KeyError):
                pass
        event: object = BoundaryUnknown(ownership.provider_key) if denied else Reset()
        try:
            if not parent_abandoned:
                await self._post_admission_event(ownership.turn_token, event)
        except (AdmissionIngressClosedError, KeyError):
            pass
        await self._wait_provider_turn_effects(ownership.turn_token)
        if lease_token is not None:
            try:
                await self._asr_admission_ingress.retire_speaker_lease(lease_token)
            except (AdmissionIngressClosedError, KeyError):
                pass
            if not denied:
                self._asr_deferred_provider_speaker_lease_events.pop(
                    lease_token,
                    None,
                )
                self._asr_deferred_provider_speaker_lease_overflow.discard(
                    lease_token
                )
                if self._asr_current_speaker_lease == lease_token:
                    candidate = self._asr_current_speaker_candidate
                    self._asr_current_speaker_lease = None
                    self._asr_current_speaker_candidate = None
                    if candidate is not None:
                        self._asr_admission_candidate_leases.pop(candidate, None)
                        self._asr_admission_candidate_turns.pop(candidate, None)
        # Admission may already have retired the child (reset/close races). In
        # that case no reducer effect remains to consume the physical slot, so
        # Runtime must write the exact tombstone itself. Never release a slot
        # after child_published became observable.
        if self._asr_provider_turn_ownerships.get(ownership.turn_token) is ownership:
            dispatcher = self._asr_admission_reservation_dispatchers.pop(
                ownership.final_key,
                None,
            )
            if dispatcher is not None:
                disposition = (
                    AdmissionDisposition.DROP
                    if denied
                    else AdmissionDisposition.ABANDON
                )
                try:
                    receipt = dispatcher.resolve_reserved(
                        ownership.final_key,
                        disposition,
                    )
                except Exception:
                    receipt = None
                if receipt is None or receipt.outcome not in {
                    TranscriptResolutionOutcome.APPLIED,
                    TranscriptResolutionOutcome.ALREADY_SAME,
                }:
                    if denied and cleanup is not None:
                        await self._fail_speaker_deny_cleanup(
                            cleanup,
                            "ASR_DENY_CLEANUP_TRANSCRIPT_UNSAFE",
                        )
            self._retire_provider_turn_ownership(ownership)
        self._asr_admission_turn_leases.pop(ownership.turn_token, None)
        self._asr_speaker_authoritative_turns.discard(ownership.turn_token)
        if (
            self._asr_provider_started_turns.get(ownership.provider_key)
            == ownership.turn_token
        ):
            self._asr_provider_started_turns.pop(ownership.provider_key, None)

    async def _anchor_provider_speaker_ledger(
        self,
        ledger: _ProviderSpeakerProvisionalLedger,
        notification: ProviderUtteranceStartedNotification,
        *,
        identity: _AsrRuntimeIdentity,
        detector: DetectorRuntime,
        turn_token: VoiceTurnToken | None = None,
    ) -> bool:
        """Bind deferred PCM to one canonical Provider start or fail open."""

        start = notification.audio_start_sample_16k
        key = notification.key
        anchored_turn = turn_token or identity.turn_token
        if ledger.provider_key not in {None, key}:
            self._poison_provider_speaker_ledger(ledger, "provider_key_conflict")
            return False
        if ledger.turn_token not in {None, anchored_turn}:
            self._poison_provider_speaker_ledger(ledger, "turn_identity_conflict")
            return False
        ledger.provider_key = key
        ledger.turn_token = anchored_turn
        self._asr_provider_speaker_key_ledgers[key] = ledger

        def anchor_is_current() -> bool:
            lifecycle = identity.lifecycle
            return bool(
                self._runtime_identity_matches(identity)
                and self._asr_provider_speaker_key_ledgers.get(key) is ledger
                and ledger.turn_token == anchored_turn
                and (
                    anchored_turn is None
                    or (
                        lifecycle is not None
                        and anchored_turn in {
                            lifecycle.current_turn_token, lifecycle.pending_turn_token
                        }
                    )
                )
            )
        if ledger.poisoned_reason is not None:
            # Preserve identity binding, but never rehabilitate a ledger that
            # already observed a gap/conflict/overflow before started.
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            return False
        if start is None:
            self._poison_provider_speaker_ledger(ledger, "missing_canonical_start")
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            return False
        anchor = getattr(detector, "anchor_provider_speaker_evidence", None)
        if not callable(anchor):
            self._poison_provider_speaker_ledger(ledger, "anchor_unsupported")
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            return False
        deadline = time.monotonic() + _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS
        result: Any = None
        while True:
            try:
                result = await anchor(
                    ledger.evidence_lease,
                    audio_start_sample_16k=start,
                    deadline=deadline,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                result = None
            if not anchor_is_current():
                return False
            status = getattr(getattr(result, "status", None), "value", None)
            if status != "pending":
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                observed = await asyncio.wait_for(
                    detector.wait_provider_audio_observed_through(start),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                observed = False
            if not observed or not anchor_is_current():
                break
        if not anchor_is_current():
            return False
        status = getattr(getattr(result, "status", None), "value", None)
        if status == "conflict":
            self._speaker_rejection_metrics["speaker_anchor_conflict_count"] += 1
            self._poison_provider_speaker_ledger(ledger, "anchor_conflict")
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            return False
        if status not in {"applied", "idempotent"}:
            origin = getattr(result, "buffer_origin_sample_16k", -1)
            if isinstance(origin, int) and start < origin:
                self._speaker_rejection_metrics["speaker_anchor_evicted_count"] += 1
            self._poison_provider_speaker_ledger(ledger, "anchor_unavailable")
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            return False
        if (
            getattr(result, "lease", None) != ledger.evidence_lease
            or getattr(result, "candidate", None) != ledger.candidate
            or getattr(result, "detector_epoch", None) != detector.detector_epoch
            or getattr(result, "lease_generation", None)
            != ledger.evidence_lease.lease_generation
            or getattr(result, "anchor_start_sample_16k", None) != start
        ):
            self._poison_provider_speaker_ledger(ledger, "anchor_receipt_conflict")
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            return False
        pcm_fence = getattr(result, "pcm_through_sequence_no", None)
        if type(pcm_fence) is not int or pcm_fence < 0:
            self._poison_provider_speaker_ledger(
                ledger,
                "anchor_pcm_sequence_missing",
            )
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            return False
        ledger.detector_epoch = result.detector_epoch
        ledger.timeline_generation = result.timeline_generation
        ledger.lease_generation = result.lease_generation
        ledger.anchor_revision = result.anchor_revision
        ledger.anchor_start_sample_16k = result.anchor_start_sample_16k
        ledger.buffer_origin_sample_16k = result.buffer_origin_sample_16k
        ledger.observed_through_sample_16k = result.observed_through_sample_16k
        ledger.pcm_sequence_fence = pcm_fence
        if (
            ledger.last_pcm_sequence_no > 0
            and pcm_fence > ledger.last_pcm_sequence_no
        ):
            self._poison_provider_speaker_ledger(
                ledger,
                "anchor_pcm_sequence_conflict",
            )
            self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
            return False
        ledger.state = _ProviderSpeakerLedgerState.ANCHORED_SCORING
        self._speaker_rejection_metrics["speaker_anchor_success_count"] += 1
        return True

    async def _open_provider_speaker_parent(
        self,
        ledger: _ProviderSpeakerProvisionalLedger,
        *,
        turn_token: VoiceTurnToken,
        identity: _AsrRuntimeIdentity,
        lifecycle: VoiceInputLifecycleController,
        detector: DetectorRuntime,
    ) -> SpeakerCaptureLeaseToken | None:
        """Open the Admission parent only after Provider started settled."""

        candidate = ledger.candidate
        lease_token = ledger.lease_token
        if lease_token is not None:
            return lease_token
        if not self._asr_admission_ingress_started:
            await self._asr_admission_ingress.start()
            self._asr_admission_ingress_started = True
        if not self._runtime_identity_matches(identity):
            return None
        self._asr_speaker_lease_nonce += 1
        lease_token = SpeakerCaptureLeaseToken(
            session_generation=self._asr_session_epoch,
            start_generation=self._asr_start_generation,
            transport_generation=lifecycle.snapshot.transport_generation,
            detector_epoch=detector.detector_epoch,
            lease_nonce=self._asr_speaker_lease_nonce,
        )
        cancelled: asyncio.CancelledError | None = None
        try:
            open_future = self._asr_admission_ingress.open_speaker_lease_nowait(
                lease_token,
                candidate,
            )
            try:
                lease_record = await asyncio.shield(open_future)
            except asyncio.CancelledError as exc:
                cancelled = exc
                lease_record = await asyncio.shield(open_future)
        except Exception:
            if cancelled is not None:
                raise cancelled
            return None
        if cancelled is not None:
            try:
                await self._asr_admission_ingress.post_speaker_lease(
                    lease_token,
                    SpeakerLeaseAbandoned(),
                )
                await self._asr_admission_ingress.retire_speaker_lease(
                    lease_token
                )
            except Exception:
                pass
            raise cancelled
        if (
            not self._runtime_identity_matches(identity)
            or lease_record.lease_token != lease_token
            or lease_record.candidate != candidate
            or lease_record.state is not SpeakerLeaseState.COLLECTING
            or lease_record.last_speaker_sequence_no != 0
        ):
            try:
                await self._asr_admission_ingress.post_speaker_lease(
                    lease_token,
                    SpeakerLeaseAbandoned(),
                )
            except Exception:
                pass
            return None
        ledger.lease_token = lease_token
        self._asr_admission_candidate_leases[candidate] = lease_token
        self._asr_admission_candidate_turns[candidate] = turn_token
        self._asr_current_speaker_lease = lease_token
        self._asr_current_speaker_candidate = candidate
        return lease_token

    async def _publish_provider_ledger_unavailable(
        self,
        ledger: _ProviderSpeakerProvisionalLedger,
    ) -> bool:
        lease_token = ledger.lease_token
        if lease_token is None:
            return False
        try:
            result = await self._asr_admission_ingress.post_speaker_lease(
                lease_token,
                SpeakerLeaseUnavailable(ledger.candidate, 1),
            )
            await self._apply_speaker_lease_result(lease_token, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return bool(
            result.after_state is SpeakerLeaseState.UNAVAILABLE
            and result.outcome
            in {
                SpeakerLeaseTransitionOutcome.APPLIED,
                SpeakerLeaseTransitionOutcome.IDEMPOTENT,
            }
        )

    async def _bind_provider_turn_speaker_unavailable(
        self,
        ownership: _ProviderTurnOwnership,
        *,
        owner_generation: str,
        identity: _AsrRuntimeIdentity,
    ) -> bool:
        """Bind Provider text while making only speaker proof unavailable."""

        if not self._runtime_identity_matches(identity):
            return False
        try:
            if not self._asr_admission_ingress_started:
                await self._asr_admission_ingress.start()
                self._asr_admission_ingress_started = True
            await self._asr_admission_ingress.open_turn(ownership.turn_token)
            await self._post_admission_event(
                ownership.turn_token,
                ProviderBound(ownership.provider_key),
            )
            await self._post_admission_event(
                ownership.turn_token,
                SpeakerAuthorityPending(owner_generation),
            )
            await self._post_admission_event(
                ownership.turn_token,
                SpeakerAuthorityUnarmed(owner_generation),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        if not self._runtime_identity_matches(identity):
            return False
        ownership.child_published = True
        ownership.state = _ProviderTurnOwnershipState.CHILD_BOUND
        self._asr_provider_started_turns[ownership.provider_key] = (
            ownership.turn_token
        )
        self._asr_speaker_authoritative_turns.add(ownership.turn_token)
        self._speaker_rejection_metrics["speaker_unavailable_count"] += 1
        self._speaker_rejection_metrics[
            "speaker_unavailable_reason_identity_count"
        ] += 1
        return True

    async def _attach_current_lease_to_provider_turn(
        self,
        key: ProviderUtteranceKey,
        turn_token: VoiceTurnToken,
        *,
        lifecycle: VoiceInputLifecycleController,
        correlator: ProviderTurnCorrelator,
        expected_identity: _AsrRuntimeIdentity,
    ) -> bool:
        lease_token = self._asr_current_speaker_lease
        candidate = self._asr_current_speaker_candidate
        evidence_lease = self._asr_provider_speaker_evidence_lease
        if (
            lease_token is None
            or candidate is None
            or evidence_lease is None
            or evidence_lease.candidate != candidate
            or self._asr_admission_candidate_leases.get(candidate) != lease_token
        ):
            return False
        return await self._attach_provider_turn_to_speaker_lease(
            turn_token,
            key,
            lease_token,
            candidate,
            expected_identity=expected_identity,
            lifecycle=lifecycle,
            correlator=correlator,
        )

    async def _bind_provider_utterance_started(
        self,
        notification: ProviderUtteranceStartedNotification,
        epoch: int,
    ) -> _ProviderStartedOutcome:
        """Bind text identity, then independently settle canonical speaker anchor."""

        deny_generation = self._asr_deny_cleanup_generation
        if (
            type(notification) is not ProviderUtteranceStartedNotification
            or epoch != self._asr_session_epoch
        ):
            return _ProviderStartedOutcome.STALE
        self._schedule_provider_boundary_diagnostic(notification, epoch)
        if self._deny_transport_blocks_provider_egress():
            return _ProviderStartedOutcome.DENIED_SETTLED

        key = notification.key
        if not self._accept_provider_timeline(key):
            return _ProviderStartedOutcome.STALE
        correlator = self._asr_provider_correlator
        lifecycle = self._asr_lifecycle
        ingress_token = self._asr_current_ingress_token
        if (
            correlator is None
            or lifecycle is None
            or ingress_token is None
            or not self._ingress_token_matches(ingress_token)
            or lifecycle.provider_policy.endpoint_authority != "provider"
        ):
            return _ProviderStartedOutcome.STALE

        existing = self._asr_provider_started_turns.get(key)
        if existing is not None:
            alias = correlator.record_for(key)
            if alias is None or alias.bound_turn_token != existing:
                return _ProviderStartedOutcome.FAILED
            ledger = self._asr_provider_speaker_key_ledgers.get(key)
            if ledger is not None:
                anchored_start = ledger.anchor_start_sample_16k
                repeated_start = notification.audio_start_sample_16k
                if anchored_start is not None and repeated_start != anchored_start:
                    self._speaker_rejection_metrics[
                        "speaker_anchor_conflict_count"
                    ] += 1
                    self._poison_provider_speaker_ledger(
                        ledger,
                        "duplicate_started_conflict",
                    )
                    await self._publish_provider_ledger_unavailable(ledger)
                    return _ProviderStartedOutcome.BOUND_SPEAKER_UNAVAILABLE
                if ledger.poisoned_reason is not None:
                    return _ProviderStartedOutcome.BOUND_SPEAKER_UNAVAILABLE
            return (
                _ProviderStartedOutcome.BOUND_ACTIVE
                if lifecycle.current_turn_token == existing
                else _ProviderStartedOutcome.BOUND_PENDING
            )
        if correlator.is_completed(key):
            return _ProviderStartedOutcome.STALE

        state = lifecycle.snapshot.state
        turn_token: VoiceTurnToken | None = None
        if state in {VoiceLifecycleState.PREWARMING, VoiceLifecycleState.ACTIVE}:
            current = self._capture_turn_token(lifecycle)
            if any(
                bound_turn == current
                for bound_turn in self._asr_provider_started_turns.values()
            ):
                return _ProviderStartedOutcome.FAILED
            turn_token = current
        elif state is VoiceLifecycleState.DRAINING:
            turn_token = lifecycle.mark_pending_turn_speech(ingress_token)
            if self._asr_pending_turn_onset_at is None:
                self._asr_pending_turn_onset_at = time.monotonic()
        if turn_token is None:
            return _ProviderStartedOutcome.STALE
        identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=(turn_token if lifecycle.current_turn_token == turn_token else None),
        )
        try:
            correlator.mark_ordered(key)
            correlator.bind_ordered(key, turn_token)
        except ProviderAliasConflictError:
            correlator.retire_namespace((key.generation, key.buffer_epoch))
            return _ProviderStartedOutcome.FAILED

        ownership = self._reserve_provider_turn_ownership(
            key,
            turn_token,
            lease_token=None,
            lifecycle=lifecycle,
            correlator=correlator,
            expected_identity=identity,
        )
        if ownership is None:
            self._retire_provider_started_alias_reservation(
                correlator,
                key,
                turn_token,
            )
            return _ProviderStartedOutcome.FAILED

        speaker_unavailable = False
        try:
            if self._speaker_verifier_enforces_admission:
                detector = self._asr_detector
                unavailable_ledger = self._unavailable_provider_speaker_ledger_for_turn(turn_token)
                if (
                    self._asr_provider_speaker_evidence_lease is None
                    and unavailable_ledger is None
                    and self._speaker_verifier_activation_generation is not None
                    and state in {VoiceLifecycleState.ACTIVE, VoiceLifecycleState.DRAINING}
                ):
                    arming = await self._await_provider_speaker_parent_lease(
                        lifecycle, turn_token, self._speaker_verifier_activation_generation
                    )
                    if not arming or not self._runtime_identity_matches(identity):
                        raise RuntimeError("ASR_PROVIDER_STARTED_IDENTITY_FAILED")
                evidence_lease = self._asr_provider_speaker_evidence_lease
                ledger = (
                    self._asr_provider_speaker_ledgers.get(
                        evidence_lease.candidate
                    )
                    if evidence_lease is not None
                    else unavailable_ledger
                )
                anchor_ok = bool(
                    detector is not None
                    and ledger is not None
                    and await self._anchor_provider_speaker_ledger(
                        ledger,
                        notification,
                        identity=identity,
                        detector=detector,
                        turn_token=turn_token,
                    )
                )
                attached = False
                if ledger is not None and detector is not None and evidence_lease is not None:
                    lease_token = await self._open_provider_speaker_parent(
                        ledger,
                        turn_token=turn_token,
                        identity=identity,
                        lifecycle=lifecycle,
                        detector=detector,
                    )
                    if lease_token is not None:
                        ownership.speaker_lease_token = lease_token
                        attached = await self._attach_current_lease_to_provider_turn(
                            key,
                            turn_token,
                            lifecycle=lifecycle,
                            correlator=correlator,
                            expected_identity=identity,
                        )
                if attached and not anchor_ok:
                    speaker_unavailable = True
                    if ledger is None or not await self._publish_provider_ledger_unavailable(
                        ledger
                    ):
                        candidate = (
                            ledger.candidate if ledger is not None else None
                        )
                        if candidate is not None:
                            await self._post_admission_event(
                                turn_token,
                                SpeakerAuthorityUnavailable(candidate),
                            )
                elif not attached:
                    speaker_unavailable = True
                    generation = self._speaker_verifier_activation_generation
                    if generation is None or not await self._bind_provider_turn_speaker_unavailable(
                        ownership,
                        owner_generation=generation,
                        identity=identity,
                    ):
                        raise RuntimeError("ASR_PROVIDER_STARTED_IDENTITY_FAILED")
            else:
                self._asr_provider_started_turns[key] = turn_token
        except asyncio.CancelledError:
            if not ownership.child_published:
                self._retire_provider_started_alias_reservation(
                    correlator,
                    key,
                    turn_token,
                )
                self._release_unpublished_provider_turn_ownership(ownership)
            raise
        except Exception:
            if not ownership.child_published:
                self._retire_provider_started_alias_reservation(
                    correlator,
                    key,
                    turn_token,
                )
                self._release_unpublished_provider_turn_ownership(ownership)
            return _ProviderStartedOutcome.FAILED

        if (
            self._asr_deny_transport_state is not DenyTransportState.OPEN
            or self._asr_deny_cleanup_generation != deny_generation
        ):
            await self._settle_published_provider_turn_ownership(
                ownership,
                denied=True,
            )
            return _ProviderStartedOutcome.DENIED_SETTLED
        if (
            not self._runtime_identity_matches(identity)
            or self._asr_lifecycle is not lifecycle
            or self._asr_provider_correlator is not correlator
        ):
            return _ProviderStartedOutcome.STALE

        self._asr_provider_started_turns[key] = turn_token
        self._asr_partial_turn_token = turn_token
        if state is VoiceLifecycleState.ACTIVE:
            task = asyncio.create_task(
                self._prepare_independent_asr_turn(epoch),
                name="provider-turn-core-prepare",
            )
            self._track_admission_effect_task(task, turn_token)
            task.add_done_callback(self._admission_effect_done)
        if speaker_unavailable:
            return _ProviderStartedOutcome.BOUND_SPEAKER_UNAVAILABLE
        return (
            _ProviderStartedOutcome.BOUND_ACTIVE
            if state is VoiceLifecycleState.ACTIVE
            else _ProviderStartedOutcome.BOUND_PENDING
        )

    async def _handle_provider_utterance_started(
        self,
        notification: ProviderUtteranceStartedNotification,
        epoch: int,
    ) -> bool:
        """Compatibility facade for focused tests and legacy callback users."""

        return (
            await self._bind_provider_utterance_started(notification, epoch)
        ).accepted

    @staticmethod
    def _retire_provider_started_alias_reservation(
        correlator: ProviderTurnCorrelator,
        key: ProviderUtteranceKey,
        turn_token: VoiceTurnToken,
    ) -> None:
        try:
            retired = correlator.abandon_turn(turn_token)
            if retired.retired:
                return
        except ProviderAliasConflictError:
            pass
        correlator.retire_namespace((key.generation, key.buffer_epoch))

    async def _materialize_deferred_provider_started_turn(
        self,
        lifecycle: VoiceInputLifecycleController,
    ) -> bool:
        del lifecycle
        # A started callback cannot report success before its child attach has
        # settled. A failed callback is therefore terminal for that key; never
        # retain it for a later hidden materialization that cannot receive text.
        self._asr_deferred_provider_started_keys.clear()
        return False

    async def _retire_provider_speaker_boundary_unknown(
        self,
        detector: DetectorRuntime,
        identity: _AsrRuntimeIdentity,
        verdict: ProviderSpeakerBoundarySnapshot | None = None,
    ) -> tuple[bool, ProviderSpeakerBoundarySnapshot | None]:
        retire = getattr(
            detector,
            "retire_provider_speaker_boundary_unknown",
            None,
        )
        retired: ProviderSpeakerBoundarySnapshot | None = None
        if callable(retire):
            try:
                result = await retire(verdict)
                if type(result) is ProviderSpeakerBoundarySnapshot:
                    retired = result
            except asyncio.CancelledError:
                raise
            except Exception:
                # Speaker ownership is advisory. A cleanup failure must not
                # turn an unknown boundary into an ASR transport failure.
                pass
        return self._runtime_identity_matches(identity), retired

    async def _handle_provider_endpoint_notification(
        self,
        notification: ProviderEndpointNotification,
        epoch: int,
    ) -> None:
        """Reconcile raw boundaries early and seal keyed text turns in order."""

        if (
            type(notification) is not ProviderEndpointNotification
            or epoch != self._asr_session_epoch
        ):
            return
        self._schedule_provider_boundary_diagnostic(notification, epoch)
        if notification.phase == "boundary":
            await self._handle_provider_boundary_notification(
                notification,
                epoch,
            )
            return
        await self._handle_ordered_provider_endpoint(
            notification,
            epoch,
        )

    def _exact_interval_runtime_is_current(
        self,
        transaction: _ProviderExactIntervalTransaction,
    ) -> bool:
        return bool(
            self._runtime_identity_matches(transaction.runtime_identity)
            and self._asr_lifecycle is transaction.lifecycle
            and self._asr_detector is transaction.detector
            and self._asr_session is transaction.session
            and self._asr_provider_correlator is transaction.correlator
            and self._provider_key_timeline_is_current(transaction.provider_key)
            and self._asr_provider_started_turns.get(transaction.provider_key)
            == transaction.turn_token
            and self._asr_provider_exact_intervals.get(transaction.provider_key)
            is transaction
            and self._asr_provider_exact_candidates.get(
                transaction.target_candidate
            )
            is transaction
        )

    def _exact_interval_evidence_owner_is_current(
        self, transaction: _ProviderExactIntervalTransaction,
    ) -> bool:
        """Keep a sealed key's logical evidence without current-turn authority."""
        return bool(
            self._runtime_identity_matches(replace(transaction.runtime_identity, turn_token=None))
            and self._asr_lifecycle is transaction.lifecycle
            and self._asr_detector is transaction.detector
            and self._asr_session is transaction.session
            and self._asr_provider_correlator is transaction.correlator
            and self._provider_key_timeline_is_current(transaction.provider_key)
            and self._asr_provider_started_turns.get(transaction.provider_key)
            == transaction.turn_token
            and self._asr_provider_exact_intervals.get(transaction.provider_key) is transaction
            and self._asr_provider_exact_candidates.get(transaction.target_candidate) is transaction
        )

    def _retire_exact_interval_runtime_aliases(
        self,
        transaction: _ProviderExactIntervalTransaction,
    ) -> None:
        self._retire_provider_evidence_observation(transaction)
        drain = transaction.drain_task
        current = asyncio.current_task()
        if drain is not None and drain is not current and not drain.done():
            drain.cancel()
        while transaction.event_queue:
            queued = transaction.event_queue.popleft()
            for waiter in queued.waiters:
                if not waiter.done():
                    waiter.set_result(None)
        if (
            self._asr_provider_exact_intervals.get(transaction.provider_key)
            is transaction
        ):
            self._asr_provider_exact_intervals.pop(transaction.provider_key, None)
        if (
            self._asr_provider_exact_candidates.get(transaction.target_candidate)
            is transaction
        ):
            self._asr_provider_exact_candidates.pop(
                transaction.target_candidate,
                None,
            )
        if (
            self._asr_provider_exact_candidates.get(transaction.parent_candidate)
            is transaction
        ):
            self._asr_provider_exact_candidates.pop(
                transaction.parent_candidate,
                None,
            )
        if (
            self._asr_admission_candidate_turns.get(transaction.target_candidate)
            == transaction.turn_token
        ):
            self._asr_admission_candidate_turns.pop(
                transaction.target_candidate,
                None,
            )
        ledger = self._asr_provider_speaker_key_ledgers.get(
            transaction.provider_key
        )
        # Numeric Provider/candidate keys can repeat after stop/start while an
        # old settlement is still awaiting a callback. Fence the ledger by its
        # captured physical and admission owners, not by the current Runtime:
        # detach also calls this helper after clearing the live session refs.
        if (
            ledger is not None
            and ledger.runtime_identity.session is transaction.session
            and ledger.runtime_identity.detector is transaction.detector
            and ledger.turn_token == transaction.turn_token
            and ledger.lease_token == transaction.parent_lease_token
            and ledger.candidate == transaction.parent_candidate
        ):
            ledger.state = _ProviderSpeakerLedgerState.RESOLVED
            if self._asr_provider_speaker_ledgers.get(ledger.candidate) is ledger:
                self._asr_provider_speaker_ledgers.pop(ledger.candidate, None)
            self._asr_provider_speaker_key_ledgers.pop(
                transaction.provider_key,
                None,
            )

    async def _abort_exact_interval_setup(
        self,
        *,
        detector: DetectorRuntime,
        reservation: ProviderExactSpeakerIntervalReservation | None,
        promotion: ExactIntervalPromotionReceipt | None,
        identity: _AsrRuntimeIdentity,
        lease_token: SpeakerCaptureLeaseToken,
        promotion_future: asyncio.Future[ExactIntervalPromotionResult] | None = None,
    ) -> bool:
        _detector_aborted = True
        if reservation is not None:
            try:
                _detector_aborted = bool(
                    detector.abort_provider_exact_speaker_interval(reservation)
                )
            except Exception:
                _detector_aborted = False
        admission_aborted = promotion is None
        if promotion is None and promotion_future is not None:
            # Cancellation can escape the second shield before the caller
            # receives a promotion that the FIFO has already accepted.
            try:
                promoted = await asyncio.shield(promotion_future)
                promotion = promoted.receipt
                admission_aborted = promoted.outcome is not ExactIntervalOutcome.PROMOTED
            except Exception:
                admission_aborted = False
        if promotion is not None:
            try:
                future = (
                    self._asr_admission_ingress.abort_exact_interval_promotion_nowait(
                        promotion
                    )
                )
                result = await asyncio.shield(future)
                admission_aborted = bool(
                    result.outcome is ExactIntervalOutcome.ABORTED
                )
            except Exception:
                admission_aborted = False
        # Detector rollback governs whether PCM can ever be reused; Admission
        # rollback governs whether Provider text can safely continue.  A
        # failed Detector abort therefore poisons speaker proof, but must not
        # escalate into transport-wide transcript deletion once the exact
        # hold has been removed.
        text_safe = admission_aborted
        if not text_safe and promotion is not None and self._runtime_identity_matches(
            identity
        ):
            try:
                unavailable = (
                    await self._asr_admission_ingress.fail_exact_interval_unavailable(
                        promotion
                    )
                )
                text_safe = unavailable.outcome is ExactIntervalOutcome.ABORTED
            except Exception:
                text_safe = False
        if not text_safe and self._runtime_identity_matches(identity):
            # A formal DENY may already exist and the unavailable CAS is
            # required to reject that rewrite. Only then retain the existing
            # sticky-deny cleanup path.
            self._start_exact_parent_cleanup(
                lease_token,
                "ASR_EXACT_INTERVAL_ROLLBACK_FAILED",
                turn_token=identity.turn_token,
            )
        # The Detector abort result is intentionally consumed only as a proof
        # reuse fence. The caller marks this utterance unavailable either way.
        return text_safe

    async def _record_provider_boundary_result(
        self,
        *,
        correlator: ProviderTurnCorrelator,
        key: ProviderUtteranceKey,
        result: ProviderBoundaryResult,
        detector: DetectorRuntime,
    ) -> ProviderBoundaryResult:
        existing_boundary = correlator.record_for(key)
        recorded = correlator.record_boundary_result(key, result)
        if (
            result.quality == "exact"
            and recorded.quality == "unknown"
            and existing_boundary is None
        ):
            self._speaker_rejection_metrics[
                "admission_boundary_proof_overflow_count"
            ] += 1
        await self._retire_admission_boundary_proofs(
            recorded.retired_proofs,
            detector,
        )
        self._speaker_rejection_metrics[
            "provider_boundary_exact_ready_count"
            if recorded.quality == "exact"
            else "provider_boundary_unknown_ready_count"
        ] += 1
        return recorded

    async def _replay_exact_pending_callbacks(
        self,
        pending: _ProviderExactIntervalPending,
        transaction: _ProviderExactIntervalTransaction | None = None,
    ) -> None:
        session = self._asr_session
        epoch = self._asr_session_epoch
        correlator = self._asr_provider_correlator

        async def drain() -> None:
            while pending.deferred:
                if (
                    self._asr_session is not session
                    or self._asr_session_epoch != epoch
                    or self._asr_provider_correlator is not correlator
                ):
                    return
                kind, args, kwargs = pending.deferred.popleft()
                if kind == "ordered":
                    await self._handle_ordered_provider_endpoint(*args, **kwargs)
                else:
                    await self._handle_provider_final(*args, **kwargs)

        replay = asyncio.create_task(
            drain(),
            name="provider-exact-interval-replay",
        )
        self._track_exact_callback_task(replay)
        try:
            await self._wait_exact_callback_task(replay)
        except asyncio.CancelledError:
            if (
                transaction is not None
                and (not replay.done() or replay.cancelled())
                and self._exact_interval_runtime_is_current(transaction)
            ):
                # An interrupted accepted FIFO cannot be blindly replayed.
                # Publish the existing failure fence without another await.
                self._start_exact_parent_cleanup(
                    transaction.parent_lease_token,
                    "ASR_EXACT_INTERVAL_REPLAY_FAILED",
                    turn_token=transaction.turn_token,
                )
            raise
        except Exception:
            if transaction is not None:
                await self._fail_exact_interval_group(
                    transaction,
                    "ASR_EXACT_INTERVAL_REPLAY_FAILED",
                )
            raise

    async def _handle_provider_boundary_notification(
        self,
        notification: ProviderEndpointNotification,
        epoch: int,
    ) -> None:
        key = notification.key
        if not self._accept_provider_timeline(key):
            return
        correlator = self._asr_provider_correlator
        detector = self._asr_detector
        if correlator is None or detector is None:
            return
        existing_exact = self._asr_provider_exact_intervals.get(key)
        if existing_exact is not None:
            if (
                notification.boundary_quality == "exact"
                and notification.audio_range == existing_exact.reservation.boundary
            ):
                return
            await self._fail_exact_interval_unavailable(
                existing_exact,
                "ASR_EXACT_INTERVAL_BOUNDARY_CONFLICT",
            )
            return
        existing_pending = self._asr_provider_exact_pending.get(key)
        if existing_pending is not None:
            if (
                notification.boundary_quality == "exact"
                and notification.audio_range == existing_pending.boundary
            ):
                await asyncio.shield(existing_pending.completion.wait())
                return
            existing_pending.conflicted = True
            await asyncio.shield(existing_pending.completion.wait())
            committed = self._asr_provider_exact_intervals.get(key)
            if committed is not None:
                await self._fail_exact_interval_unavailable(
                    committed,
                    "ASR_EXACT_INTERVAL_BOUNDARY_CONFLICT",
                )
            else:
                ledger = self._asr_provider_speaker_key_ledgers.get(key)
                if ledger is not None:
                    self._poison_provider_speaker_ledger(
                        ledger,
                        "exact_boundary_conflict",
                    )
                    await self._publish_provider_ledger_unavailable(ledger)
            return
        turn_token = self._asr_provider_started_turns.get(key)
        lease_token = (
            self._asr_admission_turn_leases.get(turn_token)
            if turn_token is not None
            else None
        )
        evidence_lease = self._asr_provider_speaker_evidence_lease
        ledger = self._asr_provider_speaker_key_ledgers.get(key)
        lifecycle = self._asr_lifecycle
        ingress_token = self._asr_current_ingress_token
        # Keep the original guard order and short-circuit semantics. Capture
        # the first failure before poisoning/awaits erase its original state.
        failed_check = None
        if turn_token is None:
            failed_check = "missing_started_turn"
        elif lease_token is None:
            failed_check = "missing_admission_lease"
        elif evidence_lease is None:
            failed_check = "missing_evidence_lease"
        elif ledger is None:
            failed_check = "missing_ledger"
        elif ledger.evidence_lease != evidence_lease:
            failed_check = "evidence_lease_mismatch"
        elif ledger.turn_token != turn_token:
            failed_check = "turn_mismatch"
        elif ledger.provider_key != key:
            failed_check = "provider_key_mismatch"
        elif ledger.state is not _ProviderSpeakerLedgerState.ANCHORED_SCORING:
            failed_check = "ledger_not_anchored_scoring"
        elif ledger.poisoned_reason is not None:
            failed_check = "ledger_poisoned"
        elif lifecycle is None:
            failed_check = "missing_lifecycle"
        elif ingress_token is None:
            failed_check = "missing_ingress"
        elif turn_token.ingress != ingress_token:
            failed_check = "ingress_mismatch"
        elif notification.boundary_quality != "exact":
            failed_check = "boundary_not_exact"
        elif notification.audio_range is None:
            failed_check = "missing_audio_range"
        elif ledger.anchor_start_sample_16k != notification.audio_range.start_sample_16k:
            failed_check = "anchor_start_mismatch"
        elif self._asr_session is not self._asr_provider_exact_session:
            failed_check = "exact_session_mismatch"
        elif (
            len(self._asr_provider_exact_intervals) + len(self._asr_provider_exact_pending)
        ) >= _MAX_PROVIDER_BOUNDARY_SNAPSHOTS:
            failed_check = "boundary_capacity_exhausted"
        if failed_check is not None:
            self._schedule_provider_guard_diagnostic(
                key, epoch, stage="provider_boundary_guard_failed", check=failed_check,
                notification=notification,
            )
            if ledger is not None and ledger.state not in {
                _ProviderSpeakerLedgerState.EXACT_DRAINING,
                _ProviderSpeakerLedgerState.RESOLVED,
            }:
                self._poison_provider_speaker_ledger(
                    ledger,
                    "exact_boundary_unavailable",
                )
                await self._publish_provider_ledger_unavailable(ledger)
            await self._record_provider_boundary_result(
                correlator=correlator,
                key=key,
                result=ProviderBoundaryResult.unknown(),
                detector=detector,
            )
            return
        pending = _ProviderExactIntervalPending(notification.audio_range)
        self._asr_provider_exact_pending[key] = pending
        ledger.state = _ProviderSpeakerLedgerState.EXACT_PREPARING
        identity = self._capture_runtime_identity(
            ingress_token=ingress_token,
            turn_token=turn_token,
        )
        deadline = time.monotonic() + _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS
        reservation: ProviderExactSpeakerIntervalReservation | None = None
        promotion: ExactIntervalPromotionReceipt | None = None
        promotion_future: asyncio.Future[ExactIntervalPromotionResult] | None = None
        cancelled: asyncio.CancelledError | None = None
        exact_result: ProviderBoundaryResult | None = None
        rollback_safe = True
        final_lock_acquired = False
        try:
            await self._asr_final_lock.acquire()
            final_lock_acquired = True
            if (
                not self._runtime_identity_matches(identity)
                or self._asr_provider_exact_pending.get(key) is not pending
                or pending.conflicted
                or self._asr_provider_started_turns.get(key) != turn_token
            ):
                raise RuntimeError("ASR_EXACT_INTERVAL_STALE")
            if (
                notification.boundary_quality == "exact"
                and notification.audio_range is not None
                and self._asr_session is self._asr_provider_exact_session
            ):
                remaining = deadline - time.monotonic()
                observed = bool(
                    remaining > 0
                    and await asyncio.wait_for(
                        detector.wait_provider_audio_observed_through(
                            notification.audio_range.end_sample_16k
                        ),
                        timeout=remaining,
                    )
                )
                if (
                    not observed
                    or not self._runtime_identity_matches(identity)
                    or pending.conflicted
                ):
                    raise RuntimeError("ASR_EXACT_INTERVAL_STALE")
                child_record, parent_record = await asyncio.gather(
                    self._asr_admission.get_record(turn_token),
                    self._asr_admission.get_speaker_lease(lease_token),
                )
                if not self._runtime_identity_matches(identity) or pending.conflicted:
                    raise RuntimeError("ASR_EXACT_INTERVAL_STALE")
                parent_candidate = evidence_lease.candidate
                sole_child = bool(
                    child_record is not None
                    and parent_record is not None
                    and child_record.provider_key == key
                    and child_record.speaker_lease_token == lease_token
                    and child_record.speaker_candidate == parent_candidate
                    and parent_record.lease_token == lease_token
                    and parent_record.candidate == parent_candidate
                    and parent_record.state is SpeakerLeaseState.COLLECTING
                    and parent_record.last_speaker_sequence_no == 0
                    and len(parent_record.child_bindings) == 1
                    and parent_record.child_bindings[0].provider_key == key
                    and parent_record.child_bindings[0].turn_token == turn_token
                    and parent_record.terminal_disposition is None
                    and self._asr_current_speaker_lease == lease_token
                    and self._asr_current_speaker_candidate == parent_candidate
                    and self._asr_admission_candidate_leases.get(parent_candidate)
                    == lease_token
                    and self._asr_provider_speaker_evidence_lease is evidence_lease
                )
                if not sole_child:
                    raise RuntimeError("ASR_EXACT_INTERVAL_PARENT_CONFLICT")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("ASR_EXACT_INTERVAL_TIMEOUT")
                reservation = await asyncio.wait_for(
                    detector.prepare_provider_exact_speaker_interval(
                        notification.audio_range,
                        speaker_evidence_lease=evidence_lease,
                    ),
                    timeout=remaining,
                )
                if (
                    reservation is None
                    or not self._runtime_identity_matches(identity)
                    or pending.conflicted
                    or self._asr_current_speaker_lease != lease_token
                    or self._asr_provider_speaker_evidence_lease is not evidence_lease
                    or getattr(reservation, "anchor_revision", None)
                    != ledger.anchor_revision
                    or getattr(reservation, "anchor_start_sample_16k", None)
                    != ledger.anchor_start_sample_16k
                    or type(reservation.provider_pcm_through_sequence_no) is not int
                    or not (
                        0 < reservation.provider_pcm_through_sequence_no
                        <= ledger.last_pcm_sequence_no
                    )
                    # The ledger advances only from consecutive actual
                    # Detector receipts. Prepare may span a legal append;
                    # anchors, timeline and the physical owner may not move.
                    or self._asr_provider_speaker_ledgers.get(parent_candidate) is not ledger
                    or ledger.evidence_lease is not evidence_lease
                    or ledger.state is not _ProviderSpeakerLedgerState.EXACT_PREPARING
                    or ledger.poisoned_reason is not None
                    or reservation.timeline_generation != ledger.timeline_generation
                ):
                    raise RuntimeError("ASR_EXACT_INTERVAL_PREPARE_FAILED")
                self._speaker_rejection_metrics["speaker_exact_prepare_count"] += 1
                child_record, parent_record = await asyncio.gather(
                    self._asr_admission.get_record(turn_token),
                    self._asr_admission.get_speaker_lease(lease_token),
                )
                if (
                    not self._runtime_identity_matches(identity)
                    or pending.conflicted
                    or child_record is None
                    or parent_record is None
                    or child_record.speaker_lease_token != lease_token
                    or child_record.speaker_candidate != parent_candidate
                    or parent_record.candidate != parent_candidate
                    or len(parent_record.child_bindings) != 1
                ):
                    raise RuntimeError("ASR_EXACT_INTERVAL_PREPARE_STALE")
                self._asr_provider_boundary_proof_sequence += 1
                proof = BoundaryProof(
                    proof_id=self._asr_provider_boundary_proof_sequence,
                    owner_generation=self._asr_admission_capability_generation,
                    provider_key=key,
                )
                scope = ExactIntervalPromotionScope(
                    parent_lease_token=lease_token,
                    parent_record_generation=parent_record.record_generation,
                    expected_parent_logical_revision=parent_record.logical_revision,
                    expected_parent_state=parent_record.state,
                    turn_token=turn_token,
                    child_record_generation=child_record.record_generation,
                    expected_child_logical_revision=child_record.logical_revision,
                    provider_key=key,
                    boundary_proof=proof,
                    target_candidate=reservation.target_candidate,
                    successor_candidate=reservation.suffix_candidate,
                )
                promotion_future = (
                    self._asr_admission_ingress.promote_exact_interval_nowait(scope)
                )
                try:
                    promoted = await asyncio.shield(promotion_future)
                except asyncio.CancelledError as exc:
                    cancelled = exc
                    promoted = await asyncio.shield(promotion_future)
                if promoted.outcome is not ExactIntervalOutcome.PROMOTED:
                    raise RuntimeError("ASR_EXACT_INTERVAL_PROMOTION_FAILED")
                promotion = promoted.receipt
                assert promotion is not None
                if (
                    cancelled is not None
                    or not self._runtime_identity_matches(identity)
                    or pending.conflicted
                ):
                    raise RuntimeError("ASR_EXACT_INTERVAL_PROMOTION_STALE")
                activation_future = (
                    self._asr_admission_ingress.activate_exact_interval_nowait(
                        promotion
                    )
                )
                try:
                    activated = await asyncio.shield(activation_future)
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                    activated = await asyncio.shield(activation_future)
                if activated.outcome is not ExactIntervalOutcome.ACTIVATED:
                    raise RuntimeError("ASR_EXACT_INTERVAL_ACTIVATION_FAILED")
                activation = activated.receipt
                assert activation is not None
                if (
                    cancelled is not None
                    or not self._runtime_identity_matches(identity)
                    or pending.conflicted
                ):
                    raise RuntimeError("ASR_EXACT_INTERVAL_ACTIVATION_STALE")
                committed = detector.commit_provider_exact_speaker_interval(reservation)
                if committed is None:
                    # Detector consumes/aborts the reservation on a failed
                    # commit; only the Admission promotion remains to unwind.
                    reservation = None
                    raise RuntimeError("ASR_EXACT_INTERVAL_COMMIT_FAILED")
                self._speaker_rejection_metrics["speaker_exact_commit_count"] += 1

                transaction = _ProviderExactIntervalTransaction(
                    provider_key=key,
                    turn_token=turn_token,
                    parent_lease_token=lease_token,
                    parent_candidate=parent_candidate,
                    target_candidate=committed.target_candidate,
                    successor_candidate=reservation.suffix_candidate,
                    successor_evidence_lease=committed.successor_evidence_lease,
                    detector=detector,
                    reservation=reservation,
                    promotion=promotion,
                    activation=activation,
                    proof=proof,
                    snapshot=committed.snapshot,
                    lifecycle=lifecycle,
                    correlator=correlator,
                    session=identity.session,
                    ingress_token=ingress_token,
                    runtime_identity=identity,
                )
                # Detector commit is irreversible. Publish every Runtime alias
                # before yielding so no completion callback can observe a
                # half-split parent/child ownership graph.
                self._asr_provider_exact_intervals[key] = transaction
                self._asr_provider_exact_candidates[
                    transaction.target_candidate
                ] = transaction
                if committed.score_reusable:
                    self._asr_provider_exact_candidates[parent_candidate] = transaction
                self._asr_admission_candidate_turns[
                    transaction.target_candidate
                ] = turn_token
                self._asr_admission_turn_leases.pop(turn_token, None)
                self._asr_admission_candidate_leases.pop(parent_candidate, None)
                if transaction.successor_candidate is not None:
                    self._asr_admission_candidate_leases[
                        transaction.successor_candidate
                    ] = lease_token
                    self._asr_current_speaker_candidate = (
                        transaction.successor_candidate
                    )
                self._asr_provider_speaker_evidence_lease = (
                    transaction.successor_evidence_lease
                )
                if transaction.successor_evidence_lease is not None:
                    successor_lease = transaction.successor_evidence_lease
                    successor_ledger = _ProviderSpeakerProvisionalLedger(
                        evidence_lease=successor_lease,
                        runtime_identity=self._capture_runtime_identity(
                            ingress_token=ingress_token,
                        ),
                        activation_generation=ledger.activation_generation,
                        detector_epoch=successor_lease.detector_epoch,
                        lease_generation=successor_lease.lease_generation,
                        lease_token=lease_token,
                        last_pcm_sequence_no=(
                            committed.provider_pcm_through_sequence_no
                            if committed.provider_pcm_through_sequence_no is not None
                            else reservation.provider_pcm_through_sequence_no
                        ),
                    )
                    self._asr_provider_speaker_ledgers[
                        successor_lease.candidate
                    ] = successor_ledger
                    self._speaker_rejection_metrics[
                        "speaker_anchor_deferred_count"
                    ] += 1
                self._asr_provider_boundary_proofs[proof.proof_id] = (
                    transaction.snapshot
                )
                self._asr_provider_boundary_completions[proof] = (
                    _ProviderBoundaryCompletion(
                        transaction.snapshot,
                        transaction.successor_evidence_lease,
                        detector,
                    )
                )
                ledger.state = _ProviderSpeakerLedgerState.EXACT_DRAINING
                if ledger.poisoned_reason is not None:
                    self._enqueue_exact_interval_event(
                        transaction,
                        SpeakerLeaseUnavailable(transaction.target_candidate, 1),
                    )
                elif committed.score_reusable:
                    for provisional in ledger.events:
                        promoted_event: SpeakerLeaseEvent = (
                            SpeakerLeaseLow(
                                transaction.target_candidate,
                                provisional.sequence_no,
                                provisional.checkpoint_kind,
                            )
                            if isinstance(provisional, SpeakerLeaseLow)
                            else SpeakerLeaseHigh(
                                transaction.target_candidate,
                                provisional.sequence_no,
                            )
                        )
                        self._enqueue_exact_interval_event(
                            transaction,
                            promoted_event,
                        )
                    if ledger.close_event is not None:
                        self._enqueue_exact_interval_event(
                            transaction,
                            SpeakerLeaseCaptureClosed(
                                transaction.target_candidate,
                                ledger.close_event.through_sequence_no,
                            ),
                        )
                exact_result = ProviderBoundaryResult(
                    quality="exact",
                    audio_range=notification.audio_range,
                    proof=proof,
                )
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            pass
        finally:
            try:
                if exact_result is None:
                    async with asyncio.timeout(_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS):
                        rollback_safe = await self._abort_exact_interval_setup(
                            detector=detector,
                            reservation=reservation,
                            promotion=promotion,
                            identity=identity,
                            lease_token=lease_token,
                            promotion_future=promotion_future,
                        )
                        self._speaker_rejection_metrics["speaker_exact_abort_count"] += 1
                        if ledger.state is _ProviderSpeakerLedgerState.EXACT_PREPARING:
                            ledger.state = _ProviderSpeakerLedgerState.UNAVAILABLE
                            if ledger.poisoned_reason is None:
                                self._poison_provider_speaker_ledger(
                                    ledger, "exact_prepare_unavailable",
                                )
                            await self._publish_provider_ledger_unavailable(ledger)
            except (asyncio.CancelledError, TimeoutError):
                if self._runtime_identity_matches(identity):
                    self._start_exact_parent_cleanup(
                        lease_token,
                        "ASR_EXACT_INTERVAL_ROLLBACK_FAILED",
                        turn_token=turn_token,
                    )
                raise
            finally:
                # These local ownership releases must survive cancellation of
                # the rollback itself. Completion is a wakeup, not a commit.
                if final_lock_acquired:
                    self._asr_final_lock.release()
                if self._asr_provider_exact_pending.get(key) is pending:
                    self._asr_provider_exact_pending.pop(key, None)
                pending.completion.set()
        if cancelled is not None:
            # Cancellation before Detector commit must not strand callbacks
            # that arrived behind the pending fence. Once both staged sides
            # are proven rolled back, publish the ordinary unknown boundary
            # and replay them in arrival order before propagating cancellation.
            if (
                exact_result is None
                and rollback_safe
                and not pending.conflicted
                and self._runtime_identity_matches(identity)
            ):
                await self._record_provider_boundary_result(
                    correlator=correlator,
                    key=key,
                    result=ProviderBoundaryResult.unknown(),
                    detector=detector,
                )
                await self._replay_exact_pending_callbacks(pending)
            raise cancelled
        if exact_result is None:
            if not self._runtime_identity_matches(identity):
                return
            exact_result = ProviderBoundaryResult.unknown()
        recorded_result = await self._record_provider_boundary_result(
            correlator=correlator,
            key=key,
            result=exact_result,
            detector=detector,
        )
        transaction = self._asr_provider_exact_intervals.get(key)
        if transaction is None or exact_result.quality != "exact":
            await self._replay_exact_pending_callbacks(pending)
            return
        if recorded_result.quality != "exact":
            await self._fail_exact_interval_unavailable(
                transaction,
                "ASR_EXACT_INTERVAL_BOUNDARY_CONFLICT",
            )
            return
        preseal_ready = False
        post_commit_cancelled: asyncio.CancelledError | None = None
        try:
            remaining = deadline - time.monotonic()
            preseal_ready = bool(
                remaining > 0
                and await asyncio.wait_for(
                    detector.wait_provider_speaker_preseal(
                        transaction.snapshot,
                        deadline=deadline,
                    ),
                    timeout=remaining,
                )
            )
        except asyncio.CancelledError as exc:
            post_commit_cancelled = exc
            preseal_ready = False
        except Exception:
            preseal_ready = False
        if not self._exact_interval_runtime_is_current(transaction):
            if post_commit_cancelled is not None:
                raise post_commit_cancelled
            return
        if not preseal_ready and transaction.resolved_disposition is None:
            # Provider PCM sequencing and speaker-fact sequencing are
            # independent domains.  Derive the fail-open fact from the exact
            # transaction's accepted speaker events; reusing the PCM counter
            # can collide with an already-drained LOW or manufacture a gap.
            accepted_speaker_sequences = tuple(
                sequence_key[1]
                for event in transaction.accepted_events
                if (
                    sequence_key := self._exact_interval_event_sequence(event)
                )
                is not None
                and sequence_key[0] == "speaker"
            )
            unavailable_sequence = max(accepted_speaker_sequences, default=0) + 1
            await self._post_exact_interval_event(
                transaction,
                SpeakerLeaseUnavailable(
                    transaction.target_candidate,
                    unavailable_sequence,
                ),
            )
        await self._replay_exact_pending_callbacks(pending, transaction)
        if post_commit_cancelled is not None:
            raise post_commit_cancelled

    async def _handle_ordered_provider_endpoint(
        self,
        notification: ProviderEndpointNotification,
        epoch: int,
        *,
        deadline: float | None = None,
    ) -> None:
        if deadline is None:
            deadline = time.monotonic() + _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS
        key = notification.key
        if not self._accept_provider_timeline(key):
            return
        correlator = self._asr_provider_correlator
        if correlator is None or correlator.is_completed(key):
            return
        pending = self._asr_provider_exact_pending.get(key)
        if pending is not None:
            if len(pending.deferred) >= _MAX_PROVIDER_BOUNDARY_SNAPSHOTS:
                pending.conflicted = True
            else:
                pending.deferred.append(
                    ("ordered", (notification, epoch), {"deadline": deadline})
                )
            return
        try:
            alias = correlator.mark_ordered(key)
        except ProviderAliasConflictError:
            return
        boundary = alias.boundary_result or ProviderBoundaryResult.unknown()
        if alias.boundary_result is None:
            self._speaker_rejection_metrics[
                "provider_boundary_ordered_jit_unknown_count"
            ] += 1
        snapshot = None
        proof = boundary.proof
        if boundary.quality != "exact":
            ledger = self._asr_provider_speaker_key_ledgers.get(key)
            if ledger is not None and ledger.state not in {
                _ProviderSpeakerLedgerState.EXACT_DRAINING,
                _ProviderSpeakerLedgerState.RESOLVED,
            }:
                self._poison_provider_speaker_ledger(
                    ledger,
                    "provider_boundary_unknown",
                )
                await self._publish_provider_ledger_unavailable(ledger)
        if (
            boundary.quality == "exact"
            and proof is not None
            and boundary.audio_range == notification.audio_range
        ):
            snapshot = self._asr_provider_boundary_proofs.get(proof.proof_id)
        exact = self._asr_provider_exact_intervals.get(key)
        if exact is not None:
            if (
                snapshot is not exact.snapshot
                or proof != exact.proof
                or boundary.audio_range != notification.audio_range
            ):
                failed_open = await self._fail_exact_interval_unavailable(
                    exact,
                    "ASR_EXACT_INTERVAL_ORDER_CONFLICT",
                )
                if failed_open:
                    # The Provider order marker is still an authoritative text
                    # endpoint even though its exact PCM proof conflicted.
                    # Seal the ordinary unavailable child so a later final can
                    # take the required fail-open path.
                    await self._handle_independent_asr_endpoint(
                        epoch,
                        provider_key=key,
                        provider_snapshot=None,
                        deadline=deadline,
                    )
                return
            await self._handle_exact_ordered_provider_endpoint(exact, epoch)
            return
        await self._handle_independent_asr_endpoint(
            epoch,
            provider_key=key,
            provider_snapshot=snapshot,
            deadline=deadline,
        )

    async def _handle_exact_ordered_provider_endpoint(
        self,
        transaction: _ProviderExactIntervalTransaction,
        epoch: int,
    ) -> None:
        lifecycle = transaction.lifecycle
        if (
            epoch != transaction.runtime_identity.session_epoch
            or not self._exact_interval_runtime_is_current(transaction)
            or lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
            or lifecycle.current_turn_token != transaction.turn_token
        ):
            return
        if not self._asr_turn_prepared:
            await self._prepare_independent_asr_turn(epoch)
            if (
                not self._exact_interval_runtime_is_current(transaction)
                or not self._asr_turn_prepared
                or lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
            ):
                return
        async with self._asr_final_lock:
            if (
                not self._exact_interval_runtime_is_current(transaction)
                or lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
                or lifecycle.current_turn_token != transaction.turn_token
                or self._asr_sealed_turn_token is not None
            ):
                return
            lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
            sealed_token = self._capture_transport_token(lifecycle)
            transaction.sealed_token = sealed_token
            self._asr_sealed_turn_token = sealed_token
            self._asr_sealed_provider_key = transaction.provider_key
            self._asr_provider_candidate_fence = None
            self._asr_turn_endpointed_at = time.monotonic()
            self._asr_last_turn_endpointed_at = self._asr_turn_endpointed_at
            self._asr_last_turn_endpointed_key = (
                f"asr-{transaction.turn_token.ingress.session_epoch}-"
                f"{transaction.turn_token.turn_id}"
            )
            self._schedule_provider_final_watchdog(
                epoch,
                lifecycle,
                sealed_token,
            )
        await self._send_exact_lifecycle_state(
            VoiceLifecycleState.DRAINING,
            provider=self._asr_provider or "unknown",
            session_epoch=epoch,
            expected_identity=transaction.runtime_identity,
        )

    async def _handle_provider_final(
        self,
        key: ProviderUtteranceKey,
        text: str,
        epoch: int,
        provider: str,
        *,
        received_at: float | None = None,
        admission_deadline: float | None = None,
    ) -> None:
        # Compatibility for existing private test/integration callers. The
        # production session callback always supplies the first-receipt pair;
        # therefore an out-of-order final never regains this budget here.
        if received_at is None and admission_deadline is None:
            received_at = time.monotonic()
            admission_deadline = (
                received_at + _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS
            )
        if received_at is None or admission_deadline is None:
            return
        if admission_deadline < received_at:
            return
        final_deadline = admission_deadline
        if (
            type(key) is not ProviderUtteranceKey
            or epoch != self._asr_session_epoch
            or not self._accept_provider_timeline(key)
        ):
            return
        correlator = self._asr_provider_correlator
        if correlator is None or correlator.is_completed(key):
            self._schedule_provider_guard_diagnostic(
                key, epoch, stage="provider_final_ignored",
                check="missing_correlator" if correlator is None else "already_completed",
            )
            return
        self._schedule_provider_guard_diagnostic(
            key, epoch, stage="provider_final_received", check="accepted_for_processing",
        )
        pending_exact = self._asr_provider_exact_pending.get(key)
        if pending_exact is not None:
            if len(pending_exact.deferred) >= _MAX_PROVIDER_BOUNDARY_SNAPSHOTS:
                pending_exact.conflicted = True
            else:
                pending_exact.deferred.append(
                    (
                        "final",
                        (
                            key,
                            text,
                            epoch,
                            provider,
                        ),
                        {
                            "received_at": received_at,
                            "admission_deadline": admission_deadline,
                        },
                    )
                )
            return
        old_exact = self._asr_provider_exact_intervals.get(key)
        if old_exact is not None and old_exact.sealed_token is not None:
            await self._handle_exact_provider_final(
                old_exact, text, epoch, provider,
                received_at=received_at, deadline=final_deadline,
            )
            return
        if self._asr_sealed_provider_key != key:
            exact_before_order = self._asr_provider_exact_intervals.get(key)
            if exact_before_order is not None:
                await self._handle_exact_ordered_provider_endpoint(
                    exact_before_order,
                    epoch,
                )
            else:
                await self._handle_ordered_provider_endpoint(
                    ProviderEndpointNotification(
                        phase="ordered",
                        generation=key.generation,
                        buffer_epoch=key.buffer_epoch,
                        utterance_id=key.utterance_id,
                        boundary_quality="unknown",
                        audio_range=None,
                    ),
                    epoch,
                    deadline=final_deadline,
                )
        if epoch != self._asr_session_epoch or self._asr_sealed_provider_key != key:
            self._schedule_provider_guard_diagnostic(
                key, epoch, stage="provider_final_ignored",
                check="session_changed" if epoch != self._asr_session_epoch else "sealed_key_mismatch",
            )
            return
        exact = self._asr_provider_exact_intervals.get(key)
        if exact is not None:
            await self._handle_exact_provider_final(
                exact,
                text,
                epoch,
                provider,
                received_at=received_at,
                deadline=final_deadline,
            )
            return
        current_turn = self._asr_provider_started_turns.get(key)
        current_lease = (
            self._asr_admission_turn_leases.get(current_turn)
            if current_turn is not None
            else None
        )
        has_started_successor = bool(
            current_lease is not None
            and any(
                other_key.utterance_id > key.utterance_id
                and self._asr_admission_turn_leases.get(other_turn) == current_lease
                for other_key, other_turn in self._asr_provider_started_turns.items()
            )
        )
        final_ledger = self._asr_provider_speaker_key_ledgers.get(key)
        evidence_lease = (
            final_ledger.evidence_lease
            if final_ledger is not None
            else None
        )
        detector = self._asr_detector
        final_identity = self._capture_runtime_identity()
        activation_generation = self._speaker_verifier_activation_generation
        if (
            not has_started_successor
            and evidence_lease is not None
            and detector is not None
        ):
            finish_evidence = getattr(
                detector,
                "finish_provider_speaker_evidence_lease",
                None,
            )
            if callable(finish_evidence):
                try:
                    await asyncio.wait_for(
                        finish_evidence(evidence_lease),
                        timeout=max(0.0, final_deadline - time.monotonic()),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                if (
                    not self._runtime_identity_matches(final_identity)
                    or activation_generation != self._speaker_verifier_activation_generation
                    or self._asr_sealed_provider_key != key
                ):
                    return
                await self._confirm_provider_speaker_evidence_retirement(
                    evidence_lease,
                    detector=detector,
                    identity=final_identity,
                    owner_generation=activation_generation,
                    turn_token=current_turn,
                    deadline=final_deadline,
                )
                if (
                    not self._runtime_identity_matches(final_identity)
                    or activation_generation != self._speaker_verifier_activation_generation
                    or self._asr_sealed_provider_key != key
                ):
                    return
        await self._handle_independent_asr_final(
            text,
            epoch,
            provider,
            provider_key=key,
            received_at=received_at,
            deadline=final_deadline,
        )

    async def _handle_exact_provider_final(
        self,
        transaction: _ProviderExactIntervalTransaction,
        text: str,
        epoch: int,
        provider: str,
        *,
        received_at: float,
        deadline: float,
    ) -> asyncio.Event | None:
        async with self._asr_final_lock:
            lifecycle = transaction.lifecycle
            sealed_token = transaction.sealed_token or self._asr_sealed_turn_token
            existing_context = self._asr_admission_final_contexts.get(transaction.turn_token)
            if (
                existing_context is not None
                and self._exact_interval_evidence_owner_is_current(transaction)
                and epoch == transaction.runtime_identity.session_epoch
            ):
                return existing_context.settled
            if (
                not self._exact_interval_runtime_is_current(transaction)
                or epoch != transaction.runtime_identity.session_epoch
                or sealed_token is None
                or sealed_token.turn != transaction.turn_token
                or self._asr_sealed_provider_key != transaction.provider_key
                or lifecycle.snapshot.state is not VoiceLifecycleState.DRAINING
                or not self._transport_token_matches(sealed_token, lifecycle)
            ):
                return None
            admission_record = await self._asr_admission.get_record(
                transaction.turn_token
            )
            if (
                not self._exact_interval_runtime_is_current(transaction)
                or self._asr_sealed_turn_token != sealed_token
            ):
                return None
            if (
                admission_record is None
                or admission_record.exact_interval_hold_id
                != transaction.activation.interval_id
                or admission_record.provider_key != transaction.provider_key
                or admission_record.terminal_disposition is not None
            ):
                await self._fail_exact_interval_group(
                    transaction,
                    "ASR_EXACT_INTERVAL_FINAL_CONFLICT",
                )
                return None
            existing_context = self._asr_admission_final_contexts.get(
                transaction.turn_token
            )
            if existing_context is not None:
                return existing_context.settled
            pending = PendingProviderFinal(
                provider_key=transaction.provider_key,
                provider=provider,
                text=str(text or "").strip(),
                received_at=received_at,
                admission_deadline=deadline,
            )
            try:
                transaction.correlator.record_final(
                    transaction.provider_key,
                    pending,
                )
            except ProviderAliasConflictError:
                await self._fail_exact_interval_group(
                    transaction,
                    "ASR_EXACT_INTERVAL_FINAL_CONFLICT",
                )
                return None
            context = _AdmissionFinalContext(
                turn_token=transaction.turn_token,
                final_key=FinalKey.from_turn(transaction.turn_token),
                epoch=epoch,
                provider=provider,
                provider_key=transaction.provider_key,
                lifecycle=lifecycle,
                detector=transaction.detector,
                correlator=transaction.correlator,
                sealed_token=sealed_token,
                provider_fence=None,
                runtime_identity=transaction.runtime_identity,
                has_pending_turn=(
                    lifecycle.has_pending_turn
                    or (
                        lifecycle.has_pending_turn_identity
                        and lifecycle.pending_turn_token
                        in self._asr_provider_started_turns.values()
                    )
                ),
            )
            self._asr_admission_final_contexts[transaction.turn_token] = context
            if self._asr_evidence_hold_enabled or self._asr_evidence_observation_enabled:
                boundary = transaction.reservation.boundary
                if boundary.end_sample_16k - boundary.start_sample_16k <= 64000:
                    audio_range = AudioRangeReference(
                        session_generation=epoch,
                        transport_generation=sealed_token.transport_generation,
                        timeline_generation=transaction.reservation.timeline_generation,
                        start_sample_16k=boundary.start_sample_16k,
                        end_sample_16k=boundary.end_sample_16k,
                        first_sequence_no=None,
                        last_sequence_no=transaction.reservation.provider_pcm_through_sequence_no,
                    )
                    binding = ProviderEvidenceBinding(
                        provider_key=transaction.provider_key,
                        turn_token=transaction.turn_token,
                        record_generation=admission_record.record_generation,
                        window=EvidenceWindow(transaction.proof.proof_id, 1, audio_range),
                        target_range=audio_range,
                        policy_version="exact-evidence-v1",
                    )
                    self._observe_provider_evidence(transaction, binding, admission_record)
                    if self._asr_evidence_hold_enabled:
                        await self._post_exact_interval_event(
                            transaction, EvidenceHoldRequested(binding, received_at),
                        )
                        if not self._exact_interval_runtime_is_current(transaction):
                            return context.settled
                        record = await self._asr_admission.get_record(transaction.turn_token)
                        if not self._exact_interval_runtime_is_current(transaction):
                            return context.settled
                        if record is not None and record.evidence_hold is not None:
                            transaction.evidence_binding = record.evidence_hold.binding
            watchdog = self._asr_final_watchdog_task
            self._asr_final_watchdog_task = None
            if watchdog is not None and watchdog is not asyncio.current_task():
                watchdog.cancel()
            receipt = await self._post_exact_interval_event(
                transaction,
                ProviderFinalReceived(pending),
            )
            if receipt is None and self._exact_interval_runtime_is_current(
                transaction
            ):
                await self._fail_exact_interval_group(
                    transaction,
                    "ASR_EXACT_INTERVAL_FINAL_UNSETTLED",
                )
        if (
            transaction.evidence_binding is not None
            and transaction.resolved_disposition is None
            and self._exact_interval_runtime_is_current(transaction)
        ):
            if transaction.audio_handoff_task is None:
                transaction.audio_handoff_task = asyncio.create_task(
                    self._handoff_exact_evidence_audio(transaction, context, deadline),
                    name="asr-evidence-audio-handoff",
                )
                self._track_admission_effect_task(transaction.audio_handoff_task, transaction.turn_token)
                transaction.audio_handoff_task.add_done_callback(self._admission_effect_done)
            await asyncio.shield(transaction.audio_handoff_task)
        return context.settled

    def _observe_provider_evidence(
        self, transaction: _ProviderExactIntervalTransaction,
        binding: ProviderEvidenceBinding, record: Any, proof: EvidenceProof | None = None,
    ) -> EvidenceProof | None:
        """Observe an existing Admission-owned key without producing authority."""
        if (
            not self._asr_evidence_observation_enabled
            or not self._exact_interval_evidence_owner_is_current(transaction)
            or record is None or record.terminal_disposition is not None
            or record.provider_key != binding.provider_key
            or record.turn_token != binding.turn_token
            or record.record_generation != binding.record_generation
            or binding.provider_key != transaction.provider_key
            or binding.turn_token != transaction.turn_token
            or (proof is not None and proof.binding != binding)
        ):
            return None
        scope = binding.target_range.timeline
        if (
            scope[0] != transaction.runtime_identity.session_epoch
            or transaction.sealed_token is None
            or scope[1] != transaction.sealed_token.transport_generation
            or scope[2] != transaction.reservation.timeline_generation
        ):
            return None
        if self._asr_evidence_observer_scope != scope:
            self._asr_evidence_observer = EvidenceObservationRegistry(active_key_capacity=8)
            self._asr_evidence_observer_scope = scope
        observed = self._asr_evidence_observer.observe(
            binding, proof.scores if proof is not None else (),
            proof.continuity if proof is not None else (),
        )
        if observed is not None:
            transaction.observation_binding = binding
        return observed

    def _retire_provider_evidence_observation(
        self, transaction: _ProviderExactIntervalTransaction,
    ) -> None:
        binding = transaction.observation_binding
        if (
            binding is not None and self._asr_evidence_observer is not None
            and binding.target_range.timeline == self._asr_evidence_observer_scope
        ):
            self._asr_evidence_observer.retire(binding)

    async def _submit_provider_evidence_proof(self, proof: EvidenceProof) -> bool:
        """Accept only an explicitly bound producer proof, never infer continuity."""
        if not self._asr_evidence_hold_enabled or type(proof) is not EvidenceProof:
            return False
        transaction = self._asr_provider_exact_intervals.get(proof.binding.provider_key)
        if (
            transaction is None or transaction.evidence_binding != proof.binding
            or not self._exact_interval_evidence_owner_is_current(transaction)
            or not self._speaker_exact_installation_is_current(transaction)
        ):
            return False
        record = await self._asr_admission.get_record(transaction.turn_token)
        if (
            not self._exact_interval_evidence_owner_is_current(transaction)
            or record is None or record.evidence_hold is None
            or record.evidence_hold.binding != proof.binding
        ):
            return False
        self._observe_provider_evidence(transaction, proof.binding, record, proof)
        receipt = await self._post_exact_interval_event(
            transaction, EvidenceHoldResolved(record.evidence_hold.ticket, proof),
        )
        return receipt is not None and receipt.outcome in {
            ExactIntervalOutcome.HELD, ExactIntervalOutcome.RESOLVED,
        }

    async def _handoff_exact_evidence_audio(
        self, transaction: _ProviderExactIntervalTransaction,
        context: _AdmissionFinalContext, boundary_deadline: float,
    ) -> bool:
        """Retire old physical revoke rights while its text remains reserved."""
        handoff = _PendingTurnHandoff(
            replace(transaction.runtime_identity, turn_token=None),
            asyncio.get_running_loop().create_future(),
        )
        if (
            not self._exact_interval_runtime_is_current(transaction)
            or self._asr_sealed_turn_token != context.sealed_token
            or context.lifecycle.snapshot.state is not VoiceLifecycleState.DRAINING
            or self._asr_pending_turn_handoff is not None
        ):
            return False
        self._asr_pending_turn_handoff = handoff
        completed = False
        try:
            async with asyncio.timeout(_EXACT_PENDING_HANDOFF_TIMEOUT_SECONDS):
                result = await transaction.detector.complete_provider_speaker_boundary(
                    transaction.snapshot,
                    successor_evidence_lease=transaction.successor_evidence_lease,
                    deadline=boundary_deadline,
                )
                if (
                    result not in {"completed", "already_completed"}
                    or not self._exact_interval_runtime_is_current(transaction)
                    or self._asr_sealed_turn_token != context.sealed_token
                    or context.lifecycle.snapshot.state is not VoiceLifecycleState.DRAINING
                ):
                    return False
                lifecycle = context.lifecycle
                pending = bool(
                    lifecycle.has_pending_turn
                    or (lifecycle.pending_turn_token is not None
                        and lifecycle.pending_turn_token in self._asr_provider_started_turns.values())
                )
                lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
                self._asr_turn_prepared = False
                self._asr_received_audio = False
                self._asr_sealed_turn_token = None
                self._asr_sealed_provider_key = None
                self._asr_provider_candidate_fence = None
                self._asr_turn_endpointed_at = None
                if self._asr_partial_turn_token == context.turn_token:
                    self._asr_partial_turn_token = None
                # This bit transfers only lifecycle authority; final settlement
                # retains the old correlator, Admission record and transcript.
                context.audio_handoff_completed = True
                if not pending:
                    lifecycle.preserve_unconfirmed_pending_audio()
                await self._send_exact_lifecycle_state(
                    VoiceLifecycleState.WARM_IDLE, provider=context.provider,
                    session_epoch=context.epoch, expected_identity=handoff.identity,
                )
                if not self._runtime_identity_matches(handoff.identity):
                    return False
                if pending:
                    await self._activate_pending_independent_turn(
                        context.epoch, bounded_notification=True,
                    )
                else:
                    self._schedule_transport_warm_expiry(
                        context.epoch, expected_state=VoiceLifecycleState.WARM_IDLE,
                    )
                completed = self._runtime_identity_matches(handoff.identity)
                return completed
        finally:
            failed_current = not completed and self._runtime_identity_matches(handoff.identity)
            if failed_current:
                cleanup = asyncio.create_task(
                    self._handle_independent_asr_error(
                        context.epoch, context.provider,
                        status_code="ASR_CORE_TRANSCRIPT_BACKPRESSURE",
                        reason_code="ASR_EVIDENCE_AUDIO_HANDOFF_FAILED",
                        expected_identity=handoff.identity,
                        failed_operation="evidence_audio_handoff",
                        failed_check="boundary_or_successor_unsettled",
                        notification_timeout_seconds=_EXACT_HANDOFF_FAILURE_NOTIFICATION_TIMEOUT_SECONDS,
                    ), name="asr-evidence-audio-handoff-failure",
                )
                self._asr_exact_callback_tasks.add(cleanup)

                def failed_callback_done(task: asyncio.Task[None]) -> None:
                    self._asr_exact_callback_tasks.discard(task)
                    self._admission_effect_done(task)

                cleanup.add_done_callback(failed_callback_done)
            if not failed_current and self._asr_pending_turn_handoff is handoff:
                self._asr_pending_turn_handoff = None
            if not handoff.completion.done():
                handoff.completion.set_result(completed)

    async def _seal_independent_asr_provider_turn_transaction(
        self,
        epoch: int,
        *,
        provider_key: ProviderUtteranceKey | None,
        provider_snapshot: ProviderSpeakerBoundarySnapshot | None,
        deadline: float | None,
    ) -> tuple[
        _ProviderTurnSealTransaction | None,
        str | None,
        _AsrRuntimeIdentity | None,
    ]:
        """Linearize Provider seal and optional reject under Core -> Detector."""

        async with self._asr_final_lock:
            lifecycle = self._asr_lifecycle
            detector = self._asr_detector
            if (
                epoch != self._asr_session_epoch
                or lifecycle is None
                or detector is None
                or self._asr_session is None
                or lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
                or not self._asr_turn_prepared
                or (
                    provider_key is not None
                    and not self._provider_key_timeline_is_current(provider_key)
                )
            ):
                return None, None, None
            try:
                turn_token = self._capture_turn_token(lifecycle)
            except Exception:
                return None, None, None
            identity = self._capture_runtime_identity(
                ingress_token=turn_token.ingress,
                turn_token=turn_token,
            )
            if not self._asr_endpointing_ready(lifecycle, detector, turn_token):
                return None, "ASR_BLOCKED_ENDPOINTING", identity
            final_key = FinalKey.from_turn(turn_token)
            if provider_key is not None:
                correlator = self._asr_provider_correlator
                if correlator is None:
                    await self._post_admission_event(turn_token, Reset())
                    return None, "ASR_ENDPOINTING_FAILED", identity
                try:
                    correlator.bind_ordered(provider_key, turn_token)
                    await self._post_admission_event(
                        turn_token,
                        ProviderBound(provider_key),
                    )
                except (ProviderAliasConflictError, KeyError):
                    await self._post_admission_event(turn_token, Reset())
                    return None, "ASR_ENDPOINTING_FAILED", identity

            provider_fence: ProviderCandidateFence | None = None
            if not _uses_smart_turn_endpointing(lifecycle.provider_policy):
                seal_started_at = time.monotonic()
                seal_budget = (
                    max(0.0, deadline - seal_started_at)
                    if deadline is not None
                    else None
                )
                try:
                    if provider_key is None:
                        provider_fence = await detector.seal_provider_candidate(
                            turn_token,
                            deadline=deadline,
                        )
                    else:
                        provider_fence = await detector.seal_provider_candidate(
                            turn_token,
                            speaker_snapshot=provider_snapshot,
                            deadline=deadline,
                        )
                except asyncio.CancelledError:
                    await self._post_admission_event(turn_token, Reset())
                    raise
                except Exception:
                    provider_fence = None
                    logger.warning(
                        "[%s] provider candidate seal failed",
                        self.display_name,
                    )
                if (
                    not self._runtime_identity_matches(identity)
                    or self._asr_lifecycle is not lifecycle
                    or self._asr_detector is not detector
                    or lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
                    or (
                        provider_key is not None
                        and not self._provider_key_timeline_is_current(provider_key)
                    )
                ):
                    await self._post_admission_event(turn_token, Reset())
                    return None, None, None
                seal_timed_out = bool(
                    provider_key is not None
                    and deadline is not None
                    and provider_fence is None
                    and (
                        time.monotonic() >= deadline
                        or seal_budget == 0.0
                        # Windows event-loop timers may wake slightly before
                        # the monotonic target.  Distinguish that bounded wait
                        # from an immediate stale/no-candidate None without
                        # broadening every seal failure into fail-open.
                        or time.monotonic() - seal_started_at
                        >= max(0.0, (seal_budget or 0.0) - 0.02)
                    )
                )
                if provider_fence is None and not seal_timed_out:
                    await self._post_admission_event(turn_token, Reset())
                    return None, "ASR_ENDPOINTING_FAILED", identity
                self._asr_provider_candidate_fence = provider_fence
                self._asr_sealed_provider_key = provider_key

            lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
            sealed_token = self._capture_transport_token(lifecycle)
            self._asr_sealed_turn_token = sealed_token
            capability: RejectionCapability | None = None
            if provider_fence is not None and provider_key is not None:
                try:
                    admission_record = await self._asr_admission.get_record(turn_token)
                    candidate = (
                        admission_record.speaker_candidate
                        if admission_record is not None
                        else None
                    )
                    if candidate is None:
                        candidate = detector.pending_provider_speaker_candidate(
                            provider_fence
                        )
                    lease = (
                        await detector.prepare_candidate_rejection(candidate)
                        if candidate is not None
                        else None
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    lease = None
                if lease is not None:
                    self._asr_admission_candidate_turns[lease.shadow_candidate] = (
                        turn_token
                    )
                    capability = self._register_admission_capability(
                        lease,
                        kind=RejectionCapabilityKind.SEALED,
                        provider_key=provider_key,
                    )
            if capability is None and provider_key is not None:
                await self._post_admission_event(
                    turn_token,
                    BoundaryUnknown(provider_key),
                )
            await self._post_admission_event(
                turn_token,
                TurnSealed(capability),
            )
            if provider_fence is not None:
                await self._post_admission_event(
                    turn_token,
                    MicroEventPending(),
                )
            sealed_wait_event = self._asr_admission_turn_sealed_events.get(turn_token)
            if sealed_wait_event is not None:
                sealed_wait_event.set()

            return (
                _ProviderTurnSealTransaction(
                    lifecycle=lifecycle,
                    turn_token=turn_token,
                    sealed_token=sealed_token,
                    final_key=final_key,
                    identity=identity,
                ),
                None,
                None,
            )

    async def _handle_independent_asr_endpoint(
        self,
        epoch: int,
        *,
        provider_key: ProviderUtteranceKey | None = None,
        provider_snapshot: ProviderSpeakerBoundarySnapshot | None = None,
        deadline: float | None = None,
    ) -> None:
        """Seal the current turn immediately at its semantic endpoint."""

        if epoch != self._asr_session_epoch:
            return
        provider = self._asr_provider or "unknown"
        lifecycle = self._asr_lifecycle
        if lifecycle is None:
            return
        provider_identity = (
            self._capture_runtime_identity() if provider_key is not None else None
        )

        def provider_key_is_current() -> bool:
            return bool(
                provider_key is None
                or (
                    provider_identity is not None
                    and self._runtime_identity_matches(provider_identity)
                    and self._provider_key_timeline_is_current(provider_key)
                )
            )

        if (
            provider_key is not None
            and lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
        ):
            # The Provider key, not a local resume/pause credit, authorizes a
            # new logical text turn. Existing onset metadata is only a timing
            # hint; if it is absent or stale, wake the turn without it.
            ingress_token = self._asr_current_ingress_token
            if ingress_token is None or not self._ingress_token_matches(ingress_token):
                return
            if self._asr_overlap_completed_token != ingress_token:
                self._asr_overlap_completed_token = ingress_token
                self._asr_overlap_completed_onsets.clear()
                self._asr_overlap_completed_turns = 0
            if self._asr_overlap_completed_turns <= 0:
                onset_at = self._asr_overlap_onset_at
                if onset_at is not None:
                    self._asr_overlap_completed_onsets.append(onset_at)
                self._asr_overlap_completed_turns = 1
        if (
            lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
            and self._asr_overlap_completed_turns > 0
        ):
            completed_token = self._asr_overlap_completed_token
            if (
                completed_token is None
                or lifecycle.provider_policy.endpoint_authority != "provider"
                or completed_token != self._asr_current_ingress_token
                or not self._ingress_token_matches(completed_token)
            ):
                # The credit belongs to a superseded ingress generation (hard
                # mute, abort, or route swap rotated the token), so drop it
                # instead of waking a stale replacement turn.
                self._asr_overlap_completed_token = None
                self._asr_overlap_completed_turns = 0
                return
            # A provider endpoint reaching Core in WARM_IDLE means the ordered
            # FIFO holds a turn whose local onset and pause both happened while
            # the previous turn was still ACTIVE (its endpoint was queued
            # behind that turn's delayed final). Redeem one completed-overlap
            # credit: replay the onset so the lifecycle is ACTIVE and prepared,
            # then fall through to seal immediately, letting the queued final
            # right behind this endpoint find a DRAINING turn.
            # ⚠️ 先重放、确认真的醒过来了，**再**记账。重放可能唤不醒这一轮
            # （会话暂时不可用时停在 PREWARMING）；此时若 credit 已经扣掉，这张
            # credit 对应的 endpoint 就再也封不了口，紧随其后的 final 会被整条
            # 丢弃，而被弹出的 onset 还会被更晚的回合继承（拿错视觉窗口）。
            replay_onset_at = (
                self._asr_overlap_completed_onsets[0]
                if self._asr_overlap_completed_onsets
                else None
            )
            # 把真实开口时刻交给重放：直接确认分支会优先取 pending onset，于是
            # SPEECH_CONFIRMED 打上的是用户当初开口的时刻，而不是这次重放的时刻。
            _lent_pending_onset = False
            if (
                replay_onset_at is not None
                and self._asr_pending_speech_onset_at is None
            ):
                self._asr_pending_speech_onset_at = replay_onset_at
                _lent_pending_onset = True
            pending_before = self._asr_pending_speech_confirmed
            credit_consumed = False
            await self._handle_independent_asr_activity(
                SpeechActivityEvent.SPEECH_RESUMED,
                epoch,
            )
            if (
                epoch != self._asr_session_epoch
                or self._asr_lifecycle is not lifecycle
                or not provider_key_is_current()
            ):
                return
            if (
                not pending_before
                and self._asr_pending_speech_confirmed
                and lifecycle.snapshot.state is VoiceLifecycleState.PREWARMING
            ):
                # 重放被"传输未就绪"挡住了，停在 PREWARMING 并挂起了确认。
                # （provider 权威下 SOFT_WAKE→PREWARMING 之后拦路的就是
                # asr_session.is_ready —— _ensure_smart_turn_ready 在 provider
                # 权威下无 await 直接返回 True；PREWARMING 的 lifecycle 广播没送达
                # 是同态的另一种成因。别在注释里写死"唯一成因"。）
                #
                # 但这一轮**不需要**传输：它的音频早在上一轮还 ACTIVE 时就已经过
                # 线，endpoint 和它自己的 final 已经排在有序 FIFO 里、正要到达。
                # 而且能走到这里就说明老 session 还被认领着——_restart_transport
                # 和 _close_transport_only 都是先把 _asr_session 置 None 再 close，
                # 之后 is_adopted_candidate() 会丢掉它的全部回调——也就是说重连
                # **还没开始**，那条 final 就排在后面。等重连救不回它：重连会换新
                # session，老队列里那条 final 必定在 is_adopted_candidate() 上被
                # 丢掉。就地补完确认，让紧随其后的 final 找到一个 DRAINING 的回合。
                #
                # 这里刻意**不**走 _handle_independent_asr_error：那条出口会 bump
                # epoch、拆掉整个 session、cancel 掉正在跑的重连任务，并把语音路由
                # fail-closed 到本次会话结束——为一句其实救得回来的话把整场语音判
                # 死，违反"绝不丢用户的句子"。真丢的情况（final 始终不来）由下面封
                # 口时装上的 provider-final watchdog 兜底：10s 硬顶，且不受
                # _voice_input_resource_optimization_enabled 开关影响（那个开关会
                # 让 _schedule_transport_warm_expiry 直接 return，所以不能靠它）。
                #
                # 门里 not pending_before 是刻意的：只补偿**这次重放自己造出来的**
                # 那笔挂起确认，不吞别人的。
                #
                # 刻意不做的两件事：不调 _activate_asr_audio_dispatcher /
                # drain_active_start_audio（重连确认分支有，但这一轮的音频早已过
                # 线，本地没有待发缓冲）；不武装 _schedule_transport_warm_expiry
                # （忙窗口的界由上面那个 watchdog 提供）。将来若有人让这条路承接
                # 未发出的 PCM，必须回来补第一条。
                lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
                self._asr_turn_onset_at = (
                    self._asr_pending_speech_onset_at
                    if self._asr_pending_speech_onset_at is not None
                    else time.monotonic()
                )
                # 与 _restart_transport 的补确认块同序：确认一落地就把挂起状态
                # 清掉。真实开口时刻已经装进 _asr_turn_onset_at，下面那个 await
                # 无论怎么返回都不会把它丢掉。
                self._asr_pending_speech_confirmed = False
                self._asr_pending_speech_onset_at = None
                # 这张 credit 就是被这次确认兑走的，账要跟着确认一起落。留到
                # 下面记的话，身份漂移那条 return 会把它跳过：这一轮照常在替换后的
                # 传输上封口，而陈旧的 credit 与 onset 还压在队列里 —— 后面真实的
                # overlap 排在它后面，兑付时拿到错的 onset，多出来的那张还会让某个
                # endpoint 重放到不属于它的回合上，把一条 final 丢掉。
                self._consume_overlap_completed_credit()
                credit_consumed = True
                self._asr_turn_audio_started_at = time.monotonic()
                self._asr_first_partial_recorded = False
                confirm_identity = self._capture_runtime_identity(
                    ingress_token=self._asr_current_ingress_token,
                )
                delivered = await self._send_asr_lifecycle_state(
                    VoiceLifecycleState.ACTIVE,
                    provider=provider,
                    session_epoch=epoch,
                    expected_identity=confirm_identity,
                )
                if (
                    not delivered
                    or epoch != self._asr_session_epoch
                    or self._asr_lifecycle is not lifecycle
                ):
                    # delivered 为假只可能是运行时身份漂移：_send_asr_lifecycle_state
                    # 吞掉回调异常之后，返回的就是 _runtime_identity_matches。而
                    # _restart_transport / _close_transport_only 换掉 _asr_session 与
                    # transport_generation 时都不走 _reset_asr_turn_state，所以这里留下
                    # 的挂起状态没人回收：上面 transition(SPEECH_CONFIRMED) 已经把它兑付
                    # 进 _asr_turn_onset_at，两个兑付点又都以 PREWARMING 为闸、ACTIVE 下
                    # 一律跳过。残留下去会被后面某个不相干的回合当成自己的开口时刻，还会
                    # 把补偿门 not pending_before 恒假化，让重叠补偿此后静默失效。
                    return
            if lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE:
                # 没唤醒。credit 原样留着等下一次兑付；借出去的 onset 也要收回，
                # 免得它被后面某个不相干的回合当成自己的起点。
                # ⚠️ 只有在**没有**挂起的确认时才收回。session 未就绪时
                # _handle_independent_asr_activity 会停在 PREWARMING、置上
                # _asr_pending_speech_confirmed 并**特意留着**这个 onset 等重连后
                # 的确认去取；此时收回等于让那次确认退回用新的 detected_at，把用户
                # 真实开口以来的帧全排除掉。
                if (
                    _lent_pending_onset
                    and not self._asr_pending_speech_confirmed
                    and self._asr_pending_speech_onset_at == replay_onset_at
                ):
                    self._asr_pending_speech_onset_at = None
                return
            # 确认 ACTIVE 之后才记账（补确认那条路已经在上面记过了）。
            if not credit_consumed:
                self._consume_overlap_completed_credit()
        if lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE:
            if not provider_key_is_current():
                return
            if not self._asr_turn_prepared:
                # A rejected preparation keeps the lifecycle ACTIVE so the
                # utterance can retry (SPEECH_RESUMED re-prepares), but Core
                # never ran the interruption/external-turn pause for this
                # turn. Re-prepare before sealing; without a successful
                # preparation the final must never reach Core, so fail
                # closed instead of sealing an unprepared turn.
                await self._prepare_independent_asr_turn(epoch)
                if (
                    epoch != self._asr_session_epoch
                    or self._asr_lifecycle is not lifecycle
                    or lifecycle.snapshot.state is not VoiceLifecycleState.ACTIVE
                    or not provider_key_is_current()
                ):
                    return
                if not self._asr_turn_prepared:
                    await self._handle_independent_asr_error(
                        epoch,
                        provider,
                        status_code="ASR_CORE_TURN_REJECTED",
                    )
                    return
            (
                transaction,
                failure_status,
                failure_identity,
            ) = await self._seal_independent_asr_provider_turn_transaction(
                epoch,
                provider_key=provider_key,
                provider_snapshot=provider_snapshot,
                deadline=deadline,
            )
            if failure_status is not None:
                await self._handle_independent_asr_error(
                    epoch,
                    provider,
                    status_code=failure_status,
                    expected_identity=failure_identity,
                )
                return
            if transaction is None:
                return
            lifecycle = transaction.lifecycle
            turn_token = transaction.turn_token
            self._asr_turn_endpointed_at = time.monotonic()
            self._asr_last_turn_endpointed_at = self._asr_turn_endpointed_at
            # 与 Core 侧 record.turn_id 同构（asr_runtime.py 的
            # external_turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"），
            # 好让冻结时能直接判"这个封口是不是这条 record 的"。
            self._asr_last_turn_endpointed_key = (
                f"asr-{turn_token.ingress.session_epoch}-{turn_token.turn_id}"
            )
            self._schedule_provider_final_watchdog(
                epoch,
                lifecycle,
                transaction.sealed_token,
            )
            await self._materialize_deferred_provider_started_turn(lifecycle)
            await self._send_asr_lifecycle_state(
                VoiceLifecycleState.DRAINING,
                provider=provider,
                session_epoch=epoch,
                expected_identity=transaction.identity,
            )

    async def _activate_pending_independent_turn(
        self, epoch: int, *, bounded_notification: bool = False,
    ) -> None:
        """Start the pending turn after the previous final completes."""

        if epoch != self._asr_session_epoch:
            return
        lifecycle = self._asr_lifecycle
        pending_token = lifecycle.pending_turn_token if lifecycle is not None else None
        provider_pending = bool(
            lifecycle is not None
            and lifecycle.provider_policy.endpoint_authority == "provider"
            and lifecycle.has_pending_turn_identity
            and pending_token is not None
            and pending_token in self._asr_provider_started_turns.values()
        )
        if lifecycle is None or not (lifecycle.has_pending_turn or provider_pending):
            # has_pending_turn 还要求 pending buffer 里真有音频：speech 先到、或者
            # 对应 PCM 被丢弃时会走到这里。不清的话这个 onset 会被**下一个**真实
            # pending turn 复用，把那一轮的起点提前到上一轮，视觉帧绑错回合。
            self._asr_pending_turn_onset_at = None
            if lifecycle is not None:
                lifecycle.discard_unconfirmed_pending_audio()
            return
        if lifecycle.snapshot.state is not VoiceLifecycleState.WARM_IDLE:
            lifecycle.discard_pending_turn()
            self._asr_pending_turn_onset_at = None
            self._asr_pending_detector_candidate = None
            return
        payload = lifecycle.begin_pending_turn(allow_empty=provider_pending)
        # begin_pending_turn() 内部完成 SPEECH_CONFIRMED 迁移（lifecycle.py），是第
        # 五个迁移点 —— 之前给另外四处补 onset 打点时漏了它，因为守卫只扫本模块的
        # 字面量。不补的话 _asr_turn_onset_at 还留着**上一轮**的值（它只在
        # close/abort/error 才清），Core 会拿上一轮的 onset 当本回合 started_at，于是
        # 上一轮保留的封口时刻反过来成了本回合的截止点，本回合之后拍的每一帧都被
        # accepts() 拒掉 —— 整轮退化成纯文本。
        self._asr_turn_onset_at = (
            self._asr_pending_turn_onset_at
            if self._asr_pending_turn_onset_at is not None
            else time.monotonic()
        )
        self._asr_pending_turn_onset_at = None
        if not payload and not provider_pending:
            return
        turn_token = self._capture_turn_token(lifecycle)
        pending_candidate = self._asr_pending_detector_candidate
        self._asr_pending_detector_candidate = None
        identity = self._capture_runtime_identity(
            ingress_token=turn_token.ingress,
            turn_token=turn_token,
        )
        self._asr_turn_audio_started_at = time.monotonic()
        self._asr_first_partial_recorded = False
        if not await self._ensure_smart_turn_ready(lifecycle, epoch):
            return
        if not self._runtime_identity_matches(identity):
            return
        notify_lifecycle = (
            self._send_exact_lifecycle_state
            if bounded_notification else self._send_asr_lifecycle_state
        )
        delivered = await notify_lifecycle(
            VoiceLifecycleState.ACTIVE,
            provider=identity.provider or "unknown",
            session_epoch=epoch,
            expected_identity=identity,
        )
        # begin_pending_turn already consumed this owner's buffered PCM. A
        # display timeout must not strand it, but a replaced owner cannot
        # prepare or activate the successor after this await.
        if not self._runtime_identity_matches(identity):
            return
        if not delivered and not bounded_notification:
            return
        if bounded_notification:
            try:
                async with asyncio.timeout(_EXACT_PENDING_PREPARE_TIMEOUT_SECONDS):
                    await self._prepare_independent_asr_turn(epoch)
            except TimeoutError:
                raise _PendingTurnPreparationError("ASR_PENDING_TURN_PREPARE_TIMEOUT") from None
            if self._runtime_identity_matches(identity) and not self._asr_turn_prepared:
                raise _PendingTurnPreparationError("ASR_PENDING_TURN_PREPARE_REJECTED")
        else:
            await self._prepare_independent_asr_turn(epoch)
        if not self._runtime_identity_matches(identity):
            return
        asr_session = identity.session
        if asr_session is None or not getattr(asr_session, "is_ready", True):
            await self._handle_independent_asr_error(
                identity.session_epoch,
                identity.provider or "unknown",
                expected_identity=identity,
            )
            return
        detector = identity.detector
        if not self._asr_endpointing_ready(lifecycle, detector, turn_token):
            await self._handle_independent_asr_error(
                identity.session_epoch,
                identity.provider or "unknown",
                status_code="ASR_BLOCKED_ENDPOINTING",
                expected_identity=identity,
            )
            return
        if pending_candidate is not None:
            assert detector is not None
            try:
                bound = await detector.bind_candidate(pending_candidate, turn_token)
            except asyncio.CancelledError:
                raise
            except Exception:
                bound = None
            if not self._runtime_identity_matches(identity):
                return
            if bound is None and _uses_smart_turn_endpointing(
                lifecycle.provider_policy
            ):
                await self._handle_independent_asr_error(
                    identity.session_epoch,
                    identity.provider or "unknown",
                    status_code="ASR_ENDPOINTING_FAILED",
                    expected_identity=identity,
                )
                return
        elif not self._runtime_identity_matches(identity):
            return
        if not await self._activate_asr_audio_dispatcher(
            lifecycle,
            turn_token,
            buffered_pcm16=payload,
        ):
            await self._handle_independent_asr_error(
                identity.session_epoch,
                identity.provider or "unknown",
                status_code="ASR_AUDIO_ORDERING_FAILED",
                expected_identity=identity,
            )
            return
        if not self._runtime_identity_matches(identity):
            return
        self._asr_received_audio = True
        self._asr_audio_bytes += len(payload)

    async def _send_independent_asr_preview(self, text: str, epoch: int) -> None:
        """Send display-only ASR partials without writing conversation history."""

        clean = str(text or "").strip()
        if not clean or epoch != self._asr_session_epoch:
            return
        turn_token = self._asr_partial_turn_token
        if turn_token is None or not self._partial_turn_is_current(turn_token):
            return
        settlement = self._asr_partial_settlements.get(turn_token)
        if settlement is not None:
            if settlement[1] is AdmissionDisposition.FORWARD:
                await self._deliver_independent_asr_preview(turn_token, clean)
            return
        if (
            self._speaker_verifier_enforces_admission
            or turn_token in self._asr_speaker_authoritative_turns
        ):
            self._asr_quarantined_partials[turn_token] = clean
            self._speaker_rejection_metrics["speaker_partial_quarantined_count"] += 1
            return
        await self._deliver_independent_asr_preview(turn_token, clean)

    async def _handle_independent_asr_final(
        self,
        text: str,
        epoch: int,
        provider: str,
        *,
        provider_key: ProviderUtteranceKey | None = None,
        received_at: float | None = None,
        deadline: float | None = None,
    ) -> asyncio.Event | None:
        """Publish one immutable final; admission owns every disposition."""

        self._schedule_pipeline_session_event("asr_final_received", epoch, has_text=bool(text.strip()))
        if epoch != self._asr_session_epoch:
            self._schedule_pipeline_session_event("asr_final_ignored", epoch, reason="session_stale")
            return
        if received_at is None:
            received_at = (
                deadline - _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS
                if deadline is not None
                else time.monotonic()
            )
        if deadline is None:
            deadline = received_at + _PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS
        if deadline < received_at:
            self._schedule_pipeline_session_event("asr_final_ignored", epoch, reason="invalid_deadline")
            return
        async with self._asr_final_lock:
            lifecycle = self._asr_lifecycle
            sealed_token = self._asr_sealed_turn_token
            if (
                epoch != self._asr_session_epoch
                or lifecycle is None
                or sealed_token is None
                or lifecycle.snapshot.state is not VoiceLifecycleState.DRAINING
                or not self._transport_token_matches(sealed_token, lifecycle)
                or (
                    provider_key is not None
                    and self._asr_sealed_provider_key != provider_key
                )
            ):
                self._schedule_pipeline_session_event("asr_final_ignored", epoch, reason="sealed_turn_mismatch")
                return
            turn_token = sealed_token.turn
            admission_record = await self._asr_admission.get_record(turn_token)
            if (
                epoch != self._asr_session_epoch
                or self._asr_lifecycle is not lifecycle
                or self._asr_sealed_turn_token != sealed_token
            ):
                return
            if (
                self._speaker_verifier_enforces_admission
                and lifecycle.provider_policy.endpoint_authority == "provider"
            ):
                lease_token = self._asr_admission_turn_leases.get(turn_token)
                lease_record = (
                    await self._asr_admission.get_speaker_lease(lease_token)
                    if lease_token is not None
                    else None
                )
                if (
                    epoch != self._asr_session_epoch
                    or self._asr_lifecycle is not lifecycle
                    or self._asr_sealed_turn_token != sealed_token
                ):
                    return
                binding_is_exact = bool(
                    admission_record is not None
                    and provider_key is not None
                    and self._asr_sealed_provider_key == provider_key
                    and self._asr_provider_started_turns.get(provider_key) == turn_token
                    and admission_record.provider_binding_state
                    is ProviderBindingState.BOUND
                    and admission_record.candidate_binding_state
                    is CandidateBindingState.BOUND
                    and admission_record.provider_key == provider_key
                    and lease_token is not None
                    and admission_record.speaker_lease_token == lease_token
                    and admission_record.speaker_candidate is not None
                    and self._asr_admission_candidate_leases.get(
                        admission_record.speaker_candidate
                    )
                    == lease_token
                    and lease_record is not None
                    and lease_record.lease_token == lease_token
                    and lease_record.candidate == admission_record.speaker_candidate
                    and any(
                        child.provider_key == provider_key
                        and child.turn_token == turn_token
                        for child in lease_record.child_bindings
                    )
                )
                binding_is_exact_unavailable = bool(
                    admission_record is not None
                    and provider_key is not None
                    and self._asr_sealed_provider_key == provider_key
                    and self._asr_provider_started_turns.get(provider_key)
                    == turn_token
                    and admission_record.provider_binding_state
                    is ProviderBindingState.BOUND
                    and admission_record.candidate_binding_state
                    is CandidateBindingState.BOUND
                    and admission_record.provider_key == provider_key
                    and admission_record.speaker_candidate is not None
                    and admission_record.speaker_lease_token is None
                    and admission_record.exact_interval_hold_id is None
                    and admission_record.capture_state is CaptureState.UNAVAILABLE
                    and admission_record.evidence_state is EvidenceState.UNAVAILABLE
                )
                if not (binding_is_exact or binding_is_exact_unavailable):
                    self._speaker_rejection_metrics[
                        "provider_candidate_bind_identity_rejected_count"
                    ] += 1
                    correlator = self._asr_provider_correlator
                    started_turn = (
                        self._asr_provider_started_turns.get(provider_key)
                        if provider_key is not None
                        else None
                    )
                    try:
                        await self._post_admission_event(turn_token, Reset())
                    except (AdmissionIngressClosedError, KeyError):
                        pass
                    cleanup_is_current = bool(
                        epoch == self._asr_session_epoch
                        and self._asr_lifecycle is lifecycle
                        and self._asr_sealed_turn_token == sealed_token
                        and self._asr_provider_correlator is correlator
                        and (
                            provider_key is None
                            or (
                                started_turn == turn_token
                                and self._asr_provider_started_turns.get(provider_key)
                                == turn_token
                            )
                        )
                    )
                    if (
                        cleanup_is_current
                        and provider_key is not None
                        and self._asr_provider_started_turns.get(provider_key)
                        == turn_token
                    ):
                        self._asr_provider_started_turns.pop(provider_key, None)
                    if cleanup_is_current and correlator is not None:
                        try:
                            correlator.abandon_turn(turn_token)
                        except ProviderAliasConflictError:
                            pass
                    settled = asyncio.Event()
                    settled.set()
                    return settled
            if (
                admission_record is None
                or admission_record.terminal_disposition is not None
            ):
                settled = asyncio.Event()
                settled.set()
                return settled
            if turn_token in self._asr_admission_final_contexts:
                return
            final_key = FinalKey.from_turn(turn_token)
            pending = PendingProviderFinal(
                provider_key=provider_key,
                provider=provider,
                text=str(text or "").strip(),
                received_at=received_at,
                admission_deadline=deadline,
            )
            correlator = self._asr_provider_correlator
            if provider_key is not None:
                if correlator is None:
                    return
                try:
                    correlator.record_final(provider_key, pending)
                except ProviderAliasConflictError:
                    return
            detector = self._asr_detector
            provider_fence = self._asr_provider_candidate_fence
            if (
                detector is not None
                and provider_fence is not None
                and not _uses_smart_turn_endpointing(lifecycle.provider_policy)
            ):
                # Provider-authority routes use local Silero only to throttle
                # idle PCM.  Its micro-event policy has no admission authority.
                await self._post_admission_event(
                    turn_token,
                    MicroEventUnavailable(),
                )
            identity = self._capture_runtime_identity(
                ingress_token=turn_token.ingress,
                turn_token=turn_token,
            )
            context = _AdmissionFinalContext(
                turn_token=turn_token,
                final_key=final_key,
                epoch=epoch,
                provider=provider,
                provider_key=provider_key,
                lifecycle=lifecycle,
                detector=detector,
                correlator=correlator,
                sealed_token=sealed_token,
                provider_fence=provider_fence,
                runtime_identity=identity,
                has_pending_turn=(
                    lifecycle.has_pending_turn
                    or (
                        lifecycle.provider_policy.endpoint_authority == "provider"
                        and lifecycle.has_pending_turn_identity
                        and lifecycle.pending_turn_token
                        in self._asr_provider_started_turns.values()
                    )
                ),
            )
            self._asr_admission_final_contexts[turn_token] = context
            watchdog = self._asr_final_watchdog_task
            self._asr_final_watchdog_task = None
            if watchdog is not None and watchdog is not asyncio.current_task():
                watchdog.cancel()
            try:
                effects = await self._post_admission_event(
                    turn_token,
                    ProviderFinalReceived(pending),
                    now=received_at,
                )
            except (AdmissionIngressClosedError, KeyError):
                if self._asr_admission_final_contexts.get(turn_token) is context:
                    self._asr_admission_final_contexts.pop(turn_token, None)
                context.settled.set()
                return context.settled
            except Exception:
                if self._asr_admission_final_contexts.get(turn_token) is context:
                    self._asr_admission_final_contexts.pop(turn_token, None)
                context.settled.set()
                raise
            if not any(isinstance(effect, ResolveReserved) for effect in effects):
                admission_record = await self._asr_admission.get_record(turn_token)
                if admission_record is None:
                    if self._asr_admission_final_contexts.get(turn_token) is context:
                        self._asr_admission_final_contexts.pop(turn_token, None)
                    context.settled.set()
                elif admission_record.terminal_disposition is not None:
                    ticket = admission_record.resolution_ticket
                    if ticket is not None and ticket.disposition in {
                        AdmissionDisposition.DROP,
                        AdmissionDisposition.ABANDON,
                    }:
                        # The terminal transition may have raced this final
                        # through another ingress consumer. Re-submit the same
                        # immutable ticket so whichever executor wins owns the
                        # late reservation and context; setdefault makes the
                        # duplicate idempotent.
                        resolver = asyncio.create_task(
                            self._resolve_admission_reservation(
                                ResolveReserved(ticket=ticket, final=None)
                            ),
                            name="voice-turn-admission-late-final-resolution",
                        )
                        self._track_admission_effect_task(resolver, turn_token)
                        resolver.add_done_callback(self._admission_effect_done)
                    else:
                        if (
                            self._asr_admission_final_contexts.get(turn_token)
                            is context
                        ):
                            self._asr_admission_final_contexts.pop(
                                turn_token,
                                None,
                            )
                        context.settled.set()
            return context.settled

    async def _dispatch_asr_transcript_envelope(
        self,
        envelope: TranscriptEnvelope,
    ) -> None:
        ingress_token = envelope.turn_token.ingress
        self._schedule_pipeline_event("transcript_callback", ingress_token, turn_id=envelope.turn_token.turn_id, outcome="started")
        degraded = False
        if not self._ingress_token_matches(ingress_token):
            self._schedule_pipeline_event("transcript_callback", ingress_token, turn_id=envelope.turn_token.turn_id, outcome="stale")
            # The envelope was accepted before the audio generation moved on,
            # so neither on_final nor a teardown path will run for this turn.
            # Release the Core-side pause keyed to it instead of leaking the
            # pause until the next turn.
            await self._notify_asr_turn_abandoned(envelope.turn_token)
            degraded = True
            execution = self._asr_admission_resolutions.get(envelope.final_key)
            if execution is not None and not execution.core_settled:
                execution.core_settled = True
                try:
                    await self._post_admission_event(
                        envelope.turn_token,
                        CoreSettled(execution.ticket, degraded=True),
                    )
                except KeyError:
                    pass
                if execution.settled.is_set():
                    self._asr_admission_resolutions.pop(
                        envelope.final_key,
                        None,
                    )
        else:
            identity = self._capture_runtime_identity(
                ingress_token=ingress_token,
                turn_token=envelope.turn_token,
            )
            try:
                await self._callbacks.on_final(
                    VoiceTranscriptEvent(
                        turn_token=envelope.turn_token,
                        provider=envelope.provider,
                        text=envelope.text,
                    )
                )
            except asyncio.CancelledError:
                self._schedule_pipeline_event("transcript_callback", ingress_token, turn_id=envelope.turn_token.turn_id, outcome="cancelled")
                degraded = True
                raise
            except Exception:
                self._schedule_pipeline_event("transcript_callback", ingress_token, turn_id=envelope.turn_token.turn_id, outcome="failed")
                degraded = True
                await self._send_asr_status(
                    "ASR_INDEPENDENT_INJECTION_FAILED",
                    envelope.provider,
                    session_epoch=ingress_token.session_epoch,
                    expected_identity=identity,
                )
            else:
                self._schedule_pipeline_event("transcript_callback", ingress_token, turn_id=envelope.turn_token.turn_id, outcome="returned")
            finally:
                execution = self._asr_admission_resolutions.get(envelope.final_key)
                if execution is not None and not execution.core_settled:
                    execution.core_settled = True
                    try:
                        await self._post_admission_event(
                            envelope.turn_token,
                            CoreSettled(execution.ticket, degraded=degraded),
                        )
                    except KeyError:
                        pass
                    if execution.settled.is_set():
                        self._asr_admission_resolutions.pop(
                            envelope.final_key,
                            None,
                        )

    def _schedule_asr_resolution_log(
        self,
        effect: ResolveReserved,
        *,
        stage: str,
        outcome: str | None = None,
        applied: bool | None = None,
    ) -> None:
        """Snapshot the ticket's verdict before cleanup, with bounded off-loop I/O.

        Dispatcher acceptance is deliberately separate from Provider ack,
        history writes and client presentation; it proves none of those.
        """
        try:
            metadata = self._asr_admission.snapshot_resolution_diagnostics(effect.ticket)
            metadata.update(diagnostic_context(self, effect.turn_token.ingress.session_epoch))
            metadata.update(stage=stage, dispatcher_outcome=outcome, dispatcher_applied=applied)
            self._schedule_asr_diagnostic_metadata(metadata)
        except Exception:
            pass

    def _schedule_provider_boundary_diagnostic(
        self,
        notification: ProviderUtteranceStartedNotification | ProviderEndpointNotification,
        epoch: int,
    ) -> None:
        try:
            from .boundary_settlement import boundary_transport_ref

            metadata = diagnostic_context(self, epoch)
            metadata["diagnostic_transport_ref"] = boundary_transport_ref(self._asr_session)
            started = isinstance(notification, ProviderUtteranceStartedNotification)
            audio_range = None if started else notification.audio_range
            metadata.update(
                stage="provider_started_received" if started else "provider_endpoint_received",
                provider_generation=notification.generation,
                provider_buffer_epoch=notification.buffer_epoch,
                provider_utterance_id=notification.utterance_id,
                provider_start_sample_16k=(
                    notification.audio_start_sample_16k if started
                    else audio_range.start_sample_16k if audio_range is not None else None
                ),
                provider_end_sample_16k=(audio_range.end_sample_16k if audio_range is not None else None),
                boundary_quality=None if started else notification.boundary_quality,
                boundary_phase=None if started else notification.phase,
            )
            self._schedule_asr_diagnostic_metadata(metadata, capacity=8)
        except Exception:
            pass

    def _accept_speaker_diagnostic(
        self, event: SpeakerShadowDiagnostic, *, activation_generation: str, source: object,
    ) -> None:
        """Read existing ownership only; never attach a candidate or admit text."""
        if (
            activation_generation != self._speaker_verifier_activation_generation
            or source is None
            or source is not getattr(self._asr_detector, "_speaker_shadow", None)
        ):
            return
        try:
            candidate = event.candidate
            exact = self._asr_provider_exact_candidates.get(candidate)
            ledger = self._asr_provider_speaker_ledgers.get(candidate)
            owner = exact if exact is not None else ledger
            if owner is None:
                if self._asr_lifecycle is None or not _uses_smart_turn_endpointing(self._asr_lifecycle.provider_policy):
                    return
                turn = self._asr_admission_candidate_turns.get(candidate)
                if turn is not None and self._ingress_token_matches(turn.ingress):
                    metadata = diagnostic_context(self, turn.ingress.session_epoch)
                    metadata.update(speaker_diagnostic_scalars(event))
                    metadata.update(turn_id=turn.turn_id, candidate_role="smart_turn")
                    self._schedule_asr_diagnostic_metadata(metadata, capacity=8)
                return
            if not self._runtime_identity_matches(owner.runtime_identity):
                # A detached/old candidate cannot borrow the current turn's key.
                return
            key = owner.provider_key
            turn = owner.turn_token
            metadata = diagnostic_context(self, owner.runtime_identity.session_epoch)
            metadata.update(speaker_diagnostic_scalars(event))
            metadata.update(
                provider_generation=key.generation if key is not None else None,
                provider_buffer_epoch=key.buffer_epoch if key is not None else None,
                provider_utterance_id=key.utterance_id if key is not None else None,
                turn_id=turn.turn_id if turn is not None else None,
                provider_start_sample_16k=(
                    exact.reservation.boundary.start_sample_16k if exact is not None
                    else ledger.anchor_start_sample_16k
                ),
                provider_end_sample_16k=(
                    exact.reservation.boundary.end_sample_16k if exact is not None else None
                ),
                audio_observed_through_sample_16k=(
                    ledger.observed_through_sample_16k if ledger is not None else None
                ),
                interval_state="exact" if exact is not None else ledger.state.value,
                candidate_role=(
                    "exact_target" if exact is not None and candidate == exact.target_candidate
                    else "exact_source" if exact is not None else "provisional"
                ),
            )
            # Reserve space for final verdicts even during diagnostic bursts.
            self._schedule_asr_diagnostic_metadata(metadata, capacity=8)
        except Exception:
            pass

    def _pipeline_observer(self):
        from .pipeline_diagnostics import PipelineDiagnostics

        observer = getattr(self, "_asr_pipeline_observer", None)
        if observer is None:
            observer = PipelineDiagnostics(self, self._schedule_pipeline_metadata)
            self._asr_pipeline_observer = observer
        return observer

    def _schedule_pipeline_metadata(self, metadata: dict, *, capacity: int = 32) -> None:
        """One bounded batch writer per Runtime; leave verdict log slots intact."""
        try:
            pending = getattr(self, "_asr_pipeline_pending", None)
            if pending is None:
                pending = self._asr_pipeline_pending = deque()
            if len(pending) >= min(capacity, 32):
                self._asr_resolution_log_dropped = getattr(self, "_asr_resolution_log_dropped", 0) + 1
                return
            metadata["diagnostic_records_dropped"] = getattr(self, "_asr_resolution_log_dropped", 0)
            self._asr_resolution_log_dropped = 0
            pending.append(metadata)
            running = getattr(self, "_asr_pipeline_log_task", None)
            if running is not None and not running.done():
                return

            async def persist() -> None:
                try:
                    while pending:
                        batch = tuple(pending.popleft() for _ in range(min(16, len(pending))))
                        try:
                            future = submit_resolution_log(batch, kind="pipeline")
                            if future is None:
                                self._asr_resolution_log_dropped += len(batch)
                            else:
                                await asyncio.wait_for(asyncio.wrap_future(future), timeout=0.2)
                        except asyncio.CancelledError:
                            self._asr_resolution_log_dropped += len(batch)
                            raise
                        except Exception:
                            self._asr_resolution_log_dropped += len(batch)
                finally:
                    self._asr_resolution_log_dropped += len(pending)
                    pending.clear()

            task = asyncio.create_task(persist(), name="asr-pipeline-log")
            self._asr_pipeline_log_task = task
            def reap_cancelled(done) -> None:
                # A task cancelled before its first step never enters finally.
                if done.cancelled() and self._asr_pipeline_log_task is done:
                    self._asr_resolution_log_dropped += len(pending)
                    pending.clear()
            task.add_done_callback(reap_cancelled)
            self._track_terminal_close_tasks({task})
        except Exception:
            pass

    def _schedule_pipeline_event(self, stage: str, ingress: VoiceIngressToken, **fields) -> None:
        try:
            self._schedule_pipeline_session_event(
                stage, ingress.session_epoch, audio_generation=ingress.audio_generation,
                route_generation=ingress.route_generation, lease_generation=ingress.lease_generation,
                **fields,
            )
        except Exception:
            pass

    def _schedule_pipeline_session_event(self, stage: str, epoch: int, **fields) -> None:
        try:
            observer = self._pipeline_observer()
            observer.flush()
            observer.event(stage, epoch, **fields)
        except Exception:
            pass

    def _schedule_pipeline_cleanup(self, metadata: dict, epoch: int) -> None:
        try:
            context = diagnostic_context(self, epoch)
            self._schedule_asr_diagnostic_metadata({**context, **metadata}, kind="cleanup")
        except Exception:
            pass

    def _observe_detector_audio_result(self, result, ingress, samples) -> None:
        try:
            status = getattr(result, "status", getattr(result, "throttle_action", None))
            self._observe_pipeline_audio("detector_audio", ingress, samples, outcome=getattr(status, "value", None))
            for activity in getattr(result, "events", ()):
                self._schedule_pipeline_event("vad_activity", ingress, reason=activity.value)
        except Exception:
            pass

    def _pipeline_audio_receipt(self, status: AsrSubmitStatus, ingress: VoiceIngressToken, frame: ProcessedVoiceFrame, reason: str) -> AsrSubmitStatus:
        self._observe_pipeline_audio(
            "audio_submit", ingress, len(frame.pcm16) // 2,
            outcome=status.value, reason=reason,
        )
        return status

    def _observe_pipeline_audio(self, stage: str, ingress: VoiceIngressToken, samples: int, **fields) -> None:
        try:
            self._pipeline_observer().audio(
                stage, ingress.session_epoch, audio_samples=samples,
                audio_generation=ingress.audio_generation, route_generation=ingress.route_generation,
                lease_generation=ingress.lease_generation, **fields,
            )
        except Exception:
            pass

    def _observe_endpoint_diagnostic(self, fields: dict, ingress, *, source, epoch: int) -> None:
        try:
            if ingress is None or epoch != ingress.session_epoch:
                return
            # Captured ingress/semantic identity stays with a late result. Never
            # read a replacement detector's state to decorate its predecessor.
            if source is not self._asr_detector or epoch != self._asr_session_epoch:
                return
            self._schedule_pipeline_event("endpoint_diagnostic", ingress, **fields)
        except Exception:
            pass

    def _schedule_speaker_fact_diagnostic(self, fact, owner) -> None:
        """Observe classification before queueing, without retaining score/PCM."""
        try:
            if not self._runtime_identity_matches(owner.runtime_identity):
                return
            key = owner.provider_key
            if key is None:
                return
            metadata = diagnostic_context(self, owner.runtime_identity.session_epoch)
            metadata.update(
                stage="speaker_fact_observed",
                provider_generation=key.generation,
                provider_buffer_epoch=key.buffer_epoch,
                provider_utterance_id=key.utterance_id,
                turn_id=owner.turn_token.turn_id if owner.turn_token is not None else None,
                speaker_sequence_no=fact.sequence_no,
                speaker_classification=("low" if isinstance(fact, SpeakerLow)
                                        else "high" if isinstance(fact, SpeakerHigh)
                                        else "unavailable"),
                checkpoint_kind=fact.checkpoint_kind.value if isinstance(fact, SpeakerLow) else None,
            )
            # Observation is not proof that the coordinator accepted the fact.
            self._schedule_asr_diagnostic_metadata(metadata, capacity=8)
        except Exception:
            pass

    def _schedule_provider_guard_diagnostic(
        self, key: ProviderUtteranceKey, epoch: int, *, stage: str, check: str,
        notification: ProviderEndpointNotification | None = None,
    ) -> None:
        """Snapshot technical ownership before mutation, using bounded off-loop I/O."""
        try:
            # A callback crossing an await must not borrow its successor's state.
            if epoch != self._asr_session_epoch:
                return
            ledger = self._asr_provider_speaker_key_ledgers.get(key)
            turn = self._asr_provider_started_turns.get(key)
            evidence = self._asr_provider_speaker_evidence_lease
            audio_range = notification.audio_range if notification is not None else None
            metadata = diagnostic_context(self, epoch)
            metadata.update(
                stage=stage, failed_check=check,
                provider_generation=key.generation,
                provider_buffer_epoch=key.buffer_epoch,
                provider_utterance_id=key.utterance_id,
                turn_id=turn.turn_id if turn is not None else None,
                ledger_turn_id=ledger.turn_token.turn_id if ledger is not None and ledger.turn_token is not None else None,
                ledger_state=ledger.state.value if ledger is not None else None,
                ledger_poisoned=ledger.poisoned_reason is not None if ledger is not None else None,
                expected_lease_generation=evidence.lease_generation if evidence is not None else None,
                ledger_lease_generation=ledger.evidence_lease.lease_generation if ledger is not None else None,
                anchor_start_sample_16k=ledger.anchor_start_sample_16k if ledger is not None else None,
                provider_start_sample_16k=audio_range.start_sample_16k if audio_range is not None else None,
                provider_end_sample_16k=audio_range.end_sample_16k if audio_range is not None else None,
                exact_interval_count=len(self._asr_provider_exact_intervals),
                exact_pending_count=len(self._asr_provider_exact_pending),
                exact_capacity=_MAX_PROVIDER_BOUNDARY_SNAPSHOTS,
            )
            self._schedule_asr_diagnostic_metadata(metadata, capacity=8)
        except Exception:
            pass

    def _schedule_asr_diagnostic_metadata(
        self, metadata: dict, *, capacity: int = 16, kind: str = "resolution",
    ) -> None:
        """One bounded delivery path for both decisions and their explanations."""
        try:
            if sum(
                task.get_name() == "asr-resolution-log" and not task.done()
                for task in self._asr_close_tasks
            ) >= capacity:
                self._asr_resolution_log_dropped = (
                    getattr(self, "_asr_resolution_log_dropped", 0) + 1
                )
                return
            metadata.update(
                diagnostic_records_dropped=getattr(self, "_asr_resolution_log_dropped", 0),
            )
            pending = submit_resolution_log(metadata, kind=kind)
            if pending is None:
                self._asr_resolution_log_dropped = metadata["diagnostic_records_dropped"] + 1
                return
            self._asr_resolution_log_dropped = 0

            async def persist() -> None:
                try:
                    await asyncio.wait_for(
                        asyncio.wrap_future(pending), timeout=0.2,
                    )
                except Exception:
                    pass
                finally:
                    # Timed-out queued writes are cancelled; an already
                    # running writer can still persist its captured record.
                    if pending.cancelled():
                        self._asr_resolution_log_dropped = (
                            getattr(self, "_asr_resolution_log_dropped", 0) + 1
                        )

            task = asyncio.create_task(persist(), name="asr-resolution-log")
            self._track_terminal_close_tasks({task})
        except Exception:
            # Diagnostics cannot fail or delay the authoritative decision.
            pass

    def _audio_failure_scalars(
        self, identity: _AsrRuntimeIdentity | None = None,
    ) -> dict[str, int | None]:
        captured = identity or self._capture_runtime_identity()
        detector = captured.detector
        lease = self._asr_provider_speaker_evidence_lease
        key = self._asr_sealed_provider_key
        turn = captured.turn_token
        return {
            "session_epoch": captured.session_epoch,
            "start_generation": captured.start_generation,
            "audio_generation": captured.audio_generation,
            "transport_generation": captured.transport_generation,
            "turn_id": turn.turn_id if turn is not None else None,
            "activation_generation": self._speaker_verifier_activation_generation,
            "candidate_generation": getattr(detector, "_candidate_generation", None),
            "sequence_no": self._asr_provider_speaker_sequence,
            "sample_cursor_16k": getattr(detector, "_provider_audio_sample_cursor_16k", None),
            "timeline_generation": getattr(detector, "_provider_audio_timeline_generation", None),
            "detector_epoch": getattr(lease, "detector_epoch", None),
            "lease_generation": getattr(lease, "lease_generation", None),
            "provider_generation": key.generation if type(key) is ProviderUtteranceKey else None,
            "provider_buffer_epoch": key.buffer_epoch if type(key) is ProviderUtteranceKey else None,
            "provider_utterance_id": key.utterance_id if type(key) is ProviderUtteranceKey else None,
        }

    def _new_audio_failure_context(
        self, operation: str, identity: _AsrRuntimeIdentity | None = None,
    ) -> AudioFailureContext:
        return AudioFailureContext(operation, expected=self._audio_failure_scalars(identity))

    def _schedule_asr_incident_log(
        self,
        *,
        incident_id: str,
        reason_code: str,
        stage: str,
        source_session_epoch: int,
        failure_context: AudioFailureContext | None = None,
    ) -> None:
        """Snapshot safe incident metadata before cleanup; persist off-loop."""

        # A broken or slow sink must neither gate failure retirement nor grow
        # an unbounded executor queue during repeated reconnect failures.
        if sum(
            task.get_name() == "asr-incident-log" and not task.done()
            for task in self._asr_close_tasks
        ) >= 4:
            self._asr_resolution_log_dropped = (
                getattr(self, "_asr_resolution_log_dropped", 0) + 1
            )
            return
        from config.application import APP_VERSION

        lease = self._asr_provider_speaker_evidence_lease
        key = self._asr_sealed_provider_key
        identity = self._capture_runtime_identity()
        previous = self._asr_speaker_degradation_incident
        metadata = {
            "schema": 1,
            "app_version": APP_VERSION,
            "incident_id": incident_id,
            "reason_code": (
                reason_code
                if type(reason_code) is str
                and _ASR_REASON_CODE_FULL_RE.fullmatch(reason_code) is not None
                else "ASR_INDEPENDENT_FAILED"
            ),
            "stage": stage,
            "source_session_epoch": source_session_epoch,
            "session_epoch": identity.session_epoch,
            "start_generation": identity.start_generation,
            "audio_generation": identity.audio_generation,
            "transport_generation": identity.transport_generation,
            "candidate_generation": (
                identity.detector._candidate_generation
                if type(identity.detector) is DetectorRuntime else None
            ),
            "timeline_generation": (
                identity.detector._provider_audio_timeline_generation
                if type(identity.detector) is DetectorRuntime else None
            ),
            "detector_epoch": (
                lease.detector_epoch
                if type(lease) is ProviderSpeakerEvidenceLease else None
            ),
            "lease_generation": (
                lease.lease_generation
                if type(lease) is ProviderSpeakerEvidenceLease else None
            ),
            "shadow_generation": (
                lease.candidate.shadow_generation
                if type(lease) is ProviderSpeakerEvidenceLease else None
            ),
            "provider_generation": (
                key.generation if type(key) is ProviderUtteranceKey else None
            ),
            "provider_buffer_epoch": (
                key.buffer_epoch if type(key) is ProviderUtteranceKey else None
            ),
            "provider_utterance_id": (
                key.utterance_id if type(key) is ProviderUtteranceKey else None
            ),
            "preceding_incident_id": (
                previous.incident_id
                if previous is not None
                and previous.incident_id != incident_id
                and previous.identity.session_epoch == source_session_epoch
                and previous.identity.start_generation == identity.start_generation
                and previous.identity.transport_generation == identity.transport_generation
                and previous.identity.detector is identity.detector
                else None
            ),
        }
        metadata["recorded_at"] = utc_now()
        try:
            metadata["diagnostic_session_ref"] = diagnostic_context(self, source_session_epoch)["diagnostic_session_ref"]
        except Exception:
            pass
        if failure_context is not None:
            metadata.update(failure_context.snapshot())

        async def persist() -> None:
            try:
                # Do not pass runtime objects, exception text, conversation,
                # PCM or speaker vectors to the logging worker.
                pending = submit_resolution_log(metadata, kind="incident")
                if pending is None:
                    self._asr_resolution_log_dropped += 1
                    return
                await asyncio.wait_for(asyncio.wrap_future(pending), timeout=0.2)
            except Exception:
                # Diagnostic I/O cannot replace the authoritative ASR failure.
                pass

        task = asyncio.create_task(persist(), name="asr-incident-log")
        self._track_terminal_close_tasks({task})

    async def _handle_independent_asr_error(
        self,
        epoch: int,
        provider: str,
        *,
        status_code: str = "ASR_INDEPENDENT_FAILED",
        expected_identity: _AsrRuntimeIdentity | None = None,
        reason_code: str | None = None,
        notification_timeout_seconds: float | None = None,
        failure_context: AudioFailureContext | None = None,
        failed_operation: str = "unknown",
        failed_check: str = "unknown",
    ) -> None:
        if epoch != self._asr_session_epoch or (
            expected_identity is not None
            and not self._runtime_identity_matches(expected_identity)
        ):
            return
        if failure_context is None:
            failure_context = self._new_audio_failure_context(failed_operation)
        if failure_context.detected_at is None:
            failure_context.fail(
                failed_check, actual=self._audio_failure_scalars(), send_state="unknown",
            )
        # The provider callback that reported failure must not be allowed to
        # deliver a queued final into the surviving Omni session.
        self._asr_session_epoch += 1
        failure_epoch = self._asr_session_epoch
        self._asr_audio_generation += 1
        try:
            explicit_reason = str(reason_code).strip()
        except Exception:
            explicit_reason = ""
        try:
            status_reason = str(status_code).strip()
        except Exception:
            status_reason = ""
        effective_reason = (
            explicit_reason
            if _ASR_REASON_CODE_FULL_RE.fullmatch(explicit_reason) is not None
            else (
                status_reason
                if _ASR_REASON_CODE_FULL_RE.fullmatch(status_reason) is not None
                else "ASR_INDEPENDENT_FAILED"
            )
        )
        incident_id = f"asr-failure-{uuid.uuid4().hex}"
        transcript_dispatcher = self._asr_transcript_dispatcher
        self._schedule_asr_incident_log(
            incident_id=incident_id,
            reason_code=effective_reason,
            stage="blocked",
            source_session_epoch=epoch,
            failure_context=failure_context,
        )
        detector_dispatcher = self._asr_detector_dispatcher
        audio_dispatcher = self._asr_audio_dispatcher
        admission_cleanup = None
        admission_owner_settled = False

        def admission_settled(done):
            nonlocal admission_owner_settled
            admission_owner_settled = True
            trace.mark(
                "admission",
                "cancelled" if done.cancelled()
                else "failed" if done.exception() is not None
                else "completed",
            )
            trace.record("completed", stage="background_cleanup_completed")

        if self._asr_admission_ingress_started:
            admission_cleanup = self._finish_admission_invalidation(
                self._asr_admission_ingress.invalidate_all_nowait(RouteReplaced()),
                transcript_dispatcher,
                self._asr_provider_correlator,
                self._asr_provider_correlator_namespace,
                self._asr_detector,
                on_settled=admission_settled,
            )
        else:
            transcript_dispatcher.invalidate_all()
        detector_dispatcher.invalidate_all()
        audio_dispatcher.abort()
        self._asr_transcript_dispatcher = self._new_asr_transcript_dispatcher()
        self._asr_detector_dispatcher = AsrDetectorDispatcher(
            self._dispatch_asr_detector_event,
            on_failure=self._handle_asr_detector_dispatcher_failure,
        )
        self._asr_audio_dispatcher = AsrAudioDispatcher(
            validator=self._asr_audio_command_is_valid,
            on_wire_audio=self._record_asr_dispatcher_wire_audio,
            on_failure=self._handle_asr_audio_dispatcher_failure,
        )
        asr_session, self._asr_session = self._asr_session, None
        lifecycle, self._asr_lifecycle = self._asr_lifecycle, None
        detector, self._asr_detector = self._asr_detector, None
        lease, self._asr_smart_turn_lease = self._asr_smart_turn_lease, None
        self._asr_provider = None
        self._asr_session_factory = None
        self._asr_transport_selection = None
        self._asr_provider_speaker_sequence = 0
        self._asr_buffered_provider_speaker_observation = None
        self._reset_asr_turn_state()
        for task_name in (
            "_asr_transport_task",
            "_asr_warm_expiry_task",
            "_asr_final_watchdog_task",
        ):
            task = getattr(self, task_name, None)
            setattr(self, task_name, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        if lifecycle is not None:
            lifecycle.stop()
        trace = CleanupTrace(
            incident_id,
            lambda metadata: self._schedule_pipeline_cleanup(metadata, epoch),
        )
        for component in ("transport", "lease", "detector", "admission"):
            trace.mark(component, "not_required")
        for component in ("notification", "audio_dispatcher", "detector_dispatcher"):
            trace.mark(component, "pending")

        def own_cleanup(awaitable, name):
            # The worker is registered before any suspension or notification.
            # A cancellation-resistant resource cannot keep the supervisor
            # waiting forever; its real completion remains separately visible.
            trace.mark(name, "pending")
            cleanup_deadline = time.monotonic() + _ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS
            worker = asyncio.create_task(
                awaitable, name="independent-asr-failure-" + name + "-worker",
            )
            self._asr_close_tasks.add(worker)
            supervisor_finished = False

            def reaped(done):
                self._asr_close_tasks.discard(done)
                outcome = (
                    "cancelled" if done.cancelled()
                    else "failed" if done.exception() is not None
                    else "completed"
                )
                if name != "admission" or not admission_owner_settled:
                    trace.mark(name, outcome, residual=(name == "admission"))
                if supervisor_finished:
                    trace.record("completed", stage="background_cleanup_completed")

            worker.add_done_callback(reaped)

            async def bounded_close():
                nonlocal supervisor_finished
                try:
                    _, pending = await asyncio.wait(
                        {worker}, timeout=max(0.0, cleanup_deadline - time.monotonic()),
                    )
                    if pending:
                        trace.mark(name, "timed_out", residual=True)
                        worker.cancel()
                except asyncio.CancelledError:
                    trace.mark(name, "cancelled", residual=not worker.done())
                    raise
                finally:
                    supervisor_finished = True
                    trace.record("completed", stage="cleanup_component_settled")

            return self._schedule_owned_cleanup(
                bounded_close(), name="independent-asr-failure-" + name,
            )

        async def close_resource(resource, *, release=False):
            # Resolve the captured resource's method inside its worker so a
            # broken resource cannot prevent the other owners being registered.
            if release:
                await resource.release()
            else:
                await resource.close()

        if asr_session is not None:
            own_cleanup(close_resource(asr_session), "transport")
        if lease is not None:
            own_cleanup(close_resource(lease, release=True), "lease")
        if detector is not None:
            own_cleanup(close_resource(detector), "detector")
        admission_task = None
        if admission_cleanup is not None:
            admission_task = own_cleanup(admission_cleanup, "admission")
        trace.record("pending", stage="cleanup_started")
        failure_identity = self._capture_runtime_identity()
        if notification_timeout_seconds is None:
            notification_timeout_seconds = _ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS
        try:
            if admission_task is not None:
                await asyncio.shield(admission_task)
            if not self._runtime_identity_matches(failure_identity):
                trace.mark("notification", "superseded")
                return
            # Notifications remain on the caller: Core.on_failure may stop
            # the route and join owned cleanup. Owning this handler would make
            # that callback wait on itself.
            try:
                async with asyncio.timeout(notification_timeout_seconds):
                    delivered = await self._send_asr_lifecycle_state(
                        VoiceLifecycleState.BLOCKED,
                        provider=provider,
                        session_epoch=failure_epoch,
                        expected_identity=failure_identity,
                        reason_code=effective_reason,
                        incident_id=incident_id,
                        delivery_trace=trace,
                    )
            except TimeoutError:
                # A lost display notice cannot skip Core's route invalidation.
                # It has its own budget and the same post-await identity fence.
                delivered = True
                trace.mark("notification", "timed_out")
                logger.debug("[%s] exact ASR failure lifecycle notice timed out", self.display_name)
            if not delivered or not self._runtime_identity_matches(failure_identity):
                trace.mark("notification", "superseded")
                return
            try:
                async with asyncio.timeout(notification_timeout_seconds):
                    await self._callbacks.on_failure(
                        AsrFailureEvent(
                            code=status_code,
                            provider=provider,
                            session_epoch=failure_epoch,
                        )
                    )
            except Exception:
                trace.mark("notification", "failed")
                logger.debug(
                    "[%s] independent ASR failure callback failed",
                    self.display_name,
                )
            if not self._runtime_identity_matches(failure_identity):
                trace.mark("notification", "superseded")
                return
            async with asyncio.timeout(notification_timeout_seconds):
                status_delivered = await self._send_asr_status(
                    status_code,
                    provider,
                    session_epoch=failure_epoch,
                    expected_identity=failure_identity,
                    reason_code=effective_reason,
                    incident_id=incident_id,
                    delivery_trace=trace,
                )
            if not status_delivered:
                trace.mark("notification", "superseded")
            if trace.components["notification"] == "pending":
                trace.mark("notification", "completed")
        except asyncio.CancelledError:
            trace.mark("notification", "cancelled")
            raise
        except TimeoutError:
            trace.mark("notification", "timed_out")
            logger.debug(
                "[%s] exact ASR handoff failure notification timed out",
                self.display_name,
            )
        finally:
            # A dispatcher can report its own failure from inside its worker.
            # Let lifecycle/failure/status delivery finish before closing that
            # worker, otherwise close() can cancel the authoritative callback.
            transcript_dispatcher.invalidate_all()
            for dispatcher, component in (
                (detector_dispatcher, "detector_dispatcher"),
                (audio_dispatcher, "audio_dispatcher"),
            ):
                own_cleanup(close_resource(dispatcher), component)
            trace.record("completed", stage="cleanup_handler_finished")

    async def _close_asr_session(self, asr_session: Any) -> None:
        try:
            await asr_session.close()
        except Exception:
            logger.warning(
                "[%s] independent ASR background close failed",
                self.display_name,
            )

    async def _send_asr_status(
        self,
        code: str,
        provider: str,
        *,
        session_epoch: int,
        expected_identity: _AsrRuntimeIdentity,
        reason_code: str | None = None,
        incident_id: str | None = None,
        delivery_trace: CleanupTrace | None = None,
    ) -> bool:
        if (
            session_epoch != expected_identity.session_epoch
            or not self._runtime_identity_matches(expected_identity)
        ):
            return False
        self._asr_lifecycle_notification_revision += 1
        revision = self._asr_lifecycle_notification_revision
        transport_generation = expected_identity.transport_generation
        if transport_generation is None:
            transport_generation = 0
        try:
            await self._callbacks.on_status(
                AsrStatusEvent(
                    code=code,
                    provider=provider,
                    session_epoch=session_epoch,
                    transport_generation=transport_generation,
                    lifecycle_revision=revision,
                    reason_code=reason_code,
                    incident_id=incident_id,
                )
            )
        except Exception:
            if delivery_trace is not None:
                delivery_trace.mark("notification", "failed")
            logger.debug(
                "[%s] independent ASR status delivery failed",
                self.display_name,
            )
        return self._runtime_identity_matches(expected_identity)

    async def _send_exact_lifecycle_state(
        self,
        state: VoiceLifecycleState,
        *,
        provider: str,
        session_epoch: int,
        expected_identity: _AsrRuntimeIdentity,
    ) -> bool:
        """Bound optional notifications without cancelling final settlement."""

        try:
            async with asyncio.timeout(_EXACT_LIFECYCLE_NOTIFICATION_TIMEOUT_SECONDS):
                return await self._send_asr_lifecycle_state(
                    state,
                    provider=provider,
                    session_epoch=session_epoch,
                    expected_identity=expected_identity,
                )
        except TimeoutError:
            # Keep this in the settlement task: no notification child can
            # outlive stop/close. External cancellation must still propagate.
            logger.debug(
                "[%s] exact ASR lifecycle notification timed out state=%s",
                self.display_name,
                state.value,
            )
            return False

    async def _send_asr_lifecycle_state(
        self,
        state: VoiceLifecycleState,
        *,
        provider: str,
        session_epoch: int,
        expected_identity: _AsrRuntimeIdentity,
        reason_code: str | None = None,
        incident_id: str | None = None,
        delivery_trace: CleanupTrace | None = None,
    ) -> bool:
        if (
            session_epoch != expected_identity.session_epoch
            or not self._runtime_identity_matches(expected_identity)
        ):
            return False
        lifecycle = expected_identity.lifecycle
        snapshot = lifecycle.snapshot if lifecycle is not None else None
        self._asr_lifecycle_notification_revision += 1
        revision = self._asr_lifecycle_notification_revision
        transport_generation = expected_identity.transport_generation
        if transport_generation is None:
            transport_generation = 0
        if snapshot is not None:
            reason_code = reason_code or snapshot.reason_code
            incident_id = incident_id or snapshot.incident_id
        self._schedule_pipeline_session_event(
            "asr_lifecycle", session_epoch, state=state.value,
            transport_generation=transport_generation,
            endpoint_authority=lifecycle.provider_policy.endpoint_authority if lifecycle is not None else None,
            speaker_enabled=self._speaker_verifier_enforces_admission,
        )
        try:
            await self._callbacks.on_lifecycle(
                AsrLifecycleNotification(
                    state=state.value,
                    provider=provider,
                    session_epoch=session_epoch,
                    transport_generation=transport_generation,
                    lifecycle_revision=revision,
                    reason_code=reason_code,
                    incident_id=incident_id,
                )
            )
        except Exception:
            if delivery_trace is not None:
                delivery_trace.mark("notification", "failed")
            logger.debug(
                "[%s] ASR lifecycle status delivery failed",
                self.display_name,
            )
        return self._runtime_identity_matches(expected_identity)
