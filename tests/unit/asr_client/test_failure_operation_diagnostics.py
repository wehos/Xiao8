"""Failure facts must be scoped, content-free and honest about completion."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client.failure_diagnostics import AudioFailureContext, CleanupTrace
from tests.unit.test_core_independent_asr import _Runtime, _install_ready_lifecycle


def test_failure_context_preserves_first_check_and_safe_scalar_copies():
    expected = {"sequence_no": 4, "text": "private", "session_epoch": 2}
    context = AudioFailureContext("submit_observation", expected=expected)
    actual = {"sequence_no": 6, "audio": b"private", "sample_cursor_16k": 6400}
    assert context.fail("sequence_gap", actual=actual, error=ValueError("secret")) is False
    actual["sequence_no"] = 999
    context.fail("cleanup_failed", actual={"sequence_no": 888})
    snapshot = context.snapshot()
    assert snapshot["failed_check"] == "sequence_gap"
    assert snapshot["actual"]["sequence_no"] == 6
    assert snapshot["expected"] == {"sequence_no": 4, "session_epoch": 2}
    assert snapshot["error_type"] == "ValueError"
    assert "private" not in repr(snapshot)
    assert "secret" not in repr(snapshot)
    expected["sequence_no"] = 8
    assert snapshot["expected"]["sequence_no"] == 4


def test_failure_context_does_not_serialize_arbitrary_values():
    context = AudioFailureContext("private utterance", expected={"turn_id": object()})
    context.fail("private utterance", actual={"session_epoch": True},
                 error=type("PrivateError", (Exception,), {})("private"), send_state="private")
    snapshot = context.snapshot()
    assert snapshot["failed_operation"] == "unknown"
    assert snapshot["failed_check"] == "unknown"
    assert snapshot["expected"] == snapshot["actual"] == {}
    assert snapshot["error_type"] == "internal_error"
    assert snapshot["send_state"] == "unknown"
    assert "private" not in repr(snapshot).lower()


def test_cleanup_trace_records_real_outcome_and_does_not_mutate_old_records():
    records = []
    trace = CleanupTrace("incident-one", records.append)
    trace.mark("transport", "timed_out")
    trace.mark("admission", "pending")
    trace.record("timed_out")
    trace.mark("admission", "completed")
    trace.record("completed", stage="background_cleanup_completed")
    assert records[0]["components"]["admission"] == "pending"
    assert records[0]["residual_components"] == 1
    assert records[1]["components"]["transport"] == "timed_out"
    assert records[0]["completed_at"] is None
    assert records[1]["completed_at"]
    assert records[1]["outcome"] == "timed_out"
    assert all(record["elapsed_ms"] >= 0 for record in records)
    assert records[1]["incident_id"] == records[0]["incident_id"]


def test_cleanup_log_failure_cannot_change_cleanup_outcome():
    def fail(_):
        raise OSError("private path")
    trace = CleanupTrace("incident", fail)
    trace.mark("transport", "failed")
    trace.record("failed")
    with pytest.raises(ValueError):
        trace.mark("unknown", "completed")
    with pytest.raises(ValueError):
        trace.mark("transport", "unknown")
    with pytest.raises(ValueError):
        trace.record("unknown")


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_failure_cancellation_releases_captured_transport(monkeypatch, cancel_count):
    core = _Runtime()
    runtime = core._asr_runtime
    session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_session = session
    _install_ready_lifecycle(core, "qwen")
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_settlement(*args, **kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(runtime, "_finish_admission_invalidation", blocked_settlement)
    task = asyncio.create_task(runtime._handle_independent_asr_error(
        runtime._asr_session_epoch, "qwen", status_code="ASR_AUDIO_ORDERING_FAILED",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        async with asyncio.timeout(0.5):
            while session.close.await_count == 0:
                await asyncio.sleep(0)
        assert session.close.await_count == 1
        assert runtime._asr_session is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        runtime._asr_admission_ingress_started = False
        await runtime._asr_admission_ingress.close()
        pending = tuple(runtime._asr_close_tasks | runtime._asr_owned_cleanup_tasks)
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 3)
        await runtime._asr_audio_dispatcher.close()
        await runtime._asr_detector_dispatcher.close()


@pytest.mark.parametrize("callback_name", ["on_lifecycle", "on_status"])
async def test_failure_notification_exception_is_not_logged_completed(monkeypatch, callback_name):
    core = _Runtime()
    runtime = core._asr_runtime
    runtime._asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    _install_ready_lifecycle(core, "qwen")
    records = []
    monkeypatch.setattr(runtime, "_schedule_asr_diagnostic_metadata", lambda row, **kw: records.append(row))
    runtime._callbacks = replace(runtime._callbacks, **{
        callback_name: AsyncMock(side_effect=ValueError("PRIVATE_NOTIFICATION_ERROR")),
    })
    try:
        await runtime._handle_independent_asr_error(runtime._asr_session_epoch, "qwen")
        while runtime._asr_close_tasks or runtime._asr_owned_cleanup_tasks:
            await asyncio.wait_for(asyncio.gather(
                *tuple(runtime._asr_close_tasks | runtime._asr_owned_cleanup_tasks),
                return_exceptions=True,
            ), 2)
        assert records[-1]["components"]["notification"] == "failed"
        assert records[-1]["outcome"] == "failed"
        assert "PRIVATE_NOTIFICATION_ERROR" not in repr(records)
    finally:
        await runtime.close()
