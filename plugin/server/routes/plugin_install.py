from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from plugin._types.models import RunCreateRequest
from plugin.logging_config import get_logger
from plugin.server import install_registry
from plugin.server.install_registry import (
    InstallKindRegistration,
    InstallPluginRegistration,
    normalize_registered_plugin_id as _normalize_registered_plugin_id,
    register_install_plugin,
    register_tutorial_migration_hook,
)
from plugin.sdk.shared.core.base_runtime import resolve_runtime_data_root
from ._install_task_store import (
    INSTALL_TERMINAL_STATUSES,
    build_install_task_state,
    load_install_task_state,
    load_latest_install_task_state,
    update_install_task_state,
)
from plugin.server.application.runs import RunService
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.error_mapping import raise_http_from_domain

router = APIRouter(tags=["plugin-install"])
logger = get_logger("server.plugin_install")
run_service = RunService()

_BLOCKING_IO_TIMEOUT_SECONDS = 15.0
_STALE_INSTALL_STATUS = "failed"
_STALE_INSTALL_PHASE = "failed"
_STALE_INSTALL_MESSAGE_TEMPLATE = (
    "{label} install task was interrupted before completion; its backend run "
    "record no longer exists. Previous phase: {previous_phase}. Please start "
    "the install again."
)
_INSTALL_STATE_PERSIST_FAILED = "Install task state could not be saved locally."
_INSTALL_STREAM_READ_FAILED = "Install task state could not be read. Please retry the install from the setup UI."
_LOCAL_STATE_RETRY_HINT = (
    "The backend run was created, but local install state could not be saved. "
    "Retry status with the returned run_id or start the install again if the UI cannot restore it."
)
_ALLOWED_UI_LOCALES = {"zh-CN", "zh-TW", "en", "ja", "ru", "ko", "es", "pt"}
_INSTALL_KIND_LABELS = {
    "textractor": "Textractor",
    "rapidocr_models": "RapidOCR Models",
    "tesseract": "Tesseract",
}
_LEGACY_READ_ONLY_INSTALL_KINDS = {("study_companion", "tesseract")}


async def _run_blocking(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=_BLOCKING_IO_TIMEOUT_SECONDS,
    )


async def _get_plugin_registration(plugin_id: str) -> InstallPluginRegistration:
    try:
        registration = await _run_blocking(
            install_registry.get_install_plugin_registration,
            plugin_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Plugin has no install API") from exc
    if registration is None:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' has no install API")
    return registration


def _ensure_tutorial_enabled(registration: InstallPluginRegistration) -> None:
    if not registration.tutorial_enabled:
        raise HTTPException(status_code=404, detail=f"Plugin '{registration.plugin_id}' has no tutorial API")


class InstallStartPayload(BaseModel):
    force: bool = False


@router.get("/plugin/{plugin_id}/ui-api/locale")
async def get_plugin_ui_locale(plugin_id: str) -> JSONResponse:
    await _get_plugin_registration(plugin_id)
    try:
        from utils.language_utils import get_global_language_full

        locale = _normalize_ui_locale(str(get_global_language_full()))
    except Exception:
        logger.warning("plugin ui locale detection failed; falling back to en", exc_info=True)
        locale = "en"
    return JSONResponse({"locale": locale})


@router.get("/plugin/{plugin_id}/ui-api/i18n/ui/{locale}.json")
async def get_plugin_ui_i18n(plugin_id: str, locale: str) -> Response:
    registration = await _get_plugin_registration(plugin_id)
    if registration.ui_i18n_dir is None or registration.config_path is None:
        return Response(status_code=404)
    normalized = str(locale or "").strip()
    if ".." in normalized or "/" in normalized or "\\" in normalized:
        return Response(status_code=404)
    if normalized not in _ALLOWED_UI_LOCALES:
        return Response(status_code=404)

    payload = await _run_blocking(
        _read_ui_locale_payload,
        registration.config_path.parent,
        registration.ui_i18n_dir,
        normalized,
    )
    if payload is None:
        return Response(status_code=404)
    return Response(content=payload, media_type="application/json")


def _read_ui_locale_payload(
    plugin_root: Path,
    base_path: Path,
    locale: str,
) -> bytes | None:
    """Read one locale through a verified handle so path swaps cannot escape."""

    file_descriptor = -1
    try:
        if not plugin_root.is_absolute():
            return None
        base_dir = base_path.resolve(strict=True)
        base_dir.relative_to(plugin_root)
        if not base_dir.is_dir():
            return None
        candidate = base_dir / f"{locale}.json"
        open_flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_descriptor = os.open(candidate, open_flags)

        opened_metadata = os.fstat(file_descriptor)
        current_metadata = os.stat(candidate, follow_symlinks=False)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or not stat.S_ISREG(current_metadata.st_mode)
            or bool(getattr(current_metadata, "st_file_attributes", 0) & reparse_attribute)
            or not os.path.samestat(opened_metadata, current_metadata)
        ):
            return None

        resolved = candidate.resolve(strict=True)
        resolved.relative_to(plugin_root)
        if not os.path.samestat(opened_metadata, resolved.stat()):
            return None

        with os.fdopen(file_descriptor, "rb") as file_obj:
            file_descriptor = -1
            return file_obj.read()
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _normalize_ui_locale(locale: str) -> str:
    normalized = str(locale or "").strip().replace("_", "-").lower()
    if normalized in {"zh-tw", "zh-hant", "zh-hk", "zh-mo"}:
        return "zh-TW"
    if normalized == "zh" or normalized.startswith("zh-"):
        return "zh-CN"
    if normalized.startswith("en"):
        return "en"
    if normalized.startswith("ja"):
        return "ja"
    if normalized.startswith("ru"):
        return "ru"
    if normalized.startswith("ko"):
        return "ko"
    if normalized.startswith("es"):
        return "es"
    if normalized.startswith("pt"):
        return "pt"
    return "zh-CN"


def _get_install_kind_spec(
    kind: str,
    *,
    registration: InstallPluginRegistration,
    allow_legacy_read: bool = False,
) -> dict[str, Any]:
    normalized = str(kind or "").strip().lower()
    # rapidocr + dxcam used to live here as runtime-pip-install entries; both
    # are now bundled into the main program. textractor still needs runtime
    # install. rapidocr_models is a model-pack download for non-bundled (lang,
    # version) combos like japan + PP-OCRv4.
    spec = registration.install_kinds.get(normalized)
    if spec is None:
        label = _INSTALL_KIND_LABELS.get(normalized, normalized or str(kind))
        if allow_legacy_read and (registration.plugin_id, normalized) in _LEGACY_READ_ONLY_INSTALL_KINDS:
            return {"kind": normalized, "label": label}
        raise HTTPException(
            status_code=404,
            detail=f"{label} install is not supported by {registration.plugin_id}",
        )
    return {
        "kind": normalized,
        "entry_id": spec.entry_id,
        "label": spec.label,
        "queued_message": spec.queued_message,
        "entry_timeout": spec.entry_timeout,
    }


def _run_to_install_status(run_status: str) -> str:
    mapping = {
        "queued": "queued",
        "running": "running",
        "cancel_requested": "canceled",
        "canceled": "canceled",
        "succeeded": "completed",
        "failed": "failed",
        "timeout": "failed",
    }
    return mapping.get(run_status, "queued")


def _install_state_from_run(run_record, *, plugin_id: str, kind: str) -> dict[str, object]:
    metrics = dict(getattr(run_record, "metrics", {}) or {})
    status = _run_to_install_status(str(getattr(run_record, "status", "") or "queued"))
    phase = str(getattr(run_record, "stage", "") or status)
    message = str(getattr(run_record, "message", "") or "")
    progress = getattr(run_record, "progress", None)
    run_error = getattr(run_record, "error", None)
    error_message = ""
    if run_error is not None:
        error_message = str(getattr(run_error, "message", "") or "")
    payload = build_install_task_state(
        task_id=str(getattr(run_record, "task_id", None) or getattr(run_record, "run_id")),
        run_id=str(getattr(run_record, "run_id")),
        plugin_id=plugin_id,
        kind=kind,
        status=status,
        phase=phase,
        message=message,
        progress=float(progress) if isinstance(progress, (int, float)) else 0.0,
        downloaded_bytes=int(metrics.get("downloaded_bytes") or 0),
        total_bytes=int(metrics.get("total_bytes") or 0),
        resume_from=int(metrics.get("resume_from") or 0),
        release_name=str(metrics.get("release_name") or ""),
        asset_name=str(metrics.get("asset_name") or ""),
        target_dir=str(metrics.get("target_dir") or ""),
        detected_path=str(metrics.get("detected_path") or ""),
        error=error_message,
    )
    payload["started_at"] = getattr(run_record, "started_at", None) or payload["started_at"]
    payload["updated_at"] = getattr(run_record, "updated_at", None) or payload["updated_at"]
    payload["completed_at"] = getattr(run_record, "finished_at", None) or payload.get("completed_at")
    return payload


def _persist_install_payload(
    task_id: str,
    *,
    plugin_id: str,
    kind: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return update_install_task_state(
        task_id,
        kind=kind,
        plugin_id=plugin_id,
        run_id=str(payload.get("run_id") or task_id),
        status=str(payload.get("status") or "queued"),
        phase=str(payload.get("phase") or payload.get("status") or "queued"),
        message=str(payload.get("message") or ""),
        progress=float(payload.get("progress") or 0.0),
        downloaded_bytes=int(payload.get("downloaded_bytes") or 0),
        total_bytes=int(payload.get("total_bytes") or 0),
        resume_from=int(payload.get("resume_from") or 0),
        release_name=str(payload.get("release_name") or ""),
        asset_name=str(payload.get("asset_name") or ""),
        target_dir=str(payload.get("target_dir") or ""),
        detected_path=str(payload.get("detected_path") or ""),
        error=str(payload.get("error") or ""),
    )


def _persist_terminal_install_payload(
    task_id: str,
    *,
    plugin_id: str,
    kind: str,
    payload: dict[str, object],
) -> dict[str, object]:
    try:
        return _persist_install_payload(
            task_id,
            plugin_id=plugin_id,
            kind=kind,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 - terminal run state should still reach clients.
        logger.warning(
            "failed to persist terminal {} install task state: task_id={}",
            kind,
            task_id,
            exc_info=True,
        )
        fallback_payload = dict(payload)
        fallback_payload["local_save_failed"] = True
        return fallback_payload


def _mark_stale_install_task(
    task_id: str,
    *,
    plugin_id: str,
    kind: str,
    label: str,
    payload: dict[str, object],
) -> dict[str, object]:
    previous_phase = str(payload.get("phase") or payload.get("status") or "queued")
    error_message = _STALE_INSTALL_MESSAGE_TEMPLATE.format(
        label=label,
        previous_phase=previous_phase,
    )
    logger.warning(
        "marking stale {} install task as failed: task_id={}, previous_phase={}",
        kind,
        task_id,
        previous_phase,
    )
    stale_payload = dict(payload)
    stale_payload.update(
        {
            "task_id": task_id,
            "run_id": str(payload.get("run_id") or task_id),
            "kind": kind,
            "status": _STALE_INSTALL_STATUS,
            "phase": _STALE_INSTALL_PHASE,
            "message": error_message,
            "error": error_message,
            "completed_at": time.time(),
        }
    )
    try:
        return _persist_install_payload(task_id, plugin_id=plugin_id, kind=kind, payload=stale_payload)
    except Exception:  # noqa: BLE001 - stale state should still reach the client.
        logger.warning(
            "failed to persist stale {} install task failure: task_id={}",
            kind,
            task_id,
            exc_info=True,
        )
        stale_payload["local_save_failed"] = True
        return stale_payload


def _queued_install_task_state(
    *,
    task_id: str,
    plugin_id: str,
    spec: Mapping[str, Any],
) -> dict[str, object]:
    return build_install_task_state(
        task_id=task_id,
        kind=str(spec["kind"]),
        plugin_id=plugin_id,
        run_id=task_id,
        status="queued",
        phase="queued",
        message=str(spec["queued_message"]),
        progress=0.0,
    )


def _install_stream_error_payload(
    task_id: str,
    *,
    plugin_id: str,
    kind: str,
    label: str,
    error: BaseException,
) -> dict[str, object]:
    error_text = str(error or "").strip() or type(error).__name__
    return build_install_task_state(
        task_id=task_id,
        kind=kind,
        plugin_id=plugin_id,
        run_id=task_id,
        status="failed",
        phase="failed",
        message=_INSTALL_STREAM_READ_FAILED,
        progress=0.0,
        error=f"{label}: {error_text}",
        extra={"stream_error": True},
    )


def _resolve_install_task_payload(
    task_id: str,
    *,
    plugin_id: str,
    kind: str,
    label: str,
) -> dict[str, object]:
    task_id = (task_id or "").strip()
    if not task_id or ".." in task_id or "/" in task_id or "\\" in task_id:
        raise HTTPException(status_code=400, detail=f"Invalid {label} install task_id")
    state_payload = load_install_task_state(task_id, kind=kind, plugin_id=plugin_id)

    # Short-circuit: persisted terminal states don't need a live run lookup.
    if state_payload is not None:
        state_status = str(state_payload.get("status") or "")
        if state_status in INSTALL_TERMINAL_STATUSES:
            return dict(state_payload)

    run_missing = False
    try:
        run_record = run_service.get_run(task_id)
    except ServerDomainError as error:
        if error.code == "RUN_NOT_FOUND":
            run_record = None
            run_missing = True
        else:
            raise_http_from_domain(error, logger=logger)

    if state_payload is None and run_record is None:
        raise HTTPException(status_code=404, detail=f"{label} install task '{task_id}' not found")

    if state_payload is None and run_record is not None:
        run_payload = _install_state_from_run(run_record, plugin_id=plugin_id, kind=kind)
        if str(run_payload.get("status") or "") in INSTALL_TERMINAL_STATUSES:
            return _persist_terminal_install_payload(
                task_id,
                plugin_id=plugin_id,
                kind=kind,
                payload=run_payload,
            )
        return run_payload

    payload = dict(state_payload or {})
    if run_record is None:
        state_status = str(payload.get("status") or "")
        if run_missing and state_status not in INSTALL_TERMINAL_STATUSES:
            return _mark_stale_install_task(
                task_id,
                plugin_id=plugin_id,
                kind=kind,
                label=label,
                payload=payload,
            )
        return payload

    run_payload = _install_state_from_run(run_record, plugin_id=plugin_id, kind=kind)
    payload["run_id"] = str(payload.get("run_id") or run_payload.get("run_id") or task_id)
    payload["task_id"] = str(payload.get("task_id") or task_id)

    state_status = str(payload.get("status") or "")
    run_status = str(run_payload.get("status") or "")
    if state_status in INSTALL_TERMINAL_STATUSES:
        return payload
    if run_status in INSTALL_TERMINAL_STATUSES:
        payload["status"] = run_status
        payload["phase"] = str(run_payload.get("phase") or run_status)
        payload["message"] = str(run_payload.get("message") or payload.get("message") or "")
        payload["progress"] = float(run_payload.get("progress") or payload.get("progress") or 0.0)
        payload["error"] = str(run_payload.get("error") or payload.get("error") or "")
        payload["release_name"] = str(run_payload.get("release_name") or payload.get("release_name") or "")
        payload["asset_name"] = str(run_payload.get("asset_name") or payload.get("asset_name") or "")
        payload["target_dir"] = str(run_payload.get("target_dir") or payload.get("target_dir") or "")
        payload["detected_path"] = str(run_payload.get("detected_path") or payload.get("detected_path") or "")
        payload["updated_at"] = run_payload.get("updated_at")
        payload["completed_at"] = run_payload.get("completed_at")
        return _persist_terminal_install_payload(
            task_id,
            plugin_id=plugin_id,
            kind=kind,
            payload=payload,
        )

    payload["status"] = run_status or state_status
    if run_payload.get("phase"):
        payload["phase"] = run_payload["phase"]
    if run_payload.get("message"):
        payload["message"] = run_payload["message"]
    if isinstance(run_payload.get("progress"), (int, float)):
        payload["progress"] = float(run_payload["progress"])
    metrics = dict(getattr(run_record, "metrics", {}) or {})
    if not payload.get("downloaded_bytes") and metrics.get("downloaded_bytes") is not None:
        payload["downloaded_bytes"] = int(metrics.get("downloaded_bytes") or 0)
    if not payload.get("total_bytes") and metrics.get("total_bytes") is not None:
        payload["total_bytes"] = int(metrics.get("total_bytes") or 0)
    if not payload.get("resume_from") and metrics.get("resume_from") is not None:
        payload["resume_from"] = int(metrics.get("resume_from") or 0)
    payload["updated_at"] = getattr(run_record, "updated_at", None) or payload.get("updated_at")
    return payload


async def _start_install_task(
    *,
    plugin_id: str,
    kind: str,
    payload: InstallStartPayload,
    request: Request,
) -> JSONResponse:
    registration = await _get_plugin_registration(plugin_id)
    spec = _get_install_kind_spec(kind, registration=registration)
    try:
        client_host = request.client.host if request.client is not None else None
        args: dict[str, object] = {"force": bool(payload.force)}
        entry_timeout = spec.get("entry_timeout")
        if isinstance(entry_timeout, (int, float)) and not isinstance(entry_timeout, bool):
            args["_ctx"] = {"entry_timeout": float(entry_timeout)}
        created = await run_service.create_run(
            RunCreateRequest(
                plugin_id=plugin_id,
                entry_id=spec["entry_id"],
                args=args,
            ),
            client_host=client_host,
        )
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)

    local_save_failed = False
    local_save_error = ""
    state_payload = _queued_install_task_state(
        task_id=created.run_id,
        plugin_id=plugin_id,
        spec=spec,
    )
    try:
        state_payload = await _run_blocking(
            update_install_task_state,
            created.run_id,
            kind=spec["kind"],
            plugin_id=plugin_id,
            run_id=created.run_id,
            status="queued",
            phase="queued",
            message=spec["queued_message"],
            progress=0.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - run exists; return retryable metadata.
        logger.warning(
            "failed to persist local install state for run={} kind={}",
            created.run_id,
            spec["kind"],
            exc_info=True,
        )
        local_save_failed = True
        local_save_error = f"{type(exc).__name__}: {exc}"
    response_payload = {
        "task_id": created.run_id,
        "run_id": created.run_id,
        "status": created.status,
        "state": state_payload,
        "local_save_failed": local_save_failed,
    }
    if local_save_failed:
        response_payload.update(
            {
                "error": "local_state_persist_failed",
                "message": _INSTALL_STATE_PERSIST_FAILED,
                "error_detail": local_save_error,
                "retry_hint": _LOCAL_STATE_RETRY_HINT,
            }
        )
    return JSONResponse(response_payload)


async def _latest_install_task_payload(*, plugin_id: str, kind: str) -> JSONResponse:
    registration = await _get_plugin_registration(plugin_id)
    spec = _get_install_kind_spec(
        kind,
        registration=registration,
        allow_legacy_read=True,
    )
    payload = await _run_blocking(
        load_latest_install_task_state,
        kind=spec["kind"],
        plugin_id=plugin_id,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail=f"No {spec['label']} install task found")
    task_id = str(payload.get("task_id") or "").strip()
    return JSONResponse(
        await _run_blocking(
            _resolve_install_task_payload,
            task_id,
            plugin_id=plugin_id,
            kind=spec["kind"],
            label=spec["label"],
        )
    )


async def _get_install_task_payload(*, plugin_id: str, kind: str, task_id: str) -> JSONResponse:
    registration = await _get_plugin_registration(plugin_id)
    spec = _get_install_kind_spec(
        kind,
        registration=registration,
        allow_legacy_read=True,
    )
    return JSONResponse(
        await _run_blocking(
            _resolve_install_task_payload,
            task_id,
            plugin_id=plugin_id,
            kind=spec["kind"],
            label=spec["label"],
        )
    )


async def _install_stream_response(
    *,
    plugin_id: str,
    kind: str,
    task_id: str,
    request: Request,
) -> StreamingResponse:
    registration = await _get_plugin_registration(plugin_id)
    spec = _get_install_kind_spec(
        kind,
        registration=registration,
        allow_legacy_read=True,
    )
    await _run_blocking(
        _resolve_install_task_payload,
        task_id,
        plugin_id=plugin_id,
        kind=spec["kind"],
        label=spec["label"],
    )

    async def _event_stream():
        last_payload = ""
        idle_cycles = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                payload = await _run_blocking(
                    _resolve_install_task_payload,
                    task_id,
                    plugin_id=plugin_id,
                    kind=spec["kind"],
                    label=spec["label"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surface failures through SSE.
                logger.warning(
                    "install SSE state read failed: plugin_id={} kind={} task_id={}",
                    plugin_id,
                    spec["kind"],
                    task_id,
                    exc_info=True,
                )
                payload = _install_stream_error_payload(
                    task_id,
                    plugin_id=plugin_id,
                    kind=spec["kind"],
                    label=spec["label"],
                    error=exc,
                )
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if serialized != last_payload:
                last_payload = serialized
                idle_cycles = 0
                yield f"data: {serialized}\n\n"
                if str(payload.get("status") or "") in INSTALL_TERMINAL_STATUSES:
                    break
            else:
                idle_cycles += 1
                if idle_cycles % 20 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/plugin/{plugin_id}/ui-api/textractor/install")
async def plugin_start_textractor_install(
    plugin_id: str,
    payload: InstallStartPayload,
    request: Request,
):
    return await _start_install_task(
        plugin_id=plugin_id,
        kind="textractor",
        payload=payload,
        request=request,
    )


@router.get("/plugin/{plugin_id}/ui-api/textractor/install/latest")
async def plugin_latest_textractor_install(plugin_id: str):
    return await _latest_install_task_payload(plugin_id=plugin_id, kind="textractor")


@router.get("/plugin/{plugin_id}/ui-api/textractor/install/{task_id}")
async def plugin_get_textractor_install(plugin_id: str, task_id: str):
    return await _get_install_task_payload(plugin_id=plugin_id, kind="textractor", task_id=task_id)


@router.get("/plugin/{plugin_id}/ui-api/textractor/install/{task_id}/stream")
async def plugin_stream_textractor_install(
    plugin_id: str,
    task_id: str,
    request: Request,
):
    return await _install_stream_response(
        plugin_id=plugin_id,
        kind="textractor",
        task_id=task_id,
        request=request,
    )


# ====== Tesseract install endpoints ======


@router.post("/plugin/{plugin_id}/ui-api/tesseract/install")
async def plugin_start_tesseract_install(
    plugin_id: str,
    payload: InstallStartPayload,
    request: Request,
):
    return await _start_install_task(
        plugin_id=plugin_id,
        kind="tesseract",
        payload=payload,
        request=request,
    )


@router.get("/plugin/{plugin_id}/ui-api/tesseract/install/latest")
async def plugin_latest_tesseract_install(plugin_id: str):
    return await _latest_install_task_payload(plugin_id=plugin_id, kind="tesseract")


@router.get("/plugin/{plugin_id}/ui-api/tesseract/install/{task_id}")
async def plugin_get_tesseract_install(plugin_id: str, task_id: str):
    return await _get_install_task_payload(plugin_id=plugin_id, kind="tesseract", task_id=task_id)


@router.get("/plugin/{plugin_id}/ui-api/tesseract/install/{task_id}/stream")
async def plugin_stream_tesseract_install(
    plugin_id: str,
    task_id: str,
    request: Request,
):
    return await _install_stream_response(
        plugin_id=plugin_id,
        kind="tesseract",
        task_id=task_id,
        request=request,
    )


# ====== RapidOCR model-download endpoints ======
# Mirrors the textractor install pattern: POST to start, GET for
# latest task, GET {task_id}, GET {task_id}/stream. URL base is
# `/rapidocr-models` (kebab-case in URL, `rapidocr_models` snake_case as the
# persisted task kind). The frontend's install task helper builds GET URLs as
# `${config.url}/${task_id}` so POST and GET must share the same base prefix.


@router.post("/plugin/{plugin_id}/ui-api/rapidocr-models")
async def plugin_start_rapidocr_models_download(
    plugin_id: str,
    payload: InstallStartPayload,
    request: Request,
):
    return await _start_install_task(
        plugin_id=plugin_id,
        kind="rapidocr_models",
        payload=payload,
        request=request,
    )


@router.get("/plugin/{plugin_id}/ui-api/rapidocr-models/latest")
async def plugin_latest_rapidocr_models_download(plugin_id: str):
    return await _latest_install_task_payload(plugin_id=plugin_id, kind="rapidocr_models")


@router.get("/plugin/{plugin_id}/ui-api/rapidocr-models/{task_id}")
async def plugin_get_rapidocr_models_download(plugin_id: str, task_id: str):
    return await _get_install_task_payload(plugin_id=plugin_id, kind="rapidocr_models", task_id=task_id)


@router.get("/plugin/{plugin_id}/ui-api/rapidocr-models/{task_id}/stream")
async def plugin_stream_rapidocr_models_download(
    plugin_id: str,
    task_id: str,
    request: Request,
):
    return await _install_stream_response(
        plugin_id=plugin_id,
        kind="rapidocr_models",
        task_id=task_id,
        request=request,
    )


# ====== Tutorial progress endpoints ======

_TUTORIAL_DEFAULTS = {
    "completed": False,
    "skipped": False,
    "last_step_index": 0,
    "started_at": 0.0,
    "completed_at": 0.0,
}
_tutorial_store_instance: Path | None = None
_tutorial_store_instances: dict[str, Path] = {}
_tutorial_store_lock = threading.RLock()
_tutorial_migrated_paths: set[Path] = set()
_GALGAME_TUTORIAL_MIGRATION_RETRY_COOLDOWN_SECONDS = 60.0
_galgame_tutorial_migration_retry_after: dict[Path, float] = {}


def _read_legacy_galgame_tutorial_progress(
    legacy_store_path: Path,
) -> dict[str, Any] | None:
    raw = json.loads(legacy_store_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("galgame store json root is not an object")
    progress = raw.get("tutorial_progress")
    return dict(progress) if isinstance(progress, dict) else None


def _load_legacy_galgame_tutorial_progress(
    registration: InstallPluginRegistration,
) -> tuple[dict[str, Any] | None, Path | None]:
    runtime_store_path = (
        resolve_runtime_data_root()
        / "plugins"
        / "galgame_plugin"
        / "data"
        / "galgame_store.json"
    )
    if runtime_store_path.is_file():
        try:
            progress = _read_legacy_galgame_tutorial_progress(runtime_store_path)
        except Exception:  # noqa: BLE001 - corrupt legacy data must not break the API.
            logger.warning(
                "failed to read legacy galgame tutorial progress from {}; trying backup",
                runtime_store_path,
                exc_info=True,
            )
            backup_path = runtime_store_path.with_name(
                f"{runtime_store_path.name}.bak"
            )
            if backup_path.is_file():
                try:
                    progress = _read_legacy_galgame_tutorial_progress(backup_path)
                except Exception:  # noqa: BLE001 - fall through to selected source.
                    logger.warning(
                        "failed to recover legacy galgame tutorial progress from {}",
                        backup_path,
                        exc_info=True,
                    )
                else:
                    if progress is not None:
                        return progress, backup_path
        else:
            if progress is not None:
                return progress, runtime_store_path

    config_path = registration.config_path
    if config_path is None:
        return None, None
    selected_store_path = config_path.parent / "data" / "galgame_store.json"
    if selected_store_path == runtime_store_path or not selected_store_path.is_file():
        return None, None
    try:
        progress = _read_legacy_galgame_tutorial_progress(selected_store_path)
    except Exception:  # noqa: BLE001 - corrupt legacy data must not break the API.
        logger.warning(
            "failed to read selected-source galgame tutorial progress from {}",
            selected_store_path,
            exc_info=True,
        )
        return None, None
    if progress is None:
        return None, None
    return progress, selected_store_path


def _write_migrated_tutorial_progress(
    store_path: Path,
    progress: dict[str, Any],
) -> bool:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = store_path.with_name(
        f".{store_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with tmp_path.open("w", encoding="utf-8") as tmp_file:
            json.dump(progress, tmp_file, ensure_ascii=False, sort_keys=True, indent=2)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        if store_path.exists():
            return False
        os.replace(tmp_path, store_path)
        return True
    finally:
        tmp_path.unlink(missing_ok=True)


def _migrate_legacy_galgame_tutorial_progress(
    store_path: Path,
    *,
    registration: InstallPluginRegistration | None,
) -> None:
    with _tutorial_store_lock:
        if registration is None or registration.plugin_id != "galgame_plugin":
            return
        if store_path.exists():
            _galgame_tutorial_migration_retry_after.pop(store_path, None)
            return
        if time.monotonic() < _galgame_tutorial_migration_retry_after.get(store_path, 0.0):
            return
        try:
            progress, source_path = _load_legacy_galgame_tutorial_progress(registration)
            if progress is None or source_path is None:
                return
            if _write_migrated_tutorial_progress(store_path, progress):
                logger.info(
                    "galgame tutorial progress migrated from legacy store: {} -> {}",
                    source_path,
                    store_path,
                )
        except Exception:  # noqa: BLE001 - migration is best effort and must not break the API.
            logger.warning(
                "galgame tutorial progress migration failed for {}",
                store_path,
                exc_info=True,
            )
        finally:
            if store_path.exists():
                _galgame_tutorial_migration_retry_after.pop(store_path, None)
            else:
                _galgame_tutorial_migration_retry_after[store_path] = (
                    time.monotonic() + _GALGAME_TUTORIAL_MIGRATION_RETRY_COOLDOWN_SECONDS
                )


def _run_tutorial_migrations(
    store_path: Path,
    *,
    plugin_id: str,
    registration: InstallPluginRegistration | None = None,
) -> None:
    _migrate_legacy_galgame_tutorial_progress(
        store_path,
        registration=registration,
    )
    if store_path in _tutorial_migrated_paths:
        return
    for hook in install_registry.tutorial_migration_hooks_for(plugin_id):
        hook(store_path)
    _tutorial_migrated_paths.add(store_path)


def _tutorial_store(
    plugin_id: str = "",
    registration: InstallPluginRegistration | None = None,
) -> Path:
    global _tutorial_store_instance
    normalized_plugin_id = _normalize_registered_plugin_id(plugin_id) if plugin_id else ""
    with _tutorial_store_lock:
        if not normalized_plugin_id and _tutorial_store_instance is not None:
            store_path = _tutorial_store_instance
        else:
            existing = _tutorial_store_instances.get(normalized_plugin_id)
            if existing is not None:
                store_path = existing
            else:
                store_dir = resolve_runtime_data_root() / "server" / "plugin_install"
                if normalized_plugin_id:
                    store_dir = store_dir / normalized_plugin_id
                store_path = store_dir / "tutorial_progress.json"
                if normalized_plugin_id:
                    _tutorial_store_instances[normalized_plugin_id] = store_path
                else:
                    _tutorial_store_instance = store_path
        _run_tutorial_migrations(
            store_path,
            plugin_id=normalized_plugin_id,
            registration=registration,
        )
        return store_path


class TutorialProgressPayload(BaseModel):
    completed: bool = False
    skipped: bool = False
    last_step_index: int = 0
    started_at: float = 0.0
    completed_at: float = 0.0


def _read_tutorial_progress(
    plugin_id: str = "",
    registration: InstallPluginRegistration | None = None,
) -> dict[str, Any] | None:
    store_path = _tutorial_store(plugin_id, registration)
    if not store_path.is_file():
        return None
    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception:
        logger.warning("tutorial progress read failed", exc_info=True)
        raise
    return raw if isinstance(raw, dict) else None


def _write_tutorial_progress(
    progress: dict[str, Any],
    plugin_id: str = "",
    registration: InstallPluginRegistration | None = None,
) -> None:
    store_path = _tutorial_store(plugin_id, registration)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = store_path.with_suffix(store_path.suffix + ".tmp")
    try:
        tmp_path.write_text(
            json.dumps(dict(progress), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(store_path)
    except Exception:
        logger.warning("tutorial progress write failed", exc_info=True)
        raise


def _normalize_tutorial_progress(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(_TUTORIAL_DEFAULTS)

    result = dict(_TUTORIAL_DEFAULTS)
    for key in _TUTORIAL_DEFAULTS:
        if key in raw:
            result[key] = raw[key]
    if not isinstance(result["completed"], bool):
        result["completed"] = _TUTORIAL_DEFAULTS["completed"]
    if not isinstance(result["skipped"], bool):
        result["skipped"] = _TUTORIAL_DEFAULTS["skipped"]
    try:
        result["last_step_index"] = max(0, int(result["last_step_index"] or 0))
    except (TypeError, ValueError):
        result["last_step_index"] = 0
    try:
        result["started_at"] = max(0.0, float(result["started_at"] or 0.0))
    except (TypeError, ValueError):
        result["started_at"] = 0.0
    try:
        result["completed_at"] = max(0.0, float(result["completed_at"] or 0.0))
    except (TypeError, ValueError):
        result["completed_at"] = 0.0
    return result


@router.get("/plugin/{plugin_id}/ui-api/tutorial/status")
async def get_tutorial_status(plugin_id: str) -> JSONResponse:
    registration = await _get_plugin_registration(plugin_id)
    _ensure_tutorial_enabled(registration)
    try:
        raw = await _run_blocking(_read_tutorial_progress, plugin_id, registration)
    except Exception:
        logger.error("tutorial progress status read failed", exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Internal server error", "progress": _normalize_tutorial_progress(None)},
            status_code=500,
        )
    return JSONResponse({"ok": True, "progress": _normalize_tutorial_progress(raw)})


@router.post("/plugin/{plugin_id}/ui-api/tutorial/progress")
async def save_tutorial_progress(
    plugin_id: str,
    body: TutorialProgressPayload,
) -> JSONResponse:
    registration = await _get_plugin_registration(plugin_id)
    _ensure_tutorial_enabled(registration)
    payload = body.model_dump(exclude_unset=True)
    try:
        current = _normalize_tutorial_progress(
            await _run_blocking(_read_tutorial_progress, plugin_id, registration)
        )
    except Exception:
        logger.error("tutorial progress save aborted after read failure", exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Internal server error", "progress": _normalize_tutorial_progress(None)},
            status_code=500,
        )
    normalized_payload = _normalize_tutorial_progress(payload)
    current.update(
        {
            key: normalized_payload[key]
            for key in payload
            if key in _TUTORIAL_DEFAULTS
        }
    )
    # Server-side consistency: completed_at only makes sense when completed=True.
    # The "Reopen Setup Guide" reset path only sends {completed:False, skipped:False,
    # last_step_index:0, started_at} and would otherwise leave a stale
    # completed_at>0 stuck on the persisted state, contradicting completed=False
    # for any reader that only inspects the timestamp.
    if not current["completed"] and not current["skipped"]:
        current["completed_at"] = _TUTORIAL_DEFAULTS["completed_at"]
    try:
        await _run_blocking(
            _write_tutorial_progress,
            current,
            plugin_id,
            registration,
        )
    except Exception:
        logger.warning("tutorial progress save failed", exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Internal server error", "progress": current},
            status_code=500,
        )
    return JSONResponse({"ok": True, "progress": current})
