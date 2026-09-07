"""FIFO ingress for every mutation of the admission coordinator.

The lane is intentionally separate from effect execution.  One worker reduces
events in the exact order in which synchronous producers enqueue them, then
completes a future only after the coordinator lock has been released.
"""

from __future__ import annotations

from .evidence_hold import EVIDENCE_HOLD_EVENT_TYPES
from .contracts import FinalDeadlineExpired

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from main_logic.voice_turn.contracts import VoiceTurnToken

from .._provider_events import ProviderUtteranceKey
from ..speaker_shadow.contracts import SpeakerShadowCandidateKey
from .contracts import (
    AdmissionEffect,
    AdmissionBulkResult,
    AdmissionEvent,
    BoundaryExact,
    CandidateBound,
    CaptureClosed,
    Close,
    ExactIntervalAbortResult,
    ExactIntervalActivationReceipt,
    ExactIntervalActivationResult,
    ExactIntervalPromotionReceipt,
    ExactIntervalPromotionResult,
    ExactIntervalPromotionScope,
    ExactIntervalTransitionReceipt,
    ProviderFinalReceived,
    Reset,
    RouteReplaced,
    SpeakerCaptureLeaseRecord,
    SpeakerCaptureLeaseToken,
    SpeakerLeaseAbandoned,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseEvent,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeasePreparedTransition,
    SpeakerLeaseTerminalClaim,
    SpeakerLeaseUnavailable,
    SpeakerLeaseTransitionOutcome,
    SpeakerLeaseTransitionReceipt,
    SpeakerAuthorityPending,
    SpeakerAuthorityUnavailable,
    SpeakerAuthorityUnarmed,
    SpeakerHigh,
    SpeakerLow,
    SpeakerUnavailable,
    VoiceTurnAdmissionRecord,
)
from .coordinator import VoiceTurnAdmissionCoordinator


_CapacityClass: TypeAlias = Literal["data", "control", "speaker_control"]


class AdmissionIngressCapacityError(RuntimeError):
    """One bounded ingress partition has no remaining reservation."""

    def __init__(
        self,
        turn_token: VoiceTurnToken | None,
        event: AdmissionEvent | SpeakerLeaseEvent | None,
        *,
        speaker_lease_token: SpeakerCaptureLeaseToken | None = None,
        capacity_class: _CapacityClass = "data",
    ) -> None:
        self.turn_token = turn_token
        self.speaker_lease_token = speaker_lease_token
        self.event = event
        self.event_type = type(event)
        self.capacity_class = capacity_class
        super().__init__(
            f"ASR_ADMISSION_INGRESS_{capacity_class.upper()}_CAPACITY_EXHAUSTED"
        )


class AdmissionIngressClosedError(RuntimeError):
    """The admission ingress lane is not accepting new events."""


@dataclass(slots=True)
class _IngressItem:
    turn_token: VoiceTurnToken | None
    speaker_lease_token: SpeakerCaptureLeaseToken | None
    event: AdmissionEvent | SpeakerLeaseEvent | None
    now: float | None
    result: asyncio.Future["_IngressResult"]
    capacity_class: _CapacityClass
    coalescing_key: (
        tuple[
            VoiceTurnToken | SpeakerCaptureLeaseToken | None,
            AdmissionEvent | SpeakerLeaseEvent,
            float | None,
        ]
        | None
    )
    retires_turn: bool = False
    opens_speaker_lease: bool = False
    retires_speaker_lease: bool = False
    attaches_turn_to_speaker_lease: bool = False
    provider_key: ProviderUtteranceKey | None = None
    speaker_candidate: SpeakerShadowCandidateKey | None = None
    terminal_claim: SpeakerLeaseTerminalClaim | None = None
    prepares_speaker_lease_transition: bool = False
    commits_speaker_lease_terminal_claim: bool = False
    exact_operation: (
        Literal["promote", "activate", "abort", "unavailable", "post"] | None
    ) = None
    exact_promotion_scope: ExactIntervalPromotionScope | None = None
    exact_promotion_receipt: ExactIntervalPromotionReceipt | None = None
    exact_activation_receipt: ExactIntervalActivationReceipt | None = None
    exact_authority_is_current: Callable[[], bool] | None = None


_IngressResult: TypeAlias = (
    bool
    | VoiceTurnAdmissionRecord
    | SpeakerCaptureLeaseRecord
    | tuple[AdmissionEffect, ...]
    | tuple[AdmissionBulkResult, ...]
    | SpeakerLeaseTerminalClaim
    | None
    | SpeakerLeaseTransitionReceipt
    | ExactIntervalPromotionResult
    | ExactIntervalActivationResult
    | ExactIntervalAbortResult
    | ExactIntervalTransitionReceipt
)


_DATA_EVENT_TYPES = (BoundaryExact,)
_SPEAKER_CONTROL_EVENT_TYPES = (
    CandidateBound,
    CaptureClosed,
    SpeakerAuthorityPending,
    SpeakerAuthorityUnavailable,
    SpeakerAuthorityUnarmed,
    SpeakerHigh,
    SpeakerLow,
    SpeakerUnavailable,
)
_SPEAKER_LEASE_EVENT_TYPES = (
    SpeakerLeaseAbandoned,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseUnavailable,
)
_EXACT_INTERVAL_EVENT_TYPES = (
    *EVIDENCE_HOLD_EVENT_TYPES,
    FinalDeadlineExpired,
    ProviderFinalReceived,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseUnavailable,
)


class AdmissionIngressLane:
    """Single-consumer admission event lane with control capacity isolation.

    Optional boundary data, general controls, and ordered speaker facts use
    separate finite reservations in one FIFO.  General/data pressure therefore
    cannot consume the slots required for the two authoritative speaker
    checkpoints.  Identical controls are still coalesced, and every queued item
    (including open/retire operations) consumes exactly one bounded slot.
    """

    def __init__(
        self,
        coordinator: VoiceTurnAdmissionCoordinator,
        *,
        data_capacity: int = 64,
        control_capacity: int = 256,
        speaker_control_capacity: int = 128,
    ) -> None:
        if type(coordinator) is not VoiceTurnAdmissionCoordinator:
            raise TypeError("coordinator must be VoiceTurnAdmissionCoordinator")
        if type(data_capacity) is not int or data_capacity <= 0:
            raise ValueError("data_capacity must be a positive integer")
        if type(control_capacity) is not int or control_capacity <= 0:
            raise ValueError("control_capacity must be a positive integer")
        if type(speaker_control_capacity) is not int or speaker_control_capacity <= 0:
            raise ValueError("speaker_control_capacity must be a positive integer")
        self._coordinator = coordinator
        self._data_capacity = data_capacity
        self._control_capacity = control_capacity
        self._speaker_control_capacity = speaker_control_capacity
        self._items: deque[_IngressItem] = deque()
        self._data_pending = 0
        self._control_pending = 0
        self._speaker_control_pending = 0
        self._pending_controls: dict[
            tuple[
                VoiceTurnToken | SpeakerCaptureLeaseToken | None,
                AdmissionEvent | SpeakerLeaseEvent,
                float | None,
            ],
            asyncio.Future[_IngressResult],
        ] = {}
        self._pending_retirements: dict[
            VoiceTurnToken,
            asyncio.Future[_IngressResult],
        ] = {}
        self._pending_speaker_lease_retirements: dict[
            SpeakerCaptureLeaseToken,
            asyncio.Future[_IngressResult],
        ] = {}
        self._available: asyncio.Event | None = None
        self._worker: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closing = False
        self._closed = False

    @property
    def data_capacity(self) -> int:
        return self._data_capacity

    @property
    def pending_data_count(self) -> int:
        return self._data_pending

    @property
    def pending_control_count(self) -> int:
        return self._control_pending + self._speaker_control_pending

    @property
    def control_capacity(self) -> int:
        return self._control_capacity

    @property
    def speaker_control_capacity(self) -> int:
        return self._speaker_control_capacity

    @property
    def pending_speaker_control_count(self) -> int:
        return self._speaker_control_pending

    def _reserve_capacity(
        self,
        capacity_class: _CapacityClass,
        turn_token: VoiceTurnToken | None,
        event: AdmissionEvent | SpeakerLeaseEvent | None,
        *,
        speaker_lease_token: SpeakerCaptureLeaseToken | None = None,
    ) -> None:
        pending = {
            "data": self._data_pending,
            "control": self._control_pending,
            "speaker_control": self._speaker_control_pending,
        }[capacity_class]
        capacity = {
            "data": self._data_capacity,
            "control": self._control_capacity,
            "speaker_control": self._speaker_control_capacity,
        }[capacity_class]
        if pending >= capacity:
            raise AdmissionIngressCapacityError(
                turn_token,
                event,
                speaker_lease_token=speaker_lease_token,
                capacity_class=capacity_class,
            )
        if capacity_class == "data":
            self._data_pending += 1
        elif capacity_class == "control":
            self._control_pending += 1
        else:
            self._speaker_control_pending += 1

    def _release_capacity(self, capacity_class: _CapacityClass) -> None:
        if capacity_class == "data":
            self._data_pending -= 1
        elif capacity_class == "control":
            self._control_pending -= 1
        else:
            self._speaker_control_pending -= 1

    async def start(self) -> None:
        """Bind the lane to the running loop and start its only consumer."""

        if self._closed or self._closing:
            raise AdmissionIngressClosedError("ASR_ADMISSION_INGRESS_CLOSED")
        loop = asyncio.get_running_loop()
        if self._worker is not None:
            if self._loop is not loop:
                raise RuntimeError("ASR_ADMISSION_INGRESS_LOOP_MISMATCH")
            return
        self._loop = loop
        self._available = asyncio.Event()
        self._worker = loop.create_task(
            self._run(),
            name="voice-turn-admission-ingress",
        )

    def post_nowait(
        self,
        turn_token: VoiceTurnToken,
        event: AdmissionEvent,
        *,
        now: float | None = None,
    ) -> asyncio.Future[tuple[AdmissionEffect, ...]]:
        """Append synchronously so callback return cannot reorder two facts."""

        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if self._closing or self._closed:
            raise AdmissionIngressClosedError("ASR_ADMISSION_INGRESS_CLOSED")
        loop = asyncio.get_running_loop()
        if self._worker is None or self._available is None or self._loop is None:
            raise RuntimeError("ASR_ADMISSION_INGRESS_NOT_STARTED")
        if loop is not self._loop:
            raise RuntimeError("ASR_ADMISSION_INGRESS_LOOP_MISMATCH")
        is_data = isinstance(event, _DATA_EVENT_TYPES)
        capacity_class: _CapacityClass = (
            "data"
            if is_data
            else (
                "speaker_control"
                if isinstance(event, _SPEAKER_CONTROL_EVENT_TYPES)
                else "control"
            )
        )
        coalescing_key: (
            tuple[
                VoiceTurnToken | SpeakerCaptureLeaseToken | None,
                AdmissionEvent | SpeakerLeaseEvent,
                float | None,
            ]
            | None
        ) = None
        if not is_data:
            coalescing_key = (turn_token, event, now)
            existing = self._pending_controls.get(coalescing_key)
            if existing is not None:
                follower = self._effectless_follower(existing)
                return cast(asyncio.Future[tuple[AdmissionEffect, ...]], follower)
        self._reserve_capacity(capacity_class, turn_token, event)
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token,
                None,
                event,
                now,
                result,
                capacity_class,
                coalescing_key,
            )
        )
        if not is_data:
            assert coalescing_key is not None
            self._pending_controls[coalescing_key] = result
        self._available.set()
        return cast(asyncio.Future[tuple[AdmissionEffect, ...]], result)

    async def post(
        self,
        turn_token: VoiceTurnToken,
        event: AdmissionEvent,
        *,
        now: float | None = None,
    ) -> tuple[AdmissionEffect, ...]:
        """Await one queued reduction without transferring cancellation ownership."""

        return await asyncio.shield(self.post_nowait(turn_token, event, now=now))

    def open_speaker_lease_nowait(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        candidate: SpeakerShadowCandidateKey,
    ) -> asyncio.Future[SpeakerCaptureLeaseRecord]:
        """Allocate one parent lease through this lane's only worker."""

        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if type(candidate) is not SpeakerShadowCandidateKey:
            raise TypeError("candidate must be SpeakerShadowCandidateKey")
        loop = self._checked_loop()
        self._reserve_capacity(
            "control",
            None,
            None,
            speaker_lease_token=lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=None,
                speaker_lease_token=lease_token,
                event=None,
                now=None,
                result=result,
                capacity_class="control",
                coalescing_key=None,
                opens_speaker_lease=True,
                speaker_candidate=candidate,
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[SpeakerCaptureLeaseRecord], result)

    async def open_speaker_lease(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        candidate: SpeakerShadowCandidateKey,
    ) -> SpeakerCaptureLeaseRecord:
        return await asyncio.shield(
            self.open_speaker_lease_nowait(lease_token, candidate)
        )

    def attach_turn_to_speaker_lease_nowait(
        self,
        turn_token: VoiceTurnToken,
        lease_token: SpeakerCaptureLeaseToken,
        provider_key: ProviderUtteranceKey,
    ) -> asyncio.Future[VoiceTurnAdmissionRecord]:
        """Open and bind one Provider child in FIFO order."""

        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if type(provider_key) is not ProviderUtteranceKey:
            raise TypeError("provider_key must be ProviderUtteranceKey")
        loop = self._checked_loop()
        self._reserve_capacity(
            "control",
            turn_token,
            None,
            speaker_lease_token=lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=turn_token,
                speaker_lease_token=lease_token,
                event=None,
                now=None,
                result=result,
                capacity_class="control",
                coalescing_key=None,
                attaches_turn_to_speaker_lease=True,
                provider_key=provider_key,
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[VoiceTurnAdmissionRecord], result)

    async def attach_turn_to_speaker_lease(
        self,
        turn_token: VoiceTurnToken,
        lease_token: SpeakerCaptureLeaseToken,
        provider_key: ProviderUtteranceKey,
    ) -> VoiceTurnAdmissionRecord:
        return await asyncio.shield(
            self.attach_turn_to_speaker_lease_nowait(
                turn_token,
                lease_token,
                provider_key,
            )
        )

    def post_speaker_lease_nowait(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        event: SpeakerLeaseEvent,
        *,
        now: float | None = None,
    ) -> asyncio.Future[SpeakerLeaseTransitionReceipt]:
        """Append an authoritative parent fact to the speaker-control partition."""

        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if not isinstance(event, _SPEAKER_LEASE_EVENT_TYPES):
            raise TypeError("event must be SpeakerLeaseEvent")
        loop = self._checked_loop()
        coalescing_key = (lease_token, event, now)
        existing = self._pending_controls.get(coalescing_key)
        if existing is not None:
            follower = self._speaker_transition_follower(existing)
            return cast(
                asyncio.Future[SpeakerLeaseTransitionReceipt],
                follower,
            )
        self._reserve_capacity(
            "speaker_control",
            None,
            event,
            speaker_lease_token=lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=None,
                speaker_lease_token=lease_token,
                event=event,
                now=now,
                result=result,
                capacity_class="speaker_control",
                coalescing_key=coalescing_key,
            )
        )
        self._pending_controls[coalescing_key] = result
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[SpeakerLeaseTransitionReceipt], result)

    async def post_speaker_lease(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        event: SpeakerLeaseEvent,
        *,
        now: float | None = None,
    ) -> SpeakerLeaseTransitionReceipt:
        return await asyncio.shield(
            self.post_speaker_lease_nowait(lease_token, event, now=now)
        )

    def prepare_speaker_lease_transition_nowait(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        event: SpeakerLeaseEvent,
        *,
        now: float | None = None,
    ) -> asyncio.Future[SpeakerLeasePreparedTransition]:
        """FIFO-commit ordinary facts or prepare one exact DROP claim."""

        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        if not isinstance(event, _SPEAKER_LEASE_EVENT_TYPES):
            raise TypeError("event must be SpeakerLeaseEvent")
        loop = self._checked_loop()
        self._reserve_capacity(
            "speaker_control",
            None,
            event,
            speaker_lease_token=lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=None,
                speaker_lease_token=lease_token,
                event=event,
                now=now,
                result=result,
                capacity_class="speaker_control",
                coalescing_key=None,
                prepares_speaker_lease_transition=True,
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[SpeakerLeasePreparedTransition], result)

    async def prepare_speaker_lease_transition(
        self,
        lease_token: SpeakerCaptureLeaseToken,
        event: SpeakerLeaseEvent,
        *,
        now: float | None = None,
    ) -> SpeakerLeasePreparedTransition:
        return await asyncio.shield(
            self.prepare_speaker_lease_transition_nowait(
                lease_token,
                event,
                now=now,
            )
        )

    def commit_speaker_lease_terminal_claim_nowait(
        self,
        claim: SpeakerLeaseTerminalClaim,
        *,
        now: float | None = None,
    ) -> asyncio.Future[SpeakerLeaseTransitionReceipt]:
        """FIFO-commit one exact prepared DROP claim under coordinator CAS."""

        if type(claim) is not SpeakerLeaseTerminalClaim:
            raise TypeError("claim must be SpeakerLeaseTerminalClaim")
        loop = self._checked_loop()
        self._reserve_capacity(
            "speaker_control",
            None,
            claim.event,
            speaker_lease_token=claim.lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=None,
                speaker_lease_token=claim.lease_token,
                event=claim.event,
                now=now,
                result=result,
                capacity_class="speaker_control",
                coalescing_key=None,
                terminal_claim=claim,
                commits_speaker_lease_terminal_claim=True,
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[SpeakerLeaseTransitionReceipt], result)

    async def commit_speaker_lease_terminal_claim(
        self,
        claim: SpeakerLeaseTerminalClaim,
        *,
        now: float | None = None,
    ) -> SpeakerLeaseTransitionReceipt:
        return await asyncio.shield(
            self.commit_speaker_lease_terminal_claim_nowait(claim, now=now)
        )

    def promote_exact_interval_nowait(
        self,
        scope: ExactIntervalPromotionScope,
    ) -> asyncio.Future[ExactIntervalPromotionResult]:
        """Queue one atomic tail promotion behind all accepted speaker facts."""

        if type(scope) is not ExactIntervalPromotionScope:
            raise TypeError("scope must be ExactIntervalPromotionScope")
        loop = self._checked_loop()
        self._reserve_capacity(
            "speaker_control",
            scope.turn_token,
            None,
            speaker_lease_token=scope.parent_lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=scope.turn_token,
                speaker_lease_token=scope.parent_lease_token,
                event=None,
                now=None,
                result=result,
                capacity_class="speaker_control",
                coalescing_key=None,
                exact_operation="promote",
                exact_promotion_scope=scope,
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[ExactIntervalPromotionResult], result)

    async def promote_exact_interval(
        self,
        scope: ExactIntervalPromotionScope,
    ) -> ExactIntervalPromotionResult:
        return await asyncio.shield(self.promote_exact_interval_nowait(scope))

    def activate_exact_interval_nowait(
        self,
        receipt: ExactIntervalPromotionReceipt,
    ) -> asyncio.Future[ExactIntervalActivationResult]:
        if type(receipt) is not ExactIntervalPromotionReceipt:
            raise TypeError("receipt must be ExactIntervalPromotionReceipt")
        loop = self._checked_loop()
        self._reserve_capacity(
            "speaker_control",
            receipt.scope.turn_token,
            None,
            speaker_lease_token=receipt.scope.parent_lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=receipt.scope.turn_token,
                speaker_lease_token=receipt.scope.parent_lease_token,
                event=None,
                now=None,
                result=result,
                capacity_class="speaker_control",
                coalescing_key=None,
                exact_operation="activate",
                exact_promotion_receipt=receipt,
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[ExactIntervalActivationResult], result)

    async def activate_exact_interval(
        self,
        receipt: ExactIntervalPromotionReceipt,
    ) -> ExactIntervalActivationResult:
        return await asyncio.shield(self.activate_exact_interval_nowait(receipt))

    def abort_exact_interval_promotion_nowait(
        self,
        receipt: ExactIntervalPromotionReceipt,
    ) -> asyncio.Future[ExactIntervalAbortResult]:
        if type(receipt) is not ExactIntervalPromotionReceipt:
            raise TypeError("receipt must be ExactIntervalPromotionReceipt")
        loop = self._checked_loop()
        self._reserve_capacity(
            "speaker_control",
            receipt.scope.turn_token,
            None,
            speaker_lease_token=receipt.scope.parent_lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=receipt.scope.turn_token,
                speaker_lease_token=receipt.scope.parent_lease_token,
                event=None,
                now=None,
                result=result,
                capacity_class="speaker_control",
                coalescing_key=None,
                exact_operation="abort",
                exact_promotion_receipt=receipt,
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[ExactIntervalAbortResult], result)

    async def abort_exact_interval_promotion(
        self,
        receipt: ExactIntervalPromotionReceipt,
    ) -> ExactIntervalAbortResult:
        return await asyncio.shield(
            self.abort_exact_interval_promotion_nowait(receipt)
        )

    def fail_exact_interval_unavailable_nowait(
        self,
        receipt: ExactIntervalPromotionReceipt | ExactIntervalActivationReceipt,
    ) -> asyncio.Future[ExactIntervalAbortResult]:
        """Queue fail-open compensation for one exact ownership token."""

        if not isinstance(
            receipt,
            (ExactIntervalPromotionReceipt, ExactIntervalActivationReceipt),
        ):
            raise TypeError(
                "receipt must be ExactIntervalPromotionReceipt or "
                "ExactIntervalActivationReceipt"
            )
        turn_token = (
            receipt.scope.turn_token
            if isinstance(receipt, ExactIntervalPromotionReceipt)
            else receipt.turn_token
        )
        lease_token = (
            receipt.scope.parent_lease_token
            if isinstance(receipt, ExactIntervalPromotionReceipt)
            else None
        )
        loop = self._checked_loop()
        self._reserve_capacity(
            "speaker_control",
            turn_token,
            None,
            speaker_lease_token=lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=turn_token,
                speaker_lease_token=lease_token,
                event=None,
                now=None,
                result=result,
                capacity_class="speaker_control",
                coalescing_key=None,
                exact_operation="unavailable",
                exact_promotion_receipt=(
                    receipt
                    if isinstance(receipt, ExactIntervalPromotionReceipt)
                    else None
                ),
                exact_activation_receipt=(
                    receipt
                    if isinstance(receipt, ExactIntervalActivationReceipt)
                    else None
                ),
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[ExactIntervalAbortResult], result)

    async def fail_exact_interval_unavailable(
        self,
        receipt: ExactIntervalPromotionReceipt | ExactIntervalActivationReceipt,
    ) -> ExactIntervalAbortResult:
        return await asyncio.shield(
            self.fail_exact_interval_unavailable_nowait(receipt)
        )

    def post_exact_interval_nowait(
        self,
        receipt: ExactIntervalActivationReceipt,
        event: SpeakerLeaseEvent | ProviderFinalReceived,
        *,
        authority_is_current: Callable[[], bool] | None = None,
    ) -> asyncio.Future[ExactIntervalTransitionReceipt]:
        """Queue one exact child fact in the shared bounded speaker FIFO."""

        if type(receipt) is not ExactIntervalActivationReceipt:
            raise TypeError("receipt must be ExactIntervalActivationReceipt")
        if not isinstance(event, _EXACT_INTERVAL_EVENT_TYPES):
            raise TypeError("event must be an exact interval fact")
        loop = self._checked_loop()
        self._reserve_capacity(
            "speaker_control",
            receipt.turn_token,
            event,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=receipt.turn_token,
                speaker_lease_token=None,
                event=event,
                now=None,
                result=result,
                capacity_class="speaker_control",
                coalescing_key=None,
                exact_operation="post",
                exact_activation_receipt=receipt,
                exact_authority_is_current=authority_is_current,
            )
        )
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[ExactIntervalTransitionReceipt], result)

    async def post_exact_interval(
        self,
        receipt: ExactIntervalActivationReceipt,
        event: SpeakerLeaseEvent | ProviderFinalReceived,
        *,
        authority_is_current: Callable[[], bool] | None = None,
    ) -> ExactIntervalTransitionReceipt:
        return await asyncio.shield(
            self.post_exact_interval_nowait(
                receipt, event, authority_is_current=authority_is_current
            )
        )

    def _speaker_transition_follower(
        self,
        leader: asyncio.Future[_IngressResult],
    ) -> asyncio.Future[SpeakerLeaseTransitionReceipt]:
        """Join an exact parent fact without duplicating child effect ownership."""

        assert self._loop is not None
        follower: asyncio.Future[SpeakerLeaseTransitionReceipt] = (
            self._loop.create_future()
        )

        def transfer_result(completed: asyncio.Future[_IngressResult]) -> None:
            if follower.done():
                return
            if completed.cancelled():
                follower.cancel()
                return
            error = completed.exception()
            if error is not None:
                follower.set_exception(error)
                return
            receipt = completed.result()
            if not isinstance(receipt, SpeakerLeaseTransitionReceipt):
                follower.set_exception(
                    RuntimeError("ASR_ADMISSION_SPEAKER_RECEIPT_INVALID")
                )
                return
            follower.set_result(
                SpeakerLeaseTransitionReceipt(
                    lease_token=receipt.lease_token,
                    before_state=receipt.before_state,
                    after_state=receipt.after_state,
                    outcome=(
                        SpeakerLeaseTransitionOutcome.IDEMPOTENT
                        if receipt.terminal_sequence_no is not None
                        else receipt.outcome
                    ),
                    terminal_sequence_no=receipt.terminal_sequence_no,
                    capture_through_sequence_no=receipt.capture_through_sequence_no,
                    frozen_children=receipt.frozen_children,
                    child_results=(),
                    diagnostics=(),
                )
            )

        leader.add_done_callback(transfer_result)
        return follower

    def retire_speaker_lease_nowait(
        self,
        lease_token: SpeakerCaptureLeaseToken,
    ) -> asyncio.Future[bool]:
        if type(lease_token) is not SpeakerCaptureLeaseToken:
            raise TypeError("lease_token must be SpeakerCaptureLeaseToken")
        loop = self._checked_loop()
        existing = self._pending_speaker_lease_retirements.get(lease_token)
        if existing is not None:
            return self._boolean_follower(existing)
        self._reserve_capacity(
            "control",
            None,
            None,
            speaker_lease_token=lease_token,
        )
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token=None,
                speaker_lease_token=lease_token,
                event=None,
                now=None,
                result=result,
                capacity_class="control",
                coalescing_key=None,
                retires_speaker_lease=True,
            )
        )
        self._pending_speaker_lease_retirements[lease_token] = result
        assert self._available is not None
        self._available.set()
        return cast(asyncio.Future[bool], result)

    async def retire_speaker_lease(
        self,
        lease_token: SpeakerCaptureLeaseToken,
    ) -> bool:
        return await asyncio.shield(self.retire_speaker_lease_nowait(lease_token))

    def _checked_loop(self) -> asyncio.AbstractEventLoop:
        if self._closing or self._closed:
            raise AdmissionIngressClosedError("ASR_ADMISSION_INGRESS_CLOSED")
        loop = asyncio.get_running_loop()
        if self._worker is None or self._available is None or self._loop is None:
            raise RuntimeError("ASR_ADMISSION_INGRESS_NOT_STARTED")
        if loop is not self._loop:
            raise RuntimeError("ASR_ADMISSION_INGRESS_LOOP_MISMATCH")
        return loop

    def open_turn_nowait(
        self,
        turn_token: VoiceTurnToken,
    ) -> asyncio.Future[VoiceTurnAdmissionRecord]:
        """Allocate one admission record in the same FIFO as every fact."""

        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if self._closing or self._closed:
            raise AdmissionIngressClosedError("ASR_ADMISSION_INGRESS_CLOSED")
        loop = asyncio.get_running_loop()
        if self._worker is None or self._available is None or self._loop is None:
            raise RuntimeError("ASR_ADMISSION_INGRESS_NOT_STARTED")
        if loop is not self._loop:
            raise RuntimeError("ASR_ADMISSION_INGRESS_LOOP_MISMATCH")
        self._reserve_capacity("control", turn_token, None)
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(turn_token, None, None, None, result, "control", None)
        )
        self._available.set()
        return cast(asyncio.Future[VoiceTurnAdmissionRecord], result)

    async def open_turn(
        self,
        turn_token: VoiceTurnToken,
    ) -> VoiceTurnAdmissionRecord:
        """Await FIFO record allocation without transferring cancellation."""

        return await asyncio.shield(self.open_turn_nowait(turn_token))

    def retire_turn_nowait(
        self,
        turn_token: VoiceTurnToken,
    ) -> asyncio.Future[bool]:
        """Queue one idempotent settled-record retirement in the same FIFO."""

        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if self._closing or self._closed:
            raise AdmissionIngressClosedError("ASR_ADMISSION_INGRESS_CLOSED")
        loop = asyncio.get_running_loop()
        if self._worker is None or self._available is None or self._loop is None:
            raise RuntimeError("ASR_ADMISSION_INGRESS_NOT_STARTED")
        if loop is not self._loop:
            raise RuntimeError("ASR_ADMISSION_INGRESS_LOOP_MISMATCH")
        existing = self._pending_retirements.get(turn_token)
        if existing is not None:
            follower: asyncio.Future[bool] = loop.create_future()

            def transfer_result(completed: asyncio.Future[_IngressResult]) -> None:
                if follower.done():
                    return
                if completed.cancelled():
                    follower.cancel()
                    return
                error = completed.exception()
                if error is not None:
                    follower.set_exception(error)
                else:
                    follower.set_result(bool(completed.result()))

            existing.add_done_callback(transfer_result)
            return follower
        self._reserve_capacity("control", turn_token, None)
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                turn_token,
                None,
                None,
                None,
                result,
                "control",
                None,
                retires_turn=True,
            )
        )
        self._pending_retirements[turn_token] = result
        self._available.set()
        return cast(asyncio.Future[bool], result)

    async def retire_turn(self, turn_token: VoiceTurnToken) -> bool:
        """Await one FIFO retirement check without transferring cancellation."""

        return await asyncio.shield(self.retire_turn_nowait(turn_token))

    def invalidate_all_nowait(
        self,
        event: Reset | Close | RouteReplaced,
        *,
        now: float | None = None,
    ) -> asyncio.Future[tuple[AdmissionBulkResult, ...]]:
        """Enqueue one bulk route fence in the same FIFO as per-turn facts."""

        if type(event) not in {Reset, Close, RouteReplaced}:
            raise TypeError("event must be Reset, Close, or RouteReplaced")
        if self._closing or self._closed:
            raise AdmissionIngressClosedError("ASR_ADMISSION_INGRESS_CLOSED")
        loop = asyncio.get_running_loop()
        if self._worker is None or self._available is None or self._loop is None:
            raise RuntimeError("ASR_ADMISSION_INGRESS_NOT_STARTED")
        if loop is not self._loop:
            raise RuntimeError("ASR_ADMISSION_INGRESS_LOOP_MISMATCH")
        coalescing_key: tuple[
            VoiceTurnToken | None,
            AdmissionEvent,
            float | None,
        ] = (None, event, now)
        existing = self._pending_controls.get(coalescing_key)
        if existing is not None:
            follower = self._effectless_follower(existing)
            return cast(asyncio.Future[tuple[AdmissionBulkResult, ...]], follower)
        self._reserve_capacity("control", None, event)
        result: asyncio.Future[_IngressResult] = loop.create_future()
        self._items.append(
            _IngressItem(
                None,
                None,
                event,
                now,
                result,
                "control",
                coalescing_key,
            )
        )
        self._pending_controls[coalescing_key] = result
        self._available.set()
        return cast(asyncio.Future[tuple[AdmissionBulkResult, ...]], result)

    async def invalidate_all(
        self,
        event: Reset | Close | RouteReplaced,
        *,
        now: float | None = None,
    ) -> tuple[AdmissionBulkResult, ...]:
        """Await a bulk route fence without bypassing ingress ordering."""

        return await asyncio.shield(self.invalidate_all_nowait(event, now=now))

    def _effectless_follower(
        self,
        leader: asyncio.Future[_IngressResult],
    ) -> asyncio.Future[_IngressResult]:
        """Follow one coalesced reduction without acquiring its effect ownership."""

        assert self._loop is not None
        follower: asyncio.Future[_IngressResult] = self._loop.create_future()

        def transfer_result(completed: asyncio.Future[_IngressResult]) -> None:
            if follower.done():
                return
            if completed.cancelled():
                follower.cancel()
                return
            error = completed.exception()
            if error is not None:
                follower.set_exception(error)
            else:
                follower.set_result(())

        leader.add_done_callback(transfer_result)
        return follower

    def _boolean_follower(
        self,
        leader: asyncio.Future[_IngressResult],
    ) -> asyncio.Future[bool]:
        """Follow an idempotent retirement without cancelling its owner."""

        assert self._loop is not None
        follower: asyncio.Future[bool] = self._loop.create_future()

        def transfer_result(completed: asyncio.Future[_IngressResult]) -> None:
            if follower.done():
                return
            if completed.cancelled():
                follower.cancel()
                return
            error = completed.exception()
            if error is not None:
                follower.set_exception(error)
            else:
                follower.set_result(bool(completed.result()))

        leader.add_done_callback(transfer_result)
        return follower

    async def close(self) -> None:
        """Stop accepting events, drain the FIFO, and join the worker."""

        if self._closed:
            return
        if self._worker is None or self._available is None:
            self._closing = True
            self._closed = True
            return
        self._closing = True
        self._available.set()
        worker = self._worker
        if worker is asyncio.current_task():
            raise RuntimeError("ASR_ADMISSION_INGRESS_SELF_CLOSE")
        await asyncio.shield(worker)

    async def _run(self) -> None:
        assert self._available is not None
        try:
            while True:
                await self._available.wait()
                while self._items:
                    item = self._items.popleft()
                    try:
                        if item.exact_operation == "promote":
                            assert item.exact_promotion_scope is not None
                            effects = (
                                await self._coordinator.promote_exact_interval_tail_child(
                                    item.exact_promotion_scope
                                )
                            )
                        elif item.exact_operation == "activate":
                            assert item.exact_promotion_receipt is not None
                            effects = await self._coordinator.activate_exact_interval(
                                item.exact_promotion_receipt
                            )
                        elif item.exact_operation == "abort":
                            assert item.exact_promotion_receipt is not None
                            effects = (
                                await self._coordinator.abort_exact_interval_promotion(
                                    item.exact_promotion_receipt
                                )
                            )
                        elif item.exact_operation == "unavailable":
                            unavailable_receipt = (
                                item.exact_activation_receipt
                                or item.exact_promotion_receipt
                            )
                            assert unavailable_receipt is not None
                            effects = (
                                await self._coordinator.fail_exact_interval_unavailable(
                                    unavailable_receipt
                                )
                            )
                        elif item.exact_operation == "post":
                            assert item.exact_activation_receipt is not None
                            assert item.event is not None
                            effects = await self._coordinator.post_exact_interval(
                                item.exact_activation_receipt,
                                cast(
                                    SpeakerLeaseEvent | ProviderFinalReceived,
                                    item.event,
                                ),
                                authority_is_current=item.exact_authority_is_current,
                            )
                        elif item.opens_speaker_lease:
                            assert item.speaker_lease_token is not None
                            assert item.speaker_candidate is not None
                            assert item.event is None
                            effects = await self._coordinator.open_speaker_lease(
                                item.speaker_lease_token,
                                item.speaker_candidate,
                            )
                        elif item.retires_speaker_lease:
                            assert item.speaker_lease_token is not None
                            assert item.event is None
                            effects = await self._coordinator.retire_speaker_lease(
                                item.speaker_lease_token,
                            )
                        elif item.attaches_turn_to_speaker_lease:
                            assert item.turn_token is not None
                            assert item.speaker_lease_token is not None
                            assert item.provider_key is not None
                            assert item.event is None
                            effects = (
                                await self._coordinator.attach_turn_to_speaker_lease(
                                    item.turn_token,
                                    item.speaker_lease_token,
                                    item.provider_key,
                                )
                            )
                        elif item.retires_turn:
                            assert item.turn_token is not None
                            assert item.event is None
                            effects = await self._coordinator.retire(item.turn_token)
                        elif item.prepares_speaker_lease_transition:
                            assert item.speaker_lease_token is not None
                            assert item.event is not None
                            effects = (
                                await self._coordinator.prepare_speaker_lease_transition(
                                    item.speaker_lease_token,
                                    cast(SpeakerLeaseEvent, item.event),
                                    now=item.now,
                                )
                            )
                        elif item.commits_speaker_lease_terminal_claim:
                            assert item.terminal_claim is not None
                            effects = (
                                await self._coordinator.commit_speaker_lease_terminal_claim(
                                    item.terminal_claim,
                                    now=item.now,
                                )
                            )
                        elif item.speaker_lease_token is not None:
                            assert item.event is not None
                            lease_event = cast(SpeakerLeaseEvent, item.event)
                            effects = await self._coordinator.post_speaker_lease(
                                item.speaker_lease_token,
                                lease_event,
                                now=item.now,
                            )
                        elif item.turn_token is None:
                            assert type(item.event) in {Reset, Close, RouteReplaced}
                            bulk_event = cast(
                                Reset | Close | RouteReplaced,
                                item.event,
                            )
                            effects = await self._coordinator.invalidate_all(
                                bulk_event,
                                now=item.now,
                            )
                        elif item.event is None:
                            effects = await self._coordinator.open_turn(item.turn_token)
                        else:
                            effects = await self._coordinator.post(
                                item.turn_token,
                                item.event,
                                now=item.now,
                            )
                    except Exception as exc:
                        if not item.result.done():
                            item.result.set_exception(exc)
                    else:
                        if not item.result.done():
                            item.result.set_result(effects)
                    finally:
                        self._release_capacity(item.capacity_class)
                        if item.retires_turn:
                            assert item.turn_token is not None
                            self._pending_retirements.pop(item.turn_token, None)
                        if item.retires_speaker_lease:
                            assert item.speaker_lease_token is not None
                            self._pending_speaker_lease_retirements.pop(
                                item.speaker_lease_token,
                                None,
                            )
                        if item.coalescing_key is not None:
                            self._pending_controls.pop(item.coalescing_key, None)
                self._available.clear()
                if self._closing:
                    return
        finally:
            self._closed = True
            self._closing = True
            error = AdmissionIngressClosedError("ASR_ADMISSION_INGRESS_CLOSED")
            while self._items:
                item = self._items.popleft()
                self._release_capacity(item.capacity_class)
                if item.coalescing_key is not None:
                    self._pending_controls.pop(item.coalescing_key, None)
                if item.retires_turn:
                    assert item.turn_token is not None
                    self._pending_retirements.pop(item.turn_token, None)
                if item.retires_speaker_lease:
                    assert item.speaker_lease_token is not None
                    self._pending_speaker_lease_retirements.pop(
                        item.speaker_lease_token,
                        None,
                    )
                if not item.result.done():
                    item.result.set_exception(error)


__all__ = [
    "AdmissionIngressCapacityError",
    "AdmissionIngressClosedError",
    "AdmissionIngressLane",
]
