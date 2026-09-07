"""Deterministic Core scheduling regressions; Runtime transaction tested separately."""

import asyncio
import inspect
import textwrap
from unittest.mock import AsyncMock, Mock

import pytest

from main_logic.asr_client.speaker_verifier_contracts import (
    SpeakerVerifierAuthority,
    SpeakerVerifierInstallIdentity,
    SpeakerVerifierInstallOutcome as Outcome,
    SpeakerVerifierInstallReceipt,
    SpeakerVerifierOwnershipState,
    SpeakerVerifierSpec,
)
from tests.unit.test_core_independent_asr import _Runtime
import main_logic.core.asr_runtime as core_asr_module


def _spec(revision):
    authority = SpeakerVerifierAuthority()
    authority.commit()
    return SpeakerVerifierSpec("profile", revision, True, True, authority, Mock())


def _manager():
    manager = _Runtime()
    manager.input_mode = "audio"
    manager.is_active = False
    runtime = manager._asr_runtime
    runtime._speaker_verifier_route_supported = Mock(return_value=True)
    runtime.retire_speaker_verifier_authority = Mock()
    runtime.create_speaker_verifier_install_identity = lambda **kw: SpeakerVerifierInstallIdentity(
        runtime_identity=id(runtime), session_generation=0, detector_identity=0,
        detector_epoch=0, installation_id=kw["activation_revision"], **kw,
    )

    async def install(spec, identity):
        receipt = SpeakerVerifierInstallReceipt(
            identity, Outcome.INSTALLED if spec is not None else Outcome.REVOKED,
            SpeakerVerifierOwnershipState.DETECTOR,
        )
        runtime._speaker_verifier_install_receipt = receipt
        return receipt

    runtime.install_speaker_verifier = AsyncMock(side_effect=install)
    return manager


@pytest.mark.asyncio
async def test_idle_registration_retains_goal_without_allocating_and_start_reconciles():
    manager = _manager()
    spec = _spec("activation")
    try:
        receipt = await manager.set_speaker_verifier_spec(spec)
        assert receipt.outcome is Outcome.DEFERRED_ROUTE
        spec.factory_builder.assert_not_called()
        manager._asr_runtime.install_speaker_verifier.assert_not_awaited()
        manager._set_microphone_route("independent")
        receipt = await manager.reconcile_speaker_verifier()
        assert receipt.outcome is Outcome.INSTALLED
        assert manager.speaker_verifier_installation_status("activation").outcome is Outcome.INSTALLED
        await manager.reconcile_speaker_verifier()
        manager._asr_runtime.install_speaker_verifier.assert_awaited_once()
    finally:
        await manager._asr_runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_route_change_during_install_never_publishes_stale_ready(iteration):
    manager = _manager()
    manager._set_microphone_route("independent")
    entered, resume = asyncio.Event(), asyncio.Event()
    original = manager._asr_runtime.install_speaker_verifier.side_effect

    async def blocked(spec, identity):
        entered.set()
        await resume.wait()
        return await original(spec, identity)

    manager._asr_runtime.install_speaker_verifier.side_effect = blocked
    operation = asyncio.create_task(manager.set_speaker_verifier_spec(_spec("A")))
    try:
        await entered.wait()
        manager._set_microphone_route("native")
        assert manager._asr_runtime.retire_speaker_verifier_authority.call_count >= 2
        resume.set()
        assert (await operation).outcome is Outcome.STALE
        assert manager.speaker_verifier_installation_status("A").outcome is Outcome.UNSUPPORTED_ROUTE
    finally:
        resume.set()
        await asyncio.gather(operation, return_exceptions=True)
        await manager._asr_runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_three_targets_have_one_owner_and_only_latest_queued_target_installs(iteration):
    manager = _manager()
    manager._set_microphone_route("independent")
    entered, resume = asyncio.Event(), asyncio.Event()
    original = manager._asr_runtime.install_speaker_verifier.side_effect
    installed = []

    async def blocked(spec, identity):
        installed.append(spec.activation_revision)
        if spec.activation_revision == "A":
            entered.set()
            await resume.wait()
        return await original(spec, identity)

    manager._asr_runtime.install_speaker_verifier.side_effect = blocked
    first = asyncio.create_task(manager.set_speaker_verifier_spec(_spec("A")))
    others = []
    try:
        await entered.wait()
        # Event-loop barriers, not timing sleeps: each request has bound its
        # desired spec once the deliberately blocked lock has a waiter.
        bound_b, bound_c = asyncio.Event(), asyncio.Event()

        async def request(spec, bound):
            manager._retire_core_speaker_installation()
            manager._speaker_verifier_spec = spec
            bound.set()
            return await manager.reconcile_speaker_verifier()

        others.append(asyncio.create_task(request(_spec("B"), bound_b)))
        await bound_b.wait()
        others.append(asyncio.create_task(request(_spec("C"), bound_c)))
        await bound_c.wait()
        resume.set()
        results = await asyncio.gather(first, *others)
        assert [r.outcome for r in results] == [Outcome.STALE, Outcome.STALE, Outcome.INSTALLED]
        assert installed == ["A", "C"]
        assert manager.speaker_verifier_installation_status("C").outcome is Outcome.INSTALLED
    finally:
        resume.set()
        await asyncio.gather(first, *others, return_exceptions=True)
        await manager._asr_runtime.close()


@pytest.mark.asyncio
async def test_text_manager_is_deferred_and_unsupported_goal_survives_round_trip():
    manager = _manager()
    manager.input_mode = "text"
    manager.is_active = True
    spec = _spec("A")
    try:
        assert (await manager.set_speaker_verifier_spec(spec)).outcome is Outcome.DEFERRED_ROUTE
        manager.input_mode = "audio"
        manager._set_microphone_route("native")
        assert (await manager.reconcile_speaker_verifier()).outcome is Outcome.UNSUPPORTED_ROUTE
        assert manager._speaker_verifier_spec is spec
        manager._set_microphone_route("independent")
        assert (await manager.reconcile_speaker_verifier()).outcome is Outcome.INSTALLED
    finally:
        await manager._asr_runtime.close()


@pytest.mark.asyncio
async def test_mutation_blind_post_await_publish_is_caught(monkeypatch):
    """Restore the old unchecked commit in memory; the route probe must fail."""
    source = textwrap.dedent(inspect.getsource(
        core_asr_module.AsrRuntimeMixin.reconcile_speaker_verifier,
    ))
    guard = """if (
            getattr(self, "_speaker_verifier_spec", None) is not spec
            or fence != self._speaker_installation_fence()
        ):"""
    assert source.count(guard) == 1
    namespace = dict(vars(core_asr_module))
    exec(compile(source.replace(guard, "if False:"), "<unchecked-install-mutation>", "exec"), namespace)
    monkeypatch.setattr(
        core_asr_module.AsrRuntimeMixin,
        "reconcile_speaker_verifier",
        namespace["reconcile_speaker_verifier"],
    )
    with pytest.raises(AssertionError):
        await test_route_change_during_install_never_publishes_stale_ready(0)
