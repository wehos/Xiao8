"""Transportable entry controls shared by config registration and metadata IPC.

Source selection/precedence belongs to the caller. Only present fields are
selected: an explicit false, null, empty list or empty mapping is not a fallback.
Python validation models and callables stay in the plugin process.
"""

from __future__ import annotations

from collections.abc import Mapping


ENTRY_CONTRACT_FIELDS = (
    "kind",
    "auto_start",
    "enabled",
    "dynamic",
    "persist",
    "model_validate",
    "timeout",
    "llm_result_fields",
    "llm_result_schema",
    "return_message",
    "metadata",
    "extra",
    "quick_action",
    "quick_action_config",
)


def entry_contract_fields(source: object) -> dict[str, object]:
    """Select controls; callers copy or JSON-normalize values at their boundary."""
    missing = object()
    fields: dict[str, object] = {}
    for name in ENTRY_CONTRACT_FIELDS:
        value = (
            source.get(name, missing)
            if isinstance(source, Mapping)
            else getattr(source, name, missing)
        )
        if value is not missing:
            fields[name] = value
    return fields
