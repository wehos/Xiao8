from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.admission.contracts import BoundaryProof
from main_logic.asr_client.runtime import _ProviderBoundaryCompletion
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack, _close_stack,
)


def _install_proof(runtime, detector, proof_id=100):
    proof = BoundaryProof(proof_id, 0, ProviderUtteranceKey(0, 0, proof_id))
    snapshot = object()
    owner = _ProviderBoundaryCompletion(snapshot, None, detector)
    runtime._asr_provider_boundary_proofs[proof.proof_id] = snapshot
    runtime._asr_provider_boundary_completions[proof] = owner
    return proof, snapshot, owner


async def _join_retirement(runtime):
    tasks = tuple(runtime._asr_owned_cleanup_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 1.0)


@pytest.mark.parametrize("failure", ["pending", "exception", "timeout"])
async def test_unsettled_completion_retires_session_once_without_batch_revoke(monkeypatch, failure):
    core, runtime, detector, _, _, session, _ = await _active_real_stack()
    proof, _, _ = _install_proof(runtime, detector)
    other_proof, _, _ = _install_proof(runtime, detector, 101)
    unknown = AsyncMock()
    monkeypatch.setattr(runtime, "_retire_provider_speaker_boundary_unknown", unknown)
    deadlines = []
    lifecycle_events = AsyncMock(wraps=runtime._callbacks.on_lifecycle)
    runtime._callbacks = replace(runtime._callbacks, on_lifecycle=lifecycle_events)

    async def complete(snapshot, *, successor_evidence_lease, deadline):
        deadlines.append(deadline)
        if failure == "exception":
            raise RuntimeError("completion failed")
        if failure == "timeout":
            await asyncio.Event().wait()
        return "pending"

    monkeypatch.setattr(detector, "complete_provider_speaker_boundary", complete)
    try:
        await asyncio.wait_for(runtime._retire_admission_boundary_proofs(
            (proof, other_proof), detector, completion=True,
        ), 0.6)
        await _join_retirement(runtime)
        assert deadlines
        assert runtime._asr_session is not session
        assert runtime._asr_provider_boundary_proofs == {}
        assert runtime._asr_provider_boundary_completions == {}
        unknown.assert_not_awaited()
        epoch = runtime._asr_session_epoch
        await runtime._retire_admission_boundary_proofs((proof, other_proof), detector, completion=True)
        assert runtime._asr_session_epoch == epoch
        assert not runtime._asr_owned_cleanup_tasks
        reason = "ASR_BOUNDARY_COMPLETION_UNSETTLED" if failure == "pending" else "ASR_BOUNDARY_COMPLETION_FAILED"
        events = [call.args[0] for call in lifecycle_events.await_args_list]
        assert any(event.reason_code == reason and event.incident_id for event in events)
    finally:
        await _close_stack(core)


async def test_cancelled_completion_effect_unwinds_without_joining_its_retirement(monkeypatch):
    core, runtime, detector, _, _, session, turn = await _active_real_stack()
    proof, _, _ = _install_proof(runtime, detector)
    entered = asyncio.Event()
    unknown = AsyncMock()
    monkeypatch.setattr(runtime, "_retire_provider_speaker_boundary_unknown", unknown)

    async def complete(snapshot, *, successor_evidence_lease, deadline):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(detector, "complete_provider_speaker_boundary", complete)
    effect = asyncio.create_task(runtime._retire_admission_boundary_proofs((proof,), detector, completion=True))
    runtime._track_admission_effect_task(effect, turn)
    effect.add_done_callback(runtime._admission_effect_done)
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        effect.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(effect), 1.0)
        await _join_retirement(runtime)
        assert runtime._asr_session is not session
        assert not runtime._asr_provider_boundary_proofs
        assert not runtime._asr_provider_boundary_completions
        unknown.assert_not_awaited()
    finally:
        if not effect.done():
            effect.cancel()
            await asyncio.gather(effect, return_exceptions=True)
        await _close_stack(core)


async def test_late_completion_failure_cannot_retire_replacement_session_or_proof(monkeypatch):
    core, runtime, detector, _, _, _, _ = await _active_real_stack()
    proof, _, _ = _install_proof(runtime, detector)
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(snapshot, *, successor_evidence_lease, deadline):
        entered.set()
        await release.wait()
        return "pending"

    monkeypatch.setattr(detector, "complete_provider_speaker_boundary", complete)
    operation = asyncio.create_task(runtime._retire_admission_boundary_proofs((proof,), detector, completion=True))
    try:
        await entered.wait()
        replacement = SimpleNamespace(is_ready=True, close=AsyncMock())
        runtime._asr_session = replacement
        runtime._asr_session_epoch += 1
        runtime._asr_audio_generation += 1
        _, new_snapshot, new_owner = _install_proof(runtime, detector)
        release.set()
        await asyncio.wait_for(operation, 1.0)
        assert runtime._asr_session is replacement
        assert runtime._asr_provider_boundary_proofs[proof.proof_id] is new_snapshot
        assert runtime._asr_provider_boundary_completions[proof] is new_owner
        assert not runtime._asr_owned_cleanup_tasks
    finally:
        release.set()
        await _close_stack(core)


async def test_old_detector_cleanup_started_after_replacement_cannot_retire_new_session(monkeypatch):
    core, runtime, old_detector, _, _, session, _ = await _active_real_stack()
    proof, _, _ = _install_proof(runtime, old_detector)
    monkeypatch.setattr(old_detector, "complete_provider_speaker_boundary", AsyncMock(return_value="pending"))
    replacement_detector = SimpleNamespace(close=AsyncMock())
    try:
        runtime._asr_detector = replacement_detector
        epoch = runtime._asr_session_epoch
        await runtime._retire_admission_boundary_proofs((proof,), old_detector, completion=True)
        assert runtime._asr_session is session
        assert runtime._asr_detector is replacement_detector
        assert runtime._asr_session_epoch == epoch
        assert not runtime._asr_provider_boundary_proofs
        assert not runtime._asr_provider_boundary_completions
        assert not runtime._asr_owned_cleanup_tasks
    finally:
        runtime._asr_detector = old_detector
        await _close_stack(core)
