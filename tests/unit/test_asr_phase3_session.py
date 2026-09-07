import asyncio
import inspect
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main_logic.asr_client._infra as infra_module

from main_logic.asr_client import create_asr_session
from main_logic.asr_client._infra import (
    AsrSessionConfig,
    _AsrWorkerEvent,
    _CallbackItem,
    _RealtimeAsrSessionImpl,
)
from main_logic.asr_client._provider_events import (
    ProviderAudioRange,
    ProviderEndpointNotification,
    ProviderFinalNotification,
    ProviderStartedSettlement,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from main_logic.asr_client.endpointing.detector import (
    AsrDetectorDispatcher,
    CoreDetectorEventEnvelope,
    DetectorActivityEvent,
    DetectorCandidateKey,
    DetectorIngressIdentity,
)
from main_logic.asr_client.lifecycle import (
    VoiceInputLifecycleController,
    VoiceLifecycleState,
    VoiceRouteMode,
    VoiceTurnToken,
)
from main_logic.asr_client.provider_policy import (
    AsrProviderPolicy,
    resolve_provider_policy,
)
from main_logic.asr_client.runtime import (
    AsrRuntimeCallbacks,
    IndependentAsrRuntime,
)
from main_logic.voice_turn.contracts import (
    AsrFailureEvent,
    AsrLifecycleNotification,
    AsrStatusEvent,
    SpeechActivityEvent,
)


pytestmark = pytest.mark.asyncio


async def test_public_turn_endpointed_callback_remains_keyless() -> None:
    parameter = inspect.signature(create_asr_session).parameters["on_turn_endpointed"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert "Callable[[], Awaitable[None]]" in str(parameter.annotation)


class _FakeVoiceTurnAdapter:
    def __init__(
        self,
        on_commit: Callable[[int, int, int], Awaitable[None]],
    ) -> None:
        self.on_commit = on_commit
        self.started = False
        self.closed = False
        self.audio: list[tuple[int, int, int, bytes]] = []
        self.resets: list[tuple[int, int, int]] = []
        self.failure = asyncio.get_running_loop().create_future()

    async def start(self) -> None:
        self.started = True

    async def push_audio(
        self,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
        pcm16: bytes,
    ) -> None:
        self.audio.append((generation, buffer_epoch, utterance_id, pcm16))

    async def reset(
        self,
        *,
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        self.resets.append((generation, buffer_epoch, utterance_id))

    async def close(self) -> None:
        self.closed = True

    async def wait_failure(self):
        return await self.failure

    def report_failure(self, failure) -> None:
        if not self.failure.done():
            self.failure.set_result(failure)


class _FailingVoiceTurnAdapter(_FakeVoiceTurnAdapter):
    def __init__(
        self,
        on_commit: Callable[[int, int, int], Awaitable[None]],
        *,
        fail_start: bool = False,
        fail_close: bool = False,
    ) -> None:
        super().__init__(on_commit)
        self.fail_start = fail_start
        self.fail_close = fail_close
        self.close_calls = 0

    async def start(self) -> None:
        self.started = True
        if self.fail_start:
            raise RuntimeError("adapter start failed")

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self.fail_close:
            raise RuntimeError("adapter close failed")


async def _recording_worker(request_queue, response_queue, api_key, config):
    del api_key, config
    await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
    while True:
        request = await request_queue.get()
        try:
            if request.kind == "shutdown":
                await response_queue.put(
                    _AsrWorkerEvent(kind="closed", generation=request.generation)
                )
                return
        finally:
            request_queue.task_done()


async def test_session_fans_same_normalized_pcm_to_worker_and_voice_turn():
    adapter = None

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        voice_turn_factory=factory,
    )
    await session.connect()
    assert adapter is not None and adapter.started

    pcm = b"\x01\x00" * 160
    await session.stream_audio(pcm)
    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)
    assert adapter.audio == [(0, 0, 1, pcm)]
    assert session.provider_wire_audio_ms == 10
    await session.close()
    assert adapter.closed


async def test_voice_turn_commit_is_identity_checked_and_commits_once():
    observed = []
    endpointed: list[str] = []

    async def worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                observed.append(request)
                if request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(kind="closed", generation=request.generation)
                    )
                    return
            finally:
                request_queue.task_done()

    adapter = None

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_turn_endpointed=AsyncMock(side_effect=lambda: endpointed.append("sealed")),
        voice_turn_factory=factory,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    assert adapter is not None
    await adapter.on_commit(0, 0, 1)
    await adapter.on_commit(0, 0, 1)
    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)
    assert [item.kind for item in observed].count("commit") == 1
    assert endpointed == ["sealed"]
    assert adapter.resets[-1] == (0, 0, 2)
    await session.close()


async def test_clear_invalidates_late_voice_turn_commit():
    observed = []

    async def worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                observed.append(request)
                if request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(kind="closed", generation=request.generation)
                    )
                    return
            finally:
                request_queue.task_done()

    adapter = None

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        voice_turn_factory=factory,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.clear_audio_buffer()
    assert adapter is not None
    await adapter.on_commit(0, 0, 1)
    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)
    assert [item.kind for item in observed].count("commit") == 0
    assert adapter.resets[-1] == (0, 1, 2)
    await session.close()


async def test_segmented_routes_use_smart_turn_and_openai_uses_provider_vad(
    monkeypatch,
):
    import utils.config_manager as config_manager

    class _ConfigManager:
        def get_core_config(self):
            return {
                "ASSIST_API_KEY_OPENAI": "openai-key",
                "ASSIST_API_KEY_GLM": "glm-key",
                "ASSIST_API_KEY_GEMINI": "gemini-key",
            }

    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.setattr(
        config_manager,
        "get_config_manager",
        lambda: _ConfigManager(),
    )
    for core_type in ("glm", "gemini"):
        session = create_asr_session(
            core_type,
            on_input_transcript=AsyncMock(),
            on_connection_error=AsyncMock(),
        )
        assert session._voice_turn_factory is not None
    openai_session = create_asr_session(
        "openai",
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )
    assert openai_session._config.endpointing_mode == "provider"
    assert openai_session._voice_turn_factory is None


async def test_voice_turn_start_failure_fails_session_and_releases_adapter():
    adapter = None
    on_error = AsyncMock()

    def factory(on_commit):
        nonlocal adapter
        adapter = _FailingVoiceTurnAdapter(on_commit, fail_start=True)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=on_error,
        voice_turn_factory=factory,
    )

    with pytest.raises(RuntimeError, match="ASR_VOICE_TURN_START_FAILED"):
        await session.connect()

    assert adapter is not None
    assert adapter.close_calls == 1
    assert adapter.closed
    assert session._state.value == "failed"
    assert session._worker_task is not None and session._worker_task.done()
    on_error.assert_awaited_once_with(
        "ASR_VOICE_TURN_START_FAILED: voice turn adapter failed to start"
    )


async def test_voice_turn_close_failure_does_not_block_session_cleanup():
    adapter = None

    def factory(on_commit):
        nonlocal adapter
        adapter = _FailingVoiceTurnAdapter(on_commit, fail_close=True)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        voice_turn_factory=factory,
    )
    await session.connect()

    await session.close()

    assert adapter is not None and adapter.close_calls == 1
    assert session._state.value == "closed"
    assert session._worker_task is not None and session._worker_task.done()


async def test_worker_failure_unloads_voice_turn_even_when_adapter_close_fails():
    emit_error = asyncio.Event()
    error_reported = asyncio.Event()
    adapter = None

    async def worker(request_queue, response_queue, api_key, config):
        del request_queue, api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        await emit_error.wait()
        await response_queue.put(
            _AsrWorkerEvent(
                kind="error",
                generation=0,
                error_code="ASR_PROVIDER_FAILED",
                error_message="provider failed",
            )
        )
        await asyncio.Event().wait()

    async def on_error(error: str) -> None:
        assert error == "ASR_PROVIDER_FAILED: provider failed"
        error_reported.set()

    def factory(on_commit):
        nonlocal adapter
        adapter = _FailingVoiceTurnAdapter(on_commit, fail_close=True)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=on_error,
        voice_turn_factory=factory,
    )
    await session.connect()
    emit_error.set()
    await asyncio.wait_for(error_reported.wait(), 1)

    assert adapter is not None and adapter.close_calls == 1
    assert session._voice_turn_adapter is None
    assert session._state.value == "failed"


async def test_voice_turn_terminal_failure_fails_session_and_closes_once():
    adapter = None
    on_error = AsyncMock()

    def factory(on_commit):
        nonlocal adapter
        adapter = _FailingVoiceTurnAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=on_error,
        voice_turn_factory=factory,
    )
    await session.connect()
    assert adapter is not None

    failure = type(
        "Failure",
        (),
        {"kind": "unavailable", "stage": "vad_load"},
    )()
    adapter.report_failure(failure)
    await asyncio.wait_for(
        asyncio.create_task(_wait_until(lambda: session._state.value == "failed")),
        1,
    )

    assert adapter.close_calls == 1
    assert session._voice_turn_adapter is None
    assert session._voice_turn_watch_task is None
    on_error.assert_awaited_once_with(
        "ASR_ENDPOINTING_FAILED: required voice turn endpointing failed"
    )
    await asyncio.wait_for(session.close(), 1)
    assert adapter.close_calls == 1
    assert session._state.value == "closed"


async def test_segmented_forced_splits_wait_for_logical_turn_completion():
    adapter = None
    callbacks: list[str] = []
    requests = []
    endpointed: list[str] = []

    async def worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                requests.append(request)
                if request.kind == "commit":
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="final",
                            generation=request.generation,
                            buffer_epoch=request.buffer_epoch,
                            utterance_id=request.utterance_id,
                            text=f"part-{request.utterance_id}",
                        )
                    )
                if request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="closed",
                            generation=request.generation,
                        )
                    )
                    return
            finally:
                request_queue.task_done()

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    async def on_transcript(text: str) -> None:
        callbacks.append(text)

    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=on_transcript,
        on_connection_error=AsyncMock(),
        on_turn_endpointed=AsyncMock(side_effect=lambda: endpointed.append("sealed")),
        voice_turn_factory=factory,
        provider_policy=AsrProviderPolicy(
            transport="segmented",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=10,
            warm_transport_ms=0,
            replay_policy="none",
        ),
    )
    await session.connect()
    assert adapter is not None

    await session.stream_audio(b"\x01\x00" * 160)
    await session.stream_audio(b"\x02\x00" * 160)
    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)
    await asyncio.sleep(0)

    assert callbacks == []
    assert endpointed == []
    assert [
        request.utterance_id for request in requests if request.kind == "commit"
    ] == [
        1,
        2,
    ]
    assert {item[2] for item in adapter.audio} == {1}

    await adapter.on_commit(0, 0, 1)
    await asyncio.wait_for(_wait_until(lambda: bool(callbacks)), 1)
    assert endpointed == ["sealed"]
    assert callbacks == ["part-1 part-2"]
    await session.close()


async def test_segmented_transcript_first_deadline_starts_at_logical_completion():
    adapter = None
    notifications: list[ProviderFinalNotification] = []

    async def worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                if request.kind == "commit":
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="final",
                            generation=request.generation,
                            buffer_epoch=request.buffer_epoch,
                            utterance_id=request.utterance_id,
                            text="physical-final",
                        )
                    )
                if request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(kind="closed", generation=request.generation)
                    )
                    return
            finally:
                request_queue.task_done()

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    async def on_ready(notification: ProviderFinalNotification) -> None:
        notifications.append(notification)

    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_final_ready=on_ready,
        voice_turn_factory=factory,
        provider_policy=AsrProviderPolicy(
            transport="segmented",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=10,
            warm_transport_ms=0,
            replay_policy="none",
        ),
    )
    await session.connect()
    assert adapter is not None
    await session.stream_audio(b"\x01\x00" * 160)
    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)
    assert session._response_queue is not None
    await asyncio.wait_for(session._response_queue.join(), 1)
    assert notifications == []

    logical_ready_not_before = infra_module.time.monotonic()
    await adapter.on_commit(0, 0, 1)
    await asyncio.wait_for(_wait_until(lambda: bool(notifications)), 1)

    notification = notifications[0]
    assert notification.key is None
    assert notification.text == "physical-final"
    assert notification.received_at >= logical_ready_not_before
    assert notification.admission_deadline - notification.received_at == pytest.approx(
        0.2
    )
    await session.close()


async def test_segmented_completion_first_deadline_starts_when_transcript_arrives():
    adapter = None
    commit_seen = asyncio.Event()
    release_final = asyncio.Event()
    notifications: list[ProviderFinalNotification] = []

    async def worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                if request.kind == "commit":
                    commit_seen.set()
                    await release_final.wait()
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="final",
                            generation=request.generation,
                            buffer_epoch=request.buffer_epoch,
                            utterance_id=request.utterance_id,
                            text="physical-final",
                        )
                    )
                if request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(kind="closed", generation=request.generation)
                    )
                    return
            finally:
                request_queue.task_done()

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    async def on_ready(notification: ProviderFinalNotification) -> None:
        notifications.append(notification)

    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_final_ready=on_ready,
        voice_turn_factory=factory,
        provider_policy=AsrProviderPolicy(
            transport="segmented",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=10,
            warm_transport_ms=0,
            replay_policy="none",
        ),
    )
    await session.connect()
    assert adapter is not None
    await session.stream_audio(b"\x01\x00" * 160)
    await asyncio.wait_for(commit_seen.wait(), 1)
    await adapter.on_commit(0, 0, 1)
    assert notifications == []

    transcript_ready_not_before = infra_module.time.monotonic()
    release_final.set()
    await asyncio.wait_for(_wait_until(lambda: bool(notifications)), 1)

    notification = notifications[0]
    assert notification.key is None
    assert notification.received_at >= transcript_ready_not_before
    assert notification.admission_deadline - notification.received_at == pytest.approx(
        0.2
    )
    await session.close()


async def test_segmented_single_chunk_is_split_before_provider_enqueue():
    adapter = None
    requests = []

    async def worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                requests.append(request)
                if request.kind == "commit":
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="final",
                            generation=request.generation,
                            buffer_epoch=request.buffer_epoch,
                            utterance_id=request.utterance_id,
                            text=f"part-{request.utterance_id}",
                        )
                    )
                if request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(kind="closed", generation=request.generation)
                    )
                    return
            finally:
                request_queue.task_done()

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    callback = AsyncMock()
    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=callback,
        on_connection_error=AsyncMock(),
        voice_turn_factory=factory,
        provider_policy=AsrProviderPolicy(
            transport="segmented",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=10,
            warm_transport_ms=0,
            replay_policy="none",
        ),
    )
    await session.connect()

    await session.stream_audio(b"\x01\x00" * 400)
    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)

    audio_by_utterance: dict[int, int] = {}
    for request in requests:
        if request.kind == "audio":
            assert request.utterance_id is not None
            audio_by_utterance[request.utterance_id] = audio_by_utterance.get(
                request.utterance_id, 0
            ) + len(request.audio)
    assert audio_by_utterance == {1: 320, 2: 320, 3: 160}
    assert callback.await_count == 0

    assert adapter is not None
    await adapter.on_commit(0, 0, 1)
    await asyncio.wait_for(_wait_until(lambda: callback.await_count == 1), 1)
    callback.assert_awaited_once_with("part-1 part-2 part-3")
    await session.close()


async def test_segmented_final_segment_is_aggregated_with_forced_segments():
    adapter = None
    callbacks: list[str] = []

    async def on_transcript(text: str) -> None:
        callbacks.append(text)

    async def worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                if request.kind == "commit":
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="final",
                            generation=request.generation,
                            buffer_epoch=request.buffer_epoch,
                            utterance_id=request.utterance_id,
                            text=f"segment-{request.utterance_id}",
                        )
                    )
                if request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(kind="closed", generation=request.generation)
                    )
                    return
            finally:
                request_queue.task_done()

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=on_transcript,
        on_connection_error=AsyncMock(),
        voice_turn_factory=factory,
        provider_policy=AsrProviderPolicy(
            transport="segmented",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=10,
            warm_transport_ms=0,
            replay_policy="none",
        ),
    )
    await session.connect()
    assert adapter is not None

    await session.stream_audio(b"\x01\x00" * 160)
    await session.stream_audio(b"\x02\x00" * 80)
    await adapter.on_commit(0, 0, 1)
    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)
    assert session._callback_queue is not None
    await asyncio.wait_for(session._callback_queue.join(), 1)

    assert callbacks == ["segment-1 segment-2"]
    assert adapter.resets[-1] == (0, 0, 2)
    await session.close()


async def test_segmented_local_buffering_is_not_counted_as_provider_wire_audio():
    adapter = None

    def factory(on_commit):
        nonlocal adapter
        adapter = _FakeVoiceTurnAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        voice_turn_factory=factory,
        provider_policy=AsrProviderPolicy(
            transport="segmented",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=1_000,
            warm_transport_ms=0,
            replay_policy="none",
        ),
    )
    await session.connect()
    await session.stream_audio(b"\x01\x00" * 160)

    assert session.provider_wire_audio_ms == 0
    assert adapter is not None
    await adapter.on_commit(0, 0, 1)
    assert session.provider_wire_audio_ms == 10
    await session.close()


async def test_clear_drops_final_already_waiting_in_callback_queue():
    callback = AsyncMock()
    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=callback,
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    assert session._callback_task is not None
    session._callback_task.cancel()
    await asyncio.gather(session._callback_task, return_exceptions=True)
    assert session._callback_queue is not None
    await session._callback_queue.put(
        _CallbackItem(
            text="stale final",
            generation=session._generation,
            buffer_epoch=session._buffer_epoch,
        )
    )

    await session.clear_audio_buffer()
    session._callback_task = asyncio.create_task(session._dispatch_callbacks())
    await asyncio.wait_for(session._callback_queue.join(), 1)

    callback.assert_not_awaited()
    await session.close()


async def test_resampler_tail_is_included_in_streaming_provider_wire_metric():
    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    await session.stream_audio(b"\x01\x00" * 160)
    before_ms = session.provider_wire_audio_ms
    session._flush_resampler = MagicMock(return_value=b"\x02\x00" * 160)

    await session.signal_user_activity_end()

    assert session.provider_wire_audio_ms == before_ms + 10
    await session.close()


async def test_voice_turn_push_failure_fails_session_instead_of_staying_ready():
    on_error = AsyncMock()

    class _PushFailAdapter(_FakeVoiceTurnAdapter):
        async def push_audio(self, **kwargs) -> None:
            del kwargs
            raise RuntimeError("push failed")

    adapter = None

    def factory(on_commit):
        nonlocal adapter
        adapter = _PushFailAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=on_error,
        voice_turn_factory=factory,
    )
    await session.connect()

    with pytest.raises(RuntimeError, match="ASR_ENDPOINTING_FAILED"):
        await session.stream_audio(b"\x01\x00" * 160)

    assert session._state.value == "failed"
    assert adapter is not None and adapter.closed
    on_error.assert_awaited_once_with(
        "ASR_ENDPOINTING_FAILED: required voice turn endpointing failed"
    )


async def test_voice_turn_reset_failure_fails_session_instead_of_staying_ready():
    on_error = AsyncMock()

    class _ResetFailAdapter(_FakeVoiceTurnAdapter):
        async def reset(self, **kwargs) -> None:
            del kwargs
            raise RuntimeError("reset failed")

    adapter = None

    def factory(on_commit):
        nonlocal adapter
        adapter = _ResetFailAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=on_error,
        voice_turn_factory=factory,
    )
    await session.connect()

    await session.stream_audio(b"\x01\x00" * 160)
    await session.signal_user_activity_end()
    await asyncio.wait_for(
        _wait_until(lambda: session._state.value == "failed"),
        1,
    )

    assert session._state.value == "failed"
    assert adapter is not None and adapter.closed
    on_error.assert_awaited_once_with(
        "ASR_ENDPOINTING_FAILED: required voice turn endpointing failed"
    )


async def test_close_waits_for_managed_voice_turn_reset_before_adapter_close():
    class _BlockingResetAdapter(_FakeVoiceTurnAdapter):
        def __init__(self, on_commit):
            super().__init__(on_commit)
            self.reset_started = asyncio.Event()
            self.release_reset = asyncio.Event()

        async def reset(self, **kwargs) -> None:
            self.reset_started.set()
            await self.release_reset.wait()
            await super().reset(**kwargs)

    adapter = None

    def factory(on_commit):
        nonlocal adapter
        adapter = _BlockingResetAdapter(on_commit)
        return adapter

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        voice_turn_factory=factory,
    )
    await session.connect()
    await session.stream_audio(b"\x01\x00" * 160)
    await session.signal_user_activity_end()
    assert adapter is not None
    await asyncio.wait_for(adapter.reset_started.wait(), 1)

    close_task = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert adapter.closed is False
    adapter.release_reset.set()
    await asyncio.wait_for(close_task, 1)

    assert adapter.closed is True
    assert session._voice_turn_reset_task is None


def _build_fail_closed_runtime() -> tuple[
    IndependentAsrRuntime,
    list[str],
    list[AsrFailureEvent],
    list[AsrStatusEvent],
]:
    lifecycle_states: list[str] = []
    failures: list[AsrFailureEvent] = []
    statuses: list[AsrStatusEvent] = []

    async def on_lifecycle(notification: AsrLifecycleNotification) -> None:
        # Yield so concurrently scheduled dispatcher teardown runs mid-delivery.
        await asyncio.sleep(0)
        lifecycle_states.append(notification.state)

    async def on_failure(event: AsrFailureEvent) -> None:
        failures.append(event)

    async def on_status(event: AsrStatusEvent) -> None:
        statuses.append(event)

    runtime = IndependentAsrRuntime(
        AsrRuntimeCallbacks(
            display_name=lambda: "Test",
            on_prepare_turn=AsyncMock(return_value=True),
            on_partial=AsyncMock(),
            on_final=AsyncMock(),
            on_turn_abandoned=AsyncMock(),
            on_failure=on_failure,
            on_status=on_status,
            on_lifecycle=on_lifecycle,
        )
    )
    return runtime, lifecycle_states, failures, statuses


def _install_independent_asr_turn(
    runtime: IndependentAsrRuntime,
    session: SimpleNamespace,
) -> tuple[VoiceTurnToken, VoiceInputLifecycleController]:
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = SimpleNamespace(
        detector_epoch=1,
        endpointing_ready=lambda _token: True,
        close=AsyncMock(),
    )
    runtime._asr_session = session
    runtime._asr_provider = "qwen"
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = detector
    ingress = runtime.capture_ingress_token(
        connection_id="socket",
        lease_generation=1,
        route_generation=1,
    )
    runtime._asr_current_ingress_token = ingress
    turn_token = VoiceTurnToken(
        ingress=ingress,
        turn_id=lifecycle.snapshot.turn_id,
    )
    return turn_token, lifecycle


async def _drain_fail_closed_teardown(runtime: IndependentAsrRuntime) -> None:
    if runtime._asr_close_tasks:
        await asyncio.gather(*runtime._asr_close_tasks, return_exceptions=True)
    await runtime._asr_audio_dispatcher.close()
    await runtime._asr_detector_dispatcher.close()


async def test_provider_stream_failure_still_delivers_fail_closed_notifications():
    runtime, lifecycle_states, failures, statuses = _build_fail_closed_runtime()
    session = SimpleNamespace(
        is_ready=True,
        stream_audio=AsyncMock(side_effect=RuntimeError("provider write failed")),
        signal_user_activity_end=AsyncMock(),
        close=AsyncMock(),
    )
    turn_token, _lifecycle = _install_independent_asr_turn(runtime, session)
    audio_dispatcher = runtime._asr_audio_dispatcher

    assert audio_dispatcher.activate(turn_token, session, b"\x01\x00" * 160)
    await asyncio.wait_for(_wait_until(lambda: len(statuses) == 1), 1)

    assert lifecycle_states == [VoiceLifecycleState.BLOCKED.value]
    assert [event.code for event in failures] == ["ASR_INDEPENDENT_STREAM_FAILED"]
    assert [event.code for event in statuses] == ["ASR_INDEPENDENT_STREAM_FAILED"]
    assert statuses[0].session_epoch == runtime._asr_session_epoch
    assert runtime._asr_session is None
    for task in list(audio_dispatcher._failure_tasks):
        await task
    await _drain_fail_closed_teardown(runtime)


async def test_detector_handler_failure_still_delivers_fail_closed_notifications():
    runtime, lifecycle_states, failures, statuses = _build_fail_closed_runtime()
    session = SimpleNamespace(
        is_ready=True,
        signal_user_activity_end=AsyncMock(),
        close=AsyncMock(),
    )
    turn_token, lifecycle = _install_independent_asr_turn(runtime, session)
    detector = runtime._asr_detector
    detector_dispatcher = AsrDetectorDispatcher(
        AsyncMock(side_effect=RuntimeError("detector handler failed")),
        on_failure=runtime._handle_asr_detector_dispatcher_failure,
    )
    runtime._asr_detector_dispatcher = detector_dispatcher
    envelope = CoreDetectorEventEnvelope(
        event=DetectorActivityEvent(
            ingress=DetectorIngressIdentity(
                ingress_token=turn_token.ingress,
                detector_epoch=detector.detector_epoch,
                sequence_no=1,
            ),
            candidate=DetectorCandidateKey(detector.detector_epoch, 1),
            activity=SpeechActivityEvent.SPEECH_STARTED,
        ),
        detector_ref=detector,
        lifecycle_ref=lifecycle,
        session_epoch=runtime._asr_session_epoch,
    )

    assert detector_dispatcher.submit_nowait(envelope)
    await asyncio.wait_for(_wait_until(lambda: len(statuses) == 1), 1)

    assert lifecycle_states == [VoiceLifecycleState.BLOCKED.value]
    assert [event.code for event in failures] == ["ASR_ENDPOINTING_FAILED"]
    assert [event.code for event in statuses] == ["ASR_ENDPOINTING_FAILED"]
    assert statuses[0].session_epoch == runtime._asr_session_epoch
    assert runtime._asr_session is None
    for task in list(detector_dispatcher._failure_tasks):
        await task
    await _drain_fail_closed_teardown(runtime)


def _make_provider_endpoint_session(events: list[str]) -> _RealtimeAsrSessionImpl:
    async def on_transcript(text: str) -> None:
        events.append(f"final:{text}")

    async def on_endpoint() -> None:
        events.append("endpoint")

    return _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=on_transcript,
        on_connection_error=AsyncMock(),
        on_turn_endpointed=on_endpoint,
    )


async def _drain_session_pipelines(session: _RealtimeAsrSessionImpl) -> None:
    assert session._response_queue is not None
    assert session._callback_queue is not None
    await asyncio.wait_for(session._response_queue.join(), 1)
    await asyncio.wait_for(session._callback_queue.join(), 1)


async def test_provider_out_of_order_finals_seal_each_turn_in_order():
    events: list[str] = []
    session = _make_provider_endpoint_session(events)
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        # Turn 2 completes before turn 1: its endpoint must not fire while
        # turn 1 is still the active ordered turn, or the runtime seals the
        # wrong turn and turn 2's final is later discarded unsealed.
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
        _AsrWorkerEvent(kind="final", utterance_id=10, text="first", **common),
    ):
        await session._response_queue.put(event)
    await asyncio.wait_for(_wait_until(lambda: len(events) == 4), 1)
    assert events == ["endpoint", "final:first", "endpoint", "final:second"]
    await session.close()


async def test_provider_ordered_lane_waits_first_settlement_before_second_endpoint():
    events: list[str] = []
    first_entered = asyncio.Event()
    first_settled = asyncio.Event()

    async def on_transcript(text: str) -> None:
        if text == "first":
            events.append("final:first:waiting")
            first_entered.set()
            await first_settled.wait()
        events.append(f"final:{text}")

    async def on_endpoint() -> None:
        events.append("endpoint")

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=on_transcript,
        on_connection_error=AsyncMock(),
        on_turn_endpointed=on_endpoint,
    )
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="final", utterance_id=10, text="first", **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
    ):
        await session._response_queue.put(event)

    await asyncio.wait_for(first_entered.wait(), 1)
    await asyncio.sleep(0)
    assert events == ["endpoint", "final:first:waiting"]

    first_settled.set()
    await asyncio.wait_for(_wait_until(lambda: len(events) == 5), 1)
    assert events == [
        "endpoint",
        "final:first:waiting",
        "final:first",
        "endpoint",
        "final:second",
    ]
    await session.close()


async def test_provider_in_order_finals_keep_endpoint_before_each_final():
    events: list[str] = []
    session = _make_provider_endpoint_session(events)
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="final", utterance_id=10, text="first", **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
    ):
        await session._response_queue.put(event)
    await asyncio.wait_for(_wait_until(lambda: len(events) == 4), 1)
    assert events == ["endpoint", "final:first", "endpoint", "final:second"]
    await session.close()


async def test_provider_completed_key_cannot_be_restarted_or_delivered_twice():
    events: list[str] = []
    session = _make_provider_endpoint_session(events)
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    assert session._response_queue is not None
    common = {
        "generation": 0,
        "buffer_epoch": 0,
        "utterance_id": 10,
    }
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(kind="final", text="first", **common),
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(kind="final", text="duplicate", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    assert events == ["endpoint", "final:first"]
    await session.close()


async def test_provider_expired_empty_final_releases_queued_endpoint():
    events: list[str] = []
    session = _make_provider_endpoint_session(events)
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)
    # Turn 2's endpoint and final are both held behind the missing turn 1.
    assert events == []
    # The worker stalled-turn expiry completes turn 1 with an empty final;
    # this must advance the ordered pipeline and release turn 2 as well.
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", utterance_id=10, text="", **common)
    )
    await asyncio.wait_for(_wait_until(lambda: len(events) == 4), 1)
    assert events == ["endpoint", "final:", "endpoint", "final:second"]
    await session.close()


async def test_provider_clear_drops_queued_endpoint_for_invalidated_keys():
    events: list[str] = []
    session = _make_provider_endpoint_session(events)
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)
    assert events == []
    await session.clear_audio_buffer()
    await _drain_session_pipelines(session)
    # The invalidated keys must never seal or deliver after the clear.
    assert events == []
    # The next epoch's turn still seals and delivers normally.
    next_common = {"generation": 0, "buffer_epoch": session._buffer_epoch}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=20, **next_common),
        _AsrWorkerEvent(kind="final", utterance_id=20, text="hello", **next_common),
    ):
        await session._response_queue.put(event)
    await asyncio.wait_for(_wait_until(lambda: len(events) == 2), 1)
    assert events == ["endpoint", "final:hello"]
    await session.close()


def _make_rich_provider_session(
    events: list[tuple],
    legacy_events: list[tuple],
) -> _RealtimeAsrSessionImpl:
    async def on_legacy_transcript(text: str) -> None:
        legacy_events.append(("final", text))

    async def on_legacy_endpoint() -> None:
        legacy_events.append(("endpoint",))

    async def on_provider_endpoint(
        notification: ProviderEndpointNotification,
    ) -> None:
        events.append(
            (
                "endpoint",
                notification.phase,
                notification.key,
                notification.boundary_quality,
                notification.audio_range,
            )
        )

    async def on_provider_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    return _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=on_legacy_transcript,
        on_connection_error=AsyncMock(),
        on_turn_endpointed=on_legacy_endpoint,
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=on_provider_final,
    )


async def test_provider_utterance_started_notification_exposes_stable_key() -> None:
    notification = ProviderUtteranceStartedNotification(
        generation=2,
        buffer_epoch=3,
        utterance_id=4,
    )

    assert notification.key == ProviderUtteranceKey(2, 3, 4)
    assert notification.namespace == (2, 3)
    assert notification.audio_start_sample_16k is None
    assert (
        ProviderUtteranceStartedNotification(2, 3, 4, 320).audio_start_sample_16k == 320
    )
    with pytest.raises(ValueError, match="ASR_PROVIDER_ENDPOINT_KEY_INVALID"):
        ProviderUtteranceStartedNotification(
            generation=2,
            buffer_epoch=3,
            utterance_id=0,
        )
    for invalid_start in (-1, True, 1.5):
        with pytest.raises(ValueError, match="ASR_PROVIDER_AUDIO_START_INVALID"):
            ProviderUtteranceStartedNotification(2, 3, 4, invalid_start)  # type: ignore[arg-type]


async def test_provider_start_uses_canonical_cursor_and_clear_rebases_it() -> None:
    starts: list[ProviderUtteranceStartedNotification] = []

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> ProviderStartedSettlement:
        starts.append(notification)
        return ProviderStartedSettlement.BOUND_EXACT_PENDING

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
    )
    await session.connect()
    assert session._response_queue is not None

    await session.stream_audio(b"\x00\x00" * 1_600)
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            generation=0,
            buffer_epoch=0,
            utterance_id=10,
            audio_start_sample_16k=320,
        )
    )
    await _drain_session_pipelines(session)

    await session.clear_audio_buffer()
    await session.stream_audio(b"\x00\x00" * 800)
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            generation=0,
            buffer_epoch=1,
            utterance_id=11,
            audio_start_sample_16k=160,
        )
    )
    await _drain_session_pipelines(session)

    assert [item.audio_start_sample_16k for item in starts] == [320, 1_760]
    await session.close()


async def test_provider_future_start_preserves_canonical_authority() -> None:
    starts: list[int | None] = []

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> ProviderStartedSettlement:
        starts.append(notification.audio_start_sample_16k)
        return ProviderStartedSettlement.BOUND_EXACT_PENDING

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    assert session._response_queue is not None
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            generation=0,
            buffer_epoch=0,
            utterance_id=10,
            audio_start_sample_16k=320,
        )
    )
    await _drain_session_pipelines(session)

    # DetectorRuntime owns the bounded wait for a start ahead of its current
    # cursor. Infra must preserve that authority instead of converting it to
    # an indistinguishable missing-start proof.
    assert starts == [320]
    await session.close()


async def test_provider_exact_validation_ignores_simulated_24k_wire_cursor() -> None:
    endpoints: list[ProviderEndpointNotification] = []

    async def on_endpoint(notification: ProviderEndpointNotification) -> None:
        endpoints.append(notification)

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_endpoint=on_endpoint,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_600)
    # Simulate a worker whose transport expands the canonical 16 kHz input to
    # 24 kHz. A forged range beyond canonical coverage must remain unknown.
    session._provider_wire_audio_bytes = 2_400 * 2
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(
            kind="utterance_started",
            audio_start_sample_16k=100,
            **common,
        ),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(100, 2_000),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="hello", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    assert [item.boundary_quality for item in endpoints] == ["unknown", "unknown"]
    await session.close()


async def test_speaker_unavailable_started_settlement_keeps_text_key_alive() -> None:
    finals: list[tuple[ProviderUtteranceKey, str]] = []

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> ProviderStartedSettlement:
        assert notification.audio_start_sample_16k is None
        return ProviderStartedSettlement.BOUND_SPEAKER_UNAVAILABLE

    async def on_final(key: ProviderUtteranceKey, text: str) -> None:
        finals.append((key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
        on_provider_final=on_final,
    )
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", **common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", text="still delivered", **common)
    )
    await _drain_session_pipelines(session)

    assert session._provider_started_failed_keys == {}
    assert finals == [(ProviderUtteranceKey(0, 0, 10), "still delivered")]
    await session.close()


async def test_failed_identity_started_settlement_latches_text_key() -> None:
    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> ProviderStartedSettlement:
        del notification
        return ProviderStartedSettlement.FAILED_IDENTITY

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
    )
    await session.connect()
    assert session._response_queue is not None
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            generation=0,
            buffer_epoch=0,
            utterance_id=10,
        )
    )
    await asyncio.wait_for(
        _wait_until(lambda: (0, 0, 10) in session._provider_started_failed_keys),
        1,
    )

    assert (0, 0, 10) in session._provider_started_failed_keys
    await session.close()


async def test_conflicting_provider_start_revokes_proof_without_latching_text() -> None:
    starts: list[int | None] = []
    finals: list[str] = []
    endpoints: list[tuple[str, str]] = []

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> ProviderStartedSettlement:
        starts.append(notification.audio_start_sample_16k)
        return (
            ProviderStartedSettlement.BOUND_EXACT_PENDING
            if notification.audio_start_sample_16k is not None
            else ProviderStartedSettlement.BOUND_SPEAKER_UNAVAILABLE
        )

    async def on_final(key: ProviderUtteranceKey, text: str) -> None:
        del key
        finals.append(text)

    async def on_endpoint(notification: ProviderEndpointNotification) -> None:
        endpoints.append((notification.phase, notification.boundary_quality))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
        on_provider_endpoint=on_endpoint,
        on_provider_final=on_final,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for start in (100, 100):
        await session._response_queue.put(
            _AsrWorkerEvent(
                kind="utterance_started",
                audio_start_sample_16k=start,
                **common,
            )
        )
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="unknown",
            **common,
        )
    )
    for start in (200, 300):
        await session._response_queue.put(
            _AsrWorkerEvent(
                kind="utterance_started",
                audio_start_sample_16k=start,
                **common,
            )
        )
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(100, 900),
            **common,
        )
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", text="kept", **common)
    )
    await _drain_session_pipelines(session)

    assert starts == [100]
    assert endpoints == [("boundary", "unknown"), ("ordered", "unknown")]
    assert session._provider_started_failed_keys == {}
    assert finals == ["kept"]
    await session.close()


async def test_conflict_queued_behind_failed_started_never_reenters_callback() -> None:
    starts: list[int | None] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> ProviderStartedSettlement:
        starts.append(notification.audio_start_sample_16k)
        entered.set()
        await release.wait()
        return ProviderStartedSettlement.FAILED_IDENTITY

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            audio_start_sample_16k=100,
            **common,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            audio_start_sample_16k=200,
            **common,
        )
    )
    await asyncio.wait_for(session._response_queue.join(), 1)
    release.set()
    assert session._callback_queue is not None
    await asyncio.wait_for(session._callback_queue.join(), 1)

    assert starts == [100]
    assert (0, 0, 10) in session._provider_started_failed_keys
    await session.close()


async def test_conflicting_start_cannot_strand_callback_fifo_on_clear() -> None:
    callback_calls = 0
    release_ignored_callback = asyncio.Event()

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> ProviderStartedSettlement:
        del notification
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 1:
            return ProviderStartedSettlement.BOUND_EXACT_PENDING
        while not release_ignored_callback.is_set():
            try:
                await release_ignored_callback.wait()
            except asyncio.CancelledError:
                continue
        return ProviderStartedSettlement.BOUND_SPEAKER_UNAVAILABLE

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            audio_start_sample_16k=100,
            **common,
        )
    )
    await _drain_session_pipelines(session)
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            audio_start_sample_16k=200,
            **common,
        )
    )
    await asyncio.wait_for(session._response_queue.join(), 1)
    await asyncio.sleep(0)
    await session.clear_audio_buffer()
    assert session._callback_queue is not None
    drained = True
    try:
        await asyncio.wait_for(asyncio.shield(session._callback_queue.join()), 0.1)
    except asyncio.TimeoutError:
        drained = False
    finally:
        release_ignored_callback.set()

    assert drained
    assert callback_calls == 1
    await session.close()


async def test_provider_started_callback_is_idempotent_and_precedes_boundary_final():
    events: list[tuple] = []
    started_entered = asyncio.Event()
    release_started = asyncio.Event()

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> None:
        started_entered.set()
        await release_started.wait()
        events.append(("started", notification.key))

    async def on_endpoint(notification: ProviderEndpointNotification) -> None:
        events.append(
            (
                "endpoint",
                notification.phase,
                notification.key,
                notification.boundary_quality,
            )
        )

    async def on_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
        on_provider_endpoint=on_endpoint,
        on_provider_final=on_final,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(100, 500),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="hello", **common),
    ):
        await session._response_queue.put(event)

    await asyncio.wait_for(started_entered.wait(), 1)
    await asyncio.wait_for(session._response_queue.join(), 1)
    assert events == []

    release_started.set()
    await _drain_session_pipelines(session)

    key = ProviderUtteranceKey(0, 0, 10)
    assert events == [
        ("started", key),
        ("endpoint", "boundary", key, "exact"),
        ("endpoint", "ordered", key, "exact"),
        ("final", key, "hello"),
    ]
    await session.close()


async def test_clear_fences_provider_started_waiting_in_callback_fifo() -> None:
    started: list[ProviderUtteranceKey] = []
    delivered: list[tuple] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> None:
        if notification.utterance_id == 10:
            first_entered.set()
            await release_first.wait()
        started.append(notification.key)

    async def on_endpoint(notification: ProviderEndpointNotification) -> None:
        delivered.append(("endpoint", notification.key))

    async def on_final(key: ProviderUtteranceKey, text: str) -> None:
        delivered.append(("final", key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
        on_provider_endpoint=on_endpoint,
        on_provider_final=on_final,
    )
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="provider_endpoint",
            utterance_id=10,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 100),
            **common,
        )
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", utterance_id=10, text="stale", **common)
    )
    await asyncio.wait_for(first_entered.wait(), 1)
    await asyncio.wait_for(session._response_queue.join(), 1)
    boundary_task = session._provider_boundary_tasks[(0, 0, 10)]
    started_settlements = tuple(session._provider_started_settlements.values())

    session._revoke_provider_boundary_chain()
    with pytest.raises(asyncio.CancelledError):
        await boundary_task
    assert all(not settlement.done() for settlement in started_settlements)

    await session.clear_audio_buffer()
    release_first.set()
    assert session._callback_queue is not None
    await asyncio.wait_for(session._callback_queue.join(), 1)

    assert all(
        settlement.done() and not settlement.result()
        for settlement in started_settlements
    )
    assert started == []
    assert delivered == []
    assert session._provider_started_settlements == {}
    assert session._provider_started_callback_tasks == {}
    assert session._provider_started_retired_tasks == set()
    await session.close()


async def test_close_revokes_provider_started_settlement_and_owned_callback() -> None:
    started_entered = asyncio.Event()
    started_cancelled = asyncio.Event()

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> None:
        del notification
        started_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            started_cancelled.set()
            raise

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
    )
    await session.connect()
    assert session._response_queue is not None
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            generation=0,
            buffer_epoch=0,
            utterance_id=10,
        )
    )
    await asyncio.wait_for(started_entered.wait(), 1)
    await asyncio.wait_for(session._response_queue.join(), 1)
    settlement = session._provider_started_settlements[(0, 0, 10)]

    await asyncio.wait_for(session.close(), 1)

    assert started_cancelled.is_set()
    assert settlement.done()
    assert settlement.result() is False
    assert session._provider_started_settlements == {}
    assert session._provider_started_callback_tasks == {}
    assert session._provider_started_retired_tasks == set()


async def test_provider_started_callback_can_close_its_own_session() -> None:
    callback_closed = asyncio.Event()
    session: _RealtimeAsrSessionImpl

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> None:
        del notification
        await session.close()
        callback_closed.set()

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
    )
    await session.connect()
    assert session._response_queue is not None
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            generation=0,
            buffer_epoch=0,
            utterance_id=10,
        )
    )

    await asyncio.wait_for(callback_closed.wait(), 1)
    await session.close()
    await asyncio.sleep(0)

    assert not session.is_ready
    assert session._provider_started_settlements == {}
    assert session._provider_started_callback_tasks == {}
    assert session._provider_started_retired_tasks == set()


async def test_provider_started_callback_failure_blocks_same_key_callbacks() -> None:
    events: list[tuple] = []
    failed_entered = asyncio.Event()
    release_failed = asyncio.Event()

    async def fail_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> None:
        if notification.utterance_id == 10:
            failed_entered.set()
            await release_failed.wait()
            raise RuntimeError("started callback failed")

    async def on_endpoint(notification: ProviderEndpointNotification) -> None:
        events.append(
            (
                "endpoint",
                notification.phase,
                notification.key,
                notification.boundary_quality,
            )
        )

    async def on_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    async def on_partial(text: str) -> None:
        events.append(("partial", text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=fail_started,
        on_provider_endpoint=on_endpoint,
        on_provider_final=on_final,
    )
    session._on_partial_transcript = on_partial
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", **common)
    )
    next_common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 11}
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", **next_common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", text="next turn", **next_common)
    )
    await asyncio.wait_for(failed_entered.wait(), 1)
    await asyncio.wait_for(session._response_queue.join(), 1)
    assert (0, 0, 11) in session._pending_finals

    release_failed.set()
    await asyncio.wait_for(
        _wait_until(lambda: (0, 0, 10) in session._provider_started_failed_keys),
        1,
    )
    assert (0, 0, 10) in session._provider_started_failed_keys
    assert session.is_ready

    await _drain_session_pipelines(session)

    # Key 10 never produces a final while key 11 is already pending behind it.
    # Exact retirement must actively drain the ready successor.
    next_key = ProviderUtteranceKey(0, 0, 11)
    assert events[-1] == ("final", next_key, "next turn")
    failed_key = ProviderUtteranceKey(0, 0, 10)
    assert all(failed_key not in event for event in events)

    delivered_before_late_events = list(events)
    # Once the failed key is retired, every late event is rejected at ingress;
    # it cannot rely solely on an unbounded tombstone to stay fail-closed.
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", **common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 100),
            **common,
        )
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="partial", text="must not leak", **common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", text="must not leak", **common)
    )
    await _drain_session_pipelines(session)
    assert events == delivered_before_late_events
    await session.close()


async def test_provider_started_child_cancellation_does_not_stop_dispatcher() -> None:
    finals: list[tuple[ProviderUtteranceKey, str]] = []

    async def on_started(
        notification: ProviderUtteranceStartedNotification,
    ) -> None:
        if notification.utterance_id == 10:
            raise asyncio.CancelledError

    async def on_final(key: ProviderUtteranceKey, text: str) -> None:
        finals.append((key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_utterance_started=on_started,
        on_provider_final=on_final,
    )
    await session.connect()
    assert session._response_queue is not None
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="utterance_started",
            generation=0,
            buffer_epoch=0,
            utterance_id=10,
        )
    )
    await asyncio.wait_for(
        _wait_until(lambda: (0, 0, 10) in session._provider_started_failed_keys),
        1,
    )

    assert session._callback_task is not None
    assert not session._callback_task.done()

    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 11}
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", **common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", text="still alive", **common)
    )
    await _drain_session_pipelines(session)

    assert finals == [(ProviderUtteranceKey(0, 0, 11), "still alive")]
    assert not session._callback_task.done()
    await session.close()


async def test_provider_started_failed_tombstones_are_bounded() -> None:
    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )

    for utterance_id in range(
        1,
        infra_module._PROVIDER_STARTED_TOMBSTONE_LIMIT + 2,
    ):
        session._latch_failed_provider_started_key((0, 0, utterance_id))

    assert (
        len(session._provider_started_failed_keys)
        == infra_module._PROVIDER_STARTED_TOMBSTONE_LIMIT
    )
    assert (0, 0, 1) in session._provider_started_failed_keys
    assert session._provider_started_failed_namespace == (0, 0)


async def test_provider_rich_callbacks_carry_exact_key_without_legacy_duplicates():
    events: list[tuple] = []
    legacy_events: list[tuple] = []
    session = _make_rich_provider_session(events, legacy_events)
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(100, 500),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="hello", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    key = ProviderUtteranceKey(0, 0, 10)
    exact_range = ProviderAudioRange(100, 500)
    assert events == [
        ("endpoint", "boundary", key, "exact", exact_range),
        ("endpoint", "ordered", key, "exact", exact_range),
        ("final", key, "hello"),
    ]
    assert legacy_events == []
    await session.close()


async def test_provider_final_deadline_is_captured_at_receipt_before_fifo_wait():
    notifications: list[ProviderFinalNotification] = []

    async def on_ready(notification: ProviderFinalNotification) -> None:
        notifications.append(notification)

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_final_ready=on_ready,
    )
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for utterance_id in (10, 11):
        await session._response_queue.put(
            _AsrWorkerEvent(
                kind="utterance_started",
                utterance_id=utterance_id,
                **common,
            )
        )
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="final",
            utterance_id=11,
            text="second",
            **common,
        )
    )
    await asyncio.wait_for(session._response_queue.join(), 1)
    pending_second = session._pending_finals[(0, 0, 11)]

    await asyncio.sleep(0.02)
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="final",
            utterance_id=10,
            text="first",
            **common,
        )
    )
    await _drain_session_pipelines(session)

    assert [item.key for item in notifications] == [
        ProviderUtteranceKey(0, 0, 10),
        ProviderUtteranceKey(0, 0, 11),
    ]
    assert notifications[1].received_at == pending_second.received_at
    assert notifications[1].admission_deadline == pending_second.admission_deadline
    await session.close()


async def test_legacy_two_argument_provider_final_callback_still_receives_final():
    received: list[tuple[ProviderUtteranceKey, str]] = []

    async def on_provider_final(
        key: ProviderUtteranceKey,
        text: str,
    ) -> None:
        received.append((key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_final=on_provider_final,
    )
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", **common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", text="hello", **common)
    )
    await _drain_session_pipelines(session)

    assert received == [(ProviderUtteranceKey(0, 0, 10), "hello")]
    await session.close()


async def test_provider_boundary_callback_never_blocks_response_consumer():
    events: list[tuple] = []
    boundary_started = asyncio.Event()
    release_boundary = asyncio.Event()

    async def on_provider_endpoint(
        notification: ProviderEndpointNotification,
    ) -> None:
        if notification.phase == "boundary":
            boundary_started.set()
            await release_boundary.wait()
        events.append(("endpoint", notification.phase, notification.key))

    async def on_provider_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=on_provider_final,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    key = ProviderUtteranceKey(0, 0, 10)
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 500),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="hello", **common),
    ):
        await session._response_queue.put(event)

    await asyncio.wait_for(boundary_started.wait(), 1)
    await asyncio.wait_for(session._response_queue.join(), 0.2)
    assert events == []

    release_boundary.set()
    await _drain_session_pipelines(session)
    assert events == [
        ("endpoint", "boundary", key),
        ("endpoint", "ordered", key),
        ("final", key, "hello"),
    ]
    await session.close()


async def test_provider_boundary_callbacks_preserve_cross_key_worker_order():
    boundary_started: list[ProviderUtteranceKey] = []
    events: list[tuple] = []
    release_first_boundary = asyncio.Event()

    async def on_provider_endpoint(
        notification: ProviderEndpointNotification,
    ) -> None:
        if notification.phase == "boundary":
            boundary_started.append(notification.key)
            if len(boundary_started) == 1:
                await release_first_boundary.wait()
        events.append(("endpoint", notification.phase, notification.key))

    async def on_provider_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=on_provider_final,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    key_one = ProviderUtteranceKey(0, 0, 10)
    key_two = ProviderUtteranceKey(0, 0, 11)
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            utterance_id=10,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 400),
            **common,
        ),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            utterance_id=11,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(400, 900),
            **common,
        ),
    ):
        await session._response_queue.put(event)

    await asyncio.wait_for(_wait_until(lambda: bool(boundary_started)), 1)
    await asyncio.wait_for(session._response_queue.join(), 0.2)
    assert boundary_started == [key_one]

    release_first_boundary.set()
    await asyncio.wait_for(_wait_until(lambda: len(boundary_started) == 2), 1)
    assert boundary_started == [key_one, key_two]

    for event in (
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
        _AsrWorkerEvent(kind="final", utterance_id=10, text="first", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    assert events == [
        ("endpoint", "boundary", key_one),
        ("endpoint", "boundary", key_two),
        ("endpoint", "ordered", key_one),
        ("final", key_one, "first"),
        ("endpoint", "ordered", key_two),
        ("final", key_two, "second"),
    ]
    await session.close()


async def test_provider_boundary_timeout_downgrades_and_delivers_final():
    events: list[tuple] = []
    boundary_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_cancelled_boundary = asyncio.Event()

    async def on_provider_endpoint(
        notification: ProviderEndpointNotification,
    ) -> None:
        if notification.phase == "boundary":
            boundary_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_cancelled_boundary.wait()
                return
        events.append(
            (
                "endpoint",
                notification.phase,
                notification.key,
                notification.boundary_quality,
            )
        )

    async def on_provider_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=on_provider_final,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    key = ProviderUtteranceKey(0, 0, 10)
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 500),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="hello", **common),
    ):
        await session._response_queue.put(event)

    await asyncio.wait_for(boundary_started.wait(), 1)
    await asyncio.wait_for(cancellation_seen.wait(), 1)
    await asyncio.wait_for(
        _wait_until(lambda: events[-1:] == [("final", key, "hello")]),
        1,
    )
    # The boundary callback consumed the final's absolute budget. Optional
    # ordered speaker authority is skipped, while the transcript still flows.
    assert events == [("final", key, "hello")]

    release_cancelled_boundary.set()
    await session.close()


async def test_provider_boundary_callback_failure_downgrades_ordered_exact():
    events: list[tuple] = []

    async def on_provider_endpoint(
        notification: ProviderEndpointNotification,
    ) -> None:
        if notification.phase == "boundary":
            raise RuntimeError("speaker reconciliation failed")
        events.append(
            (
                "endpoint",
                notification.phase,
                notification.key,
                notification.boundary_quality,
            )
        )

    async def on_provider_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=on_provider_final,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    key = ProviderUtteranceKey(0, 0, 10)
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 500),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="still delivered", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    assert events == [
        ("endpoint", "ordered", key, "unknown"),
        ("final", key, "still delivered"),
    ]
    await session.close()


async def test_ordered_provider_callback_deadline_retires_late_task_and_final_flows(
    monkeypatch,
):
    """The ordered callback has its own hard budget and cannot hold final FIFO."""

    monkeypatch.setattr(
        infra_module,
        "_PROVIDER_BOUNDARY_CALLBACK_TIMEOUT_SECONDS",
        0.01,
    )
    events: list[tuple] = []
    ordered_started = asyncio.Event()
    ordered_cancelled = asyncio.Event()
    release_cancelled_ordered = asyncio.Event()

    async def on_provider_endpoint(
        notification: ProviderEndpointNotification,
    ) -> None:
        if notification.phase == "ordered":
            ordered_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                ordered_cancelled.set()
                await release_cancelled_ordered.wait()
                return
        events.append(("endpoint", notification.phase, notification.key))

    async def on_provider_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=on_provider_final,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    key = ProviderUtteranceKey(0, 0, 10)
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 500),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="still-forwarded", **common),
    ):
        await session._response_queue.put(event)

    await asyncio.wait_for(ordered_started.wait(), 1)
    await asyncio.wait_for(ordered_cancelled.wait(), 1)
    await asyncio.wait_for(
        _wait_until(lambda: events[-1:] == [("final", key, "still-forwarded")]),
        1,
    )

    assert events == [("endpoint", "boundary", key), ("final", key, "still-forwarded")]
    assert session._provider_boundary_retired_tasks
    release_cancelled_ordered.set()
    await session.close()


async def test_close_recancels_retired_provider_boundary_task_without_hanging():
    boundary_started = asyncio.Event()
    first_cancellation = asyncio.Event()
    shutdown_cancellation = asyncio.Event()
    block_boundary = asyncio.Event()
    block_retired_boundary = asyncio.Event()

    async def on_provider_endpoint(
        notification: ProviderEndpointNotification,
    ) -> None:
        if notification.phase != "boundary":
            return
        boundary_started.set()
        try:
            await block_boundary.wait()
        except asyncio.CancelledError:
            first_cancellation.set()
            try:
                await block_retired_boundary.wait()
            except asyncio.CancelledError:
                shutdown_cancellation.set()
                raise

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=AsyncMock(),
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 500),
            **common,
        ),
    ):
        await session._response_queue.put(event)

    await asyncio.wait_for(boundary_started.wait(), 1)
    await asyncio.wait_for(session.close(), 1)
    assert first_cancellation.is_set()
    assert shutdown_cancellation.is_set()
    assert session._provider_boundary_retired_tasks == set()


async def test_provider_boundary_task_overflow_fails_open_without_blocking_final():
    events: list[tuple] = []
    boundary_started: list[tuple[ProviderUtteranceKey, str]] = []
    boundary_cancelled: list[tuple[ProviderUtteranceKey, str]] = []
    release_boundaries = asyncio.Event()

    async def on_provider_endpoint(
        notification: ProviderEndpointNotification,
    ) -> None:
        if notification.phase == "boundary":
            marker = (notification.key, notification.boundary_quality)
            boundary_started.append(marker)
            try:
                await release_boundaries.wait()
            except asyncio.CancelledError:
                boundary_cancelled.append(marker)
                raise
        events.append(
            (
                "endpoint",
                notification.phase,
                notification.key,
                notification.boundary_quality,
            )
        )

    async def on_provider_final(key: ProviderUtteranceKey, text: str) -> None:
        events.append(("final", key, text))

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=on_provider_final,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    keys = [ProviderUtteranceKey(0, 0, value) for value in range(10, 20)]

    for key in keys:
        await session._response_queue.put(
            _AsrWorkerEvent(
                kind="utterance_started",
                utterance_id=key.utterance_id,
                **common,
            )
        )
    for key in keys[:8]:
        await session._response_queue.put(
            _AsrWorkerEvent(
                kind="provider_endpoint",
                utterance_id=key.utterance_id,
                boundary_quality="exact",
                audio_range=ProviderAudioRange(0, 500),
                **common,
            )
        )
    await asyncio.wait_for(_wait_until(lambda: bool(boundary_started)), 1)
    await asyncio.wait_for(session._response_queue.join(), 0.2)
    assert boundary_started == [(keys[0], "exact")]

    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="provider_endpoint",
            utterance_id=keys[8].utterance_id,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 500),
            **common,
        )
    )
    await asyncio.wait_for(session._response_queue.join(), 0.2)
    await asyncio.wait_for(
        _wait_until(lambda: len(boundary_started) == 2),
        1,
    )
    assert boundary_started == [
        (keys[0], "exact"),
        (keys[0], "unknown"),
    ]
    assert boundary_cancelled == [(keys[0], "exact")]

    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="provider_endpoint",
            utterance_id=keys[9].utterance_id,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 500),
            **common,
        )
    )
    await asyncio.wait_for(session._response_queue.join(), 0.2)
    assert boundary_started == [
        (keys[0], "exact"),
        (keys[0], "unknown"),
    ]
    assert len(session._provider_boundary_tasks) == 8
    assert {
        ProviderUtteranceKey(*key): notification.boundary_quality
        for key, notification in session._provider_endpoints.items()
    } == {key: "unknown" for key in keys}

    release_boundaries.set()
    await asyncio.wait_for(
        _wait_until(lambda: len(boundary_started) == len(keys[:8]) + 1),
        1,
    )
    assert boundary_started == [
        (keys[0], "exact"),
        *((key, "unknown") for key in keys[:8]),
    ]
    assert events == [("endpoint", "boundary", key, "unknown") for key in keys[:8]]
    assert len(session._provider_boundary_tasks) == 8

    for index, key in enumerate(keys):
        await session._response_queue.put(
            _AsrWorkerEvent(
                kind="final",
                utterance_id=key.utterance_id,
                text=f"delivered-{index}",
                **common,
            )
        )
    await _drain_session_pipelines(session)
    assert events[len(keys[:8]) :] == [
        item
        for index, key in enumerate(keys)
        for item in (
            ("endpoint", "ordered", key, "unknown"),
            ("final", key, f"delivered-{index}"),
        )
    ]

    await asyncio.wait_for(session.close(), 1)


async def test_provider_final_without_boundary_emits_unknown_before_ordered_final():
    events: list[tuple] = []
    session = _make_rich_provider_session(events, [])
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    await session._response_queue.put(
        _AsrWorkerEvent(kind="utterance_started", **common)
    )
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", text="ok", **common)
    )
    await _drain_session_pipelines(session)

    key = ProviderUtteranceKey(0, 0, 10)
    assert events == [
        ("endpoint", "boundary", key, "unknown", None),
        ("endpoint", "ordered", key, "unknown", None),
        ("final", key, "ok"),
    ]
    await session.close()


async def test_provider_conflicting_boundary_revokes_exact_authority_monotonically():
    events: list[tuple] = []
    session = _make_rich_provider_session(events, [])
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    exact = _AsrWorkerEvent(
        kind="provider_endpoint",
        boundary_quality="exact",
        audio_range=ProviderAudioRange(0, 500),
        **common,
    )
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        exact,
        exact,
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 600),
            **common,
        ),
        exact,
        _AsrWorkerEvent(kind="final", text="ok", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    key = ProviderUtteranceKey(0, 0, 10)
    assert events == [
        ("endpoint", "boundary", key, "exact", ProviderAudioRange(0, 500)),
        ("endpoint", "boundary", key, "unknown", None),
        ("endpoint", "ordered", key, "unknown", None),
        ("final", key, "ok"),
    ]
    await session.close()


async def test_provider_rich_out_of_order_finals_keep_each_keyed_boundary():
    events: list[tuple] = []
    session = _make_rich_provider_session(events, [])
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    key_one = ProviderUtteranceKey(0, 0, 10)
    key_two = ProviderUtteranceKey(0, 0, 11)
    range_one = ProviderAudioRange(0, 400)
    range_two = ProviderAudioRange(400, 900)
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            utterance_id=10,
            boundary_quality="exact",
            audio_range=range_one,
            **common,
        ),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            utterance_id=11,
            boundary_quality="exact",
            audio_range=range_two,
            **common,
        ),
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
        _AsrWorkerEvent(kind="final", utterance_id=10, text="first", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    assert events == [
        ("endpoint", "boundary", key_one, "exact", range_one),
        ("endpoint", "boundary", key_two, "exact", range_two),
        ("endpoint", "ordered", key_one, "exact", range_one),
        ("final", key_one, "first"),
        ("endpoint", "ordered", key_two, "exact", range_two),
        ("final", key_two, "second"),
    ]
    await session.close()


async def test_provider_boundary_beyond_epoch_wire_ledger_fails_open():
    events: list[tuple] = []
    session = _make_rich_provider_session(events, [])
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 100)
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0, "utterance_id": 10}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 101),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="safe", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    key = ProviderUtteranceKey(0, 0, 10)
    assert events == [
        ("endpoint", "boundary", key, "unknown", None),
        ("endpoint", "ordered", key, "unknown", None),
        ("final", key, "safe"),
    ]
    await session.close()


async def test_provider_clear_rebases_exact_range_and_rejects_old_epoch_boundary():
    events: list[tuple] = []
    session = _make_rich_provider_session(events, [])
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 1_000)
    await session.clear_audio_buffer()
    await session.stream_audio(b"\x00\x00" * 500)
    assert session._response_queue is not None
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="provider_endpoint",
            generation=0,
            buffer_epoch=0,
            utterance_id=10,
            boundary_quality="exact",
            audio_range=ProviderAudioRange(0, 100),
        )
    )
    common = {
        "generation": 0,
        "buffer_epoch": session._buffer_epoch,
        "utterance_id": 20,
    }
    for event in (
        _AsrWorkerEvent(kind="utterance_started", **common),
        _AsrWorkerEvent(
            kind="provider_endpoint",
            boundary_quality="exact",
            audio_range=ProviderAudioRange(100, 400),
            **common,
        ),
        _AsrWorkerEvent(kind="final", text="fresh", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)

    key = ProviderUtteranceKey(0, session._buffer_epoch, 20)
    rebased_range = ProviderAudioRange(1_100, 1_400)
    assert events == [
        ("endpoint", "boundary", key, "exact", rebased_range),
        ("endpoint", "ordered", key, "exact", rebased_range),
        ("final", key, "fresh"),
    ]
    await session.close()


def _make_provider_preview_session(events: list[str]) -> _RealtimeAsrSessionImpl:
    session = _make_provider_endpoint_session(events)

    async def on_partial(text: str) -> None:
        events.append(f"partial:{text}")

    session._on_partial_transcript = on_partial
    return session


async def test_provider_out_of_order_partial_waits_for_earlier_final():
    events: list[str] = []
    session = _make_provider_preview_session(events)
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        # Turn 2 streams while turn 1 still heads the order; the frontend
        # keeps one preview slot which turn 1's final unconditionally
        # clears, so these must be held back (never delivered live).
        _AsrWorkerEvent(kind="partial", utterance_id=11, text="sec", **common),
        _AsrWorkerEvent(kind="partial", utterance_id=11, text="second live", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)
    assert events == []
    # A partial for the head turn keeps flowing immediately.
    await session._response_queue.put(
        _AsrWorkerEvent(kind="partial", utterance_id=10, text="first live", **common)
    )
    await _drain_session_pipelines(session)
    assert events == ["partial:first live"]
    # Turn 1's final releases turn 2's latest (coalesced) preview after it.
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", utterance_id=10, text="first", **common)
    )
    await _drain_session_pipelines(session)
    assert events == [
        "partial:first live",
        "endpoint",
        "final:first",
        "partial:second live",
    ]
    await session._response_queue.put(
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common)
    )
    await _drain_session_pipelines(session)
    assert events == [
        "partial:first live",
        "endpoint",
        "final:first",
        "partial:second live",
        "endpoint",
        "final:second",
    ]
    await session.close()


async def test_provider_in_order_partials_flow_immediately():
    events: list[str] = []
    session = _make_provider_preview_session(events)
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="partial", utterance_id=10, text="one", **common),
        _AsrWorkerEvent(kind="final", utterance_id=10, text="first", **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(kind="partial", utterance_id=11, text="two", **common),
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)
    assert events == [
        "partial:one",
        "endpoint",
        "final:first",
        "partial:two",
        "endpoint",
        "final:second",
    ]
    await session.close()


async def test_provider_queued_partial_is_superseded_by_its_own_final():
    events: list[str] = []
    session = _make_provider_preview_session(events)
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(kind="partial", utterance_id=11, text="held", **common),
        _AsrWorkerEvent(kind="final", utterance_id=11, text="second", **common),
        _AsrWorkerEvent(kind="final", utterance_id=10, text="first", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)
    # Turn 2's final arrived before turn 1's, so its queued preview is
    # obsolete and must never resurface after its own final.
    assert events == ["endpoint", "final:first", "endpoint", "final:second"]
    assert session._pending_partials == {}
    await session.close()


async def test_provider_clear_drops_queued_partials():
    events: list[str] = []
    session = _make_provider_preview_session(events)
    await session.connect()
    assert session._response_queue is not None
    common = {"generation": 0, "buffer_epoch": 0}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=10, **common),
        _AsrWorkerEvent(kind="utterance_started", utterance_id=11, **common),
        _AsrWorkerEvent(kind="partial", utterance_id=11, text="held", **common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)
    assert events == []
    assert session._pending_partials
    await session.clear_audio_buffer()
    assert session._pending_partials == {}
    await _drain_session_pipelines(session)
    # The invalidated preview must never be delivered after the clear.
    assert events == []
    # The next epoch's previews still flow immediately.
    next_common = {"generation": 0, "buffer_epoch": session._buffer_epoch}
    for event in (
        _AsrWorkerEvent(kind="utterance_started", utterance_id=20, **next_common),
        _AsrWorkerEvent(kind="partial", utterance_id=20, text="fresh", **next_common),
    ):
        await session._response_queue.put(event)
    await _drain_session_pipelines(session)
    assert events == ["partial:fresh"]
    await session.close()


async def test_manual_partial_for_next_utterance_waits_for_committed_final():
    events: list[str] = []

    async def on_transcript(text: str) -> None:
        events.append(f"final:{text}")

    async def on_partial(text: str) -> None:
        events.append(f"partial:{text}")

    session = _RealtimeAsrSessionImpl(
        worker_fn=_recording_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=on_transcript,
        on_connection_error=AsyncMock(),
    )
    session._on_partial_transcript = on_partial
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()
    # Utterance 1 is committed and awaits its final; utterance 2 starts
    # streaming, so its previews must wait behind the pending final.
    await session.stream_audio(b"\x00\x00" * 160)
    assert session._response_queue is not None
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="partial", generation=0, buffer_epoch=0, utterance_id=2, text="next"
        )
    )
    await _drain_session_pipelines(session)
    assert events == []
    await session._response_queue.put(
        _AsrWorkerEvent(
            kind="final", generation=0, buffer_epoch=0, utterance_id=1, text="first"
        )
    )
    await _drain_session_pipelines(session)
    assert events == ["final:first", "partial:next"]
    await session.close()


async def _wait_until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0)
