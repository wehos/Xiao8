"""Evidence range regressions through real Runtime/Detector/Shadow/Admission.

Synthetic PCM and a deterministic external scoring host prove engineering
contracts only. Core callbacks are the delivery boundary: remote acceptance,
history persistence, acoustic identity accuracy and UI playback remain unknown.
"""

from __future__ import annotations

import asyncio

import pytest

from main_logic.asr_client._provider_events import (
    ProviderAudioRange,
    ProviderEndpointNotification,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from main_logic.asr_client.admission.contracts import (
    CaptureClosed, SpeakerCheckpointKind, SpeakerHigh, SpeakerLow,
)
from main_logic.voice_turn.contracts import AsrSubmitStatus
from tests.unit.asr_client.test_candidate_rejection_runtime import (
    _drain_runtime_admission,
)
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack,
    _close_stack,
    _submit_pcm,
)


async def _settle(core, runtime):
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()
    await core._voice_input_registry.wait_idle()


def _boundary(key, start, end, phase="boundary"):
    return ProviderEndpointNotification(
        phase=phase,
        generation=key.generation,
        buffer_epoch=key.buffer_epoch,
        utterance_id=key.utterance_id,
        boundary_quality="exact",
        audio_range=ProviderAudioRange(start, end),
    )


async def _feed(runtime, turn, first, last):
    for sequence in range(first, last + 1):
        result = await _submit_pcm(runtime, turn, sequence=sequence)
        assert result.status is AsrSubmitStatus.ACCEPTED
    await runtime._asr_audio_dispatcher.wait_idle()


@pytest.mark.parametrize("score", [0.95, 0.20])
@pytest.mark.parametrize("end", [23_760, 24_000])
async def test_completed_score_keeps_original_range_at_short_exact_boundary(score, end):
    core, runtime, detector, shadow, lifecycle, session, turn = (
        await _active_real_stack(score=score)
    )
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        ledger = runtime._asr_provider_speaker_key_ledgers[key]
        old_score_token = shadow._candidate_tokens[ledger.candidate]
        scored = old_score_token.scored_sample_count
        assert scored == 24_000
        await runtime._handle_provider_endpoint_notification(
            _boundary(key, 0, end), runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        transaction = runtime._asr_provider_exact_intervals[key]
        assert transaction.reservation.source_candidate == ledger.candidate
        assert transaction.reservation.score_reusable is (end >= scored)
        if end < scored:
            assert transaction.target_candidate != ledger.candidate
            assert shadow._finalized[transaction.target_candidate].terminal_reason == "insufficient"
        assert ledger.poisoned_reason is None
        # The saved old score token is not rewritten to fit the shorter text.
        assert old_score_token.scored_sample_count >= scored
        assert detector._provider_audio_sample_cursor_16k == 25_600
        assert sum(len(c.args[0]) // 2 for c in session.stream_audio.await_args_list) == 25_600
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


@pytest.mark.parametrize("score", [0.95, 0.20])
@pytest.mark.parametrize("first_end", [23_760, 20_816])
async def test_short_exact_keeps_preobserved_successor_audio_for_late_anchor(
    score, first_end,
):
    core, runtime, detector, shadow, lifecycle, session, turn = (
        await _active_real_stack(score=score)
    )
    first, second = ProviderUtteranceKey(0, 0, 1), ProviderUtteranceKey(0, 0, 2)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        await runtime._handle_provider_endpoint_notification(
            _boundary(first, 0, first_end), runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        transaction = runtime._asr_provider_exact_intervals[first]
        successor = transaction.successor_evidence_lease
        assert successor is not None
        observed_pcm = b"".join(c.args[0] for c in session.stream_audio.await_args_list)
        assert bytes(shadow._buffers[successor.candidate].pcm16) == observed_pcm[first_end * 2:]
        await runtime._handle_provider_endpoint_notification(
            _boundary(first, 0, first_end, "ordered"), runtime._asr_session_epoch,
        )
        await runtime._handle_provider_final(
            first, "synthetic first short key", runtime._asr_session_epoch, "qwen",
        )
        await _settle(core, runtime)
        assert [c.args[0] for c in core.handle_input_transcript.await_args_list] == [
            "synthetic first short key",
        ]
        await _feed(runtime, turn, 17, 17)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(
                0, 0, 2, audio_start_sample_16k=first_end,
            ), runtime._asr_session_epoch,
        )
        ledger = runtime._asr_provider_speaker_key_ledgers[second]
        assert ledger.anchor_start_sample_16k == first_end, (
            "The successor onset was already observed before the short exact; "
            "retiring unusable old score evidence must preserve that suffix.",
            ledger.poisoned_reason,
        )
        assert ledger.poisoned_reason is None
        assert detector._provider_audio_sample_cursor_16k == 27_200
        assert sum(len(c.args[0]) // 2 for c in session.stream_audio.await_args_list) == 27_200
        successor_turn = runtime._asr_provider_started_turns[second]
        successor_end = first_end + 20_816
        full_chunks, remaining_samples = divmod(successor_end - 27_200, 1_600)
        await _feed(runtime, successor_turn, 18, 17 + full_chunks)
        if remaining_samples:
            assert remaining_samples % 16 == 0
            result = await _submit_pcm(
                runtime, successor_turn, sequence=18 + full_chunks,
                duration_ms=remaining_samples // 16,
            )
            assert result.status is AsrSubmitStatus.ACCEPTED
            await runtime._asr_audio_dispatcher.wait_idle()
        await runtime._handle_provider_endpoint_notification(
            _boundary(second, first_end, successor_end), runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        await runtime._handle_provider_endpoint_notification(
            _boundary(second, first_end, successor_end, "ordered"), runtime._asr_session_epoch,
        )
        await runtime._handle_provider_final(
            second, "synthetic second short key", runtime._asr_session_epoch, "qwen",
        )
        await _settle(core, runtime)
        await runtime._handle_provider_final(
            first, "synthetic stale duplicate", runtime._asr_session_epoch, "qwen",
        )
        await _settle(core, runtime)
        expected = ["synthetic first short key", "synthetic second short key"]
        assert [c.args[0] for c in core.handle_input_transcript.await_args_list] == expected
        assert [c.args[0] for c in core.session.create_response.await_args_list] == expected
        assert detector._provider_audio_sample_cursor_16k == successor_end
        assert sum(len(c.args[0]) // 2 for c in session.stream_audio.await_args_list) == successor_end
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_independent_1301ms_sentence_remains_unavailable_without_fabricated_score():
    core, runtime, detector, shadow, lifecycle, session, turn = (
        await _active_real_stack(score=0.95)
    )
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 13)
        result = await _submit_pcm(runtime, turn, sequence=14, duration_ms=1)
        assert result.status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        await runtime._handle_provider_endpoint_notification(
            _boundary(key, 0, 20_816), runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        transaction = runtime._asr_provider_exact_intervals[key]
        await runtime._handle_provider_endpoint_notification(
            _boundary(key, 0, 20_816, "ordered"), runtime._asr_session_epoch,
        )
        await runtime._handle_provider_final(
            key, "synthetic independent short sentence", runtime._asr_session_epoch, "qwen",
        )
        await _settle(core, runtime)
        assert transaction.resolved_disposition.value == "forward"
        assert core.continuity_score_host.calls == 0
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
        assert detector._provider_audio_sample_cursor_16k == 20_816
        assert sum(len(c.args[0]) // 2 for c in session.stream_audio.await_args_list) == 20_816
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


@pytest.mark.parametrize("late_low", [False, True])
async def test_late_source_score_and_close_cannot_authorize_or_abort_fresh_exact(late_low):
    core, runtime, detector, shadow, lifecycle, session, turn = (
        await _active_real_stack(score=0.95)
    )
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        source = runtime._asr_provider_speaker_key_ledgers[key].candidate
        await runtime._handle_provider_endpoint_notification(
            _boundary(key, 0, 23_760), runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        transaction = runtime._asr_provider_exact_intervals[key]
        successor = transaction.successor_evidence_lease
        assert transaction.target_candidate != source
        fact = (
            SpeakerLow(source, 2, SpeakerCheckpointKind.COMPLETION_CONFIRMATION)
            if late_low else SpeakerHigh(source, 2)
        )
        # External ordered callbacks may finish after the PCM split. These
        # still name the old source and are not facts about the new target.
        runtime._accept_speaker_evidence_fact(
            fact, activation_generation="continuity-test", enforce=True,
        )
        runtime._close_speaker_evidence(
            CaptureClosed(source, 2), activation_generation="continuity-test",
            enforce=True, evidence_complete=True,
        )
        await _settle(core, runtime)
        assert not transaction.queue_poisoned
        assert runtime._asr_provider_speaker_evidence_lease is successor
        assert shadow._finalized[transaction.target_candidate].terminal_reason == "insufficient"
        await runtime._handle_provider_endpoint_notification(
            _boundary(key, 0, 23_760, "ordered"), runtime._asr_session_epoch,
        )
        await runtime._handle_provider_final(
            key, "synthetic short after late source", runtime._asr_session_epoch, "qwen",
        )
        await _settle(core, runtime)
        assert [c.args[0] for c in core.handle_input_transcript.await_args_list] == [
            "synthetic short after late source",
        ]
        assert core.session.create_response.await_count == 1
        assert runtime._asr_provider_speaker_evidence_lease is successor
        session.close.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_commit_publishes_audio_watermark_that_arrived_after_prepare(monkeypatch):
    core, runtime, detector, shadow, lifecycle, session, turn = (
        await _active_real_stack(score=0.95)
    )
    prepared, commit_allowed = asyncio.Event(), asyncio.Event()
    real_prepare = detector.prepare_provider_exact_speaker_interval
    task = None

    async def pause_after_real_prepare(*args, **kwargs):
        reservation = await real_prepare(*args, **kwargs)
        assert reservation is not None
        prepared.set()
        await commit_allowed.wait()
        return reservation

    # Scheduling barrier only: the actual prepare, reservation, commit,
    # observation receipts and dispatcher sends all execute unchanged.
    monkeypatch.setattr(detector, "prepare_provider_exact_speaker_interval", pause_after_real_prepare)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        key = ProviderUtteranceKey(0, 0, 1)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        await shadow.wait_idle()
        task = asyncio.create_task(runtime._handle_provider_endpoint_notification(
            _boundary(key, 0, 25_600), runtime._asr_session_epoch,
        ))
        await asyncio.wait_for(prepared.wait(), 1)
        await asyncio.wait_for(_feed(runtime, turn, 17, 17), 1)
        commit_allowed.set()
        await asyncio.wait_for(task, 1)
        transaction = runtime._asr_provider_exact_intervals[key]
        assert transaction.reservation.provider_pcm_through_sequence_no == 16
        successor = transaction.successor_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[successor.candidate]
        assert ledger.last_pcm_sequence_no == 17
        await _feed(runtime, turn, 18, 18)
        assert ledger.last_pcm_sequence_no == 18
        assert ledger.poisoned_reason is None
        assert detector._provider_audio_sample_cursor_16k == 28_800
        assert sum(len(c.args[0]) // 2 for c in session.stream_audio.await_args_list) == 28_800
        session.close.assert_not_awaited()
    finally:
        commit_allowed.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await _close_stack(core)


async def test_twenty_exact_keys_have_single_ordered_disposition_and_bounded_resources():
    core, runtime, detector, shadow, lifecycle, session, turn = (
        await _active_real_stack(score=0.95)
    )
    expected = []
    generations = []
    try:
        core.continuity_score_host.ready.set()
        for utterance in range(1, 21):
            key = ProviderUtteranceKey(0, 0, utterance)
            start, end = (utterance - 1) * 25_600, utterance * 25_600
            await _feed(runtime, turn, (utterance - 1) * 16 + 1, utterance * 16)
            assert await runtime._handle_provider_utterance_started(
                ProviderUtteranceStartedNotification(
                    0, 0, utterance, audio_start_sample_16k=start,
                ), runtime._asr_session_epoch,
            )
            turn = runtime._asr_provider_started_turns[key]
            await runtime._handle_provider_endpoint_notification(
                _boundary(key, start, end), runtime._asr_session_epoch,
            )
            transaction = runtime._asr_provider_exact_intervals[key]
            generations.append(transaction.snapshot.candidate_generation)
            await shadow.wait_idle()
            await runtime._handle_provider_endpoint_notification(
                _boundary(key, start, end, "ordered"), runtime._asr_session_epoch,
            )
            text = f"synthetic provider key {key.generation}:{key.buffer_epoch}:{key.utterance_id}"
            expected.append(text)
            await runtime._handle_provider_final(key, text, runtime._asr_session_epoch, "qwen")
            await _settle(core, runtime)
            await runtime._handle_provider_final(
                key, "synthetic duplicate must not dispatch", runtime._asr_session_epoch, "qwen",
            )
            await _settle(core, runtime)
            assert [c.args[0] for c in core.handle_input_transcript.await_args_list] == expected
            assert [c.args[0] for c in core.session.create_response.await_args_list] == expected
            assert len(runtime._asr_provider_exact_intervals) <= 8
            assert shadow.snapshot()["retained_pcm_bytes"] < 8 * 1024 * 1024
            assert detector._provider_audio_sample_cursor_16k == end
            assert sum(len(c.args[0]) // 2 for c in session.stream_audio.await_args_list) == end
            session.close.assert_not_awaited()
        assert len(set(generations)) == 20
    finally:
        await _close_stack(core)
