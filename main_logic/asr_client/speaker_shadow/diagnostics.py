"""Immutable, content-free observations; never speaker admission authority."""

from dataclasses import dataclass

from .contracts import SpeakerShadowCandidateKey


@dataclass(frozen=True, slots=True)
class SpeakerShadowDiagnostic:
    candidate: SpeakerShadowCandidateKey
    stage: str
    worker_generation: int
    sample_rate_hz: int
    accepted_sample_count: int
    buffered_sample_count: int | None
    finish_sample_count: int | None
    minimum_sample_count: int | None
    score_attempt_count: int
    score_input_sample_count: int
    score_outcome: str
    scored_sample_count: int
    last_checkpoint_ms: int | None
    terminal_reason: str | None
    evidence_sequence_no: int
    anchor_applied: bool
    anchor_discard_prefix_sample_count: int | None
    scoring_deferred: bool
