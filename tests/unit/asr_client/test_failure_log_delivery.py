"""Actual failure records reach the bounded sink without content or false completion."""

import asyncio
import threading
from types import SimpleNamespace

from main_logic.asr_client import diagnostic_logging, runtime as runtime_module
from main_logic.asr_client.failure_diagnostics import AudioFailureContext, CleanupTrace
from tests.unit.test_core_independent_asr import _Runtime, _install_ready_lifecycle


async def _join_logs(runtime):
    while runtime._asr_close_tasks:
        await asyncio.wait_for(asyncio.gather(
            *tuple(runtime._asr_close_tasks), return_exceptions=True,
        ), 1)
        await asyncio.sleep(0)


async def test_failure_logs_keep_unfinished_transport_visible_and_exclude_content(monkeypatch):
    monkeypatch.setattr(runtime_module, "_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS", .03)
    records = []
    monkeypatch.setattr(diagnostic_logging.logger, "info", lambda _, row: records.append(row))
    monkeypatch.setattr(diagnostic_logging.logger, "warning", lambda _, row: records.append(row))
    core = _Runtime()
    runtime = core._asr_runtime
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn_close():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    runtime._asr_session = SimpleNamespace(is_ready=True, close=stubborn_close)
    _install_ready_lifecycle(core, "qwen")
    context = AudioFailureContext("submit_observation", expected={
        "sequence_no": 3, "raw_transcript": "PRIVATE_TRANSCRIPT",
        "audio": b"PRIVATE_PCM", "speaker_vector": [987654.0],
    })
    context.fail("sequence_gap", actual={"sequence_no": 5, "raw_transcript": "PRIVATE_TEXT"},
                 error=ValueError("PRIVATE_EXCEPTION"), send_state="written")
    try:
        await asyncio.wait_for(runtime._handle_independent_asr_error(
            runtime._asr_session_epoch, "qwen", status_code="ASR_AUDIO_ORDERING_FAILED",
            failure_context=context,
        ), .5)
        await asyncio.wait_for(entered.wait(), .2)
        await asyncio.wait_for(cancelled.wait(), .2)
        await asyncio.wait_for(asyncio.to_thread(diagnostic_logging._QUEUE.join), .5)
        pending_records = [row for row in records if "components" in row]
        assert pending_records
        assert all(row["completed_at"] is None for row in pending_records)
        assert any(row["components"].get("transport") == "timed_out"
                   and row["residual_components"] >= 1 for row in pending_records)
        incident = next(row for row in records if row.get("stage") == "blocked")
        assert incident["failed_operation"] == "submit_observation"
        assert incident["failed_check"] == "sequence_gap"
        assert incident["expected"]["sequence_no"] == 3
        assert incident["actual"]["sequence_no"] == 5
        assert incident["send_state"] == "written"
        release.set()
        await _join_logs(runtime)
        await asyncio.wait_for(asyncio.to_thread(diagnostic_logging._QUEUE.join), .5)
        completed = [row for row in records if row.get("completed_at") is not None]
        assert completed
        assert completed[-1]["residual_components"] == 0
        assert completed[-1]["components"]["transport"] == "completed"
        assert completed[-1]["incident_id"] == incident["incident_id"]
        assert "PRIVATE_" not in repr(records)
        assert "987654" not in repr(records)
    finally:
        release.set()
        await _join_logs(runtime)
        await runtime.close()


async def test_incident_and_cleanup_share_bounded_slow_sink(monkeypatch):
    runtime = _Runtime()._asr_runtime
    entered, release = threading.Event(), threading.Event()
    records = []

    def stalled_writer(_, row):
        entered.set()
        release.wait(2)
        records.append(row)

    monkeypatch.setattr(diagnostic_logging.logger, "warning", stalled_writer)
    monkeypatch.setattr(diagnostic_logging.logger, "info", stalled_writer)
    trace = CleanupTrace("incident-bounded", lambda row: runtime._schedule_asr_diagnostic_metadata(
        row, kind="cleanup",
    ))
    try:
        for index in range(40):
            if index % 2:
                trace.mark("transport", "pending")
                trace.record("pending", stage="cleanup_started")
            else:
                runtime._schedule_asr_incident_log(
                    incident_id="incident-bounded", reason_code="ASR_AUDIO_ORDERING_FAILED",
                    stage="blocked", source_session_epoch=1,
                )
        assert sum(task.get_name() == "asr-resolution-log"
                   for task in runtime._asr_close_tasks) == 16
        assert sum(task.get_name() == "asr-incident-log"
                   for task in runtime._asr_close_tasks) == 4
        assert len(runtime._asr_close_tasks) == 20
        assert runtime._asr_resolution_log_dropped >= 4
        assert await asyncio.to_thread(entered.wait, .5)
        assert diagnostic_logging._QUEUE.qsize() <= 32
        await _join_logs(runtime)
        assert not release.is_set(), "bounded sink wait depended on writer completion"
        assert not runtime._asr_close_tasks
    finally:
        release.set()
        await asyncio.wait_for(asyncio.to_thread(diagnostic_logging._QUEUE.join), 1)
        await runtime.close()
    assert records
    assert all(row["incident_id"] == "incident-bounded" for row in records)
