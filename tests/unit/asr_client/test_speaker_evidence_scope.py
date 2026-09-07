from dataclasses import replace

import pytest

from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.speaker_evidence import (
    AudioRangeReference, ContinuityEvidence, ContinuityVerdict, EvidenceMode,
    EvidenceObservationRegistry, EvidenceStatus, EvidenceWindow,
    ProviderEvidenceBinding, ScoreEvidence, evaluate_coverage,
)
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken


def binding_for(key=1, *, end=24000, revision=1):
    audio = AudioRangeReference(2, 3, 4, 0, end, 1, 15)
    return ProviderEvidenceBinding(
        ProviderUtteranceKey(0, 0, key),
        VoiceTurnToken(VoiceIngressToken(2, "test", 1, 1, 1), key),
        key, EvidenceWindow(key, revision, audio), audio, "policy-test",
    )


def score_for(binding, status=EvidenceStatus.VERIFIED, **kwargs):
    return ScoreEvidence(binding.window, binding.target_range, status,
                         "model-test", binding.policy_version, 1.0, 1, True, **kwargs)


def test_exact_score_does_not_cover_shorter_text_by_cropping():
    scored = binding_for()
    shorter = replace(scored, target_range=replace(scored.target_range, end_sample_16k=23760))
    proof = evaluate_coverage(shorter, (score_for(scored),))
    assert proof.status is EvidenceStatus.UNAVAILABLE
    assert proof.scores[0].scored_range.end_sample_16k == 24000


@pytest.mark.parametrize("mutation", ["revision", "timeline", "unclosed", "policy"])
def test_scope_fences_reject_old_or_unclosed_evidence(mutation):
    binding = binding_for()
    score = score_for(binding)
    if mutation == "revision":
        score = replace(score, window=replace(score.window, revision=2))
    elif mutation == "timeline":
        other = replace(binding.target_range, timeline_generation=5)
        score = replace(score, window=replace(score.window, audio_range=other), scored_range=other)
    elif mutation == "unclosed":
        score = replace(score, events_closed=False)
    else:
        score = replace(score, policy_version="other")
    assert evaluate_coverage(binding, (score,)).status is EvidenceStatus.UNAVAILABLE


def test_single_high_score_and_unevaluated_continuity_cannot_cover_tail():
    binding = binding_for(end=48000)
    head = replace(binding.target_range, end_sample_16k=24000)
    score = replace(score_for(binding), scored_range=head)
    continuity = ContinuityEvidence(binding.target_range, ContinuityVerdict.SAME_SPEAKER, "unvalidated", "good")
    assert evaluate_coverage(binding, (score,), (continuity,)).status is EvidenceStatus.UNAVAILABLE


def test_multiple_complete_windows_without_evaluated_join_stay_unknown():
    binding = binding_for(end=48000)
    head = replace(binding.target_range, end_sample_16k=24000)
    tail = replace(binding.target_range, start_sample_16k=24000)
    scores = (
        replace(score_for(binding), scored_range=head),
        replace(score_for(binding), scored_range=tail),
    )
    assert evaluate_coverage(binding, scores).status is EvidenceStatus.UNAVAILABLE


def test_observer_never_reuses_registry_across_transport_or_session():
    observer = EvidenceObservationRegistry()
    binding = binding_for()
    assert observer.observe(binding) is not None
    other_range = replace(binding.target_range, transport_generation=4)
    other = replace(binding, target_range=other_range,
                    window=replace(binding.window, audio_range=other_range))
    assert observer.observe(other) is None
    assert observer.snapshot()["stale_scope"] == 1


def test_turn_session_cannot_be_mismatched_to_audio_reference():
    binding = binding_for()
    other = replace(binding.target_range, session_generation=3)
    with pytest.raises(ValueError, match="turn session"):
        replace(binding, target_range=other, window=replace(binding.window, audio_range=other))


def test_unknown_first_sequence_can_be_observed_but_never_verified():
    binding = binding_for()
    unknown = replace(binding.target_range, first_sequence_no=None)
    binding = replace(binding, target_range=unknown,
                      window=replace(binding.window, audio_range=unknown))
    proof = evaluate_coverage(binding, (score_for(binding),), mode=EvidenceMode.AUTHORITATIVE)
    assert proof.status is EvidenceStatus.UNAVAILABLE
    assert proof.reason == "sequence_unknown"


def test_exact_deny_wins_over_high_and_observation_has_no_authority():
    binding = binding_for()
    proof = evaluate_coverage(binding, (score_for(binding), score_for(binding, EvidenceStatus.DENY)))
    assert proof.status is EvidenceStatus.DENY
    assert proof.mode is EvidenceMode.OBSERVE


def test_observer_twenty_settled_keys_is_bounded_and_recycled_key_stays_stale():
    observer = EvidenceObservationRegistry(retired_capacity=4)
    first = binding_for()
    for key in range(1, 21):
        binding = binding_for(key)
        assert observer.observe(binding) is not None
        assert observer.retire(binding) == "completed"
        assert observer.retire(binding) == "already_completed"
    assert observer.snapshot()["active"] == 0
    assert observer.snapshot()["retired"] == 4
    assert observer.retire(first) == "stale"
    assert observer.observe(first) is None


def test_observer_ninth_key_cannot_evict_pending_or_replace_revision():
    observer = EvidenceObservationRegistry()
    for key in range(1, 9):
        assert observer.observe(binding_for(key)) is not None
    assert observer.observe(binding_for(9)) is None
    assert observer.observe(binding_for(1, revision=2)) is None
    assert observer.retire(binding_for(1, revision=2)) == "stale"
    assert observer.snapshot()["active"] == 8
    assert observer.snapshot()["capacity_skipped"] == 1


def test_window_cannot_hide_more_than_four_seconds_or_invalid_sample_coordinates():
    with pytest.raises(ValueError):
        binding_for(end=64001)
    with pytest.raises(ValueError):
        AudioRangeReference(1, 1, 1, 0, 0, 1, 1)
