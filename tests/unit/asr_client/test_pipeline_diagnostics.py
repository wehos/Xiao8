"""Correlated diagnostics observe real decisions without becoming authority."""

import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client import runtime as runtime_module
from main_logic.asr_client.pipeline_diagnostics import PipelineDiagnostics, safe_fields
from main_logic.asr_client.endpointing.detector import DetectorIngressIdentity
from main_logic.asr_client.endpointing.detector_runtime import _VoiceTurnAdapter
from main_logic.voice_turn.contracts import SpeechActivityEvent, EvaluationStatus, VoiceTranscriptEvent
from scripts.check_asr_pipeline_log import summarize, parse_record
from tests.unit.test_asr_voice_turn_adapter import (
    _FakeVad, _UnavailableVad, _FakeGate, _FailingGate, _FakeCoordinator,
    _complete, _incomplete, _failed_evaluation, _eventually,
)
from tests.unit.test_core_independent_asr import _Runtime, _install_ready_lifecycle
from tests.unit.asr_client.test_short_speaker_diagnostics import _join_logs, _interval
from tests.unit.asr_client.test_provider_speaker_continuity import _active_real_stack, _close_stack


def test_projection_and_audio_aggregation_are_bounded():
    core = _Runtime()
    records = []
    observer = PipelineDiagnostics(core._asr_runtime, lambda r, **kw: records.append(r))
    assert safe_fields({"text": "PRIVATE", "reason": "PRIVATE!", "phase": object(), "audio_samples": 3}) == {"audio_samples": 3}
    for _ in range(300):
        observer.audio("audio_received", 1, audio_samples=1600)
    assert len(records) == 1
    observer.flush()
    assert records[-1]["frame_count"] == 300
    assert records[-1]["audio_samples"] == 480000
    assert records[0]["diagnostic_session_ref"] == records[-1]["diagnostic_session_ref"]
    for epoch in range(100):
        observer.audio("audio_received", epoch, audio_samples=1)
    assert len(observer._progress) == 32
    observer.flush()
    assert not observer._progress
    assert "PRIVATE" not in json.dumps(records)


def test_broken_observer_cannot_raise():
    def broken(*args, **kwargs):
        raise OSError("PRIVATE")
    observer = PipelineDiagnostics(_Runtime()._asr_runtime, broken)
    observer.event("hello", 1, reason="test")
    observer.event("unsafe value!", 1)
    observer.audio("audio_received", 1, audio_samples=10)
    observer.flush()


@pytest.mark.parametrize("mode", ["complete", "incomplete", "unavailable", "error", "stale", "discarded", "superseded", "cancelled", "broken_sink"])
async def test_smart_turn_result_logs_captured_identity_and_preserves_commit(mode):
    core = _Runtime()
    ingress = core._capture_ingress_token()
    captured = DetectorIngressIdentity(ingress, 7, 11)
    result = {"incomplete": _incomplete(), "unavailable": _failed_evaluation(EvaluationStatus.UNAVAILABLE),
              "error": _failed_evaluation(EvaluationStatus.ERROR), "stale": _failed_evaluation(EvaluationStatus.STALE)}.get(mode, _complete())
    coordinator = _FakeCoordinator([result], block_evaluation=True)
    coordinator.generation = 0
    coordinator.activity_seq = 1
    coordinator.evaluation_threshold = .5
    commit = AsyncMock()
    adapter = _VoiceTurnAdapter(
        vad=_FakeVad(), gate=_FakeGate([(SpeechActivityEvent.CANDIDATE_PAUSE,)]),
        coordinator=coordinator, on_commit=commit, smart_turn_required=True,
    )
    records = []
    def observe(fields, token):
        if mode == "broken_sink":
            raise OSError("PRIVATE")
        records.append((fields, token))
    adapter._on_pipeline_diagnostic = observe
    try:
        await adapter.start()
        await adapter.push_audio(generation=1, buffer_epoch=2, utterance_id=3, pcm16=b"\x01\x00" * 1600, detector_identity=captured)
        await coordinator.evaluate_started.wait()
        if mode == "discarded":
            coordinator.generation += 1
        elif mode == "superseded":
            coordinator.activity_seq += 1
        elif mode == "cancelled":
            await adapter.reset(generation=2, buffer_epoch=2, utterance_id=4)
        coordinator.evaluate_release.set()
        if mode != "broken_sink":
            await _eventually(lambda: any(r[0]["phase"] == "evaluation_result" for r in records))
            evaluations = [r for r in records if r[0]["phase"] == "evaluation_result"]
            assert evaluations[-1][0]["outcome"] == mode
            assert evaluations[-1][0]["semantic_turn_id"] == 3
            assert evaluations[-1][0]["sequence_no"] == 11
            assert evaluations[-1][1] is ingress
            assert "PRIVATE" not in json.dumps([r[0] for r in records])
        if mode in {"complete", "broken_sink"}:
            await _eventually(lambda: commit.await_count == 1)
        else:
            assert commit.await_count == 0
    finally:
        coordinator.evaluate_release.set()
        await adapter.close()


@pytest.mark.parametrize("failure", ["vad_load", "vad_feed"])
async def test_vad_failure_records_degradation_before_periodic_smart_turn(failure):
    core = _Runtime()
    ingress = core._capture_ingress_token()
    records = []
    commit = AsyncMock()
    adapter = _VoiceTurnAdapter(
        vad=_UnavailableVad() if failure == "vad_load" else _FakeVad(),
        gate=_FailingGate() if failure == "vad_feed" else _FakeGate(),
        coordinator=_FakeCoordinator([_complete()]), on_commit=commit, smart_turn_required=True,
    )
    adapter._on_pipeline_diagnostic = lambda fields, token: records.append(fields)
    try:
        await adapter.start()
        for sequence in range(1, 7):
            await adapter.push_audio(generation=1, buffer_epoch=0, utterance_id=1,
                pcm16=b"\x01\x00" * 1600, detector_identity=DetectorIngressIdentity(ingress, 0, sequence))
        await _eventually(lambda: commit.await_count == 1)
        assert any(r["phase"] == failure for r in records)
        assert any(r.get("reason") == "periodic_no_vad" for r in records)
    finally:
        await adapter.close()


@pytest.mark.parametrize("mode", ["submitted", "empty", "rejected", "swap_timeout", "cancelled", "failed"])
async def test_core_terminal_records_distinguish_request_from_reply(monkeypatch, mode):
    core = _Runtime()
    _install_ready_lifecycle(core)
    runtime = core._asr_runtime
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, r: logs.append(r))
    turn = runtime._capture_turn_token(core._asr_lifecycle)
    event = VoiceTranscriptEvent(turn, "qwen", "" if mode == "empty" else "PRIVATE")
    if mode == "rejected":
        core.handle_input_transcript.return_value = False
    elif mode in {"cancelled", "failed"}:
        core.session.create_response.side_effect = asyncio.CancelledError() if mode == "cancelled" else RuntimeError("PRIVATE")
    if mode == "swap_timeout":
        core._core_voice_session_swap_barrier_timeout_s = .01
        await core._core_voice_session_swap_lock.acquire()
    try:
        if mode in {"cancelled", "failed"}:
            with pytest.raises(asyncio.CancelledError if mode == "cancelled" else RuntimeError):
                await core._dispatch_core_asr_transcript(event)
        else:
            await core._dispatch_core_asr_transcript(event)
        await _join_logs(runtime)
        terminal = [r for r in logs if r.get("stage") == "core_voice_delivery"][-1]
        assert terminal["outcome"] == (mode if mode in {"submitted", "cancelled", "failed"} else "abandoned")
        assert terminal["turn_id"] == turn.turn_id
        assert "PRIVATE" not in json.dumps(logs)
    finally:
        if core._core_voice_session_swap_lock.locked():
            core._core_voice_session_swap_lock.release()
        await runtime.close()


async def test_real_provider_pipeline_report_and_old_log_gaps(monkeypatch):
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, r: logs.append(r))
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.78)
    core.continuity_score_host.ready.set()
    try:
        await _interval(runtime, shadow, turn, 1600)
        await core._voice_input_registry.wait_idle()
        await _join_logs(runtime)
        report = summarize("ASR resolution " + repr(r) for r in logs)
        current = report["sessions"][0]
        assert current["coverage"]["audio_input"] == "observed"
        assert current["coverage"]["audio_write"] == "observed"
        assert current["turns"][-1]["core_outcome"] == "submitted"
        assert core.session.create_response.await_count == 1
        assert "PRIVATE" not in json.dumps(report)
        old = summarize(["ASR resolution " + repr({"diagnostic_session_ref": "a" * 24, "stage": "provider_final_received", "text": "PRIVATE"})])
        assert old["sessions"][0]["coverage"]["audio_input"] == "not_observed"
        assert old["sessions"][0]["turns"] == []
    finally:
        await _close_stack(core)


def test_checker_missing_truncated_dropped_and_untrusted_records():
    assert parse_record("ASR resolution __import__('os').system('PRIVATE')") is None
    assert parse_record("ASR resolution {not valid}") is None
    assert parse_record("other line") is None
    records = ["ASR resolution " + repr({"diagnostic_session_ref": "a" * 24, "stage": "asr_lifecycle",
               "endpoint_authority": "provider", "diagnostic_records_dropped": 1})]
    report = summarize(records)
    assert report["sessions"][0]["log_gaps"]
    assert report["sessions"][0]["coverage"]["smart_turn"] == "not_applicable"
    assert summarize(records * 4, max_records=2)["sessions"][0]["log_gaps"]
    assert summarize(records + [records[0].replace("a" * 24, "b" * 24)], max_sessions=1)["sessions_truncated"]


def test_checker_never_merges_reused_turn_ids_across_routes():
    base = {"diagnostic_session_ref": "a" * 24, "stage": "core_voice_delivery", "turn_id": 1,
            "audio_generation": 0, "lease_generation": 1, "outcome": "submitted"}
    records = [{**base, "route_generation": 1}, {**base, "route_generation": 2, "outcome": "abandoned"},
               {"diagnostic_session_ref": "a" * 24, "turn_id": 1, "stage": "admission_decision", "disposition": "forward"}]
    session = summarize("ASR resolution " + repr(r) for r in records)["sessions"][0]
    assert session["ambiguous_partial_turn_ids"] == [1]
    assert len(session["turns"]) == 2
    assert [r["core_outcome"] for r in session["turns"]] == ["submitted", "abandoned"]
    assert all(r["admission"] == "not_observed" for r in session["turns"])


async def test_smart_turn_speaker_diagnostics_require_current_binding(monkeypatch):
    from main_logic.asr_client.speaker_shadow.contracts import SpeakerShadowCandidateKey
    from main_logic.asr_client.speaker_shadow.diagnostics import SpeakerShadowDiagnostic
    core = _Runtime()
    _install_ready_lifecycle(core)
    runtime = core._asr_runtime
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, r: logs.append(r))
    source = object()
    runtime._asr_detector._speaker_shadow = source
    runtime._speaker_verifier_activation_generation = "test-generation"
    candidate = SpeakerShadowCandidateKey(0, 1, "smart_turn_turn")
    turn = runtime._capture_turn_token(core._asr_lifecycle)
    event = SpeakerShadowDiagnostic(candidate, "speaker_score_completed", 0, 16000, 24000,
        24000, None, 24000, 1, 24000, "completed", 24000, 1500, None, 1, False, None, False)
    try:
        runtime._accept_speaker_diagnostic(event, activation_generation="test-generation", source=source)
        await _join_logs(runtime)
        assert not logs
        runtime._asr_admission_candidate_turns[candidate] = turn
        runtime._accept_speaker_diagnostic(event, activation_generation="test-generation", source=source)
        await _join_logs(runtime)
        assert logs[-1]["candidate_role"] == "smart_turn"
        assert logs[-1]["turn_id"] == turn.turn_id
        runtime._asr_admission_candidate_turns[candidate] = replace(turn, ingress=replace(turn.ingress, audio_generation=99))
        runtime._accept_speaker_diagnostic(event, activation_generation="test-generation", source=source)
        await _join_logs(runtime)
        assert len(logs) == 1
    finally:
        runtime._asr_detector._speaker_shadow = None
        await runtime.close()


async def test_endpoint_callback_keeps_original_semantic_identity(monkeypatch):
    from main_logic.asr_client.endpointing.detector_runtime import DetectorRuntime
    from tests.unit.test_asr_detector_runtime import _smart_turn_policy
    core = _Runtime()
    _install_ready_lifecycle(core)
    runtime = core._asr_runtime
    detector = DetectorRuntime(vad=_FakeVad(), gate=_FakeGate(), provider_policy=_smart_turn_policy(), coordinator=_FakeCoordinator(), on_event=AsyncMock())
    runtime._asr_detector = detector
    ingress = core._capture_ingress_token()
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, r: logs.append(r))
    detector.set_pipeline_diagnostic_callback(lambda fields, token: runtime._observe_endpoint_diagnostic(fields, token, source=detector, epoch=ingress.session_epoch))
    try:
        callback = detector._semantic_adapter._on_pipeline_diagnostic
        callback({"phase": "evaluation_result", "semantic_turn_id": 5, "outcome": "stale"}, ingress)
        await _join_logs(runtime)
        assert logs[-1]["semantic_turn_id"] == 5
        callback({"phase": "evaluation_result"}, None)
        callback({"phase": "evaluation_result"}, replace(ingress, session_epoch=99))
        runtime._observe_endpoint_diagnostic({}, ingress, source=object(), epoch=ingress.session_epoch)
        await _join_logs(runtime)
        assert len(logs) == 1
    finally:
        await runtime.close()


@pytest.mark.parametrize("mode", ["missing_lifecycle", "deny_fenced", "ingress_stale", "feed_error"])
async def test_audio_exit_reason_explains_non_delivery(monkeypatch, mode):
    from main_logic.asr_client.runtime import DenyTransportState
    from tests.unit.asr_client.test_provider_speaker_continuity import _submit_pcm
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.95)
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, r: logs.append(r))
    try:
        if mode == "missing_lifecycle":
            runtime._asr_lifecycle = None
        elif mode == "deny_fenced":
            runtime._asr_deny_transport_state = DenyTransportState.DENY_FENCED
        elif mode == "ingress_stale":
            turn = replace(turn, ingress=replace(turn.ingress, audio_generation=99))
        else:
            monkeypatch.setattr(detector, "feed", AsyncMock(side_effect=RuntimeError("PRIVATE")))
        result = await _submit_pcm(runtime, turn, sequence=1)
        await _join_logs(runtime)
        events = [r for r in logs if r.get("stage") == "audio_submit"]
        assert events
        expected = {"missing_lifecycle": "unavailable", "deny_fenced": "accepted", "ingress_stale": "stale", "feed_error": "unavailable"}[mode]
        assert result.status.value == expected
        assert events[-1]["outcome"] == expected
        assert events[-1]["reason"] in {mode, "handoff_stale", "submit_exception"}
        assert core.session.create_response.await_count == 0
        assert "PRIVATE" not in json.dumps(logs)
    finally:
        await _close_stack(core)


def test_checker_cli_writes_only_safe_report(tmp_path, monkeypatch):
    import sys
    from scripts.check_asr_pipeline_log import main
    source = tmp_path / "main.log"
    target = tmp_path / "report.json"
    record = {"diagnostic_session_ref": "a" * 24, "source_session_epoch": 1,
              "reason_code": "ASR_TEST_FAILED", "secret": "PRIVATE"}
    source.write_text("ASR incident " + repr(record), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_asr_pipeline_log", str(source), "--output", str(target)])
    main()
    output = target.read_text(encoding="utf-8")
    assert "PRIVATE" not in output
    assert json.loads(output)["sessions"][0]["session_findings"][0]["reason_code"] == "ASR_TEST_FAILED"


@pytest.mark.parametrize("failure", ["overflow", "unavailable", "error", "cancelled", "cancelled_before_start"])
async def test_pipeline_writer_bound_failures_are_visible(monkeypatch, failure):
    from concurrent.futures import Future
    core = _Runtime()
    runtime = core._asr_runtime
    batches = []
    blocked = Future()
    def submit(records, *, kind):
        assert kind == "pipeline"
        assert len(records) <= 16
        batches.append(records)
        if failure == "unavailable":
            return None
        if failure == "error":
            raise OSError("PRIVATE")
        return blocked
    monkeypatch.setattr(runtime_module, "submit_resolution_log", submit)
    try:
        for _ in range(100 if failure == "overflow" else 1):
            runtime._schedule_pipeline_session_event("test_event", 0, outcome="started")
        assert len(runtime._asr_pipeline_pending) <= 32
        assert len([t for t in runtime._asr_close_tasks if t.get_name() == "asr-pipeline-log"]) == 1
        if failure == "cancelled_before_start":
            runtime._asr_pipeline_log_task.cancel()
        await asyncio.sleep(0)
        if failure in {"cancelled", "cancelled_before_start"}:
            runtime._asr_pipeline_log_task.cancel()
            await asyncio.gather(runtime._asr_pipeline_log_task, return_exceptions=True)
        elif failure == "overflow":
            blocked.set_result(None)
            await _join_logs(runtime)
        else:
            await _join_logs(runtime)
        assert runtime._asr_resolution_log_dropped > 0
        assert len(runtime._asr_pipeline_pending) == 0
        assert "PRIVATE" not in json.dumps(batches)
    finally:
        if not blocked.done():
            blocked.set_result(None)
        await runtime.close()
