from __future__ import annotations

import asyncio
import inspect
import multiprocessing
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest

from main_logic.asr_client.speaker_shadow.contracts import (
    MAX_SPEAKER_SHADOW_FRAME_PCM_BYTES,
    SPEAKER_SHADOW_SAMPLE_RATE_HZ,
    SpeakerShadowBatchReconcileReceipt,
    SpeakerShadowBatchReconcileRequest,
    SpeakerShadowCandidateKey,
    SpeakerShadowCaptureDecisionState,
    SpeakerShadowCaptureDisposition,
    SpeakerShadowCompletion,
    SpeakerShadowConfig,
    SpeakerShadowDeferredAnchorRequest,
    SpeakerShadowObservation,
    SpeakerShadowReconcileSource,
    SpeakerShadowTerminalCoverageRequest,
)
from main_logic.asr_client.speaker_shadow.runtime import (
    SpeakerShadowRuntime,
    _AudioFrame,
    _BackendProcessHost,
    _CandidateActivated,
    _CandidateBatchReconciliation,
    _CandidateBuffer,
    _CandidateDeferred,
    _CandidateFinished,
    _CandidateToken,
    _backend_host_main,
)


@dataclass(slots=True)
class _BackendFactory:
    score_value: float = 0.9
    load_ok: bool = True
    load_error: bool = False
    score_error: bool = False
    score_error_after: int | None = None
    close_error: bool = False
    expected_pcm: bytes | None = None
    block_stage: str | None = None
    stage_started: Any = None
    stage_release: Any = None
    parent_close_calls: int = 0
    parent_profile: bytearray | None = None

    def __call__(self) -> _Backend:
        return _Backend(self)

    def close(self) -> None:
        self.parent_close_calls += 1
        if self.parent_profile is not None:
            self.parent_profile[:] = b"\x00" * len(self.parent_profile)


class _Backend:
    def __init__(self, settings: _BackendFactory) -> None:
        self._settings = settings
        self._score_calls = 0

    def _maybe_block(self, stage: str) -> None:
        settings = self._settings
        if settings.block_stage != stage:
            return
        if settings.stage_started is not None:
            settings.stage_started.set()
        if settings.stage_release is None:
            while True:
                time.sleep(60.0)
        settings.stage_release.wait()

    def load(self) -> bool:
        self._maybe_block("load")
        if self._settings.load_error:
            raise RuntimeError("load failed")
        return self._settings.load_ok

    def score(self, pcm16: bytes, sample_rate_hz: int) -> float:
        self._maybe_block("score")
        should_fail = self._settings.score_error or (
            self._settings.score_error_after is not None
            and self._score_calls >= self._settings.score_error_after
        )
        self._score_calls += 1
        if should_fail:
            raise RuntimeError("score failed")
        if self._settings.expected_pcm is not None:
            assert pcm16 == self._settings.expected_pcm
        assert sample_rate_hz == SPEAKER_SHADOW_SAMPLE_RATE_HZ
        return self._settings.score_value

    def close(self) -> None:
        self._maybe_block("close")
        if self._settings.close_error:
            raise RuntimeError("close failed")


def _pcm(duration_ms: int) -> bytes:
    return b"\x01\x00" * (SPEAKER_SHADOW_SAMPLE_RATE_HZ * duration_ms // 1_000)


def _tagged_pcm(duration_ms: int, tag: int) -> bytes:
    return bytes((tag, 0)) * (SPEAKER_SHADOW_SAMPLE_RATE_HZ * duration_ms // 1_000)


def _candidate(
    generation: int,
    scope: str = "provider_candidate",
) -> SpeakerShadowCandidateKey:
    return SpeakerShadowCandidateKey(1, generation, scope)  # type: ignore[arg-type]


def _reconcile_source(
    candidate: SpeakerShadowCandidateKey,
    duration_ms: int,
    *,
    keep_start_ms: int = 0,
    keep_end_ms: int | None = None,
) -> SpeakerShadowReconcileSource:
    samples_per_ms = SPEAKER_SHADOW_SAMPLE_RATE_HZ // 1_000
    return SpeakerShadowReconcileSource(
        candidate=candidate,
        expected_sample_count=duration_ms * samples_per_ms,
        keep_start_sample=keep_start_ms * samples_per_ms,
        keep_end_sample=(duration_ms if keep_end_ms is None else keep_end_ms)
        * samples_per_ms,
    )


def _config(**overrides: object) -> SpeakerShadowConfig:
    values: dict[str, object] = {
        "enabled": True,
        "minimum_audio_ms": 20,
        "maximum_audio_ms": 100,
        "idle_unload_seconds": 60.0,
        "queue_capacity": 8,
        "buffered_candidate_capacity": 4,
        "finalized_candidate_capacity": 16,
        "shutdown_grace_seconds": 0.05,
        "callback_timeout_seconds": 0.05,
        "backend_load_timeout_seconds": 3.0,
        "backend_score_timeout_seconds": 1.0,
        "backend_close_timeout_seconds": 1.0,
        "process_terminate_timeout_seconds": 0.5,
    }
    values.update(overrides)
    return SpeakerShadowConfig(**values)


def _provider_gate_config(
    *,
    prewarm: bool = False,
    **overrides: object,
) -> SpeakerShadowConfig:
    values: dict[str, object] = {
        "minimum_audio_ms": 1_500,
        "maximum_audio_ms": 4_000,
        "observation_checkpoints_ms": (1_500, 3_000),
        "completion_confirmation_scopes": ("provider_candidate",),
        "pending_observation_gate_scopes": ("provider_candidate",),
        "backend_prewarm_scopes": (("provider_candidate",) if prewarm else ()),
    }
    values.update(overrides)
    return _config(**values)


async def _finalize_provider_candidate_score(
    runtime: SpeakerShadowRuntime,
    candidate: SpeakerShadowCandidateKey,
) -> None:
    first = runtime.submit_capture(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert first.disposition is SpeakerShadowCaptureDisposition.ACCEPTED
    await runtime.wait_idle()
    terminal = runtime.submit_capture(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert terminal.disposition is SpeakerShadowCaptureDisposition.COMPLETE
    assert terminal.decision_state is SpeakerShadowCaptureDecisionState.PENDING
    await runtime.wait_idle()


def _spawn_event() -> Any:
    return multiprocessing.get_context("spawn").Event()


def _speaker_host_pids() -> set[int]:
    return {
        process.pid
        for process in multiprocessing.active_children()
        if process.pid is not None
        and process.name == "speaker-shadow-backend"
        and process.is_alive()
    }


async def test_deferred_candidate_buffers_then_scores_in_order_after_activation() -> (
    None
):
    observations: list[SpeakerShadowObservation] = []
    completions: list[SpeakerShadowCompletion] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(9_001)

    assert runtime.defer_candidate(candidate)
    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()

    assert observations == []
    assert runtime.requires_provisional_decision(candidate) is False
    assert runtime.snapshot()["retained_pcm_bytes"] == len(_pcm(2_999))

    assert runtime.activate_candidate(candidate)
    assert runtime.requires_provisional_decision(candidate) is True
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert [item.observation_kind for item in observations] == [
        "checkpoint",
        "completion_confirmation",
    ]
    assert [item.audio_ms for item in observations] == [1_500, 2_999]
    assert completions == [SpeakerShadowCompletion(candidate, "scored", 1_500)]
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_anchor_pending_caps_primary_at_terminal_scoring_window() -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_observation=observe,
    )
    candidate = _candidate(9_002)
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker  # type: ignore[assignment]
    try:
        assert runtime.defer_candidate(candidate)
        first = runtime.submit_capture(
            _pcm(1_000),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        assert first.accepted_sample_count == 16_000
        receipt = runtime.anchor_deferred_candidate(
            SpeakerShadowDeferredAnchorRequest(
                candidate=candidate,
                expected_observed_sample_count=16_000,
                discard_prefix_sample_count=0,
                anchor_revision=1,
            )
        )
        assert receipt is not None

        crossing = runtime.submit_capture(
            _pcm(3_000),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        assert crossing.accepted_sample_count == 32_000
        assert crossing.cumulative_sample_count == 48_000
        assert crossing.disposition is SpeakerShadowCaptureDisposition.COMPLETE
        assert crossing.decision_state is SpeakerShadowCaptureDecisionState.PENDING
        assert observations == []

        hold_worker.set()
        await fake_worker
        runtime._worker_task = None
        assert runtime._ensure_worker()
        assert (
            await runtime.wait_deferred_anchor_settled(
                receipt,
                deadline=time.monotonic() + 2.0,
            )
            == "applied"
        )
        await runtime.wait_idle()
        assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize(
    ("prefix_tag", "suffix_tag", "score", "would_block"),
    (
        pytest.param(0x11, 0x70, 0.70, False, id="low-prefix-owner-suffix"),
        pytest.param(0x70, 0x11, 0.20, True, id="high-prefix-nonowner-suffix"),
    ),
)
async def test_nonzero_anchor_scores_only_canonical_suffix(
    prefix_tag: int,
    suffix_tag: int,
    score: float,
    would_block: bool,
) -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    prefix_pcm = _tagged_pcm(500, prefix_tag)
    suffix_pcm = _tagged_pcm(1_500, suffix_tag)
    config = _provider_gate_config(similarity_thresholds=(0.40,))
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            score_value=score,
            expected_pcm=suffix_pcm,
        ),
        config=config,
        on_observation=observe,
    )
    candidate = _candidate(9_003 + prefix_tag)
    try:
        assert config.similarity_thresholds == (0.40,)
        assert runtime.defer_candidate(candidate)
        assert runtime.submit(
            prefix_pcm,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        assert runtime.submit(
            suffix_pcm,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        await runtime.wait_idle()
        assert observations == []
        assert runtime.snapshot()["evaluated_candidate_count"] == 0
        assert runtime.snapshot()["backend_loaded_count"] == 0

        receipt = runtime.anchor_deferred_candidate(
            SpeakerShadowDeferredAnchorRequest(
                candidate=candidate,
                expected_observed_sample_count=32_000,
                discard_prefix_sample_count=8_000,
                anchor_revision=1,
            )
        )
        assert receipt is not None
        assert receipt.discarded_sample_count == 8_000
        assert receipt.retained_sample_count == 24_000
        assert (
            await runtime.wait_deferred_anchor_settled(
                receipt,
                deadline=time.monotonic() + 2.0,
            )
            == "applied"
        )
        await runtime.wait_idle()

        assert len(observations) == 1
        observation = observations[0]
        assert observation.similarity == pytest.approx(score)
        assert observation.checkpoint_ms == 1_500
        assert observation.audio_ms == 1_500
        assert observation.would_block == ((0.40, would_block),)
        assert runtime.snapshot()["backend_loaded_count"] == 1
    finally:
        await runtime.close()


async def test_reconcile_deferred_tail_into_head_preserves_checkpoint_continuity() -> (
    None
):
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_observation=observe,
    )
    head = _candidate(9_101)
    tail = _candidate(9_102)

    assert runtime.submit(
        _pcm(800),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=head,
    )
    assert runtime.defer_candidate(tail)
    assert runtime.submit(
        _pcm(2_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=tail,
    )
    assert runtime.reconcile_candidate_prefix(
        source=tail,
        target=head,
        prefix_sample_count=SPEAKER_SHADOW_SAMPLE_RATE_HZ * 2_500 // 1_000,
    )
    assert (
        runtime.submit(
            _pcm(1),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=tail,
        )
        is False
    )
    assert runtime.finish_candidate(head)
    assert runtime.finish_candidate(tail)

    await runtime.wait_idle()

    assert [item.candidate for item in observations] == [head, head]
    assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]
    assert runtime._finalized[head].terminal_reason == "scored"
    assert runtime._finalized[tail].terminal_reason == "dropped"
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_reconcile_marker_reserves_split_and_future_pcm_before_execution() -> (
    None
):
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    head = _candidate(9_103)
    tail = _candidate(9_104)
    suffix = _candidate(9_105)
    head_pcm = _tagged_pcm(200, 0x11)
    covered_tail_pcm = _tagged_pcm(300, 0x22)
    successor_tail_pcm = _tagged_pcm(300, 0x33)
    future_head_pcm = _tagged_pcm(400, 0x44)
    future_suffix_pcm = _tagged_pcm(200, 0x55)

    assert runtime.submit(
        head_pcm,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=head,
    )
    assert runtime.defer_candidate(tail)
    assert runtime.submit(
        covered_tail_pcm,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=tail,
    )
    assert runtime.submit(
        successor_tail_pcm,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=tail,
    )
    assert runtime.reconcile_candidate_prefix(
        source=tail,
        target=head,
        prefix_sample_count=len(covered_tail_pcm) // 2,
        suffix=suffix,
    )
    # These submits race marker execution.  Their admission must already see
    # the transferred prefix/remainder reservations.
    assert runtime.submit(
        future_head_pcm,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=head,
    )
    assert runtime.submit(
        future_suffix_pcm,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=suffix,
    )
    assert (
        runtime.submit(
            _pcm(1),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=tail,
        )
        is False
    )

    await runtime.wait_idle()

    assert bytes(runtime._buffers[head].pcm16) == (
        head_pcm + covered_tail_pcm + future_head_pcm
    )
    assert bytes(runtime._buffers[suffix].pcm16) == (
        successor_tail_pcm + future_suffix_pcm
    )
    assert runtime._candidate_tokens[head].accepted_sample_count == (
        len(head_pcm + covered_tail_pcm + future_head_pcm) // 2
    )
    assert runtime._candidate_tokens[suffix].accepted_sample_count == (
        len(successor_tail_pcm + future_suffix_pcm) // 2
    )
    assert runtime._finalized[tail].terminal_reason == "dropped"

    assert runtime.finish_candidate(head)
    assert runtime.activate_candidate(suffix)
    assert runtime.finish_candidate(suffix)
    await runtime.wait_idle()
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_reconcile_in_place_splits_queued_pcm_sample_exactly() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    head = _candidate(9_106)
    suffix = _candidate(9_107)
    retained_pcm = _tagged_pcm(600, 0x61)
    successor_pcm = _tagged_pcm(400, 0x72)

    assert runtime.submit(
        retained_pcm + successor_pcm,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=head,
    )
    assert runtime.reconcile_candidate_prefix(
        source=head,
        target=head,
        prefix_sample_count=len(retained_pcm) // 2,
        suffix=suffix,
    )
    assert (
        runtime.submit(
            _pcm(1),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=head,
        )
        is False
    )

    await runtime.wait_idle()

    assert bytes(runtime._buffers[head].pcm16) == retained_pcm
    assert bytes(runtime._buffers[suffix].pcm16) == successor_pcm
    assert runtime._candidate_tokens[head].pcm_frozen is True
    assert runtime._candidate_tokens[suffix].scoring_deferred is True

    assert runtime.finish_candidate(head)
    assert runtime.activate_candidate(suffix)
    assert runtime.finish_candidate(suffix)
    await runtime.wait_idle()
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_reconcile_splits_deferred_head_after_activation_marker() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    head = _candidate(9_108)
    suffix = _candidate(9_109)
    retained_pcm = _tagged_pcm(600, 0x63)
    successor_pcm = _tagged_pcm(400, 0x74)

    assert runtime.defer_candidate(head)
    assert runtime.submit(
        retained_pcm + successor_pcm,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=head,
    )
    assert runtime.activate_candidate(head)
    assert runtime.reconcile_candidate_prefix(
        source=head,
        target=head,
        prefix_sample_count=len(retained_pcm) // 2,
        suffix=suffix,
    )

    await runtime.wait_idle()

    assert bytes(runtime._buffers[head].pcm16) == retained_pcm
    assert bytes(runtime._buffers[suffix].pcm16) == successor_pcm
    assert runtime._candidate_tokens[head].scoring_deferred is False
    assert runtime._candidate_tokens[suffix].scoring_deferred is True

    assert runtime.finish_candidate(head)
    assert runtime.activate_candidate(suffix)
    assert runtime.finish_candidate(suffix)
    await runtime.wait_idle()
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_reconcile_during_first_checkpoint_stops_second_until_split() -> None:
    score_started = _spawn_event()
    score_release = _spawn_event()
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            score_value=0.2,
            block_stage="score",
            stage_started=score_started,
            stage_release=score_release,
        ),
        config=_provider_gate_config(),
        on_observation=observe,
    )
    head = _candidate(9_110)
    suffix = _candidate(9_111)

    assert runtime.submit(
        _pcm(3_000),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=head,
    )
    await _wait_until(score_started.is_set)
    assert runtime.reconcile_candidate_prefix(
        source=head,
        target=head,
        prefix_sample_count=SPEAKER_SHADOW_SAMPLE_RATE_HZ * 2_000 // 1_000,
        suffix=suffix,
    )

    score_release.set()
    await runtime.wait_idle()

    assert [item.checkpoint_ms for item in observations] == [1_500]
    assert runtime._buffers[head].sample_count == (
        SPEAKER_SHADOW_SAMPLE_RATE_HZ * 2_000 // 1_000
    )
    assert runtime._buffers[suffix].sample_count == SPEAKER_SHADOW_SAMPLE_RATE_HZ

    assert runtime.finish_candidate(head)
    assert runtime.activate_candidate(suffix)
    assert runtime.finish_candidate(suffix)
    await runtime.wait_idle()
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_reconcile_into_terminal_scored_head_preserves_verdict_and_wipes_tail() -> (
    None
):
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_observation=observe,
    )
    head = _candidate(9_108)
    tail = _candidate(9_109)

    assert runtime.submit(
        _pcm(3_000),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=head,
    )
    await runtime.wait_idle()
    assert runtime._finalized[head].terminal_reason == "scored"
    assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]

    assert runtime.defer_candidate(tail)
    assert runtime.submit(
        _pcm(500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=tail,
    )
    assert runtime.reconcile_candidate_prefix(
        source=tail,
        target=head,
        prefix_sample_count=SPEAKER_SHADOW_SAMPLE_RATE_HZ * 500 // 1_000,
    )
    assert runtime.finish_candidate(tail)
    await runtime.wait_idle()

    assert runtime._finalized[head].terminal_reason == "scored"
    assert runtime._finalized[tail].terminal_reason == "dropped"
    assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]
    assert (
        runtime.submit(
            _pcm(1),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=tail,
        )
        is False
    )
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_reconcile_data_queue_full_preserves_original_sample_ownership() -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(
            queue_capacity=3,
            terminal_queue_capacity=2,
        ),
        on_observation=observe,
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    head = _candidate(9_112)
    tail = _candidate(9_113)
    suffix = _candidate(9_114)

    try:
        assert runtime.submit(
            _pcm(800),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=head,
        )
        assert runtime.defer_candidate(tail)
        assert runtime.submit(
            _pcm(500),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=tail,
        )
        head_token = runtime._candidate_tokens[head]
        tail_token = runtime._candidate_tokens[tail]
        head_samples = head_token.accepted_sample_count
        tail_samples = tail_token.accepted_sample_count
        assert runtime.snapshot()["queued_item_count"] == 3

        assert (
            runtime.reconcile_candidate_prefix(
                source=tail,
                target=head,
                prefix_sample_count=tail_samples // 2,
                suffix=suffix,
            )
            is False
        )

        assert head_token.accepted_sample_count == head_samples
        assert tail_token.accepted_sample_count == tail_samples
        assert head_token.pcm_frozen is False
        assert tail_token.pcm_frozen is False
        assert suffix not in runtime._candidate_tokens
        assert runtime.finish_candidate(head)
        assert runtime.finish_candidate(tail)

        hold_worker.set()
        await fake_worker
        await runtime.wait_idle()

        assert observations == []
        assert runtime._finalized[head].terminal_reason == "insufficient"
        assert runtime._finalized[tail].terminal_reason == "insufficient"
        assert suffix not in runtime._buffers
        assert suffix not in runtime._finalized
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_reconcile_terminal_queue_full_drops_reserved_ownership_fail_open() -> (
    None
):
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(
            queue_capacity=4,
            terminal_queue_capacity=1,
        ),
        on_observation=observe,
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    blocker = _candidate(9_115)
    head = _candidate(9_116)
    tail = _candidate(9_117)

    try:
        assert runtime.finish_candidate(blocker)
        assert runtime.submit(
            _pcm(800),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=head,
        )
        assert runtime.defer_candidate(tail)
        assert runtime.submit(
            _pcm(500),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=tail,
        )
        assert runtime.reconcile_candidate_prefix(
            source=tail,
            target=head,
            prefix_sample_count=SPEAKER_SHADOW_SAMPLE_RATE_HZ * 500 // 1_000,
        )
        assert runtime._candidate_tokens[tail].pcm_frozen is True

        # The terminal channel is independently saturated. Rejecting head's
        # finish must retire the reserved target instead of publishing a
        # partially reconciled candidate.
        assert runtime.finish_candidate(head) is False
        assert runtime._finalized[head].terminal_reason == "dropped"

        hold_worker.set()
        await fake_worker
        await runtime.wait_idle()

        assert observations == []
        assert runtime._finalized[head].terminal_reason == "dropped"
        assert runtime._finalized[tail].terminal_reason == "dropped"
        assert (
            runtime.submit(
                _pcm(1),
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                candidate=head,
            )
            is False
        )
        assert (
            runtime.submit(
                _pcm(1),
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                candidate=tail,
            )
            is False
        )
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("boundary", ["reset", "close"])
async def test_pending_reconcile_marker_lifecycle_boundary_wipes_all_pcm(
    boundary: str,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(queue_capacity=4),
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    head = _candidate(9_118)
    tail = _candidate(9_119)
    suffix = _candidate(9_120)

    try:
        assert runtime.submit(
            _tagged_pcm(800, 0x31),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=head,
        )
        assert runtime.defer_candidate(tail)
        assert runtime.submit(
            _tagged_pcm(500, 0x42),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=tail,
        )
        assert runtime.reconcile_candidate_prefix(
            source=tail,
            target=head,
            prefix_sample_count=SPEAKER_SHADOW_SAMPLE_RATE_HZ * 250 // 1_000,
            suffix=suffix,
        )
        queued_pcm = [
            item.pcm16
            for item in tuple(runtime._queue._queue)
            if isinstance(item, _AudioFrame)
        ]
        assert len(queued_pcm) == 2
        assert runtime._candidate_tokens[tail].pcm_frozen is True
        assert suffix in runtime._candidate_tokens

        if boundary == "reset":
            await runtime.reset()
        else:
            await runtime.close()

        assert not runtime._candidate_tokens
        assert not runtime._buffers
        assert not runtime._finalized
        assert all(not any(pcm16) for pcm16 in queued_pcm)
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
        assert runtime.snapshot()["queued_audio_bytes"] == 0
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_batch_reconcile_merges_pause_and_owns_finish_checkpoint_order() -> None:
    observations: list[SpeakerShadowObservation] = []
    completions: list[SpeakerShadowCompletion] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_observation=observe,
        on_completion=complete,
    )
    head = _candidate(9_201)
    tail = _candidate(9_202)
    assert runtime.submit(
        _pcm(800),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=head,
    )
    assert runtime.defer_candidate(tail)
    assert runtime.submit(
        _pcm(2_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=tail,
    )

    receipt = runtime.reconcile_candidate_batch(
        SpeakerShadowBatchReconcileRequest(
            sources=(
                _reconcile_source(head, 800),
                _reconcile_source(tail, 2_500),
            ),
            target=head,
        )
    )
    assert receipt is not None
    assert runtime.reconciliation_status(receipt) == "pending"
    assert runtime._candidate_tokens[head].finish_state == "queued"
    assert runtime.snapshot()["pending_terminal_count"] == 1

    await runtime.wait_idle()

    assert runtime.reconciliation_status(receipt) == "applied"
    assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]
    assert completions == [SpeakerShadowCompletion(head, "scored", 3_000)]
    assert runtime._finalized[head].terminal_reason == "scored"
    assert runtime._finalized[tail].terminal_reason == "dropped"
    assert runtime.snapshot()["pending_terminal_count"] == 0
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 1
    assert runtime.snapshot()["reconciliation_batch_applied_count"] == 1
    assert runtime.snapshot()["reconciliation_batch_failed_count"] == 0
    assert runtime.snapshot()["reconciliation_batch_revoked_count"] == 0
    await runtime.close()


async def test_prepared_exact_interval_is_invisible_until_commit_and_abort_restores_source() -> (
    None
):
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    source = _candidate(9_250)
    target = _candidate(9_251)
    suffix = _candidate(9_252)
    try:
        assert runtime.submit(
            _tagged_pcm(1_000, 0x31),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        source_token = runtime._candidate_tokens[source]
        original_samples = source_token.accepted_sample_count
        receipt = runtime.prepare_exact_interval(
            SpeakerShadowBatchReconcileRequest(
                sources=(
                    _reconcile_source(
                        source,
                        1_000,
                        keep_start_ms=200,
                        keep_end_ms=700,
                    ),
                ),
                target=target,
                suffix=suffix,
            )
        )

        assert receipt is not None
        assert source_token.pcm_frozen is True
        assert target in runtime._candidate_tokens
        assert suffix in runtime._candidate_tokens
        assert not runtime._reconciliations
        assert all(
            not isinstance(item, _CandidateBatchReconciliation)
            for item in tuple(runtime._queue._queue)
        )
        assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 0

        assert runtime.abort_exact_interval(receipt)
        assert not runtime.abort_exact_interval(receipt)
        assert source_token.pcm_frozen is False
        assert source_token.accepted_sample_count == original_samples
        assert source_token.finish_state == "open"
        assert target not in runtime._candidate_tokens
        assert suffix not in runtime._candidate_tokens
        assert runtime._queued_terminal_count == 0
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_prepared_exact_interval_commit_publishes_one_marker_and_successor() -> (
    None
):
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
        on_completion=complete,
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    source = _candidate(9_253)
    target = _candidate(9_254)
    suffix = _candidate(9_255)
    try:
        assert runtime.submit(
            _tagged_pcm(1_000, 0x41),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        receipt = runtime.prepare_exact_interval(
            SpeakerShadowBatchReconcileRequest(
                sources=(
                    _reconcile_source(
                        source,
                        1_000,
                        keep_start_ms=200,
                        keep_end_ms=700,
                    ),
                ),
                target=target,
                suffix=suffix,
            )
        )
        assert receipt is not None
        assert completions == []
        staged = runtime.submit_capture(
            _tagged_pcm(100, 0x52),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        assert staged.accepted_sample_count == 100 * 16
        assert staged.cumulative_sample_count == 1_100 * 16
        assert runtime.commit_exact_interval(receipt)
        assert not runtime.commit_exact_interval(receipt)
        assert len(runtime._reconciliations) == 1
        assert sum(
            isinstance(item, _CandidateBatchReconciliation)
            for item in tuple(runtime._queue._queue)
        ) == 1

        hold_worker.set()
        await fake_worker
        runtime._worker_task = None
        await runtime.wait_idle()

        assert runtime.reconciliation_status(receipt) == "applied"
        assert runtime._finalized[source].terminal_reason == "dropped"
        assert bytes(runtime._buffers[suffix].pcm16) == (
            _tagged_pcm(300, 0x41) + _tagged_pcm(100, 0x52)
        )
        assert completions == [SpeakerShadowCompletion(target, "insufficient", None)]
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_prepared_exact_interval_abort_reports_staged_pcm_restore_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    source = _candidate(9_262)
    target = _candidate(9_263)
    suffix = _candidate(9_264)
    try:
        assert runtime.submit(
            _tagged_pcm(1_000, 0x61),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        source_token = runtime._candidate_tokens[source]
        original_samples = source_token.accepted_sample_count
        receipt = runtime.prepare_exact_interval(
            SpeakerShadowBatchReconcileRequest(
                sources=(
                    _reconcile_source(
                        source,
                        1_000,
                        keep_start_ms=200,
                        keep_end_ms=700,
                    ),
                ),
                target=target,
                suffix=suffix,
            )
        )
        assert receipt is not None
        staged = runtime.submit_capture(
            _tagged_pcm(100, 0x62),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        assert staged.accepted_sample_count == 100 * 16
        scratch = runtime._prepared_exact_intervals[
            receipt.batch_id
        ].suffix_scratch_pcm16
        assert any(scratch)
        monkeypatch.setattr(runtime, "_ensure_worker", lambda: False)

        assert not runtime.abort_exact_interval(receipt)
        assert not runtime._prepared_exact_intervals
        assert source_token.accepted_sample_count == original_samples
        assert target not in runtime._candidate_tokens
        assert suffix not in runtime._candidate_tokens
        assert not any(scratch)
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_prepared_exact_interval_rejects_forged_stale_and_repeated_receipts() -> (
    None
):
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    source = _candidate(9_256)
    assert runtime.submit(
        _pcm(400),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=source,
    )
    receipt = runtime.prepare_exact_interval(
        SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(source, 400),),
            target=source,
        )
    )
    assert receipt is not None
    forged = SpeakerShadowBatchReconcileReceipt(
        runtime_generation=receipt.runtime_generation,
        batch_id=receipt.batch_id,
        target=receipt.target,
        suffix=receipt.suffix,
        target_sample_count=receipt.target_sample_count,
        suffix_sample_count=receipt.suffix_sample_count,
        _owner=object(),
    )
    assert not runtime.commit_exact_interval(forged)
    assert not runtime.abort_exact_interval(forged)
    assert runtime.abort_exact_interval(receipt)
    assert not runtime.abort_exact_interval(receipt)
    assert not runtime.commit_exact_interval(receipt)
    await runtime.close()


async def test_prepared_exact_interval_abort_wipes_unexpected_provisional_buffer() -> (
    None
):
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    source = _candidate(9_257)
    target = _candidate(9_258)
    assert runtime.submit(
        _pcm(400),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=source,
    )
    receipt = runtime.prepare_exact_interval(
        SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(source, 400),),
            target=target,
        )
    )
    assert receipt is not None
    token = runtime._candidate_tokens[target]
    scratch = bytearray(b"\x55\x00" * 10)
    runtime._buffers[target] = _CandidateBuffer(
        token=token,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        pcm16=scratch,
        sample_count=10,
    )

    assert runtime.abort_exact_interval(receipt)
    assert target not in runtime._buffers
    assert not any(scratch)
    await runtime.close()


@pytest.mark.parametrize("operation", ["reset", "close"])
async def test_prepared_exact_interval_is_automatically_aborted_on_lifecycle_reset(
    operation: str,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    source = _candidate(9_259)
    target = _candidate(9_260)
    suffix = _candidate(9_261)
    assert runtime.submit(
        _pcm(400),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=source,
    )
    receipt = runtime.prepare_exact_interval(
        SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(source, 400, keep_end_ms=300),),
            target=target,
            suffix=suffix,
        )
    )
    assert receipt is not None
    staged = runtime.submit_capture(
        _pcm(50),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=source,
    )
    assert staged.accepted_sample_count == 50 * 16
    scratch = runtime._prepared_exact_intervals[
        receipt.batch_id
    ].suffix_scratch_pcm16
    assert any(scratch)

    if operation == "reset":
        await runtime.reset()
    else:
        await runtime.close()

    assert not runtime._prepared_exact_intervals
    assert runtime._queued_data_item_count == 0
    assert runtime._queued_terminal_count == 0
    assert not any(scratch)
    assert not runtime.abort_exact_interval(receipt)
    await runtime.close()


async def test_batch_reconcile_double_split_preserves_suffix_pcm_submitted_after_marker() -> (
    None
):
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    source = _candidate(9_203)
    target = _candidate(9_204)
    suffix = _candidate(9_205)
    finish_started = asyncio.Event()
    finish_release = asyncio.Event()
    original_process_finish = runtime._process_finish

    async def hold_process_finish(marker: _CandidateFinished) -> None:
        finish_started.set()
        await finish_release.wait()
        await original_process_finish(marker)

    runtime._process_finish = hold_process_finish  # type: ignore[method-assign]
    try:
        assert runtime.submit(
            _tagged_pcm(1_000, 0x44),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        receipt = runtime.reconcile_candidate_batch(
            SpeakerShadowBatchReconcileRequest(
                sources=(
                    _reconcile_source(
                        source,
                        1_000,
                        keep_start_ms=200,
                        keep_end_ms=700,
                    ),
                ),
                target=target,
                suffix=suffix,
            )
        )
        assert receipt is not None
        assert runtime.submit(
            _tagged_pcm(100, 0x55),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=suffix,
        )

        await asyncio.wait_for(finish_started.wait(), 2.0)
        assert runtime.reconciliation_status(receipt) == "applied"
        assert bytes(runtime._buffers[target].pcm16) == _tagged_pcm(500, 0x44)
        assert bytes(runtime._buffers[suffix].pcm16) == _tagged_pcm(300, 0x44)
        assert runtime._buffers[suffix].sample_count == 300 * 16

        finish_release.set()
        await runtime.wait_idle()
        assert bytes(runtime._buffers[suffix].pcm16) == (
            _tagged_pcm(300, 0x44) + _tagged_pcm(100, 0x55)
        )
        assert runtime._buffers[suffix].sample_count == 400 * 16
        assert runtime._finalized[source].terminal_reason == "dropped"
        assert runtime._finalized[target].terminal_reason == "insufficient"
        assert runtime.activate_candidate(suffix)
        assert runtime.finish_candidate(suffix)
        await runtime.wait_idle()
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        finish_release.set()
        await runtime.close()


async def test_batch_successor_can_be_consumed_by_next_exact_batch() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    source = _candidate(9_215)
    suffix = _candidate(9_216)
    assert runtime.defer_candidate(source)
    assert runtime.submit(
        _tagged_pcm(1_000, 0x41),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=source,
    )
    first_receipt = runtime.reconcile_candidate_batch(
        SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(source, 1_000, keep_end_ms=600),),
            target=source,
            suffix=suffix,
        )
    )
    assert first_receipt is not None
    await runtime.wait_idle()
    assert runtime.reconciliation_status(first_receipt) == "applied"
    assert runtime._candidate_tokens[suffix].reconciliation_batch_id is None
    assert runtime._buffers[suffix].sample_count == 400 * 16

    assert runtime.submit(
        _tagged_pcm(400, 0x52),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=suffix,
    )
    second_receipt = runtime.reconcile_candidate_batch(
        SpeakerShadowBatchReconcileRequest(
            sources=(_reconcile_source(suffix, 800),),
            target=suffix,
        )
    )
    assert second_receipt is not None
    await runtime.wait_idle()

    assert runtime.reconciliation_status(second_receipt) == "applied"
    assert runtime._finalized[source].terminal_reason == "insufficient"
    assert runtime._finalized[suffix].terminal_reason == "insufficient"
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_revoke_applied_batch_wipes_once_and_retires_queue_counts_once() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    source = _candidate(9_217)
    target = _candidate(9_218)
    suffix = _candidate(9_219)
    finish_started = asyncio.Event()
    finish_release = asyncio.Event()
    original_process_finish = runtime._process_finish

    async def hold_process_finish(marker: _CandidateFinished) -> None:
        finish_started.set()
        await finish_release.wait()
        await original_process_finish(marker)

    runtime._process_finish = hold_process_finish  # type: ignore[method-assign]
    try:
        assert runtime.submit(
            _pcm(1_000),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        receipt = runtime.reconcile_candidate_batch(
            SpeakerShadowBatchReconcileRequest(
                sources=(
                    _reconcile_source(
                        source,
                        1_000,
                        keep_start_ms=200,
                        keep_end_ms=700,
                    ),
                ),
                target=target,
                suffix=suffix,
            )
        )
        assert receipt is not None
        await asyncio.wait_for(finish_started.wait(), 2.0)
        assert runtime.reconciliation_status(receipt) == "applied"
        assert runtime._queued_data_item_count == 1
        assert runtime._queued_terminal_count == 1

        runtime.revoke_reconciliation(receipt)
        assert runtime.reconciliation_status(receipt) == "failed"
        assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_applied_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_failed_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_revoked_count"] == 1
        assert runtime._finalized[target].terminal_reason == "dropped"
        assert runtime._finalized[suffix].terminal_reason == "dropped"
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
        assert runtime._queued_data_item_count == 1
        assert runtime._queued_terminal_count == 1

        finish_release.set()
        await runtime.wait_idle()
        assert runtime._queued_data_item_count == 0
        assert runtime._queued_terminal_count == 0
        runtime.revoke_reconciliation(receipt)
        assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_applied_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_failed_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_revoked_count"] == 1
        assert runtime._queued_data_item_count == 0
        assert runtime._queued_terminal_count == 0
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        finish_release.set()
        await runtime.close()


async def test_batch_reconcile_full_data_queue_has_no_partial_reservation() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(queue_capacity=3, terminal_queue_capacity=2),
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    head = _candidate(9_206)
    tail = _candidate(9_207)
    try:
        assert runtime.submit(
            _pcm(800),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=head,
        )
        assert runtime.defer_candidate(tail)
        assert runtime.submit(
            _pcm(500),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=tail,
        )
        head_token = runtime._candidate_tokens[head]
        tail_token = runtime._candidate_tokens[tail]
        head_samples = head_token.accepted_sample_count
        tail_samples = tail_token.accepted_sample_count

        assert (
            runtime.reconcile_candidate_batch(
                SpeakerShadowBatchReconcileRequest(
                    sources=(
                        _reconcile_source(head, 800),
                        _reconcile_source(tail, 500),
                    ),
                    target=head,
                )
            )
            is None
        )
        assert head_token.accepted_sample_count == head_samples
        assert tail_token.accepted_sample_count == tail_samples
        assert head_token.pcm_frozen is False
        assert tail_token.pcm_frozen is False
        assert head_token.finish_state == "open"
        assert not runtime._reconciliations
        assert runtime.snapshot()["pending_terminal_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_applied_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_failed_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_revoked_count"] == 0
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_batch_reconcile_full_terminal_queue_has_no_partial_reservation() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(queue_capacity=2, terminal_queue_capacity=1),
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    blocker = _candidate(9_213)
    source = _candidate(9_214)
    try:
        assert runtime.finish_candidate(blocker)
        assert runtime.submit(
            _pcm(800),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        token = runtime._candidate_tokens[source]
        accepted_samples = token.accepted_sample_count
        assert (
            runtime.reconcile_candidate_batch(
                SpeakerShadowBatchReconcileRequest(
                    sources=(_reconcile_source(source, 800),),
                    target=source,
                )
            )
            is None
        )
        assert token.accepted_sample_count == accepted_samples
        assert token.pcm_frozen is False
        assert token.finish_state == "open"
        assert not runtime._reconciliations
        assert runtime.snapshot()["pending_terminal_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_applied_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_failed_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_revoked_count"] == 0

        hold_worker.set()
        await fake_worker
        await runtime.wait_idle()
        assert runtime.finish_candidate(source)
        await runtime.wait_idle()
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_batch_reconcile_worker_cas_failure_wipes_and_tombstones_all() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(queue_capacity=4),
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    head = _candidate(9_208)
    tail = _candidate(9_209)
    try:
        assert runtime.submit(
            _pcm(800),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=head,
        )
        assert runtime.defer_candidate(tail)
        assert runtime.submit(
            _pcm(500),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=tail,
        )
        receipt = runtime.reconcile_candidate_batch(
            SpeakerShadowBatchReconcileRequest(
                sources=(
                    _reconcile_source(head, 800),
                    _reconcile_source(tail, 500),
                ),
                target=head,
            )
        )
        assert receipt is not None
        runtime._candidate_tokens[head].accepted_sample_count += 1

        hold_worker.set()
        await fake_worker
        await runtime.wait_idle()

        assert runtime.reconciliation_status(receipt) == "failed"
        assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_applied_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_failed_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_revoked_count"] == 0
        assert runtime._finalized[head].terminal_reason == "dropped"
        assert runtime._finalized[tail].terminal_reason == "dropped"
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
        assert (
            runtime.submit(
                _pcm(1),
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                candidate=head,
            )
            is False
        )
        assert (
            runtime.submit(
                _pcm(1),
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                candidate=tail,
            )
            is False
        )
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_submit_capture_keeps_finalized_score_complete_across_twelve_seconds() -> (
    None
):
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    candidate = _candidate(9_301)
    try:
        await _finalize_provider_candidate_score(runtime, candidate)

        for _ in range(9):
            result = runtime.submit_capture(
                _pcm(1_000),
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                candidate=candidate,
            )
            assert result.disposition is SpeakerShadowCaptureDisposition.COMPLETE
            assert result.decision_state is SpeakerShadowCaptureDecisionState.SCORED
            assert result.accepted_sample_count == 0
            assert result.cumulative_sample_count == 48_000
            assert result.completed_window_sample_count == 48_000

        assert (
            runtime.submit(
                _pcm(100),
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                candidate=candidate,
            )
            is False
        )
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        await runtime.close()


async def test_submit_capture_reports_backend_failure_as_unavailable() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_error=True),
        config=_provider_gate_config(),
    )
    candidate = _candidate(9_302)
    try:
        admitted = runtime.submit_capture(
            _pcm(1_500),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        assert admitted.disposition is SpeakerShadowCaptureDisposition.ACCEPTED
        await runtime.wait_idle()

        failed = runtime.submit_capture(
            _pcm(100),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        assert failed.disposition is SpeakerShadowCaptureDisposition.UNAVAILABLE
        assert failed.decision_state is SpeakerShadowCaptureDecisionState.UNAVAILABLE
        assert failed.accepted_sample_count == 0
    finally:
        await runtime.close()


async def test_abandon_candidate_fences_queued_pcm_without_terminal_evidence() -> None:
    evidence: list[SpeakerShadowObservation | SpeakerShadowCompletion] = []
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_evidence=evidence.append,
    )
    candidate = _candidate(9_312)
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker  # type: ignore[assignment]
    try:
        admitted = runtime.submit_capture(
            _pcm(1_500),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        assert admitted.accepted_sample_count == 24_000
        assert runtime.abandon_candidate(candidate)

        hold_worker.set()
        await fake_worker
        runtime._worker_task = None
        await runtime.wait_idle()

        assert evidence == []
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
        stale = runtime.submit_capture(
            _pcm(100),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        assert stale.disposition is SpeakerShadowCaptureDisposition.UNAVAILABLE
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_terminal_coverage_preserves_scored_window_for_long_exact_range() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_303)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        receipt = runtime.reconcile_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(_reconcile_source(target, 12_000),),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=192_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
            )
        )

        assert receipt is not None
        assert receipt.retained_sample_count == 48_000
        assert receipt.covered_sample_count == 192_000
        assert receipt.terminal_preserved is True
        assert runtime.terminal_coverage_status(receipt) == "pending"
        assert (
            await runtime.wait_reconciliation_settled(
                receipt,
                deadline=time.monotonic() + 2.0,
            )
            == "applied"
        )
        scored = runtime.submit_capture(
            _pcm(100),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=target,
        )
        assert scored.disposition is SpeakerShadowCaptureDisposition.COMPLETE
        assert scored.decision_state is SpeakerShadowCaptureDecisionState.SCORED
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        await runtime.close()


async def test_prepared_terminal_coverage_defers_consumption_until_commit() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_320)
    continuation = _candidate(9_321)
    suffix = _candidate(9_322)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        assert runtime.defer_coverage_candidate(continuation)
        initial_pcm = _tagged_pcm(500, 0x31)
        assert runtime.submit_capture(
            initial_pcm,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=continuation,
        ).accepted_sample_count == 8_000
        await runtime.wait_idle()

        receipt = runtime.prepare_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(
                    _reconcile_source(target, 3_000),
                    _reconcile_source(continuation, 500),
                ),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=56_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
                suffix=suffix,
            )
        )
        assert receipt is not None
        assert runtime.terminal_coverage_status(receipt) == "pending"
        assert runtime._buffers[continuation].pcm16 == bytearray(initial_pcm)

        staged_pcm = _tagged_pcm(100, 0x41)
        staged = runtime.submit_capture(
            staged_pcm,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=continuation,
        )
        assert staged.accepted_sample_count == 1_600
        assert runtime._buffers[continuation].pcm16 == bytearray(initial_pcm)

        assert runtime.commit_finalized_candidate_coverage(receipt)
        await runtime.wait_idle()
        assert runtime.terminal_coverage_status(receipt) == "applied"
        assert runtime._finalized[target].terminal_reason == "scored"
        assert runtime._finalized[continuation].terminal_reason == "dropped"
        assert runtime._buffers[suffix].pcm16 == bytearray(staged_pcm)
    finally:
        await runtime.close()


async def test_aborted_terminal_coverage_restores_staged_pcm_to_continuation() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_323)
    continuation = _candidate(9_324)
    suffix = _candidate(9_325)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        assert runtime.defer_coverage_candidate(continuation)
        initial_pcm = _tagged_pcm(500, 0x51)
        assert runtime.submit(
            initial_pcm,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=continuation,
        )
        await runtime.wait_idle()

        receipt = runtime.prepare_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(
                    _reconcile_source(target, 3_000),
                    _reconcile_source(continuation, 500),
                ),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=56_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
                suffix=suffix,
            )
        )
        assert receipt is not None
        staged_pcm = _tagged_pcm(100, 0x61)
        assert runtime.submit(
            staged_pcm,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=continuation,
        )

        assert runtime.abort_finalized_candidate_coverage(receipt)
        await runtime.wait_idle()
        assert runtime.terminal_coverage_status(receipt) == "stale"
        assert suffix not in runtime._candidate_tokens
        assert runtime._buffers[continuation].pcm16 == bytearray(
            initial_pcm + staged_pcm
        )
        assert runtime._finalized[target].terminal_reason == "scored"
    finally:
        await runtime.close()


async def test_prepared_terminal_coverage_capacity_rejects_without_consuming() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(buffered_candidate_capacity=2),
    )
    targets = tuple(_candidate(9_326 + index) for index in range(3))
    try:
        for target in targets:
            await _finalize_provider_candidate_score(runtime, target)

        def request(
            target: SpeakerShadowCandidateKey,
        ) -> SpeakerShadowTerminalCoverageRequest:
            return SpeakerShadowTerminalCoverageRequest(
                sources=(_reconcile_source(target, 3_000),),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=48_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
            )

        first = runtime.prepare_finalized_candidate_coverage(request(targets[0]))
        second = runtime.prepare_finalized_candidate_coverage(request(targets[1]))
        assert first is not None and second is not None
        assert runtime.terminal_coverage_status(first) == "pending"
        assert runtime.terminal_coverage_status(second) == "pending"

        assert runtime.prepare_finalized_candidate_coverage(request(targets[2])) is None
        assert runtime.terminal_coverage_status(first) == "pending"
        assert runtime.terminal_coverage_status(second) == "pending"
        assert runtime._finalized[targets[2]].terminal_reason == "scored"

        assert runtime.commit_finalized_candidate_coverage(first)
        assert runtime.abort_finalized_candidate_coverage(second)
        await runtime.wait_idle()
        assert runtime.terminal_coverage_status(first) == "applied"
        assert runtime.terminal_coverage_status(second) == "stale"
    finally:
        await runtime.close()


async def test_target_only_terminal_prepare_stages_pcm_into_suffix_on_commit() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_329)
    suffix = _candidate(9_330)
    staged_pcm = _tagged_pcm(100, 0x71)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        receipt = runtime.prepare_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(_reconcile_source(target, 3_000),),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=48_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
                suffix=suffix,
            )
        )
        assert receipt is not None

        staged = runtime.submit_capture(
            staged_pcm,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=target,
        )
        assert staged.accepted_sample_count == 1_600
        assert staged.decision_state is SpeakerShadowCaptureDecisionState.PENDING
        assert suffix not in runtime._buffers
        assert runtime._finalized[target].terminal_reason == "scored"

        assert runtime.commit_finalized_candidate_coverage(receipt)
        await runtime.wait_idle()
        assert runtime.terminal_coverage_status(receipt) == "applied"
        assert runtime._buffers[suffix].pcm16 == bytearray(staged_pcm)
        assert runtime._finalized[target].terminal_reason == "scored"
    finally:
        await runtime.close()


async def test_target_only_terminal_abort_reports_unrecoverable_staged_pcm() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_331)
    suffix = _candidate(9_332)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        receipt = runtime.prepare_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(_reconcile_source(target, 3_000),),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=48_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
                suffix=suffix,
            )
        )
        assert receipt is not None
        assert runtime.submit(
            _tagged_pcm(100, 0x72),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=target,
        )
        before_abort = runtime.snapshot()

        assert not runtime.abort_finalized_candidate_coverage(receipt)
        after_abort = runtime.snapshot()
        assert runtime.terminal_coverage_status(receipt) == "stale"
        assert suffix not in runtime._candidate_tokens
        assert runtime._finalized[target].terminal_reason == "scored"
        assert after_abort["dropped_frame_count"] == (
            before_abort["dropped_frame_count"] + 1
        )
        assert after_abort["dropped_audio_ms"] == (
            before_abort["dropped_audio_ms"] + 100
        )
    finally:
        await runtime.close()


async def test_terminal_coverage_may_trim_only_unscored_retained_tail() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_309)
    try:
        admitted = runtime.submit_capture(
            _pcm(4_000),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=target,
        )
        assert admitted.accepted_sample_count == 64_000
        assert admitted.disposition is SpeakerShadowCaptureDisposition.COMPLETE
        await runtime.wait_idle()

        receipt = runtime.reconcile_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(_reconcile_source(target, 3_100),),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=49_600,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
            )
        )
        assert receipt is not None
        assert receipt.retained_sample_count == 48_000
        assert receipt.covered_sample_count == 49_600
        assert (
            await runtime.wait_reconciliation_settled(
                receipt,
                deadline=time.monotonic() + 2.0,
            )
            == "applied"
        )
    finally:
        await runtime.close()


async def test_terminal_coverage_pending_marker_accepts_next_suffix_pcm_in_order() -> (
    None
):
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_310)
    suffix = _candidate(9_311)
    hold_worker = asyncio.Event()
    fake_worker: asyncio.Task[bool] | None = None
    try:
        await _finalize_provider_candidate_score(runtime, target)
        worker = runtime._worker_task
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        fake_worker = asyncio.create_task(hold_worker.wait())
        runtime._worker_task = fake_worker  # type: ignore[assignment]

        receipt = runtime.reconcile_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(
                    _reconcile_source(
                        target,
                        12_000,
                        keep_end_ms=10_000,
                    ),
                ),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=160_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
                suffix=suffix,
            )
        )
        assert receipt is not None
        assert runtime.terminal_coverage_status(receipt) == "pending"

        next_frame = runtime.submit_capture(
            _tagged_pcm(100, 0x41),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=suffix,
        )
        assert next_frame.disposition is SpeakerShadowCaptureDisposition.ACCEPTED
        assert next_frame.accepted_sample_count == 1_600
        assert runtime._candidate_tokens[suffix].accepted_sample_count == 1_600

        hold_worker.set()
        await fake_worker
        await runtime.wait_idle()

        assert runtime.terminal_coverage_status(receipt) == "applied"
        assert runtime._finalized[target].terminal_reason == "scored"
        assert runtime._candidate_tokens[suffix].accepted_sample_count == 1_600
        suffix_buffer = runtime._buffers[suffix]
        assert suffix_buffer.sample_count == 1_600
        assert suffix_buffer.pcm16 == bytearray(_tagged_pcm(100, 0x41))
        assert runtime.snapshot()["retained_pcm_bytes"] == len(_tagged_pcm(100, 0x41))

        assert runtime.activate_candidate(suffix)
        assert runtime.finish_candidate(suffix)
        await runtime.wait_idle()
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        hold_worker.set()
        if fake_worker is not None:
            await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_terminal_coverage_rejects_trimmed_scoring_window() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_304)
    suffix = _candidate(9_305)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        prefix_trimmed = SpeakerShadowTerminalCoverageRequest(
            sources=(
                _reconcile_source(
                    target,
                    12_000,
                    keep_start_ms=100,
                ),
            ),
            target=target,
            provider_exact_start_sample=1_600,
            provider_exact_end_sample=192_000,
            scored_window_start_sample=0,
            scored_window_end_sample=48_000,
        )
        too_short = SpeakerShadowTerminalCoverageRequest(
            sources=(
                _reconcile_source(
                    target,
                    12_000,
                    keep_end_ms=2_900,
                ),
            ),
            target=target,
            provider_exact_start_sample=0,
            provider_exact_end_sample=46_400,
            scored_window_start_sample=0,
            scored_window_end_sample=48_000,
            suffix=suffix,
        )
        wrong_scored_window = SpeakerShadowTerminalCoverageRequest(
            sources=(_reconcile_source(target, 12_000),),
            target=target,
            provider_exact_start_sample=0,
            provider_exact_end_sample=192_000,
            scored_window_start_sample=0,
            scored_window_end_sample=47_999,
        )

        assert runtime.reconcile_finalized_candidate_coverage(prefix_trimmed) is None
        assert runtime.reconcile_finalized_candidate_coverage(too_short) is None
        assert (
            runtime.reconcile_finalized_candidate_coverage(wrong_scored_window) is None
        )
        assert not runtime._terminal_coverages
        assert runtime._finalized[target].terminal_reason == "scored"
    finally:
        await runtime.close()


async def test_terminal_coverage_merges_live_resume_without_replacing_verdict() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_306)
    resumed = _candidate(9_307)
    try:
        await _finalize_provider_candidate_score(runtime, target)
        assert runtime.submit(
            _pcm(500),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=resumed,
        )
        receipt = runtime.reconcile_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(
                    _reconcile_source(target, 3_000),
                    _reconcile_source(resumed, 500),
                ),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=56_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
            )
        )

        assert receipt is not None
        await runtime.wait_idle()
        assert runtime.terminal_coverage_status(receipt) == "applied"
        assert runtime._finalized[target].terminal_reason == "scored"
        assert runtime._finalized[resumed].terminal_reason == "dropped"
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        await runtime.close()


async def test_pending_terminal_coverage_is_stale_after_reset() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
    )
    target = _candidate(9_308)
    hold_worker = asyncio.Event()
    fake_worker: asyncio.Task[bool] | None = None
    try:
        await _finalize_provider_candidate_score(runtime, target)
        worker = runtime._worker_task
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        fake_worker = asyncio.create_task(hold_worker.wait())
        runtime._worker_task = fake_worker  # type: ignore[assignment]
        receipt = runtime.reconcile_finalized_candidate_coverage(
            SpeakerShadowTerminalCoverageRequest(
                sources=(_reconcile_source(target, 12_000),),
                target=target,
                provider_exact_start_sample=0,
                provider_exact_end_sample=192_000,
                scored_window_start_sample=0,
                scored_window_end_sample=48_000,
            )
        )
        assert receipt is not None
        assert runtime.terminal_coverage_status(receipt) == "pending"
        assert (
            await runtime.wait_reconciliation_settled(
                receipt,
                deadline=time.monotonic() + 0.01,
            )
            == "pending"
        )

        await runtime.reset()

        assert runtime.terminal_coverage_status(receipt) == "stale"
        assert not runtime._terminal_coverages
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
    finally:
        hold_worker.set()
        if fake_worker is not None:
            await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("boundary", ["revoke", "reset", "close"])
async def test_pending_batch_lifecycle_boundary_is_fail_open_and_wipes_pcm(
    boundary: str,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(queue_capacity=3),
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    source = _candidate(9_210)
    target = _candidate(9_211)
    suffix = _candidate(9_212)
    try:
        assert runtime.submit(
            _tagged_pcm(1_000, 0x31),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=source,
        )
        receipt = runtime.reconcile_candidate_batch(
            SpeakerShadowBatchReconcileRequest(
                sources=(
                    _reconcile_source(
                        source,
                        1_000,
                        keep_start_ms=100,
                        keep_end_ms=900,
                    ),
                ),
                target=target,
                suffix=suffix,
            )
        )
        assert receipt is not None
        queued_pcm = [
            item.pcm16
            for item in tuple(runtime._queue._queue)
            if isinstance(item, _AudioFrame)
        ]
        if boundary == "revoke":
            runtime.revoke_reconciliation(receipt)
            assert runtime.reconciliation_status(receipt) == "failed"
            hold_worker.set()
            await fake_worker
            await runtime.wait_idle()
        elif boundary == "reset":
            await runtime.reset()
            assert runtime.reconciliation_status(receipt) == "stale"
        else:
            await runtime.close()
            assert runtime.reconciliation_status(receipt) == "stale"

        assert all(not any(pcm16) for pcm16 in queued_pcm)
        assert runtime.snapshot()["retained_pcm_bytes"] == 0
        assert runtime.snapshot()["queued_audio_bytes"] == 0
        assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_applied_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_failed_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_revoked_count"] == 1
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()
        assert runtime.snapshot()["reconciliation_batch_admitted_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_applied_count"] == 0
        assert runtime.snapshot()["reconciliation_batch_failed_count"] == 1
        assert runtime.snapshot()["reconciliation_batch_revoked_count"] == 1


async def test_deferred_finish_before_activation_is_fail_open_and_wipes_pcm() -> None:
    observations: list[SpeakerShadowObservation] = []
    completions: list[SpeakerShadowCompletion] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(9_002)

    assert runtime.defer_candidate(candidate)
    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    assert runtime.activate_candidate(candidate) is False
    await runtime.wait_idle()

    assert observations == []
    assert completions == [SpeakerShadowCompletion(candidate, "insufficient", None)]
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_defer_must_precede_first_pcm_and_reset_wipes_buffer() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(),
    )
    ordinary_candidate = _candidate(9_003)
    deferred_candidate = _candidate(9_004)

    assert runtime.submit(
        _pcm(100),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=ordinary_candidate,
    )
    assert runtime.defer_candidate(ordinary_candidate) is False
    assert runtime.defer_candidate(deferred_candidate)
    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=deferred_candidate,
    )
    await runtime.wait_idle()
    assert runtime.snapshot()["retained_pcm_bytes"] > 0

    await runtime.reset()

    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_defer_requires_scope_enabled_for_pending_observation_gate() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=SpeakerShadowConfig(
            enabled=True,
            minimum_audio_ms=1_500,
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=("provider_candidate",),
        ),
    )
    provider_candidate = _candidate(9_005)
    smart_turn_candidate = SpeakerShadowCandidateKey(
        detector_epoch=1,
        shadow_generation=9_006,
        scope="smart_turn_turn",
    )

    assert runtime.supports_deferred_candidate(provider_candidate) is False
    assert runtime.supports_deferred_candidate(smart_turn_candidate) is False
    assert runtime.defer_candidate(provider_candidate) is False
    assert runtime.defer_candidate(smart_turn_candidate) is False
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_deferred_first_frame_prewarms_without_scoring() -> None:
    score_started = _spawn_event()
    score_release = _spawn_event()
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            block_stage="score",
            stage_started=score_started,
            stage_release=score_release,
        ),
        config=_provider_gate_config(prewarm=True),
    )
    candidate = _candidate(9_005)

    assert runtime.defer_candidate(candidate)
    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()

    assert runtime.snapshot()["backend_loaded_count"] == 1
    assert score_started.is_set() is False
    assert runtime.requires_provisional_decision(candidate) is False
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_deferred_predeclarations_are_bounded_without_evicting_owner() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(buffered_candidate_capacity=1),
    )
    retained = _candidate(9_006)
    rejected = _candidate(9_007)

    assert runtime.defer_candidate(retained)
    assert runtime.defer_candidate(rejected) is False
    assert runtime.activate_candidate(retained)
    assert runtime.finish_candidate(retained)
    await runtime.wait_idle()

    assert runtime.snapshot()["finished_candidate_count"] == 1
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.005)


class _InspectingHostConnection:
    def __init__(self) -> None:
        self._messages = iter(
            [
                ("load",),
                ("score", len(_pcm(10)), SPEAKER_SHADOW_SAMPLE_RATE_HZ),
                ("close",),
            ]
        )
        self.responses: list[tuple[object, ...]] = []
        self.score_pcm_cleared = False
        self.closed = False

    def recv(self) -> tuple[object, ...]:
        message = next(self._messages)
        if message[0] == "close":
            frame = inspect.currentframe()
            assert frame is not None and frame.f_back is not None
            retained_pcm = frame.f_back.f_locals.get("pcm16")
            self.score_pcm_cleared = retained_pcm is None or not any(retained_pcm)
        return message

    def send(self, response: tuple[object, ...]) -> None:
        self.responses.append(response)

    def close(self) -> None:
        self.closed = True


class _PollBlindProcess:
    """A live child whose only job is to keep the request path going."""

    pid = -1
    exitcode = None

    def is_alive(self) -> bool:
        return True

    # Reaping hooks, so a host that gives up on the response fails with the
    # timeout it actually hit instead of an AttributeError that hides it.
    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> None:
        return None

    def close(self) -> None:
        return None


class _PollBlindConnection:
    """A pipe end that answers a read but never admits readiness to ``poll``."""

    def __init__(self, response: tuple[object, ...]) -> None:
        self._response = response
        self.sent: list[tuple[object, ...]] = []
        self.polls = 0

    def poll(self, timeout: float = 0.0) -> bool:
        self.polls += 1
        return False

    def send(self, message: tuple[object, ...]) -> None:
        self.sent.append(message)

    def recv(self) -> tuple[object, ...]:
        return self._response

    def close(self) -> None:
        return None


async def test_host_response_survives_a_pipe_that_never_polls_ready() -> None:
    # Windows loses a response when the host waits on ``poll(0)``: each poll
    # starts an overlapped pipe read and cancels it in the same breath, and
    # that cancellation can swallow the very answer it asked about. The child
    # stays alive, the answer never comes back, and the request spins to a
    # false timeout — a live backend reported as "failed" roughly half the
    # time on a loaded runner. So the request path must not gate the read on
    # a readiness poll at all: a connection that answers ``recv`` but always
    # reports "not ready" has to complete the request anyway.
    host = _BackendProcessHost(
        factory=_BackendFactory(),
        terminate_timeout_seconds=0.1,
    )
    parent_connection = host._connection
    child_connection = host._child_connection
    assert parent_connection is not None and child_connection is not None
    parent_connection.close()
    child_connection.close()
    connection = _PollBlindConnection((True, True))
    host._connection = connection  # type: ignore[assignment]
    host._child_connection = None
    host._process = _PollBlindProcess()  # type: ignore[assignment]

    assert await host.load(timeout_seconds=1.0) is True
    assert connection.sent == [("load",)]


def test_backend_host_wipes_score_pcm_before_waiting_for_next_command() -> None:
    pcm16 = _pcm(10)
    pcm_buffer = bytearray(pcm16)
    connection = _InspectingHostConnection()

    _backend_host_main(  # type: ignore[arg-type]
        _BackendFactory(expected_pcm=pcm16),
        connection,
        pcm_buffer,
    )

    assert connection.score_pcm_cleared is True
    assert connection.closed is True


@pytest.mark.parametrize(
    ("config", "has_factory"),
    [
        (SpeakerShadowConfig(enabled=False), True),
        (SpeakerShadowConfig(enabled=True), False),
    ],
)
async def test_disabled_or_missing_factory_does_zero_work(
    config: SpeakerShadowConfig,
    has_factory: bool,
) -> None:
    before = _speaker_host_pids()
    factory = _BackendFactory() if has_factory else None
    runtime = SpeakerShadowRuntime(
        backend_factory=factory,
        config=config,
    )
    candidate = _candidate(1)

    assert runtime.enabled is False
    assert (
        runtime.submit(
            _pcm(20),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        is False
    )
    assert runtime.finish_candidate(candidate) is False
    await runtime.wait_idle()
    await runtime.close()

    metrics = runtime.snapshot()
    assert metrics["submitted_frame_count"] == 0
    assert metrics["queued_item_count"] == 0
    assert metrics["worker_task_count"] == 0
    assert metrics["cleanup_task_count"] == 0
    assert metrics["backend_loaded_count"] == 0
    assert metrics["backend_process_count"] == 0
    assert _speaker_host_pids() == before
    if factory is not None:
        assert factory.parent_close_calls == 1


async def test_ordered_finish_scores_once_and_rejects_late_pcm() -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    first = _pcm(10)
    second = _pcm(10)
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            score_value=0.2,
            expected_pcm=first + second,
        ),
        config=_config(),
        on_observation=observe,
    )
    candidate = _candidate(2)

    assert runtime.submit(
        first,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.submit(
        second,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert observations == [
        SpeakerShadowObservation(
            candidate=candidate,
            similarity=pytest.approx(0.2),
            would_block=(
                (0.40, True),
                (0.44, True),
                (0.48, True),
                (0.52, True),
                (0.55, True),
            ),
            audio_ms=20,
        )
    ]
    assert (
        runtime.submit(
            first,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        is False
    )
    assert runtime.finish_candidate(candidate)
    metrics = runtime.snapshot()
    assert metrics["scored_candidate_count"] == 1
    assert metrics["finished_candidate_count"] == 1
    assert metrics["evaluated_candidate_count"] == 1
    await runtime.close()


async def test_explicit_checkpoints_emit_1500ms_and_3000ms_observations() -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
        ),
        on_observation=observe,
    )
    candidate = _candidate(47)

    assert runtime.submit(
        _pcm(3_000),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()

    assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]
    assert [item.audio_ms for item in observations] == [1_500, 3_000]
    assert [item.candidate for item in observations] == [candidate, candidate]
    metrics = runtime.snapshot()
    assert metrics["scored_candidate_count"] == 1
    assert metrics["evaluated_candidate_count"] == 1
    assert metrics["would_block_at_0_4_count"] == 2
    assert metrics["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_3000ms_completion_follows_both_observations() -> None:
    events: list[tuple[str, int | None]] = []
    completions: list[SpeakerShadowCompletion] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        events.append(("observation", observation.checkpoint_ms))

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)
        events.append(("completion", completion.last_checkpoint_ms))

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
        ),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(147)

    assert runtime.submit(
        _pcm(3_000),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert events == [
        ("observation", 1_500),
        ("observation", 3_000),
        ("completion", 3_000),
    ]
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="scored",
            last_checkpoint_ms=3_000,
        )
    ]
    await runtime.close()


async def test_authoritative_evidence_sink_orders_two_observations_before_close() -> (
    None
):
    evidence: list[SpeakerShadowObservation | SpeakerShadowCompletion] = []
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_evidence=evidence.append,
    )
    candidate = _candidate(9_401)

    assert runtime.submit(
        _pcm(3_000),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert [type(item) for item in evidence] == [
        SpeakerShadowObservation,
        SpeakerShadowObservation,
        SpeakerShadowCompletion,
    ]
    observations = [
        item for item in evidence if isinstance(item, SpeakerShadowObservation)
    ]
    closed = evidence[-1]
    assert [item.sequence_no for item in observations] == [1, 2]
    assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]
    assert isinstance(closed, SpeakerShadowCompletion)
    assert closed.through_sequence_no == 2
    assert closed.evidence_complete is True
    await runtime.close()


async def test_legacy_callback_timeout_cannot_reorder_authoritative_evidence() -> None:
    evidence: list[SpeakerShadowObservation | SpeakerShadowCompletion] = []
    release_legacy = asyncio.Event()

    async def observe(_observation: SpeakerShadowObservation) -> None:
        while not release_legacy.is_set():
            try:
                await release_legacy.wait()
            except asyncio.CancelledError:
                continue

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(callback_timeout_seconds=0.01),
        on_evidence=evidence.append,
        on_observation=observe,
    )
    candidate = _candidate(9_402)

    assert runtime.submit(
        _pcm(3_000),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert [
        item.sequence_no
        for item in evidence
        if isinstance(item, SpeakerShadowObservation)
    ] == [1, 2]
    assert isinstance(evidence[-1], SpeakerShadowCompletion)
    assert evidence[-1].through_sequence_no == 2
    release_legacy.set()
    await runtime.close()


async def test_terminal_overflow_publishes_unavailable_then_closed() -> None:
    evidence: list[SpeakerShadowObservation | SpeakerShadowCompletion] = []
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_provider_gate_config(terminal_queue_capacity=1),
        on_evidence=evidence.append,
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    blocker = _candidate(9_403)
    candidate = _candidate(9_404)

    try:
        assert runtime.finish_candidate(blocker)
        assert runtime.finish_candidate(candidate) is False

        assert len(evidence) == 2
        unavailable, closed = evidence
        assert isinstance(unavailable, SpeakerShadowObservation)
        assert unavailable.candidate == candidate
        assert unavailable.sequence_no == 1
        assert unavailable.evidence_available is False
        assert isinstance(closed, SpeakerShadowCompletion)
        assert closed.candidate == candidate
        assert closed.through_sequence_no == 1
        assert closed.evidence_complete is True
    finally:
        hold_worker.set()
        await asyncio.gather(fake_worker, return_exceptions=True)
        await runtime.close()


async def test_duplicate_finish_emits_completion_once() -> None:
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(minimum_audio_ms=20),
        on_completion=complete,
    )
    candidate = _candidate(148)

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()
    assert runtime.finish_candidate(candidate)

    assert len(completions) == 1
    assert completions[0].candidate == candidate
    assert runtime.snapshot()["completion_count"] == 1
    await runtime.close()


async def test_score_failure_completion_is_ordered_after_prior_checkpoint() -> None:
    events: list[tuple[str, int | None, str | None]] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        events.append(("observation", observation.checkpoint_ms, None))

    async def complete(completion: SpeakerShadowCompletion) -> None:
        events.append(
            (
                "completion",
                completion.last_checkpoint_ms,
                completion.terminal_reason,
            )
        )

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_error_after=1),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
        ),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(149)

    assert runtime.submit(
        _pcm(3_000),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert events == [
        ("observation", 1_500, None),
        ("completion", 1_500, "failed"),
    ]
    assert runtime.snapshot()["completion_count"] == 1
    await runtime.close()


async def test_short_candidate_emits_no_3000ms_confirmation() -> None:
    observations: list[SpeakerShadowObservation] = []
    completions: list[SpeakerShadowCompletion] = []
    event_order: list[str] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)
        event_order.append(f"observation:{observation.checkpoint_ms}")

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)
        event_order.append("completion")

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
        ),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(48)

    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert [item.checkpoint_ms for item in observations] == [1_500]
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="scored",
            last_checkpoint_ms=1_500,
        )
    ]
    assert event_order == ["observation:1500", "completion"]
    metrics = runtime.snapshot()
    assert metrics["scored_candidate_count"] == 1
    assert metrics["insufficient_candidate_count"] == 0
    assert metrics["finished_candidate_count"] == 1
    assert metrics["evaluated_candidate_count"] == 1
    assert metrics["completion_count"] == 1
    assert metrics["completion_after_first_checkpoint_count"] == 1
    assert metrics["completion_before_first_checkpoint_count"] == 0
    assert metrics["retained_pcm_bytes"] == 0
    await runtime.close()


@pytest.mark.parametrize(
    ("duration_ms", "expected_kinds"),
    [
        (1_499, []),
        (1_500, ["checkpoint"]),
        (1_501, ["checkpoint", "completion_confirmation"]),
        (2_999, ["checkpoint", "completion_confirmation"]),
        (3_000, ["checkpoint", "checkpoint"]),
    ],
)
async def test_provider_completion_confirmation_respects_checkpoint_boundaries(
    duration_ms: int,
    expected_kinds: list[str],
) -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=("provider_candidate",),
        ),
        on_observation=observe,
    )
    candidate = _candidate(248 + duration_ms)

    assert runtime.submit(
        _pcm(duration_ms),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert [item.observation_kind for item in observations] == expected_kinds
    if "completion_confirmation" in expected_kinds:
        confirmation = observations[-1]
        assert confirmation.checkpoint_ms == 1_500
        assert confirmation.audio_ms == duration_ms
        assert confirmation.candidate == candidate
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_completion_confirmation_observation_precedes_single_completion() -> None:
    events: list[tuple[str, str | None, int | None]] = []
    completions: list[SpeakerShadowCompletion] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        events.append(
            (
                "observation",
                observation.observation_kind,
                observation.checkpoint_ms,
            )
        )

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)
        events.append(("completion", None, completion.last_checkpoint_ms))

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=("provider_candidate",),
        ),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(349)

    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert events == [
        ("observation", "checkpoint", 1_500),
        ("observation", "completion_confirmation", 1_500),
        ("completion", None, 1_500),
    ]
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="scored",
            last_checkpoint_ms=1_500,
        )
    ]
    assert runtime.snapshot()["completion_count"] == 1
    await runtime.close()


@pytest.mark.parametrize(
    ("scope", "score_value"),
    [
        ("provider_candidate", 0.9),
        ("smart_turn_turn", 0.2),
    ],
    ids=["first-owner", "non-provider"],
)
async def test_completion_confirmation_does_not_rescore_unarmed_candidate(
    scope: str,
    score_value: float,
) -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=score_value),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=("provider_candidate",),
        ),
        on_observation=observe,
    )
    candidate = _candidate(350, scope)

    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert [item.observation_kind for item in observations] == ["checkpoint"]
    assert [item.checkpoint_ms for item in observations] == [1_500]
    await runtime.close()


async def test_completion_confirmation_score_failure_still_completes_fail_open() -> (
    None
):
    events: list[tuple[str, str]] = []
    completions: list[SpeakerShadowCompletion] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        events.append(("observation", observation.observation_kind))

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)
        events.append(("completion", completion.terminal_reason))

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2, score_error_after=1),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=("provider_candidate",),
        ),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(351)

    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    assert events == [
        ("observation", "checkpoint"),
        ("completion", "failed"),
    ]
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="failed",
            last_checkpoint_ms=1_500,
        )
    ]
    metrics = runtime.snapshot()
    assert metrics["completion_count"] == 1
    assert metrics["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_uncooperative_confirmation_callback_cannot_suppress_completion() -> None:
    confirmation_started = asyncio.Event()
    release_confirmation = asyncio.Event()
    completion_delivered = asyncio.Event()
    completions: list[SpeakerShadowCompletion] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        if observation.observation_kind != "completion_confirmation":
            return
        confirmation_started.set()
        while not release_confirmation.is_set():
            try:
                await release_confirmation.wait()
            except asyncio.CancelledError:
                continue

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)
        completion_delivered.set()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=("provider_candidate",),
            callback_timeout_seconds=0.01,
        ),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(353)

    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await asyncio.wait_for(confirmation_started.wait(), 2.0)
    await asyncio.wait_for(completion_delivered.wait(), 1.0)

    release_confirmation.set()
    await runtime.wait_idle()
    await asyncio.sleep(0)

    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="scored",
            last_checkpoint_ms=1_500,
        )
    ]
    metrics = runtime.snapshot()
    assert metrics["completion_count"] == 1
    assert metrics["callback_failure_count"] == 1
    assert metrics["completion_callback_failure_count"] == 0
    assert metrics["retained_pcm_bytes"] == 0
    assert metrics["callback_task_count"] == 0
    await runtime.close()


async def test_close_tracks_and_reaps_detached_confirmation_callback() -> None:
    confirmation_started = asyncio.Event()
    completion_delivered = asyncio.Event()
    cancellation_count = 0

    async def observe(observation: SpeakerShadowObservation) -> None:
        nonlocal cancellation_count
        if observation.observation_kind != "completion_confirmation":
            return
        confirmation_started.set()
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_count += 1
                if cancellation_count >= 4:
                    raise

    async def complete(_completion: SpeakerShadowCompletion) -> None:
        completion_delivered.set()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=("provider_candidate",),
            callback_timeout_seconds=0.01,
        ),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(354)

    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await asyncio.wait_for(confirmation_started.wait(), 2.0)
    await asyncio.wait_for(completion_delivered.wait(), 1.0)
    await runtime.wait_idle()

    assert cancellation_count == 2
    assert runtime.snapshot()["callback_task_count"] == 1

    await asyncio.wait_for(runtime.close(), 2.0)

    assert cancellation_count >= 4
    assert runtime.snapshot()["callback_task_count"] == 0


async def test_reset_invalidates_in_flight_completion_confirmation() -> None:
    confirmation_started = asyncio.Event()
    completions: list[SpeakerShadowCompletion] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        if observation.observation_kind == "completion_confirmation":
            confirmation_started.set()
            await asyncio.Event().wait()

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=("provider_candidate",),
            callback_timeout_seconds=1.0,
        ),
        on_observation=observe,
        on_completion=complete,
    )
    candidate = _candidate(352)

    assert runtime.submit(
        _pcm(2_999),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await asyncio.wait_for(confirmation_started.wait(), 2.0)
    assert (
        runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        is False
    )

    await runtime.reset()
    await runtime.wait_idle()

    assert completions == []
    metrics = runtime.snapshot()
    assert metrics["retained_pcm_bytes"] == 0
    assert metrics["buffered_candidate_count"] == 0
    await runtime.close()


async def test_provisional_decision_tracks_first_checkpoint_delivery() -> None:
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()

    async def observe(_observation: SpeakerShadowObservation) -> None:
        callback_started.set()
        await callback_release.wait()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(callback_timeout_seconds=1.0),
        on_observation=observe,
    )
    candidate = _candidate(353)

    assert runtime.submit(
        _pcm(1_499),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.requires_provisional_decision(candidate) is False
    assert runtime.submit(
        _pcm(1),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.requires_provisional_decision(candidate) is True

    await asyncio.wait_for(callback_started.wait(), 2.0)
    assert runtime.requires_provisional_decision(candidate) is True
    callback_release.set()
    await runtime.wait_idle()

    assert runtime.requires_provisional_decision(candidate) is False
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()
    assert runtime.requires_provisional_decision(candidate) is False
    await runtime.close()


async def test_failed_checkpoint_callback_keeps_provisional_decision() -> None:
    async def observe(_observation: SpeakerShadowObservation) -> None:
        raise RuntimeError("checkpoint callback failed")

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(),
        on_observation=observe,
    )
    candidate = _candidate(354)

    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()

    assert runtime.requires_provisional_decision(candidate) is True
    assert runtime.snapshot()["callback_failure_count"] == 1
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()
    assert runtime.requires_provisional_decision(candidate) is False
    await runtime.close()


async def test_reset_prevents_late_callback_from_delivering_checkpoint() -> None:
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()

    async def observe(_observation: SpeakerShadowObservation) -> None:
        callback_started.set()
        try:
            await callback_release.wait()
        except asyncio.CancelledError:
            await callback_release.wait()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(callback_timeout_seconds=1.0),
        on_observation=observe,
    )
    candidate = _candidate(355)

    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await asyncio.wait_for(callback_started.wait(), 2.0)
    assert runtime.requires_provisional_decision(candidate) is True

    await runtime.reset()
    assert runtime.requires_provisional_decision(candidate) is False
    callback_release.set()
    await runtime.wait_idle()

    assert runtime.requires_provisional_decision(candidate) is False
    await runtime.close()


@pytest.mark.parametrize(
    ("config", "scope"),
    [
        (_config(minimum_audio_ms=1_500, maximum_audio_ms=4_000), "provider_candidate"),
        (_provider_gate_config(), "smart_turn_turn"),
    ],
    ids=["default-off", "smart-turn"],
)
async def test_provisional_decision_is_scope_gated(
    config: SpeakerShadowConfig,
    scope: str,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=config,
    )
    candidate = _candidate(356, scope)

    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.requires_provisional_decision(candidate) is False
    await runtime.wait_idle()
    assert runtime.requires_provisional_decision(candidate) is False
    await runtime.close()


async def test_provider_prewarm_loads_once_without_scoring() -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(prewarm=True),
        on_observation=observe,
    )
    candidate = _candidate(357)

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["load_count"] == 1
    assert metrics["evaluated_candidate_count"] == 0
    assert metrics["would_block_count"] == 0
    assert metrics["active_audio_bytes"] == 0
    assert observations == []
    await runtime.close()


@pytest.mark.parametrize(
    ("config", "scope"),
    [
        (_provider_gate_config(), "provider_candidate"),
        (_provider_gate_config(prewarm=True), "smart_turn_turn"),
    ],
    ids=["prewarm-default-off", "smart-turn"],
)
async def test_backend_prewarm_is_scope_gated(
    config: SpeakerShadowConfig,
    scope: str,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=config,
    )
    candidate = _candidate(358, scope)

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()

    assert runtime.snapshot()["load_count"] == 0
    assert runtime.snapshot()["backend_process_count"] == 0
    await runtime.close()


async def test_backend_prewarm_failure_finalizes_candidate_fail_open() -> None:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(load_ok=False),
        config=_provider_gate_config(prewarm=True),
        on_observation=observe,
    )
    candidate = _candidate(359)

    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["failed_candidate_count"] == 1
    assert metrics["retained_pcm_bytes"] == 0
    assert observations == []
    assert runtime.requires_provisional_decision(candidate) is False
    await runtime.close()


async def test_reset_invalidates_in_flight_backend_prewarm() -> None:
    load_started = _spawn_event()
    load_release = _spawn_event()
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            block_stage="load",
            stage_started=load_started,
            stage_release=load_release,
        ),
        config=_provider_gate_config(prewarm=True),
    )
    candidate = _candidate(360)

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await _wait_until(load_started.is_set)

    await runtime.reset()
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    # Reset now cancels the separately owned cold load and reaps its host.
    # Do not touch the multiprocessing Event after terminating its waiter.
    assert runtime._backend_load_task is None
    assert metrics["backend_process_count"] == 0
    assert metrics["load_count"] == 0
    assert metrics["retained_pcm_bytes"] == 0
    assert runtime.requires_provisional_decision(candidate) is False
    await runtime.close()


async def test_close_cancels_in_flight_backend_prewarm() -> None:
    load_started = _spawn_event()
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            block_stage="load",
            stage_started=load_started,
        ),
        config=_provider_gate_config(
            prewarm=True,
            shutdown_grace_seconds=0.05,
        ),
    )

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(361),
    )
    await _wait_until(load_started.is_set)

    await asyncio.wait_for(runtime.close(), 2.0)

    metrics = runtime.snapshot()
    assert metrics["worker_task_count"] == 0
    assert metrics["backend_process_count"] == 0
    assert metrics["retained_pcm_bytes"] == 0


async def test_checkpoint_callback_failure_does_not_block_confirmation() -> None:
    seen_checkpoints: list[int | None] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        if observation.checkpoint_ms == 1_500:
            raise RuntimeError("first checkpoint callback failed")
        seen_checkpoints.append(observation.checkpoint_ms)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
        ),
        on_observation=observe,
    )

    assert runtime.submit(
        _pcm(3_000),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(49),
    )
    await runtime.wait_idle()

    assert seen_checkpoints == [3_000]
    assert runtime.snapshot()["callback_failure_count"] == 1
    assert runtime.snapshot()["scored_candidate_count"] == 1
    await runtime.close()


async def test_checkpoint_accepts_pcm_while_intermediate_score_is_running() -> None:
    score_started = _spawn_event()
    score_release = _spawn_event()
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            score_value=0.2,
            block_stage="score",
            stage_started=score_started,
            stage_release=score_release,
        ),
        config=SpeakerShadowConfig(
            enabled=True,
            observation_checkpoints_ms=(1_500, 3_000),
        ),
        on_observation=observe,
    )
    candidate = _candidate(50)

    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await _wait_until(score_started.is_set)
    for _ in range(250):
        assert runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
    score_release.set()
    await runtime.wait_idle()

    assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]
    assert runtime.snapshot()["scored_candidate_count"] == 1
    assert runtime.snapshot()["submitted_audio_ms"] == 4_000
    await runtime.close()


async def test_intermediate_score_failure_wipes_buffer_and_fails_open() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_error=True),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
        ),
    )

    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(51),
    )
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["failed_candidate_count"] == 1
    assert metrics["retained_pcm_bytes"] == 0
    assert metrics["buffered_candidate_count"] == 0
    await runtime.close()


async def test_finish_releases_short_buffer_without_starting_host() -> None:
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            minimum_audio_ms=1_500,
            maximum_audio_ms=4_000,
            observation_checkpoints_ms=(1_500, 3_000),
        ),
        on_completion=complete,
    )
    candidate = _candidate(3)

    assert runtime.submit(
        _pcm(1_499),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["insufficient_candidate_count"] == 1
    assert metrics["finished_candidate_count"] == 1
    assert metrics["backend_process_count"] == 0
    assert metrics["buffered_audio_bytes"] == 0
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="insufficient",
            last_checkpoint_ms=None,
        )
    ]
    assert metrics["completion_count"] == 1
    assert metrics["completion_before_first_checkpoint_count"] == 1
    assert metrics["completion_after_first_checkpoint_count"] == 0
    await runtime.close()


async def test_candidate_pcm_is_capped_at_four_seconds_across_frames() -> None:
    expected = _pcm(1_000) * 4
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(expected_pcm=expected),
        config=_config(
            minimum_audio_ms=4_000,
            maximum_audio_ms=4_000,
            queue_capacity=8,
        ),
    )
    candidate = _candidate(4)

    for _ in range(4):
        assert runtime.submit(
            _pcm(1_000),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
    assert (
        runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        is False
    )
    await runtime.wait_idle()

    assert runtime.snapshot()["submitted_audio_ms"] == 4_000
    assert runtime.snapshot()["scored_candidate_count"] == 1
    await runtime.close()


async def test_single_lifecycle_preroll_payload_above_one_second_is_accepted() -> None:
    preroll = _pcm(2_500)
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(expected_pcm=preroll),
        config=_config(
            minimum_audio_ms=2_500,
            maximum_audio_ms=4_000,
        ),
    )
    candidate = _candidate(5)

    assert runtime.submit(
        preroll,
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["submitted_audio_ms"] == 2_500
    assert metrics["scored_candidate_count"] == 1
    assert metrics["retained_pcm_bytes"] == 0
    await runtime.close()


async def test_warm_worker_releases_the_last_parent_pcm_frame() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(minimum_audio_ms=10, idle_unload_seconds=60.0),
    )

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(46),
    )
    await runtime.wait_idle()

    worker = runtime._worker_task
    assert worker is not None and not worker.done()
    worker_frame = worker.get_coro().cr_frame
    assert worker_frame is not None
    assert worker_frame.f_locals.get("item") is None
    assert runtime.snapshot()["retained_pcm_bytes"] == 0
    await runtime.close()


@pytest.mark.parametrize(
    ("pcm16", "sample_rate_hz", "candidate"),
    [
        (b"", SPEAKER_SHADOW_SAMPLE_RATE_HZ, _candidate(5)),
        (b"\x00", SPEAKER_SHADOW_SAMPLE_RATE_HZ, _candidate(6)),
        (_pcm(10), 48_000, _candidate(7)),
        (_pcm(10), SPEAKER_SHADOW_SAMPLE_RATE_HZ, (1, 1)),
        (
            b"\x00" * (MAX_SPEAKER_SHADOW_FRAME_PCM_BYTES + 2),
            SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            _candidate(8),
        ),
    ],
    ids=["empty", "odd", "wrong-rate", "wrong-key", "oversized"],
)
async def test_invalid_or_oversized_frames_fail_open_without_starting_host(
    pcm16: bytes,
    sample_rate_hz: int,
    candidate: object,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(),
    )

    assert (
        runtime.submit(  # type: ignore[arg-type]
            pcm16,
            sample_rate_hz=sample_rate_hz,
            candidate=candidate,
        )
        is False
    )
    await runtime.wait_idle()
    assert runtime.snapshot()["backend_process_count"] == 0
    await runtime.close()


async def test_queue_saturation_drops_only_shadow_candidate() -> None:
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            minimum_audio_ms=20,
            queue_capacity=1,
            finalized_candidate_capacity=2,
        ),
        on_completion=complete,
    )
    candidate = _candidate(9)

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert (
        runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        is False
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["dropped_frame_count"] == 1
    assert metrics["dropped_candidate_count"] == 1
    assert metrics["finished_candidate_count"] == 1
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="dropped",
            last_checkpoint_ms=None,
        )
    ]
    assert metrics["backend_process_count"] == 0
    await runtime.close()


async def test_pcm_capacity_saturation_preserves_finish_admission() -> None:
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            minimum_audio_ms=20,
            queue_capacity=1,
            terminal_queue_capacity=1,
            completion_queue_capacity=1,
        ),
        on_completion=complete,
    )
    candidate = _candidate(901)

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    token = runtime._candidate_tokens[candidate]
    assert runtime.snapshot()["queued_item_count"] == 1

    assert runtime.finish_candidate(candidate)
    assert token.finish_state == "queued"
    queued = runtime.snapshot()
    assert queued["pending_terminal_count"] == 1
    assert queued["terminal_queued_count"] == 1
    assert queued["terminal_overflow_count"] == 0

    await runtime.wait_idle()

    assert token.finish_state == "processed"
    assert token.completion_state == "attempted"
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="insufficient",
            last_checkpoint_ms=None,
        )
    ]
    completed = runtime.snapshot()
    assert completed["pending_terminal_count"] == 0
    assert completed["pending_completion_count"] == 0
    assert completed["completion_count"] == 1
    assert completed["completion_dispatched_count"] == 1
    assert completed["completion_attempted_count"] == 1
    await runtime.close()


async def test_terminal_capacity_overflow_never_claims_finish_processed() -> None:
    degraded_calls = 0

    def degraded() -> None:
        nonlocal degraded_calls
        degraded_calls += 1

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            queue_capacity=1,
            terminal_queue_capacity=1,
            completion_queue_capacity=1,
        ),
        on_backend_degraded=degraded,
    )
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime._worker_task = fake_worker
    queued_candidate = _candidate(902)
    overflowed_candidate = _candidate(903)

    try:
        assert runtime.finish_candidate(queued_candidate)
        queued_token = runtime._candidate_tokens[queued_candidate]
        assert queued_token.finish_state == "queued"

        assert runtime.finish_candidate(overflowed_candidate) is False
        overflowed = runtime._finalized[overflowed_candidate]
        assert overflowed.token is not None
        assert overflowed.token.finish_state == "abandoned"
        assert overflowed.token.finish_state != "processed"

        metrics = runtime.snapshot()
        assert metrics["pending_terminal_count"] == 1
        assert metrics["terminal_queued_count"] == 1
        assert metrics["terminal_overflow_count"] == 1
        assert metrics["terminal_abandoned_count"] == 1
        assert metrics["delivery_degraded_count"] == 1
        assert metrics["delivery_degraded_cause_count"] == 1
        assert metrics["completion_count"] == 0
        assert degraded_calls == 1
    finally:
        hold_worker.set()
        await fake_worker
        await runtime.close()


async def test_worker_start_failure_abandons_unaccepted_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    degraded_calls = 0

    def degraded() -> None:
        nonlocal degraded_calls
        degraded_calls += 1

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            terminal_queue_capacity=1,
            completion_queue_capacity=1,
        ),
        on_backend_degraded=degraded,
    )
    candidate = _candidate(909)
    monkeypatch.setattr(runtime, "_ensure_worker", lambda: False)

    assert runtime.finish_candidate(candidate) is False

    finalized = runtime._finalized[candidate]
    assert finalized.token is not None
    assert finalized.token.finish_state == "abandoned"
    metrics = runtime.snapshot()
    assert metrics["terminal_queued_count"] == 0
    assert metrics["pending_terminal_count"] == 0
    assert metrics["terminal_overflow_count"] == 0
    assert metrics["terminal_abandoned_count"] == 1
    assert metrics["worker_start_failure_count"] == 1
    assert metrics["delivery_degraded_count"] == 1
    assert metrics["delivery_degraded_cause_count"] == 1
    assert degraded_calls == 1
    await runtime.close()


async def test_finish_processing_exception_still_delivers_failed_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    async def fail_process_finish(_marker: _CandidateFinished) -> None:
        raise RuntimeError("finish processing failed")

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            terminal_queue_capacity=1,
            completion_queue_capacity=1,
        ),
        on_completion=complete,
    )
    monkeypatch.setattr(runtime, "_process_finish", fail_process_finish)
    candidate = _candidate(910)

    assert runtime.finish_candidate(candidate)
    token = runtime._candidate_tokens[candidate]
    await runtime.wait_idle()

    assert token.finish_state == "processed"
    assert token.completion_state == "attempted"
    assert token.terminal_reason == "failed"
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="failed",
            last_checkpoint_ms=None,
        )
    ]
    metrics = runtime.snapshot()
    assert metrics["inference_failure_count"] == 1
    assert metrics["completion_count"] == 1
    assert metrics["completion_dispatched_count"] == 1
    assert metrics["completion_attempted_count"] == 1
    assert metrics["pending_terminal_count"] == 0
    assert metrics["pending_completion_count"] == 0
    await runtime.close()


async def test_finalized_candidate_keeps_completion_when_other_pcm_fills_queue() -> (
    None
):
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.8),
        config=_config(minimum_audio_ms=10, queue_capacity=1),
        on_completion=complete,
    )
    finalized_candidate = _candidate(91)
    queued_candidate = _candidate(92)
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=finalized_candidate,
    )
    await runtime.wait_idle()

    assert runtime.submit(
        _pcm(1),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=queued_candidate,
    )
    assert runtime.finish_candidate(finalized_candidate)
    await runtime.wait_idle()

    assert completions == [
        SpeakerShadowCompletion(
            candidate=finalized_candidate,
            terminal_reason="scored",
            last_checkpoint_ms=10,
        )
    ]
    metrics = runtime.snapshot()
    assert metrics["completion_count"] == 1
    assert metrics["dropped_frame_count"] == 0
    assert metrics["dropped_candidate_count"] == 0
    await runtime.close()


async def test_completion_callbacks_never_overlap_after_ignored_cancellation() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    active_callbacks = 0
    maximum_active_callbacks = 0
    order: list[tuple[str, int]] = []
    first_candidate = _candidate(93)
    second_candidate = _candidate(94)

    async def complete(completion: SpeakerShadowCompletion) -> None:
        nonlocal active_callbacks, maximum_active_callbacks
        active_callbacks += 1
        maximum_active_callbacks = max(maximum_active_callbacks, active_callbacks)
        order.append(("start", completion.candidate.shadow_generation))
        try:
            if completion.candidate == first_candidate:
                first_started.set()
                while not release_first.is_set():
                    try:
                        await release_first.wait()
                    except asyncio.CancelledError:
                        continue
            else:
                second_started.set()
        finally:
            order.append(("end", completion.candidate.shadow_generation))
            active_callbacks -= 1

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            minimum_audio_ms=20,
            queue_capacity=2,
            callback_timeout_seconds=0.05,
        ),
        on_completion=complete,
    )
    assert runtime.finish_candidate(first_candidate)
    await asyncio.wait_for(first_started.wait(), 1.0)
    await runtime.wait_idle()
    assert runtime._completion_callback_task is not None

    assert runtime.finish_candidate(second_candidate)
    await asyncio.sleep(0)
    assert second_started.is_set() is False
    assert maximum_active_callbacks == 1
    release_first.set()
    await runtime.wait_idle()

    assert second_started.is_set()
    assert maximum_active_callbacks == 1
    assert order == [
        ("start", first_candidate.shadow_generation),
        ("end", first_candidate.shadow_generation),
        ("start", second_candidate.shadow_generation),
        ("end", second_candidate.shadow_generation),
    ]
    await runtime.close()


async def test_blocked_completion_keeps_next_completion_queued_until_fifo_turn() -> (
    None
):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    order: list[tuple[str, int]] = []
    first_candidate = _candidate(904)
    second_candidate = _candidate(905)

    async def complete(completion: SpeakerShadowCompletion) -> None:
        generation = completion.candidate.shadow_generation
        order.append(("start", generation))
        try:
            if completion.candidate == first_candidate:
                first_started.set()
                while not release_first.is_set():
                    try:
                        await release_first.wait()
                    except asyncio.CancelledError:
                        continue
            else:
                second_started.set()
        finally:
            order.append(("end", generation))

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            queue_capacity=2,
            terminal_queue_capacity=2,
            completion_queue_capacity=2,
            callback_timeout_seconds=0.01,
        ),
        on_completion=complete,
    )

    assert runtime.finish_candidate(first_candidate)
    first_token = runtime._candidate_tokens[first_candidate]
    await asyncio.wait_for(first_started.wait(), 1.0)
    assert runtime.finish_candidate(second_candidate)
    second_token = runtime._candidate_tokens[second_candidate]

    await asyncio.wait_for(runtime.wait_idle(), 1.0)

    stalled = runtime.snapshot()
    assert first_token.completion_state == "dispatched"
    assert second_token.completion_state == "queued"
    assert second_started.is_set() is False
    assert stalled["completion_count"] == 2
    assert stalled["completion_dispatched_count"] == 1
    assert stalled["completion_attempted_count"] == 0
    assert stalled["completion_stall_count"] == 1
    assert stalled["pending_completion_count"] == 1
    assert stalled["delivery_degraded_cause_count"] == 1

    release_first.set()
    await asyncio.wait_for(second_started.wait(), 1.0)
    await asyncio.wait_for(runtime.wait_idle(), 1.0)

    assert first_token.completion_state == "attempted"
    assert second_token.completion_state == "attempted"
    assert order == [
        ("start", first_candidate.shadow_generation),
        ("end", first_candidate.shadow_generation),
        ("start", second_candidate.shadow_generation),
        ("end", second_candidate.shadow_generation),
    ]
    delivered = runtime.snapshot()
    assert delivered["completion_dispatched_count"] == 2
    assert delivered["completion_attempted_count"] == 2
    assert delivered["pending_completion_count"] == 0
    await runtime.close()


async def test_completion_outbox_overflow_is_explicit_and_never_dispatched() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    delivered_candidates: list[SpeakerShadowCandidateKey] = []
    first_candidate = _candidate(906)
    second_candidate = _candidate(907)
    overflowed_candidate = _candidate(908)

    async def complete(completion: SpeakerShadowCompletion) -> None:
        delivered_candidates.append(completion.candidate)
        if completion.candidate == first_candidate:
            first_started.set()
            while not release_first.is_set():
                try:
                    await release_first.wait()
                except asyncio.CancelledError:
                    continue
        elif completion.candidate == second_candidate:
            second_started.set()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            queue_capacity=3,
            terminal_queue_capacity=3,
            completion_queue_capacity=1,
            callback_timeout_seconds=0.01,
        ),
        on_completion=complete,
    )

    assert runtime.finish_candidate(first_candidate)
    await asyncio.wait_for(first_started.wait(), 1.0)
    assert runtime.finish_candidate(second_candidate)
    second_token = runtime._candidate_tokens[second_candidate]
    deadline = asyncio.get_running_loop().time() + 1.0
    while second_token.completion_state != "queued":
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)

    assert runtime.finish_candidate(overflowed_candidate)
    overflowed_token = runtime._candidate_tokens[overflowed_candidate]
    await asyncio.wait_for(runtime.wait_idle(), 1.0)

    overflowed = runtime.snapshot()
    assert overflowed_token.finish_state == "processed"
    assert overflowed_token.completion_state == "abandoned"
    assert second_token.completion_state == "queued"
    assert second_started.is_set() is False
    assert overflowed["completion_count"] == 2
    assert overflowed["completion_dispatched_count"] == 1
    assert overflowed["completion_overflow_count"] == 1
    assert overflowed["completion_abandoned_count"] == 1
    assert overflowed["pending_completion_count"] == 1
    assert overflowed["delivery_degraded_count"] == 1
    assert overflowed["delivery_degraded_cause_count"] >= 1

    release_first.set()
    await asyncio.wait_for(second_started.wait(), 1.0)
    await asyncio.wait_for(runtime.wait_idle(), 1.0)

    assert delivered_candidates == [first_candidate, second_candidate]
    assert overflowed_candidate not in delivered_candidates
    assert runtime.snapshot()["completion_dispatched_count"] == 2
    await runtime.close()


async def test_reset_detaches_uncooperative_completion_without_false_busy() -> None:
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()

    async def complete(_completion: SpeakerShadowCompletion) -> None:
        callback_started.set()
        while not release_callback.is_set():
            try:
                await release_callback.wait()
            except asyncio.CancelledError:
                continue

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(callback_timeout_seconds=0.01),
        on_completion=complete,
    )
    candidate = _candidate(912)
    assert runtime.finish_candidate(candidate)
    token = runtime._candidate_tokens[candidate]
    await asyncio.wait_for(callback_started.wait(), 1.0)

    await asyncio.wait_for(runtime.reset(), 1.0)
    await asyncio.wait_for(runtime.wait_idle(), 0.2)

    detached = runtime.snapshot()
    assert token.completion_state == "dispatched"
    assert detached["detached_callback_task_count"] == 1
    assert detached["callback_task_count"] == 1

    release_callback.set()
    await _wait_until(lambda: token.completion_state == "attempted")
    completed = runtime.snapshot()
    assert completed["completion_attempted_count"] == 1
    assert completed["detached_callback_task_count"] == 0
    assert completed["callback_task_count"] == 0
    await runtime.close()


async def test_reset_stalled_completion_recovers_when_detached_task_finishes() -> None:
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    degraded_count = 0
    recovered_count = 0

    async def complete(_completion: SpeakerShadowCompletion) -> None:
        callback_started.set()
        while not release_callback.is_set():
            try:
                await release_callback.wait()
            except asyncio.CancelledError:
                continue

    def on_degraded() -> None:
        nonlocal degraded_count
        degraded_count += 1

    def on_recovered() -> None:
        nonlocal recovered_count
        recovered_count += 1

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(callback_timeout_seconds=0.01),
        on_completion=complete,
        on_backend_degraded=on_degraded,
        on_backend_recovered=on_recovered,
    )
    candidate = _candidate(913)
    assert runtime.finish_candidate(candidate)
    token = runtime._candidate_tokens[candidate]
    await asyncio.wait_for(callback_started.wait(), 1.0)
    await asyncio.wait_for(runtime.wait_idle(), 1.0)
    assert runtime.snapshot()["delivery_degraded_cause_count"] == 1

    await asyncio.wait_for(runtime.reset(), 1.0)
    release_callback.set()
    await _wait_until(lambda: token.completion_state == "attempted")

    recovered = runtime.snapshot()
    assert degraded_count == 1
    assert recovered_count == 1
    assert recovered["delivery_degraded_cause_count"] == 0
    assert recovered["completion_attempted_count"] == 1
    await runtime.close()


async def test_concurrent_reset_shares_cleanup_when_one_waiter_is_cancelled() -> None:
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()
    callback_release = asyncio.Event()

    async def observe(_observation: SpeakerShadowObservation) -> None:
        callback_started.set()
        while not callback_release.is_set():
            try:
                await callback_release.wait()
            except asyncio.CancelledError:
                callback_cancelled.set()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(callback_timeout_seconds=0.05),
        on_observation=observe,
    )
    candidate = _candidate(918)
    assert runtime.submit(
        _pcm(1_500),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await asyncio.wait_for(callback_started.wait(), 1.0)
    generation = runtime.generation

    first_reset = asyncio.create_task(runtime.reset())
    await asyncio.wait_for(callback_cancelled.wait(), 1.0)
    second_reset = asyncio.create_task(runtime.reset())
    await asyncio.sleep(0)
    first_reset.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_reset

    callback_release.set()
    await asyncio.wait_for(second_reset, 1.0)
    await asyncio.wait_for(runtime.wait_idle(), 1.0)

    assert runtime.generation == generation + 1
    snapshot = runtime.snapshot()
    assert snapshot["retained_pcm_bytes"] == 0
    assert snapshot["queued_item_count"] == 0
    assert snapshot["pending_completion_count"] == 0
    assert runtime.requires_provisional_decision(candidate) is False
    await runtime.close()


async def test_reset_blocks_admission_and_provisional_capability_until_done() -> None:
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()
    callback_release = asyncio.Event()

    async def observe(_observation: SpeakerShadowObservation) -> None:
        callback_started.set()
        while not callback_release.is_set():
            try:
                await callback_release.wait()
            except asyncio.CancelledError:
                callback_cancelled.set()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.2),
        config=_provider_gate_config(callback_timeout_seconds=0.05),
        on_observation=observe,
    )
    reset_task: asyncio.Task[None] | None = None
    try:
        stale_candidate = _candidate(919)
        new_candidate = _candidate(920)
        deferred_candidate = _candidate(921)
        assert runtime.submit(
            _pcm(1_500),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=stale_candidate,
        )
        await asyncio.wait_for(callback_started.wait(), 1.0)
        assert runtime.requires_provisional_decision(stale_candidate) is True
        assert runtime.defer_candidate(deferred_candidate)

        reset_task = asyncio.create_task(runtime.reset())
        await asyncio.wait_for(callback_cancelled.wait(), 1.0)

        assert runtime.requires_provisional_decision(stale_candidate) is False
        assert runtime.supports_deferred_candidate(new_candidate) is True
        assert runtime.defer_candidate(new_candidate) is False
        assert runtime.activate_candidate(deferred_candidate) is False
        assert (
            runtime.submit(
                _pcm(1),
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                candidate=new_candidate,
            )
            is False
        )
        assert runtime.finish_candidate(new_candidate) is False

        callback_release.set()
        await asyncio.wait_for(reset_task, 1.0)
        reset_task = None
        assert runtime.supports_deferred_candidate(new_candidate) is True
        assert runtime.submit(
            _pcm(1),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=new_candidate,
        )
        await runtime.wait_idle()
    finally:
        callback_release.set()
        if reset_task is not None:
            await asyncio.gather(reset_task, return_exceptions=True)
        await runtime.close()


async def test_reset_abandons_completion_after_finish_was_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_processed = asyncio.Event()
    finish_release = asyncio.Event()

    async def block_after_process(marker: _CandidateFinished) -> None:
        runtime._mark_finish_processed(marker.token)
        finish_processed.set()
        await finish_release.wait()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(terminal_queue_capacity=1),
    )
    monkeypatch.setattr(runtime, "_process_finish", block_after_process)
    candidate = _candidate(921)
    try:
        assert runtime.finish_candidate(candidate)
        token = runtime._candidate_tokens[candidate]
        await asyncio.wait_for(finish_processed.wait(), 1.0)
        assert token.finish_state == "processed"
        assert token.completion_state == "none"

        await asyncio.wait_for(runtime.reset(), 1.0)

        assert token.finish_state == "processed"
        assert token.completion_state == "abandoned"
        assert runtime.snapshot()["completion_abandoned_count"] == 1
        finish_release.set()
        await asyncio.wait_for(runtime.wait_idle(), 1.0)
    finally:
        finish_release.set()
        await runtime.close()


async def test_reset_abandons_completion_for_queued_terminal() -> None:
    hold_worker = asyncio.Event()
    fake_worker = asyncio.create_task(hold_worker.wait())
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(terminal_queue_capacity=1),
    )
    runtime._worker_task = fake_worker
    candidate = _candidate(922)

    try:
        assert runtime.finish_candidate(candidate)
        token = runtime._candidate_tokens[candidate]
        assert token.finish_state == "queued"
        assert token.completion_state == "none"

        await asyncio.wait_for(runtime.reset(), 1.0)

        assert token.finish_state == "abandoned"
        assert token.completion_state == "abandoned"
        snapshot = runtime.snapshot()
        assert snapshot["terminal_abandoned_count"] == 1
        assert snapshot["completion_abandoned_count"] == 1
        assert snapshot["pending_terminal_count"] == 0
    finally:
        hold_worker.set()
        await fake_worker
        await runtime.close()


async def test_finalized_tombstone_before_marker_still_delivers_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_processed = asyncio.Event()
    frame_release = asyncio.Event()
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(score_value=0.8),
        config=_config(minimum_audio_ms=20, queue_capacity=2),
        on_completion=complete,
    )
    # This test isolates a warm scoring tombstone before its finish marker;
    # cold loading is now deliberately independent of the frame worker.
    assert await runtime._ensure_backend() is not None
    process_frame = runtime._process_frame

    async def pause_after_frame(frame: _AudioFrame) -> None:
        await process_frame(frame)
        frame_processed.set()
        await frame_release.wait()

    monkeypatch.setattr(runtime, "_process_frame", pause_after_frame)
    candidate = _candidate(923)
    assert runtime.submit(
        _pcm(20),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    token = runtime._candidate_tokens[candidate]
    assert runtime.finish_candidate(candidate)
    await asyncio.wait_for(frame_processed.wait(), 2.0)

    assert candidate in runtime._finalized
    assert token.finish_state == "queued"
    assert token.completion_state == "none"
    frame_release.set()
    await asyncio.wait_for(runtime.wait_idle(), 2.0)

    assert token.finish_state == "processed"
    assert token.completion_state == "attempted"
    assert completions == [
        SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="scored",
            last_checkpoint_ms=20,
        )
    ]
    await runtime.close()


async def test_new_generation_completion_waits_for_detached_predecessor() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    first_candidate = _candidate(915)
    second_candidate = _candidate(916)

    async def complete(completion: SpeakerShadowCompletion) -> None:
        if completion.candidate == first_candidate:
            first_started.set()
            while not release_first.is_set():
                try:
                    await release_first.wait()
                except asyncio.CancelledError:
                    continue
            return
        second_started.set()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(callback_timeout_seconds=0.01),
        on_completion=complete,
    )
    assert runtime.finish_candidate(first_candidate)
    await asyncio.wait_for(first_started.wait(), 1.0)
    await asyncio.wait_for(runtime.reset(), 1.0)

    assert runtime.finish_candidate(second_candidate)
    second_token = runtime._candidate_tokens[second_candidate]
    await asyncio.wait_for(runtime.wait_idle(), 0.2)

    assert second_token.completion_state == "queued"
    assert second_started.is_set() is False
    assert runtime.snapshot()["detached_callback_task_count"] == 1

    release_first.set()
    await asyncio.wait_for(second_started.wait(), 1.0)
    await asyncio.wait_for(runtime.wait_idle(), 1.0)
    assert second_token.completion_state == "attempted"
    await runtime.close()


async def test_unexpected_worker_cancel_abandons_consumed_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_started = asyncio.Event()

    async def block_finish(_marker: _CandidateFinished) -> None:
        finish_started.set()
        await asyncio.Event().wait()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(terminal_queue_capacity=1),
    )
    monkeypatch.setattr(runtime, "_process_finish", block_finish)
    candidate = _candidate(914)
    assert runtime.finish_candidate(candidate)
    token = runtime._candidate_tokens[candidate]
    await asyncio.wait_for(finish_started.wait(), 1.0)

    worker = runtime._worker_task
    assert worker is not None
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert token.finish_state == "abandoned"
    assert token.completion_state == "abandoned"
    assert runtime.finish_candidate(candidate) is False
    metrics = runtime.snapshot()
    assert metrics["pending_terminal_count"] == 0
    assert metrics["worker_start_failure_count"] == 1
    assert metrics["delivery_degraded_cause_count"] == 1
    await runtime.close()


async def test_unexpected_worker_cancel_abandons_processed_finish_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_processed = asyncio.Event()

    async def block_after_process(marker: _CandidateFinished) -> None:
        runtime._mark_finish_processed(marker.token)
        finish_processed.set()
        await asyncio.Event().wait()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(terminal_queue_capacity=1),
    )
    monkeypatch.setattr(runtime, "_process_finish", block_after_process)
    candidate = _candidate(917)
    assert runtime.finish_candidate(candidate)
    token = runtime._candidate_tokens[candidate]
    await asyncio.wait_for(finish_processed.wait(), 1.0)

    worker = runtime._worker_task
    assert worker is not None
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert token.finish_state == "processed"
    assert token.completion_state == "abandoned"
    assert runtime.snapshot()["completion_abandoned_count"] == 1
    await runtime.close()


async def test_unexpected_worker_cancel_drains_following_terminals_and_idle_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_started = asyncio.Event()

    async def block_first(_marker: _CandidateFinished) -> None:
        first_started.set()
        await asyncio.Event().wait()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(queue_capacity=2, terminal_queue_capacity=2),
    )
    process_finish = runtime._process_finish
    monkeypatch.setattr(runtime, "_process_finish", block_first)
    first_candidate = _candidate(924)
    second_candidate = _candidate(925)
    queued_audio_candidate = _candidate(926)
    restarted_candidate = _candidate(927)

    try:
        assert runtime.finish_candidate(first_candidate)
        first_token = runtime._candidate_tokens[first_candidate]
        await asyncio.wait_for(first_started.wait(), 1.0)
        assert runtime.finish_candidate(second_candidate)
        second_token = runtime._candidate_tokens[second_candidate]
        assert second_token.finish_state == "queued"
        assert runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=queued_audio_candidate,
        )
        queued_audio_token = runtime._candidate_tokens[queued_audio_candidate]
        assert queued_audio_token.accepted_sample_count > 0

        worker = runtime._worker_task
        assert worker is not None
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        await asyncio.sleep(0)
        await asyncio.wait_for(runtime.wait_idle(), 1.0)

        assert first_token.finish_state == "abandoned"
        assert first_token.completion_state == "abandoned"
        assert second_token.finish_state == "abandoned"
        assert second_token.completion_state == "abandoned"
        assert queued_audio_token.terminal_reason == "dropped"
        assert queued_audio_candidate in runtime._finalized
        assert (
            runtime.submit(
                _pcm(10),
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                candidate=queued_audio_candidate,
            )
            is False
        )
        snapshot = runtime.snapshot()
        assert snapshot["queued_item_count"] == 0
        assert snapshot["pending_terminal_count"] == 0
        assert snapshot["retained_pcm_bytes"] == 0
        assert snapshot["terminal_abandoned_count"] == 2
        assert snapshot["completion_abandoned_count"] == 2

        monkeypatch.setattr(runtime, "_process_finish", process_finish)
        assert runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=restarted_candidate,
        )
        assert runtime.finish_candidate(restarted_candidate)
        restarted_token = runtime._candidate_tokens[restarted_candidate]
        await asyncio.wait_for(runtime.wait_idle(), 1.0)
        assert restarted_token.finish_state == "processed"
        assert restarted_token.completion_state == "attempted"
    finally:
        await runtime.close()


async def test_buffers_and_tombstones_remain_bounded() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            minimum_audio_ms=20,
            queue_capacity=4,
            buffered_candidate_capacity=2,
            finalized_candidate_capacity=4,
        ),
    )

    for generation in range(10, 20):
        candidate = _candidate(generation)
        assert runtime.submit(
            _pcm(1),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["buffered_candidate_count"] == 2
    assert metrics["dropped_candidate_count"] == 8
    assert metrics["finalized_tombstone_count"] <= 4
    assert metrics["buffered_audio_bytes"] <= len(_pcm(1)) * 2
    await runtime.close()


async def test_evicted_tombstone_keeps_late_finish_idempotent() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            queue_capacity=1,
            finalized_candidate_capacity=1,
        ),
    )
    evicted_candidate = _candidate(20)

    assert runtime.finish_candidate(evicted_candidate)
    await runtime.wait_idle()
    assert runtime.finish_candidate(_candidate(21))
    await runtime.wait_idle()
    before_duplicate = runtime.snapshot()

    assert runtime.finish_candidate(evicted_candidate)
    assert (
        runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=evicted_candidate,
        )
        is False
    )
    await runtime.wait_idle()

    after_duplicate = runtime.snapshot()
    assert (
        after_duplicate["finished_candidate_count"]
        == before_duplicate["finished_candidate_count"]
    )
    assert (
        after_duplicate["insufficient_candidate_count"]
        == before_duplicate["insufficient_candidate_count"]
    )
    assert after_duplicate["finalized_tombstone_count"] == 1
    await runtime.close()


async def test_eviction_watermark_preserves_older_live_candidate() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            queue_capacity=1,
            finalized_candidate_capacity=1,
        ),
    )
    live_candidate = _candidate(20)

    assert runtime.submit(
        _pcm(1),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=live_candidate,
    )
    await runtime.wait_idle()
    for generation in (21, 22):
        assert runtime.finish_candidate(_candidate(generation))
        await runtime.wait_idle()

    assert runtime.finish_candidate(live_candidate)
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["buffered_candidate_count"] == 0
    assert metrics["finished_candidate_count"] == 3
    assert metrics["insufficient_candidate_count"] == 3
    await runtime.close()


async def test_queued_work_ignores_evicted_candidate_watermark() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(queue_capacity=1, finalized_candidate_capacity=1),
    )
    candidate = _candidate(20)
    marker = _CandidateFinished(
        runtime._generation,
        candidate,
        _CandidateToken(candidate, 0),
    )
    pcm16 = bytearray(_pcm(10))
    frame = _AudioFrame(
        runtime._generation,
        candidate,
        _CandidateToken(candidate, SPEAKER_SHADOW_SAMPLE_RATE_HZ),
        pcm16,
        SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        len(pcm16) // 2,
    )
    runtime._record_evicted_candidate(candidate)
    before_work = runtime.snapshot()

    await runtime._process_finish(marker)
    await runtime._process_frame(frame)

    assert runtime.snapshot() == before_work
    await runtime.close()


def test_threshold_metric_keys_preserve_distinct_float_values() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=None,
        config=_config(similarity_thresholds=(0.4, 0.404)),
    )

    metrics = runtime.snapshot()

    assert metrics["would_block_at_0_4_count"] == 0
    assert metrics["would_block_at_0_404_count"] == 0
    asyncio.run(runtime.close())


@pytest.mark.parametrize("failure_stage", ["load", "score", "callback"])
async def test_failures_stay_inside_shadow_and_have_one_terminal_state(
    failure_stage: str,
) -> None:
    async def callback(_observation: SpeakerShadowObservation) -> None:
        if failure_stage == "callback":
            raise RuntimeError("callback failed")

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            load_error=failure_stage == "load",
            score_error=failure_stage == "score",
        ),
        config=_config(minimum_audio_ms=10),
        on_observation=callback,
    )
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(20, "smart_turn_turn"),
    )
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    terminal_count = sum(
        metrics[f"{reason}_candidate_count"]
        for reason in ("scored", "insufficient", "dropped", "failed")
    )
    assert terminal_count == 1
    if failure_stage == "callback":
        assert metrics["scored_candidate_count"] == 1
        assert metrics["callback_failure_count"] == 1
    else:
        assert metrics["failed_candidate_count"] == 1
    await runtime.close()
    assert runtime.snapshot()["backend_process_count"] == 0


async def test_reset_discards_in_flight_result_by_generation() -> None:
    started = _spawn_event()
    release = _spawn_event()
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            block_stage="score",
            stage_started=started,
            stage_release=release,
        ),
        config=_config(minimum_audio_ms=10),
        on_observation=observe,
    )
    before = _candidate(21)
    after = _candidate(22)

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=before,
    )
    await _wait_until(started.is_set)
    old_generation = runtime.generation
    await runtime.reset()
    assert runtime.generation == old_generation + 1
    release.set()
    await runtime.wait_idle()

    assert observations == []
    assert runtime.snapshot()["stale_result_count"] == 1
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=after,
    )
    await runtime.wait_idle()
    assert [item.candidate for item in observations] == [after]
    await runtime.close()


async def test_reset_cancels_stale_observation_delivery() -> None:
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        callback_started.set()
        await callback_release.wait()
        observations.append(observation)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(minimum_audio_ms=10),
        on_observation=observe,
    )
    candidate = _candidate(23)
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    await asyncio.wait_for(callback_started.wait(), 2.0)

    await runtime.reset()
    callback_release.set()
    await runtime.wait_idle()

    assert observations == []
    assert runtime.snapshot()["stale_result_count"] == 1
    assert runtime.snapshot()["callback_task_count"] == 0
    await runtime.close()


async def test_reset_cancels_stale_completion_delivery() -> None:
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()
    completions: list[SpeakerShadowCompletion] = []

    async def complete(completion: SpeakerShadowCompletion) -> None:
        callback_started.set()
        await callback_release.wait()
        completions.append(completion)

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(minimum_audio_ms=20),
        on_completion=complete,
    )
    candidate = _candidate(123)
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await asyncio.wait_for(callback_started.wait(), 2.0)

    await runtime.reset()
    callback_release.set()
    await runtime.wait_idle()

    assert completions == []
    assert runtime.snapshot()["stale_result_count"] == 1
    assert runtime.snapshot()["callback_task_count"] == 0
    await runtime.close()


async def test_completion_callback_failure_is_counted_and_contained() -> None:
    async def complete(_completion: SpeakerShadowCompletion) -> None:
        raise RuntimeError("completion failed")

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(minimum_audio_ms=20),
        on_completion=complete,
    )
    candidate = _candidate(124)
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    assert runtime.finish_candidate(candidate)
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["completion_count"] == 1
    assert metrics["completion_callback_failure_count"] == 1
    assert metrics["callback_failure_count"] == 1
    await runtime.close()


def test_sync_observation_callback_is_rejected() -> None:
    with pytest.raises(TypeError, match="callback must be async"):
        SpeakerShadowRuntime(
            backend_factory=_BackendFactory(),
            config=_config(),
            on_observation=lambda _observation: None,  # type: ignore[arg-type]
        )


def test_sync_completion_callback_is_rejected() -> None:
    with pytest.raises(TypeError, match="completion callback must be async"):
        SpeakerShadowRuntime(
            backend_factory=_BackendFactory(),
            config=_config(),
            on_completion=lambda _completion: None,  # type: ignore[arg-type]
        )


async def test_idle_unload_releases_then_reloads_one_serial_host() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(minimum_audio_ms=10, idle_unload_seconds=0.05),
    )

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(24),
    )
    await runtime.wait_idle()
    assert runtime.snapshot()["backend_process_count"] == 1
    await _wait_until(lambda: runtime.snapshot()["unload_count"] == 1)
    assert runtime.snapshot()["backend_process_count"] == 0
    assert runtime.snapshot()["unload_count"] == 1

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(25),
    )
    await runtime.wait_idle()
    assert runtime.snapshot()["load_count"] == 2
    assert runtime.snapshot()["backend_process_count"] == 1
    await runtime.close()
    assert runtime.snapshot()["backend_process_count"] == 0


async def test_close_error_is_fail_open_and_next_candidate_can_reload() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(close_error=True),
        config=_config(minimum_audio_ms=10, idle_unload_seconds=0.05),
    )

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(26),
    )
    await runtime.wait_idle()
    await _wait_until(lambda: runtime.snapshot()["unload_failure_count"] == 1)
    assert runtime.snapshot()["backend_process_count"] == 0
    assert runtime.snapshot()["unload_failure_count"] == 1
    assert runtime.enabled is True

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(27),
    )
    await runtime.wait_idle()
    assert runtime.snapshot()["load_count"] == 2
    await runtime.close()
    assert runtime.snapshot()["backend_process_count"] == 0


async def test_parent_factory_profile_is_wiped_once_on_idempotent_close() -> None:
    profile = bytearray(b"private-profile")
    factory = _BackendFactory(parent_profile=profile)
    runtime = SpeakerShadowRuntime(
        backend_factory=factory,
        config=_config(minimum_audio_ms=10),
    )

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(28),
    )
    await runtime.wait_idle()
    assert profile == bytearray(b"private-profile")

    await runtime.close()
    await runtime.close()

    assert profile == bytearray(len(profile))
    assert factory.parent_close_calls == 1
    assert runtime.snapshot()["backend_process_count"] == 0


async def test_unpicklable_factory_fails_open_without_process_leak() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=lambda: _BackendFactory()(),
        config=_config(minimum_audio_ms=10),
    )

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(29),
    )
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    assert metrics["failed_candidate_count"] == 1
    assert metrics["load_failure_count"] == 1
    assert metrics["host_start_task_count"] == 0
    assert metrics["backend_process_count"] == 0
    await runtime.close()


@pytest.mark.parametrize("blocked_stage", ["load", "score"])
async def test_backend_operation_timeout_is_fail_open_and_reaps_host(
    blocked_stage: str,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(block_stage=blocked_stage),
        config=_config(
            minimum_audio_ms=10,
            backend_load_timeout_seconds=0.1,
            backend_score_timeout_seconds=0.1,
            process_terminate_timeout_seconds=0.2,
        ),
    )
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(40 + (blocked_stage == "score")),
    )

    await asyncio.wait_for(runtime.wait_idle(), 3.0)

    metrics = runtime.snapshot()
    assert metrics["failed_candidate_count"] == 1
    assert metrics["backend_timeout_count"] == 1
    assert metrics["backend_process_termination_count"] == 1
    assert metrics["backend_process_count"] == 0
    await runtime.close()


@pytest.mark.parametrize(
    "factory",
    [
        _BackendFactory(load_ok=False),
        _BackendFactory(score_value=float("nan")),
    ],
    ids=["unavailable-load", "invalid-score"],
)
async def test_backend_invalid_results_fail_open(
    factory: _BackendFactory,
) -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=factory,
        config=_config(minimum_audio_ms=10),
    )
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(42),
    )

    await runtime.wait_idle()

    assert runtime.snapshot()["failed_candidate_count"] == 1
    await runtime.close()
    assert runtime.snapshot()["backend_process_count"] == 0


async def test_close_cancels_in_flight_observation_and_reaps_host() -> None:
    callback_started = asyncio.Event()

    async def observe(_observation: SpeakerShadowObservation) -> None:
        callback_started.set()
        await asyncio.Event().wait()

    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(minimum_audio_ms=10, shutdown_grace_seconds=0.05),
        on_observation=observe,
    )
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(43),
    )
    await asyncio.wait_for(callback_started.wait(), 2.0)

    await asyncio.wait_for(runtime.close(), 2.0)

    metrics = runtime.snapshot()
    assert metrics["worker_task_count"] == 0
    assert metrics["callback_task_count"] == 0
    assert metrics["backend_process_count"] == 0


async def test_close_retries_after_callback_consumes_first_cancellation() -> None:
    callback_started = asyncio.Event()
    first_cancellation_seen = asyncio.Event()
    force_release = asyncio.Event()
    profile = bytearray(b"private-profile")
    factory = _BackendFactory(parent_profile=profile)

    async def observe(_observation: SpeakerShadowObservation) -> None:
        callback_started.set()
        try:
            await force_release.wait()
        except asyncio.CancelledError:
            first_cancellation_seen.set()
        await force_release.wait()

    runtime = SpeakerShadowRuntime(
        backend_factory=factory,
        config=_config(
            minimum_audio_ms=10,
            callback_timeout_seconds=0.05,
            shutdown_grace_seconds=0.05,
        ),
        on_observation=observe,
    )
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(47),
    )
    await asyncio.wait_for(callback_started.wait(), 2.0)

    try:
        await asyncio.wait_for(runtime.close(), 2.0)

        metrics = runtime.snapshot()
        assert first_cancellation_seen.is_set()
        assert metrics["worker_task_count"] == 0
        assert metrics["cleanup_task_count"] == 0
        assert metrics["backend_process_count"] == 0
        assert metrics["callback_task_count"] == 0
        assert factory.parent_close_calls == 1
        assert profile == bytearray(len(profile))
    finally:
        force_release.set()
        callback_task = runtime._observation_task
        if callback_task is not None and not callback_task.done():
            callback_task.cancel()
            await asyncio.wait({callback_task}, timeout=2.0)
        await runtime.close()


async def test_finish_without_audio_and_late_pcm_has_one_terminal_state() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(),
    )
    candidate = _candidate(44)

    assert runtime.finish_candidate(candidate)
    assert (
        runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=candidate,
        )
        is False
    )
    await runtime.wait_idle()

    metrics = runtime.snapshot()
    terminal_count = sum(
        metrics[f"{reason}_candidate_count"]
        for reason in ("scored", "insufficient", "dropped", "failed")
    )
    assert terminal_count == 1
    assert metrics["dropped_candidate_count"] == 1
    assert runtime.finish_candidate(candidate)
    assert runtime.finish_candidate(candidate)
    assert runtime.snapshot()["finished_candidate_count"] == 1
    await runtime.close()


def test_submit_without_running_loop_fails_open_and_drains_pcm() -> None:
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(),
    )

    assert (
        runtime.submit(
            _pcm(10),
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            candidate=_candidate(45),
        )
        is False
    )

    metrics = runtime.snapshot()
    assert metrics["worker_start_failure_count"] == 1
    assert metrics["queued_item_count"] == 0
    assert metrics["queued_audio_bytes"] == 0
    asyncio.run(runtime.close())


async def test_close_tracks_cancelled_off_loop_host_start_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from main_logic.asr_client.speaker_shadow import runtime as runtime_module

    start_entered = threading.Event()
    start_release = threading.Event()
    original_start = runtime_module._BackendProcessHost.create_started

    def delayed_start(**kwargs: object) -> Any:
        start_entered.set()
        start_release.wait(timeout=2.0)
        return original_start(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        runtime_module._BackendProcessHost,
        "create_started",
        delayed_start,
    )
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(),
        config=_config(
            minimum_audio_ms=10,
            shutdown_grace_seconds=0.05,
            process_terminate_timeout_seconds=0.05,
        ),
    )
    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=_candidate(30),
    )
    await _wait_until(start_entered.is_set)

    loop_progressed = False

    async def mark_loop_progress() -> None:
        nonlocal loop_progressed
        await asyncio.sleep(0)
        loop_progressed = True

    await mark_loop_progress()
    close_task = asyncio.create_task(runtime.close())
    # Exceed both cleanup cancellation budgets. Repeated cancellation must not
    # detach the off-loop start task and orphan the host it eventually returns.
    await asyncio.sleep(0.35)
    assert loop_progressed is True
    assert close_task.done() is False

    start_release.set()
    await asyncio.wait_for(close_task, 2.0)
    await _wait_until(lambda: not _speaker_host_pids())
    metrics = runtime.snapshot()
    assert metrics["host_start_task_count"] == 0
    assert metrics["worker_task_count"] == 0
    assert metrics["backend_process_count"] == 0


@pytest.mark.parametrize("blocked_stage", ["load", "score", "close"])
async def test_permanently_blocked_backend_close_is_bounded_and_leaves_no_resources(
    blocked_stage: str,
) -> None:
    started = _spawn_event()
    runtime = SpeakerShadowRuntime(
        backend_factory=_BackendFactory(
            block_stage=blocked_stage,
            stage_started=started,
        ),
        config=_config(
            minimum_audio_ms=10,
            shutdown_grace_seconds=0.05,
            backend_load_timeout_seconds=3.0,
            backend_score_timeout_seconds=3.0,
            backend_close_timeout_seconds=0.1,
            process_terminate_timeout_seconds=0.2,
        ),
    )
    candidate = _candidate(30 + ("load", "score", "close").index(blocked_stage))

    assert runtime.submit(
        _pcm(10),
        sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        candidate=candidate,
    )
    if blocked_stage == "close":
        await runtime.wait_idle()
        close_task = asyncio.create_task(runtime.close())
        await _wait_until(started.is_set)
    else:
        await _wait_until(started.is_set)
        close_task = asyncio.create_task(runtime.close())

    await asyncio.wait_for(close_task, 1.0)
    await _wait_until(lambda: not _speaker_host_pids())

    metrics = runtime.snapshot()
    assert metrics["worker_task_count"] == 0
    assert metrics["callback_task_count"] == 0
    assert metrics["cleanup_task_count"] == 0
    assert metrics["backend_loaded_count"] == 0
    assert metrics["backend_process_count"] == 0
    assert metrics["retained_pcm_bytes"] == 0
    assert metrics["backend_process_termination_count"] >= 1
    if blocked_stage == "load":
        assert runtime._backend_load_task is None
    else:
        assert metrics["shutdown_timeout_count"] + metrics["backend_timeout_count"] >= 1
