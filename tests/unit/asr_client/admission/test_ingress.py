from __future__ import annotations

import asyncio

import pytest

from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.admission.contracts import (
    AdmissionDisposition,
    BoundaryProof,
    BoundaryExact,
    CandidateBound,
    CaptureClosed,
    CoreSettled,
    EvidenceState,
    ExactIntervalOutcome,
    ExactIntervalPromotionScope,
    LifecycleSettled,
    PendingProviderFinal,
    ProviderFinalReceived,
    RejectionCapability,
    RejectionCapabilityKind,
    ResolveReserved,
    RouteReplaced,
    SpeakerCaptureLeaseToken,
    SpeakerAuthorityPending,
    SpeakerAuthorityUnarmed,
    SpeakerCheckpointKind,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseState,
    SpeakerLeaseTerminalClaim,
    SpeakerLeaseTransitionOutcome,
    SpeakerLeaseTransitionReceipt,
    SpeakerLow,
    SpeakerUnavailable,
    TransportSettled,
)
from main_logic.asr_client.admission.coordinator import (
    VoiceTurnAdmissionCoordinator,
)
from main_logic.asr_client.admission.ingress import (
    AdmissionIngressCapacityError,
    AdmissionIngressClosedError,
    AdmissionIngressLane,
)
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCandidateKey,
)
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken


pytestmark = pytest.mark.asyncio


def _token(turn_id: int = 1) -> VoiceTurnToken:
    return VoiceTurnToken(VoiceIngressToken(1, "socket", 2, 3, 4), turn_id)


def _candidate() -> SpeakerShadowCandidateKey:
    return SpeakerShadowCandidateKey(2, 1, "provider_candidate")


def _successor() -> SpeakerShadowCandidateKey:
    return SpeakerShadowCandidateKey(2, 2, "provider_candidate")


def _lease() -> SpeakerCaptureLeaseToken:
    return SpeakerCaptureLeaseToken(1, 2, 3, 4, 5)


def _boundary(token: VoiceTurnToken, capability_id: int) -> BoundaryExact:
    return BoundaryExact(
        RejectionCapability(
            capability_id=capability_id,
            owner_generation=1,
            kind=RejectionCapabilityKind.SEALED,
            turn_token=token,
            candidate=_candidate(),
            provider_key=ProviderUtteranceKey(1, 0, 1),
        )
    )


async def test_post_nowait_preserves_fact_then_completion_fifo():
    coordinator = VoiceTurnAdmissionCoordinator()
    token = _token()
    await coordinator.open_turn(token, speaker_candidate=_candidate())
    lane = AdmissionIngressLane(coordinator, data_capacity=2)
    await lane.start()

    first = lane.post_nowait(
        token,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    second = lane.post_nowait(
        token,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    closed = lane.post_nowait(token, CaptureClosed(_candidate(), 2))
    await first
    await second
    await closed

    record = await coordinator.get_record(token)
    assert record is not None
    assert record.last_speaker_sequence_no == 2
    assert record.evidence_state is EvidenceState.DENY_LATCHED
    await lane.close()


async def test_open_turn_is_ordered_before_immediate_speaker_fact():
    coordinator = VoiceTurnAdmissionCoordinator()
    token = _token()
    lane = AdmissionIngressLane(coordinator, data_capacity=1)
    await lane.start()

    opened = lane.open_turn_nowait(token)
    bound = lane.post_nowait(token, CandidateBound(_candidate()))
    fact = lane.post_nowait(
        token,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )

    await opened
    await bound
    await fact
    record = await coordinator.get_record(token)
    assert record is not None
    assert record.evidence_state is EvidenceState.FIRST_LOW
    await lane.close()


async def test_terminal_settlement_retires_capacity_through_same_fifo():
    coordinator = VoiceTurnAdmissionCoordinator(capacity=1, clock=lambda: 10.0)
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    first = _token(1)

    await lane.open_turn(first)
    effects = await lane.post(
        first,
        ProviderFinalReceived(PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)),
    )
    resolution = next(
        effect for effect in effects if isinstance(effect, ResolveReserved)
    )
    await lane.post(first, CoreSettled(resolution.ticket))
    await lane.post(first, TransportSettled(resolution.ticket))
    assert await lane.retire_turn(first) is False
    await lane.post(first, LifecycleSettled(resolution.ticket))
    assert await lane.retire_turn(first) is True

    await lane.open_turn(_token(2))
    assert await coordinator.get_record(first) is None
    assert await coordinator.get_record(_token(2)) is not None
    await lane.close()


async def test_open_turn_then_bulk_fence_then_fact_is_one_fifo():
    coordinator = VoiceTurnAdmissionCoordinator()
    token = _token()
    lane = AdmissionIngressLane(coordinator)
    await lane.start()

    opened = lane.open_turn_nowait(token)
    fenced = lane.invalidate_all_nowait(RouteReplaced())
    fact = lane.post_nowait(token, SpeakerUnavailable(_candidate(), 1))

    await opened
    bulk = await fenced
    await fact
    assert len(bulk) == 1
    record = await coordinator.get_record(token)
    assert record is not None
    assert record.admission_state.value == "abandoned"
    assert record.evidence_state is EvidenceState.NONE
    await lane.close()


async def test_boundary_overflow_cannot_evict_ordered_speaker_control_facts():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    token = _token()
    arming_token = _token(2)
    await coordinator.open_turn(token, speaker_candidate=_candidate())
    await coordinator.open_turn(arming_token)
    lane = AdmissionIngressLane(coordinator, data_capacity=1)
    await lane.start()

    boundary = lane.post_nowait(token, _boundary(token, 1))
    overflowed = _boundary(token, 2)
    with pytest.raises(
        AdmissionIngressCapacityError,
        match="DATA_CAPACITY_EXHAUSTED",
    ) as error:
        lane.post_nowait(token, overflowed)
    assert error.value.turn_token == token
    assert error.value.event is overflowed

    pending = lane.post_nowait(
        arming_token,
        SpeakerAuthorityPending("generation-a"),
    )
    unarmed = lane.post_nowait(
        arming_token,
        SpeakerAuthorityUnarmed("generation-a"),
    )

    first = lane.post_nowait(
        token,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    second = lane.post_nowait(
        token,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    final = lane.post_nowait(
        token,
        ProviderFinalReceived(PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)),
    )
    await boundary
    await pending
    await unarmed
    await first
    effects = await second
    late_final = await final

    resolution = next(
        effect for effect in effects if isinstance(effect, ResolveReserved)
    )
    assert resolution.disposition is AdmissionDisposition.DROP
    assert not any(isinstance(effect, ResolveReserved) for effect in late_final)
    record = await coordinator.get_record(token)
    assert record is not None and record.evidence_state is EvidenceState.DENY_LATCHED
    arming_record = await coordinator.get_record(arming_token)
    assert arming_record is not None
    assert arming_record.evidence_state is EvidenceState.UNAVAILABLE
    await lane.close()


async def test_bounded_partitions_reserve_two_speaker_fact_slots():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    token = _token()
    await coordinator.open_turn(token, speaker_candidate=_candidate())
    lane = AdmissionIngressLane(
        coordinator,
        data_capacity=1,
        control_capacity=1,
        speaker_control_capacity=2,
    )
    await lane.start()
    await coordinator._lock.acquire()
    try:
        boundary = lane.post_nowait(token, _boundary(token, 1))
        final = lane.post_nowait(
            token,
            ProviderFinalReceived(
                PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
            ),
        )
        first = lane.post_nowait(
            token,
            SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
        )
        second = lane.post_nowait(
            token,
            SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
        )

        assert lane.pending_data_count == lane.data_capacity == 1
        assert lane.pending_control_count == 3
        assert lane.pending_speaker_control_count == 2
        assert lane.speaker_control_capacity == 2
        assert len(lane._items) == 4

        with pytest.raises(
            AdmissionIngressCapacityError,
            match="DATA_CAPACITY_EXHAUSTED",
        ):
            lane.post_nowait(token, _boundary(token, 2))
        with pytest.raises(
            AdmissionIngressCapacityError,
            match="CONTROL_CAPACITY_EXHAUSTED",
        ):
            lane.post_nowait(
                token,
                ProviderFinalReceived(
                    PendingProviderFinal(None, "qwen", "other", 10.0, 10.2)
                ),
            )
        with pytest.raises(
            AdmissionIngressCapacityError,
            match="SPEAKER_CONTROL_CAPACITY_EXHAUSTED",
        ):
            lane.post_nowait(token, SpeakerUnavailable(_candidate(), 3))
    finally:
        coordinator._lock.release()

    await boundary
    await final
    await first
    await second
    assert lane.pending_data_count == 0
    assert lane.pending_control_count == 0
    assert lane.pending_speaker_control_count == 0
    await lane.close()


async def test_failed_item_does_not_stop_single_consumer_and_close_drains():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    token = _token()
    await coordinator.open_turn(token)
    lane = AdmissionIngressLane(coordinator)
    await lane.start()

    missing = lane.post_nowait(
        _token(2),
        ProviderFinalReceived(
            PendingProviderFinal(None, "qwen", "missing", 10.0, 10.2)
        ),
    )
    accepted = lane.post_nowait(
        token,
        ProviderFinalReceived(
            PendingProviderFinal(None, "qwen", "accepted", 10.0, 10.2)
        ),
    )
    with pytest.raises(KeyError):
        await missing
    effects = await accepted
    assert any(isinstance(effect, ResolveReserved) for effect in effects)

    await lane.close()
    with pytest.raises(AdmissionIngressClosedError, match="INGRESS_CLOSED"):
        lane.post_nowait(token, SpeakerUnavailable(_candidate(), 1))
    with pytest.raises(AdmissionIngressClosedError, match="INGRESS_CLOSED"):
        await lane.start()


async def test_identical_control_retry_has_no_effect_execution_ownership():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    token = _token()
    await coordinator.open_turn(token)
    lane = AdmissionIngressLane(coordinator, data_capacity=1)
    await lane.start()
    event = ProviderFinalReceived(
        PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
    )

    first = lane.post_nowait(token, event)
    duplicate = lane.post_nowait(token, event)

    assert duplicate is not first
    assert lane.pending_control_count == 1
    leader_effects = await first
    follower_effects = await duplicate
    assert sum(isinstance(effect, ResolveReserved) for effect in leader_effects) == 1
    assert follower_effects == ()
    assert lane.pending_control_count == 0
    await lane.close()


async def test_control_follower_cancellation_does_not_cancel_effect_owner():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    token = _token()
    await coordinator.open_turn(token)
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    await coordinator._lock.acquire()
    event = ProviderFinalReceived(
        PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
    )
    leader = lane.post_nowait(token, event)
    follower = lane.post_nowait(token, event)

    follower.cancel()
    coordinator._lock.release()
    leader_effects = await leader

    assert follower.cancelled() is True
    assert sum(isinstance(effect, ResolveReserved) for effect in leader_effects) == 1
    await lane.close()


async def test_control_follower_propagates_leader_exception():
    coordinator = VoiceTurnAdmissionCoordinator()
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    token = _token()
    event = SpeakerUnavailable(_candidate(), 1)
    leader = lane.post_nowait(token, event)
    follower = lane.post_nowait(token, event)

    with pytest.raises(KeyError) as leader_error:
        await leader
    with pytest.raises(KeyError) as follower_error:
        await follower

    assert follower_error.value is leader_error.value
    await lane.close()


async def test_identical_bulk_retry_has_no_effect_execution_ownership():
    coordinator = VoiceTurnAdmissionCoordinator()
    token = _token()
    await coordinator.open_turn(token)
    lane = AdmissionIngressLane(coordinator)
    await lane.start()

    leader = lane.invalidate_all_nowait(RouteReplaced())
    follower = lane.invalidate_all_nowait(RouteReplaced())
    leader_results = await leader
    follower_results = await follower

    assert len(leader_results) == 1
    assert (
        sum(
            isinstance(effect, ResolveReserved)
            for result in leader_results
            for effect in result.effects
        )
        == 1
    )
    assert follower_results == ()
    await lane.close()


async def test_bulk_route_fence_is_ordered_with_per_turn_facts():
    coordinator = VoiceTurnAdmissionCoordinator()
    token = _token()
    await coordinator.open_turn(token, speaker_candidate=_candidate())
    lane = AdmissionIngressLane(coordinator, data_capacity=2)
    await lane.start()

    before = lane.post_nowait(
        token,
        SpeakerLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    fenced = lane.invalidate_all_nowait(RouteReplaced())
    after = lane.post_nowait(
        token,
        SpeakerLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    await before
    bulk_results = await fenced
    await after

    assert len(bulk_results) == 1
    assert any(
        isinstance(effect, ResolveReserved)
        and effect.disposition is AdmissionDisposition.ABANDON
        for effect in bulk_results[0].effects
    )
    record = await coordinator.get_record(token)
    assert record is not None
    assert record.admission_state.value == "abandoned"
    assert record.last_speaker_sequence_no == 1
    await lane.close()


async def test_waiter_cancellation_does_not_cancel_accepted_final_ownership():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    token = _token()
    await coordinator.open_turn(token)
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    await coordinator._lock.acquire()
    waiter = asyncio.create_task(
        lane.post(
            token,
            ProviderFinalReceived(
                PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
            ),
        )
    )
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    coordinator._lock.release()
    await lane.close()

    record = await coordinator.get_record(token)
    assert record is not None
    assert record.admission_state.value == "forwarded"


async def test_terminal_claim_commit_is_fifo_ordered_after_intervening_fact():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    claim = await lane.prepare_speaker_lease_transition(
        lease,
        SpeakerLeaseHigh(_candidate(), 2),
    )
    assert claim is not None

    await coordinator._lock.acquire()
    try:
        intervening = lane.post_speaker_lease_nowait(
            lease,
            SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.FIRST),
        )
        committed = lane.commit_speaker_lease_terminal_claim_nowait(claim)
        await asyncio.sleep(0)
        assert lane.pending_speaker_control_count == 2
    finally:
        coordinator._lock.release()

    advanced = await intervening
    stale = await committed
    parent = await coordinator.get_speaker_lease(lease)
    assert advanced.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    assert stale.outcome is SpeakerLeaseTransitionOutcome.STALE
    assert parent is not None
    assert parent.state is SpeakerLeaseState.FIRST_LOW
    assert parent.last_speaker_sequence_no == 2
    await lane.close()


async def test_terminal_claim_waiter_cancellation_preserves_accepted_commit():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    claim = await lane.prepare_speaker_lease_transition(
        lease,
        SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.FIRST),
    )
    assert claim is not None

    await coordinator._lock.acquire()
    waiter = asyncio.create_task(
        lane.commit_speaker_lease_terminal_claim(claim)
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert lane.pending_speaker_control_count == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        coordinator._lock.release()

    await lane.close()
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.state is SpeakerLeaseState.MIXED_DENY_LATCHED
    assert parent.terminal_disposition is AdmissionDisposition.DROP


@pytest.mark.parametrize(
    "events",
    (
        (
            SpeakerLeaseHigh(_candidate(), 1),
            SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.FIRST),
        ),
        (
            SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
            SpeakerLeaseHigh(_candidate(), 2),
        ),
    ),
)
async def test_fifo_prepare_commits_first_fact_before_preparing_mixed_deny(events):
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    lane = AdmissionIngressLane(coordinator)
    await lane.start()

    await coordinator._lock.acquire()
    try:
        first = lane.prepare_speaker_lease_transition_nowait(lease, events[0])
        second = lane.prepare_speaker_lease_transition_nowait(lease, events[1])
        await asyncio.sleep(0)
        assert lane.pending_speaker_control_count == 2
    finally:
        coordinator._lock.release()

    first_result = await first
    second_result = await second
    assert isinstance(first_result, SpeakerLeaseTransitionReceipt)
    assert first_result.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    assert isinstance(second_result, SpeakerLeaseTerminalClaim)
    assert (
        second_result.expected_terminal_state
        is SpeakerLeaseState.MIXED_DENY_LATCHED
    )
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.logical_revision == 1
    await lane.close()


async def test_exact_promotion_is_fifo_after_previously_accepted_parent_fact():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn = _lease(), _token()
    key = ProviderUtteranceKey(1, 0, 1)
    await coordinator.open_speaker_lease(lease, _candidate())
    child = await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    scope = ExactIntervalPromotionScope(
        parent_lease_token=lease,
        parent_record_generation=parent.record_generation,
        expected_parent_logical_revision=parent.logical_revision + 1,
        expected_parent_state=SpeakerLeaseState.HIGH_SEEN,
        turn_token=turn,
        child_record_generation=child.record_generation,
        expected_child_logical_revision=child.logical_revision,
        provider_key=key,
        boundary_proof=BoundaryProof(1, 1, key),
        target_candidate=_candidate(),
        successor_candidate=_successor(),
    )

    await coordinator._lock.acquire()
    try:
        high = lane.prepare_speaker_lease_transition_nowait(
            lease,
            SpeakerLeaseHigh(_candidate(), 1),
        )
        promoted = lane.promote_exact_interval_nowait(scope)
        await asyncio.sleep(0)
        assert lane.pending_speaker_control_count == 2
    finally:
        coordinator._lock.release()

    high_result = await high
    promoted_result = await promoted
    assert isinstance(high_result, SpeakerLeaseTransitionReceipt)
    assert promoted_result.outcome is ExactIntervalOutcome.PROMOTED
    assert promoted_result.receipt is not None
    activated = await lane.activate_exact_interval(promoted_result.receipt)
    assert activated.outcome is ExactIntervalOutcome.ACTIVATED
    record = await coordinator.get_record(turn)
    assert record is not None
    assert record.evidence_state is EvidenceState.ALLOW
    await lane.close()


async def test_exact_final_and_completion_fact_keep_fifo_order():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn = _lease(), _token()
    key = ProviderUtteranceKey(1, 0, 1)
    await coordinator.open_speaker_lease(lease, _candidate())
    child = await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    scope = ExactIntervalPromotionScope(
        parent_lease_token=lease,
        parent_record_generation=parent.record_generation,
        expected_parent_logical_revision=parent.logical_revision,
        expected_parent_state=parent.state,
        turn_token=turn,
        child_record_generation=child.record_generation,
        expected_child_logical_revision=child.logical_revision,
        provider_key=key,
        boundary_proof=BoundaryProof(1, 1, key),
        target_candidate=_candidate(),
        successor_candidate=_successor(),
    )
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    promoted = await lane.promote_exact_interval(scope)
    assert promoted.receipt is not None
    activated = await lane.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None

    await coordinator._lock.acquire()
    try:
        final = lane.post_exact_interval_nowait(
            activated.receipt,
            ProviderFinalReceived(
                PendingProviderFinal(key, "qwen", "deny", 10.0, 10.2)
            ),
        )
        completion = lane.post_exact_interval_nowait(
            activated.receipt,
            SpeakerLeaseLow(
                _candidate(),
                2,
                SpeakerCheckpointKind.COMPLETION_CONFIRMATION,
            ),
        )
        await asyncio.sleep(0)
        assert lane.pending_speaker_control_count == 2
    finally:
        coordinator._lock.release()

    final_result = await final
    completion_result = await completion
    assert final_result.outcome is ExactIntervalOutcome.HELD
    assert completion_result.outcome is ExactIntervalOutcome.RESOLVED
    assert completion_result.disposition is AdmissionDisposition.DROP
    await lane.close()


async def test_exact_post_waiter_cancellation_does_not_revoke_accepted_fact():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn = _lease(), _token()
    key = ProviderUtteranceKey(1, 0, 1)
    await coordinator.open_speaker_lease(lease, _candidate())
    child = await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    scope = ExactIntervalPromotionScope(
        parent_lease_token=lease,
        parent_record_generation=parent.record_generation,
        expected_parent_logical_revision=parent.logical_revision,
        expected_parent_state=parent.state,
        turn_token=turn,
        child_record_generation=child.record_generation,
        expected_child_logical_revision=child.logical_revision,
        provider_key=key,
        boundary_proof=BoundaryProof(1, 1, key),
        target_candidate=_candidate(),
        successor_candidate=_successor(),
    )
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    promoted = await lane.promote_exact_interval(scope)
    assert promoted.receipt is not None
    activated = await lane.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None

    await coordinator._lock.acquire()
    waiter = asyncio.create_task(
        lane.post_exact_interval(
            activated.receipt,
            ProviderFinalReceived(
                PendingProviderFinal(key, "qwen", "accepted", 10.0, 10.2)
            ),
        )
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert lane.pending_speaker_control_count == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        coordinator._lock.release()

    await lane.close()
    record = await coordinator.get_record(turn)
    assert record is not None
    assert record.pending_final is not None
    assert record.pending_final.text == "accepted"
