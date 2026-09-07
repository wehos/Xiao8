"""Continuous exact intervals use real Detector and Shadow transactions."""

from functools import partial

import pytest

from tests.unit import test_asr_detector_runtime as support


class _ScoreBackend(support._LowScoreSpeakerBackend):
    def __init__(self, score: float) -> None:
        self.value = score

    def score(self, _pcm16: bytes, _sample_rate_hz: int) -> float:
        return self.value


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0.2, 0.95])
@pytest.mark.parametrize("count", [3, 20])
async def test_consecutive_exact_generations_and_completed_capacity(score, count):
    evidence = []
    shadow = support.SpeakerShadowRuntime(
        backend_factory=partial(_ScoreBackend, score),
        config=support._provider_speaker_config(), on_evidence=evidence.append,
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        first_completed = None
        for index in range(count):
            assert lease is not None
            await detector.observe_provider_audio_ordered(
                support._speaker_pcm(1600), sample_rate_hz=16000,
                identity=identity, sequence_no=index + 1, split_before_audio=False,
                speaker_evidence_lease=lease,
            )
            await support._anchor_provider_evidence(
                detector, lease, start_sample_16k=index * 25600,
            )
            reservation = await detector.prepare_provider_exact_speaker_interval(
                support.ProviderAudioRange(index * 25600, (index + 1) * 25600),
                speaker_evidence_lease=lease,
            )
            assert reservation is not None
            assert reservation.candidate_generation == index
            committed = detector.commit_provider_exact_speaker_interval(reservation)
            assert committed is not None
            assert detector._candidate_generation == index + 1
            assert detector.commit_provider_exact_speaker_interval(reservation) is None
            lease = committed.successor_evidence_lease
            await shadow.wait_idle()
            assert await detector.complete_provider_speaker_boundary(
                committed.snapshot, successor_evidence_lease=lease,
            ) == "completed"
            assert await detector.complete_provider_speaker_boundary(
                committed.snapshot, successor_evidence_lease=lease,
            ) == "already_completed"
            first_completed = first_completed or (committed.snapshot, lease)
            assert len(detector._provider_preseal_entries) <= 8
            assert not detector._provider_boundary_completion_entries
        observations = [e for e in evidence if isinstance(e, support.SpeakerShadowObservation)]
        assert len(observations) >= count
        assert all(e.similarity == score for e in observations)
        if count == 20:
            assert await detector.complete_provider_speaker_boundary(
                first_completed[0], successor_evidence_lease=first_completed[1],
            ) == "stale"
        assert detector._provider_speaker_evidence_state_for(lease) is not None
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_pending_exact_proofs_are_not_evicted_for_capacity():
    shadow = support.SpeakerShadowRuntime(
        backend_factory=support._LowScoreSpeakerBackendFactory(),
        config=support._provider_speaker_config(),
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        snapshots = []
        for index in range(9):
            await detector.observe_provider_audio_ordered(
                support._speaker_pcm(800), sample_rate_hz=16000, identity=identity,
                sequence_no=index + 1, split_before_audio=False, speaker_evidence_lease=lease,
            )
            await support._anchor_provider_evidence(detector, lease, start_sample_16k=index * 12800)
            reservation = await detector.prepare_provider_exact_speaker_interval(
                support.ProviderAudioRange(index * 12800, (index + 1) * 12800),
                speaker_evidence_lease=lease,
            )
            if index == 8:
                assert reservation is None
                break
            assert reservation is not None
            committed = detector.commit_provider_exact_speaker_interval(reservation)
            assert committed is not None
            lease = committed.successor_evidence_lease
            snapshots.append((committed.snapshot, lease))
            await shadow.wait_idle()
        assert detector._candidate_generation == 8
        assert len(detector._provider_preseal_entries) == 8
        assert len(detector._provider_boundary_completion_entries) == 8
        assert detector._provider_speaker_evidence_state_for(lease) is not None
        for snapshot, successor in snapshots:
            assert await detector.complete_provider_speaker_boundary(
                snapshot, successor_evidence_lease=successor,
            ) == "completed"
        reservation = await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(8 * 12800, 9 * 12800), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert reservation.candidate_generation == 8
        assert detector.abort_provider_exact_speaker_interval(reservation)
        assert detector._candidate_generation == 8
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_exact_then_unknown_seal_then_exact_advances_only_at_consumption():
    shadow = support.SpeakerShadowRuntime(
        backend_factory=support._LowScoreSpeakerBackendFactory(),
        config=support._provider_speaker_config(),
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        await detector.observe_provider_audio_ordered(
            support._speaker_pcm(800), sample_rate_hz=16000, identity=identity,
            sequence_no=1, split_before_audio=False, speaker_evidence_lease=lease,
        )
        await support._anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(0, 12800), speaker_evidence_lease=lease,
        )
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        await shadow.wait_idle()
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=committed.successor_evidence_lease,
        ) == "completed"
        unknown = await detector.retire_provider_speaker_boundary_unknown()
        assert unknown.candidate_generation == 1
        assert detector._candidate_generation == 1
        fence = await detector.seal_provider_candidate(speaker_snapshot=unknown)
        assert fence.candidate_generation == 1
        assert detector._candidate_generation == 2
        assert await detector.seal_provider_candidate(speaker_snapshot=unknown) is fence
        assert detector._candidate_generation == 2
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=2)
        old = committed.successor_evidence_lease
        await detector.abandon_provider_speaker_evidence_lease(old)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        await detector.observe_provider_audio_ordered(
            support._speaker_pcm(800), sample_rate_hz=16000, identity=identity,
            sequence_no=2, split_before_audio=False, speaker_evidence_lease=lease,
        )
        await support._anchor_provider_evidence(detector, lease, start_sample_16k=12800)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(12800, 25600), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert reservation.candidate_generation == 2
        assert detector.commit_provider_exact_speaker_interval(reservation) is not None
        assert detector._candidate_generation == 3
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_abort_then_retry_uses_new_reservation_without_reusing_shadow_identity():
    shadow = support.SpeakerShadowRuntime(
        backend_factory=support._LowScoreSpeakerBackendFactory(),
        config=support._provider_speaker_config(),
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        await detector.observe_provider_audio_ordered(
            support._speaker_pcm(800), sample_rate_hz=16000, identity=identity,
            sequence_no=1, split_before_audio=False, speaker_evidence_lease=lease,
        )
        await support._anchor_provider_evidence(detector, lease)
        boundary = support.ProviderAudioRange(0, 12800)
        first = await detector.prepare_provider_exact_speaker_interval(boundary, speaker_evidence_lease=lease)
        assert first is not None
        await detector.observe_provider_audio_ordered(
            support._speaker_pcm(100), sample_rate_hz=16000, identity=identity,
            sequence_no=2, split_before_audio=True, speaker_evidence_lease=lease,
        )
        assert detector.abort_provider_exact_speaker_interval(first)
        assert not detector.abort_provider_exact_speaker_interval(first)
        assert detector._candidate_generation == 0
        await shadow.wait_idle()
        second = await detector.prepare_provider_exact_speaker_interval(boundary, speaker_evidence_lease=lease)
        assert second is not None
        assert second._token is not first._token
        assert second.candidate_generation == first.candidate_generation == 0
        assert second.suffix_candidate.shadow_generation > first.suffix_candidate.shadow_generation
        assert detector.commit_provider_exact_speaker_interval(first) is None
        committed = detector.commit_provider_exact_speaker_interval(second)
        assert committed is not None
        assert detector._candidate_generation == 1
        successor = committed.successor_evidence_lease
        assert detector._provider_speaker_evidence_state_for(successor).cumulative_sample_count == 1600
        await shadow.wait_idle()
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=successor,
        ) == "completed"
        assert detector.commit_provider_exact_speaker_interval(second) is None
        assert detector._provider_speaker_evidence_state_for(successor) is not None
    finally:
        await detector.close()
