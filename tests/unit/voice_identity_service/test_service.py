from __future__ import annotations

import asyncio
from pathlib import Path
import threading

import numpy as np
import pytest

import main_logic.voice_identity_service.profile_store as store_module
from main_logic.asr_client import VoiceIdentityActivationResult
from main_logic.asr_client.speaker_shadow.campplus import CAMPPLUS_EMBEDDING_DIM
from main_logic.voice_identity.contracts import SpeakerModelIdentity
from main_logic.voice_identity.profile import SpeakerProfile
from main_logic.voice_identity.reference import SpeakerReference
from main_logic.voice_identity_service.preference_store import (
    VoiceIdentityPreferenceStore,
    VoiceIdentityPreferenceStoreError,
)
from main_logic.voice_identity_service.enrollment import EnrollmentSpeechResult
from main_logic.voice_identity_service.audio_contract import (
    OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID,
    desktop_audio_contract_snapshot,
)
from main_logic.voice_identity_service.enrollment_audio import (
    EnrollmentAudioNormalizationError,
)
from main_logic.voice_identity_service.profile_store import (
    SecureStorageUnavailableError,
    VoiceIdentityProfileCorruptError,
    VoiceIdentityProfileIncompatibleError,
    VoiceIdentityProfileStore,
    VoiceIdentityProfileStoreError,
)
from main_logic.voice_identity_service.service import (
    VoiceIdentityService,
    VoiceIdentityServiceError,
)
from main_logic.voice_input.suppression import VoiceInputSuppressionController

from .test_profile_store import _TestKeyProtector


class _Model:
    model_id = "3d-speaker-campplus-zh-en"
    model_revision = "2025-06-16-sherpa-onnx-campplus"

    def __init__(
        self,
        *,
        loads: bool = True,
        embeddings: list[np.ndarray] | None = None,
    ) -> None:
        self.loads = loads
        self.closed = False
        self.embeddings = list(embeddings or [])
        self.inference_count = 0

    def load(self) -> bool:
        return self.loads

    def cancel_load(self) -> None:
        return

    def embedding_from_pcm16(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
    ) -> np.ndarray:
        assert pcm16
        assert sample_rate_hz == 16_000
        self.inference_count += 1
        if self.embeddings:
            return self.embeddings.pop(0)
        result = np.zeros(CAMPPLUS_EMBEDDING_DIM, dtype=np.float32)
        result[0] = 1.0
        return result

    def cancel_inference(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class _SpeechValidator:
    def __init__(self, *, loads: bool = True) -> None:
        self.loads = loads
        self.closed = False

    async def load(self) -> bool:
        return self.loads

    async def validate_pcm16(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int = 16_000,
    ) -> EnrollmentSpeechResult:
        assert pcm16
        assert sample_rate_hz == 16_000
        return EnrollmentSpeechResult(window_count=96, active_window_count=96)

    async def close(self) -> None:
        self.closed = True


class _AudioNormalizer:
    def __init__(self, nr_enabled: bool, *, failure_code: str | None = None) -> None:
        self.nr_enabled = nr_enabled
        self.failure_code = failure_code
        self.calls: list[tuple[int, int, int]] = []

    async def normalize(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        target_samples: int,
    ) -> bytes:
        self.calls.append((len(pcm16), sample_rate_hz, target_samples))
        if self.failure_code is not None:
            raise EnrollmentAudioNormalizationError(self.failure_code)
        assert sample_rate_hz == 48_000
        assert target_samples in (48_000, 80_000)
        required_bytes = target_samples * 2
        if len(pcm16) < required_bytes:
            raise EnrollmentAudioNormalizationError("speech_too_short")
        return pcm16[:required_bytes]


def _pcm() -> bytes:
    samples = np.full(48_000, 4_000, dtype="<i2")
    return samples.tobytes()


def _verification_pcm(milliseconds: int = 5_000) -> bytes:
    samples = np.full(48_000 * milliseconds // 1_000, 4_000, dtype="<i2")
    return samples.tobytes()


async def _wait_until(predicate, *, timeout_seconds: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not satisfied before timeout")
        await asyncio.sleep(0.005)


def _service(
    tmp_path: Path,
    *,
    model: _Model | None = None,
    activation_results: list[bool | VoiceIdentityActivationResult] | None = None,
    runtime_status_results: list[VoiceIdentityActivationResult] | None = None,
    enrollment_ttl_seconds: float = 30.0,
    model_timeout_seconds: float = 1.0,
    runtime_mode: str = "enforce",
    speech_validator: _SpeechValidator | None = None,
    audio_normalizer_factory=None,
    enrollment_noise_reduction_enabled: bool = True,
) -> tuple[
    VoiceIdentityService,
    _Model,
    list[tuple[SpeakerProfile | None, str]],
    list[str],
]:
    selected_model = model or _Model()
    selected_validator = speech_validator or _SpeechValidator()
    activations: list[tuple[SpeakerProfile | None, str]] = []
    results = activation_results or []
    runtime_results = runtime_status_results or []
    suppression_events: list[str] = []

    async def activate(
        profile: SpeakerProfile | None,
        generation: str,
    ) -> bool:
        activations.append((profile, generation))
        return results.pop(0) if results else True

    async def suppress(reason: str) -> None:
        suppression_events.append(f"suppress:{reason}")

    async def restore(reason: str) -> None:
        suppression_events.append(f"restore:{reason}")

    def runtime_status() -> VoiceIdentityActivationResult:
        return (
            runtime_results[-1]
            if runtime_results
            else VoiceIdentityActivationResult.READY
        )

    service = VoiceIdentityService(
        VoiceIdentityProfileStore(
            tmp_path / "voice_identity.profile",
            key_protector=_TestKeyProtector(),
        ),
        VoiceIdentityPreferenceStore(tmp_path / "voice_identity.preference"),
        VoiceInputSuppressionController(
            suppress,
            restore,
            default_ttl_seconds=enrollment_ttl_seconds,
            hard_ttl_seconds=max(1.0, enrollment_ttl_seconds),
        ),
        lambda: selected_model,
        activate,
        runtime_mode=runtime_mode,  # type: ignore[arg-type]
        enrollment_ttl_seconds=enrollment_ttl_seconds,
        model_timeout_seconds=model_timeout_seconds,
        activation_timeout_seconds=1.0,
        runtime_status_callback=(runtime_status if runtime_status_results else None),
        speech_validator_factory=lambda: selected_validator,
        enrollment_audio_normalizer_factory=(
            audio_normalizer_factory or _AudioNormalizer
        ),
        enrollment_noise_reduction_enabled=enrollment_noise_reduction_enabled,
    )

    production_submit_enrollment_segment = service.submit_enrollment_segment

    async def submit_enrollment_segment(
        enrollment_id: str,
        profile_id: str,
        segment_index: int,
        pcm16: bytes,
        *,
        sample_rate_hz: int = 48_000,
        audio_contract_id: str = OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID,
    ):
        return await production_submit_enrollment_segment(
            enrollment_id,
            profile_id,
            segment_index,
            pcm16,
            sample_rate_hz=sample_rate_hz,
            audio_contract_id=audio_contract_id,
        )

    service.submit_enrollment_segment = submit_enrollment_segment  # type: ignore[method-assign]

    async def complete_enrollment(
        enrollment_id: str,
        profile_id: str,
        pcm16: bytes,
    ):
        status = service.status()
        for segment_index in range(1, 5):
            status = await service.submit_enrollment_segment(
                enrollment_id,
                profile_id,
                segment_index,
                _verification_pcm() if segment_index == 4 else pcm16,
            )
        return status

    # Keep the legacy tests focused on transaction semantics while the production
    # service exposes only the four-segment API.
    service.complete_enrollment = complete_enrollment  # type: ignore[attr-defined]
    return service, selected_model, activations, suppression_events


def _embedding(axis: int = 0) -> np.ndarray:
    result = np.zeros(CAMPPLUS_EMBEDDING_DIM, dtype=np.float32)
    result[axis] = 1.0
    return result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_segment_progress_is_server_owned_idempotent_and_profile_bound(
    tmp_path: Path,
) -> None:
    service, model, _activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    assert enrollment.profile_id is None
    assert enrollment.next_segment_index == 1
    assert enrollment.required_segments == 4

    with pytest.raises(VoiceIdentityServiceError, match="segment_out_of_order"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            2,
            _pcm(),
        )

    first = await service.submit_enrollment_segment(
        enrollment.enrollment_id,
        "profile-a",
        1,
        _pcm(),
    )
    assert first.enrollment is not None
    assert first.enrollment.profile_id == "profile-a"
    assert first.enrollment.accepted_segments == 1
    assert first.enrollment.next_segment_index == 2
    assert model.inference_count == 1

    retry = await service.submit_enrollment_segment(
        enrollment.enrollment_id,
        "profile-a",
        1,
        _pcm(),
    )
    assert retry.enrollment == first.enrollment
    assert model.inference_count == 1
    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-b",
            1,
            _pcm(),
        )
    await service.cancel_enrollment(enrollment.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_segment_requires_explicit_desktop_contract_before_normalization(
    tmp_path: Path,
) -> None:
    normalizers: list[_AudioNormalizer] = []

    def factory(enabled: bool) -> _AudioNormalizer:
        normalizer = _AudioNormalizer(enabled)
        normalizers.append(normalizer)
        return normalizer

    service, _model, _activations, _events = _service(
        tmp_path,
        audio_normalizer_factory=factory,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    with pytest.raises(VoiceIdentityServiceError, match="unsupported_audio_contract"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
            sample_rate_hz=44_100,
            audio_contract_id=OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID,
        )
    with pytest.raises(VoiceIdentityServiceError, match="unsupported_audio_contract"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
            sample_rate_hz=48_000,
            audio_contract_id="owner-campplus-desktop-v0",
        )
    with pytest.raises(VoiceIdentityServiceError, match="audio_too_long"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            bytes(48_000 * 4 * 2 + 2),
        )

    assert normalizers == []
    assert service.status().enrollment is not None
    assert service.status().enrollment.next_segment_index == 1
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_segment_duration_limits_follow_the_48khz_desktop_contract(
    tmp_path: Path,
) -> None:
    target_samples_seen: list[int] = []

    class StrictDurationNormalizer:
        def __init__(self, _enabled: bool) -> None:
            pass

        async def normalize(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
            target_samples: int,
        ) -> bytes:
            assert sample_rate_hz == 48_000
            target_samples_seen.append(target_samples)
            required_source_bytes = target_samples * 3 * 2
            if len(pcm16) < required_source_bytes:
                raise EnrollmentAudioNormalizationError("speech_too_short")
            return bytes(target_samples * 2)

    service, model, _activations, _events = _service(
        tmp_path,
        audio_normalizer_factory=StrictDurationNormalizer,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    with pytest.raises(VoiceIdentityServiceError, match="audio_too_long"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _verification_pcm(4_001),
        )
    for segment_index in (1, 2, 3):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            segment_index,
            _verification_pcm(3_000),
        )

    with pytest.raises(VoiceIdentityServiceError, match="speech_too_short"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            4,
            _verification_pcm(4_999),
        )
    with pytest.raises(VoiceIdentityServiceError, match="audio_too_long"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            4,
            _verification_pcm(5_001),
        )

    completed = await service.submit_enrollment_segment(
        enrollment.enrollment_id,
        "profile-a",
        4,
        _verification_pcm(),
    )
    assert completed.verification is not None
    assert completed.verification.passed
    assert target_samples_seen == [48_000, 48_000, 48_000, 80_000, 80_000]
    assert model.inference_count == 6
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_each_segment_gets_fresh_normalizer_with_frozen_nr_snapshot(
    tmp_path: Path,
) -> None:
    normalizers: list[_AudioNormalizer] = []

    def factory(enabled: bool) -> _AudioNormalizer:
        normalizer = _AudioNormalizer(enabled)
        normalizers.append(normalizer)
        return normalizer

    service, _model, _activations, _events = _service(
        tmp_path,
        audio_normalizer_factory=factory,
        enrollment_noise_reduction_enabled=False,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    for segment_index in (1, 2):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            segment_index,
            _pcm(),
        )

    assert len(normalizers) == 2
    assert normalizers[0] is not normalizers[1]
    assert [normalizer.nr_enabled for normalizer in normalizers] == [False, False]
    assert normalizers[0].calls == [(len(_pcm()), 48_000, 48_000)]
    assert normalizers[1].calls == [(len(_pcm()), 48_000, 48_000)]
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_normalization_unavailable_never_replaces_existing_profile(
    tmp_path: Path,
) -> None:
    failure_code: list[str | None] = [None]

    def factory(enabled: bool) -> _AudioNormalizer:
        return _AudioNormalizer(enabled, failure_code=failure_code[0])

    service, model, _activations, _events = _service(
        tmp_path,
        audio_normalizer_factory=factory,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    completed = await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )
    old_generation = completed.profile_generation
    old_inference_count = model.inference_count

    failure_code[0] = "audio_processing_unavailable"
    reenrollment = await service.start_enrollment()
    with pytest.raises(
        VoiceIdentityServiceError,
        match="audio_processing_unavailable",
    ):
        await service.submit_enrollment_segment(
            reenrollment.enrollment_id,
            "profile-b",
            1,
            _pcm(),
        )

    current = service.status()
    assert current.state.has_profile
    assert current.state.effective_reason == "ready"
    assert current.profile_generation == old_generation
    assert current.enrollment is None
    assert model.inference_count == old_inference_count
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_normalization_unavailable_without_profile_degrades_runtime(
    tmp_path: Path,
) -> None:
    def factory(enabled: bool) -> _AudioNormalizer:
        return _AudioNormalizer(
            enabled,
            failure_code="audio_processing_unavailable",
        )

    service, model, _activations, _events = _service(
        tmp_path,
        audio_normalizer_factory=factory,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    with pytest.raises(
        VoiceIdentityServiceError,
        match="audio_processing_unavailable",
    ):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )

    current = service.status()
    assert not current.state.has_profile
    assert not current.state.effective_enabled
    assert current.state.effective_reason == "runtime_degraded"
    assert current.enrollment is None
    assert model.inference_count == 0
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_normalization_result_rechecks_operation_fence_before_silero(
    tmp_path: Path,
) -> None:
    normalization_started = asyncio.Event()
    release_normalization = asyncio.Event()

    class BlockingNormalizer(_AudioNormalizer):
        async def normalize(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
            target_samples: int,
        ) -> bytes:
            normalization_started.set()
            await release_normalization.wait()
            return await super().normalize(
                pcm16,
                sample_rate_hz=sample_rate_hz,
                target_samples=target_samples,
            )

    validator = _SpeechValidator()
    validation_calls = 0
    original_validate = validator.validate_pcm16

    async def count_validation(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return await original_validate(*args, **kwargs)

    validator.validate_pcm16 = count_validation  # type: ignore[method-assign]
    service, model, _activations, _events = _service(
        tmp_path,
        speech_validator=validator,
        audio_normalizer_factory=BlockingNormalizer,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    submission = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _verification_pcm(3_000),
        )
    )
    await normalization_started.wait()
    session = service._enrollment  # type: ignore[attr-defined]
    assert session is not None
    session.operation_nonce += 1
    release_normalization.set()

    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await submission
    assert validation_calls == 0
    assert model.inference_count == 0
    await service.cancel_enrollment(enrollment.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reference_inconsistency_wipes_all_inputs_and_resets_round(
    tmp_path: Path,
) -> None:
    embeddings = [_embedding(), _embedding(), _embedding(1)]
    model = _Model(embeddings=embeddings)
    service, _selected, _activations, _events = _service(tmp_path, model=model)
    await service.initialize()
    enrollment = await service.start_enrollment()
    for segment_index in (1, 2):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            segment_index,
            _pcm(),
        )
    with pytest.raises(VoiceIdentityServiceError, match="voice_samples_inconsistent"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            3,
            _pcm(),
        )

    current = service.status().enrollment
    assert current is not None
    assert current.profile_id == "profile-a"
    assert current.next_segment_index == 1
    assert current.accepted_segments == 0
    assert all(np.count_nonzero(item) == 0 for item in embeddings)
    await service.cancel_enrollment(enrollment.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holdout_first_failure_retries_fourth_second_resets_all(
    tmp_path: Path,
) -> None:
    embeddings = [
        _embedding(),
        _embedding(),
        _embedding(),
        _embedding(1),
        _embedding(),
        _embedding(),
        _embedding(1),
        _embedding(),
        _embedding(),
    ]
    model = _Model(embeddings=embeddings)
    service, _selected, _activations, _events = _service(tmp_path, model=model)
    await service.initialize()
    enrollment = await service.start_enrollment()
    for segment_index in (1, 2, 3):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            segment_index,
            _pcm(),
        )
    session = service._enrollment  # type: ignore[attr-defined]
    assert session is not None
    centroid = session.reference_centroid
    assert centroid is not None

    for expected_next in (4, 1):
        result = await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            4,
            _verification_pcm(),
        )
        assert result.verification is not None
        assert not result.verification.passed
        assert result.verification.match_percent == 0
        current = service.status().enrollment
        assert current is not None
        assert current.next_segment_index == expected_next
        assert service.status().verification is None
    assert np.count_nonzero(centroid) == 0
    assert all(np.count_nonzero(item) == 0 for item in embeddings)
    await service.cancel_enrollment(enrollment.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_segment_in_flight_is_bounded_and_not_duplicated(
    tmp_path: Path,
) -> None:
    class BlockingValidator(_SpeechValidator):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def validate_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int = 16_000,
        ) -> EnrollmentSpeechResult:
            self.started.set()
            await self.release.wait()
            return await super().validate_pcm16(
                pcm16,
                sample_rate_hz=sample_rate_hz,
            )

    validator = BlockingValidator()
    service, model, _activations, _events = _service(
        tmp_path,
        speech_validator=validator,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    first = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )
    )
    await asyncio.wait_for(validator.started.wait(), 1.0)
    with pytest.raises(VoiceIdentityServiceError, match="segment_in_progress"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )
    validator.release.set()
    result = await first
    assert result.enrollment is not None
    assert result.enrollment.next_segment_index == 2
    assert model.inference_count == 1
    await service.cancel_enrollment(enrollment.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_during_speech_validation_invalidates_old_operation(
    tmp_path: Path,
) -> None:
    class BlockingValidator(_SpeechValidator):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def validate_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int = 16_000,
        ) -> EnrollmentSpeechResult:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    validator = BlockingValidator()
    service, model, _activations, events = _service(
        tmp_path,
        speech_validator=validator,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    submission = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )
    )
    await asyncio.wait_for(validator.started.wait(), 1.0)
    assert await service.cancel_enrollment(enrollment.enrollment_id)
    await asyncio.wait_for(validator.cancelled.wait(), 1.0)
    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await submission
    assert service.status().enrollment is None
    assert model.inference_count == 0
    assert model.closed and validator.closed
    assert events[-1] == "restore:voice_identity_enrollment"
    replacement = await service.start_enrollment()
    assert replacement.enrollment_id != enrollment.enrollment_id
    await service.cancel_enrollment(replacement.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_late_inference_is_wiped_and_cannot_commit_after_cancel(
    tmp_path: Path,
) -> None:
    class LateModel(_Model):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.result = _embedding()

        def embedding_from_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
        ) -> np.ndarray:
            self.started.set()
            assert self.release.wait(2.0)
            return self.result

        def cancel_inference(self) -> None:
            return

    model = LateModel()
    service, _selected, _activations, _events = _service(
        tmp_path,
        model=model,
        model_timeout_seconds=1.0,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    submission = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )
    )
    assert await asyncio.to_thread(model.started.wait, 1.0)
    service._model_timeout_seconds = 0.05  # type: ignore[attr-defined]
    assert await service.cancel_enrollment(enrollment.enrollment_id)
    with pytest.raises(VoiceIdentityServiceError, match="model_unavailable"):
        await service.start_enrollment()
    model.release.set()
    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await submission
    await _wait_until(
        lambda: service._model_inference_cleanup_task is None,  # type: ignore[attr-defined]
    )
    assert np.count_nonzero(model.result) == 0
    assert service.status().profile_generation is None
    replacement = await service.start_enrollment()
    await service.cancel_enrollment(replacement.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_embedding_is_terminal_and_wiped(tmp_path: Path) -> None:
    invalid = _embedding()
    invalid[3] = np.nan
    model = _Model(embeddings=[invalid])
    service, _selected, _activations, events = _service(tmp_path, model=model)
    await service.initialize()
    enrollment = await service.start_enrollment()
    with pytest.raises(VoiceIdentityServiceError, match="model_unavailable"):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )
    assert service.status().enrollment is None
    assert np.count_nonzero(invalid) == 0
    assert model.closed
    assert events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validator_load_timeout_retains_owned_cleanup(tmp_path: Path) -> None:
    class SlowLoadValidator(_SpeechValidator):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def load(self) -> bool:
            self.started.set()
            await self.release.wait()
            return True

    validator = SlowLoadValidator()
    service, model, _activations, events = _service(
        tmp_path,
        speech_validator=validator,
        model_timeout_seconds=0.05,
    )
    await service.initialize()
    with pytest.raises(VoiceIdentityServiceError, match="model_unavailable"):
        await service.start_enrollment()
    await asyncio.wait_for(validator.started.wait(), 1.0)
    assert events == []
    with pytest.raises(VoiceIdentityServiceError, match="model_unavailable"):
        await service.start_enrollment()
    validator.release.set()
    await _wait_until(
        lambda: service._speech_validator_load_cleanup_task is None,  # type: ignore[attr-defined]
    )
    assert validator.closed and model.closed
    retry = await service.start_enrollment()
    await service.cancel_enrollment(retry.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_while_waiting_for_cas_wipes_computed_embedding(
    tmp_path: Path,
) -> None:
    class BlockingValidator(_SpeechValidator):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def validate_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int = 16_000,
        ) -> EnrollmentSpeechResult:
            self.started.set()
            await self.release.wait()
            return await super().validate_pcm16(
                pcm16,
                sample_rate_hz=sample_rate_hz,
            )

    computed_embedding = _embedding()
    model = _Model(embeddings=[computed_embedding])
    validator = BlockingValidator()
    service, _selected, _activations, _events = _service(
        tmp_path,
        model=model,
        speech_validator=validator,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    submission = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )
    )
    await asyncio.wait_for(validator.started.wait(), 1.0)
    await service._operation_lock.acquire()  # type: ignore[attr-defined]
    validator.release.set()
    await _wait_until(lambda: model.inference_count == 1)
    submission.cancel()
    await asyncio.sleep(0)
    service._operation_lock.release()  # type: ignore[attr-defined]

    with pytest.raises(asyncio.CancelledError):
        await submission
    assert np.count_nonzero(computed_embedding) == 0
    assert service.status().enrollment is None
    assert model.closed and validator.closed
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reference_and_holdout_inference_use_exact_checkpoint_lengths(
    tmp_path: Path,
) -> None:
    class LengthRecordingModel(_Model):
        def __init__(self) -> None:
            super().__init__()
            self.pcm_lengths: list[int] = []

        def embedding_from_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
        ) -> np.ndarray:
            self.pcm_lengths.append(len(pcm16))
            return super().embedding_from_pcm16(
                pcm16,
                sample_rate_hz=sample_rate_hz,
            )

    normalizers: list[_AudioNormalizer] = []

    def factory(enabled: bool) -> _AudioNormalizer:
        normalizer = _AudioNormalizer(enabled)
        normalizers.append(normalizer)
        return normalizer

    model = LengthRecordingModel()
    service, _selected, _activations, _events = _service(
        tmp_path,
        model=model,
        audio_normalizer_factory=factory,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    result = service.status()
    for segment_index in range(1, 5):
        result = await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            segment_index,
            _verification_pcm() if segment_index == 4 else _pcm(),
        )
    assert [normalizer.calls[0][2] for normalizer in normalizers] == [
        48_000,
        48_000,
        48_000,
        80_000,
    ]
    assert model.pcm_lengths == [
        96_000,
        96_000,
        96_000,
        48_000,
        96_000,
        160_000,
    ]
    assert result.verification is not None
    assert result.verification.passed
    assert result.verification.match_percent == 100
    assert result.as_dict()["verification"] == {
        "passed": True,
        "match_percent": 100,
    }
    assert "verification" not in service.status().as_dict()
    reconciled = await service.submit_enrollment_segment(
        enrollment.enrollment_id,
        "profile-a",
        4,
        _verification_pcm(),
    )
    assert reconciled.verification is None
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expiry_during_validation_retires_operation_before_late_result(
    tmp_path: Path,
) -> None:
    class BlockingValidator(_SpeechValidator):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def validate_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int = 16_000,
        ) -> EnrollmentSpeechResult:
            self.started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    validator = BlockingValidator()
    service, model, _activations, events = _service(
        tmp_path,
        speech_validator=validator,
        enrollment_ttl_seconds=0.03,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    submission = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )
    )
    await asyncio.wait_for(validator.started.wait(), 1.0)
    await _wait_until(lambda: service.status().enrollment is None)
    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await submission
    assert model.inference_count == 0
    assert model.closed and validator.closed
    assert events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_linearizes_before_concurrent_profile_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, _activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    for segment_index in (1, 2, 3):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            segment_index,
            _pcm(),
        )

    profile_store = service._profile_store  # type: ignore[attr-defined]
    original_stage = profile_store.stage
    stage_started = threading.Event()
    stage_release = threading.Event()

    def blocking_stage(profile: SpeakerProfile, *, audio_contract):
        stage_started.set()
        assert stage_release.wait(1.0)
        return original_stage(profile, audio_contract=audio_contract)

    monkeypatch.setattr(profile_store, "stage", blocking_stage)
    completion = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            4,
            _verification_pcm(),
        )
    )
    assert await asyncio.to_thread(stage_started.wait, 1.0)
    deletion = asyncio.create_task(service.delete_profile())
    await asyncio.sleep(0)
    assert not deletion.done()
    stage_release.set()

    committed = await completion
    deleted = await deletion
    assert committed.profile_generation == "profile-a"
    assert deleted.profile_generation is None
    assert not deleted.state.has_profile
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_late_validation_cannot_start_inference_after_session_cancel(
    tmp_path: Path,
) -> None:
    class LateValidator(_SpeechValidator):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def validate_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int = 16_000,
        ) -> EnrollmentSpeechResult:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.release.wait()
                return EnrollmentSpeechResult(window_count=96, active_window_count=96)

    validator = LateValidator()
    service, model, _activations, _events = _service(
        tmp_path,
        speech_validator=validator,
        model_timeout_seconds=1.0,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    submission = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            1,
            _pcm(),
        )
    )
    await asyncio.wait_for(validator.started.wait(), 1.0)
    service._model_timeout_seconds = 0.05  # type: ignore[attr-defined]
    assert await service.cancel_enrollment(enrollment.enrollment_id)
    await asyncio.wait_for(validator.cancel_seen.wait(), 1.0)
    assert model.closed
    validator.release.set()
    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await submission
    assert model.inference_count == 0
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_after_first_holdout_never_starts_second_holdout(
    tmp_path: Path,
) -> None:
    class FourthCallBlockingModel(_Model):
        def __init__(self) -> None:
            super().__init__()
            self.holdout_started = threading.Event()
            self.holdout_release = threading.Event()

        def embedding_from_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
        ) -> np.ndarray:
            if self.inference_count == 3:
                self.holdout_started.set()
                assert self.holdout_release.wait(1.0)
            return super().embedding_from_pcm16(
                pcm16,
                sample_rate_hz=sample_rate_hz,
            )

        def cancel_inference(self) -> None:
            self.holdout_release.set()

    model = FourthCallBlockingModel()
    service, _selected, _activations, _events = _service(tmp_path, model=model)
    await service.initialize()
    enrollment = await service.start_enrollment()
    for segment_index in (1, 2, 3):
        await service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            segment_index,
            _pcm(),
        )
    fourth = asyncio.create_task(
        service.submit_enrollment_segment(
            enrollment.enrollment_id,
            "profile-a",
            4,
            _verification_pcm(),
        )
    )
    assert await asyncio.to_thread(model.holdout_started.wait, 1.0)
    assert await service.cancel_enrollment(enrollment.enrollment_id)
    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await fourth
    assert model.inference_count == 4
    assert service.status().profile_generation is None
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_enrollment_loads_before_suppression_and_enables(
    tmp_path: Path,
) -> None:
    service, model, activations, suppression_events = _service(tmp_path)
    await service.initialize()

    enrollment = await service.start_enrollment()
    assert suppression_events == ["suppress:voice_identity_enrollment"]
    status = await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )

    assert status.state.requested_enabled
    assert status.state.effective_enabled
    assert status.state.has_profile
    assert status.profile_generation == "profile-a"
    assert activations[-1][1] == "profile-a"
    assert suppression_events[-1] == "restore:voice_identity_enrollment"
    assert model.closed
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsupported_route_saves_profile_without_reporting_ready(
    tmp_path: Path,
) -> None:
    service, model, _activations, suppression_events = _service(
        tmp_path,
        activation_results=[VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE],
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    status = await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )

    assert status.state.requested_enabled
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "unsupported_asr_route"
    assert status.state.has_profile
    assert status.profile_generation == "profile-a"
    assert model.closed
    assert suppression_events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_reconciles_live_runtime_route_result(tmp_path: Path) -> None:
    runtime_results = [VoiceIdentityActivationResult.READY]
    service, _model, _activations, _events = _service(
        tmp_path,
        runtime_status_results=runtime_results,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )

    runtime_results[0] = VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    unsupported = service.status()
    assert not unsupported.state.effective_enabled
    assert unsupported.state.effective_reason == "unsupported_asr_route"

    runtime_results[0] = VoiceIdentityActivationResult.READY
    assert service.status().state.effective_enabled
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_profile_staging_aborts_completed_worker_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, _activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    profile_store = service._profile_store  # type: ignore[attr-defined]
    original_stage = profile_store.stage
    stage_started = threading.Event()
    stage_release = threading.Event()

    def blocking_stage(profile: SpeakerProfile, *, audio_contract):
        stage_started.set()
        assert stage_release.wait(1.0)
        return original_stage(profile, audio_contract=audio_contract)

    monkeypatch.setattr(profile_store, "stage", blocking_stage)
    completion = asyncio.create_task(
        service.complete_enrollment(
            enrollment.enrollment_id,
            "profile-a",
            _pcm(),
        )
    )
    assert await asyncio.to_thread(stage_started.wait, 1.0)
    completion.cancel()
    stage_release.set()

    with pytest.raises(asyncio.CancelledError):
        await completion

    assert list(tmp_path.glob(".*.tmp")) == []
    assert not (tmp_path / "voice_identity.profile").exists()
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_model_failure_never_suppresses_input(tmp_path: Path) -> None:
    service, model, _activations, suppression_events = _service(
        tmp_path,
        model=_Model(loads=False),
    )
    await service.initialize()

    with pytest.raises(VoiceIdentityServiceError, match="model_unavailable"):
        await service.start_enrollment()

    assert suppression_events == []
    assert model.closed
    assert service.status().state.effective_reason == "model_unavailable"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_releases_model_and_lease(
    tmp_path: Path,
) -> None:
    service, model, _activations, suppression_events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()

    assert await service.cancel_enrollment(enrollment.enrollment_id)
    assert not await service.cancel_enrollment(enrollment.enrollment_id)
    assert model.closed
    assert suppression_events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_timeout_releases_model_and_lease(tmp_path: Path) -> None:
    service, model, _activations, suppression_events = _service(
        tmp_path,
        enrollment_ttl_seconds=0.02,
    )
    await service.initialize()
    await service.start_enrollment()

    await asyncio.sleep(0.08)

    assert service.status().enrollment is None
    assert model.closed
    assert suppression_events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrollment_expiry_uses_suppression_lease_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, model, _activations, suppression_events = _service(
        tmp_path,
        enrollment_ttl_seconds=0.2,
    )
    await service.initialize()
    lease_released = False

    class ShortLease:
        def __init__(self, expires_at: float) -> None:
            self.expires_at = expires_at

        async def release(self) -> bool:
            nonlocal lease_released
            lease_released = True
            suppression_events.append("restore:voice_identity_enrollment")
            return True

    async def slow_acquire(reason: str, *, ttl_seconds: float):
        del ttl_seconds
        suppression_events.append(f"suppress:{reason}")
        expires_at = asyncio.get_running_loop().time() + 0.02
        await asyncio.sleep(0.05)
        return ShortLease(expires_at)

    monkeypatch.setattr(service._suppression_controller, "acquire", slow_acquire)  # type: ignore[attr-defined]

    await service.start_enrollment()
    await _wait_until(
        lambda: (
            lease_released
            and model.closed
            and service.status().enrollment is None
        ),
        timeout_seconds=0.1,
    )

    assert lease_released
    assert model.closed
    assert suppression_events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_enrollment_completion_is_rejected_and_cleaned(
    tmp_path: Path,
) -> None:
    service, model, _activations, suppression_events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    session = service._enrollment  # type: ignore[attr-defined]
    assert session is not None
    session.expires_at = asyncio.get_running_loop().time() - 0.001

    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await service.complete_enrollment(enrollment.enrollment_id, "profile-a", _pcm())

    assert service.status().enrollment is None
    assert model.closed
    assert suppression_events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_failure_rolls_back_old_activation_and_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "profile-a", _pcm())
    second = await service.start_enrollment()

    async def fail_commit() -> None:
        raise RuntimeError("commit failed")

    original_stage = service._profile_store.astage  # type: ignore[attr-defined]

    async def staged_with_failed_commit(profile: SpeakerProfile, *, audio_contract):
        staged = await original_stage(profile, audio_contract=audio_contract)
        monkeypatch.setattr(staged, "acommit", fail_commit)
        return staged

    monkeypatch.setattr(service._profile_store, "astage", staged_with_failed_commit)  # type: ignore[attr-defined]
    with pytest.raises(VoiceIdentityServiceError, match="runtime_degraded"):
        await service.complete_enrollment(second.enrollment_id, "profile-b", _pcm())

    assert activations[-1][1] == "profile-a"
    assert service.status().state.effective_enabled
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_reenrollment_restores_requested_unsupported_activation(
    tmp_path: Path,
) -> None:
    service, _model, activations, _events = _service(
        tmp_path,
        activation_results=[
            VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE,
            VoiceIdentityActivationResult.RUNTIME_DEGRADED,
            VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE,
        ],
    )
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "profile-a", _pcm())
    second = await service.start_enrollment()

    with pytest.raises(VoiceIdentityServiceError, match="runtime_degraded"):
        await service.complete_enrollment(second.enrollment_id, "profile-b", _pcm())

    status = service.status()
    assert status.state.requested_enabled
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "unsupported_asr_route"
    assert status.profile_generation == "profile-a"
    assert [generation for _profile, generation in activations[-2:]] == [
        "profile-b",
        "profile-a",
    ]
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_reenrollment_activation_restores_previous_profile(
    tmp_path: Path,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "profile-a", _pcm())
    second = await service.start_enrollment()
    activation_started = asyncio.Event()
    activation_release = asyncio.Event()

    async def blocking_activate(
        profile: SpeakerProfile | None,
        generation: str,
    ) -> bool:
        activations.append((profile, generation))
        if generation == "profile-b":
            activation_started.set()
            await activation_release.wait()
        return True

    service._activation_callback = blocking_activate  # type: ignore[attr-defined]
    completion = asyncio.create_task(
        service.complete_enrollment(second.enrollment_id, "profile-b", _pcm())
    )
    await asyncio.wait_for(activation_started.wait(), 1.0)
    completion.cancel()
    activation_release.set()

    with pytest.raises(asyncio.CancelledError):
        await completion

    status = service.status()
    assert status.state.requested_enabled
    assert status.state.effective_enabled
    assert status.profile_generation == "profile-a"
    assert [generation for _profile, generation in activations[-2:]] == [
        "profile-b",
        "profile-a",
    ]
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_filter_toggle_delete_and_completion_retry(tmp_path: Path) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    first = await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )
    retry = await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        b"not reprocessed",
    )
    assert first.verification is not None
    assert first.verification.passed
    assert retry == service.status()
    assert retry.verification is None

    disabled = await service.set_filter(False)
    assert not disabled.state.requested_enabled
    assert not disabled.state.effective_enabled
    assert activations[-1][0] is None
    enabled = await service.set_filter(True)
    assert enabled.state.effective_enabled

    deleted = await service.delete_profile()
    assert not deleted.state.has_profile
    assert not deleted.state.requested_enabled
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_stays_valid_while_filter_disable_detaches(
    tmp_path: Path,
) -> None:
    service, _model, _activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(enrollment.enrollment_id, "profile-a", _pcm())
    detach_started = asyncio.Event()
    detach_release = asyncio.Event()

    async def blocking_activate(
        profile: SpeakerProfile | None,
        generation: str,
    ) -> bool:
        del generation
        if profile is None:
            detach_started.set()
            await detach_release.wait()
        return True

    service._activation_callback = blocking_activate  # type: ignore[attr-defined]
    disable = asyncio.create_task(service.set_filter(False))
    await asyncio.wait_for(detach_started.wait(), 1.0)

    status = service.status()
    assert not status.state.requested_enabled
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "disabled"

    detach_release.set()
    await disable
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_stays_valid_while_profile_delete_detaches(
    tmp_path: Path,
) -> None:
    service, _model, _activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(enrollment.enrollment_id, "profile-a", _pcm())
    detach_started = asyncio.Event()
    detach_release = asyncio.Event()

    async def blocking_activate(
        profile: SpeakerProfile | None,
        generation: str,
    ) -> bool:
        del generation
        if profile is None:
            detach_started.set()
            await detach_release.wait()
        return True

    service._activation_callback = blocking_activate  # type: ignore[attr-defined]
    deletion = asyncio.create_task(service.delete_profile())
    await asyncio.wait_for(detach_started.wait(), 1.0)

    status = service.status()
    assert not status.state.requested_enabled
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "disabled"

    detach_release.set()
    await deletion
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_pcm_preserves_session_for_current_segment_retry(
    tmp_path: Path,
) -> None:
    service, model, _activations, events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()

    with pytest.raises(VoiceIdentityServiceError, match="speech_too_short"):
        await service.complete_enrollment(
            enrollment.enrollment_id,
            "profile-a",
            b"\x00\x00",
        )

    status = service.status().enrollment
    assert status is not None
    assert status.profile_id is None
    assert status.next_segment_index == 1
    assert service.status().state.effective_reason == "enrollment_active"
    assert not model.closed
    assert events == ["suppress:voice_identity_enrollment"]
    assert await service.cancel_enrollment(enrollment.enrollment_id)
    assert model.closed
    assert events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_activation_failure_aborts_first_profile_transaction(
    tmp_path: Path,
) -> None:
    service, model, _activations, events = _service(
        tmp_path,
        activation_results=[False],
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    with pytest.raises(VoiceIdentityServiceError, match="runtime_degraded"):
        await service.complete_enrollment(
            enrollment.enrollment_id,
            "profile-a",
            _pcm(),
        )

    status = service.status()
    assert not status.state.has_profile
    assert not status.state.requested_enabled
    assert status.state.effective_reason == "runtime_degraded"
    assert not (tmp_path / "voice_identity.profile").exists()
    assert model.closed
    assert events[-1] == "restore:voice_identity_enrollment"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_reenrollment_marks_degraded_when_old_activation_cannot_restore(
    tmp_path: Path,
) -> None:
    service, _model, activations, _events = _service(
        tmp_path,
        activation_results=[True, False, False],
    )
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "profile-a", _pcm())
    second = await service.start_enrollment()

    with pytest.raises(VoiceIdentityServiceError, match="runtime_degraded"):
        await service.complete_enrollment(second.enrollment_id, "profile-b", _pcm())

    status = service.status()
    assert status.profile_generation == "profile-a"
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "runtime_degraded"
    assert [generation for _profile, generation in activations[-2:]] == [
        "profile-b",
        "profile-a",
    ]
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_timed_out_model_load_is_cancelled_and_model_is_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingModel(_Model):
        def __init__(self) -> None:
            super().__init__()
            self.load_started = threading.Event()
            self.load_release = threading.Event()
            self.close_finished = threading.Event()
            self.load_calls = 0

        def load(self) -> bool:
            self.load_calls += 1
            self.load_started.set()
            if not self.load_release.wait(1.0):
                raise TimeoutError("test did not release model load")
            return True

        def cancel_load(self) -> None:
            self.load_release.set()

        def close(self) -> None:
            assert self.load_release.is_set()
            super().close()
            self.close_finished.set()

    model = BlockingModel()
    retry_model = BlockingModel()
    retry_model.load_release.set()
    models = iter((model, retry_model))
    service, _selected, _activations, _events = _service(
        tmp_path,
        model=model,
        model_timeout_seconds=0.1,
    )
    monkeypatch.setattr(service, "_model_factory", lambda: next(models))
    await service.initialize()

    with pytest.raises(VoiceIdentityServiceError, match="model_unavailable"):
        await service.start_enrollment()
    assert await asyncio.to_thread(model.load_started.wait, 1.0)
    assert await asyncio.to_thread(model.close_finished.wait, 1.0)
    assert model.closed
    assert service._model_load_cleanup_task is None  # type: ignore[attr-defined]
    retry = await service.start_enrollment()
    assert model.load_calls == 1
    assert retry_model.load_calls == 1
    assert not retry_model.closed
    assert await service.cancel_enrollment(retry.enrollment_id)
    assert retry_model.closed
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_expired_completion_retains_cleanup_to_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, model, _activations, suppression_events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    session = service._enrollment  # type: ignore[attr-defined]
    assert session is not None
    session.expires_at = asyncio.get_running_loop().time() - 1.0
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_completed = False
    original_cleanup = service._cleanup_session  # type: ignore[attr-defined]

    async def blocking_cleanup(cleanup_session) -> bool:
        nonlocal cleanup_completed
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_ok = await original_cleanup(cleanup_session)
        cleanup_completed = True
        return cleanup_ok

    monkeypatch.setattr(service, "_cleanup_session", blocking_cleanup)
    completion = asyncio.create_task(
        service.complete_enrollment(enrollment.enrollment_id, "profile", _pcm())
    )
    await asyncio.wait_for(cleanup_started.wait(), 1.0)
    completion.cancel()
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await completion

    assert cleanup_completed
    assert model.closed
    assert suppression_events[-1] == "restore:voice_identity_enrollment"
    assert not service._suppression_controller.snapshot().active  # type: ignore[attr-defined]
    assert service._enrollment is None  # type: ignore[attr-defined]
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_timed_out_embedding_is_cancelled_and_model_is_released(
    tmp_path: Path,
) -> None:
    class BlockingModel(_Model):
        def __init__(self) -> None:
            super().__init__()
            self.embedding_started = threading.Event()
            self.embedding_release = threading.Event()
            self.close_finished = threading.Event()
            self.load_calls = 0

        def load(self) -> bool:
            self.load_calls += 1
            return True

        def embedding_from_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
        ) -> np.ndarray:
            self.embedding_started.set()
            if not self.embedding_release.wait(1.0):
                raise TimeoutError("test did not release model inference")
            return super().embedding_from_pcm16(
                pcm16,
                sample_rate_hz=sample_rate_hz,
            )

        def cancel_inference(self) -> None:
            self.embedding_release.set()

        def close(self) -> None:
            assert self.embedding_release.is_set()
            super().close()
            self.close_finished.set()

    model = BlockingModel()
    service, _selected, _activations, suppression_events = _service(
        tmp_path,
        model=model,
        model_timeout_seconds=0.1,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    with pytest.raises(VoiceIdentityServiceError, match="model_unavailable"):
        await service.complete_enrollment(
            enrollment.enrollment_id,
            "profile",
            _pcm(),
        )
    assert await asyncio.to_thread(model.embedding_started.wait, 1.0)
    assert model.closed
    assert suppression_events[-1] == "restore:voice_identity_enrollment"
    assert await asyncio.to_thread(model.close_finished.wait, 1.0)
    assert service._model_inference_cleanup_task is None  # type: ignore[attr-defined]
    retry = await service.start_enrollment()
    assert model.load_calls == 2
    assert await service.cancel_enrollment(retry.enrollment_id)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_completed_timed_out_embedding_is_cleared_before_model_close(
    tmp_path: Path,
) -> None:
    class RacingModel(_Model):
        def __init__(self) -> None:
            super().__init__()
            self.embedding_release = threading.Event()
            self.embedding_finished = threading.Event()
            self.embedding_result: np.ndarray | None = None

        def embedding_from_pcm16(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
        ) -> np.ndarray:
            if not self.embedding_release.wait(1.0):
                raise TimeoutError("test did not release model inference")
            self.embedding_result = super().embedding_from_pcm16(
                pcm16,
                sample_rate_hz=sample_rate_hz,
            )
            self.embedding_finished.set()
            return self.embedding_result

        def close(self) -> None:
            assert self.embedding_result is not None
            assert not np.any(self.embedding_result)
            super().close()

    model = RacingModel()
    service, _selected, _activations, _suppression_events = _service(
        tmp_path,
        model=model,
        model_timeout_seconds=0.1,
    )
    await service.initialize()
    enrollment = await service.start_enrollment()
    session = service._enrollment  # type: ignore[attr-defined]
    assert session is not None
    original_lease = session.lease

    class ReleaseAfterInference:
        expires_at = original_lease.expires_at

        async def release(self) -> None:
            model.embedding_release.set()
            assert await asyncio.to_thread(model.embedding_finished.wait, 1.0)
            task = session.embedding_task
            assert task is not None
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            await original_lease.release()

    session.lease = ReleaseAfterInference()  # type: ignore[assignment]

    with pytest.raises(VoiceIdentityServiceError, match="model_unavailable"):
        await service.complete_enrollment(
            enrollment.enrollment_id,
            "profile",
            _pcm(),
        )

    assert model.closed
    assert model.embedding_result is not None
    assert not np.any(model.embedding_result)
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reenrollment_while_disabled_keeps_user_preference(
    tmp_path: Path,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "profile-a", _pcm())
    await service.set_filter(False)
    activation_count = len(activations)

    second = await service.start_enrollment()
    status = await service.complete_enrollment(
        second.enrollment_id,
        "profile-b",
        _pcm(),
    )

    assert not status.state.requested_enabled
    assert not status.state.effective_enabled
    assert status.state.has_profile
    assert len(activations) == activation_count
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_initialize_restores_encrypted_profile_and_preference(
    tmp_path: Path,
) -> None:
    first_service, _model, _activations, _events = _service(tmp_path)
    await first_service.initialize()
    enrollment = await first_service.start_enrollment()
    await first_service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )
    await first_service.close()

    restored, _restored_model, activations, _restored_events = _service(tmp_path)
    status = await restored.initialize()

    assert status.state.requested_enabled
    assert status.state.effective_enabled
    assert status.state.has_profile
    assert activations[-1][1] == "profile-a"
    await restored.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_off_mode_records_profile_without_runtime_activation(
    tmp_path: Path,
) -> None:
    service, _model, activations, _events = _service(
        tmp_path,
        runtime_mode="off",
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    status = await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )

    assert status.runtime_mode == "off"
    assert status.state.requested_enabled
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "runtime_degraded"
    assert activations == []
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shadow_mode_records_profile_without_reporting_enforced(
    tmp_path: Path,
) -> None:
    service, _model, activations, _events = _service(
        tmp_path,
        runtime_mode="shadow",
    )
    await service.initialize()
    enrollment = await service.start_enrollment()

    status = await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )

    assert status.runtime_mode == "shadow"
    assert status.state.requested_enabled
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "shadow_mode"
    assert activations[-1][1] == "profile-a"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suppression_failure_closes_loaded_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, model, _activations, _events = _service(tmp_path)
    await service.initialize()

    async def fail_acquire(*_args, **_kwargs):
        raise RuntimeError("suppression unavailable")

    monkeypatch.setattr(
        service._suppression_controller,  # type: ignore[attr-defined]
        "acquire",
        fail_acquire,
    )
    with pytest.raises(VoiceIdentityServiceError, match="runtime_degraded"):
        await service.start_enrollment()

    assert model.closed
    assert service.status().enrollment is None
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_suppression_acquire_closes_loaded_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, model, _activations, _events = _service(tmp_path)
    await service.initialize()
    acquire_started = asyncio.Event()

    async def block_acquire(*_args, **_kwargs):
        acquire_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        service._suppression_controller,  # type: ignore[attr-defined]
        "acquire",
        block_acquire,
    )
    enrollment = asyncio.create_task(service.start_enrollment())
    await acquire_started.wait()
    enrollment.cancel()

    with pytest.raises(asyncio.CancelledError):
        await enrollment

    assert model.closed
    assert service.status().enrollment is None
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_guards_and_status_shape(tmp_path: Path) -> None:
    service, _model, _activations, _events = _service(tmp_path)
    with pytest.raises(VoiceIdentityServiceError, match="not_initialized"):
        await service.start_enrollment()
    with pytest.raises(TypeError, match="enabled"):
        await service.set_filter(1)  # type: ignore[arg-type]

    initial = await service.initialize()
    assert await service.initialize() == initial
    assert initial.as_dict() == {
        "requested_enabled": False,
        "effective_enabled": False,
        "effective_reason": "disabled",
        "has_profile": False,
        "enrollment": None,
        "profile_generation": None,
        "runtime_mode": "enforce",
        "last_completed_enrollment_id": None,
    }
    enrollment = await service.start_enrollment()
    duplicate = await service.start_enrollment()
    assert duplicate == enrollment
    assert duplicate.as_dict()["enrollment_id"] == enrollment.enrollment_id
    assert not await service.cancel_enrollment("different-enrollment")
    assert await service.cancel_enrollment(enrollment.enrollment_id)
    with pytest.raises(VoiceIdentityServiceError, match="invalid_profile_id"):
        await service.complete_enrollment("enrollment", "", _pcm())
    with pytest.raises(VoiceIdentityServiceError, match="stale_enrollment"):
        await service.complete_enrollment("enrollment", "profile", _pcm())

    await service.close()
    await service.close()
    with pytest.raises(VoiceIdentityServiceError, match="service_closed"):
        await service.start_enrollment()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_profile_commit_keeps_memory_and_disk_on_new_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "profile-a", _pcm())
    second = await service.start_enrollment()
    replace_started = threading.Event()
    replace_release = threading.Event()
    original_replace = store_module._replace

    def blocking_replace(source: Path, destination: Path) -> None:
        replace_started.set()
        if not replace_release.wait(1.0):
            raise TimeoutError("test did not release profile commit")
        original_replace(source, destination)

    monkeypatch.setattr(store_module, "_replace", blocking_replace)
    completion = asyncio.create_task(
        service.complete_enrollment(second.enrollment_id, "profile-b", _pcm())
    )
    assert await asyncio.to_thread(replace_started.wait, 1.0)
    completion.cancel()
    replace_release.set()

    with pytest.raises(asyncio.CancelledError):
        await completion

    status = service.status()
    assert status.state.effective_enabled
    assert status.profile_generation == "profile-b"
    assert activations[-1][1] == "profile-b"
    stored = await service._profile_store.aload()  # type: ignore[attr-defined]
    assert stored is not None
    try:
        assert stored.profile.generation == "profile-b"
    finally:
        stored.close()
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            SecureStorageUnavailableError("secure_storage_unavailable"),
            "secure_storage_unavailable",
        ),
        (
            VoiceIdentityProfileIncompatibleError("incompatible"),
            "profile_incompatible",
        ),
        (
            VoiceIdentityProfileCorruptError("corrupt"),
            "runtime_degraded",
        ),
    ],
)
async def test_initialize_maps_profile_storage_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    reason: str,
) -> None:
    service, _model, _activations, _events = _service(tmp_path)

    async def fail_load():
        raise failure

    monkeypatch.setattr(
        service._profile_store,  # type: ignore[attr-defined]
        "aload",
        fail_load,
    )
    status = await service.initialize()
    assert status.state.effective_reason == reason
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_initialize_maps_preference_failure_and_incompatible_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, _model, _activations, _events = _service(tmp_path / "broken")

    async def fail_preference():
        raise VoiceIdentityPreferenceStoreError("corrupt")

    monkeypatch.setattr(
        broken._preference_store,  # type: ignore[attr-defined]
        "aload",
        fail_preference,
    )
    assert (await broken.initialize()).state.effective_reason == "runtime_degraded"
    await broken.close()

    service, _model, _activations, _events = _service(
        tmp_path / "incompatible",
        runtime_status_results=[VoiceIdentityActivationResult.READY],
    )
    reference = SpeakerReference(
        SpeakerModelIdentity("other-model", "v1", 2),
        [1.0, 0.0],
    )
    try:
        profile = SpeakerProfile("incompatible", reference)
    finally:
        reference.close()
    try:
        await service._profile_store.asave(  # type: ignore[attr-defined]
            profile,
            audio_contract=desktop_audio_contract_snapshot(
                noise_reduction_enabled=True,
            ),
        )
        await service._preference_store.asave(True)  # type: ignore[attr-defined]
    finally:
        profile.close()

    status = await service.initialize()
    assert status.state.effective_reason == "profile_incompatible"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_failure_still_disables_requested_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )

    async def fail_delete() -> bool:
        raise VoiceIdentityProfileStoreError("delete failed")

    monkeypatch.setattr(
        service._profile_store,  # type: ignore[attr-defined]
        "adelete",
        fail_delete,
    )
    with pytest.raises(VoiceIdentityServiceError, match="runtime_degraded"):
        await service.delete_profile()

    status = service.status()
    assert status.state.requested_enabled
    assert status.state.has_profile
    assert status.state.effective_enabled
    assert activations[-1][0] is not None
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_noise_reduction_mismatch_detaches_and_restore_reactivates(
    tmp_path: Path,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )

    assert await service.prepare_runtime_audio_contract_change()
    mismatched = await service.update_runtime_noise_reduction_enabled(False)
    assert not mismatched.state.effective_enabled
    assert mismatched.state.effective_reason == "audio_contract_mismatch"
    assert activations[-1][0] is None

    assert await service.prepare_runtime_audio_contract_change()
    restored = await service.update_runtime_noise_reduction_enabled(True)
    assert restored.state.effective_enabled
    assert restored.state.effective_reason == "ready"
    assert activations[-1][0] is not None
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_rolls_back_profile_when_preference_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )

    async def fail_preference(_enabled: bool) -> None:
        raise VoiceIdentityPreferenceStoreError("write failed")

    monkeypatch.setattr(
        service._preference_store,  # type: ignore[attr-defined]
        "asave",
        fail_preference,
    )
    with pytest.raises(VoiceIdentityServiceError, match="runtime_degraded"):
        await service.delete_profile()

    restored = await service._profile_store.aload()  # type: ignore[attr-defined]
    assert restored is not None
    try:
        assert restored.profile.generation == "profile-a"
    finally:
        restored.close()
    status = service.status()
    assert status.state.requested_enabled
    assert status.state.has_profile
    assert status.state.effective_enabled
    assert activations[-1][0] is not None
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_revokes_activation_when_profile_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(
        enrollment.enrollment_id,
        "profile-a",
        _pcm(),
    )
    old_profile = service._profile  # type: ignore[attr-defined]
    assert old_profile is not None

    async def fail_preference(_enabled: bool) -> None:
        raise VoiceIdentityPreferenceStoreError("write failed")

    async def fail_restore(_profile: SpeakerProfile, *, audio_contract) -> None:
        del audio_contract
        raise VoiceIdentityProfileStoreError("restore failed")

    monkeypatch.setattr(
        service._preference_store,  # type: ignore[attr-defined]
        "asave",
        fail_preference,
    )
    monkeypatch.setattr(
        service._profile_store,  # type: ignore[attr-defined]
        "asave",
        fail_restore,
    )
    with pytest.raises(VoiceIdentityServiceError, match="runtime_degraded"):
        await service.delete_profile()

    assert activations[-1][0] is None
    assert old_profile.closed
    assert await service._profile_store.aload() is None  # type: ignore[attr-defined]
    status = service.status()
    assert status.state.requested_enabled
    assert not status.state.has_profile
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "runtime_degraded"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_filter_write_reconciles_runtime_and_preference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(enrollment.enrollment_id, "profile-a", _pcm())
    save_started = threading.Event()
    save_release = threading.Event()
    preference_store = service._preference_store  # type: ignore[attr-defined]
    original_save = preference_store.save

    def blocking_save(enabled: bool) -> None:
        save_started.set()
        assert save_release.wait(1.0)
        original_save(enabled)

    monkeypatch.setattr(preference_store, "save", blocking_save)
    update = asyncio.create_task(service.set_filter(False))
    assert await asyncio.to_thread(save_started.wait, 1.0)
    update.cancel()
    save_release.set()

    with pytest.raises(asyncio.CancelledError):
        await update

    status = service.status()
    assert not status.state.requested_enabled
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "disabled"
    assert not await preference_store.aload()
    assert activations[-1][0] is None
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_enrollment_cancel_retains_cleanup_to_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, _activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_completed = False

    async def blocking_cleanup(session) -> bool:
        nonlocal cleanup_completed
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_completed = True
        return True

    monkeypatch.setattr(service, "_cleanup_session", blocking_cleanup)
    cancellation = asyncio.create_task(service.cancel_enrollment(enrollment.enrollment_id))
    await asyncio.wait_for(cleanup_started.wait(), 1.0)
    cancellation.cancel()
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await cancellation

    assert cleanup_completed
    assert service.status().state.effective_reason == "disabled"
    await service.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_close_retains_cleanup_to_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "profile-a", _pcm())
    old_profile = service._profile  # type: ignore[attr-defined]
    assert old_profile is not None
    await service.start_enrollment()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_completed = False

    async def blocking_cleanup(session) -> bool:
        nonlocal cleanup_completed
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_completed = True
        return True

    monkeypatch.setattr(service, "_cleanup_session", blocking_cleanup)
    shutdown = asyncio.create_task(service.close())
    await asyncio.wait_for(cleanup_started.wait(), 1.0)
    shutdown.cancel()
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert cleanup_completed
    assert service._enrollment is None  # type: ignore[attr-defined]
    assert old_profile.closed
    assert activations[-1][0] is None
    status = service.status()
    assert not status.state.has_profile
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "disabled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_profile_delete_reconciles_memory_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _model, activations, _events = _service(tmp_path)
    await service.initialize()
    enrollment = await service.start_enrollment()
    await service.complete_enrollment(enrollment.enrollment_id, "profile-a", _pcm())
    old_profile = service._profile  # type: ignore[attr-defined]
    assert old_profile is not None
    delete_started = threading.Event()
    delete_release = threading.Event()
    profile_store = service._profile_store  # type: ignore[attr-defined]
    original_delete = profile_store.delete

    def blocking_delete() -> bool:
        delete_started.set()
        assert delete_release.wait(1.0)
        return original_delete()

    monkeypatch.setattr(profile_store, "delete", blocking_delete)
    deletion = asyncio.create_task(service.delete_profile())
    assert await asyncio.to_thread(delete_started.wait, 1.0)
    deletion.cancel()
    delete_release.set()

    with pytest.raises(asyncio.CancelledError):
        await deletion

    status = service.status()
    assert not status.state.requested_enabled
    assert not status.state.has_profile
    assert not status.state.effective_enabled
    assert status.state.effective_reason == "disabled"
    assert old_profile.closed
    assert await profile_store.aload() is None
    assert activations[-1][0] is None
    await service.close()
