# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A turn's end-of-turn hooks belong to that turn — including when the turn
that replaced it was started by the HOST (issue #2612).

Contracts, each written so it can be falsified:

1.  **The hooks stand down once the host is on a new turn.** The host's speech
    id is its turn token; a turn that began under a different one has no
    business closing this one. Falsified by either hook firing after the host
    rotated.

2.  **Re-read between the hooks, not once at entry.** ``on_response_done`` is
    exactly where the host blocks (it awaits the frontend), so a turn that
    starts during it must still stop ``on_sid_rotate`` — which is the step that
    causes the field failure: on a provider without server VAD it discards the
    speech id the new turn is speaking under, and TTS upstream then drops every
    later turn's text for the life of the connection. Falsified by the rotation
    running after a rotation the host already did.

    This is the one condition allowed to split the pair, and only because it
    cannot produce the state that rule protects against ("old sid closed, no
    new one issued"): it is true precisely because the host issued a new one.

3.  **An ordinary turn is untouched.** Same hooks, same order, when the host
    stayed on the turn — this guard adds a stand-down, not a new default.

4.  **No accessor, no guard.** A client constructed without
    ``get_host_turn_id``, or whose host raises while answering, behaves exactly
    as it did before this existed. The fail-safe direction is to notify.

Contract 2 is why the check is separate from the arbiter's ``still_ours``
epoch comparison rather than folded into it: the epoch only counts turn starts
the transport observes, and ``handle_new_message`` off a text input or an
independent ASR utterance never reaches it. See
``test_realtime_arbiter_fail_open.py`` for the epoch side of the same
question.
"""

import asyncio
import contextlib
import json
import logging
from types import SimpleNamespace

import pytest

from main_logic.omni_realtime_client import _gemini_support, _transport


class _RecordingSocket:
    """Socket double that also plays the server side.

    ``feed()`` pushes an event that ``handle_messages()`` reads out of its
    ``async for``; ``finish()`` ends the loop. Same shape as the one in
    ``test_realtime_arbiter_native_path.py``, and it exists here for the same
    reason: driving a whole turn through the REAL receive loop is what covers
    the sample point, which no amount of poking the guard directly can.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._inbound: asyncio.Queue = asyncio.Queue()

    async def send(self, payload) -> None:
        self.sent.append(json.loads(payload) if isinstance(payload, str) else payload)

    async def close(self) -> None:
        pass

    def feed(self, event: dict) -> None:
        self._inbound.put_nowait(json.dumps(event))

    def finish(self) -> None:
        self._inbound.put_nowait(None)

    async def __aiter__(self):
        while True:
            message = await self._inbound.get()
            if message is None:
                return
            yield message


async def _settle(times: int = 50) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


@contextlib.contextmanager
def _records_from(logger: logging.Logger):
    """Collect a logger's own records, independent of propagation."""

    lines: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    sink = _Sink()
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(sink)
    try:
        yield lines
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous_level)


class _Host:
    """Stands in for the session manager's ``current_speech_id`` and hooks."""

    def __init__(self) -> None:
        self.speech_id: str | None = "sid-turn-1"
        self.calls: list[str] = []
        self.block_in_response_done: asyncio.Event | None = None

    def read_speech_id(self) -> str | None:
        return self.speech_id

    async def on_response_done(self) -> None:
        self.calls.append("response_done")
        if self.block_in_response_done is not None:
            await self.block_in_response_done.wait()

    async def on_sid_rotate(self) -> None:
        self.calls.append("sid_rotate")
        self.speech_id = "sid-rotated-by-hook"

    def starts_a_new_turn(self) -> None:
        """What ``handle_new_message`` does that this side never observes."""
        self.speech_id = "sid-turn-2"


def _free_client(host: _Host | None, **hooks):
    """A client on a provider WITHOUT server VAD, where sid rotation matters.

    The lanlan.app host is load-bearing: ``_is_free_proxy`` keys on it, and
    that is what makes ``_has_server_vad`` False. With any other host the same
    client rotates from ``speech_stopped`` instead and never reaches the hook
    these tests are about.
    """

    from main_logic.omni_realtime_client import OmniRealtimeClient

    client = OmniRealtimeClient(
        "wss://www.lanlan.app/realtime",
        "test-key",
        model="free-model",
        api_type="free",
        on_response_done=None if host is None else host.on_response_done,
        on_sid_rotate=None if host is None else host.on_sid_rotate,
        **hooks,
    )
    assert client._has_server_vad is False, (
        "this fixture exists to cover the providers whose only sid rotation "
        "point is the turn-finished hook"
    )
    return client


def _begin_turn(client) -> None:
    """Put the client where ``response.created`` leaves it.

    Only the identity bookkeeping matters here, but it is written as the event
    handler writes it — sampling the host id is part of starting a turn, not a
    step a caller remembers to add.
    """

    client._is_responding = True
    client._turn_epoch += 1
    client._current_turn_epoch = client._turn_epoch
    client._current_turn_host_id = client._read_host_turn_id()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_input_transcript_keeps_route_captured_at_voice_ingress():
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._remember_input_route_identity()
    current_route[0] = ("example-game", "session-b", "route-b")

    await client._deliver_input_transcript("hello")

    assert delivered == [
        ("hello", ("example-game", "session-a", "route-a")),
    ]
    assert client._input_route_identity_captured is False
    assert client._input_route_identity is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_vad_binds_route_captured_at_speech_onset_before_audio_processing_await():
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []
    processing_started = asyncio.Event()
    processing_release = asyncio.Event()

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def blocked_audio_processing(chunk):
        processing_started.set()
        await processing_release.wait()
        return chunk

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._audio_processor = SimpleNamespace(
        noise_reduce_enabled=True,
        _denoiser=object(),
        speech_probability=1.0,
    )
    client.process_audio_chunk_async = blocked_audio_processing
    client.send_event = discard_event

    loud_frame = (1000).to_bytes(2, "little", signed=True) * 480
    ingress = asyncio.create_task(client.stream_audio(loud_frame))
    await asyncio.wait_for(processing_started.wait(), timeout=1)
    current_route[0] = ("example-game", "session-b", "route-b")
    processing_release.set()
    await asyncio.wait_for(ingress, timeout=1)

    client._bind_input_route_identity_to_item("provider-item-a")
    await client._deliver_input_transcript("hello", item_id="provider-item-a")

    assert delivered == [
        ("hello", ("example-game", "session-a", "route-a")),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_vad_does_not_capture_route_from_idle_silence():
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def passthrough_audio(chunk):
        return chunk

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._audio_processor = SimpleNamespace(
        noise_reduce_enabled=True,
        _denoiser=object(),
        speech_probability=0.0,
    )
    client.process_audio_chunk_async = passthrough_audio
    client.send_event = discard_event

    await client.stream_audio(bytes(960))
    assert client._input_route_identity_captured is False

    current_route[0] = ("example-game", "session-b", "route-b")
    client._audio_processor.speech_probability = 1.0
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 480
    await client.stream_audio(loud_frame)
    client._bind_input_route_identity_to_item("provider-item-b")
    await client._deliver_input_transcript("hello", item_id="provider-item-b")

    assert delivered == [
        ("hello", ("example-game", "session-b", "route-b")),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_vad_without_local_onset_binds_the_route_that_owned_the_frames():
    """Audio recorded under A stays owned by A, even if the route moved to B.

    The local onset gate never arms on these frames, but they were still
    captured under exactly one route, so that route is the provable owner.
    Reporting it (instead of an unroutable ``None``) lets
    ``handle_input_transcript`` do the rejecting: A's owner will not match the
    live B route, so the stale final is dropped there — while ordinary soft
    speech under a stable route keeps reaching the game.
    """
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._has_server_vad = True
    client.send_event = discard_event

    quiet_frame = (100).to_bytes(2, "little", signed=True) * 512
    await client.stream_audio(quiet_frame)
    assert client._input_route_identity_captured is False

    current_route[0] = ("example-game", "session-b", "route-b")
    client._bind_input_route_identity_to_item("provider-item-old-audio")
    await client._deliver_input_transcript(
        "old audio",
        item_id="provider-item-old-audio",
    )

    # Never the event-time route: that is what would misroute A's audio into B.
    assert delivered == [
        ("old audio", ("example-game", "session-a", "route-a")),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_soft_speech_under_a_stable_route_still_reaches_that_route():
    """The regression guard: a quiet utterance must not become unroutable.

    Server VAD and the client RMS/RNNoise gate are independent detectors with
    independent thresholds, so "server committed an utterance the local gate
    never heard" is ordinary. Such a final used to arrive with ``None`` and was
    dropped by ``handle_input_transcript`` before the takeover dispatcher, i.e.
    the player spoke and the game got nothing.
    """
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._has_server_vad = True
    client.send_event = discard_event

    quiet_frame = (100).to_bytes(2, "little", signed=True) * 512
    await client.stream_audio(quiet_frame)
    await client.stream_audio(quiet_frame)
    assert client._input_route_identity_captured is False

    client._bind_input_route_identity_to_item("provider-item-soft")
    await client._deliver_input_transcript("soft line", item_id="provider-item-soft")

    assert delivered == [
        ("soft line", ("example-game", "session-a", "route-a")),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_finished_utterance_does_not_poison_the_next_one():
    """Ownership observed for utterance N must not leak into utterance N+1.

    ``speech_started`` binds the open utterance and clears the observation, but
    the rest of that utterance keeps streaming and re-arms it. If that stale
    owner survives into the next utterance, the first frame under a new route
    reads as a mid-buffer route switch and the new, entirely valid transcript is
    delivered as ``None`` and dropped.
    """
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._has_server_vad = True
    client.send_event = discard_event
    quiet_frame = (100).to_bytes(2, "little", signed=True) * 512

    # Utterance A: frames, server onset, more frames, then its transcript.
    await client.stream_audio(quiet_frame)
    client._bind_input_route_identity_to_item("item-a")
    client._audio_in_buffer = True
    await client.stream_audio(quiet_frame)
    client._audio_in_buffer = False
    await client._deliver_input_transcript("first", item_id="item-a")

    # The route moves on, then an ordinary quiet utterance B arrives.
    current_route[0] = ("example-game", "session-b", "route-b")
    await client.stream_audio(quiet_frame)
    client._bind_input_route_identity_to_item("item-b")
    await client._deliver_input_transcript("second", item_id="item-b")

    assert delivered == [
        ("first", ("example-game", "session-a", "route-a")),
        ("second", ("example-game", "session-b", "route-b")),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_silence_buffer_clear_does_not_let_a_late_onset_grab_the_new_route():
    """With no frame evidence left, refuse rather than read the live route.

    ``stream_audio`` clears the input buffer itself on detected silence, which
    drops the frame observation. A ``speech_started`` the server already emitted
    for the pre-clear audio can land after that clear and after the route moved
    on. Answering with the live route would tag route A's audio as B and pass
    the ``handle_input_transcript`` comparison. Nothing legitimate is lost by
    refusing: real speech streams frames, and frames re-arm the observation.
    """
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._has_server_vad = True
    client.send_event = discard_event

    quiet_frame = (100).to_bytes(2, "little", signed=True) * 512
    await client.stream_audio(quiet_frame)
    assert client._input_route_identity_stream_armed is True

    # Silence clears the buffer, taking the frame evidence with it.
    await client.clear_audio_buffer()
    assert client._input_route_identity_stream_armed is False

    # The route moves on, then A's already-emitted onset finally lands.
    current_route[0] = ("example-game", "session-b", "route-b")
    client._bind_input_route_identity_to_item("provider-item-late")
    await client._deliver_input_transcript("stale A", item_id="provider-item-late")

    assert delivered == [("stale A", None)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idle_frames_before_a_route_switch_do_not_strand_the_next_utterance():
    """Stale ownership must never outlive the frames that produced it.

    An open microphone streams background frames under route A. The route then
    switches while still idle and the user speaks under B. Deriving a
    per-buffer verdict here used to leave A's ownership armed, read B's first
    frame as a mid-buffer route switch, and deliver B's perfectly valid final
    as an unroutable ``None`` -- silently dropping the first thing the player
    said after every route switch. Ownership is last-write-wins, so it
    self-corrects on the very next frame.
    """
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._has_server_vad = True
    client.send_event = discard_event

    quiet_frame = (100).to_bytes(2, "little", signed=True) * 512
    # Idle background audio under A; too quiet to arm the local onset gate.
    await client.stream_audio(quiet_frame)
    await client.stream_audio(quiet_frame)

    # The route moves on while idle, then the player actually speaks under B.
    current_route[0] = ("example-game", "session-b", "route-b")
    await client.stream_audio(quiet_frame)
    client._bind_input_route_identity_to_item("provider-item-b")
    await client._deliver_input_transcript("hello B", item_id="provider-item-b")

    assert delivered == [
        ("hello B", ("example-game", "session-b", "route-b")),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rnnoise_rejects_a_loud_noise_frame_before_route_ownership_freezes():
    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def passthrough_audio(chunk):
        return chunk

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client._audio_processor = SimpleNamespace(
        noise_reduce_enabled=True,
        _denoiser=object(),
        speech_probability=0.0,
    )
    client.process_audio_chunk_async = passthrough_audio
    client.send_event = discard_event
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 480

    await client.stream_audio(loud_frame)
    assert client._input_route_identity_captured is False

    current_route[0] = ("example-game", "session-b", "route-b")
    client._audio_processor.speech_probability = 1.0
    await client.stream_audio(loud_frame)
    client._bind_input_route_identity_to_item("provider-item-b")
    await client._deliver_input_transcript("hello", item_id="provider-item-b")

    assert delivered == [
        ("hello", ("example-game", "session-b", "route-b")),
    ]


@pytest.mark.unit
def test_server_vad_does_not_recapture_tail_frames_as_the_next_utterance():
    current_route = [("example-game", "session-a", "route-a")]
    client = _free_client(
        None,
        get_input_route_identity=lambda: current_route[0],
    )

    client._capture_input_route_identity()
    client._bind_input_route_identity_to_item("provider-item-a")
    client._audio_in_buffer = True
    current_route[0] = ("example-game", "session-b", "route-b")
    client._capture_input_route_identity()

    assert client._input_route_identity_captured is False
    assert client._input_route_identity is None

    client._audio_in_buffer = False
    client._capture_input_route_identity()
    assert client._input_route_identity == (
        "example-game", "session-b", "route-b",
    )


@pytest.mark.unit
def test_evicted_server_vad_item_does_not_consume_the_next_utterance_owner():
    current_route = [("example-game", "session-0", "route-0")]
    client = _free_client(
        None,
        get_input_route_identity=lambda: current_route[0],
    )
    client._has_server_vad = True

    for index in range(_transport._INPUT_ROUTE_IDENTITY_ITEM_LIMIT + 1):
        current_route[0] = (
            "example-game",
            f"session-{index}",
            f"route-{index}",
        )
        client._capture_input_route_identity()
        client._bind_input_route_identity_to_item(f"provider-item-{index}")

    current_route[0] = ("example-game", "session-next", "route-next")
    client._capture_input_route_identity()

    assert client._take_input_route_identity("provider-item-0") is None
    assert client._take_input_route_identity() == (
        "example-game", "session-next", "route-next",
    )


@pytest.mark.unit
def test_manual_vad_item_id_consumes_the_ingress_owner_without_an_item_mapping():
    current_route = [("example-game", "session-a", "route-a")]
    client = _free_client(
        None,
        get_input_route_identity=lambda: current_route[0],
    )
    client._has_server_vad = False

    client._capture_input_route_identity()

    assert client._take_input_route_identity("provider-manual-item") == (
        "example-game", "session-a", "route-a",
    )
    assert client._input_route_identity is None
    assert client._input_route_identity_captured is False


# ---------------------------------------------------------------------------
# Contract 3 first: the shape of an ordinary turn is the baseline everything
# else is measured against.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_ordinary_turn_still_runs_both_hooks_in_order():
    host = _Host()
    client = _free_client(host, get_host_turn_id=host.read_speech_id)
    _begin_turn(client)

    await client._notify_turn_finished()

    assert host.calls == ["response_done", "sid_rotate"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_vad_rotation_owns_the_next_unannounced_response():
    """An id-less no-VAD response still finishes under the rotated host turn."""

    host = _Host()

    async def deliver_transcript(_text, _is_first):
        return None

    client = _free_client(
        host,
        get_host_turn_id=host.read_speech_id,
        on_output_transcript=deliver_transcript,
    )
    socket = _RecordingSocket()
    client.ws = socket
    receive_loop = asyncio.create_task(client.handle_messages())

    socket.feed({"type": "response.created", "response": {"id": "first"}})
    socket.feed(
        {
            "type": "response.done",
            "response": {"id": "first", "status": "completed"},
        }
    )
    await _settle()
    assert client._current_turn_host_id == "sid-rotated-by-hook"
    host.calls.clear()

    socket.feed(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "user-2",
            "transcript": "next",
        }
    )
    socket.feed({"type": "response.audio_transcript.delta", "delta": "reply"})
    socket.feed({"type": "response.done", "response": {"status": "completed"}})
    await _settle()

    assert host.calls == ["response_done", "sid_rotate"]

    empty_ticket = await client._response_arbiter.enqueue(
        source="empty-idless-response",
        response_started_timeout=0.5,
        response_done_timeout=0.5,
    )
    await asyncio.wait_for(empty_ticket.sent, timeout=1)
    socket.feed({"type": "response.done", "response": {"status": "completed"}})
    await _settle()

    assert empty_ticket.done.done()
    assert host.calls == [
        "response_done",
        "sid_rotate",
        "response_done",
        "sid_rotate",
    ]

    socket.finish()
    await asyncio.wait_for(receive_loop, timeout=1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stuck_release_does_not_carry_ownership_to_a_late_terminal():
    """Only a normal terminal may carry the rotated host turn forward."""

    host = _Host()
    client = _free_client(host, get_host_turn_id=host.read_speech_id)
    client._begin_response_lifecycle("stuck")

    await client._on_arbiter_stuck_release("probe", response_id="stuck")
    assert host.calls == ["response_done", "sid_rotate"]

    socket = _RecordingSocket()
    client.ws = socket
    receive_loop = asyncio.create_task(client.handle_messages())
    socket.feed({"type": "response.done", "response": {"status": "completed"}})
    await _settle()

    assert host.calls == ["response_done", "sid_rotate"]

    socket.finish()
    await asyncio.wait_for(receive_loop, timeout=1)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["canceled", "failed", "incomplete"])
async def test_abnormal_terminal_does_not_carry_host_turn_forward(status):
    """Only successful completion may own a later unannounced response."""

    host = _Host()
    client = _free_client(host, get_host_turn_id=host.read_speech_id)
    socket = _RecordingSocket()
    client.ws = socket
    receive_loop = asyncio.create_task(client.handle_messages())

    socket.feed({"type": "response.created", "response": {"id": "abnormal"}})
    socket.feed(
        {
            "type": "response.done",
            "response": {"id": "abnormal", "status": status},
        }
    )
    await _settle()

    assert host.calls == ["response_done", "sid_rotate"]
    assert client._current_turn_host_id == "sid-turn-1"
    assert host.speech_id == "sid-rotated-by-hook"

    socket.feed({"type": "response.done", "response": {"status": "completed"}})
    await _settle()
    assert host.calls == ["response_done", "sid_rotate"]

    socket.finish()
    await asyncio.wait_for(receive_loop, timeout=1)


# ---------------------------------------------------------------------------
# Contract 1: the host moved on before the notification ran.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_neither_hook_runs_once_the_host_is_on_a_new_turn():
    host = _Host()
    client = _free_client(host, get_host_turn_id=host.read_speech_id)
    _begin_turn(client)
    # A text input or an independent-ASR utterance took a fresh speech id.
    # Nothing about it reached this transport, so the turn epoch is unchanged.
    epoch_before = client._turn_epoch
    host.starts_a_new_turn()
    assert client._turn_epoch == epoch_before, (
        "the premise of this test is that the epoch cannot see this turn "
        "start; if it can, the guard under test is not the one being exercised"
    )

    await client._notify_turn_finished()

    assert host.calls == [], (
        "the dead turn must not announce its end under the live one, and must "
        "not rotate away the speech id the live one is speaking under"
    )
    assert host.speech_id == "sid-turn-2", "the live turn keeps its own id"


# ---------------------------------------------------------------------------
# Contract 2: the host moved on WHILE the notification ran.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_rotation_stands_down_when_a_turn_starts_during_response_done():
    host = _Host()
    host.block_in_response_done = asyncio.Event()
    client = _free_client(host, get_host_turn_id=host.read_speech_id)
    _begin_turn(client)

    notify = asyncio.create_task(client._notify_turn_finished())
    for _ in range(10):
        await asyncio.sleep(0)
    assert host.calls == ["response_done"], "the first hook should be in flight"

    host.starts_a_new_turn()
    host.block_in_response_done.set()
    await asyncio.wait_for(notify, timeout=1)

    assert host.calls == ["response_done"], (
        "the rotation is the step that discards the live turn's speech id; on "
        "a provider without server VAD that silences every later turn"
    )
    assert host.speech_id == "sid-turn-2"


# ---------------------------------------------------------------------------
# Contract 4: unwired hosts and hosts that cannot answer.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_the_accessor_the_hooks_are_unconditional():
    host = _Host()
    client = _free_client(host)  # no get_host_turn_id
    _begin_turn(client)
    assert client._current_turn_host_id is None
    host.starts_a_new_turn()

    await client._notify_turn_finished()

    assert host.calls == ["response_done", "sid_rotate"], (
        "an unwired client must behave exactly as it did before #2612"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_host_that_cannot_answer_gets_notified_anyway():
    host = _Host()

    def _raises() -> str | None:
        raise RuntimeError("host is mid-teardown")

    client = _free_client(host, get_host_turn_id=host.read_speech_id)
    _begin_turn(client)
    assert client._current_turn_host_id == "sid-turn-1"
    client.get_host_turn_id = _raises

    # Its own handler rather than ``caplog``: this module's logger does not
    # always propagate to the root once the app's logging setup has been
    # imported by another test, and a log assertion that quietly depends on
    # test ordering is worse than no log assertion.
    with _records_from(_transport.logger) as logged:
        await client._notify_turn_finished()

    assert host.calls == ["response_done", "sid_rotate"], (
        "withholding the end of a turn is the worse failure; an unreadable "
        "host disables the guard rather than the hooks"
    )
    assert any("turn guard is off" in line for line in logged), (
        "a guard that silently stopped guarding is the thing nobody notices"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_whole_turn_through_the_receive_loop_samples_and_compares():
    """The sample point, not just the guard.

    Every case above puts the client where ``response.created`` leaves it by
    hand, so all of them would still pass if the handler stopped sampling the
    host id at all. This one lets the real loop do it: created, then the host
    takes a turn of its own, then the provider's terminal arrives.
    """
    host = _Host()
    client = _free_client(host, get_host_turn_id=host.read_speech_id)
    socket = _RecordingSocket()
    client.ws = socket
    receive_loop = asyncio.create_task(client.handle_messages())

    socket.feed({"type": "response.created", "response": {"id": "resp-1"}})
    await _settle()
    assert client._current_turn_host_id == "sid-turn-1", (
        "the created handler is where the turn's host identity is taken"
    )

    host.starts_a_new_turn()
    socket.feed({"type": "response.done", "response": {"id": "resp-1"}})
    await _settle()

    assert host.calls == []
    assert host.speech_id == "sid-turn-2"

    socket.finish()
    await asyncio.wait_for(receive_loop, timeout=1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_turn_that_began_before_the_host_had_an_id_is_not_guarded():
    """``None`` is "nothing to compare", not "everything is different"."""
    host = _Host()
    host.speech_id = None
    client = _free_client(host, get_host_turn_id=host.read_speech_id)
    _begin_turn(client)
    host.speech_id = "sid-turn-2"

    await client._notify_turn_finished()

    assert host.calls == ["response_done", "sid_rotate"]


class _StubGeminiTypes:
    """Minimal stand-in for ``google.genai.types`` at the MANUAL commit."""

    class ActivityEnd:
        pass


class _StubGeminiSession:
    """Records activity_end sends and can fail on a still-usable connection."""

    def __init__(self, error: Exception | None = None) -> None:
        self.activity_ends = 0
        self._error = error

    async def send_realtime_input(self, **kwargs):
        # Gemini audio streaming uses this same method, so only the turn
        # boundary is counted here.
        if "activity_end" not in kwargs:
            return
        if self._error is not None:
            raise self._error
        self.activity_ends += 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_commit_freezes_ownership_before_a_later_route_streams(monkeypatch):
    """MANUAL mode has no `speech_started`, so the commit must pin the owner.

    Server VAD binds an owner when it reports the onset; MANUAL disables server
    VAD entirely, so nothing bound the buffer being committed. A route starting
    after the commit streams frames that move the last-write-wins mark, and the
    already-committed utterance would then be delivered to that newer route
    while its transcription was still in flight -- the exact misattribution this
    ownership exists to prevent.
    """
    from main_logic.omni_realtime_client import TurnDetectionMode

    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client.turn_detection_mode = TurnDetectionMode.MANUAL
    client._has_server_vad = False
    client.send_event = discard_event
    client._is_gemini = True
    client._gemini_session = _StubGeminiSession()
    monkeypatch.setattr(_gemini_support, "types", _StubGeminiTypes)

    quiet_frame = (100).to_bytes(2, "little", signed=True) * 512
    await client.stream_audio(quiet_frame)
    assert client._input_route_identity_captured is False

    await client.signal_user_activity_end()
    assert client._gemini_session.activity_ends == 1
    assert client._input_route_identity_captured is True

    # A replacement route opens and streams before the transcript lands.
    current_route[0] = ("example-game", "session-b", "route-b")
    await client.stream_audio(quiet_frame)
    await client._deliver_input_transcript("committed under A", item_id="manual-item")

    assert delivered == [
        ("committed under A", ("example-game", "session-a", "route-a")),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_manual_commit_leaves_ownership_unfrozen(monkeypatch):
    """A commit that never reached the provider must not pin an owner.

    ``send_realtime_input`` can raise on a connection that stays usable (only a
    "closed" error is treated as fatal). That buffer is never committed, so it
    will never produce a transcript -- but a freeze left behind outlives it and
    answers for the NEXT utterance instead. After a route change every one of
    those is rejected as a mismatch and dropped before the takeover dispatcher,
    which is silent: the player speaks and the game receives nothing.
    """
    from main_logic.omni_realtime_client import TurnDetectionMode

    current_route = [("example-game", "session-a", "route-a")]
    delivered = []

    async def on_transcript_with_route(text, *, source_game_route_identity):
        delivered.append((text, source_game_route_identity))

    async def discard_event(_event):
        return None

    client = _free_client(
        None,
        on_input_transcript_with_route=on_transcript_with_route,
        get_input_route_identity=lambda: current_route[0],
    )
    client.turn_detection_mode = TurnDetectionMode.MANUAL
    client._has_server_vad = False
    client.send_event = discard_event
    client._is_gemini = True
    client._gemini_session = _StubGeminiSession(error=RuntimeError("transient send failure"))
    monkeypatch.setattr(_gemini_support, "types", _StubGeminiTypes)

    quiet_frame = (100).to_bytes(2, "little", signed=True) * 512
    await client.stream_audio(quiet_frame)
    await client.signal_user_activity_end()

    # Not fatal, so the connection is reused -- and nothing may be pinned.
    assert client._fatal_error_occurred is False
    assert client._input_route_identity_captured is False

    # The next utterance, under a new route, must still reach that new route.
    current_route[0] = ("example-game", "session-b", "route-b")
    await client.stream_audio(quiet_frame)
    await client._deliver_input_transcript("spoken under B", item_id="manual-item-2")

    assert delivered == [
        ("spoken under B", ("example-game", "session-b", "route-b")),
    ]
