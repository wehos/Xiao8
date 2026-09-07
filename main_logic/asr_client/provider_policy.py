"""Resolved transport and endpoint policy for one independent ASR session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ._registry_meta import (
    ASR_PROVIDER_REGISTRY,
    AsrEndpointingMode,
    AsrProviderAvailability,
    AsrSpeakerExactIntervalCapability,
)


AsrTransport = Literal["streaming", "segmented"]
AsrEndpointAuthority = Literal["provider", "smart_turn"]
AsrReplayPolicy = Literal["none", "preconnect_only", "provider_managed"]


@dataclass(frozen=True, slots=True)
class AsrProviderPolicy:
    transport: AsrTransport
    endpoint_authority: AsrEndpointAuthority
    smart_turn_required: bool
    max_segment_ms: int | None
    warm_transport_ms: int
    replay_policy: AsrReplayPolicy
    speaker_exact_interval_capability: AsrSpeakerExactIntervalCapability = (
        AsrSpeakerExactIntervalCapability.UNSUPPORTED
    )
    availability: AsrProviderAvailability = AsrProviderAvailability.IMPLEMENTED
    provider_final_timeout_ms: int = 10_000
    connect_max_attempts: int = 1
    connect_retry_base_seconds: float = 0.25
    connect_retry_cap_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_segment_ms is not None and self.max_segment_ms <= 0:
            raise ValueError("max_segment_ms must be positive")
        if self.warm_transport_ms < 0:
            raise ValueError("warm_transport_ms must not be negative")
        if self.transport == "segmented" and not self.smart_turn_required:
            raise ValueError("segmented ASR must require SmartTurn")
        if self.provider_final_timeout_ms <= 0:
            raise ValueError("provider_final_timeout_ms must be positive")
        if self.connect_max_attempts <= 0:
            raise ValueError("connect_max_attempts must be positive")
        if self.connect_retry_base_seconds <= 0:
            raise ValueError("connect_retry_base_seconds must be positive")
        if self.connect_retry_cap_seconds < self.connect_retry_base_seconds:
            raise ValueError(
                "connect_retry_cap_seconds must cover the retry base"
            )


class AsrProviderUnavailableError(RuntimeError):
    """Typed provider capability failure for callers that need policy."""

    def __init__(self, availability: AsrProviderAvailability) -> None:
        self.availability = availability
        super().__init__(f"ASR_PROVIDER_{availability.value.upper()}")


def resolve_provider_policy(
    provider_key: str,
    endpointing_mode: AsrEndpointingMode,
) -> AsrProviderPolicy:
    normalized_provider = str(provider_key or "").strip().lower()
    meta = ASR_PROVIDER_REGISTRY.get(normalized_provider)
    if meta is None:
        raise RuntimeError(f"ASR_BACKEND_NOT_IMPLEMENTED: {normalized_provider}")
    if endpointing_mode not in meta.supported_endpointing_modes:
        raise RuntimeError(
            "ASR_ENDPOINTING_NOT_SUPPORTED: "
            f"{normalized_provider} does not support {endpointing_mode}"
        )
    if meta.availability is not AsrProviderAvailability.IMPLEMENTED:
        raise AsrProviderUnavailableError(meta.availability)

    transport: AsrTransport = (
        "segmented" if meta.category in {"dummy", "segmented_request"} else "streaming"
    )
    endpoint_authority: AsrEndpointAuthority = (
        "provider" if endpointing_mode == "provider" else "smart_turn"
    )
    smart_turn_required = transport == "segmented" or endpoint_authority == "smart_turn"
    return AsrProviderPolicy(
        transport=transport,
        endpoint_authority=endpoint_authority,
        smart_turn_required=smart_turn_required,
        max_segment_ms=meta.max_segment_ms if transport == "segmented" else None,
        warm_transport_ms=meta.warm_transport_ms if transport == "streaming" else 0,
        replay_policy=meta.replay_policy,
        speaker_exact_interval_capability=(
            meta.speaker_exact_interval_capability
            if endpoint_authority == "provider"
            else AsrSpeakerExactIntervalCapability.UNSUPPORTED
        ),
        availability=meta.availability,
        provider_final_timeout_ms=meta.provider_final_timeout_ms,
        connect_max_attempts=meta.connect_max_attempts,
        connect_retry_base_seconds=meta.connect_retry_base_seconds,
        connect_retry_cap_seconds=meta.connect_retry_cap_seconds,
    )
