# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Generic candidate rendering and fair selection for proactive chat."""

from __future__ import annotations

from typing import Any

from .decisions import _should_skip_source
from .state import _source_hash


def _escape_phase1_community_text(value: Any) -> str:
    """Serialize public community text without reproducing prompt delimiters."""

    text = " ".join(str(value or "").split())
    return (
        text.replace("\\", "\\\\")
        .replace("|", r"\u007c")
        .replace("=", r"\u003d")
        .replace("<", r"\u003c")
        .replace(">", r"\u003e")
    )


def _format_phase1_link_candidate(index: int, item: dict[str, Any]) -> str:
    """Render useful candidate evidence without leaking bulky raw metadata."""

    is_community_card = item.get("mode") == "community"

    def _field_text(value: Any) -> str:
        text = " ".join(str(value or "").split())
        if not is_community_card:
            return text
        # Community card fields are public, untrusted text inside a prompt
        # section whose structural delimiters use ``=`` and ``|``.
        return _escape_phase1_community_text(text)

    title = str(item.get("phase1_title") or _field_text(item.get("title"))).strip()
    details: list[str] = []
    field_labels = (
        ("source", "来源"),
        ("author", "作者"),
        ("reason", "推荐依据"),
        ("description_hint", "简介"),
        ("tags", "标签"),
        ("url", "URL"),
    )
    for field, label in field_labels:
        raw_value = item.get(field)
        if field == "tags" and isinstance(raw_value, list):
            value = "、".join(
                _field_text(tag)
                for tag in raw_value
                if _field_text(tag)
            )
        else:
            value = _field_text(raw_value)
        if not value:
            continue
        if field == "description_hint":
            value = value[:240]
        details.append(f"{label}: {value}")
    published_at = item.get("published_at")
    if published_at:
        details.append(f"发布时间戳: {_field_text(published_at)}")
    suffix = f" | {' | '.join(details)}" if details else ""
    return f"{index}. {title}{suffix}"


def _number_phase1_links_by_source(
    links: list[dict[str, Any]], *, source_positions: dict[str, int] | None = None
) -> list[tuple[int, dict[str, Any]]]:
    """Number displayable candidates globally for each source in a Phase 1 prompt."""

    positions = source_positions if source_positions is not None else {}
    numbered: list[tuple[int, dict[str, Any]]] = []
    for link in links:
        if not str(link.get("title") or "").strip():
            continue
        source = " ".join(str(link.get("source") or "").split()).casefold()
        positions[source] = positions.get(source, 0) + 1
        numbered.append((positions[source], link))
    return numbered


def _phase1_linkless_modes(
    modes: list[str], sources: dict[str, Any]
) -> list[str]:
    """Return formatted-only modes that each need a Phase 1 budget slot."""

    return [
        mode
        for mode in modes
        if not ((sources.get(mode) or {}).get("links") or [])
        and str((sources.get(mode) or {}).get("formatted_content") or "").strip()
    ]


def _round_robin_phase1_links(
    modes: list[str],
    sources: dict[str, Any],
    *,
    total: int,
) -> dict[str, list[dict[str, Any]]]:
    """Give every enabled web mode candidates before any one mode can dominate."""

    selected = {mode: [] for mode in modes}
    positions = {mode: 0 for mode in modes}
    links_by_mode = {
        mode: list((sources.get(mode) or {}).get("links", []) or [])
        for mode in modes
    }
    seen_keys: set[str] = set()
    remaining = max(0, total)
    while remaining:
        made_progress = False
        for mode in modes:
            links = links_by_mode[mode]
            while positions[mode] < len(links):
                link = dict(links[positions[mode]])
                positions[mode] += 1
                key = _source_hash(
                    link.get("dedupe_key") or link.get("url", ""),
                    link.get("title", ""),
                )
                if key and (key in seen_keys or _should_skip_source(key)):
                    continue
                if key:
                    seen_keys.add(key)
                link.setdefault("mode", mode)
                if link["mode"] == "community":
                    link["phase1_title"] = _escape_phase1_community_text(
                        link.get("title")
                    )
                selected[mode].append(link)
                remaining -= 1
                made_progress = True
                break
            if remaining <= 0:
                break
        if not made_progress:
            break
    return selected
