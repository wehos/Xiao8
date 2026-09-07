from __future__ import annotations

from main_routers import debug_router
from main_logic.voice_identity_service.diagnostics import (
    VOICE_IDENTITY_DIAGNOSTIC_COUNTERS,
)


_SPEAKER_FAILURE_REASON_CATEGORIES = {
    "gap",
    "overflow",
    "anchor",
    "prepare",
    "identity",
    "sequence",
    "proof",
}


def test_exact_interval_diagnostics_expose_only_bounded_reason_counters() -> None:
    expected = {
        "speaker_anchor_deferred_count",
        "speaker_anchor_success_count",
        "speaker_anchor_evicted_count",
        "speaker_anchor_conflict_count",
        "speaker_provisional_fact_count",
        "speaker_pre_anchor_fact_ignored_count",
        "speaker_ledger_poisoned_count",
        "speaker_exact_prepare_count",
        "speaker_exact_commit_count",
        "speaker_exact_abort_count",
        "speaker_unavailable_count",
        "unsupported_asr_route_count",
        *{
            f"speaker_ledger_poisoned_reason_{reason}_count"
            for reason in _SPEAKER_FAILURE_REASON_CATEGORIES
        },
        *{
            f"speaker_unavailable_reason_{reason}_count"
            for reason in _SPEAKER_FAILURE_REASON_CATEGORIES
        },
    }

    assert expected <= VOICE_IDENTITY_DIAGNOSTIC_COUNTERS
    assert not any(
        name.startswith("speaker_unavailable_reason_")
        and name not in expected
        for name in VOICE_IDENTITY_DIAGNOSTIC_COUNTERS
    )


def test_debug_health_voice_identity_diagnostics_keep_only_safe_counters(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        debug_router,
        "_VOICE_IDENTITY_DIAGNOSTICS_PROVIDER",
        lambda: {
            "observation_count": 3,
            "rejection_task_applied_count": 1,
            "admission_terminal_forward_count": 2,
            "admission_terminal_drop_count": 1,
            "admission_deadline_forward_count": 1,
            "admission_rejection_applied_sealed_count": 1,
            "admission_core_settlement_degraded_count": 0,
            "admission_late_operation_ignored_count": 1,
            "micro_event_candidate_count": 3,
            "micro_event_evidence_complete_count": 2,
            "micro_event_evidence_unavailable_count": 1,
            "micro_event_would_suppress_count": 1,
            "micro_event_suppressed_count": 0,
            "micro_event_shadow_forward_count": 1,
            "micro_event_fail_open_count": 1,
            "micro_event_stale_fence_count": 0,
            "micro_event_rnnoise_unavailable_count": 1,
            "speaker_unavailable_reason_anchor_count": 1,
            "speaker_unavailable_reason_private_audio_count": 1,
            "similarity": 0.12,
            "embedding": [1.0, 0.0],
            "unexpected": 99,
            "negative": -1,
            "boolean": True,
        },
    )

    assert debug_router._safe_voice_identity_diagnostics() == {
        "observation_count": 3,
        "rejection_task_applied_count": 1,
        "admission_terminal_forward_count": 2,
        "admission_terminal_drop_count": 1,
        "admission_deadline_forward_count": 1,
        "admission_rejection_applied_sealed_count": 1,
        "admission_core_settlement_degraded_count": 0,
        "admission_late_operation_ignored_count": 1,
        "micro_event_candidate_count": 3,
        "micro_event_evidence_complete_count": 2,
        "micro_event_evidence_unavailable_count": 1,
        "micro_event_would_suppress_count": 1,
        "micro_event_suppressed_count": 0,
        "micro_event_shadow_forward_count": 1,
        "micro_event_fail_open_count": 1,
        "micro_event_stale_fence_count": 0,
        "micro_event_rnnoise_unavailable_count": 1,
        "speaker_unavailable_reason_anchor_count": 1,
    }
