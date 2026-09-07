"""Conservative range analysis without transport or admission side effects."""

from .contracts import (
    EvidenceMode, EvidenceProof, EvidenceStatus, ProviderEvidenceBinding, ScoreEvidence,
    ContinuityEvidence,
)


def evaluate_coverage(
    binding: ProviderEvidenceBinding,
    scores: tuple[ScoreEvidence, ...] = (),
    continuity: tuple[ContinuityEvidence, ...] = (),
    *,
    mode: EvidenceMode = EvidenceMode.OBSERVE,
) -> EvidenceProof:
    """Preserve exact range semantics; cross-window authority remains disabled.

There is currently no evaluated continuity backend. Even a caller-provided
SAME_SPEAKER fact, adjacent VAD ranges or overlapping high scores cannot widen
an existing score's scope. Observational proofs never authorize admission.
    """
    if len(scores) > 32 or len(continuity) > 32:
        return EvidenceProof(binding, EvidenceStatus.UNAVAILABLE, "capacity", mode)
    if binding.target_range.first_sequence_no is None:
        return EvidenceProof(binding, EvidenceStatus.UNAVAILABLE, "sequence_unknown", mode, scores, continuity)
    matching = tuple(
        score for score in scores
        if score.window == binding.window
        and score.scored_range == binding.target_range
        and score.policy_version == binding.policy_version
        and score.events_closed
    )
    statuses = {score.status for score in matching}
    if EvidenceStatus.DENY in statuses:
        status, reason = EvidenceStatus.DENY, "exact_deny"
    elif EvidenceStatus.VERIFIED in statuses:
        status, reason = EvidenceStatus.VERIFIED, "exact_covered"
    else:
        status = EvidenceStatus.UNAVAILABLE
        reason = "continuity_unevaluated" if scores else "evidence_missing"
    return EvidenceProof(binding, status, reason, mode, scores, continuity)
