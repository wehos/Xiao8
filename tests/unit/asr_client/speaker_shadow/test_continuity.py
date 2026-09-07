from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowBatchReconcileRequest,
    SpeakerShadowDeferredAnchorRequest,
    SpeakerShadowTerminalCoverageRequest,
)
from main_logic.asr_client.speaker_shadow.runtime import SpeakerShadowRuntime
from .test_runtime import (
    _BackendFactory, _candidate, _pcm, _provider_gate_config,
    _reconcile_source, _spawn_event, _wait_until,
    _finalize_provider_candidate_score,
)


@pytest.mark.parametrize("score", [0.20, 0.95])
async def test_normal_completion_retires_old_revoke_without_losing_successor(score):
    observations = []

    async def observe(event):
        observations.append(event)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=score),
        config=_provider_gate_config(), on_observation=observe,
    )
    source, successor = _candidate(100_001), _candidate(100_002)
    try:
        assert runtime.defer_candidate(source)
        assert runtime.submit(_pcm(2_000), sample_rate_hz=16_000, candidate=source)
        receipt = runtime.reconcile_candidate_batch(SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(source, 2_000, keep_end_ms=1_600),),
            target=source, suffix=successor,
        ))
        assert receipt is not None
        assert runtime.complete_reconciliation(receipt, successor=source) == "invalid"
        assert runtime.complete_reconciliation(receipt, successor=successor) == "pending"
        await runtime.wait_idle()
        assert runtime.reconciliation_status(receipt) == "applied"
        assert observations and observations[0].similarity == score
        complete = getattr(runtime, "complete_reconciliation", None)
        if complete is not None:
            assert complete(receipt, successor=successor) == "completed"
            assert complete(receipt, successor=successor) == "already_completed"
        # The legacy normal-cleanup path only had this whole-batch revocation.
        runtime.revoke_reconciliation(receipt)
        assert runtime.submit(_pcm(100), sample_rate_hz=16_000, candidate=successor)
        await runtime.wait_idle()
        assert runtime._buffers[successor].sample_count == 500 * 16
        assert runtime.complete_reconciliation(replace(receipt), successor=successor) == "stale"
        await runtime.reset()
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
        assert runtime.complete_reconciliation(receipt, successor=successor) == "stale"
    finally:
        await runtime.close()


@pytest.mark.parametrize("boundary", ["reset", "close"])
async def test_cold_load_lifecycle_retires_pending_work_and_host(boundary):
    started, release = _spawn_event(), _spawn_event()
    observations = []

    async def observe(event):
        observations.append(event)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(block_stage="load", stage_started=started, stage_release=release),
        config=_provider_gate_config(prewarm=True, buffered_candidate_capacity=2),
        on_observation=observe,
    )
    retired = False
    try:
        for index in range(2):
            candidate = _candidate(100_020 + index)
            assert runtime.submit(_pcm(1_600), sample_rate_hz=16_000, candidate=candidate)
            assert runtime.finish_candidate(candidate)
        await _wait_until(started.is_set)
        await asyncio.wait_for(runtime._queue.join(), 0.5)
        assert len(runtime._pending_backend_candidates) == 2
        await asyncio.wait_for(getattr(runtime, boundary)(), 2.0)
        retired = True
        await runtime.wait_idle()
        assert not runtime._pending_backend_candidates
        assert runtime._backend_load_task is None
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
        assert runtime.snapshot()["backend_process_count"] == 0
        assert observations == []
    finally:
        if not retired:
            release.set()
        await runtime.close()


async def test_completed_receipt_cannot_revoke_successor_consumed_by_later_batch():
    runtime = SpeakerShadowRuntime(backend_factory=_BackendFactory(), config=_provider_gate_config())
    first, second, third = (_candidate(100_030 + index) for index in range(3))
    try:
        assert runtime.defer_candidate(first)
        assert runtime.submit(_pcm(1_000), sample_rate_hz=16_000, candidate=first)
        old = runtime.reconcile_candidate_batch(SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(first, 1_000, keep_end_ms=600),), target=first, suffix=second,
        ))
        assert old is not None
        await runtime.wait_idle()
        new = runtime.reconcile_candidate_batch(SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(second, 400, keep_end_ms=300),), target=second, suffix=third,
        ))
        assert new is not None
        await runtime.wait_idle()
        assert runtime.complete_reconciliation(old, successor=second) == "completed"
        runtime.revoke_reconciliation(old)
        assert runtime.reconciliation_status(new) == "applied"
        assert runtime.submit(_pcm(100), sample_rate_hz=16_000, candidate=third)
        await runtime.wait_idle()
        assert runtime._buffers[third].sample_count == 200 * 16
        # A real, non-completed receipt still revokes its own current successor.
        runtime.revoke_reconciliation(new)
        assert not runtime.submit(_pcm(100), sample_rate_hz=16_000, candidate=third)
    finally:
        await runtime.close()


@pytest.mark.parametrize("score", [0.20, 0.95])
async def test_terminal_coverage_completion_preserves_scored_verdict_and_successor(score):
    runtime = SpeakerShadowRuntime(backend_factory=_BackendFactory(score_value=score), config=_provider_gate_config())
    target, successor = _candidate(100_040), _candidate(100_041)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        original = runtime._finalized[target]
        receipt = runtime.reconcile_finalized_candidate_coverage(SpeakerShadowTerminalCoverageRequest(
            sources=(_reconcile_source(target, 12_000, keep_end_ms=10_000),),
            target=target, provider_exact_start_sample=0,
            provider_exact_end_sample=160_000, scored_window_start_sample=0,
            scored_window_end_sample=48_000, suffix=successor,
        ))
        assert receipt is not None
        assert runtime.complete_reconciliation(receipt, successor=successor) == "pending"
        await runtime.wait_idle()
        assert runtime.complete_reconciliation(receipt, successor=successor) == "completed"
        completed = runtime._finalized[target]
        assert completed.token is original.token
        assert completed.terminal_reason == original.terminal_reason == "scored"
        assert completed.finish_seen
        runtime.revoke_terminal_coverage(receipt)
        assert runtime._finalized[target] is completed
        assert runtime.submit(_pcm(100), sample_rate_hz=16_000, candidate=successor)
        await runtime.wait_idle()
        assert runtime._buffers[successor].sample_count == 1_600
        assert runtime.complete_reconciliation(receipt, successor=successor) == "already_completed"
    finally:
        await runtime.close()


async def test_cancel_ready_worker_retires_all_waiting_candidates_and_finishes():
    started = _spawn_event()
    completions = []

    async def complete(event):
        completions.append(event)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(block_stage="score", stage_started=started),
        config=_provider_gate_config(), on_completion=complete,
    )
    candidates = [_candidate(100_050 + index) for index in range(2)]
    try:
        for candidate in candidates:
            assert runtime.submit(_pcm(1_600), sample_rate_hz=16_000, candidate=candidate)
            assert runtime.finish_candidate(candidate)
        await _wait_until(started.is_set)
        worker = runtime._worker_task
        assert worker is not None
        worker.cancel()
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 2.0)
        await runtime.wait_idle()
        assert not runtime._pending_backend_candidates
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
        assert set(item.candidate for item in completions) == set(candidates)
        assert all(item.terminal_reason == "failed" for item in completions)
    finally:
        await runtime.close()


async def test_cold_load_does_not_hold_pcm_anchor_or_exact_marker():
    started, release = _spawn_event(), _spawn_event()
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(block_stage="load", stage_started=started, stage_release=release),
        config=_provider_gate_config(prewarm=True),
    )
    source, successor = _candidate(100_011), _candidate(100_012)
    try:
        assert runtime.defer_candidate(source)
        assert runtime.submit(_pcm(100), sample_rate_hz=16_000, candidate=source)
        await _wait_until(started.is_set)
        anchor = runtime.anchor_deferred_candidate(SpeakerShadowDeferredAnchorRequest(
            candidate=source, expected_observed_sample_count=1_600,
            discard_prefix_sample_count=0, anchor_revision=1,
        ))
        assert anchor is not None
        assert await runtime.wait_deferred_anchor_settled(anchor, deadline=time.monotonic() + 0.2) == "applied"
        assert not release.is_set()
        assert runtime.submit(_pcm(1_900), sample_rate_hz=16_000, candidate=source)
        receipt = runtime.reconcile_candidate_batch(SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(source, 2_000, keep_end_ms=1_600),),
            target=source, suffix=successor,
        ))
        assert receipt is not None
        assert await runtime.wait_reconciliation_settled(receipt, deadline=time.monotonic() + 0.2) == "applied"
        assert runtime.submit(_pcm(100), sample_rate_hz=16_000, candidate=successor)
        await asyncio.wait_for(runtime._queue.join(), 0.5)
        assert runtime._buffers[successor].sample_count == 500 * 16
        release.set()
        await runtime.wait_idle()
        assert runtime.snapshot()["load_count"] == 1
    finally:
        release.set()
        await runtime.close()
