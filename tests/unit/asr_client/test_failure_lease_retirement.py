"""Failure notification deadlines cannot own microphone lease retirement."""

import asyncio
from unittest.mock import MagicMock

import pytest

import tests.unit.asr_client.test_provider_speaker_continuity as stack_module
from main_logic.voice_turn.contracts import AsrFailureEvent
from tests.unit.asr_client.test_pending_turn_handoff import _pending_exact_final
from tests.unit.asr_client.test_provider_speaker_continuity import _close_stack


async def _connected_stack(monkeypatch):
    original_host = stack_module._Runtime

    def host():
        core = original_host()
        core._voice_lease_connection_id = "recorder"
        core._voice_lease_control_seen = True
        return core

    monkeypatch.setattr(stack_module, "_Runtime", host)
    stack = await stack_module._active_real_stack(score=0.95)
    core = stack[0]
    core._set_voice_input_websocket("recorder", core.websocket)
    core.session.abandon_external_voice_turn = MagicMock()
    return stack


@pytest.mark.parametrize("action", ["cancel", "timeout"])
@pytest.mark.parametrize("clear_delay", [1.3, None])
async def test_handoff_retires_lease_before_slow_registry_cleanup(
    monkeypatch, action, clear_delay,
):
    core, runtime, detector, shadow, lifecycle, session, turn = await _connected_stack(monkeypatch)
    prepared, release_prepare = asyncio.Event(), asyncio.Event()
    clear_entered, clear_exited = asyncio.Event(), asyncio.Event()
    release_clear = asyncio.Event()
    successor_id = None
    tasks = []
    original_send = core.websocket.send_json

    async def send(payload):
        if payload.get("asr_turn_id") == successor_id and payload.get("text") == "":
            clear_entered.set()
            try:
                if clear_delay is None:
                    await release_clear.wait()
                else:
                    await asyncio.sleep(clear_delay)
            finally:
                clear_exited.set()
        await original_send(payload)

    async def prepare(*, turn_id):
        prepared.set()
        await release_prepare.wait()
        return False

    core.websocket.send_json = send
    core.session.prepare_external_voice_turn = prepare
    try:
        transaction, successor, final = await _pending_exact_final(runtime, core, shadow, turn)
        successor_id = f"asr-{successor.ingress.session_epoch}-{successor.turn_id}"
        tasks.append(final)
        await asyncio.wait_for(prepared.wait(), 2)
        if action == "cancel":
            final.cancel()
        result = (await asyncio.wait_for(asyncio.gather(final, return_exceptions=True), 7))[0]
        if action == "cancel":
            assert isinstance(result, asyncio.CancelledError)
        await asyncio.wait_for(clear_entered.wait(), 7)
        async with asyncio.timeout(2):
            while core._asr_route_mode != "blocked":
                await asyncio.sleep(0)
        # Assert while the external write is still pending: bounding preview
        # alone must not hide an incorrectly ordered mandatory revoke.
        assert not clear_exited.is_set()
        assert core._voice_lease_owner == "none"
        assert core._voice_lease_connection_id == ""
        assert not core._voice_input_accepts_pcm()
        await asyncio.wait_for(clear_exited.wait(), 2)
        await asyncio.wait_for(core._voice_input_registry.wait_idle(), 2)
        async with asyncio.timeout(2):
            while runtime._asr_owned_cleanup_tasks:
                await asyncio.sleep(0)
        assert not core._voice_input_registry._background_tasks
        assert not core._core_asr_cleanup_tasks
        assert runtime._asr_pending_turn_handoff is None
        assert transaction.drain_task.done()
        session.close.assert_awaited_once()
        # key (0,0,1) was forwarded once; (0,0,2) had no final and was abandoned.
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
        assert sum(c.args == (successor_id,) for c in core.session.abandon_external_voice_turn.call_args_list) == 1
        await asyncio.wait_for(runtime.stop_session(), 2)
    finally:
        release_prepare.set()
        release_clear.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


@pytest.mark.parametrize("action", ["cancel", "replacement", "game"])
async def test_failure_cleanup_cannot_retire_a_later_lease(monkeypatch, action):
    core, runtime, detector, shadow, lifecycle, session, turn = await _connected_stack(monkeypatch)
    entered, exited, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_session = core.session
    turn_id = f"asr-{turn.ingress.session_epoch}-{turn.turn_id}"

    async def send(payload):
        if payload.get("asr_turn_id") == turn_id and payload.get("text") == "":
            entered.set()
            try:
                await release.wait()
            finally:
                exited.set()

    core.websocket.send_json = send
    failure = asyncio.create_task(core._handle_core_asr_failure(AsrFailureEvent(
        code="ASR_INDEPENDENT_FAILED", provider="qwen", session_epoch=runtime._asr_session_epoch,
    )))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert core._voice_lease_owner == "none"
        if action == "cancel":
            failure.cancel()
            with pytest.raises(asyncio.CancelledError):
                await failure
            await asyncio.wait_for(exited.wait(), 2)
        else:
            assert core._begin_voice_input_connection("replacement")
            control = asyncio.create_task(core._handle_voice_input_control(
                "lease_sync", 1, owner="game" if action == "game" else "core",
                hard_muted=False, focus_suppressed=False,
            ))
            try:
                await asyncio.sleep(0)
                release.set()
                assert await asyncio.wait_for(control, 2)
                await asyncio.wait_for(failure, 2)
                assert core._voice_lease_connection_id == "replacement"
                assert core._voice_lease_owner == ("game" if action == "game" else "core")
                assert core._voice_lease_synchronized
            finally:
                if not control.done():
                    control.cancel()
                await asyncio.gather(control, return_exceptions=True)
        await asyncio.wait_for(core._voice_input_registry.wait_idle(), 2)
        original_session.abandon_external_voice_turn.assert_called_once_with(turn_id)
        assert not core._voice_input_registry._background_tasks
    finally:
        release.set()
        if not failure.done():
            failure.cancel()
        await asyncio.gather(failure, return_exceptions=True)
        await _close_stack(core)
