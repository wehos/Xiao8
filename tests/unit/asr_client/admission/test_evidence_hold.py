"""Independent evidence deadlines using the real admission writer/reducer."""

from dataclasses import replace

import pytest

from main_logic.asr_client.admission.contracts import (
    AbortProviderTransport, AdmissionDisposition, CoreSettled,
    EvidenceDeadlineExpired, EvidenceHoldRequested, EvidenceHoldResolved,
    FinalDeadlineExpired, LifecycleSettled, PendingProviderFinal,
    ProviderFinalReceived, Reset, ResolveReserved, ScheduleEvidenceDeadline,
    ScheduleFinalDeadline, TransportSettled,
)
from main_logic.asr_client.admission.coordinator import (
    AdmissionCapacityError, VoiceTurnAdmissionCoordinator,
)
from main_logic.asr_client.speaker_evidence import (
    EvidenceMode, EvidenceProof, EvidenceStatus, evaluate_coverage,
)
from tests.unit.asr_client.test_speaker_evidence_scope import binding_for, score_for


async def exact_held_turn(*, coordinator=None, key=1, clock=None):
    from main_logic.asr_client.admission.contracts import (
        BoundaryProof, ExactIntervalPromotionScope, SpeakerCaptureLeaseToken,
    )
    from main_logic.asr_client.speaker_shadow.contracts import SpeakerShadowCandidateKey
    clock = clock or [10.0]
    coordinator = coordinator or VoiceTurnAdmissionCoordinator(evidence_hold_enabled=True, clock=lambda: clock[0])
    binding = binding_for(key)
    candidate = SpeakerShadowCandidateKey(2, key * 2 - 1, "provider_candidate")
    successor = SpeakerShadowCandidateKey(2, key * 2, "provider_candidate")
    lease = SpeakerCaptureLeaseToken(2, 2, 3, 4, key)
    await coordinator.open_speaker_lease(lease, candidate)
    child = await coordinator.attach_turn_to_speaker_lease(binding.turn_token, lease, binding.provider_key)
    parent = await coordinator.get_speaker_lease(lease)
    promoted = await coordinator.promote_exact_interval_tail_child(ExactIntervalPromotionScope(
        parent_lease_token=lease, parent_record_generation=parent.record_generation,
        expected_parent_logical_revision=parent.logical_revision, expected_parent_state=parent.state,
        turn_token=binding.turn_token, child_record_generation=child.record_generation,
        expected_child_logical_revision=child.logical_revision, provider_key=binding.provider_key,
        boundary_proof=BoundaryProof(1, 1, binding.provider_key), target_candidate=candidate,
        successor_candidate=successor,
    ))
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    binding = replace(binding, record_generation=child.record_generation)
    return coordinator, activated.receipt, candidate, binding, clock


async def held_turn(*, enabled=True, coordinator=None, key=1):
    coordinator = coordinator or VoiceTurnAdmissionCoordinator(evidence_hold_enabled=enabled)
    binding = binding_for(key)
    record = await coordinator.open_turn(binding.turn_token, provider_key=binding.provider_key)
    binding = replace(binding, record_generation=record.record_generation)
    final = PendingProviderFinal(binding.provider_key, "test", "synthetic", 10.0, 10.2)
    effects = await coordinator.post(binding.turn_token, EvidenceHoldRequested(binding, 10.0), now=10.0)
    return coordinator, binding, final, effects


def resolutions(effects):
    return [effect for effect in effects if isinstance(effect, ResolveReserved)]


@pytest.mark.asyncio
async def test_boundary_deadline_cannot_release_independent_evidence_hold():
    coordinator, binding, final, request = await held_turn()
    evidence_timer = next(e for e in request if isinstance(e, ScheduleEvidenceDeadline))
    assert evidence_timer.absolute_deadline == 12.0
    final_effects = await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0)
    boundary_timer = next(e for e in final_effects if isinstance(e, ScheduleFinalDeadline))
    assert not resolutions(final_effects)
    effects = await coordinator.post(binding.turn_token, FinalDeadlineExpired(boundary_timer.ticket, 10.2), now=10.2)
    record = await coordinator.get_record(binding.turn_token)
    assert record.provider_boundary_deadline_expired
    assert record.evidence_hold.status is EvidenceStatus.PENDING
    assert record.terminal_disposition is None
    assert not resolutions(effects)
    effects = await coordinator.post(binding.turn_token, EvidenceDeadlineExpired(evidence_timer.ticket, 12.0), now=12.0)
    assert [e.disposition for e in resolutions(effects)] == [AdmissionDisposition.FORWARD]
    assert not any(isinstance(e, AbortProviderTransport) for e in effects)
    record = await coordinator.get_record(binding.turn_token)
    assert record.evidence_hold.status is EvidenceStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_evidence_expiry_ends_wait_for_unclosed_ordinary_speaker_capture():
    from main_logic.asr_client.speaker_shadow.contracts import SpeakerShadowCandidateKey
    coordinator = VoiceTurnAdmissionCoordinator(evidence_hold_enabled=True)
    binding = binding_for()
    await coordinator.open_turn(binding.turn_token, provider_key=binding.provider_key,
                                speaker_candidate=SpeakerShadowCandidateKey(1, 1, "provider_candidate"))
    effects = await coordinator.post(binding.turn_token, EvidenceHoldRequested(binding, 10.0), now=10.0)
    timer = next(e for e in effects if isinstance(e, ScheduleEvidenceDeadline))
    final = PendingProviderFinal(binding.provider_key, "test", "synthetic", 10.0, 10.2)
    effects = await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0)
    boundary = next(e for e in effects if isinstance(e, ScheduleFinalDeadline))
    await coordinator.post(binding.turn_token, FinalDeadlineExpired(boundary.ticket, 10.2), now=10.2)
    effects = await coordinator.post(binding.turn_token, EvidenceDeadlineExpired(timer.ticket, 12.0), now=12.0)
    assert [e.disposition for e in resolutions(effects)] == [AdmissionDisposition.FORWARD]


@pytest.mark.asyncio
async def test_default_disabled_preserves_existing_final_admission():
    coordinator, binding, final, request = await held_turn(enabled=False)
    assert not any(isinstance(e, ScheduleEvidenceDeadline) for e in request)
    assert (await coordinator.get_record(binding.turn_token)).evidence_hold is None
    effects = await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0)
    assert len(resolutions(effects)) == 1


@pytest.mark.asyncio
async def test_duplicate_final_or_request_never_refreshes_deadline():
    coordinator, binding, final, effects = await held_turn()
    timer = next(e for e in effects if isinstance(e, ScheduleEvidenceDeadline))
    await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0)
    await coordinator.post(binding.turn_token, EvidenceHoldRequested(binding, 11.0), now=11.0)
    await coordinator.post(binding.turn_token, ProviderFinalReceived(replace(final, received_at=11.0, admission_deadline=11.2)), now=11.0)
    record = await coordinator.get_record(binding.turn_token)
    assert record.evidence_hold.absolute_deadline == 12.0
    assert record.evidence_hold.ticket == timer.ticket
    assert record.pending_final.received_at == 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["observed", "empty", "revision", "ticket", "transport"])
async def test_handcrafted_or_wrong_source_proof_does_not_authorize(bad):
    coordinator, binding, final, effects = await held_turn()
    ticket = next(e.ticket for e in effects if isinstance(e, ScheduleEvidenceDeadline))
    await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0)
    proof = evaluate_coverage(binding, (score_for(binding),), mode=EvidenceMode.AUTHORITATIVE)
    if bad == "observed":
        proof = replace(proof, mode=EvidenceMode.OBSERVE)
    elif bad == "empty":
        proof = EvidenceProof(binding, EvidenceStatus.VERIFIED, "forged", EvidenceMode.AUTHORITATIVE)
    elif bad == "revision":
        proof = replace(proof, binding=replace(binding, window=replace(binding.window, revision=2)))
    elif bad == "ticket":
        ticket = replace(ticket, operation_nonce=ticket.operation_nonce + 1)
    else:
        other = replace(binding.target_range, transport_generation=4)
        proof = replace(proof, binding=replace(binding, window=replace(binding.window, audio_range=other), target_range=other))
    effects = await coordinator.post(binding.turn_token, EvidenceHoldResolved(ticket, proof), now=10.1)
    assert not resolutions(effects)
    assert (await coordinator.get_record(binding.turn_token)).evidence_hold.status is EvidenceStatus.PENDING


@pytest.mark.asyncio
async def test_late_valid_deny_drops_only_its_final_after_capability_deadline():
    coordinator, binding, final, effects = await held_turn()
    timer = next(e for e in effects if isinstance(e, ScheduleEvidenceDeadline))
    effects = await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0)
    boundary = next(e for e in effects if isinstance(e, ScheduleFinalDeadline))
    await coordinator.post(binding.turn_token, FinalDeadlineExpired(boundary.ticket, 10.2), now=10.2)
    denied = evaluate_coverage(binding, (score_for(binding, EvidenceStatus.DENY),), mode=EvidenceMode.AUTHORITATIVE)
    effects = await coordinator.post(binding.turn_token, EvidenceHoldResolved(timer.ticket, denied), now=10.3)
    assert [e.disposition for e in resolutions(effects)] == [AdmissionDisposition.DROP]
    assert not any(isinstance(e, AbortProviderTransport) for e in effects)
    allowed = evaluate_coverage(binding, (score_for(binding),), mode=EvidenceMode.AUTHORITATIVE)
    later = await coordinator.post(binding.turn_token, EvidenceHoldResolved(timer.ticket, allowed), now=10.4)
    assert not resolutions(later)
    assert (await coordinator.get_record(binding.turn_token)).terminal_disposition is AdmissionDisposition.DROP


@pytest.mark.asyncio
async def test_expired_or_reset_hold_does_not_revive_on_late_score():
    coordinator, binding, final, effects = await held_turn()
    timer = next(e for e in effects if isinstance(e, ScheduleEvidenceDeadline))
    await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0)
    await coordinator.post(binding.turn_token, Reset(), now=10.1)
    proof = evaluate_coverage(binding, (score_for(binding),), mode=EvidenceMode.AUTHORITATIVE)
    assert not resolutions(await coordinator.post(binding.turn_token, EvidenceHoldResolved(timer.ticket, proof), now=10.2))
    assert (await coordinator.get_record(binding.turn_token)).terminal_disposition is AdmissionDisposition.ABANDON


@pytest.mark.asyncio
async def test_evidence_hold_uses_existing_eight_slots_and_twenty_turns_settle():
    coordinator = VoiceTurnAdmissionCoordinator(evidence_hold_enabled=True)
    for key in range(1, 21):
        _, binding, final, effects = await held_turn(coordinator=coordinator, key=key)
        timer = next(e for e in effects if isinstance(e, ScheduleEvidenceDeadline))
        await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0)
        effects = await coordinator.post(binding.turn_token, EvidenceDeadlineExpired(timer.ticket, 12.0), now=12.0)
        resolution = resolutions(effects)[0]
        for event_type in (CoreSettled, TransportSettled, LifecycleSettled):
            await coordinator.post(binding.turn_token, event_type(resolution.ticket), now=12.0)
        assert await coordinator.retire(binding.turn_token)
    assert not await coordinator.live_turn_tokens()
    for key in range(21, 29):
        await held_turn(coordinator=coordinator, key=key)
    with pytest.raises(AdmissionCapacityError):
        await held_turn(coordinator=coordinator, key=29)


@pytest.mark.asyncio
async def test_exact_ingress_does_not_bypass_hold_and_generic_timer_can_finish_it():
    from main_logic.asr_client.admission.contracts import (
        ExactIntervalOutcome, SpeakerLeaseCaptureClosed, SpeakerLeaseHigh,
    )
    from main_logic.asr_client.admission.ingress import AdmissionIngressLane
    coordinator, receipt, candidate, binding, clock = await exact_held_turn()
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    try:
        requested = await lane.post_exact_interval(receipt, EvidenceHoldRequested(binding, 10.0))
        timer = next(e for e in requested.effects if isinstance(e, ScheduleEvidenceDeadline))
        final = PendingProviderFinal(binding.provider_key, "test", "synthetic", 10.0, 10.2)
        final_result = await lane.post_exact_interval(receipt, ProviderFinalReceived(final))
        boundary = next(e for e in final_result.effects if isinstance(e, ScheduleFinalDeadline))
        await lane.post_exact_interval(receipt, SpeakerLeaseHigh(candidate, 1))
        ready = await lane.post_exact_interval(receipt, SpeakerLeaseCaptureClosed(candidate, 1))
        assert ready.outcome is ExactIntervalOutcome.HELD
        assert not resolutions(ready.effects)
        clock[0] = 10.2
        effects = await lane.post(binding.turn_token, FinalDeadlineExpired(boundary.ticket, 10.2))
        assert not resolutions(effects)
        clock[0] = 12.0
        effects = await lane.post(binding.turn_token, EvidenceDeadlineExpired(timer.ticket, 12.0))
        assert [e.disposition for e in resolutions(effects)] == [AdmissionDisposition.FORWARD]
        assert not any(isinstance(e, AbortProviderTransport) for e in effects)
        repeated = await lane.post_exact_interval(receipt, ProviderFinalReceived(final))
        assert repeated.outcome is ExactIntervalOutcome.STALE
    finally:
        await lane.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_outcome", ["timeout", "reset", "deny"])
async def test_later_ready_final_waits_for_predecessor_and_releases_in_key_order(first_outcome):
    coordinator, first, final1, first_effects = await held_turn()
    _, second, final2, second_effects = await held_turn(coordinator=coordinator, key=2)
    first_timer = next(e for e in first_effects if isinstance(e, ScheduleEvidenceDeadline))
    second_timer = next(e for e in second_effects if isinstance(e, ScheduleEvidenceDeadline))
    await coordinator.post(first.turn_token, ProviderFinalReceived(final1), now=10.0)
    await coordinator.post(second.turn_token, ProviderFinalReceived(final2), now=10.0)
    high = evaluate_coverage(second, (score_for(second),), mode=EvidenceMode.AUTHORITATIVE)
    ready = await coordinator.post(second.turn_token, EvidenceHoldResolved(second_timer.ticket, high), now=10.1)
    assert not resolutions(ready)
    if first_outcome == "timeout":
        event, now, disposition = EvidenceDeadlineExpired(first_timer.ticket, 12.0), 12.0, AdmissionDisposition.FORWARD
    elif first_outcome == "reset":
        event, now, disposition = Reset(), 10.15, AdmissionDisposition.ABANDON
    else:
        denied = evaluate_coverage(first, (score_for(first, EvidenceStatus.DENY),), mode=EvidenceMode.AUTHORITATIVE)
        event, now, disposition = EvidenceHoldResolved(first_timer.ticket, denied), 10.15, AdmissionDisposition.DROP
    effects = await coordinator.post(first.turn_token, event, now=now)
    assert [(e.turn_token, e.disposition) for e in resolutions(effects)] == [
        (first.turn_token, disposition), (second.turn_token, AdmissionDisposition.FORWARD),
    ]


@pytest.mark.asyncio
async def test_later_exact_ready_cannot_bypass_earlier_exact_hold():
    from main_logic.asr_client.admission.contracts import SpeakerLeaseHigh, SpeakerLeaseCaptureClosed
    coordinator, receipt1, candidate1, first, clock = await exact_held_turn()
    _, receipt2, candidate2, second, _ = await exact_held_turn(coordinator=coordinator, key=2, clock=clock)
    tickets = []
    for receipt, binding in ((receipt1, first), (receipt2, second)):
        requested = await coordinator.post_exact_interval(receipt, EvidenceHoldRequested(binding, 10.0))
        tickets.append(next(e.ticket for e in requested.effects if isinstance(e, ScheduleEvidenceDeadline)))
        await coordinator.post_exact_interval(receipt, ProviderFinalReceived(
            PendingProviderFinal(binding.provider_key, "test", "synthetic", 10.0, 10.2)))
    await coordinator.post_exact_interval(receipt2, SpeakerLeaseHigh(candidate2, 1))
    await coordinator.post_exact_interval(receipt2, SpeakerLeaseCaptureClosed(candidate2, 1))
    proof = evaluate_coverage(second, (score_for(second),), mode=EvidenceMode.AUTHORITATIVE)
    ready = await coordinator.post_exact_interval(receipt2, EvidenceHoldResolved(tickets[1], proof))
    assert not resolutions(ready.effects)
    clock[0] = 12.0
    expired = await coordinator.post_exact_interval(receipt1, EvidenceDeadlineExpired(tickets[0], 12.0),
                                                     authority_is_current=lambda: False)
    assert [e.turn_token for e in resolutions(expired.effects)] == [first.turn_token, second.turn_token]


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor", ["allow", "deny"])
async def test_lease_terminal_fanout_obeys_order_and_separates_successor_effects(predecessor):
    from main_logic.asr_client.admission.contracts import (
        SpeakerCaptureLeaseToken, SpeakerLeaseHigh, SpeakerLeaseLow, SpeakerCheckpointKind,
        SpeakerLeaseCaptureClosed,
    )
    from main_logic.asr_client.speaker_shadow.contracts import SpeakerShadowCandidateKey
    coordinator = VoiceTurnAdmissionCoordinator(evidence_hold_enabled=True)
    turns = []
    for key in (1, 2):
        binding = binding_for(key)
        lease = SpeakerCaptureLeaseToken(2, 2, 3, 4, key)
        candidate = SpeakerShadowCandidateKey(2, key, "provider_candidate")
        await coordinator.open_speaker_lease(lease, candidate)
        await coordinator.attach_turn_to_speaker_lease(binding.turn_token, lease, binding.provider_key)
        final = PendingProviderFinal(binding.provider_key, "test", "synthetic", 10.0, 10.2)
        assert not resolutions(await coordinator.post(binding.turn_token, ProviderFinalReceived(final), now=10.0))
        turns.append((binding, lease, candidate))
    first, lease1, candidate1 = turns[0]
    second, lease2, candidate2 = turns[1]
    await coordinator.post_speaker_lease(lease2, SpeakerLeaseHigh(candidate2, 1), now=10.04)
    ready = await coordinator.post_speaker_lease(lease2, SpeakerLeaseCaptureClosed(candidate2, 1), now=10.05)
    assert not resolutions([e for child in ready.child_results for e in child.effects])
    assert not resolutions(ready.successor_effects)
    if predecessor == "allow":
        await coordinator.post_speaker_lease(lease1, SpeakerLeaseHigh(candidate1, 1), now=10.06)
        event = SpeakerLeaseCaptureClosed(candidate1, 1)
        expected = AdmissionDisposition.FORWARD
    else:
        await coordinator.post_speaker_lease(lease1, SpeakerLeaseLow(candidate1, 1, SpeakerCheckpointKind.FIRST), now=10.06)
        event = SpeakerLeaseLow(candidate1, 2, SpeakerCheckpointKind.SECOND)
        expected = AdmissionDisposition.DROP
    settled = await coordinator.post_speaker_lease(lease1, event, now=10.1)
    assert [(e.turn_token, e.disposition) for e in resolutions(
        [e for child in settled.child_results for e in child.effects]
    )] == [(first.turn_token, expected)]
    assert [(e.turn_token, e.disposition) for e in resolutions(settled.successor_effects)] == [
        (second.turn_token, AdmissionDisposition.FORWARD),
    ]
