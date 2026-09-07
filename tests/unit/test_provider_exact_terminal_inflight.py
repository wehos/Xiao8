"""An in-flight real terminal score cannot invalidate an exact reservation."""

import asyncio
from dataclasses import replace
from functools import partial

import pytest

from tests.unit import test_asr_detector_runtime as support
from tests.unit.test_provider_exact_generation import _ScoreBackend
from main_logic.asr_client.speaker_shadow import runtime as shadow_module
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowBatchReconcileRequest,
    SpeakerShadowCandidateKey,
    SpeakerShadowDeferredAnchorRequest,
    SpeakerShadowReconcileSource,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [.2, .95])
@pytest.mark.parametrize("exact_end", [40_000, 48_000, 50_000])
async def test_terminal_score_return_after_prepare_preserves_source_and_suffix(monkeypatch, exact_end, score):
    entered, release = asyncio.Event(), asyncio.Event()
    scored = []
    shadow = support.SpeakerShadowRuntime(
        backend_factory=partial(_ScoreBackend, score), config=support._provider_speaker_config(),
    )
    detector = support.DetectorRuntime(
        vad=support._Vad(), gate=support._Gate(),
        provider_policy=support._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    actual_score = shadow_module._BackendProcessHost.score
    paused = False

    async def pause_after_actual_terminal_score(host, pcm16, **kwargs):
        nonlocal paused
        candidate = shadow._active_evaluation[1]
        scored.append((candidate, bytes(pcm16)))
        result = await actual_score(host, pcm16, **kwargs)
        if len(pcm16) == 48_000 * 2 and not paused:
            paused = True
            entered.set()
            await release.wait()
        return result

    # Only the return edge is paused. The actual backend process receives and
    # scores the PCM; Detector/Shadow prepare, commit and retirement stay real.
    monkeypatch.setattr(shadow_module._BackendProcessHost, "score", pause_after_actual_terminal_score)
    try:
        _, identity, _ = await support._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        pcm = support._speaker_pcm(3200)
        await detector.observe_provider_audio_ordered(
            pcm, sample_rate_hz=16000, identity=identity, sequence_no=1,
            split_before_audio=False, speaker_evidence_lease=lease,
        )
        await support._anchor_provider_evidence(detector, lease)
        await asyncio.wait_for(entered.wait(), 2)
        assert shadow._active_evaluation_terminal
        assert lease.candidate not in shadow._finalized
        reservation = await detector.prepare_provider_exact_speaker_interval(
            support.ProviderAudioRange(0, exact_end), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert not reservation.score_reusable
        assert reservation.target_candidate != lease.candidate
        release.set()
        await shadow.wait_idle()
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None, "terminal score return must not erase prepared source PCM"
        await shadow.wait_idle()
        successor = committed.successor_evidence_lease
        assert successor is not None
        assert bytes(shadow._buffers[successor.candidate].pcm16) == pcm[exact_end * 2:]
        if not reservation.score_reusable:
            target_scores = [part for candidate, part in scored if candidate == committed.target_candidate]
            assert target_scores
            assert all(part == pcm[:len(part)] and len(part) <= exact_end * 2 for part in target_scores)
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=successor,
        ) == "completed"
        assert bytes(shadow._buffers[successor.candidate].pcm16) == pcm[exact_end * 2:]
    finally:
        release.set()
        await detector.close()


@pytest.mark.asyncio
async def test_prepare_reserves_finalized_capacity_for_inflight_terminal_source(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    config = replace(
        support._provider_speaker_config(), minimum_audio_ms=20,
        observation_checkpoints_ms=(20, 40), queue_capacity=4,
        finalized_candidate_capacity=4, buffered_candidate_capacity=32,
    )
    shadow = support.SpeakerShadowRuntime(
        backend_factory=partial(_ScoreBackend, .95), config=config,
    )
    candidates = tuple(SpeakerShadowCandidateKey(1, index, "provider_candidate") for index in range(1, 6))
    actual_score = shadow_module._BackendProcessHost.score

    async def pause_fifth_terminal(host, pcm16, **kwargs):
        candidate = shadow._active_evaluation[1]
        result = await actual_score(host, pcm16, **kwargs)
        if candidate == candidates[-1] and len(pcm16) == 640 * 2:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(shadow_module._BackendProcessHost, "score", pause_fifth_terminal)
    try:
        for index, candidate in enumerate(candidates):
            assert shadow.defer_coverage_candidate(candidate)
            assert shadow.submit(support._speaker_pcm(40), sample_rate_hz=16000, candidate=candidate)
            await shadow.wait_idle()
            assert shadow.anchor_deferred_candidate(SpeakerShadowDeferredAnchorRequest(
                candidate, expected_observed_sample_count=640,
                discard_prefix_sample_count=0, anchor_revision=1,
            )) is not None
            if index < 4:
                await shadow.wait_idle()
                assert shadow._queued_data_item_count == 0, "processed anchors must release data queue capacity"
        await asyncio.wait_for(entered.wait(), 2)
        assert len(shadow._finalized) == 4
        original = {candidate: shadow._buffers[candidate] for candidate in candidates}
        original_candidates = set(shadow._candidate_tokens)
        original_bytes = shadow._retained_pcm_bytes()
        original_batch_id = shadow._next_reconciliation_batch_id
        original_queue_count = shadow._queued_data_item_count
        request = SpeakerShadowBatchReconcileRequest(
            sources=tuple(SpeakerShadowReconcileSource(candidate, 640, 0, 640) for candidate in candidates),
            target=SpeakerShadowCandidateKey(1, 6, "provider_candidate"),
        )
        # Five tiny sources fit the PCM, candidate and queue budgets. The fifth
        # terminal result must still have a bounded finalized slot available.
        assert shadow.prepare_exact_interval(request) is None
        assert not shadow._prepared_exact_intervals
        assert set(shadow._candidate_tokens) == original_candidates
        assert shadow._retained_pcm_bytes() == original_bytes
        assert shadow._next_reconciliation_batch_id == original_batch_id
        assert shadow._queued_data_item_count == original_queue_count
        assert all(shadow._buffers[candidate] is buffer for candidate, buffer in original.items())
        assert all(not buffer.token.pcm_frozen for buffer in original.values())
        release.set()
        await shadow.wait_idle()
        assert len(shadow._finalized) <= 4
    finally:
        release.set()
        await shadow.close()
