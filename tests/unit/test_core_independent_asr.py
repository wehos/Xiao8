import ast
import asyncio
import hashlib
import inspect
import json
import logging
import textwrap
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

from main_logic.asr_client import VoiceIdentityActivationResult
from main_logic.asr_client.admission.contracts import (
    AdmissionDisposition,
    EvidenceState,
)
from main_logic.core import LLMSessionManager
from main_logic.core.asr_runtime import (
    AsrRuntimeMixin,
    _HotSwapAudioFrame,
    _ONSET_TRUST_WINDOW_S,
)
from main_logic.core.multimodal_turn import (
    _MAX_LIVE_TURN_RECORDS,
    _MAX_PRERECORD_VISUAL_VALIDATIONS,
)
from main_logic.asr_client.runtime import (
    AsrRuntimeCallbacks,
    AsrStartResult,
    AsrStartStatus,
    IndependentAsrRuntime,
)
from main_logic.asr_client._provider_events import (
    ProviderAudioRange,
    ProviderEndpointNotification,
    ProviderUtteranceStartedNotification,
    ProviderUtteranceKey,
)
from main_logic.asr_client.endpointing.detector_runtime import DetectorFeedResult, DetectorRuntime
from main_logic.voice_input import VoiceInputDispatchResult
from main_logic.voice_input.consumers import CoreChatTurnContext
from main_logic.asr_client.lifecycle import (
    AudioDisposition,
    FinalKey,
    VoiceLifecycleConfig,
    VoiceLifecycleEvent,
    VoiceLifecycleState,
    VoiceTurnToken,
    VoiceRouteMode,
)
from main_logic.asr_client.lifecycle import VoiceInputLifecycleController
from main_logic.asr_client.provider_policy import resolve_provider_policy
from main_logic.voice_turn.activity_evidence import RnnoiseEvidence
from main_logic.voice_turn.audio_input import ProcessedVoiceFrame
from main_logic.voice_turn.contracts import (
    AsrFailureEvent,
    AsrLifecycleNotification,
    AsrStatusEvent,
    AsrSubmitResult,
    AsrSubmitStatus,
    SpeechActivityEvent,
    VoiceIngressToken,
    VoicePartialEvent,
    VoiceTranscriptEvent,
)
from main_logic.voice_turn.contracts import EvaluationStatus, TurnDecision
from main_logic.asr_client.endpointing.coordinator import CoordinatorState
from main_logic.asr_client.endpointing.detector import (
    BoundDetectorTurn,
    CoreDetectorEventEnvelope,
    DetectorCandidateKey,
    DetectorIngressIdentity,
    ProviderCandidateFence,
    ProviderSpeakerBoundarySnapshot,
    DetectorRuntimeEvent,
    DetectorTurnEvent,
    DetectorSubmitResult,
    DetectorSubmitStatus,
)
import main_logic.core.asr_runtime as core_asr_runtime_module
import main_logic.core as core_module
import main_logic.asr_client.runtime as asr_runtime_module
import main_logic.voice_turn.audio_input as audio_input_module
from utils import preferences


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("value", "fallback", "expected"),
    [
        (
            "  ASR_QWEN_PROVIDER_ERROR: private provider text  ",
            "ASR_INDEPENDENT_FAILED",
            "ASR_QWEN_PROVIDER_ERROR",
        ),
        (
            "prefix ASR_QWEN_PROVIDER_ERROR: private provider text",
            "ASR_INDEPENDENT_FAILED",
            "ASR_INDEPENDENT_FAILED",
        ),
        (
            "asr_qwen_provider_error: private provider text",
            "ASR_INDEPENDENT_FAILED",
            "ASR_INDEPENDENT_FAILED",
        ),
        (
            f"ASR_{'A' * 61}: private provider text",
            "ASR_INDEPENDENT_FAILED",
            "ASR_INDEPENDENT_FAILED",
        ),
        (
            "ASR_QWEN-PROVIDER-ERROR: private provider text",
            "ASR_INDEPENDENT_FAILED",
            "ASR_INDEPENDENT_FAILED",
        ),
        (
            "provider failure",
            "invalid fallback",
            "ASR_INDEPENDENT_FAILED",
        ),
    ],
)
async def test_extract_asr_reason_code_accepts_only_safe_prefixes(
    value,
    fallback,
    expected,
) -> None:
    assert (
        asr_runtime_module._extract_asr_reason_code(value, fallback=fallback)
        == expected
    )


async def test_extract_asr_reason_code_survives_broken_stringification() -> None:
    class _BrokenString:
        def __str__(self) -> str:
            raise RuntimeError("must not escape")

    assert (
        asr_runtime_module._extract_asr_reason_code(
            _BrokenString(),
            fallback="ASR_ENDPOINTING_FAILED",
        )
        == "ASR_ENDPOINTING_FAILED"
    )
    assert (
        asr_runtime_module._extract_asr_reason_code(
            "provider failure",
            fallback=_BrokenString(),
        )
        == "ASR_INDEPENDENT_FAILED"
    )


class _Runtime(AsrRuntimeMixin):
    def __init__(self) -> None:
        self._init_asr_runtime_state()
        self._voice_lease_synchronized = True
        self._voice_lease_owner = "core"
        self._voice_input_suppressed = False
        self.lanlan_name = "Test"
        self.session = type("Omni", (), {})()
        self.session.create_response = AsyncMock()
        self.session.handle_interruption = AsyncMock()
        self.handle_new_message = AsyncMock()
        self.handle_input_transcript = AsyncMock(return_value=True)
        self.send_status = AsyncMock()

    def __getattr__(self, name: str):
        component = self.__dict__.get("_asr_runtime")
        if component is not None and hasattr(component, name):
            return getattr(component, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        component = self.__dict__.get("_asr_runtime")
        if name in {
            "_asr_route_mode",
            # Keep the operation generation on the instance so reads observe
            # bumps, matching production (writes routed to the component
            # would freeze it at its initial value for every read).
            "_asr_route_operation_generation",
            "_microphone_route_generation",
            "_independent_asr_provider",
            "_independent_asr_route_key",
            "_voice_input_audio_pipeline",
        }:
            object.__setattr__(self, name, value)
            return
        if component is not None and (
            name.startswith("_asr_")
            or name
            in {
                "_voice_input_resource_optimization_enabled",
            }
        ):
            setattr(component, name, value)
            if name == "_asr_lifecycle" and value is not None:
                component._asr_current_ingress_token = self._capture_ingress_token()
            return
        object.__setattr__(self, name, value)


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


async def test_external_voice_suppression_aborts_once_and_restores_pcm_gate() -> None:
    runtime = _Runtime()
    runtime._invalidate_voice_pcm_sync = MagicMock()
    runtime._abort_independent_asr = AsyncMock()
    assert runtime._voice_input_accepts_pcm() is True

    await runtime.set_voice_input_suppressed(
        "voice_identity_enrollment",
        suppressed=True,
    )
    await runtime.set_voice_input_suppressed(
        "voice_identity_enrollment",
        suppressed=True,
    )

    assert runtime._voice_input_accepts_pcm() is False
    runtime._abort_independent_asr.assert_awaited_once_with(
        "voice_identity_enrollment"
    )
    assert runtime._invalidate_voice_pcm_sync.call_count == 1

    await runtime.set_voice_input_suppressed(
        "voice_identity_enrollment",
        suppressed=False,
    )

    assert runtime._voice_input_accepts_pcm() is True
    assert runtime._invalidate_voice_pcm_sync.call_count == 2


async def test_external_voice_suppression_reasons_are_independent() -> None:
    runtime = _Runtime()
    runtime._invalidate_voice_pcm_sync = MagicMock()
    runtime._abort_independent_asr = AsyncMock()

    await runtime.set_voice_input_suppressed("enrollment", suppressed=True)
    await runtime.set_voice_input_suppressed("maintenance", suppressed=True)
    await runtime.set_voice_input_suppressed("enrollment", suppressed=False)

    assert runtime._voice_input_accepts_pcm() is False

    await runtime.set_voice_input_suppressed("maintenance", suppressed=False)

    assert runtime._voice_input_accepts_pcm() is True
    assert runtime._abort_independent_asr.await_count == 2


async def test_external_voice_suppression_resets_native_audio_turn() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "native"
    runtime._invalidate_voice_pcm_sync = MagicMock()
    runtime._abort_independent_asr = AsyncMock()
    runtime.session.clear_audio_buffer = AsyncMock()

    await runtime.set_voice_input_suppressed(
        "voice_identity_enrollment",
        suppressed=True,
    )

    runtime.session.clear_audio_buffer.assert_awaited_once_with()
    runtime._abort_independent_asr.assert_not_awaited()


async def test_native_route_rejects_verifier_before_installation() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "native"
    runtime._asr_runtime.set_speaker_verifier_factory = AsyncMock(return_value=True)
    factory = MagicMock()

    result = await runtime.set_speaker_verifier_factory(
        factory,
        activation_generation="profile-generation",
    )

    assert result is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    assert runtime._speaker_shadow_factory is None
    runtime._asr_runtime.set_speaker_verifier_factory.assert_not_awaited()
    factory.close.assert_called_once_with()


async def test_provider_route_without_exact_interval_rejects_verifier() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    runtime._asr_runtime.set_speaker_verifier_factory = AsyncMock(return_value=True)
    factory = MagicMock()
    assert runtime._asr_runtime._speaker_verifier_diagnostics()[
        "unsupported_asr_route_count"
    ] == 0

    result = await runtime.set_speaker_verifier_factory(
        factory,
        activation_generation="profile-generation",
    )

    assert result is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    assert runtime._speaker_shadow_factory is None
    runtime._asr_runtime.set_speaker_verifier_factory.assert_not_awaited()
    factory.close.assert_called_once_with()
    assert runtime._asr_runtime._speaker_verifier_diagnostics()[
        "unsupported_asr_route_count"
    ] == 1


async def test_smart_turn_route_retains_verifier_support() -> None:
    from main_logic.asr_client.speaker_verifier_contracts import (
        SpeakerVerifierInstallOutcome,
        SpeakerVerifierInstallReceipt,
    )
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    factory = MagicMock()

    async def install(spec, identity):
        assert spec.factory_builder(runtime._asr_runtime, identity) is factory
        receipt = SpeakerVerifierInstallReceipt(identity, SpeakerVerifierInstallOutcome.INSTALLED)
        runtime._asr_runtime._speaker_verifier_install_receipt = receipt
        return receipt

    runtime._asr_runtime.install_speaker_verifier = AsyncMock(side_effect=install)

    result = await runtime.set_speaker_verifier_factory(
        factory,
        activation_generation="profile-generation",
    )

    assert result is VoiceIdentityActivationResult.READY
    assert runtime._speaker_shadow_factory is None
    assert runtime._speaker_verifier_spec.profile_generation == "profile-generation"
    runtime._asr_runtime.install_speaker_verifier.assert_awaited_once()


async def test_core_forgets_future_verifier_when_physical_detach_degrades() -> None:
    from main_logic.asr_client.speaker_verifier_contracts import (
        SpeakerVerifierInstallOutcome,
        SpeakerVerifierInstallReceipt,
    )
    runtime = _Runtime()
    runtime._speaker_shadow_factory = MagicMock()
    runtime._asr_runtime.install_speaker_verifier = AsyncMock(return_value=
        SpeakerVerifierInstallReceipt(None, SpeakerVerifierInstallOutcome.FAILED))

    updated = await runtime.set_speaker_verifier_factory(
        None,
        activation_generation="revoked-profile",
    )

    assert updated is VoiceIdentityActivationResult.RUNTIME_DEGRADED
    assert runtime._speaker_shadow_factory is None
    assert not runtime._speaker_verifier_spec.requested_enabled


class _TestSmartTurnLease:
    def __init__(self, token) -> None:
        self.token = token
        self.released = False

    async def release(self) -> None:
        self.released = True


async def test_independent_asr_activity_probe_is_provider_neutral() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")

    assert runtime._independent_asr_user_turn_active() is False

    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._independent_asr_user_turn_active() is True

    runtime._asr_route_mode = "native"
    assert runtime._independent_asr_user_turn_active() is False


class _ReadyDetector:
    def __init__(self, feed_result: DetectorFeedResult | None = None) -> None:
        self.detector_epoch = 1
        self._token = None
        self._feed_result = feed_result or DetectorFeedResult((), True)
        self.bind_candidate = AsyncMock(return_value=object())
        self.reset = AsyncMock(side_effect=self._reset)
        self.close = AsyncMock()
        self.release_deferred_turn = AsyncMock()
        self.seal_provider_candidate = AsyncMock(
            return_value=ProviderCandidateFence(1, 0, 0)
        )
        self.complete_provider_candidate = AsyncMock(return_value=False)
        self.discard_provider_successor = AsyncMock(return_value=True)
        self.observe_provider_audio_ordered = AsyncMock()
        self.observe_provider_audio = MagicMock()
        self.wait_provider_audio_observed_through = AsyncMock(return_value=True)
        self.reconcile_provider_endpoint = AsyncMock()
        self.wait_provider_speaker_preseal = AsyncMock(return_value=True)
        self.retire_provider_speaker_boundary_unknown = AsyncMock()
        self.reset_provider_audio_timeline = AsyncMock(return_value=True)

    async def prepare_endpointing(self, token):
        self._token = token
        return _TestSmartTurnLease(token)

    def endpointing_ready(self, token) -> bool:
        return self._token == token

    async def feed(self, _pcm16: bytes, **_kwargs) -> DetectorFeedResult:
        return self._feed_result

    async def _reset(self) -> None:
        self._token = None


class _FailedSmartTurnDetector(_ReadyDetector):
    async def prepare_endpointing(self, token):
        self._token = None
        return None

    def endpointing_ready(self, token) -> bool:
        return False


class _QueuedSmartTurnDetector(_ReadyDetector):
    def __init__(self) -> None:
        super().__init__()
        self.queued_audio_ms = 0
        self.smart_turn_evaluation_ms = 0
        self.smart_turn_stale_result_count = 0
        self.smart_turn_coalesced_evaluation_count = 0
        self.force_speech_started = AsyncMock(return_value=True)
        self._sequence_no = 0

    async def submit_audio(
        self,
        _pcm16: bytes,
        *,
        ingress_token,
        **_kwargs,
    ) -> DetectorSubmitResult:
        self._sequence_no += 1
        return DetectorSubmitResult(
            status=DetectorSubmitStatus.ACCEPTED,
            throttle_available=False,
            endpointing_available=True,
            identity=DetectorIngressIdentity(
                ingress_token=ingress_token,
                detector_epoch=1,
                sequence_no=self._sequence_no,
            ),
            candidate=DetectorCandidateKey(1, 0),
        )


def _selection(provider_key: str, endpointing_mode: str = "manual"):
    return type(
        "Selection",
        (),
        {
            "provider_key": provider_key,
            "endpointing_mode": endpointing_mode,
            "soniox_region": None,
        },
    )()


async def _start_runtime_with_callback_candidates(
    monkeypatch,
    *,
    candidate_count: int = 2,
):
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    selection = _selection("qwen", "provider")
    selection_ref = selection
    detector = _ReadyDetector()
    callbacks: list[dict[str, object]] = []
    sessions = [
        SimpleNamespace(
            is_ready=True,
            connect=AsyncMock(),
            close=AsyncMock(),
        )
        for _ in range(candidate_count)
    ]

    def create_candidate(_core_type, *, selection: object, **kwargs):
        assert selection is selection_ref
        callbacks.append(kwargs)
        return sessions[len(callbacks) - 1]

    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(
            return_value={
                "independentAsrEnabled": True,
                "voiceInputResourceOptimizationEnabled": False,
            }
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=selection),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        create_candidate,
    )
    monkeypatch.setattr(
        runtime_module,
        "DetectorRuntime",
        MagicMock(return_value=detector),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_session is sessions[0]
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    assert runtime._asr_detector is detector
    assert runtime._asr_route_mode == "independent"
    return runtime, sessions, callbacks, detector


def _install_ready_lifecycle(
    runtime: _Runtime,
    provider: str = "qwen",
) -> None:
    if runtime._asr_session is None:
        runtime._asr_session = type("Asr", (), {"is_ready": True})()
    runtime._asr_provider = provider
    runtime._set_microphone_route("independent")
    endpointing_mode = "provider" if provider == "openai" else "manual"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy(provider, endpointing_mode),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = _ReadyDetector()
    runtime._asr_provider_exact_session = runtime._asr_session
    runtime._asr_runtime._asr_current_ingress_token = runtime._capture_ingress_token()


def _install_replacement_runtime_generation(
    runtime: _Runtime,
    provider: str = "qwen",
):
    component = runtime._asr_runtime
    component._asr_session_epoch += 1
    component._asr_audio_generation += 1
    component._asr_transcript_dispatcher.invalidate_all()
    component._asr_detector_dispatcher.invalidate_all()
    component._asr_audio_dispatcher.abort()
    session = SimpleNamespace(
        is_ready=True,
        close=AsyncMock(),
        signal_user_activity_end=AsyncMock(),
    )
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy(provider, "manual"),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = _QueuedSmartTurnDetector()
    detector.detector_epoch = 1
    component._asr_session = session
    component._asr_provider = provider
    component._asr_lifecycle = lifecycle
    component._asr_detector = detector
    runtime._set_microphone_route("independent")
    component._asr_current_ingress_token = runtime._capture_ingress_token()
    return session, lifecycle, detector


async def _install_active_smart_turn(runtime: _Runtime, provider: str = "qwen") -> None:
    _install_ready_lifecycle(runtime, provider)
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        runtime._asr_session_epoch,
    )


async def _start_and_seal_turn(
    runtime: _Runtime,
    provider: str = "qwen",
) -> None:
    if runtime._asr_lifecycle is None:
        _install_ready_lifecycle(runtime, provider)
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        runtime._asr_session_epoch,
    )
    await runtime._handle_independent_asr_endpoint(runtime._asr_session_epoch)


async def test_activity_probe_tracks_accepted_final_until_dispatch_completes() -> None:
    runtime = _Runtime()
    await _start_and_seal_turn(runtime, "qwen")
    sealed_token = runtime._asr_sealed_turn_token
    assert sealed_token is not None
    release_started = asyncio.Event()
    release_lease = asyncio.Event()
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    class _BlockingLease:
        token = sealed_token.turn

        async def release(self) -> None:
            release_started.set()
            await release_lease.wait()

    async def block_dispatch(*_args, **_kwargs) -> bool:
        dispatch_started.set()
        await release_dispatch.wait()
        return True

    runtime._asr_smart_turn_lease = _BlockingLease()
    runtime.handle_input_transcript.side_effect = block_dispatch
    final_task = asyncio.create_task(
        runtime._handle_independent_asr_final(
            "短语音",
            runtime._asr_session_epoch,
            "qwen",
        )
    )

    await release_started.wait()
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._independent_asr_user_turn_active() is True

    release_lease.set()
    await dispatch_started.wait()
    assert runtime._independent_asr_user_turn_active() is True

    release_dispatch.set()
    await final_task
    await runtime._wait_asr_transcript_dispatch_idle()
    assert runtime._independent_asr_user_turn_active() is False


async def test_independent_route_sends_pcm_to_asr_only() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    await _install_active_smart_turn(runtime)

    consumed = await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    assert consumed is True
    asr.stream_audio.assert_awaited_once_with(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )
    assert runtime._asr_audio_bytes == 320
    assert runtime._omni_mic_audio_bytes == 0


async def test_stale_submit_drops_only_current_frame() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    runtime._asr_runtime.submit = AsyncMock(
        return_value=AsrSubmitResult(AsrSubmitStatus.STALE)
    )

    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )

    assert runtime._asr_route_mode == "independent"


async def test_unavailable_submit_blocks_core_route() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "qwen"
    runtime._asr_runtime.submit = AsyncMock(
        return_value=AsrSubmitResult(AsrSubmitStatus.UNAVAILABLE)
    )
    clear_queue = MagicMock(wraps=runtime._clear_audio_stream_queue)
    clear_cache = MagicMock(wraps=runtime.hot_swap_audio_cache.clear)
    runtime._clear_audio_stream_queue = clear_queue
    runtime.hot_swap_audio_cache.clear = clear_cache

    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )

    assert runtime._asr_route_mode == "blocked"
    clear_queue.assert_called_once_with("independent_asr_unavailable")
    clear_cache.assert_called_once_with()


async def test_stale_unavailable_submit_cannot_block_replacement_route() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "provider-a"
    submit_started = asyncio.Event()
    release_submit = asyncio.Event()

    async def unavailable_after_replacement(*_args, **_kwargs):
        submit_started.set()
        await release_submit.wait()
        return AsrSubmitResult(AsrSubmitStatus.UNAVAILABLE)

    runtime._asr_runtime.submit = AsyncMock(side_effect=unavailable_after_replacement)
    clear_queue = MagicMock(wraps=runtime._clear_audio_stream_queue)
    clear_cache = MagicMock(wraps=runtime.hot_swap_audio_cache.clear)
    runtime._clear_audio_stream_queue = clear_queue
    runtime.hot_swap_audio_cache.clear = clear_cache
    routed = asyncio.create_task(
        runtime._route_microphone_audio(
            b"\x01\x00" * 160,
            sample_rate_hz=16_000,
        )
    )
    await asyncio.wait_for(submit_started.wait(), 1)

    new_core_session = SimpleNamespace(stream_audio=AsyncMock())
    new_asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime.session = new_core_session
    runtime._asr_runtime._asr_audio_generation += 1
    runtime._asr_session = new_asr_session
    runtime._independent_asr_provider = "provider-b"
    runtime._asr_runtime._asr_current_ingress_token = runtime._capture_ingress_token()
    release_submit.set()
    await asyncio.wait_for(routed, 1)

    assert runtime._asr_route_mode == "independent"
    assert runtime._independent_asr_provider == "provider-b"
    assert runtime.session is new_core_session
    assert runtime._asr_session is new_asr_session
    clear_queue.assert_not_called()
    clear_cache.assert_not_called()


async def test_async_detector_orders_pre_roll_before_smart_turn_seal() -> None:
    class Vad:
        def load(self) -> bool:
            return True

        def close(self) -> None:
            return None

    class Gate:
        def feed(self, _pcm16: bytes):
            return (
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.CANDIDATE_PAUSE,
            )

        def reset(self) -> None:
            return None

    class Coordinator:
        state = CoordinatorState.IDLE

        def push_audio(self, _pcm16: bytes) -> None:
            return None

        async def on_activity_event(self, event) -> None:
            self.state = (
                CoordinatorState.PAUSE_CANDIDATE
                if event is SpeechActivityEvent.CANDIDATE_PAUSE
                else CoordinatorState.SPEECH_ACTIVE
            )

        async def evaluate_buffered(self):
            return SimpleNamespace(
                status=EvaluationStatus.OK,
                decision=TurnDecision.COMPLETE,
            )

        async def prepare_predictor(self) -> bool:
            return True

        async def reset(self) -> None:
            self.state = CoordinatorState.IDLE

        async def close(self) -> None:
            self.state = CoordinatorState.CLOSED

        async def unload_predictor(self) -> None:
            return None

    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    asr.signal_user_activity_end = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_provider = "glm"
    runtime._asr_route_mode = "independent"
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("glm", "manual"),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_lifecycle = lifecycle
    detector: DetectorRuntime

    async def on_event(event) -> None:
        assert runtime._asr_detector_dispatcher.submit_nowait(
            CoreDetectorEventEnvelope(
                event=event,
                detector_ref=detector,
                lifecycle_ref=lifecycle,
                session_epoch=runtime._asr_session_epoch,
            )
        )

    detector = DetectorRuntime(
        vad=Vad(),
        gate=Gate(),
        provider_policy=resolve_provider_policy("glm", "manual"),
        coordinator=Coordinator(),
        on_event=on_event,
    )
    runtime._asr_detector = detector
    pcm16 = b"\x01\x00" * 160

    assert await runtime._route_microphone_audio(
        pcm16,
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    for _ in range(200):
        if asr.signal_user_activity_end.await_count:
            break
        await asyncio.sleep(0.001)
    await runtime._asr_detector_dispatcher.wait_idle()
    await runtime._asr_audio_dispatcher.wait_idle()

    asr.stream_audio.assert_awaited_once_with(pcm16, sample_rate_hz=16_000)
    asr.signal_user_activity_end.assert_awaited_once()
    assert runtime._omni_mic_audio_bytes == 0
    await detector.close()


# The former submit/endpoint/arm-failed variants asserted the deleted Runtime
# gate implementation. Their rejection/deadline behavior now lives in the
# focused admission reducer and candidate-rejection runtime suites; this case
# retains the still-relevant cross-component identity/order contract.
@pytest.mark.parametrize(
    "provider",
    ["dummy", "glm", "gemini"],
)
async def test_smart_turn_unavailable_blocks_segmented_provider_before_wire_audio(
    provider: str,
) -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    asr.close = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_provider = provider
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy(
            provider,
            "manual",
        ),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = _FailedSmartTurnDetector()

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        runtime._asr_session_epoch,
    )

    asr.stream_audio.assert_not_awaited()
    assert runtime._asr_route_mode == "blocked"
    assert runtime._omni_mic_audio_bytes == 0


@pytest.mark.parametrize("provider", ["qwen", "grok", "soniox"])
async def test_provider_endpoint_does_not_wait_for_smart_turn(
    provider: str,
) -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    asr.close = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_provider = provider
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy(provider, "provider"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = _FailedSmartTurnDetector(
        DetectorFeedResult((SpeechActivityEvent.SPEECH_STARTED,), True)
    )
    pcm16 = b"\x01\x00" * 160

    assert await runtime._route_microphone_audio(
        pcm16,
        sample_rate_hz=16_000,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    asr.stream_audio.assert_awaited_once_with(pcm16, sample_rate_hz=16_000)
    assert runtime._asr_route_mode == "independent"
    assert runtime._omni_mic_audio_bytes == 0


@pytest.mark.parametrize("provider", ["qwen", "soniox"])
async def test_manual_streaming_provider_waits_for_smart_turn(
    provider: str,
) -> None:
    runtime = _Runtime()
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy(provider, "manual"),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = _FailedSmartTurnDetector()
    turn_token = VoiceTurnToken(
        VoiceIngressToken(1, "socket", 1, 1, 1),
        turn_id=1,
    )

    assert runtime._asr_endpointing_ready(lifecycle, detector, turn_token) is False


async def test_enforced_lifecycle_suppresses_local_silence_upload() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = type(
        "Detector",
        (),
        {"feed": AsyncMock(return_value=DetectorFeedResult((), True))},
    )()

    consumed = await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    assert consumed is True
    asr.stream_audio.assert_not_awaited()
    assert runtime._asr_lifecycle.pre_roll_bytes == 320


async def test_local_speech_wake_uploads_pre_roll_to_independent_asr() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = _ReadyDetector()
    detector.feed = AsyncMock(
        side_effect=[
            DetectorFeedResult((), True),
            DetectorFeedResult((SpeechActivityEvent.SPEECH_STARTED,), True),
        ]
    )
    runtime._asr_detector = detector

    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )
    await runtime._route_microphone_audio(
        b"\x02\x00" * 160,
        sample_rate_hz=16_000,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    asr.stream_audio.assert_awaited_once_with(
        (b"\x01\x00" * 160) + (b"\x02\x00" * 160),
        sample_rate_hz=16_000,
    )
    runtime.session.handle_interruption.assert_awaited_once_with()


async def test_detector_failure_fails_open_to_same_independent_asr() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    await _install_active_smart_turn(runtime)
    runtime._asr_detector.feed = AsyncMock(return_value=DetectorFeedResult((), False))

    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    asr.stream_audio.assert_awaited_once_with(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )
    assert runtime._asr_route_mode == "independent"


async def test_game_takeover_clears_provider_audio_and_suspends_lifecycle() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.close = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = type("Detector", (), {"reset": AsyncMock()})()
    runtime._asr_detector = detector

    await runtime._suspend_independent_voice_input_for_game()

    asr.close.assert_awaited_once_with()
    detector.reset.assert_awaited_once_with()
    assert runtime._asr_lifecycle.snapshot.state.value == "suspended"

    await runtime._resume_independent_voice_input_after_game()
    assert runtime._asr_lifecycle.snapshot.state.value == "local_listen"


async def test_game_takeover_wins_even_if_provider_clear_fails() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.close = AsyncMock(side_effect=RuntimeError("provider abort failed"))
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)

    await runtime._suspend_independent_voice_input_for_game()

    assert runtime._asr_lifecycle.snapshot.state.value == "suspended"


async def test_game_consumer_reuses_smart_turn_asr_without_core(
    monkeypatch,
) -> None:
    runtime = _Runtime()
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

    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="game",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    assert runtime._voice_input_accepts_pcm() is True

    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "qwen")
    await runtime._handle_independent_asr_final("play", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    route_transcript.assert_awaited_once_with(
        "Test",
        "play",
        request_id=f"asr-{epoch}-1",
        game_type="game",
        session_id="session-a",
    )
    runtime.handle_new_message.assert_not_awaited()
    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()
    assert runtime._omni_mic_audio_bytes == 0

    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            2,
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )


async def test_game_consumer_ignores_empty_final(monkeypatch) -> None:
    runtime = _Runtime()
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
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="game",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    _install_ready_lifecycle(runtime, "qwen")
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    event = VoiceTranscriptEvent(
        turn_token=runtime._asr_runtime._capture_turn_token(lifecycle),
        provider="qwen",
        text="",
    )

    assert await runtime._prepare_voice_input_turn(event.turn_token) is True
    await runtime._dispatch_voice_input_final(event)
    await runtime._voice_input_registry.wait_idle()

    route_transcript.assert_not_awaited()
    runtime.handle_new_message.assert_not_awaited()
    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()


async def test_game_takeover_pre_abort_window_rejects_stale_core_turn(
    monkeypatch,
) -> None:
    runtime = _Runtime()
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
    runtime.session.abandon_external_voice_turn = MagicMock()
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    _install_ready_lifecycle(runtime, "openai")
    runtime._asr_session.close = AsyncMock()
    runtime._asr_session.signal_user_activity_end = AsyncMock()

    preview_clear_started = asyncio.Event()
    release_preview_clear = asyncio.Event()

    async def block_preview_clear(payload: dict[str, object]) -> None:
        if (
            payload.get("type") == "user_transcript_preview"
            and payload.get("text") == ""
        ):
            preview_clear_started.set()
            await release_preview_clear.wait()

    runtime.websocket = SimpleNamespace(
        send_json=AsyncMock(side_effect=block_preview_clear),
    )
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "openai")
    sealed = runtime._asr_runtime._asr_sealed_turn_token
    assert sealed is not None
    stale_ingress = sealed.turn.ingress

    await runtime._handle_independent_asr_final("", epoch, "openai")
    await asyncio.wait_for(preview_clear_started.wait(), 1)

    takeover = asyncio.create_task(
        runtime._handle_voice_input_control("game_takeover", 2)
    )
    endpoint: asyncio.Task[None] | None = None
    try:
        for _ in range(100):
            if runtime._voice_lease_owner == "game":
                break
            await asyncio.sleep(0)
        assert runtime._voice_lease_owner == "game"
        assert runtime._voice_lease_generation == 2
        assert takeover.done() is False

        await runtime._handle_independent_asr_activity(
            SpeechActivityEvent.SPEECH_STARTED,
            epoch,
        )
        prepared_token = runtime._asr_runtime._asr_partial_turn_token
        endpoint = asyncio.create_task(
            runtime._handle_independent_asr_endpoint(epoch)
        )
        for _ in range(100):
            if endpoint.done():
                break
            await asyncio.sleep(0)
        await runtime._handle_independent_asr_final(
            "stale core audio",
            epoch,
            "openai",
        )
        await runtime._asr_runtime.wait_transcript_idle()

        assert stale_ingress.lease_generation == 1
        assert prepared_token is None
        route_transcript.assert_not_awaited()
    finally:
        release_preview_clear.set()
        pending = [takeover]
        if endpoint is not None:
            pending.append(endpoint)
        results = await asyncio.wait_for(asyncio.gather(*pending), 1)
        assert results[0] is True
        await runtime._voice_input_registry.wait_idle()

    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{epoch}-1"
    )


async def test_rejected_voice_input_final_is_observable(monkeypatch) -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    event = VoiceTranscriptEvent(
        turn_token=runtime._asr_runtime._capture_turn_token(
            runtime._asr_lifecycle
        ),
        provider="qwen",
        text="hello",
    )
    runtime._voice_input_registry.dispatch_final = AsyncMock(
        return_value=VoiceInputDispatchResult.REJECTED
    )
    debug = MagicMock()
    monkeypatch.setattr(core_asr_runtime_module.logger, "debug", debug)

    await runtime._dispatch_voice_input_final(event)

    debug.assert_called_once()
    assert "voice input final rejected" in debug.call_args.args[0]


async def test_game_consumer_accepts_real_pcm_through_pipeline(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.is_active = True
    runtime.is_hot_swap_imminent = False
    monkeypatch.setattr(
        "main_logic.voice_input.consumers.game.is_game_route_active",
        lambda _name: True,
    )
    monkeypatch.setattr(
        "main_logic.voice_input.consumers.game.get_active_game_route_identity",
        lambda _name: ("game", "session-a"),
    )
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="game",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "qwen"
    route_audio = AsyncMock(return_value=True)
    runtime._route_microphone_audio = route_audio
    evidence = RnnoiseEvidence(True, 3, 0.9, 0.6, 0.2, 0.55)
    processed = ProcessedVoiceFrame(
        pcm16=b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        speech_probability=0.8,
        rnnoise_available=True,
        rnnoise_evidence=evidence,
    )
    runtime._voice_input_audio_pipeline.process = AsyncMock(return_value=processed)
    token = runtime._capture_ingress_token()

    await runtime._process_microphone_stream_data(
        {
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [1] * 160,
        },
        ingress_token=token,
        captured_at=1234.5,
    )

    runtime._voice_input_audio_pipeline.process.assert_awaited_once()
    route_audio.assert_awaited_once_with(
        processed.pcm16,
        sample_rate_hz=processed.sample_rate_hz,
        speech_probability=processed.speech_probability,
        rnnoise_available=processed.rnnoise_available,
        rnnoise_evidence=evidence,
        ingress_token=token,
        ingress_sequence=1,
        captured_at=1234.5,
    )


async def test_game_consumer_submit_preserves_owner_identity(monkeypatch) -> None:
    runtime = _Runtime()
    monkeypatch.setattr(
        "main_logic.voice_input.consumers.game.is_game_route_active",
        lambda _name: True,
    )
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="game",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "qwen"
    runtime._asr_runtime.submit = AsyncMock(
        return_value=AsrSubmitResult(AsrSubmitStatus.ACCEPTED)
    )
    token = runtime._capture_ingress_token()
    evidence = RnnoiseEvidence(True, 3, 0.9, 0.6, 0.2, 0.55)
    processed = ProcessedVoiceFrame(
        pcm16=b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        speech_probability=0.8,
        rnnoise_available=True,
        rnnoise_evidence=evidence,
    )

    await runtime._route_microphone_audio(
        processed.pcm16,
        sample_rate_hz=processed.sample_rate_hz,
        speech_probability=processed.speech_probability,
        rnnoise_available=processed.rnnoise_available,
        rnnoise_evidence=evidence,
        ingress_token=token,
    )

    runtime._asr_runtime.submit.assert_awaited_once_with(
        processed,
        ingress_token=token,
    )


async def test_hot_swap_cache_replay_preserves_rnnoise_evidence() -> None:
    runtime = _Runtime()
    runtime.is_active = True
    runtime.is_hot_swap_imminent = True
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "qwen"
    evidence = RnnoiseEvidence(True, 3, 0.9, 0.6, 0.2, 0.55)
    processed = ProcessedVoiceFrame(
        pcm16=b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        speech_probability=evidence.peak,
        rnnoise_available=True,
        rnnoise_evidence=evidence,
    )
    runtime._voice_input_audio_pipeline.process = AsyncMock(return_value=processed)
    route_audio = AsyncMock(return_value=True)
    runtime._route_microphone_audio = route_audio
    token = runtime._capture_ingress_token()

    await runtime._process_microphone_stream_data(
        {
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [1] * 160,
        },
        ingress_token=token,
        captured_at=2345.6,
    )

    assert len(runtime.hot_swap_audio_cache) == 1
    route_audio.assert_not_awaited()
    runtime.is_hot_swap_imminent = False
    await runtime._flush_hot_swap_audio_cache()

    route_audio.assert_awaited_once_with(
        processed.pcm16,
        sample_rate_hz=processed.sample_rate_hz,
        speech_probability=processed.speech_probability,
        rnnoise_available=processed.rnnoise_available,
        rnnoise_evidence=evidence,
        ingress_token=token,
        captured_at=2345.6,
    )


async def test_stale_audio_epoch_rejects_processed_rnnoise_evidence() -> None:
    runtime = _Runtime()
    runtime.is_active = True
    runtime.is_hot_swap_imminent = False
    runtime.is_flushing_hot_swap_cache = False
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "qwen"
    evidence = RnnoiseEvidence(True, 3, 0.9, 0.6, 0.2, 0.55)
    processed = ProcessedVoiceFrame(
        pcm16=b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        speech_probability=evidence.peak,
        rnnoise_available=True,
        rnnoise_evidence=evidence,
    )
    runtime._voice_input_audio_pipeline.process = AsyncMock(return_value=processed)
    route_audio = AsyncMock(return_value=True)
    runtime._route_microphone_audio = route_audio

    await runtime._process_microphone_stream_data(
        {
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [1] * 160,
        },
        ingress_token=runtime._capture_ingress_token(),
        audio_stream_epoch=runtime._audio_stream_epoch + 1,
    )

    runtime._voice_input_audio_pipeline.process.assert_awaited_once()
    route_audio.assert_not_awaited()


async def test_game_consumer_failure_never_falls_back_to_core(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    route_transcript = AsyncMock(side_effect=RuntimeError("consumer failed"))
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
    await runtime._handle_voice_input_control(
        "lease_sync",
        1,
        owner="game",
        hard_muted=False,
        focus_suppressed=False,
    )
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "qwen")

    await runtime._handle_independent_asr_final("play", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    route_transcript.assert_awaited_once_with(
        "Test",
        "play",
        request_id=f"asr-{epoch}-1",
        game_type="game",
        session_id="session-a",
    )
    runtime.handle_new_message.assert_not_awaited()
    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()
    assert runtime._omni_mic_audio_bytes == 0


async def test_game_final_cannot_cross_lease_back_to_core(monkeypatch) -> None:
    runtime = _Runtime()
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
    await runtime._handle_voice_input_control(
        "lease_sync",
        1,
        owner="game",
        hard_muted=False,
        focus_suppressed=False,
    )
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "qwen")

    await runtime._handle_voice_input_control(
        "lease_sync",
        2,
        owner="core",
        hard_muted=False,
        focus_suppressed=False,
    )
    await runtime._handle_independent_asr_final("stale", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    route_transcript.assert_not_awaited()
    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()
    assert runtime._omni_mic_audio_bytes == 0


async def test_hard_mute_overrides_game_consumer(monkeypatch) -> None:
    runtime = _Runtime()
    monkeypatch.setattr(
        "main_logic.voice_input.consumers.game.is_game_route_active",
        lambda _name: True,
    )

    await runtime._handle_voice_input_control(
        "lease_sync",
        1,
        owner="game",
        hard_muted=True,
        focus_suppressed=False,
    )

    assert runtime._voice_input_accepts_pcm() is False
    assert runtime._voice_input_suppression_reasons == {"hard_mute"}
    assert runtime._omni_mic_audio_bytes == 0


async def test_game_owner_without_consumer_remains_fail_closed() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")

    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="game",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )

    assert runtime._voice_input_accepts_pcm() is False
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.SUSPENDED
    assert runtime._omni_mic_audio_bytes == 0


async def test_fresh_blocked_route_consumes_pcm_without_omni() -> None:
    runtime = _Runtime()

    consumed = await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )

    assert consumed is True
    assert runtime._asr_audio_bytes == 0
    assert runtime._omni_mic_audio_bytes == 0


async def test_native_route_is_sufficient_to_authorize_omni_audio() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "native"
    runtime.session.stream_audio = AsyncMock()

    consumed = await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )

    assert consumed is True
    assert runtime._asr_route_mode == "native"
    runtime.session.stream_audio.assert_awaited_once()
    assert not hasattr(runtime._asr_runtime, "_asr_required")


async def test_speech_started_interrupts_and_prepares_turn_once() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )

    runtime.session.handle_interruption.assert_awaited_once_with()
    runtime.handle_new_message.assert_awaited_once_with()
    assert runtime._asr_turn_prepared is True


async def test_speech_started_prepares_external_voice_turn() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.prepare_external_voice_turn = AsyncMock()

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        runtime._asr_session_epoch,
    )

    runtime.session.prepare_external_voice_turn.assert_awaited_once_with(
        turn_id=f"asr-{runtime._asr_session_epoch}-1"
    )
    runtime.handle_new_message.assert_awaited_once_with()


async def test_gemini_prepare_reconnect_replaces_core_receive_task() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.prepare_external_voice_turn = AsyncMock(return_value=True)
    runtime._restart_message_handler_after_session_reconnect = AsyncMock(
        return_value=True
    )

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        runtime._asr_session_epoch,
    )

    runtime._restart_message_handler_after_session_reconnect.assert_awaited_once_with(
        runtime.session
    )
    runtime.handle_new_message.assert_awaited_once_with()


async def test_reconnect_listener_replacement_cancels_retired_receive_task() -> None:
    manager = LLMSessionManager.__new__(LLMSessionManager)
    manager.lock = asyncio.Lock()
    manager.is_active = True
    replacement_started = asyncio.Event()

    class Session:
        async def handle_messages(self):
            replacement_started.set()
            await asyncio.Event().wait()

    session = Session()
    manager.session = session
    retired_cancelled = asyncio.Event()

    async def retired_receive_loop():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            retired_cancelled.set()
            raise

    retired_task = asyncio.create_task(retired_receive_loop())
    manager.message_handler_task = retired_task
    await asyncio.sleep(0)

    assert await manager._restart_message_handler_after_session_reconnect(session)
    await asyncio.wait_for(replacement_started.wait(), 1)

    assert retired_cancelled.is_set()
    assert retired_task.done()
    assert manager.message_handler_task is not retired_task
    manager.message_handler_task.cancel()
    await asyncio.gather(manager.message_handler_task, return_exceptions=True)


async def test_game_takeover_during_core_prepare_drops_stale_message() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()

    async def block_prepare(*, turn_id: str) -> None:
        prepare_started.set()
        await release_prepare.wait()

    runtime.session.prepare_external_voice_turn = AsyncMock(side_effect=block_prepare)
    runtime.session.abandon_external_voice_turn = MagicMock()
    runtime._asr_runtime.suspend = AsyncMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    prepare_task = asyncio.create_task(runtime._prepare_core_voice_turn(token))
    await asyncio.wait_for(prepare_started.wait(), 1)

    await runtime._suspend_independent_voice_input_for_game()
    release_prepare.set()

    assert await asyncio.wait_for(prepare_task, 1) is False
    runtime.handle_new_message.assert_not_awaited()
    external_turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    assert runtime.session.abandon_external_voice_turn.call_args_list == [
        call(external_turn_id),
    ]


async def test_stale_core_prepare_restores_previous_preview_owner() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()

    async def block_prepare(*, turn_id: str) -> None:
        del turn_id
        prepare_started.set()
        await release_prepare.wait()

    runtime.session.prepare_external_voice_turn = AsyncMock(side_effect=block_prepare)
    runtime.session.abandon_external_voice_turn = MagicMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    previous_token = replace(token, turn_id=token.turn_id + 100)
    previous_turn_id = (
        f"asr-{previous_token.ingress.session_epoch}-{previous_token.turn_id}"
    )
    runtime._core_asr_preview_turn_id = previous_turn_id
    runtime._core_asr_preview_turn_token = previous_token
    runtime._core_asr_preview_text = "previous partial"

    prepare_task = asyncio.create_task(runtime._prepare_core_voice_turn(token))
    await asyncio.wait_for(prepare_started.wait(), 1)
    runtime._voice_input_transition_generation += 1
    release_prepare.set()

    assert await asyncio.wait_for(prepare_task, 1) is False
    assert runtime._core_asr_preview_turn_id == previous_turn_id
    assert runtime._core_asr_preview_turn_token == previous_token
    assert runtime._core_asr_preview_text == "previous partial"
    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    )


async def test_turn_endpoint_seals_immediately_before_provider_final() -> None:
    runtime = _Runtime()
    runtime._asr_session = type("Asr", (), {"is_ready": True})()
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )

    await runtime._handle_independent_asr_endpoint(epoch)

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.DRAINING


async def test_keyed_exact_boundary_reconciles_before_ordered_final() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    detector = component._asr_detector
    assert isinstance(detector, _ReadyDetector)
    epoch = component._asr_session_epoch
    await component._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    snapshot = ProviderSpeakerBoundarySnapshot(
        detector_epoch=1,
        candidate_generation=0,
        through_sequence_no=7,
        shadow_generation=3,
        merged_resume_count=1,
        successor_present=False,
        evidence_complete=True,
        _owner=object(),
    )
    detector.reconcile_provider_endpoint.return_value = snapshot
    boundary = ProviderEndpointNotification(
        phase="boundary",
        generation=4,
        buffer_epoch=2,
        utterance_id=1,
        boundary_quality="exact",
        audio_range=ProviderAudioRange(0, 52_800),
    )

    await component._handle_provider_endpoint_notification(boundary, epoch)
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    detector.reconcile_provider_endpoint.assert_awaited_once_with(
        boundary.audio_range
    )

    ordered = replace(boundary, phase="ordered")
    await component._handle_provider_endpoint_notification(ordered, epoch)

    key = boundary.key
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    assert component._asr_sealed_provider_key == key
    detector.seal_provider_candidate.assert_awaited_once()
    assert detector.seal_provider_candidate.await_args.kwargs[
        "speaker_snapshot"
    ] is snapshot
    assert detector.seal_provider_candidate.await_args.kwargs["deadline"] > 0

    await component._handle_provider_final(key, "joined", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert component._asr_sealed_provider_key is None
    assert key in component._asr_completed_provider_keys
    assert key not in component._asr_provider_boundary_snapshots
    runtime.handle_input_transcript.assert_awaited_once()
    runtime.session.create_response.assert_awaited_once_with("joined")


async def test_exact_boundary_waits_for_terminal_receipt_before_ready_exact() -> (
    None
):
    """READY_EXACT is published only after the terminal receipt settles."""

    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    detector = component._asr_detector
    assert isinstance(detector, _ReadyDetector)
    epoch = component._asr_session_epoch
    await component._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    snapshot = ProviderSpeakerBoundarySnapshot(
        detector_epoch=detector.detector_epoch,
        candidate_generation=0,
        through_sequence_no=7,
        shadow_generation=3,
        merged_resume_count=0,
        successor_present=False,
        evidence_complete=True,
        _owner=object(),
    )
    detector.reconcile_provider_endpoint.return_value = snapshot
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()

    async def settle_receipt(
        observed: ProviderSpeakerBoundarySnapshot,
        *,
        deadline: float,
    ) -> bool:
        assert observed is snapshot
        assert deadline > time.monotonic()
        settlement_entered.set()
        await settlement_release.wait()
        return True

    detector.wait_provider_speaker_preseal.side_effect = settle_receipt
    boundary = ProviderEndpointNotification(
        phase="boundary",
        generation=4,
        buffer_epoch=2,
        utterance_id=1,
        boundary_quality="exact",
        audio_range=ProviderAudioRange(0, 52_800),
    )
    boundary_task = asyncio.create_task(
        component._handle_provider_endpoint_notification(boundary, epoch)
    )
    await asyncio.wait_for(settlement_entered.wait(), 1)

    record = component._asr_provider_boundary_snapshots[boundary.key]
    assert record.state == "presealing"
    assert record.snapshot is None
    assert not record.preseal_settled.is_set()

    settlement_release.set()
    await asyncio.wait_for(boundary_task, 1)

    assert record.state == "ready_exact"
    assert record.snapshot is snapshot
    assert record.preseal_settled.is_set()


async def test_terminal_receipt_timeout_cannot_late_upgrade_unknown(
    monkeypatch,
) -> None:
    """A receipt released after its absolute deadline stays fail-open unknown."""

    monkeypatch.setattr(
        asr_runtime_module,
        "_PROVIDER_BOUNDARY_SETTLEMENT_TIMEOUT_SECONDS",
        0.01,
    )
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    detector = component._asr_detector
    assert isinstance(detector, _ReadyDetector)
    epoch = component._asr_session_epoch
    await component._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    snapshot = ProviderSpeakerBoundarySnapshot(
        detector_epoch=detector.detector_epoch,
        candidate_generation=0,
        through_sequence_no=7,
        shadow_generation=3,
        merged_resume_count=0,
        successor_present=False,
        evidence_complete=True,
        _owner=object(),
    )
    detector.reconcile_provider_endpoint.return_value = snapshot
    settlement_entered = asyncio.Event()
    settlement_release = asyncio.Event()

    async def settle_until_deadline(
        observed: ProviderSpeakerBoundarySnapshot,
        *,
        deadline: float,
    ) -> bool:
        assert observed is snapshot
        settlement_entered.set()
        await settlement_release.wait()
        return True

    detector.wait_provider_speaker_preseal.side_effect = settle_until_deadline
    boundary = ProviderEndpointNotification(
        phase="boundary",
        generation=4,
        buffer_epoch=2,
        utterance_id=1,
        boundary_quality="exact",
        audio_range=ProviderAudioRange(0, 52_800),
    )
    boundary_task = asyncio.create_task(
        component._handle_provider_endpoint_notification(boundary, epoch)
    )
    await asyncio.wait_for(settlement_entered.wait(), 1)
    record = component._asr_provider_boundary_snapshots[boundary.key]
    record.absolute_deadline = time.monotonic() - 1
    settlement_release.set()
    await asyncio.wait_for(boundary_task, 1)

    assert record.state == "ready_unknown"
    assert record.snapshot is snapshot
    assert record.preseal_settled.is_set()

    await component._handle_ordered_provider_endpoint(boundary, epoch)
    detector.retire_provider_speaker_boundary_unknown.assert_awaited_once_with(
        snapshot
    )


async def test_keyed_boundary_snapshot_overflow_fails_open_without_losing_final() -> (
    None
):
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    detector = component._asr_detector
    assert isinstance(detector, _ReadyDetector)
    epoch = component._asr_session_epoch
    await component._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    snapshots = [
        ProviderSpeakerBoundarySnapshot(
            detector_epoch=detector.detector_epoch,
            candidate_generation=index,
            through_sequence_no=index,
            shadow_generation=index,
            merged_resume_count=0,
            successor_present=False,
            evidence_complete=True,
            _owner=object(),
        )
        for index in range(8)
    ]
    detector.reconcile_provider_endpoint.side_effect = snapshots
    boundaries = [
        ProviderEndpointNotification(
            phase="boundary",
            generation=3,
            buffer_epoch=4,
            utterance_id=index,
            boundary_quality="exact",
            audio_range=ProviderAudioRange((index - 1) * 160, index * 160),
        )
        for index in range(1, 10)
    ]

    for boundary in boundaries[:8]:
        await component._handle_provider_endpoint_notification(boundary, epoch)

    assert list(component._asr_provider_boundary_snapshots) == [
        boundary.key for boundary in boundaries[:8]
    ]
    detector.retire_provider_speaker_boundary_unknown.assert_not_awaited()

    overflow = boundaries[8]
    await component._handle_provider_endpoint_notification(overflow, epoch)

    assert list(component._asr_provider_boundary_snapshots) == [
        boundary.key for boundary in boundaries[:8]
    ]
    assert list(component._asr_provider_boundary_overflow_keys) == [overflow.key]
    assert component._asr_provider_boundary_overflow_keys[overflow.key] is None
    assert detector.reconcile_provider_endpoint.await_count == 8
    # Overflow is tracked separately as unknown without evicting or widening
    # the bounded eight-entry exact-snapshot FIFO.
    assert detector.retire_provider_speaker_boundary_unknown.await_count == 1

    await component._handle_provider_endpoint_notification(
        replace(overflow, phase="ordered"),
        epoch,
    )

    fence = component._asr_provider_candidate_fence
    assert type(fence) is ProviderCandidateFence
    assert component._asr_sealed_provider_key == overflow.key
    detector.seal_provider_candidate.assert_awaited_once()
    seal_call = detector.seal_provider_candidate.await_args
    assert seal_call.args == (component._asr_sealed_turn_token.turn,)
    assert seal_call.kwargs["speaker_snapshot"] is None
    assert type(seal_call.kwargs["deadline"]) is float
    await component._handle_provider_final(
        overflow.key,
        "overflow kept",
        epoch,
        "openai",
    )
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.handle_input_transcript.assert_awaited_once_with(
        "overflow kept",
        is_voice_source=True,
        source="independent_asr",
        metadata={"provider": "openai"},
    )


async def test_keyed_unknown_retires_tail_without_ghost_and_key2_still_delivers() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    detector = component._asr_detector
    assert isinstance(detector, _ReadyDetector)
    epoch = component._asr_session_epoch
    await component._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await component._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await component._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    assert component._asr_overlap_completed_turns == 1

    key1 = ProviderUtteranceKey(5, 0, 1)
    ordered1 = ProviderEndpointNotification(
        phase="ordered",
        generation=key1.generation,
        buffer_epoch=key1.buffer_epoch,
        utterance_id=key1.utterance_id,
        boundary_quality="unknown",
        audio_range=None,
    )
    await component._handle_provider_endpoint_notification(ordered1, epoch)
    await component._handle_provider_final(key1, "first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    detector.retire_provider_speaker_boundary_unknown.assert_awaited()
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert component._asr_overlap_completed_turns == 0
    assert component._asr_overlap_onset_token is None

    key2 = ProviderUtteranceKey(5, 0, 2)
    ordered2 = ProviderEndpointNotification(
        phase="ordered",
        generation=key2.generation,
        buffer_epoch=key2.buffer_epoch,
        utterance_id=key2.utterance_id,
        boundary_quality="unknown",
        audio_range=None,
    )
    await component._handle_provider_endpoint_notification(ordered2, epoch)
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    await component._handle_provider_final(key2, "second", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0]
        for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second"]
    assert runtime.session.create_response.await_count == 2


async def test_rejected_prepare_fails_closed_instead_of_sealing_turn() -> None:
    runtime = _Runtime()
    runtime._asr_session = type(
        "Asr", (), {"is_ready": True, "close": AsyncMock()}
    )()
    _install_ready_lifecycle(runtime, "qwen")
    runtime.session.abandon_external_voice_turn = MagicMock()
    runtime.handle_new_message.side_effect = RuntimeError("prepare rejected")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    assert runtime._asr_turn_prepared is False
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE

    await runtime._handle_independent_asr_endpoint(epoch)

    # A persistently rejected preparation must never seal the turn: sealing
    # is the only gate through which a provider final reaches Core, and Core
    # does not re-run the interruption/external-turn pause at dispatch time.
    assert runtime._asr_route_mode == "blocked"
    assert "ASR_CORE_TURN_REJECTED" in str(runtime.send_status.await_args_list)

    await runtime._handle_independent_asr_final("hello", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()


async def test_endpoint_reprepares_turn_after_transient_prepare_rejection() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    runtime.session.abandon_external_voice_turn = MagicMock()
    runtime.handle_new_message.side_effect = [RuntimeError("transient"), None]
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    assert runtime._asr_turn_prepared is False
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE

    await runtime._handle_independent_asr_endpoint(epoch)

    # The retry-able recovery path: the endpoint re-runs preparation, so the
    # interruption/external-turn pause is established before the seal and the
    # provider final is injected normally.
    assert runtime._asr_turn_prepared is True
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    assert runtime.handle_new_message.await_count == 2

    await runtime._handle_independent_asr_final("hello", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.handle_input_transcript.assert_awaited_once()
    runtime.session.create_response.assert_awaited_once_with("hello")


async def test_empty_final_completes_turn_without_core_injection() -> None:
    runtime = _Runtime()
    runtime.session.prepare_external_voice_turn = AsyncMock()
    runtime.session.abandon_external_voice_turn = MagicMock()
    await _start_and_seal_turn(runtime)
    turn_id = runtime.session.prepare_external_voice_turn.await_args.kwargs["turn_id"]

    await runtime._handle_independent_asr_final(
        "",
        runtime._asr_session_epoch,
        "qwen",
    )
    # Teardown racing the queued empty final may win or lose, but both paths
    # terminate the same pinned route. Repeated invalidation and a duplicate
    # provider final must not produce a second cancellation/abandonment.
    runtime._invalidate_voice_pcm_sync("duplicate_after_empty_final")
    runtime._invalidate_voice_pcm_sync("duplicate_after_empty_final")
    await runtime._handle_independent_asr_final(
        "",
        runtime._asr_session_epoch,
        "qwen",
    )
    await runtime._wait_asr_transcript_dispatch_idle()

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_lifecycle.metrics.false_wake_count == 1
    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()
    runtime.session.abandon_external_voice_turn.assert_called_once_with(turn_id)
    assert runtime._omni_mic_audio_bytes == 0


async def test_blocked_consumer_callback_does_not_block_next_turn_lifecycle() -> (
    None
):
    runtime = _Runtime()
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()

    async def block_first_final(*_args, **_kwargs) -> bool:
        callback_started.set()
        await release_callback.wait()
        return True

    runtime.handle_input_transcript.side_effect = block_first_final
    await _start_and_seal_turn(runtime, "qwen")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_final("first", epoch, "qwen")
    await asyncio.wait_for(callback_started.wait(), 1)
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_turn_prepared is True
    release_callback.set()
    await runtime._wait_asr_transcript_dispatch_idle()
    runtime.session.create_response.assert_awaited_once_with("first")


async def test_prepare_failure_releases_keyed_external_turn_pause() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.prepare_external_voice_turn = AsyncMock(
        side_effect=RuntimeError("prepare failed")
    )
    runtime.session.abandon_external_voice_turn = MagicMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)

    assert await runtime._prepare_core_voice_turn(token) is False

    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    )


async def test_registry_prepare_rejection_releases_keyed_external_turn_pause() -> (
    None
):
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.abandon_external_voice_turn = MagicMock()
    runtime.handle_new_message = AsyncMock(side_effect=RuntimeError("history failed"))
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)

    assert await runtime._prepare_voice_input_turn(token) is False

    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    )


async def test_registry_cancelled_prepare_releases_keyed_external_turn_pause() -> (
    None
):
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.abandon_external_voice_turn = MagicMock()
    runtime.handle_new_message = AsyncMock(side_effect=asyncio.CancelledError)
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)

    with pytest.raises(asyncio.CancelledError):
        await runtime._prepare_voice_input_turn(token)
    await runtime._voice_input_registry.wait_idle()

    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    )


async def test_final_transcript_drops_new_conversation_swap_mid_restore() -> None:
    """A real conversation transition still invalidates the prepared final."""
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.abandon_external_voice_turn = MagicMock()
    timed_session = runtime.session

    replacement = type("Omni", (), {})()
    replacement.create_response = AsyncMock()
    replacement.submit_external_voice_turn = AsyncMock()
    replacement.abandon_external_voice_turn = MagicMock()

    async def _hot_swap_mid_restore(*_args, **_kwargs) -> None:
        runtime._voice_input_transition_generation += 1
        runtime.session = replacement

    runtime._restore_core_asr_preview_after_final = _hot_swap_mid_restore

    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(turn_token=token, provider="qwen", text="hello"),
    )

    # CodeRabbit: the race is manufactured inside a hook, so if that hook ever
    # stops being called -- preview restore skipped, moved, or bypassed on the
    # accepted branch -- runtime.session would never move, and all three
    # assertions below would pass while modelling an ordinary final with no hot
    # swap at all. Pin that the swap really happened first.
    assert runtime.session is replacement
    timed_session.create_response.assert_not_awaited()
    replacement.create_response.assert_not_awaited()
    replacement.submit_external_voice_turn.assert_not_awaited()


async def test_pre_dispatch_hot_swap_reprepares_turn_on_promoted_session() -> None:
    """A same-route hot swap transfers the final off the closed old arbiter."""
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.abandon_external_voice_turn = MagicMock()
    prepared_session = runtime.session
    prepared_session.create_response.side_effect = RuntimeError("closed arbiter")

    replacement = type("Omni", (), {})()
    replacement.create_response = AsyncMock()
    replacement.submit_external_voice_turn = AsyncMock()
    replacement.prepare_external_voice_turn = AsyncMock()
    replacement.abandon_external_voice_turn = MagicMock()

    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    runtime.session = replacement
    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(turn_token=token, provider="qwen", text="prepared"),
        session_ref=prepared_session,
    )

    prepared_session.create_response.assert_not_awaited()
    replacement.prepare_external_voice_turn.assert_awaited_once_with(
        turn_id=f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    )
    replacement.submit_external_voice_turn.assert_awaited_once_with(
        "prepared",
        turn_id=f"asr-{token.ingress.session_epoch}-{token.turn_id}",
    )


async def test_final_waits_for_shared_swap_barrier_then_uses_promoted_session() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    prepared_session = runtime.session
    prepared_session.abandon_external_voice_turn = MagicMock()

    replacement = type("Omni", (), {})()
    replacement.submit_external_voice_turn = AsyncMock()
    replacement.prepare_external_voice_turn = AsyncMock()
    replacement.abandon_external_voice_turn = MagicMock()

    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    await runtime._core_voice_session_swap_lock.acquire()
    dispatch = asyncio.create_task(
        runtime._dispatch_core_asr_transcript(
            VoiceTranscriptEvent(
                turn_token=token,
                provider="qwen",
                text="after swap",
            ),
            session_ref=prepared_session,
        )
    )
    try:
        await asyncio.sleep(0)
        assert dispatch.done() is False
        runtime.session = replacement
    finally:
        runtime._core_voice_session_swap_lock.release()
    await dispatch

    replacement.prepare_external_voice_turn.assert_awaited_once_with(
        turn_id=f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    )
    replacement.submit_external_voice_turn.assert_awaited_once_with(
        "after swap",
        turn_id=f"asr-{token.ingress.session_epoch}-{token.turn_id}",
    )


async def test_final_swap_barrier_timeout_drops_without_blocking_dispatcher() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.abandon_external_voice_turn = MagicMock()
    runtime._core_voice_session_swap_barrier_timeout_s = 0.01
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)

    await runtime._core_voice_session_swap_lock.acquire()
    try:
        await asyncio.wait_for(
            runtime._dispatch_core_asr_transcript(
                VoiceTranscriptEvent(
                    turn_token=token,
                    provider="qwen",
                    text="bounded",
                )
            ),
            timeout=0.5,
        )
    finally:
        runtime._core_voice_session_swap_lock.release()

    runtime.session.create_response.assert_not_awaited()


async def test_hot_swap_lifecycle_guards_close_and_promote_with_voice_barrier() -> None:
    source = inspect.getsource(
        core_module.LLMSessionManager._perform_final_swap_sequence
    )

    barrier = source.index("async with core_voice_session_lock")
    close = source.index("await old_main_session.close()")
    promote = source.index("self.session = new_session")
    assert barrier < close < promote


async def test_final_transcript_is_dropped_when_the_route_leaves_core_mid_restore() -> None:
    # Codex P2, the other half of the case above. Pinning session_ref protects
    # only the SESSION: a game or text takeover landing inside the preview
    # restore's websocket send moves _voice_lease_owner off "core" WITHOUT
    # necessarily replacing self.session, and the transcript was still injected
    # and an ordinary Core response started after the route had left Core.
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.abandon_external_voice_turn = MagicMock()
    timed_session = runtime.session

    takeover_ran = False

    async def _game_takeover_mid_restore(*_args, **_kwargs) -> None:
        nonlocal takeover_ran
        takeover_ran = True
        runtime._voice_lease_owner = "game"

    runtime._restore_core_asr_preview_after_final = _game_takeover_mid_restore

    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(turn_token=token, provider="qwen", text="hello"),
    )

    # Pin from OUTSIDE the hook that the race was actually manufactured; without
    # this the case degrades into an ordinary final that never left Core.
    assert takeover_ran
    assert runtime._voice_lease_owner == "game"
    # Session identity never moved, so only the route check can catch this.
    assert runtime.session is timed_session
    # No Core response is started for a route that has moved on.
    timed_session.create_response.assert_not_awaited()


async def test_transcript_dispatch_failure_releases_keyed_external_turn_pause() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.abandon_external_voice_turn = MagicMock()
    runtime.handle_input_transcript.side_effect = RuntimeError("history failed")
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    event = VoiceTranscriptEvent(
        turn_token=token,
        provider="qwen",
        text="hello",
    )

    with pytest.raises(RuntimeError, match="history failed"):
        await runtime._dispatch_core_asr_transcript(event)

    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    )


async def test_cancelled_preview_clear_still_releases_keyed_external_turn_pause() -> None:
    runtime = _Runtime()
    session = runtime.session
    session.abandon_external_voice_turn = MagicMock()
    runtime._send_core_asr_preview_clear = AsyncMock(
        side_effect=asyncio.CancelledError
    )
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=7)
    context = CoreChatTurnContext(
        token=token,
        external_turn_id="asr-cancelled-preview",
        session_ref=session,
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime._cancel_core_chat_voice_turn(context, "takeover")

    session.abandon_external_voice_turn.assert_called_once_with(
        "asr-cancelled-preview"
    )


@pytest.mark.parametrize("stale_guard", ["ingress", "owner"])
async def test_stale_final_guard_releases_keyed_external_turn_pause(
    stale_guard: str,
) -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime)
    runtime.session.abandon_external_voice_turn = MagicMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    event = VoiceTranscriptEvent(
        turn_token=token,
        provider="qwen",
        text="hello",
    )
    if stale_guard == "ingress":
        runtime._asr_audio_generation += 1
    else:
        runtime._voice_lease_owner = "game"

    await runtime._dispatch_core_asr_transcript(event)

    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    )


async def test_abort_bumps_generation_before_waiting_for_registry_cancel() -> None:
    runtime = _Runtime()
    order: list[str] = []
    runtime._asr_runtime.abort = AsyncMock(
        side_effect=lambda _reason: order.append("abort")
    )
    runtime._invalidate_voice_pcm_sync = MagicMock(
        side_effect=lambda _reason: order.append("invalidate")
    )
    runtime._voice_input_registry.wait_idle = AsyncMock(
        side_effect=lambda: order.append("wait_idle")
    )

    await runtime._abort_independent_asr("ingress_backpressure")

    assert order == ["abort", "invalidate", "wait_idle"]


async def test_suspend_advances_runtime_barrier_before_waiting_for_registry_cancel() -> (
    None
):
    runtime = _Runtime()
    order: list[str] = []
    runtime._invalidate_voice_pcm_sync = MagicMock(
        side_effect=lambda _reason: order.append("invalidate")
    )
    runtime._asr_runtime.suspend = AsyncMock(
        side_effect=lambda _reason: order.append("suspend")
    )
    runtime._voice_input_registry.wait_idle = AsyncMock(
        side_effect=lambda: order.append("wait_idle")
    )

    await runtime._suspend_independent_asr("game_takeover")

    assert order == ["suspend", "invalidate", "wait_idle"]


@pytest.mark.parametrize(
    ("previous_owner", "owner", "reason", "barrier_method"),
    [
        ("core", "game", "game_takeover", "suspend"),
        ("game", "core", "game_release", "abort"),
        ("core", "none", "connection_closed", "abort"),
    ],
)
async def test_voice_lease_advances_runtime_barrier_before_waiting_for_registry(
    previous_owner: str,
    owner: str,
    reason: str,
    barrier_method: str,
) -> None:
    runtime = _Runtime()
    runtime._voice_lease_owner = previous_owner
    order: list[str] = []
    runtime._invalidate_voice_pcm_sync = MagicMock(
        side_effect=lambda _reason: order.append("invalidate")
    )
    runtime._asr_runtime.suspend = AsyncMock(
        side_effect=lambda _reason: order.append("suspend")
    )
    runtime._asr_runtime.abort = AsyncMock(
        side_effect=lambda _reason: order.append("abort")
    )
    runtime._asr_runtime.resume = AsyncMock()
    runtime._voice_input_registry.wait_idle = AsyncMock(
        side_effect=lambda: order.append("wait_idle")
    )

    await runtime._apply_voice_lease_state(
        owner=owner,
        hard_muted=False,
        focus_suppressed=False,
        reason=reason,
        force_abort=True,
    )

    assert order == ["invalidate", barrier_method, "wait_idle"]


@pytest.mark.parametrize("operation", ["abort", "close"])
async def test_core_asr_teardown_force_releases_external_turn_pause(
    operation: str,
) -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    runtime.session.abandon_external_voice_turn = MagicMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    assert await runtime._prepare_voice_input_turn(token) is True

    if operation == "abort":
        runtime._asr_runtime.abort = AsyncMock()
        await runtime._abort_independent_asr("test_abort")
        runtime._asr_runtime.abort.assert_awaited_once_with("test_abort")
    else:
        runtime._asr_runtime.stop_session = AsyncMock()
        await runtime._close_independent_asr(next_route_mode="blocked")
        runtime._asr_runtime.stop_session.assert_awaited_once_with()

    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}",
    )


async def test_current_asr_failure_force_releases_external_turn_pause() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    runtime.session.abandon_external_voice_turn = MagicMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    assert await runtime._prepare_voice_input_turn(token) is True

    await runtime._handle_core_asr_failure(
        AsrFailureEvent(
            code="ASR_INDEPENDENT_FAILED",
            provider="qwen",
            session_epoch=runtime._asr_session_epoch,
        )
    )

    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}",
    )


async def test_registry_cancellation_abandons_the_prepared_session_after_swap() -> (
    None
):
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    original_session = runtime.session
    original_session.abandon_external_voice_turn = MagicMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    assert await runtime._prepare_voice_input_turn(token) is True

    replacement = type("Omni", (), {})()
    replacement.abandon_external_voice_turn = MagicMock()
    runtime.session = replacement
    assert runtime._voice_input_registry.invalidate_utterance(
        token,
        reason="session_hot_swap",
    )
    await runtime._voice_input_registry.wait_idle()

    original_session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{token.ingress.session_epoch}-{token.turn_id}",
    )
    replacement.abandon_external_voice_turn.assert_not_called()


async def test_runtime_close_preserves_manager_lifetime_registry_builtins() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    registry = runtime._voice_input_registry
    core_registration = runtime._core_chat_voice_input_registration
    game_registration = runtime._game_voice_input_registration
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    assert await runtime._prepare_voice_input_turn(token) is True
    runtime._asr_runtime.stop_session = AsyncMock()

    await runtime._close_independent_asr(next_route_mode="blocked")
    runtime._ensure_asr_runtime_state()
    runtime._ensure_asr_runtime_state()

    assert runtime._voice_input_registry is registry
    assert runtime._core_chat_voice_input_registration is core_registration
    assert runtime._game_voice_input_registration is game_registration
    assert core_registration.closed is False
    assert game_registration.closed is False
    assert len(registry._records) == 2


async def test_runtime_state_initializes_and_backfills_phase4a_fields() -> None:
    runtime = _Runtime()

    assert runtime._voice_input_resource_optimization_handshake_override is None
    assert runtime._voice_input_resource_optimization_session_value is None
    assert runtime._core_asr_preview_turn_token is None
    assert runtime._voice_input_external_suppressions == set()

    del runtime._voice_input_resource_optimization_handshake_override
    del runtime._voice_input_resource_optimization_session_value
    del runtime._core_asr_preview_turn_token
    del runtime._voice_input_external_suppressions
    runtime._ensure_asr_runtime_state()

    assert runtime._voice_input_resource_optimization_handshake_override is None
    assert runtime._voice_input_resource_optimization_session_value is None
    assert runtime._core_asr_preview_turn_token is None
    assert runtime._voice_input_external_suppressions == set()


async def test_provider_final_watchdog_blocks_only_independent_asr() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {"is_ready": True, "close": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_provider = "qwen"
    runtime._asr_route_mode = "independent"
    policy = replace(
        resolve_provider_policy("qwen", "manual"),
        provider_final_timeout_ms=10,
    )
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=policy,
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = _ReadyDetector()

    await _start_and_seal_turn(runtime)
    # 10ms 超时跟 Windows 的 15.6ms 定时器分辨率同量级，固定 sleep 到底睡多久
    # 完全看运气；守护任务跑完才是「已开火」的权威信号，直接等它。
    watchdog = runtime._asr_final_watchdog_task
    assert watchdog is not None
    await asyncio.wait_for(watchdog, 5)

    assert runtime._asr_route_mode == "blocked"
    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()
    assert runtime._omni_mic_audio_bytes == 0


async def test_provider_final_watchdog_honors_per_provider_policy_timeout() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {"is_ready": True, "close": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_provider = "glm"
    runtime._asr_route_mode = "independent"
    # Segmented providers resolve a longer final timeout than the streaming
    # default; scale both down so the watchdog must track the policy value.
    policy = replace(
        resolve_provider_policy("glm", "manual"),
        # 500ms 而不是 80ms：下面「还没到点」那半只能靠时间证明，被测窗口必须
        # 远大于 Windows 的 15.6ms 定时器分辨率，否则余量不到一个 tick。
        provider_final_timeout_ms=500,
    )
    assert resolve_provider_policy("glm", "manual").provider_final_timeout_ms == 40_000
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=policy,
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = _ReadyDetector()

    await _start_and_seal_turn(runtime, "glm")
    armed_at = time.monotonic()
    watchdog = runtime._asr_final_watchdog_task
    assert watchdog is not None

    # 单个 sleep 睡了多久在 Windows 上不可信（15.6ms 分辨率，既会提前弹出也会
    # 超发），所以只信真实时钟：轮询到确实过了 150ms —— 远超一个 tick，也远小于
    # 上面的 500ms 窗口。醒来后必须先复查挂钟再断言：这一觉可能被别的任务拖长而
    # 睡过了观察窗口（甚至睡过 500ms 守护窗口），那时守护任务改成 blocked 是合法的，
    # 先断言就会把「事后才发生」误报成「窗口内提前开火」。
    # A watchdog stuck on the shared default (10 ms in the scaled test above)
    # would have fired by now; the per-provider override keeps it armed.
    deadline = armed_at + 0.15
    while True:
        await asyncio.sleep(0.005)
        if time.monotonic() >= deadline:
            break
        assert runtime._asr_route_mode == "independent"

    # 正向那半不猜时间：守护任务自己跑完（内部 await 完错误处理才结束）即同步点。
    await asyncio.wait_for(watchdog, 5)
    elapsed = time.monotonic() - armed_at

    assert runtime._asr_route_mode == "blocked"
    # 上界也必须钉住，否则这条用例只主张「五秒内会开火」：把 per-provider 超时写成
    # 常量 2s、或者把 ms 当成 s 换算错的回归，在 150ms 观察窗口里同样还是
    # independent，然后在 5s 内跑完，照样通过。配置是 500ms，给 3 倍余量。
    assert elapsed < 1.5, (
        f"守护任务没有按 per-provider 的 500ms 超时开火，实际 {elapsed:.3f}s"
    )


async def test_optimization_disabled_streaming_uploads_without_smart_turn() -> None:
    runtime = _Runtime()
    runtime._voice_input_resource_optimization_enabled = False
    asr = type("Asr", (), {"is_ready": True, "stream_audio": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_provider = "qwen"
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
        resource_optimization_enabled=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = _ReadyDetector()

    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        rnnoise_available=False,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    asr.stream_audio.assert_awaited_once()
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_smart_turn_lease is None
    assert runtime._asr_detector._token is None
    assert runtime._omni_mic_audio_bytes == 0


async def test_segmented_fail_open_uses_continuous_wake_without_fake_speech() -> None:
    runtime = _Runtime()
    runtime._voice_input_resource_optimization_enabled = False
    asr = type("Asr", (), {"is_ready": True, "stream_audio": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_provider = "glm"
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("glm", "manual"),
        shadow_mode=False,
        resource_optimization_enabled=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = _QueuedSmartTurnDetector()
    runtime._asr_detector = detector

    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        rnnoise_available=False,
    )
    await runtime._asr_detector_dispatcher.wait_idle()
    await runtime._asr_audio_dispatcher.wait_idle()

    detector.force_speech_started.assert_not_awaited()
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    asr.stream_audio.assert_awaited_once()
    assert runtime._asr_route_mode == "independent"


async def test_native_connection_close_is_latched_and_not_retried() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("native")
    runtime.session_closed_by_server = False
    runtime.last_audio_send_error_time = 0.0
    runtime.audio_error_log_interval = 2.0
    runtime.session.stream_audio = AsyncMock(
        side_effect=AttributeError("connection already closed")
    )

    assert (
        await runtime._route_microphone_audio(b"\x01\x00", sample_rate_hz=16_000)
        is True
    )
    assert (
        await runtime._route_microphone_audio(b"\x01\x00", sample_rate_hz=16_000)
        is True
    )

    assert runtime.session_closed_by_server is True
    runtime.session.stream_audio.assert_awaited_once()


async def test_native_audio_failure_log_is_rate_limited(monkeypatch) -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("native")
    runtime.session_closed_by_server = False
    runtime.last_audio_send_error_time = 0.0
    runtime.audio_error_log_interval = 2.0
    runtime.session.stream_audio = AsyncMock(side_effect=RuntimeError("send failed"))
    log_error = MagicMock()
    monkeypatch.setattr(core_asr_runtime_module.logger, "error", log_error)

    await runtime._route_microphone_audio(b"\x01\x00", sample_rate_hz=16_000)
    await runtime._route_microphone_audio(b"\x01\x00", sample_rate_hz=16_000)

    assert runtime.session.stream_audio.await_count == 2
    log_error.assert_called_once()


@pytest.mark.parametrize("provider", ["qwen", "openai"])
async def test_optimization_disabled_provider_route_never_prepares_smart_turn(
    provider: str,
) -> None:
    runtime = _Runtime()
    runtime._voice_input_resource_optimization_enabled = False
    asr = type("Asr", (), {"is_ready": True, "stream_audio": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_provider = provider
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy(provider, "provider"),
        shadow_mode=False,
        resource_optimization_enabled=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = _ReadyDetector()
    detector.prepare_endpointing = AsyncMock()
    runtime._asr_detector = detector

    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
        rnnoise_available=False,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    asr.stream_audio.assert_awaited_once()
    detector.prepare_endpointing.assert_not_awaited()
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_smart_turn_lease is None
    assert runtime._omni_mic_audio_bytes == 0


async def test_draining_next_speech_waits_for_old_final_then_starts_new_turn() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_provider = "qwen"
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = _ReadyDetector()
    detector.feed = AsyncMock(return_value=DetectorFeedResult((), True))
    detector.reset = AsyncMock()
    detector.release_deferred_turn = AsyncMock()
    runtime._asr_detector = detector
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    old_turn = runtime._asr_lifecycle.identity.turn_id
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._route_microphone_audio(
        b"\x02\x00" * 160,
        sample_rate_hz=16_000,
    )

    asr.stream_audio.assert_not_awaited()
    assert runtime._asr_lifecycle.pending_turn_bytes == 320

    await runtime._handle_independent_asr_final("first", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_lifecycle.identity.turn_id == old_turn + 1
    asr.stream_audio.assert_awaited_once_with(
        b"\x02\x00" * 160,
        sample_rate_hz=16_000,
    )
    assert runtime.handle_new_message.await_count == 2
    detector.reset.assert_not_awaited()
    detector.release_deferred_turn.assert_awaited_once_with()

    runtime.handle_input_transcript.reset_mock()
    await runtime._handle_independent_asr_final("stale-old-turn", epoch, "qwen")
    runtime.handle_input_transcript.assert_not_awaited()


async def test_warm_idle_pending_speech_does_not_reenter_draining_guard() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_provider = "qwen"
    runtime._asr_route_mode = "independent"
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._route_microphone_audio(
        b"\x02\x00" * 160,
        sample_rate_hz=16_000,
    )
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    assert lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert lifecycle.has_pending_turn is True

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )

    assert lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert lifecycle.has_pending_turn is True


async def test_stale_pending_activation_discards_confirmed_candidate() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_provider = "qwen"
    runtime._asr_route_mode = "independent"
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._route_microphone_audio(
        b"\x02\x00" * 160,
        sample_rate_hz=16_000,
    )
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    assert lifecycle.has_pending_turn is True

    await runtime._activate_pending_independent_turn(epoch)

    assert lifecycle.pending_turn_bytes == 0
    assert lifecycle.has_pending_turn is False
    assert runtime._asr_pending_detector_candidate is None


async def test_final_without_observed_pending_preserves_racing_next_onset() -> None:
    runtime = _Runtime()
    await _start_and_seal_turn(runtime, "gemini")
    detector = runtime._asr_detector
    assert isinstance(detector, _ReadyDetector)

    await runtime._handle_independent_asr_final(
        "first",
        runtime._asr_session_epoch,
        "gemini",
    )

    # A next onset may be admitted after final acceptance but before cleanup.
    # Releasing the completed turn preserves that audio; a full reset loses it.
    detector.reset.assert_not_awaited()
    detector.release_deferred_turn.assert_awaited_once_with()


async def test_active_onset_before_delayed_provider_final_starts_next_turn() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    assert runtime._asr_turn_prepared is True

    # Provider VAD already ended turn 1, but its ordered endpoint callback is
    # delivered only right before the delayed final. The local detector sees
    # the next turn's onset while Core is still ACTIVE and prepared.
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )

    # Turn 1's ordered callbacks arrive: endpoint immediately before final.
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    # The remembered onset was replayed: turn 2 is ACTIVE and prepared.
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_turn_prepared is True

    # Turn 2's ordered callbacks now seal and deliver instead of no-oping.
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("second", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second"]
    assert runtime.handle_new_message.await_count == 2


async def test_stale_overlap_onset_is_not_replayed_after_final() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )

    # The audio generation moves on before the delayed final, so the recorded
    # onset belongs to a stale ingress and must not wake a replacement turn.
    component = runtime._asr_runtime
    component._asr_audio_generation += 1
    component._asr_current_ingress_token = runtime._capture_ingress_token()

    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_turn_prepared is False
    # The prepared Registry route retains the original full VoiceTurnToken.
    # Rotating audio_generation makes the later final a different route, so
    # strict routing drops it together with the stale overlap onset.
    runtime.handle_input_transcript.assert_not_awaited()
    assert runtime.handle_new_message.await_count == 1


async def test_candidate_pause_defers_overlap_onset_without_ghost_wake() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    # Local VAD then observes a pause: the provider final that follows may be
    # the current utterance ending, so replaying the onset at that final would
    # wake a ghost turn. The onset converts into a completed-overlap credit
    # that only a later provider endpoint in WARM_IDLE can redeem.
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    assert runtime._asr_overlap_onset_token is None
    assert runtime._asr_overlap_completed_turns == 1

    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("hello", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    # No second endpoint arrived, so the credit must not wake anything.
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_turn_prepared is False
    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["hello"]
    assert runtime.handle_new_message.await_count == 1


async def test_completed_overlap_before_delayed_final_delivers_both_finals() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    assert runtime._asr_turn_prepared is True

    # Turn 2 both starts and reaches local silence while turn 1 is still
    # ACTIVE and prepared: its provider endpoint and final are queued in the
    # ordered FIFO behind turn 1's delayed final.
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )

    # Turn 1's ordered callbacks arrive: endpoint immediately before final.
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    # The completed overlap is not replayed yet: only turn 2's own provider
    # endpoint proves a queued turn exists.
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_turn_prepared is False

    # Turn 2's queued endpoint redeems the credit: the turn activates,
    # prepares, and seals so the final right behind it can deliver.
    await runtime._handle_independent_asr_endpoint(epoch)
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    await runtime._handle_independent_asr_final("second", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second"]
    assert runtime.handle_new_message.await_count == 2
    assert runtime._asr_overlap_completed_turns == 0


async def test_two_completed_overlaps_replay_in_order_after_delayed_final() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    # Turns 2 and 3 each start and reach local silence while turn 1 is still
    # ACTIVE: one completed-overlap credit accumulates per onset+pause cycle.
    for _ in range(2):
        await runtime._handle_independent_asr_activity(
            SpeechActivityEvent.SPEECH_RESUMED,
            epoch,
        )
        await runtime._handle_independent_asr_activity(
            SpeechActivityEvent.CANDIDATE_PAUSE,
            epoch,
        )
    assert runtime._asr_overlap_completed_turns == 2

    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    for text in ("second", "third"):
        await runtime._handle_independent_asr_endpoint(epoch)
        await runtime._handle_independent_asr_final(text, epoch, "openai")
        await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second", "third"]
    assert runtime.handle_new_message.await_count == 3
    assert runtime._asr_overlap_completed_turns == 0


async def test_hard_mute_clears_completed_overlap_credit() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.close = AsyncMock()
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    _install_ready_lifecycle(runtime, "openai")
    runtime._clear_audio_stream_queue = MagicMock()
    runtime.hot_swap_audio_cache = []
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    assert runtime._asr_overlap_completed_turns == 1

    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            12,
            owner="core",
            hard_muted=True,
            focus_suppressed=False,
        )
        is True
    )
    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )

    # Hard mute tears the turn state down: neither the onset nor the credit
    # may survive to wake a replacement turn.
    assert runtime._asr_overlap_onset_token is None
    assert runtime._asr_overlap_completed_token is None
    assert runtime._asr_overlap_completed_turns == 0

    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("ghost", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    # Turn 1's preparation before the mute awaited handle_new_message once;
    # the muted ghost final must not deliver a transcript or a second turn.
    assert runtime.handle_input_transcript.await_count == 0
    assert runtime.handle_new_message.await_count == 1


async def test_stale_completed_overlap_is_dropped_at_next_endpoint() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    assert runtime._asr_overlap_completed_turns == 1

    # The audio generation moves on before the delayed final, so the credit
    # belongs to a stale ingress and must not wake a replacement turn.
    component = runtime._asr_runtime
    component._asr_audio_generation += 1
    component._asr_current_ingress_token = runtime._capture_ingress_token()

    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    await runtime._handle_independent_asr_endpoint(epoch)
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_turn_prepared is False
    assert runtime._asr_overlap_completed_turns == 0
    await runtime._handle_independent_asr_final("ghost", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    # Both the overlap credit and the final belong to the superseded full
    # VoiceTurnToken once audio_generation rotates.
    runtime.handle_input_transcript.assert_not_awaited()
    assert runtime.handle_new_message.await_count == 1


async def test_smart_turn_active_resumed_is_not_recorded_for_replay() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    # SmartTurn authority orders activity and endpoint through one detector
    # queue, so a mid-turn resume is same-turn speech and must stay a no-op.
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )

    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("hello", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_turn_prepared is False
    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["hello"]
    assert runtime.handle_new_message.await_count == 1


async def test_draining_pending_turn_overflow_discards_candidate_and_reports_backpressure() -> (
    None
):
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_provider = "qwen"
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = _ReadyDetector()
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )

    await runtime._route_microphone_audio(
        b"\x01\x00" * (16_000 * 9),
        sample_rate_hz=16_000,
    )

    asr.stream_audio.assert_not_awaited()
    assert runtime._asr_session is asr
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    assert runtime._asr_sealed_turn_token is not None
    assert runtime._asr_lifecycle.pending_turn_bytes == 0
    assert runtime._asr_lifecycle.has_pending_turn is False
    runtime._asr_detector.reset.assert_not_awaited()
    runtime._asr_detector.discard_provider_successor.assert_awaited_once_with(
        runtime._asr_provider_candidate_fence
    )
    assert any(
        "ASR_INGRESS_BACKPRESSURE" in call.args[0]
        for call in runtime.send_status.await_args_list
    )
    assert runtime._omni_mic_audio_bytes == 0


async def test_active_ingress_backpressure_releases_keyed_core_turn_without_blocking(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, detector = (
        await _start_runtime_with_callback_candidates(
            monkeypatch,
            candidate_count=1,
        )
    )
    current_pause_id: str | None = None

    async def prepare_external_voice_turn(*, turn_id: str) -> None:
        nonlocal current_pause_id
        current_pause_id = turn_id

    def abandon_external_voice_turn(turn_id: str | None = None) -> None:
        nonlocal current_pause_id
        if turn_id is not None and turn_id != current_pause_id:
            return
        current_pause_id = None

    runtime.session.prepare_external_voice_turn = AsyncMock(
        side_effect=prepare_external_voice_turn
    )
    runtime.session.abandon_external_voice_turn = MagicMock(
        side_effect=abandon_external_voice_turn
    )
    core_session = runtime.session
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    assert lifecycle is not None
    current_ingress = runtime._capture_ingress_token()
    component._asr_current_ingress_token = current_ingress
    on_activity = callbacks[0]["on_speech_activity"]
    assert callable(on_activity)

    await on_activity(SpeechActivityEvent.SPEECH_STARTED)

    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    prepared_turn_id = lifecycle.snapshot.turn_id
    runtime.session.prepare_external_voice_turn.assert_awaited_once()
    external_turn_id = (
        runtime.session.prepare_external_voice_turn.await_args.kwargs["turn_id"]
    )
    assert current_pause_id == external_turn_id
    backpressure_status_started = asyncio.Event()
    release_backpressure_status = asyncio.Event()

    async def block_backpressure_status(payload: str) -> None:
        if json.loads(payload).get("code") == "ASR_INGRESS_BACKPRESSURE":
            backpressure_status_started.set()
            await release_backpressure_status.wait()

    runtime.send_status.side_effect = block_backpressure_status
    backpressure_task = asyncio.create_task(
        component._handle_audio_ingress_backpressure(current_ingress)
    )
    await asyncio.wait_for(backpressure_status_started.wait(), 1)

    next_turn = VoiceTurnToken(
        ingress=runtime._capture_ingress_token(),
        turn_id=lifecycle.snapshot.turn_id,
    )
    assert next_turn.turn_id != prepared_turn_id
    assert await runtime._prepare_core_voice_turn(next_turn) is True
    assert runtime.session.prepare_external_voice_turn.await_count == 2
    next_external_turn_id = (
        runtime.session.prepare_external_voice_turn.await_args.kwargs["turn_id"]
    )
    assert next_external_turn_id != external_turn_id
    assert current_pause_id == next_external_turn_id

    release_backpressure_status.set()
    await asyncio.wait_for(backpressure_task, 1)

    assert runtime.session is core_session
    assert runtime._asr_route_mode == "independent"
    assert component._asr_lifecycle is lifecycle
    assert lifecycle.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    assert component._asr_session is None
    assert component._asr_detector is detector
    assert component._asr_current_ingress_token is None
    sessions[0].close.assert_awaited_once_with()
    detector.reset.assert_awaited_once_with()
    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        external_turn_id
    )
    assert current_pause_id == next_external_turn_id
    assert all(
        "ASR_INDEPENDENT_FAILED" not in call.args[0]
        for call in runtime.send_status.await_args_list
    )


async def test_transport_only_close_enters_deep_sleep_without_closing_detector() -> (
    None
):
    runtime = _Runtime()
    asr = type("Asr", (), {"close": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    detector = type("Detector", (), {"close": AsyncMock()})()
    runtime._asr_detector = detector

    await runtime._close_transport_only()

    assert runtime._asr_session is None
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.DEEP_SLEEP
    assert runtime._asr_detector is detector
    assert runtime._asr_route_mode == "independent"
    asr.close.assert_awaited_once_with()
    detector.close.assert_not_awaited()


async def test_initial_ready_transport_also_expires_from_local_listen() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {"close": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    policy = replace(resolve_provider_policy("qwen", "manual"), warm_transport_ms=1_000)
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=policy,
        config=VoiceLifecycleConfig(default_warm_transport_ms=0),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)

    runtime._schedule_transport_warm_expiry(
        runtime._asr_session_epoch,
        expected_state=VoiceLifecycleState.LOCAL_LISTEN,
    )
    # default_warm_transport_ms=0 会立刻到期，固定 sleep 赌不起；
    # 到期任务本身就是「已过期并关闭」的同步点。
    expiry = runtime._asr_warm_expiry_task
    assert expiry is not None
    await asyncio.wait_for(expiry, 5)

    assert runtime._asr_session is None
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.DEEP_SLEEP
    asr.close.assert_awaited_once_with()


async def test_prewarming_uses_idle_transport_ttl() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {"close": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    policy = replace(resolve_provider_policy("openai", "provider"), warm_transport_ms=1_000)
    lifecycle = VoiceInputLifecycleController(
        provider_policy=policy,
        config=VoiceLifecycleConfig(default_warm_transport_ms=0),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    runtime._asr_lifecycle = lifecycle

    runtime._schedule_transport_warm_expiry(
        runtime._asr_session_epoch,
        expected_state=VoiceLifecycleState.PREWARMING,
    )
    expiry = runtime._asr_warm_expiry_task
    assert expiry is not None
    await asyncio.wait_for(expiry, 1)

    assert runtime._asr_session is None
    assert lifecycle.snapshot.state is VoiceLifecycleState.DEEP_SLEEP
    asr.close.assert_awaited_once_with()


async def test_warm_idle_uses_provider_transport_ttl() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {"close": AsyncMock()})()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    policy = replace(resolve_provider_policy("openai", "provider"), warm_transport_ms=0)
    lifecycle = VoiceInputLifecycleController(
        provider_policy=policy,
        config=VoiceLifecycleConfig(default_warm_transport_ms=1_000),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    runtime._asr_lifecycle = lifecycle

    runtime._schedule_transport_warm_expiry(
        runtime._asr_session_epoch,
        expected_state=VoiceLifecycleState.WARM_IDLE,
    )
    expiry = runtime._asr_warm_expiry_task
    assert expiry is not None
    await asyncio.wait_for(expiry, 1)

    assert runtime._asr_session is None
    assert lifecycle.snapshot.state is VoiceLifecycleState.DEEP_SLEEP
    asr.close.assert_awaited_once_with()


@pytest.mark.parametrize(
    "replacement",
    ["epoch", "lifecycle", "session", "transport", "state"],
)
async def test_stale_transport_expiry_never_closes_successor(
    replacement: str,
) -> None:
    runtime = _Runtime()
    original_session = type("Asr", (), {"close": AsyncMock()})()
    runtime._asr_session = original_session
    runtime._asr_route_mode = "independent"
    policy = replace(resolve_provider_policy("openai", "provider"), warm_transport_ms=0)
    original_lifecycle = VoiceInputLifecycleController(
        provider_policy=policy,
        shadow_mode=False,
    )
    original_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    original_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    original_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    original_lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    original_lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    runtime._asr_lifecycle = original_lifecycle
    runtime._schedule_transport_warm_expiry(
        runtime._asr_session_epoch,
        expected_state=VoiceLifecycleState.WARM_IDLE,
    )

    successor_session = type("Asr", (), {"close": AsyncMock()})()
    if replacement == "epoch":
        runtime._asr_session_epoch += 1
    elif replacement == "lifecycle":
        successor_lifecycle = VoiceInputLifecycleController(
            provider_policy=policy,
            shadow_mode=False,
        )
        successor_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
        runtime._asr_lifecycle = successor_lifecycle
    elif replacement == "session":
        runtime._asr_session = successor_session
    elif replacement == "transport":
        original_lifecycle.invalidate_transport()
    else:
        original_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
        original_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)

    expected_current_session = (
        successor_session if replacement == "session" else original_session
    )
    assert runtime._asr_session is expected_current_session
    expiry = runtime._asr_warm_expiry_task
    assert expiry is not None
    await asyncio.wait_for(expiry, 1)

    original_session.close.assert_not_awaited()
    expected_current_session.close.assert_not_awaited()


async def test_prewarm_expiry_rechecks_identity_after_detector_reset() -> None:
    runtime = _Runtime()
    original_session = type("Asr", (), {"close": AsyncMock()})()
    runtime._asr_session = original_session
    runtime._asr_route_mode = "independent"
    policy = replace(resolve_provider_policy("openai", "provider"), warm_transport_ms=1_000)
    lifecycle = VoiceInputLifecycleController(
        provider_policy=policy,
        config=VoiceLifecycleConfig(default_warm_transport_ms=0),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    runtime._asr_lifecycle = lifecycle
    reset_started = asyncio.Event()
    reset_release = asyncio.Event()
    detector = _ReadyDetector()

    async def reset() -> None:
        reset_started.set()
        await reset_release.wait()

    detector.reset.side_effect = reset
    runtime._asr_detector = detector
    runtime._schedule_transport_warm_expiry(
        runtime._asr_session_epoch,
        expected_state=VoiceLifecycleState.PREWARMING,
    )
    await asyncio.wait_for(reset_started.wait(), 1)

    successor_session = type("Asr", (), {"close": AsyncMock()})()
    runtime._asr_session = successor_session
    reset_release.set()
    expiry = runtime._asr_warm_expiry_task
    assert expiry is not None
    await asyncio.wait_for(expiry, 1)

    assert lifecycle.snapshot.state is VoiceLifecycleState.PREWARMING
    original_session.close.assert_not_awaited()
    successor_session.close.assert_not_awaited()


async def test_submit_without_lifecycle_returns_typed_unavailable() -> None:
    runtime = _Runtime()
    result = await runtime._asr_runtime.submit(
        ProcessedVoiceFrame(b"\x01\x00" * 160, 16_000, 0.0, False),
        ingress_token=VoiceIngressToken(0, "socket", 0, 0, 0),
    )

    assert result == AsrSubmitResult(AsrSubmitStatus.UNAVAILABLE)
    assert not isinstance(result, bool)


async def test_submit_has_only_typed_top_level_return_paths() -> None:
    source = textwrap.dedent(inspect.getsource(IndependentAsrRuntime.submit))
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.AsyncFunctionDef)
    returns: list[ast.Return] = []

    def collect(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                returns.append(child)
            else:
                collect(child)

    for statement in function.body:
        collect(statement)

    assert returns
    assert all(
        isinstance(return_node.value, ast.Call)
        and isinstance(return_node.value.func, ast.Name)
        and return_node.value.func.id == "AsrSubmitResult"
        for return_node in returns
    )


async def test_deep_sleep_speech_reconnects_and_flushes_pending_audio() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    runtime._asr_provider = "qwen"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.WARM_EXPIRED)
    detector = _ReadyDetector()
    detector.feed = AsyncMock(
        return_value=DetectorFeedResult((SpeechActivityEvent.SPEECH_STARTED,), True)
    )
    runtime._asr_detector = detector
    new_asr = type("Asr", (), {})()
    new_asr.is_ready = True
    connect_started = asyncio.Event()
    connect_release = asyncio.Event()

    async def connect() -> None:
        connect_started.set()
        await connect_release.wait()

    new_asr.connect = AsyncMock(side_effect=connect)
    new_asr.stream_audio = AsyncMock()
    runtime._asr_session_factory = MagicMock(return_value=new_asr)
    runtime._asr_transport_selection = _selection("qwen")

    await runtime._route_microphone_audio(
        b"\x03\x00" * 160,
        sample_rate_hz=16_000,
    )
    await asyncio.wait_for(connect_started.wait(), 1)

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.PREWARMING
    assert runtime._asr_lifecycle.pending_connect_bytes == 320
    connect_release.set()
    assert runtime._asr_transport_task is not None
    await runtime._asr_transport_task

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    new_asr.connect.assert_awaited_once_with()
    new_asr.stream_audio.assert_awaited_once_with(
        b"\x03\x00" * 160,
        sample_rate_hz=16_000,
    )


@pytest.mark.parametrize("provider", ["glm", "gemini"])
async def test_smart_turn_fail_open_buffers_until_deep_sleep_transport_reconnects(
    provider: str,
) -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    runtime._asr_provider = provider
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy(provider, "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    runtime._asr_lifecycle.transition(VoiceLifecycleEvent.WARM_EXPIRED)
    runtime._asr_detector = _QueuedSmartTurnDetector()
    new_asr = type("Asr", (), {})()
    new_asr.is_ready = True
    connect_started = asyncio.Event()
    connect_release = asyncio.Event()

    async def connect() -> None:
        connect_started.set()
        await connect_release.wait()

    new_asr.connect = AsyncMock(side_effect=connect)
    new_asr.stream_audio = AsyncMock()
    runtime._asr_session_factory = MagicMock(return_value=new_asr)
    runtime._asr_transport_selection = _selection(provider)
    pcm16 = b"\x03\x00" * 160

    await runtime._route_microphone_audio(
        pcm16,
        sample_rate_hz=16_000,
        rnnoise_available=False,
    )
    await asyncio.wait_for(connect_started.wait(), 1)

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.PREWARMING
    assert runtime._asr_route_mode == "independent"
    assert runtime._asr_lifecycle.pending_connect_bytes == len(pcm16)
    connect_release.set()
    await runtime._asr_detector_dispatcher.wait_idle()
    await runtime._asr_audio_dispatcher.wait_idle()

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_route_mode == "independent"
    assert runtime._omni_mic_audio_bytes == 0
    new_asr.connect.assert_awaited_once_with()
    new_asr.stream_audio.assert_awaited_once_with(
        pcm16,
        sample_rate_hz=16_000,
    )
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    assert all(status.get("code") != "ASR_BLOCKED_ENDPOINTING" for status in statuses)


async def test_optimization_disabled_buffers_until_initial_transport_is_ready() -> None:
    runtime = _Runtime()
    runtime._voice_input_resource_optimization_enabled = False
    runtime._asr_route_mode = "independent"
    runtime._asr_provider = "glm"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("glm", "manual"),
        shadow_mode=False,
        resource_optimization_enabled=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_detector = _QueuedSmartTurnDetector()
    new_asr = type("Asr", (), {})()
    new_asr.is_ready = True
    connect_started = asyncio.Event()
    connect_release = asyncio.Event()

    async def connect() -> None:
        connect_started.set()
        await connect_release.wait()

    new_asr.connect = AsyncMock(side_effect=connect)
    new_asr.stream_audio = AsyncMock()
    runtime._asr_session_factory = MagicMock(return_value=new_asr)
    runtime._asr_transport_selection = _selection("glm")
    pcm16 = b"\x04\x00" * 160

    await runtime._route_microphone_audio(
        pcm16,
        sample_rate_hz=16_000,
        rnnoise_available=False,
    )
    await asyncio.wait_for(connect_started.wait(), 1)

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.PREWARMING
    assert runtime._asr_route_mode == "independent"
    assert runtime._asr_lifecycle.pending_connect_bytes == len(pcm16)
    connect_release.set()
    await runtime._asr_detector_dispatcher.wait_idle()
    await runtime._asr_audio_dispatcher.wait_idle()

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_route_mode == "independent"
    assert runtime._omni_mic_audio_bytes == 0
    new_asr.connect.assert_awaited_once_with()
    new_asr.stream_audio.assert_awaited_once_with(
        pcm16,
        sample_rate_hz=16_000,
    )
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    assert all(status.get("code") != "ASR_BLOCKED_ENDPOINTING" for status in statuses)


async def test_hard_mute_is_backend_authoritative_and_rejects_stale_lease_events() -> (
    None
):
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.close = AsyncMock()
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = type("Detector", (), {})()
    detector.reset = AsyncMock()
    detector.feed = AsyncMock(return_value=DetectorFeedResult((), True))
    runtime._asr_detector = detector
    runtime._clear_audio_stream_queue = MagicMock()
    runtime.hot_swap_audio_cache = [b"old-pcm"]
    old_token = runtime._capture_ingress_token(runtime._asr_lifecycle)

    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            12,
            owner="core",
            hard_muted=True,
            focus_suppressed=False,
        )
        is True
    )
    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )

    asr.close.assert_awaited_once_with()
    runtime._clear_audio_stream_queue.assert_called_once_with("lease_sync")
    assert runtime.hot_swap_audio_cache == []
    assert runtime._ingress_token_matches(old_token) is False
    detector.reset.assert_awaited_once_with()
    detector.feed.assert_not_awaited()
    asr.stream_audio.assert_not_awaited()
    assert runtime._asr_lifecycle.pre_roll_bytes == 0

    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            11,
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
        )
        is False
    )
    assert runtime._voice_input_suppressed is True
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            13,
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    assert runtime._voice_input_suppressed is False


async def test_hard_mute_during_detector_await_invalidates_inflight_pcm() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.close = AsyncMock()
    asr.stream_audio = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    runtime._asr_provider = "qwen"
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)

    feed_started = asyncio.Event()
    release_feed = asyncio.Event()

    class _BlockingDetector(_ReadyDetector):
        async def feed(self, _pcm16: bytes, **_kwargs) -> DetectorFeedResult:
            feed_started.set()
            await release_feed.wait()
            return DetectorFeedResult((), True)

    runtime._asr_detector = _BlockingDetector()
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        runtime._asr_session_epoch,
    )

    route_task = asyncio.create_task(
        runtime._route_microphone_audio(
            b"\x01\x00" * 160,
            sample_rate_hz=16_000,
        )
    )
    await asyncio.wait_for(feed_started.wait(), 1)
    await runtime._handle_voice_input_control(
        "lease_sync",
        1,
        owner="core",
        hard_muted=True,
        focus_suppressed=False,
    )
    release_feed.set()

    assert await route_task is True
    asr.stream_audio.assert_not_awaited()
    assert runtime._asr_audio_bytes == 0
    assert runtime._omni_mic_audio_bytes == 0


async def test_hard_mute_suppresses_stale_audio_dispatcher_failure() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.close = AsyncMock()
    runtime._asr_session = asr
    _install_ready_lifecycle(runtime, "qwen")
    runtime._asr_detector = _ReadyDetector()
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    turn_token = VoiceTurnToken(
        ingress=runtime._capture_ingress_token(lifecycle),
        turn_id=lifecycle.snapshot.turn_id,
    )

    assert await runtime._handle_voice_input_control(
        "lease_sync",
        1,
        owner="core",
        hard_muted=True,
        focus_suppressed=False,
    )
    runtime.send_status.reset_mock()
    await runtime._handle_asr_audio_dispatcher_failure(
        turn_token,
        RuntimeError("old provider write failed after hard mute"),
    )

    assert runtime._asr_route_mode == "independent"
    assert runtime._asr_lifecycle is lifecycle
    runtime.send_status.assert_not_awaited()


async def test_game_takeover_suppresses_stale_detector_dispatcher_failure() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.close = AsyncMock()
    runtime._asr_session = asr
    _install_ready_lifecycle(runtime, "qwen")
    detector = _ReadyDetector()
    detector.detector_epoch = 1
    runtime._asr_detector = detector
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    ingress_token = runtime._capture_ingress_token(lifecycle)
    envelope = CoreDetectorEventEnvelope(
        event=DetectorRuntimeEvent(
            ingress=DetectorIngressIdentity(
                ingress_token=ingress_token,
                detector_epoch=detector.detector_epoch,
                sequence_no=1,
            ),
            candidate=DetectorCandidateKey(detector.detector_epoch, 1),
            kind="control_lane_failed",
        ),
        detector_ref=detector,
        lifecycle_ref=lifecycle,
        session_epoch=runtime._asr_session_epoch,
    )

    assert await runtime._handle_voice_input_control(
        "game_takeover",
        1,
    )
    runtime.send_status.reset_mock()
    await runtime._handle_asr_detector_dispatcher_failure(
        envelope,
        RuntimeError("old detector callback failed after game takeover"),
    )

    assert runtime._asr_route_mode == "independent"
    assert runtime._asr_lifecycle is lifecycle
    assert lifecycle.snapshot.state is VoiceLifecycleState.SUSPENDED
    runtime.send_status.assert_not_awaited()


async def test_new_websocket_connection_resets_mic_lease_generation_once() -> None:
    runtime = _Runtime()
    runtime._voice_lease_generation = 12

    assert runtime._begin_voice_input_connection("socket-a") is True
    assert runtime._voice_lease_generation == -1
    assert runtime._voice_lease_control_seen is False
    assert runtime._voice_input_accepts_pcm() is False
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="none",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    assert runtime._voice_lease_control_seen is True
    assert runtime._voice_input_accepts_pcm() is False

    assert runtime._begin_voice_input_connection("socket-a") is False
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
        )
        is False
    )

    assert runtime._begin_voice_input_connection("socket-b") is True
    assert runtime._voice_lease_control_seen is False
    assert runtime._voice_input_accepts_pcm() is False
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    assert runtime._voice_input_accepts_pcm() is True


async def test_legacy_audio_session_authorization_is_one_shot() -> None:
    runtime = _Runtime()
    runtime._asr_runtime.abort = AsyncMock()

    assert runtime._begin_voice_input_connection("legacy-socket") is True
    assert await runtime._ensure_voice_input_session_authorized("legacy-socket") is True
    assert runtime._voice_lease_generation == 0
    assert runtime._voice_lease_synchronized is True
    assert runtime._voice_lease_owner == "core"
    assert runtime._voice_lease_hard_muted is False
    assert runtime._voice_lease_focus_suppressed is False
    assert runtime._voice_input_accepts_pcm() is True
    runtime._asr_runtime.abort.assert_awaited_once_with("legacy_session_start")

    runtime._asr_runtime.abort.reset_mock()
    assert await runtime._ensure_voice_input_session_authorized("legacy-socket") is True
    assert runtime._voice_lease_generation == 0
    runtime._asr_runtime.abort.assert_not_awaited()


async def test_explicit_owner_none_cannot_be_overridden_by_legacy_authorization() -> (
    None
):
    runtime = _Runtime()
    runtime._asr_runtime.abort = AsyncMock()

    assert runtime._begin_voice_input_connection("explicit-socket") is True
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="none",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    runtime._asr_runtime.abort.reset_mock()

    assert (
        await runtime._ensure_voice_input_session_authorized("explicit-socket") is True
    )
    assert runtime._voice_lease_generation == 1
    assert runtime._voice_lease_owner == "none"
    assert runtime._voice_input_accepts_pcm() is False
    runtime._asr_runtime.abort.assert_not_awaited()


@pytest.mark.parametrize(
    ("event", "generation"),
    [
        ("invalid-control", 0),
        ("lease_sync", -1),
    ],
)
async def test_rejected_explicit_control_permanently_disables_legacy_fallback(
    event: str,
    generation: int,
) -> None:
    runtime = _Runtime()
    runtime._asr_runtime.abort = AsyncMock()

    assert runtime._begin_voice_input_connection("explicit-socket") is True
    assert (
        await runtime._handle_voice_input_control(
            event,
            generation,
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
        )
        is False
    )
    assert runtime._voice_lease_control_seen is True
    assert (
        await runtime._ensure_voice_input_session_authorized("explicit-socket") is False
    )
    await runtime._enqueue_audio_stream_data(
        {
            "input_type": "audio",
            "sample_rate_hz": 16_000,
            "data": [1] * 160,
        }
    )

    assert runtime._voice_lease_synchronized is False
    assert runtime._voice_lease_owner == "none"
    assert runtime._voice_input_accepts_pcm() is False
    assert runtime._audio_stream_queue.empty()
    runtime._asr_runtime.abort.assert_not_awaited()


async def test_legacy_authorization_loses_race_to_new_connection_identity() -> None:
    runtime = _Runtime()
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def _block_old_abort(_reason: str) -> None:
        abort_started.set()
        await release_abort.wait()

    old_abort = AsyncMock(side_effect=_block_old_abort)
    runtime._asr_runtime.abort = old_abort
    assert runtime._begin_voice_input_connection("socket-a") is True

    authorize_task = asyncio.create_task(
        runtime._ensure_voice_input_session_authorized("socket-a")
    )
    await asyncio.wait_for(abort_started.wait(), 1)

    assert runtime._begin_voice_input_connection("socket-b") is True
    runtime._asr_runtime.abort = AsyncMock()
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="none",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )
    release_abort.set()

    assert await authorize_task is False
    assert runtime._voice_lease_connection_id == "socket-b"
    assert runtime._voice_lease_generation == 1
    assert runtime._voice_lease_control_seen is True
    assert runtime._voice_lease_owner == "none"
    assert runtime._voice_input_accepts_pcm() is False
    old_abort.assert_awaited_once_with("legacy_session_start")


async def test_game_owner_and_hard_mute_remain_simultaneously_authoritative() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")

    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="game",
            hard_muted=True,
            focus_suppressed=False,
        )
        is True
    )

    assert runtime._voice_lease_owner == "game"
    assert runtime._voice_lease_hard_muted is True
    assert runtime._voice_input_suppression_reasons == {"game", "hard_mute"}
    assert runtime._voice_input_accepts_pcm() is False
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.SUSPENDED


async def test_accepted_final_is_recorded_and_injected_once() -> None:
    runtime = _Runtime()
    runtime._asr_provider = "glm"
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "glm")

    await asyncio.gather(
        runtime._handle_independent_asr_final(" hello ", epoch, "glm"),
        runtime._handle_independent_asr_final(" hello ", epoch, "glm"),
    )
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.handle_input_transcript.assert_awaited_once_with(
        "hello",
        is_voice_source=True,
        source="independent_asr",
        metadata={"provider": "glm"},
        source_game_route_identity=None,
    )
    runtime.session.create_response.assert_awaited_once_with("hello")


async def test_final_records_segmented_wire_audio_committed_at_seal() -> None:
    runtime = _Runtime()
    session = SimpleNamespace(is_ready=True, provider_wire_audio_ms=0)
    runtime._asr_session = session
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "glm")
    # Segmented sessions advance the cumulative counter only at the seal-time
    # physical-segment commit, after the dispatcher's last per-chunk sample.
    session.provider_wire_audio_ms = 480

    await runtime._handle_independent_asr_final("hello", epoch, "glm")
    await runtime._wait_asr_transcript_dispatch_idle()

    metrics = runtime._asr_lifecycle.metrics
    assert metrics.provider_wire_audio_ms == 480
    assert metrics.cloud_audio_ms == 480
    assert runtime._asr_last_provider_wire_audio_ms == 480


async def test_final_does_not_double_count_sampled_streaming_wire_audio() -> None:
    runtime = _Runtime()
    session = SimpleNamespace(is_ready=True, provider_wire_audio_ms=480)
    runtime._asr_session = session
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "qwen")
    # Streaming sessions advance the counter inside stream_audio, so the
    # per-chunk dispatcher sample has already recorded the full amount.
    runtime._sync_provider_wire_metrics(session)
    assert runtime._asr_lifecycle.metrics.provider_wire_audio_ms == 480

    await runtime._handle_independent_asr_final("hello", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    metrics = runtime._asr_lifecycle.metrics
    assert metrics.provider_wire_audio_ms == 480
    assert metrics.cloud_audio_ms == 480
    assert runtime._asr_last_provider_wire_audio_ms == 480


async def test_identical_text_in_consecutive_turns_is_delivered_twice() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch

    for _ in range(2):
        await runtime._handle_independent_asr_activity(
            SpeechActivityEvent.SPEECH_STARTED,
            epoch,
        )
        await runtime._handle_independent_asr_endpoint(epoch)
        await runtime._handle_independent_asr_final("嗯", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["嗯", "嗯"]
    assert [
        call.args[0] for call in runtime.session.create_response.await_args_list
    ] == [
        "嗯",
        "嗯",
    ]


async def test_blocked_core_response_does_not_block_next_asr_turn() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch
    response_started = asyncio.Event()
    release_response = asyncio.Event()

    async def block_response(_text: str) -> None:
        response_started.set()
        await release_response.wait()

    runtime.session.create_response.side_effect = block_response
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "qwen")
    await response_started.wait()

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )

    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    release_response.set()
    await runtime._wait_asr_transcript_dispatch_idle()


async def test_core_swap_cancels_blocked_old_final_without_touching_new_state() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    old_epoch = runtime._asr_session_epoch
    old_core_session = runtime.session
    transcript_started = asyncio.Event()
    release_transcript = asyncio.Event()

    async def block_transcript(_text: str, **_kwargs: object) -> bool:
        transcript_started.set()
        await release_transcript.wait()
        return True

    runtime.handle_input_transcript.side_effect = block_transcript
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        old_epoch,
    )
    await runtime._handle_independent_asr_endpoint(old_epoch)
    await runtime._handle_independent_asr_final("old", old_epoch, "qwen")
    await transcript_started.wait()

    await runtime._close_independent_asr(next_route_mode="blocked")
    new_core_session = type("NewCore", (), {})()
    new_core_session.create_response = AsyncMock()
    new_core_session.handle_interruption = AsyncMock()
    runtime.session = new_core_session
    _install_ready_lifecycle(runtime, "qwen")
    new_lifecycle = runtime._asr_lifecycle
    assert new_lifecycle is not None
    expected_state = new_lifecycle.snapshot.state

    release_transcript.set()
    await asyncio.sleep(0)

    old_core_session.create_response.assert_not_awaited()
    new_core_session.create_response.assert_not_awaited()
    assert runtime._asr_lifecycle is new_lifecycle
    assert new_lifecycle.snapshot.state is expected_state
    assert runtime._asr_sealed_turn_token is None


async def test_late_first_final_then_second_final_recovers_in_linear_order() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch
    events: list[str] = []

    runtime.session.handle_interruption.side_effect = lambda: events.append(
        "interruption"
    )
    runtime.handle_new_message.side_effect = lambda: events.append("prepare")
    runtime.handle_input_transcript.side_effect = lambda text, **_kwargs: (
        events.append(f"transcript:{text}") or True
    )
    runtime.session.create_response.side_effect = lambda text: events.append(
        f"response:{text}"
    )

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first fragment", epoch, "openai")
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("second fragment", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert events.count("interruption") == 2
    assert events.count("prepare") == 2
    assert [event for event in events if event.startswith("transcript:")] == [
        "transcript:first fragment",
        "transcript:second fragment",
    ]
    assert [event for event in events if event.startswith("response:")] == [
        "response:first fragment",
        "response:second fragment",
    ]


async def test_three_pending_finals_recover_without_request_multiplication() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    for text in ("first", "second", "third"):
        await runtime._handle_independent_asr_activity(
            SpeechActivityEvent.SPEECH_STARTED,
            epoch,
        )
        await runtime._handle_independent_asr_endpoint(epoch)
        await runtime._handle_independent_asr_final(text, epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert runtime.session.handle_interruption.await_count == 3
    assert runtime.handle_new_message.await_count == 3
    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == [
        "first",
        "second",
        "third",
    ]
    assert [
        call.args[0] for call in runtime.session.create_response.await_args_list
    ] == [
        "first",
        "second",
        "third",
    ]


async def test_consumed_or_suppressed_final_does_not_create_response() -> None:
    runtime = _Runtime()
    runtime.handle_input_transcript.return_value = False
    await _start_and_seal_turn(runtime, "gemini")

    await runtime._handle_independent_asr_final(
        "echo",
        runtime._asr_session_epoch,
        "gemini",
    )
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.session.create_response.assert_not_awaited()


async def test_close_invalidates_late_final_before_waiting_for_provider() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.close = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    old_epoch = runtime._asr_session_epoch

    await runtime._close_independent_asr(next_route_mode="blocked")
    await runtime._handle_independent_asr_final("late", old_epoch, "glm")

    asr.close.assert_awaited_once_with()
    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()
    assert runtime._asr_route_mode == "blocked"


async def test_close_releases_independent_audio_pipeline() -> None:
    runtime = _Runtime()
    pipeline = type("Pipeline", (), {})()
    pipeline.close = AsyncMock()
    runtime._voice_input_audio_pipeline = pipeline

    await runtime._close_independent_asr(next_route_mode="blocked")

    pipeline.close.assert_awaited_once_with()
    assert runtime._voice_input_audio_pipeline is not pipeline


async def test_cancelled_core_close_keeps_detached_cleanup_owned() -> None:
    runtime = _Runtime()
    pipeline_close_started = asyncio.Event()
    release_pipeline_close = asyncio.Event()
    registry_wait_started = asyncio.Event()
    release_registry_wait = asyncio.Event()

    async def block_pipeline_close() -> None:
        pipeline_close_started.set()
        await release_pipeline_close.wait()

    async def block_registry_wait() -> None:
        registry_wait_started.set()
        await release_registry_wait.wait()

    pipeline = SimpleNamespace(close=AsyncMock(side_effect=block_pipeline_close))
    runtime._voice_input_audio_pipeline = pipeline
    runtime._voice_input_registry.wait_idle = AsyncMock(
        side_effect=block_registry_wait
    )
    runtime._asr_runtime.stop_session = AsyncMock()

    closing = asyncio.create_task(
        runtime._close_independent_asr(next_route_mode="blocked")
    )
    await asyncio.wait_for(pipeline_close_started.wait(), 1)
    await asyncio.wait_for(registry_wait_started.wait(), 1)
    replacement = runtime._voice_input_audio_pipeline
    cleanup_tasks = set(runtime._core_asr_cleanup_tasks)

    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert replacement is not pipeline
    assert all(task.cancelled() is False for task in cleanup_tasks)
    release_pipeline_close.set()
    release_registry_wait.set()
    await asyncio.wait_for(asyncio.gather(*cleanup_tasks), 1)

    pipeline.close.assert_awaited_once_with()
    runtime._asr_runtime.stop_session.assert_awaited_once_with()


async def test_cancelled_core_close_waiting_for_pipeline_lock_stays_owned() -> None:
    runtime = _Runtime()
    gate = _GateAsyncLock()
    runtime._voice_input_pipeline_transition_lock = gate
    old_pipeline = SimpleNamespace(close=AsyncMock())
    runtime._voice_input_audio_pipeline = old_pipeline
    runtime._independent_asr_provider = "old-provider"
    runtime._independent_asr_route_key = "old-core"
    runtime._voice_input_registry.wait_idle = AsyncMock()
    runtime._asr_runtime.stop_session = AsyncMock()

    closing = asyncio.create_task(
        runtime._close_independent_asr(next_route_mode="blocked")
    )
    await asyncio.wait_for(gate.requested.wait(), 1)
    close_cleanup = next(
        task
        for task in runtime._core_asr_cleanup_tasks
        if task.get_name() == "core-independent-asr-close"
    )

    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert close_cleanup.cancelled() is False
    assert runtime._voice_input_audio_pipeline is old_pipeline
    gate.release.set()
    await asyncio.wait_for(asyncio.shield(close_cleanup), 1)

    assert runtime._voice_input_audio_pipeline is not old_pipeline
    assert runtime._independent_asr_provider is None
    assert runtime._independent_asr_route_key is None
    old_pipeline.close.assert_awaited_once_with()
    runtime._voice_input_registry.wait_idle.assert_awaited_once_with()
    runtime._asr_runtime.stop_session.assert_awaited_once_with()


async def test_core_close_detaches_shared_state_before_registry_wait() -> None:
    runtime = _Runtime()
    registry_wait_started = asyncio.Event()
    release_registry_wait = asyncio.Event()

    async def block_registry_wait() -> None:
        registry_wait_started.set()
        await release_registry_wait.wait()

    old_pipeline = SimpleNamespace(close=AsyncMock())
    runtime._voice_input_audio_pipeline = old_pipeline
    runtime._independent_asr_provider = "old-provider"
    runtime._independent_asr_route_key = "old-core"
    runtime._voice_input_registry.wait_idle = AsyncMock(
        side_effect=block_registry_wait
    )
    runtime._asr_runtime.stop_session = AsyncMock()

    closing = asyncio.create_task(
        runtime._close_independent_asr(next_route_mode="blocked")
    )
    await asyncio.wait_for(registry_wait_started.wait(), 1)

    detached_replacement = runtime._voice_input_audio_pipeline
    assert detached_replacement is not old_pipeline
    assert runtime._independent_asr_provider is None
    assert runtime._independent_asr_route_key is None

    runtime._begin_asr_route_operation()
    runtime._independent_asr_provider = "new-provider"
    runtime._independent_asr_route_key = "new-core"
    runtime._set_microphone_route("independent")
    release_registry_wait.set()
    await asyncio.wait_for(closing, 1)

    assert runtime._voice_input_audio_pipeline is detached_replacement
    assert runtime._independent_asr_provider == "new-provider"
    assert runtime._independent_asr_route_key == "new-core"
    assert runtime._asr_route_mode == "independent"
    runtime._asr_runtime.stop_session.assert_not_awaited()


async def test_cancelled_successor_close_owns_runtime_cleanup_after_old_close() -> None:
    runtime = _Runtime()
    first_wait_started = asyncio.Event()
    second_wait_started = asyncio.Event()
    release_registry_wait = asyncio.Event()
    wait_calls = 0

    async def block_registry_wait() -> None:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            first_wait_started.set()
        elif wait_calls == 2:
            second_wait_started.set()
        await release_registry_wait.wait()

    runtime._voice_input_registry.wait_idle = AsyncMock(
        side_effect=block_registry_wait
    )
    runtime._asr_runtime.stop_session = AsyncMock()

    retired_close = asyncio.create_task(
        runtime._close_independent_asr(next_route_mode="blocked")
    )
    await first_wait_started.wait()

    successor_close = asyncio.create_task(
        runtime._close_independent_asr(next_route_mode="blocked")
    )
    await second_wait_started.wait()
    successor_cleanup = tuple(runtime._core_asr_cleanup_tasks)
    successor_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await successor_close

    release_registry_wait.set()
    await retired_close
    await asyncio.gather(*successor_cleanup)

    runtime._asr_runtime.stop_session.assert_awaited_once_with()


async def test_stale_start_waiting_for_pipeline_lock_cannot_replace_successor(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime._close_independent_asr = AsyncMock()
    gate = _GateAsyncLock()
    runtime._voice_input_pipeline_transition_lock = gate
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(
            return_value={
                "independentAsrEnabled": False,
                "noiseReductionEnabled": False,
            }
        ),
    )

    starting = asyncio.create_task(runtime._start_independent_asr_if_enabled("audio"))
    await asyncio.wait_for(gate.requested.wait(), 1)
    runtime._begin_asr_route_operation()
    successor_pipeline = SimpleNamespace(
        nr_enabled=True,
        close=AsyncMock(),
    )
    runtime._voice_input_audio_pipeline = successor_pipeline
    gate.release.set()
    await asyncio.wait_for(starting, 1)

    assert runtime._voice_input_audio_pipeline is successor_pipeline
    assert runtime._voice_input_noise_reduction_enabled is True
    successor_pipeline.close.assert_not_awaited()


async def test_stale_close_waiting_for_pipeline_lock_cannot_replace_successor() -> None:
    runtime = _Runtime()
    runtime._asr_runtime.stop_session = AsyncMock()
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "old-provider"
    runtime._independent_asr_route_key = "old-core"
    gate = _GateAsyncLock()
    runtime._voice_input_pipeline_transition_lock = gate

    closing = asyncio.create_task(
        runtime._close_independent_asr(next_route_mode="blocked")
    )
    await asyncio.wait_for(gate.requested.wait(), 1)
    runtime._begin_asr_route_operation()
    successor_pipeline = SimpleNamespace(
        nr_enabled=True,
        close=AsyncMock(),
    )
    runtime._voice_input_audio_pipeline = successor_pipeline
    runtime._independent_asr_provider = "new-provider"
    runtime._independent_asr_route_key = "new-core"
    runtime._set_microphone_route("independent")
    gate.release.set()
    await asyncio.wait_for(closing, 1)

    assert runtime._voice_input_audio_pipeline is successor_pipeline
    assert runtime._independent_asr_provider == "new-provider"
    assert runtime._independent_asr_route_key == "new-core"
    assert runtime._asr_route_mode == "independent"
    successor_pipeline.close.assert_not_awaited()
    runtime._asr_runtime.stop_session.assert_not_awaited()


async def test_cancelled_start_settings_swap_keeps_pipeline_cleanup_owned(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def block_pipeline_close() -> None:
        close_started.set()
        await release_close.wait()

    stale_pipeline = SimpleNamespace(
        nr_enabled=True,
        close=AsyncMock(side_effect=block_pipeline_close),
    )
    runtime._voice_input_audio_pipeline = stale_pipeline
    runtime._close_independent_asr = AsyncMock()
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(
            return_value={
                "independentAsrEnabled": False,
                "noiseReductionEnabled": False,
            }
        ),
    )

    starting = asyncio.create_task(
        runtime._start_independent_asr_if_enabled("audio")
    )
    await asyncio.wait_for(close_started.wait(), 1)
    replacement = runtime._voice_input_audio_pipeline
    cleanup = next(
        task
        for task in runtime._core_asr_cleanup_tasks
        if task.get_name() == "core-voice-input-pipeline-close"
    )

    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert replacement is not stale_pipeline
    assert replacement.nr_enabled is False
    assert cleanup.cancelled() is False
    release_close.set()
    await asyncio.wait_for(cleanup, 1)
    stale_pipeline.close.assert_awaited_once_with()


async def test_cancelled_noise_reduction_swap_keeps_pipeline_cleanup_owned() -> None:
    runtime = _Runtime()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def block_pipeline_close() -> None:
        close_started.set()
        await release_close.wait()

    stale_pipeline = SimpleNamespace(
        nr_enabled=True,
        close=AsyncMock(side_effect=block_pipeline_close),
    )
    runtime._voice_input_audio_pipeline = stale_pipeline

    applying = asyncio.create_task(
        runtime.apply_voice_input_noise_reduction(False)
    )
    await asyncio.wait_for(close_started.wait(), 1)
    replacement = runtime._voice_input_audio_pipeline
    cleanup = next(
        task
        for task in runtime._core_asr_cleanup_tasks
        if task.get_name() == "core-voice-input-pipeline-close"
    )

    applying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await applying

    assert replacement is not stale_pipeline
    assert replacement.nr_enabled is False
    assert cleanup.cancelled() is False
    release_close.set()
    await asyncio.wait_for(cleanup, 1)
    stale_pipeline.close.assert_awaited_once_with()


async def test_close_uses_reusable_runtime_stop_and_keeps_requested_blocked_route() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.close = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_runtime.stop_session = AsyncMock()
    runtime._asr_runtime.close = AsyncMock()
    runtime._asr_route_mode = "independent"

    await runtime._close_independent_asr(next_route_mode="blocked")

    assert runtime._asr_route_mode == "blocked"
    runtime._asr_runtime.stop_session.assert_awaited_once_with()
    runtime._asr_runtime.close.assert_not_awaited()
    assert not hasattr(runtime._asr_runtime, "_asr_route_mode")
    assert (
        await runtime._route_microphone_audio(b"\x00\x00", sample_rate_hz=16_000)
        is True
    )


async def test_close_requires_callers_to_declare_the_next_route() -> None:
    parameter = inspect.signature(AsrRuntimeMixin._close_independent_asr).parameters[
        "next_route_mode"
    ]

    assert parameter.default is inspect.Parameter.empty


async def test_asr_stream_failure_never_replays_the_failed_frame_to_omni() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock(side_effect=RuntimeError("sensitive provider body"))
    runtime._asr_session = asr
    runtime._asr_provider = "qwen"
    runtime._asr_route_mode = "independent"
    await _install_active_smart_turn(runtime, "qwen")
    failed_dispatcher = runtime._asr_audio_dispatcher

    consumed = await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )
    await failed_dispatcher.wait_idle()
    await asyncio.gather(*tuple(failed_dispatcher._failure_tasks))

    assert consumed is True
    assert runtime._asr_route_mode == "blocked"
    assert runtime._asr_session is None
    assert "sensitive provider body" not in str(runtime.send_status.await_args)


async def test_asr_backpressure_reports_specific_blocking_status() -> None:
    runtime = _Runtime()
    blocking_status_sent = asyncio.Event()

    async def record_status(message: str) -> None:
        if "ASR_STREAM_BACKPRESSURE" in message:
            blocking_status_sent.set()

    runtime.send_status.side_effect = record_status
    asr = type("Asr", (), {})()
    asr.is_ready = True
    asr.stream_audio = AsyncMock(
        side_effect=RuntimeError("ASR_STREAM_BACKPRESSURE: queue full")
    )
    asr.close = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_provider = "qwen"
    runtime._asr_route_mode = "independent"
    await _install_active_smart_turn(runtime, "qwen")

    await runtime._route_microphone_audio(
        b"\x00\x00" * 160,
        sample_rate_hz=16_000,
    )
    await runtime._asr_audio_dispatcher.wait_idle()
    await asyncio.wait_for(blocking_status_sent.wait(), 1)

    assert "ASR_STREAM_BACKPRESSURE" in runtime.send_status.await_args.args[0]
    assert runtime._asr_route_mode == "blocked"


async def test_independent_asr_setting_is_persisted_as_a_boolean() -> None:
    assert "independentAsrEnabled" in preferences._ALLOWED_CONVERSATION_SETTINGS
    assert (
        "voiceInputResourceOptimizationEnabled"
        in preferences._ALLOWED_CONVERSATION_SETTINGS
    )
    assert (
        "voice_input_resource_optimization_enabled"
        not in preferences._ALLOWED_CONVERSATION_SETTINGS
    )


async def test_start_uses_current_core_route_only_after_provider_ready(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    asr = type("Asr", (), {})()
    asr.connect = AsyncMock()
    asr.close = AsyncMock()
    factory = MagicMock(return_value=asr)
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=_selection("gemini")),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        factory,
    )

    await runtime._start_independent_asr_if_enabled("audio")

    asr.connect.assert_awaited_once_with()
    assert runtime._asr_session is asr
    assert runtime._asr_provider == "gemini"
    assert runtime._asr_route_mode == "independent"
    assert factory.call_args.args == ("gemini",)
    assert factory.call_args.kwargs["selection"].provider_key == "gemini"


async def test_runtime_builds_primary_candidate_from_its_single_selection(
    monkeypatch,
) -> None:
    import main_logic.asr_client as asr_client
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    selection = asr_client._AsrSelection(
        provider_key="gemini",
        endpointing_mode="manual",
    )
    resolver = MagicMock(return_value=selection)
    asr = type("Asr", (), {})()
    asr.connect = AsyncMock()
    asr.close = AsyncMock()
    builder = MagicMock(return_value=asr)

    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(runtime_module, "_resolve_asr_selection", resolver)
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        builder,
        raising=False,
    )
    assert not hasattr(runtime_module, "create_asr_session")

    await runtime._start_independent_asr_if_enabled("audio")

    resolver.assert_called_once_with("gemini")
    assert builder.call_args.kwargs["selection"] is selection
    asr.connect.assert_awaited_once_with()
    assert runtime._asr_session is asr
    assert runtime._asr_provider == "gemini"
    assert runtime._asr_route_mode == "independent"


async def _start_bridge_and_capture_builder_call(monkeypatch, runtime):
    import main_logic.asr_client.runtime as runtime_module

    asr = type("Asr", (), {})()
    asr.connect = AsyncMock()
    asr.close = AsyncMock()
    builder = MagicMock(return_value=asr)
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=_selection("gemini")),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        builder,
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "independent"
    return builder.call_args.kwargs


async def test_start_forwards_core_user_language_to_session_builder(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.user_language = "ja"

    kwargs = await _start_bridge_and_capture_builder_call(monkeypatch, runtime)

    assert kwargs["user_language"] == "ja"


async def test_start_without_user_language_builds_session_without_hint(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    assert getattr(runtime, "user_language", None) is None

    kwargs = await _start_bridge_and_capture_builder_call(monkeypatch, runtime)

    assert kwargs["user_language"] is None


async def test_startup_close_window_is_blocked_before_settings_resolution(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class _OldAsr:
        is_ready = True

        async def close(self) -> None:
            close_started.set()
            await release_close.wait()

    runtime._asr_session = _OldAsr()
    runtime._asr_route_mode = "independent"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": False}),
    )

    start_task = asyncio.create_task(runtime._start_independent_asr_if_enabled("audio"))
    await asyncio.wait_for(close_started.wait(), 1)

    assert runtime._asr_route_mode == "blocked"
    assert (
        await runtime._route_microphone_audio(b"\x00\x00", sample_rate_hz=16_000)
        is True
    )

    release_close.set()
    await asyncio.wait_for(start_task, 1)
    assert runtime._asr_route_mode == "native"
    assert not hasattr(runtime._asr_runtime, "_asr_required")


async def test_explicit_intl_soniox_is_selected_before_audio(monkeypatch) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    asr = type("Asr", (), {})()
    asr.connect = AsyncMock()
    asr.close = AsyncMock()
    factory = MagicMock(return_value=asr)
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=_selection("soniox", "provider")),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        factory,
    )

    await runtime._start_independent_asr_if_enabled("audio")

    asr.connect.assert_awaited_once_with()
    assert runtime._asr_session is asr
    assert runtime._asr_provider == "soniox"
    assert runtime._asr_received_audio is False


async def test_soniox_connect_failure_retries_same_selection_before_audio(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    soniox_selection = _selection("soniox", "provider")
    primary_resolver = MagicMock(return_value=soniox_selection)
    forbidden_core_resolver = MagicMock(
        side_effect=AssertionError("Soniox recovery must not resolve another provider")
    )
    save_settings = MagicMock(
        side_effect=AssertionError("Provider recovery must not rewrite user settings")
    )
    sleep = AsyncMock()
    sessions = []
    for side_effect in (
        RuntimeError("provider detail 1"),
        RuntimeError("provider detail 2"),
        None,
    ):
        session = type("Soniox", (), {})()
        session.connect = AsyncMock(side_effect=side_effect)
        session.close = AsyncMock()
        sessions.append(session)
    built_selections = []
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        primary_resolver,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_core_follow_selection",
        forbidden_core_resolver,
        raising=False,
    )
    monkeypatch.setattr(runtime_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        preferences,
        "save_global_conversation_settings",
        save_settings,
    )

    def build_candidate(_core_type, *, selection, **_kwargs):
        assert runtime._asr_provider == "soniox"
        built_selections.append(selection)
        assert selection is soniox_selection
        return sessions[len(built_selections) - 1]

    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        build_candidate,
    )

    await runtime._start_independent_asr_if_enabled("audio")

    sessions[0].close.assert_awaited_once_with()
    sessions[1].close.assert_awaited_once_with()
    sessions[2].close.assert_not_awaited()
    for session in sessions:
        session.connect.assert_awaited_once_with()
    primary_resolver.assert_called_once_with("gemini")
    forbidden_core_resolver.assert_not_called()
    save_settings.assert_not_called()
    assert built_selections == [soniox_selection] * 3
    assert [call.args for call in sleep.await_args_list] == [(0.25,), (0.5,)]
    assert runtime._asr_session is sessions[2]
    assert runtime._asr_provider == "soniox"
    assert runtime._asr_transport_selection is soniox_selection
    assert runtime._asr_lifecycle.provider_policy.endpoint_authority == "provider"
    assert runtime._asr_route_mode == "independent"
    assert "provider detail" not in str(runtime.send_status.await_args_list)
    assert "ASR_INDEPENDENT_PROVIDER_UNAVAILABLE" not in str(
        runtime.send_status.await_args_list
    )


async def test_soniox_connect_retries_exhausted_blocks_without_provider_fallback(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.session.stream_audio = AsyncMock()
    soniox_selection = _selection("soniox", "provider")
    forbidden_core_resolver = MagicMock(
        side_effect=AssertionError("Soniox recovery must not resolve another provider")
    )
    sleep = AsyncMock()
    sessions = []
    for attempt in range(3):
        session = type("Soniox", (), {})()
        session.connect = AsyncMock(
            side_effect=RuntimeError(
                f"ASR_SONIOX_CONNECT_{attempt}: private provider detail {attempt}"
            )
        )
        session.close = AsyncMock()
        sessions.append(session)
    built_selections = []

    def create_candidate(_core_type, *, selection, **_kwargs):
        built_selections.append(selection)
        assert selection is soniox_selection
        return sessions[len(built_selections) - 1]

    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=soniox_selection),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_core_follow_selection",
        forbidden_core_resolver,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        create_candidate,
    )
    monkeypatch.setattr(runtime_module.asyncio, "sleep", sleep)

    await runtime._start_independent_asr_if_enabled("audio")

    consumed = await runtime._route_microphone_audio(
        b"\x00\x00",
        sample_rate_hz=16_000,
    )
    for session in sessions:
        session.connect.assert_awaited_once_with()
        session.close.assert_awaited_once_with()
    forbidden_core_resolver.assert_not_called()
    assert built_selections == [soniox_selection] * 3
    assert [call.args for call in sleep.await_args_list] == [(0.25,), (0.5,)]
    assert runtime._asr_session is None
    assert runtime._asr_provider is None
    assert runtime._asr_route_mode == "blocked"
    assert consumed is True
    runtime.session.stream_audio.assert_not_awaited()
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    terminal = statuses[-1]
    assert terminal["code"] == "ASR_INDEPENDENT_PROVIDER_UNAVAILABLE"
    assert terminal["details"]["provider"] == "soniox"
    assert terminal["details"]["session_epoch"] == runtime._asr_session_epoch
    assert terminal["details"]["reason_code"] == "ASR_SONIOX_CONNECT_2"
    incident_id = terminal["details"]["incident_id"]
    assert incident_id.startswith("asr-failure-")
    assert len(incident_id) == len("asr-failure-") + 32
    assert all(character in "0123456789abcdef" for character in incident_id[-32:])
    assert "private provider detail" not in str(runtime.send_status.await_args_list)


async def test_single_connect_failure_reports_safe_reason_without_lifecycle(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    selection = _selection("qwen", "manual")
    session = SimpleNamespace(
        connect=AsyncMock(
            side_effect=RuntimeError(
                "ASR_QWEN_PROVIDER_ERROR: https://provider.invalid?api_key=secret"
            )
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=selection),
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_create_asr_session_from_selection",
        MagicMock(return_value=session),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    payloads = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    assert [payload["code"] for payload in payloads] == ["ASR_INDEPENDENT_FAILED"]
    terminal = payloads[0]
    assert terminal["details"]["reason_code"] == "ASR_QWEN_PROVIDER_ERROR"
    assert terminal["details"]["incident_id"].startswith("asr-failure-")
    assert "provider.invalid" not in str(runtime.send_status.await_args_list)
    assert "secret" not in str(runtime.send_status.await_args_list)
    assert runtime._asr_route_mode == "blocked"
    session.close.assert_awaited_once_with()


async def test_failed_soniox_candidate_cannot_invalidate_successful_successor(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.websocket = type("WebSocket", (), {"send_json": AsyncMock()})()
    callbacks: list[dict[str, object]] = []

    failed_session = type("Soniox", (), {})()
    failed_session.connect = AsyncMock(side_effect=RuntimeError("provider detail"))
    failed_session.close = AsyncMock()
    successful_session = type("Soniox", (), {})()
    successful_session.connect = AsyncMock()
    successful_session.close = AsyncMock()
    soniox_selection = _selection("soniox", "provider")
    sessions = [failed_session, successful_session]

    def capture_partial(session, callback) -> None:
        session.partial_callback = callback

    def create_candidate(_core_type, *, selection, **kwargs):
        assert selection is soniox_selection
        callbacks.append(kwargs)
        return sessions[len(callbacks) - 1]

    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=soniox_selection),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_core_follow_selection",
        MagicMock(
            side_effect=AssertionError(
                "Soniox recovery must not resolve another provider"
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        create_candidate,
    )
    monkeypatch.setattr(runtime_module, "_attach_partial_callback", capture_partial)
    monkeypatch.setattr(runtime_module.asyncio, "sleep", AsyncMock())

    await runtime._start_independent_asr_if_enabled("audio")
    adopted_epoch = runtime._asr_session_epoch

    await callbacks[0]["on_input_transcript"]("late soniox final")
    await callbacks[0]["on_speech_activity"](SpeechActivityEvent.SPEECH_STARTED)
    await failed_session.partial_callback("late soniox preview")
    await callbacks[0]["on_connection_error"]("late soniox error")
    await asyncio.sleep(0)

    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.handle_interruption.assert_not_awaited()
    runtime.handle_new_message.assert_not_awaited()
    runtime.websocket.send_json.assert_not_awaited()
    successful_session.close.assert_not_awaited()
    assert runtime._asr_session is successful_session
    assert runtime._asr_provider == "soniox"
    assert runtime._asr_route_mode == "independent"
    assert runtime._asr_session_epoch == adopted_epoch


async def test_adopted_start_activity_callback_survives_idle_audio_generation_bump(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, detector = (
        await _start_runtime_with_callback_candidates(
            monkeypatch,
            candidate_count=1,
        )
    )
    component = runtime._asr_runtime
    original_audio_generation = component._asr_audio_generation
    current_ingress = runtime._capture_ingress_token()
    component._asr_current_ingress_token = current_ingress

    await component._handle_audio_ingress_backpressure(current_ingress)

    assert component._asr_audio_generation == original_audio_generation + 1
    assert component._asr_session is sessions[0]
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    detector.reset.assert_awaited_once_with()

    updated_ingress = runtime._capture_ingress_token()
    component._asr_current_ingress_token = updated_ingress
    on_activity = callbacks[0]["on_speech_activity"]
    assert callable(on_activity)
    await on_activity(SpeechActivityEvent.SPEECH_STARTED)

    assert component._asr_current_ingress_token == updated_ingress
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    sessions[0].close.assert_not_awaited()


async def test_reconnected_start_callback_survives_abort_start_generation_change(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, _detector = (
        await _start_runtime_with_callback_candidates(monkeypatch)
    )
    component = runtime._asr_runtime
    original_start_generation = component._asr_start_generation

    await component.abort("hard_mute")

    assert component._asr_start_generation > original_start_generation
    assert component._asr_session is None
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    sessions[0].close.assert_awaited_once_with()

    await component._restart_transport(max_attempts=1)

    assert len(callbacks) == 2
    assert component._asr_session is sessions[1]
    updated_ingress = runtime._capture_ingress_token()
    component._asr_current_ingress_token = updated_ingress
    old_activity = callbacks[0]["on_speech_activity"]
    new_activity = callbacks[1]["on_speech_activity"]
    assert callable(old_activity)
    assert callable(new_activity)

    await old_activity(SpeechActivityEvent.SPEECH_STARTED)

    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN

    await new_activity(SpeechActivityEvent.SPEECH_STARTED)

    assert component._asr_current_ingress_token == updated_ingress
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    sessions[1].close.assert_not_awaited()


async def test_restart_closes_not_ready_session_before_replacement() -> None:
    runtime = _Runtime()
    events: list[str] = []

    async def close_old() -> None:
        events.append("old.close")

    async def connect_new() -> None:
        events.append("new.connect")

    old_session = SimpleNamespace(
        is_ready=False,
        close=AsyncMock(side_effect=close_old),
    )
    candidate = SimpleNamespace(
        is_ready=True,
        connect=AsyncMock(side_effect=connect_new),
        close=AsyncMock(),
    )
    runtime._asr_session = old_session
    _install_ready_lifecycle(runtime, "qwen")
    runtime._asr_session_factory = MagicMock(return_value=candidate)
    runtime._asr_transport_selection = _selection("qwen")

    await runtime._restart_transport(max_attempts=1)

    assert events == ["old.close", "new.connect"]
    old_session.close.assert_awaited_once_with()
    candidate.connect.assert_awaited_once_with()
    candidate.close.assert_not_awaited()
    assert runtime._asr_session is candidate
    runtime._asr_detector.reset_provider_audio_timeline.assert_not_awaited()
    assert runtime._asr_provider_exact_session is None


async def test_transport_restart_reuses_provider_key_in_fresh_physical_namespace(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, detector = (
        await _start_runtime_with_callback_candidates(monkeypatch)
    )
    component = runtime._asr_runtime
    component._asr_current_ingress_token = runtime._capture_ingress_token()
    key = ProviderUtteranceKey(0, 0, 1)
    ordered = ProviderEndpointNotification(
        phase="ordered",
        generation=key.generation,
        buffer_epoch=key.buffer_epoch,
        utterance_id=key.utterance_id,
        boundary_quality="unknown",
        audio_range=None,
    )

    await callbacks[0]["on_speech_activity"](SpeechActivityEvent.SPEECH_STARTED)
    await callbacks[0]["on_provider_endpoint"](ordered)
    await callbacks[0]["on_provider_final"](key, "first")
    await runtime._wait_asr_transcript_dispatch_idle()
    assert key in component._asr_completed_provider_keys

    await component._close_transport_only()
    assert component._asr_provider_exact_session is None
    events: list[str] = []

    async def connect_replacement() -> None:
        events.append("connect")

    async def reset_provider_timeline() -> bool:
        assert component._asr_session is None
        events.append("reset")
        return True

    sessions[1].connect.side_effect = connect_replacement
    detector.reset_provider_audio_timeline.side_effect = reset_provider_timeline

    await component._restart_transport(max_attempts=1)

    assert events == ["connect", "reset"]
    assert component._asr_session is sessions[1]
    assert component._asr_provider_exact_session is sessions[1]
    assert key not in component._asr_completed_provider_keys
    await callbacks[1]["on_speech_activity"](SpeechActivityEvent.SPEECH_STARTED)
    await callbacks[1]["on_provider_endpoint"](ordered)
    await callbacks[1]["on_provider_final"](key, "second")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0]
        for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second"]


@pytest.mark.parametrize("reset_outcome", ["missing", "false", "error"])
async def test_transport_restart_timeline_reset_failure_disables_only_exact_speaker(
    monkeypatch,
    reset_outcome: str,
) -> None:
    runtime, sessions, callbacks, detector = (
        await _start_runtime_with_callback_candidates(monkeypatch)
    )
    component = runtime._asr_runtime
    component._asr_current_ingress_token = runtime._capture_ingress_token()
    key = ProviderUtteranceKey(0, 0, 1)
    unknown = ProviderEndpointNotification(
        phase="ordered",
        generation=key.generation,
        buffer_epoch=key.buffer_epoch,
        utterance_id=key.utterance_id,
        boundary_quality="unknown",
        audio_range=None,
    )
    await callbacks[0]["on_speech_activity"](SpeechActivityEvent.SPEECH_STARTED)
    await callbacks[0]["on_provider_endpoint"](unknown)
    await callbacks[0]["on_provider_final"](key, "first")
    await runtime._wait_asr_transcript_dispatch_idle()
    await component._close_transport_only()

    if reset_outcome == "missing":
        del detector.reset_provider_audio_timeline
    elif reset_outcome == "error":
        detector.reset_provider_audio_timeline.side_effect = RuntimeError(
            "speaker reset failed"
        )
    else:
        detector.reset_provider_audio_timeline.return_value = False
    await component._restart_transport(max_attempts=1)

    assert component._asr_session is sessions[1]
    assert component._asr_provider_exact_session is None
    assert key not in component._asr_completed_provider_keys
    exact_boundary = ProviderEndpointNotification(
        phase="boundary",
        generation=key.generation,
        buffer_epoch=key.buffer_epoch,
        utterance_id=key.utterance_id,
        boundary_quality="exact",
        audio_range=ProviderAudioRange(0, 160),
    )
    await callbacks[1]["on_speech_activity"](SpeechActivityEvent.SPEECH_STARTED)
    await callbacks[1]["on_provider_endpoint"](exact_boundary)
    await callbacks[1]["on_provider_endpoint"](
        replace(exact_boundary, phase="ordered")
    )
    await callbacks[1]["on_provider_final"](key, "second")
    await runtime._wait_asr_transcript_dispatch_idle()

    detector.reconcile_provider_endpoint.assert_not_awaited()
    assert [
        call.args[0]
        for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second"]


def _install_failing_restart_candidates(
    runtime: _Runtime,
    provider: str,
    *,
    failure_count: int,
) -> list[SimpleNamespace]:
    runtime._asr_session = SimpleNamespace(is_ready=False, close=AsyncMock())
    _install_ready_lifecycle(runtime, provider)
    candidates: list[SimpleNamespace] = []

    def build_candidate(_selection):
        candidate = SimpleNamespace(
            is_ready=True,
            connect=AsyncMock(
                side_effect=RuntimeError("private restart connect detail")
            ),
            close=AsyncMock(),
        )
        candidates.append(candidate)
        assert len(candidates) <= failure_count
        return candidate

    runtime._asr_session_factory = MagicMock(side_effect=build_candidate)
    runtime._asr_transport_selection = _selection(provider)
    return candidates


async def test_restart_default_attempts_follow_single_attempt_policy(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    sleep = AsyncMock()
    monkeypatch.setattr(runtime_module.asyncio, "sleep", sleep)
    candidates = _install_failing_restart_candidates(runtime, "qwen", failure_count=1)
    assert runtime._asr_lifecycle.provider_policy.connect_max_attempts == 1

    await runtime._restart_transport()
    while runtime._asr_runtime._asr_close_tasks:
        await asyncio.gather(
            *tuple(runtime._asr_runtime._asr_close_tasks),
            return_exceptions=True,
        )

    assert len(candidates) == 1
    candidates[0].connect.assert_awaited_once_with()
    candidates[0].close.assert_awaited_once_with()
    sleep.assert_not_awaited()
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    assert statuses[-1]["code"] == "ASR_INDEPENDENT_FAILED"
    assert "private restart connect detail" not in str(
        runtime.send_status.await_args_list
    )


async def test_restart_default_attempts_follow_soniox_policy_ladder(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    sleep = AsyncMock()
    monkeypatch.setattr(runtime_module.asyncio, "sleep", sleep)
    candidates = _install_failing_restart_candidates(
        runtime, "soniox", failure_count=3
    )
    assert runtime._asr_lifecycle.provider_policy.connect_max_attempts == 3

    await runtime._restart_transport()
    while runtime._asr_runtime._asr_close_tasks:
        await asyncio.gather(
            *tuple(runtime._asr_runtime._asr_close_tasks),
            return_exceptions=True,
        )

    assert len(candidates) == 3
    for candidate in candidates:
        candidate.connect.assert_awaited_once_with()
        candidate.close.assert_awaited_once_with()
    assert [call.args for call in sleep.await_args_list] == [(0.25,), (0.5,)]
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    assert statuses[-1]["code"] == "ASR_INDEPENDENT_FAILED"


async def test_restart_explicit_attempt_override_beats_policy(monkeypatch) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    sleep = AsyncMock()
    monkeypatch.setattr(runtime_module.asyncio, "sleep", sleep)
    candidates = _install_failing_restart_candidates(
        runtime, "soniox", failure_count=1
    )

    await runtime._restart_transport(max_attempts=1)
    while runtime._asr_runtime._asr_close_tasks:
        await asyncio.gather(
            *tuple(runtime._asr_runtime._asr_close_tasks),
            return_exceptions=True,
        )

    assert len(candidates) == 1
    candidates[0].connect.assert_awaited_once_with()
    sleep.assert_not_awaited()


async def test_restart_rejects_non_positive_attempt_override() -> None:
    runtime = _Runtime()

    with pytest.raises(ValueError, match="max_attempts must be positive"):
        await runtime._restart_transport(max_attempts=0)


async def test_not_ready_close_cannot_overwrite_replacement_generation() -> None:
    runtime = _Runtime()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def close_old() -> None:
        close_started.set()
        await release_close.wait()

    old_session = SimpleNamespace(
        is_ready=False,
        close=AsyncMock(side_effect=close_old),
    )
    old_factory = MagicMock()
    runtime._asr_session = old_session
    _install_ready_lifecycle(runtime, "qwen")
    runtime._asr_session_factory = old_factory
    runtime._asr_transport_selection = _selection("qwen")

    restarting = asyncio.create_task(runtime._restart_transport(max_attempts=1))
    await asyncio.wait_for(close_started.wait(), 1)
    assert runtime._asr_session is None

    new_session, new_lifecycle, new_detector = _install_replacement_runtime_generation(
        runtime, "qwen"
    )
    new_factory = object()
    new_selection = object()
    runtime._asr_session_factory = new_factory
    runtime._asr_transport_selection = new_selection
    release_close.set()
    await asyncio.wait_for(restarting, 1)

    old_session.close.assert_awaited_once_with()
    old_factory.assert_not_called()
    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert runtime._asr_session_factory is new_factory
    assert runtime._asr_transport_selection is new_selection
    new_session.close.assert_not_awaited()


async def test_adopted_restart_cancellation_fails_closed_and_propagates(
    monkeypatch,
) -> None:
    runtime, sessions, _callbacks, detector = (
        await _start_runtime_with_callback_candidates(monkeypatch)
    )
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    assert lifecycle is not None
    started_epoch = component._asr_session_epoch
    on_failure = AsyncMock(side_effect=component._callbacks.on_failure)
    component._callbacks = replace(component._callbacks, on_failure=on_failure)

    await component._close_transport_only()
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    component._asr_pending_speech_confirmed = True
    component._asr_current_ingress_token = runtime._capture_ingress_token()
    prepare_started = asyncio.Event()
    keep_preparing = asyncio.Event()

    async def block_prepare(_epoch: int) -> None:
        prepare_started.set()
        await keep_preparing.wait()

    component._prepare_independent_asr_turn = AsyncMock(side_effect=block_prepare)
    restarting = asyncio.create_task(component._restart_transport(max_attempts=3))
    await asyncio.wait_for(prepare_started.wait(), 1)
    assert component._asr_session is sessions[1]

    restarting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await restarting
    while component._asr_close_tasks:
        await asyncio.gather(
            *tuple(component._asr_close_tasks),
            return_exceptions=True,
        )

    on_failure.assert_awaited_once()
    failure = on_failure.await_args.args[0]
    assert failure.code == "ASR_INDEPENDENT_FAILED"
    assert failure.session_epoch == started_epoch + 1
    sessions[1].close.assert_awaited_once_with()
    detector.close.assert_awaited_once_with()
    assert component._asr_session is None
    assert component._asr_lifecycle is None
    assert component._asr_detector is None
    assert component._asr_session_factory is None
    assert component._asr_transport_selection is None
    assert component._asr_session_epoch == started_epoch + 1
    assert runtime._asr_route_mode == "blocked"
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    terminal = next(
        payload
        for payload in statuses
        if payload["code"] == "ASR_INDEPENDENT_FAILED"
    )
    assert terminal["details"]["provider"] == "qwen"
    assert terminal["details"]["session_epoch"] == started_epoch + 1
    assert terminal["details"]["reason_code"] == "ASR_INDEPENDENT_FAILED"
    assert terminal["details"]["incident_id"].startswith("asr-failure-")


async def test_adopted_restart_exception_fails_closed_without_retry(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, detector = (
        await _start_runtime_with_callback_candidates(monkeypatch)
    )
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    assert lifecycle is not None
    started_epoch = component._asr_session_epoch

    await component._close_transport_only()

    assert lifecycle.snapshot.state is VoiceLifecycleState.DEEP_SLEEP
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    assert lifecycle.snapshot.state is VoiceLifecycleState.PREWARMING
    component._asr_pending_speech_confirmed = True
    component._asr_current_ingress_token = runtime._capture_ingress_token()
    component._prepare_independent_asr_turn = AsyncMock(
        side_effect=RuntimeError("post-adoption recovery failed")
    )

    await component._restart_transport(max_attempts=3)
    while component._asr_close_tasks:
        await asyncio.gather(
            *tuple(component._asr_close_tasks),
            return_exceptions=True,
        )

    assert len(callbacks) == 2
    sessions[0].close.assert_awaited_once_with()
    sessions[1].connect.assert_awaited_once_with()
    sessions[1].close.assert_awaited_once_with()
    component._prepare_independent_asr_turn.assert_awaited_once_with(started_epoch)
    detector.close.assert_awaited_once_with()
    assert component._asr_session is None
    assert component._asr_lifecycle is None
    assert component._asr_detector is None
    assert component._asr_session_factory is None
    assert component._asr_transport_selection is None
    assert component._asr_session_epoch == started_epoch + 1
    assert runtime._asr_route_mode == "blocked"
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    terminal = next(
        payload
        for payload in statuses
        if payload["code"] == "ASR_INDEPENDENT_FAILED"
    )
    assert terminal["details"]["provider"] == "qwen"
    assert terminal["details"]["session_epoch"] == started_epoch + 1
    assert terminal["details"]["reason_code"] == "ASR_INDEPENDENT_FAILED"
    assert terminal["details"]["incident_id"].startswith("asr-failure-")


async def test_selection_failure_is_reported_without_escaping_session_start(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(side_effect=ValueError("invalid provider configuration")),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "blocked"
    assert not hasattr(runtime._asr_runtime, "_asr_required")
    assert runtime._asr_session is None
    assert runtime._asr_provider is None
    assert "ASR_INDEPENDENT_FAILED" in runtime.send_status.await_args.args[0]
    assert "invalid provider configuration" not in str(
        runtime.send_status.await_args_list
    )


async def test_selection_failure_during_core_change_stays_blocked(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.input_mode = "audio"
    runtime._independent_asr_route_key = "openai"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(side_effect=ValueError("invalid region configuration")),
    )

    await runtime._reconcile_independent_asr_after_core_change()

    assert runtime._independent_asr_route_key == "gemini"
    assert runtime._asr_route_mode == "blocked"
    assert runtime._asr_session is None
    assert "ASR_INDEPENDENT_FAILED" in runtime.send_status.await_args.args[0]


@pytest.mark.parametrize("core_type", ["qwen", "qwen_intl"])
async def test_qwen_core_starts_independent_asr_with_external_turn_support(
    monkeypatch,
    core_type: str,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = core_type
    runtime.session.set_visual_delivery_mode = MagicMock()
    runtime.session.block_raw_visual_delivery = MagicMock()
    asr = type("Asr", (), {})()

    async def connect_after_visual_fail_closed() -> None:
        delivered_modes = [
            getattr(call.args[0], "value", call.args[0])
            for call in runtime.session.set_visual_delivery_mode.call_args_list
        ]
        assert "external_description" not in delivered_modes
        runtime.session.block_raw_visual_delivery.assert_called()

    asr.connect = AsyncMock(side_effect=connect_after_visual_fail_closed)
    asr.close = AsyncMock()
    factory = MagicMock(return_value=asr)
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=_selection("qwen")),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        factory,
    )

    await runtime._start_independent_asr_if_enabled("audio")

    factory.assert_called_once()
    asr.connect.assert_awaited_once_with()
    assert runtime._asr_route_mode == "independent"
    assert runtime._asr_session is asr
    assert runtime._asr_provider == "qwen"


async def test_stale_settings_failure_cannot_refence_replacement_session(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    settings_read_started = asyncio.Event()
    release_stale_read = asyncio.Event()
    read_count = 0

    async def load_settings(*, strict: bool = False) -> dict:
        nonlocal read_count
        assert strict is True
        read_count += 1
        if read_count == 1:
            settings_read_started.set()
            await release_stale_read.wait()
            raise OSError("stale settings read failed")
        return {"independentAsrEnabled": False}

    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        load_settings,
    )
    stale_start = asyncio.create_task(
        runtime._start_independent_asr_if_enabled(
            "audio",
            handshake_override=True,
        )
    )
    await settings_read_started.wait()

    replacement_session = MagicMock()
    replacement_session.set_visual_delivery_mode = MagicMock()
    replacement_session.block_raw_visual_delivery = MagicMock()
    runtime.session = replacement_session
    runtime.core_api_type = "gemini"
    await runtime._start_independent_asr_if_enabled(
        "audio",
        handshake_override=False,
    )
    assert runtime._asr_route_mode == "native"

    release_stale_read.set()
    await stale_start

    delivered_modes = [
        getattr(call.args[0], "value", call.args[0])
        for call in replacement_session.set_visual_delivery_mode.call_args_list
    ]
    assert delivered_modes
    assert set(delivered_modes) == {"native"}


async def test_websocket_core_submits_one_external_turn_after_local_history() -> None:
    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    runtime.session.submit_external_voice_turn = AsyncMock()
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "qwen")

    await runtime._handle_independent_asr_final(" hello ", epoch, "qwen")
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.handle_input_transcript.assert_awaited_once_with(
        "hello",
        is_voice_source=True,
        source="independent_asr",
        metadata={"provider": "qwen"},
        source_game_route_identity=None,
    )
    runtime.session.submit_external_voice_turn.assert_awaited_once()
    call = runtime.session.submit_external_voice_turn.await_args
    assert call.args == ("hello",)
    assert call.kwargs["turn_id"].startswith("asr-")
    runtime.session.create_response.assert_not_awaited()


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("observer_raises", [False, True])
async def test_audio_activation_observes_before_dispatch_and_retires_rejected_payload(
    accepted: bool,
    observer_raises: bool,
) -> None:
    runtime = _Runtime()
    session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = session
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    detector = component._asr_detector
    assert lifecycle is not None
    assert isinstance(detector, _ReadyDetector)
    if observer_raises:
        detector.observe_provider_audio.side_effect = RuntimeError("observer failed")
    token = component._capture_turn_token(lifecycle)
    epoch = component._asr_session_epoch
    payload = b"\x01\x00" * 320

    def accept_after_observation(*_args, **_kwargs):
        detector.observe_provider_audio.assert_called_once()
        return accepted

    activate = MagicMock(side_effect=accept_after_observation)
    abort = MagicMock()
    component._asr_audio_dispatcher = SimpleNamespace(
        active_turn=None,
        activate=activate,
        abort=abort,
        close=AsyncMock(),
    )

    result = await component._activate_asr_audio_dispatcher(
        lifecycle,
        token,
        buffered_pcm16=payload,
    )

    assert result is accepted
    activate.assert_called_once()
    assert activate.call_args.args[2] is payload
    detector.observe_provider_audio.assert_called_once()
    assert detector.observe_provider_audio.call_args.args[0] is payload
    assert detector.observe_provider_audio.call_args.kwargs == {
        "sample_rate_hz": 16_000,
    }
    if accepted:
        abort.assert_not_called()
        session.close.assert_not_awaited()
        assert component._asr_session is session
        assert component._asr_session_epoch == epoch
    else:
        # Ordered observation may already have committed its sample positions;
        # enqueue refusal retires that physical timeline instead of replaying it.
        abort.assert_called()
        session.close.assert_awaited_once()
        assert component._asr_session is None
        assert component._asr_session_epoch > epoch


async def test_buffered_resume_spans_preserve_pcm_boundary() -> None:
    runtime = _Runtime()
    session = SimpleNamespace(
        is_ready=True,
        stream_audio=AsyncMock(),
        close=AsyncMock(),
        signal_user_activity_end=AsyncMock(),
    )
    runtime._asr_session = session
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    detector = component._asr_detector
    ingress = component._asr_current_ingress_token
    assert lifecycle is not None
    assert isinstance(detector, _ReadyDetector)
    assert ingress is not None
    token = component._capture_turn_token(lifecycle)
    predecessor_identity = DetectorIngressIdentity(
        ingress_token=ingress,
        detector_epoch=1,
        sequence_no=18,
    )
    successor_identity = DetectorIngressIdentity(
        ingress_token=ingress,
        detector_epoch=1,
        sequence_no=19,
    )
    predecessor_pcm = b"\x01\x00" * 160
    successor_pcm = b"\x02\x00" * 160
    payload = predecessor_pcm + successor_pcm
    component._record_buffered_provider_speaker_observation(
        identity=predecessor_identity,
        byte_count=len(predecessor_pcm),
        split_before_audio=False,
        evidence_complete=True,
    )
    component._record_buffered_provider_speaker_observation(
        identity=successor_identity,
        byte_count=len(successor_pcm),
        split_before_audio=True,
        evidence_complete=True,
    )
    component._asr_provider_speaker_sequence = 4

    assert await component._activate_asr_audio_dispatcher(
        lifecycle,
        token,
        buffered_pcm16=payload,
    )
    await component._asr_audio_dispatcher.wait_idle()

    assert detector.observe_provider_audio_ordered.await_args_list == [
        call(
            predecessor_pcm,
            sample_rate_hz=16_000,
            identity=predecessor_identity,
            sequence_no=5,
            split_before_audio=False,
            evidence_complete=True,
        ),
        call(
            successor_pcm,
            sample_rate_hz=16_000,
            identity=successor_identity,
            sequence_no=6,
            split_before_audio=True,
            evidence_complete=True,
        ),
    ]
    detector.observe_provider_audio.assert_not_called()
    session.stream_audio.assert_awaited_once_with(
        payload,
        sample_rate_hz=16_000,
    )
    await component._asr_audio_dispatcher.close()


async def _start_blocked_buffered_provider_span_replay() -> SimpleNamespace:
    runtime = _Runtime()
    provider_sent = asyncio.Event()

    async def stream_audio(_pcm16: bytes, *, sample_rate_hz: int) -> None:
        assert sample_rate_hz == 16_000
        provider_sent.set()

    session = SimpleNamespace(
        is_ready=True,
        stream_audio=AsyncMock(side_effect=stream_audio),
        close=AsyncMock(),
        signal_user_activity_end=AsyncMock(),
    )
    runtime._asr_session = session
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    detector = component._asr_detector
    ingress = component._asr_current_ingress_token
    assert lifecycle is not None
    assert isinstance(detector, _ReadyDetector)
    assert ingress is not None
    epoch = component._asr_session_epoch
    await component._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    turn_token = component._capture_turn_token(lifecycle)
    payload = b"\x31\x00" * 160
    identity = DetectorIngressIdentity(
        ingress_token=ingress,
        detector_epoch=detector.detector_epoch,
        sequence_no=1,
    )
    component._record_buffered_provider_speaker_observation(
        identity=identity,
        byte_count=len(payload),
        split_before_audio=False,
        evidence_complete=True,
    )
    observation_started = asyncio.Event()
    release_observation = asyncio.Event()
    observation_caught_up = asyncio.Event()
    wait_started = asyncio.Event()

    async def block_observation(*_args, **_kwargs) -> None:
        observation_started.set()
        await release_observation.wait()
        observation_caught_up.set()

    async def wait_observed_through(end_sample_16k: int) -> bool:
        assert end_sample_16k == 160
        wait_started.set()
        await observation_caught_up.wait()
        return True

    detector.observe_provider_audio_ordered.side_effect = block_observation
    detector.wait_provider_audio_observed_through.side_effect = (
        wait_observed_through
    )
    snapshot = ProviderSpeakerBoundarySnapshot(
        detector_epoch=detector.detector_epoch,
        candidate_generation=0,
        through_sequence_no=1,
        shadow_generation=1,
        merged_resume_count=0,
        successor_present=False,
        evidence_complete=True,
        _owner=object(),
    )
    detector.reconcile_provider_endpoint.return_value = snapshot
    activation = asyncio.create_task(
        component._activate_asr_audio_dispatcher(
            lifecycle,
            turn_token,
            buffered_pcm16=payload,
        )
    )
    await asyncio.wait_for(provider_sent.wait(), 1)
    await asyncio.wait_for(observation_started.wait(), 1)
    return SimpleNamespace(
        runtime=runtime,
        component=component,
        detector=detector,
        epoch=epoch,
        session=session,
        payload=payload,
        snapshot=snapshot,
        activation=activation,
        release_observation=release_observation,
        wait_started=wait_started,
    )


async def test_exact_boundary_waits_for_blocked_buffered_span_replay() -> None:
    state = await _start_blocked_buffered_provider_span_replay()
    key = ProviderUtteranceKey(0, 0, 1)
    boundary = ProviderEndpointNotification(
        phase="boundary",
        generation=key.generation,
        buffer_epoch=key.buffer_epoch,
        utterance_id=key.utterance_id,
        boundary_quality="exact",
        audio_range=ProviderAudioRange(0, 160),
    )
    boundary_task = asyncio.create_task(
        state.component._handle_provider_endpoint_notification(
            boundary,
            state.epoch,
        )
    )
    await asyncio.wait_for(state.wait_started.wait(), 1)
    state.detector.reconcile_provider_endpoint.assert_not_awaited()

    state.release_observation.set()
    assert await asyncio.wait_for(state.activation, 1) is True
    await asyncio.wait_for(boundary_task, 1)

    state.detector.reconcile_provider_endpoint.assert_awaited_once_with(
        boundary.audio_range
    )
    record = state.component._asr_provider_boundary_snapshots[key]
    assert record.notification.boundary_quality == "exact"
    assert record.snapshot is state.snapshot
    await state.component._asr_audio_dispatcher.close()


async def test_blocked_buffered_span_replay_times_out_unknown_without_losing_final() -> (
    None
):
    state = await _start_blocked_buffered_provider_span_replay()
    key = ProviderUtteranceKey(0, 0, 1)
    boundary = ProviderEndpointNotification(
        phase="boundary",
        generation=key.generation,
        buffer_epoch=key.buffer_epoch,
        utterance_id=key.utterance_id,
        boundary_quality="exact",
        audio_range=ProviderAudioRange(0, 160),
    )
    boundary_task = asyncio.create_task(
        state.component._handle_provider_endpoint_notification(
            boundary,
            state.epoch,
        )
    )
    await asyncio.wait_for(state.wait_started.wait(), 1)
    await asyncio.wait_for(boundary_task, 1)

    state.detector.reconcile_provider_endpoint.assert_not_awaited()
    record = state.component._asr_provider_boundary_snapshots[key]
    assert record.notification.boundary_quality == "unknown"
    await state.component._handle_provider_endpoint_notification(
        replace(boundary, phase="ordered"),
        state.epoch,
    )
    await state.component._handle_provider_final(
        key,
        "kept transcript",
        state.epoch,
        "openai",
    )
    await state.runtime._wait_asr_transcript_dispatch_idle()

    state.runtime.handle_input_transcript.assert_awaited_once_with(
        "kept transcript",
        is_voice_source=True,
        source="independent_asr",
        metadata={"provider": "openai"},
    )
    state.release_observation.set()
    assert await asyncio.wait_for(state.activation, 1) is True
    await state.component._asr_audio_dispatcher.close()


async def test_provider_speaker_sequence_remains_monotonic_across_turns() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    detector = component._asr_detector
    ingress = component._asr_current_ingress_token
    assert lifecycle is not None
    assert isinstance(detector, _ReadyDetector)
    assert ingress is not None
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    first_turn = component._capture_turn_token(lifecycle)
    first_identity = DetectorIngressIdentity(ingress, 1, 21)

    assert await component._observe_admitted_provider_audio(
        lifecycle,
        detector,
        b"\x03\x00" * 160,
        sample_rate_hz=16_000,
        identity=first_identity,
        split_before_audio=False,
        evidence_complete=True,
        turn_token=first_turn,
    )
    lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    second_turn = component._capture_turn_token(lifecycle)
    second_identity = DetectorIngressIdentity(ingress, 1, 22)

    assert second_turn.turn_id > first_turn.turn_id
    assert await component._observe_admitted_provider_audio(
        lifecycle,
        detector,
        b"\x04\x00" * 160,
        sample_rate_hz=16_000,
        identity=second_identity,
        split_before_audio=False,
        evidence_complete=True,
        turn_token=second_turn,
    )

    assert [
        observed.kwargs["sequence_no"]
        for observed in detector.observe_provider_audio_ordered.await_args_list
    ] == [1, 2]


async def test_partial_preview_is_display_only_and_epoch_guarded() -> None:
    runtime = _Runtime()
    websocket = type("WebSocket", (), {})()
    websocket.send_json = AsyncMock()
    runtime.websocket = websocket
    runtime.current_speech_id = "speech-current"
    runtime._set_microphone_route("independent")
    await _install_active_smart_turn(runtime)
    epoch = runtime._asr_session_epoch
    token = runtime._asr_runtime._asr_partial_turn_token
    assert token is not None
    assert await runtime._activate_asr_audio_dispatcher(
        runtime._asr_lifecycle,
        token,
    )

    await runtime._send_independent_asr_preview(" draft ", epoch)
    await runtime._send_independent_asr_preview("stale", epoch + 1)

    websocket.send_json.assert_awaited_once_with(
        {
            "type": "user_transcript_preview",
            "text": "draft",
            "turn_id": "speech-current",
            "asr_turn_id": f"asr-{epoch}-1",
        }
    )
    runtime.handle_input_transcript.assert_not_awaited()


async def test_partial_preview_keeps_prepared_token_and_rejects_after_abort() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    on_partial = AsyncMock()
    runtime._asr_runtime._callbacks = replace(
        runtime._asr_runtime._callbacks,
        on_partial=on_partial,
    )
    await _install_active_smart_turn(runtime)
    epoch = runtime._asr_session_epoch
    captured_token = runtime._asr_runtime._asr_partial_turn_token
    assert captured_token is not None
    assert await runtime._activate_asr_audio_dispatcher(
        runtime._asr_lifecycle,
        captured_token,
    )

    await runtime._send_independent_asr_preview("current", epoch)

    event = on_partial.await_args.args[0]
    assert event.turn_token is captured_token
    assert event.session_epoch == epoch
    on_partial.reset_mock()

    runtime._asr_audio_dispatcher.abort(captured_token)
    await runtime._send_independent_asr_preview("late", epoch)

    on_partial.assert_not_awaited()


async def test_start_failure_blocks_omni_without_leaking_error(monkeypatch) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "glm"
    asr = type("Asr", (), {})()
    asr.connect = AsyncMock(side_effect=RuntimeError("secret provider response"))
    asr.close = AsyncMock()
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=_selection("glm")),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        MagicMock(return_value=asr),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "blocked"
    assert runtime._asr_session is None
    assert "secret provider response" not in str(runtime.send_status.await_args)


async def test_builder_failure_stays_blocked_and_never_sends_audio_to_omni(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.session.stream_audio = AsyncMock()
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=_selection("gemini")),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        MagicMock(side_effect=RuntimeError("private provider detail")),
    )

    await runtime._start_independent_asr_if_enabled("audio")
    consumed = await runtime._route_microphone_audio(
        b"\x00\x00",
        sample_rate_hz=16_000,
    )
    if not consumed:
        await runtime.session.stream_audio(b"\x00\x00")

    assert runtime._asr_route_mode == "blocked"
    assert runtime._asr_session is None
    assert consumed is True
    runtime.session.stream_audio.assert_not_awaited()
    assert "private provider detail" not in str(runtime.send_status.await_args)


async def test_hot_swap_reuses_matching_asr_provider() -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.input_mode = "audio"
    runtime._asr_route_mode = "independent"
    runtime._asr_provider = "gemini"
    runtime._independent_asr_route_key = "gemini"
    runtime._start_independent_asr_if_enabled = AsyncMock()

    await runtime._reconcile_independent_asr_after_core_change()

    runtime._start_independent_asr_if_enabled.assert_not_awaited()


async def test_hot_swap_replaces_asr_before_cached_audio_for_new_core() -> None:
    runtime = _Runtime()
    runtime.core_api_type = "glm"
    runtime.input_mode = "audio"
    runtime._asr_route_mode = "independent"
    runtime._asr_provider = "gemini"
    runtime._independent_asr_route_key = "gemini"
    runtime._start_independent_asr_if_enabled = AsyncMock()

    await runtime._reconcile_independent_asr_after_core_change()

    runtime._start_independent_asr_if_enabled.assert_awaited_once_with(
        "audio",
        preserve_hot_swap_audio=True,
    )


@pytest.mark.parametrize("core_type", ["openai", "glm", "gemini"])
async def test_hot_swap_starts_independent_asr_after_core_route_change(
    core_type: str,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = core_type
    runtime.input_mode = "audio"
    runtime._asr_route_mode = "blocked"
    runtime._independent_asr_route_key = "free"
    runtime._start_independent_asr_if_enabled = AsyncMock()

    await runtime._reconcile_independent_asr_after_core_change()

    runtime._start_independent_asr_if_enabled.assert_awaited_once_with(
        "audio",
        preserve_hot_swap_audio=True,
    )


async def test_hot_swap_does_not_retry_failed_same_core_route() -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.input_mode = "audio"
    runtime._asr_route_mode = "blocked"
    runtime._independent_asr_route_key = "gemini"
    runtime._start_independent_asr_if_enabled = AsyncMock()

    await runtime._reconcile_independent_asr_after_core_change()

    runtime._start_independent_asr_if_enabled.assert_not_awaited()


@pytest.mark.parametrize("route_mode", ["independent", "native"])
async def test_same_core_session_promotion_resyncs_visual_delivery_mode(
    route_mode: str,
) -> None:
    """A promoted session inherits the live route even when provider key is unchanged."""
    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    runtime.input_mode = "audio"
    runtime._asr_route_mode = route_mode
    runtime._independent_asr_route_key = "qwen"
    runtime._start_independent_asr_if_enabled = AsyncMock()
    replacement_session = type("ReplacementOmni", (), {})()
    replacement_session._supports_native_image = True
    replacement_session.set_visual_delivery_mode = MagicMock()
    replacement_session.block_raw_visual_delivery = MagicMock()
    runtime.session = replacement_session

    await runtime._reconcile_independent_asr_after_core_change()

    if route_mode == "independent":
        replacement_session.set_visual_delivery_mode.assert_not_called()
        replacement_session.block_raw_visual_delivery.assert_called_once_with()
    else:
        replacement_session.set_visual_delivery_mode.assert_called_once_with("native")
    runtime._start_independent_asr_if_enabled.assert_not_awaited()


async def test_blocked_replacement_session_preserves_external_visual_policy_and_fence() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    runtime._set_microphone_route("blocked")
    replacement_session = type("ReplacementOmni", (), {})()
    replacement_session.set_visual_delivery_mode = MagicMock()
    replacement_session.block_raw_visual_delivery = MagicMock()
    runtime.session = replacement_session

    runtime._set_microphone_route("blocked")

    replacement_session.set_visual_delivery_mode.assert_not_called()
    replacement_session.block_raw_visual_delivery.assert_called()


async def test_native_to_blocked_fences_raw_frames_during_route_reconciliation() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("native")
    replacement_session = type("ReplacementOmni", (), {})()
    replacement_session.set_visual_delivery_mode = MagicMock()
    replacement_session.block_raw_visual_delivery = MagicMock()
    runtime.session = replacement_session

    runtime._set_microphone_route("blocked")

    replacement_session.set_visual_delivery_mode.assert_called_once_with("native")
    replacement_session.block_raw_visual_delivery.assert_called_once_with()


async def test_disabled_native_route_key_prevents_same_core_reconcile(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.input_mode = "audio"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": False}),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "native"
    assert runtime._independent_asr_route_key == "gemini"
    runtime.is_active = True
    runtime.is_hot_swap_imminent = True
    runtime.session.stream_audio = AsyncMock()
    old_token = runtime._capture_ingress_token()
    assert runtime.hot_swap_audio_cache.append(
        _HotSwapAudioFrame(
            pcm16=b"\x01\x00" * 160,
            token=old_token,
            audio_stream_epoch=runtime._audio_stream_epoch,
        )
    )
    runtime._set_microphone_route("blocked")
    runtime._set_microphone_route("native")
    runtime._start_independent_asr_if_enabled = AsyncMock()
    await runtime._reconcile_independent_asr_after_core_change()
    runtime._start_independent_asr_if_enabled.assert_not_awaited()
    await runtime._flush_hot_swap_audio_cache()
    runtime.session.stream_audio.assert_awaited_once_with(b"\x01\x00" * 160)
    assert runtime._omni_mic_audio_bytes == 320


async def test_start_session_handshake_true_overrides_persisted_disabled(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": False}),
    )
    start_mock = AsyncMock(
        return_value=AsrStartResult(
            status=AsrStartStatus.FAILED,
            failure_code="ASR_START_STALE",
        )
    )
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    runtime.set_independent_asr_handshake(True)
    await runtime._start_independent_asr_if_enabled("audio")

    # The handshake beats the stale persisted value: the independent runtime
    # start is attempted instead of the native fallback.
    start_mock.assert_awaited_once()
    assert runtime._asr_route_mode != "native"


async def test_start_session_handshake_false_overrides_persisted_enabled(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    start_mock = AsyncMock()
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    runtime.set_independent_asr_handshake(False)
    await runtime._start_independent_asr_if_enabled("audio")

    start_mock.assert_not_awaited()
    assert runtime._asr_route_mode == "native"


async def test_resource_optimization_handshake_false_overrides_persisted_enabled(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(
            return_value={
                "independentAsrEnabled": True,
                "voiceInputResourceOptimizationEnabled": True,
            }
        ),
    )
    start_mock = AsyncMock(
        return_value=AsrStartResult(
            status=AsrStartStatus.FAILED,
            failure_code="ASR_START_STALE",
        )
    )
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    runtime.set_voice_input_resource_optimization_handshake(False)
    await runtime._start_independent_asr_if_enabled("audio")

    assert start_mock.await_args.kwargs["resource_optimization_enabled"] is False
    assert runtime._speaker_shadow_factory is None
    assert "speaker_shadow_factory" not in start_mock.await_args.kwargs


async def test_core_passes_only_configured_speaker_shadow_factory(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    factory = MagicMock()
    runtime._speaker_shadow_factory = factory
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    start_mock = AsyncMock(
        return_value=AsrStartResult(
            status=AsrStartStatus.FAILED,
            failure_code="ASR_START_STALE",
        )
    )
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    await runtime._start_independent_asr_if_enabled("audio")

    # A previous route's factory is not a rebuildable desired configuration.
    assert "speaker_shadow_factory" not in start_mock.await_args.kwargs
    assert runtime._speaker_shadow_factory is None
    factory.assert_not_called()


async def test_failed_independent_start_preserves_external_visual_route_memory(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.session.set_visual_delivery_mode = MagicMock()
    runtime.session.block_raw_visual_delivery = MagicMock()
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    start_mock = AsyncMock(
        return_value=AsrStartResult(
            status=AsrStartStatus.FAILED,
            failure_code="ASR_CONNECT_FAILED",
        )
    )
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "blocked"
    assert runtime._visual_route_mode == "independent"
    runtime.session.block_raw_visual_delivery.assert_called()


async def test_connect_budget_does_not_block_a_free_native_route(
    monkeypatch,
) -> None:
    # Codex P2. The budget bounds the PROVIDER CONNECT, nothing else. A request
    # whose handshake disables independent ASR settles on native without talking
    # to anyone, so refusing it over a connect budget would leave the route on
    # its blocked placeholder and abort a microphone start that had nothing to
    # wait for.
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.input_mode = "audio"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    start_mock = AsyncMock()
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    await runtime._start_independent_asr_if_enabled(
        "audio",
        handshake_override=False,
        connect_budget_seconds=0.0,
    )

    assert runtime._asr_route_mode == "native"
    start_mock.assert_not_awaited()


async def test_connect_budget_stops_a_connect_it_cannot_finish(
    monkeypatch,
) -> None:
    # The other half: independent ASR IS wanted, so the decision would connect --
    # and a verdict produced after the frontend's deadline is worse than none,
    # because the client's timeout tears down the session that did start. Leave
    # the route on the blocked placeholder, which is what the caller would have
    # re-acked without re-deciding at all.
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.input_mode = "audio"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    start_mock = AsyncMock()
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    await runtime._start_independent_asr_if_enabled(
        "audio",
        handshake_override=True,
        connect_budget_seconds=0.0,
    )

    assert runtime._asr_route_mode == "blocked"
    start_mock.assert_not_awaited()


async def test_connect_budget_is_opt_in(monkeypatch) -> None:
    # Every other caller (hot-swap, device change, the ordinary start) passes no
    # budget and must keep connecting exactly as before.
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.input_mode = "audio"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    async def _ready(**_kwargs):
        # Epoch read at call time: the teardown that precedes the connect bumps
        # it, and a result stamped with the pre-call value reads as stale.
        return AsrStartResult(
            status=AsrStartStatus.READY,
            provider="qwen",
            session_epoch=runtime._capture_ingress_token().session_epoch,
        )

    start_mock = AsyncMock(side_effect=_ready)
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    await runtime._start_independent_asr_if_enabled("audio", handshake_override=True)

    assert runtime._asr_route_mode == "independent"
    start_mock.assert_awaited_once()


async def test_provider_restart_reuses_accepted_session_optimization(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.input_mode = "audio"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(
            return_value={
                "independentAsrEnabled": True,
                "voiceInputResourceOptimizationEnabled": True,
            }
        ),
    )
    start_mock = AsyncMock(
        return_value=AsrStartResult(
            status=AsrStartStatus.READY,
            provider="qwen",
            session_epoch=0,
        )
    )
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    await runtime._start_independent_asr_if_enabled(
        "audio",
        resource_optimization_override=False,
    )
    assert runtime._voice_input_resource_optimization_session_value is False

    # A losing/deduplicated request may overwrite the shared handshake, but a
    # provider-changing restart still belongs to the already accepted session.
    runtime.set_voice_input_resource_optimization_handshake(True)
    runtime.core_api_type = "openai"
    await runtime._reconcile_independent_asr_after_core_change()

    assert start_mock.await_count == 2
    assert all(
        call.kwargs["resource_optimization_enabled"] is False
        for call in start_mock.await_args_list
    )


@pytest.mark.parametrize("malformed", ["false", 0, 1, [False], {"enabled": False}])
async def test_resource_optimization_handshake_malformed_falls_back_to_persisted(
    monkeypatch,
    malformed,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(
            return_value={
                "independentAsrEnabled": True,
                "voiceInputResourceOptimizationEnabled": True,
            }
        ),
    )
    start_mock = AsyncMock(
        return_value=AsrStartResult(
            status=AsrStartStatus.FAILED,
            failure_code="ASR_START_STALE",
        )
    )
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    runtime.set_voice_input_resource_optimization_handshake(malformed)
    await runtime._start_independent_asr_if_enabled("audio")

    assert start_mock.await_args.kwargs["resource_optimization_enabled"] is True


async def test_start_session_handshake_missing_falls_back_to_persisted(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    start_mock = AsyncMock(
        return_value=AsrStartResult(
            status=AsrStartStatus.FAILED,
            failure_code="ASR_START_STALE",
        )
    )
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    # An absent field (forwarded as None by the router) clears any override a
    # previous session left behind, restoring the persisted-setting behavior.
    runtime.set_independent_asr_handshake(True)
    runtime.set_independent_asr_handshake(None)
    await runtime._start_independent_asr_if_enabled("audio")

    start_mock.assert_awaited_once()


async def test_missing_independent_asr_setting_defaults_disabled(monkeypatch) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={}),
    )
    start_mock = AsyncMock(
        return_value=AsrStartResult(
            status=AsrStartStatus.FAILED,
            failure_code="ASR_START_STALE",
        )
    )
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    await runtime._start_independent_asr_if_enabled("audio")

    start_mock.assert_not_awaited()
    assert runtime._asr_route_mode == "native"


@pytest.mark.parametrize("malformed", ["true", 1, 0, [True], {"enabled": True}])
async def test_start_session_handshake_malformed_value_is_ignored(
    monkeypatch,
    malformed,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": False}),
    )
    start_mock = AsyncMock()
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)

    # Strict bool typing: truthy non-bool values never enable the route.
    runtime.set_independent_asr_handshake(malformed)
    await runtime._start_independent_asr_if_enabled("audio")

    start_mock.assert_not_awaited()
    assert runtime._asr_route_mode == "native"


class _HotSwapRuntimeStub:
    def __init__(self, *, start_status: AsrStartStatus) -> None:
        self.session_epoch = 1
        self.audio_generation = 1
        self.active_provider: str | None = "provider-a"
        self.start_status = start_status
        self.submissions: list[tuple[str | None, bytes, object]] = []
        self.abort = AsyncMock()

    def capture_ingress_token(
        self,
        *,
        connection_id: str,
        lease_generation: int,
        route_generation: int,
    ):
        from main_logic.voice_turn.contracts import VoiceIngressToken

        return VoiceIngressToken(
            self.session_epoch,
            connection_id,
            lease_generation,
            route_generation,
            self.audio_generation,
        )

    async def stop_session(self) -> None:
        self.session_epoch += 1
        self.audio_generation += 1
        self.active_provider = None

    async def close(self) -> None:
        await self.stop_session()

    async def start(
        self,
        *,
        route_key: str,
        resource_optimization_enabled: bool,
        user_language: str | None = None,
    ) -> AsrStartResult:
        _ = (route_key, resource_optimization_enabled, user_language)
        self.active_provider = (
            "provider-b" if self.start_status is AsrStartStatus.READY else None
        )
        return AsrStartResult(
            self.start_status,
            provider="provider-b",
            session_epoch=self.session_epoch,
        )

    async def submit(self, frame, *, ingress_token) -> AsrSubmitResult:
        self.submissions.append((self.active_provider, frame.pcm16, ingress_token))
        return AsrSubmitResult(AsrSubmitStatus.ACCEPTED)


async def test_provider_hot_swap_drops_cached_pcm_from_old_asr_generation(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    bridge = _HotSwapRuntimeStub(start_status=AsrStartStatus.READY)
    object.__setattr__(runtime, "_asr_runtime", bridge)
    runtime.core_api_type = "glm"
    runtime.input_mode = "audio"
    runtime.is_active = True
    runtime.is_hot_swap_imminent = True
    runtime.session.stream_audio = AsyncMock()
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "provider-a"
    runtime._independent_asr_route_key = "gemini"
    old_token = runtime._capture_ingress_token()
    assert runtime.hot_swap_audio_cache.append(
        _HotSwapAudioFrame(
            pcm16=b"\x01\x00" * 160,
            token=old_token,
            audio_stream_epoch=runtime._audio_stream_epoch,
        )
    )
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )

    await runtime._reconcile_independent_asr_after_core_change()

    assert len(runtime.hot_swap_audio_cache) == 1
    assert runtime._asr_route_mode == "independent"
    await runtime._flush_hot_swap_audio_cache()

    assert bridge.submissions == []
    runtime.session.stream_audio.assert_not_awaited()


async def test_failed_provider_hot_swap_blocks_and_discards_cached_pcm(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    bridge = _HotSwapRuntimeStub(start_status=AsrStartStatus.UNAVAILABLE)
    object.__setattr__(runtime, "_asr_runtime", bridge)
    runtime.core_api_type = "glm"
    runtime.input_mode = "audio"
    runtime.is_active = True
    runtime.is_hot_swap_imminent = True
    runtime.session.stream_audio = AsyncMock()
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "provider-a"
    runtime._independent_asr_route_key = "gemini"
    assert runtime.hot_swap_audio_cache.append(
        _HotSwapAudioFrame(
            pcm16=b"\x01\x00" * 160,
            token=runtime._capture_ingress_token(),
            audio_stream_epoch=runtime._audio_stream_epoch,
        )
    )
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )

    await runtime._reconcile_independent_asr_after_core_change()
    assert runtime._asr_route_mode == "blocked"
    await runtime._flush_hot_swap_audio_cache()

    assert bridge.submissions == []
    assert not runtime.hot_swap_audio_cache
    runtime.session.stream_audio.assert_not_awaited()


async def test_current_audio_pipeline_failure_blocks_once_without_pcm() -> None:
    runtime = _Runtime()
    runtime.is_active = True
    runtime.is_hot_swap_imminent = False
    runtime.is_flushing_hot_swap_cache = False
    runtime.session.stream_audio = AsyncMock()
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    runtime._asr_runtime.abort = AsyncMock()
    runtime._asr_runtime.submit = AsyncMock()
    runtime._voice_input_audio_pipeline.process = AsyncMock(
        side_effect=RuntimeError("soxr failed")
    )
    token = runtime._capture_ingress_token()
    message = {
        "input_type": "audio",
        "sample_rate_hz": 48_000,
        "data": [1] * 480,
    }

    await runtime._process_microphone_stream_data(
        message,
        ingress_token=token,
    )
    await runtime._process_microphone_stream_data(
        message,
        ingress_token=token,
    )

    assert runtime._asr_route_mode == "blocked"
    runtime._asr_runtime.abort.assert_awaited_once_with("audio_preprocessing_failed")
    runtime._asr_runtime.submit.assert_not_awaited()
    runtime.session.stream_audio.assert_not_awaited()
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    assert statuses == [
        {
            "code": "ASR_AUDIO_PREPROCESSING_FAILED",
            "details": {"provider": "glm", "session_epoch": 0},
        }
    ]


async def test_noise_reduction_replacement_waits_for_pipeline_failure_revoke() -> None:
    class _ObservedAsyncLock:
        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self._requests = 0
            self.second_request = asyncio.Event()

        async def __aenter__(self):
            self._requests += 1
            if self._requests == 2:
                self.second_request.set()
            await self._lock.acquire()
            return self

        async def __aexit__(self, *_exc_info) -> None:
            self._lock.release()

    runtime = _Runtime()
    runtime.is_active = True
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    assert runtime._begin_voice_input_connection("socket-a") is True
    runtime._voice_lease_owner = "core"
    runtime._voice_lease_synchronized = True
    transition_lock = _ObservedAsyncLock()
    runtime._voice_input_pipeline_transition_lock = transition_lock
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def block_first_abort(_reason: str) -> None:
        abort_started.set()
        await release_abort.wait()

    runtime._asr_runtime.abort = AsyncMock(side_effect=block_first_abort)
    source_pipeline = runtime._voice_input_audio_pipeline
    failure = asyncio.create_task(
        runtime._fail_voice_input_pipeline(
            ingress_token=runtime._capture_ingress_token(),
            session_ref=runtime.session,
            audio_epoch=runtime._audio_stream_epoch,
            pipeline_ref=source_pipeline,
        )
    )
    await asyncio.wait_for(abort_started.wait(), 1)

    replacement = asyncio.create_task(runtime.apply_voice_input_noise_reduction(False))
    await asyncio.wait_for(transition_lock.second_request.wait(), 1)
    release_abort.set()
    failure_result, replacement_result = await asyncio.wait_for(
        asyncio.gather(failure, replacement),
        1,
    )

    assert failure_result is None
    assert replacement_result is True
    assert runtime._asr_route_mode == "blocked"
    assert runtime._voice_lease_connection_id == ""
    assert runtime._voice_lease_owner == "none"
    assert runtime._voice_input_audio_pipeline is not source_pipeline
    assert runtime._voice_input_audio_pipeline.nr_enabled is False
    assert runtime._voice_input_pipeline_failed is False


async def test_pipeline_failure_still_revokes_after_a_bare_pipeline_swap() -> None:
    """A replacement that does not end the route must not skip the revoke.

    The mirror image of the case above. Replacing the pipeline clears
    ``_voice_input_pipeline_failed`` -- that is all a noise-reduction toggle
    does -- but it neither unblocks the route nor revokes the lease, so
    reading it as "someone else owns this failure now" leaves the microphone
    blocked forever with the lease still held. That is the race commit
    94c26715 was written for, and it is why the notify phase fences on the
    failure's own token instead: a replacement that genuinely retires this
    failure (a start, a close) advances the route operation generation, which
    ``_fail_closed_voice_route`` checks on its own.
    """

    runtime = _Runtime()
    runtime.is_active = True
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    assert runtime._begin_voice_input_connection("socket-a") is True
    runtime._voice_lease_owner = "core"
    runtime._voice_lease_synchronized = True
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def delayed_abort(_reason: str) -> None:
        abort_started.set()
        await release_abort.wait()

    runtime._asr_runtime.abort = AsyncMock(side_effect=delayed_abort)
    failure = asyncio.create_task(
        runtime._fail_voice_input_pipeline(
            ingress_token=runtime._capture_ingress_token(),
            session_ref=runtime.session,
            audio_epoch=runtime._audio_stream_epoch,
            pipeline_ref=runtime._voice_input_audio_pipeline,
        )
    )
    await asyncio.wait_for(abort_started.wait(), 1)

    runtime._voice_input_audio_pipeline = SimpleNamespace(
        process=AsyncMock(),
        close=AsyncMock(),
    )
    runtime._voice_input_pipeline_failed = False

    release_abort.set()
    await asyncio.wait_for(failure, 1)

    runtime.send_status.assert_awaited()
    assert runtime._voice_lease_connection_id == ""
    assert runtime._voice_lease_owner == "none"


async def test_backpressured_status_send_does_not_block_pipeline_transitions() -> None:
    """The frontend socket is unbounded; the transition lock must not wait on it.

    ``_fail_closed_voice_route`` writes the failure notice to the voice owner,
    and a throttled or backpressured client can stall that write for as long
    as it likes. Session restart, independent-ASR close and the
    noise-reduction toggle all need the same pipeline transition lock, so
    holding it across the notify phase parked every recovery operation behind
    one unrelated client write.
    """

    runtime = _Runtime()
    runtime.is_active = True
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    assert runtime._begin_voice_input_connection("socket-a") is True
    runtime._voice_lease_owner = "core"
    runtime._voice_lease_synchronized = True
    runtime._asr_runtime.abort = AsyncMock()
    status_started = asyncio.Event()
    release_status = asyncio.Event()

    async def backpressured_status(_payload) -> None:
        status_started.set()
        await release_status.wait()

    runtime.send_status = AsyncMock(side_effect=backpressured_status)
    failure = asyncio.create_task(
        runtime._fail_voice_input_pipeline(
            ingress_token=runtime._capture_ingress_token(),
            session_ref=runtime.session,
            audio_epoch=runtime._audio_stream_epoch,
            pipeline_ref=runtime._voice_input_audio_pipeline,
        )
    )
    await asyncio.wait_for(status_started.wait(), 1)

    source_pipeline = runtime._voice_input_audio_pipeline
    # The client is still absorbing the notice. A toggle must not queue behind
    # it: this is the whole point of shrinking the lock.
    assert await asyncio.wait_for(
        runtime.apply_voice_input_noise_reduction(False),
        1,
    ) is True
    assert runtime._voice_input_audio_pipeline is not source_pipeline

    release_status.set()
    await asyncio.wait_for(failure, 1)

    # ...and the failure still finishes fail-closed once the client catches up.
    assert runtime._asr_route_mode == "blocked"
    assert runtime._voice_lease_connection_id == ""
    assert runtime._voice_lease_owner == "none"


async def test_pipeline_toggle_during_failure_notify_keeps_ingress_closed() -> None:
    """A toggle must not reopen the microphone while the route is failing.

    `_voice_input_pipeline_failed` is the ingress gate, and ANY pipeline
    replacement clears it -- a noise-reduction toggle included. Once the
    notify phase left the transition lock, such a toggle can land while a
    backpressured status send is still in flight, i.e. while this failure
    still owns a blocked route whose lease has not been revoked. Frames would
    then be parsed, queued and run through the replacement DSP until the route
    discards them: bounded queue refilled, preprocessing burnt, ingress
    backpressure tripped, all during what must stay a fail-closed interval.
    """

    runtime = _Runtime()
    runtime.is_active = True
    runtime.is_hot_swap_imminent = False
    runtime.is_flushing_hot_swap_cache = False
    runtime.session.stream_audio = AsyncMock()
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    assert runtime._begin_voice_input_connection("socket-a") is True
    runtime._voice_lease_owner = "core"
    runtime._voice_lease_synchronized = True
    runtime._asr_runtime.abort = AsyncMock()
    runtime._asr_runtime.submit = AsyncMock()
    token = runtime._capture_ingress_token()
    status_started = asyncio.Event()
    release_status = asyncio.Event()

    async def backpressured_status(_payload) -> None:
        status_started.set()
        await release_status.wait()

    runtime.send_status = AsyncMock(side_effect=backpressured_status)
    failure = asyncio.create_task(
        runtime._fail_voice_input_pipeline(
            ingress_token=token,
            session_ref=runtime.session,
            audio_epoch=runtime._audio_stream_epoch,
            pipeline_ref=runtime._voice_input_audio_pipeline,
        )
    )
    await asyncio.wait_for(status_started.wait(), 1)

    assert await asyncio.wait_for(
        runtime.apply_voice_input_noise_reduction(False),
        1,
    ) is True
    # Premise: the toggle really did clear the old gate, so anything still
    # holding ingress closed is the committed failure's own latch.
    assert runtime._voice_input_pipeline_failed is False
    replacement = runtime._voice_input_audio_pipeline
    replacement.process = AsyncMock()

    # Captured HERE, not before the failure. A live client keeps sending PCM
    # with a current token, so `_ingress_token_matches` passes and the frame
    # reaches the DSP without any lease check in between -- which is the whole
    # point. A token snapshotted before the route was blocked would be dropped
    # by the token fence instead, and this test would prove nothing.
    live_token = runtime._capture_ingress_token()
    assert runtime._ingress_token_matches(live_token) is True

    await runtime._process_microphone_stream_data(
        {"input_type": "audio", "sample_rate_hz": 48_000, "data": [1] * 480},
        ingress_token=live_token,
    )

    replacement.process.assert_not_awaited()
    runtime._asr_runtime.submit.assert_not_awaited()
    runtime.session.stream_audio.assert_not_awaited()

    release_status.set()
    await asyncio.wait_for(failure, 1)
    assert runtime._asr_route_mode == "blocked"
    assert runtime._voice_lease_connection_id == ""


async def test_a_live_route_releases_the_pipeline_failure_ingress_latch() -> None:
    """The latch is fail-closed, not permanent: a live route clears it."""

    runtime = _Runtime()
    runtime.is_active = True
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    runtime._asr_runtime.abort = AsyncMock()
    runtime._voice_input_audio_pipeline.process = AsyncMock(
        side_effect=RuntimeError("soxr failed")
    )
    token = runtime._capture_ingress_token()
    await runtime._process_microphone_stream_data(
        {"input_type": "audio", "sample_rate_hz": 48_000, "data": [1] * 480},
        ingress_token=token,
    )
    assert runtime._asr_route_mode == "blocked"
    assert runtime._voice_input_pipeline_failure_token is not None

    runtime._set_microphone_route("independent")
    assert runtime._voice_input_pipeline_failure_token is None


async def test_pipeline_failure_stops_accepting_pcm_at_ingress() -> None:
    """The latch drops frames before the queue, not after the worker dequeues.

    NATIVE route and the fixture's DEFAULT lease state, both load-bearing.
    The independent route reaches `_abort_independent_asr` on the way here and
    that invalidates the voice PCM sync, which closes the lease gate one step
    below the latch; and `_begin_voice_input_connection` also leaves the lease
    in a state that refuses PCM. Either one makes this test pass without ever
    exercising the latch -- the first version of it did exactly that, and the
    mutant survived.

    On the native route with a live lease nothing else stands in the way:
    `_voice_input_accepts_pcm` is lease-only and reads neither the route mode
    nor the latch, so a backpressured status send left the client free to fill
    the bounded queue -- and overflowing it takes the QueueFull path, which
    aborts the run all over again.
    """

    runtime = _Runtime()
    runtime.is_active = True
    runtime._set_microphone_route("native")
    runtime._asr_runtime.abort = AsyncMock()
    status_started = asyncio.Event()
    release_status = asyncio.Event()

    async def backpressured_status(_payload) -> None:
        status_started.set()
        await release_status.wait()

    runtime.send_status = AsyncMock(side_effect=backpressured_status)
    # Keep the worker from draining what the queue accepts, so the depth below
    # measures what ingress ADMITTED rather than what survived a race with it.
    runtime._ensure_audio_stream_worker = lambda: None

    failure = asyncio.create_task(
        runtime._fail_voice_input_pipeline(
            ingress_token=runtime._capture_ingress_token(),
            session_ref=runtime.session,
            audio_epoch=runtime._audio_stream_epoch,
            pipeline_ref=runtime._voice_input_audio_pipeline,
        )
    )
    await asyncio.wait_for(status_started.wait(), 1)

    # Premise: everything DOWNSTREAM of the latch would still take this PCM.
    # Without this the test can pass for the wrong reason.
    assert runtime._voice_input_accepts_pcm() is True

    for _ in range(4):
        await runtime._enqueue_audio_stream_data(
            {"input_type": "audio", "sample_rate_hz": 48_000, "data": [1] * 480}
        )

    assert runtime._audio_stream_queue.qsize() == 0, (
        "PCM arriving during the failure notice must be dropped at ingress "
        "rather than queued behind it"
    )

    release_status.set()
    await asyncio.wait_for(failure, 1)
    assert runtime._asr_route_mode == "blocked"


async def test_stale_failure_abort_does_not_clear_successor_route_audio() -> None:
    """A restart landing inside the abort keeps its own queued audio.

    `_abort_independent_asr` invalidates the voice PCM sync AFTER awaiting the
    runtime abort. That await is no longer covered by the pipeline transition
    lock, so a session restart can install a newer route inside it -- and the
    old failure would then clear the successor's queued and hot-swap audio and
    drop its microphone input.
    """

    runtime = _Runtime()
    runtime.is_active = True
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    assert runtime._begin_voice_input_connection("socket-a") is True
    runtime._voice_lease_owner = "core"
    runtime._voice_lease_synchronized = True
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def blocking_abort(_reason: str) -> None:
        abort_started.set()
        await release_abort.wait()

    runtime._asr_runtime.abort = AsyncMock(side_effect=blocking_abort)
    invalidated: list[str] = []
    runtime._invalidate_voice_pcm_sync = lambda reason: invalidated.append(reason)

    failure = asyncio.create_task(
        runtime._fail_voice_input_pipeline(
            ingress_token=runtime._capture_ingress_token(),
            session_ref=runtime.session,
            audio_epoch=runtime._audio_stream_epoch,
            pipeline_ref=runtime._voice_input_audio_pipeline,
        )
    )
    await asyncio.wait_for(abort_started.wait(), 1)

    # A newer route operation claims the route while the abort is in flight.
    object.__setattr__(
        runtime,
        "_asr_route_operation_generation",
        runtime._asr_route_operation_generation + 1,
    )
    release_abort.set()
    await asyncio.wait_for(failure, 1)

    assert invalidated == [], (
        "the successor route owns the voice PCM sync now; this failure must "
        "not clear it"
    )
    runtime.send_status.assert_not_awaited()


async def test_pipeline_failure_from_replaced_connection_is_silent() -> None:
    runtime = _Runtime()
    runtime.is_active = True
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def delayed_abort(_reason: str) -> None:
        abort_started.set()
        await release_abort.wait()

    runtime._asr_runtime.abort = AsyncMock(side_effect=delayed_abort)
    token = runtime._capture_ingress_token()
    failure = asyncio.create_task(
        runtime._fail_voice_input_pipeline(
            ingress_token=token,
            session_ref=runtime.session,
            audio_epoch=runtime._audio_stream_epoch,
            pipeline_ref=runtime._voice_input_audio_pipeline,
        )
    )
    await asyncio.wait_for(abort_started.wait(), 1)

    assert runtime._begin_voice_input_connection("replacement-connection")
    replacement_lease_state = (
        runtime._voice_lease_connection_id,
        runtime._voice_lease_generation,
        runtime._voice_lease_owner,
        runtime._voice_lease_synchronized,
    )
    release_abort.set()
    await asyncio.wait_for(failure, 1)

    assert (
        runtime._voice_lease_connection_id,
        runtime._voice_lease_generation,
        runtime._voice_lease_owner,
        runtime._voice_lease_synchronized,
    ) == replacement_lease_state
    runtime.send_status.assert_not_awaited()
    runtime._asr_runtime.abort.assert_awaited_once_with("audio_preprocessing_failed")


@pytest.mark.parametrize(
    "changed_identity",
    [
        "lease_generation",
        "hard_mute",
        "focus_suppression",
        "game_takeover",
        "route_operation",
        "core_session",
    ],
)
async def test_stale_pipeline_failure_never_reports_to_current_identity(
    changed_identity: str,
) -> None:
    runtime = _Runtime()
    runtime.is_active = True
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "glm"
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def delayed_abort(_reason: str) -> None:
        abort_started.set()
        await release_abort.wait()

    runtime._asr_runtime.abort = AsyncMock(side_effect=delayed_abort)
    token = runtime._capture_ingress_token()
    source_pipeline = runtime._voice_input_audio_pipeline
    failure = asyncio.create_task(
        runtime._fail_voice_input_pipeline(
            ingress_token=token,
            session_ref=runtime.session,
            audio_epoch=runtime._audio_stream_epoch,
            pipeline_ref=source_pipeline,
        )
    )
    await asyncio.wait_for(abort_started.wait(), 1)

    if changed_identity == "lease_generation":
        runtime._voice_lease_generation += 1
    elif changed_identity == "hard_mute":
        runtime._voice_lease_hard_muted = True
        runtime._voice_input_transition_generation += 1
    elif changed_identity == "focus_suppression":
        runtime._voice_lease_focus_suppressed = True
        runtime._voice_input_transition_generation += 1
    elif changed_identity == "game_takeover":
        runtime._voice_lease_owner = "game"
        runtime._voice_input_transition_generation += 1
    elif changed_identity == "route_operation":
        object.__setattr__(
            runtime,
            "_asr_route_operation_generation",
            runtime._asr_route_operation_generation + 1,
        )
    elif changed_identity == "core_session":
        runtime.session = SimpleNamespace(stream_audio=AsyncMock())
    else:
        raise AssertionError(changed_identity)

    release_abort.set()
    await asyncio.wait_for(failure, 1)

    runtime.send_status.assert_not_awaited()
    runtime._asr_runtime.abort.assert_awaited_once_with("audio_preprocessing_failed")


async def test_replaced_audio_pipeline_late_failure_is_silent() -> None:
    runtime = _Runtime()
    runtime.is_active = True
    runtime.is_hot_swap_imminent = True
    runtime.is_flushing_hot_swap_cache = False
    runtime.session.stream_audio = AsyncMock()
    runtime._set_microphone_route("independent")
    runtime._asr_runtime.abort = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fail_late(*_args, **_kwargs):
        started.set()
        await release.wait()
        raise RuntimeError("old pipeline failed")

    old_pipeline = runtime._voice_input_audio_pipeline
    old_pipeline.process = AsyncMock(side_effect=fail_late)
    token = runtime._capture_ingress_token()
    processing = asyncio.create_task(
        runtime._process_microphone_stream_data(
            {
                "input_type": "audio",
                "sample_rate_hz": 48_000,
                "data": [1] * 480,
            },
            ingress_token=token,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    runtime._voice_input_audio_pipeline = type(
        "ReplacementPipeline",
        (),
        {"process": AsyncMock(), "close": AsyncMock()},
    )()
    runtime._set_microphone_route("blocked")
    runtime._set_microphone_route("independent")
    release.set()
    await asyncio.wait_for(processing, 1)

    runtime._asr_runtime.abort.assert_not_awaited()
    runtime.session.stream_audio.assert_not_awaited()
    runtime.send_status.assert_not_awaited()
    assert runtime._voice_input_pipeline_failed is False


async def test_old_abort_release_cannot_close_replacement_session() -> None:
    runtime = _Runtime()
    old_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = old_session
    _install_ready_lifecycle(runtime, "qwen")
    old_lifecycle = runtime._asr_lifecycle
    old_detector = runtime._asr_detector
    release_started = asyncio.Event()
    release_old_lease = asyncio.Event()

    class BlockingLease:
        async def release(self) -> None:
            release_started.set()
            await release_old_lease.wait()

    runtime._asr_smart_turn_lease = BlockingLease()
    abort_task = asyncio.create_task(runtime._asr_runtime.abort("test_abort"))
    await asyncio.wait_for(release_started.wait(), 1)

    new_session, new_lifecycle, new_detector = _install_replacement_runtime_generation(
        runtime, "qwen"
    )
    release_old_lease.set()
    await asyncio.wait_for(abort_task, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert runtime._asr_lifecycle is not old_lifecycle
    assert runtime._asr_detector is not old_detector
    old_session.close.assert_awaited_once_with()
    new_session.close.assert_not_awaited()


async def test_old_failure_callback_cannot_detach_replacement_runtime() -> None:
    runtime = _Runtime()
    old_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = old_session
    _install_ready_lifecycle(runtime, "qwen")
    old_lifecycle = runtime._asr_lifecycle
    old_detector = runtime._asr_detector
    blocked_started = asyncio.Event()
    release_blocked = asyncio.Event()

    async def block_old_lifecycle(payload: str) -> None:
        status = json.loads(payload)
        if (
            status.get("code") == "ASR_LIFECYCLE_STATE"
            and status.get("details", {}).get("state") == "blocked"
        ):
            blocked_started.set()
            await release_blocked.wait()

    runtime.send_status.side_effect = block_old_lifecycle
    old_epoch = runtime._asr_session_epoch
    failure_task = asyncio.create_task(
        runtime._handle_independent_asr_error(old_epoch, "qwen")
    )
    await asyncio.wait_for(blocked_started.wait(), 1)

    new_session, new_lifecycle, new_detector = _install_replacement_runtime_generation(
        runtime, "qwen"
    )
    release_blocked.set()
    await asyncio.wait_for(failure_task, 1)
    await asyncio.sleep(0)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert old_lifecycle.snapshot.state is VoiceLifecycleState.OFF
    old_detector.close.assert_awaited_once_with()
    new_session.close.assert_not_awaited()
    new_detector.close.assert_not_awaited()
    assert runtime._asr_route_mode == "independent"
    assert [
        json.loads(call.args[0])["code"]
        for call in runtime.send_status.await_args_list
    ] == ["ASR_LIFECYCLE_STATE"]


async def test_old_detector_endpoint_cannot_seal_replacement_runtime() -> None:
    runtime = _Runtime()
    old_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = old_session
    _install_ready_lifecycle(runtime, "qwen")
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    detector = _QueuedSmartTurnDetector()
    detector.detector_epoch = 1
    runtime._asr_detector = detector
    runtime._asr_runtime._asr_current_ingress_token = runtime._capture_ingress_token()
    turn_token = runtime._asr_runtime._capture_turn_token(lifecycle)
    detector._token = turn_token
    candidate = DetectorCandidateKey(detector.detector_epoch, 1)
    envelope = CoreDetectorEventEnvelope(
        event=DetectorTurnEvent(
            ingress=DetectorIngressIdentity(
                ingress_token=turn_token.ingress,
                detector_epoch=detector.detector_epoch,
                sequence_no=1,
            ),
            bound_turn=BoundDetectorTurn(
                candidate=candidate,
                turn_token=turn_token,
            ),
            kind="complete",
        ),
        detector_ref=detector,
        lifecycle_ref=lifecycle,
        session_epoch=runtime._asr_session_epoch,
    )
    draining_started = asyncio.Event()
    release_draining = asyncio.Event()

    async def block_old_lifecycle(payload: str) -> None:
        status = json.loads(payload)
        if (
            status.get("code") == "ASR_LIFECYCLE_STATE"
            and status.get("details", {}).get("state") == "draining"
        ):
            draining_started.set()
            await release_draining.wait()

    runtime.send_status.side_effect = block_old_lifecycle
    endpoint_task = asyncio.create_task(
        runtime._asr_runtime._dispatch_asr_detector_event(envelope)
    )
    await asyncio.wait_for(draining_started.wait(), 1)

    new_session, new_lifecycle, new_detector = _install_replacement_runtime_generation(
        runtime, "qwen"
    )
    release_draining.set()
    await asyncio.wait_for(endpoint_task, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    new_session.close.assert_not_awaited()
    assert runtime._asr_route_mode == "independent"
    statuses = [
        json.loads(call.args[0]).get("code")
        for call in runtime.send_status.await_args_list
    ]
    assert "ASR_AUDIO_ORDERING_FAILED" not in statuses


async def test_old_smart_turn_release_cannot_clear_replacement_lease() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "glm")
    lifecycle = runtime._asr_lifecycle
    detector = runtime._asr_detector
    assert lifecycle is not None
    assert detector is not None
    release_started = asyncio.Event()
    release_old_lease = asyncio.Event()

    class BlockingLease:
        token = object()

        async def release(self) -> None:
            release_started.set()
            await release_old_lease.wait()

    old_lease = BlockingLease()
    runtime._asr_smart_turn_lease = old_lease
    prepare_task = asyncio.create_task(
        runtime._asr_runtime._ensure_smart_turn_ready(
            lifecycle,
            runtime._asr_session_epoch,
        )
    )
    await asyncio.wait_for(release_started.wait(), 1)

    new_session, new_lifecycle, new_detector = _install_replacement_runtime_generation(
        runtime, "glm"
    )
    new_lease = _TestSmartTurnLease(
        runtime._asr_runtime._capture_turn_token(new_lifecycle)
    )
    runtime._asr_smart_turn_lease = new_lease
    release_old_lease.set()

    assert await asyncio.wait_for(prepare_task, 1) is False
    assert runtime._asr_smart_turn_lease is new_lease
    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert new_lease.released is False


async def test_concurrent_smart_turn_readiness_callers_share_installed_lease() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "glm")
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()

    class _Lease:
        def __init__(self, token, detector) -> None:
            self.token = token
            self._detector = detector
            self.released = False

        async def release(self) -> None:
            self.released = True
            self._detector.token = None

    class _BlockingDetector:
        def __init__(self) -> None:
            self.token = None
            self.prepare_calls = 0

        async def prepare_endpointing(self, token):
            self.prepare_calls += 1
            prepare_started.set()
            await release_prepare.wait()
            self.token = token
            return _Lease(token, self)

        def endpointing_ready(self, token) -> bool:
            return self.token == token

    detector = _BlockingDetector()
    runtime._asr_detector = detector
    component = runtime._asr_runtime
    epoch = component._asr_session_epoch
    first = asyncio.create_task(
        component._ensure_smart_turn_ready(lifecycle, epoch)
    )
    await asyncio.wait_for(prepare_started.wait(), 1)
    second_started = asyncio.Event()

    async def ensure_from_speech_caller() -> bool:
        second_started.set()
        return await component._ensure_smart_turn_ready(lifecycle, epoch)

    second = asyncio.create_task(ensure_from_speech_caller())
    await asyncio.wait_for(second_started.wait(), 1)
    release_prepare.set()

    assert await asyncio.wait_for(first, 1) is True
    assert await asyncio.wait_for(second, 1) is True
    assert detector.prepare_calls == 1
    lease = component._asr_smart_turn_lease
    assert lease is not None
    assert lease.released is False
    assert detector.endpointing_ready(lease.token) is True


async def test_stale_detector_feed_exception_cannot_fail_new_generation() -> None:
    runtime = _Runtime()
    old_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = old_session
    _install_ready_lifecycle(runtime, "qwen")
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingDetector(_ReadyDetector):
        async def feed(self, _pcm16: bytes, **_kwargs):
            started.set()
            await release.wait()
            raise RuntimeError("old detector failed")

    runtime._asr_detector = _BlockingDetector()
    ingress = runtime._capture_ingress_token()
    runtime._asr_runtime._asr_current_ingress_token = ingress
    submit = asyncio.create_task(
        runtime._asr_runtime.submit(
            ProcessedVoiceFrame(b"\x01\x00" * 160, 16_000, 0.8, True),
            ingress_token=ingress,
        )
    )
    await asyncio.wait_for(started.wait(), 1)

    new_session, new_lifecycle, new_detector = _install_replacement_runtime_generation(
        runtime, "qwen"
    )
    release.set()
    result = await asyncio.wait_for(submit, 1)

    assert result.status is AsrSubmitStatus.STALE
    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    new_session.close.assert_not_awaited()
    runtime.send_status.assert_not_awaited()


async def test_current_detector_feed_exception_fails_closed_once() -> None:
    runtime = _Runtime()
    runtime._asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    _install_ready_lifecycle(runtime, "qwen")
    runtime._asr_detector.feed = AsyncMock(
        side_effect=RuntimeError("current detector failed")
    )
    ingress = runtime._capture_ingress_token()
    runtime._asr_runtime._asr_current_ingress_token = ingress

    result = await runtime._asr_runtime.submit(
        ProcessedVoiceFrame(b"\x01\x00" * 160, 16_000, 0.8, True),
        ingress_token=ingress,
    )
    await asyncio.sleep(0)

    assert result.status is AsrSubmitStatus.UNAVAILABLE
    codes = [
        json.loads(call.args[0])["code"] for call in runtime.send_status.await_args_list
    ]
    assert codes.count("ASR_INDEPENDENT_STREAM_FAILED") == 1
    assert runtime._asr_session is None
    assert runtime._asr_lifecycle is None
    assert runtime._asr_detector is None


async def test_stale_connect_failure_cannot_fail_new_generation() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    runtime._asr_session = None
    started = asyncio.Event()
    release = asyncio.Event()
    candidate = SimpleNamespace(close=AsyncMock())

    async def connect() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("old candidate failed")

    candidate.connect = AsyncMock(side_effect=connect)
    runtime._asr_session_factory = MagicMock(return_value=candidate)
    runtime._asr_transport_selection = _selection("qwen")
    old_restart = asyncio.create_task(runtime._restart_transport())
    runtime._asr_transport_task = old_restart
    await asyncio.wait_for(started.wait(), 1)

    new_session, new_lifecycle, new_detector = _install_replacement_runtime_generation(
        runtime, "qwen"
    )
    new_factory = object()
    new_selection = object()
    keep_transport = asyncio.Event()
    new_transport = asyncio.create_task(keep_transport.wait())
    runtime._asr_session_factory = new_factory
    runtime._asr_transport_selection = new_selection
    runtime._asr_transport_task = new_transport
    release.set()
    await asyncio.wait_for(old_restart, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert runtime._asr_session_factory is new_factory
    assert runtime._asr_transport_selection is new_selection
    assert runtime._asr_transport_task is new_transport
    candidate.close.assert_awaited_once_with()
    new_session.close.assert_not_awaited()
    runtime.send_status.assert_not_awaited()
    keep_transport.set()
    await new_transport


async def test_close_unwind_cannot_clear_new_generation_owned_fields() -> None:
    runtime = _Runtime()
    old_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = old_session
    _install_ready_lifecycle(runtime, "qwen")
    old_detector = runtime._asr_detector
    assert old_detector is not None
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def close_detector() -> None:
        close_started.set()
        await release_close.wait()

    old_detector.close = AsyncMock(side_effect=close_detector)
    runtime._asr_session_factory = object()
    runtime._asr_transport_selection = object()
    old_transport = asyncio.create_task(asyncio.Event().wait())
    runtime._asr_transport_task = old_transport
    closing = asyncio.create_task(runtime._asr_runtime._close_independent_asr())
    await asyncio.wait_for(close_started.wait(), 1)

    new_session, new_lifecycle, new_detector = _install_replacement_runtime_generation(
        runtime, "qwen"
    )
    new_factory = object()
    new_selection = object()
    keep_transport = asyncio.Event()
    new_transport = asyncio.create_task(keep_transport.wait())
    runtime._asr_session_factory = new_factory
    runtime._asr_transport_selection = new_selection
    runtime._asr_transport_task = new_transport
    new_token = runtime._capture_ingress_token()
    runtime._asr_runtime._asr_current_ingress_token = new_token
    new_transcript_dispatcher = runtime._asr_transcript_dispatcher
    new_detector_dispatcher = runtime._asr_detector_dispatcher
    new_audio_dispatcher = runtime._asr_audio_dispatcher
    release_close.set()
    await asyncio.wait_for(closing, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert runtime._asr_current_ingress_token == new_token
    assert runtime._asr_session_factory is new_factory
    assert runtime._asr_transport_selection is new_selection
    assert runtime._asr_transport_task is new_transport
    assert runtime._asr_transcript_dispatcher is new_transcript_dispatcher
    assert runtime._asr_detector_dispatcher is new_detector_dispatcher
    assert runtime._asr_audio_dispatcher is new_audio_dispatcher
    old_detector.close.assert_awaited_once_with()
    old_session.close.assert_awaited_once_with()
    new_session.close.assert_not_awaited()
    keep_transport.set()
    await new_transport


async def test_same_epoch_reconnect_survives_old_abort_release() -> None:
    runtime = _Runtime()
    old_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = old_session
    _install_ready_lifecycle(runtime, "qwen")
    lifecycle = runtime._asr_lifecycle
    old_detector = runtime._asr_detector
    assert lifecycle is not None
    assert old_detector is not None
    release_started = asyncio.Event()
    release_old_lease = asyncio.Event()

    class _BlockingLease:
        async def release(self) -> None:
            release_started.set()
            await release_old_lease.wait()

    runtime._asr_smart_turn_lease = _BlockingLease()
    aborting = asyncio.create_task(runtime._asr_runtime.abort("test_abort"))
    await asyncio.wait_for(release_started.wait(), 1)

    new_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    new_detector = _ReadyDetector()
    new_lease = _TestSmartTurnLease(object())
    runtime._asr_session = new_session
    runtime._asr_detector = new_detector
    runtime._asr_smart_turn_lease = new_lease
    lifecycle.invalidate_transport()
    runtime._asr_runtime._asr_current_ingress_token = runtime._capture_ingress_token()
    release_old_lease.set()
    await asyncio.wait_for(aborting, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is lifecycle
    assert runtime._asr_detector is new_detector
    assert runtime._asr_smart_turn_lease is new_lease
    old_session.close.assert_awaited_once_with()
    new_session.close.assert_not_awaited()
    new_detector.reset.assert_not_awaited()
    assert new_lease.released is False


async def test_old_pipeline_failure_does_not_report_replacement_provider() -> None:
    runtime = _Runtime()
    runtime.is_active = True
    runtime.is_hot_swap_imminent = True
    runtime.is_flushing_hot_swap_cache = False
    runtime._set_microphone_route("independent")
    runtime._independent_asr_provider = "provider-a"
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def block_abort(_reason: str) -> None:
        abort_started.set()
        await release_abort.wait()

    runtime._asr_runtime.abort = AsyncMock(side_effect=block_abort)
    old_pipeline = runtime._voice_input_audio_pipeline
    old_pipeline.process = AsyncMock(side_effect=RuntimeError("soxr failed"))
    processing = asyncio.create_task(
        runtime._process_microphone_stream_data(
            {
                "input_type": "audio",
                "sample_rate_hz": 48_000,
                "data": [1] * 480,
            },
            ingress_token=runtime._capture_ingress_token(),
        )
    )
    await asyncio.wait_for(abort_started.wait(), 1)

    runtime._voice_input_audio_pipeline = SimpleNamespace(
        process=AsyncMock(),
        close=AsyncMock(),
    )
    runtime._voice_input_pipeline_failed = False
    runtime._independent_asr_provider = "provider-b"
    runtime._set_microphone_route("independent")
    release_abort.set()
    await asyncio.wait_for(processing, 1)

    assert runtime._asr_route_mode == "independent"
    assert runtime._independent_asr_provider == "provider-b"
    assert runtime._voice_input_pipeline_failed is False
    runtime.send_status.assert_not_awaited()
    runtime._asr_runtime.abort.assert_awaited_once_with("audio_preprocessing_failed")


async def test_session_activation_resolves_asr_before_frontend_ack() -> None:
    order: list[str] = []
    manager = LLMSessionManager.__new__(LLMSessionManager)
    manager.lock = asyncio.Lock()
    manager.input_cache_lock = asyncio.Lock()
    manager.is_active = False
    manager._session_turn_count = 0
    manager.session_start_failure_count = 1
    manager.session_start_last_failure_time = 1.0
    manager._memory_error_retry_after = 1.0
    manager._session_start_circuit_open = True
    manager.pending_agent_callbacks = []
    manager._activity_tracker = type(
        "Tracker", (), {"on_voice_mode": lambda self, value: None}
    )()
    manager.is_goodbye_silent = lambda: False
    manager._drain_pending_context_appends_before_ready = AsyncMock()
    manager._flush_pending_input_data = AsyncMock()
    manager._consume_next_session_context_messages = MagicMock()
    manager._start_independent_asr_if_enabled = AsyncMock(
        side_effect=lambda _mode, **_kwargs: order.append("asr")
    )
    manager.send_session_started = AsyncMock(
        side_effect=lambda _mode, **_kwargs: order.append("started")
    )

    stop = asyncio.Event()

    class _Session:
        async def handle_messages(self) -> None:
            await stop.wait()

    manager.session = _Session()

    await LLMSessionManager._start_session_activate(
        manager,
        "audio",
        0,
        time.time(),
    )

    assert order == ["asr", "started"]
    stop.set()
    await manager.message_handler_task


async def test_disabled_or_text_session_never_creates_provider(monkeypatch) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    factory = MagicMock()
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": False}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        factory,
    )

    await runtime._start_independent_asr_if_enabled("audio")
    await runtime._start_independent_asr_if_enabled("text")

    factory.assert_not_called()
    assert runtime._asr_route_mode == "blocked"
    assert not hasattr(runtime._asr_runtime, "_asr_route_mode")


@pytest.mark.parametrize(
    ("persisted_enabled", "handshake_enabled"),
    [
        (True, None),
        (False, None),
        (False, True),
        (True, False),
    ],
)
async def test_free_core_always_uses_native_asr_regardless_of_toggle(
    monkeypatch,
    persisted_enabled: bool,
    handshake_enabled: bool | None,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "free"
    runtime.session.stream_audio = AsyncMock()
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": persisted_enabled}),
    )
    start_mock = AsyncMock()
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)
    runtime.set_independent_asr_handshake(handshake_enabled)

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "native"
    assert runtime._independent_asr_route_key == "free"
    assert runtime._independent_asr_provider is None
    start_mock.assert_not_awaited()
    assert "ASR_INDEPENDENT_DISABLED" in runtime.send_status.await_args.args[0]
    assert "ASR_INDEPENDENT_UNAVAILABLE" not in runtime.send_status.await_args.args[0]

    assert await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    ) is True
    runtime.session.stream_audio.assert_awaited_once_with(b"\x01\x00" * 160)


async def test_free_core_uses_native_asr_when_preferences_are_unreadable(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "free"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(side_effect=OSError("preferences unavailable")),
    )
    start_mock = AsyncMock()
    monkeypatch.setattr(runtime._asr_runtime, "start", start_mock)
    runtime.set_independent_asr_handshake(True)

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "native"
    start_mock.assert_not_awaited()
    assert "ASR_INDEPENDENT_DISABLED" in runtime.send_status.await_args.args[0]


async def test_unreadable_independent_setting_preserves_visual_route_on_hot_swap(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    runtime.session.set_visual_delivery_mode = MagicMock()
    runtime.session.block_raw_visual_delivery = MagicMock()
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(side_effect=OSError("preferences unavailable")),
    )
    runtime.set_independent_asr_handshake(True)

    await runtime._start_independent_asr_if_enabled("audio")
    await runtime._reconcile_independent_asr_after_core_change()

    assert runtime._asr_route_mode == "blocked"
    assert runtime._visual_route_mode == "independent"
    runtime.session.block_raw_visual_delivery.assert_called()


async def test_unknown_core_capability_remains_fail_closed(monkeypatch) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "unknown"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "blocked"
    assert any(
        "ASR_INDEPENDENT_FAILED" in status_call.args[0]
        for status_call in runtime.send_status.await_args_list
    )


async def test_provider_error_without_audio_closes_and_blocks_omni() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {})()
    asr.close = AsyncMock()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_error(epoch, "glm")
    await asyncio.sleep(0)

    assert runtime._asr_session_epoch == epoch + 1
    assert runtime._asr_route_mode == "blocked"
    asr.close.assert_awaited_once_with()


async def test_provider_error_reports_one_correlated_safe_reason(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, _detector = (
        await _start_runtime_with_callback_candidates(
            monkeypatch,
            candidate_count=1,
        )
    )
    runtime.send_status.reset_mock()
    provider_error = callbacks[0]["on_connection_error"]

    await provider_error(
        "  ASR_QWEN_PROVIDER_ERROR: https://provider.invalid?api_key=secret  "
    )
    await provider_error("ASR_SECOND_ERROR: must be stale")
    await asyncio.sleep(0)

    payloads = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    assert [payload["code"] for payload in payloads] == [
        "ASR_LIFECYCLE_STATE",
        "ASR_INDEPENDENT_FAILED",
    ]
    lifecycle, terminal = payloads
    assert lifecycle["details"]["state"] == "blocked"
    assert lifecycle["details"]["reason_code"] == "ASR_QWEN_PROVIDER_ERROR"
    assert terminal["details"]["reason_code"] == "ASR_QWEN_PROVIDER_ERROR"
    incident_id = lifecycle["details"]["incident_id"]
    assert incident_id == terminal["details"]["incident_id"]
    assert incident_id.startswith("asr-failure-")
    assert "provider.invalid" not in str(runtime.send_status.await_args_list)
    assert "secret" not in str(runtime.send_status.await_args_list)
    assert "ASR_SECOND_ERROR" not in str(runtime.send_status.await_args_list)
    sessions[0].close.assert_awaited_once_with()


async def test_internal_failure_uses_status_code_as_reason() -> None:
    runtime = _Runtime()
    runtime._asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_error(
        epoch,
        "qwen",
        status_code="ASR_ENDPOINTING_FAILED",
        reason_code="ASR_NOT_AN_EXPLICIT_CODE: private detail",
    )

    payloads = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    lifecycle, terminal = payloads
    assert lifecycle["code"] == "ASR_LIFECYCLE_STATE"
    assert terminal["code"] == "ASR_ENDPOINTING_FAILED"
    assert lifecycle["details"]["reason_code"] == "ASR_ENDPOINTING_FAILED"
    assert terminal["details"]["reason_code"] == "ASR_ENDPOINTING_FAILED"
    assert "private detail" not in str(runtime.send_status.await_args_list)
    assert (
        lifecycle["details"]["incident_id"]
        == terminal["details"]["incident_id"]
    )


async def test_broken_reason_stringification_cannot_interrupt_failure_cleanup() -> None:
    class _BrokenString:
        def __str__(self) -> str:
            raise RuntimeError("private provider failure")

    runtime = _Runtime()
    session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = session
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_error(
        epoch,
        "qwen",
        reason_code=_BrokenString(),
    )
    await asyncio.sleep(0)

    assert runtime._asr_session is None
    assert runtime._asr_route_mode == "blocked"
    session.close.assert_awaited_once_with()
    payloads = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    assert payloads[-1]["details"]["reason_code"] == "ASR_INDEPENDENT_FAILED"
    assert "private provider failure" not in str(runtime.send_status.await_args_list)


async def test_blocked_route_consumes_audio_without_an_asr_or_omni_send() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "blocked"

    assert (
        await runtime._route_microphone_audio(
            b"\x00\x00",
            sample_rate_hz=16_000,
        )
        is True
    )
    assert runtime._asr_route_mode == "blocked"


async def test_independent_route_without_ready_session_blocks_omni() -> None:
    runtime = _Runtime()
    asr = type("Asr", (), {"is_ready": False})()
    runtime._asr_session = asr
    runtime._asr_route_mode = "independent"

    assert (
        await runtime._route_microphone_audio(b"\x00\x00", sample_rate_hz=16_000)
        is True
    )
    assert runtime._asr_route_mode == "blocked"


async def test_settings_read_failure_blocks_omni(monkeypatch) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(side_effect=RuntimeError("settings unavailable")),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._asr_route_mode == "blocked"
    assert (
        await runtime._route_microphone_audio(b"\x00\x00", sample_rate_hz=16_000)
        is True
    )


async def test_injection_failure_is_reported_once_without_provider_body() -> None:
    runtime = _Runtime()
    runtime.session.create_response.side_effect = RuntimeError("sensitive response")
    await _start_and_seal_turn(runtime, "gemini")

    await runtime._handle_independent_asr_final(
        "hello",
        runtime._asr_session_epoch,
        "gemini",
    )
    await runtime._wait_asr_transcript_dispatch_idle()

    status_payloads = [call.args[0] for call in runtime.send_status.await_args_list]
    assert any("ASR_INDEPENDENT_INJECTION_FAILED" in item for item in status_payloads)
    assert "sensitive response" not in str(status_payloads)
    runtime.session.create_response.assert_awaited_once_with("hello")


async def test_session_swap_during_transcript_reprepares_promoted_final() -> None:
    runtime = _Runtime()
    old_session = runtime.session
    old_session.create_response.side_effect = RuntimeError("closed arbiter")
    new_session = type(
        "Omni",
        (),
        {
            "create_response": AsyncMock(),
            "prepare_external_voice_turn": AsyncMock(),
            "submit_external_voice_turn": AsyncMock(),
            "abandon_external_voice_turn": MagicMock(),
        },
    )()

    async def swap_session(*_args, **_kwargs) -> bool:
        runtime.session = new_session
        return True

    runtime.handle_input_transcript.side_effect = swap_session
    await _start_and_seal_turn(runtime, "glm")

    await runtime._handle_independent_asr_final(
        "belongs to old role",
        runtime._asr_session_epoch,
        "glm",
    )
    await runtime._wait_asr_transcript_dispatch_idle()

    old_session.create_response.assert_not_awaited()
    new_session.create_response.assert_not_awaited()
    new_session.prepare_external_voice_turn.assert_awaited_once()
    new_session.submit_external_voice_turn.assert_awaited_once()


async def test_game_takeover_during_transcript_drops_stale_core_final() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "glm")
    transcript_started = asyncio.Event()
    release_transcript = asyncio.Event()

    async def block_transcript(*_args, **_kwargs) -> bool:
        transcript_started.set()
        await release_transcript.wait()
        return True

    runtime.handle_input_transcript.side_effect = block_transcript
    runtime.session.submit_external_voice_turn = AsyncMock()
    runtime._asr_runtime.suspend = AsyncMock()
    turn_token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    event = VoiceTranscriptEvent(
        turn_token=turn_token,
        provider="glm",
        text="belongs to Core",
    )
    dispatch_task = asyncio.create_task(runtime._dispatch_core_asr_transcript(event))
    await asyncio.wait_for(transcript_started.wait(), 1)

    await runtime._suspend_independent_voice_input_for_game()
    release_transcript.set()
    await asyncio.wait_for(dispatch_task, 1)

    runtime.session.submit_external_voice_turn.assert_not_awaited()
    runtime.session.create_response.assert_not_awaited()


async def test_status_delivery_failure_never_breaks_audio_runtime() -> None:
    runtime = _Runtime()
    runtime.send_status.side_effect = RuntimeError("socket closed")
    runtime._set_microphone_route("native")
    runtime.session.stream_audio = AsyncMock()
    identity = runtime._asr_runtime._capture_runtime_identity()

    await runtime._send_asr_status(
        "ASR_INDEPENDENT_READY",
        "glm",
        session_epoch=runtime._asr_session_epoch,
        expected_identity=identity,
    )
    await runtime._route_microphone_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )

    runtime.send_status.assert_awaited_once()
    runtime.session.stream_audio.assert_awaited_once()
    assert runtime._voice_input_pipeline_failed is False


async def test_old_core_close_cannot_clear_new_pipeline_or_provider() -> None:
    runtime = _Runtime()
    runtime_close_entered = asyncio.Event()
    release_runtime_close = asyncio.Event()

    async def block_runtime_close() -> None:
        runtime_close_entered.set()
        await release_runtime_close.wait()

    runtime._asr_runtime.stop_session = AsyncMock(side_effect=block_runtime_close)
    old_pipeline = runtime._voice_input_audio_pipeline
    old_pipeline.close = AsyncMock()
    runtime._independent_asr_provider = "old-provider"
    runtime._independent_asr_route_key = "old-core"
    runtime._set_microphone_route("independent")

    closing = asyncio.create_task(
        runtime._close_independent_asr(next_route_mode="blocked")
    )
    await asyncio.wait_for(runtime_close_entered.wait(), 1)
    new_pipeline = runtime._voice_input_audio_pipeline
    runtime._begin_asr_route_operation()
    runtime._independent_asr_provider = "new-provider"
    runtime._independent_asr_route_key = "new-core"
    runtime._set_microphone_route("independent")
    release_runtime_close.set()
    await asyncio.wait_for(closing, 1)

    old_pipeline.close.assert_awaited_once_with()
    assert runtime._voice_input_audio_pipeline is new_pipeline
    assert runtime._independent_asr_provider == "new-provider"
    assert runtime._independent_asr_route_key == "new-core"
    assert runtime._asr_route_mode == "independent"


async def test_stale_runtime_ready_result_cannot_replace_new_route(
    monkeypatch,
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()

    class BlockingBridge:
        def __init__(self) -> None:
            self.session_epoch = 0
            self.audio_generation = 0

        def capture_ingress_token(
            self,
            *,
            connection_id,
            lease_generation,
            route_generation,
        ):
            from main_logic.voice_turn.contracts import VoiceIngressToken

            return VoiceIngressToken(
                self.session_epoch,
                connection_id,
                lease_generation,
                route_generation,
                self.audio_generation,
            )

        async def stop_session(self) -> None:
            self.session_epoch += 1
            self.audio_generation += 1

        async def close(self) -> None:
            await self.stop_session()

        async def start(self, **_kwargs) -> AsrStartResult:
            source_epoch = self.session_epoch
            start_entered.set()
            await release_start.wait()
            return AsrStartResult(
                AsrStartStatus.READY,
                provider="old-provider",
                session_epoch=source_epoch,
            )

    runtime = _Runtime()
    bridge = BlockingBridge()
    object.__setattr__(runtime, "_asr_runtime", bridge)
    runtime.core_api_type = "old-core"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )

    starting = asyncio.create_task(runtime._start_independent_asr_if_enabled("audio"))
    await asyncio.wait_for(start_entered.wait(), 1)
    runtime._begin_asr_route_operation()
    bridge.session_epoch += 1
    runtime.core_api_type = "new-core"
    runtime._independent_asr_provider = "new-provider"
    runtime._independent_asr_route_key = "new-core"
    runtime._set_microphone_route("independent")
    release_start.set()
    await asyncio.wait_for(starting, 1)

    assert runtime._independent_asr_provider == "new-provider"
    assert runtime._independent_asr_route_key == "new-core"
    assert runtime._asr_route_mode == "independent"


async def test_old_native_send_failure_cannot_close_new_session() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("native")
    runtime.session_closed_by_server = False
    send_entered = asyncio.Event()
    release_send = asyncio.Event()

    async def fail_old_send(_pcm16) -> None:
        send_entered.set()
        await release_send.wait()
        raise RuntimeError("connection closed")

    old_session = type("OldOmni", (), {})()
    old_session.stream_audio = AsyncMock(side_effect=fail_old_send)
    runtime.session = old_session
    old_token = runtime._capture_native_ingress_token()
    old_send = asyncio.create_task(
        runtime._route_microphone_audio(
            b"\x01\x00",
            sample_rate_hz=16_000,
            ingress_token=old_token,
        )
    )
    await asyncio.wait_for(send_entered.wait(), 1)

    new_session = type("NewOmni", (), {})()
    new_session.stream_audio = AsyncMock()
    runtime.session = new_session
    release_send.set()
    await asyncio.wait_for(old_send, 1)

    assert runtime.session_closed_by_server is False
    assert runtime._omni_mic_audio_bytes == 0
    await runtime._route_microphone_audio(
        b"\x02\x00",
        sample_rate_hz=16_000,
        ingress_token=runtime._capture_native_ingress_token(),
    )
    new_session.stream_audio.assert_awaited_once_with(b"\x02\x00")
    assert runtime._omni_mic_audio_bytes == 2


async def test_current_native_send_failure_still_closes_route_once() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("native")
    runtime.session_closed_by_server = False
    runtime.session.stream_audio = AsyncMock(
        side_effect=RuntimeError("connection closed")
    )

    await runtime._route_microphone_audio(
        b"\x01\x00",
        sample_rate_hz=16_000,
        ingress_token=runtime._capture_native_ingress_token(),
    )

    runtime.session.stream_audio.assert_awaited_once_with(b"\x01\x00")
    assert runtime.session_closed_by_server is True
    assert runtime._omni_mic_audio_bytes == 0


async def test_partial_preview_requires_current_core_lease() -> None:
    runtime = _Runtime()
    websocket = type("WebSocket", (), {})()
    websocket.send_json = AsyncMock()
    runtime.websocket = websocket
    runtime._set_microphone_route("independent")
    epoch = runtime._asr_session_epoch
    token = VoiceTurnToken(
        ingress=runtime._capture_ingress_token(),
        turn_id=1,
    )
    stale_token = VoiceTurnToken(
        ingress=replace(token.ingress, session_epoch=epoch + 1),
        turn_id=token.turn_id,
    )

    runtime._voice_lease_owner = "game"
    await runtime._send_core_asr_preview(
        VoicePartialEvent(turn_token=token, text="game")
    )
    runtime._voice_lease_owner = "core"
    runtime._voice_lease_hard_muted = True
    await runtime._send_core_asr_preview(
        VoicePartialEvent(turn_token=token, text="muted")
    )
    runtime._voice_lease_hard_muted = False
    runtime._voice_lease_focus_suppressed = True
    await runtime._send_core_asr_preview(
        VoicePartialEvent(turn_token=token, text="focused")
    )
    runtime._voice_lease_focus_suppressed = False
    await runtime._send_core_asr_preview(
        VoicePartialEvent(turn_token=stale_token, text="stale")
    )
    await runtime._send_core_asr_preview(
        VoicePartialEvent(turn_token=token, text="current")
    )

    websocket.send_json.assert_awaited_once_with(
        {
            "type": "user_transcript_preview",
            "text": "current",
            "turn_id": f"asr-preview-{epoch}",
        }
    )


async def test_old_notifications_cannot_override_new_generation() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    first_send_entered = asyncio.Event()
    release_first_send = asyncio.Event()
    payloads = []

    async def ordered_send_status(payload: str) -> None:
        payloads.append(json.loads(payload))
        if len(payloads) == 1:
            first_send_entered.set()
            await release_first_send.wait()

    runtime.send_status = AsyncMock(side_effect=ordered_send_status)
    old_epoch = runtime._asr_session_epoch
    old_event = AsrLifecycleNotification(
        state="local_listen",
        provider="old-provider",
        session_epoch=old_epoch,
    )
    old_delivery = asyncio.create_task(runtime._send_core_asr_lifecycle(old_event))
    await asyncio.wait_for(first_send_entered.wait(), 1)

    runtime._asr_session_epoch += 1
    new_epoch = runtime._asr_session_epoch
    new_event = AsrLifecycleNotification(
        state="blocked",
        provider="new-provider",
        session_epoch=new_epoch,
    )
    new_delivery = asyncio.create_task(runtime._send_core_asr_lifecycle(new_event))
    release_first_send.set()
    await asyncio.wait_for(
        asyncio.gather(old_delivery, new_delivery),
        1,
    )
    await runtime._send_core_asr_lifecycle(old_event)
    await runtime._send_core_asr_status(
        AsrStatusEvent(
            code="ASR_OLD_READY",
            provider="old-provider",
            session_epoch=old_epoch,
        )
    )
    await runtime._send_core_asr_status(
        AsrStatusEvent(
            code="ASR_NEW_READY",
            provider="new-provider",
            session_epoch=new_epoch,
        )
    )

    assert [
        payload["details"]["state"]
        for payload in payloads
        if payload["code"] == "ASR_LIFECYCLE_STATE"
    ] == ["local_listen", "blocked"]
    assert payloads[-1]["code"] == "ASR_NEW_READY"
    latest_revision = payloads[-1]["details"]["lifecycle_revision"]
    assert payloads[-1]["details"] == {
        "provider": "new-provider",
        "session_epoch": new_epoch,
        "transport_generation": 0,
        "lifecycle_revision": latest_revision,
        "reason_code": None,
        "incident_id": None,
    }
    assert latest_revision > payloads[1]["details"]["lifecycle_revision"]
    assert payloads[1]["details"]["session_epoch"] == new_epoch


async def test_failure_event_only_blocks_current_generation() -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    current_epoch = runtime._asr_session_epoch

    await runtime._handle_core_asr_failure(
        AsrFailureEvent(
            code="ASR_INDEPENDENT_FAILED",
            provider="old-provider",
            session_epoch=current_epoch - 1,
        )
    )
    assert runtime._asr_route_mode == "independent"

    await runtime._handle_core_asr_failure(
        AsrFailureEvent(
            code="ASR_INDEPENDENT_FAILED",
            provider="current-provider",
            session_epoch=current_epoch,
        )
    )
    assert runtime._asr_route_mode == "blocked"


async def test_runtime_failure_from_a_live_route_still_revokes_the_lease() -> None:
    # Codex P2. The chokepoint refactor passed the PRE-transition identity tuple
    # as still_current, but that tuple carries _asr_route_mode (and
    # _microphone_route_generation, inside the ingress token) while the handler
    # sets the route to "blocked" two lines earlier. The predicate was therefore
    # false on ENTRY -- against the handler's own step -- so
    # _fail_closed_voice_route returned before revoking, leaving the recording
    # socket holding a live hardware microphone on a dead route. Only reachable
    # from a LIVE route, which is exactly the real runtime-failure case; an
    # already-blocked route happened to compare equal and masked it.
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    runtime._voice_lease_connection_id = "socket-a"
    runtime._voice_input_websocket = object()

    await runtime._handle_core_asr_failure(
        AsrFailureEvent(
            code="ASR_INDEPENDENT_FAILED",
            provider="current-provider",
            session_epoch=runtime._asr_session_epoch,
        )
    )

    assert runtime._asr_route_mode == "blocked"
    assert runtime._voice_lease_connection_id == ""


async def test_runtime_failure_still_fences_a_competing_newer_operation() -> None:
    # Re-basing the identity must not weaken the fence it exists for: a NEWER
    # route operation landing during this handler's own transition still has to
    # stop the revoke, because _revoke_voice_input_connection calls
    # _invalidate_asr_start() and would cancel that newer start.
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    runtime._voice_lease_connection_id = "socket-a"
    original_set_route = runtime._set_microphone_route

    def _set_route_then_supersede(mode: str) -> None:
        original_set_route(mode)
        runtime._begin_asr_route_operation()

    runtime._set_microphone_route = _set_route_then_supersede

    await runtime._handle_core_asr_failure(
        AsrFailureEvent(
            code="ASR_INDEPENDENT_FAILED",
            provider="current-provider",
            session_epoch=runtime._asr_session_epoch,
        )
    )

    assert runtime._voice_lease_connection_id == "socket-a"


async def test_runtime_failure_leaves_the_game_lease_alone() -> None:
    # The galgame route holds the mic through its built-in consumer route and tears
    # down via GAME_ROUTE_ENDED; re-basing the identity must not start
    # collaterally revoking it.
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    runtime._voice_lease_connection_id = "socket-a"
    runtime._voice_lease_owner = "game"

    await runtime._handle_core_asr_failure(
        AsrFailureEvent(
            code="ASR_INDEPENDENT_FAILED",
            provider="current-provider",
            session_epoch=runtime._asr_session_epoch,
        )
    )

    assert runtime._voice_lease_connection_id == "socket-a"


@pytest.mark.parametrize(
    "transition",
    [
        "hard_mute",
        "focus_suppress",
        "game_takeover",
        "lease_sync",
        "connection_replacement",
    ],
)
async def test_core_start_is_invalidated_by_mic_lease_transition(
    monkeypatch,
    transition: str,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    release_connect = asyncio.Event()

    class Candidate:
        def __init__(self) -> None:
            self.connect_started = asyncio.Event()
            self.is_ready = True
            self.close = AsyncMock()

        async def connect(self) -> None:
            self.connect_started.set()
            await release_connect.wait()

    candidate = Candidate()
    selection = SimpleNamespace(
        provider_key="qwen",
        endpointing_mode="provider",
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=selection),
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        MagicMock(return_value=candidate),
    )
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": True}),
    )
    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    runtime._voice_lease_connection_id = "connection"
    runtime._voice_lease_generation = 0

    starting = asyncio.create_task(runtime._start_independent_asr_if_enabled("audio"))
    await asyncio.wait_for(candidate.connect_started.wait(), 1)
    if transition == "connection_replacement":
        runtime._begin_voice_input_connection("replacement")
    elif transition == "lease_sync":
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
        )
    else:
        await runtime._handle_voice_input_control(transition, 1)
    release_connect.set()
    await asyncio.wait_for(starting, 1)

    assert runtime._asr_route_mode == "blocked"
    assert runtime._independent_asr_provider is None
    assert "ASR_INDEPENDENT_READY" not in str(runtime.send_status.await_args_list)
    candidate.close.assert_awaited_once_with()


async def test_settings_result_is_stale_after_connection_replacement(
    monkeypatch,
) -> None:
    settings_started = asyncio.Event()
    release_settings = asyncio.Event()

    async def load_settings(**_kwargs):
        settings_started.set()
        await release_settings.wait()
        return {"independentAsrEnabled": True}

    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    runtime._voice_lease_connection_id = "connection"
    runtime._voice_lease_generation = 0
    runtime._asr_runtime.start = AsyncMock(
        return_value=AsrStartResult(
            AsrStartStatus.READY,
            provider="qwen",
            session_epoch=runtime._asr_session_epoch,
        )
    )
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        load_settings,
    )

    starting = asyncio.create_task(runtime._start_independent_asr_if_enabled("audio"))
    await asyncio.wait_for(settings_started.wait(), 1)
    runtime._begin_voice_input_connection("replacement")
    release_settings.set()
    await asyncio.wait_for(starting, 1)

    runtime._asr_runtime.start.assert_not_awaited()
    assert runtime._asr_route_mode == "blocked"
    assert runtime._independent_asr_provider is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_cold_start_with_unclaimed_lease_still_routes(
    monkeypatch, enabled: bool
) -> None:
    """The bundled frontend flips the lease owner to "core" only after
    session_started, so route setup must not require owner=="core": gating
    the start on lease state would leave every cold start blocked."""

    runtime = _Runtime()
    runtime._voice_lease_owner = "none"
    runtime._voice_lease_synchronized = True
    runtime.core_api_type = "qwen"

    async def ready_start(**_kwargs) -> AsrStartResult:
        return AsrStartResult(
            AsrStartStatus.READY,
            provider="qwen",
            session_epoch=runtime._asr_session_epoch,
        )

    runtime._asr_runtime.start = AsyncMock(side_effect=ready_start)
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={"independentAsrEnabled": enabled}),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    if enabled:
        assert runtime._asr_route_mode == "independent"
        assert runtime._independent_asr_provider == "qwen"
    else:
        assert runtime._asr_route_mode == "native"


async def test_stale_start_abort_does_not_clobber_newer_start_placeholder(
    monkeypatch,
) -> None:
    """A stale start parked in its abort must not clear the blocked
    placeholder a newer start installed meanwhile: clearing it would make
    the newer start's fence fail before it even reaches the native
    fallback, leaving the route blocked with no failure status."""

    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    runtime._voice_lease_connection_id = "conn-A"
    runtime._voice_lease_generation = 0

    a_start_parked = asyncio.Event()
    release_a_start = asyncio.Event()
    a_abort_parked = asyncio.Event()
    release_a_abort = asyncio.Event()
    b_settings_parked = asyncio.Event()
    release_b_settings = asyncio.Event()
    start_calls: list[dict] = []

    async def fake_runtime_start(**kwargs):
        start_calls.append(kwargs)
        if len(start_calls) == 1:
            a_start_parked.set()
            await release_a_start.wait()
            return AsrStartResult(
                AsrStartStatus.FAILED,
                failure_code="ASR_START_STALE",
                session_epoch=runtime._asr_session_epoch,
            )
        return AsrStartResult(
            AsrStartStatus.READY,
            provider="qwen",
            session_epoch=runtime._asr_session_epoch,
        )

    async def fake_abort(reason):
        a_abort_parked.set()
        await release_a_abort.wait()

    runtime._asr_runtime.start = fake_runtime_start
    runtime._asr_runtime.abort = fake_abort
    runtime._asr_runtime.stop_session = AsyncMock()

    settings_calls = 0

    async def load_settings(**_kwargs):
        nonlocal settings_calls
        settings_calls += 1
        if settings_calls == 2:
            b_settings_parked.set()
            await release_b_settings.wait()
        return {"independentAsrEnabled": True}

    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        load_settings,
    )

    task_a = asyncio.create_task(runtime._start_independent_asr_if_enabled("audio"))
    await asyncio.wait_for(a_start_parked.wait(), 1)
    runtime._begin_voice_input_connection("conn-B")
    release_a_start.set()
    await asyncio.wait_for(a_abort_parked.wait(), 1)

    task_b = asyncio.create_task(runtime._start_independent_asr_if_enabled("audio"))
    await asyncio.wait_for(b_settings_parked.wait(), 1)
    assert runtime._independent_asr_route_key == "qwen"

    release_a_abort.set()
    await asyncio.wait_for(task_a, 1)
    assert runtime._independent_asr_route_key == "qwen"

    release_b_settings.set()
    await asyncio.wait_for(task_b, 1)

    assert len(start_calls) == 2
    assert runtime._asr_route_mode == "independent"
    assert runtime._independent_asr_provider == "qwen"


async def test_core_start_survives_benign_lease_transition(monkeypatch) -> None:
    """Owner flip / mute toggle / lease bump during the settings await are
    PCM-gating changes, not route operations; they must not abort the start
    (there is no retry or failure status on that path)."""

    settings_started = asyncio.Event()
    release_settings = asyncio.Event()

    async def load_settings(**_kwargs):
        settings_started.set()
        await release_settings.wait()
        return {"independentAsrEnabled": True}

    runtime = _Runtime()
    runtime._voice_lease_owner = "none"
    runtime._voice_lease_synchronized = True
    runtime.core_api_type = "qwen"

    async def ready_start(**_kwargs) -> AsrStartResult:
        return AsrStartResult(
            AsrStartStatus.READY,
            provider="qwen",
            session_epoch=runtime._asr_session_epoch,
        )

    runtime._asr_runtime.start = AsyncMock(side_effect=ready_start)
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        load_settings,
    )

    starting = asyncio.create_task(runtime._start_independent_asr_if_enabled("audio"))
    await asyncio.wait_for(settings_started.wait(), 1)
    runtime._voice_lease_owner = "core"
    runtime._voice_lease_hard_muted = True
    runtime._voice_lease_generation += 1
    release_settings.set()
    await asyncio.wait_for(starting, 1)

    runtime._asr_runtime.start.assert_awaited_once()
    assert runtime._asr_route_mode == "independent"
    assert runtime._independent_asr_provider == "qwen"


@pytest.mark.parametrize(
    "newer_transition",
    [
        "game_takeover",
        "hard_mute",
        "focus_suppress",
        "lease_generation",
        "connection_replacement",
    ],
)
async def test_game_release_resume_only_survives_pcm_gating_transitions(
    newer_transition: str,
) -> None:
    """Ownership loss during the release abort must skip resume; PCM-gating
    transitions (mute/focus/lease bump) must not, because resume has no other
    call site and skipping it leaves the runtime SUSPENDED for the session."""
    runtime = _Runtime()
    runtime._voice_lease_connection_id = "connection"
    runtime._voice_lease_generation = 1
    runtime._voice_lease_owner = "game"
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def abort(reason: str) -> None:
        if reason == "game_release":
            abort_started.set()
            await release_abort.wait()

    runtime._asr_runtime.abort = AsyncMock(side_effect=abort)
    runtime._asr_runtime.resume = AsyncMock()
    runtime._asr_runtime.suspend = AsyncMock()
    releasing = asyncio.create_task(
        runtime._apply_voice_lease_state(
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
            reason="game_release",
            force_abort=True,
        )
    )
    await asyncio.wait_for(abort_started.wait(), 1)

    if newer_transition == "connection_replacement":
        runtime._begin_voice_input_connection("replacement")
    elif newer_transition == "lease_generation":
        runtime._voice_lease_generation += 1
    elif newer_transition == "game_takeover":
        await runtime._apply_voice_lease_state(
            owner="game",
            hard_muted=False,
            focus_suppressed=False,
            reason="game_takeover",
            force_abort=True,
        )
    elif newer_transition == "hard_mute":
        await runtime._apply_voice_lease_state(
            owner="core",
            hard_muted=True,
            focus_suppressed=False,
            reason="hard_mute",
            force_abort=True,
        )
    else:
        await runtime._apply_voice_lease_state(
            owner="core",
            hard_muted=False,
            focus_suppressed=True,
            reason="focus_suppress",
            force_abort=True,
        )
    release_abort.set()
    await asyncio.wait_for(releasing, 1)

    if newer_transition in {"game_takeover", "connection_replacement"}:
        runtime._asr_runtime.resume.assert_not_awaited()
    else:
        runtime._asr_runtime.resume.assert_awaited_once_with("game_release")


async def test_current_game_release_still_aborts_and_resumes_once() -> None:
    runtime = _Runtime()
    runtime._voice_lease_connection_id = "connection"
    runtime._voice_lease_generation = 1
    runtime._voice_lease_owner = "game"
    runtime._asr_runtime.abort = AsyncMock()
    runtime._asr_runtime.resume = AsyncMock()

    await runtime._apply_voice_lease_state(
        owner="core",
        hard_muted=False,
        focus_suppressed=False,
        reason="game_release",
        force_abort=True,
    )

    runtime._asr_runtime.abort.assert_awaited_once_with("game_release")
    runtime._asr_runtime.resume.assert_awaited_once_with("game_release")


@pytest.mark.parametrize("notification", ["status", "lifecycle"])
async def test_notification_waiting_on_lock_drops_same_epoch_stale_identity(
    notification: str,
) -> None:
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    current_epoch = runtime._asr_session_epoch
    await runtime._asr_notification_lock.acquire()
    if notification == "status":
        event = AsrStatusEvent(
            code="ASR_OLD_READY",
            provider="old-provider",
            session_epoch=current_epoch,
        )
        delivery = asyncio.create_task(runtime._send_core_asr_status(event))
    else:
        event = AsrLifecycleNotification(
            state="local_listen",
            provider="old-provider",
            session_epoch=current_epoch,
        )
        delivery = asyncio.create_task(runtime._send_core_asr_lifecycle(event))
    await asyncio.sleep(0)

    runtime._asr_audio_generation += 1
    runtime._asr_notification_lock.release()
    await asyncio.wait_for(delivery, 1)

    runtime.send_status.assert_not_awaited()
    assert runtime._asr_route_mode == "independent"


async def test_failure_cancellation_can_publish_without_notification_deadlock() -> (
    None
):
    runtime = _Runtime()
    runtime._set_microphone_route("independent")
    current_epoch = runtime._asr_session_epoch

    async def cancellation_wait_idle() -> None:
        assert runtime._asr_notification_lock.locked() is False
        await runtime._send_core_asr_status(
            AsrStatusEvent(
                code="ASR_CANCEL_CLEANUP",
                provider="plugin-consumer",
                session_epoch=current_epoch,
            )
        )

    runtime._voice_input_registry.wait_idle = AsyncMock(
        side_effect=cancellation_wait_idle
    )

    await asyncio.wait_for(
        runtime._handle_core_asr_failure(
            AsrFailureEvent(
                code="ASR_INDEPENDENT_FAILED",
                provider="current-provider",
                session_epoch=current_epoch,
            )
        ),
        1,
    )

    assert "ASR_CANCEL_CLEANUP" in str(runtime.send_status.await_args_list)


def _lease_resync_statuses(runtime: _Runtime) -> list[dict]:
    statuses = [
        json.loads(call.args[0]) for call in runtime.send_status.await_args_list
    ]
    return [
        status
        for status in statuses
        if status["code"] == "VOICE_INPUT_LEASE_RESYNC_REQUIRED"
    ]


def _mic_frame() -> dict:
    return {"input_type": "audio", "sample_rate_hz": 16_000, "data": [1] * 160}


async def test_unsynchronized_pcm_signals_lease_resync_once_per_state() -> None:
    runtime = _Runtime()
    assert runtime._begin_voice_input_connection("chat-window") is True

    for _ in range(3):
        await runtime._enqueue_audio_stream_data(_mic_frame())

    resync = _lease_resync_statuses(runtime)
    assert len(resync) == 1
    assert resync[0]["details"]["reason"] == "lease_unsynchronized"
    assert runtime._audio_stream_queue.empty()
    assert runtime._audio_stream_worker_task is None

    assert runtime._begin_voice_input_connection("pet-window") is True
    for _ in range(2):
        await runtime._enqueue_audio_stream_data(_mic_frame())

    assert len(_lease_resync_statuses(runtime)) == 2
    assert runtime._audio_stream_queue.empty()


async def test_cancelled_lease_resync_send_retries_same_episode() -> None:
    runtime = _Runtime()
    assert runtime._begin_voice_input_connection("chat-window") is True
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def block_send(_message: str) -> None:
        send_started.set()
        await release_send.wait()

    runtime.send_status = AsyncMock(side_effect=block_send)
    first = asyncio.create_task(runtime._maybe_signal_voice_lease_resync())
    await asyncio.wait_for(send_started.wait(), 1)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert runtime._voice_lease_resync_signal_state is None
    runtime.send_status = AsyncMock()
    await runtime._maybe_signal_voice_lease_resync()

    runtime.send_status.assert_awaited_once()
    assert runtime._voice_lease_resync_signal_state is not None


async def test_lease_resync_rearms_for_new_microphone_route_generation() -> None:
    runtime = _Runtime()
    assert runtime._begin_voice_input_connection("chat-window") is True

    await runtime._maybe_signal_voice_lease_resync()
    first_episode = runtime._voice_lease_resync_signal_state
    assert first_episode is not None
    assert first_episode[-1] == runtime._microphone_route_generation

    runtime._set_microphone_route("native")
    assert runtime._voice_lease_resync_signal_state is None
    await runtime._maybe_signal_voice_lease_resync()
    native_episode = runtime._voice_lease_resync_signal_state
    assert native_episode is not None

    runtime._set_microphone_route("native")
    await runtime._maybe_signal_voice_lease_resync()
    assert runtime._voice_lease_resync_signal_state == native_episode
    assert runtime.send_status.await_count == 2

    runtime._set_microphone_route("blocked")

    await runtime._maybe_signal_voice_lease_resync()
    second_episode = runtime._voice_lease_resync_signal_state
    assert second_episode is not None
    assert second_episode != first_episode
    assert second_episode[-1] == runtime._microphone_route_generation
    assert runtime.send_status.await_count == 3


async def test_blocked_text_notice_commits_only_for_current_connection() -> None:
    runtime = _Runtime()
    runtime.input_mode = "text"
    assert runtime._begin_voice_input_connection("chat-window") is True
    runtime._set_microphone_route("blocked")
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def block_send(_message: str) -> None:
        send_started.set()
        await release_send.wait()

    runtime.send_status = AsyncMock(side_effect=block_send)
    first = asyncio.create_task(
        runtime._maybe_signal_blocked_text_mode_microphone()
    )
    await asyncio.wait_for(send_started.wait(), 1)

    assert runtime._begin_voice_input_connection("pet-window") is True
    release_send.set()
    await asyncio.wait_for(first, 1)

    assert runtime._blocked_text_mode_microphone_signal_state is None
    runtime.send_status = AsyncMock()
    await runtime._maybe_signal_blocked_text_mode_microphone()

    runtime.send_status.assert_awaited_once()
    assert runtime._blocked_text_mode_microphone_signal_state is not None


async def test_voice_control_status_resolves_owner_after_display_delivery() -> None:
    runtime = _Runtime()
    voice_owner = None

    async def deliver_display(_message: str) -> bool:
        nonlocal voice_owner
        voice_owner = object()
        return True

    runtime.send_status = AsyncMock(side_effect=deliver_display)
    runtime._voice_owner_socket = MagicMock(side_effect=lambda: voice_owner)
    runtime._send_to_voice_owner = AsyncMock(side_effect=lambda _payload: voice_owner)

    delivered = await runtime._send_voice_control_status("lease changed")

    assert delivered == (True, True)
    runtime._send_to_voice_owner.assert_awaited_once_with(
        {"type": "status", "message": "lease changed"}
    )


async def test_blocked_text_episode_keeps_session_identity_reference() -> None:
    runtime = _Runtime()
    runtime.input_mode = "text"
    runtime._set_microphone_route("blocked")
    session = runtime.session

    episode = runtime._blocked_text_mode_microphone_episode()

    assert episode is not None
    assert episode[-1] is session


async def test_cancelled_blocked_text_notice_retries_same_episode() -> None:
    runtime = _Runtime()
    runtime.input_mode = "text"
    assert runtime._begin_voice_input_connection("chat-window") is True
    runtime._set_microphone_route("blocked")
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def block_send(_message: str) -> None:
        send_started.set()
        await release_send.wait()

    runtime.send_status = AsyncMock(side_effect=block_send)
    first = asyncio.create_task(
        runtime._maybe_signal_blocked_text_mode_microphone()
    )
    await asyncio.wait_for(send_started.wait(), 1)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert runtime._blocked_text_mode_microphone_signal_state is None
    runtime.send_status = AsyncMock()
    await runtime._maybe_signal_blocked_text_mode_microphone()

    runtime.send_status.assert_awaited_once()
    assert runtime._blocked_text_mode_microphone_signal_state is not None


async def test_synchronized_none_owner_pcm_signals_lease_resync() -> None:
    runtime = _Runtime()
    assert runtime._begin_voice_input_connection("chat-window") is True
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="none",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )

    for _ in range(2):
        await runtime._enqueue_audio_stream_data(_mic_frame())

    resync = _lease_resync_statuses(runtime)
    assert len(resync) == 1
    assert resync[0]["details"]["reason"] == "owner_none"
    assert runtime._audio_stream_queue.empty()


async def test_hard_muted_pcm_never_signals_lease_resync() -> None:
    runtime = _Runtime()
    assert runtime._begin_voice_input_connection("chat-window") is True
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="core",
            hard_muted=True,
            focus_suppressed=False,
        )
        is True
    )

    for _ in range(2):
        await runtime._enqueue_audio_stream_data(_mic_frame())

    assert _lease_resync_statuses(runtime) == []
    assert runtime._audio_stream_queue.empty()


async def test_game_owner_pcm_never_signals_lease_resync() -> None:
    runtime = _Runtime()
    assert runtime._begin_voice_input_connection("chat-window") is True
    assert (
        await runtime._handle_voice_input_control(
            "lease_sync",
            1,
            owner="game",
            hard_muted=False,
            focus_suppressed=False,
        )
        is True
    )

    for _ in range(2):
        await runtime._enqueue_audio_stream_data(_mic_frame())

    assert _lease_resync_statuses(runtime) == []
    assert runtime._audio_stream_queue.empty()


async def test_noise_reduction_disabled_reaches_pipeline_audio_processor(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    created: list[dict] = []

    class _RecordingProcessor:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)
            self.speech_probability = 0.0
            self.rnnoise_available = False

        def process_chunk(self, _audio_bytes: bytes) -> bytes:
            return b""

        def close(self) -> None:
            return None

    monkeypatch.setattr(audio_input_module, "AudioProcessor", _RecordingProcessor)
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(
            return_value={
                "independentAsrEnabled": False,
                "noiseReductionEnabled": False,
            }
        ),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._voice_input_noise_reduction_enabled is False
    assert runtime._voice_input_audio_pipeline.nr_enabled is False
    await runtime._voice_input_audio_pipeline.process(
        b"\x01\x00" * 480,
        sample_rate_hz=48_000,
    )
    assert created[-1]["noise_reduce_enabled"] is False

    started_pipeline = runtime._voice_input_audio_pipeline
    await runtime._close_independent_asr(next_route_mode="blocked")

    assert runtime._voice_input_audio_pipeline is not started_pipeline
    assert runtime._voice_input_audio_pipeline.nr_enabled is False
    await runtime._voice_input_audio_pipeline.process(
        b"\x01\x00" * 480,
        sample_rate_hz=48_000,
    )
    assert len(created) == 2
    assert created[-1]["noise_reduce_enabled"] is False


async def test_settings_read_failure_keeps_noise_reduction_enabled(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    runtime.core_api_type = "gemini"
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(side_effect=RuntimeError("settings unavailable")),
    )

    await runtime._start_independent_asr_if_enabled("audio")

    assert runtime._voice_input_noise_reduction_enabled is True
    assert runtime._voice_input_audio_pipeline.nr_enabled is True


async def test_idle_backpressure_trailing_activity_is_dropped_cleanly(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, detector = (
        await _start_runtime_with_callback_candidates(
            monkeypatch,
            candidate_count=1,
        )
    )
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    assert lifecycle is not None
    token = runtime._capture_ingress_token()
    component._asr_current_ingress_token = token
    on_activity = callbacks[0]["on_speech_activity"]
    assert callable(on_activity)

    await component._handle_audio_ingress_backpressure(token)

    # The idle branch bumps the audio generation but keeps the session
    # adopted so genuinely new speech keeps working.
    assert lifecycle.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    assert component._asr_session is sessions[0]
    assert not component._ingress_token_matches(token)

    # Trailing session-side speech events with the stale generation must be
    # dropped cleanly: without the identity gate the first event corrupts the
    # lifecycle toward ACTIVE and the second raises an uncaught
    # ASR_INGRESS_TOKEN_REQUIRED into the provider adapter.
    await on_activity(SpeechActivityEvent.SPEECH_STARTED)
    await on_activity(SpeechActivityEvent.SPEECH_STARTED)

    assert lifecycle.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    assert component._asr_turn_prepared is False
    runtime.session.handle_interruption.assert_not_awaited()
    runtime.handle_new_message.assert_not_awaited()
    assert all(
        "ASR_INDEPENDENT_FAILED" not in call.args[0]
        for call in runtime.send_status.await_args_list
    )


async def test_idle_backpressure_new_speech_still_wakes_adopted_session(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, detector = (
        await _start_runtime_with_callback_candidates(
            monkeypatch,
            candidate_count=1,
        )
    )
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    assert lifecycle is not None
    stale_token = runtime._capture_ingress_token()
    component._asr_current_ingress_token = stale_token
    on_activity = callbacks[0]["on_speech_activity"]

    await component._handle_audio_ingress_backpressure(stale_token)

    # New speech re-arms the current ingress token through submit() before
    # the provider observes it; the adopted session must then wake normally.
    component._asr_current_ingress_token = runtime._capture_ingress_token()
    await on_activity(SpeechActivityEvent.SPEECH_STARTED)

    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert component._asr_turn_prepared is True
    runtime.handle_new_message.assert_awaited_once()


async def test_accepted_final_dropped_by_generation_bump_abandons_turn(
    monkeypatch,
) -> None:
    runtime, sessions, callbacks, detector = (
        await _start_runtime_with_callback_candidates(
            monkeypatch,
            candidate_count=1,
        )
    )
    component = runtime._asr_runtime
    lifecycle = component._asr_lifecycle
    assert lifecycle is not None
    runtime.session.abandon_external_voice_turn = MagicMock()
    component._asr_current_ingress_token = runtime._capture_ingress_token()
    epoch = component._asr_session_epoch
    on_activity = callbacks[0]["on_speech_activity"]
    on_final = callbacks[0]["on_input_transcript"]

    await on_activity(SpeechActivityEvent.SPEECH_STARTED)
    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    sealed_turn_id = lifecycle.snapshot.turn_id
    await component._handle_independent_asr_endpoint(epoch)
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING

    await on_final("hello world")

    # The final was accepted, but the generation moves on before the serial
    # transcript dispatcher delivers the queued envelope.
    component._asr_audio_generation += 1
    await component.wait_transcript_idle()

    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{epoch}-{sealed_turn_id}"
    )


async def test_accepted_final_identity_loss_before_dispatch_abandons_turn() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "glm")
    runtime.session.abandon_external_voice_turn = MagicMock()
    component = runtime._asr_runtime
    epoch = component._asr_session_epoch
    await _start_and_seal_turn(runtime, "glm")
    sealed_turn_id = component._asr_lifecycle.snapshot.turn_id
    lease = component._asr_smart_turn_lease
    assert lease is not None

    async def bumping_release() -> None:
        component._asr_audio_generation += 1

    lease.release = bumping_release

    await runtime._handle_independent_asr_final("hello", epoch, "glm")
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.handle_input_transcript.assert_not_awaited()
    runtime.session.abandon_external_voice_turn.assert_called_once_with(
        f"asr-{epoch}-{sealed_turn_id}"
    )


async def test_failed_lease_release_does_not_skip_accepted_final_delivery() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "glm")
    component = runtime._asr_runtime
    component._asr_lifecycle.provider_policy = replace(
        component._asr_lifecycle.provider_policy,
        warm_transport_ms=60_000,
    )
    epoch = component._asr_session_epoch
    await _start_and_seal_turn(runtime, "glm")
    lease = component._asr_smart_turn_lease
    assert lease is not None

    async def raising_release() -> None:
        raise RuntimeError("release boom")

    lease.release = raising_release

    await runtime._handle_independent_asr_final("hello", epoch, "glm")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert component._asr_smart_turn_lease is None
    assert (
        component._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    )
    runtime.handle_input_transcript.assert_awaited_once_with(
        "hello",
        is_voice_source=True,
        source="independent_asr",
        metadata={"provider": "glm"},
        source_game_route_identity=None,
    )
    assert component._asr_warm_expiry_task is not None
    component._asr_warm_expiry_task.cancel()


async def test_transport_restart_task_failure_is_logged(caplog) -> None:
    runtime = _Runtime()
    component = runtime._asr_runtime

    async def failing_restart() -> None:
        raise RuntimeError("restart boom")

    component._restart_transport = failing_restart
    with caplog.at_level(logging.ERROR, logger="main_logic.asr_client._infra"):
        component._ensure_transport_restart_task()
        task = component._asr_transport_task
        assert task is not None
        await asyncio.wait([task])
        await asyncio.sleep(0)

    assert "independent-asr-transport-restart" in caplog.text
    assert "restart boom" in caplog.text


@pytest.mark.parametrize(
    ("endpointing_mode", "expects_micro_event_config"),
    [("provider", True), ("manual", False)],
)
async def test_start_resolves_selection_off_event_loop(
    monkeypatch,
    endpointing_mode: str,
    expects_micro_event_config: bool,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    selection = _selection("qwen", endpointing_mode)
    resolver_threads: list[threading.Thread] = []

    def resolver(core_type: str):
        assert core_type == "qwen"
        resolver_threads.append(threading.current_thread())
        return selection

    session = SimpleNamespace(
        is_ready=True,
        connect=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(runtime_module, "_resolve_asr_selection", resolver)
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        lambda _core_type, **_kwargs: session,
    )
    detector_factory = MagicMock(return_value=_ReadyDetector())
    monkeypatch.setattr(runtime_module, "DetectorRuntime", detector_factory)

    result = await runtime._asr_runtime.start(
        route_key="qwen",
        resource_optimization_enabled=False,
    )

    assert result.status is AsrStartStatus.READY
    assert len(resolver_threads) == 1
    assert resolver_threads[0] is not threading.main_thread()
    assert (
        detector_factory.call_args.kwargs["resource_optimization_enabled"] is False
    )
    assert detector_factory.call_args.kwargs["speaker_shadow"] is None
    micro_event_config = detector_factory.call_args.kwargs[
        "provider_micro_event_config"
    ]
    if expects_micro_event_config:
        assert micro_event_config.mode == "shadow"
        assert micro_event_config.calibration_revision is None
        assert micro_event_config.maximum_silero_span_ms == 384
        assert micro_event_config.maximum_post_start_onset_windows == 4
        assert (
            micro_event_config.maximum_rnnoise_active_run_upper_bound_ms == 160
        )
    else:
        assert micro_event_config is None


@pytest.mark.parametrize("factory_fails", [False, True])
async def test_speaker_shadow_factory_is_lightweight_sync_and_fail_open(
    monkeypatch,
    factory_fails: bool,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    selection = _selection("qwen", "provider")
    session = SimpleNamespace(
        is_ready=True,
        connect=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        lambda _core_type: selection,
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        lambda _core_type, **_kwargs: session,
    )
    detector_factory = MagicMock(return_value=_ReadyDetector())
    monkeypatch.setattr(runtime_module, "DetectorRuntime", detector_factory)
    shadow = SimpleNamespace(close=AsyncMock())
    factory_threads: list[threading.Thread] = []

    def factory():
        factory_threads.append(threading.current_thread())
        if factory_fails:
            raise RuntimeError("missing shadow backend")
        return shadow

    result = await runtime._asr_runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
        speaker_shadow_factory=factory,
    )

    assert result.status is AsrStartStatus.READY
    assert factory_threads == [threading.main_thread()]
    assert detector_factory.call_args.kwargs["speaker_shadow"] is (
        None if factory_fails else shadow
    )


async def test_start_installs_latest_verifier_published_during_connect(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module
    from main_logic.asr_client.speaker_verifier_contracts import (
        SpeakerVerifierAuthority,
        SpeakerVerifierInstallOutcome,
        SpeakerVerifierSpec,
    )

    runtime = _Runtime()
    runtime.core_api_type = "qwen"
    runtime.input_mode = "audio"
    runtime.is_active = False
    selection = _selection("qwen", "provider")
    connect_started = asyncio.Event()
    connect_release = asyncio.Event()

    async def connect() -> None:
        connect_started.set()
        await connect_release.wait()

    session = SimpleNamespace(
        is_ready=True,
        connect=connect,
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        lambda _core_type: selection,
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        lambda _core_type, **_kwargs: session,
    )
    monkeypatch.setattr(
        core_module,
        "aload_global_conversation_settings",
        AsyncMock(return_value={
            "independentAsrEnabled": True,
            "voiceInputResourceOptimizationEnabled": False,
        }),
    )
    stale_shadow = SimpleNamespace(close=AsyncMock())
    current_shadow = SimpleNamespace(close=AsyncMock())
    stale_factory = MagicMock(return_value=stale_shadow)
    current_factory = MagicMock(return_value=current_shadow)

    def spec(factory, revision):
        authority = SpeakerVerifierAuthority()
        authority.commit()
        return SpeakerVerifierSpec(
            revision, revision, True, True, authority,
            lambda _runtime, _identity: factory,
        )

    assert (await runtime.set_speaker_verifier_spec(spec(stale_factory, "old"))).outcome is SpeakerVerifierInstallOutcome.DEFERRED_ROUTE

    start_task = asyncio.create_task(
        runtime._start_independent_asr_if_enabled("audio")
    )
    try:
        await asyncio.wait_for(connect_started.wait(), 1.0)
        pending = await runtime.set_speaker_verifier_spec(spec(current_factory, "current"))
        assert pending.outcome is SpeakerVerifierInstallOutcome.DEFERRED_ROUTE
        current_factory.assert_not_called()
        connect_release.set()
        await asyncio.wait_for(start_task, 1.0)

        assert runtime._asr_route_mode == "independent"
        stale_factory.assert_not_called()
        current_factory.assert_called_once_with()
        assert runtime._asr_runtime._asr_detector._speaker_shadow is current_shadow
        assert runtime.speaker_verifier_installation_status("current").outcome is SpeakerVerifierInstallOutcome.INSTALLED
    finally:
        connect_release.set()
        await asyncio.gather(start_task, return_exceptions=True)
        await runtime._asr_runtime.close()


async def test_failed_detector_construction_closes_created_speaker_shadow(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    runtime = _Runtime()
    selection = _selection("qwen", "provider")
    session = SimpleNamespace(
        is_ready=True,
        connect=AsyncMock(),
        close=AsyncMock(),
    )
    shadow = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        lambda _core_type: selection,
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        lambda _core_type, **_kwargs: session,
    )
    monkeypatch.setattr(
        runtime_module,
        "DetectorRuntime",
        MagicMock(side_effect=RuntimeError("detector construction failed")),
    )

    result = await runtime._asr_runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
        speaker_shadow_factory=lambda: shadow,
    )

    assert result.status in {AsrStartStatus.FAILED, AsrStartStatus.UNAVAILABLE}
    shadow.close.assert_awaited_once_with()


async def test_teardown_routines_share_one_turn_state_reset() -> None:
    import ast
    import inspect as inspect_module

    from main_logic.asr_client import runtime as runtime_module

    source = inspect_module.getsource(runtime_module.IndependentAsrRuntime)
    tree = ast.parse(source)
    class_node = tree.body[0]
    for method_name in (
        "_detach_independent_asr",
        "_abort_transport",
        "_handle_independent_asr_error",
    ):
        method = next(
            node
            for node in class_node.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == method_name
        )
        calls = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert "_reset_asr_turn_state" in calls, method_name


def test_hot_swap_replay_damage_accounts_for_rebound_frames() -> None:
    # Codex P2. Cached pre-swap frames carry a stale route generation, so replay
    # rebinds them onto the new session -- but only the local SEND token is
    # rebound; the frame objects appended to damaged_frames keep their original
    # token. The final `any(_ingress_token_matches(frame.token) ...)` check was
    # therefore false, _invalidate_interrupted_voice_turn was skipped, and a
    # prefix that had already reached the new provider stayed in place: later
    # speech got concatenated across the missing tail instead of the damaged
    # turn being cleared.
    #
    # Structural, and deliberately so: driving _flush_hot_swap_audio_cache to a
    # mid-replay failure needs a cache, live session, route mode and token
    # generations. What this pins is that the rebind records current-route
    # damage and that the damage check consults it.
    import inspect

    from main_logic.core import asr_runtime as asr_runtime_module

    source = inspect.getsource(asr_runtime_module.AsrRuntimeMixin._flush_hot_swap_audio_cache)

    assert "rebound_to_current_route = False" in source, (
        "the flush must track whether any frame was rebound onto the live route"
    )
    assert "nonlocal rebound_to_current_route" in source, (
        "replay_frames must be able to record the rebind"
    )
    # Set at the rebind, consulted at the damage check, in that order.
    set_at = source.index("rebound_to_current_route = True")
    # Anchor on the damage condition itself: a bare-name match also hits the
    # `nonlocal` declaration, which sits BEFORE the rebind and inverted this
    # ordering assertion into a false failure.
    checked_at = source.index("if damaged_frames and (")
    assert source.index("token = rebound") < set_at, (
        "the flag belongs with the rebind it records"
    )
    assert set_at < checked_at
    assert "_invalidate_interrupted_voice_turn" in source[checked_at:], (
        "the damage check must still be what gates the invalidation"
    )


async def test_provider_final_preserves_unconfirmed_successor_pcm_as_pre_roll() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    detector = runtime._asr_detector
    assert isinstance(detector, _ReadyDetector)
    detector.complete_provider_candidate.return_value = True
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    successor_pcm = b"\x02\x00" * 160
    assert runtime._asr_lifecycle.accept_audio(
        successor_pcm,
        sample_rate_hz=16_000,
    ).disposition is AudioDisposition.BUFFER

    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    decision = runtime._asr_lifecycle.accept_audio(
        b"\x03\x00" * 160,
        sample_rate_hz=16_000,
    )

    assert decision.disposition is AudioDisposition.FORWARD_WITH_PRE_ROLL
    assert decision.pre_roll.startswith(successor_pcm)
    detector.complete_provider_candidate.assert_awaited_once()


async def test_provider_fence_failure_forwards_final_without_speaker_authority() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    detector = runtime._asr_detector
    assert isinstance(detector, _ReadyDetector)
    detector.complete_provider_candidate.return_value = None
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    turn_token = runtime._asr_lifecycle.current_turn_token
    assert turn_token is not None
    final_key = FinalKey.from_turn(turn_token)
    dispatcher = runtime._asr_transcript_dispatcher
    dispatcher.resolve_reserved = MagicMock(wraps=dispatcher.resolve_reserved)
    await runtime._handle_independent_asr_endpoint(epoch)

    await runtime._handle_independent_asr_final(
        "must-not-publish",
        epoch,
        "openai",
    )
    await runtime._wait_asr_transcript_dispatch_idle()

    statuses = [json.loads(call.args[0]) for call in runtime.send_status.await_args_list]
    codes = [payload["code"] for payload in statuses]
    assert "ASR_ENDPOINTING_FAILED" not in codes
    runtime.handle_input_transcript.assert_awaited_once_with(
        "must-not-publish",
        is_voice_source=True,
        source="independent_asr",
        metadata={"provider": "openai"},
        source_game_route_identity=None,
    )
    assert final_key in dispatcher._resolved
    assert final_key not in dispatcher._reservations
    resolution = dispatcher.resolve_reserved.call_args
    assert resolution.args == (final_key, AdmissionDisposition.FORWARD)
    assert resolution.kwargs["envelope"].final_key == final_key
    assert runtime._asr_route_mode == "independent"


async def test_stale_provider_endpoint_abandons_local_final_reservation() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    component = runtime._asr_runtime
    dispatcher = component._asr_transcript_dispatcher
    dispatcher.resolve_reserved = MagicMock(wraps=dispatcher.resolve_reserved)
    epoch = component._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    turn_token = component._asr_lifecycle.current_turn_token
    assert turn_token is not None
    final_key = FinalKey.from_turn(turn_token)
    assert final_key in dispatcher._reservations

    # Preparation is current and owns a reservation. Only the subsequent
    # endpoint callback becomes stale; otherwise the post-open identity fence
    # correctly refuses to reserve and this test never reaches its subject.
    component._runtime_identity_matches = MagicMock(return_value=False)

    await runtime._handle_independent_asr_endpoint(epoch)

    async def wait_for_resolution() -> None:
        while final_key not in dispatcher._resolved:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_resolution(), 1)
    await dispatcher.wait_idle()

    assert final_key not in dispatcher._reservations
    assert final_key not in component._asr_admission_reservation_dispatchers
    resolution = dispatcher.resolve_reserved.call_args
    assert resolution.args == (final_key, AdmissionDisposition.ABANDON)
    assert resolution.kwargs == {"envelope": None}
    assert component._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE


async def test_provider_successor_discard_failure_fails_closed_once() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    detector = runtime._asr_detector
    assert isinstance(detector, _ReadyDetector)
    detector.discard_provider_successor.side_effect = RuntimeError("private failure")
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    ingress_token = runtime._asr_runtime._asr_current_ingress_token
    assert ingress_token is not None

    await runtime._handle_audio_ingress_backpressure(
        ingress_token,
        observed_state=VoiceLifecycleState.DRAINING,
    )

    statuses = [json.loads(call.args[0]) for call in runtime.send_status.await_args_list]
    codes = [payload["code"] for payload in statuses]
    assert codes.count("ASR_ENDPOINTING_FAILED") == 1
    assert codes.count("ASR_INGRESS_BACKPRESSURE") == 0
    assert runtime._asr_session_epoch == epoch + 1
    assert runtime._asr_route_mode == "blocked"
    assert "private failure" not in str(runtime.send_status.await_args_list)


async def test_provider_final_lock_then_overflow_preserves_accepted_final() -> None:
    runtime = _Runtime()
    asr = type(
        "Asr",
        (),
        {
            "is_ready": True,
            "stream_audio": AsyncMock(),
            "close": AsyncMock(),
        },
    )()
    runtime._asr_session = asr
    _install_ready_lifecycle(runtime, "openai")
    detector = runtime._asr_detector
    assert isinstance(detector, _ReadyDetector)
    completion_started = asyncio.Event()
    completion_release = asyncio.Event()

    async def complete_provider_candidate(_fence) -> bool:
        completion_started.set()
        await completion_release.wait()
        return False

    detector.complete_provider_candidate.side_effect = complete_provider_candidate
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    runtime._asr_lifecycle.accept_audio(
        b"\x01\x00" * 160,
        sample_rate_hz=16_000,
    )
    ingress_token = runtime._asr_runtime._asr_current_ingress_token
    assert ingress_token is not None

    final_task = asyncio.create_task(
        runtime._handle_independent_asr_final("first", epoch, "openai")
    )
    await asyncio.wait_for(completion_started.wait(), 1)
    overflow_task = asyncio.create_task(
        runtime._handle_audio_ingress_backpressure(
            ingress_token,
            observed_state=VoiceLifecycleState.DRAINING,
        )
    )
    await asyncio.sleep(0)
    assert final_task.done() is False
    assert overflow_task.done() is False
    assert runtime._asr_final_lock.locked()
    completion_release.set()
    await asyncio.gather(final_task, overflow_task)
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.handle_input_transcript.assert_awaited_once_with(
        "first",
        is_voice_source=True,
        source="independent_asr",
        metadata={"provider": "openai"},
        source_game_route_identity=None,
    )
    assert runtime._asr_lifecycle.has_pending_turn is False
    assert runtime._asr_sealed_turn_token is None


async def test_provider_overflow_lock_then_final_preserves_accepted_final() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    detector = runtime._asr_detector
    assert isinstance(detector, _ReadyDetector)
    discard_started = asyncio.Event()
    discard_release = asyncio.Event()

    async def discard_provider_successor(_fence) -> bool:
        discard_started.set()
        await discard_release.wait()
        return True

    detector.discard_provider_successor.side_effect = discard_provider_successor
    detector.complete_provider_candidate.return_value = False
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    sealed_token = runtime._asr_sealed_turn_token
    assert sealed_token is not None
    final_key = FinalKey.from_turn(sealed_token.turn)
    dispatcher = runtime._asr_transcript_dispatcher
    dispatcher.resolve_reserved = MagicMock(wraps=dispatcher.resolve_reserved)
    ingress_token = runtime._asr_runtime._asr_current_ingress_token
    assert ingress_token is not None

    overflow_task = asyncio.create_task(
        runtime._handle_audio_ingress_backpressure(
            ingress_token,
            observed_state=VoiceLifecycleState.DRAINING,
        )
    )
    await asyncio.wait_for(discard_started.wait(), 1)
    final_task = asyncio.create_task(
        runtime._handle_independent_asr_final("first", epoch, "openai")
    )
    await asyncio.sleep(0)
    assert final_task.done() is False
    discard_release.set()
    await asyncio.gather(overflow_task, final_task)
    await runtime._wait_asr_transcript_dispatch_idle()

    detector.discard_provider_successor.assert_awaited_once()
    detector.complete_provider_candidate.assert_awaited_once()
    runtime.handle_input_transcript.assert_awaited_once_with(
        "first",
        is_voice_source=True,
        source="independent_asr",
        metadata={"provider": "openai"},
        source_game_route_identity=None,
    )
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert final_key in dispatcher._resolved
    assert final_key not in dispatcher._reservations
    resolution = dispatcher.resolve_reserved.call_args
    assert resolution.args == (final_key, AdmissionDisposition.FORWARD)
    assert resolution.kwargs["envelope"].final_key == final_key


@pytest.mark.parametrize("replacement", ["epoch", "lifecycle", "detector"])
async def test_provider_overflow_waiting_on_final_lock_is_identity_fenced(
    replacement: str,
) -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    detector = runtime._asr_detector
    assert isinstance(detector, _ReadyDetector)
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    ingress_token = runtime._asr_runtime._asr_current_ingress_token
    assert ingress_token is not None

    await runtime._asr_final_lock.acquire()
    overflow_task = asyncio.create_task(
        runtime._handle_audio_ingress_backpressure(
            ingress_token,
            observed_state=VoiceLifecycleState.DRAINING,
        )
    )
    await asyncio.sleep(0)
    if replacement == "epoch":
        runtime._asr_session_epoch += 1
    elif replacement == "lifecycle":
        replacement_lifecycle = VoiceInputLifecycleController(
            provider_policy=resolve_provider_policy("openai", "provider"),
            shadow_mode=False,
        )
        replacement_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
        runtime._asr_lifecycle = replacement_lifecycle
    else:
        runtime._asr_detector = _ReadyDetector()
    runtime._asr_final_lock.release()
    await overflow_task

    detector.discard_provider_successor.assert_not_awaited()
    assert "ASR_INGRESS_BACKPRESSURE" not in str(runtime.send_status.await_args_list)
    watchdog = runtime._asr_final_watchdog_task
    if watchdog is not None:
        watchdog.cancel()


async def test_owner_voice_composition_preserves_detector_candidate_class_identity(
    monkeypatch,
) -> None:
    import numpy as np

    import main_logic.asr_client.endpointing.detector_runtime as detector_module
    import main_logic.asr_client.runtime as runtime_module
    import main_logic.asr_client.speaker_shadow.contracts as contracts_module
    from main_logic.asr_client.speaker_shadow.asset_manifest import (
        CAMPPLUS_MODEL_ID,
        CAMPPLUS_MODEL_REVISION,
    )
    from main_logic.asr_client.speaker_shadow.campplus import (
        CAMPPLUS_EMBEDDING_DIM,
    )
    from main_logic.voice_identity.contracts import SpeakerModelIdentity
    from main_logic.voice_identity.profile import SpeakerProfile
    from main_logic.voice_identity.reference import SpeakerReference
    from main_logic.voice_identity_service.asr_composition import (
        OwnerVoiceAsrCompositionFactory,
    )

    observations: list[object] = []
    prepared_candidates: list[object] = []

    class _Vad:
        def load(self) -> bool:
            return True

        def close(self) -> None:
            return None

    class _Gate:
        def feed(self, _pcm16: bytes):
            return (SpeechActivityEvent.SPEECH_STARTED,)

        def reset(self) -> None:
            return None

    class _ScoringHost:
        alive = True
        loaded = True
        timed_out = False
        was_terminated = False

        async def score(
            self,
            _pcm16: bytes,
            *,
            timeout_seconds: float,
        ) -> float:
            assert timeout_seconds > 0
            return 0.20

        async def close(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds > 0
            self.alive = False
            self.loaded = False
            return True

        async def terminate(self) -> None:
            self.alive = False
            self.loaded = False
            self.was_terminated = True

    async def on_turn_abandoned(_turn_token: VoiceTurnToken) -> None:
        return None

    runtime = IndependentAsrRuntime(
        AsrRuntimeCallbacks(
            display_name=lambda: "owner-composition-candidate-identity",
            on_prepare_turn=AsyncMock(return_value=True),
            on_partial=AsyncMock(),
            on_final=AsyncMock(),
            on_turn_abandoned=on_turn_abandoned,
            on_failure=AsyncMock(),
            on_status=AsyncMock(),
            on_lifecycle=AsyncMock(),
        )
    )
    model_identity = SpeakerModelIdentity(
        CAMPPLUS_MODEL_ID,
        CAMPPLUS_MODEL_REVISION,
        CAMPPLUS_EMBEDDING_DIM,
    )
    embedding = np.arange(
        1,
        CAMPPLUS_EMBEDDING_DIM + 1,
        dtype=np.float32,
    )
    reference = SpeakerReference(model_identity, embedding)
    embedding.fill(0.0)
    try:
        profile = SpeakerProfile("profile-generation", reference)
    finally:
        reference.close()
    composition = OwnerVoiceAsrCompositionFactory(
        runtime,
        profile,
        activation_generation="composition-activation",
        enforce=True,
    )
    shadow = composition()
    original_evidence_callback = shadow._on_evidence
    assert original_evidence_callback is not None

    def capture_evidence(event) -> None:
        if isinstance(event, contracts_module.SpeakerShadowObservation):
            observations.append(event)
        original_evidence_callback(event)

    monkeypatch.setattr(shadow, "_on_evidence", capture_evidence)
    def on_speaker_candidate_bound(
        candidate,
        turn_token,
        speaker_owner_generation,
    ) -> None:
        runtime._accept_speaker_candidate_binding(
            candidate,
            turn_token,
            detector=detector,
            activation_generation=speaker_owner_generation,
        )

    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=resolve_provider_policy("qwen", "provider"),
        speaker_shadow=shadow,
        speaker_owner_generation="composition-activation",
        on_speaker_candidate_bound=on_speaker_candidate_bound,
    )
    original_prepare = detector.prepare_candidate_rejection

    async def capture_prepare(candidate):
        prepared_candidates.append(candidate)
        return await original_prepare(candidate)

    monkeypatch.setattr(detector, "prepare_candidate_rejection", capture_prepare)
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_provider = "qwen"
    runtime._asr_session = session
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = detector
    runtime._speaker_verifier_activation_generation = "composition-activation"
    runtime._speaker_verifier_enforces_admission = composition.enforces_admission
    runtime._ensure_transport_restart_task = MagicMock()  # type: ignore[method-assign]
    ingress_token = runtime.capture_ingress_token(
        connection_id="composition-candidate",
        lease_generation=3,
        route_generation=5,
    )
    runtime._asr_current_ingress_token = ingress_token
    feed_result = await detector.feed(
        b"\x11\x00" * 160,
        speech_probability=0.9,
        rnnoise_available=True,
        ingress_token=ingress_token,
    )
    assert feed_result.candidate is not None
    turn_token = runtime._capture_turn_token(lifecycle)
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    await runtime._asr_admission_ingress.open_turn(turn_token)
    assert await detector.bind_candidate(feed_result.candidate, turn_token) is not None
    runtime._asr_turn_prepared = True
    runtime._asr_partial_turn_token = turn_token
    final_key = FinalKey.from_turn(turn_token)
    assert runtime._asr_transcript_dispatcher.try_reserve(final_key)
    runtime._asr_admission_reservation_dispatchers[final_key] = (
        runtime._asr_transcript_dispatcher
    )
    runtime._asr_transcript_dispatcher.resolve_reserved = MagicMock(
        wraps=runtime._asr_transcript_dispatcher.resolve_reserved
    )
    assert runtime._asr_audio_dispatcher.activate(turn_token, session, b"")

    checkpoint_pcm16 = b"\x21\x00" * (16_000 * 1_500 // 1_000)
    detector.observe_provider_audio(checkpoint_pcm16, sample_rate_hz=16_000)
    detector_candidate = detector._speaker_shadow_candidate
    assert detector_candidate is not None
    detector.observe_provider_audio(checkpoint_pcm16, sample_rate_hz=16_000)
    # Install after Detector initialization has reset the Shadow lifecycle.
    # Exercise real readiness checks with a cached, loaded host; a mocked
    # _ensure_backend return alone leaves its ownership cache unset.
    shadow._backend_host = _ScoringHost()
    await shadow.wait_idle()
    async def wait_for_reject_requested() -> None:
        for _ in range(200):
            admission_record = await runtime._asr_admission.get_record(turn_token)
            if (
                admission_record is not None
                and admission_record.evidence_state is EvidenceState.DENY_LATCHED
                and prepared_candidates
            ):
                return
            await asyncio.sleep(0.005)
        admission_record = await runtime._asr_admission.get_record(turn_token)
        raise AssertionError(
            (
                runtime._speaker_verifier_diagnostics(),
                observations,
                admission_record,
                runtime._asr_admission_candidate_turns,
            )
        )

    await asyncio.wait_for(wait_for_reject_requested(), 5.0)

    assert len(observations) == 2
    observation_candidates = [
        observation.candidate for observation in observations
    ]
    assert observation_candidates == [detector_candidate, detector_candidate]
    assert all(candidate is detector_candidate for candidate in observation_candidates)
    # The first-low arm owns the stable lease; second-low reuses it instead of
    # preparing the same Detector candidate again.
    assert prepared_candidates == [detector_candidate]
    assert all(candidate is detector_candidate for candidate in prepared_candidates)
    assert type(detector_candidate) is contracts_module.SpeakerShadowCandidateKey
    assert type(detector_candidate) is detector_module.SpeakerShadowCandidateKey
    assert type(detector_candidate) is runtime_module.SpeakerShadowCandidateKey
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["rejection_task_applied_count"] == 0
    assert diagnostics["rejection_task_stale_count"] == 0
    assert diagnostics["rejection_stale_prepare_count"] == 0
    assert diagnostics["rejection_prepare_unbound_count"] == 0
    admission_record = await runtime._asr_admission.get_record(turn_token)
    assert admission_record is not None
    assert admission_record.evidence_state is EvidenceState.DENY_LATCHED
    assert admission_record.rejection_capability is None
    runtime._asr_transcript_dispatcher.resolve_reserved.assert_called_once_with(
        final_key,
        AdmissionDisposition.DROP,
        envelope=None,
    )
    assert final_key not in runtime._asr_transcript_dispatcher._reservations

    await runtime.close()
    composition.close()
    profile.close()


async def test_provider_candidate_is_bound_only_after_canonical_start(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    pcm16 = b"\x31\x00" * 160
    call_order: list[tuple[str, int, int]] = []
    finals: list[VoiceTranscriptEvent] = []
    sessions: list[object] = []
    detector_ref: DetectorRuntime | None = None
    selection = _selection("qwen", "provider")

    class _Vad:
        def load(self) -> bool:
            return True

        def close(self) -> None:
            return None

    class _Gate:
        def feed(self, _pcm16: bytes):
            return (SpeechActivityEvent.SPEECH_STARTED,)

        def reset(self) -> None:
            return None

    class _ProviderSession:
        def __init__(self, callbacks: dict[str, object]) -> None:
            self.callbacks = callbacks
            self.partial_callback = None
            self.is_ready = True
            self.close_count = 0
            self.wire_pcm: list[bytes] = []

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            self.close_count += 1

        async def stream_audio(
            self,
            payload: bytes,
            *,
            sample_rate_hz: int,
        ) -> None:
            assert sample_rate_hz == 16_000
            self.wire_pcm.append(payload)

        async def signal_user_activity_end(self) -> None:
            return None

    class _SpeakerShadow:
        enabled = True
        enforces_admission = True
        activation_generation = "speaker-observation-order"
        generation = 0

        def __init__(self) -> None:
            self.candidate = None
            self.deferred_candidate = None
            self.anchored = False

        def defer_candidate(self, candidate) -> bool:
            self.deferred_candidate = candidate
            return True

        def activate_candidate(self, candidate) -> bool:
            return candidate == self.deferred_candidate

        def anchor_deferred_candidate(self, request):
            if request.candidate != self.deferred_candidate:
                return None
            return SimpleNamespace(
                runtime_generation=self.generation,
                operation_id=1,
                candidate=request.candidate,
                anchor_revision=request.anchor_revision,
                observed_sample_count=request.expected_observed_sample_count,
                discarded_sample_count=request.discard_prefix_sample_count,
                retained_sample_count=(
                    request.expected_observed_sample_count
                    - request.discard_prefix_sample_count
                ),
                _owner=self,
            )

        def deferred_anchor_status(self, _receipt):
            return "applied" if self.anchored else "pending"

        async def wait_deferred_anchor_settled(self, receipt, *, deadline):
            del deadline
            self.anchored = True
            self.candidate = receipt.candidate
            call_order.append(
                (
                    "observe",
                    receipt.candidate.detector_epoch,
                    receipt.candidate.shadow_generation,
                )
            )
            return "applied"

        def submit(
            self,
            _pcm16: bytes,
            *,
            sample_rate_hz: int,
            candidate,
        ) -> bool:
            assert sample_rate_hz == 16_000
            if not self.anchored:
                assert candidate == self.deferred_candidate
                # Deferred PCM was retained successfully, but it must not be
                # treated as scored/observable until canonical started rebases
                # the candidate.
                return True
            call_order.append(
                (
                    "observe",
                    candidate.detector_epoch,
                    candidate.shadow_generation,
                )
            )
            self.candidate = candidate
            return False

        def finish_candidate(self, candidate) -> bool:
            del candidate
            return False

        async def reset(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def on_final(event: VoiceTranscriptEvent) -> None:
        finals.append(event)

    def create_session(_core_type: str, **kwargs) -> _ProviderSession:
        session = _ProviderSession(kwargs)
        sessions.append(session)
        return session

    def create_detector(**kwargs) -> DetectorRuntime:
        nonlocal detector_ref
        detector = DetectorRuntime(vad=_Vad(), gate=_Gate(), **kwargs)
        detector_ref = detector
        return detector

    def attach_partial(session: _ProviderSession, callback) -> None:
        session.partial_callback = callback

    monkeypatch.setattr(
        runtime_module,
        "_resolve_asr_selection",
        lambda _core_type: selection,
    )
    monkeypatch.setattr(
        runtime_module,
        "_create_asr_session_from_selection",
        create_session,
    )
    monkeypatch.setattr(runtime_module, "_attach_partial_callback", attach_partial)
    monkeypatch.setattr(runtime_module, "DetectorRuntime", create_detector)

    runtime = IndependentAsrRuntime(
        AsrRuntimeCallbacks(
            display_name=lambda: "provider-speaker-observation-order",
            on_prepare_turn=AsyncMock(return_value=True),
            on_partial=AsyncMock(),
            on_final=on_final,
            on_turn_abandoned=AsyncMock(),
            on_failure=AsyncMock(),
            on_status=AsyncMock(),
            on_lifecycle=AsyncMock(),
        )
    )
    start_result = await runtime.start(
        route_key="qwen",
        resource_optimization_enabled=False,
        speaker_shadow_factory=_SpeakerShadow,
    )
    assert start_result.status is AsrStartStatus.READY
    assert detector_ref is not None
    assert len(sessions) == 1
    provider_session = sessions[0]
    assert isinstance(provider_session, _ProviderSession)
    ingress_token = runtime.capture_ingress_token(
        connection_id="speaker-observation-order",
        lease_generation=7,
        route_generation=11,
    )
    original_open_speaker_lease_nowait = (
        runtime._asr_admission_ingress.open_speaker_lease_nowait
    )

    def traced_open_speaker_lease_nowait(lease_token, candidate):
        future = original_open_speaker_lease_nowait(lease_token, candidate)
        call_order.append(
            (
                "lease",
                candidate.detector_epoch,
                candidate.shadow_generation,
            )
        )
        return future

    runtime._asr_admission_ingress.open_speaker_lease_nowait = (
        traced_open_speaker_lease_nowait
    )

    submit_result = await runtime.submit(
        ProcessedVoiceFrame(
            pcm16,
            16_000,
            0.9,
            True,
        ),
        ingress_token=ingress_token,
    )
    await runtime._asr_audio_dispatcher.wait_idle()
    speaker_shadow = detector_ref._speaker_shadow
    assert isinstance(speaker_shadow, _SpeakerShadow)
    assert speaker_shadow.candidate is None
    assert call_order == []
    assert runtime._asr_current_speaker_lease is None
    assert provider_session.wire_pcm == [pcm16], (
        submit_result,
        runtime._asr_lifecycle.snapshot,
        runtime._asr_audio_dispatcher.active_turn,
        runtime._asr_current_speaker_candidate,
        runtime._asr_provider_speaker_ledgers,
    )

    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            audio_start_sample_16k=0,
        ),
        runtime._asr_session_epoch,
    )
    assert speaker_shadow.candidate is not None, (
        speaker_shadow.deferred_candidate,
        speaker_shadow.anchored,
        runtime._asr_provider_speaker_ledgers,
        runtime._asr_provider_speaker_key_ledgers,
        runtime._speaker_verifier_diagnostics(),
    )

    assert submit_result.status is AsrSubmitStatus.ACCEPTED
    assert call_order[:2] == [("observe", 0, 0), ("lease", 0, 0)]
    lease_token = runtime._asr_current_speaker_lease
    assert lease_token is not None
    lease_record = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert lease_record is not None
    assert lease_record.candidate == speaker_shadow.candidate
    assert provider_session.wire_pcm == [pcm16]
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["rejection_prepare_unbound_count"] == 0
    assert diagnostics["provider_candidate_bind_attempt_count"] == 0
    assert diagnostics["provider_candidate_bind_success_count"] == 0
    assert diagnostics["provider_candidate_bind_empty_count"] == 0
    assert diagnostics["provider_candidate_bind_failed_count"] == 0

    assert diagnostics["rejection_task_applied_count"] == 0

    await runtime.close()


async def test_speaker_shadow_abba_cannot_change_provider_authority(
    monkeypatch,
) -> None:
    import main_logic.asr_client.runtime as runtime_module

    quiet = b"\x00\x00" * 160
    started = b"\x01\x00" * 160
    continued = b"\x02\x00" * 160
    paused = b"\x03\x00" * 160
    successor = b"\x04\x00" * 160
    candidate_frames = (quiet, started, continued, paused)
    candidate_probabilities = (0.0, 0.9, 0.1, 0.1)
    candidate_rnnoise_available = (False, True, True, True)
    candidate_processed_frames = tuple(
        ProcessedVoiceFrame(
            pcm16=pcm16,
            sample_rate_hz=16_000,
            speech_probability=probability,
            rnnoise_available=rnnoise_available,
        )
        for pcm16, probability, rnnoise_available in zip(
            candidate_frames,
            candidate_probabilities,
            candidate_rnnoise_available,
            strict=True,
        )
    )
    successor_processed_frame = ProcessedVoiceFrame(
        pcm16=successor,
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    gate_events = (
        (),
        (SpeechActivityEvent.SPEECH_STARTED,),
        (),
        (SpeechActivityEvent.CANDIDATE_PAUSE,),
        (SpeechActivityEvent.SPEECH_STARTED,),
    )
    # Metrics derived from wall-clock, excluded from the snapshot comparison
    # because their value depends on how the runner happened to schedule us.
    #
    # ⚠️ Audio-duration metrics (local_audio_ms / cloud_audio_ms /
    # provider_wire_audio_ms / suppressed_silence_ms / shadow_suppressed_audio_ms)
    # are deliberately NOT here: they are computed from the frames fed in, so they
    # are deterministic and are part of what this test asserts.
    #
    # ⚠️ Any new wall-clock metric must be added here. Nothing on the dataclass
    # marks a field as wall-clock, so this list cannot be derived — which is how
    # asr_audio_command_queue_ms was missed when it landed: it measures
    # `time.monotonic() - queued_at` (asr_client/audio.py), reads 0 on an idle
    # machine, and came back as 16 (the Windows timer granularity) on a busy CI
    # runner, failing whole-snapshot equality on a single value.
    volatile_metric_names = frozenset(
        {
            "connect_latency_ms",
            "first_partial_latency_ms",
            "final_latency_ms",
            "smart_turn_load_ms",
            "smart_turn_inference_ms",
            "detector_submit_latency_ms",
            "detector_queue_audio_ms",
            "detector_queue_high_water_ms",
            "detector_oldest_frame_age_ms",
            "asr_audio_command_queue_ms",
        }
    )
    real_detector_runtime = DetectorRuntime
    selection = _selection("qwen", "provider")

    class _Vad:
        def __init__(self) -> None:
            self.load_count = 0
            self.close_count = 0

        def load(self) -> bool:
            self.load_count += 1
            return True

        def close(self) -> None:
            self.close_count += 1

    class _Gate:
        def __init__(self) -> None:
            self.frames: list[bytes] = []
            self._events = iter(gate_events)

        def feed(self, pcm16: bytes):
            self.frames.append(pcm16)
            return next(self._events)

        def reset(self) -> None:
            return None

    class _ProviderSession:
        def __init__(self, callbacks: dict[str, object]) -> None:
            self.callbacks = callbacks
            self.is_ready = True
            self.connect_count = 0
            self.close_count = 0
            self.signal_end_count = 0
            self.provider_wire_audio_ms = 0
            self.wire_pcm: list[bytes] = []

        async def connect(self) -> None:
            self.connect_count += 1

        async def close(self) -> None:
            self.close_count += 1

        async def stream_audio(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
        ) -> None:
            assert sample_rate_hz == 16_000
            self.wire_pcm.append(pcm16)
            self.provider_wire_audio_ms += len(pcm16) * 1_000 // (16_000 * 2)

        async def signal_user_activity_end(self) -> None:
            self.signal_end_count += 1

    class _Shadow:
        enabled = True

        def __init__(
            self,
            *,
            raises: bool,
            call_order: list[tuple[str, int, int]],
        ) -> None:
            self.raises = raises
            self.call_order = call_order
            self.submissions: list[tuple[bytes, int, object]] = []
            self.finishes: list[object] = []
            self.reset_count = 0
            self.close_count = 0

        def submit(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
            candidate,
        ) -> bool:
            self.submissions.append((pcm16, sample_rate_hz, candidate))
            self.call_order.append(
                (
                    "observe",
                    candidate.detector_epoch,
                    candidate.shadow_generation,
                )
            )
            if self.raises and len(self.submissions) == 2:
                raise RuntimeError("shadow submit failure")
            return False

        def finish_candidate(self, candidate) -> bool:
            self.finishes.append(candidate)
            if self.raises:
                raise RuntimeError("shadow finish failure")
            return False

        async def reset(self) -> None:
            self.reset_count += 1

        async def close(self) -> None:
            self.close_count += 1

    def turn_identity(turn_token: VoiceTurnToken) -> tuple[object, ...]:
        ingress = turn_token.ingress
        return (
            turn_token.turn_id,
            ingress.session_epoch,
            ingress.connection_id,
            ingress.lease_generation,
            ingress.route_generation,
            ingress.audio_generation,
        )

    def transcript_identity(event: VoiceTranscriptEvent) -> tuple[object, ...]:
        return (event.text, event.provider, *turn_identity(event.turn_token))

    async def replay(
        shadow_mode: str | None,
    ) -> tuple[dict[str, object], _Shadow | None]:
        lifecycle_notifications: list[AsrLifecycleNotification] = []
        statuses: list[AsrStatusEvent] = []
        failures: list[AsrFailureEvent] = []
        prepared_turns: list[VoiceTurnToken] = []
        finals: list[VoiceTranscriptEvent] = []
        abandoned_turns: list[VoiceTurnToken] = []
        partials: list[VoicePartialEvent] = []
        gate = _Gate()
        vad = _Vad()
        provider_sessions: list[_ProviderSession] = []
        detector_shadows: list[object | None] = []
        binding_observation_order: list[tuple[str, int, int]] = []
        factory_calls = 0
        shadow = (
            None
            if shadow_mode is None
            else _Shadow(
                raises=shadow_mode == "raises",
                call_order=binding_observation_order,
            )
        )

        async def on_prepare_turn(turn_token: VoiceTurnToken) -> bool:
            prepared_turns.append(turn_token)
            return True

        async def on_partial(event: VoicePartialEvent) -> None:
            partials.append(event)

        async def on_final(event: VoiceTranscriptEvent) -> None:
            finals.append(event)

        async def on_turn_abandoned(turn_token: VoiceTurnToken) -> None:
            abandoned_turns.append(turn_token)

        async def on_failure(event: AsrFailureEvent) -> None:
            failures.append(event)

        async def on_status(event: AsrStatusEvent) -> None:
            statuses.append(event)

        async def on_lifecycle(event: AsrLifecycleNotification) -> None:
            lifecycle_notifications.append(event)

        runtime = IndependentAsrRuntime(
            AsrRuntimeCallbacks(
                display_name=lambda: "ABBA",
                on_prepare_turn=on_prepare_turn,
                on_partial=on_partial,
                on_final=on_final,
                on_turn_abandoned=on_turn_abandoned,
                on_failure=on_failure,
                on_status=on_status,
                on_lifecycle=on_lifecycle,
            )
        )

        def create_session(_core_type: str, **kwargs) -> _ProviderSession:
            assert kwargs["selection"] is selection
            session = _ProviderSession(kwargs)
            provider_sessions.append(session)
            return session

        def create_detector(**kwargs) -> DetectorRuntime:
            detector_shadows.append(kwargs.get("speaker_shadow"))
            detector = real_detector_runtime(vad=vad, gate=gate, **kwargs)
            original_bind = detector.bind_candidate

            async def traced_bind(candidate, turn_token):
                binding_observation_order.append(
                    (
                        "bind",
                        candidate.detector_epoch,
                        candidate.candidate_generation,
                    )
                )
                return await original_bind(candidate, turn_token)

            detector.bind_candidate = traced_bind  # type: ignore[method-assign]
            return detector

        def create_shadow():
            nonlocal factory_calls
            factory_calls += 1
            return shadow

        monkeypatch.setattr(
            runtime_module,
            "_resolve_asr_selection",
            lambda _core_type: selection,
        )
        monkeypatch.setattr(
            runtime_module,
            "_create_asr_session_from_selection",
            create_session,
        )
        monkeypatch.setattr(runtime_module, "DetectorRuntime", create_detector)

        start_result = await runtime.start(
            route_key="qwen",
            resource_optimization_enabled=True,
            speaker_shadow_factory=(create_shadow if shadow is not None else None),
        )
        assert start_result.status is AsrStartStatus.READY
        assert len(provider_sessions) == 1
        provider_session = provider_sessions[0]
        lifecycle = runtime._asr_lifecycle
        assert lifecycle is not None
        ingress_token = runtime.capture_ingress_token(
            connection_id="abba-connection",
            lease_generation=7,
            route_generation=11,
        )
        snapshots: list[tuple[object, ...]] = []

        def record_snapshot(step: str) -> None:
            snapshot = lifecycle.snapshot
            snapshots.append(
                (
                    step,
                    snapshot.state.value,
                    snapshot.route_mode.value,
                    snapshot.route_generation,
                    snapshot.transport_generation,
                    snapshot.turn_id,
                    tuple(
                        sorted(
                            (name, value)
                            for name, value in lifecycle.metrics.snapshot().items()
                            if name not in volatile_metric_names
                        )
                    ),
                )
            )

        record_snapshot("started")
        submit_statuses: list[AsrSubmitStatus] = []
        for index, processed_frame in enumerate(candidate_processed_frames):
            result = await runtime.submit(
                processed_frame,
                ingress_token=ingress_token,
            )
            await runtime._asr_audio_dispatcher.wait_idle()
            submit_statuses.append(result.status)
            record_snapshot(f"candidate-frame-{index}")

        endpoint_result = await provider_session.callbacks["on_turn_endpointed"]()
        record_snapshot("endpoint")
        provider_wire_before_successor = tuple(provider_session.wire_pcm)
        shadow_submissions_before_successor = (
            () if shadow is None else tuple(shadow.submissions)
        )

        successor_result = await runtime.submit(
            successor_processed_frame,
            ingress_token=ingress_token,
        )
        await runtime._asr_audio_dispatcher.wait_idle()
        submit_statuses.append(successor_result.status)
        record_snapshot("successor-buffered")
        provider_wire_before_final = tuple(provider_session.wire_pcm)
        shadow_submissions_before_final = (
            () if shadow is None else tuple(shadow.submissions)
        )

        final_result = await provider_session.callbacks["on_input_transcript"](
            "abba-final"
        )
        await runtime._asr_audio_dispatcher.wait_idle()
        await runtime.wait_transcript_idle()
        record_snapshot("final-successor-active")

        await runtime.close()
        record_snapshot("closed")
        trace = {
            "start_result": (
                start_result.status.value,
                start_result.provider,
                start_result.failure_code,
                start_result.session_epoch,
            ),
            "wire_pcm": tuple(provider_session.wire_pcm),
            "wire_sha256": tuple(
                hashlib.sha256(payload).hexdigest()
                for payload in provider_session.wire_pcm
            ),
            "wire_concat_sha256": hashlib.sha256(
                b"".join(provider_session.wire_pcm)
            ).hexdigest(),
            "wire_order": tuple(
                (
                    index,
                    hashlib.sha256(payload).hexdigest(),
                )
                for index, payload in enumerate(provider_session.wire_pcm, start=1)
            ),
            "wire_before_successor": provider_wire_before_successor,
            "wire_before_final": provider_wire_before_final,
            "wire_object_provenance": (
                provider_session.wire_pcm[0]
                is candidate_processed_frames[0].pcm16,
                provider_session.wire_pcm[0]
                is candidate_processed_frames[1].pcm16,
                provider_session.wire_pcm[1]
                is candidate_processed_frames[2].pcm16,
                provider_session.wire_pcm[2]
                is candidate_processed_frames[3].pcm16,
                provider_session.wire_pcm[3] is successor_processed_frame.pcm16,
            ),
            "submit_statuses": tuple(status.value for status in submit_statuses),
            "provider_callbacks": (
                ("endpoint", 1, endpoint_result),
                ("final", 1, final_result),
            ),
            "prepared_turns": tuple(
                turn_identity(token) for token in prepared_turns
            ),
            "finals": tuple(transcript_identity(event) for event in finals),
            "lifecycle": tuple(
                (event.state, event.provider, event.session_epoch)
                for event in lifecycle_notifications
            ),
            "snapshots": tuple(snapshots),
            "statuses": tuple(
                (event.code, event.provider, event.session_epoch)
                for event in statuses
            ),
            "failures": tuple(
                (event.code, event.provider, event.session_epoch)
                for event in failures
            ),
            "abandoned": tuple(
                turn_identity(token) for token in abandoned_turns
            ),
            "partials": tuple(
                (event.text, *turn_identity(event.turn_token)) for event in partials
            ),
            "provider_signal_end_count": provider_session.signal_end_count,
            "provider_connect_count": provider_session.connect_count,
            "provider_close_count": provider_session.close_count,
            "gate_pcm": tuple(gate.frames),
            "vad_load_count": vad.load_count,
            "vad_close_count": vad.close_count,
            "closed": (
                runtime._asr_session is None,
                runtime._asr_detector is None,
                runtime._asr_lifecycle is None,
                runtime._asr_warm_expiry_task is None,
            ),
        }

        assert detector_shadows == [shadow]
        assert factory_calls == (0 if shadow is None else 1)
        assert provider_wire_before_successor == (
            quiet + started,
            continued,
            paused,
        )
        assert provider_session.wire_pcm[1] is candidate_processed_frames[2].pcm16
        assert provider_session.wire_pcm[2] is candidate_processed_frames[3].pcm16
        assert provider_wire_before_final == provider_wire_before_successor
        if shadow is not None:
            assert shadow_submissions_before_successor == tuple(shadow.submissions[:3])
            assert shadow_submissions_before_final == shadow_submissions_before_successor
            assert len(shadow.submissions) == 4
            assert all(
                submission[0] is provider_payload
                for submission, provider_payload in zip(
                    shadow.submissions,
                    provider_session.wire_pcm,
                    strict=True,
                )
            )
            assert [submission[0] for submission in shadow.submissions] == [
                quiet + started,
                continued,
                paused,
                successor,
            ]
            assert quiet not in [
                submission[0] for submission in shadow.submissions
            ]
            assert all(submission[1] == 16_000 for submission in shadow.submissions)
            candidates = [submission[2] for submission in shadow.submissions]
            assert candidates[0] == candidates[1] == candidates[2]
            assert candidates[3] != candidates[0]
            assert all(
                getattr(candidate, "scope") == "provider_candidate"
                for candidate in candidates
            )
            assert getattr(candidates[3], "shadow_generation") > getattr(
                candidates[0],
                "shadow_generation",
            )
            assert shadow.finishes == [candidates[0]]
            assert shadow.reset_count == 0
            assert shadow.close_count == 1
            first_observation_index = next(
                index
                for index, observation in enumerate(binding_observation_order)
                if observation[0] == "observe"
            )
            first_observation = binding_observation_order[first_observation_index]
            assert (
                "bind",
                first_observation[1],
                first_observation[2],
            ) in binding_observation_order[:first_observation_index]
        return trace, shadow

    disabled_a, _ = await replay(None)
    observed_false, false_shadow = await replay("false")
    observed_raising, raising_shadow = await replay("raises")
    disabled_b, _ = await replay(None)

    assert false_shadow is not None
    assert raising_shadow is not None
    assert disabled_a == observed_false == observed_raising == disabled_b
    assert disabled_a["wire_pcm"] == (
        quiet + started,
        continued,
        paused,
        successor,
    )
    assert disabled_a["wire_concat_sha256"] == hashlib.sha256(
        quiet + started + continued + paused + successor
    ).hexdigest()
    assert disabled_a["wire_object_provenance"] == (
        False,
        False,
        True,
        True,
        False,
    )
    assert disabled_a["submit_statuses"] == ("accepted",) * 5
    assert disabled_a["provider_callbacks"] == (
        ("endpoint", 1, None),
        ("final", 1, None),
    )
    assert disabled_a["provider_signal_end_count"] == 0
    assert disabled_a["provider_connect_count"] == 1
    assert disabled_a["provider_close_count"] == 1
    assert len(disabled_a["finals"]) == 1


@pytest.mark.unit
async def test_microphone_route_syncs_provider_neutral_visual_delivery_mode() -> None:
    """Independent ASR must fail closed for raw vision during every route state."""
    runtime = _Runtime()
    runtime.session._supports_native_image = True
    runtime.session.set_visual_delivery_mode = MagicMock()
    runtime.session.block_raw_visual_delivery = MagicMock()
    runtime.session.allow_raw_visual_delivery = MagicMock()

    runtime._set_microphone_route("independent")
    runtime._set_microphone_route("blocked")
    runtime._set_microphone_route("native")

    delivered_modes = [
        getattr(item.args[0], "value", item.args[0])
        for item in runtime.session.set_visual_delivery_mode.call_args_list
    ]
    assert delivered_modes == ["native"]
    assert runtime.session.block_raw_visual_delivery.call_count >= 2
    runtime.session.allow_raw_visual_delivery.assert_called_once_with()


@pytest.mark.unit
async def test_native_route_leaves_provider_capability_routing_inside_session() -> None:
    """Core selects the ASR strategy, while session capability keeps legacy behavior."""
    runtime = _Runtime()
    runtime.session._supports_native_image = False
    runtime.session.set_visual_delivery_mode = MagicMock()

    runtime._set_microphone_route("native")

    delivered_mode = runtime.session.set_visual_delivery_mode.call_args.args[0]
    assert getattr(delivered_mode, "value", delivered_mode) == "native"


@pytest.mark.unit
async def test_independent_visual_sync_failure_blocks_raw_images_without_stopping_asr() -> None:
    runtime = _Runtime()
    call_order: list[str] = []

    def block_raw_visual_delivery() -> None:
        call_order.append("block")

    def fail_visual_mode_sync(_mode: str) -> None:
        call_order.append("sync")
        raise RuntimeError("stale realtime session")

    runtime.session.block_raw_visual_delivery = block_raw_visual_delivery
    runtime.session.set_visual_delivery_mode = fail_visual_mode_sync

    runtime._set_microphone_route("independent")

    assert runtime._asr_route_mode == "independent"
    assert call_order == ["block"]


@pytest.mark.unit
async def test_independent_multimodal_turn_samples_the_utterance_span() -> None:
    """One utterance carries first/middle/last; identity fields name the last."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=77)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    captured_at = time.monotonic()

    assert runtime._stage_independent_visual_frame(
        "first-frame",
        source="screen",
        request_id="frame-1",
        captured_at=captured_at,
    )
    assert runtime._stage_independent_visual_frame(
        "latest-frame",
        source="camera",
        request_id="frame-2",
        captured_at=captured_at + 0.1,
    )
    assert not runtime._stage_independent_visual_frame(
        "stale-frame",
        source="screen",
        request_id="frame-stale",
        captured_at=captured_at - 0.1,
    )

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert turn is not None
    # 两帧都在本回合窗口内：开头那张不能因为"不是最新"被丢掉——用户开口时指的
    # 东西就在那张上。source / request_id 仍然描述最新那张（回合的收尾身份）。
    assert turn.images == ("first-frame", "latest-frame")
    assert turn.source == "camera"
    assert turn.request_id == "frame-2"
    assert turn.image_generation > turn.start_image_generation


@pytest.mark.unit
async def test_independent_multimodal_turn_never_reuses_prior_turn_frame() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    captured_at = time.monotonic()
    assert runtime._stage_independent_visual_frame(
        "prior-turn-frame",
        source="screen",
        request_id="screen-prior",
        captured_at=captured_at,
    )
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=78)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "new question")

    assert turn is None


@pytest.mark.unit
async def test_independent_multimodal_turn_rejects_delayed_prior_capture() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    captured_before_turn = time.monotonic() - 1.0
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=79)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    # Validation completes after prepare, so generation alone looks current;
    # the ingress capture time must keep this prior image out of the new turn.
    assert runtime._stage_independent_visual_frame(
        "delayed-prior-frame",
        source="screen",
        request_id="screen-delayed",
        captured_at=captured_before_turn,
    )
    assert record.last_frame is None
    assert runtime._snapshot_core_multimodal_turn(turn_id, "new question") is None

    assert runtime._stage_independent_visual_frame(
        "current-turn-frame",
        source="camera",
        request_id="camera-current",
        captured_at=record.started_at,
    )
    turn = runtime._snapshot_core_multimodal_turn(turn_id, "new question")

    assert turn is not None
    assert turn.images == ("current-turn-frame",)
    assert turn.captured_at == record.started_at


@pytest.mark.unit
async def test_independent_multimodal_turn_rejects_owned_frame_expired_at_final() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=80)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]
    assert runtime._stage_independent_visual_frame(
        "expired-owned-frame",
        source="screen",
        request_id="screen-expired",
        captured_at=record.started_at,
    )

    with patch(
        "main_logic.core.asr_runtime.time.monotonic",
        return_value=(
            record.started_at + runtime._independent_visual_frame_ttl_s + 1.0
        ),
    ):
        turn = runtime._snapshot_core_multimodal_turn(turn_id, "delayed final")

    assert turn is None


@pytest.mark.unit
async def test_direct_multimodal_final_submits_raw_image_once() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    runtime._asr_route_mode = "independent"
    runtime.session.get_multimodal_turn_delivery = MagicMock(
        return_value="direct_atomic"
    )
    # 在**调用发生的那一刻**取一次所有权判据的值。它是个活闭包，事后再调时
    # 这一轮早已结束、所有权已释放，所以只能在这里记。
    owned_at_call: list = []

    async def _record_ownership(*_args, **kwargs):
        cb = kwargs.get("visual_still_owned")
        owned_at_call.append(cb() if callable(cb) else None)

    runtime.session.submit_multimodal_turn = AsyncMock(
        side_effect=_record_ownership
    )
    runtime.session.submit_external_voice_turn = AsyncMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    async def validate_frame() -> None:
        await asyncio.sleep(0)
        assert runtime._stage_independent_visual_frame(
            "raw-frame",
            source="screen",
            request_id="screen-1",
            captured_at=record.started_at,
        )

    validation_task = asyncio.create_task(validate_frame())
    assert runtime._track_independent_visual_validation_task(
        validation_task,
        captured_at=record.started_at,
    )

    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(
            turn_token=token,
            provider="openai",
            text="look here",
        )
    )
    await validation_task

    runtime.session.submit_multimodal_turn.assert_awaited_once_with(
        "look here",
        ("raw-frame",),
        turn_id=turn_id,
        # 帧总线的频道标签，与这批帧一起冻结。会话侧读活状态会在裁剪 / arbiter
        # 排队 / SDK send 那几段 await 里漂到后继发声的通道上。
        source="screen",
        # Gemini 那条路在真正送出之前还有一段压缩 await，所有权判据必须跟着进去。
        visual_still_owned=ANY,
    )
    # 传的是这一轮 record 自己的 source，不是某个字面量碰巧相等。
    assert runtime.session.submit_multimodal_turn.await_args.kwargs["source"] == (
        record.source if hasattr(record, "source") else "screen"
    )
    # 穿进去的必须是活的判据，且在真正调用 provider 的那一刻仍持有所有权。
    assert owned_at_call == [True]
    runtime.session.submit_external_voice_turn.assert_not_awaited()
    assert turn_id not in runtime._core_multimodal_turns


@pytest.mark.unit
async def test_final_superseded_after_freeze_submits_text_without_frames() -> None:
    """Freezing the frames is not the last word; the submit is.

    The record is retained past a successor prepare so this final keeps its
    transcript, which means the route self-check still finds the same record
    object and passes. But the successor now owns the visuals, so the frozen
    frames belong to the newer utterance. The sentence still has to be
    submitted -- as plain text, the ordinary no-image path.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    runtime._asr_route_mode = "independent"
    runtime.session.get_multimodal_turn_delivery = MagicMock(
        return_value="direct_atomic"
    )
    runtime.session.submit_multimodal_turn = AsyncMock()
    runtime.session.submit_external_voice_turn = AsyncMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]
    assert runtime._stage_independent_visual_frame(
        "frame-of-the-old-turn",
        source="screen",
        request_id="screen-1",
        captured_at=record.started_at,
    )

    accepted = runtime.handle_input_transcript

    async def accept_then_let_a_successor_start(*args, **kwargs):
        result = await accepted(*args, **kwargs)
        # 冻结之后、提交之前：后继发声 prepare，视觉所有权交出去。
        successor = VoiceTurnToken(
            ingress=runtime._capture_ingress_token(),
            turn_id=token.turn_id + 1,
        )
        runtime._begin_core_multimodal_turn(
            f"asr-{successor.ingress.session_epoch}-{successor.turn_id}",
            successor,
        )
        return result

    runtime.handle_input_transcript = accept_then_let_a_successor_start

    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(
            turn_token=token,
            provider="openai",
            text="look here",
        )
    )

    assert record.invalidated.is_set()
    runtime.session.submit_multimodal_turn.assert_not_awaited()
    runtime.session.submit_external_voice_turn.assert_awaited_once()
    assert "look here" in runtime.session.submit_external_voice_turn.await_args.args


@pytest.mark.unit
@pytest.mark.parametrize("delivery", ["direct_atomic", "handoff_required"])
async def test_ownership_lost_between_the_freeze_check_and_the_provider_call(
    delivery,
) -> None:
    """One check up front is not enough; every await is another window.

    Between the post-freeze check and the actual provider call there is still
    the transcript send, preview restoration, the swap barrier and (on the
    handoff path) preparing a replacement session. A successor prepared in any
    of those windows owns the frames, so the last synchronous point before the
    call has to look again.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    runtime._asr_route_mode = "independent"
    runtime.session.submit_multimodal_turn = AsyncMock()
    runtime.session.submit_external_voice_turn = AsyncMock()
    runtime._handoff_to_offline_vlm_and_submit = AsyncMock(return_value=True)
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]
    assert runtime._stage_independent_visual_frame(
        "frame-of-the-old-turn",
        source="screen",
        request_id="screen-1",
        captured_at=record.started_at,
    )

    def _take_ownership_then_report_delivery():
        # 这一步排在冻结后那次检查**之后**、真正调 provider 之前。
        successor = VoiceTurnToken(
            ingress=runtime._capture_ingress_token(),
            turn_id=token.turn_id + 1,
        )
        runtime._begin_core_multimodal_turn(
            f"asr-{successor.ingress.session_epoch}-{successor.turn_id}",
            successor,
        )
        return delivery

    runtime.session.get_multimodal_turn_delivery = MagicMock(
        side_effect=_take_ownership_then_report_delivery
    )

    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(
            turn_token=token,
            provider="openai",
            text="look here",
        )
    )

    assert record.invalidated.is_set()
    runtime.session.submit_multimodal_turn.assert_not_awaited()
    runtime._handoff_to_offline_vlm_and_submit.assert_not_awaited()
    runtime.session.submit_external_voice_turn.assert_awaited_once()
    assert "look here" in runtime.session.submit_external_voice_turn.await_args.args


@pytest.mark.unit
async def test_dispatch_hands_the_ownership_predicate_to_the_handoff() -> None:
    """Checking before the handoff is not enough; it must check inside too.

    Connecting and promoting the Offline candidate, starting TTS and syncing
    tools are the longest awaits on the path, and the handoff's own
    ``operation_is_current`` covers route identity only. A guard that merely
    exercises the predicate in isolation still passes when the dispatch stops
    handing it over, so assert the call site itself.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    runtime._asr_route_mode = "independent"
    runtime.session.get_multimodal_turn_delivery = MagicMock(
        return_value="handoff_required"
    )
    runtime.session.submit_external_voice_turn = AsyncMock()
    seen: dict = {}

    async def observe_predicate_inside_the_handoff(_turn, **kwargs):
        still_owned = kwargs["visual_still_owned"]
        seen["before"] = still_owned()
        # 后继发声在交接进行中 prepare —— 谓词必须立刻反映出来，而不是停在
        # 进入交接那一刻的快照。
        seen["record"].invalidated.set()
        seen["after"] = still_owned()
        return True

    runtime._handoff_to_offline_vlm_and_submit = AsyncMock(
        side_effect=observe_predicate_inside_the_handoff
    )
    handoff_token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    handoff_turn_id = (
        f"asr-{handoff_token.ingress.session_epoch}-{handoff_token.turn_id}"
    )
    runtime._begin_core_multimodal_turn(handoff_turn_id, handoff_token)
    handoff_record = runtime._core_multimodal_turns[handoff_turn_id]
    seen["record"] = handoff_record
    assert runtime._stage_independent_visual_frame(
        "frame-of-this-turn",
        source="screen",
        request_id="screen-1",
        captured_at=handoff_record.started_at,
    )

    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(
            turn_token=handoff_token,
            provider="openai",
            text="look here",
        )
    )

    runtime._handoff_to_offline_vlm_and_submit.assert_awaited_once()
    assert seen["before"] is True
    assert seen["after"] is False


@pytest.mark.unit
async def test_provider_admission_rejection_submits_the_transcript_as_text() -> None:
    """Losing the provider's admission window must not lose the sentence.

    The arbiter rejects a multimodal ticket once a newer turn has armed its
    pause, and deletes the committed item on the way out -- nothing of this
    request survives provider-side. Propagating that error drops the user's
    whole utterance; the frames are gone but the transcript still has to be
    answered, exactly as when Core detects the supersession itself.
    """
    from main_logic.omni_realtime_client._response_arbiter import (
        ResponseAdmissionRejected,
    )

    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    runtime._asr_route_mode = "independent"
    runtime.session.get_multimodal_turn_delivery = MagicMock(
        return_value="direct_atomic"
    )
    runtime.session.submit_multimodal_turn = AsyncMock(
        side_effect=ResponseAdmissionRejected(
            "response dispatch admission rejected after commit"
        )
    )
    runtime.session.submit_external_voice_turn = AsyncMock()
    admission_token = runtime._asr_runtime._capture_turn_token(
        runtime._asr_lifecycle
    )
    admission_turn_id = (
        f"asr-{admission_token.ingress.session_epoch}-{admission_token.turn_id}"
    )
    runtime._begin_core_multimodal_turn(admission_turn_id, admission_token)
    admission_record = runtime._core_multimodal_turns[admission_turn_id]
    assert runtime._stage_independent_visual_frame(
        "frame-of-this-turn",
        source="screen",
        request_id="screen-1",
        captured_at=admission_record.started_at,
    )

    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(
            turn_token=admission_token,
            provider="openai",
            text="这句话不能消失",
        )
    )

    runtime.session.submit_multimodal_turn.assert_awaited_once()
    runtime.session.submit_external_voice_turn.assert_awaited_once()
    assert (
        "这句话不能消失"
        in runtime.session.submit_external_voice_turn.await_args.args
    )


@pytest.mark.unit
async def test_route_close_drops_the_staged_visual_caches() -> None:
    """Staged originals belong to the route, not to the process.

    Their only other clearing point is the NEXT turn starting, so an episode
    that ends while screen sharing is on -- with no further utterance -- leaves
    full-size base64 originals pinned on a long-lived character manager, and the
    next episode starts with a buffer already full of the previous one's frames.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    assert runtime._stage_independent_visual_frame(
        "frame-with-no-utterance",
        source="screen",
        request_id="screen-1",
        captured_at=time.monotonic(),
    )
    assert runtime._prerecord_visual_frames
    assert runtime._latest_independent_visual_frame is not None

    await runtime._close_independent_asr(next_route_mode="blocked")

    assert runtime._prerecord_visual_frames == []
    assert runtime._latest_independent_visual_frame is None


@pytest.mark.unit
async def test_visual_validation_wait_timeout_does_not_cancel_image_task() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    runtime._independent_visual_frame_ttl_s = 0.01
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=81)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]
    release = asyncio.Event()
    validation_task = asyncio.create_task(release.wait())
    assert runtime._track_independent_visual_validation_task(
        validation_task,
        captured_at=record.started_at,
    )

    await runtime._await_independent_visual_validation_tasks(turn_id)

    assert not validation_task.done()
    release.set()
    await validation_task


@pytest.mark.unit
async def test_new_turn_wakes_visual_validation_wait_without_cancelling_task() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    first_token = VoiceTurnToken(
        ingress=runtime._capture_ingress_token(),
        turn_id=82,
    )
    first_turn_id = (
        f"asr-{first_token.ingress.session_epoch}-{first_token.turn_id}"
    )
    runtime._begin_core_multimodal_turn(first_turn_id, first_token)
    first_record = runtime._core_multimodal_turns[first_turn_id]
    release = asyncio.Event()
    validation_task = asyncio.create_task(release.wait())
    assert runtime._track_independent_visual_validation_task(
        validation_task,
        captured_at=first_record.started_at,
    )
    waiting = asyncio.create_task(
        runtime._await_independent_visual_validation_tasks(first_turn_id)
    )
    await asyncio.sleep(0)

    second_token = VoiceTurnToken(
        ingress=runtime._capture_ingress_token(),
        turn_id=83,
    )
    runtime._begin_core_multimodal_turn(
        f"asr-{second_token.ingress.session_epoch}-{second_token.turn_id}",
        second_token,
    )

    await asyncio.wait_for(waiting, timeout=0.1)
    assert not validation_task.done()
    release.set()
    await validation_task


async def test_offline_image_free_voice_turn_retries_tts_after_failure() -> None:
    runtime = _Runtime()
    runtime.response_backend = "offline_vlm"
    runtime.ensure_tts_pipeline_alive = AsyncMock(
        side_effect=[RuntimeError("tts unavailable"), None]
    )
    runtime.session.submit_external_voice_turn = AsyncMock()

    with pytest.raises(RuntimeError, match="tts unavailable"):
        await runtime._submit_core_voice_turn(
            "first",
            turn_id="turn-1",
            session_ref=runtime.session,
        )
    runtime.session.submit_external_voice_turn.assert_not_awaited()

    await runtime._submit_core_voice_turn(
        "second",
        turn_id="turn-2",
        session_ref=runtime.session,
    )

    assert runtime.ensure_tts_pipeline_alive.await_count == 2
    runtime.session.submit_external_voice_turn.assert_awaited_once_with(
        "second",
        turn_id="turn-2",
    )


@pytest.mark.unit
async def test_direct_multimodal_failure_reports_status_without_text_fallback() -> None:
    runtime = _Runtime()
    runtime.core_api_type = "openai"
    runtime.session.get_multimodal_turn_delivery = MagicMock(
        return_value="direct_atomic"
    )
    runtime.session.submit_multimodal_turn = AsyncMock(
        side_effect=RuntimeError("provider rejected image")
    )
    runtime.session.submit_external_voice_turn = AsyncMock()
    epoch = runtime._asr_session_epoch
    await _start_and_seal_turn(runtime, "openai")
    assert runtime._stage_independent_visual_frame(
        "raw-frame",
        source="screen",
        request_id="screen-1",
        captured_at=time.monotonic(),
    )

    await runtime._handle_independent_asr_final(
        "look here",
        epoch,
        "openai",
    )
    await runtime._wait_asr_transcript_dispatch_idle()

    runtime.session.submit_multimodal_turn.assert_awaited_once()
    runtime.session.submit_external_voice_turn.assert_not_awaited()
    status_payloads = [call.args[0] for call in runtime.send_status.await_args_list]
    assert any("ASR_INDEPENDENT_INJECTION_FAILED" in item for item in status_payloads)
    assert "provider rejected image" not in str(status_payloads)


@pytest.mark.unit
async def test_handoff_failure_never_falls_back_to_transcript_only() -> None:
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "qwen")
    runtime._asr_route_mode = "independent"
    runtime.session.get_multimodal_turn_delivery = MagicMock(
        return_value="handoff_required"
    )
    runtime.session.submit_external_voice_turn = AsyncMock()
    runtime._handoff_to_offline_vlm_and_submit = AsyncMock(return_value=False)
    runtime.is_preparing_new_session = True
    runtime.message_cache_for_new_session = [
        {"role": "Test", "text": "earlier reply"}
    ]

    async def cache_current_final(*_args, **_kwargs) -> bool:
        runtime.message_cache_for_new_session.append(
            {"role": "master", "text": "what is this"}
        )
        return True

    runtime.handle_input_transcript.side_effect = cache_current_final
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    assert runtime._stage_independent_visual_frame(
        "raw-frame",
        source="camera",
        request_id="camera-1",
        captured_at=time.monotonic(),
    )

    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(
            turn_token=token,
            provider="qwen",
            text="what is this",
        )
    )

    runtime._handoff_to_offline_vlm_and_submit.assert_awaited_once()
    handoff_kwargs = (
        runtime._handoff_to_offline_vlm_and_submit.await_args.kwargs
    )
    assert handoff_kwargs["prepared_session"] is runtime.session
    assert handoff_kwargs["cached_turns_before_final"] == [
        {"role": "Test", "text": "earlier reply"}
    ]
    runtime.session.submit_external_voice_turn.assert_not_awaited()
    assert "ASR_MULTIMODAL_TURN_FAILED" in str(
        runtime.send_status.await_args_list
    )


@pytest.mark.unit
async def test_native_visual_sync_failure_keeps_raw_images_blocked() -> None:
    runtime = _Runtime()
    call_order: list[str] = []

    def allow_raw_visual_delivery() -> None:
        call_order.append("allow")

    def block_raw_visual_delivery() -> None:
        call_order.append("block")

    def fail_visual_mode_sync(_mode: str) -> None:
        call_order.append("sync")
        raise RuntimeError("stale realtime session")

    runtime.session.allow_raw_visual_delivery = allow_raw_visual_delivery
    runtime.session.block_raw_visual_delivery = block_raw_visual_delivery
    runtime.session.set_visual_delivery_mode = fail_visual_mode_sync

    runtime._set_microphone_route("native")

    assert runtime._asr_route_mode == "native"
    assert call_order == ["sync", "block"]


@pytest.mark.unit
async def test_out_of_order_frame_still_joins_the_turn_sample() -> None:
    """A frame that validates late must not be dropped by the latest-frame guard."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=91)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]
    base = record.started_at

    assert runtime._stage_independent_visual_frame(
        "later-frame",
        source="screen",
        request_id="screen-later",
        captured_at=base + 1.0,
    )
    # 更早拍摄、更晚校验完：不能顶掉最新帧缓存，但必须进本回合抽样。
    assert runtime._stage_independent_visual_frame(
        "earlier-frame",
        source="camera",
        request_id="camera-earlier",
        captured_at=base + 0.1,
    )
    assert runtime._latest_independent_visual_frame.image_b64 == "later-frame"

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert turn is not None
    assert turn.images == ("earlier-frame", "later-frame")


def _seal_utterance(runtime) -> None:
    runtime._asr_lifecycle = SimpleNamespace(
        snapshot=SimpleNamespace(state=VoiceLifecycleState.DRAINING)
    )


@pytest.mark.unit
async def test_frames_captured_after_the_endpoint_are_not_folded_in() -> None:
    """Screen state from after the user stopped talking is not this turn."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=92)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    assert runtime._stage_independent_visual_frame(
        "spoken-frame",
        source="screen",
        request_id="screen-spoken",
        captured_at=record.started_at,
    )

    _seal_utterance(runtime)
    runtime._mark_independent_asr_endpoint_if_sealed()
    assert record.endpoint_at is not None
    runtime._stage_independent_visual_frame(
        "post-endpoint-frame",
        source="screen",
        request_id="screen-post",
        captured_at=record.endpoint_at + 0.5,
    )

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert turn is not None
    assert turn.images == ("spoken-frame",)


@pytest.mark.unit
async def test_frame_captured_before_the_endpoint_survives_late_validation() -> None:
    """Validation finishing after DRAINING must not discard a spoken-window frame."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=93)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]
    captured_while_speaking = record.started_at

    # 端点先到，这帧的校验任务才跑完 —— 拍摄时它还在说话，必须留下。
    _seal_utterance(runtime)
    runtime._mark_independent_asr_endpoint_if_sealed()
    assert runtime._stage_independent_visual_frame(
        "late-validated-frame",
        source="screen",
        request_id="screen-late",
        captured_at=captured_while_speaking,
    )

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert turn is not None
    assert turn.images == ("late-validated-frame",)


@pytest.mark.unit
async def test_post_endpoint_cache_frame_cannot_seed_an_empty_turn() -> None:
    """The empty-record fallback must respect the endpoint cutoff too."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=94)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    _seal_utterance(runtime)
    runtime._mark_independent_asr_endpoint_if_sealed()
    runtime._stage_independent_visual_frame(
        "post-endpoint-frame",
        source="screen",
        request_id="screen-post",
        captured_at=record.endpoint_at + 0.5,
    )
    # 缓存里有这一帧（主动搭话观察还要用），但本回合一帧都没收到。
    assert runtime._latest_independent_visual_frame.image_b64 == "post-endpoint-frame"
    assert record.last_frame is None

    assert runtime._snapshot_core_multimodal_turn(turn_id, "what is that") is None


@pytest.mark.unit
async def test_endpoint_cutoff_uses_the_recorded_seal_instant() -> None:
    """A frame captured in the gap before Core looks must still be excluded."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=95)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    assert runtime._stage_independent_visual_frame(
        "spoken-frame",
        source="screen",
        request_id="screen-spoken",
        captured_at=record.started_at,
    )

    # ASR 在这一刻封口，但 Core 要到下一帧 staging 才会去看。
    sealed_at = record.started_at + 1.0
    runtime._asr_turn_endpointed_at = sealed_at
    _seal_utterance(runtime)

    # 这帧拍摄于封口之后、Core 观察之前——按观察时刻当截止值它会被放行。
    runtime._stage_independent_visual_frame(
        "gap-frame",
        source="screen",
        request_id="screen-gap",
        captured_at=sealed_at + 0.5,
    )

    assert record.endpoint_at == sealed_at
    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert turn is not None
    assert turn.images == ("spoken-frame",)


@pytest.mark.unit
async def test_live_seal_between_onset_and_registration_still_binds() -> None:
    """The live field floors on started_at, not registered_at.

    started_at is rolled back to the speech onset (an overlapping successor can
    even predate the previous turn's seal), so a real window exists between the
    seal and the registration: a very short utterance can be sealed by ASR
    before its record is built. Flooring the live field on registered_at would
    leave such a turn without a cutoff forever, folding everything captured
    after the user stopped talking into this turn.

    Found by mutation: flipping the live branch to registered_at turned nothing
    red in this whole file before this case existed.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    # 把语音起点回拨，制造 started_at < registered_at 的真实窗口。
    onset = time.monotonic() - 0.5
    runtime._asr_turn_onset_at = onset
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=99)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]
    assert record.started_at < record.registered_at, "夹具没造出那段窗口"

    # 在飞字段：封口发生在开口之后、record 建立之前。
    sealed_at = record.started_at + 0.1
    assert sealed_at < record.registered_at
    runtime._asr_turn_endpointed_at = sealed_at

    runtime._stage_independent_visual_frame(
        "post-seal-frame",
        source="screen",
        request_id="screen-post-seal",
        captured_at=sealed_at + 0.05,
    )

    assert record.endpoint_at == sealed_at


@pytest.mark.unit
async def test_previous_turn_seal_in_the_same_tick_is_not_this_turn_cutoff() -> None:
    """A previous turn's seal in the same tick is not this turn's cutoff.

    monotonic is ~15ms coarse on Windows (_begin_core_multimodal_turn in this
    same module already falls back to a generation criterion for exactly this
    reason), so the previous turn's seal and the successor record's
    registration can land in one tick and compare equal. Stamping it onto the
    successor makes every later frame fail accepts(); once the opening frame
    expires, a slightly longer utterance degrades to text-only and the user
    sees "she only caught the instant I started talking".

    The criterion is turn identity, not the timestamp -- see the dual below.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=97)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    # 上一轮的封口副本，时刻与本轮 record 的注册时刻**相等**（同一个 tick），
    # 但身份是上一轮的。live 字段是空的——PROVIDER_FINAL 已经把它清掉了，这正是
    # 保留副本存在的原因。
    runtime._asr_turn_endpointed_at = None
    runtime._asr_last_turn_endpointed_at = record.registered_at
    runtime._asr_last_turn_endpointed_key = "asr-0-96"
    assert runtime._asr_last_turn_endpointed_key != record.turn_id

    assert runtime._stage_independent_visual_frame(
        "opening-frame",
        source="screen",
        request_id="screen-opening",
        captured_at=record.started_at,
    )
    # 发声中段拍的帧——如果上一轮的封口被误绑成本轮截止点，它会被 accepts() 拒掉。
    assert runtime._stage_independent_visual_frame(
        "middle-frame",
        source="screen",
        request_id="screen-middle",
        captured_at=record.registered_at + 1.0,
    )

    assert record.endpoint_at is None, (
        "上一轮的封口被盖到了后继回合上：相等必须归上一轮"
    )
    turn = runtime._snapshot_core_multimodal_turn(turn_id, "这是什么")

    assert turn is not None
    assert "middle-frame" in turn.images


@pytest.mark.unit
async def test_this_turn_seal_in_the_same_tick_is_still_its_cutoff() -> None:
    """The other direction: this turn's own seal must survive a tick collision.

    A very short utterance can seal inside the same ~15ms tick its record was
    registered in; PROVIDER_FINAL then clears the live field, leaving only the
    retained copy. A pure timestamp test is wrong in one direction or the
    other, and this is the half where "equality belongs to the previous turn"
    is wrong: this turn loses its cutoff and post-speech frames get folded into
    its transcript.

    Hence the criterion is turn identity, not the timestamp -- the runtime
    records which turn the retained seal belongs to.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=101)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    # 本轮自己的封口，恰好与注册落在同一个 tick 上。
    runtime._asr_turn_endpointed_at = None
    runtime._asr_last_turn_endpointed_at = record.registered_at
    runtime._asr_last_turn_endpointed_key = record.turn_id

    runtime._stage_independent_visual_frame(
        "opening-frame",
        source="screen",
        request_id="screen-opening",
        captured_at=record.started_at,
    )

    assert record.endpoint_at == record.registered_at, (
        "本轮自己的封口被当成上一轮残值丢掉了：相等时必须靠身份而不是时间戳"
    )


@pytest.mark.unit
async def test_a_seal_after_this_record_registered_still_becomes_its_cutoff() -> None:
    """Dual: a retained seal that really belongs to this turn still binds.

    Guards against over-tightening the gate into "never trust a retained copy".
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=98)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    sealed_at = record.registered_at + 1.0
    runtime._asr_turn_endpointed_at = None
    runtime._asr_last_turn_endpointed_at = sealed_at
    runtime._asr_last_turn_endpointed_key = record.turn_id

    runtime._stage_independent_visual_frame(
        "late-frame",
        source="screen",
        request_id="screen-late",
        captured_at=sealed_at + 0.5,
    )

    assert record.endpoint_at == sealed_at


@pytest.mark.unit
async def test_stale_seal_instant_from_a_previous_turn_is_not_this_turn_cutoff() -> None:
    """A leftover timestamp predates this record and must not seal it early."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    runtime._asr_turn_endpointed_at = time.monotonic() - 30.0
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=96)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    assert runtime._stage_independent_visual_frame(
        "spoken-frame",
        source="screen",
        request_id="screen-spoken",
        captured_at=record.started_at,
    )

    assert record.endpoint_at is None
    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert turn is not None
    assert turn.images == ("spoken-frame",)


@pytest.mark.unit
async def test_endpoint_cutoff_survives_provider_final_clearing_the_live_field() -> None:
    """PROVIDER_FINAL clears the live timestamp before Core freezes the turn."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=97)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    assert runtime._stage_independent_visual_frame(
        "spoken-frame",
        source="screen",
        request_id="screen-spoken",
        captured_at=record.started_at,
    )

    # 封口 -> provider final：runtime 清掉了 live 字段，lifecycle 也已经离开
    # DRAINING，只剩下不随 final 清除的那个副本。
    sealed_at = record.started_at + 1.0
    runtime._asr_turn_endpointed_at = None
    runtime._asr_last_turn_endpointed_at = sealed_at
    runtime._asr_lifecycle = SimpleNamespace(
        snapshot=SimpleNamespace(state=VoiceLifecycleState.WARM_IDLE)
    )

    # 端点之后拍的帧在 final 派发期间才校验完。
    runtime._stage_independent_visual_frame(
        "post-endpoint-frame",
        source="screen",
        request_id="screen-post",
        captured_at=sealed_at + 0.5,
    )

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert record.endpoint_at == sealed_at
    assert turn is not None
    assert turn.images == ("spoken-frame",)


@pytest.mark.unit
async def test_frame_validated_during_lifecycle_notification_joins_the_turn() -> None:
    """Speech onset, not record creation, is the ownership boundary."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()
    # 刻意只设 _asr_turn_onset_at：_asr_turn_audio_started_at 在两条生产路径上是
    # 投递完成之后才打的，用它当起点正是被修掉的那个缺陷，所以这条用例不能靠它。
    runtime._asr_turn_onset_at = onset

    # 语音已确认，Core 还卡在 _send_asr_lifecycle_state 的投递里；这一帧就是这段
    # 发声的开头（用户开口时指的东西），它先于 record 落地。
    assert runtime._stage_independent_visual_frame(
        "onset-frame",
        source="screen",
        request_id="screen-onset",
        captured_at=onset + 0.01,
    )

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=98)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert turn is not None
    assert turn.images == ("onset-frame",)


@pytest.mark.unit
async def test_frame_captured_before_the_onset_is_still_a_prior_turn_frame() -> None:
    """Widening the window to the onset must not reach into the previous turn."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()

    assert runtime._stage_independent_visual_frame(
        "prior-turn-frame",
        source="screen",
        request_id="screen-prior",
        captured_at=onset - 1.0,
    )
    runtime._asr_turn_onset_at = onset

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=99)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)

    assert runtime._snapshot_core_multimodal_turn(turn_id, "new question") is None


@pytest.mark.unit
async def test_prerecord_validation_task_is_attached_to_the_onset_record() -> None:
    """A frame task created before the record exists must not be dropped."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()
    runtime._asr_turn_onset_at = onset

    gate = asyncio.Event()

    async def pending_validation() -> None:
        await gate.wait()

    task = asyncio.create_task(pending_validation())
    await asyncio.sleep(0)

    # record 还没建出来：这一步在旧实现里等于永久丢弃这个任务。
    assert runtime._track_independent_visual_validation_task(
        task,
        captured_at=onset + 0.01,
    ) is False

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=100)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    assert task in record.pending_visual_validations
    assert runtime._prerecord_visual_validations == {}

    gate.set()
    await task


@pytest.mark.unit
async def test_prerecord_validation_stash_is_bounded() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()
    runtime._asr_turn_onset_at = onset
    gate = asyncio.Event()

    async def pending_validation() -> None:
        await gate.wait()

    tasks = [asyncio.create_task(pending_validation()) for _ in range(40)]
    await asyncio.sleep(0)
    for task in tasks:
        runtime._track_independent_visual_validation_task(
            task,
            captured_at=onset + 0.01,
        )

    assert len(runtime._prerecord_visual_validations) <= 8

    gate.set()
    await asyncio.gather(*tasks)


@pytest.mark.unit
def test_speech_onset_is_stamped_at_the_transition_not_after_delivery() -> None:
    """The onset stamp must not sit behind an awaited lifecycle notification.

    Two production SPEECH_CONFIRMED paths stamp ``_asr_turn_audio_started_at``
    only after awaiting ``_send_asr_lifecycle_state()``. Visual ownership uses
    the onset as its lower bound, so a stamp taken after that await turns every
    frame captured during delivery into a "not this utterance" frame. The
    invariant is syntactic: the stamp follows the transition with no await in
    between.
    """
    import inspect

    from main_logic.asr_client import lifecycle as asr_lifecycle_module
    from main_logic.asr_client import runtime as asr_runtime_module

    source = inspect.getsource(asr_runtime_module).splitlines()

    # ⚠️ 这个守卫的第一版只扫 runtime.py 里的字面量
    # `lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)`，因此完全看不见
    # lifecycle.py 自己的 `self.transition(...)`（begin_pending_turn 里那一处）——
    # 第五个迁移点就是这么漏掉的，还给了"五处都打点了"的假绿。清单式守卫必须自己
    # 证明清单是全的：先跨模块把所有迁移点数出来，再逐个查。
    lifecycle_source = inspect.getsource(asr_lifecycle_module).splitlines()
    lifecycle_sites = [
        index
        for index, line in enumerate(lifecycle_source)
        if "transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)" in line
    ]
    # lifecycle 侧的迁移点没有 runtime 字段可写，只能要求它的**调用方**补打点。
    for index in lifecycle_sites:
        owner = None
        for back in range(index, -1, -1):
            stripped = lifecycle_source[back].strip()
            if stripped.startswith("def "):
                owner = stripped[4:].split("(")[0]
                break
        assert owner is not None
        callers = [
            i for i, line in enumerate(source) if f"lifecycle.{owner}()" in line
        ]
        assert callers, (
            f"lifecycle.{owner}() performs a SPEECH_CONFIRMED transition but no "
            f"runtime call site was found to stamp the onset"
        )
        for caller in callers:
            window = chr(10).join(source[caller : caller + 12])
            assert "self._asr_turn_onset_at" in window, (
                f"runtime line {caller + 1}: lifecycle.{owner}() transitions to "
                f"SPEECH_CONFIRMED, so its caller must stamp the onset; got: "
                f"{window!r}"
            )
    transition = "lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)"
    stamp = "self._asr_turn_onset_at ="
    sites = [i for i, line in enumerate(source) if transition in line]

    assert sites, "no SPEECH_CONFIRMED transition found"
    for index in sites:
        # 赋值必须**紧接**转换那一行开始（注释和空行不算，它们引入不了 await）。
        # 值本身可以是多行表达式：几条路径都要在"暂存的 onset"和"进函数时刻"之间选。
        first = next(
            offset
            for offset in range(1, 12)
            if source[index + offset].strip()
            and not source[index + offset].strip().startswith("#")
        )
        assert source[index + first].strip().startswith(stamp), (
            f"line {index + 1}: SPEECH_CONFIRMED must start stamping the onset "
            f"before anything else, got: {source[index + first].strip()!r}"
        )

    # 每一条路径的 onset 赋值都必须**优先取暂存的 pending onset**，只有它为空时才
    # 用进函数时刻。session 先未就绪、随后又 ready 时，真实开口时刻就是当初记下的
    # 那个值；就地取时钟会把整段重连等待算成「开口之后」，期间拍的帧全被排除。
    #
    # 规则对所有迁移点一视同仁，因此不再需要"哪条是延迟路径"这种启发式识别 ——
    # 之前那版靠往上扫若干行找条件语句，既会跨函数误标，也挡不住直接分支退化。
    for index in sites:
        begin = next(
            offset
            for offset in range(1, 12)
            if source[index + offset].strip()
            and not source[index + offset].strip().startswith("#")
        )
        statement = []
        depth = 0
        for offset in range(begin, begin + 9):
            line = source[index + offset]
            statement.append(line)
            depth += line.count("(") - line.count(")")
            if depth <= 0:
                break
        window = chr(10).join(statement)
        assert "self._asr_pending_speech_onset_at" in window, (
            f"line {index + 1}: the onset assignment must prefer the pending "
            f"onset captured before the reconnect, got: {window!r}"
        )

    # detected_at 本身必须在函数里任何 await 之前捕获。
    for index, line in enumerate(source):
        if line.strip() != "detected_at = time.monotonic()":
            continue
        for back in range(index, -1, -1):
            stripped = source[back].strip()
            if stripped.startswith(("async def ", "def ")):
                break
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("await ") and " await " not in stripped, (
                f"line {index + 1}: detected_at must be captured before any await; "
                f"line {back + 1} is {stripped!r}"
            )

    # 暂存的 pending turn onset 也必须用进函数时刻。函数入口已经存了 detected_at
    # （上面那条规则保证它在任何 await 之前），DRAINING 分支再读一次时钟等于把
    # 「进函数 → 走到这一行」之间拍的帧排除在这段发声之外，而这个字段正是后面
    # begin_pending_turn 那处 _asr_turn_onset_at 的来源。
    for index, line in enumerate(source):
        stripped = line.strip()
        if not stripped.startswith("self._asr_pending_turn_onset_at = "):
            continue
        rhs = stripped.split(" = ", 1)[1]
        if rhs == "None":
            continue
        captures_detected_at = False
        for back in range(index, -1, -1):
            # 只在**方法**定义处收边（4 空格缩进）。这些函数里 detected_at 与
            # DRAINING 分支之间隔着 event_is_current / wake_is_current 这类嵌套
            # def，按 "任意 def" 收边会提前停下，规则对这两处直接失效。
            if source[back].startswith(("    def ", "    async def ")):
                break
            if source[back].strip() == "detected_at = time.monotonic()":
                captures_detected_at = True
                break
        if not captures_detected_at:
            continue
        assert rhs == "detected_at", (
            f"line {index + 1}: the pending turn onset must carry the entry "
            f"timestamp its function already captured, got: {rhs!r}"
        )


@pytest.mark.unit
async def test_reconnect_listener_join_is_bounded() -> None:
    """A receive task that swallows cancellation must not wedge the swap lock."""
    from main_logic.core import LLMSessionManager

    manager = LLMSessionManager.__new__(LLMSessionManager)
    manager.lanlan_name = "Test"
    manager.lock = asyncio.Lock()
    manager.is_active = True
    manager._core_voice_listener_cancel_timeout_s = 0.05
    manager.session_ready = True
    manager._close_independent_asr = AsyncMock()
    manager.send_session_ended_by_server = AsyncMock()
    session = SimpleNamespace(handle_messages=AsyncMock(), close=AsyncMock())
    manager.session = session

    stuck_release = asyncio.Event()

    async def stuck_listener() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await stuck_release.wait()

    listener = asyncio.create_task(stuck_listener())
    manager.message_handler_task = listener
    await asyncio.sleep(0)

    installed = await asyncio.wait_for(
        manager._restart_message_handler_after_session_reconnect(session),
        5.0,
    )

    # fail-closed：停不下来的 listener 还绑在退休会话上，不能在它之上再装一个
    # receive 循环；调用方都把 False 当成"放弃这次重连"。
    assert installed is False
    session.handle_messages.assert_not_called()

    # 而且必须把这条会话**退休**掉：只返回 False 会留下一个看起来还活着、实际没有
    # receive 循环的 client，之后每一轮都撞上同一个卡死的 task 再超时一次，语音从此
    # 永远收不到回复。
    assert manager.session is None
    assert manager.message_handler_task is None
    assert manager.is_active is False
    assert manager.session_ready is False

    # 会话没了，麦克风也必须收掉：否则独立 ASR 继续往一个不存在的回答会话投
    # transcript，用户说什么都石沉大海。
    manager._close_independent_asr.assert_awaited_once_with(next_route_mode="blocked")
    manager.send_session_ended_by_server.assert_awaited_once_with()

    stuck_release.set()
    await asyncio.gather(listener, return_exceptions=True)
    for _ in range(50):
        if session.close.await_count:
            break
        await asyncio.sleep(0.01)
    session.close.assert_awaited_once_with()


@pytest.mark.unit
async def test_pending_turn_does_not_inherit_the_previous_turn_endpoint() -> None:
    """A turn started while the previous one drained must not be sealed by it.

    ``_asr_turn_onset_at`` survives a normal turn end (only close/abort/error
    clear it), and ``_asr_last_turn_endpointed_at`` is never cleared. If the
    pending-turn activation forgets to re-stamp the onset, Core takes the
    PREVIOUS turn's onset as this record's ``started_at``, the previous seal
    then satisfies ``sealed_at >= started_at``, and every frame captured for
    the new utterance is rejected as post-endpoint — a silent text-only turn.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    previous_onset = time.monotonic() - 2.0
    previous_seal = previous_onset + 1.0
    runtime._asr_turn_onset_at = previous_onset          # 上一轮遗留，没人清
    runtime._asr_turn_endpointed_at = None               # PROVIDER_FINAL 已清
    runtime._asr_last_turn_endpointed_at = previous_seal  # 永不清
    # pending turn 在上一轮排空期间被标记，之后才激活。
    runtime._asr_turn_onset_at = previous_seal + 0.2

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=101)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    # 截止点是在第一次 staging（或 final 冻结）时才认领的，所以要先喂一帧再判。
    assert runtime._stage_independent_visual_frame(
        "new-utterance-frame",
        source="screen",
        request_id="screen-new",
        captured_at=record.started_at + 0.1,
    )
    assert record.endpoint_at is None, (
        "the previous turn's seal must not become this turn's cutoff"
    )
    turn = runtime._snapshot_core_multimodal_turn(turn_id, "and this one?")

    assert turn is not None
    assert turn.images == ("new-utterance-frame",)


@pytest.mark.unit
async def test_retained_seal_predating_registration_is_not_this_turn_cutoff() -> None:
    """Second line of defence behind the onset stamp.

    A retained seal survives across turns, so "is it >= started_at" cannot tell
    whether it belongs to this turn — an overlapping successor's onset is even
    recorded BEFORE the predecessor sealed. The floor for the retained copy is
    therefore the moment the record was registered: the previous turn's seal
    necessarily happened before that.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    previous_onset = time.monotonic() - 1.0
    previous_seal = previous_onset + 0.5
    # 模拟"激活 pending turn 时忘了补 onset"：留着上一轮的 onset。
    runtime._asr_turn_onset_at = previous_onset
    runtime._asr_turn_endpointed_at = None
    runtime._asr_last_turn_endpointed_at = previous_seal

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=102)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    assert runtime._stage_independent_visual_frame(
        "post-seal-frame",
        source="screen",
        request_id="screen-post",
        captured_at=previous_seal + 0.5,
    )
    # 即使 onset 是上一轮的残值（第一道防线失效），上一轮的封口也不能成为本轮的
    # 截止点 —— 它发生在本 record 注册之前。
    assert record.endpoint_at is None
    turn = runtime._snapshot_core_multimodal_turn(turn_id, "survives")

    assert turn is not None
    assert turn.images == ("post-seal-frame",)


@pytest.mark.unit
async def test_all_prerecord_frames_join_the_turn_not_just_the_newest() -> None:
    """Frames validated before the record exists must survive as a span.

    The single-slot cache keeps only the newest frame, and the pending-task
    stash drops a task the moment it completes. If lifecycle delivery is slow
    enough for several validations to land first, keeping only the newest one
    silently loses the actual first/middle frames of the utterance.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()
    runtime._asr_turn_onset_at = onset

    for index in range(3):
        assert runtime._stage_independent_visual_frame(
            f"prerecord-{index}",
            source="screen",
            request_id=f"screen-{index}",
            captured_at=onset + 0.01 * (index + 1),
        )
    assert len(runtime._prerecord_visual_frames) == 3

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=103)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "what is that")

    assert turn is not None
    # 开头 / 中间 / 结尾都在，而不是只剩最新那张。
    assert turn.images == ("prerecord-0", "prerecord-1", "prerecord-2")
    # 消费即清空，不会漏进下一轮。
    assert runtime._prerecord_visual_frames == []


@pytest.mark.unit
async def test_prerecord_frame_buffer_is_bounded() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()
    runtime._asr_turn_onset_at = onset

    for index in range(40):
        runtime._stage_independent_visual_frame(
            f"prerecord-{index}",
            source="screen",
            request_id=f"screen-{index}",
            captured_at=onset + 0.001 * (index + 1),
        )

    assert len(runtime._prerecord_visual_frames) <= 8
    # 超限时丢的是"最冗余"的内点，**不是队头** —— 队头正是这段发声的开头。
    kept = [frame.image_b64 for frame in runtime._prerecord_visual_frames]
    assert kept[0] == "prerecord-0"
    assert kept[-1] == "prerecord-39"


@pytest.mark.unit
async def test_prerecord_frames_from_a_previous_route_are_not_adopted() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()
    runtime._asr_turn_onset_at = onset

    assert runtime._stage_independent_visual_frame(
        "prerecord-frame",
        source="screen",
        request_id="screen-0",
        captured_at=onset + 0.01,
    )
    # 路由换代之后，那一帧不再属于这条链路。
    runtime._voice_input_transition_generation += 1

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=104)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    # 关键断言打在"有没有被并进 record"上。只断言 snapshot 为 None 是不够的 ——
    # accepts() 在冻结时还会按 route_generation 再过滤一次，采纳环节即使漏判也照样
    # 返回 None，那样这条用例就是假绿（实测：去掉采纳侧的 route 过滤仍然通过）。
    assert record.last_frame is None
    assert record.first_frame is None
    assert runtime._snapshot_core_multimodal_turn(turn_id, "lost") is None


@pytest.mark.unit
def test_overlap_replay_carries_the_real_onset_not_the_replay_instant() -> None:
    """The overlap replay happens long after the user actually resumed speaking.

    A provider-VAD successor utterance can reach Core while the previous turn
    is still ACTIVE; its onset is remembered and replayed only once the delayed
    final arrives. Stamping the replay instant as the onset would classify
    everything captured in between as "after the user spoke", so the successor
    utterance loses the frames it was actually about.
    """
    import inspect

    from main_logic.asr_client import runtime as asr_runtime_module

    source = inspect.getsource(asr_runtime_module).splitlines()

    record = [
        index
        for index, line in enumerate(source)
        if "self._asr_overlap_onset_token = self._asr_current_ingress_token" in line
    ]
    assert record, "overlap onset token is never recorded"
    for index in record:
        window = chr(10).join(source[index : index + 3])
        assert "self._asr_overlap_onset_at = detected_at" in window, (
            f"line {index + 1}: the overlap onset instant must be recorded "
            f"alongside its token, got: {window!r}"
        )

    # 只认「把 SPEECH_RESUMED 重放给 _handle_independent_asr_activity」那一处，
    # 不要把无关的集合字面量里出现的同名枚举也算进来。
    # 只认 overlap **重放**那一处：它由「兑付一次 completed-overlap credit」的那段
    # 代码驱动。同名枚举在别处也会被正常派发（那些是真实发生的时刻，用进函数时钟
    # 是对的），不能一并要求它们交接 onset。
    # overlap 有**两条**重放路径：credit 兑付那条，和 provider final 到达时的直接
    # 重放。两条都必须把真实开口时刻交给确认分支 —— 只修其中一条正是上一轮的漏。
    replay = [
        index
        for index, line in enumerate(source)
        if "await self._handle_independent_asr_activity(" in line
        and "SpeechActivityEvent.SPEECH_RESUMED," in source[index + 1]
    ]
    assert len(replay) >= 2, f"expected both overlap replay paths, got {len(replay)}"
    for index in replay:
        window = chr(10).join(source[max(0, index - 30) : index])
        # credit 兑付那条按队列 popleft（每张 credit 一个时刻），直接重放那条用它
        # 自己捕获的 overlap_onset_at。两条都必须交接。
        assert (
            "self._asr_pending_speech_onset_at = replay_onset_at" in window
            or "self._asr_pending_speech_onset_at = overlap_onset_at" in window
        ), (
            f"line {index + 1}: every overlap replay must hand the recorded "
            f"onset to the confirmation path, got: {window!r}"
        )


@pytest.mark.unit
async def test_overlapping_successor_is_not_sealed_by_its_predecessor() -> None:
    """The successor's onset predates the predecessor's seal — by design.

    A provider-VAD successor utterance begins while the previous turn is still
    ACTIVE, so its recorded onset is EARLIER than the previous turn's endpoint.
    Comparing the retained seal against ``started_at`` would therefore bind the
    predecessor's endpoint to the successor and reject every frame it captures.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    successor_onset = time.monotonic() - 1.0
    predecessor_seal = successor_onset + 0.3
    runtime._asr_turn_onset_at = successor_onset
    runtime._asr_turn_endpointed_at = None
    runtime._asr_last_turn_endpointed_at = predecessor_seal

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=105)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]
    assert record.started_at < predecessor_seal

    assert runtime._stage_independent_visual_frame(
        "successor-frame",
        source="screen",
        request_id="screen-successor",
        captured_at=predecessor_seal + 0.4,
    )
    assert record.endpoint_at is None

    turn = runtime._snapshot_core_multimodal_turn(turn_id, "and this?")

    assert turn is not None
    assert turn.images == ("successor-frame",)


@pytest.mark.unit
async def test_live_endpoint_still_seals_its_own_turn() -> None:
    """The live field only ever describes the in-flight turn, so keep it loose."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=106)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    assert runtime._stage_independent_visual_frame(
        "spoken-frame",
        source="screen",
        request_id="screen-spoken",
        captured_at=record.started_at,
    )
    # 极短发声：封口甚至可能早于 record 注册那一刻。live 字段仍然必须绑上。
    sealed_at = record.started_at
    runtime._asr_turn_endpointed_at = sealed_at
    runtime._mark_independent_asr_endpoint_if_sealed()

    assert record.endpoint_at == sealed_at


@pytest.mark.unit
async def test_prerecord_buffer_trims_in_capture_order_not_arrival_order() -> None:
    """Concurrent validation means arrival order is not capture order.

    The cap evicts the most redundant INTERIOR point and keeps both ends. If the
    buffer is held in arrival order, those "ends" are not the temporal first and
    last, so the eviction can drop the actual start of the utterance — the same
    trap already fixed once for the middle-frame candidates.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()
    runtime._asr_turn_onset_at = onset

    # 落地顺序把两端交替喂进来：0, 19, 1, 18, 2, 17, ...
    capture_order = [i if i % 2 == 0 else 19 - i for i in range(20)]
    for generation, index in enumerate(capture_order):
        runtime._stage_independent_visual_frame(
            f"f{index}",
            source="screen",
            request_id=f"screen-{generation}",
            captured_at=onset + 0.001 * (index + 1),
        )

    kept = [frame.image_b64 for frame in runtime._prerecord_visual_frames]
    assert len(kept) <= 8
    # 时间上的首尾必须活着，而不是"最先/最后落地的那两帧"。
    assert kept[0] == "f0"
    assert kept[-1] == f"f{max(capture_order)}"
    captured = [frame.captured_at for frame in runtime._prerecord_visual_frames]
    assert captured == sorted(captured)


@pytest.mark.unit
async def test_prerecord_task_stash_keeps_the_earliest_validation() -> None:
    """Evicting the oldest task drops the opening frame of the utterance.

    The router registers validation tasks in capture order, so the oldest entry
    is the earliest capture. If a short utterance reaches final before that
    evicted task completes, the final freeze cannot wait for it and the record
    is abandoned before the opening frame lands.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    onset = time.monotonic()
    runtime._asr_turn_onset_at = onset
    gate = asyncio.Event()

    async def pending_validation() -> None:
        await gate.wait()

    tasks = [asyncio.create_task(pending_validation()) for _ in range(30)]
    await asyncio.sleep(0)
    for index, task in enumerate(tasks):
        runtime._track_independent_visual_validation_task(
            task,
            captured_at=onset + 0.001 * index,
        )

    stash = runtime._prerecord_visual_validations
    assert len(stash) <= 8
    kept = sorted(stash.values())
    # 时间上的首尾都必须活着 —— 淘汰只能发生在中间。
    assert kept[0] == onset
    assert kept[-1] == onset + 0.001 * 29

    gate.set()
    await asyncio.gather(*tasks)


@pytest.mark.unit
async def test_new_prepare_does_not_erase_a_preceding_turn_record() -> None:
    """An in-flight accepted final must still find its own record.

    The preceding final can still be running in TranscriptDispatcher (for
    example awaiting the bounded visual-validation join) when the successor is
    prepared. Clearing every record there makes that dispatch fail its identity
    self-check and return without recording OR submitting the transcript — the
    overlapping utterance erases a complete user turn.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    first = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=201)
    first_id = f"asr-{first.ingress.session_epoch}-{first.turn_id}"
    runtime._begin_core_multimodal_turn(first_id, first)
    first_record = runtime._core_multimodal_turns[first_id]

    second = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=202)
    second_id = f"asr-{second.ingress.session_epoch}-{second.turn_id}"
    runtime._begin_core_multimodal_turn(second_id, second)

    # 前一条的记录仍在，且仍是同一个对象 —— 身份自检因此不会误判。
    assert runtime._core_multimodal_turns.get(first_id) is first_record
    # 但它已被标记作废：图归新回合，旧 final 只是别被整句丢掉。
    assert first_record.invalidated.is_set()
    assert runtime._core_multimodal_turns.get(second_id) is not None


@pytest.mark.unit
async def test_retained_turn_records_are_bounded() -> None:
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    for turn_id in range(210, 230):
        token = VoiceTurnToken(
            ingress=runtime._capture_ingress_token(), turn_id=turn_id
        )
        runtime._begin_core_multimodal_turn(
            f"asr-{token.ingress.session_epoch}-{token.turn_id}", token
        )

    # 记录本该由各自 dispatch 的 finally 移除；这个上限只是内存兜底。
    assert len(runtime._core_multimodal_turns) <= 8
    # 留下的是最近的那些 —— 淘汰绝不能挑到最新那条（它才是当前在跑的）。
    kept = sorted(runtime._core_multimodal_turns)
    assert kept[-1].endswith("-229")


@pytest.mark.unit
async def test_successor_prepares_do_not_evict_a_still_running_final() -> None:
    """A record is removed by its own dispatch, never by a successor's prepare.

    An accepted final can sit inside handle_input_transcript for a while (bounded
    visual-validation join, provider submit). Meanwhile provider VAD can prepare
    several successor utterances. Evicting the oldest record to make room drops
    the identity that in-flight final needs, so the user's whole sentence is
    neither stored nor submitted.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    running = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=401)
    running_id = f"asr-{running.ingress.session_epoch}-{running.turn_id}"
    runtime._begin_core_multimodal_turn(running_id, running)
    running_record = runtime._core_multimodal_turns[running_id]

    for turn_id in (402, 403, 404):
        token = VoiceTurnToken(
            ingress=runtime._capture_ingress_token(), turn_id=turn_id
        )
        runtime._begin_core_multimodal_turn(
            f"asr-{token.ingress.session_epoch}-{token.turn_id}", token
        )

    assert runtime._core_multimodal_turns.get(running_id) is running_record

    # 它自己的 dispatch 收尾时才该消失。
    runtime._abandon_core_voice_turn(running_id, session_ref=None)
    assert running_id not in runtime._core_multimodal_turns


@pytest.mark.unit
async def test_a_dispatching_record_outlives_the_cap() -> None:
    """The cap must never be the thing that drops an accepted final.

    Raising the limit only moves the failure to a higher overlap count. What
    decides eviction is whether that record's own dispatch has finished -- the
    dict is bounded by removals from each dispatch's own finally, and a run of
    prepares long enough to hit the cap must skip anything mid-dispatch.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    running = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=501)
    running_id = f"asr-{running.ingress.session_epoch}-{running.turn_id}"
    runtime._begin_core_multimodal_turn(running_id, running)
    running_record = runtime._core_multimodal_turns[running_id]
    running_record.dispatch_started = True

    # 远多于上限的后继 prepare。
    for turn_id in range(502, 502 + _MAX_LIVE_TURN_RECORDS * 3):
        token = VoiceTurnToken(
            ingress=runtime._capture_ingress_token(), turn_id=turn_id
        )
        runtime._begin_core_multimodal_turn(
            f"asr-{token.ingress.session_epoch}-{token.turn_id}", token
        )

    assert runtime._core_multimodal_turns.get(running_id) is running_record
    # 没在派发的那些仍然有界。
    assert len(runtime._core_multimodal_turns) <= _MAX_LIVE_TURN_RECORDS


@pytest.mark.unit
async def test_all_records_mid_dispatch_keeps_them_past_the_cap() -> None:
    """When nothing is evictable the cap yields, it does not pick a victim.

    Every record in the dict belongs to a final that is still being dispatched,
    so evicting any of them drops a sentence the user already finished. Going
    over the cap is the lesser failure: unbounded growth would mean a dispatch
    that never returns, which is a different bug and must not be papered over
    by discarding speech.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    for turn_id in range(701, 701 + _MAX_LIVE_TURN_RECORDS + 4):
        token = VoiceTurnToken(
            ingress=runtime._capture_ingress_token(), turn_id=turn_id
        )
        record_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
        runtime._begin_core_multimodal_turn(record_id, token)
        # 每一条都立刻进入派发，于是永远没有可淘汰的记录。
        runtime._core_multimodal_turns[record_id].dispatch_started = True

    assert len(runtime._core_multimodal_turns) == _MAX_LIVE_TURN_RECORDS + 4
    assert all(
        record.dispatch_started
        for record in runtime._core_multimodal_turns.values()
    )
    # 各自的 dispatch 收尾时才回落到界内。
    for record_id in list(runtime._core_multimodal_turns)[:4]:
        runtime._abandon_core_voice_turn(record_id, session_ref=None)
    assert len(runtime._core_multimodal_turns) == _MAX_LIVE_TURN_RECORDS


@pytest.mark.unit
async def test_the_real_dispatch_marks_its_record_before_it_can_be_evicted() -> None:
    """The flag has to be set by the dispatch itself, not only in a test.

    A guard that only checks the eviction predicate passes even when nothing
    ever sets the flag; this drives the actual final through
    ``_dispatch_core_asr_transcript`` and lets a long run of successor prepares
    land while it is suspended.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    runtime._asr_route_mode = "independent"
    runtime.session.submit_external_voice_turn = AsyncMock()
    token = runtime._asr_runtime._capture_turn_token(runtime._asr_lifecycle)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    accepted = runtime.handle_input_transcript
    seen_mid_dispatch = {}

    async def accept_then_let_successors_pile_up(*args, **kwargs):
        result = await accepted(*args, **kwargs)
        for turn_id_n in range(601, 601 + _MAX_LIVE_TURN_RECORDS * 2):
            successor = VoiceTurnToken(
                ingress=runtime._capture_ingress_token(),
                turn_id=turn_id_n,
            )
            runtime._begin_core_multimodal_turn(
                f"asr-{successor.ingress.session_epoch}-{successor.turn_id}",
                successor,
            )
        seen_mid_dispatch["record"] = runtime._core_multimodal_turns.get(turn_id)
        return result

    runtime.handle_input_transcript = accept_then_let_successors_pile_up

    await runtime._dispatch_core_asr_transcript(
        VoiceTranscriptEvent(
            turn_token=token,
            provider="openai",
            text="the sentence that must not be dropped",
        )
    )

    assert seen_mid_dispatch["record"] is record
    runtime.session.submit_external_voice_turn.assert_awaited_once()
    # 自己的 finally 摘掉它。
    assert turn_id not in runtime._core_multimodal_turns


@pytest.mark.unit
async def test_validation_tracking_picks_the_active_record_not_a_retained_one() -> None:
    """Retained records exist only so an in-flight final keeps its transcript.

    They are invalidated; the active turn is the newest live one. Selecting
    "whichever record happens to be first" binds new frame validations to a
    superseded turn.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    first = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=301)
    first_id = f"asr-{first.ingress.session_epoch}-{first.turn_id}"
    runtime._begin_core_multimodal_turn(first_id, first)
    first_record = runtime._core_multimodal_turns[first_id]

    second = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=302)
    second_id = f"asr-{second.ingress.session_epoch}-{second.turn_id}"
    runtime._begin_core_multimodal_turn(second_id, second)
    second_record = runtime._core_multimodal_turns[second_id]

    gate = asyncio.Event()

    async def pending_validation() -> None:
        await gate.wait()

    task = asyncio.create_task(pending_validation())
    await asyncio.sleep(0)
    assert runtime._track_independent_visual_validation_task(
        task,
        captured_at=second_record.started_at,
    ) is True

    assert task in second_record.pending_visual_validations
    assert task not in first_record.pending_visual_validations

    gate.set()
    await task


@pytest.mark.unit
async def test_invalidated_record_does_not_hand_over_its_frames() -> None:
    """A superseded turn keeps its words but not the successor's frames."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    first = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=303)
    first_id = f"asr-{first.ingress.session_epoch}-{first.turn_id}"
    runtime._begin_core_multimodal_turn(first_id, first)
    record = runtime._core_multimodal_turns[first_id]
    assert runtime._stage_independent_visual_frame(
        "first-turn-frame",
        source="screen",
        request_id="screen-first",
        captured_at=record.started_at,
    )
    assert runtime._snapshot_core_multimodal_turn(first_id, "first") is not None

    second = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=304)
    runtime._begin_core_multimodal_turn(
        f"asr-{second.ingress.session_epoch}-{second.turn_id}", second
    )

    # 记录还在（话要留住），但视觉所有权已经交给后继回合 —— 走纯文本提交。
    assert runtime._core_multimodal_turns.get(first_id) is record
    assert runtime._snapshot_core_multimodal_turn(first_id, "first") is None


@pytest.mark.unit
async def test_prerecord_stash_still_arms_while_older_records_are_retained() -> None:
    """The dict is no longer empty between turns, so 'no records' is the wrong test."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    first = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=305)
    first_id = f"asr-{first.ingress.session_epoch}-{first.turn_id}"
    runtime._begin_core_multimodal_turn(first_id, first)
    runtime._core_multimodal_turns[first_id].invalidated.set()

    onset = time.monotonic()
    runtime._asr_turn_onset_at = onset
    assert runtime._stage_independent_visual_frame(
        "between-turns-frame",
        source="screen",
        request_id="screen-between",
        captured_at=onset,
    )

    # 当前这一轮还没建起来，这帧必须被暂存下来等它。
    assert [f.image_b64 for f in runtime._prerecord_visual_frames] == [
        "between-turns-frame"
    ]


@pytest.mark.unit
async def test_live_onset_replay_waits_behind_queued_overlap_credits() -> None:
    """FIFO order decides who gets replayed, not who is newest.

    A completed onset/pause cycle (turn 2) and a still-live onset (turn 3) can
    coexist when turn 1's final is delayed. The provider FIFO still delivers
    turn 2's endpoint/final first, so replaying turn 3 right now hands turn 2's
    endpoint a turn-3 record: turn 2's transcript takes turn 3's visual window,
    and turn 3's own endpoint finds no credit left, dropping its final.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    # Turn 2: a full onset/pause cycle while turn 1 is ACTIVE -> one credit.
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    assert runtime._asr_overlap_completed_turns == 1
    # Turn 3: onset only -- the user is still speaking, so it stays in the
    # single slot instead of becoming a credit.
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    assert runtime._asr_overlap_onset_token is not None

    # Turn 1's delayed final. Turn 3 must NOT be replayed here.
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_overlap_completed_turns == 1
    assert runtime._asr_overlap_onset_token is not None

    # Turn 2 redeems its own credit, in its own FIFO slot.
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("second", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()
    # Credits are drained, so turn 3's onset finally gets its replay.
    assert runtime._asr_overlap_completed_turns == 0

    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("third", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second", "third"]
    assert runtime.handle_new_message.await_count == 3
    assert runtime._asr_overlap_onset_token is None


@pytest.mark.unit
async def test_endpoint_marking_skips_invalidated_records() -> None:
    """A successor's seal has no business landing on a superseded record."""
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    first = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=401)
    first_id = f"asr-{first.ingress.session_epoch}-{first.turn_id}"
    runtime._begin_core_multimodal_turn(first_id, first)
    retained = runtime._core_multimodal_turns[first_id]

    second = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=402)
    second_id = f"asr-{second.ingress.session_epoch}-{second.turn_id}"
    runtime._begin_core_multimodal_turn(second_id, second)
    active = runtime._core_multimodal_turns[second_id]

    runtime._asr_turn_endpointed_at = time.monotonic()
    runtime._mark_independent_asr_endpoint_if_sealed()

    assert active.endpoint_at is not None
    assert retained.endpoint_at is None


@pytest.mark.unit
async def test_frames_after_a_sealed_turn_are_kept_for_the_successor() -> None:
    """A sealed record is done taking frames, so it must not block the buffer.

    Between the endpoint and the provider final, the record is sealed but not
    yet invalidated -- the successor cannot be prepared until that final lands.
    Frames captured in that window fail the sealed record's ``accepts()``
    (they are past its endpoint), so if it still counts as the active record
    they are neither attached nor retained: the successor turn loses its
    opening and middle frames and keeps only the latest-frame cache.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=901)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    # 这一轮说完了：封口，但 provider final 还没回来，所以还没作废。
    record.endpoint_at = record.started_at + 1.0
    assert not record.invalidated.is_set()
    assert runtime._active_multimodal_turn_record() is None

    # 后继开口，帧在封口之后拍到。
    assert runtime._stage_independent_visual_frame(
        "successor-opening-frame",
        source="screen",
        request_id="screen-successor",
        captured_at=record.endpoint_at + 0.5,
    )

    # 它进不了已封口的那条记录，但必须被留住给后继。
    assert [f.image_b64 for f in runtime._prerecord_visual_frames] == [
        "successor-opening-frame"
    ]


@pytest.mark.unit
async def test_a_late_registration_still_adopts_its_real_onset() -> None:
    """Waiting behind a provider final does not make an onset stale.

    An overlapping utterance registers only after the previous turn's final
    lands, and that provider timeout reaches 40s in the registry. Judging the
    onset by the FRAME freshness window (5s) rejects it, resets ``started_at``
    to registration time and drops every frame captured since the user actually
    started speaking -- the turn goes text-only while the screen was streaming
    the whole time. Frame freshness is enforced separately, at freeze time.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"

    now = time.monotonic()
    real_onset = now - 30.0  # 排在一个 provider final 后面，远超帧的 5s TTL
    runtime._asr_runtime._asr_turn_onset_at = real_onset

    frame_at = real_onset + 1.0
    assert runtime._stage_independent_visual_frame(
        "frame-from-the-real-onset",
        source="screen",
        request_id="screen-late",
        captured_at=frame_at,
    )

    token = VoiceTurnToken(ingress=runtime._capture_ingress_token(), turn_id=902)
    turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
    runtime._begin_core_multimodal_turn(turn_id, token)
    record = runtime._core_multimodal_turns[turn_id]

    # 起点是真实开口时刻，不是注册时刻。
    assert record.started_at == pytest.approx(real_onset, abs=0.01)
    # 那一刻以来的帧被采纳了，而不是整轮退化成纯文本。
    assert [f.image_b64 for f in record.sampled_frames()] == [
        "frame-from-the-real-onset"
    ]


@pytest.mark.unit
async def test_idle_frames_do_not_consume_the_prerecord_budget() -> None:
    """Frames from before the user spoke are not this turn's to keep.

    Screen sharing fills the eight-slot buffer while nobody is talking. The
    sampler deliberately preserves widely spaced endpoints, so those idle
    frames hold their slots; the few captured between speech confirmation and
    record creation then get sampled together with the whole idle history, and
    the onset filter at record creation discards all of them -- leaving only
    the newest frame and losing this turn's opening and middle views.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    now = time.monotonic()

    # 共享着但没人说话：闲置帧铺满缓冲。
    runtime._asr_runtime._asr_turn_onset_at = None
    for i in range(_MAX_PRERECORD_VISUAL_VALIDATIONS):
        assert runtime._stage_independent_visual_frame(
            f"idle-{i}",
            source="screen",
            request_id=f"screen-idle-{i}",
            captured_at=now - 60.0 + i * 5.0,
        )
    assert len(runtime._prerecord_visual_frames) == _MAX_PRERECORD_VISUAL_VALIDATIONS

    # 用户开口。确认到注册之间又拍了三张。
    onset = now - 2.0
    runtime._asr_runtime._asr_turn_onset_at = onset
    for i in range(3):
        assert runtime._stage_independent_visual_frame(
            f"speech-{i}",
            source="screen",
            request_id=f"screen-speech-{i}",
            captured_at=onset + 0.2 * (i + 1),
        )

    kept = [f.image_b64 for f in runtime._prerecord_visual_frames]
    # 开口之前的一张都不占名额了，这一轮自己的三张全在。
    assert kept == ["speech-0", "speech-1", "speech-2"]


@pytest.mark.unit
async def test_a_stale_onset_does_not_evict_the_prerecord_buffer() -> None:
    """Trimming is only safe against an onset this turn actually owns.

    A leftover value from an older turn would otherwise look like "speech began
    long ago" and evict every frame captured since -- the opposite of what the
    trim exists for. The buffer uses the same trust window as the record, so
    the two agree on where the turn began.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    now = time.monotonic()

    # 不可信的 onset。取**未来**时刻这一侧：那是真正危险的方向 —— 拿它去裁，
    # 每一帧都「早于开口」，整个缓冲会被清空。（过去那一侧的残值只会裁掉比它
    # 更早的帧，多数情况下是空操作，判别不出这条守卫。）
    runtime._asr_runtime._asr_turn_onset_at = now + 30.0

    for i in range(3):
        assert runtime._stage_independent_visual_frame(
            f"frame-{i}",
            source="screen",
            request_id=f"screen-{i}",
            captured_at=now - 1.0 + i * 0.2,
        )

    # onset 不可信 → 不裁，三张都留着（若采信，三张会被全部清掉）。
    assert [f.image_b64 for f in runtime._prerecord_visual_frames] == [
        "frame-0",
        "frame-1",
        "frame-2",
    ]


@pytest.mark.unit
async def test_replay_drops_the_pending_slot_when_the_transport_identity_moves_on() -> None:
    """A drifted runtime identity must not strand the pending confirmation.

    _send_asr_lifecycle_state() swallows delivery exceptions and returns
    _runtime_identity_matches(), so a false return means the runtime identity
    moved on -- and _restart_transport / _close_transport_only swap
    _asr_session and bump transport_generation without bumping the epoch or
    running _reset_asr_turn_state(). Holding the pending confirmation across
    that return strands it: the compensation already transitioned to ACTIVE,
    and both redemption sites gate on PREWARMING, so nothing ever collects it.
    The next unrelated utterance then adopts the stale onset as its visual
    ownership boundary, and the poisoned flag pins pending_before True so the
    overlap compensation silently stops firing.

    The real onset is already committed to _asr_turn_onset_at before the
    broadcast, so clearing the slot on confirmation loses nothing.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch
    component = runtime._asr_runtime

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    assert component._asr_overlap_onset_at is not None
    await runtime._handle_independent_asr_endpoint(epoch)

    # monotonic 在这台机器上一整个测试跑下来只走一格，靠时钟自然推进区分不了
    # 「陈旧 onset」和「新回合 onset」。按仓库既有做法直接注入一个明显靠前的
    # 时刻，后面那条继承断言才有分辨力。
    recorded_onset = time.monotonic() - 5.0
    component._asr_overlap_onset_at = recorded_onset

    component._asr_session.is_ready = False
    lifecycle_ref = runtime._asr_lifecycle

    # ACTIVE 广播飞在半空时来一次「仅关传输」：换掉 _asr_session、bump
    # transport_generation，epoch 与 lifecycle 对象都不动 —— 这正是
    # _close_transport_only 干的事，也是唯一能让 delivered 为假的那条腿。
    real_on_lifecycle = component._callbacks.on_lifecycle
    drifted = False

    async def _drift_transport_midflight(note: AsrLifecycleNotification) -> None:
        nonlocal drifted
        if note.state == VoiceLifecycleState.ACTIVE.value and not drifted:
            drifted = True
            component._asr_session = None
            lifecycle_ref.invalidate_transport()
        await real_on_lifecycle(note)

    component._callbacks = replace(
        component._callbacks,
        on_lifecycle=_drift_transport_midflight,
    )

    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert drifted is True
    # 走的确实是「传输身份漂移」这条腿，不是 detach / fail-closed 那条
    # （那两条会 bump epoch、换 lifecycle，并且自己会跑 _reset_asr_turn_state）。
    assert runtime._asr_session_epoch == epoch
    assert runtime._asr_lifecycle is lifecycle_ref

    # 挂起槽必须已经腾空 —— 没人会再来兑付它。
    assert component._asr_pending_speech_confirmed is False
    assert component._asr_pending_speech_onset_at is None
    # 而用户真实开口的时刻一点没丢：它在 await 之前就装进了 _asr_turn_onset_at。
    assert component._asr_turn_onset_at == recorded_onset

    # 行为层：走完这一轮，下一次**不相干**的开口不能继承那个陈旧时刻。
    component._asr_session = type("Asr", (), {"is_ready": True})()
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("second", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE

    fresh_floor = time.monotonic()
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    assert component._asr_turn_onset_at != recorded_onset
    assert component._asr_turn_onset_at >= fresh_floor


@pytest.mark.unit
async def test_credit_redemption_drops_the_pending_slot_when_the_transport_identity_moves_on() -> None:
    """Dual of the direct-replay case for the completed-overlap credit path.

    Both compensation blocks force-confirm the same way, so both strand the
    pending slot the same way when the runtime identity drifts across the
    ACTIVE broadcast. Covering only one leaves the other free to regress.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch
    component = runtime._asr_runtime

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    # 后继在上一轮还 ACTIVE 时开口又停顿：攒下一张 completed-overlap credit。
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()
    assert runtime._asr_overlap_completed_turns == 1
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE

    # 同上：注入一个明显靠前的时刻，后面那条继承断言才有分辨力。
    recorded_onset = time.monotonic() - 5.0
    component._asr_overlap_completed_onsets[0] = recorded_onset

    component._asr_session.is_ready = False
    lifecycle_ref = runtime._asr_lifecycle
    real_on_lifecycle = component._callbacks.on_lifecycle
    drifted = False

    async def _drift_transport_midflight(note: AsrLifecycleNotification) -> None:
        nonlocal drifted
        if note.state == VoiceLifecycleState.ACTIVE.value and not drifted:
            drifted = True
            component._asr_session = None
            lifecycle_ref.invalidate_transport()
        await real_on_lifecycle(note)

    component._callbacks = replace(
        component._callbacks,
        on_lifecycle=_drift_transport_midflight,
    )

    # 后继自己的 endpoint 兑付这张 credit，重放停在 PREWARMING 后就地补确认。
    await runtime._handle_independent_asr_endpoint(epoch)

    assert drifted is True
    assert runtime._asr_session_epoch == epoch
    assert runtime._asr_lifecycle is lifecycle_ref
    assert component._asr_pending_speech_confirmed is False
    assert component._asr_pending_speech_onset_at is None
    assert component._asr_turn_onset_at == recorded_onset
    # 确认已经落地（lifecycle 是 ACTIVE，这一轮会照常封口），所以那张 credit
    # 必须跟着确认一起记掉，不能被身份漂移那条 return 跳过。
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_overlap_completed_turns == 0
    assert not component._asr_overlap_completed_onsets

    # 身份漂移让这一轮停在 ACTIVE（那次 return 越过了随后的封口）。恢复身份、
    # 把它正常走完，才谈得上「下一次不相干的开口」。
    component._asr_session = type("Asr", (), {"is_ready": True})()
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("second", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE

    fresh_floor = time.monotonic()
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    assert component._asr_turn_onset_at != recorded_onset
    assert component._asr_turn_onset_at >= fresh_floor

    # 行为层：后面一次**真实**的 overlap 兑付必须拿到它自己的 onset。credit 若
    # 被漏记，这张陈旧的会按 FIFO 排在前面先被兑走，这一轮就拿错了开口时刻。
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("third", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()
    assert runtime._asr_overlap_completed_turns == 1
    later_onset = component._asr_overlap_completed_onsets[0]
    assert later_onset != recorded_onset

    await runtime._handle_independent_asr_endpoint(epoch)
    assert component._asr_turn_onset_at == later_onset
    assert runtime._asr_overlap_completed_turns == 0


@pytest.mark.unit
async def test_direct_overlap_replay_seals_when_the_session_is_not_ready() -> None:
    """The direct replay must complete its confirmation in place too.

    Dual of the completed-overlap credit path. Parking in PREWARMING and just
    holding the onset is not enough: this successor's provider endpoint and
    final are already queued in the ordered FIFO and about to arrive, a
    PREWARMING lifecycle cannot seal, and _handle_independent_asr_final()
    requires DRAINING -- so the whole utterance is discarded with no watchdog
    armed.

    Waiting for the reconnect cannot recover it either: a reconnect swaps in a
    new session and is_adopted_candidate() drops every callback still queued on
    the old one (_restart_transport / _close_transport_only both null
    _asr_session before closing it). Reaching this point proves the old session
    is still adopted, i.e. the reconnect has not started.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch
    component = runtime._asr_runtime

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    # 后继在上一轮还 ACTIVE 时开口：它的 onset 被记下来等直接重放。
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    recorded_onset = component._asr_overlap_onset_at
    assert recorded_onset is not None
    await runtime._handle_independent_asr_endpoint(epoch)

    # 两条有序回调之间传输掉线：重放会停在 PREWARMING 并挂起确认。
    component._asr_session.is_ready = False

    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    # 重放就地补完了确认：回合醒着，后继排在 FIFO 里的 endpoint 才封得了口。
    # （HEAD 上这里是 PREWARMING，封不了口，那条 final 会被整条丢弃。）
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    # 这一刻还没 prepare 是对的：直接重放只负责唤醒，prepare 由后继自己的
    # endpoint 完成（_handle_independent_asr_endpoint 的 not _asr_turn_prepared
    # 分支）。断言它已 prepare 属于对契约的过度主张。
    # 用的是用户当初真实开口的时刻，不是这次重放的时刻。
    assert component._asr_turn_onset_at == recorded_onset
    assert component._asr_pending_speech_onset_at is None
    # 没走 fail-closed 出口（那条会 bump epoch、拆掉 session）。
    assert runtime._asr_session_epoch == epoch

    # 后继自己的 endpoint 紧随其后到达 —— 这一步才封口。
    await runtime._handle_independent_asr_endpoint(epoch)
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    # 忙窗口有定时器兜底。
    assert component._asr_final_watchdog_task is not None

    await runtime._handle_independent_asr_final("second", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second"]
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE


@pytest.mark.unit
async def test_direct_overlap_replay_reclaims_its_lent_onset_when_it_never_wakes() -> None:
    """A direct replay that never reaches ACTIVE must take its onset back.

    Both overlap replay paths lend the recorded onset to the confirmation
    branch. The credit-redemption path reclaims it when the wake-up fails; the
    direct path (driven by the delayed provider final) did not, so the stale
    timestamp stayed in the pending slot and the NEXT, unrelated utterance
    adopted it as its visual ownership boundary -- pulling in frames that
    belong to nobody and rejecting the ones it is actually about.

    The carve-out is identical to the credit path: an onset held for a PENDING
    confirmation is deliberately kept, because clearing it would send that
    confirmation back to a fresh detected_at and drop every frame since the
    user actually started speaking. The dual below pins that half.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    # A successor spoke while the first turn was still ACTIVE and prepared:
    # its onset is remembered for the direct replay after the delayed final.
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    assert runtime._asr_overlap_onset_at is not None
    await runtime._handle_independent_asr_endpoint(epoch)

    # The replay cannot wake the turn, and leaves no pending confirmation
    # behind (Smart Turn lease unavailable / lifecycle broadcast undelivered).
    async def refuse_to_wake(*_args, **_kwargs):
        return None

    runtime._asr_runtime._handle_independent_asr_activity = refuse_to_wake

    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert runtime._asr_pending_speech_onset_at is None, (
        "the lent onset stayed behind and a later unrelated turn will adopt it"
    )


@pytest.mark.unit
async def test_direct_overlap_replay_keeps_the_onset_for_a_pending_confirmation() -> None:
    """Dual: an onset held for a pending confirmation must NOT be reclaimed.

    When the session is momentarily unavailable the replay parks in PREWARMING
    with the confirmation pending and deliberately holds the onset for it.
    Reclaiming it there sends that confirmation back to a fresh detected_at and
    every frame since the user started speaking is excluded.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)

    async def park_with_pending_confirmation(*_args, **_kwargs):
        runtime._asr_runtime._asr_pending_speech_confirmed = True

    runtime._asr_runtime._handle_independent_asr_activity = (
        park_with_pending_confirmation
    )

    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert runtime._asr_pending_speech_onset_at is not None


@pytest.mark.unit
async def test_overlap_credit_survives_a_replay_that_never_activates() -> None:
    """Spend the credit on a successful wake-up, not on the attempt.

    The replay can leave the lifecycle short of ACTIVE when the session is
    momentarily unavailable. Deducting the credit first strands that turn: its
    endpoint can no longer seal, the final queued right behind it is discarded,
    and the popped onset goes on to be inherited by an unrelated later turn.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    assert runtime._asr_overlap_completed_turns == 1
    onset_before = list(runtime._asr_overlap_completed_onsets)

    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE

    # 重放唤不醒这一轮（会话暂时不可用）。
    async def refuse_to_wake(*_args, **_kwargs):
        return None

    runtime._asr_runtime._handle_independent_asr_activity = refuse_to_wake
    await runtime._handle_independent_asr_endpoint(epoch)

    # credit 和 onset 都原样留着，等下一次兑付。
    assert runtime._asr_overlap_completed_turns == 1
    assert list(runtime._asr_overlap_completed_onsets) == onset_before
    # 借出去的 onset 也收回了，不会被后面不相干的回合继承。
    assert runtime._asr_pending_speech_onset_at is None


@pytest.mark.unit
async def test_an_unwoken_redemption_still_seals_and_delivers_the_queued_final() -> None:
    """An unwoken replay must still seal: its final is already on the way.

    This test REPLACES test_a_pending_confirmation_keeps_the_lent_onset and
    deliberately overturns its reasoning ("hold the onset for the confirmation
    that follows the reconnect"). The reconnect cannot recover this final:
    _restart_transport() nulls _asr_session before closing it, after which
    every provider callback is dropped by is_adopted_candidate(). Reaching this
    point proves the old session is still adopted -- the reconnect has not
    started and the final is right behind this endpoint in the ordered FIFO.
    Completing the confirmation in place, so that final finds a DRAINING turn,
    is the only way not to lose the utterance.

    Measured on HEAD: state=PREWARMING, credit still 1, sealed_token=None, both
    the warm-expiry and provider-final timers None, transcripts only ["first"]
    -- the whole sentence lost AND a busy flag left with no timer behind it.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch

    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_RESUMED,
        epoch,
    )
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.CANDIDATE_PAUSE,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    component = runtime._asr_runtime
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert component._asr_overlap_completed_turns == 1
    recorded_onset = component._asr_overlap_completed_onsets[0]

    # 不打桩：跑真实控制流，只让传输在两条有序回调之间掉线。
    component._asr_session.is_ready = False

    await runtime._handle_independent_asr_endpoint(epoch)

    # 仍然封口，那条排在后面的 final 才有 DRAINING 可落。
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    # 恰好兑付一次，不多不少。
    assert component._asr_overlap_completed_turns == 0
    assert list(component._asr_overlap_completed_onsets) == []
    assert component._asr_overlap_completed_token is None
    # onset 被本轮消费掉，不会被后面某个不相干的回合当成自己的起点。
    assert component._asr_pending_speech_onset_at is None
    # 用的是用户当初真实开口的时刻，不是这次重放的时刻。
    assert component._asr_turn_onset_at == recorded_onset
    # 忙窗口有定时器兜底（HEAD 上这里是 None）。
    assert component._asr_final_watchdog_task is not None
    # 没走 fail-closed 出口：那条会 bump epoch、拆掉 session、把语音判死。
    # 只有这组断言能区分两条出口——错误出口也会发同名 status。
    assert runtime._asr_session_epoch == epoch
    assert runtime._asr_lifecycle is not None

    await runtime._handle_independent_asr_final("second", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    assert [
        call.args[0] for call in runtime.handle_input_transcript.await_args_list
    ] == ["first", "second"]
    # 收尾不留忙标志。
    assert runtime._asr_lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE


@pytest.mark.unit
async def test_an_unwoken_redemption_never_parks_in_an_untimed_busy_state() -> None:
    """Invariant: a busy state must always carry a timer, whatever the fix is.

    Asserts the absence of the combination "busy state AND both timers None"
    rather than any particular implementation, so it survives a different
    compensation strategy later. HEAD lands squarely in that forbidden
    combination.
    """
    runtime = _Runtime()
    _install_ready_lifecycle(runtime, "openai")
    epoch = runtime._asr_session_epoch
    component = runtime._asr_runtime

    for event in (
        SpeechActivityEvent.SPEECH_STARTED,
        SpeechActivityEvent.SPEECH_RESUMED,
        SpeechActivityEvent.CANDIDATE_PAUSE,
    ):
        await runtime._handle_independent_asr_activity(event, epoch)
    await runtime._handle_independent_asr_endpoint(epoch)
    await runtime._handle_independent_asr_final("first", epoch, "openai")
    await runtime._wait_asr_transcript_dispatch_idle()

    component._asr_session.is_ready = False
    await runtime._handle_independent_asr_endpoint(epoch)

    state = (
        runtime._asr_lifecycle.snapshot.state
        if runtime._asr_lifecycle is not None
        else None
    )
    busy = {
        VoiceLifecycleState.PREWARMING,
        VoiceLifecycleState.ACTIVE,
        VoiceLifecycleState.DRAINING,
    }
    assert not (
        state in busy
        and component._asr_warm_expiry_task is None
        and component._asr_final_watchdog_task is None
    ), "忙标志停在了没有任何定时器兜底的状态上"


@pytest.mark.unit
async def test_overlap_prerecord_trims_against_the_pending_turn_onset() -> None:
    """During an overlap the successor's boundary lives in the pending slot.

    Speech that starts while the previous turn is still DRAINING records its
    onset in ``_asr_pending_turn_onset_at``; it is only copied into
    ``_asr_turn_onset_at`` once the previous provider final activates that turn.
    Reading only the latter means the whole overlap window is judged against the
    PRECEDING turn's onset, so frames from after its endpoint still count as
    "this turn's" and fill the bounded buffer, evicting the successor's real
    opening and middle views.
    """
    runtime = _Runtime()
    runtime._asr_route_mode = "independent"
    now = time.monotonic()

    # 前一轮的 onset 很早；后继在它还没收场时开口，边界记在 pending 槽。
    runtime._asr_runtime._asr_turn_onset_at = now - 40.0
    runtime._asr_runtime._asr_pending_turn_onset_at = now - 2.0

    # 前一轮封口之后、后继开口之前的帧。
    for i in range(3):
        assert runtime._stage_independent_visual_frame(
            f"between-{i}",
            source="screen",
            request_id=f"screen-between-{i}",
            captured_at=now - 30.0 + i,
        )
    # 后继自己的帧。
    for i in range(2):
        assert runtime._stage_independent_visual_frame(
            f"successor-{i}",
            source="screen",
            request_id=f"screen-successor-{i}",
            captured_at=now - 1.5 + i * 0.3,
        )

    # 只有后继自己的帧留下，中间那些没占名额。
    assert [f.image_b64 for f in runtime._prerecord_visual_frames] == [
        "successor-0",
        "successor-1",
    ]
async def test_lease_resync_does_not_hand_a_successor_the_replaced_episode() -> None:
    """A takeover inside the display send must not reach the new recorder.

    The display push is an await, so the voice-owner lookup that follows it
    can resolve the SUCCESSOR's socket. Withholding the ledger commit
    afterwards is not enough -- a delivered status cannot be retracted.
    """

    runtime = _Runtime()
    assert runtime._begin_voice_input_connection("chat-window") is True

    send_started = asyncio.Event()
    release_send = asyncio.Event()
    owner_payloads: list[dict] = []

    async def stalling_send_status(_message: str) -> bool:
        send_started.set()
        await release_send.wait()
        return True

    async def record_owner_send(payload: dict):
        owner_payloads.append(payload)
        return successor_socket

    successor_socket = object()
    runtime.send_status = AsyncMock(side_effect=stalling_send_status)
    runtime._voice_owner_socket = lambda: successor_socket
    runtime._send_to_voice_owner = record_owner_send

    signal = asyncio.create_task(runtime._maybe_signal_voice_lease_resync())
    await asyncio.wait_for(send_started.wait(), timeout=1)

    # A different window claims the microphone while the display push is stuck.
    assert runtime._begin_voice_input_connection("recorder-window") is True
    release_send.set()
    await asyncio.wait_for(signal, timeout=1)

    assert owner_payloads == []
    assert runtime._voice_lease_resync_signal_state is None
