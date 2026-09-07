from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

import main_logic.voice_identity_service.enrollment as enrollment_module
from main_logic.voice_identity_service.enrollment import (
    ENROLLMENT_MAXIMUM_PCM_BYTES,
    ENROLLMENT_VERIFICATION_MAXIMUM_PCM_BYTES,
    EnrollmentAudioError,
    EnrollmentSpeechValidatorUnavailableError,
    SileroEnrollmentSpeechValidator,
    create_enrollment_reference_centroid,
    validate_enrollment_pcm16,
    verify_enrollment_holdout,
    wipe_enrollment_embedding,
)


def _pcm(milliseconds: int, *, amplitude: int = 2_000) -> bytes:
    sample_count = 16_000 * milliseconds // 1_000
    samples = np.full(sample_count, amplitude, dtype="<i2")
    return samples.tobytes()


def test_accepts_four_seconds_of_usable_pcm() -> None:
    validate_enrollment_pcm16(_pcm(4_000))
    validate_enrollment_pcm16(
        _pcm(5_000),
        maximum_pcm_bytes=ENROLLMENT_VERIFICATION_MAXIMUM_PCM_BYTES,
    )


@pytest.mark.parametrize(
    ("pcm16", "code"),
    [
        pytest.param(b"\x00", "invalid_pcm", id="odd-byte-count"),
        pytest.param(_pcm(1_499), "speech_too_short", id="too-short"),
        pytest.param(
            b"\x00\x00" * 32_000,
            "volume_too_low",
            id="silence",
        ),
        pytest.param(_pcm(4_001), "audio_too_long", id="too-long"),
        pytest.param(
            _pcm(4_000, amplitude=32_767),
            "severe_clipping",
            id="clipped",
        ),
    ],
)
def test_rejects_unusable_pcm(pcm16: bytes, code: str) -> None:
    with pytest.raises(EnrollmentAudioError) as caught:
        validate_enrollment_pcm16(pcm16)

    assert caught.value.code == code


def test_payload_ceiling_matches_four_second_pcm16() -> None:
    assert ENROLLMENT_MAXIMUM_PCM_BYTES == 128_000
    assert ENROLLMENT_VERIFICATION_MAXIMUM_PCM_BYTES == 160_000


class _FakeSileroVad:
    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = probabilities
        self.is_ready = False
        self.load_count = 0
        self.reset_count = 0
        self.process_count = 0
        self.close_count = 0
        self.process_error: Exception | None = None

    def load(self) -> bool:
        self.load_count += 1
        self.is_ready = True
        return True

    def reset_stream(self) -> None:
        self.reset_count += 1

    def process_pcm16(self, _pcm16_le: bytes) -> list[float]:
        self.process_count += 1
        if self.process_error is not None:
            raise self.process_error
        return list(self.probabilities)

    def close(self) -> None:
        self.close_count += 1
        self.is_ready = False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_silero_validator_resets_each_segment_and_accepts_47_windows() -> None:
    vad = _FakeSileroVad([0.5] * 47 + [0.49] * 46)
    validator = SileroEnrollmentSpeechValidator(vad=vad)

    assert await validator.load()
    first = await validator.validate_pcm16(_pcm(3_000))
    second = await validator.validate_pcm16(_pcm(3_000))
    await validator.close()
    await validator.close()

    assert first.window_count == 93
    assert first.active_window_count == 47
    assert second == first
    assert vad.load_count == 1
    assert vad.reset_count == 2
    assert vad.process_count == 2
    assert vad.close_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_silero_validator_rejects_insufficient_real_speech() -> None:
    vad = _FakeSileroVad([0.9] * 46 + [0.1] * 47)
    validator = SileroEnrollmentSpeechValidator(vad=vad)
    assert await validator.load()

    with pytest.raises(EnrollmentAudioError) as caught:
        await validator.validate_pcm16(_pcm(3_000))

    assert caught.value.code == "no_speech_detected"
    await validator.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_silero_validator_fails_closed_when_inference_is_unavailable() -> None:
    vad = _FakeSileroVad([0.9] * 93)
    validator = SileroEnrollmentSpeechValidator(vad=vad)
    assert await validator.load()
    vad.process_error = RuntimeError("private silero failure")

    with pytest.raises(EnrollmentSpeechValidatorUnavailableError):
        await validator.validate_pcm16(_pcm(3_000))

    await validator.close()
    assert not await validator.load()


class _BlockingSileroVad(_FakeSileroVad):
    def __init__(self) -> None:
        super().__init__([0.9] * 93)
        self.started = threading.Event()
        self.release = threading.Event()

    def process_pcm16(self, pcm16_le: bytes) -> list[float]:
        self.started.set()
        assert self.release.wait(timeout=5.0)
        return super().process_pcm16(pcm16_le)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_silero_close_waits_for_in_flight_validation() -> None:
    vad = _BlockingSileroVad()
    validator = SileroEnrollmentSpeechValidator(vad=vad)
    assert await validator.load()
    validation = asyncio.create_task(validator.validate_pcm16(_pcm(3_000)))
    assert await asyncio.to_thread(vad.started.wait, 5.0)

    closing = asyncio.create_task(validator.close())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(closing), timeout=0.05)

    vad.release.set()
    assert (await validation).active_window_count == 93
    await closing
    assert vad.close_count == 1


def _unit(values: tuple[float, ...]) -> np.ndarray:
    embedding = np.asarray(values, dtype=np.float32)
    embedding /= np.linalg.norm(embedding)
    return embedding


@pytest.mark.unit
def test_three_consistent_references_create_normalized_centroid() -> None:
    references = [
        _unit((1.0, 0.0)),
        _unit((0.9, 0.1)),
        _unit((0.8, -0.1)),
    ]
    originals = [embedding.copy() for embedding in references]

    centroid = create_enrollment_reference_centroid(references)
    try:
        assert np.linalg.norm(centroid) == pytest.approx(1.0)
        assert centroid[0] > 0.99
        for embedding, original in zip(references, originals, strict=True):
            np.testing.assert_array_equal(embedding, original)
    finally:
        wipe_enrollment_embedding(centroid)
        for embedding in references:
            wipe_enrollment_embedding(embedding)
        for original in originals:
            wipe_enrollment_embedding(original)


@pytest.mark.unit
def test_centroid_helper_wipes_every_internal_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references = [
        _unit((1.0, 0.0)),
        _unit((0.9, 0.1)),
        _unit((0.8, -0.1)),
    ]
    wiped: list[np.ndarray] = []
    original_wipe = enrollment_module.wipe_enrollment_embedding

    def tracking_wipe(embedding: np.ndarray | None) -> None:
        if embedding is not None:
            wiped.append(embedding)
        original_wipe(embedding)

    monkeypatch.setattr(
        enrollment_module,
        "wipe_enrollment_embedding",
        tracking_wipe,
    )
    centroid = create_enrollment_reference_centroid(references)
    try:
        assert len(wiped) == 6
        for temporary in wiped:
            assert not np.count_nonzero(temporary)
        assert np.count_nonzero(centroid)
    finally:
        original_wipe(centroid)
        for embedding in references:
            original_wipe(embedding)


@pytest.mark.unit
def test_inconsistent_reference_resets_are_rejected() -> None:
    references = [
        _unit((1.0, 0.0)),
        _unit((1.0, 0.0)),
        _unit((0.0, 1.0)),
    ]
    try:
        with pytest.raises(EnrollmentAudioError) as caught:
            create_enrollment_reference_centroid(references)
        assert caught.value.code == "voice_samples_inconsistent"
    finally:
        for embedding in references:
            wipe_enrollment_embedding(embedding)


@pytest.mark.unit
def test_opposing_reference_sum_maps_to_inconsistent_and_wipes_temporaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references = [
        _unit((1.0, 0.0)),
        _unit((-1.0, 0.0)),
        _unit((0.0, 1.0)),
    ]
    wiped: list[np.ndarray] = []
    original_wipe = enrollment_module.wipe_enrollment_embedding

    def tracking_wipe(embedding: np.ndarray | None) -> None:
        if embedding is not None:
            wiped.append(embedding)
        original_wipe(embedding)

    monkeypatch.setattr(
        enrollment_module,
        "wipe_enrollment_embedding",
        tracking_wipe,
    )
    try:
        with pytest.raises(EnrollmentAudioError) as caught:
            create_enrollment_reference_centroid(references)
        assert caught.value.code == "voice_samples_inconsistent"
        assert len(wiped) == 6
        for temporary in wiped:
            assert not np.count_nonzero(temporary)
    finally:
        for embedding in references:
            original_wipe(embedding)


@pytest.mark.unit
def test_holdout_requires_both_runtime_checkpoints() -> None:
    centroid = _unit((1.0, 0.0))
    holdout_1_5 = _unit((0.9, 0.1))
    holdout_3_0 = _unit((0.8, -0.1))
    holdout_5_0 = _unit((0.6, 0.8))
    originals = [
        value.copy()
        for value in (centroid, holdout_1_5, holdout_3_0, holdout_5_0)
    ]
    try:
        result = verify_enrollment_holdout(
            centroid,
            holdout_1_5,
            holdout_3_0,
            holdout_5_0,
        )
        assert result.passed
        assert result.match_percent == 60
        for embedding, original in zip(
            (centroid, holdout_1_5, holdout_3_0, holdout_5_0),
            originals,
            strict=True,
        ):
            np.testing.assert_array_equal(embedding, original)
    finally:
        for embedding in (
            centroid,
            holdout_1_5,
            holdout_3_0,
            holdout_5_0,
            *originals,
        ):
            wipe_enrollment_embedding(embedding)


@pytest.mark.unit
@pytest.mark.parametrize("failed_checkpoint", ("1.5", "3.0", "5.0"))
def test_holdout_rejects_either_low_checkpoint(failed_checkpoint: str) -> None:
    centroid = _unit((1.0, 0.0))
    high = _unit((0.9, 0.1))
    low = _unit((0.0, 1.0))
    holdout_1_5 = low if failed_checkpoint == "1.5" else high
    holdout_3_0 = low if failed_checkpoint == "3.0" else high
    holdout_5_0 = low if failed_checkpoint == "5.0" else high
    try:
        result = verify_enrollment_holdout(
            centroid,
            holdout_1_5,
            holdout_3_0,
            holdout_5_0,
        )
        assert not result.passed
        assert result.match_percent == 0
    finally:
        for embedding in (centroid, high, low):
            wipe_enrollment_embedding(embedding)


@pytest.mark.unit
def test_holdout_match_percent_clamps_negative_and_accepts_threshold() -> None:
    centroid = _unit((1.0, 0.0))
    threshold = _unit((0.4, 0.9165151))
    negative = _unit((-1.0, 0.0))
    try:
        accepted = verify_enrollment_holdout(
            centroid,
            threshold,
            threshold,
            threshold,
        )
        rejected = verify_enrollment_holdout(
            centroid,
            threshold,
            threshold,
            negative,
        )
        assert accepted.passed
        assert accepted.match_percent == 40
        assert not rejected.passed
        assert rejected.match_percent == 0
    finally:
        for embedding in (centroid, threshold, negative):
            wipe_enrollment_embedding(embedding)


@pytest.mark.unit
def test_wipe_embedding_overwrites_in_place() -> None:
    embedding = np.asarray([0.1, 0.2, 0.3], dtype=np.float32)
    wipe_enrollment_embedding(embedding)
    np.testing.assert_array_equal(embedding, np.zeros(3, dtype=np.float32))
