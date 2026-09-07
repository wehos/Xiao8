from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from main_logic.asr_client.lifecycle import (
    FinalKey,
    VoiceIngressToken,
    VoiceTurnToken,
)
from main_logic.asr_client.admission.contracts import AdmissionDisposition
from main_logic.asr_client.transcript import (
    TranscriptDispatcher,
    TranscriptEnvelope,
    TranscriptResolutionOutcome,
    TranscriptTerminalSettlement,
    TranscriptTombstoneCapacityError,
)


pytestmark = pytest.mark.asyncio


def _envelope(turn_id: int, *, audio_generation: int = 4) -> TranscriptEnvelope:
    token = VoiceTurnToken(
        ingress=VoiceIngressToken(1, "socket", 2, 3, audio_generation),
        turn_id=turn_id,
    )
    return TranscriptEnvelope(token, "qwen", f"text-{turn_id}")


async def test_pending_delivery_spans_reservation_queue_and_active_dispatch() -> None:
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def dispatch(_envelope: TranscriptEnvelope) -> None:
        dispatch_started.set()
        await release_dispatch.wait()

    dispatcher = TranscriptDispatcher(dispatch)
    envelope = _envelope(1)

    assert dispatcher.has_pending_delivery is False
    assert dispatcher.try_reserve(envelope.final_key) is True
    assert dispatcher.has_pending_delivery is True

    dispatcher.submit(envelope)
    assert dispatcher.has_pending_delivery is True
    await dispatch_started.wait()
    assert dispatcher.has_pending_delivery is True

    release_dispatch.set()
    await dispatcher.wait_idle()
    assert dispatcher.has_pending_delivery is False


async def test_dispatcher_reserves_capacity_and_serializes_delivery() -> None:
    release_first = asyncio.Event()
    delivered: list[int] = []

    async def dispatch(envelope: TranscriptEnvelope) -> None:
        if envelope.turn_token.turn_id == 1:
            await release_first.wait()
        delivered.append(envelope.turn_token.turn_id)

    dispatcher = TranscriptDispatcher(dispatch, capacity=2)
    first = _envelope(1)
    second = _envelope(2)

    assert dispatcher.try_reserve(first.final_key) is True
    assert dispatcher.try_reserve(second.final_key) is True
    assert dispatcher.try_reserve(_envelope(3).final_key) is False
    dispatcher.submit(first)
    dispatcher.submit(second)
    await asyncio.sleep(0)
    assert delivered == []

    release_first.set()
    await dispatcher.wait_idle()
    assert delivered == [1, 2]


async def test_dispatcher_invalidation_cancels_old_core_work() -> None:
    blocked = asyncio.Event()

    async def wait_forever(_envelope: TranscriptEnvelope) -> None:
        await blocked.wait()

    dispatch = AsyncMock(side_effect=wait_forever)
    dispatcher = TranscriptDispatcher(dispatch)
    envelope = _envelope(1)
    assert dispatcher.try_reserve(envelope.final_key) is True
    dispatcher.submit(envelope)
    await asyncio.sleep(0)

    dispatcher.invalidate_all()
    await dispatcher.wait_idle()

    assert dispatch.await_count == 1


async def test_old_worker_unwind_cannot_clear_new_active_dispatch() -> None:
    old_cancelled = asyncio.Event()
    release_old = asyncio.Event()
    new_started = asyncio.Event()
    release_new = asyncio.Event()

    async def dispatch(envelope: TranscriptEnvelope) -> None:
        if envelope.turn_token.turn_id == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                old_cancelled.set()
                await release_old.wait()
                raise
        new_started.set()
        await release_new.wait()

    dispatcher = TranscriptDispatcher(dispatch, capacity=1)
    old_envelope = _envelope(1)
    new_envelope = _envelope(2)
    third_envelope = _envelope(3)

    assert dispatcher.try_reserve(old_envelope.final_key) is True
    dispatcher.submit(old_envelope)
    await asyncio.sleep(0)
    old_worker = dispatcher._worker

    dispatcher.invalidate_all()
    assert dispatcher.try_reserve(new_envelope.final_key) is True
    dispatcher.submit(new_envelope)
    await asyncio.wait_for(old_cancelled.wait(), 1)
    await asyncio.sleep(0)
    assert new_started.is_set() is False
    assert dispatcher._active is old_envelope

    release_old.set()
    assert old_worker is not None
    await asyncio.wait_for(old_worker, 1)
    await asyncio.wait_for(new_started.wait(), 1)
    assert dispatcher._active is new_envelope

    wait_idle = asyncio.create_task(dispatcher.wait_idle())
    await asyncio.sleep(0)
    assert wait_idle.done() is False
    assert dispatcher.try_reserve(third_envelope.final_key) is False

    assert dispatcher._active is new_envelope
    assert wait_idle.done() is False

    release_new.set()
    await asyncio.wait_for(wait_idle, 1)
    assert dispatcher._active is None


async def test_wait_idle_returns_while_next_turn_slot_is_reserved() -> None:
    # Pins the idle predicate against a plausible-looking "fix": folding
    # self._reservations into _set_idle_if_empty. A live session always holds
    # the next turn's reservation while the previous final drains
    # (runtime.py _handle_independent_asr_final -> _activate_pending_
    # independent_turn -> _prepare_independent_asr_turn), so a reservation-
    # aware predicate never settles and wait_idle() hangs forever.
    dispatcher = TranscriptDispatcher(AsyncMock(), capacity=2)
    first = _envelope(1)
    second = _envelope(2)

    assert dispatcher.try_reserve(first.final_key) is True
    assert dispatcher.try_reserve(second.final_key) is True
    dispatcher.submit(first)

    await asyncio.wait_for(dispatcher.wait_idle(), 1)
    assert second.final_key in dispatcher._reservations


async def test_invalidate_all_from_inside_dispatch_does_not_cancel_its_caller() -> None:
    """Teardown paths run ON the worker; cancelling it truncates their cleanup.

    An independent-ASR final that discovers the session is unusable calls
    `_close_independent_asr()`, which reaches `invalidate_all()` while still
    executing inside `_run()`. Cancelling the current worker there makes the
    very next await raise CancelledError, so the remaining detector/provider
    cleanup and the frontend "session ended" notification never happen.
    """
    steps: list[str] = []
    settlements: list[TranscriptTerminalSettlement] = []
    captured: dict[str, asyncio.Task] = {}

    async def settle_terminal(
        settlement: TranscriptTerminalSettlement,
    ) -> None:
        steps.append("settlement")
        settlements.append(settlement)

    async def dispatch(_envelope: TranscriptEnvelope) -> None:
        steps.append("start")
        captured["worker"] = asyncio.current_task()
        dispatcher.invalidate_all()
        # 收口路径在 invalidate_all 之后还有若干 await —— 它们必须照常跑完。
        await asyncio.sleep(0)
        steps.append("after-await")
        await asyncio.sleep(0)
        steps.append("cleanup-done")

    dispatcher = TranscriptDispatcher(
        dispatch,
        settle_terminal=settle_terminal,
        require_terminal_settlement=True,
    )
    envelope = _envelope(1)
    assert dispatcher.try_reserve(envelope.final_key) is True
    dispatcher.submit(envelope)

    for _ in range(50):
        if "cleanup-done" in steps:
            break
        await asyncio.sleep(0.01)

    await asyncio.wait_for(dispatcher.wait_idle(), 1)
    assert steps == [
        "start",
        "after-await",
        "cleanup-done",
        "settlement",
    ]
    assert len(settlements) == 1
    assert settlements[0].final_key == envelope.final_key
    assert (
        settlements[0].admission_disposition
        is AdmissionDisposition.FORWARD
    )
    assert settlements[0].cleanup_kind == "invalidate_forward"

    # 跑完手头这条之后必须**退出**：再回去 await queue.get() 就成了和新 worker
    # 并存的僵尸，两个消费者抢同一个队列。
    worker = captured["worker"]
    for _ in range(50):
        if worker.done():
            break
        await asyncio.sleep(0.01)
    assert worker.done(), "the self-invalidated worker must exit after its envelope"
    assert not worker.cancelled()


async def test_resolve_reserved_is_exactly_once_for_forward_drop_and_abandon() -> None:
    delivered: list[TranscriptEnvelope] = []
    settlements: list[TranscriptTerminalSettlement] = []

    async def dispatch(envelope: TranscriptEnvelope) -> None:
        delivered.append(envelope)

    async def settle(settlement: TranscriptTerminalSettlement) -> None:
        settlements.append(settlement)

    dispatcher = TranscriptDispatcher(
        dispatch,
        settle_terminal=settle,
        require_terminal_settlement=True,
    )
    forward = _envelope(1)
    dropped = _envelope(2)
    abandoned = _envelope(3)
    for envelope in (forward, dropped, abandoned):
        assert dispatcher.try_reserve(envelope.final_key) is True

    assert dispatcher.resolve_reserved(
        forward.final_key,
        AdmissionDisposition.FORWARD,
        envelope=forward,
    ).outcome is TranscriptResolutionOutcome.APPLIED
    conflict = dispatcher.resolve_reserved(
        forward.final_key,
        AdmissionDisposition.DROP,
    )
    assert conflict.outcome is TranscriptResolutionOutcome.CONFLICT
    assert conflict.existing is AdmissionDisposition.FORWARD
    assert dispatcher.resolve_reserved(
        dropped.final_key,
        AdmissionDisposition.DROP,
    ).outcome is TranscriptResolutionOutcome.APPLIED
    assert dispatcher.resolve_reserved(
        dropped.final_key,
        AdmissionDisposition.DROP,
    ).outcome is TranscriptResolutionOutcome.ALREADY_SAME
    assert dispatcher.resolve_reserved(
        abandoned.final_key,
        AdmissionDisposition.ABANDON,
    ).outcome is TranscriptResolutionOutcome.APPLIED

    await asyncio.wait_for(dispatcher.wait_idle(), 1)
    assert delivered == [forward]
    assert [item.cleanup_kind for item in settlements] == ["drop", "abandon"]
    for envelope in (forward, dropped, abandoned):
        assert dispatcher.try_reserve(envelope.final_key) is False


async def test_out_of_order_resolution_does_not_tombstone_lower_reserved_key() -> None:
    dispatcher = TranscriptDispatcher(AsyncMock(), capacity=2)
    first = _envelope(1)
    second = _envelope(2)
    assert dispatcher.try_reserve(first.final_key) is True
    assert dispatcher.try_reserve(second.final_key) is True

    dispatcher.submit(second)
    assert first.final_key in dispatcher._reservations
    dispatcher.submit(first)
    await asyncio.wait_for(dispatcher.wait_idle(), 1)


async def test_invalidate_preserves_active_queued_terminal_and_reservation_settlement() -> None:
    active_started = asyncio.Event()
    active_cancelled = asyncio.Event()
    settlements: list[TranscriptTerminalSettlement] = []

    async def dispatch(_envelope: TranscriptEnvelope) -> None:
        active_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active_cancelled.set()

    async def settle(settlement: TranscriptTerminalSettlement) -> None:
        settlements.append(settlement)

    dispatcher = TranscriptDispatcher(
        dispatch,
        capacity=3,
        settle_terminal=settle,
        require_terminal_settlement=True,
    )
    active = _envelope(1)
    dropped = _envelope(2)
    unresolved = _envelope(3)
    for envelope in (active, dropped, unresolved):
        assert dispatcher.try_reserve(envelope.final_key) is True
    dispatcher.submit(active)
    await asyncio.wait_for(active_started.wait(), 1)
    assert dispatcher.resolve_reserved(
        dropped.final_key,
        AdmissionDisposition.DROP,
    ).outcome is TranscriptResolutionOutcome.APPLIED

    dispatcher.invalidate_all()
    await asyncio.wait_for(active_cancelled.wait(), 1)
    await asyncio.wait_for(dispatcher.wait_idle(), 1)

    by_key = {item.final_key: item for item in settlements}
    assert len(settlements) == 3
    assert by_key[active.final_key].admission_disposition is AdmissionDisposition.FORWARD
    assert by_key[active.final_key].cleanup_kind == "invalidate_forward"
    assert by_key[dropped.final_key].admission_disposition is AdmissionDisposition.DROP
    assert by_key[dropped.final_key].cleanup_kind == "drop"
    assert (
        by_key[unresolved.final_key].admission_disposition
        is AdmissionDisposition.ABANDON
    )
    assert by_key[unresolved.final_key].cleanup_kind == "abandon"


async def test_invalidate_all_still_cancels_a_worker_from_outside() -> None:
    """The normal identity-barrier use must keep cancelling the worker."""
    started = asyncio.Event()
    finished = asyncio.Event()

    async def dispatch(_envelope: TranscriptEnvelope) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    dispatcher = TranscriptDispatcher(dispatch)
    envelope = _envelope(1)
    assert dispatcher.try_reserve(envelope.final_key) is True
    dispatcher.submit(envelope)
    await asyncio.wait_for(started.wait(), 1.0)

    dispatcher.invalidate_all()

    await asyncio.wait_for(finished.wait(), 1.0)


async def test_resolution_receipt_distinguishes_missing_and_wrong_owner() -> None:
    dispatcher = TranscriptDispatcher(AsyncMock())
    reserved = _envelope(1)
    wrong_owner = _envelope(2)

    assert dispatcher.try_reserve(reserved.final_key)
    missing = dispatcher.resolve_reserved(
        wrong_owner.final_key,
        AdmissionDisposition.DROP,
    )
    assert missing.outcome is TranscriptResolutionOutcome.NOT_RESERVED
    assert missing.existing is None

    mismatch = dispatcher.resolve_reserved(
        reserved.final_key,
        AdmissionDisposition.FORWARD,
        envelope=wrong_owner,
    )
    assert mismatch.outcome is TranscriptResolutionOutcome.OWNER_MISMATCH
    assert reserved.final_key in dispatcher._reservations


async def test_tombstone_capacity_fails_closed_until_retired_transport_watermark() -> None:
    dispatcher = TranscriptDispatcher(
        AsyncMock(),
        capacity=1,
        resolution_tombstone_capacity=1,
    )
    first = _envelope(1)
    second = _envelope(2, audio_generation=5)
    assert dispatcher.try_reserve(first.final_key)
    assert (
        dispatcher.resolve_reserved(first.final_key, AdmissionDisposition.DROP).outcome
        is TranscriptResolutionOutcome.APPLIED
    )
    await dispatcher.wait_idle()

    with pytest.raises(
        TranscriptTombstoneCapacityError,
        match="ASR_TRANSCRIPT_TOMBSTONE_CAPACITY_EXHAUSTED",
    ):
        dispatcher.try_reserve(second.final_key)

    assert dispatcher.retire_resolution(
        first.final_key,
        retired_transport=first.turn_token.ingress,
    )
    assert first.final_key not in dispatcher._resolved
    assert dispatcher.try_reserve(first.final_key) is False
    assert dispatcher.try_reserve(second.final_key)


async def test_terminal_settlement_failure_is_observable_and_not_retirable() -> None:
    failure = RuntimeError("terminal settlement failed")

    async def settle(_settlement: TranscriptTerminalSettlement) -> None:
        raise failure

    dispatcher = TranscriptDispatcher(
        AsyncMock(),
        settle_terminal=settle,
        require_terminal_settlement=True,
    )
    envelope = _envelope(1)
    assert dispatcher.try_reserve(envelope.final_key)
    assert (
        dispatcher.resolve_reserved(envelope.final_key, AdmissionDisposition.DROP).outcome
        is TranscriptResolutionOutcome.APPLIED
    )

    with pytest.raises(RuntimeError, match="terminal settlement failed"):
        await asyncio.wait_for(dispatcher.wait_idle(), 1)
    assert not dispatcher.retire_resolution(
        envelope.final_key,
        retired_transport=envelope.turn_token.ingress,
    )
