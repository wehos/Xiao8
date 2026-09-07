"""Optional metadata observation follows real Admission keys and retirement."""

from unittest.mock import patch

import pytest

from main_logic.asr_client._provider_events import ProviderUtteranceKey, ProviderUtteranceStartedNotification
from main_logic.asr_client.runtime import IndependentAsrRuntime
from main_logic.asr_client.speaker_evidence import EvidenceMode, EvidenceStatus
from tests.unit.asr_client.test_provider_speaker_continuity import _active_real_stack, _close_stack
from tests.unit.asr_client.test_evidence_hold_runtime_integration import _finalize_exact, _unavailable_proof
from tests.unit.asr_client.test_evidence_pipeline_integration import _boundary, _feed, _settle


async def observed_stack(*, hold):
    original_init = IndependentAsrRuntime.__init__

    def configured(self, callbacks, **kwargs):
        original_init(self, callbacks, evidence_hold_enabled=hold, evidence_observation_enabled=True)

    with patch.object(IndependentAsrRuntime, "__init__", configured):
        return await _active_real_stack(score=.95)


@pytest.mark.asyncio
async def test_observer_twenty_actual_held_keys_register_retire_and_reset():
    core, runtime, detector, shadow, lifecycle, session, turn = await observed_stack(hold=True)
    try:
        core.continuity_score_host.ready.set()
        observer = None
        for ordinal in range(1, 21):
            key = ProviderUtteranceKey(0, 0, ordinal)
            await _feed(runtime, turn, (ordinal - 1) * 16 + 1, ordinal * 16)
            transaction = await _finalize_exact(
                runtime, shadow, key, (ordinal - 1) * 25_600, ordinal * 25_600,
                f"synthetic observed key {ordinal}",
            )
            record = await runtime._asr_admission.get_record(transaction.turn_token)
            assert record.evidence_hold is not None
            current = runtime._asr_evidence_observer
            assert current is not None
            assert observer is None or observer is current
            observer = current
            observed = observer._active[key]
            assert observed.binding == record.evidence_hold.binding == transaction.observation_binding
            assert observed.mode is EvidenceMode.OBSERVE
            assert observed.status is EvidenceStatus.UNAVAILABLE
            assert observed.reason == "sequence_unknown"
            assert observer.snapshot()["active"] == 1
            assert core.handle_input_transcript.await_count == ordinal - 1
            assert await runtime._submit_provider_evidence_proof(_unavailable_proof(transaction))
            await _settle(core, runtime)
            assert core.handle_input_transcript.await_count == ordinal
            assert observer.snapshot()["active"] == 0
            assert observer.snapshot()["retired"] == ordinal
            assert transaction.provider_key not in runtime._asr_provider_exact_intervals
        await runtime.stop_session()
        assert runtime._asr_evidence_observer is None
        assert runtime._asr_evidence_observer_scope is None
        assert observer.snapshot()["active"] == 0
        assert observer.snapshot()["retired"] <= 32
        assert runtime._observe_provider_evidence(transaction, transaction.observation_binding, record) is None
        assert runtime._asr_evidence_observer is None
    finally:
        await _close_stack(core)


@pytest.mark.asyncio
async def test_observation_only_never_creates_hold_or_delays_existing_final():
    core, runtime, detector, shadow, lifecycle, session, turn = await observed_stack(hold=False)
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        key = ProviderUtteranceKey(0, 0, 1)
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
        await runtime._handle_provider_final(
            key, "synthetic observation without authority", runtime._asr_session_epoch, "qwen",
        )
        await _settle(core, runtime)
        assert transaction.observation_binding is not None
        assert transaction.evidence_binding is None
        assert transaction.audio_handoff_task is None
        assert not runtime._asr_admission_deadline_tasks
        assert core.handle_input_transcript.await_count == 1
        assert runtime._asr_evidence_observer.snapshot()["active"] == 0
        assert runtime._asr_evidence_observer.snapshot()["retired"] == 1
    finally:
        await _close_stack(core)
