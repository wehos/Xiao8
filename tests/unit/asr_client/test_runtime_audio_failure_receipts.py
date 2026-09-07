"""Invalid accounting receipts remain blocked and identify the failed check."""

from dataclasses import replace

import pytest

from main_logic.asr_client.endpointing.detector_runtime import ProviderAudioAccountingReceipt
from main_logic.asr_client._provider_events import ProviderUtteranceStartedNotification
from main_logic.voice_turn.contracts import AsrSubmitStatus
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack, _close_stack, _submit_pcm,
)


@pytest.mark.parametrize("field,check", [
    ("type", "accounting_receipt_type_invalid"),
    ("detector_epoch", "accounting_receipt_detector_mismatch"),
    ("timeline_generation", "accounting_receipt_timeline_mismatch"),
    ("sequence_no", "accounting_receipt_sequence_mismatch"),
    ("end_sample_16k", "accounting_receipt_samples_mismatch"),
])
async def test_corrupt_receipt_after_real_retirement_is_blocked_without_replay(monkeypatch, field, check):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack()
    try:
        assert (await _submit_pcm(runtime, turn, sequence=1)).status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        runtime._poison_provider_speaker_ledger(ledger, "speaker_capture_unavailable")
        observe = detector.observe_provider_audio_ordered
        actual_receipts = []

        async def corrupted(*args, **kwargs):
            receipt = await observe(*args, **kwargs)
            assert type(receipt) is ProviderAudioAccountingReceipt
            actual_receipts.append(receipt)
            if field == "type":
                return None
            return replace(receipt, **{field: getattr(receipt, field) + 1})

        incidents = []
        monkeypatch.setattr(detector, "observe_provider_audio_ordered", corrupted)
        monkeypatch.setattr(runtime, "_schedule_asr_incident_log", lambda **kwargs: incidents.append(kwargs))
        result = await _submit_pcm(runtime, turn, sequence=2)
        assert result.status is AsrSubmitStatus.UNAVAILABLE
        assert len(actual_receipts) == len(incidents) == 1
        facts = incidents[0]["failure_context"].snapshot()
        assert facts["failed_operation"] == "submit"
        assert facts["failed_check"] == check
        if field == "sequence_no":
            assert facts["expected"]["sequence_no"] == 2
            assert facts["actual"]["sequence_no"] == 3
        assert runtime._asr_session is None
        assert detector._provider_speaker_evidence_state_for(evidence) is None
        assert session.stream_audio.await_count == 1
        assert len(session.stream_audio.await_args_list[0].args[0]) // 2 == 1600
    finally:
        await _close_stack(core)
