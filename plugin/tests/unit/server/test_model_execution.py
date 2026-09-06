from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from plugin.server.domain.model_config import ModelSlot
from plugin.server.model_gateway.errors import ModelGatewayError
from plugin.server.model_gateway.execution import ModelExecutor, ResolvedModelCall
from plugin.server.model_gateway.request import prepare_chat_request


USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
DONE = b"data: [DONE]\n\n"


def slot(model="primary", **updates):
    return ModelSlot.model_validate({
        "name": model, "protocol": "openai_chat", "base_url": "https://models.test/v1",
        "model": model, "capabilities": ["text", "streaming"], **updates,
    })


def call(primary=None, fallback=None):
    return ResolvedModelCall(
        "test_plugin", "analysis", "slot_" + "a" * 32, primary or slot(),
        "slot_" + "b" * 32 if fallback is not None else None, fallback,
    )


def body(**updates):
    return {"model": "analysis", "messages": [{"role": "user", "content": "hello"}], **updates}


class Recorder:
    def __init__(self):
        self.requests = []
        self.entered = asyncio.Event()
        self.gate = None
        self.error = None

    async def record_request(self, request):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        self.requests.append(deepcopy(request))


class Gateway:
    def __init__(self, complete=None, stream=None):
        self.on_complete = complete
        self.on_stream = stream
        self.calls = []
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def complete(self, model_slot, request, *, observation):
        prepare_chat_request(model_slot, request)
        self.calls.append(model_slot.model)
        observation.upstream_started = True
        self.started.set()
        try:
            if self.on_complete is not None:
                return await self.on_complete(model_slot, request, observation)
            observation.observe(USAGE, reported=True)
            return {"model": request["model"], "usage": USAGE}
        finally:
            self.closed.set()

    async def stream(self, model_slot, request, *, observation):
        prepare_chat_request(model_slot, request)
        self.calls.append(model_slot.model)
        observation.upstream_started = True
        self.started.set()
        try:
            if self.on_stream is not None:
                upstream = self.on_stream(model_slot, request, observation)
                try:
                    async for chunk in upstream:
                        yield chunk
                finally:
                    await upstream.aclose()
            else:
                yield b"first"
                observation.observe(USAGE, reported=True)
                yield DONE
        finally:
            self.closed.set()


async def stalled(*_args):
    await asyncio.Event().wait()


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_success_has_one_request_and_one_upstream_attempt():
    recorder = Recorder()
    executor = ModelExecutor(Gateway(), recorder)
    result = await executor.complete(call(), body())
    assert result["usage"] == USAGE
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request["plugin_id"] == "test_plugin"
    assert request["usage_id"] == "analysis"
    assert request["status"] == "success"
    assert request["error_code"] is None
    assert isinstance(request["started_at"], float)
    assert request["started_at"] > 0
    assert request["duration_ms"] >= 0
    assert len(request["request_id"]) == 32
    attempt, = request["attempts"]
    assert attempt["upstream_started"] is True
    assert attempt["usage"] == USAGE
    assert attempt["usage_status"] == "reported"
    assert attempt["model"] == "primary"
    await executor.aclose()


@pytest.mark.parametrize("code", [
    "upstream_connection_error", "upstream_timeout", "upstream_rate_limited", "upstream_error",
])
async def test_only_one_fallback_attempt_and_each_usage_is_retained(code):
    async def execute(model_slot, _request, observation):
        observation.observe(USAGE, reported=True)
        if model_slot.model == "primary":
            raise ModelGatewayError(code, "failure", 502)
        return {"ok": True}

    recorder = Recorder()
    gateway = Gateway(complete=execute)
    executor = ModelExecutor(gateway, recorder)
    assert await executor.complete(call(fallback=slot("backup")), body()) == {"ok": True}
    assert gateway.calls == ["primary", "backup"]
    request, = recorder.requests
    assert request["status"] == "success"
    assert len(request["attempts"]) == 2
    assert request["attempts"][0]["error_code"] == code
    assert all(attempt["usage"] == USAGE for attempt in request["attempts"])
    assert request["attempts"][0]["attempt_id"] != request["attempts"][1]["attempt_id"]


@pytest.mark.parametrize("code", [
    "upstream_authentication_failed", "upstream_request_rejected", "invalid_upstream_response",
    "incomplete_upstream_stream", "unsupported_parameter", "upstream_redirect_rejected",
])
async def test_permanent_errors_do_not_fallback(code):
    async def fail(*_args):
        raise ModelGatewayError(code, "failure", 502)

    recorder = Recorder()
    gateway = Gateway(complete=fail)
    with pytest.raises(ModelGatewayError, match="failure"):
        await ModelExecutor(gateway, recorder).complete(call(fallback=slot("backup")), body())
    assert gateway.calls == ["primary"]
    assert recorder.requests[0]["error_code"] == code


async def test_fallback_failure_is_final_without_following_its_fallback():
    async def fail(*_args):
        raise ModelGatewayError("upstream_error", "failure", 502)

    gateway = Gateway(complete=fail)
    recorder = Recorder()
    fallback = slot("backup", fallback_slot_id="slot_" + "c" * 32)
    with pytest.raises(ModelGatewayError):
        await ModelExecutor(gateway, recorder).complete(call(fallback=fallback), body())
    assert gateway.calls == ["primary", "backup"]
    assert len(recorder.requests[0]["attempts"]) == 2


async def test_validation_failure_is_recorded_without_upstream_usage():
    recorder = Recorder()
    gateway = Gateway()
    with pytest.raises(ModelGatewayError):
        await ModelExecutor(gateway, recorder).complete(call(fallback=slot("backup")), body(audio={}))
    assert not gateway.calls
    attempt, = recorder.requests[0]["attempts"]
    assert attempt["upstream_started"] is False
    assert attempt["usage_status"] == "unknown"
    assert attempt["usage"] is None


async def test_fallback_cannot_drop_declared_slot_capabilities():
    async def fail(*_args):
        raise ModelGatewayError("upstream_error", "failure", 502)

    recorder = Recorder()
    gateway = Gateway(complete=fail)
    resolved = call(slot(capabilities=["text", "image_input"]), slot("backup", capabilities=["text"]))
    with pytest.raises(ModelGatewayError) as error:
        await ModelExecutor(gateway, recorder).complete(resolved, body())
    assert error.value.code == "model_capability_mismatch"
    assert gateway.calls == ["primary"]


async def test_total_deadline_covers_upstream_and_records_partial_usage():
    async def timeout(_slot, _request, observation):
        observation.observe(USAGE)
        await stalled()

    recorder = Recorder()
    gateway = Gateway(complete=timeout)
    with pytest.raises(ModelGatewayError) as error:
        await ModelExecutor(gateway, recorder).complete(call(slot(timeout_seconds=0.04)), body())
    assert error.value.code == "gateway_timeout"
    assert error.value.status_code == 504
    assert gateway.closed.is_set()
    request, = recorder.requests
    assert request["status"] == "timeout"
    attempt, = request["attempts"]
    assert attempt["status"] == "timeout"
    assert attempt["error_code"] == "gateway_timeout"
    assert attempt["usage"] == USAGE
    assert attempt["usage_status"] == "partial"


async def test_fallback_shares_primary_deadline():
    async def execute(model_slot, _request, _observation):
        if model_slot.model == "primary":
            await asyncio.sleep(0.04)
            raise ModelGatewayError("upstream_timeout", "failure", 504)
        await stalled()

    recorder = Recorder()
    executor = ModelExecutor(Gateway(complete=execute), recorder)
    with pytest.raises(ModelGatewayError) as error:
        await executor.complete(call(slot(timeout_seconds=0.09), slot("backup", timeout_seconds=10.0)), body())
    assert error.value.code == "gateway_timeout"
    request, = recorder.requests
    assert request["duration_ms"] < 500
    assert len(request["attempts"]) == 2
    assert request["attempts"][1]["status"] == "timeout"


async def test_queue_timeout_and_capacity_rejection_have_no_attempts():
    gateway = Gateway(complete=stalled)
    recorder = Recorder()
    executor = ModelExecutor(gateway, recorder, max_active=1, max_waiting=1)
    active = asyncio.create_task(executor.complete(call(), body()))
    await gateway.started.wait()
    waiting = asyncio.create_task(executor.complete(call(slot(timeout_seconds=0.05)), body()))
    await wait_until(lambda: executor._admitted == 2)
    with pytest.raises(ModelGatewayError) as busy:
        await executor.complete(call(), body())
    assert busy.value.code == "model_gateway_busy"
    assert busy.value.status_code == 429
    with pytest.raises(ModelGatewayError) as timeout:
        await waiting
    assert timeout.value.code == "gateway_timeout"
    assert gateway.calls == ["primary"]
    assert all(request["attempts"] == [] for request in recorder.requests)
    await executor.aclose()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert recorder.requests[-1]["status"] == "cancelled"
    assert executor._admitted == 0


async def test_close_cancels_active_and_queued_and_rejects_new_requests():
    gateway = Gateway(complete=stalled)
    recorder = Recorder()
    executor = ModelExecutor(gateway, recorder, max_active=1, max_waiting=1)
    active = asyncio.create_task(executor.complete(call(), body()))
    await gateway.started.wait()
    waiting = asyncio.create_task(executor.complete(call(), body()))
    await wait_until(lambda: executor._admitted == 2)
    await executor.aclose()
    for task in (active, waiting):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(recorder.requests) == 2
    assert sorted(len(item["attempts"]) for item in recorder.requests) == [0, 1]
    assert all(item["status"] == "cancelled" for item in recorder.requests)
    with pytest.raises(ModelGatewayError) as error:
        await executor.complete(call(), body())
    assert error.value.code == "model_gateway_unavailable"


async def test_close_before_runner_starts_still_accounts_for_the_request():
    recorder = Recorder()
    gateway = Gateway()
    executor = ModelExecutor(gateway, recorder)
    consumer = asyncio.create_task(executor.complete(call(), body()))
    # complete() schedules its owned runner after this task's next turn.
    await asyncio.sleep(0)
    await executor.aclose()
    with pytest.raises(ModelGatewayError) as error:
        await consumer
    assert error.value.code == "model_gateway_unavailable"
    assert not gateway.calls
    assert len(recorder.requests) == 1
    assert recorder.requests[0]["attempts"] == []


async def test_complete_cancellation_waits_for_single_record_even_if_cancelled_twice():
    recorder = Recorder()
    recorder.gate = asyncio.Event()
    gateway = Gateway(complete=stalled)
    executor = ModelExecutor(gateway, recorder)
    consumer = asyncio.create_task(executor.complete(call(), body()))
    await gateway.started.wait()
    consumer.cancel()
    await recorder.entered.wait()
    consumer.cancel()
    await asyncio.sleep(0)
    assert not consumer.done()
    recorder.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert len(recorder.requests) == 1
    assert recorder.requests[0]["status"] == "cancelled"
    assert gateway.closed.is_set()
    await executor.aclose()


@pytest.mark.parametrize("failure", [RuntimeError("ledger unavailable"), asyncio.CancelledError()])
async def test_recording_failure_cannot_change_model_result(failure):
    recorder = Recorder()
    recorder.error = failure
    result = await ModelExecutor(Gateway(), recorder).complete(call(), body())
    assert result["usage"] == USAGE


async def test_stream_prefetch_and_body_consumption_can_use_different_tasks():
    recorder = Recorder()
    executor = ModelExecutor(Gateway(), recorder)
    stream = executor.stream(call(), body(stream=True))
    assert await asyncio.create_task(anext(stream)) == b"first"

    async def consume():
        return [chunk async for chunk in stream]

    assert await asyncio.create_task(consume()) == [DONE]
    assert recorder.requests[0]["status"] == "success"


async def test_done_is_delivered_only_after_successful_accounting():
    recorder = Recorder()
    recorder.gate = asyncio.Event()
    gateway = Gateway()
    stream = ModelExecutor(gateway, recorder).stream(call(), body(stream=True))
    assert await anext(stream) == b"first"
    terminal = asyncio.create_task(anext(stream))
    await recorder.entered.wait()
    assert not terminal.done()
    assert gateway.closed.is_set()
    recorder.gate.set()
    assert await terminal == DONE
    await stream.aclose()
    assert len(recorder.requests) == 1
    assert recorder.requests[0]["status"] == "success"


async def test_stream_fallback_discards_primary_chunks_not_yet_delivered():
    async def execute(model_slot, _request, observation):
        if model_slot.model == "primary":
            # No await: the producer fails before the waiting consumer wakes.
            yield b"discarded-primary"
            raise ModelGatewayError("upstream_error", "failure", 502)
        yield b"backup"
        observation.observe(USAGE, reported=True)
        yield DONE

    recorder = Recorder()
    gateway = Gateway(stream=execute)
    executor = ModelExecutor(gateway, recorder)
    output = [chunk async for chunk in executor.stream(call(fallback=slot("backup")), body(stream=True))]
    assert output == [b"backup", DONE]
    assert gateway.calls == ["primary", "backup"]
    assert len(recorder.requests[0]["attempts"]) == 2


async def test_stream_never_falls_back_after_prefetch_delivered_a_chunk():
    proceed = asyncio.Event()

    async def execute(_slot, _request, observation):
        yield b"primary"
        await proceed.wait()
        observation.observe(USAGE)
        raise ModelGatewayError("upstream_connection_error", "failure", 502)

    recorder = Recorder()
    gateway = Gateway(stream=execute)
    stream = ModelExecutor(gateway, recorder).stream(call(fallback=slot("backup")), body(stream=True))
    assert await anext(stream) == b"primary"
    proceed.set()
    with pytest.raises(ModelGatewayError) as error:
        await anext(stream)
    assert error.value.code == "upstream_connection_error"
    assert gateway.calls == ["primary"]
    assert recorder.requests[0]["attempts"][0]["usage_status"] == "partial"


async def test_backpressure_counts_toward_deadline_and_does_not_deadlock_error():
    async def execute(*_args):
        yield b"first"
        yield b"queued"
        yield b"blocked"

    recorder = Recorder()
    gateway = Gateway(stream=execute)
    stream = ModelExecutor(gateway, recorder).stream(call(slot(timeout_seconds=0.04)), body(stream=True))
    assert await anext(stream) == b"first"
    await recorder.entered.wait()
    assert await anext(stream) == b"queued"
    with pytest.raises(ModelGatewayError) as error:
        await anext(stream)
    assert error.value.code == "gateway_timeout"
    assert recorder.requests[0]["status"] == "timeout"
    assert gateway.closed.is_set()


async def test_stream_close_cancels_stalled_upstream_and_keeps_partial_usage():
    async def execute(_slot, _request, observation):
        observation.observe(USAGE)
        yield b"first"
        await stalled()

    recorder = Recorder()
    gateway = Gateway(stream=execute)
    stream = ModelExecutor(gateway, recorder).stream(call(), body(stream=True))
    assert await anext(stream) == b"first"
    await stream.aclose()
    assert gateway.closed.is_set()
    assert len(recorder.requests) == 1
    assert recorder.requests[0]["status"] == "cancelled"
    assert recorder.requests[0]["attempts"][0]["usage_status"] == "partial"


async def test_empty_bytes_do_not_prevent_fallback():
    async def execute(model_slot, _request, _observation):
        if model_slot.model == "primary":
            yield b""
            raise ModelGatewayError("upstream_error", "failure", 502)
        yield b"backup"
        yield DONE

    recorder = Recorder()
    stream = ModelExecutor(Gateway(stream=execute), recorder).stream(call(fallback=slot("backup")), body(stream=True))
    assert [chunk async for chunk in stream] == [b"backup", DONE]


@pytest.mark.parametrize("active, waiting", [(0, 1), (-1, 1), (1, -1), (True, 1), (1, 1.5)])
def test_invalid_concurrency_limits_are_rejected(active, waiting):
    with pytest.raises(ValueError):
        ModelExecutor(None, None, max_active=active, max_waiting=waiting)
