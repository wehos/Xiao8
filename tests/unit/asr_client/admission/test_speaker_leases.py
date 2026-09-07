from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.admission.contracts import (
    AdmissionDisposition,
    AdmissionState,
    AbortProviderTransport,
    BoundaryProof,
    BoundaryUnknown,
    BoundaryExact,
    CandidateBindingState,
    CaptureState,
    CountDiagnostic,
    EvidenceState,
    ExactIntervalOutcome,
    ExactIntervalPromotionScope,
    PendingProviderFinal,
    ProviderBindingState,
    ProviderFinalReceived,
    ResolveReserved,
    SettlePartial,
    RejectionCapability,
    RejectionCapabilityKind,
    RouteReplaced,
    SpeakerCaptureLeaseRecord,
    SpeakerCaptureLeaseToken,
    SpeakerLeaseChildBinding,
    SpeakerCheckpointKind,
    SpeakerLeaseAbandoned,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseState,
    SpeakerLeaseTerminalClaim,
    SpeakerLeaseTransitionOutcome,
    SpeakerLeaseTransitionReceipt,
    SpeakerLeaseUnavailable,
    SpeakerAuthorityPending,
)
from main_logic.asr_client.admission.coordinator import (
    AdmissionIdentityError,
    SpeakerLeaseCapacityError,
    VoiceTurnAdmissionCoordinator,
)
from main_logic.asr_client.admission.ingress import (
    AdmissionIngressLane,
)
from main_logic.asr_client.admission.speaker_leases import (
    SpeakerLeaseChildCapacityError,
    SpeakerLeaseTerminalError,
    reduce_speaker_lease,
)
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCandidateKey,
)
from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken


pytestmark = pytest.mark.asyncio


def _lease(nonce: int = 1) -> SpeakerCaptureLeaseToken:
    return SpeakerCaptureLeaseToken(1, 2, 3, 4, nonce)


def _candidate(generation: int = 1) -> SpeakerShadowCandidateKey:
    return SpeakerShadowCandidateKey(4, generation, "provider_candidate")


def _key(utterance_id: int) -> ProviderUtteranceKey:
    return ProviderUtteranceKey(3, 0, utterance_id)


def _turn(turn_id: int) -> VoiceTurnToken:
    return VoiceTurnToken(VoiceIngressToken(1, "socket", 2, 3, 4), turn_id)


def _final(key: ProviderUtteranceKey, text: str) -> PendingProviderFinal:
    return PendingProviderFinal(key, "qwen", text, 10.0, 10.2)


async def test_pure_lease_reducer_requires_ordered_first_then_second_low():
    record = SpeakerCaptureLeaseRecord(_lease(), 1, _candidate())

    record, _ = reduce_speaker_lease(
        record,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    assert record.state is SpeakerLeaseState.FIRST_LOW

    record, _ = reduce_speaker_lease(
        record,
        SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert record.state is SpeakerLeaseState.DENY_LATCHED

    for late in (
        SpeakerLeaseUnavailable(_candidate(), 3),
        SpeakerLeaseCaptureClosed(_candidate(), 2),
        SpeakerLeaseHigh(_candidate(), 3),
        SpeakerLeaseAbandoned(),
    ):
        unchanged, _ = reduce_speaker_lease(record, late)
        assert unchanged is record
        assert unchanged.state is SpeakerLeaseState.DENY_LATCHED


async def test_second_low_without_first_fails_open_and_capture_close_is_terminal():
    record = SpeakerCaptureLeaseRecord(_lease(), 1, _candidate())
    unavailable, _ = reduce_speaker_lease(
        record,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.SECOND),
    )
    assert unavailable.state is SpeakerLeaseState.UNAVAILABLE

    closed, _ = reduce_speaker_lease(
        record,
        SpeakerLeaseCaptureClosed(_candidate(), 0),
    )
    assert closed.state is SpeakerLeaseState.UNAVAILABLE
    assert closed.capture_through_sequence_no == 0


async def test_two_provider_children_share_one_sticky_deny_and_fan_out_in_order():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    # VoiceTurn ids intentionally oppose Provider order; fan-out follows keys.
    first, second = _turn(2), _turn(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(first, lease, _key(1))
    await coordinator.attach_turn_to_speaker_lease(second, lease, _key(2))
    await coordinator.post(first, ProviderFinalReceived(_final(_key(1), "a")))
    await coordinator.post(second, ProviderFinalReceived(_final(_key(2), "b")))

    first_receipt = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    assert first_receipt.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    assert (
        await coordinator.get_record(first)
    ).admission_state is AdmissionState.PENDING
    assert (
        await coordinator.get_record(second)
    ).admission_state is AdmissionState.PENDING

    receipt = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
    )
    assert receipt.outcome is SpeakerLeaseTransitionOutcome.APPLIED
    assert receipt.frozen_children == tuple(
        SpeakerLeaseChildBinding(_key(index), turn)
        for index, turn in ((1, first), (2, second))
    )
    results = receipt.child_results
    assert tuple(result.turn_token for result in results) == (first, second)
    assert all(
        any(
            isinstance(effect, ResolveReserved)
            and effect.disposition is AdmissionDisposition.DROP
            for effect in result.effects
        )
        for result in results
    )
    assert (
        await coordinator.get_record(first)
    ).admission_state is AdmissionState.DROPPED
    assert (
        await coordinator.get_record(second)
    ).admission_state is AdmissionState.DROPPED

    await coordinator.post(first, BoundaryUnknown(_key(1)))
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseUnavailable(_candidate(), 3),
    )
    lease_record = await coordinator.get_speaker_lease(lease)
    assert lease_record is not None
    assert lease_record.state is SpeakerLeaseState.DENY_LATCHED


@pytest.mark.parametrize(
    ("facts", "state", "disposition"),
    (
        (
            (
                SpeakerLeaseHigh(_candidate(), 1),
                SpeakerLeaseCaptureClosed(_candidate(), 1),
            ),
            SpeakerLeaseState.ALLOW,
            AdmissionDisposition.FORWARD,
        ),
        (
            (SpeakerLeaseUnavailable(_candidate(), 1),),
            SpeakerLeaseState.UNAVAILABLE,
            AdmissionDisposition.FORWARD,
        ),
        (
            (SpeakerLeaseAbandoned(),),
            SpeakerLeaseState.ABANDONED,
            AdmissionDisposition.ABANDON,
        ),
    ),
)
async def test_terminal_parent_verdict_resolves_pending_child(
    facts,
    state: SpeakerLeaseState,
    disposition: AdmissionDisposition,
):
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    child = await coordinator.attach_turn_to_speaker_lease(turn, lease, _key(1))
    assert child.speaker_lease_token == lease
    assert child.provider_key == _key(1)
    await coordinator.post(turn, ProviderFinalReceived(_final(_key(1), "hello")))

    receipt = None
    for fact in facts:
        receipt = await coordinator.post_speaker_lease(lease, fact)
    assert receipt is not None
    results = receipt.child_results
    assert len(results) == 1
    resolution = next(
        effect for effect in results[0].effects if isinstance(effect, ResolveReserved)
    )
    assert resolution.disposition is disposition
    assert (await coordinator.get_speaker_lease(lease)).state is state


async def test_attach_is_atomic_idempotent_ordered_and_rejects_abandoned_lease():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    turn = _turn(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    first = await coordinator.attach_turn_to_speaker_lease(turn, lease, _key(1))
    duplicate = await coordinator.attach_turn_to_speaker_lease(turn, lease, _key(1))
    assert duplicate is first

    with pytest.raises(AdmissionIdentityError, match="ALIAS_CONFLICT"):
        await coordinator.attach_turn_to_speaker_lease(turn, lease, _key(2))

    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseAbandoned(),
    )
    with pytest.raises(SpeakerLeaseTerminalError, match="LEASE_TERMINAL"):
        await coordinator.attach_turn_to_speaker_lease(_turn(2), lease, _key(2))


@pytest.mark.parametrize(
    ("parent_events", "evidence", "capture", "disposition"),
    (
        (
            (
                SpeakerLeaseHigh(_candidate(), 1),
                SpeakerLeaseCaptureClosed(_candidate(), 1),
            ),
            EvidenceState.ALLOW,
            CaptureState.COLLECTING,
            AdmissionDisposition.FORWARD,
        ),
        (
            (SpeakerLeaseUnavailable(_candidate(), 1),),
            EvidenceState.UNAVAILABLE,
            CaptureState.UNAVAILABLE,
            AdmissionDisposition.FORWARD,
        ),
        (
            (
                SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
                SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
            ),
            EvidenceState.DENY_LATCHED,
            CaptureState.COLLECTING,
            AdmissionDisposition.DROP,
        ),
    ),
)
async def test_late_child_inherits_terminal_parent_and_resolves_final(
    parent_events,
    evidence: EvidenceState,
    capture: CaptureState,
    disposition: AdmissionDisposition,
):
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    first_turn = _turn(1)
    late_turn = _turn(2)
    first_key = _key(1)
    late_key = _key(2)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(first_turn, lease, first_key)
    for event in parent_events:
        await coordinator.post_speaker_lease(lease, event)

    del evidence, capture, disposition
    with pytest.raises(SpeakerLeaseTerminalError, match="LEASE_TERMINAL"):
        await coordinator.attach_turn_to_speaker_lease(
            late_turn,
            lease,
            late_key,
        )

    assert await coordinator.get_record(late_turn) is None
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert tuple(binding.provider_key for binding in parent.child_bindings) == (
        first_key,
    )


async def test_terminal_parent_late_child_preserves_provider_order():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(_turn(1), lease, _key(2))
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseCaptureClosed(_candidate(), 1),
    )

    with pytest.raises(SpeakerLeaseTerminalError, match="LEASE_TERMINAL"):
        await coordinator.attach_turn_to_speaker_lease(_turn(2), lease, _key(1))

    assert await coordinator.get_record(_turn(2)) is None
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert tuple(binding.provider_key for binding in parent.child_bindings) == (
        _key(2),
    )


async def test_terminal_parent_late_child_preserves_capacity():
    coordinator = VoiceTurnAdmissionCoordinator(speaker_lease_child_capacity=1)
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(_turn(1), lease, _key(1))
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseUnavailable(_candidate(), 1),
    )

    with pytest.raises(SpeakerLeaseTerminalError, match="LEASE_TERMINAL"):
        await coordinator.attach_turn_to_speaker_lease(_turn(2), lease, _key(2))

    assert await coordinator.get_record(_turn(2)) is None


async def test_terminal_parent_late_child_preserves_provider_key_uniqueness():
    coordinator = VoiceTurnAdmissionCoordinator()
    first_lease = _lease(1)
    terminal_lease = _lease(2)
    provider_key = _key(2)
    await coordinator.open_speaker_lease(first_lease, _candidate(1))
    await coordinator.open_speaker_lease(terminal_lease, _candidate(2))
    await coordinator.attach_turn_to_speaker_lease(
        _turn(1),
        first_lease,
        provider_key,
    )
    await coordinator.post_speaker_lease(
        terminal_lease,
        SpeakerLeaseHigh(_candidate(2), 1),
    )
    await coordinator.post_speaker_lease(
        terminal_lease,
        SpeakerLeaseCaptureClosed(_candidate(2), 1),
    )

    with pytest.raises(AdmissionIdentityError, match="KEY_ALREADY_BOUND"):
        await coordinator.attach_turn_to_speaker_lease(
            _turn(2),
            terminal_lease,
            provider_key,
        )

    assert await coordinator.get_record(_turn(2)) is None


async def test_terminal_parent_rejects_exact_empty_placeholder():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseCaptureClosed(_candidate(), 1),
    )
    opened = await coordinator.open_turn(turn)
    await coordinator.post(turn, SpeakerAuthorityPending("provider-arming"))

    with pytest.raises(SpeakerLeaseTerminalError, match="LEASE_TERMINAL"):
        await coordinator.attach_turn_to_speaker_lease(
            turn,
            lease,
            provider_key,
        )
    assert await coordinator.get_record(turn) is not None
    assert (await coordinator.get_record(turn)).record_generation == opened.record_generation


async def test_terminal_parent_rejects_placeholder_with_early_final():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseCaptureClosed(_candidate(), 1),
    )
    await coordinator.open_turn(turn)
    await coordinator.post(
        turn,
        ProviderFinalReceived(_final(provider_key, "early")),
    )
    pending = await coordinator.get_record(turn)
    assert pending is not None

    with pytest.raises(AdmissionIdentityError, match="TERMINAL_BINDING_CONFLICT"):
        await coordinator.attach_turn_to_speaker_lease(
            turn,
            lease,
            provider_key,
        )

    assert await coordinator.get_record(turn) is pending
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.child_bindings == ()
    assert provider_key not in coordinator._provider_speaker_lease_bindings


async def test_attach_upgrades_exact_placeholder_and_preserves_pending_state():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)
    pending_final = _final(provider_key, "held")
    await coordinator.open_speaker_lease(lease, _candidate())
    opened = await coordinator.open_turn(turn)
    await coordinator.post(turn, SpeakerAuthorityPending("provider-arming"))
    await coordinator.post(turn, ProviderFinalReceived(pending_final))
    placeholder = await coordinator.get_record(turn)
    assert placeholder is not None
    assert placeholder.record_generation == opened.record_generation
    assert placeholder.admission_state is AdmissionState.PENDING
    assert placeholder.pending_final is pending_final
    assert placeholder.candidate_binding_state is CandidateBindingState.ARMING

    upgraded = await coordinator.attach_turn_to_speaker_lease(
        turn,
        lease,
        provider_key,
    )

    assert upgraded.record_generation == placeholder.record_generation
    assert upgraded.logical_revision == placeholder.logical_revision + 1
    assert upgraded.admission_state is AdmissionState.PENDING
    assert upgraded.pending_final is pending_final
    assert upgraded.speaker_authority_generation == "provider-arming"
    assert upgraded.provider_binding_state is ProviderBindingState.BOUND
    assert upgraded.candidate_binding_state is CandidateBindingState.BOUND
    assert upgraded.capture_state is CaptureState.COLLECTING
    assert upgraded.provider_key == provider_key
    assert upgraded.speaker_lease_token == lease
    assert upgraded.speaker_candidate == _candidate()
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert tuple(
        (binding.provider_key, binding.turn_token) for binding in parent.child_bindings
    ) == ((provider_key, turn),)

    duplicate = await coordinator.attach_turn_to_speaker_lease(
        turn,
        lease,
        provider_key,
    )
    assert duplicate is upgraded


async def test_attach_rejects_terminal_child_without_partial_parent_binding():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.open_turn(turn, provider_key=provider_key)
    await coordinator.post(turn, ProviderFinalReceived(_final(provider_key, "done")))
    terminal = await coordinator.get_record(turn)
    assert terminal is not None
    assert terminal.admission_state is AdmissionState.FORWARDED

    with pytest.raises(AdmissionIdentityError, match="TERMINAL_BINDING_CONFLICT"):
        await coordinator.attach_turn_to_speaker_lease(turn, lease, provider_key)

    assert await coordinator.get_record(turn) is terminal
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.child_bindings == ()
    assert provider_key not in coordinator._provider_speaker_lease_bindings


async def test_attach_candidate_conflict_does_not_partially_commit_binding():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate(1))
    placeholder = await coordinator.open_turn(
        turn,
        provider_key=provider_key,
        speaker_candidate=_candidate(2),
    )

    with pytest.raises(AdmissionIdentityError, match="ALIAS_CONFLICT"):
        await coordinator.attach_turn_to_speaker_lease(turn, lease, provider_key)

    assert await coordinator.get_record(turn) is placeholder
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.child_bindings == ()
    assert provider_key not in coordinator._provider_speaker_lease_bindings


@pytest.mark.parametrize(
    "malformed_changes",
    (
        {"provider_binding_state": ProviderBindingState.BOUND},
        {
            "provider_binding_state": ProviderBindingState.UNBOUND,
            "provider_key": _key(1),
        },
        {"candidate_binding_state": CandidateBindingState.BOUND},
        {
            "candidate_binding_state": CandidateBindingState.BOUND,
            "capture_state": CaptureState.COLLECTING,
            "speaker_candidate": _candidate(),
        },
        {"candidate_binding_state": CandidateBindingState.ARMING},
        {
            "candidate_binding_state": CandidateBindingState.UNBOUND,
            "speaker_lease_token": _lease(),
        },
        {
            "provider_binding_state": ProviderBindingState.BOUND,
            "provider_key": _key(1),
            "candidate_binding_state": CandidateBindingState.BOUND,
            "capture_state": CaptureState.COLLECTING,
            "speaker_candidate": _candidate(),
            "speaker_lease_token": _lease(),
        },
    ),
)
async def test_attach_rejects_malformed_state_field_combinations(
    malformed_changes,
):
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    placeholder = await coordinator.open_turn(turn)
    malformed = replace(placeholder, **malformed_changes)
    coordinator._records[turn] = malformed

    with pytest.raises(AdmissionIdentityError, match="ALIAS_CONFLICT"):
        await coordinator.attach_turn_to_speaker_lease(turn, lease, provider_key)

    assert await coordinator.get_record(turn) is malformed
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.child_bindings == ()
    assert provider_key not in coordinator._provider_speaker_lease_bindings


async def test_placeholder_upgrade_is_atomic_when_child_capacity_is_exhausted():
    coordinator = VoiceTurnAdmissionCoordinator(
        capacity=3,
        speaker_lease_child_capacity=1,
    )
    lease = _lease(1)
    first_turn = _turn(1)
    placeholder_turn = _turn(2)
    await coordinator.open_speaker_lease(lease, _candidate(1))
    await coordinator.attach_turn_to_speaker_lease(first_turn, lease, _key(1))
    placeholder = await coordinator.open_turn(placeholder_turn)

    with pytest.raises(SpeakerLeaseChildCapacityError, match="CHILD_CAPACITY"):
        await coordinator.attach_turn_to_speaker_lease(
            placeholder_turn,
            lease,
            _key(2),
        )

    assert await coordinator.get_record(placeholder_turn) is placeholder
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert tuple(binding.turn_token for binding in parent.child_bindings) == (
        first_turn,
    )
    assert _key(2) not in coordinator._provider_speaker_lease_bindings


async def test_detach_exact_child_atomically_releases_capacity():
    coordinator = VoiceTurnAdmissionCoordinator(
        capacity=2,
        speaker_lease_child_capacity=1,
    )
    lease = _lease()
    first_turn = _turn(1)
    second_turn = _turn(2)
    first_key = _key(1)
    second_key = _key(2)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(first_turn, lease, first_key)

    assert await coordinator.detach_turn_from_speaker_lease(
        first_turn,
        lease,
        first_key,
    )
    assert await coordinator.get_record(first_turn) is None
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.child_bindings == ()
    assert coordinator._speaker_candidate_bindings[_candidate()] == lease
    assert first_key not in coordinator._provider_speaker_lease_bindings

    replacement = await coordinator.attach_turn_to_speaker_lease(
        second_turn,
        lease,
        second_key,
    )
    assert replacement.turn_token == second_turn


async def test_detach_rejects_identity_conflict_without_touching_replacement():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease(1)
    replacement_lease = _lease(2)
    turn = _turn(1)
    replacement_turn = _turn(2)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate(1))
    await coordinator.open_speaker_lease(replacement_lease, _candidate(2))
    child = await coordinator.attach_turn_to_speaker_lease(
        turn,
        lease,
        provider_key,
    )
    replacement_binding = (replacement_lease, replacement_turn)
    coordinator._provider_speaker_lease_bindings[provider_key] = replacement_binding

    with pytest.raises(AdmissionIdentityError, match="DETACH_IDENTITY_CONFLICT"):
        await coordinator.detach_turn_from_speaker_lease(
            turn,
            lease,
            provider_key,
        )

    assert await coordinator.get_record(turn) is child
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.child_bindings == (runtime_binding := parent.child_bindings[0],)
    assert runtime_binding.provider_key == provider_key
    assert runtime_binding.turn_token == turn
    assert coordinator._provider_speaker_lease_bindings[provider_key] == (
        replacement_binding
    )


@pytest.mark.parametrize("terminal_parent", (False, True))
async def test_detach_rejects_final_or_terminal_binding(terminal_parent: bool):
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    child = await coordinator.attach_turn_to_speaker_lease(
        turn,
        lease,
        provider_key,
    )
    if terminal_parent:
        await coordinator.post_speaker_lease(
            lease,
            SpeakerLeaseHigh(_candidate(), 1),
        )
        await coordinator.post_speaker_lease(
            lease,
            SpeakerLeaseCaptureClosed(_candidate(), 1),
        )
    else:
        await coordinator.post(
            turn,
            ProviderFinalReceived(_final(provider_key, "held")),
        )

    with pytest.raises(AdmissionIdentityError, match="DETACH_ALREADY_COMMITTED"):
        await coordinator.detach_turn_from_speaker_lease(
            turn,
            lease,
            provider_key,
        )

    current = await coordinator.get_record(turn)
    assert current is not None
    assert current is not child
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.child_bindings[0].turn_token == turn
    assert coordinator._provider_speaker_lease_bindings[provider_key] == (
        lease,
        turn,
    )


async def test_detach_missing_and_duplicate_are_idempotent_false():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)

    assert not await coordinator.detach_turn_from_speaker_lease(
        turn,
        lease,
        provider_key,
    )
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(turn, lease, provider_key)
    assert await coordinator.detach_turn_from_speaker_lease(
        turn,
        lease,
        provider_key,
    )
    assert not await coordinator.detach_turn_from_speaker_lease(
        turn,
        lease,
        provider_key,
    )


@pytest.mark.parametrize(
    ("parent_events", "parent_state"),
    (
        (
            (
                SpeakerLeaseHigh(_candidate(), 1),
                SpeakerLeaseCaptureClosed(_candidate(), 1),
            ),
            SpeakerLeaseState.ALLOW,
        ),
        (
            (SpeakerLeaseUnavailable(_candidate(), 1),),
            SpeakerLeaseState.UNAVAILABLE,
        ),
        (
            (
                SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
                SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
            ),
            SpeakerLeaseState.DENY_LATCHED,
        ),
    ),
)
async def test_terminal_parent_rejects_late_child_and_preserves_frozen_siblings(
    parent_events,
    parent_state: SpeakerLeaseState,
):
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    first_turn = _turn(1)
    late_turn = _turn(2)
    first_key = _key(1)
    late_key = _key(2)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(first_turn, lease, first_key)
    for event in parent_events:
        await coordinator.post_speaker_lease(lease, event)
    sibling = await coordinator.get_record(first_turn)
    parent_before = await coordinator.get_speaker_lease(lease)
    assert sibling is not None
    assert parent_before is not None
    with pytest.raises(SpeakerLeaseTerminalError, match="LEASE_TERMINAL"):
        await coordinator.attach_turn_to_speaker_lease(
            late_turn,
            lease,
            late_key,
        )

    assert await coordinator.get_record(late_turn) is None
    assert await coordinator.get_record(first_turn) is sibling
    parent_after = await coordinator.get_speaker_lease(lease)
    assert parent_after is not None
    assert parent_after.state is parent_state
    assert parent_after.terminal_sequence_no == parent_before.terminal_sequence_no
    assert parent_after.child_bindings == parent_before.child_bindings
    assert parent_after.child_bindings[0].turn_token == first_turn
    assert coordinator._speaker_candidate_bindings[_candidate()] == lease
    assert late_key not in coordinator._provider_speaker_lease_bindings


@pytest.mark.parametrize("advance_child", ("final", "boundary"))
async def test_detach_terminal_late_child_rejects_committed_child_state(
    advance_child: str,
):
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(turn, lease, provider_key)
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseCaptureClosed(_candidate(), 1),
    )
    if advance_child == "final":
        await coordinator.post(
            turn,
            ProviderFinalReceived(_final(provider_key, "committed")),
        )
    else:
        await coordinator.post(turn, BoundaryUnknown(provider_key))
    committed = await coordinator.get_record(turn)

    with pytest.raises(AdmissionIdentityError, match="DETACH_ALREADY_COMMITTED"):
        await coordinator.detach_turn_from_speaker_lease(
            turn,
            lease,
            provider_key,
        )

    assert await coordinator.get_record(turn) is committed
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.state is SpeakerLeaseState.ALLOW
    assert parent.child_bindings[0].turn_token == turn
    assert coordinator._provider_speaker_lease_bindings[provider_key] == (
        lease,
        turn,
    )


async def test_detach_terminal_late_child_does_not_touch_replacement_mapping():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease(1)
    replacement_lease = _lease(2)
    turn = _turn(1)
    replacement_turn = _turn(2)
    provider_key = _key(1)
    await coordinator.open_speaker_lease(lease, _candidate(1))
    await coordinator.open_speaker_lease(replacement_lease, _candidate(2))
    child = await coordinator.attach_turn_to_speaker_lease(
        turn,
        lease,
        provider_key,
    )
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseUnavailable(_candidate(1), 1),
    )
    child = await coordinator.get_record(turn)
    replacement = (replacement_lease, replacement_turn)
    coordinator._provider_speaker_lease_bindings[provider_key] = replacement

    with pytest.raises(AdmissionIdentityError, match="DETACH_IDENTITY_CONFLICT"):
        await coordinator.detach_turn_from_speaker_lease(
            turn,
            lease,
            provider_key,
        )

    assert await coordinator.get_record(turn) is child
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.state is SpeakerLeaseState.UNAVAILABLE
    assert parent.child_bindings[0].turn_token == turn
    assert coordinator._provider_speaker_lease_bindings[provider_key] == replacement


async def test_provider_key_cannot_be_attached_to_two_live_leases():
    coordinator = VoiceTurnAdmissionCoordinator()
    first_lease, second_lease = _lease(1), _lease(2)
    await coordinator.open_speaker_lease(first_lease, _candidate(1))
    await coordinator.open_speaker_lease(second_lease, _candidate(2))
    await coordinator.attach_turn_to_speaker_lease(
        _turn(1),
        first_lease,
        _key(1),
    )

    with pytest.raises(AdmissionIdentityError, match="KEY_ALREADY_BOUND"):
        await coordinator.attach_turn_to_speaker_lease(
            _turn(2),
            second_lease,
            _key(1),
        )


async def test_speaker_lease_and_child_capacities_are_strictly_bounded():
    coordinator = VoiceTurnAdmissionCoordinator(
        capacity=16,
        speaker_lease_capacity=8,
        speaker_lease_child_capacity=8,
    )
    for nonce in range(1, 9):
        await coordinator.open_speaker_lease(_lease(nonce), _candidate(nonce))
    with pytest.raises(SpeakerLeaseCapacityError, match="CAPACITY_EXHAUSTED"):
        await coordinator.open_speaker_lease(_lease(9), _candidate(9))

    lease = _lease(1)
    for child in range(1, 9):
        await coordinator.attach_turn_to_speaker_lease(
            _turn(child),
            lease,
            _key(child),
        )
    with pytest.raises(SpeakerLeaseChildCapacityError, match="CHILD_CAPACITY"):
        await coordinator.attach_turn_to_speaker_lease(_turn(9), lease, _key(9))
    assert len((await coordinator.get_speaker_lease(lease)).child_bindings) == 8


async def test_bulk_route_invalidation_abandons_parent_and_child_atomically():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(turn, lease, _key(1))
    await coordinator.post(turn, ProviderFinalReceived(_final(_key(1), "hello")))

    results = await coordinator.invalidate_all(RouteReplaced())

    assert len(results) == 1
    assert (
        await coordinator.get_speaker_lease(lease)
    ).state is SpeakerLeaseState.ABANDONED
    assert (
        await coordinator.get_record(turn)
    ).admission_state is AdmissionState.ABANDONED


async def test_lease_facts_share_single_ingress_worker_and_reserved_capacity():
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lane = AdmissionIngressLane(
        coordinator,
        data_capacity=1,
        control_capacity=4,
        speaker_control_capacity=2,
    )
    await lane.start()
    worker = lane._worker
    lease = _lease()
    turn = _turn(1)
    await lane.open_speaker_lease(lease, _candidate())
    await lane.attach_turn_to_speaker_lease(turn, lease, _key(1))

    await coordinator._lock.acquire()
    try:
        boundary = lane.post_nowait(
            turn,
            BoundaryExact(
                RejectionCapability(
                    capability_id=1,
                    owner_generation=1,
                    kind=RejectionCapabilityKind.SEALED,
                    turn_token=turn,
                    candidate=_candidate(),
                    provider_key=_key(1),
                )
            ),
        )
        first = lane.post_speaker_lease_nowait(
            lease,
            SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
        )
        second = lane.post_speaker_lease_nowait(
            lease,
            SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
        )
        assert lane.pending_data_count == 1
        assert lane.pending_speaker_control_count == 2
        assert lane._worker is worker
    finally:
        coordinator._lock.release()

    await boundary
    assert (await first).outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    receipt = await second
    results = receipt.child_results
    assert tuple(result.turn_token for result in results) == (turn,)
    assert (
        await coordinator.get_record(turn)
    ).admission_state is AdmissionState.DROPPED
    assert lane._worker is worker
    await lane.close()


async def test_terminal_empty_lease_retires_through_same_ingress():
    coordinator = VoiceTurnAdmissionCoordinator()
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    lease = _lease()
    await lane.open_speaker_lease(lease, _candidate())
    await lane.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    await lane.post_speaker_lease(
        lease,
        SpeakerLeaseCaptureClosed(_candidate(), 1),
    )
    assert await lane.retire_speaker_lease(lease) is True
    assert await coordinator.get_speaker_lease(lease) is None
    await lane.close()


async def test_zero_child_deny_returns_terminal_receipt_and_exact_retry_is_idempotent():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    fact = SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND)

    receipt = await coordinator.post_speaker_lease(lease, fact)
    assert receipt.outcome is SpeakerLeaseTransitionOutcome.APPLIED
    assert receipt.after_state is SpeakerLeaseState.DENY_LATCHED
    assert receipt.terminal_sequence_no == 2
    assert receipt.frozen_children == ()
    assert receipt.child_results == ()

    duplicate = await coordinator.post_speaker_lease(lease, fact)
    assert duplicate.outcome is SpeakerLeaseTransitionOutcome.IDEMPOTENT
    assert duplicate.frozen_children == ()
    assert duplicate.child_results == ()

    conflict = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 2),
    )
    assert conflict.outcome is SpeakerLeaseTransitionOutcome.CONFLICT


async def test_high_requires_capture_close_before_allowing_parent() -> None:
    record = SpeakerCaptureLeaseRecord(_lease(), 1, _candidate())

    high_seen, _ = reduce_speaker_lease(
        record,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    allowed, _ = reduce_speaker_lease(
        high_seen,
        SpeakerLeaseCaptureClosed(_candidate(), 1),
    )

    assert high_seen.state is SpeakerLeaseState.HIGH_SEEN
    assert high_seen.terminal_disposition is None
    assert high_seen.terminal_sequence_no is None
    assert allowed.state is SpeakerLeaseState.ALLOW
    assert allowed.terminal_disposition is AdmissionDisposition.FORWARD
    assert allowed.terminal_event == SpeakerLeaseCaptureClosed(_candidate(), 1)


async def test_high_seen_close_repeat_is_exact_and_late_facts_do_not_reopen() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    closed = SpeakerLeaseCaptureClosed(_candidate(), 1)

    applied = await coordinator.post_speaker_lease(lease, closed)
    duplicate = await coordinator.post_speaker_lease(lease, closed)
    conflicting = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    stale = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 2),
    )

    assert applied.outcome is SpeakerLeaseTransitionOutcome.APPLIED
    assert duplicate.outcome is SpeakerLeaseTransitionOutcome.IDEMPOTENT
    assert conflicting.outcome is SpeakerLeaseTransitionOutcome.CONFLICT
    assert stale.outcome is SpeakerLeaseTransitionOutcome.STALE


async def test_legacy_terminal_record_without_exact_event_remains_constructible() -> None:
    legacy = SpeakerCaptureLeaseRecord(
        _lease(),
        1,
        _candidate(),
        state=SpeakerLeaseState.ALLOW,
        logical_revision=1,
        last_speaker_sequence_no=1,
        terminal_sequence_no=1,
        capture_through_sequence_no=1,
    )

    assert legacy.terminal_event is None
    assert legacy.terminal_disposition is AdmissionDisposition.FORWARD
    assert (
        VoiceTurnAdmissionCoordinator._terminal_speaker_event_outcome(
            legacy,
            SpeakerLeaseHigh(_candidate(), 1),
        )
        is SpeakerLeaseTransitionOutcome.IDEMPOTENT
    )


@pytest.mark.parametrize(
    "events",
    (
        (
            SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
            SpeakerLeaseHigh(_candidate(), 2),
        ),
        (
            SpeakerLeaseHigh(_candidate(), 1),
            SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.FIRST),
        ),
    ),
)
async def test_mixed_high_low_evidence_latches_sticky_deny(events) -> None:
    record = SpeakerCaptureLeaseRecord(_lease(), 1, _candidate())

    for event in events:
        record, _ = reduce_speaker_lease(record, event)

    assert record.state is SpeakerLeaseState.MIXED_DENY_LATCHED
    assert record.terminal_disposition is AdmissionDisposition.DROP
    assert record.terminal_event == events[-1]
    late, _ = reduce_speaker_lease(
        record,
        SpeakerLeaseUnavailable(_candidate(), 3),
    )
    assert late is record


async def test_high_seen_backend_unavailable_remains_fail_open() -> None:
    record = SpeakerCaptureLeaseRecord(_lease(), 1, _candidate())
    record, _ = reduce_speaker_lease(
        record,
        SpeakerLeaseHigh(_candidate(), 1),
    )

    record, _ = reduce_speaker_lease(
        record,
        SpeakerLeaseUnavailable(_candidate(), 2),
    )

    assert record.state is SpeakerLeaseState.UNAVAILABLE
    assert record.terminal_disposition is AdmissionDisposition.FORWARD


async def test_mixed_deny_parent_fans_out_drop_to_every_child() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    first, second = _turn(1), _turn(2)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(first, lease, _key(1))
    await coordinator.attach_turn_to_speaker_lease(second, lease, _key(2))
    await coordinator.post(first, ProviderFinalReceived(_final(_key(1), "first")))
    await coordinator.post(second, ProviderFinalReceived(_final(_key(2), "second")))
    high = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    assert high.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL

    receipt = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.FIRST),
    )

    assert receipt.after_state is SpeakerLeaseState.MIXED_DENY_LATCHED
    assert tuple(result.turn_token for result in receipt.child_results) == (
        first,
        second,
    )
    assert all(
        result.speaker_lease_terminal_state
        is SpeakerLeaseState.MIXED_DENY_LATCHED
        for result in receipt.child_results
    )
    assert all(
        any(
            isinstance(effect, ResolveReserved)
            and effect.disposition is AdmissionDisposition.DROP
            for effect in result.effects
        )
        for result in receipt.child_results
    )


async def test_terminal_claim_prepare_is_read_only_and_commit_is_exact_cas() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease = _lease()
    turn = _turn(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.attach_turn_to_speaker_lease(turn, lease, _key(1))
    await coordinator.post(turn, ProviderFinalReceived(_final(_key(1), "pending")))
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    before = await coordinator.get_speaker_lease(lease)
    event = SpeakerLeaseHigh(_candidate(), 2)

    claim = await coordinator.prepare_speaker_lease_transition(lease, event)

    assert type(claim) is SpeakerLeaseTerminalClaim
    assert claim.expected_terminal_state is SpeakerLeaseState.MIXED_DENY_LATCHED
    assert await coordinator.get_speaker_lease(lease) is before
    assert (await coordinator.get_record(turn)).admission_state is AdmissionState.PENDING

    receipt = await coordinator.commit_speaker_lease_terminal_claim(claim)
    replay = await coordinator.commit_speaker_lease_terminal_claim(claim)

    assert receipt.outcome is SpeakerLeaseTransitionOutcome.APPLIED
    assert receipt.after_state is SpeakerLeaseState.MIXED_DENY_LATCHED
    assert replay.outcome is SpeakerLeaseTransitionOutcome.STALE
    assert replay.child_results == ()
    assert (
        await coordinator.get_record(turn)
    ).admission_state is AdmissionState.DROPPED


async def test_terminal_claim_rejects_forged_owner_without_mutation() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    claim = await coordinator.prepare_speaker_lease_transition(
        lease,
        SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.FIRST),
    )
    assert claim is not None
    before = await coordinator.get_speaker_lease(lease)
    forged = replace(claim, _owner=object())

    with pytest.raises(AdmissionIdentityError, match="TERMINAL_CLAIM_INVALID"):
        await coordinator.commit_speaker_lease_terminal_claim(forged)

    assert await coordinator.get_speaker_lease(lease) is before


async def test_terminal_claim_is_stale_after_ordinary_fact_advances_revision() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    claim = await coordinator.prepare_speaker_lease_transition(
        lease,
        SpeakerLeaseHigh(_candidate(), 2),
    )
    assert claim is not None
    advanced = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.FIRST),
    )

    receipt = await coordinator.commit_speaker_lease_terminal_claim(claim)

    assert advanced.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    assert receipt.outcome is SpeakerLeaseTransitionOutcome.STALE
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    assert parent.state is SpeakerLeaseState.FIRST_LOW
    assert parent.last_speaker_sequence_no == 2


async def test_terminal_claim_is_stale_after_record_generation_replacement() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )
    claim = await coordinator.prepare_speaker_lease_transition(
        lease,
        SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.FIRST),
    )
    assert claim is not None
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None
    replacement = replace(parent, record_generation=parent.record_generation + 1)
    coordinator._speaker_leases[lease] = replacement

    receipt = await coordinator.commit_speaker_lease_terminal_claim(claim)

    assert receipt.outcome is SpeakerLeaseTransitionOutcome.STALE
    assert await coordinator.get_speaker_lease(lease) is replacement


async def test_prepare_transition_commits_nonterminal_without_second_post() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())

    receipt = await coordinator.prepare_speaker_lease_transition(
        lease,
        SpeakerLeaseHigh(_candidate(), 1),
    )

    assert isinstance(receipt, SpeakerLeaseTransitionReceipt)
    assert receipt.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    parent = await coordinator.get_speaker_lease(lease)
    assert parent is not None and parent.state is SpeakerLeaseState.HIGH_SEEN


def _exact_scope(
    parent: SpeakerCaptureLeaseRecord,
    child,
    *,
    turn_token: VoiceTurnToken,
    provider_key: ProviderUtteranceKey,
    target: SpeakerShadowCandidateKey,
    successor: SpeakerShadowCandidateKey | None,
) -> ExactIntervalPromotionScope:
    return ExactIntervalPromotionScope(
        parent_lease_token=parent.lease_token,
        parent_record_generation=parent.record_generation,
        expected_parent_logical_revision=parent.logical_revision,
        expected_parent_state=parent.state,
        turn_token=turn_token,
        child_record_generation=child.record_generation,
        expected_child_logical_revision=child.logical_revision,
        provider_key=provider_key,
        boundary_proof=BoundaryProof(1, 1, provider_key),
        target_candidate=target,
        successor_candidate=successor,
    )


async def test_exact_high_tail_promotes_then_close_resolves_only_local_allow() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    high = await coordinator.prepare_speaker_lease_transition(
        lease,
        SpeakerLeaseHigh(target, 1),
    )
    assert isinstance(high, SpeakerLeaseTransitionReceipt)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None

    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )

    assert promoted.outcome is ExactIntervalOutcome.PROMOTED
    assert promoted.receipt is not None
    reset_parent = await coordinator.get_speaker_lease(lease)
    held_child = await coordinator.get_record(turn)
    assert reset_parent is not None
    assert reset_parent.candidate == successor
    assert reset_parent.state is SpeakerLeaseState.COLLECTING
    assert reset_parent.last_speaker_sequence_no == 0
    assert reset_parent.child_bindings == ()
    assert held_child is not None
    assert held_child.exact_interval_hold_id == promoted.receipt.interval_id
    assert held_child.speaker_lease_token is None

    activated = await coordinator.activate_exact_interval(promoted.receipt)
    replay = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.outcome is ExactIntervalOutcome.ACTIVATED
    assert activated.receipt is not None
    assert replay.outcome is ExactIntervalOutcome.STALE
    active_child = await coordinator.get_record(turn)
    assert active_child is not None
    assert active_child.evidence_state is EvidenceState.ALLOW
    assert active_child.terminal_disposition is None

    final_held = await coordinator.post_exact_interval(
        activated.receipt,
        ProviderFinalReceived(_final(key, "owner")),
    )
    assert final_held.outcome is ExactIntervalOutcome.HELD
    closed = await coordinator.post_exact_interval(
        activated.receipt,
        SpeakerLeaseCaptureClosed(target, 1),
    )
    assert closed.outcome is ExactIntervalOutcome.RESOLVED
    assert closed.disposition is AdmissionDisposition.FORWARD
    assert tuple(type(effect) for effect in closed.effects) == (
        SettlePartial,
        ResolveReserved,
    )
    resolved = await coordinator.get_record(turn)
    assert resolved is not None
    assert resolved.admission_state is AdmissionState.FORWARDED
    assert resolved.exact_interval_hold_id is None


async def test_exact_first_low_completion_resolves_drop_without_transport_abort() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    low = await coordinator.prepare_speaker_lease_transition(
        lease,
        SpeakerLeaseLow(target, 1, SpeakerCheckpointKind.FIRST),
    )
    assert isinstance(low, SpeakerLeaseTransitionReceipt)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None
    await coordinator.post_exact_interval(
        activated.receipt,
        ProviderFinalReceived(_final(key, "not owner")),
    )

    denied = await coordinator.post_exact_interval(
        activated.receipt,
        SpeakerLeaseLow(
            target,
            2,
            SpeakerCheckpointKind.COMPLETION_CONFIRMATION,
        ),
    )

    assert denied.outcome is ExactIntervalOutcome.RESOLVED
    assert denied.disposition is AdmissionDisposition.DROP
    assert tuple(type(effect) for effect in denied.effects) == (
        SettlePartial,
        ResolveReserved,
    )
    assert not any(
        isinstance(effect, AbortProviderTransport) for effect in denied.effects
    )
    resolved = await coordinator.get_record(turn)
    assert resolved is not None
    assert resolved.admission_state is AdmissionState.DROPPED


async def test_exact_promotion_rejects_tail_when_parent_has_any_predecessor() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    target, successor = _candidate(1), _candidate(2)
    first, tail = _turn(1), _turn(2)
    first_key, tail_key = _key(1), _key(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(first, lease, first_key)
    await coordinator.attach_turn_to_speaker_lease(tail, lease, tail_key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(tail)
    assert parent is not None and child is not None
    before = parent

    result = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=tail,
            provider_key=tail_key,
            target=target,
            successor=successor,
        )
    )

    assert result.outcome is ExactIntervalOutcome.CONFLICT
    assert result.receipt is None
    assert await coordinator.get_speaker_lease(lease) is before


async def test_exact_promotion_stale_revision_and_parent_terminal_are_noops() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(target, 1),
    )
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    scope = _exact_scope(
        parent,
        child,
        turn_token=turn,
        provider_key=key,
        target=target,
        successor=successor,
    )
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseCaptureClosed(target, 1),
    )

    result = await coordinator.promote_exact_interval_tail_child(scope)

    assert result.outcome is ExactIntervalOutcome.STALE
    assert result.receipt is None


async def test_exact_promotion_wins_before_parent_terminal_fanout() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(target, 1),
    )
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.outcome is ExactIntervalOutcome.PROMOTED

    successor_high = await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(successor, 1),
    )

    assert successor_high.outcome is SpeakerLeaseTransitionOutcome.NON_TERMINAL
    assert successor_high.child_results == ()
    held = await coordinator.get_record(turn)
    assert held is not None and held.exact_interval_hold_id is not None


async def test_exact_activation_is_stale_after_child_revision_changes() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    await coordinator.post(
        turn,
        ProviderFinalReceived(_final(key, "raced")),
        now=10.0,
    )

    activated = await coordinator.activate_exact_interval(promoted.receipt)

    assert activated.outcome is ExactIntervalOutcome.STALE
    assert activated.receipt is None


async def test_exact_target_candidate_is_reserved_until_local_terminal() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    target = _candidate(1)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=None,
        )
    )
    assert promoted.outcome is ExactIntervalOutcome.PROMOTED
    terminal_parent = await coordinator.get_speaker_lease(lease)
    assert terminal_parent is not None
    assert terminal_parent.state is SpeakerLeaseState.ABANDONED
    assert terminal_parent.child_bindings == ()

    with pytest.raises(
        AdmissionIdentityError,
        match="CANDIDATE_EXACT_INTERVAL_HELD",
    ):
        await coordinator.open_speaker_lease(_lease(2), target)


async def test_exact_promotion_abort_restores_parent_child_and_bindings() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseHigh(target, 1),
    )
    parent_before = await coordinator.get_speaker_lease(lease)
    child_before = await coordinator.get_record(turn)
    assert parent_before is not None and child_before is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent_before,
            child_before,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None

    aborted = await coordinator.abort_exact_interval_promotion(promoted.receipt)
    replay = await coordinator.abort_exact_interval_promotion(promoted.receipt)

    assert aborted.outcome is ExactIntervalOutcome.ABORTED
    assert replay.outcome is ExactIntervalOutcome.STALE
    assert await coordinator.get_speaker_lease(lease) is parent_before
    assert await coordinator.get_record(turn) is child_before
    reopened = await coordinator.open_speaker_lease(_lease(2), successor)
    assert reopened.candidate == successor


async def test_exact_promotion_abort_refuses_drift_and_forged_owner() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    forged = replace(promoted.receipt, _owner=object())

    forged_result = await coordinator.abort_exact_interval_promotion(forged)
    assert forged_result.outcome is ExactIntervalOutcome.CONFLICT

    held = await coordinator.get_record(turn)
    assert held is not None
    coordinator._records[turn] = replace(
        held,
        logical_revision=held.logical_revision + 1,
    )
    drifted = await coordinator.abort_exact_interval_promotion(promoted.receipt)
    assert drifted.outcome is ExactIntervalOutcome.CONFLICT


async def test_exact_promotion_abort_after_activation_restores_before_any_fact() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.outcome is ExactIntervalOutcome.ACTIVATED

    aborted = await coordinator.abort_exact_interval_promotion(promoted.receipt)

    assert aborted.outcome is ExactIntervalOutcome.ABORTED
    restored = await coordinator.get_record(turn)
    assert restored is child


async def test_exact_promotion_abort_after_activated_fact_is_conflict() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None
    held = await coordinator.post_exact_interval(
        activated.receipt,
        ProviderFinalReceived(_final(key, "raced")),
    )
    assert held.outcome is ExactIntervalOutcome.HELD

    aborted = await coordinator.abort_exact_interval_promotion(promoted.receipt)

    assert aborted.outcome is ExactIntervalOutcome.CONFLICT


async def test_exact_unavailable_compensation_detaches_hold_and_forwards_final() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None
    first_low = await coordinator.post_exact_interval(
        activated.receipt,
        SpeakerLeaseLow(target, 1, SpeakerCheckpointKind.FIRST),
    )
    assert first_low.outcome is ExactIntervalOutcome.HELD

    unavailable = await coordinator.fail_exact_interval_unavailable(
        activated.receipt
    )
    replay = await coordinator.fail_exact_interval_unavailable(activated.receipt)

    assert unavailable.outcome is ExactIntervalOutcome.ABORTED
    assert replay.outcome is ExactIntervalOutcome.STALE
    compensated = await coordinator.get_record(turn)
    assert compensated is not None
    assert compensated.exact_interval_hold_id is None
    assert compensated.capture_state is CaptureState.UNAVAILABLE
    assert compensated.evidence_state is EvidenceState.UNAVAILABLE
    parent_after = await coordinator.get_speaker_lease(lease)
    assert parent_after is not None
    assert parent_after.candidate == successor
    assert parent_after.state is SpeakerLeaseState.COLLECTING

    effects = await coordinator.post(
        turn,
        ProviderFinalReceived(_final(key, "kept")),
        now=10.0,
    )
    assert any(isinstance(effect, ResolveReserved) for effect in effects)
    resolved = await coordinator.get_record(turn)
    assert resolved is not None
    assert resolved.admission_state is AdmissionState.FORWARDED


async def test_exact_unavailable_compensation_never_rewrites_formal_deny() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target = _candidate(1)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=None,
        )
    )
    assert promoted.receipt is not None
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None
    await coordinator.post_exact_interval(
        activated.receipt,
        SpeakerLeaseLow(target, 1, SpeakerCheckpointKind.FIRST),
    )
    denied = await coordinator.post_exact_interval(
        activated.receipt,
        SpeakerLeaseLow(target, 2, SpeakerCheckpointKind.SECOND),
    )
    assert denied.outcome is ExactIntervalOutcome.HELD

    unavailable = await coordinator.fail_exact_interval_unavailable(
        activated.receipt
    )

    assert unavailable.outcome is ExactIntervalOutcome.CONFLICT
    held = await coordinator.get_record(turn)
    assert held is not None
    assert held.exact_interval_hold_id == activated.receipt.interval_id
    assert held.evidence_state is EvidenceState.DENY_LATCHED


async def test_exact_promotion_requires_matching_boundary_proof_key() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    target = _candidate(1)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None

    with pytest.raises(ValueError, match="boundary proof must match provider_key"):
        ExactIntervalPromotionScope(
            parent_lease_token=parent.lease_token,
            parent_record_generation=parent.record_generation,
            expected_parent_logical_revision=parent.logical_revision,
            expected_parent_state=parent.state,
            turn_token=turn,
            child_record_generation=child.record_generation,
            expected_child_logical_revision=child.logical_revision,
            provider_key=key,
            boundary_proof=BoundaryProof(1, 1, _key(2)),
            target_candidate=target,
            successor_candidate=None,
        )


async def test_exact_abort_restores_source_candidate_when_target_is_refined() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    source, target, successor = _candidate(1), _candidate(2), _candidate(3)
    await coordinator.open_speaker_lease(lease, source)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None

    aborted = await coordinator.abort_exact_interval_promotion(promoted.receipt)

    assert aborted.outcome is ExactIntervalOutcome.ABORTED
    restored = await coordinator.get_speaker_lease(lease)
    assert restored is parent
    assert restored.candidate == source
    with pytest.raises(AdmissionIdentityError, match="CANDIDATE_ALREADY_BOUND"):
        await coordinator.open_speaker_lease(_lease(2), source)
    assert (await coordinator.open_speaker_lease(_lease(3), target)).candidate == target


@pytest.mark.parametrize(
    "event",
    (
        SpeakerLeaseLow(_candidate(1), 1, SpeakerCheckpointKind.FIRST),
        SpeakerLeaseHigh(_candidate(1), 1),
    ),
)
async def test_exact_refined_target_rejects_parent_with_existing_evidence(
    event,
) -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    source, refined, successor = _candidate(1), _candidate(2), _candidate(3)
    await coordinator.open_speaker_lease(lease, source)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    await coordinator.post_speaker_lease(lease, event)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None

    result = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=refined,
            successor=successor,
        )
    )

    assert result.outcome is ExactIntervalOutcome.CONFLICT
    assert result.receipt is None
    assert await coordinator.get_speaker_lease(lease) is parent
    assert await coordinator.get_record(turn) is child


async def test_exact_refined_target_allows_fact_free_collecting_parent() -> None:
    coordinator = VoiceTurnAdmissionCoordinator()
    lease, turn, key = _lease(), _turn(1), _key(1)
    source, refined, successor = _candidate(1), _candidate(2), _candidate(3)
    await coordinator.open_speaker_lease(lease, source)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    assert parent.state is SpeakerLeaseState.COLLECTING
    assert parent.last_speaker_sequence_no == 0

    result = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=refined,
            successor=successor,
        )
    )

    assert result.outcome is ExactIntervalOutcome.PROMOTED
    assert result.receipt is not None
    held = await coordinator.get_record(turn)
    assert held is not None
    assert held.exact_interval_hold_id == result.receipt.interval_id


async def test_exact_terminal_fact_before_final_resolves_when_final_arrives() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(target, 1, SpeakerCheckpointKind.FIRST),
    )
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None
    terminal_held = await coordinator.post_exact_interval(
        activated.receipt,
        SpeakerLeaseLow(
            target,
            2,
            SpeakerCheckpointKind.COMPLETION_CONFIRMATION,
        ),
    )
    assert terminal_held.outcome is ExactIntervalOutcome.HELD

    resolved = await coordinator.post_exact_interval(
        activated.receipt,
        ProviderFinalReceived(_final(key, "deny")),
    )

    assert resolved.outcome is ExactIntervalOutcome.RESOLVED
    assert resolved.disposition is AdmissionDisposition.DROP
    replay = await coordinator.post_exact_interval(
        activated.receipt,
        ProviderFinalReceived(_final(key, "deny")),
    )
    assert replay.outcome is ExactIntervalOutcome.STALE


async def test_exact_first_low_then_close_resolves_local_forward() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(target, 1, SpeakerCheckpointKind.FIRST),
    )
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None
    await coordinator.post_exact_interval(
        activated.receipt,
        ProviderFinalReceived(_final(key, "single low")),
    )

    resolved = await coordinator.post_exact_interval(
        activated.receipt,
        SpeakerLeaseCaptureClosed(target, 1),
    )

    assert resolved.outcome is ExactIntervalOutcome.RESOLVED
    assert resolved.disposition is AdmissionDisposition.FORWARD
    assert tuple(type(effect) for effect in resolved.effects) == (
        SettlePartial,
        ResolveReserved,
    )


async def test_exact_receipt_owners_reject_forged_activation_and_post() -> None:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lease, turn, key = _lease(), _turn(1), _key(1)
    target, successor = _candidate(1), _candidate(2)
    await coordinator.open_speaker_lease(lease, target)
    await coordinator.attach_turn_to_speaker_lease(turn, lease, key)
    parent = await coordinator.get_speaker_lease(lease)
    child = await coordinator.get_record(turn)
    assert parent is not None and child is not None
    promoted = await coordinator.promote_exact_interval_tail_child(
        _exact_scope(
            parent,
            child,
            turn_token=turn,
            provider_key=key,
            target=target,
            successor=successor,
        )
    )
    assert promoted.receipt is not None
    forged_promotion = replace(promoted.receipt, _owner=object())
    rejected_activation = await coordinator.activate_exact_interval(
        forged_promotion
    )
    assert rejected_activation.outcome is ExactIntervalOutcome.CONFLICT

    activated = await coordinator.activate_exact_interval(promoted.receipt)
    assert activated.receipt is not None
    forged_activation = replace(activated.receipt, _owner=object())
    rejected_post = await coordinator.post_exact_interval(
        forged_activation,
        ProviderFinalReceived(_final(key, "forged")),
    )
    assert rejected_post.outcome is ExactIntervalOutcome.CONFLICT
    held = await coordinator.get_record(turn)
    assert held is not None and held.pending_final is None


async def test_attach_and_deny_are_linearized_by_one_coordinator_lock():
    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    turn = _turn(1)
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )

    await coordinator._lock.acquire()
    attach = asyncio.create_task(
        coordinator.attach_turn_to_speaker_lease(turn, lease, _key(1))
    )
    await asyncio.sleep(0)
    deny = asyncio.create_task(
        coordinator.post_speaker_lease(
            lease,
            SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
        )
    )
    await asyncio.sleep(0)
    coordinator._lock.release()
    await attach
    receipt = await deny
    assert tuple(binding.turn_token for binding in receipt.frozen_children) == (turn,)

    coordinator = VoiceTurnAdmissionCoordinator()
    lease = _lease()
    await coordinator.open_speaker_lease(lease, _candidate())
    await coordinator.post_speaker_lease(
        lease,
        SpeakerLeaseLow(_candidate(), 1, SpeakerCheckpointKind.FIRST),
    )
    await coordinator._lock.acquire()
    deny = asyncio.create_task(
        coordinator.post_speaker_lease(
            lease,
            SpeakerLeaseLow(_candidate(), 2, SpeakerCheckpointKind.SECOND),
        )
    )
    await asyncio.sleep(0)
    attach = asyncio.create_task(
        coordinator.attach_turn_to_speaker_lease(turn, lease, _key(1))
    )
    await asyncio.sleep(0)
    coordinator._lock.release()
    receipt = await deny
    assert receipt.frozen_children == ()
    with pytest.raises(SpeakerLeaseTerminalError, match="LEASE_TERMINAL"):
        await attach
