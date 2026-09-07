"""Real Registry -> Core -> Runtime -> Detector -> composition installation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.main_server.voice_identity_runtime import OwnerVoiceRuntimeRegistry
from main_logic.asr_client import VoiceIdentityActivationResult as PublicResult
from main_logic.asr_client.endpointing.detector_runtime import DetectorRuntime
from main_logic.asr_client.provider_policy import resolve_provider_policy
from main_logic.asr_client.speaker_verifier_contracts import SpeakerVerifierInstallOutcome as Outcome
from main_logic.asr_client.speaker_verifier_contracts import (
    SpeakerVerifierAuthority,
    SpeakerVerifierHealthEvent,
    SpeakerVerifierSpec,
)
from main_logic.voice_identity_service.asr_composition import OwnerVoiceAsrCompositionFactory
from tests.unit.test_core_independent_asr import _Runtime, _selection
from tests.unit.voice_identity_service.test_asr_composition import _profile
import main_logic.core as core_module
import main_logic.asr_client.runtime as runtime_module


def _supported_route(manager):
    policy = resolve_provider_policy("qwen", endpointing_mode="provider")
    manager._asr_runtime._asr_lifecycle = SimpleNamespace(provider_policy=policy)
    manager._asr_runtime._asr_detector = DetectorRuntime(provider_policy=policy)
    manager._set_microphone_route("independent")


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_idle_registry_bind_and_real_supported_unsupported_round_trip(iteration):
    manager = _Runtime()
    manager.input_mode = "audio"
    manager.is_active = False
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    profile = _profile()
    try:
        assert await registry.activate(profile, "A") is PublicResult.ACTIVATION_PENDING
        assert await registry.register_manager(manager) is PublicResult.ACTIVATION_PENDING
        assert manager._asr_runtime._asr_detector is None
        assert manager._asr_runtime._speaker_verifier_factory is None
        assert registry._attach_retry_task is None
        _supported_route(manager)
        first = await manager.reconcile_speaker_verifier()
        assert first.outcome is Outcome.INSTALLED
        assert registry.activation_status() is PublicResult.READY
        runtime = manager._asr_runtime
        assert runtime.speaker_verifier_installation_permits_evidence(first.identity)
        old_shadow = runtime._asr_detector._speaker_shadow
        manager._set_microphone_route("native")
        assert not runtime.speaker_verifier_installation_permits_evidence(first.identity)
        assert registry.activation_status() is PublicResult.UNSUPPORTED_ASR_ROUTE
        assert await registry.activate(profile, "B") is PublicResult.UNSUPPORTED_ASR_ROUTE
        desired = manager._speaker_verifier_spec
        assert desired is not None and desired.activation_revision != first.identity.activation_revision
        manager._set_microphone_route("independent")
        second = await manager.reconcile_speaker_verifier()
        assert second.outcome is Outcome.INSTALLED
        assert second.identity.installation_id != first.identity.installation_id
        assert second.identity.activation_revision == desired.activation_revision
        assert runtime._asr_detector._speaker_shadow is not old_shadow
        assert old_shadow._closed
        assert registry.activation_status() is PublicResult.READY
        await registry.activate(None, "disabled")
        assert manager._speaker_verifier_spec is None
        assert runtime._asr_detector._speaker_shadow is None
        manager._set_microphone_route("native")
        manager._set_microphone_route("independent")
        assert (await manager.reconcile_speaker_verifier()).outcome is Outcome.REVOKED
        assert runtime._asr_detector._speaker_shadow is None
    finally:
        await registry.close()
        profile.close()
        manager._asr_runtime._asr_lifecycle = None
        await manager._asr_runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("register_first", [True, False])
@pytest.mark.parametrize("iteration", range(25))
async def test_real_core_start_reconciles_both_registration_orders_and_restart(
    monkeypatch, register_first, iteration,
):
    manager = _Runtime()
    manager.core_api_type = "qwen"
    manager.input_mode = "audio"
    manager.is_active = False
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    profile = _profile()
    monkeypatch.setattr(core_module, "aload_global_conversation_settings", AsyncMock(
        return_value={"independentAsrEnabled": True, "voiceInputResourceOptimizationEnabled": False},
    ))
    monkeypatch.setattr(runtime_module, "_resolve_asr_selection", lambda *_: _selection("qwen", "provider"))
    monkeypatch.setattr(runtime_module, "_create_asr_session_from_selection", lambda *a, **kw:
        SimpleNamespace(is_ready=True, connect=AsyncMock(), close=AsyncMock()))
    try:
        await registry.activate(profile, "A")
        if register_first:
            assert await registry.register_manager(manager) is PublicResult.ACTIVATION_PENDING
        await manager._start_independent_asr_if_enabled("audio")
        if not register_first:
            assert await registry.register_manager(manager) is PublicResult.READY
        assert registry.activation_status() is PublicResult.READY
        first = manager._asr_runtime._speaker_verifier_install_receipt
        assert first.outcome is Outcome.INSTALLED
        await manager._close_independent_asr(next_route_mode="blocked")
        assert registry.activation_status() is PublicResult.ACTIVATION_PENDING
        await manager._start_independent_asr_if_enabled("audio")
        second = manager._asr_runtime._speaker_verifier_install_receipt
        assert second.outcome is Outcome.INSTALLED
        assert second.identity.installation_id != first.identity.installation_id
        assert second.identity.activation_revision == first.identity.activation_revision
        assert registry.activation_status() is PublicResult.READY
    finally:
        await registry.close()
        profile.close()
        await manager._asr_runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_committed_installation_survives_normal_sentence_detector_resets(iteration):
    manager = _Runtime()
    manager.input_mode = "audio"
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    profile = _profile()
    try:
        _supported_route(manager)
        await registry.register_manager(manager)
        assert await registry.activate(profile, "A") is PublicResult.READY
        runtime = manager._asr_runtime
        detector = runtime._asr_detector
        receipt = runtime._speaker_verifier_install_receipt
        identity = receipt.identity
        shadow = detector._speaker_shadow
        initial_epoch = detector._detector_epoch
        for sentence in (1, 2):
            await detector.reset()
            assert detector._detector_epoch == initial_epoch + sentence
            assert detector._speaker_shadow is shadow
            assert detector._speaker_owner_generation == identity.installation_id
            assert runtime.speaker_verifier_installation_permits_evidence(identity)
            runtime._accept_speaker_verifier_health(SpeakerVerifierHealthEvent(
                identity, sentence * 2, frozenset({"backend_unavailable"}),
            ))
            assert runtime._speaker_verifier_degraded
            assert registry.activation_status() is PublicResult.RUNTIME_DEGRADED
            runtime._accept_speaker_verifier_health(SpeakerVerifierHealthEvent(
                identity, sentence * 2 + 1,
            ))
            assert not runtime._speaker_verifier_degraded
            assert registry.activation_status() is PublicResult.READY
    finally:
        await registry.close()
        profile.close()
        manager._asr_runtime._asr_lifecycle = None
        await manager._asr_runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_detector_reset_during_pending_installation_is_stale(iteration):
    manager = _Runtime()
    manager.input_mode = "audio"
    _supported_route(manager)
    runtime = manager._asr_runtime
    profile = _profile()
    created = asyncio.Event()
    factories = []
    authority = SpeakerVerifierAuthority()
    authority.commit()

    def build(target, identity):
        factory = OwnerVoiceAsrCompositionFactory(
            target, profile, activation_generation="A", enforce=True,
            authority=authority, installation_identity=identity,
        )
        factories.append(factory)
        created.set()
        return factory

    spec = SpeakerVerifierSpec("profile", "A", True, True, authority, build)
    await runtime._asr_admission._lock.acquire()
    task = asyncio.create_task(manager.set_speaker_verifier_spec(spec))
    try:
        await created.wait()
        await runtime._asr_detector.reset()
        runtime._asr_admission._lock.release()
        receipt = await task
        assert receipt.outcome is Outcome.STALE
        assert runtime._asr_detector._speaker_shadow is None
        assert not runtime.speaker_verifier_installation_permits_evidence(receipt.identity)
        assert factories[0]._closed
    finally:
        if runtime._asr_admission._lock.locked():
            runtime._asr_admission._lock.release()
        await asyncio.gather(task, return_exceptions=True)
        profile.close()
        runtime._asr_lifecycle = None
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace", "close"])
@pytest.mark.parametrize("iteration", range(50))
async def test_direct_detector_mount_retirement_cannot_leave_core_ready(operation, iteration):
    manager = _Runtime()
    manager.input_mode = "audio"
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    profile = _profile()
    try:
        _supported_route(manager)
        await registry.register_manager(manager)
        assert await registry.activate(profile, "A") is PublicResult.READY
        runtime = manager._asr_runtime
        receipt = runtime._speaker_verifier_install_receipt
        identity = receipt.identity
        detector = runtime._asr_detector
        if operation == "replace":
            await detector.replace_speaker_verifier(None, owner_generation="retired")
        else:
            await detector.close()
        # No Core route transition ran, and Runtime's historical receipt is
        # unchanged. READY still requires proof of the actual current mount.
        assert runtime._speaker_verifier_install_receipt is receipt
        assert not runtime.speaker_verifier_installation_permits_evidence(identity)
        assert manager.speaker_verifier_installation_status(identity.activation_revision).outcome is Outcome.STALE
        assert registry.activation_status() is not PublicResult.READY
    finally:
        await registry.close()
        profile.close()
        manager._asr_runtime._asr_lifecycle = None
        await manager._asr_runtime.close()
