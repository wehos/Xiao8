"""Content-free observations of admission decisions; never replay authority."""

from __future__ import annotations

import hashlib
import secrets
import time

from config.application import APP_VERSION

from .contracts import (
    AdmissionDisposition,
    AdmissionResolutionTicket,
    EvidenceState,
    MicroEventState,
    VoiceTurnAdmissionRecord,
)

_PROCESS_NONCE = secrets.token_bytes(16)


def resolution_diagnostics(
    record: VoiceTurnAdmissionRecord | None,
    ticket: AdmissionResolutionTicket,
) -> dict[str, str | int | bool | None]:
    """Copy scalars only; stale tickets cannot borrow a successor's verdict."""
    turn = ticket.turn_token
    ingress = turn.ingress
    correlation = hashlib.blake2s(
        repr(turn).encode("utf-8"), key=_PROCESS_NONCE, digest_size=12,
    ).hexdigest()
    result = {
        "schema": 1,
        "observed_at_ns": time.time_ns(),
        "app_version": APP_VERSION,
        "turn_ref": correlation,
        "session_epoch": ingress.session_epoch,
        "turn_id": turn.turn_id,
        "route_generation": ingress.route_generation,
        "audio_generation": ingress.audio_generation,
        "lease_generation": ingress.lease_generation,
        "record_generation": ticket.record_generation,
        "resolution_nonce": ticket.resolution_nonce,
        "disposition": ticket.disposition.value,
        "reason_code": "ASR_DECISION_RECORD_UNAVAILABLE",
        "evidence_state": "unknown",
    }
    if record is None or record.resolution_ticket != ticket:
        return result
    if ticket.disposition is AdmissionDisposition.ABANDON:
        reason = "ASR_TURN_ABANDONED"
    elif ticket.disposition is AdmissionDisposition.DROP:
        reason = (
            "ASR_SPEAKER_REJECTED"
            if record.evidence_state is EvidenceState.DENY_LATCHED
            else "ASR_MICRO_EVENT_SUPPRESSED"
            if record.micro_event_state is MicroEventState.SUPPRESS
            else "ASR_ADMISSION_REJECTED"
        )
    elif record.evidence_state is EvidenceState.ALLOW:
        reason = "ASR_SPEAKER_VERIFIED"
    elif record.evidence_state is EvidenceState.UNAVAILABLE:
        reason = "ASR_SPEAKER_EVIDENCE_UNAVAILABLE"
    else:
        # NONE/FIRST_LOW includes disabled/legacy gates and empty finals.
        # It must never be described as a successful speaker verification.
        reason = "ASR_FORWARD_WITHOUT_VERIFIED_EVIDENCE"
    key = record.provider_key
    candidate = record.speaker_candidate
    result.update(
        reason_code=reason,
        evidence_state=record.evidence_state.value,
        capture_state=record.capture_state.value,
        boundary_state=record.boundary_state.value,
        micro_event_state=record.micro_event_state.value,
        rejection_apply_state=record.rejection_apply_state.value,
        speaker_sequence=record.last_speaker_sequence_no,
        capture_through_sequence=record.capture_through_sequence_no,
        provider_generation=key.generation if key is not None else None,
        provider_buffer_epoch=key.buffer_epoch if key is not None else None,
        provider_utterance_id=key.utterance_id if key is not None else None,
        detector_epoch=candidate.detector_epoch if candidate is not None else None,
        shadow_generation=candidate.shadow_generation if candidate is not None else None,
        final_present=record.pending_final is not None,
        final_empty=(
            not bool(record.pending_final.text.strip())
            if record.pending_final is not None else None
        ),
    )
    return result
