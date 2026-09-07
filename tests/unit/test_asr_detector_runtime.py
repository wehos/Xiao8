from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main_logic.asr_client.endpointing.detector_runtime as detector_runtime_module
from main_logic.asr_client._provider_events import ProviderAudioRange
from main_logic.asr_client.endpointing.detector_runtime import (
    DetectorCandidateRejectionCommitResult,
    DetectorCandidateRejectionLease,
    DetectorFeedResult,
    DetectorRuntime,
    ProviderExactSpeakerIntervalReservation,
    ProviderSpeakerEvidenceAnchorStatus,
    SmartTurnLease,
    SmartTurnReadiness,
    _AudioItem,
    _ResetItem,
    _VoiceTurnAdapter,
)
from main_logic.asr_client.endpointing.config import SmartTurnConfig
from main_logic.asr_client.endpointing.detector import (
    DetectorActivityEvent,
    DetectorCandidateKey,
    DetectorIngressIdentity,
    DetectorPrewarmEvent,
    DetectorSubmitStatus,
    DetectorTurnEvent,
    ProviderCandidateFence,
    ProviderSpeakerBoundarySnapshot,
)
from main_logic.asr_client.endpointing.micro_event_policy import (
    ProviderMicroEventConfig,
)
from main_logic.asr_client.endpointing.silero_vad import (
    SileroActivityGate,
    SileroFeedResult,
)
from main_logic.asr_client.lifecycle import (
    VoiceIngressToken,
    VoiceInputLifecycleController,
    VoiceLifecycleEvent,
    VoiceRouteMode,
    VoiceTurnToken,
)
from main_logic.asr_client.provider_policy import AsrProviderPolicy
from main_logic.asr_client.runtime import AsrRuntimeCallbacks, IndependentAsrRuntime
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowBatchReconcileReceipt,
    SpeakerShadowBatchReconcileRequest,
    SpeakerShadowCandidateKey,
    SpeakerShadowCaptureDecisionState,
    SpeakerShadowCaptureDisposition,
    SpeakerShadowCaptureResult,
    SpeakerShadowConfig,
    SpeakerShadowObservation,
    SpeakerShadowTerminalCoverageReceipt,
    SpeakerShadowTerminalCoverageRequest,
)
from main_logic.asr_client.speaker_shadow.runtime import SpeakerShadowRuntime
from main_logic.asr_client.endpointing.throttle_policy import (
    ThrottleAction,
    VoiceThrottlePolicy,
)
from main_logic.voice_turn.activity_evidence import RnnoiseEvidence
from main_logic.voice_turn.contracts import (
    EvaluationStatus,
    SpeechActivityEvent,
    TurnDecision,
)
from main_logic.asr_client.endpointing.coordinator import CoordinatorState


class _Vad:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.load_threads: list[int] = []
        self.closed = False

    def load(self) -> bool:
        self.load_threads.append(threading.get_ident())
        return self.available

    def close(self) -> None:
        self.closed = True


class _SileroVadStub:
    def __init__(self) -> None:
        self.reset_calls = 0

    def reset_stream(self) -> None:
        self.reset_calls += 1


class _Gate:
    def __init__(self, events=()) -> None:
        self.events = tuple(events)
        self.inputs: list[bytes] = []

    def feed(self, pcm16: bytes):
        self.inputs.append(pcm16)
        return self.events

    def reset(self) -> None:
        return None


class _DenyRearmGate(_Gate):
    def __init__(self, events=()) -> None:
        super().__init__(events)
        self.prepare_calls = 0
        self.operations: list[str] = []
        self.prepare_started = asyncio.Event()
        self.prepare_error: Exception | None = None

    def feed(self, pcm16: bytes):
        self.operations.append("audio")
        return super().feed(pcm16)

    def prepare_post_deny_silence_boundary(self) -> None:
        self.prepare_calls += 1
        self.operations.append("prepare")
        self.prepare_started.set()
        if self.prepare_error is not None:
            raise self.prepare_error


class _BlockingDenyRearmGate(_DenyRearmGate):
    def __init__(self) -> None:
        super().__init__()
        self.feed_started = threading.Event()
        self.feed_release = threading.Event()

    def feed(self, pcm16: bytes):
        self.feed_started.set()
        if not self.feed_release.wait(timeout=2):
            raise TimeoutError("test did not release blocked Silero feed")
        return super().feed(pcm16)


class _LowScoreSpeakerBackendFactory:
    def __call__(self) -> "_LowScoreSpeakerBackend":
        return _LowScoreSpeakerBackend()


class _LowScoreSpeakerBackend:
    def load(self) -> bool:
        return True

    def score(self, _pcm16: bytes, _sample_rate_hz: int) -> float:
        return 0.2

    def close(self) -> None:
        return None


class _EvidenceGate(_Gate):
    def __init__(self, results: list[SileroFeedResult]) -> None:
        super().__init__()
        self.results = list(results)
        self.feed_calls = 0
        self.evidence_calls = 0
        self.reset_calls = 0

    def _next(self, pcm16: bytes) -> SileroFeedResult:
        self.inputs.append(pcm16)
        return self.results.pop(0)

    def feed(self, pcm16: bytes):
        self.feed_calls += 1
        return self._next(pcm16).events

    def feed_with_evidence(self, pcm16: bytes) -> SileroFeedResult:
        self.evidence_calls += 1
        return self._next(pcm16)

    def reset(self) -> None:
        self.reset_calls += 1


class _FailingVad(_Vad):
    def load(self) -> bool:
        raise RuntimeError("load failed")


class _FailingGate(_Gate):
    def feed(self, pcm16: bytes):
        raise RuntimeError("feed failed")


class _SemanticCoordinator:
    def __init__(self, *, available: bool = True) -> None:
        self.state = CoordinatorState.IDLE
        self.audio: list[bytes] = []
        self.available = available
        self.prepare_calls = 0

    def push_audio(self, pcm16: bytes) -> None:
        self.audio.append(pcm16)

    async def on_activity_event(self, event: SpeechActivityEvent) -> None:
        if event is SpeechActivityEvent.CANDIDATE_PAUSE:
            self.state = CoordinatorState.PAUSE_CANDIDATE

    async def evaluate_buffered(self):
        self.state = CoordinatorState.PAUSE_CANDIDATE
        return SimpleNamespace(
            status=EvaluationStatus.OK,
            decision=TurnDecision.COMPLETE,
        )

    async def prepare_predictor(self) -> bool:
        self.prepare_calls += 1
        return self.available

    async def reset(self) -> None:
        self.state = CoordinatorState.IDLE

    async def close(self) -> None:
        self.state = CoordinatorState.CLOSED

    async def unload_predictor(self) -> None:
        return None


class _BlockingSemanticCoordinator(_SemanticCoordinator):
    def __init__(self, *, block_prepare: bool = False) -> None:
        super().__init__()
        self.evaluate_started = asyncio.Event()
        self.evaluate_release = asyncio.Event()
        self.prepare_started = asyncio.Event()
        self.prepare_release = asyncio.Event()
        if not block_prepare:
            self.prepare_release.set()

    async def evaluate_buffered(self):
        self.evaluate_started.set()
        await self.evaluate_release.wait()
        return await super().evaluate_buffered()

    async def prepare_predictor(self) -> bool:
        self.prepare_calls += 1
        self.prepare_started.set()
        await self.prepare_release.wait()
        return True


class _BlockingResultCoordinator(_BlockingSemanticCoordinator):
    def __init__(
        self,
        *,
        status: EvaluationStatus,
        decision: TurnDecision | None,
    ) -> None:
        super().__init__()
        self._status = status
        self._decision = decision
        self.activity_seq = 0

    async def on_activity_event(self, event: SpeechActivityEvent) -> None:
        self.activity_seq += 1
        await super().on_activity_event(event)

    async def evaluate_buffered(self):
        self.evaluate_started.set()
        await self.evaluate_release.wait()
        self.state = CoordinatorState.PAUSE_CANDIDATE
        return SimpleNamespace(status=self._status, decision=self._decision)


class _SequencedSemanticCoordinator(_SemanticCoordinator):
    def __init__(
        self,
        results: list[tuple[TurnDecision, float]],
    ) -> None:
        super().__init__()
        self.results = list(results)
        self.evaluation_count = 0
        self.activity_seq = 0
        self.evaluation_threshold = 0.5

    async def on_activity_event(self, event: SpeechActivityEvent) -> None:
        if event in (
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.SPEECH_RESUMED,
        ):
            self.activity_seq += 1
            self.state = CoordinatorState.SPEECH_ACTIVE
        elif event is SpeechActivityEvent.CANDIDATE_PAUSE:
            self.state = CoordinatorState.PAUSE_CANDIDATE

    async def evaluate_buffered(self):
        self.evaluation_count += 1
        decision, probability = self.results.pop(0)
        self.state = (
            CoordinatorState.WAIT_CONTINUATION
            if decision is TurnDecision.INCOMPLETE
            else CoordinatorState.PAUSE_CANDIDATE
        )
        return SimpleNamespace(
            status=EvaluationStatus.OK,
            decision=decision,
            probability=probability,
        )


class _ResetClearsSemanticCoordinator(_SemanticCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.reset_calls = 0

    async def reset(self) -> None:
        self.reset_calls += 1
        self.audio.clear()
        await super().reset()


class _RaisingPrepareCoordinator(_SemanticCoordinator):
    async def prepare_predictor(self) -> bool:
        raise RuntimeError("prepare failed")


class _OverflowAdapter:
    def __init__(self) -> None:
        self.failed = False
        self.throttle_available = True
        self.push_calls = 0
        self.reset_started = asyncio.Event()
        self.reset_release = asyncio.Event()
        self.closed = False

    async def push_audio(self, **_kwargs) -> None:
        self.push_calls += 1
        if self.push_calls == 1:
            raise asyncio.QueueFull

    async def reset(self, **_kwargs) -> None:
        self.reset_started.set()
        await self.reset_release.wait()

    async def close(self) -> None:
        self.closed = True

    async def wait_failure(self):
        await asyncio.Event().wait()

    def pin_smart_turn(self) -> None:
        return None

    def unpin_smart_turn(self) -> None:
        return None


class _SpeakerShadowSpy:
    def __init__(self) -> None:
        self.enabled_value = True
        self.enabled_error: Exception | None = None
        self.submit_error: Exception | None = None
        self.finish_error: Exception | None = None
        self.reset_error: Exception | None = None
        self.close_error: Exception | None = None
        self.frames: list[tuple[bytes, int, SpeakerShadowCandidateKey]] = []
        self.finished: list[SpeakerShadowCandidateKey] = []
        self.events: list[tuple[str, object]] = []
        self.reset_calls = 0
        self.close_calls = 0
        self.provisional_pending = False

    @property
    def enabled(self) -> bool:
        if self.enabled_error is not None:
            raise self.enabled_error
        return self.enabled_value

    def submit(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        if self.submit_error is not None:
            raise self.submit_error
        self.frames.append((pcm16, sample_rate_hz, candidate))
        self.events.append(("submit", candidate))
        return False

    def finish_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        if self.finish_error is not None:
            raise self.finish_error
        self.finished.append(candidate)
        self.events.append(("finish", candidate))
        return False

    def requires_provisional_decision(
        self,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        return bool(self.provisional_pending and candidate in self.finished)

    def snapshot(self) -> dict[str, int]:
        return {"submitted_frame_count": len(self.frames)}

    async def reset(self) -> None:
        self.reset_calls += 1
        if self.reset_error is not None:
            raise self.reset_error

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _BlockingCloseSpeakerShadow(_SpeakerShadowSpy):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.close_release.wait()


class _DeferredSpeakerShadowSpy(_SpeakerShadowSpy):
    def __init__(
        self,
        *,
        support: bool = True,
        support_error: Exception | None = None,
        defer_result: bool = True,
        activate_result: bool = True,
    ) -> None:
        super().__init__()
        self.support = support
        self.support_error = support_error
        self.defer_result = defer_result
        self.activate_result = activate_result
        self.support_calls = 0

    def supports_deferred_candidate(
        self,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        self.support_calls += 1
        self.events.append(("supports", candidate))
        if self.support_error is not None:
            raise self.support_error
        return self.support

    def defer_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        self.events.append(("defer", candidate))
        return self.defer_result

    def activate_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        self.events.append(("activate", candidate))
        return self.activate_result

    def submit(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        super().submit(
            pcm16,
            sample_rate_hz=sample_rate_hz,
            candidate=candidate,
        )
        return True

    def finish_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        super().finish_candidate(candidate)
        return True


class _StableEvidenceSpeakerShadowSpy(_DeferredSpeakerShadowSpy):
    def __init__(self) -> None:
        super().__init__()
        self.sample_counts: dict[SpeakerShadowCandidateKey, int] = {}
        self.abandoned: list[SpeakerShadowCandidateKey] = []

    def submit_capture(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> SpeakerShadowCaptureResult:
        assert self.submit(
            pcm16,
            sample_rate_hz=sample_rate_hz,
            candidate=candidate,
        )
        accepted = len(pcm16) // 2
        cumulative = self.sample_counts.get(candidate, 0) + accepted
        self.sample_counts[candidate] = cumulative
        complete = cumulative >= 48_000
        return SpeakerShadowCaptureResult(
            disposition=(
                SpeakerShadowCaptureDisposition.COMPLETE
                if complete
                else SpeakerShadowCaptureDisposition.ACCEPTED
            ),
            accepted_sample_count=accepted,
            cumulative_sample_count=cumulative,
            completed_window_sample_count=48_000 if complete else 0,
            decision_state=(
                SpeakerShadowCaptureDecisionState.SCORED
                if complete
                else SpeakerShadowCaptureDecisionState.PENDING
            ),
        )

    def abandon_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        self.abandoned.append(candidate)
        self.sample_counts.pop(candidate, None)
        return True


class _PendingCompletionSpeakerShadowSpy(_DeferredSpeakerShadowSpy):
    """Model terminal work that a whole-shadow reset would incorrectly erase."""

    def __init__(self) -> None:
        super().__init__()
        self.pending_completions: list[SpeakerShadowCandidateKey] = []

    def finish_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        accepted = super().finish_candidate(candidate)
        if accepted:
            self.pending_completions.append(candidate)
        return accepted

    async def reset(self) -> None:
        await super().reset()
        self.pending_completions.clear()


class _ReconcilingSpeakerShadowSpy(_DeferredSpeakerShadowSpy):
    def __init__(self) -> None:
        super().__init__()
        self.sample_counts: dict[SpeakerShadowCandidateKey, int] = {}
        self.reconciliations: list[
            tuple[
                SpeakerShadowCandidateKey,
                SpeakerShadowCandidateKey,
                int,
                SpeakerShadowCandidateKey | None,
            ]
        ] = []
        self.batch_requests: list[SpeakerShadowBatchReconcileRequest] = []
        self.batch_receipts: list[SpeakerShadowBatchReconcileReceipt] = []
        self.batch_status = "applied"
        self.batch_admitted = True
        self.revoked_receipts: list[SpeakerShadowBatchReconcileReceipt] = []
        self.receipt_statuses: dict[SpeakerShadowBatchReconcileReceipt, str] = {}
        self._batch_owner = object()

    def submit(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        accepted = super().submit(
            pcm16,
            sample_rate_hz=sample_rate_hz,
            candidate=candidate,
        )
        if accepted:
            self.sample_counts[candidate] = (
                self.sample_counts.get(candidate, 0) + len(pcm16) // 2
            )
        return accepted

    def reconcile_candidate_prefix(
        self,
        *,
        source: SpeakerShadowCandidateKey,
        target: SpeakerShadowCandidateKey,
        prefix_sample_count: int,
        suffix: SpeakerShadowCandidateKey | None = None,
    ) -> bool:
        source_samples = self.sample_counts.get(source)
        if source_samples is None or not 0 <= prefix_sample_count <= source_samples:
            return False
        remainder = source_samples - prefix_sample_count
        if (remainder > 0) != (suffix is not None):
            return False
        self.reconciliations.append((source, target, prefix_sample_count, suffix))
        self.events.append(("reconcile", source))
        if source == target:
            self.sample_counts[source] = prefix_sample_count
        else:
            self.sample_counts[target] = (
                self.sample_counts.get(target, 0) + prefix_sample_count
            )
            self.sample_counts.pop(source, None)
        if suffix is not None:
            self.sample_counts[suffix] = remainder
        return True

    def reconcile_candidate_batch(
        self,
        request: SpeakerShadowBatchReconcileRequest,
    ) -> SpeakerShadowBatchReconcileReceipt | None:
        self.batch_requests.append(request)
        if not self.batch_admitted:
            return None
        if any(
            self.sample_counts.get(source.candidate) != source.expected_sample_count
            for source in request.sources
        ):
            return None

        target_samples = sum(
            source.keep_end_sample - source.keep_start_sample
            for source in request.sources
        )
        last = request.sources[-1]
        suffix_samples = last.expected_sample_count - last.keep_end_sample
        if (suffix_samples > 0) != (request.suffix is not None):
            return None

        original_counts = dict(self.sample_counts)
        for source in request.sources:
            self.sample_counts.pop(source.candidate, None)
        self.sample_counts[request.target] = target_samples
        if request.suffix is not None:
            self.sample_counts[request.suffix] = suffix_samples

        first = request.sources[0]
        if first.keep_start_sample > 0 and first.candidate != request.target:
            self.reconciliations.append(
                (
                    first.candidate,
                    first.candidate,
                    first.keep_start_sample,
                    request.target,
                )
            )
            self.reconciliations.append(
                (
                    request.target,
                    request.target,
                    target_samples,
                    request.suffix,
                )
            )
            self.sample_counts[first.candidate] = first.keep_start_sample
        else:
            for index, source in enumerate(request.sources):
                kept = source.keep_end_sample - source.keep_start_sample
                if kept <= 0 or (index == 0 and source.candidate == request.target):
                    continue
                suffix = request.suffix if index == len(request.sources) - 1 else None
                self.reconciliations.append(
                    (source.candidate, request.target, kept, suffix)
                )
        for source in request.sources:
            self.events.append(("reconcile", source.candidate))
        if ("defer", request.target) in self.events:
            self.events.append(("activate", request.target))
        if request.finish_target:
            self.finished.append(request.target)
            self.events.append(("finish", request.target))

        receipt = SpeakerShadowBatchReconcileReceipt(
            runtime_generation=0,
            batch_id=len(self.batch_receipts) + 1,
            target=request.target,
            suffix=request.suffix,
            target_sample_count=target_samples,
            suffix_sample_count=suffix_samples,
            _owner=self._batch_owner,
        )
        self.batch_receipts.append(receipt)
        return receipt

    def reconciliation_status(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> str:
        if receipt in self.revoked_receipts:
            return "failed"
        return self.receipt_statuses.get(receipt, self.batch_status)

    def revoke_reconciliation(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> None:
        if receipt not in self.revoked_receipts:
            self.revoked_receipts.append(receipt)


class _MalformedBatchReceiptSpeakerShadowSpy(_ReconcilingSpeakerShadowSpy):
    def reconcile_candidate_batch(
        self,
        request: SpeakerShadowBatchReconcileRequest,
    ) -> SpeakerShadowBatchReconcileReceipt | None:
        admitted = super().reconcile_candidate_batch(request)
        if admitted is None:
            return None
        malformed = SpeakerShadowBatchReconcileReceipt(
            runtime_generation=admitted.runtime_generation,
            batch_id=admitted.batch_id,
            target=admitted.target,
            suffix=admitted.suffix,
            target_sample_count=admitted.target_sample_count + 1,
            suffix_sample_count=admitted.suffix_sample_count,
            _owner=admitted._owner,
        )
        self.batch_receipts[-1] = malformed
        return malformed


class _MalformedTerminalReceiptSpeakerShadowSpy(_ReconcilingSpeakerShadowSpy):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_receipts: list[SpeakerShadowTerminalCoverageReceipt] = []
        self.revoked_terminal_receipts: list[SpeakerShadowTerminalCoverageReceipt] = []
        self.terminal_receipt_statuses: dict[
            SpeakerShadowTerminalCoverageReceipt, str
        ] = {}
        self._terminal_owner = object()

    def submit_capture(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> SpeakerShadowCaptureResult:
        assert self.submit(
            pcm16,
            sample_rate_hz=sample_rate_hz,
            candidate=candidate,
        )
        accepted_sample_count = len(pcm16) // 2
        cumulative_sample_count = self.sample_counts[candidate]
        return SpeakerShadowCaptureResult(
            disposition=SpeakerShadowCaptureDisposition.COMPLETE,
            accepted_sample_count=accepted_sample_count,
            cumulative_sample_count=cumulative_sample_count,
            completed_window_sample_count=cumulative_sample_count,
            decision_state=SpeakerShadowCaptureDecisionState.SCORED,
        )

    def reconcile_finalized_candidate_coverage(
        self,
        request: SpeakerShadowTerminalCoverageRequest,
    ) -> SpeakerShadowTerminalCoverageReceipt:
        malformed = SpeakerShadowTerminalCoverageReceipt(
            runtime_generation=0,
            batch_id=len(self.terminal_receipts) + 1,
            target=request.target,
            suffix=request.suffix,
            retained_sample_count=request.scored_window_end_sample,
            covered_sample_count=request.provider_exact_end_sample + 1,
            terminal_preserved=True,
            _owner=self._terminal_owner,
        )
        self.terminal_receipts.append(malformed)
        return malformed

    def terminal_coverage_status(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> str:
        if receipt in self.revoked_terminal_receipts:
            return "failed"
        return self.terminal_receipt_statuses.get(receipt, "pending")

    def revoke_terminal_coverage(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> None:
        if receipt not in self.revoked_terminal_receipts:
            self.revoked_terminal_receipts.append(receipt)


class _BlockingSettlementSpeakerShadowSpy(_ReconcilingSpeakerShadowSpy):
    def __init__(self) -> None:
        super().__init__()
        self.batch_status = "pending"
        self.settlement_started = asyncio.Event()
        self.settlement_release = asyncio.Event()
        self.settlement_returning = asyncio.Event()

    async def wait_reconciliation_settled(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
        *,
        deadline: float,
    ) -> str:
        assert deadline > asyncio.get_running_loop().time()
        self.settlement_started.set()
        await self.settlement_release.wait()
        if receipt in self.revoked_receipts:
            self.settlement_returning.set()
            return "failed"
        self.receipt_statuses[receipt] = "applied"
        self.settlement_returning.set()
        return "applied"


def _smart_turn_policy() -> AsrProviderPolicy:
    return AsrProviderPolicy(
        transport="segmented",
        endpoint_authority="smart_turn",
        smart_turn_required=True,
        max_segment_ms=30_000,
        warm_transport_ms=0,
        replay_policy="none",
    )


def _provider_endpoint_policy() -> AsrProviderPolicy:
    return AsrProviderPolicy(
        transport="streaming",
        endpoint_authority="provider",
        smart_turn_required=False,
        max_segment_ms=None,
        warm_transport_ms=25_000,
        replay_policy="preconnect_only",
    )


def _provider_speaker_config() -> SpeakerShadowConfig:
    return SpeakerShadowConfig(
        enabled=True,
        minimum_audio_ms=1_500,
        maximum_audio_ms=4_000,
        observation_checkpoints_ms=(1_500, 3_000),
        similarity_thresholds=(0.40,),
        completion_confirmation_scopes=("provider_candidate",),
        pending_observation_gate_scopes=("provider_candidate",),
        queue_capacity=32,
        terminal_queue_capacity=16,
        buffered_candidate_capacity=8,
        finalized_candidate_capacity=32,
        idle_unload_seconds=60.0,
        callback_timeout_seconds=0.1,
        backend_load_timeout_seconds=3.0,
        backend_score_timeout_seconds=1.0,
        backend_close_timeout_seconds=1.0,
        shutdown_grace_seconds=0.1,
        process_terminate_timeout_seconds=0.5,
    )


def _speaker_pcm(duration_ms: int) -> bytes:
    return b"\x21\x00" * (16_000 * duration_ms // 1_000)


def _ingress_token() -> VoiceIngressToken:
    return VoiceIngressToken(1, "socket", 1, 1, 1)


def _silero_micro_result(
    events: tuple[SpeechActivityEvent, ...],
    *,
    window_count: int = 4,
    onset_window_count: int = 2,
    offset_window_count: int = 2,
    ambiguous_window_count: int = 0,
    first_onset_window_index: int | None = 0,
    last_onset_window_index: int | None = 1,
    post_confirmation_onset_window_count: int = 0,
) -> SileroFeedResult:
    return SileroFeedResult(
        events=events,
        window_count=window_count,
        onset_window_count=onset_window_count,
        offset_window_count=offset_window_count,
        ambiguous_window_count=ambiguous_window_count,
        first_onset_window_index=first_onset_window_index,
        last_onset_window_index=last_onset_window_index,
        post_confirmation_onset_window_count=(post_confirmation_onset_window_count),
    )


def _rnnoise_chunk(peak: float = 0.4) -> RnnoiseEvidence:
    return RnnoiseEvidence(True, 4, peak, peak, peak, peak)


async def _prepare_candidate_rejection_fixture() -> tuple[
    DetectorRuntime,
    _SpeakerShadowSpy,
    DetectorCandidateKey,
    SpeakerShadowCandidateKey,
    VoiceTurnToken,
]:
    shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    ingress = _ingress_token()
    result = await detector.feed(
        b"\x01\x00" * 160,
        ingress_token=ingress,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert result.throttle_available is True
    assert result.identity == DetectorIngressIdentity(
        ingress_token=ingress,
        detector_epoch=detector.detector_epoch,
        sequence_no=1,
    )
    candidate = result.candidate
    assert candidate is not None
    turn_token = VoiceTurnToken(ingress, turn_id=1)
    assert await detector.bind_candidate(candidate, turn_token) is not None
    detector.observe_provider_audio(b"\x02\x00" * 160, sample_rate_hz=16_000)
    shadow_candidate = detector._speaker_shadow_candidate
    assert shadow_candidate is not None
    return detector, shadow, candidate, shadow_candidate, turn_token


async def test_speaker_candidate_binding_snapshot_publishes_when_turn_binds_first() -> (
    None
):
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        turn_token,
    ) = await _prepare_candidate_rejection_fixture()

    assert (
        detector._bound_turn_token_for_speaker_candidate(shadow_candidate) == turn_token
    )
    await detector.close()


async def test_speaker_candidate_binding_snapshot_publishes_when_shadow_opens_first() -> (
    None
):
    shadow = _SpeakerShadowSpy()
    published: list[tuple[SpeakerShadowCandidateKey, VoiceTurnToken, str | None]] = []
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
        speaker_owner_generation="owner-a",
        on_speaker_candidate_bound=lambda candidate, token, owner: published.append(
            (candidate, token, owner)
        ),
    )
    ingress = _ingress_token()
    result = await detector.feed(
        b"\x01\x00" * 160,
        ingress_token=ingress,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert result.candidate is not None
    detector.observe_provider_audio(b"\x02\x00" * 160, sample_rate_hz=16_000)
    shadow_candidate = detector._speaker_shadow_candidate
    assert shadow_candidate is not None
    assert detector._bound_turn_token_for_speaker_candidate(shadow_candidate) is None

    turn_token = VoiceTurnToken(ingress, turn_id=1)
    assert await detector.bind_candidate(result.candidate, turn_token) is not None
    assert (
        detector._bound_turn_token_for_speaker_candidate(shadow_candidate) == turn_token
    )
    assert published == [(shadow_candidate, turn_token, "owner-a")]
    await detector.close()


async def test_speaker_candidate_binding_snapshot_reset_fences_stale_candidate() -> (
    None
):
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    assert (
        detector._bound_turn_token_for_speaker_candidate(shadow_candidate) == turn_token
    )

    await detector.reset()

    assert detector._bound_turn_token_for_speaker_candidate(shadow_candidate) is None
    await detector.close()


async def test_speaker_candidate_binding_callback_publishes_once_before_score() -> None:
    shadow = _SpeakerShadowSpy()
    published: list[tuple[SpeakerShadowCandidateKey, VoiceTurnToken, str | None]] = []
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
        speaker_owner_generation="owner-a",
        on_speaker_candidate_bound=lambda candidate, token, owner: published.append(
            (candidate, token, owner)
        ),
    )
    ingress = _ingress_token()
    result = await detector.feed(
        b"\x01\x00" * 160,
        ingress_token=ingress,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert result.candidate is not None
    turn_token = VoiceTurnToken(ingress, turn_id=1)
    assert await detector.bind_candidate(result.candidate, turn_token) is not None

    detector.observe_provider_audio(b"\x02\x00" * 160, sample_rate_hz=16_000)
    shadow_candidate = detector._speaker_shadow_candidate
    assert shadow_candidate is not None
    assert published == [(shadow_candidate, turn_token, "owner-a")]
    assert shadow.frames

    # Re-observing and re-binding the same immutable candidate cannot publish
    # a second CandidateBound or move it to a different turn.
    detector.observe_provider_audio(b"\x03\x00" * 160, sample_rate_hz=16_000)
    assert await detector.bind_candidate(result.candidate, turn_token) is not None
    assert published == [(shadow_candidate, turn_token, "owner-a")]
    await detector.close()


async def test_speaker_candidate_binding_callback_failure_keeps_lookup_fallback() -> (
    None
):
    def fail_publication(
        _candidate: SpeakerShadowCandidateKey,
        _turn_token: VoiceTurnToken,
        _owner_generation: str | None,
    ) -> None:
        raise RuntimeError("candidate publication failed")

    shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
        on_speaker_candidate_bound=fail_publication,
    )
    candidate, _identity, turn_token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    detector.observe_provider_audio(b"\x02\x00" * 160, sample_rate_hz=16_000)
    shadow_candidate = detector._speaker_shadow_candidate
    assert shadow_candidate is not None
    assert candidate in detector._bound_turns
    assert (
        detector._bound_turn_token_for_speaker_candidate(shadow_candidate) == turn_token
    )
    await detector.close()


async def test_provider_finish_preserves_binding_until_terminal_release() -> None:
    (
        detector,
        shadow,
        _candidate,
        shadow_candidate,
        turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    score_started = asyncio.Event()
    score_release = asyncio.Event()

    async def resume_score() -> VoiceTurnToken | None:
        score_started.set()
        await score_release.wait()
        return detector._bound_turn_token_for_speaker_candidate(shadow_candidate)

    score_task = asyncio.create_task(resume_score())
    await score_started.wait()
    fence = await detector.seal_provider_candidate(turn_token)
    assert fence is not None
    assert shadow.finished == [shadow_candidate]

    # Provider sealing ends capture, but a queued model result still owns the
    # original turn until the admission owner accepts CaptureClosed.
    score_release.set()
    assert await score_task == turn_token
    assert not detector.release_speaker_candidate_binding(
        shadow_candidate,
        VoiceTurnToken(turn_token.ingress, turn_id=turn_token.turn_id + 1),
    )
    assert (
        detector._bound_turn_token_for_speaker_candidate(shadow_candidate) == turn_token
    )
    assert detector.release_speaker_candidate_binding(shadow_candidate, turn_token)
    assert detector._bound_turn_token_for_speaker_candidate(shadow_candidate) is None
    assert not detector.release_speaker_candidate_binding(
        shadow_candidate,
        turn_token,
    )
    await detector.close()


async def test_provider_finish_then_reset_fences_late_score_binding() -> None:
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    score_started = asyncio.Event()
    score_release = asyncio.Event()

    async def resume_score() -> VoiceTurnToken | None:
        score_started.set()
        await score_release.wait()
        return detector._bound_turn_token_for_speaker_candidate(shadow_candidate)

    score_task = asyncio.create_task(resume_score())
    await score_started.wait()
    assert await detector.seal_provider_candidate(turn_token) is not None
    await detector.reset()
    score_release.set()

    assert await score_task is None
    assert not detector.release_speaker_candidate_binding(
        shadow_candidate,
        turn_token,
    )
    await detector.close()


async def test_speaker_verifier_replacement_fences_old_candidate_binding() -> None:
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    assert (
        detector._bound_turn_token_for_speaker_candidate(shadow_candidate) == turn_token
    )

    await detector.replace_speaker_verifier(
        _SpeakerShadowSpy(),
        owner_generation="owner-b",
    )

    assert detector._bound_turn_token_for_speaker_candidate(shadow_candidate) is None
    assert not detector.release_speaker_candidate_binding(
        shadow_candidate,
        turn_token,
    )
    await detector.close()


async def _open_provider_candidate(
    detector: DetectorRuntime,
    *,
    turn_id: int,
) -> tuple[
    DetectorCandidateKey,
    DetectorIngressIdentity,
    VoiceTurnToken,
]:
    ingress = _ingress_token()
    result = await detector.feed(
        b"\x01\x00" * 160,
        ingress_token=ingress,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert result.candidate is not None
    assert result.identity is not None
    token = VoiceTurnToken(ingress, turn_id=turn_id)
    assert await detector.bind_candidate(result.candidate, token) is not None
    return result.candidate, result.identity, token


async def _anchor_provider_evidence(
    detector: DetectorRuntime,
    lease,
    *,
    start_sample_16k: int = 0,
) -> None:
    result = await detector.anchor_provider_speaker_evidence(
        lease,
        audio_start_sample_16k=start_sample_16k,
        deadline=asyncio.get_running_loop().time() + 1.0,
    )
    assert result.status in {
        ProviderSpeakerEvidenceAnchorStatus.APPLIED,
        ProviderSpeakerEvidenceAnchorStatus.IDEMPOTENT,
    }


async def _open_real_terminal_provider_candidate(
    *,
    duration_ms: int,
) -> tuple[
    DetectorRuntime,
    SpeakerShadowRuntime,
    DetectorCandidateRejectionLease,
    VoiceTurnToken,
    list[SpeakerShadowObservation],
]:
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
        on_observation=observe,
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        _speaker_pcm(1_500),
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    await shadow.wait_idle()
    segment = detector._provider_speaker_segments[0]
    assert segment.candidate is not None
    lease = await detector.prepare_candidate_rejection(segment.candidate)
    assert lease is not None

    await detector.observe_provider_audio_ordered(
        _speaker_pcm(1_500),
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=2,
        split_before_audio=False,
    )
    await shadow.wait_idle()
    remaining_ms = duration_ms - 3_000
    sequence_no = 3
    while remaining_ms > 0:
        chunk_ms = min(1_000, remaining_ms)
        await detector.observe_provider_audio_ordered(
            _speaker_pcm(chunk_ms),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=sequence_no,
            split_before_audio=False,
        )
        remaining_ms -= chunk_ms
        sequence_no += 1
    return detector, shadow, lease, token, observations


async def test_provider_speaker_evidence_lease_survives_segment_rotation_and_unknown_boundary() -> (
    None
):
    shadow = _StableEvidenceSpeakerShadowSpy()
    published: list[tuple[SpeakerShadowCandidateKey, VoiceTurnToken, str | None]] = []
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
        speaker_owner_generation="owner-a",
        on_speaker_candidate_bound=lambda candidate, token, owner: published.append(
            (candidate, token, owner)
        ),
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        evidence_lease = await detector.ensure_provider_speaker_evidence_lease()
        assert evidence_lease is not None
        assert await detector.ensure_provider_speaker_evidence_lease() == evidence_lease

        first = await detector.observe_provider_audio_ordered(
            _speaker_pcm(1_500),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=evidence_lease,
        )
        second = await detector.observe_provider_audio_ordered(
            _speaker_pcm(1_500),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=evidence_lease,
        )

        assert first is not None and second is not None
        assert first.lease == second.lease == evidence_lease
        assert second.capture.cumulative_sample_count == 48_000
        assert second.capture.disposition is SpeakerShadowCaptureDisposition.COMPLETE
        assert {frame[2] for frame in shadow.frames} == {evidence_lease.candidate}
        assert published == [
            (evidence_lease.candidate, _token, "owner-a"),
        ]
        assert len(detector._provider_speaker_segments) == 2
        assert all(
            segment.candidate is None for segment in detector._provider_speaker_segments
        )

        boundary = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 48_000)
        )
        assert boundary is not None and boundary.boundary_exact is False
        assert shadow.finished == []
        assert await detector.ensure_provider_speaker_evidence_lease() == evidence_lease

        assert await detector.finish_provider_speaker_evidence_lease(evidence_lease)
        assert shadow.finished == [evidence_lease.candidate]
        successor = await detector.ensure_provider_speaker_evidence_lease()
        assert successor is not None and successor != evidence_lease
    finally:
        await detector.close()


async def test_provider_speaker_evidence_allows_runtime_owned_binding() -> None:
    shadow = _StableEvidenceSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        feed = await detector.feed(b"\x01\x00" * 160)
        assert feed.identity is not None
        evidence_lease = await detector.ensure_provider_speaker_evidence_lease()
        assert evidence_lease is not None

        update = await detector.observe_provider_audio_ordered(
            b"\x11\x00" * 160,
            sample_rate_hz=16_000,
            identity=feed.identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=evidence_lease,
        )

        assert update is not None
        assert update.capture.disposition is SpeakerShadowCaptureDisposition.ACCEPTED
        assert [frame[2] for frame in shadow.frames] == [evidence_lease.candidate]
        assert shadow.abandoned == []
    finally:
        await detector.close()


async def test_provider_exact_interval_preserves_pcm_arriving_before_commit() -> None:
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        first = await detector.observe_provider_audio_ordered(
            _speaker_pcm(800),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        assert first is not None
        await _anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 12_800),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None

        second = await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=lease,
        )
        assert second is not None
        assert second.capture.accepted_sample_count == 1_600
        assert second.capture.cumulative_sample_count == 14_400

        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        assert committed.snapshot.boundary_exact is True
        assert committed.snapshot.successor_present is True
        assert committed.snapshot.through_sequence_no == 1
        assert committed.target_candidate == lease.candidate
        successor = committed.successor_evidence_lease
        assert successor is not None
        assert successor != lease
        assert await detector.ensure_provider_speaker_evidence_lease() == successor
        assert len(detector._provider_speaker_segments) == 1
        segment = detector._provider_speaker_segments[0]
        assert (segment.start_sample_16k, segment.end_sample_16k) == (12_800, 14_400)

        await shadow.wait_idle()
        assert shadow._buffers[successor.candidate].sample_count == 1_600
        assert detector.commit_provider_exact_speaker_interval(reservation) is None
        assert not detector.abort_provider_exact_speaker_interval(reservation)
    finally:
        await detector.close()


async def test_provider_exact_interval_empty_successor_accepts_future_pcm() -> None:
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        first = await detector.observe_provider_audio_ordered(
            _speaker_pcm(800),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        assert first is not None
        await _anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 12_800),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None

        committed = detector.commit_provider_exact_speaker_interval(reservation)

        assert committed is not None
        assert committed.snapshot.successor_present is False
        successor = committed.successor_evidence_lease
        assert successor is not None
        assert await detector.ensure_provider_speaker_evidence_lease() == successor
        assert not detector._provider_speaker_segments

        future = await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=successor,
        )
        assert future is not None
        assert future.lease == successor
        assert future.capture.disposition is SpeakerShadowCaptureDisposition.ACCEPTED
        assert future.capture.cumulative_sample_count == 1_600

        await shadow.wait_idle()
        assert shadow._buffers[successor.candidate].sample_count == 1_600
    finally:
        await detector.close()


async def test_provider_exact_interval_abort_returns_staged_pcm_to_parent() -> None:
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(800),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        await _anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 12_800),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=lease,
        )

        assert detector.abort_provider_exact_speaker_interval(reservation)
        assert not detector.abort_provider_exact_speaker_interval(reservation)
        assert await detector.ensure_provider_speaker_evidence_lease() == lease
        await shadow.wait_idle()
        assert shadow._buffers[lease.candidate].sample_count == 14_400
    finally:
        await detector.close()


async def test_provider_exact_interval_abort_restore_failure_is_not_reported_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(800),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        await _anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 12_800),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=lease,
        )
        monkeypatch.setattr(shadow, "_ensure_worker", lambda: False)

        assert not detector.abort_provider_exact_speaker_interval(reservation)
        assert not detector._provider_exact_interval_records
        assert not detector.abort_provider_exact_speaker_interval(reservation)
    finally:
        await detector.close()


async def test_provider_exact_interval_rejects_forged_and_stale_timeline_authority() -> (
    None
):
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(400),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        await _anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 6_400),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None
        forged = ProviderExactSpeakerIntervalReservation(
            boundary=reservation.boundary,
            target_candidate=reservation.target_candidate,
            suffix_candidate=reservation.suffix_candidate,
            detector_epoch=reservation.detector_epoch,
            timeline_generation=reservation.timeline_generation,
            lease_generation=reservation.lease_generation,
            candidate_generation=reservation.candidate_generation,
            shadow_runtime_generation=reservation.shadow_runtime_generation,
            anchor_revision=reservation.anchor_revision,
            anchor_start_sample_16k=reservation.anchor_start_sample_16k,
            provider_pcm_through_sequence_no=(
                reservation.provider_pcm_through_sequence_no
            ),
            _owner=object(),
            _token=reservation._token,
        )
        assert detector.commit_provider_exact_speaker_interval(forged) is None
        assert not detector.abort_provider_exact_speaker_interval(forged)

        detector._provider_audio_timeline_generation += 1
        assert detector.commit_provider_exact_speaker_interval(reservation) is None
        assert not detector.abort_provider_exact_speaker_interval(reservation)
        assert not detector._provider_exact_interval_records
    finally:
        await detector.close()


async def test_provider_exact_interval_rejects_sequence_gap_and_overlapping_segments() -> (
    None
):
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        await _anchor_provider_evidence(detector, lease)
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=3,
            split_before_audio=True,
            speaker_evidence_lease=lease,
        )
        assert await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 1_600),
            speaker_evidence_lease=lease,
        ) is None

        successor = await detector.ensure_provider_speaker_evidence_lease()
        if successor is lease:
            await detector.abandon_provider_speaker_evidence_lease(lease)
            successor = await detector.ensure_provider_speaker_evidence_lease()
        assert successor is not None
    finally:
        await detector.close()

    overlap_shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    overlap_detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=overlap_shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            overlap_detector,
            turn_id=2,
        )
        lease = await overlap_detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        assert await overlap_detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        await _anchor_provider_evidence(overlap_detector, lease)
        assert await overlap_detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=lease,
        )
        overlap_detector._provider_speaker_segments[1].start_sample_16k -= 1
        assert await overlap_detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 1_600),
            speaker_evidence_lease=lease,
        ) is None
    finally:
        await overlap_detector.close()


@pytest.mark.parametrize("operation", ["reset", "close"])
async def test_provider_exact_interval_lifecycle_invalidates_reservation_immediately(
    operation: str,
) -> None:
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        await _anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 1_600),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None

        if operation == "reset":
            await detector.reset()
        else:
            await detector.close()

        assert not detector._provider_exact_interval_records
        assert detector.commit_provider_exact_speaker_interval(reservation) is None
        assert not detector.abort_provider_exact_speaker_interval(reservation)
    finally:
        await detector.close()


async def test_provider_speaker_evidence_lease_idle_clock_follows_latest_split_pcm() -> (
    None
):
    shadow = _StableEvidenceSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        evidence_lease = await detector.ensure_provider_speaker_evidence_lease()
        assert evidence_lease is not None
        first = await detector.observe_provider_audio_ordered(
            b"\x11\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=evidence_lease,
        )
        second = await detector.observe_provider_audio_ordered(
            b"\x12\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=evidence_lease,
        )
        assert first is not None and second is not None
        assert second.last_progress_at >= first.last_progress_at
        assert all(
            segment.last_progress_at == second.last_progress_at
            for segment in detector._provider_speaker_segments
        )

        detector._expire_provider_segments(second.last_progress_at + 9.9)
        assert await detector.ensure_provider_speaker_evidence_lease() == evidence_lease
        detector._expire_provider_segments(second.last_progress_at + 10.1)
        assert shadow.abandoned == [evidence_lease.candidate]
        successor = await detector.ensure_provider_speaker_evidence_lease()
        assert successor is not None and successor != evidence_lease
    finally:
        await detector.close()


async def test_provider_speaker_evidence_sequence_gap_abandons_without_renewing() -> (
    None
):
    shadow = _StableEvidenceSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        evidence_lease = await detector.ensure_provider_speaker_evidence_lease()
        assert evidence_lease is not None
        first = await detector.observe_provider_audio_ordered(
            b"\x11\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=evidence_lease,
        )
        assert first is not None
        unavailable = await detector.observe_provider_audio_ordered(
            b"\x12\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=3,
            split_before_audio=False,
            speaker_evidence_lease=evidence_lease,
        )
        assert unavailable is not None
        assert (
            unavailable.capture.disposition
            is SpeakerShadowCaptureDisposition.UNAVAILABLE
        )
        assert unavailable.last_progress_at == first.last_progress_at
        assert shadow.abandoned == [evidence_lease.candidate]
        assert not await detector.finish_provider_speaker_evidence_lease(evidence_lease)
    finally:
        await detector.close()


async def test_real_provider_speaker_evidence_scores_only_after_canonical_anchor() -> (
    None
):
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
        on_observation=observe,
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        evidence_lease = await detector.ensure_provider_speaker_evidence_lease()
        assert evidence_lease is not None

        first = await detector.observe_provider_audio_ordered(
            _speaker_pcm(1_500),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=evidence_lease,
        )
        assert first is not None
        await shadow.wait_idle()
        assert observations == []
        await _anchor_provider_evidence(
            detector,
            evidence_lease,
            start_sample_16k=24_000,
        )
        second = await detector.observe_provider_audio_ordered(
            _speaker_pcm(1_500),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=2,
            split_before_audio=True,
            speaker_evidence_lease=evidence_lease,
        )
        assert second is not None
        await shadow.wait_idle()

        for sequence_no in range(3, 12):
            update = await detector.observe_provider_audio_ordered(
                _speaker_pcm(1_000),
                sample_rate_hz=16_000,
                identity=identity,
                sequence_no=sequence_no,
                split_before_audio=False,
                speaker_evidence_lease=evidence_lease,
            )
            assert update is not None and update.lease == evidence_lease
            await shadow.wait_idle()

        assert [item.checkpoint_ms for item in observations] == [1_500, 3_000]
        assert {item.candidate for item in observations} == {evidence_lease.candidate}
        assert second.capture.cumulative_sample_count == 24_000
        assert len(detector._provider_speaker_evidence_state.coverage_candidates) >= 2

        unknown = await detector.retire_provider_speaker_boundary_unknown()
        assert unknown is not None and unknown.boundary_exact is False
        assert shadow.snapshot()["finished_candidate_count"] == 0
        assert await detector.ensure_provider_speaker_evidence_lease() == evidence_lease
        assert await detector.finish_provider_speaker_evidence_lease(evidence_lease)
        await shadow.wait_idle()
        assert shadow.snapshot()["finished_candidate_count"] == 1
    finally:
        await detector.close()


async def test_provider_speaker_anchor_future_duplicate_and_conflict_are_fenced() -> (
    None
):
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None

        pending = await detector.anchor_provider_speaker_evidence(
            lease,
            audio_start_sample_16k=1_600,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert pending.status is ProviderSpeakerEvidenceAnchorStatus.PENDING
        assert pending.buffer_origin_sample_16k == 0

        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=lease,
        )
        applied = await detector.anchor_provider_speaker_evidence(
            lease,
            audio_start_sample_16k=1_600,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert applied.status is ProviderSpeakerEvidenceAnchorStatus.APPLIED
        assert applied.anchor_revision == 1
        assert applied.buffer_origin_sample_16k == 0
        assert applied.anchor_start_sample_16k == 1_600

        duplicate = await detector.anchor_provider_speaker_evidence(
            lease,
            audio_start_sample_16k=1_600,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert duplicate.status is ProviderSpeakerEvidenceAnchorStatus.IDEMPOTENT
        assert duplicate.anchor_revision == applied.anchor_revision

        conflict = await detector.anchor_provider_speaker_evidence(
            lease,
            audio_start_sample_16k=0,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert conflict.status is ProviderSpeakerEvidenceAnchorStatus.CONFLICT
        assert not await detector.finish_provider_speaker_evidence_lease(lease)
    finally:
        await detector.close()


async def test_provider_speaker_anchor_evicted_prefix_is_unavailable_without_score() -> (
    None
):
    observations: list[SpeakerShadowObservation] = []

    async def observe(observation: SpeakerShadowObservation) -> None:
        observations.append(observation)

    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
        on_observation=observe,
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        for sequence_no in range(1, 6):
            assert await detector.observe_provider_audio_ordered(
                _speaker_pcm(1_000),
                sample_rate_hz=16_000,
                identity=identity,
                sequence_no=sequence_no,
                split_before_audio=False,
                speaker_evidence_lease=lease,
            )
        await shadow.wait_idle()
        assert observations == []

        unavailable = await detector.anchor_provider_speaker_evidence(
            lease,
            audio_start_sample_16k=0,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert unavailable.status is ProviderSpeakerEvidenceAnchorStatus.UNAVAILABLE
        assert unavailable.buffer_origin_sample_16k == 0
        assert not await detector.finish_provider_speaker_evidence_lease(lease)
    finally:
        await detector.close()


async def test_provider_exact_long_utterance_commits_continuation_coverage() -> None:
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        applied = await detector.anchor_provider_speaker_evidence(
            lease,
            audio_start_sample_16k=0,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert applied.status is ProviderSpeakerEvidenceAnchorStatus.APPLIED
        assert applied.observed_through_sample_16k == 0
        duplicate = await detector.anchor_provider_speaker_evidence(
            lease,
            audio_start_sample_16k=0,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert duplicate.status is ProviderSpeakerEvidenceAnchorStatus.IDEMPOTENT
        assert duplicate.observed_through_sample_16k == 0

        final_capture = None
        for sequence_no in range(1, 13):
            update = await detector.observe_provider_audio_ordered(
                _speaker_pcm(1_000),
                sample_rate_hz=16_000,
                identity=identity,
                sequence_no=sequence_no,
                split_before_audio=False,
                speaker_evidence_lease=lease,
            )
            assert update is not None
            final_capture = update.capture
            await shadow.wait_idle()

        state = detector._provider_speaker_evidence_state
        assert state is not None
        assert len(state.coverage_candidates) == 4
        assert state.completed_window_sample_count == 48_000
        assert final_capture is not None
        assert final_capture.decision_state is SpeakerShadowCaptureDecisionState.PENDING

        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 192_000),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        assert committed.snapshot.boundary_exact is True
        assert committed.snapshot.evidence_complete is True
        await shadow.wait_idle()
        assert shadow.snapshot()["retained_pcm_bytes"] == 0
    finally:
        await detector.close()


async def test_provider_exact_at_primary_completion_stages_successor_pcm() -> None:
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        await _anchor_provider_evidence(detector, lease)
        for sequence_no in range(1, 4):
            update = await detector.observe_provider_audio_ordered(
                _speaker_pcm(1_000),
                sample_rate_hz=16_000,
                identity=identity,
                sequence_no=sequence_no,
                split_before_audio=False,
                speaker_evidence_lease=lease,
            )
            assert update is not None
            await shadow.wait_idle()

        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 48_000),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None
        next_pcm = await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=4,
            split_before_audio=True,
            speaker_evidence_lease=lease,
        )
        assert next_pcm is not None
        assert next_pcm.capture.accepted_sample_count == 1_600

        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        assert committed.successor_evidence_lease is not None
        assert (
            committed.successor_evidence_lease.candidate
            == reservation.suffix_candidate
        )
        await shadow.wait_idle()
        suffix = reservation.suffix_candidate
        assert suffix is not None
        assert shadow._buffers[suffix].sample_count == 1_600
    finally:
        await detector.close()


async def test_provider_exact_primary_abort_fences_unrecoverable_successor_pcm() -> (
    None
):
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        await _anchor_provider_evidence(detector, lease)
        for sequence_no in range(1, 4):
            assert await detector.observe_provider_audio_ordered(
                _speaker_pcm(1_000),
                sample_rate_hz=16_000,
                identity=identity,
                sequence_no=sequence_no,
                split_before_audio=False,
                speaker_evidence_lease=lease,
            )
            await shadow.wait_idle()

        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 48_000),
            speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100),
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=4,
            split_before_audio=True,
            speaker_evidence_lease=lease,
        )

        assert not detector.abort_provider_exact_speaker_interval(reservation)
        assert detector._provider_speaker_evidence_state is None
        assert not await detector.finish_provider_speaker_evidence_lease(lease)
        assert reservation.suffix_candidate not in shadow._candidate_tokens
    finally:
        await detector.close()


async def test_provider_audio_timeline_reset_abandons_stable_speaker_evidence() -> None:
    shadow = _StableEvidenceSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        evidence_lease = await detector.ensure_provider_speaker_evidence_lease()
        assert evidence_lease is not None
        assert await detector.observe_provider_audio_ordered(
            b"\x11\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
            speaker_evidence_lease=evidence_lease,
        )

        assert await detector.reset_provider_audio_timeline()
        assert shadow.abandoned == [evidence_lease.candidate]
        assert not await detector.finish_provider_speaker_evidence_lease(evidence_lease)
        successor = await detector.ensure_provider_speaker_evidence_lease()
        assert successor is not None and successor != evidence_lease
    finally:
        await detector.close()


async def test_real_terminal_capture_survives_twelve_second_exact_boundary() -> None:
    (
        detector,
        shadow,
        lease,
        token,
        observations,
    ) = await _open_real_terminal_provider_candidate(duration_ms=12_000)
    try:
        assert [
            (observation.checkpoint_ms, observation.similarity)
            for observation in observations
        ] == [(1_500, 0.2), (3_000, 0.2)]
        segment = detector._provider_speaker_segments[0]
        assert segment.ownership_complete is True
        assert segment.shadow_capture_state.value == "complete"
        assert segment.last_progress_at > segment.created_at
        assert (
            segment.detector_candidate
            not in detector._provider_micro_event_ambiguous_candidates
        )

        snapshot = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 12 * 16_000)
        )
        assert snapshot is not None
        assert snapshot.boundary_exact is True
        deadline = asyncio.get_running_loop().time() + 1.0
        assert await detector.wait_provider_speaker_preseal(
            snapshot,
            deadline=deadline,
        )
        assert (
            await lease.commit_async(deadline=asyncio.get_running_loop().time() + 1.0)
            is DetectorCandidateRejectionCommitResult.PRESEAL_READY
        )
        fence = await detector.seal_provider_candidate(
            token,
            speaker_snapshot=snapshot,
        )
        assert fence is not None
        assert fence.boundary_exact is True
        assert (
            await lease.commit_async(deadline=asyncio.get_running_loop().time() + 1.0)
            is DetectorCandidateRejectionCommitResult.SEALED_APPLIED
        )
    finally:
        await detector.close()


async def test_terminal_coverage_suffix_is_unavailable_and_micro_event_ambiguous() -> (
    None
):
    (
        detector,
        _shadow,
        _lease,
        _token,
        _observations,
    ) = await _open_real_terminal_provider_candidate(duration_ms=12_000)
    try:
        snapshot = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 10 * 16_000)
        )
        assert snapshot is not None
        assert snapshot.boundary_exact is True
        assert await detector.wait_provider_speaker_preseal(
            snapshot,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert len(detector._provider_speaker_segments) == 1
        successor = detector._provider_speaker_segments[0]
        assert successor.shadow_capture_state.value == "unavailable"
        assert (
            successor.detector_candidate
            in detector._provider_micro_event_ambiguous_candidates
        )
    finally:
        await detector.close()


@pytest.mark.parametrize(
    ("boundary",),
    [
        (ProviderAudioRange(1_600, 48_000),),
        (ProviderAudioRange(0, 46_400),),
    ],
    ids=("trimmed-prefix", "short-terminal-window"),
)
async def test_real_terminal_capture_rejects_trimmed_scoring_coverage(
    boundary: ProviderAudioRange,
) -> None:
    (
        detector,
        _shadow,
        _lease,
        _token,
        _observations,
    ) = await _open_real_terminal_provider_candidate(duration_ms=3_000)
    try:
        snapshot = await detector.reconcile_provider_endpoint(boundary)
        assert snapshot is not None
        assert snapshot.boundary_exact is False
        assert snapshot.evidence_complete is False
    finally:
        await detector.close()


async def test_malformed_terminal_receipt_is_revoked_without_batch_fallback() -> None:
    shadow = _MalformedTerminalReceiptSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        await detector.observe_provider_audio_ordered(
            b"\x21\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
        )

        snapshot = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 160)
        )

        assert snapshot is not None and snapshot.boundary_exact is False
        assert len(shadow.terminal_receipts) == 1
        receipt = shadow.terminal_receipts[0]
        assert shadow.revoked_terminal_receipts == [receipt]
        assert shadow.batch_requests == []
        shadow.terminal_receipt_statuses[receipt] = "applied"
        assert shadow.terminal_coverage_status(receipt) == "failed"
        fence = await detector.seal_provider_candidate(
            token,
            speaker_snapshot=snapshot,
        )
        assert fence is not None and fence.boundary_exact is False
    finally:
        await detector.close()


async def test_malformed_batch_receipt_is_revoked_before_unknown_fallback() -> None:
    shadow = _MalformedBatchReceiptSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        await detector.observe_provider_audio_ordered(
            b"\x31\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
        )

        snapshot = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 160)
        )

        assert snapshot is not None and snapshot.boundary_exact is False
        assert len(shadow.batch_receipts) == 1
        receipt = shadow.batch_receipts[0]
        assert shadow.revoked_receipts == [receipt]
        shadow.receipt_statuses[receipt] = "applied"
        assert shadow.reconciliation_status(receipt) == "failed"
        fence = await detector.seal_provider_candidate(
            token,
            speaker_snapshot=snapshot,
        )
        assert fence is not None and fence.boundary_exact is False
    finally:
        await detector.close()


async def test_provider_seal_deadline_expires_without_late_fence_mutation() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        candidate, identity, token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        await detector.observe_provider_audio_ordered(
            b"\x41\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
        )
        snapshot = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 160)
        )
        assert snapshot is not None and snapshot.boundary_exact is True

        await detector._lock.acquire()
        try:
            fence = await asyncio.wait_for(
                detector.seal_provider_candidate(
                    token,
                    speaker_snapshot=snapshot,
                    deadline=asyncio.get_running_loop().time() + 0.01,
                ),
                timeout=1.0,
            )
        finally:
            detector._lock.release()

        assert fence is None
        assert detector._provider_candidate_fence is None
        assert detector._candidate_generation == candidate.candidate_generation
        assert detector._provider_preseal_entries[0].verdict is snapshot

        fence = await detector.seal_provider_candidate(
            token,
            speaker_snapshot=snapshot,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
        assert fence is not None and fence.boundary_exact is True
    finally:
        if detector._lock.locked():
            detector._lock.release()
        await detector.close()


async def test_provider_segment_expiry_tracks_idle_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        detector_runtime_module,
        "_PROVIDER_SEGMENT_EXPIRY_SECONDS",
        0.05,
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=_ReconcilingSpeakerShadowSpy(),
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        for sequence_no in range(1, 5):
            await detector.observe_provider_audio_ordered(
                b"\x11\x00" * 160,
                sample_rate_hz=16_000,
                identity=identity,
                sequence_no=sequence_no,
                split_before_audio=False,
            )
            await asyncio.sleep(0.025)
        assert detector._provider_speaker_segments

        await asyncio.sleep(0.08)
        assert not detector._provider_speaker_segments
        assert detector._provider_segment_alignment_lost is True
    finally:
        await detector.close()


async def test_provider_sequence_gap_does_not_renew_idle_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        detector_runtime_module,
        "_PROVIDER_SEGMENT_EXPIRY_SECONDS",
        0.08,
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=_ReconcilingSpeakerShadowSpy(),
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        await detector.observe_provider_audio_ordered(
            b"\x11\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
        )
        progress_at = detector._provider_speaker_segments[0].last_progress_at
        await asyncio.sleep(0.05)
        await detector.observe_provider_audio_ordered(
            b"\x12\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=3,
            split_before_audio=False,
        )
        assert detector._provider_speaker_segments[0].last_progress_at == progress_at

        await asyncio.sleep(0.06)
        assert not detector._provider_speaker_segments
    finally:
        await detector.close()


async def test_provider_preseal_wait_cancelled_during_post_await_lock_revokes() -> None:
    shadow = _BlockingSettlementSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        await detector.observe_provider_audio_ordered(
            b"\x31\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
        )
        snapshot = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 160)
        )
        assert snapshot is not None and snapshot.boundary_exact
        wait_task = asyncio.create_task(
            detector.wait_provider_speaker_preseal(
                snapshot,
                deadline=asyncio.get_running_loop().time() + 1.0,
            )
        )
        await shadow.settlement_started.wait()
        await detector._lock.acquire()
        shadow.settlement_release.set()
        await shadow.settlement_returning.wait()
        wait_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait_task
        assert shadow.revoked_receipts == shadow.batch_receipts
        detector._lock.release()
    finally:
        if detector._lock.locked():
            detector._lock.release()
        await detector.close()


async def test_provider_preseal_wait_reset_during_settlement_fails_open() -> None:
    shadow = _BlockingSettlementSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        await detector.observe_provider_audio_ordered(
            b"\x31\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
        )
        snapshot = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 160)
        )
        assert snapshot is not None and snapshot.boundary_exact
        wait_task = asyncio.create_task(
            detector.wait_provider_speaker_preseal(
                snapshot,
                deadline=asyncio.get_running_loop().time() + 1.0,
            )
        )
        await shadow.settlement_started.wait()
        await detector.reset()
        shadow.settlement_release.set()
        assert await wait_task is False
        assert shadow.revoked_receipts == shadow.batch_receipts
    finally:
        await detector.close()


async def test_provider_preseal_late_applied_receipt_is_revoked_after_deadline() -> (
    None
):
    shadow = _BlockingSettlementSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _candidate, identity, _token = await _open_provider_candidate(
            detector,
            turn_id=1,
        )
        await detector.observe_provider_audio_ordered(
            b"\x31\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=1,
            split_before_audio=False,
        )
        snapshot = await detector.reconcile_provider_endpoint(
            ProviderAudioRange(0, 160)
        )
        assert snapshot is not None and snapshot.boundary_exact
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.02
        wait_task = asyncio.create_task(
            detector.wait_provider_speaker_preseal(
                snapshot,
                deadline=deadline,
            )
        )
        await shadow.settlement_started.wait()
        loop.call_at(deadline + 0.01, shadow.settlement_release.set)

        assert await asyncio.wait_for(wait_task, 1) is False
        assert shadow.revoked_receipts == shadow.batch_receipts
    finally:
        await detector.close()


async def test_speaker_shadow_default_none_keeps_smart_turn_callbacks_installed() -> (
    None
):
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
    )

    adapter = detector._semantic_adapter
    assert adapter is not None
    assert adapter._on_accepted_audio is not None
    assert adapter._on_candidate_complete is not None
    await detector.close()


async def test_provider_shadow_observes_admitted_pcm_until_explicit_seal() -> None:
    shadow = _PendingCompletionSpeakerShadowSpy()
    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    first_pcm = b"\x01\x00" * 160
    second_pcm = b"\x02\x00" * 160

    feed_result = await detector.feed(
        first_pcm,
        speech_probability=0.9,
        ingress_token=_ingress_token(),
    )

    assert feed_result.events == (SpeechActivityEvent.CANDIDATE_PAUSE,)
    assert shadow.frames == []
    assert shadow.finished == []

    detector.observe_provider_audio(first_pcm, sample_rate_hz=16_000)
    detector.observe_provider_audio(second_pcm, sample_rate_hz=16_000)

    candidate = SpeakerShadowCandidateKey(0, 0, "provider_candidate")
    assert shadow.frames == [
        (first_pcm, 16_000, candidate),
        (second_pcm, 16_000, candidate),
    ]
    assert shadow.frames[0][0] is first_pcm
    assert shadow.frames[1][0] is second_pcm
    assert shadow.finished == []

    fence = await detector.seal_provider_candidate()
    assert fence is not None
    assert shadow.finished == [candidate]
    assert await detector.seal_provider_candidate() == fence
    assert shadow.finished == [candidate]

    successor_pcm = b"\x03\x00" * 160
    detector.observe_provider_audio(successor_pcm, sample_rate_hz=16_000)
    assert shadow.frames[-1] == (
        successor_pcm,
        16_000,
        SpeakerShadowCandidateKey(0, 1, "provider_candidate"),
    )
    assert await detector.discard_provider_successor(fence) is True
    assert detector._speaker_shadow_candidate is None
    assert detector._speaker_shadow_generation == 2
    successor_candidate = SpeakerShadowCandidateKey(
        0,
        1,
        "provider_candidate",
    )
    assert shadow.finished == [candidate, successor_candidate]
    assert shadow.pending_completions == [candidate, successor_candidate]
    assert shadow.reset_calls == 0

    replacement_pcm = b"\x04\x00" * 160
    detector.observe_provider_audio(replacement_pcm, sample_rate_hz=16_000)
    assert shadow.frames[-1] == (
        replacement_pcm,
        16_000,
        SpeakerShadowCandidateKey(0, 2, "provider_candidate"),
    )

    await detector.close()
    assert shadow.close_calls == 1


async def test_ordered_discard_finishes_only_successor_without_reset() -> None:
    shadow = _PendingCompletionSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    (
        _predecessor,
        predecessor_identity,
        predecessor_token,
    ) = await _open_provider_candidate(detector, turn_id=1)
    predecessor_pcm = b"\x05\x00" * 160
    await detector.observe_provider_audio_ordered(
        predecessor_pcm,
        sample_rate_hz=16_000,
        identity=predecessor_identity,
        sequence_no=1,
        split_before_audio=False,
    )
    predecessor_shadow = detector._provider_speaker_segments[0].candidate
    assert predecessor_shadow == SpeakerShadowCandidateKey(
        0,
        0,
        "provider_candidate",
    )
    fence = await detector.seal_provider_candidate(predecessor_token)
    assert fence is not None
    assert shadow.pending_completions == [predecessor_shadow]
    assert not detector._provider_speaker_segments

    successor_pcm = b"\x06\x00" * 160
    successor = await detector.feed(
        successor_pcm,
        ingress_token=_ingress_token(),
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert successor.identity is not None
    await detector.observe_provider_audio_ordered(
        successor_pcm,
        sample_rate_hz=16_000,
        identity=successor.identity,
        sequence_no=2,
        split_before_audio=False,
    )
    successor_shadow = detector._provider_speaker_segments[0].candidate
    assert successor_shadow is not None
    assert successor_shadow != predecessor_shadow
    deferred_pcm = b"\x07\x00" * 160
    deferred = await detector.feed(
        deferred_pcm,
        ingress_token=_ingress_token(),
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert deferred.identity is not None
    await detector.observe_provider_audio_ordered(
        deferred_pcm,
        sample_rate_hz=16_000,
        identity=deferred.identity,
        sequence_no=3,
        split_before_audio=True,
    )
    deferred_shadow = detector._provider_speaker_segments[1].candidate
    assert deferred_shadow is not None
    assert deferred_shadow not in {predecessor_shadow, successor_shadow}

    assert await detector.discard_provider_successor(fence) is True

    assert shadow.finished == [
        predecessor_shadow,
        successor_shadow,
        deferred_shadow,
    ]
    assert shadow.pending_completions == [
        predecessor_shadow,
        successor_shadow,
        deferred_shadow,
    ]
    assert shadow.reset_calls == 0
    assert detector._provider_candidate_fence == fence
    assert detector._provider_segment_ordered_mode is True
    assert detector._provider_segment_last_sequence_no == 3
    assert detector._speaker_shadow_generation >= 4
    assert not detector._provider_speaker_segments
    assert detector._provider_segment_expiry_task is None
    assert not detector._provider_segment_retired_expiry_tasks

    replacement_pcm = b"\x08\x00" * 160
    replacement = await detector.feed(
        replacement_pcm,
        ingress_token=_ingress_token(),
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert replacement.identity is not None
    await detector.observe_provider_audio_ordered(
        replacement_pcm,
        sample_rate_hz=16_000,
        identity=replacement.identity,
        sequence_no=4,
        split_before_audio=False,
    )
    replacement_segment = detector._provider_speaker_segments[0]
    assert replacement_segment.candidate is not None
    assert replacement_segment.candidate not in {
        predecessor_shadow,
        successor_shadow,
        deferred_shadow,
    }
    assert replacement_segment.evidence_complete is True
    assert replacement_segment.ownership_ambiguous is False
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["provider_speaker_segment_sequence_gap_count"] == 0
    await detector.close()


@pytest.mark.parametrize("status", ["missing", "false"])
async def test_ordered_provider_api_keeps_default_legacy_observer_unchanged(
    status: str,
) -> None:
    shadow = (
        _SpeakerShadowSpy()
        if status == "missing"
        else _DeferredSpeakerShadowSpy(support=False)
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    first = b"\x11\x00" * 160
    second = b"\x12\x00" * 160

    await detector.observe_provider_audio_ordered(
        first,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=40,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        second,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=41,
        split_before_audio=True,
        evidence_complete=False,
    )

    legacy_candidate = SpeakerShadowCandidateKey(0, 0, "provider_candidate")
    assert detector._provider_segment_ordered_mode is False
    assert not detector._provider_speaker_segments
    assert [frame[2] for frame in shadow.frames] == [
        legacy_candidate,
        legacy_candidate,
    ]
    fence = await detector.seal_provider_candidate(token)
    assert fence is not None
    assert await detector.prepare_candidate_rejection(legacy_candidate) is not None
    await detector.close()


async def test_ordered_provider_status_exception_latches_legacy_fail_open() -> None:
    shadow = _DeferredSpeakerShadowSpy(support_error=RuntimeError("status unavailable"))
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )

    await detector.observe_provider_audio_ordered(
        b"\x21\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    shadow.support_error = None
    shadow.support = True
    await detector.observe_provider_audio_ordered(
        b"\x22\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=2,
        split_before_audio=False,
    )

    assert shadow.support_calls == 1
    assert detector._provider_segment_deferred_support == "error"
    assert detector._provider_segment_ordered_mode is False
    legacy_candidate = SpeakerShadowCandidateKey(0, 0, "provider_candidate")
    fence = await detector.seal_provider_candidate(token)
    assert fence is not None
    assert await detector.prepare_candidate_rejection(legacy_candidate) is None
    await detector.close()


async def test_ordered_provider_overlap_preserves_exact_fifo_ownership() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    candidate_a, identity_a, token_a = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x31\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=10,
        split_before_audio=False,
    )
    shadow_a = detector._provider_speaker_segments[0].candidate
    assert shadow_a is not None
    open_a = await detector.prepare_candidate_rejection(shadow_a)
    assert open_a is not None

    await detector.observe_provider_audio_ordered(
        b"\x32\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=11,
        split_before_audio=True,
    )
    shadow_b = detector._provider_speaker_segments[1].candidate
    assert shadow_b is not None
    assert shadow.events.index(("defer", shadow_b)) < shadow.events.index(
        ("submit", shadow_b)
    )
    assert open_a.commit() is False
    assert await detector.prepare_candidate_rejection(shadow_a) is None
    assert candidate_a not in detector._provider_micro_event_ambiguous_candidates
    assert detector._provider_speaker_segments[1].tentative is True

    snapshot_a = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert snapshot_a is not None
    assert snapshot_a.successor_present is True
    fence_a = await detector.seal_provider_candidate(
        token_a,
        speaker_snapshot=snapshot_a,
    )
    assert fence_a is not None
    assert fence_a.boundary_exact is True
    assert fence_a.successor_present is True
    assert await detector.prepare_candidate_rejection(shadow_a) is not None
    assert await detector.complete_provider_candidate(fence_a) is True

    candidate_b, identity_b, token_b = await _open_provider_candidate(
        detector,
        turn_id=2,
    )
    assert candidate_b == DetectorCandidateKey(0, 1)
    await detector.observe_provider_audio_ordered(
        b"\x33\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_b,
        sequence_no=12,
        split_before_audio=False,
    )
    snapshot_b = await detector.reconcile_provider_endpoint(
        ProviderAudioRange(160, 480)
    )
    assert snapshot_b is not None
    fence_b = await detector.seal_provider_candidate(
        token_b,
        speaker_snapshot=snapshot_b,
    )

    assert fence_b is not None
    assert shadow.events.index(("activate", shadow_b)) < shadow.events.index(
        ("finish", shadow_b)
    )
    sealed_b = await detector.prepare_candidate_rejection(shadow_b)
    assert sealed_b is not None
    assert sealed_b.provider_fence == fence_b
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["provider_speaker_segment_split_count"] == 1
    assert diagnostics["provider_speaker_segment_deferred_count"] == 1
    assert diagnostics["provider_speaker_segment_activated_count"] == 1
    assert diagnostics["provider_speaker_segment_exact_snapshot_count"] == 2
    await detector.close()


async def test_exact_fence_through_sequence_does_not_consume_unobserved_successor() -> (
    None
):
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    candidate_a, identity_a, token_a = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x51\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=1,
        split_before_audio=False,
    )
    successor_feed = await detector.feed(
        b"\x52\x00" * 160,
        ingress_token=_ingress_token(),
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert successor_feed.candidate == candidate_a
    assert successor_feed.identity is not None
    assert successor_feed.identity.sequence_no > identity_a.sequence_no

    snapshot_a = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert snapshot_a is not None
    assert snapshot_a.through_sequence_no == identity_a.sequence_no
    fence_a = await detector.seal_provider_candidate(
        token_a,
        speaker_snapshot=snapshot_a,
    )
    assert fence_a is not None
    assert fence_a.through_sequence_no == identity_a.sequence_no
    assert await detector.complete_provider_candidate(fence_a) is True

    candidate_b = DetectorCandidateKey(0, 1)
    token_b = VoiceTurnToken(_ingress_token(), turn_id=2)
    assert await detector.bind_candidate(candidate_b, token_b) is not None
    await detector.observe_provider_audio_ordered(
        b"\x52\x00" * 160,
        sample_rate_hz=16_000,
        identity=successor_feed.identity,
        sequence_no=2,
        split_before_audio=False,
    )

    assert len(detector._provider_speaker_segments) == 1
    successor = detector._provider_speaker_segments[0]
    assert (successor.start_sample_16k, successor.end_sample_16k) == (160, 320)
    snapshot_b = await detector.reconcile_provider_endpoint(
        ProviderAudioRange(160, 320)
    )
    assert snapshot_b is not None
    assert snapshot_b.candidate_generation == candidate_b.candidate_generation
    fence_b = await detector.seal_provider_candidate(
        token_b,
        speaker_snapshot=snapshot_b,
    )
    assert fence_b is not None
    assert fence_b.boundary_exact is True
    await detector.close()


async def test_exact_boundary_rejects_cursor_gap_after_late_sealed_chunk() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate_a, identity_a, token_a = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    successor_feed = await detector.feed(
        b"\x62\x00" * 160,
        ingress_token=_ingress_token(),
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert successor_feed.identity is not None
    await detector.observe_provider_audio_ordered(
        b"\x61\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=1,
        split_before_audio=False,
    )
    snapshot_a = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert snapshot_a is not None
    fence_a = await detector.seal_provider_candidate(
        token_a,
        speaker_snapshot=snapshot_a,
    )
    assert fence_a is not None

    await detector.observe_provider_audio_ordered(
        b"\x62\x00" * 160,
        sample_rate_hz=16_000,
        identity=successor_feed.identity,
        sequence_no=2,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x63\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=3,
        split_before_audio=False,
    )

    assert detector._provider_audio_sample_cursor_16k == 480
    assert [
        (segment.start_sample_16k, segment.end_sample_16k)
        for segment in detector._provider_speaker_segments
    ] == [(160, 320)]
    cursor_gap = await detector.reconcile_provider_endpoint(
        ProviderAudioRange(160, 480)
    )
    assert type(cursor_gap) is ProviderSpeakerBoundarySnapshot
    assert cursor_gap.boundary_exact is False
    assert cursor_gap.evidence_complete is False
    assert not detector._provider_speaker_segments
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["provider_speaker_segment_exact_reconcile_failed_count"] == 1
    assert diagnostics["provider_speaker_segment_unknown_retired_count"] == 1
    await detector.close()


async def test_exact_boundary_merges_pause_segments_before_seal() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, first_identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    second_feed = await detector.feed(
        b"\x02\x00" * 160,
        ingress_token=_ingress_token(),
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert second_feed.identity is not None
    head_pcm = b"\x11\x00" * 12_800
    tail_pcm = b"\x12\x00" * 40_000
    await detector.observe_provider_audio_ordered(
        head_pcm,
        sample_rate_hz=16_000,
        identity=first_identity,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        tail_pcm,
        sample_rate_hz=16_000,
        identity=second_feed.identity,
        sequence_no=2,
        split_before_audio=True,
    )
    head = detector._provider_speaker_segments[0].candidate
    tail = detector._provider_speaker_segments[1].candidate
    assert head is not None and tail is not None

    snapshot = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 52_800))

    assert snapshot is not None
    assert snapshot.merged_resume_count == 1
    assert snapshot.successor_present is False
    assert shadow.sample_counts[head] == 52_800
    assert tail not in shadow.sample_counts
    assert shadow.reconciliations == [(tail, head, 40_000, None)]
    assert not detector._provider_speaker_segments
    fence = await detector.seal_provider_candidate(
        token,
        speaker_snapshot=snapshot,
    )
    assert fence is not None
    assert fence.boundary_exact is True
    assert fence.merged_resume_count == 1
    assert await detector.prepare_candidate_rejection(head) is not None
    await detector.close()


async def test_provider_audio_timeline_reset_preserves_local_identity_and_rebases_exact_range() -> (
    None
):
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x41\x00" * 320,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=7,
        split_before_audio=False,
    )
    detector_epoch = detector.detector_epoch
    candidate_generation = detector._candidate_generation
    ingress_token = detector._ingress_token
    detector_sequence = detector._sequence_no
    assert detector._provider_audio_sample_cursor_16k == 320

    assert await detector.reset_provider_audio_timeline() is True

    assert detector.detector_epoch == detector_epoch
    assert detector._candidate_generation == candidate_generation
    assert detector._ingress_token == ingress_token
    assert detector._sequence_no == detector_sequence
    assert detector.candidate_open is True
    assert detector._provider_audio_sample_cursor_16k == 0
    assert detector._provider_segment_last_sequence_no is None
    assert detector._provider_segment_ordered_mode is False
    assert not detector._provider_speaker_segments
    assert detector._provider_candidate_fence is None

    observed_through = asyncio.create_task(
        detector.wait_provider_audio_observed_through(160)
    )
    await asyncio.sleep(0)
    assert not observed_through.done()
    await detector.observe_provider_audio_ordered(
        b"\x42\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=8,
        split_before_audio=False,
    )
    assert await observed_through is True
    snapshot = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert snapshot is not None
    assert snapshot.detector_epoch == detector_epoch
    assert snapshot.candidate_generation == candidate.candidate_generation
    fence = await detector.seal_provider_candidate(
        token,
        speaker_snapshot=snapshot,
    )
    assert fence is not None
    assert fence.boundary_exact is True
    await detector.close()


@pytest.mark.parametrize("action", ["reset", "close"])
async def test_provider_audio_observation_wait_is_revoked_by_timeline_end(
    action: str,
) -> None:
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=_ReconcilingSpeakerShadowSpy(),
    )
    waiter = asyncio.create_task(detector.wait_provider_audio_observed_through(160))
    await asyncio.sleep(0)
    assert not waiter.done()

    if action == "reset":
        assert await detector.reset_provider_audio_timeline() is True
    else:
        await detector.close()

    assert await waiter is False
    if action == "reset":
        await detector.close()


async def test_exact_boundary_splits_one_chunk_without_sample_loss() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x21\x00" * 1_000,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    head = detector._provider_speaker_segments[0].candidate
    assert head is not None

    snapshot = await detector.reconcile_provider_endpoint(ProviderAudioRange(200, 600))

    assert snapshot is not None
    assert snapshot.successor_present is True
    successor = detector._provider_speaker_segments[0]
    assert successor.candidate is not None
    assert (successor.start_sample_16k, successor.end_sample_16k) == (600, 1_000)
    range_candidate = shadow.reconciliations[0][3]
    assert range_candidate is not None
    assert shadow.sample_counts[head] == 200
    assert shadow.sample_counts[range_candidate] == 400
    assert shadow.sample_counts[successor.candidate] == 400
    assert shadow.reconciliations == [
        (head, head, 200, range_candidate),
        (range_candidate, range_candidate, 400, successor.candidate),
    ]
    fence = await detector.seal_provider_candidate(
        token,
        speaker_snapshot=snapshot,
    )
    assert fence is not None
    assert fence.boundary_exact is True
    assert fence.successor_present is True
    await detector.close()


async def test_unknown_boundary_retires_tail_without_losing_asr_fence() -> None:
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x31\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x32\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=2,
        split_before_audio=True,
    )
    head, tail = [segment.candidate for segment in detector._provider_speaker_segments]
    assert head is not None and tail is not None
    assert detector._bound_turn_token_for_speaker_candidate(head) == token

    await detector.retire_provider_speaker_boundary_unknown()
    await detector.retire_provider_speaker_boundary_unknown()

    assert not detector._provider_speaker_segments
    assert ("activate", tail) not in shadow.events
    assert ("finish", head) in shadow.events
    assert ("finish", tail) in shadow.events
    assert detector._bound_turn_token_for_speaker_candidate(head) == token
    fence = await detector.seal_provider_candidate(token)
    assert fence is not None
    assert fence.boundary_exact is False
    assert await detector.prepare_candidate_rejection(head) is None
    assert (
        detector.speaker_rejection_diagnostics_snapshot()[
            "provider_speaker_segment_unknown_retired_count"
        ]
        == 1
    )
    assert detector.release_speaker_candidate_binding(head, token)
    await detector.close()


async def test_e1_exact_is_preserved_when_e2_retires_as_unknown() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate_a, identity_a, token_a = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x61\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x62\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=2,
        split_before_audio=True,
    )

    exact_a = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert exact_a is not None and exact_a.boundary_exact is True
    receipt_a = shadow.batch_receipts[-1]
    unknown_b = await detector.retire_provider_speaker_boundary_unknown()

    assert unknown_b is not None and unknown_b.boundary_exact is False
    assert unknown_b.candidate_generation == 1
    assert detector._provider_preseal_entries[0].verdict is exact_a
    assert detector._provider_preseal_entries[1].verdict is unknown_b
    assert receipt_a not in shadow.revoked_receipts

    fence_a = await detector.seal_provider_candidate(
        token_a,
        speaker_snapshot=exact_a,
    )
    assert fence_a is not None and fence_a.boundary_exact is True
    assert await detector.complete_provider_candidate(fence_a) is True

    _candidate_b, _identity_b, token_b = await _open_provider_candidate(
        detector,
        turn_id=2,
    )
    fence_b = await detector.seal_provider_candidate(
        token_b,
        speaker_snapshot=unknown_b,
    )
    assert fence_b is not None and fence_b.boundary_exact is False
    assert fence_b.candidate_generation == 1
    await detector.close()


async def test_late_consumed_verdict_does_not_clear_newer_preseal_entry() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate_a, identity_a, token_a = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x71\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x72\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=2,
        split_before_audio=True,
    )
    exact_a = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    exact_b = await detector.reconcile_provider_endpoint(ProviderAudioRange(160, 320))
    assert exact_a is not None and exact_b is not None
    receipt_b = shadow.batch_receipts[-1]

    fence_a = await detector.seal_provider_candidate(
        token_a,
        speaker_snapshot=exact_a,
    )
    assert fence_a is not None and fence_a.boundary_exact is True
    assert 0 not in detector._provider_preseal_entries
    assert detector._provider_preseal_entries[1].verdict is exact_b

    stale_unknown = await detector.retire_provider_speaker_boundary_unknown(exact_a)
    assert stale_unknown is not None and stale_unknown.boundary_exact is False
    assert stale_unknown._owner is not detector._provider_boundary_snapshot_owner
    assert detector._provider_preseal_entries[1].verdict is exact_b
    assert receipt_b not in shadow.revoked_receipts

    targeted_unknown = await detector.retire_provider_speaker_boundary_unknown(exact_b)
    repeated_unknown = await detector.retire_provider_speaker_boundary_unknown(exact_b)
    repeated_tombstone = await detector.retire_provider_speaker_boundary_unknown(
        targeted_unknown
    )
    assert targeted_unknown is not None
    assert repeated_unknown is targeted_unknown
    assert repeated_tombstone is targeted_unknown
    assert detector._provider_preseal_entries[1].verdict is targeted_unknown
    assert shadow.revoked_receipts.count(receipt_b) == 1
    await detector.close()


async def test_preseal_overflow_fails_only_new_generation_open() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    for sequence_no in range(1, 9):
        await detector.observe_provider_audio_ordered(
            bytes((0x20 + sequence_no, 0)) * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=sequence_no,
            split_before_audio=sequence_no > 1,
        )

    exact_verdicts = [
        await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    ]
    await detector.observe_provider_audio_ordered(
        b"\x39\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=9,
        split_before_audio=True,
    )
    for generation in range(1, 8):
        exact_verdicts.append(
            await detector.reconcile_provider_endpoint(
                ProviderAudioRange(generation * 160, (generation + 1) * 160)
            )
        )
    assert all(
        verdict is not None and verdict.boundary_exact for verdict in exact_verdicts
    )
    frozen_entries = dict(detector._provider_preseal_entries)
    frozen_receipts = tuple(shadow.batch_receipts)

    overflow = await detector.reconcile_provider_endpoint(
        ProviderAudioRange(1_280, 1_440)
    )

    assert overflow is not None and overflow.boundary_exact is False
    assert overflow.candidate_generation == 8
    assert detector._provider_preseal_entries == frozen_entries
    assert len(shadow.batch_requests) == 8
    assert not any(receipt in shadow.revoked_receipts for receipt in frozen_receipts)
    fence = await detector.seal_provider_candidate(
        token,
        speaker_snapshot=exact_verdicts[0],
    )
    assert fence is not None and fence.boundary_exact is True
    await detector.close()


async def test_exact_preseal_rejection_ready_upgrades_on_ordered_seal() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x51\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    shadow_candidate = detector._provider_speaker_segments[0].candidate
    assert shadow_candidate is not None
    exact = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert exact is not None and exact.boundary_exact is True

    lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert lease is not None
    assert lease.provider_preseal_verdict is exact
    assert lease.provider_fence is None
    assert lease.commit() is True
    assert detector._candidate_generation == candidate.candidate_generation
    assert detector._provider_candidate_fence is None
    assert detector._sealed_provider_candidate_rejection is None
    assert detector._provider_preseal_entries[0].rejection_ready is True

    fence = await detector.seal_provider_candidate(token, speaker_snapshot=exact)
    assert fence is not None and fence.boundary_exact is True
    sealed = detector._sealed_provider_candidate_rejection
    assert sealed is not None
    assert sealed.provider_fence == fence
    assert sealed.candidate == candidate
    assert sealed.shadow_candidate == shadow_candidate
    assert sealed.turn_token == token
    assert sealed.rejection_ready is True
    assert detector.ready_provider_speaker_rejection(fence) == shadow_candidate
    wrong_fence = ProviderCandidateFence(
        detector_epoch=fence.detector_epoch,
        candidate_generation=fence.candidate_generation + 1,
        through_sequence_no=fence.through_sequence_no,
        boundary_exact=True,
    )
    assert detector.ready_provider_speaker_rejection(wrong_fence) is None
    assert await detector.complete_provider_candidate(fence) is False
    assert detector.ready_provider_speaker_rejection(fence) is None
    await detector.close()


async def test_pending_exact_receipt_applies_before_final_deadline() -> None:
    shadow = _BlockingSettlementSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    candidate, identity, _token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x61\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    shadow_candidate = detector._provider_speaker_segments[0].candidate
    exact = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert exact is not None and exact.boundary_exact is True
    assert shadow.batch_status == "pending"

    lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert lease is not None
    assert lease.candidate == candidate
    assert lease.provider_preseal_verdict is exact
    commit = asyncio.create_task(
        lease.commit_async(deadline=asyncio.get_running_loop().time() + 1.0)
    )
    await asyncio.wait_for(shadow.settlement_started.wait(), 1.0)
    assert not commit.done()

    shadow.settlement_release.set()
    result = await asyncio.wait_for(commit, 1.0)

    assert result is DetectorCandidateRejectionCommitResult.PRESEAL_READY
    assert detector._provider_preseal_entries[0].rejection_ready is True
    await detector.close()


async def test_pending_exact_receipt_applies_after_ordered_seal_before_deadline() -> (
    None
):
    shadow = _BlockingSettlementSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x62\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    shadow_candidate = detector._provider_speaker_segments[0].candidate
    exact = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert exact is not None and exact.boundary_exact is True
    assert shadow.batch_status == "pending"

    lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert lease is not None
    assert lease.candidate == candidate
    deadline = asyncio.get_running_loop().time() + 1.0
    seal = asyncio.create_task(
        detector.seal_provider_candidate(
            token,
            speaker_snapshot=exact,
            deadline=deadline,
        )
    )
    await asyncio.wait_for(shadow.settlement_started.wait(), 1.0)
    commit = asyncio.create_task(lease.commit_async(deadline=deadline))
    await asyncio.sleep(0)
    assert not seal.done()
    assert not commit.done()

    shadow.settlement_release.set()
    fence = await asyncio.wait_for(seal, 1.0)
    result = await asyncio.wait_for(commit, 1.0)
    if result is DetectorCandidateRejectionCommitResult.PRESEAL_READY:
        # The rejection waiter may win the settlement lock immediately before
        # ordered seal. Runtime's ordered lane then reuses the same lease once
        # more against the sealed capability.
        result = await asyncio.wait_for(
            lease.commit_async(deadline=deadline),
            1.0,
        )

    assert fence is not None and fence.boundary_exact is True
    assert result is DetectorCandidateRejectionCommitResult.SEALED_APPLIED
    await detector.close()


async def test_pending_exact_seal_reset_during_settlement_cannot_mutate_successor() -> (
    None
):
    shadow = _BlockingSettlementSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x63\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    exact = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert exact is not None and exact.boundary_exact is True
    seal = asyncio.create_task(
        detector.seal_provider_candidate(
            token,
            speaker_snapshot=exact,
            deadline=asyncio.get_running_loop().time() + 1.0,
        )
    )
    await asyncio.wait_for(shadow.settlement_started.wait(), 1.0)

    await detector.reset()
    shadow.settlement_release.set()

    assert await asyncio.wait_for(seal, 1.0) is None
    assert detector._provider_candidate_fence is None
    assert not detector._provider_preseal_entries
    await detector.close()


async def test_batch_admission_failure_preserves_earlier_exact_entry() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x41\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x42\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=2,
        split_before_audio=True,
    )
    exact = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert exact is not None and exact.boundary_exact is True
    exact_receipt = shadow.batch_receipts[-1]
    shadow.batch_admitted = False

    unknown = await detector.reconcile_provider_endpoint(ProviderAudioRange(160, 320))

    assert unknown is not None and unknown.boundary_exact is False
    assert unknown.candidate_generation == 1
    assert len(shadow.batch_requests) == 2
    assert len(shadow.batch_receipts) == 1
    assert detector._provider_preseal_entries[0].verdict is exact
    assert detector._provider_preseal_entries[1].verdict is unknown
    assert exact_receipt not in shadow.revoked_receipts
    fence = await detector.seal_provider_candidate(token, speaker_snapshot=exact)
    assert fence is not None and fence.boundary_exact is True
    await detector.close()


@pytest.mark.parametrize("status", ["pending", "failed", "stale"])
async def test_non_applied_receipt_downgrades_only_its_generation(
    status: str,
) -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x31\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x32\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=2,
        split_before_audio=True,
    )
    exact_a = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    exact_b = await detector.reconcile_provider_endpoint(ProviderAudioRange(160, 320))
    assert exact_a is not None and exact_b is not None
    receipt_a, receipt_b = shadow.batch_receipts
    shadow.receipt_statuses[receipt_a] = status

    fence = await detector.seal_provider_candidate(
        token,
        speaker_snapshot=exact_a,
    )

    assert fence is not None and fence.boundary_exact is False
    assert receipt_a in shadow.revoked_receipts
    assert detector._provider_preseal_entries[1].verdict is exact_b
    assert receipt_b not in shadow.revoked_receipts
    await detector.close()


async def test_forged_exact_snapshot_fails_open_and_cannot_authorize() -> None:
    shadow = _ReconcilingSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x41\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    snapshot = await detector.reconcile_provider_endpoint(ProviderAudioRange(0, 160))
    assert snapshot is not None
    forged = ProviderSpeakerBoundarySnapshot(
        detector_epoch=snapshot.detector_epoch,
        candidate_generation=snapshot.candidate_generation,
        through_sequence_no=snapshot.through_sequence_no,
        shadow_generation=snapshot.shadow_generation,
        merged_resume_count=snapshot.merged_resume_count,
        successor_present=snapshot.successor_present,
        evidence_complete=snapshot.evidence_complete,
        _owner=object(),
    )

    fence = await detector.seal_provider_candidate(
        token,
        speaker_snapshot=forged,
    )

    assert fence is not None
    assert fence.boundary_exact is False
    assert not detector._provider_boundary_snapshots
    await detector.close()


async def test_runtime_legacy_fallback_does_not_create_cross_candidate_gap() -> None:
    policy = _provider_endpoint_policy()
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=policy,
        speaker_shadow=shadow,
    )
    lifecycle = VoiceInputLifecycleController(
        provider_policy=policy,
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    runtime = IndependentAsrRuntime(
        AsrRuntimeCallbacks(
            display_name=lambda: "provider-sequence-test",
            on_prepare_turn=AsyncMock(return_value=True),
            on_partial=AsyncMock(),
            on_final=AsyncMock(),
            on_turn_abandoned=AsyncMock(),
            on_failure=AsyncMock(),
            on_status=AsyncMock(),
            on_lifecycle=AsyncMock(),
        )
    )
    runtime._asr_session_epoch = 1
    runtime._asr_audio_generation = 1
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = detector
    runtime._asr_current_ingress_token = _ingress_token()

    candidate_a, identity_a, token_a = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    assert (
        await runtime._observe_admitted_provider_audio(
            lifecycle,
            detector,
            b"\x41\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity_a,
            split_before_audio=False,
            evidence_complete=True,
            turn_token=token_a,
        )
        is True
    )
    segment_a = detector._provider_speaker_segments[0]
    shadow_a = segment_a.candidate
    assert shadow_a is not None

    assert (
        await runtime._observe_admitted_provider_audio(
            lifecycle,
            detector,
            b"\x42\x00" * 160,
            sample_rate_hz=16_000,
            identity=None,
            split_before_audio=False,
            evidence_complete=False,
            turn_token=token_a,
        )
        is True
    )

    assert runtime._asr_provider_speaker_sequence == 1
    assert segment_a.evidence_complete is False
    assert segment_a.ownership_ambiguous is True
    assert candidate_a in detector._provider_micro_event_ambiguous_candidates

    fence_a = await detector.seal_provider_candidate(token_a)
    assert fence_a is not None
    assert await detector.prepare_candidate_rejection(shadow_a) is None
    assert await detector.complete_provider_candidate(fence_a) is False
    lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)

    candidate_b, identity_b, token_b = await _open_provider_candidate(
        detector,
        turn_id=2,
    )
    assert candidate_b == DetectorCandidateKey(0, 1)
    assert (
        await runtime._observe_admitted_provider_audio(
            lifecycle,
            detector,
            b"\x43\x00" * 160,
            sample_rate_hz=16_000,
            identity=identity_b,
            split_before_audio=False,
            evidence_complete=True,
            turn_token=token_b,
        )
        is True
    )

    assert runtime._asr_provider_speaker_sequence == 2
    segment_b = detector._provider_speaker_segments[0]
    assert segment_b.detector_candidate == candidate_b
    assert segment_b.evidence_complete is True
    assert segment_b.ownership_ambiguous is False
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["provider_speaker_segment_sequence_gap_count"] == 0

    shadow_b = segment_b.candidate
    assert shadow_b is not None
    fence_b = await detector.seal_provider_candidate(token_b)
    assert fence_b is not None
    sealed_b = await detector.prepare_candidate_rejection(shadow_b)
    assert sealed_b is None
    await detector.close()


async def test_ordered_provider_duplicate_stale_and_gap_are_bounded() -> None:
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    for sequence_no in (20, 20, 19, 22):
        await detector.observe_provider_audio_ordered(
            bytes([sequence_no, 0]) * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=sequence_no,
            split_before_audio=False,
        )

    assert len(shadow.frames) == 2
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["provider_speaker_segment_sequence_stale_count"] == 2
    assert diagnostics["provider_speaker_segment_sequence_gap_count"] == 1
    shadow_candidate = detector._provider_speaker_segments[0].candidate
    assert shadow_candidate is not None
    fence = await detector.seal_provider_candidate(token)
    assert fence is not None
    assert await detector.prepare_candidate_rejection(shadow_candidate) is None
    await detector.close()


async def test_ordered_provider_defer_failure_never_submits_or_activates_tail() -> None:
    shadow = _DeferredSpeakerShadowSpy(defer_result=False)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate_a, identity_a, token_a = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x39\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x3a\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=2,
        split_before_audio=True,
    )
    tail = detector._provider_speaker_segments[1]
    assert tail.candidate is not None
    assert ("submit", tail.candidate) not in shadow.events
    assert tail.evidence_complete is False

    fence_a = await detector.seal_provider_candidate(token_a)
    assert fence_a is not None
    assert await detector.complete_provider_candidate(fence_a) is False
    _candidate_b, identity_b, token_b = await _open_provider_candidate(
        detector,
        turn_id=2,
    )
    await detector.observe_provider_audio_ordered(
        b"\x3b\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_b,
        sequence_no=3,
        split_before_audio=False,
    )
    fence_b = await detector.seal_provider_candidate(token_b)
    assert fence_b is not None
    assert ("activate", tail.candidate) not in shadow.events
    assert await detector.prepare_candidate_rejection(tail.candidate) is None
    await detector.close()


async def test_ordered_provider_seal_binds_once_and_preserves_fence_equality() -> None:
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    ingress = _ingress_token()
    result = await detector.feed(
        b"\x3c\x00" * 160,
        ingress_token=ingress,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert result.identity is not None
    await detector.observe_provider_audio_ordered(
        b"\x3c\x00" * 160,
        sample_rate_hz=16_000,
        identity=result.identity,
        sequence_no=1,
        split_before_audio=False,
    )
    shadow_candidate = detector._provider_speaker_segments[0].candidate
    assert shadow_candidate is not None
    token = VoiceTurnToken(ingress, turn_id=1)
    fence = await detector.seal_provider_candidate(token)
    assert fence is not None
    assert fence.boundary_exact is False
    assert await detector.prepare_candidate_rejection(shadow_candidate) is None

    wrong_token = VoiceTurnToken(ingress, turn_id=2)
    assert await detector.seal_provider_candidate(wrong_token) == fence
    assert await detector.prepare_candidate_rejection(shadow_candidate) is None
    await detector.close()


async def test_ordered_provider_late_observation_cannot_cross_sealed_fence() -> None:
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    candidate, identity_1, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    result_2 = await detector.feed(
        b"\x42\x00" * 160,
        ingress_token=_ingress_token(),
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert result_2.candidate == candidate
    assert result_2.identity is not None
    await detector.observe_provider_audio_ordered(
        b"\x41\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_1,
        sequence_no=100,
        split_before_audio=False,
    )
    shadow_candidate = detector._provider_speaker_segments[0].candidate
    assert shadow_candidate is not None
    fence = await detector.seal_provider_candidate(token)
    assert fence is not None
    assert fence.boundary_exact is False
    assert await detector.prepare_candidate_rejection(shadow_candidate) is None

    await detector.observe_provider_audio_ordered(
        b"\x42\x00" * 160,
        sample_rate_hz=16_000,
        identity=result_2.identity,
        sequence_no=101,
        split_before_audio=False,
    )

    assert len(shadow.frames) == 1
    assert not detector._provider_speaker_segments
    assert await detector.prepare_candidate_rejection(shadow_candidate) is None
    await detector.close()


async def test_ordered_provider_preroll_ambiguity_carries_to_successor() -> None:
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate_a, identity_a, token_a = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x51\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_a,
        sequence_no=1,
        split_before_audio=False,
        evidence_complete=False,
    )
    shadow_a = detector._provider_speaker_segments[0].candidate
    assert shadow_a is not None
    fence_a = await detector.seal_provider_candidate(token_a)
    assert fence_a is not None
    assert await detector.prepare_candidate_rejection(shadow_a) is None
    assert await detector.complete_provider_candidate(fence_a) is False

    _candidate_b, identity_b, token_b = await _open_provider_candidate(
        detector,
        turn_id=2,
    )
    await detector.observe_provider_audio_ordered(
        b"\x52\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity_b,
        sequence_no=2,
        split_before_audio=False,
    )
    segment_b = detector._provider_speaker_segments[0]
    assert segment_b.evidence_complete is True
    assert segment_b.candidate is not None
    fence_b = await detector.seal_provider_candidate(token_b)
    assert fence_b is not None
    assert await detector.prepare_candidate_rejection(segment_b.candidate) is None
    await detector.close()


async def test_ordered_provider_expiry_never_activates_and_loses_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from main_logic.asr_client.endpointing import detector_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_PROVIDER_SEGMENT_EXPIRY_SECONDS", 0.01)
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x61\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x62\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=2,
        split_before_audio=True,
    )
    deferred = detector._provider_speaker_segments[1].candidate
    assert deferred is not None

    await asyncio.sleep(0.03)

    assert not detector._provider_speaker_segments
    assert ("activate", deferred) not in shadow.events
    assert ("finish", deferred) in shadow.events
    assert (
        detector.speaker_rejection_diagnostics_snapshot()[
            "provider_speaker_segment_expired_count"
        ]
        == 2
    )
    await detector.observe_provider_audio_ordered(
        b"\x63\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=3,
        split_before_audio=False,
    )
    successor = detector._provider_speaker_segments[0]
    assert successor.evidence_complete is False
    assert successor.candidate is not None
    fence = await detector.seal_provider_candidate(token)
    assert fence is not None
    assert await detector.prepare_candidate_rejection(successor.candidate) is None
    await detector.close()


async def test_ordered_provider_fifo_overflow_stays_fail_open_after_drain() -> None:
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    for sequence_no in range(1, 10):
        await detector.observe_provider_audio_ordered(
            bytes([sequence_no, 0]) * 160,
            sample_rate_hz=16_000,
            identity=identity,
            sequence_no=sequence_no,
            split_before_audio=sequence_no > 1,
        )

    assert len(detector._provider_speaker_segments) == 8
    assert (
        detector.speaker_rejection_diagnostics_snapshot()[
            "provider_speaker_segment_overflow_fail_open_count"
        ]
        == 1
    )
    detector._expire_provider_segments(asyncio.get_running_loop().time() + 11.0)
    assert not detector._provider_speaker_segments
    await detector.observe_provider_audio_ordered(
        b"\x0a\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=10,
        split_before_audio=False,
    )
    successor = detector._provider_speaker_segments[0]
    assert successor.evidence_complete is False
    assert successor.candidate is not None
    fence = await detector.seal_provider_candidate(token)
    assert fence is not None
    assert await detector.prepare_candidate_rejection(successor.candidate) is None
    await detector.close()


@pytest.mark.parametrize("boundary", ["reset", "replace", "close"])
async def test_ordered_provider_lifecycle_finishes_deferred_without_activation(
    boundary: str,
) -> None:
    shadow = _DeferredSpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    _candidate, identity, _token = await _open_provider_candidate(
        detector,
        turn_id=1,
    )
    await detector.observe_provider_audio_ordered(
        b"\x71\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=1,
        split_before_audio=False,
    )
    await detector.observe_provider_audio_ordered(
        b"\x72\x00" * 160,
        sample_rate_hz=16_000,
        identity=identity,
        sequence_no=2,
        split_before_audio=True,
    )
    deferred = detector._provider_speaker_segments[1].candidate
    assert deferred is not None

    if boundary == "reset":
        await detector.reset()
    elif boundary == "replace":
        await detector.replace_speaker_verifier(
            _DeferredSpeakerShadowSpy(),
            owner_generation="owner-b",
        )
    else:
        await detector.close()

    assert ("activate", deferred) not in shadow.events
    assert ("finish", deferred) in shadow.events
    assert detector._provider_segment_expiry_task is None
    assert not detector._provider_segment_retired_expiry_tasks
    await detector.close()


async def test_speaker_shadow_reset_and_close_advance_generation_fail_open() -> None:
    shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    detector.observe_provider_audio(b"\x01\x00" * 160, sample_rate_hz=16_000)
    assert detector._speaker_shadow_candidate == SpeakerShadowCandidateKey(
        0,
        0,
        "provider_candidate",
    )

    shadow.reset_error = RuntimeError("observer reset failed")
    await detector.reset()

    assert detector._speaker_shadow_candidate is None
    assert detector._speaker_shadow_generation == 1
    assert shadow.reset_calls == 1

    detector.observe_provider_audio(b"\x02\x00" * 160, sample_rate_hz=16_000)
    assert detector._speaker_shadow_candidate == SpeakerShadowCandidateKey(
        1,
        1,
        "provider_candidate",
    )
    shadow.close_error = RuntimeError("observer close failed")
    await detector.close()

    assert detector._speaker_shadow_candidate is None
    assert detector._speaker_shadow_generation == 2
    assert shadow.close_calls == 1


async def test_replace_provider_verifier_waits_for_candidate_boundary() -> None:
    old_shadow = _BlockingCloseSpeakerShadow()
    new_shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=old_shadow,
    )
    first_pcm = b"\x01\x00" * 160
    tail_pcm = b"\x02\x00" * 160
    next_pcm = b"\x03\x00" * 160
    detector.observe_provider_audio(first_pcm, sample_rate_hz=16_000)
    old_candidate = SpeakerShadowCandidateKey(0, 0, "provider_candidate")
    assert old_shadow.frames == [(first_pcm, 16_000, old_candidate)]

    replacement = asyncio.create_task(
        detector.replace_speaker_verifier(
            new_shadow,
            owner_generation="owner-b",
        )
    )
    await asyncio.wait_for(old_shadow.close_started.wait(), 1.0)

    detector.observe_provider_audio(tail_pcm, sample_rate_hz=16_000)
    assert new_shadow.frames == []
    fence = await asyncio.wait_for(detector.seal_provider_candidate(), 1.0)
    assert fence is not None
    detector.observe_provider_audio(next_pcm, sample_rate_hz=16_000)
    assert new_shadow.frames == [
        (
            next_pcm,
            16_000,
            SpeakerShadowCandidateKey(0, 2, "provider_candidate"),
        )
    ]

    old_shadow.close_release.set()
    await replacement
    await detector.close()
    assert old_shadow.close_calls == 1
    assert new_shadow.close_calls == 1


async def test_replace_verifier_keeps_old_binding_owner_until_atomic_install() -> None:
    old_shadow = _SpeakerShadowSpy()
    new_shadow = _SpeakerShadowSpy()
    published: list[tuple[SpeakerShadowCandidateKey, VoiceTurnToken, str | None]] = []
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=old_shadow,
        speaker_owner_generation="owner-old",
        on_speaker_candidate_bound=lambda candidate, token, owner: published.append(
            (candidate, token, owner)
        ),
    )
    ingress = _ingress_token()
    result = await detector.feed(
        b"\x01\x00" * 160,
        ingress_token=ingress,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert result.candidate is not None
    detector.observe_provider_audio(b"\x02\x00" * 160, sample_rate_hz=16_000)
    old_candidate = detector._speaker_shadow_candidate
    assert old_candidate is not None

    await detector._lock.acquire()
    try:
        replacement = asyncio.create_task(
            detector.replace_speaker_verifier(
                new_shadow,
                owner_generation="owner-new",
            )
        )
        done, _pending = await asyncio.wait({replacement}, timeout=0.01)
        assert not done

        turn_token = VoiceTurnToken(ingress, turn_id=1)
        assert await detector.bind_candidate(result.candidate, turn_token) is not None
        assert published == [(old_candidate, turn_token, "owner-old")]
    finally:
        detector._lock.release()

    await replacement
    assert detector._speaker_owner_generation == "owner-new"
    await detector.close()


async def test_replace_verifier_cancel_before_lock_closes_handed_off_shadow() -> None:
    old_shadow = _SpeakerShadowSpy()
    new_shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=old_shadow,
        speaker_owner_generation="owner-old",
    )

    await detector._lock.acquire()
    try:
        replacement = asyncio.create_task(
            detector.replace_speaker_verifier(
                new_shadow,
                owner_generation="owner-new",
            )
        )
        done, _pending = await asyncio.wait({replacement}, timeout=0.01)
        assert not done
        replacement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement
    finally:
        detector._lock.release()

    assert detector._speaker_shadow is old_shadow
    assert detector._speaker_owner_generation == "owner-old"
    assert new_shadow.close_calls == 1
    await detector.close()
    assert old_shadow.close_calls == 1


async def test_replace_verifier_cancel_after_install_keeps_new_shadow() -> None:
    old_shadow = _BlockingCloseSpeakerShadow()
    new_shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=old_shadow,
        speaker_owner_generation="owner-old",
    )

    replacement = asyncio.create_task(
        detector.replace_speaker_verifier(
            new_shadow,
            owner_generation="owner-new",
        )
    )
    await asyncio.wait_for(old_shadow.close_started.wait(), 1.0)
    assert detector._speaker_shadow is new_shadow
    assert detector._speaker_owner_generation == "owner-new"

    replacement.cancel()
    done, _pending = await asyncio.wait({replacement}, timeout=0.01)
    assert not done
    old_shadow.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await replacement

    assert detector._speaker_shadow is new_shadow
    assert new_shadow.close_calls == 0
    assert old_shadow.close_calls == 1
    await detector.close()
    assert new_shadow.close_calls == 1


async def test_discard_provider_successor_reopens_replaced_verifier() -> None:
    old_shadow = _SpeakerShadowSpy()
    new_shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=old_shadow,
    )
    first_pcm = b"\x01\x00" * 160
    successor_pcm = b"\x02\x00" * 160
    next_pcm = b"\x03\x00" * 160
    detector.observe_provider_audio(first_pcm, sample_rate_hz=16_000)
    fence = await detector.seal_provider_candidate()
    assert fence is not None
    detector.observe_provider_audio(successor_pcm, sample_rate_hz=16_000)

    await detector.replace_speaker_verifier(
        new_shadow,
        owner_generation="owner-b",
    )
    assert await detector.discard_provider_successor(fence) is True
    detector.observe_provider_audio(next_pcm, sample_rate_hz=16_000)

    assert new_shadow.frames == [
        (
            next_pcm,
            16_000,
            SpeakerShadowCandidateKey(0, 3, "provider_candidate"),
        )
    ]
    await detector.close()


async def test_replace_smart_turn_verifier_activates_after_turn_boundary() -> None:
    new_shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
    )
    ingress = _ingress_token()
    identity = DetectorIngressIdentity(ingress, 0, 1)
    detector._ingress_token = ingress
    detector._sequence_no = 1
    detector._candidate_open = True

    await detector.replace_speaker_verifier(
        new_shadow,
        owner_generation="owner-b",
    )
    detector._observe_smart_turn_speaker_shadow(
        b"\x01\x00" * 160,
        16_000,
        identity,
    )
    assert new_shadow.frames == []

    detector._finish_smart_turn_speaker_shadow(identity)
    detector._observe_smart_turn_speaker_shadow(
        b"\x02\x00" * 160,
        16_000,
        identity,
    )
    assert new_shadow.frames == [
        (
            b"\x02\x00" * 160,
            16_000,
            SpeakerShadowCandidateKey(0, 2, "smart_turn_turn"),
        )
    ]
    await detector.close()


async def test_replace_verifier_close_failure_does_not_break_new_shadow() -> None:
    old_shadow = _SpeakerShadowSpy()
    old_shadow.close_error = RuntimeError("observer close failed")
    new_shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=old_shadow,
    )

    await detector.replace_speaker_verifier(
        new_shadow,
        owner_generation="owner-b",
    )
    detector.observe_provider_audio(b"\x01\x00" * 160, sample_rate_hz=16_000)

    assert old_shadow.close_calls == 1
    assert len(new_shadow.frames) == 1
    await detector.close()


async def test_replace_verifier_close_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from main_logic.asr_client.endpointing import detector_runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "_SPEAKER_SHADOW_REPLACEMENT_CLOSE_SECONDS",
        0.01,
    )
    old_shadow = _BlockingCloseSpeakerShadow()
    new_shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=old_shadow,
    )

    await asyncio.wait_for(
        detector.replace_speaker_verifier(
            new_shadow,
            owner_generation="owner-b",
        ),
        0.5,
    )

    assert old_shadow.close_started.is_set()
    assert old_shadow.close_calls == 1
    detector.observe_provider_audio(b"\x01\x00" * 160, sample_rate_hz=16_000)
    assert len(new_shadow.frames) == 1
    await detector.close()


async def test_prepare_candidate_rejection_maps_only_authoritative_shadow_key() -> None:
    (
        detector,
        _shadow,
        candidate,
        shadow_candidate,
        turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    private_mismatch = SpeakerShadowCandidateKey(
        shadow_candidate.detector_epoch,
        shadow_candidate.shadow_generation + 1,
        shadow_candidate.scope,
    )

    assert await detector.prepare_candidate_rejection(private_mismatch) is None
    assert (
        detector.speaker_rejection_diagnostics_snapshot()[
            "rejection_prepare_shadow_mismatch_count"
        ]
        == 1
    )
    lease = await detector.prepare_candidate_rejection(shadow_candidate)

    assert isinstance(lease, DetectorCandidateRejectionLease)
    assert lease.candidate == candidate
    assert lease.shadow_candidate == shadow_candidate
    assert lease.turn_token == turn_token
    assert lease._runtime is detector
    await detector.close()


async def test_prepare_candidate_rejection_counts_candidate_class_mismatch() -> None:
    (
        detector,
        _shadow,
        _candidate,
        _shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()

    assert await detector.prepare_candidate_rejection(object()) is None  # type: ignore[arg-type]
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["rejection_prepare_type_mismatch_count"] == 1
    assert diagnostics["rejection_prepare_shadow_mismatch_count"] == 0
    await detector.close()


async def test_prepare_candidate_rejection_explains_closed_candidate_without_seal() -> (
    None
):
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    detector._candidate_open = False

    assert await detector.prepare_candidate_rejection(shadow_candidate) is None
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["rejection_prepare_candidate_closed_count"] == 1
    assert diagnostics["rejection_prepare_closed_no_sealed_count"] == 1
    assert diagnostics["rejection_prepare_closed_fence_mismatch_count"] == 0
    assert diagnostics["rejection_prepare_closed_shadow_mismatch_count"] == 0
    await detector.close()


async def test_sealed_provider_rejection_consumes_only_exact_speaker_authority() -> (
    None
):
    (
        detector,
        _shadow,
        candidate,
        shadow_candidate,
        turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    open_lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert open_lease is not None
    assert open_lease.provider_fence is None

    fence = await detector.seal_provider_candidate()
    assert fence is not None
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["rejection_seal_snapshot_created_count"] == 1
    assert diagnostics["rejection_seal_snapshot_missing_shadow_count"] == 0
    assert diagnostics["rejection_seal_snapshot_invalid_shadow_count"] == 0
    assert diagnostics["rejection_seal_snapshot_unbound_count"] == 0
    sealed_lease = await detector.prepare_candidate_rejection(shadow_candidate)

    assert sealed_lease is not None
    assert sealed_lease.provider_fence == fence
    assert sealed_lease.candidate == candidate
    assert sealed_lease.turn_token == turn_token
    assert open_lease.commit() is False

    successor = await detector.feed(
        b"\x03\x00" * 160,
        ingress_token=_ingress_token(),
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert successor.candidate == DetectorCandidateKey(0, 1)
    generation_before = detector._candidate_generation
    candidate_open_before = detector.candidate_open

    assert sealed_lease.commit() is True
    assert sealed_lease.commit() is False
    assert detector._provider_candidate_fence == fence
    assert detector._candidate_generation == generation_before
    assert detector.candidate_open is candidate_open_before
    assert detector._bound_turns[candidate].turn_token == turn_token
    assert await detector.complete_provider_candidate(fence) is True
    assert (
        detector.speaker_rejection_diagnostics_snapshot()[
            "rejection_complete_cleared_snapshot_count"
        ]
        == 0
    )
    await detector.close()


async def test_sealed_provider_candidate_exposes_only_pending_speaker_decision() -> (
    None
):
    (
        detector,
        shadow,
        _candidate,
        shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    shadow.provisional_pending = True

    fence = await detector.seal_provider_candidate()

    assert fence is not None
    assert detector.pending_provider_speaker_candidate(fence) == shadow_candidate
    shadow.provisional_pending = False
    assert detector.pending_provider_speaker_candidate(fence) is None
    wrong_fence = ProviderCandidateFence(
        fence.detector_epoch,
        fence.candidate_generation + 1,
        fence.through_sequence_no,
    )
    assert detector.pending_provider_speaker_candidate(wrong_fence) is None
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["rejection_provisional_query_count"] == 3
    assert diagnostics["rejection_provisional_pending_count"] == 1
    assert diagnostics["rejection_provisional_stale_count"] == 2
    await detector.close()


async def test_provider_final_counts_unconsumed_sealed_rejection_snapshot() -> None:
    (
        detector,
        _shadow,
        _candidate,
        _shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    fence = await detector.seal_provider_candidate()
    assert fence is not None

    assert await detector.complete_provider_candidate(fence) is False
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["rejection_seal_snapshot_created_count"] == 1
    assert diagnostics["rejection_complete_cleared_snapshot_count"] == 1
    await detector.close()


async def test_sealed_provider_rejection_rejects_wrong_shadow_fence_and_epoch() -> None:
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    fence = await detector.seal_provider_candidate()
    assert fence is not None
    sealed_lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert sealed_lease is not None

    wrong_shadow = SpeakerShadowCandidateKey(
        shadow_candidate.detector_epoch,
        shadow_candidate.shadow_generation + 1,
        shadow_candidate.scope,
    )
    wrong_epoch = SpeakerShadowCandidateKey(
        shadow_candidate.detector_epoch + 1,
        shadow_candidate.shadow_generation,
        shadow_candidate.scope,
    )
    wrong_fence_lease = DetectorCandidateRejectionLease(
        candidate=sealed_lease.candidate,
        shadow_candidate=sealed_lease.shadow_candidate,
        turn_token=sealed_lease.turn_token,
        _runtime=detector,
        provider_fence=ProviderCandidateFence(
            fence.detector_epoch,
            fence.candidate_generation + 1,
            fence.through_sequence_no,
        ),
    )

    assert await detector.prepare_candidate_rejection(wrong_shadow) is None
    assert await detector.prepare_candidate_rejection(wrong_epoch) is None
    assert wrong_fence_lease.commit() is False
    assert sealed_lease.commit() is True
    await detector.close()


@pytest.mark.parametrize(
    "advance",
    ["provider_final", "detector_reset", "verifier_replacement", "detector_close"],
)
async def test_sealed_provider_rejection_is_invalidated_by_lifecycle_boundary(
    advance: str,
) -> None:
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    fence = await detector.seal_provider_candidate()
    assert fence is not None
    lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert lease is not None
    assert lease.provider_fence == fence

    if advance == "provider_final":
        assert await detector.complete_provider_candidate(fence) is False
    elif advance == "detector_reset":
        await detector.reset()
    elif advance == "verifier_replacement":
        await detector.replace_speaker_verifier(
            _SpeakerShadowSpy(),
            owner_generation="owner-b",
        )
    else:
        await detector.close()

    assert lease.commit() is False
    await detector.close()


async def test_smart_turn_rejection_lease_keeps_open_candidate_semantics() -> None:
    shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        speaker_shadow=shadow,
        on_turn_complete=AsyncMock(),
    )
    ingress = _ingress_token()
    candidate = DetectorCandidateKey(0, 0)
    turn_token = VoiceTurnToken(ingress, turn_id=1)
    detector._ingress_token = ingress
    detector._candidate_open = True
    assert await detector.bind_candidate(candidate, turn_token) is not None
    shadow_candidate = detector._open_speaker_shadow_candidate("smart_turn_turn")
    assert shadow_candidate is not None
    lease = await detector.prepare_candidate_rejection(shadow_candidate)

    assert lease is not None
    assert lease.provider_fence is None
    assert lease.commit() is True
    assert detector.candidate_open is False
    assert detector._candidate_generation == 1
    assert candidate not in detector._bound_turns
    assert shadow.finished == [shadow_candidate]
    await detector.close()


async def test_candidate_rejection_commit_fails_open_while_detector_lock_is_busy() -> (
    None
):
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert lease is not None

    await detector._lock.acquire()
    try:
        assert lease.commit() is False
    finally:
        detector._lock.release()

    assert lease.commit() is True
    await detector.close()


async def test_candidate_rejection_async_commit_waits_for_short_lock_contention() -> (
    None
):
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert lease is not None

    await detector._lock.acquire()
    commit_task = asyncio.create_task(
        lease.commit_async(deadline=asyncio.get_running_loop().time() + 1.0)
    )
    detector._lock.release()
    assert await commit_task is DetectorCandidateRejectionCommitResult.ACTIVE_APPLIED
    await detector.close()


async def test_candidate_rejection_async_commit_deadline_retires_late_authority() -> (
    None
):
    (
        detector,
        _shadow,
        _candidate,
        shadow_candidate,
        _turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert lease is not None

    await detector._lock.acquire()
    try:
        result = await lease.commit_async(
            deadline=asyncio.get_running_loop().time() + 0.01
        )
    finally:
        detector._lock.release()
    assert result is DetectorCandidateRejectionCommitResult.STALE

    await detector.reset()
    assert (
        await lease.commit_async(deadline=asyncio.get_running_loop().time() + 0.1)
        is DetectorCandidateRejectionCommitResult.STALE
    )
    await detector.close()


async def test_candidate_rejection_commit_revokes_old_candidate_authority() -> None:
    (
        detector,
        shadow,
        candidate,
        shadow_candidate,
        turn_token,
    ) = await _prepare_candidate_rejection_fixture()
    lease = await detector.prepare_candidate_rejection(shadow_candidate)
    assert lease is not None
    generation_before = detector._candidate_generation

    assert lease.commit() is True

    assert detector._candidate_generation == generation_before + 1
    assert detector.candidate_open is False
    assert candidate not in detector._bound_turns
    assert shadow.finished == [shadow_candidate]
    assert await detector.bind_candidate(candidate, turn_token) is None
    assert await detector.prepare_candidate_rejection(shadow_candidate) is None
    assert lease.commit() is False
    await detector.close()


async def test_speaker_shadow_submit_finish_and_enabled_failures_are_ignored() -> None:
    shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )

    shadow.enabled_error = RuntimeError("observer enabled failed")
    detector.observe_provider_audio(b"\x01\x00" * 160, sample_rate_hz=16_000)
    assert detector._speaker_shadow_candidate is None

    shadow.enabled_error = None
    shadow.submit_error = RuntimeError("observer submit failed")
    detector.observe_provider_audio(b"\x02\x00" * 160, sample_rate_hz=16_000)
    assert detector._speaker_shadow_candidate is not None
    assert shadow.frames == []

    shadow.finish_error = RuntimeError("observer finish failed")
    fence = await detector.seal_provider_candidate()

    assert fence is not None
    assert detector._speaker_shadow_candidate is None
    assert detector._speaker_shadow_generation == 1
    await detector.close()


async def test_semantic_failure_invalidates_speaker_shadow_fail_open() -> None:
    shadow = _SpeakerShadowSpy()
    shadow.reset_error = RuntimeError("observer reset failed")
    on_failure = AsyncMock()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        speaker_shadow=shadow,
        on_turn_complete=AsyncMock(),
        on_endpointing_failure=on_failure,
    )
    assert detector._open_speaker_shadow_candidate("smart_turn_turn") is not None
    adapter = detector._semantic_adapter
    assert adapter is not None
    adapter.wait_failure = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(stage="smart_turn")
    )

    await detector._watch_semantic_failure(adapter)

    assert detector.detector_epoch == 1
    assert detector._speaker_shadow_candidate is None
    assert detector._speaker_shadow_generation == 1
    assert shadow.reset_calls == 1
    assert detector.smart_turn_readiness is SmartTurnReadiness.FAILED
    on_failure.assert_awaited_once()
    await detector.close()


async def test_semantic_failure_wait_cannot_invalidate_reset_generation() -> None:
    on_failure = AsyncMock()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
        on_endpointing_failure=on_failure,
    )
    adapter = detector._semantic_adapter
    assert adapter is not None
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_failure() -> SimpleNamespace:
        started.set()
        await release.wait()
        return SimpleNamespace(stage="smart_turn")

    adapter.wait_failure = wait_failure  # type: ignore[method-assign]
    watch = asyncio.create_task(detector._watch_semantic_failure(adapter))
    await started.wait()
    await detector.reset()
    reset_epoch = detector.detector_epoch
    release.set()
    await watch

    assert detector.detector_epoch == reset_epoch
    on_failure.assert_not_awaited()
    await detector.close()


async def test_provider_prewarm_keeps_candidate_open_until_silero_confirms() -> None:
    gate = _Gate()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
    )
    ingress = _ingress_token()

    prewarm = await detector.feed(
        b"\x01\x00",
        rnnoise_evidence=RnnoiseEvidence.from_legacy_probability(
            0.9,
            available=True,
        ),
        ingress_token=ingress,
    )
    followup = await detector.feed(
        b"\x02\x00",
        rnnoise_evidence=RnnoiseEvidence.from_legacy_probability(
            0.05,
            available=True,
        ),
        ingress_token=ingress,
    )

    assert prewarm.throttle_action is ThrottleAction.PREWARM
    assert followup.throttle_action is ThrottleAction.KEEP_CANDIDATE_OPEN
    assert gate.inputs == [b"\x01\x00", b"\x02\x00"]
    assert detector.candidate_open is True
    await detector.close()


async def test_stale_provider_ingress_does_not_mutate_throttle_policy() -> None:
    policy = VoiceThrottlePolicy(
        resource_optimization_enabled=True,
        minimum_baseline_samples=1,
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_provider_endpoint_policy(),
        throttle_policy=policy,
    )
    current_ingress = _ingress_token()
    await detector.feed(
        b"\x01\x00",
        rnnoise_evidence=RnnoiseEvidence.unavailable(),
        ingress_token=current_ingress,
    )
    before = policy.shadow_metrics

    stale = await detector.feed(
        b"\x02\x00",
        rnnoise_evidence=RnnoiseEvidence.from_legacy_probability(
            0.05,
            available=True,
        ),
        ingress_token=VoiceIngressToken(1, "stale-socket", 1, 1, 1),
        allow_baseline_update=True,
    )

    assert stale.endpointing_available is False
    assert policy.shadow_metrics == before
    assert policy.baseline is None
    await detector.close()


async def test_completion_fence_replays_pcm_consumed_during_inference() -> None:
    coordinator = _BlockingSemanticCoordinator()
    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    completed = asyncio.Event()
    shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        speaker_shadow=shadow,
        on_turn_complete=lambda: completed.set() or asyncio.sleep(0),
    )
    first_pcm = b"\x01\x00" * 160

    await detector.submit_audio(
        first_pcm,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    await asyncio.wait_for(coordinator.evaluate_started.wait(), 1)
    predecessor = SpeakerShadowCandidateKey(0, 0, "smart_turn_turn")
    assert shadow.frames == [(first_pcm, 16_000, predecessor)]
    gate.events = ()
    successor_pcm = b"\x02\x00" * 160
    successor = await detector.submit_audio(
        successor_pcm,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    async with asyncio.timeout(1):
        while gate.inputs.count(successor_pcm) < 1:
            await asyncio.sleep(0)
    assert shadow.frames == [(first_pcm, 16_000, predecessor)]

    coordinator.evaluate_release.set()
    await asyncio.wait_for(completed.wait(), 1)
    async with asyncio.timeout(1):
        while gate.inputs.count(successor_pcm) < 2:
            await asyncio.sleep(0)

    assert successor.candidate is not None
    assert successor.candidate.candidate_generation == 0
    assert gate.inputs.count(successor_pcm) == 2
    assert coordinator.audio.count(successor_pcm) == 2
    successor_candidate = SpeakerShadowCandidateKey(0, 1, "smart_turn_turn")
    assert shadow.frames == [
        (first_pcm, 16_000, predecessor),
        (successor_pcm, 16_000, successor_candidate),
    ]
    assert shadow.frames[1][0] is successor_pcm
    assert shadow.finished == [predecessor]
    assert shadow.events == [
        ("submit", predecessor),
        ("finish", predecessor),
        ("submit", successor_candidate),
    ]
    await detector.close()


@pytest.mark.parametrize(
    ("status", "decision", "tail_events"),
    [
        (EvaluationStatus.STALE, None, ()),
        (EvaluationStatus.OK, TurnDecision.INCOMPLETE, ()),
        (
            EvaluationStatus.OK,
            TurnDecision.INCOMPLETE,
            (SpeechActivityEvent.SPEECH_RESUMED,),
        ),
    ],
)
async def test_speaker_shadow_keeps_stale_or_incomplete_tail_on_predecessor(
    status: EvaluationStatus,
    decision: TurnDecision | None,
    tail_events: tuple[SpeechActivityEvent, ...],
) -> None:
    coordinator = _BlockingResultCoordinator(status=status, decision=decision)
    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        speaker_shadow=shadow,
        on_turn_complete=AsyncMock(),
    )
    first_pcm = b"\x01\x00" * 160
    tail_pcm = b"\x02\x00" * 160

    await detector.submit_audio(
        first_pcm,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    await asyncio.wait_for(coordinator.evaluate_started.wait(), 1)
    gate.events = tail_events
    await detector.submit_audio(
        tail_pcm,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    async with asyncio.timeout(1):
        while tail_pcm not in gate.inputs:
            await asyncio.sleep(0)

    candidate = SpeakerShadowCandidateKey(0, 0, "smart_turn_turn")
    assert shadow.frames == [(first_pcm, 16_000, candidate)]

    coordinator.evaluate_release.set()
    adapter = detector._semantic_adapter
    assert adapter is not None
    await adapter.wait_idle()

    assert shadow.frames == [
        (first_pcm, 16_000, candidate),
        (tail_pcm, 16_000, candidate),
    ]
    assert shadow.finished == []
    await detector.close()


async def test_strict_retry_complete_commits_after_confirmation_delay() -> None:
    coordinator = _SequencedSemanticCoordinator(
        [
            (TurnDecision.INCOMPLETE, 0.2),
            (TurnDecision.COMPLETE, 0.8),
        ]
    )
    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    committed = asyncio.Event()
    commits: list[tuple[int, int, int]] = []

    async def on_commit(
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        commits.append((generation, buffer_epoch, utterance_id))
        committed.set()

    adapter = _VoiceTurnAdapter(
        vad=_Vad(),
        gate=gate,
        coordinator=coordinator,
        on_commit=on_commit,
        continuation_timeout_seconds=0.01,
        strict_complete_confirmation_seconds=0.2,
        smart_turn_required=True,
    )

    await adapter.start()
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x01\x00" * 160,
    )
    await adapter.wait_idle()
    assert commits == []

    await asyncio.sleep(0.02)
    await adapter.wait_idle()
    assert commits == []

    await asyncio.wait_for(committed.wait(), 1)
    assert commits == [(1, 0, 1)]
    await adapter.close()


async def test_strict_retry_complete_is_cancelled_by_continuation() -> None:
    coordinator = _SequencedSemanticCoordinator(
        [
            (TurnDecision.INCOMPLETE, 0.2),
            (TurnDecision.COMPLETE, 0.8),
            (TurnDecision.COMPLETE, 0.9),
        ]
    )
    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    committed = asyncio.Event()
    commits: list[tuple[int, int, int]] = []

    async def on_commit(
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        commits.append((generation, buffer_epoch, utterance_id))
        committed.set()

    adapter = _VoiceTurnAdapter(
        vad=_Vad(),
        gate=gate,
        coordinator=coordinator,
        on_commit=on_commit,
        continuation_timeout_seconds=0.01,
        strict_complete_confirmation_seconds=0.1,
        smart_turn_required=True,
    )

    await adapter.start()
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x01\x00" * 160,
    )
    await adapter.wait_idle()
    await asyncio.sleep(0.02)
    await adapter.wait_idle()
    assert coordinator.evaluation_count == 2
    assert commits == []

    gate.events = (SpeechActivityEvent.SPEECH_RESUMED,)
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x02\x00" * 160,
    )
    await adapter.wait_idle()
    await asyncio.sleep(0.12)
    assert commits == []

    gate.events = (SpeechActivityEvent.CANDIDATE_PAUSE,)
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x03\x00" * 160,
    )
    await adapter.wait_idle()

    await asyncio.wait_for(committed.wait(), 1)
    assert commits == [(1, 0, 1)]
    await adapter.close()


async def test_candidate_pause_complete_commits_after_confirmation_delay() -> None:
    coordinator = _SequencedSemanticCoordinator(
        [
            (TurnDecision.COMPLETE, 0.97),
        ]
    )
    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    committed = asyncio.Event()
    commits: list[tuple[int, int, int]] = []

    async def on_commit(
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        commits.append((generation, buffer_epoch, utterance_id))
        committed.set()

    adapter = _VoiceTurnAdapter(
        vad=_Vad(),
        gate=gate,
        coordinator=coordinator,
        on_commit=on_commit,
        candidate_complete_confirmation_seconds=0.2,
        smart_turn_required=True,
    )

    await adapter.start()
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x01\x00" * 160,
    )
    await adapter.wait_idle()
    assert commits == []

    await asyncio.sleep(0.02)
    await adapter.wait_idle()
    assert commits == []

    await asyncio.wait_for(committed.wait(), 1)
    assert commits == [(1, 0, 1)]
    await adapter.close()


async def test_candidate_pause_complete_is_cancelled_by_continuation() -> None:
    coordinator = _SequencedSemanticCoordinator(
        [
            (TurnDecision.COMPLETE, 0.97),
            (TurnDecision.COMPLETE, 0.96),
        ]
    )
    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    committed = asyncio.Event()
    commits: list[tuple[int, int, int]] = []

    async def on_commit(
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        commits.append((generation, buffer_epoch, utterance_id))
        committed.set()

    adapter = _VoiceTurnAdapter(
        vad=_Vad(),
        gate=gate,
        coordinator=coordinator,
        on_commit=on_commit,
        candidate_complete_confirmation_seconds=0.1,
        smart_turn_required=True,
    )

    await adapter.start()
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x01\x00" * 160,
    )
    await adapter.wait_idle()
    assert coordinator.evaluation_count == 1
    assert commits == []

    gate.events = (SpeechActivityEvent.SPEECH_RESUMED,)
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x02\x00" * 160,
    )
    await adapter.wait_idle()
    await asyncio.sleep(0.12)
    assert commits == []

    gate.events = (SpeechActivityEvent.CANDIDATE_PAUSE,)
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x03\x00" * 160,
    )
    await adapter.wait_idle()

    await asyncio.wait_for(committed.wait(), 1)
    assert commits == [(1, 0, 1)]
    await adapter.close()


async def test_candidate_pause_pending_complete_publishes_on_close() -> None:
    coordinator = _SequencedSemanticCoordinator(
        [
            (TurnDecision.COMPLETE, 0.97),
        ]
    )
    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    commits: list[tuple[int, int, int]] = []

    async def on_commit(
        generation: int,
        buffer_epoch: int,
        utterance_id: int,
    ) -> None:
        commits.append((generation, buffer_epoch, utterance_id))

    adapter = _VoiceTurnAdapter(
        vad=_Vad(),
        gate=gate,
        coordinator=coordinator,
        on_commit=on_commit,
        candidate_complete_confirmation_seconds=10.0,
        smart_turn_required=True,
    )

    await adapter.start()
    await adapter.push_audio(
        generation=1,
        buffer_epoch=0,
        utterance_id=1,
        pcm16=b"\x01\x00" * 160,
    )
    await adapter.wait_idle()
    assert commits == []

    coordinator.state = CoordinatorState.IDLE
    await adapter.close()

    assert commits == [(1, 0, 1)]


async def test_successor_fence_uses_active_identity_for_queued_pause() -> None:
    coordinator = _BlockingSemanticCoordinator()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate((SpeechActivityEvent.CANDIDATE_PAUSE,)),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    adapter = detector._semantic_adapter
    assert isinstance(adapter, _VoiceTurnAdapter)
    await adapter.start()
    predecessor_identity = (0, 0, 1)
    active_identity = (1, 0, 2)
    adapter._identity = active_identity
    adapter._successor_audio_fence = (
        predecessor_identity,
        1,
        active_identity,
    )

    await adapter._process_audio(
        _AudioItem(
            identity=predecessor_identity,
            pcm16=b"\x01\x00" * 160,
            duration_us=10_000,
            detector_identity=DetectorIngressIdentity(
                _ingress_token(),
                detector_epoch=0,
                sequence_no=2,
            ),
        )
    )

    await asyncio.wait_for(coordinator.evaluate_started.wait(), 1)
    assert adapter._evaluation_task is not None
    coordinator.evaluate_release.set()
    await detector.close()


async def test_completed_turn_does_not_clear_successor_candidate_activity() -> None:
    completion_started = asyncio.Event()
    completion_release = asyncio.Event()
    completion_published = asyncio.Event()
    events: list[object] = []
    detector: DetectorRuntime

    async def on_event(event: object) -> None:
        events.append(event)
        if isinstance(event, DetectorPrewarmEvent):
            await detector.bind_candidate(
                event.candidate,
                VoiceTurnToken(event.ingress.ingress_token, 1),
            )
        elif isinstance(event, DetectorTurnEvent):
            completion_started.set()
            await completion_release.wait()

    async def on_complete() -> None:
        completion_published.set()

    gate = _Gate((SpeechActivityEvent.CANDIDATE_PAUSE,))
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_event=on_event,
        on_turn_complete=on_complete,
    )

    first = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    await asyncio.wait_for(completion_started.wait(), 1)
    gate.events = ()
    successor = await detector.submit_audio(
        b"\x02\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    completion_release.set()
    await asyncio.wait_for(completion_published.wait(), 1)
    followup = await detector.submit_audio(
        b"\x03\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.0,
        rnnoise_available=True,
    )

    completed = [event for event in events if isinstance(event, DetectorTurnEvent)]
    assert first.candidate is not None
    assert successor.candidate is not None
    assert successor.candidate.candidate_generation == (
        first.candidate.candidate_generation + 1
    )
    assert followup.status is DetectorSubmitStatus.ACCEPTED
    assert detector.candidate_open is True
    assert len(completed) == 1
    assert completed[0].bound_turn.candidate == first.candidate
    await detector.close()


async def test_completion_fence_clears_activity_without_successor_pcm() -> None:
    completed = asyncio.Event()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate((SpeechActivityEvent.CANDIDATE_PAUSE,)),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=lambda: completed.set() or asyncio.sleep(0),
    )
    await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    await asyncio.wait_for(completed.wait(), 1)

    quiet = await detector.submit_audio(
        b"\x02\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.0,
        rnnoise_available=True,
    )

    assert quiet.status is DetectorSubmitStatus.SKIPPED_QUIET
    assert detector.candidate_open is False
    await detector.close()


async def test_completed_candidate_bindings_stay_bounded() -> None:
    completed: list[DetectorTurnEvent] = []
    detector: DetectorRuntime
    next_turn_id = 0

    async def on_event(event: object) -> None:
        nonlocal next_turn_id
        if (
            isinstance(event, DetectorActivityEvent)
            and event.activity is SpeechActivityEvent.SPEECH_STARTED
        ):
            next_turn_id += 1
            bound = await detector.bind_candidate(
                event.candidate,
                VoiceTurnToken(_ingress_token(), turn_id=next_turn_id),
            )
            assert bound is not None
        elif isinstance(event, DetectorTurnEvent):
            completed.append(event)

    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(
            (
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.CANDIDATE_PAUSE,
            )
        ),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_event=on_event,
    )

    for turn_index in range(100):
        await detector.submit_audio(
            bytes((turn_index + 1, 0)) * 160,
            ingress_token=_ingress_token(),
            sample_rate_hz=16_000,
            speech_probability=0.9,
            rnnoise_available=True,
        )
        async with asyncio.timeout(1):
            while len(completed) <= turn_index:
                await asyncio.sleep(0)
        assert detector._bound_turns == {}
        assert detector._deferred_completions == {}
        await detector.release_deferred_turn()

    assert len(completed) == 100
    await detector.close()


async def test_provider_candidate_fence_preserves_post_discard_successor() -> None:
    gate = _Gate((SpeechActivityEvent.SPEECH_STARTED,))
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
    )
    evidence = RnnoiseEvidence.from_legacy_probability(0.9, available=True)
    predecessor = await detector.feed(
        b"\x01\x00",
        rnnoise_evidence=evidence,
        ingress_token=_ingress_token(),
    )
    assert predecessor.candidate == DetectorCandidateKey(0, 0)
    fence = await detector.seal_provider_candidate()
    assert isinstance(fence, ProviderCandidateFence)

    successor = await detector.feed(
        b"\x02\x00",
        rnnoise_evidence=evidence,
        ingress_token=_ingress_token(),
    )
    assert successor.candidate == DetectorCandidateKey(0, 1)
    assert await detector.discard_provider_successor(fence) is True
    post_discard = await detector.feed(
        b"\x03\x00",
        rnnoise_evidence=evidence,
        ingress_token=_ingress_token(),
    )
    assert post_discard.candidate == DetectorCandidateKey(0, 2)

    assert await detector.complete_provider_candidate(fence) is True
    assert await detector.complete_provider_candidate(fence) is None
    assert detector._speech_active is True
    await detector.close()


async def test_provider_fence_preserves_quiet_pcm_then_closes_candidate() -> None:
    gate = _Gate()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
    )
    ingress = _ingress_token()
    await detector.feed(
        b"\x01\x00",
        rnnoise_evidence=RnnoiseEvidence.from_legacy_probability(
            0.9,
            available=True,
        ),
        ingress_token=ingress,
    )
    fence = await detector.seal_provider_candidate()
    assert fence is not None

    successor = await detector.feed(
        b"\x02\x00",
        rnnoise_evidence=RnnoiseEvidence.from_legacy_probability(
            0.05,
            available=True,
        ),
        ingress_token=ingress,
    )

    assert successor.throttle_action is ThrottleAction.KEEP_CANDIDATE_OPEN
    assert gate.inputs == [b"\x01\x00", b"\x02\x00"]
    assert await detector.complete_provider_candidate(fence) is True
    assert detector.candidate_open is False
    quiet = await detector.feed(
        b"\x03\x00",
        rnnoise_evidence=RnnoiseEvidence.from_legacy_probability(
            0.05,
            available=True,
        ),
        ingress_token=ingress,
        allow_baseline_update=True,
    )
    assert quiet.throttle_action is ThrottleAction.SKIP_IDLE_PCM
    assert gate.inputs == [b"\x01\x00", b"\x02\x00"]
    await detector.close()


async def test_provider_candidate_completion_restores_idle_throttle() -> None:
    policy = VoiceThrottlePolicy(
        resource_optimization_enabled=True,
        minimum_baseline_samples=1,
    )
    gate = _Gate((SpeechActivityEvent.SPEECH_STARTED,))
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
        throttle_policy=policy,
    )
    await detector.feed(
        b"\x01\x00",
        rnnoise_evidence=RnnoiseEvidence.from_legacy_probability(
            0.9,
            available=True,
        ),
        ingress_token=_ingress_token(),
    )
    fence = await detector.seal_provider_candidate()
    assert fence is not None
    assert await detector.complete_provider_candidate(fence) is False
    gate.events = ()
    before = len(gate.inputs)

    quiet = await detector.feed(
        b"\x02\x00",
        rnnoise_evidence=RnnoiseEvidence.from_legacy_probability(
            0.05,
            available=True,
        ),
        ingress_token=_ingress_token(),
        allow_baseline_update=True,
    )

    assert quiet.throttle_action is ThrottleAction.SKIP_IDLE_PCM
    assert len(gate.inputs) == before
    await detector.close()


async def test_detector_loads_silero_off_loop_and_returns_activity() -> None:
    vad = _Vad()
    gate = _Gate((SpeechActivityEvent.SPEECH_STARTED,))
    detector = DetectorRuntime(vad=vad, gate=gate)

    result = await detector.feed(b"\x01\x00" * 160)

    assert result == DetectorFeedResult(
        events=(SpeechActivityEvent.SPEECH_STARTED,),
        throttle_available=True,
        throttle_action=ThrottleAction.OPEN_CANDIDATE,
        identity=DetectorIngressIdentity(
            ingress_token=VoiceIngressToken(
                session_epoch=0,
                connection_id="detector-feed-compat",
                lease_generation=0,
                route_generation=0,
                audio_generation=0,
            ),
            detector_epoch=0,
            sequence_no=1,
        ),
        candidate=DetectorCandidateKey(0, 0),
    )
    assert gate.inputs == [b"\x01\x00" * 160]
    assert vad.load_threads and vad.load_threads[0] != threading.get_ident()
    await detector.close()
    assert vad.closed is True


async def test_detector_failure_requests_independent_asr_fail_open() -> None:
    detector = DetectorRuntime(vad=_Vad(available=False), gate=_Gate())

    first = await detector.feed(b"\x01\x00")
    second = await detector.feed(b"\x02\x00")

    assert first.throttle_available is False
    assert second.throttle_available is False
    assert first.events == second.events == ()
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["detector_vad_load_unavailable_count"] == 1
    assert diagnostics["detector_vad_load_exception_count"] == 0
    assert diagnostics["detector_feed_unavailable_count"] == 1


async def test_rnnoise_soft_gate_skips_silero_until_probable_voice() -> None:
    gate = _Gate((SpeechActivityEvent.SPEECH_STARTED,))
    detector = DetectorRuntime(vad=_Vad(), gate=gate, rnnoise_onset_probability=0.4)

    quiet = await detector.feed(b"\x01\x00", speech_probability=0.1)
    speech = await detector.feed(b"\x02\x00", speech_probability=0.8)

    assert quiet.events == ()
    assert quiet.throttle_available is True
    assert quiet.identity is None
    assert quiet.candidate is None
    assert gate.inputs == [b"\x02\x00"]
    assert speech.events == (SpeechActivityEvent.SPEECH_STARTED,)
    assert speech.identity is not None
    assert speech.candidate == DetectorCandidateKey(0, 0)


async def test_disabled_resource_optimization_never_skips_quiet_silero_pcm() -> None:
    gate = _Gate((SpeechActivityEvent.SPEECH_STARTED,))
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        rnnoise_onset_probability=0.4,
        resource_optimization_enabled=False,
    )

    result = await detector.feed(
        b"\x01\x00",
        speech_probability=0.1,
        rnnoise_available=True,
    )

    assert gate.inputs == [b"\x01\x00"]
    assert result.events == (SpeechActivityEvent.SPEECH_STARTED,)


async def test_rnnoise_unavailable_does_not_look_like_zero_probability() -> None:
    gate = _Gate((SpeechActivityEvent.SPEECH_STARTED,))
    detector = DetectorRuntime(vad=_Vad(), gate=gate, rnnoise_onset_probability=0.4)

    result = await detector.feed(
        b"\x01\x00",
        speech_probability=0.0,
        rnnoise_available=False,
    )

    assert gate.inputs == [b"\x01\x00"]
    assert result.events == (SpeechActivityEvent.SPEECH_STARTED,)


async def test_active_speech_still_feeds_silero_when_rnnoise_probability_drops() -> (
    None
):
    gate = _Gate((SpeechActivityEvent.SPEECH_STARTED,))
    detector = DetectorRuntime(vad=_Vad(), gate=gate)

    await detector.feed(b"\x01\x00", speech_probability=0.8)
    await detector.feed(b"\x02\x00", speech_probability=0.0)

    assert gate.inputs == [b"\x01\x00", b"\x02\x00"]


async def test_unified_detector_owns_smart_turn_and_emits_semantic_completion() -> None:
    completed = asyncio.Event()
    completion_count = 0

    async def on_complete() -> None:
        nonlocal completion_count
        completion_count += 1
        completed.set()

    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate((SpeechActivityEvent.CANDIDATE_PAUSE,)),
        provider_policy=AsrProviderPolicy(
            transport="segmented",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=27_000,
            warm_transport_ms=0,
            replay_policy="none",
        ),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=on_complete,
    )

    result = await detector.feed(b"\x01\x00" * 160)
    await asyncio.wait_for(completed.wait(), 1)

    assert result.events == (SpeechActivityEvent.CANDIDATE_PAUSE,)
    assert result.throttle_available is True

    await detector.feed(b"\x02\x00" * 160)
    assert completion_count == 1
    await detector.release_deferred_turn()
    assert completion_count == 2
    await detector.close()


async def test_submit_audio_does_not_wait_for_smart_turn_inference() -> None:
    coordinator = _BlockingSemanticCoordinator()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate((SpeechActivityEvent.CANDIDATE_PAUSE,)),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )

    first = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    await asyncio.wait_for(coordinator.evaluate_started.wait(), 1)
    second = await asyncio.wait_for(
        detector.submit_audio(
            b"\x02\x00" * 160,
            ingress_token=_ingress_token(),
            sample_rate_hz=16_000,
            speech_probability=0.1,
            rnnoise_available=True,
        ),
        0.1,
    )

    assert first.status is DetectorSubmitStatus.ACCEPTED
    assert second.status is DetectorSubmitStatus.ACCEPTED
    assert detector.candidate_open is True
    coordinator.evaluate_release.set()
    await detector.close()


async def test_candidate_open_prevents_rnnoise_from_skipping_followup_pcm() -> None:
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
    )

    quiet = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.1,
        rnnoise_available=True,
    )
    onset = await detector.submit_audio(
        b"\x02\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    followup = await detector.submit_audio(
        b"\x03\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.0,
        rnnoise_available=True,
    )

    assert quiet.status is DetectorSubmitStatus.SKIPPED_QUIET
    assert onset.status is DetectorSubmitStatus.ACCEPTED
    assert followup.status is DetectorSubmitStatus.ACCEPTED
    await detector.close()


async def test_smart_turn_activity_updates_throttle_shadow_metrics() -> None:
    policy = VoiceThrottlePolicy(resource_optimization_enabled=True)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate((SpeechActivityEvent.SPEECH_STARTED,)),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
        throttle_policy=policy,
    )

    result = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    adapter = detector._semantic_adapter
    assert adapter is not None
    await adapter.wait_idle()
    second_result = await detector.submit_audio(
        b"\x02\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    await adapter.wait_idle()

    assert result.status is DetectorSubmitStatus.ACCEPTED
    assert second_result.status is DetectorSubmitStatus.ACCEPTED
    assert detector.throttle_shadow_metrics.rnnoise_trigger_count == 2
    assert detector.throttle_shadow_metrics.silero_trigger_count == 1
    assert detector.throttle_shadow_metrics.rnnoise_silero_disagreement_count == 1
    await detector.close()


async def test_disabled_resource_optimization_never_skips_quiet_smart_turn_pcm() -> (
    None
):
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
        resource_optimization_enabled=False,
    )

    result = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.1,
        rnnoise_available=True,
    )

    assert result.status is DetectorSubmitStatus.ACCEPTED
    await detector.close()


async def test_smart_turn_loading_does_not_hold_detector_audio_submission() -> None:
    coordinator = _BlockingSemanticCoordinator(block_prepare=True)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    turn_token = VoiceTurnToken(_ingress_token(), turn_id=1)
    prepare_task = asyncio.create_task(detector.prepare_endpointing(turn_token))
    await asyncio.wait_for(coordinator.prepare_started.wait(), 1)

    submitted = await asyncio.wait_for(
        detector.submit_audio(
            b"\x01\x00" * 160,
            ingress_token=_ingress_token(),
            sample_rate_hz=16_000,
            speech_probability=0.9,
            rnnoise_available=True,
        ),
        0.1,
    )

    assert submitted.status is DetectorSubmitStatus.ACCEPTED
    assert detector.smart_turn_readiness is SmartTurnReadiness.LOADING
    assert detector.endpointing_ready(turn_token) is False
    coordinator.prepare_release.set()
    lease = await asyncio.wait_for(prepare_task, 1)
    assert lease is not None
    await lease.release()
    await detector.close()


async def test_scoped_detector_events_bind_before_logical_complete() -> None:
    events: list[DetectorActivityEvent | DetectorTurnEvent] = []
    detector: DetectorRuntime
    turn_token = VoiceTurnToken(_ingress_token(), turn_id=7)

    async def on_event(event) -> None:
        events.append(event)
        if (
            isinstance(event, DetectorActivityEvent)
            and event.activity is SpeechActivityEvent.SPEECH_STARTED
        ):
            assert await detector.bind_candidate(event.candidate, turn_token)

    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(
            (
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.CANDIDATE_PAUSE,
            )
        ),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_event=on_event,
    )
    lease = await detector.prepare_endpointing(turn_token)
    assert lease is not None

    submitted = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert submitted.status is DetectorSubmitStatus.ACCEPTED
    for _ in range(100):
        if any(isinstance(event, DetectorTurnEvent) for event in events):
            break
        await asyncio.sleep(0.001)

    assert [type(event) for event in events] == [
        DetectorPrewarmEvent,
        DetectorActivityEvent,
        DetectorActivityEvent,
        DetectorTurnEvent,
    ]
    complete = events[-1]
    assert isinstance(complete, DetectorTurnEvent)
    assert complete.bound_turn.turn_token == turn_token
    assert not detector._bound_turns
    await lease.release()
    await detector.close()


async def test_late_candidate_binding_retires_deferred_completion() -> None:
    events: list[DetectorActivityEvent | DetectorTurnEvent] = []
    candidate = None
    turn_token = VoiceTurnToken(_ingress_token(), turn_id=8)

    async def on_event(event) -> None:
        nonlocal candidate
        events.append(event)
        if (
            isinstance(event, DetectorActivityEvent)
            and event.activity is SpeechActivityEvent.SPEECH_STARTED
        ):
            candidate = event.candidate

    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(
            (
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.CANDIDATE_PAUSE,
            )
        ),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_event=on_event,
    )
    lease = await detector.prepare_endpointing(turn_token)
    assert lease is not None

    submitted = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert submitted.status is DetectorSubmitStatus.ACCEPTED
    for _ in range(100):
        if candidate is not None and candidate in detector._deferred_completions:
            break
        await asyncio.sleep(0.001)

    assert candidate is not None
    assert candidate in detector._deferred_completions
    bound = await detector.bind_candidate(candidate, turn_token)

    assert bound is not None
    assert bound.turn_token == turn_token
    assert candidate not in detector._bound_turns
    assert candidate not in detector._deferred_completions
    assert sum(isinstance(event, DetectorTurnEvent) for event in events) == 1
    await lease.release()
    await detector.close()


async def test_deferred_completion_retires_candidate_before_third_turn() -> None:
    events: list[DetectorActivityEvent | DetectorTurnEvent] = []
    candidates = []
    detector: DetectorRuntime
    turn_tokens = [
        VoiceTurnToken(_ingress_token(), turn_id=turn_id) for turn_id in range(1, 4)
    ]

    async def on_event(event) -> None:
        events.append(event)
        if (
            isinstance(event, DetectorActivityEvent)
            and event.activity is SpeechActivityEvent.SPEECH_STARTED
        ):
            turn_token = turn_tokens[len(candidates)]
            candidates.append(event.candidate)
            bound = await detector.bind_candidate(event.candidate, turn_token)
            assert bound is not None
            assert bound.turn_token == turn_token

    gate = _Gate(
        (
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.CANDIDATE_PAUSE,
        )
    )
    coordinator = _ResetClearsSemanticCoordinator()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_event=on_event,
    )

    await detector.feed(b"\x01\x00" * 160)
    assert detector._candidate_generation == 1

    await detector.feed(b"\x02\x00" * 160)
    async with asyncio.timeout(1):
        while not detector._deferred_turn_complete:
            await asyncio.sleep(0)
    deferred_candidate = candidates[1]
    assert detector._candidate_generation == 2
    assert deferred_candidate not in detector._bound_turns

    gate.events = ()
    third_pcm = b"\x03\x00" * 160
    await detector.feed(third_pcm)
    reset_calls = coordinator.reset_calls

    await detector.release_deferred_turn()
    assert detector._candidate_generation == 2
    assert deferred_candidate not in detector._bound_turns
    assert deferred_candidate not in detector._deferred_completions
    assert coordinator.reset_calls == reset_calls
    assert third_pcm in coordinator.audio

    gate.events = (
        SpeechActivityEvent.SPEECH_STARTED,
        SpeechActivityEvent.CANDIDATE_PAUSE,
    )
    await detector.feed(b"\x04\x00" * 160)

    assert [candidate.candidate_generation for candidate in candidates] == [0, 1, 2]
    assert [
        event.bound_turn.turn_token.turn_id
        for event in events
        if isinstance(event, DetectorTurnEvent)
    ] == [1, 2, 3]
    await detector.close()


async def test_smart_turn_readiness_is_pinned_to_one_logical_turn() -> None:
    coordinator = _SemanticCoordinator()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(
        VoiceIngressToken(1, "socket", 1, 1, 1),
        turn_id=1,
    )

    lease = await detector.prepare_endpointing(token)

    assert lease is not None
    assert detector.smart_turn_readiness is SmartTurnReadiness.READY
    assert detector.endpointing_ready(token) is True
    await lease.release()
    assert detector.endpointing_ready(token) is False
    assert detector.smart_turn_readiness is SmartTurnReadiness.UNLOADED

    next_token = VoiceTurnToken(token.ingress, turn_id=2)
    next_lease = await detector.prepare_endpointing(next_token)

    assert next_lease is not None
    assert coordinator.prepare_calls == 2
    await detector.reset()
    assert detector.smart_turn_readiness is SmartTurnReadiness.UNLOADED
    await next_lease.release()
    await detector.close()


async def test_provider_authority_streaming_never_builds_or_prepares_smart_turn() -> (
    None
):
    coordinator = _SemanticCoordinator()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate((SpeechActivityEvent.CANDIDATE_PAUSE,)),
        provider_policy=AsrProviderPolicy(
            transport="streaming",
            endpoint_authority="provider",
            smart_turn_required=False,
            max_segment_ms=None,
            warm_transport_ms=25_000,
            replay_policy="preconnect_only",
        ),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )

    result = await detector.feed(b"\x01\x00" * 160)

    assert result.events == (SpeechActivityEvent.CANDIDATE_PAUSE,)
    assert detector._semantic_adapter is None
    assert detector.smart_turn_readiness is SmartTurnReadiness.UNLOADED
    assert coordinator.prepare_calls == 0
    assert coordinator.audio == []
    assert coordinator.state is CoordinatorState.IDLE
    await detector.close()
    assert coordinator.state is CoordinatorState.IDLE


async def test_manual_streaming_provider_builds_and_prepares_smart_turn() -> None:
    coordinator = _SemanticCoordinator()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=AsrProviderPolicy(
            transport="streaming",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=None,
            warm_transport_ms=25_000,
            replay_policy="preconnect_only",
        ),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(_ingress_token(), turn_id=1)

    lease = await detector.prepare_endpointing(token)

    assert lease is not None
    assert detector.smart_turn_readiness is SmartTurnReadiness.READY
    assert detector.endpointing_ready(token) is True
    assert coordinator.prepare_calls == 1
    await lease.release()
    await detector.close()


async def test_cancelled_smart_turn_release_can_be_retried() -> None:
    token = VoiceTurnToken(
        VoiceIngressToken(1, "socket", 1, 1, 1),
        turn_id=1,
    )
    runtime = SimpleNamespace(
        release_endpointing=AsyncMock(
            side_effect=[asyncio.CancelledError(), None],
        )
    )
    lease = SmartTurnLease(token, runtime, 7)

    with pytest.raises(asyncio.CancelledError):
        await lease.release()
    assert lease._released is False

    await lease.release()

    assert lease._released is True
    assert runtime.release_endpointing.await_count == 2


async def test_smart_turn_prepare_failure_never_becomes_ready() -> None:
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=AsrProviderPolicy(
            transport="segmented",
            endpoint_authority="smart_turn",
            smart_turn_required=True,
            max_segment_ms=27_000,
            warm_transport_ms=0,
            replay_policy="none",
        ),
        coordinator=_SemanticCoordinator(available=False),
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(
        VoiceIngressToken(1, "socket", 1, 1, 1),
        turn_id=1,
    )

    assert await detector.prepare_endpointing(token) is None
    assert detector.smart_turn_readiness is SmartTurnReadiness.FAILED
    assert detector.endpointing_ready(token) is False
    await detector.close()


async def test_smart_turn_prepare_exception_cleans_task_and_pin() -> None:
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_RaisingPrepareCoordinator(),
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(_ingress_token(), turn_id=1)

    assert await detector.prepare_endpointing(token) is None
    assert detector.smart_turn_readiness is SmartTurnReadiness.FAILED
    assert detector._prepare_task is None
    assert detector._prepare_token is None
    assert detector._semantic_adapter._smart_turn_pin_count == 0
    await detector.close()


async def test_close_cancels_prepare_after_cleanup() -> None:
    coordinator = _BlockingSemanticCoordinator(block_prepare=True)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(_ingress_token(), turn_id=1)
    prepare = asyncio.create_task(detector.prepare_endpointing(token))
    await asyncio.wait_for(coordinator.prepare_started.wait(), 1)

    await detector.close()

    with pytest.raises(asyncio.CancelledError):
        await prepare
    assert detector._prepare_task is None
    assert detector._prepare_token is None
    assert detector._semantic_adapter._smart_turn_pin_count == 0
    assert detector.smart_turn_readiness is SmartTurnReadiness.UNLOADED


async def test_reset_during_inflight_prepare_does_not_block_next_turn() -> None:
    coordinator = _BlockingSemanticCoordinator(block_prepare=True)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    first_token = VoiceTurnToken(_ingress_token(), turn_id=1)
    first = asyncio.create_task(detector.prepare_endpointing(first_token))
    await asyncio.wait_for(coordinator.prepare_started.wait(), 1)

    await detector.reset()
    assert detector._prepare_task is not None
    assert detector._prepare_token is None

    next_token = VoiceTurnToken(_ingress_token(), turn_id=2)
    second = asyncio.create_task(detector.prepare_endpointing(next_token))
    await asyncio.sleep(0)
    coordinator.prepare_release.set()

    lease = await asyncio.wait_for(second, 1)
    assert lease is not None
    assert detector.smart_turn_readiness is SmartTurnReadiness.READY
    assert detector.endpointing_ready(next_token) is True
    # The superseded waiter loses its turn without raising or going fatal.
    assert await asyncio.wait_for(first, 1) is None
    assert detector._semantic_adapter._smart_turn_pin_count == 1
    await lease.release()
    assert detector._semantic_adapter._smart_turn_pin_count == 0
    await detector.close()


async def test_stale_prepare_waiter_cannot_clear_same_token_successor() -> None:
    class _SequencedCoordinator(_SemanticCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.first_release = asyncio.Event()
            self.second_started = asyncio.Event()
            self.second_release = asyncio.Event()

        async def prepare_predictor(self) -> bool:
            self.prepare_calls += 1
            if self.prepare_calls == 1:
                self.first_started.set()
                await self.first_release.wait()
            else:
                self.second_started.set()
                await self.second_release.wait()
            return True

    coordinator = _SequencedCoordinator()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(_ingress_token(), turn_id=1)
    first = asyncio.create_task(detector.prepare_endpointing(token))
    await asyncio.wait_for(coordinator.first_started.wait(), 1)
    first_prepare_task = detector._prepare_task
    assert first_prepare_task is not None
    await detector.reset()

    old_finish_started = asyncio.Event()
    allow_old_finish = asyncio.Event()
    successor_finish_started = asyncio.Event()
    allow_successor_finish = asyncio.Event()
    original_finish = detector._finish_prepare_waiter

    async def controlled_finish(prepare_task, *args, **kwargs):
        if prepare_task is first_prepare_task:
            old_finish_started.set()
            await allow_old_finish.wait()
        else:
            successor_finish_started.set()
            await allow_successor_finish.wait()
        return await original_finish(prepare_task, *args, **kwargs)

    detector._finish_prepare_waiter = controlled_finish
    successor = asyncio.create_task(detector.prepare_endpointing(token))
    coordinator.first_release.set()
    await asyncio.wait_for(old_finish_started.wait(), 1)
    await asyncio.wait_for(coordinator.second_started.wait(), 1)
    coordinator.second_release.set()
    await asyncio.wait_for(successor_finish_started.wait(), 1)

    allow_old_finish.set()
    assert await asyncio.wait_for(first, 1) is None
    assert detector.endpointing_ready(token) is True

    allow_successor_finish.set()
    lease = await asyncio.wait_for(successor, 1)
    assert lease is not None
    assert detector.endpointing_ready(token) is True
    await lease.release()
    await detector.close()


async def test_concurrent_same_token_prepare_calls_coalesce() -> None:
    coordinator = _BlockingSemanticCoordinator(block_prepare=True)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(_ingress_token(), turn_id=1)
    first = asyncio.create_task(detector.prepare_endpointing(token))
    await asyncio.wait_for(coordinator.prepare_started.wait(), 1)
    second_started = asyncio.Event()

    async def prepare_second_lease():
        second_started.set()
        return await detector.prepare_endpointing(token)

    second = asyncio.create_task(prepare_second_lease())
    await asyncio.wait_for(second_started.wait(), 1)
    coordinator.prepare_release.set()

    first_lease = await asyncio.wait_for(first, 1)
    second_lease = await asyncio.wait_for(second, 1)
    assert first_lease is not None
    assert second_lease is not None
    assert coordinator.prepare_calls == 1
    await first_lease.release()
    assert detector.endpointing_ready(token) is True
    assert detector._semantic_adapter._smart_turn_pin_count == 1
    await second_lease.release()
    assert detector.endpointing_ready(token) is False
    assert detector._semantic_adapter._smart_turn_pin_count == 0
    await detector.close()


async def test_cancelled_same_token_prepare_waiters_do_not_orphan_pin() -> None:
    coordinator = _BlockingSemanticCoordinator(block_prepare=True)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(_ingress_token(), turn_id=1)
    first = asyncio.create_task(detector.prepare_endpointing(token))
    await asyncio.wait_for(coordinator.prepare_started.wait(), 1)
    second_started = asyncio.Event()

    async def prepare_second_lease():
        second_started.set()
        return await detector.prepare_endpointing(token)

    second = asyncio.create_task(prepare_second_lease())
    await asyncio.wait_for(second_started.wait(), 1)
    prepare_task = detector._prepare_task
    assert prepare_task is not None

    first.cancel()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(asyncio.CancelledError):
        await second

    coordinator.prepare_release.set()
    assert await asyncio.wait_for(prepare_task, 1) is False
    assert detector.endpointing_ready(token) is False
    assert detector.smart_turn_readiness is SmartTurnReadiness.UNLOADED
    assert detector._semantic_adapter._smart_turn_pin_count == 0
    await detector.close()


async def test_close_while_waiting_for_stale_prepare_cleans_up() -> None:
    coordinator = _BlockingSemanticCoordinator(block_prepare=True)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=coordinator,
        on_turn_complete=AsyncMock(),
    )
    first_token = VoiceTurnToken(_ingress_token(), turn_id=1)
    first = asyncio.create_task(detector.prepare_endpointing(first_token))
    await asyncio.wait_for(coordinator.prepare_started.wait(), 1)
    await detector.reset()
    next_token = VoiceTurnToken(_ingress_token(), turn_id=2)
    second = asyncio.create_task(detector.prepare_endpointing(next_token))
    await asyncio.sleep(0)

    await detector.close()

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(
        result is None or isinstance(result, asyncio.CancelledError)
        for result in results
    )
    assert detector._prepare_task is None
    assert detector._prepare_token is None
    assert detector._semantic_adapter._smart_turn_pin_count == 0
    assert detector.smart_turn_readiness is SmartTurnReadiness.UNLOADED


async def test_cancelled_detector_close_retry_waits_for_owned_cleanup() -> None:
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
    )
    adapter = _OverflowAdapter()
    detector._semantic_adapter = adapter
    detector._semantic_started = True

    async def blocked_overflow_reset() -> None:
        adapter.reset_started.set()
        await adapter.reset_release.wait()

    overflow_reset = asyncio.create_task(blocked_overflow_reset())
    detector._overflow_reset_task = overflow_reset
    await adapter.reset_started.wait()
    close_impl_started = asyncio.Event()
    original_close_impl = detector._close_impl

    async def observed_close_impl() -> None:
        close_impl_started.set()
        await original_close_impl()

    detector._close_impl = observed_close_impl

    first_close = asyncio.create_task(detector.close())
    await close_impl_started.wait()
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    assert detector._closed is True
    assert overflow_reset.cancelled() is False
    assert adapter.closed is False

    retry_started = asyncio.Event()

    async def retry_detector_close() -> None:
        retry_started.set()
        await detector.close()

    retry_close = asyncio.create_task(retry_detector_close())
    await retry_started.wait()
    assert retry_close.done() is False

    adapter.reset_release.set()
    await asyncio.wait_for(retry_close, 1)
    assert adapter.closed is True


async def test_overflow_reset_rejects_audio_until_barrier_finishes() -> None:
    shadow = _SpeakerShadowSpy()
    shadow.reset_error = RuntimeError("observer reset failed")
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        speaker_shadow=shadow,
        on_turn_complete=AsyncMock(),
    )
    assert detector._open_speaker_shadow_candidate("smart_turn_turn") is not None
    adapter = _OverflowAdapter()
    detector._semantic_adapter = adapter
    detector._semantic_started = True

    first = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    await asyncio.wait_for(adapter.reset_started.wait(), 1)
    second = await detector.submit_audio(
        b"\x02\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )

    assert first.status is DetectorSubmitStatus.BACKPRESSURE
    assert second.status is DetectorSubmitStatus.BACKPRESSURE
    assert adapter.push_calls == 1
    assert detector._speaker_shadow_candidate is None
    assert detector._speaker_shadow_generation == 1

    overflow_reset = detector._overflow_reset_task
    assert overflow_reset is not None
    adapter.reset_release.set()
    await asyncio.wait_for(overflow_reset, 1)
    assert shadow.reset_calls == 1
    third = await detector.submit_audio(
        b"\x03\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert third.status is DetectorSubmitStatus.ACCEPTED
    assert adapter.push_calls == 2
    await detector.close()


async def test_overflow_epoch_bump_clears_deferred_completion_flags() -> None:
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
    )
    adapter = _OverflowAdapter()
    adapter.reset_release.set()
    detector._semantic_adapter = adapter
    detector._semantic_started = True
    # A sealed turn is waiting for the provider final when the queue overflows.
    detector._defer_turn_complete = True
    detector._deferred_turn_complete = True

    result = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )

    assert result.status is DetectorSubmitStatus.BACKPRESSURE
    assert detector._defer_turn_complete is False
    assert detector._deferred_turn_complete is False
    overflow_reset = detector._overflow_reset_task
    assert overflow_reset is not None
    await asyncio.wait_for(overflow_reset, 1)

    # The stale deferred flag must not let release_deferred_turn advance the
    # fresh epoch's first candidate or generation.
    semantic_generation = detector._semantic_generation
    await detector.release_deferred_turn()
    assert detector._candidate_generation == 0
    assert detector._semantic_generation == semantic_generation
    await detector.close()


async def test_invalidate_clears_deferred_completion_flags() -> None:
    shadow = _SpeakerShadowSpy()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        speaker_shadow=shadow,
        on_turn_complete=AsyncMock(),
    )
    token = VoiceTurnToken(_ingress_token(), turn_id=1)
    lease = await detector.prepare_endpointing(token)
    assert lease is not None
    detector._defer_turn_complete = True
    detector._deferred_turn_complete = True
    assert detector._open_speaker_shadow_candidate("smart_turn_turn") is not None
    epoch_before = detector.detector_epoch

    await detector.invalidate(token)

    assert detector.detector_epoch == epoch_before + 1
    assert detector._defer_turn_complete is False
    assert detector._deferred_turn_complete is False
    assert detector._speaker_shadow_candidate is None
    assert detector._speaker_shadow_generation == 1
    assert shadow.reset_calls == 1
    semantic_generation = detector._semantic_generation
    await detector.release_deferred_turn()
    assert detector._candidate_generation == 0
    assert detector._semantic_generation == semantic_generation
    await detector.close()


async def test_gate_reset_serializes_with_concurrent_feed() -> None:
    reset_started = threading.Event()
    reset_release = threading.Event()

    class _BlockingResetGate(_Gate):
        def reset(self) -> None:
            reset_started.set()
            reset_release.wait(timeout=5)

    gate = _BlockingResetGate()
    detector = DetectorRuntime(vad=_Vad(), gate=gate)
    reset_task = asyncio.create_task(detector.reset())
    await asyncio.to_thread(reset_started.wait, 1)
    feed_task = asyncio.create_task(detector.feed(b"\x01\x00" * 160))
    await asyncio.sleep(0.05)

    # The gate counters are unlocked, so feed must not run while reset is
    # still inside the gate.
    assert gate.inputs == []
    reset_release.set()
    await asyncio.wait_for(asyncio.gather(reset_task, feed_task), 1)
    assert gate.inputs == [b"\x01\x00" * 160]
    await detector.close()


async def test_stale_reset_consumed_after_newer_reset_keeps_new_identity() -> None:
    gate = _Gate()
    adapter = _VoiceTurnAdapter(
        vad=_Vad(),
        gate=gate,
        coordinator=_SemanticCoordinator(),
        on_commit=AsyncMock(),
    )
    await adapter.start()
    await adapter.push_audio(
        generation=1, buffer_epoch=0, utterance_id=1, pcm16=b"\x01\x00" * 160
    )
    await adapter.wait_idle()
    assert adapter._identity == (1, 0, 1)

    loop = asyncio.get_running_loop()
    stale_completed: asyncio.Future[None] = loop.create_future()
    newer_completed: asyncio.Future[None] = loop.create_future()
    # Racing resets both enqueue with priority, so the later-enqueued newer
    # reset is consumed first and the stale one afterwards.
    adapter._queue.put_control_nowait(
        _ResetItem((2, 0, 2), stale_completed), priority=True
    )
    adapter._queue.put_control_nowait(
        _ResetItem((3, 0, 3), newer_completed), priority=True
    )
    await asyncio.wait_for(asyncio.gather(newer_completed, stale_completed), 1)

    assert adapter._identity == (3, 0, 3)
    gate.inputs.clear()
    await adapter.push_audio(
        generation=3, buffer_epoch=0, utterance_id=3, pcm16=b"\x02\x00" * 160
    )
    await adapter.wait_idle()
    assert gate.inputs == [b"\x02\x00" * 160]
    await adapter.close()


async def test_silero_unavailable_keeps_periodic_smart_turn_authority() -> None:
    completed = asyncio.Event()
    detector = DetectorRuntime(
        vad=_Vad(available=False),
        gate=_Gate(),
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=lambda: completed.set() or asyncio.sleep(0),
    )
    token = VoiceTurnToken(
        VoiceIngressToken(1, "socket", 1, 1, 1),
        turn_id=1,
    )
    lease = await detector.prepare_endpointing(token)
    assert lease is not None

    result = await detector.feed(b"\x01\x00" * 8_000)
    await detector.feed(b"\x02\x00" * 160)
    await asyncio.wait_for(completed.wait(), 1)

    assert result.throttle_available is False
    assert result.endpointing_available is True
    assert result.events == (SpeechActivityEvent.SPEECH_STARTED,)
    assert detector.endpointing_ready(token) is True
    await lease.release()
    await detector.close()


def test_custom_vad_requires_matching_gate() -> None:
    with pytest.raises(ValueError, match="gate is required"):
        DetectorRuntime(vad=_Vad())


async def test_detector_validates_pcm_and_accepts_empty_frames() -> None:
    detector = DetectorRuntime(vad=_Vad(), gate=_Gate())

    with pytest.raises(ValueError, match="complete PCM16"):
        await detector.feed(b"\x00")
    assert await detector.feed(b"") == DetectorFeedResult((), True)


async def test_detector_latches_load_and_inference_failures() -> None:
    load_failed = DetectorRuntime(vad=_FailingVad(), gate=_Gate())
    inference_failed = DetectorRuntime(vad=_Vad(), gate=_FailingGate())

    load_result = await load_failed.feed(b"\x00\x00")
    inference_result = await inference_failed.feed(b"\x00\x00")
    latched_result = await inference_failed.feed(b"\x00\x00")

    assert load_result.throttle_available is False
    assert load_result.identity is None
    assert load_result.candidate is None
    assert inference_result.throttle_available is False
    assert inference_result.identity is None
    assert inference_result.candidate is None
    assert latched_result.events == ()
    assert latched_result.identity is None
    assert latched_result.candidate is None
    load_diagnostics = load_failed.speaker_rejection_diagnostics_snapshot()
    assert load_diagnostics["detector_vad_load_exception_count"] == 1
    assert load_diagnostics["detector_vad_load_unavailable_count"] == 0
    inference_diagnostics = inference_failed.speaker_rejection_diagnostics_snapshot()
    assert inference_diagnostics["detector_gate_exception_count"] == 1
    assert inference_diagnostics["detector_feed_unavailable_count"] == 1


async def test_detector_reset_and_close_are_idempotent() -> None:
    vad = _Vad()
    gate = _Gate()
    detector = DetectorRuntime(vad=vad, gate=gate)

    await detector.reset()
    await detector.close()
    await detector.reset()
    await detector.close()

    assert vad.closed is True
    assert (await detector.feed(b"\x00\x00")).throttle_available is False


async def test_provider_micro_event_aggregates_global_silero_indices() -> None:
    gate = _EvidenceGate(
        [
            _silero_micro_result(
                (SpeechActivityEvent.SPEECH_STARTED,),
            ),
            _silero_micro_result(
                (SpeechActivityEvent.CANDIDATE_PAUSE,),
                onset_window_count=1,
                offset_window_count=3,
                first_onset_window_index=2,
                last_onset_window_index=2,
                post_confirmation_onset_window_count=1,
            ),
        ]
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )

    for value in (1, 2):
        result = await detector.feed(
            bytes((value, 0)) * 512,
            ingress_token=_ingress_token(),
            rnnoise_evidence=_rnnoise_chunk(),
        )
        assert result.throttle_available is True
    fence = await detector.seal_provider_candidate()

    assert fence is not None
    wrong_fence = ProviderCandidateFence(
        fence.detector_epoch,
        fence.candidate_generation + 1,
        fence.through_sequence_no,
    )
    assert detector.sealed_provider_micro_event_decision(wrong_fence) is None
    decision = detector.sealed_provider_micro_event_decision(fence)
    assert decision is not None
    assert decision.would_suppress is True
    assert decision.suppress is False
    assert decision.reason == "micro_event_shadow"
    assert gate.evidence_calls == 2
    assert gate.feed_calls == 0
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["micro_event_candidate_count"] == 1
    assert diagnostics["micro_event_evidence_complete_count"] == 1
    assert diagnostics["micro_event_would_suppress_count"] == 1
    assert diagnostics["micro_event_fail_open_count"] == 0
    assert diagnostics["micro_event_stale_fence_count"] == 1
    await detector.close()


async def test_provider_micro_event_rnnoise_active_upper_bound_is_inclusive() -> None:
    config = ProviderMicroEventConfig(
        mode="shadow",
        maximum_rnnoise_active_run_upper_bound_ms=160,
    )
    decisions = []
    for sample_count in (2_560, 2_576):
        gate = _EvidenceGate(
            [
                _silero_micro_result(
                    (
                        SpeechActivityEvent.SPEECH_STARTED,
                        SpeechActivityEvent.CANDIDATE_PAUSE,
                    )
                )
            ]
        )
        detector = DetectorRuntime(
            vad=_Vad(),
            gate=gate,
            provider_policy=_provider_endpoint_policy(),
            provider_micro_event_config=config,
        )
        await detector.feed(
            b"\x01\x00" * sample_count,
            ingress_token=_ingress_token(),
            rnnoise_evidence=_rnnoise_chunk(0.35),
        )
        fence = await detector.seal_provider_candidate()
        assert fence is not None
        decisions.append(detector.sealed_provider_micro_event_decision(fence))
        await detector.close()

    assert decisions[0] is not None and decisions[0].would_suppress is True
    assert decisions[1] is not None and decisions[1].would_suppress is False
    assert decisions[1].reason == "rnnoise_active_run_exceeded"


async def test_provider_micro_event_normalizes_only_initial_resume() -> None:
    micro_events = (
        SpeechActivityEvent.SPEECH_STARTED,
        SpeechActivityEvent.CANDIDATE_PAUSE,
    )
    gate = _EvidenceGate(
        [
            _silero_micro_result(micro_events),
            _silero_micro_result(
                (
                    SpeechActivityEvent.SPEECH_RESUMED,
                    SpeechActivityEvent.CANDIDATE_PAUSE,
                ),
                window_count=6,
                onset_window_count=5,
                offset_window_count=1,
                first_onset_window_index=0,
                last_onset_window_index=4,
                post_confirmation_onset_window_count=5,
            ),
            _silero_micro_result(
                (
                    SpeechActivityEvent.SPEECH_RESUMED,
                    SpeechActivityEvent.CANDIDATE_PAUSE,
                    SpeechActivityEvent.SPEECH_RESUMED,
                    SpeechActivityEvent.CANDIDATE_PAUSE,
                )
            ),
        ]
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )

    await detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    first_fence = await detector.seal_provider_candidate()
    assert first_fence is not None
    assert await detector.complete_provider_candidate(first_fence) is False

    await detector.feed(
        b"\x02\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    second_fence = await detector.seal_provider_candidate()
    assert second_fence is not None
    second = detector.sealed_provider_micro_event_decision(second_fence)
    assert second is not None and second.would_suppress is True
    assert await detector.complete_provider_candidate(second_fence) is False

    await detector.feed(
        b"\x03\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    third_fence = await detector.seal_provider_candidate()
    assert third_fence is not None
    third = detector.sealed_provider_micro_event_decision(third_fence)
    assert third is not None and third.would_suppress is False
    assert third.reason == "multiple_or_resumed_speech_segments"
    assert third.fail_open is False
    await detector.close()


@pytest.mark.parametrize(
    "events",
    [
        (
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.CANDIDATE_PAUSE,
        ),
        (
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.CANDIDATE_PAUSE,
            SpeechActivityEvent.CANDIDATE_PAUSE,
        ),
    ],
)
async def test_provider_micro_event_rejects_duplicate_activity_transitions(
    events: tuple[SpeechActivityEvent, ...],
) -> None:
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_EvidenceGate([_silero_micro_result(events)]),
        provider_policy=_provider_endpoint_policy(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )
    await detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    fence = await detector.seal_provider_candidate()

    assert fence is not None
    decision = detector.sealed_provider_micro_event_decision(fence)
    assert decision is not None
    assert decision.would_suppress is False
    assert decision.reason == "unordered_silero_events"
    assert decision.fail_open is True
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["micro_event_evidence_unavailable_count"] == 1
    assert diagnostics["micro_event_fail_open_count"] == 1
    await detector.close()


async def test_provider_micro_event_preserves_sealed_snapshot_across_discard() -> None:
    gate = _EvidenceGate(
        [
            _silero_micro_result(
                (
                    SpeechActivityEvent.SPEECH_STARTED,
                    SpeechActivityEvent.CANDIDATE_PAUSE,
                )
            ),
            _silero_micro_result(
                (
                    SpeechActivityEvent.SPEECH_RESUMED,
                    SpeechActivityEvent.CANDIDATE_PAUSE,
                )
            ),
        ]
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )
    await detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    fence = await detector.seal_provider_candidate()
    assert fence is not None
    await detector.feed(
        b"\x02\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )

    assert detector.sealed_provider_micro_event_decision(fence) is not None
    assert await detector.discard_provider_successor(fence) is True
    assert detector.sealed_provider_micro_event_decision(fence) is not None
    assert await detector.complete_provider_candidate(fence) is False
    assert detector.sealed_provider_micro_event_decision(fence) is None
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["micro_event_stale_fence_count"] == 1
    await detector.close()


async def test_provider_micro_event_custom_gate_fails_open_without_evidence() -> None:
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(
            (
                SpeechActivityEvent.SPEECH_STARTED,
                SpeechActivityEvent.CANDIDATE_PAUSE,
            )
        ),
        provider_policy=_provider_endpoint_policy(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )
    await detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    fence = await detector.seal_provider_candidate()

    assert fence is not None
    decision = detector.sealed_provider_micro_event_decision(fence)
    assert decision is not None
    assert decision.would_suppress is False
    assert decision.reason == "incomplete_silero_evidence"
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["micro_event_evidence_unavailable_count"] == 1
    assert diagnostics["micro_event_fail_open_count"] == 1
    assert diagnostics["micro_event_rnnoise_unavailable_count"] == 0
    await detector.close()


async def test_provider_micro_event_missing_pause_and_rnnoise_fail_open() -> None:
    gate = _EvidenceGate([_silero_micro_result((SpeechActivityEvent.SPEECH_STARTED,))])
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )
    await detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=RnnoiseEvidence.unavailable(),
    )
    fence = await detector.seal_provider_candidate()

    assert fence is not None
    decision = detector.sealed_provider_micro_event_decision(fence)
    assert decision is not None
    assert decision.would_suppress is False
    assert decision.reason == "incomplete_rnnoise_evidence"
    diagnostics = detector.speaker_rejection_diagnostics_snapshot()
    assert diagnostics["micro_event_evidence_unavailable_count"] == 1
    assert diagnostics["micro_event_fail_open_count"] == 1
    assert diagnostics["micro_event_rnnoise_unavailable_count"] == 1
    await detector.close()


async def test_provider_micro_event_default_off_uses_legacy_gate_feed() -> None:
    gate = _EvidenceGate(
        [
            _silero_micro_result(
                (
                    SpeechActivityEvent.SPEECH_STARTED,
                    SpeechActivityEvent.CANDIDATE_PAUSE,
                )
            )
        ]
    )
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_provider_endpoint_policy(),
    )

    await detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    fence = await detector.seal_provider_candidate()

    assert fence is not None
    assert gate.feed_calls == 1
    assert gate.evidence_calls == 0
    assert detector.sealed_provider_micro_event_decision(fence) is None
    assert (
        detector.speaker_rejection_diagnostics_snapshot()["micro_event_candidate_count"]
        == 0
    )
    await detector.close()


async def test_smart_turn_uses_legacy_gate_feed_with_micro_event_config() -> None:
    gate = _EvidenceGate([_silero_micro_result((SpeechActivityEvent.SPEECH_STARTED,))])
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )

    await detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )

    assert gate.feed_calls == 1
    assert gate.evidence_calls == 0
    assert (
        detector.speaker_rejection_diagnostics_snapshot()["micro_event_candidate_count"]
        == 0
    )
    await detector.close()


async def test_provider_micro_event_reset_and_close_clear_sealed_snapshot() -> None:
    result = _silero_micro_result(
        (
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.CANDIDATE_PAUSE,
        )
    )
    reset_detector = DetectorRuntime(
        vad=_Vad(),
        gate=_EvidenceGate([result]),
        provider_policy=_provider_endpoint_policy(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )
    await reset_detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    reset_fence = await reset_detector.seal_provider_candidate()
    assert reset_fence is not None
    await reset_detector.reset()
    assert reset_detector.sealed_provider_micro_event_decision(reset_fence) is None
    await reset_detector.close()

    close_detector = DetectorRuntime(
        vad=_Vad(),
        gate=_EvidenceGate([result]),
        provider_policy=_provider_endpoint_policy(),
        provider_micro_event_config=ProviderMicroEventConfig(mode="shadow"),
    )
    await close_detector.feed(
        b"\x01\x00" * 512,
        ingress_token=_ingress_token(),
        rnnoise_evidence=_rnnoise_chunk(),
    )
    close_fence = await close_detector.seal_provider_candidate()
    assert close_fence is not None
    await close_detector.close()
    assert close_detector.sealed_provider_micro_event_decision(close_fence) is None


def _post_deny_silero_gate() -> SileroActivityGate:
    return SileroActivityGate(
        _SileroVadStub(),  # type: ignore[arg-type]
        SmartTurnConfig(enabled=True),
    )


async def test_detector_deny_rearm_prepare_is_idempotent_per_identity() -> None:
    gate = _DenyRearmGate()
    detector = DetectorRuntime(vad=_Vad(), gate=gate)

    first = await detector.prepare_deny_rearm(
        cleanup_generation=3,
        cutoff_sequence=17,
        expected_detector_epoch=detector.detector_epoch,
    )
    duplicate = await detector.prepare_deny_rearm(
        cleanup_generation=3,
        cutoff_sequence=17,
        expected_detector_epoch=detector.detector_epoch,
    )
    new_cutoff = await detector.prepare_deny_rearm(
        cleanup_generation=3,
        cutoff_sequence=18,
        expected_detector_epoch=detector.detector_epoch,
    )
    new_cleanup = await detector.prepare_deny_rearm(
        cleanup_generation=4,
        cutoff_sequence=18,
        expected_detector_epoch=detector.detector_epoch,
    )

    assert (first, duplicate, new_cutoff, new_cleanup) == (True, True, True, True)
    assert gate.prepare_calls == 3
    await detector.close()


async def test_detector_deny_rearm_rejects_stale_epoch_and_closed_runtime() -> None:
    gate = _DenyRearmGate()
    detector = DetectorRuntime(vad=_Vad(), gate=gate)

    stale = await detector.prepare_deny_rearm(
        cleanup_generation=1,
        cutoff_sequence=0,
        expected_detector_epoch=detector.detector_epoch + 1,
    )
    await detector.close()
    closed = await detector.prepare_deny_rearm(
        cleanup_generation=1,
        cutoff_sequence=0,
        expected_detector_epoch=detector.detector_epoch,
    )

    assert stale is False
    assert closed is False
    assert gate.prepare_calls == 0


async def test_detector_deny_rearm_fails_closed_when_silero_is_unavailable() -> None:
    detector = DetectorRuntime(vad=_Vad(available=False), gate=_DenyRearmGate())

    prepared = await detector.prepare_deny_rearm(
        cleanup_generation=1,
        cutoff_sequence=0,
        expected_detector_epoch=detector.detector_epoch,
    )

    assert prepared is False
    await detector.close()


async def test_detector_deny_rearm_fails_closed_on_gate_exception() -> None:
    gate = _DenyRearmGate()
    gate.prepare_error = RuntimeError("forced gate failure")
    detector = DetectorRuntime(vad=_Vad(), gate=gate)

    prepared = await detector.prepare_deny_rearm(
        cleanup_generation=1,
        cutoff_sequence=0,
        expected_detector_epoch=detector.detector_epoch,
    )

    assert prepared is False
    assert gate.prepare_calls == 1
    await detector.close()


async def test_semantic_deny_rearm_prepare_is_ordered_after_admitted_audio() -> None:
    gate = _DenyRearmGate()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
    )

    submitted = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    prepared = await detector.prepare_deny_rearm(
        cleanup_generation=1,
        cutoff_sequence=1,
        expected_detector_epoch=detector.detector_epoch,
    )

    assert submitted.status is DetectorSubmitStatus.ACCEPTED
    assert prepared is True
    assert gate.operations == ["audio", "prepare"]
    await detector.close()


async def test_semantic_deny_rearm_audio_emits_no_normal_control_event() -> None:
    on_event = AsyncMock()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_DenyRearmGate(),
        resource_optimization_enabled=False,
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_event=on_event,
    )
    prepared = await detector.prepare_deny_rearm(
        cleanup_generation=1,
        cutoff_sequence=0,
        expected_detector_epoch=detector.detector_epoch,
    )

    submitted = await detector.submit_audio(
        b"\x00\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.0,
        rnnoise_available=True,
    )

    assert prepared is True
    assert submitted.status is DetectorSubmitStatus.ACCEPTED
    assert submitted.control_event_emitted is False
    assert detector.candidate_open is False
    on_event.assert_not_awaited()
    await detector.close()


async def test_semantic_deny_rearm_cancel_completes_or_leaves_no_partial_token() -> (
    None
):
    gate = _BlockingDenyRearmGate()
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=gate,
        provider_policy=_smart_turn_policy(),
        coordinator=_SemanticCoordinator(),
        on_turn_complete=AsyncMock(),
    )
    submitted = await detector.submit_audio(
        b"\x01\x00" * 160,
        ingress_token=_ingress_token(),
        sample_rate_hz=16_000,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert submitted.status is DetectorSubmitStatus.ACCEPTED
    assert await asyncio.to_thread(gate.feed_started.wait, 1)
    preparation = asyncio.create_task(
        detector.prepare_deny_rearm(
            cleanup_generation=5,
            cutoff_sequence=8,
            expected_detector_epoch=detector.detector_epoch,
        )
    )
    await asyncio.sleep(0)
    preparation.cancel()
    gate.feed_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(preparation, 1)
    retry = await detector.prepare_deny_rearm(
        cleanup_generation=5,
        cutoff_sequence=8,
        expected_detector_epoch=detector.detector_epoch,
    )

    assert retry is True
    assert gate.prepare_calls == 1
    assert gate.operations == ["audio", "prepare"]
    await detector.close()


def test_silero_deny_rearm_accepts_initial_continuous_silence_boundary() -> None:
    gate = _post_deny_silero_gate()
    gate.prepare_post_deny_silence_boundary()

    result = gate.process_probabilities([0.0] * 10)

    assert result == (SpeechActivityEvent.CANDIDATE_PAUSE,)


def test_silero_deny_rearm_requires_complete_silence_window() -> None:
    gate = _post_deny_silero_gate()
    gate.prepare_post_deny_silence_boundary()

    result = gate.process_probabilities([0.0] * 9)

    assert result == ()


@pytest.mark.parametrize("interruption_probability", [0.5, 0.4])
def test_silero_deny_rearm_interruption_restarts_silence_proof(
    interruption_probability: float,
) -> None:
    gate = _post_deny_silero_gate()
    gate.prepare_post_deny_silence_boundary()

    interrupted = gate.process_probabilities(
        [0.0] * 5 + [interruption_probability] + [0.0] * 5
    )
    completed = gate.process_probabilities([0.0] * 5)

    assert interrupted == ()
    assert completed == (SpeechActivityEvent.CANDIDATE_PAUSE,)


def test_silero_normal_initial_silence_remains_event_free() -> None:
    gate = _post_deny_silero_gate()

    result = gate.process_probabilities([0.0] * 20)

    assert result == ()
