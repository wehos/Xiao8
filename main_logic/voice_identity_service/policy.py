"""Stateless Owner-speaker classification for independent-ASR evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowObservationKind,
)


class OwnerVoiceClassification(StrEnum):
    LOW = "low"
    HIGH = "high"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class OwnerVoicePolicyResult:
    classification: OwnerVoiceClassification
    reason: str


class OwnerVoicePolicy:
    """Classify one immutable score without retaining candidate state."""

    FIRST_CHECKPOINT_MS = 1_500
    SECOND_CHECKPOINT_MS = 3_000
    SIMILARITY_THRESHOLD = 0.40

    @classmethod
    def classify(
        cls,
        *,
        checkpoint_ms: int | None,
        similarity: float,
        observation_kind: SpeakerShadowObservationKind = "checkpoint",
        audio_ms: int | None = None,
    ) -> OwnerVoicePolicyResult:
        if (
            type(observation_kind) is not str
            or observation_kind not in ("checkpoint", "completion_confirmation")
            or type(similarity) not in {int, float}
            or not math.isfinite(float(similarity))
            or not -1.0 <= float(similarity) <= 1.0
        ):
            return OwnerVoicePolicyResult(
                OwnerVoiceClassification.UNAVAILABLE,
                "invalid_observation",
            )

        if observation_kind == "completion_confirmation":
            valid_checkpoint = bool(
                type(checkpoint_ms) is int
                and checkpoint_ms == cls.FIRST_CHECKPOINT_MS
                and type(audio_ms) is int
                and cls.FIRST_CHECKPOINT_MS
                < audio_ms
                < cls.SECOND_CHECKPOINT_MS
            )
        else:
            valid_checkpoint = bool(
                type(checkpoint_ms) is int
                and checkpoint_ms
                in {cls.FIRST_CHECKPOINT_MS, cls.SECOND_CHECKPOINT_MS}
            )
        if not valid_checkpoint:
            return OwnerVoicePolicyResult(
                OwnerVoiceClassification.UNAVAILABLE,
                "invalid_observation",
            )
        if float(similarity) < cls.SIMILARITY_THRESHOLD:
            return OwnerVoicePolicyResult(
                OwnerVoiceClassification.LOW,
                "clear_mismatch",
            )
        return OwnerVoicePolicyResult(
            OwnerVoiceClassification.HIGH,
            "owner_or_uncertain",
        )


__all__ = [
    "OwnerVoiceClassification",
    "OwnerVoicePolicy",
    "OwnerVoicePolicyResult",
]
