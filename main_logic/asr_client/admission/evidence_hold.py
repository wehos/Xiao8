"""Local evidence waiting; tickets carry no transport capabilities."""

from dataclasses import replace

from ..speaker_evidence import EvidenceMode, EvidenceStatus, evaluate_coverage
from .contracts import (
    AdmissionEffect, AdmissionOperationKind, AdmissionOperationTicket, CountDiagnostic,
    EvidenceDeadlineExpired, EvidenceHoldRecord, EvidenceHoldRequested,
    EvidenceHoldResolved, ProviderBindingState, ScheduleEvidenceDeadline, VoiceTurnAdmissionRecord,
)


EVIDENCE_HOLD_EVENT_TYPES = (EvidenceHoldRequested, EvidenceHoldResolved, EvidenceDeadlineExpired)


def reduce_evidence_hold(
    record: VoiceTurnAdmissionRecord,
    event: EvidenceHoldRequested | EvidenceHoldResolved | EvidenceDeadlineExpired,
    now: float,
) -> tuple[VoiceTurnAdmissionRecord, tuple[AdmissionEffect, ...]]:
    hold = record.evidence_hold
    if isinstance(event, EvidenceHoldRequested):
        binding = event.binding
        if (
            not record.evidence_hold_enabled or record.terminal_disposition is not None
            or hold is not None or record.pending_final is not None
            or record.provider_boundary_deadline_expired
            or record.provider_binding_state is not ProviderBindingState.BOUND
            or binding.provider_key != record.provider_key
            or binding.turn_token != record.turn_token
            or binding.record_generation != record.record_generation
            or now > event.first_final_received_at + 0.2
        ):
            return record, (CountDiagnostic("evidence_hold_request_ignored"),)
        deadline = event.first_final_received_at + 2.0
        if event.hard_deadline is not None:
            deadline = min(deadline, event.hard_deadline)
        nonce = record.operation_nonce_sequence + 1
        ticket = AdmissionOperationTicket(record.turn_token, record.record_generation,
                                         AdmissionOperationKind.EVIDENCE_DEADLINE, nonce)
        hold = EvidenceHoldRecord(binding, ticket, event.first_final_received_at, deadline,
                                  EvidenceStatus.PENDING if now < deadline else EvidenceStatus.UNAVAILABLE)
        updated = replace(record, evidence_hold=hold, operation_nonce_sequence=nonce,
                          logical_revision=record.logical_revision + 1)
        effects = (CountDiagnostic("evidence_hold_registered"),)
        if hold.status is EvidenceStatus.PENDING:
            effects += (ScheduleEvidenceDeadline(ticket, deadline),)
        return updated, effects
    if (
        hold is None or event.ticket != hold.ticket
        or event.ticket.record_generation != record.record_generation
        or hold.status is not EvidenceStatus.PENDING
    ):
        return record, (CountDiagnostic("evidence_hold_stale_ticket"),)
    proof = None
    if isinstance(event, EvidenceDeadlineExpired):
        if event.deadline != hold.absolute_deadline or now < hold.absolute_deadline:
            return record, (CountDiagnostic("evidence_hold_early_deadline_ignored"),)
        status = EvidenceStatus.UNAVAILABLE
    else:
        proof = event.proof
        if (
            proof.mode is not EvidenceMode.AUTHORITATIVE or proof.binding != hold.binding
            or now >= hold.absolute_deadline
            or any(score.completed_at > now for score in proof.scores)
        ):
            return record, (CountDiagnostic("evidence_hold_invalid_proof"),)
        checked = evaluate_coverage(hold.binding, proof.scores, proof.continuity,
                                    mode=EvidenceMode.AUTHORITATIVE)
        if checked.status != proof.status or proof.status is EvidenceStatus.PENDING:
            return record, (CountDiagnostic("evidence_hold_invalid_proof"),)
        status = checked.status
        proof = checked
    return replace(record, evidence_hold=replace(hold, status=status, proof=proof),
                   logical_revision=record.logical_revision + 1), (
        CountDiagnostic("evidence_hold_" + status.value),
    )
