import pytest

from main_logic.asr_client.endpointing.micro_event_policy import (
    ProviderMicroEventConfig,
    ProviderMicroEventDecision,
    ProviderMicroEventEvidence,
    ProviderMicroEventPolicy,
)
from main_logic.asr_client.endpointing.silero_vad import SileroFeedResult
from main_logic.voice_turn.contracts import SpeechActivityEvent


def _silero_evidence(**overrides):
    values = {
        "events": (
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.CANDIDATE_PAUSE,
        ),
        "window_count": 7,
        "onset_window_count": 6,
        "offset_window_count": 1,
        "ambiguous_window_count": 0,
        "first_onset_window_index": 0,
        "last_onset_window_index": 5,
        "post_confirmation_onset_window_count": 4,
    }
    values.update(overrides)
    return SileroFeedResult(**values)


def _evidence(**overrides):
    values = {
        "silero": _silero_evidence(),
        "rnnoise_evidence_complete": True,
        "rnnoise_longest_active_run_upper_bound_ms": 160,
        "speech_started_count": 1,
        "speech_resumed_count": 0,
        "candidate_pause_count": 1,
        "event_sequence_valid": True,
        "candidate_local_start_kind": "speech_started",
    }
    values.update(overrides)
    return ProviderMicroEventEvidence(**values)


def test_micro_event_policy_defaults_off():
    policy = ProviderMicroEventPolicy()

    assert policy.config == ProviderMicroEventConfig()
    assert policy.decide(_evidence()) == ProviderMicroEventDecision(
        False,
        False,
        "disabled",
    )


@pytest.mark.parametrize("mode", [True, 1, "invalid"])
def test_micro_event_config_rejects_invalid_mode(mode):
    with pytest.raises(ValueError, match="mode"):
        ProviderMicroEventConfig(mode=mode)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("maximum_silero_span_ms", 0),
        ("maximum_silero_span_ms", True),
        ("maximum_post_start_onset_windows", -1),
        ("maximum_post_start_onset_windows", False),
        ("maximum_rnnoise_active_run_upper_bound_ms", 0),
        ("maximum_rnnoise_active_run_upper_bound_ms", True),
    ],
)
def test_micro_event_config_rejects_invalid_thresholds(name, value):
    with pytest.raises(ValueError, match=name):
        ProviderMicroEventConfig(**{name: value})


def test_micro_event_config_allows_zero_post_start_onset_windows():
    config = ProviderMicroEventConfig(maximum_post_start_onset_windows=0)

    assert config.maximum_post_start_onset_windows == 0


@pytest.mark.parametrize("revision", [None, "", "  "])
def test_micro_event_enforce_requires_calibration_revision(revision):
    with pytest.raises(ValueError, match="calibration_revision"):
        ProviderMicroEventConfig(mode="enforce", calibration_revision=revision)


@pytest.mark.parametrize(
    ("mode", "would_suppress", "suppress", "reason"),
    [
        ("off", False, False, "disabled"),
        ("shadow", True, False, "micro_event_shadow"),
        ("enforce", True, True, "micro_event_enforced"),
    ],
)
def test_micro_event_policy_separates_classification_from_enforcement(
    mode,
    would_suppress,
    suppress,
    reason,
):
    revision = "calibration-v1" if mode == "enforce" else None
    policy = ProviderMicroEventPolicy(
        ProviderMicroEventConfig(mode=mode, calibration_revision=revision)
    )

    assert policy.decide(_evidence()) == ProviderMicroEventDecision(
        would_suppress,
        suppress,
        reason,
    )


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (
            _evidence(
                silero=_silero_evidence(
                    first_onset_window_index=0,
                    last_onset_window_index=12,
                    window_count=14,
                    onset_window_count=13,
                    offset_window_count=1,
                )
            ),
            "silero_span_exceeded",
        ),
        (
            _evidence(
                silero=_silero_evidence(
                    post_confirmation_onset_window_count=5,
                )
            ),
            "post_start_onset_exceeded",
        ),
        (
            _evidence(rnnoise_longest_active_run_upper_bound_ms=161),
            "rnnoise_active_run_exceeded",
        ),
    ],
)
def test_micro_event_policy_fails_open_outside_calibrated_bounds(evidence, reason):
    policy = ProviderMicroEventPolicy(ProviderMicroEventConfig(mode="shadow"))

    assert policy.decide(evidence) == ProviderMicroEventDecision(False, False, reason)


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (_evidence(silero=None), "incomplete_silero_evidence"),
        (
            _evidence(
                silero=_silero_evidence(
                    events=(SpeechActivityEvent.CANDIDATE_PAUSE,),
                )
            ),
            "unexpected_silero_events",
        ),
        (
            _evidence(
                silero=_silero_evidence(
                    events=(
                        SpeechActivityEvent.CANDIDATE_PAUSE,
                        SpeechActivityEvent.SPEECH_STARTED,
                    ),
                )
            ),
            "unexpected_silero_events",
        ),
        (
            _evidence(
                silero=_silero_evidence(
                    events=(
                        SpeechActivityEvent.SPEECH_STARTED,
                        SpeechActivityEvent.CANDIDATE_PAUSE,
                        SpeechActivityEvent.SPEECH_RESUMED,
                    ),
                )
            ),
            "unexpected_silero_events",
        ),
        (
            _evidence(
                silero=_silero_evidence(
                    first_onset_window_index=6,
                    last_onset_window_index=5,
                )
            ),
            "unordered_silero_evidence",
        ),
        (
            _evidence(
                silero=_silero_evidence(ambiguous_window_count=1),
            ),
            "unordered_silero_evidence",
        ),
        (_evidence(rnnoise_evidence_complete=False), "incomplete_rnnoise_evidence"),
        (_evidence(rnnoise_evidence_complete=1), "incomplete_rnnoise_evidence"),
        (
            _evidence(rnnoise_longest_active_run_upper_bound_ms=None),
            "incomplete_rnnoise_evidence",
        ),
        (
            _evidence(rnnoise_longest_active_run_upper_bound_ms=-1),
            "incomplete_rnnoise_evidence",
        ),
    ],
)
def test_micro_event_policy_fails_open_on_malformed_or_incomplete_evidence(
    evidence,
    reason,
):
    policy = ProviderMicroEventPolicy(ProviderMicroEventConfig(mode="shadow"))

    assert policy.decide(evidence) == ProviderMicroEventDecision(
        False,
        False,
        reason,
        fail_open=True,
    )


def test_micro_event_policy_records_candidate_boundary_resume_provenance():
    policy = ProviderMicroEventPolicy(ProviderMicroEventConfig(mode="shadow"))

    decision = policy.decide(
        _evidence(
            speech_started_count=0,
            speech_resumed_count=1,
            candidate_local_start_kind=(
                "speech_resumed_at_candidate_boundary"
            ),
        )
    )

    assert decision == ProviderMicroEventDecision(
        True,
        False,
        "micro_event_shadow",
    )
