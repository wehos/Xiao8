"""Final verdicts remain distinguishable through the real admission reducers."""

import asyncio
import json
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from main_logic.asr_client import runtime as runtime_module
from main_logic.asr_client import diagnostic_logging
from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.admission.contracts import (
    ExactIntervalPromotionScope,
    BoundaryProof,
    PendingProviderFinal,
    ProviderFinalReceived,
    ResolveReserved,
    SpeakerCaptureLeaseToken,
    SpeakerCheckpointKind,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseUnavailable,
)
from main_logic.asr_client.admission.coordinator import VoiceTurnAdmissionCoordinator
from main_logic.asr_client.speaker_shadow.contracts import SpeakerShadowCandidateKey
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken
from tests.unit.test_core_independent_asr import _Runtime
from tests.unit.asr_client.test_incident_diagnostics import _join_close_tasks
from tests.unit.asr_client import test_provider_speaker_continuity as continuity


async def _resolved(*, mode, exact=True):
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    turn = VoiceTurnToken(VoiceIngressToken(1, "PRIVATE_CONNECTION", 2, 3, 4), 7)
    key = ProviderUtteranceKey(1, 2, 3)
    lease = SpeakerCaptureLeaseToken(1, 2, 3, 4, 5)
    target = SpeakerShadowCandidateKey(2, 1, "provider_candidate")
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    if exact:
        parent = await coordinator.get_speaker_lease(lease)
        child = await coordinator.get_record(turn)
        promoted = await coordinator.promote_exact_interval_tail_child(
            ExactIntervalPromotionScope(
                parent_lease_token=lease,
                parent_record_generation=parent.record_generation,
                expected_parent_logical_revision=parent.logical_revision,
                expected_parent_state=parent.state,
                turn_token=turn,
                child_record_generation=child.record_generation,
                expected_child_logical_revision=child.logical_revision,
                provider_key=key,
                boundary_proof=BoundaryProof(1, 1, key),
                target_candidate=target,
                successor_candidate=SpeakerShadowCandidateKey(2, 2, "provider_candidate"),
            )
        )
        activated = await coordinator.activate_exact_interval(promoted.receipt)

    events = {
        "verified": [SpeakerLeaseHigh(target, 1), SpeakerLeaseCaptureClosed(target, 1)],
        "unavailable": [SpeakerLeaseUnavailable(target, 1)],
        "insufficient": [SpeakerLeaseCaptureClosed(target, 0)],
        "rejected": [
            SpeakerLeaseLow(target, 1, SpeakerCheckpointKind.FIRST),
            SpeakerLeaseLow(target, 2, SpeakerCheckpointKind.SECOND),
        ],
    }[mode]
    earlier_effects = []
    for event in events:
        if exact:
            await coordinator.post_exact_interval(activated.receipt, event)
        else:
            transition = await coordinator.post_speaker_lease(lease, event)
            for child_result in transition.child_results:
                earlier_effects.extend(child_result.effects)
    final = ProviderFinalReceived(
        PendingProviderFinal(key, "qwen", "PRIVATE_TRANSCRIPT", 10.0, 10.2),
    )
    if exact:
        result = await coordinator.post_exact_interval(activated.receipt, final)
        effects = result.effects
    else:
        effects = await coordinator.post(turn, final)
    effect = next(effect for effect in [*earlier_effects, *effects] if isinstance(effect, ResolveReserved))
    return coordinator, effect


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("mode,reason,evidence", [
    ("verified", "ASR_SPEAKER_VERIFIED", "allow"),
    ("unavailable", "ASR_SPEAKER_EVIDENCE_UNAVAILABLE", "unavailable"),
    ("insufficient", "ASR_SPEAKER_EVIDENCE_UNAVAILABLE", "unavailable"),
    ("rejected", "ASR_SPEAKER_REJECTED", "deny_latched"),
])
async def test_authoritative_verdict_snapshot(mode, reason, evidence, exact):
    coordinator, effect = await _resolved(mode=mode, exact=exact)
    record = await coordinator.get_record(effect.turn_token)
    snapshot = coordinator.snapshot_resolution_diagnostics(effect.ticket)
    assert snapshot["reason_code"] == reason
    assert snapshot["evidence_state"] == evidence
    assert snapshot["provider_utterance_id"] == 3
    assert snapshot["disposition"] == ("drop" if mode == "rejected" else "forward")
    assert await coordinator.get_record(effect.turn_token) is record
    assert "PRIVATE" not in json.dumps(snapshot)
    assert all(type(value) in (str, int, bool, type(None)) for value in snapshot.values())
    stale = replace(effect.ticket, resolution_nonce=effect.ticket.resolution_nonce + 1)
    assert coordinator.snapshot_resolution_diagnostics(stale)["evidence_state"] == "unknown"


@pytest.mark.parametrize("final_first", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
async def test_real_runtime_detector_shadow_finals_have_one_decision_per_key(
    monkeypatch, final_first, mixed,
):
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda *args: logs.append(args))
    await continuity.test_real_exact_three_finals_keep_successor_and_core_delivery(
        0.20 if mixed else 0.95, mixed, True, final_first,
    )
    decisions = [args[1] for args in logs if args[1]["stage"] == "admission_decision"
                 and args[1].get("provider_utterance_id") in (1, 2, 3)]
    assert len(decisions) == 3
    assert len({item["turn_ref"] for item in decisions}) == 3
    for item in decisions:
        rejected = mixed and item["provider_utterance_id"] == 1
        assert item["reason_code"] == ("ASR_SPEAKER_REJECTED" if rejected else "ASR_SPEAKER_VERIFIED")
    deliveries = [args[1] for args in logs if args[1]["stage"] == "transcript_resolution"
                  and args[1].get("provider_utterance_id") in (1, 2, 3)]
    assert len(deliveries) == 3
    assert all(item["dispatcher_applied"] for item in deliveries)
    assert "utterance " not in repr(logs)


async def test_slow_sink_is_bounded_and_does_not_hold_live_records(monkeypatch):
    coordinator, effect = await _resolved(mode="verified")
    runtime = _Runtime()
    runtime._asr_admission = coordinator
    release = threading.Event()
    captured = []

    def slow_writer(*args):
        release.wait(3)
        captured.append(args[1])

    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", slow_writer)
    try:
        for _ in range(40):
            runtime._schedule_asr_resolution_log(effect, stage="admission_decision")
        assert len(runtime._asr_close_tasks) == 16
        assert runtime._asr_resolution_log_dropped == 24
        coordinator._records.clear()  # Simulate retirement after scalar capture.
    finally:
        release.set()
        await _join_close_tasks(runtime)
    assert all(item["evidence_state"] == "allow" for item in captured)
    runtime._schedule_asr_resolution_log(effect, stage="admission_decision")
    await _join_close_tasks(runtime)
    assert captured[-1]["diagnostic_records_dropped"] == 24
    assert captured[-1]["evidence_state"] == "unknown"
    assert not runtime._asr_close_tasks


async def test_failed_sink_cannot_change_decision(monkeypatch):
    coordinator, effect = await _resolved(mode="unavailable")
    runtime = _Runtime()
    runtime._asr_admission = coordinator

    def fail(*args):
        raise OSError("sink unavailable")

    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", fail)
    before = await coordinator.get_record(effect.turn_token)
    runtime._schedule_asr_resolution_log(effect, stage="admission_decision")
    await _join_close_tasks(runtime)
    assert await coordinator.get_record(effect.turn_token) is before


async def test_forward_without_score_is_never_reported_as_verified():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    turn = VoiceTurnToken(VoiceIngressToken(1, "private", 2, 3, 4), 1)
    await coordinator.open_turn(turn)
    effects = await coordinator.post(turn, ProviderFinalReceived(
        PendingProviderFinal(None, "qwen", "private", 10.0, 10.2),
    ))
    effect = next(item for item in effects if isinstance(item, ResolveReserved))
    snapshot = coordinator.snapshot_resolution_diagnostics(effect.ticket)
    assert snapshot["reason_code"] == "ASR_FORWARD_WITHOUT_VERIFIED_EVIDENCE"
    assert snapshot["evidence_state"] == "none"


async def test_cancelled_log_tasks_leave_no_async_workers(monkeypatch):
    coordinator, effect = await _resolved(mode="unavailable")
    runtime = _Runtime()
    runtime._asr_admission = coordinator
    runtime._schedule_asr_resolution_log(effect, stage="admission_decision")
    tasks = tuple(runtime._asr_close_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert not runtime._asr_close_tasks
    assert coordinator.snapshot_resolution_diagnostics(effect.ticket)["evidence_state"] == "unavailable"


def test_production_setup_persists_decision_and_incident(tmp_path):
    # Isolate process-global handlers. No test FileHandler is attached to ASR.
    script = '''
import asyncio, logging, sys
from pathlib import Path
from utils.logger_config import RobustLoggerConfig, setup_logging
from tests.unit.asr_client.test_resolution_diagnostics import _resolved
from tests.unit.asr_client.test_incident_diagnostics import _join_close_tasks
from tests.unit.test_core_independent_asr import _Runtime
RobustLoggerConfig._get_log_directory = lambda self: Path(sys.argv[1])
service, config = setup_logging(service_name="Main", silent=True)
async def run():
    runtime = _Runtime()
    runtime._asr_admission, effect = await _resolved(mode="unavailable")
    runtime._schedule_asr_resolution_log(effect, stage="admission_decision")
    runtime._schedule_asr_incident_log(incident_id="asr-routing-test", reason_code="ASR_AUDIO_ORDERING_FAILED", stage="blocked", source_session_epoch=1)
    await _join_close_tasks(runtime)
asyncio.run(run())
logging.shutdown()
payload = config.log_file.read_text(encoding="utf-8")
assert "ASR_SPEAKER_EVIDENCE_UNAVAILABLE" in payload
assert "ASR_AUDIO_ORDERING_FAILED" in payload
assert "provider_utterance_id" in payload
assert "PRIVATE_TRANSCRIPT" not in payload
assert "PRIVATE_CONNECTION" not in payload
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_stalled_writer_has_process_wide_capacity_and_bounded_join(monkeypatch):
    coordinator, effect = await _resolved(mode="verified")
    runtime = _Runtime()
    runtime._asr_admission = coordinator
    entered = threading.Event()
    release = threading.Event()

    def stalled(*args):
        entered.set()
        release.wait(3)

    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", stalled)
    try:
        runtime._schedule_asr_resolution_log(effect, stage="admission_decision")
        assert await asyncio.to_thread(entered.wait, 1)
        # This completes while the filesystem substitute is STILL blocked.
        await asyncio.wait_for(_join_close_tasks(runtime), 1)
        assert not release.is_set()
        assert not runtime._asr_close_tasks
        snapshot = coordinator.snapshot_resolution_diagnostics(effect.ticket)
        futures = [diagnostic_logging.submit_resolution_log(snapshot) for _ in range(40)]
        assert sum(future is not None for future in futures) == 32
        assert diagnostic_logging._QUEUE.qsize() == 32
        runtime._schedule_asr_resolution_log(effect, stage="admission_decision")
        assert runtime._asr_resolution_log_dropped == 1
    finally:
        release.set()
        await asyncio.wait_for(asyncio.to_thread(diagnostic_logging._QUEUE.join), 2)
