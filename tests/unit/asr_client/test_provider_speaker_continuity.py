"""Provider continuity across the real Runtime, Detector and Shadow state machines.

Only VAD and speaker scoring are deterministic substitutes. These tests do not
claim microphone, remote ASR, or client presentation coverage.
"""

from __future__ import annotations

from functools import partial
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import json

import pytest

from tests.unit import test_asr_detector_runtime as detector_fixture
from tests.unit.test_core_independent_asr import _Runtime
from tests.unit.test_core_independent_asr import _selection
import main_logic.asr_client.runtime as runtime_module
from tests.unit.asr_client.test_candidate_rejection_runtime import (
    _drain_runtime_admission,
)
from tests.unit.voice_identity_service.test_asr_composition import _profile
from main_logic.voice_identity_service import asr_composition
from main_logic.asr_client._provider_events import (
    ProviderAudioRange,
    ProviderUtteranceKey,
)
from main_logic.asr_client.admission.contracts import AdmissionDisposition
from main_logic.asr_client.audio import AsrActivateCommand
from main_logic.asr_client._provider_events import (
    ProviderEndpointNotification,
    ProviderUtteranceStartedNotification,
)
from main_logic.asr_client.lifecycle import (
    VoiceInputLifecycleController,
    VoiceLifecycleEvent,
    VoiceRouteMode,
)
from main_logic.voice_turn.audio_input import ProcessedVoiceFrame
from main_logic.voice_turn.contracts import AsrSubmitStatus
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCaptureDisposition,
    SpeakerShadowObservation,
)


class _ConstantBackend:
    def __init__(self, score: float) -> None:
        self.value = score

    def load(self) -> bool:
        return True

    def score(self, pcm16: bytes, sample_rate_hz: int) -> float:
        assert pcm16
        assert sample_rate_hz == 16_000
        return self.value

    def close(self) -> None:
        pass


class _MarkerBackend(_ConstantBackend):
    def score(self, pcm16: bytes, sample_rate_hz: int) -> float:
        super().score(pcm16, sample_rate_hz)
        return 0.20 if pcm16[:2] == b"\x21\x00" else 0.95


class _GatedScoreHost:
    """External model-service substitute; production evidence handling stays real."""

    alive = True
    loaded = True
    timed_out = False
    was_terminated = False
    pcm_bytes_in_use = 0
    process_count = 0

    def __init__(self, score, mixed_scores):
        self.backend = (_MarkerBackend if mixed_scores else _ConstantBackend)(score)
        self.ready = asyncio.Event()
        self.calls = 0

    async def score(self, pcm16, *, timeout_seconds):
        await asyncio.wait_for(self.ready.wait(), timeout=timeout_seconds)
        self.calls += 1
        return self.backend.score(pcm16, 16_000)

    async def close(self, *, timeout_seconds):
        self.alive = False
        self.ready.set()
        return True

    async def terminate(self):
        self.alive = False
        self.ready.set()


async def _active_real_stack(
    *, score: float | None = None, mixed_scores: bool = False, gated_scores: bool = True
):
    core = _Runtime()
    core.core_api_type = "qwen"
    core.input_mode = "audio"
    core.is_active = True
    core.is_hot_swap_imminent = False
    core.is_flushing_hot_swap_cache = False
    core.user_language = "en"
    core.websocket = SimpleNamespace(send_json=AsyncMock())
    runtime = core._asr_runtime
    if score is None:
        shadow = detector_fixture.SpeakerShadowRuntime(
            backend_factory=partial(_ConstantBackend, 0.95),
            config=detector_fixture._provider_speaker_config(),
        )
    else:
        profile = _profile()
        composition = asr_composition.OwnerVoiceAsrCompositionFactory(
            runtime,
            profile,
            activation_generation="continuity-test",
            enforce=True,
        )
        backend = _MarkerBackend if mixed_scores else _ConstantBackend
        with patch.object(
            asr_composition,
            "CampPlusBackendFactory",
            return_value=partial(backend, score),
        ):
            shadow = composition()
        core.continuity_profile = profile
        core.continuity_composition = composition
        if gated_scores:
            core.continuity_score_host = _GatedScoreHost(score, mixed_scores)
            shadow._backend_host = core.continuity_score_host
        assert await shadow._ensure_backend() is not None
    detector = detector_fixture.DetectorRuntime(
        vad=detector_fixture._Vad(),
        gate=detector_fixture._Gate(),
        provider_policy=detector_fixture._provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    lifecycle = VoiceInputLifecycleController(
        provider_policy=detector_fixture._provider_endpoint_policy(),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    session = SimpleNamespace(
        is_ready=True,
        close=AsyncMock(),
        stream_audio=AsyncMock(),
        signal_user_activity_end=AsyncMock(),
    )
    runtime._asr_session = session
    runtime._asr_provider_exact_session = session
    runtime._asr_provider = "qwen"
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = detector
    core._asr_route_mode = "independent"
    runtime._asr_current_ingress_token = core._capture_ingress_token()
    runtime._speaker_verifier_activation_generation = "continuity-test"
    runtime._speaker_verifier_enforces_admission = True
    turn = runtime._capture_turn_token(lifecycle)
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    await runtime._prepare_independent_asr_turn(runtime._asr_session_epoch)
    assert runtime._asr_turn_prepared
    assert runtime._asr_audio_dispatcher.activate(turn, session, b"")
    return core, runtime, detector, shadow, lifecycle, session, turn


async def _submit_pcm(
    runtime, turn, *, sequence: int, duration_ms: int = 100, owner_pcm: bool = False
):
    pcm = detector_fixture._speaker_pcm(duration_ms)
    if owner_pcm:
        pcm = b"\x22\x00" * (len(pcm) // 2)
    return await runtime.submit(
        ProcessedVoiceFrame(
            pcm,
            16_000,
            0.9,
            True,
            ingress_sequence=sequence,
            captured_at=sequence / 10,
        ),
        ingress_token=turn.ingress,
    )


async def _close_stack(core):
    await core._asr_runtime.close()
    await core._voice_input_audio_pipeline.close()
    await core._voice_input_registry.wait_idle()
    if hasattr(core, "continuity_composition"):
        core.continuity_composition.close()
        core.continuity_profile.close()


@pytest.mark.parametrize(
    "score,mixed_scores,gated_scores",
    [(0.95, False, True), (0.20, True, True), (0.95, False, False)],
)
@pytest.mark.parametrize("final_first", [False, True])
async def test_real_exact_three_finals_keep_successor_and_core_delivery(
    score, mixed_scores, gated_scores, final_first
) -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack(
        score=score, mixed_scores=mixed_scores, gated_scores=gated_scores
    )
    try:
        sequence = 0
        for utterance in (1, 2, 3):
            if gated_scores and utterance == 1:
                core.continuity_score_host.ready.clear()
            for _ in range(16):
                sequence += 1
                submitted = await _submit_pcm(
                    runtime,
                    turn,
                    sequence=sequence,
                    owner_pcm=mixed_scores and utterance > 1,
                )
                assert submitted.status is AsrSubmitStatus.ACCEPTED, (
                    utterance,
                    sequence,
                    runtime._asr_provider_speaker_ledgers,
                )
            await shadow.wait_idle()
            key = ProviderUtteranceKey(0, 0, utterance)
            assert await runtime._handle_provider_utterance_started(
                ProviderUtteranceStartedNotification(
                    0,
                    0,
                    utterance,
                    audio_start_sample_16k=(utterance - 1) * 25_600,
                ),
                runtime._asr_session_epoch,
            )
            turn = runtime._asr_provider_started_turns[key]
            interval_ledger = runtime._asr_provider_speaker_key_ledgers[key]
            boundary = ProviderAudioRange((utterance - 1) * 25_600, utterance * 25_600)
            boundary_operation = runtime._handle_provider_endpoint_notification(
                ProviderEndpointNotification(
                    phase="boundary",
                    generation=0,
                    buffer_epoch=0,
                    utterance_id=utterance,
                    boundary_quality="exact",
                    audio_range=boundary,
                ),
                runtime._asr_session_epoch,
            )
            if gated_scores and utterance == 1:
                boundary_task = asyncio.create_task(boundary_operation)
                async with asyncio.timeout(1):
                    while (
                        key not in runtime._asr_provider_exact_intervals
                        and not boundary_task.done()
                    ):
                        await asyncio.sleep(0)
                assert key in runtime._asr_provider_exact_intervals
                # Commit/promotion is visible, but settlement is still waiting
                # for the external score; release it before joining boundary.
                core.continuity_score_host.ready.set()
                await boundary_task
            else:
                await boundary_operation
            transaction = runtime._asr_provider_exact_intervals.get(key)
            if utterance == 1:
                assert transaction is not None
            # Only now can LOW become an exact-child judgment. A parent DENY
            # before promotion has a different, intentionally broader scope.
            if gated_scores:
                core.continuity_score_host.ready.set()
            await shadow.wait_idle()
            if gated_scores:
                assert core.continuity_score_host.calls > 0, shadow.snapshot()
            if final_first:
                await runtime._handle_provider_final(
                    key,
                    f"utterance {utterance}",
                    runtime._asr_session_epoch,
                    "qwen",
                )
            await runtime._handle_provider_endpoint_notification(
                ProviderEndpointNotification(
                    phase="ordered",
                    generation=0,
                    buffer_epoch=0,
                    utterance_id=utterance,
                    boundary_quality="exact",
                    audio_range=boundary,
                ),
                runtime._asr_session_epoch,
            )
            if not final_first:
                await runtime._handle_provider_final(
                    key,
                    f"utterance {utterance}",
                    runtime._asr_session_epoch,
                    "qwen",
                )
            await _drain_runtime_admission(runtime)
            await runtime.wait_transcript_idle()
            await core._voice_input_registry.wait_idle()
            assert interval_ledger.state is (
                runtime_module._ProviderSpeakerLedgerState.RESOLVED
            )
            assert key not in runtime._asr_provider_speaker_key_ledgers
            if transaction is not None:
                assert transaction.successor_candidate in shadow._candidate_tokens, (
                    runtime._speaker_verifier_diagnostics()
                )
                if mixed_scores and utterance == 1:
                    assert transaction.resolved_disposition is AdmissionDisposition.DROP
            expected = (
                (utterance - 1) if mixed_scores else (utterance if score >= 0.40 else 0)
            )
            assert core.handle_input_transcript.await_count == expected
            if mixed_scores and utterance == 1:
                assert transaction.resolved_disposition is AdmissionDisposition.DROP
            assert core.session.create_response.await_count == expected
            await runtime._handle_provider_final(
                key,
                f"late duplicate {utterance}",
                runtime._asr_session_epoch,
                "qwen",
            )
            await _drain_runtime_admission(runtime)
            await runtime.wait_transcript_idle()
            assert core.handle_input_transcript.await_count == expected
            session.close.assert_not_awaited()
            if utterance < 3:
                lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
                lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
                turn = runtime._capture_turn_token(lifecycle)
                await runtime._prepare_independent_asr_turn(runtime._asr_session_epoch)
        assert (
            core.continuity_composition.diagnostics_snapshot()["observation_count"] >= 3
        )
    finally:
        await _close_stack(core)


class _ObservedLock(asyncio.Lock):
    def __init__(self):
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.contended.set()
        return await super().acquire()


async def test_cancelled_arming_waiter_cannot_abandon_shared_physical_lease() -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    lock = _ObservedLock()
    detector._lock = lock
    await lock.acquire()
    waiter = asyncio.create_task(
        runtime._arm_speaker_authority_for_provider_audio(turn)
    )
    try:
        await asyncio.wait_for(lock.contended.wait(), timeout=1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        owned = tuple(runtime._asr_provider_speaker_arming_tasks.values())
        assert len(owned) == 1 and not owned[0].done()
        lock.release()
        await asyncio.wait_for(asyncio.gather(*owned), timeout=1)
        evidence = runtime._asr_provider_speaker_evidence_lease
        assert evidence is not None
        assert await detector.ensure_provider_speaker_evidence_lease() == evidence
        assert evidence.candidate in shadow._candidate_tokens
        submitted = await _submit_pcm(runtime, turn, sequence=1)
        assert submitted.status is AsrSubmitStatus.ACCEPTED
    finally:
        if lock.locked():
            lock.release()
        if not waiter.done():
            waiter.cancel()
        await _close_stack(core)


async def test_stale_ingress_does_not_advance_current_audio_or_close_route() -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    try:
        first = await _submit_pcm(runtime, turn, sequence=1)
        assert first.status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()
        before = detector._provider_audio_sample_cursor_16k
        stale = replace(turn.ingress, session_epoch=turn.ingress.session_epoch + 1)
        result = await runtime.submit(
            ProcessedVoiceFrame(
                detector_fixture._speaker_pcm(100),
                16_000,
                0.9,
                True,
                ingress_sequence=2,
                captured_at=0.2,
            ),
            ingress_token=stale,
        )
        assert result.status is AsrSubmitStatus.STALE
        assert detector._provider_audio_sample_cursor_16k == before
        assert session.stream_audio.await_count == 1
        assert core._asr_route_mode == "independent"
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_real_exact_successor_accepts_audio_while_first_final_pending() -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    try:
        for sequence in range(1, 17):
            submitted = await _submit_pcm(runtime, turn, sequence=sequence)
            assert submitted.status is AsrSubmitStatus.ACCEPTED
        await shadow.wait_idle()
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        ledger = runtime._asr_provider_speaker_key_ledgers[
            ProviderUtteranceKey(0, 0, 1)
        ]
        assert ledger.poisoned_reason is None, (
            ledger,
            runtime._speaker_verifier_diagnostics(),
        )
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=ProviderAudioRange(0, 25_600),
            ),
            runtime._asr_session_epoch,
        )
        assert ProviderUtteranceKey(0, 0, 1) in runtime._asr_provider_exact_intervals, (
            ledger,
            runtime._speaker_verifier_diagnostics(),
        )
        transaction = runtime._asr_provider_exact_intervals[
            ProviderUtteranceKey(0, 0, 1)
        ]
        successor = transaction.successor_evidence_lease
        assert successor is not None
        await shadow.wait_idle()
        result = await _submit_pcm(runtime, turn, sequence=17)
        assert result.status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()
        assert (
            sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list)
            == 27_200
        )
        assert successor.candidate in shadow._candidate_tokens
    finally:
        await _close_stack(core)


@pytest.mark.parametrize("capture_failure", [False, True])
async def test_real_unavailable_ledger_still_accounts_for_submitted_pcm(
    capture_failure,
) -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    try:
        first = await _submit_pcm(runtime, turn, sequence=1)
        assert first.status is AsrSubmitStatus.ACCEPTED
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        if capture_failure:
            await shadow.close()
        else:
            runtime._poison_provider_speaker_ledger(
                ledger, "speaker_capture_unavailable"
            )
        for sequence in (2, 3, 4):
            result = await _submit_pcm(runtime, turn, sequence=sequence)
            assert result.status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()
        accepted = sum(
            len(call.args[0]) // 2 for call in session.stream_audio.await_args_list
        )
        assert accepted == 6400
        assert detector._provider_audio_sample_cursor_16k == accepted
        assert core._asr_route_mode == "independent"
    finally:
        await _close_stack(core)


@pytest.mark.parametrize("score", [0.20, 0.95])
@pytest.mark.parametrize("retire", [False, True])
async def test_real_exact_boundary_completion_preserves_successor_pcm(
    score: float,
    retire: bool,
) -> None:
    observations = []
    shadow = detector_fixture.SpeakerShadowRuntime(
        backend_factory=partial(_ConstantBackend, score),
        config=detector_fixture._provider_speaker_config(),
        on_evidence=observations.append,
    )
    detector = detector_fixture.DetectorRuntime(
        vad=detector_fixture._Vad(),
        gate=detector_fixture._Gate(),
        provider_policy=detector_fixture._provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    core = _Runtime()
    runtime = core._asr_runtime
    runtime._asr_detector = detector
    try:
        _, identity, _ = await detector_fixture._open_provider_candidate(
            detector, turn_id=1
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        first = await detector.observe_provider_audio_ordered(
            detector_fixture._speaker_pcm(1600),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        assert first is not None
        await detector_fixture._anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 25_600),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None
        await detector.observe_provider_audio_ordered(
            detector_fixture._speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=lease,
        )
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        successor = committed.successor_evidence_lease
        assert successor is not None and successor != lease
        await shadow.wait_idle()
        scored = [
            event
            for event in observations
            if isinstance(event, SpeakerShadowObservation)
        ]
        assert scored and all(event.similarity == score for event in scored)
        assert successor.candidate in shadow._candidate_tokens
        assert shadow._buffers[successor.candidate].sample_count == 1600
        if retire:
            completed = await detector.complete_provider_speaker_boundary(
                committed.snapshot,
                successor_evidence_lease=successor,
            )
            assert completed == "completed"
        assert successor.candidate in shadow._candidate_tokens
        assert await detector.ensure_provider_speaker_evidence_lease() == successor
        for sequence in (3, 4):
            future = await detector.observe_provider_audio_ordered(
                detector_fixture._speaker_pcm(100),
                sample_rate_hz=16_000,
                identity=identity,
                sequence_no=sequence,
                split_before_audio=True,
                speaker_evidence_lease=successor,
            )
            assert future is not None
            assert (
                future.capture.disposition is SpeakerShadowCaptureDisposition.ACCEPTED
            )
            assert future.capture.accepted_sample_count == 1600
        await shadow.wait_idle()
        assert shadow._buffers[successor.candidate].sample_count == 4800
    finally:
        await runtime.close()
        await core._voice_input_audio_pipeline.close()


@pytest.mark.parametrize("takeover", [False, True])
async def test_submit_cancelled_after_real_observation_retires_only_its_timeline(
    monkeypatch: pytest.MonkeyPatch,
    takeover: bool,
) -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    observed = asyncio.Event()
    release = asyncio.Event()
    original_observe = detector.observe_provider_audio_ordered

    async def hold_after_real_observation(*args, **kwargs):
        result = await original_observe(*args, **kwargs)
        observed.set()
        await release.wait()
        return result

    monkeypatch.setattr(
        detector, "observe_provider_audio_ordered", hold_after_real_observation
    )
    submitted = asyncio.create_task(_submit_pcm(runtime, turn, sequence=1))
    replacement = None
    try:
        await asyncio.wait_for(observed.wait(), timeout=1)
        assert detector._provider_audio_sample_cursor_16k == 1600
        session.stream_audio.assert_not_awaited()
        if takeover:
            await detector.reset_provider_audio_timeline()
            replacement = SimpleNamespace(
                is_ready=True, close=AsyncMock(), stream_audio=AsyncMock()
            )
            runtime._asr_session = replacement
            runtime._asr_session_epoch += 1
            runtime._asr_current_ingress_token = core._capture_ingress_token()
        submitted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submitted
        if takeover:
            assert runtime._asr_session is replacement
            assert runtime._asr_detector is detector
            assert detector._provider_audio_sample_cursor_16k == 0
            replacement.close.assert_not_awaited()
        else:
            assert runtime._asr_session is not session
            assert not runtime._ingress_token_matches(turn.ingress)
        session.stream_audio.assert_not_awaited()
    finally:
        release.set()
        if not submitted.done():
            submitted.cancel()
            await asyncio.gather(submitted, return_exceptions=True)
        await _close_stack(core)
        if replacement is not None:
            await session.close()


async def test_direct_activation_partial_observation_retires_physical_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    pcm = detector_fixture._speaker_pcm(100)
    runtime._asr_audio_dispatcher.abort(turn)
    await runtime._asr_audio_dispatcher.wait_idle()
    original_observe = detector.observe_provider_audio_ordered
    observations = 0
    observed_cursors = []

    async def fail_after_second_real_observation(*args, **kwargs):
        nonlocal observations
        result = await original_observe(*args, **kwargs)
        observed_cursors.append(detector._provider_audio_sample_cursor_16k)
        observations += 1
        if observations == 2:
            raise RuntimeError("test observer acknowledgement lost after bookkeeping")
        return result

    monkeypatch.setattr(
        detector, "observe_provider_audio_ordered", fail_after_second_real_observation
    )
    try:
        for _ in (1, 2):
            feed = await detector.feed(
                pcm,
                ingress_token=turn.ingress,
                speech_probability=0.9,
                rnnoise_available=True,
            )
            assert feed.identity is not None
            runtime._record_buffered_provider_speaker_observation(
                identity=feed.identity,
                byte_count=len(pcm),
                split_before_audio=True,
                evidence_complete=True,
            )
        activated = await runtime._activate_asr_audio_dispatcher(
            lifecycle,
            turn,
            buffered_pcm16=pcm + pcm,
        )
        assert not activated
        assert observations == 2
        assert observed_cursors == [1600, 3200]
        # Neither span was enqueued, although the real Detector accounted both.
        session.stream_audio.assert_not_awaited()
        assert runtime._asr_session is not session
        assert not runtime._ingress_token_matches(turn.ingress)
    finally:
        await _close_stack(core)


async def test_direct_activation_queue_rejection_retires_accounted_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    dispatcher = runtime._asr_audio_dispatcher
    dispatcher.abort(turn)
    await dispatcher.wait_idle()
    pcm = detector_fixture._speaker_pcm(100)
    queue_put = dispatcher._queue.put_nowait
    rejected = []

    def full_on_activation(command):
        if isinstance(command, AsrActivateCommand):
            rejected.append(command)
            raise asyncio.QueueFull
        return queue_put(command)

    monkeypatch.setattr(dispatcher._queue, "put_nowait", full_on_activation)
    try:
        feed = await detector.feed(
            pcm,
            ingress_token=turn.ingress,
            speech_probability=0.9,
            rnnoise_available=True,
        )
        assert feed.identity is not None
        runtime._record_buffered_provider_speaker_observation(
            identity=feed.identity,
            byte_count=len(pcm),
            split_before_audio=False,
            evidence_complete=True,
        )
        activated = await runtime._activate_asr_audio_dispatcher(
            lifecycle,
            turn,
            buffered_pcm16=pcm,
        )
        assert not activated
        assert len(rejected) == 1
        assert rejected[0].buffered_pcm16 == pcm
        assert not runtime._ingress_token_matches(turn.ingress)
        failures = tuple(dispatcher._failure_tasks)
        if failures:
            await asyncio.wait_for(asyncio.gather(*failures), timeout=1)
        assert runtime._asr_session is not session
        session.stream_audio.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_admission_effect_cancellation_after_observation_finishes_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    observed = asyncio.Event()
    release = asyncio.Event()
    original_observe = detector.observe_provider_audio_ordered

    async def held_acknowledgment(*args, **kwargs):
        result = await original_observe(*args, **kwargs)
        observed.set()
        await release.wait()
        return result

    monkeypatch.setattr(detector, "observe_provider_audio_ordered", held_acknowledgment)
    effect = asyncio.create_task(_submit_pcm(runtime, turn, sequence=1))
    # Use the real admission task registration and invalidation join machinery.
    runtime._track_admission_effect_task(effect, turn)
    effect.add_done_callback(runtime._admission_effect_done)
    try:
        await asyncio.wait_for(observed.wait(), timeout=1)
        assert detector._provider_audio_sample_cursor_16k == 1600
        effect.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(effect), timeout=1)
        await asyncio.wait_for(_drain_runtime_admission(runtime), timeout=1)
        owned = tuple(runtime._asr_owned_cleanup_tasks)
        if owned:
            await asyncio.wait_for(asyncio.gather(*owned), timeout=1)
        assert runtime._asr_session is not session
        assert not runtime._ingress_token_matches(turn.ingress)
        assert effect not in runtime._asr_admission_effect_task_turns
        session.stream_audio.assert_not_awaited()
    finally:
        release.set()
        if not effect.done():
            effect.cancel()
            await asyncio.gather(effect, return_exceptions=True)
        await _close_stack(core)


async def test_direct_activation_without_detector_identity_cannot_hide_audio_gap() -> (
    None
):
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    dispatcher = runtime._asr_audio_dispatcher
    dispatcher.abort(turn)
    await dispatcher.wait_idle()
    pcm = detector_fixture._speaker_pcm(100)
    try:
        # This is the production helper's explicit no-buffered-spans branch.
        assert runtime._asr_buffered_provider_speaker_observation is None
        activated = await runtime._activate_asr_audio_dispatcher(
            lifecycle,
            turn,
            buffered_pcm16=pcm,
        )
        await dispatcher.wait_idle()
        written = sum(
            len(call.args[0]) // 2 for call in session.stream_audio.await_args_list
        )
        if activated:
            assert written == 1600
            assert detector._provider_audio_sample_cursor_16k == written
        else:
            assert written == 0
            assert runtime._asr_session is not session
            assert not runtime._ingress_token_matches(turn.ingress)
    finally:
        await _close_stack(core)


async def test_late_observer_exception_cannot_poison_replacement_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    observed = asyncio.Event()
    release = asyncio.Event()
    original_observe = detector.observe_provider_audio_ordered

    async def fail_old_acknowledgment(*args, **kwargs):
        result = await original_observe(*args, **kwargs)
        if kwargs["identity"].ingress_token == turn.ingress:
            observed.set()
            await release.wait()
            raise RuntimeError("old observer acknowledgment failed after takeover")
        return result

    monkeypatch.setattr(
        detector, "observe_provider_audio_ordered", fail_old_acknowledgment
    )
    old_submit = asyncio.create_task(_submit_pcm(runtime, turn, sequence=1))
    try:
        await asyncio.wait_for(observed.wait(), timeout=1)
        assert detector._provider_audio_sample_cursor_16k == 1600
        await detector.reset_provider_audio_timeline()
        runtime._reset_asr_turn_state()
        lifecycle.stop()
        replacement = SimpleNamespace(
            is_ready=True,
            close=AsyncMock(),
            stream_audio=AsyncMock(),
            signal_user_activity_end=AsyncMock(),
        )
        replacement_lifecycle = VoiceInputLifecycleController(
            provider_policy=detector_fixture._provider_endpoint_policy(),
            shadow_mode=False,
        )
        replacement_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
        replacement_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
        replacement_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
        replacement_shadow = detector_fixture.SpeakerShadowRuntime(
            backend_factory=partial(_ConstantBackend, 0.95),
            config=detector_fixture._provider_speaker_config(),
        )
        replacement_detector = detector_fixture.DetectorRuntime(
            vad=detector_fixture._Vad(),
            gate=detector_fixture._Gate(),
            provider_policy=detector_fixture._provider_endpoint_policy(),
            speaker_shadow=replacement_shadow,
        )
        runtime._asr_session = replacement
        runtime._asr_lifecycle = replacement_lifecycle
        runtime._asr_detector = replacement_detector
        runtime._asr_session_epoch += 1
        runtime._asr_provider_speaker_sequence = 0
        runtime._asr_current_ingress_token = core._capture_ingress_token()
        replacement_turn = runtime._capture_turn_token(replacement_lifecycle)
        pcm = detector_fixture._speaker_pcm(100)
        feed = await replacement_detector.feed(
            pcm,
            ingress_token=replacement_turn.ingress,
            speech_probability=0.9,
            rnnoise_available=True,
        )
        assert feed.identity is not None
        assert await runtime._arm_speaker_authority_for_provider_audio(replacement_turn)
        assert await runtime._observe_admitted_provider_audio(
            replacement_lifecycle,
            replacement_detector,
            pcm,
            sample_rate_hz=16000,
            identity=feed.identity,
            split_before_audio=False,
            evidence_complete=True,
            turn_token=replacement_turn,
        )
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        assert ledger.poisoned_reason is None
        release.set()
        stale = await asyncio.wait_for(old_submit, timeout=1)
        assert stale.status is AsrSubmitStatus.STALE
        assert runtime._asr_session is replacement
        assert runtime._asr_provider_speaker_ledgers[evidence.candidate] is ledger
        assert ledger.poisoned_reason is None
        assert replacement_detector._provider_audio_sample_cursor_16k == 1600
        replacement.close.assert_not_awaited()
        session.stream_audio.assert_not_awaited()
    finally:
        release.set()
        if not old_submit.done():
            old_submit.cancel()
            await asyncio.gather(old_submit, return_exceptions=True)
        await _close_stack(core)
        await detector.close()
        await session.close()


async def test_enforcement_without_activation_owner_cannot_admit_audio() -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    runtime._speaker_verifier_activation_generation = None
    try:
        result = await _submit_pcm(runtime, turn, sequence=1)
        assert result.status is AsrSubmitStatus.UNAVAILABLE
        assert not runtime._ingress_token_matches(turn.ingress)
        assert runtime._asr_session is not session
        session.stream_audio.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_accounting_receipt_from_wrong_timeline_cannot_authorize_wire_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    try:
        first = await _submit_pcm(runtime, turn, sequence=1)
        assert first.status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        assert ledger.timeline_generation >= 0
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        original_observe = detector.observe_provider_audio_ordered

        async def wrong_timeline_acknowledgment(*args, **kwargs):
            result = await original_observe(*args, **kwargs)
            assert kwargs["accounting_only"] is True
            return replace(result, timeline_generation=result.timeline_generation + 1)

        monkeypatch.setattr(
            detector, "observe_provider_audio_ordered", wrong_timeline_acknowledgment
        )
        second = await _submit_pcm(runtime, turn, sequence=2)
        assert second.status is AsrSubmitStatus.UNAVAILABLE
        assert not runtime._ingress_token_matches(turn.ingress)
        assert session.stream_audio.await_count == 1
    finally:
        await _close_stack(core)


async def test_real_capture_unavailable_notifies_core_without_stopping_asr() -> None:
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    try:
        assert (
            await _submit_pcm(runtime, turn, sequence=1)
        ).status is AsrSubmitStatus.ACCEPTED
        core.send_status.reset_mock()
        await shadow.close()
        for sequence in (2, 3, 4):
            assert (
                await _submit_pcm(runtime, turn, sequence=sequence)
            ).status is AsrSubmitStatus.ACCEPTED
        await asyncio.gather(*tuple(runtime._asr_owned_cleanup_tasks))
        await runtime._asr_audio_dispatcher.wait_idle()
        notices = [
            json.loads(call.args[0]) for call in core.send_status.await_args_list
        ]
        degraded = [
            item
            for item in notices
            if item["code"] == "ASR_SPEAKER_EVIDENCE_UNAVAILABLE"
        ]
        assert len(degraded) == 1, notices
        assert degraded[0]["details"]["reason_code"]
        assert degraded[0]["details"]["incident_id"]
        assert degraded[0]["details"]["session_epoch"] == runtime._asr_session_epoch
        assert not any(
            item["code"] == "ASR_LIFECYCLE_STATE"
            and item["details"].get("state") == "blocked"
            for item in notices
        )
        assert runtime._asr_session is session
        assert core._asr_route_mode == "independent"
        assert detector._provider_audio_sample_cursor_16k == 6400
        assert (
            sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list)
            == 6400
        )
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_speaker_degradation_first_reason_survives_installation_retirement() -> (
    None
):
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    try:
        assert (
            await _submit_pcm(runtime, turn, sequence=1)
        ).status is AsrSubmitStatus.ACCEPTED
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        core.send_status.reset_mock()
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        await asyncio.gather(*tuple(runtime._asr_owned_cleanup_tasks))
        first_notices = [
            json.loads(call.args[0]) for call in core.send_status.await_args_list
        ]
        first = [
            item
            for item in first_notices
            if item["code"] == "ASR_SPEAKER_EVIDENCE_UNAVAILABLE"
        ]
        assert len(first) == 1, first_notices
        assert first[0]["details"]["reason_code"]
        assert first[0]["details"]["incident_id"]
        runtime.retire_speaker_verifier_authority()
        await asyncio.gather(*tuple(runtime._asr_owned_cleanup_tasks))
        after_notices = [
            json.loads(call.args[0]) for call in core.send_status.await_args_list
        ]
        after = [
            item
            for item in after_notices
            if item["code"] == "ASR_SPEAKER_EVIDENCE_UNAVAILABLE"
        ]
        assert after == first
        assert ledger.poisoned_reason == "speaker_capture_unavailable"
        assert runtime._asr_session is session
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_pending_old_speaker_degradation_cannot_notify_replacement_session() -> (
    None
):
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack()
    try:
        assert (
            await _submit_pcm(runtime, turn, sequence=1)
        ).status is AsrSubmitStatus.ACCEPTED
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        core.send_status.reset_mock()
        previous_tasks = set(runtime._asr_owned_cleanup_tasks)
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        notice_tasks = set(runtime._asr_owned_cleanup_tasks) - previous_tasks
        assert notice_tasks and all(not task.done() for task in notice_tasks)
        replacement = SimpleNamespace(
            is_ready=True, close=AsyncMock(), stream_audio=AsyncMock()
        )
        runtime._asr_session = replacement
        runtime._asr_session_epoch += 1
        runtime._asr_current_ingress_token = core._capture_ingress_token()
        await asyncio.gather(*tuple(runtime._asr_owned_cleanup_tasks))
        notices = [
            json.loads(call.args[0]) for call in core.send_status.await_args_list
        ]
        assert not any(
            item["code"] == "ASR_SPEAKER_EVIDENCE_UNAVAILABLE" for item in notices
        )
        assert runtime._asr_session is replacement
        replacement.close.assert_not_awaited()
    finally:
        await _close_stack(core)
        await session.close()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("cancel_callback", [False, True])
async def test_exact_unavailable_cleanup_preserves_ledger_owner(
    monkeypatch: pytest.MonkeyPatch, restart: bool, cancel_callback: bool
) -> None:
    """A late boundary owns its old ledger, even if new numeric keys repeat."""
    core, runtime, detector, shadow, lifecycle, session, turn = (
        await _active_real_stack(score=0.95)
    )
    entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks: list[asyncio.Task] = []
    partial_calls = []

    async def deliver_partial(event):
        partial_calls.append(event)
        entered.set()
        await release.wait()

    runtime._callbacks = replace(runtime._callbacks, on_partial=deliver_partial)
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        for sequence in range(1, 17):
            submitted = await _submit_pcm(runtime, turn, sequence=sequence)
            assert submitted.status is AsrSubmitStatus.ACCEPTED
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        boundary = asyncio.create_task(runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary", generation=0, buffer_epoch=0, utterance_id=1,
                boundary_quality="exact", audio_range=ProviderAudioRange(0, 25_600),
            ), runtime._asr_session_epoch,
        ))
        tasks.append(boundary)
        async with asyncio.timeout(2):
            while key not in runtime._asr_provider_exact_intervals:
                assert not boundary.done()
                await asyncio.sleep(0)
        transaction = runtime._asr_provider_exact_intervals[key]
        old_ledger = runtime._asr_provider_speaker_key_ledgers[key]
        successor = runtime._asr_provider_speaker_ledgers[
            transaction.successor_candidate
        ]
        await runtime._send_independent_asr_preview(
            "synthetic preview", runtime._asr_session_epoch
        )
        assert transaction.turn_token in runtime._asr_quarantined_partials
        conflict = asyncio.create_task(runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary", generation=0, buffer_epoch=0, utterance_id=1,
                boundary_quality="unknown", audio_range=None,
            ), runtime._asr_session_epoch,
        ))
        tasks.append(conflict)
        await asyncio.wait_for(entered.wait(), 2)
        core.continuity_score_host.ready.set()

        if restart:
            async def close_transport():
                closing.set()
                if cancel_callback:
                    conflict.cancel()
                    _, remaining = await asyncio.wait((conflict,), timeout=0.2)
                    for task in remaining:
                        task.cancel()

            session.close.side_effect = close_transport
            stop = asyncio.create_task(runtime.stop_session())
            tasks.append(stop)
            await asyncio.wait_for(closing.wait(), 2)
            # Stop now owns/cancels the internal partial callback before the
            # transport closes; it must no longer survive into the new session.
            await asyncio.wait_for(asyncio.gather(conflict, return_exceptions=True), 1)
            assert conflict.cancelled()
            new_session = SimpleNamespace(
                is_ready=True, connect=AsyncMock(), close=AsyncMock(),
                stream_audio=AsyncMock(), signal_user_activity_end=AsyncMock(),
            )
            monkeypatch.setattr(runtime_module, "_resolve_asr_selection",
                                lambda _: _selection("qwen", "provider"))
            monkeypatch.setattr(runtime_module, "_create_asr_session_from_selection",
                                lambda *args, **kwargs: new_session)
            real_detector = detector_fixture.DetectorRuntime
            monkeypatch.setattr(runtime_module, "DetectorRuntime", lambda **kwargs:
                real_detector(vad=detector_fixture._Vad(),
                              gate=detector_fixture._Gate(), **kwargs))

            def factory():
                return detector_fixture.SpeakerShadowRuntime(
                    backend_factory=partial(_ConstantBackend, 0.95),
                    config=detector_fixture._provider_speaker_config(),
                )

            factory.enforces_admission = True
            await asyncio.wait_for(runtime.start(
                route_key="qwen", resource_optimization_enabled=False,
                speaker_shadow_factory=factory,
            ), 2)
            assert runtime._asr_session is new_session
            new_lifecycle = runtime._asr_lifecycle
            new_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
            new_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
            runtime._asr_current_ingress_token = core._capture_ingress_token()
            new_turn = runtime._capture_turn_token(new_lifecycle)
            await runtime._prepare_independent_asr_turn(runtime._asr_session_epoch)
            assert runtime._asr_audio_dispatcher.activate(new_turn, new_session, b"")
            assert (await _submit_pcm(runtime, new_turn, sequence=1)).status is (
                AsrSubmitStatus.ACCEPTED
            )
            assert await runtime._handle_provider_utterance_started(
                ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
                runtime._asr_session_epoch,
            )
            new_ledger = runtime._asr_provider_speaker_key_ledgers[key]
            new_state = new_ledger.state
            assert new_ledger is not old_ledger
            assert new_ledger.turn_token != transaction.turn_token
            assert runtime._asr_detector is not transaction.detector
        elif cancel_callback:
            conflict.cancel()

        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 2
        )
        if cancel_callback or restart:
            assert isinstance(results[1], asyncio.CancelledError), results
        else:
            assert results[1] is None, results
        assert all(not isinstance(result, Exception) for result in results), results
        assert len(partial_calls) == 1
        assert partial_calls[0].turn_token == transaction.turn_token
        assert not transaction.event_queue
        assert all(task.done() for task in tasks)
        assert old_ledger.state is runtime_module._ProviderSpeakerLedgerState.RESOLVED
        if restart:
            assert runtime._asr_provider_speaker_key_ledgers.get(key) is new_ledger
            assert runtime._asr_provider_speaker_ledgers.get(new_ledger.candidate) is (
                new_ledger
            )
            assert new_ledger.state is new_state
            assert (await _submit_pcm(runtime, new_turn, sequence=2)).status is (
                AsrSubmitStatus.ACCEPTED
            )
            assert new_ledger.last_pcm_sequence_no == 2
            new_shadow = runtime._asr_detector._speaker_shadow
            await new_shadow.wait_idle()
            assert new_shadow._buffers[new_ledger.candidate].sample_count == 3_200
            runtime._retire_exact_interval_runtime_aliases(transaction)
            assert runtime._asr_provider_speaker_key_ledgers.get(key) is new_ledger
            new_session.close.assert_not_awaited()
        else:
            assert key not in runtime._asr_provider_speaker_key_ledgers
            assert runtime._asr_provider_speaker_ledgers.get(successor.candidate) is (
                successor
            )
            assert runtime._asr_provider_speaker_evidence_lease == successor.evidence_lease
            session.close.assert_not_awaited()
    finally:
        release.set()
        core.continuity_score_host.ready.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)
