from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main_logic.voice_identity_service.registry import (
    VoiceIdentityServiceRegistryError,
)
from main_logic.voice_identity_service.service import VoiceIdentityServiceError
import main_routers.system_router._shared as system_router_shared
import main_routers.voice_identity_router as voice_identity_router


API_ROOT = "/api/voice-identity"
PCM_CONTENT_TYPE = "audio/pcm;format=pcm_s16le;rate=48000;channels=1"
AUDIO_CONTRACT_ID = "owner-campplus-desktop-v1"
MAX_PCM_BYTES = 48_000 * 4 * 2
MAX_VERIFICATION_PCM_BYTES = 48_000 * 5 * 2
MAX_FILTER_JSON_BYTES = 1024
AUTH_HEADERS = {
    "Origin": "http://testserver",
    "X-CSRF-Token": "voice-identity-test-token",
}
SAFE_STATUS = {
    "requested_enabled": True,
    "effective_enabled": True,
    "effective_reason": "ready",
    "has_profile": True,
    "enrollment": None,
    "profile_generation": "profile-a",
    "last_completed_enrollment_id": None,
    "runtime_mode": "enforce",
}


class _Status:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self._payload = copy.deepcopy(payload or SAFE_STATUS)

    def as_dict(self) -> dict[str, object]:
        return copy.deepcopy(self._payload)


def _fake_service(payload: dict[str, object] | None = None) -> SimpleNamespace:
    status = _Status(payload)
    return SimpleNamespace(
        status=MagicMock(return_value=status),
        start_enrollment=AsyncMock(),
        submit_enrollment_segment=AsyncMock(return_value=status),
        cancel_enrollment=AsyncMock(return_value=True),
        set_filter=AsyncMock(return_value=status),
        delete_profile=AsyncMock(return_value=status),
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    service: SimpleNamespace | None,
    *,
    authenticated: bool = True,
) -> TestClient:
    if service is None:

        def unavailable():
            raise VoiceIdentityServiceRegistryError("unavailable")

        monkeypatch.setattr(
            voice_identity_router,
            "get_voice_identity_service_for_router",
            unavailable,
        )
    else:
        monkeypatch.setattr(
            voice_identity_router,
            "get_voice_identity_service_for_router",
            lambda: service,
        )
    app = FastAPI()
    app.include_router(voice_identity_router.router)
    client = TestClient(app, base_url="http://testserver")
    if authenticated:
        client.headers.update(AUTH_HEADERS)
        client.headers.update({"X-Voice-Audio-Contract": AUDIO_CONTRACT_ID})
    return client


def _assert_private_values_absent(payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).lower()
    for private_name in ("pcm", "embedding", "similarity", "score"):
        assert private_name not in encoded


@pytest.fixture(autouse=True)
def _fixed_csrf_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        system_router_shared,
        "AUTOSTART_CSRF_TOKEN",
        AUTH_HEADERS["X-CSRF-Token"],
    )


@pytest.mark.unit
def test_registry_unavailable_is_ui_safe_for_status_and_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, None)

    status = client.get(f"{API_ROOT}/status")
    start = client.post(f"{API_ROOT}/enrollment/start")

    assert status.status_code == 503
    assert start.status_code == 503
    assert status.json() == {"error_code": "runtime_degraded"}
    assert start.json() == {"error_code": "runtime_degraded"}


@pytest.mark.unit
def test_status_is_public_and_does_not_require_mutation_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service, authenticated=False)

    response = client.get(f"{API_ROOT}/status")

    assert response.status_code == 200
    assert response.json() == SAFE_STATUS
    _assert_private_values_absent(response.json())


@pytest.mark.unit
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://testserver"},
        {"X-CSRF-Token": AUTH_HEADERS["X-CSRF-Token"]},
        {
            "Origin": "https://attacker.invalid",
            "X-CSRF-Token": AUTH_HEADERS["X-CSRF-Token"],
        },
        {
            "Origin": "http://testserver",
            "X-CSRF-Token": "wrong-token",
        },
    ],
)
def test_mutations_require_matching_csrf_and_local_origin(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service, authenticated=False)

    response = client.post(f"{API_ROOT}/enrollment/start", headers=headers)

    assert response.status_code == 403
    assert response.json()["error_code"] == "csrf_validation_failed"
    service.start_enrollment.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "path", "request_kwargs"),
    [
        ("post", "/enrollment/start", {}),
        (
            "put",
            "/enrollment/segment",
            {
                "content": b"",
                "headers": {
                    "Content-Type": PCM_CONTENT_TYPE,
                    "X-Voice-Identity-Segment": "1",
                },
            },
        ),
        ("post", "/enrollment/cancel", {}),
        ("put", "/filter", {"json": {"enabled": True}}),
        ("delete", "/profile", {}),
    ],
)
def test_every_mutation_route_is_csrf_guarded(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    request_kwargs: dict[str, object],
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service, authenticated=False)

    response = client.request(method, f"{API_ROOT}{path}", **request_kwargs)

    assert response.status_code == 403
    assert response.json()["error_code"] == "csrf_validation_failed"
    service.start_enrollment.assert_not_awaited()
    service.submit_enrollment_segment.assert_not_awaited()
    service.cancel_enrollment.assert_not_awaited()
    service.set_filter.assert_not_awaited()
    service.delete_profile.assert_not_awaited()


@pytest.mark.unit
def test_start_returns_canonical_status_without_private_model_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        **SAFE_STATUS,
        "requested_enabled": False,
        "effective_enabled": False,
        "effective_reason": "enrollment_active",
        "has_profile": False,
        "enrollment": {
            "enrollment_id": "enrollment-1",
            "expires_at": 123.5,
        },
        "profile_generation": None,
    }
    service = _fake_service(payload)
    client = _client(monkeypatch, service)

    response = client.post(f"{API_ROOT}/enrollment/start")

    assert response.status_code == 200
    assert response.json() == payload
    service.start_enrollment.assert_awaited_once_with()
    _assert_private_values_absent(response.json())


@pytest.mark.unit
def test_binary_segment_upload_forwards_exact_headers_index_and_body_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)
    pcm16 = bytes(MAX_PCM_BYTES)
    headers = {
        "Content-Type": PCM_CONTENT_TYPE,
        "X-Voice-Identity-Enrollment": "enrollment-1",
        "X-Voice-Identity-Profile": "profile-1",
        "X-Voice-Identity-Segment": "3",
    }

    first = client.put(f"{API_ROOT}/enrollment/segment", content=pcm16, headers=headers)
    second = client.put(
        f"{API_ROOT}/enrollment/segment", content=pcm16, headers=headers
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json() == SAFE_STATUS
    assert service.submit_enrollment_segment.await_count == 2
    assert service.submit_enrollment_segment.await_args_list[0].args == (
        "enrollment-1",
        "profile-1",
        3,
        pcm16,
    )
    assert service.submit_enrollment_segment.await_args_list[1].args == (
        "enrollment-1",
        "profile-1",
        3,
        pcm16,
    )
    assert service.submit_enrollment_segment.await_args_list[0].kwargs == {
        "sample_rate_hz": 48_000,
        "audio_contract_id": AUDIO_CONTRACT_ID,
    }
    assert service.submit_enrollment_segment.await_args_list[1].kwargs == {
        "sample_rate_hz": 48_000,
        "audio_contract_id": AUDIO_CONTRACT_ID,
    }
    _assert_private_values_absent(first.json())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("passed", "match_percent"),
    [(False, 31), (True, 72)],
)
def test_fourth_segment_verification_is_transient_and_recovery_is_canonical(
    monkeypatch: pytest.MonkeyPatch,
    passed: bool,
    match_percent: int,
) -> None:
    service = _fake_service()
    verification_status = _Status(
        {
            **SAFE_STATUS,
            "verification": {
                "passed": passed,
                "match_percent": match_percent,
            },
        }
    )
    service.submit_enrollment_segment.side_effect = [
        verification_status,
        _Status(),
    ]
    client = _client(monkeypatch, service)
    headers = {
        "Content-Type": PCM_CONTENT_TYPE,
        "X-Voice-Identity-Enrollment": "enrollment-1",
        "X-Voice-Identity-Profile": "profile-1",
        "X-Voice-Identity-Segment": "4",
    }
    pcm16 = bytes(MAX_VERIFICATION_PCM_BYTES)

    first = client.put(f"{API_ROOT}/enrollment/segment", content=pcm16, headers=headers)
    recovered = client.put(
        f"{API_ROOT}/enrollment/segment", content=pcm16, headers=headers
    )
    status = client.get(f"{API_ROOT}/status")

    assert first.status_code == 200
    assert first.json() == {
        **SAFE_STATUS,
        "verification": {
            "passed": passed,
            "match_percent": match_percent,
        },
    }
    assert recovered.status_code == 200
    assert recovered.json() == SAFE_STATUS
    assert status.status_code == 200
    assert status.json() == SAFE_STATUS
    assert "verification" not in recovered.json()
    assert "verification" not in status.json()
    _assert_private_values_absent(first.json())


@pytest.mark.unit
@pytest.mark.parametrize("segment_header", [None, "", "0", "5", "01", "+1", " 1"])
def test_segment_upload_requires_canonical_one_to_four_header(
    monkeypatch: pytest.MonkeyPatch,
    segment_header: str | None,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)
    headers = {
        "Content-Type": PCM_CONTENT_TYPE,
        "X-Voice-Identity-Enrollment": "enrollment-1",
        "X-Voice-Identity-Profile": "profile-1",
    }
    if segment_header is not None:
        headers["X-Voice-Identity-Segment"] = segment_header

    response = client.put(
        f"{API_ROOT}/enrollment/segment",
        content=bytes(48_000),
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json() == {"error_code": "invalid_segment_index"}
    service.submit_enrollment_segment.assert_not_awaited()


@pytest.mark.unit
def test_retired_one_shot_profile_endpoint_is_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    response = client.put(
        f"{API_ROOT}/enrollment/profile",
        content=bytes(48_000),
        headers={"Content-Type": PCM_CONTENT_TYPE},
    )

    assert response.status_code == 404
    service.submit_enrollment_segment.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body_size", "content_type", "expected_status", "expected_code"),
    [
        (MAX_PCM_BYTES, "application/octet-stream", 415, "invalid_pcm"),
        (
            MAX_PCM_BYTES,
            "audio/pcm;format=pcm_s16le;rate=16000;channels=1",
            415,
            "invalid_pcm",
        ),
        (
            MAX_PCM_BYTES,
            "audio/pcm;format=pcm_s16le;rate=44100;channels=1",
            415,
            "invalid_pcm",
        ),
        (
            MAX_PCM_BYTES,
            "audio/pcm;format=pcm_s16le;rate=48000;channels=2",
            415,
            "invalid_pcm",
        ),
        (MAX_PCM_BYTES + 1, PCM_CONTENT_TYPE, 413, "audio_too_long"),
    ],
)
def test_segment_upload_rejects_wrong_type_and_more_than_four_seconds(
    monkeypatch: pytest.MonkeyPatch,
    body_size: int,
    content_type: str,
    expected_status: int,
    expected_code: str,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    response = client.put(
        f"{API_ROOT}/enrollment/segment",
        content=bytes(body_size),
        headers={
            "Content-Type": content_type,
            "X-Voice-Identity-Enrollment": "enrollment-1",
            "X-Voice-Identity-Profile": "profile-1",
            "X-Voice-Identity-Segment": "1",
        },
    )

    assert response.status_code == expected_status
    assert response.json() == {"error_code": expected_code}
    service.submit_enrollment_segment.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("contract_id", [None, "", "owner-campplus-desktop-v0"])
def test_segment_upload_requires_known_audio_contract(
    monkeypatch: pytest.MonkeyPatch,
    contract_id: str | None,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)
    client.headers.pop("X-Voice-Audio-Contract", None)
    headers = {
        "Content-Type": PCM_CONTENT_TYPE,
        "X-Voice-Identity-Enrollment": "enrollment-1",
        "X-Voice-Identity-Profile": "profile-1",
        "X-Voice-Identity-Segment": "1",
    }
    if contract_id is not None:
        headers["X-Voice-Audio-Contract"] = contract_id

    response = client.put(
        f"{API_ROOT}/enrollment/segment",
        content=bytes(48_000 * 31 // 10 * 2),
        headers=headers,
    )

    assert response.status_code == 415
    assert response.json() == {"error_code": "unsupported_audio_contract"}
    service.submit_enrollment_segment.assert_not_awaited()


@pytest.mark.unit
def test_segment_upload_rejects_odd_pcm_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    response = client.put(
        f"{API_ROOT}/enrollment/segment",
        content=b"\x00",
        headers={
            "Content-Type": PCM_CONTENT_TYPE,
            "X-Voice-Identity-Enrollment": "enrollment-1",
            "X-Voice-Identity-Profile": "profile-1",
            "X-Voice-Identity-Segment": "1",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error_code": "invalid_pcm"}
    service.submit_enrollment_segment.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("segment_index", "body_size", "expected_status"),
    [
        (1, MAX_PCM_BYTES, 200),
        (1, MAX_PCM_BYTES + 1, 413),
        (2, MAX_PCM_BYTES, 200),
        (2, MAX_PCM_BYTES + 1, 413),
        (3, MAX_PCM_BYTES, 200),
        (3, MAX_PCM_BYTES + 1, 413),
        (4, MAX_VERIFICATION_PCM_BYTES, 200),
        (4, MAX_VERIFICATION_PCM_BYTES + 1, 413),
    ],
)
def test_segment_upload_applies_reference_and_verification_size_limits(
    monkeypatch: pytest.MonkeyPatch,
    segment_index: int,
    body_size: int,
    expected_status: int,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    response = client.put(
        f"{API_ROOT}/enrollment/segment",
        content=bytes(body_size),
        headers={
            "Content-Type": PCM_CONTENT_TYPE,
            "X-Voice-Identity-Enrollment": "enrollment-1",
            "X-Voice-Identity-Profile": "profile-1",
            "X-Voice-Identity-Segment": str(segment_index),
        },
    )

    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json() == SAFE_STATUS
        service.submit_enrollment_segment.assert_awaited_once()
    else:
        assert response.json() == {"error_code": "audio_too_long"}
        service.submit_enrollment_segment.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunked_profile_body_is_bounded_without_content_length() -> None:
    yielded_chunks = 0

    async def stream():
        nonlocal yielded_chunks
        for chunk in (bytes(MAX_PCM_BYTES), b"x", b"unreachable"):
            yielded_chunks += 1
            yield chunk

    request = SimpleNamespace(stream=stream)

    body = await voice_identity_router._read_bounded_body(request, MAX_PCM_BYTES)

    assert body is None
    assert yielded_chunks == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("invalid_enrollment_id", 400),
        ("invalid_profile_id", 400),
        ("invalid_segment_index", 400),
        ("stale_enrollment", 409),
        ("segment_out_of_order", 409),
        ("segment_in_progress", 409),
        ("invalid_pcm", 422),
        ("speech_too_short", 422),
        ("audio_too_long", 413),
        ("silence", 422),
        ("volume_too_low", 422),
        ("severe_clipping", 422),
        ("no_speech_detected", 422),
        ("voice_samples_inconsistent", 422),
        ("owner_verification_failed", 422),
        ("audio_processing_unavailable", 503),
        ("unsupported_audio_contract", 503),
        ("model_unavailable", 503),
    ],
)
def test_segment_upload_maps_stable_service_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_status: int,
) -> None:
    service = _fake_service()
    service.submit_enrollment_segment.side_effect = VoiceIdentityServiceError(error_code)
    client = _client(monkeypatch, service)

    response = client.put(
        f"{API_ROOT}/enrollment/segment",
        content=bytes(48_000),
        headers={
            "Content-Type": PCM_CONTENT_TYPE,
            "X-Voice-Identity-Enrollment": "enrollment-1",
            "X-Voice-Identity-Profile": "profile-1",
            "X-Voice-Identity-Segment": "1",
        },
    )

    assert response.status_code == expected_status
    assert response.json() == {"error_code": error_code}


@pytest.mark.unit
def test_cancel_forwards_enrollment_header_and_returns_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    response = client.post(
        f"{API_ROOT}/enrollment/cancel",
        headers={"X-Voice-Identity-Enrollment": "enrollment-1"},
    )

    assert response.status_code == 200
    assert response.json() == SAFE_STATUS
    service.cancel_enrollment.assert_awaited_once_with("enrollment-1")
    _assert_private_values_absent(response.json())


@pytest.mark.unit
def test_cancel_without_enrollment_header_passes_stable_empty_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    response = client.post(f"{API_ROOT}/enrollment/cancel")

    assert response.status_code == 200
    service.cancel_enrollment.assert_awaited_once_with("")


@pytest.mark.unit
def test_filter_requires_boolean_and_forwards_requested_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    invalid = client.put(f"{API_ROOT}/filter", json={"enabled": 1})
    enabled = client.put(f"{API_ROOT}/filter", json={"enabled": True})

    assert invalid.status_code == 422
    assert invalid.json() == {"error_code": "invalid_enabled"}
    assert enabled.status_code == 200
    assert enabled.json() == SAFE_STATUS
    service.set_filter.assert_awaited_once_with(True)
    _assert_private_values_absent(enabled.json())


@pytest.mark.unit
@pytest.mark.parametrize(
    "request_kwargs",
    [
        {},
        {"content": "{"},
        {"json": []},
        {"json": None},
    ],
)
def test_filter_rejects_missing_malformed_and_non_object_json_consistently(
    monkeypatch: pytest.MonkeyPatch,
    request_kwargs: dict[str, object],
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    response = client.put(f"{API_ROOT}/filter", **request_kwargs)

    assert response.status_code == 422
    assert response.json() == {"error_code": "invalid_enabled"}
    service.set_filter.assert_not_awaited()


@pytest.mark.unit
def test_filter_rejects_oversized_json_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)

    response = client.put(
        f"{API_ROOT}/filter",
        content=b"{" + b" " * MAX_FILTER_JSON_BYTES,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"error_code": "invalid_enabled"}
    service.set_filter.assert_not_awaited()


@pytest.mark.unit
def test_filter_accepts_json_body_at_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service)
    prefix = b'{"enabled":true,"pad":"'
    suffix = b'"}'
    padding = b"x" * (MAX_FILTER_JSON_BYTES - len(prefix) - len(suffix))

    response = client.put(
        f"{API_ROOT}/filter",
        content=prefix + padding + suffix,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    service.set_filter.assert_awaited_once_with(True)


@pytest.mark.unit
def test_filter_rejects_invalid_header_auth_before_reading_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    bounded_reader = AsyncMock()
    monkeypatch.setattr(voice_identity_router, "_read_bounded_body", bounded_reader)
    client = _client(monkeypatch, service, authenticated=False)

    response = client.put(
        f"{API_ROOT}/filter",
        json={"enabled": True},
        headers={
            "Origin": AUTH_HEADERS["Origin"],
            "X-CSRF-Token": "invalid-token",
        },
    )

    assert response.status_code == 403
    bounded_reader.assert_not_awaited()
    service.set_filter.assert_not_awaited()


@pytest.mark.unit
def test_filter_preserves_bounded_body_csrf_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fake_service()
    client = _client(monkeypatch, service, authenticated=False)

    response = client.put(
        f"{API_ROOT}/filter",
        json={
            "enabled": False,
            "_csrf_token": AUTH_HEADERS["X-CSRF-Token"],
        },
        headers={"Origin": AUTH_HEADERS["Origin"]},
    )

    assert response.status_code == 200
    service.set_filter.assert_awaited_once_with(False)


@pytest.mark.unit
def test_delete_profile_returns_canonical_disabled_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        **SAFE_STATUS,
        "requested_enabled": False,
        "effective_enabled": False,
        "effective_reason": "disabled",
        "has_profile": False,
        "profile_generation": None,
    }
    service = _fake_service(payload)
    client = _client(monkeypatch, service)

    response = client.delete(f"{API_ROOT}/profile")

    assert response.status_code == 200
    assert response.json() == payload
    service.delete_profile.assert_awaited_once_with()
    _assert_private_values_absent(response.json())
