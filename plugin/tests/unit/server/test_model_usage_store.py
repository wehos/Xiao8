from __future__ import annotations

import asyncio
import copy
import json
import threading
from contextlib import nullcontext
from contextvars import ContextVar

import pytest

from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure import model_usage_store as usage_store
from plugin.server.infrastructure.model_usage_store import (
    USAGE_FILENAME,
    ModelUsageRecorder,
)
from utils.file_utils import atomic_write_json

pytestmark = pytest.mark.plugin_unit


class TemporaryConfigManager:
    def __init__(self, root):
        self.root = root
        self.io_threads = []
        self.writes = 0

    def get_runtime_config_path(self, filename):
        assert filename == USAGE_FILENAME
        self.io_threads.append(threading.get_ident())
        return self.root / filename

    def save_json_config(self, filename, data):
        atomic_write_json(self.get_runtime_config_path(filename), data)
        self.writes += 1


class FakeTracker:
    def __init__(self):
        self.calls = []
        self.saves = 0
        self.starts = 0
        self._save_task = None

    def record(self, **kwargs):
        self.calls.append(kwargs)

    def start_periodic_save(self):
        self.starts += 1
        self._save_task = asyncio.create_task(asyncio.Event().wait())

    def save(self):
        self.saves += 1


def request_record(request_id="request-1", *, plugin_id="alpha", slot_id="primary", **changes):
    return {
        "request_id": request_id,
        "plugin_id": plugin_id,
        "usage_id": "analysis",
        "slot_id": slot_id,
        "started_at": 1000.0,
        "duration_ms": 100.0,
        "status": "success",
        "error_code": None,
        "attempts": [{
            "attempt_id": request_id + "-1",
            "slot_id": slot_id,
            "protocol": "openai_chat",
            "model": "test-model",
            "duration_ms": 90.0,
            "status": "success",
            "error_code": None,
            "upstream_started": True,
            "usage_status": "reported",
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
                "prompt_tokens_details": {"cached_tokens": 10},
            },
        }],
        **changes,
    }


@pytest.fixture
async def usage_env(tmp_path, monkeypatch):
    from utils import cloudsave_runtime

    transactions = []

    def transaction(cm, **kwargs):
        transactions.append(kwargs)
        return nullcontext()

    monkeypatch.setattr(cloudsave_runtime, "cloudsave_writable_transaction", transaction)
    cm = TemporaryConfigManager(tmp_path)
    tracker = FakeTracker()
    recorder = ModelUsageRecorder(cm, tracker_getter=lambda: tracker)
    yield recorder, cm, tracker, transactions
    await recorder.aclose()


async def test_empty_read_never_writes_or_starts_tracker(usage_env):
    recorder, cm, tracker, transactions = usage_env
    result = await recorder.get_usage()
    assert result["requests"] == []
    assert result["summary"]["window"] == "recent_retained"
    assert result["summary"]["logical_request_count"] == 0
    assert result["summary"]["upstream_attempt_count"] == 0
    assert not (cm.root / USAGE_FILENAME).exists()
    assert not transactions and not tracker.calls and tracker.starts == 0


async def test_record_whitelist_and_aggregate_source_never_include_secrets(usage_env):
    recorder, cm, tracker, transactions = usage_env
    record = request_record()
    secret = "sk-should-never-be-saved"
    for target in (record, record["attempts"][0], record["attempts"][0]["usage"]):
        target.update(prompt=secret, api_key=secret, token=secret, headers={"Authorization": secret}, base_url=secret)
    record["attempts"][0]["usage"]["prompt_tokens_details"]["raw"] = secret
    record["attempts"][0]["usage"]["completion_tokens_details"] = {"reasoning_tokens": 2, "raw": secret}
    await recorder.record_request(record)
    serialized = (cm.root / USAGE_FILENAME).read_text(encoding="utf-8")
    assert secret not in serialized
    assert "completion_tokens_details" not in serialized
    assert secret not in json.dumps(await recorder.get_usage())
    assert tracker.calls == [{
        "model": "test-model", "prompt_tokens": 20, "completion_tokens": 5,
        "total_tokens": 25, "cached_tokens": 10, "call_type": "plugin_model",
        "source": "plugin_gateway", "success": True,
    }]
    assert tracker.saves == 0
    assert tracker.starts == 1
    assert transactions == [{"operation": "save", "target": USAGE_FILENAME}]
    assert all(thread_id != threading.get_ident() for thread_id in cm.io_threads)


async def test_duplicate_finalizers_and_concurrent_recorders_do_not_double_count(usage_env):
    recorder, cm, tracker, _ = usage_env
    other = ModelUsageRecorder(cm, tracker_getter=lambda: tracker)
    await asyncio.gather(*(item.record_request(request_record()) for item in [recorder, other] * 20))
    assert cm.writes == 1
    assert len(tracker.calls) == 1
    assert len((await recorder.get_usage())["requests"]) == 1
    await other.aclose()


async def test_concurrent_distinct_requests_are_not_lost(usage_env):
    recorder, cm, tracker, _ = usage_env
    await asyncio.gather(*(recorder.record_request(request_record(f"r-{index}")) for index in range(40)))
    result = await recorder.get_usage()
    assert len(result["requests"]) == 40
    assert len(tracker.calls) == 40
    assert cm.writes == 40
    assert result["summary"]["tokens"]["total_tokens"] == 1000


async def test_retention_readthrough_and_bounded_process_idempotence(usage_env, monkeypatch):
    recorder, cm, tracker, _ = usage_env
    monkeypatch.setattr(usage_store, "MAX_RECORDS", 3)
    for index in range(5):
        await recorder.record_request(request_record(f"r-{index}", started_at=float(index)))
    result = await recorder.get_usage(limit=2)
    assert [r["request_id"] for r in result["requests"]] == ["r-4", "r-3"]
    assert result["summary"]["retained_request_count"] == 3
    assert result["summary"]["logical_request_count"] == 3
    assert result["summary"]["tokens"]["total_tokens"] == 75
    await recorder.record_request(request_record("r-0", started_at=0.0))
    assert len(tracker.calls) == 5
    # A fresh reader must observe the same file without shared object state.
    reader = ModelUsageRecorder(cm, tracker_getter=lambda: tracker)
    assert (await reader.get_usage(limit=2))["requests"] == result["requests"]
    await reader.record_request(request_record("r-4"))
    assert len(tracker.calls) == 5
    for index in range(5, 10):
        await recorder.record_request(request_record(f"r-{index}", started_at=float(index)))
    path_key = usage_store.os.path.normcase(str((cm.root / USAGE_FILENAME).resolve()))
    assert len(usage_store._seen_requests[path_key]) == 6


async def test_retained_ids_are_idempotent_after_in_memory_dedupe_is_empty(usage_env, monkeypatch):
    recorder, cm, tracker, _ = usage_env
    await recorder.record_request(request_record())
    monkeypatch.setattr(usage_store, "_seen_requests", {})
    reader = ModelUsageRecorder(cm, tracker_getter=lambda: tracker)
    await reader.record_request(request_record())
    assert cm.writes == 1
    assert len(tracker.calls) == 1


async def test_fallback_counts_logical_requests_and_actual_upstreams_separately(usage_env):
    recorder, _, tracker, _ = usage_env
    first = request_record()
    fallback = copy.deepcopy(first["attempts"][0])
    fallback.update(attempt_id="request-1-2", slot_id="fallback", protocol="anthropic_messages")
    first["attempts"][0].update(status="error", error_code="UPSTREAM_FAILED", usage_status="unknown", usage=None)
    first["attempts"].append(fallback)
    await recorder.record_request(first)
    second = request_record("request-2", plugin_id="beta", started_at=2000.0)
    second["attempts"][0].update(status="timeout", usage_status="partial")
    second.update(status="timeout", error_code="DEADLINE_EXCEEDED")
    await recorder.record_request(second)
    result = await recorder.get_usage(limit=1)
    assert len(result["requests"]) == 1
    summary = result["summary"]
    assert summary["logical_request_count"] == 2
    assert summary["upstream_attempt_count"] == 3
    assert summary["usage_counts"] == {"reported": 1, "partial": 1, "unknown": 1}
    assert summary["tokens"]["total_tokens"] == 50
    assert len(tracker.calls) == 1
    by_plugin = await recorder.get_usage(plugin_id="alpha")
    assert by_plugin["summary"]["upstream_attempt_count"] == 2
    by_slot = await recorder.get_usage(slot_id="fallback")
    assert by_slot["summary"]["logical_request_count"] == 1
    assert by_slot["summary"]["upstream_attempt_count"] == 1
    assert by_slot["summary"]["tokens"]["total_tokens"] == 25
    assert len(by_slot["requests"][0]["attempts"]) == 2
    assert (await recorder.get_usage(plugin_id="beta", slot_id="fallback"))["requests"] == []


@pytest.mark.parametrize("status", ["error", "timeout", "cancelled"])
async def test_no_upstream_start_never_counts_as_upstream_or_token_usage(usage_env, status):
    recorder, _, tracker, _ = usage_env
    record = request_record(status=status)
    record["attempts"][0].update(upstream_started=False, status=status)
    await recorder.record_request(record)
    result = await recorder.get_usage()
    assert result["summary"]["logical_request_count"] == 1
    assert result["summary"]["upstream_attempt_count"] == 0
    assert result["requests"][0]["attempts"][0]["usage"] is None
    assert not tracker.calls and not tracker.starts


@pytest.mark.parametrize("field,value", [
    ("prompt_tokens", -1), ("completion_tokens", True), ("total_tokens", "25"),
    ("prompt_tokens", 1.5), ("completion_tokens", None),
])
async def test_invalid_token_counters_are_omitted_and_reported_downgraded(usage_env, field, value):
    recorder, _, tracker, _ = usage_env
    record = request_record()
    record["attempts"][0]["usage"][field] = value
    await recorder.record_request(record)
    attempt = (await recorder.get_usage())["requests"][0]["attempts"][0]
    assert field not in attempt["usage"]
    assert attempt["usage_status"] == "partial"
    assert not tracker.calls


async def test_invalid_cache_counter_is_never_persisted(usage_env):
    recorder, _, tracker, _ = usage_env
    record = request_record()
    record["attempts"][0]["usage"]["prompt_tokens_details"]["cached_tokens"] = False
    await recorder.record_request(record)
    assert "prompt_tokens_details" not in (await recorder.get_usage())["requests"][0]["attempts"][0]["usage"]
    assert tracker.calls[0]["cached_tokens"] == 0


@pytest.mark.parametrize("original", [
    "{invalid", '[]', '{"schema_version":2,"requests":[]}',
    '{"schema_version":1,"requests":[{"api_key":"secret-preserve"}]}',
])
async def test_invalid_disk_history_is_preserved_and_errors_are_safe(usage_env, original):
    recorder, cm, tracker, _ = usage_env
    path = cm.root / USAGE_FILENAME
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ServerDomainError) as error:
        await recorder.record_request(request_record())
    assert "secret-preserve" not in str(error.value)
    assert path.read_text(encoding="utf-8") == original
    assert not tracker.calls
    with pytest.raises(ServerDomainError):
        await recorder.get_usage()


async def test_write_failure_does_not_count_and_can_be_retried(usage_env, monkeypatch):
    recorder, cm, tracker, _ = usage_env
    original = cm.save_json_config

    def fail(*_args):
        raise OSError("secret filesystem info")

    monkeypatch.setattr(cm, "save_json_config", fail)
    with pytest.raises(ServerDomainError) as error:
        await recorder.record_request(request_record())
    assert error.value.code == "MODEL_USAGE_WRITE_FAILED"
    assert "secret" not in str(error.value)
    assert not tracker.calls
    monkeypatch.setattr(cm, "save_json_config", original)
    await recorder.record_request(request_record())
    assert len(tracker.calls) == 1


async def test_storage_maintenance_blocks_mutation_and_accounting(usage_env, monkeypatch):
    from utils import cloudsave_runtime

    recorder, cm, tracker, _ = usage_env

    def blocked(*_args, **_kwargs):
        raise cloudsave_runtime.MaintenanceModeError("maintenance", operation="save", target=USAGE_FILENAME)

    monkeypatch.setattr(cloudsave_runtime, "cloudsave_writable_transaction", blocked)
    with pytest.raises(cloudsave_runtime.MaintenanceModeError):
        await recorder.record_request(request_record())
    assert not (cm.root / USAGE_FILENAME).exists()
    assert not tracker.calls


@pytest.mark.parametrize("changes", [
    {"status": []}, {"started_at": float("nan")}, {"duration_ms": float("inf")},
    {"request_id": ""}, {"error_code": "upstream error includes secret"},
    {"attempts": [{"api_key": "secret"}]},
])
async def test_malformed_request_is_rejected_before_storage(usage_env, changes):
    recorder, cm, tracker, _ = usage_env
    with pytest.raises(ServerDomainError) as error:
        await recorder.record_request(request_record(**changes))
    assert error.value.code == "MODEL_USAGE_INVALID"
    assert "secret" not in str(error.value)
    assert not (cm.root / USAGE_FILENAME).exists()
    assert not tracker.calls


async def test_existing_main_tracker_saver_is_not_replaced_or_stopped(usage_env):
    recorder, _, tracker, _ = usage_env
    external_task = asyncio.create_task(asyncio.Event().wait())
    tracker._save_task = external_task
    try:
        await recorder.record_request(request_record())
        await recorder.aclose()
        assert tracker._save_task is external_task
        assert not external_task.done()
        assert tracker.starts == 0
        assert tracker.saves == 0
    finally:
        external_task.cancel()
        await asyncio.gather(external_task, return_exceptions=True)


async def test_owned_tracker_saver_is_closed_and_flushed_once(usage_env):
    recorder, _, tracker, _ = usage_env
    await recorder.record_request(request_record())
    task = tracker._save_task
    assert task is not None and not task.done()
    await recorder.aclose()
    assert task.done()
    assert tracker._save_task is None
    assert tracker.saves == 1
    await recorder.aclose()
    assert tracker.saves == 1


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "10"])
async def test_query_limit_validation(usage_env, limit):
    recorder, _, _, _ = usage_env
    with pytest.raises(ServerDomainError) as error:
        await recorder.get_usage(limit=limit)
    assert error.value.code == "MODEL_USAGE_INVALID_LIMIT"


async def test_daemon_writer_preserves_storage_context_and_ignores_cancelled_result():
    marker = ContextVar("storage_transaction_marker", default=None)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    seen = []
    loop = asyncio.get_running_loop()
    errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    def operation():
        seen.append((marker.get(), threading.current_thread().daemon))
        entered.set()
        release.wait(2)
        completed.set()
        return "late result"

    token = marker.set("inherited-root-transaction")
    try:
        task = asyncio.create_task(usage_store._write_in_daemon(operation))
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        async with asyncio.timeout(1):
            while not completed.is_set():
                await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        assert seen == [("inherited-root-transaction", True)]
        assert errors == []  # No InvalidStateError publishing to a cancelled future.
    finally:
        release.set()
        marker.reset(token)
        loop.set_exception_handler(previous_handler)


def test_blocked_accounting_io_does_not_block_asyncio_run_exit(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from textwrap import dedent

    source = dedent('''
        import asyncio
        import threading
        from plugin.server.domain.model_config import ModelSlot
        from plugin.server.infrastructure.model_usage_store import ModelUsageRecorder
        from plugin.server.model_gateway.execution import ModelExecutor, ResolvedModelCall

        entered = threading.Event()
        class StuckRecorder(ModelUsageRecorder):
            def _persist(self, record):
                entered.set()
                threading.Event().wait()

        class Gateway:
            async def complete(self, slot, body, *, observation):
                return {"ok": True}

        async def main():
            recorder = StuckRecorder(tracker_getter=lambda: None)
            executor = ModelExecutor(Gateway(), recorder, accounting_flush_timeout_seconds=0.02)
            slot = ModelSlot(name="test", protocol="openai_chat", base_url="https://unused.test/v1", model="test")
            result = await executor.complete(ResolvedModelCall("test", "analysis", "slot", slot), {})
            assert result == {"ok": True}
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0)
            await executor.aclose()
        asyncio.run(main())
        print("ACCOUNTING_LOOP_EXITED")
    ''')
    env = {**os.environ, "NEKO_STORAGE_SELECTED_ROOT": str(tmp_path / "runtime")}
    result = subprocess.run(
        [sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[4],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "ACCOUNTING_LOOP_EXITED" in result.stdout
