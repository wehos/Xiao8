from __future__ import annotations

import asyncio
import inspect
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

import main_logic.asr_client.runtime as runtime_module
from main_logic.asr_client.admission.contracts import (
    AbortProviderTransport,
    AdmissionDisposition,
    AdmissionResolutionTicket,
    AdmissionState,
    ApplyRejection,
    BoundaryExact,
    CandidateBound,
    CaptureState,
    CaptureClosed,
    ExactIntervalAbortResult,
    ExactIntervalOutcome,
    FinalDeadlineExpired,
    LifecycleSettled,
    MicroEventAllowed,
    MicroEventPending,
    MicroEventSuppressed,
    MicroEventUnavailable,
    PendingProviderFinal,
    ProviderBound,
    ProviderFinalReceived,
    RejectionApplied,
    RejectionCapability,
    RejectionCapabilityKind,
    ResolveReserved,
    ScheduleFinalDeadline,
    SettlePartial,
    SpeakerCheckpointKind,
    SpeakerCaptureLeaseToken,
    SpeakerHigh,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseState,
    SpeakerLeaseTerminalClaim,
    SpeakerLeaseTransitionOutcome,
    SpeakerLeaseTransitionReceipt,
    SpeakerLeaseUnavailable,
    SpeakerLow,
    SpeakerUnavailable,
    TransportSettled,
)
from main_logic.asr_client.admission.coordinator import (
    VoiceTurnAdmissionCoordinator,
)
from main_logic.asr_client.admission.ingress import AdmissionIngressLane
from main_logic.asr_client._provider_events import (
    ProviderAudioRange,
    ProviderEndpointNotification,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from main_logic.asr_client.candidate_control import CandidateRejectionOutcome
from main_logic.asr_client.endpointing.detector import (
    DetectorCandidateKey,
    DetectorIngressIdentity,
    ProviderCandidateFence,
    ProviderSpeakerBoundarySnapshot,
)
from main_logic.asr_client.endpointing.detector_runtime import (
    DetectorCandidateRejectionCommitResult,
    DetectorFeedResult,
    DetectorRuntime,
    ProviderExactSpeakerIntervalCommitResult,
    ProviderExactSpeakerIntervalReservation,
    ProviderSpeakerEvidenceAnchorResult,
    ProviderSpeakerEvidenceAnchorStatus,
    ProviderSpeakerEvidenceSettlementStatus,
    ProviderSpeakerEvidenceLease,
    ProviderSpeakerEvidenceUpdate,
)
from main_logic.asr_client.endpointing.micro_event_policy import (
    ProviderMicroEventDecision,
)
from main_logic.asr_client.lifecycle import (
    AudioDisposition,
    FinalKey,
    VoiceInputLifecycleController,
    VoiceLifecycleEvent,
    VoiceLifecycleState,
    VoiceRouteMode,
    VoiceTransportToken,
)
from main_logic.asr_client.provider_policy import resolve_provider_policy
from main_logic.asr_client.runtime import (
    AsrRuntimeCallbacks,
    DenyTransportState,
    IndependentAsrRuntime,
)
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCaptureDecisionState,
    SpeakerShadowCaptureDisposition,
    SpeakerShadowCaptureResult,
    SpeakerShadowCandidateKey,
    SpeakerShadowConfig,
)
from main_logic.asr_client.speaker_verifier_contracts import (
    SpeakerVerifierInstallOutcome,
    SpeakerVerifierOwnershipState,
)
from main_logic.asr_client.transcript import (
    TranscriptResolutionOutcome,
    TranscriptResolutionReceipt,
)
from main_logic.voice_turn.audio_input import ProcessedVoiceFrame
from main_logic.voice_turn.contracts import (
    AsrSubmitStatus,
    SpeechActivityEvent,
    VoiceIngressToken,
    VoiceTurnToken,
)


class _RejectionLease:
    def __init__(
        self,
        detector: object,
        turn_token: VoiceTurnToken,
        *,
        provider_fence: ProviderCandidateFence | None = None,
    ) -> None:
        self.candidate = DetectorCandidateKey(7, 11)
        self.shadow_candidate = _shadow_candidate()
        self.turn_token = turn_token
        self.provider_fence = provider_fence
        self.provider_preseal_verdict = None
        self._detector = detector
        self.commit_calls = 0
        self.commit_result = True

    def belongs_to(self, detector: object) -> bool:
        return detector is self._detector

    def commit(self) -> bool:
        self.commit_calls += 1
        return self.commit_result

    async def commit_async(
        self,
        *,
        deadline: float | None = None,
    ) -> DetectorCandidateRejectionCommitResult:
        detector = self._detector
        detector.commit_entered.set()
        if deadline is not None and runtime_module.time.monotonic() >= deadline:
            return DetectorCandidateRejectionCommitResult.STALE
        if detector.block_commit:
            try:
                if deadline is None:
                    await detector.commit_release.wait()
                else:
                    await asyncio.wait_for(
                        detector.commit_release.wait(),
                        timeout=max(
                            0.0,
                            deadline - runtime_module.time.monotonic(),
                        ),
                    )
            except TimeoutError:
                return DetectorCandidateRejectionCommitResult.STALE
        if deadline is not None and runtime_module.time.monotonic() >= deadline:
            return DetectorCandidateRejectionCommitResult.STALE
        self.commit_calls += 1
        if not self.commit_result:
            return DetectorCandidateRejectionCommitResult.STALE
        if self.provider_fence is not None:
            return DetectorCandidateRejectionCommitResult.SEALED_APPLIED
        if self.provider_preseal_verdict is not None:
            return DetectorCandidateRejectionCommitResult.PRESEAL_READY
        return DetectorCandidateRejectionCommitResult.ACTIVE_APPLIED


class _RejectionDetector:
    def __init__(self) -> None:
        self.detector_epoch = 7
        self.lease: _RejectionLease | None = None
        self.prepare_entered = asyncio.Event()
        self.prepare_release = asyncio.Event()
        self.block_prepare = False
        self.commit_entered = asyncio.Event()
        self.commit_release = asyncio.Event()
        self.block_commit = False
        self.seal_entered = asyncio.Event()
        self.seal_release = asyncio.Event()
        self.block_seal = False
        self.reset = AsyncMock()
        self.replace_speaker_verifier = AsyncMock()
        self.close = AsyncMock()
        self.complete_provider_candidate = AsyncMock(return_value=False)
        self.sealed_provider_micro_event_decision = MagicMock(return_value=None)
        self.release_deferred_turn = AsyncMock()
        self.release_speaker_candidate_binding = MagicMock(return_value=True)
        self.endpointing_ready = MagicMock(return_value=True)
        self.prepare_deny_rearm = AsyncMock(return_value=True)
        self.observe_provider_audio_ordered = AsyncMock(
            side_effect=self._observe_provider_audio_ordered
        )
        self.observe_provider_audio = MagicMock()
        self._provider_evidence_owner = object()
        self._provider_evidence_lease: ProviderSpeakerEvidenceLease | None = None
        self._provider_anchor_starts: dict[ProviderSpeakerEvidenceLease, int] = {}
        self._provider_anchor_revisions: dict[ProviderSpeakerEvidenceLease, int] = {}
        self.abandon_provider_speaker_evidence_lease = AsyncMock(return_value=True)
        self.provisional_pending = False
        self.ready_rejection = False
        self.replace_preseal_lease_on_seal = False
        self.seal_turn_tokens: list[VoiceTurnToken | None] = []
        self.exact_commit_result: ProviderExactSpeakerIntervalCommitResult | None = None
        self.exact_abort_calls = 0
        self.exact_abort_result = True
        self.exact_prepare_entered = asyncio.Event()
        self.exact_prepare_release = asyncio.Event()
        self.block_exact_prepare = False
        self.wait_provider_audio_observed_through = AsyncMock(return_value=True)
        self.wait_provider_speaker_preseal = AsyncMock(return_value=True)

    async def ensure_provider_speaker_evidence_lease(
        self,
    ) -> ProviderSpeakerEvidenceLease:
        lease = self._provider_evidence_lease
        if lease is None:
            lease = ProviderSpeakerEvidenceLease(
                detector_epoch=self.detector_epoch,
                lease_generation=1,
                candidate=_shadow_candidate(),
                _owner=self._provider_evidence_owner,
            )
            self._provider_evidence_lease = lease
        return lease

    async def _observe_provider_audio_ordered(self, pcm16: bytes, **kwargs):
        lease = kwargs.get("speaker_evidence_lease")
        assert type(lease) is ProviderSpeakerEvidenceLease
        sequence_no = kwargs["sequence_no"]
        samples = len(pcm16) // 2
        if kwargs.get("accounting_only"):
            assert kwargs["evidence_complete"] is False
            settlement = runtime_module.ProviderSpeakerEvidenceSettlement(
                lease=lease,
                detector_epoch=self.detector_epoch,
                timeline_generation=kwargs.get("expected_timeline_generation", 0),
                operation_serial=sequence_no,
                status=ProviderSpeakerEvidenceSettlementStatus.RETIRED,
                reason="accounting_only",
            )
            self._provider_last_evidence_settlement = settlement
            self._provider_evidence_lease = None
            return runtime_module.ProviderAudioAccountingReceipt(
                detector_epoch=self.detector_epoch,
                timeline_generation=kwargs.get("expected_timeline_generation", 0),
                sequence_no=sequence_no,
                start_sample_16k=0,
                end_sample_16k=samples,
                evidence_settlement=settlement,
            )
        return ProviderSpeakerEvidenceUpdate(
            lease=lease,
            capture=SpeakerShadowCaptureResult(
                disposition=SpeakerShadowCaptureDisposition.ACCEPTED,
                accepted_sample_count=samples,
                cumulative_sample_count=samples,
                completed_window_sample_count=0,
                decision_state=SpeakerShadowCaptureDecisionState.PENDING,
            ),
            sequence_no=sequence_no,
            last_progress_at=10.0,
        )

    def validate_provider_speaker_evidence_settlement(
        self, settlement, *, lease, timeline_generation=None,
    ):
        return bool(
            settlement is getattr(self, "_provider_last_evidence_settlement", None)
            and settlement is not None
            and settlement.lease is lease
            and settlement.detector_epoch == self.detector_epoch
            and (
                timeline_generation is None
                or settlement.timeline_generation == timeline_generation
            )
        )

    async def anchor_provider_speaker_evidence(
        self,
        lease: ProviderSpeakerEvidenceLease,
        *,
        audio_start_sample_16k: int,
        deadline: float,
    ) -> ProviderSpeakerEvidenceAnchorResult:
        assert deadline > runtime_module.time.monotonic()
        anchored_start = self._provider_anchor_starts.get(lease)
        anchor_revision = self._provider_anchor_revisions.get(lease, 0)
        if anchored_start is None:
            self._provider_anchor_starts[lease] = audio_start_sample_16k
            anchor_revision += 1
            self._provider_anchor_revisions[lease] = anchor_revision
            status = ProviderSpeakerEvidenceAnchorStatus.APPLIED
        elif anchored_start == audio_start_sample_16k:
            status = ProviderSpeakerEvidenceAnchorStatus.IDEMPOTENT
        else:
            status = ProviderSpeakerEvidenceAnchorStatus.CONFLICT
        return ProviderSpeakerEvidenceAnchorResult(
            status=status,
            lease=lease,
            candidate=lease.candidate,
            detector_epoch=self.detector_epoch,
            timeline_generation=1,
            lease_generation=lease.lease_generation,
            anchor_revision=anchor_revision,
            anchor_start_sample_16k=audio_start_sample_16k,
            buffer_origin_sample_16k=0,
            observed_through_sample_16k=160,
            pcm_through_sequence_no=0,
            shadow_runtime_generation=1,
        )

    async def prepare_candidate_rejection(self, _candidate):
        self.prepare_entered.set()
        if self.block_prepare:
            await self.prepare_release.wait()
        return self.lease

    async def prepare_provider_exact_speaker_interval(
        self,
        boundary: ProviderAudioRange,
        *,
        speaker_evidence_lease: ProviderSpeakerEvidenceLease,
    ) -> ProviderExactSpeakerIntervalReservation:
        self.exact_prepare_entered.set()
        if self.block_exact_prepare:
            await self.exact_prepare_release.wait()
        suffix = SpeakerShadowCandidateKey(
            self.detector_epoch,
            speaker_evidence_lease.candidate.shadow_generation + 1,
            "provider_candidate",
        )
        return ProviderExactSpeakerIntervalReservation(
            boundary=boundary,
            target_candidate=speaker_evidence_lease.candidate,
            suffix_candidate=suffix,
            detector_epoch=self.detector_epoch,
            timeline_generation=1,
            lease_generation=speaker_evidence_lease.lease_generation,
            candidate_generation=3,
            shadow_runtime_generation=1,
            anchor_revision=self._provider_anchor_revisions.get(
                speaker_evidence_lease,
                0,
            ),
            anchor_start_sample_16k=self._provider_anchor_starts.get(
                speaker_evidence_lease,
                boundary.start_sample_16k,
            ),
            provider_pcm_through_sequence_no=0,
            _owner=self,
            _token=object(),
        )

    def abort_provider_exact_speaker_interval(self, _reservation) -> bool:
        self.exact_abort_calls += 1
        return self.exact_abort_result

    def commit_provider_exact_speaker_interval(self, reservation):
        if self.exact_commit_result is not None:
            return self.exact_commit_result
        successor = ProviderSpeakerEvidenceLease(
            detector_epoch=self.detector_epoch,
            lease_generation=reservation.lease_generation + 1,
            candidate=reservation.suffix_candidate,
            _owner=self._provider_evidence_owner,
        )
        return ProviderExactSpeakerIntervalCommitResult(
            snapshot=ProviderSpeakerBoundarySnapshot(
                detector_epoch=self.detector_epoch,
                candidate_generation=reservation.candidate_generation,
                through_sequence_no=1,
                shadow_generation=reservation.target_candidate.shadow_generation,
                merged_resume_count=0,
                successor_present=True,
                evidence_complete=True,
                _owner=self,
                boundary_exact=True,
            ),
            target_candidate=reservation.target_candidate,
            successor_evidence_lease=successor,
        )

    async def complete_provider_speaker_boundary(
        self,
        snapshot: ProviderSpeakerBoundarySnapshot,
        *,
        successor_evidence_lease: ProviderSpeakerEvidenceLease | None,
        deadline: float | None = None,
    ) -> str:
        # This Detector-only fixture models immediately applied exact commits;
        # the cross-layer suite exercises real Shadow receipt settlement.
        if (
            type(snapshot) is not ProviderSpeakerBoundarySnapshot
            or snapshot._owner is not self
            or snapshot.detector_epoch != self.detector_epoch
        ):
            return "stale"
        if snapshot.successor_present and (
            type(successor_evidence_lease) is not ProviderSpeakerEvidenceLease
            or successor_evidence_lease._owner is not self._provider_evidence_owner
            or successor_evidence_lease.detector_epoch != self.detector_epoch
        ):
            return "invalid"
        if deadline is not None and runtime_module.time.monotonic() >= deadline:
            return "pending"
        completed = getattr(self, "_completed_boundary_snapshots", None)
        if completed is None:
            completed = self._completed_boundary_snapshots = []
        if any(previous is snapshot for previous in completed):
            return "already_completed"
        completed.append(snapshot)
        return "completed"

    async def seal_provider_candidate(
        self,
        turn_token: VoiceTurnToken | None = None,
        *,
        speaker_snapshot=None,
        deadline: float | None = None,
    ):
        del speaker_snapshot
        self.seal_turn_tokens.append(turn_token)
        self.seal_entered.set()
        if self.block_seal:
            try:
                if deadline is None:
                    await self.seal_release.wait()
                else:
                    await asyncio.wait_for(
                        self.seal_release.wait(),
                        timeout=max(
                            0.0,
                            deadline - runtime_module.time.monotonic(),
                        ),
                    )
            except TimeoutError:
                return None
        if deadline is not None and runtime_module.time.monotonic() >= deadline:
            return None
        lease = self.lease
        if lease is None:
            return None
        if turn_token is not None and turn_token != lease.turn_token:
            return None
        fence = ProviderCandidateFence(7, 11, 23)
        if (
            self.replace_preseal_lease_on_seal
            and lease.provider_preseal_verdict is not None
        ):
            sealed_lease = _RejectionLease(
                self,
                lease.turn_token,
                provider_fence=fence,
            )
            self.lease = sealed_lease
            return fence
        lease.provider_fence = fence
        lease.provider_preseal_verdict = None
        return fence

    def ready_provider_speaker_rejection(self, provider_fence):
        lease = self.lease
        if (
            not self.ready_rejection
            or lease is None
            or lease.provider_fence != provider_fence
        ):
            return None
        return lease.shadow_candidate

    def pending_provider_speaker_candidate(self, provider_fence):
        lease = self.lease
        if (
            not self.provisional_pending
            or lease is None
            or lease.provider_fence != provider_fence
        ):
            return None
        return lease.shadow_candidate


def _callbacks(*, abandoned: AsyncMock | None = None) -> AsrRuntimeCallbacks:
    return AsrRuntimeCallbacks(
        display_name=lambda: "candidate-rejection-test",
        on_prepare_turn=AsyncMock(return_value=True),
        on_partial=AsyncMock(),
        on_final=AsyncMock(),
        on_turn_abandoned=abandoned or AsyncMock(),
        on_failure=AsyncMock(),
        on_status=AsyncMock(),
        on_lifecycle=AsyncMock(),
    )


def test_terminal_runtime_rejects_late_speaker_fact_before_ingress_post() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    runtime._asr_terminal_close_requested = True
    runtime._speaker_verifier_activation_generation = "terminal-generation"
    runtime._asr_admission_ingress_started = True
    runtime._asr_admission_ingress.post_nowait = MagicMock()
    candidate = _shadow_candidate()

    accepted = runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="terminal-generation",
        enforce=True,
    )

    assert accepted is False
    runtime._asr_admission_ingress.post_nowait.assert_not_called()
    assert runtime._asr_admission_candidate_turns == {}


def _install_active_candidate(
    runtime: IndependentAsrRuntime,
    detector: _RejectionDetector,
    *,
    provider: str = "glm",
    endpointing_mode: str = "manual",
) -> tuple[SimpleNamespace, VoiceInputLifecycleController, VoiceTurnToken]:
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy(provider, endpointing_mode),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    session = SimpleNamespace(
        is_ready=True,
        close=AsyncMock(),
        signal_user_activity_end=AsyncMock(),
        stream_audio=AsyncMock(),
    )
    runtime._asr_session = session
    runtime._asr_provider = provider
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = detector
    runtime._asr_current_ingress_token = runtime.capture_ingress_token(
        connection_id="connection",
        lease_generation=1,
        route_generation=1,
    )
    turn_token = runtime._capture_turn_token(lifecycle)
    detector.lease = _RejectionLease(detector, turn_token)
    runtime._asr_partial_turn_token = turn_token
    runtime._asr_turn_prepared = True
    runtime._speaker_verifier_activation_generation = "profile-generation"
    runtime._speaker_verifier_enforces_admission = True
    assert runtime._asr_audio_dispatcher.activate(turn_token, session, b"") is True
    final_key = FinalKey.from_turn(turn_token)
    assert runtime._asr_transcript_dispatcher.try_reserve(final_key) is True
    runtime._asr_admission_reservation_dispatchers[final_key] = (
        runtime._asr_transcript_dispatcher
    )
    runtime._ensure_transport_restart_task = MagicMock()
    return session, lifecycle, turn_token


async def _seal_provider_candidate(
    runtime: IndependentAsrRuntime,
    detector: _RejectionDetector,
) -> tuple[
    SimpleNamespace,
    VoiceInputLifecycleController,
    VoiceTurnToken,
    ProviderCandidateFence,
]:
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    if not runtime._asr_admission_ingress_started:
        await runtime._asr_admission_ingress.start()
        runtime._asr_admission_ingress_started = True
    await runtime._asr_admission_ingress.open_turn(turn_token)
    runtime._asr_admission_reservation_dispatchers[FinalKey.from_turn(turn_token)] = (
        runtime._asr_transcript_dispatcher
    )
    provider_fence = _seal_installed_provider_candidate(
        runtime,
        detector,
        session,
        lifecycle,
        turn_token,
    )
    return session, lifecycle, turn_token, provider_fence


def _seal_installed_provider_candidate(
    runtime: IndependentAsrRuntime,
    detector: _RejectionDetector,
    session: SimpleNamespace,
    lifecycle: VoiceInputLifecycleController,
    turn_token: VoiceTurnToken,
) -> ProviderCandidateFence:
    provider_fence = ProviderCandidateFence(7, 11, 23)
    assert detector.lease is not None
    detector.lease.provider_fence = provider_fence
    assert runtime._asr_audio_dispatcher.seal(
        turn_token,
        session,
        after_sequence=0,
    )
    lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    runtime._asr_sealed_turn_token = VoiceTransportToken(
        turn=turn_token,
        transport_generation=lifecycle.snapshot.transport_generation,
    )
    runtime._asr_provider_candidate_fence = provider_fence
    return provider_fence


def _shadow_candidate() -> SpeakerShadowCandidateKey:
    return SpeakerShadowCandidateKey(7, 3, "provider_candidate")


def _smart_turn_shadow_candidate() -> SpeakerShadowCandidateKey:
    return SpeakerShadowCandidateKey(7, 3, "smart_turn_turn")


async def _open_admission_turn(
    *,
    provider_bound: bool,
    candidate: SpeakerShadowCandidateKey | None = None,
) -> tuple[
    VoiceTurnAdmissionCoordinator,
    AdmissionIngressLane,
    VoiceTurnToken,
    SpeakerShadowCandidateKey,
    ProviderUtteranceKey | None,
]:
    coordinator = VoiceTurnAdmissionCoordinator(clock=lambda: 10.0)
    lane = AdmissionIngressLane(coordinator)
    await lane.start()
    turn_token = VoiceTurnToken(
        VoiceIngressToken(1, "socket", 2, 3, 4),
        1,
    )
    speaker_candidate = candidate or _shadow_candidate()
    provider_key = ProviderUtteranceKey(1, 0, 1) if provider_bound else None
    await lane.open_turn(turn_token)
    await lane.post(turn_token, CandidateBound(speaker_candidate))
    if provider_key is not None:
        await lane.post(turn_token, ProviderBound(provider_key))
    return coordinator, lane, turn_token, speaker_candidate, provider_key


def _admission_capability(
    turn_token: VoiceTurnToken,
    candidate: SpeakerShadowCandidateKey,
    *,
    provider_key: ProviderUtteranceKey | None,
    kind: RejectionCapabilityKind,
) -> RejectionCapability:
    return RejectionCapability(
        capability_id=1,
        owner_generation=7,
        kind=kind,
        turn_token=turn_token,
        candidate=candidate,
        provider_key=provider_key,
    )


def _admission_final(
    provider_key: ProviderUtteranceKey | None,
    *,
    text: str = "final",
) -> PendingProviderFinal:
    return PendingProviderFinal(provider_key, "qwen", text, 10.0, 10.2)


async def _drain_runtime_admission(runtime: IndependentAsrRuntime) -> None:
    for _ in range(20):
        tasks = {
            *runtime._asr_admission_effect_tasks,
            *runtime._asr_admission_candidate_tasks.values(),
        }
        pending = tuple(task for task in tasks if not task.done())
        if pending:
            await asyncio.gather(*pending)
            continue
        await asyncio.sleep(0)
        if not runtime._asr_admission_effect_tasks and not (
            runtime._asr_admission_candidate_tasks
        ):
            return
    raise AssertionError("admission tasks did not become idle")


async def _open_runtime_admission_turn(
    runtime: IndependentAsrRuntime,
    turn_token: VoiceTurnToken,
) -> None:
    if not runtime._asr_admission_ingress_started:
        await runtime._asr_admission_ingress.start()
        runtime._asr_admission_ingress_started = True
    if await runtime._asr_admission.get_record(turn_token) is None:
        await runtime._asr_admission_ingress.open_turn(turn_token)


async def test_enforced_partial_is_quarantined_until_forward_verdict() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
    )

    await runtime._send_independent_asr_preview(
        "first draft",
        runtime._asr_session_epoch,
    )
    await runtime._send_independent_asr_preview(
        "latest draft",
        runtime._asr_session_epoch,
    )

    callbacks.on_partial.assert_not_awaited()
    assert runtime._asr_quarantined_partials == {turn_token: "latest draft"}

    await runtime._execute_admission_effect(
        SettlePartial(turn_token, 1, AdmissionDisposition.FORWARD)
    )

    callbacks.on_partial.assert_awaited_once()
    assert callbacks.on_partial.await_args.args[0].text == "latest draft"
    assert runtime._asr_quarantined_partials == {}

    await runtime._send_independent_asr_preview(
        "admitted draft",
        runtime._asr_session_epoch,
    )
    assert callbacks.on_partial.await_count == 2
    assert callbacks.on_partial.await_args.args[0].text == "admitted draft"
    await _close_dispatchers(runtime)


async def test_denied_partial_is_discarded_and_never_revived() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
    )

    await runtime._send_independent_asr_preview(
        "blocked draft",
        runtime._asr_session_epoch,
    )
    await runtime._execute_admission_effect(
        SettlePartial(turn_token, 1, AdmissionDisposition.DROP)
    )
    await runtime._send_independent_asr_preview(
        "late blocked draft",
        runtime._asr_session_epoch,
    )

    callbacks.on_partial.assert_not_awaited()
    assert runtime._asr_quarantined_partials == {}
    await _close_dispatchers(runtime)


async def test_forward_verdict_drops_cached_partial_when_final_already_pending() -> (
    None
):
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
    )

    await runtime._send_independent_asr_preview(
        "obsolete draft",
        runtime._asr_session_epoch,
    )
    runtime._asr_admission_final_contexts[turn_token] = SimpleNamespace()

    await runtime._execute_admission_effect(
        SettlePartial(turn_token, 1, AdmissionDisposition.FORWARD)
    )

    callbacks.on_partial.assert_not_awaited()
    assert runtime._asr_quarantined_partials == {}
    await _close_dispatchers(runtime)


async def test_unenforced_partial_preserves_immediate_preview_contract() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, _turn_token = _install_active_candidate(
        runtime,
        detector,
    )
    runtime._speaker_verifier_enforces_admission = False

    await runtime._send_independent_asr_preview(
        "ordinary draft",
        runtime._asr_session_epoch,
    )

    callbacks.on_partial.assert_awaited_once()
    assert callbacks.on_partial.await_args.args[0].text == "ordinary draft"
    await _close_dispatchers(runtime)


async def test_candidate_binding_is_published_before_first_score() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
    )
    candidate = detector.lease.shadow_candidate
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    await runtime._asr_admission_ingress.open_turn(turn_token)
    try:
        assert runtime._accept_speaker_candidate_binding(
            candidate,
            turn_token,
            detector=detector,
            activation_generation="profile-generation",
        )
        await _drain_runtime_admission(runtime)

        record = await runtime._asr_admission.get_record(turn_token)
        assert record is not None
        assert record.speaker_candidate == candidate
        assert runtime._asr_admission_candidate_turns[candidate] == turn_token
    finally:
        await _close_dispatchers(runtime)


async def test_deny_before_final_returns_settled_without_installing_context() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token, _provider_fence = await _seal_provider_candidate(
        runtime, detector
    )
    candidate = detector.lease.shadow_candidate
    try:
        await runtime._asr_admission_ingress.post(
            turn_token,
            CandidateBound(candidate),
        )
        await runtime._asr_admission_ingress.post(
            turn_token,
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        )
        await runtime._asr_admission_ingress.post(
            turn_token,
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        )

        settled = await runtime._handle_independent_asr_final(
            "blocked final",
            runtime._asr_session_epoch,
            "qwen",
            received_at=10.0,
            deadline=10.2,
        )

        assert settled is not None and settled.is_set()
        assert turn_token not in runtime._asr_admission_final_contexts
        callbacks.on_final.assert_not_awaited()
    finally:
        await _close_dispatchers(runtime)


async def test_deny_interleaved_with_final_post_retires_late_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token, _provider_fence = await _seal_provider_candidate(
        runtime, detector
    )
    candidate = detector.lease.shadow_candidate
    await runtime._asr_admission_ingress.post(
        turn_token,
        CandidateBound(candidate),
    )
    original_post = runtime._post_admission_event

    async def interleaved_post(token, event, *, now=None):
        if isinstance(event, ProviderFinalReceived):
            await runtime._asr_admission_ingress.post(
                token,
                SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
            )
            await runtime._asr_admission_ingress.post(
                token,
                SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
            )
            record = await runtime._asr_admission.get_record(token)
            assert record is not None
            assert record.resolution_ticket is not None
            runtime._asr_admission_resolutions[FinalKey.from_turn(token)] = (
                runtime_module._AdmissionResolutionExecution(
                    record.resolution_ticket,
                    core_resolution_succeeded=False,
                    owner_done=True,
                )
            )
        return await original_post(token, event, now=now)

    monkeypatch.setattr(runtime, "_post_admission_event", interleaved_post)
    try:
        settled = await runtime._handle_independent_asr_final(
            "blocked final",
            runtime._asr_session_epoch,
            "qwen",
            received_at=10.0,
            deadline=10.2,
        )

        assert settled is not None
        await _drain_runtime_admission(runtime)
        await asyncio.wait_for(settled.wait(), 0.2)
        assert turn_token not in runtime._asr_admission_final_contexts
        callbacks.on_final.assert_not_awaited()
    finally:
        await _close_dispatchers(runtime)


async def test_same_ticket_executor_adopts_context_attached_before_owner_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
    )
    final_key = FinalKey.from_turn(turn_token)
    dispatcher = MagicMock()
    dispatcher.resolve_reserved.return_value = TranscriptResolutionReceipt(
        final_key,
        AdmissionDisposition.DROP,
        TranscriptResolutionOutcome.APPLIED,
        AdmissionDisposition.DROP,
    )
    runtime._asr_admission_reservation_dispatchers[final_key] = dispatcher
    first_context = SimpleNamespace(settled=asyncio.Event())
    second_context = SimpleNamespace(settled=asyncio.Event())
    runtime._asr_admission_final_contexts[turn_token] = first_context
    first_settle_entered = asyncio.Event()
    release_first_settle = asyncio.Event()
    settled_contexts = []

    async def settle_context(_ticket, context) -> None:
        settled_contexts.append(context)
        if context is first_context:
            first_settle_entered.set()
            await release_first_settle.wait()

    monkeypatch.setattr(runtime, "_settle_admission_final", settle_context)
    ticket = AdmissionResolutionTicket(
        turn_token=turn_token,
        record_generation=1,
        resolution_nonce=1,
        disposition=AdmissionDisposition.DROP,
    )
    effect = ResolveReserved(ticket=ticket, final=None)
    owner = asyncio.create_task(runtime._resolve_admission_reservation(effect))
    await asyncio.wait_for(first_settle_entered.wait(), 1)
    runtime._asr_admission_final_contexts[turn_token] = second_context

    await runtime._resolve_admission_reservation(effect)
    execution = runtime._asr_admission_resolutions[final_key]
    assert execution.owner_done is False
    assert execution.late_context is second_context
    release_first_settle.set()
    await asyncio.wait_for(owner, 1)

    assert settled_contexts == [first_context, second_context]
    assert first_context.settled.is_set()
    assert second_context.settled.is_set()
    await _close_dispatchers(runtime)


async def test_second_low_survives_capture_completion_during_arming() -> None:
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    capability = _admission_capability(
        turn_token,
        candidate,
        provider_key=provider_key,
        kind=RejectionCapabilityKind.SEALED,
    )
    try:
        first = lane.post_nowait(
            turn_token,
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        )
        second = lane.post_nowait(
            turn_token,
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        )
        completed = lane.post_nowait(turn_token, CaptureClosed(candidate, 2))
        first_effects = await first
        second_effects = await second
        completed_effects = await completed

        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.evidence_state.value == "deny_latched"
        assert record.admission_state is AdmissionState.DROPPED
        assert (
            sum(
                isinstance(effect, ResolveReserved)
                for effect in (*first_effects, *second_effects, *completed_effects)
            )
            == 1
        )
        assert any(
            isinstance(effect, AbortProviderTransport) for effect in second_effects
        )
        effects = await lane.post(turn_token, BoundaryExact(capability))
        assert not any(isinstance(effect, ApplyRejection) for effect in effects)
    finally:
        await lane.close()


async def test_reject_requested_survives_completion_before_ordered_seal() -> None:
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    capability = _admission_capability(
        turn_token,
        candidate,
        provider_key=provider_key,
        kind=RejectionCapabilityKind.SEALED,
    )
    try:
        await lane.post(
            turn_token,
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        )
        deny_effects = await lane.post(
            turn_token,
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        )
        await lane.post(turn_token, CaptureClosed(candidate, 2))
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.evidence_state.value == "deny_latched"
        assert record.admission_state is AdmissionState.DROPPED
        assert [
            effect.disposition
            for effect in deny_effects
            if isinstance(effect, ResolveReserved)
        ] == [AdmissionDisposition.DROP]

        effects = await lane.post(turn_token, BoundaryExact(capability))
        assert not any(isinstance(effect, ApplyRejection) for effect in effects)
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.evidence_state.value == "deny_latched"
    finally:
        await lane.close()


async def test_pending_exact_receipt_applies_before_final_deadline() -> None:
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    capability = _admission_capability(
        turn_token,
        candidate,
        provider_key=provider_key,
        kind=RejectionCapabilityKind.SEALED,
    )
    try:
        await lane.post(
            turn_token,
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        )
        final_effects = await lane.post(
            turn_token,
            ProviderFinalReceived(_admission_final(provider_key)),
            now=10.0,
        )
        deadline = next(
            effect
            for effect in final_effects
            if isinstance(effect, ScheduleFinalDeadline)
        )
        assert deadline.absolute_deadline == 10.2

        exact_effects = await lane.post(
            turn_token,
            BoundaryExact(capability),
            now=10.1,
        )
        assert not any(isinstance(effect, ApplyRejection) for effect in exact_effects)
        timeout_effects = await lane.post(
            turn_token,
            FinalDeadlineExpired(deadline.ticket, deadline.absolute_deadline),
            now=deadline.absolute_deadline,
        )
        assert not any(
            isinstance(effect, ResolveReserved) for effect in timeout_effects
        )
        pending = await coordinator.get_record(turn_token)
        assert pending is not None
        assert pending.admission_state is AdmissionState.PENDING

        denied_effects = await lane.post(
            turn_token,
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
            now=10.21,
        )
        assert [
            effect.disposition
            for effect in denied_effects
            if isinstance(effect, ResolveReserved)
        ] == [AdmissionDisposition.DROP]
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.DROPPED
    finally:
        await lane.close()


@pytest.mark.parametrize(
    ("event", "expected_split", "expected_evidence_complete"),
    [
        (SpeechActivityEvent.SPEECH_STARTED, False, True),
        (SpeechActivityEvent.SPEECH_RESUMED, False, True),
    ],
)
async def test_provider_submit_observes_admitted_audio_in_dispatch_order(
    event: SpeechActivityEvent,
    expected_split: bool,
    expected_evidence_complete: bool,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._voice_input_resource_optimization_enabled = True
    await _open_runtime_admission_turn(runtime, turn_token)
    detector_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=7,
        sequence_no=41,
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(event,),
            throttle_available=True,
            identity=detector_identity,
            candidate=DetectorCandidateKey(7, 11),
        )
    )
    runtime._bind_provider_detector_candidate = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    pcm16 = b"\x09\x00" * 160

    result = await runtime.submit(
        ProcessedVoiceFrame(pcm16, 16_000, 0.9, True),
        ingress_token=turn_token.ingress,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    assert result.status is AsrSubmitStatus.ACCEPTED
    detector.observe_provider_audio_ordered.assert_awaited_once_with(
        pcm16,
        sample_rate_hz=16_000,
        identity=detector_identity,
        sequence_no=1,
        split_before_audio=expected_split,
        evidence_complete=expected_evidence_complete,
        speaker_evidence_lease=detector._provider_evidence_lease,
    )
    detector.observe_provider_audio.assert_not_called()
    session.stream_audio.assert_awaited_once_with(pcm16, sample_rate_hz=16_000)
    await _close_dispatchers(runtime)


async def test_buffered_activation_arms_speaker_before_provider_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._asr_audio_dispatcher.abort(turn_token)
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    await runtime._asr_admission_ingress.open_turn(turn_token)
    observation_entered = asyncio.Event()
    observation_release = asyncio.Event()

    async def block_observation(*_args, **_kwargs) -> bool:
        observation_entered.set()
        await observation_release.wait()
        return True

    monkeypatch.setattr(
        runtime,
        "_observe_admitted_provider_audio",
        block_observation,
    )
    activation = asyncio.create_task(
        runtime._activate_asr_audio_dispatcher(
            lifecycle,
            turn_token,
            buffered_pcm16=b"\x01\x00" * 160,
        )
    )
    await asyncio.wait_for(observation_entered.wait(), 1)

    # Before Provider started, only a deferred PCM ledger exists; Admission
    # remains unopened while Provider wire is still fenced.
    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    assert record.candidate_binding_state.value == "unbound"
    candidate = runtime._asr_current_speaker_candidate
    lease_token = runtime._asr_current_speaker_lease
    assert candidate is not None
    assert lease_token is None
    assert runtime._asr_admission_candidate_leases == {}
    assert candidate in runtime._asr_provider_speaker_ledgers
    session = runtime._asr_session
    assert session is not None
    session.stream_audio.assert_not_awaited()

    observation_release.set()
    assert await asyncio.wait_for(activation, 1) is True
    await runtime._asr_audio_dispatcher.wait_idle()
    session.stream_audio.assert_awaited_once()
    await _drain_runtime_admission(runtime)
    await _close_dispatchers(runtime)


async def test_active_enqueue_keeps_pre_exact_lows_out_of_admission() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    provider_key = ProviderUtteranceKey(0, 0, 1)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            generation=provider_key.generation,
            buffer_epoch=provider_key.buffer_epoch,
            utterance_id=provider_key.utterance_id,
            audio_start_sample_16k=0,
        ),
        runtime._asr_session_epoch,
    )
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    assert runtime._accept_speaker_candidate_binding(
        candidate,
        turn_token,
        detector=detector,
        activation_generation="profile-generation",
    )
    await _drain_runtime_admission(runtime)
    runtime._voice_input_resource_optimization_enabled = True
    detector_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=7,
        sequence_no=81,
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.SPEECH_STARTED,),
            throttle_available=True,
            identity=detector_identity,
            candidate=DetectorCandidateKey(7, 11),
        )
    )
    runtime._bind_provider_detector_candidate = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    observation_entered = asyncio.Event()
    observation_release = asyncio.Event()
    provider_wire_seen = asyncio.Event()

    async def block_observation(*_args, **kwargs) -> ProviderSpeakerEvidenceUpdate:
        observation_entered.set()
        await observation_release.wait()
        return ProviderSpeakerEvidenceUpdate(
            lease=kwargs["speaker_evidence_lease"],
            capture=SpeakerShadowCaptureResult(
                disposition=SpeakerShadowCaptureDisposition.ACCEPTED,
                accepted_sample_count=160,
                cumulative_sample_count=160,
                completed_window_sample_count=0,
                decision_state=SpeakerShadowCaptureDecisionState.PENDING,
            ),
            sequence_no=kwargs["sequence_no"],
            last_progress_at=10.0,
        )

    async def record_provider_wire(*_args, **_kwargs) -> None:
        provider_wire_seen.set()

    detector.observe_provider_audio_ordered.side_effect = block_observation
    session.stream_audio.side_effect = record_provider_wire
    submission = asyncio.create_task(
        runtime.submit(
            ProcessedVoiceFrame(b"\x12\x00" * 160, 16_000, 0.9, True),
            ingress_token=turn_token.ingress,
        )
    )
    await asyncio.wait_for(observation_entered.wait(), 1)
    assert provider_wire_seen.is_set() is False

    final_effects = await runtime._asr_admission_ingress.post(
        turn_token,
        ProviderFinalReceived(_admission_final(provider_key)),
        now=10.0,
    )
    assert not any(isinstance(effect, ResolveReserved) for effect in final_effects)
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        activation_generation="profile-generation",
        enforce=True,
    )
    await _drain_runtime_admission(runtime)
    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    assert record.last_speaker_sequence_no == 0
    assert runtime._speaker_rejection_metrics["speaker_deny_latched_count"] == 0
    runtime._callbacks.on_final.assert_not_awaited()

    observation_release.set()
    await asyncio.wait_for(submission, 1)
    assert provider_wire_seen.is_set() is True
    session.stream_audio.assert_awaited_once()
    await _close_dispatchers(runtime)


@pytest.mark.parametrize("evidence_order", ("low_high", "high_low"))
async def test_deferred_mixed_evidence_remains_provisional_before_exact(
    evidence_order: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    lease_token = runtime._asr_current_speaker_lease
    assert lease_token is not None
    prepare = MagicMock(
        wraps=runtime._asr_admission_ingress.prepare_speaker_lease_transition_nowait
    )
    monkeypatch.setattr(
        runtime._asr_admission_ingress,
        "prepare_speaker_lease_transition_nowait",
        prepare,
    )
    committed_claims: list[SpeakerLeaseTerminalClaim] = []

    def commit_after_fence(claim: SpeakerLeaseTerminalClaim):
        assert runtime._asr_deny_transport_state is DenyTransportState.DENY_FENCED
        assert runtime._asr_session is None
        committed_claims.append(claim)
        raise AssertionError("provisional evidence cannot commit Admission")

    monkeypatch.setattr(
        runtime._asr_admission_ingress,
        "commit_speaker_lease_terminal_claim_nowait",
        commit_after_fence,
    )
    facts = (
        (
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
            SpeakerHigh(candidate, 2),
        )
        if evidence_order == "low_high"
        else (
            SpeakerHigh(candidate, 1),
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.FIRST),
        )
    )

    for fact in facts:
        assert runtime._accept_speaker_evidence_fact(
            fact,
            activation_generation="profile-generation",
            enforce=True,
        )
    await _drain_runtime_admission(runtime)

    assert prepare.call_count == 0
    assert committed_claims == []
    assert runtime._speaker_rejection_metrics["speaker_deny_latched_count"] == 0
    session.close.assert_not_awaited()
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    ledger = runtime._asr_provider_speaker_ledgers[candidate]
    assert tuple(ledger.events)
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None
    assert parent.state is SpeakerLeaseState.COLLECTING
    assert parent.last_speaker_sequence_no == 0
    await _close_dispatchers(runtime)


async def test_stale_provisional_mixed_facts_never_close_replacement_session() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    old_session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    assert runtime._asr_current_speaker_lease is not None
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerHigh(candidate, 2),
        activation_generation="profile-generation",
        enforce=True,
    )
    replacement_session = SimpleNamespace(close=AsyncMock())
    runtime._asr_session = replacement_session
    runtime._asr_session_epoch += 1
    await _drain_runtime_admission(runtime)

    replacement_session.close.assert_not_awaited()
    old_session.close.assert_not_awaited()
    assert runtime._asr_session is replacement_session
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    await _close_dispatchers(runtime)


@pytest.mark.parametrize("verdict", ("single_low_close", "unavailable"))
async def test_non_deny_terminal_speaker_evidence_still_forwards(
    verdict: str,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    key = ProviderUtteranceKey(0, 0, 1)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 1),
        runtime._asr_session_epoch,
    )
    candidate = runtime._asr_current_speaker_candidate
    lease_token = runtime._asr_current_speaker_lease
    assert candidate is not None
    assert lease_token is not None
    if verdict == "single_low_close":
        assert runtime._accept_speaker_evidence_fact(
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
            activation_generation="profile-generation",
            enforce=True,
        )
        assert runtime._close_speaker_evidence(
            CaptureClosed(candidate, 1),
            activation_generation="profile-generation",
            enforce=True,
            evidence_complete=True,
        )
    else:
        assert runtime._accept_speaker_evidence_fact(
            SpeakerUnavailable(candidate, 1),
            activation_generation="profile-generation",
            enforce=True,
        )
    await _drain_runtime_admission(runtime)

    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None
    assert parent.state is SpeakerLeaseState.UNAVAILABLE
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    session.close.assert_not_awaited()
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="ordered",
            generation=key.generation,
            buffer_epoch=key.buffer_epoch,
            utterance_id=key.utterance_id,
            boundary_quality="unknown",
            audio_range=None,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_final(
        key,
        "forwarded",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_final.await_args.args[0].text == "forwarded"
    await _close_dispatchers(runtime)


async def test_observation_without_candidate_unarms_pending_authority() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="glm",
        endpointing_mode="manual",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    await runtime._asr_admission_ingress.open_turn(turn_token)
    owner_generation = await runtime._arm_speaker_authority_for_provider_audio(
        turn_token
    )
    assert owner_generation.owner_generation == "profile-generation"

    assert await runtime._observe_admitted_provider_audio(
        lifecycle,
        detector,
        b"\x13\x00" * 160,
        sample_rate_hz=16_000,
        identity=DetectorIngressIdentity(
            ingress_token=turn_token.ingress,
            detector_epoch=7,
            sequence_no=82,
        ),
        split_before_audio=False,
        evidence_complete=True,
        turn_token=turn_token,
    )
    await _drain_runtime_admission(runtime)

    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    assert record.candidate_binding_state.value == "retired"
    assert record.evidence_state.value == "unavailable"
    assert turn_token not in runtime._asr_speaker_authority_pending_turns
    await _close_dispatchers(runtime)


async def test_arm_failure_never_wires_buffered_provider_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="glm",
        endpointing_mode="manual",
    )
    runtime._asr_audio_dispatcher.abort(turn_token)
    await _open_runtime_admission_turn(runtime, turn_token)
    original_post = runtime._post_admission_event

    async def fail_pending(token, event, *, now=None):
        if isinstance(event, runtime_module.SpeakerAuthorityPending):
            raise KeyError(token)
        return await original_post(token, event, now=now)

    monkeypatch.setattr(runtime, "_post_admission_event", fail_pending)

    assert not await runtime._activate_asr_audio_dispatcher(
        lifecycle,
        turn_token,
        buffered_pcm16=b"\x14\x00" * 160,
    )
    session.stream_audio.assert_not_awaited()
    assert runtime._asr_audio_dispatcher.active_turn is None
    await _close_dispatchers(runtime)


async def test_arm_post_await_identity_drift_never_wires_provider_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="glm",
        endpointing_mode="manual",
    )
    runtime._asr_audio_dispatcher.abort(turn_token)
    await _open_runtime_admission_turn(runtime, turn_token)
    original_post = runtime._post_admission_event
    pending_reduced = asyncio.Event()
    release_post = asyncio.Event()

    async def block_after_pending(token, event, *, now=None):
        effects = await original_post(token, event, now=now)
        if isinstance(event, runtime_module.SpeakerAuthorityPending):
            pending_reduced.set()
            await release_post.wait()
        return effects

    monkeypatch.setattr(runtime, "_post_admission_event", block_after_pending)
    activation = asyncio.create_task(
        runtime._activate_asr_audio_dispatcher(
            lifecycle,
            turn_token,
            buffered_pcm16=b"\x16\x00" * 160,
        )
    )
    await asyncio.wait_for(pending_reduced.wait(), 1)
    runtime._asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._speaker_verifier_enforces_admission = False
    release_post.set()

    assert not await asyncio.wait_for(activation, 1)
    session.stream_audio.assert_not_awaited()
    assert runtime._asr_audio_dispatcher.active_turn is None
    await _close_dispatchers(runtime)


async def test_cancelled_observation_still_unarms_pending_authority() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    observation_entered = asyncio.Event()

    async def block_observation(*_args, **_kwargs) -> None:
        observation_entered.set()
        await asyncio.Event().wait()

    detector.observe_provider_audio_ordered.side_effect = block_observation
    observation = asyncio.create_task(
        runtime._observe_admitted_provider_audio(
            lifecycle,
            detector,
            b"\x15\x00" * 160,
            sample_rate_hz=16_000,
            identity=DetectorIngressIdentity(
                ingress_token=turn_token.ingress,
                detector_epoch=7,
                sequence_no=83,
            ),
            split_before_audio=False,
            evidence_complete=True,
            turn_token=turn_token,
        )
    )
    await asyncio.wait_for(observation_entered.wait(), 1)
    observation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observation
    await _drain_runtime_admission(runtime)

    record = await runtime._asr_admission.get_record(turn_token)
    # Cancellation cannot establish whether the ordered observer committed.
    # Retire the physical timeline along with its pending authority.
    assert record is None
    assert runtime._asr_session is None
    assert turn_token not in runtime._asr_speaker_authority_pending_turns
    await _close_dispatchers(runtime)


async def test_continuous_provider_frames_reuse_one_settled_parent_lease() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    original_ensure = detector.ensure_provider_speaker_evidence_lease
    detector.ensure_provider_speaker_evidence_lease = AsyncMock(
        side_effect=original_ensure
    )
    detector.feed = AsyncMock(
        side_effect=(
            DetectorFeedResult(
                events=(),
                throttle_available=True,
                identity=DetectorIngressIdentity(turn_token.ingress, 7, 101),
                candidate=DetectorCandidateKey(7, 11),
            ),
            DetectorFeedResult(
                events=(),
                throttle_available=True,
                identity=DetectorIngressIdentity(turn_token.ingress, 7, 102),
                candidate=DetectorCandidateKey(7, 11),
            ),
        )
    )
    runtime._bind_provider_detector_candidate = AsyncMock(return_value=True)

    first = await runtime.submit(
        ProcessedVoiceFrame(b"\x21\x00" * 160, 16_000, 0.9, True),
        ingress_token=turn_token.ingress,
    )
    evidence_lease = runtime._asr_provider_speaker_evidence_lease
    lease_token = runtime._asr_current_speaker_lease
    second = await runtime.submit(
        ProcessedVoiceFrame(b"\x22\x00" * 160, 16_000, 0.9, True),
        ingress_token=turn_token.ingress,
    )

    assert first.status is second.status is AsrSubmitStatus.ACCEPTED
    assert detector.ensure_provider_speaker_evidence_lease.await_count == 1
    assert runtime._asr_provider_speaker_evidence_lease == evidence_lease
    assert runtime._asr_current_speaker_lease == lease_token
    # Provider PCM is buffered under a deferred speaker ledger until the
    # canonical Provider start establishes the candidate's true origin.
    # Admission must therefore remain unopened before started settlement.
    assert runtime._asr_speaker_lease_nonce == 0
    assert runtime._asr_current_speaker_lease is None
    assert len(runtime._asr_provider_speaker_ledgers) == 1
    detector.abandon_provider_speaker_evidence_lease.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_cancelled_parent_open_retires_late_admission_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    open_entered = asyncio.Event()
    open_release = asyncio.Event()
    original_open = runtime._asr_admission.open_speaker_lease

    async def block_parent_open(lease_token, candidate):
        open_entered.set()
        await open_release.wait()
        return await original_open(lease_token, candidate)

    monkeypatch.setattr(runtime._asr_admission, "open_speaker_lease", block_parent_open)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    started = asyncio.create_task(
        runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(
                0,
                0,
                1,
                audio_start_sample_16k=0,
            ),
            runtime._asr_session_epoch,
        )
    )
    await asyncio.wait_for(open_entered.wait(), 1)
    started.cancel()
    open_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(started, 1)
    lease_token = runtime_module.SpeakerCaptureLeaseToken(
        session_generation=runtime._asr_session_epoch,
        start_generation=runtime._asr_start_generation,
        transport_generation=lifecycle.snapshot.transport_generation,
        detector_epoch=detector.detector_epoch,
        lease_nonce=1,
    )
    assert await runtime._asr_admission.get_speaker_lease(lease_token) is None
    assert runtime._asr_admission_candidate_leases == {}
    assert runtime._asr_current_speaker_lease is None
    assert detector._provider_evidence_lease is not None
    await _close_dispatchers(runtime)


async def test_provider_started_commits_aliases_only_after_attach_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    attach_entered = asyncio.Event()
    attach_release = asyncio.Event()
    original_attach = runtime._asr_admission.attach_turn_to_speaker_lease

    async def block_attach(child_turn, lease_token, provider_key):
        attach_entered.set()
        await attach_release.wait()
        return await original_attach(child_turn, lease_token, provider_key)

    monkeypatch.setattr(
        runtime._asr_admission,
        "attach_turn_to_speaker_lease",
        block_attach,
    )
    key = ProviderUtteranceKey(0, 0, 1)
    started = asyncio.create_task(
        runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(0, 0, 1),
            runtime._asr_session_epoch,
        )
    )
    await asyncio.wait_for(attach_entered.wait(), 1)

    assert runtime._asr_provider_started_turns == {}
    assert runtime._asr_admission_turn_leases == {}
    assert runtime._asr_provider_correlator is not None
    reservation = runtime._asr_provider_correlator.record_for(key)
    assert reservation is not None
    assert reservation.bound_turn_token == turn_token

    attach_release.set()
    assert await asyncio.wait_for(started, 1) is True
    assert runtime._asr_provider_started_turns[key] == turn_token
    assert turn_token in runtime._asr_admission_turn_leases
    alias = runtime._asr_provider_correlator.record_for(key)
    assert alias is not None and alias.bound_turn_token == turn_token
    await _close_dispatchers(runtime)


async def test_provider_started_gate_active_never_reserves_child() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, _turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._asr_deny_cleanup_generation += 1
    runtime._asr_deny_transport_state = DenyTransportState.DENY_FENCED
    dispatcher = runtime._asr_transcript_dispatcher
    reservations_before = set(dispatcher._reservations)

    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 1),
        runtime._asr_session_epoch,
    )

    assert set(dispatcher._reservations) == reservations_before
    assert runtime._asr_provider_turn_ownerships == {}
    assert runtime._asr_provider_started_turns == {}
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_legacy_deferred_flush_hook_cannot_break_started_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    dispatcher = runtime._asr_transcript_dispatcher
    dispatcher.release = MagicMock(wraps=dispatcher.release)
    dispatcher.resolve_reserved = MagicMock(wraps=dispatcher.resolve_reserved)

    async def fail_after_publication(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(
        runtime,
        "_flush_deferred_provider_speaker_lease_events",
        fail_after_publication,
    )

    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    await _drain_runtime_admission(runtime)

    final_key = FinalKey.from_turn(turn_token)
    dispatcher.release.assert_not_called()
    dispatcher.resolve_reserved.assert_not_called()
    assert final_key in dispatcher._reservations
    assert turn_token in runtime._asr_provider_turn_ownerships
    assert runtime._asr_admission_reservation_dispatchers[final_key] is dispatcher
    assert runtime._asr_provider_started_turns == {
        ProviderUtteranceKey(0, 0, 1): turn_token
    }
    await _close_dispatchers(runtime)


async def test_provider_started_keeps_pre_exact_lows_provisional() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    assert runtime._asr_current_speaker_lease is None
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        activation_generation="profile-generation",
        enforce=True,
    )
    dispatcher = runtime._asr_transcript_dispatcher
    dispatcher.resolve_reserved = MagicMock(wraps=dispatcher.resolve_reserved)

    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    await _drain_runtime_admission(runtime)
    lease_token = runtime._asr_current_speaker_lease
    assert lease_token is not None
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None
    assert parent.state is SpeakerLeaseState.COLLECTING
    assert parent.last_speaker_sequence_no == 0
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN

    key = ProviderUtteranceKey(0, 0, 1)
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="ordered",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="unknown",
            audio_range=None,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_final(
        key,
        "provisional-lows-forward",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    session.close.assert_not_awaited()
    assert callbacks.on_final.await_args.args[0].text == "provisional-lows-forward"
    assert not any(
        item.args[1] is AdmissionDisposition.DROP
        for item in dispatcher.resolve_reserved.call_args_list
    )
    await _close_dispatchers(runtime)


async def test_active_deny_cleanup_callback_only_handoffs_drop_ownership() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    lease_token = runtime._asr_current_speaker_lease
    candidate = runtime._asr_current_speaker_candidate
    assert lease_token is not None
    assert candidate is not None
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    ownership = runtime._asr_provider_turn_ownerships[turn_token]
    dispatcher = ownership.transcript_dispatcher
    dispatcher.resolve_reserved = MagicMock(wraps=dispatcher.resolve_reserved)
    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    ticket = AdmissionResolutionTicket(
        turn_token=turn_token,
        record_generation=record.record_generation,
        resolution_nonce=1,
        disposition=AdmissionDisposition.DROP,
    )
    cleanup = runtime._begin_speaker_deny_cleanup(
        lease_token,
        (AbortProviderTransport(ticket=ticket),),
        candidate_key=candidate,
        terminal_sequence=1,
    )

    await asyncio.wait_for(
        runtime._settle_published_provider_turn_ownership(
            ownership,
            denied=True,
        ),
        0.1,
    )

    dispatcher.resolve_reserved.assert_not_called()
    assert runtime._asr_provider_turn_ownerships[turn_token] is ownership
    assert runtime._asr_admission_reservation_dispatchers[ownership.final_key] is (
        dispatcher
    )
    assert cleanup.provisional_reservations == {ownership.final_key: dispatcher}
    cleanup.settled.set()
    runtime._asr_speaker_deny_cleanups.clear()
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_active_deny_cleanup_effect_without_ownership_only_handoffs() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    lease_token = runtime._asr_current_speaker_lease
    candidate = runtime._asr_current_speaker_candidate
    assert lease_token is not None
    assert candidate is not None
    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    ticket = AdmissionResolutionTicket(
        turn_token=turn_token,
        record_generation=record.record_generation,
        resolution_nonce=1,
        disposition=AdmissionDisposition.DROP,
    )
    final_key = FinalKey.from_turn(turn_token)
    dispatcher = MagicMock()
    runtime._asr_provider_turn_ownerships.pop(turn_token, None)
    runtime._asr_admission_reservation_dispatchers[final_key] = dispatcher
    cleanup = runtime._begin_speaker_deny_cleanup(
        lease_token,
        (AbortProviderTransport(ticket=ticket),),
        candidate_key=candidate,
        terminal_sequence=1,
    )

    await runtime._resolve_admission_reservation(
        ResolveReserved(ticket=ticket, final=None)
    )

    dispatcher.resolve_reserved.assert_not_called()
    assert cleanup.provisional_reservations == {final_key: dispatcher}
    assert runtime._asr_admission_reservation_dispatchers[final_key] is dispatcher
    execution = runtime._asr_admission_resolutions[final_key]
    assert execution.owner_done is False
    cleanup.settled.set()
    runtime._asr_speaker_deny_cleanups.clear()
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_active_deny_cleanup_rejects_replacement_dispatcher() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    lease_token = runtime._asr_current_speaker_lease
    candidate = runtime._asr_current_speaker_candidate
    assert lease_token is not None
    assert candidate is not None
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    ownership = runtime._asr_provider_turn_ownerships[turn_token]
    dispatcher = ownership.transcript_dispatcher
    dispatcher.resolve_reserved = MagicMock(wraps=dispatcher.resolve_reserved)
    cleanup = runtime._begin_speaker_deny_cleanup(
        lease_token,
        (),
        candidate_key=candidate,
        terminal_sequence=1,
    )
    replacement = MagicMock()
    runtime._asr_admission_reservation_dispatchers[ownership.final_key] = replacement

    await runtime._settle_published_provider_turn_ownership(
        ownership,
        denied=True,
    )

    dispatcher.resolve_reserved.assert_not_called()
    replacement.resolve_reserved.assert_not_called()
    assert cleanup.failure_reason == "ASR_DENY_CLEANUP_TRANSCRIPT_CONFLICT"
    assert runtime._asr_deny_transport_state is DenyTransportState.QUARANTINED
    assert runtime._asr_provider_turn_ownerships[turn_token] is ownership
    cleanup.settled.set()
    runtime._asr_speaker_deny_cleanups.clear()
    runtime._asr_admission_reservation_dispatchers[ownership.final_key] = dispatcher
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


@pytest.mark.parametrize(
    ("outcome", "existing", "expected_success"),
    (
        (
            TranscriptResolutionOutcome.ALREADY_SAME,
            AdmissionDisposition.DROP,
            True,
        ),
        (TranscriptResolutionOutcome.NOT_RESERVED, None, False),
    ),
)
async def test_cleanup_owner_accepts_only_exact_drop_tombstone(
    outcome: TranscriptResolutionOutcome,
    existing: AdmissionDisposition | None,
    expected_success: bool,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    lease_token = runtime._asr_current_speaker_lease
    candidate = runtime._asr_current_speaker_candidate
    assert lease_token is not None
    assert candidate is not None
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    ownership = runtime._asr_provider_turn_ownerships[turn_token]
    final_key = ownership.final_key
    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    ticket = AdmissionResolutionTicket(
        turn_token=turn_token,
        record_generation=record.record_generation,
        resolution_nonce=1,
        disposition=AdmissionDisposition.DROP,
    )
    cleanup = runtime._begin_speaker_deny_cleanup(
        lease_token,
        (AbortProviderTransport(ticket=ticket),),
        candidate_key=candidate,
        terminal_sequence=1,
    )
    cleanup.provisional_reservations[final_key] = ownership.transcript_dispatcher
    late_context = SimpleNamespace(settled=asyncio.Event())
    runtime._settle_admission_final = AsyncMock()
    execution = runtime_module._AdmissionResolutionExecution(
        ticket,
        core_settled=True,
        owner_done=True,
        late_context=late_context,
    )
    runtime._asr_admission_resolutions[final_key] = execution
    ownership.transcript_dispatcher.resolve_reserved = MagicMock(
        return_value=TranscriptResolutionReceipt(
            final_key,
            AdmissionDisposition.DROP,
            outcome,
            existing,
        )
    )

    await runtime._resolve_admission_reservation(
        ResolveReserved(ticket=ticket, final=None),
        cleanup_owner=cleanup,
    )

    assert execution.core_resolution_succeeded is expected_success
    assert execution.owner_done is True
    assert late_context.settled.is_set()
    if expected_success:
        runtime._settle_admission_final.assert_awaited_once_with(
            ticket,
            late_context,
        )
    else:
        runtime._settle_admission_final.assert_not_awaited()
    assert runtime._asr_admission_resolutions[final_key] is execution
    assert runtime._asr_provider_turn_ownerships[turn_token] is ownership
    assert runtime._asr_admission_reservation_dispatchers[final_key] is (
        ownership.transcript_dispatcher
    )
    cleanup.settled.set()
    runtime._asr_speaker_deny_cleanups.clear()
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_provider_started_gate_change_drops_late_terminal_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    lease_token = SpeakerCaptureLeaseToken(
        session_generation=runtime._asr_session_epoch,
        start_generation=runtime._asr_start_generation,
        transport_generation=runtime._asr_lifecycle.snapshot.transport_generation,
        detector_epoch=detector.detector_epoch,
        lease_nonce=1,
    )
    attach_entered = asyncio.Event()
    attach_release = asyncio.Event()
    original_attach = runtime._asr_admission.attach_turn_to_speaker_lease

    async def block_attach(child_turn, parent_lease, provider_key):
        attach_entered.set()
        await attach_release.wait()
        return await original_attach(child_turn, parent_lease, provider_key)

    monkeypatch.setattr(
        runtime._asr_admission,
        "attach_turn_to_speaker_lease",
        block_attach,
    )
    started = asyncio.create_task(
        runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(
                0, 0, 1, audio_start_sample_16k=0
            ),
            runtime._asr_session_epoch,
        )
    )
    await asyncio.wait_for(attach_entered.wait(), 1)
    await runtime._asr_admission.post_speaker_lease(
        lease_token,
        SpeakerLeaseLow(candidate, 1, SpeakerCheckpointKind.FIRST),
    )
    deny_receipt = await runtime._asr_admission.post_speaker_lease(
        lease_token,
        SpeakerLeaseLow(candidate, 2, SpeakerCheckpointKind.SECOND),
    )
    runtime._begin_speaker_deny_cleanup(
        lease_token,
        (),
        candidate_key=candidate,
        terminal_sequence=2,
    )
    await runtime._apply_speaker_lease_result(lease_token, deny_receipt)
    attach_release.set()

    assert not await asyncio.wait_for(started, 1)
    await _drain_runtime_admission(runtime)

    final_key = FinalKey.from_turn(turn_token)
    assert final_key not in runtime._asr_transcript_dispatcher._reservations
    assert runtime._asr_provider_turn_ownerships == {}
    assert runtime._asr_admission_reservation_dispatchers == {}
    assert runtime._asr_speaker_deny_cleanups == {}
    assert runtime._asr_deny_cleanup_active is False
    session.close.assert_awaited_once_with()
    await _close_dispatchers(runtime)


async def test_pre_exact_lows_never_enter_deny_cleanup_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        activation_generation="profile-generation",
        enforce=True,
    )
    original_transition = lifecycle.transition

    def fail_deny_transition(event):
        if event is VoiceLifecycleEvent.TURN_DENIED:
            raise RuntimeError("forced lifecycle cleanup failure")
        return original_transition(event)

    monkeypatch.setattr(lifecycle, "transition", fail_deny_transition)

    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    await _drain_runtime_admission(runtime)

    session.close.assert_not_awaited()
    assert runtime._asr_speaker_deny_cleanups == {}
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    runtime._callbacks.on_status.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_pre_exact_lows_never_attempt_drop_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        activation_generation="profile-generation",
        enforce=True,
    )
    dispatcher = runtime._asr_transcript_dispatcher
    original_resolve = dispatcher.resolve_reserved

    def fail_drop(final_key, disposition, **kwargs):
        if disposition is AdmissionDisposition.DROP:
            raise RuntimeError("forced transcript settlement failure")
        return original_resolve(final_key, disposition, **kwargs)

    monkeypatch.setattr(dispatcher, "resolve_reserved", fail_drop)

    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    await _drain_runtime_admission(runtime)

    final_key = FinalKey.from_turn(turn_token)
    session.close.assert_not_awaited()
    assert final_key in dispatcher._reservations
    assert final_key not in dispatcher._resolved
    assert runtime._asr_speaker_deny_cleanups == {}
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    assert runtime._asr_deny_cleanup_active is False
    runtime._callbacks.on_status.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_provider_started_identity_drift_detaches_exact_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    lease_token = SpeakerCaptureLeaseToken(
        session_generation=runtime._asr_session_epoch,
        start_generation=runtime._asr_start_generation,
        transport_generation=runtime._asr_lifecycle.snapshot.transport_generation,
        detector_epoch=detector.detector_epoch,
        lease_nonce=1,
    )
    attach = runtime._asr_admission.attach_turn_to_speaker_lease

    async def attach_then_drift(child_turn, parent_lease, provider_key):
        record = await attach(child_turn, parent_lease, provider_key)
        runtime._asr_audio_generation += 1
        return record

    monkeypatch.setattr(
        runtime._asr_admission,
        "attach_turn_to_speaker_lease",
        attach_then_drift,
    )
    key = ProviderUtteranceKey(0, 0, 1)

    assert not await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0,
            0,
            1,
            audio_start_sample_16k=0,
        ),
        runtime._asr_session_epoch,
    )

    assert await runtime._asr_admission.get_record(turn_token) is None
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None and not parent.child_bindings
    assert runtime._asr_admission_turn_leases == {}
    assert runtime._asr_provider_started_turns == {}
    assert runtime._asr_provider_correlator is not None
    assert runtime._asr_provider_correlator.record_for(key) is None
    await _close_dispatchers(runtime)


async def test_cancelled_provider_started_detaches_late_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    lease_token = SpeakerCaptureLeaseToken(
        session_generation=runtime._asr_session_epoch,
        start_generation=runtime._asr_start_generation,
        transport_generation=runtime._asr_lifecycle.snapshot.transport_generation,
        detector_epoch=detector.detector_epoch,
        lease_nonce=1,
    )
    attach = runtime._asr_admission.attach_turn_to_speaker_lease
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_attach(child_turn, parent_lease, provider_key):
        entered.set()
        await release.wait()
        return await attach(child_turn, parent_lease, provider_key)

    monkeypatch.setattr(
        runtime._asr_admission,
        "attach_turn_to_speaker_lease",
        blocked_attach,
    )
    key = ProviderUtteranceKey(0, 0, 1)
    started = asyncio.create_task(
        runtime._handle_provider_utterance_started(
            ProviderUtteranceStartedNotification(
                0,
                0,
                1,
                audio_start_sample_16k=0,
            ),
            runtime._asr_session_epoch,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    started.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(started, 1)

    assert await runtime._asr_admission.get_record(turn_token) is None
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None and not parent.child_bindings
    assert runtime._asr_admission_turn_leases == {}
    assert runtime._asr_provider_started_turns == {}
    assert runtime._asr_provider_correlator is not None
    assert runtime._asr_provider_correlator.record_for(key) is None
    await _close_dispatchers(runtime)


async def test_provider_started_alias_conflict_never_attaches_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    key = ProviderUtteranceKey(0, 0, 1)
    assert runtime._accept_provider_timeline(key)
    correlator = runtime._asr_provider_correlator
    assert correlator is not None

    def reject_alias(_key, _turn_token):
        raise runtime_module.ProviderAliasConflictError("forced conflict")

    monkeypatch.setattr(correlator, "bind_ordered", reject_alias)

    assert not await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0,
            0,
            1,
            audio_start_sample_16k=0,
        ),
        runtime._asr_session_epoch,
    )

    assert await runtime._asr_admission.get_record(turn_token) is None
    assert runtime._asr_current_speaker_lease is None
    assert runtime._asr_admission_turn_leases == {}
    assert runtime._asr_provider_started_turns == {}
    assert correlator.record_for(key) is None
    await _close_dispatchers(runtime)


async def test_duplicate_same_provider_started_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    notification = ProviderUtteranceStartedNotification(
        0, 0, 1, audio_start_sample_16k=0
    )
    assert await runtime._handle_provider_utterance_started(
        notification,
        runtime._asr_session_epoch,
    )
    monkeypatch.setattr(
        runtime._asr_admission,
        "get_record",
        AsyncMock(return_value=None),
    )

    assert await runtime._handle_provider_utterance_started(
        notification,
        runtime._asr_session_epoch,
    )
    await _close_dispatchers(runtime)


async def test_occupied_provider_started_fails_without_deferred_child_leak() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    key_a = ProviderUtteranceKey(0, 0, 1)
    key_b = ProviderUtteranceKey(0, 0, 2)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 1),
        runtime._asr_session_epoch,
    )

    assert not await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 2),
        runtime._asr_session_epoch,
    )

    assert not runtime._asr_deferred_provider_started_keys
    assert runtime._asr_provider_started_turns == {key_a: turn_token}
    assert runtime._asr_provider_correlator is not None
    assert runtime._asr_provider_correlator.record_for(key_b) is None
    lease_token = runtime._asr_current_speaker_lease
    assert lease_token is not None
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None
    assert [child.provider_key for child in parent.child_bindings] == [key_a]
    await _close_dispatchers(runtime)


async def test_provisional_ledger_overflow_fails_open_with_bound_child() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    for sequence_no in range(
        1,
        runtime_module._MAX_PROVIDER_PROVISIONAL_SPEAKER_EVENTS + 1,
    ):
        assert runtime._accept_speaker_evidence_fact(
            SpeakerLow(candidate, sequence_no, SpeakerCheckpointKind.FIRST),
            activation_generation="profile-generation",
            enforce=True,
        )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(
            candidate,
            runtime_module._MAX_PROVIDER_PROVISIONAL_SPEAKER_EVENTS + 1,
            SpeakerCheckpointKind.FIRST,
        ),
        activation_generation="profile-generation",
        enforce=True,
    )

    ledger = runtime._asr_provider_speaker_ledgers[candidate]
    assert ledger.poisoned_reason == "ledger_capacity"
    lease_token = runtime._asr_current_speaker_lease
    assert lease_token is not None
    assert await runtime._publish_provider_ledger_unavailable(ledger)
    assert await runtime._asr_admission.get_record(turn_token) is not None
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None and parent.state is SpeakerLeaseState.UNAVAILABLE
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["speaker_ledger_poisoned_reason_overflow_count"] == 1
    assert diagnostics["speaker_unavailable_reason_overflow_count"] == 1
    await _close_dispatchers(runtime)


@pytest.mark.parametrize(
    ("reason", "expected_category"),
    (
        ("provider_pcm_sequence_gap", "gap"),
        ("ledger_capacity", "overflow"),
        ("anchor_unavailable", "anchor"),
        ("exact_prepare_unavailable", "prepare"),
        ("turn_identity_conflict", "identity"),
        ("speaker_sequence_reorder", "sequence"),
        ("speaker_evidence_unavailable", "proof"),
    ),
)
async def test_provider_ledger_poison_diagnostics_use_bounded_reason_codes(
    reason: str,
    expected_category: str,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    ledger = runtime._asr_provider_speaker_ledgers[candidate]

    runtime._poison_provider_speaker_ledger(ledger, reason)

    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["speaker_ledger_poisoned_count"] == 1
    assert diagnostics[
        f"speaker_ledger_poisoned_reason_{expected_category}_count"
    ] == 1
    assert diagnostics[
        f"speaker_unavailable_reason_{expected_category}_count"
    ] == 1
    assert sum(
        diagnostics[f"speaker_ledger_poisoned_reason_{category}_count"]
        for category in runtime_module._SPEAKER_FAILURE_REASON_CATEGORIES
    ) == 1
    await _close_dispatchers(runtime)


@pytest.mark.parametrize("verdict", ("high", "unavailable"))
async def test_parent_provisional_after_started_forwards_on_unknown_boundary(
    verdict: str,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._voice_input_resource_optimization_enabled = True
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    runtime._asr_admission_reservation_dispatchers[FinalKey.from_turn(turn_token)] = (
        runtime._asr_transcript_dispatcher
    )
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    evidence_lease = runtime._asr_provider_speaker_evidence_lease
    assert evidence_lease is not None
    key = ProviderUtteranceKey(0, 0, 1)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    identities = tuple(
        DetectorIngressIdentity(
            ingress_token=turn_token.ingress,
            detector_epoch=detector.detector_epoch,
            sequence_no=sequence_no,
        )
        for sequence_no in (91, 92)
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        side_effect=[
            DetectorFeedResult(
                events=(SpeechActivityEvent.SPEECH_STARTED,),
                throttle_available=True,
                identity=identities[0],
                candidate=DetectorCandidateKey(7, 11),
            ),
            DetectorFeedResult(
                events=(),
                throttle_available=True,
                identity=identities[1],
                candidate=DetectorCandidateKey(7, 11),
            ),
        ]
    )
    runtime._bind_provider_detector_candidate = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )

    async def publish_terminal_update(pcm16: bytes, **kwargs):
        if kwargs.get("accounting_only"):
            return await detector._observe_provider_audio_ordered(pcm16, **kwargs)
        lease = kwargs["speaker_evidence_lease"]
        candidate = lease.candidate
        sequence_no = kwargs["sequence_no"]
        fact = (
            SpeakerHigh(candidate, sequence_no)
            if verdict == "high"
            else SpeakerUnavailable(candidate, sequence_no)
        )
        assert runtime._accept_speaker_evidence_fact(
            fact,
            activation_generation="profile-generation",
            enforce=True,
        )
        samples = len(pcm16) // 2
        unavailable = verdict == "unavailable"
        return ProviderSpeakerEvidenceUpdate(
            lease=lease,
            capture=SpeakerShadowCaptureResult(
                disposition=(
                    SpeakerShadowCaptureDisposition.UNAVAILABLE
                    if unavailable
                    else SpeakerShadowCaptureDisposition.ACCEPTED
                ),
                accepted_sample_count=0 if unavailable else samples,
                cumulative_sample_count=0 if unavailable else samples,
                completed_window_sample_count=0,
                decision_state=(
                    SpeakerShadowCaptureDecisionState.UNAVAILABLE
                    if unavailable
                    else SpeakerShadowCaptureDecisionState.PENDING
                ),
            ),
            sequence_no=sequence_no,
            last_progress_at=10.0,
        )

    detector.observe_provider_audio_ordered.side_effect = publish_terminal_update
    for value in (21, 22):
        result = await runtime.submit(
            ProcessedVoiceFrame(bytes((value, 0)) * 160, 16_000, 0.9, True),
            ingress_token=turn_token.ingress,
        )
        assert result.status is AsrSubmitStatus.ACCEPTED
        await runtime._asr_audio_dispatcher.wait_idle()

    if verdict == "high":
        assert runtime._asr_provider_speaker_evidence_lease is evidence_lease
        # HIGH_SEEN is non-terminal: the next frame must still reach the
        # observer, and capture close is what makes the lease ALLOW.
        assert detector.observe_provider_audio_ordered.await_count == 2
        assert runtime._close_speaker_evidence(
            CaptureClosed(evidence_lease.candidate, 2),
            activation_generation="profile-generation",
            enforce=True,
            evidence_complete=True,
        )
        await _drain_runtime_admission(runtime)
    else:
        assert runtime._asr_provider_speaker_evidence_lease is None
        assert runtime._asr_current_speaker_candidate is None
        assert detector.observe_provider_audio_ordered.await_count == 2
        assert detector.observe_provider_audio_ordered.await_args.kwargs["accounting_only"]
    assert session.stream_audio.await_count == 2
    lease_token = runtime._asr_admission_turn_leases.get(turn_token)
    assert lease_token is not None
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None and parent.state is SpeakerLeaseState.COLLECTING
    assert parent.last_speaker_sequence_no == 0
    assert parent.child_bindings[0].provider_key == key

    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="ordered",
            generation=key.generation,
            buffer_epoch=key.buffer_epoch,
            utterance_id=key.utterance_id,
            boundary_quality="unknown",
            audio_range=None,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_final(
        key,
        "forwarded",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_final.await_args.args[0].text == "forwarded"
    assert runtime._asr_provider_turn_ownerships == {}
    assert runtime._asr_admission_reservation_dispatchers == {}
    await _close_dispatchers(runtime)


async def test_unleased_drop_retires_provider_turn_ownership() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._speaker_verifier_enforces_admission = False
    runtime._asr_current_speaker_lease = None
    runtime._asr_current_speaker_candidate = None
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 1),
        runtime._asr_session_epoch,
    )
    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    ticket = AdmissionResolutionTicket(
        turn_token=turn_token,
        record_generation=record.record_generation,
        resolution_nonce=1,
        disposition=AdmissionDisposition.DROP,
    )

    await runtime._resolve_admission_reservation(
        ResolveReserved(ticket=ticket, final=None)
    )
    assert turn_token in runtime._asr_provider_turn_ownerships
    await runtime._abort_admission_transport(
        AbortProviderTransport(ticket=ticket)
    )

    assert runtime._asr_provider_turn_ownerships == {}
    assert runtime._asr_admission_reservation_dispatchers == {}
    await _close_dispatchers(runtime)


async def test_enforced_unbound_provider_final_fails_closed() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token, _provider_fence = (
        await _seal_provider_candidate(runtime, detector)
    )
    key = ProviderUtteranceKey(0, 0, 1)
    assert runtime._accept_provider_timeline(key)
    runtime._asr_sealed_provider_key = key

    settled = await runtime._handle_independent_asr_final(
        "must-not-forward",
        runtime._asr_session_epoch,
        "qwen",
        provider_key=key,
    )

    assert settled is not None and settled.is_set()
    callbacks.on_final.assert_not_awaited()
    assert (
        runtime._speaker_rejection_metrics[
            "provider_candidate_bind_identity_rejected_count"
        ]
        == 1
    )
    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    assert record.terminal_disposition is AdmissionDisposition.ABANDON
    await _close_dispatchers(runtime)


async def test_unbound_final_cleanup_cannot_remove_replacement_alias() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token, _provider_fence = (
        await _seal_provider_candidate(runtime, detector)
    )
    key = ProviderUtteranceKey(0, 0, 1)
    assert runtime._accept_provider_timeline(key)
    runtime._asr_sealed_provider_key = key
    old_correlator = runtime._asr_provider_correlator
    assert old_correlator is not None
    replacement = type(old_correlator)(namespace=(0, 0))
    replacement_turn = VoiceTurnToken(turn_token.ingress, turn_token.turn_id + 1)
    replacement.mark_ordered(key)
    replacement.bind_ordered(key, replacement_turn)
    post_event = runtime._post_admission_event

    async def replace_alias_after_reset(token, event, **kwargs):
        result = await post_event(token, event, **kwargs)
        if isinstance(event, runtime_module.Reset):
            runtime._asr_provider_correlator = replacement
            runtime._asr_provider_started_turns[key] = replacement_turn
        return result

    runtime._post_admission_event = replace_alias_after_reset  # type: ignore[method-assign]

    settled = await runtime._handle_independent_asr_final(
        "must-not-forward",
        runtime._asr_session_epoch,
        "qwen",
        provider_key=key,
    )

    assert settled is not None and settled.is_set()
    callbacks.on_final.assert_not_awaited()
    assert runtime._asr_provider_started_turns[key] == replacement_turn
    assert replacement.record_for(key) is not None
    assert not replacement.is_completed(key)
    await _close_dispatchers(runtime)


async def test_unbound_final_cleanup_rejects_preexisting_replacement_alias() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token, _provider_fence = (
        await _seal_provider_candidate(runtime, detector)
    )
    key = ProviderUtteranceKey(0, 0, 1)
    assert runtime._accept_provider_timeline(key)
    runtime._asr_sealed_provider_key = key
    correlator = runtime._asr_provider_correlator
    assert correlator is not None
    replacement_turn = VoiceTurnToken(turn_token.ingress, turn_token.turn_id + 1)
    correlator.mark_ordered(key)
    correlator.bind_ordered(key, replacement_turn)
    runtime._asr_provider_started_turns[key] = replacement_turn

    settled = await runtime._handle_independent_asr_final(
        "must-not-forward",
        runtime._asr_session_epoch,
        "qwen",
        provider_key=key,
    )

    assert settled is not None and settled.is_set()
    callbacks.on_final.assert_not_awaited()
    assert runtime._asr_provider_started_turns[key] == replacement_turn
    record = correlator.record_for(key)
    assert record is not None and record.bound_turn_token == replacement_turn
    await _close_dispatchers(runtime)


async def test_hot_swap_cannot_bypass_authoritative_turn_partial_drop() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
    )
    runtime._asr_speaker_authoritative_turns.add(turn_token)
    runtime._speaker_verifier_enforces_admission = False

    await runtime._send_independent_asr_preview(
        "quarantined across swap",
        runtime._asr_session_epoch,
    )
    callbacks.on_partial.assert_not_awaited()
    await runtime._execute_admission_effect(
        SettlePartial(turn_token, 1, AdmissionDisposition.DROP)
    )
    await runtime._send_independent_asr_preview(
        "late denied partial",
        runtime._asr_session_epoch,
    )
    callbacks.on_partial.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_provider_silero_resume_only_throttles_and_never_splits() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._voice_input_resource_optimization_enabled = True
    await _open_runtime_admission_turn(runtime, turn_token)
    detector_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=7,
        sequence_no=41,
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.SPEECH_STARTED,),
            throttle_available=True,
            identity=detector_identity,
            candidate=DetectorCandidateKey(7, 11),
        )
    )
    runtime._bind_provider_detector_candidate = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    ingress = turn_token.ingress

    first = await runtime.submit(
        ProcessedVoiceFrame(b"\x09\x00" * 160, 16_000, 0.9, True),
        ingress_token=ingress,
    )
    detector.observe_provider_audio_ordered.reset_mock()
    resumed_identity = DetectorIngressIdentity(
        ingress_token=ingress,
        detector_epoch=7,
        sequence_no=42,
    )
    detector.feed.return_value = DetectorFeedResult(
        events=(SpeechActivityEvent.SPEECH_RESUMED,),
        throttle_available=True,
        identity=resumed_identity,
        candidate=DetectorCandidateKey(7, 11),
    )
    resumed_pcm = b"\x0a\x00" * 160

    second = await runtime.submit(
        ProcessedVoiceFrame(resumed_pcm, 16_000, 0.9, True),
        ingress_token=ingress,
    )

    assert first.status is AsrSubmitStatus.ACCEPTED
    assert second.status is AsrSubmitStatus.ACCEPTED
    detector.observe_provider_audio_ordered.assert_awaited_once_with(
        resumed_pcm,
        sample_rate_hz=16_000,
        identity=resumed_identity,
        sequence_no=2,
        split_before_audio=False,
        evidence_complete=True,
        speaker_evidence_lease=detector._provider_evidence_lease,
    )
    await _close_dispatchers(runtime)


async def test_provider_split_with_pre_roll_marks_ordered_evidence_incomplete() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._voice_input_resource_optimization_enabled = True
    await _open_runtime_admission_turn(runtime, turn_token)
    detector_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=7,
        sequence_no=42,
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.SPEECH_RESUMED,),
            throttle_available=True,
            identity=detector_identity,
            candidate=DetectorCandidateKey(7, 11),
        )
    )
    runtime._bind_provider_detector_candidate = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    pre_roll = b"\x0c\x00" * 320
    lifecycle.accept_audio = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            disposition=AudioDisposition.FORWARD_WITH_PRE_ROLL,
            pre_roll=pre_roll,
        )
    )

    result = await runtime.submit(
        ProcessedVoiceFrame(b"\x0d\x00" * 160, 16_000, 0.9, True),
        ingress_token=turn_token.ingress,
    )

    assert result.status is AsrSubmitStatus.ACCEPTED
    detector.observe_provider_audio_ordered.assert_awaited_once_with(
        pre_roll,
        sample_rate_hz=16_000,
        identity=detector_identity,
        sequence_no=1,
        split_before_audio=False,
        evidence_complete=False,
        speaker_evidence_lease=detector._provider_evidence_lease,
    )
    await _close_dispatchers(runtime)


async def test_provider_ordered_observation_drift_is_stale_after_audio_admission() -> (
    None
):
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._voice_input_resource_optimization_enabled = True
    await _open_runtime_admission_turn(runtime, turn_token)
    detector_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=7,
        sequence_no=51,
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.SPEECH_STARTED,),
            throttle_available=True,
            identity=detector_identity,
            candidate=DetectorCandidateKey(7, 11),
        )
    )
    runtime._bind_provider_detector_candidate = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )

    async def drift_after_observation(*_args, **_kwargs) -> None:
        runtime._asr_audio_generation += 1

    detector.observe_provider_audio_ordered.side_effect = drift_after_observation

    result = await runtime.submit(
        ProcessedVoiceFrame(b"\x0a\x00" * 160, 16_000, 0.9, True),
        ingress_token=turn_token.ingress,
    )

    assert result.status is AsrSubmitStatus.STALE
    detector.observe_provider_audio_ordered.assert_awaited_once()
    detector.observe_provider_audio.assert_not_called()
    await _close_dispatchers(runtime)


async def test_provider_ordered_observation_missing_identity_retires_timeline() -> (
    None
):
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._voice_input_resource_optimization_enabled = True
    await _open_runtime_admission_turn(runtime, turn_token)
    first_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=7,
        sequence_no=52,
    )
    third_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=7,
        sequence_no=54,
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        side_effect=(
            DetectorFeedResult(
                events=(),
                throttle_available=True,
                identity=first_identity,
                candidate=DetectorCandidateKey(7, 11),
            ),
            DetectorFeedResult(
                events=(),
                throttle_available=True,
                identity=None,
                candidate=DetectorCandidateKey(7, 11),
            ),
            DetectorFeedResult(
                events=(),
                throttle_available=True,
                identity=third_identity,
                candidate=DetectorCandidateKey(7, 11),
            ),
        )
    )
    runtime._bind_provider_detector_candidate = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    first_pcm16 = b"\x0a\x00" * 160
    fallback_pcm16 = b"\x0b\x00" * 160
    third_pcm16 = b"\x0c\x00" * 160

    results = [
        await runtime.submit(
            ProcessedVoiceFrame(pcm16, 16_000, 0.9, True),
            ingress_token=turn_token.ingress,
        )
        for pcm16 in (first_pcm16, fallback_pcm16, third_pcm16)
    ]

    assert [result.status for result in results] == [
        AsrSubmitStatus.ACCEPTED,
        AsrSubmitStatus.UNAVAILABLE,
        AsrSubmitStatus.UNAVAILABLE,
    ]
    assert detector.observe_provider_audio_ordered.await_args_list == [
        call(
            first_pcm16,
            sample_rate_hz=16_000,
            identity=first_identity,
            sequence_no=1,
            split_before_audio=False,
            evidence_complete=True,
            speaker_evidence_lease=detector._provider_evidence_lease,
        ),
    ]
    detector.observe_provider_audio.assert_not_called()
    assert runtime._asr_session is None
    await _close_dispatchers(runtime)


@pytest.mark.parametrize("failure_mode", ["exception", "missing_update"])
async def test_unknown_provider_audio_accounting_retires_asr_timeline(
    failure_mode: str,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    runtime._voice_input_resource_optimization_enabled = True
    await _open_runtime_admission_turn(runtime, turn_token)
    detector_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=7,
        sequence_no=61,
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.SPEECH_STARTED,),
            throttle_available=True,
            identity=detector_identity,
            candidate=DetectorCandidateKey(7, 11),
        )
    )
    runtime._bind_provider_detector_candidate = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    if failure_mode == "exception":
        detector.observe_provider_audio_ordered.side_effect = RuntimeError(
            "private observer failure"
        )
    else:
        detector.observe_provider_audio_ordered.side_effect = AsyncMock(
            return_value=None
        )
    pcm16 = b"\x0e\x00" * 160

    result = await runtime.submit(
        ProcessedVoiceFrame(pcm16, 16_000, 0.9, True),
        ingress_token=turn_token.ingress,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    assert result.status is AsrSubmitStatus.UNAVAILABLE
    detector.observe_provider_audio_ordered.assert_awaited_once()
    detector.observe_provider_audio.assert_not_called()
    session.stream_audio.assert_not_awaited()
    runtime._callbacks.on_failure.assert_awaited_once()
    assert runtime._asr_session is None
    assert runtime._asr_session_epoch > turn_token.ingress.session_epoch
    status = runtime._callbacks.on_status.await_args.args[0]
    assert status.code == "ASR_AUDIO_ORDERING_FAILED"
    assert status.incident_id
    await _close_dispatchers(runtime)


async def test_concurrent_ordered_observers_reserve_unique_sequences() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await _open_runtime_admission_turn(runtime, turn_token)
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    entered_sequences: list[int] = []
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_observer(
        _pcm16: bytes,
        *,
        sample_rate_hz: int,
        identity: DetectorIngressIdentity,
        sequence_no: int,
        split_before_audio: bool,
        evidence_complete: bool,
        speaker_evidence_lease: ProviderSpeakerEvidenceLease,
    ) -> ProviderSpeakerEvidenceUpdate:
        assert sample_rate_hz == 16_000
        assert identity.ingress_token == turn_token.ingress
        assert split_before_audio is False
        assert evidence_complete is True
        assert speaker_evidence_lease == detector._provider_evidence_lease
        entered_sequences.append(sequence_no)
        if len(entered_sequences) == 2:
            both_entered.set()
        await release.wait()
        return ProviderSpeakerEvidenceUpdate(
            lease=speaker_evidence_lease,
            capture=SpeakerShadowCaptureResult(
                disposition=SpeakerShadowCaptureDisposition.ACCEPTED,
                accepted_sample_count=160,
                cumulative_sample_count=160 * sequence_no,
                completed_window_sample_count=0,
                decision_state=SpeakerShadowCaptureDecisionState.PENDING,
            ),
            sequence_no=sequence_no,
            last_progress_at=10.0,
        )

    detector.observe_provider_audio_ordered.side_effect = blocked_observer
    identities = (
        DetectorIngressIdentity(
            ingress_token=turn_token.ingress,
            detector_epoch=7,
            sequence_no=71,
        ),
        DetectorIngressIdentity(
            ingress_token=turn_token.ingress,
            detector_epoch=7,
            sequence_no=72,
        ),
    )
    tasks = tuple(
        asyncio.create_task(
            runtime._observe_admitted_provider_audio(
                lifecycle,
                detector,
                bytes((value, 0)) * 160,
                sample_rate_hz=16_000,
                identity=identity,
                split_before_audio=False,
                evidence_complete=True,
                turn_token=turn_token,
            )
        )
        for value, identity in zip((17, 18), identities, strict=True)
    )

    await asyncio.wait_for(both_entered.wait(), 1)
    assert entered_sequences == [1, 2]
    assert len(set(entered_sequences)) == 2
    release.set()
    assert await asyncio.wait_for(asyncio.gather(*tasks), 1) == [True, True]
    await _close_dispatchers(runtime)


async def _close_dispatchers(runtime: IndependentAsrRuntime) -> None:
    if runtime._asr_admission_ingress_started:
        await runtime._asr_admission_ingress.close()
        runtime._asr_admission_ingress_started = False
    await runtime._asr_audio_dispatcher.close()
    transcript_workers = {
        worker
        for worker in (
            runtime._asr_transcript_dispatcher._worker,
            runtime._asr_transcript_dispatcher._handoff_worker,
        )
        if worker is not None
    }
    runtime._asr_transcript_dispatcher.invalidate_all()
    if transcript_workers:
        await asyncio.gather(*transcript_workers, return_exceptions=True)


def _verifier_supported_route(runtime, detector) -> None:
    runtime._asr_detector = detector
    runtime._asr_lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
    )
    runtime._asr_lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)


def _accept_verifier_handoff(detector, shadow, owner_generation, operation) -> None:
    assert operation.identity.detector_identity == id(detector)
    assert operation.identity.installation_id == owner_generation
    assert operation.identity.activation_revision == "new-profile"
    assert owner_generation != "new-profile"
    operation.ownership_state = SpeakerVerifierOwnershipState.DETECTOR
    detector._speaker_shadow = shadow
    operation.outcome = SpeakerVerifierInstallOutcome.INSTALLED


async def test_verifier_factory_hot_replaces_active_detector_and_closes_old() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _verifier_supported_route(runtime, detector)
    old_factory = MagicMock()
    old_factory.close = MagicMock()
    runtime._speaker_verifier_factory = old_factory
    runtime._speaker_verifier_activation_generation = "old-profile"
    shadow = SimpleNamespace(close=AsyncMock())
    factory = MagicMock(return_value=shadow)
    factory.enforces_admission = True

    async def install(new_shadow, *, owner_generation, operation):
        _accept_verifier_handoff(detector, new_shadow, owner_generation, operation)

    detector.replace_speaker_verifier.side_effect = install

    updated = await runtime.set_speaker_verifier_factory(
        factory,
        activation_generation="new-profile",
    )

    assert updated is True
    detector.replace_speaker_verifier.assert_awaited_once()
    installed = runtime._speaker_verifier_install_receipt
    assert installed is not None and installed.identity is not None
    assert installed.outcome is SpeakerVerifierInstallOutcome.INSTALLED
    assert installed.ownership_state is SpeakerVerifierOwnershipState.DETECTOR
    assert detector._speaker_shadow is shadow
    assert runtime._speaker_verifier_factory is factory
    assert runtime._speaker_verifier_activation_generation == installed.identity.installation_id
    assert installed.identity.activation_revision == "new-profile"
    assert runtime._speaker_verifier_enforces_admission is True
    old_factory.close.assert_called_once_with()
    await detector._speaker_shadow.close()
    factory.close()


async def test_verifier_factory_failure_fences_old_activation_fail_open() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    detector.replace_speaker_verifier.side_effect = RuntimeError("swap failed")
    _verifier_supported_route(runtime, detector)
    old_factory = MagicMock()
    runtime._speaker_verifier_factory = old_factory
    runtime._speaker_verifier_activation_generation = "old-profile"
    shadow = SimpleNamespace(close=AsyncMock())
    factory = MagicMock(return_value=shadow)
    factory.enforces_admission = True

    updated = await runtime.set_speaker_verifier_factory(
        factory,
        activation_generation="new-profile",
    )

    assert updated is False
    # A failed call before explicit transfer leaves cleanup with the installer.
    shadow.close.assert_awaited_once_with()
    assert runtime._speaker_verifier_factory is None
    assert runtime._speaker_verifier_activation_generation is None
    assert runtime._speaker_verifier_enforces_admission is False
    old_factory.close.assert_called_once_with()
    factory.close.assert_called_once_with()


async def test_verifier_failure_after_new_binding_retires_collecting_authority() -> (
    None
):
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    await runtime._asr_admission_ingress.open_turn(turn_token)
    old_factory = MagicMock()
    old_factory.close = MagicMock()
    runtime._speaker_verifier_factory = old_factory
    runtime._speaker_verifier_activation_generation = "old-profile"
    shadow = SimpleNamespace(close=AsyncMock())
    factory = MagicMock(return_value=shadow)
    factory.enforces_admission = True
    new_candidate = SpeakerShadowCandidateKey(7, 999, "provider_candidate")
    old_candidate = _shadow_candidate()
    assert runtime._accept_speaker_candidate_binding(
        old_candidate, turn_token, detector=detector,
        activation_generation="old-profile",
    )
    await _drain_runtime_admission(runtime)
    prior = await runtime._asr_admission.get_record(turn_token)
    assert prior is not None and prior.capture_state is CaptureState.COLLECTING

    async def publish_then_fail(new_shadow, *, owner_generation, operation) -> None:
        assert new_shadow is shadow
        _accept_verifier_handoff(detector, new_shadow, owner_generation, operation)
        # The old test published new permission before installation committed.
        # Explicit transfer alone must no longer authorize a collecting candidate.
        assert not runtime._accept_speaker_candidate_binding(
            new_candidate,
            turn_token,
            detector=detector,
            activation_generation=owner_generation,
        )
        await _drain_runtime_admission(runtime)
        record = await runtime._asr_admission.get_record(turn_token)
        assert record is not None
        assert record.capture_state is not CaptureState.COLLECTING
        raise RuntimeError("swap failed after install")

    detector.replace_speaker_verifier.side_effect = publish_then_fail
    try:
        updated = await runtime.set_speaker_verifier_factory(
            factory,
            activation_generation="new-profile",
        )
        await _drain_runtime_admission(runtime)

        assert updated is False
        record = await runtime._asr_admission.get_record(turn_token)
        assert record is None or record.capture_state is not CaptureState.COLLECTING
        assert runtime._asr_admission_candidate_turns == {}
        assert runtime._asr_admission_capabilities == {}
        shadow.close.assert_not_awaited()
        old_factory.close.assert_called_once_with()
    finally:
        await detector._speaker_shadow.close()
        shadow.close.assert_awaited_once_with()
        await _close_dispatchers(runtime)


async def test_cancelled_verifier_swap_keeps_transferred_shadow_owned_by_detector() -> (
    None
):
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _verifier_supported_route(runtime, detector)
    old_factory = MagicMock()
    old_factory.close = MagicMock()
    runtime._speaker_verifier_factory = old_factory
    runtime._speaker_verifier_activation_generation = "old-profile"
    shadow = SimpleNamespace(close=AsyncMock())
    factory = MagicMock(return_value=shadow)
    factory.enforces_admission = True
    entered = asyncio.Event()

    async def block_after_handoff(new_shadow, *, owner_generation, operation) -> None:
        assert new_shadow is shadow
        _accept_verifier_handoff(detector, new_shadow, owner_generation, operation)
        assert runtime._speaker_verifier_activation_generation is None
        assert runtime._speaker_verifier_enforces_admission is False
        entered.set()
        await asyncio.Event().wait()

    detector.replace_speaker_verifier.side_effect = block_after_handoff
    task = asyncio.create_task(
        runtime.set_speaker_verifier_factory(
            factory,
            activation_generation="new-profile",
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    shadow.close.assert_not_awaited()
    assert runtime._speaker_verifier_factory is None
    assert runtime._speaker_verifier_activation_generation is None
    assert runtime._speaker_verifier_enforces_admission is False
    old_factory.close.assert_called_once_with()
    factory.close.assert_called_once_with()
    assert detector._speaker_shadow is shadow
    await detector._speaker_shadow.close()
    shadow.close.assert_awaited_once_with()


async def test_sealed_provider_rejection_suppresses_only_exact_final() -> None:
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    capability = _admission_capability(
        turn_token,
        candidate,
        provider_key=provider_key,
        kind=RejectionCapabilityKind.SEALED,
    )
    try:
        await lane.post(
            turn_token,
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        )
        deny_effects = await lane.post(
            turn_token,
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        )
        boundary_effects = await lane.post(turn_token, BoundaryExact(capability))
        assert not any(
            isinstance(effect, ApplyRejection) for effect in boundary_effects
        )
        final_effects = await lane.post(
            turn_token,
            ProviderFinalReceived(_admission_final(provider_key)),
            now=10.0,
        )
        assert not any(isinstance(effect, ResolveReserved) for effect in final_effects)
        resolutions = [
            effect for effect in deny_effects if isinstance(effect, ResolveReserved)
        ]
        assert [effect.disposition for effect in resolutions] == [
            AdmissionDisposition.DROP
        ]
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.DROPPED
    finally:
        await lane.close()


async def test_provider_micro_event_shadow_forwards_non_empty_final() -> None:
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    try:
        await lane.post(turn_token, SpeakerHigh(candidate, 1))
        await lane.post(turn_token, MicroEventPending())
        final_effects = await lane.post(
            turn_token,
            ProviderFinalReceived(_admission_final(provider_key, text="嗯")),
            now=10.0,
        )
        assert not any(isinstance(effect, ResolveReserved) for effect in final_effects)
        allowed_effects = await lane.post(turn_token, MicroEventAllowed(), now=10.1)
        assert [
            effect.disposition
            for effect in allowed_effects
            if isinstance(effect, ResolveReserved)
        ] == [AdmissionDisposition.FORWARD]
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.FORWARDED
    finally:
        await lane.close()


@pytest.mark.parametrize("enforce", [False, True])
async def test_provider_micro_event_empty_final_is_never_suppressed_or_counted(
    enforce: bool,
) -> None:
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    try:
        await lane.post(turn_token, SpeakerHigh(candidate, 1))
        await lane.post(turn_token, MicroEventPending())
        await lane.post(
            turn_token,
            MicroEventSuppressed() if enforce else MicroEventAllowed(),
        )
        effects = await lane.post(
            turn_token,
            ProviderFinalReceived(_admission_final(provider_key, text="")),
            now=10.0,
        )
        assert [
            effect.disposition
            for effect in effects
            if isinstance(effect, ResolveReserved)
        ] == [AdmissionDisposition.FORWARD]
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.FORWARDED
    finally:
        await lane.close()


async def test_provider_micro_event_enforce_suppresses_exact_non_empty_final() -> None:
    (
        coordinator,
        lane,
        turn_token,
        _candidate,
        provider_key,
    ) = await _open_admission_turn(provider_bound=True)
    assert provider_key is not None
    try:
        await lane.post(turn_token, MicroEventPending())
        final_effects = await lane.post(
            turn_token,
            ProviderFinalReceived(_admission_final(provider_key, text="嗯")),
            now=10.0,
        )
        assert not any(isinstance(effect, ResolveReserved) for effect in final_effects)
        suppressed_effects = await lane.post(
            turn_token,
            MicroEventSuppressed(),
            now=10.1,
        )
        assert [
            effect.disposition
            for effect in suppressed_effects
            if isinstance(effect, ResolveReserved)
        ] == [AdmissionDisposition.DROP]
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.DROPPED
    finally:
        await lane.close()


async def test_exact_speaker_and_micro_event_suppress_only_once() -> None:
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    capability = _admission_capability(
        turn_token,
        candidate,
        provider_key=provider_key,
        kind=RejectionCapabilityKind.SEALED,
    )
    try:
        await lane.post(
            turn_token,
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        )
        deny_effects = await lane.post(
            turn_token,
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        )
        boundary_effects = await lane.post(turn_token, BoundaryExact(capability))
        assert not any(
            isinstance(effect, ApplyRejection) for effect in boundary_effects
        )
        await lane.post(turn_token, MicroEventPending())
        final_effects = await lane.post(
            turn_token,
            ProviderFinalReceived(_admission_final(provider_key, text="嗯")),
            now=10.0,
        )
        micro_effects = await lane.post(
            turn_token,
            MicroEventSuppressed(),
            now=10.11,
        )
        assert (
            sum(
                isinstance(effect, ResolveReserved)
                for effect in (*deny_effects, *final_effects, *micro_effects)
            )
            == 1
        )
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.DROPPED
    finally:
        await lane.close()


async def test_provider_micro_event_query_failure_fails_open() -> None:
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    try:
        await lane.post(turn_token, SpeakerHigh(candidate, 1))
        await lane.post(turn_token, MicroEventPending())
        await lane.post(
            turn_token,
            ProviderFinalReceived(_admission_final(provider_key, text="嗯")),
            now=10.0,
        )
        effects = await lane.post(turn_token, MicroEventUnavailable(), now=10.1)
        assert [
            effect.disposition
            for effect in effects
            if isinstance(effect, ResolveReserved)
        ] == [AdmissionDisposition.FORWARD]
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.FORWARDED
    finally:
        await lane.close()


async def test_provider_micro_event_decision_is_stale_after_completion_drift() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token, _provider_fence = await _seal_provider_candidate(
        runtime,
        detector,
    )
    detector.sealed_provider_micro_event_decision.return_value = (
        ProviderMicroEventDecision(True, True, "micro_event_enforced")
    )

    async def drift_during_completion(_provider_fence) -> bool:
        runtime._asr_audio_generation += 1
        return False

    detector.complete_provider_candidate.side_effect = drift_during_completion

    await runtime._handle_independent_asr_final("嗯", 0, "qwen")

    callbacks.on_final.assert_not_awaited()
    assert not runtime._asr_transcript_dispatcher.try_reserve(
        FinalKey.from_turn(turn_token)
    )
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["micro_event_suppressed_count"] == 0
    assert diagnostics["micro_event_shadow_forward_count"] == 0
    await _close_dispatchers(runtime)


async def test_provider_completion_rejects_session_replacement_during_await() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, lifecycle, turn_token, _provider_fence = await _seal_provider_candidate(
        runtime, detector
    )
    runtime._speaker_verifier_enforces_admission = False
    posted_events: list[object] = []
    post_admission_event = runtime._post_admission_event

    async def capture_admission_event(token, event, **kwargs):
        posted_events.append(event)
        return await post_admission_event(token, event, **kwargs)

    runtime._post_admission_event = capture_admission_event

    async def replace_session_during_completion(_provider_fence) -> bool:
        runtime._asr_session = SimpleNamespace(
            is_ready=True,
            close=AsyncMock(),
            signal_user_activity_end=AsyncMock(),
            stream_audio=AsyncMock(),
        )
        return False

    detector.complete_provider_candidate.side_effect = replace_session_during_completion

    settled = await runtime._handle_independent_asr_final(
        "stale-final",
        0,
        "qwen",
    )
    assert settled is not None
    await asyncio.wait_for(settled.wait(), 1)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_awaited_once()
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    assert any(
        isinstance(event, TransportSettled) and event.degraded
        for event in posted_events
    )
    assert any(
        isinstance(event, LifecycleSettled) and event.degraded
        for event in posted_events
    )
    final_key = FinalKey.from_turn(turn_token)
    assert not runtime._asr_transcript_dispatcher.try_reserve(final_key)
    await _close_dispatchers(runtime)


async def test_provider_silero_micro_event_cannot_suppress_text_successor() -> None:
    abandoned = AsyncMock()
    callbacks = _callbacks(abandoned=abandoned)
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    session, lifecycle, first_turn, _provider_fence = await _seal_provider_candidate(
        runtime,
        detector,
    )
    runtime._speaker_verifier_enforces_admission = False
    lifecycle.mark_pending_turn_speech()
    pending_pcm = b"\x01\x00" * 320
    buffered = lifecycle.accept_audio(pending_pcm, sample_rate_hz=16_000)
    assert buffered.disposition.value == "buffer"
    detector.sealed_provider_micro_event_decision.return_value = (
        ProviderMicroEventDecision(True, True, "micro_event_enforced")
    )

    await runtime._handle_independent_asr_final("嗯", 0, "qwen")
    await runtime._asr_audio_dispatcher.wait_idle()

    assert callbacks.on_final.await_count == 1
    abandoned.assert_not_awaited()
    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    successor_turn = runtime._asr_partial_turn_token
    assert successor_turn is not None and successor_turn != first_turn
    detector.lease = _RejectionLease(detector, successor_turn)
    await runtime._handle_independent_asr_endpoint(0)
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    detector.sealed_provider_micro_event_decision.return_value = (
        ProviderMicroEventDecision(False, False, "silero_span_exceeded")
    )

    await runtime._handle_independent_asr_final("嗯", 0, "qwen")
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    assert callbacks.on_final.await_count == 2
    assert callbacks.on_final.await_args.args[0].text == "嗯"
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["micro_event_suppressed_count"] == 0
    assert diagnostics["micro_event_shadow_forward_count"] == 0
    detector.sealed_provider_micro_event_decision.assert_not_called()
    await _close_dispatchers(runtime)


@pytest.mark.parametrize("enforce", [False, True])
async def test_duplicate_micro_event_final_counts_once_and_preserves_next_turn(
    enforce: bool,
) -> None:
    abandoned = AsyncMock()
    callbacks = _callbacks(abandoned=abandoned)
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    session, lifecycle, first_turn, provider_fence = await _seal_provider_candidate(
        runtime,
        detector,
    )
    runtime._speaker_verifier_enforces_admission = False
    lifecycle.mark_pending_turn_speech()
    pending_pcm = b"\x02\x00" * 320
    buffered = lifecycle.accept_audio(pending_pcm, sample_rate_hz=16_000)
    assert buffered.disposition.value == "buffer"
    detector.sealed_provider_micro_event_decision.return_value = (
        ProviderMicroEventDecision(True, enforce, "eligible_micro_event")
    )
    complete_entered = asyncio.Event()
    complete_release = asyncio.Event()

    async def blocked_complete(received_fence) -> bool:
        assert received_fence == provider_fence
        complete_entered.set()
        await complete_release.wait()
        return False

    detector.complete_provider_candidate.side_effect = blocked_complete
    first_final = asyncio.create_task(
        runtime._handle_independent_asr_final("嗯", 0, "qwen")
    )
    await asyncio.wait_for(complete_entered.wait(), 1)
    duplicate_final = asyncio.create_task(
        runtime._handle_independent_asr_final("嗯", 0, "qwen")
    )
    await asyncio.sleep(0)
    assert duplicate_final.done() is False
    complete_release.set()

    settlements = await asyncio.wait_for(
        asyncio.gather(first_final, duplicate_final),
        1,
    )
    assert sum(settled is not None for settled in settlements) == 1
    settled = next(settled for settled in settlements if settled is not None)
    await asyncio.wait_for(settled.wait(), 1)
    await runtime._asr_audio_dispatcher.wait_idle()

    detector.sealed_provider_micro_event_decision.assert_not_called()
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["micro_event_suppressed_count"] == 0
    assert diagnostics["micro_event_shadow_forward_count"] == 0
    assert callbacks.on_final.await_count == 1
    abandoned.assert_not_awaited()

    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    successor_turn = runtime._asr_partial_turn_token
    assert successor_turn is not None and successor_turn != first_turn
    detector.lease = _RejectionLease(detector, successor_turn)
    detector.complete_provider_candidate.side_effect = None
    detector.complete_provider_candidate.return_value = False
    await runtime._handle_independent_asr_endpoint(0)
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    detector.sealed_provider_micro_event_decision.return_value = (
        ProviderMicroEventDecision(False, False, "silero_span_exceeded")
    )

    await runtime._handle_independent_asr_final("嗯", 0, "qwen")
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    assert callbacks.on_final.await_count == 2
    assert callbacks.on_final.await_args.args[0].text == "嗯"
    detector.sealed_provider_micro_event_decision.assert_not_called()
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["micro_event_suppressed_count"] == 0
    assert diagnostics["micro_event_shadow_forward_count"] == 0
    await _close_dispatchers(runtime)


async def test_smart_turn_final_does_not_query_provider_micro_event() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="manual",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    await runtime._asr_admission_ingress.open_turn(turn_token)
    runtime._asr_admission_reservation_dispatchers[FinalKey.from_turn(turn_token)] = (
        runtime._asr_transcript_dispatcher
    )
    _seal_installed_provider_candidate(
        runtime,
        detector,
        session,
        lifecycle,
        turn_token,
    )
    # This test isolates SmartTurn's micro-event contract. Speaker admission
    # is disabled so a missing verdict cannot hold the unrelated final.
    runtime._speaker_verifier_enforces_admission = False
    detector.sealed_provider_micro_event_decision.return_value = (
        ProviderMicroEventDecision(True, True, "micro_event_enforced")
    )

    await runtime._handle_independent_asr_final("嗯", 0, "qwen")
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    detector.sealed_provider_micro_event_decision.assert_not_called()
    callbacks.on_final.assert_awaited_once()
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["micro_event_suppressed_count"] == 0
    assert diagnostics["micro_event_shadow_forward_count"] == 0
    await _close_dispatchers(runtime)


async def test_pre_anchor_low_never_becomes_formal_provider_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    from main_logic.asr_client.speaker_shadow.asset_manifest import (
        CAMPPLUS_MODEL_ID,
        CAMPPLUS_MODEL_REVISION,
    )
    from main_logic.asr_client.speaker_shadow.campplus import (
        CAMPPLUS_EMBEDDING_DIM,
    )
    from main_logic.voice_identity.contracts import SpeakerModelIdentity
    from main_logic.voice_identity.profile import SpeakerProfile
    from main_logic.voice_identity.reference import SpeakerReference
    from main_logic.voice_identity_service.asr_composition import (
        OwnerVoiceAsrCompositionFactory,
    )

    class _Vad:
        def load(self) -> bool:
            return True

        def close(self) -> None:
            return None

    class _Gate:
        def feed(self, _pcm16: bytes) -> tuple[()]:
            return ()

        def reset(self) -> None:
            return None

    class _LowScoreHost:
        alive = True
        loaded = True
        timed_out = False
        was_terminated = False
        pcm_bytes_in_use = 0
        process_count = 0

        def __init__(self) -> None:
            self.score_count = 0

        async def score(
            self,
            _pcm16: bytes,
            *,
            timeout_seconds: float,
        ) -> float:
            assert timeout_seconds > 0
            self.score_count += 1
            return 0.20

        async def close(self, *, timeout_seconds: float) -> bool:
            self.alive = False
            return True

        async def terminate(self) -> None:
            self.alive = False

    runtime = IndependentAsrRuntime(_callbacks())
    placeholder = _RejectionDetector()
    session, lifecycle, first_turn = _install_active_candidate(
        runtime,
        placeholder,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True

    model_identity = SpeakerModelIdentity(
        CAMPPLUS_MODEL_ID,
        CAMPPLUS_MODEL_REVISION,
        CAMPPLUS_EMBEDDING_DIM,
    )
    embedding = np.arange(1, CAMPPLUS_EMBEDDING_DIM + 1, dtype=np.float32)
    reference = SpeakerReference(model_identity, embedding)
    embedding.fill(0.0)
    try:
        profile = SpeakerProfile("profile-generation", reference)
    finally:
        reference.close()
    composition = OwnerVoiceAsrCompositionFactory(
        runtime,
        profile,
        activation_generation="profile-generation",
        enforce=True,
    )
    shadow = composition()
    scoring_host = _LowScoreHost()
    monkeypatch.setattr(shadow, "_backend_host", scoring_host)
    detector = DetectorRuntime(
        vad=_Vad(),
        gate=_Gate(),
        provider_policy=resolve_provider_policy("qwen", "provider"),
        speaker_shadow=shadow,
    )
    runtime._asr_detector = detector

    key_a = ProviderUtteranceKey(0, 0, 1)
    assert await runtime._arm_speaker_authority_for_provider_audio(first_turn)
    assert runtime._asr_current_speaker_lease is None

    first_feed = await detector.feed(
        b"\x01\x00" * 160,
        ingress_token=first_turn.ingress,
        speech_probability=0.9,
        rnnoise_available=True,
    )
    assert first_feed.identity is not None
    assert await runtime._observe_admitted_provider_audio(
        lifecycle,
        detector,
        b"\x11\x00" * 24_000,
        sample_rate_hz=16_000,
        identity=first_feed.identity,
        split_before_audio=False,
        evidence_complete=True,
        turn_token=first_turn,
    )
    await shadow.wait_idle()
    await _drain_runtime_admission(runtime)
    assert scoring_host.score_count == 0
    assert runtime._speaker_verifier_diagnostics()["speaker_deny_latched_count"] == 0

    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0, 0, 1, audio_start_sample_16k=0
        ),
        runtime._asr_session_epoch,
    )
    await shadow.wait_idle()
    await _drain_runtime_admission(runtime)
    assert scoring_host.score_count == 1
    lease_token = runtime._asr_current_speaker_lease
    assert lease_token is not None
    first_lease = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert first_lease is not None
    assert first_lease.state is SpeakerLeaseState.COLLECTING
    assert first_lease.last_speaker_sequence_no == 0
    assert first_lease.child_bindings[0].provider_key == key_a

    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="boundary",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="unknown",
            audio_range=None,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="ordered",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="unknown",
            audio_range=None,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_final(
        key_a,
        "first",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()
    runtime._callbacks.on_final.assert_awaited_once()
    assert runtime._callbacks.on_final.await_args.args[0].text == "first"
    session.close.assert_not_awaited()

    await detector.close()
    composition.close()
    profile.close()
    await _close_dispatchers(runtime)


async def _install_exact_provider_parent(
    runtime: IndependentAsrRuntime,
    detector: _RejectionDetector,
) -> tuple[
    SimpleNamespace,
    VoiceInputLifecycleController,
    VoiceTurnToken,
    ProviderUtteranceKey,
    SpeakerCaptureLeaseToken,
    ProviderSpeakerEvidenceLease,
    ProviderAudioRange,
]:
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    evidence = runtime._asr_provider_speaker_evidence_lease
    assert evidence is not None
    runtime._asr_provider_exact_session = session
    key = ProviderUtteranceKey(0, 0, 1)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0,
            0,
            1,
            audio_start_sample_16k=0,
        ),
        runtime._asr_session_epoch,
    )
    lease_token = runtime._asr_current_speaker_lease
    assert lease_token is not None
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(
            evidence.candidate,
            1,
            SpeakerCheckpointKind.FIRST,
        ),
        activation_generation="profile-generation",
        enforce=True,
    )
    boundary = ProviderAudioRange(0, 160)
    return (
        session,
        lifecycle,
        turn_token,
        key,
        lease_token,
        evidence,
        boundary,
    )


async def _install_exact_provider_interval(
    runtime: IndependentAsrRuntime,
    detector: _RejectionDetector,
    *,
    ordered: bool = True,
) -> tuple[
    SimpleNamespace,
    VoiceInputLifecycleController,
    VoiceTurnToken,
    ProviderUtteranceKey,
    SpeakerCaptureLeaseToken,
    SpeakerShadowCandidateKey,
    SpeakerShadowCandidateKey,
]:
    (
        session,
        lifecycle,
        turn_token,
        key,
        lease_token,
        _evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="boundary",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=boundary,
        ),
        runtime._asr_session_epoch,
    )
    transaction = runtime._asr_provider_exact_intervals[key]
    if ordered:
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="ordered",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=boundary,
            ),
            runtime._asr_session_epoch,
        )
    return (
        session,
        lifecycle,
        turn_token,
        key,
        lease_token,
        transaction.target_candidate,
        transaction.successor_candidate,
    )


async def _install_nonzero_anchor_provider_parent(
    runtime: IndependentAsrRuntime,
    detector: _RejectionDetector,
    *,
    pre_anchor_fact: SpeakerLow | SpeakerHigh,
) -> tuple[
    SimpleNamespace,
    VoiceInputLifecycleController,
    VoiceTurnToken,
    ProviderUtteranceKey,
    SpeakerCaptureLeaseToken,
    ProviderSpeakerEvidenceLease,
    ProviderAudioRange,
]:
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    evidence = runtime._asr_provider_speaker_evidence_lease
    assert evidence is not None
    assert pre_anchor_fact.candidate == evidence.candidate
    runtime._asr_provider_exact_session = session

    assert runtime._accept_speaker_evidence_fact(
        pre_anchor_fact,
        activation_generation="profile-generation",
        enforce=True,
    )
    ledger = runtime._asr_provider_speaker_ledgers[evidence.candidate]
    assert ledger.state is runtime_module._ProviderSpeakerLedgerState.UNANCHORED_DEFERRED
    assert tuple(ledger.events) == ()
    assert ledger.last_speaker_sequence_no == 0
    assert runtime._asr_current_speaker_lease is None

    key = ProviderUtteranceKey(0, 0, 1)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0,
            0,
            1,
            audio_start_sample_16k=80,
        ),
        runtime._asr_session_epoch,
    )
    lease_token = runtime._asr_current_speaker_lease
    assert lease_token is not None
    lease = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert lease is not None
    assert lease.state is SpeakerLeaseState.COLLECTING
    assert lease.last_speaker_sequence_no == 0
    return (
        session,
        lifecycle,
        turn_token,
        key,
        lease_token,
        evidence,
        ProviderAudioRange(80, 160),
    )


async def test_nonzero_preroll_low_is_discarded_before_owner_suffix_forwards(
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    evidence = await detector.ensure_provider_speaker_evidence_lease()
    (
        session,
        lifecycle,
        turn_token,
        _key,
        lease_token,
        anchored_evidence,
        boundary,
    ) = await _install_nonzero_anchor_provider_parent(
        runtime,
        detector,
        pre_anchor_fact=SpeakerLow(
            evidence.candidate,
            1,
            SpeakerCheckpointKind.FIRST,
        ),
    )
    assert anchored_evidence == evidence
    threshold = SpeakerShadowConfig().similarity_thresholds[0]
    owner_suffix_similarity = 0.70
    assert threshold == 0.40
    assert owner_suffix_similarity >= threshold
    assert runtime._accept_speaker_evidence_fact(
        SpeakerHigh(evidence.candidate, 1),
        activation_generation="profile-generation",
        enforce=True,
    )

    for phase in ("boundary", "ordered"):
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase=phase,
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=boundary,
            ),
            runtime._asr_session_epoch,
        )
    transaction = runtime._asr_provider_exact_intervals[
        ProviderUtteranceKey(0, 0, 1)
    ]
    await runtime._handle_provider_final(
        transaction.provider_key,
        "owner-suffix-forward",
        runtime._asr_session_epoch,
        "qwen",
    )
    assert runtime._close_speaker_evidence(
        CaptureClosed(transaction.target_candidate, 1),
        activation_generation="profile-generation",
        enforce=True,
        evidence_complete=True,
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_final.await_args.args[0].text == "owner-suffix-forward"
    assert lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    session.close.assert_not_awaited()
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None
    assert parent.terminal_disposition is None
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["speaker_pre_anchor_fact_ignored_count"] == 1
    assert diagnostics["speaker_deny_latched_count"] == 0
    assert await runtime._asr_admission.get_record(turn_token) is None
    await _close_dispatchers(runtime)


async def test_nonzero_preroll_high_cannot_mask_non_owner_suffix_drop(
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    evidence = await detector.ensure_provider_speaker_evidence_lease()
    (
        session,
        lifecycle,
        _turn_token,
        key,
        _lease_token,
        anchored_evidence,
        boundary,
    ) = await _install_nonzero_anchor_provider_parent(
        runtime,
        detector,
        pre_anchor_fact=SpeakerHigh(evidence.candidate, 1),
    )
    assert anchored_evidence == evidence
    threshold = SpeakerShadowConfig().similarity_thresholds[0]
    non_owner_suffix_similarity = 0.20
    assert threshold == 0.40
    assert non_owner_suffix_similarity < threshold
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(
            evidence.candidate,
            1,
            SpeakerCheckpointKind.FIRST,
        ),
        activation_generation="profile-generation",
        enforce=True,
    )
    for phase in ("boundary", "ordered"):
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase=phase,
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=boundary,
            ),
            runtime._asr_session_epoch,
        )
    transaction = runtime._asr_provider_exact_intervals[key]
    await runtime._handle_provider_final(
        key,
        "non-owner-suffix-drop",
        runtime._asr_session_epoch,
        "qwen",
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(
            transaction.target_candidate,
            2,
            SpeakerCheckpointKind.SECOND,
        ),
        activation_generation="profile-generation",
        enforce=True,
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_not_awaited()
    assert lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    session.close.assert_not_awaited()
    diagnostics = runtime._speaker_verifier_diagnostics()
    assert diagnostics["speaker_pre_anchor_fact_ignored_count"] == 1
    assert transaction.resolved_disposition is AdmissionDisposition.DROP
    await _close_dispatchers(runtime)


async def test_exact_interval_completion_low_drops_only_child_without_transport_abort(
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        lifecycle,
        turn_token,
        key,
        lease_token,
        target,
        successor,
    ) = await _install_exact_provider_interval(runtime, detector)

    assert successor is not None
    assert runtime._asr_admission_turn_leases.get(turn_token) is None
    assert runtime._asr_current_speaker_lease == lease_token
    assert runtime._asr_current_speaker_candidate == successor
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING

    await runtime._handle_provider_final(
        key,
        "drop-me",
        runtime._asr_session_epoch,
        "qwen",
    )
    held = await runtime._asr_admission.get_record(turn_token)
    assert held is not None
    assert held.pending_final is not None
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(target, 2, SpeakerCheckpointKind.COMPLETION_CONFIRMATION),
        activation_generation="profile-generation",
        enforce=True,
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_not_awaited()
    session.close.assert_not_awaited()
    assert key not in runtime._asr_provider_exact_intervals
    assert target not in runtime._asr_provider_exact_candidates
    assert runtime._asr_current_speaker_candidate == successor
    assert lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    assert parent is not None
    assert parent.candidate == successor
    assert parent.terminal_disposition is None
    await _close_dispatchers(runtime)


async def test_exact_interval_final_first_uses_exact_seal_and_forwards_child(
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        lifecycle,
        _turn_token,
        key,
        _lease_token,
        target,
        _successor,
    ) = await _install_exact_provider_interval(
        runtime,
        detector,
        ordered=False,
    )

    await runtime._handle_provider_final(
        key,
        "forward-me",
        runtime._asr_session_epoch,
        "qwen",
    )
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    assert detector.seal_turn_tokens == []
    assert runtime._close_speaker_evidence(
        CaptureClosed(target, 1),
        activation_generation="profile-generation",
        enforce=True,
        evidence_complete=True,
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_final.await_args.args[0].text == "forward-me"
    session.close.assert_not_awaited()
    assert key not in runtime._asr_provider_exact_intervals
    assert lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    await _close_dispatchers(runtime)


@pytest.mark.parametrize("conflict_phase", ("boundary", "ordered"))
async def test_post_commit_exact_conflict_keeps_final_fail_open(
    conflict_phase: str,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        lifecycle,
        turn_token,
        key,
        _lease_token,
        _target,
        _successor,
    ) = await _install_exact_provider_interval(
        runtime,
        detector,
        ordered=False,
    )
    transaction = runtime._asr_provider_exact_intervals[key]
    conflicting_range = ProviderAudioRange(0, 80)
    await runtime._send_independent_asr_preview(
        "held-partial",
        runtime._asr_session_epoch,
    )
    callbacks.on_partial.assert_not_awaited()
    assert runtime._asr_quarantined_partials == {turn_token: "held-partial"}
    if conflict_phase == "boundary":
        await runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="ordered",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=ProviderAudioRange(0, 160),
            ),
            runtime._asr_session_epoch,
        )

    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase=conflict_phase,
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=conflicting_range,
        ),
        runtime._asr_session_epoch,
    )

    record = await runtime._asr_admission.get_record(turn_token)
    assert record is not None
    assert record.capture_state is CaptureState.UNAVAILABLE
    assert record.speaker_lease_token is None
    assert record.exact_interval_hold_id is None
    assert transaction.resolved_disposition is AdmissionDisposition.FORWARD
    assert key not in runtime._asr_provider_exact_intervals
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    callbacks.on_partial.assert_awaited_once()
    assert callbacks.on_partial.await_args.args[0].text == "held-partial"
    assert runtime._asr_quarantined_partials == {}
    assert runtime._speaker_verifier_diagnostics()[
        "speaker_unavailable_reason_prepare_count"
    ] == 1

    await runtime._handle_provider_final(
        key,
        f"{conflict_phase}-conflict-forward",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_partial.await_count == 1
    assert (
        callbacks.on_final.await_args.args[0].text
        == f"{conflict_phase}-conflict-forward"
    )
    session.close.assert_not_awaited()
    assert lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    await _close_dispatchers(runtime)


class _ExactRetirementWriterLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


async def _assert_exact_retirement_preserves_queued_final(monkeypatch, *, mutate=False):
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    session, _, turn, key, _, target, _ = await _install_exact_provider_interval(runtime, detector)
    await _drain_runtime_admission(runtime)
    transaction = runtime._asr_provider_exact_intervals[key]
    await runtime._send_independent_asr_preview("retired-partial", runtime._asr_session_epoch)
    callbacks.on_partial.assert_not_awaited()
    assert runtime._asr_quarantined_partials == {turn: "retired-partial"}

    if mutate:
        # Restore the old omission in memory only: the current LOW is not final,
        # so failing open clears the FIFO without retaining its accepted final.
        source = textwrap.dedent(inspect.getsource(runtime_module.IndependentAsrRuntime._apply_exact_interval_event))
        omitted = """queued.event for queued in transaction.event_queue
                    if isinstance(queued.event, ProviderFinalReceived)"""
        assert omitted in source
        source = source.replace(omitted, "queued.event for queued in ()", 1)
        namespace = {}
        exec(compile(source, "<queued-final-retention-mutation>", "exec"), runtime_module.__dict__, namespace)
        monkeypatch.setattr(runtime, "_apply_exact_interval_event",
            namespace["_apply_exact_interval_event"].__get__(runtime))

    writer = _ExactRetirementWriterLock()
    runtime._asr_admission._lock = writer
    final_queued = asyncio.Event()
    final_ready = asyncio.Event()
    release_final = asyncio.Event()
    enqueue = runtime._enqueue_exact_interval_event
    post_exact = runtime._post_exact_interval_event

    async def gate_final_post(observed_transaction, event):
        if isinstance(event, ProviderFinalReceived):
            final_ready.set()
            await release_final.wait()
        return await post_exact(observed_transaction, event)

    def observed_enqueue(observed_transaction, event, *, waiter=None):
        accepted = enqueue(observed_transaction, event, waiter=waiter)
        if accepted and isinstance(event, ProviderFinalReceived):
            final_queued.set()
        return accepted

    monkeypatch.setattr(runtime, "_enqueue_exact_interval_event", observed_enqueue)
    monkeypatch.setattr(runtime, "_post_exact_interval_event", gate_final_post)
    final_task = None
    try:
        final_task = asyncio.create_task(runtime._handle_provider_final(
            key, "retired-final", runtime._asr_session_epoch, "qwen",
        ))
        await asyncio.wait_for(final_ready.wait(), 1)
        await writer.acquire()
        try:
            assert runtime._accept_speaker_evidence_fact(
                SpeakerLow(target, 2, SpeakerCheckpointKind.COMPLETION_CONFIRMATION),
                activation_generation="profile-generation", enforce=True,
            )
            await asyncio.wait_for(writer.waiting.wait(), 1)
            release_final.set()
            await asyncio.wait_for(final_queued.wait(), 1)
            assert any(isinstance(item.event, ProviderFinalReceived) for item in transaction.event_queue)
            runtime.retire_speaker_verifier_authority()
        finally:
            writer.release()
        await asyncio.wait_for(final_task, 1)
        await _drain_runtime_admission(runtime)
        await runtime.wait_transcript_idle()

        settled = await runtime._asr_admission.get_record(turn)
        assert callbacks.on_final.await_count == 1, (
            "accepted final was lost during retirement",
            transaction.resolved_disposition,
            None if settled is None else (settled.capture_state, settled.evidence_state,
                settled.exact_interval_hold_id, settled.logical_revision),
            runtime._asr_admission_final_contexts,
            runtime._speaker_verifier_diagnostics(),
        )
        assert callbacks.on_final.await_args.args[0].text == "retired-final"
        # The canonical final is already pending: existing admission semantics
        # retire its cached partial instead of briefly replaying stale draft text.
        callbacks.on_partial.assert_not_awaited()
        assert transaction.resolved_disposition is AdmissionDisposition.FORWARD
        assert not runtime._asr_admission_final_contexts
        assert not runtime._asr_quarantined_partials
        session.close.assert_not_awaited()
    finally:
        if final_task is not None and not final_task.done():
            final_task.cancel()
            await asyncio.gather(final_task, return_exceptions=True)
        await _close_dispatchers(runtime)


@pytest.mark.parametrize("iteration", range(50))
async def test_installation_retirement_after_exact_enqueue_preserves_final_once(monkeypatch, iteration):
    await _assert_exact_retirement_preserves_queued_final(monkeypatch)


async def test_queued_final_retention_mutation_is_detected(monkeypatch):
    with pytest.raises(AssertionError, match="accepted final was lost during retirement"):
        await _assert_exact_retirement_preserves_queued_final(monkeypatch, mutate=True)


async def test_poisoned_exact_fifo_replays_queued_final_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        lifecycle,
        _turn_token,
        key,
        _lease_token,
        target,
        _successor,
    ) = await _install_exact_provider_interval(runtime, detector)
    transaction = runtime._asr_provider_exact_intervals[key]
    await _drain_runtime_admission(runtime)

    apply_entered = asyncio.Event()
    apply_release = asyncio.Event()
    original_apply = runtime._apply_exact_interval_event

    async def gated_apply(observed_transaction, event):
        if isinstance(event, SpeakerLeaseUnavailable) and event.sequence_no == 2:
            apply_entered.set()
            await apply_release.wait()
        return await original_apply(observed_transaction, event)

    monkeypatch.setattr(runtime, "_apply_exact_interval_event", gated_apply)
    assert runtime._accept_speaker_evidence_fact(
        SpeakerUnavailable(target, 2),
        activation_generation="profile-generation",
        enforce=True,
    )
    await apply_entered.wait()
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(target, 2, SpeakerCheckpointKind.COMPLETION_CONFIRMATION),
        activation_generation="profile-generation",
        enforce=True,
    )
    final_task = asyncio.create_task(
        runtime._handle_provider_final(
            key,
            "poisoned-fifo-forward",
            runtime._asr_session_epoch,
            "qwen",
        )
    )
    for _ in range(10):
        if any(
            isinstance(item.event, ProviderFinalReceived)
            for item in transaction.event_queue
        ):
            break
        await asyncio.sleep(0)
    assert any(
        isinstance(item.event, ProviderFinalReceived)
        for item in transaction.event_queue
    )

    apply_release.set()
    await final_task
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    settled_record = await runtime._asr_admission.get_record(
        transaction.turn_token
    )
    assert callbacks.on_final.await_count == 1, (
        settled_record.capture_state if settled_record else None,
        settled_record.evidence_state if settled_record else None,
        settled_record.admission_state if settled_record else None,
        settled_record.terminal_disposition if settled_record else None,
        settled_record.pending_final if settled_record else None,
        transaction.resolved_disposition,
        {
            turn: context.settled.is_set()
            for turn, context in runtime._asr_admission_final_contexts.items()
        },
    )
    assert callbacks.on_final.await_args.args[0].text == "poisoned-fifo-forward"
    assert not runtime._asr_admission_final_contexts
    assert transaction.resolved_disposition is AdmissionDisposition.FORWARD
    assert lifecycle.snapshot.state is VoiceLifecycleState.WARM_IDLE
    session.close.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_exact_interval_drop_preserves_successor_for_new_provider_turn(
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        lifecycle,
        first_turn,
        key_a,
        lease_token,
        target,
        successor,
    ) = await _install_exact_provider_interval(runtime, detector)
    assert successor is not None
    assert successor not in runtime._asr_admission_candidate_turns

    key_b = ProviderUtteranceKey(0, 0, 2)
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(
            0,
            0,
            2,
            audio_start_sample_16k=160,
        ),
        runtime._asr_session_epoch,
    )
    second_turn = runtime._asr_provider_started_turns[key_b]
    assert second_turn != first_turn
    assert runtime._asr_admission_turn_leases[second_turn] == lease_token

    await runtime._handle_provider_final(
        key_a,
        "drop-a",
        runtime._asr_session_epoch,
        "qwen",
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(target, 2, SpeakerCheckpointKind.COMPLETION_CONFIRMATION),
        activation_generation="profile-generation",
        enforce=True,
    )
    await _drain_runtime_admission(runtime)
    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert lifecycle.current_turn_token == second_turn

    detector.lease = _RejectionLease(detector, second_turn)
    assert runtime._accept_speaker_evidence_fact(
        SpeakerHigh(successor, 3),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert runtime._close_speaker_evidence(
        CaptureClosed(successor, 3),
        activation_generation="profile-generation",
        enforce=True,
        evidence_complete=True,
    )
    await _drain_runtime_admission(runtime)
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="boundary",
            generation=0,
            buffer_epoch=0,
            utterance_id=2,
            boundary_quality="unknown",
            audio_range=None,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="ordered",
            generation=0,
            buffer_epoch=0,
            utterance_id=2,
            boundary_quality="unknown",
            audio_range=None,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_final(
        key_b,
        "forward-b",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    assert [item.args[0].text for item in callbacks.on_final.await_args_list] == [
        "forward-b"
    ]
    session.close.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_exact_interval_pending_replays_final_then_ordered_without_loss(
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        lifecycle,
        _turn_token,
        key,
        _lease_token,
        _evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    detector.block_exact_prepare = True
    boundary_notification = ProviderEndpointNotification(
        phase="boundary",
        generation=0,
        buffer_epoch=0,
        utterance_id=1,
        boundary_quality="exact",
        audio_range=boundary,
    )
    ordered_notification = ProviderEndpointNotification(
        phase="ordered",
        generation=0,
        buffer_epoch=0,
        utterance_id=1,
        boundary_quality="exact",
        audio_range=boundary,
    )
    boundary_task = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(
            boundary_notification,
            runtime._asr_session_epoch,
        )
    )
    await detector.exact_prepare_entered.wait()
    duplicate_task = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(
            boundary_notification,
            runtime._asr_session_epoch,
        )
    )
    await runtime._handle_provider_final(
        key,
        "queued-final",
        runtime._asr_session_epoch,
        "qwen",
    )
    await runtime._handle_provider_endpoint_notification(
        ordered_notification,
        runtime._asr_session_epoch,
    )
    assert len(runtime._asr_provider_exact_pending[key].deferred) == 2

    detector.exact_prepare_release.set()
    await asyncio.gather(boundary_task, duplicate_task)
    transaction = runtime._asr_provider_exact_intervals[key]
    assert lifecycle.snapshot.state is VoiceLifecycleState.DRAINING
    assert runtime._close_speaker_evidence(
        CaptureClosed(transaction.target_candidate, 2),
        activation_generation="profile-generation",
        enforce=True,
        evidence_complete=True,
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_final.await_args.args[0].text == "queued-final"
    session.close.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_exact_interval_cancel_during_replay_drains_remaining_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        _lifecycle,
        _turn_token,
        key,
        _lease_token,
        _evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    detector.block_exact_prepare = True
    replay_entered = asyncio.Event()
    replay_release = asyncio.Event()
    original_final = runtime._handle_provider_final

    async def gated_final(*args, **kwargs):
        if key not in runtime._asr_provider_exact_pending:
            replay_entered.set()
            await replay_release.wait()
        return await original_final(*args, **kwargs)

    monkeypatch.setattr(runtime, "_handle_provider_final", gated_final)
    boundary_task = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=boundary,
            ),
            runtime._asr_session_epoch,
        )
    )
    await detector.exact_prepare_entered.wait()
    await runtime._handle_provider_final(
        key,
        "replay-after-cancel",
        runtime._asr_session_epoch,
        "qwen",
    )
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="ordered",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=boundary,
        ),
        runtime._asr_session_epoch,
    )
    pending = runtime._asr_provider_exact_pending[key]
    detector.exact_prepare_release.set()
    await replay_entered.wait()
    boundary_task.cancel()
    replay_release.set()
    with pytest.raises(asyncio.CancelledError):
        await boundary_task

    assert not pending.deferred
    transaction = runtime._asr_provider_exact_intervals[key]
    assert runtime._close_speaker_evidence(
        CaptureClosed(transaction.target_candidate, 2),
        activation_generation="profile-generation",
        enforce=True,
        evidence_complete=True,
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()
    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_final.await_args.args[0].text == "replay-after-cancel"
    session.close.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_exact_interval_commit_order_is_prepare_promote_activate_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    (
        _session,
        _lifecycle,
        _turn_token,
        key,
        _lease_token,
        _evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    order: list[str] = []
    original_prepare = detector.prepare_provider_exact_speaker_interval
    original_promote = runtime._asr_admission_ingress.promote_exact_interval_nowait
    original_activate = runtime._asr_admission_ingress.activate_exact_interval_nowait
    original_commit = detector.commit_provider_exact_speaker_interval

    async def traced_prepare(*args, **kwargs):
        order.append("detector_prepare")
        return await original_prepare(*args, **kwargs)

    def traced_promote(scope):
        order.append("admission_promote")
        return original_promote(scope)

    def traced_activate(receipt):
        order.append("admission_activate")
        return original_activate(receipt)

    def traced_commit(reservation):
        order.append("detector_commit")
        return original_commit(reservation)

    monkeypatch.setattr(
        detector,
        "prepare_provider_exact_speaker_interval",
        traced_prepare,
    )
    monkeypatch.setattr(
        runtime._asr_admission_ingress,
        "promote_exact_interval_nowait",
        traced_promote,
    )
    monkeypatch.setattr(
        runtime._asr_admission_ingress,
        "activate_exact_interval_nowait",
        traced_activate,
    )
    monkeypatch.setattr(
        detector,
        "commit_provider_exact_speaker_interval",
        traced_commit,
    )

    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="boundary",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=boundary,
        ),
        runtime._asr_session_epoch,
    )

    assert order == [
        "detector_prepare",
        "admission_promote",
        "admission_activate",
        "detector_commit",
    ]
    assert key in runtime._asr_provider_exact_intervals
    assert runtime._asr_provider_speaker_key_ledgers[key].state is (
        runtime_module._ProviderSpeakerLedgerState.EXACT_DRAINING
    )
    await _close_dispatchers(runtime)


@pytest.mark.parametrize(
    "method_name",
    ("promote_exact_interval_nowait", "activate_exact_interval_nowait"),
)
async def test_exact_interval_cancelled_waiter_compensates_staged_split(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    (
        session,
        _lifecycle,
        turn_token,
        key,
        lease_token,
        evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = getattr(runtime._asr_admission_ingress, method_name)

    def delayed_nowait(argument):
        inner = original(argument)

        async def wait_after_fifo_commit():
            result = await inner
            entered.set()
            await release.wait()
            return result

        return asyncio.create_task(wait_after_fifo_commit())

    monkeypatch.setattr(
        runtime._asr_admission_ingress,
        method_name,
        delayed_nowait,
    )
    task = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=boundary,
            ),
            runtime._asr_session_epoch,
        )
    )
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert key not in runtime._asr_provider_exact_intervals
    assert key not in runtime._asr_provider_exact_pending
    assert detector.exact_abort_calls == 1
    parent = await runtime._asr_admission.get_speaker_lease(lease_token)
    child = await runtime._asr_admission.get_record(turn_token)
    assert parent is not None
    assert parent.candidate == evidence.candidate
    assert child is not None
    assert child.speaker_lease_token == lease_token
    assert child.exact_interval_hold_id is None
    session.close.assert_not_awaited()
    await _close_dispatchers(runtime)


@pytest.mark.parametrize("failed_side", ("detector", "admission"))
async def test_exact_interval_failed_pcm_rollback_marks_speaker_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    failed_side: str,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        _lifecycle,
        _turn_token,
        key,
        _lease_token,
        _evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_promote = runtime._asr_admission_ingress.promote_exact_interval_nowait

    def delayed_promote(scope):
        inner = original_promote(scope)

        async def wait_after_fifo_commit():
            result = await inner
            entered.set()
            await release.wait()
            return result

        return asyncio.create_task(wait_after_fifo_commit())

    monkeypatch.setattr(
        runtime._asr_admission_ingress,
        "promote_exact_interval_nowait",
        delayed_promote,
    )
    if failed_side == "detector":
        detector.exact_abort_result = False
    else:

        def conflict_abort(_receipt):
            future = asyncio.get_running_loop().create_future()
            future.set_result(
                ExactIntervalAbortResult(ExactIntervalOutcome.CONFLICT)
            )
            return future

        monkeypatch.setattr(
            runtime._asr_admission_ingress,
            "abort_exact_interval_promotion_nowait",
            conflict_abort,
        )

    task = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=boundary,
            ),
            runtime._asr_session_epoch,
        )
    )
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _drain_runtime_admission(runtime)

    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    session.close.assert_not_awaited()
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="ordered",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="unknown",
            audio_range=None,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_final(
        key,
        f"{failed_side}-rollback-forward",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()
    callbacks.on_final.assert_awaited_once()
    assert (
        callbacks.on_final.await_args.args[0].text
        == f"{failed_side}-rollback-forward"
    )
    await _close_dispatchers(runtime)


@pytest.mark.parametrize("cancel_wait", (False, True))
async def test_exact_interval_preseal_unavailable_reaches_bounded_forward(
    cancel_wait: bool,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        _lifecycle,
        _turn_token,
        key,
        _lease_token,
        _evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    preseal_entered = asyncio.Event()
    preseal_release = asyncio.Event()

    async def wait_preseal(*_args, **_kwargs) -> bool:
        preseal_entered.set()
        if cancel_wait:
            await preseal_release.wait()
        return False

    detector.wait_provider_speaker_preseal = AsyncMock(side_effect=wait_preseal)
    boundary_task = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=boundary,
            ),
            runtime._asr_session_epoch,
        )
    )
    await preseal_entered.wait()
    if cancel_wait:
        boundary_task.cancel()
        preseal_release.set()
        with pytest.raises(asyncio.CancelledError):
            await boundary_task
    else:
        await boundary_task

    transaction = runtime._asr_provider_exact_intervals[key]
    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="ordered",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=boundary,
        ),
        runtime._asr_session_epoch,
    )
    await runtime._handle_provider_final(
        key,
        "unavailable-forward",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_final.await_args.args[0].text == "unavailable-forward"
    assert transaction.resolved_disposition is AdmissionDisposition.FORWARD
    session.close.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_exact_interval_reset_blocks_reconnect_takeover_while_unsettled(
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, *_rest = await _install_exact_provider_interval(
        runtime,
        detector,
        ordered=False,
    )
    intervals = dict(runtime._asr_provider_exact_intervals)
    candidates = dict(runtime._asr_provider_exact_candidates)

    with pytest.raises(RuntimeError, match="ASR_EXACT_INTERVAL_RESET_UNSETTLED"):
        runtime._reset_asr_provider_transport_namespace(
            retire_owned_proofs=True,
        )

    assert runtime._asr_session is session
    assert runtime._asr_provider_exact_intervals == intervals
    assert runtime._asr_provider_exact_candidates == candidates
    assert runtime._asr_deny_cleanup_active is False
    session.close.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_exact_interval_reset_blocks_while_resolution_effect_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    (
        session,
        _lifecycle,
        _turn_token,
        key,
        _lease_token,
        target,
        _successor,
    ) = await _install_exact_provider_interval(runtime, detector)
    transaction = runtime._asr_provider_exact_intervals[key]
    effect_entered = asyncio.Event()
    effect_release = asyncio.Event()
    original_execute = runtime._execute_admission_effect

    async def blocked_execute(effect):
        if isinstance(effect, ResolveReserved):
            effect_entered.set()
            await effect_release.wait()
        await original_execute(effect)

    monkeypatch.setattr(runtime, "_execute_admission_effect", blocked_execute)
    await runtime._handle_provider_final(
        key,
        "settle-before-reset",
        runtime._asr_session_epoch,
        "qwen",
    )
    assert runtime._close_speaker_evidence(
        CaptureClosed(target, 2),
        activation_generation="profile-generation",
        enforce=True,
        evidence_complete=True,
    )
    await effect_entered.wait()
    assert transaction.resolved_disposition is AdmissionDisposition.FORWARD
    assert runtime._asr_provider_exact_intervals[key] is transaction

    with pytest.raises(RuntimeError, match="ASR_EXACT_INTERVAL_RESET_UNSETTLED"):
        runtime._reset_asr_provider_transport_namespace(
            retire_owned_proofs=True,
        )

    assert runtime._asr_session is session
    assert runtime._asr_provider_exact_intervals[key] is transaction
    effect_release.set()
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()
    assert key not in runtime._asr_provider_exact_intervals
    session.close.assert_not_awaited()
    await _close_dispatchers(runtime)


async def test_exact_interval_different_pending_boundary_fails_open_for_text(
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    (
        session,
        _lifecycle,
        turn_token,
        key,
        _lease_token,
        _evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    detector.block_exact_prepare = True
    first = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=boundary,
            ),
            runtime._asr_session_epoch,
        )
    )
    await detector.exact_prepare_entered.wait()
    conflict = asyncio.create_task(
        runtime._handle_provider_endpoint_notification(
            ProviderEndpointNotification(
                phase="boundary",
                generation=0,
                buffer_epoch=0,
                utterance_id=1,
                boundary_quality="exact",
                audio_range=ProviderAudioRange(0, 320),
            ),
            runtime._asr_session_epoch,
        )
    )
    await asyncio.sleep(0)
    detector.exact_prepare_release.set()
    await asyncio.gather(first, conflict)
    await runtime._handle_provider_final(
        key,
        "boundary-conflict-forward",
        runtime._asr_session_epoch,
        "qwen",
    )
    await _drain_runtime_admission(runtime)
    await runtime.wait_transcript_idle()

    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    session.close.assert_not_awaited()
    assert await runtime._asr_admission.get_record(turn_token) is None
    callbacks.on_final.assert_awaited_once()
    assert callbacks.on_final.await_args.args[0].text == "boundary-conflict-forward"
    await _close_dispatchers(runtime)


async def test_exact_interval_capacity_counts_pending_transactions() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    (
        _session,
        _lifecycle,
        _turn_token,
        key,
        _lease_token,
        _evidence,
        boundary,
    ) = await _install_exact_provider_parent(runtime, detector)
    for utterance_id in range(20, 20 + runtime_module._MAX_PROVIDER_BOUNDARY_SNAPSHOTS):
        pending_key = ProviderUtteranceKey(0, 0, utterance_id)
        runtime._asr_provider_exact_pending[pending_key] = (
            runtime_module._ProviderExactIntervalPending(boundary)
        )

    await runtime._handle_provider_endpoint_notification(
        ProviderEndpointNotification(
            phase="boundary",
            generation=0,
            buffer_epoch=0,
            utterance_id=1,
            boundary_quality="exact",
            audio_range=boundary,
        ),
        runtime._asr_session_epoch,
    )

    assert key not in runtime._asr_provider_exact_intervals
    assert detector.exact_prepare_entered.is_set() is False
    assert runtime._speaker_rejection_metrics[
        "provider_boundary_unknown_ready_count"
    ] == 1
    await _close_dispatchers(runtime)


async def test_exact_interval_drop_rejects_existing_forward_tombstone(
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    (
        session,
        _lifecycle,
        turn_token,
        key,
        _lease_token,
        target,
        _successor,
    ) = await _install_exact_provider_interval(runtime, detector)
    transaction = runtime._asr_provider_exact_intervals[key]
    dispatcher = runtime._asr_admission_reservation_dispatchers[
        FinalKey.from_turn(turn_token)
    ]
    dispatcher.resolve_reserved = MagicMock(
        return_value=TranscriptResolutionReceipt(
            FinalKey.from_turn(turn_token),
            AdmissionDisposition.DROP,
            TranscriptResolutionOutcome.ALREADY_SAME,
            AdmissionDisposition.FORWARD,
        )
    )
    await runtime._handle_provider_final(
        key,
        "must-not-be-declared-dropped",
        runtime._asr_session_epoch,
        "qwen",
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(target, 2, SpeakerCheckpointKind.COMPLETION_CONFIRMATION),
        activation_generation="profile-generation",
        enforce=True,
    )
    await _drain_runtime_admission(runtime)

    assert transaction.drop_tombstone_succeeded is False
    assert runtime._asr_deny_transport_state is not DenyTransportState.OPEN
    session.close.assert_awaited()
    await _close_dispatchers(runtime)


async def test_provider_final_callback_does_not_wait_for_speaker_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, _turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    key = ProviderUtteranceKey(0, 0, 1)
    assert runtime._accept_provider_timeline(key)
    runtime._asr_sealed_provider_key = key
    unsettled = asyncio.Event()
    handle_final = AsyncMock(return_value=unsettled)
    monkeypatch.setattr(runtime, "_handle_independent_asr_final", handle_final)

    await asyncio.wait_for(
        runtime._handle_provider_final(
            key,
            "reserved",
            runtime._asr_session_epoch,
            "qwen",
        ),
        timeout=0.1,
    )

    handle_final.assert_awaited_once()
    assert unsettled.is_set() is False
    await _close_dispatchers(runtime)


async def test_provider_started_after_final_reservation_activates_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    _session, lifecycle, first_turn, _provider_fence = (
        await _seal_provider_candidate(runtime, detector)
    )
    runtime._speaker_verifier_enforces_admission = False
    settle_entered = asyncio.Event()
    settle_release = asyncio.Event()
    original_settle = runtime._settle_admission_final

    async def block_settle(ticket, context) -> None:
        settle_entered.set()
        await settle_release.wait()
        await original_settle(ticket, context)

    monkeypatch.setattr(runtime, "_settle_admission_final", block_settle)

    settlement = await runtime._handle_independent_asr_final(
        "first",
        runtime._asr_session_epoch,
        "qwen",
    )
    assert settlement is not None
    await asyncio.wait_for(settle_entered.wait(), 1)
    key_b = ProviderUtteranceKey(0, 0, 2)
    await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 2),
        runtime._asr_session_epoch,
    )
    second_turn = runtime._asr_provider_started_turns[key_b]
    assert second_turn != first_turn

    settle_release.set()
    await asyncio.wait_for(settlement.wait(), 1)
    await runtime._asr_audio_dispatcher.wait_idle()

    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert lifecycle.current_turn_token == second_turn
    assert runtime._asr_partial_turn_token == second_turn
    await _close_dispatchers(runtime)


async def test_provider_gate_timeout_forwards_final_and_rejects_late_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch
    coordinator, lane, turn_token, candidate, provider_key = await _open_admission_turn(
        provider_bound=True
    )
    assert provider_key is not None
    capability = _admission_capability(
        turn_token,
        candidate,
        provider_key=provider_key,
        kind=RejectionCapabilityKind.SEALED,
    )
    try:
        await lane.post(
            turn_token,
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        )
        boundary_effects = await lane.post(turn_token, BoundaryExact(capability))
        assert not any(
            isinstance(effect, ApplyRejection) for effect in boundary_effects
        )
        final_effects = await lane.post(
            turn_token,
            ProviderFinalReceived(_admission_final(provider_key)),
            now=10.0,
        )
        deadline = next(
            effect
            for effect in final_effects
            if isinstance(effect, ScheduleFinalDeadline)
        )
        assert deadline.absolute_deadline == 10.2

        timeout_effects = await lane.post(
            turn_token,
            FinalDeadlineExpired(deadline.ticket, deadline.absolute_deadline),
            now=deadline.absolute_deadline,
        )
        assert not any(
            isinstance(effect, ResolveReserved) for effect in timeout_effects
        )
        pending = await coordinator.get_record(turn_token)
        assert pending is not None
        assert pending.admission_state is AdmissionState.PENDING

        denied_effects = await lane.post(
            turn_token,
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
            now=10.21,
        )
        assert [
            effect.disposition
            for effect in denied_effects
            if isinstance(effect, ResolveReserved)
        ] == [AdmissionDisposition.DROP]
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.DROPPED
    finally:
        await lane.close()


async def test_smart_turn_rejection_does_not_require_provider_gate() -> None:
    candidate = _smart_turn_shadow_candidate()
    coordinator, lane, turn_token, _, provider_key = await _open_admission_turn(
        provider_bound=False,
        candidate=candidate,
    )
    assert provider_key is None
    capability = _admission_capability(
        turn_token,
        candidate,
        provider_key=None,
        kind=RejectionCapabilityKind.ACTIVE,
    )
    try:
        await lane.post(
            turn_token,
            SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        )
        denied_effects = await lane.post(
            turn_token,
            SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        )
        boundary_effects = await lane.post(turn_token, BoundaryExact(capability))
        assert not any(
            isinstance(effect, ApplyRejection) for effect in boundary_effects
        )

        assert any(
            isinstance(effect, AbortProviderTransport) for effect in denied_effects
        )
        assert [
            effect.disposition
            for effect in denied_effects
            if isinstance(effect, ResolveReserved)
        ] == [AdmissionDisposition.DROP]
        record = await coordinator.get_record(turn_token)
        assert record is not None
        assert record.admission_state is AdmissionState.DROPPED
    finally:
        await lane.close()


@pytest.mark.parametrize(
    ("events", "endpointing_available"),
    (
        ((SpeechActivityEvent.SPEECH_STARTED,), True),
        ((SpeechActivityEvent.SPEECH_RESUMED,), True),
        ((SpeechActivityEvent.CANDIDATE_PAUSE,), False),
    ),
)
async def test_deny_wait_silence_requires_available_silero_pause(
    events: tuple[SpeechActivityEvent, ...],
    endpointing_available: bool,
) -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=events,
            throttle_available=True,
            endpointing_available=endpointing_available,
        )
    )
    runtime._asr_deny_transport_state = DenyTransportState.WAIT_SILENCE
    runtime._asr_rearm_cutoff_sequence = 0
    runtime._asr_rearm_last_sequence = 0
    runtime._asr_rearm_last_captured_at = 0.0

    result = await runtime.submit(
        ProcessedVoiceFrame(
            b"\x01\x00" * 160,
            16_000,
            0.9,
            False,
            ingress_sequence=1,
            captured_at=1.0,
        ),
        ingress_token=turn_token.ingress,
    )

    assert result.status is AsrSubmitStatus.ACCEPTED
    assert runtime._asr_deny_transport_state is DenyTransportState.WAIT_SILENCE
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_deny_rearm_rejects_gap_then_arms_on_contiguous_silero_pause() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.CANDIDATE_PAUSE,),
            throttle_available=True,
        )
    )
    runtime._asr_deny_transport_state = DenyTransportState.WAIT_SILENCE
    runtime._asr_rearm_cutoff_sequence = 10
    runtime._asr_rearm_last_sequence = 10
    runtime._asr_rearm_last_captured_at = 10.0
    runtime._asr_last_ingress_sequence = 10
    runtime._asr_last_captured_at = 10.0

    await runtime.submit(
        ProcessedVoiceFrame(
            b"\x01\x00" * 160,
            16_000,
            0.0,
            False,
            ingress_sequence=12,
            captured_at=12.0,
        ),
        ingress_token=turn_token.ingress,
    )
    assert runtime._asr_deny_transport_state is DenyTransportState.WAIT_SILENCE
    detector.feed.assert_not_awaited()

    await runtime.submit(
        ProcessedVoiceFrame(
            b"\x00\x00" * 160,
            16_000,
            0.0,
            False,
            ingress_sequence=13,
            captured_at=13.0,
        ),
        ingress_token=turn_token.ingress,
    )
    assert runtime._asr_deny_transport_state is DenyTransportState.ARMED
    detector.prepare_deny_rearm.assert_awaited_once_with(
        cleanup_generation=runtime._asr_deny_cleanup_generation,
        cutoff_sequence=12,
        expected_detector_epoch=detector.detector_epoch,
    )
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_deny_rearm_silence_then_onset_forwards_same_frame() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    onset_identity = DetectorIngressIdentity(
        ingress_token=turn_token.ingress,
        detector_epoch=detector.detector_epoch,
        sequence_no=2,
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        side_effect=(
            DetectorFeedResult(
                events=(SpeechActivityEvent.CANDIDATE_PAUSE,),
                throttle_available=True,
            ),
            DetectorFeedResult(
                events=(SpeechActivityEvent.SPEECH_RESUMED,),
                throttle_available=True,
            ),
            DetectorFeedResult(
                events=(SpeechActivityEvent.SPEECH_RESUMED,),
                throttle_available=True,
                identity=onset_identity,
                candidate=DetectorCandidateKey(detector.detector_epoch, 11),
            ),
        )
    )
    runtime._asr_deny_transport_state = DenyTransportState.WAIT_SILENCE
    runtime._asr_rearm_cutoff_sequence = 0
    runtime._asr_rearm_last_sequence = 0
    runtime._asr_rearm_last_captured_at = 0.0

    silence = await runtime.submit(
        ProcessedVoiceFrame(
            b"\x00\x00" * 160,
            16_000,
            0.0,
            False,
            ingress_sequence=1,
            captured_at=1.0,
        ),
        ingress_token=turn_token.ingress,
    )
    owner_pcm = b"\x21\x00" * 160
    owner = await runtime.submit(
        ProcessedVoiceFrame(
            owner_pcm,
            16_000,
            0.9,
            True,
            ingress_sequence=2,
            captured_at=2.0,
        ),
        ingress_token=turn_token.ingress,
    )
    await runtime._asr_audio_dispatcher.wait_idle()

    assert silence.status is AsrSubmitStatus.ACCEPTED
    assert owner.status is AsrSubmitStatus.ACCEPTED
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    assert detector.feed.await_count == 3
    assert detector.feed.await_args_list[1].args[0] == owner_pcm
    assert detector.feed.await_args_list[2].args[0] == owner_pcm
    session.stream_audio.assert_awaited()
    assert owner_pcm in session.stream_audio.await_args.args[0]
    await _close_dispatchers(runtime)


async def test_pre_exact_lows_do_not_close_or_rebuild_provider_session() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    denied_detector = _RejectionDetector()
    denied_session, lifecycle, denied_turn = _install_active_candidate(
        runtime,
        denied_detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(denied_turn)
    denied_candidate = runtime._asr_current_speaker_candidate
    assert denied_candidate is not None
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(denied_candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(denied_candidate, 2, SpeakerCheckpointKind.SECOND),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 1),
        runtime._asr_session_epoch,
    )
    await _drain_runtime_admission(runtime)

    denied_session.close.assert_not_awaited()
    assert runtime._asr_session is denied_session
    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    assert runtime._callbacks.on_partial.await_count == 0
    assert runtime._callbacks.on_final.await_count == 0
    await _close_dispatchers(runtime)


async def test_deny_rearm_stale_high_sequence_cannot_poison_current_ingress() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.CANDIDATE_PAUSE,),
            throttle_available=True,
        )
    )
    runtime._asr_deny_transport_state = DenyTransportState.WAIT_SILENCE
    runtime._asr_rearm_cutoff_sequence = 10
    runtime._asr_rearm_last_sequence = 10
    runtime._asr_rearm_last_captured_at = 10.0
    runtime._asr_last_ingress_sequence = 10
    runtime._asr_last_captured_at = 10.0
    current = turn_token.ingress
    stale = VoiceIngressToken(
        session_epoch=current.session_epoch,
        connection_id="old-connection",
        lease_generation=max(0, current.lease_generation - 1),
        route_generation=max(0, current.route_generation - 1),
        audio_generation=current.audio_generation,
    )

    stale_result = await runtime.submit(
        ProcessedVoiceFrame(
            b"\x7f\x00" * 160,
            16_000,
            0.0,
            False,
            ingress_sequence=999,
            captured_at=999.0,
        ),
        ingress_token=stale,
    )
    current_result = await runtime.submit(
        ProcessedVoiceFrame(
            b"\x00\x00" * 160,
            16_000,
            0.0,
            False,
            ingress_sequence=11,
            captured_at=11.0,
        ),
        ingress_token=current,
    )

    assert stale_result.status is AsrSubmitStatus.STALE
    assert current_result.status is AsrSubmitStatus.ACCEPTED
    assert runtime._asr_last_ingress_sequence == 11
    assert runtime._asr_rearm_cutoff_sequence == 10
    assert runtime._asr_deny_transport_state is DenyTransportState.ARMED
    detector.feed.assert_awaited_once()
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_deny_rearm_late_prepare_cannot_arm_new_cleanup() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    prepare_entered = asyncio.Event()
    prepare_release = asyncio.Event()

    async def block_prepare(**_kwargs) -> bool:
        prepare_entered.set()
        await prepare_release.wait()
        return True

    detector.prepare_deny_rearm.side_effect = block_prepare
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.CANDIDATE_PAUSE,),
            throttle_available=True,
        )
    )
    runtime._asr_deny_transport_state = DenyTransportState.WAIT_SILENCE
    runtime._asr_rearm_cutoff_sequence = 0
    runtime._asr_rearm_last_sequence = 0
    runtime._asr_rearm_last_captured_at = 0.0

    pending = asyncio.create_task(
        runtime.submit(
            ProcessedVoiceFrame(
                b"\x00\x00" * 160,
                16_000,
                0.0,
                False,
                ingress_sequence=1,
                captured_at=1.0,
            ),
            ingress_token=turn_token.ingress,
        )
    )
    await asyncio.wait_for(prepare_entered.wait(), 1)
    runtime._asr_deny_cleanup_generation += 1
    runtime._asr_rearm_cutoff_sequence = 1
    prepare_release.set()
    result = await asyncio.wait_for(pending, 1)

    assert result.status is AsrSubmitStatus.ACCEPTED
    assert runtime._asr_deny_transport_state is DenyTransportState.WAIT_SILENCE
    detector.feed.assert_not_awaited()
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_deny_rearm_late_feed_from_replaced_detector_cannot_arm() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    _session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    feed_entered = asyncio.Event()
    feed_release = asyncio.Event()

    async def block_feed(*_args, **_kwargs) -> DetectorFeedResult:
        feed_entered.set()
        await feed_release.wait()
        return DetectorFeedResult(
            events=(SpeechActivityEvent.CANDIDATE_PAUSE,),
            throttle_available=True,
        )

    detector.feed = AsyncMock(side_effect=block_feed)  # type: ignore[attr-defined]
    runtime._asr_deny_transport_state = DenyTransportState.WAIT_SILENCE
    runtime._asr_rearm_cutoff_sequence = 0
    runtime._asr_rearm_last_sequence = 0
    runtime._asr_rearm_last_captured_at = 0.0

    pending = asyncio.create_task(
        runtime.submit(
            ProcessedVoiceFrame(
                b"\x00\x00" * 160,
                16_000,
                0.0,
                False,
                ingress_sequence=1,
                captured_at=1.0,
            ),
            ingress_token=turn_token.ingress,
        )
    )
    await asyncio.wait_for(feed_entered.wait(), 1)
    runtime._asr_detector = _RejectionDetector()
    feed_release.set()
    result = await asyncio.wait_for(pending, 1)

    assert result.status is AsrSubmitStatus.ACCEPTED
    assert runtime._asr_deny_transport_state is DenyTransportState.WAIT_SILENCE
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_deny_rearm_onset_cannot_forward_after_new_cleanup_takes_over() -> None:
    runtime = IndependentAsrRuntime(_callbacks())
    detector = _RejectionDetector()
    session, _lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    detector.feed = AsyncMock(  # type: ignore[attr-defined]
        return_value=DetectorFeedResult(
            events=(SpeechActivityEvent.SPEECH_RESUMED,),
            throttle_available=True,
        )
    )
    runtime._asr_deny_transport_state = DenyTransportState.ARMED
    runtime._asr_rearm_cutoff_sequence = 0
    runtime._asr_rearm_last_sequence = 0
    runtime._asr_rearm_last_captured_at = 0.0

    async def supersede_during_activity(*_args, **_kwargs) -> bool:
        runtime._asr_deny_cleanup_generation += 1
        runtime._asr_deny_transport_state = DenyTransportState.DENY_FENCED
        return True

    runtime._handle_independent_asr_activity = AsyncMock(  # type: ignore[method-assign]
        side_effect=supersede_during_activity
    )

    result = await runtime.submit(
        ProcessedVoiceFrame(
            b"\x2b\x00" * 160,
            16_000,
            0.9,
            True,
            ingress_sequence=1,
            captured_at=1.0,
        ),
        ingress_token=turn_token.ingress,
    )

    assert result.status is AsrSubmitStatus.ACCEPTED
    assert runtime._asr_deny_transport_state is DenyTransportState.DENY_FENCED
    detector.feed.assert_awaited_once()
    session.stream_audio.assert_not_awaited()
    runtime._asr_deny_transport_state = DenyTransportState.OPEN
    await _close_dispatchers(runtime)


async def test_pre_exact_lows_never_invoke_provider_close() -> None:
    callbacks = _callbacks()
    runtime = IndependentAsrRuntime(callbacks)
    detector = _RejectionDetector()
    session, lifecycle, turn_token = _install_active_candidate(
        runtime,
        detector,
        provider="qwen",
        endpointing_mode="provider",
    )
    session.close.side_effect = RuntimeError("forced close failure")
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    assert await runtime._arm_speaker_authority_for_provider_audio(turn_token)
    candidate = runtime._asr_current_speaker_candidate
    assert candidate is not None
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 1, SpeakerCheckpointKind.FIRST),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert runtime._accept_speaker_evidence_fact(
        SpeakerLow(candidate, 2, SpeakerCheckpointKind.SECOND),
        activation_generation="profile-generation",
        enforce=True,
    )
    assert await runtime._handle_provider_utterance_started(
        ProviderUtteranceStartedNotification(0, 0, 1),
        runtime._asr_session_epoch,
    )
    await _drain_runtime_admission(runtime)

    session.close.assert_not_awaited()
    assert runtime._asr_session is session
    assert runtime._asr_deny_transport_state is DenyTransportState.OPEN
    assert lifecycle.snapshot.state is VoiceLifecycleState.ACTIVE
    callbacks.on_failure.assert_not_awaited()
    assert all(
        item.args[0].code == "ASR_SPEAKER_EVIDENCE_UNAVAILABLE"
        for item in callbacks.on_status.await_args_list
    )
    await _close_dispatchers(runtime)
