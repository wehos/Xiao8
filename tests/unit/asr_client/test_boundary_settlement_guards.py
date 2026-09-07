import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client import boundary_settlement as diagnostics
from main_logic.asr_client._provider_events import ProviderAudioRange, ProviderEndpointNotification
from tests.unit.test_asr_phase3_session import (
    _RealtimeAsrSessionImpl, _AsrWorkerEvent, AsrSessionConfig,
    _recording_worker, _drain_session_pipelines,
)


async def _session(callback):
    finals = []
    async def final(key, text):
        finals.append(key)
    session = _RealtimeAsrSessionImpl(worker_fn=_recording_worker, api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(), on_connection_error=AsyncMock(),
        on_provider_endpoint=callback, on_provider_final=final)
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1000)
    common = dict(generation=0, buffer_epoch=0, utterance_id=1)
    await session._response_queue.put(_AsrWorkerEvent(kind="utterance_started", **common))
    await _drain_session_pipelines(session)
    await session._response_queue.put(_AsrWorkerEvent(kind="provider_endpoint",
        boundary_quality="exact", audio_range=ProviderAudioRange(0,1000), **common))
    await asyncio.wait_for(session._response_queue.join(), 1)
    return session, common, finals


@pytest.mark.parametrize("failure", ["error", "cancel", "timeout", "late_after_cancel"])
async def test_failed_callbacks_cannot_become_success_on_late_read(failure, monkeypatch):
    records = []
    monkeypatch.setattr(diagnostics, "submit_resolution_log", lambda row: records.append(row) or object())
    release, entered = asyncio.Event(), asyncio.Event()
    ordered = []
    async def callback(n):
        if n.phase == "ordered":
            ordered.append(n.boundary_quality)
            return
        entered.set()
        if failure == "error":
            raise RuntimeError("synthetic callback failure")
        if failure == "cancel":
            raise asyncio.CancelledError
        try:
            await release.wait()
        except asyncio.CancelledError:
            if failure != "late_after_cancel":
                raise
            await release.wait()
    session, common, finals = await _session(callback)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(.25)
        release.set()
        await asyncio.sleep(.01)
        await session._response_queue.put(_AsrWorkerEvent(kind="final", text="private fixture", **common))
        await _drain_session_pipelines(session)
        assert ordered == ["unknown"]
        assert len(finals) == 1
        assert not session._provider_boundary_tasks
        assert "private fixture" not in str(records)
        consumed = [r for r in records if r["stage"] == "provider_boundary_consumed"]
        assert consumed and consumed[-1]["disposition"] != "accepted"
        assert consumed[-1]["final_received_at_monotonic"] is not None
        if failure == "late_after_cancel":
            completed = [r for r in records if r["stage"] == "provider_boundary_callback_completed"]
            assert completed[-1]["boundary_completed_at_monotonic"] > completed[-1]["boundary_deadline_monotonic"]
    finally:
        release.set()
        await session.close()
        assert not session._provider_boundary_chain_tasks


@pytest.mark.parametrize("action", ["clear", "close", "revoke"])
async def test_completed_receipt_cannot_survive_revocation(action):
    session, common, finals = await _session(AsyncMock())
    try:
        task = session._provider_boundary_tasks[(0,0,1)]
        assert await task
        if action == "clear":
            await session.clear_audio_buffer()
        elif action == "close":
            await session.close()
        else:
            session._revoke_provider_boundary_chain()
        assert not await session._wait_provider_boundary_callback((0,0,1))
        assert not session._provider_boundary_tasks
        assert finals == []
    finally:
        await session.close()


@pytest.mark.parametrize("retire", [None, "cancel", "clear", "close", "revoke", "replace"])
async def test_cancelled_predecessor_only_releases_current_successor(retire):
    entered, release = asyncio.Event(), asyncio.Event()
    events, finals = [], []

    async def endpoint(notification):
        events.append((notification.phase, notification.utterance_id,
                       notification.boundary_quality))
        if notification.phase == "boundary" and notification.utterance_id == 1:
            entered.set()
            await release.wait()
            raise asyncio.CancelledError

    async def final(key, text):
        finals.append(key)

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker, api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(), on_connection_error=AsyncMock(),
        on_provider_endpoint=endpoint, on_provider_final=final,
    )
    try:
        await session.connect()
        await session.stream_audio(b"\x00\x00" * 2000)
        for utterance in (1, 2):
            common = dict(generation=0, buffer_epoch=0, utterance_id=utterance)
            await session._response_queue.put(_AsrWorkerEvent(
                kind="utterance_started", audio_start_sample_16k=(utterance-1)*1000,
                **common,
            ))
            await session._response_queue.put(_AsrWorkerEvent(
                kind="provider_endpoint", boundary_quality="exact",
                audio_range=ProviderAudioRange((utterance-1)*1000, utterance*1000),
                **common,
            ))
        await asyncio.wait_for(session._response_queue.join(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        tasks = tuple(session._provider_boundary_tasks.values())
        successor = session._provider_boundary_tasks[(0, 0, 2)]
        if retire == "cancel":
            successor.cancel()
        elif retire == "clear":
            await session.clear_audio_buffer()
        elif retire == "close":
            await session.close()
        elif retire == "revoke":
            session._revoke_provider_boundary_chain()
        elif retire == "replace":
            # A conflicting endpoint replaces only this key's queued proof.
            await session._response_queue.put(_AsrWorkerEvent(
                kind="provider_endpoint", generation=0, buffer_epoch=0,
                utterance_id=2, boundary_quality="exact",
                audio_range=ProviderAudioRange(1000, 1900),
            ))
            await asyncio.wait_for(session._response_queue.join(), 1)
            replacement = session._provider_boundary_tasks[(0, 0, 2)]
            assert replacement is not successor
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
        if retire == "replace":
            assert await successor
            assert await asyncio.wait_for(asyncio.shield(replacement), 1)
        elif retire is not None:
            assert successor.cancelled()
            assert events == [("boundary", 1, "exact")]
            assert finals == []
            return
        for utterance in (1, 2):
            # Duplicate provider finals must not cause duplicate delivery.
            for _ in range(2):
                await session._response_queue.put(_AsrWorkerEvent(
                    kind="final", generation=0, buffer_epoch=0,
                    utterance_id=utterance, text="fixture",
                ))
        await _drain_session_pipelines(session)
        assert [(key.generation, key.buffer_epoch, key.utterance_id)
                for key in finals] == [(0, 0, 1), (0, 0, 2)]
        successor_quality = "unknown" if retire == "replace" else "exact"
        expected = [("boundary", 1, "exact"), ("boundary", 2, "exact")]
        if retire == "replace":
            expected.append(("boundary", 2, "unknown"))
        expected.extend([
            ("ordered", 1, "unknown"), ("ordered", 2, successor_quality),
        ])
        assert events == expected
        assert not session._provider_boundary_tasks
        assert not session._provider_boundary_deadlines
    finally:
        release.set()
        await session.close()
        assert not session._provider_boundary_chain_tasks
        assert not session._provider_boundary_retired_tasks


async def test_waiter_cancel_is_propagated_and_close_reaps_callback():
    entered, release = asyncio.Event(), asyncio.Event()
    async def callback(n):
        entered.set()
        await release.wait()
    session, _, _ = await _session(callback)
    waiter = None
    try:
        await entered.wait()
        waiter = asyncio.create_task(session._wait_provider_boundary_callback((0,0,1)))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        release.set()
        await session.close()
        assert not session._provider_boundary_chain_tasks


async def test_completed_result_does_not_extend_final_admission_deadline():
    session, _, _ = await _session(AsyncMock())
    try:
        assert await session._provider_boundary_tasks[(0,0,1)]
        assert not await session._wait_provider_boundary_callback((0,0,1), not_after=time.monotonic()-1)
    finally:
        await session.close()


async def test_success_diagnostics_distinguish_completion_from_late_consumption(monkeypatch):
    records = []
    monkeypatch.setattr(diagnostics, "submit_resolution_log", lambda row: records.append(row) or object())
    session, common, _ = await _session(AsyncMock())
    try:
        assert await session._provider_boundary_tasks[(0,0,1)]
        await asyncio.sleep(.25)
        await session._response_queue.put(_AsrWorkerEvent(kind="final", text="private fixture", **common))
        await _drain_session_pipelines(session)
        row = [r for r in records if r["stage"] == "provider_boundary_consumed"][-1]
        assert row["disposition"] == "accepted"
        assert row["boundary_started_at_monotonic"] <= row["boundary_completed_at_monotonic"]
        assert row["boundary_completed_at_monotonic"] <= row["boundary_deadline_monotonic"]
        assert row["boundary_deadline_monotonic"] < row["final_received_at_monotonic"]
        assert row["final_received_at_monotonic"] <= row["boundary_consumed_at_monotonic"]
        assert len({r["diagnostic_transport_ref"] for r in records}) == 1
        assert "private fixture" not in str(records)
    finally:
        await session.close()


@pytest.mark.parametrize("writer_failure", ["full", "exception"])
async def test_diagnostics_failure_cannot_change_admission(writer_failure, monkeypatch):
    def write(_):
        if writer_failure == "exception":
            raise OSError("synthetic log fault")
        return None
    monkeypatch.setattr(diagnostics, "submit_resolution_log", write)
    session, _, _ = await _session(AsyncMock())
    try:
        task = session._provider_boundary_tasks[(0,0,1)]
        result = await task
        assert result.records_dropped > 0
        await asyncio.sleep(.25)
        assert await session._wait_provider_boundary_callback((0,0,1))
    finally:
        await session.close()


async def test_old_waiter_cannot_consume_or_remove_replacement_task():
    release = asyncio.Event()
    async def callback(n):
        await release.wait()
    session, _, _ = await _session(callback)
    key = (0,0,1)
    original = session._provider_boundary_tasks[key]
    def replace_completed(_):
        session._revoke_provider_boundary_chain()
        session._schedule_provider_boundary_callback(key, ProviderEndpointNotification(
            phase="boundary", generation=0, buffer_epoch=0, utterance_id=1,
            boundary_quality="exact", audio_range=ProviderAudioRange(0,1000)))
    original.add_done_callback(replace_completed)
    waiter = asyncio.create_task(session._wait_provider_boundary_callback(key))
    try:
        await asyncio.sleep(0)
        release.set()
        assert not await waiter
        replacement = session._provider_boundary_tasks[key]
        assert replacement is not original
        assert await replacement
        assert await session._wait_provider_boundary_callback(key)
    finally:
        release.set()
        await session.close()


async def test_twenty_settled_delayed_finals_keep_task_tables_bounded():
    ordered, finals = [], []
    async def endpoint(n):
        if n.phase == "ordered":
            ordered.append((n.key.utterance_id, n.boundary_quality))
    async def final(key, text):
        finals.append(key.utterance_id)
    session = _RealtimeAsrSessionImpl(worker_fn=_recording_worker, api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(), on_connection_error=AsyncMock(),
        on_provider_endpoint=endpoint, on_provider_final=final)
    try:
        await session.connect()
        for utterance in range(1,21):
            common = dict(generation=0, buffer_epoch=0, utterance_id=utterance)
            await session.stream_audio(b"\x00\x00" * 1000)
            await session._response_queue.put(_AsrWorkerEvent(kind="utterance_started",
                audio_start_sample_16k=(utterance-1)*1000, **common))
            await session._response_queue.put(_AsrWorkerEvent(kind="provider_endpoint",
                boundary_quality="exact", audio_range=ProviderAudioRange((utterance-1)*1000,utterance*1000), **common))
            await _drain_session_pipelines(session)
            assert await session._provider_boundary_tasks[(0,0,utterance)]
            await asyncio.sleep(.22)
            await session._response_queue.put(_AsrWorkerEvent(kind="final",text="fixture",**common))
            await _drain_session_pipelines(session)
            assert not session._provider_boundary_tasks
            assert not session._provider_boundary_deadlines
            assert not session._provider_boundary_chain_tasks
        assert finals == list(range(1,21))
        assert ordered == [(i,"exact") for i in range(1,21)]
    finally:
        await session.close()
