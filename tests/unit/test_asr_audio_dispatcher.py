from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from main_logic.asr_client.lifecycle import VoiceIngressToken, VoiceTurnToken
from main_logic.asr_client.audio import AsrAudioDispatcher


def _turn(turn_id: int = 1) -> VoiceTurnToken:
    return VoiceTurnToken(VoiceIngressToken(1, "socket", 1, 1, 1), turn_id)


async def test_activate_audio_and_seal_are_strictly_ordered() -> None:
    calls: list[tuple[str, bytes | None]] = []
    session = type("Session", (), {})()

    async def stream_audio(pcm16: bytes, *, sample_rate_hz: int) -> None:
        assert sample_rate_hz == 16_000
        calls.append(("audio", pcm16))

    async def seal() -> None:
        calls.append(("seal", None))

    session.stream_audio = stream_audio
    session.signal_user_activity_end = seal
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=AsyncMock(),
        on_failure=AsyncMock(),
    )
    turn = _turn()

    assert dispatcher.activate(turn, session, b"pre-roll")
    assert dispatcher.enqueue_audio(
        turn,
        session,
        b"realtime",
        sample_rate_hz=16_000,
        sequence_no=1,
    )
    assert dispatcher.seal(turn, session, after_sequence=1)
    await dispatcher.wait_idle()

    assert calls == [
        ("audio", b"pre-roll"),
        ("audio", b"realtime"),
        ("seal", None),
    ]
    await dispatcher.close()


async def test_abort_discards_queued_writes_before_they_start() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    writes: list[bytes] = []
    session = type("Session", (), {})()

    async def stream_audio(pcm16: bytes, *, sample_rate_hz: int) -> None:
        del sample_rate_hz
        writes.append(pcm16)
        first_started.set()
        await release_first.wait()

    session.stream_audio = stream_audio
    session.signal_user_activity_end = AsyncMock()
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=AsyncMock(),
        on_failure=AsyncMock(),
    )
    turn = _turn()
    dispatcher.activate(turn, session, b"first!")
    dispatcher.enqueue_audio(
        turn,
        session,
        b"must-not-start",
        sample_rate_hz=16_000,
        sequence_no=1,
    )
    await asyncio.wait_for(first_started.wait(), 1)

    dispatcher.abort(turn)
    release_first.set()
    await dispatcher.wait_idle()

    assert writes == [b"first!"]
    session.signal_user_activity_end.assert_not_awaited()
    await dispatcher.close()


async def test_abort_invalidates_inflight_success_side_effects() -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    session = type("Session", (), {})()

    async def stream_audio(_pcm16: bytes, *, sample_rate_hz: int) -> None:
        assert sample_rate_hz == 16_000
        started.set()
        await release.wait()

    session.stream_audio = stream_audio
    session.signal_user_activity_end = AsyncMock()
    on_wire_audio = AsyncMock()
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=on_wire_audio,
        on_failure=AsyncMock(),
    )
    turn = _turn()
    dispatcher.activate(turn, session, b"first!")
    dispatcher.enqueue_audio(
        turn,
        session,
        b"second",
        sample_rate_hz=16_000,
        sequence_no=1,
    )
    await asyncio.wait_for(started.wait(), 1)

    dispatcher.abort(turn)
    release.set()
    await dispatcher.wait_idle()

    assert dispatcher.provider_wire_sequence == 0
    on_wire_audio.assert_not_awaited()
    assert dispatcher.asr_abort_discarded_command_count >= 1
    assert dispatcher.asr_audio_command_queue_ms >= 0
    await dispatcher.close()


async def test_current_success_records_wire_side_effects_once() -> None:
    session = type("Session", (), {})()
    session.stream_audio = AsyncMock()
    session.signal_user_activity_end = AsyncMock()
    on_wire_audio = AsyncMock()
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=on_wire_audio,
        on_failure=AsyncMock(),
    )
    turn = _turn()

    assert dispatcher.activate(turn, session, b"\x01\x00")
    await dispatcher.wait_idle()

    assert dispatcher.provider_wire_sequence == 1
    on_wire_audio.assert_awaited_once_with(turn, session, 2)
    await dispatcher.close()


async def test_abort_suppresses_failure_from_inflight_audio_command() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    session = type("Session", (), {})()

    async def stream_audio(_pcm16: bytes, *, sample_rate_hz: int) -> None:
        assert sample_rate_hz == 16_000
        started.set()
        await release.wait()
        raise RuntimeError("session closed during intentional abort")

    session.stream_audio = stream_audio
    session.signal_user_activity_end = AsyncMock()
    on_failure = AsyncMock()
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=AsyncMock(),
        on_failure=on_failure,
    )
    turn = _turn()
    assert dispatcher.activate(turn, session, b"\x01\x00")
    await asyncio.wait_for(started.wait(), 1)

    dispatcher.abort(turn)
    release.set()
    await dispatcher.wait_idle()

    on_failure.assert_not_awaited()
    await dispatcher.close()


async def test_abort_and_join_closes_session_while_joining_active_writer() -> None:
    writer_started = asyncio.Event()
    writer_released = asyncio.Event()
    close_started = asyncio.Event()
    writes: list[bytes] = []
    session = type("Session", (), {})()

    async def stream_audio(pcm16: bytes, *, sample_rate_hz: int) -> None:
        assert sample_rate_hz == 16_000
        writes.append(pcm16)
        writer_started.set()
        await writer_released.wait()

    async def close_session() -> None:
        close_started.set()
        writer_released.set()

    session.stream_audio = stream_audio
    session.signal_user_activity_end = AsyncMock()
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=AsyncMock(),
        on_failure=AsyncMock(),
    )
    turn = _turn()
    assert dispatcher.activate(turn, session, b"\x01\x00")
    retired_generation = dispatcher.transport_generation
    assert dispatcher.enqueue_audio(
        turn,
        session,
        b"\x02\x00",
        sample_rate_hz=16_000,
        sequence_no=1,
    )
    await asyncio.wait_for(writer_started.wait(), 1)

    receipt = await asyncio.wait_for(
        dispatcher.abort_and_join(
            turn,
            close_session=close_session,
            transport_generation=retired_generation,
        ),
        1,
    )

    assert close_started.is_set()
    assert writes == [b"\x01\x00"]
    assert receipt.transport_generation == retired_generation
    assert receipt.discarded_commands == 1
    assert receipt.active_writer_joined is True
    assert receipt.session_closed is True
    await dispatcher.close()


async def test_abort_and_join_reports_close_failure_without_claiming_safety() -> None:
    session = type("Session", (), {})()
    session.stream_audio = AsyncMock()
    session.signal_user_activity_end = AsyncMock()
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=AsyncMock(),
        on_failure=AsyncMock(),
    )
    turn = _turn()
    assert dispatcher.activate(turn, session, b"")
    await dispatcher.wait_idle()

    async def close_session() -> None:
        raise RuntimeError("provider close failed")

    receipt = await dispatcher.abort_and_join(
        turn,
        close_session=close_session,
        transport_generation=dispatcher.transport_generation,
    )

    assert receipt.active_writer_joined is True
    assert receipt.session_closed is False
    await dispatcher.close()


async def test_worker_rechecks_generation_after_validator_returns() -> None:
    session = type("Session", (), {})()
    session.stream_audio = AsyncMock()
    session.signal_user_activity_end = AsyncMock()
    turn = _turn()
    dispatcher: AsrAudioDispatcher

    def validator(_token: VoiceTurnToken, ref: object) -> bool:
        assert ref is session
        dispatcher.abort(turn)
        return True

    dispatcher = AsrAudioDispatcher(
        validator=validator,
        on_wire_audio=AsyncMock(),
        on_failure=AsyncMock(),
    )
    assert dispatcher.activate(turn, session, b"\x01\x00")
    await dispatcher.wait_idle()

    session.stream_audio.assert_not_awaited()
    await dispatcher.close()


async def test_current_audio_command_failure_still_fails_closed() -> None:
    session = type("Session", (), {})()

    async def stream_audio(_pcm16: bytes, *, sample_rate_hz: int) -> None:
        assert sample_rate_hz == 16_000
        raise RuntimeError("current provider write failed")

    session.stream_audio = stream_audio
    session.signal_user_activity_end = AsyncMock()
    on_failure = AsyncMock()
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=AsyncMock(),
        on_failure=on_failure,
    )
    turn = _turn()
    assert dispatcher.activate(turn, session, b"\x01\x00")
    await dispatcher.wait_idle()

    on_failure.assert_awaited_once()
    await dispatcher.close()


async def test_backpressure_failure_task_is_retained_until_completion() -> None:
    failure_started = asyncio.Event()
    release_failure = asyncio.Event()
    session = type("Session", (), {})()

    async def on_failure(
        _turn_token: VoiceTurnToken, _error: BaseException
    ) -> None:
        failure_started.set()
        await release_failure.wait()

    session.stream_audio = AsyncMock()
    session.signal_user_activity_end = AsyncMock()
    dispatcher = AsrAudioDispatcher(
        validator=lambda _token, ref: ref is session,
        on_wire_audio=AsyncMock(),
        on_failure=on_failure,
        max_commands=1,
    )
    turn = _turn()

    assert dispatcher.activate(turn, session, b"first!")
    assert not dispatcher.enqueue_audio(
        turn,
        session,
        b"overflow",
        sample_rate_hz=16_000,
        sequence_no=1,
    )
    await asyncio.wait_for(failure_started.wait(), 1)

    assert len(dispatcher._failure_tasks) == 1
    failure_task = next(iter(dispatcher._failure_tasks))
    assert failure_task.get_name() == "asr-audio-command-backpressure"

    release_failure.set()
    await failure_task
    await asyncio.sleep(0)

    assert not dispatcher._failure_tasks
    await dispatcher.close()
