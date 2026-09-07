"""Exact PCM ownership remains valid when an old score exceeds the prefix."""

from functools import partial

import pytest

from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowBatchReconcileRequest,
    SpeakerShadowCandidateKey,
    SpeakerShadowReconcileSource,
)
from tests.unit import test_asr_detector_runtime as support
from tests.unit.test_provider_exact_generation import _ScoreBackend


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0.2, 0.95])
@pytest.mark.parametrize("end", [23760, 20816])
@pytest.mark.parametrize("abort_first", [False, True])
async def test_short_exact_owns_pcm_without_reusing_out_of_range_score(
    score, end, abort_first,
):
    events = []
    shadow = support.SpeakerShadowRuntime(
        backend_factory=partial(_ScoreBackend, score),
        config=support._provider_speaker_config(), on_evidence=events.append,
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        pcm = support._speaker_pcm(1600)
        await detector.observe_provider_audio_ordered(
            pcm, sample_rate_hz=16000, identity=identity, sequence_no=1,
            split_before_audio=False, speaker_evidence_lease=lease,
        )
        await support._anchor_provider_evidence(detector, lease)
        await shadow.wait_idle()
        assert shadow._candidate_tokens[lease.candidate].scored_sample_count == 24000
        boundary = support.ProviderAudioRange(0, end)
        first = await detector.prepare_provider_exact_speaker_interval(
            boundary, speaker_evidence_lease=lease,
        )
        assert first is not None
        assert first.score_reusable is False
        assert first.source_candidate == lease.candidate
        assert first.target_candidate != lease.candidate
        assert detector._candidate_generation == 0
        extra = support._speaker_pcm(100)
        await detector.observe_provider_audio_ordered(
            extra, sample_rate_hz=16000, identity=identity, sequence_no=2,
            split_before_audio=True, speaker_evidence_lease=lease,
        )
        if abort_first:
            assert detector.abort_provider_exact_speaker_interval(first)
            await shadow.wait_idle()
            assert bytes(shadow._buffers[lease.candidate].pcm16) == pcm + extra
            reservation = await detector.prepare_provider_exact_speaker_interval(
                boundary, speaker_evidence_lease=lease,
            )
            assert reservation is not None
            assert reservation.target_candidate.shadow_generation > first.target_candidate.shadow_generation
            assert reservation.suffix_candidate.shadow_generation > first.suffix_candidate.shadow_generation
            assert detector.commit_provider_exact_speaker_interval(first) is None
        else:
            reservation = first
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        assert committed.score_reusable is False
        assert first.provider_pcm_through_sequence_no == 1
        assert committed.provider_pcm_through_sequence_no == 2
        assert committed.observed_through_sample_16k == 27200
        assert detector._candidate_generation == 1
        assert detector.commit_provider_exact_speaker_interval(reservation) is None
        successor = committed.successor_evidence_lease
        await shadow.wait_idle()
        assert bytes(shadow._buffers[successor.candidate].pcm16) == (pcm + extra)[end * 2:]
        assert shadow._finalized[committed.target_candidate].terminal_reason == "insufficient"
        assert not any(
            isinstance(event, support.SpeakerShadowObservation)
            and event.candidate == committed.target_candidate for event in events
        )
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=successor,
        ) == "completed"
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=successor,
        ) == "already_completed"
        await support._anchor_provider_evidence(detector, successor, start_sample_16k=end)
        assert detector._provider_speaker_evidence_state_for(successor).anchor_start_sample_16k == end
        assert bytes(shadow._buffers[successor.candidate].pcm16) == (pcm + extra)[end * 2:]
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_fresh_target_requires_real_live_pcm_and_an_out_of_range_score():
    shadow = support.SpeakerShadowRuntime(
        backend_factory=partial(_ScoreBackend, 0.95),
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
            support._speaker_pcm(1600), sample_rate_hz=16000, identity=identity,
            sequence_no=1, split_before_audio=False, speaker_evidence_lease=lease,
        )
        await support._anchor_provider_evidence(detector, lease)
        await shadow.wait_idle()
        def source(end, expected=25600):
            return SpeakerShadowReconcileSource(
                candidate=lease.candidate, expected_sample_count=expected,
                keep_start_sample=0, keep_end_sample=end,
            )
        assert shadow.exact_interval_requires_fresh_target(source(23760))
        assert not shadow.exact_interval_requires_fresh_target(source(24000))
        assert not shadow.exact_interval_requires_fresh_target(source(23760, 27000))
        assert shadow.prepare_exact_interval(SpeakerShadowBatchReconcileRequest(
            sources=(source(23760),), target=lease.candidate,
            suffix=SpeakerShadowCandidateKey(
                lease.candidate.detector_epoch, 100000, "provider_candidate",
            ), finish_target=True,
        )) is None
        reservation = await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(0, 23760), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert not shadow.exact_interval_requires_fresh_target(source(23760))
        assert detector.abort_provider_exact_speaker_interval(reservation)
        await detector.abandon_provider_speaker_evidence_lease(lease)
        assert not shadow.exact_interval_requires_fresh_target(source(23760))
        await detector.close()
        assert not shadow.exact_interval_requires_fresh_target(source(23760))
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_twenty_short_exact_intervals_preserve_bounded_successors():
    shadow = support.SpeakerShadowRuntime(
        backend_factory=partial(_ScoreBackend, 0.95),
        config=support._provider_speaker_config(),
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        first = None
        for index in range(20):
            await detector.observe_provider_audio_ordered(
                support._speaker_pcm(1600 if index == 0 else 1485),
                sample_rate_hz=16000, identity=identity, sequence_no=index + 1,
                split_before_audio=False, speaker_evidence_lease=lease,
            )
            await support._anchor_provider_evidence(
                detector, lease, start_sample_16k=index * 23760,
            )
            await shadow.wait_idle()
            reservation = await detector.prepare_provider_exact_speaker_interval(
                support.ProviderAudioRange(index * 23760, (index + 1) * 23760),
                speaker_evidence_lease=lease,
            )
            assert reservation is not None
            assert reservation.score_reusable is False
            committed = detector.commit_provider_exact_speaker_interval(reservation)
            assert committed is not None
            assert detector._candidate_generation == index + 1
            lease = committed.successor_evidence_lease
            await shadow.wait_idle()
            assert shadow._buffers[lease.candidate].sample_count == 1840
            assert await detector.complete_provider_speaker_boundary(
                committed.snapshot, successor_evidence_lease=lease,
            ) == "completed"
            first = first or (committed.snapshot, lease)
            assert len(detector._provider_preseal_entries) <= 8
            assert len(shadow._candidate_tokens) == 1
            assert len(shadow._buffers) == 1
            assert shadow._retained_pcm_bytes() == 3680
            assert not detector._provider_exact_interval_records
            assert not detector._provider_boundary_completion_entries
        assert await detector.complete_provider_speaker_boundary(
            first[0], successor_evidence_lease=first[1],
        ) == "stale"
        assert detector._provider_speaker_evidence_state_for(lease) is not None
    finally:
        await detector.close()
