"""Shared local-memory fact selection for card-drop HTTP entrypoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("neko.card_forge_facts")


@dataclass(frozen=True)
class ActiveNekoContext:
    master_name: str
    lanlan_name: str
    memory_dir: Path | None
    facts_path: Path | None
    lanlan_prompt: str = ""
    source: str = "neko-config"


def _safe_character_segment(name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return None
    value = name.strip()
    if not value or len(value) > 80:
        return None
    if any(part in value for part in ("/", "\\", "..", "\x00", ":")):
        return None
    return value


def _resolve_memory_dir(config_manager: Any) -> Path | None:
    env_memory_dir = os.environ.get("NEKO_MEMORY_DIR", "").strip()
    if env_memory_dir:
        return Path(env_memory_dir)
    memory_dir = getattr(config_manager, "memory_dir", None)
    return Path(memory_dir) if memory_dir else None


def _known_character_names(config_manager: Any) -> set[str] | None:
    try:
        character_data = config_manager.get_character_data()
    except Exception:
        return None
    if len(character_data) <= 5 or not isinstance(character_data[5], dict):
        return None
    return {
        str(name).strip()
        for name in character_data[5]
        if isinstance(name, str) and name.strip()
    }


def _resolve_prompt(config_manager: Any, lanlan_name: str, master_name: str) -> str:
    try:
        character_data = config_manager.get_character_data()
        prompt_map = (
            character_data[5]
            if len(character_data) > 5 and isinstance(character_data[5], dict)
            else {}
        )
        prompt = str(prompt_map.get(lanlan_name, "") or "")
        return prompt.replace("{LANLAN_NAME}", lanlan_name).replace(
            "{MASTER_NAME}", master_name
        )
    except Exception:
        return ""


def _build_context(
    config_manager: Any,
    character_override: str | None = None,
) -> ActiveNekoContext:
    master_name, current_lanlan, *_rest = config_manager.get_character_data()
    master = str(master_name or "").strip()
    active_lanlan = _safe_character_segment(str(current_lanlan or ""))
    override_lanlan = _safe_character_segment(str(character_override or ""))
    known_names = _known_character_names(config_manager)
    # Browser-provided runtime hints never select another role's memory. A
    # character override reaches this helper only after the explicit debug
    # feature flag is checked, and must still name a configured character.
    selected_lanlan = override_lanlan if character_override is not None else active_lanlan
    lanlan = (
        selected_lanlan
        if selected_lanlan and known_names and selected_lanlan in known_names
        else ""
    )

    direct_facts = os.environ.get("NEKO_FACTS_JSON", "").strip()
    memory_dir = _resolve_memory_dir(config_manager)
    if direct_facts and lanlan:
        facts_path = Path(direct_facts)
        source = "env-facts-json"
    elif memory_dir and lanlan:
        facts_path = memory_dir / lanlan / "facts.json"
        source = "character-override" if character_override is not None else "neko-config"
    else:
        facts_path = None
        source = "unresolved"

    return ActiveNekoContext(
        master_name=master,
        lanlan_name=lanlan,
        memory_dir=memory_dir,
        facts_path=facts_path,
        lanlan_prompt=_resolve_prompt(config_manager, lanlan, master),
        source=source,
    )


async def resolve_active_neko_context(
    character_override: str | None = None,
) -> ActiveNekoContext:
    """Resolve the active, configured character without blocking the event loop."""
    import asyncio

    from utils.config_manager import get_config_manager

    config_manager = get_config_manager()
    return await asyncio.to_thread(
        _build_context,
        config_manager,
        character_override,
    )


def _load_facts_json(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("forge-facts: failed to read %s: %s", path, type(exc).__name__)
        return []


async def _fetch_facts_from_url(url: str) -> list[dict[str, Any]] | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            logger.warning("forge-facts: configured URL returned %s", response.status_code)
            return None
        data = response.json()
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and isinstance(data.get("facts"), list):
            return [item for item in data["facts"] if isinstance(item, dict)]
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        logger.exception("forge-facts: configured URL fetch failed")
    return None


def _parse_fact_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fact_memory_datetime(item: dict[str, Any]) -> datetime | None:
    return _parse_fact_datetime(item.get("event_start_at")) or _parse_fact_datetime(
        item.get("created_at")
    )


def _safe_importance(value: Any) -> int:
    try:
        return int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0


def _importance_weight(item: dict[str, Any]) -> float:
    return 1.0 + max(0, _safe_importance(item.get("importance")))


def _weighted_pick(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    pool = [
        item
        for item in items
        if item.get("id")
        or item.get("hash")
        or item.get("_forge_fid")
        or item.get("_forge_hash")
    ]
    picked: list[dict[str, Any]] = []
    for _ in range(min(count, len(pool))):
        total = sum(_importance_weight(item) for item in pool)
        roll = random.uniform(0, total)
        cursor = 0.0
        chosen_index = 0
        for index, item in enumerate(pool):
            cursor += _importance_weight(item)
            if roll <= cursor:
                chosen_index = index
                break
        picked.append(pool.pop(chosen_index))
    return picked


_FactKey = tuple[str, ...]
_FORGE_WIRE_ID_PREFIX = "__neko_forge_id_v1__:"


def _fact_identity(item: dict[str, Any]) -> tuple[_FactKey | None, str, set[str]]:
    text = str(item.get("text") or "")
    raw_hash = str(item.get("hash") or "")
    subject_fields = tuple(
        str(item.get(field) or "").strip()
        for field in ("subject_kind", "subject_id", "scope")
    )
    subject_key = subject_fields if any(subject_fields) else ()
    # Equal text in different subject domains is distinct in FactStore.
    text_identity = (
        json.dumps([subject_key, text], ensure_ascii=False, separators=(",", ":"))
        if subject_key else text
    )
    text_hash = hashlib.sha1(text_identity.encode("utf-8")).hexdigest() if text else ""
    raw_id = item.get("id")
    if isinstance(raw_id, (str, int, float, bool)) and raw_id != "":
        fact_key: _FactKey | None = (f"id:{type(raw_id).__name__}", str(raw_id))
    elif raw_hash:
        fact_key = ("hash", raw_hash)
    elif text_hash:
        fact_key = ("text", text_hash)
    else:
        fact_key = None
    if fact_key is not None:
        fact_key = (*fact_key, *subject_key)
    return fact_key, raw_hash or text_hash, {value for value in (raw_hash, text_hash) if value}


def _fact_wire_id(fact_key: _FactKey) -> str:
    """Preserve ordinary string IDs; encode other identities for unique round trips."""
    kind, value, *subject_key = fact_key
    # Old responses erased scalar types. Move ambiguous string forms too, so
    # a historical forged ID cannot hide another type; its hash still excludes it.
    legacy_scalar_string = value in {"True", "False"}
    if kind == "id:str":
        try:
            float(value)
        except ValueError:
            pass
        else:
            legacy_scalar_string = True
    if (
        kind == "id:str"
        and not subject_key
        and not legacy_scalar_string
        and not value.startswith((_FORGE_WIRE_ID_PREFIX, "hash:", "text:"))
        and 1 <= len(value) <= 128
        and value == value.strip()
        and not any(char == "," or ord(char) < 32 for char in value)
    ):
        return value
    encoded = json.dumps(fact_key, ensure_ascii=False, separators=(",", ":"))
    return _FORGE_WIRE_ID_PREFIX + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _legacy_excluded_fact_keys(
    raw: list[dict[str, Any]],
    raw_archive: list[dict[str, Any]],
    exclude_ids: set[str],
) -> set[_FactKey]:
    """Resolve pre-scope wire IDs only when the entire memory pool is unambiguous."""
    if not exclude_ids:
        return set()
    # Eligibility can change after forging. Ignoring a now-private/archived row
    # would reassign its historical ID to another subject with the same raw ID.
    matches: dict[str, set[_FactKey]] = {}
    for collection in (raw, raw_archive):
        for item in collection:
            if not isinstance(item, dict):
                continue
            fact_key, _, _ = _fact_identity(item)
            if fact_key is None:
                continue
            legacy_key = fact_key
            if len(fact_key) > 2:
                legacy_key, _, _ = _fact_identity({
                    **item, "subject_kind": None, "subject_id": None, "scope": None,
                })
            if legacy_key is None:
                continue
            legacy_id = _fact_wire_id(legacy_key)
            if legacy_id in exclude_ids:
                matches.setdefault(legacy_id, set()).add(fact_key)
    return {next(iter(keys)) for keys in matches.values() if len(keys) == 1}


def _memory_identity_stats(
    raw: list[dict[str, Any]], raw_archive: list[dict[str, Any]],
) -> tuple[int, set[_FactKey], set[str]]:
    """Count accumulated memories before eligibility filters; active rows win overlaps."""
    ids: set[_FactKey] = set()
    hashes: set[str] = set()
    active_ids: set[_FactKey] = set()
    active_hashes: set[str] = set()
    count = 0
    for collection, is_active in ((raw, True), (raw_archive, False)):
        for item in collection:
            if not isinstance(item, dict):
                continue
            fact_id, _, fact_hashes = _fact_identity(item)
            if fact_id is None:
                continue
            if fact_id not in ids and not fact_hashes.intersection(hashes):
                count += 1
            ids.add(fact_id)
            hashes.update(fact_hashes)
            if is_active:
                active_ids.add(fact_id)
                active_hashes.update(fact_hashes)
    return count, active_ids, active_hashes


def _select_forge_facts_with_stats(
    raw: list[dict[str, Any]],
    *,
    min_importance: int = 5,
    include_absorbed: bool = True,
    limit: int = 5,
    exclude_ids: set[str] | None = None,
    exclude_hashes: set[str] | None = None,
    exclude_keys: set[_FactKey] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    exclude_ids = exclude_ids or set()
    exclude_hashes = exclude_hashes or set()
    exclude_keys = exclude_keys or set()
    filtered: list[dict[str, Any]] = []
    missing_id_count = 0
    excluded_count = 0
    absorbed_count = 0
    low_importance_count = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        fact_key, hash_key, hash_aliases = _fact_identity(item)
        has_scalar_id = fact_key is not None and fact_key[0].startswith("id:")
        if not has_scalar_id:
            missing_id_count += 1
        if fact_key is None or not str(item.get("text") or "").strip():
            continue
        wire_id = _fact_wire_id(fact_key)
        if wire_id in exclude_ids or fact_key in exclude_keys or hash_aliases.intersection(exclude_hashes):
            excluded_count += 1
            continue
        if item.get("private") is True or item.get("redacted") is True:
            excluded_count += 1
            continue
        if not include_absorbed and item.get("absorbed"):
            absorbed_count += 1
            continue
        importance = _safe_importance(item.get("importance"))
        if importance < min_importance:
            low_importance_count += 1
            continue
        filtered.append({
            **item, "_forge_fid": fact_key, "_forge_wire_id": wire_id, "_forge_hash": hash_key,
            "_forge_hash_aliases": hash_aliases,
        })

    random.shuffle(filtered)
    deduped: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    seen_ids: set[_FactKey] = set()
    duplicate_count = 0
    for item in filtered:
        fact_key = item["_forge_fid"]
        hash_aliases = item["_forge_hash_aliases"]
        if fact_key in seen_ids or hash_aliases.intersection(seen_hashes):
            duplicate_count += 1
            continue
        if fact_key:
            seen_ids.add(fact_key)
        seen_hashes.update(hash_aliases)
        deduped.append(item)

    def item_key(item: dict[str, Any]) -> _FactKey:
        return item["_forge_fid"]

    recent_count = 0
    distant_count = 0
    weighted_count = 0
    if len(deduped) >= limit and limit >= 5:
        recent_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        recent_candidates = [
            item
            for item in deduped
            if (created := _parse_fact_datetime(item.get("created_at"))) is not None
            and created >= recent_cutoff
        ]
        recent_candidates.sort(
            key=lambda item: (
                _importance_weight(item),
                _parse_fact_datetime(item.get("created_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        recent = _weighted_pick(recent_candidates, 2)
        recent.sort(
            key=lambda item: (
                _importance_weight(item),
                _parse_fact_datetime(item.get("created_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        used = {item_key(item) for item in recent}
        for item in recent:
            item["_forge_recent_guaranteed"] = True

        distant_candidates = [
            item
            for item in deduped
            if item_key(item) not in used and _fact_memory_datetime(item) is not None
        ]
        distant_candidates.sort(
            key=lambda item: (
                _fact_memory_datetime(item) or datetime.max.replace(tzinfo=timezone.utc),
                -_importance_weight(item),
            )
        )
        oldest_size = max(
            1,
            min(len(distant_candidates), max(1, len(distant_candidates) // 4)),
        )
        oldest_pool = distant_candidates[:oldest_size]
        oldest_keys = {item_key(item) for item in oldest_pool}
        distant = _weighted_pick(oldest_pool, 1)
        for item in distant:
            item["_forge_distant_guaranteed"] = True
        used.update(item_key(item) for item in distant)

        needed = max(0, limit - len(recent) - len(distant))
        random_candidates = [
            item
            for item in deduped
            if item_key(item) not in used and item_key(item) not in oldest_keys
        ]
        weighted = _weighted_pick(random_candidates, needed)
        if len(weighted) < needed:
            used.update(item_key(item) for item in weighted)
            fallback = [item for item in deduped if item_key(item) not in used]
            random.shuffle(fallback)
            weighted.extend(fallback[: needed - len(weighted)])
        random.shuffle(weighted)
        picked = [*recent, *weighted, *distant][:limit]
        if len(picked) < limit:
            used = {item_key(item) for item in picked}
            fallback = [item for item in deduped if item_key(item) not in used]
            random.shuffle(fallback)
            picked.extend(fallback[: limit - len(picked)])
        if len(picked) >= limit and distant:
            distant_item = distant[0]
            picked = [item for item in picked if item_key(item) != item_key(distant_item)]
            picked = [*picked[: limit - 1], distant_item]
        picked = picked[:limit]
        recent_count = sum(bool(item.get("_forge_recent_guaranteed")) for item in picked)
        distant_count = sum(bool(item.get("_forge_distant_guaranteed")) for item in picked)
        weighted_count = len(picked) - recent_count - distant_count
    else:
        random.shuffle(deduped)
        picked = deduped[:limit]

    facts = [
        {
            "id": item["_forge_wire_id"],
            "text": str(item.get("text", "")),
            "importance": _safe_importance(item.get("importance")),
            "entity": str(item.get("entity", "")),
            "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
            "created_at": item.get("created_at"),
            "event_start_at": item.get("event_start_at"),
            "hash": str(item.get("hash") or item.get("_forge_hash") or ""),
            "recentGuaranteed": bool(item.get("_forge_recent_guaranteed")),
            "distantGuaranteed": bool(item.get("_forge_distant_guaranteed")),
            "sourceCollection": str(item.get("_forge_source_collection") or "facts"),
        }
        for item in picked
    ]
    return facts, {
        "rawCount": len([item for item in raw if isinstance(item, dict)]),
        "filteredCount": len(filtered),
        "dedupedCount": len(deduped),
        "excludedCount": excluded_count,
        "lowImportanceCount": low_importance_count,
        "absorbedSkippedCount": absorbed_count,
        "missingIdCount": missing_id_count,
        "duplicateCount": duplicate_count,
        "recentGuaranteedCount": recent_count,
        "distantGuaranteedCount": distant_count,
        "weightedRandomCount": weighted_count,
    }


def _select_archive_facts(
    raw_archive: list[dict[str, Any]],
    *,
    min_importance: int,
    include_absorbed: bool,
    exclude_ids: set[str],
    exclude_hashes: set[str],
    fill_count: int = 0,
    exclude_keys: set[_FactKey] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not raw_archive:
        return [], {"archiveRawCount": 0, "archiveFilteredCount": 0}
    archive_raw_count = len([
        item for item in raw_archive if isinstance(item, dict)
    ])
    eligible_archive = [
        item for item in raw_archive
        if isinstance(item, dict)
        and not item.get("subject_archived_at")
        and not item.get("arbitration_archived_at")
        and not item.get("arbitration_reason")
    ]
    candidates, stats = _select_forge_facts_with_stats(
        eligible_archive,
        min_importance=min_importance,
        include_absorbed=include_absorbed,
        limit=len(raw_archive) + 1,
        exclude_ids=exclude_ids,
        exclude_hashes=exclude_hashes,
        exclude_keys=exclude_keys,
    )
    dated = [item for item in candidates if _fact_memory_datetime(item) is not None]
    archive_stats = {
        "archiveRawCount": archive_raw_count,
        "archiveFilteredCount": stats.get("filteredCount", 0),
    }
    dated.sort(
        key=lambda item: (
            _fact_memory_datetime(item) or datetime.max.replace(tzinfo=timezone.utc),
            -_importance_weight(item),
        )
    )
    oldest_size = max(1, min(len(dated), max(1, len(dated) // 4)))
    distant = _weighted_pick(dated[:oldest_size], 1)
    # Candidates are already deduplicated by typed identity and hash aliases.
    # Remove the selected object, since distinct scalar IDs can serialize alike.
    remaining = [item for item in candidates if not distant or item is not distant[0]]
    # Keep the distant slot last, then fill any active-memory shortfall from
    # all other eligible archive memories, including entries without dates.
    fillers = _weighted_pick(remaining, max(0, fill_count - len(distant)))
    return [
        {
            **item,
            "distantGuaranteed": is_distant,
            "recentGuaranteed": False,
            "sourceCollection": "facts_archive",
        }
        for items, is_distant in ((fillers, False), (distant, True))
        for item in items
    ], archive_stats


def _parse_csv_set(value: str | None) -> set[str]:
    if not value or not isinstance(value, str):
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def _empty_payload(character: str | None, limit: int) -> dict[str, Any]:
    return {
        "character": "",
        "factsSource": "runtime-unlinked",
        "characterOverrideIgnored": bool(character),
        "runtimeCharacterHintUsed": False,
        "facts": [],
        "requestedLimit": limit,
        "returnedCount": 0,
        "fallbackReason": "runtime_character_hint_missing",
        "error": "active_neko_runtime_not_linked",
        "rawCount": 0,
        "totalMemoryCount": 0,
        "filteredCount": 0,
        "dedupedCount": 0,
        "excludedCount": 0,
        "lowImportanceCount": 0,
        "absorbedSkippedCount": 0,
        "missingIdCount": 0,
        "duplicateCount": 0,
        "recentGuaranteedCount": 0,
        "distantGuaranteedCount": 0,
        "weightedRandomCount": 0,
    }


async def build_forge_facts_payload(
    *,
    character: str | None = None,
    runtime_character_hint: str | None = None,
    min_importance: int = 5,
    include_absorbed: bool = True,
    limit: int = 5,
    exclude_fact_ids: str | None = None,
    exclude_hashes: str | None = None,
) -> dict[str, Any]:
    """Build the local fact response consumed by the card-drop integration."""
    runtime_hint = (
        runtime_character_hint.strip()
        if isinstance(runtime_character_hint, str)
        else ""
    )
    allow_override = os.environ.get(
        "NEKO_CARD_FORGE_ALLOW_CHARACTER_OVERRIDE", ""
    ).strip() == "1"
    if not runtime_hint and not (allow_override and character):
        return _empty_payload(character, limit)

    error: str | None = None
    raw: list[dict[str, Any]] = []
    raw_archive: list[dict[str, Any]] = []
    try:
        context = await resolve_active_neko_context(
            character if allow_override else None,
        )
    except Exception as exc:
        logger.warning(
            "forge-facts: failed to resolve active NEKO context: %s",
            type(exc).__name__,
        )
        context = None
        error = "active_neko_context_unavailable"

    resolved_character = context.lanlan_name if context else ""
    facts_source = context.source if context else "unresolved"
    override_ignored = bool(
        character
        and character != resolved_character
        and not allow_override
    )
    if runtime_hint and runtime_hint != resolved_character:
        return _empty_payload(character, limit)
    runtime_hint_used = bool(runtime_hint and runtime_hint == resolved_character)

    remote_facts_used = False
    url_template = os.environ.get("NEKO_FORGE_FACTS_URL", "").strip()
    if url_template:
        try:
            url = url_template.format(character=resolved_character or "")
        except (KeyError, IndexError, ValueError):
            url = url_template
        fetched = await _fetch_facts_from_url(url)
        if fetched is not None:
            raw = fetched
            remote_facts_used = bool(fetched)

    if not raw:
        path = context.facts_path if context else None
        if path is None:
            error = "facts_source_not_configured"
        else:
            raw = await asyncio.to_thread(_load_facts_json, path)
            if not raw and error is None:
                error = "facts_file_empty_or_missing"

    archive_path = (
        context.facts_path.with_name("facts_archive.json")
        if context and context.facts_path
        else None
    )
    # Local archives only belong to the local fallback, never to a remote pool.
    if archive_path is not None and not remote_facts_used:
        raw_archive = await asyncio.to_thread(_load_facts_json, archive_path)

    excluded_ids = _parse_csv_set(exclude_fact_ids)
    excluded_hashes = _parse_csv_set(exclude_hashes)
    total_memory_count, active_ids, active_hashes = await asyncio.to_thread(
        _memory_identity_stats, raw, raw_archive,
    )
    legacy_excluded_keys = await asyncio.to_thread(
        _legacy_excluded_fact_keys, raw, raw_archive, excluded_ids,
    )
    facts, stats = await asyncio.to_thread(
        _select_forge_facts_with_stats,
        raw,
        min_importance=min_importance,
        include_absorbed=include_absorbed,
        limit=limit,
        exclude_ids=excluded_ids,
        exclude_hashes=excluded_hashes,
        exclude_keys=legacy_excluded_keys,
    )
    archive_facts: list[dict[str, Any]] = []
    archive_stats = {
        "archiveRawCount": len([item for item in raw_archive if isinstance(item, dict)]),
        "archiveFilteredCount": 0,
    }
    if limit >= 5 and raw_archive:
        # 排除集取自**全部**活跃 facts，而不是这次抽中的那几条：归档的两文件
        # 提交（先 facts_archive.json 再 facts.json）被打断时同一行会同时留在
        # 两个文件里，最长到下一次成功归档才收敛。只按抽中的几条排除的话，
        # 那种行只要这轮没被抽中，就会作为"久远记忆"发出去，而它其实还活着。
        archive_facts, archive_stats = await asyncio.to_thread(
            _select_archive_facts,
            raw_archive,
            min_importance=min_importance,
            include_absorbed=include_absorbed,
            exclude_ids=excluded_ids,
            exclude_keys=active_ids | legacy_excluded_keys,
            exclude_hashes=excluded_hashes | active_hashes,
            fill_count=max(0, limit - len(facts)),
        )
        if archive_facts:
            facts = [*facts[: limit - len(archive_facts)], *archive_facts]
            recent_count = sum(bool(item.get("recentGuaranteed")) for item in facts)
            distant_count = sum(bool(item.get("distantGuaranteed")) for item in facts)
            stats["recentGuaranteedCount"] = recent_count
            stats["distantGuaranteedCount"] = distant_count
            stats["weightedRandomCount"] = max(
                0, len(facts) - recent_count - distant_count
            )

    fallback_reason = ""
    if len(facts) >= limit:
        pass
    elif facts:
        fallback_reason = "insufficient_facts"
    elif error:
        fallback_reason = error
    elif not raw:
        fallback_reason = "facts_file_empty_or_missing"
    elif stats.get("excludedCount", 0) > 0 and stats.get("filteredCount", 0) == 0:
        fallback_reason = "all_available_facts_excluded"
    elif stats.get("filteredCount", 0) == 0:
        fallback_reason = "no_facts_after_filter"

    payload: dict[str, Any] = {
        "character": resolved_character,
        "factsSource": facts_source,
        "characterOverrideIgnored": override_ignored,
        "runtimeCharacterHintUsed": runtime_hint_used,
        "facts": facts,
        "requestedLimit": limit,
        "returnedCount": len(facts),
        "fallbackReason": fallback_reason,
        "archiveDistantCount": sum(bool(item["distantGuaranteed"]) for item in archive_facts),
        "totalMemoryCount": total_memory_count,
        **archive_stats,
        **stats,
    }
    if error and not facts:
        payload["error"] = error
    return payload
