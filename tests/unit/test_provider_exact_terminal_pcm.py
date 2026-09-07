"""A scored terminal is not authority to erase an unassigned exact suffix."""

import asyncio
import time
from dataclasses import replace
from functools import partial

import pytest

from tests.unit import test_asr_detector_runtime as support
from tests.unit.test_provider_exact_generation import _ScoreBackend
from main_logic.asr_client.speaker_shadow import runtime as shadow_module


@pytest.mark.parametrize("duration", [-1, 2.01, float("nan"), float("inf"), True, "2"])
def test_terminal_pcm_retention_has_a_fixed_bounded_configuration(duration):
    with pytest.raises(ValueError):
        replace(support._provider_speaker_config(), exact_boundary_pcm_retention_seconds=duration)


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [.2, .95])
@pytest.mark.parametrize("continuation_before_prepare", [False, True])
@pytest.mark.parametrize("abort_first", [False, True])
async def test_late_exact_inside_terminal_window_preserves_real_suffix(
    score, continuation_before_prepare, abort_first, monkeypatch,
):
    events, scored_pcm = [], []
    shadow = support.SpeakerShadowRuntime(
        backend_factory=partial(_ScoreBackend, score),
        config=support._provider_speaker_config(), on_evidence=events.append,
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    real_evaluate = shadow._evaluate_candidate

    async def observe_actual_score(**kwargs):
        scored_pcm.append((kwargs["candidate"], bytes(kwargs["pcm16"])))
        return await real_evaluate(**kwargs)

    monkeypatch.setattr(shadow, "_evaluate_candidate", observe_actual_score)
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        original_pcm = support._speaker_pcm(3200)
        await detector.observe_provider_audio_ordered(
            original_pcm, sample_rate_hz=16000, identity=identity, sequence_no=1,
            split_before_audio=False, speaker_evidence_lease=lease,
        )
        await support._anchor_provider_evidence(detector, lease)
        await shadow.wait_idle()
        old_terminal = shadow._finalized[lease.candidate]
        assert old_terminal.terminal_reason == "scored"
        assert old_terminal.token.scored_sample_count == 48_000
        assert detector._provider_audio_sample_cursor_16k == 51_200
        sequence_no = 1
        if continuation_before_prepare:
            sequence_no += 1
            extra = support._speaker_pcm(100)
            original_pcm += extra
            await detector.observe_provider_audio_ordered(
                extra, sample_rate_hz=16000, identity=identity, sequence_no=sequence_no,
                split_before_audio=False, speaker_evidence_lease=lease,
            )
            await shadow.wait_idle()
        reservation = await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(0, 40_000), speaker_evidence_lease=lease,
        )
        assert reservation is not None, "late 2.5 s endpoint must split the retained 3.2 s PCM"
        assert not reservation.score_reusable
        assert reservation.target_candidate != lease.candidate
        original_deadline = shadow._buffers[lease.candidate].exact_boundary_deadline
        sequence_no += 1
        extra = support._speaker_pcm(100)
        original_pcm += extra
        await detector.observe_provider_audio_ordered(
            extra, sample_rate_hz=16000, identity=identity, sequence_no=sequence_no,
            split_before_audio=False, speaker_evidence_lease=lease,
        )
        if abort_first:
            first = reservation
            assert detector.abort_provider_exact_speaker_interval(first)
            await shadow.wait_idle()
            assert shadow._buffers[lease.candidate].exact_boundary_deadline == original_deadline
            reservation = await detector.prepare_provider_exact_speaker_interval(
                support.ProviderAudioRange(0, 40_000), speaker_evidence_lease=lease,
            )
            assert reservation is not None
            assert reservation.target_candidate.shadow_generation > first.target_candidate.shadow_generation
            assert detector.commit_provider_exact_speaker_interval(first) is None
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None and not committed.score_reusable
        await shadow.wait_idle()
        successor = committed.successor_evidence_lease
        assert successor is not None
        assert bytes(shadow._buffers[successor.candidate].pcm16) == original_pcm[80_000:]
        assert shadow._finalized[lease.candidate] is old_terminal
        assert old_terminal.token.scored_sample_count == 48_000
        target_scores = [pcm for candidate, pcm in scored_pcm if candidate == committed.target_candidate]
        assert target_scores
        assert all(pcm == original_pcm[:len(pcm)] and len(pcm) <= 80_000 for pcm in target_scores)
        if score < .4:
            assert any(len(pcm) == 80_000 for pcm in target_scores)
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=successor,
        ) == "completed"
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=successor,
        ) == "already_completed"
        assert bytes(shadow._buffers[successor.candidate].pcm16) == original_pcm[80_000:]
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_terminal_retention_respects_existing_scoring_copy_budget(monkeypatch):
    # The initial checkpoint fits alongside original PCM; the terminal copy
    # does not. Exercise the actual scorer and existing unavailable path.
    monkeypatch.setattr(shadow_module, "MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES", 160_000)
    detector, shadow, lease, _ = await _retained_terminal_stack()
    try:
        terminal = shadow._finalized[lease.candidate]
        assert terminal.terminal_reason == "dropped"
        assert terminal.token.scored_sample_count == 24_000
        assert shadow._retained_pcm_bytes() == 0
        assert shadow._terminal_pcm_expiry_handle is None
        assert await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(0, 40_000), speaker_evidence_lease=lease,
        ) is None
    finally:
        await detector.close()


async def _retained_terminal_stack(**overrides):
    config = replace(support._provider_speaker_config(), **overrides)
    shadow = support.SpeakerShadowRuntime(backend_factory=partial(_ScoreBackend, .95), config=config)
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
    lease = await detector.ensure_provider_speaker_evidence_lease()
    pcm = support._speaker_pcm(3200)
    await detector.observe_provider_audio_ordered(
        pcm, sample_rate_hz=16000, identity=identity, sequence_no=1,
        split_before_audio=False, speaker_evidence_lease=lease,
    )
    await support._anchor_provider_evidence(detector, lease)
    await shadow.wait_idle()
    return detector, shadow, lease, pcm


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["idle", "prepared", "committed"])
async def test_terminal_pcm_expiry_revokes_only_unapplied_sources(phase, monkeypatch):
    detector, shadow, lease, pcm = await _retained_terminal_stack(exact_boundary_pcm_retention_seconds=.08)
    entered, release = asyncio.Event(), asyncio.Event()
    actual_apply = shadow._process_candidate_batch_reconciliation

    async def wait_before_actual_apply(marker):
        entered.set()
        await release.wait()
        await actual_apply(marker)

    monkeypatch.setattr(shadow, "_process_candidate_batch_reconciliation", wait_before_actual_apply)
    try:
        old_buffer = shadow._buffers[lease.candidate]
        old_token = old_buffer.token
        reservation = None
        if phase != "idle":
            reservation = await detector.prepare_provider_exact_speaker_interval(
                support.ProviderAudioRange(0, 40_000), speaker_evidence_lease=lease,
            )
            assert reservation is not None
        if phase == "committed":
            assert detector.commit_provider_exact_speaker_interval(reservation) is not None
            await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(max(0, old_buffer.exact_boundary_deadline - time.monotonic()) + .03)
        assert lease.candidate not in shadow._buffers
        assert old_buffer.pcm16 == bytearray(len(pcm))
        assert old_token.terminal_reason == "scored"
        assert old_token.scored_sample_count == 48_000
        assert lease.candidate not in shadow._candidate_tokens
        assert not shadow._prepared_exact_intervals
        assert shadow._terminal_pcm_expiry_handle is None
        if reservation is not None:
            assert detector.commit_provider_exact_speaker_interval(reservation) is None
        release.set()
        await shadow.wait_idle()
        assert shadow._retained_pcm_bytes() == 0
    finally:
        release.set()
        await detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["reset", "close", "finish", "abandon"])
async def test_retained_terminal_pcm_cleanup_is_immediate(cleanup):
    detector, shadow, lease, _ = await _retained_terminal_stack()
    try:
        buffer = shadow._buffers[lease.candidate]
        if cleanup == "reset":
            await shadow.reset()
        elif cleanup == "close":
            await detector.close()
        elif cleanup == "finish":
            await detector.finish_provider_speaker_evidence_lease(lease)
            await shadow.wait_idle()
        else:
            await detector.abandon_provider_speaker_evidence_lease(lease)
            await shadow.wait_idle()
        assert lease.candidate not in shadow._buffers
        assert not any(buffer.pcm16)
        assert shadow._retained_pcm_bytes() == 0
    finally:
        await detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [4, 32])
async def test_retained_source_and_prepared_destinations_share_candidate_capacity(capacity):
    detector, shadow, lease, pcm = await _retained_terminal_stack(buffered_candidate_capacity=capacity)
    try:
        reservation = await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(0, 40_000), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        source_buffer = shadow._buffers[lease.candidate]
        for index in range(capacity - 3):
            candidate = support.SpeakerShadowCandidateKey(lease.detector_epoch, 10_000 + index, "provider_candidate")
            assert shadow.defer_coverage_candidate(candidate)
            assert shadow.submit(support._speaker_pcm(10), sample_rate_hz=16000, candidate=candidate)
            await shadow.wait_idle()
        await shadow.wait_idle()
        overflow = support.SpeakerShadowCandidateKey(lease.detector_epoch, 20_000, "provider_candidate")
        assert not shadow.defer_coverage_candidate(overflow)
        assert not shadow.submit(support._speaker_pcm(10), sample_rate_hz=16000, candidate=overflow)
        assert shadow._buffers[lease.candidate] is source_buffer
        assert bytes(source_buffer.pcm16) == pcm
        assert shadow._buffer_slots_in_use() <= capacity
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        await shadow.wait_idle()
        assert bytes(shadow._buffers[committed.successor_evidence_lease.candidate].pcm16) == pcm[80_000:]
        assert shadow._buffer_slots_in_use() <= capacity
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_finalized_capacity_does_not_evict_a_reserved_terminal_source():
    detector, shadow, lease, pcm = await _retained_terminal_stack(queue_capacity=4, finalized_candidate_capacity=4)
    try:
        reservation = await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(0, 40_000), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        source_buffer = shadow._buffers[lease.candidate]
        source_terminal = shadow._finalized[lease.candidate]
        for index in range(8):
            candidate = support.SpeakerShadowCandidateKey(lease.detector_epoch, 30_000 + index, "provider_candidate")
            assert shadow.submit(support._speaker_pcm(3000), sample_rate_hz=16000, candidate=candidate)
            await shadow.wait_idle()
            assert len(shadow._finalized) <= 4
            assert shadow._finalized[lease.candidate] is source_terminal
            assert shadow._buffers[lease.candidate] is source_buffer
            assert bytes(source_buffer.pcm16) == pcm
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        await shadow.wait_idle()
        assert bytes(shadow._buffers[committed.successor_evidence_lease.candidate].pcm16) == pcm[80_000:]
    finally:
        await detector.close()


@pytest.mark.asyncio
async def test_twenty_terminal_late_exact_intervals_keep_pcm_and_timers_bounded():
    shadow = support.SpeakerShadowRuntime(backend_factory=partial(_ScoreBackend, .95), config=support._provider_speaker_config())
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    expected_pcm = b""
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        for index in range(20):
            sample_count = 51_200 if index == 0 else 40_000
            frame = (index + 1).to_bytes(2, "little") * sample_count
            expected_pcm += frame
            await detector.observe_provider_audio_ordered(
                frame, sample_rate_hz=16000, identity=identity, sequence_no=index + 1,
                split_before_audio=False, speaker_evidence_lease=lease,
            )
            await support._anchor_provider_evidence(detector, lease, start_sample_16k=index * 40_000)
            await shadow.wait_idle()
            assert shadow._finalized[lease.candidate].terminal_reason == "scored"
            reservation = await detector.prepare_provider_exact_speaker_interval(
                support.ProviderAudioRange(index * 40_000, (index + 1) * 40_000),
                speaker_evidence_lease=lease,
            )
            assert reservation is not None and not reservation.score_reusable
            committed = detector.commit_provider_exact_speaker_interval(reservation)
            assert committed is not None
            await shadow.wait_idle()
            lease = committed.successor_evidence_lease
            expected_pcm = expected_pcm[80_000:]
            assert bytes(shadow._buffers[lease.candidate].pcm16) == expected_pcm
            assert await detector.complete_provider_speaker_boundary(
                committed.snapshot, successor_evidence_lease=lease,
            ) == "completed"
            assert detector._candidate_generation == index + 1
            assert len(detector._provider_preseal_entries) <= 8
            assert shadow._retained_pcm_bytes() == len(expected_pcm)
            assert len(shadow._buffers) == 1
            assert shadow._terminal_pcm_expiry_handle is None
            assert not shadow._prepared_exact_intervals
            assert shadow._queued_data_item_count == 0
    finally:
        await detector.close()
