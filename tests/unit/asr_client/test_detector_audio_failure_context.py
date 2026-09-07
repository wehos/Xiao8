"""Actual Detector rejection branches emit content-free local failure receipts."""

from dataclasses import replace
from functools import partial

import pytest

from main_logic.asr_client.failure_diagnostics import AudioFailureContext
from tests.unit import test_asr_detector_runtime as fixtures
from tests.unit.asr_client.test_provider_speaker_continuity import _ConstantBackend


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,check", [
    ("sequence", "audio_sequence_stale"),
    ("timeline", "audio_timeline_changed"),
    ("epoch", "detector_epoch_changed"),
    ("owner", "speaker_lease_foreign_owner"),
    ("accounting_sequence", "accounting_sequence"),
    ("accounting_owner", "accounting_other_owner"),
    ("retired", "speaker_lease_retired"),
    ("unaligned", "audio_sample_alignment"),
])
async def test_detector_failure_names_first_exact_local_check(failure, check):
    shadow = fixtures.SpeakerShadowRuntime(
        backend_factory=partial(_ConstantBackend, 0.95),
        config=fixtures._provider_speaker_config(),
    )
    detector = fixtures.DetectorRuntime(
        vad=fixtures._Vad(), gate=fixtures._Gate(),
        provider_policy=fixtures._provider_endpoint_policy(), speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await fixtures._open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        kwargs = dict(sample_rate_hz=16000, identity=identity, sequence_no=1,
                      split_before_audio=False, speaker_evidence_lease=lease)
        clean = AudioFailureContext("provider_audio", {"sequence_no": 1})
        assert await detector.observe_provider_audio_ordered(
            fixtures._speaker_pcm(100), **kwargs, failure_context=clean) is not None
        assert clean.detected_at is None
        kwargs["sequence_no"] = 2
        if failure == "sequence":
            kwargs["sequence_no"] = 1
        elif failure == "timeline":
            kwargs["expected_timeline_generation"] = detector._provider_audio_timeline_generation + 1
        elif failure == "epoch":
            kwargs["identity"] = replace(identity, detector_epoch=identity.detector_epoch + 1)
        elif failure == "owner":
            kwargs["speaker_evidence_lease"] = replace(lease, _owner=object())
        elif failure.startswith("accounting"):
            kwargs.update(accounting_only=True, evidence_complete=False)
            if failure == "accounting_sequence":
                kwargs["sequence_no"] = 3
            else:
                kwargs["speaker_evidence_lease"] = None
        elif failure == "retired":
            await detector.abandon_provider_speaker_evidence_lease(lease)
        else:
            kwargs["sample_rate_hz"] = 44101
        context = AudioFailureContext("provider_audio", {"sequence_no": kwargs["sequence_no"]})
        result = await detector.observe_provider_audio_ordered(
            fixtures._speaker_pcm(100), **kwargs, failure_context=context)
        assert result is None
        assert context.check == check
        assert context.detected_at is not None
        assert context.actual["detector_epoch"] == detector.detector_epoch
        before = context.snapshot()
        context.fail("later_cleanup_failed", actual={"sequence_no": 999})
        assert context.snapshot() == before
        assert all(type(value) is int or value is None for value in context.actual.values())
    finally:
        await detector.close()
        await shadow.close()
