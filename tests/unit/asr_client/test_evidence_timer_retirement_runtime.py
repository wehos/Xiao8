"""Early terminal evidence must retire only that record's deadline tasks."""

import asyncio

import pytest

from main_logic.asr_client._provider_events import ProviderUtteranceKey
from main_logic.asr_client.admission.contracts import AdmissionOperationKind, ScheduleEvidenceDeadline
from tests.unit.asr_client.test_evidence_hold_runtime_integration import (
    _enabled_stack, _finalize_exact, _unavailable_proof,
)
from tests.unit.asr_client.test_evidence_pipeline_integration import _feed, _settle
from tests.unit.asr_client.test_provider_speaker_continuity import _close_stack


def active_timers_for(runtime, binding):
    return {
        ticket: task for ticket, task in runtime._asr_admission_deadline_tasks.items()
        if ticket.turn_token == binding.turn_token
        and ticket.record_generation == binding.record_generation
        and task is not asyncio.current_task() and not task.done()
    }


@pytest.mark.asyncio
async def test_twenty_early_settled_records_leave_no_live_deadline_tasks():
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    leaks = []
    try:
        core.continuity_score_host.ready.set()
        for ordinal in range(1, 21):
            await _feed(runtime, turn, (ordinal - 1) * 16 + 1, ordinal * 16)
            transaction = await _finalize_exact(
                runtime, shadow, ProviderUtteranceKey(0, 0, ordinal),
                (ordinal - 1) * 25_600, ordinal * 25_600, f"synthetic timer key {ordinal}",
            )
            binding = transaction.evidence_binding
            owned = active_timers_for(runtime, binding)
            assert owned
            assert await runtime._submit_provider_evidence_proof(_unavailable_proof(transaction))
            await _settle(core, runtime)
            assert core.handle_input_transcript.await_count == ordinal
            active = {**active_timers_for(runtime, binding),
                      **{ticket: task for ticket, task in owned.items() if not task.done()}}
            if active:
                leaks.append((ordinal, tuple(ticket.operation_kind.value for ticket in active)))
        assert not leaks, f"settled records kept live deadlines: {leaks}"
    finally:
        await _close_stack(core)


@pytest.mark.asyncio
async def test_old_record_timer_retirement_keeps_pending_successor_deadline():
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        first = await _finalize_exact(
            runtime, shadow, ProviderUtteranceKey(0, 0, 1), 0, 25_600, "synthetic old timer",
        )
        await _feed(runtime, turn, 17, 32)
        second = await _finalize_exact(
            runtime, shadow, ProviderUtteranceKey(0, 0, 2), 25_600, 51_200, "synthetic successor timer",
        )
        successor_timers = active_timers_for(runtime, second.evidence_binding)
        first_timers = active_timers_for(runtime, first.evidence_binding)
        assert any(ticket.operation_kind is AdmissionOperationKind.EVIDENCE_DEADLINE for ticket in successor_timers)
        assert await runtime._submit_provider_evidence_proof(_unavailable_proof(first))
        await _settle(core, runtime)
        # Verify the successor before the old-task assertion, so the failing
        # baseline also proves the two independent record scopes are present.
        assert all(runtime._asr_admission_deadline_tasks.get(ticket) is task
                   and not task.done() for ticket, task in successor_timers.items())
        assert core.handle_input_transcript.await_count == 1
        assert not active_timers_for(runtime, first.evidence_binding)
        assert all(task.done() for task in first_timers.values())
        assert await runtime._submit_provider_evidence_proof(_unavailable_proof(second))
        await _settle(core, runtime)
        assert not active_timers_for(runtime, second.evidence_binding)
        assert core.handle_input_transcript.await_count == 2
    finally:
        await _close_stack(core)


@pytest.mark.asyncio
async def test_late_original_schedule_cannot_recreate_a_settled_record_timer():
    core, runtime, detector, shadow, lifecycle, session, turn = await _enabled_stack()
    try:
        core.continuity_score_host.ready.set()
        await _feed(runtime, turn, 1, 16)
        transaction = await _finalize_exact(
            runtime, shadow, ProviderUtteranceKey(0, 0, 1), 0, 25_600, "synthetic late schedule",
        )
        record = await runtime._asr_admission.get_record(transaction.turn_token)
        hold = record.evidence_hold
        schedule = ScheduleEvidenceDeadline(hold.ticket, hold.absolute_deadline)
        assert await runtime._submit_provider_evidence_proof(_unavailable_proof(transaction))
        await _settle(core, runtime)
        # Replay only the old local scheduling effect, never PCM or a final.
        await runtime._execute_admission_effect(schedule)
        assert not active_timers_for(runtime, transaction.evidence_binding)
        assert core.handle_input_transcript.await_count == 1
    finally:
        await _close_stack(core)
