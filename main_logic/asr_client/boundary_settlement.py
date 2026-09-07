"""Local boundary completion evidence and content-free timing diagnostics."""

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from config.application import APP_VERSION
from .diagnostic_logging import submit_resolution_log


_TRANSPORT_REFS: WeakKeyDictionary = WeakKeyDictionary()


def boundary_transport_ref(transport: object) -> str | None:
    """Join Session timing to Runtime diagnostics without retaining sessions."""
    try:
        reference = _TRANSPORT_REFS.get(transport)
        if reference is None:
            reference = secrets.token_hex(12)
            _TRANSPORT_REFS[transport] = reference
        return reference
    except TypeError:
        # Legacy adapters may not support weak references; never use addresses
        # or create an unbounded strong-reference fallback just for logging.
        return None


@dataclass
class BoundarySettlement:
    """Owned by one bounded Session task; logging never grants authority."""

    transport_ref: str | None
    key: tuple[int, int, int]
    deadline: float
    scheduled_at: float | None = field(default_factory=time.monotonic)
    started_at: float | None = None
    completed_at: float | None = None
    settled_at: float | None = None
    callback_outcome: str = "not_started"
    outcome: str = "pending"
    records_dropped: int = 0

    @classmethod
    def create(cls, transport, key, deadline):
        return cls(boundary_transport_ref(transport), key, deadline)

    async def invoke(self, callback, notification):
        self.started_at = time.monotonic()
        self.callback_outcome = "running"
        try:
            await callback(notification)
        except asyncio.CancelledError:
            self.callback_outcome = "cancelled"
            raise
        except Exception:
            self.callback_outcome = "failed"
            raise
        else:
            self.callback_outcome = "completed"
        finally:
            self.completed_at = time.monotonic()
            self.record("provider_boundary_callback_completed")

    def __bool__(self):
        return bool(
            self.outcome == "completed"
            and self.callback_outcome == "completed"
            and self.completed_at is not None
            and self.completed_at <= self.deadline
        )

    def record(self, stage, *, consumed_at=None, final_received_at=None, disposition=None):
        # The existing writer has a process-wide bounded queue and no IO here.
        try:
            result = submit_resolution_log({
                "schema": 2, "app_version": APP_VERSION,
                "observed_at_ns": time.time_ns(), "stage": stage,
                "diagnostic_transport_ref": self.transport_ref,
                "provider_generation": self.key[0], "provider_buffer_epoch": self.key[1],
                "provider_utterance_id": self.key[2],
                "boundary_scheduled_at_monotonic": self.scheduled_at,
                "boundary_deadline_monotonic": self.deadline,
                "boundary_started_at_monotonic": self.started_at,
                "boundary_completed_at_monotonic": self.completed_at,
                "boundary_settled_at_monotonic": self.settled_at,
                "boundary_consumed_at_monotonic": consumed_at,
                "final_received_at_monotonic": final_received_at,
                "callback_outcome": self.callback_outcome,
                "settlement_outcome": self.outcome, "disposition": disposition,
                "diagnostic_records_dropped": self.records_dropped,
            })
            if result is None:
                self.records_dropped += 1
        except Exception:
            self.records_dropped += 1
