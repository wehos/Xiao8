"""Content-free, operation-local failure and cleanup facts.

These records grant no transport or admission authority. They contain no live
runtime objects, audio, text, exception messages, or speaker vectors.
"""

from __future__ import annotations

import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SCALARS = frozenset({
    "session_epoch", "start_generation", "audio_generation", "transport_generation",
    "turn_id", "detector_epoch", "timeline_generation", "lease_generation",
    "activation_generation", "candidate_generation",
    "sequence_no", "sample_cursor_16k", "buffer_origin_sample_16k",
    "payload_samples", "provider_generation", "provider_buffer_epoch",
    "provider_utterance_id", "score_start_sample_16k", "score_end_sample_16k",
    "exact_start_sample_16k", "exact_end_sample_16k",
})
_OUTCOMES = frozenset({
    "pending", "completed", "timed_out", "cancelled", "failed", "superseded",
    "not_required", "not_started",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_label(value: object) -> str:
    return value if type(value) is str and _LABEL.fullmatch(value) else "unknown"


def scalar_snapshot(values: dict) -> dict[str, int | None]:
    return {
        name: value for name, value in values.items()
        if name in _SCALARS and (type(value) is int or value is None)
    }


@dataclass(slots=True)
class AudioFailureContext:
    """One caller's local receipt; never reused through mutable runtime state."""

    operation: str
    expected: dict[str, int | None] = field(default_factory=dict)
    check: str = "unknown"
    actual: dict[str, int | None] = field(default_factory=dict)
    send_state: str = "not_sent"
    error_type: str | None = None
    stack: tuple[tuple[str, str, int], ...] = ()
    detected_at: str | None = None

    def __post_init__(self) -> None:
        self.expected = scalar_snapshot(self.expected)

    def fail(
        self, check: str, *, actual: dict | None = None,
        error: BaseException | None = None, send_state: str = "not_sent",
    ) -> bool:
        # Preserve the first failed check within this operation. A subsequent
        # cleanup exception must not replace the accounting/ownership cause.
        if self.detected_at is None:
            self.check = safe_label(check)
            self.actual = scalar_snapshot(actual or {})
            self.send_state = send_state if send_state in {
                "not_sent", "queued", "written", "confirmed", "unknown",
            } else "unknown"
            self.detected_at = utc_now()
            if error is not None:
                self.error_type = (
                    type(error).__name__ if type(error).__module__ == "builtins"
                    else "internal_error"
                )
                frames = traceback.extract_tb(error.__traceback__, limit=6)
                self.stack = tuple(
                    (frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
                     safe_label(frame.name.lstrip("_")), frame.lineno)
                    for frame in frames
                    if "/main_logic/" in frame.filename.replace("\\", "/")
                )
        return False

    def snapshot(self) -> dict:
        return {
            "failed_operation": safe_label(self.operation),
            "failed_check": safe_label(self.check),
            "expected": scalar_snapshot(self.expected),
            "actual": scalar_snapshot(self.actual),
            "send_state": self.send_state if self.send_state in {
                "not_sent", "queued", "written", "confirmed", "unknown",
            } else "unknown",
            "error_type": self.error_type,
            "error_stack": self.stack,
            "detected_at": self.detected_at or utc_now(),
        }


@dataclass(slots=True)
class CleanupTrace:
    """A captured operation's outcomes, separate from the current session."""

    incident_id: str
    emit: Callable[[dict], None]
    started_at: str = field(default_factory=utc_now)
    started_monotonic: float = field(default_factory=time.monotonic)
    components: dict[str, str] = field(default_factory=dict)
    residual: set[str] = field(default_factory=set)

    def mark(self, component: str, outcome: str, *, residual: bool = False) -> None:
        if component not in {
            "admission", "transport", "lease", "detector", "dispatchers", "notification",
            "audio_dispatcher", "detector_dispatcher",
        }:
            raise ValueError("unknown cleanup component")
        if outcome not in _OUTCOMES:
            raise ValueError("unknown cleanup outcome")
        self.components[component] = outcome
        if residual or outcome in {"pending", "not_started"}:
            self.residual.add(component)
        else:
            self.residual.discard(component)

    def record(self, outcome: str, *, stage: str = "cleanup_finished") -> None:
        if outcome not in _OUTCOMES:
            raise ValueError("unknown cleanup outcome")
        if outcome == "completed":
            outcome = next((value for value in (
                "timed_out", "failed", "cancelled", "superseded", "pending", "not_started",
            ) if value in self.components.values()), "completed")
        if self.residual and outcome == "completed":
            outcome = "pending"
        try:
            self.emit({
                "schema": 1,
                "incident_id": self.incident_id,
                "stage": safe_label(stage),
                "retirement_started_at": self.started_at,
                "recorded_at": utc_now(),
                "completed_at": None if self.residual else utc_now(),
                "elapsed_ms": round((time.monotonic() - self.started_monotonic) * 1000, 3),
                "outcome": outcome,
                "components": dict(self.components),
                "residual_components": len(self.residual),
            })
        except Exception:
            # Diagnostic I/O must never replace the authoritative result.
            pass
