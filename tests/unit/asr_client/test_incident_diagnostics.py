"""Content-free incident logging must survive cleanup without owning the route."""

import asyncio
import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client import runtime as runtime_module
from main_logic.asr_client.runtime import asr_diagnostic_logger
from tests.unit.test_core_independent_asr import _Runtime, _install_ready_lifecycle
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack,
    _close_stack,
    _submit_pcm,
)


async def _join_close_tasks(runtime):
    tasks = tuple(runtime._asr_close_tasks)
    if tasks:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
        assert all(result is None or isinstance(result, asyncio.CancelledError) for result in results)


async def test_failure_log_preserves_safe_first_reason_and_status_incident(monkeypatch):
    runtime = _Runtime()
    runtime._asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    _install_ready_lifecycle(runtime, "qwen")
    runtime._asr_provider = "qwen"
    runtime._asr_sealed_provider_key = runtime_module.ProviderUtteranceKey(3, 4, 5)
    epoch = runtime._asr_session_epoch
    logs = []
    monkeypatch.setattr(asr_diagnostic_logger, "warning", lambda *args: logs.append(args))

    await runtime._handle_independent_asr_error(
        epoch, "qwen", status_code="ASR_AUDIO_ORDERING_FAILED",
        reason_code="ASR_PRIVATE_DETAIL: transcript and api_key=secret",
    )
    await runtime._handle_independent_asr_error(epoch, "qwen", reason_code="ASR_SECOND")
    await _join_close_tasks(runtime)

    incidents = [args[1] for args in logs if args[0] == "ASR incident %s"]
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["reason_code"] == "ASR_AUDIO_ORDERING_FAILED"
    assert incident["stage"] == "blocked"
    assert incident["source_session_epoch"] == epoch
    assert incident["session_epoch"] == epoch + 1
    assert incident["provider_utterance_id"] == 5
    assert incident["provider_buffer_epoch"] == 4
    assert incident["app_version"]
    assert "secret" not in repr(incidents)
    assert "transcript" not in repr(incidents)
    assert "Test" not in repr(incidents)
    payloads = [json.loads(call.args[0]) for call in runtime.send_status.await_args_list]
    assert incident["incident_id"] == payloads[-1]["details"]["incident_id"]
    assert runtime._asr_route_mode == "blocked"


@pytest.mark.parametrize("fail_writer", [False, True])
async def test_slow_or_failed_logger_does_not_delay_route_cleanup(monkeypatch, fail_writer):
    runtime = _Runtime()
    session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = session
    _install_ready_lifecycle(runtime, "qwen")
    epoch = runtime._asr_session_epoch
    entered = threading.Event()
    release = threading.Event()
    writer_threads = []
    loop_thread = threading.get_ident()

    def warning(*args):
        if args[0] != "ASR incident %s":
            return
        writer_threads.append(threading.get_ident())
        entered.set()
        release.wait(3)
        if fail_writer:
            raise OSError("private log sink failure")

    monkeypatch.setattr(asr_diagnostic_logger, "warning", warning)
    try:
        await asyncio.wait_for(runtime._handle_independent_asr_error(epoch, "qwen"), 1)
        assert await asyncio.to_thread(entered.wait, 1)
        assert runtime._asr_route_mode == "blocked"
        assert runtime._asr_session is None
        assert len(writer_threads) == 1
        assert writer_threads[0] != loop_thread
    finally:
        release.set()
        await _join_close_tasks(runtime)
    session.close.assert_awaited_once()


async def test_failure_snapshot_is_queued_before_cancellable_invalidation(monkeypatch):
    runtime = _Runtime()
    runtime._asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    _install_ready_lifecycle(runtime, "qwen")
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    entered = asyncio.Event()
    logs = []

    async def pause_invalidation(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asr_diagnostic_logger, "warning", lambda *args: logs.append(args))
    monkeypatch.setattr(runtime._asr_runtime, "_finish_admission_invalidation", pause_invalidation)
    task = asyncio.create_task(runtime._handle_independent_asr_error(runtime._asr_session_epoch, "qwen"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _join_close_tasks(runtime)
    assert len([args for args in logs if args[0] == "ASR incident %s"]) == 1
    runtime._asr_admission_ingress_started = False
    await runtime._asr_admission_ingress.close()


async def test_degradation_and_terminal_logs_correlate_without_overwriting_first_reason(monkeypatch):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack()
    logs = []
    monkeypatch.setattr(asr_diagnostic_logger, "warning", lambda *args: logs.append(args))
    try:
        await _submit_pcm(runtime, turn, sequence=1)
        evidence = runtime._asr_provider_speaker_evidence_lease
        ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
        runtime._poison_provider_speaker_ledger(ledger, "provider_pcm_receipt_missing")
        runtime._poison_provider_speaker_ledger(ledger, "another_unavailable_reason")
        await runtime._handle_independent_asr_error(
            runtime._asr_session_epoch, "qwen",
            status_code="ASR_AUDIO_ORDERING_FAILED",
            reason_code="ASR_SPEAKER_LEASE_OWNER_CONFLICT",
        )
        await _join_close_tasks(runtime)
        incidents = [args[1] for args in logs if args[0] == "ASR incident %s"]
        assert len(incidents) == 2
        by_stage = {record["stage"]: record for record in incidents}
        degraded = by_stage["evidence_unavailable"]
        blocked = by_stage["blocked"]
        assert degraded["reason_code"] == "ASR_PROVIDER_PCM_RECEIPT_MISSING"
        assert blocked["reason_code"] == "ASR_SPEAKER_LEASE_OWNER_CONFLICT"
        assert blocked["preceding_incident_id"] == degraded["incident_id"]
        assert degraded["lease_generation"] == evidence.lease_generation
        assert degraded["timeline_generation"] is not None
        assert "another_unavailable_reason" not in repr(incidents)
    finally:
        await _close_stack(core)


async def test_incident_records_are_bounded_and_snapshot_values_are_not_live(monkeypatch):
    runtime = _Runtime()
    logs = []
    monkeypatch.setattr(asr_diagnostic_logger, "warning", lambda *args: logs.append(args))
    original_epoch = runtime._asr_session_epoch
    for index in range(12):
        runtime._schedule_asr_incident_log(
            incident_id=f"asr-failure-{index:032x}",
            reason_code="ASR_AUDIO_ORDERING_FAILED",
            stage="blocked",
            source_session_epoch=original_epoch,
        )
    assert len(runtime._asr_close_tasks) == 4
    runtime._asr_session_epoch += 1
    await _join_close_tasks(runtime)
    assert len(logs) == 4
    assert all(args[1]["session_epoch"] == original_epoch for args in logs)
    assert not runtime._asr_close_tasks


async def test_safe_failure_record_reaches_file_after_route_cleanup(tmp_path):
    runtime = _Runtime()
    runtime._asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    _install_ready_lifecycle(runtime, "qwen")
    destination = tmp_path / "incident.log"
    handler = logging.FileHandler(destination, encoding="utf-8")
    asr_diagnostic_logger.addHandler(handler)
    try:
        await runtime._handle_independent_asr_error(
            runtime._asr_session_epoch, "qwen",
            reason_code="ASR_SPEAKER_LEASE_OWNER_CONFLICT",
        )
        await _join_close_tasks(runtime)
        assert runtime._asr_session is None
    finally:
        asr_diagnostic_logger.removeHandler(handler)
        handler.close()
    persisted = await asyncio.to_thread(destination.read_text, encoding="utf-8")
    assert "ASR_SPEAKER_LEASE_OWNER_CONFLICT" in persisted
    assert "asr-failure-" in persisted
    assert "source_session_epoch" in persisted
