import numpy as np
import pytest

from main_logic.voice_turn.contracts import SpeechActivityEvent
from main_logic.asr_client.endpointing.config import SmartTurnConfig
from main_logic.asr_client.endpointing.onnx_runtime import RuntimeState
from main_logic.asr_client.endpointing.silero_vad import (
    SileroActivityGate,
    SileroFeedResult,
    SileroVad,
)


class _Session:
    def __init__(self):
        self.inputs = []

    def run(self, output_names, inputs):
        self.inputs.append(
            {key: np.asarray(value).copy() for key, value in inputs.items()}
        )
        state = inputs["state"] + 1
        return [np.asarray([[0.9]], dtype=np.float32), state]


def _ready_vad():
    vad = SileroVad(enabled=True)
    vad._session = _Session()
    vad._state = RuntimeState.READY
    return vad


def test_silero_preserves_context_and_lstm_state_across_windows():
    vad = _ready_vad()
    values = np.arange(1024, dtype=np.int16)
    assert vad.process_pcm16(values.tobytes()) == pytest.approx([0.9, 0.9])
    first, second = vad._session.inputs
    assert first["input"].shape == (1, 576)
    assert np.all(first["input"][0, :64] == 0)
    assert np.allclose(second["input"][0, :64], values[448:512] / 32768.0)
    assert np.all(second["state"] == 1)


def test_silero_reset_clears_context_state_and_pending_audio():
    vad = _ready_vad()
    vad.process_pcm16(np.ones(700, dtype=np.int16).tobytes())
    vad.reset_stream()
    assert not vad._pending.size
    assert np.all(vad._context == 0)
    assert np.all(vad._lstm_state == 0)


class _NoopVad:
    def reset_stream(self):
        pass


class _BatchVad(_NoopVad):
    def __init__(self):
        self.call_count = 0

    def process_pcm16(self, pcm16_le):
        self.call_count += 1
        return [0.9, 0.1]


def test_activity_gate_emits_pause_once_and_resume_without_force_commit():
    config = SmartTurnConfig(
        enabled=True,
        minimum_speech_ms=32,
        candidate_silence_ms=32,
    )
    gate = SileroActivityGate(_NoopVad(), config)
    assert gate.process_probabilities([0.9]) == (SpeechActivityEvent.SPEECH_STARTED,)
    assert gate.process_probabilities([0.1]) == (SpeechActivityEvent.CANDIDATE_PAUSE,)
    assert gate.process_probabilities([0.1] * 100) == ()
    assert gate.process_probabilities([0.9]) == (SpeechActivityEvent.SPEECH_RESUMED,)
    assert {event.value for event in SpeechActivityEvent}.isdisjoint(
        {"force_end", "turn_complete"}
    )


def test_activity_gate_duration_thresholds_never_round_down():
    gate = SileroActivityGate(
        _NoopVad(),
        SmartTurnConfig(
            enabled=True,
            minimum_speech_ms=200,
            candidate_silence_ms=300,
        ),
    )
    assert gate.process_probabilities([0.9] * 6) == ()
    assert gate.process_probabilities([0.9]) == (SpeechActivityEvent.SPEECH_STARTED,)
    assert gate.process_probabilities([0.1] * 9) == ()
    assert gate.process_probabilities([0.1]) == (SpeechActivityEvent.CANDIDATE_PAUSE,)


def test_activity_gate_preserves_all_ordered_events_from_one_batch():
    config = SmartTurnConfig(
        enabled=True,
        minimum_speech_ms=32,
        candidate_silence_ms=32,
    )
    gate = SileroActivityGate(_NoopVad(), config)

    assert gate.process_probabilities([0.9, 0.1]) == (
        SpeechActivityEvent.SPEECH_STARTED,
        SpeechActivityEvent.CANDIDATE_PAUSE,
    )


def test_activity_gate_feed_preserves_ordered_events_from_vad_batch():
    config = SmartTurnConfig(
        enabled=True,
        minimum_speech_ms=32,
        candidate_silence_ms=32,
    )
    vad = _BatchVad()
    gate = SileroActivityGate(vad, config)

    assert gate.feed(b"pcm") == (
        SpeechActivityEvent.SPEECH_STARTED,
        SpeechActivityEvent.CANDIDATE_PAUSE,
    )
    assert vad.call_count == 1


def test_activity_gate_evidence_classifies_every_window_without_probabilities():
    gate = SileroActivityGate(
        _NoopVad(),
        SmartTurnConfig(
            enabled=True,
            minimum_speech_ms=64,
            candidate_silence_ms=32,
            onset_probability=0.7,
            offset_probability=0.3,
        ),
    )

    result = gate.process_probabilities_with_evidence([0.9, 0.9, 0.6, 0.9, 0.1])

    assert result == SileroFeedResult(
        events=(
            SpeechActivityEvent.SPEECH_STARTED,
            SpeechActivityEvent.CANDIDATE_PAUSE,
        ),
        window_count=5,
        onset_window_count=3,
        offset_window_count=1,
        ambiguous_window_count=1,
        first_onset_window_index=0,
        last_onset_window_index=3,
        post_confirmation_onset_window_count=1,
    )
    assert not any("probability" in name for name in SileroFeedResult.__slots__)


def test_activity_gate_evidence_consumes_probability_iterable_once():
    class _SingleUseIterable:
        def __init__(self):
            self.iteration_count = 0

        def __iter__(self):
            self.iteration_count += 1
            if self.iteration_count > 1:
                raise AssertionError("probabilities were iterated more than once")
            return iter((0.9, 0.1))

    probabilities = _SingleUseIterable()
    gate = SileroActivityGate(
        _NoopVad(),
        SmartTurnConfig(
            enabled=True,
            minimum_speech_ms=32,
            candidate_silence_ms=32,
        ),
    )

    evidence = gate.process_probabilities_with_evidence(probabilities)

    assert probabilities.iteration_count == 1
    assert evidence.events == (
        SpeechActivityEvent.SPEECH_STARTED,
        SpeechActivityEvent.CANDIDATE_PAUSE,
    )


def test_activity_gate_feed_with_evidence_runs_vad_once_and_legacy_feed_is_tuple():
    config = SmartTurnConfig(
        enabled=True,
        minimum_speech_ms=32,
        candidate_silence_ms=32,
    )
    evidence_vad = _BatchVad()
    evidence_gate = SileroActivityGate(evidence_vad, config)

    evidence = evidence_gate.feed_with_evidence(b"pcm")

    assert evidence_vad.call_count == 1
    assert evidence.events == (
        SpeechActivityEvent.SPEECH_STARTED,
        SpeechActivityEvent.CANDIDATE_PAUSE,
    )
    legacy_vad = _BatchVad()
    legacy_result = SileroActivityGate(legacy_vad, config).feed(b"pcm")
    assert type(legacy_result) is tuple
    assert legacy_vad.call_count == 1
