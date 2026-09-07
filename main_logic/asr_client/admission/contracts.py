"""Immutable facts and effects for transcript admission.

These contracts intentionally contain no tasks, futures, runtime objects, or
Detector instances.  Component adapters retain executable capabilities and
refer to them here only through bounded immutable identifiers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from main_logic.voice_turn.contracts import VoiceTurnToken

from .._provider_events import ProviderUtteranceKey
from ..speaker_shadow.contracts import SpeakerShadowCandidateKey
from ..speaker_evidence.contracts import EvidenceProof, EvidenceStatus, ProviderEvidenceBinding


class ProviderBindingState(StrEnum):
    UNBOUND = "unbound"
    BOUND = "bound"
    RETIRED = "retired"


class CandidateBindingState(StrEnum):
    UNBOUND = "unbound"
    ARMING = "arming"
    BOUND = "bound"
    RETIRED = "retired"


class BoundaryState(StrEnum):
    OPEN = "open"
    EXACT = "exact"
    UNKNOWN = "unknown"
    RETIRED = "retired"


class CaptureState(StrEnum):
    NONE = "none"
    COLLECTING = "collecting"
    CLOSED = "closed"
    UNAVAILABLE = "unavailable"


class EvidenceState(StrEnum):
    NONE = "none"
    FIRST_LOW = "first_low"
    ALLOW = "allow"
    DENY_LATCHED = "deny_latched"
    UNAVAILABLE = "unavailable"


class RejectionApplyState(StrEnum):
    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    APPLIED_ACTIVE = "applied_active"
    APPLIED_SEALED = "applied_sealed"
    STALE = "stale"
    FAILED = "failed"


class MicroEventState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    ALLOW = "allow"
    SUPPRESS = "suppress"
    UNAVAILABLE = "unavailable"


class ProviderFinalState(StrEnum):
    NOT_RECEIVED = "not_received"
    RECEIVED = "received"
    ABORTED = "aborted"
    SETTLED = "settled"


class AdmissionState(StrEnum):
    RESERVED = "reserved"
    PENDING = "pending"
    FORWARDED = "forwarded"
    DROPPED = "dropped"
    ABANDONED = "abandoned"


class SettlementState(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    SETTLED = "settled"
    DEGRADED = "degraded"


class AdmissionDisposition(StrEnum):
    FORWARD = "forward"
    DROP = "drop"
    ABANDON = "abandon"


class RejectionCapabilityKind(StrEnum):
    ACTIVE = "active"
    SEALED = "sealed"


class AdmissionOperationKind(StrEnum):
    APPLY_REJECTION = "apply_rejection"
    FINAL_DEADLINE = "final_deadline"
    EVIDENCE_DEADLINE = "evidence_deadline"
    REVOKE_CAPABILITY = "revoke_capability"
    POISON_SPEAKER_NAMESPACE = "poison_speaker_namespace"


class SpeakerCheckpointKind(StrEnum):
    FIRST = "first"
    SECOND = "second"
    COMPLETION_CONFIRMATION = "completion_confirmation"


class SpeakerLeaseState(StrEnum):
    """Authoritative verdict for one continuous physical speaker capture."""

    COLLECTING = "collecting"
    FIRST_LOW = "first_low"
    HIGH_SEEN = "high_seen"
    ALLOW = "allow"
    DENY_LATCHED = "deny_latched"
    MIXED_DENY_LATCHED = "mixed_deny_latched"
    UNAVAILABLE = "unavailable"
    ABANDONED = "abandoned"


class ExactIntervalOutcome(StrEnum):
    PROMOTED = "promoted"
    ABORTED = "aborted"
    ACTIVATED = "activated"
    HELD = "held"
    RESOLVED = "resolved"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SpeakerCaptureLeaseToken:
    """Stable identity spanning every Provider text turn in one capture."""

    session_generation: int
    start_generation: int
    transport_generation: int
    detector_epoch: int
    lease_nonce: int

    def __post_init__(self) -> None:
        for name in (
            "session_generation",
            "start_generation",
            "transport_generation",
            "detector_epoch",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.lease_nonce) is not int or self.lease_nonce < 1:
            raise ValueError("lease_nonce must be a positive integer")


@dataclass(frozen=True, slots=True)
class SpeakerLeaseChildBinding:
    """One Provider text child in immutable Provider-start order."""

    provider_key: ProviderUtteranceKey
    turn_token: VoiceTurnToken

    def __post_init__(self) -> None:
        if type(self.provider_key) is not ProviderUtteranceKey:
            raise TypeError("provider_key must be ProviderUtteranceKey")
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")


@dataclass(frozen=True, slots=True)
class SpeakerCaptureLeaseRecord:
    """Pure bounded state for one authoritative speaker verdict."""

    lease_token: SpeakerCaptureLeaseToken
    record_generation: int
    candidate: SpeakerShadowCandidateKey
    state: SpeakerLeaseState = SpeakerLeaseState.COLLECTING
    logical_revision: int = 0
    last_speaker_sequence_no: int = 0
    terminal_sequence_no: int | None = None
    capture_through_sequence_no: int | None = None
    child_bindings: tuple[SpeakerLeaseChildBinding, ...] = ()
    terminal_event: SpeakerLeaseEvent | None = None

    def __post_init__(self) -> None:
        if type(self.lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if type(self.record_generation) is not int or self.record_generation < 1:
            raise ValueError("record_generation must be a positive integer")
        if type(self.candidate) is not SpeakerShadowCandidateKey:
            raise TypeError("candidate must be SpeakerShadowCandidateKey")
        if type(self.state) is not SpeakerLeaseState:
            raise TypeError("state must be SpeakerLeaseState")
        for name in ("logical_revision", "last_speaker_sequence_no"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.capture_through_sequence_no is not None and (
            type(self.capture_through_sequence_no) is not int
            or self.capture_through_sequence_no < 0
        ):
            raise ValueError(
                "capture_through_sequence_no must be a non-negative integer or None"
            )
        if self.terminal_sequence_no is not None and (
            type(self.terminal_sequence_no) is not int or self.terminal_sequence_no < 0
        ):
            raise ValueError(
                "terminal_sequence_no must be a non-negative integer or None"
            )
        if (
            self.state
            in {
                SpeakerLeaseState.COLLECTING,
                SpeakerLeaseState.FIRST_LOW,
                SpeakerLeaseState.HIGH_SEEN,
            }
            and self.terminal_sequence_no is not None
        ):
            raise ValueError("pending speaker lease cannot have a terminal fence")
        if (
            self.state
            not in {
                SpeakerLeaseState.COLLECTING,
                SpeakerLeaseState.FIRST_LOW,
                SpeakerLeaseState.HIGH_SEEN,
            }
            and self.terminal_sequence_no is None
        ):
            raise ValueError("terminal speaker lease requires a terminal fence")
        pending = self.state in {
            SpeakerLeaseState.COLLECTING,
            SpeakerLeaseState.FIRST_LOW,
            SpeakerLeaseState.HIGH_SEEN,
        }
        if pending and self.terminal_event is not None:
            raise ValueError("pending speaker lease cannot have a terminal event")
        if self.terminal_event is not None and not isinstance(
            self.terminal_event,
            (
                SpeakerLeaseAbandoned,
                SpeakerLeaseCaptureClosed,
                SpeakerLeaseHigh,
                SpeakerLeaseLow,
                SpeakerLeaseUnavailable,
            ),
        ):
            raise TypeError("terminal_event must be SpeakerLeaseEvent or None")
        keys = tuple(binding.provider_key for binding in self.child_bindings)
        turns = tuple(binding.turn_token for binding in self.child_bindings)
        if len(set(keys)) != len(keys) or len(set(turns)) != len(turns):
            raise ValueError("speaker lease child bindings must be unique")

    @property
    def terminal_disposition(self) -> AdmissionDisposition | None:
        return {
            SpeakerLeaseState.ALLOW: AdmissionDisposition.FORWARD,
            SpeakerLeaseState.DENY_LATCHED: AdmissionDisposition.DROP,
            SpeakerLeaseState.MIXED_DENY_LATCHED: AdmissionDisposition.DROP,
            SpeakerLeaseState.UNAVAILABLE: AdmissionDisposition.FORWARD,
            SpeakerLeaseState.ABANDONED: AdmissionDisposition.ABANDON,
        }.get(self.state)


@dataclass(frozen=True, slots=True)
class SpeakerLeaseLow:
    candidate: SpeakerShadowCandidateKey
    sequence_no: int
    checkpoint_kind: SpeakerCheckpointKind


@dataclass(frozen=True, slots=True)
class SpeakerLeaseHigh:
    candidate: SpeakerShadowCandidateKey
    sequence_no: int


@dataclass(frozen=True, slots=True)
class SpeakerLeaseUnavailable:
    candidate: SpeakerShadowCandidateKey
    sequence_no: int


@dataclass(frozen=True, slots=True)
class SpeakerLeaseCaptureClosed:
    candidate: SpeakerShadowCandidateKey
    through_sequence_no: int


@dataclass(frozen=True, slots=True)
class SpeakerLeaseAbandoned:
    pass


SpeakerLeaseEvent: TypeAlias = (
    SpeakerLeaseLow
    | SpeakerLeaseHigh
    | SpeakerLeaseUnavailable
    | SpeakerLeaseCaptureClosed
    | SpeakerLeaseAbandoned
)


@dataclass(frozen=True, slots=True)
class SpeakerLeaseTerminalClaim:
    """Opaque one-shot CAS authority for a prepared DROP terminal transition."""

    lease_token: SpeakerCaptureLeaseToken
    record_generation: int
    expected_logical_revision: int
    event: SpeakerLeaseEvent
    expected_terminal_state: SpeakerLeaseState
    claim_nonce: int
    _owner: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        for name in ("record_generation", "claim_nonce"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            type(self.expected_logical_revision) is not int
            or self.expected_logical_revision < 0
        ):
            raise ValueError("expected_logical_revision must be non-negative")
        if not isinstance(
            self.event,
            (
                SpeakerLeaseAbandoned,
                SpeakerLeaseCaptureClosed,
                SpeakerLeaseHigh,
                SpeakerLeaseLow,
                SpeakerLeaseUnavailable,
            ),
        ):
            raise TypeError("event must be SpeakerLeaseEvent")
        if self.expected_terminal_state not in {
            SpeakerLeaseState.DENY_LATCHED,
            SpeakerLeaseState.MIXED_DENY_LATCHED,
        }:
            raise ValueError("terminal claim must target a DROP speaker lease state")
        if self._owner is None:
            raise ValueError("terminal claim owner is required")


@dataclass(frozen=True, slots=True)
class BoundaryProof:
    """Opaque ownership result captured before an ordered turn is bound."""

    proof_id: int
    owner_generation: int
    provider_key: ProviderUtteranceKey

    def __post_init__(self) -> None:
        if type(self.proof_id) is not int or self.proof_id < 1:
            raise ValueError("proof_id must be a positive integer")
        if type(self.owner_generation) is not int or self.owner_generation < 0:
            raise ValueError("owner_generation must be a non-negative integer")
        if type(self.provider_key) is not ProviderUtteranceKey:
            raise TypeError("provider_key must be ProviderUtteranceKey")


@dataclass(frozen=True, slots=True)
class ExactIntervalPromotionScope:
    """Exact identities required to move one sole tail child out of a lease."""

    parent_lease_token: SpeakerCaptureLeaseToken
    parent_record_generation: int
    expected_parent_logical_revision: int
    expected_parent_state: SpeakerLeaseState
    turn_token: VoiceTurnToken
    child_record_generation: int
    expected_child_logical_revision: int
    provider_key: ProviderUtteranceKey
    boundary_proof: BoundaryProof
    target_candidate: SpeakerShadowCandidateKey
    successor_candidate: SpeakerShadowCandidateKey | None

    def __post_init__(self) -> None:
        if type(self.parent_lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("parent_lease_token must be SpeakerCaptureLeaseToken")
        for name in ("parent_record_generation", "child_record_generation"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "expected_parent_logical_revision",
            "expected_child_logical_revision",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.expected_parent_state not in {
            SpeakerLeaseState.COLLECTING,
            SpeakerLeaseState.FIRST_LOW,
            SpeakerLeaseState.HIGH_SEEN,
        }:
            raise ValueError("exact interval requires a pending parent state")
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(self.provider_key) is not ProviderUtteranceKey:
            raise TypeError("provider_key must be ProviderUtteranceKey")
        if type(self.boundary_proof) is not BoundaryProof:
            raise TypeError("boundary_proof must be BoundaryProof")
        if self.boundary_proof.provider_key != self.provider_key:
            raise ValueError("boundary proof must match provider_key")
        if type(self.target_candidate) is not SpeakerShadowCandidateKey:
            raise TypeError("target_candidate must be SpeakerShadowCandidateKey")
        if self.successor_candidate is not None and (
            type(self.successor_candidate) is not SpeakerShadowCandidateKey
        ):
            raise TypeError(
                "successor_candidate must be SpeakerShadowCandidateKey or None"
            )
        if self.successor_candidate == self.target_candidate:
            raise ValueError("successor_candidate must differ from target_candidate")


@dataclass(frozen=True, slots=True)
class ExactIntervalPromotionReceipt:
    interval_id: int
    scope: ExactIntervalPromotionScope
    _owner: object = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.interval_id) is not int or self.interval_id < 1:
            raise ValueError("interval_id must be a positive integer")
        if type(self.scope) is not ExactIntervalPromotionScope:
            raise TypeError("scope must be ExactIntervalPromotionScope")
        if self._owner is None or self._token is None:
            raise ValueError("promotion receipt authority is required")


@dataclass(frozen=True, slots=True)
class ExactIntervalPromotionResult:
    outcome: ExactIntervalOutcome
    receipt: ExactIntervalPromotionReceipt | None = None

    def __post_init__(self) -> None:
        if self.outcome is ExactIntervalOutcome.PROMOTED:
            if type(self.receipt) is not ExactIntervalPromotionReceipt:
                raise ValueError("promoted result requires a receipt")
        elif self.outcome not in {
            ExactIntervalOutcome.STALE,
            ExactIntervalOutcome.CONFLICT,
        }:
            raise ValueError("invalid promotion outcome")
        elif self.receipt is not None:
            raise ValueError("failed promotion cannot carry a receipt")


@dataclass(frozen=True, slots=True)
class ExactIntervalAbortResult:
    outcome: ExactIntervalOutcome

    def __post_init__(self) -> None:
        if self.outcome not in {
            ExactIntervalOutcome.ABORTED,
            ExactIntervalOutcome.STALE,
            ExactIntervalOutcome.CONFLICT,
        }:
            raise ValueError("invalid exact interval abort outcome")


@dataclass(frozen=True, slots=True)
class ExactIntervalActivationReceipt:
    interval_id: int
    turn_token: VoiceTurnToken
    child_record_generation: int
    _owner: object = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.interval_id) is not int or self.interval_id < 1:
            raise ValueError("interval_id must be a positive integer")
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(self.child_record_generation) is not int or (
            self.child_record_generation < 1
        ):
            raise ValueError("child_record_generation must be positive")
        if self._owner is None or self._token is None:
            raise ValueError("activation receipt authority is required")


@dataclass(frozen=True, slots=True)
class ExactIntervalActivationResult:
    outcome: ExactIntervalOutcome
    receipt: ExactIntervalActivationReceipt | None = None

    def __post_init__(self) -> None:
        if self.outcome is ExactIntervalOutcome.ACTIVATED:
            if type(self.receipt) is not ExactIntervalActivationReceipt:
                raise ValueError("activated result requires a receipt")
        elif self.outcome not in {
            ExactIntervalOutcome.STALE,
            ExactIntervalOutcome.CONFLICT,
        }:
            raise ValueError("invalid activation outcome")
        elif self.receipt is not None:
            raise ValueError("failed activation cannot carry a receipt")


@dataclass(frozen=True, slots=True)
class ExactIntervalTransitionReceipt:
    interval_id: int
    outcome: ExactIntervalOutcome
    disposition: AdmissionDisposition | None
    effects: tuple[AdmissionEffect, ...]

    def __post_init__(self) -> None:
        if type(self.interval_id) is not int or self.interval_id < 1:
            raise ValueError("interval_id must be a positive integer")
        if self.outcome is ExactIntervalOutcome.RESOLVED:
            if self.disposition not in {
                AdmissionDisposition.FORWARD,
                AdmissionDisposition.DROP,
            }:
                raise ValueError("resolved interval requires local disposition")
        elif self.outcome not in {
            ExactIntervalOutcome.HELD,
            ExactIntervalOutcome.STALE,
            ExactIntervalOutcome.CONFLICT,
        }:
            raise ValueError("invalid exact interval transition outcome")
        elif self.disposition is not None:
            raise ValueError("unresolved interval cannot carry disposition")


@dataclass(frozen=True, slots=True)
class RejectionCapability:
    """Pure reference to executable rejection authority held by an adapter."""

    capability_id: int
    owner_generation: int
    kind: RejectionCapabilityKind
    turn_token: VoiceTurnToken
    candidate: SpeakerShadowCandidateKey
    provider_key: ProviderUtteranceKey | None = None

    def __post_init__(self) -> None:
        if type(self.capability_id) is not int or self.capability_id < 1:
            raise ValueError("capability_id must be a positive integer")
        if type(self.owner_generation) is not int or self.owner_generation < 0:
            raise ValueError("owner_generation must be a non-negative integer")
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(self.candidate) is not SpeakerShadowCandidateKey:
            raise TypeError("candidate must be SpeakerShadowCandidateKey")
        if (
            self.provider_key is not None
            and type(self.provider_key) is not ProviderUtteranceKey
        ):
            raise TypeError("provider_key must be ProviderUtteranceKey or None")


@dataclass(frozen=True, slots=True)
class PendingProviderFinal:
    """One logical final with its Provider-boundary budget fixed at ingress.

    ``admission_deadline`` is retained as a compatibility field for existing
    Provider adapters.  It is only the 200ms boundary/reconciliation deadline;
    it is not authority to forward while the speaker verdict is still pending.
    New admission code should use :attr:`boundary_deadline` to make that scope
    explicit.
    """

    provider_key: ProviderUtteranceKey | None
    provider: str
    text: str
    received_at: float
    admission_deadline: float

    def __post_init__(self) -> None:
        if (
            self.provider_key is not None
            and type(self.provider_key) is not ProviderUtteranceKey
        ):
            raise TypeError("provider_key must be ProviderUtteranceKey or None")
        if type(self.provider) is not str or not self.provider.strip():
            raise ValueError("provider must be a non-empty string")
        if type(self.text) is not str:
            raise TypeError("text must be a string")
        if not math.isfinite(self.received_at) or not math.isfinite(
            self.admission_deadline
        ):
            raise ValueError("final timing must be finite")
        if self.admission_deadline < self.received_at:
            raise ValueError("admission_deadline cannot precede received_at")
        if not math.isclose(
            self.admission_deadline - self.received_at,
            0.2,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("admission deadline must be exactly 200ms after receipt")

    @property
    def boundary_deadline(self) -> float:
        return self.admission_deadline


@dataclass(frozen=True, slots=True)
class AdmissionOperationTicket:
    turn_token: VoiceTurnToken
    record_generation: int
    operation_kind: AdmissionOperationKind
    operation_nonce: int
    capability_id: int | None = None
    capability_owner_generation: int | None = None
    capability_kind: RejectionCapabilityKind | None = None

    def __post_init__(self) -> None:
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(self.record_generation) is not int or self.record_generation < 1:
            raise ValueError("record_generation must be a positive integer")
        if type(self.operation_nonce) is not int or self.operation_nonce < 1:
            raise ValueError("operation_nonce must be a positive integer")
        capability_identity = (
            self.capability_id,
            self.capability_owner_generation,
            self.capability_kind,
        )
        if self.operation_kind in {
            AdmissionOperationKind.APPLY_REJECTION,
            AdmissionOperationKind.REVOKE_CAPABILITY,
        }:
            if (
                type(self.capability_id) is not int
                or self.capability_id < 1
                or type(self.capability_owner_generation) is not int
                or self.capability_owner_generation < 0
                or type(self.capability_kind) is not RejectionCapabilityKind
            ):
                raise ValueError("rejection ticket requires capability identity")
        elif capability_identity != (None, None, None):
            raise ValueError("non-rejection ticket cannot carry capability identity")


@dataclass(frozen=True, slots=True)
class EvidenceHoldRecord:
    binding: ProviderEvidenceBinding
    ticket: AdmissionOperationTicket
    first_final_received_at: float
    absolute_deadline: float
    status: EvidenceStatus = EvidenceStatus.PENDING
    proof: EvidenceProof | None = None


@dataclass(frozen=True, slots=True)
class AdmissionResolutionTicket:
    turn_token: VoiceTurnToken
    record_generation: int
    resolution_nonce: int
    disposition: AdmissionDisposition

    def __post_init__(self) -> None:
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(self.record_generation) is not int or self.record_generation < 1:
            raise ValueError("record_generation must be a positive integer")
        if type(self.resolution_nonce) is not int or self.resolution_nonce < 1:
            raise ValueError("resolution_nonce must be a positive integer")
        if type(self.disposition) is not AdmissionDisposition:
            raise TypeError("disposition must be AdmissionDisposition")


@dataclass(frozen=True, slots=True)
class PendingCapabilityRevocation:
    ticket: AdmissionOperationTicket
    capability: RejectionCapability
    degraded: bool = False

    def __post_init__(self) -> None:
        if self.ticket.operation_kind is not AdmissionOperationKind.REVOKE_CAPABILITY:
            raise ValueError("revocation requires a revoke ticket")
        if (
            self.ticket.capability_id != self.capability.capability_id
            or self.ticket.capability_owner_generation
            != self.capability.owner_generation
            or self.ticket.capability_kind is not self.capability.kind
        ):
            raise ValueError("revocation ticket capability mismatch")


@dataclass(frozen=True, slots=True)
class VoiceTurnAdmissionRecord:
    turn_token: VoiceTurnToken
    record_generation: int
    logical_revision: int = 0
    operation_nonce_sequence: int = 0

    provider_binding_state: ProviderBindingState = ProviderBindingState.UNBOUND
    candidate_binding_state: CandidateBindingState = CandidateBindingState.UNBOUND
    boundary_state: BoundaryState = BoundaryState.OPEN
    capture_state: CaptureState = CaptureState.NONE
    evidence_state: EvidenceState = EvidenceState.NONE
    rejection_apply_state: RejectionApplyState = RejectionApplyState.NOT_STARTED
    micro_event_state: MicroEventState = MicroEventState.NOT_APPLICABLE
    provider_final_state: ProviderFinalState = ProviderFinalState.NOT_RECEIVED
    admission_state: AdmissionState = AdmissionState.RESERVED
    core_settlement_state: SettlementState = SettlementState.NOT_STARTED
    transport_settlement_state: SettlementState = SettlementState.NOT_STARTED
    lifecycle_settlement_state: SettlementState = SettlementState.NOT_STARTED

    provider_key: ProviderUtteranceKey | None = None
    speaker_lease_token: SpeakerCaptureLeaseToken | None = None
    speaker_candidate: SpeakerShadowCandidateKey | None = None
    speaker_authority_generation: str | None = None
    rejection_capability: RejectionCapability | None = None
    pending_final: PendingProviderFinal | None = None
    resolution_ticket: AdmissionResolutionTicket | None = None

    last_speaker_sequence_no: int = 0
    capture_through_sequence_no: int | None = None
    micro_event_shadow_would_suppress: bool = False
    micro_event_terminal_counted: bool = False
    rejection_operation_nonce: int | None = None
    rejection_operation_capability_id: int | None = None
    rejection_operation_owner_generation: int | None = None
    rejection_operation_kind: RejectionCapabilityKind | None = None
    revoked_rejection_ticket: AdmissionOperationTicket | None = None
    revoked_rejection_capability: RejectionCapability | None = None
    pending_revocations: tuple[PendingCapabilityRevocation, ...] = ()
    revocation_degraded: bool = False
    namespace_poison_ticket: AdmissionOperationTicket | None = None
    deadline_operation_nonce: int | None = None
    provider_boundary_deadline_expired: bool = False
    partial_settlement_disposition: AdmissionDisposition | None = None
    speaker_deny_cleanup_failed_counted: bool = False
    exact_interval_hold_id: int | None = None
    evidence_hold_enabled: bool = False
    evidence_hold: EvidenceHoldRecord | None = None
    evidence_order_blocked: bool = False

    def __post_init__(self) -> None:
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(self.evidence_hold_enabled) is not bool:
            raise TypeError("evidence_hold_enabled must be bool")
        if type(self.record_generation) is not int or self.record_generation < 1:
            raise ValueError("record_generation must be a positive integer")
        for name in (
            "logical_revision",
            "operation_nonce_sequence",
            "last_speaker_sequence_no",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.micro_event_shadow_would_suppress) is not bool:
            raise TypeError("micro_event_shadow_would_suppress must be bool")
        if type(self.micro_event_terminal_counted) is not bool:
            raise TypeError("micro_event_terminal_counted must be bool")
        if type(self.provider_boundary_deadline_expired) is not bool:
            raise TypeError("provider_boundary_deadline_expired must be bool")
        if type(self.speaker_deny_cleanup_failed_counted) is not bool:
            raise TypeError("speaker_deny_cleanup_failed_counted must be bool")
        if self.exact_interval_hold_id is not None and (
            type(self.exact_interval_hold_id) is not int
            or self.exact_interval_hold_id < 1
        ):
            raise ValueError("exact_interval_hold_id must be positive or None")
        if (
            self.partial_settlement_disposition is not None
            and type(self.partial_settlement_disposition) is not AdmissionDisposition
        ):
            raise TypeError(
                "partial_settlement_disposition must be AdmissionDisposition or None"
            )
        if self.speaker_authority_generation is not None and (
            type(self.speaker_authority_generation) is not str
            or not self.speaker_authority_generation
        ):
            raise ValueError(
                "speaker_authority_generation must be a non-empty string or None"
            )
        if self.speaker_lease_token is not None and (
            type(self.speaker_lease_token) is not SpeakerCaptureLeaseToken
        ):
            raise TypeError(
                "speaker_lease_token must be SpeakerCaptureLeaseToken or None"
            )

    @property
    def terminal_disposition(self) -> AdmissionDisposition | None:
        return {
            AdmissionState.FORWARDED: AdmissionDisposition.FORWARD,
            AdmissionState.DROPPED: AdmissionDisposition.DROP,
            AdmissionState.ABANDONED: AdmissionDisposition.ABANDON,
        }.get(self.admission_state)


# Admission events.  Separate immutable payloads make invalid state/payload
# combinations unrepresentable without a large optional-field envelope.
@dataclass(frozen=True, slots=True)
class TurnOpened:
    turn_token: VoiceTurnToken


@dataclass(frozen=True, slots=True)
class ProviderBound:
    provider_key: ProviderUtteranceKey


@dataclass(frozen=True, slots=True)
class CandidateBound:
    candidate: SpeakerShadowCandidateKey
    owner_generation: str | None = None

    def __post_init__(self) -> None:
        if self.owner_generation is not None and (
            type(self.owner_generation) is not str or not self.owner_generation
        ):
            raise ValueError("owner_generation must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class SpeakerAuthorityPending:
    owner_generation: str

    def __post_init__(self) -> None:
        if type(self.owner_generation) is not str or not self.owner_generation:
            raise ValueError("owner_generation must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SpeakerAuthorityUnarmed:
    owner_generation: str

    def __post_init__(self) -> None:
        if type(self.owner_generation) is not str or not self.owner_generation:
            raise ValueError("owner_generation must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SpeakerLow:
    candidate: SpeakerShadowCandidateKey
    sequence_no: int
    checkpoint_kind: SpeakerCheckpointKind


@dataclass(frozen=True, slots=True)
class SpeakerHigh:
    candidate: SpeakerShadowCandidateKey
    sequence_no: int


@dataclass(frozen=True, slots=True)
class SpeakerUnavailable:
    candidate: SpeakerShadowCandidateKey
    sequence_no: int


@dataclass(frozen=True, slots=True)
class SpeakerAuthorityUnavailable:
    """The verifier authority disappeared without fabricating audio sequence."""

    candidate: SpeakerShadowCandidateKey


@dataclass(frozen=True, slots=True)
class CaptureClosed:
    candidate: SpeakerShadowCandidateKey
    through_sequence_no: int


@dataclass(frozen=True, slots=True)
class BoundaryExact:
    capability: RejectionCapability


@dataclass(frozen=True, slots=True)
class BoundaryUnknown:
    provider_key: ProviderUtteranceKey | None = None


@dataclass(frozen=True, slots=True)
class TurnSealed:
    capability: RejectionCapability | None = None


@dataclass(frozen=True, slots=True)
class RejectionApplied:
    ticket: AdmissionOperationTicket
    kind: RejectionCapabilityKind


@dataclass(frozen=True, slots=True)
class RejectionStale:
    ticket: AdmissionOperationTicket


@dataclass(frozen=True, slots=True)
class RejectionFailed:
    ticket: AdmissionOperationTicket


@dataclass(frozen=True, slots=True)
class CapabilityRevoked:
    ticket: AdmissionOperationTicket


@dataclass(frozen=True, slots=True)
class CapabilityRevokeFailed:
    ticket: AdmissionOperationTicket


@dataclass(frozen=True, slots=True)
class SpeakerAuthorityNamespacePoisoned:
    ticket: AdmissionOperationTicket


@dataclass(frozen=True, slots=True)
class SpeakerAuthorityNamespacePoisonFailed:
    ticket: AdmissionOperationTicket


@dataclass(frozen=True, slots=True)
class EvidenceHoldRequested:
    binding: ProviderEvidenceBinding
    first_final_received_at: float
    hard_deadline: float | None = None

    def __post_init__(self) -> None:
        if type(self.binding) is not ProviderEvidenceBinding:
            raise TypeError("binding must be ProviderEvidenceBinding")
        if not math.isfinite(self.first_final_received_at) or self.first_final_received_at < 0:
            raise ValueError("first final receipt time must be finite and non-negative")
        if self.hard_deadline is not None and not math.isfinite(self.hard_deadline):
            raise ValueError("hard deadline must be finite")


@dataclass(frozen=True, slots=True)
class EvidenceHoldResolved:
    ticket: AdmissionOperationTicket
    proof: EvidenceProof


@dataclass(frozen=True, slots=True)
class EvidenceDeadlineExpired:
    ticket: AdmissionOperationTicket
    deadline: float


@dataclass(frozen=True, slots=True)
class ProviderFinalReceived:
    final: PendingProviderFinal


@dataclass(frozen=True, slots=True)
class FinalDeadlineExpired:
    ticket: AdmissionOperationTicket
    deadline: float


@dataclass(frozen=True, slots=True)
class MicroEventPending:
    pass


@dataclass(frozen=True, slots=True)
class MicroEventAllowed:
    shadow_would_suppress: bool = False

    def __post_init__(self) -> None:
        if type(self.shadow_would_suppress) is not bool:
            raise TypeError("shadow_would_suppress must be bool")


@dataclass(frozen=True, slots=True)
class MicroEventSuppressed:
    pass


@dataclass(frozen=True, slots=True)
class MicroEventUnavailable:
    pass


@dataclass(frozen=True, slots=True)
class CoreSettled:
    ticket: AdmissionResolutionTicket
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class TransportSettled:
    ticket: AdmissionResolutionTicket
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class LifecycleSettled:
    ticket: AdmissionResolutionTicket
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class Reset:
    pass


@dataclass(frozen=True, slots=True)
class Close:
    pass


@dataclass(frozen=True, slots=True)
class RouteReplaced:
    pass


AdmissionEvent: TypeAlias = (
    TurnOpened
    | EvidenceHoldRequested
    | EvidenceHoldResolved
    | EvidenceDeadlineExpired
    | ProviderBound
    | SpeakerAuthorityPending
    | SpeakerAuthorityUnarmed
    | CandidateBound
    | SpeakerLow
    | SpeakerHigh
    | SpeakerUnavailable
    | SpeakerAuthorityUnavailable
    | CaptureClosed
    | BoundaryExact
    | BoundaryUnknown
    | TurnSealed
    | RejectionApplied
    | RejectionStale
    | RejectionFailed
    | CapabilityRevoked
    | CapabilityRevokeFailed
    | SpeakerAuthorityNamespacePoisoned
    | SpeakerAuthorityNamespacePoisonFailed
    | ProviderFinalReceived
    | FinalDeadlineExpired
    | MicroEventPending
    | MicroEventAllowed
    | MicroEventSuppressed
    | MicroEventUnavailable
    | CoreSettled
    | TransportSettled
    | LifecycleSettled
    | Reset
    | Close
    | RouteReplaced
)


@dataclass(frozen=True, slots=True)
class ApplyRejection:
    ticket: AdmissionOperationTicket
    capability: RejectionCapability
    absolute_deadline: float | None


@dataclass(frozen=True, slots=True)
class ScheduleEvidenceDeadline:
    ticket: AdmissionOperationTicket
    absolute_deadline: float


@dataclass(frozen=True, slots=True)
class ScheduleFinalDeadline:
    ticket: AdmissionOperationTicket
    absolute_deadline: float


@dataclass(frozen=True, slots=True)
class ConstrainRejectionDeadline:
    ticket: AdmissionOperationTicket
    absolute_deadline: float


@dataclass(frozen=True, slots=True)
class ResolveReserved:
    ticket: AdmissionResolutionTicket
    final: PendingProviderFinal | None

    @property
    def turn_token(self) -> VoiceTurnToken:
        return self.ticket.turn_token

    @property
    def disposition(self) -> AdmissionDisposition:
        return self.ticket.disposition


@dataclass(frozen=True, slots=True)
class SettlePartial:
    """Settle the latest quarantined partial without carrying UI or text data."""

    turn_token: VoiceTurnToken
    record_generation: int
    disposition: AdmissionDisposition

    def __post_init__(self) -> None:
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(self.record_generation) is not int or self.record_generation < 1:
            raise ValueError("record_generation must be a positive integer")
        if type(self.disposition) is not AdmissionDisposition:
            raise TypeError("disposition must be AdmissionDisposition")


@dataclass(frozen=True, slots=True)
class RevokeRejectionCapability:
    capability: RejectionCapability
    ticket: AdmissionOperationTicket | None = None


@dataclass(frozen=True, slots=True)
class AbortProviderTransport:
    """Request cleanup for the exact admission resolution that dropped a turn.

    The immutable resolution ticket remains usable after the coordinator record
    retires.  A parent lease token lets the Runtime coalesce every child abort
    from one sticky speaker denial into one transport cleanup operation.
    """

    ticket: AdmissionResolutionTicket
    speaker_lease_token: SpeakerCaptureLeaseToken | None = None

    def __post_init__(self) -> None:
        if type(self.ticket) is not AdmissionResolutionTicket:
            raise TypeError("ticket must be AdmissionResolutionTicket")
        if self.ticket.disposition is not AdmissionDisposition.DROP:
            raise ValueError("transport abort requires a DROP resolution")
        if self.speaker_lease_token is not None and (
            type(self.speaker_lease_token) is not SpeakerCaptureLeaseToken
        ):
            raise TypeError(
                "speaker_lease_token must be SpeakerCaptureLeaseToken or None"
            )

    @property
    def turn_token(self) -> VoiceTurnToken:
        return self.ticket.turn_token

    @property
    def record_generation(self) -> int:
        return self.ticket.record_generation

    @property
    def resolution_nonce(self) -> int:
        return self.ticket.resolution_nonce

    @property
    def disposition(self) -> AdmissionDisposition:
        return self.ticket.disposition


@dataclass(frozen=True, slots=True)
class PoisonSpeakerAuthorityNamespace:
    turn_token: VoiceTurnToken
    ticket: AdmissionOperationTicket | None = None


@dataclass(frozen=True, slots=True)
class CountDiagnostic:
    name: str


AdmissionEffect: TypeAlias = (
    ApplyRejection
    | ScheduleEvidenceDeadline
    | ConstrainRejectionDeadline
    | ScheduleFinalDeadline
    | ResolveReserved
    | SettlePartial
    | RevokeRejectionCapability
    | AbortProviderTransport
    | PoisonSpeakerAuthorityNamespace
    | CountDiagnostic
)


@dataclass(frozen=True, slots=True)
class AdmissionBulkResult:
    """Effects produced for one child by an atomic parent transition."""

    turn_token: VoiceTurnToken
    effects: tuple[AdmissionEffect, ...]
    speaker_lease_token: SpeakerCaptureLeaseToken | None = None
    speaker_lease_terminal_state: SpeakerLeaseState | None = None

    def __post_init__(self) -> None:
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        lease_identity = (
            self.speaker_lease_token,
            self.speaker_lease_terminal_state,
        )
        if lease_identity == (None, None):
            return
        if type(self.speaker_lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError(
                "speaker_lease_token must be SpeakerCaptureLeaseToken when present"
            )
        if type(self.speaker_lease_terminal_state) is not SpeakerLeaseState:
            raise TypeError(
                "speaker_lease_terminal_state must be SpeakerLeaseState when present"
            )
        if self.speaker_lease_terminal_state in {
            SpeakerLeaseState.COLLECTING,
            SpeakerLeaseState.FIRST_LOW,
            SpeakerLeaseState.HIGH_SEEN,
        }:
            raise ValueError("speaker lease bulk result requires a terminal state")


class SpeakerLeaseTransitionOutcome(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    NON_TERMINAL = "non_terminal"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SpeakerLeaseTransitionReceipt:
    """Typed result of one parent fact under the coordinator's writer lock."""

    lease_token: SpeakerCaptureLeaseToken
    before_state: SpeakerLeaseState
    after_state: SpeakerLeaseState
    outcome: SpeakerLeaseTransitionOutcome
    terminal_sequence_no: int | None
    capture_through_sequence_no: int | None
    frozen_children: tuple[SpeakerLeaseChildBinding, ...]
    child_results: tuple[AdmissionBulkResult, ...]
    diagnostics: tuple[AdmissionEffect, ...]
    # Other turns released by this transition retain their own effect tickets.
    # They must never be absorbed by the originating lease's DENY cleanup.
    successor_effects: tuple[AdmissionEffect, ...] = ()

    def __post_init__(self) -> None:
        if type(self.lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if type(self.before_state) is not SpeakerLeaseState:
            raise TypeError("before_state must be SpeakerLeaseState")
        if type(self.after_state) is not SpeakerLeaseState:
            raise TypeError("after_state must be SpeakerLeaseState")
        if type(self.outcome) is not SpeakerLeaseTransitionOutcome:
            raise TypeError("outcome must be SpeakerLeaseTransitionOutcome")


SpeakerLeasePreparedTransition: TypeAlias = (
    SpeakerLeaseTransitionReceipt | SpeakerLeaseTerminalClaim
)
