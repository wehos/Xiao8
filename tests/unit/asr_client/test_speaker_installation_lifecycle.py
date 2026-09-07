"""Deterministic install races using real Runtime, Detector and Admission locks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client.runtime import IndependentAsrRuntime, AsrRuntimeCallbacks
from main_logic.asr_client.endpointing.detector_runtime import DetectorRuntime
from main_logic.asr_client.provider_policy import resolve_provider_policy
from main_logic.asr_client.speaker_verifier_contracts import (
    SpeakerVerifierAuthority,
    SpeakerVerifierSpec,
    SpeakerVerifierHealthEvent,
    SpeakerVerifierInstallOutcome as Outcome,
)


class Shadow:
    def __init__(self):
        self.closed = False
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()

    async def close(self):
        self.close_entered.set()
        await self.close_release.wait()
        self.closed = True


def test_installation_change_preserves_scoring_contract():
    from main_logic.voice_identity_service.policy import OwnerVoicePolicy
    from main_logic.voice_identity_service.enrollment import (
        ENROLLMENT_SIMILARITY_THRESHOLD,
        ENROLLMENT_VERIFICATION_AUDIO_MS,
    )

    assert OwnerVoicePolicy.SIMILARITY_THRESHOLD == 0.40
    assert OwnerVoicePolicy.FIRST_CHECKPOINT_MS == 1_500
    assert OwnerVoicePolicy.SECOND_CHECKPOINT_MS == 3_000
    assert ENROLLMENT_SIMILARITY_THRESHOLD == 0.40
    assert ENROLLMENT_VERIFICATION_AUDIO_MS == 5_000


class Factory:
    def __init__(self, shadow):
        self.shadow = shadow
        self.created = asyncio.Event()
        self.closed = False

    def __call__(self):
        self.created.set()
        return self.shadow

    def close(self):
        self.closed = True


def setup_install():
    callbacks = AsrRuntimeCallbacks(
        display_name=lambda: "install-test",
        **{
            name: AsyncMock()
            for name in (
                "on_prepare_turn",
                "on_partial",
                "on_final",
                "on_turn_abandoned",
                "on_failure",
                "on_status",
                "on_lifecycle",
            )
        },
    )
    runtime = IndependentAsrRuntime(callbacks)
    policy = resolve_provider_policy("qwen", endpointing_mode="provider")
    runtime._asr_lifecycle = SimpleNamespace(provider_policy=policy)
    runtime._asr_detector = DetectorRuntime(provider_policy=policy)
    return runtime


def target(runtime, revision="a"):
    shadow = Shadow()
    factory = Factory(shadow)
    authority = SpeakerVerifierAuthority()
    authority.commit()
    spec = SpeakerVerifierSpec(
        "profile", revision, True, True, authority, lambda runtime, identity: factory
    )
    identity = runtime.create_speaker_verifier_install_identity(1, 1, revision)
    return spec, identity, factory, shadow


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_cancel_before_handoff_closes_created_shadow(iteration):
    runtime = setup_install()
    spec, identity, factory, shadow = target(runtime)
    await runtime._asr_admission._lock.acquire()
    task = asyncio.create_task(runtime.install_speaker_verifier(spec, identity))
    await factory.created.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime._asr_admission._lock.release()
    assert shadow.closed and factory.closed
    assert runtime._asr_detector._speaker_shadow is None
    assert not runtime.speaker_verifier_installation_permits_evidence(identity)
    await runtime._asr_detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_detector_disappears_during_revoke_never_installed(iteration):
    runtime = setup_install()
    detector = runtime._asr_detector
    spec, identity, factory, shadow = target(runtime)
    await runtime._asr_admission._lock.acquire()
    task = asyncio.create_task(runtime.install_speaker_verifier(spec, identity))
    await factory.created.wait()
    runtime._asr_detector = None
    runtime._asr_admission._lock.release()
    receipt = await task
    assert receipt.outcome is Outcome.STALE
    assert shadow.closed and factory.closed
    await detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_install_timeout_before_transfer_closes_owned_shadow(iteration):
    runtime = setup_install()
    spec, identity, factory, shadow = target(runtime)
    await runtime._asr_admission._lock.acquire()
    task = asyncio.create_task(runtime.install_speaker_verifier(spec, identity))
    await factory.created.wait()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=0)
        assert shadow.closed and factory.closed
        assert not runtime.speaker_verifier_installation_permits_evidence(identity)
    finally:
        runtime._asr_admission._lock.release()
        await runtime._asr_detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_inflight_health_survives_install_commit_and_old_health_is_stale(
    iteration,
):
    runtime = setup_install()
    first, first_id, _, old = target(runtime)
    assert (
        await runtime.install_speaker_verifier(first, first_id)
    ).outcome is Outcome.INSTALLED
    old.close_release.clear()
    spec, identity, _, shadow = target(runtime, "b")
    task = asyncio.create_task(runtime.install_speaker_verifier(spec, identity))
    await old.close_entered.wait()
    runtime._accept_speaker_verifier_health(
        SpeakerVerifierHealthEvent(identity, 1, frozenset({"backend_unavailable"}))
    )
    old.close_release.set()
    assert (await task).outcome is Outcome.INSTALLED
    assert runtime._speaker_verifier_degraded
    runtime._accept_speaker_verifier_health(SpeakerVerifierHealthEvent(first_id, 99))
    assert runtime._speaker_verifier_degraded
    runtime._accept_speaker_verifier_health(SpeakerVerifierHealthEvent(identity, 2))
    assert not runtime._speaker_verifier_degraded
    runtime._accept_speaker_verifier_health(
        SpeakerVerifierHealthEvent(identity, 1, frozenset({"backend_unavailable"}))
    )
    assert not runtime._speaker_verifier_degraded
    await runtime._asr_detector.close()
    assert shadow.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_authority_revocation_and_retirement_are_separate_fences(iteration):
    runtime = setup_install()
    spec, identity, _, _ = target(runtime)
    assert (
        await runtime.install_speaker_verifier(spec, identity)
    ).outcome is Outcome.INSTALLED
    assert runtime.speaker_verifier_installation_permits_evidence(identity)
    spec.revocable_authority.revoke()
    assert not spec.revocable_authority.commit()
    assert not runtime.speaker_verifier_installation_permits_evidence(identity)
    with pytest.raises(TypeError):
        bool(runtime._speaker_verifier_install_receipt)
    await runtime._asr_detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_detector_pre_swap_exception_still_owns_cleanup(iteration, monkeypatch):
    runtime = setup_install()
    spec, identity, factory, shadow = target(runtime)

    def fail_before_swap():
        raise RuntimeError("injected pre-swap failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            runtime._asr_detector, "_clear_provider_segment_state", fail_before_swap
        )
        receipt = await runtime.install_speaker_verifier(spec, identity)
    assert receipt.outcome is Outcome.FAILED
    assert shadow.closed and factory.closed
    assert runtime._asr_detector._speaker_shadow is None
    await runtime._asr_detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_pending_health_conflict_cannot_be_erased_at_commit(iteration):
    runtime = setup_install()
    spec, identity, factory, _ = target(runtime)
    await runtime._asr_admission._lock.acquire()
    task = asyncio.create_task(runtime.install_speaker_verifier(spec, identity))
    await factory.created.wait()
    runtime._accept_speaker_verifier_health(
        SpeakerVerifierHealthEvent(identity, 0, frozenset({"backend_unavailable"}))
    )
    runtime._asr_admission._lock.release()
    assert (await task).outcome is Outcome.INSTALLED
    assert runtime._speaker_verifier_degraded
    assert (
        "health_revision_conflict"
        in runtime._speaker_installation_health[identity].causes
    )
    await runtime._asr_detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_failed_physical_cleanup_is_retained_and_caps_new_allocations(iteration):
    runtime = setup_install()

    async def broken_close():
        raise RuntimeError("physical close not proven")

    for revision in ("a", "b", "c"):
        spec, identity, _, shadow = target(runtime, revision)
        receipt = await runtime.install_speaker_verifier(spec, identity)
        assert receipt.outcome is Outcome.INSTALLED
        if revision != "c":
            shadow.close = broken_close
    assert len(runtime._speaker_retired_cleanup) == 2
    spec, identity, factory, _ = target(runtime, "d")
    receipt = await runtime.install_speaker_verifier(spec, identity)
    assert receipt.outcome is Outcome.FAILED and receipt.cleanup_pending
    assert not factory.created.is_set()
    await runtime._asr_detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_cancel_after_detector_accepts_before_lock_closes_new_shadow(iteration):
    runtime = setup_install()
    detector = runtime._asr_detector
    spec, identity, factory, shadow = target(runtime)
    await detector._lock.acquire()
    original_replace = detector.replace_speaker_verifier
    accepted = asyncio.Event()

    async def signal_accept(*args, **kwargs):
        accepted.set()
        return await original_replace(*args, **kwargs)

    detector.replace_speaker_verifier = signal_accept
    task = asyncio.create_task(runtime.install_speaker_verifier(spec, identity))
    await accepted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    detector._lock.release()
    assert shadow.closed and factory.closed
    assert not runtime.speaker_verifier_installation_permits_evidence(identity)
    await detector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_cancel_after_swap_keeps_detector_cleanup_ownership(iteration):
    runtime = setup_install()
    first, first_id, _, old = target(runtime)
    await runtime.install_speaker_verifier(first, first_id)
    old.close_release.clear()
    spec, identity, factory, shadow = target(runtime, "b")
    task = asyncio.create_task(runtime.install_speaker_verifier(spec, identity))
    await old.close_entered.wait()
    task.cancel()
    old.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert factory.closed and old.closed
    assert runtime._asr_detector._speaker_shadow is shadow
    assert not runtime.speaker_verifier_installation_permits_evidence(identity)
    await runtime._asr_detector.close()
    assert shadow.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["erase_health", "skip_unadopted_cleanup"])
async def test_install_regressions_reject_old_ordering(monkeypatch, mutation):
    import inspect
    import textwrap
    import main_logic.asr_client.speaker_verifier_installation as module

    method = module.SpeakerVerifierInstallation._install_speaker_verifier_locked
    source = textwrap.dedent(inspect.getsource(method))
    if mutation == "erase_health":
        source = source.replace("bool(health.causes)", "False")
    else:
        source = source.replace(
            "operation.ownership_state is Ownership.OPERATION",
            "False",
        )
    assert source != textwrap.dedent(inspect.getsource(method))
    namespace = dict(vars(module))
    exec(compile(source, "<installation-order-mutation>", "exec"), namespace)
    monkeypatch.setattr(
        module.SpeakerVerifierInstallation,
        method.__name__,
        namespace[method.__name__],
    )
    with pytest.raises(AssertionError):
        if mutation == "erase_health":
            await test_inflight_health_survives_install_commit_and_old_health_is_stale(
                0
            )
        else:
            await test_cancel_before_handoff_closes_created_shadow(0)
