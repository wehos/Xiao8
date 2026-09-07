"""Public, UI-safe state for the voice identity application service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class VoiceIdentityEffectiveReason(StrEnum):
    """Stable reasons explaining why the requested filter is or is not active."""

    DISABLED = "disabled"
    READY = "ready"
    ACTIVATION_PENDING = "activation_pending"
    NO_PROFILE = "no_profile"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROFILE_INCOMPATIBLE = "profile_incompatible"
    AUDIO_CONTRACT_MISMATCH = "audio_contract_mismatch"
    SECURE_STORAGE_UNAVAILABLE = "secure_storage_unavailable"
    ENROLLMENT_ACTIVE = "enrollment_active"
    RUNTIME_DEGRADED = "runtime_degraded"
    UNSUPPORTED_ASR_ROUTE = "unsupported_asr_route"
    SHADOW_MODE = "shadow_mode"


@dataclass(frozen=True, slots=True)
class VoiceIdentityState:
    """Immutable snapshot returned by the application-level controller."""

    requested_enabled: bool
    effective_enabled: bool
    effective_reason: VoiceIdentityEffectiveReason
    has_profile: bool

    def __post_init__(self) -> None:
        for name in ("requested_enabled", "effective_enabled", "has_profile"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if type(self.effective_reason) is not VoiceIdentityEffectiveReason:
            raise TypeError("effective_reason must be VoiceIdentityEffectiveReason")
        if self.effective_enabled:
            if not self.requested_enabled:
                raise ValueError("effective voice identity requires the user request")
            if not self.has_profile:
                raise ValueError("effective voice identity requires a stored profile")
            if self.effective_reason is not VoiceIdentityEffectiveReason.READY:
                raise ValueError("effective voice identity must be ready")
        elif self.effective_reason is VoiceIdentityEffectiveReason.READY:
            raise ValueError("ready voice identity must be effective")

    def as_dict(self) -> dict[str, bool | str]:
        """Return the stable JSON-compatible control-plane representation."""

        return {
            "requested_enabled": self.requested_enabled,
            "effective_enabled": self.effective_enabled,
            "effective_reason": self.effective_reason.value,
            "has_profile": self.has_profile,
        }
