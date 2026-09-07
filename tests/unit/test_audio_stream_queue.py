import asyncio
import json
import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from main_logic.core import LLMSessionManager
from main_logic.core import asr_runtime as core_asr_runtime_module
from main_logic.omni_realtime_client import OmniRealtimeClient
from main_logic.voice_turn.audio_input import ProcessedVoiceFrame
from main_logic.core.asr_runtime import (
    _AudioDurationQueue as AudioDurationQueue,
    _HotSwapAudioBuffer as HotSwapAudioBuffer,
    _HotSwapAudioFrame as HotSwapAudioFrame,
    _QueuedMicFrame as QueuedMicFrame,
)
from main_logic.asr_client.lifecycle import (
    VoiceIngressToken,
    VoiceLifecycleEvent,
    VoiceLifecycleState,
    VoiceRouteMode,
    VoiceTurnToken,
)
from main_logic.voice_turn.contracts import VoicePartialEvent, VoiceTranscriptEvent
from main_logic.asr_client.lifecycle import VoiceInputLifecycleController
from main_logic.asr_client.provider_policy import resolve_provider_policy
from main_logic.asr_client.runtime import AsrStartResult, AsrStartStatus


async def test_starting_session_audio_does_not_enter_pending_input_data():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.session_ready = False
    mgr._starting_session_count = 1
    mgr.pending_input_data = []
    mgr.input_cache_lock = asyncio.Lock()

    await LLMSessionManager._stream_data_now(
        mgr, {"input_type": "audio", "data": [0] * 480}
    )

    assert mgr.pending_input_data == []


async def test_goodbye_silent_drops_live_vision_stream_before_processing():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr.goodbye_silent = True
    mgr._stream_data_now = AsyncMock()

    await LLMSessionManager.stream_data(
        mgr,
        {"input_type": "screen", "data": "data:image/jpeg;base64,abc"},
    )

    mgr._stream_data_now.assert_not_awaited()


async def test_live_vision_stream_does_not_auto_start_session_when_inactive():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr.goodbye_silent = False
    mgr.session_ready = False
    mgr._starting_session_count = 0
    mgr.session = None
    mgr.is_active = False
    mgr.input_cache_lock = asyncio.Lock()
    mgr.start_session = AsyncMock()

    await LLMSessionManager._stream_data_now(
        mgr,
        {"input_type": "screen", "data": "data:image/jpeg;base64,abc"},
    )

    mgr.start_session.assert_not_awaited()


async def test_goodbye_silent_drops_live_vision_stream_in_internal_processor():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr.goodbye_silent = True
    mgr.session = MagicMock()
    mgr.session.stream_image = AsyncMock()
    mgr.is_active = True

    await LLMSessionManager._process_stream_data_internal(
        mgr,
        {"input_type": "camera", "data": "data:image/jpeg;base64,abc"},
    )

    mgr.session.stream_image.assert_not_called()


async def test_flush_pending_input_data_routes_audio_through_bounded_queue():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    audio_msg = {"input_type": "audio", "data": [1] * 480}
    text_msg = {"input_type": "text", "data": "hello"}
    mgr.pending_input_data = [audio_msg, text_msg]
    mgr.input_cache_lock = asyncio.Lock()
    mgr.session = object()
    mgr.is_active = True
    mgr._enqueue_audio_stream_data = AsyncMock()
    mgr._process_stream_data_internal = AsyncMock()

    await LLMSessionManager._flush_pending_input_data(mgr)

    mgr._enqueue_audio_stream_data.assert_awaited_once_with(audio_msg)
    mgr._process_stream_data_internal.assert_awaited_once_with(text_msg)
    assert mgr.pending_input_data == []


async def test_cancelled_pending_input_flush_restores_unprocessed_suffix_first():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    first = {"input_type": "text", "data": "first"}
    blocked = {"input_type": "text", "data": "blocked"}
    suffix = {"input_type": "text", "data": "suffix"}
    live = {"input_type": "text", "data": "live"}
    mgr.pending_input_data = [first, blocked, suffix]
    mgr.input_cache_lock = asyncio.Lock()
    mgr.session = object()
    mgr.is_active = True
    mgr._enqueue_audio_stream_data = AsyncMock()
    blocked_started = asyncio.Event()

    async def process(message):
        if message is blocked:
            blocked_started.set()
            await asyncio.Event().wait()

    mgr._process_stream_data_internal = process
    task = asyncio.create_task(LLMSessionManager._flush_pending_input_data(mgr))
    await blocked_started.wait()
    mgr.pending_input_data.append(live)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert mgr.pending_input_data == [blocked, suffix, live]
    assert mgr._pending_input_flush_active is False


async def test_failed_pending_input_drops_only_current_and_continues_suffix():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    first = {"input_type": "text", "data": "first"}
    failed = {"input_type": "text", "data": "failed"}
    suffix = {"input_type": "text", "data": "suffix"}
    live = {"input_type": "text", "data": "live"}
    mgr.pending_input_data = [first, failed, suffix]
    mgr.input_cache_lock = asyncio.Lock()
    mgr.session = object()
    mgr.is_active = True
    mgr._enqueue_audio_stream_data = AsyncMock()
    attempted: list[dict] = []

    async def process(message):
        attempted.append(message)
        if message is failed:
            mgr.pending_input_data.append(live)
            raise RuntimeError("bad cached message")

    mgr._process_stream_data_internal = process

    await LLMSessionManager._flush_pending_input_data(mgr)

    assert attempted == [first, failed, suffix, live]
    assert mgr.pending_input_data == []
    assert mgr._pending_input_flush_active is False


def _queue_token() -> VoiceIngressToken:
    return VoiceIngressToken(1, "socket", 1, 1, 1)


def _hot_swap_frame(
    token: VoiceIngressToken,
    *,
    samples: int = 160,
    speech_probability: float | None = 0.5,
    rnnoise_available: bool = True,
    captured_at: float = 0.0,
) -> HotSwapAudioFrame:
    return HotSwapAudioFrame(
        pcm16=b"\x01\x00" * samples,
        token=token,
        captured_at=captured_at,
        speech_probability=speech_probability,
        rnnoise_available=rnnoise_available,
    )


def _authorize_core_lease(mgr: LLMSessionManager) -> None:
    mgr._voice_lease_synchronized = True
    mgr._voice_lease_owner = "core"
    mgr._voice_input_suppressed = False


async def test_native_audio_without_asr_lifecycle_reaches_internal_processor():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr._init_asr_runtime_state()
    _authorize_core_lease(mgr)
    mgr._set_microphone_route("native")
    mgr._ensure_audio_stream_worker = lambda: None
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=10,
    )
    mgr._audio_stream_dropped_total = 0
    mgr._last_audio_stream_backlog_log_time = 0.0
    message = {"input_type": "audio", "data": [1] * 160}

    await LLMSessionManager._enqueue_audio_stream_data(mgr, message)

    frame = await mgr._audio_stream_queue.get()
    assert frame.message is message
    assert mgr._ingress_token_matches(frame.token)
    mgr._set_microphone_route("blocked")
    assert not mgr._ingress_token_matches(frame.token)
    mgr._audio_stream_queue.task_done()


def test_audio_stream_queue_uses_ceiling_duration_accounting():
    frame = QueuedMicFrame.from_message(
        {
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [1] * 161,
        },
        token=_queue_token(),
        received_at=1.0,
    )

    assert frame.duration_us == 10_063


async def test_audio_stream_queue_clears_whole_candidate_when_full():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    _authorize_core_lease(mgr)
    mgr._asr_runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    mgr._asr_runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=2,
    )
    mgr._audio_stream_dropped_total = 0
    mgr._last_audio_stream_backlog_log_time = 0.0
    mgr._ensure_audio_stream_worker = lambda: None
    mgr._bg_tasks = set()
    mgr._asr_runtime.abort = AsyncMock()

    def message(seq: int) -> dict:
        return {
            "seq": seq,
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [seq] * 160,
        }

    await LLMSessionManager._enqueue_audio_stream_data(mgr, message(1))
    await LLMSessionManager._enqueue_audio_stream_data(mgr, message(2))
    await LLMSessionManager._enqueue_audio_stream_data(mgr, message(3))
    await asyncio.gather(*list(mgr._bg_tasks))

    assert mgr._audio_stream_dropped_total == 3
    assert mgr._audio_stream_queue.empty()
    mgr._asr_runtime.abort.assert_awaited_once_with("ingress_backpressure")


async def test_active_audio_queue_overflow_aborts_turn_then_resumes_local_listen():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr.send_status = AsyncMock()
    mgr._init_asr_runtime_state()
    _authorize_core_lease(mgr)
    mgr._asr_runtime._asr_provider = "qwen"
    mgr._asr_runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    mgr._asr_runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    mgr._asr_runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    mgr._asr_runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    mgr._asr_runtime._asr_current_ingress_token = mgr._capture_ingress_token()
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=1,
    )
    mgr._audio_stream_dropped_total = 0
    mgr._last_audio_stream_backlog_log_time = 0.0
    mgr._ensure_audio_stream_worker = lambda: None
    mgr._bg_tasks = set()
    message = {
        "input_type": "audio",
        "sample_rate_hz": 16_000,
        "data": [1] * 160,
    }

    await LLMSessionManager._enqueue_audio_stream_data(mgr, message)
    await LLMSessionManager._enqueue_audio_stream_data(mgr, message)
    await asyncio.gather(*list(mgr._bg_tasks))

    assert mgr._asr_runtime._asr_lifecycle is not None
    assert (
        mgr._asr_runtime._asr_lifecycle.snapshot.state
        is VoiceLifecycleState.LOCAL_LISTEN
    )
    assert mgr._audio_stream_queue.empty()
    assert any(
        "ASR_INGRESS_BACKPRESSURE" in call.args[0]
        for call in mgr.send_status.await_args_list
    )
    assert mgr._omni_mic_audio_bytes == 0


async def test_audio_worker_leaves_runtime_generation_validation_to_submit():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr._init_asr_runtime_state()
    _authorize_core_lease(mgr)
    mgr._asr_runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    mgr._asr_runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=4,
    )
    mgr._audio_stream_dropped_total = 0
    mgr._process_microphone_stream_data = AsyncMock()
    frame = QueuedMicFrame.from_message(
        {
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [1] * 160,
        },
        token=mgr._capture_ingress_token(),
    )
    mgr._audio_stream_queue.put_nowait(frame)
    mgr._asr_runtime._asr_audio_generation += 1

    worker = asyncio.create_task(LLMSessionManager._audio_stream_worker_loop(mgr))
    while not mgr._audio_stream_queue.empty():
        await asyncio.sleep(0)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    mgr._process_microphone_stream_data.assert_awaited_once_with(
        frame.message,
        ingress_token=frame.token,
        audio_stream_epoch=frame.audio_stream_epoch,
        ingress_sequence=frame.ingress_sequence,
        captured_at=frame.captured_at,
    )
    assert mgr._audio_stream_dropped_total == 0


async def test_audio_worker_does_not_wait_for_core_session_readiness():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr._init_asr_runtime_state()
    _authorize_core_lease(mgr)
    mgr._asr_runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    mgr._asr_runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=4,
    )
    mgr._audio_stream_dropped_total = 0
    mgr.session_ready = False
    mgr._starting_session_count = 1
    mgr._process_microphone_stream_data = AsyncMock()
    frame = QueuedMicFrame.from_message(
        {
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [1] * 160,
        },
        token=mgr._capture_ingress_token(),
    )
    mgr._audio_stream_queue.put_nowait(frame)

    worker = asyncio.create_task(LLMSessionManager._audio_stream_worker_loop(mgr))
    while not mgr._audio_stream_queue.empty():
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    mgr._process_microphone_stream_data.assert_awaited_once_with(
        frame.message,
        ingress_token=frame.token,
        audio_stream_epoch=frame.audio_stream_epoch,
        ingress_sequence=frame.ingress_sequence,
        captured_at=frame.captured_at,
    )


async def test_inflight_audio_is_dropped_when_epoch_changes():
    mgr = _make_routable_audio_manager(True)

    async def advance_epoch(*_args, **_kwargs):
        mgr._audio_stream_epoch += 1
        return ProcessedVoiceFrame(b"\x01\x00" * 160, 16_000, 0.5, True)

    mgr._voice_input_audio_pipeline.process = AsyncMock(side_effect=advance_epoch)

    await _process_microphone_message(
        mgr,
        {"input_type": "audio", "data": [1] * 480},
    )

    mgr._route_microphone_audio.assert_not_awaited()
    mgr.session.stream_audio.assert_not_awaited()


def _make_routable_audio_manager(route_result: bool):
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    _authorize_core_lease(mgr)
    mgr._set_microphone_route("native")
    mgr.session_ready = True
    mgr._starting_session_count = 0
    mgr.is_active = True
    mgr.session_start_failure_count = 0
    mgr.session_start_max_failures = 3
    mgr._session_start_circuit_open = False
    mgr._audio_stream_epoch = 0
    mgr.session_closed_by_server = False
    mgr.last_audio_send_error_time = 0.0
    mgr.audio_error_log_interval = 2.0
    mgr.is_hot_swap_imminent = False
    mgr.is_flushing_hot_swap_cache = False
    mgr.hot_swap_cache_lock = asyncio.Lock()
    mgr._route_microphone_audio = AsyncMock(return_value=route_result)
    mgr._record_omni_microphone_audio = MagicMock()
    mgr._voice_input_audio_pipeline.process = AsyncMock(
        return_value=ProcessedVoiceFrame(
            pcm16=b"\x01\x00" * 160,
            sample_rate_hz=16_000,
            speech_probability=0.5,
            rnnoise_available=True,
        )
    )

    class _RealtimeSession(OmniRealtimeClient):
        def __init__(self):
            self.ws = object()
            self._fatal_error_occurred = False
            self._audio_processor = object()
            self.stream_audio = AsyncMock()
            # The class bypasses OmniRealtimeClient.__init__ (no _is_gemini),
            # so the real coroutine would raise AttributeError and be swallowed
            # by the reset helper, making native-clear assertions vacuous.
            self.clear_audio_buffer = AsyncMock()

        async def process_audio_chunk_async(self, audio_bytes):
            return audio_bytes

    mgr.session = _RealtimeSession()
    return mgr


async def _process_microphone_message(
    mgr: LLMSessionManager,
    message: dict,
) -> VoiceIngressToken:
    token = mgr._capture_ingress_token()
    await LLMSessionManager._process_microphone_stream_data(
        mgr,
        message,
        ingress_token=token,
    )
    return token


async def test_independent_asr_route_does_not_send_microphone_audio_to_omni():
    mgr = _make_routable_audio_manager(True)

    await _process_microphone_message(
        mgr,
        {"input_type": "audio", "data": [1] * 480},
    )

    mgr._route_microphone_audio.assert_awaited_once()
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_binary_audio_sample_rate_contract_reaches_audio_pipeline():
    mgr = _make_routable_audio_manager(True)

    await _process_microphone_message(
        mgr,
        {
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [1] * 480,
        },
    )

    mgr._voice_input_audio_pipeline.process.assert_awaited_once_with(
        struct.pack("<480h", *([1] * 480)),
        sample_rate_hz=16_000,
        ingress_sequence=1,
        captured_at=ANY,
    )


async def test_blocked_route_never_sends_microphone_audio_to_omni():
    mgr = _make_routable_audio_manager(True)

    await _process_microphone_message(
        mgr,
        {"input_type": "audio", "data": [1] * 480},
    )

    mgr._route_microphone_audio.assert_awaited_once()
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_independent_audio_route_precedes_omni_websocket_checks():
    mgr = _make_routable_audio_manager(True)
    mgr.session.ws = None
    mgr._voice_input_audio_pipeline.process = AsyncMock(
        return_value=ProcessedVoiceFrame(
            pcm16=b"\x02\x00" * 160,
            sample_rate_hz=16_000,
            speech_probability=0.8,
            rnnoise_available=True,
        )
    )

    token = await _process_microphone_message(
        mgr,
        {"input_type": "audio", "data": [1] * 480},
    )

    mgr._voice_input_audio_pipeline.process.assert_awaited_once()
    mgr._route_microphone_audio.assert_awaited_once_with(
        b"\x02\x00" * 160,
        sample_rate_hz=16_000,
        speech_probability=0.8,
        rnnoise_available=True,
        rnnoise_evidence=None,
        ingress_token=token,
        ingress_sequence=1,
        captured_at=ANY,
    )
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_fatal_omni_state_does_not_block_independent_asr_audio():
    mgr = _make_routable_audio_manager(True)
    mgr.session._fatal_error_occurred = True

    await _process_microphone_message(
        mgr,
        {"input_type": "audio", "data": [1] * 480},
    )

    mgr._voice_input_audio_pipeline.process.assert_awaited_once()
    mgr._route_microphone_audio.assert_awaited_once()
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_independent_audio_route_does_not_require_omni_session_container():
    mgr = _make_routable_audio_manager(True)
    mgr.session = type("TextOnlyCore", (), {})()
    mgr.start_session = AsyncMock()
    mgr.end_session = AsyncMock()

    token = await _process_microphone_message(
        mgr,
        {"input_type": "audio", "data": [1] * 480},
    )

    mgr._route_microphone_audio.assert_awaited_once_with(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        speech_probability=0.5,
        rnnoise_available=True,
        rnnoise_evidence=None,
        ingress_token=token,
        ingress_sequence=1,
        captured_at=ANY,
    )
    mgr.start_session.assert_not_awaited()
    mgr.end_session.assert_not_awaited()


async def test_active_teardown_blocks_audio_while_independent_asr_close_waits():
    mgr = _make_routable_audio_manager(True)
    del mgr._route_microphone_audio
    mgr._init_asr_runtime_state()
    mgr._set_microphone_route("independent")
    mgr._asr_runtime._asr_provider = "dummy"
    mgr.lock = asyncio.Lock()
    mgr._user_session_abandon_epoch = 0
    mgr._reset_tts_retry_state = lambda: None
    mgr._reset_proactive_gate = lambda: None

    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class _WaitingAsr:
        async def close(self):
            close_started.set()
            await allow_close.wait()

    mgr._asr_runtime._asr_session = _WaitingAsr()

    end_task = asyncio.create_task(LLMSessionManager.end_session(mgr))
    await close_started.wait()
    try:
        assert mgr.is_active is True
        assert mgr.session_ready is True
        assert mgr._asr_route_mode == "blocked"

        await LLMSessionManager._process_stream_data_internal(
            mgr,
            {"input_type": "audio", "data": [1] * 480},
        )

        mgr.session.stream_audio.assert_not_awaited()
        mgr._record_omni_microphone_audio.assert_not_called()
    finally:
        end_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await end_task

    assert mgr._asr_route_mode == "blocked"


async def test_hot_swap_flush_preserves_identity_and_detector_metadata():
    mgr = _make_routable_audio_manager(True)
    token = _queue_token()
    mgr._ingress_token_matches = MagicMock(return_value=True)
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    assert mgr.hot_swap_audio_cache.append(
        _hot_swap_frame(
            token,
            speech_probability=0.75,
            rnnoise_available=True,
            captured_at=1234.5,
        )
    )

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    mgr._ingress_token_matches.assert_called_once_with(token)
    mgr._route_microphone_audio.assert_awaited_once_with(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        speech_probability=0.75,
        rnnoise_available=True,
        rnnoise_evidence=None,
        ingress_token=token,
        ingress_sequence=0,
        captured_at=1234.5,
    )
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()
    assert not mgr.hot_swap_audio_cache


async def test_hot_swap_flush_supports_text_only_core_without_omni_pcm():
    mgr = _make_routable_audio_manager(False)
    omni_session = mgr.session
    mgr.session = type("TextOnlyCore", (), {})()
    mgr._ingress_token_matches = MagicMock(return_value=True)
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(_queue_token()))

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    mgr._route_microphone_audio.assert_awaited_once()
    omni_session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_hot_swap_flush_discards_stale_generation():
    mgr = _make_routable_audio_manager(True)
    mgr._ingress_token_matches = MagicMock(return_value=False)
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(_queue_token()))

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    mgr._route_microphone_audio.assert_not_awaited()
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_hot_swap_rebinds_queued_raw_and_processed_cache_in_order():
    mgr = _make_routable_audio_manager(True)
    old_token = mgr._capture_ingress_token()
    mgr.is_hot_swap_imminent = True
    mgr._set_microphone_route("blocked")

    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    assert mgr.hot_swap_audio_cache.append(
        HotSwapAudioFrame(pcm16=b"\x10\x00" * 160, token=old_token)
    )
    mgr._voice_input_audio_pipeline.process = AsyncMock(
        return_value=ProcessedVoiceFrame(
            b"\x20\x00" * 160,
            16_000,
            0.8,
            True,
        )
    )
    message = {
        "input_type": "audio",
        "sample_rate_hz": 16_000,
        "data": [2] * 160,
    }
    mgr._audio_stream_queue.put_nowait(
        QueuedMicFrame.from_message(message, token=old_token)
    )
    worker = asyncio.create_task(LLMSessionManager._audio_stream_worker_loop(mgr))
    try:
        deadline = asyncio.get_running_loop().time() + 1
        while len(mgr.hot_swap_audio_cache) < 2:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("queued raw frame was not processed")
            await asyncio.sleep(0)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert mgr._audio_stream_dropped_total == 0
    mgr._set_microphone_route("independent")
    current_token = mgr._capture_ingress_token()
    assert current_token.route_generation != old_token.route_generation

    routed: list[tuple[bytes, VoiceIngressToken]] = []

    async def route(
        pcm16: bytes,
        *,
        ingress_token: VoiceIngressToken,
        **_kwargs,
    ) -> bool:
        routed.append((pcm16[:2], ingress_token))
        return True

    mgr._route_microphone_audio = AsyncMock(side_effect=route)
    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    assert routed == [
        (b"\x10\x00", current_token),
        (b"\x20\x00", current_token),
    ]
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_hot_swap_rebinds_inflight_pipeline_result_after_core_swap():
    mgr = _make_routable_audio_manager(True)
    old_token = mgr._capture_ingress_token()
    old_pipeline = mgr._voice_input_audio_pipeline
    processing_started = asyncio.Event()
    release_processing = asyncio.Event()

    async def process(*_args, **_kwargs):
        processing_started.set()
        await release_processing.wait()
        return ProcessedVoiceFrame(b"\x30\x00" * 160, 16_000, 0.9, True)

    old_pipeline.process = AsyncMock(side_effect=process)
    processing = asyncio.create_task(
        LLMSessionManager._process_microphone_stream_data(
            mgr,
            {"input_type": "audio", "sample_rate_hz": 16_000, "data": [3] * 160},
            ingress_token=old_token,
        )
    )
    await asyncio.wait_for(processing_started.wait(), 1)

    mgr.is_hot_swap_imminent = True
    mgr._set_microphone_route("blocked")
    mgr.session = type("NewCoreSession", (), {"stream_audio": AsyncMock()})()
    mgr._voice_input_audio_pipeline = type(
        "NewPipeline",
        (),
        {"process": AsyncMock(), "close": AsyncMock()},
    )()
    mgr._set_microphone_route("independent")
    current_token = mgr._capture_ingress_token()
    routed: list[tuple[bytes, VoiceIngressToken]] = []

    async def route(
        pcm16: bytes,
        *,
        ingress_token: VoiceIngressToken,
        **_kwargs,
    ) -> bool:
        routed.append((pcm16, ingress_token))
        return True

    mgr._route_microphone_audio = AsyncMock(side_effect=route)
    flush = asyncio.create_task(LLMSessionManager._flush_hot_swap_audio_cache(mgr))
    await asyncio.sleep(0)
    assert not flush.done()

    release_processing.set()
    await asyncio.wait_for(asyncio.gather(processing, flush), 1)

    assert routed == [(b"\x30\x00" * 160, current_token)]
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_hot_swap_queue_full_retry_rebinds_without_silent_drop():
    mgr = _make_routable_audio_manager(True)
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=20_000,
        max_frames=1,
    )
    mgr._audio_stream_worker_task = asyncio.current_task()
    old_token = mgr._capture_ingress_token()
    first_message = {
        "input_type": "audio",
        "sample_rate_hz": 16_000,
        "data": [1] * 160,
    }
    second_message = {
        "input_type": "audio",
        "sample_rate_hz": 16_000,
        "data": [2] * 160,
    }
    mgr._audio_stream_queue.put_nowait(
        QueuedMicFrame.from_message(first_message, token=old_token)
    )

    async def enter_hot_swap_and_free_slot() -> None:
        mgr.is_hot_swap_imminent = True
        mgr._set_microphone_route("blocked")
        mgr._audio_stream_queue.get_nowait()
        mgr._audio_stream_queue.task_done()

    transition = asyncio.create_task(enter_hot_swap_and_free_slot())
    await LLMSessionManager._enqueue_audio_stream_data(mgr, second_message)
    await transition

    rebound = mgr._audio_stream_queue.get_nowait()
    mgr._audio_stream_queue.task_done()
    assert rebound.message == second_message
    assert rebound.token == mgr._capture_ingress_token()
    assert mgr._audio_stream_dropped_total == 0


@pytest.mark.parametrize(
    "stale_reason",
    [
        "session_epoch",
        "audio_generation",
        "audio_stream_epoch",
        "connection",
        "lease",
        "hard_mute",
        "focus_suppressed",
        "game",
    ],
)
def test_hot_swap_never_rebinds_lease_mute_or_game_identity(
    stale_reason: str,
):
    mgr = _make_routable_audio_manager(True)
    old_token = mgr._capture_ingress_token()
    old_audio_stream_epoch = mgr._audio_stream_epoch
    mgr.is_hot_swap_imminent = True
    mgr._set_microphone_route("blocked")
    mgr._set_microphone_route("independent")

    if stale_reason == "session_epoch":
        mgr._asr_runtime._asr_session_epoch += 1
    elif stale_reason == "audio_generation":
        mgr._asr_runtime._asr_audio_generation += 1
    elif stale_reason == "audio_stream_epoch":
        mgr._audio_stream_epoch += 1
    elif stale_reason == "connection":
        mgr._voice_lease_connection_id = "replacement"
    elif stale_reason == "lease":
        mgr._voice_lease_generation += 1
    elif stale_reason == "hard_mute":
        mgr._voice_lease_hard_muted = True
    elif stale_reason == "focus_suppressed":
        mgr._voice_lease_focus_suppressed = True
    else:
        mgr._voice_lease_owner = "game"

    assert (
        mgr._rebind_hot_swap_ingress_token(
            old_token,
            audio_stream_epoch=old_audio_stream_epoch,
        )
        is None
    )


def test_hot_swap_rebind_changes_only_route_generation():
    mgr = _make_routable_audio_manager(True)
    old_token = mgr._capture_ingress_token()
    audio_stream_epoch = mgr._audio_stream_epoch
    mgr.is_hot_swap_imminent = True
    mgr._set_microphone_route("blocked")

    rebound = mgr._rebind_hot_swap_ingress_token(
        old_token,
        audio_stream_epoch=audio_stream_epoch,
    )

    assert rebound == mgr._capture_ingress_token()
    assert rebound is not None
    assert rebound.session_epoch == old_token.session_epoch
    assert rebound.audio_generation == old_token.audio_generation
    assert rebound.connection_id == old_token.connection_id
    assert rebound.lease_generation == old_token.lease_generation
    assert rebound.route_generation != old_token.route_generation


async def test_hot_swap_flush_rechecks_cutoff_after_event_edge():
    mgr = _make_routable_audio_manager(True)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.is_hot_swap_imminent = True
    token = mgr._capture_ingress_token()
    started = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    call_index = 0

    async def process(*_args, **_kwargs):
        nonlocal call_index
        index = call_index
        call_index += 1
        started[index].set()
        await release[index].wait()
        return ProcessedVoiceFrame(
            bytes([index + 1, 0]) * 160,
            16_000,
            0.8,
            True,
        )

    mgr._voice_input_audio_pipeline.process = AsyncMock(side_effect=process)
    first = asyncio.create_task(
        LLMSessionManager._process_microphone_stream_data(
            mgr,
            {"input_type": "audio", "sample_rate_hz": 16_000, "data": [1] * 160},
            ingress_token=token,
        )
    )
    second = asyncio.create_task(
        LLMSessionManager._process_microphone_stream_data(
            mgr,
            {"input_type": "audio", "sample_rate_hz": 16_000, "data": [2] * 160},
            ingress_token=token,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(started[0].wait(), started[1].wait()),
        1,
    )
    flush = asyncio.create_task(LLMSessionManager._flush_hot_swap_audio_cache(mgr))
    await asyncio.sleep(0)

    release[0].set()
    await first
    third = asyncio.create_task(
        LLMSessionManager._process_microphone_stream_data(
            mgr,
            {"input_type": "audio", "sample_rate_hz": 16_000, "data": [3] * 160},
            ingress_token=token,
        )
    )
    await asyncio.wait_for(started[2].wait(), 1)
    assert not flush.done()

    release[1].set()
    await second
    await asyncio.wait_for(flush, 1)
    assert not third.done()
    assert not mgr.is_hot_swap_imminent
    assert not mgr.is_flushing_hot_swap_cache

    release[2].set()
    await asyncio.wait_for(third, 1)
    # The native replay coalesces the two cached frames into one send; the
    # third frame is routed live after the flush completes.
    assert mgr._route_microphone_audio.await_count == 2
    mgr._asr_runtime.abort.assert_not_awaited()


async def test_hot_swap_overflow_blocks_whole_candidate():
    mgr = _make_routable_audio_manager(True)
    token = _queue_token()
    mgr.is_hot_swap_imminent = True
    mgr._ingress_token_matches = MagicMock(return_value=True)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=10)
    assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(token))

    await LLMSessionManager._process_microphone_stream_data(
        mgr,
        {"input_type": "audio", "sample_rate_hz": 16_000, "data": [1] * 160},
        ingress_token=token,
    )

    assert not mgr.hot_swap_audio_cache
    mgr._asr_runtime.abort.assert_awaited_once_with("ingress_backpressure")
    mgr._route_microphone_audio.assert_not_awaited()
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()


async def test_hot_swap_flush_orders_inflight_live_then_cached_audio():
    mgr = _make_routable_audio_manager(True)
    token = _queue_token()
    mgr._ingress_token_matches = MagicMock(return_value=True)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    assert mgr.hot_swap_audio_cache.append(
        HotSwapAudioFrame(pcm16=b"\x10\x00" * 160, token=token)
    )
    mgr._voice_input_audio_pipeline.process = AsyncMock(
        side_effect=[
            ProcessedVoiceFrame(b"\x20\x00" * 160, 16_000, 0.8, True),
            ProcessedVoiceFrame(b"\x30\x00" * 160, 16_000, 0.8, True),
        ]
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    routed: list[bytes] = []

    async def route(pcm16: bytes, **_kwargs) -> bool:
        routed.append(pcm16)
        if pcm16.startswith(b"\x20\x00"):
            first_started.set()
            await release_first.wait()
        return True

    mgr._route_microphone_audio = AsyncMock(side_effect=route)
    first = asyncio.create_task(
        LLMSessionManager._process_microphone_stream_data(
            mgr,
            {"input_type": "audio", "sample_rate_hz": 16_000, "data": [1] * 160},
            ingress_token=token,
        )
    )
    await asyncio.wait_for(first_started.wait(), 1)
    flush = asyncio.create_task(LLMSessionManager._flush_hot_swap_audio_cache(mgr))
    await asyncio.sleep(0)
    second = asyncio.create_task(
        LLMSessionManager._process_microphone_stream_data(
            mgr,
            {"input_type": "audio", "sample_rate_hz": 16_000, "data": [2] * 160},
            ingress_token=token,
        )
    )
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.wait_for(asyncio.gather(first, second, flush), 1)

    # The inflight live frame is routed first; the cached frames follow in
    # order (the native replay may coalesce them into a single send).
    assert routed[0][:2] == b"\x20\x00"
    assert b"".join(routed) == (
        b"\x20\x00" * 160 + b"\x10\x00" * 160 + b"\x30\x00" * 160
    )
    mgr._asr_runtime.abort.assert_not_awaited()
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()
    assert not mgr.hot_swap_audio_cache


async def test_hot_swap_mid_batch_failure_invalidates_candidate():
    mgr = _make_routable_audio_manager(True)
    token = _queue_token()
    mgr._ingress_token_matches = MagicMock(return_value=True)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    assert mgr.hot_swap_audio_cache.append(
        HotSwapAudioFrame(pcm16=b"\x10\x00" * 160, token=token)
    )
    assert mgr.hot_swap_audio_cache.append(
        HotSwapAudioFrame(pcm16=b"\x20\x00" * 160, token=token)
    )
    mgr._route_microphone_audio = AsyncMock(side_effect=RuntimeError("route failed"))

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    mgr._route_microphone_audio.assert_awaited_once()
    mgr._asr_runtime.abort.assert_awaited_once_with("ingress_backpressure")
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()
    assert not mgr.hot_swap_audio_cache
    assert mgr.is_flushing_hot_swap_cache is False


class _FakeClock:
    """Module-local stand-in for ``time`` inside ``core_asr_runtime_module``."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


async def test_hot_swap_flush_hands_off_sustained_arrival_without_abort(
    monkeypatch,
):
    mgr = _make_routable_audio_manager(True)
    del mgr._route_microphone_audio
    token = mgr._capture_ingress_token()
    mgr._asr_runtime.abort = AsyncMock()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    for _ in range(40):
        assert mgr.hot_swap_audio_cache.append(
            HotSwapAudioFrame(pcm16=b"\x01\x00" * 160, token=token)
        )
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def paced_arrival_sleep(delay: float) -> None:
        # Sustained live ingress: ~2 frames arrive during each 25 ms pacing
        # gap, so a paced drain settles at a small steady state instead of
        # converging to empty on its own. A bare yield is not a pacing gap
        # (see degraded_sleep) and must not manufacture frames.
        if delay <= 0:
            await real_sleep(0)
            return
        sleeps.append(delay)
        for _ in range(2):
            assert mgr.hot_swap_audio_cache.append(
                HotSwapAudioFrame(pcm16=b"\x01\x00" * 160, token=token)
            )
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", paced_arrival_sleep)

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    # One paced pass (40 frames, 8 batches) plus the unpaced tail handoff
    # (16 frames that arrived during pacing); nothing is damaged.
    assert sleeps == [0.025] * 8
    sent = b"".join(
        call.args[0] for call in mgr.session.stream_audio.await_args_list
    )
    assert sent == b"\x01\x00" * 160 * 56
    mgr._asr_runtime.abort.assert_not_awaited()
    assert not mgr.hot_swap_audio_cache
    assert mgr.is_flushing_hot_swap_cache is False
    assert mgr.is_hot_swap_imminent is False


async def test_hot_swap_flush_deadline_invalidates_non_converging_replay(
    monkeypatch,
):
    mgr = _make_routable_audio_manager(True)
    token = _queue_token()
    mgr._ingress_token_matches = MagicMock(return_value=True)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    for _ in range(40):
        assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(token))
    clock = _FakeClock()
    monkeypatch.setattr(core_asr_runtime_module, "time", clock)
    real_sleep = asyncio.sleep

    async def degraded_sleep(delay: float) -> None:
        # Ingress matches the replay rate exactly (5 frames per 25 ms
        # pacing gap), so the backlog never shrinks below the handoff
        # threshold: genuine backpressure, not a healthy steady state.
        # Only a real pacing gap admits live ingress -- a bare
        # ``asyncio.sleep(0)`` yield (the ordering tick inside
        # _invalidate_interrupted_voice_turn) is not wall-clock time and must
        # not manufacture frames, or the post-invalidation cache assertions
        # below would measure the harness instead of the product.
        if delay <= 0:
            await real_sleep(0)
            return
        clock.now += delay
        for _ in range(5):
            assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(token))
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", degraded_sleep)

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    mgr._asr_runtime.abort.assert_awaited_once_with("ingress_backpressure")
    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()
    assert not mgr.hot_swap_audio_cache
    assert mgr.is_flushing_hot_swap_cache is False
    assert mgr.is_hot_swap_imminent is False
    # Codex P2: the native route's own invalidation is the input-buffer clear.
    mgr.session.clear_audio_buffer.assert_awaited_once()


async def test_native_route_skips_send_after_fatal_error_with_rate_limited_log(
    monkeypatch,
):
    mgr = _make_routable_audio_manager(True)
    mgr.session._fatal_error_occurred = True
    token = mgr._capture_ingress_token()
    log_warning = MagicMock()
    monkeypatch.setattr(core_asr_runtime_module.logger, "warning", log_warning)

    await LLMSessionManager._route_microphone_audio(
        mgr,
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        ingress_token=token,
    )
    await LLMSessionManager._route_microphone_audio(
        mgr,
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        ingress_token=token,
    )

    mgr.session.stream_audio.assert_not_awaited()
    mgr._record_omni_microphone_audio.assert_not_called()
    log_warning.assert_called_once()


async def test_hot_swap_flush_batches_and_paces_native_replay(monkeypatch):
    mgr = _make_routable_audio_manager(True)
    del mgr._route_microphone_audio
    token = mgr._capture_ingress_token()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    # 27 frames (270 ms) exceeds the 250 ms tail-handoff threshold, so the
    # first pass replays with batching and pacing.
    for _ in range(27):
        assert mgr.hot_swap_audio_cache.append(
            HotSwapAudioFrame(pcm16=b"\x01\x00" * 160, token=token)
        )
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    sent = [call.args[0] for call in mgr.session.stream_audio.await_args_list]
    assert sent == [b"\x01\x00" * 160 * 5] * 5 + [b"\x01\x00" * 160 * 2]
    assert sleeps == [0.025] * 6
    assert not mgr.hot_swap_audio_cache


async def test_hot_swap_flush_bursts_small_tail_without_pacing(monkeypatch):
    mgr = _make_routable_audio_manager(True)
    del mgr._route_microphone_audio
    token = mgr._capture_ingress_token()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    for _ in range(7):
        assert mgr.hot_swap_audio_cache.append(
            HotSwapAudioFrame(pcm16=b"\x01\x00" * 160, token=token)
        )
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    sent = [call.args[0] for call in mgr.session.stream_audio.await_args_list]
    assert sent == [b"\x01\x00" * 160 * 5, b"\x01\x00" * 160 * 2]
    assert sleeps == []
    assert not mgr.hot_swap_audio_cache


async def test_hot_swap_flush_counts_and_logs_unrebindable_frames(monkeypatch):
    mgr = _make_routable_audio_manager(True)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(_queue_token()))
    assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(_queue_token()))
    log_warning = MagicMock()
    monkeypatch.setattr(core_asr_runtime_module.logger, "warning", log_warning)

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    assert mgr._audio_stream_dropped_total == 2
    log_warning.assert_called_once()
    mgr._route_microphone_audio.assert_not_awaited()
    mgr._asr_runtime.abort.assert_not_awaited()
    assert not mgr.hot_swap_audio_cache


async def test_hot_swap_flush_aborts_once_for_multiple_damaged_tokens():
    mgr = _make_routable_audio_manager(True)
    mgr._ingress_token_matches = MagicMock(return_value=True)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    token_a = VoiceIngressToken(1, "socket", 1, 1, 1)
    token_b = VoiceIngressToken(1, "socket", 1, 2, 1)
    assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(token_a))
    assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(token_b))
    mgr._route_microphone_audio = AsyncMock(side_effect=RuntimeError("route failed"))

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    mgr._route_microphone_audio.assert_awaited_once()
    mgr._asr_runtime.abort.assert_awaited_once_with("ingress_backpressure")
    assert not mgr.hot_swap_audio_cache


async def test_slow_runtime_abort_does_not_block_enqueue_processing():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    _authorize_core_lease(mgr)
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=1,
    )
    mgr._audio_stream_dropped_total = 0
    mgr._last_audio_stream_backlog_log_time = 0.0
    mgr._ensure_audio_stream_worker = lambda: None
    mgr._bg_tasks = set()
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def slow_abort(_reason: str) -> None:
        abort_started.set()
        await release_abort.wait()

    mgr._asr_runtime.abort = AsyncMock(side_effect=slow_abort)
    message = {
        "input_type": "audio",
        "sample_rate_hz": 16_000,
        "data": [1] * 160,
    }

    await LLMSessionManager._enqueue_audio_stream_data(mgr, message)
    # Overflow: the teardown is scheduled off the receive path, so this call
    # returns without waiting for the slow provider close.
    await LLMSessionManager._enqueue_audio_stream_data(mgr, message)
    assert mgr._audio_stream_queue.empty()
    await asyncio.wait_for(abort_started.wait(), 1)
    assert not release_abort.is_set()

    # A later frame is accepted while the abort is still pending.
    await LLMSessionManager._enqueue_audio_stream_data(mgr, message)
    assert mgr._audio_stream_queue.qsize() == 1

    release_abort.set()
    await asyncio.gather(*list(mgr._bg_tasks))
    mgr._asr_runtime.abort.assert_awaited_once_with("ingress_backpressure")


# ---------------------------------------------------------------------------
# Empty/rejected final -> explicit preview clear (Codex P2)
#
# A provider can stream partials and then complete the turn with an EMPTY
# final (e.g. the OpenAI/Step stalled-item timeouts). Core deliberately
# injects no user_transcript for empty text, but user_transcript was the only
# per-turn frontend message that removed the streaming preview bubble, so it
# lingered indefinitely. The dispatch path must send the reused
# user_transcript_preview message with empty text as an explicit clear.
# ---------------------------------------------------------------------------


def _make_transcript_dispatch_manager() -> LLMSessionManager:
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr._init_asr_runtime_state()
    _authorize_core_lease(mgr)
    mgr.lanlan_name = "Test"
    session = MagicMock()
    session.create_response = AsyncMock()
    session.prepare_external_voice_turn = AsyncMock()
    session.submit_external_voice_turn = AsyncMock()
    session.abandon_external_voice_turn = MagicMock()
    mgr.session = session
    mgr.handle_input_transcript = AsyncMock(return_value=True)
    mgr.handle_new_message = AsyncMock()
    mgr.websocket = MagicMock()
    mgr.websocket.send_json = AsyncMock()
    return mgr


def _transcript_event(mgr: LLMSessionManager, text: str, turn_id: int = 7):
    token = mgr._capture_ingress_token()
    return VoiceTranscriptEvent(
        turn_token=VoiceTurnToken(ingress=token, turn_id=turn_id),
        provider="qwen",
        text=text,
    )


def _preview_clear_payload(mgr: LLMSessionManager, turn_id: int = 7) -> dict:
    epoch = mgr._capture_ingress_token().session_epoch
    external_turn_id = f"asr-{epoch}-{turn_id}"
    return {
        "type": "user_transcript_preview",
        "text": "",
        "turn_id": external_turn_id,
        "asr_turn_id": external_turn_id,
    }


async def test_empty_asr_final_sends_preview_clear_and_skips_injection():
    mgr = _make_transcript_dispatch_manager()
    event = _transcript_event(mgr, "   ")

    await mgr._dispatch_core_asr_transcript(event)

    mgr.websocket.send_json.assert_awaited_once_with(_preview_clear_payload(mgr))
    mgr.handle_input_transcript.assert_not_awaited()
    mgr.session.submit_external_voice_turn.assert_not_awaited()
    mgr.session.create_response.assert_not_awaited()


async def test_non_empty_asr_final_injects_without_preview_clear():
    # Negative validation: a real transcript must go through injection and
    # must NOT emit the empty-text clear (that would race the user_transcript
    # bubble replacement the frontend performs on its own).
    mgr = _make_transcript_dispatch_manager()
    event = _transcript_event(mgr, "hello", turn_id=8)

    await mgr._dispatch_core_asr_transcript(event)

    mgr.websocket.send_json.assert_not_awaited()
    mgr.handle_input_transcript.assert_awaited_once()
    epoch = mgr._capture_ingress_token().session_epoch
    mgr.session.submit_external_voice_turn.assert_awaited_once_with(
        "hello",
        turn_id=f"asr-{epoch}-8",
    )


async def test_rejected_asr_final_sends_preview_clear():
    # Echo suppression / takeover routing reject the text
    # (handle_input_transcript -> False) and also never emit user_transcript;
    # the preview must be cleared there too.
    mgr = _make_transcript_dispatch_manager()
    mgr.handle_input_transcript = AsyncMock(return_value=False)
    event = _transcript_event(mgr, "hello again")

    await mgr._dispatch_core_asr_transcript(event)

    mgr.websocket.send_json.assert_awaited_once_with(_preview_clear_payload(mgr))
    mgr.session.submit_external_voice_turn.assert_not_awaited()
    mgr.session.create_response.assert_not_awaited()


async def test_rejected_final_after_runtime_moved_on_sends_no_clear():
    # Negative validation (identity guard): when the runtime identity changed
    # while the transcript was being handled, a newer turn may already own
    # the preview bubble -- the stale rejection must NOT clear it.
    mgr = _make_transcript_dispatch_manager()
    expected_clear = _preview_clear_payload(mgr)

    async def _reject_and_swap_session(*args, **kwargs):
        mgr.session = MagicMock()
        mgr.session.abandon_external_voice_turn = MagicMock()
        return False

    mgr.handle_input_transcript = AsyncMock(side_effect=_reject_and_swap_session)
    event = _transcript_event(mgr, "stale text")

    await mgr._dispatch_core_asr_transcript(event)

    mgr.websocket.send_json.assert_not_awaited()
    assert expected_clear  # payload helper stays usable for the positive twin


async def test_game_takeover_clears_core_preview_and_empty_final_stays_terminal(
    monkeypatch,
):
    mgr = _make_transcript_dispatch_manager()
    mgr._set_microphone_route("independent")
    route_transcript = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "main_logic.voice_input.consumers.game.is_game_route_active",
        lambda _name: True,
    )
    monkeypatch.setattr(
        "main_logic.voice_input.consumers.game.get_active_game_route_identity",
        lambda _name: ("game", "session-a"),
    )
    monkeypatch.setattr(
        "main_logic.voice_input.consumers.game.route_external_voice_transcript",
        route_transcript,
    )
    core_turn = VoiceTurnToken(ingress=mgr._capture_ingress_token(), turn_id=7)
    assert await mgr._prepare_voice_input_turn(core_turn) is True
    await mgr._dispatch_voice_input_partial(
        VoicePartialEvent(turn_token=core_turn, text="go"),
    )
    mgr.websocket.send_json.reset_mock()

    await mgr._apply_voice_lease_state(
        owner="game",
        hard_muted=False,
        focus_suppressed=False,
        reason="game_takeover",
        force_abort=True,
    )

    mgr.websocket.send_json.assert_awaited_once_with(_preview_clear_payload(mgr))
    route_transcript.assert_not_awaited()

    mgr.websocket.send_json.reset_mock()
    empty = _transcript_event(mgr, "  ", turn_id=9)
    assert await mgr._prepare_voice_input_turn(empty.turn_token) is True
    await mgr._dispatch_voice_input_final(empty)
    await mgr._voice_input_registry.wait_idle()
    route_transcript.assert_not_awaited()
    mgr.websocket.send_json.assert_not_awaited()

    non_empty = _transcript_event(mgr, "go left", turn_id=10)
    assert await mgr._prepare_voice_input_turn(non_empty.turn_token) is True
    await mgr._dispatch_voice_input_final(non_empty)
    route_transcript.assert_awaited_once_with(
        "Test",
        "go left",
        request_id=(
            f"asr-{non_empty.turn_token.ingress.session_epoch}-"
            f"{non_empty.turn_token.turn_id}"
        ),
        game_type="game",
        session_id="session-a",
    )
    mgr.websocket.send_json.assert_not_awaited()


async def test_preview_clear_send_failure_is_swallowed():
    # The clear rides the on_final dispatch; a websocket hiccup must not
    # surface as an injection failure for a turn Core intentionally dropped.
    mgr = _make_transcript_dispatch_manager()
    mgr.websocket.send_json = AsyncMock(side_effect=RuntimeError("socket gone"))
    event = _transcript_event(mgr, "")

    await mgr._dispatch_core_asr_transcript(event)

    mgr.handle_input_transcript.assert_not_awaited()
    mgr.session.submit_external_voice_turn.assert_not_awaited()


# ---------------------------------------------------------------------------
# Turn-keyed preview bubble (Codex P2, second dispatcher boundary)
#
# Finals reach Core on the TranscriptDispatcher's own worker task while
# _handle_independent_asr_final already activated the pending turn, so a
# previous turn's on_final can trail the NEXT turn's partials on the ordered
# websocket. Both frontend removal paths used to erase the singleton preview
# unconditionally, wiping the newer turn's bubble. Previews are now stamped
# with the prepared turn id so a stale clear is a frontend no-op, and the
# identity-free user_transcript path is repaired by re-sending the newer
# turn's preview right behind the transcript.
# ---------------------------------------------------------------------------


def _make_preview_dispatch_manager() -> LLMSessionManager:
    mgr = _make_transcript_dispatch_manager()
    mgr._set_microphone_route("independent")
    return mgr


async def _prepare_preview_turn(mgr: LLMSessionManager, turn_id: int) -> str:
    token = mgr._capture_ingress_token()
    accepted = await mgr._prepare_core_voice_turn(
        VoiceTurnToken(ingress=token, turn_id=turn_id)
    )
    assert accepted
    return f"asr-{token.session_epoch}-{turn_id}"


async def _send_preview_partial(mgr: LLMSessionManager, text: str) -> dict:
    turn_token = getattr(mgr, "_core_asr_preview_turn_token", None)
    if turn_token is None:
        turn_token = VoiceTurnToken(
            ingress=mgr._capture_ingress_token(),
            turn_id=0,
        )
    await mgr._send_core_asr_preview(
        VoicePartialEvent(
            turn_token=turn_token,
            text=text,
        )
    )
    return mgr.websocket.send_json.await_args.args[0]


async def test_preview_partial_and_clear_share_the_prepared_turn_id():
    mgr = _make_preview_dispatch_manager()
    external_turn_id = await _prepare_preview_turn(mgr, 7)

    partial = await _send_preview_partial(mgr, "hello")
    assert partial["asr_turn_id"] == external_turn_id
    mgr.websocket.send_json.reset_mock()

    # The turn's own empty final still clears its own bubble (matching id).
    await mgr._dispatch_core_asr_transcript(_transcript_event(mgr, "  ", turn_id=7))

    mgr.websocket.send_json.assert_awaited_once_with(_preview_clear_payload(mgr, 7))


async def test_preview_without_prepared_turn_stays_unkeyed():
    # Backward compat: no prepared turn -> no asr_turn_id, which keeps the
    # frontend on the pre-existing unconditional removal path.
    mgr = _make_preview_dispatch_manager()

    partial = await _send_preview_partial(mgr, "hello")

    assert "asr_turn_id" not in partial
    assert partial["turn_id"]


async def test_stale_empty_final_clear_does_not_target_the_newer_turn():
    # Negative validation: the delayed turn-7 clear must carry turn 7, not the
    # turn-8 bubble now on screen, so the frontend ignores it.
    mgr = _make_preview_dispatch_manager()
    old_turn_id = await _prepare_preview_turn(mgr, 7)
    await _send_preview_partial(mgr, "old text")
    new_turn_id = await _prepare_preview_turn(mgr, 8)
    displayed = await _send_preview_partial(mgr, "new text")
    assert displayed["asr_turn_id"] == new_turn_id
    mgr.websocket.send_json.reset_mock()

    await mgr._dispatch_core_asr_transcript(_transcript_event(mgr, "", turn_id=7))

    clear = mgr.websocket.send_json.await_args.args[0]
    assert clear["text"] == ""
    assert clear["asr_turn_id"] == old_turn_id
    assert clear["asr_turn_id"] != displayed["asr_turn_id"]


async def test_late_accepted_final_restores_the_newer_turn_preview():
    # The accepted final's user_transcript carries no turn identity, so the
    # frontend removes whatever bubble is on screen; Core re-sends the newer
    # turn's preview behind it instead of waiting for the next partial.
    mgr = _make_preview_dispatch_manager()
    await _prepare_preview_turn(mgr, 7)
    await _send_preview_partial(mgr, "old text")
    new_turn_id = await _prepare_preview_turn(mgr, 8)
    await _send_preview_partial(mgr, "new text")
    mgr.websocket.send_json.reset_mock()

    await mgr._dispatch_core_asr_transcript(
        _transcript_event(mgr, "old text", turn_id=7)
    )

    mgr.handle_input_transcript.assert_awaited_once()
    assert mgr.websocket.send_json.await_count == 1
    restored = mgr.websocket.send_json.await_args.args[0]
    assert restored["text"] == "new text"
    assert restored["asr_turn_id"] == new_turn_id
    mgr.session.submit_external_voice_turn.assert_awaited_once()


async def test_final_of_the_displayed_turn_sends_no_restore_preview():
    # Negative validation: the normal single-turn flow is untouched -- the
    # owning turn's user_transcript is the correct bubble removal, so Core
    # must not re-send anything behind it.
    mgr = _make_preview_dispatch_manager()
    await _prepare_preview_turn(mgr, 7)
    await _send_preview_partial(mgr, "hello")
    mgr.websocket.send_json.reset_mock()

    await mgr._dispatch_core_asr_transcript(
        _transcript_event(mgr, "hello", turn_id=7)
    )

    mgr.websocket.send_json.assert_not_awaited()
    mgr.session.submit_external_voice_turn.assert_awaited_once()


async def test_restore_skipped_when_the_newer_turn_has_no_preview_yet():
    # Negative validation: a prepared-but-silent newer turn owns no bubble,
    # so the late final must not resurrect the previous turn's text.
    mgr = _make_preview_dispatch_manager()
    await _prepare_preview_turn(mgr, 7)
    await _send_preview_partial(mgr, "old text")
    await _prepare_preview_turn(mgr, 8)
    mgr.websocket.send_json.reset_mock()

    await mgr._dispatch_core_asr_transcript(
        _transcript_event(mgr, "old text", turn_id=7)
    )

    mgr.websocket.send_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# Repair freshness (Codex P2 follow-up on the repair itself)
#
# handle_input_transcript is awaited, so the newer turn keeps streaming --
# possibly handing the bubble to a turn newer still -- while the injection is
# in flight. The repair therefore reads the owning turn id and its text at
# restore time; a pre-await snapshot would re-send stale text and, worse,
# write it back into the cache, regressing the visible preview until the next
# partial (permanently if none follows).
# ---------------------------------------------------------------------------


def _sent_preview_payloads(mgr: LLMSessionManager) -> list[dict]:
    return [call.args[0] for call in mgr.websocket.send_json.await_args_list]


async def test_restore_resends_the_partial_that_landed_during_injection():
    mgr = _make_preview_dispatch_manager()
    await _prepare_preview_turn(mgr, 7)
    await _send_preview_partial(mgr, "old text")
    new_turn_id = await _prepare_preview_turn(mgr, 8)
    await _send_preview_partial(mgr, "new")

    async def _inject_then_stream(*args, **kwargs):
        await _send_preview_partial(mgr, "new text")
        return True

    mgr.handle_input_transcript = AsyncMock(side_effect=_inject_then_stream)
    mgr.websocket.send_json.reset_mock()

    await mgr._dispatch_core_asr_transcript(
        _transcript_event(mgr, "old text", turn_id=7)
    )

    payloads = _sent_preview_payloads(mgr)
    # The in-flight partial, then the repair mirroring it -- never "new".
    assert [payload["text"] for payload in payloads] == ["new text", "new text"]
    assert payloads[-1]["asr_turn_id"] == new_turn_id
    # Negative validation: the repair must not push its own copy back in.
    assert mgr._core_asr_preview_text == "new text"
    mgr.session.submit_external_voice_turn.assert_awaited_once()


async def test_restore_follows_a_turn_handover_during_injection():
    # The bubble can change owner mid-injection; the repair belongs to
    # whichever turn owns it when the transcript actually went out.
    mgr = _make_preview_dispatch_manager()
    epoch = mgr._capture_ingress_token().session_epoch
    await _prepare_preview_turn(mgr, 7)
    await _send_preview_partial(mgr, "old text")
    await _prepare_preview_turn(mgr, 8)
    await _send_preview_partial(mgr, "new text")

    async def _inject_then_hand_over(*args, **kwargs):
        await _prepare_preview_turn(mgr, 9)
        await _send_preview_partial(mgr, "newest text")
        return True

    mgr.handle_input_transcript = AsyncMock(side_effect=_inject_then_hand_over)
    mgr.websocket.send_json.reset_mock()

    await mgr._dispatch_core_asr_transcript(
        _transcript_event(mgr, "old text", turn_id=7)
    )

    payloads = _sent_preview_payloads(mgr)
    # Turn 9's own partial, then the repair behind the transcript -- a repair
    # keyed to the pre-await owner would have been skipped outright here.
    assert [payload["text"] for payload in payloads] == ["newest text"] * 2
    assert payloads[-1]["asr_turn_id"] == f"asr-{epoch}-9"
    assert mgr._core_asr_preview_text == "newest text"


async def test_restore_skipped_when_the_newer_preview_cleared_during_injection():
    # Negative validation: the newer turn's bubble was legitimately cleared
    # while the injection was in flight, so there is nothing to repair and the
    # cleared text must not be resurrected.
    mgr = _make_preview_dispatch_manager()
    await _prepare_preview_turn(mgr, 7)
    await _send_preview_partial(mgr, "old text")
    new_turn_id = await _prepare_preview_turn(mgr, 8)
    await _send_preview_partial(mgr, "new text")

    async def _inject_then_clear(*args, **kwargs):
        await mgr._send_core_asr_preview_clear(new_turn_id)
        return True

    mgr.handle_input_transcript = AsyncMock(side_effect=_inject_then_clear)
    mgr.websocket.send_json.reset_mock()

    await mgr._dispatch_core_asr_transcript(
        _transcript_event(mgr, "old text", turn_id=7)
    )

    assert [payload["text"] for payload in _sent_preview_payloads(mgr)] == [""]
    assert mgr._core_asr_preview_text == ""


async def test_native_route_overflow_clears_provider_input_buffer():
    # Codex P2. A native turn is segmented by the provider's server VAD over
    # one continuously appended input buffer, so a multi-second ingress hole is
    # invisible to it: speech from both sides of the discarded interval gets
    # concatenated into one wrong transcript. The independent abort fired here
    # owns nothing on the native route (no lifecycle, no provider session), so
    # the buffer clear is the only thing that invalidates the broken turn.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr._set_microphone_route("native")
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=2,
    )
    mgr._audio_stream_dropped_total = 0
    mgr._last_audio_stream_backlog_log_time = 0.0
    mgr._ensure_audio_stream_worker = lambda: None
    mgr._bg_tasks = set()
    mgr._asr_runtime.abort = AsyncMock()

    def message(seq: int) -> dict:
        return {
            "seq": seq,
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [seq] * 160,
        }

    for seq in (1, 2, 3):
        await LLMSessionManager._enqueue_audio_stream_data(mgr, message(seq))
    await asyncio.gather(*list(mgr._bg_tasks))

    assert mgr._audio_stream_queue.empty()
    assert mgr._audio_stream_dropped_total == 3
    assert mgr._asr_route_mode == "native"
    mgr.session.clear_audio_buffer.assert_awaited_once()


async def test_independent_route_overflow_does_not_clear_provider_input_buffer():
    # Route-dispatch guard (not a fail-before test): the independent route owns
    # its own invalidation via IndependentAsrRuntime.abort, so the helper must
    # not degrade into an unconditional clear of the Omni input buffer.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr._set_microphone_route("independent")
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=2,
    )
    mgr._audio_stream_dropped_total = 0
    mgr._last_audio_stream_backlog_log_time = 0.0
    mgr._ensure_audio_stream_worker = lambda: None
    mgr._bg_tasks = set()
    mgr._asr_runtime.abort = AsyncMock()

    for seq in (1, 2, 3):
        await LLMSessionManager._enqueue_audio_stream_data(
            mgr,
            {
                "seq": seq,
                "input_type": "audio",
                "sample_rate_hz": 16_000,
                "data": [seq] * 160,
            },
        )
    await asyncio.gather(*list(mgr._bg_tasks))

    mgr._asr_runtime.abort.assert_awaited_once_with("ingress_backpressure")
    mgr.session.clear_audio_buffer.assert_not_awaited()


async def test_hot_swap_flush_damaged_tail_clears_native_input_buffer():
    # The flush replays the cache into the POST-swap session and explicitly
    # supports the native route. A send failure drops the whole remaining tail
    # into damaged_frames, so the same PCM hole opens against the new session.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr._set_microphone_route("native")
    token = _queue_token()
    mgr._ingress_token_matches = MagicMock(return_value=True)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.hot_swap_audio_cache = HotSwapAudioBuffer(capacity_ms=8_000)
    for _ in range(3):
        assert mgr.hot_swap_audio_cache.append(_hot_swap_frame(token))
    mgr._route_microphone_audio = AsyncMock(side_effect=RuntimeError("send failed"))

    await LLMSessionManager._flush_hot_swap_audio_cache(mgr)

    mgr._asr_runtime.abort.assert_awaited_once_with("ingress_backpressure")
    mgr.session.clear_audio_buffer.assert_awaited_once()


async def test_revoke_voice_input_connection_releases_lease_and_aborts_turn():
    # Codex P2 primitive. A recording socket that dies while a newer chat
    # socket is current gets no manager-side teardown at all, so this is the
    # only thing that releases its voice turn.
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._asr_runtime.abort = AsyncMock()
    mgr.session = None

    assert await mgr._revoke_voice_input_connection("socket-a") is True

    assert mgr._voice_lease_connection_id == ""
    assert mgr._voice_lease_owner == "none"
    assert mgr._voice_lease_synchronized is False
    assert mgr._voice_lease_control_seen is False
    assert mgr._voice_input_accepts_pcm() is False
    mgr._asr_runtime.abort.assert_awaited_once()


async def test_revoke_voice_input_connection_ignores_a_superseded_socket():
    # A socket that already lost the identity to a newer claim must never
    # clear the winner's lease.
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    mgr._begin_voice_input_connection("socket-a")
    mgr._begin_voice_input_connection("socket-b")
    _authorize_core_lease(mgr)
    mgr._asr_runtime.abort = AsyncMock()

    assert await mgr._revoke_voice_input_connection("socket-a") is False
    assert await mgr._revoke_voice_input_connection("") is False

    assert mgr._voice_lease_connection_id == "socket-b"
    assert mgr._voice_lease_owner == "core"
    mgr._asr_runtime.abort.assert_not_awaited()


# Literal bounds, matching the binary decoder's parametrization in
# tests/unit/test_websocket_binary_audio.py: both ingress paths must reject at
# the same DURATION, so the 16 kHz limit is 1920 samples and 48 kHz is 5760.
# Deriving them from the constant would make the test pass against any value.
@pytest.mark.parametrize(
    "sample_rate_hz, samples",
    [(16_000, 1_921), (48_000, 5_761)],
)
async def test_json_mic_frame_rejects_an_oversized_sample_list(
    sample_rate_hz: int,
    samples: int,
):
    # The JSON stream_data branch materializes the same int list as the binary
    # decoder but had no per-frame bound: the queue's 2 s / 256-frame limits are
    # post-decode. The frontend never sends JSON audio, so any oversized frame
    # is malformed by construction.
    with pytest.raises(ValueError, match="MIC_PCM_FRAME_TOO_LONG"):
        QueuedMicFrame.from_message(
            {
                "input_type": "audio",
                "sample_rate_hz": sample_rate_hz,
                "data": [0] * samples,
            },
            token=_queue_token(),
        )


@pytest.mark.parametrize(
    "sample_rate_hz, samples",
    [(16_000, 512), (48_000, 480), (16_000, 1_920), (48_000, 5_760)],
)
async def test_json_mic_frame_accepts_real_and_boundary_frame_sizes(
    sample_rate_hz: int,
    samples: int,
):
    frame = QueuedMicFrame.from_message(
        {
            "input_type": "audio",
            "sample_rate_hz": sample_rate_hz,
            "data": [0] * samples,
        },
        token=_queue_token(),
    )
    assert frame.source_rate_hz == sample_rate_hz


async def test_text_session_microphone_is_signalled_once_not_silently_dropped():
    # PR #2345 removed streaming.py's audio-branch rebuild. The mic lease is
    # frontend-owned and no session-lifecycle path resets it, while a text
    # session pins the route to "blocked" — so a client that keeps recording
    # had every frame accepted at ingress and dropped at routing with no
    # signal and no recovery. The current frontend stops the mic itself on
    # session_started(input_mode='text'); this is the fallback for older and
    # third-party clients.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr.input_mode = "text"
    mgr._set_microphone_route("blocked")
    mgr.send_status = AsyncMock()
    del mgr._route_microphone_audio

    for _ in range(3):
        await LLMSessionManager._route_microphone_audio(
            mgr, b"\x01\x00" * 160, sample_rate_hz=16_000
        )

    mgr.send_status.assert_awaited_once()
    payload = json.loads(mgr.send_status.await_args.args[0])
    assert payload["code"] == "VOICE_INPUT_BLOCKED_TEXT_SESSION"

    # Re-arms for the next text-mode episode once the route recovers.
    mgr._set_microphone_route("native")
    mgr._set_microphone_route("blocked")
    await LLMSessionManager._route_microphone_audio(
        mgr, b"\x01\x00" * 160, sample_rate_hz=16_000
    )
    assert mgr.send_status.await_count == 2


async def test_audio_session_microphone_block_stays_silent():
    # Only a text-mode session gets the notice: a blocked audio session is a
    # provider failure that already has its own ASR_* status.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr.input_mode = "audio"
    mgr._set_microphone_route("blocked")
    mgr.send_status = AsyncMock()
    del mgr._route_microphone_audio

    await LLMSessionManager._route_microphone_audio(
        mgr, b"\x01\x00" * 160, sample_rate_hz=16_000
    )

    mgr.send_status.assert_not_awaited()


async def test_overflow_abort_prefix_lands_before_ingress_accepts_another_frame():
    # CodeRabbit: the overflow branch dispatches the invalidation with
    # _fire_task and yields ONE tick so the abort's synchronous prefix (the
    # audio-generation bump inside IndependentAsrRuntime._abort_transport)
    # runs before _enqueue_audio_stream_data returns. Creating the abort task
    # inside _invalidate_interrupted_voice_turn instead puts it one scheduling
    # layer deeper, so this coroutine resumes first and the ordering is lost.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr._set_microphone_route("independent")
    mgr._audio_stream_queue = AudioDurationQueue(
        capacity_us=1_000_000,
        max_frames=2,
    )
    mgr._audio_stream_dropped_total = 0
    mgr._last_audio_stream_backlog_log_time = 0.0
    mgr._ensure_audio_stream_worker = lambda: None
    mgr._bg_tasks = set()

    aborted = asyncio.Event()

    async def _abort(_reason: str) -> None:
        # Stands in for the real abort's synchronous prefix: everything up to
        # IndependentAsrRuntime.abort's first await runs on the task's first
        # step. The sleep models its first real suspension (a frontend status
        # send under the same congestion that caused the overflow).
        aborted.set()
        await asyncio.sleep(0.05)

    mgr._abort_independent_asr = _abort

    for seq in (1, 2, 3):
        await LLMSessionManager._enqueue_audio_stream_data(
            mgr,
            {
                "seq": seq,
                "input_type": "audio",
                "sample_rate_hz": 16_000,
                "data": [seq] * 160,
            },
        )
        if seq == 3:
            # The overflow frame has been rejected and this coroutine has
            # returned: the prefix must already have run, without waiting on
            # the abort's slow tail.
            assert aborted.is_set(), (
                "abort prefix must land before ingress accepts another frame"
            )

    await asyncio.gather(*list(mgr._bg_tasks))


async def test_voice_control_status_reaches_the_lease_holding_socket():
    # Codex P2. send_status targets the manager's CURRENT socket, and
    # sync_message_queue feeds the monitor process on a separate port that no
    # app window connects to -- there is no fan-out. So when a recorder is
    # superseded by a newer chat window, mic control-plane notices (lifecycle
    # BLOCKED, blocked-route notices, lease resync) reached only the window
    # holding no microphone, and the teardown never ran where the hardware was.
    class _LiveState:
        CONNECTED = "connected"

        def __eq__(self, other) -> bool:
            return other == "connected"

    class _Socket:
        def __init__(self) -> None:
            self.client_state = _LiveState()
            self.sent: list[str] = []

        async def send_text(self, data: str) -> None:
            self.sent.append(data)

    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    mgr._begin_voice_input_connection("socket-a")

    recorder = _Socket()
    mgr.websocket = _Socket()
    assert mgr._set_voice_input_websocket("socket-a", recorder) is True

    mgr.send_status = AsyncMock()
    await LLMSessionManager._send_voice_control_status(mgr, '{"code": "X"}')

    # Display plane still goes through send_status (the current socket).
    mgr.send_status.assert_awaited_once()
    # And the mic control plane additionally reaches the recorder.
    assert len(recorder.sent) == 1
    delivered = json.loads(recorder.sent[0])
    assert delivered["type"] == "status"
    assert json.loads(delivered["message"])["code"] == "X"

    # When the lease holder IS the current socket, there is no second send:
    # single-window behaviour must stay bit-identical.
    mgr.websocket = recorder
    mgr.send_status.reset_mock()
    recorder.sent.clear()
    await LLMSessionManager._send_voice_control_status(mgr, '{"code": "Y"}')
    mgr.send_status.assert_awaited_once()
    assert recorder.sent == []


async def test_lease_resync_retries_when_display_and_voice_delivery_fail():
    class _LiveState:
        CONNECTED = "connected"

        def __eq__(self, other) -> bool:
            return other == "connected"

    class _Socket:
        def __init__(self, *, fail: bool) -> None:
            self.client_state = _LiveState()
            self.fail = fail
            self.attempts = 0

        async def send_text(self, _data: str) -> None:
            self.attempts += 1
            if self.fail:
                raise OSError("delivery failed")

    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    mgr._begin_voice_input_connection("socket-a")
    display = _Socket(fail=True)
    voice = _Socket(fail=True)
    mgr.websocket = display
    mgr.sync_message_queue = SimpleNamespace(put=MagicMock())
    assert mgr._set_voice_input_websocket("socket-a", voice) is True

    await LLMSessionManager._maybe_signal_voice_lease_resync(mgr)

    assert mgr._voice_lease_resync_signal_state is None
    assert display.attempts == 1
    assert voice.attempts == 1

    display.fail = False
    voice.fail = False
    await LLMSessionManager._maybe_signal_voice_lease_resync(mgr)

    assert mgr._voice_lease_resync_signal_state is not None
    assert display.attempts == 2
    assert voice.attempts == 2


@pytest.mark.parametrize(
    ("display_fails", "voice_fails", "display_attempts", "voice_attempts"),
    [
        (False, True, 1, 2),
        (True, False, 2, 1),
    ],
)
async def test_blocked_text_notice_retries_only_failed_delivery_plane(
    display_fails: bool,
    voice_fails: bool,
    display_attempts: int,
    voice_attempts: int,
):
    class _LiveState:
        CONNECTED = "connected"

        def __eq__(self, other) -> bool:
            return other == "connected"

    class _Socket:
        def __init__(self, *, fail: bool) -> None:
            self.client_state = _LiveState()
            self.fail = fail
            self.attempts = 0

        async def send_text(self, _data: str) -> None:
            self.attempts += 1
            if self.fail:
                raise OSError("delivery failed")

    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    mgr._begin_voice_input_connection("socket-a")
    mgr.input_mode = "text"
    mgr._set_microphone_route("blocked")
    display = _Socket(fail=display_fails)
    voice = _Socket(fail=voice_fails)
    mgr.websocket = display
    mgr.sync_message_queue = SimpleNamespace(put=MagicMock())
    assert mgr._set_voice_input_websocket("socket-a", voice) is True

    await LLMSessionManager._maybe_signal_blocked_text_mode_microphone(mgr)

    assert mgr._blocked_text_mode_microphone_signal_state is None
    assert display.attempts == 1
    assert voice.attempts == 1

    display.fail = False
    voice.fail = False
    await LLMSessionManager._maybe_signal_blocked_text_mode_microphone(mgr)

    assert mgr._blocked_text_mode_microphone_signal_state is not None
    assert display.attempts == display_attempts
    assert voice.attempts == voice_attempts


async def test_cancelled_voice_notice_retry_does_not_repeat_display_delivery():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    mgr._begin_voice_input_connection("socket-a")
    mgr.input_mode = "text"
    mgr._set_microphone_route("blocked")
    mgr.send_status = AsyncMock(return_value=True)
    voice_owner = object()
    voice_send_started = asyncio.Event()
    voice_attempts = 0

    mgr._voice_owner_socket = lambda: voice_owner

    async def send_to_voice_owner(_payload):
        nonlocal voice_attempts
        voice_attempts += 1
        if voice_attempts == 1:
            voice_send_started.set()
            await asyncio.Event().wait()
        return voice_owner

    mgr._send_to_voice_owner = send_to_voice_owner

    first = asyncio.create_task(
        LLMSessionManager._maybe_signal_blocked_text_mode_microphone(mgr)
    )
    await asyncio.wait_for(voice_send_started.wait(), timeout=1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert mgr._blocked_text_mode_microphone_signal_state is None
    assert mgr.send_status.await_count == 1
    assert voice_attempts == 1

    await LLMSessionManager._maybe_signal_blocked_text_mode_microphone(mgr)

    assert mgr._blocked_text_mode_microphone_signal_state is not None
    assert mgr.send_status.await_count == 1
    assert voice_attempts == 2


async def test_voice_socket_setter_rejects_a_stale_claim():
    mgr = LLMSessionManager.__new__(LLMSessionManager)
    mgr.lanlan_name = "Test"
    mgr._init_asr_runtime_state()
    mgr._begin_voice_input_connection("socket-a")
    mgr._begin_voice_input_connection("socket-b")

    assert mgr._set_voice_input_websocket("socket-a", object()) is False
    assert mgr._voice_input_websocket is None
    assert mgr._set_voice_input_websocket("socket-b", "ws-b") is True
    assert mgr._voice_input_websocket == "ws-b"
    # A later claim must not inherit the previous holder's socket.
    mgr._begin_voice_input_connection("socket-c")
    assert mgr._voice_input_websocket is None


async def test_deliberate_revoke_does_not_ask_the_client_to_resync():
    # The revoke fail-safe stops ingress for clients that never honour the
    # teardown. Without suppression it immediately emits
    # VOICE_INPUT_LEASE_RESYNC_REQUIRED, whose handler makes a still-recording
    # window re-send its lease snapshot and re-establish exactly the lease that
    # was just dropped -- a revoke/resync ping-pong.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._voice_lease_resync_suppressed = True
    mgr._voice_lease_synchronized = False
    mgr.send_status = AsyncMock()

    await LLMSessionManager._maybe_signal_voice_lease_resync(mgr)
    mgr.send_status.assert_not_awaited()

    # A live route re-arms it.
    mgr._set_microphone_route("native")
    assert mgr._voice_lease_resync_suppressed is False


@pytest.mark.parametrize("owner, revoked", [("core", True), ("game", False)])
async def test_startup_failure_revokes_the_lease_except_for_the_game_owner(
    owner: str,
    revoked: bool,
):
    # Codex P2. A startup failure (provider connect, credentials, config) pins
    # the route blocked but can never emit a BLOCKED lifecycle event, so the
    # backstop is the only server-side stop. The galgame route holds the lease
    # through its built-in Registry consumer and must not be collaterally revoked.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._voice_lease_owner = owner
    mgr._set_microphone_route("blocked")
    mgr._asr_runtime.abort = AsyncMock()

    result = await mgr._revoke_lease_for_blocked_route("asr_start_failed")

    assert result is revoked
    assert (mgr._voice_lease_connection_id == "") is revoked
    if revoked:
        # And it must not immediately ask the client to re-establish it.
        assert mgr._voice_lease_resync_suppressed is True


async def test_healthy_native_route_is_never_revoked():
    # independentAsrEnabled == false is a HEALTHY native route, not a failure.
    mgr = _make_routable_audio_manager(True)
    _authorize_core_lease(mgr)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_microphone_route("native")
    mgr._asr_runtime.abort = AsyncMock()

    assert await mgr._revoke_lease_for_blocked_route("should_not_fire") is False
    assert mgr._voice_lease_connection_id == "socket-a"
    mgr._asr_runtime.abort.assert_not_awaited()


async def test_enabled_but_failed_startup_stops_accepting_microphone_pcm():
    # End-to-end pin for the wiring, not just the helper: independent ASR is
    # ENABLED and start() comes back not-READY, so the route is blocked for the
    # whole session. Ingress must stop accepting PCM. Without the revoke at
    # that exit the lease stays live and every frame is decoded, denoised and
    # VAD'd before being dropped at the router.
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._asr_runtime.abort = AsyncMock()
    async def _failed_start(**_kwargs):
        # Read the epoch at call time: _start_independent_asr_if_enabled closes
        # the previous runtime first, which bumps it.
        return AsrStartResult(
            AsrStartStatus.FAILED,
            provider="qwen",
            failure_code="ASR_INDEPENDENT_FAILED",
            session_epoch=mgr._capture_ingress_token().session_epoch,
        )

    mgr._asr_runtime.start = _failed_start
    mgr.core_api_type = "qwen"
    mgr.user_language = None
    mgr.send_status = AsyncMock()

    async def _settings():
        return {"independentAsrEnabled": True}

    with patch.object(
        core_asr_runtime_module._core_facade,
        "aload_global_conversation_settings",
        _settings,
    ):
        await LLMSessionManager._start_independent_asr_if_enabled(mgr, "audio")

    assert mgr._asr_route_mode == "blocked"
    assert mgr._voice_lease_connection_id == ""
    assert mgr._voice_input_accepts_pcm() is False


def _fake_socket_pair():
    class _LiveState:
        CONNECTED = "connected"

        def __eq__(self, other) -> bool:
            return other == "connected"

    class _Socket:
        def __init__(self) -> None:
            self.client_state = _LiveState()
            self.sent: list[str] = []

        async def send_text(self, data: str) -> None:
            self.sent.append(data)

    return _Socket(), _Socket()


async def test_text_takeover_reaches_the_recorder_before_the_revoke():
    # Codex P2. The revoke clears BOTH _voice_input_websocket and the lease id,
    # and _voice_owner_socket() returns None on either -- so the fan-out added
    # for exactly this case had no target by the time send_session_started ran.
    # The recorder never heard that the route died and kept the hardware mic.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    assert mgr._set_voice_input_websocket("socket-a", recorder) is True
    mgr.websocket = chat
    mgr._asr_runtime.abort = AsyncMock()

    await LLMSessionManager._start_independent_asr_if_enabled(mgr, "text")

    delivered = [json.loads(x) for x in recorder.sent]
    assert delivered == [{"type": "session_started", "input_mode": "text"}]
    # The revoke still happens -- delivery is advisory, the lease is the backstop.
    assert mgr._voice_lease_connection_id == ""
    assert mgr._voice_input_websocket is None


async def test_text_takeover_does_not_revoke_a_competing_newer_start():
    # The push above is the first await after the route-operation fence. A
    # competing newer start can install its own blocked placeholder during it,
    # and _revoke_voice_input_connection would then _invalidate_asr_start() it.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr._asr_runtime.abort = AsyncMock()
    invalidated = MagicMock()
    mgr._asr_runtime._invalidate_asr_start = invalidated

    original_send = recorder.send_text

    async def _send_then_supersede(data: str) -> None:
        await original_send(data)
        # A newer route operation starts while the push is in flight.
        mgr._begin_asr_route_operation()

    recorder.send_text = _send_then_supersede

    await LLMSessionManager._start_independent_asr_if_enabled(mgr, "text")

    # Delivery is already implied by the retained lease below -- the fence can
    # only see a newer operation because _send_then_supersede ran -- but pin it
    # directly so the two facts fail separately.
    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_started", "input_mode": "text"}
    ]
    invalidated.assert_not_called()
    assert mgr._voice_lease_connection_id == "socket-a"


async def test_pipeline_failure_revokes_after_notifying():
    # ASR_AUDIO_PREPROCESSING_FAILED rides neither the BLOCKED channel nor the
    # ASR_INDEPENDENT_ prefix, so it was the one "route dead" status with no
    # ingress stop behind it.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr._set_microphone_route("independent")
    mgr._asr_runtime.abort = AsyncMock()
    mgr.is_active = True

    await LLMSessionManager._fail_voice_input_pipeline(
        mgr,
        ingress_token=mgr._capture_ingress_token(),
        session_ref=mgr.session,
        audio_epoch=mgr._audio_stream_epoch,
        pipeline_ref=mgr._voice_input_audio_pipeline,
    )

    codes = [json.loads(json.loads(x)["message"])["code"] for x in recorder.sent]
    assert "ASR_AUDIO_PREPROCESSING_FAILED" in codes
    assert mgr._asr_route_mode == "blocked"
    assert mgr._voice_input_accepts_pcm() is False


def _blocked_route_manager_with_recorder():
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr._set_microphone_route("blocked")
    mgr._asr_runtime.abort = AsyncMock()
    return mgr, recorder


async def test_fail_closed_chokepoint_notifies_then_revokes():
    # The ordering the chokepoint exists to own: the notice must reach the
    # lease holder BEFORE the revoke, because the revoke clears both
    # _voice_input_websocket and the lease id and _voice_owner_socket() then
    # returns None.
    mgr, recorder = _blocked_route_manager_with_recorder()
    generation = mgr._begin_asr_route_operation()

    revoked = await LLMSessionManager._fail_closed_voice_route(
        mgr,
        "text_session_active",
        operation_generation=generation,
        voice_owner_notice={"type": "session_started", "input_mode": "text", "microphone_route": "native"},
    )

    assert revoked is True
    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_started", "input_mode": "text", "microphone_route": "native"}
    ]
    assert mgr._voice_lease_connection_id == ""


async def test_fail_closed_chokepoint_refuses_to_revoke_a_competing_newer_start():
    # Step 3 of the chokepoint. The notice is an await, so a competing NEWER
    # route operation can install its own blocked placeholder during it;
    # _revoke_voice_input_connection would then _invalidate_asr_start() and
    # cancel it. This is the constraint that sank a naive "revoke whenever the
    # route ends blocked" -- holding it inside the chokepoint is what makes a
    # new exit safe without rediscovering it.
    mgr, recorder = _blocked_route_manager_with_recorder()
    invalidated = MagicMock()
    mgr._asr_runtime._invalidate_asr_start = invalidated
    generation = mgr._begin_asr_route_operation()

    original_send = recorder.send_text

    async def _send_then_supersede(data: str) -> None:
        await original_send(data)
        mgr._begin_asr_route_operation()

    recorder.send_text = _send_then_supersede

    revoked = await LLMSessionManager._fail_closed_voice_route(
        mgr,
        "text_session_active",
        operation_generation=generation,
        voice_owner_notice={"type": "session_started", "input_mode": "text", "microphone_route": "native"},
    )

    assert revoked is False
    invalidated.assert_not_called()
    assert mgr._voice_lease_connection_id == "socket-a"
    # Fencing protects the newer start; it does not silence the recorder.
    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_started", "input_mode": "text", "microphone_route": "native"}
    ]


@pytest.mark.parametrize(
    ("snapshot", "clobbered_shared_value"),
    [
        # This request said "independent"; a later one cleared the shared field
        # (an older frontend omits the key entirely, which CLEARS the override).
        (True, None),
        # ...and the opposite direction: a later text request said "disabled".
        (True, False),
        # A request that said "disabled" must not inherit a later "enabled".
        (False, True),
    ],
)
async def test_start_uses_its_own_handshake_not_a_later_requests(
    snapshot,
    clobbered_shared_value,
):
    # Codex P2. websocket_router writes each start_session's authoritative
    # independent-ASR toggle into ONE manager-level field and then fires
    # start_session as a background task; the route decision reads that field
    # much later, after many awaits. A second start_session arriving in between
    # replaced or cleared the first request's value, so that audio session
    # selected the persisted or the opposite route. start_session now snapshots
    # the handshake before its first await and carries it down.
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr.core_api_type = "qwen"
    mgr.user_language = None
    mgr.send_status = AsyncMock()
    mgr._asr_runtime.abort = AsyncMock()
    mgr._asr_runtime.start = AsyncMock(
        return_value=AsrStartResult(
            AsrStartStatus.READY,
            provider="qwen",
            session_epoch=mgr._capture_ingress_token().session_epoch,
        )
    )

    async def _settings(**_kwargs):
        # Persisted value deliberately opposes the snapshot, so a fallback to
        # the persisted read is distinguishable from honouring the snapshot.
        return {"independentAsrEnabled": not snapshot}

    async def _clobber_mid_start(**_kwargs):
        # A competing start_session lands while this one is still resolving.
        mgr.set_independent_asr_handshake(clobbered_shared_value)
        return await _settings()

    with patch.object(
        core_asr_runtime_module._core_facade,
        "aload_global_conversation_settings",
        _clobber_mid_start,
    ):
        await LLMSessionManager._start_independent_asr_if_enabled(
            mgr,
            "audio",
            handshake_override=snapshot,
        )

    # The discriminator is which BRANCH the handshake selected, not how far the
    # independent start then got: "native" means the route decision read
    # `enabled = False`, anything else means it took the independent path.
    if snapshot:
        assert mgr._asr_route_mode != "native"
    else:
        assert mgr._asr_route_mode == "native"


def test_start_session_snapshots_the_handshake_and_hands_it_down():
    # The behavioural test above calls _start_independent_asr_if_enabled with an
    # explicit override, so it pins only that the callee honours one. The actual
    # fix is in start_session: read the shared field BEFORE the first await, and
    # carry it down. Delete either half and the test above stays green, because
    # the callee then falls back to the shared field it was already reading.
    # This pins the wiring itself.
    import ast

    source = (
        Path(__file__).resolve().parents[2] / "main_logic" / "core" / "lifecycle.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    start_session = functions["start_session"]
    snapshot_lines = [
        node.lineno
        for node in ast.walk(start_session)
        if isinstance(node, ast.Name)
        and node.id == "session_handshake_override"
        and isinstance(node.ctx, ast.Store)
    ]
    assert snapshot_lines, "start_session no longer snapshots the handshake"
    await_lines = [
        node.lineno for node in ast.walk(start_session) if isinstance(node, ast.Await)
    ]
    assert await_lines, "start_session has no awaits -- this pin would be vacuous"
    # The whole point: the read must beat every await, or a competing
    # start_session can replace the shared field before it runs.
    assert min(snapshot_lines) < min(await_lines)

    # ...and the snapshot must actually reach the route decision, both hops.
    def passes_handshake(fn, keyword_value):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "handshake_override" and isinstance(kw.value, ast.Name):
                    if kw.value.id == keyword_value:
                        return True
        return False

    assert passes_handshake(start_session, "session_handshake_override"), (
        "start_session no longer passes its snapshot to _start_session_activate"
    )
    assert passes_handshake(functions["_start_session_activate"], "handshake_override"), (
        "_start_session_activate no longer forwards the handshake to the route decision"
    )


def test_start_session_snapshots_resource_optimization_handshake_before_await():
    import ast

    source = (
        Path(__file__).resolve().parents[2] / "main_logic" / "core" / "lifecycle.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    start_session = functions["start_session"]
    snapshot_name = "session_resource_optimization_handshake_override"
    snapshot_lines = [
        node.lineno
        for node in ast.walk(start_session)
        if isinstance(node, ast.Name)
        and node.id == snapshot_name
        and isinstance(node.ctx, ast.Store)
    ]
    await_lines = [
        node.lineno for node in ast.walk(start_session) if isinstance(node, ast.Await)
    ]

    assert snapshot_lines
    assert await_lines
    assert max(snapshot_lines) < min(await_lines)

    def passes_override(fn, keyword_value):
        return any(
            kw.arg == "resource_optimization_override"
            and isinstance(kw.value, ast.Name)
            and kw.value.id == keyword_value
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            for kw in node.keywords
        )

    assert passes_override(start_session, snapshot_name)
    assert passes_override(
        functions["_start_session_activate"],
        "resource_optimization_override",
    )


async def test_start_without_a_snapshot_still_reads_the_shared_handshake():
    # Non-vacuity, and the contract for the internal re-entry paths (hot swap,
    # device change): with no snapshot supplied the shared field still decides.
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr.core_api_type = "qwen"
    mgr.user_language = None
    mgr.send_status = AsyncMock()
    mgr._asr_runtime.abort = AsyncMock()
    mgr.set_independent_asr_handshake(False)

    async def _settings(**_kwargs):
        return {"independentAsrEnabled": True}

    with patch.object(
        core_asr_runtime_module._core_facade,
        "aload_global_conversation_settings",
        _settings,
    ):
        await LLMSessionManager._start_independent_asr_if_enabled(mgr, "audio")

    assert mgr._asr_route_mode == "native"


class _InertHotSwapCache:
    """Empty cache that still supports the clear() a lease handover performs."""

    duration_ms = 0

    def clear(self) -> None:
        return None

    def __bool__(self) -> bool:
        return False


async def test_silence_timeout_does_not_stop_a_replacement_recorder():
    # Codex P2. handle_silence_timeout holds no self.lock, so a replacement
    # session can be installed while the display send awaits -- and
    # _send_to_voice_owner resolves the owner socket at CALL time, so this old
    # timeout's auto_close_mic landed on the NEW recorder and stopped a
    # microphone the user had just opened. end_session is guarded by
    # expected_session and refuses on its own, which is exactly why the damage
    # was frontend-only and invisible from the backend.
    old_recorder, chat = _fake_socket_pair()
    new_recorder, _unused = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", old_recorder)
    mgr.websocket = chat
    mgr.pending_session = None
    mgr.hot_swap_audio_cache = _InertHotSwapCache()
    mgr.core_api_type = "paid"
    mgr.end_session = AsyncMock()
    timed_out_session = mgr.session

    async def _replace_session_mid_send(_payload: dict) -> None:
        mgr.session = object()
        mgr._begin_voice_input_connection("socket-b")
        mgr._set_voice_input_websocket("socket-b", new_recorder)

    chat.send_json = _replace_session_mid_send

    await LLMSessionManager.handle_silence_timeout(
        mgr,
        expected_session=timed_out_session,
    )

    assert new_recorder.sent == []
    assert old_recorder.sent == []
    # Still attempted: a stale timeout has nothing to close, and end_session's
    # own expected_session guard is what decides that -- not this function.
    mgr.end_session.assert_awaited_once()


async def test_silence_timeout_skips_when_only_the_lease_moved():
    # The test above moves session AND lease together, so session_still_current
    # is already false and the lease half of the guard is never exercised.
    # Move ONLY the lease: a different window took the microphone while the
    # display send awaited, and the old timeout's teardown must not reach it.
    old_recorder, chat = _fake_socket_pair()
    new_recorder, _unused = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", old_recorder)
    mgr.websocket = chat
    mgr.pending_session = None
    mgr.hot_swap_audio_cache = _InertHotSwapCache()
    mgr.core_api_type = "paid"
    mgr.end_session = AsyncMock()

    async def _replace_lease_mid_send(_payload: dict) -> None:
        # Session deliberately untouched.
        mgr._begin_voice_input_connection("socket-b")
        _authorize_core_lease(mgr)
        mgr._set_voice_input_websocket("socket-b", new_recorder)

    chat.send_json = _replace_lease_mid_send

    await LLMSessionManager.handle_silence_timeout(
        mgr,
        expected_session=mgr.session,
    )

    assert new_recorder.sent == []
    assert old_recorder.sent == []
    mgr.end_session.assert_awaited_once()


async def test_silence_timeout_still_delivers_when_only_the_mute_state_moved():
    # The guard compares lease IDENTITY, not the lease generation: the SAME
    # holder bumps the generation on every hard_mute / hard_unmute /
    # focus_suppress / focus_resume / lease_sync. Comparing it made a user
    # muting during the display send look like a handover, so auto_close_mic
    # was skipped while end_session still ran -- backend session closed,
    # recorder never told, hardware microphone still open.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr.pending_session = None
    mgr.hot_swap_audio_cache = _InertHotSwapCache()
    mgr.core_api_type = "paid"
    mgr.end_session = AsyncMock()
    generation_before = mgr._voice_lease_generation
    mid_send: list[str] = []

    async def _mute_mid_send(_payload: dict) -> None:
        # Same window, same lease -- it just muted itself.
        applied = await mgr._handle_voice_input_control(
            "hard_mute",
            generation_before + 1,
        )
        assert applied is True
        mid_send.append("muted")

    chat.send_json = _mute_mid_send

    await LLMSessionManager.handle_silence_timeout(
        mgr,
        expected_session=mgr.session,
    )

    # CodeRabbit: assertions inside the callback only bind if the callback
    # RUNS. Should the display send ever stop happening -- the CONNECTED guard
    # changing, or the fake socket losing send_json -- this would silently
    # decay into a duplicate of the "nothing moved" case and stay green while
    # covering none of the mute path. Pin the race itself from out here.
    assert mid_send == ["muted"]
    assert mgr._voice_lease_generation != generation_before
    assert mgr._voice_lease_hard_muted is True
    assert mgr._voice_lease_connection_id == "socket-a"

    assert [json.loads(x)["type"] for x in recorder.sent] == ["auto_close_mic"]
    mgr.end_session.assert_awaited_once()


async def test_silence_timeout_still_reaches_the_recorder_when_nothing_moved():
    # Non-vacuity for the guard above: with the lease and session unchanged the
    # teardown must still be delivered.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr.pending_session = None
    mgr.hot_swap_audio_cache = _InertHotSwapCache()
    mgr.core_api_type = "paid"
    mgr.end_session = AsyncMock()

    await LLMSessionManager.handle_silence_timeout(
        mgr,
        expected_session=mgr.session,
    )

    assert [json.loads(x)["type"] for x in recorder.sent] == ["auto_close_mic"]
    mgr.end_session.assert_awaited_once()


async def test_unreadable_settings_fail_closed_instead_of_selecting_native():
    # Codex P2. load_global_conversation_settings() swallowed every IO/JSON
    # error and returned the SAME empty dict as a file with no settings yet, so
    # the asr_settings_unreadable branch was unreachable for the failure it
    # exists to catch: an unreadable user_preferences.json fell through to
    # `enabled = False` and quietly selected the native Omni route, overriding a
    # persisted choice that required independent ASR. The read is strict here
    # now, so a real read failure stays fail-closed.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr.core_api_type = "qwen"
    mgr.user_language = None
    mgr.send_status = AsyncMock()
    mgr._asr_runtime.abort = AsyncMock()

    async def _unreadable(*, strict: bool = False, **_kwargs):
        # Models the real loader exactly: it only SURFACES the failure when the
        # caller asks for it, and otherwise reports the same empty dict as a
        # file that simply has no settings. A double that raised either way
        # could not tell whether the call site still passes strict=True.
        if not strict:
            return {}
        raise json.JSONDecodeError("Expecting value", "", 0)

    with patch.object(
        core_asr_runtime_module._core_facade,
        "aload_global_conversation_settings",
        _unreadable,
    ):
        await LLMSessionManager._start_independent_asr_if_enabled(mgr, "audio")

    assert mgr._asr_route_mode == "blocked"
    assert mgr._asr_route_mode != "native"
    assert mgr._voice_lease_connection_id == ""
    assert mgr._voice_input_accepts_pcm() is False


async def test_unreadable_settings_do_not_kill_the_mic_when_asr_is_disabled():
    # Fail-closed is only the safe answer for a user who WANTS independent ASR:
    # for them the persisted read is the authority. A user whose frontend
    # handshake says the feature is OFF has no such choice to protect, and this
    # path runs for EVERY audio session -- so revoking here killed the
    # microphone of someone who never enabled the feature, over a settings file
    # they may not know exists. Before this PR that case simply used native.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr.core_api_type = "qwen"
    mgr.user_language = None
    mgr.send_status = AsyncMock()
    mgr._asr_runtime.abort = AsyncMock()

    async def _unreadable(*, strict: bool = False, **_kwargs):
        if not strict:
            return {}
        raise json.JSONDecodeError("Expecting value", "", 0)

    with patch.object(
        core_asr_runtime_module._core_facade,
        "aload_global_conversation_settings",
        _unreadable,
    ):
        await LLMSessionManager._start_independent_asr_if_enabled(
            mgr,
            "audio",
            handshake_override=False,
        )

    # Native route, lease intact, microphone still usable.
    assert mgr._asr_route_mode == "native"
    assert mgr._voice_lease_connection_id == "socket-a"
    assert mgr._voice_input_accepts_pcm() is True
    # ...and the client is told why, exactly as the ordinary disabled path does.
    codes = [json.loads(json.loads(x)["message"])["code"] for x in recorder.sent]
    assert "ASR_INDEPENDENT_DISABLED" in codes


async def test_absent_settings_use_disabled_default_without_failing_closed():
    # The other half of the strict read: an ABSENT file is not a failure. A
    # first run has no settings yet and must use the disabled default rather
    # than blocking the microphone route.
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr.core_api_type = "qwen"
    mgr.user_language = None
    mgr.send_status = AsyncMock()
    mgr._asr_runtime.abort = AsyncMock()

    mgr._asr_runtime.start = AsyncMock()

    async def _empty(**_kwargs):
        return {}

    with patch.object(
        core_asr_runtime_module._core_facade,
        "aload_global_conversation_settings",
        _empty,
    ):
        await LLMSessionManager._start_independent_asr_if_enabled(mgr, "audio")

    assert mgr._asr_route_mode == "native"
    mgr._asr_runtime.start.assert_not_called()


async def test_fail_closed_chokepoint_honours_the_callers_own_predicate():
    # still_current carries each exit's own, usually stricter, staleness check
    # (route key, provider, session ref, ingress epoch...). A false predicate
    # must stop the exit before anything is delivered or revoked.
    mgr, recorder = _blocked_route_manager_with_recorder()
    generation = mgr._begin_asr_route_operation()

    revoked = await LLMSessionManager._fail_closed_voice_route(
        mgr,
        "asr_settings_unreadable",
        operation_generation=generation,
        still_current=lambda: False,
        voice_owner_notice={"type": "session_started", "input_mode": "text", "microphone_route": "native"},
    )

    assert revoked is False
    assert recorder.sent == []
    assert mgr._voice_lease_connection_id == "socket-a"


async def test_fail_closed_chokepoint_exempts_the_game_owner():
    # The galgame gate owns the mic through its built-in consumer route and tears
    # down via GAME_ROUTE_ENDED, so it must be neither notified nor revoked.
    mgr, recorder = _blocked_route_manager_with_recorder()
    mgr._voice_lease_owner = "game"
    generation = mgr._begin_asr_route_operation()

    revoked = await LLMSessionManager._fail_closed_voice_route(
        mgr,
        "text_session_active",
        operation_generation=generation,
        voice_owner_notice={"type": "session_started", "input_mode": "text", "microphone_route": "native"},
    )

    assert revoked is False
    assert recorder.sent == []
    assert mgr._voice_lease_connection_id == "socket-a"


# Starlette does not raise WebSocketDisconnect on send: an already-closed
# socket raises a bare RuntimeError, which the generic `except Exception` arm
# would swallow just as fatally as the typed one. Every isolation below is
# parametrized over both rather than over the type the reviewers named.
_DEAD_DISPLAY_SENDS = [
    WebSocketDisconnect(1006),
    RuntimeError('Cannot call "send" once a close message has been sent.'),
]


def _raising_display_send(display_error):
    """A failing display send, plus proof it was actually reached.

    CodeRabbit: without the flag these cases sit one guard change away from
    vacuity. If the CONNECTED check ever stopped admitting the fake socket, the
    raiser would not run, nothing would raise, the fan-out would deliver anyway
    -- and the PRE-FIX shared-try code would pass too, silently.
    """

    attempted: list[bool] = []

    async def _die(_payload) -> None:
        attempted.append(True)
        raise display_error

    return _die, attempted


@pytest.mark.parametrize("display_error", _DEAD_DISPLAY_SENDS)
async def test_dead_display_socket_does_not_swallow_the_recorder_teardown(
    display_error,
):
    # Codex P2 / CodeRabbit. The display send and the lease-holder fan-out
    # shared one try, so a display socket dying between the CONNECTED check and
    # the send skipped the fan-out -- dropping the one message that stops a live
    # hardware microphone, which then kept uploading into a dead route.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr.input_mode = "audio"

    chat.send_text, attempted = _raising_display_send(display_error)

    await LLMSessionManager.send_session_ended_by_server(mgr)

    assert attempted, "display send never reached -- the case proves nothing"
    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_ended_by_server", "input_mode": "audio"}
    ]


@pytest.mark.parametrize("display_error", _DEAD_DISPLAY_SENDS)
async def test_dead_display_socket_does_not_swallow_the_text_takeover(
    display_error,
):
    # The third site with this exact shape, and the one this round missed:
    # send_session_started's text fan-out is what tells a recorder superseded by
    # THIS chat window that its route just went blocked. Same defect, same fix --
    # leaving it behind keeps a reachable zombie microphone even with the other
    # two isolations in place.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat

    chat.send_text, attempted = _raising_display_send(display_error)

    await LLMSessionManager.send_session_started(mgr, "text")

    assert attempted, "display send never reached -- the case proves nothing"
    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_started", "input_mode": "text", "microphone_route": "native"}
    ]


async def test_audio_start_ack_reaches_the_window_that_asked_for_it():
    # Codex P2. `self.websocket` is reassigned to EVERY newly accepted socket,
    # and a whole session start (TTS + LLM + independent ASR) sits between that
    # reassignment and this ack. A second window opening in that interval took
    # the ack, and the window that actually asked -- still the voice-lease
    # holder, because the router claims the lease for the requesting socket
    # synchronously before firing start_session -- sat on sessionStartPromise
    # until its 15s deadline and never called startMicCapture. The user clicks
    # the mic and simply never gets a microphone.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    # The second window opened mid-start and took the display plane.
    mgr.websocket = chat

    await LLMSessionManager.send_session_started(mgr, "audio")

    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_started", "input_mode": "audio", "microphone_route": "native"}
    ], "the requesting window (the lease holder) must receive its own start ack"
    # The display plane still gets its copy; the two planes are independent.
    assert [json.loads(x) for x in chat.sent] == [
        {"type": "session_started", "input_mode": "audio", "microphone_route": "native"}
    ]


async def test_session_preparing_reaches_the_window_that_asked_for_it():
    # Completes the set with session_started / session_failed. Only cosmetic on
    # its own -- it drives the "preparing" banner -- but a requester that sees
    # neither this nor the ack has no feedback for the whole start.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat

    await LLMSessionManager.send_session_preparing(mgr, "audio")

    expected = {"type": "session_preparing", "input_mode": "audio"}
    assert [json.loads(x) for x in recorder.sent] == [expected]
    assert [json.loads(x) for x in chat.sent] == [expected]


async def test_session_preparing_is_not_duplicated_for_a_single_window():
    recorder, _chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = recorder

    await LLMSessionManager.send_session_preparing(mgr, "audio")

    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_preparing", "input_mode": "audio"}
    ]


async def test_audio_start_failure_reaches_the_window_that_asked_for_it():
    # Codex P2, the failure twin of the start ack. self.websocket is reassigned
    # to every newly accepted socket, so a second window opening during a start
    # that then FAILS took this notice, while the window that asked -- still the
    # lease holder, because the router claims the lease for the requesting
    # socket before firing start_session -- sat on sessionStartPromise until its
    # 15s deadline instead of failing fast. That timeout path then sends
    # end_session, tearing down whatever did get built.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat

    await LLMSessionManager.send_session_failed(mgr, "audio")

    expected = {"type": "session_failed", "input_mode": "audio"}
    assert [json.loads(x) for x in recorder.sent] == [expected], (
        "the requesting window must learn its start failed"
    )
    assert [json.loads(x) for x in chat.sent] == [expected]


async def test_session_failure_is_not_duplicated_for_a_single_window():
    recorder, _chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = recorder

    await LLMSessionManager.send_session_failed(mgr, "audio")

    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_failed", "input_mode": "audio"}
    ], "a single window must be told exactly once"


async def test_session_failure_does_not_reach_the_game_microphone():
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr._voice_lease_owner = "game"

    await LLMSessionManager.send_session_failed(mgr, "audio")

    assert recorder.sent == []
    assert [json.loads(x) for x in chat.sent] == [
        {"type": "session_failed", "input_mode": "audio"}
    ]


async def test_audio_start_ack_carries_the_settled_blocked_route():
    # Codex P2. The route verdict otherwise travels only as an ASR_INDEPENDENT_*
    # status on the mic control plane, and there are paths where it reaches
    # nobody: a second window claiming the voice lease while _asr_runtime.start()
    # is running bumps the ASR start generation, so the failing start's own
    # terminal status is fenced off and never emitted at all. The route stays
    # pinned "blocked", every window's fail-closed latch stays false, and the
    # ack says "started" -- so the microphone opens onto a route that discards
    # every frame, with no status and no recovery path.
    #
    # Qualifying the ack covers that, and carries the in-flight dedupe re-ack's
    # verdict too (_start_session_handle_inflight re-decides a blocked route
    # before re-acking; see test_session_start_guard.py).
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr._set_microphone_route("blocked")

    await LLMSessionManager.send_session_started(mgr, "audio")

    expected = {
        "type": "session_started",
        "input_mode": "audio",
        "microphone_route": "blocked",
    }
    # Both planes carry it: the window that asked is on the lease plane, and a
    # window that merely opened is on the display plane -- either could be the
    # one with no verdict.
    assert [json.loads(x) for x in recorder.sent] == [expected]
    assert [json.loads(x) for x in chat.sent] == [expected]


async def test_audio_start_ack_is_not_duplicated_for_a_single_window():
    # The fan-out must stay a no-op when the lease holder IS the current
    # socket, or an ordinary single-window start would deliver session_started
    # twice. _voice_owner_socket returns None in that case; this pins it.
    recorder, _chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = recorder

    await LLMSessionManager.send_session_started(mgr, "audio")

    assert [json.loads(x) for x in recorder.sent] == [
        {"type": "session_started", "input_mode": "audio", "microphone_route": "native"}
    ], "a single window must be acked exactly once"


async def test_start_ack_names_the_request_it_answers():
    # #2539 / Codex P2. An anonymous ack is settled by whichever window receives
    # it, and the lease fan-out routinely delivers one start's ack to a DIFFERENT
    # window -- the claimant that took the microphone mid-start. That window's
    # frontend then clears its timeout, resolves, reads the blocked route the ack
    # carries and aborts its own microphone flow; its real ack arrives to a flow
    # that already gave up. The id is what lets the receiver tell the two apart.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat

    await LLMSessionManager.send_session_started(mgr, "audio", request_id="w1-7")

    expected = {
        "type": "session_started",
        "input_mode": "audio",
        "request_id": "w1-7",
        "microphone_route": "native",
    }
    # Both planes carry it, or the plane that reaches the requester is the one
    # that cannot be recognised.
    assert [json.loads(x) for x in recorder.sent] == [expected]
    assert [json.loads(x) for x in chat.sent] == [expected]


async def test_start_ack_stays_anonymous_without_a_request_id():
    # Internal starts (proactive, greeting, the disconnect recovery) carry no
    # request of their own. The field must be absent rather than null: the
    # frontend treats an ack with no id as "mine" to keep those paths working,
    # and a null would have to be special-cased at every reader.
    recorder, _chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = recorder

    await LLMSessionManager.send_session_started(mgr, "audio")

    assert "request_id" not in json.loads(recorder.sent[0])


async def test_dedupe_ack_reaches_the_requester_after_a_fail_closed_reroute():
    # Codex P2. The dedupe re-ack runs a route re-decision first, and that can
    # fail closed -- which REVOKES the lease, clearing _voice_lease_connection_id
    # and the voice socket. With a newer window as self.websocket the requester
    # is then on neither plane: it sits on its promise until the 15s timeout, and
    # that timeout's end_session tears down the session that just started.
    requester, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr.websocket = chat
    # Post-revoke state: no lease identity, no voice socket.
    mgr._voice_lease_connection_id = ""
    mgr._set_microphone_route("blocked")

    await LLMSessionManager.send_session_started(
        mgr, "audio", request_id="w2-3", also_notify=requester
    )

    expected = {
        "type": "session_started",
        "input_mode": "audio",
        "request_id": "w2-3",
        "microphone_route": "blocked",
    }
    assert [json.loads(x) for x in requester.sent] == [expected]


async def test_addressed_ack_dedupes_against_who_actually_got_it():
    # CodeRabbit. The fan-out's send is an await, and a lease takeover inside it
    # points _voice_owner_socket() at a DIFFERENT socket. Deduping against a
    # fresh read would then miss the socket that just got the payload -- the
    # requester -- and send it a second copy. One ack, one resolver, but the
    # handler around it runs again in full: microphone teardown, composer
    # visibility, the lot.
    requester, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", requester)
    mgr.websocket = chat

    original_send = requester.send_text

    async def _send_then_lose_the_lease(payload):
        await original_send(payload)
        # A third window claims the microphone while this send is in flight.
        later, _ = _fake_socket_pair()
        mgr._begin_voice_input_connection("socket-c")
        _authorize_core_lease(mgr)
        mgr._set_voice_input_websocket("socket-c", later)

    requester.send_text = _send_then_lose_the_lease

    await LLMSessionManager.send_session_started(
        mgr, "audio", request_id="w5-1", also_notify=requester
    )

    assert len(requester.sent) == 1, "the requester must be acked exactly once"


async def test_route_override_reaches_only_the_addressed_requester():
    # Codex P2, twice over. For a requester that LOST the voice lease while it
    # waited, the live route belongs to the new holder -- and it may well be
    # healthy by then. Reporting it opens a microphone on a window whose PCM the
    # server discards as superseded, so that requester needs a blocked verdict
    # (override rather than suppress, or it hangs to its own timeout).
    #
    # But the verdict is true of the REQUESTER, not of the session: the new
    # holder is on the very same fan-out, and a window with no start pending
    # latches any blocked route it sees. Broadcasting the override would
    # fail-close the microphone of the window that legitimately owns it.
    requester, new_holder = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-c")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-c", new_holder)
    mgr.websocket = new_holder
    mgr._set_microphone_route("independent")

    await LLMSessionManager.send_session_started(
        mgr,
        "audio",
        request_id="w4-2",
        also_notify=requester,
        microphone_route_override="blocked",
    )

    assert json.loads(requester.sent[0])["microphone_route"] == "blocked"
    assert [json.loads(x)["microphone_route"] for x in new_holder.sent] == [
        "independent"
    ], "the live route must stay intact for everyone but the requester"


async def test_route_override_travels_when_the_requester_is_the_display_socket():
    # The requester is simply the newest connection often enough, and then the
    # display plane is its ONLY copy -- the addressed send would dedupe itself
    # away. The override has to ride that plane in exactly that case.
    requester, _other = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr.websocket = requester
    mgr._voice_lease_connection_id = ""
    mgr._set_microphone_route("independent")

    await LLMSessionManager.send_session_started(
        mgr,
        "audio",
        request_id="w4-3",
        also_notify=requester,
        microphone_route_override="blocked",
    )

    assert [json.loads(x)["microphone_route"] for x in requester.sent] == ["blocked"]


async def test_addressed_ack_is_not_a_second_copy_for_the_same_socket():
    # The addressed send must stay a no-op when the requester is already on one
    # of the planes. A duplicate ack is not harmless: the resolver is one-shot
    # but the handler around it runs again in full -- microphone teardown,
    # composer visibility, the lot.
    requester, _chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", requester)
    mgr.websocket = requester

    await LLMSessionManager.send_session_started(
        mgr, "audio", request_id="w3-1", also_notify=requester
    )

    assert len(requester.sent) == 1, "the requester must be acked exactly once"


async def test_audio_start_ack_does_not_reach_the_game_microphone():
    # Same exemption the text path carries: the galgame gate owns the mic
    # through its built-in consumer route, and a session_started handler that
    # calls stopRecording would release a lease this ack never meant to touch.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr._voice_lease_owner = "game"

    await LLMSessionManager.send_session_started(mgr, "audio")

    assert recorder.sent == []
    assert [json.loads(x) for x in chat.sent] == [
        {"type": "session_started", "input_mode": "audio", "microphone_route": "native"}
    ]


async def test_text_takeover_does_not_stop_the_game_microphone():
    # Codex P2. websocket_router acknowledges a text entry made DURING an active
    # game route with a bare send_session_started("text") -- no ordinary text
    # session, no blocked route. Fanning that ack out to the lease holder
    # reaches the game window, whose session_started(text) handler calls
    # stopRecording({notifyServer:false}) on any window with isRecording true --
    # which a game STT gate requires -- releasing the game lease and closing
    # hardware the text entry never meant to touch. The other two teardown
    # senders and _fail_closed_voice_route all exempt owner "game"; this one was
    # the odd site out, and only became reliably reachable once the fan-out
    # stopped being swallowed by the display send.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr._voice_lease_owner = "game"

    await LLMSessionManager.send_session_started(mgr, "text")

    assert recorder.sent == []
    # The display socket still gets its ordinary ack.
    assert [json.loads(x) for x in chat.sent] == [
        {"type": "session_started", "input_mode": "text", "microphone_route": "native"}
    ]


@pytest.mark.parametrize("display_error", _DEAD_DISPLAY_SENDS)
async def test_silence_timeout_reaches_the_recorder_without_a_live_display(
    display_error,
):
    # Same shape one layer up: auto_close_mic sat INSIDE the display-socket
    # guard, so closing the chat window (mgr.websocket points at the dead socket
    # until that disconnect's voice handover repoints it) silently dropped the
    # recorder's teardown -- and a raising display send also skipped end_session.
    recorder, chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = chat
    mgr.pending_session = None
    mgr.hot_swap_audio_cache = None
    mgr.core_api_type = "paid"
    mgr.end_session = AsyncMock()

    chat.send_json, attempted = _raising_display_send(display_error)

    await LLMSessionManager.handle_silence_timeout(mgr)

    assert attempted, "display send never reached -- the case proves nothing"
    delivered = [json.loads(x) for x in recorder.sent]
    assert [x["type"] for x in delivered] == ["auto_close_mic"]
    assert delivered[0]["reason_code"] == "silence_timeout"
    mgr.end_session.assert_awaited_once()


async def test_silence_timeout_reaches_the_recorder_with_no_display_at_all():
    # A RAISING display send still ENTERS the CONNECTED guard, so the case above
    # would pass even if the fan-out had been left inside it. Only a socket that
    # fails the guard outright pins the hoist -- and that is the half that
    # matters when the chat window is simply gone rather than dying mid-send,
    # leaving the lease holder as the only window with hardware to release.
    recorder, _chat = _fake_socket_pair()
    mgr = _make_routable_audio_manager(True)
    mgr._begin_voice_input_connection("socket-a")
    _authorize_core_lease(mgr)
    mgr._set_voice_input_websocket("socket-a", recorder)
    mgr.websocket = None
    mgr.pending_session = None
    mgr.hot_swap_audio_cache = None
    mgr.core_api_type = "paid"
    mgr.end_session = AsyncMock()

    await LLMSessionManager.handle_silence_timeout(mgr)

    assert [json.loads(x)["type"] for x in recorder.sent] == ["auto_close_mic"]
    mgr.end_session.assert_awaited_once()
