"""Versioned desktop audio-domain contract for Owner CAM++ profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID: Final = "owner-campplus-desktop-v1"
OWNER_CAMPPLUS_DESKTOP_CONTRACT_REVISION: Final = 1
OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ: Final = 48_000
OWNER_CAMPPLUS_DESKTOP_RUNTIME_CHUNK_SAMPLES: Final = 480
OWNER_CAMPPLUS_TARGET_SAMPLE_RATE_HZ: Final = 16_000


@dataclass(frozen=True, slots=True)
class VoiceIdentityAudioContractSnapshot:
    """Profile-owned processing-domain fields that affect score semantics."""

    contract_id: str
    revision: int
    noise_reduction_enabled: bool

    def __post_init__(self) -> None:
        if self.contract_id != OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID:
            raise ValueError("unsupported voice identity audio contract")
        if self.revision != OWNER_CAMPPLUS_DESKTOP_CONTRACT_REVISION:
            raise ValueError("unsupported voice identity audio contract revision")
        if type(self.noise_reduction_enabled) is not bool:
            raise TypeError("noise_reduction_enabled must be bool")

    def matches_runtime(self, *, noise_reduction_enabled: bool) -> bool:
        if type(noise_reduction_enabled) is not bool:
            return False
        return self.noise_reduction_enabled is noise_reduction_enabled


def desktop_audio_contract_snapshot(
    *,
    noise_reduction_enabled: bool,
) -> VoiceIdentityAudioContractSnapshot:
    """Freeze the runtime-relevant processing configuration for enrollment."""

    return VoiceIdentityAudioContractSnapshot(
        contract_id=OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID,
        revision=OWNER_CAMPPLUS_DESKTOP_CONTRACT_REVISION,
        noise_reduction_enabled=noise_reduction_enabled,
    )
