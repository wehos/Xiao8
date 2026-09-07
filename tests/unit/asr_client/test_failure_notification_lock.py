"""Display serialization must not own required Core failure retirement."""

import asyncio
import json

import pytest

from main_logic.voice_turn.contracts import AsrFailureEvent
from tests.unit.asr_client.test_failure_lease_retirement import _connected_stack
from tests.unit.asr_client.test_pending_turn_handoff import _pending_exact_final
from tests.unit.asr_client.test_provider_speaker_continuity import _close_stack


@pytest.mark.parametrize("delay", [0.05, 2.3, None])
async def test_handoff_failure_retires_while_prior_notice_owns_lock(monkeypatch, delay):
    core, runtime, detector, shadow, lifecycle, session, turn = await _connected_stack(monkeypatch)
    prepared, fail_prepare = asyncio.Event(), asyncio.Event()
    entered, release_notice, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks = []
    original_status = core.send_status

    async def status(message, *args, **kwargs):
        if json.loads(message).get("code") == "ASR_SPEAKER_EVIDENCE_UNAVAILABLE":
            entered.set()
            try:
                if delay is None:
                    await release_notice.wait()
                else:
                    await asyncio.sleep(delay)
            finally:
                exited.set()
        return await original_status(message, *args, **kwargs)

    async def prepare(*, turn_id):
        prepared.set()
        await fail_prepare.wait()
        raise RuntimeError("synthetic external prepare failure")

    core.send_status = status
    core.session.prepare_external_voice_turn = prepare
    try:
        transaction, successor, final = await _pending_exact_final(runtime, core, shadow, turn)
        tasks.append(final)
        await asyncio.wait_for(prepared.wait(), 2)
        notice = asyncio.create_task(runtime._send_asr_status(
            "ASR_SPEAKER_EVIDENCE_UNAVAILABLE", "qwen",
            session_epoch=runtime._asr_session_epoch,
            expected_identity=runtime._capture_runtime_identity(),
        ))
        tasks.append(notice)
        await asyncio.wait_for(entered.wait(), 2)
        fail_prepare.set()
        await asyncio.wait_for(asyncio.gather(final, return_exceptions=True), 2)
        # BLOCKED display has a 1 s deadline. Required retirement must then
        # finish even while the earlier writer still owns the real lock.
        async with asyncio.timeout(1.8):
            while core._asr_route_mode != "blocked":
                await asyncio.sleep(.001)
        assert core._voice_lease_owner == "none"
        assert not core._voice_lease_connection_id
        assert not core._voice_input_accepts_pcm()
        if delay != 0.05:
            assert not exited.is_set()
            assert core._asr_notification_lock.locked()
        release_notice.set()
        await asyncio.wait_for(notice, 3)
        async with asyncio.timeout(2):
            while runtime._asr_owned_cleanup_tasks:
                await asyncio.sleep(.001)
        await runtime.wait_transcript_idle()
        await core._voice_input_registry.wait_idle()
        assert transaction.drain_task.done()
        assert runtime._asr_pending_turn_handoff is None
        session.close.assert_awaited_once()
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
        successor_id = f"asr-{successor.ingress.session_epoch}-{successor.turn_id}"
        assert sum(c.args == (successor_id,) for c in core.session.abandon_external_voice_turn.call_args_list) == 1
        await asyncio.wait_for(runtime.stop_session(), 2)
    finally:
        fail_prepare.set()
        release_notice.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


@pytest.mark.parametrize("owner", ["core", "game"])
async def test_old_failure_waiting_for_blocked_display_cannot_revoke_new_claim(monkeypatch, owner):
    core, runtime, detector, shadow, lifecycle, session, turn = await _connected_stack(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original_status = core.send_status

    async def status(message, *args, **kwargs):
        payload = json.loads(message)
        if payload.get("code") == "ASR_LIFECYCLE_STATE" and payload.get("details", {}).get("state") == "blocked":
            entered.set()
            await release.wait()
        return await original_status(message, *args, **kwargs)

    core.send_status = status
    failure = asyncio.create_task(runtime._handle_independent_asr_error(
        runtime._asr_session_epoch, "qwen", notification_timeout_seconds=1.0,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        core._begin_asr_route_operation()
        assert core._begin_voice_input_connection("replacement")
        assert await asyncio.wait_for(core._handle_voice_input_control(
            "lease_sync", 1, owner=owner, hard_muted=False, focus_suppressed=False,
        ), 2)
        identity = core._capture_core_asr_operation_identity()
        release.set()
        await asyncio.wait_for(failure, 2)
        assert core._capture_core_asr_operation_identity() == identity
        assert core._voice_lease_connection_id == "replacement"
        assert core._voice_lease_owner == owner
        assert core._voice_lease_synchronized
    finally:
        release.set()
        if not failure.done():
            failure.cancel()
        await asyncio.gather(failure, return_exceptions=True)
        await _close_stack(core)


async def test_stale_failure_is_rejected_without_waiting_for_display(monkeypatch):
    core, runtime, detector, shadow, lifecycle, session, turn = await _connected_stack(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    failure = None

    async def status(*args, **kwargs):
        entered.set()
        await release.wait()

    core.send_status = status
    notice = asyncio.create_task(runtime._send_asr_status(
        "ASR_SPEAKER_EVIDENCE_UNAVAILABLE", "qwen", session_epoch=runtime._asr_session_epoch,
        expected_identity=runtime._capture_runtime_identity(),
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        identity = core._capture_core_asr_operation_identity()
        failure = asyncio.create_task(core._handle_core_asr_failure(AsrFailureEvent(
            code="ASR_INDEPENDENT_FAILED", provider="qwen", session_epoch=runtime._asr_session_epoch - 1,
        )))
        await asyncio.sleep(0)
        assert core._capture_core_asr_operation_identity() == identity
        assert runtime._asr_session is session
        assert core._voice_input_accepts_pcm()
        await asyncio.wait_for(failure, .5)
        session.close.assert_not_awaited()
    finally:
        release.set()
        if failure is not None:
            if not failure.done():
                failure.cancel()
            await asyncio.gather(failure, return_exceptions=True)
        await asyncio.gather(notice, return_exceptions=True)
        await _close_stack(core)
