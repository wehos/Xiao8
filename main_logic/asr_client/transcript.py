"""ASR segment aggregation and ordered final-transcript delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Hashable, Literal

from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken

from .admission.contracts import AdmissionDisposition
from .lifecycle import FinalKey


SegmentId = Hashable


@dataclass(frozen=True, slots=True)
class AggregatedTranscript:
    """One completed logical turn and the physical segments it consumed."""

    turn_id: int
    segment_ids: tuple[SegmentId, ...]
    text: str


@dataclass(slots=True)
class _LogicalTurn:
    segment_ids: list[SegmentId] = field(default_factory=list)
    transcripts: dict[SegmentId, str] = field(default_factory=dict)
    complete: bool = False


class SegmentAggregator:
    """Single source of truth for physical-to-logical transcript assembly."""

    def __init__(self) -> None:
        self._turn_id = 0
        self._next_turn_to_publish = 1
        self._turns: dict[int, _LogicalTurn] = {}
        self._segment_turns: dict[SegmentId, int] = {}

    @property
    def turn_id(self) -> int:
        return self._turn_id

    def begin_turn(self, turn_id: int | None = None) -> int:
        next_turn_id = self._turn_id + 1 if turn_id is None else turn_id
        if next_turn_id <= 0:
            raise ValueError("turn_id must be positive")
        if next_turn_id <= self._turn_id:
            raise ValueError("turn_id must increase monotonically")
        for stale_turn_id, stale_turn in tuple(self._turns.items()):
            if not stale_turn.complete:
                self.discard_turn(stale_turn_id)
        self._turn_id = next_turn_id
        self._turns[next_turn_id] = _LogicalTurn()
        self._next_turn_to_publish = min(self._turns)
        return next_turn_id

    def register_segment(self, turn_id: int, segment_id: SegmentId) -> bool:
        turn = self._turns.get(turn_id)
        if turn is None or turn.complete or segment_id in self._segment_turns:
            return False
        turn.segment_ids.append(segment_id)
        self._segment_turns[segment_id] = turn_id
        return True

    def has_segments(self, turn_id: int) -> bool:
        turn = self._turns.get(turn_id)
        return bool(turn and turn.segment_ids)

    def turn_for_segment(self, segment_id: SegmentId) -> int | None:
        return self._segment_turns.get(segment_id)

    def record_transcript(self, segment_id: SegmentId, text: str) -> bool:
        turn_id = self._segment_turns.get(segment_id)
        if turn_id is None:
            return False
        turn = self._turns.get(turn_id)
        if turn is None or segment_id in turn.transcripts:
            return False
        turn.transcripts[segment_id] = " ".join(str(text or "").split())
        return True

    def complete_turn(self, turn_id: int) -> bool:
        turn = self._turns.get(turn_id)
        if turn is None or not turn.segment_ids:
            return False
        turn.complete = True
        return True

    def collect_ready(self) -> list[AggregatedTranscript]:
        ready: list[AggregatedTranscript] = []
        while True:
            turn = self._turns.get(self._next_turn_to_publish)
            if (
                turn is None
                or not turn.complete
                or any(
                    segment_id not in turn.transcripts
                    for segment_id in turn.segment_ids
                )
            ):
                break
            turn_id = self._next_turn_to_publish
            segment_ids = tuple(turn.segment_ids)
            ready.append(
                AggregatedTranscript(
                    turn_id=turn_id,
                    segment_ids=segment_ids,
                    text=" ".join(
                        turn.transcripts[segment_id]
                        for segment_id in segment_ids
                        if turn.transcripts[segment_id]
                    ),
                )
            )
            for segment_id in segment_ids:
                self._segment_turns.pop(segment_id, None)
            self._turns.pop(turn_id, None)
            self._advance_publish_cursor()
        return ready

    def discard_turn(self, turn_id: int) -> None:
        turn = self._turns.pop(turn_id, None)
        if turn is None:
            return
        for segment_id in turn.segment_ids:
            self._segment_turns.pop(segment_id, None)
        if turn_id == self._next_turn_to_publish:
            self._advance_publish_cursor()

    def _advance_publish_cursor(self) -> None:
        self._next_turn_to_publish = min(
            self._turns,
            default=self._turn_id + 1,
        )

    def add_transcript(
        self,
        turn_id: int,
        segment_id: SegmentId,
        text: str,
        *,
        forced_split: bool,
    ) -> str | None:
        """Compatibility helper for callers that submit complete segments."""

        if isinstance(segment_id, int) and segment_id <= 0:
            raise ValueError("segment_id must be positive")
        if turn_id != self._turn_id:
            return None
        self.register_segment(turn_id, segment_id)
        if not self.record_transcript(segment_id, text):
            return None
        if forced_split:
            return None
        self.complete_turn(turn_id)
        ready = self.collect_ready()
        return ready[0].text if ready else None

    def clear(self, *, next_turn_id: int | None = None) -> None:
        self._turns.clear()
        self._segment_turns.clear()
        if next_turn_id is not None:
            if next_turn_id <= 0:
                raise ValueError("next_turn_id must be positive")
            self._turn_id = next_turn_id - 1
            self._next_turn_to_publish = next_turn_id
        else:
            self._next_turn_to_publish = self._turn_id + 1


@dataclass(frozen=True, slots=True)
class TranscriptEnvelope:
    turn_token: VoiceTurnToken
    provider: str
    text: str

    @property
    def final_key(self) -> FinalKey:
        return FinalKey.from_turn(self.turn_token)


TranscriptCleanupKind = Literal["drop", "abandon", "invalidate_forward"]


@dataclass(frozen=True, slots=True)
class TranscriptTerminalSettlement:
    final_key: FinalKey
    admission_disposition: AdmissionDisposition
    cleanup_kind: TranscriptCleanupKind


class TranscriptResolutionOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_SAME = "already_same"
    CONFLICT = "conflict"
    NOT_RESERVED = "not_reserved"
    OWNER_MISMATCH = "owner_mismatch"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TranscriptResolutionReceipt:
    final_key: FinalKey
    requested: AdmissionDisposition
    outcome: TranscriptResolutionOutcome
    existing: AdmissionDisposition | None = None

    def __post_init__(self) -> None:
        if type(self.final_key) is not FinalKey:
            raise TypeError("final_key must be FinalKey")
        if type(self.requested) is not AdmissionDisposition:
            raise TypeError("requested must be AdmissionDisposition")
        if type(self.outcome) is not TranscriptResolutionOutcome:
            raise TypeError("outcome must be TranscriptResolutionOutcome")
        if self.existing is not None and type(self.existing) is not AdmissionDisposition:
            raise TypeError("existing must be AdmissionDisposition or None")


class TranscriptTombstoneCapacityError(RuntimeError):
    """A live resolution tombstone cannot be evicted without reopening history."""


_DispatchQueueItem = TranscriptEnvelope | TranscriptTerminalSettlement


class TranscriptDispatcher:
    """Own bounded final delivery slots and one serial dispatch worker."""

    def __init__(
        self,
        dispatch: Callable[[TranscriptEnvelope], Awaitable[None]],
        *,
        capacity: int = 8,
        settle_terminal: Callable[[TranscriptTerminalSettlement], Awaitable[None]]
        | None = None,
        require_terminal_settlement: bool = False,
        resolution_tombstone_capacity: int = 256,
    ) -> None:
        if capacity <= 0:
            raise ValueError("dispatcher capacity must be positive")
        if require_terminal_settlement and settle_terminal is None:
            raise ValueError("dispatcher terminal settlement callback is required")
        if (
            type(resolution_tombstone_capacity) is not int
            or resolution_tombstone_capacity < capacity
        ):
            raise ValueError(
                "resolution_tombstone_capacity must be an integer at least capacity"
            )
        self._dispatch = dispatch
        self._settle_terminal = settle_terminal
        self._capacity = capacity
        self._resolution_tombstone_capacity = resolution_tombstone_capacity
        self._queue: asyncio.Queue[_DispatchQueueItem] = asyncio.Queue(
            maxsize=capacity
        )
        self._reservations: set[FinalKey] = set()
        self._resolved: dict[FinalKey, AdmissionDisposition] = {}
        self._terminal_settlement_pending: set[FinalKey] = set()
        self._terminal_settlement_failures: dict[FinalKey, Exception] = {}
        self._delivery_pending: set[FinalKey] = set()
        self._retired_transport_watermarks: dict[
            str,
            tuple[int, int, int, int],
        ] = {}
        self._worker: asyncio.Task[None] | None = None
        self._handoff_worker: asyncio.Task[None] | None = None
        self._active: _DispatchQueueItem | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def has_pending_delivery(self) -> bool:
        """Return whether an accepted final still owns delivery priority."""

        return bool(
            self._reservations
            or not self._queue.empty()
            or self._active is not None
        )

    def try_reserve(self, key: FinalKey) -> bool:
        if key in self._resolved:
            return False
        if key in self._reservations:
            return True
        if self._is_retired_transport(key.turn_token.ingress):
            return False
        if len(self._resolved) >= self._resolution_tombstone_capacity:
            raise TranscriptTombstoneCapacityError(
                "ASR_TRANSCRIPT_TOMBSTONE_CAPACITY_EXHAUSTED"
            )
        occupied = (
            len(self._reservations)
            + self._queue.qsize()
            + int(
                self._active is not None
                and self._handoff_worker is None
            )
        )
        if occupied >= self._capacity:
            return False
        self._reservations.add(key)
        return True

    def release(self, key: FinalKey) -> None:
        self._reservations.discard(key)
        self._set_idle_if_empty()

    def submit(self, envelope: TranscriptEnvelope) -> None:
        key = envelope.final_key
        if key not in self._reservations:
            raise RuntimeError("ASR_TRANSCRIPT_SLOT_NOT_RESERVED")
        receipt = self.resolve_reserved(
            key,
            AdmissionDisposition.FORWARD,
            envelope=envelope,
        )
        if receipt.outcome is not TranscriptResolutionOutcome.APPLIED:
            raise RuntimeError("ASR_TRANSCRIPT_SLOT_NOT_RESERVED")

    def resolve_reserved(
        self,
        final_key: FinalKey,
        disposition: AdmissionDisposition,
        *,
        envelope: TranscriptEnvelope | None = None,
    ) -> TranscriptResolutionReceipt:
        """Resolve a reservation with an explicit idempotency/conflict receipt."""

        if type(disposition) is not AdmissionDisposition:
            raise TypeError("ASR_TRANSCRIPT_DISPOSITION_INVALID")
        if disposition is AdmissionDisposition.FORWARD:
            if envelope is None:
                raise ValueError("ASR_TRANSCRIPT_ENVELOPE_INVALID")
            if envelope.final_key != final_key:
                return TranscriptResolutionReceipt(
                    final_key,
                    disposition,
                    TranscriptResolutionOutcome.OWNER_MISMATCH,
                )
        elif envelope is not None:
            raise ValueError("ASR_TRANSCRIPT_TERMINAL_ENVELOPE_FORBIDDEN")
        existing = self._resolved.get(final_key)
        if existing is not None:
            outcome = (
                TranscriptResolutionOutcome.ALREADY_SAME
                if existing is disposition
                else TranscriptResolutionOutcome.CONFLICT
            )
            return TranscriptResolutionReceipt(
                final_key,
                disposition,
                outcome,
                existing,
            )
        if final_key not in self._reservations:
            return TranscriptResolutionReceipt(
                final_key,
                disposition,
                TranscriptResolutionOutcome.NOT_RESERVED,
            )

        # Write the tombstone before relinquishing the reservation. No retry,
        # timeout, or late callback can reserve this FinalKey again.
        self._remember_resolution(final_key, disposition)
        self._reservations.remove(final_key)
        if disposition is AdmissionDisposition.FORWARD:
            assert envelope is not None
            self._queue.put_nowait(envelope)
            self._delivery_pending.add(final_key)
            self._idle.clear()
            self._ensure_worker()
        elif self._settle_terminal is not None:
            self._enqueue_terminal(final_key, disposition)
        else:
            self._set_idle_if_empty()
        return TranscriptResolutionReceipt(
            final_key,
            disposition,
            TranscriptResolutionOutcome.APPLIED,
        )

    def retire_resolution(
        self,
        final_key: FinalKey,
        *,
        retired_transport: VoiceIngressToken,
    ) -> bool:
        """Release a settled tombstone after the caller proves transport retirement."""

        if final_key.turn_token.ingress != retired_transport:
            return False
        if final_key not in self._resolved:
            return False
        if (
            final_key in self._reservations
            or final_key in self._delivery_pending
            or final_key in self._terminal_settlement_pending
            or final_key in self._terminal_settlement_failures
        ):
            return False
        identity = self._transport_identity(retired_transport)
        previous = self._retired_transport_watermarks.get(
            retired_transport.connection_id
        )
        if previous is None or identity > previous:
            self._retired_transport_watermarks[retired_transport.connection_id] = (
                identity
            )
        self._resolved.pop(final_key)
        return True

    def invalidate_all(self) -> None:
        """Seize every slot and preserve one settlement for every owned turn."""

        unresolved = tuple(self._reservations)
        for final_key in unresolved:
            self._remember_resolution(final_key, AdmissionDisposition.ABANDON)
        self._reservations.clear()
        queued: list[_DispatchQueueItem] = []
        while True:
            try:
                queued.append(self._queue.get_nowait())
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        worker = self._worker
        if worker is not None and worker.done():
            worker = None
        if worker is None:
            handoff = self._handoff_worker
            if handoff is not None and not handoff.done():
                worker = handoff
        self._worker = None
        if worker is not None:
            self._handoff_worker = worker

        if self._settle_terminal is not None:
            for item in queued:
                if isinstance(item, TranscriptTerminalSettlement):
                    # Its callback has not run yet; keep the exact terminal
                    # control instead of silently discarding it.
                    self._queue.put_nowait(item)
                else:
                    # Admission was already FORWARD. Invalidation cancels only
                    # pending Core delivery and must not rewrite disposition.
                    self._enqueue_terminal(
                        item.final_key,
                        AdmissionDisposition.FORWARD,
                        cleanup_kind="invalidate_forward",
                    )
            for final_key in unresolved:
                self._enqueue_terminal(
                    final_key,
                    AdmissionDisposition.ABANDON,
                    cleanup_kind="abandon",
                )
            active = self._active
            if (
                worker is not None
                and isinstance(active, TranscriptEnvelope)
            ):
                self._enqueue_terminal(
                    active.final_key,
                    AdmissionDisposition.FORWARD,
                    cleanup_kind="invalidate_forward",
                )
            elif (
                worker is not None
                and worker is not asyncio.current_task()
                and isinstance(active, TranscriptTerminalSettlement)
            ):
                # A terminal cleanup owns Core side effects. Let it finish;
                # cancelling and replaying it could execute those effects twice.
                pass

        if worker is not None and not worker.done():
            # A self-invalidating Core callback must finish its cleanup before
            # a replacement worker starts. External invalidation cancels the
            # old callback, but handoff still waits for its unwind.
            if (
                worker is not asyncio.current_task()
                and not isinstance(self._active, TranscriptTerminalSettlement)
            ):
                worker.cancel()
            self._idle.clear()
            return

        self._handoff_worker = None
        self._active = None
        if self._queue.empty():
            self._idle.set()
        else:
            self._idle.clear()
            self._ensure_worker()

    async def wait_idle(self) -> None:
        """Await dispatch quiescence: no queued and no active envelope.

        This is not "no turn in flight". Outstanding reservations are
        excluded on purpose; see ``_set_idle_if_empty``.
        """

        await self._idle.wait()
        if self._terminal_settlement_failures:
            raise next(iter(self._terminal_settlement_failures.values()))

    def _ensure_worker(self) -> None:
        if (
            self._handoff_worker is not None
            and not self._handoff_worker.done()
        ):
            return
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(
            self._run(),
            name="independent-asr-core-transcript-dispatcher",
        )

    async def _run(self) -> None:
        worker_task = asyncio.current_task()
        try:
            while True:
                item = await self._queue.get()
                self._active = item
                try:
                    if isinstance(item, TranscriptEnvelope):
                        await self._dispatch(item)
                    elif self._settle_terminal is not None:
                        await self._settle_terminal(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Dispatch callbacks own status reporting. Keep the serial
                    # worker alive if a defensive caller still leaks an error.
                    if isinstance(item, TranscriptTerminalSettlement):
                        self._terminal_settlement_failures[item.final_key] = exc
                finally:
                    self._queue.task_done()
                    if (
                        (
                            self._worker is worker_task
                            or self._handoff_worker is worker_task
                        )
                        and self._active is item
                    ):
                        self._active = None
                        if isinstance(item, TranscriptTerminalSettlement):
                            self._terminal_settlement_pending.discard(
                                item.final_key
                            )
                        self._delivery_pending.discard(item.final_key)
                        self._set_idle_if_empty()
                if self._worker is not worker_task:
                    # invalidate_all() 已经把我们从 _worker 上摘掉了（而且刻意没有
                    # 取消调用者自己）。跑完手头这条 envelope 就必须退出 —— 再回去
                    # await self._queue.get() 会变成一个和新 worker 并存的僵尸，
                    # 两个消费者抢同一个队列。
                    return
        except asyncio.CancelledError:
            return
        finally:
            if self._handoff_worker is worker_task:
                self._handoff_worker = None
                if self._worker is None and not self._queue.empty():
                    self._ensure_worker()
                self._set_idle_if_empty()

    def _set_idle_if_empty(self) -> None:
        # Reservations are deliberately NOT part of the idle predicate. A slot
        # is reserved at turn preparation and stays held for the whole live
        # turn, and the next turn reserves its slot while the previous final
        # is still draining. Folding reservations in here would make
        # wait_idle() unsettleable for any back-to-back session.
        if self._queue.empty() and self._active is None:
            self._idle.set()

    def _remember_resolution(
        self,
        final_key: FinalKey,
        disposition: AdmissionDisposition,
    ) -> None:
        if (
            final_key not in self._resolved
            and len(self._resolved) >= self._resolution_tombstone_capacity
        ):
            raise TranscriptTombstoneCapacityError(
                "ASR_TRANSCRIPT_TOMBSTONE_CAPACITY_EXHAUSTED"
            )
        self._resolved[final_key] = disposition

    def _enqueue_terminal(
        self,
        final_key: FinalKey,
        disposition: AdmissionDisposition,
        *,
        cleanup_kind: TranscriptCleanupKind | None = None,
    ) -> None:
        if final_key in self._terminal_settlement_pending:
            return
        if cleanup_kind is None:
            cleanup_kind = (
                "drop"
                if disposition is AdmissionDisposition.DROP
                else "abandon"
            )
        self._terminal_settlement_pending.add(final_key)
        self._delivery_pending.add(final_key)
        self._queue.put_nowait(
            TranscriptTerminalSettlement(
                final_key=final_key,
                admission_disposition=disposition,
                cleanup_kind=cleanup_kind,
            )
        )
        self._idle.clear()
        self._ensure_worker()

    @staticmethod
    def _transport_identity(ingress: VoiceIngressToken) -> tuple[int, int, int, int]:
        return (
            ingress.session_epoch,
            ingress.lease_generation,
            ingress.route_generation,
            ingress.audio_generation,
        )

    def _is_retired_transport(self, ingress: VoiceIngressToken) -> bool:
        watermark = self._retired_transport_watermarks.get(ingress.connection_id)
        return bool(
            watermark is not None
            and self._transport_identity(ingress) <= watermark
        )
