"""Stable contracts for provider-neutral semantic turn detection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeAlias, runtime_checkable


class TurnDecision(Enum):
    """A semantic judgment only; timeout and provider commits live elsewhere."""

    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


class EvaluationStatus(Enum):
    """Execution status kept separate from the semantic decision."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    STALE = "stale"


class SpeechActivityEvent(Enum):
    """Cheap VAD events; none of these commits an ASR turn."""

    NONE = "none"
    SPEECH_STARTED = "speech_started"
    CANDIDATE_PAUSE = "candidate_pause"
    SPEECH_RESUMED = "speech_resumed"


class AsrSubmitStatus(Enum):
    """Outcome of submitting one already-normalized frame to independent ASR."""

    ACCEPTED = "accepted"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class VoiceIngressToken:
    """Identity captured before one microphone frame belongs to a turn."""

    session_epoch: int
    connection_id: str
    lease_generation: int
    route_generation: int
    audio_generation: int


@dataclass(frozen=True, slots=True)
class VoiceTurnToken:
    """Logical voice-turn identity shared by Core and independent ASR."""

    ingress: VoiceIngressToken
    turn_id: int


@dataclass(frozen=True, slots=True)
class VoiceTranscriptEvent:
    """One route-authorized logical transcript for a Core-side consumer."""

    turn_token: VoiceTurnToken
    provider: str
    text: str


@dataclass(frozen=True, slots=True)
class AsrFailureEvent:
    """One provider-runtime failure that forces the Core route closed."""

    code: str
    provider: str
    session_epoch: int


@dataclass(frozen=True, slots=True)
class VoicePartialEvent:
    """Display-only partial transcript emitted by independent ASR."""

    turn_token: VoiceTurnToken
    text: str

    @property
    def session_epoch(self) -> int:
        """Compatibility view for existing read-only epoch checks."""

        return self.turn_token.ingress.session_epoch


@dataclass(frozen=True, slots=True)
class AsrStatusEvent:
    """Stable Core-facing status without provider implementation details."""

    code: str
    provider: str
    # Default keeps narrow legacy test doubles constructible; production
    # runtime call sites always provide the captured source epoch explicitly.
    session_epoch: int = -1
    transport_generation: int = -1
    lifecycle_revision: int = -1
    reason_code: str | None = None
    incident_id: str | None = None


@dataclass(frozen=True, slots=True)
class AsrLifecycleNotification:
    """Independent-ASR lifecycle state; Core remains route authority."""

    state: str
    provider: str
    session_epoch: int
    transport_generation: int = -1
    lifecycle_revision: int = -1
    reason_code: str | None = None
    incident_id: str | None = None


@dataclass(frozen=True, slots=True)
class AsrSubmitResult:
    """Explicit submit disposition so Core never inspects runtime state."""

    status: AsrSubmitStatus


VoiceTranscriptCallback: TypeAlias = Callable[
    [VoiceTranscriptEvent],
    Awaitable[None],
]

@dataclass(frozen=True, slots=True)
class TurnEvaluation:
    status: EvaluationStatus
    decision: TurnDecision | None
    probability: float | None
    generation: int
    activity_seq: int
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is EvaluationStatus.OK:
            if self.decision is None or self.probability is None:
                raise ValueError("OK evaluations require a decision and probability")
            if not 0.0 <= self.probability <= 1.0:
                raise ValueError("probability must be within [0, 1]")
        elif self.decision is not None or self.probability is not None:
            raise ValueError("non-OK evaluations must not carry a semantic result")


@runtime_checkable
class TurnDetector(Protocol):
    """Contract consumed by the future ASR Controller."""

    async def on_speech_started(self) -> None: ...

    async def evaluate(self, audio_tail: bytes) -> TurnEvaluation: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AsrTurnCapabilities:
    """Only the capability needed to choose an endpoint authority."""

    semantic_endpoint: bool


def requires_external_turn_detector(capabilities: AsrTurnCapabilities) -> bool:
    """Return false for Soniox-like providers with authoritative endpoints."""

    return not capabilities.semantic_endpoint


def build_turn_detector_if_required(
    capabilities: AsrTurnCapabilities, factory: Callable[[], TurnDetector]
) -> TurnDetector | None:
    """Construct only for providers without an authoritative semantic endpoint."""

    if not requires_external_turn_detector(capabilities):
        return None
    return factory()
