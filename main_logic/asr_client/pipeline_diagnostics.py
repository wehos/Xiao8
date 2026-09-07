"""Content-free pipeline observations and bounded audio progress aggregation."""

from collections import OrderedDict
import re
import time

from .speaker_diagnostics import diagnostic_context

_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FIELDS = frozenset({
    "turn_id", "audio_generation", "transport_generation", "route_generation",
    "lease_generation", "detector_epoch", "sequence_no", "candidate_generation",
    "semantic_generation", "semantic_buffer_epoch", "semantic_turn_id",
    "evaluation_ms", "evaluation_id", "identity_matches", "generation_matches",
    "activity_matches", "queue_audio_ms", "audio_samples", "sample_rate_hz",
    "frame_count", "reason", "outcome", "phase", "state", "endpoint_authority",
    "speaker_enabled", "has_text", "probability_milli", "threshold_milli",
    "transport_current", "coalesced_count", "confirmation_ms", "elapsed_ms",
})


def safe_fields(fields: dict) -> dict:
    """No repr/str coercion of arbitrary objects, text, errors or biometric scores."""
    return {key: value for key, value in fields.items() if key in _FIELDS and (
        value is None or type(value) in (int, bool)
        or (type(value) is str and _LABEL.fullmatch(value))
    )}


class PipelineDiagnostics:
    """One observer per Runtime; at most 32 aggregate buckets, no timers/tasks."""

    def __init__(self, runtime, emit):
        self._runtime = runtime
        self._emit = emit
        self._progress = OrderedDict()

    def event(self, stage: str, epoch: int, **fields) -> None:
        try:
            if not _LABEL.fullmatch(stage):
                return
            record = diagnostic_context(self._runtime, epoch)
            record.update(safe_fields(fields), stage=stage, pipeline_schema=1)
            self._emit(record, capacity=32)
        except Exception:
            pass

    def audio(self, stage: str, epoch: int, *, audio_samples: int, **fields) -> None:
        """Emit first frame and cumulative progress every 5s; flush at transitions."""
        try:
            fields = safe_fields(fields)
            key = (stage, epoch, tuple(sorted(fields.items())))
            now = time.monotonic()
            bucket = self._progress.get(key)
            if bucket is None:
                if len(self._progress) >= 32:
                    old_key, old = self._progress.popitem(last=False)
                    self._publish_audio(old_key, old)
                bucket = [0, 0, now, 0]
                self._progress[key] = bucket
            bucket[0] += 1
            bucket[1] += max(0, audio_samples)
            if bucket[0] == 1 or now - bucket[2] >= 5:
                self._publish_audio(key, bucket)
                bucket[2] = now
        except Exception:
            pass

    def _publish_audio(self, key, bucket) -> None:
        if bucket[0] == bucket[3]:
            return
        stage, epoch, fields = key
        self.event(stage, epoch, **dict(fields), frame_count=bucket[0], audio_samples=bucket[1])
        bucket[3] = bucket[0]

    def flush(self) -> None:
        try:
            for key, bucket in self._progress.items():
                self._publish_audio(key, bucket)
            self._progress.clear()
        except Exception:
            pass
