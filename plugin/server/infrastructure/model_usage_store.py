"""Bounded, credential-free plugin model request history and token accounting."""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from contextvars import copy_context
from typing import Any

from plugin.server.domain.errors import ServerDomainError
from utils.file_utils import read_json_tolerating_replace

USAGE_FILENAME = "plugin_model_usage.json"
MAX_RECORDS = 1000
_write_lock = threading.RLock()
_periodic_lock = threading.Lock()
# A second bounded window catches late repeated finalizers even after eviction
# from disk. The execution service remains responsible for finalizing once.
_seen_requests: dict[str, OrderedDict[bytes, None]] = {}
_STATUSES = {"success", "error", "timeout", "cancelled"}
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _invalid() -> ServerDomainError:
    return ServerDomainError("MODEL_USAGE_INVALID", "Plugin model usage data is invalid", 500)


def _text(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise _invalid()
    return value


def _number(value: object) -> int | float:
    if type(value) not in (int, float) or value < 0:
        raise _invalid()
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise _invalid()
    return value


def _is_enum(value: object, choices: set[str]) -> bool:
    return isinstance(value, str) and value in choices


def _error_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_]{1,96}", value) is None:
        raise _invalid()
    return value


def _usage(value: object) -> dict | None:
    """Retain known nonnegative counters only, never arbitrary provider fields."""
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in _TOKEN_FIELDS if type(value.get(key)) is int and value[key] >= 0}
    details = value.get("prompt_tokens_details")
    if isinstance(details, dict) and type(details.get("cached_tokens")) is int and details["cached_tokens"] >= 0:
        result["prompt_tokens_details"] = {"cached_tokens": details["cached_tokens"]}
    return result or None


def _clean_record(raw: object) -> dict:
    if not isinstance(raw, dict) or not _is_enum(raw.get("status"), _STATUSES):
        raise _invalid()
    attempts = raw.get("attempts")
    if not isinstance(attempts, list) or len(attempts) > 2:
        raise _invalid()
    record = {
        key: _text(raw.get(key)) for key in ("request_id", "plugin_id", "usage_id", "slot_id")
    }
    record.update(
        started_at=_number(raw.get("started_at")),
        duration_ms=_number(raw.get("duration_ms")),
        status=raw["status"],
        error_code=_error_code(raw.get("error_code")),
        attempts=[],
    )
    attempt_ids = set()
    for raw_attempt in attempts:
        if not isinstance(raw_attempt, dict) or not _is_enum(raw_attempt.get("status"), _STATUSES):
            raise _invalid()
        if not _is_enum(raw_attempt.get("protocol"), {"openai_chat", "anthropic_messages"}):
            raise _invalid()
        if type(raw_attempt.get("upstream_started")) is not bool:
            raise _invalid()
        state = raw_attempt.get("usage_status")
        if not _is_enum(state, {"unknown", "partial", "reported"}):
            raise _invalid()
        attempt = {key: _text(raw_attempt.get(key)) for key in ("attempt_id", "slot_id", "model")}
        if attempt["attempt_id"] in attempt_ids:
            raise _invalid()
        attempt_ids.add(attempt["attempt_id"])
        usage = _usage(raw_attempt.get("usage")) if raw_attempt["upstream_started"] else None
        if usage is None or state == "unknown":
            state, usage = "unknown", None
        elif state == "reported" and not all(key in usage for key in _TOKEN_FIELDS):
            state = "partial"
        attempt.update(
            protocol=raw_attempt["protocol"],
            duration_ms=_number(raw_attempt.get("duration_ms")),
            status=raw_attempt["status"],
            error_code=_error_code(raw_attempt.get("error_code")),
            upstream_started=raw_attempt["upstream_started"],
            usage_status=state,
            usage=usage,
        )
        record["attempts"].append(attempt)
    return record


async def _write_in_daemon(operation: Callable, *args):
    """Wait for a serial writer without attaching blocked I/O to loop shutdown.

    Cancelling the waiter cannot stop an OS write. It may finish later, but must
    not make asyncio's default-executor shutdown join a stuck filesystem thread.
    Preserve inherited storage transaction context just as to_thread does.
    """
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    context = copy_context()

    def deliver(value, error):
        if not result.done():
            if error is None:
                result.set_result(value)
            else:
                result.set_exception(error)

    def run():
        try:
            value, error = context.run(operation, *args), None
        except Exception as exc:
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            pass  # The owning event loop has already shut down.

    threading.Thread(target=run, name="plugin-model-usage", daemon=True).start()
    return await result


class ModelUsageRecorder:
    """Read through the runtime file; never cache request data or credentials.

    Executor-owned background writes finish before record_request returns,
    independently of model responses. TokenTracker.record only updates its
    in-memory delta; its existing periodic saver handles telemetry.
    """

    def __init__(self, config_manager: Any = None, tracker_getter: Callable | None = None):
        self._config_manager = config_manager
        self._tracker_getter = tracker_getter
        self._owned_save_task: asyncio.Task | None = None
        self._owned_tracker: Any = None
        self._closed = False

    def _manager(self):
        if self._config_manager is not None:
            return self._config_manager
        from utils.config_manager import get_config_manager

        return get_config_manager()

    @staticmethod
    def _read(cm) -> list[dict]:
        try:
            data = read_json_tolerating_replace(cm.get_runtime_config_path(USAGE_FILENAME))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, UnicodeError) as exc:
            raise ServerDomainError("MODEL_USAGE_READ_FAILED", "Plugin model usage could not be read", 500) from exc
        if (
            not isinstance(data, dict)
            or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1
            or not isinstance(data.get("requests"), list)
            or len(data["requests"]) > MAX_RECORDS
        ):
            raise _invalid()
        records = [_clean_record(raw) for raw in data["requests"]]
        if len({record["request_id"] for record in records}) != len(records):
            raise _invalid()
        return records

    def _persist(self, record: dict) -> bool:
        from utils.cloudsave_runtime import cloudsave_writable_transaction

        cm = self._manager()
        path_key = os.path.normcase(str(cm.get_runtime_config_path(USAGE_FILENAME).resolve()))
        digest = hashlib.sha256(record["request_id"].encode()).digest()
        failure = None
        added = False
        # Coordinate storage-root maintenance and concurrent request finalizers.
        with _write_lock, cloudsave_writable_transaction(cm, operation="save", target=USAGE_FILENAME):
            try:
                records = self._read(cm)
                seen = _seen_requests.setdefault(path_key, OrderedDict())
                if digest not in seen and not any(item["request_id"] == record["request_id"] for item in records):
                    records.append(record)
                    records.sort(key=lambda item: item["started_at"])
                    try:
                        cm.save_json_config(USAGE_FILENAME, {"schema_version": 1, "requests": records[-MAX_RECORDS:]})
                    except OSError as exc:
                        raise ServerDomainError(
                            "MODEL_USAGE_WRITE_FAILED", "Plugin model usage could not be saved", 500
                        ) from exc
                    seen[digest] = None
                    while len(seen) > 2 * MAX_RECORDS:
                        seen.popitem(last=False)
                    added = True
            except ServerDomainError as exc:
                # Frozen domain errors cannot unwind generator context managers.
                failure = exc
        if failure is not None:
            raise failure
        return added

    def _record_tokens(self, record: dict):
        attempts = [
            item for item in record["attempts"]
            if item["upstream_started"] and item["usage_status"] == "reported"
        ]
        if not attempts:
            return None
        if self._tracker_getter is None:
            from utils.token_tracker import TokenTracker

            tracker = TokenTracker.get_instance()
        else:
            tracker = self._tracker_getter()
        for attempt in attempts:
            usage = attempt["usage"]
            tracker.record(
                model=attempt["model"],
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                cached_tokens=usage.get("prompt_tokens_details", {}).get("cached_tokens", 0),
                call_type="plugin_model",
                source="plugin_gateway",
                success=attempt["status"] == "success",
            )
        return tracker

    async def record_request(self, record: dict) -> None:
        if self._closed:
            return
        normalized = _clean_record(record)

        def write():
            if not self._persist(normalized) or self._closed:
                return None
            return self._record_tokens(normalized)

        tracker = await _write_in_daemon(write)
        if tracker is None or self._closed:
            return
        with _periodic_lock:
            existing = getattr(tracker, "_save_task", None)
            if existing is None or existing.done():
                tracker.start_periodic_save()
                self._owned_save_task = tracker._save_task
                self._owned_tracker = tracker

    async def get_usage(self, plugin_id: str | None = None, slot_id: str | None = None, limit: int = 100) -> dict:
        if type(limit) is not int or not 1 <= limit <= MAX_RECORDS:
            raise ServerDomainError("MODEL_USAGE_INVALID_LIMIT", f"Usage limit must be between 1 and {MAX_RECORDS}", 400)
        records = await asyncio.to_thread(lambda: self._read(self._manager()))
        selected = [
            record for record in records
            if (plugin_id is None or record["plugin_id"] == plugin_id)
            and (slot_id is None or record["slot_id"] == slot_id
                 or any(attempt["slot_id"] == slot_id for attempt in record["attempts"]))
        ]
        selected.sort(key=lambda record: record["started_at"], reverse=True)
        summary = {
            "window": "recent_retained",
            "retained_request_count": len(records),
            "logical_request_count": len(selected),
            "upstream_attempt_count": 0,
            "usage_counts": {"reported": 0, "partial": 0, "unknown": 0},
            "status_counts": {status: 0 for status in sorted(_STATUSES)},
            "tokens": {**dict.fromkeys(_TOKEN_FIELDS, 0), "cached_tokens": 0},
        }
        for record in selected:
            summary["status_counts"][record["status"]] += 1
            for attempt in record["attempts"]:
                if not attempt["upstream_started"] or (slot_id is not None and attempt["slot_id"] != slot_id):
                    continue
                summary["upstream_attempt_count"] += 1
                summary["usage_counts"][attempt["usage_status"]] += 1
                usage = attempt["usage"] or {}
                for key in _TOKEN_FIELDS:
                    summary["tokens"][key] += usage.get(key, 0)
                summary["tokens"]["cached_tokens"] += usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        return {
            "requests": selected[:limit],
            "summary": summary,
            "filters": {"plugin_id": plugin_id, "slot_id": slot_id},
        }

    async def aclose(self) -> None:
        """Only stop the periodic saver this recorder started, never another owner."""
        self._closed = True
        task, tracker = self._owned_save_task, self._owned_tracker
        self._owned_save_task = self._owned_tracker = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if getattr(tracker, "_save_task", None) is task:
            tracker._save_task = None
        await _write_in_daemon(tracker.save)
