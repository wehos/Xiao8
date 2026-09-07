from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main_logic.asr_client.runtime as runtime_module
from main_logic.asr_client.admission.contracts import (
    AdmissionDisposition,
    AdmissionResolutionTicket,
    BoundaryProof,
    CaptureClosed,
    CoreSettled,
    LifecycleSettled,
    PendingProviderFinal,
    ProviderFinalReceived,
    ResolveReserved,
    SpeakerCaptureLeaseToken,
    SpeakerCheckpointKind,
    SpeakerLow,
    TransportSettled,
)
from main_logic.asr_client.admission.coordinator import (
    VoiceTurnAdmissionCoordinator,
)
from main_logic.asr_client.admission.ingress import AdmissionIngressLane
from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.runtime import IndependentAsrRuntime
from main_logic.asr_client.lifecycle import FinalKey, VoiceTransportToken
from main_logic.asr_client.transcript import TranscriptDispatcher
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCandidateKey,
)
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken


@pytest.mark.asyncio
async def test_provider_namespace_reset_retires_every_owned_proof() -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    proof = BoundaryProof(
        proof_id=7,
        owner_generation=3,
        provider_key=ProviderUtteranceKey(1, 2, 4),
    )
    snapshot = object()
    correlator = MagicMock()
    correlator.retire_namespace.return_value = SimpleNamespace(
        retired_proofs=(proof,)
    )
    runtime._asr_provider_correlator = correlator
    runtime._asr_provider_correlator_namespace = (1, 2)
    runtime._asr_provider_boundary_proofs = {proof.proof_id: snapshot}
    runtime._asr_detector = MagicMock()
    runtime._asr_admission_effect_tasks = set()
    runtime._asr_exact_callback_tasks = set()
    runtime._asr_admission_effect_task_turns = {}
    runtime._asr_sealed_provider_key = None
    runtime._asr_provider_exact_session = None
    runtime._asr_provider_exact_intervals = {}
    runtime._asr_provider_exact_pending = {}
    runtime._asr_provider_exact_candidates = {}
    runtime._asr_provider_speaker_ledgers = {}
    runtime._asr_provider_speaker_key_ledgers = {}
    runtime._asr_provider_started_turns = {}
    runtime._asr_deferred_provider_started_keys = deque()

    retirement_entered = asyncio.Event()
    retirement_release = asyncio.Event()

    async def retire_boundary_proofs(proofs, detector) -> None:
        assert proofs == (proof,)
        assert detector is runtime._asr_detector
        retirement_entered.set()
        await retirement_release.wait()
        runtime._asr_provider_boundary_proofs.pop(proof.proof_id)

    runtime._retire_admission_boundary_proofs = retire_boundary_proofs
    runtime._admission_effect_done = runtime._asr_admission_effect_tasks.discard

    runtime._reset_asr_provider_transport_namespace(retire_owned_proofs=True)
    await retirement_entered.wait()

    assert runtime._asr_provider_correlator is None
    assert runtime._asr_provider_correlator_namespace is None
    assert runtime._asr_provider_boundary_proofs == {proof.proof_id: snapshot}

    retirement_release.set()
    await asyncio.gather(*runtime._asr_admission_effect_tasks)

    assert runtime._asr_provider_boundary_proofs == {}
    correlator.retire_namespace.assert_called_once_with((1, 2))


@pytest.mark.asyncio
async def test_provider_namespace_retirement_counts_actual_proof_ownership_once(
) -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    runtime._asr_exact_callback_tasks = set()
    proof = BoundaryProof(
        proof_id=8,
        owner_generation=3,
        provider_key=ProviderUtteranceKey(1, 2, 5),
    )
    correlator = MagicMock()
    correlator.retire_namespace.return_value = SimpleNamespace(
        retired_proofs=(proof, proof)
    )
    runtime._asr_provider_correlator = correlator
    runtime._asr_provider_correlator_namespace = (1, 2)
    runtime._asr_provider_boundary_proofs = {proof.proof_id: object()}
    runtime._asr_provider_boundary_completions = {}
    runtime._asr_detector = None
    runtime._asr_admission_effect_tasks = set()
    runtime._asr_admission_effect_task_turns = {}
    runtime._asr_sealed_provider_key = None
    runtime._asr_provider_exact_session = None
    runtime._asr_provider_exact_intervals = {}
    runtime._asr_provider_exact_pending = {}
    runtime._asr_provider_exact_candidates = {}
    runtime._asr_provider_speaker_ledgers = {}
    runtime._asr_provider_speaker_key_ledgers = {}
    runtime._asr_provider_started_turns = {}
    runtime._asr_deferred_provider_started_keys = deque()
    runtime._speaker_rejection_metrics = runtime_module._new_speaker_rejection_metrics()
    runtime._admission_effect_done = runtime._asr_admission_effect_tasks.discard

    runtime._reset_asr_provider_transport_namespace(retire_owned_proofs=True)
    await asyncio.gather(*runtime._asr_admission_effect_tasks)

    assert runtime._asr_provider_boundary_proofs == {}
    assert (
        runtime._speaker_rejection_metrics[
            "admission_boundary_proof_retired_count"
        ]
        == 1
    )
    correlator.retire_namespace.assert_called_once_with((1, 2))


@pytest.mark.asyncio
async def test_speaker_alias_is_retained_until_capture_closed_is_queued() -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    candidate = SpeakerShadowCandidateKey(2, 3, "provider_candidate")
    turn_token = VoiceTurnToken(
        VoiceIngressToken(1, "connection", 2, 3, 4),
        5,
    )
    runtime._ensure_asr_runtime_state = lambda: None
    runtime._speaker_verifier_activation_generation = "verifier"
    runtime._speaker_verifier_enforces_admission = True
    runtime._asr_terminal_close_requested = False
    runtime._asr_detector = MagicMock()
    runtime._asr_admission_ingress_started = True
    runtime._asr_admission_candidate_turns = {candidate: turn_token}
    runtime._asr_admission_candidate_leases = {}
    runtime._asr_provider_exact_candidates = {}
    runtime._asr_provider_speaker_ledgers = {}
    runtime._asr_admission_effect_tasks = set()
    runtime._asr_admission_effect_task_turns = {}
    runtime._admission_effect_done = runtime._asr_admission_effect_tasks.discard

    ingress = MagicMock()

    def post_nowait(*_args, **_kwargs):
        future = asyncio.get_running_loop().create_future()
        future.set_result(())
        return future

    ingress.post_nowait.side_effect = post_nowait
    ingress.retire_turn = AsyncMock(return_value=False)
    runtime._asr_admission_ingress = ingress

    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="verifier",
        enforce=True,
    )
    assert runtime._asr_admission_candidate_turns == {candidate: turn_token}

    assert runtime._close_speaker_evidence(
        CaptureClosed(candidate, 1),
        activation_generation="verifier",
        enforce=True,
        evidence_complete=True,
    )
    assert runtime._asr_admission_candidate_turns == {}

    await asyncio.gather(*runtime._asr_admission_effect_tasks)


@pytest.mark.asyncio
async def test_old_route_bulk_cleanup_does_not_wait_new_route_effects() -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    old_turn = VoiceTurnToken(
        VoiceIngressToken(1, "old", 1, 1, 1),
        1,
    )
    new_turn = VoiceTurnToken(
        VoiceIngressToken(2, "new", 2, 2, 2),
        1,
    )
    old_release = asyncio.Event()
    new_release = asyncio.Event()
    old_task = asyncio.create_task(old_release.wait())
    new_task = asyncio.create_task(new_release.wait())
    runtime._asr_admission_effect_tasks = {old_task, new_task}
    runtime._asr_admission_effect_task_turns = {
        old_task: old_turn,
        new_task: new_turn,
    }
    runtime._execute_admission_effect = AsyncMock()
    dispatcher = MagicMock()
    future = asyncio.get_running_loop().create_future()
    future.set_result((SimpleNamespace(turn_token=old_turn, effects=()),))

    cleanup = asyncio.create_task(
        runtime._finish_admission_invalidation(
            future,
            dispatcher,
            None,
            None,
            None,
        )
    )
    await asyncio.sleep(0)
    assert not cleanup.done()

    old_release.set()
    await asyncio.wait_for(cleanup, timeout=0.2)

    assert not new_task.done()
    dispatcher.invalidate_all.assert_called_once_with()
    new_task.cancel()
    await asyncio.gather(new_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_settlement_posts_retire_record_capacity() -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    coordinator = VoiceTurnAdmissionCoordinator(capacity=1, clock=lambda: 10.0)
    ingress = AdmissionIngressLane(coordinator)
    await ingress.start()
    runtime._asr_admission = coordinator
    runtime._asr_admission_ingress = ingress
    runtime._asr_admission_ingress_started = True
    runtime._asr_admission_effect_tasks = set()
    runtime._asr_admission_effect_task_turns = {}
    runtime._execute_admission_effect = AsyncMock()
    runtime._log_asr_background_task_failure = MagicMock()
    turn_token = VoiceTurnToken(
        VoiceIngressToken(1, "connection", 1, 1, 1),
        1,
    )
    await ingress.open_turn(turn_token)

    effects = await runtime._post_admission_event(
        turn_token,
        ProviderFinalReceived(
            PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2)
        ),
    )
    resolution = next(
        effect for effect in effects if isinstance(effect, ResolveReserved)
    )
    await runtime._post_admission_event(
        turn_token,
        CoreSettled(resolution.ticket),
    )
    await runtime._post_admission_event(
        turn_token,
        TransportSettled(resolution.ticket),
    )
    await runtime._post_admission_event(
        turn_token,
        LifecycleSettled(resolution.ticket),
    )

    assert await coordinator.get_record(turn_token) is None
    await ingress.open_turn(
        VoiceTurnToken(VoiceIngressToken(1, "connection", 1, 1, 1), 2)
    )
    await ingress.close()


@pytest.mark.asyncio
async def test_cancelled_bulk_cleanup_finishes_resolution_before_invalidation() -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    runtime._asr_admission_effect_task_turns = {}
    runtime._execute_admission_effect = AsyncMock()
    dispatcher = MagicMock()
    bulk_future = asyncio.get_running_loop().create_future()
    cleanup = asyncio.create_task(
        runtime._finish_admission_invalidation(
            bulk_future,
            dispatcher,
            None,
            None,
            None,
        )
    )
    await asyncio.sleep(0)
    cleanup.cancel()
    await asyncio.sleep(0)

    dispatcher.invalidate_all.assert_not_called()
    bulk_future.set_result(())
    with pytest.raises(asyncio.CancelledError):
        await cleanup

    dispatcher.invalidate_all.assert_called_once_with()


@pytest.mark.asyncio
async def test_provider_final_callback_waits_only_admission_settlement() -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    key = ProviderUtteranceKey(3, 4, 5)
    settlement = asyncio.Event()
    runtime._asr_session_epoch = 7
    runtime._asr_sealed_provider_key = key
    runtime._asr_provider_correlator = MagicMock()
    runtime._asr_provider_correlator.is_completed.return_value = False
    runtime._asr_provider_started_turns = {}
    runtime._asr_provider_exact_pending = {}
    runtime._asr_provider_exact_intervals = {}
    runtime._asr_provider_speaker_key_ledgers = {}
    runtime._asr_provider_speaker_evidence_lease = None
    runtime._speaker_verifier_activation_generation = None
    runtime._capture_runtime_identity = MagicMock()
    runtime._asr_detector = None
    runtime._accept_provider_timeline = MagicMock(return_value=True)

    async def wait_for_settlement(*_args, **_kwargs) -> None:
        await settlement.wait()

    runtime._handle_independent_asr_final = AsyncMock(
        side_effect=wait_for_settlement
    )

    callback = asyncio.create_task(
        runtime._handle_provider_final(
            key,
            "first",
            7,
            "qwen",
            received_at=10.0,
            admission_deadline=10.2,
        )
    )
    await asyncio.sleep(0)

    assert not callback.done()
    runtime._handle_independent_asr_final.assert_awaited_once()

    settlement.set()
    await callback


@pytest.mark.asyncio
async def test_old_route_settlement_cannot_clear_new_provider_owner() -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    old_turn = VoiceTurnToken(
        VoiceIngressToken(1, "old", 1, 1, 1),
        1,
    )
    old_key = ProviderUtteranceKey(1, 1, 1)
    new_key = ProviderUtteranceKey(2, 2, 1)
    new_turn = VoiceTurnToken(
        VoiceIngressToken(2, "new", 2, 2, 2),
        1,
    )
    old_lease = SpeakerCaptureLeaseToken(1, 1, 1, 1, 1)
    new_lease = SpeakerCaptureLeaseToken(2, 2, 2, 2, 2)
    old_correlator = MagicMock()
    old_correlator.complete.return_value = SimpleNamespace(
        completed=True,
        retired_proofs=(),
    )
    runtime._runtime_identity_matches = MagicMock(return_value=False)
    runtime._asr_smart_turn_lease = None
    runtime._asr_provider_correlator = MagicMock()
    runtime._asr_sealed_provider_key = new_key
    runtime._asr_provider_exact_intervals = {}
    runtime._asr_provider_started_turns = {old_key: new_turn}
    runtime._asr_admission_turn_leases = {old_turn: old_lease}
    runtime._asr_admission_candidate_leases = {}
    runtime._asr_current_speaker_candidate = None
    runtime._asr_current_speaker_lease = None
    runtime._asr_admission_ingress = MagicMock()
    runtime._asr_admission_ingress.retire_speaker_lease = AsyncMock()
    runtime._send_asr_lifecycle_state = AsyncMock(return_value=False)
    runtime._post_admission_event = AsyncMock()

    async def replace_lease_owner(*_args, **_kwargs) -> None:
        runtime._asr_admission_turn_leases[old_turn] = new_lease

    runtime._retire_admission_boundary_proofs = AsyncMock(
        side_effect=replace_lease_owner
    )
    context = runtime_module._AdmissionFinalContext(
        turn_token=old_turn,
        final_key=FinalKey.from_turn(old_turn),
        epoch=1,
        provider="qwen",
        provider_key=old_key,
        lifecycle=MagicMock(),
        detector=None,
        correlator=old_correlator,
        sealed_token=VoiceTransportToken(old_turn, 1),
        provider_fence=None,
        runtime_identity=MagicMock(),
        has_pending_turn=False,
    )
    ticket = AdmissionResolutionTicket(
        old_turn,
        1,
        1,
        AdmissionDisposition.FORWARD,
    )

    await runtime._settle_admission_final(ticket, context)

    assert runtime._asr_sealed_provider_key == new_key
    assert runtime._asr_provider_started_turns == {old_key: new_turn}
    assert runtime._asr_admission_turn_leases[old_turn] == new_lease
    old_correlator.complete.assert_called_once_with(old_key, ticket)
    runtime._asr_provider_correlator.complete.assert_not_called()
    runtime._asr_admission_ingress.retire_speaker_lease.assert_not_awaited()
    runtime._send_asr_lifecycle_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_admission_settlement_does_not_wait_blocked_core_delivery() -> None:
    turn_token = VoiceTurnToken(
        VoiceIngressToken(1, "connection", 1, 1, 1),
        1,
    )
    final_key = FinalKey.from_turn(turn_token)
    core_entered = asyncio.Event()
    core_release = asyncio.Event()

    async def dispatch(_envelope) -> None:
        core_entered.set()
        await core_release.wait()

    dispatcher = TranscriptDispatcher(dispatch)
    assert dispatcher.try_reserve(final_key)
    runtime = object.__new__(IndependentAsrRuntime)
    runtime._asr_admission_resolutions = {}
    runtime._asr_provider_turn_ownerships = {}
    runtime._asr_provider_exact_intervals = {}
    runtime._asr_speaker_deny_cleanups = {}
    runtime._asr_current_speaker_lease = None
    runtime._asr_admission_reservation_dispatchers = {
        final_key: dispatcher,
    }
    runtime._asr_admission_final_contexts = {}
    runtime._settle_admission_final = AsyncMock()
    context = runtime_module._AdmissionFinalContext(
        turn_token=turn_token,
        final_key=final_key,
        epoch=1,
        provider="qwen",
        provider_key=None,
        lifecycle=MagicMock(),
        detector=None,
        correlator=None,
        sealed_token=VoiceTransportToken(turn_token, 1),
        provider_fence=None,
        runtime_identity=MagicMock(),
        has_pending_turn=False,
    )
    runtime._asr_admission_final_contexts[turn_token] = context
    ticket = AdmissionResolutionTicket(
        turn_token,
        1,
        1,
        AdmissionDisposition.FORWARD,
    )
    effect = ResolveReserved(
        ticket,
        PendingProviderFinal(None, "qwen", "hello", 10.0, 10.2),
    )

    await runtime._resolve_admission_reservation(effect)
    await core_entered.wait()

    assert context.settled.is_set()
    execution = runtime._asr_admission_resolutions[final_key]
    assert execution.settled.is_set()
    assert not execution.core_settled

    core_release.set()
    await dispatcher.wait_idle()


@pytest.mark.asyncio
async def test_resolution_effect_runs_before_active_drop_abort_effect() -> None:
    runtime = object.__new__(IndependentAsrRuntime)
    turn_token = VoiceTurnToken(
        VoiceIngressToken(1, "connection", 1, 1, 1),
        1,
    )
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    order: list[object] = []
    resolve_effect = object()
    abort_effect = object()

    async def execute(effect) -> None:
        order.append(effect)
        if effect is resolve_effect:
            first_started.set()
            await first_release.wait()

    runtime._execute_admission_effect = execute
    runtime._asr_admission_effect_tasks = set()
    runtime._asr_admission_effect_task_turns = {}
    runtime._log_asr_background_task_failure = MagicMock()
    runtime._asr_admission_ingress = MagicMock()
    runtime._asr_admission_ingress.retire_turn = AsyncMock(return_value=False)
    future = asyncio.get_running_loop().create_future()
    future.set_result((resolve_effect, abort_effect))

    await runtime._consume_admission_future(
        turn_token,
        future,
        suppress_terminal_errors=False,
    )
    await first_started.wait()
    assert order == [resolve_effect]

    first_release.set()
    pending = tuple(
        task for task in runtime._asr_admission_effect_tasks if not task.done()
    )
    await asyncio.gather(*pending)
    assert order == [resolve_effect, abort_effect]
