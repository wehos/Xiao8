# -- coding: utf-8 --
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Audio Processor Module with RNNoise, AGC and Limiter
Audio preprocessing module using RNNoise deep-learning denoising, with built-in AGC and Limiter

RNNoise is a real-time noise suppression algorithm developed by Mozilla, using a GRU
neural network with only 13.3ms latency, suitable for real-time speech processing.

Processing chain: RNNoise -> AGC -> Limiter -> downsampling

AGC (Automatic Gain Control): keeps the volume stable
Limiter: prevents audio clipping

Important: RNNoise's GRU state drifts while processing background noise,
and must be reset once end of speech is detected.
"""

from contextlib import suppress

import numpy as np
from typing import Optional
from utils.logger_config import get_module_logger
import soxr
import time
import os
import wave

logger = get_module_logger(__name__)

# ============== DEBUG 音频存储功能 ==============
# 设置为 True 可以将 RNNoise 处理前后的音频存储到文件中
# 用于对比降噪效果
DEBUG_SAVE_AUDIO = False
DEBUG_AUDIO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_audio")
# ===============================================

# Direct ctypes loading of rnnoise native library.
# We bypass ALL pyrnnoise Python code because pyrnnoise/__init__.py
# unconditionally imports pyrnnoise.pyrnnoise which imports audiolab,
# and audiolab is excluded from Nuitka via --nofollow-import-to.
# Python's import system executes __init__.py even for submodule imports,
# so there is no way to import pyrnnoise.rnnoise without triggering audiolab.
import sys
import ctypes
import platform
import importlib.util

_rnnoise_lib = None
_rnnoise_available = None


def _find_rnnoise_dll() -> str:
    """Locate rnnoise native library inside pyrnnoise package-data."""
    names = {
        "Windows": "rnnoise.dll",
        "Darwin": "librnnoise.dylib",
        "Linux": "librnnoise.so",
    }
    lib_name = names.get(platform.system())
    if lib_name is None:
        raise OSError(f"Unsupported platform: {platform.system()}")

    tried: list[str] = []

    # 1. find_spec — normal Python / venv / uv (does NOT execute __init__.py)
    spec = importlib.util.find_spec("pyrnnoise")
    if spec is not None and spec.submodule_search_locations:
        for search_dir in spec.submodule_search_locations:
            candidate = os.path.join(search_dir, lib_name)
            tried.append(candidate)
            if os.path.isfile(candidate):
                return candidate

    # 2. Nuitka standalone — --include-data-files places it at <exe_dir>/pyrnnoise/
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for sub in ("pyrnnoise", "."):
        candidate = os.path.join(exe_dir, sub, lib_name)
        tried.append(candidate)
        if os.path.isfile(candidate):
            return candidate

    raise OSError(f"{lib_name} not found. Searched: {tried}")


def _load_rnnoise_native():
    """Load rnnoise native library and return a lightweight API object."""
    lib_path = _find_rnnoise_dll()
    lib = ctypes.CDLL(lib_path)

    lib.rnnoise_create.argtypes = [ctypes.c_void_p]
    lib.rnnoise_create.restype = ctypes.c_void_p
    lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
    lib.rnnoise_destroy.restype = None
    lib.rnnoise_get_frame_size.argtypes = []
    lib.rnnoise_get_frame_size.restype = ctypes.c_int
    lib.rnnoise_process_frame.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
    ]
    lib.rnnoise_process_frame.restype = ctypes.c_float

    frame_size = lib.rnnoise_get_frame_size()

    class _Lib:
        FRAME_SIZE = frame_size
        SAMPLE_RATE = 48000

        @staticmethod
        def create():
            return lib.rnnoise_create(None)

        @staticmethod
        def destroy(state):
            lib.rnnoise_destroy(state)

        @staticmethod
        def process_mono_frame(state, frame):
            if frame.dtype == np.int16:
                frame = frame.astype(np.float32)
            else:
                frame = (frame * 32767).astype(np.float32)
            n = len(frame)
            if n < frame_size:
                frame = np.pad(frame, (0, frame_size - n))
            ptr = frame.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            prob = lib.rnnoise_process_frame(state, ptr, ptr)
            return np.clip(np.round(frame), -32768, 32767).astype(np.int16)[:n], float(prob)

    return _Lib()


def _get_rnnoise():
    """Lazy load rnnoise native library."""
    global _rnnoise_lib, _rnnoise_available
    if _rnnoise_available is None:
        try:
            _rnnoise_lib = _load_rnnoise_native()
            _rnnoise_available = True
            logger.info(f"✅ rnnoise loaded (frame_size={_rnnoise_lib.FRAME_SIZE})")
        except Exception as e:
            logger.warning(f"⚠️ rnnoise not available: {e}")
            _rnnoise_available = False
    return _rnnoise_lib if _rnnoise_available else None


class _LiteDenoiser:
    """Lightweight RNNoise wrapper using direct ctypes calls.
    
    No pyrnnoise / audiolab / av Python imports — only the native DLL.
    """

    def __init__(self, rnnoise_lib):
        self._lib = rnnoise_lib
        self._state = rnnoise_lib.create()
        if not self._state:
            raise RuntimeError("rnnoise_create() returned NULL — native library failed to initialise")

    def process_frame(self, frame_int16: np.ndarray):
        """Process one mono 480-sample int16 frame.
        
        Returns (denoised_int16, speech_prob_float).
        """
        return self._lib.process_mono_frame(self._state, frame_int16)

    def reset(self):
        new_state = self._lib.create()
        if not new_state:
            raise RuntimeError("rnnoise_create() returned NULL during reset")
        old_state = self._state
        self._state = new_state
        if old_state:
            self._lib.destroy(old_state)

    def close(self) -> None:
        """Destroy the owned native RNNoise state exactly once."""
        state = self._state
        self._state = None
        if state:
            self._lib.destroy(state)

    def __del__(self):
        # Finalization may run during interpreter teardown; never let native
        # cleanup errors escape or depend on the logging subsystem still existing.
        with suppress(Exception):
            self.close()


class AudioProcessor:
    """
    Real-time audio processor using RNNoise for noise reduction,
    with built-in AGC (Automatic Gain Control) and Limiter.
    
    Processing chain: RNNoise -> AGC -> Limiter -> Resample
    
    RNNoise requires 48kHz audio with 480-sample frames (10ms).
    After processing, audio is downsampled to 16kHz for API compatibility.
    
    IMPORTANT: Call reset() after each speech turn to clear RNNoise's
    internal GRU state and prevent state drift during silence/background.
    
    Thread Safety:
        This class is NOT safe for concurrent use. The following mutable
        state is unprotected: _frame_buffer, _last_speech_prob,
        _last_speech_time, _needs_reset, _denoiser.
        
        Callers must NOT invoke process_chunk() or reset() from multiple
        threads or coroutines simultaneously. If concurrent access is
        required, wrap calls with an external lock (e.g., threading.Lock
        for threads or asyncio.Lock for async coroutines).
    """
    
    RNNOISE_SAMPLE_RATE = 48000  # RNNoise requires 48kHz
    RNNOISE_FRAME_SIZE = 480     # 10ms at 48kHz
    RNNOISE_SPEECH_PROBABILITY_THRESHOLD = 0.2
    RNNOISE_EMA_ALPHA = 0.35
    API_SAMPLE_RATE = 16000      # API expects 16kHz
    
    # Reset denoiser if no speech detected for this many seconds
    RESET_TIMEOUT_SECONDS = 4.0
    
    # AGC Configuration
    AGC_TARGET_LEVEL = 0.25        # Target RMS level (0.0-1.0), raised for easier VAD trigger
    AGC_MAX_GAIN = 20.0            # Maximum gain multiplier (safe with noise floor protection)
    AGC_MIN_GAIN = 0.25            # Minimum gain multiplier
    AGC_NOISE_FLOOR = 0.015        # RMS below this = silence/noise, don't increase gain
    AGC_ATTACK_TIME = 0.01         # Attack time in seconds (fast response to peaks)
    AGC_RELEASE_TIME = 0.4         # Release time in seconds (slow return to normal)
    
    # Limiter Configuration
    LIMITER_THRESHOLD = 0.95       # Threshold before limiting (0.0-1.0)
    LIMITER_KNEE = 0.05            # Soft knee width
    
    def __init__(
        self,
        input_sample_rate: int = 48000,
        output_sample_rate: int = 16000,
        noise_reduce_enabled: bool = True,
        agc_enabled: bool = True,
        limiter_enabled: bool = True,
        on_silence_reset: Optional[callable] = None
    ):
        self.input_sample_rate = input_sample_rate
        self.output_sample_rate = output_sample_rate
        self.noise_reduce_enabled = noise_reduce_enabled
        self.agc_enabled = agc_enabled
        self.limiter_enabled = limiter_enabled
        # 静音重置回调：当检测到4秒静音并重置状态时调用
        self.on_silence_reset = on_silence_reset
        
        # Initialize RNNoise denoiser
        self._denoiser = None
        self._init_denoiser()
        
        # Fixed-capacity pending buffer. A processed call can leave at most one
        # incomplete RNNoise frame, so a growing/ring buffer is unnecessary.
        self._frame_buffer = np.empty(self.RNNOISE_FRAME_SIZE, dtype=np.int16)
        self._frame_buffer_size = 0
        
        # Track voice activity for auto-reset
        self._last_speech_prob = 0.0
        self._last_speech_time = time.time()
        self._needs_reset = False
        self._rnnoise_frame_count = 0
        self._rnnoise_peak: float | None = None
        self._rnnoise_mean: float | None = None
        self._rnnoise_last: float | None = None
        self._rnnoise_ema: float | None = None
        self._rnnoise_ema_state: float | None = None
        
        # AGC state
        self._agc_gain = 1.0
        self._agc_attack_coeff = np.exp(-1.0 / (self.AGC_ATTACK_TIME * self.RNNOISE_SAMPLE_RATE))
        self._agc_release_coeff = np.exp(-1.0 / (self.AGC_RELEASE_TIME * self.RNNOISE_SAMPLE_RATE))

        # Streaming downsample resampler: maintains FIR state across chunks.
        # Stateless soxr.resample() on 10ms chunks produces edge artifacts at every
        # chunk boundary (perceived as 100Hz periodic clicks → "电流声"), so we use
        # ResampleStream which carries filter state and outputs a continuous signal.
        if self.input_sample_rate != self.output_sample_rate:
            self._downsample_resampler = soxr.ResampleStream(
                self.input_sample_rate,
                self.output_sample_rate,
                1,
                dtype='float32',
                quality='HQ',
            )
        else:
            self._downsample_resampler = None
        self._stream_finalized = False
        self._closed = False
        
        # Debug audio buffers - 累积存储完整音频
        self._debug_audio_before: list[np.ndarray] = []
        self._debug_audio_after: list[np.ndarray] = []
        if DEBUG_SAVE_AUDIO:
            os.makedirs(DEBUG_AUDIO_DIR, exist_ok=True)
            logger.info(f"🔧 DEBUG: 音频录制已启用，文件将保存到 {DEBUG_AUDIO_DIR}")
        
        logger.info(f"🎤 AudioProcessor initialized: input={input_sample_rate}Hz, "
                   f"output={output_sample_rate}Hz, rnnoise={self._denoiser is not None}, "
                   f"agc={agc_enabled}, limiter={limiter_enabled}")
    
    def _init_denoiser(self) -> None:
        """Initialize RNNoise denoiser if available."""
        if not self.noise_reduce_enabled:
            return
        
        if self.input_sample_rate != self.RNNOISE_SAMPLE_RATE:
            logger.warning(
                f"⚠️ Skipping RNNoise initialization: input sample rate "
                f"{self.input_sample_rate}Hz != required {self.RNNOISE_SAMPLE_RATE}Hz"
            )
            return
            
        rnnoise_mod = _get_rnnoise()
        if rnnoise_mod:
            try:
                self._denoiser = _LiteDenoiser(rnnoise_mod)
                logger.info("🔊 RNNoise denoiser initialized")
            except Exception:  # noqa: BLE001 - RNNoise can fail for various reasons (missing libs, bad state); must catch all to ensure graceful fallback
                logger.exception("❌ Failed to initialize RNNoise")
                self._denoiser = None
    
    def process_chunk(self, audio_bytes: bytes) -> bytes:
        """
        Process a chunk of PCM16 audio data.
        
        Args:
            audio_bytes: Raw PCM16 audio bytes at input_sample_rate (48kHz)
            
        Returns:
            Processed audio as PCM16 bytes at output_sample_rate (16kHz)
        """
        self._require_mutable()
        # Keep as int16 - pyrnnoise expects int16!
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        
        # Check if we need to reset (after long silence or on request)
        current_time = time.time()
        silence_triggered = (current_time - self._last_speech_time > self.RESET_TIMEOUT_SECONDS)
        if self._needs_reset or silence_triggered:
            if self._denoiser is not None:
                self._reset_internal_state()
                self._last_speech_time = current_time  # Prevent infinite reset loop
                logger.debug("🔄 RNNoise state auto-reset after silence")
                # 调用静音重置回调（仅在静音触发时，非手动请求时）
                if silence_triggered and self.on_silence_reset:
                    try:
                        self.on_silence_reset()
                    except Exception as e:
                        logger.error(f"❌ on_silence_reset callback error: {e}")
            self._needs_reset = False
        
        # Apply RNNoise if available (processes int16, returns int16)
        if self._denoiser is not None and self.noise_reduce_enabled:
            # DEBUG: 记录 RNNoise 处理前的音频
            if DEBUG_SAVE_AUDIO:
                self._debug_audio_before.append(audio_int16.copy())
            
            processed = self._process_with_rnnoise(audio_int16)
            if len(processed) == 0:
                return b''  # Buffering
            
            # DEBUG: 记录 RNNoise 处理后的音频
            if DEBUG_SAVE_AUDIO:
                self._debug_audio_after.append(processed.copy())
            
            audio_int16 = processed
        
        # Apply AGC (Automatic Gain Control) after RNNoise
        if self.agc_enabled and len(audio_int16) > 0:
            audio_int16 = self._apply_agc(audio_int16)
        
        # Apply Limiter to prevent clipping
        if self.limiter_enabled and len(audio_int16) > 0:
            audio_int16 = self._apply_limiter(audio_int16)
        
        # Downsample using streaming resampler (maintains FIR state across chunks
        # to avoid boundary artifacts; see __init__ for context).
        if self._downsample_resampler is not None and len(audio_int16) > 0:
            audio_float = audio_int16.astype(np.float32) / 32768.0
            audio_float = self._downsample_resampler.resample_chunk(audio_float)
            audio_int16 = self._float_to_pcm16(audio_float)
        return audio_int16.tobytes()

    @staticmethod
    def _float_to_pcm16(audio_float: np.ndarray) -> np.ndarray:
        """Use one conversion contract for streamed audio and the EOF tail."""

        return (audio_float * 32768.0).clip(-32768, 32767).astype(np.int16)

    def _require_mutable(self) -> None:
        if getattr(self, "_closed", False):
            raise RuntimeError("AUDIO_PROCESSOR_CLOSED")
        if getattr(self, "_stream_finalized", False):
            raise RuntimeError("AUDIO_PROCESSOR_STREAM_FINALIZED")

    def finalize_stream(self) -> bytes:
        """Flush the streaming resampler once and enter terminal state."""

        if getattr(self, "_closed", False):
            raise RuntimeError("AUDIO_PROCESSOR_CLOSED")
        if getattr(self, "_stream_finalized", False):
            raise RuntimeError("AUDIO_PROCESSOR_STREAM_FINALIZED")
        # Latch before entering native code: a failed EOF call is not safe to
        # retry because the resampler may already have consumed its tail.
        self._stream_finalized = True
        if self._frame_buffer_size:
            raise RuntimeError("AUDIO_PROCESSOR_INCOMPLETE_RNNOISE_FRAME")
        resampler = self._downsample_resampler
        if resampler is None:
            return b""
        audio_float = resampler.resample_chunk(
            np.empty(0, dtype=np.float32),
            last=True,
        )
        return self._float_to_pcm16(audio_float).tobytes()
    
    def _process_with_rnnoise(self, audio: np.ndarray) -> np.ndarray:
        """Process audio through RNNoise frame by frame.
        
        Args:
            audio: int16 numpy array
            
        Returns:
            Denoised int16 numpy array
        """
        self._rnnoise_frame_count = 0
        self._rnnoise_peak = None
        self._rnnoise_mean = None
        self._rnnoise_last = None
        self._rnnoise_ema = None
        frame_probabilities: list[float] = []
        pending_size = self._frame_buffer_size
        max_buffer_samples = self.RNNOISE_SAMPLE_RATE

        # Preserve the previous one-second overflow policy without first
        # concatenating the pending samples and the new chunk.
        drop_samples = pending_size + len(audio) - max_buffer_samples
        if drop_samples > 0:
            if drop_samples < pending_size:
                retained = pending_size - drop_samples
                self._frame_buffer[:retained] = self._frame_buffer[
                    drop_samples:pending_size
                ].copy()
                pending_size = retained
            else:
                audio = audio[drop_samples - pending_size :]
                pending_size = 0

        frame_count = (pending_size + len(audio)) // self.RNNOISE_FRAME_SIZE
        if frame_count == 0:
            self._frame_buffer[pending_size : pending_size + len(audio)] = audio
            self._frame_buffer_size = pending_size + len(audio)
            return np.empty(0, dtype=np.int16)

        output = np.empty(frame_count * self.RNNOISE_FRAME_SIZE, dtype=np.int16)
        input_offset = 0
        output_offset = 0

        def process_frame(frame: np.ndarray) -> None:
            nonlocal output_offset
            try:
                denoised, prob = self._denoiser.process_frame(frame)
                probability = min(1.0, max(0.0, float(prob)))
                frame_probabilities.append(probability)
                self._last_speech_prob = probability
                if probability > self.RNNOISE_SPEECH_PROBABILITY_THRESHOLD:
                    self._last_speech_time = time.time()
                output[output_offset : output_offset + self.RNNOISE_FRAME_SIZE] = denoised
            except Exception as e:
                logger.error(f"❌ RNNoise processing error: {e}")
                output[output_offset : output_offset + self.RNNOISE_FRAME_SIZE] = frame
            output_offset += self.RNNOISE_FRAME_SIZE

        if pending_size:
            needed = self.RNNOISE_FRAME_SIZE - pending_size
            self._frame_buffer[pending_size:] = audio[:needed]
            input_offset = needed
            process_frame(self._frame_buffer)
            self._frame_buffer.fill(0)

        while input_offset + self.RNNOISE_FRAME_SIZE <= len(audio):
            frame = audio[input_offset : input_offset + self.RNNOISE_FRAME_SIZE]
            process_frame(frame)
            input_offset += self.RNNOISE_FRAME_SIZE

        remaining = len(audio) - input_offset
        if remaining:
            self._frame_buffer[:remaining] = audio[input_offset:]
        self._frame_buffer_size = remaining
        if frame_probabilities:
            self._rnnoise_frame_count = len(frame_probabilities)
            self._rnnoise_peak = max(frame_probabilities)
            self._rnnoise_mean = sum(frame_probabilities) / len(frame_probabilities)
            self._rnnoise_last = frame_probabilities[-1]
            ema_state = getattr(self, "_rnnoise_ema_state", None)
            for probability in frame_probabilities:
                if ema_state is None:
                    ema_state = probability
                else:
                    alpha = self.RNNOISE_EMA_ALPHA
                    ema_state = alpha * probability + (1.0 - alpha) * ema_state
            self._rnnoise_ema_state = ema_state
            self._rnnoise_ema = ema_state
        return output
    
    def _clear_rnnoise_evidence(self) -> None:
        """Clear per-chunk evidence and the cross-chunk EMA accumulator."""
        self._last_speech_prob = 0.0
        self._rnnoise_frame_count = 0
        self._rnnoise_peak = None
        self._rnnoise_mean = None
        self._rnnoise_last = None
        self._rnnoise_ema = None
        self._rnnoise_ema_state = None

    def _reset_internal_state(self) -> None:
        """Reset RNNoise internal state without full reinitialization."""
        self._frame_buffer.fill(0)
        self._frame_buffer_size = 0
        self._clear_rnnoise_evidence()
        # Reset AGC gain state
        self._agc_gain = 1.0
        # Reset denoiser GRU hidden states (do not reinitialize)
        if self._denoiser is not None:
            try:
                self._denoiser.reset()
            except Exception as e:
                logger.warning(f"⚠️ Failed to reset RNNoise denoiser: {e}")
        # Flush streaming resampler's latency buffer + FIR history. After
        # multi-second silence the buffer is already silent so this is a no-op,
        # but on a forced mid-speech reset (interrupt / cancel turn) it prevents
        # previous-turn tail samples from bleeding into the next turn.
        if self._downsample_resampler is not None:
            try:
                self._downsample_resampler.clear()
            except Exception as e:
                logger.warning(f"⚠️ Failed to clear downsample resampler: {e}")
    
    def reset(self) -> None:
        """
        Reset the processor state. Call this after each speech turn ends
        to prevent RNNoise state drift during silence/background noise.
        """
        self._require_mutable()
        self._reset_internal_state()
        self._last_speech_time = time.time()
        logger.info("🔄 AudioProcessor state reset (external call)")

    def close(self) -> None:
        """Release native denoiser and streaming-buffer resources."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        denoiser = self._denoiser
        self._denoiser = None
        self._downsample_resampler = None
        self._frame_buffer = np.array([], dtype=np.int16)
        self._debug_audio_before.clear()
        self._debug_audio_after.clear()
        if denoiser is not None:
            close = getattr(denoiser, "close", None)
            if callable(close):
                close()
    
    def request_reset(self) -> None:
        """Request a reset on the next process_chunk call."""
        self._require_mutable()
        self._needs_reset = True
    
    def save_debug_audio(self) -> None:
        """
        Save the accumulated debug audio to WAV files.
        Two files are written:
        - debug_audio_before.wav: raw audio before RNNoise processing
        - debug_audio_after.wav: denoised audio after RNNoise processing
        
        Calling this method clears the debug buffers.
        """
        if not DEBUG_SAVE_AUDIO:
            return
        
        if not self._debug_audio_before and not self._debug_audio_after:
            logger.warning("⚠️ 没有可保存的 debug 音频数据")
            return
        
        # 合并所有音频片段
        if self._debug_audio_before:
            audio_before = np.concatenate(self._debug_audio_before)
            before_path = os.path.join(DEBUG_AUDIO_DIR, "debug_audio_before.wav")
            self._save_wav(before_path, audio_before, self.RNNOISE_SAMPLE_RATE)
            logger.info(f"💾 已保存处理前音频: {before_path} ({len(audio_before)/self.RNNOISE_SAMPLE_RATE:.2f}秒)")
        
        if self._debug_audio_after:
            audio_after = np.concatenate(self._debug_audio_after)
            after_path = os.path.join(DEBUG_AUDIO_DIR, "debug_audio_after.wav")
            self._save_wav(after_path, audio_after, self.RNNOISE_SAMPLE_RATE)
            logger.info(f"💾 已保存处理后音频: {after_path} ({len(audio_after)/self.RNNOISE_SAMPLE_RATE:.2f}秒)")
        
        # 清空缓冲区
        self._debug_audio_before.clear()
        self._debug_audio_after.clear()
        logger.info("🔧 DEBUG: 音频已保存，缓冲区已清空")
    
    def _save_wav(self, filepath: str, audio: np.ndarray, sample_rate: int) -> None:
        """Save int16 audio data as a WAV file."""
        with wave.open(filepath, 'wb') as wf:
            wf.setnchannels(1)  # mono
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(sample_rate)
            wf.writeframes(audio.tobytes())
    
    @property
    def speech_probability(self) -> float:
        """Get the last detected speech probability (0.0-1.0)."""
        return self._last_speech_prob

    @property
    def rnnoise_frame_count(self) -> int:
        return self._rnnoise_frame_count

    @property
    def rnnoise_available(self) -> bool:
        """Whether this processor can currently produce RNNoise evidence."""

        return bool(self.noise_reduce_enabled and self._denoiser is not None)

    @property
    def rnnoise_probability_peak(self) -> float | None:
        return self._rnnoise_peak

    @property
    def rnnoise_probability_mean(self) -> float | None:
        return self._rnnoise_mean

    @property
    def rnnoise_probability_last(self) -> float | None:
        return self._rnnoise_last

    @property
    def rnnoise_probability_ema(self) -> float | None:
        return self._rnnoise_ema
    
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable noise reduction."""
        prev = self.noise_reduce_enabled
        self.noise_reduce_enabled = enabled
        if enabled:
            if self._denoiser is None:
                self._init_denoiser()
            if (
                self._denoiser is not None
                and self._frame_buffer.size != self.RNNOISE_FRAME_SIZE
            ):
                self._frame_buffer = np.empty(self.RNNOISE_FRAME_SIZE, dtype=np.int16)
        else:
            if self._denoiser is not None:
                self._denoiser.close()
                self._denoiser = None
            self._frame_buffer = np.array([], dtype=np.int16)
        if prev != enabled:
            if self._frame_buffer.size:
                self._frame_buffer.fill(0)
            self._frame_buffer_size = 0
            self._agc_gain = 1.0
            self._clear_rnnoise_evidence()
        logger.info(f"🎤 Noise reduction {'enabled' if enabled else 'disabled'}")
    
    def set_agc_enabled(self, enabled: bool) -> None:
        """Enable or disable AGC."""
        self.agc_enabled = enabled
        if enabled:
            self._agc_gain = 1.0  # Reset gain when re-enabling
        logger.info(f"🎤 AGC {'enabled' if enabled else 'disabled'}")
    
    def set_limiter_enabled(self, enabled: bool) -> None:
        """Enable or disable Limiter."""
        self.limiter_enabled = enabled
        logger.info(f"🎤 Limiter {'enabled' if enabled else 'disabled'}")
    
    def _apply_agc(self, audio: np.ndarray) -> np.ndarray:
        """
        Apply Automatic Gain Control to normalize audio levels.
        
        Uses a simple peak-following AGC with attack/release dynamics.
        
        Args:
            audio: int16 numpy array
            
        Returns:
            Gain-adjusted int16 numpy array
        """
        # Convert to float for processing
        audio_float = audio.astype(np.float32) / 32768.0
        
        # Calculate RMS of the current chunk
        rms = np.sqrt(np.mean(audio_float ** 2) + 1e-10)
        
        # Calculate desired gain with noise floor protection
        if rms > self.AGC_NOISE_FLOOR:
            # Real signal detected - calculate normal gain
            desired_gain = self.AGC_TARGET_LEVEL / rms
            desired_gain = np.clip(desired_gain, self.AGC_MIN_GAIN, self.AGC_MAX_GAIN)
        else:
            # Below noise floor: don't increase gain to avoid amplifying background noise
            # Only allow gain to stay same or decrease, cap at 1.0
            desired_gain = min(self._agc_gain, 1.0)
        
        # Smooth gain changes using attack/release coefficients
        if desired_gain < self._agc_gain:
            # Attack: fast response to loud signals
            self._agc_gain = (self._agc_attack_coeff * self._agc_gain + 
                             (1 - self._agc_attack_coeff) * desired_gain)
        else:
            # Release: slow return to higher gain
            self._agc_gain = (self._agc_release_coeff * self._agc_gain + 
                             (1 - self._agc_release_coeff) * desired_gain)
        
        # Apply gain
        audio_float = audio_float * self._agc_gain
        
        # Convert back to int16 (clipping will be handled by limiter)
        return (audio_float * 32768.0).clip(-32768, 32767).astype(np.int16)
    
    def _apply_limiter(self, audio: np.ndarray) -> np.ndarray:
        """
        Apply a soft limiter to prevent clipping.
        
        Uses a soft-knee limiter to gently compress peaks above threshold.
        
        Args:
            audio: int16 numpy array
            
        Returns:
            Limited int16 numpy array
        """
        # Convert to float (-1.0 to 1.0 range)
        audio_float = audio.astype(np.float32) / 32768.0
        
        # Apply soft-knee limiting
        threshold = self.LIMITER_THRESHOLD
        knee = self.LIMITER_KNEE
        
        # Calculate threshold boundaries
        knee_start = threshold - knee / 2
        knee_end = threshold + knee / 2
        
        # Get absolute values for comparison
        abs_audio = np.abs(audio_float)
        
        # Apply soft knee compression
        # Below knee_start: pass through
        # In knee region: gentle compression
        # Above knee_end: hard limiting
        
        output = np.copy(audio_float)
        
        # Knee region (soft transition)
        in_knee = (abs_audio > knee_start) & (abs_audio <= knee_end)
        if np.any(in_knee):
            # Quadratic compression in knee region
            knee_ratio = (abs_audio[in_knee] - knee_start) / knee
            compression = 1 - 0.5 * knee_ratio ** 2
            output[in_knee] = np.sign(audio_float[in_knee]) * (
                knee_start + (abs_audio[in_knee] - knee_start) * compression
            )
        
        # Above knee (hard limiting with soft saturation)
        above_knee = abs_audio > knee_end
        if np.any(above_knee):
            # Soft saturation using tanh
            excess = abs_audio[above_knee] - threshold
            limited = threshold + 0.5 * np.tanh(excess * 2) * (1 - threshold)
            output[above_knee] = np.sign(audio_float[above_knee]) * limited
        
        # Final clip to ensure no samples exceed 1.0
        output = np.clip(output, -1.0, 1.0)
        
        # Convert back to int16
        return (output * 32768.0).clip(-32768, 32767).astype(np.int16)
