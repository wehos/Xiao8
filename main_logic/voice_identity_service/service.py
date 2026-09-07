"""Fail-open application controller for the single local Owner profile."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
import math
from typing import Literal, Protocol, TypeVar
import uuid

import numpy as np

from main_logic.asr_client import VoiceIdentityActivationResult
from main_logic.asr_client.speaker_shadow.asset_manifest import (
    CAMPPLUS_MODEL_ID,
    CAMPPLUS_MODEL_REVISION,
    CAMPPLUS_SAMPLE_RATE_HZ,
)
from main_logic.asr_client.speaker_shadow.campplus import CAMPPLUS_EMBEDDING_DIM
from main_logic.voice_identity.contracts import SpeakerModelIdentity
from main_logic.voice_identity.profile import SpeakerProfile
from main_logic.voice_identity.reference import SpeakerReference
from main_logic.voice_input.suppression import (
    VoiceInputSuppressionController,
    VoiceInputSuppressionLease,
)

from .audio_contract import (
    OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID,
    OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ,
    VoiceIdentityAudioContractSnapshot,
    desktop_audio_contract_snapshot,
)
from .enrollment import (
    ENROLLMENT_MAXIMUM_PCM_BYTES,
    ENROLLMENT_SAMPLE_RATE_HZ,
    ENROLLMENT_VERIFICATION_MAXIMUM_PCM_BYTES,
    EnrollmentAudioError,
    EnrollmentSpeechValidator,
    EnrollmentSpeechValidatorFactory,
    EnrollmentSpeechValidatorUnavailableError,
    EnrollmentVerificationResult,
    SileroEnrollmentSpeechValidator,
    create_enrollment_reference_centroid,
    verify_enrollment_holdout,
    wipe_enrollment_embedding,
)
from .enrollment_audio import (
    EnrollmentAudioNormalizationError,
    EnrollmentAudioNormalizer,
)
from .preference_store import (
    VoiceIdentityPreferenceStore,
    VoiceIdentityPreferenceStoreError,
)
from .profile_store import (
    SecureStorageUnavailableError,
    VoiceIdentityProfileCorruptError,
    VoiceIdentityProfileIncompatibleError,
    VoiceIdentityProfileStore,
    VoiceIdentityProfileStoreError,
    VoiceIdentityProfileWrite,
)
from .state import VoiceIdentityEffectiveReason, VoiceIdentityState


class EnrollmentEmbeddingModel(Protocol):
    model_id: str
    model_revision: str

    def load(self) -> bool: ...

    def cancel_load(self) -> None: ...

    def embedding_from_pcm16(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
    ) -> np.ndarray: ...

    def cancel_inference(self) -> None: ...

    def close(self) -> None: ...


EnrollmentModelFactory = Callable[[], EnrollmentEmbeddingModel]
EnrollmentAudioNormalizerFactory = Callable[[bool], EnrollmentAudioNormalizer]
ActivationCallback = Callable[
    [SpeakerProfile | None, str],
    Awaitable[bool | VoiceIdentityActivationResult],
]
RuntimeStatusCallback = Callable[[], VoiceIdentityActivationResult]


class PreparedVoiceActivation(Protocol):
    result: VoiceIdentityActivationResult


class VoiceActivationTransaction(Protocol):
    async def prepare_activation(
        self, profile: SpeakerProfile | None, generation: str,
    ) -> PreparedVoiceActivation: ...

    def commit_activation(self, prepared: PreparedVoiceActivation) -> VoiceIdentityActivationResult: ...

    def revoke_prepared_activation(self, prepared: PreparedVoiceActivation) -> None: ...

    async def abort_activation(self, prepared: PreparedVoiceActivation) -> VoiceIdentityActivationResult: ...


VoiceIdentityRuntimeMode = Literal["off", "shadow", "enforce"]
EnrollmentPhase = Literal[
    "collecting_reference",
    "checking_consistency",
    "verifying",
    "committing",
]
_ResultT = TypeVar("_ResultT")


async def _await_cancellation_safe(
    awaitable: Awaitable[_ResultT],
    *,
    name: str,
    cancellations: list[asyncio.CancelledError],
) -> _ResultT:
    task = asyncio.create_task(awaitable, name=name)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if not cancellations:
                cancellations.append(exc)
    return task.result()


class VoiceIdentityServiceError(RuntimeError):
    """A stable, UI-safe control-plane failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _StaleEnrollmentOperation(RuntimeError):
    """An async result whose session ownership was already revoked."""


@dataclass(frozen=True, slots=True)
class EnrollmentStatus:
    enrollment_id: str
    profile_id: str | None
    expires_at: float
    remaining_seconds: float
    accepted_segments: int
    required_segments: int
    next_segment_index: int
    phase: EnrollmentPhase

    def as_dict(self) -> dict[str, object]:
        return {
            "enrollment_id": self.enrollment_id,
            "profile_id": self.profile_id,
            "expires_at": self.expires_at,
            "remaining_seconds": self.remaining_seconds,
            "accepted_segments": self.accepted_segments,
            "required_segments": self.required_segments,
            "next_segment_index": self.next_segment_index,
            "phase": self.phase,
        }


@dataclass(frozen=True, slots=True)
class VoiceIdentityServiceStatus:
    state: VoiceIdentityState
    enrollment: EnrollmentStatus | None
    profile_generation: str | None
    runtime_mode: VoiceIdentityRuntimeMode
    last_completed_enrollment_id: str | None
    verification: EnrollmentVerificationResult | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = self.state.as_dict()
        result["enrollment"] = (
            None if self.enrollment is None else self.enrollment.as_dict()
        )
        result["profile_generation"] = self.profile_generation
        result["runtime_mode"] = self.runtime_mode
        result["last_completed_enrollment_id"] = self.last_completed_enrollment_id
        if self.verification is not None:
            result["verification"] = self.verification.as_dict()
        return result


@dataclass(slots=True)
class _EnrollmentSession:
    enrollment_id: str
    profile_id: str | None
    expires_at: float
    model: EnrollmentEmbeddingModel
    speech_validator: EnrollmentSpeechValidator
    lease: VoiceInputSuppressionLease
    expiry_task: asyncio.Task[None]
    session_generation: int
    requested_enabled_snapshot: bool
    noise_reduction_enabled_snapshot: bool
    operation_nonce: int = 0
    in_flight_segment_index: int | None = None
    operation_task: asyncio.Task[object] | None = None
    next_segment_index: int = 1
    phase: EnrollmentPhase = "collecting_reference"
    reference_embeddings: list[np.ndarray] = field(default_factory=list)
    reference_centroid: np.ndarray | None = None
    holdout_failure_count: int = 0
    inference_task: asyncio.Task[np.ndarray] | None = None
    validation_task: asyncio.Task[object] | None = None


@dataclass(slots=True)
class _SegmentComputation:
    reference_embedding: np.ndarray | None = None
    holdout_1_5: np.ndarray | None = None
    holdout_3_0: np.ndarray | None = None
    holdout_5_0: np.ndarray | None = None

    def wipe(self) -> None:
        wipe_enrollment_embedding(self.reference_embedding)
        wipe_enrollment_embedding(self.holdout_1_5)
        wipe_enrollment_embedding(self.holdout_3_0)
        wipe_enrollment_embedding(self.holdout_5_0)
        self.reference_embedding = None
        self.holdout_1_5 = None
        self.holdout_3_0 = None
        self.holdout_5_0 = None


class VoiceIdentityService:
    """Own persistence, enrollment, activation, and fail-open state."""

    def __init__(
        self,
        profile_store: VoiceIdentityProfileStore,
        preference_store: VoiceIdentityPreferenceStore,
        suppression_controller: VoiceInputSuppressionController,
        model_factory: EnrollmentModelFactory,
        activation_callback: ActivationCallback,
        *,
        runtime_mode: VoiceIdentityRuntimeMode = "enforce",
        enrollment_ttl_seconds: float = 45.0,
        model_timeout_seconds: float = 30.0,
        activation_timeout_seconds: float = 5.0,
        runtime_status_callback: RuntimeStatusCallback | None = None,
        activation_transaction: VoiceActivationTransaction | None = None,
        speech_validator_factory: EnrollmentSpeechValidatorFactory | None = None,
        enrollment_audio_normalizer_factory: (
            EnrollmentAudioNormalizerFactory | None
        ) = None,
        enrollment_noise_reduction_enabled: bool = True,
    ) -> None:
        if not isinstance(profile_store, VoiceIdentityProfileStore):
            raise TypeError("profile_store must be VoiceIdentityProfileStore")
        if not isinstance(preference_store, VoiceIdentityPreferenceStore):
            raise TypeError("preference_store must be VoiceIdentityPreferenceStore")
        if not isinstance(
            suppression_controller,
            VoiceInputSuppressionController,
        ):
            raise TypeError(
                "suppression_controller must be VoiceInputSuppressionController"
            )
        if not callable(model_factory) or not callable(activation_callback):
            raise TypeError("model_factory and activation_callback must be callable")
        if runtime_status_callback is not None and not callable(
            runtime_status_callback
        ):
            raise TypeError("runtime_status_callback must be callable or None")
        if speech_validator_factory is not None and not callable(
            speech_validator_factory
        ):
            raise TypeError("speech_validator_factory must be callable or None")
        if (
            enrollment_audio_normalizer_factory is not None
            and not callable(enrollment_audio_normalizer_factory)
        ):
            raise TypeError(
                "enrollment_audio_normalizer_factory must be callable or None"
            )
        if type(enrollment_noise_reduction_enabled) is not bool:
            raise TypeError("enrollment_noise_reduction_enabled must be bool")
        if runtime_mode not in ("off", "shadow", "enforce"):
            raise ValueError("runtime_mode must be off, shadow, or enforce")
        for name, value in (
            ("enrollment_ttl_seconds", enrollment_ttl_seconds),
            ("model_timeout_seconds", model_timeout_seconds),
            ("activation_timeout_seconds", activation_timeout_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if enrollment_ttl_seconds > 45.0:
            raise ValueError("enrollment_ttl_seconds cannot exceed 45 seconds")

        self._profile_store = profile_store
        self._preference_store = preference_store
        self._suppression_controller = suppression_controller
        self._model_factory = model_factory
        self._speech_validator_factory = (
            speech_validator_factory or SileroEnrollmentSpeechValidator
        )
        self._enrollment_audio_normalizer_factory = (
            enrollment_audio_normalizer_factory
            or (lambda enabled: EnrollmentAudioNormalizer(nr_enabled=enabled))
        )
        self._runtime_noise_reduction_enabled = enrollment_noise_reduction_enabled
        self._activation_callback = activation_callback
        self._activation_transaction = activation_transaction
        self._runtime_status_callback = runtime_status_callback
        self._runtime_mode: VoiceIdentityRuntimeMode = runtime_mode
        self._enrollment_ttl_seconds = float(enrollment_ttl_seconds)
        self._model_timeout_seconds = float(model_timeout_seconds)
        self._activation_timeout_seconds = float(activation_timeout_seconds)
        self._operation_lock = asyncio.Lock()
        self._profile: SpeakerProfile | None = None
        self._profile_audio_contract: VoiceIdentityAudioContractSnapshot | None = None
        self._requested_enabled = False
        self._effective_enabled = False
        self._effective_reason = VoiceIdentityEffectiveReason.DISABLED
        self._enrollment: _EnrollmentSession | None = None
        self._last_completed: tuple[str, str] | None = None
        self._enrollment_generation = 0
        self._model_load_cleanup_task: asyncio.Task[None] | None = None
        self._speech_validator_load_cleanup_task: asyncio.Task[None] | None = None
        self._model_inference_cleanup_task: asyncio.Task[None] | None = None
        self._initialized = False
        self._closed = False

    async def initialize(self) -> VoiceIdentityServiceStatus:
        async with self._operation_lock:
            self._require_open()
            if self._initialized:
                return self.status()
            try:
                requested_enabled = await self._preference_store.aload()
            except VoiceIdentityPreferenceStoreError:
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
                self._initialized = True
                return self.status()
            try:
                stored_profile = await self._profile_store.aload()
            except SecureStorageUnavailableError:
                self._requested_enabled = requested_enabled
                self._set_ineffective(
                    VoiceIdentityEffectiveReason.SECURE_STORAGE_UNAVAILABLE
                )
                self._initialized = True
                return self.status()
            except VoiceIdentityProfileIncompatibleError:
                self._requested_enabled = requested_enabled
                self._set_ineffective(VoiceIdentityEffectiveReason.PROFILE_INCOMPATIBLE)
                self._initialized = True
                return self.status()
            except VoiceIdentityProfileCorruptError:
                self._requested_enabled = requested_enabled
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
                self._initialized = True
                return self.status()
            except VoiceIdentityProfileStoreError:
                self._requested_enabled = requested_enabled
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
                self._initialized = True
                return self.status()

            self._requested_enabled = requested_enabled
            profile = None if stored_profile is None else stored_profile.profile
            profile_audio_contract = (
                None if stored_profile is None else stored_profile.audio_contract
            )
            self._profile = profile
            self._profile_audio_contract = profile_audio_contract
            if profile is None:
                self._set_ineffective(
                    VoiceIdentityEffectiveReason.NO_PROFILE
                    if requested_enabled
                    else VoiceIdentityEffectiveReason.DISABLED
                )
            elif not self._profile_is_compatible(profile):
                self._set_ineffective(VoiceIdentityEffectiveReason.PROFILE_INCOMPATIBLE)
            elif not self._audio_contract_matches_runtime(profile_audio_contract):
                self._set_ineffective(
                    VoiceIdentityEffectiveReason.AUDIO_CONTRACT_MISMATCH
                )
            elif not requested_enabled:
                self._set_ineffective(VoiceIdentityEffectiveReason.DISABLED)
            elif self._runtime_mode == "off":
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
            else:
                self._apply_activation_result(
                    await self._activate(profile, profile.generation)
                )
            self._initialized = True
            return self.status()

    def status(self) -> VoiceIdentityServiceStatus:
        if (
            self._runtime_status_callback is not None
            and self._requested_enabled
            and self._profile is not None
            and self._runtime_mode != "off"
            and self._effective_reason
            in {
                VoiceIdentityEffectiveReason.READY,
                VoiceIdentityEffectiveReason.RUNTIME_DEGRADED,
                VoiceIdentityEffectiveReason.UNSUPPORTED_ASR_ROUTE,
            }
        ):
            try:
                self._apply_activation_result(self._runtime_status_callback())
            except Exception:
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
        enrollment = self._enrollment
        enrollment_status = (
            None
            if enrollment is None
            else self._enrollment_status(enrollment)
        )
        return VoiceIdentityServiceStatus(
            VoiceIdentityState(
                requested_enabled=self._requested_enabled,
                effective_enabled=self._effective_enabled,
                effective_reason=self._effective_reason,
                has_profile=self._profile is not None,
            ),
            enrollment_status,
            None if self._profile is None else self._profile.generation,
            self._runtime_mode,
            None if self._last_completed is None else self._last_completed[0],
        )

    def _enrollment_status(self, session: _EnrollmentSession) -> EnrollmentStatus:
        try:
            remaining_seconds = max(
                0.0,
                session.expires_at - asyncio.get_running_loop().time(),
            )
        except RuntimeError:
            remaining_seconds = 0.0
        return EnrollmentStatus(
            enrollment_id=session.enrollment_id,
            profile_id=session.profile_id,
            expires_at=session.expires_at,
            remaining_seconds=remaining_seconds,
            accepted_segments=max(0, session.next_segment_index - 1),
            required_segments=4,
            next_segment_index=session.next_segment_index,
            phase=session.phase,
        )

    async def start_enrollment(self) -> EnrollmentStatus:
        async with self._operation_lock:
            self._require_initialized()
            if self._enrollment is not None:
                return self._enrollment_status(self._enrollment)
            cleanup_task = self._model_load_cleanup_task
            if cleanup_task is not None:
                if not cleanup_task.done():
                    self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                    raise VoiceIdentityServiceError("model_unavailable")
                self._model_load_cleanup_task = None
            cleanup_task = self._model_inference_cleanup_task
            if cleanup_task is not None:
                if not cleanup_task.done():
                    self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                    raise VoiceIdentityServiceError("model_unavailable")
                self._model_inference_cleanup_task = None
            cleanup_task = self._speech_validator_load_cleanup_task
            if cleanup_task is not None:
                if not cleanup_task.done():
                    self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                    raise VoiceIdentityServiceError("model_unavailable")
                self._speech_validator_load_cleanup_task = None
            try:
                model = self._model_factory()
            except Exception as exc:
                self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                raise VoiceIdentityServiceError("model_unavailable") from exc
            load_task = asyncio.create_task(
                asyncio.to_thread(model.load),
                name="voice-identity-model-load",
            )
            try:
                loaded = bool(
                    await asyncio.wait_for(
                        asyncio.shield(load_task),
                        timeout=self._model_timeout_seconds,
                    )
                )
            except TimeoutError:
                try:
                    model.cancel_load()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(
                        asyncio.shield(load_task),
                        timeout=self._model_timeout_seconds,
                    )
                except TimeoutError:
                    self._retain_timed_out_model_load(model, load_task)
                except asyncio.CancelledError:
                    self._retain_timed_out_model_load(model, load_task)
                    raise
                except Exception:
                    await self._close_model(model)
                else:
                    await self._close_model(model)
                self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                raise VoiceIdentityServiceError("model_unavailable")
            except asyncio.CancelledError:
                try:
                    model.cancel_load()
                except Exception:
                    pass
                self._retain_timed_out_model_load(model, load_task)
                raise
            except Exception:
                loaded = False
            if not loaded:
                await self._close_model(model)
                self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                raise VoiceIdentityServiceError("model_unavailable")

            try:
                speech_validator = self._speech_validator_factory()
                validator_load_task = asyncio.create_task(
                    speech_validator.load(),
                    name="voice-identity-speech-validator-load",
                )
                validator_loaded = bool(
                    await asyncio.wait_for(
                        asyncio.shield(validator_load_task),
                        timeout=self._model_timeout_seconds,
                    )
                )
            except TimeoutError as exc:
                self._retain_timed_out_validator_load(
                    model,
                    speech_validator,
                    validator_load_task,
                )
                self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                raise VoiceIdentityServiceError("model_unavailable") from exc
            except asyncio.CancelledError as exc:
                if "validator_load_task" in locals():
                    self._retain_timed_out_validator_load(
                        model,
                        speech_validator,
                        validator_load_task,
                    )
                else:
                    await self._close_model(model)
                raise exc
            except Exception as exc:
                if "speech_validator" in locals():
                    await self._close_speech_validator(speech_validator)
                await self._close_model(model)
                self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                raise VoiceIdentityServiceError("model_unavailable") from exc
            if not validator_loaded:
                await self._close_speech_validator(speech_validator)
                await self._close_model(model)
                self._record_failure(VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE)
                raise VoiceIdentityServiceError("model_unavailable")

            try:
                lease = await self._suppression_controller.acquire(
                    "voice_identity_enrollment",
                    ttl_seconds=self._enrollment_ttl_seconds,
                )
            except asyncio.CancelledError as exc:
                cancellations = [exc]
                await _await_cancellation_safe(
                    self._close_speech_validator(speech_validator),
                    name="voice-identity-cancelled-acquire-validator-close",
                    cancellations=cancellations,
                )
                await _await_cancellation_safe(
                    self._close_model(model),
                    name="voice-identity-cancelled-acquire-model-close",
                    cancellations=cancellations,
                )
                raise cancellations[0]
            except Exception as exc:
                await self._close_speech_validator(speech_validator)
                await self._close_model(model)
                self._record_failure(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
                raise VoiceIdentityServiceError("runtime_degraded") from exc

            enrollment_id = str(uuid.uuid4())
            loop = asyncio.get_running_loop()
            expiry_delay = max(0.0, lease.expires_at - loop.time())
            expiry_task = asyncio.create_task(
                self._expire_enrollment(
                    enrollment_id,
                    expiry_delay,
                ),
                name="voice-identity-enrollment-expiry",
            )
            self._enrollment = _EnrollmentSession(
                enrollment_id=enrollment_id,
                profile_id=None,
                expires_at=lease.expires_at,
                model=model,
                speech_validator=speech_validator,
                lease=lease,
                expiry_task=expiry_task,
                session_generation=self._enrollment_generation + 1,
                requested_enabled_snapshot=(
                    True if self._profile is None else self._requested_enabled
                ),
                noise_reduction_enabled_snapshot=(
                    self._runtime_noise_reduction_enabled
                ),
            )
            self._enrollment_generation += 1
            if not self._effective_enabled:
                self._set_ineffective(VoiceIdentityEffectiveReason.ENROLLMENT_ACTIVE)
            return self._enrollment_status(self._enrollment)

    async def submit_enrollment_segment(
        self,
        enrollment_id: str,
        profile_id: str,
        segment_index: int,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        audio_contract_id: str,
    ) -> VoiceIdentityServiceStatus:
        """Validate and atomically append one enrollment segment."""

        _require_identifier("enrollment_id", enrollment_id)
        _require_identifier("profile_id", profile_id)
        if type(segment_index) is not int or segment_index not in (1, 2, 3, 4):
            raise VoiceIdentityServiceError("segment_out_of_order")
        operation_task = asyncio.current_task()
        if operation_task is None:  # pragma: no cover - asyncio always owns this call
            raise VoiceIdentityServiceError("runtime_degraded")

        async with self._operation_lock:
            self._require_initialized()
            if self._last_completed == (enrollment_id, profile_id):
                return self.status()
            if type(pcm16) is not bytes or len(pcm16) % 2:
                raise VoiceIdentityServiceError("invalid_pcm")
            if (
                sample_rate_hz != OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ
                or audio_contract_id != OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID
            ):
                raise VoiceIdentityServiceError("unsupported_audio_contract")
            maximum_raw_pcm_bytes = (
                OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ
                * (5 if segment_index == 4 else 4)
                * 2
            )
            if len(pcm16) > maximum_raw_pcm_bytes:
                raise VoiceIdentityServiceError("audio_too_long")
            session = self._enrollment
            if session is None or session.enrollment_id != enrollment_id:
                raise VoiceIdentityServiceError("stale_enrollment")
            if session.profile_id is not None and session.profile_id != profile_id:
                raise VoiceIdentityServiceError("stale_enrollment")
            if self._session_expired(session):
                await self._retire_expired_session(session)
                raise VoiceIdentityServiceError("stale_enrollment")
            if segment_index < session.next_segment_index:
                return self.status()
            if segment_index > session.next_segment_index:
                raise VoiceIdentityServiceError("segment_out_of_order")
            if session.in_flight_segment_index is not None:
                raise VoiceIdentityServiceError("segment_in_progress")
            session.operation_nonce += 1
            operation_nonce = session.operation_nonce
            session_generation = session.session_generation
            session.in_flight_segment_index = segment_index
            session.operation_task = operation_task
            session.phase = (
                "checking_consistency"
                if segment_index == 3
                else "verifying"
                if segment_index == 4
                else "collecting_reference"
            )

        try:
            computed = await self._compute_enrollment_segment(
                session,
                segment_index,
                pcm16,
                sample_rate_hz=sample_rate_hz,
                session_generation=session_generation,
                operation_nonce=operation_nonce,
                operation_task=operation_task,
            )
        except _StaleEnrollmentOperation as exc:
            raise VoiceIdentityServiceError("stale_enrollment") from exc
        except EnrollmentAudioError as exc:
            async with self._operation_lock:
                if self._session_operation_matches(
                    session,
                    session_generation,
                    operation_nonce,
                    segment_index,
                    operation_task,
                ):
                    self._clear_segment_operation(session)
            raise VoiceIdentityServiceError(exc.code) from exc
        except EnrollmentAudioNormalizationError as exc:
            retired = await self._terminate_failed_segment_operation(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
                VoiceIdentityEffectiveReason.RUNTIME_DEGRADED,
            )
            if not retired:
                raise VoiceIdentityServiceError("stale_enrollment") from exc
            raise VoiceIdentityServiceError(
                "audio_processing_unavailable"
            ) from exc
        except EnrollmentSpeechValidatorUnavailableError as exc:
            retired = await self._terminate_failed_segment_operation(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
                VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE,
            )
            if not retired:
                raise VoiceIdentityServiceError("stale_enrollment") from exc
            raise VoiceIdentityServiceError("model_unavailable") from exc
        except asyncio.CancelledError:
            caller_cancelled = operation_task.cancelling() > 0
            await self._terminate_failed_segment_operation(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
                self._idle_reason(),
            )
            if caller_cancelled:
                raise
            raise VoiceIdentityServiceError("stale_enrollment")
        except Exception as exc:
            retired = await self._terminate_failed_segment_operation(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
                VoiceIdentityEffectiveReason.MODEL_UNAVAILABLE,
            )
            if not retired:
                raise VoiceIdentityServiceError("stale_enrollment") from exc
            raise VoiceIdentityServiceError("model_unavailable") from exc

        try:
            await self._operation_lock.acquire()
        except asyncio.CancelledError:
            computed.wipe()
            await self._terminate_failed_segment_operation(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
                self._idle_reason(),
            )
            raise
        if operation_task.cancelling() > 0:
            self._operation_lock.release()
            computed.wipe()
            await self._terminate_failed_segment_operation(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
                self._idle_reason(),
            )
            raise asyncio.CancelledError
        try:
            if not self._session_operation_matches(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
            ) or self._session_expired(session):
                computed.wipe()
                if self._enrollment is session and self._session_expired(session):
                    await self._retire_expired_session(session)
                raise VoiceIdentityServiceError("stale_enrollment")

            if segment_index in (1, 2):
                embedding = computed.reference_embedding
                computed.reference_embedding = None
                if embedding is None:
                    raise VoiceIdentityServiceError("model_unavailable")
                if segment_index == 1:
                    session.profile_id = profile_id
                session.reference_embeddings.append(embedding)
                session.next_segment_index += 1
                self._clear_segment_operation(session)
                return self.status()

            if segment_index == 3:
                embedding = computed.reference_embedding
                computed.reference_embedding = None
                if embedding is None:
                    raise VoiceIdentityServiceError("model_unavailable")
                session.reference_embeddings.append(embedding)
                try:
                    centroid = create_enrollment_reference_centroid(
                        session.reference_embeddings
                    )
                except EnrollmentAudioError as exc:
                    self._reset_session_references(session)
                    self._clear_segment_operation(session)
                    raise VoiceIdentityServiceError(exc.code) from exc
                finally:
                    for reference_embedding in session.reference_embeddings:
                        wipe_enrollment_embedding(reference_embedding)
                    session.reference_embeddings.clear()
                session.reference_centroid = centroid
                session.next_segment_index = 4
                session.phase = "verifying"
                self._clear_segment_operation(session, preserve_phase=True)
                return self.status()

            try:
                centroid = session.reference_centroid
                if centroid is None:
                    raise VoiceIdentityServiceError("stale_enrollment")
                if (
                    computed.holdout_1_5 is None
                    or computed.holdout_3_0 is None
                    or computed.holdout_5_0 is None
                ):
                    raise VoiceIdentityServiceError("model_unavailable")
                verification = verify_enrollment_holdout(
                    centroid,
                    computed.holdout_1_5,
                    computed.holdout_3_0,
                    computed.holdout_5_0,
                )
            finally:
                computed.wipe()

            if not verification.passed:
                session.holdout_failure_count += 1
                if session.holdout_failure_count >= 2:
                    self._reset_session_references(session)
                self._clear_segment_operation(session, preserve_phase=True)
                return replace(self.status(), verification=verification)

            session.phase = "committing"
            committed = await self._commit_enrollment_reference(
                session,
                session_generation=session_generation,
                operation_nonce=operation_nonce,
                profile_id=profile_id,
            )
            return replace(committed, verification=verification)
        finally:
            computed.wipe()
            self._operation_lock.release()

    async def _compute_enrollment_segment(
        self,
        session: _EnrollmentSession,
        segment_index: int,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        session_generation: int,
        operation_nonce: int,
        operation_task: asyncio.Task[object],
    ) -> _SegmentComputation:
        target_samples = ENROLLMENT_SAMPLE_RATE_HZ * (
            5 if segment_index == 4 else 3
        )
        normalizer = self._enrollment_audio_normalizer_factory(
            session.noise_reduction_enabled_snapshot
        )
        try:
            async with asyncio.timeout(self._model_timeout_seconds):
                normalized_pcm16 = await normalizer.normalize(
                    pcm16,
                    sample_rate_hz=sample_rate_hz,
                    target_samples=target_samples,
                )
        except TimeoutError as exc:
            raise EnrollmentAudioNormalizationError(
                "audio_processing_unavailable"
            ) from exc
        except EnrollmentAudioNormalizationError as exc:
            if exc.code in {"invalid_pcm", "speech_too_short"}:
                raise EnrollmentAudioError(exc.code) from exc
            raise

        self._require_compute_fence(
            session,
            session_generation,
            operation_nonce,
            segment_index,
            operation_task,
        )

        reference_bytes = ENROLLMENT_SAMPLE_RATE_HZ * 3 * 2
        verification_bytes = ENROLLMENT_SAMPLE_RATE_HZ * 5 * 2
        maximum_normalized_bytes = (
            ENROLLMENT_VERIFICATION_MAXIMUM_PCM_BYTES
            if segment_index == 4
            else ENROLLMENT_MAXIMUM_PCM_BYTES
        )
        if len(normalized_pcm16) > maximum_normalized_bytes:
            raise EnrollmentAudioError("audio_too_long")

        validation_task = asyncio.create_task(
            session.speech_validator.validate_pcm16(
                normalized_pcm16,
                sample_rate_hz=ENROLLMENT_SAMPLE_RATE_HZ,
            ),
            name="voice-identity-enrollment-speech-validation",
        )
        session.validation_task = validation_task
        try:
            await asyncio.wait_for(
                asyncio.shield(validation_task),
                timeout=self._model_timeout_seconds,
            )
        except TimeoutError as exc:
            raise EnrollmentSpeechValidatorUnavailableError(
                "speech validator timed out"
            ) from exc
        finally:
            if validation_task.done() and session.validation_task is validation_task:
                session.validation_task = None
        self._require_compute_fence(
            session,
            session_generation,
            operation_nonce,
            segment_index,
            operation_task,
        )

        computation = _SegmentComputation()
        try:
            if segment_index <= 3:
                computation.reference_embedding = await self._infer_embedding(
                    session,
                    normalized_pcm16[:reference_bytes],
                )
                self._require_compute_fence(
                    session,
                    session_generation,
                    operation_nonce,
                    segment_index,
                    operation_task,
                )
                return computation
            computation.holdout_1_5 = await self._infer_embedding(
                session,
                normalized_pcm16[: ENROLLMENT_SAMPLE_RATE_HZ * 3],
            )
            self._require_compute_fence(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
            )
            computation.holdout_3_0 = await self._infer_embedding(
                session,
                normalized_pcm16[:reference_bytes],
            )
            self._require_compute_fence(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
            )
            computation.holdout_5_0 = await self._infer_embedding(
                session,
                normalized_pcm16[:verification_bytes],
            )
            self._require_compute_fence(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
            )
            return computation
        except BaseException:
            computation.wipe()
            raise

    async def _infer_embedding(
        self,
        session: _EnrollmentSession,
        pcm16: bytes,
    ) -> np.ndarray:
        inference_task = asyncio.create_task(
            asyncio.to_thread(
                session.model.embedding_from_pcm16,
                pcm16,
                sample_rate_hz=CAMPPLUS_SAMPLE_RATE_HZ,
            ),
            name="voice-identity-model-inference",
        )
        session.inference_task = inference_task
        try:
            embedding = await asyncio.wait_for(
                asyncio.shield(inference_task),
                timeout=self._model_timeout_seconds,
            )
            if session.inference_task is not inference_task:
                raise _StaleEnrollmentOperation
            session.inference_task = None
            structurally_valid = (
                isinstance(embedding, np.ndarray)
                and embedding.ndim == 1
                and embedding.shape == (CAMPPLUS_EMBEDDING_DIM,)
                and np.issubdtype(embedding.dtype, np.floating)
                and embedding.flags.writeable
                and np.isfinite(embedding).all()
            )
            norm_squared = (
                float(np.dot(embedding, embedding))
                if structurally_valid
                else math.nan
            )
            if (
                not isinstance(embedding, np.ndarray)
                or not structurally_valid
                or not math.isfinite(norm_squared)
                or norm_squared <= 1e-12
            ):
                wipe_enrollment_embedding(embedding)
                raise EnrollmentSpeechValidatorUnavailableError(
                    "embedding inference returned an invalid result"
                )
            return embedding
        except TimeoutError as exc:
            try:
                session.model.cancel_inference()
            except Exception:
                pass
            raise EnrollmentSpeechValidatorUnavailableError(
                "embedding inference timed out"
            ) from exc
        except asyncio.CancelledError:
            raise
        except _StaleEnrollmentOperation:
            raise
        except Exception as exc:
            raise EnrollmentSpeechValidatorUnavailableError(
                "embedding inference failed"
            ) from exc

    def _require_compute_fence(
        self,
        session: _EnrollmentSession,
        session_generation: int,
        operation_nonce: int,
        segment_index: int,
        operation_task: asyncio.Task[object],
    ) -> None:
        if operation_task.cancelling() > 0:
            raise asyncio.CancelledError
        if (
            not self._session_operation_matches(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
            )
            or self._session_expired(session)
        ):
            raise _StaleEnrollmentOperation

    async def _commit_enrollment_reference(
        self,
        session: _EnrollmentSession,
        *,
        session_generation: int,
        operation_nonce: int,
        profile_id: str,
    ) -> VoiceIdentityServiceStatus:
        old_profile = self._profile
        old_audio_contract = self._profile_audio_contract
        old_requested = self._requested_enabled
        old_effective = self._effective_enabled
        old_activation_requested = (
            old_requested
            and old_profile is not None
            and self._runtime_mode != "off"
            and self._audio_contract_matches_runtime(old_audio_contract)
        )
        desired_requested = session.requested_enabled_snapshot
        new_audio_contract = desktop_audio_contract_snapshot(
            noise_reduction_enabled=session.noise_reduction_enabled_snapshot,
        )
        new_profile: SpeakerProfile | None = None
        staged: VoiceIdentityProfileWrite | None = None
        activation_changed = False
        prepared_activation: PreparedVoiceActivation | None = None
        activation_result = VoiceIdentityActivationResult.READY
        preference_changed = False
        succeeded = False
        commit_cancellation: asyncio.CancelledError | None = None
        old_activation_restore_result: VoiceIdentityActivationResult | None = None
        failure_reason = VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
        try:
            centroid = session.reference_centroid
            if centroid is None:
                raise VoiceIdentityServiceError("stale_enrollment")
            reference = SpeakerReference(
                SpeakerModelIdentity(
                    CAMPPLUS_MODEL_ID,
                    CAMPPLUS_MODEL_REVISION,
                    CAMPPLUS_EMBEDDING_DIM,
                ),
                centroid,
            )
            try:
                new_profile = SpeakerProfile(profile_id, reference)
            finally:
                reference.close()

            staging_cancellations: list[asyncio.CancelledError] = []
            staged = await _await_cancellation_safe(
                self._profile_store.astage(
                    new_profile,
                    audio_contract=new_audio_contract,
                ),
                name="voice-identity-profile-stage",
                cancellations=staging_cancellations,
            )
            self._require_commit_fence(
                session,
                session_generation,
                operation_nonce,
                profile_id,
            )
            if staging_cancellations:
                raise staging_cancellations[0]

            contract_matches_runtime = self._audio_contract_matches_runtime(
                new_audio_contract
            )
            if (
                desired_requested
                and self._runtime_mode != "off"
                and contract_matches_runtime
            ):
                activation_cancellations: list[asyncio.CancelledError] = []
                if self._activation_transaction is not None:
                    prepared_activation = await _await_cancellation_safe(
                        asyncio.wait_for(
                            self._activation_transaction.prepare_activation(new_profile, profile_id),
                            timeout=self._activation_timeout_seconds,
                        ),
                        name="voice-identity-enrollment-activation-prepare",
                        cancellations=activation_cancellations,
                    )
                    activation_result = prepared_activation.result
                else:
                    activation_result = await _await_cancellation_safe(
                        self._activate(new_profile, profile_id),
                        name="voice-identity-enrollment-activation",
                        cancellations=activation_cancellations,
                    )
                activation_changed = True
                self._require_commit_fence(
                    session,
                    session_generation,
                    operation_nonce,
                    profile_id,
                )
                if activation_cancellations:
                    raise activation_cancellations[0]
                if activation_result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
                    raise VoiceIdentityServiceError("runtime_degraded")
            elif desired_requested and self._runtime_mode != "off":
                activation_cancellations = []
                activation_result = await _await_cancellation_safe(
                    self._activate(None, str(uuid.uuid4())),
                    name="voice-identity-enrollment-contract-mismatch-detach",
                    cancellations=activation_cancellations,
                )
                activation_changed = True
                self._require_commit_fence(
                    session,
                    session_generation,
                    operation_nonce,
                    profile_id,
                )
                if activation_cancellations:
                    raise activation_cancellations[0]
                if activation_result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
                    raise VoiceIdentityServiceError("runtime_degraded")

            if desired_requested != old_requested:
                preference_cancellations: list[asyncio.CancelledError] = []
                await _await_cancellation_safe(
                    self._preference_store.asave(desired_requested),
                    name="voice-identity-enrollment-preference-save",
                    cancellations=preference_cancellations,
                )
                preference_changed = True
                self._require_commit_fence(
                    session,
                    session_generation,
                    operation_nonce,
                    profile_id,
                )
                if preference_cancellations:
                    raise preference_cancellations[0]

            self._require_commit_fence(
                session,
                session_generation,
                operation_nonce,
                profile_id,
            )
            commit_task = asyncio.create_task(
                staged.acommit(),
                name="voice-identity-profile-commit",
            )
            while not commit_task.done():
                try:
                    await asyncio.shield(commit_task)
                except asyncio.CancelledError as exc:
                    if commit_cancellation is None:
                        commit_cancellation = exc
            await commit_task

            self._profile = new_profile
            self._profile_audio_contract = new_audio_contract
            new_profile = None
            self._requested_enabled = desired_requested
            # Disk has settled. Publish Service's profile and activation authority
            # in one no-await segment; cancellation after durable commit must not
            # compensate back to the previous disk profile.
            if prepared_activation is not None:
                if self._closed or self._enrollment is not session:
                    # Shutdown or explicit enrollment retirement already owns
                    # runtime teardown. Keep the durable profile settlement but
                    # never grant a superseded operation fresh authority.
                    self._activation_transaction.revoke_prepared_activation(prepared_activation)
                    activation_result = VoiceIdentityActivationResult.RUNTIME_DEGRADED
                else:
                    activation_result = self._activation_transaction.commit_activation(
                        prepared_activation,
                    )
            if (
                desired_requested
                and self._runtime_mode != "off"
                and contract_matches_runtime
            ):
                self._apply_activation_result(activation_result)
            elif desired_requested and self._runtime_mode != "off":
                self._set_ineffective(
                    VoiceIdentityEffectiveReason.AUDIO_CONTRACT_MISMATCH
                )
            elif desired_requested:
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
            else:
                self._set_ineffective(VoiceIdentityEffectiveReason.DISABLED)
            self._last_completed = (session.enrollment_id, profile_id)
            succeeded = True
            if old_profile is not None:
                old_profile.close()
            if commit_cancellation is not None:
                raise commit_cancellation
        except VoiceIdentityServiceError:
            raise
        except VoiceIdentityPreferenceStoreError as exc:
            raise VoiceIdentityServiceError("runtime_degraded") from exc
        except SecureStorageUnavailableError as exc:
            failure_reason = VoiceIdentityEffectiveReason.SECURE_STORAGE_UNAVAILABLE
            raise VoiceIdentityServiceError("secure_storage_unavailable") from exc
        except VoiceIdentityProfileStoreError as exc:
            raise VoiceIdentityServiceError("runtime_degraded") from exc
        except Exception as exc:
            raise VoiceIdentityServiceError("runtime_degraded") from exc
        finally:
            if not succeeded:
                if prepared_activation is not None:
                    # Retire permissions before even the staged-file abort await.
                    self._activation_transaction.revoke_prepared_activation(prepared_activation)
                    abort_cancellations: list[asyncio.CancelledError] = []
                    old_activation_restore_result = await _await_cancellation_safe(
                        self._activation_transaction.abort_activation(prepared_activation),
                        name="voice-identity-enrollment-activation-abort",
                        cancellations=abort_cancellations,
                    )
                if staged is not None:
                    try:
                        file_abort_cancellations: list[asyncio.CancelledError] = []
                        await _await_cancellation_safe(
                            staged.aabort(),
                            name="voice-identity-enrollment-file-abort",
                            cancellations=file_abort_cancellations,
                        )
                    except Exception:
                        pass
                if prepared_activation is None and activation_changed:
                    rollback_profile = old_profile if old_activation_requested else None
                    rollback_generation = (
                        old_profile.generation
                        if rollback_profile is not None
                        else str(uuid.uuid4())
                    )
                    old_activation_restore_result = await self._activate(
                        rollback_profile,
                        rollback_generation,
                    )
                    if old_activation_restore_result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
                        failure_reason = VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
                if preference_changed:
                    try:
                        await self._preference_store.asave(old_requested)
                    except VoiceIdentityPreferenceStoreError:
                        failure_reason = VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
                if new_profile is not None:
                    new_profile.close()
                if (
                    old_activation_requested
                    and old_activation_restore_result is not None
                    and old_activation_restore_result is not VoiceIdentityActivationResult.RUNTIME_DEGRADED
                ):
                    self._apply_activation_result(old_activation_restore_result)
                elif old_effective:
                    self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
                else:
                    self._set_ineffective(failure_reason)
            if self._enrollment is session:
                self._enrollment = None
            self._invalidate_session(session)
            cleanup_ok = await self._cleanup_session(session)
            if not cleanup_ok and not self._effective_enabled:
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
        return self.status()

    def _session_expired(self, session: _EnrollmentSession) -> bool:
        return asyncio.get_running_loop().time() >= session.expires_at

    def _session_operation_matches(
        self,
        session: _EnrollmentSession,
        session_generation: int,
        operation_nonce: int,
        segment_index: int,
        operation_task: asyncio.Task[object],
    ) -> bool:
        return (
            self._enrollment is session
            and session.session_generation == session_generation
            and session.operation_nonce == operation_nonce
            and session.in_flight_segment_index == segment_index
            and session.operation_task is operation_task
        )

    def _clear_segment_operation(
        self,
        session: _EnrollmentSession,
        *,
        preserve_phase: bool = False,
    ) -> None:
        session.in_flight_segment_index = None
        session.operation_task = None
        if not preserve_phase:
            session.phase = (
                "verifying"
                if session.next_segment_index == 4
                else "collecting_reference"
            )

    def _reset_session_references(self, session: _EnrollmentSession) -> None:
        for embedding in session.reference_embeddings:
            wipe_enrollment_embedding(embedding)
        session.reference_embeddings.clear()
        wipe_enrollment_embedding(session.reference_centroid)
        session.reference_centroid = None
        session.holdout_failure_count = 0
        session.next_segment_index = 1
        session.phase = "collecting_reference"
        session.session_generation += 1
        session.operation_nonce += 1

    def _invalidate_session(self, session: _EnrollmentSession) -> None:
        session.session_generation += 1
        session.operation_nonce += 1
        session.in_flight_segment_index = None
        session.operation_task = None

    async def _retire_expired_session(self, session: _EnrollmentSession) -> None:
        if self._enrollment is session:
            self._enrollment = None
        self._invalidate_session(session)
        cancellations: list[asyncio.CancelledError] = []
        cleanup_ok = await _await_cancellation_safe(
            self._cleanup_session(session),
            name="voice-identity-expired-enrollment-cleanup",
            cancellations=cancellations,
        )
        if not self._effective_enabled:
            self._set_ineffective(
                self._idle_reason()
                if cleanup_ok
                else VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
            )
        if cancellations:
            raise cancellations[0]

    async def _terminate_failed_segment_operation(
        self,
        session: _EnrollmentSession,
        session_generation: int,
        operation_nonce: int,
        segment_index: int,
        operation_task: asyncio.Task[object],
        failure_reason: VoiceIdentityEffectiveReason,
    ) -> bool:
        async with self._operation_lock:
            if not self._session_operation_matches(
                session,
                session_generation,
                operation_nonce,
                segment_index,
                operation_task,
            ):
                return False
            self._enrollment = None
            self._invalidate_session(session)
            cancellations: list[asyncio.CancelledError] = []
            cleanup_ok = await _await_cancellation_safe(
                self._cleanup_session(session),
                name="voice-identity-failed-segment-cleanup",
                cancellations=cancellations,
            )
            if not self._effective_enabled:
                self._set_ineffective(
                    failure_reason
                    if cleanup_ok
                    else VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
                )
            if cancellations:
                raise cancellations[0]
            return True

    def _require_commit_fence(
        self,
        session: _EnrollmentSession,
        session_generation: int,
        operation_nonce: int,
        profile_id: str,
    ) -> None:
        operation_task = asyncio.current_task()
        if (
            operation_task is None
            or not self._session_operation_matches(
                session,
                session_generation,
                operation_nonce,
                4,
                operation_task,
            )
            or session.profile_id != profile_id
            or session.phase != "committing"
            or self._session_expired(session)
        ):
            raise VoiceIdentityServiceError("stale_enrollment")

    async def cancel_enrollment(self, enrollment_id: str | None = None) -> bool:
        async with self._operation_lock:
            self._require_initialized()
            session = self._enrollment
            if session is None:
                return False
            if enrollment_id is not None and session.enrollment_id != enrollment_id:
                return False
            self._enrollment = None
            self._invalidate_session(session)
            cancellations: list[asyncio.CancelledError] = []
            cleanup_ok = await _await_cancellation_safe(
                self._cleanup_session(session),
                name="voice-identity-cancel-enrollment-cleanup",
                cancellations=cancellations,
            )
            if not self._effective_enabled:
                self._set_ineffective(
                    self._idle_reason()
                    if cleanup_ok
                    else VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
                )
            if cancellations:
                raise cancellations[0]
            return True

    async def update_runtime_noise_reduction_enabled(
        self,
        enabled: bool,
    ) -> VoiceIdentityServiceStatus:
        """Reconcile the active profile after the runtime DSP domain changes."""

        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        async with self._operation_lock:
            self._require_initialized()
            self._runtime_noise_reduction_enabled = enabled
            profile = self._profile
            if not self._requested_enabled:
                self._set_ineffective(VoiceIdentityEffectiveReason.DISABLED)
                return self.status()
            if profile is None:
                self._set_ineffective(VoiceIdentityEffectiveReason.NO_PROFILE)
                return self.status()
            if not self._profile_is_compatible(profile):
                self._set_ineffective(VoiceIdentityEffectiveReason.PROFILE_INCOMPATIBLE)
                return self.status()
            if self._runtime_mode == "off":
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
                return self.status()

            cancellations: list[asyncio.CancelledError] = []
            if not self._audio_contract_matches_runtime(
                self._profile_audio_contract
            ):
                detached = await _await_cancellation_safe(
                    self._activate(None, str(uuid.uuid4())),
                    name="voice-identity-audio-contract-mismatch-detach",
                    cancellations=cancellations,
                )
                self._set_ineffective(
                    VoiceIdentityEffectiveReason.AUDIO_CONTRACT_MISMATCH
                    if detached
                    else VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
                )
            else:
                activated = await _await_cancellation_safe(
                    self._activate(profile, profile.generation),
                    name="voice-identity-audio-contract-restore",
                    cancellations=cancellations,
                )
                self._apply_activation_result(activated)
            status = self.status()
            if cancellations:
                raise cancellations[0]
            return status

    async def prepare_runtime_audio_contract_change(self) -> bool:
        """Detach speaker evidence before a live DSP configuration transition."""

        async with self._operation_lock:
            self._require_initialized()
            if (
                not self._requested_enabled
                or self._profile is None
                or self._runtime_mode == "off"
            ):
                return True
            cancellations: list[asyncio.CancelledError] = []
            detached = await _await_cancellation_safe(
                self._activate(None, str(uuid.uuid4())),
                name="voice-identity-audio-contract-transition-detach",
                cancellations=cancellations,
            )
            self._set_ineffective(
                VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
            )
            if cancellations:
                raise cancellations[0]
            return bool(detached)

    async def set_filter(self, enabled: bool) -> VoiceIdentityServiceStatus:
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        async with self._operation_lock:
            self._require_initialized()
            cancellations: list[asyncio.CancelledError] = []
            try:
                await _await_cancellation_safe(
                    self._preference_store.asave(enabled),
                    name="voice-identity-filter-preference-save",
                    cancellations=cancellations,
                )
            except VoiceIdentityPreferenceStoreError as exc:
                self._record_failure(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
                if cancellations:
                    raise cancellations[0]
                raise VoiceIdentityServiceError("runtime_degraded") from exc
            self._requested_enabled = enabled
            if not enabled:
                self._set_ineffective(VoiceIdentityEffectiveReason.DISABLED)
                detached = await _await_cancellation_safe(
                    self._activate(None, str(uuid.uuid4())),
                    name="voice-identity-filter-disable",
                    cancellations=cancellations,
                )
                self._set_ineffective(
                    VoiceIdentityEffectiveReason.DISABLED
                    if detached
                    else VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
                )
            elif self._profile is None:
                self._set_ineffective(VoiceIdentityEffectiveReason.NO_PROFILE)
            elif not self._profile_is_compatible(self._profile):
                self._set_ineffective(VoiceIdentityEffectiveReason.PROFILE_INCOMPATIBLE)
            elif not self._audio_contract_matches_runtime(
                self._profile_audio_contract
            ):
                self._set_ineffective(
                    VoiceIdentityEffectiveReason.AUDIO_CONTRACT_MISMATCH
                )
            elif self._runtime_mode == "off":
                self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
            else:
                activated = await _await_cancellation_safe(
                    self._activate(self._profile, self._profile.generation),
                    name="voice-identity-filter-enable",
                    cancellations=cancellations,
                )
                self._apply_activation_result(activated)
            status = self.status()
            if cancellations:
                raise cancellations[0]
            return status

    async def delete_profile(self) -> VoiceIdentityServiceStatus:
        async with self._operation_lock:
            self._require_initialized()
            cancellations: list[asyncio.CancelledError] = []
            session = self._enrollment
            self._enrollment = None
            cleanup_ok = True
            if session is not None:
                self._invalidate_session(session)
                cleanup_ok = await _await_cancellation_safe(
                    self._cleanup_session(session),
                    name="voice-identity-delete-enrollment-cleanup",
                    cancellations=cancellations,
                )
                if not cleanup_ok:
                    self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
            old_profile = self._profile
            old_audio_contract = self._profile_audio_contract
            try:
                await _await_cancellation_safe(
                    self._profile_store.adelete(),
                    name="voice-identity-profile-delete",
                    cancellations=cancellations,
                )
            except VoiceIdentityProfileStoreError as exc:
                if cancellations:
                    raise cancellations[0]
                raise VoiceIdentityServiceError("runtime_degraded") from exc
            try:
                await _await_cancellation_safe(
                    self._preference_store.asave(False),
                    name="voice-identity-delete-preference-save",
                    cancellations=cancellations,
                )
            except VoiceIdentityPreferenceStoreError as exc:
                rollback_failed = (
                    old_profile is not None and old_audio_contract is None
                )
                if old_profile is not None and old_audio_contract is not None:
                    try:
                        await _await_cancellation_safe(
                            self._profile_store.asave(
                                old_profile,
                                audio_contract=old_audio_contract,
                            ),
                            name="voice-identity-delete-profile-rollback",
                            cancellations=cancellations,
                        )
                    except VoiceIdentityProfileStoreError:
                        rollback_failed = True
                if rollback_failed:
                    await _await_cancellation_safe(
                        self._activate(None, str(uuid.uuid4())),
                        name="voice-identity-delete-failed-rollback-detach",
                        cancellations=cancellations,
                    )
                    self._profile = None
                    self._profile_audio_contract = None
                    if old_profile is not None:
                        old_profile.close()
                    self._set_ineffective(VoiceIdentityEffectiveReason.RUNTIME_DEGRADED)
                if cancellations:
                    raise cancellations[0]
                raise VoiceIdentityServiceError("runtime_degraded") from exc
            self._requested_enabled = False
            self._set_ineffective(VoiceIdentityEffectiveReason.DISABLED)
            detached = await _await_cancellation_safe(
                self._activate(None, str(uuid.uuid4())),
                name="voice-identity-delete-profile-detach",
                cancellations=cancellations,
            )
            self._profile = None
            self._profile_audio_contract = None
            self._set_ineffective(
                VoiceIdentityEffectiveReason.DISABLED
                if detached and cleanup_ok
                else VoiceIdentityEffectiveReason.RUNTIME_DEGRADED
            )
            if old_profile is not None:
                old_profile.close()
            status = self.status()
            if cancellations:
                raise cancellations[0]
            return status

    async def close(self) -> None:
        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            cancellations: list[asyncio.CancelledError] = []
            session = self._enrollment
            self._enrollment = None
            if session is not None:
                self._invalidate_session(session)
                await _await_cancellation_safe(
                    self._cleanup_session(session),
                    name="voice-identity-close-enrollment-cleanup",
                    cancellations=cancellations,
                )
            await _await_cancellation_safe(
                self._activate(None, str(uuid.uuid4())),
                name="voice-identity-close-profile-detach",
                cancellations=cancellations,
            )
            try:
                await _await_cancellation_safe(
                    self._suppression_controller.close(),
                    name="voice-identity-close-suppression-controller",
                    cancellations=cancellations,
                )
            except Exception:
                pass
            if self._profile is not None:
                self._profile.close()
                self._profile = None
            self._profile_audio_contract = None
            self._effective_enabled = False
            self._effective_reason = VoiceIdentityEffectiveReason.DISABLED
            if cancellations:
                raise cancellations[0]

    async def _expire_enrollment(
        self,
        enrollment_id: str,
        ttl_seconds: float,
    ) -> None:
        try:
            await asyncio.sleep(ttl_seconds)
            await self.cancel_enrollment(enrollment_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _cleanup_session(self, session: _EnrollmentSession) -> bool:
        current = asyncio.current_task()
        if session.expiry_task is not current:
            session.expiry_task.cancel()
        ok = True
        try:
            await session.lease.release()
        except Exception:
            ok = False
        validation_task = session.validation_task
        session.validation_task = None
        if validation_task is not None and not validation_task.done():
            validation_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(validation_task),
                    timeout=self._model_timeout_seconds,
                )
            except TimeoutError:
                ok = False
            except asyncio.CancelledError:
                if not validation_task.cancelled():
                    raise
            except Exception:
                pass
        if validation_task is not None and validation_task.done():
            try:
                validation_task.result()
            except BaseException:
                pass

        inference_task = session.inference_task
        session.inference_task = None
        if inference_task is not None:
            if not inference_task.done():
                try:
                    session.model.cancel_inference()
                except Exception:
                    ok = False
                try:
                    await asyncio.wait_for(
                        asyncio.shield(inference_task),
                        timeout=self._model_timeout_seconds,
                    )
                except TimeoutError:
                    self._wipe_session_embeddings(session)
                    self._retain_timed_out_session_cleanup(session, inference_task)
                    return False
                except asyncio.CancelledError:
                    if not inference_task.done():
                        self._wipe_session_embeddings(session)
                        self._retain_timed_out_session_cleanup(
                            session,
                            inference_task,
                        )
                    raise
                except Exception:
                    pass
            try:
                embedding = inference_task.result()
            except BaseException:
                pass
            else:
                wipe_enrollment_embedding(embedding)
        self._wipe_session_embeddings(session)

        close_task = asyncio.create_task(
            session.speech_validator.close(),
            name="voice-identity-speech-validator-close",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(close_task),
                timeout=self._model_timeout_seconds,
            )
        except TimeoutError:
            self._retain_timed_out_session_cleanup(session, close_task=close_task)
            return False
        except asyncio.CancelledError:
            if not close_task.done():
                self._retain_timed_out_session_cleanup(session, close_task=close_task)
            raise
        except Exception:
            ok = False
        await self._close_model(session.model)
        return ok

    def _wipe_session_embeddings(self, session: _EnrollmentSession) -> None:
        for embedding in session.reference_embeddings:
            wipe_enrollment_embedding(embedding)
        session.reference_embeddings.clear()
        wipe_enrollment_embedding(session.reference_centroid)
        session.reference_centroid = None

    async def _close_speech_validator(
        self,
        validator: EnrollmentSpeechValidator,
    ) -> None:
        try:
            await validator.close()
        except Exception:
            pass

    async def _close_model(self, model: EnrollmentEmbeddingModel) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(model.close),
                timeout=self._model_timeout_seconds,
            )
        except Exception:
            pass

    def _retain_timed_out_model_load(
        self,
        model: EnrollmentEmbeddingModel,
        load_task: asyncio.Task[bool],
    ) -> None:
        async def finish_and_close() -> None:
            try:
                await load_task
            except BaseException:
                pass
            await self._close_model(model)

        cleanup_task = asyncio.create_task(
            finish_and_close(),
            name="voice-identity-model-load-cleanup",
        )
        self._model_load_cleanup_task = cleanup_task

        def clear_finished(task: asyncio.Task[None]) -> None:
            if self._model_load_cleanup_task is task:
                self._model_load_cleanup_task = None

        cleanup_task.add_done_callback(clear_finished)

    def _retain_timed_out_validator_load(
        self,
        model: EnrollmentEmbeddingModel,
        validator: EnrollmentSpeechValidator,
        load_task: asyncio.Task[bool],
    ) -> None:
        async def finish_and_close() -> None:
            try:
                await load_task
            except BaseException:
                pass
            await self._close_speech_validator(validator)
            await self._close_model(model)

        cleanup_task = asyncio.create_task(
            finish_and_close(),
            name="voice-identity-speech-validator-load-cleanup",
        )
        self._speech_validator_load_cleanup_task = cleanup_task

        def clear_finished(task: asyncio.Task[None]) -> None:
            if self._speech_validator_load_cleanup_task is task:
                self._speech_validator_load_cleanup_task = None

        cleanup_task.add_done_callback(clear_finished)

    def _retain_timed_out_session_cleanup(
        self,
        session: _EnrollmentSession,
        inference_task: asyncio.Task[np.ndarray] | None = None,
        *,
        close_task: asyncio.Task[object] | None = None,
    ) -> None:
        async def finish_and_close() -> None:
            if inference_task is not None:
                try:
                    embedding = await inference_task
                except BaseException:
                    pass
                else:
                    wipe_enrollment_embedding(embedding)
            if close_task is None:
                await self._close_speech_validator(session.speech_validator)
            else:
                try:
                    await close_task
                except BaseException:
                    pass
            await self._close_model(session.model)

        cleanup_task = asyncio.create_task(
            finish_and_close(),
            name="voice-identity-enrollment-resource-cleanup",
        )
        self._model_inference_cleanup_task = cleanup_task

        def clear_finished(task: asyncio.Task[None]) -> None:
            if self._model_inference_cleanup_task is task:
                self._model_inference_cleanup_task = None

        cleanup_task.add_done_callback(clear_finished)

    async def _activate(
        self,
        profile: SpeakerProfile | None,
        generation: str,
    ) -> VoiceIdentityActivationResult:
        try:
            result = await asyncio.wait_for(
                self._activation_callback(profile, generation),
                timeout=self._activation_timeout_seconds,
            )
            if isinstance(result, VoiceIdentityActivationResult):
                return result
            return (
                VoiceIdentityActivationResult.READY
                if result
                else VoiceIdentityActivationResult.RUNTIME_DEGRADED
            )
        except Exception:
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED

    def _apply_activation_result(
        self,
        result: VoiceIdentityActivationResult,
    ) -> None:
        if result is VoiceIdentityActivationResult.READY:
            if self._runtime_mode == "shadow":
                self._set_ineffective(VoiceIdentityEffectiveReason.SHADOW_MODE)
                return
            self._set_ready()
            return
        self._set_ineffective(VoiceIdentityEffectiveReason(result.value))

    def _profile_is_compatible(self, profile: SpeakerProfile) -> bool:
        identity = profile.model_identity
        return identity == SpeakerModelIdentity(
            CAMPPLUS_MODEL_ID,
            CAMPPLUS_MODEL_REVISION,
            CAMPPLUS_EMBEDDING_DIM,
        )

    def _audio_contract_matches_runtime(
        self,
        audio_contract: VoiceIdentityAudioContractSnapshot | None,
    ) -> bool:
        return bool(
            audio_contract is not None
            and audio_contract.matches_runtime(
                noise_reduction_enabled=self._runtime_noise_reduction_enabled,
            )
        )

    def _set_ready(self) -> None:
        self._effective_enabled = True
        self._effective_reason = VoiceIdentityEffectiveReason.READY

    def _set_ineffective(self, reason: VoiceIdentityEffectiveReason) -> None:
        self._effective_enabled = False
        self._effective_reason = reason

    def _record_failure(self, reason: VoiceIdentityEffectiveReason) -> None:
        if not self._effective_enabled:
            self._set_ineffective(reason)

    def _idle_reason(self) -> VoiceIdentityEffectiveReason:
        if not self._requested_enabled:
            return VoiceIdentityEffectiveReason.DISABLED
        if self._profile is None:
            return VoiceIdentityEffectiveReason.NO_PROFILE
        return VoiceIdentityEffectiveReason.RUNTIME_DEGRADED

    def _require_open(self) -> None:
        if self._closed:
            raise VoiceIdentityServiceError("service_closed")

    def _require_initialized(self) -> None:
        self._require_open()
        if not self._initialized:
            raise VoiceIdentityServiceError("service_not_initialized")


def _require_identifier(name: str, value: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > 128:
        raise VoiceIdentityServiceError(f"invalid_{name}")
