"""Exact handoff budgets must include Core ingress and failure notification I/O."""

import asyncio
import json
from dataclasses import replace

import pytest

from main_logic.asr_client._provider_events import ProviderUtteranceKey
from tests.unit.asr_client.test_pending_turn_handoff import _pending_exact_final
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack,
    _close_stack,
)


@pytest.mark.parametrize("prepare_delay,frame_ms,duration_ms", [(0.05, 100, 2400), (2.7, 100, 2400), (4.0, 10, 3600)])
async def test_real_ingress_preserves_audio_during_handoff(prepare_delay, frame_ms, duration_ms):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=0.95)
    entered = asyncio.Event()
    epoch = runtime._asr_session_epoch
    tasks = []

    def fire(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    core._fire_task = fire
    core._hot_swap_ingress_sequence = 17

    async def prepare(*, turn_id):
        entered.set()
        await asyncio.sleep(prepare_delay)
        return False

    core.session.prepare_external_voice_turn = prepare
    try:
        transaction, successor, final = await _pending_exact_final(runtime, core, shadow, turn)
        tasks.append(final)
        await asyncio.wait_for(entered.wait(), 2)
        assert runtime.has_pending_turn_handoff(turn.ingress)
        assert not runtime.has_pending_turn_handoff(replace(turn.ingress, audio_generation=999))
        samples = frame_ms * 16
        frames = duration_ms // frame_ms
        next_capture = asyncio.get_running_loop().time()
        # Use real enqueue -> worker -> normalization -> route -> submit.
        for sequence in range(frames):
            await core._enqueue_audio_stream_data({
                "input_type": "audio", "sample_rate_hz": 16000,
                "data": [100 + sequence] * samples,
            })
            next_capture += frame_ms / 1000
            await asyncio.sleep(max(0, next_capture - asyncio.get_running_loop().time()))
        await asyncio.wait_for(asyncio.gather(final), 7)
        async with asyncio.timeout(2):
            while core._hot_swap_pending_sequences:
                await asyncio.sleep(0.001)
        await runtime._asr_audio_dispatcher.wait_idle()
        wire = b"".join(c.args[0] for c in session.stream_audio.await_args_list)
        expected = b"\x22\x00" * 1600 + b"".join(
            (100 + sequence).to_bytes(2, "little") * samples for sequence in range(frames)
        )
        assert core._audio_stream_dropped_total == 0
        assert runtime._asr_session is session
        assert len(wire) // 2 == 27200 + duration_ms * 16
        assert wire[25600 * 2:] == expected
        for key in (ProviderUtteranceKey(0, 0, 2), ProviderUtteranceKey(0, 0, 2)):
            await runtime._handle_provider_final(key, "synthetic second", epoch, "qwen")
            await runtime.wait_transcript_idle()
            await core._voice_input_registry.wait_idle()
            assert core.handle_input_transcript.await_count == 2
            assert core.session.create_response.await_count == 2
        assert transaction.drain_task.done()
        assert runtime._asr_pending_turn_handoff is None
        assert not runtime.has_pending_turn_handoff(turn.ingress)
        assert core._audio_stream_queue.capacity_us == 2_000_000
        assert core._audio_stream_queue.maxsize == 256
        session.close.assert_not_awaited()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


@pytest.mark.parametrize("hold_notice,stop_during_notice,notice_code", [
    (False, True, "ASR_LIFECYCLE_STATE"),
    (True, True, "ASR_LIFECYCLE_STATE"),
    (True, False, "ASR_LIFECYCLE_STATE"),
    (True, True, "ASR_CORE_TRANSCRIPT_BACKPRESSURE"),
])
async def test_handoff_failure_notice_cannot_hold_session_stop(hold_notice, stop_during_notice, notice_code):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=0.95)
    prepared, release_prepare = asyncio.Event(), asyncio.Event()
    notified, release_notice = asyncio.Event(), asyncio.Event()
    notice_exited = asyncio.Event()
    original_status = core.send_status
    tasks = []

    async def prepare(*, turn_id):
        prepared.set()
        await release_prepare.wait()
        return False

    async def status(message, *args, **kwargs):
        value = json.loads(message)
        if value.get("code") == notice_code and (
            notice_code != "ASR_LIFECYCLE_STATE" or value.get("details", {}).get("state") == "blocked"
        ):
            notified.set()
            try:
                if hold_notice:
                    await release_notice.wait()
            finally:
                notice_exited.set()
        await original_status(message, *args, **kwargs)

    core.session.prepare_external_voice_turn = prepare
    core.send_status = status
    try:
        transaction, successor, final = await _pending_exact_final(runtime, core, shadow, turn)
        tasks.append(final)
        await asyncio.wait_for(prepared.wait(), 2)
        final.cancel()
        result = (await asyncio.wait_for(asyncio.gather(final, return_exceptions=True), 2))[0]
        assert isinstance(result, asyncio.CancelledError)
        await asyncio.wait_for(notified.wait(), 7)
        if not stop_during_notice:
            async with asyncio.timeout(3):
                while runtime._asr_owned_cleanup_tasks:
                    await asyncio.sleep(0.001)
            # Even when no client consumes BLOCKED, Core must stop the route.
            assert core._asr_route_mode == "blocked"
        stop = asyncio.create_task(runtime.stop_session())
        tasks.append(stop)
        done, _ = await asyncio.wait({stop}, timeout=2)
        assert stop in done
        await stop
        assert notice_exited.is_set()
        assert not core._asr_notification_lock.locked()
        assert not runtime._asr_final_lock.locked()
        assert not runtime._asr_owned_cleanup_tasks
        session.close.assert_awaited_once()
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
    finally:
        release_prepare.set()
        release_notice.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        await _close_stack(core)


@pytest.mark.parametrize("action", ["stop", "close"])
async def test_handoff_backlog_is_retired_without_replay(action):
    core, runtime, detector, shadow, lifecycle, session, turn = await _active_real_stack(score=0.95)
    prepared, release = asyncio.Event(), asyncio.Event()
    tasks = []

    def fire(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    core._fire_task = fire
    core._hot_swap_ingress_sequence = 17

    async def prepare(*, turn_id):
        prepared.set()
        await release.wait()
        return False

    core.session.prepare_external_voice_turn = prepare
    try:
        transaction, successor, final = await _pending_exact_final(runtime, core, shadow, turn)
        tasks.append(final)
        await asyncio.wait_for(prepared.wait(), 2)
        for _ in range(24):
            await core._enqueue_audio_stream_data({
                "input_type": "audio", "sample_rate_hz": 16000, "data": [99] * 1600,
            })
            await asyncio.sleep(0)
        assert core._audio_stream_queue.duration_us > 2_000_000
        assert core._audio_stream_dropped_total == 0
        await asyncio.wait_for(runtime.close() if action == "close" else runtime.stop_session(), 2)
        release.set()
        async with asyncio.timeout(2):
            while core._hot_swap_pending_sequences:
                await asyncio.sleep(0.001)
        await asyncio.gather(final, return_exceptions=True)
        assert not runtime.has_pending_turn_handoff(turn.ingress)
        assert core._audio_stream_queue.capacity_us == 2_000_000
        assert core._audio_stream_queue.maxsize == 256
        assert b"".join(c.args[0] for c in session.stream_audio.await_args_list) == b"\x21\x00" * 25600
        await runtime._handle_provider_final(ProviderUtteranceKey(0, 0, 2), "stale second", turn.ingress.session_epoch, "qwen")
        await runtime.wait_transcript_idle()
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
        session.close.assert_awaited_once()
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)
