"""Real Detector/Shadow ownership and accounting continuity regressions."""

from __future__ import annotations

from dataclasses import replace
import inspect
from functools import partial
import asyncio

import pytest

from tests.unit.test_asr_detector_runtime import (
    _Gate,
    _LowScoreSpeakerBackendFactory,
    _Vad,
    _anchor_provider_evidence,
    _open_provider_candidate,
    _provider_endpoint_policy,
    _provider_speaker_config,
    _speaker_pcm,
)
from main_logic.asr_client._provider_events import ProviderAudioRange
from main_logic.asr_client.endpointing.detector_runtime import DetectorRuntime
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCaptureDisposition,
    SpeakerShadowObservation,
    SpeakerShadowTerminalCoverageReceipt,
)
from main_logic.asr_client.speaker_shadow.runtime import SpeakerShadowRuntime


async def _committed_successor(*, settle=True):
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(), gate=_Gate(), provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await _open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert lease is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(800), sample_rate_hz=16_000, identity=identity,
            sequence_no=1, split_before_audio=False, speaker_evidence_lease=lease,
        ) is not None
        await _anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 12_800), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        if settle:
            await shadow.wait_idle()
        return detector, shadow, identity, committed
    except BaseException:
        await detector.close()
        raise


@pytest.mark.parametrize("seal_first", [False, True])
async def test_completed_boundary_preserves_real_successor_capture(seal_first) -> None:
    detector, shadow, identity, committed = await _committed_successor()
    try:
        successor = committed.successor_evidence_lease
        assert successor is not None
        if seal_first:
            assert await detector.seal_provider_candidate(
                speaker_snapshot=committed.snapshot,
            ) is not None
        complete = getattr(detector, "complete_provider_speaker_boundary", None)
        if complete is None:
            # Exercise the previous product cleanup path on the unfixed base.
            await detector.retire_provider_speaker_boundary_unknown(committed.snapshot)
        else:
            assert await complete(
                committed.snapshot, successor_evidence_lease=successor,
            ) == "completed"
        assert successor.candidate in shadow._candidate_tokens
        if seal_first:
            _, identity, _ = await _open_provider_candidate(detector, turn_id=2)
        update = await detector.observe_provider_audio_ordered(
            _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
            sequence_no=2, split_before_audio=True, speaker_evidence_lease=successor,
        )
        assert update is not None
        assert update.capture.disposition is SpeakerShadowCaptureDisposition.ACCEPTED
        assert update.capture.accepted_sample_count == 1_600
        await shadow.wait_idle()
        assert shadow._buffers[successor.candidate].sample_count == 1_600
    finally:
        await detector.close()


async def test_unavailable_audio_accounts_without_creating_speaker_candidate() -> None:
    detector, shadow, identity, committed = await _committed_successor()
    try:
        lease = committed.successor_evidence_lease
        assert lease is not None
        await detector.abandon_provider_speaker_evidence_lease(lease)
        before_candidates = dict(shadow._candidate_tokens)
        before_cursor = detector._provider_audio_sample_cursor_16k
        for sequence_no in (2, 3, 4):
            accounting_kwargs = (
                {"accounting_only": True}
                if "accounting_only" in inspect.signature(
                    detector.observe_provider_audio_ordered
                ).parameters else {}
            )
            receipt = await detector.observe_provider_audio_ordered(
                _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
                sequence_no=sequence_no, split_before_audio=False,
                speaker_evidence_lease=lease, evidence_complete=False,
                **accounting_kwargs,
            )
            assert detector._provider_audio_sample_cursor_16k == (
                before_cursor + (sequence_no - 1) * 1_600
            )
            assert receipt is not None
            assert receipt.sequence_no == sequence_no
            assert receipt.end_sample_16k - receipt.start_sample_16k == 1_600
        assert detector._provider_audio_sample_cursor_16k - before_cursor == 4_800
        assert shadow._candidate_tokens == before_candidates
        assert detector._provider_speaker_evidence_state is None
    finally:
        await detector.close()


@pytest.mark.parametrize("mutation", ["copy", "foreign_owner", "next_generation"])
async def test_completion_rejects_nonidentical_transfer_without_revoking(mutation) -> None:
    detector, shadow, _, committed = await _committed_successor()
    try:
        lease = committed.successor_evidence_lease
        assert lease is not None
        forged = replace(lease)
        if mutation == "foreign_owner":
            forged = replace(lease, _owner=object())
        elif mutation == "next_generation":
            forged = replace(lease, lease_generation=lease.lease_generation + 1)
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=forged,
        ) == "invalid"
        assert successor_alive(shadow, lease)
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
        ) == "completed"
    finally:
        await detector.close()


def successor_alive(shadow, lease) -> bool:
    return lease.candidate in shadow._candidate_tokens


async def test_late_and_repeated_cleanup_cannot_revoke_completed_successor() -> None:
    detector, shadow, _, committed = await _committed_successor()
    try:
        lease = committed.successor_evidence_lease
        assert lease is not None
        entry = detector._provider_preseal_entries[committed.snapshot.candidate_generation]
        receipt = entry.reconciliation
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
        ) == "completed"
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
        ) == "already_completed"
        for _ in range(3):
            await detector.retire_provider_speaker_boundary_unknown(committed.snapshot)
            shadow.revoke_reconciliation(receipt)
        assert successor_alive(shadow, lease)
        assert await detector.ensure_provider_speaker_evidence_lease() is lease
    finally:
        await detector.close()


async def test_real_revocation_retires_detector_lease_with_shadow_candidate() -> None:
    detector, shadow, _, committed = await _committed_successor()
    try:
        lease = committed.successor_evidence_lease
        assert lease is not None
        await detector.retire_provider_speaker_boundary_unknown(committed.snapshot)
        assert not successor_alive(shadow, lease)
        assert detector._provider_speaker_evidence_state is None
        replacement = await detector.ensure_provider_speaker_evidence_lease()
        assert replacement is not None and replacement != lease
    finally:
        await detector.close()


@pytest.mark.parametrize("operation", ["reset", "close", "reset_provider_audio_timeline"])
async def test_completion_after_lifecycle_retirement_is_stale(operation) -> None:
    detector, shadow, _, committed = await _committed_successor()
    try:
        lease = committed.successor_evidence_lease
        assert lease is not None
        assert await detector.seal_provider_candidate(
            speaker_snapshot=committed.snapshot,
        ) is not None
        await getattr(detector, operation)()
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
        ) == "stale"
        assert not successor_alive(shadow, lease)
        assert not detector._provider_boundary_completion_entries
    finally:
        await detector.close()


async def test_cancelled_completion_waiter_does_not_retire_successor() -> None:
    detector, shadow, _, committed = await _committed_successor()
    try:
        lease = committed.successor_evidence_lease
        await detector._lock.acquire()
        task = asyncio.create_task(detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
        ))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        detector._lock.release()
        assert successor_alive(shadow, lease)
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
        ) == "completed"
    finally:
        if detector._lock.locked():
            detector._lock.release()
        await detector.close()


@pytest.mark.parametrize("sequence_no", [1, 3])
async def test_accounting_duplicate_or_gap_rejects_without_side_effects(sequence_no) -> None:
    detector, shadow, identity, committed = await _committed_successor()
    try:
        lease = committed.successor_evidence_lease
        before_cursor = detector._provider_audio_sample_cursor_16k
        receipt = await detector.observe_provider_audio_ordered(
            _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
            sequence_no=sequence_no, split_before_audio=False,
            evidence_complete=False, accounting_only=True,
            speaker_evidence_lease=lease,
        )
        assert receipt is None
        assert detector._provider_audio_sample_cursor_16k == before_cursor
        assert await detector.ensure_provider_speaker_evidence_lease() is lease
        assert successor_alive(shadow, lease)
    finally:
        await detector.close()


async def test_accounting_old_lease_cannot_abandon_new_owner() -> None:
    detector, shadow, identity, committed = await _committed_successor()
    try:
        old_lease = committed.successor_evidence_lease
        await detector.abandon_provider_speaker_evidence_lease(old_lease)
        current = await detector.ensure_provider_speaker_evidence_lease()
        assert current is not None and current != old_lease
        before_cursor = detector._provider_audio_sample_cursor_16k
        for supplied in (None, old_lease):
            assert await detector.observe_provider_audio_ordered(
                _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
                sequence_no=2, split_before_audio=False, evidence_complete=False,
                accounting_only=True, speaker_evidence_lease=supplied,
            ) is None
        assert detector._provider_audio_sample_cursor_16k == before_cursor
        assert await detector.ensure_provider_speaker_evidence_lease() is current
        assert successor_alive(shadow, current)
    finally:
        await detector.close()


async def test_accounting_waiter_cannot_cross_timeline_reset() -> None:
    detector, shadow, identity, committed = await _committed_successor()
    try:
        lease = committed.successor_evidence_lease
        generation = detector._provider_audio_timeline_generation
        await detector._lock.acquire()
        task = asyncio.create_task(detector.observe_provider_audio_ordered(
            _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
            sequence_no=2, split_before_audio=False, evidence_complete=False,
            accounting_only=True, speaker_evidence_lease=lease,
            expected_timeline_generation=generation,
        ))
        # Rotate the same physical timeline before the waiting operation runs.
        detector._clear_provider_segment_state()
        detector._lock.release()
        assert await task is None
        assert detector._provider_audio_sample_cursor_16k == 0
    finally:
        if detector._lock.locked():
            detector._lock.release()
        await detector.close()


class _ConstantBackend:
    def __init__(self, score):
        self.value = score

    def load(self):
        return True

    def score(self, pcm16, sample_rate_hz):
        return self.value

    def close(self):
        pass


@pytest.mark.parametrize("score", [0.20, 0.95])
async def test_scored_exact_completion_preserves_successor_and_score(score) -> None:
    evidence = []
    shadow = SpeakerShadowRuntime(
        backend_factory=partial(_ConstantBackend, score),
        config=_provider_speaker_config(), on_evidence=evidence.append,
    )
    detector = DetectorRuntime(
        vad=_Vad(), gate=_Gate(), provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await _open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(1600), sample_rate_hz=16_000, identity=identity,
            sequence_no=1, split_before_audio=False, speaker_evidence_lease=lease,
        ) is not None
        await _anchor_provider_evidence(detector, lease)
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 25_600), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
            sequence_no=2, split_before_audio=True, speaker_evidence_lease=lease,
        ) is not None
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        await shadow.wait_idle()
        observations = [item for item in evidence if isinstance(item, SpeakerShadowObservation)]
        assert observations and all(item.similarity == score for item in observations)
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=committed.successor_evidence_lease,
        ) == "completed"
        update = await detector.observe_provider_audio_ordered(
            _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
            sequence_no=3, split_before_audio=False,
            speaker_evidence_lease=committed.successor_evidence_lease,
        )
        assert update is not None
        assert update.capture.accepted_sample_count == 1_600
        assert all(item.similarity == score for item in observations)
    finally:
        await detector.close()


async def test_pending_completion_waits_without_revocation_and_rechecks_owner() -> None:
    detector, shadow, _, committed = await _committed_successor(settle=False)
    try:
        lease = committed.successor_evidence_lease
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
        ) == "pending"
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
            deadline=asyncio.get_running_loop().time() - 1,
        ) == "pending"
        assert successor_alive(shadow, lease)
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=lease,
            deadline=asyncio.get_running_loop().time() + 1,
        ) == "completed"
        assert successor_alive(shadow, lease)
    finally:
        await detector.close()


async def test_completion_deadline_bounds_detector_lock_wait() -> None:
    detector, shadow, _, committed = await _committed_successor()
    try:
        await detector._lock.acquire()
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot,
            successor_evidence_lease=committed.successor_evidence_lease,
            deadline=asyncio.get_running_loop().time() + 0.01,
        ) == "pending"
        assert successor_alive(shadow, committed.successor_evidence_lease)
    finally:
        detector._lock.release()
        await detector.close()


async def _next_exact(detector, shadow, committed, sequence_no):
    fence = await detector.seal_provider_candidate(
        speaker_snapshot=committed.snapshot,
    )
    assert fence is not None
    assert await detector.complete_provider_candidate(fence) is not None
    _, identity, _ = await _open_provider_candidate(detector, turn_id=sequence_no)
    lease = committed.successor_evidence_lease
    start = detector._provider_audio_sample_cursor_16k
    assert await detector.observe_provider_audio_ordered(
        _speaker_pcm(800), sample_rate_hz=16_000, identity=identity,
        sequence_no=sequence_no, split_before_audio=False,
        speaker_evidence_lease=lease,
    ) is not None
    await _anchor_provider_evidence(detector, lease, start_sample_16k=start)
    reservation = await detector.prepare_provider_exact_speaker_interval(
        ProviderAudioRange(start, start + 12_800), speaker_evidence_lease=lease,
    )
    return identity, lease, reservation


async def test_three_exact_turns_survive_delayed_old_completion() -> None:
    detector, shadow, _, first = await _committed_successor()
    try:
        committed = [first]
        for sequence_no in (2, 3):
            identity, _, reservation = await _next_exact(
                detector, shadow, committed[-1], sequence_no,
            )
            assert reservation is not None
            next_commit = detector.commit_provider_exact_speaker_interval(reservation)
            assert next_commit is not None
            await shadow.wait_idle()
            committed.append(next_commit)
        for old in committed:
            assert await detector.complete_provider_speaker_boundary(
                old.snapshot, successor_evidence_lease=old.successor_evidence_lease,
            ) == "completed"
        successor = committed[-1].successor_evidence_lease
        assert successor_alive(shadow, successor)
        update = await detector.observe_provider_audio_ordered(
            _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
            sequence_no=4, split_before_audio=False, speaker_evidence_lease=successor,
        )
        assert update is not None
        assert update.capture.accepted_sample_count == 1_600
        assert not detector._provider_boundary_completion_entries
    finally:
        await detector.close()


async def test_completion_capacity_preserves_pending_owners_and_recovers() -> None:
    detector, shadow, _, first = await _committed_successor()
    try:
        committed = [first]
        for sequence_no in range(2, 9):
            _, _, reservation = await _next_exact(
                detector, shadow, committed[-1], sequence_no,
            )
            assert reservation is not None
            next_commit = detector.commit_provider_exact_speaker_interval(reservation)
            assert next_commit is not None
            await shadow.wait_idle()
            committed.append(next_commit)
        _, current, reservation = await _next_exact(detector, shadow, committed[-1], 9)
        assert reservation is None
        assert len(detector._provider_boundary_completion_entries) == 8
        assert await detector.ensure_provider_speaker_evidence_lease() is current
        assert successor_alive(shadow, current)
        assert await detector.complete_provider_speaker_boundary(
            first.snapshot, successor_evidence_lease=first.successor_evidence_lease,
        ) == "completed"
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(8 * 12_800, 9 * 12_800), speaker_evidence_lease=current,
        )
        assert reservation is not None
        assert detector.abort_provider_exact_speaker_interval(reservation)
    finally:
        await detector.close()


@pytest.mark.parametrize("normal_completion", [True, False])
async def test_terminal_coverage_completion_preserves_real_suffix_and_anchor(normal_completion) -> None:
    shadow = SpeakerShadowRuntime(
        backend_factory=_LowScoreSpeakerBackendFactory(),
        config=_provider_speaker_config(),
    )
    detector = DetectorRuntime(
        vad=_Vad(), gate=_Gate(), provider_policy=_provider_endpoint_policy(),
        speaker_shadow=shadow,
    )
    try:
        _, identity, _ = await _open_provider_candidate(detector, turn_id=1)
        lease = await detector.ensure_provider_speaker_evidence_lease()
        await _anchor_provider_evidence(detector, lease)
        for sequence_no in (1, 2, 3):
            assert await detector.observe_provider_audio_ordered(
                _speaker_pcm(1000), sample_rate_hz=16_000, identity=identity,
                sequence_no=sequence_no, split_before_audio=False,
                speaker_evidence_lease=lease,
            ) is not None
            await shadow.wait_idle()
        reservation = await detector.prepare_provider_exact_speaker_interval(
            ProviderAudioRange(0, 48_000), speaker_evidence_lease=lease,
        )
        assert reservation is not None
        assert await detector.observe_provider_audio_ordered(
            _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
            sequence_no=4, split_before_audio=True, speaker_evidence_lease=lease,
        ) is not None
        committed = detector.commit_provider_exact_speaker_interval(reservation)
        assert committed is not None
        entry = detector._provider_boundary_completion_entries[committed.snapshot.candidate_generation]
        receipt = entry.reconciliation
        assert type(receipt) is SpeakerShadowTerminalCoverageReceipt
        await shadow.wait_idle()
        if not normal_completion:
            await detector.retire_provider_speaker_boundary_unknown(committed.snapshot)
            assert detector._provider_speaker_evidence_state is None
            assert not successor_alive(shadow, committed.successor_evidence_lease)
            return
        assert await detector.complete_provider_speaker_boundary(
            committed.snapshot, successor_evidence_lease=committed.successor_evidence_lease,
        ) == "completed"
        shadow.revoke_terminal_coverage(receipt)
        successor = committed.successor_evidence_lease
        assert successor_alive(shadow, successor)
        assert shadow._buffers[successor.candidate].sample_count == 1_600
        await _anchor_provider_evidence(detector, successor, start_sample_16k=48_000)
        update = await detector.observe_provider_audio_ordered(
            _speaker_pcm(100), sample_rate_hz=16_000, identity=identity,
            sequence_no=5, split_before_audio=False, speaker_evidence_lease=successor,
        )
        assert update is not None
        assert update.capture.accepted_sample_count == 1_600
    finally:
        await detector.close()
