"""Admission invalidation owners remain bounded and visible after cancellation."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from main_logic.asr_client import runtime as runtime_module
from tests.unit.test_core_independent_asr import _Runtime, _install_ready_lifecycle


async def _drain(runtime):
    pending = tuple(runtime._asr_close_tasks)
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 1)
    await runtime._asr_audio_dispatcher.close()
    await runtime._asr_detector_dispatcher.close()


async def test_admission_owner_survives_caller_cancel_and_settles_once():
    runtime = _Runtime()._asr_runtime
    future = asyncio.get_running_loop().create_future()
    dispatcher = runtime._asr_transcript_dispatcher
    old_invalidate = dispatcher.invalidate_all
    dispatcher.invalidate_all = Mock(wraps=old_invalidate)
    settled = []
    task = asyncio.create_task(runtime._finish_admission_invalidation(
        future, dispatcher, None, None, None, on_settled=settled.append,
    ))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=.2)
        assert task in done, "caller cancellation waited for the hidden owner"
        with pytest.raises(asyncio.CancelledError):
            task.result()
        assert not future.cancelled()
        owners = [item for item in runtime._asr_close_tasks
                  if item.get_name() == "voice-turn-admission-invalidation-owner"]
        assert len(owners) == 1 and not owners[0].done()
        assert settled == []
        future.set_result(())
        await _drain(runtime)
        assert settled == owners
        assert settled[0].exception() is None
        dispatcher.invalidate_all.assert_called_once()
    finally:
        if not future.done():
            future.set_result(())
        await asyncio.gather(task, return_exceptions=True)
        await _drain(runtime)


async def test_admission_deadline_cancels_owner_without_cancelling_ingress(monkeypatch):
    monkeypatch.setattr(runtime_module, "_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS", .03)
    runtime = _Runtime()._asr_runtime
    future = asyncio.get_running_loop().create_future()
    settled = []
    try:
        with pytest.raises(TimeoutError, match="ASR_ADMISSION_INVALIDATION_TIMEOUT"):
            await runtime._finish_admission_invalidation(
                future, runtime._asr_transcript_dispatcher, None, None, None,
                on_settled=settled.append,
            )
        await asyncio.sleep(0)
        assert not future.cancelled()
        assert len(settled) == 1 and settled[0].cancelled()
    finally:
        future.set_result(())
        await _drain(runtime)


async def test_cancellation_resistant_admission_owner_stays_tracked_after_deadline(monkeypatch):
    monkeypatch.setattr(runtime_module, "_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS", .03)
    runtime = _Runtime()._asr_runtime
    future = asyncio.get_running_loop().create_future()
    future.set_result(())
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = runtime._retire_admission_boundary_proofs

    async def hold_after_actual_retirement(*args):
        await original(*args)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    monkeypatch.setattr(runtime, "_retire_admission_boundary_proofs", hold_after_actual_retirement)
    correlator = SimpleNamespace(retire_namespace=lambda _: SimpleNamespace(retired_proofs=()))
    settled = []
    task = asyncio.create_task(runtime._finish_admission_invalidation(
        future, runtime._asr_transcript_dispatcher, correlator, (1, 1), None,
        on_settled=settled.append,
    ))
    try:
        await asyncio.wait_for(entered.wait(), .5)
        done, _ = await asyncio.wait({task}, timeout=.2)
        assert task in done, "deadline joined a cancellation-resistant owner"
        with pytest.raises(TimeoutError):
            task.result()
        await asyncio.wait_for(cancelled.wait(), .2)
        owners = [item for item in runtime._asr_close_tasks if not item.done()]
        assert len(owners) == 1
        assert owners[0].get_name() == "voice-turn-admission-invalidation-owner"
        assert settled == [], "timeout must not claim actual settlement"
        release.set()
        await _drain(runtime)
        assert settled == owners
        assert settled[0].exception() is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await _drain(runtime)


async def test_failure_callback_can_await_real_stop_session_without_self_join():
    core = _Runtime()
    runtime = core._asr_runtime
    closes = []
    callbacks = []

    async def close_transport():
        closes.append(True)

    async def stop_on_failure(*args, **kwargs):
        callbacks.append(True)
        await runtime.stop_session()

    runtime._callbacks = replace(runtime._callbacks, on_failure=stop_on_failure)
    runtime._asr_session = SimpleNamespace(is_ready=True, close=close_transport)
    _install_ready_lifecycle(core, "qwen")
    try:
        await asyncio.wait_for(runtime._handle_independent_asr_error(
            runtime._asr_session_epoch, "qwen", status_code="ASR_AUDIO_ORDERING_FAILED",
        ), 2)
        assert callbacks == [True]
        assert closes == [True]
        assert runtime._asr_session is None
    finally:
        await runtime.close()
