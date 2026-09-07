from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.main_server.voice_identity_runtime as runtime_module
from app.main_server.voice_identity_runtime import OwnerVoiceRuntimeRegistry
from main_logic.asr_client import VoiceIdentityActivationResult
from main_logic.voice_identity.contracts import SpeakerModelIdentity
from main_logic.voice_identity.profile import SpeakerProfile
from main_logic.voice_identity.reference import SpeakerReference
from main_logic.voice_identity_service.audio_contract import (
    desktop_audio_contract_snapshot,
)


@dataclass
class _Factory:
    runtime: object
    profile: SpeakerProfile
    activation_generation: str
    enforce: bool
    closed: bool = False

    def __init__(
        self,
        runtime: object,
        profile: SpeakerProfile,
        *,
        activation_generation: str,
        enforce: bool,
    ) -> None:
        self.runtime = runtime
        self.profile = profile
        self.activation_generation = activation_generation
        self.enforce = enforce
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Manager:
    def __init__(self) -> None:
        self._asr_runtime = object()
        self.verifier_calls: list[tuple[_Factory | None, str]] = []
        self.verifier_outcomes: list[
            bool | VoiceIdentityActivationResult | BaseException
        ] = []
        self.suppression_calls: list[tuple[str, bool]] = []
        self.restore_failures = 0
        self.cancel_restore = False
        self.cancel_suppress = False
        self.suppress_failure = False

    async def set_speaker_verifier_factory(
        self,
        factory: _Factory | None,
        *,
        activation_generation: str,
    ) -> bool | VoiceIdentityActivationResult:
        self.verifier_calls.append((factory, activation_generation))
        outcome = self.verifier_outcomes.pop(0) if self.verifier_outcomes else True
        if isinstance(outcome, BaseException):
            if factory is not None:
                factory.close()
            raise outcome
        if not outcome and factory is not None:
            factory.close()
        return outcome

    async def set_voice_input_suppressed(
        self,
        reason: str,
        *,
        suppressed: bool,
    ) -> None:
        self.suppression_calls.append((reason, suppressed))
        if suppressed and self.cancel_suppress:
            raise asyncio.CancelledError
        if suppressed and self.suppress_failure:
            raise RuntimeError("suppression failed")
        if not suppressed and self.cancel_restore:
            raise asyncio.CancelledError
        if not suppressed and self.restore_failures:
            self.restore_failures -= 1
            raise RuntimeError("transient restore failure")


def _profile(generation: str) -> SpeakerProfile:
    reference = SpeakerReference(
        SpeakerModelIdentity("model", "revision", 2),
        [1.0, 0.0],
    )
    try:
        return SpeakerProfile(generation, reference)
    finally:
        reference.close()


async def _wait_until(predicate, *, timeout_seconds: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not satisfied before timeout")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _fake_composition_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_module,
        "OwnerVoiceAsrCompositionFactory",
        _Factory,
    )
    monkeypatch.setattr(runtime_module, "_runtime_registry", None)
    monkeypatch.setattr(runtime_module, "_service", None)


def test_missing_registry_diagnostics_preserve_fixed_schema() -> None:
    diagnostics = runtime_module.get_voice_identity_diagnostics()

    assert diagnostics == {
        **{
            name: 0
            for name in runtime_module._VOICE_IDENTITY_DIAGNOSTIC_COUNTERS
        },
        "registered_manager_count": 0,
        "diagnostic_runtime_count": 0,
    }


def test_voice_identity_diagnostics_include_admission_and_completion_counters() -> None:
    expected = {
        "admission_terminal_forward_count",
        "admission_terminal_drop_count",
        "admission_terminal_abandon_count",
        "admission_deadline_forward_count",
        "admission_rejection_applied_active_count",
        "admission_rejection_applied_sealed_count",
        "admission_core_settlement_degraded_count",
        "admission_transport_settlement_degraded_count",
        "admission_lifecycle_settlement_degraded_count",
        "admission_boundary_proof_retired_count",
        "admission_boundary_proof_overflow_count",
        "admission_late_operation_ignored_count",
        "speaker_completion_count",
        "speaker_completion_before_first_checkpoint_count",
        "speaker_completion_after_first_checkpoint_count",
        "speaker_completion_stale_count",
    }

    assert expected <= runtime_module._VOICE_IDENTITY_DIAGNOSTIC_COUNTERS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_activation_updates_current_and_future_managers() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    current = _Manager()
    future = _Manager()
    await registry.register_manager(current)
    borrowed = _profile("profile-a")
    try:
        assert await registry.activate(borrowed, "generation-a")
    finally:
        borrowed.close()

    assert current.verifier_calls[-1][1] == "generation-a"
    current_factory = current.verifier_calls[-1][0]
    assert current_factory is not None
    assert current_factory.enforce

    assert await registry.register_manager(future)
    assert future.verifier_calls[-1][1] == "generation-a"
    assert future.verifier_calls[-1][0] is not current_factory


@pytest.mark.unit
@pytest.mark.asyncio
async def test_activation_preserves_unsupported_route_result() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _Manager()
    manager.verifier_outcomes.append(
        VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    )
    await registry.register_manager(manager)
    profile = _profile("profile")
    try:
        result = await registry.activate(profile, "generation")
    finally:
        profile.close()

    assert result is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    assert registry._activation is not None  # type: ignore[attr-defined]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_late_registration_preserves_unsupported_route_result() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    manager = _Manager()
    manager.verifier_outcomes.append(
        VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    )

    result = await registry.register_manager(manager)

    assert result is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_activation_status_tracks_live_route_and_runtime_degradation() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _Manager()
    manager._asr_route_mode = "independent"  # type: ignore[attr-defined]
    manager._asr_runtime = SimpleNamespace(_speaker_verifier_degraded=False)
    await registry.register_manager(manager)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()

    assert registry.activation_status() is VoiceIdentityActivationResult.READY
    registry._restore_pending.add(manager)  # type: ignore[attr-defined]
    assert (
        registry.activation_status()
        is VoiceIdentityActivationResult.RUNTIME_DEGRADED
    )
    registry._restore_pending.discard(manager)  # type: ignore[attr-defined]
    manager._asr_route_mode = "native"  # type: ignore[attr-defined]
    assert (
        registry.activation_status()
        is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    )
    manager._asr_route_mode = "independent"  # type: ignore[attr-defined]
    manager._asr_runtime._speaker_verifier_degraded = True
    assert (
        registry.activation_status()
        is VoiceIdentityActivationResult.RUNTIME_DEGRADED
    )
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_diagnostics_snapshot_aggregates_only_safe_counters_once_per_runtime() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    runtime = SimpleNamespace(
        _speaker_verifier_diagnostics=lambda: {
            "observation_count": 4,
            "low_checkpoint_count": 2,
            "rejection_task_applied_count": 1,
            "admission_terminal_forward_count": 2,
            "admission_terminal_drop_count": 1,
            "admission_deadline_forward_count": 1,
            "admission_rejection_applied_sealed_count": 1,
            "admission_core_settlement_degraded_count": 0,
            "admission_late_operation_ignored_count": 1,
            "provider_candidate_bind_missing_identity_count": 3,
            "rejection_seal_snapshot_unbound_count": 2,
            "similarity": 0.12,
            "unexpected": 99,
        }
    )
    first = _Manager()
    first._asr_runtime = runtime
    duplicate = _Manager()
    duplicate._asr_runtime = runtime
    await registry.register_manager(first)
    await registry.register_manager(duplicate)

    diagnostics = registry.diagnostics_snapshot()

    assert diagnostics["registered_manager_count"] == 2
    assert diagnostics["diagnostic_runtime_count"] == 1
    assert diagnostics["observation_count"] == 4
    assert diagnostics["low_checkpoint_count"] == 2
    assert diagnostics["rejection_task_applied_count"] == 1
    assert diagnostics["admission_terminal_forward_count"] == 2
    assert diagnostics["admission_terminal_drop_count"] == 1
    assert diagnostics["admission_deadline_forward_count"] == 1
    assert diagnostics["admission_rejection_applied_sealed_count"] == 1
    assert diagnostics["admission_core_settlement_degraded_count"] == 0
    assert diagnostics["admission_late_operation_ignored_count"] == 1
    assert diagnostics["diagnostic_runtime_missing_identity_count"] == 1
    assert diagnostics["diagnostic_runtime_seal_unbound_count"] == 1
    assert (
        diagnostics["diagnostic_runtime_missing_identity_and_seal_unbound_count"]
        == 1
    )
    assert "similarity" not in diagnostics
    assert "unexpected" not in diagnostics
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inactive_blocked_managers_do_not_override_active_route_status() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    active = _Manager()
    active.is_active = True  # type: ignore[attr-defined]
    active._asr_route_mode = "independent"  # type: ignore[attr-defined]
    inactive = _Manager()
    inactive.is_active = False  # type: ignore[attr-defined]
    inactive._asr_route_mode = "blocked"  # type: ignore[attr-defined]
    inactive._asr_runtime = SimpleNamespace(_speaker_verifier_degraded=True)
    await registry.register_manager(active)
    await registry.register_manager(inactive)
    inactive.verifier_outcomes.append(
        VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    )
    profile = _profile("profile")
    try:
        result = await registry.activate(profile, "generation")
    finally:
        profile.close()

    assert result is VoiceIdentityActivationResult.READY
    assert registry.activation_status() is VoiceIdentityActivationResult.READY

    late_inactive = _Manager()
    late_inactive.is_active = False  # type: ignore[attr-defined]
    late_inactive._asr_route_mode = "blocked"  # type: ignore[attr-defined]
    late_inactive._asr_runtime = SimpleNamespace(_speaker_verifier_degraded=True)
    late_inactive.verifier_outcomes.append(
        VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    )
    assert (
        await registry.register_manager(late_inactive)
        is VoiceIdentityActivationResult.ACTIVATION_PENDING
    )
    assert registry.activation_status() is VoiceIdentityActivationResult.READY

    active._asr_route_mode = "blocked"  # type: ignore[attr-defined]
    assert (
        registry.activation_status()
        is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
    )
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registration_attachment_failure_retries_current_activation() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=1.0,
    )
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    manager = _Manager()
    manager.verifier_outcomes.extend([False, True])

    assert not await registry.register_manager(manager)
    assert manager in registry._attach_pending  # type: ignore[attr-defined]
    await _wait_until(
        lambda: manager not in registry._attach_pending  # type: ignore[attr-defined]
    )

    assert len(manager.verifier_calls) == 2
    assert manager.verifier_calls[-1][1] == "generation"
    await _wait_until(
        lambda: registry._attach_retry_task is None  # type: ignore[attr-defined]
    )
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reregistration_invalidates_stale_detach_before_attach_retry() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=1.0,
    )
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    manager = _Manager()
    assert await registry.register_manager(manager)
    manager.verifier_outcomes.extend([False, False, True])

    await registry.unregister_manager(manager)
    assert manager in registry._detach_pending  # type: ignore[attr-defined]
    calls_before_registration = len(manager.verifier_calls)

    assert not await registry.register_manager(manager)
    assert manager not in registry._detach_pending  # type: ignore[attr-defined]
    assert manager in registry._attach_pending  # type: ignore[attr-defined]
    await _wait_until(
        lambda: manager not in registry._attach_pending  # type: ignore[attr-defined]
    )

    registration_calls = manager.verifier_calls[calls_before_registration:]
    assert len(registration_calls) == 2
    assert all(factory is not None for factory, _generation in registration_calls)
    assert all(generation == "generation" for _factory, generation in registration_calls)
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_activation_rolls_changed_managers_back() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=False)
    managers = [_Manager(), _Manager()]
    for manager in managers:
        await registry.register_manager(manager)
    old_profile = _profile("old")
    new_profile = _profile("new")
    try:
        assert await registry.activate(old_profile, "old-generation")
        ordered = tuple(registry._managers)  # type: ignore[attr-defined]
        ordered[1].verifier_outcomes.append(False)

        assert not await registry.activate(new_profile, "new-generation")

        await _wait_until(
            lambda: all(
                manager
                not in registry._attach_pending  # type: ignore[attr-defined]
                for manager in ordered
            )
        )
        assert ordered[0].verifier_calls[-1][1] == "old-generation"
        assert ordered[1].verifier_calls[-1][1] == "old-generation"
        assert not ordered[0].verifier_calls[-1][0].enforce
    finally:
        old_profile.close()
        new_profile.close()
        await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_activation_retries_prior_verifier_when_rollback_degrades() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=1.0,
    )
    managers = [_Manager(), _Manager()]
    for manager in managers:
        await registry.register_manager(manager)
    old_profile = _profile("old")
    new_profile = _profile("new")
    try:
        assert await registry.activate(old_profile, "old-generation")
        ordered = tuple(registry._managers)  # type: ignore[attr-defined]
        ordered[0].verifier_outcomes.extend([True, False, True])
        ordered[1].verifier_outcomes.append(False)

        assert not await registry.activate(new_profile, "new-generation")

        assert ordered[0] in registry._attach_pending  # type: ignore[attr-defined]
        assert ordered[0] not in registry._detach_pending  # type: ignore[attr-defined]
        await _wait_until(
            lambda: ordered[0]
            not in registry._attach_pending  # type: ignore[attr-defined]
        )
        assert ordered[0].verifier_calls[-1][1] == "old-generation"
        assert ordered[0].verifier_calls[-1][0] is not None
    finally:
        old_profile.close()
        new_profile.close()
        await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_activation_hands_blocked_rollback_to_watchdog() -> None:
    class BlockingRollbackManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.block_generation: str | None = None
            self.rollback_started = asyncio.Event()
            self.rollback_release = asyncio.Event()

        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool:
            if activation_generation == self.block_generation:
                self.rollback_started.set()
                # Intentionally ignore cancellation to model a blocking rollback;
                # cleanup must set rollback_release before closing the registry.
                while not self.rollback_release.is_set():
                    try:
                        await self.rollback_release.wait()
                    except asyncio.CancelledError:
                        continue
            return await super().set_speaker_verifier_factory(
                factory,
                activation_generation=activation_generation,
            )

    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=1.0,
    )
    managers = [BlockingRollbackManager(), BlockingRollbackManager()]
    for manager in managers:
        await registry.register_manager(manager)
    old_profile = _profile("old")
    new_profile = _profile("new")
    try:
        assert await registry.activate(old_profile, "old-generation")
        ordered = tuple(registry._managers)  # type: ignore[attr-defined]
        changed = ordered[0]
        failed = ordered[1]
        changed.block_generation = "old-generation"
        failed.verifier_outcomes.append(False)

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        assert not await registry.activate(new_profile, "new-generation")
        assert loop.time() - started_at < 0.5
        assert changed in registry._attach_pending  # type: ignore[attr-defined]

        await asyncio.wait_for(changed.rollback_started.wait(), timeout=0.5)
        changed.rollback_release.set()
        await _wait_until(
            lambda: changed not in registry._attach_pending  # type: ignore[attr-defined]
        )
    finally:
        old_profile.close()
        new_profile.close()
        for manager in managers:
            manager.rollback_release.set()
        await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_attach_closes_unadopted_factory_material() -> None:
    class CancellingManager(_Manager):
        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool:
            self.verifier_calls.append((factory, activation_generation))
            raise asyncio.CancelledError

    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = CancellingManager()
    await registry.register_manager(manager)
    profile = _profile("profile")
    try:
        assert not await registry.activate(profile, "generation")
    finally:
        profile.close()

    factory = manager.verifier_calls[-1][0]
    assert factory is not None and factory.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attach_watchdog_propagates_inflight_cancellation() -> None:
    class BlockingRetryManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.retry_started = asyncio.Event()

        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool:
            self.verifier_calls.append((factory, activation_generation))
            if factory is None:
                return True
            if len(self.verifier_calls) == 1:
                factory.close()
                return False
            self.retry_started.set()
            await asyncio.Event().wait()
            return True

    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=1.0,
    )
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    manager = BlockingRetryManager()
    assert not await registry.register_manager(manager)
    await asyncio.wait_for(manager.retry_started.wait(), 1)

    watchdog = registry._attach_retry_task  # type: ignore[attr-defined]
    assert watchdog is not None
    watchdog.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watchdog

    factory = manager.verifier_calls[-1][0]
    assert factory is not None and factory.closed
    assert registry._attach_retry_task is None  # type: ignore[attr-defined]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detach_clears_current_and_future_factory() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    current = _Manager()
    await registry.register_manager(current)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "active-generation")
    finally:
        profile.close()

    assert await registry.activate(None, "detach-generation")
    assert current.verifier_calls[-1] == (None, "detach-generation")
    future = _Manager()
    assert await registry.register_manager(future)
    assert future.verifier_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_detach_never_restores_old_activation() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=0.1,
    )
    manager = _Manager()
    await registry.register_manager(manager)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "active-generation")
    finally:
        profile.close()
    old_activation = registry._activation  # type: ignore[attr-defined]
    manager.verifier_outcomes.append(False)

    assert not await registry.activate(None, "detach-generation")

    assert registry._activation is None  # type: ignore[attr-defined]
    assert old_activation.profile.closed
    assert manager.verifier_calls[-1] == (None, "detach-generation")
    assert registry._detach_pending[manager] == "detach-generation"  # type: ignore[attr-defined]
    future = _Manager()
    assert await registry.register_manager(future)
    assert future.verifier_calls == []
    await _wait_until(
        lambda: manager
        not in registry._detach_pending  # type: ignore[attr-defined]
    )
    assert all(
        generation != "active-generation"
        for _factory, generation in manager.verifier_calls[1:]
    )
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_detach_defers_current_and_remaining_managers() -> None:
    class BlockingDetachManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.detach_started = asyncio.Event()
            self.detach_release = asyncio.Event()

        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool:
            if factory is None:
                self.verifier_calls.append((factory, activation_generation))
                self.detach_started.set()
                await self.detach_release.wait()
            return await super().set_speaker_verifier_factory(
                factory,
                activation_generation=activation_generation,
            )

    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=10.0,
    )
    managers = [BlockingDetachManager(), BlockingDetachManager()]
    for manager in managers:
        await registry.register_manager(manager)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "active-generation")
    finally:
        profile.close()
    ordered = tuple(registry._managers)  # type: ignore[attr-defined]

    detach_task = asyncio.create_task(
        registry.activate(None, "detach-generation")
    )
    await asyncio.wait_for(ordered[0].detach_started.wait(), 1.0)
    detach_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(detach_task, 1.0)
    assert not ordered[1].detach_started.is_set()
    assert registry._detach_pending == {  # type: ignore[attr-defined]
        ordered[0]: "detach-generation",
        ordered[1]: "detach-generation",
    }
    for manager in ordered:
        manager.detach_release.set()
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suppression_and_restore_apply_to_all_managers() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    managers = [_Manager(), _Manager()]
    for manager in managers:
        await registry.register_manager(manager)

    await registry.suppress("voice_identity_enrollment")
    await registry.suppress("voice_identity_enrollment")
    await registry.restore("voice_identity_enrollment")

    for manager in managers:
        assert manager.suppression_calls == [
            ("voice_identity_enrollment", True),
            ("voice_identity_enrollment", False),
        ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transient_restore_failure_never_leaves_registry_gate() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    failing = _Manager()
    failing.restore_failures = 1
    await registry.register_manager(failing)
    await registry.suppress("voice_identity_enrollment")

    await registry.restore("voice_identity_enrollment")

    assert failing.suppression_calls[-2:] == [
        ("voice_identity_enrollment", False),
        ("voice_identity_enrollment", False),
    ]
    assert not registry._suppressed  # type: ignore[attr-defined]
    assert not registry._restore_pending  # type: ignore[attr-defined]
    replacement = _Manager()
    await registry.register_manager(replacement)
    assert replacement.suppression_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restore_watchdog_retries_pending_manager() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=2.0,
    )
    failing = _Manager()
    failing.restore_failures = 3
    await registry.register_manager(failing)
    await registry.suppress("voice_identity_enrollment")

    await registry.restore("voice_identity_enrollment")
    assert registry._restore_pending  # type: ignore[attr-defined]
    await _wait_until(
        lambda: (
            not registry._restore_pending  # type: ignore[attr-defined]
            and registry._restore_retry_task is None
        )  # type: ignore[attr-defined]
    )

    assert not registry._restore_pending  # type: ignore[attr-defined]
    assert registry._restore_retry_task is None  # type: ignore[attr-defined]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bounded_restore_allows_second_attempt_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS",
        0.2,
    )

    class FirstAttemptBlocks(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def set_voice_input_suppressed(
            self,
            reason: str,
            *,
            suppressed: bool,
        ) -> None:
            self.calls += 1
            self.suppression_calls.append((reason, suppressed))
            if self.calls == 1:
                await asyncio.Event().wait()

    manager = FirstAttemptBlocks()

    restored = await OwnerVoiceRuntimeRegistry._restore_manager_bounded(
        manager,
        "voice_identity_enrollment",
    )

    assert restored
    assert manager.calls == 2
    assert manager.suppression_calls == [
        ("voice_identity_enrollment", False),
        ("voice_identity_enrollment", False),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_restore_cannot_gate_replacement_manager() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    cancelled = _Manager()
    await registry.register_manager(cancelled)
    await registry.suppress("voice_identity_enrollment")
    cancelled.cancel_restore = True

    with pytest.raises(asyncio.CancelledError):
        await registry.restore("voice_identity_enrollment")

    assert not registry._suppressed  # type: ignore[attr-defined]
    replacement = _Manager()
    await registry.register_manager(replacement)
    assert replacement.suppression_calls == []
    cancelled.cancel_restore = False
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manager_replacement_gets_current_state_and_old_is_detached() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    old = _Manager()
    await registry.register_manager(old)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    await registry.suppress("voice_identity_enrollment")

    replacement = _Manager()
    assert await registry.register_manager(replacement)
    assert replacement.suppression_calls == [("voice_identity_enrollment", True)]
    assert replacement.verifier_calls[-1][1] == "generation"

    await registry.unregister_manager(old)
    assert old.verifier_calls[-1][0] is None
    assert old.suppression_calls[-1] == ("voice_identity_enrollment", False)

    new_profile = _profile("new")
    try:
        assert await registry.activate(new_profile, "new-generation")
    finally:
        new_profile.close()
    assert replacement.verifier_calls[-1][1] == "new-generation"
    assert old.verifier_calls[-1][1] != "new-generation"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unregister_cancellation_records_cleanup_before_propagating() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _Manager()
    await registry.register_manager(manager)
    await registry.suppress("voice_identity_enrollment")
    manager.verifier_outcomes.append(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await registry.unregister_manager(manager)

    assert manager in registry._detach_pending  # type: ignore[attr-defined]
    assert manager.suppression_calls[-1] == (
        "voice_identity_enrollment",
        False,
    )
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unregister_bounds_pending_restore_and_starts_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS",
        0.02,
    )
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.5,
        restore_retry_timeout_seconds=1.0,
    )
    manager = _Manager()
    await registry.register_manager(manager)
    registry._restore_pending.add(manager)  # type: ignore[attr-defined]
    restore_started = asyncio.Event()

    async def never_restore(reason: str, *, suppressed: bool) -> None:
        manager.suppression_calls.append((reason, suppressed))
        if not suppressed:
            restore_started.set()
            await asyncio.Event().wait()

    manager.set_voice_input_suppressed = never_restore  # type: ignore[method-assign]

    await asyncio.wait_for(registry.unregister_manager(manager), timeout=0.2)

    assert restore_started.is_set()
    assert manager in registry._restore_pending  # type: ignore[attr-defined]
    assert registry._restore_retry_task is not None  # type: ignore[attr-defined]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_register_bounds_pending_restore_and_starts_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS",
        0.02,
    )
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.5,
        restore_retry_timeout_seconds=1.0,
    )
    manager = _Manager()
    registry._restore_pending.add(manager)  # type: ignore[attr-defined]
    restore_started = asyncio.Event()

    async def never_restore(reason: str, *, suppressed: bool) -> None:
        manager.suppression_calls.append((reason, suppressed))
        if not suppressed:
            restore_started.set()
            await asyncio.Event().wait()

    manager.set_voice_input_suppressed = never_restore  # type: ignore[method-assign]

    result = await asyncio.wait_for(registry.register_manager(manager), timeout=0.2)

    assert result is VoiceIdentityActivationResult.RUNTIME_DEGRADED
    assert restore_started.is_set()
    assert manager in registry._restore_pending  # type: ignore[attr-defined]
    assert registry._restore_retry_task is not None  # type: ignore[attr-defined]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attach_failure_keeps_manager_registered_for_recovery() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    active_profile = _profile("profile")
    try:
        assert await registry.activate(active_profile, "generation")
    finally:
        active_profile.close()
    await registry.suppress("voice_identity_enrollment")
    manager = _Manager()
    manager.verifier_outcomes.append(False)

    assert not await registry.register_manager(manager)

    assert manager in registry._managers  # type: ignore[attr-defined]
    assert manager.suppression_calls == [
        ("voice_identity_enrollment", True),
    ]
    replacement_profile = _profile("replacement")
    try:
        assert await registry.activate(replacement_profile, "replacement-generation")
    finally:
        replacement_profile.close()
    assert manager.verifier_calls[-1][1] == "replacement-generation"
    await registry.restore("voice_identity_enrollment")
    assert manager.suppression_calls[-1] == (
        "voice_identity_enrollment",
        False,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_suppression_rollback_gets_watchdog_retry() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=2.0,
    )
    changed = _Manager()
    failing = _Manager()
    await registry.register_manager(changed)
    await registry.register_manager(failing)
    ordered = tuple(registry._managers)  # type: ignore[attr-defined]
    ordered[1].suppress_failure = True
    ordered[0].restore_failures = 3

    with pytest.raises(RuntimeError, match="suppression failed"):
        await registry.suppress("voice_identity_enrollment")

    assert registry._restore_retry_task is not None  # type: ignore[attr-defined]
    await _wait_until(
        lambda: (
            not registry._restore_pending  # type: ignore[attr-defined]
            and registry._restore_retry_task is None
        )  # type: ignore[attr-defined]
    )
    assert not registry._restore_pending  # type: ignore[attr-defined]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watchdog_exhaustion_emits_restore_and_detach_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        runtime_module.logger,
        "warning",
        lambda message, *_args, **_kwargs: warnings.append(message),
    )
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=0.1,
    )
    restore_manager = _Manager()
    detach_manager = _Manager()
    await registry.register_manager(restore_manager)
    await registry.register_manager(detach_manager)
    await registry.suppress("voice_identity_enrollment")

    async def never_restore(reason: str, *, suppressed: bool) -> None:
        restore_manager.suppression_calls.append((reason, suppressed))
        if not suppressed:
            raise RuntimeError("restore remains unavailable")

    restore_manager.set_voice_input_suppressed = never_restore  # type: ignore[method-assign]
    await registry.restore("voice_identity_enrollment")
    restore_task = registry._restore_retry_task  # type: ignore[attr-defined]
    assert restore_task is not None

    async def never_detach(_factory, *, activation_generation: str) -> bool:
        del activation_generation
        return False

    detach_manager.set_speaker_verifier_factory = never_detach  # type: ignore[method-assign]
    await registry.unregister_manager(detach_manager)
    detach_task = registry._detach_retry_task  # type: ignore[attr-defined]
    assert detach_task is not None

    await asyncio.gather(restore_task, detach_task)

    assert any("restore watchdog exhausted" in message for message in warnings)
    assert any("detach watchdog exhausted" in message for message in warnings)
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("watchdog_kind", ["attach", "restore", "detach"])
async def test_watchdog_bounds_never_returning_manager_call(
    watchdog_kind: str,
) -> None:
    class BlockingManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.block_attach = False
            self.block_restore = False
            self.block_detach = False
            self.call_started = asyncio.Event()

        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool:
            if (factory is None and self.block_detach) or (
                factory is not None and self.block_attach
            ):
                self.call_started.set()
                await asyncio.Event().wait()
            return await super().set_speaker_verifier_factory(
                factory,
                activation_generation=activation_generation,
            )

        async def set_voice_input_suppressed(
            self,
            reason: str,
            *,
            suppressed: bool,
        ) -> None:
            if not suppressed and self.block_restore:
                self.call_started.set()
                await asyncio.Event().wait()
            await super().set_voice_input_suppressed(reason, suppressed=suppressed)

    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=0.05,
    )
    manager = BlockingManager()

    if watchdog_kind == "attach":
        profile = _profile("profile")
        try:
            assert await registry.activate(profile, "generation")
        finally:
            profile.close()
        manager.verifier_outcomes.append(False)
        assert not await registry.register_manager(manager)
        manager.block_attach = True
        watchdog = registry._attach_retry_task  # type: ignore[attr-defined]
    elif watchdog_kind == "restore":
        await registry.register_manager(manager)
        await registry.suppress("voice_identity_enrollment")
        manager.restore_failures = 2
        await registry.restore("voice_identity_enrollment")
        manager.block_restore = True
        watchdog = registry._restore_retry_task  # type: ignore[attr-defined]
    else:
        await registry.register_manager(manager)
        manager.verifier_outcomes.append(False)
        await registry.unregister_manager(manager)
        manager.block_detach = True
        watchdog = registry._detach_retry_task  # type: ignore[attr-defined]

    assert watchdog is not None
    await asyncio.wait_for(manager.call_started.wait(), 0.5)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    await asyncio.wait_for(watchdog, 0.5)
    assert loop.time() - started_at < 0.2
    assert watchdog.done()

    manager.block_attach = False
    manager.block_restore = False
    manager.block_detach = False
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_late_registration_timeout_transfers_to_attach_watchdog() -> None:
    class BlockingAttachManager(_Manager):
        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool | VoiceIdentityActivationResult:
            if factory is not None:
                await asyncio.Event().wait()
            return await super().set_speaker_verifier_factory(
                factory,
                activation_generation=activation_generation,
            )

    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=0.05,
    )
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    manager = BlockingAttachManager()

    result = await registry.register_manager(manager)

    assert result is VoiceIdentityActivationResult.RUNTIME_DEGRADED
    assert manager in registry._attach_pending  # type: ignore[attr-defined]
    assert registry._attach_retry_task is not None  # type: ignore[attr-defined]

    async def verifier_success(
        factory: _Factory | None,
        *,
        activation_generation: str,
    ) -> bool:
        del factory, activation_generation
        return True

    manager.set_speaker_verifier_factory = verifier_success  # type: ignore[method-assign]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unregister_timeout_transfers_to_detach_watchdog() -> None:
    class BlockingDetachManager(_Manager):
        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool | VoiceIdentityActivationResult:
            if factory is None:
                await asyncio.Event().wait()
            return await super().set_speaker_verifier_factory(
                factory,
                activation_generation=activation_generation,
            )

    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=0.05,
    )
    manager = BlockingDetachManager()
    await registry.register_manager(manager)

    await registry.unregister_manager(manager)

    assert manager in registry._detach_pending  # type: ignore[attr-defined]
    assert registry._detach_retry_task is not None  # type: ignore[attr-defined]

    async def verifier_success(
        factory: _Factory | None,
        *,
        activation_generation: str,
    ) -> bool:
        del factory, activation_generation
        return True

    manager.set_speaker_verifier_factory = verifier_success  # type: ignore[method-assign]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("watchdog_kind", ["attach", "restore", "detach"])
async def test_watchdog_retries_manager_originated_cancellation(
    watchdog_kind: str,
) -> None:
    class CancellingManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_attach = False
            self.cancel_restore_watchdog = False
            self.cancel_detach = False
            self.cancelled_calls = 0
            self.call_started = asyncio.Event()

        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool:
            if (factory is not None and self.cancel_attach) or (
                factory is None and self.cancel_detach
            ):
                self.cancelled_calls += 1
                self.call_started.set()
                raise asyncio.CancelledError("manager-originated")
            return await super().set_speaker_verifier_factory(
                factory,
                activation_generation=activation_generation,
            )

        async def set_voice_input_suppressed(
            self,
            reason: str,
            *,
            suppressed: bool,
        ) -> None:
            if not suppressed and self.cancel_restore_watchdog:
                self.cancelled_calls += 1
                self.call_started.set()
                raise asyncio.CancelledError("manager-originated")
            await super().set_voice_input_suppressed(reason, suppressed=suppressed)

    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=0.05,
    )
    manager = CancellingManager()

    if watchdog_kind == "attach":
        profile = _profile("profile")
        try:
            assert await registry.activate(profile, "generation")
        finally:
            profile.close()
        manager.verifier_outcomes.append(False)
        assert not await registry.register_manager(manager)
        manager.cancel_attach = True
        watchdog = registry._attach_retry_task  # type: ignore[attr-defined]
    elif watchdog_kind == "restore":
        await registry.register_manager(manager)
        await registry.suppress("voice_identity_enrollment")
        manager.restore_failures = 2
        await registry.restore("voice_identity_enrollment")
        manager.cancel_restore_watchdog = True
        watchdog = registry._restore_retry_task  # type: ignore[attr-defined]
    else:
        await registry.register_manager(manager)
        manager.verifier_outcomes.append(False)
        await registry.unregister_manager(manager)
        manager.cancel_detach = True
        watchdog = registry._detach_retry_task  # type: ignore[attr-defined]

    assert watchdog is not None
    await asyncio.wait_for(manager.call_started.wait(), 0.5)
    await asyncio.wait_for(watchdog, 0.5)
    assert manager.cancelled_calls >= 2
    assert watchdog.done()

    manager.cancel_attach = False
    manager.cancel_restore_watchdog = False
    manager.cancel_detach = False
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_inflight_suppression_restores_current_manager() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _Manager()
    manager.cancel_suppress = True
    await registry.register_manager(manager)

    with pytest.raises(asyncio.CancelledError):
        await registry.suppress("voice_identity_enrollment")

    assert manager.suppression_calls == [
        ("voice_identity_enrollment", True),
        ("voice_identity_enrollment", False),
    ]
    assert not registry._suppressed  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registration_suppression_failure_keeps_manager_for_retry() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=2.0,
    )
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    await registry.suppress("voice_identity_enrollment")
    manager = _Manager()
    manager.suppress_failure = True

    with pytest.raises(RuntimeError, match="suppression failed"):
        await registry.register_manager(manager)

    assert manager in registry._managers  # type: ignore[attr-defined]
    assert manager in registry._attach_pending  # type: ignore[attr-defined]
    assert (
        registry.activation_status()
        is VoiceIdentityActivationResult.RUNTIME_DEGRADED
    )
    manager.suppress_failure = False
    await registry.restore("voice_identity_enrollment")
    await _wait_until(lambda: manager not in registry._attach_pending)  # type: ignore[attr-defined]

    assert registry.activation_status() is VoiceIdentityActivationResult.READY
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registration_suppression_timeout_keeps_manager_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS",
        0.02,
    )
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=0.5,
    )
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    await registry.suppress("voice_identity_enrollment")

    class BlockingSuppressManager(_Manager):
        async def set_voice_input_suppressed(
            self,
            reason: str,
            *,
            suppressed: bool,
        ) -> None:
            self.suppression_calls.append((reason, suppressed))
            if suppressed:
                await asyncio.Event().wait()
            await super().set_voice_input_suppressed(
                reason,
                suppressed=suppressed,
            )

    manager = BlockingSuppressManager()

    result = await registry.register_manager(manager)

    assert result is VoiceIdentityActivationResult.RUNTIME_DEGRADED
    assert manager in registry._managers  # type: ignore[attr-defined]
    assert manager in registry._restore_pending  # type: ignore[attr-defined]
    assert manager in registry._attach_pending  # type: ignore[attr-defined]
    assert (
        registry.activation_status()
        is VoiceIdentityActivationResult.RUNTIME_DEGRADED
    )
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_window", ["restore", "attach"])
async def test_cancelled_registration_keeps_attach_pending_for_retry(
    cancel_window: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS",
        1.0,
    )
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.05,
        restore_retry_timeout_seconds=0.5,
    )
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()

    class BlockingManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.block_restore = False
            self.block_attach = False
            self.block_started = asyncio.Event()

        async def set_voice_input_suppressed(
            self,
            reason: str,
            *,
            suppressed: bool,
        ) -> None:
            self.suppression_calls.append((reason, suppressed))
            if not suppressed and self.block_restore:
                self.block_restore = False
                self.block_started.set()
                await asyncio.Event().wait()
            await super().set_voice_input_suppressed(reason, suppressed=suppressed)

        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool | VoiceIdentityActivationResult:
            if factory is not None and self.block_attach:
                self.block_attach = False
                self.block_started.set()
                await asyncio.Event().wait()
            return await super().set_speaker_verifier_factory(
                factory,
                activation_generation=activation_generation,
            )

    manager = BlockingManager()
    if cancel_window == "restore":
        registry._restore_pending.add(manager)  # type: ignore[attr-defined]
        manager.block_restore = True
    else:
        manager.block_attach = True

    task = asyncio.create_task(registry.register_manager(manager))
    await asyncio.wait_for(manager.block_started.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager in registry._managers  # type: ignore[attr-defined]
    assert manager in registry._attach_pending  # type: ignore[attr-defined]
    assert registry._attach_retry_task is not None  # type: ignore[attr-defined]
    if cancel_window == "restore":
        assert manager in registry._restore_pending  # type: ignore[attr-defined]
        assert registry._restore_retry_task is not None  # type: ignore[attr-defined]

    await _wait_until(
        lambda: manager not in registry._attach_pending  # type: ignore[attr-defined]
        and manager not in registry._restore_pending  # type: ignore[attr-defined]
    )
    assert registry.activation_status() is VoiceIdentityActivationResult.READY
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_registration_keeps_active_suppression_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS",
        1.0,
    )
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=1.0,
        restore_retry_timeout_seconds=2.0,
    )
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    await registry.suppress("voice_identity_enrollment")

    class BlockingAttachManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.attach_started = asyncio.Event()
            self.input_suppressed = False

        async def set_voice_input_suppressed(
            self,
            reason: str,
            *,
            suppressed: bool,
        ) -> None:
            self.input_suppressed = suppressed
            await super().set_voice_input_suppressed(reason, suppressed=suppressed)

        async def set_speaker_verifier_factory(
            self,
            factory: _Factory | None,
            *,
            activation_generation: str,
        ) -> bool | VoiceIdentityActivationResult:
            if factory is not None and not self.attach_started.is_set():
                self.attach_started.set()
                await asyncio.Event().wait()
            return await super().set_speaker_verifier_factory(
                factory,
                activation_generation=activation_generation,
            )

    manager = BlockingAttachManager()
    task = asyncio.create_task(registry.register_manager(manager))
    await asyncio.wait_for(manager.attach_started.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.input_suppressed is True
    assert manager in registry._restore_pending  # type: ignore[attr-defined]
    assert manager in registry._attach_pending  # type: ignore[attr-defined]
    assert manager.suppression_calls == [("voice_identity_enrollment", True)]

    await registry.restore("voice_identity_enrollment")
    assert manager.input_suppressed is False
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_restore_queues_every_manager_before_retry() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=2.0,
    )
    first = _Manager()
    second = _Manager()
    await registry.register_manager(first)
    await registry.register_manager(second)
    await registry.suppress("voice_identity_enrollment")
    first.cancel_restore = True
    second.cancel_restore = True

    with pytest.raises(asyncio.CancelledError):
        await registry.restore("voice_identity_enrollment")

    assert first in registry._restore_pending  # type: ignore[attr-defined]
    assert second in registry._restore_pending  # type: ignore[attr-defined]
    first.cancel_restore = False
    second.cancel_restore = False
    await _wait_until(lambda: not registry._restore_pending)  # type: ignore[attr-defined]
    await registry.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registry_close_bounds_blocking_detach() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _Manager()

    async def never_detach(
        factory: _Factory | None,
        *,
        activation_generation: str,
    ) -> bool:
        del activation_generation
        if factory is None:
            await asyncio.Event().wait()
        return True

    await registry.register_manager(manager)
    manager.set_speaker_verifier_factory = never_detach  # type: ignore[method-assign]

    await asyncio.wait_for(registry.close(), 3.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registry_close_bounds_blocking_restore_before_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS",
        0.02,
    )
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _Manager()
    restore_started = asyncio.Event()
    detach_called = asyncio.Event()

    async def blocking_restore(reason: str, *, suppressed: bool) -> None:
        manager.suppression_calls.append((reason, suppressed))
        if not suppressed:
            restore_started.set()
            await asyncio.Event().wait()

    async def bounded_detach(
        factory: _Factory | None,
        *,
        activation_generation: str,
    ) -> bool:
        del activation_generation
        if factory is None:
            detach_called.set()
        return True

    await registry.register_manager(manager)
    manager.set_voice_input_suppressed = blocking_restore  # type: ignore[method-assign]
    manager.set_speaker_verifier_factory = bounded_detach  # type: ignore[method-assign]
    await registry.suppress("voice_identity_enrollment")

    await asyncio.wait_for(registry.close(), 1.0)

    assert restore_started.is_set()
    assert detach_called.is_set()
    assert not registry._managers  # type: ignore[attr-defined]
    assert registry._activation is None  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registry_close_propagates_external_cancellation_and_cleans() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _Manager()
    detach_started = asyncio.Event()

    async def blocking_detach(
        factory: _Factory | None,
        *,
        activation_generation: str,
    ) -> bool:
        del activation_generation
        if factory is None:
            detach_started.set()
            await asyncio.Event().wait()
        return True

    await registry.register_manager(manager)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    manager.set_speaker_verifier_factory = blocking_detach  # type: ignore[method-assign]
    close_task = asyncio.create_task(registry.close())
    await asyncio.wait_for(detach_started.wait(), 1.0)

    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert not registry._managers  # type: ignore[attr-defined]
    assert registry._activation is None  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registry_close_cancellation_during_watchdog_join_still_cleans() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    manager = _Manager()
    await registry.register_manager(manager)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()

    retry_cancelled = asyncio.Event()
    retry_release = asyncio.Event()

    async def slow_retry_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            retry_cancelled.set()
            await retry_release.wait()

    retry_task = asyncio.create_task(slow_retry_task())
    registry._restore_retry_task = retry_task  # type: ignore[attr-defined]
    close_task = asyncio.create_task(registry.close())
    await asyncio.wait_for(retry_cancelled.wait(), 1.0)

    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()

    retry_release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert not registry._managers  # type: ignore[attr-defined]
    assert registry._activation is None  # type: ignore[attr-defined]
    assert registry._restore_retry_task is None  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejects_unknown_suppression_reason() -> None:
    registry = OwnerVoiceRuntimeRegistry(enforce=True)
    with pytest.raises(ValueError, match="unsupported"):
        await registry.suppress("other")


@pytest.mark.unit
def test_registry_requires_boolean_enforcement_mode() -> None:
    with pytest.raises(TypeError, match="enforce"):
        OwnerVoiceRuntimeRegistry(enforce=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="retry bounds"):
        OwnerVoiceRuntimeRegistry(
            enforce=True,
            restore_retry_interval_seconds=2.0,
            restore_retry_timeout_seconds=1.0,
        )


@pytest.mark.unit
def test_unavailable_profile_store_never_falls_back_to_plaintext(
    tmp_path: Path,
) -> None:
    store = runtime_module._UnavailableProfileStore(tmp_path / "profile")
    profile = _profile("profile")
    try:
        with pytest.raises(RuntimeError, match="secure_storage_unavailable"):
            store.load()
        with pytest.raises(RuntimeError, match="secure_storage_unavailable"):
            store.stage(
                profile,
                audio_contract=desktop_audio_contract_snapshot(
                    noise_reduction_enabled=True,
                ),
            )
        with pytest.raises(RuntimeError, match="secure_storage_unavailable"):
            store.delete()
    finally:
        profile.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_install_and_wrapper_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    installed: list[object] = []

    class FakeProfileStore:
        def __init__(self, _path: Path) -> None:
            raise runtime_module.SecureStorageUnavailableError(
                "secure_storage_unavailable"
            )

    class FakePreferenceStore:
        def __init__(self, path: Path) -> None:
            self.path = path

    class FakeSuppression:
        def __init__(self, suppress, restore, **kwargs) -> None:
            self.suppress = suppress
            self.restore = restore
            self.kwargs = kwargs

    class FakeService:
        def __init__(
            self,
            *args,
            runtime_mode: str,
            runtime_status_callback,
            activation_transaction,
            enrollment_ttl_seconds: float,
            speech_validator_factory,
            enrollment_noise_reduction_enabled: bool,
            ) -> None:
            self.args = args
            self.runtime_mode = runtime_mode
            self.runtime_status_callback = runtime_status_callback
            self.enrollment_ttl_seconds = enrollment_ttl_seconds
            self.speech_validator_factory = speech_validator_factory
            self.enrollment_noise_reduction_enabled = (
                enrollment_noise_reduction_enabled
            )
            self.initialized = 0
            self.closed = 0

        async def initialize(self) -> None:
            self.initialized += 1

        async def close(self) -> None:
            self.closed += 1

    monkeypatch.setattr(runtime_module, "VoiceIdentityProfileStore", FakeProfileStore)
    monkeypatch.setattr(
        runtime_module,
        "VoiceIdentityPreferenceStore",
        FakePreferenceStore,
    )
    monkeypatch.setattr(
        runtime_module,
        "VoiceInputSuppressionController",
        FakeSuppression,
    )
    monkeypatch.setattr(runtime_module, "VoiceIdentityService", FakeService)
    monkeypatch.setattr(
        runtime_module,
        "install_voice_identity_service_for_app",
        installed.append,
    )
    monkeypatch.setenv("NEKO_VOICE_IDENTITY_MODE", "invalid-mode")
    config = SimpleNamespace(local_state_dir=tmp_path)

    service = runtime_module.install_voice_identity_runtime(config)
    assert service.runtime_mode == "off"
    assert service.enrollment_noise_reduction_enabled
    assert "Unsupported NEKO_VOICE_IDENTITY_MODE" in caplog.text
    assert isinstance(service.args[0], runtime_module._UnavailableProfileStore)
    assert service.enrollment_ttl_seconds == 45.0
    assert (
        service.speech_validator_factory
        is runtime_module.SileroEnrollmentSpeechValidator
    )
    assert service.args[2].kwargs == {
        "default_ttl_seconds": 45.0,
        "hard_ttl_seconds": 60.0,
    }
    assert installed == [service]
    assert runtime_module.install_voice_identity_runtime(config) is service

    await runtime_module.initialize_voice_identity_runtime(config)
    assert service.initialized == 1
    await runtime_module.close_voice_identity_runtime()
    assert service.closed == 1

    manager = _Manager()
    assert not await runtime_module.register_voice_identity_manager(manager)
    await runtime_module.unregister_voice_identity_manager(manager)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_close_always_closes_registry_and_preserves_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingService:
        async def close(self) -> None:
            raise RuntimeError("service close failed")

    class Registry:
        closed = False

        async def close(self) -> None:
            self.closed = True

    registry = Registry()
    monkeypatch.setattr(runtime_module, "_service", FailingService())
    monkeypatch.setattr(runtime_module, "_runtime_registry", registry)

    with pytest.raises(RuntimeError, match="service close failed"):
        await runtime_module.close_voice_identity_runtime()

    assert registry.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registration_wrapper_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRegistry:
        async def register_manager(self, _manager) -> bool:
            raise RuntimeError("registration failed")

        async def unregister_manager(self, _manager) -> None:
            self.unregistered = True

    registry = FailingRegistry()
    monkeypatch.setattr(runtime_module, "_runtime_registry", registry)

    assert not await runtime_module.register_voice_identity_manager(object())
    await runtime_module.unregister_voice_identity_manager(object())
    assert registry.unregistered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registry_close_cancels_watchdog_and_detaches_managers() -> None:
    registry = OwnerVoiceRuntimeRegistry(
        enforce=True,
        restore_retry_interval_seconds=0.01,
        restore_retry_timeout_seconds=1.0,
    )
    manager = _Manager()
    manager.restore_failures = 100
    await registry.register_manager(manager)
    profile = _profile("profile")
    try:
        assert await registry.activate(profile, "generation")
    finally:
        profile.close()
    await registry.suppress("voice_identity_enrollment")
    await registry.restore("voice_identity_enrollment")
    retry_task = registry._restore_retry_task  # type: ignore[attr-defined]
    assert retry_task is not None

    await registry.close()
    await registry.close()

    assert retry_task.done()
    assert registry._restore_retry_task is None  # type: ignore[attr-defined]
    assert not registry._managers  # type: ignore[attr-defined]
    assert registry._activation is None  # type: ignore[attr-defined]
    assert manager.verifier_calls[-1][0] is None
