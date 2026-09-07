from __future__ import annotations

import asyncio
import threading

import pytest

from main_logic.voice_turn.audio_input import VoiceInputAudioPipeline


class _Processor:
    def __init__(self) -> None:
        self.inputs: list[bytes] = []
        self.closed = False
        self.speech_probability = 0.75
        self.rnnoise_available = True
        self.rnnoise_frame_count = 3
        self.rnnoise_probability_peak = 0.9
        self.rnnoise_probability_mean = 0.6
        self.rnnoise_probability_last = 0.2
        self.rnnoise_probability_ema = 0.55
        self.finalize_calls = 0

    def process_chunk(self, pcm16: bytes) -> bytes:
        self.inputs.append(pcm16)
        return b"\x02\x00" * 160

    def close(self) -> None:
        self.closed = True

    def finalize_stream(self) -> bytes:
        self.finalize_calls += 1
        return b"tail"


async def test_pipeline_passes_16k_without_creating_rnnoise_processor() -> None:
    created: list[_Processor] = []
    pipeline = VoiceInputAudioPipeline(
        processor_factory=lambda: created.append(_Processor()) or created[-1]
    )

    pcm16 = b"\x01\x00" * 160
    frame = await pipeline.process(pcm16, sample_rate_hz=16_000)

    assert frame.pcm16 == pcm16
    assert frame.sample_rate_hz == 16_000
    assert frame.speech_probability is None
    assert frame.rnnoise_evidence is not None
    assert frame.rnnoise_evidence.available is False
    assert created == []


async def test_pipeline_preserves_core_capture_identity() -> None:
    pipeline = VoiceInputAudioPipeline()

    frame = await pipeline.process(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        ingress_sequence=41,
        captured_at=1234.5,
    )

    assert frame.ingress_sequence == 41
    assert frame.captured_at == 1234.5


async def test_pipeline_owns_48k_processor_and_exposes_rnnoise_probability() -> None:
    processor = _Processor()
    pipeline = VoiceInputAudioPipeline(processor_factory=lambda: processor)
    source = b"\x01\x00" * 480

    frame = await pipeline.process(source, sample_rate_hz=48_000)

    assert processor.inputs == [source]
    assert frame.pcm16 == b"\x02\x00" * 160
    assert frame.sample_rate_hz == 16_000
    assert frame.speech_probability == 0.9
    assert frame.rnnoise_available is True
    assert frame.rnnoise_evidence is not None
    assert frame.rnnoise_evidence.frame_count == 3
    assert frame.rnnoise_evidence.peak == 0.9
    assert frame.rnnoise_evidence.mean == 0.6
    assert frame.rnnoise_evidence.last == 0.2
    assert frame.rnnoise_evidence.ema == 0.55
    await pipeline.close()
    assert processor.closed is True


async def test_pipeline_does_not_reuse_stale_evidence_when_rnnoise_unavailable() -> None:
    processor = _Processor()
    processor.rnnoise_available = False
    pipeline = VoiceInputAudioPipeline(processor_factory=lambda: processor)

    frame = await pipeline.process(b"\x01\x00" * 480, sample_rate_hz=48_000)

    assert frame.speech_probability is None
    assert frame.rnnoise_available is False
    assert frame.rnnoise_evidence is not None
    assert frame.rnnoise_evidence.available is False
    assert frame.rnnoise_evidence.frame_count == 0


async def test_pipeline_rejects_invalid_pcm_and_sample_rate() -> None:
    pipeline = VoiceInputAudioPipeline()

    try:
        await pipeline.process(b"\x00", sample_rate_hz=16_000)
    except ValueError as exc:
        assert "PCM16" in str(exc)
    else:
        raise AssertionError("odd PCM must be rejected")

    try:
        await pipeline.process(b"\x00\x00", sample_rate_hz=24_000)
    except ValueError as exc:
        assert "sample rate" in str(exc)
    else:
        raise AssertionError("unsupported sample rate must be rejected")


async def test_pipeline_close_waits_for_cancelled_processing_thread() -> None:
    processing_started = threading.Event()
    release_processing = threading.Event()

    class _BlockingProcessor(_Processor):
        def __init__(self) -> None:
            super().__init__()
            self.processing = False

        def process_chunk(self, pcm16: bytes) -> bytes:
            self.processing = True
            processing_started.set()
            assert release_processing.wait(5)
            self.processing = False
            return super().process_chunk(pcm16)

        def close(self) -> None:
            assert not self.processing
            super().close()

    processor = _BlockingProcessor()
    pipeline = VoiceInputAudioPipeline(processor_factory=lambda: processor)
    process_task = asyncio.create_task(
        pipeline.process(b"\x01\x00" * 480, sample_rate_hz=48_000)
    )
    assert await asyncio.to_thread(processing_started.wait, 5)

    process_task.cancel()
    close_task = asyncio.create_task(pipeline.close())
    await asyncio.sleep(0)

    assert not process_task.done()
    assert not close_task.done()
    assert processor.closed is False

    release_processing.set()
    with pytest.raises(asyncio.CancelledError):
        await process_task
    await close_task

    assert processor.closed is True


async def test_pipeline_finalize_returns_bytes_and_enters_terminal_state() -> None:
    processor = _Processor()
    pipeline = VoiceInputAudioPipeline(processor_factory=lambda: processor)

    with pytest.raises(RuntimeError, match="VOICE_AUDIO_PIPELINE_EMPTY"):
        await pipeline.finalize_stream()
    assert processor.finalize_calls == 0

    await pipeline.process(b"\x01\x00" * 480, sample_rate_hz=48_000)

    tail = await pipeline.finalize_stream()

    assert type(tail) is bytes
    assert tail == b"tail"
    assert processor.finalize_calls == 1
    with pytest.raises(RuntimeError, match="VOICE_AUDIO_PIPELINE_FINALIZED"):
        await pipeline.finalize_stream()
    with pytest.raises(RuntimeError, match="VOICE_AUDIO_PIPELINE_FINALIZED"):
        await pipeline.process(b"\x01\x00" * 480, sample_rate_hz=48_000)
    await pipeline.close()


async def test_concurrent_pipeline_finalize_enters_native_processor_once() -> None:
    finalize_started = threading.Event()
    release_finalize = threading.Event()

    class _BlockingFinalizeProcessor(_Processor):
        def finalize_stream(self) -> bytes:
            self.finalize_calls += 1
            finalize_started.set()
            assert release_finalize.wait(5)
            return b"tail"

    processor = _BlockingFinalizeProcessor()
    pipeline = VoiceInputAudioPipeline(processor_factory=lambda: processor)
    await pipeline.process(b"\x01\x00" * 480, sample_rate_hz=48_000)

    first = asyncio.create_task(pipeline.finalize_stream())
    assert await asyncio.to_thread(finalize_started.wait, 5)
    second = asyncio.create_task(pipeline.finalize_stream())
    await asyncio.sleep(0)

    assert not first.done()
    assert not second.done()
    release_finalize.set()
    assert await first == b"tail"
    with pytest.raises(RuntimeError, match="VOICE_AUDIO_PIPELINE_FINALIZED"):
        await second
    assert processor.finalize_calls == 1
    await pipeline.close()


async def test_cancelled_pipeline_finalize_waits_for_native_completion() -> None:
    finalize_started = threading.Event()
    release_finalize = threading.Event()

    class _BlockingFinalizeProcessor(_Processor):
        def finalize_stream(self) -> bytes:
            self.finalize_calls += 1
            finalize_started.set()
            assert release_finalize.wait(5)
            return b"tail"

    processor = _BlockingFinalizeProcessor()
    pipeline = VoiceInputAudioPipeline(processor_factory=lambda: processor)
    await pipeline.process(b"\x01\x00" * 480, sample_rate_hz=48_000)
    finalize_task = asyncio.create_task(pipeline.finalize_stream())
    assert await asyncio.to_thread(finalize_started.wait, 5)

    finalize_task.cancel()
    close_task = asyncio.create_task(pipeline.close())
    await asyncio.sleep(0)

    assert not finalize_task.done()
    assert not close_task.done()
    assert processor.closed is False
    release_finalize.set()
    with pytest.raises(asyncio.CancelledError):
        await finalize_task
    await close_task
    assert processor.finalize_calls == 1
    assert processor.closed is True


# A 48 kHz PCM16 frame: 960 bytes = 480 samples. Written as an ASCII literal on
# purpose -- the byte values are irrelevant to these cases and escaped ones only
# invite an encoding accident.
_PC_FRAME = b"ab" * 480


class _NoiseReductionManager:
    """Minimal stand-in carrying just the state the toggle path touches."""

    lanlan_name = "test"

    def __init__(self, *, nr_enabled: bool) -> None:
        self._voice_input_noise_reduction_enabled = nr_enabled
        self._voice_input_audio_pipeline = VoiceInputAudioPipeline(
            nr_enabled=nr_enabled
        )
        self._voice_input_pipeline_failed = True

    def _ensure_asr_runtime_state(self) -> None:  # pragma: no cover - trivial
        return None


class _GateAsyncLock:
    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self):
        self.requested.set()
        await self.release.wait()
        return self

    async def __aexit__(self, *_exc_info) -> None:
        return None


async def test_noise_reduction_toggle_rebuilds_the_core_microphone_pipeline() -> None:
    # Codex P2. The settings endpoint updated only the Omni processor, but every
    # microphone frame passes through this Core-owned pipeline first -- and it
    # downsamples PC audio to 16 kHz, so the Omni processor downstream skips
    # RNNoise on what it receives, while independent-ASR routes never reach the
    # Omni processor at all. The toggle was a no-op for the rest of the session
    # on every route, while the endpoint reported success.
    from main_logic.core.asr_runtime import AsrRuntimeMixin

    manager = _NoiseReductionManager(nr_enabled=True)
    original = manager._voice_input_audio_pipeline

    rebuilt = await AsrRuntimeMixin.apply_voice_input_noise_reduction(manager, False)

    assert rebuilt is True
    assert manager._voice_input_audio_pipeline is not original, (
        "the live pipeline must be replaced, or the toggle never reaches the mic"
    )
    assert manager._voice_input_audio_pipeline.nr_enabled is False
    assert manager._voice_input_noise_reduction_enabled is False
    assert manager._voice_input_pipeline_failed is False
    # Replacing is what lets the ingress staleness guards
    # (`self._voice_input_audio_pipeline is not pipeline_ref`) drop frames still
    # in flight against the old processor, so the stale one must be closed.
    # Asserted behaviourally rather than on private state.
    with pytest.raises(RuntimeError, match="VOICE_AUDIO_PIPELINE_CLOSED"):
        await original.process(_PC_FRAME, sample_rate_hz=48_000)


async def test_noise_reduction_toggle_to_the_same_value_is_a_no_op() -> None:
    # A settings POST that does not change the value must not tear down a live
    # pipeline: every rebuild drops the frame in flight.
    from main_logic.core.asr_runtime import AsrRuntimeMixin

    manager = _NoiseReductionManager(nr_enabled=True)
    original = manager._voice_input_audio_pipeline

    rebuilt = await AsrRuntimeMixin.apply_voice_input_noise_reduction(manager, True)

    assert rebuilt is False
    assert manager._voice_input_audio_pipeline is original
    # Still usable: nothing was torn down.
    frame = await original.process(_PC_FRAME, sample_rate_hz=48_000)
    assert frame.sample_rate_hz == 16_000


async def test_noise_reduction_toggle_survives_cancel_while_waiting_for_lock() -> None:
    from main_logic.core.asr_runtime import AsrRuntimeMixin

    manager = _NoiseReductionManager(nr_enabled=True)
    original = manager._voice_input_audio_pipeline
    gate = _GateAsyncLock()
    manager._voice_input_pipeline_transition_lock = gate

    caller = asyncio.create_task(
        AsrRuntimeMixin.apply_voice_input_noise_reduction(manager, False)
    )
    await asyncio.wait_for(gate.requested.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert manager._voice_input_noise_reduction_enabled is True
    assert manager._voice_input_audio_pipeline is original
    transition_task = next(
        task
        for task in manager._core_asr_cleanup_tasks
        if task.get_name() == "core-voice-input-pipeline-transition"
    )

    gate.release.set()
    assert await asyncio.wait_for(asyncio.shield(transition_task), 1) is True
    assert manager._voice_input_noise_reduction_enabled is False
    assert manager._voice_input_audio_pipeline.nr_enabled is False
    with pytest.raises(RuntimeError, match="VOICE_AUDIO_PIPELINE_CLOSED"):
        await original.process(_PC_FRAME, sample_rate_hz=48_000)


async def test_one_manager_failing_does_not_abandon_the_rest_of_the_toggle():
    # Codex P2. The apply loop had a single try around the whole iteration, so
    # the first manager whose live realtime transport rejected the update
    # abandoned every character after it in iteration order -- the user sees the
    # setting saved while some sessions never got it. Both the Core pipeline
    # call and the Omni call are now isolated per manager.
    from unittest.mock import AsyncMock, MagicMock

    import main_routers.config_router.preferences as preferences
    from main_logic.omni_realtime_client import OmniRealtimeClient

    def _manager(*, core_raises: bool, omni_raises: bool):
        mgr = MagicMock()
        mgr.is_active = True
        mgr.session = MagicMock(spec=OmniRealtimeClient)
        mgr.session.set_audio_noise_reduction_enabled = AsyncMock(
            side_effect=RuntimeError("omni down") if omni_raises else None
        )
        mgr.apply_voice_input_noise_reduction = AsyncMock(
            side_effect=RuntimeError("core down") if core_raises else None
        )
        return mgr

    first = _manager(core_raises=False, omni_raises=True)
    second = _manager(core_raises=True, omni_raises=False)
    third = _manager(core_raises=False, omni_raises=False)
    managers = {"a": first, "b": second, "c": third}

    original = preferences.get_session_manager
    preferences.get_session_manager = lambda: managers
    try:
        await preferences._apply_noise_reduction_to_active_sessions(False)
    finally:
        preferences.get_session_manager = original

    # Every manager was reached on both planes despite the earlier failures.
    for name, mgr in managers.items():
        mgr.apply_voice_input_noise_reduction.assert_awaited_once_with(False)
        mgr.session.set_audio_noise_reduction_enabled.assert_awaited_once_with(False)
