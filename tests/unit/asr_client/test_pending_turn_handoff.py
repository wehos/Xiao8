"""Exact final handoff owns the buffered prefix and bounded Core preparation."""

import asyncio
import threading
from dataclasses import replace

import pytest
import main_logic.asr_client.runtime as runtime_module

from main_logic.asr_client._provider_events import (
    ProviderAudioRange,
    ProviderEndpointNotification,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack,
    _close_stack,
    _submit_pcm,
)


async def _pending_exact_final(runtime, core, shadow, turn, *, before_final=None):
    epoch = runtime._asr_session_epoch
    key = ProviderUtteranceKey(0, 0, 1)
    for sequence in range(1, 17):
        await _submit_pcm(runtime, turn, sequence=sequence)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0), epoch
    )

    def endpoint(phase):
        return ProviderEndpointNotification(
            phase=phase,
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 25600),
        )

    boundary = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(endpoint("boundary"), epoch)
    )
    try:
        async with asyncio.timeout(2):
            while key not in runtime._asr_provider_exact_intervals:
                assert not boundary.done()
                await asyncio.sleep(0)
        transaction = runtime._asr_provider_exact_intervals[key]
        core.continuity_score_host.ready.set()
        await boundary
    finally:
        if not boundary.done():
            boundary.cancel()
        await asyncio.gather(boundary, return_exceptions=True)
    await shadow.wait_idle()
    await runtime._handle_provider_endpoint_notification(endpoint("ordered"), epoch)
    await _submit_pcm(runtime, turn, sequence=17, owner_pcm=True)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 2, audio_start_sample_16k=25600),
        epoch,
    )
    successor = runtime._asr_provider_started_turns[ProviderUtteranceKey(0, 0, 2)]
    if before_final is not None:
        await before_final(successor)
    return (
        transaction,
        successor,
        asyncio.create_task(
            runtime._handle_provider_final(key, "synthetic first", epoch, "qwen")
        ),
    )


@pytest.mark.parametrize(
    "action",
    [
        "quick",
        "timeout",
        "rejected",
        "cancel",
        "stop",
        "close",
        "transport_invalidated",
        "cancel_ingress",
    ],
)
async def test_pending_core_preparation_has_bounded_owner(action):
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack(score=0.95)
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks = []
    epoch = runtime._asr_session_epoch

    async def prepare(*, turn_id):
        entered.set()
        try:
            await release.wait()
            if action == "rejected":
                raise RuntimeError("synthetic Core prepare failure")
            return False  # No reconnect, ordinary successful session preparation.
        finally:
            exited.set()

    core.session.prepare_external_voice_turn = prepare
    try:
        transaction, successor, final = await _pending_exact_final(
            runtime, core, shadow, turn
        )
        tasks.append(final)
        await asyncio.wait_for(entered.wait(), 2)
        audio = asyncio.create_task(_submit_pcm(runtime, successor, sequence=18))
        tasks.append(audio)
        await asyncio.sleep(0.01)
        assert not audio.done()
        assert runtime._asr_audio_dispatcher.active_turn != successor
        if action in {"quick", "rejected"}:
            release.set()
        elif action == "cancel_ingress":
            audio.cancel()
            result = (await asyncio.gather(audio, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            assert not runtime._asr_pending_turn_handoff.completion.cancelled()
            release.set()
        elif action == "cancel":
            final.cancel()
        elif action == "stop":
            await asyncio.wait_for(runtime.stop_session(), 2)
        elif action == "close":
            await asyncio.wait_for(runtime.close(), 2)
        elif action == "transport_invalidated":
            lifecycle.invalidate_transport()
            release.set()
        # Removing the prepare deadline must fail before the outer handoff
        # emergency limit; these are required preparation, not display budgets.
        prepare_budget = runtime_module._EXACT_PENDING_PREPARE_TIMEOUT_SECONDS + 0.6
        result = (
            await asyncio.wait_for(
                asyncio.gather(final, return_exceptions=True), prepare_budget
            )
        )[0]
        await asyncio.wait_for(
            asyncio.gather(transaction.drain_task, return_exceptions=True),
            prepare_budget,
        )
        await asyncio.wait_for(asyncio.gather(audio, return_exceptions=True), 1)
        await runtime.wait_transcript_idle()
        await core._voice_input_registry.wait_idle()
        assert exited.is_set()
        assert not runtime._asr_final_lock.locked()
        assert runtime._asr_pending_turn_handoff is None
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
        if action in {"quick", "cancel_ingress"}:
            assert result is None
            assert runtime._asr_session is session
            assert runtime._asr_audio_dispatcher.active_turn == successor
            # Cancelled ingress never reached Provider and is not retried.
            wire = b"".join(
                call.args[0] for call in session.stream_audio.await_args_list
            )
            assert len(wire) // 2 == (28800 if action == "quick" else 27200)
            assert wire[25600 * 2 : 27200 * 2] == b"\x22\x00" * 1600
            await runtime._handle_provider_final(
                ProviderUtteranceKey(0, 0, 2), "synthetic second", epoch, "qwen"
            )
            await runtime.wait_transcript_idle()
            await core._voice_input_registry.wait_idle()
            assert core.handle_input_transcript.await_count == 2
            assert core.session.create_response.await_count == 2
        elif action == "transport_invalidated":
            assert runtime._asr_audio_dispatcher.active_turn != successor
        else:
            assert runtime._asr_session is None
            session.close.assert_awaited_once()
            if action == "cancel":
                assert isinstance(result, asyncio.CancelledError)
        release.set()
        await asyncio.sleep(0)
        assert all(task.done() for task in tasks)
    finally:
        release.set()
        core.continuity_score_host.ready.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


async def test_ingress_already_in_detector_waits_for_exact_handoff():
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack(score=0.95)
    feed_entered = asyncio.Event()
    feed_release = threading.Event()
    notify_entered, notify_release = asyncio.Event(), asyncio.Event()
    original_feed = detector._gate.feed
    original_notify = runtime._callbacks.on_lifecycle
    loop = asyncio.get_running_loop()
    tasks = []

    def gated_feed(pcm):
        loop.call_soon_threadsafe(feed_entered.set)
        if not feed_release.wait(2):
            raise TimeoutError("synthetic feed was not released")
        return original_feed(pcm)

    async def notify(event):
        if event.state == "warm_idle":
            notify_entered.set()
            await notify_release.wait()
        await original_notify(event)

    async def before_final(successor):
        detector._gate.feed = gated_feed
        tasks.append(asyncio.create_task(_submit_pcm(runtime, successor, sequence=18)))
        await asyncio.wait_for(feed_entered.wait(), 1)

    runtime._callbacks = replace(runtime._callbacks, on_lifecycle=notify)
    try:
        transaction, successor, final = await _pending_exact_final(
            runtime,
            core,
            shadow,
            turn,
            before_final=before_final,
        )
        tasks.append(final)
        # The submit passed its entry guard before final published the gate.
        async with asyncio.timeout(1):
            while runtime._asr_pending_turn_handoff is None:
                await asyncio.sleep(0)
        feed_release.set()
        await asyncio.wait_for(notify_entered.wait(), 1)
        await asyncio.sleep(0.01)
        assert not tasks[0].done()
        notify_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        await runtime._asr_audio_dispatcher.wait_idle()
        assert runtime._asr_session is session
        assert runtime._asr_audio_dispatcher.active_turn == successor
        wire = b"".join(call.args[0] for call in session.stream_audio.await_args_list)
        assert len(wire) // 2 == 28800
        assert wire[25600 * 2 : 27200 * 2] == b"\x22\x00" * 1600
        assert transaction.drain_task.done()
        assert not runtime._asr_final_lock.locked()
        assert runtime._asr_pending_turn_handoff is None
    finally:
        feed_release.set()
        notify_release.set()
        detector._gate.feed = original_feed
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)
