from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import main_logic.asr_client._infra as asr_infra
from main_logic.asr_client._infra import (
    AsrSessionConfig,
    _AsrRequestQueue,
    _AsrWorkerEvent,
    _AsrWorkerRequest,
    _RealtimeAsrSessionImpl,
)
from main_logic.asr_client._provider_events import ProviderAudioRange
from main_logic.asr_client.workers import gemini, grok, openai, qwen, soniox, step


_END = object()
_TIMEOUT = object()


class _FakeWebSocket:
    def __init__(
        self,
        *,
        initial: list[dict[str, Any]] | None = None,
        on_send: Callable[["_FakeWebSocket", str | bytes], Awaitable[None]]
        | None = None,
    ) -> None:
        self.incoming: asyncio.Queue[str | object] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.closed = False
        self.on_send = on_send
        for event in initial or []:
            self.incoming.put_nowait(json.dumps(event))

    async def send(self, payload: str | bytes) -> None:
        if self.closed:
            raise RuntimeError("fake websocket is closed")
        self.sent.append(payload)
        if self.on_send is not None:
            await self.on_send(self, payload)

    async def recv(self) -> str:
        message = await self.incoming.get()
        if message is _END:
            raise RuntimeError("fake websocket closed before ready")
        if message is _TIMEOUT:
            raise asyncio.TimeoutError
        assert isinstance(message, str)
        return message

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        message = await self.incoming.get()
        if message is _END:
            raise StopAsyncIteration
        assert isinstance(message, str)
        return message

    async def server_send(self, event: dict[str, Any]) -> None:
        await self.incoming.put(json.dumps(event))

    async def server_end(self) -> None:
        await self.incoming.put(_END)

    async def server_timeout(self) -> None:
        # Makes the next recv() raise asyncio.TimeoutError, simulating a
        # bounded receive wait expiring without tearing the connection down.
        await self.incoming.put(_TIMEOUT)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.incoming.put(_END)


class _FakeConnector:
    def __init__(self, *websockets: _FakeWebSocket) -> None:
        self.websockets = list(websockets)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **kwargs: Any) -> _FakeWebSocket:
        self.calls.append((url, kwargs))
        if not self.websockets:
            raise AssertionError("unexpected extra WebSocket connection")
        return self.websockets.pop(0)


async def _next_event(
    queue: asyncio.Queue[_AsrWorkerEvent],
    kind: str | None = None,
    *,
    timeout: float = 1.0,
) -> _AsrWorkerEvent:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout)
        queue.task_done()
        if kind is None or event.kind == kind:
            return event


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not reached")
        await asyncio.sleep(0)


async def _stop_worker(
    task: asyncio.Task[None],
    requests: asyncio.Queue[_AsrWorkerRequest],
    responses: asyncio.Queue[_AsrWorkerEvent],
    *,
    generation: int = 0,
    buffer_epoch: int = 0,
    utterance_id: int = 1,
) -> _AsrWorkerEvent:
    await requests.put(
        _AsrWorkerRequest(
            kind="shutdown",
            generation=generation,
            buffer_epoch=buffer_epoch,
            utterance_id=utterance_id,
        )
    )
    closed = await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    await asyncio.wait_for(requests.join(), 1)
    return closed


async def _qwen_provider_lifecycle_on_send(
    ws: _FakeWebSocket,
    payload: str | bytes,
) -> None:
    if not isinstance(payload, str):
        return
    message = json.loads(payload)
    if message["type"] == "session.update":
        await ws.server_send({"type": "session.updated"})
    elif message["type"] == "session.finish":
        await ws.server_send({"type": "session.finished"})


async def _start_qwen_provider_worker(
    monkeypatch,
    *websockets_: _FakeWebSocket,
) -> tuple[
    asyncio.Task[None],
    asyncio.Queue[_AsrWorkerRequest],
    asyncio.Queue[_AsrWorkerEvent],
    _FakeConnector,
]:
    connector = _FakeConnector(*websockets_)
    monkeypatch.setattr(qwen.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    ready = await _next_event(responses)
    assert ready.kind == "ready"
    return task, requests, responses, connector


async def test_qwen_duplicate_item_created_preserves_next_manual_commit(
    monkeypatch,
) -> None:
    commit_count = 0

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        nonlocal commit_count
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {"type": "session.updated", "session": message["session"]}
            )
        elif message["type"] == "input_audio_buffer.commit":
            commit_count += 1
        elif message["type"] == "session.finish":
            await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(requests, responses, "key", AsrSessionConfig())
    )
    await _next_event(responses, "ready")

    for utterance_id in (1, 2):
        await requests.put(
            _AsrWorkerRequest(
                kind="audio",
                generation=0,
                utterance_id=utterance_id,
                audio=b"\0\0",
            )
        )
        await requests.put(
            _AsrWorkerRequest(
                kind="commit",
                generation=0,
                utterance_id=utterance_id,
            )
        )
    await asyncio.wait_for(requests.join(), 1)
    assert commit_count == 2

    await websocket.server_send(
        {"type": "conversation.item.created", "item": {"id": "first"}}
    )
    await websocket.server_send(
        {"type": "conversation.item.created", "item": {"id": "first"}}
    )
    await websocket.server_send(
        {"type": "conversation.item.created", "item": {"id": "second"}}
    )
    for item_id, transcript in (("first", "one"), ("second", "two")):
        await websocket.server_send(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item_id,
                "transcript": transcript,
            }
        )

    first = await _next_event(responses, "final")
    second = await _next_event(responses, "final")
    assert (first.utterance_id, first.text) == (1, "one")
    assert (second.utterance_id, second.text) == (2, "two")
    await _stop_worker(task, requests, responses, utterance_id=3)


@pytest.mark.parametrize(
    ("region", "domain", "legacy_without_item_ids"),
    [
        ("cn", "dashscope.aliyuncs.com", True),
        ("intl", "dashscope-intl.aliyuncs.com", False),
    ],
)
async def test_qwen_manual_regions_payload_and_final(
    monkeypatch,
    region: str,
    domain: str,
    legacy_without_item_ids: bool,
) -> None:
    commit_count = 0

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        nonlocal commit_count
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})
        elif message["type"] == "input_audio_buffer.commit":
            commit_count += 1
            item_id = f"qwen-{commit_count}"
            if legacy_without_item_ids:
                await ws.server_send(
                    {
                        "type": "conversation.item.created",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_audio"}],
                        },
                    }
                )
                await ws.server_send({"type": "input_audio_buffer.committed"})
            else:
                await ws.server_send(
                    {"type": "input_audio_buffer.committed", "item_id": item_id}
                )
            await ws.server_send(
                {
                    "type": "conversation.item.input_audio_transcription.text",
                    "text": "你",
                    "stash": "好",
                    **({} if legacy_without_item_ids else {"item_id": item_id}),
                }
            )
            await ws.server_send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "你好",
                    **({} if legacy_without_item_ids else {"item_id": item_id}),
                }
            )
        elif message["type"] == "session.finish":
            await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(qwen.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "secret-qwen-key",
            AsrSessionConfig(language="zh-CN"),
            region=region,
        )
    )

    assert (await _next_event(responses, "ready")).generation == 0
    pcm = b"\x01\x02" * 320
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=pcm)
    )
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))

    partial = await _next_event(responses, "partial")
    final = await _next_event(responses, "final")
    assert (partial.text, final.text, final.utterance_id) == ("你好", "你好", 1)
    assert not task.done(), "commit must keep the provider connection open"

    url, kwargs = connector.calls[0]
    assert urlparse(url).hostname == domain
    assert parse_qs(urlparse(url).query)["model"] == ["qwen3-asr-flash-realtime"]
    assert kwargs["additional_headers"] == {"Authorization": "Bearer secret-qwen-key"}
    messages = [json.loads(payload) for payload in websocket.sent]
    session = next(
        message for message in messages if message["type"] == "session.update"
    )
    assert session["session"]["sample_rate"] == 16_000
    assert session["session"]["turn_detection"] is None
    append = next(
        message
        for message in messages
        if message["type"] == "input_audio_buffer.append"
    )
    assert base64.b64decode(append["audio"]) == pcm

    await _stop_worker(task, requests, responses)


async def test_qwen_server_vad_maps_items_and_reconnects_on_clear(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    first = _FakeWebSocket(on_send=on_send)
    second = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(qwen.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(
            kind="audio", generation=0, buffer_epoch=0, utterance_id=1, audio=b"\0\0"
        )
    )
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "one"}
    )
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "one"}
    )
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "two"}
    )
    started_one = await _next_event(responses, "utterance_started")
    started_two = await _next_event(responses, "utterance_started")
    assert (started_one.utterance_id, started_two.utterance_id) == (1, 2)

    for item_id, text in (("two", "second"), ("one", "first")):
        await first.server_send(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item_id,
                "transcript": text,
            }
        )
    final_two = await _next_event(responses, "final")
    final_one = await _next_event(responses, "final")
    assert (final_two.utterance_id, final_two.text) == (2, "second")
    assert (final_one.utterance_id, final_one.text) == (1, "first")

    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=0, buffer_epoch=1, utterance_id=3)
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=0,
            buffer_epoch=1,
            utterance_id=3,
            audio=b"\x03\x04",
        )
    )
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(
        lambda: any(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "input_audio_buffer.append"
            for payload in second.sent
        )
    )
    assert len(connector.calls) == 2
    await _stop_worker(
        task,
        requests,
        responses,
        buffer_epoch=1,
        utterance_id=3,
    )


async def test_qwen_server_vad_emits_exact_canonical_boundary(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "exact",
            "audio_start_ms": 20,
        }
    )
    started = await _next_event(responses, "utterance_started")
    assert started.audio_start_sample_16k == 320
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "exact",
            "audio_end_ms": 170,
        }
    )
    boundary = await _next_event(responses, "provider_endpoint")
    assert boundary.utterance_id == started.utterance_id
    assert boundary.boundary_quality == "exact"
    assert boundary.audio_range == ProviderAudioRange(320, 2_720)

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "exact",
            "transcript": "hello",
        }
    )
    final = await _next_event(responses)
    assert (final.kind, final.utterance_id, final.text) == (
        "final",
        started.utterance_id,
        "hello",
    )
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_conflicting_duplicate_boundary_revokes_exact_authority(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "conflict",
            "audio_start_ms": 0,
        }
    )
    await _next_event(responses, "utterance_started")
    stopped = {
        "type": "input_audio_buffer.speech_stopped",
        "item_id": "conflict",
        "audio_end_ms": 100,
    }
    await websocket.server_send(stopped)
    exact = await _next_event(responses, "provider_endpoint")
    assert exact.boundary_quality == "exact"
    await websocket.server_send(stopped)
    await websocket.server_send({**stopped, "audio_end_ms": 120})
    invalidated = await _next_event(responses)
    assert invalidated.kind == "provider_endpoint"
    assert invalidated.boundary_quality == "unknown"
    assert invalidated.audio_range is None

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "conflict",
            "transcript": "done",
        }
    )
    assert (await _next_event(responses)).kind == "final"
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_conflicting_late_speech_start_revokes_exact_authority(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "start-conflict",
            "audio_start_ms": 0,
        }
    )
    started = await _next_event(responses, "utterance_started")
    assert started.audio_start_sample_16k == 0
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "start-conflict",
            "audio_end_ms": 100,
        }
    )
    exact = await _next_event(responses, "provider_endpoint")
    assert exact.boundary_quality == "exact"

    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "start-conflict",
            "audio_start_ms": 20,
        }
    )
    invalidated = await _next_event(responses)
    assert invalidated.kind == "provider_endpoint"
    assert invalidated.utterance_id == started.utterance_id
    assert invalidated.boundary_quality == "unknown"
    assert invalidated.audio_range is None
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "start-conflict",
            "transcript": "safe",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (started.utterance_id, "safe")
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_missing_item_ids_use_unknown_fifo_without_losing_final(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": 0}
    )
    started = await _next_event(responses, "utterance_started")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 100}
    )
    boundary = await _next_event(responses, "provider_endpoint")
    assert boundary.utterance_id == started.utterance_id
    assert boundary.boundary_quality == "unknown"
    assert boundary.audio_range is None

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "preserved",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (
        started.utterance_id,
        "preserved",
    )
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_unmatched_completed_item_uses_fifo_and_revokes_exact(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "audio-item",
            "audio_start_ms": 0,
        }
    )
    started = await _next_event(responses, "utterance_started")
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "audio-item",
            "audio_end_ms": 100,
        }
    )
    assert (await _next_event(responses, "provider_endpoint")).boundary_quality == (
        "exact"
    )

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "transcription-item",
            "transcript": "same turn",
        }
    )
    invalidated = await _next_event(responses, "provider_endpoint")
    assert invalidated.utterance_id == started.utterance_id
    assert invalidated.boundary_quality == "unknown"
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (
        started.utterance_id,
        "same turn",
    )
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_retired_item_tombstone_blocks_late_resurrection(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "retired",
            "audio_start_ms": 0,
        }
    )
    await _next_event(responses, "utterance_started")
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "retired",
            "transcript": "once",
        }
    )
    await _next_event(responses, "provider_endpoint")
    assert (await _next_event(responses, "final")).text == "once"

    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "retired",
            "audio_start_ms": 0,
        }
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "retired",
            "transcript": "duplicate",
        }
    )
    await asyncio.sleep(0.05)
    assert responses.empty()

    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "fresh",
            "audio_start_ms": 100,
        }
    )
    fresh = await _next_event(responses, "utterance_started")
    assert fresh.utterance_id == 2
    await _stop_worker(task, requests, responses, utterance_id=3)


def test_qwen_retired_item_tombstone_is_bounded() -> None:
    state = qwen._QwenConnectionState(
        generation=0,
        buffer_epoch=0,
        next_utterance_id=1,
        emit_ready=False,
    )
    for index in range(qwen._QWEN_ITEM_TOMBSTONE_LIMIT + 2):
        qwen._qwen_remember_retired_item(state, f"item-{index}")

    assert len(state.retired_item_ids) == qwen._QWEN_ITEM_TOMBSTONE_LIMIT
    assert len(state.retired_item_order) == qwen._QWEN_ITEM_TOMBSTONE_LIMIT
    assert "item-0" not in state.retired_item_ids
    assert "item-1" not in state.retired_item_ids
    assert f"item-{qwen._QWEN_ITEM_TOMBSTONE_LIMIT + 1}" in state.retired_item_ids


def test_qwen_provisional_commits_are_globally_bounded() -> None:
    state = qwen._QwenConnectionState(
        generation=0,
        buffer_epoch=0,
        next_utterance_id=1,
        emit_ready=False,
    )
    for index in range(qwen._QWEN_PROVISIONAL_COMMIT_LIMIT + 2):
        qwen._qwen_record_provisional_commit(state, f"named-{index}")
        qwen._qwen_record_provisional_commit(state, "")

    assert (
        len(state.provisional_commits)
        + len(state.anonymous_provisional_commits)
        == qwen._QWEN_PROVISIONAL_COMMIT_LIMIT
    )
    assert "named-0" not in state.provisional_commits


async def test_qwen_terminal_fifo_claim_beats_stalled_expiry_during_unknown_put(
    monkeypatch,
) -> None:
    monkeypatch.setattr(qwen, "_QWEN_STALLED_ITEM_TIMEOUT_SECONDS", 0.0)
    state = qwen._QwenConnectionState(
        generation=0,
        buffer_epoch=0,
        next_utterance_id=2,
        emit_ready=False,
    )
    key = (0, 0, 1)
    state.item_keys["audio-item"] = key
    state.item_start_samples_16k["audio-item"] = 0
    state.item_boundaries["audio-item"] = ProviderAudioRange(0, 1_600)
    state.provider_item_order.append("audio-item")
    state.provider_item_aliases["audio-item"] = "audio-item"
    state.item_deadlines["audio-item"] = 0.0
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue(maxsize=1)
    await responses.put(_AsrWorkerEvent(kind="ready", generation=0))

    resolve_task = asyncio.create_task(
        qwen._qwen_resolve_provider_item(
            responses,
            state,
            "transcription-item",
            terminal=True,
        )
    )
    await _wait_until(lambda: "audio-item" in state.completing_provider_items)
    await qwen._qwen_expire_stalled_items(responses, state)
    assert state.item_keys["audio-item"] == key

    assert (await _next_event(responses)).kind == "ready"
    assert await resolve_task == "audio-item"
    invalidated = await _next_event(responses, "provider_endpoint")
    assert invalidated.boundary_quality == "unknown"
    assert state.item_keys["audio-item"] == key


async def test_qwen_terminal_open_claim_precedes_started_queue_put() -> None:
    state = qwen._QwenConnectionState(
        generation=0,
        buffer_epoch=0,
        next_utterance_id=1,
        emit_ready=False,
    )
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue(maxsize=1)
    await responses.put(_AsrWorkerEvent(kind="ready", generation=0))

    open_task = asyncio.create_task(
        qwen._qwen_open_provider_item(
            responses,
            state,
            "completed-without-start",
            start_sample_16k=None,
            ambiguous=True,
            terminal=True,
        )
    )
    await _wait_until(
        lambda: "completed-without-start" in state.completing_provider_items
    )
    assert not open_task.done()
    assert state.item_keys["completed-without-start"] == (0, 0, 1)

    assert (await _next_event(responses)).kind == "ready"
    assert await open_task == "completed-without-start"
    started = await _next_event(responses)
    assert (started.kind, started.utterance_id) == ("utterance_started", 1)


async def test_qwen_committed_before_stopped_keeps_single_key_and_exact_boundary(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "committed-first",
            "audio_start_ms": 20,
        }
    )
    started = await _next_event(responses)
    assert (started.kind, started.utterance_id) == ("utterance_started", 1)
    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "committed-first"}
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)

    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "committed-first",
            "audio_end_ms": 170,
        }
    )
    boundary = await _next_event(responses)
    assert boundary.kind == "provider_endpoint"
    assert boundary.utterance_id == started.utterance_id
    assert boundary.boundary_quality == "exact"
    assert boundary.audio_range == ProviderAudioRange(320, 2_720)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "committed-first",
            "transcript": "done",
        }
    )
    final = await _next_event(responses)
    assert (final.kind, final.utterance_id, final.text) == (
        "final",
        started.utterance_id,
        "done",
    )
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_stopped_before_committed_preserves_exact_boundary(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "stopped-first",
            "audio_start_ms": 0,
        }
    )
    started = await _next_event(responses)
    assert started.kind == "utterance_started"
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "stopped-first",
            "audio_end_ms": 100,
        }
    )
    boundary = await _next_event(responses)
    assert boundary.boundary_quality == "exact"
    assert boundary.audio_range == ProviderAudioRange(0, 1_600)

    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "stopped-first"}
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "stopped-first",
            "transcript": "kept exact",
        }
    )
    final = await _next_event(responses)
    assert (final.kind, final.utterance_id, final.text) == (
        "final",
        started.utterance_id,
        "kept exact",
    )
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_committed_without_stopped_fails_open_only_on_completed(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "completed-fallback",
            "audio_start_ms": 0,
        }
    )
    started = await _next_event(responses)
    assert started.kind == "utterance_started"
    await websocket.server_send(
        {
            "type": "input_audio_buffer.committed",
            "item_id": "completed-fallback",
        }
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "completed-fallback",
            "transcript": "safe text",
        }
    )
    boundary = await _next_event(responses)
    final = await _next_event(responses)
    assert (
        boundary.kind,
        boundary.utterance_id,
        boundary.boundary_quality,
        boundary.audio_range,
    ) == ("provider_endpoint", started.utterance_id, "unknown", None)
    assert (final.kind, final.utterance_id, final.text) == (
        "final",
        started.utterance_id,
        "safe text",
    )
    await _stop_worker(task, requests, responses, utterance_id=2)


@pytest.mark.parametrize("payload", [{"item_id": "orphan"}, {}])
async def test_qwen_orphan_committed_expires_without_ghost_turn(
    monkeypatch,
    payload: dict[str, str],
) -> None:
    monkeypatch.setattr(qwen, "_QWEN_STALLED_ITEM_TIMEOUT_SECONDS", 0.03)
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send({"type": "input_audio_buffer.committed", **payload})
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.1)

    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "fresh-after-orphan",
            "audio_start_ms": 0,
        }
    )
    started = await _next_event(responses)
    assert (started.kind, started.utterance_id) == ("utterance_started", 1)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "fresh-after-orphan",
            "transcript": "fresh",
        }
    )
    boundary = await _next_event(responses)
    final = await _next_event(responses)
    assert boundary.boundary_quality == "unknown"
    assert (final.kind, final.utterance_id, final.text) == ("final", 1, "fresh")
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_anonymous_committed_promotes_started_item_without_ghost(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send({"type": "input_audio_buffer.committed"})
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "promoted",
            "audio_start_ms": 10,
        }
    )
    started = await _next_event(responses)
    assert (started.kind, started.utterance_id) == ("utterance_started", 1)
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "promoted",
            "audio_end_ms": 110,
        }
    )
    boundary = await _next_event(responses)
    assert boundary.boundary_quality == "exact"
    assert boundary.audio_range == ProviderAudioRange(160, 1_760)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "promoted",
            "transcript": "one turn",
        }
    )
    final = await _next_event(responses)
    assert (final.kind, final.utterance_id, final.text) == (
        "final",
        started.utterance_id,
        "one turn",
    )
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_committed_completed_without_started_preserves_text(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "no-start"}
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "no-start",
            "transcript": "preserved",
        }
    )
    started = await _next_event(responses)
    boundary = await _next_event(responses)
    final = await _next_event(responses)
    assert (started.kind, started.utterance_id) == ("utterance_started", 1)
    assert (
        boundary.kind,
        boundary.utterance_id,
        boundary.boundary_quality,
    ) == ("provider_endpoint", 1, "unknown")
    assert (final.kind, final.utterance_id, final.text) == (
        "final",
        1,
        "preserved",
    )
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_named_provisional_completed_keeps_distinct_overlapping_key(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "active-a",
            "audio_start_ms": 0,
        }
    )
    first_started = await _next_event(responses)
    assert (first_started.kind, first_started.utterance_id) == (
        "utterance_started",
        1,
    )

    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "provisional-b"}
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "provisional-b",
            "transcript": "second",
        }
    )
    second_started = await _next_event(responses)
    second_boundary = await _next_event(responses)
    second_final = await _next_event(responses)
    assert (second_started.kind, second_started.utterance_id) == (
        "utterance_started",
        2,
    )
    assert (
        second_boundary.kind,
        second_boundary.utterance_id,
        second_boundary.boundary_quality,
    ) == ("provider_endpoint", 2, "unknown")
    assert (second_final.kind, second_final.utterance_id, second_final.text) == (
        "final",
        2,
        "second",
    )

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "active-a",
            "transcript": "first",
        }
    )
    first_boundary = await _next_event(responses)
    first_final = await _next_event(responses)
    assert (
        first_boundary.kind,
        first_boundary.utterance_id,
        first_boundary.boundary_quality,
    ) == ("provider_endpoint", 1, "unknown")
    assert (first_final.kind, first_final.utterance_id, first_final.text) == (
        "final",
        1,
        "first",
    )
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_qwen_expired_named_provisional_allows_late_completed_text(
    monkeypatch,
) -> None:
    monkeypatch.setattr(qwen, "_QWEN_STALLED_ITEM_TIMEOUT_SECONDS", 0.03)
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "expired"}
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.1)

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "expired",
            "transcript": "late but real",
        }
    )
    started = await _next_event(responses)
    boundary = await _next_event(responses)
    final = await _next_event(responses)
    assert (started.kind, started.utterance_id) == ("utterance_started", 1)
    assert (
        boundary.kind,
        boundary.utterance_id,
        boundary.boundary_quality,
    ) == ("provider_endpoint", 1, "unknown")
    assert (final.kind, final.utterance_id, final.text) == (
        "final",
        1,
        "late but real",
    )
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_qwen_completed_before_stopped_ignores_late_boundary(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, _connector = await _start_qwen_provider_worker(
        monkeypatch,
        websocket,
    )
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "early-completed",
            "audio_start_ms": 0,
        }
    )
    started = await _next_event(responses)
    assert started.kind == "utterance_started"
    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "early-completed"}
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "early-completed",
            "transcript": "first",
        }
    )
    boundary = await _next_event(responses)
    final = await _next_event(responses)
    assert boundary.boundary_quality == "unknown"
    assert (final.kind, final.utterance_id, final.text) == (
        "final",
        started.utterance_id,
        "first",
    )

    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "early-completed",
            "audio_end_ms": 100,
        }
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "early-completed"}
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)

    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "fresh-after-late",
            "audio_start_ms": 100,
        }
    )
    fresh_started = await _next_event(responses)
    assert (fresh_started.kind, fresh_started.utterance_id) == (
        "utterance_started",
        2,
    )
    await websocket.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "fresh-after-late",
            "audio_end_ms": 200,
        }
    )
    fresh_boundary = await _next_event(responses)
    assert fresh_boundary.boundary_quality == "exact"
    assert fresh_boundary.audio_range == ProviderAudioRange(1_600, 3_200)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "fresh-after-late",
            "transcript": "second",
        }
    )
    fresh_final = await _next_event(responses)
    assert (fresh_final.kind, fresh_final.utterance_id, fresh_final.text) == (
        "final",
        2,
        "second",
    )
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_qwen_clear_discards_provisional_commit_timeline(monkeypatch) -> None:
    first = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    second = _FakeWebSocket(on_send=_qwen_provider_lifecycle_on_send)
    task, requests, responses, connector = await _start_qwen_provider_worker(
        monkeypatch,
        first,
        second,
    )
    await first.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "same-id"}
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)

    await requests.put(
        _AsrWorkerRequest(
            kind="clear",
            generation=0,
            buffer_epoch=1,
            utterance_id=1,
        )
    )
    await _wait_until(lambda: len(connector.calls) == 2)
    await first.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "same-id",
            "audio_end_ms": 100,
        }
    )
    await first.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "same-id",
            "transcript": "stale",
        }
    )
    with pytest.raises(asyncio.TimeoutError):
        await _next_event(responses, timeout=0.03)

    await second.server_send(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "same-id",
            "audio_start_ms": 0,
        }
    )
    started = await _next_event(responses)
    assert (
        started.kind,
        started.buffer_epoch,
        started.utterance_id,
    ) == ("utterance_started", 1, 1)
    await second.server_send(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "same-id",
            "audio_end_ms": 100,
        }
    )
    boundary = await _next_event(responses)
    assert (
        boundary.kind,
        boundary.buffer_epoch,
        boundary.utterance_id,
        boundary.boundary_quality,
    ) == ("provider_endpoint", 1, 1, "exact")
    await second.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "same-id",
            "transcript": "fresh",
        }
    )
    final = await _next_event(responses)
    assert (
        final.kind,
        final.buffer_epoch,
        final.utterance_id,
        final.text,
    ) == ("final", 1, 1, "fresh")
    await _stop_worker(
        task,
        requests,
        responses,
        buffer_epoch=1,
        utterance_id=2,
    )


async def test_qwen_server_vad_speech_stopped_without_completed_expires_empty_final(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    monkeypatch.setattr(qwen, "_QWEN_STALLED_ITEM_TIMEOUT_SECONDS", 0.2)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "qwen-stalled"}
    )
    started = await _next_event(responses, "utterance_started")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "qwen-stalled"}
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "qwen-stalled"}
    )

    # Server VAD sealed the turn but the transcription completed event never
    # arrives: the stalled-item deadline must close the turn with an empty
    # final instead of leaving the upstream session waiting unboundedly.
    boundary = await _next_event(responses)
    assert (
        boundary.kind,
        boundary.utterance_id,
        boundary.boundary_quality,
    ) == ("provider_endpoint", started.utterance_id, "unknown")
    expired = await _next_event(responses)
    assert expired.kind == "final"
    assert expired.text == ""
    assert expired.utterance_id == started.utterance_id

    # A late completed event for the expired item must not resurrect it.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "qwen-stalled",
            "transcript": "late text",
        }
    )

    # The session keeps transcribing: a following turn whose completed event
    # arrives before the deadline is delivered unchanged.
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "qwen-next"}
    )
    next_started = await _next_event(responses, "utterance_started")
    assert next_started.utterance_id != started.utterance_id
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "qwen-next"}
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "qwen-next",
            "transcript": "next turn",
        }
    )
    final = await _next_event(responses, "final")
    assert final.text == "next turn"
    assert final.utterance_id == next_started.utterance_id

    # A disarmed deadline must not fire a duplicate empty final later.
    await asyncio.sleep(0.5)
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_qwen_server_vad_partial_text_refreshes_stalled_deadline(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send({"type": "session.updated"})
            elif message["type"] == "session.finish":
                await ws.server_send({"type": "session.finished"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(qwen.websockets, "connect", _FakeConnector(websocket))
    monkeypatch.setattr(qwen, "_QWEN_STALLED_ITEM_TIMEOUT_SECONDS", 0.5)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        qwen.qwen_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "qwen-live"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1

    # Continuous speech without an endpoint never arms the deadline: waiting
    # far past the bound must not expire the still-live turn mid-speech.
    await asyncio.sleep(0.75)
    assert responses.empty()

    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "qwen-live"}
    )
    # Each streaming text frame refreshes the armed deadline, so the total
    # elapsed time since the endpoint exceeds the bound without expiring.
    for text in ("你", "你好"):
        await asyncio.sleep(0.25)
        await websocket.server_send(
            {
                "type": "conversation.item.input_audio_transcription.text",
                "item_id": "qwen-live",
                "text": text,
            }
        )
        assert (await _next_event(responses, "partial")).text == text
    await asyncio.sleep(0.25)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "qwen-live",
            "transcript": "你好呀",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (1, "你好呀")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_step_manual_payload_and_cumulative_partial(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})
        elif message["type"] == "input_audio_buffer.commit":
            await ws.server_send(
                {"type": "input_audio_buffer.committed", "item_id": "step-1"}
            )
            await ws.server_send(
                {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "item_id": "step-1",
                    "text": "你好，请问",
                    "stash": "退款",
                }
            )
            await ws.server_send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "step-1",
                    "transcript": "你好，请问退款流程",
                }
            )

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "step-key",
            AsrSessionConfig(language="zh-CN"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x11\x22" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=pcm)
    )
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))
    partial = await _next_event(responses, "partial")
    final = await _next_event(responses, "final")
    assert partial.text == "你好，请问"
    assert final.text == "你好，请问退款流程"
    assert not task.done()

    url, kwargs = connector.calls[0]
    assert url == "wss://api.stepfun.com/v1/realtime/asr/stream"
    assert kwargs["additional_headers"] == {"Authorization": "Bearer step-key"}
    messages = [json.loads(payload) for payload in websocket.sent]
    session = messages[0]["session"]["audio"]["input"]
    assert session["format"] == {
        "type": "pcm",
        "codec": "pcm_s16le",
        "rate": 16_000,
        "bits": 16,
        "channel": 1,
    }
    assert session["transcription"]["model"] == "stepaudio-2.5-asr-stream"
    assert "turn_detection" not in session
    append = next(
        message
        for message in messages
        if message["type"] == "input_audio_buffer.append"
    )
    assert base64.b64decode(append["audio"]) == pcm
    await _stop_worker(task, requests, responses)


async def test_step_manual_uses_transcription_item_id_not_committed_item_id(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})
        elif message["type"] == "input_audio_buffer.append":
            await ws.server_send(
                {
                    "type": "conversation.item.created",
                    "item": {"id": "step-audio-item"},
                }
            )
            await ws.server_send(
                {
                    "type": "conversation.item.created",
                    "item": {"id": "step-transcription-item"},
                }
            )
            await ws.server_send(
                {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "item_id": "step-transcription-item",
                    "text": "hello",
                }
            )
        elif message["type"] == "input_audio_buffer.commit":
            await ws.server_send(
                {
                    "type": "input_audio_buffer.committed",
                    "item_id": "step-audio-item",
                }
            )
            await ws.server_send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "step-transcription-item",
                    "transcript": "hello step",
                }
            )

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "step-key",
            AsrSessionConfig(language="en", endpointing_mode="manual"),
        )
    )
    await _next_event(responses, "ready")
    try:
        await requests.put(
            _AsrWorkerRequest(
                kind="audio",
                generation=0,
                utterance_id=7,
                audio=b"\x11\x22" * 160,
            )
        )
        await requests.put(
            _AsrWorkerRequest(kind="commit", generation=0, utterance_id=7)
        )
        final = await _next_event(responses, "final")
        assert (final.utterance_id, final.text) == (7, "hello step")
    finally:
        await _stop_worker(task, requests, responses, utterance_id=8)


async def test_step_server_vad_correlates_distinct_item_ids_and_tombstones(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript-1",
            "transcript": "done",
        }
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    assert (await _next_event(responses, "final")).text == "done"

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript-1",
            "transcript": "duplicate",
        }
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "step-transcript-1",
            "text": "late",
        }
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert responses.empty()

    session_update = json.loads(websocket.sent[0])
    assert session_update["session"]["audio"]["input"]["turn_detection"] == {
        "type": "server_vad"
    }
    assert not any(
        isinstance(payload, str)
        and json.loads(payload).get("type") == "input_audio_buffer.commit"
        for payload in websocket.sent
    )
    await _stop_worker(task, requests, responses)


async def test_step_server_vad_keeps_tombstones_for_connection_lifetime(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")

    for index in range(1_025):
        await websocket.server_send(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": f"unknown-{index}",
                "transcript": "",
            }
        )
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "current-audio"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1

    # A very late duplicate must not consume the current FIFO fallback turn,
    # even after more than the former bounded tombstone limit.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "unknown-0",
            "transcript": "stale",
        }
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert responses.empty()

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "current-audio",
            "transcript": "current",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (1, "current")
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_step_server_vad_preserves_overlapping_provider_turns(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "unknown-before-start",
            "transcript": "must be ignored",
        }
    )
    for audio_item_id in ("step-audio-1", "step-audio-2"):
        await websocket.server_send(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": audio_item_id,
            }
        )
    first_started = await _next_event(responses, "utterance_started")
    second_started = await _next_event(responses, "utterance_started")
    assert (first_started.utterance_id, second_started.utterance_id) == (1, 2)

    for item_id, text in (
        ("step-audio-2", "second"),
        ("step-audio-1", "first"),
    ):
        await websocket.server_send(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item_id,
                "transcript": text,
            }
        )
    second_final = await _next_event(responses, "final")
    first_final = await _next_event(responses, "final")
    assert (second_final.utterance_id, second_final.text) == (2, "second")
    assert (first_final.utterance_id, first_final.text) == (1, "first")
    assert task.done() is False
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_step_manual_fails_closed_on_ambiguous_second_commit(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=2))

    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_STEP_PROTOCOL_ERROR"
    await asyncio.wait_for(task, 1)


async def test_step_server_vad_reconnect_isolates_item_bindings(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    first = _FakeWebSocket(on_send=on_send)
    second = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio"}
    )
    await first.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript",
            "transcript": "first",
        }
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    assert (await _next_event(responses, "final")).text == "first"

    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=0, buffer_epoch=1, utterance_id=2)
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=0,
            buffer_epoch=1,
            utterance_id=2,
            audio=b"\x01\x02",
        )
    )
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(
        lambda: any(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "input_audio_buffer.append"
            for payload in second.sent
        )
    )
    await second.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio"}
    )
    await second.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript",
            "transcript": "second",
        }
    )
    started = await _next_event(responses, "utterance_started")
    final = await _next_event(responses, "final")
    assert (started.buffer_epoch, started.utterance_id) == (1, 2)
    assert (final.buffer_epoch, final.utterance_id, final.text) == (1, 2, "second")
    await _stop_worker(
        task,
        requests,
        responses,
        buffer_epoch=1,
        utterance_id=2,
    )


async def test_step_manual_late_completed_does_not_consume_next_commit(
    monkeypatch,
) -> None:
    commit_count = 0

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        nonlocal commit_count
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})
        elif message["type"] == "input_audio_buffer.commit":
            commit_count += 1
            if commit_count == 1:
                await ws.server_send(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "step-transcript-old",
                        "transcript": "first",
                    }
                )
            else:
                await ws.server_send(
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "item_id": "step-transcript-old",
                        "text": "late",
                    }
                )
                await ws.server_send(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "step-transcript-old",
                        "transcript": "duplicate",
                    }
                )
                await ws.server_send(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "step-transcript-new",
                        "transcript": "second",
                    }
                )

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))
    first_final = await _next_event(responses, "final")
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=2))
    second_final = await _next_event(responses, "final")

    assert (first_final.utterance_id, first_final.text) == (1, "first")
    assert (second_final.utterance_id, second_final.text) == (2, "second")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_step_server_vad_expires_stalled_turn_with_empty_final(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    now = {"seconds": 0.0}
    first = _FakeWebSocket(on_send=on_send)
    second = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1

    # The endpoint event arms the stalled-turn deadline; the trailing timeout
    # sentinel guarantees speech_stopped is fully processed (armed at the
    # current fake clock) before the test advances the clock.
    await first.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "step-audio-1"}
    )
    await first.server_timeout()
    await _wait_until(lambda: first.incoming.empty())

    # The next inbound frame triggers the sweep. The expiry is ambiguous (no
    # exact audio-id binding proved id reuse on this connection), so the
    # worker completes the turn empty, drops the frame in hand, and retires
    # the poisoned FIFO namespace by reconnecting.
    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-2"}
    )
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (1, "")
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(lambda: first.closed)
    assert responses.empty(), "reset must not emit a duplicate ready event"

    await second.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-2"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 2
    await second.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript-2",
            "transcript": "second",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "second")
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_step_manual_expires_stalled_commit_with_empty_final(
    monkeypatch,
) -> None:
    commit_count = 0

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        nonlocal commit_count
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})
        elif message["type"] == "input_audio_buffer.commit":
            commit_count += 1
            if commit_count == 2:
                await ws.server_send(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "step-transcript-next",
                        "transcript": "second",
                    }
                )

    now = {"seconds": 0.0}
    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))
    await _wait_until(lambda: commit_count == 1)

    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=2))
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (1, "")
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "second")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_step_server_vad_expires_stalled_turn_without_inbound_frames(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    now = {"seconds": 0.0}
    first = _FakeWebSocket(on_send=on_send)
    second = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1

    # committed is the alternate endpoint event and must arm the deadline
    # too. The trailing timeout sentinel guarantees it is fully processed at
    # the current fake clock before the test advances the clock.
    await first.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "step-audio-1"}
    )
    await first.server_timeout()
    await _wait_until(lambda: first.incoming.empty())

    # No further inbound frames arrive after the endpoint. The bounded
    # receive wait times out at the pending-turn deadline and the sweep must
    # emit the empty final on its own, then recycle the ambiguous connection.
    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await first.server_timeout()
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (1, "")
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(lambda: first.closed)

    # Item ids are connection-scoped: even the expired turn's own audio id is
    # a fresh identity on the new connection and binds a new turn cleanly.
    await second.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 2
    await second.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript-2",
            "transcript": "second",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "second")
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_step_receive_wait_wakes_at_pending_turn_deadline(
    monkeypatch,
) -> None:
    # Uses the real clock with a tiny timeout so the receiver provably wakes
    # itself at the deadline instead of relying on a simulated timeout.
    monkeypatch.setattr(step, "_STEP_PENDING_TURN_TIMEOUT_SECONDS", 0.05)

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    # The ambiguous expiry recycles the connection, so the shutdown at the
    # end of the test is served by a second fake websocket.
    first = _FakeWebSocket(on_send=on_send)
    second = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(first, second))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    await first.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "step-audio-1"}
    )

    # After the endpoint: no commits, no provider frames, nothing. The empty
    # final must still arrive once the deadline elapses.
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (1, "")
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_step_server_vad_continuous_speech_never_expires_before_endpoint(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    now = {"seconds": 0.0}
    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1

    # The user keeps speaking past the stalled-turn bound with no endpoint
    # event. The sweep runs (forced by the timeout sentinel) but must not
    # expire the still-live turn mid-speech.
    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await websocket.server_timeout()
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "step-audio-1",
            "text": "still talking",
        }
    )
    event = await _next_event(responses)
    assert (event.kind, event.utterance_id, event.text) == (
        "partial",
        1,
        "still talking",
    )

    # Even far past the original bound, the eventual real transcript must be
    # delivered instead of a mid-speech empty final.
    now["seconds"] = 3 * step._STEP_PENDING_TURN_TIMEOUT_SECONDS
    await websocket.server_timeout()
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-audio-1",
            "transcript": "still talking done",
        }
    )
    event = await _next_event(responses)
    assert (event.kind, event.utterance_id, event.text) == (
        "final",
        1,
        "still talking done",
    )
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_step_server_vad_deltas_refresh_bound_turn_deadline(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    now = {"seconds": 0.0}
    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "step-audio-1"}
    )
    await websocket.server_timeout()
    await _wait_until(lambda: websocket.incoming.empty())

    # A transcription delta binds the turn; the deadline follows the bound
    # item instead of being disarmed, and each delta pushes it forward.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "step-audio-1",
            "text": "midway",
        }
    )
    assert (await _next_event(responses, "partial")).text == "midway"

    # Just inside the deadline the sweep must not expire the streaming turn.
    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS - 1.0
    await websocket.server_timeout()
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "step-audio-1",
            "text": "midway more",
        }
    )
    assert (await _next_event(responses, "partial")).text == "midway more"

    # Past the ORIGINAL deadline but inside the refreshed one: still alive,
    # and the real transcript is delivered instead of a mid-stream empty
    # final.
    now["seconds"] = 2.0 * (step._STEP_PENDING_TURN_TIMEOUT_SECONDS - 1.0)
    await websocket.server_timeout()
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-audio-1",
            "transcript": "midway done",
        }
    )
    event = await _next_event(responses)
    assert (event.kind, event.utterance_id, event.text) == (
        "final",
        1,
        "midway done",
    )
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_step_server_vad_bound_turn_stalled_after_delta_expires_empty(
    monkeypatch,
) -> None:
    # Regression: a delta used to remove the turn from the pending queue and
    # disarm its deadline entirely, so "one delta then silence" kept the
    # upstream utterance ACTIVE forever.
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    now = {"seconds": 0.0}
    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "step-audio-1"}
    )
    await websocket.server_timeout()
    await _wait_until(lambda: websocket.incoming.empty())

    # A distinct-id delta binds the turn through the FIFO fallback, then the
    # stream goes silent with no terminal event.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "step-transcript-1",
            "text": "half",
        }
    )
    assert (await _next_event(responses, "partial")).text == "half"

    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await websocket.server_timeout()
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (1, "")

    # Both item ids of the bound turn are known and tombstoned, so the late
    # terminal event is dropped fail-closed and no connection reset is
    # needed (the single-websocket connector would fail on a reconnect).
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript-1",
            "transcript": "too late",
        }
    )
    await websocket.server_timeout()
    await _wait_until(lambda: websocket.incoming.empty())
    assert responses.empty()

    # The same connection keeps serving later turns.
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-2"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 2
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript-2",
            "transcript": "second",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "second")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_step_manual_bound_commit_stalled_after_delta_expires_empty(
    monkeypatch,
) -> None:
    # Manual-mode variant of the stalled bound turn: the delta pops the
    # pending commit at binding time, so only the bound-item deadline can
    # expire a commit whose terminal event never arrives.
    commit_count = 0

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        nonlocal commit_count
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})
        elif message["type"] == "input_audio_buffer.commit":
            commit_count += 1
            if commit_count == 1:
                await ws.server_send(
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "item_id": "step-transcript-1",
                        "text": "half",
                    }
                )
            else:
                await ws.server_send(
                    {
                        "type": (
                            "conversation.item.input_audio_transcription.completed"
                        ),
                        "item_id": "step-transcript-2",
                        "transcript": "second",
                    }
                )

    now = {"seconds": 0.0}
    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))
    partial = await _next_event(responses, "partial")
    assert (partial.utterance_id, partial.text) == (1, "half")

    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await websocket.server_timeout()
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (1, "")

    # The late terminal event for the expired bound item stays tombstoned.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript-1",
            "transcript": "too late",
        }
    )
    await websocket.server_timeout()
    await _wait_until(lambda: websocket.incoming.empty())
    assert responses.empty()

    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=2))
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "second")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_step_manual_expired_commit_quarantines_late_items(
    monkeypatch,
) -> None:
    commit_count = 0

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        nonlocal commit_count
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})
        elif message["type"] == "input_audio_buffer.commit":
            commit_count += 1
            if commit_count == 2:
                # The expired commit's completed arrives only after the next
                # commit was sent, then the next commit's own transcription.
                await ws.server_send(
                    {
                        "type": (
                            "conversation.item.input_audio_transcription.completed"
                        ),
                        "item_id": "step-transcript-old",
                        "transcript": "stale speech",
                    }
                )
                await ws.server_send(
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "item_id": "step-transcript-new",
                        "text": "second",
                    }
                )
                await ws.server_send(
                    {
                        "type": (
                            "conversation.item.input_audio_transcription.completed"
                        ),
                        "item_id": "step-transcript-new",
                        "transcript": "second answer",
                    }
                )

    now = {"seconds": 0.0}
    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))
    await _wait_until(lambda: commit_count == 1)

    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await websocket.server_timeout()
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (1, "")

    # A late delta for the expired commit arrives while no commit is pending.
    # It must be tombstoned, not parked as bindable for the next commit. The
    # timeout sentinel guarantees it is fully processed before the commit.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "step-transcript-old",
            "text": "stale",
        }
    )
    await websocket.server_timeout()
    await _wait_until(lambda: websocket.incoming.empty())
    assert responses.empty()

    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=2))
    event = await _next_event(responses)
    assert (event.kind, event.utterance_id, event.text) == ("partial", 2, "second")
    event = await _next_event(responses)
    assert (event.kind, event.utterance_id, event.text) == (
        "final",
        2,
        "second answer",
    )
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_step_server_vad_ambiguous_expiry_flushes_live_turns_and_resets(
    monkeypatch,
) -> None:
    # Regression for the quarantine flaw: turn 2 starts before turn 1's
    # expiry and turn 1's late distinct-id transcription could arrive after
    # turn 2 is live. The protocol exposes no correlation field, so ANY
    # same-connection quarantine either eats turn 2's transcription or lets
    # turn 1's bind to turn 2. The worker must instead close every
    # outstanding turn empty and retire the connection-scoped id namespace
    # by reconnecting, so the late event can never bind at all.
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    now = {"seconds": 0.0}
    first = _FakeWebSocket(on_send=on_send)
    second = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(step.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    await first.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "step-audio-1"}
    )
    # Turn 2 enters the pending queue BEFORE the expiry, so it is exactly
    # the live turn a late distinct-id event could wrongly consume.
    await first.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-2"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 2

    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await first.server_timeout()
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (1, "")
    # The still-live turn 2 is flushed empty (bounded loss) rather than left
    # to receive turn 1's late text or expire against a dead socket.
    flushed_final = await _next_event(responses, "final")
    assert (flushed_final.utterance_id, flushed_final.text) == (2, "")
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(lambda: first.closed)

    # Turn 1's late distinct-id transcription targets the retired socket and
    # can never reach a live turn.
    await first.server_send(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "step-late-1",
            "text": "stale",
        }
    )
    await first.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-late-1",
            "transcript": "stale done",
        }
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert responses.empty()

    # The fresh connection serves the next turn cleanly, including the
    # distinct-id FIFO fallback.
    await second.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-3"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 3
    await second.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-transcript-3",
            "transcript": "third",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (3, "third")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=4)


async def test_step_server_vad_proven_id_reuse_expiry_keeps_connection(
    monkeypatch,
) -> None:
    # Once an exact audio-id binding proved that this deployment reuses
    # audio item ids for transcription events, a stalled-turn expiry is not
    # ambiguous: the late transcription must carry the already-tombstoned
    # audio id, so the connection (and its live turns) is kept instead of
    # being reset. The single-websocket connector fails on any reconnect.
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    now = {"seconds": 0.0}
    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
            clock=lambda: now["seconds"],
        )
    )
    await _next_event(responses, "ready")

    # Turn 1 completes under its own audio id: reuse is proven.
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-audio-1",
            "transcript": "first",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (1, "first")

    # Turn 2 stalls after its endpoint and expires with an empty final.
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-2"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 2
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "step-audio-2"}
    )
    await websocket.server_timeout()
    await _wait_until(lambda: websocket.incoming.empty())
    now["seconds"] = step._STEP_PENDING_TURN_TIMEOUT_SECONDS + 1.0
    await websocket.server_timeout()
    stalled_final = await _next_event(responses, "final")
    assert (stalled_final.utterance_id, stalled_final.text) == (2, "")

    # The late transcription arrives under the tombstoned audio id and is
    # dropped fail-closed on the SAME connection.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-audio-2",
            "transcript": "late second",
        }
    )
    await _wait_until(lambda: websocket.incoming.empty())
    assert responses.empty()

    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-3"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 3
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-audio-3",
            "transcript": "third",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (3, "third")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=4)


async def test_step_receiver_skips_valid_non_dict_json_events(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(step.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        step.step_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")

    # Valid JSON that is not an object must be skipped, not kill the session.
    await websocket.server_send([])
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "step-audio-1"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "step-audio-1",
            "transcript": "still alive",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (1, "still alive")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_openai_transcription_resampling_and_out_of_order_finals(
    monkeypatch,
) -> None:
    diagnostics: list[dict[str, Any]] = []
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {
                    "type": "session.updated",
                    "session": message["session"],
                }
            )

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(openai.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "openai-key",
            AsrSessionConfig(language="en-US", endpointing_mode="provider"),
            diagnostic_sink=diagnostics.append,
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x01\x00" * 1_600
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=7,
            buffer_epoch=3,
            audio=pcm,
        )
    )
    await _wait_until(
        lambda: any(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "input_audio_buffer.append"
            for payload in websocket.sent
        )
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "openai-1"}
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "openai-1"}
    )
    # OpenAI may acknowledge a committed item before speech_started is observed.
    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "openai-2"}
    )
    started_one = await _next_event(responses, "utterance_started")
    started_two = await _next_event(responses, "utterance_started")
    assert (
        started_one.generation,
        started_one.buffer_epoch,
        started_one.utterance_id,
    ) == (7, 3, 1)
    assert started_two.utterance_id == 2
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "openai-2",
            "transcript": "second",
        }
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "openai-1",
            "transcript": "first",
        }
    )
    second = await _next_event(responses, "final")
    first = await _next_event(responses, "final")
    assert (second.utterance_id, second.text) == (2, "second")
    assert (first.utterance_id, first.text) == (1, "first")
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "openai-2",
            "transcript": "duplicate",
        }
    )
    await asyncio.sleep(0)
    assert responses.empty()

    url, kwargs = connector.calls[0]
    assert url == "wss://api.openai.com/v1/realtime?intent=transcription"
    assert kwargs["additional_headers"] == {"Authorization": "Bearer openai-key"}
    messages = [json.loads(payload) for payload in websocket.sent]
    session = messages[0]["session"]
    assert session["type"] == "transcription"
    audio_input = session["audio"]["input"]
    assert audio_input["format"] == {"type": "audio/pcm", "rate": 24_000}
    assert (
        audio_input["transcription"]["model"]
        == "gpt-4o-mini-transcribe-2025-12-15"
    )
    assert audio_input["transcription"]["language"] == "en"
    assert audio_input["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 1_000,
    }
    sent_types = {message["type"] for message in messages}
    assert "response.create" not in sent_types
    assert "input_audio_buffer.commit" not in sent_types
    assert diagnostics[0]["type"] == "websocket.connected"
    requested = next(event for event in diagnostics if event["type"] == "session.update")
    accepted = next(event for event in diagnostics if event["type"] == "session.updated")
    assert requested["requested_model"] == "gpt-4o-mini-transcribe-2025-12-15"
    assert requested["requested_turn_detection"]["silence_duration_ms"] == 1_000
    assert accepted["accepted_model"] == requested["requested_model"]
    assert accepted["accepted_turn_detection"]["type"] == "server_vad"
    wire_audio = b"".join(
        base64.b64decode(message["audio"])
        for message in messages
        if message["type"] == "input_audio_buffer.append"
    )
    assert len(wire_audio) > len(pcm) * 1.3
    assert len(wire_audio) < len(pcm) * 1.7
    await _stop_worker(
        task,
        requests,
        responses,
        generation=7,
        buffer_epoch=3,
        utterance_id=3,
    )


async def test_openai_partial_transcripts_accumulate_deltas(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {
                    "type": "session.updated",
                    "session": message["session"],
                }
            )

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(openai.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "openai-key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "openai-partial"}
    )
    await _next_event(responses, "utterance_started")
    for delta in ("hello ", "world"):
        await websocket.server_send(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "openai-partial",
                "delta": delta,
            }
        )

    first = await _next_event(responses, "partial")
    second = await _next_event(responses, "partial")
    assert first.text == "hello "
    assert second.text == "hello world"

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "openai-partial",
            "transcript": "hello world",
        }
    )
    assert (await _next_event(responses, "final")).text == "hello world"
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_openai_completed_without_transcript_emits_empty_final(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {
                    "type": "session.updated",
                    "session": message["session"],
                }
            )

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(openai.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "openai-key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "openai-no-text"}
    )
    await _next_event(responses, "utterance_started")

    # A completed event without a usable transcript still terminates the
    # provider utterance; the final must not be silently dropped.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "openai-no-text",
            "transcript": 123,
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (1, "")
    assert responses.empty()
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_openai_speech_stopped_without_completed_expires_empty_final(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {"type": "session.updated", "session": message["session"]}
            )

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(openai.websockets, "connect", _FakeConnector(websocket))
    monkeypatch.setattr(openai, "_OPENAI_STALLED_ITEM_TIMEOUT_SECONDS", 0.2)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "openai-key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "openai-stalled"}
    )
    started = await _next_event(responses, "utterance_started")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "openai-stalled"}
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.committed", "item_id": "openai-stalled"}
    )

    # Server VAD sealed the turn but the transcription completed event never
    # arrives: the stalled-item deadline must close the turn with an empty
    # final instead of leaving the upstream session waiting unboundedly.
    expired = await _next_event(responses, "final")
    assert expired.text == ""
    assert expired.utterance_id == started.utterance_id

    # A late completed event for the expired item must not resurrect it.
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "openai-stalled",
            "transcript": "late text",
        }
    )

    # The session keeps transcribing: a following turn whose completed event
    # arrives before the deadline is delivered unchanged.
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "openai-next"}
    )
    next_started = await _next_event(responses, "utterance_started")
    assert next_started.utterance_id != started.utterance_id
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_stopped", "item_id": "openai-next"}
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "openai-next",
            "transcript": "next turn",
        }
    )
    final = await _next_event(responses, "final")
    assert final.text == "next turn"
    assert final.utterance_id == next_started.utterance_id

    # A disarmed deadline must not fire a duplicate empty final later.
    await asyncio.sleep(0.5)
    assert responses.empty()
    await _stop_worker(task, requests, responses)


async def test_openai_transcription_failure_terminates_item_and_worker(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {
                    "type": "session.updated",
                    "session": message["session"],
                }
            )

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(openai.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "openai-key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )

    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "failed-item"}
    )
    await _next_event(responses, "utterance_started")
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.failed",
            "item_id": "failed-item",
        }
    )

    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_OPENAI_TRANSCRIPTION_FAILED"
    assert (error.generation, error.buffer_epoch, error.utterance_id) == (0, 0, 1)
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    assert responses.empty()


async def test_openai_native_clear_and_mode_rejection(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send(
                    {"type": "session.updated", "session": message["session"]}
                )

    websocket = _FakeWebSocket(on_send=on_send)
    commit_websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket, commit_websocket)
    monkeypatch.setattr(openai.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "old"}
    )
    await _next_event(responses, "utterance_started")
    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=0, buffer_epoch=1, utterance_id=2)
    )
    await _wait_until(
        lambda: any(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "input_audio_buffer.clear"
            for payload in websocket.sent
        )
    )
    assert len(connector.calls) == 1
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "old",
            "transcript": "late old final",
        }
    )
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "late-old"}
    )
    late_started = await _next_event(responses, "utterance_started")
    assert late_started.buffer_epoch == 0
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "late-old",
            "transcript": "late old item",
        }
    )
    late_final = await _next_event(responses, "final")
    assert (late_final.buffer_epoch, late_final.text) == (0, "late old item")
    await websocket.server_send({"type": "input_audio_buffer.cleared"})
    await asyncio.wait_for(requests.join(), 1)
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "new"}
    )
    started = await _next_event(responses, "utterance_started")
    assert (started.buffer_epoch, started.utterance_id) == (1, 2)
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "new",
            "transcript": "new final",
        }
    )
    final = await _next_event(responses, "final")
    assert (final.buffer_epoch, final.utterance_id, final.text) == (1, 2, "new final")
    await _stop_worker(
        task,
        requests,
        responses,
        buffer_epoch=1,
        utterance_id=2,
    )

    commit_requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    commit_responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    commit_task = asyncio.create_task(
        openai.openai_asr_worker(
            commit_requests,
            commit_responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(commit_responses, "ready")
    await commit_requests.put(
        _AsrWorkerRequest(kind="commit", generation=1, buffer_epoch=2, utterance_id=3)
    )
    error = await _next_event(commit_responses, "error")
    assert error.error_code == "ASR_OPENAI_PROTOCOL_ERROR"
    await _next_event(commit_responses, "closed")
    await asyncio.wait_for(commit_task, 1)
    assert all(
        not isinstance(payload, str)
        or json.loads(payload).get("type") != "input_audio_buffer.commit"
        for payload in commit_websocket.sent
    )

    rejected_requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    rejected_responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    rejected = asyncio.create_task(
        openai.openai_asr_worker(
            rejected_requests,
            rejected_responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
        )
    )
    error = await _next_event(rejected_responses, "error")
    assert error.error_code == "ASR_ENDPOINTING_NOT_SUPPORTED"
    await _next_event(rejected_responses, "closed")
    await asyncio.wait_for(rejected, 1)
    assert len(connector.calls) == 2


async def test_openai_clear_barrier_drops_late_old_item_from_session(
    monkeypatch,
) -> None:
    clear_send_started = asyncio.Event()
    release_clear_send = asyncio.Event()
    late_final_seen = asyncio.Event()

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {"type": "session.updated", "session": message["session"]}
            )
        elif message["type"] == "input_audio_buffer.clear":
            clear_send_started.set()
            await release_clear_send.wait()

    def diagnostics(event: dict[str, Any]) -> None:
        if (
            event.get("type")
            == "conversation.item.input_audio_transcription.completed"
            and event.get("item_id") == "late-old"
        ):
            late_final_seen.set()

    async def worker(
        request_queue: asyncio.Queue[_AsrWorkerRequest],
        response_queue: asyncio.Queue[_AsrWorkerEvent],
        api_key: str,
        config: AsrSessionConfig,
    ) -> None:
        await openai.openai_asr_worker(
            request_queue,
            response_queue,
            api_key,
            config,
            diagnostic_sink=diagnostics,
        )

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(openai.websockets, "connect", _FakeConnector(websocket))
    transcripts: list[str] = []
    endpoints: list[int] = []
    errors: list[str] = []

    async def on_transcript(text: str) -> None:
        transcripts.append(text)

    async def on_error(error: str) -> None:
        errors.append(error)

    async def on_endpoint() -> None:
        endpoints.append(len(endpoints) + 1)

    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="key",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=on_transcript,
        on_connection_error=on_error,
        on_turn_endpointed=on_endpoint,
    )
    await session.connect()
    await session.stream_audio(b"\x01\x00" * 1_600)
    await _wait_until(
        lambda: any(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "input_audio_buffer.append"
            for payload in websocket.sent
        )
    )
    await session.clear_audio_buffer()
    await asyncio.wait_for(clear_send_started.wait(), 1)

    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "late-old"}
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "late-old",
            "transcript": "STALE",
        }
    )
    await asyncio.wait_for(late_final_seen.wait(), 1)

    release_clear_send.set()
    await websocket.server_send({"type": "input_audio_buffer.cleared"})
    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)

    await session.stream_audio(b"\x02\x00" * 1_600)
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "new"}
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "new",
            "transcript": "NEW",
        }
    )
    await _wait_until(lambda: transcripts == ["NEW"])

    assert "STALE" not in transcripts
    assert endpoints == [1]
    assert errors == []
    await session.close()


async def test_openai_clear_timeout_fails_closed_without_stranding_queue(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {"type": "session.updated", "session": message["session"]}
            )

    monkeypatch.setattr(openai, "_CLEAR_TIMEOUT_SECONDS", 0.01)
    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(openai.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=0, buffer_epoch=1, utterance_id=2)
    )

    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_OPENAI_CLEAR_TIMEOUT"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    await asyncio.wait_for(requests.join(), 1)
    assert responses.empty()


async def test_openai_consecutive_clears_require_distinct_acknowledgements(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {"type": "session.updated", "session": message["session"]}
            )

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(openai.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")

    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=0, buffer_epoch=1, utterance_id=2)
    )
    await _wait_until(
        lambda: sum(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "input_audio_buffer.clear"
            for payload in websocket.sent
        )
        == 1
    )
    await websocket.server_send({"type": "input_audio_buffer.cleared"})
    await asyncio.wait_for(requests.join(), 1)

    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=0, buffer_epoch=2, utterance_id=3)
    )
    await _wait_until(
        lambda: sum(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "input_audio_buffer.clear"
            for payload in websocket.sent
        )
        == 2
    )
    join_task = asyncio.create_task(requests.join())
    await asyncio.sleep(0)
    assert join_task.done() is False
    await websocket.server_send({"type": "input_audio_buffer.cleared"})
    await asyncio.wait_for(join_task, 1)

    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "after-two-clears"}
    )
    started = await _next_event(responses, "utterance_started")
    assert (started.buffer_epoch, started.utterance_id) == (2, 3)
    await _stop_worker(
        task,
        requests,
        responses,
        buffer_epoch=2,
        utterance_id=3,
    )


async def test_openai_clear_ignores_old_failed_item_and_keys_current_failure(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {"type": "session.updated", "session": message["session"]}
            )

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(openai.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "old"}
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1

    await requests.put(
        _AsrWorkerRequest(
            kind="clear",
            generation=4,
            buffer_epoch=2,
            utterance_id=7,
        )
    )
    await _wait_until(
        lambda: any(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "input_audio_buffer.clear"
            for payload in websocket.sent
        )
    )
    await websocket.server_send({"type": "input_audio_buffer.cleared"})
    await asyncio.wait_for(requests.join(), 1)

    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.failed",
            "item_id": "old",
        }
    )
    await asyncio.sleep(0)
    assert responses.empty()
    assert task.done() is False

    await websocket.server_send(
        {"type": "input_audio_buffer.speech_started", "item_id": "current"}
    )
    started = await _next_event(responses, "utterance_started")
    assert (started.generation, started.buffer_epoch, started.utterance_id) == (
        4,
        2,
        7,
    )
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.failed",
            "item_id": "current",
        }
    )
    error = await _next_event(responses, "error")
    assert (
        error.error_code,
        error.generation,
        error.buffer_epoch,
        error.utterance_id,
    ) == ("ASR_OPENAI_TRANSCRIPTION_FAILED", 4, 2, 7)
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    assert responses.empty()


@pytest.mark.parametrize("item_id", [None, 3])
async def test_openai_failed_event_without_valid_item_id_fails_closed(
    monkeypatch,
    item_id: object,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] == "session.update":
            await ws.server_send(
                {"type": "session.updated", "session": message["session"]}
            )

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(openai.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_send(
        {
            "type": "conversation.item.input_audio_transcription.failed",
            "item_id": item_id,
        }
    )

    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_OPENAI_PROTOCOL_ERROR"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize(
    ("accepted_model", "accepted_vad"),
    [
        ("gpt-4o-transcribe", "server_vad"),
        ("gpt-4o-mini-transcribe-2025-12-15", "semantic_vad"),
    ],
)
async def test_openai_rejects_unaccepted_session_capabilities(
    monkeypatch,
    accepted_model: str,
    accepted_vad: str,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        assert isinstance(payload, str)
        message = json.loads(payload)
        if message["type"] != "session.update":
            return
        accepted = message["session"]
        accepted["audio"]["input"]["transcription"]["model"] = accepted_model
        accepted["audio"]["input"]["turn_detection"]["type"] = accepted_vad
        await ws.server_send({"type": "session.updated", "session": accepted})

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(openai.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )

    error = await _next_event(responses)
    assert error.kind == "error"
    assert error.error_code == "ASR_OPENAI_PROTOCOL_ERROR"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)


async def test_grok_manual_binary_finalize_and_shutdown(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if not isinstance(payload, str):
            return
        message = json.loads(payload)
        if message["type"] == "finalize":
            await ws.server_send(
                {
                    "type": "transcript.partial",
                    "text": "manual final",
                    "is_final": True,
                    "speech_final": True,
                }
            )
        elif message["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    websocket = _FakeWebSocket(
        initial=[{"type": "transcript.created"}],
        on_send=on_send,
    )
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(
            requests,
            responses,
            "grok-key",
            AsrSessionConfig(language="zh-CN"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x12\x34" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=pcm)
    )
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (1, "manual final")
    assert not task.done()

    url, kwargs = connector.calls[0]
    query = parse_qs(urlparse(url).query)
    assert urlparse(url).path == "/v1/stt"
    assert query == {
        "sample_rate": ["16000"],
        "encoding": ["pcm"],
        "interim_results": ["true"],
        "language": ["zh"],
    }
    assert "smart_turn" not in query
    assert kwargs["additional_headers"] == {"Authorization": "Bearer grok-key"}
    assert pcm in websocket.sent
    assert {
        json.loads(payload)["type"]
        for payload in websocket.sent
        if isinstance(payload, str)
    } == {"finalize"}
    await _stop_worker(task, requests, responses)
    assert any(
        isinstance(payload, str) and json.loads(payload)["type"] == "audio.done"
        for payload in websocket.sent
    )


async def test_grok_manual_preserves_natural_segments_before_commit(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if not isinstance(payload, str):
            return
        message = json.loads(payload)
        if message["type"] == "finalize":
            await ws.server_send(
                {
                    "type": "transcript.partial",
                    "text": "后半段",
                    "is_final": True,
                    "speech_final": True,
                }
            )
        elif message["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    websocket = _FakeWebSocket(
        initial=[{"type": "transcript.created"}], on_send=on_send
    )
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(requests, responses, "key", AsrSessionConfig())
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await _wait_until(lambda: b"\0\0" in websocket.sent)
    await websocket.server_send(
        {
            "type": "transcript.partial",
            "text": "前半段",
            "is_final": True,
            "speech_final": True,
        }
    )
    assert (await _next_event(responses, "partial")).text == "前半段"

    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\1\1")
    )
    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))
    final = await _next_event(responses, "final")
    assert final.text == "前半段后半段"
    assert any(
        isinstance(payload, str) and json.loads(payload)["type"] == "finalize"
        for payload in websocket.sent
    )
    await _stop_worker(task, requests, responses)


async def test_grok_server_vad_three_states_and_clear_reconnect(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    first = _FakeWebSocket(initial=[{"type": "transcript.created"}], on_send=on_send)
    second = _FakeWebSocket(initial=[{"type": "transcript.created"}], on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    query = parse_qs(urlparse(connector.calls[0][0]).query)
    assert query["endpointing"] == ["10"]
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await _wait_until(lambda: any(isinstance(item, bytes) for item in first.sent))
    for text, is_final, speech_final in (
        ("mutable", False, False),
        ("locked", True, False),
        ("utterance", True, True),
    ):
        await first.server_send(
            {
                "type": "transcript.partial",
                "text": text,
                "is_final": is_final,
                "speech_final": speech_final,
            }
        )
    started = await _next_event(responses, "utterance_started")
    mutable = await _next_event(responses, "partial")
    locked = await _next_event(responses, "partial")
    final = await _next_event(responses, "final")
    assert started.utterance_id == 1
    # The locked segment (is_final without speech_final) is retained and the
    # terminal event only carries the trailing segment, so the Core final is
    # the concatenation of both.
    assert (mutable.text, locked.text, final.text) == (
        "mutable",
        "locked",
        "lockedutterance",
    )

    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=0, buffer_epoch=1, utterance_id=2)
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=0,
            buffer_epoch=1,
            utterance_id=2,
            audio=b"\x56\x78",
        )
    )
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(lambda: b"\x56\x78" in second.sent)
    await _stop_worker(
        task,
        requests,
        responses,
        buffer_epoch=1,
        utterance_id=2,
    )


async def test_grok_server_vad_concatenates_locked_segments_into_final(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    websocket = _FakeWebSocket(
        initial=[{"type": "transcript.created"}], on_send=on_send
    )
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await _wait_until(lambda: any(isinstance(item, bytes) for item in websocket.sent))
    for text, is_final, speech_final in (
        ("draft", False, False),
        ("seg1", True, False),
        ("tail", False, False),
        ("seg2", True, False),
        ("seg3", True, True),
    ):
        await websocket.server_send(
            {
                "type": "transcript.partial",
                "text": text,
                "is_final": is_final,
                "speech_final": speech_final,
            }
        )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    # Locked segments accumulate and mutable tails render cumulatively, so
    # the preview always matches what the final will say.
    for expected in ("draft", "seg1", "seg1tail", "seg1seg2"):
        assert (await _next_event(responses, "partial")).text == expected
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (1, "seg1seg2seg3")

    # A following utterance on the same connection starts from an empty
    # segment buffer: no text bleeds over, and a single-segment utterance's
    # final carries only the terminal event's text (the mutable draft is
    # never concatenated in).
    for text, is_final, speech_final in (
        ("next", False, False),
        ("done", True, True),
    ):
        await websocket.server_send(
            {
                "type": "transcript.partial",
                "text": text,
                "is_final": is_final,
                "speech_final": speech_final,
            }
        )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 2
    assert (await _next_event(responses, "partial")).text == "next"
    next_final = await _next_event(responses, "final")
    assert (next_final.utterance_id, next_final.text) == (2, "done")
    assert responses.empty()
    await _stop_worker(task, requests, responses)


async def test_grok_server_vad_clear_resets_locked_segments(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    first = _FakeWebSocket(initial=[{"type": "transcript.created"}], on_send=on_send)
    second = _FakeWebSocket(initial=[{"type": "transcript.created"}], on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await _wait_until(lambda: any(isinstance(item, bytes) for item in first.sent))
    await first.server_send(
        {
            "type": "transcript.partial",
            "text": "stale",
            "is_final": True,
            "speech_final": False,
        }
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    assert (await _next_event(responses, "partial")).text == "stale"

    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=0, buffer_epoch=1, utterance_id=1)
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=0,
            buffer_epoch=1,
            utterance_id=1,
            audio=b"\1\1",
        )
    )
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(lambda: b"\1\1" in second.sent)
    await second.server_send(
        {
            "type": "transcript.partial",
            "text": "fresh",
            "is_final": True,
            "speech_final": True,
        }
    )
    # The reconnect dropped the locked segment of the cleared utterance: the
    # new epoch's final must not carry any pre-clear text.
    started = await _next_event(responses, "utterance_started")
    assert started.buffer_epoch == 1
    final = await _next_event(responses, "final")
    assert (final.buffer_epoch, final.text) == (1, "fresh")
    assert responses.empty()
    await _stop_worker(
        task,
        requests,
        responses,
        buffer_epoch=1,
    )


async def test_grok_server_vad_stalled_turn_expires_with_locked_segments(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    first = _FakeWebSocket(initial=[{"type": "transcript.created"}], on_send=on_send)
    second = _FakeWebSocket(initial=[{"type": "transcript.created"}], on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    monkeypatch.setattr(grok, "_GROK_STALLED_TURN_TIMEOUT_SECONDS", 0.2)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await _wait_until(lambda: any(isinstance(item, bytes) for item in first.sent))
    await first.server_send(
        {
            "type": "transcript.partial",
            "text": "locked half",
            "is_final": True,
            "speech_final": False,
        }
    )
    started = await _next_event(responses, "utterance_started")
    assert started.utterance_id == 1
    assert (await _next_event(responses, "partial")).text == "locked half"

    # The provider goes silent and the speech_final never arrives: the
    # stalled-turn deadline completes the turn with the provider-committed
    # locked text instead of dropping real speech.
    expired = await _next_event(responses, "final")
    assert (expired.utterance_id, expired.text) == (1, "locked half")

    # Expiry retires the connection, so a late speech_final for the expired
    # utterance dies with the old socket instead of becoming a phantom turn.
    await _wait_until(lambda: len(connector.calls) == 2)
    await first.server_send(
        {
            "type": "transcript.partial",
            "text": "late tail",
            "is_final": True,
            "speech_final": True,
        }
    )
    await asyncio.sleep(0.05)
    assert responses.empty()

    # The session keeps transcribing on the fresh connection, and the fresh
    # utterance never reuses the expired utterance's id.
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\1\1")
    )
    await _wait_until(lambda: b"\1\1" in second.sent)
    await second.server_send(
        {
            "type": "transcript.partial",
            "text": "after",
            "is_final": True,
            "speech_final": True,
        }
    )
    next_started = await _next_event(responses, "utterance_started")
    assert next_started.utterance_id == 2
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "after")
    assert responses.empty()
    await _stop_worker(task, requests, responses)


async def test_grok_server_vad_stalled_turn_without_locked_segments_expires_empty(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    first = _FakeWebSocket(initial=[{"type": "transcript.created"}], on_send=on_send)
    second = _FakeWebSocket(initial=[{"type": "transcript.created"}], on_send=on_send)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    monkeypatch.setattr(grok, "_GROK_STALLED_TURN_TIMEOUT_SECONDS", 0.2)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await _wait_until(lambda: any(isinstance(item, bytes) for item in first.sent))
    await first.server_send(
        {
            "type": "transcript.partial",
            "text": "mutable draft",
            "is_final": False,
            "speech_final": False,
        }
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    assert (await _next_event(responses, "partial")).text == "mutable draft"

    # No provider-committed text exists, so expiry follows the sibling
    # workers' empty-final contract (bounded loss of the mutable draft).
    expired = await _next_event(responses, "final")
    assert (expired.utterance_id, expired.text) == (1, "")
    await _wait_until(lambda: len(connector.calls) == 2)
    await _stop_worker(task, requests, responses)


async def test_grok_server_vad_partials_refresh_stalled_deadline(
    monkeypatch,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    websocket = _FakeWebSocket(
        initial=[{"type": "transcript.created"}], on_send=on_send
    )
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    monkeypatch.setattr(grok, "_GROK_STALLED_TURN_TIMEOUT_SECONDS", 0.5)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await _wait_until(lambda: any(isinstance(item, bytes) for item in websocket.sent))
    await websocket.server_send(
        {
            "type": "transcript.partial",
            "text": "a",
            "is_final": False,
            "speech_final": False,
        }
    )
    assert (await _next_event(responses, "utterance_started")).utterance_id == 1
    assert (await _next_event(responses, "partial")).text == "a"

    # Both mutable and locked partials refresh the armed deadline, so the
    # total elapsed time since the first partial exceeds the bound without
    # expiring the still-live utterance.
    for is_final in (False, True):
        await asyncio.sleep(0.25)
        await websocket.server_send(
            {
                "type": "transcript.partial",
                "text": "ab",
                "is_final": is_final,
                "speech_final": False,
            }
        )
        assert (await _next_event(responses, "partial")).text == "ab"
    await asyncio.sleep(0.25)
    await websocket.server_send(
        {
            "type": "transcript.partial",
            "text": "c",
            "is_final": True,
            "speech_final": True,
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (1, "abc")
    assert responses.empty()
    assert len(connector.calls) == 1
    await _stop_worker(task, requests, responses)


async def test_grok_manual_partials_never_arm_stalled_deadline(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "audio.done":
            await ws.server_send({"type": "transcript.done", "duration": 1.0})

    websocket = _FakeWebSocket(
        initial=[{"type": "transcript.created"}], on_send=on_send
    )
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(grok.websockets, "connect", connector)
    monkeypatch.setattr(grok, "_GROK_STALLED_TURN_TIMEOUT_SECONDS", 0.1)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        grok.grok_asr_worker(requests, responses, "key", AsrSessionConfig())
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await _wait_until(lambda: any(isinstance(item, bytes) for item in websocket.sent))
    await websocket.server_send(
        {
            "type": "transcript.partial",
            "text": "draft",
            "is_final": False,
            "speech_final": False,
        }
    )
    assert (await _next_event(responses, "partial")).text == "draft"

    # Manual-mode turns are sealed by upstream commits, not by the
    # server-VAD stalled deadline: waiting far past the bound must neither
    # emit a final nor reset the connection.
    await asyncio.sleep(0.3)
    assert responses.empty()
    assert len(connector.calls) == 1
    await _stop_worker(task, requests, responses)


@pytest.mark.parametrize(
    ("module", "worker"),
    [
        (qwen, qwen.qwen_asr_worker),
        (step, step.step_asr_worker),
        (openai, openai.openai_asr_worker),
        (grok, grok.grok_asr_worker),
    ],
)
async def test_workers_reject_unsupported_languages_without_connecting(
    monkeypatch,
    module,
    worker,
) -> None:
    connect_calls = 0

    async def unexpected_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("language validation must precede network access")

    monkeypatch.setattr(module.websockets, "connect", unexpected_connect)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    config = AsrSessionConfig(
        language="eo",
        endpointing_mode="provider" if module is openai else "manual",
    )
    await worker(
        requests,
        responses,
        "key",
        config,
    )
    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_LANGUAGE_NOT_SUPPORTED"
    assert (await _next_event(responses, "closed")).kind == "closed"
    assert connect_calls == 0


@pytest.mark.parametrize(
    ("worker", "expected_code"),
    [
        (qwen.qwen_asr_worker, "ASR_INVALID_CONFIG"),
        (step.step_asr_worker, "ASR_INVALID_CONFIG"),
        (grok.grok_asr_worker, "ASR_ENDPOINTING_NOT_SUPPORTED"),
    ],
)
async def test_workers_reject_unknown_endpointing_before_connect(
    worker,
    expected_code,
) -> None:
    config = AsrSessionConfig()
    object.__setattr__(config, "endpointing_mode", "vendor_private_mode")
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()

    await worker(requests, responses, "key", config)

    assert (await _next_event(responses, "error")).error_code == expected_code
    assert (await _next_event(responses, "closed")).kind == "closed"


@pytest.mark.parametrize("worker", [qwen.qwen_asr_worker, step.step_asr_worker])
async def test_workers_report_missing_credentials_without_connecting(worker) -> None:
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()

    await worker(requests, responses, "", AsrSessionConfig())

    assert (await _next_event(responses, "error")).error_code == (
        "ASR_CREDENTIALS_MISSING"
    )
    assert (await _next_event(responses, "closed")).kind == "closed"


async def test_qwen_rejects_unknown_region_without_connecting() -> None:
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()

    await qwen.qwen_asr_worker(
        requests,
        responses,
        "key",
        AsrSessionConfig(),
        region="unknown",
    )

    assert (await _next_event(responses, "error")).error_code == "ASR_INVALID_CONFIG"
    assert (await _next_event(responses, "closed")).kind == "closed"


@pytest.mark.parametrize(
    ("module", "worker", "expected_code"),
    [
        (qwen, qwen.qwen_asr_worker, "ASR_QWEN_PROTOCOL_ERROR"),
        (step, step.step_asr_worker, "ASR_STEP_PROTOCOL_ERROR"),
    ],
)
async def test_provider_endpointing_rejects_manual_commit(
    monkeypatch,
    module,
    worker,
    expected_code,
) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload)["type"] == "session.update":
            await ws.server_send({"type": "session.updated"})

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(module.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        worker(
            requests, responses, "key", AsrSessionConfig(endpointing_mode="provider")
        )
    )
    await _next_event(responses, "ready")

    await requests.put(_AsrWorkerRequest(kind="commit", generation=0, utterance_id=1))

    assert (await _next_event(responses, "error")).error_code == expected_code
    assert (await _next_event(responses, "closed")).kind == "closed"
    await asyncio.wait_for(task, 1)


async def test_workers_report_unexpected_disconnect(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str):
            message = json.loads(payload)
            if message["type"] == "session.update":
                await ws.server_send(
                    {"type": "session.updated", "session": message["session"]}
                )

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(openai.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        openai.openai_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    await websocket.server_end()
    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_OPENAI_DISCONNECTED"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)


def test_auth_rejection_classification() -> None:
    class Response:
        status_code = 401

    error = RuntimeError("must not be inspected or logged")
    error.response = Response()  # type: ignore[attr-defined]
    assert qwen._qwen_is_auth_rejection(error)
    assert step._step_is_auth_rejection(error)
    assert openai._openai_is_auth_rejection(error)
    assert grok._grok_is_auth_rejection(error)
    assert gemini._is_auth_rejection(error)

    ordinary_error = RuntimeError("network failure")
    assert not qwen._qwen_is_auth_rejection(ordinary_error)
    assert not step._step_is_auth_rejection(ordinary_error)
    assert not openai._openai_is_auth_rejection(ordinary_error)
    assert not grok._grok_is_auth_rejection(ordinary_error)
    assert not gemini._is_auth_rejection(ordinary_error)

    websocket_auth_error = RuntimeError("sensitive close reason")
    websocket_auth_error.code = 3000  # type: ignore[attr-defined]
    websocket_auth_error.reason = "invalid_request_error.invalid_api_key"  # type: ignore[attr-defined]
    assert openai._openai_is_auth_rejection(websocket_auth_error)

    websocket_other_error = RuntimeError("sensitive close reason")
    websocket_other_error.code = 3000  # type: ignore[attr-defined]
    websocket_other_error.reason = "invalid_request_error.unsupported_model"  # type: ignore[attr-defined]
    assert not openai._openai_is_auth_rejection(websocket_other_error)

    assert openai._openai_event_is_auth_rejection(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "message": "sensitive provider body",
            },
        }
    )
    assert not openai._openai_event_is_auth_rejection(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "unsupported_model",
            },
        }
    )


async def test_soniox_provider_endpoint_aggregates_stable_tokens(monkeypatch) -> None:
    websocket = _FakeWebSocket()
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "soniox-key",
            AsrSessionConfig(language="auto", endpointing_mode="provider"),
            region="jp",
        )
    )
    await _next_event(responses, "ready")
    config = json.loads(websocket.sent[0])
    assert connector.calls[0][0] == soniox.SONIOX_REGION_URLS["jp"]
    assert config["enable_endpoint_detection"] is True
    assert config["enable_language_identification"] is True

    pcm = b"\x12\x34" * 800
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=2,
            buffer_epoch=3,
            utterance_id=1,
            audio=pcm,
        )
    )
    await _wait_until(lambda: pcm in websocket.sent)
    await websocket.server_send(
        {
            "tokens": [
                {"text": "Hello ", "is_final": True},
                {"text": "wor", "is_final": False},
            ]
        }
    )
    started = await _next_event(responses, "utterance_started")
    partial = await _next_event(responses, "partial")
    assert (started.generation, started.buffer_epoch, started.utterance_id) == (
        2,
        3,
        1,
    )
    assert partial.text == "Hello wor"

    await websocket.server_send(
        {
            "tokens": [
                {"text": "world", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    final = await _next_event(responses, "final")
    assert final.text == "Hello world"
    assert "<end>" not in final.text

    second_pcm = b"\x56\x78" * 800
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=2,
            buffer_epoch=3,
            utterance_id=1,
            audio=second_pcm,
        )
    )
    await _wait_until(lambda: second_pcm in websocket.sent)
    await websocket.server_send(
        {
            "tokens": [
                {"text": "Second", "is_final": False},
            ]
        }
    )
    second_started = await _next_event(responses, "utterance_started")
    second_partial = await _next_event(responses, "partial")
    assert (
        second_started.generation,
        second_started.buffer_epoch,
        second_started.utterance_id,
    ) == (2, 3, 2)
    assert second_partial.text == "Second"
    await websocket.server_send(
        {
            "tokens": [
                {"text": "Second turn", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    second_final = await _next_event(responses, "final")
    assert (
        second_final.generation,
        second_final.buffer_epoch,
        second_final.utterance_id,
        second_final.text,
    ) == (2, 3, 2, "Second turn")
    await _stop_worker(
        task,
        requests,
        responses,
        generation=2,
        buffer_epoch=3,
        utterance_id=3,
    )


async def test_soniox_provider_session_endpoints_two_distinct_turns(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket()
    monkeypatch.setattr(soniox.websockets, "connect", _FakeConnector(websocket))
    transcripts: list[str] = []
    endpoints: list[int] = []
    errors: list[str] = []

    async def on_transcript(text: str) -> None:
        transcripts.append(text)

    async def on_error(error: str) -> None:
        errors.append(error)

    async def on_endpoint() -> None:
        endpoints.append(len(endpoints) + 1)

    session = _RealtimeAsrSessionImpl(
        worker_fn=soniox.soniox_asr_worker,
        api_key="key",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=on_transcript,
        on_connection_error=on_error,
        on_turn_endpointed=on_endpoint,
    )
    await session.connect()

    first_pcm = b"\x01\x00" * 160
    await session.stream_audio(first_pcm)
    await _wait_until(lambda: first_pcm in websocket.sent)
    await websocket.server_send(
        {
            "tokens": [
                {"text": "first", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    await _wait_until(lambda: transcripts == ["first"])

    second_pcm = b"\x02\x00" * 160
    await session.stream_audio(second_pcm)
    await _wait_until(lambda: second_pcm in websocket.sent)
    await websocket.server_send(
        {
            "tokens": [
                {"text": "second", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    await _wait_until(lambda: transcripts == ["first", "second"])

    assert endpoints == [1, 2]
    assert session._endpointed_turn_keys == {(0, 0, 1), (0, 0, 2)}
    assert errors == []
    await session.close()


async def test_soniox_manual_finalize_waits_for_fin(monkeypatch) -> None:
    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload).get("type") == "finalize":
            await ws.server_send(
                {
                    "tokens": [
                        {"text": "manual text", "is_final": True},
                        {"text": "<fin>", "is_final": True},
                    ]
                }
            )

    websocket = _FakeWebSocket(on_send=on_send)
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
            region="eu",
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=b"\0\0")
    )
    await requests.put(
        _AsrWorkerRequest(kind="commit", generation=0, utterance_id=1)
    )
    assert (await _next_event(responses, "final")).text == "manual text"
    await _stop_worker(task, requests, responses)


async def test_soniox_manual_finalize_preserves_turn_identity_until_fin(
    monkeypatch,
) -> None:
    finalize_count = 0

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        nonlocal finalize_count
        if not isinstance(payload, str):
            return
        if json.loads(payload).get("type") != "finalize":
            return
        finalize_count += 1
        if finalize_count == 2:
            await ws.server_send(
                {
                    "tokens": [
                        {"text": "second", "is_final": True},
                        {"text": "<fin>", "is_final": True},
                    ]
                }
            )

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(soniox.websockets, "connect", _FakeConnector(websocket))
    requests = _AsrRequestQueue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
        )
    )
    await _next_event(responses, "ready")

    first_audio = b"\x10\x10"
    second_audio = b"\x20\x20"
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=3,
            buffer_epoch=4,
            utterance_id=5,
            audio=first_audio,
        )
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="commit", generation=3, buffer_epoch=4, utterance_id=5
        )
    )
    await _wait_until(lambda: finalize_count == 1)

    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=8,
            buffer_epoch=9,
            utterance_id=6,
            audio=second_audio,
        )
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="commit", generation=8, buffer_epoch=9, utterance_id=6
        )
    )
    await _wait_until(requests.empty)
    await _wait_until(lambda: requests.held_audio_bytes == len(second_audio))
    assert second_audio not in websocket.sent
    assert requests.waiting_audio_bytes == len(second_audio)
    assert requests.waiting_audio_items == 1

    await websocket.server_send(
        {
            "tokens": [
                {"text": "first", "is_final": True},
                {"text": "<fin>", "is_final": True},
            ]
        }
    )
    first = await _next_event(responses, "final")
    assert (first.generation, first.buffer_epoch, first.utterance_id) == (3, 4, 5)
    assert first.text == "first"

    await _wait_until(lambda: finalize_count == 2)
    assert second_audio in websocket.sent
    second = await _next_event(responses, "final")
    assert (second.generation, second.buffer_epoch, second.utterance_id) == (8, 9, 6)
    assert second.text == "second"
    assert requests.waiting_audio_bytes == 0
    assert requests.waiting_audio_items == 0
    await _stop_worker(
        task,
        requests,
        responses,
        generation=8,
        buffer_epoch=9,
        utterance_id=7,
    )


async def test_soniox_pending_fin_keeps_deferred_audio_in_session_backpressure(
    monkeypatch,
) -> None:
    finalize_sent = asyncio.Event()

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        del ws
        if isinstance(payload, str) and json.loads(payload).get("type") == "finalize":
            finalize_sent.set()

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(soniox.websockets, "connect", _FakeConnector(websocket))
    monkeypatch.setattr(asr_infra, "_REQUEST_BACKPRESSURE_TIMEOUT_SECONDS", 0.02)
    transcripts: list[str] = []
    errors: list[str] = []

    async def capture_transcript(text: str) -> None:
        transcripts.append(text)

    async def capture_error(error: str) -> None:
        errors.append(error)

    session = _RealtimeAsrSessionImpl(
        worker_fn=soniox.soniox_asr_worker,
        api_key="key",
        config=AsrSessionConfig(endpointing_mode="manual"),
        on_input_transcript=capture_transcript,
        on_connection_error=capture_error,
    )
    await session.connect()
    first_audio = b"\x01\x00" * 160
    await session.stream_audio(first_audio)
    await session.signal_user_activity_end()
    await asyncio.wait_for(finalize_sent.wait(), 1)

    second_audio_a = b"\x02\x00" * 16_000
    second_audio_b = b"\x03\x00" * 16_000
    await session.stream_audio(second_audio_a)
    await session.stream_audio(second_audio_b)
    queue = session._request_queue
    assert isinstance(queue, _AsrRequestQueue)
    await _wait_until(
        lambda: queue.held_audio_bytes == len(second_audio_a) + len(second_audio_b)
    )

    with pytest.raises(RuntimeError, match="ASR_STREAM_BACKPRESSURE"):
        await session.stream_audio(b"\x04\x00" * 160)

    assert queue.waiting_audio_bytes == len(second_audio_a) + len(second_audio_b)
    assert queue.waiting_audio_items == 2
    assert second_audio_a not in websocket.sent
    assert second_audio_b not in websocket.sent
    assert transcripts == []
    assert errors == []

    await session.close()
    assert queue.waiting_audio_bytes == 0
    assert queue.waiting_audio_items == 0


async def test_soniox_fin_releases_deferred_audio_budget_in_fifo_order(
    monkeypatch,
) -> None:
    finalize_sent = asyncio.Event()

    async def on_send(ws: _FakeWebSocket, payload: str | bytes) -> None:
        del ws
        if isinstance(payload, str) and json.loads(payload).get("type") == "finalize":
            finalize_sent.set()

    websocket = _FakeWebSocket(on_send=on_send)
    monkeypatch.setattr(soniox.websockets, "connect", _FakeConnector(websocket))
    session = _RealtimeAsrSessionImpl(
        worker_fn=soniox.soniox_asr_worker,
        api_key="key",
        config=AsrSessionConfig(endpointing_mode="manual"),
        on_input_transcript=lambda text: asyncio.sleep(0),
        on_connection_error=lambda error: asyncio.sleep(0),
    )
    await session.connect()
    first_audio = b"\x11\x00" * 160
    await session.stream_audio(first_audio)
    await session.signal_user_activity_end()
    await asyncio.wait_for(finalize_sent.wait(), 1)

    second_audio_a = b"\x12\x00" * 16_000
    second_audio_b = b"\x13\x00" * 16_000
    second_audio_c = b"\x14\x00" * 160
    await session.stream_audio(second_audio_a)
    await session.stream_audio(second_audio_b)
    queue = session._request_queue
    assert isinstance(queue, _AsrRequestQueue)
    await _wait_until(
        lambda: queue.held_audio_bytes == len(second_audio_a) + len(second_audio_b)
    )
    blocked_producer = asyncio.create_task(session.stream_audio(second_audio_c))
    await asyncio.sleep(0)
    assert blocked_producer.done() is False

    await websocket.server_send(
        {
            "tokens": [
                {"text": "first", "is_final": True},
                {"text": "<fin>", "is_final": True},
            ]
        }
    )
    await asyncio.wait_for(blocked_producer, 1)
    await _wait_until(
        lambda: all(
            audio in websocket.sent
            for audio in (second_audio_a, second_audio_b, second_audio_c)
        )
    )
    wire_audio = [payload for payload in websocket.sent if isinstance(payload, bytes)]
    assert wire_audio[:4] == [
        first_audio,
        second_audio_a,
        second_audio_b,
        second_audio_c,
    ]
    assert queue.waiting_audio_bytes == 0
    assert queue.waiting_audio_items == 0

    await session.close()


@pytest.mark.parametrize("disconnect_at", ["before_send", "send_raises", "after_send"])
async def test_soniox_manual_reconnect_replays_pending_finalize(
    monkeypatch, disconnect_at: str
) -> None:
    finalize_started = asyncio.Event()

    class FirstWebSocket(_FakeWebSocket):
        async def send(self, payload: str | bytes) -> None:
            is_finalize = (
                isinstance(payload, str)
                and json.loads(payload).get("type") == "finalize"
            )
            if not is_finalize:
                await super().send(payload)
                return
            finalize_started.set()
            if disconnect_at == "before_send":
                await asyncio.Event().wait()
            if disconnect_at == "send_raises":
                raise RuntimeError("disconnect while sending finalize")
            await super().send(payload)
            await self.server_end()

    async def complete_on_finalize(
        ws: _FakeWebSocket, payload: str | bytes
    ) -> None:
        if isinstance(payload, str) and json.loads(payload).get("type") == "finalize":
            await ws.server_send(
                {
                    "tokens": [
                        {"text": "replayed manual", "is_final": True},
                        {"text": "<fin>", "is_final": True},
                    ]
                }
            )

    first = FirstWebSocket()
    second = _FakeWebSocket(on_send=complete_on_finalize)
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x30\x20" * 320
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=3,
            buffer_epoch=4,
            utterance_id=5,
            audio=pcm,
        )
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="commit", generation=3, buffer_epoch=4, utterance_id=5
        )
    )
    await finalize_started.wait()
    if disconnect_at == "before_send":
        await first.server_end()

    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(
        lambda: any(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "finalize"
            for payload in second.sent
        )
    )
    second_types = [
        "audio" if isinstance(payload, bytes) else json.loads(payload).get("type")
        for payload in second.sent
    ]
    assert second_types[:3] == [None, "audio", "finalize"]
    assert (await _next_event(responses, "final")).text == "replayed manual"
    await _stop_worker(
        task,
        requests,
        responses,
        generation=3,
        buffer_epoch=4,
        utterance_id=6,
    )


async def test_soniox_sender_cancellation_preserves_dequeued_control(
    monkeypatch,
) -> None:
    real_wait = asyncio.wait
    inner_wait_started = asyncio.Event()
    request_dequeued = asyncio.Event()
    hold_inner_wait = asyncio.Event()
    intercept_inner_wait = True

    async def controlled_wait(tasks, *args, **kwargs):
        nonlocal intercept_inner_wait
        task_names = {task.get_name() for task in tasks}
        is_connection_wait = {
            "soniox-asr-receiver",
            "soniox-asr-sender",
        }.issubset(task_names)
        if is_connection_wait or not intercept_inner_wait:
            return await real_wait(tasks, *args, **kwargs)
        inner_wait_started.set()
        result = await real_wait(tasks, *args, **kwargs)
        intercept_inner_wait = False
        request_dequeued.set()
        await hold_inner_wait.wait()
        return result

    monkeypatch.setattr(soniox.asyncio, "wait", controlled_wait)
    sockets = (_FakeWebSocket(), _FakeWebSocket(), _FakeWebSocket())
    connector = _FakeConnector(*sockets)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=1,
            buffer_epoch=2,
            utterance_id=3,
            audio=b"\x01\x00" * 160,
        )
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="commit",
            generation=1,
            buffer_epoch=2,
            utterance_id=3,
        )
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=1,
            buffer_epoch=2,
            utterance_id=4,
            audio=b"\x02\x00" * 160,
        )
    )
    await asyncio.wait_for(inner_wait_started.wait(), 1)
    await requests.put(
        _AsrWorkerRequest(
            kind="clear",
            generation=2,
            buffer_epoch=0,
            utterance_id=5,
        )
    )
    await asyncio.wait_for(request_dequeued.wait(), 1)
    await sockets[0].server_end()
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(lambda: len(connector.calls) == 3)

    await _stop_worker(
        task,
        requests,
        responses,
        generation=2,
        buffer_epoch=0,
        utterance_id=6,
    )


async def test_soniox_manual_clear_discards_pending_finalize(monkeypatch) -> None:
    first = _FakeWebSocket()
    second = _FakeWebSocket()
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests = _AsrRequestQueue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(
            kind="audio", generation=1, buffer_epoch=2, utterance_id=3, audio=b"\0\0"
        )
    )
    await requests.put(
        _AsrWorkerRequest(kind="commit", generation=1, buffer_epoch=2, utterance_id=3)
    )
    await _wait_until(
        lambda: any(
            isinstance(payload, str)
            and json.loads(payload).get("type") == "finalize"
            for payload in first.sent
        )
    )
    await requests.put(
        _AsrWorkerRequest(
            kind="audio",
            generation=1,
            buffer_epoch=2,
            utterance_id=4,
            audio=b"\x02\x00" * 160,
        )
    )
    await _wait_until(lambda: requests.held_audio_bytes > 0)
    await requests.put(
        _AsrWorkerRequest(kind="clear", generation=1, buffer_epoch=3, utterance_id=4)
    )
    await _wait_until(lambda: len(connector.calls) == 2)
    assert requests.held_audio_bytes == 0
    assert requests.held_audio_items == 0
    assert not any(
        isinstance(payload, str) and json.loads(payload).get("type") == "finalize"
        for payload in second.sent
    )
    await _stop_worker(
        task, requests, responses, generation=1, buffer_epoch=3, utterance_id=4
    )


async def test_soniox_empty_fin_clears_pending_finalize(monkeypatch) -> None:
    async def finish_empty(ws: _FakeWebSocket, payload: str | bytes) -> None:
        if isinstance(payload, str) and json.loads(payload).get("type") == "finalize":
            await ws.server_send({"tokens": [{"text": "<fin>", "is_final": True}]})

    first = _FakeWebSocket(on_send=finish_empty)
    second = _FakeWebSocket()
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="manual"),
        )
    )
    await _next_event(responses, "ready")
    await requests.put(
        _AsrWorkerRequest(
            kind="audio", generation=0, buffer_epoch=0, utterance_id=1, audio=b"\0\0"
        )
    )
    await requests.put(
        _AsrWorkerRequest(kind="commit", generation=0, buffer_epoch=0, utterance_id=1)
    )
    await asyncio.wait_for(requests.join(), 1)
    empty_final = await _next_event(responses, "final")
    assert empty_final.text == ""
    await first.server_end()
    await _wait_until(lambda: len(connector.calls) == 2)
    assert not any(
        isinstance(payload, str) and json.loads(payload).get("type") == "finalize"
        for payload in second.sent
    )
    await _stop_worker(task, requests, responses)


async def test_soniox_reconnects_once_and_replays_current_audio(monkeypatch) -> None:
    first = _FakeWebSocket()
    second = _FakeWebSocket()
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x20\x10" * 320
    await requests.put(
        _AsrWorkerRequest(
            kind="audio", generation=4, buffer_epoch=5, utterance_id=1, audio=pcm
        )
    )
    await _wait_until(lambda: pcm in first.sent)
    await first.server_end()
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(lambda: pcm in second.sent)
    await second.server_send(
        {
            "tokens": [
                {"text": "replayed", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    assert (await _next_event(responses, "utterance_started")).generation == 4
    assert (await _next_event(responses, "final")).text == "replayed"
    await _stop_worker(task, requests, responses, generation=4, buffer_epoch=5)


async def test_soniox_reconnect_preserves_next_turn_audio_sent_before_end(
    monkeypatch,
) -> None:
    # A tiny retention tail proves both directions: the next turn's frames
    # that reached the wire before <end> survive into the replay, while
    # audio older than the tail is dropped at the turn boundary.
    monkeypatch.setattr(soniox, "_REPLAY_TURN_TAIL_BYTES", 320)
    first = _FakeWebSocket()
    second = _FakeWebSocket()
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    first_pcm = b"\x01\x00" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=first_pcm)
    )
    await _wait_until(lambda: first_pcm in first.sent)
    # Turn 2's opening frame reaches the wire while turn 1's <end> token is
    # still in flight from the server.
    turn2_prefix = b"\x02\x00" * 160
    await requests.put(
        _AsrWorkerRequest(
            kind="audio", generation=0, utterance_id=1, audio=turn2_prefix
        )
    )
    await _wait_until(lambda: turn2_prefix in first.sent)
    await first.server_send(
        {
            "tokens": [
                {"text": "first", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    assert (await _next_event(responses, "final")).text == "first"

    turn2_rest = b"\x03\x00" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=turn2_rest)
    )
    await _wait_until(lambda: turn2_rest in first.sent)
    await first.server_end()

    # The reconnect replay must contain turn 2's pre-<end> prefix, not only
    # the frames sent after <end> was processed; turn 1's older audio beyond
    # the retention tail is gone.
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(
        lambda: any(isinstance(sent, bytes) and sent for sent in second.sent)
    )
    replayed = next(sent for sent in second.sent if isinstance(sent, bytes) and sent)
    assert replayed == turn2_prefix + turn2_rest

    await second.server_send(
        {
            "tokens": [
                {"text": "second turn", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "second turn")
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_soniox_retained_tail_does_not_complete_empty_turn(
    monkeypatch,
) -> None:
    websocket = _FakeWebSocket()
    monkeypatch.setattr(soniox.websockets, "connect", _FakeConnector(websocket))
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x01\x00" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=pcm)
    )
    await _wait_until(lambda: pcm in websocket.sent)
    await websocket.server_send(
        {
            "tokens": [
                {"text": "first", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    assert (await _next_event(responses, "final")).text == "first"

    # A stray <end> with no tokens (e.g. re-detected on the retained trailing
    # audio) must not complete an empty turn off the carried tail alone.
    await websocket.server_send({"tokens": [{"text": "<end>", "is_final": True}]})
    await _wait_until(lambda: websocket.incoming.empty())
    assert responses.empty()

    await websocket.server_send(
        {
            "tokens": [
                {"text": "second", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    started = await _next_event(responses, "utterance_started")
    final = await _next_event(responses, "final")
    assert (started.utterance_id, final.utterance_id, final.text) == (2, 2, "second")
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_soniox_planned_rotation_drops_completed_turn_carryover(
    monkeypatch,
) -> None:
    monkeypatch.setattr(soniox, "_KEEPALIVE_SECONDS", 0.05)
    first = _FakeWebSocket()
    second = _FakeWebSocket()
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    pcm1 = b"\x01\x00" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=pcm1)
    )
    await _wait_until(lambda: pcm1 in first.sent)
    await first.server_send(
        {
            "tokens": [
                {"text": "first", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    assert (await _next_event(responses, "final")).text == "first"

    # The buffer now holds only the completed turn's carried tail; the next
    # keepalive timeout takes the planned rotation.
    monkeypatch.setattr(soniox, "_SAFE_ROTATION_SECONDS", -1.0)
    await _wait_until(lambda: len(connector.calls) == 2)
    monkeypatch.setattr(soniox, "_SAFE_ROTATION_SECONDS", 10_000.0)
    assert responses.empty()

    pcm2 = b"\x02\x00" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=pcm2)
    )
    await _wait_until(lambda: pcm2 in second.sent)
    # The fresh connection must not replay the finished turn's PCM, or the
    # provider could transcribe its final words as a duplicate utterance.
    assert [
        payload for payload in second.sent if isinstance(payload, bytes) and payload
    ] == [pcm2]
    await second.server_send(
        {
            "tokens": [
                {"text": "second", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "second")
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_soniox_unexpected_disconnect_still_replays_completed_turn_tail(
    monkeypatch,
) -> None:
    first = _FakeWebSocket()
    second = _FakeWebSocket()
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x01\x00" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=pcm)
    )
    await _wait_until(lambda: pcm in first.sent)
    await first.server_send(
        {
            "tokens": [
                {"text": "first", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    assert (await _next_event(responses, "final")).text == "first"
    await first.server_end()

    # An unexpected disconnect (not a planned rotation) keeps the carried
    # tail so the next turn's opening frames cannot be replayed away.
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(
        lambda: any(isinstance(sent, bytes) and sent for sent in second.sent)
    )
    replayed = next(sent for sent in second.sent if isinstance(sent, bytes) and sent)
    assert replayed == pcm
    await second.server_send(
        {
            "tokens": [
                {"text": "second", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    final = await _next_event(responses, "final")
    assert (final.utterance_id, final.text) == (2, "second")
    await _stop_worker(task, requests, responses, utterance_id=3)


async def test_soniox_rotation_deferred_while_current_turn_audio_buffered(
    monkeypatch,
) -> None:
    monkeypatch.setattr(soniox, "_KEEPALIVE_SECONDS", 0.05)
    first = _FakeWebSocket()
    connector = _FakeConnector(first)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x01\x00" * 160
    await requests.put(
        _AsrWorkerRequest(kind="audio", generation=0, utterance_id=1, audio=pcm)
    )
    await _wait_until(lambda: pcm in first.sent)
    monkeypatch.setattr(soniox, "_SAFE_ROTATION_SECONDS", -1.0)
    sent_before = len(first.sent)
    # With current-turn audio buffered the rotation gate must defer and send
    # keepalives on the existing connection instead of rotating. Waiting for
    # two keepalives guarantees at least one gate evaluation ran after the
    # rotation threshold was lowered.
    await _wait_until(
        lambda: sum(
            1
            for payload in first.sent[sent_before:]
            if isinstance(payload, str)
            and json.loads(payload).get("type") == "keepalive"
        )
        >= 2,
        timeout=2.0,
    )
    assert len(connector.calls) == 1
    monkeypatch.setattr(soniox, "_SAFE_ROTATION_SECONDS", 10_000.0)
    await first.server_send(
        {
            "tokens": [
                {"text": "kept", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    assert (await _next_event(responses, "final")).text == "kept"
    await _stop_worker(task, requests, responses, utterance_id=2)


async def test_soniox_reconnect_replays_frame_cancelled_mid_send(monkeypatch) -> None:
    send_suspended = asyncio.Event()

    class BlockingAudioWebSocket(_FakeWebSocket):
        async def send(self, payload: str | bytes) -> None:
            if isinstance(payload, bytes) and payload:
                # Suspend the sender inside connection.send() forever so the
                # receiver-detected disconnect cancels it mid-flight.
                send_suspended.set()
                await asyncio.Event().wait()
            await super().send(payload)

    first = BlockingAudioWebSocket()
    second = _FakeWebSocket()
    connector = _FakeConnector(first, second)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x20\x10" * 320
    await requests.put(
        _AsrWorkerRequest(
            kind="audio", generation=4, buffer_epoch=5, utterance_id=1, audio=pcm
        )
    )
    await asyncio.wait_for(send_suspended.wait(), 1)
    await first.server_end()

    # The dequeued frame whose send() was cancelled mid-flight must survive
    # into the reconnect replay buffer instead of being silently dropped.
    await _wait_until(lambda: len(connector.calls) == 2)
    await _wait_until(lambda: pcm in second.sent)
    assert second.sent[1] == pcm
    await second.server_send(
        {
            "tokens": [
                {"text": "recovered", "is_final": True},
                {"text": "<end>", "is_final": True},
            ]
        }
    )
    assert (await _next_event(responses, "final")).text == "recovered"
    await _stop_worker(task, requests, responses, generation=4, buffer_epoch=5)


async def test_soniox_blocks_disconnect_when_replay_is_incomplete(monkeypatch) -> None:
    first = _FakeWebSocket()
    connector = _FakeConnector(first)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    monkeypatch.setattr(soniox, "_MAX_REPLAY_BYTES", 4)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    pcm = b"\x01\x00" * 3
    await requests.put(
        _AsrWorkerRequest(
            kind="audio", generation=1, buffer_epoch=2, utterance_id=3, audio=pcm
        )
    )
    await _wait_until(lambda: pcm in first.sent)
    await first.server_end()

    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_SONIOX_REPLAY_INCOMPLETE"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    assert len(connector.calls) == 1


async def test_soniox_replay_policy_can_disable_preconnect_reconnect(
    monkeypatch,
) -> None:
    first = _FakeWebSocket()
    connector = _FakeConnector(first)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
            replay_policy="none",
        )
    )
    await _next_event(responses, "ready")
    await first.server_end()

    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_SONIOX_REPLAY_DISABLED"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    assert len(connector.calls) == 1


def test_workers_preserve_provider_auto_language_detection() -> None:
    assert qwen._qwen_language_code("auto") is None
    assert step._step_language_code("auto") is None
    assert openai._normalize_openai_language("auto") is None
    assert grok._normalize_grok_language("auto") is None


async def test_soniox_auth_error_is_terminal_without_reconnect(monkeypatch) -> None:
    websocket = _FakeWebSocket(
        initial=[
            {
                "error_code": 401,
                "error_message": "not logged",
                "request_id": "request-1",
            }
        ]
    )
    connector = _FakeConnector(websocket)
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "bad-key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_CREDENTIALS_REJECTED"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    assert len(connector.calls) == 1


async def test_soniox_rate_limit_backs_off_and_reconnects_only_once(
    monkeypatch,
) -> None:
    rate_limit_event = {
        "error_code": 429,
        "error_message": "rate limited",
        "request_id": "request-rate-limit",
    }
    connector = _FakeConnector(
        _FakeWebSocket(initial=[rate_limit_event]),
        _FakeWebSocket(initial=[rate_limit_event]),
    )
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    monkeypatch.setattr(soniox, "_RETRY_BACKOFF_BASE_SECONDS", 0.0)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_RATE_LIMITED"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    assert len(connector.calls) == 2


async def test_soniox_rate_limit_backoff_is_capped(monkeypatch) -> None:
    rate_limit_event = {
        "error_code": 429,
        "error_message": "rate limited",
        "request_id": "request-rate-limit",
    }
    connector = _FakeConnector(
        _FakeWebSocket(initial=[rate_limit_event]),
        _FakeWebSocket(initial=[rate_limit_event]),
    )
    monkeypatch.setattr(soniox.websockets, "connect", connector)
    monkeypatch.setattr(
        soniox,
        "_RETRY_BACKOFF_BASE_SECONDS",
        soniox._RETRY_BACKOFF_CAP_SECONDS * 128,
    )
    observed_delays: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float) -> None:
        observed_delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(soniox.asyncio, "sleep", recording_sleep)
    requests: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    responses: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    task = asyncio.create_task(
        soniox.soniox_asr_worker(
            requests,
            responses,
            "key",
            AsrSessionConfig(endpointing_mode="provider"),
        )
    )
    await _next_event(responses, "ready")
    error = await _next_event(responses, "error")
    assert error.error_code == "ASR_RATE_LIMITED"
    await _next_event(responses, "closed")
    await asyncio.wait_for(task, 1)
    assert observed_delays
    assert max(observed_delays) <= soniox._RETRY_BACKOFF_CAP_SECONDS
