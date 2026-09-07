"""Deterministic permission/persistence regressions for installation lifecycle."""

import asyncio
from types import SimpleNamespace

import pytest

from app.main_server.voice_identity_runtime import OwnerVoiceRuntimeRegistry
from main_logic.asr_client import VoiceIdentityActivationResult as Result
from main_logic.asr_client.speaker_verifier_contracts import (
    SpeakerVerifierAuthorityState as AuthorityState,
    SpeakerVerifierInstallIdentity,
    SpeakerVerifierInstallOutcome as Outcome,
    SpeakerVerifierInstallReceipt,
    SpeakerVerifierOwnershipState,
)
from main_logic.voice_identity_service.service import VoiceIdentityServiceError

from .test_voice_identity_runtime import _profile
from .voice_identity_service.test_service import _service, _pcm


class _TypedManager:
    def __init__(self, *, active=True):
        self.speaker_verifier_participating = active
        self.supported = True
        self.spec = None
        self.calls = []
        self.fail = False
        self.barrier = None
        self.entered = asyncio.Event()
        self._asr_runtime = SimpleNamespace()
        self.receipt = SpeakerVerifierInstallReceipt(None, Outcome.REVOKED)

    async def set_speaker_verifier_spec(self, spec):
        self.spec = spec
        self.calls.append(spec)
        self.entered.set()
        if self.barrier is not None:
            await self.barrier.wait()
        if spec is None:
            self.receipt = SpeakerVerifierInstallReceipt(None, Outcome.REVOKED)
        elif self.fail:
            self.receipt = SpeakerVerifierInstallReceipt(None, Outcome.FAILED)
        elif not self.speaker_verifier_participating:
            self.receipt = SpeakerVerifierInstallReceipt(None, Outcome.DEFERRED_ROUTE)
        elif not self.supported:
            self.receipt = SpeakerVerifierInstallReceipt(None, Outcome.UNSUPPORTED_ROUTE)
        else:
            identity = SpeakerVerifierInstallIdentity(
                id(self), id(self._asr_runtime), 1, 1, 1, 1,
                spec.activation_revision, str(len(self.calls)),
            )
            self.receipt = SpeakerVerifierInstallReceipt(
                identity, Outcome.INSTALLED, SpeakerVerifierOwnershipState.DETECTOR,
            )
        return self.receipt

    def speaker_verifier_installation_status(self, activation_revision):
        if self.spec is None or self.spec.activation_revision != activation_revision:
            return SpeakerVerifierInstallReceipt(None, Outcome.STALE)
        return self.receipt

    async def set_voice_input_suppressed(self, reason, *, suppressed):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_prepare_never_grants_authority_and_stale_commit_cannot_resurrect(iteration):
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _TypedManager()
    await registry.register_manager(manager)
    profile = _profile("same-profile")
    try:
        prepared = await registry.prepare_activation(profile, profile.generation)
        staged_spec = manager.spec
        assert staged_spec.revocable_authority.state is AuthorityState.STAGED
        assert not staged_spec.revocable_authority.permits_evidence
        assert registry.commit_activation(prepared) is Result.READY
        assert staged_spec.revocable_authority.permits_evidence
        next_prepared = await registry.prepare_activation(profile, profile.generation)
        assert manager.spec.activation_revision != staged_spec.activation_revision
        assert manager.receipt.identity.installation_id != "1"
        await registry.activate(None, "disable")
        assert registry.commit_activation(next_prepared) is Result.RUNTIME_DEGRADED
        assert staged_spec.revocable_authority.state is AuthorityState.REVOKED
        assert manager.spec is None
    finally:
        profile.close()
        await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_failed_b_revoked_before_unsupported_recovery_and_resumes_a(iteration):
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    first, second = _TypedManager(), _TypedManager()
    await registry.register_manager(first)
    await registry.register_manager(second)
    # Stable order, independent from weak-set address ordering.
    first, second = tuple(registry._managers)
    profile_a, profile_b = _profile("a"), _profile("b")
    try:
        assert await registry.activate(profile_a, "a") is Result.READY
        old_revision = first.spec.activation_revision
        second.barrier = asyncio.Event()
        second.entered.clear()
        second.fail = True
        preparing = asyncio.create_task(registry.prepare_activation(profile_b, "b"))
        await second.entered.wait()
        failed_authority = first.spec.revocable_authority
        first.supported = False
        second.barrier.set()
        prepared = await preparing
        assert prepared.result is Result.RUNTIME_DEGRADED
        assert failed_authority.state is AuthorityState.REVOKED
        second.fail = False
        second.barrier = None
        # Drive exactly one recovery pass, no randomized timing.
        await registry.register_manager(first)
        await registry.register_manager(second)
        assert first.spec.profile_generation == "a"
        assert first.spec.activation_revision != old_revision
        assert first.receipt.outcome is Outcome.UNSUPPORTED_ROUTE
        assert registry.activation_status() is Result.UNSUPPORTED_ASR_ROUTE
        first.supported = True
        await first.set_speaker_verifier_spec(first.spec)
        assert registry.activation_status() is Result.READY
        assert failed_authority.state is AuthorityState.REVOKED
    finally:
        profile_a.close()
        profile_b.close()
        await registry.close()


@pytest.mark.asyncio
async def test_idle_registration_preserves_spec_without_building_and_ignores_text_peer():
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    active, idle = _TypedManager(), _TypedManager(active=False)
    profile = _profile("owner")
    try:
        await registry.register_manager(active)
        await registry.activate(profile, "owner")
        assert await registry.register_manager(idle) is Result.ACTIVATION_PENDING
        assert idle.spec.profile_generation == "owner"
        assert idle.receipt.outcome is Outcome.DEFERRED_ROUTE
        assert registry.activation_status() is Result.READY
        assert registry._attach_retry_task is None
        idle.speaker_verifier_participating = True
        await idle.set_speaker_verifier_spec(idle.spec)
        assert registry.activation_status() is Result.READY
    finally:
        profile.close()
        await registry.close()


@pytest.mark.asyncio
async def test_manager_registered_during_persistence_gets_staged_target():
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    first, late = _TypedManager(), _TypedManager()
    profile_a, profile_b = _profile("a"), _profile("b")
    try:
        await registry.register_manager(first)
        await registry.activate(profile_a, "a")
        prepared = await registry.prepare_activation(profile_b, "b")
        await registry.register_manager(late)
        assert late.spec.profile_generation == "b"
        assert not late.spec.revocable_authority.permits_evidence
        assert registry.commit_activation(prepared) is Result.READY
        assert late.spec.revocable_authority.permits_evidence
    finally:
        profile_a.close()
        profile_b.close()
        await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_failure", [True, False])
@pytest.mark.parametrize("iteration", range(50))
async def test_enrollment_publishes_authority_only_after_disk_and_retires_before_abort(
    tmp_path, monkeypatch, commit_failure, iteration,
):
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _TypedManager()
    await registry.register_manager(manager)
    service, _, _, _ = _service(tmp_path)
    service._activation_callback = registry.activate
    service._activation_transaction = registry
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "a", _pcm())
    staged_authorities = []
    abort_observations = []
    original_stage = service._profile_store.astage

    async def stage(profile, *, audio_contract):
        staged = await original_stage(profile, audio_contract=audio_contract)
        original_commit, original_abort = staged.acommit, staged.aabort

        async def commit():
            authority = manager.spec.revocable_authority
            staged_authorities.append(authority)
            assert authority.state is AuthorityState.STAGED
            if commit_failure:
                raise RuntimeError("injected commit failure")
            await original_commit()
            assert authority.state is AuthorityState.STAGED

        async def abort():
            abort_observations.append(staged_authorities[-1].state)
            assert staged_authorities[-1].state is AuthorityState.REVOKED
            await original_abort()

        monkeypatch.setattr(staged, "acommit", commit)
        monkeypatch.setattr(staged, "aabort", abort)
        return staged

    monkeypatch.setattr(service._profile_store, "astage", stage)
    second = await service.start_enrollment()
    try:
        if commit_failure:
            with pytest.raises(VoiceIdentityServiceError):
                await service.complete_enrollment(second.enrollment_id, "b", _pcm())
            assert service.status().profile_generation == "a"
            assert staged_authorities[-1].state is AuthorityState.REVOKED
            assert abort_observations == [AuthorityState.REVOKED]
        else:
            await service.complete_enrollment(second.enrollment_id, "b", _pcm())
            assert service.status().profile_generation == "b"
            assert staged_authorities[-1].state is AuthorityState.COMMITTED
        stored = await service._profile_store.aload()
        assert stored.profile.generation == ("a" if commit_failure else "b")
        stored.close()
    finally:
        await service.close()
        await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_cancel_after_durable_profile_commit_commits_authority_without_rollback(
    tmp_path, monkeypatch, iteration,
):
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _TypedManager()
    await registry.register_manager(manager)
    service, _, _, _ = _service(tmp_path)
    service._activation_callback = registry.activate
    service._activation_transaction = registry
    await service.initialize()
    first = await service.start_enrollment()
    await service.complete_enrollment(first.enrollment_id, "a", _pcm())
    durable = asyncio.Event()
    release = asyncio.Event()
    original_stage = service._profile_store.astage

    async def stage(profile, *, audio_contract):
        staged = await original_stage(profile, audio_contract=audio_contract)
        original_commit = staged.acommit

        async def commit():
            await original_commit()
            durable.set()
            await release.wait()

        monkeypatch.setattr(staged, "acommit", commit)
        return staged

    monkeypatch.setattr(service._profile_store, "astage", stage)
    second = await service.start_enrollment()
    completion = asyncio.create_task(
        service.complete_enrollment(second.enrollment_id, "b", _pcm()),
    )
    try:
        await durable.wait()
        assert manager.spec.revocable_authority.state is AuthorityState.STAGED
        completion.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await completion
        assert service.status().profile_generation == "b"
        assert manager.spec.revocable_authority.state is AuthorityState.COMMITTED
        assert registry._activation.generation == "b"
        stored = await service._profile_store.aload()
        assert stored.profile.generation == "b"
        stored.close()
    finally:
        release.set()
        await service.close()
        await registry.close()


@pytest.mark.asyncio
async def test_mutation_early_authority_commit_is_detected(monkeypatch):
    original_prepare = OwnerVoiceRuntimeRegistry.prepare_activation

    async def early_commit(self, profile, generation):
        prepared = await original_prepare(self, profile, generation)
        if prepared.candidate is not None:
            prepared.candidate.authority.commit()
        return prepared

    monkeypatch.setattr(OwnerVoiceRuntimeRegistry, "prepare_activation", early_commit)
    with pytest.raises(AssertionError):
        await test_prepare_never_grants_authority_and_stale_commit_cannot_resurrect(0)


@pytest.mark.asyncio
async def test_mutation_late_failed_authority_revocation_is_detected(tmp_path, monkeypatch):
    async def delayed_abort(self, prepared):
        return Result.RUNTIME_DEGRADED

    monkeypatch.setattr(
        OwnerVoiceRuntimeRegistry, "revoke_prepared_activation", lambda self, prepared: None,
    )
    monkeypatch.setattr(OwnerVoiceRuntimeRegistry, "abort_activation", delayed_abort)
    with pytest.raises(AssertionError):
        await test_enrollment_publishes_authority_only_after_disk_and_retires_before_abort(
            tmp_path, monkeypatch, True, 0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_revoke_synchronously_retires_only_captured_installation(iteration):
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    matching, successor = _TypedManager(), _TypedManager()
    profile = _profile("b")
    retired = []
    try:
        await registry.register_manager(matching)
        await registry.register_manager(successor)
        prepared = await registry.prepare_activation(profile, "b")
        for manager in (matching, successor):
            manager._asr_runtime._speaker_installation_identity = manager.receipt.identity
            manager._asr_runtime.retire_speaker_verifier_authority = (
                lambda manager=manager: retired.append(manager)
            )
        # C has taken ownership of a pending installation on this manager.
        successor._asr_runtime._speaker_installation_pending = SimpleNamespace(
            activation_revision="successor-c",
        )
        registry.revoke_prepared_activation(prepared)
        assert retired == [matching]
        assert prepared.candidate.authority.state is AuthorityState.REVOKED
        assert registry.installation_diagnostics_snapshot() == {"install_requested": 1}
    finally:
        profile.close()
        await registry.close()
