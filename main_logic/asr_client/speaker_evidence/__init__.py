"""Sample-scoped evidence metadata; no PCM, transcript queue or I/O authority."""

from .contracts import (
    AudioRangeReference, ContinuityEvidence, ContinuityVerdict, EvidenceMode,
    EvidenceProof, EvidenceStatus, EvidenceWindow, EvidenceWindowState,
    ProviderEvidenceBinding, ScoreEvidence,
)
from .coverage import evaluate_coverage
from .observation import EvidenceObservationRegistry

__all__ = [
    "AudioRangeReference", "ContinuityEvidence", "ContinuityVerdict", "EvidenceMode",
    "EvidenceProof", "EvidenceStatus", "EvidenceWindow", "EvidenceWindowState",
    "ProviderEvidenceBinding", "ScoreEvidence", "evaluate_coverage",
    "EvidenceObservationRegistry",
]
