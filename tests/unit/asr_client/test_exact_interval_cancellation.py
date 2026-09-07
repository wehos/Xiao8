"""Real ASR state machines under cancellation; synthetic IO and scoring only."""

import asyncio
from dataclasses import replace

import pytest

from main_logic.asr_client._provider_events import (
    ProviderAudioRange,
    ProviderEndpointNotification,
    ProviderUtteranceStartedNotification,
    ProviderUtteranceKey,
)
from main_logic.asr_client.admission.contracts import ProviderFinalReceived
from tests.unit.asr_client.test_provider_speaker_continuity import (
    _active_real_stack,
    _submit_pcm,
    _close_stack,
)


@pytest.mark.parametrize(
    "operation", ["promote_exact_interval_nowait", "activate_exact_interval_nowait"]
)
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_setup_reconciles_accepted_admission_result(
    monkeypatch, operation, cancel_count
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
    receipt_tasks = []
    boundary = None
    original = getattr(runtime._asr_admission_ingress, operation)

    def delay_receipt(argument):
        accepted = original(argument)

        async def deliver():
            # The real Admission FIFO commits first; only receipt delivery is
            # paused, never promotion/activation or its compensating abort.
            result = await accepted
            entered.set()
            await release.wait()
            return result

        receipt = asyncio.create_task(deliver())
        receipt_tasks.append(receipt)
        return receipt

    monkeypatch.setattr(runtime._asr_admission_ingress, operation, delay_receipt)
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        for sequence in range(1, 17):
            await _submit_pcm(runtime, turn, sequence=sequence)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        boundary = asyncio.create_task(
            runtime._handle_provider_endpoint_notification(
                ProviderEndpointNotification(
                    "boundary", 0, 0, 1, "exact", ProviderAudioRange(0, 25600)
                ),
                runtime._asr_session_epoch,
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert runtime._asr_admission._exact_interval_records
        boundary.cancel()
        await asyncio.sleep(0)
        if cancel_count == 2:
            boundary.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(boundary, 2)
        child = await runtime._asr_admission.get_record(turn)
        assert child is not None
        assert child.exact_interval_hold_id is None
        assert not runtime._asr_admission._exact_interval_records
        assert key not in runtime._asr_provider_exact_pending
        assert not runtime._asr_final_lock.locked()
        session.close.assert_not_awaited()
    finally:
        release.set()
        core.continuity_score_host.ready.set()
        if boundary is not None and not boundary.done():
            boundary.cancel()
        await asyncio.gather(
            *receipt_tasks, *([boundary] if boundary else []), return_exceptions=True
        )
        await _close_stack(core)


def awaiting_names(task):
    names = []
    current = task.get_coro()
    while current is not None:
        code = getattr(current, "cr_code", None)
        if code is not None:
            names.append(code.co_name)
        current = getattr(current, "cr_await", None)
    return names


@pytest.mark.parametrize("cancel_count", [0, 1, 2])
async def test_precommit_cancellation_releases_lock_and_pending(cancel_count):
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack(score=0.95)
    boundary = None
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        for sequence in range(1, 17):
            await _submit_pcm(runtime, turn, sequence=sequence)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        # Observe natural task scheduling; do not hold private coordinator locks.
        boundary = asyncio.create_task(
            runtime._handle_provider_endpoint_notification(
                ProviderEndpointNotification(
                    phase="boundary",
                    generation=0,
                    buffer_epoch=0,
                    utterance_id=1,
                    boundary_quality="exact",
                    audio_range=ProviderAudioRange(0, 25600),
                ),
                runtime._asr_session_epoch,
            )
        )
        async with asyncio.timeout(2):
            while (
                key not in runtime._asr_provider_exact_pending
                or not runtime._asr_final_lock.locked()
            ):
                assert not boundary.done()
                await asyncio.sleep(0)
        pending = runtime._asr_provider_exact_pending[key]
        async with asyncio.timeout(2):
            while type(boundary._fut_waiter).__name__ != "_GatheringFuture":
                assert not boundary.done()
                await asyncio.sleep(0)
        if cancel_count:
            boundary.cancel()
            async with asyncio.timeout(2):
                while "_publish_provider_ledger_unavailable" not in awaiting_names(
                    boundary
                ):
                    assert not boundary.done()
                    await asyncio.sleep(0)
        if cancel_count == 2:
            boundary.cancel()
            await asyncio.gather(boundary, return_exceptions=True)
        core.continuity_score_host.ready.set()
        await asyncio.wait_for(asyncio.gather(boundary, return_exceptions=True), 2)
        evidence = {
            "cancel_count": cancel_count,
            "boundary_done": boundary.done(),
            "boundary_cancelled": boundary.cancelled(),
            "final_lock_held": runtime._asr_final_lock.locked(),
            "pending_retained": runtime._asr_provider_exact_pending.get(key) is pending,
            "pending_completed": pending.completion.is_set(),
            "committed": key in runtime._asr_provider_exact_intervals,
        }
        print("PRECOMMIT_CANCEL_EVIDENCE", evidence)
        assert boundary.cancelled() is bool(cancel_count)
        assert not runtime._asr_final_lock.locked(), evidence
        assert pending.completion.is_set(), evidence
        assert runtime._asr_provider_exact_pending.get(key) is not pending, evidence
    finally:
        core.continuity_score_host.ready.set()
        if boundary is not None and not boundary.done():
            boundary.cancel()
            await asyncio.gather(boundary, return_exceptions=True)
        # The failed case has no live owner left; release only the probe's leaked lock.
        if runtime._asr_final_lock.locked():
            runtime._asr_final_lock.release()
        await _close_stack(core)


@pytest.mark.parametrize("stop", [False, True])
async def test_taken_final_waiter_is_settled_on_drain_exit(monkeypatch, stop):
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
    tasks, final_waiters = [], []
    real_enqueue = runtime._enqueue_exact_interval_event
    real_lifecycle = runtime._callbacks.on_lifecycle

    def record_waiter(transaction, event, *, waiter=None):
        if isinstance(event, ProviderFinalReceived) and waiter is not None:
            final_waiters.append(waiter)
        return real_enqueue(transaction, event, waiter=waiter)

    async def lifecycle_callback(event):
        if event.state == "warm_idle":
            entered.set()
            await release.wait()
        await real_lifecycle(event)

    monkeypatch.setattr(runtime, "_enqueue_exact_interval_event", record_waiter)
    runtime._callbacks = replace(runtime._callbacks, on_lifecycle=lifecycle_callback)
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        for sequence in range(1, 17):
            await _submit_pcm(runtime, turn, sequence=sequence)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )

        def notification(phase):
            return ProviderEndpointNotification(
                phase=phase,
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=ProviderAudioRange(0, 25600),
            )

        boundary = asyncio.create_task(
            runtime._handle_provider_endpoint_notification(
                notification("boundary"), runtime._asr_session_epoch
            )
        )
        tasks.append(boundary)
        async with asyncio.timeout(2):
            while key not in runtime._asr_provider_exact_intervals:
                assert not boundary.done()
                await asyncio.sleep(0)
        transaction = runtime._asr_provider_exact_intervals[key]
        core.continuity_score_host.ready.set()
        await asyncio.wait_for(boundary, 2)
        await shadow.wait_idle()
        await runtime._handle_provider_endpoint_notification(
            notification("ordered"), runtime._asr_session_epoch
        )
        final = asyncio.create_task(
            runtime._handle_provider_final(
                key, "synthetic final", runtime._asr_session_epoch, "qwen"
            )
        )
        tasks.append(final)
        await asyncio.wait_for(entered.wait(), 2)
        assert len(final_waiters) == 1
        assert not final_waiters[0].done()
        assert runtime._asr_final_lock.locked()
        await runtime.wait_transcript_idle()
        await core._voice_input_registry.wait_idle()
        delivered = core.handle_input_transcript.await_count
        if stop:
            await asyncio.wait_for(runtime.stop_session(), 2)
        release.set()
        drain = transaction.drain_task
        await asyncio.wait_for(asyncio.gather(drain, return_exceptions=True), 2)
        evidence = {
            "drain_done": drain.done(),
            "drain_cancelled": drain.cancelled(),
            "final_lock_held": runtime._asr_final_lock.locked(),
            "final_caller_done": final.done(),
            "queue_size": len(transaction.event_queue),
            "core_deliveries": delivered,
        }
        print("SETTLEMENT_EVIDENCE", evidence)
        assert final_waiters[0].done(), evidence
        await asyncio.wait_for(final, 2)
    finally:
        release.set()
        core.continuity_score_host.ready.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


@pytest.mark.parametrize("action", ["normal", "stop", "close_after_stop"])
async def test_deferred_final_replay_cannot_outlive_session(action):
    (
        core,
        runtime,
        detector,
        shadow,
        lifecycle,
        session,
        turn,
    ) = await _active_real_stack(score=0.95)
    stop = action != "normal"
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []
    held_setup_lock = False
    original_lifecycle = runtime._callbacks.on_lifecycle

    async def on_lifecycle(event):
        if event.state == "warm_idle":
            entered.set()
            await release.wait()
        await original_lifecycle(event)

    runtime._callbacks = replace(runtime._callbacks, on_lifecycle=on_lifecycle)
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        for sequence in range(1, 17):
            await _submit_pcm(runtime, turn, sequence=sequence)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )

        def notification(phase):
            return ProviderEndpointNotification(
                phase=phase,
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=ProviderAudioRange(0, 25600),
            )

        # Exercise real lock contention. Do not replace Detector prepare/commit.
        await runtime._asr_final_lock.acquire()
        held_setup_lock = True
        boundary = asyncio.create_task(
            runtime._handle_provider_endpoint_notification(
                notification("boundary"), runtime._asr_session_epoch
            )
        )
        tasks.append(boundary)
        async with asyncio.timeout(2):
            while key not in runtime._asr_provider_exact_pending:
                assert not boundary.done()
                await asyncio.sleep(0)
        await runtime._handle_provider_final(
            key, "synthetic deferred final", runtime._asr_session_epoch, "qwen"
        )
        await runtime._handle_provider_endpoint_notification(
            notification("ordered"), runtime._asr_session_epoch
        )
        assert len(runtime._asr_provider_exact_pending[key].deferred) == 2
        runtime._asr_final_lock.release()
        held_setup_lock = False
        async with asyncio.timeout(2):
            while key not in runtime._asr_provider_exact_intervals:
                assert not boundary.done()
                await asyncio.sleep(0)
        transaction = runtime._asr_provider_exact_intervals[key]
        core.continuity_score_host.ready.set()
        await asyncio.wait_for(entered.wait(), 2)
        replay = next(
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "provider-exact-interval-replay"
        )
        tasks.append(replay)
        await runtime.wait_transcript_idle()
        await core._voice_input_registry.wait_idle()
        assert core.handle_input_transcript.await_count == 1

        async def close_transport():
            # The Provider final callback already returned after deferral.
            # The physical Session owns/cancels the boundary callback only.
            boundary.cancel()
            _, pending = await asyncio.wait((boundary,), timeout=0.2)
            for task in pending:
                task.cancel()

        session.close.side_effect = close_transport
        if stop:
            await asyncio.wait_for(runtime.stop_session(), 2)
        if action == "close_after_stop":
            await asyncio.wait_for(runtime.close(), 3)
        release.set()
        if stop:
            await asyncio.wait_for(
                asyncio.gather(
                    boundary, transaction.drain_task, return_exceptions=True
                ),
                2,
            )
            await asyncio.wait((replay,), timeout=0.05)
            evidence = {
                "boundary_cancelled": boundary.cancelled(),
                "drain_cancelled": transaction.drain_task.cancelled(),
                "replay_done": replay.done(),
                "final_lock_held": runtime._asr_final_lock.locked(),
                "replay_tracked": replay in runtime._asr_admission_effect_tasks,
                "terminal_close_done": bool(
                    runtime._asr_terminal_close_task
                    and runtime._asr_terminal_close_task.done()
                ),
                "core_deliveries": core.handle_input_transcript.await_count,
            }
            print("DEFERRED_REPLAY_EVIDENCE", evidence)
            assert replay.done(), evidence
            assert not runtime._asr_final_lock.locked()
        else:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
            assert not runtime._asr_final_lock.locked()
            assert core.handle_input_transcript.await_count == 1
    finally:
        release.set()
        if held_setup_lock:
            runtime._asr_final_lock.release()
        core.continuity_score_host.ready.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


@pytest.mark.parametrize(
    "action", ["stop", "close_after_stop", "timeout", "double_cancel"]
)
async def test_unavailable_partial_has_bounded_ownership(action):
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
    tasks, partial_tasks, returned_after_close = [], [], []
    reaped_before_transport_close = []

    async def deliver_partial(event):
        partial_tasks.append(asyncio.current_task())
        entered.set()
        await release.wait()
        returned_after_close.append(runtime._asr_session is None)

    runtime._callbacks = replace(runtime._callbacks, on_partial=deliver_partial)
    key = ProviderUtteranceKey(0, 0, 1)
    try:
        for sequence in range(1, 17):
            await _submit_pcm(runtime, turn, sequence=sequence)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        boundary = asyncio.create_task(
            runtime._handle_provider_endpoint_notification(
                ProviderEndpointNotification(
                    phase="boundary",
                    generation=0,
                    buffer_epoch=0,
                    utterance_id=1,
                    boundary_quality="exact",
                    audio_range=ProviderAudioRange(0, 25600),
                ),
                runtime._asr_session_epoch,
            )
        )
        tasks.append(boundary)
        async with asyncio.timeout(2):
            while key not in runtime._asr_provider_exact_intervals:
                assert not boundary.done()
                await asyncio.sleep(0)
        await runtime._send_independent_asr_preview(
            "synthetic preview", runtime._asr_session_epoch
        )
        conflict = asyncio.create_task(
            runtime._handle_provider_endpoint_notification(
                ProviderEndpointNotification(
                    phase="boundary",
                    generation=0,
                    buffer_epoch=0,
                    utterance_id=1,
                    boundary_quality="unknown",
                    audio_range=None,
                ),
                runtime._asr_session_epoch,
            )
        )
        tasks.append(conflict)
        await asyncio.wait_for(entered.wait(), 2)
        core.continuity_score_host.ready.set()
        partial_task = partial_tasks[0]
        assert not partial_task.done()

        async def close_transport():
            reaped_before_transport_close.append(partial_task.done())
            # Mirror Session._cancel_provider_boundary_tasks: cancel, wait
            # its real 200ms budget, then cancel the remaining callback again.
            conflict.cancel()
            _, pending = await asyncio.wait((conflict,), timeout=0.2)
            for task in pending:
                task.cancel()

        session.close.side_effect = close_transport
        if action in {"stop", "close_after_stop"}:
            await asyncio.wait_for(runtime.stop_session(), 3)
        elif action == "double_cancel":
            conflict.cancel()
            await asyncio.sleep(0)
            conflict.cancel()
        if action == "close_after_stop":
            await asyncio.wait_for(runtime.close(), 3)
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 2
        )
        if action == "timeout":
            assert results[1] is None
            assert runtime._asr_session is session
        else:
            assert isinstance(results[1], asyncio.CancelledError)
        if action in {"stop", "close_after_stop"}:
            assert runtime._asr_session is None
            assert reaped_before_transport_close == [True]
        orphaned = not partial_task.done()
        tracked = partial_task in runtime._asr_admission_effect_tasks
        release.set()
        await asyncio.wait_for(asyncio.gather(partial_task, return_exceptions=True), 2)
        assert not returned_after_close
        assert partial_task.cancelled()
        assert partial_task not in runtime._asr_exact_callback_tasks
        assert not orphaned, {
            "task": partial_task.get_name(),
            "tracked": tracked,
            "callback_returned_after_close": returned_after_close,
        }
    finally:
        release.set()
        core.continuity_score_host.ready.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *partial_tasks, return_exceptions=True)
        await _close_stack(core)
