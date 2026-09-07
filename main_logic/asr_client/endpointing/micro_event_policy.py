"""Fail-open policy for tiny Provider activity probes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from main_logic.voice_turn.contracts import SpeechActivityEvent

from .silero_vad import SileroFeedResult, SileroVad

ProviderMicroEventMode = Literal["off", "shadow", "enforce"]
ProviderMicroEventStartKind = Literal[
    "speech_started",
    "speech_resumed_at_candidate_boundary",
]
_SILERO_WINDOW_MS = (
    SileroVad.WINDOW_SAMPLES * 1_000 // SileroVad.SAMPLE_RATE
)


@dataclass(frozen=True, slots=True)
class ProviderMicroEventConfig:
    """Calibration-gated thresholds; disabled unless explicitly selected."""

    mode: ProviderMicroEventMode = "off"
    calibration_revision: str | None = None
    maximum_silero_span_ms: int = 384
    maximum_post_start_onset_windows: int = 4
    maximum_rnnoise_active_run_upper_bound_ms: int = 160

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in {
            "off",
            "shadow",
            "enforce",
        }:
            raise ValueError("mode must be one of: off, shadow, enforce")
        revision = self.calibration_revision
        if revision is not None and (
            type(revision) is not str or not revision.strip()
        ):
            raise ValueError(
                "calibration_revision must be a non-empty string when provided"
            )
        if self.mode == "enforce" and revision is None:
            raise ValueError(
                "enforce mode requires a non-empty calibration_revision"
            )
        for name in (
            "maximum_silero_span_ms",
            "maximum_rnnoise_active_run_upper_bound_ms",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        maximum_post_start = self.maximum_post_start_onset_windows
        if type(maximum_post_start) is not int or maximum_post_start < 0:
            raise ValueError(
                "maximum_post_start_onset_windows must be a non-negative integer"
            )


@dataclass(frozen=True, slots=True)
class ProviderMicroEventEvidence:
    """Candidate-local aggregate evidence without PCM or probabilities arrays."""

    silero: SileroFeedResult | None
    rnnoise_evidence_complete: bool
    rnnoise_longest_active_run_upper_bound_ms: int | None
    speech_started_count: int = 0
    speech_resumed_count: int = 0
    candidate_pause_count: int = 0
    event_sequence_valid: bool = False
    candidate_local_start_kind: ProviderMicroEventStartKind | None = None


@dataclass(frozen=True, slots=True)
class ProviderMicroEventDecision:
    """Separate probe classification from enforcement authority."""

    would_suppress: bool
    suppress: bool
    reason: str
    fail_open: bool = False


class ProviderMicroEventPolicy:
    """Suppress only one tightly calibrated, fully evidenced micro-event."""

    def __init__(self, config: ProviderMicroEventConfig | None = None) -> None:
        if config is not None and type(config) is not ProviderMicroEventConfig:
            raise TypeError("config must be ProviderMicroEventConfig")
        self._config = config or ProviderMicroEventConfig()

    @property
    def config(self) -> ProviderMicroEventConfig:
        return self._config

    def decide(
        self,
        evidence: ProviderMicroEventEvidence,
    ) -> ProviderMicroEventDecision:
        if self._config.mode == "off":
            return ProviderMicroEventDecision(False, False, "disabled")
        if type(evidence) is not ProviderMicroEventEvidence:
            return ProviderMicroEventDecision(
                False,
                False,
                "invalid_evidence",
                fail_open=True,
            )

        valid, reason, fail_open = self._validate_evidence(evidence)
        if not valid:
            return ProviderMicroEventDecision(
                False,
                False,
                reason,
                fail_open=fail_open,
            )

        silero = evidence.silero
        assert silero is not None
        first_onset = silero.first_onset_window_index
        last_onset = silero.last_onset_window_index
        assert first_onset is not None and last_onset is not None
        span_ms = (last_onset - first_onset + 1) * _SILERO_WINDOW_MS
        if span_ms > self._config.maximum_silero_span_ms:
            return ProviderMicroEventDecision(False, False, "silero_span_exceeded")
        if (
            silero.post_confirmation_onset_window_count
            > self._config.maximum_post_start_onset_windows
        ):
            return ProviderMicroEventDecision(
                False,
                False,
                "post_start_onset_exceeded",
            )
        active_run_ms = evidence.rnnoise_longest_active_run_upper_bound_ms
        assert active_run_ms is not None
        if (
            active_run_ms
            > self._config.maximum_rnnoise_active_run_upper_bound_ms
        ):
            return ProviderMicroEventDecision(
                False,
                False,
                "rnnoise_active_run_exceeded",
            )

        suppress = self._config.mode == "enforce"
        return ProviderMicroEventDecision(
            True,
            suppress,
            "micro_event_enforced" if suppress else "micro_event_shadow",
        )

    @staticmethod
    def _validate_evidence(
        evidence: ProviderMicroEventEvidence,
    ) -> tuple[bool, str, bool]:
        silero = evidence.silero
        if type(silero) is not SileroFeedResult:
            return False, "incomplete_silero_evidence", True
        if (
            type(silero.window_count) is not int
            or silero.window_count <= 0
            or type(silero.onset_window_count) is not int
            or silero.onset_window_count <= 0
            or type(silero.offset_window_count) is not int
            or silero.offset_window_count < 0
            or type(silero.ambiguous_window_count) is not int
            or silero.ambiguous_window_count < 0
            or type(silero.first_onset_window_index) is not int
            or type(silero.last_onset_window_index) is not int
            or type(silero.post_confirmation_onset_window_count) is not int
        ):
            return False, "incomplete_silero_evidence", True
        first_onset = silero.first_onset_window_index
        last_onset = silero.last_onset_window_index
        if (
            not 0 <= first_onset <= last_onset < silero.window_count
            or (
                silero.onset_window_count
                + silero.offset_window_count
                + silero.ambiguous_window_count
                != silero.window_count
            )
            or silero.onset_window_count > last_onset - first_onset + 1
            or not 0
            <= silero.post_confirmation_onset_window_count
            <= silero.onset_window_count
        ):
            return False, "unordered_silero_evidence", True

        active_run_ms = evidence.rnnoise_longest_active_run_upper_bound_ms
        if (
            type(evidence.rnnoise_evidence_complete) is not bool
            or not evidence.rnnoise_evidence_complete
            or type(active_run_ms) is not int
            or active_run_ms < 0
        ):
            return False, "incomplete_rnnoise_evidence", True

        counts = (
            evidence.speech_started_count,
            evidence.speech_resumed_count,
            evidence.candidate_pause_count,
        )
        if (
            any(type(value) is not int or value < 0 for value in counts)
            or type(evidence.event_sequence_valid) is not bool
        ):
            return False, "malformed_event_counts", True
        if not evidence.event_sequence_valid:
            return False, "unordered_silero_events", True
        start_kind = evidence.candidate_local_start_kind
        if start_kind == "speech_started":
            expected_start_counts = (1, 0)
        elif start_kind == "speech_resumed_at_candidate_boundary":
            expected_start_counts = (0, 1)
        elif start_kind is None and sum(counts[:2]) == 0:
            return False, "missing_speech_start", False
        else:
            return False, "invalid_candidate_local_start", True
        if counts[:2] != expected_start_counts:
            return False, "multiple_or_resumed_speech_segments", False
        if evidence.candidate_pause_count != 1:
            return False, "missing_or_repeated_candidate_pause", False
        if type(silero.events) is not tuple or silero.events != (
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.CANDIDATE_PAUSE,
        ):
            return False, "unexpected_silero_events", True
        return True, "eligible", False
