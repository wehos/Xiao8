from __future__ import annotations

import asyncio

import pytest

from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.admission.contracts import (
    AdmissionDisposition,
    BoundaryProof,
    EvidenceState,
    ExactIntervalOutcome,
    ExactIntervalPromotionScope,
    PendingProviderFinal,
    ProviderFinalReceived,
    ResolveReserved,
    SpeakerCaptureLeaseToken,
    SpeakerCheckpointKind,
    SpeakerLeaseLow,
)
from main_logic.asr_client.admission.coordinator import VoiceTurnAdmissionCoordinator
from main_logic.asr_client.admission.ingress import AdmissionIngressLane
from main_logic.asr_client.speaker_shadow.contracts import SpeakerShadowCandidateKey
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken


pytestmark = pytest.mark.asyncio


class _ObservedLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


async def _exact_with_first_low():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    coordinator._lock = _ObservedLock()
    lease = SpeakerCaptureLeaseToken(1, 2, 3, 4, 5)
    turn = VoiceTurnToken(VoiceIngressToken(1, "socket", 2, 3, 4), 1)
    key = ProviderUtteranceKey(1, 0, 1)
    target = SpeakerShadowCandidateKey(2, 1, "provider_candidate")
    successor = SpeakerShadowCandidateKey(2, 2, "provider_candidate")
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    await coordinator.post_speaker_lease(
        lease, SpeakerLeaseLow(target, 1, SpeakerCheckpointKind.FIRST)
    )
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        ExactIntervalPromotionScope(
            parent_lease_token=lease,
            parent_record_generation=parent.record_generation,
            expected_parent_logical_revision=parent.logical_revision,
            expected_parent_state=parent.state,
            turn_token=turn,
            child_record_generation=child.record_generation,
            expected_child_logical_revision=child.logical_revision,
            provider_key=key,
            boundary_proof=BoundaryProof(1, 1, key),
            target_candidate=target,
            successor_candidate=successor,
        )
    )
    assert promoted.receipt is not None
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None
    return coordinator, activated.receipt, target, turn, key


async def _assert_queued_low_cannot_cross_retirement(*, mutate_old_order=False):
    coordinator, receipt, target, turn, key = await _exact_with_first_low()
    if mutate_old_order:
        original = coordinator.post_exact_interval

        async def without_writer_fence(receipt, event, *, authority_is_current=None):
            return await original(receipt, event)

        coordinator.post_exact_interval = without_writer_fence
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    permitted = True
    checks = []

    def guard():
        checks.append((coordinator._lock.locked(), permitted))
        return permitted

    try:
        await coordinator._lock.acquire()
        try:
            queued = lane.post_exact_interval_nowait(
                receipt,
                SpeakerLeaseLow(target, 2, SpeakerCheckpointKind.SECOND),
                authority_is_current=guard,
            )
            await asyncio.wait_for(coordinator._lock.waiting.wait(), 1.0)
            permitted = False
        finally:
            coordinator._lock.release()
        held = await queued
        assert held.outcome is ExactIntervalOutcome.HELD
        child = await coordinator.get_record(turn)
        assert child is not None
        assert child.evidence_state is EvidenceState.FIRST_LOW
        assert child.last_speaker_sequence_no == 1
        assert checks == [(True, False)]
        unavailable = await lane.fail_exact_interval_unavailable(receipt)
        assert unavailable.outcome is ExactIntervalOutcome.ABORTED
        effects = await lane.post(
            turn, ProviderFinalReceived(PendingProviderFinal(key, "qwen", "retained", 10.0, 10.2))
        )
        resolved = [effect for effect in effects if isinstance(effect, ResolveReserved)]
        assert len(resolved) == 1
        assert resolved[0].ticket.disposition is AdmissionDisposition.FORWARD
    finally:
        await lane.close()


@pytest.mark.parametrize("iteration", range(50))
async def test_queued_low_revalidates_authority_inside_writer_lock(iteration):
    await _assert_queued_low_cannot_cross_retirement()


async def test_mutation_without_writer_fence_is_detected():
    with pytest.raises(AssertionError):
        await _assert_queued_low_cannot_cross_retirement(mutate_old_order=True)


@pytest.mark.parametrize("iteration", range(50))
async def test_formal_deny_cannot_be_revoked_by_installation_guard(iteration):
    coordinator, receipt, target, _, key = await _exact_with_first_low()
    await coordinator.post_exact_interval(
        receipt, SpeakerLeaseLow(target, 2, SpeakerCheckpointKind.SECOND)
    )
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    try:
        final = await lane.post_exact_interval(
            receipt,
            ProviderFinalReceived(PendingProviderFinal(key, "qwen", "denied", 10.0, 10.2)),
            authority_is_current=lambda: False,
        )
        assert final.outcome is ExactIntervalOutcome.RESOLVED
        assert final.disposition is AdmissionDisposition.DROP
    finally:
        await lane.close()


async def test_guard_failure_holds_without_consuming_fact():
    coordinator, receipt, target, turn, _ = await _exact_with_first_low()

    def failed_guard():
        raise RuntimeError("installation snapshot unavailable")

    held = await coordinator.post_exact_interval(
        receipt,
        SpeakerLeaseLow(target, 2, SpeakerCheckpointKind.SECOND),
        authority_is_current=failed_guard,
    )
    assert held.outcome is ExactIntervalOutcome.HELD
    child = await coordinator.get_record(turn)
    assert child is not None and child.last_speaker_sequence_no == 1
