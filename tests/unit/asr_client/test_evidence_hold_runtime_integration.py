"""Opt-in hold contracts exercised through actual capture and final delivery."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

import pytest

from main_logic.asr_client.runtime import IndependentAsrRuntime
from main_logic.asr_client._provider_events import (
    ProviderUtteranceKey, ProviderUtteranceStartedNotification,
)
from main_logic.asr_client.speaker_evidence import EvidenceMode, EvidenceStatus, evaluate_coverage
from tests.unit.asr_client.test_candidate_rejection_runtime import _drain_runtime_admission
from tests.unit.asr_client.test_provider_speaker_continuity import _active_real_stack, _close_stack
from tests.unit.asr_client.test_evidence_pipeline_integration import _boundary, _feed, _settle


async def _enabled_stack():
    original_init = IndependentAsrRuntime.__init__

    def configured_init(self, callbacks, **kwargs):
        return original_init(self, callbacks, evidence_hold_enabled=True)

    # Only configuration is injected. Runtime construction, Coordinator,
    # Detector, Shadow, capture, final registration and settlement stay real.
    with patch.object(IndependentAsrRuntime, "__init__", configured_init):
        return await _active_real_stack(score=.95)


async def test_held_final_releases_capture_and_duplicate_cannot_extend_timeout():
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    first = ProviderUtteranceKey(0, 0, 1)
    second = ProviderUtteranceKey(0, 0, 2)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        await runtime._handle_provider_endpoint_notification(
            _boundary(first, 0, 20_816), runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        transaction = runtime._asr_provider_exact_intervals[first]
        await runtime._handle_provider_endpoint_notification(
            _boundary(first, 0, 20_816, "ordered"), runtime._asr_session_epoch,
        )
        await runtime._handle_provider_final(
            first, "synthetic held first", runtime._asr_session_epoch, "qwen",
        )
        await _drain_runtime_admission(runtime)
        record = await runtime._asr_admission.get_record(transaction.turn_token)
        assert record.evidence_hold is not None, "real final path must register the hold"
        hold = record.evidence_hold
        assert hold.status is EvidenceStatus.PENDING
        assert hold.absolute_deadline == hold.first_final_received_at + 2
        assert core.handle_input_transcript.await_count == 0
        successor = transaction.successor_evidence_lease
        assert runtime._asr_provider_speaker_evidence_lease is successor
        await _feed(runtime, turn, 17, 17)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 2, audio_start_sample_16k=20_816),
            runtime._asr_session_epoch,
        )
        successor_turn = runtime._asr_provider_started_turns[second]
        await _feed(runtime, successor_turn, 18, 18)
        assert detector._provider_audio_sample_cursor_16k == 28_800
        assert sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list) == 28_800
        assert core.handle_input_transcript.await_count == 0
        await runtime._handle_provider_final(
            first, "synthetic duplicate held first", runtime._asr_session_epoch, "qwen",
        )
        record = await runtime._asr_admission.get_record(transaction.turn_token)
        assert record.evidence_hold.ticket == hold.ticket
        assert record.evidence_hold.absolute_deadline == hold.absolute_deadline
        assert runtime._asr_provider_speaker_evidence_lease is successor
        # Wait for the real absolute timer, without synthesizing expiry events.
        async with asyncio.timeout(3):
            while core.handle_input_transcript.await_count == 0:
                await asyncio.sleep(.01)
        await _settle(core, runtime)
        assert [call.args[0] for call in core.handle_input_transcript.await_args_list] == [
            "synthetic held first",
        ]
        assert [call.args[0] for call in core.session.create_response.await_args_list] == [
            "synthetic held first",
        ]
        assert runtime._asr_provider_speaker_evidence_lease is successor
        assert runtime._asr_provider_started_turns[second] == successor_turn
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


async def _finalize_exact(runtime, shadow, key, start, end, text):
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            key.generation, key.buffer_epoch, key.utterance_id, audio_start_sample_16k=start,
        ), runtime._asr_session_epoch,
    )
    await runtime._handle_provider_endpoint_notification(
        _boundary(key, start, end), runtime._asr_session_epoch,
    )
    await shadow.wait_idle()
    transaction = runtime._asr_provider_exact_intervals[key]
    await runtime._handle_provider_endpoint_notification(
        _boundary(key, start, end, "ordered"), runtime._asr_session_epoch,
    )
    await runtime._handle_provider_final(key, text, runtime._asr_session_epoch, "qwen")
    await _drain_runtime_admission(runtime)
    assert transaction.evidence_binding is not None
    return transaction


def _unavailable_proof(transaction):
    # Explicit test producer reports no usable score. This exercises only the
    # existing unavailable policy and grants no acoustic identity authority.
    return evaluate_coverage(transaction.evidence_binding, (), mode=EvidenceMode.AUTHORITATIVE)


async def test_second_ready_key_waits_for_first_and_old_resolution_keeps_new_owner():
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    first, second = ProviderUtteranceKey(0, 0, 1), ProviderUtteranceKey(0, 0, 2)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        first_tx = await _finalize_exact(runtime, shadow, first, 0, 25_600, "synthetic key one")
        await _feed(runtime, turn, 17, 32)
        second_tx = await _finalize_exact(
            runtime, shadow, second, 25_600, 51_200, "synthetic key two",
        )
        successor = second_tx.successor_evidence_lease
        assert runtime._asr_provider_speaker_evidence_lease is successor
        assert core.handle_input_transcript.await_count == 0
        assert await runtime._submit_provider_evidence_proof(_unavailable_proof(second_tx))
        await _drain_runtime_admission(runtime)
        assert core.handle_input_transcript.await_count == 0
        second_record = await runtime._asr_admission.get_record(second_tx.turn_token)
        assert second_record.evidence_hold.status is EvidenceStatus.UNAVAILABLE
        assert await runtime._submit_provider_evidence_proof(_unavailable_proof(first_tx))
        await _settle(core, runtime)
        assert [call.args[0] for call in core.handle_input_transcript.await_args_list] == [
            "synthetic key one", "synthetic key two",
        ]
        assert [call.args[0] for call in core.session.create_response.await_args_list] == [
            "synthetic key one", "synthetic key two",
        ]
        assert runtime._asr_provider_speaker_evidence_lease is successor
        await runtime._submit_provider_evidence_proof(_unavailable_proof(first_tx))
        await runtime._handle_provider_final(
            first, "synthetic old duplicate", runtime._asr_session_epoch, "qwen",
        )
        await _settle(core, runtime)
        assert core.handle_input_transcript.await_count == 2
        assert runtime._asr_provider_speaker_evidence_lease is successor
        assert detector._provider_audio_sample_cursor_16k == 51_200
        assert sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list) == 51_200
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_twenty_held_finals_settle_once_with_bounded_retention():
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    expected = []
    generations = []
    try:
        core.continuity_score_host.ready.set()
        for ordinal in range(1, 21):
            key = ProviderUtteranceKey(0, 0, ordinal)
            await _feed(runtime, turn, (ordinal - 1) * 16 + 1, ordinal * 16)
            text = f"synthetic held key {ordinal}"
            transaction = await _finalize_exact(
                runtime, shadow, key, (ordinal - 1) * 25_600, ordinal * 25_600, text,
            )
            generations.append(transaction.snapshot.candidate_generation)
            assert [call.args[0] for call in core.handle_input_transcript.await_args_list] == expected
            assert await runtime._submit_provider_evidence_proof(_unavailable_proof(transaction))
            expected.append(text)
            await _settle(core, runtime)
            assert [call.args[0] for call in core.handle_input_transcript.await_args_list] == expected
            assert [call.args[0] for call in core.session.create_response.await_args_list] == expected
            assert len(runtime._asr_provider_exact_intervals) <= 8
            assert shadow.snapshot()["retained_pcm_bytes"] < 8 * 1024 * 1024
            assert detector._provider_audio_sample_cursor_16k == ordinal * 25_600
            session.close.assert_not_awaited()
        assert len(set(generations)) == 20
        assert sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list) == 512_000
    finally:
        await _close_stack(core)


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_final_preserves_owned_audio_handoff_and_single_delivery(monkeypatch, cancel_count):
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    key = ProviderUtteranceKey(0, 0, 1)
    entered, release = asyncio.Event(), asyncio.Event()
    actual_complete = detector.complete_provider_speaker_boundary
    final = None

    async def pause_after_actual_complete(*args, **kwargs):
        result = await actual_complete(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(detector, "complete_provider_speaker_boundary", pause_after_actual_complete)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        await runtime._handle_provider_endpoint_notification(
            _boundary(key, 0, 25_600), runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        transaction = runtime._asr_provider_exact_intervals[key]
        await runtime._handle_provider_endpoint_notification(
            _boundary(key, 0, 25_600, "ordered"), runtime._asr_session_epoch,
        )
        final = asyncio.create_task(runtime._handle_provider_final(
            key, "synthetic accepted before caller cancel", runtime._asr_session_epoch, "qwen",
        ))
        await asyncio.wait_for(entered.wait(), .5)
        for _ in range(cancel_count):
            final.cancel()
            await asyncio.sleep(0)
        done, _ = await asyncio.wait({final}, timeout=.2)
        assert final in done
        with pytest.raises(asyncio.CancelledError):
            final.result()
        assert not transaction.audio_handoff_task.done()
        assert transaction.audio_handoff_task in runtime._asr_admission_effect_tasks
        release.set()
        assert await asyncio.wait_for(transaction.audio_handoff_task, .5)
        successor = transaction.successor_evidence_lease
        assert runtime._asr_provider_speaker_evidence_lease is successor
        await _feed(runtime, turn, 17, 17)
        assert detector._provider_audio_sample_cursor_16k == 27_200
        assert sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list) == 27_200
        assert await runtime._submit_provider_evidence_proof(_unavailable_proof(transaction))
        await _settle(core, runtime)
        assert [call.args[0] for call in core.handle_input_transcript.await_args_list] == [
            "synthetic accepted before caller cancel",
        ]
        assert runtime._asr_provider_speaker_evidence_lease is successor
        session.close.assert_not_awaited()
    finally:
        release.set()
        if final is not None:
            await asyncio.gather(final, return_exceptions=True)
        await _close_stack(core)


async def test_stop_retires_held_final_and_late_proof_cannot_restore_it():
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        transaction = await _finalize_exact(
            runtime, shadow, key, 0, 25_600, "synthetic held before stop",
        )
        proof = _unavailable_proof(transaction)
        assert core.handle_input_transcript.await_count == 0
        await asyncio.wait_for(runtime.stop_session(), 2)
        assert not await runtime._submit_provider_evidence_proof(proof)
        await runtime._handle_provider_final(
            key, "synthetic stale after stop", transaction.runtime_identity.session_epoch, "qwen",
        )
        await _drain_runtime_admission(runtime)
        assert core.handle_input_transcript.await_count == 0
        assert core.session.create_response.await_count == 0
        assert not runtime._asr_provider_exact_intervals
        assert not runtime._asr_admission_deadline_tasks
        assert not runtime._asr_admission_effect_tasks
        session.close.assert_awaited_once()
    finally:
        await _close_stack(core)


async def test_handoff_timeout_closes_transport_and_failure_callback_can_stop(monkeypatch):
    from main_logic.asr_client import runtime as runtime_module
    monkeypatch.setattr(runtime_module, "_EXACT_PENDING_HANDOFF_TIMEOUT_SECONDS", .03)
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    key = ProviderUtteranceKey(0, 0, 1)
    actual_complete = detector.complete_provider_speaker_boundary
    stop_completed = asyncio.Event()

    async def never_return_after_actual_complete(*args, **kwargs):
        await actual_complete(*args, **kwargs)
        await asyncio.Event().wait()

    async def stop_from_failure(*args):
        await runtime.stop_session()
        stop_completed.set()

    monkeypatch.setattr(detector, "complete_provider_speaker_boundary", never_return_after_actual_complete)
    runtime._callbacks = replace(runtime._callbacks, on_failure=stop_from_failure)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_finalize_exact(
                runtime, shadow, key, 0, 25_600, "synthetic timed out handoff",
            ), .5)
        await asyncio.wait_for(stop_completed.wait(), 2)
        assert runtime._asr_session is None
        session.close.assert_awaited_once()
        assert core.handle_input_transcript.await_count == 0
        assert sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list) == 25_600
    finally:
        await _close_stack(core)
