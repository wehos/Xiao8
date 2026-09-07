"""Voice revocation retains physical cleanup across admission cancellation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main_logic.asr_client.runtime as runtime_module
from main_logic.asr_client._provider_events import ProviderUtteranceStartedNotification
from main_logic.asr_client.endpointing.detector_runtime import DetectorRuntime
from tests.unit import test_asr_detector_runtime as detector_fixture
from tests.unit.asr_client.test_failure_lease_retirement import _connected_stack
from tests.unit.asr_client.test_pending_turn_handoff import _pending_exact_final
from tests.unit.asr_client.test_provider_speaker_continuity import _close_stack, _submit_pcm
from tests.unit.test_core_independent_asr import _selection


async def _wait_revoke_started(core, generation):
    async with asyncio.timeout(2):
        while (
            core._voice_lease_owner != "none"
            or core._asr_runtime._asr_audio_generation == generation
        ):
            await asyncio.sleep(0)


async def _wait_cleanup_idle(runtime):
    async with asyncio.timeout(3):
        while runtime._asr_owned_cleanup_tasks or runtime._asr_close_tasks:
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("action", ["complete", "cancel", "double_cancel", "timeout", "prepare_timeout"])
async def test_revoke_during_admission_settlement_closes_old_provider(monkeypatch, action):
    core, runtime, detector, shadow, lifecycle, session, turn = await _connected_stack(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []
    if action == "prepare_timeout":
        monkeypatch.setattr(runtime_module, "_EXACT_PENDING_PREPARE_TIMEOUT_SECONDS", 0.1)

    async def prepare(*, turn_id):
        entered.set()
        await release.wait()
        return False

    async def revoke_with_deadline():
        async with asyncio.timeout(0.05):
            await core._revoke_voice_input_connection("recorder")

    core.session.prepare_external_voice_turn = prepare
    try:
        transaction, successor, final = await _pending_exact_final(runtime, core, shadow, turn)
        tasks.append(final)
        await asyncio.wait_for(entered.wait(), 2)
        generation = runtime._asr_audio_generation
        revoke = asyncio.create_task(
            revoke_with_deadline() if action == "timeout"
            else core._revoke_voice_input_connection("recorder")
        )
        tasks.append(revoke)
        await _wait_revoke_started(core, generation)
        if action in {"cancel", "double_cancel"}:
            revoke.cancel()
            await asyncio.sleep(0)
            if action == "double_cancel":
                revoke.cancel()
                await asyncio.sleep(0)
        elif action == "timeout":
            await asyncio.sleep(0.08)
        if action != "prepare_timeout":
            release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        await asyncio.wait_for(core._voice_input_registry.wait_idle(), 2)
        await _wait_cleanup_idle(runtime)
        if action in {"cancel", "double_cancel"}:
            assert isinstance(results[-1], asyncio.CancelledError)
        elif action == "timeout":
            assert isinstance(results[-1], TimeoutError)
        else:
            assert results[-1] is True
        assert runtime._asr_session is not session
        session.close.assert_awaited_once()
        assert core._voice_lease_owner == "none"
        assert not core._voice_input_accepts_pcm()
        assert not await core._revoke_voice_input_connection("recorder")
        # The actual key (0, 0, 1) has one final; successor (0, 0, 2)
        # has no final and must settle as abandoned without another reply.
        assert transaction.provider_key.utterance_id == 1
        assert core.handle_input_transcript.await_count == 1
        assert core.handle_input_transcript.await_args.args[0] == "synthetic first"
        assert core.session.create_response.await_count == 1
        successor_id = f"asr-{successor.ingress.session_epoch}-{successor.turn_id}"
        assert sum(
            call.args == (successor_id,)
            for call in core.session.abandon_external_voice_turn.call_args_list
        ) == 1
        assert transaction.drain_task.done()
        assert not core._voice_input_registry._background_tasks
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


@pytest.mark.parametrize("cancel", [False, True])
async def test_old_abort_settlement_preserves_real_replacement(monkeypatch, cancel):
    core, runtime, detector, shadow, lifecycle, old_session, turn = await _connected_stack(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    close_entered, close_release = asyncio.Event(), asyncio.Event()
    tasks, created_detectors = [], []
    replacement = SimpleNamespace(
        is_ready=True, connect=AsyncMock(), close=AsyncMock(), stream_audio=AsyncMock(),
    )

    def create_detector(**kwargs):
        result = DetectorRuntime(vad=detector_fixture._Vad(), gate=detector_fixture._Gate(), **kwargs)
        created_detectors.append(result)
        return result

    async def prepare(*, turn_id):
        entered.set()
        await release.wait()
        return False

    async def close_old():
        close_entered.set()
        await close_release.wait()

    monkeypatch.setattr(runtime_module, "_resolve_asr_selection", lambda _: _selection("qwen", "provider"))
    monkeypatch.setattr(runtime_module, "_create_asr_session_from_selection", lambda *args, **kwargs: replacement)
    monkeypatch.setattr(runtime_module, "DetectorRuntime", create_detector)
    core.session.prepare_external_voice_turn = prepare
    old_session.close.side_effect = close_old
    try:
        transaction, successor, final = await _pending_exact_final(runtime, core, shadow, turn)
        tasks.append(final)
        await asyncio.wait_for(entered.wait(), 2)
        generation = runtime._asr_audio_generation
        revoke = asyncio.create_task(core._revoke_voice_input_connection("recorder"))
        tasks.append(revoke)
        await _wait_revoke_started(core, generation)
        await asyncio.wait_for(close_entered.wait(), 0.5)
        # Physical close has a registered owner even while prepare is pending.
        assert not release.is_set()
        assert not revoke.done()
        if cancel:
            revoke.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(revoke, 0.5)
        assert core._begin_voice_input_connection("replacement")
        start = asyncio.create_task(runtime.start(route_key="qwen", resource_optimization_enabled=False))
        tasks.append(start)
        async with asyncio.timeout(2):
            while runtime._asr_session is not replacement:
                await asyncio.sleep(0)
        new_detector, new_lifecycle = runtime._asr_detector, runtime._asr_lifecycle
        new_dispatchers = (runtime._asr_audio_dispatcher, runtime._asr_detector_dispatcher)
        release.set()
        close_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        await _wait_cleanup_idle(runtime)
        assert runtime._asr_session is replacement
        assert runtime._asr_detector is new_detector
        assert runtime._asr_lifecycle is new_lifecycle
        assert (runtime._asr_audio_dispatcher, runtime._asr_detector_dispatcher) == new_dispatchers
        assert core._voice_lease_connection_id == "replacement"
        assert not await core._revoke_voice_input_connection("recorder")
        old_session.close.assert_awaited_once()
        replacement.close.assert_not_awaited()
        assert transaction.drain_task.done()
        assert core.handle_input_transcript.await_count == 1
        assert core.session.create_response.await_count == 1
    finally:
        release.set()
        close_release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)
        for item in created_detectors:
            await item.close()


@pytest.mark.parametrize("phase", ["lease", "provider"])
@pytest.mark.parametrize("outcome", ["cancel", "timeout", "error"])
async def test_abort_physical_cleanup_is_owned_and_bounded(monkeypatch, phase, outcome):
    core, runtime, detector, shadow, lifecycle, session, turn = await _connected_stack(monkeypatch)
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks = []
    lease = SimpleNamespace(release=AsyncMock())
    runtime._asr_smart_turn_lease = lease
    if outcome == "timeout":
        monkeypatch.setattr(runtime_module, "_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS", 0.05)

    async def physical_operation():
        entered.set()
        try:
            if outcome == "error":
                raise RuntimeError("synthetic physical cleanup failure")
            await release.wait()
        finally:
            exited.set()

    if phase == "lease":
        lease.release.side_effect = physical_operation
    else:
        session.close.side_effect = physical_operation
    try:
        revoke = asyncio.create_task(core._revoke_voice_input_connection("recorder"))
        tasks.append(revoke)
        await asyncio.wait_for(entered.wait(), 1)
        if outcome == "cancel":
            # Cancel at the physical-close join, after admission has settled.
            async with asyncio.timeout(0.5):
                while any(
                    task.get_name() == "independent-asr-abort-admission"
                    for task in runtime._asr_owned_cleanup_tasks
                ):
                    await asyncio.sleep(0)
            await asyncio.sleep(0)
            revoke.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(revoke, 0.5)
            assert not exited.is_set()
            assert runtime._asr_owned_cleanup_tasks
            release.set()
        else:
            assert await asyncio.wait_for(revoke, 0.5) is True
        await _wait_cleanup_idle(runtime)
        assert exited.is_set()
        assert runtime._asr_session is None
        assert runtime._asr_smart_turn_lease is None
        assert not core._voice_input_accepts_pcm()
        lease.release.assert_awaited_once()
        session.close.assert_awaited_once()
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)


async def test_voice_reclaim_after_old_preview_preserves_new_audio_ingress(monkeypatch):
    core, runtime, detector, shadow, lifecycle, old_session, turn = await _connected_stack(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    tasks, created_detectors = [], []
    replacement = SimpleNamespace(
        is_ready=True, connect=AsyncMock(), close=AsyncMock(), stream_audio=AsyncMock(),
    )
    old_turn_id = f"asr-{turn.ingress.session_epoch}-{turn.turn_id}"
    original_send = core.websocket.send_json

    async def send(payload):
        if payload.get("text") == "" and payload.get("asr_turn_id") == old_turn_id:
            entered.set()
            await release.wait()
        await original_send(payload)

    def create_detector(**kwargs):
        result = DetectorRuntime(vad=detector_fixture._Vad(), gate=detector_fixture._Gate(), **kwargs)
        created_detectors.append(result)
        return result

    monkeypatch.setattr(runtime_module, "_resolve_asr_selection", lambda _: _selection("qwen", "provider"))
    monkeypatch.setattr(runtime_module, "_create_asr_session_from_selection", lambda *args, **kwargs: replacement)
    monkeypatch.setattr(runtime_module, "DetectorRuntime", create_detector)
    core.websocket.send_json = send
    try:
        await _submit_pcm(runtime, turn, sequence=1)
        assert await runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1, audio_start_sample_16k=0),
            runtime._asr_session_epoch,
        )
        revoke = asyncio.create_task(core._revoke_voice_input_connection("recorder"))
        tasks.append(revoke)
        await asyncio.wait_for(entered.wait(), 1)
        assert core._begin_voice_input_connection("replacement")
        control = asyncio.create_task(core._handle_voice_input_control(
            "lease_sync", 1, owner="core", hard_muted=False, focus_suppressed=False,
        ))
        tasks.append(control)
        async with asyncio.timeout(0.5):
            while not core._voice_input_accepts_pcm():
                await asyncio.sleep(0)
        await asyncio.wait_for(runtime.start(route_key="qwen", resource_optimization_enabled=False), 0.5)
        new_ingress = core._capture_ingress_token()
        await _submit_pcm(runtime, SimpleNamespace(ingress=new_ingress), sequence=1)
        assert runtime._asr_current_ingress_token == new_ingress
        assert not revoke.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        await _wait_cleanup_idle(runtime)
        assert runtime._asr_session is replacement
        assert runtime._asr_current_ingress_token == new_ingress
        replacement.close.assert_not_awaited()
        old_session.close.assert_awaited_once()
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close_stack(core)
        for item in created_detectors:
            await item.close()
