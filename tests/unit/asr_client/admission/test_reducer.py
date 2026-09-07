from __future__ import annotations

from dataclasses import replace

from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.admission.contracts import (
    AbortProviderTransport,
    AdmissionDisposition,
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
    CoreSettled,
    CountDiagnostic,
    ConstrainRejectionDeadline,
    EvidenceState,
    FinalDeadlineExpired,
    LifecycleSettled,
    MicroEventPending,
    MicroEventState,
    MicroEventSuppressed,
    MicroEventUnavailable,
    PendingProviderFinal,
    PoisonSpeakerAuthorityNamespace,
    ProviderBindingState,
    ProviderFinalReceived,
    RejectionApplied,
    RejectionApplyState,
    RejectionCapability,
    RejectionCapabilityKind,
    Reset,
    ResolveReserved,
    RevokeRejectionCapability,
    ScheduleFinalDeadline,
    SettlePartial,
    SettlementState,
    SpeakerCaptureLeaseToken,
    SpeakerCheckpointKind,
    SpeakerAuthorityPending,
    SpeakerAuthorityUnarmed,
    SpeakerAuthorityUnavailable,
    SpeakerHigh,
    SpeakerLow,
    SpeakerUnavailable,
    SpeakerAuthorityNamespacePoisoned,
    TransportSettled,
    VoiceTurnAdmissionRecord,
)
from main_logic.asr_client.admission.reducer import reduce
from main_logic.asr_client.speaker_shadow.contracts import SpeakerShadowCandidateKey
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken


def _token(turn_id: int = 1) -> VoiceTurnToken:
    return VoiceTurnToken(
        ingress=VoiceIngressToken(1, "socket", 2, 3, 4),
        turn_id=turn_id,
    )


def _provider_key(utterance_id: int = 1) -> ProviderUtteranceKey:
    return ProviderUtteranceKey(1, 0, utterance_id)


def _candidate(generation: int = 1) -> SpeakerShadowCandidateKey:
    return SpeakerShadowCandidateKey(5, generation, "provider_candidate")


def _lease() -> SpeakerCaptureLeaseToken:
    return SpeakerCaptureLeaseToken(1, 2, 3, 4, 5)


def _record(
    *, capability_kind: RejectionCapabilityKind = RejectionCapabilityKind.SEALED
):
    token = _token()
    key = _provider_key()
    candidate = _candidate()
    capability = RejectionCapability(
        capability_id=7,
        owner_generation=5,
        kind=capability_kind,
        turn_token=token,
        candidate=candidate,
        provider_key=key,
    )
    record = VoiceTurnAdmissionRecord(
        turn_token=token,
        record_generation=1,
        provider_binding_state=ProviderBindingState.BOUND,
        candidate_binding_state=CandidateBindingState.BOUND,
        capture_state=CaptureState.COLLECTING,
        provider_key=key,
        speaker_candidate=candidate,
    )
    return record, capability


def _unbound_record() -> VoiceTurnAdmissionRecord:
    return VoiceTurnAdmissionRecord(
        turn_token=_token(),
        record_generation=1,
        provider_binding_state=ProviderBindingState.BOUND,
        provider_key=_provider_key(),
    )


def _final(*, text: str = "hello", deadline: float = 10.2) -> PendingProviderFinal:
    return PendingProviderFinal(
        provider_key=_provider_key(),
        provider="qwen",
        text=text,
        received_at=10.0,
        admission_deadline=deadline,
    )


def _step(record, event, now: float = 10.0):
    return reduce(record, event, now)


def _resolve_effects(effects):
    return [effect for effect in effects if isinstance(effect, ResolveReserved)]


def _apply_effect(effects) -> ApplyRejection:
    return next(effect for effect in effects if isinstance(effect, ApplyRejection))


def _deadline_effect(effects) -> ScheduleFinalDeadline:
    return next(
        effect for effect in effects if isinstance(effect, ScheduleFinalDeadline)
    )


def test_final_then_second_low_and_exact_before_deadline_drops():
    record, capability = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(record, ProviderFinalReceived(_final()))
    assert record.admission_state is AdmissionState.PENDING
    assert isinstance(_deadline_effect(effects), ScheduleFinalDeadline)

    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
        now=10.05,
    )
    assert record.admission_state is AdmissionState.DROPPED
    assert [effect.disposition for effect in _resolve_effects(effects)] == [
        AdmissionDisposition.DROP
    ]
    record, late = _step(record, BoundaryExact(capability), now=10.06)
    assert record.rejection_capability is None
    assert any(isinstance(effect, RevokeRejectionCapability) for effect in late)


def test_exact_final_then_second_low_before_deadline_drops():
    record, capability = _record()
    record, _ = _step(record, BoundaryExact(capability))
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(record, ProviderFinalReceived(_final()))
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
        now=10.1,
    )
    assert record.admission_state is AdmissionState.DROPPED
    assert len(_resolve_effects(effects)) == 1
    assert not any(isinstance(effect, ApplyRejection) for effect in effects)
    assert any(
        isinstance(effect, CountDiagnostic)
        and effect.name == "speaker_deny_final_dropped_count"
        for effect in effects
    )


def test_final_before_first_score_holds_then_two_lows_drop():
    record, _ = _record()
    record, effects = _step(record, ProviderFinalReceived(_final()))
    assert record.admission_state is AdmissionState.PENDING
    assert not _resolve_effects(effects)

    record, later = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
        now=10.05,
    )
    record, latest = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
        now=10.06,
    )
    assert record.admission_state is AdmissionState.DROPPED
    assert _resolve_effects(latest)[0].disposition is AdmissionDisposition.DROP
    assert not _resolve_effects(later)


def test_authority_pending_before_candidate_holds_final():
    record = _unbound_record()
    record, _ = _step(record, SpeakerAuthorityPending("generation-a"))
    assert record.candidate_binding_state is CandidateBindingState.ARMING
    record, effects = _step(record, ProviderFinalReceived(_final()))
    assert record.admission_state is AdmissionState.PENDING
    assert not _resolve_effects(effects)


def test_matching_candidate_bind_after_arming_can_latch_deny():
    record = _unbound_record()
    record, _ = _step(record, SpeakerAuthorityPending("generation-a"))
    record, _ = _step(
        record,
        CandidateBound(_candidate(), owner_generation="generation-a"),
    )
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert record.evidence_state is EvidenceState.DENY_LATCHED
    assert record.admission_state is AdmissionState.DROPPED
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.DROP


def test_matching_unarm_releases_pending_final_fail_open():
    record = _unbound_record()
    record, _ = _step(record, SpeakerAuthorityPending("generation-a"))
    record, _ = _step(record, ProviderFinalReceived(_final()))
    record, effects = _step(record, SpeakerAuthorityUnarmed("generation-a"))
    assert record.candidate_binding_state is CandidateBindingState.RETIRED
    assert record.evidence_state is EvidenceState.UNAVAILABLE
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.FORWARD


def test_stale_unarm_is_ignored_and_bound_authority_is_not_degraded():
    record = _unbound_record()
    record, _ = _step(record, SpeakerAuthorityPending("generation-a"))
    arming = record
    record, effects = _step(record, SpeakerAuthorityUnarmed("generation-b"))
    assert record is arming
    assert any(
        isinstance(effect, CountDiagnostic)
        and effect.name == "admission_stale_speaker_authority_unarmed"
        for effect in effects
    )
    record, _ = _step(
        record,
        CandidateBound(_candidate(), owner_generation="generation-a"),
    )
    bound = record
    record, _ = _step(record, SpeakerAuthorityUnarmed("generation-a"))
    assert record is bound
    assert record.candidate_binding_state is CandidateBindingState.BOUND
    assert record.capture_state is CaptureState.COLLECTING


def test_first_low_closed_without_second_low_forwards():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(record, CaptureClosed(_candidate(), 1))
    record, effects = _step(record, ProviderFinalReceived(_final()))
    assert record.admission_state is AdmissionState.FORWARDED
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.FORWARD


def test_capture_complete_cannot_clear_latched_deny():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    record, _ = _step(record, CaptureClosed(_candidate(), 2))
    assert record.capture_state is CaptureState.COLLECTING
    assert record.evidence_state is EvidenceState.DENY_LATCHED


def test_boundary_deadline_does_not_forward_while_speaker_is_pending():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(record, ProviderFinalReceived(_final()))
    deadline = _deadline_effect(effects)
    record, effects = _step(
        record,
        FinalDeadlineExpired(deadline.ticket, deadline.absolute_deadline),
        now=deadline.absolute_deadline,
    )
    assert record.admission_state is AdmissionState.PENDING
    assert record.provider_boundary_deadline_expired is True
    assert not _resolve_effects(effects)
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
        now=10.3,
    )
    assert record.admission_state is AdmissionState.DROPPED
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.DROP


def test_reset_abandons_and_ignores_late_operation():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(record, Reset())
    assert record.admission_state is AdmissionState.ABANDONED
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.ABANDON


def test_micro_event_and_speaker_veto_combine_once():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert record.admission_state is AdmissionState.DROPPED
    assert len(_resolve_effects(effects)) == 1
    terminal = record
    record, later = _step(record, MicroEventSuppressed())
    assert record is terminal
    assert not _resolve_effects(later)


def test_partial_settlement_is_emitted_once_for_terminal_speaker_verdict():
    record, _ = _record()
    record, effects = _step(record, SpeakerHigh(_candidate(), 1))
    assert len([effect for effect in effects if isinstance(effect, SettlePartial)]) == 1
    record, effects = _step(record, CaptureClosed(_candidate(), 1))
    assert not any(isinstance(effect, SettlePartial) for effect in effects)


def test_terminal_forward_ignores_late_boundary_speaker_and_micro_facts():
    record, capability = _record()
    record, _ = _step(record, SpeakerHigh(_candidate(), 1))
    record, _ = _step(record, ProviderFinalReceived(_final()))
    terminal = record

    record, boundary_effects = _step(record, BoundaryExact(capability), now=10.1)
    assert record.admission_state is terminal.admission_state
    assert record.pending_final == terminal.pending_final
    assert record.rejection_capability is None
    assert any(
        isinstance(effect, RevokeRejectionCapability)
        and effect.capability == capability
        for effect in boundary_effects
    )
    post_boundary = record

    record, stale = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
        now=10.11,
    )
    record, _ = _step(record, MicroEventSuppressed(), now=10.12)
    assert record is post_boundary
    assert record.evidence_state is EvidenceState.ALLOW
    assert record.micro_event_state.value == "not_applicable"
    assert any(
        isinstance(effect, CountDiagnostic)
        and effect.name == "speaker_late_fact_stale_count"
        for effect in stale
    )


def test_deny_cleanup_degraded_is_counted_once():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    resolution = _resolve_effects(effects)[0]
    record, transport = _step(
        record,
        TransportSettled(resolution.ticket, degraded=True),
    )
    assert (
        sum(
            isinstance(effect, CountDiagnostic)
            and effect.name == "speaker_deny_cleanup_failed_count"
            for effect in transport
        )
        == 1
    )
    record, lifecycle = _step(
        record,
        LifecycleSettled(resolution.ticket, degraded=True),
    )
    assert not any(
        isinstance(effect, CountDiagnostic)
        and effect.name == "speaker_deny_cleanup_failed_count"
        for effect in lifecycle
    )


def test_unknown_boundary_cannot_clear_latched_deny():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert record.admission_state is AdmissionState.DROPPED
    terminal = record
    record, effects = _step(record, BoundaryUnknown(_provider_key()))
    assert record is terminal
    assert record.evidence_state is EvidenceState.DENY_LATCHED
    assert not _resolve_effects(effects)


def test_late_exact_after_deny_is_revoked_without_rebinding():
    record, capability = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    record, effects = _step(record, BoundaryExact(capability))
    assert record.admission_state is AdmissionState.DROPPED
    assert record.rejection_capability is None
    assert any(isinstance(effect, RevokeRejectionCapability) for effect in effects)


def test_deny_before_final_drops_without_capability():
    record, _ = _record()
    record = replace(record, speaker_lease_token=_lease())
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert record.admission_state is AdmissionState.DROPPED
    resolution = _resolve_effects(effects)[0]
    abort = next(
        effect for effect in effects if isinstance(effect, AbortProviderTransport)
    )
    assert resolution.final is None
    assert abort.ticket is resolution.ticket
    assert abort.speaker_lease_token == _lease()
    assert abort.turn_token == record.turn_token
    assert abort.record_generation == record.record_generation
    assert abort.resolution_nonce == resolution.ticket.resolution_nonce
    assert abort.disposition is AdmissionDisposition.DROP


def test_empty_final_is_not_dropped_by_micro_event_suppress():
    record, _ = _record()
    record, _ = _step(record, MicroEventSuppressed())
    record, effects = _step(record, ProviderFinalReceived(_final(text="")))
    assert record.admission_state is AdmissionState.PENDING
    assert not _resolve_effects(effects)
    record, effects = _step(record, CaptureClosed(_candidate(), 0))
    assert record.admission_state is AdmissionState.FORWARDED
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.FORWARD


def test_active_applied_drops_before_provider_final_and_late_final_only_settles():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert record.admission_state is AdmissionState.DROPPED
    assert any(isinstance(effect, AbortProviderTransport) for effect in effects)
    assert len(_resolve_effects(effects)) == 1

    record, effects = _step(record, ProviderFinalReceived(_final()), now=10.1)
    assert record.provider_final_state.value == "not_received"
    assert record.admission_state is AdmissionState.DROPPED
    assert not _resolve_effects(effects)


def test_speaker_authority_loss_terminalizes_without_fabricating_sequence():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(record, SpeakerAuthorityUnavailable(_candidate()))
    assert record.evidence_state is EvidenceState.UNAVAILABLE
    assert record.capture_state is CaptureState.UNAVAILABLE
    assert record.last_speaker_sequence_no == 1
    assert any(
        isinstance(effect, SettlePartial)
        and effect.disposition is AdmissionDisposition.FORWARD
        for effect in effects
    )

    record, effects = _step(record, ProviderFinalReceived(_final()))
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.FORWARD


def test_foreign_capability_does_not_destroy_current_exact_authority():
    record, capability = _record()
    record, _ = _step(record, BoundaryExact(capability))
    foreign = RejectionCapability(
        capability_id=capability.capability_id + 1,
        owner_generation=capability.owner_generation,
        kind=capability.kind,
        turn_token=capability.turn_token,
        candidate=_candidate(2),
        provider_key=capability.provider_key,
    )
    record, effects = _step(record, BoundaryExact(foreign))
    assert record.boundary_state is BoundaryState.EXACT
    assert record.rejection_capability == capability
    assert [
        effect.capability
        for effect in effects
        if isinstance(effect, RevokeRejectionCapability)
    ] == [foreign]


def test_unknown_boundary_absorbs_late_exact_capability():
    record, capability = _record()
    record, _ = _step(record, BoundaryUnknown(_provider_key()))
    record, effects = _step(record, BoundaryExact(capability))
    assert record.boundary_state is BoundaryState.UNKNOWN
    assert record.rejection_capability is None
    assert any(
        isinstance(effect, RevokeRejectionCapability)
        and effect.capability == capability
        for effect in effects
    )


def test_capture_close_sequence_gap_fails_open_and_late_low_is_ignored():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(record, CaptureClosed(_candidate(), 2))
    assert record.capture_state is CaptureState.UNAVAILABLE
    assert record.evidence_state is EvidenceState.UNAVAILABLE

    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    record, effects = _step(record, ProviderFinalReceived(_final()))
    assert record.admission_state is AdmissionState.FORWARDED
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.FORWARD


def test_speaker_unavailable_cannot_clear_sticky_deny():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    record, effects = _step(record, SpeakerUnavailable(_candidate(), 3))
    assert record.evidence_state is EvidenceState.DENY_LATCHED
    assert record.admission_state is AdmissionState.DROPPED
    assert not _resolve_effects(effects)


def test_boundary_deadline_releases_terminal_speaker_when_micro_is_pending():
    record, _ = _record()
    record, _ = _step(record, SpeakerHigh(_candidate(), 1))
    record, _ = _step(record, MicroEventPending())
    record, effects = _step(record, ProviderFinalReceived(_final()))
    deadline = _deadline_effect(effects)
    record, effects = _step(
        record,
        FinalDeadlineExpired(deadline.ticket, deadline.absolute_deadline),
        now=deadline.absolute_deadline,
    )
    assert record.provider_boundary_deadline_expired is True
    assert record.micro_event_state is MicroEventState.UNAVAILABLE
    assert record.admission_state is AdmissionState.FORWARDED
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.FORWARD


def test_boundary_deadline_cannot_override_latched_deny():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert record.admission_state is AdmissionState.DROPPED


def test_empty_final_is_dropped_by_latched_deny():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    record, effects = _step(record, ProviderFinalReceived(_final(text="")))
    assert record.admission_state is AdmissionState.DROPPED
    assert not _resolve_effects(effects)


def test_terminal_reset_revokes_active_capability_without_changing_disposition():
    record, capability = _record(capability_kind=RejectionCapabilityKind.ACTIVE)
    record, _ = _step(record, BoundaryExact(capability))
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert any(isinstance(effect, RevokeRejectionCapability) for effect in effects)
    generation = record.record_generation

    record, effects = _step(record, Reset())
    assert record.admission_state is AdmissionState.DROPPED
    assert record.record_generation == generation + 1
    assert record.rejection_capability is None
    assert not _resolve_effects(effects)


def test_settlement_requires_matching_resolution_nonce_and_disposition():
    record, _ = _record()
    record, _ = _step(record, SpeakerHigh(_candidate(), 1))
    record, effects = _step(record, ProviderFinalReceived(_final()))
    resolution = _resolve_effects(effects)[0]
    wrong = AdmissionResolutionTicket(
        turn_token=record.turn_token,
        record_generation=record.record_generation,
        resolution_nonce=resolution.ticket.resolution_nonce + 1,
        disposition=AdmissionDisposition.FORWARD,
    )
    record, _ = _step(record, CoreSettled(wrong))
    assert record.core_settlement_state is SettlementState.NOT_STARTED

    wrong_disposition = AdmissionResolutionTicket(
        turn_token=record.turn_token,
        record_generation=record.record_generation,
        resolution_nonce=resolution.ticket.resolution_nonce,
        disposition=AdmissionDisposition.DROP,
    )
    record, _ = _step(record, CoreSettled(wrong_disposition))
    assert record.core_settlement_state is SettlementState.NOT_STARTED

    record, _ = _step(record, CoreSettled(resolution.ticket))
    assert record.core_settlement_state is SettlementState.SETTLED


def test_authority_loss_for_foreign_candidate_is_ignored():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(record, SpeakerAuthorityUnavailable(_candidate(2)))
    assert record.evidence_state is EvidenceState.FIRST_LOW
    assert record.capture_state is CaptureState.COLLECTING
    assert any(
        isinstance(effect, CountDiagnostic)
        and effect.name == "admission_stale_speaker_authority"
        for effect in effects
    )


def test_missing_provider_key_cannot_reopen_latched_deny():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    missing_key_final = PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
    record, effects = _step(record, ProviderFinalReceived(missing_key_final))
    assert record.admission_state is AdmissionState.DROPPED
    assert record.pending_final is None
    assert not _resolve_effects(effects)


def test_latched_deny_partial_settlement_is_drop_and_emitted_once():
    record, _ = _record()
    record, _ = _step(
        record,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    record, effects = _step(
        record,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    partials = [effect for effect in effects if isinstance(effect, SettlePartial)]
    assert len(partials) == 1
    assert partials[0].disposition is AdmissionDisposition.DROP
    record, later = _step(record, CaptureClosed(_candidate(), 2))
    assert not any(isinstance(effect, SettlePartial) for effect in later)


def test_capability_revoke_requires_ack_and_failure_keeps_cleanup_handle():
    record, capability = _record()
    record, effects = _step(record, BoundaryExact(capability))
    record, effects = _step(record, BoundaryUnknown(_provider_key()))
    revoke = next(
        effect for effect in effects if isinstance(effect, RevokeRejectionCapability)
    )
    assert revoke.ticket is not None
    assert len(record.pending_revocations) == 1

    record, effects = _step(record, CapabilityRevokeFailed(revoke.ticket))
    assert len(record.pending_revocations) == 1
    assert record.pending_revocations[0].degraded is True
    assert record.revocation_degraded is True
    poison = next(
        effect
        for effect in effects
        if isinstance(effect, PoisonSpeakerAuthorityNamespace)
    )
    assert poison.ticket is not None

    record, _ = _step(record, CapabilityRevoked(revoke.ticket))
    assert record.pending_revocations == ()
    assert record.revocation_degraded is True
    record, _ = _step(record, SpeakerAuthorityNamespacePoisoned(poison.ticket))
    assert record.pending_revocations == ()
    assert record.revocation_degraded is False
    assert record.namespace_poison_ticket is None


def test_micro_terminal_results_are_monotonic_and_conflicts_fail_open():
    record, _ = _record()
    record, _ = _step(record, SpeakerHigh(_candidate(), 1))
    record, _ = _step(record, MicroEventPending())
    record, _ = _step(record, MicroEventUnavailable())
    record, _ = _step(record, MicroEventSuppressed())
    assert record.micro_event_state.value == "unavailable"
    record, effects = _step(record, ProviderFinalReceived(_final()))
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.FORWARD

    record, _ = _record()
    record, _ = _step(record, SpeakerHigh(_candidate(), 1))
    record, _ = _step(record, MicroEventPending())
    record, _ = _step(record, MicroEventSuppressed())
    record, _ = _step(record, MicroEventUnavailable())
    assert record.micro_event_state.value == "unavailable"
    record, effects = _step(record, ProviderFinalReceived(_final()))
    assert _resolve_effects(effects)[0].disposition is AdmissionDisposition.FORWARD
