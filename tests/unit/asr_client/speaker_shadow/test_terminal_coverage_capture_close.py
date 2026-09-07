"""Terminal coverage must publish one ordered close, preserving scored facts."""

import pytest

from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCompletion, SpeakerShadowObservation, SpeakerShadowTerminalCoverageRequest,
)
from main_logic.asr_client.speaker_shadow.runtime import SpeakerShadowRuntime
from tests.unit.asr_client.test_provider_speaker_continuity import _GatedScoreHost
from .test_runtime import (
    _BackendFactory, _candidate, _pcm, _provider_gate_config,
    _reconcile_source, _finalize_provider_candidate_score,
)


@pytest.mark.parametrize("score", [.2, .95])
@pytest.mark.parametrize("operation", ["commit", "abort", "revoke", "reset"])
async def test_terminal_coverage_close_respects_commit_and_revoke(score, operation):
    evidence = []
    runtime = SpeakerShadowRuntime(backend_factory=_BackendFactory(score_value=score),
        config=_provider_gate_config(), on_evidence=evidence.append)
    host = _GatedScoreHost(score, False)
    host.ready.set()
    runtime._backend_host = host
    target, successor = _candidate(110001), _candidate(110002)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        original = runtime._finalized[target].token
        assert len(evidence) == 2
        receipt = runtime.prepare_finalized_candidate_coverage(SpeakerShadowTerminalCoverageRequest(
            sources=(_reconcile_source(target, 12_000, keep_end_ms=10_000),),
            target=target, suffix=successor, provider_exact_start_sample=0,
            provider_exact_end_sample=160_000, scored_window_start_sample=0,
            scored_window_end_sample=48_000))
        assert receipt is not None
        assert len(evidence) == 2
        if operation == "abort":
            assert runtime.abort_finalized_candidate_coverage(receipt)
        else:
            assert runtime.commit_finalized_candidate_coverage(receipt)
            if operation == "revoke":
                runtime.revoke_terminal_coverage(receipt)
            elif operation == "reset":
                await runtime.reset()
        await runtime.wait_idle()
        closes = [e for e in evidence if isinstance(e, SpeakerShadowCompletion)
            and e.candidate == target and e.terminal_reason == "scored" and e.evidence_complete]
        if operation != "commit":
            assert closes == []
            return
        assert len(closes) == 1
        assert [type(e) for e in evidence] == [SpeakerShadowObservation, SpeakerShadowObservation, SpeakerShadowCompletion]
        assert closes[0].through_sequence_no == 2
        assert [e.similarity for e in evidence[:2]] == [score, score]
        assert runtime._finalized[target].token is original
        assert host.calls == 2
        assert runtime.finish_candidate(target)
        await runtime.wait_idle()
        assert len(evidence) == 3
        assert runtime.complete_reconciliation(receipt, successor=successor) == "completed"
        runtime.revoke_terminal_coverage(receipt)
        assert runtime.submit(_pcm(100), sample_rate_hz=16_000, candidate=successor)
        await runtime.wait_idle()
        assert runtime._buffers[successor].sample_count == 1600
        assert host.calls == 2
    finally:
        await runtime.close()


@pytest.mark.parametrize("capacity", [2, 16])
@pytest.mark.parametrize("score", [.2, .95])
@pytest.mark.parametrize("operation", ["commit", "abort", "revoke", "reset", "abandon"])
async def test_terminal_coverage_close_survives_full_tombstone_table(
    capacity, score, operation,
):
    evidence = []
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=score),
        config=_provider_gate_config(queue_capacity=2, finalized_candidate_capacity=capacity),
        on_evidence=evidence.append,
    )
    host = _GatedScoreHost(score, False)
    host.ready.set()
    runtime._backend_host = host
    target = _candidate(120000)
    resumed, successor = _candidate(120000+capacity), _candidate(120001+capacity)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        original = runtime._finalized[target].token
        for generation in range(120001, 120000+capacity):
            filler = _candidate(generation)
            assert runtime.submit(_pcm(100), sample_rate_hz=16000, candidate=filler)
            assert runtime.finish_candidate(filler)
            await runtime.wait_idle()
        assert next(iter(runtime._finalized)) == target
        assert len(runtime._finalized) == capacity
        assert runtime.submit(_pcm(500), sample_rate_hz=16000, candidate=resumed)
        await runtime.wait_idle()
        receipt = runtime.prepare_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(_reconcile_source(target, 3000),
                         _reconcile_source(resumed, 500, keep_end_ms=400)),
                target=target, suffix=successor,
                provider_exact_start_sample=0, provider_exact_end_sample=54400,
                scored_window_start_sample=0, scored_window_end_sample=48000,
            )
        )
        assert receipt is not None
        if operation == "abort":
            assert runtime.abort_finalized_candidate_coverage(receipt)
        else:
            assert runtime.commit_finalized_candidate_coverage(receipt)
            if operation == "revoke":
                runtime.revoke_terminal_coverage(receipt)
            elif operation == "reset":
                await runtime.reset()
            elif operation == "abandon":
                assert runtime.abandon_candidate(target)
        await runtime.wait_idle()
        target_evidence = [item for item in evidence if item.candidate == target]
        closes = [item for item in target_evidence if isinstance(item, SpeakerShadowCompletion)]
        assert len(runtime._finalized) <= capacity
        if operation != "commit":
            assert closes == []
            return
        assert runtime.terminal_coverage_status(receipt) == "applied"
        assert target not in runtime._finalized  # Receipt ownership survives eviction.
        assert len(closes) == 1
        assert closes[0].terminal_reason == "scored"
        assert closes[0].evidence_complete
        assert closes[0].through_sequence_no == 2
        assert original.terminal_reason == "scored"
        assert [item.similarity for item in target_evidence[:2]] == [score, score]
        assert host.calls == 2
        assert runtime._buffers[successor].sample_count == 1600
        assert runtime.finish_candidate(target)
        assert runtime.complete_reconciliation(receipt, successor=successor) == "completed"
        runtime.revoke_terminal_coverage(receipt)
        assert runtime.submit(_pcm(100), sample_rate_hz=16000, candidate=successor)
        await runtime.wait_idle()
        assert runtime._buffers[successor].sample_count == 3200
        assert len([item for item in evidence if isinstance(item, SpeakerShadowCompletion)
                    and item.candidate == target]) == 1
        assert len(runtime._finalized) <= capacity
        assert not runtime._prepared_terminal_coverages
        assert runtime._queued_terminal_count == 0
        assert host.calls == 2
    finally:
        await runtime.close()
