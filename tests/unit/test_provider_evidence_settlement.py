"""Physical retirement is explicit, retained, and fenced to issued handles."""

import asyncio
from dataclasses import replace

import pytest

from main_logic.asr_client.endpointing.detector_runtime import (
    ProviderAudioAccountingReceipt,
    ProviderSpeakerEvidenceSettlementStatus as Status,
)
from tests.unit import test_asr_detector_runtime as support


async def _opened():
    shadow = support.SpeakerShadowRuntime(
        backend_factory=support._LowScoreSpeakerBackendFactory(),
        config=support._provider_speaker_config(),
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
    lease = await detector.ensure_provider_speaker_evidence_lease()
    assert lease is not None
    return detector, shadow, identity, lease


async def _account(detector, identity, lease, sequence_no):
    return await detector.observe_provider_audio_ordered(
        support._speaker_pcm(100), sample_rate_hz=16000, identity=identity,
        sequence_no=sequence_no, split_before_audio=False,
        accounting_only=True, evidence_complete=False, speaker_evidence_lease=lease,
    )


@pytest.mark.asyncio
async def test_accounting_retirement_confirmation_and_successor_fence():
    detector, _, identity, lease = await _opened()
    try:
        receipt = await _account(detector, identity, lease, 1)
        assert type(receipt) is ProviderAudioAccountingReceipt
        settlement = receipt.evidence_settlement
        assert settlement.status is Status.RETIRED
        assert settlement.reason == "evidence_unavailable"
        assert detector._provider_speaker_evidence_state_for(lease) is None
        assert detector.validate_provider_speaker_evidence_settlement(settlement, lease=lease)
        confirmed = await detector.confirm_provider_speaker_evidence_retirement(lease)
        assert confirmed.status is Status.ALREADY_RETIRED
        assert confirmed.operation_serial == settlement.operation_serial
        assert detector.validate_provider_speaker_evidence_settlement(confirmed, lease=lease)
        repeat = await _account(detector, identity, lease, 2)
        assert repeat.evidence_settlement is confirmed
        assert repeat.start_sample_16k == receipt.end_sample_16k
        successor = await detector.ensure_provider_speaker_evidence_lease()
        assert successor is not lease
        for stale_lease in (None, lease, replace(successor)):
            assert await _account(detector, identity, stale_lease, 3) is None
            assert detector._provider_speaker_evidence_state_for(successor) is not None
        assert await detector.confirm_provider_speaker_evidence_retirement(lease) is confirmed
        assert not await detector.finish_provider_speaker_evidence_lease(lease)
        assert detector._provider_speaker_evidence_state_for(successor) is not None
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_settlement_rejects_copies_other_detector_and_timeline_reset():
    detector, _, identity, lease = await _opened()
    other, _, _, _ = await _opened()
    try:
        assert detector._provider_speaker_evidence_state_for(replace(lease)) is None
        assert not await detector.abandon_provider_speaker_evidence_lease(replace(lease))
        receipt = await _account(detector, identity, lease, 1)
        settlement = receipt.evidence_settlement
        assert not detector.validate_provider_speaker_evidence_settlement(replace(settlement), lease=lease)
        assert not detector.validate_provider_speaker_evidence_settlement(settlement, lease=replace(lease))
        assert not other.validate_provider_speaker_evidence_settlement(settlement, lease=lease)
        await detector.reset_provider_audio_timeline()
        assert not detector.validate_provider_speaker_evidence_settlement(settlement, lease=lease)
        assert (await detector.confirm_provider_speaker_evidence_retirement(lease)).status is Status.UNPROVEN
    finally:
        await detector.close()
        await other.close()


@pytest.mark.asyncio
async def test_lost_accounting_return_can_be_confirmed_without_pcm_replay():
    detector, _, identity, lease = await _opened()
    completed = asyncio.Event()
    parked = asyncio.Event()

    async def caller():
        await _account(detector, identity, lease, 1)
        completed.set()
        await parked.wait()

    try:
        task = asyncio.create_task(caller())
        await asyncio.wait_for(completed.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cursor = detector._provider_audio_sample_cursor_16k
        confirmed = await detector.confirm_provider_speaker_evidence_retirement(lease)
        assert confirmed.status is Status.ALREADY_RETIRED
        assert detector.validate_provider_speaker_evidence_settlement(confirmed, lease=lease)
        assert detector._provider_audio_sample_cursor_16k == cursor == 1600
        assert detector._provider_segment_last_sequence_no == 1
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_retirement_records_are_bounded_and_absence_does_not_prove_retirement():
    detector, _, identity, lease = await _opened()
    try:
        first = lease
        first_receipt = await _account(detector, identity, lease, 1)
        for sequence in range(2, 22):
            lease = await detector.ensure_provider_speaker_evidence_lease()
            assert lease is not None
            assert (await detector.confirm_provider_speaker_evidence_retirement(lease)).status is Status.LIVE
            assert await _account(detector, identity, lease, sequence) is not None
            assert len(detector._provider_speaker_evidence_settlements) <= 8
        assert detector._provider_speaker_evidence_state is None
        assert (await detector.confirm_provider_speaker_evidence_retirement(first)).status is Status.UNPROVEN
        assert not detector.validate_provider_speaker_evidence_settlement(first_receipt.evidence_settlement, lease=first)
        assert await _account(detector, identity, first, 22) is None
        assert detector._provider_segment_last_sequence_no == 21
    finally:
        await detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["abandon", "finish", "expire"])
async def test_all_same_timeline_physical_retirements_are_confirmable(action):
    detector, shadow, identity, lease = await _opened()
    try:
        await detector.observe_provider_audio_ordered(
            support._speaker_pcm(100), sample_rate_hz=16000,
            identity=identity, sequence_no=1, split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        if action == "abandon":
            await detector.abandon_provider_speaker_evidence_lease(lease)
        elif action == "finish":
            # A short deferred candidate may reject finish. Its physical handle
            # still retires; the bool describes observer completion separately.
            await detector.finish_provider_speaker_evidence_lease(lease)
        else:
            state = detector._provider_speaker_evidence_state_for(lease)
            detector._expire_provider_segments(state.last_progress_at + 100)
        assert detector._provider_speaker_evidence_state_for(lease) is None
        confirmed = await detector.confirm_provider_speaker_evidence_retirement(lease)
        assert confirmed.status is Status.ALREADY_RETIRED
        assert detector.validate_provider_speaker_evidence_settlement(confirmed, lease=lease)
        await detector.close()
        assert not detector.validate_provider_speaker_evidence_settlement(confirmed, lease=lease)
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_shadow_abandon_false_does_not_hide_physical_retirement():
    detector, shadow, _, lease = await _opened()
    try:
        await shadow.close()
        assert not await detector.abandon_provider_speaker_evidence_lease(lease)
        assert detector._provider_speaker_evidence_state_for(lease) is None
        confirmed = await detector.confirm_provider_speaker_evidence_retirement(lease)
        assert confirmed.status is Status.ALREADY_RETIRED
        assert detector.validate_provider_speaker_evidence_settlement(confirmed, lease=lease)
    finally:
        await detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("accounted_frames", [0, 2])
async def test_empty_new_evidence_anchor_retains_canonical_observation_fence(accounted_frames):
    detector, _, identity, lease = await _opened()
    try:
        if accounted_frames:
            for sequence in range(1, accounted_frames + 1):
                assert await _account(detector, identity, lease, sequence) is not None
            lease = await detector.ensure_provider_speaker_evidence_lease()
        result = await detector.anchor_provider_speaker_evidence(
            lease, audio_start_sample_16k=accounted_frames * 1600,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        assert result.status is support.ProviderSpeakerEvidenceAnchorStatus.APPLIED
        assert result.pcm_through_sequence_no == accounted_frames
        state = detector._provider_speaker_evidence_state_for(lease)
        assert state.cumulative_sample_count == 0
        update = await detector.observe_provider_audio_ordered(
            support._speaker_pcm(100), sample_rate_hz=16000, identity=identity,
            sequence_no=accounted_frames + 1, split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        assert update.capture.accepted_sample_count == 1600
        assert detector._provider_audio_sample_cursor_16k == (accounted_frames + 1) * 1600
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_empty_timeline_anchor_does_not_constrain_first_dispatch_sequence():
    detector, _, identity, lease = await _opened()
    try:
        await support._anchor_provider_evidence(detector, lease)
        update = await detector.observe_provider_audio_ordered(
            support._speaker_pcm(100), sample_rate_hz=16000, identity=identity,
            sequence_no=17, split_before_audio=False, speaker_evidence_lease=lease,
        )
        assert update.capture.accepted_sample_count == 1600
        assert detector._provider_speaker_evidence_state_for(lease) is not None
    finally:
        await detector.close()
