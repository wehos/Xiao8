from __future__ import annotations

import pytest

from main_logic.asr_client.admission.contracts import (
    AbortProviderTransport,
    AdmissionDisposition,
    AdmissionState,
    CoreSettled,
    LifecycleSettled,
    PendingProviderFinal,
    ProviderFinalReceived,
    ResolveReserved,
    RouteReplaced,
    SpeakerCaptureLeaseToken,
    SpeakerCheckpointKind,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseLow,
    SpeakerLeaseState,
    SpeakerLeaseTransitionOutcome,
    TransportSettled,
)
from main_logic.asr_client.admission.coordinator import (
    AdmissionCapacityError,
    AdmissionIdentityError,
    VoiceTurnAdmissionCoordinator,
)
from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.speaker_shadow.contracts import SpeakerShadowCandidateKey
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken


pytestmark = pytest.mark.asyncio


def _token(turn_id: int) -> VoiceTurnToken:
    return VoiceTurnToken(VoiceIngressToken(1, "socket", 2, 3, 4), turn_id)


def _lease() -> SpeakerCaptureLeaseToken:
    return SpeakerCaptureLeaseToken(1, 2, 3, 4, 1)


def _candidate() -> SpeakerShadowCandidateKey:
    return SpeakerShadowCandidateKey(5, 6, "provider_candidate")


def _provider_key(utterance_id: int) -> ProviderUtteranceKey:
    return ProviderUtteranceKey(1, 0, utterance_id)


async def test_post_reduces_under_single_writer_and_returns_effects_without_awaiting_them():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    token = _token(1)
    await coordinator.open_turn(token)
    effects = await coordinator.post(
        token,
        ProviderFinalReceived(
            PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
        ),
    )
    resolution = next(effect for effect in effects if isinstance(effect, ResolveReserved))
    record = await coordinator.get_record(token)
    assert resolution.disposition is AdmissionDisposition.FORWARD
    assert record is not None and record.admission_state is AdmissionState.FORWARDED


async def test_record_capacity_failure_is_explicit_not_silent():
    coordinator = VoiceTurnAdmissionCoordinator(capacity=1)
    await coordinator.open_turn(_token(1))
    with pytest.raises(AdmissionCapacityError, match="ASR_ADMISSION_CAPACITY_EXHAUSTED"):
        await coordinator.open_turn(_token(2))


async def test_retire_requires_all_three_settlements_for_same_resolution():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    token = _token(1)
    await coordinator.open_turn(token)
    effects = await coordinator.post(
        token,
        ProviderFinalReceived(
            PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
        ),
    )
    resolution = next(effect for effect in effects if isinstance(effect, ResolveReserved))
    assert await coordinator.retire(token) is False

    await coordinator.post(token, CoreSettled(resolution.ticket))
    await coordinator.post(token, TransportSettled(resolution.ticket))
    assert await coordinator.retire(token) is False
    await coordinator.post(token, LifecycleSettled(resolution.ticket))
    assert await coordinator.retire(token) is True
    assert await coordinator.get_record(token) is None

    with pytest.raises(AdmissionIdentityError, match="TURN_ALREADY_RETIRED"):
        await coordinator.open_turn(token)


async def test_reopening_live_token_cannot_silently_add_or_replace_aliases():
    coordinator = VoiceTurnAdmissionCoordinator()
    token = _token(1)
    await coordinator.open_turn(token)
    with pytest.raises(AdmissionIdentityError, match="ALIAS_CONFLICT"):
        await coordinator.open_turn(token, provider_key=ProviderUtteranceKey(1, 0, 1))


async def test_live_snapshot_and_bulk_route_replacement_are_atomic_and_ordered():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 12.5)
    first, second = _token(1), _token(2)
    await coordinator.open_turn(first)
    await coordinator.open_turn(second)

    assert await coordinator.live_turn_tokens() == (first, second)
    results = await coordinator.invalidate_all(RouteReplaced())

    assert tuple(result.turn_token for result in results) == (first, second)
    assert all(
        any(
            isinstance(effect, ResolveReserved)
            and effect.disposition is AdmissionDisposition.ABANDON
            for effect in result.effects
        )
        for result in results
    )
    assert (await coordinator.get_record(first)).admission_state is AdmissionState.ABANDONED
    assert (await coordinator.get_record(second)).admission_state is AdmissionState.ABANDONED


async def test_bulk_invalidation_rejects_non_route_control_event():
    coordinator = VoiceTurnAdmissionCoordinator()
    with pytest.raises(TypeError, match="Reset, Close, or RouteReplaced"):
        await coordinator.invalidate_all(  # type: ignore[arg-type]
            ProviderFinalReceived(
                PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
            )
        )


async def test_terminal_deny_fanout_carries_parent_state_and_exact_child_tickets():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    first, second = _token(1), _token(2)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(first, lease, _provider_key(1))
    await coordinator.attach_turn_to_speaker_lease(second, lease, _provider_key(2))

    first_receipt = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    assert first_receipt.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    receipt = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert receipt.outcome is SpeakerLeaseTransitionOutcome.APPLIED
    results = receipt.child_results

    assert tuple(result.turn_token for result in results) == (first, second)
    assert all(result.speaker_lease_token == lease for result in results)
    assert all(
        result.speaker_lease_terminal_state is SpeakerLeaseState.DENY_LATCHED
        for result in results
    )
    aborts = [
        next(
            effect
            for effect in result.effects
            if isinstance(effect, AbortProviderTransport)
        )
        for result in results
    ]
    resolutions = [
        next(effect for effect in result.effects if isinstance(effect, ResolveReserved))
        for result in results
    ]
    assert tuple(abort.ticket for abort in aborts) == tuple(
        resolution.ticket for resolution in resolutions
    )
    assert all(abort.speaker_lease_token == lease for abort in aborts)
    assert {abort.turn_token for abort in aborts} == {first, second}
    assert len({abort.record_generation for abort in aborts}) == 2


async def test_first_low_then_capture_close_remains_fail_open_with_parent_metadata():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _token(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(turn, lease, _provider_key(1))
    await coordinator.post(
        turn,
        ProviderFinalReceived(
            PendingProviderFinal(_provider_key(1), "qwen", "hello", 10.0, 10.2)
        ),
    )

    first_receipt = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    assert first_receipt.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    receipt = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseCaptureClosed(_candidate(), 1),
    )
    results = receipt.child_results

    assert len(results) == 1
    assert results[0].speaker_lease_token == lease
    assert results[0].speaker_lease_terminal_state is SpeakerLeaseState.UNAVAILABLE
    resolution = next(
        effect for effect in results[0].effects if isinstance(effect, ResolveReserved)
    )
    assert resolution.disposition is AdmissionDisposition.FORWARD
    assert not any(
        isinstance(effect, AbortProviderTransport) for effect in results[0].effects
    )
