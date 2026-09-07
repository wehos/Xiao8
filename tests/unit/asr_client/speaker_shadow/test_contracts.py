from __future__ import annotations

import pytest

from main_logic.asr_client.speaker_shadow.contracts import (
    MAX_SPEAKER_SHADOW_BUFFERED_CANDIDATES,
    MAX_SPEAKER_SHADOW_CALLBACK_TIMEOUT_SECONDS,
    MAX_SPEAKER_SHADOW_CANDIDATE_PCM_BYTES,
    MAX_SPEAKER_SHADOW_COMPLETION_QUEUE_CAPACITY,
    MAX_SPEAKER_SHADOW_FRAME_PCM_BYTES,
    MAX_SPEAKER_SHADOW_FINALIZED_CANDIDATES,
    MAX_SPEAKER_SHADOW_QUEUE_CAPACITY,
    MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES,
    MAX_SPEAKER_SHADOW_SHUTDOWN_GRACE_SECONDS,
    MAX_SPEAKER_SHADOW_TERMINAL_QUEUE_CAPACITY,
    MAX_SPEAKER_SHADOW_THRESHOLDS,
    SpeakerShadowCandidateReconciliationControl,
    SpeakerShadowCandidateKey,
    SpeakerShadowBatchReconciliationControl,
    SpeakerShadowBatchReconcileReceipt,
    SpeakerShadowBatchReconcileRequest,
    SpeakerShadowCaptureDecisionState,
    SpeakerShadowCaptureDisposition,
    SpeakerShadowCaptureResult,
    SpeakerShadowCaptureStatus,
    SpeakerShadowConfig,
    SpeakerShadowDecisionStatus,
    SpeakerShadowDeferredCandidateControl,
    SpeakerShadowDeferredCandidateStatus,
    SpeakerShadowObservation,
    SpeakerShadowReconcileSource,
    SpeakerShadowReconciliationSettlement,
    SpeakerShadowMetrics,
    SpeakerShadowTerminalCoverageControl,
    SpeakerShadowTerminalCoverageReceipt,
    SpeakerShadowTerminalCoverageRequest,
)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"similarity_thresholds": ()}, "similarity_thresholds"),
        ({"similarity_thresholds": (0.5, 0.4)}, "similarity_thresholds"),
        ({"minimum_audio_ms": 0}, "minimum_audio_ms"),
        (
            {"minimum_audio_ms": 2_000, "maximum_audio_ms": 1_000},
            "maximum_audio_ms",
        ),
        ({"maximum_audio_ms": 4_001}, "maximum_audio_ms"),
        ({"observation_checkpoints_ms": ()}, "observation_checkpoints_ms"),
        (
            {"observation_checkpoints_ms": (1_500, 1_500)},
            "observation_checkpoints_ms",
        ),
        (
            {"observation_checkpoints_ms": (1_499, 3_000)},
            "observation_checkpoints_ms",
        ),
        (
            {"observation_checkpoints_ms": (1_500, 4_001)},
            "observation_checkpoints_ms",
        ),
        ({"queue_capacity": 0}, "queue_capacity"),
        (
            {"queue_capacity": MAX_SPEAKER_SHADOW_QUEUE_CAPACITY + 1},
            "queue_capacity",
        ),
        ({"terminal_queue_capacity": 0}, "terminal_queue_capacity"),
        ({"terminal_queue_capacity": True}, "terminal_queue_capacity"),
        (
            {
                "terminal_queue_capacity": (
                    MAX_SPEAKER_SHADOW_TERMINAL_QUEUE_CAPACITY + 1
                )
            },
            "terminal_queue_capacity",
        ),
        ({"completion_queue_capacity": 0}, "completion_queue_capacity"),
        ({"completion_queue_capacity": 1.5}, "completion_queue_capacity"),
        (
            {
                "completion_queue_capacity": (
                    MAX_SPEAKER_SHADOW_COMPLETION_QUEUE_CAPACITY + 1
                )
            },
            "completion_queue_capacity",
        ),
        ({"buffered_candidate_capacity": 0}, "buffered_candidate_capacity"),
        (
            {
                "buffered_candidate_capacity": (
                    MAX_SPEAKER_SHADOW_BUFFERED_CANDIDATES + 1
                )
            },
            "buffered_candidate_capacity",
        ),
        (
            {"queue_capacity": 4, "finalized_candidate_capacity": 3},
            "finalized_candidate_capacity",
        ),
        (
            {
                "finalized_candidate_capacity": (
                    MAX_SPEAKER_SHADOW_FINALIZED_CANDIDATES + 1
                )
            },
            "finalized_candidate_capacity",
        ),
        (
            {
                "load_retry_initial_seconds": 2.0,
                "load_retry_max_seconds": 1.0,
            },
            "load_retry_max_seconds",
        ),
        ({"shutdown_grace_seconds": 0.0}, "shutdown_grace_seconds"),
        (
            {
                "shutdown_grace_seconds": (
                    MAX_SPEAKER_SHADOW_SHUTDOWN_GRACE_SECONDS + 0.1
                )
            },
            "shutdown_grace_seconds",
        ),
        ({"callback_timeout_seconds": 0.0}, "callback_timeout_seconds"),
        (
            {
                "callback_timeout_seconds": (
                    MAX_SPEAKER_SHADOW_CALLBACK_TIMEOUT_SECONDS + 0.1
                )
            },
            "callback_timeout_seconds",
        ),
        ({"backend_load_timeout_seconds": float("inf")}, "backend_load"),
        ({"backend_score_timeout_seconds": 0.0}, "backend_score"),
        ({"backend_close_timeout_seconds": 3.0}, "backend_close"),
        ({"process_terminate_timeout_seconds": 3.0}, "process_terminate"),
        (
            {
                "similarity_thresholds": tuple(
                    index / (MAX_SPEAKER_SHADOW_THRESHOLDS + 1)
                    for index in range(MAX_SPEAKER_SHADOW_THRESHOLDS + 1)
                )
            },
            "similarity_thresholds",
        ),
    ],
)
def test_config_rejects_unsafe_resource_bounds(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        SpeakerShadowConfig(**overrides)


def test_config_is_default_off_and_caps_candidate_audio() -> None:
    config = SpeakerShadowConfig()

    assert config.enabled is False
    assert config.maximum_audio_ms == 4_000
    assert config.observation_checkpoints_ms is None
    assert config.completion_confirmation_scopes == ()
    assert config.pending_observation_gate_scopes == ()
    assert config.backend_prewarm_scopes == ()
    assert config.terminal_queue_capacity == 512
    assert config.completion_queue_capacity == 512
    assert MAX_SPEAKER_SHADOW_FRAME_PCM_BYTES == 128_000
    assert MAX_SPEAKER_SHADOW_CANDIDATE_PCM_BYTES == 128_000
    assert MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES < 8 * 1024 * 1024


def test_config_accepts_provider_completion_confirmation_with_two_checkpoints() -> None:
    config = SpeakerShadowConfig(
        observation_checkpoints_ms=(1_500, 3_000),
        completion_confirmation_scopes=("provider_candidate",),
    )

    assert config.completion_confirmation_scopes == ("provider_candidate",)


def test_config_preserves_legacy_positional_argument_order() -> None:
    config = SpeakerShadowConfig(False, (0.40,), 1_500, 4_000, None, 60.0)

    assert config.idle_unload_seconds == 60.0
    assert config.completion_confirmation_scopes == ()
    assert tuple(SpeakerShadowConfig.__dataclass_fields__)[-6:] == (
        "completion_confirmation_scopes",
        "pending_observation_gate_scopes",
        "backend_prewarm_scopes",
        "terminal_queue_capacity",
        "completion_queue_capacity",
        "exact_boundary_pcm_retention_seconds",
    )


def test_config_accepts_bounded_terminal_and_completion_capacity_limits() -> None:
    config = SpeakerShadowConfig(
        terminal_queue_capacity=MAX_SPEAKER_SHADOW_TERMINAL_QUEUE_CAPACITY,
        completion_queue_capacity=MAX_SPEAKER_SHADOW_COMPLETION_QUEUE_CAPACITY,
    )

    assert config.terminal_queue_capacity == MAX_SPEAKER_SHADOW_TERMINAL_QUEUE_CAPACITY
    assert (
        config.completion_queue_capacity == MAX_SPEAKER_SHADOW_COMPLETION_QUEUE_CAPACITY
    )


def test_metrics_snapshot_exposes_terminal_delivery_aggregate_counters() -> None:
    metrics = SpeakerShadowMetrics(
        terminal_queued_count=1,
        terminal_overflow_count=2,
        terminal_abandoned_count=3,
        completion_dispatched_count=4,
        completion_attempted_count=5,
        completion_overflow_count=6,
        completion_abandoned_count=7,
        completion_stall_count=8,
        pending_terminal_count=9,
        pending_completion_count=10,
        detached_callback_task_count=11,
        delivery_degraded_count=12,
    )

    snapshot = metrics.snapshot()

    assert snapshot["terminal_queued_count"] == 1
    assert snapshot["terminal_overflow_count"] == 2
    assert snapshot["terminal_abandoned_count"] == 3
    assert snapshot["completion_dispatched_count"] == 4
    assert snapshot["completion_attempted_count"] == 5
    assert snapshot["completion_overflow_count"] == 6
    assert snapshot["completion_abandoned_count"] == 7
    assert snapshot["completion_stall_count"] == 8
    assert snapshot["pending_terminal_count"] == 9
    assert snapshot["pending_completion_count"] == 10
    assert snapshot["detached_callback_task_count"] == 11
    assert snapshot["delivery_degraded_count"] == 12


def test_config_accepts_nested_pending_gate_and_backend_prewarm_scopes() -> None:
    config = SpeakerShadowConfig(
        observation_checkpoints_ms=(1_500, 3_000),
        completion_confirmation_scopes=(
            "provider_candidate",
            "smart_turn_turn",
        ),
        pending_observation_gate_scopes=("provider_candidate",),
        backend_prewarm_scopes=("provider_candidate",),
    )

    assert config.pending_observation_gate_scopes == ("provider_candidate",)
    assert config.backend_prewarm_scopes == ("provider_candidate",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pending_observation_gate_scopes", ["provider_candidate"]),
        ("pending_observation_gate_scopes", ("unsupported",)),
        (
            "pending_observation_gate_scopes",
            ("provider_candidate", "provider_candidate"),
        ),
        ("backend_prewarm_scopes", ["provider_candidate"]),
        ("backend_prewarm_scopes", ("unsupported",)),
        (
            "backend_prewarm_scopes",
            ("provider_candidate", "provider_candidate"),
        ),
    ],
)
def test_config_rejects_invalid_pending_gate_or_prewarm_scopes(
    field: str,
    value: object,
) -> None:
    overrides: dict[str, object] = {
        "observation_checkpoints_ms": (1_500, 3_000),
        "completion_confirmation_scopes": ("provider_candidate",),
        "pending_observation_gate_scopes": ("provider_candidate",),
    }
    overrides[field] = value

    with pytest.raises(ValueError, match=field):
        SpeakerShadowConfig(**overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "completion_confirmation_scopes": ("provider_candidate",),
                "pending_observation_gate_scopes": ("smart_turn_turn",),
            },
            "pending_observation_gate_scopes",
        ),
        (
            {
                "completion_confirmation_scopes": (
                    "provider_candidate",
                    "smart_turn_turn",
                ),
                "pending_observation_gate_scopes": ("provider_candidate",),
                "backend_prewarm_scopes": ("smart_turn_turn",),
            },
            "backend_prewarm_scopes",
        ),
    ],
)
def test_config_rejects_scope_relationships_outside_nested_subsets(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        SpeakerShadowConfig(
            observation_checkpoints_ms=(1_500, 3_000),
            **overrides,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "completion_confirmation_scopes",
    [
        ["provider_candidate"],
        ("unsupported",),
        ("provider_candidate", "provider_candidate"),
    ],
)
def test_config_rejects_invalid_completion_confirmation_scopes(
    completion_confirmation_scopes: object,
) -> None:
    with pytest.raises(ValueError, match="completion_confirmation_scopes"):
        SpeakerShadowConfig(
            observation_checkpoints_ms=(1_500, 3_000),
            completion_confirmation_scopes=completion_confirmation_scopes,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "observation_checkpoints_ms",
    [None, (1_500,)],
)
def test_completion_confirmation_requires_two_explicit_checkpoints(
    observation_checkpoints_ms: tuple[int, ...] | None,
) -> None:
    with pytest.raises(ValueError, match="at least two explicit"):
        SpeakerShadowConfig(
            observation_checkpoints_ms=observation_checkpoints_ms,
            completion_confirmation_scopes=("provider_candidate",),
        )


def test_observation_kind_defaults_to_checkpoint_and_accepts_confirmation() -> None:
    candidate = SpeakerShadowCandidateKey(1, 2, "provider_candidate")
    observation = SpeakerShadowObservation(
        candidate=candidate,
        similarity=0.2,
        would_block=((0.4, True),),
        audio_ms=1_500,
        checkpoint_ms=1_500,
    )

    assert observation.observation_kind == "checkpoint"
    assert (
        SpeakerShadowObservation(
            candidate=candidate,
            similarity=0.2,
            would_block=((0.4, True),),
            audio_ms=2_999,
            checkpoint_ms=1_500,
            observation_kind="completion_confirmation",
        ).observation_kind
        == "completion_confirmation"
    )


def test_decision_status_is_an_optional_structural_read_only_protocol() -> None:
    candidate = SpeakerShadowCandidateKey(4, 5, "provider_candidate")

    class StatusOnly:
        def requires_provisional_decision(
            self,
            requested_candidate: SpeakerShadowCandidateKey,
        ) -> bool:
            return requested_candidate == candidate

    status = StatusOnly()

    assert isinstance(status, SpeakerShadowDecisionStatus)
    assert status.requires_provisional_decision(candidate) is True
    assert not isinstance(object(), SpeakerShadowDecisionStatus)


def test_deferred_candidate_control_is_an_optional_structural_protocol() -> None:
    candidate = SpeakerShadowCandidateKey(6, 7, "provider_candidate")

    class DeferredControlOnly:
        def defer_candidate(self, requested: SpeakerShadowCandidateKey) -> bool:
            return requested == candidate

        def activate_candidate(self, requested: SpeakerShadowCandidateKey) -> bool:
            return requested == candidate

    control = DeferredControlOnly()

    assert isinstance(control, SpeakerShadowDeferredCandidateControl)
    assert control.defer_candidate(candidate) is True
    assert control.activate_candidate(candidate) is True
    assert not isinstance(object(), SpeakerShadowDeferredCandidateControl)


def test_candidate_reconciliation_is_an_independent_optional_protocol() -> None:
    source = SpeakerShadowCandidateKey(6, 8, "provider_candidate")
    target = SpeakerShadowCandidateKey(6, 7, "provider_candidate")

    class ReconciliationOnly:
        def reconcile_candidate_prefix(
            self,
            *,
            source: SpeakerShadowCandidateKey,
            target: SpeakerShadowCandidateKey,
            prefix_sample_count: int,
            suffix: SpeakerShadowCandidateKey | None = None,
        ) -> bool:
            return source != target and prefix_sample_count == 160 and suffix is None

    control = ReconciliationOnly()

    assert isinstance(control, SpeakerShadowCandidateReconciliationControl)
    assert control.reconcile_candidate_prefix(
        source=source,
        target=target,
        prefix_sample_count=160,
    )
    assert not isinstance(control, SpeakerShadowDeferredCandidateControl)
    assert not isinstance(object(), SpeakerShadowCandidateReconciliationControl)


def test_batch_reconciliation_is_an_independent_optional_protocol() -> None:
    source = SpeakerShadowCandidateKey(6, 9, "provider_candidate")
    target = SpeakerShadowCandidateKey(6, 10, "provider_candidate")
    request = SpeakerShadowBatchReconcileRequest(
        sources=(SpeakerShadowReconcileSource(source, 320, 0, 320),),
        target=target,
    )
    owner = object()
    receipt = SpeakerShadowBatchReconcileReceipt(
        runtime_generation=0,
        batch_id=1,
        target=target,
        suffix=None,
        target_sample_count=320,
        suffix_sample_count=0,
        _owner=owner,
    )

    class BatchOnly:
        def reconcile_candidate_batch(
            self,
            requested: SpeakerShadowBatchReconcileRequest,
        ) -> SpeakerShadowBatchReconcileReceipt | None:
            return receipt if requested == request else None

        def reconciliation_status(
            self,
            requested: SpeakerShadowBatchReconcileReceipt,
        ) -> str:
            return "pending" if requested is receipt else "stale"

        def revoke_reconciliation(
            self,
            requested: SpeakerShadowBatchReconcileReceipt,
        ) -> None:
            assert requested is receipt

    control = BatchOnly()

    assert isinstance(control, SpeakerShadowBatchReconciliationControl)
    assert control.reconcile_candidate_batch(request) is receipt
    assert control.reconciliation_status(receipt) == "pending"
    control.revoke_reconciliation(receipt)
    assert not isinstance(control, SpeakerShadowCandidateReconciliationControl)
    assert not isinstance(object(), SpeakerShadowBatchReconciliationControl)


def test_capture_status_is_an_independent_single_submit_protocol() -> None:
    candidate = SpeakerShadowCandidateKey(6, 11, "provider_candidate")
    result = SpeakerShadowCaptureResult(
        disposition=SpeakerShadowCaptureDisposition.COMPLETE,
        accepted_sample_count=160,
        cumulative_sample_count=48_000,
        completed_window_sample_count=48_000,
        decision_state=SpeakerShadowCaptureDecisionState.PENDING,
    )

    class CaptureOnly:
        def submit_capture(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
            candidate: SpeakerShadowCandidateKey,
        ) -> SpeakerShadowCaptureResult:
            assert pcm16 and sample_rate_hz == 16_000 and candidate.scope
            return result

    control = CaptureOnly()

    assert isinstance(control, SpeakerShadowCaptureStatus)
    assert (
        control.submit_capture(
            b"\x01\x00",
            sample_rate_hz=16_000,
            candidate=candidate,
        )
        is result
    )
    assert not isinstance(object(), SpeakerShadowCaptureStatus)


@pytest.mark.parametrize(
    ("disposition", "decision"),
    [
        (
            SpeakerShadowCaptureDisposition.ACCEPTED,
            SpeakerShadowCaptureDecisionState.SCORED,
        ),
        (
            SpeakerShadowCaptureDisposition.ACCEPTED,
            SpeakerShadowCaptureDecisionState.UNAVAILABLE,
        ),
        (
            SpeakerShadowCaptureDisposition.COMPLETE,
            SpeakerShadowCaptureDecisionState.UNAVAILABLE,
        ),
        (
            SpeakerShadowCaptureDisposition.UNAVAILABLE,
            SpeakerShadowCaptureDecisionState.PENDING,
        ),
    ],
)
def test_capture_result_rejects_impossible_state_combinations(
    disposition: SpeakerShadowCaptureDisposition,
    decision: SpeakerShadowCaptureDecisionState,
) -> None:
    with pytest.raises(ValueError, match="capture"):
        SpeakerShadowCaptureResult(
            disposition=disposition,
            accepted_sample_count=0,
            cumulative_sample_count=0,
            completed_window_sample_count=0,
            decision_state=decision,
        )


def test_terminal_coverage_is_an_independent_optional_protocol() -> None:
    target = SpeakerShadowCandidateKey(6, 12, "provider_candidate")
    source = SpeakerShadowReconcileSource(target, 192_000, 0, 192_000)
    request = SpeakerShadowTerminalCoverageRequest(
        sources=(source,),
        target=target,
        provider_exact_start_sample=0,
        provider_exact_end_sample=192_000,
        scored_window_start_sample=0,
        scored_window_end_sample=48_000,
    )
    receipt = SpeakerShadowTerminalCoverageReceipt(
        runtime_generation=2,
        batch_id=3,
        target=target,
        suffix=None,
        retained_sample_count=48_000,
        covered_sample_count=192_000,
        terminal_preserved=True,
        _owner=object(),
    )

    class TerminalOnly:
        def reconcile_finalized_candidate_coverage(
            self,
            requested: SpeakerShadowTerminalCoverageRequest,
        ) -> SpeakerShadowTerminalCoverageReceipt | None:
            return receipt if requested == request else None

        def terminal_coverage_status(
            self,
            requested: SpeakerShadowTerminalCoverageReceipt,
        ) -> str:
            return "applied" if requested is receipt else "stale"

        def revoke_terminal_coverage(
            self,
            requested: SpeakerShadowTerminalCoverageReceipt,
        ) -> None:
            assert requested is receipt

    control = TerminalOnly()

    assert isinstance(control, SpeakerShadowTerminalCoverageControl)
    assert control.reconcile_finalized_candidate_coverage(request) is receipt
    assert control.terminal_coverage_status(receipt) == "applied"
    control.revoke_terminal_coverage(receipt)
    assert not isinstance(control, SpeakerShadowBatchReconciliationControl)
    assert not isinstance(object(), SpeakerShadowTerminalCoverageControl)

    class SettlementOnly:
        async def wait_reconciliation_settled(
            self,
            requested: SpeakerShadowTerminalCoverageReceipt,
            *,
            deadline: float,
        ) -> str:
            assert requested is receipt and deadline > 0
            return "applied"

    assert isinstance(SettlementOnly(), SpeakerShadowReconciliationSettlement)
    assert not isinstance(object(), SpeakerShadowReconciliationSettlement)


def test_deferred_candidate_status_is_an_optional_read_only_protocol() -> None:
    candidate = SpeakerShadowCandidateKey(8, 9, "provider_candidate")

    class DeferredStatusOnly:
        def supports_deferred_candidate(
            self,
            requested: SpeakerShadowCandidateKey,
        ) -> bool:
            return requested == candidate

    status = DeferredStatusOnly()

    assert isinstance(status, SpeakerShadowDeferredCandidateStatus)
    assert status.supports_deferred_candidate(candidate) is True
    assert not isinstance(object(), SpeakerShadowDeferredCandidateStatus)


@pytest.mark.parametrize(
    "arguments",
    [
        (-1, 0, "provider_candidate"),
        (0, -1, "provider_candidate"),
        (True, 0, "provider_candidate"),
        (0, 0, "unsupported"),
    ],
)
def test_candidate_key_rejects_identity_shapes_outside_fixed_contract(
    arguments: tuple[object, object, object],
) -> None:
    with pytest.raises(ValueError):
        SpeakerShadowCandidateKey(*arguments)  # type: ignore[arg-type]
