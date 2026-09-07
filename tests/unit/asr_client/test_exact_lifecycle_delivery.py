"""Optional lifecycle delivery must not consume accepted exact finals."""

import asyncio
from dataclasses import replace

import pytest

from main_logic.asr_client._provider_events import (
    ProviderAudioRange,
    ProviderEndpointNotification,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from main_logic.asr_client.admission.contracts import AdmissionDisposition
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack,
    _close_stack,
    _submit_pcm,
)


@pytest.mark.parametrize("state", ["draining", "warm_idle"])
@pytest.mark.parametrize(
    "action", ["quick", "timeout", "cancel", "error", "stop", "close"]
)
@pytest.mark.parametrize("score", [0.95, 0.20])
async def test_exact_final_survives_optional_notification(state, action, score):
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack(score=score)
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = runtime._callbacks.on_lifecycle
    tasks = []
    held = False
    callback_cancellations = []

    async def callback(event):
        if event.state == state:
            entered.set()
            try:
                await release.wait()
                if action == "error":
                    raise RuntimeError("synthetic notification delivery failure")
            except asyncio.CancelledError:
                callback_cancellations.append(event.state)
                raise
            finally:
                exited.set()
        await original(event)

    runtime._callbacks = replace(runtime._callbacks, on_lifecycle=callback)
    key = ProviderUtteranceKey(0, 0, 1)
    epoch = runtime._asr_session_epoch

    def notification(phase):
        return ProviderEndpointNotification(
            phase=phase,
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 25600),
        )

    try:
        for sequence in range(1, 17):
            await _submit_pcm(runtime, turn, sequence=sequence)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            epoch,
        )
        # Real final-lock contention exposes the supported pending callback FIFO.
        await runtime._asr_final_lock.acquire()
        held = True
        boundary = asyncio.create_task(
            runtime._handle_provider_endpoint_notification(
                notification("boundary"), epoch
            )
        )
        tasks.append(boundary)
        async with asyncio.timeout(2):
            while key not in runtime._asr_provider_exact_pending:
                assert not boundary.done()
                await asyncio.sleep(0)
        pending = runtime._asr_provider_exact_pending[key]
        await runtime._handle_provider_final(
            key, "synthetic accepted final", epoch, "qwen"
        )
        await runtime._handle_provider_endpoint_notification(
            notification("ordered"), epoch
        )
        assert len(pending.deferred) == 2
        runtime._asr_final_lock.release()
        held = False
        core.continuity_score_host.ready.set()
        await asyncio.wait_for(entered.wait(), 2)
        transaction = runtime._asr_provider_exact_intervals[key]
        assert transaction.provider_key == key

        if action in {"quick", "error"}:
            release.set()
        elif action == "cancel":
            # Cancel while the accepted replay is inside the optional callback.
            # The child must settle the final before cancellation propagates.
            boundary.cancel()
        elif action == "stop":
            await asyncio.wait_for(runtime.stop_session(), 2)
        elif action == "close":
            await asyncio.wait_for(runtime.close(), 2)

        result = (
            await asyncio.wait_for(asyncio.gather(boundary, return_exceptions=True), 2)
        )[0]
        release.set()
        if transaction.drain_task is not None:
            await asyncio.wait_for(
                asyncio.gather(transaction.drain_task, return_exceptions=True), 2
            )
        await shadow.wait_idle()
        await runtime.wait_transcript_idle()
        await core._voice_input_registry.wait_idle()
        assert exited.is_set()
        assert not runtime._asr_final_lock.locked()
        assert not runtime._asr_exact_callback_tasks

        if action in {"stop", "close"}:
            assert runtime._asr_session is None
            assert session.close.await_count == 1
            assert callback_cancellations == [state]
            delivered = int(state == "warm_idle" and score > 0.4)
            assert core.handle_input_transcript.await_count == delivered
            return

        if action == "cancel":
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result is None
        if action in {"timeout", "cancel"}:
            assert callback_cancellations == [state]
        assert not pending.deferred
        assert runtime._asr_session is session
        session.close.assert_not_awaited()
        assert lifecycle.snapshot.state.value == "warm_idle"
        expected = int(score > 0.4)
        assert core.handle_input_transcript.await_count == expected
        assert core.session.create_response.await_count == expected
        assert transaction.correlator.is_completed(key)
        assert transaction.resolved_disposition is (
            AdmissionDisposition.FORWARD if expected else AdmissionDisposition.DROP
        )
        assert key not in runtime._asr_provider_exact_intervals
        # An already accepted key cannot be redelivered after notification expiry.
        await runtime._handle_provider_final(key, "synthetic duplicate", epoch, "qwen")
        await runtime.wait_transcript_idle()
        await core._voice_input_registry.wait_idle()
        assert core.handle_input_transcript.await_count == expected
    finally:
        release.set()
        core.continuity_score_host.ready.set()
        if held:
            runtime._asr_final_lock.release()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


@pytest.mark.parametrize("state", ["warm_idle", "active"])
@pytest.mark.parametrize("audio_during_handoff", [False, True])
@pytest.mark.parametrize(
    "action", ["quick", "timeout", "cancel", "stop", "close", "transport_invalidated"]
)
async def test_exact_notification_timeout_preserves_pending_turn_authority(
    state, action, audio_during_handoff
):
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack(score=0.95)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime._callbacks.on_lifecycle
    states = []
    notification_exited = asyncio.Event()
    tasks = []
    epoch = runtime._asr_session_epoch
    key = ProviderUtteranceKey(0, 0, 1)
    successor_key = ProviderUtteranceKey(0, 0, 2)

    async def callback(event):
        if event.state == state:
            entered.set()
            try:
                await release.wait()
            finally:
                notification_exited.set()
        states.append(event.state)
        await original(event)

    runtime._callbacks = replace(runtime._callbacks, on_lifecycle=callback)

    def notification(phase):
        return ProviderEndpointNotification(
            phase=phase,
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 25600),
        )

    try:
        for sequence in range(1, 17):
            await _submit_pcm(runtime, turn, sequence=sequence)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            epoch,
        )
        boundary = asyncio.create_task(
            runtime._handle_provider_endpoint_notification(
                notification("boundary"),
                epoch,
            )
        )
        tasks.append(boundary)
        async with asyncio.timeout(2):
            while key not in runtime._asr_provider_exact_intervals:
                assert not boundary.done()
                await asyncio.sleep(0)
        transaction = runtime._asr_provider_exact_intervals[key]
        core.continuity_score_host.ready.set()
        await boundary
        await shadow.wait_idle()
        await runtime._handle_provider_endpoint_notification(
            notification("ordered"), epoch
        )
        await _submit_pcm(runtime, turn, sequence=17, owner_pcm=True)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 2, audio_start_sample_16k=25600),
            epoch,
        )
        successor_turn = runtime._asr_provider_started_turns[successor_key]
        assert lifecycle.pending_turn_token == successor_turn
        final = asyncio.create_task(
            runtime._handle_provider_final(
                key,
                "synthetic first final",
                epoch,
                "qwen",
            )
        )
        tasks.append(final)
        await asyncio.wait_for(entered.wait(), 2)
        live_audio = None
        if audio_during_handoff:
            live_audio = asyncio.create_task(
                _submit_pcm(runtime, successor_turn, sequence=18)
            )
            tasks.append(live_audio)
            # Let ingress run while the notification is still suspended.
            await asyncio.sleep(0.01)
        if action == "quick":
            release.set()
        elif action == "cancel":
            final.cancel()
        elif action == "stop":
            await asyncio.wait_for(runtime.stop_session(), 2)
        elif action == "close":
            await asyncio.wait_for(runtime.close(), 2)
        elif action == "transport_invalidated":
            # Exercise the real lifecycle generation fence while notification
            # delivery is suspended, without mocking the activation method.
            lifecycle.invalidate_transport()
        result = (
            await asyncio.wait_for(asyncio.gather(final, return_exceptions=True), 2)
        )[0]
        await asyncio.wait_for(
            asyncio.gather(transaction.drain_task, return_exceptions=True),
            2,
        )
        await runtime.wait_transcript_idle()
        await core._voice_input_registry.wait_idle()
        if live_audio is not None:
            await asyncio.wait_for(live_audio, 2)
        assert core.handle_input_transcript.await_count == 1
        assert notification_exited.is_set()
        assert not runtime._asr_final_lock.locked()
        if action == "transport_invalidated":
            assert result is None
            assert not runtime._asr_turn_prepared
            assert runtime._asr_audio_dispatcher.active_turn != successor_turn
            assert "active" not in states
        elif action in {"stop", "close"}:
            assert runtime._asr_session is None
            assert "active" not in states
            session.close.assert_awaited_once()
        else:
            if action == "cancel":
                assert isinstance(result, asyncio.CancelledError)
            else:
                assert result is None
            assert runtime._asr_session is session
            session.close.assert_not_awaited()
            assert lifecycle.current_turn_token == successor_turn
            assert lifecycle.snapshot.state.value == "active"
            assert runtime._asr_turn_prepared
            assert runtime._asr_audio_dispatcher.active_turn == successor_turn
            assert states.count("active") == int(state != "active" or action == "quick")
            assert (
                runtime._asr_provider_speaker_evidence_lease
                == transaction.successor_evidence_lease
            )
            assert successor_key in runtime._asr_provider_started_turns
            assert key not in runtime._asr_provider_exact_intervals
            # Exercise the prepared successor, not just its lifecycle label.
            await _submit_pcm(
                runtime, successor_turn, sequence=19 if audio_during_handoff else 18
            )
            await runtime._asr_audio_dispatcher.wait_idle()
            assert sum(
                len(call.args[0]) // 2 for call in session.stream_audio.await_args_list
            ) == (30400 if audio_during_handoff else 28800)
            assert (
                sum(
                    call.args[0].count(b"\x22\x00")
                    for call in session.stream_audio.await_args_list
                )
                == 1600
            )
            await runtime._handle_provider_final(
                successor_key,
                "synthetic second final",
                epoch,
                "qwen",
            )
            await runtime.wait_transcript_idle()
            await core._voice_input_registry.wait_idle()
            assert core.handle_input_transcript.await_count == 2
            assert core.session.create_response.await_count == 2
            await runtime._handle_provider_final(
                key, "synthetic late duplicate", epoch, "qwen"
            )
            await runtime.wait_transcript_idle()
            assert core.handle_input_transcript.await_count == 2
    finally:
        release.set()
        core.continuity_score_host.ready.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)
