"""A failed predecessor must not retire a route installed by a real start."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client import runtime as runtime_module
from main_logic.asr_client.runtime import AsrStartStatus
from tests.unit.test_core_independent_asr import _selection
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack,
    _close_stack,
    _submit_pcm,
    detector_fixture,
)
from main_logic.voice_turn.contracts import AsrSubmitStatus


async def test_partial_audio_failure_cannot_detach_successful_start_takeover(
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
    invalidation_completed = asyncio.Event()
    resume_failure = asyncio.Event()
    original_finish = runtime._finish_admission_invalidation
    invalidation_calls = 0

    async def hold_first_invalidation_return(*args, **kwargs):
        nonlocal invalidation_calls
        invalidation_calls += 1
        ordinal = invalidation_calls
        # Execute the actual ingress invalidation, effect settlement, correlator
        # retirement and dispatcher invalidation. Only its return is gated.
        await original_finish(*args, **kwargs)
        if ordinal == 1:
            invalidation_completed.set()
            await resume_failure.wait()

    monkeypatch.setattr(
        runtime, "_finish_admission_invalidation", hold_first_invalidation_return
    )
    replacement = SimpleNamespace(
        is_ready=True,
        connect=AsyncMock(),
        close=AsyncMock(),
        stream_audio=AsyncMock(),
        signal_user_activity_end=AsyncMock(),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        lambda _: _selection("qwen", "provider"),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        lambda *args, **kwargs: replacement,
    )

    def create_real_detector(**kwargs):
        return detector_fixture.DetectorRuntime(
            vad=detector_fixture._Vad(),
            gate=detector_fixture._Gate(),
            **kwargs,
        )

    monkeypatch.setattr(runtime_module, "DetectorRuntime", create_real_detector)
    failure = None
    try:
        assert (
            await _submit_pcm(runtime, turn, sequence=1)
        ).status is AsrSubmitStatus.ACCEPTED
        identity = runtime._capture_runtime_identity(ingress_token=turn.ingress)
        failure = asyncio.create_task(runtime._retire_partial_provider_audio(identity))
        await asyncio.wait_for(invalidation_completed.wait(), timeout=2)
        assert not failure.done()
        start = await asyncio.wait_for(
            runtime.start(route_key="qwen", resource_optimization_enabled=False),
            timeout=3,
        )
        assert start.status is AsrStartStatus.READY
        assert runtime._asr_session is replacement
        replacement_lifecycle = runtime._asr_lifecycle
        replacement_detector = runtime._asr_detector
        replacement_audio = runtime._asr_audio_dispatcher
        replacement_events = runtime._asr_detector_dispatcher
        replacement_transcripts = runtime._asr_transcript_dispatcher
        assert replacement_lifecycle is not lifecycle
        assert replacement_detector is not detector
        replacement_epoch = runtime._asr_session_epoch
        core.send_status.reset_mock()
        resume_failure.set()
        await asyncio.wait_for(failure, timeout=2)
        assert runtime._asr_session is replacement
        assert runtime._asr_lifecycle is replacement_lifecycle
        assert runtime._asr_detector is replacement_detector
        assert runtime._asr_audio_dispatcher is replacement_audio
        assert runtime._asr_detector_dispatcher is replacement_events
        assert runtime._asr_transcript_dispatcher is replacement_transcripts
        assert runtime._asr_session_epoch == replacement_epoch
        replacement.close.assert_not_awaited()
        notices = [
            json.loads(call.args[0]) for call in core.send_status.await_args_list
        ]
        assert not any(item["code"] == "ASR_AUDIO_ORDERING_FAILED" for item in notices)
        assert not any(
            item["code"] == "ASR_LIFECYCLE_STATE"
            and item["details"].get("state") == "blocked"
            for item in notices
        )
    finally:
        resume_failure.set()
        if failure is not None:
            await asyncio.gather(failure, return_exceptions=True)
        await _close_stack(core)
