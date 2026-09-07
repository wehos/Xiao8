"""Physical evidence retirement never rehabilitates an unavailable Provider turn."""

from dataclasses import replace
import asyncio

import pytest

from main_logic.asr_client._provider_events import (
    ProviderEndpointNotification,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from main_logic.asr_client.runtime import _SpeakerArmingStatus
from main_logic.asr_client.admission.contracts import AdmissionDisposition
from main_logic.asr_client.lifecycle import FinalKey
from main_logic.voice_turn.contracts import AsrSubmitStatus
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack,
    _close_stack,
    _submit_pcm,
)


async def test_accounting_retires_current_aliases_but_keeps_turn_unavailable():
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack()
    try:
        assert (await _submit_pcm(runtime, turn, sequence=1)).status is AsrSubmitStatus.ACCEPTED
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        logical_lease = ledger.lease_token
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        assert (await _submit_pcm(runtime, turn, sequence=2)).status is AsrSubmitStatus.ACCEPTED
        assert detector._provider_speaker_evidence_state_for(evidence) is None
        assert runtime._asr_provider_speaker_evidence_lease is None
        assert runtime._asr_current_speaker_candidate is None
        assert runtime._asr_current_speaker_lease is None
        assert runtime._asr_admission_turn_leases[turn] == logical_lease
        assert runtime._asr_provider_speaker_key_ledgers[ledger.provider_key] is ledger
        for sequence in (3, 4, 5):
            assert (await _submit_pcm(runtime, turn, sequence=sequence)).status is AsrSubmitStatus.ACCEPTED
            assert runtime._asr_provider_speaker_evidence_lease is None
            assert detector._provider_speaker_evidence_state is None
        assert ledger.poisoned_reason == "speaker_capture_unavailable"
        await runtime._asr_audio_dispatcher.wait_idle()
        assert sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list) == 8000
    finally:
        await _close_stack(core)


async def test_cancelled_retirement_confirmation_can_be_retried_without_pcm(monkeypatch):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack()
    try:
        assert (await _submit_pcm(runtime, turn, sequence=1)).status is AsrSubmitStatus.ACCEPTED
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        await detector.abandon_provider_speaker_evidence_lease(evidence)
        confirmed = asyncio.Event()
        confirm = detector.confirm_provider_speaker_evidence_retirement

        async def pause_after_confirmation(lease):
            result = await confirm(lease)
            confirmed.set()
            await asyncio.Event().wait()
            return result

        identity = runtime._capture_runtime_identity(ingress_token=turn.ingress)
        kwargs = dict(
            detector=detector, identity=identity,
            owner_generation=runtime._speaker_verifier_activation_generation,
            turn_token=turn,
        )
        monkeypatch.setattr(detector, "confirm_provider_speaker_evidence_retirement", pause_after_confirmation)
        task = asyncio.create_task(runtime._confirm_provider_speaker_evidence_retirement(evidence, **kwargs))
        await asyncio.wait_for(confirmed.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime._asr_provider_speaker_evidence_lease is evidence
        monkeypatch.setattr(detector, "confirm_provider_speaker_evidence_retirement", confirm)
        assert await runtime._confirm_provider_speaker_evidence_retirement(evidence, **kwargs)
        assert runtime._asr_provider_speaker_evidence_lease is None
        assert (await runtime._arm_speaker_authority_for_provider_audio(turn)).status is _SpeakerArmingStatus.EVIDENCE_UNAVAILABLE
        await runtime._asr_audio_dispatcher.wait_idle()
        assert session.stream_audio.await_count == 1
        assert detector._provider_audio_sample_cursor_16k == 1600
    finally:
        await _close_stack(core)


async def test_unavailable_pending_successor_anchor_survives_old_final():
    from tests.unit.asr_client.test_asr_incident_recovery import _settle_deliveries

    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack()
    try:
        assert (await _submit_pcm(runtime, turn, sequence=1)).status is AsrSubmitStatus.ACCEPTED
        old_key = ProviderUtteranceKey(0, 0, 1)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0), runtime._asr_session_epoch
        )
        ledger = runtime._asr_provider_speaker_key_ledgers[old_key]
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        assert (await _submit_pcm(runtime, turn, sequence=2)).status is AsrSubmitStatus.ACCEPTED
        assert runtime._asr_provider_speaker_evidence_lease is None
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification("ordered", 0, 0, 1, "unknown", None), runtime._asr_session_epoch
        )
        assert (await _submit_pcm(runtime, turn, sequence=3)).status is AsrSubmitStatus.ACCEPTED
        next_key = ProviderUtteranceKey(0, 0, 2)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 2, audio_start_sample_16k=3200), runtime._asr_session_epoch
        )
        successor = runtime._asr_provider_speaker_evidence_lease
        assert successor is not None and successor is not ledger.evidence_lease
        next_turn = runtime._asr_provider_started_turns[next_key]
        assert next_turn == lifecycle.pending_turn_token
        assert runtime._asr_provider_speaker_key_ledgers[next_key].turn_token == next_turn
        await runtime._handle_provider_final(old_key, "old degraded", runtime._asr_session_epoch, "qwen")
        await _settle_deliveries(core, runtime)
        assert runtime._asr_provider_speaker_evidence_lease is successor
        assert detector._provider_speaker_evidence_state_for(successor) is not None
        for sequence in range(4, 8):
            assert (await _submit_pcm(runtime, next_turn, sequence=sequence)).status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()
        assert sum(len(call.args[0]) // 2 for call in session.stream_audio.await_args_list) == 11200
        assert detector._provider_audio_sample_cursor_16k == 11200
        assert runtime._asr_provider_speaker_key_ledgers[next_key].poisoned_reason is None
    finally:
        await _close_stack(core)


@pytest.mark.parametrize("takeover", ["pending_discarded", "activation_replaced"])
async def test_pending_arming_cannot_adopt_after_owner_changes(monkeypatch, takeover):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack()
    try:
        assert (await _submit_pcm(runtime, turn, sequence=1)).status is AsrSubmitStatus.ACCEPTED
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0), runtime._asr_session_epoch
        )
        ledger = runtime._asr_provider_speaker_key_ledgers[ProviderUtteranceKey(0, 0, 1)]
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        assert (await _submit_pcm(runtime, turn, sequence=2)).status is AsrSubmitStatus.ACCEPTED
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification("ordered", 0, 0, 1, "unknown", None), runtime._asr_session_epoch
        )
        assert (await _submit_pcm(runtime, turn, sequence=3)).status is AsrSubmitStatus.ACCEPTED
        created = asyncio.Event()
        release = asyncio.Event()
        ensure = detector.ensure_provider_speaker_evidence_lease
        acquired = []

        async def pause_after_ensure():
            lease = await ensure()
            acquired.append(lease)
            created.set()
            await release.wait()
            return lease

        monkeypatch.setattr(detector, "ensure_provider_speaker_evidence_lease", pause_after_ensure)
        task = asyncio.create_task(runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 2, audio_start_sample_16k=3200),
            runtime._asr_session_epoch,
        ))
        await asyncio.wait_for(created.wait(), timeout=1)
        pending = lifecycle.pending_turn_token
        assert pending is not None
        if takeover == "pending_discarded":
            lifecycle.discard_pending_turn()
        else:
            runtime._speaker_verifier_activation_generation = "replacement"
        release.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert result is False
        assert runtime._asr_provider_speaker_evidence_lease is None
        assert runtime._asr_current_speaker_candidate is None
        assert detector._provider_speaker_evidence_state_for(acquired[0]) is None
    finally:
        await _close_stack(core)


async def test_final_without_speaker_ledger_has_no_right_to_finish_successor(monkeypatch):
    from tests.unit.asr_client.test_asr_incident_recovery import _settle_deliveries

    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack()
    try:
        # Speaker capture was physically unavailable when the old turn started.
        # Recovery must not give that final rights over the new capture.
        detector._speaker_shadow = None
        assert (await _submit_pcm(runtime, turn, sequence=1)).status is AsrSubmitStatus.ACCEPTED
        old_key = ProviderUtteranceKey(0, 0, 1)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0), runtime._asr_session_epoch
        )
        assert old_key not in runtime._asr_provider_speaker_key_ledgers
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification("ordered", 0, 0, 1, "unknown", None), runtime._asr_session_epoch
        )
        detector._speaker_shadow = shadow
        assert (await _submit_pcm(runtime, turn, sequence=2)).status is AsrSubmitStatus.ACCEPTED
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 2, audio_start_sample_16k=1600), runtime._asr_session_epoch
        )
        successor = runtime._asr_provider_speaker_evidence_lease
        assert successor is not None
        finished = []
        finish = detector.finish_provider_speaker_evidence_lease

        async def record_finish(lease):
            finished.append(lease)
            return await finish(lease)

        monkeypatch.setattr(detector, "finish_provider_speaker_evidence_lease", record_finish)
        await runtime._handle_provider_final(old_key, "old unverified", runtime._asr_session_epoch, "qwen")
        await _settle_deliveries(core, runtime)
        assert not any(lease is successor for lease in finished)
        assert runtime._asr_provider_speaker_evidence_lease is successor
        assert detector._provider_speaker_evidence_state_for(successor) is not None
        assert runtime._asr_transcript_dispatcher._resolved[FinalKey.from_turn(turn)] is AdmissionDisposition.ABANDON
        core.handle_input_transcript.assert_not_awaited()
    finally:
        await _close_stack(core)


async def test_forged_retirement_cannot_authorize_accounting_audio(monkeypatch):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack()
    try:
        assert (await _submit_pcm(runtime, turn, sequence=1)).status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        observe = detector.observe_provider_audio_ordered

        async def forge(*args, **kwargs):
            receipt = await observe(*args, **kwargs)
            return replace(receipt, evidence_settlement=replace(receipt.evidence_settlement))

        monkeypatch.setattr(detector, "observe_provider_audio_ordered", forge)
        result = await _submit_pcm(runtime, turn, sequence=2)
        assert result.status is AsrSubmitStatus.UNAVAILABLE
        assert not runtime._ingress_token_matches(turn.ingress)
        assert session.stream_audio.await_count == 1
    finally:
        await _close_stack(core)
