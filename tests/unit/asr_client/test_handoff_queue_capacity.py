"""Temporary handoff storage stays bounded and belongs to one ingress."""

from dataclasses import replace

import asyncio
import pytest

from main_logic.core.asr_runtime import _AudioDurationQueue, _QueuedMicFrame
from main_logic.voice_turn.contracts import VoiceIngressToken


def _frame(*, samples=1600):
    token = VoiceIngressToken(
        session_epoch=1, audio_generation=1, connection_id="queue-owner",
        lease_generation=1, route_generation=1,
    )
    return _QueuedMicFrame.from_message(
        {"data": [1] * samples, "sample_rate_hz": 16000}, token=token,
    )


@pytest.mark.parametrize("reserve,frames", [(False, 20), (True, 80)])
def test_queue_handoff_duration_limit(reserve, frames):
    queue = _AudioDurationQueue(capacity_us=2_000_000, max_frames=256)
    frame = _frame()
    for _ in range(frames):
        queue.put_nowait(frame, handoff_reserve=reserve)
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(frame, handoff_reserve=reserve)
    assert queue.duration_us == frames * 100_000
    assert queue.qsize() == frames


def test_queue_handoff_frame_limit_for_tiny_frames():
    queue = _AudioDurationQueue(capacity_us=2_000_000, max_frames=256)
    frame = _frame(samples=1)
    for _ in range(1024):
        queue.put_nowait(frame, handoff_reserve=True)
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(frame, handoff_reserve=True)
    assert queue.qsize() == 1024
    assert queue.duration_us < 2_000_000
    for _ in range(768):
        queue.get_nowait()
        queue.task_done()
    assert queue.maxsize == 256
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(frame)


@pytest.mark.parametrize("change", ["token", "audio_epoch"])
def test_replacement_cannot_borrow_old_handoff_reserve(change):
    queue = _AudioDurationQueue(capacity_us=2_000_000, max_frames=256)
    frame = _frame()
    for _ in range(30):
        queue.put_nowait(frame, handoff_reserve=True)
    replacement = (
        replace(frame, token=replace(frame.token, audio_generation=2))
        if change == "token" else replace(frame, audio_stream_epoch=1)
    )
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(replacement)
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(replacement, handoff_reserve=True)
    assert queue.qsize() == 30


async def test_reserve_survives_drain_then_restores_normal_capacity():
    queue = _AudioDurationQueue(capacity_us=2_000_000, max_frames=256)
    frame = _frame()
    for _ in range(30):
        queue.put_nowait(frame, handoff_reserve=True)
    assert await queue.get() is frame
    queue.task_done()
    queue.put_nowait(frame)  # Handoff ended, but its backlog is still draining.
    assert queue.duration_us == 3_000_000
    for _ in range(10):
        queue.get_nowait()
        queue.task_done()
    assert queue.capacity_us == 2_000_000
    assert queue.maxsize == 256
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(frame)
    for _ in range(20):
        queue.get_nowait()
        queue.task_done()
    replacement = replace(frame, token=replace(frame.token, session_epoch=2))
    queue.put_nowait(replacement, handoff_reserve=True)
    assert queue.capacity_us == 8_000_000
    queue.get_nowait()
    queue.task_done()
    assert queue.capacity_us == 2_000_000
    assert queue._handoff_owner is None
