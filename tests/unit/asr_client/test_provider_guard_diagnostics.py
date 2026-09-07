"""Boundary rejection explains the first failed invariant without changing policy."""

from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client import runtime as runtime_module
from main_logic.asr_client._provider_events import (
    ProviderAudioRange, ProviderEndpointNotification, ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack, _close_stack, _submit_pcm,
)
from tests.unit.asr_client.test_short_speaker_diagnostics import _interval, _join_logs


CHECKS = (
    "missing_started_turn", "missing_admission_lease", "missing_evidence_lease",
    "missing_ledger", "evidence_lease_mismatch", "turn_mismatch", "provider_key_mismatch",
    "ledger_not_anchored_scoring", "ledger_poisoned", "missing_lifecycle",
    "missing_ingress", "ingress_mismatch", "boundary_not_exact",
    "anchor_start_mismatch", "exact_session_mismatch", "boundary_capacity_exhausted",
)


@pytest.mark.parametrize("check", CHECKS)
async def test_each_boundary_guard_records_original_state(monkeypatch, check):
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, data: logs.append(data))
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.95)
    core.continuity_score_host.ready.set()
    try:
        for sequence in range(1, 9):
            await _submit_pcm(runtime, turn, sequence=sequence)
        await shadow.wait_idle()
        epoch = runtime._asr_session_epoch
        key = ProviderUtteranceKey(0, 0, 1)
        await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0), epoch,
        )
        await shadow.wait_idle()
        await _join_logs(runtime)
        ledger = runtime._asr_provider_speaker_key_ledgers[key]
        boundary = ProviderEndpointNotification("boundary", 0, 0, 1, "exact", ProviderAudioRange(0, 12800))
        # The table isolates the admission precondition; real failure settlement
        # and Core delivery are exercised separately below.
        with monkeypatch.context() as patch:
            record = AsyncMock()
            patch.setattr(runtime, "_record_provider_boundary_result", record)
            patch.setattr(runtime, "_publish_provider_ledger_unavailable", AsyncMock())
            if check == "missing_started_turn":
                patch.setattr(runtime, "_asr_provider_started_turns", {})
            elif check == "missing_admission_lease":
                patch.setattr(runtime, "_asr_admission_turn_leases", {})
            elif check == "missing_evidence_lease":
                patch.setattr(runtime, "_asr_provider_speaker_evidence_lease", None)
            elif check == "missing_ledger":
                patch.setattr(runtime, "_asr_provider_speaker_key_ledgers", {})
            elif check == "evidence_lease_mismatch":
                patch.setattr(ledger, "evidence_lease", replace(ledger.evidence_lease, lease_generation=999))
            elif check == "turn_mismatch":
                patch.setattr(ledger, "turn_token", replace(ledger.turn_token, turn_id=999))
            elif check == "provider_key_mismatch":
                patch.setattr(ledger, "provider_key", ProviderUtteranceKey(0, 0, 999))
            elif check == "ledger_not_anchored_scoring":
                patch.setattr(ledger, "state", runtime_module._ProviderSpeakerLedgerState.UNAVAILABLE)
            elif check == "ledger_poisoned":
                patch.setattr(ledger, "poisoned_reason", "PRIVATE_ERROR")
            elif check == "missing_lifecycle":
                patch.setattr(runtime, "_asr_lifecycle", None)
            elif check == "missing_ingress":
                patch.setattr(runtime, "_asr_current_ingress_token", None)
            elif check == "ingress_mismatch":
                patch.setattr(runtime, "_asr_current_ingress_token", replace(turn.ingress, session_epoch=999))
            elif check == "boundary_not_exact":
                boundary = replace(boundary, boundary_quality="unknown", audio_range=None)
            elif check == "anchor_start_mismatch":
                boundary = replace(boundary, audio_range=ProviderAudioRange(1, 12800))
            elif check == "exact_session_mismatch":
                patch.setattr(runtime, "_asr_provider_exact_session", object())
            elif check == "boundary_capacity_exhausted":
                patch.setattr(runtime_module, "_MAX_PROVIDER_BOUNDARY_SNAPSHOTS", 0)
            original_state = ledger.state.value
            await runtime._handle_provider_boundary_notification(boundary, epoch)
            await _join_logs(runtime)
            record.assert_awaited_once()
            assert record.call_args.kwargs["result"].quality == "unknown"
        records = [item for item in logs if item["stage"] == "provider_boundary_guard_failed"]
        assert len(records) == 1
        diagnostic = records[0]
        assert diagnostic["failed_check"] == check
        assert diagnostic["provider_utterance_id"] == 1
        assert diagnostic["ledger_state"] == (None if check == "missing_ledger" else original_state)
        if check == "anchor_start_mismatch":
            assert diagnostic["anchor_start_sample_16k"] == 0
            assert diagnostic["provider_start_sample_16k"] == 1
        assert "PRIVATE" not in json.dumps(logs)
        assert all(type(v) in (str, int, bool, type(None)) for item in logs for v in item.values())
        assert not runtime._asr_provider_exact_pending
    finally:
        await _close_stack(core)


def test_exact_notification_without_range_is_rejected_before_runtime():
    with pytest.raises(ValueError, match="ASR_PROVIDER_ENDPOINT_BOUNDARY_INVALID"):
        ProviderEndpointNotification("boundary", 0, 0, 1, "exact", None)


async def test_guard_diagnostic_cannot_borrow_successor_session(monkeypatch):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.95)
    logs = []
    monkeypatch.setattr(runtime, "_schedule_asr_diagnostic_metadata", lambda data, **kw: logs.append(data))
    try:
        runtime._schedule_provider_guard_diagnostic(
            ProviderUtteranceKey(0, 0, 1), runtime._asr_session_epoch - 1,
            stage="provider_final_ignored", check="session_changed",
        )
        assert logs == []
    finally:
        await _close_stack(core)


@pytest.mark.parametrize("score,classification,delivered", [(.78, "high", 1), (.20, "low", 0)])
async def test_live_classification_and_final_trace_preserve_core_delivery(monkeypatch, score, classification, delivered):
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, data: logs.append(data))
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=score)
    core.continuity_score_host.ready.set()
    try:
        await _interval(runtime, shadow, turn, 3200)
        facts = [item for item in logs if item["stage"] == "speaker_fact_observed"]
        assert facts
        assert {item["speaker_classification"] for item in facts} == {classification}
        assert all(item["provider_utterance_id"] == 1 for item in facts)
        assert core.handle_input_transcript.await_count == delivered
        assert core.session.create_response.await_count == delivered
        assert any(item["stage"] == "provider_final_received" for item in logs)
        await runtime._handle_provider_final(ProviderUtteranceKey(0, 0, 1), "PRIVATE_TEXT", runtime._asr_session_epoch, "qwen")
        await _join_logs(runtime)
        assert any(item["stage"] == "provider_final_ignored" and item["failed_check"] == "already_completed" for item in logs)
        assert core.session.create_response.await_count == delivered
        assert "PRIVATE" not in json.dumps(logs)
        assert "similarity" not in json.dumps(logs)
    finally:
        await _close_stack(core)


@pytest.mark.parametrize("broken_sink", [False, True])
async def test_short_mismatched_boundary_uses_real_failure_path(monkeypatch, broken_sink):
    logs = []
    def log(_, data):
        if broken_sink:
            raise OSError("PRIVATE_DISK_ERROR")
        logs.append(data)
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", log)
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.95)
    core.continuity_score_host.ready.set()
    try:
        for sequence in range(1, 9):
            await _submit_pcm(runtime, turn, sequence=sequence)
        await shadow.wait_idle()
        epoch = runtime._asr_session_epoch
        key = ProviderUtteranceKey(0, 0, 1)
        await runtime._handle_provider_utterance_started(ProviderUtteranceStartedNotification(0, 0, 1, 0), epoch)
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification("boundary", 0, 0, 1, "exact", ProviderAudioRange(1, 12800)), epoch,
        )
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification("ordered", 0, 0, 1, "exact", ProviderAudioRange(1, 12800)), epoch,
        )
        await runtime._handle_provider_final(key, "PRIVATE_TEXT", epoch, "qwen")
        await shadow.wait_idle()
        await runtime.wait_transcript_idle()
        await _join_logs(runtime)
        assert core.continuity_score_host.calls == 0
        assert not runtime._asr_provider_exact_pending
        if not broken_sink:
            assert any(item.get("failed_check") == "anchor_start_mismatch" for item in logs)
            assert "PRIVATE" not in json.dumps(logs)
        # Both healthy and broken diagnostic sinks must preserve fallback delivery.
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
    finally:
        await _close_stack(core)
