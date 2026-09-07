"""Local control plane for one encrypted Owner voice profile."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from main_logic.voice_identity_service.audio_contract import (
    OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID,
    OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ,
)
from main_logic.voice_identity_service.registry import (
    VoiceIdentityServiceRegistryError,
    get_voice_identity_service_for_router,
)
from main_logic.voice_identity_service.service import VoiceIdentityServiceError
from main_routers.system_router import _validate_local_mutation_request


router = APIRouter(prefix="/api/voice-identity", tags=["voice-identity"])
_ENROLLMENT_HEADER = "X-Voice-Identity-Enrollment"
_PROFILE_HEADER = "X-Voice-Identity-Profile"
_SEGMENT_HEADER = "X-Voice-Identity-Segment"
_AUDIO_CONTRACT_HEADER = "X-Voice-Audio-Contract"
_PCM_CONTENT_TYPE = "audio/pcm;format=pcm_s16le;rate=48000;channels=1"
_MAX_REFERENCE_PCM_BYTES = (
    OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ * 4 * 2
)
_MAX_VERIFICATION_PCM_BYTES = (
    OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ * 5 * 2
)
_MAX_FILTER_JSON_BYTES = 1024


def _service():
    try:
        return get_voice_identity_service_for_router()
    except VoiceIdentityServiceRegistryError:
        return None


def _service_unavailable() -> JSONResponse:
    return JSONResponse(
        {"error_code": "runtime_degraded"},
        status_code=503,
    )


def _service_error(exc: VoiceIdentityServiceError) -> JSONResponse:
    if exc.code in {
        "invalid_enrollment_id",
        "invalid_profile_id",
        "invalid_segment_index",
    }:
        status_code = 400
    elif exc.code in {
        "stale_enrollment",
        "segment_out_of_order",
        "segment_in_progress",
    }:
        status_code = 409
    elif exc.code == "audio_too_long":
        status_code = 413
    elif exc.code in {
        "invalid_pcm",
        "speech_too_short",
        "silence",
        "volume_too_low",
        "severe_clipping",
        "no_speech_detected",
        "voice_samples_inconsistent",
        "owner_verification_failed",
    }:
        status_code = 422
    elif exc.code in {
        "audio_processing_unavailable",
        "unsupported_audio_contract",
        "model_unavailable",
        "secure_storage_unavailable",
    }:
        status_code = 503
    else:
        status_code = 503
    return JSONResponse({"error_code": exc.code}, status_code=status_code)


def _validate_mutation(request: Request, payload: dict | None = None):
    return _validate_local_mutation_request(
        request,
        payload=payload,
        error_defaults={"error_code": "mutation_not_allowed"},
    )


async def _read_bounded_body(request: Request, maximum_bytes: int) -> bytes | None:
    """Read an ASGI request body without ever retaining more than the limit."""

    buffered = bytearray()
    try:
        async for chunk in request.stream():
            if len(chunk) > maximum_bytes - len(buffered):
                return None
            buffered.extend(chunk)
        return bytes(buffered)
    finally:
        buffered[:] = b"\x00" * len(buffered)


@router.get("/status")
async def get_voice_identity_status():
    service = _service()
    if service is None:
        return _service_unavailable()
    return service.status().as_dict()


@router.post("/enrollment/start")
async def start_voice_identity_enrollment(request: Request):
    rejected = _validate_mutation(request)
    if rejected is not None:
        return rejected
    service = _service()
    if service is None:
        return _service_unavailable()
    try:
        await service.start_enrollment()
    except VoiceIdentityServiceError as exc:
        return _service_error(exc)
    return service.status().as_dict()


@router.put("/enrollment/segment")
async def submit_voice_identity_enrollment_segment(request: Request):
    rejected = _validate_mutation(request)
    if rejected is not None:
        return rejected
    if request.headers.get("content-type", "").lower() != _PCM_CONTENT_TYPE:
        return JSONResponse({"error_code": "invalid_pcm"}, status_code=415)
    audio_contract_id = request.headers.get(_AUDIO_CONTRACT_HEADER, "")
    if audio_contract_id != OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID:
        return JSONResponse(
            {"error_code": "unsupported_audio_contract"},
            status_code=415,
        )
    raw_segment_index = request.headers.get(_SEGMENT_HEADER, "")
    if raw_segment_index not in {"1", "2", "3", "4"}:
        return JSONResponse(
            {"error_code": "invalid_segment_index"},
            status_code=400,
        )
    segment_index = int(raw_segment_index)
    maximum_pcm_bytes = (
        _MAX_VERIFICATION_PCM_BYTES
        if segment_index == 4
        else _MAX_REFERENCE_PCM_BYTES
    )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise ValueError
            if parsed_content_length > maximum_pcm_bytes:
                return JSONResponse(
                    {"error_code": "audio_too_long"},
                    status_code=413,
                )
        except ValueError:
            return JSONResponse({"error_code": "invalid_pcm"}, status_code=400)
    pcm16 = await _read_bounded_body(request, maximum_pcm_bytes)
    if pcm16 is None:
        return JSONResponse({"error_code": "audio_too_long"}, status_code=413)
    if len(pcm16) % 2:
        return JSONResponse({"error_code": "invalid_pcm"}, status_code=400)
    service = _service()
    if service is None:
        return _service_unavailable()
    try:
        status = await service.submit_enrollment_segment(
            request.headers.get(_ENROLLMENT_HEADER, ""),
            request.headers.get(_PROFILE_HEADER, ""),
            segment_index,
            pcm16,
            sample_rate_hz=OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ,
            audio_contract_id=audio_contract_id,
        )
    except VoiceIdentityServiceError as exc:
        return _service_error(exc)
    return status.as_dict()


@router.post("/enrollment/cancel")
async def cancel_voice_identity_enrollment(request: Request):
    rejected = _validate_mutation(request)
    if rejected is not None:
        return rejected
    service = _service()
    if service is None:
        return _service_unavailable()
    enrollment_id = request.headers.get(_ENROLLMENT_HEADER, "")
    try:
        await service.cancel_enrollment(enrollment_id)
    except VoiceIdentityServiceError as exc:
        return _service_error(exc)
    return service.status().as_dict()


@router.put("/filter")
async def set_voice_identity_filter(request: Request):
    has_csrf_header = bool(request.headers.get("X-CSRF-Token"))
    if has_csrf_header:
        rejected = _validate_mutation(request)
        if rejected is not None:
            return rejected

    body = await _read_bounded_body(request, _MAX_FILTER_JSON_BYTES)
    if body is None:
        return JSONResponse({"error_code": "invalid_enabled"}, status_code=413)
    try:
        parsed_payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed_payload = None
    payload = parsed_payload if type(parsed_payload) is dict else None
    if not has_csrf_header:
        rejected = _validate_mutation(request, payload)
        if rejected is not None:
            return rejected
    if payload is None:
        return JSONResponse({"error_code": "invalid_enabled"}, status_code=422)
    enabled = payload.get("enabled")
    if type(enabled) is not bool:
        return JSONResponse({"error_code": "invalid_enabled"}, status_code=422)
    service = _service()
    if service is None:
        return _service_unavailable()
    try:
        status = await service.set_filter(enabled)
    except VoiceIdentityServiceError as exc:
        return _service_error(exc)
    return status.as_dict()


@router.delete("/profile")
async def delete_voice_identity_profile(request: Request):
    rejected = _validate_mutation(request)
    if rejected is not None:
        return rejected
    service = _service()
    if service is None:
        return _service_unavailable()
    try:
        status = await service.delete_profile()
    except VoiceIdentityServiceError as exc:
        return _service_error(exc)
    return status.as_dict()


__all__ = ["router"]
