"""Immutable coordinates shared by evidence producers and admission.

Ranges refer to the existing canonical 16 kHz timeline. They neither retain
PCM nor claim that a transport write was acknowledged by the Provider.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from main_logic.voice_turn.contracts import VoiceTurnToken
from .._provider_events import ProviderUtteranceKey


class EvidenceMode(StrEnum):
    OBSERVE = "observe"
    AUTHORITATIVE = "authoritative"


class EvidenceStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    DENY = "deny"
    UNAVAILABLE = "unavailable"


class EvidenceWindowState(StrEnum):
    COLLECTING = "collecting"
    PREFIX_FROZEN = "prefix_frozen"
    SETTLED = "settled"
    RETIRED = "retired"


class ContinuityVerdict(StrEnum):
    SAME_SPEAKER = "same_speaker"
    CHANGE = "change"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AudioRangeReference:
    session_generation: int
    transport_generation: int
    timeline_generation: int
    start_sample_16k: int
    end_sample_16k: int
    first_sequence_no: int | None
    last_sequence_no: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name == "first_sequence_no" and value is None:
                continue
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.end_sample_16k <= self.start_sample_16k:
            raise ValueError("audio range must be nonempty and half-open")
        if self.last_sequence_no < 1 or (
            self.first_sequence_no is not None
            and (self.first_sequence_no < 1 or self.last_sequence_no < self.first_sequence_no)
        ):
            raise ValueError("audio sequence range must be positive and ordered")

    @property
    def timeline(self) -> tuple[int, int, int]:
        return self.session_generation, self.transport_generation, self.timeline_generation

    def contains(self, other: AudioRangeReference) -> bool:
        return bool(
            self.timeline == other.timeline
            and self.start_sample_16k <= other.start_sample_16k
            and self.end_sample_16k >= other.end_sample_16k
            and (self.first_sequence_no is None or other.first_sequence_no is None
                 or self.first_sequence_no <= other.first_sequence_no)
            and self.last_sequence_no >= other.last_sequence_no
        )


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    window_id: int
    revision: int
    audio_range: AudioRangeReference
    state: EvidenceWindowState = EvidenceWindowState.PREFIX_FROZEN

    def __post_init__(self) -> None:
        if type(self.window_id) is not int or self.window_id < 1:
            raise ValueError("window_id must be positive")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be positive")
        if type(self.audio_range) is not AudioRangeReference:
            raise TypeError("audio_range must be AudioRangeReference")
        if self.audio_range.end_sample_16k - self.audio_range.start_sample_16k > 64000:
            raise ValueError("one evidence window cannot exceed four seconds")
        if type(self.state) is not EvidenceWindowState:
            raise TypeError("state must be EvidenceWindowState")


@dataclass(frozen=True, slots=True)
class ScoreEvidence:
    window: EvidenceWindow
    scored_range: AudioRangeReference
    status: EvidenceStatus
    model_version: str
    policy_version: str
    completed_at: float
    through_sequence_no: int
    events_closed: bool

    def __post_init__(self) -> None:
        if type(self.window) is not EvidenceWindow:
            raise TypeError("window must be EvidenceWindow")
        if type(self.scored_range) is not AudioRangeReference:
            raise TypeError("scored_range must be AudioRangeReference")
        if not self.window.audio_range.contains(self.scored_range):
            raise ValueError("raw scored range must remain inside its frozen window")
        if type(self.status) is not EvidenceStatus:
            raise TypeError("status must be EvidenceStatus")
        if not self.model_version or not self.policy_version:
            raise ValueError("model and policy versions are required")
        if not math.isfinite(self.completed_at) or self.completed_at < 0:
            raise ValueError("completion time must be finite and non-negative")
        if type(self.through_sequence_no) is not int or self.through_sequence_no < 1:
            raise ValueError("ordered evidence sequence must be positive")
        if type(self.events_closed) is not bool:
            raise TypeError("events_closed must be bool")


@dataclass(frozen=True, slots=True)
class ContinuityEvidence:
    audio_range: AudioRangeReference
    verdict: ContinuityVerdict = ContinuityVerdict.UNKNOWN
    algorithm_version: str = "unavailable"
    quality: str = "unevaluated"


@dataclass(frozen=True, slots=True)
class ProviderEvidenceBinding:
    provider_key: ProviderUtteranceKey
    turn_token: VoiceTurnToken
    record_generation: int
    window: EvidenceWindow
    target_range: AudioRangeReference
    policy_version: str

    def __post_init__(self) -> None:
        if type(self.provider_key) is not ProviderUtteranceKey:
            raise TypeError("provider_key must be ProviderUtteranceKey")
        if type(self.turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(self.record_generation) is not int or self.record_generation < 1:
            raise ValueError("record_generation must be positive")
        if type(self.window) is not EvidenceWindow:
            raise TypeError("window must be EvidenceWindow")
        if type(self.target_range) is not AudioRangeReference:
            raise TypeError("target_range must be AudioRangeReference")
        if not self.window.audio_range.contains(self.target_range):
            raise ValueError("target must be supported by the frozen window coordinates")
        if self.target_range.session_generation != self.turn_token.ingress.session_epoch:
            raise ValueError("sample reference must belong to the turn session")
        if self.window.state not in {EvidenceWindowState.PREFIX_FROZEN, EvidenceWindowState.SETTLED}:
            raise ValueError("binding requires a frozen prefix")
        if not self.policy_version:
            raise ValueError("policy version is required")


@dataclass(frozen=True, slots=True)
class EvidenceProof:
    binding: ProviderEvidenceBinding
    status: EvidenceStatus
    reason: str
    mode: EvidenceMode
    scores: tuple[ScoreEvidence, ...] = ()
    continuity: tuple[ContinuityEvidence, ...] = ()

    def __post_init__(self) -> None:
        if type(self.binding) is not ProviderEvidenceBinding:
            raise TypeError("binding must be ProviderEvidenceBinding")
        if type(self.status) is not EvidenceStatus or type(self.mode) is not EvidenceMode:
            raise TypeError("invalid proof status or mode")
        if len(self.scores) > 32 or len(self.continuity) > 32:
            raise ValueError("evidence dependencies exceed the shared candidate bound")
