# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Local/cloud character snapshot construction and the merged cloudsave
summary/detail views.

Split out of the former monolithic ``utils/cloudsave_runtime.py``.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._shared import (
    MANAGED_MEMORY_FILENAMES,
    is_cloudsave_disabled,
    is_cloudsave_provider_available,
    logger,
)
from .bindings import (
    _collect_workshop_character_origin_candidates,
    _derive_character_binding_summary,
    _derive_character_origin_metadata,
)
from .bootstrap import bootstrap_local_cloudsave_environment, load_cloudsave_manifest
from .staging import (
    _json_canonical_dumps,
    _load_json_if_exists,
    _looks_like_sqlite_database,
    _normalize_tombstones_state,
    _run_sqlite_shadow_copy,
    _sha256_bytes,
    _sha256_file,
    _stage_json_file,
    _stage_memory_file,
)


def _iso_from_timestamp(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _max_mtime_iso(paths: list[Path]) -> str:
    latest_timestamp: float | None = None
    for path in paths:
        try:
            timestamp = path.stat().st_mtime
        except OSError:
            continue
        if latest_timestamp is None or timestamp > latest_timestamp:
            latest_timestamp = timestamp
    return _iso_from_timestamp(latest_timestamp)


def _memory_file_hashes_from_root(root_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for filename in MANAGED_MEMORY_FILENAMES:
        path = root_dir / filename
        if path.is_file():
            hashes[filename] = _compute_managed_memory_file_hash(path)
    return hashes


def _compute_managed_memory_file_hash(path: Path) -> str:
    if path.name != "time_indexed.db" or not _looks_like_sqlite_database(path):
        return _sha256_file(path)

    temp_root = Path(tempfile.mkdtemp(prefix="cloudsave-hash-"))
    shadow_copy_path = temp_root / path.name
    try:
        _run_sqlite_shadow_copy(path, shadow_copy_path)
        return _sha256_file(shadow_copy_path)
    except sqlite3.DatabaseError as exc:
        logger.warning(
            "Falling back to direct SQLite file hash for %s after shadow-copy failure: %s",
            path,
            exc,
        )
        return _sha256_file(path)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _stable_binding_payload_for_fingerprint(binding_payload: Any) -> dict[str, Any]:
    if not isinstance(binding_payload, dict):
        return {}
    stable_keys = (
        "character_name",
        "model_type",
        "asset_source",
        "asset_source_id",
        "model_ref",
        "asset_display_name",
        "fallback_model_ref",
        "experience_overrides",
    )
    return {
        key: deepcopy(binding_payload.get(key))
        for key in stable_keys
        if key in binding_payload
    }


def _build_character_payload_fingerprint(
    *,
    character_name: str,
    character_payload: Any,
    binding_payload: Any,
    memory_hashes: dict[str, str],
) -> str:
    fingerprint_payload = {
        "schema_version": 1,
        "character_name": str(character_name or ""),
        "character_payload": deepcopy(character_payload) if isinstance(character_payload, dict) else {},
        "binding_payload": _stable_binding_payload_for_fingerprint(binding_payload),
        "memory_files": dict(sorted((memory_hashes or {}).items())),
    }
    return "sha256:" + _sha256_bytes(_json_canonical_dumps(fingerprint_payload).encode("utf-8"))


def _build_character_summary_warnings(*, asset_state: str, warning_scope: str) -> list[str]:
    warnings: list[str] = []
    if asset_state in {"import_required", "downloadable", "missing"}:
        if warning_scope == "local":
            warnings.append("local_resource_missing_on_this_device")
        elif warning_scope == "cloud":
            warnings.append("cloud_resource_may_be_missing_after_download")
    return warnings


def _build_character_meta_payload(
    *,
    character_name: str,
    binding_payload: dict[str, Any],
    payload_fingerprint: str,
    sequence_number: int,
    exported_at: str,
    client_id: str,
    device_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "character_name": character_name,
        "payload_fingerprint": payload_fingerprint,
        "updated_at_utc": exported_at,
        "sequence_number": int(sequence_number),
        "source_client_id": str(client_id or ""),
        "source_device_id": str(device_id or ""),
        "asset_state": str(binding_payload.get("asset_state") or ""),
        "asset_source": str(binding_payload.get("asset_source") or ""),
        "asset_source_id": str(binding_payload.get("asset_source_id") or ""),
        "origin_source": str(binding_payload.get("origin_source") or ""),
        "origin_source_id": str(binding_payload.get("origin_source_id") or ""),
        "origin_model_ref": str(binding_payload.get("origin_model_ref") or ""),
        "origin_display_name": str(binding_payload.get("origin_display_name") or ""),
    }


def _stage_single_character_cloudsave_entries(
    config_manager,
    stage_root: Path,
    *,
    character_name: str,
    character_payload: dict[str, Any],
    binding_payload: dict[str, Any],
    sequence_number: int,
    exported_at: str,
    client_id: str,
    device_id: str,
    memory_stage_overrides: dict[str, Path] | None = None,
) -> tuple[dict[str, Path], dict[str, Any]]:
    staged_entries: dict[str, Path] = {}
    object_root = f"characters/{character_name}"

    memory_root = Path(config_manager.memory_dir) / character_name
    memory_hashes: dict[str, str] = {}
    for filename in MANAGED_MEMORY_FILENAMES:
        source_path = memory_root / filename
        relative_path = f"{object_root}/memory/{filename}"
        staged_path = (memory_stage_overrides or {}).get(filename)
        if staged_path is None:
            if not source_path.is_file():
                continue
            staged_path = _stage_memory_file(stage_root, relative_path, source_path)
        staged_entries[relative_path] = staged_path
        memory_hashes[filename] = _sha256_file(staged_path)

    payload_fingerprint = _build_character_payload_fingerprint(
        character_name=character_name,
        character_payload=character_payload,
        binding_payload=binding_payload,
        memory_hashes=memory_hashes,
    )
    meta_payload = _build_character_meta_payload(
        character_name=character_name,
        binding_payload=binding_payload,
        payload_fingerprint=payload_fingerprint,
        sequence_number=sequence_number,
        exported_at=exported_at,
        client_id=client_id,
        device_id=device_id,
    )

    staged_entries[f"{object_root}/profile.json"] = _stage_json_file(
        stage_root,
        f"{object_root}/profile.json",
        character_payload,
    )
    staged_entries[f"{object_root}/binding.json"] = _stage_json_file(
        stage_root,
        f"{object_root}/binding.json",
        binding_payload,
    )
    staged_entries[f"{object_root}/meta.json"] = _stage_json_file(
        stage_root,
        f"{object_root}/meta.json",
        meta_payload,
    )
    return staged_entries, meta_payload


def _build_local_character_snapshot(
    config_manager,
    *,
    character_name: str,
    character_payload: dict[str, Any],
    characters_config_path: Path,
    workshop_origin_index: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    binding_payload = _derive_character_binding_summary(
        config_manager,
        character_name,
        character_payload,
        workshop_origin_index=workshop_origin_index,
    )
    memory_root = Path(config_manager.memory_dir) / character_name
    memory_hashes = _memory_file_hashes_from_root(memory_root)
    updated_paths = [characters_config_path]
    updated_paths.extend(memory_root / filename for filename in memory_hashes)
    return {
        "character_name": character_name,
        "display_name": str(character_payload.get("档案名") or character_name),
        "model_type": str(binding_payload.get("model_type") or ""),
        "asset_source": str(binding_payload.get("asset_source") or ""),
        "asset_source_id": str(binding_payload.get("asset_source_id") or ""),
        "asset_state": str(binding_payload.get("asset_state") or ""),
        "origin_source": str(binding_payload.get("origin_source") or ""),
        "origin_source_id": str(binding_payload.get("origin_source_id") or ""),
        "origin_model_ref": str(binding_payload.get("origin_model_ref") or ""),
        "origin_display_name": str(binding_payload.get("origin_display_name") or ""),
        "updated_at_utc": _max_mtime_iso(updated_paths),
        "fingerprint": _build_character_payload_fingerprint(
            character_name=character_name,
            character_payload=character_payload,
            binding_payload=binding_payload,
            memory_hashes=memory_hashes,
        ),
        "warnings": _build_character_summary_warnings(
            asset_state=str(binding_payload.get("asset_state") or ""),
            warning_scope="local",
        ),
    }


def _collect_cloudsave_catalog_entries(config_manager) -> dict[str, dict[str, Any]]:
    payload = _load_json_if_exists(config_manager.cloudsave_catalog_dir / "catgirls_index.json")
    if not isinstance(payload, dict):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for entry in payload.get("characters") or []:
        if not isinstance(entry, dict):
            continue
        character_name = str(entry.get("character_name") or "").strip()
        if not character_name:
            continue
        entries[character_name] = entry
    return entries


def _load_cloudsave_tombstone_names(config_manager) -> set[str]:
    tombstones_payload = _load_json_if_exists(config_manager.cloudsave_catalog_dir / "character_tombstones.json")
    tombstones_state = _normalize_tombstones_state(tombstones_payload)
    return {
        entry["character_name"]
        for entry in tombstones_state.get("tombstones") or []
        if isinstance(entry, dict) and entry.get("character_name")
    }


def _load_cloudsave_sharded_character_unit(config_manager, character_name: str) -> dict[str, Any] | None:
    object_dir = config_manager.cloudsave_dir / "characters" / character_name
    profile_path = object_dir / "profile.json"
    if not profile_path.is_file():
        return None

    profile_payload = _load_json_if_exists(profile_path)
    if not isinstance(profile_payload, dict):
        raise ValueError(f"cloudsave shard profile is invalid for {character_name}")

    binding_payload = _load_json_if_exists(object_dir / "binding.json")
    if binding_payload is not None and not isinstance(binding_payload, dict):
        raise ValueError(f"cloudsave shard binding is invalid for {character_name}")

    meta_payload = _load_json_if_exists(object_dir / "meta.json")
    if meta_payload is not None and not isinstance(meta_payload, dict):
        raise ValueError(f"cloudsave shard meta is invalid for {character_name}")

    memory_files: dict[str, Path] = {}
    memory_dir = object_dir / "memory"
    if memory_dir.is_dir():
        for filename in MANAGED_MEMORY_FILENAMES:
            source_path = memory_dir / filename
            if source_path.is_file():
                memory_files[filename] = source_path

    return {
        "character_name": character_name,
        "profile": profile_payload,
        "binding": binding_payload or {},
        "meta": meta_payload or {},
        "memory_files": memory_files,
    }


def _collect_cloudsave_meta_payloads(config_manager) -> dict[str, dict[str, Any]]:
    meta_payloads: dict[str, dict[str, Any]] = {}
    sharded_root = config_manager.cloudsave_dir / "characters"
    if not sharded_root.is_dir():
        return meta_payloads
    for child in sorted(sharded_root.iterdir()):
        if not child.is_dir():
            continue
        payload = _load_json_if_exists(child / "meta.json")
        if isinstance(payload, dict):
            meta_payloads[child.name] = payload
    return meta_payloads


def _collect_cloudsave_binding_payloads(config_manager) -> dict[str, dict[str, Any]]:
    binding_payloads: dict[str, dict[str, Any]] = {}
    sharded_root = config_manager.cloudsave_dir / "characters"
    if sharded_root.is_dir():
        for child in sorted(sharded_root.iterdir()):
            if not child.is_dir():
                continue
            payload = _load_json_if_exists(child / "binding.json")
            if isinstance(payload, dict):
                binding_payloads[child.name] = payload
    bindings_dir = config_manager.cloudsave_bindings_dir
    if not bindings_dir.is_dir():
        bindings_dir = None
    if bindings_dir is not None:
        for path in sorted(bindings_dir.glob("*.json")):
            payload = _load_json_if_exists(path)
            if not isinstance(payload, dict):
                continue
            character_name = str(payload.get("character_name") or path.stem).strip()
            if not character_name or character_name in binding_payloads:
                continue
            binding_payloads[character_name] = payload
    return binding_payloads


def _collect_cloudsave_memory_hashes(config_manager, character_name: str) -> tuple[dict[str, str], list[Path]]:
    sharded_memory_root = config_manager.cloudsave_dir / "characters" / character_name / "memory"
    sharded_hashes = _memory_file_hashes_from_root(sharded_memory_root)
    if sharded_hashes:
        return sharded_hashes, [sharded_memory_root / filename for filename in sharded_hashes]

    legacy_memory_root = config_manager.cloudsave_memory_dir / character_name
    legacy_hashes = _memory_file_hashes_from_root(legacy_memory_root)
    return legacy_hashes, [legacy_memory_root / filename for filename in legacy_hashes]


def _load_cloudsave_character_unit(config_manager, character_name: str) -> dict[str, Any] | None:
    tombstone_names = _load_cloudsave_tombstone_names(config_manager)
    if character_name in tombstone_names:
        return None

    sharded_unit = _load_cloudsave_sharded_character_unit(config_manager, character_name)
    if sharded_unit is not None:
        return sharded_unit

    characters_payload = _load_json_if_exists(config_manager.cloudsave_profiles_dir / "characters.json")
    if not isinstance(characters_payload, dict):
        return None
    character_payload = (characters_payload.get("猫娘") or {}).get(character_name)
    if not isinstance(character_payload, dict):
        return None

    binding_payload = _load_json_if_exists(config_manager.cloudsave_bindings_dir / f"{character_name}.json")
    if binding_payload is not None and not isinstance(binding_payload, dict):
        raise ValueError(f"cloudsave binding payload is invalid for {character_name}")

    memory_files: dict[str, Path] = {}
    memory_dir = config_manager.cloudsave_memory_dir / character_name
    for filename in MANAGED_MEMORY_FILENAMES:
        source_path = memory_dir / filename
        if source_path.is_file():
            memory_files[filename] = source_path

    detail = build_cloudsave_character_detail(config_manager, character_name)
    return {
        "character_name": character_name,
        "profile": character_payload,
        "binding": binding_payload or {},
        "meta": {
            "schema_version": 1,
            "character_name": character_name,
            "payload_fingerprint": str((((detail or {}).get("cloud_summary") or {}).get("fingerprint")) or ""),
            "updated_at_utc": str((((detail or {}).get("cloud_summary") or {}).get("updated_at_utc")) or ""),
            "sequence_number": 0,
            "source_client_id": "",
            "source_device_id": "",
            "asset_state": str((((detail or {}).get("cloud_summary") or {}).get("asset_state")) or ""),
            "asset_source": str((((detail or {}).get("cloud_summary") or {}).get("asset_source")) or ""),
            "asset_source_id": str((((detail or {}).get("cloud_summary") or {}).get("asset_source_id")) or ""),
            "origin_source": str((((detail or {}).get("cloud_summary") or {}).get("origin_source")) or ""),
            "origin_source_id": str((((detail or {}).get("cloud_summary") or {}).get("origin_source_id")) or ""),
            "origin_model_ref": str((((detail or {}).get("cloud_summary") or {}).get("origin_model_ref")) or ""),
            "origin_display_name": str((((detail or {}).get("cloud_summary") or {}).get("origin_display_name")) or ""),
        },
        "memory_files": memory_files,
    }


def _build_cloud_character_snapshot(
    config_manager,
    *,
    character_name: str,
    character_payload: dict[str, Any],
    binding_payloads: dict[str, dict[str, Any]],
    meta_payloads: dict[str, dict[str, Any]],
    manifest_exported_at: str,
    catalog_entries: dict[str, dict[str, Any]],
    workshop_origin_index: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    binding_payload = deepcopy(binding_payloads.get(character_name) or {})
    meta_payload = deepcopy(meta_payloads.get(character_name) or {})
    memory_hashes, memory_paths = _collect_cloudsave_memory_hashes(config_manager, character_name)
    object_dir = config_manager.cloudsave_dir / "characters" / character_name
    payload_paths = [
        object_dir / "profile.json",
        object_dir / "binding.json",
        object_dir / "meta.json",
        config_manager.cloudsave_profiles_dir / "characters.json",
        config_manager.cloudsave_bindings_dir / f"{character_name}.json",
    ]
    payload_paths.extend(memory_paths)
    catalog_entry = catalog_entries.get(character_name) or {}
    asset_state = str(
        binding_payload.get("asset_state")
        or catalog_entry.get("asset_state")
        or meta_payload.get("asset_state")
        or ""
    )
    default_origin_payload = _derive_character_origin_metadata(
        config_manager,
        character_name=character_name,
        character_payload=character_payload,
        model_type=str(binding_payload.get("model_type") or catalog_entry.get("model_type") or ""),
        workshop_origin_index=workshop_origin_index,
    )
    updated_at_utc = str(
        meta_payload.get("updated_at_utc")
        or _max_mtime_iso(payload_paths)
        or manifest_exported_at
    )
    return {
        "character_name": character_name,
        "display_name": str(character_payload.get("档案名") or catalog_entry.get("display_name") or character_name),
        "model_type": str(
            binding_payload.get("model_type")
            or catalog_entry.get("model_type")
            or ""
        ),
        "asset_source": str(
            binding_payload.get("asset_source")
            or catalog_entry.get("asset_source")
            or meta_payload.get("asset_source")
            or ""
        ),
        "asset_source_id": str(
            binding_payload.get("asset_source_id")
            or catalog_entry.get("asset_source_id")
            or meta_payload.get("asset_source_id")
            or ""
        ),
        "asset_state": asset_state,
        "origin_source": str(
            binding_payload.get("origin_source")
            or catalog_entry.get("origin_source")
            or meta_payload.get("origin_source")
            or default_origin_payload.get("origin_source")
            or ""
        ),
        "origin_source_id": str(
            binding_payload.get("origin_source_id")
            or catalog_entry.get("origin_source_id")
            or meta_payload.get("origin_source_id")
            or default_origin_payload.get("origin_source_id")
            or ""
        ),
        "origin_model_ref": str(
            binding_payload.get("origin_model_ref")
            or catalog_entry.get("origin_model_ref")
            or meta_payload.get("origin_model_ref")
            or default_origin_payload.get("origin_model_ref")
            or ""
        ),
        "origin_display_name": str(
            binding_payload.get("origin_display_name")
            or catalog_entry.get("origin_display_name")
            or meta_payload.get("origin_display_name")
            or default_origin_payload.get("origin_display_name")
            or ""
        ),
        "updated_at_utc": updated_at_utc,
        "fingerprint": _build_character_payload_fingerprint(
            character_name=character_name,
            character_payload=character_payload,
            binding_payload=binding_payload,
            memory_hashes=memory_hashes,
        ),
        "warnings": _build_character_summary_warnings(
            asset_state=asset_state,
            warning_scope="cloud",
        ),
    }


def _load_cloudsave_character_payloads(config_manager) -> tuple[dict[str, dict[str, Any]], set[str]]:
    tombstone_names = _load_cloudsave_tombstone_names(config_manager)
    cloud_characters: dict[str, dict[str, Any]] = {}

    for profile_name in ("characters.json", "character_collection.json"):
        characters_payload = _load_json_if_exists(config_manager.cloudsave_profiles_dir / profile_name)
        if not isinstance(characters_payload, dict):
            continue
        for character_name, character_payload in (characters_payload.get("猫娘") or {}).items():
            if character_name in tombstone_names or not isinstance(character_payload, dict):
                continue
            cloud_characters[character_name] = character_payload

    sharded_root = config_manager.cloudsave_dir / "characters"
    if sharded_root.is_dir():
        for child in sorted(sharded_root.iterdir()):
            if not child.is_dir():
                continue
            payload = _load_json_if_exists(child / "profile.json")
            if child.name in tombstone_names or not isinstance(payload, dict):
                continue
            # Prefer per-character shards when both formats are present.
            cloud_characters[child.name] = payload

    return cloud_characters, tombstone_names


def _merge_character_summary_item(
    *,
    character_name: str,
    local_summary: dict[str, Any] | None,
    cloud_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    local_exists = local_summary is not None
    cloud_exists = cloud_summary is not None

    local_fingerprint = str((local_summary or {}).get("fingerprint") or "")
    cloud_fingerprint = str((cloud_summary or {}).get("fingerprint") or "")
    relation_state = "local_only"
    if local_exists and cloud_exists:
        relation_state = "matched" if local_fingerprint and local_fingerprint == cloud_fingerprint else "diverged"
    elif cloud_exists:
        relation_state = "cloud_only"

    available_actions: list[str] = []
    if relation_state == "local_only":
        available_actions = ["upload"]
    elif relation_state == "cloud_only":
        available_actions = ["download"]
    elif relation_state == "diverged":
        available_actions = ["upload", "download"]

    # Warning text is phrased for the current device, so when a local character exists
    # we should trust the local asset check and avoid leaking cloud-side warning state.
    warnings_source = local_summary if local_exists else cloud_summary
    warnings = list((warnings_source or {}).get("warnings") or [])

    deduped_warnings: list[str] = []
    for warning in warnings:
        if warning not in deduped_warnings:
            deduped_warnings.append(warning)

    primary_summary = local_summary or cloud_summary or {}
    return {
        "character_name": character_name,
        "display_name": str(primary_summary.get("display_name") or character_name),
        "relation_state": relation_state,
        "local_exists": local_exists,
        "cloud_exists": cloud_exists,
        "model_type": str(primary_summary.get("model_type") or ""),
        "asset_source": str(primary_summary.get("asset_source") or ""),
        "asset_source_id": str(primary_summary.get("asset_source_id") or ""),
        "local_asset_source": str((local_summary or {}).get("asset_source") or ""),
        "local_asset_source_id": str((local_summary or {}).get("asset_source_id") or ""),
        "cloud_asset_source": str((cloud_summary or {}).get("asset_source") or ""),
        "cloud_asset_source_id": str((cloud_summary or {}).get("asset_source_id") or ""),
        "local_origin_source": str((local_summary or {}).get("origin_source") or ""),
        "local_origin_source_id": str((local_summary or {}).get("origin_source_id") or ""),
        "local_origin_display_name": str((local_summary or {}).get("origin_display_name") or ""),
        "cloud_origin_source": str((cloud_summary or {}).get("origin_source") or ""),
        "cloud_origin_source_id": str((cloud_summary or {}).get("origin_source_id") or ""),
        "cloud_origin_display_name": str((cloud_summary or {}).get("origin_display_name") or ""),
        "local_asset_state": str((local_summary or {}).get("asset_state") or ""),
        "cloud_asset_state": str((cloud_summary or {}).get("asset_state") or ""),
        "local_updated_at_utc": str((local_summary or {}).get("updated_at_utc") or ""),
        "cloud_updated_at_utc": str((cloud_summary or {}).get("updated_at_utc") or ""),
        "local_fingerprint": local_fingerprint,
        "cloud_fingerprint": cloud_fingerprint,
        "available_actions": available_actions,
        "warnings": deduped_warnings,
    }


def _build_cloudsave_summary_state(
    config_manager,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if not is_cloudsave_disabled():
        bootstrap_local_cloudsave_environment(config_manager)

    characters_payload = config_manager.load_characters()
    local_character_map = characters_payload.get("猫娘") or {}
    current_character_name = str(characters_payload.get("当前猫娘") or "")
    characters_config_path = Path(config_manager.get_runtime_config_path("characters.json"))
    workshop_origin_index = _collect_workshop_character_origin_candidates(config_manager)
    local_summaries = {
        character_name: _build_local_character_snapshot(
            config_manager,
            character_name=character_name,
            character_payload=character_payload,
            characters_config_path=characters_config_path,
            workshop_origin_index=workshop_origin_index,
        )
        for character_name, character_payload in sorted(local_character_map.items())
        if isinstance(character_payload, dict)
    }

    provider_available = is_cloudsave_provider_available(config_manager)
    cloud_summaries: dict[str, dict[str, Any]] = {}
    if provider_available:
        manifest = load_cloudsave_manifest(config_manager)
        cloud_character_map, _tombstone_names = _load_cloudsave_character_payloads(config_manager)
        catalog_entries = _collect_cloudsave_catalog_entries(config_manager)
        binding_payloads = _collect_cloudsave_binding_payloads(config_manager)
        meta_payloads = _collect_cloudsave_meta_payloads(config_manager)
        cloud_summaries = {
            character_name: _build_cloud_character_snapshot(
                config_manager,
                character_name=character_name,
                character_payload=character_payload,
                binding_payloads=binding_payloads,
                meta_payloads=meta_payloads,
                manifest_exported_at=str(manifest.get("exported_at_utc") or ""),
                catalog_entries=catalog_entries,
                workshop_origin_index=workshop_origin_index,
            )
            for character_name, character_payload in sorted(cloud_character_map.items())
        }

    all_names = sorted(set(local_summaries) | set(cloud_summaries))
    items = [
        _merge_character_summary_item(
            character_name=character_name,
            local_summary=local_summaries.get(character_name),
            cloud_summary=cloud_summaries.get(character_name),
        )
        for character_name in all_names
    ]
    summary = {
        "success": True,
        "provider_available": provider_available,
        "current_character_name": current_character_name,
        "items": items,
    }
    return summary, local_summaries, cloud_summaries


def build_cloudsave_summary(config_manager) -> dict[str, Any]:
    summary, _local_summaries, _cloud_summaries = _build_cloudsave_summary_state(config_manager)
    return summary


def build_cloudsave_character_detail(config_manager, character_name: str) -> dict[str, Any] | None:
    summary, local_summaries, cloud_summaries = _build_cloudsave_summary_state(config_manager)
    for item in summary.get("items") or []:
        if item.get("character_name") == character_name:
            return {
                "success": True,
                "provider_available": bool(summary.get("provider_available", True)),
                "current_character_name": str(summary.get("current_character_name") or ""),
                "item": item,
                "local_summary": deepcopy(local_summaries.get(character_name)),
                "cloud_summary": deepcopy(cloud_summaries.get(character_name)),
            }
    return None
