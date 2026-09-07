"""Enrollment adapter backed by the provider-neutral runtime PCM pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from main_logic.voice_turn.audio_input import (
    ProcessedVoiceFrame,
    VoiceInputAudioPipeline,
)

from .audio_contract import (
    OWNER_CAMPPLUS_DESKTOP_RUNTIME_CHUNK_SAMPLES,
    OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ,
    OWNER_CAMPPLUS_TARGET_SAMPLE_RATE_HZ,
)


OWNER_CAMPPLUS_RUNTIME_CHUNK_BYTES = (
    OWNER_CAMPPLUS_DESKTOP_RUNTIME_CHUNK_SAMPLES * 2
)


class EnrollmentAudioNormalizationError(RuntimeError):
    """Stable normalization failure that never represents speaker evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _EnrollmentPipeline(Protocol):
    async def process(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
    ) -> ProcessedVoiceFrame: ...

    async def finalize_stream(self) -> bytes: ...

    async def close(self) -> None: ...


EnrollmentPipelineFactory = Callable[[], _EnrollmentPipeline]


async def _close_pipeline_cancellation_safe(
    pipeline: _EnrollmentPipeline,
) -> asyncio.CancelledError | None:
    close_task = asyncio.create_task(
        pipeline.close(),
        name="voice-identity-enrollment-audio-close",
    )
    cancellation: asyncio.CancelledError | None = None
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    close_task.result()
    return cancellation


class EnrollmentAudioNormalizer:
    """Normalize one raw desktop segment through a fresh runtime pipeline."""

    def __init__(
        self,
        *,
        nr_enabled: bool,
        pipeline_factory: EnrollmentPipelineFactory | None = None,
    ) -> None:
        self._nr_enabled = bool(nr_enabled)
        self._pipeline_factory = pipeline_factory or (
            lambda: VoiceInputAudioPipeline(nr_enabled=self._nr_enabled)
        )

    @property
    def nr_enabled(self) -> bool:
        return self._nr_enabled

    async def normalize(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        target_samples: int,
    ) -> bytes:
        """Return exactly ``target_samples`` of normalized 16 kHz PCM16."""

        if (
            type(pcm16) is not bytes
            or len(pcm16) % OWNER_CAMPPLUS_RUNTIME_CHUNK_BYTES
        ):
            raise EnrollmentAudioNormalizationError("invalid_pcm")
        if sample_rate_hz != OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ:
            raise EnrollmentAudioNormalizationError("invalid_pcm")
        if type(target_samples) is not int or target_samples <= 0:
            raise ValueError("target_samples must be a positive integer")
        required_source_samples, remainder = divmod(
            target_samples * OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ,
            OWNER_CAMPPLUS_TARGET_SAMPLE_RATE_HZ,
        )
        if remainder:
            raise ValueError("target_samples cannot map exactly to source PCM")
        if len(pcm16) < required_source_samples * 2:
            raise EnrollmentAudioNormalizationError("speech_too_short")

        try:
            pipeline = self._pipeline_factory()
        except Exception as exc:
            raise EnrollmentAudioNormalizationError(
                "audio_processing_unavailable"
            ) from exc

        normalized = bytearray()
        primary_failure: BaseException | None = None
        try:
            for offset in range(0, len(pcm16), OWNER_CAMPPLUS_RUNTIME_CHUNK_BYTES):
                chunk = pcm16[
                    offset : offset + OWNER_CAMPPLUS_RUNTIME_CHUNK_BYTES
                ]
                frame = await pipeline.process(
                    chunk,
                    sample_rate_hz=OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ,
                )
                if (
                    frame.sample_rate_hz != OWNER_CAMPPLUS_TARGET_SAMPLE_RATE_HZ
                    or type(frame.pcm16) is not bytes
                    or len(frame.pcm16) % 2
                    or (self._nr_enabled and not frame.rnnoise_available)
                ):
                    raise EnrollmentAudioNormalizationError(
                        "audio_processing_unavailable"
                    )
                normalized.extend(frame.pcm16)

            tail = await pipeline.finalize_stream()
            if type(tail) is not bytes or len(tail) % 2:
                raise EnrollmentAudioNormalizationError(
                    "audio_processing_unavailable"
                )
            normalized.extend(tail)

            required_bytes = target_samples * 2
            if len(normalized) < required_bytes:
                raise EnrollmentAudioNormalizationError(
                    "audio_processing_unavailable"
                )
            return bytes(normalized[:required_bytes])
        except BaseException as exc:
            primary_failure = exc
            if isinstance(
                exc,
                (asyncio.CancelledError, EnrollmentAudioNormalizationError),
            ):
                raise
            raise EnrollmentAudioNormalizationError(
                "audio_processing_unavailable"
            ) from exc
        finally:
            try:
                close_cancellation = await _close_pipeline_cancellation_safe(
                    pipeline
                )
            except Exception as exc:
                if primary_failure is None:
                    raise EnrollmentAudioNormalizationError(
                        "audio_processing_unavailable"
                    ) from exc
            else:
                if close_cancellation is not None and primary_failure is None:
                    raise close_cancellation
            finally:
                normalized[:] = b"\x00" * len(normalized)
