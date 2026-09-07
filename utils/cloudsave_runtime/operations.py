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

"""Single-character export/import, full local snapshot export/import, and
operation backup/rollback plumbing.

Split out of the former monolithic ``utils/cloudsave_runtime.py``.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any

from utils.file_utils import atomic_write_json
from utils.character_memory import (
    evict_character_runtime_caches,
    retire_character_runtime_caches,
    revive_character_runtime_caches,
    list_character_recent_paths,
)
from utils.recent_file import (
    acquire_recent_file_locks,
    activate_recent_paths,
    clear_recent_deletions,
    clear_recent_redirects,
    fence_recent_deletions_and_clear_redirects,
    get_recent_pending_unlocked,
    read_recent_text_unlocked,
    recent_file_access,
    recent_file_locks,
    release_recent_file_locks,
    restore_recent_deletions,
    restore_recent_redirects,
    restore_recent_registry_state,
    set_recent_pending_unlocked,
    snapshot_recent_deletions,
)

# Late-bound package reference: tests monkeypatch attributes such as
# ``utils.cloudsave_runtime._atomic_copy_file`` and ``_apply_runtime_file``
# on the package facade, so those helpers must be resolved through the
# package at call time instead of being from-imported (early bound) here.
from utils import cloudsave_runtime as _facade

from ._shared import (
    CloudsaveOperationError,
    MANAGED_MEMORY_FILENAMES,
    ROOT_MODE_BOOTSTRAP_IMPORTING,
    _assert_deadline_not_exceeded,
    _raise_cloudsave_disabled,
    _raise_for_name_audit,
    _utc_now_iso,
    audit_cloudsave_character_names,
    is_cloudsave_disabled,
    scan_for_sensitive_values,
)
from .bindings import (
    _build_catalog_current_character_payload,
    _build_catalog_index_payload,
    _build_runtime_preferences_payload,
    _collect_workshop_character_origin_candidates,
    _derive_character_binding_summary,
    _extract_conversation_settings,
    _load_staged_json_file,
    _parse_binding_payloads,
    _parse_catalog_character_names,
)
from .bootstrap import (
    bootstrap_local_cloudsave_environment,
    ensure_cloudsave_manifest,
    load_cloudsave_manifest,
    save_cloudsave_manifest,
)
from .fence import cloud_apply_fence
from .snapshots import (
    _build_local_character_snapshot,
    _collect_cloudsave_binding_payloads,
    _load_cloudsave_character_payloads,
    _load_cloudsave_character_unit,
    _stage_single_character_cloudsave_entries,
    build_cloudsave_character_detail,
)
from .staging import (
    _build_manifest_fingerprint,
    _cleanup_empty_parent_dirs,
    _create_staging_workspace,
    _list_existing_cloudsave_files,
    _load_json_if_exists,
    _load_local_tombstones_state,
    _make_tombstones_catalog_payload,
    _normalize_tombstones_state,
    _save_local_tombstones_state,
    _sha256_file,
    _stage_file_copy,
    _stage_json_file,
    _stage_memory_file,
)


SNAPSHOT_KIND_CHARACTER_COLLECTION = "character_collection"
SNAPSHOT_KIND_FULL_RUNTIME = "full_runtime"
CHARACTER_COLLECTION_PROFILE_PATH = "profiles/character_collection.json"
LEGACY_RUNTIME_PROFILE_PATH = "profiles/characters.json"
CLOUDSAVE_READER_SCHEMA_VERSION = 2
_SUPPORTED_SNAPSHOT_KINDS = {
    SNAPSHOT_KIND_CHARACTER_COLLECTION,
    SNAPSHOT_KIND_FULL_RUNTIME,
}


def _manifest_schema_version(manifest: dict[str, Any]) -> int:
    try:
        return int(manifest.get("schema_version") or 1)
    except (TypeError, ValueError):
        return 1


def _resolve_snapshot_kind(manifest: dict[str, Any], staged_entries: dict[str, Path]) -> str:
    """Resolve explicit snapshot semantics and fail safe for legacy manifests.

    Before ``snapshot_kind`` existed, per-character exports did not stage the
    global payloads but could retain copies from an earlier full export.
    Treating every ambiguous legacy payload as a character collection is
    intentionally conservative: a merge can leave extra local data behind,
    while a mistaken full replacement can erase the owner profile and
    unrelated character memories.
    """
    full_runtime_markers = {
        "profiles/conversation_settings.json",
        "catalog/current_character.json",
    }
    schema_version = _manifest_schema_version(manifest)
    snapshot_kind = str(manifest.get("snapshot_kind") or "").strip()
    # Legacy writers preserve unknown top-level keys while rebuilding the
    # manifest, so a stale ``full_runtime`` kind can survive a character-only
    # upload. Those writers reset schema_version to 1; only schema 2+ binds the
    # kind to writer semantics we understand.
    if snapshot_kind and schema_version >= 2:
        if snapshot_kind not in _SUPPORTED_SNAPSHOT_KINDS:
            raise ValueError(f"unsupported cloudsave snapshot kind: {snapshot_kind}")
        if (
            snapshot_kind == SNAPSHOT_KIND_FULL_RUNTIME
            and not full_runtime_markers <= set(staged_entries)
        ):
            raise ValueError("full_runtime cloudsave snapshot is missing required global payloads")
        return snapshot_kind

    # Legacy full exports and single-character uploads can have identical file
    # shapes. A character upload rebuilt its manifest from files already on
    # disk, retaining both global markers from an earlier full export; across
    # devices, its new sequence can also collide with the retained marker's
    # sequence. There is therefore no reliable proof that a kind-less manifest
    # is a full snapshot. Always choose the non-destructive merge semantics.
    return SNAPSHOT_KIND_CHARACTER_COLLECTION


def _validate_manifest_reader_compatibility(manifest: dict[str, Any]) -> None:
    try:
        min_reader_schema_version = int(manifest.get("min_reader_schema_version") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid cloudsave minimum reader schema version") from exc
    if min_reader_schema_version > CLOUDSAVE_READER_SCHEMA_VERSION:
        raise ValueError(
            "cloudsave snapshot requires a newer reader schema: "
            f"{min_reader_schema_version}"
        )


def _has_usable_master_profile(payload: Any) -> bool:
    return bool(
        isinstance(payload, dict)
        and str(payload.get("档案名") or "").strip()
    )


def _runtime_characters_with_safe_master(config_manager) -> dict[str, Any]:
    runtime_payload = config_manager.load_characters()
    if not isinstance(runtime_payload, dict):
        runtime_payload = {}
    runtime_payload = deepcopy(runtime_payload)

    if not _has_usable_master_profile(runtime_payload.get("主人")):
        defaults = config_manager.get_default_characters()
        default_master = defaults.get("主人") if isinstance(defaults, dict) else None
        if not _has_usable_master_profile(default_master):
            raise ValueError("default characters payload does not contain a usable master profile")
        runtime_payload["主人"] = deepcopy(default_master)

    if not isinstance(runtime_payload.get("猫娘"), dict):
        runtime_payload["猫娘"] = {}
    return runtime_payload


def _assert_single_character_name_safe(character_name: str, *, context: str) -> None:
    audit_result = audit_cloudsave_character_names([character_name])
    try:
        _raise_for_name_audit(audit_result, context=context)
    except ValueError as exc:
        raise CloudsaveOperationError(
            "NAME_AUDIT_FAILED",
            str(exc),
            character_name=character_name,
        ) from exc


def _recent_pending_payload(messages: list[Any]) -> list[Any]:
    if not messages:
        return []
    if all(isinstance(message, dict) for message in messages):
        return deepcopy(messages)
    from utils.llm_client import messages_to_dict
    return messages_to_dict(messages)


def _stage_recent_memory_file(
    stage_root: Path,
    relative_path: str,
    source_path: Path,
) -> Path | None:
    """Stage one locked recent snapshot, including accepted pending turns."""
    with recent_file_access(source_path) as resolved_path:
        requested_path = os.path.normcase(os.path.abspath(os.fspath(source_path)))
        if resolved_path != requested_path:
            raise CloudsaveOperationError(
                "LOCAL_CHARACTER_CHANGED",
                f"character changed while staging recent history: {source_path}",
            )
        pending = get_recent_pending_unlocked(resolved_path)
        resolved_source = Path(resolved_path)
        if not resolved_source.is_file():
            if not pending:
                return None
            disk_payload: list[Any] = []
        elif not pending:
            return _stage_memory_file(stage_root, relative_path, resolved_source)
        else:
            disk_payload = json.loads(read_recent_text_unlocked(resolved_source))
            if not isinstance(disk_payload, list):
                raise ValueError(f"recent history is not a list: {resolved_source}")
        return _stage_json_file(
            stage_root,
            relative_path,
            disk_payload + _recent_pending_payload(pending),
        )


def export_cloudsave_character_unit(config_manager, character_name: str, *, overwrite: bool = False) -> dict[str, Any]:
    if is_cloudsave_disabled():
        _raise_cloudsave_disabled("single_character_upload", character_name=character_name)
    bootstrap_local_cloudsave_environment(config_manager)
    _assert_single_character_name_safe(character_name, context="single_character_upload")

    with cloud_apply_fence(
        config_manager,
        mode=ROOT_MODE_BOOTSTRAP_IMPORTING,
        reason=f"single_character_upload:{character_name}",
    ):
        characters_payload = config_manager.load_characters()
        character_payload = (characters_payload.get("猫娘") or {}).get(character_name)
        if not isinstance(character_payload, dict):
            raise CloudsaveOperationError(
                "LOCAL_CHARACTER_NOT_FOUND",
                f"local character not found: {character_name}",
                character_name=character_name,
            )

        existing_cloud_unit = _load_cloudsave_character_unit(config_manager, character_name)
        if existing_cloud_unit is not None and not overwrite:
            raise CloudsaveOperationError(
                "CLOUD_CHARACTER_EXISTS",
                f"cloud character already exists: {character_name}",
                character_name=character_name,
            )

        stage_root = _create_staging_workspace(config_manager, "single-export")
        cloud_state = config_manager.load_cloudsave_local_state()
        sequence_number = max(1, int(cloud_state.get("next_sequence_number") or 1))
        exported_at = _utc_now_iso()
        manifest = ensure_cloudsave_manifest(config_manager)
        workshop_origin_index = _collect_workshop_character_origin_candidates(config_manager)
        binding_payload = _derive_character_binding_summary(
            config_manager,
            character_name,
            character_payload,
            workshop_origin_index=workshop_origin_index,
        )
        local_summary = _build_local_character_snapshot(
            config_manager,
            character_name=character_name,
            character_payload=character_payload,
            characters_config_path=Path(config_manager.get_runtime_config_path("characters.json")),
            workshop_origin_index=workshop_origin_index,
        )

        staged_entries: dict[str, Path] = {}
        existing_cloud_character_map, _tombstone_names = _load_cloudsave_character_payloads(config_manager)
        merged_cloud_character_map = deepcopy(existing_cloud_character_map)
        merged_cloud_character_map[character_name] = deepcopy(character_payload)
        cloud_profiles_payload = {
            "猫娘": {
                name: deepcopy(payload)
                for name, payload in sorted(merged_cloud_character_map.items())
            },
        }
        staged_entries[CHARACTER_COLLECTION_PROFILE_PATH] = _stage_json_file(
            stage_root,
            CHARACTER_COLLECTION_PROFILE_PATH,
            cloud_profiles_payload,
        )

        staged_entries[f"bindings/{character_name}.json"] = _stage_json_file(
            stage_root,
            f"bindings/{character_name}.json",
            binding_payload,
        )

        character_memory_dir = Path(config_manager.memory_dir) / character_name
        staged_memory_relative_paths: set[str] = set()
        for filename in MANAGED_MEMORY_FILENAMES:
            source_path = character_memory_dir / filename
            relative_path = f"memory/{character_name}/{filename}"
            if filename == "recent.json":
                staged_path = _stage_recent_memory_file(stage_root, relative_path, source_path)
                if staged_path is None:
                    continue
                staged_entries[relative_path] = staged_path
            else:
                if not source_path.is_file():
                    continue
                staged_entries[relative_path] = _stage_memory_file(stage_root, relative_path, source_path)
            staged_memory_relative_paths.add(relative_path)

        single_character_entries, meta_payload = _stage_single_character_cloudsave_entries(
            config_manager,
            stage_root,
            character_name=character_name,
            character_payload=character_payload,
            binding_payload=binding_payload,
            sequence_number=sequence_number,
            exported_at=exported_at,
            client_id=str(cloud_state.get("client_id", "")),
            device_id=str(manifest.get("device_id", "")),
            memory_stage_overrides={
                filename: staged_entries[f"memory/{character_name}/{filename}"]
                for filename in MANAGED_MEMORY_FILENAMES
                if f"memory/{character_name}/{filename}" in staged_entries
            },
        )
        staged_entries.update(single_character_entries)

        merged_binding_payloads = _collect_cloudsave_binding_payloads(config_manager)
        merged_binding_payloads[character_name] = deepcopy(binding_payload)
        updated_catalog_payload = _build_catalog_index_payload(
            character_names=sorted(merged_cloud_character_map),
            characters_payload=cloud_profiles_payload,
            binding_payloads=merged_binding_payloads,
            sequence_number=sequence_number,
            exported_at=exported_at,
        )
        staged_entries["catalog/catgirls_index.json"] = _stage_json_file(
            stage_root,
            "catalog/catgirls_index.json",
            updated_catalog_payload,
        )

        updated_tombstones_payload = _remove_tombstone_from_catalog_payload(
            _load_json_if_exists(config_manager.cloudsave_catalog_dir / "character_tombstones.json"),
            character_name=character_name,
            sequence_number=sequence_number,
            exported_at=exported_at,
        )
        staged_entries["catalog/character_tombstones.json"] = _stage_json_file(
            stage_root,
            "catalog/character_tombstones.json",
            updated_tombstones_payload,
        )

        upload_tag = exported_at.replace(":", "").replace(".", "")
        backup_root = config_manager.cloudsave_backups_dir / f"character-upload-{upload_tag}" / character_name

        existing_cloud_memory_root = config_manager.cloudsave_memory_dir / character_name
        existing_cloud_character_root = config_manager.cloudsave_dir / "characters" / character_name
        delete_targets: set[Path] = {
            path
            for path in (
                config_manager.cloudsave_profiles_dir / "characters.json",
                config_manager.cloudsave_profiles_dir / "conversation_settings.json",
                config_manager.cloudsave_catalog_dir / "current_character.json",
            )
            if path.exists()
        }
        for base_dir in (existing_cloud_memory_root, existing_cloud_character_root / "memory"):
            if not base_dir.is_dir():
                continue
            for child in base_dir.iterdir():
                if not child.is_file():
                    continue
                if base_dir == existing_cloud_memory_root:
                    relative_path = f"memory/{character_name}/{child.name}"
                else:
                    relative_path = f"characters/{character_name}/memory/{child.name}"
                if relative_path not in staged_entries:
                    delete_targets.add(child)

        mutation_targets = {
            config_manager.cloudsave_profiles_dir / "characters.json",
            config_manager.cloudsave_profiles_dir / "character_collection.json",
            config_manager.cloudsave_bindings_dir / f"{character_name}.json",
            config_manager.cloudsave_catalog_dir / "catgirls_index.json",
            config_manager.cloudsave_catalog_dir / "character_tombstones.json",
            config_manager.cloudsave_profiles_dir / "conversation_settings.json",
            config_manager.cloudsave_catalog_dir / "current_character.json",
            config_manager.cloudsave_dir / "characters" / character_name,
            config_manager.cloudsave_memory_dir / character_name,
            config_manager.cloudsave_manifest_path,
            config_manager.cloudsave_local_state_path,
        }
        backup_records = _snapshot_existing_targets(
            config_manager,
            backup_root,
            mutation_targets | delete_targets,
        )

        try:
            for relative_path, staged_path in staged_entries.items():
                _facade._atomic_copy_file(staged_path, config_manager.cloudsave_dir / relative_path)

            for target_path in sorted(delete_targets):
                if target_path.exists():
                    target_path.unlink()
                    _cleanup_empty_parent_dirs(target_path, config_manager.cloudsave_dir)

            manifest = _rebuild_cloudsave_manifest_from_disk(
                config_manager,
                sequence_number=sequence_number,
                exported_at=exported_at,
                client_id=str(cloud_state.get("client_id", "")),
            )
            cloud_state["next_sequence_number"] = sequence_number + 1
            cloud_state["last_applied_manifest_fingerprint"] = str(manifest.get("fingerprint") or "")
            cloud_state["last_successful_export_at"] = exported_at
            config_manager.save_cloudsave_local_state(cloud_state)
        except Exception:
            _restore_backup_records(
                config_manager, backup_records, evict_sidecar_caches=False
            )
            raise

        detail = build_cloudsave_character_detail(config_manager, character_name)
        return {
            "character_name": character_name,
            "sequence_number": sequence_number,
            "meta": meta_payload,
            "manifest": manifest,
            "local_summary": local_summary,
            "detail": detail,
        }


def import_cloudsave_character_unit(
    config_manager,
    character_name: str,
    *,
    overwrite: bool = False,
    backup_before_overwrite: bool = True,
    retain_recent_locks: bool = False,
    use_cloud_apply_fence: bool = True,
) -> dict[str, Any]:
    if is_cloudsave_disabled():
        _raise_cloudsave_disabled("single_character_download", character_name=character_name)
    bootstrap_local_cloudsave_environment(config_manager)
    _assert_single_character_name_safe(character_name, context="single_character_download")

    fence_scope = (
        cloud_apply_fence(
            config_manager,
            mode=ROOT_MODE_BOOTSTRAP_IMPORTING,
            reason=f"single_character_download:{character_name}",
        )
        if use_cloud_apply_fence
        else nullcontext()
    )
    with fence_scope:
        cloud_unit = _load_cloudsave_character_unit(config_manager, character_name)
        if cloud_unit is None:
            raise CloudsaveOperationError(
                "CLOUD_CHARACTER_NOT_FOUND",
                f"cloud character not found: {character_name}",
                character_name=character_name,
            )

        runtime_characters = config_manager.load_characters()
        local_exists = character_name in (runtime_characters.get("猫娘") or {})
        if local_exists and not overwrite:
            raise CloudsaveOperationError(
                "LOCAL_CHARACTER_EXISTS",
                f"local character already exists: {character_name}",
                character_name=character_name,
            )

        stage_root = _create_staging_workspace(config_manager, "single-import")
        apply_time = _utc_now_iso()
        updated_characters = deepcopy(runtime_characters)
        updated_characters.setdefault("猫娘", {})
        updated_characters["猫娘"][character_name] = deepcopy(cloud_unit["profile"])
        current_character_name = str(updated_characters.get("当前猫娘") or "")
        if not current_character_name:
            updated_characters["当前猫娘"] = character_name
        characters_stage_path = _stage_json_file(
            stage_root,
            "__runtime__/profiles/characters.json",
            updated_characters,
        )

        updated_tombstones_state = _remove_tombstone_from_state_payload(
            config_manager.load_character_tombstones_state(),
            character_name=character_name,
        )
        tombstones_stage_path = _stage_json_file(
            stage_root,
            "__runtime__/state/character_tombstones.json",
            updated_tombstones_state,
        )

        cloud_state = config_manager.load_cloudsave_local_state()
        cloud_state["last_successful_import_at"] = apply_time
        cloud_state_stage_path = _stage_json_file(
            stage_root,
            "__runtime__/state/cloudsave_local_state.json",
            cloud_state,
        )

        runtime_targets: dict[Path, Path] = {
            Path(config_manager.get_runtime_config_path("characters.json")): characters_stage_path,
            config_manager.character_tombstones_state_path: tombstones_stage_path,
            config_manager.cloudsave_local_state_path: cloud_state_stage_path,
        }
        expected_memory_filenames: set[str] = set()
        for filename, source_path in (cloud_unit.get("memory_files") or {}).items():
            target_stage_path = _stage_file_copy(
                stage_root,
                f"__runtime__/memory/{character_name}/{filename}",
                source_path,
            )
            runtime_targets[Path(config_manager.memory_dir) / character_name / filename] = target_stage_path
            expected_memory_filenames.add(filename)

        delete_file_targets: set[Path] = set()
        target_memory_dir = Path(config_manager.memory_dir) / character_name
        for filename in MANAGED_MEMORY_FILENAMES:
            if filename in expected_memory_filenames:
                continue
            candidate = target_memory_dir / filename
            if candidate.exists():
                delete_file_targets.add(candidate)

        backup_root = config_manager.cloudsave_backups_dir / f"character-download-{apply_time.replace(':', '').replace('.', '')}" / character_name
        backup_targets = set(runtime_targets) | delete_file_targets
        if backup_before_overwrite or not local_exists:
            backup_targets.add(target_memory_dir)
        recent_target = target_memory_dir / "recent.json"
        recent_targets = list_character_recent_paths(config_manager, character_name)
        held_locks = acquire_recent_file_locks(recent_targets)
        recent_transaction = {
            "held_locks": held_locks,
            "recent_paths": recent_targets,
        }
        ownership_transferred = False
        try:
            (
                redirect_snapshot,
                activation_scope,
                deletion_snapshot,
                generation_snapshot,
            ) = (
                activate_recent_paths(recent_targets)
            )
            recent_state_paths = set(recent_targets) | set(activation_scope)
            pending_snapshot = {
                recent_path: get_recent_pending_unlocked(recent_path)
                for recent_path in recent_state_paths
            }
            backup_records: list[dict[str, Any]] = []
            try:
                # 锁覆盖 rollback snapshot、apply/delete 与内部 rollback，关闭
                # fence 前已启动 writer 的 copy/apply 窗口。
                backup_records = _snapshot_existing_targets(
                    config_manager, backup_root, backup_targets,
                )
                _write_operation_backup_metadata(
                    config_manager,
                    backup_root,
                    operation="character_download",
                    character_name=character_name,
                    backup_records=backup_records,
                    recent_pending=pending_snapshot,
                    recent_redirects=redirect_snapshot,
                    recent_deleted=deletion_snapshot,
                )

                for target_path, staged_path in runtime_targets.items():
                    _facade._apply_runtime_file(staged_path, target_path)

                for target_path in sorted(delete_file_targets):
                    if target_path.exists():
                        target_path.unlink()
                        _cleanup_empty_parent_dirs(target_path, Path(config_manager.memory_dir))
                detail = build_cloudsave_character_detail(config_manager, character_name)
            except BaseException:
                try:
                    _restore_backup_records(
                        config_manager, backup_records, evict_sidecar_caches=False
                    )
                finally:
                    for recent_path, messages in pending_snapshot.items():
                        set_recent_pending_unlocked(recent_path, messages)
                    restore_recent_registry_state(
                        list(activation_scope), redirect_snapshot, deletion_snapshot,
                        generation_snapshot,
                    )
                raise
            for recent_path in recent_state_paths:
                set_recent_pending_unlocked(recent_path, [])
            recent_transaction.update({
                "pending_snapshot": pending_snapshot,
                "redirect_snapshot": redirect_snapshot,
                "deletion_snapshot": deletion_snapshot,
                "activation_scope": activation_scope,
                "generation_snapshot": generation_snapshot,
            })
            if not retain_recent_locks:
                release_recent_file_locks(held_locks)
                recent_transaction["held_locks"] = []

            # The APPLY above rewrote only MANAGED_MEMORY_FILENAMES, and none of
            # the three sidecars (anti-repeat effects, anti-repeat corpus,
            # startup-greeting history) are in it -- verified by planting
            # sentinels and reading them back byte-identical. So their caches
            # still match disk and must NOT be evicted: eviction raises each
            # store's sequence fence, which silently discards a snapshot that
            # was staged and not yet flushed -- the reply just delivered.
            #
            # What a download does need is the retirement lifted, so a name
            # reused after an earlier delete can create its directory again.
            revive_character_runtime_caches(character_name)

            result = {
                "character_name": character_name,
                "applied_at_utc": apply_time,
                "detail": detail,
                "backup_path": str(backup_root),
            }
            if retain_recent_locks:
                result["_recent_import_transaction"] = recent_transaction
                ownership_transferred = True
            return result
        finally:
            if not ownership_transferred:
                retained_locks = recent_transaction.get("held_locks") or []
                if retained_locks:
                    recent_transaction["held_locks"] = []
                    release_recent_file_locks(retained_locks)


def finalize_cloudsave_character_import(result: dict[str, Any]) -> None:
    """Release recent locks retained across the caller's reload transaction."""
    transaction = result.pop("_recent_import_transaction", None) or {}
    held_locks = transaction.get("held_locks") or []
    if held_locks:
        transaction["held_locks"] = []
        release_recent_file_locks(held_locks)


def rollback_cloudsave_character_import_registry(result: dict[str, Any]) -> None:
    """Restore the pre-import recent identity after its disk backup is restored."""
    transaction = result.get("_recent_import_transaction") or {}
    restore_recent_registry_state(
        list(transaction.get("activation_scope") or ()),
        transaction.get("redirect_snapshot") or {},
        transaction.get("deletion_snapshot") or set(),
        transaction.get("generation_snapshot") or {},
    )


def _collect_memory_stage_entries(
    config_manager,
    stage_root: Path,
    character_names: list[str],
    *,
    deadline_monotonic: float | None = None,
    operation: str = "export",
) -> dict[str, Path]:
    staged_entries: dict[str, Path] = {}
    for character_name in sorted(character_names):
        character_dir = Path(config_manager.memory_dir) / character_name
        for filename in MANAGED_MEMORY_FILENAMES:
            _assert_deadline_not_exceeded(
                deadline_monotonic,
                operation=operation,
                stage=f"stage_memory:{character_name}:{filename}",
            )
            source_path = character_dir / filename
            relative_path = f"memory/{character_name}/{filename}"
            if filename == "recent.json":
                staged_path = _stage_recent_memory_file(stage_root, relative_path, source_path)
                if staged_path is not None:
                    staged_entries[relative_path] = staged_path
            elif source_path.is_file():
                staged_entries[relative_path] = _stage_memory_file(
                    stage_root, relative_path, source_path,
                )
    return staged_entries


def _managed_target_relative_path(config_manager, target_path: Path) -> Path:
    normalized_target = Path(target_path).expanduser().resolve(strict=False)
    runtime_root = Path(config_manager.app_docs_dir).expanduser().resolve(strict=False)
    anchor_root = Path(getattr(config_manager, "anchor_root", config_manager.app_docs_dir)).expanduser().resolve(strict=False)
    project_memory_root = Path(config_manager.project_memory_dir).expanduser().resolve(strict=False)

    candidate_roots = []
    seen_roots: set[Path] = set()
    for scope, root in (
        ("runtime", runtime_root),
        ("anchor", anchor_root),
        ("project_memory", project_memory_root),
    ):
        if root not in seen_roots:
            candidate_roots.append((scope, root))
            seen_roots.add(root)
    candidate_roots.sort(key=lambda item: len(item[1].parts), reverse=True)

    for scope, root in candidate_roots:
        try:
            relative_path = normalized_target.relative_to(root)
        except ValueError:
            continue
        return Path(scope) / relative_path

    raise ValueError(f"unmanaged cloudsave backup target: {target_path}")


def _resolve_managed_target_path(config_manager, relative_path: str) -> Path:
    normalized_relative_path = str(relative_path or "").strip().replace("\\", "/")
    if not normalized_relative_path:
        raise ValueError("managed backup relative path is empty")

    parts = Path(normalized_relative_path)
    if not parts.parts or parts.is_absolute() or ".." in parts.parts:
        raise ValueError("managed backup relative path is invalid")

    scope = parts.parts[0]
    suffix = Path(*parts.parts[1:]) if len(parts.parts) > 1 else Path()
    if scope == "anchor":
        root = Path(getattr(config_manager, "anchor_root", config_manager.app_docs_dir))
    elif scope == "project_memory":
        root = Path(config_manager.project_memory_dir)
    elif scope == "runtime":
        root = Path(config_manager.app_docs_dir)
    else:
        # Backward compatibility for backups created before dual-root metadata was introduced.
        root = Path(config_manager.app_docs_dir)
        suffix = Path(normalized_relative_path)

    resolved_root = root.expanduser().resolve(strict=False)
    candidate = (root / suffix).expanduser().resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("managed backup relative path escapes storage root") from exc
    return candidate


def _build_backup_path(config_manager, backup_root: Path, target_path: Path) -> Path:
    return backup_root / _managed_target_relative_path(config_manager, target_path)


def _snapshot_existing_targets(config_manager, backup_root: Path, targets: set[Path]) -> list[dict[str, Any]]:
    backup_records: list[dict[str, Any]] = []
    for target_path in sorted(targets, key=lambda path: (len(path.parts), str(path))):
        relative_path = _managed_target_relative_path(config_manager, target_path)
        record = {
            "target": target_path,
            "backup": None,
            "is_dir": target_path.is_dir(),
            "relative_path": str(relative_path).replace("\\", "/"),
        }
        if target_path.exists():
            backup_path = _build_backup_path(config_manager, backup_root, target_path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.is_dir():
                shutil.copytree(target_path, backup_path, dirs_exist_ok=True)
            else:
                shutil.copy2(target_path, backup_path)
            record["backup"] = backup_path
        backup_records.append(record)
    return backup_records


def _memory_character_names_from_backup_records(config_manager, backup_records):
    """Character names whose memory directory a restore is about to replace.

    Only a DELIBERATE restore asks for this -- see the flag on
    ``_restore_backup_records``. It rmtree+copytree's whole ``memory/<name>/``
    directories, so it puts the three sidecars back to whatever the backup
    holds, and a cache left loaded would write the rolled-back content
    straight back out.
    """
    # Resolved on BOTH sides. restore_cloudsave_operation_backup builds its
    # targets through _resolve_managed_target_path, which resolves, so a
    # memory_dir carrying a symlink, a "~", or a ".." never matched the raw
    # parent -- the name list came back empty, nothing was evicted, and the
    # stale caches wrote over the files the rollback had just restored.
    memory_root = Path(config_manager.memory_dir).expanduser().resolve(strict=False)
    restored: list[str] = []
    removed: list[str] = []
    for record in backup_records:
        raw_target = str(record.get("target") or "")
        # Guarded before resolving: Path("") is ".", which resolves to the
        # working directory and would contribute its name.
        if not raw_target or not record.get("is_dir"):
            continue
        target = Path(raw_target).expanduser().resolve(strict=False)
        if target.parent != memory_root or not target.name:
            continue
        # Nothing to put back means the restore DELETES this directory --
        # the operation being rolled back is what created it. Evicting
        # there would leave the name live, and a write still in flight
        # would recreate the directory for a character the restored
        # characters.json no longer contains. Retirement is what refuses
        # that: a retired name never creates a directory.
        if record.get("backup") is None:
            removed.append(target.name)
        else:
            restored.append(target.name)
    return tuple(dict.fromkeys(restored)), tuple(dict.fromkeys(removed))


def _restore_backup_records(
    config_manager,
    backup_records: list[dict[str, Any]],
    *,
    evict_sidecar_caches: bool,
) -> None:
    """Put backed-up targets back, evicting sidecar caches only on request.

    This rmtree+copytree's whole ``memory/<name>/`` directories, so unlike
    the apply -- which writes only MANAGED_MEMORY_FILENAMES -- it does put
    the three sidecars back to whatever the backup holds. Whether that
    should drop their caches depends on WHY the restore is running, which
    is why the flag is required rather than defaulted.

    Rolling back a FAILED export or import: no. The apply never touched
    the sidecars, so the only difference the restore can make to them is to
    revert a flush that landed while the operation was in flight. The cache
    is then strictly fresher than the file written over it, and evicting
    adopts the older state -- the sequence fence advances, the pending
    flush early-returns on ``seq <= _written_seq``, and the reply just
    delivered is lost. Leaving the cache alone lets the next flush put it
    back.

    Restoring an operation backup on purpose: yes. There the older state
    is exactly what was asked for, and a cache left loaded would write the
    rolled-back content straight back out.
    """
    # In a finally, because the damage is already done by the time anything
    # in the loop can fail. Records are processed deepest-first, so a
    # character directory is removed EARLY and the runtime/state files come
    # after it; one of those raising left the removal in place and skipped
    # the retirement entirely. The name then stayed live with no directory,
    # and the next in-flight write recreated it as an orphan --
    # ``character_memory_exists`` reporting a character the restored
    # characters.json no longer contains. That is precisely what the
    # retirement below exists to prevent, so it must not be the thing a
    # failure skips.
    #
    # Reaching it on the error path is also the safer arm on its own terms:
    # the disk has changed either way, so a cache left loaded is stale
    # whether the loop finished or not.
    # Only the records this call actually REACHED. The loop stops at the
    # first raise, and a character whose directory was never touched has a
    # cache that is still correct for what is on disk -- evicting it adopts
    # an older state, advances the sequence fence past the pending flush,
    # and loses a reply that was already delivered. Measured: 2 decisions
    # expected, 1 on disk, with _written_seq bumped by the eviction alone.
    #
    # The raising record itself counts as reached: its removal may already
    # have happened, which is the whole reason this block moved into a
    # finally.
    processed: list[dict] = []
    try:
        for record in sorted(
            backup_records,
            key=lambda item: len(item["target"].parts),
            reverse=True,
        ):
            processed.append(record)
            target_path = record["target"]
            if target_path.exists():
                # Refreshed from disk: the recorded flag is from BACKUP
                # time, so a directory this operation created carries False
                # and its character would never reach the lifecycle
                # handling below.
                record["is_dir"] = target_path.is_dir()
                if target_path.is_dir():
                    shutil.rmtree(target_path, ignore_errors=True)
                else:
                    target_path.unlink()
            backup_path = record.get("backup")
            if backup_path is None or not backup_path.exists():
                continue
            if record.get("is_dir"):
                shutil.copytree(backup_path, target_path, dirs_exist_ok=True)
            else:
                _facade._apply_runtime_file(backup_path, target_path)
    finally:
        if evict_sidecar_caches:
            restored, removed = _memory_character_names_from_backup_records(
                config_manager, processed
            )
            evict_character_runtime_caches(*restored)
            retire_character_runtime_caches(*removed)


def _write_operation_backup_metadata(
    config_manager,
    backup_root: Path,
    *,
    operation: str,
    character_name: str,
    backup_records: list[dict[str, Any]],
    recent_pending: dict[Path, list[Any]] | None = None,
    recent_redirects: dict[str, str] | None = None,
    recent_deleted: set[str] | None = None,
) -> Path:
    payload = {
        "schema_version": 2 if recent_pending is not None else 1,
        "operation": operation,
        "character_name": character_name,
        "targets": [
            {
                "relative_path": str(record.get("relative_path") or ""),
                "had_backup": record.get("backup") is not None,
                "is_dir": bool(record.get("is_dir", False)),
            }
            for record in backup_records
        ],
    }
    if recent_pending is not None:
        payload["recent_state"] = {
            "pending": [
                {
                    "relative_path": str(
                        _managed_target_relative_path(config_manager, Path(path))
                    ).replace("\\", "/"),
                    "messages": _recent_pending_payload(messages),
                }
                for path, messages in recent_pending.items()
            ],
            "redirects": [
                {
                    "source_relative_path": str(
                        _managed_target_relative_path(config_manager, Path(source))
                    ).replace("\\", "/"),
                    "target_relative_path": str(
                        _managed_target_relative_path(config_manager, Path(target))
                    ).replace("\\", "/"),
                }
                for source, target in (recent_redirects or {}).items()
            ],
            "deleted": [
                str(
                    _managed_target_relative_path(config_manager, Path(path))
                ).replace("\\", "/")
                for path in (recent_deleted or set())
            ],
        }
    metadata_path = backup_root / "_operation.json"
    atomic_write_json(metadata_path, payload, ensure_ascii=False, indent=2)
    return metadata_path


def _recent_paths_from_backup_records(config_manager, backup_records) -> set[Path]:
    memory_root = Path(config_manager.memory_dir).resolve(strict=False)
    recent_paths: set[Path] = set()
    for record in backup_records:
        target_path = Path(record["target"])
        if target_path.name == "recent.json":
            recent_paths.add(target_path)
        if not record.get("is_dir"):
            continue
        try:
            target_path.resolve(strict=False).relative_to(memory_root)
        except ValueError:
            continue
        recent_paths.add(target_path / "recent.json")
    return recent_paths


def restore_cloudsave_operation_backup(
    config_manager,
    backup_root: str | Path,
    *,
    recent_locks_held: bool = False,
) -> None:
    backup_root_path = Path(backup_root)
    metadata = _load_json_if_exists(backup_root_path / "_operation.json")
    if not isinstance(metadata, dict):
        raise FileNotFoundError(f"cloudsave backup metadata missing: {backup_root_path}")

    backup_records: list[dict[str, Any]] = []
    for target in metadata.get("targets") or []:
        if not isinstance(target, dict):
            continue
        relative_path = str(target.get("relative_path") or "").strip().replace("\\", "/")
        if not relative_path:
            continue
        runtime_target = _resolve_managed_target_path(config_manager, relative_path)
        backup_path = backup_root_path / relative_path
        backup_records.append(
            {
                "target": runtime_target,
                "backup": backup_path if bool(target.get("had_backup")) and backup_path.exists() else None,
                "is_dir": bool(target.get("is_dir", False)),
            }
        )
    backup_recent_paths = _recent_paths_from_backup_records(config_manager, backup_records)
    recent_state = metadata.get("recent_state")
    if not isinstance(recent_state, dict):
        lock_scope = nullcontext() if recent_locks_held else recent_file_locks(
            list(backup_recent_paths)
        )
        with lock_scope:
            current_redirects = clear_recent_redirects(list(backup_recent_paths))
            current_pending = {
                path: get_recent_pending_unlocked(path)
                for path in backup_recent_paths
            }
            current_deleted = snapshot_recent_deletions(list(backup_recent_paths))
            try:
                _restore_backup_records(
                    config_manager, backup_records, evict_sidecar_caches=True
                )
                for path in backup_recent_paths:
                    set_recent_pending_unlocked(path, [])
                if recent_locks_held:
                    clear_recent_deletions(list(backup_recent_paths))
                else:
                    generation_scope = set(backup_recent_paths)
                    generation_scope.update(Path(path) for path in current_redirects)
                    generation_scope.update(Path(path) for path in current_redirects.values())
                    restore_recent_registry_state(
                        list(generation_scope), {}, set(),
                    )
            except Exception:
                for path, messages in current_pending.items():
                    set_recent_pending_unlocked(path, messages)
                restore_recent_redirects(current_redirects)
                restore_recent_deletions(
                    list(backup_recent_paths), current_deleted,
                )
                raise
        return

    from utils.llm_client import messages_from_dict

    pending_snapshot: dict[Path, list[Any]] = {}
    for entry in recent_state.get("pending") or []:
        if not isinstance(entry, dict):
            continue
        relative_path = str(entry.get("relative_path") or "")
        messages = entry.get("messages")
        if not relative_path or not isinstance(messages, list):
            continue
        pending_snapshot[_resolve_managed_target_path(config_manager, relative_path)] = (
            messages_from_dict(messages)
        )

    redirect_snapshot: dict[str, str] = {}
    for entry in recent_state.get("redirects") or []:
        if not isinstance(entry, dict):
            continue
        source_relative_path = str(entry.get("source_relative_path") or "")
        target_relative_path = str(entry.get("target_relative_path") or "")
        if not source_relative_path or not target_relative_path:
            continue
        source_path = _resolve_managed_target_path(config_manager, source_relative_path)
        target_path = _resolve_managed_target_path(config_manager, target_relative_path)
        redirect_snapshot[str(source_path)] = str(target_path)

    deleted_snapshot = {
        str(_resolve_managed_target_path(config_manager, str(relative_path)))
        for relative_path in recent_state.get("deleted") or []
        if str(relative_path or "")
    }

    recent_paths = backup_recent_paths | set(pending_snapshot)
    redirect_paths = set(recent_paths)
    redirect_paths.update(Path(path) for path in redirect_snapshot)
    redirect_paths.update(Path(path) for path in redirect_snapshot.values())
    lock_scope = nullcontext() if recent_locks_held else recent_file_locks(list(redirect_paths))
    with lock_scope:
        current_redirects = clear_recent_redirects(list(redirect_paths))
        current_pending = {
            path: get_recent_pending_unlocked(path)
            for path in recent_paths
        }
        current_deleted = snapshot_recent_deletions(list(recent_paths))
        try:
            _restore_backup_records(
                config_manager, backup_records, evict_sidecar_caches=True
            )
            for path in recent_paths:
                set_recent_pending_unlocked(path, pending_snapshot.get(path, []))
            if recent_locks_held:
                restore_recent_deletions(list(recent_paths), deleted_snapshot)
                restore_recent_redirects(redirect_snapshot)
            else:
                generation_scope = set(redirect_paths)
                generation_scope.update(Path(path) for path in current_redirects)
                generation_scope.update(Path(path) for path in current_redirects.values())
                restore_recent_registry_state(
                    list(generation_scope), redirect_snapshot, deleted_snapshot,
                )
        except Exception:
            for path, messages in current_pending.items():
                set_recent_pending_unlocked(path, messages)
            restore_recent_deletions(list(recent_paths), current_deleted)
            restore_recent_redirects(current_redirects)
            raise


def _rebuild_cloudsave_manifest_from_disk(
    config_manager,
    *,
    sequence_number: int,
    exported_at: str,
    client_id: str,
) -> dict[str, Any]:
    manifest = ensure_cloudsave_manifest(config_manager)
    files = {
        relative_path: {
            "sha256": _sha256_file(config_manager.cloudsave_dir / relative_path),
            "size": (config_manager.cloudsave_dir / relative_path).stat().st_size,
        }
        for relative_path in sorted(_list_existing_cloudsave_files(config_manager))
    }
    manifest.update(
        {
            "schema_version": 2,
            "min_reader_schema_version": 2,
            "min_app_version": "",
            "client_id": str(client_id or manifest.get("client_id", "")),
            "device_id": str(manifest.get("device_id", "")),
            "sequence_number": int(sequence_number),
            "exported_at_utc": exported_at,
            "snapshot_kind": SNAPSHOT_KIND_CHARACTER_COLLECTION,
            "files": files,
        }
    )
    manifest["fingerprint"] = _build_manifest_fingerprint(
        client_id=str(manifest.get("client_id", "")),
        sequence_number=int(manifest.get("sequence_number") or 0),
        files=files,
        snapshot_kind=SNAPSHOT_KIND_CHARACTER_COLLECTION,
    )
    save_cloudsave_manifest(config_manager, manifest)
    return manifest


def _default_catalog_index_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sequence_number": 0,
        "exported_at_utc": "",
        "characters": [],
    }


def _default_tombstones_catalog_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sequence_number": 0,
        "exported_at_utc": "",
        "tombstones": [],
    }


def _upsert_catalog_character_entry(
    catalog_payload: Any,
    *,
    character_entry: dict[str, Any],
    sequence_number: int,
    exported_at: str,
) -> dict[str, Any]:
    payload = deepcopy(catalog_payload) if isinstance(catalog_payload, dict) else _default_catalog_index_payload()
    entries_by_name: dict[str, dict[str, Any]] = {}
    for entry in payload.get("characters") or []:
        if not isinstance(entry, dict):
            continue
        existing_name = str(entry.get("character_name") or "").strip()
        if existing_name:
            entries_by_name[existing_name] = deepcopy(entry)
    entry_name = str(character_entry.get("character_name") or "").strip()
    if entry_name:
        entries_by_name[entry_name] = deepcopy(character_entry)
    payload["schema_version"] = 1
    payload["sequence_number"] = int(sequence_number)
    payload["exported_at_utc"] = exported_at
    payload["characters"] = [entries_by_name[name] for name in sorted(entries_by_name)]
    return payload


def _remove_tombstone_from_catalog_payload(
    tombstones_payload: Any,
    *,
    character_name: str,
    sequence_number: int,
    exported_at: str,
) -> dict[str, Any]:
    payload = deepcopy(tombstones_payload) if isinstance(tombstones_payload, dict) else _default_tombstones_catalog_payload()
    tombstones_state = _normalize_tombstones_state(payload)
    filtered_tombstones = [
        entry
        for entry in tombstones_state.get("tombstones") or []
        if str(entry.get("character_name") or "") != character_name
    ]
    return {
        "schema_version": 1,
        "sequence_number": int(sequence_number),
        "exported_at_utc": exported_at,
        "tombstones": filtered_tombstones,
    }


def _remove_tombstone_from_state_payload(
    tombstones_payload: Any,
    *,
    character_name: str,
) -> dict[str, Any]:
    tombstones_state = _normalize_tombstones_state(tombstones_payload)
    return {
        "version": 1,
        "tombstones": [
            entry
            for entry in tombstones_state.get("tombstones") or []
            if str(entry.get("character_name") or "") != character_name
        ],
    }


def export_local_cloudsave_snapshot(
    config_manager,
    *,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Export the current local runtime truth into cloudsave/ with manifest-last semantics."""
    if is_cloudsave_disabled():
        _raise_cloudsave_disabled("local_cloudsave_export")
    bootstrap_local_cloudsave_environment(config_manager)

    with cloud_apply_fence(
        config_manager,
        mode=ROOT_MODE_BOOTSTRAP_IMPORTING,
        reason="local_cloudsave_export",
    ):
        _assert_deadline_not_exceeded(
            deadline_monotonic,
            operation="export",
            stage="prepare_export",
        )
        stage_root = _create_staging_workspace(config_manager, "export")
        cloud_state = config_manager.load_cloudsave_local_state()
        sequence_number = max(1, int(cloud_state.get("next_sequence_number") or 1))
        exported_at = _utc_now_iso()

        characters_payload = config_manager.load_characters()
        conversation_settings = _extract_conversation_settings(config_manager)
        tombstones_state = _load_local_tombstones_state(config_manager)
        tombstones = tombstones_state.get("tombstones") or []
        live_character_names = sorted((characters_payload.get("猫娘") or {}).keys())
        live_name_set = set(live_character_names)
        filtered_tombstones = [
            tombstone
            for tombstone in tombstones
            if tombstone.get("character_name") not in live_name_set
        ]
        if filtered_tombstones != tombstones:
            tombstones_state["tombstones"] = filtered_tombstones
            tombstones_state = _save_local_tombstones_state(config_manager, tombstones_state)
            tombstones = tombstones_state.get("tombstones") or []
        tombstone_names = [tombstone["character_name"] for tombstone in tombstones]
        name_audit = audit_cloudsave_character_names(live_character_names, tombstone_names)
        _raise_for_name_audit(name_audit, context="export")
        character_names = live_character_names
        current_character_name = str(characters_payload.get("当前猫娘") or "")
        workshop_origin_index = _collect_workshop_character_origin_candidates(config_manager)
        binding_payloads = {
            name: _derive_character_binding_summary(
                config_manager,
                name,
                (characters_payload.get("猫娘") or {}).get(name, {}),
                workshop_origin_index=workshop_origin_index,
            )
            for name in character_names
        }

        sensitive_findings = scan_for_sensitive_values(characters_payload, path="profiles.characters")
        if sensitive_findings:
            raise ValueError(f"sensitive values detected in export payload: {', '.join(sensitive_findings)}")

        staged_entries: dict[str, Path] = {
            "profiles/characters.json": _stage_json_file(stage_root, "profiles/characters.json", characters_payload),
            "profiles/conversation_settings.json": _stage_json_file(
                stage_root,
                "profiles/conversation_settings.json",
                conversation_settings,
            ),
            "catalog/catgirls_index.json": _stage_json_file(
                stage_root,
                "catalog/catgirls_index.json",
                _build_catalog_index_payload(
                    character_names=character_names,
                    characters_payload=characters_payload,
                    binding_payloads=binding_payloads,
                    sequence_number=sequence_number,
                    exported_at=exported_at,
                ),
            ),
            "catalog/current_character.json": _stage_json_file(
                stage_root,
                "catalog/current_character.json",
                _build_catalog_current_character_payload(
                    current_character_name=current_character_name,
                    exported_at=exported_at,
                    sequence_number=sequence_number,
                ),
            ),
            "catalog/character_tombstones.json": _stage_json_file(
                stage_root,
                "catalog/character_tombstones.json",
                _make_tombstones_catalog_payload(
                    tombstones=tombstones,
                    sequence_number=sequence_number,
                    exported_at=exported_at,
                ),
            ),
        }
        memory_stage_entries = _collect_memory_stage_entries(
            config_manager,
            stage_root,
            character_names,
            deadline_monotonic=deadline_monotonic,
            operation="export",
        )
        staged_entries.update(memory_stage_entries)
        manifest = ensure_cloudsave_manifest(config_manager)
        manifest_device_id = str(manifest.get("device_id", ""))
        for name, binding_payload in binding_payloads.items():
            _assert_deadline_not_exceeded(
                deadline_monotonic,
                operation="export",
                stage=f"stage_character:{name}",
            )
            staged_entries[f"bindings/{name}.json"] = _stage_json_file(
                stage_root,
                f"bindings/{name}.json",
                binding_payload,
            )
            single_character_entries, _meta_payload = _stage_single_character_cloudsave_entries(
                config_manager,
                stage_root,
                character_name=name,
                character_payload=(characters_payload.get("猫娘") or {}).get(name, {}),
                binding_payload=binding_payload,
                sequence_number=sequence_number,
                exported_at=exported_at,
                client_id=str(cloud_state.get("client_id", "")),
                device_id=manifest_device_id,
                memory_stage_overrides={
                    filename: memory_stage_entries[f"memory/{name}/{filename}"]
                    for filename in MANAGED_MEMORY_FILENAMES
                    if f"memory/{name}/{filename}" in memory_stage_entries
                },
            )
            staged_entries.update(single_character_entries)

        _assert_deadline_not_exceeded(
            deadline_monotonic,
            operation="export",
            stage="finalize_manifest",
        )
        files = {
            relative_path: {
                "sha256": _sha256_file(staged_path),
                "size": staged_path.stat().st_size,
            }
            for relative_path, staged_path in sorted(staged_entries.items())
        }

        manifest.update(
            {
                "schema_version": 2,
                "min_reader_schema_version": 2,
                "min_app_version": "",
                "client_id": str(cloud_state.get("client_id", "")),
                "device_id": str(manifest.get("device_id", "")),
                "sequence_number": sequence_number,
                "exported_at_utc": exported_at,
                "snapshot_kind": SNAPSHOT_KIND_FULL_RUNTIME,
                "files": files,
            }
        )
        manifest["fingerprint"] = _build_manifest_fingerprint(
            client_id=manifest["client_id"],
            sequence_number=sequence_number,
            files=files,
            snapshot_kind=SNAPSHOT_KIND_FULL_RUNTIME,
        )

        _assert_deadline_not_exceeded(
            deadline_monotonic,
            operation="export",
            stage="apply_snapshot",
        )
        for relative_path, staged_path in staged_entries.items():
            _facade._atomic_copy_file(staged_path, config_manager.cloudsave_dir / relative_path)

        stale_files = _list_existing_cloudsave_files(config_manager) - set(staged_entries)
        for relative_path in sorted(stale_files):
            target_path = config_manager.cloudsave_dir / relative_path
            if target_path.exists():
                target_path.unlink()
                _cleanup_empty_parent_dirs(target_path, config_manager.cloudsave_dir)

        save_cloudsave_manifest(config_manager, manifest)

        cloud_state["next_sequence_number"] = sequence_number + 1
        cloud_state["last_applied_manifest_fingerprint"] = manifest["fingerprint"]
        cloud_state["last_successful_export_at"] = exported_at
        config_manager.save_cloudsave_local_state(cloud_state)

        return {
            "manifest": manifest,
            "staged_file_count": len(staged_entries),
            "name_audit": name_audit,
        }


def import_local_cloudsave_snapshot(
    config_manager,
    *,
    deadline_monotonic: float | None = None,
    use_cloud_apply_fence: bool = True,
) -> dict[str, Any]:
    """Import the current local cloudsave snapshot back into runtime truth with rollback."""
    if is_cloudsave_disabled():
        _raise_cloudsave_disabled("local_cloudsave_import")
    bootstrap_local_cloudsave_environment(config_manager)
    fence_scope = (
        cloud_apply_fence(
            config_manager,
            mode=ROOT_MODE_BOOTSTRAP_IMPORTING,
            reason="local_cloudsave_import",
        )
        if use_cloud_apply_fence
        else nullcontext()
    )
    with fence_scope:
        _assert_deadline_not_exceeded(
            deadline_monotonic,
            operation="import",
            stage="prepare_import",
        )
        manifest = load_cloudsave_manifest(config_manager)
        _validate_manifest_reader_compatibility(manifest)
        manifest_files = manifest.get("files") or {}
        if not isinstance(manifest_files, dict) or not manifest_files:
            raise ValueError("cloudsave manifest does not contain any staged files")

        stage_root = _create_staging_workspace(config_manager, "import")
        staged_entries: dict[str, Path] = {}
        for relative_path in sorted(manifest_files):
            _assert_deadline_not_exceeded(
                deadline_monotonic,
                operation="import",
                stage=f"stage_file:{relative_path}",
            )
            source_path = config_manager.cloudsave_dir / relative_path
            if not source_path.is_file():
                raise FileNotFoundError(f"cloudsave file missing from manifest: {relative_path}")
            staged_entries[relative_path] = _stage_file_copy(stage_root, relative_path, source_path)

        computed_files = {
            relative_path: {
                "sha256": _sha256_file(staged_path),
                "size": staged_path.stat().st_size,
            }
            for relative_path, staged_path in sorted(staged_entries.items())
        }
        schema_version = _manifest_schema_version(manifest)
        fingerprint_snapshot_kind = (
            str(manifest.get("snapshot_kind") or "").strip()
            if schema_version >= 2
            else ""
        )
        manifest_fingerprint = str(manifest.get("fingerprint") or "")
        if schema_version >= 2 and fingerprint_snapshot_kind and not manifest_fingerprint:
            raise ValueError("schema 2 cloudsave manifest fingerprint is required")
        computed_fingerprint = _build_manifest_fingerprint(
            client_id=str(manifest.get("client_id", "")),
            sequence_number=int(manifest.get("sequence_number") or 0),
            files=computed_files,
            snapshot_kind=fingerprint_snapshot_kind,
        )
        if manifest_fingerprint and manifest_fingerprint != computed_fingerprint:
            raise ValueError("cloudsave manifest fingerprint mismatch")

        snapshot_kind = _resolve_snapshot_kind(manifest, staged_entries)
        profile_path = (
            CHARACTER_COLLECTION_PROFILE_PATH
            if snapshot_kind == SNAPSHOT_KIND_CHARACTER_COLLECTION
            and CHARACTER_COLLECTION_PROFILE_PATH in staged_entries
            else LEGACY_RUNTIME_PROFILE_PATH
        )
        cloud_characters_payload = _load_staged_json_file(
            staged_entries, profile_path, required=True,
        )
        if not isinstance(cloud_characters_payload, dict):
            raise ValueError(f"{profile_path} must contain a JSON object")

        conversation_settings = _load_staged_json_file(staged_entries, "profiles/conversation_settings.json") or {}
        if not isinstance(conversation_settings, dict):
            raise ValueError("profiles/conversation_settings.json must contain a JSON object")

        binding_payloads = _parse_binding_payloads(staged_entries)
        catalog_index_payload = _load_staged_json_file(staged_entries, "catalog/catgirls_index.json")
        current_character_catalog_payload = _load_staged_json_file(staged_entries, "catalog/current_character.json")
        tombstones_catalog_payload = _load_staged_json_file(staged_entries, "catalog/character_tombstones.json") or {}
        tombstones_state = _normalize_tombstones_state(tombstones_catalog_payload)
        tombstones = tombstones_state.get("tombstones") or []
        tombstone_names = [tombstone["character_name"] for tombstone in tombstones]

        sensitive_findings = scan_for_sensitive_values(cloud_characters_payload, path="profiles.characters")
        if sensitive_findings:
            raise ValueError(f"sensitive values detected in import payload: {', '.join(sensitive_findings)}")

        snapshot_character_map = deepcopy(cloud_characters_payload.get("猫娘") or {})
        if not isinstance(snapshot_character_map, dict):
            raise ValueError(f"{profile_path} 猫娘 must contain a JSON object")
        live_character_names = sorted(snapshot_character_map.keys())
        name_audit = audit_cloudsave_character_names(live_character_names, tombstone_names)
        _raise_for_name_audit(name_audit, context="import")

        catalog_character_names = _parse_catalog_character_names(catalog_index_payload)
        if catalog_character_names and catalog_character_names != set(live_character_names):
            raise ValueError(f"catalog/catgirls_index.json is inconsistent with {profile_path}")
        if binding_payloads and set(binding_payloads) != set(live_character_names):
            raise ValueError(f"bindings/ payloads are inconsistent with {profile_path}")

        for tombstone_name in tombstone_names:
            snapshot_character_map.pop(tombstone_name, None)

        requested_current_name = str(cloud_characters_payload.get("当前猫娘") or "").strip()
        if isinstance(current_character_catalog_payload, dict):
            catalog_current_name = str(current_character_catalog_payload.get("current_character_name") or "").strip()
            if catalog_current_name:
                requested_current_name = catalog_current_name

        applied_character_names = sorted(snapshot_character_map.keys())
        if snapshot_kind == SNAPSHOT_KIND_CHARACTER_COLLECTION:
            runtime_characters_path = Path(
                config_manager.get_runtime_config_path("characters.json")
            )
            runtime_characters_existed = runtime_characters_path.is_file()
            characters_payload = _runtime_characters_with_safe_master(config_manager)
            merged_character_map = deepcopy(characters_payload.get("猫娘") or {})
            for tombstone_name in tombstone_names:
                merged_character_map.pop(tombstone_name, None)
            merged_character_map.update(snapshot_character_map)
            characters_payload["猫娘"] = merged_character_map

            local_current_name = str(characters_payload.get("当前猫娘") or "").strip()
            if (
                runtime_characters_existed
                and local_current_name
                and local_current_name in merged_character_map
            ):
                characters_payload["当前猫娘"] = local_current_name
            elif requested_current_name and requested_current_name in merged_character_map:
                characters_payload["当前猫娘"] = requested_current_name
            elif applied_character_names:
                characters_payload["当前猫娘"] = applied_character_names[0]
            elif local_current_name and local_current_name in merged_character_map:
                characters_payload["当前猫娘"] = local_current_name
            elif merged_character_map:
                characters_payload["当前猫娘"] = sorted(merged_character_map)[0]
            else:
                characters_payload["当前猫娘"] = ""
        else:
            characters_payload = deepcopy(cloud_characters_payload)
            characters_payload["猫娘"] = snapshot_character_map
            if not _has_usable_master_profile(characters_payload.get("主人")):
                safe_runtime_payload = _runtime_characters_with_safe_master(config_manager)
                characters_payload["主人"] = deepcopy(safe_runtime_payload["主人"])

            if requested_current_name and requested_current_name in snapshot_character_map:
                characters_payload["当前猫娘"] = requested_current_name
            elif applied_character_names:
                characters_payload["当前猫娘"] = applied_character_names[0]
            else:
                characters_payload["当前猫娘"] = ""
        apply_time = _utc_now_iso()
        backup_root = config_manager.cloudsave_backups_dir / f"import-{apply_time.replace(':', '').replace('.', '')}"

        characters_stage_path = _stage_json_file(
            stage_root,
            "__runtime__/profiles/characters.json",
            characters_payload,
        )
        runtime_targets: dict[Path, Path] = {
            Path(config_manager.get_runtime_config_path("characters.json")): characters_stage_path,
        }

        if snapshot_kind == SNAPSHOT_KIND_FULL_RUNTIME:
            preferences_stage_path = _stage_json_file(
                stage_root,
                "__runtime__/user_preferences.json",
                _build_runtime_preferences_payload(config_manager, conversation_settings),
            )
            runtime_targets[Path(config_manager.get_runtime_config_path("user_preferences.json"))] = preferences_stage_path

        memory_entries: list[tuple[str, str, Path]] = []
        for relative_path, staged_path in staged_entries.items():
            if not relative_path.startswith("memory/"):
                continue
            parts = Path(relative_path).parts
            # manifest key 是不可信输入：三段结构之外，还要挡住 '..'/'.' 段
            # （"memory/../x" 的 parts 恰好三段）和白名单外的叶子文件名。
            if (
                len(parts) != 3
                or Path(relative_path).is_absolute()
                or any(part in ("..", ".") for part in parts)
                or parts[2] not in MANAGED_MEMORY_FILENAMES
            ):
                raise ValueError(f"unsupported cloudsave memory path: {relative_path}")
            _, character_name, filename = parts
            memory_entries.append((character_name, filename, staged_path))

        memory_name_audit = audit_cloudsave_character_names(
            sorted({character_name for character_name, _, _ in memory_entries})
        )
        _raise_for_name_audit(memory_name_audit, context="import memory")
        for character_name, filename, staged_path in memory_entries:
            if character_name in tombstone_names:
                continue
            runtime_targets[Path(config_manager.memory_dir) / character_name / filename] = staged_path

        cloud_state = config_manager.load_cloudsave_local_state()
        cloud_state["last_applied_manifest_fingerprint"] = computed_fingerprint
        cloud_state["last_successful_import_at"] = apply_time
        cloud_state_stage_path = _stage_json_file(
            stage_root,
            "__runtime__/state/cloudsave_local_state.json",
            cloud_state,
        )
        runtime_targets[config_manager.cloudsave_local_state_path] = cloud_state_stage_path
        tombstones_state_stage_path = _stage_json_file(
            stage_root,
            "__runtime__/state/character_tombstones.json",
            tombstones_state,
        )
        runtime_targets[config_manager.character_tombstones_state_path] = tombstones_state_stage_path

        delete_file_targets: set[Path] = set()
        delete_dir_targets: set[Path] = set()
        for character_name in applied_character_names:
            character_dir = Path(config_manager.memory_dir) / character_name
            for filename in MANAGED_MEMORY_FILENAMES:
                relative_path = f"memory/{character_name}/{filename}"
                target_path = character_dir / filename
                if relative_path not in staged_entries and target_path.exists():
                    delete_file_targets.add(target_path)

        from utils.config_manager.migrations import (
            _MIGRATION_WORKSPACE_PREFIX,
        )

        def _is_migration_workspace(path):
            """The NAME, which is the one thing that cannot be forged.

            A character name never begins with a dot -- a product rule, to be
            enforced in ``validate_character_name`` as a follow-up -- and the
            workspace prefix does. So the two namespaces do not overlap and
            the name settles it.

            Six rounds of findings landed on this exemption while it tried to
            tell the namespaces apart with evidence instead: the prefix plus
            a marker file, a held lock, a held lock on a regular file, the
            ledger, the ledger or a held lock. Every one was the same shape,
            because every piece of evidence is written AFTER the directory
            exists and there is always a window in which the directory has
            none. mkdtemp returns a name, and the name is enough.

            The cost is that an abandoned workspace is not swept by the
            import either. That is not the import's job: the migration
            reclaims its own, by age and by ledger, which is where the
            evidence belongs.
            """
            return path.name.startswith(_MIGRATION_WORKSPACE_PREFIX)


        memory_root = Path(config_manager.memory_dir)
        if memory_root.exists():
            for child in memory_root.iterdir():
                if not child.is_dir():
                    continue
                if _is_migration_workspace(child):
                    # A startup migration WORKSPACE, not stale runtime
                    # data. When memory/ is a junction onto another
                    # volume the migration has to stage inside it, and
                    # this import can run while that copy is in flight --
                    # so removing it here deletes a half-copied character
                    # tree out from under the process writing it.
                    #
                    # Exempt because it is IN THE LEDGER. Four narrower
                    # readings of the on-disk evidence were reported in
                    # turn -- the prefix, the prefix plus a marker file, a
                    # held lock, a held lock on a regular file -- and each
                    # was a tighter guess at a question those shapes
                    # cannot answer: a character may legally be named
                    # ".mig-anything" and may hold a ".lock", so the very
                    # data being swept can reproduce every one of them.
                    #
                    # The ledger is written immediately after mkdtemp and
                    # before the workspace is used, so a workspace whose
                    # lock is not claimed yet is already recorded; and a
                    # character's stray marker never will be, however
                    # firmly something holds it.
                    continue
                if snapshot_kind == SNAPSHOT_KIND_FULL_RUNTIME:
                    if child.name not in applied_character_names:
                        delete_dir_targets.add(child)
                elif child.name in tombstone_names:
                    delete_dir_targets.add(child)

        recent_targets = {
            target_path
            for target_path in set(runtime_targets) | delete_file_targets
            if target_path.name == "recent.json"
        }
        for character_name in applied_character_names:
            recent_targets.update(
                list_character_recent_paths(config_manager, character_name)
            )
        for directory in delete_dir_targets:
            recent_targets.update(
                list_character_recent_paths(config_manager, directory.name)
            )
        with recent_file_locks(list(recent_targets)):
            deleted_recent_paths = {
                recent_path
                for directory in delete_dir_targets
                for recent_path in list_character_recent_paths(
                    config_manager, directory.name,
                )
            }
            imported_recent_paths = {
                recent_path
                for character_name in applied_character_names
                for recent_path in list_character_recent_paths(
                    config_manager, character_name,
                )
            }
            (
                deleted_redirects,
                deletion_scope,
                deleted_deletion_snapshot,
            ) = fence_recent_deletions_and_clear_redirects(deleted_recent_paths)
            (
                active_redirects,
                activation_scope,
                active_deletion_snapshot,
                active_generation_snapshot,
            ) = activate_recent_paths(list(imported_recent_paths))
            redirect_snapshot = dict(deleted_redirects)
            redirect_snapshot.update(active_redirects)
            deletion_snapshot = deleted_deletion_snapshot | (
                active_deletion_snapshot - deletion_scope
            )
            registry_restore_scope = deletion_scope | activation_scope
            recent_state_paths = set(recent_targets) | registry_restore_scope
            pending_snapshot = {
                recent_path: get_recent_pending_unlocked(recent_path)
                for recent_path in recent_state_paths
            }
            backup_records: list[dict[str, Any]] = []
            try:
                # recent 锁必须覆盖 rollback backup；否则 fence 前已启动的 writer
                # 能在 copy 与 apply 之间成功落盘，失败回滚再拿旧 backup 把它盖掉。
                for target_path in sorted(
                    set(runtime_targets) | delete_file_targets | delete_dir_targets,
                    key=lambda path: len(path.parts),
                ):
                    record = {
                        "target": target_path,
                        "backup": None,
                        "is_dir": target_path.is_dir(),
                    }
                    if target_path.exists():
                        backup_path = _build_backup_path(config_manager, backup_root, target_path)
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        if target_path.is_dir():
                            shutil.copytree(target_path, backup_path, dirs_exist_ok=True)
                        else:
                            shutil.copy2(target_path, backup_path)
                        record["backup"] = backup_path
                    backup_records.append(record)

                _assert_deadline_not_exceeded(
                    deadline_monotonic,
                    operation="import",
                    stage="apply_runtime",
                )
                for target_path, staged_path in runtime_targets.items():
                    _facade._apply_runtime_file(staged_path, target_path)

                for target_path in sorted(delete_file_targets):
                    if target_path.exists():
                        target_path.unlink()
                        _cleanup_empty_parent_dirs(target_path, Path(config_manager.memory_dir))

                for target_path in sorted(delete_dir_targets, key=lambda path: len(path.parts), reverse=True):
                    # Asked AGAIN, because the answer can change after
                    # enumeration. A cross-device migration that had returned
                    # from mkdtemp() but not yet created and locked its marker
                    # read as inactive when this set was built, and could
                    # claim the workspace and begin copying during the
                    # file-apply phase above -- which is long.
                    #
                    # This narrows the window rather than closing it: nothing
                    # can be held across an rmtree. It is the same move the
                    # migration's own publish steps make, re-checking one
                    # statement before the irreversible one.
                    if _is_migration_workspace(target_path):
                        continue
                    if target_path.exists():
                        shutil.rmtree(target_path)

                for recent_path in recent_state_paths:
                    set_recent_pending_unlocked(recent_path, [])
                # The apply above wrote managed files and removed the directories
                # of characters absent from the snapshot. Per-character sidecar
                # caches only re-read on a MISS, so for a REMOVED name a stale
                # entry describes a file that is gone and would be flushed back,
                # recreating the directory. Handle both here, while the fence is
                # still closed, so nothing can repopulate from the old state.
                memory_root = Path(config_manager.memory_dir)
                removed_character_names = [
                    target_path.name
                    for target_path in delete_dir_targets
                    if target_path.parent == memory_root
                ]
                # Imported names are LIVE identities, and the apply never touched
                # their sidecars, so they only need the retirement lifted --
                # evicting would fence away a staged-but-unflushed snapshot.
                # Removed names are the opposite case: their directories really
                # are gone, so they retire.
                revive_character_runtime_caches(*applied_character_names)
                retire_character_runtime_caches(*removed_character_names)
                return {
                    "manifest_fingerprint": computed_fingerprint,
                    "snapshot_kind": snapshot_kind,
                    "applied_character_count": len(applied_character_names),
                    "name_audit": name_audit,
                }
            except Exception:
                try:
                    for record in sorted(
                        backup_records,
                        key=lambda item: len(item["target"].parts),
                        reverse=True,
                    ):
                        target_path = record["target"]
                        # Rollback has to ask the same question the deletion
                        # loop does. A workspace recorded after enumeration is
                        # skipped there but is still in backup_records, so an
                        # unrelated failure elsewhere would have this restore a
                        # stale backup over a tree another process is writing
                        # -- and leave that migration's seed unavailable for
                        # the session.
                        if _is_migration_workspace(target_path):
                            continue
                        if target_path.exists():
                            if target_path.is_dir():
                                shutil.rmtree(target_path, ignore_errors=True)
                            else:
                                target_path.unlink()
                        backup_path = record["backup"]
                        if backup_path is None or not backup_path.exists():
                            continue
                        if record["is_dir"]:
                            shutil.copytree(backup_path, target_path, dirs_exist_ok=True)
                        else:
                            _facade._apply_runtime_file(backup_path, target_path)
                finally:
                    for recent_path, messages in pending_snapshot.items():
                        set_recent_pending_unlocked(recent_path, messages)
                    restore_recent_registry_state(
                        list(registry_restore_scope),
                        redirect_snapshot,
                        deletion_snapshot,
                        active_generation_snapshot,
                    )
                raise
