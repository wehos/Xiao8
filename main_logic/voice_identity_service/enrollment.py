"""Audio quality, speech, and embedding gates for local Owner enrollment."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
import threading
from typing import Protocol

import numpy as np

from main_logic.asr_client.endpointing.silero_vad import SileroVad


ENROLLMENT_SAMPLE_RATE_HZ = 16_000
ENROLLMENT_MINIMUM_AUDIO_MS = 1_500
ENROLLMENT_TARGET_AUDIO_MS = 4_000
ENROLLMENT_VERIFICATION_AUDIO_MS = 5_000
ENROLLMENT_MINIMUM_PCM_BYTES = (
    ENROLLMENT_SAMPLE_RATE_HZ * ENROLLMENT_MINIMUM_AUDIO_MS // 1_000 * 2
)
ENROLLMENT_MAXIMUM_PCM_BYTES = (
    ENROLLMENT_SAMPLE_RATE_HZ * ENROLLMENT_TARGET_AUDIO_MS // 1_000 * 2
)
ENROLLMENT_VERIFICATION_MAXIMUM_PCM_BYTES = (
    ENROLLMENT_SAMPLE_RATE_HZ * ENROLLMENT_VERIFICATION_AUDIO_MS // 1_000 * 2
)
_FRAME_SAMPLES = 320
_ACTIVE_FRAME_RMS = 0.008
_MAX_CLIPPED_SAMPLE_RATIO = 0.05
ENROLLMENT_SPEECH_PROBABILITY_THRESHOLD = 0.5
ENROLLMENT_MINIMUM_SPEECH_WINDOWS = 47
ENROLLMENT_SIMILARITY_THRESHOLD = 0.40


class EnrollmentAudioError(ValueError):
    """A stable, UI-safe rejection reason for enrollment PCM."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EnrollmentSpeechValidatorUnavailableError(RuntimeError):
    """Raised when enrollment speech validation cannot fail closed."""


@dataclass(frozen=True, slots=True)
class EnrollmentSpeechResult:
    """Probability-free aggregate evidence for one accepted segment."""

    window_count: int
    active_window_count: int


@dataclass(frozen=True, slots=True)
class EnrollmentVerificationResult:
    """Privacy-safe result for one independent verification segment."""

    passed: bool
    match_percent: int

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "match_percent": self.match_percent,
        }


class EnrollmentSpeechValidator(Protocol):
    """Asynchronous, enrollment-owned real-speech validator."""

    async def load(self) -> bool: ...

    async def validate_pcm16(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int = ENROLLMENT_SAMPLE_RATE_HZ,
    ) -> EnrollmentSpeechResult: ...

    async def close(self) -> None: ...


EnrollmentSpeechValidatorFactory = Callable[[], EnrollmentSpeechValidator]


class _EnrollmentSileroBackend(Protocol):
    @property
    def is_ready(self) -> bool: ...

    def load(self) -> bool: ...

    def reset_stream(self) -> None: ...

    def process_pcm16(self, pcm16_le: bytes) -> list[float]: ...

    def close(self) -> None: ...


class SileroEnrollmentSpeechValidator:
    """Own one Silero runtime and reset it before every enrollment segment."""

    def __init__(
        self,
        *,
        asset_dir: Path | None = None,
        vad: _EnrollmentSileroBackend | None = None,
    ) -> None:
        self._vad: _EnrollmentSileroBackend = vad or SileroVad(
            enabled=True,
            asset_dir=asset_dir,
        )
        self._operation_lock = threading.Lock()
        self._closed = False

    async def load(self) -> bool:
        return bool(await asyncio.to_thread(self._load_sync))

    def _load_sync(self) -> bool:
        with self._operation_lock:
            if self._closed:
                return False
            return bool(self._vad.load())

    async def validate_pcm16(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int = ENROLLMENT_SAMPLE_RATE_HZ,
    ) -> EnrollmentSpeechResult:
        return await asyncio.to_thread(
            self._validate_sync,
            pcm16,
            sample_rate_hz,
        )

    def _validate_sync(
        self,
        pcm16: bytes,
        sample_rate_hz: int,
    ) -> EnrollmentSpeechResult:
        if sample_rate_hz != ENROLLMENT_SAMPLE_RATE_HZ:
            raise EnrollmentAudioError("invalid_pcm")
        validate_enrollment_pcm16(
            pcm16,
            maximum_pcm_bytes=ENROLLMENT_VERIFICATION_MAXIMUM_PCM_BYTES,
        )
        with self._operation_lock:
            if self._closed or not self._vad.is_ready:
                raise EnrollmentSpeechValidatorUnavailableError(
                    "model_unavailable"
                )
            try:
                self._vad.reset_stream()
                probabilities = self._vad.process_pcm16(pcm16)
            except Exception as exc:
                raise EnrollmentSpeechValidatorUnavailableError(
                    "model_unavailable"
                ) from exc
        active_window_count = sum(
            probability >= ENROLLMENT_SPEECH_PROBABILITY_THRESHOLD
            for probability in probabilities
        )
        if active_window_count < ENROLLMENT_MINIMUM_SPEECH_WINDOWS:
            raise EnrollmentAudioError("no_speech_detected")
        return EnrollmentSpeechResult(
            window_count=len(probabilities),
            active_window_count=active_window_count,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._vad.close()


def validate_enrollment_pcm16(
    pcm16: bytes,
    *,
    maximum_pcm_bytes: int = ENROLLMENT_MAXIMUM_PCM_BYTES,
) -> None:
    """Accept usable 16 kHz mono PCM16 without retaining derived samples."""

    if type(pcm16) is not bytes or len(pcm16) % 2:
        raise EnrollmentAudioError("invalid_pcm")
    if len(pcm16) < ENROLLMENT_MINIMUM_PCM_BYTES:
        raise EnrollmentAudioError("speech_too_short")
    if len(pcm16) > maximum_pcm_bytes:
        raise EnrollmentAudioError("audio_too_long")

    samples: np.ndarray | None = None
    normalized: np.ndarray | None = None
    frames: np.ndarray | None = None
    try:
        samples = np.frombuffer(pcm16, dtype="<i2")
        if samples.size == 0:
            raise EnrollmentAudioError("silence")
        clipped = np.count_nonzero(np.abs(samples.astype(np.int32)) >= 32_760)
        if clipped / samples.size > _MAX_CLIPPED_SAMPLE_RATIO:
            raise EnrollmentAudioError("severe_clipping")

        complete_samples = samples.size - samples.size % _FRAME_SAMPLES
        if complete_samples < _FRAME_SAMPLES:
            raise EnrollmentAudioError("speech_too_short")
        normalized = samples[:complete_samples].astype(np.float32)
        normalized /= np.float32(32_768.0)
        frames = normalized.reshape(-1, _FRAME_SAMPLES)
        rms = np.sqrt(
            np.mean(frames * frames, axis=1, dtype=np.float32),
            dtype=np.float32,
        )
        active_frames = int(np.count_nonzero(rms >= _ACTIVE_FRAME_RMS))
        required_frames = math.ceil(
            ENROLLMENT_MINIMUM_AUDIO_MS
            / (_FRAME_SAMPLES * 1_000 / ENROLLMENT_SAMPLE_RATE_HZ)
        )
        if active_frames < required_frames:
            raise EnrollmentAudioError("volume_too_low")
    finally:
        if frames is not None:
            frames.fill(0.0)
        if normalized is not None:
            normalized.fill(0.0)


def wipe_enrollment_embedding(embedding: np.ndarray | None) -> None:
    """Best-effort overwrite for task- or session-owned biometric arrays."""

    if embedding is None:
        return
    try:
        if not embedding.flags.writeable:
            embedding.setflags(write=True)
        embedding.fill(0.0)
    except Exception:
        pass


def _normalized_embedding_copy(embedding: np.ndarray) -> np.ndarray:
    if (
        type(embedding) is not np.ndarray
        or embedding.ndim != 1
        or embedding.size == 0
        or not np.issubdtype(embedding.dtype, np.floating)
        or not np.isfinite(embedding).all()
    ):
        raise ValueError("invalid enrollment embedding")
    normalized = np.array(embedding, dtype=np.float32, order="C", copy=True)
    try:
        norm = math.sqrt(float(np.dot(normalized, normalized)))
        if not math.isfinite(norm) or norm <= 1e-12:
            raise ValueError("invalid enrollment embedding norm")
        np.divide(normalized, np.float32(norm), out=normalized)
        return normalized
    except BaseException:
        wipe_enrollment_embedding(normalized)
        raise


def _normalized_embedding_sum(
    embeddings: Sequence[np.ndarray],
) -> np.ndarray:
    if not embeddings:
        raise ValueError("enrollment embeddings are required")
    shape = embeddings[0].shape
    result = np.zeros(shape, dtype=np.float32)
    try:
        for embedding in embeddings:
            if embedding.shape != shape:
                raise ValueError("enrollment embedding dimensions differ")
            np.add(result, embedding, out=result)
        norm = math.sqrt(float(np.dot(result, result)))
        if not math.isfinite(norm) or norm <= 1e-12:
            raise ValueError("invalid enrollment centroid norm")
        np.divide(result, np.float32(norm), out=result)
        return result
    except BaseException:
        wipe_enrollment_embedding(result)
        raise


def create_enrollment_reference_centroid(
    reference_embeddings: Sequence[np.ndarray],
) -> np.ndarray:
    """Validate three references and return a new caller-owned centroid.

    Input embeddings remain caller-owned and are never modified. The caller must
    overwrite them after this function returns or raises.
    """

    if len(reference_embeddings) != 3:
        raise ValueError("exactly three reference embeddings are required")
    normalized: list[np.ndarray] = []
    leave_one_out: list[np.ndarray] = []
    centroid: np.ndarray | None = None
    try:
        for embedding in reference_embeddings:
            normalized.append(_normalized_embedding_copy(embedding))
        if len({embedding.shape for embedding in normalized}) != 1:
            raise ValueError("enrollment embedding dimensions differ")
        try:
            leave_one_out.append(
                _normalized_embedding_sum((normalized[1], normalized[2]))
            )
            leave_one_out.append(
                _normalized_embedding_sum((normalized[0], normalized[2]))
            )
            leave_one_out.append(
                _normalized_embedding_sum((normalized[0], normalized[1]))
            )
        except ValueError as exc:
            raise EnrollmentAudioError("voice_samples_inconsistent") from exc
        similarities = tuple(
            float(np.dot(embedding, comparison))
            for embedding, comparison in zip(
                normalized,
                leave_one_out,
                strict=True,
            )
        )
        if (
            not all(math.isfinite(value) for value in similarities)
            or min(similarities) < ENROLLMENT_SIMILARITY_THRESHOLD
        ):
            raise EnrollmentAudioError("voice_samples_inconsistent")
        try:
            centroid = _normalized_embedding_sum(normalized)
        except ValueError as exc:
            raise EnrollmentAudioError("voice_samples_inconsistent") from exc
        result = centroid
        centroid = None
        return result
    finally:
        for embedding in normalized:
            wipe_enrollment_embedding(embedding)
        for comparison in leave_one_out:
            wipe_enrollment_embedding(comparison)
        wipe_enrollment_embedding(centroid)


def verify_enrollment_holdout(
    reference_centroid: np.ndarray,
    holdout_1_5: np.ndarray,
    holdout_3_0: np.ndarray,
    holdout_5_0: np.ndarray,
) -> EnrollmentVerificationResult:
    """Compare one independent segment at all three enrollment checkpoints.

    All four arrays remain caller-owned and are never modified. The returned
    percentage is the clamped minimum score; raw checkpoint scores never leave
    this function.
    """

    normalized: list[np.ndarray] = []
    try:
        for embedding in (
            reference_centroid,
            holdout_1_5,
            holdout_3_0,
            holdout_5_0,
        ):
            normalized.append(_normalized_embedding_copy(embedding))
        if len({embedding.shape for embedding in normalized}) != 1:
            raise ValueError("enrollment embedding dimensions differ")
        scores = tuple(
            float(np.dot(normalized[0], holdout)) for holdout in normalized[1:]
        )
        passed = all(
            math.isfinite(score)
            and score >= ENROLLMENT_SIMILARITY_THRESHOLD
            for score in scores
        )
        minimum_score = min(scores) if all(map(math.isfinite, scores)) else 0.0
        return EnrollmentVerificationResult(
            passed=passed,
            match_percent=round(max(0.0, min(1.0, minimum_score)) * 100),
        )
    finally:
        for embedding in normalized:
            wipe_enrollment_embedding(embedding)
