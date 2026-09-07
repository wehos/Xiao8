"""Real Session/Runtime/Detector/Shadow/Admission, synthetic IO and scorer only."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from tests.unit.test_asr_phase3_session import (
    _make_rich_provider_session, _drain_session_pipelines, _recording_worker,
    _AsrWorkerEvent, _RealtimeAsrSessionImpl, AsrSessionConfig, ProviderAudioRange,
)
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack, _submit_pcm, _close_stack, _drain_runtime_admission,
)
from main_logic.asr_client._provider_events import (
    ProviderStartedSettlement, ProviderUtteranceKey,
)
from main_logic.asr_client.admission.contracts import AdmissionDisposition
from main_logic.voice_turn.contracts import AsrSubmitStatus


async def _inject_boundary(session, *, end=1000):
    common = dict(generation=0, buffer_epoch=0, utterance_id=1)
    await session._response_queue.put(_AsrWorkerEvent(
        kind="provider_endpoint", boundary_quality="exact",
        audio_range=ProviderAudioRange(0, end), **common,
    ))
    await asyncio.wait_for(session._response_queue.join(), 1)
    task = session._provider_boundary_tasks[(0, 0, 1)]
    deadline = session._provider_boundary_deadlines[(0, 0, 1)]
    assert await asyncio.wait_for(asyncio.shield(task), 1)
    assert time.monotonic() < deadline
    return common


async def test_completed_boundary_survives_delayed_final():
    events, legacy = [], []
    session = _make_rich_provider_session(events, legacy)
    try:
        await session.connect()
        await session.stream_audio(b"\x00\x00" * 1000)
        await session._response_queue.put(_AsrWorkerEvent(kind="utterance_started",
            generation=0, buffer_epoch=0, utterance_id=1, audio_start_sample_16k=0))
        await _drain_session_pipelines(session)
        common = await _inject_boundary(session)
        await asyncio.sleep(.32)
        await session._response_queue.put(_AsrWorkerEvent(kind="final", text="fixture", **common))
        await _drain_session_pipelines(session)
        assert [(e[1], e[3]) for e in events if e[0] == "endpoint"] == [
            ("boundary", "exact"), ("ordered", "exact")]
        assert len([e for e in events if e[0] == "final"]) == 1
        assert not session._provider_boundary_tasks
    finally:
        await session.close()


@pytest.mark.parametrize("delay", [.03, .32])
@pytest.mark.parametrize("speech_ms", [1600, 3258])
async def test_exact_scored_final_delivered_once_across_callback_deadline(delay, speech_ms):
    core, runtime, detector, shadow, lifecycle, transport, turn = await _active_real_stack(score=.95)
    core.continuity_score_host.ready.set()
    events = []

    async def started(notification):
        outcome = await runtime._bind_provider_utterance_started(notification, runtime._asr_session_epoch)
        assert outcome.value not in {"failed", "stale", "bound_speaker_unavailable"}
        return ProviderStartedSettlement.BOUND_EXACT_PENDING

    async def endpoint(notification):
        events.append((notification.phase, notification.boundary_quality))
        await runtime._handle_provider_endpoint_notification(notification, runtime._asr_session_epoch)

    async def final(notification):
        await runtime._handle_provider_final(notification.key, notification.text,
            runtime._asr_session_epoch, "qwen", received_at=notification.received_at,
            admission_deadline=notification.admission_deadline)

    session = _RealtimeAsrSessionImpl(worker_fn=_recording_worker, api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(), on_connection_error=AsyncMock(),
        on_provider_utterance_started=started, on_provider_endpoint=endpoint,
        on_provider_final_ready=final)
    try:
        await session.connect()
        transport.stream_audio.side_effect = session.stream_audio
        common = dict(generation=0, buffer_epoch=0, utterance_id=1)
        await session._response_queue.put(_AsrWorkerEvent(kind="utterance_started",
            audio_start_sample_16k=0, **common))
        await _drain_session_pipelines(session)
        key = ProviderUtteranceKey(0, 0, 1)
        turn = runtime._asr_provider_started_turns[key]
        durations = [100] * (speech_ms // 100) + ([speech_ms % 100] if speech_ms % 100 else [])
        for seq, duration in enumerate(durations, 1):
            submitted = await _submit_pcm(runtime, turn, sequence=seq, duration_ms=duration)
            assert submitted.status is AsrSubmitStatus.ACCEPTED
            await runtime._asr_audio_dispatcher.wait_idle()
            await shadow.wait_idle()
        await _inject_boundary(session, end=speech_ms * 16)
        transaction = runtime._asr_provider_exact_intervals[key]
        await asyncio.sleep(delay)
        for _ in range(2):
            await session._response_queue.put(_AsrWorkerEvent(kind="final", text="fixture", **common))
            await _drain_session_pipelines(session)
        await _drain_runtime_admission(runtime)
        await runtime.wait_transcript_idle()
        await core._voice_input_registry.wait_idle()
        assert events == [("boundary", "exact"), ("ordered", "exact")]
        assert transaction.resolved_disposition is AdmissionDisposition.FORWARD
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
        assert core.continuity_score_host.calls == (1 if speech_ms == 1600 else 2)
        assert runtime._speaker_verifier_diagnostics()["speaker_unavailable_count"] == 0
        assert not runtime._asr_admission_final_contexts
        assert transaction.successor_candidate in shadow._candidate_tokens
    finally:
        await session.close()
        await _close_stack(core)
