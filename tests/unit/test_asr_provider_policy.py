from __future__ import annotations

import pytest

from main_logic.asr_client.provider_policy import (
    AsrProviderPolicy,
    resolve_provider_policy,
)
from main_logic.asr_client._registry_meta import (
    ASR_PROVIDER_REGISTRY,
    AsrProviderAvailability,
    AsrSpeakerExactIntervalCapability,
)


def test_streaming_manual_provider_requires_smart_turn() -> None:
    policy = resolve_provider_policy("qwen", "manual")

    assert policy == AsrProviderPolicy(
        transport="streaming",
        endpoint_authority="smart_turn",
        smart_turn_required=True,
        max_segment_ms=None,
        warm_transport_ms=25_000,
        replay_policy="preconnect_only",
    )


def test_provider_endpoint_is_the_logical_turn_authority() -> None:
    policy = resolve_provider_policy("soniox", "provider")

    assert policy.endpoint_authority == "provider"
    assert policy.smart_turn_required is False
    assert policy.replay_policy == "provider_managed"


def test_only_qwen_provider_route_declares_exact_speaker_interval() -> None:
    capability = AsrSpeakerExactIntervalCapability

    assert (
        resolve_provider_policy("qwen", "provider").speaker_exact_interval_capability
        is capability.CANONICAL_16K_EXACT_INTERVAL
    )
    assert (
        resolve_provider_policy("qwen", "manual").speaker_exact_interval_capability
        is capability.UNSUPPORTED
    )
    for provider_key, meta in ASR_PROVIDER_REGISTRY.items():
        if (
            provider_key == "qwen"
            or meta.availability is not AsrProviderAvailability.IMPLEMENTED
            or "provider" not in meta.supported_endpointing_modes
        ):
            continue
        assert (
            resolve_provider_policy(
                provider_key,
                "provider",
            ).speaker_exact_interval_capability
            is capability.UNSUPPORTED
        )


@pytest.mark.parametrize("provider_key", ["glm", "gemini"])
def test_segmented_provider_always_requires_smart_turn(provider_key: str) -> None:
    policy = resolve_provider_policy(provider_key, "manual")

    assert policy.transport == "segmented"
    assert policy.endpoint_authority == "smart_turn"
    assert policy.smart_turn_required is True
    assert policy.max_segment_ms == 27_000
    assert policy.warm_transport_ms == 0


@pytest.mark.parametrize("provider_key", ["glm", "gemini"])
def test_segmented_final_timeout_covers_worker_request_timeout(
    provider_key: str,
) -> None:
    # The segmented workers allow 35 s per HTTP request and the final request
    # starts at the turn seal, so the provider-final watchdog must be armed
    # with a longer timeout than the shared streaming default.
    policy = resolve_provider_policy(provider_key, "manual")

    assert policy.provider_final_timeout_ms == 40_000


@pytest.mark.parametrize("provider_key", ["qwen", "soniox"])
def test_streaming_final_timeout_keeps_shared_default(provider_key: str) -> None:
    policy = resolve_provider_policy(provider_key, "manual")

    assert policy.provider_final_timeout_ms == 10_000


def test_openai_provider_endpoint_does_not_require_smart_turn() -> None:
    policy = resolve_provider_policy("openai", "provider")

    assert policy.transport == "streaming"
    assert policy.endpoint_authority == "provider"
    assert policy.smart_turn_required is False


def test_openai_manual_endpointing_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="ASR_ENDPOINTING_NOT_SUPPORTED"):
        resolve_provider_policy("openai", "manual")


def test_blocked_backend_has_explicit_availability() -> None:
    assert (
        ASR_PROVIDER_REGISTRY["free"].availability
        is AsrProviderAvailability.BLOCKED_BACKEND
    )
