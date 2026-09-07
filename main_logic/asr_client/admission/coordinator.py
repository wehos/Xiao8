"""Single-writer storage around the pure admission reducer."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace

from main_logic.voice_turn.contracts import VoiceTurnToken

from .._provider_events import ProviderUtteranceKey
from ..speaker_shadow.contracts import SpeakerShadowCandidateKey
from ..speaker_evidence import EvidenceStatus
from .evidence_hold import EVIDENCE_HOLD_EVENT_TYPES
from .contracts import (
    AdmissionDisposition,
    AdmissionState,
    AdmissionBulkResult,
    AdmissionEffect,
    AdmissionResolutionTicket,
    AdmissionEvent,
    BoundaryState,
    CandidateBindingState,
    CaptureState,
    Close,
    EvidenceState,
    EvidenceDeadlineExpired,
    FinalDeadlineExpired,
    ExactIntervalActivationReceipt,
    ExactIntervalActivationResult,
    ExactIntervalAbortResult,
    ExactIntervalOutcome,
    ExactIntervalPromotionReceipt,
    ExactIntervalPromotionResult,
    ExactIntervalPromotionScope,
    ExactIntervalTransitionReceipt,
    MicroEventState,
    ProviderBindingState,
    ProviderFinalState,
    ProviderFinalReceived,
    RejectionApplyState,
    Reset,
    RouteReplaced,
    SettlementState,
    SpeakerCaptureLeaseRecord,
    SpeakerCaptureLeaseToken,
    SpeakerLeaseAbandoned,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseChildBinding,
    SpeakerLeaseEvent,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseState,
    SpeakerLeasePreparedTransition,
    SpeakerLeaseTerminalClaim,
    SpeakerLeaseTransitionOutcome,
    SpeakerLeaseTransitionReceipt,
    SpeakerLeaseUnavailable,
    SpeakerCheckpointKind,
    SpeakerHigh,
    SpeakerLow,
    SpeakerUnavailable,
    VoiceTurnAdmissionRecord,
    TurnOpened,
)
from .reducer import hold_exact_interval_final, reduce, resolve_exact_interval
from .diagnostics import resolution_diagnostics
from .speaker_leases import (
    MAX_SPEAKER_LEASE_CHILDREN,
    MAX_SPEAKER_LEASES,
    SpeakerLeaseIdentityError,
    SpeakerLeaseTerminalError,
    bind_speaker_lease_child,
    reduce_speaker_lease,
)


class AdmissionCapacityError(RuntimeError):
    """A core admission record could not be reserved without data loss."""


class AdmissionIdentityError(RuntimeError):
    """A logical turn token or one of its aliases was reused inconsistently."""


class SpeakerLeaseCapacityError(RuntimeError):
    """A live speaker lease could not be reserved without eviction."""


@dataclass(slots=True)
class _ExactIntervalRecord:
    promotion_receipt: ExactIntervalPromotionReceipt
    evidence: SpeakerCaptureLeaseRecord
    parent_before: SpeakerCaptureLeaseRecord
    parent_after: SpeakerCaptureLeaseRecord
    child_before: VoiceTurnAdmissionRecord
    child_logical_revision: int
    activation_receipt: ExactIntervalActivationReceipt | None = None
    post_started: bool = False


class VoiceTurnAdmissionCoordinator:
    """Own admission records while leaving every asynchronous effect outside."""

    def __init__(
        self,
        *,
        capacity: int = 8,
        speaker_lease_capacity: int = MAX_SPEAKER_LEASES,
        speaker_lease_child_capacity: int = MAX_SPEAKER_LEASE_CHILDREN,
        retired_speaker_lease_capacity: int = 256,
        clock: Callable[[], float] = time.monotonic,
        evidence_hold_enabled: bool = False,
    ) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if not callable(clock):
            raise TypeError("clock must be callable")
        for name, value in (
            ("speaker_lease_capacity", speaker_lease_capacity),
            ("speaker_lease_child_capacity", speaker_lease_child_capacity),
            ("retired_speaker_lease_capacity", retired_speaker_lease_capacity),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if speaker_lease_capacity > MAX_SPEAKER_LEASES:
            raise ValueError(
                f"speaker_lease_capacity cannot exceed {MAX_SPEAKER_LEASES}"
            )
        if speaker_lease_child_capacity > MAX_SPEAKER_LEASE_CHILDREN:
            raise ValueError(
                "speaker_lease_child_capacity cannot exceed "
                f"{MAX_SPEAKER_LEASE_CHILDREN}"
            )
        self._capacity = capacity
        if type(evidence_hold_enabled) is not bool:
            raise TypeError("evidence_hold_enabled must be bool")
        self._evidence_hold_enabled = evidence_hold_enabled
        self._speaker_lease_capacity = speaker_lease_capacity
        self._speaker_lease_child_capacity = speaker_lease_child_capacity
        self._retired_speaker_lease_capacity = retired_speaker_lease_capacity
        self._clock = clock
        self._records: dict[VoiceTurnToken, VoiceTurnAdmissionRecord] = {}
        self._speaker_leases: dict[
            SpeakerCaptureLeaseToken,
            SpeakerCaptureLeaseRecord,
        ] = {}
        self._speaker_candidate_bindings: dict[
            SpeakerShadowCandidateKey,
            SpeakerCaptureLeaseToken,
        ] = {}
        self._provider_speaker_lease_bindings: dict[
            ProviderUtteranceKey,
            tuple[SpeakerCaptureLeaseToken, VoiceTurnToken],
        ] = {}
        self._retired_speaker_leases: OrderedDict[
            SpeakerCaptureLeaseToken,
            None,
        ] = OrderedDict()
        self._retired_turn_high_water: dict[object, int] = {}
        self._record_generation = 0
        self._speaker_lease_record_generation = 0
        self._speaker_lease_terminal_claim_sequence = 0
        self._speaker_lease_terminal_claim_owner = object()
        self._exact_interval_sequence = 0
        self._exact_interval_owner = object()
        self._exact_interval_records: dict[object, _ExactIntervalRecord] = {}
        self._exact_interval_candidate_bindings: dict[
            SpeakerShadowCandidateKey,
            object,
        ] = {}
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def speaker_lease_capacity(self) -> int:
        return self._speaker_lease_capacity

    @property
    def speaker_lease_child_capacity(self) -> int:
        return self._speaker_lease_child_capacity

    async def open_speaker_lease(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        candidate: SpeakerShadowCandidateKey,
    ) -> SpeakerCaptureLeaseRecord:
        """Reserve one stable parent verdict identity without evicting another."""

        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if type(candidate) is not SpeakerShadowCandidateKey:
            raise TypeError("candidate must be SpeakerShadowCandidateKey")
        async with self._lock:
            existing = self._speaker_leases.get(lease_token)
            if existing is not None:
                if existing.candidate != candidate:
                    raise AdmissionIdentityError("ASR_SPEAKER_LEASE_CANDIDATE_CONFLICT")
                return existing
            if lease_token in self._retired_speaker_leases:
                raise AdmissionIdentityError("ASR_SPEAKER_LEASE_ALREADY_RETIRED")
            if candidate in self._exact_interval_candidate_bindings:
                raise AdmissionIdentityError(
                    "ASR_SPEAKER_LEASE_CANDIDATE_EXACT_INTERVAL_HELD"
                )
            existing_token = self._speaker_candidate_bindings.get(candidate)
            if existing_token is not None and existing_token != lease_token:
                raise AdmissionIdentityError(
                    "ASR_SPEAKER_LEASE_CANDIDATE_ALREADY_BOUND"
                )
            if len(self._speaker_leases) >= self._speaker_lease_capacity:
                raise SpeakerLeaseCapacityError("ASR_SPEAKER_LEASE_CAPACITY_EXHAUSTED")
            self._speaker_lease_record_generation += 1
            record = SpeakerCaptureLeaseRecord(
                lease_token=lease_token,
                record_generation=self._speaker_lease_record_generation,
                candidate=candidate,
            )
            self._speaker_leases[lease_token] = record
            self._speaker_candidate_bindings[candidate] = lease_token
            return record

    async def attach_turn_to_speaker_lease(
        self,
        turn_token: VoiceTurnToken,
        lease_token: SpeakerCaptureLeaseToken,
        provider_key: ProviderUtteranceKey,
    ) -> VoiceTurnAdmissionRecord:
        """Open and bind one Provider child atomically under the same writer."""

        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if type(provider_key) is not ProviderUtteranceKey:
            raise TypeError("provider_key must be ProviderUtteranceKey")
        async with self._lock:
            lease = self._speaker_leases.get(lease_token)
            if lease is None:
                raise KeyError(lease_token)
            if self._speaker_candidate_bindings.get(lease.candidate) != lease_token:
                raise AdmissionIdentityError("ASR_ADMISSION_ALIAS_CONFLICT")
            binding = SpeakerLeaseChildBinding(provider_key, turn_token)
            provider_binding = self._provider_speaker_lease_bindings.get(provider_key)
            expected_binding = (lease_token, turn_token)
            terminal_parent = lease.state in {
                SpeakerLeaseState.ALLOW,
                SpeakerLeaseState.UNAVAILABLE,
                SpeakerLeaseState.DENY_LATCHED,
                SpeakerLeaseState.MIXED_DENY_LATCHED,
                SpeakerLeaseState.ABANDONED,
            }
            if provider_binding not in {None, expected_binding}:
                raise AdmissionIdentityError(
                    "ASR_SPEAKER_LEASE_PROVIDER_KEY_ALREADY_BOUND"
                )
            existing = self._records.get(turn_token)
            if existing is not None:
                provider_placeholder = (
                    existing.provider_binding_state is ProviderBindingState.UNBOUND
                    and existing.provider_key is None
                )
                provider_exact = (
                    existing.provider_binding_state is ProviderBindingState.BOUND
                    and existing.provider_key == provider_key
                )
                candidate_unbound = (
                    existing.candidate_binding_state is CandidateBindingState.UNBOUND
                    and existing.speaker_candidate is None
                    and existing.speaker_lease_token is None
                    and existing.speaker_authority_generation is None
                    and existing.capture_state is CaptureState.NONE
                )
                candidate_arming = (
                    existing.candidate_binding_state is CandidateBindingState.ARMING
                    and existing.speaker_candidate is None
                    and existing.speaker_lease_token is None
                    and existing.speaker_authority_generation is not None
                    and existing.capture_state is CaptureState.NONE
                )
                candidate_exact = (
                    existing.candidate_binding_state is CandidateBindingState.BOUND
                    and existing.speaker_candidate == lease.candidate
                    and existing.speaker_lease_token == lease_token
                    and (
                        existing.capture_state is CaptureState.COLLECTING
                        or (
                            lease.state is SpeakerLeaseState.UNAVAILABLE
                            and existing.capture_state is CaptureState.UNAVAILABLE
                        )
                    )
                )
                if (
                    existing.turn_token != turn_token
                    or not (provider_placeholder or provider_exact)
                    or not (candidate_unbound or candidate_arming or candidate_exact)
                ):
                    raise AdmissionIdentityError("ASR_ADMISSION_ALIAS_CONFLICT")
                if candidate_exact:
                    if (
                        not provider_exact
                        or provider_binding != expected_binding
                        or binding not in lease.child_bindings
                        or (
                            terminal_parent
                            and not self._terminal_parent_child_is_exact(
                                existing,
                                lease,
                            )
                        )
                    ):
                        raise AdmissionIdentityError("ASR_ADMISSION_ALIAS_CONFLICT")
                    return existing
                if existing.terminal_disposition is not None:
                    raise AdmissionIdentityError(
                        "ASR_ADMISSION_TERMINAL_BINDING_CONFLICT"
                    )
                if provider_binding is not None or binding in lease.child_bindings:
                    raise AdmissionIdentityError("ASR_ADMISSION_ALIAS_CONFLICT")
            else:
                if provider_binding is not None or binding in lease.child_bindings:
                    raise AdmissionIdentityError("ASR_ADMISSION_ALIAS_CONFLICT")
                if turn_token.turn_id <= self._retired_turn_high_water.get(
                    turn_token.ingress,
                    0,
                ):
                    raise AdmissionIdentityError("ASR_ADMISSION_TURN_ALREADY_RETIRED")
                if len(self._records) >= self._capacity:
                    raise AdmissionCapacityError("ASR_ADMISSION_CAPACITY_EXHAUSTED")

            if terminal_parent:
                raise SpeakerLeaseTerminalError("ASR_SPEAKER_LEASE_TERMINAL")

            try:
                updated_lease = bind_speaker_lease_child(
                    lease,
                    binding,
                    capacity=self._speaker_lease_child_capacity,
                )
            except SpeakerLeaseIdentityError as exc:
                raise AdmissionIdentityError(str(exc)) from exc

            if existing is None:
                self._record_generation += 1
                record = VoiceTurnAdmissionRecord(
                    evidence_hold_enabled=self._evidence_hold_enabled,
                    turn_token=turn_token,
                    record_generation=self._record_generation,
                    provider_binding_state=ProviderBindingState.BOUND,
                    candidate_binding_state=CandidateBindingState.BOUND,
                    capture_state=CaptureState.COLLECTING,
                    provider_key=provider_key,
                    speaker_lease_token=lease_token,
                    speaker_candidate=lease.candidate,
                )
            else:
                record = replace(
                    existing,
                    logical_revision=existing.logical_revision + 1,
                    provider_binding_state=ProviderBindingState.BOUND,
                    candidate_binding_state=CandidateBindingState.BOUND,
                    capture_state=CaptureState.COLLECTING,
                    provider_key=provider_key,
                    speaker_lease_token=lease_token,
                    speaker_candidate=lease.candidate,
                )
            self._speaker_leases[lease_token] = updated_lease
            self._records[turn_token] = record
            self._provider_speaker_lease_bindings[provider_key] = expected_binding
            return record

    @staticmethod
    def _terminal_parent_child_is_exact(
        record: VoiceTurnAdmissionRecord,
        lease: SpeakerCaptureLeaseRecord,
    ) -> bool:
        if lease.state is SpeakerLeaseState.ALLOW:
            return bool(
                record.capture_state is CaptureState.COLLECTING
                and record.evidence_state is EvidenceState.ALLOW
                and record.last_speaker_sequence_no == 1
            )
        if lease.state is SpeakerLeaseState.UNAVAILABLE:
            return bool(
                record.capture_state is CaptureState.UNAVAILABLE
                and record.evidence_state is EvidenceState.UNAVAILABLE
                and record.rejection_apply_state is RejectionApplyState.STALE
                and record.last_speaker_sequence_no == 1
            )
        if lease.state in {
            SpeakerLeaseState.DENY_LATCHED,
            SpeakerLeaseState.MIXED_DENY_LATCHED,
        }:
            return bool(
                record.capture_state is CaptureState.COLLECTING
                and record.evidence_state is EvidenceState.DENY_LATCHED
                and record.last_speaker_sequence_no == 2
            )
        if lease.state is SpeakerLeaseState.ABANDONED:
            return record.admission_state is AdmissionState.ABANDONED
        return False

    async def detach_turn_from_speaker_lease(
        self,
        turn_token: VoiceTurnToken,
        lease_token: SpeakerCaptureLeaseToken,
        provider_key: ProviderUtteranceKey,
    ) -> bool:
        """Compensate one exact child attach before any final or side effect."""

        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if type(provider_key) is not ProviderUtteranceKey:
            raise TypeError("provider_key must be ProviderUtteranceKey")
        async with self._lock:
            lease = self._speaker_leases.get(lease_token)
            record = self._records.get(turn_token)
            binding = SpeakerLeaseChildBinding(provider_key, turn_token)
            provider_binding = self._provider_speaker_lease_bindings.get(provider_key)
            expected_provider_binding = (lease_token, turn_token)
            binding_count = (
                lease.child_bindings.count(binding) if lease is not None else 0
            )

            if record is None and provider_binding is None and binding_count == 0:
                return False
            if lease is None:
                raise AdmissionIdentityError("ASR_ADMISSION_DETACH_IDENTITY_CONFLICT")
            pending_projection_exact = bool(
                lease.state
                in {
                    SpeakerLeaseState.COLLECTING,
                    SpeakerLeaseState.FIRST_LOW,
                    SpeakerLeaseState.HIGH_SEEN,
                }
                and record is not None
                and record.capture_state is CaptureState.COLLECTING
                and record.evidence_state is EvidenceState.NONE
                and record.rejection_apply_state is RejectionApplyState.NOT_STARTED
                and record.last_speaker_sequence_no == 0
            )
            terminal_projection_exact = bool(
                record is not None
                and self._terminal_parent_child_is_exact(record, lease)
            )
            if (
                record is None
                or record.turn_token != turn_token
                or record.provider_binding_state is not ProviderBindingState.BOUND
                or record.candidate_binding_state is not CandidateBindingState.BOUND
                or record.provider_key != provider_key
                or record.speaker_lease_token != lease_token
                or record.speaker_candidate != lease.candidate
                or self._speaker_candidate_bindings.get(lease.candidate) != lease_token
                or provider_binding != expected_provider_binding
                or binding_count != 1
                or not (pending_projection_exact or terminal_projection_exact)
            ):
                raise AdmissionIdentityError("ASR_ADMISSION_DETACH_IDENTITY_CONFLICT")

            side_effect_free = bool(
                record.terminal_disposition is None
                and record.boundary_state is BoundaryState.OPEN
                and record.micro_event_state is MicroEventState.NOT_APPLICABLE
                and record.provider_final_state is ProviderFinalState.NOT_RECEIVED
                and record.admission_state is AdmissionState.RESERVED
                and record.operation_nonce_sequence == 0
                and record.core_settlement_state is SettlementState.NOT_STARTED
                and record.transport_settlement_state is SettlementState.NOT_STARTED
                and record.lifecycle_settlement_state is SettlementState.NOT_STARTED
                and record.rejection_capability is None
                and record.pending_final is None
                and record.resolution_ticket is None
                and record.capture_through_sequence_no is None
                and not record.micro_event_shadow_would_suppress
                and not record.micro_event_terminal_counted
                and record.rejection_operation_nonce is None
                and record.rejection_operation_capability_id is None
                and record.rejection_operation_owner_generation is None
                and record.rejection_operation_kind is None
                and record.revoked_rejection_ticket is None
                and record.revoked_rejection_capability is None
                and not record.pending_revocations
                and not record.revocation_degraded
                and record.namespace_poison_ticket is None
                and record.deadline_operation_nonce is None
                and not record.provider_boundary_deadline_expired
                and record.partial_settlement_disposition is None
                and not record.speaker_deny_cleanup_failed_counted
            )
            if not side_effect_free:
                raise AdmissionIdentityError("ASR_ADMISSION_DETACH_ALREADY_COMMITTED")

            self._speaker_leases[lease_token] = replace(
                lease,
                logical_revision=lease.logical_revision + 1,
                child_bindings=tuple(
                    child for child in lease.child_bindings if child != binding
                ),
            )
            self._records.pop(turn_token)
            self._provider_speaker_lease_bindings.pop(provider_key)
            return True

    @staticmethod
    def _exact_interval_promotion_failure(
        outcome: ExactIntervalOutcome,
    ) -> ExactIntervalPromotionResult:
        return ExactIntervalPromotionResult(outcome=outcome)

    @staticmethod
    def _exact_interval_activation_failure(
        outcome: ExactIntervalOutcome,
    ) -> ExactIntervalActivationResult:
        return ExactIntervalActivationResult(outcome=outcome)

    @staticmethod
    def _exact_interval_child_is_held(
        record: VoiceTurnAdmissionRecord,
        scope: ExactIntervalPromotionScope,
        parent: SpeakerCaptureLeaseRecord,
    ) -> bool:
        pending_final_exact = bool(
            (
                record.provider_final_state is ProviderFinalState.NOT_RECEIVED
                and record.pending_final is None
                and record.admission_state is AdmissionState.RESERVED
            )
            or (
                record.provider_final_state is ProviderFinalState.RECEIVED
                and record.pending_final is not None
                and record.pending_final.provider_key == scope.provider_key
                and record.admission_state is AdmissionState.PENDING
            )
        )
        return bool(
            record.turn_token == scope.turn_token
            and record.provider_binding_state is ProviderBindingState.BOUND
            and record.provider_key == scope.provider_key
            and record.candidate_binding_state is CandidateBindingState.BOUND
            and record.speaker_lease_token == scope.parent_lease_token
            and record.speaker_candidate == parent.candidate
            and record.capture_state is CaptureState.COLLECTING
            and record.evidence_state is EvidenceState.NONE
            and record.boundary_state is BoundaryState.OPEN
            and record.rejection_apply_state is RejectionApplyState.NOT_STARTED
            and record.rejection_capability is None
            and record.resolution_ticket is None
            and record.partial_settlement_disposition is None
            and record.exact_interval_hold_id is None
            and record.deadline_operation_nonce is None
            and record.operation_nonce_sequence == 0
            and record.core_settlement_state is SettlementState.NOT_STARTED
            and record.transport_settlement_state is SettlementState.NOT_STARTED
            and record.lifecycle_settlement_state is SettlementState.NOT_STARTED
            and pending_final_exact
        )

    async def promote_exact_interval_tail_child(
        self,
        scope: ExactIntervalPromotionScope,
    ) -> ExactIntervalPromotionResult:
        """Atomically move one sole tail child into an unpublished exact hold."""

        if type(scope) is not ExactIntervalPromotionScope:
            raise TypeError("scope must be ExactIntervalPromotionScope")
        async with self._lock:
            parent = self._speaker_leases.get(scope.parent_lease_token)
            child = self._records.get(scope.turn_token)
            if parent is None or child is None:
                return self._exact_interval_promotion_failure(
                    ExactIntervalOutcome.STALE
                )
            if (
                parent.record_generation != scope.parent_record_generation
                or parent.logical_revision != scope.expected_parent_logical_revision
                or child.record_generation != scope.child_record_generation
                or child.logical_revision != scope.expected_child_logical_revision
                or parent.terminal_disposition is not None
            ):
                return self._exact_interval_promotion_failure(
                    ExactIntervalOutcome.STALE
                )
            if parent.state is not scope.expected_parent_state:
                return self._exact_interval_promotion_failure(
                    ExactIntervalOutcome.CONFLICT
                )
            if (
                scope.target_candidate != parent.candidate
                and (
                    parent.state is not SpeakerLeaseState.COLLECTING
                    or parent.last_speaker_sequence_no != 0
                )
            ):
                return self._exact_interval_promotion_failure(
                    ExactIntervalOutcome.CONFLICT
                )
            binding = SpeakerLeaseChildBinding(
                scope.provider_key,
                scope.turn_token,
            )
            expected_provider_binding = (
                scope.parent_lease_token,
                scope.turn_token,
            )
            if (
                parent.child_bindings != (binding,)
                or self._speaker_candidate_bindings.get(parent.candidate)
                != scope.parent_lease_token
                or self._provider_speaker_lease_bindings.get(scope.provider_key)
                != expected_provider_binding
                or not self._exact_interval_child_is_held(child, scope, parent)
            ):
                return self._exact_interval_promotion_failure(
                    ExactIntervalOutcome.CONFLICT
                )
            target_owner = self._speaker_candidate_bindings.get(
                scope.target_candidate
            )
            successor_owner = (
                self._speaker_candidate_bindings.get(scope.successor_candidate)
                if scope.successor_candidate is not None
                else None
            )
            if (
                len(self._exact_interval_records) >= self._capacity
                or scope.target_candidate in self._exact_interval_candidate_bindings
                or target_owner not in {None, scope.parent_lease_token}
                or successor_owner not in {None, scope.parent_lease_token}
                or (
                    scope.successor_candidate is not None
                    and scope.successor_candidate
                    in self._exact_interval_candidate_bindings
                )
            ):
                return self._exact_interval_promotion_failure(
                    ExactIntervalOutcome.CONFLICT
                )

            self._exact_interval_sequence += 1
            interval_id = self._exact_interval_sequence
            token = object()
            receipt = ExactIntervalPromotionReceipt(
                interval_id=interval_id,
                scope=scope,
                _owner=self._exact_interval_owner,
                _token=token,
            )
            exact_evidence = SpeakerCaptureLeaseRecord(
                lease_token=scope.parent_lease_token,
                record_generation=parent.record_generation,
                candidate=scope.target_candidate,
                state=parent.state,
                last_speaker_sequence_no=parent.last_speaker_sequence_no,
            )
            if scope.successor_candidate is None:
                updated_parent = replace(
                    parent,
                    logical_revision=parent.logical_revision + 1,
                    state=SpeakerLeaseState.ABANDONED,
                    last_speaker_sequence_no=0,
                    terminal_sequence_no=0,
                    capture_through_sequence_no=None,
                    child_bindings=(),
                    terminal_event=SpeakerLeaseAbandoned(),
                )
            else:
                updated_parent = replace(
                    parent,
                    logical_revision=parent.logical_revision + 1,
                    candidate=scope.successor_candidate,
                    state=SpeakerLeaseState.COLLECTING,
                    last_speaker_sequence_no=0,
                    terminal_sequence_no=None,
                    capture_through_sequence_no=None,
                    child_bindings=(),
                    terminal_event=None,
                )
            updated_child = replace(
                child,
                logical_revision=child.logical_revision + 1,
                speaker_lease_token=None,
                exact_interval_hold_id=interval_id,
            )

            self._speaker_leases[scope.parent_lease_token] = updated_parent
            self._records[scope.turn_token] = updated_child
            self._provider_speaker_lease_bindings.pop(scope.provider_key)
            if (
                self._speaker_candidate_bindings.get(parent.candidate)
                == scope.parent_lease_token
            ):
                self._speaker_candidate_bindings.pop(parent.candidate)
            if scope.successor_candidate is not None:
                self._speaker_candidate_bindings[scope.successor_candidate] = (
                    scope.parent_lease_token
                )
            self._exact_interval_candidate_bindings[scope.target_candidate] = token
            self._exact_interval_records[token] = _ExactIntervalRecord(
                promotion_receipt=receipt,
                evidence=exact_evidence,
                parent_before=parent,
                parent_after=updated_parent,
                child_before=child,
                child_logical_revision=updated_child.logical_revision,
            )
            return ExactIntervalPromotionResult(
                outcome=ExactIntervalOutcome.PROMOTED,
                receipt=receipt,
            )

    async def abort_exact_interval_promotion(
        self,
        receipt: ExactIntervalPromotionReceipt,
    ) -> ExactIntervalAbortResult:
        """Roll back one unpublished promotion after Detector commit failed."""

        if type(receipt) is not ExactIntervalPromotionReceipt:
            raise TypeError("receipt must be ExactIntervalPromotionReceipt")
        async with self._lock:
            if receipt._owner is not self._exact_interval_owner:
                return ExactIntervalAbortResult(ExactIntervalOutcome.CONFLICT)
            exact = self._exact_interval_records.get(receipt._token)
            if exact is None or exact.promotion_receipt is not receipt:
                return ExactIntervalAbortResult(ExactIntervalOutcome.STALE)
            if exact.post_started:
                return ExactIntervalAbortResult(ExactIntervalOutcome.CONFLICT)

            scope = receipt.scope
            parent = self._speaker_leases.get(scope.parent_lease_token)
            child = self._records.get(scope.turn_token)
            expected_child = replace(
                exact.child_before,
                logical_revision=exact.child_before.logical_revision + 1,
                speaker_lease_token=None,
                exact_interval_hold_id=receipt.interval_id,
            )
            if exact.activation_receipt is not None:
                expected_child = replace(
                    expected_child,
                    logical_revision=expected_child.logical_revision + 1,
                    speaker_candidate=scope.target_candidate,
                    evidence_state=self._exact_interval_evidence_projection(
                        exact.evidence.state
                    ),
                    last_speaker_sequence_no=(
                        exact.evidence.last_speaker_sequence_no
                    ),
                )
            provider_binding = self._provider_speaker_lease_bindings.get(
                scope.provider_key
            )
            target_binding = self._speaker_candidate_bindings.get(
                scope.target_candidate
            )
            successor_binding = (
                self._speaker_candidate_bindings.get(scope.successor_candidate)
                if scope.successor_candidate is not None
                else None
            )
            if (
                parent != exact.parent_after
                or child != expected_child
                or provider_binding is not None
                or target_binding is not None
                or (
                    scope.successor_candidate is not None
                    and successor_binding != scope.parent_lease_token
                )
                or self._exact_interval_candidate_bindings.get(
                    scope.target_candidate
                )
                is not receipt._token
            ):
                return ExactIntervalAbortResult(ExactIntervalOutcome.CONFLICT)

            if scope.successor_candidate is not None:
                self._speaker_candidate_bindings.pop(
                    scope.successor_candidate,
                    None,
                )
            self._speaker_leases[scope.parent_lease_token] = exact.parent_before
            self._records[scope.turn_token] = exact.child_before
            self._provider_speaker_lease_bindings[scope.provider_key] = (
                scope.parent_lease_token,
                scope.turn_token,
            )
            self._speaker_candidate_bindings[exact.parent_before.candidate] = (
                scope.parent_lease_token
            )
            self._exact_interval_candidate_bindings.pop(
                scope.target_candidate,
                None,
            )
            self._exact_interval_records.pop(receipt._token, None)
            return ExactIntervalAbortResult(ExactIntervalOutcome.ABORTED)

    async def fail_exact_interval_unavailable(
        self,
        receipt: ExactIntervalPromotionReceipt | ExactIntervalActivationReceipt,
    ) -> ExactIntervalAbortResult:
        """CAS one unpublished exact hold into a fail-open ordinary child.

        This is deliberately different from promotion rollback.  Once exact
        ownership or Detector commit is uncertain, restoring the provisional
        parent would make its pre-anchor facts authoritative again.  Instead,
        retain the already-promoted parent/successor topology, retire the exact
        evidence token, and mark only the bound text child unavailable.

        A DROP fact is sticky: once exact evidence reaches a deny terminal,
        this compensation refuses to rewrite the record. Non-terminal exact
        facts may still degrade to unavailable when later proof is missing.
        """

        if not isinstance(
            receipt,
            (ExactIntervalPromotionReceipt, ExactIntervalActivationReceipt),
        ):
            raise TypeError(
                "receipt must be ExactIntervalPromotionReceipt or "
                "ExactIntervalActivationReceipt"
            )
        async with self._lock:
            if receipt._owner is not self._exact_interval_owner:
                return ExactIntervalAbortResult(ExactIntervalOutcome.CONFLICT)
            exact = self._exact_interval_records.get(receipt._token)
            if exact is None:
                return ExactIntervalAbortResult(ExactIntervalOutcome.STALE)
            if isinstance(receipt, ExactIntervalPromotionReceipt):
                receipt_matches = exact.promotion_receipt is receipt
            else:
                receipt_matches = exact.activation_receipt is receipt
            if not receipt_matches:
                return ExactIntervalAbortResult(ExactIntervalOutcome.STALE)

            evidence = exact.evidence
            if (
                evidence.terminal_disposition is AdmissionDisposition.DROP
                or evidence.state
                in {
                    SpeakerLeaseState.DENY_LATCHED,
                    SpeakerLeaseState.MIXED_DENY_LATCHED,
                }
            ):
                return ExactIntervalAbortResult(ExactIntervalOutcome.CONFLICT)

            scope = exact.promotion_receipt.scope
            child = self._records.get(scope.turn_token)
            if (
                child is None
                or child.record_generation != scope.child_record_generation
                or child.logical_revision != exact.child_logical_revision
                or child.exact_interval_hold_id
                != exact.promotion_receipt.interval_id
                or child.terminal_disposition is not None
            ):
                return ExactIntervalAbortResult(ExactIntervalOutcome.STALE)

            self._records[scope.turn_token] = replace(
                child,
                logical_revision=child.logical_revision + 1,
                speaker_candidate=scope.target_candidate,
                capture_state=CaptureState.UNAVAILABLE,
                evidence_state=EvidenceState.UNAVAILABLE,
                last_speaker_sequence_no=0,
                capture_through_sequence_no=None,
                partial_settlement_disposition=AdmissionDisposition.FORWARD,
                exact_interval_hold_id=None,
            )
            if (
                self._exact_interval_candidate_bindings.get(
                    scope.target_candidate
                )
                is receipt._token
            ):
                self._exact_interval_candidate_bindings.pop(
                    scope.target_candidate,
                    None,
                )
            self._exact_interval_records.pop(receipt._token, None)
            return ExactIntervalAbortResult(ExactIntervalOutcome.ABORTED)

    @staticmethod
    def _exact_interval_evidence_projection(
        state: SpeakerLeaseState,
    ) -> EvidenceState:
        return {
            SpeakerLeaseState.COLLECTING: EvidenceState.NONE,
            SpeakerLeaseState.FIRST_LOW: EvidenceState.FIRST_LOW,
            SpeakerLeaseState.HIGH_SEEN: EvidenceState.ALLOW,
            SpeakerLeaseState.ALLOW: EvidenceState.ALLOW,
            SpeakerLeaseState.UNAVAILABLE: EvidenceState.UNAVAILABLE,
            SpeakerLeaseState.DENY_LATCHED: EvidenceState.DENY_LATCHED,
            SpeakerLeaseState.MIXED_DENY_LATCHED: EvidenceState.DENY_LATCHED,
        }[state]

    async def activate_exact_interval(
        self,
        receipt: ExactIntervalPromotionReceipt,
    ) -> ExactIntervalActivationResult:
        """Publish transferred evidence only after Detector commit succeeded."""

        if type(receipt) is not ExactIntervalPromotionReceipt:
            raise TypeError("receipt must be ExactIntervalPromotionReceipt")
        async with self._lock:
            if receipt._owner is not self._exact_interval_owner:
                return self._exact_interval_activation_failure(
                    ExactIntervalOutcome.CONFLICT
                )
            exact = self._exact_interval_records.get(receipt._token)
            if exact is None or exact.promotion_receipt is not receipt:
                return self._exact_interval_activation_failure(
                    ExactIntervalOutcome.STALE
                )
            if exact.activation_receipt is not None:
                return self._exact_interval_activation_failure(
                    ExactIntervalOutcome.STALE
                )
            scope = receipt.scope
            child = self._records.get(scope.turn_token)
            if (
                child is None
                or child.record_generation != scope.child_record_generation
                or child.logical_revision != exact.child_logical_revision
                or child.exact_interval_hold_id != receipt.interval_id
            ):
                return self._exact_interval_activation_failure(
                    ExactIntervalOutcome.STALE
                )
            if child.terminal_disposition is not None:
                return self._exact_interval_activation_failure(
                    ExactIntervalOutcome.CONFLICT
                )
            evidence = exact.evidence
            updated_child = replace(
                child,
                logical_revision=child.logical_revision + 1,
                speaker_candidate=scope.target_candidate,
                evidence_state=self._exact_interval_evidence_projection(
                    evidence.state
                ),
                last_speaker_sequence_no=evidence.last_speaker_sequence_no,
            )
            activation = ExactIntervalActivationReceipt(
                interval_id=receipt.interval_id,
                turn_token=scope.turn_token,
                child_record_generation=scope.child_record_generation,
                _owner=self._exact_interval_owner,
                _token=receipt._token,
            )
            exact.child_logical_revision = updated_child.logical_revision
            exact.activation_receipt = activation
            self._records[scope.turn_token] = updated_child
            return ExactIntervalActivationResult(
                outcome=ExactIntervalOutcome.ACTIVATED,
                receipt=activation,
            )

    @staticmethod
    def _exact_interval_transition_failure(
        receipt: ExactIntervalActivationReceipt,
        outcome: ExactIntervalOutcome,
    ) -> ExactIntervalTransitionReceipt:
        return ExactIntervalTransitionReceipt(
            interval_id=receipt.interval_id,
            outcome=outcome,
            disposition=None,
            effects=(),
        )

    async def post_exact_interval(
        self,
        receipt: ExactIntervalActivationReceipt,
        event: SpeakerLeaseEvent | ProviderFinalReceived,
        *,
        authority_is_current: Callable[[], bool] | None = None,
    ) -> ExactIntervalTransitionReceipt:
        """Apply one local exact fact; never fan out or abort provider transport."""

        if type(receipt) is not ExactIntervalActivationReceipt:
            raise TypeError("receipt must be ExactIntervalActivationReceipt")
        if not isinstance(
            event,
            (
                ProviderFinalReceived,
                *EVIDENCE_HOLD_EVENT_TYPES,
                FinalDeadlineExpired,
                SpeakerLeaseCaptureClosed,
                SpeakerLeaseHigh,
                SpeakerLeaseLow,
                SpeakerLeaseUnavailable,
            ),
        ):
            raise TypeError("event must be an exact interval fact")
        async with self._lock:
            if receipt._owner is not self._exact_interval_owner:
                return self._exact_interval_transition_failure(
                    receipt,
                    ExactIntervalOutcome.CONFLICT,
                )
            exact = self._exact_interval_records.get(receipt._token)
            if (
                exact is None
                or exact.activation_receipt is not receipt
                or exact.promotion_receipt.scope.turn_token != receipt.turn_token
            ):
                return self._exact_interval_transition_failure(
                    receipt,
                    ExactIntervalOutcome.STALE,
                )
            scope = exact.promotion_receipt.scope
            child = self._records.get(scope.turn_token)
            if (
                child is None
                or child.record_generation != scope.child_record_generation
                or child.logical_revision != exact.child_logical_revision
                or child.exact_interval_hold_id != receipt.interval_id
            ):
                return self._exact_interval_transition_failure(
                    receipt,
                    ExactIntervalOutcome.STALE,
                )
            evidence = exact.evidence
            if (
                evidence.terminal_disposition is None and authority_is_current is not None
                and not isinstance(event, (EvidenceDeadlineExpired, FinalDeadlineExpired))
            ):
                try:
                    authority_current = authority_is_current() is True
                except Exception:
                    authority_current = False
                if not authority_current:
                    # Check after taking the writer lock, not merely at enqueue.
                    # Runtime owns the unavailable/final replay transaction. A
                    # formal terminal above this boundary is never revoked.
                    return ExactIntervalTransitionReceipt(
                        interval_id=receipt.interval_id,
                        outcome=ExactIntervalOutcome.HELD,
                        disposition=None,
                        effects=(),
                    )
            exact.post_started = True

            local_effects: tuple[AdmissionEffect, ...] = ()
            if isinstance(event, (*EVIDENCE_HOLD_EVENT_TYPES, FinalDeadlineExpired)):
                child, local_effects = reduce(child, event, self._clock())
                exact.child_logical_revision = child.logical_revision
                self._records[scope.turn_token] = child
                return self._finish_exact_transition(exact, child, local_effects)

            if isinstance(event, ProviderFinalReceived):
                if event.final.provider_key != scope.provider_key:
                    return self._exact_interval_transition_failure(
                        receipt,
                        ExactIntervalOutcome.CONFLICT,
                    )
                try:
                    updated_child = hold_exact_interval_final(child, event.final)
                except ValueError:
                    return self._exact_interval_transition_failure(
                        receipt,
                        ExactIntervalOutcome.CONFLICT,
                    )
                if updated_child is not child:
                    exact.child_logical_revision = updated_child.logical_revision
                    self._records[scope.turn_token] = updated_child
                    child = updated_child
                if child.evidence_hold is not None:
                    # The exact path normally holds a final without generic
                    # scheduling. Independent evidence waiting still retires
                    # the separate 200ms operation budget.
                    from .reducer import _schedule_deadline_if_needed
                    child, local_effects = _schedule_deadline_if_needed(child)
                    exact.child_logical_revision = child.logical_revision
                    self._records[scope.turn_token] = child
            else:
                if getattr(event, "candidate", None) != scope.target_candidate:
                    return self._exact_interval_transition_failure(
                        receipt,
                        ExactIntervalOutcome.CONFLICT,
                    )
                reduced, _diagnostics = reduce_speaker_lease(evidence, event)
                if reduced is evidence:
                    if (
                        isinstance(event, SpeakerLeaseCaptureClosed)
                        and evidence.terminal_disposition is AdmissionDisposition.DROP
                        and type(event.through_sequence_no) is int
                        and event.through_sequence_no
                        == evidence.last_speaker_sequence_no
                        == evidence.terminal_sequence_no
                    ):
                        # A denied exact child can finish capture before its
                        # Provider final arrives. The reducer keeps DENY
                        # immutable; this matching lifecycle fence is a held
                        # no-op, not a stale owner that should abort the group.
                        return ExactIntervalTransitionReceipt(
                            interval_id=receipt.interval_id,
                            outcome=ExactIntervalOutcome.HELD,
                            disposition=None,
                            effects=(),
                        )
                    return self._exact_interval_transition_failure(
                        receipt,
                        ExactIntervalOutcome.STALE,
                    )
                capture_state = child.capture_state
                if reduced.state is SpeakerLeaseState.UNAVAILABLE:
                    capture_state = CaptureState.UNAVAILABLE
                elif isinstance(event, SpeakerLeaseCaptureClosed):
                    capture_state = CaptureState.CLOSED
                updated_child = replace(
                    child,
                    logical_revision=child.logical_revision + 1,
                    evidence_state=self._exact_interval_evidence_projection(
                        reduced.state
                    ),
                    capture_state=capture_state,
                    last_speaker_sequence_no=reduced.last_speaker_sequence_no,
                    capture_through_sequence_no=(
                        reduced.capture_through_sequence_no
                    ),
                )
                exact.evidence = reduced
                exact.child_logical_revision = updated_child.logical_revision
                self._records[scope.turn_token] = updated_child
                evidence = reduced
                child = updated_child

            return self._finish_exact_transition(exact, child, local_effects)

    @staticmethod
    def _evidence_key_order(record: VoiceTurnAdmissionRecord) -> tuple[int, int, int]:
        key = record.provider_key
        return (key.generation, key.buffer_epoch, key.utterance_id) if key is not None else (-1, -1, -1)

    def _with_evidence_order(self, record: VoiceTurnAdmissionRecord) -> VoiceTurnAdmissionRecord:
        if not record.evidence_hold_enabled or record.provider_key is None:
            return record
        order = self._evidence_key_order(record)
        blocked = any(
            other.turn_token != record.turn_token
            and other.turn_token.ingress == record.turn_token.ingress
            and other.provider_key is not None
            and self._evidence_key_order(other) < order
            and other.terminal_disposition is None
            and (other.evidence_hold is None or record.evidence_hold is None
                 or other.evidence_hold.binding.target_range.timeline
                 == record.evidence_hold.binding.target_range.timeline)
            for other in self._records.values()
        )
        if blocked == record.evidence_order_blocked:
            return record
        return replace(record, evidence_order_blocked=blocked,
                       logical_revision=record.logical_revision + 1)

    def _drain_evidence_successors(self) -> tuple[AdmissionEffect, ...]:
        effects: list[AdmissionEffect] = []
        for before in sorted(tuple(self._records.values()), key=self._evidence_key_order):
            child = self._records.get(before.turn_token)
            if (child is None or not child.evidence_hold_enabled
                or child.terminal_disposition is not None or child.pending_final is None):
                continue
            child = self._with_evidence_order(child)
            self._records[child.turn_token] = child
            exact = next((item for item in self._exact_interval_records.values()
                          if item.activation_receipt is not None
                          and item.promotion_receipt.scope.turn_token == child.turn_token
                          and item.activation_receipt.interval_id == child.exact_interval_hold_id), None)
            if exact is not None:
                exact.child_logical_revision = child.logical_revision
                effects.extend(self._resolve_ready_exact(exact, child).effects)
            else:
                child, emitted = reduce(child, TurnOpened(child.turn_token), self._clock())
                self._records[child.turn_token] = child
                effects.extend(emitted)
        return tuple(effects)

    def _finish_exact_transition(self, exact, child, effects):
        result = self._resolve_ready_exact(exact, child, effects)
        successors = self._drain_evidence_successors()
        return replace(result, effects=(*result.effects, *successors)) if successors else result

    def _resolve_ready_exact(
        self, exact: _ExactIntervalRecord, child: VoiceTurnAdmissionRecord,
        local_effects: tuple[AdmissionEffect, ...] = (),
    ) -> ExactIntervalTransitionReceipt:
        receipt = exact.activation_receipt
        assert receipt is not None
        child = self._with_evidence_order(child)
        self._records[child.turn_token] = child
        exact.child_logical_revision = child.logical_revision
        scope = exact.promotion_receipt.scope
        disposition = exact.evidence.terminal_disposition
        hold = child.evidence_hold
        if hold is not None and disposition is not AdmissionDisposition.DROP:
            if hold.status is EvidenceStatus.PENDING:
                disposition = None
            elif hold.status is EvidenceStatus.DENY:
                disposition = AdmissionDisposition.DROP
            else:
                disposition = AdmissionDisposition.FORWARD
        if (
            disposition
            not in {AdmissionDisposition.FORWARD, AdmissionDisposition.DROP}
            or child.pending_final is None
            or (disposition is AdmissionDisposition.FORWARD and child.evidence_order_blocked)
        ):
            return ExactIntervalTransitionReceipt(
                interval_id=receipt.interval_id,
                outcome=ExactIntervalOutcome.HELD,
                disposition=None,
                effects=local_effects,
            )
        try:
            resolved, effects = resolve_exact_interval(child, disposition)
        except ValueError:
            return self._exact_interval_transition_failure(
                receipt,
                ExactIntervalOutcome.CONFLICT,
            )
        self._records[scope.turn_token] = resolved
        self._exact_interval_records.pop(receipt._token, None)
        if (
            self._exact_interval_candidate_bindings.get(
                scope.target_candidate
            )
            is receipt._token
        ):
            self._exact_interval_candidate_bindings.pop(
                scope.target_candidate,
                None,
            )
        return ExactIntervalTransitionReceipt(
            interval_id=receipt.interval_id,
            outcome=ExactIntervalOutcome.RESOLVED,
            disposition=disposition,
            effects=(*local_effects, *effects),
        )

    def _new_speaker_lease_terminal_claim(
        self,
        record: SpeakerCaptureLeaseRecord,
        event: SpeakerLeaseEvent,
        reduced: SpeakerCaptureLeaseRecord,
    ) -> SpeakerLeaseTerminalClaim | None:
        if reduced.terminal_disposition is not AdmissionDisposition.DROP:
            return None
        if reduced.state not in {
            SpeakerLeaseState.DENY_LATCHED,
            SpeakerLeaseState.MIXED_DENY_LATCHED,
        }:
            return None
        if record.terminal_disposition is not None or reduced is record:
            return None
        self._speaker_lease_terminal_claim_sequence += 1
        return SpeakerLeaseTerminalClaim(
            lease_token=record.lease_token,
            record_generation=record.record_generation,
            expected_logical_revision=record.logical_revision,
            event=event,
            expected_terminal_state=reduced.state,
            claim_nonce=self._speaker_lease_terminal_claim_sequence,
            _owner=self._speaker_lease_terminal_claim_owner,
        )

    def _prepare_speaker_lease_transition_locked(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        event: SpeakerLeaseEvent,
        *,
        now: float,
        defer_drop_terminal: bool,
    ) -> SpeakerLeasePreparedTransition:
        record = self._speaker_leases.get(lease_token)
        if record is None:
            raise KeyError(lease_token)
        reduced, diagnostics = reduce_speaker_lease(record, event)
        if reduced.terminal_disposition is None:
            self._speaker_leases[lease_token] = reduced
            return SpeakerLeaseTransitionReceipt(
                lease_token=lease_token,
                before_state=record.state,
                after_state=reduced.state,
                outcome=(
                    SpeakerLeaseTransitionOutcome.NON_TERMINAL
                    if reduced is not record
                    else SpeakerLeaseTransitionOutcome.STALE
                ),
                terminal_sequence_no=None,
                capture_through_sequence_no=reduced.capture_through_sequence_no,
                frozen_children=(),
                child_results=(),
                diagnostics=diagnostics,
            )
        if record.terminal_disposition is not None:
            return SpeakerLeaseTransitionReceipt(
                lease_token=lease_token,
                before_state=record.state,
                after_state=record.state,
                outcome=self._terminal_speaker_event_outcome(record, event),
                terminal_sequence_no=record.terminal_sequence_no,
                capture_through_sequence_no=record.capture_through_sequence_no,
                frozen_children=record.child_bindings,
                child_results=(),
                diagnostics=diagnostics,
            )
        claim = self._new_speaker_lease_terminal_claim(record, event, reduced)
        if claim is not None:
            if defer_drop_terminal:
                return claim
            return self._commit_speaker_lease_terminal_claim_locked(claim, now=now)
        results, child_updates = self._prepare_speaker_lease_terminal_fanout(
            reduced,
            now=now,
        )
        self._speaker_leases[lease_token] = reduced
        for turn_token, child in child_updates:
            self._records[turn_token] = child
        return SpeakerLeaseTransitionReceipt(
            lease_token=lease_token,
            before_state=record.state,
            after_state=reduced.state,
            outcome=SpeakerLeaseTransitionOutcome.APPLIED,
            terminal_sequence_no=reduced.terminal_sequence_no,
            capture_through_sequence_no=reduced.capture_through_sequence_no,
            frozen_children=reduced.child_bindings,
            child_results=results,
            diagnostics=diagnostics,
            successor_effects=self._drain_evidence_successors(),
        )

    async def prepare_speaker_lease_transition(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        event: SpeakerLeaseEvent,
        *,
        now: float | None = None,
    ) -> SpeakerLeasePreparedTransition:
        """Commit ordinary facts, but stop before publishing a DROP terminal."""

        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        effective_now = self._clock() if now is None else now
        async with self._lock:
            return self._prepare_speaker_lease_transition_locked(
                lease_token,
                event,
                now=effective_now,
                defer_drop_terminal=True,
            )

    def _stale_speaker_lease_terminal_claim_receipt(
        self,
        record: SpeakerCaptureLeaseRecord,
    ) -> SpeakerLeaseTransitionReceipt:
        return SpeakerLeaseTransitionReceipt(
            lease_token=record.lease_token,
            before_state=record.state,
            after_state=record.state,
            outcome=SpeakerLeaseTransitionOutcome.STALE,
            terminal_sequence_no=record.terminal_sequence_no,
            capture_through_sequence_no=record.capture_through_sequence_no,
            frozen_children=record.child_bindings,
            child_results=(),
            diagnostics=(),
        )

    def _commit_speaker_lease_terminal_claim_locked(
        self,
        claim: SpeakerLeaseTerminalClaim,
        *,
        now: float,
    ) -> SpeakerLeaseTransitionReceipt:
        if (
            claim._owner is not self._speaker_lease_terminal_claim_owner
            or claim.claim_nonce > self._speaker_lease_terminal_claim_sequence
        ):
            raise AdmissionIdentityError("ASR_SPEAKER_LEASE_TERMINAL_CLAIM_INVALID")
        record = self._speaker_leases.get(claim.lease_token)
        if record is None:
            raise KeyError(claim.lease_token)
        if (
            record.record_generation != claim.record_generation
            or record.logical_revision != claim.expected_logical_revision
        ):
            return self._stale_speaker_lease_terminal_claim_receipt(record)
        reduced, diagnostics = reduce_speaker_lease(record, claim.event)
        if (
            reduced is record
            or reduced.logical_revision != record.logical_revision + 1
            or reduced.state is not claim.expected_terminal_state
            or reduced.terminal_disposition is not AdmissionDisposition.DROP
            or reduced.terminal_event != claim.event
        ):
            return self._stale_speaker_lease_terminal_claim_receipt(record)
        results, child_updates = self._prepare_speaker_lease_terminal_fanout(
            reduced,
            now=now,
        )
        self._speaker_leases[claim.lease_token] = reduced
        for turn_token, child in child_updates:
            self._records[turn_token] = child
        return SpeakerLeaseTransitionReceipt(
            lease_token=claim.lease_token,
            before_state=record.state,
            after_state=reduced.state,
            outcome=SpeakerLeaseTransitionOutcome.APPLIED,
            terminal_sequence_no=reduced.terminal_sequence_no,
            capture_through_sequence_no=reduced.capture_through_sequence_no,
            frozen_children=reduced.child_bindings,
            child_results=results,
            diagnostics=diagnostics,
            successor_effects=self._drain_evidence_successors(),
        )

    async def commit_speaker_lease_terminal_claim(
        self,
        claim: SpeakerLeaseTerminalClaim,
        *,
        now: float | None = None,
    ) -> SpeakerLeaseTransitionReceipt:
        """Commit one prepared DROP transition iff its exact lease revision owns."""

        if type(claim) is not SpeakerLeaseTerminalClaim:
            raise TypeError("claim must be SpeakerLeaseTerminalClaim")
        effective_now = self._clock() if now is None else now
        async with self._lock:
            return self._commit_speaker_lease_terminal_claim_locked(
                claim,
                now=effective_now,
            )

    async def post_speaker_lease(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        event: SpeakerLeaseEvent,
        *,
        now: float | None = None,
    ) -> SpeakerLeaseTransitionReceipt:
        """Reduce one parent fact and return its typed linearization receipt."""

        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        effective_now = self._clock() if now is None else now
        async with self._lock:
            result = self._prepare_speaker_lease_transition_locked(
                lease_token,
                event,
                now=effective_now,
                defer_drop_terminal=False,
            )
            assert isinstance(result, SpeakerLeaseTransitionReceipt)
            return result

    @staticmethod
    def _terminal_speaker_event_outcome(
        record: SpeakerCaptureLeaseRecord,
        event: SpeakerLeaseEvent,
    ) -> SpeakerLeaseTransitionOutcome:
        if record.state is SpeakerLeaseState.ABANDONED:
            return (
                SpeakerLeaseTransitionOutcome.IDEMPOTENT
                if isinstance(event, SpeakerLeaseAbandoned)
                else SpeakerLeaseTransitionOutcome.STALE
            )
        candidate = getattr(event, "candidate", None)
        if candidate != record.candidate:
            return SpeakerLeaseTransitionOutcome.STALE
        terminal_sequence_no = record.terminal_sequence_no
        event_sequence_no = getattr(
            event,
            "through_sequence_no",
            getattr(event, "sequence_no", None),
        )
        if event_sequence_no != terminal_sequence_no:
            return SpeakerLeaseTransitionOutcome.STALE
        if record.terminal_event is not None:
            exact = event == record.terminal_event
        else:
            # Compatibility for records constructed before exact terminal-event
            # identity was persisted. Newly reduced records always take the
            # exact-event branch above.
            exact = bool(
                (
                    record.state is SpeakerLeaseState.DENY_LATCHED
                    and isinstance(event, SpeakerLeaseLow)
                    and event.checkpoint_kind
                    in {
                        SpeakerCheckpointKind.SECOND,
                        SpeakerCheckpointKind.COMPLETION_CONFIRMATION,
                    }
                )
                or (
                    record.state is SpeakerLeaseState.ALLOW
                    and isinstance(event, SpeakerLeaseHigh)
                )
                or (
                    record.state is SpeakerLeaseState.UNAVAILABLE
                    and isinstance(event, SpeakerLeaseUnavailable)
                )
                or (
                    record.state is SpeakerLeaseState.UNAVAILABLE
                    and isinstance(event, SpeakerLeaseCaptureClosed)
                    and record.capture_through_sequence_no
                    == event.through_sequence_no
                )
            )
        return (
            SpeakerLeaseTransitionOutcome.IDEMPOTENT
            if exact
            else SpeakerLeaseTransitionOutcome.CONFLICT
        )

    def _prepare_speaker_lease_terminal_fanout(
        self,
        lease: SpeakerCaptureLeaseRecord,
        *,
        now: float,
    ) -> tuple[
        tuple[AdmissionBulkResult, ...],
        tuple[tuple[VoiceTurnToken, VoiceTurnAdmissionRecord], ...],
    ]:
        results: list[AdmissionBulkResult] = []
        updates: list[tuple[VoiceTurnToken, VoiceTurnAdmissionRecord]] = []
        for binding in lease.child_bindings:
            child = self._records.get(binding.turn_token)
            if child is None:
                continue
            child = self._with_evidence_order(child)
            events: tuple[AdmissionEvent, ...]
            if lease.state in {
                SpeakerLeaseState.DENY_LATCHED,
                SpeakerLeaseState.MIXED_DENY_LATCHED,
            }:
                next_sequence = child.last_speaker_sequence_no + 1
                if child.evidence_state is EvidenceState.FIRST_LOW:
                    events = (
                        SpeakerLow(
                            lease.candidate,
                            next_sequence,
                            SpeakerCheckpointKind.SECOND,
                        ),
                    )
                else:
                    events = (
                        SpeakerLow(
                            lease.candidate,
                            next_sequence,
                            SpeakerCheckpointKind.FIRST,
                        ),
                        SpeakerLow(
                            lease.candidate,
                            next_sequence + 1,
                            SpeakerCheckpointKind.SECOND,
                        ),
                    )
            elif lease.state is SpeakerLeaseState.ALLOW:
                events = (
                    SpeakerHigh(
                        lease.candidate,
                        child.last_speaker_sequence_no + 1,
                    ),
                )
            elif lease.state is SpeakerLeaseState.UNAVAILABLE:
                events = (
                    SpeakerUnavailable(
                        lease.candidate,
                        child.last_speaker_sequence_no + 1,
                    ),
                )
            elif lease.state is SpeakerLeaseState.ABANDONED:
                events = (RouteReplaced(),)
            else:
                continue

            effects: list[AdmissionEffect] = []
            for event in events:
                child, emitted = reduce(child, event, now)
                effects.extend(emitted)
            updates.append((binding.turn_token, child))
            results.append(
                AdmissionBulkResult(
                    binding.turn_token,
                    tuple(effects),
                    lease.lease_token,
                    lease.state,
                )
            )
        return tuple(results), tuple(updates)

    async def get_speaker_lease(
        self,
        lease_token: SpeakerCaptureLeaseToken,
    ) -> SpeakerCaptureLeaseRecord | None:
        async with self._lock:
            return self._speaker_leases.get(lease_token)

    async def live_speaker_lease_tokens(
        self,
    ) -> tuple[SpeakerCaptureLeaseToken, ...]:
        async with self._lock:
            return tuple(self._speaker_leases)

    async def retire_speaker_lease(
        self,
        lease_token: SpeakerCaptureLeaseToken,
    ) -> bool:
        """Retire only a terminal parent whose child records are all gone."""

        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        async with self._lock:
            record = self._speaker_leases.get(lease_token)
            if record is None:
                return False
            if record.terminal_disposition is None or any(
                binding.turn_token in self._records for binding in record.child_bindings
            ):
                return False
            self._speaker_leases.pop(lease_token, None)
            if self._speaker_candidate_bindings.get(record.candidate) == lease_token:
                self._speaker_candidate_bindings.pop(record.candidate, None)
            for binding in record.child_bindings:
                expected = (lease_token, binding.turn_token)
                if (
                    self._provider_speaker_lease_bindings.get(binding.provider_key)
                    == expected
                ):
                    self._provider_speaker_lease_bindings.pop(
                        binding.provider_key,
                        None,
                    )
            self._retired_speaker_leases[lease_token] = None
            while (
                len(self._retired_speaker_leases) > self._retired_speaker_lease_capacity
            ):
                self._retired_speaker_leases.popitem(last=False)
            return True

    async def open_turn(
        self,
        turn_token: VoiceTurnToken,
        *,
        provider_key: ProviderUtteranceKey | None = None,
        speaker_candidate: SpeakerShadowCandidateKey | None = None,
    ) -> VoiceTurnAdmissionRecord:
        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        async with self._lock:
            existing = self._records.get(turn_token)
            if existing is not None:
                if (
                    provider_key is not None and existing.provider_key != provider_key
                ) or (
                    speaker_candidate is not None
                    and existing.speaker_candidate != speaker_candidate
                ):
                    raise AdmissionIdentityError("ASR_ADMISSION_ALIAS_CONFLICT")
                return existing
            if turn_token.turn_id <= self._retired_turn_high_water.get(
                turn_token.ingress,
                0,
            ):
                raise AdmissionIdentityError("ASR_ADMISSION_TURN_ALREADY_RETIRED")
            if len(self._records) >= self._capacity:
                raise AdmissionCapacityError("ASR_ADMISSION_CAPACITY_EXHAUSTED")
            self._record_generation += 1
            record = VoiceTurnAdmissionRecord(
                turn_token=turn_token,
                record_generation=self._record_generation,
                evidence_hold_enabled=self._evidence_hold_enabled,
                provider_binding_state=(
                    ProviderBindingState.BOUND
                    if provider_key is not None
                    else ProviderBindingState.UNBOUND
                ),
                candidate_binding_state=(
                    CandidateBindingState.BOUND
                    if speaker_candidate is not None
                    else CandidateBindingState.UNBOUND
                ),
                capture_state=(
                    CaptureState.COLLECTING
                    if speaker_candidate is not None
                    else CaptureState.NONE
                ),
                provider_key=provider_key,
                speaker_candidate=speaker_candidate,
            )
            self._records[turn_token] = record
            return record

    async def post(
        self,
        turn_token: VoiceTurnToken,
        event: AdmissionEvent,
        *,
        now: float | None = None,
    ) -> tuple[AdmissionEffect, ...]:
        """Reduce under the short lock and return effects without executing them."""

        async with self._lock:
            record = self._records.get(turn_token)
            if record is None:
                raise KeyError(turn_token)
            record = self._with_evidence_order(record)
            reduced, effects = reduce(
                record,
                event,
                self._clock() if now is None else now,
            )
            self._records[turn_token] = reduced
            if isinstance(event, (*EVIDENCE_HOLD_EVENT_TYPES, FinalDeadlineExpired)):
                for exact in tuple(self._exact_interval_records.values()):
                    if (
                        exact.promotion_receipt.scope.turn_token == turn_token
                        and exact.activation_receipt is not None
                        and reduced.exact_interval_hold_id == exact.activation_receipt.interval_id
                    ):
                        exact.child_logical_revision = reduced.logical_revision
                        result = self._resolve_ready_exact(exact, reduced, effects)
                        return (*result.effects, *self._drain_evidence_successors())
            return (*effects, *self._drain_evidence_successors())

    def snapshot_resolution_diagnostics(
        self, ticket: AdmissionResolutionTicket,
    ) -> dict[str, str | int | bool | None]:
        """Read on the owning event loop, without yielding or exposing records.

        All writers commit synchronously while holding the writer lock. This
        diagnostic-only copy adds no await to the delivery/cleanup transaction.
        """
        return resolution_diagnostics(self._records.get(ticket.turn_token), ticket)

    async def get_record(
        self,
        turn_token: VoiceTurnToken,
    ) -> VoiceTurnAdmissionRecord | None:
        async with self._lock:
            return self._records.get(turn_token)

    async def live_turn_tokens(self) -> tuple[VoiceTurnToken, ...]:
        """Return one insertion-ordered snapshot without exposing the record table."""

        async with self._lock:
            return tuple(self._records)

    async def invalidate_all(
        self,
        event: Reset | Close | RouteReplaced,
        *,
        now: float | None = None,
    ) -> tuple[AdmissionBulkResult, ...]:
        """Reduce one route invalidation against the complete live snapshot.

        The reducer is run for every record while this coordinator remains the
        single writer.  Effects are only returned after the lock is released;
        callers execute them and post their acknowledgements through ingress.
        """

        if type(event) not in {Reset, Close, RouteReplaced}:
            raise TypeError("event must be Reset, Close, or RouteReplaced")
        effective_now = self._clock() if now is None else now
        async with self._lock:
            lease_updates: list[
                tuple[SpeakerCaptureLeaseToken, SpeakerCaptureLeaseRecord]
            ] = []
            for lease_token, lease in self._speaker_leases.items():
                reduced, _ = reduce_speaker_lease(lease, SpeakerLeaseAbandoned())
                lease_updates.append((lease_token, reduced))
            results: list[AdmissionBulkResult] = []
            record_updates: list[tuple[VoiceTurnToken, VoiceTurnAdmissionRecord]] = []
            for turn_token, record in self._records.items():
                reduced, effects = reduce(record, event, effective_now)
                record_updates.append((turn_token, reduced))
                results.append(AdmissionBulkResult(turn_token, effects))
            for lease_token, lease in lease_updates:
                self._speaker_leases[lease_token] = lease
            for turn_token, record in record_updates:
                self._records[turn_token] = record
            self._exact_interval_records.clear()
            self._exact_interval_candidate_bindings.clear()
            return tuple(results)

    async def retire(self, turn_token: VoiceTurnToken) -> bool:
        """Remove only an already-settled record; never evict live admission."""

        async with self._lock:
            record = self._records.get(turn_token)
            if record is None:
                return False
            if record.terminal_disposition is None or any(
                state not in {SettlementState.SETTLED, SettlementState.DEGRADED}
                for state in (
                    record.core_settlement_state,
                    record.transport_settlement_state,
                    record.lifecycle_settlement_state,
                )
            ):
                return False
            if record.revoked_rejection_ticket is not None:
                return False
            if record.pending_revocations:
                return False
            if record.revocation_degraded:
                return False
            if record.namespace_poison_ticket is not None:
                return False
            if record.rejection_capability is not None:
                return False
            self._records.pop(turn_token, None)
            self._retired_turn_high_water[turn_token.ingress] = max(
                self._retired_turn_high_water.get(turn_token.ingress, 0),
                turn_token.turn_id,
            )
            return True


__all__ = [
    "AdmissionBulkResult",
    "AdmissionCapacityError",
    "AdmissionIdentityError",
    "SpeakerLeaseCapacityError",
    "SpeakerLeaseTransitionOutcome",
    "SpeakerLeaseTransitionReceipt",
    "VoiceTurnAdmissionCoordinator",
]
