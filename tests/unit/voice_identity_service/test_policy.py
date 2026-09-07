import math

import pytest

from main_logic.voice_identity_service.policy import (
    OwnerVoiceClassification,
    OwnerVoicePolicy,
)


@pytest.mark.parametrize("checkpoint_ms", [1_500, 3_000])
def test_policy_classifies_each_low_checkpoint_without_candidate_state(
    checkpoint_ms: int,
) -> None:
    result = OwnerVoicePolicy.classify(
        checkpoint_ms=checkpoint_ms,
        similarity=0.39,
    )

    assert result.classification is OwnerVoiceClassification.LOW
    assert result.reason == "clear_mismatch"


def test_policy_classifies_threshold_as_high() -> None:
    result = OwnerVoicePolicy.classify(
        checkpoint_ms=1_500,
        similarity=0.40,
    )

    assert result.classification is OwnerVoiceClassification.HIGH
    assert result.reason == "owner_or_uncertain"


@pytest.mark.parametrize("audio_ms", [1_501, 2_999])
def test_policy_accepts_completion_confirmation_as_one_low_fact(
    audio_ms: int,
) -> None:
    result = OwnerVoicePolicy.classify(
        checkpoint_ms=1_500,
        similarity=0.20,
        observation_kind="completion_confirmation",
        audio_ms=audio_ms,
    )

    assert result.classification is OwnerVoiceClassification.LOW


@pytest.mark.parametrize(
    ("checkpoint_ms", "similarity", "observation_kind", "audio_ms"),
    [
        (None, 0.1, "checkpoint", None),
        (2_000, 0.1, "checkpoint", None),
        (1_500, math.nan, "checkpoint", None),
        (1_500, math.inf, "checkpoint", None),
        (1_500, 0.1, "completion_confirmation", 1_500),
        (1_500, 0.1, "completion_confirmation", 3_000),
    ],
)
def test_policy_invalid_observation_is_unavailable(
    checkpoint_ms: int | None,
    similarity: float,
    observation_kind: str,
    audio_ms: int | None,
) -> None:
    result = OwnerVoicePolicy.classify(
        checkpoint_ms=checkpoint_ms,
        similarity=similarity,
        observation_kind=observation_kind,  # type: ignore[arg-type]
        audio_ms=audio_ms,
    )

    assert result.classification is OwnerVoiceClassification.UNAVAILABLE
    assert result.reason == "invalid_observation"
