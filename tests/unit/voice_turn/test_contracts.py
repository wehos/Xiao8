import pytest
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

from main_logic.voice_turn.contracts import (
    AsrTurnCapabilities,
    AsrLifecycleNotification,
    AsrStatusEvent,
    AsrSubmitResult,
    AsrSubmitStatus,
    EvaluationStatus,
    TurnDecision,
    TurnEvaluation,
    VoiceIngressToken,
    VoicePartialEvent,
    VoiceTurnToken,
    build_turn_detector_if_required,
    requires_external_turn_detector,
)


def _turn_token(*, session_epoch: int = 1, turn_id: int = 2) -> VoiceTurnToken:
    return VoiceTurnToken(
        ingress=VoiceIngressToken(
            session_epoch=session_epoch,
            connection_id="connection",
            lease_generation=3,
            route_generation=4,
            audio_generation=5,
        ),
        turn_id=turn_id,
    )


def test_semantic_endpoint_provider_does_not_require_smart_turn():
    assert requires_external_turn_detector(AsrTurnCapabilities(semantic_endpoint=True)) is False
    assert requires_external_turn_detector(AsrTurnCapabilities(semantic_endpoint=False)) is True


def test_semantic_endpoint_provider_never_constructs_smart_turn_runtime():
    factory = Mock()
    detector = build_turn_detector_if_required(
        AsrTurnCapabilities(semantic_endpoint=True), factory
    )
    assert detector is None
    factory.assert_not_called()


def test_unavailable_is_not_an_incomplete_decision():
    evaluation = TurnEvaluation(
        status=EvaluationStatus.UNAVAILABLE,
        decision=None,
        probability=None,
        generation=1,
        activity_seq=2,
        reason="model_missing",
    )
    assert evaluation.decision is not TurnDecision.INCOMPLETE


def test_ok_evaluation_requires_probability_and_decision():
    with pytest.raises(ValueError):
        TurnEvaluation(EvaluationStatus.OK, TurnDecision.COMPLETE, None, 0, 0)


def test_non_ok_evaluation_rejects_probability():
    with pytest.raises(ValueError):
        TurnEvaluation(EvaluationStatus.ERROR, None, 0.4, 0, 0)


@pytest.mark.parametrize(
    "event",
    [
        VoicePartialEvent(turn_token=_turn_token(), text="hello"),
        AsrStatusEvent(code="ASR_READY", provider="qwen"),
        AsrLifecycleNotification(
            state="local_listen",
            provider="qwen",
            session_epoch=1,
        ),
        AsrSubmitResult(status=AsrSubmitStatus.ACCEPTED),
    ],
)
def test_cross_layer_asr_events_are_immutable(event):
    with pytest.raises(FrozenInstanceError):
        event.__setattr__(next(iter(event.__dataclass_fields__)), object())


def test_partial_event_exposes_read_only_epoch_from_full_turn_identity() -> None:
    token = _turn_token(session_epoch=7, turn_id=11)

    event = VoicePartialEvent(turn_token=token, text="draft")

    assert event.turn_token is token
    assert event.session_epoch == 7


def test_asr_control_events_carry_ordering_and_incident_identity() -> None:
    status = AsrStatusEvent(
        code="ASR_DENY_CLEANUP_FAILED",
        provider="qwen",
        session_epoch=7,
        transport_generation=11,
        lifecycle_revision=13,
        reason_code="ASR_DENY_CLEANUP_FAILED",
        incident_id="incident-17",
    )
    lifecycle = AsrLifecycleNotification(
        state="blocked",
        provider="qwen",
        session_epoch=7,
        transport_generation=11,
        lifecycle_revision=12,
        reason_code="ASR_DENY_CLEANUP_FAILED",
        incident_id="incident-17",
    )

    assert status.transport_generation == lifecycle.transport_generation == 11
    assert status.lifecycle_revision > lifecycle.lifecycle_revision
    assert status.reason_code == lifecycle.reason_code
    assert status.incident_id == lifecycle.incident_id
