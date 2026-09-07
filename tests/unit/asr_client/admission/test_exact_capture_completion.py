"""Capture completion is a lifecycle fact after an exact child is denied."""

from __future__ import annotations

import pytest

from tests.unit.asr_client.admission.test_exact_installation_authority import (
    _exact_with_first_low,
)
from main_logic.asr_client.admission.contracts import (
    AbortProviderTransport,
    AdmissionDisposition,
    ExactIntervalOutcome,
    PendingProviderFinal,
    ProviderFinalReceived,
    ResolveReserved,
    SpeakerCheckpointKind,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseLow,
)


@pytest.mark.parametrize("kind", [SpeakerCheckpointKind.SECOND, SpeakerCheckpointKind.COMPLETION_CONFIRMATION])
async def test_exact_deny_then_capture_close_waits_for_one_local_drop(kind) -> None:
    coordinator, receipt, target, turn, key = await _exact_with_first_low()
    low = await coordinator.post_exact_interval(receipt, SpeakerLeaseLow(target, 2, kind))
    assert low.outcome is ExactIntervalOutcome.HELD
    child_before = await coordinator.get_record(turn)
    for _ in range(2):
        closed = await coordinator.post_exact_interval(
            receipt, SpeakerLeaseCaptureClosed(target, 2),
            authority_is_current=lambda: False,
        )
        assert closed.outcome is ExactIntervalOutcome.HELD
        assert closed.effects == ()
        assert await coordinator.get_record(turn) is child_before
    final = ProviderFinalReceived(PendingProviderFinal(key, "qwen", "rejected", 10.0, 10.2))
    resolved = await coordinator.post_exact_interval(receipt, final)
    assert resolved.outcome is ExactIntervalOutcome.RESOLVED
    assert resolved.disposition is AdmissionDisposition.DROP
    assert sum(isinstance(effect, ResolveReserved) for effect in resolved.effects) == 1
    assert not any(isinstance(effect, AbortProviderTransport) for effect in resolved.effects)
    replay = await coordinator.post_exact_interval(receipt, final)
    assert replay.outcome is ExactIntervalOutcome.STALE
    assert replay.effects == ()


@pytest.mark.parametrize("through", [0, 1, 3, True])
async def test_exact_deny_does_not_accept_mismatched_capture_fence(through) -> None:
    coordinator, receipt, target, turn, _ = await _exact_with_first_low()
    await coordinator.post_exact_interval(
        receipt, SpeakerLeaseLow(target, 2, SpeakerCheckpointKind.SECOND),
    )
    before = await coordinator.get_record(turn)
    result = await coordinator.post_exact_interval(
        receipt, SpeakerLeaseCaptureClosed(target, through),
    )
    assert result.outcome is ExactIntervalOutcome.STALE
    assert result.effects == ()
    assert await coordinator.get_record(turn) is before
