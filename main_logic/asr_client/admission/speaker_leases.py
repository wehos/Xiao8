"""Pure parent state for speaker verdicts spanning Provider text turns."""

from __future__ import annotations

from dataclasses import replace

from .contracts import (
    CountDiagnostic,
    SpeakerCaptureLeaseRecord,
    SpeakerLeaseAbandoned,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseChildBinding,
    SpeakerLeaseEvent,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseState,
    SpeakerLeaseUnavailable,
    SpeakerCheckpointKind,
)


MAX_SPEAKER_LEASES = 8
MAX_SPEAKER_LEASE_CHILDREN = 8

_TERMINAL_STATES = {
    SpeakerLeaseState.ALLOW,
    SpeakerLeaseState.DENY_LATCHED,
    SpeakerLeaseState.MIXED_DENY_LATCHED,
    SpeakerLeaseState.UNAVAILABLE,
    SpeakerLeaseState.ABANDONED,
}
_EVENT_TYPES = (
    SpeakerLeaseAbandoned,
    SpeakerLeaseCaptureClosed,
    SpeakerLeaseHigh,
    SpeakerLeaseLow,
    SpeakerLeaseUnavailable,
)


class SpeakerLeaseChildCapacityError(RuntimeError):
    """A lease cannot accept another Provider text child without overflow."""


class SpeakerLeaseIdentityError(RuntimeError):
    """A Provider key or voice turn attempted an inconsistent lease binding."""


class SpeakerLeaseTerminalError(RuntimeError):
    """A terminal lease cannot acquire a later Provider child."""


def _changed(
    record: SpeakerCaptureLeaseRecord,
    **changes: object,
) -> SpeakerCaptureLeaseRecord:
    if all(getattr(record, name) == value for name, value in changes.items()):
        return record
    return replace(
        record,
        logical_revision=record.logical_revision + 1,
        **changes,
    )


def bind_speaker_lease_child(
    record: SpeakerCaptureLeaseRecord,
    binding: SpeakerLeaseChildBinding,
    *,
    capacity: int = MAX_SPEAKER_LEASE_CHILDREN,
) -> SpeakerCaptureLeaseRecord:
    """Bind exactly one child while preserving Provider-start ordering."""

    if type(record) is not SpeakerCaptureLeaseRecord:
        raise TypeError("record must be SpeakerCaptureLeaseRecord")
    if type(binding) is not SpeakerLeaseChildBinding:
        raise TypeError("binding must be SpeakerLeaseChildBinding")
    if type(capacity) is not int or capacity <= 0:
        raise ValueError("capacity must be a positive integer")

    for current in record.child_bindings:
        if current == binding:
            return record
        if current.provider_key == binding.provider_key:
            raise SpeakerLeaseIdentityError("ASR_SPEAKER_LEASE_PROVIDER_KEY_CONFLICT")
        if current.turn_token == binding.turn_token:
            raise SpeakerLeaseIdentityError("ASR_SPEAKER_LEASE_TURN_CONFLICT")

    if record.state in _TERMINAL_STATES:
        raise SpeakerLeaseTerminalError("ASR_SPEAKER_LEASE_TERMINAL")
    if len(record.child_bindings) >= capacity:
        raise SpeakerLeaseChildCapacityError(
            "ASR_SPEAKER_LEASE_CHILD_CAPACITY_EXHAUSTED"
        )

    if record.child_bindings:
        previous = record.child_bindings[-1].provider_key
        current = binding.provider_key
        if (previous.generation, previous.buffer_epoch) != (
            current.generation,
            current.buffer_epoch,
        ):
            raise SpeakerLeaseIdentityError(
                "ASR_SPEAKER_LEASE_PROVIDER_NAMESPACE_CONFLICT"
            )
        if current.utterance_id <= previous.utterance_id:
            raise SpeakerLeaseIdentityError("ASR_SPEAKER_LEASE_PROVIDER_ORDER_CONFLICT")

    return _changed(
        record,
        child_bindings=(*record.child_bindings, binding),
    )


def _ordered_fact_is_current(
    record: SpeakerCaptureLeaseRecord,
    *,
    candidate: object,
    sequence_no: object,
) -> bool:
    return bool(
        candidate == record.candidate
        and type(sequence_no) is int
        and sequence_no == record.last_speaker_sequence_no + 1
    )


def reduce_speaker_lease(
    record: SpeakerCaptureLeaseRecord,
    event: SpeakerLeaseEvent,
) -> tuple[SpeakerCaptureLeaseRecord, tuple[CountDiagnostic, ...]]:
    """Reduce one ordered fact; a formal denial is permanently sticky."""

    if type(record) is not SpeakerCaptureLeaseRecord:
        raise TypeError("record must be SpeakerCaptureLeaseRecord")
    if not isinstance(event, _EVENT_TYPES):
        raise TypeError("event must be SpeakerLeaseEvent")

    if record.state in {
        SpeakerLeaseState.DENY_LATCHED,
        SpeakerLeaseState.MIXED_DENY_LATCHED,
    }:
        return record, (CountDiagnostic("speaker_lease_late_fact_stale_count"),)

    if isinstance(event, SpeakerLeaseAbandoned):
        if record.state in _TERMINAL_STATES:
            return record, ()
        return _changed(
            record,
            state=SpeakerLeaseState.ABANDONED,
            terminal_sequence_no=record.last_speaker_sequence_no,
            terminal_event=event,
        ), ()

    if record.state in _TERMINAL_STATES:
        return record, (CountDiagnostic("speaker_lease_late_fact_stale_count"),)

    candidate = getattr(event, "candidate", None)
    if candidate != record.candidate:
        return record, (CountDiagnostic("speaker_lease_late_fact_stale_count"),)

    if isinstance(event, SpeakerLeaseCaptureClosed):
        through = event.through_sequence_no
        if type(through) is not int or through < record.last_speaker_sequence_no:
            return record, (CountDiagnostic("speaker_lease_late_fact_stale_count"),)
        if record.state is SpeakerLeaseState.HIGH_SEEN:
            return _changed(
                record,
                state=SpeakerLeaseState.ALLOW,
                terminal_sequence_no=through,
                capture_through_sequence_no=through,
                terminal_event=event,
            ), (CountDiagnostic("speaker_lease_allow_count"),)
        return _changed(
            record,
            state=SpeakerLeaseState.UNAVAILABLE,
            terminal_sequence_no=through,
            capture_through_sequence_no=through,
            terminal_event=event,
        ), (CountDiagnostic("speaker_lease_capture_closed_unavailable_count"),)

    sequence_no = getattr(event, "sequence_no", None)
    if not _ordered_fact_is_current(
        record,
        candidate=candidate,
        sequence_no=sequence_no,
    ):
        return record, (CountDiagnostic("speaker_lease_late_fact_stale_count"),)

    if isinstance(event, SpeakerLeaseLow):
        if type(event.checkpoint_kind) is not SpeakerCheckpointKind:
            raise TypeError("checkpoint_kind must be SpeakerCheckpointKind")
        if record.state is SpeakerLeaseState.HIGH_SEEN:
            return _changed(
                record,
                state=SpeakerLeaseState.MIXED_DENY_LATCHED,
                last_speaker_sequence_no=sequence_no,
                terminal_sequence_no=sequence_no,
                terminal_event=event,
            ), (CountDiagnostic("speaker_lease_mixed_deny_latched_count"),)
        if record.state is SpeakerLeaseState.FIRST_LOW and event.checkpoint_kind in {
            SpeakerCheckpointKind.SECOND,
            SpeakerCheckpointKind.COMPLETION_CONFIRMATION,
        }:
            return _changed(
                record,
                state=SpeakerLeaseState.DENY_LATCHED,
                last_speaker_sequence_no=sequence_no,
                terminal_sequence_no=sequence_no,
                terminal_event=event,
            ), (CountDiagnostic("speaker_lease_deny_latched_count"),)
        if (
            record.state is SpeakerLeaseState.COLLECTING
            and event.checkpoint_kind is SpeakerCheckpointKind.FIRST
        ):
            return _changed(
                record,
                state=SpeakerLeaseState.FIRST_LOW,
                last_speaker_sequence_no=sequence_no,
            ), (CountDiagnostic("speaker_lease_first_low_count"),)
        if record.state is SpeakerLeaseState.FIRST_LOW:
            return _changed(
                record,
                last_speaker_sequence_no=sequence_no,
            ), (CountDiagnostic("speaker_lease_duplicate_first_low_count"),)
        return _changed(
            record,
            state=SpeakerLeaseState.UNAVAILABLE,
            last_speaker_sequence_no=sequence_no,
            terminal_sequence_no=sequence_no,
            terminal_event=event,
        ), (CountDiagnostic("speaker_lease_low_without_first_count"),)

    if isinstance(event, SpeakerLeaseHigh):
        if record.state is SpeakerLeaseState.FIRST_LOW:
            return _changed(
                record,
                state=SpeakerLeaseState.MIXED_DENY_LATCHED,
                last_speaker_sequence_no=sequence_no,
                terminal_sequence_no=sequence_no,
                terminal_event=event,
            ), (CountDiagnostic("speaker_lease_mixed_deny_latched_count"),)
        return _changed(
            record,
            state=SpeakerLeaseState.HIGH_SEEN,
            last_speaker_sequence_no=sequence_no,
        ), (CountDiagnostic("speaker_lease_high_seen_count"),)

    if isinstance(event, SpeakerLeaseUnavailable):
        return _changed(
            record,
            state=SpeakerLeaseState.UNAVAILABLE,
            last_speaker_sequence_no=sequence_no,
            terminal_sequence_no=sequence_no,
            terminal_event=event,
        ), (CountDiagnostic("speaker_lease_unavailable_count"),)

    raise TypeError("unsupported speaker lease event")


__all__ = [
    "MAX_SPEAKER_LEASES",
    "MAX_SPEAKER_LEASE_CHILDREN",
    "SpeakerLeaseChildCapacityError",
    "SpeakerLeaseIdentityError",
    "SpeakerLeaseTerminalError",
    "bind_speaker_lease_child",
    "reduce_speaker_lease",
]
