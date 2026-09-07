from __future__ import annotations

import pytest

from main_logic.asr_client._provider_events import ProviderAudioRange, ProviderUtteranceKey
from main_logic.asr_client.admission.contracts import (
    AdmissionDisposition,
    AdmissionResolutionTicket,
    BoundaryProof,
    PendingProviderFinal,
)
from main_logic.asr_client.admission.provider_turns import (
    ProviderAliasConflictError,
    ProviderBoundaryResult,
    ProviderTurnCorrelator,
)
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken


def _key(utterance_id: int) -> ProviderUtteranceKey:
    return ProviderUtteranceKey(1, 0, utterance_id)


def _token(turn_id: int) -> VoiceTurnToken:
    return VoiceTurnToken(VoiceIngressToken(1, "socket", 2, 3, 4), turn_id)


def _exact(key: ProviderUtteranceKey, turn_id: int) -> ProviderBoundaryResult:
    proof = BoundaryProof(
        proof_id=turn_id,
        owner_generation=7,
        provider_key=key,
    )
    return ProviderBoundaryResult(
        quality="exact",
        audio_range=ProviderAudioRange((turn_id - 1) * 100, turn_id * 100),
        proof=proof,
    )


def _final(key: ProviderUtteranceKey, text: str, received: float) -> PendingProviderFinal:
    return PendingProviderFinal(key, "qwen", text, received, received + 0.2)


def _resolution(turn_id: int, nonce: int = 1) -> AdmissionResolutionTicket:
    return AdmissionResolutionTicket(
        turn_token=_token(turn_id),
        record_generation=turn_id,
        resolution_nonce=nonce,
        disposition=AdmissionDisposition.FORWARD,
    )


def test_boundary_phase_cannot_bind_current_turn():
    correlator = ProviderTurnCorrelator(namespace=(1, 0))
    key = _key(1)
    correlator.record_boundary_result(key, _exact(key, 1))
    with pytest.raises(
        ProviderAliasConflictError,
        match="PROVIDER_ALIAS_BIND_REQUIRES_ORDERED",
    ):
        correlator.bind_ordered(key, _token(1))


def test_ordered_key_binds_exactly_one_voice_turn_token():
    correlator = ProviderTurnCorrelator(namespace=(1, 0))
    first_key = _key(1)
    second_key = _key(2)
    correlator.mark_ordered(first_key)
    record = correlator.bind_ordered(first_key, _token(1))
    assert record.bound_turn_token == _token(1)
    assert correlator.bind_ordered(first_key, _token(1)) is record

    correlator.mark_ordered(second_key)
    with pytest.raises(ProviderAliasConflictError, match="VOICE_TURN_ALREADY_BOUND"):
        correlator.bind_ordered(second_key, _token(1))


def test_conflicting_boundary_downgrades_only_same_key():
    correlator = ProviderTurnCorrelator(namespace=(1, 0))
    first_key, second_key = _key(1), _key(2)
    correlator.record_boundary_result(first_key, _exact(first_key, 1))
    correlator.record_boundary_result(second_key, _exact(second_key, 2))

    conflict = ProviderBoundaryResult(
        quality="exact",
        audio_range=ProviderAudioRange(0, 99),
        proof=_exact(first_key, 1).proof,
    )
    result = correlator.record_boundary_result(first_key, conflict)
    assert result.quality == "unknown"
    assert correlator.record_for(second_key).boundary_result.quality == "exact"


def test_optional_proof_overflow_downgrades_new_key_without_dropping_final():
    correlator = ProviderTurnCorrelator(namespace=(1, 0), proof_capacity=1)
    first_key, second_key = _key(1), _key(2)
    correlator.record_boundary_result(first_key, _exact(first_key, 1))
    second_exact = _exact(second_key, 2)
    result = correlator.record_boundary_result(second_key, second_exact)
    assert result.quality == "unknown"
    assert result.retired_proofs == (second_exact.proof,)
    assert correlator.record_for(second_key) is None

    correlator.mark_ordered(second_key)
    correlator.bind_ordered(second_key, _token(2))
    final = _final(second_key, "second", 12.0)
    record = correlator.record_final(second_key, final)
    assert record.pending_final is final


def test_provider_final_deadline_is_preserved_while_waiting_for_earlier_key():
    correlator = ProviderTurnCorrelator(namespace=(1, 0))
    first_key, second_key = _key(1), _key(2)
    correlator.mark_ordered(first_key)
    correlator.mark_ordered(second_key)
    first = _final(first_key, "first", 10.0)
    second = _final(second_key, "second", 10.05)
    correlator.record_final(second_key, second)
    correlator.record_final(first_key, first)
    assert correlator.record_for(second_key).pending_final.admission_deadline == 10.25


def test_complete_retires_only_finalized_record_and_bounds_tombstones():
    correlator = ProviderTurnCorrelator(namespace=(1, 0), completed_capacity=1)
    first_key, second_key = _key(1), _key(2)

    correlator.mark_ordered(first_key)
    correlator.bind_ordered(first_key, _token(1))
    assert correlator.complete(first_key, _resolution(1)).completed is False
    assert correlator.record_for(first_key) is not None

    correlator.record_final(first_key, _final(first_key, "first", 10.0))
    assert correlator.complete(first_key, _resolution(1)).completed is True
    assert correlator.record_for(first_key) is None
    assert correlator.is_completed(first_key) is True
    assert correlator.completed_tombstone_count == 1
    assert correlator.record_boundary_result(first_key, _exact(first_key, 1)).quality == (
        "unknown"
    )
    assert correlator.record_for(first_key) is None
    with pytest.raises(
        ProviderAliasConflictError,
        match="PROVIDER_KEY_ALREADY_COMPLETED",
    ):
        correlator.mark_ordered(first_key)

    correlator.mark_ordered(second_key)
    correlator.bind_ordered(second_key, _token(2))
    correlator.record_final(second_key, _final(second_key, "second", 11.0))
    assert correlator.complete(second_key, _resolution(2)).completed is True
    assert correlator.completed_tombstone_count == 1
    assert correlator.is_completed(second_key) is True
    assert correlator.is_completed(first_key) is True
    assert correlator.record_boundary_result(first_key, _exact(first_key, 1)).quality == (
        "unknown"
    )
    with pytest.raises(
        ProviderAliasConflictError,
        match="PROVIDER_KEY_ALREADY_COMPLETED",
    ):
        correlator.mark_ordered(first_key)


def test_pending_final_requires_exact_absolute_200ms_budget():
    key = _key(1)
    with pytest.raises(ValueError, match="exactly 200ms"):
        PendingProviderFinal(key, "qwen", "too short", 10.0, 10.0)
    with pytest.raises(ValueError, match="exactly 200ms"):
        PendingProviderFinal(key, "qwen", "too long", 10.0, 40.0)


def test_conflicting_exact_boundary_returns_both_proofs_for_retirement():
    correlator = ProviderTurnCorrelator(namespace=(1, 0))
    key = _key(1)
    first = _exact(key, 1)
    second = ProviderBoundaryResult(
        quality="exact",
        audio_range=ProviderAudioRange(1, 100),
        proof=BoundaryProof(2, 7, key),
    )
    assert correlator.record_boundary_result(key, first).quality == "exact"

    conflict = correlator.record_boundary_result(key, second)

    assert conflict.quality == "unknown"
    assert conflict.retired_proofs == (first.proof, second.proof)
    assert correlator.record_for(key).boundary_result.quality == "unknown"


def test_active_drop_abandons_alias_without_final_and_prevents_resurrection():
    correlator = ProviderTurnCorrelator(namespace=(1, 0))
    key = _key(1)
    token = _token(1)
    exact = _exact(key, 1)
    correlator.record_boundary_result(key, exact)
    correlator.mark_ordered(key)
    correlator.bind_ordered(key, token)

    retired = correlator.abandon_turn(token)

    assert retired.retired is True
    assert retired.provider_keys == (key,)
    assert retired.bound_turn_tokens == (token,)
    assert retired.retired_proofs == (exact.proof,)
    assert correlator.is_completed(key) is True
    assert correlator.record_for(key) is None
    assert correlator.abandon_turn(token).retired is False
    with pytest.raises(ProviderAliasConflictError, match="ALREADY_COMPLETED"):
        correlator.mark_ordered(key)

    successor = _key(2)
    correlator.mark_ordered(successor)
    with pytest.raises(ProviderAliasConflictError, match="VOICE_TURN_ALREADY_BOUND"):
        correlator.bind_ordered(successor, token)


def test_active_drop_can_only_retire_the_oldest_ordered_key():
    correlator = ProviderTurnCorrelator(namespace=(1, 0), completed_capacity=1)
    first_key, second_key = _key(1), _key(2)
    correlator.mark_ordered(first_key)
    correlator.bind_ordered(first_key, _token(1))
    correlator.mark_ordered(second_key)
    correlator.bind_ordered(second_key, _token(2))

    assert correlator.abandon_turn(_token(2)).retired is False
    assert correlator.is_completed(first_key) is False
    assert correlator.record_for(first_key) is not None
    assert correlator.abandon_turn(_token(1)).retired is True
    assert correlator.is_completed(first_key) is True
    assert correlator.abandon_turn(_token(2)).retired is True
    assert correlator.is_completed(second_key) is True


def test_active_drop_retires_earlier_boundary_only_proof_without_leak():
    correlator = ProviderTurnCorrelator(namespace=(1, 0))
    first_key, second_key = _key(1), _key(2)
    first_exact, second_exact = _exact(first_key, 1), _exact(second_key, 2)
    correlator.record_boundary_result(first_key, first_exact)
    correlator.record_boundary_result(second_key, second_exact)
    correlator.mark_ordered(second_key)
    correlator.bind_ordered(second_key, _token(2))

    retired = correlator.abandon_turn(_token(2))

    assert retired.retired is True
    assert retired.provider_keys == (first_key, second_key)
    assert retired.retired_proofs == (first_exact.proof, second_exact.proof)
    assert correlator.record_for(first_key) is None
    assert correlator.record_for(second_key) is None
    assert correlator.is_completed(first_key) is True
    assert correlator.is_completed(second_key) is True


def test_namespace_retirement_fences_every_alias_and_returns_cleanup_ownership():
    correlator = ProviderTurnCorrelator(namespace=(1, 0))
    first_key, second_key = _key(1), _key(2)
    first_exact, second_exact = _exact(first_key, 1), _exact(second_key, 2)
    correlator.record_boundary_result(first_key, first_exact)
    correlator.record_boundary_result(second_key, second_exact)
    correlator.mark_ordered(first_key)
    correlator.bind_ordered(first_key, _token(1))
    correlator.mark_ordered(second_key)
    correlator.bind_ordered(second_key, _token(2))

    retired = correlator.retire_namespace((1, 0))

    assert retired.retired is True
    assert retired.provider_keys == (first_key, second_key)
    assert retired.bound_turn_tokens == (_token(1), _token(2))
    assert retired.retired_proofs == (first_exact.proof, second_exact.proof)
    assert correlator.retire_namespace((1, 0)).retired is False
    late_exact = _exact(_key(3), 3)
    late = correlator.record_boundary_result(_key(3), late_exact)
    assert late.quality == "unknown"
    assert late.retired_proofs == (late_exact.proof,)
    with pytest.raises(ProviderAliasConflictError, match="ALREADY_COMPLETED"):
        correlator.mark_ordered(_key(3))


def test_unknown_boundaries_do_not_consume_optional_proof_or_alias_capacity():
    correlator = ProviderTurnCorrelator(namespace=(1, 0), proof_capacity=1)
    for utterance_id in range(1, 20):
        key = _key(utterance_id)
        result = correlator.record_boundary_result(
            key,
            ProviderBoundaryResult.unknown(),
        )
        assert result.quality == "unknown"
        assert correlator.record_for(key) is None

    exact_key = _key(20)
    assert correlator.record_boundary_result(exact_key, _exact(exact_key, 20)).quality == (
        "exact"
    )


def test_completed_key_and_turn_tombstones_remain_bounded_without_resurrection():
    correlator = ProviderTurnCorrelator(namespace=(1, 0), completed_capacity=1)
    ingress = _token(1).ingress
    for utterance_id in range(1, 20):
        key = _key(utterance_id)
        token = _token(utterance_id)
        correlator.mark_ordered(key)
        correlator.bind_ordered(key, token)
        correlator.record_final(key, _final(key, str(utterance_id), 10.0))
        assert correlator.complete(key, _resolution(utterance_id)).completed is True

    assert correlator.completed_tombstone_count == 1
    assert correlator._retired_turn_high_water == {ingress: 19}
    assert correlator.is_completed(_key(1)) is True
    new_key = _key(20)
    correlator.mark_ordered(new_key)
    with pytest.raises(ProviderAliasConflictError, match="VOICE_TURN_ALREADY_BOUND"):
        correlator.bind_ordered(new_key, _token(1))
