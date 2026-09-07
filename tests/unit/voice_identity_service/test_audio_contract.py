from __future__ import annotations

import pytest

from main_logic.voice_identity_service.audio_contract import (
    OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID,
    OWNER_CAMPPLUS_DESKTOP_CONTRACT_REVISION,
    OWNER_CAMPPLUS_DESKTOP_RUNTIME_CHUNK_SAMPLES,
    OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ,
    OWNER_CAMPPLUS_TARGET_SAMPLE_RATE_HZ,
    VoiceIdentityAudioContractSnapshot,
    desktop_audio_contract_snapshot,
)


@pytest.mark.unit
def test_desktop_contract_pins_the_runtime_processing_domain() -> None:
    contract = desktop_audio_contract_snapshot(noise_reduction_enabled=True)

    assert contract.contract_id == "owner-campplus-desktop-v1"
    assert contract.revision == 1
    assert contract.noise_reduction_enabled
    assert OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID == contract.contract_id
    assert OWNER_CAMPPLUS_DESKTOP_CONTRACT_REVISION == contract.revision
    assert OWNER_CAMPPLUS_DESKTOP_SOURCE_SAMPLE_RATE_HZ == 48_000
    assert OWNER_CAMPPLUS_DESKTOP_RUNTIME_CHUNK_SAMPLES == 480
    assert OWNER_CAMPPLUS_TARGET_SAMPLE_RATE_HZ == 16_000


@pytest.mark.unit
def test_noise_reduction_is_part_of_profile_compatibility() -> None:
    enabled = desktop_audio_contract_snapshot(noise_reduction_enabled=True)
    disabled = desktop_audio_contract_snapshot(noise_reduction_enabled=False)

    assert enabled.matches_runtime(noise_reduction_enabled=True)
    assert not enabled.matches_runtime(noise_reduction_enabled=False)
    assert disabled.matches_runtime(noise_reduction_enabled=False)
    assert not disabled.matches_runtime(noise_reduction_enabled=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("contract_id", "revision"),
    [("unknown", 1), (OWNER_CAMPPLUS_DESKTOP_CONTRACT_ID, 2)],
)
def test_unknown_contract_or_revision_is_rejected(
    contract_id: str,
    revision: int,
) -> None:
    with pytest.raises(ValueError, match="unsupported voice identity audio contract"):
        VoiceIdentityAudioContractSnapshot(
            contract_id=contract_id,
            revision=revision,
            noise_reduction_enabled=True,
        )
