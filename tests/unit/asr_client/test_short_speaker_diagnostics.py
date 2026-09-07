"""Real ownership and scoring paths explain short-interval fail-open decisions."""

import asyncio
import json
from dataclasses import replace

import pytest

from main_logic.asr_client import runtime as runtime_module
from main_logic.asr_client._provider_events import (
    ProviderAudioRange, ProviderEndpointNotification, ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from main_logic.asr_client.speaker_shadow.runtime import _BackendHostTimeout
from main_logic.asr_client.speaker_shadow.runtime import SpeakerShadowRuntime
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack, _submit_pcm, _close_stack,
)
from tests.unit.asr_client.test_candidate_rejection_runtime import _drain_runtime_admission


async def _join_logs(runtime):
    if runtime._asr_close_tasks:
        await asyncio.gather(*tuple(runtime._asr_close_tasks))


async def _interval(runtime, shadow, turn, duration, start=0, *, final_first=False, finalize=True):
    for sequence, offset in enumerate(range(0, duration, 100), 1):
        receipt = await _submit_pcm(runtime, turn, sequence=sequence,
                                    duration_ms=min(100, duration - offset))
        assert receipt.status.value == "accepted"
    await shadow.wait_idle()
    await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=start * 16),
        runtime._asr_session_epoch,
    )
    await shadow.wait_idle()
    await _join_logs(runtime)
    for phase in ("boundary", "ordered"):
        if phase == "ordered" and final_first:
            await runtime._handle_provider_final(
                ProviderUtteranceKey(0, 0, 1), "PRIVATE_TRANSCRIPT", runtime._asr_session_epoch, "qwen",
            )
        notification = ProviderEndpointNotification(
            phase, 0, 0, 1, "exact", ProviderAudioRange(start * 16, duration * 16),
        )
        await runtime._handle_provider_endpoint_notification(notification, runtime._asr_session_epoch)
        await shadow.wait_idle()
        await _join_logs(runtime)
    if not final_first and finalize:
        await runtime._handle_provider_final(
            ProviderUtteranceKey(0, 0, 1), "PRIVATE_TRANSCRIPT", runtime._asr_session_epoch, "qwen",
        )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()
    await _join_logs(runtime)


@pytest.mark.parametrize("final_first", [False, True])
@pytest.mark.parametrize("duration,start,score,failure,reason,calls", [
    (800, 0, .95, None, "UNAVAILABLE", 0),
    (1499, 0, .95, None, "UNAVAILABLE", 0),
    (1500, 0, .95, None, "VERIFIED", 1),
    (1600, 0, .95, None, "VERIFIED", 1),
    (1600, 0, .20, None, "REJECTED", 2),
    (1600, 0, .95, "failed", "UNAVAILABLE", 1),
    (1600, 0, .95, "timeout", "UNAVAILABLE", 1),
    (2000, 1200, .95, None, "UNAVAILABLE", 0),
])
async def test_real_interval_diagnostics(
    monkeypatch, duration, start, score, failure, reason, calls, final_first,
):
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, data: logs.append(data))
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=score)
    core.continuity_score_host.ready.set()
    if failure:
        async def fail_score(*args, **kwargs):
            if failure == "timeout":
                raise _BackendHostTimeout("PRIVATE_ERROR")
            raise RuntimeError("PRIVATE_ERROR")
        core.continuity_score_host.score = fail_score
    try:
        await _interval(runtime, shadow, turn, duration, start, final_first=final_first)
        await core._voice_input_registry.wait_idle()
        decisions = [item for item in logs if item["stage"] == "admission_decision"]
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision["schema"] == 2
        assert decision["reason_code"].endswith(reason)
        assert core.handle_input_transcript.await_count == (0 if reason == "REJECTED" else 1)
        assert core.session.create_response.await_count == (0 if reason == "REJECTED" else 1)
        correlated = [item for item in logs if item.get("provider_utterance_id") == 1
                      and item.get("diagnostic_session_ref") == decision["diagnostic_session_ref"]]
        closed = [item for item in correlated if item["stage"] == "speaker_capture_closed"]
        assert len(closed) == 1, logs
        terminal = closed[0]
        assert terminal["minimum_sample_count"] == 24000
        assert terminal["provider_start_sample_16k"] == start * 16
        assert terminal["provider_end_sample_16k"] == (None if failure else duration * 16)
        endpoints = [item for item in correlated if item["stage"] == "provider_endpoint_received"]
        assert endpoints[-1]["provider_end_sample_16k"] == duration * 16
        assert terminal["accepted_sample_count"] == (duration - start) * 16
        # Exact reconciliation may transfer prior scoring to a new candidate.
        # Count actual model attempts across all candidates for this key.
        attempts = [item for item in correlated if item["stage"] == "speaker_score_started"]
        assert len(attempts) == calls
        if calls == 0:
            assert terminal["terminal_reason"] == "insufficient"
            assert terminal["score_outcome"] == "not_started"
            assert terminal["finish_sample_count"] == (duration - start) * 16
        if failure:
            assert any(item.get("score_outcome") == failure for item in correlated)
        assert all(type(v) in (str, int, bool, type(None)) for item in logs for v in item.values())
        assert "PRIVATE" not in json.dumps(logs)
        assert not any(item.get("diagnostic_records_dropped") for item in logs)
    finally:
        await _close_stack(core)


async def test_stale_diagnostics_cannot_borrow_new_owner(monkeypatch):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.95)
    observed = []
    actual = shadow._on_diagnostic
    shadow._on_diagnostic = lambda event: (observed.append(event), actual(event))
    core.continuity_score_host.ready.set()
    try:
        await _interval(runtime, shadow, turn, 800, finalize=False)
        assert observed
        output = []
        monkeypatch.setattr(runtime, "_schedule_asr_diagnostic_metadata", lambda data, **kw: output.append(data))
        event = observed[-1]
        runtime._accept_speaker_diagnostic(event, activation_generation="continuity-test", source=shadow)
        assert len(output) == 1
        output.clear()
        runtime._accept_speaker_diagnostic(event, activation_generation="old-installation", source=shadow)
        assert not output
        runtime._accept_speaker_diagnostic(event, activation_generation="continuity-test", source=object())
        assert not output
        runtime._accept_speaker_diagnostic(
            replace(event, candidate=replace(event.candidate, shadow_generation=999)),
            activation_generation="continuity-test", source=shadow,
        )
        assert not output
        runtime._asr_session_epoch += 1
        runtime._accept_speaker_diagnostic(event, activation_generation="continuity-test", source=shadow)
        assert not output
    finally:
        await _close_stack(core)


async def test_diagnostic_callback_failure_does_not_change_evidence():
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.95)
    def fail(event):
        raise RuntimeError("diagnostic sink failed")
    shadow._on_diagnostic = fail
    core.continuity_score_host.ready.set()
    try:
        await _interval(runtime, shadow, turn, 1600)
        await core._voice_input_registry.wait_idle()
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
        assert shadow.snapshot()["callback_failure_count"] == 0
    finally:
        await _close_stack(core)


async def test_cancelled_scoring_reports_attempt_without_publishing_a_score():
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.95)
    observed = []
    evidence = []
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    shadow._on_diagnostic = observed.append
    original_evidence = shadow._on_evidence
    shadow._on_evidence = lambda event: (evidence.append(event), original_evidence(event))

    async def waiting_score(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    core.continuity_score_host.score = waiting_score
    try:
        for sequence in range(1, 17):
            await _submit_pcm(runtime, turn, sequence=sequence, duration_ms=100)
        await shadow.wait_idle()
        await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        await asyncio.wait_for(entered.wait(), 1)
        worker = shadow._worker_task
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2)
        assert cancelled.is_set()
        finished = [event for event in observed if event.stage == "speaker_score_finished"]
        assert len(finished) == 1
        assert finished[0].score_outcome == "cancelled"
        assert finished[0].score_attempt_count == 1
        assert finished[0].score_input_sample_count == 24000
        assert finished[0].scored_sample_count == 0
        assert not any(getattr(event, "evidence_available", False) for event in evidence)
        assert core.handle_input_transcript.await_count == 0
    finally:
        await _close_stack(core)


async def test_unknown_boundary_is_recorded_without_inventing_an_interval(monkeypatch):
    logs = []
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", lambda _, data: logs.append(data))
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=.95)
    try:
        await _submit_pcm(runtime, turn, sequence=1, duration_ms=800)
        await shadow.wait_idle()
        await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        for phase in ("boundary", "ordered"):
            await runtime._handle_provider_endpoint_notification(
                ProviderEndpointNotification(phase, 0, 0, 1, "unknown", None),
                runtime._asr_session_epoch,
            )
            await _join_logs(runtime)
        await runtime._handle_provider_final(
            ProviderUtteranceKey(0, 0, 1), "PRIVATE", runtime._asr_session_epoch, "qwen",
        )
        await _drain_runtime_admission(runtime)
        await runtime.wait_transcript_idle()
        await _join_logs(runtime)
        endpoints = [item for item in logs if item["stage"] == "provider_endpoint_received"]
        assert len(endpoints) == 2
        assert all(item["boundary_quality"] == "unknown" and item["provider_end_sample_16k"] is None
                   for item in endpoints)
        assert core.handle_input_transcript.await_count == 1
    finally:
        await _close_stack(core)


async def test_detail_burst_reserves_capacity_for_decision(monkeypatch):
    from tests.unit.asr_client.test_resolution_diagnostics import _resolved
    from tests.unit.test_core_independent_asr import _Runtime
    import threading

    coordinator, effect = await _resolved(mode="verified")
    runtime = _Runtime()
    runtime._asr_admission = coordinator
    release = threading.Event()
    logs = []
    def slow_sink(_, metadata):
        release.wait(2)
        logs.append(metadata)
    monkeypatch.setattr(runtime_module.asr_diagnostic_logger, "info", slow_sink)
    try:
        for _ in range(40):
            runtime._schedule_asr_diagnostic_metadata({"stage": "detail"}, capacity=8)
        assert len(runtime._asr_close_tasks) == 8
        assert runtime._asr_resolution_log_dropped == 32
        runtime._schedule_asr_resolution_log(effect, stage="admission_decision")
        assert len(runtime._asr_close_tasks) == 9
    finally:
        release.set()
        await _join_logs(runtime)
    decision = next(item for item in logs if item["stage"] == "admission_decision")
    assert decision["reason_code"] == "ASR_SPEAKER_VERIFIED"
    assert decision["diagnostic_records_dropped"] == 32
    assert not runtime._asr_close_tasks


def test_session_correlation_is_stable_private_and_releases_retired_runtime():
    from main_logic.asr_client.speaker_diagnostics import diagnostic_context, _RUNTIME_REFS
    class RuntimeIdentity:
        pass
    first, second = RuntimeIdentity(), RuntimeIdentity()
    first_ref = diagnostic_context(first, 1)["diagnostic_session_ref"]
    assert diagnostic_context(first, 1)["diagnostic_session_ref"] == first_ref
    assert diagnostic_context(first, 2)["diagnostic_session_ref"] != first_ref
    assert diagnostic_context(second, 1)["diagnostic_session_ref"] != first_ref
    count = len(_RUNTIME_REFS)
    del first
    assert len(_RUNTIME_REFS) == count - 1


async def _async_diagnostic(event):
    pass


@pytest.mark.parametrize("callback", [42, _async_diagnostic])
def test_diagnostic_callback_must_be_synchronous(callback):
    with pytest.raises(TypeError, match="diagnostic callback must be synchronous"):
        SpeakerShadowRuntime(backend_factory=None, on_diagnostic=callback)
