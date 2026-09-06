"""One upstream attempt's usage snapshot, independent of response presentation."""
from __future__ import annotations

from dataclasses import dataclass


_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_DETAIL_FIELDS = {
    "prompt_tokens_details": ("cached_tokens", "audio_tokens"),
    "completion_tokens_details": (
        "accepted_prediction_tokens", "audio_tokens", "reasoning_tokens", "rejected_prediction_tokens",
    ),
}


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def normalize_usage(value: object, *, protocol: str = "openai_chat") -> dict | None:
    """Keep only recognized numeric counters; never store arbitrary provider data."""
    if not isinstance(value, dict):
        return None
    if protocol == "anthropic_messages":
        if not all(_count(value.get(key)) for key in ("input_tokens", "output_tokens")):
            return None
        if not all(_count(value.get(key, 0)) for key in ("cache_read_input_tokens", "cache_creation_input_tokens")):
            return None
        cached = value.get("cache_read_input_tokens", 0)
        prompt = value["input_tokens"] + cached + value.get("cache_creation_input_tokens", 0)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": value["output_tokens"],
            "total_tokens": prompt + value["output_tokens"],
            "prompt_tokens_details": {"cached_tokens": cached},
        }
    if not all(_count(value.get(key)) for key in _TOKEN_FIELDS):
        return None
    result = {key: value[key] for key in _TOKEN_FIELDS}
    for field, keys in _DETAIL_FIELDS.items():
        details = value.get(field)
        if details is None:
            continue
        if not isinstance(details, dict):
            return None
        normalized = {}
        for key in keys:
            count = details.get(key)
            if count is not None:
                if not _count(count):
                    return None
                normalized[key] = count
        if normalized:
            result[field] = normalized
    return result


@dataclass
class AttemptObservation:
    """Mutable attempt result owned by the execution policy, never a token ledger.

    Streaming counters are cumulative snapshots, not deltas. A failed/cancelled
    stream retains its last valid snapshot as partial rather than inventing zero.
    """

    upstream_started: bool = False
    usage: dict | None = None
    usage_status: str = "unknown"

    def observe(self, value: object, *, protocol: str = "openai_chat", reported: bool = False) -> None:
        usage = normalize_usage(value, protocol=protocol)
        if usage is not None:
            self.usage = usage
            self.usage_status = "reported" if reported else "partial"
