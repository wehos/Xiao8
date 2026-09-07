"""Pure state reduction for one logical voice turn."""

from __future__ import annotations

from ..speaker_evidence import EvidenceStatus
from .evidence_hold import EVIDENCE_HOLD_EVENT_TYPES, reduce_evidence_hold

from dataclasses import replace

from .contracts import (
    AbortProviderTransport,
    AdmissionDisposition,
    AdmissionEffect,
    AdmissionEvent,
    AdmissionOperationKind,
    AdmissionOperationTicket,
    AdmissionResolutionTicket,
    AdmissionState,
    ApplyRejection,
    BoundaryExact,
    BoundaryState,
    BoundaryUnknown,
    CandidateBindingState,
    CandidateBound,
    CapabilityRevokeFailed,
    CapabilityRevoked,
    CaptureClosed,
    CaptureState,
    Close,
    ConstrainRejectionDeadline,
    CoreSettled,
    CountDiagnostic,
    EvidenceState,
    FinalDeadlineExpired,
    LifecycleSettled,
    MicroEventAllowed,
    MicroEventPending,
    MicroEventState,
    MicroEventSuppressed,
    MicroEventUnavailable,
    PendingProviderFinal,
    PendingCapabilityRevocation,
    PoisonSpeakerAuthorityNamespace,
    ProviderBindingState,
    ProviderBound,
    ProviderFinalReceived,
    ProviderFinalState,
    RejectionApplied,
    RejectionApplyState,
    RejectionCapabilityKind,
    RejectionFailed,
    RejectionStale,
    Reset,
    ResolveReserved,
    RevokeRejectionCapability,
    RouteReplaced,
    ScheduleFinalDeadline,
    SettlePartial,
    SettlementState,
    SpeakerCheckpointKind,
    SpeakerAuthorityPending,
    SpeakerAuthorityUnarmed,
    SpeakerAuthorityUnavailable,
    SpeakerHigh,
    SpeakerLow,
    SpeakerUnavailable,
    SpeakerAuthorityNamespacePoisoned,
    SpeakerAuthorityNamespacePoisonFailed,
    TransportSettled,
    TurnOpened,
    TurnSealed,
    VoiceTurnAdmissionRecord,
)


_APPLIED_STATES = {
    RejectionApplyState.APPLIED_ACTIVE,
    RejectionApplyState.APPLIED_SEALED,
}
_TERMINAL_ADMISSION_STATES = {
    AdmissionState.FORWARDED,
    AdmissionState.DROPPED,
    AdmissionState.ABANDONED,
}
_MAX_PENDING_REVOCATIONS = 8


def _changed(
    record: VoiceTurnAdmissionRecord,
    **changes: object,
) -> VoiceTurnAdmissionRecord:
    if all(getattr(record, name) == value for name, value in changes.items()):
        return record
    return replace(
        record,
        logical_revision=record.logical_revision + 1,
        **changes,
    )


def _ticket_matches(
    record: VoiceTurnAdmissionRecord,
    ticket: AdmissionOperationTicket,
    *,
    kind: AdmissionOperationKind,
    nonce: int | None,
) -> bool:
    return bool(
        ticket.turn_token == record.turn_token
        and ticket.record_generation == record.record_generation
        and ticket.operation_kind is kind
        and nonce is not None
        and ticket.operation_nonce == nonce
    )


def _speaker_fact_is_current(
    record: VoiceTurnAdmissionRecord,
    candidate: object,
    sequence_no: object,
) -> bool:
    return bool(
        record.candidate_binding_state is CandidateBindingState.BOUND
        and record.capture_state is CaptureState.COLLECTING
        and candidate == record.speaker_candidate
        and type(sequence_no) is int
        and sequence_no == record.last_speaker_sequence_no + 1
    )


def _capability_matches_record(
    record: VoiceTurnAdmissionRecord,
    capability: object,
) -> bool:
    return bool(
        getattr(capability, "turn_token", None) == record.turn_token
        and (
            record.speaker_candidate is None
            or getattr(capability, "candidate", None) == record.speaker_candidate
        )
        and (
            record.provider_key is None
            or getattr(capability, "provider_key", None) in {None, record.provider_key}
        )
    )


def _current_rejection_ticket(
    record: VoiceTurnAdmissionRecord,
) -> AdmissionOperationTicket | None:
    if (
        record.rejection_apply_state is not RejectionApplyState.IN_FLIGHT
        or record.rejection_operation_nonce is None
        or record.rejection_operation_capability_id is None
        or record.rejection_operation_owner_generation is None
        or record.rejection_operation_kind is None
    ):
        return None
    return AdmissionOperationTicket(
        turn_token=record.turn_token,
        record_generation=record.record_generation,
        operation_kind=AdmissionOperationKind.APPLY_REJECTION,
        operation_nonce=record.rejection_operation_nonce,
        capability_id=record.rejection_operation_capability_id,
        capability_owner_generation=record.rejection_operation_owner_generation,
        capability_kind=record.rejection_operation_kind,
    )


def _revoked_inflight_changes(
    record: VoiceTurnAdmissionRecord,
) -> dict[str, object]:
    ticket = _current_rejection_ticket(record)
    capability = record.rejection_capability
    if ticket is None or capability is None:
        return {}
    return {
        "revoked_rejection_ticket": ticket,
        "revoked_rejection_capability": capability,
    }


def _start_rejection_if_ready(
    record: VoiceTurnAdmissionRecord,
    *,
    now: float,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    capability = record.rejection_capability
    if (
        record.admission_state in _TERMINAL_ADMISSION_STATES
        or record.evidence_state is not EvidenceState.DENY_LATCHED
        or record.rejection_apply_state is not RejectionApplyState.NOT_STARTED
        or capability is None
        or record.boundary_state is not BoundaryState.EXACT
        or record.provider_boundary_deadline_expired
    ):
        return record, ()
    final = record.pending_final
    if final is not None and now >= final.boundary_deadline:
        return record, ()
    nonce = record.operation_nonce_sequence + 1
    ticket = AdmissionOperationTicket(
        turn_token=record.turn_token,
        record_generation=record.record_generation,
        operation_kind=AdmissionOperationKind.APPLY_REJECTION,
        operation_nonce=nonce,
        capability_id=capability.capability_id,
        capability_owner_generation=capability.owner_generation,
        capability_kind=capability.kind,
    )
    record = _changed(
        record,
        operation_nonce_sequence=nonce,
        rejection_operation_nonce=nonce,
        rejection_operation_capability_id=capability.capability_id,
        rejection_operation_owner_generation=capability.owner_generation,
        rejection_operation_kind=capability.kind,
        rejection_apply_state=RejectionApplyState.IN_FLIGHT,
    )
    return record, (
        ApplyRejection(
            ticket=ticket,
            capability=capability,
            absolute_deadline=(final.boundary_deadline if final is not None else None),
        ),
    )


def _resolve(
    record: VoiceTurnAdmissionRecord,
    disposition: AdmissionDisposition,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    if record.admission_state in _TERMINAL_ADMISSION_STATES:
        return record, ()
    next_state = {
        AdmissionDisposition.FORWARD: AdmissionState.FORWARDED,
        AdmissionDisposition.DROP: AdmissionState.DROPPED,
        AdmissionDisposition.ABANDON: AdmissionState.ABANDONED,
    }[disposition]
    nonce = record.operation_nonce_sequence + 1
    resolution_ticket = AdmissionResolutionTicket(
        turn_token=record.turn_token,
        record_generation=record.record_generation,
        resolution_nonce=nonce,
        disposition=disposition,
    )
    effects: list[AdmissionEffect] = [
        CountDiagnostic(f"admission_terminal_{disposition.value}"),
    ]
    partial_disposition = record.partial_settlement_disposition
    if partial_disposition is None:
        partial_disposition = disposition
        effects.append(
            SettlePartial(
                turn_token=record.turn_token,
                record_generation=record.record_generation,
                disposition=disposition,
            )
        )
    effects.append(
        ResolveReserved(
            ticket=resolution_ticket,
            final=record.pending_final,
        )
    )
    applied = record.rejection_apply_state in _APPLIED_STATES
    applied_drop = disposition is AdmissionDisposition.DROP and applied
    speaker_deny = record.evidence_state is EvidenceState.DENY_LATCHED
    if (
        disposition is AdmissionDisposition.DROP
        and speaker_deny
        and record.pending_final is not None
    ):
        effects.append(CountDiagnostic("speaker_deny_final_dropped_count"))
    capability = record.rejection_capability
    keep_rejection_authority = (
        disposition is AdmissionDisposition.DROP
        and capability is not None
        and record.rejection_apply_state is RejectionApplyState.APPLIED_ACTIVE
    )
    if capability is not None and not keep_rejection_authority:
        effects.append(RevokeRejectionCapability(capability))
    if disposition is AdmissionDisposition.DROP and (
        speaker_deny
        or record.rejection_apply_state is RejectionApplyState.APPLIED_ACTIVE
    ) and (record.evidence_hold is None or not record.provider_boundary_deadline_expired):
        effects.append(
            AbortProviderTransport(
                ticket=resolution_ticket,
                speaker_lease_token=record.speaker_lease_token,
            )
        )
    apply_state = record.rejection_apply_state
    revoked_inflight = _revoked_inflight_changes(record)
    if (
        not speaker_deny
        and not applied_drop
        and apply_state
        in {
            RejectionApplyState.NOT_STARTED,
            RejectionApplyState.IN_FLIGHT,
            RejectionApplyState.APPLIED_ACTIVE,
            RejectionApplyState.APPLIED_SEALED,
        }
    ):
        apply_state = RejectionApplyState.STALE
    final_state = record.provider_final_state
    if (
        disposition is AdmissionDisposition.DROP
        and record.rejection_apply_state is RejectionApplyState.APPLIED_ACTIVE
        and final_state is ProviderFinalState.NOT_RECEIVED
    ):
        final_state = ProviderFinalState.ABORTED
    record = _changed(
        record,
        operation_nonce_sequence=nonce,
        admission_state=next_state,
        rejection_apply_state=apply_state,
        rejection_operation_nonce=None,
        rejection_operation_capability_id=None,
        rejection_operation_owner_generation=None,
        rejection_operation_kind=None,
        deadline_operation_nonce=None,
        rejection_capability=(capability if keep_rejection_authority else None),
        provider_final_state=final_state,
        resolution_ticket=resolution_ticket,
        partial_settlement_disposition=partial_disposition,
        exact_interval_hold_id=None,
        **revoked_inflight,
    )
    return record, tuple(effects)


def _release_active_authority_if_settled(
    record: VoiceTurnAdmissionRecord,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    capability = record.rejection_capability
    settled = {SettlementState.SETTLED, SettlementState.DEGRADED}
    if (
        record.admission_state is not AdmissionState.DROPPED
        or record.rejection_apply_state is not RejectionApplyState.APPLIED_ACTIVE
        or capability is None
        or record.transport_settlement_state not in settled
        or record.lifecycle_settlement_state not in settled
    ):
        return record, ()
    return _changed(record, rejection_capability=None), (
        RevokeRejectionCapability(capability),
    )


def _count_terminal_micro_event_if_settled(
    record: VoiceTurnAdmissionRecord,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    """Count only a terminal micro-event whose turn settlement stayed valid."""

    if record.micro_event_terminal_counted or record.terminal_disposition is None:
        return record, ()
    settlements = (
        record.transport_settlement_state,
        record.lifecycle_settlement_state,
    )
    terminal = {SettlementState.SETTLED, SettlementState.DEGRADED}
    if any(state not in terminal for state in settlements):
        return record, ()
    record = _changed(record, micro_event_terminal_counted=True)
    if any(state is SettlementState.DEGRADED for state in settlements):
        return record, ()
    if (
        record.terminal_disposition is AdmissionDisposition.DROP
        and record.micro_event_state is MicroEventState.SUPPRESS
    ):
        return record, (CountDiagnostic("micro_event_suppressed_count"),)
    if (
        record.terminal_disposition is AdmissionDisposition.FORWARD
        and record.micro_event_shadow_would_suppress
    ):
        return record, (CountDiagnostic("micro_event_shadow_forward_count"),)
    return record, ()


def _rejection_can_still_be_confirmed(record: VoiceTurnAdmissionRecord) -> bool:
    """Return whether ordered speaker facts can still produce a formal deny."""

    evidence_pending = record.evidence_state in {
        EvidenceState.NONE,
        EvidenceState.FIRST_LOW,
    }
    return bool(
        evidence_pending
        and (
            record.candidate_binding_state is CandidateBindingState.ARMING
            or (
                record.candidate_binding_state is CandidateBindingState.BOUND
                and record.capture_state is CaptureState.COLLECTING
            )
        )
    )


def _settle_forward_partial_if_terminal(
    record: VoiceTurnAdmissionRecord,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    """Release a quarantined partial once speaker evidence can no longer deny."""

    if record.exact_interval_hold_id is not None or (
        record.evidence_hold is not None
        and record.evidence_hold.status in {EvidenceStatus.PENDING, EvidenceStatus.DENY}
    ):
        return record, ()
    if record.partial_settlement_disposition is not None:
        return record, ()
    speaker_allows = record.evidence_state in {
        EvidenceState.ALLOW,
        EvidenceState.UNAVAILABLE,
    }
    capture_finished_without_deny = (
        record.capture_state is CaptureState.CLOSED
        and record.evidence_state in {EvidenceState.NONE, EvidenceState.FIRST_LOW}
    )
    if not speaker_allows and not capture_finished_without_deny:
        return record, ()
    record = _changed(
        record,
        partial_settlement_disposition=AdmissionDisposition.FORWARD,
    )
    return record, (
        SettlePartial(
            turn_token=record.turn_token,
            record_generation=record.record_generation,
            disposition=AdmissionDisposition.FORWARD,
        ),
    )


def hold_exact_interval_final(
    record: VoiceTurnAdmissionRecord,
    final: PendingProviderFinal,
) -> VoiceTurnAdmissionRecord:
    """Store one exact-held final without scheduling generic resolution work."""

    if type(record) is not VoiceTurnAdmissionRecord:
        raise TypeError("record must be VoiceTurnAdmissionRecord")
    if type(final) is not PendingProviderFinal:
        raise TypeError("final must be PendingProviderFinal")
    if record.exact_interval_hold_id is None:
        raise ValueError("record is not exact-interval held")
    if record.terminal_disposition is not None:
        return record
    if final.provider_key != record.provider_key:
        raise ValueError("exact interval final provider key mismatch")
    if record.pending_final is not None:
        if record.pending_final != final:
            raise ValueError("conflicting exact interval final")
        return record
    return _changed(
        record,
        provider_final_state=ProviderFinalState.RECEIVED,
        pending_final=final,
        admission_state=AdmissionState.PENDING,
    )


def resolve_exact_interval(
    record: VoiceTurnAdmissionRecord,
    disposition: AdmissionDisposition,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    """Resolve one exact child locally without transport-wide side effects."""

    if type(record) is not VoiceTurnAdmissionRecord:
        raise TypeError("record must be VoiceTurnAdmissionRecord")
    if disposition not in {
        AdmissionDisposition.FORWARD,
        AdmissionDisposition.DROP,
    }:
        raise ValueError("exact interval disposition must be FORWARD or DROP")
    if record.exact_interval_hold_id is None:
        raise ValueError("record is not exact-interval held")
    if record.terminal_disposition is not None:
        return record, ()
    if record.pending_final is None:
        raise ValueError("exact interval resolution requires a held final")
    if (
        record.rejection_capability is not None
        or (record.rejection_apply_state is not RejectionApplyState.NOT_STARTED
            and not (record.evidence_hold is not None
                     and record.rejection_apply_state is RejectionApplyState.STALE))
        or record.partial_settlement_disposition is not None
        or record.resolution_ticket is not None
    ):
        raise ValueError("exact interval record has incompatible authority")
    nonce = record.operation_nonce_sequence + 1
    ticket = AdmissionResolutionTicket(
        turn_token=record.turn_token,
        record_generation=record.record_generation,
        resolution_nonce=nonce,
        disposition=disposition,
    )
    resolved = _changed(
        record,
        operation_nonce_sequence=nonce,
        admission_state=(
            AdmissionState.FORWARDED
            if disposition is AdmissionDisposition.FORWARD
            else AdmissionState.DROPPED
        ),
        resolution_ticket=ticket,
        partial_settlement_disposition=disposition,
        exact_interval_hold_id=None,
    )
    return resolved, (
        SettlePartial(
            turn_token=record.turn_token,
            record_generation=record.record_generation,
            disposition=disposition,
        ),
        ResolveReserved(ticket=ticket, final=record.pending_final),
    )


def maybe_resolve(
    record: VoiceTurnAdmissionRecord,
    now: float,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    """Resolve one reservation if the accumulated facts make it terminal."""

    if record.admission_state in _TERMINAL_ADMISSION_STATES:
        return record, ()
    if record.exact_interval_hold_id is not None:
        return record, ()
    if record.evidence_state is EvidenceState.DENY_LATCHED:
        return _resolve(record, AdmissionDisposition.DROP)
    hold = record.evidence_hold
    if hold is not None and hold.status is EvidenceStatus.DENY:
        return _resolve(record, AdmissionDisposition.DROP)
    final = record.pending_final
    if record.rejection_apply_state is RejectionApplyState.APPLIED_ACTIVE:
        return _resolve(record, AdmissionDisposition.DROP)
    if (
        final is not None
        and record.rejection_apply_state is RejectionApplyState.APPLIED_SEALED
    ):
        return _resolve(record, AdmissionDisposition.DROP)
    if (
        final is not None
        and final.text.strip()
        and record.micro_event_state is MicroEventState.SUPPRESS
    ):
        return _resolve(record, AdmissionDisposition.DROP)
    if final is None:
        return record, ()
    if hold is not None and hold.status is EvidenceStatus.PENDING:
        return record, ()
    if _rejection_can_still_be_confirmed(record) and not (
        hold is not None and hold.status in {EvidenceStatus.VERIFIED, EvidenceStatus.UNAVAILABLE}
    ):
        return record, ()
    if not final.text.strip():
        return _resolve(record, AdmissionDisposition.FORWARD)
    if record.micro_event_state is MicroEventState.PENDING:
        return record, ()
    if record.evidence_order_blocked:
        return record, ()
    return _resolve(record, AdmissionDisposition.FORWARD)


def _schedule_deadline_if_needed(
    record: VoiceTurnAdmissionRecord,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    final = record.pending_final
    if (
        record.admission_state is not AdmissionState.PENDING
        or final is None
        or record.deadline_operation_nonce is not None
        or record.provider_boundary_deadline_expired
    ):
        return record, ()
    nonce = record.operation_nonce_sequence + 1
    ticket = AdmissionOperationTicket(
        turn_token=record.turn_token,
        record_generation=record.record_generation,
        operation_kind=AdmissionOperationKind.FINAL_DEADLINE,
        operation_nonce=nonce,
    )
    record = _changed(
        record,
        operation_nonce_sequence=nonce,
        deadline_operation_nonce=nonce,
    )
    return record, (
        ScheduleFinalDeadline(
            ticket=ticket,
            absolute_deadline=final.boundary_deadline,
        ),
    )


def _invalidate(
    record: VoiceTurnAdmissionRecord,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    capability = record.rejection_capability
    revoked_inflight = _revoked_inflight_changes(record)
    record = _changed(
        record,
        record_generation=record.record_generation + 1,
        provider_binding_state=ProviderBindingState.RETIRED,
        candidate_binding_state=CandidateBindingState.RETIRED,
        boundary_state=BoundaryState.RETIRED,
        rejection_apply_state=(
            record.rejection_apply_state
            if record.rejection_apply_state in _APPLIED_STATES
            else RejectionApplyState.STALE
        ),
        rejection_capability=None,
        rejection_operation_nonce=None,
        rejection_operation_capability_id=None,
        rejection_operation_owner_generation=None,
        rejection_operation_kind=None,
        deadline_operation_nonce=None,
        **revoked_inflight,
    )
    record, resolve_effects = _resolve(record, AdmissionDisposition.ABANDON)
    if capability is None or any(
        isinstance(effect, RevokeRejectionCapability) for effect in resolve_effects
    ):
        return record, resolve_effects
    return record, (RevokeRejectionCapability(capability), *resolve_effects)


def _reduce_untracked(
    record: VoiceTurnAdmissionRecord,
    event: AdmissionEvent,
    now: float,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    """Apply one immutable event and return effects for execution outside locks."""

    effects: list[AdmissionEffect] = []

    if isinstance(event, (Reset, Close, RouteReplaced)):
        return _invalidate(record)

    if record.admission_state in _TERMINAL_ADMISSION_STATES and not isinstance(
        event,
        (
            RejectionApplied,
            RejectionStale,
            RejectionFailed,
            CapabilityRevoked,
            CapabilityRevokeFailed,
            SpeakerAuthorityNamespacePoisoned,
            SpeakerAuthorityNamespacePoisonFailed,
            CoreSettled,
            TransportSettled,
            LifecycleSettled,
        ),
    ):
        if isinstance(event, BoundaryExact):
            return record, (
                RevokeRejectionCapability(event.capability),
                CountDiagnostic("admission_late_boundary_ignored"),
            )
        if isinstance(event, ProviderFinalReceived):
            return record, (CountDiagnostic("admission_final_after_terminal_ignored"),)
        if isinstance(
            event,
            (
                SpeakerLow,
                SpeakerHigh,
                SpeakerUnavailable,
                SpeakerAuthorityPending,
                SpeakerAuthorityUnarmed,
                SpeakerAuthorityUnavailable,
                CaptureClosed,
            ),
        ):
            return record, (
                CountDiagnostic("admission_late_fact_ignored"),
                CountDiagnostic("speaker_late_fact_stale_count"),
            )
        return record, (CountDiagnostic("admission_late_fact_ignored"),)

    if isinstance(event, EVIDENCE_HOLD_EVENT_TYPES):
        record, hold_effects = reduce_evidence_hold(record, event, now)
        effects.extend(hold_effects)
    elif isinstance(event, TurnOpened):
        if event.turn_token != record.turn_token:
            return record, (CountDiagnostic("admission_stale_turn_opened"),)
    elif isinstance(event, ProviderBound):
        if record.provider_key is None:
            record = _changed(
                record,
                provider_binding_state=ProviderBindingState.BOUND,
                provider_key=event.provider_key,
            )
        elif record.provider_key != event.provider_key:
            return record, (CountDiagnostic("admission_provider_alias_conflict"),)
    elif isinstance(event, SpeakerAuthorityPending):
        if record.candidate_binding_state is CandidateBindingState.UNBOUND:
            record = _changed(
                record,
                candidate_binding_state=CandidateBindingState.ARMING,
                speaker_authority_generation=event.owner_generation,
            )
        elif record.candidate_binding_state is CandidateBindingState.ARMING:
            if record.speaker_authority_generation != event.owner_generation:
                record = _changed(
                    record,
                    speaker_authority_generation=event.owner_generation,
                )
                effects.append(CountDiagnostic("admission_speaker_authority_rearmed"))
        else:
            return record, (
                CountDiagnostic("admission_stale_speaker_authority_pending"),
            )
    elif isinstance(event, SpeakerAuthorityUnarmed):
        if (
            record.candidate_binding_state is CandidateBindingState.ARMING
            and record.speaker_authority_generation == event.owner_generation
        ):
            record = _changed(
                record,
                candidate_binding_state=CandidateBindingState.RETIRED,
                capture_state=CaptureState.UNAVAILABLE,
                evidence_state=(
                    EvidenceState.DENY_LATCHED
                    if record.evidence_state is EvidenceState.DENY_LATCHED
                    else EvidenceState.UNAVAILABLE
                ),
            )
        else:
            return record, (
                CountDiagnostic("admission_stale_speaker_authority_unarmed"),
            )
    elif isinstance(event, CandidateBound):
        legacy_direct_bind = (
            event.owner_generation is None
            and record.candidate_binding_state is CandidateBindingState.UNBOUND
        )
        generation_bound = (
            event.owner_generation is not None
            and record.candidate_binding_state is CandidateBindingState.ARMING
            and record.speaker_authority_generation == event.owner_generation
        )
        idempotent_bound = (
            record.candidate_binding_state is CandidateBindingState.BOUND
            and record.speaker_candidate == event.candidate
            and record.speaker_authority_generation == event.owner_generation
        )
        if idempotent_bound:
            pass
        elif legacy_direct_bind or generation_bound:
            record = _changed(
                record,
                candidate_binding_state=CandidateBindingState.BOUND,
                capture_state=CaptureState.COLLECTING,
                speaker_candidate=event.candidate,
                speaker_authority_generation=event.owner_generation,
            )
        else:
            return record, (CountDiagnostic("admission_candidate_alias_conflict"),)
    elif isinstance(event, SpeakerLow):
        if not _speaker_fact_is_current(record, event.candidate, event.sequence_no):
            return record, (
                CountDiagnostic("admission_stale_speaker_fact"),
                CountDiagnostic("speaker_late_fact_stale_count"),
            )
        evidence = record.evidence_state
        deny_latched = False
        if evidence is not EvidenceState.DENY_LATCHED:
            if (
                event.checkpoint_kind is SpeakerCheckpointKind.FIRST
                and evidence is EvidenceState.NONE
            ):
                evidence = EvidenceState.FIRST_LOW
            elif (
                event.checkpoint_kind
                in {
                    SpeakerCheckpointKind.SECOND,
                    SpeakerCheckpointKind.COMPLETION_CONFIRMATION,
                }
                and evidence is EvidenceState.FIRST_LOW
            ):
                evidence = EvidenceState.DENY_LATCHED
                deny_latched = True
            else:
                evidence = EvidenceState.UNAVAILABLE
        record = _changed(
            record,
            last_speaker_sequence_no=event.sequence_no,
            evidence_state=evidence,
        )
        if deny_latched:
            effects.append(CountDiagnostic("speaker_deny_latched_count"))
    elif isinstance(event, SpeakerHigh):
        if not _speaker_fact_is_current(record, event.candidate, event.sequence_no):
            return record, (
                CountDiagnostic("admission_stale_speaker_fact"),
                CountDiagnostic("speaker_late_fact_stale_count"),
            )
        record = _changed(
            record,
            last_speaker_sequence_no=event.sequence_no,
            evidence_state=(
                EvidenceState.DENY_LATCHED
                if record.evidence_state is EvidenceState.DENY_LATCHED
                else EvidenceState.ALLOW
            ),
        )
    elif isinstance(event, SpeakerUnavailable):
        if not _speaker_fact_is_current(record, event.candidate, event.sequence_no):
            return record, (
                CountDiagnostic("admission_stale_speaker_fact"),
                CountDiagnostic("speaker_late_fact_stale_count"),
            )
        if record.evidence_state is EvidenceState.DENY_LATCHED:
            record = _changed(
                record,
                last_speaker_sequence_no=event.sequence_no,
                capture_state=CaptureState.UNAVAILABLE,
            )
        else:
            capability = record.rejection_capability
            if capability is not None:
                effects.append(RevokeRejectionCapability(capability))
            revoked_inflight = _revoked_inflight_changes(record)
            record = _changed(
                record,
                last_speaker_sequence_no=event.sequence_no,
                capture_state=CaptureState.UNAVAILABLE,
                evidence_state=EvidenceState.UNAVAILABLE,
                rejection_apply_state=RejectionApplyState.STALE,
                rejection_capability=None,
                rejection_operation_nonce=None,
                rejection_operation_capability_id=None,
                rejection_operation_owner_generation=None,
                rejection_operation_kind=None,
                **revoked_inflight,
            )
    elif isinstance(event, SpeakerAuthorityUnavailable):
        if (
            record.candidate_binding_state is not CandidateBindingState.BOUND
            or event.candidate != record.speaker_candidate
        ):
            return record, (CountDiagnostic("admission_stale_speaker_authority"),)
        if record.evidence_state is EvidenceState.DENY_LATCHED:
            record = _changed(record, capture_state=CaptureState.UNAVAILABLE)
        else:
            capability = record.rejection_capability
            if capability is not None:
                effects.append(RevokeRejectionCapability(capability))
            revoked_inflight = _revoked_inflight_changes(record)
            record = _changed(
                record,
                capture_state=CaptureState.UNAVAILABLE,
                evidence_state=EvidenceState.UNAVAILABLE,
                rejection_apply_state=RejectionApplyState.STALE,
                rejection_capability=None,
                rejection_operation_nonce=None,
                rejection_operation_capability_id=None,
                rejection_operation_owner_generation=None,
                rejection_operation_kind=None,
                **revoked_inflight,
            )
    elif isinstance(event, CaptureClosed):
        if (
            event.candidate != record.speaker_candidate
            or type(event.through_sequence_no) is not int
            or event.through_sequence_no < record.last_speaker_sequence_no
        ):
            return record, (CountDiagnostic("admission_stale_capture_close"),)
        if (
            event.through_sequence_no > record.last_speaker_sequence_no
            and record.evidence_state is not EvidenceState.DENY_LATCHED
        ):
            capability = record.rejection_capability
            if capability is not None:
                effects.append(RevokeRejectionCapability(capability))
            revoked_inflight = _revoked_inflight_changes(record)
            record = _changed(
                record,
                capture_state=CaptureState.UNAVAILABLE,
                capture_through_sequence_no=event.through_sequence_no,
                evidence_state=EvidenceState.UNAVAILABLE,
                rejection_apply_state=RejectionApplyState.STALE,
                rejection_capability=None,
                rejection_operation_nonce=None,
                rejection_operation_capability_id=None,
                rejection_operation_owner_generation=None,
                rejection_operation_kind=None,
                **revoked_inflight,
            )
            effects.append(CountDiagnostic("admission_speaker_sequence_gap"))
        else:
            record = _changed(
                record,
                capture_state=CaptureState.CLOSED,
                capture_through_sequence_no=event.through_sequence_no,
            )
    elif isinstance(event, BoundaryExact):
        capability = event.capability
        if record.boundary_state in {BoundaryState.UNKNOWN, BoundaryState.RETIRED}:
            effects.extend(
                (
                    RevokeRejectionCapability(capability),
                    CountDiagnostic("admission_late_exact_after_unknown"),
                )
            )
        elif not _capability_matches_record(record, capability):
            effects.extend(
                (
                    RevokeRejectionCapability(capability),
                    CountDiagnostic("admission_foreign_capability_ignored"),
                )
            )
        elif (
            record.rejection_capability is not None
            and record.rejection_capability != capability
        ):
            revoked_inflight = _revoked_inflight_changes(record)
            effects.extend(
                (
                    RevokeRejectionCapability(record.rejection_capability),
                    RevokeRejectionCapability(capability),
                    CountDiagnostic("admission_capability_conflict"),
                )
            )
            record = _changed(
                record,
                boundary_state=BoundaryState.UNKNOWN,
                rejection_apply_state=RejectionApplyState.STALE,
                rejection_capability=None,
                rejection_operation_nonce=None,
                rejection_operation_capability_id=None,
                rejection_operation_owner_generation=None,
                rejection_operation_kind=None,
                **revoked_inflight,
            )
        else:
            changes: dict[str, object] = {
                "boundary_state": BoundaryState.EXACT,
                "rejection_capability": capability,
            }
            if record.speaker_candidate is None:
                changes.update(
                    candidate_binding_state=CandidateBindingState.BOUND,
                    capture_state=CaptureState.COLLECTING,
                    speaker_candidate=capability.candidate,
                )
            if record.provider_key is None and capability.provider_key is not None:
                changes.update(
                    provider_binding_state=ProviderBindingState.BOUND,
                    provider_key=capability.provider_key,
                )
            record = _changed(record, **changes)
    elif isinstance(event, BoundaryUnknown):
        if event.provider_key is not None and record.provider_key not in {
            None,
            event.provider_key,
        }:
            return record, (CountDiagnostic("admission_stale_boundary_unknown"),)
        capability = record.rejection_capability
        provider_authority_applied = (
            record.rejection_apply_state is RejectionApplyState.APPLIED_SEALED
        )
        if capability is not None and (
            provider_authority_applied
            or record.rejection_apply_state
            in {
                RejectionApplyState.NOT_STARTED,
                RejectionApplyState.IN_FLIGHT,
            }
        ):
            effects.append(RevokeRejectionCapability(capability))
        revoked_inflight = _revoked_inflight_changes(record)
        record = _changed(
            record,
            boundary_state=BoundaryState.UNKNOWN,
            rejection_capability=(
                capability
                if record.rejection_apply_state is RejectionApplyState.APPLIED_ACTIVE
                else None
            ),
            rejection_apply_state=(
                RejectionApplyState.STALE
                if record.rejection_apply_state
                in {
                    RejectionApplyState.NOT_STARTED,
                    RejectionApplyState.IN_FLIGHT,
                    RejectionApplyState.APPLIED_SEALED,
                }
                else record.rejection_apply_state
            ),
            rejection_operation_nonce=None,
            rejection_operation_capability_id=None,
            rejection_operation_owner_generation=None,
            rejection_operation_kind=None,
            **revoked_inflight,
        )
    elif isinstance(event, TurnSealed):
        if event.capability is not None:
            return reduce(record, BoundaryExact(event.capability), now)
    elif isinstance(event, RejectionApplied):
        if event.ticket == record.revoked_rejection_ticket:
            capability = record.revoked_rejection_capability
            record = _changed(
                record,
                revoked_rejection_ticket=None,
                revoked_rejection_capability=None,
            )
            return record, (
                *((RevokeRejectionCapability(capability),) if capability else ()),
                CountDiagnostic("admission_revoked_operation_applied_late"),
            )
        if not _ticket_matches(
            record,
            event.ticket,
            kind=AdmissionOperationKind.APPLY_REJECTION,
            nonce=record.rejection_operation_nonce,
        ) or (
            event.ticket.capability_id != record.rejection_operation_capability_id
            or event.ticket.capability_owner_generation
            != record.rejection_operation_owner_generation
            or event.ticket.capability_kind is not record.rejection_operation_kind
        ):
            return record, (CountDiagnostic("admission_late_operation_ignored"),)
        if event.kind is not record.rejection_operation_kind:
            capability = record.rejection_capability
            record = _changed(
                record,
                rejection_apply_state=RejectionApplyState.STALE,
                rejection_capability=None,
                rejection_operation_nonce=None,
                rejection_operation_capability_id=None,
                rejection_operation_owner_generation=None,
                rejection_operation_kind=None,
            )
            if capability is not None:
                effects.append(RevokeRejectionCapability(capability))
            effects.append(CountDiagnostic("admission_rejection_kind_mismatch"))
        else:
            record = _changed(
                record,
                rejection_apply_state=(
                    RejectionApplyState.APPLIED_ACTIVE
                    if event.kind is RejectionCapabilityKind.ACTIVE
                    else RejectionApplyState.APPLIED_SEALED
                ),
                rejection_operation_nonce=None,
                rejection_operation_capability_id=None,
                rejection_operation_owner_generation=None,
                rejection_operation_kind=None,
            )
            effects.append(
                CountDiagnostic(
                    "admission_rejection_applied_active"
                    if event.kind is RejectionCapabilityKind.ACTIVE
                    else "admission_rejection_applied_sealed"
                )
            )
            if (
                record.admission_state is AdmissionState.DROPPED
                and event.kind is RejectionCapabilityKind.SEALED
                and record.rejection_capability is not None
            ):
                effects.append(RevokeRejectionCapability(record.rejection_capability))
                record = _changed(record, rejection_capability=None)
    elif isinstance(event, (RejectionStale, RejectionFailed)):
        if event.ticket == record.revoked_rejection_ticket:
            record = _changed(
                record,
                revoked_rejection_ticket=None,
                revoked_rejection_capability=None,
            )
            return record, (CountDiagnostic("admission_revoked_operation_settled"),)
        if not _ticket_matches(
            record,
            event.ticket,
            kind=AdmissionOperationKind.APPLY_REJECTION,
            nonce=record.rejection_operation_nonce,
        ) or (
            event.ticket.capability_id != record.rejection_operation_capability_id
            or event.ticket.capability_owner_generation
            != record.rejection_operation_owner_generation
            or event.ticket.capability_kind is not record.rejection_operation_kind
        ):
            return record, (CountDiagnostic("admission_late_operation_ignored"),)
        capability = record.rejection_capability
        if capability is not None:
            effects.append(RevokeRejectionCapability(capability))
        record = _changed(
            record,
            rejection_apply_state=(
                RejectionApplyState.STALE
                if isinstance(event, RejectionStale)
                else RejectionApplyState.FAILED
            ),
            rejection_operation_nonce=None,
            rejection_operation_capability_id=None,
            rejection_operation_owner_generation=None,
            rejection_operation_kind=None,
            rejection_capability=None,
        )
    elif isinstance(event, CapabilityRevoked):
        remaining = tuple(
            operation
            for operation in record.pending_revocations
            if operation.ticket != event.ticket
        )
        if len(remaining) == len(record.pending_revocations):
            return record, (CountDiagnostic("admission_late_revoke_ack_ignored"),)
        record = _changed(record, pending_revocations=remaining)
    elif isinstance(event, CapabilityRevokeFailed):
        matched = False
        pending: list[PendingCapabilityRevocation] = []
        for operation in record.pending_revocations:
            if operation.ticket == event.ticket:
                matched = True
                pending.append(replace(operation, degraded=True))
            else:
                pending.append(operation)
        if not matched:
            return record, (CountDiagnostic("admission_late_revoke_failure_ignored"),)
        record = _changed(
            record,
            pending_revocations=tuple(pending),
            revocation_degraded=True,
        )
        effects.append(PoisonSpeakerAuthorityNamespace(record.turn_token))
    elif isinstance(event, SpeakerAuthorityNamespacePoisoned):
        if event.ticket != record.namespace_poison_ticket:
            return record, (CountDiagnostic("admission_late_namespace_poison_ack"),)
        record = _changed(
            record,
            pending_revocations=(),
            revocation_degraded=False,
            namespace_poison_ticket=None,
        )
    elif isinstance(event, SpeakerAuthorityNamespacePoisonFailed):
        if event.ticket != record.namespace_poison_ticket:
            return record, (CountDiagnostic("admission_late_namespace_poison_failure"),)
        record = _changed(record, revocation_degraded=True)
    elif isinstance(event, ProviderFinalReceived):
        if (
            record.admission_state is AdmissionState.ABANDONED
            or record.provider_binding_state is ProviderBindingState.RETIRED
        ):
            return record, (
                CountDiagnostic("admission_final_after_retirement_ignored"),
            )
        final = event.final
        if (
            record.admission_state not in _TERMINAL_ADMISSION_STATES
            and record.provider_key is not None
            and final.provider_key is None
            and record.boundary_state is not BoundaryState.UNKNOWN
        ):
            downgraded, downgrade_effects = reduce(
                record,
                BoundaryUnknown(record.provider_key),
                now,
            )
            accepted, final_effects = reduce(downgraded, event, now)
            return accepted, (*downgrade_effects, *final_effects)
        if final.provider_key is not None and record.provider_key not in {
            None,
            final.provider_key,
        }:
            return record, (CountDiagnostic("admission_stale_provider_final"),)
        if record.pending_final is not None:
            if record.pending_final == final:
                return record, ()
            return record, (CountDiagnostic("admission_conflicting_provider_final"),)
        changes = {
            "provider_final_state": ProviderFinalState.RECEIVED,
            "pending_final": final,
        }
        if record.admission_state is AdmissionState.RESERVED:
            changes["admission_state"] = AdmissionState.PENDING
        if record.provider_key is None and final.provider_key is not None:
            changes.update(
                provider_binding_state=ProviderBindingState.BOUND,
                provider_key=final.provider_key,
            )
        record = _changed(record, **changes)
        rejection_ticket = _current_rejection_ticket(record)
        if (
            rejection_ticket is not None
            and not record.provider_boundary_deadline_expired
        ):
            effects.append(
                ConstrainRejectionDeadline(
                    ticket=rejection_ticket,
                    absolute_deadline=final.boundary_deadline,
                )
            )
    elif isinstance(event, FinalDeadlineExpired):
        final = record.pending_final
        if (
            final is None
            or event.deadline != final.boundary_deadline
            or not _ticket_matches(
                record,
                event.ticket,
                kind=AdmissionOperationKind.FINAL_DEADLINE,
                nonce=record.deadline_operation_nonce,
            )
        ):
            return record, (CountDiagnostic("admission_late_deadline_ignored"),)
        capability = record.rejection_capability
        if capability is not None:
            effects.append(RevokeRejectionCapability(capability))
        revoked_inflight = _revoked_inflight_changes(record)
        record = _changed(
            record,
            deadline_operation_nonce=None,
            provider_boundary_deadline_expired=True,
            boundary_state=(
                BoundaryState.UNKNOWN
                if record.boundary_state in {BoundaryState.OPEN, BoundaryState.EXACT}
                else record.boundary_state
            ),
            micro_event_state=(
                MicroEventState.UNAVAILABLE
                if record.micro_event_state is MicroEventState.PENDING
                else record.micro_event_state
            ),
            rejection_apply_state=(
                RejectionApplyState.STALE
                if record.rejection_apply_state
                in {RejectionApplyState.NOT_STARTED, RejectionApplyState.IN_FLIGHT}
                else record.rejection_apply_state
            ),
            rejection_capability=None,
            rejection_operation_nonce=None,
            rejection_operation_capability_id=None,
            rejection_operation_owner_generation=None,
            rejection_operation_kind=None,
            **revoked_inflight,
        )
    elif isinstance(event, MicroEventPending):
        if record.micro_event_state is MicroEventState.NOT_APPLICABLE:
            record = _changed(record, micro_event_state=MicroEventState.PENDING)
    elif isinstance(event, MicroEventAllowed):
        accepted = record.micro_event_state in {
            MicroEventState.NOT_APPLICABLE,
            MicroEventState.PENDING,
        }
        record = _changed(
            record,
            micro_event_shadow_would_suppress=(
                event.shadow_would_suppress
                if accepted
                else (
                    (
                        record.micro_event_shadow_would_suppress
                        or event.shadow_would_suppress
                    )
                    if record.micro_event_state is MicroEventState.ALLOW
                    else False
                )
            ),
            micro_event_state=(
                MicroEventState.ALLOW
                if record.micro_event_state
                in {MicroEventState.NOT_APPLICABLE, MicroEventState.PENDING}
                else (
                    MicroEventState.ALLOW
                    if record.micro_event_state is MicroEventState.ALLOW
                    else MicroEventState.UNAVAILABLE
                )
            ),
        )
    elif isinstance(event, MicroEventSuppressed):
        record = _changed(
            record,
            micro_event_shadow_would_suppress=False,
            micro_event_state=(
                MicroEventState.SUPPRESS
                if record.micro_event_state
                in {MicroEventState.NOT_APPLICABLE, MicroEventState.PENDING}
                else (
                    MicroEventState.SUPPRESS
                    if record.micro_event_state is MicroEventState.SUPPRESS
                    else MicroEventState.UNAVAILABLE
                )
            ),
        )
    elif isinstance(event, MicroEventUnavailable):
        record = _changed(
            record,
            micro_event_state=MicroEventState.UNAVAILABLE,
            micro_event_shadow_would_suppress=False,
        )
    elif isinstance(event, CoreSettled):
        if event.ticket != record.resolution_ticket:
            return record, (CountDiagnostic("admission_stale_core_settlement"),)
        record = _changed(
            record,
            core_settlement_state=(
                SettlementState.DEGRADED if event.degraded else SettlementState.SETTLED
            ),
        )
        if event.degraded:
            effects.append(CountDiagnostic("admission_core_settlement_degraded"))
    elif isinstance(event, TransportSettled):
        if event.ticket != record.resolution_ticket:
            return record, (CountDiagnostic("admission_stale_transport_settlement"),)
        record = _changed(
            record,
            transport_settlement_state=(
                SettlementState.DEGRADED if event.degraded else SettlementState.SETTLED
            ),
        )
        if event.degraded:
            effects.append(CountDiagnostic("admission_transport_settlement_degraded"))
            if (
                record.evidence_state is EvidenceState.DENY_LATCHED
                and not record.speaker_deny_cleanup_failed_counted
            ):
                record = _changed(
                    record,
                    speaker_deny_cleanup_failed_counted=True,
                )
                effects.append(CountDiagnostic("speaker_deny_cleanup_failed_count"))
    elif isinstance(event, LifecycleSettled):
        if event.ticket != record.resolution_ticket:
            return record, (CountDiagnostic("admission_stale_lifecycle_settlement"),)
        record = _changed(
            record,
            lifecycle_settlement_state=(
                SettlementState.DEGRADED if event.degraded else SettlementState.SETTLED
            ),
        )
        if event.degraded:
            effects.append(CountDiagnostic("admission_lifecycle_settlement_degraded"))
            if (
                record.evidence_state is EvidenceState.DENY_LATCHED
                and not record.speaker_deny_cleanup_failed_counted
            ):
                record = _changed(
                    record,
                    speaker_deny_cleanup_failed_counted=True,
                )
                effects.append(CountDiagnostic("speaker_deny_cleanup_failed_count"))

    record, authority_effects = _release_active_authority_if_settled(record)
    effects.extend(authority_effects)
    record, micro_event_effects = _count_terminal_micro_event_if_settled(record)
    effects.extend(micro_event_effects)
    record, partial_effects = _settle_forward_partial_if_terminal(record)
    effects.extend(partial_effects)
    record, resolution_effects = maybe_resolve(record, now)
    effects.extend(resolution_effects)
    record, apply_effects = _start_rejection_if_ready(record, now=now)
    effects.extend(apply_effects)
    record, deadline_effects = _schedule_deadline_if_needed(record)
    effects.extend(deadline_effects)
    return record, tuple(effects)


def _track_revocation_effects(
    record: VoiceTurnAdmissionRecord,
    effects: tuple[AdmissionEffect, ...],
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    pending = list(record.pending_revocations)
    nonce = record.operation_nonce_sequence
    rewritten: list[AdmissionEffect] = []
    changed = False
    for effect in effects:
        if isinstance(effect, PoisonSpeakerAuthorityNamespace):
            ticket = record.namespace_poison_ticket
            if ticket is None:
                nonce += 1
                ticket = AdmissionOperationTicket(
                    turn_token=record.turn_token,
                    record_generation=record.record_generation,
                    operation_kind=AdmissionOperationKind.POISON_SPEAKER_NAMESPACE,
                    operation_nonce=nonce,
                )
                record = _changed(
                    record,
                    operation_nonce_sequence=nonce,
                    namespace_poison_ticket=ticket,
                    revocation_degraded=True,
                )
            rewritten.append(replace(effect, ticket=ticket))
            continue
        if not isinstance(effect, RevokeRejectionCapability):
            rewritten.append(effect)
            continue
        existing = next(
            (
                operation
                for operation in pending
                if operation.capability == effect.capability
            ),
            None,
        )
        if existing is not None:
            rewritten.append(replace(effect, ticket=existing.ticket))
            continue
        if len(pending) >= _MAX_PENDING_REVOCATIONS:
            poison_ticket = record.namespace_poison_ticket
            if poison_ticket is None:
                nonce += 1
                poison_ticket = AdmissionOperationTicket(
                    turn_token=record.turn_token,
                    record_generation=record.record_generation,
                    operation_kind=AdmissionOperationKind.POISON_SPEAKER_NAMESPACE,
                    operation_nonce=nonce,
                )
                record = _changed(
                    record,
                    operation_nonce_sequence=nonce,
                    namespace_poison_ticket=poison_ticket,
                    revocation_degraded=True,
                )
            rewritten.append(
                PoisonSpeakerAuthorityNamespace(record.turn_token, poison_ticket)
            )
            continue
        nonce += 1
        ticket = AdmissionOperationTicket(
            turn_token=record.turn_token,
            record_generation=record.record_generation,
            operation_kind=AdmissionOperationKind.REVOKE_CAPABILITY,
            operation_nonce=nonce,
            capability_id=effect.capability.capability_id,
            capability_owner_generation=effect.capability.owner_generation,
            capability_kind=effect.capability.kind,
        )
        pending.append(PendingCapabilityRevocation(ticket, effect.capability))
        rewritten.append(replace(effect, ticket=ticket))
        changed = True
    if changed:
        record = _changed(
            record,
            operation_nonce_sequence=nonce,
            pending_revocations=tuple(pending),
        )
    return record, tuple(rewritten)


def reduce(
    record: VoiceTurnAdmissionRecord,
    event: AdmissionEvent,
    now: float,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    reduced, effects = _reduce_untracked(record, event, now)
    return _track_revocation_effects(reduced, effects)


__all__ = [
    "hold_exact_interval_final",
    "maybe_resolve",
    "reduce",
    "resolve_exact_interval",
]
