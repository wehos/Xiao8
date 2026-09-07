from __future__ import annotations

import asyncio
import importlib
import threading

import numpy as np
import pytest
import soxr

from main_logic.omni_realtime_client import OmniRealtimeClient
from utils.audio_processor import AudioProcessor, _LiteDenoiser


client_module = importlib.import_module("main_logic.omni_realtime_client._client")


class _FakeRnnoise:
    def __init__(self) -> None:
        self.destroyed: list[object] = []

    def create(self) -> object:
        return object()

    def destroy(self, state: object) -> None:
        self.destroyed.append(state)


def test_lite_denoiser_close_destroys_native_state_once() -> None:
    library = _FakeRnnoise()
    denoiser = _LiteDenoiser(library)
    state = denoiser._state

    denoiser.close()
    denoiser.close()

    assert library.destroyed == [state]
    assert denoiser._state is None


def test_audio_processor_close_releases_owned_buffers_and_denoiser() -> None:
    class _Denoiser:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    processor = object.__new__(AudioProcessor)
    denoiser = _Denoiser()
    processor._denoiser = denoiser
    processor._downsample_resampler = object()
    processor._frame_buffer = np.ones(4, dtype=np.int16)
    processor._debug_audio_before = [np.ones(1, dtype=np.int16)]
    processor._debug_audio_after = [np.ones(1, dtype=np.int16)]

    processor.close()
    processor.close()

    assert denoiser.close_calls == 1
    assert processor._denoiser is None
    assert processor._downsample_resampler is None
    assert processor._frame_buffer.size == 0
    assert processor._debug_audio_before == []
    assert processor._debug_audio_after == []


def test_audio_processor_finalize_stream_flushes_real_soxr_tail_once() -> None:
    processor = AudioProcessor(
        input_sample_rate=48_000,
        output_sample_rate=16_000,
        noise_reduce_enabled=False,
        agc_enabled=False,
        limiter_enabled=False,
    )
    source = np.random.default_rng(20_260_903).integers(
        -32_768,
        32_768,
        size=48_000 * 5,
        dtype=np.int16,
    )

    body = bytearray()
    for offset in range(0, source.size, 480):
        body.extend(processor.process_chunk(source[offset : offset + 480].tobytes()))
    tail = processor.finalize_stream()
    streamed = np.frombuffer(bytes(body) + tail, dtype=np.int16)
    offline_float = soxr.resample(
        source.astype(np.float32) / 32768.0,
        48_000,
        16_000,
        quality="HQ",
    )
    offline = (offline_float * 32768.0).clip(-32768, 32767).astype(np.int16)

    assert tail
    assert streamed.size == 80_000
    np.testing.assert_array_equal(streamed, offline)
    with pytest.raises(RuntimeError, match="AUDIO_PROCESSOR_STREAM_FINALIZED"):
        processor.finalize_stream()
    processor.close()


def test_audio_processor_finalize_uses_normal_pcm16_conversion() -> None:
    class _Resampler:
        def __init__(self) -> None:
            self.calls: list[tuple[int, bool]] = []

        def resample_chunk(
            self,
            audio: np.ndarray,
            *,
            last: bool = False,
        ) -> np.ndarray:
            self.calls.append((len(audio), last))
            return np.array([-1.0, -0.5, 0.0, 0.5, 1.0], dtype=np.float32)

    processor = AudioProcessor(
        input_sample_rate=48_000,
        output_sample_rate=16_000,
        noise_reduce_enabled=False,
        agc_enabled=False,
        limiter_enabled=False,
    )
    resampler = _Resampler()
    processor._downsample_resampler = resampler

    body = processor.process_chunk(np.zeros(480, dtype=np.int16).tobytes())
    tail = processor.finalize_stream()

    assert body == tail
    assert np.frombuffer(tail, dtype=np.int16).tolist() == [
        -32768,
        -16384,
        0,
        16384,
        32767,
    ]
    assert resampler.calls == [(480, False), (0, True)]
    processor.close()

    incomplete = AudioProcessor(
        input_sample_rate=48_000,
        output_sample_rate=16_000,
        noise_reduce_enabled=False,
        agc_enabled=False,
        limiter_enabled=False,
    )
    incomplete_resampler = _Resampler()
    incomplete._downsample_resampler = incomplete_resampler
    incomplete._frame_buffer_size = 1

    with pytest.raises(
        RuntimeError,
        match="AUDIO_PROCESSOR_INCOMPLETE_RNNOISE_FRAME",
    ):
        incomplete.finalize_stream()
    assert incomplete_resampler.calls == []
    with pytest.raises(RuntimeError, match="AUDIO_PROCESSOR_STREAM_FINALIZED"):
        incomplete.finalize_stream()
    incomplete.close()


def test_audio_processor_finalized_state_rejects_mutation() -> None:
    processor = AudioProcessor(
        input_sample_rate=48_000,
        output_sample_rate=16_000,
        noise_reduce_enabled=False,
        agc_enabled=False,
        limiter_enabled=False,
    )
    processor.finalize_stream()

    with pytest.raises(RuntimeError, match="AUDIO_PROCESSOR_STREAM_FINALIZED"):
        processor.process_chunk(b"\x00\x00")
    with pytest.raises(RuntimeError, match="AUDIO_PROCESSOR_STREAM_FINALIZED"):
        processor.reset()
    with pytest.raises(RuntimeError, match="AUDIO_PROCESSOR_STREAM_FINALIZED"):
        processor.request_reset()
    processor.close()


def test_audio_processor_close_is_idempotent_without_implicit_finalize() -> None:
    class _Resampler:
        def __init__(self) -> None:
            self.calls = 0

        def resample_chunk(self, _audio: np.ndarray, *, last: bool = False):
            self.calls += 1
            assert not last, "close must not manufacture an EOF flush"
            return np.empty(0, dtype=np.float32)

    processor = AudioProcessor(
        input_sample_rate=48_000,
        output_sample_rate=16_000,
        noise_reduce_enabled=False,
        agc_enabled=False,
        limiter_enabled=False,
    )
    resampler = _Resampler()
    processor._downsample_resampler = resampler

    processor.close()
    processor.close()

    assert resampler.calls == 0


def test_disabling_noise_reduction_releases_native_denoiser() -> None:
    class _Denoiser:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    processor = object.__new__(AudioProcessor)
    denoiser = _Denoiser()
    processor.noise_reduce_enabled = True
    processor._denoiser = denoiser
    processor._frame_buffer = np.ones(4, dtype=np.int16)
    processor._agc_gain = 2.0

    processor.set_enabled(False)

    assert denoiser.close_calls == 1
    assert processor._denoiser is None
    assert processor._frame_buffer.size == 0


def test_reenabling_noise_reduction_recreates_frame_buffer(monkeypatch) -> None:
    class _Denoiser:
        def process_frame(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
            return frame.copy(), 0.0

        def close(self) -> None:
            return None

    processor = AudioProcessor(
        input_sample_rate=48_000,
        output_sample_rate=48_000,
        noise_reduce_enabled=False,
        agc_enabled=False,
        limiter_enabled=False,
    )
    monkeypatch.setattr(
        processor,
        "_init_denoiser",
        lambda: setattr(processor, "_denoiser", _Denoiser()),
    )

    processor.set_enabled(True)
    output = processor.process_chunk(np.zeros(480, dtype=np.int16).tobytes())

    assert processor._frame_buffer.size == processor.RNNOISE_FRAME_SIZE
    assert len(output) == 480 * np.dtype(np.int16).itemsize
    processor.close()

def test_toggling_noise_reduction_clears_rnnoise_evidence(monkeypatch) -> None:
    class _Denoiser:
        def close(self) -> None:
            return None

    processor = AudioProcessor(
        input_sample_rate=48_000,
        output_sample_rate=48_000,
        noise_reduce_enabled=False,
        agc_enabled=False,
        limiter_enabled=False,
    )
    monkeypatch.setattr(
        processor,
        "_init_denoiser",
        lambda: setattr(processor, "_denoiser", _Denoiser()),
    )
    processor._last_speech_prob = 0.9
    processor._rnnoise_frame_count = 2
    processor._rnnoise_peak = 0.9
    processor._rnnoise_mean = 0.8
    processor._rnnoise_last = 0.7
    processor._rnnoise_ema = 0.6
    processor._rnnoise_ema_state = 0.5

    processor.set_enabled(True)

    assert processor.speech_probability == 0.0
    assert processor.rnnoise_frame_count == 0
    assert processor.rnnoise_probability_peak is None
    assert processor.rnnoise_probability_mean is None
    assert processor.rnnoise_probability_last is None
    assert processor.rnnoise_probability_ema is None
    assert processor._rnnoise_ema_state is None

    processor._last_speech_prob = 0.3
    processor._rnnoise_frame_count = 1
    processor._rnnoise_peak = 0.3
    processor._rnnoise_mean = 0.2
    processor._rnnoise_last = 0.1
    processor._rnnoise_ema = 0.4
    processor._rnnoise_ema_state = 0.4
    processor.set_enabled(True)
    assert processor.rnnoise_probability_ema == 0.4
    assert processor._rnnoise_ema_state == 0.4

    processor.set_enabled(False)
    assert processor.speech_probability == 0.0
    assert processor.rnnoise_frame_count == 0
    assert processor.rnnoise_probability_peak is None
    assert processor.rnnoise_probability_mean is None
    assert processor.rnnoise_probability_last is None
    assert processor.rnnoise_probability_ema is None
    assert processor._rnnoise_ema_state is None



@pytest.mark.asyncio
async def test_audio_close_waits_for_executor_chunk_processing() -> None:
    processing_started = threading.Event()
    release_processing = threading.Event()

    class _Processor:
        def __init__(self) -> None:
            self.close_calls = 0

        def process_chunk(self, audio_chunk: bytes) -> bytes:
            processing_started.set()
            assert release_processing.wait(timeout=2.0)
            return audio_chunk

        def save_debug_audio(self) -> None:
            return None

        def close(self) -> None:
            self.close_calls += 1

    client = object.__new__(OmniRealtimeClient)
    processor = _Processor()
    client._noise_reduction_enabled = True
    client._audio_processor = processor
    client._audio_processing_lock = asyncio.Lock()

    process_task = asyncio.create_task(client.process_audio_chunk_async(b"chunk"))
    assert await asyncio.to_thread(processing_started.wait, 2.0)
    close_task = asyncio.create_task(client._close_audio_processor())
    await asyncio.sleep(0)

    assert processor.close_calls == 0
    release_processing.set()

    assert await process_task == b"chunk"
    assert await close_task is None
    assert processor.close_calls == 1
    assert client._audio_processor is None


@pytest.mark.asyncio
async def test_audio_processing_drops_frame_after_processor_close() -> None:
    client = object.__new__(OmniRealtimeClient)
    client._audio_processor = None
    client._audio_processing_lock = asyncio.Lock()

    assert await client.process_audio_chunk_async(b"48khz-frame") == b""


@pytest.mark.asyncio
async def test_cancelled_audio_processing_keeps_lock_until_worker_finishes() -> None:
    processing_started = threading.Event()
    release_processing = threading.Event()

    class _Processor:
        def __init__(self) -> None:
            self.close_calls = 0

        def process_chunk(self, audio_chunk: bytes) -> bytes:
            processing_started.set()
            assert release_processing.wait(timeout=2.0)
            return audio_chunk

        def save_debug_audio(self) -> None:
            return None

        def close(self) -> None:
            self.close_calls += 1

    client = object.__new__(OmniRealtimeClient)
    processor = _Processor()
    client._audio_processor = processor
    client._audio_processing_lock = asyncio.Lock()

    process_task = asyncio.create_task(client.process_audio_chunk_async(b"chunk"))
    assert await asyncio.to_thread(processing_started.wait, 2.0)
    process_task.cancel()
    close_task = asyncio.create_task(client._close_audio_processor())
    await asyncio.sleep(0)

    assert processor.close_calls == 0
    assert not close_task.done()

    release_processing.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await process_task
    assert await close_task is None
    assert processor.close_calls == 1
    assert client._audio_processor is None


@pytest.mark.asyncio
async def test_live_noise_reduction_toggle_waits_for_chunk_processing() -> None:
    processing_started = threading.Event()
    release_processing = threading.Event()

    class _Processor:
        def __init__(self) -> None:
            self.enabled_calls: list[bool] = []

        def process_chunk(self, audio_chunk: bytes) -> bytes:
            processing_started.set()
            assert release_processing.wait(timeout=2.0)
            return audio_chunk

        def set_enabled(self, enabled: bool) -> None:
            self.enabled_calls.append(enabled)

    client = object.__new__(OmniRealtimeClient)
    processor = _Processor()
    client._audio_processor = processor
    client._audio_processing_lock = asyncio.Lock()

    process_task = asyncio.create_task(client.process_audio_chunk_async(b"chunk"))
    assert await asyncio.to_thread(processing_started.wait, 2.0)
    toggle_task = asyncio.create_task(client.set_audio_noise_reduction_enabled(False))
    await asyncio.sleep(0)

    assert processor.enabled_calls == []
    release_processing.set()

    assert await process_task == b"chunk"
    assert await toggle_task is None
    assert processor.enabled_calls == [False]
    assert client._noise_reduction_enabled is False


@pytest.mark.asyncio
async def test_audio_toggle_failure_does_not_escape_session_setup() -> None:
    class _Processor:
        def set_enabled(self, enabled: bool) -> None:
            raise RuntimeError("native close failed")

    client = object.__new__(OmniRealtimeClient)
    client._noise_reduction_enabled = True
    client._audio_processor = _Processor()
    client._audio_processing_lock = asyncio.Lock()

    assert await client.set_audio_noise_reduction_enabled(False) is None
    assert client._noise_reduction_enabled is False


def test_recreated_audio_processor_preserves_noise_reduction_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []

    def _create_processor(**kwargs):
        created.append(kwargs)
        return object()

    monkeypatch.setattr(client_module, "AudioProcessor", _create_processor)
    client = object.__new__(OmniRealtimeClient)
    client._noise_reduction_enabled = False
    client._on_silence_reset = lambda: None

    processor = client._create_audio_processor()

    assert processor is not None
    assert created[0]["noise_reduce_enabled"] is False


def test_initial_audio_processor_honors_noise_reduction_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []

    def _create_processor(**kwargs):
        created.append(kwargs)
        return object()

    monkeypatch.setattr(client_module, "AudioProcessor", _create_processor)

    client = OmniRealtimeClient(
        base_url="",
        api_key="",
        noise_reduction_enabled=False,
    )

    assert client._noise_reduction_enabled is False
    assert created[0]["noise_reduce_enabled"] is False


@pytest.mark.asyncio
async def test_audio_close_failure_does_not_escape_cleanup() -> None:
    class _Processor:
        def save_debug_audio(self) -> None:
            return None

        def close(self) -> None:
            raise RuntimeError("native close failed")

    client = object.__new__(OmniRealtimeClient)
    client._audio_processor = _Processor()
    client._audio_processing_lock = asyncio.Lock()

    assert await client._close_audio_processor() is None
    assert client._audio_processor is None
