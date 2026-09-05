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

"""One-shot bootstrap import of legacy runtime roots into the deterministic
app data root, including config/character merge heuristics.

Split out of the former monolithic ``utils/cloudsave_runtime.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from config import DEFAULT_CONFIG_DATA
from utils.file_utils import atomic_write_json
from utils.storage_path_rewrite import rebase_runtime_bound_workshop_config_paths

from ._shared import (
    LEGACY_OPTIONAL_STATE_FILES,
    LEGACY_RUNTIME_DIR_NAMES,
    NON_RUNTIME_CONTENT_DIR_NAMES,
    ROOT_CONFIG_MERGE_FILES,
    TRANSACTIONAL_RUNTIME_ENTRY_PATTERNS,
    RUNTIME_ASSET_DIR_NAMES,
    TARGET_OPTIONAL_STATE_FILES,
)
from .staging import (
    _json_canonical_dumps,
    _load_json_if_exists,
    _load_tombstone_names_from_state_path,
    _stage_file_copy,
)


def _runtime_config_path_matches_pristine_default(config_manager, runtime_path: Path) -> bool:
    source_path = None
    if runtime_path.name == "characters.json":
        localized_source = getattr(config_manager, "_get_localized_characters_source", lambda: None)()
        if localized_source:
            source_path = Path(localized_source)
    if source_path is None:
        candidate = Path(config_manager.project_config_dir) / runtime_path.name
        if candidate.exists():
            source_path = candidate

    if source_path is not None and source_path.exists():
        try:
            return runtime_path.read_bytes() == source_path.read_bytes()
        except Exception:
            return False

    default_payload = DEFAULT_CONFIG_DATA.get(runtime_path.name)
    if default_payload is None:
        return False
    try:
        return json.loads(runtime_path.read_text(encoding="utf-8")) == default_payload
    except Exception:
        return False


def _runtime_config_dir_has_user_content(config_manager) -> bool:
    config_dir = Path(config_manager.config_dir)
    if not config_dir.exists():
        return False
    for child in config_dir.iterdir():
        if _is_ignorable_runtime_entry(child):
            continue
        if child.is_dir():
            return True
        if not _runtime_config_path_matches_pristine_default(config_manager, child):
            return True
    return False


def _runtime_root_has_user_content(root: Path, *, config_manager=None) -> bool:
    if not root.exists():
        return False
    config_dir = None
    if config_manager is not None:
        try:
            config_dir = Path(config_manager.config_dir)
        except Exception:
            config_dir = None
    for name in LEGACY_RUNTIME_DIR_NAMES:
        if name in NON_RUNTIME_CONTENT_DIR_NAMES:
            continue
        candidate = root / name
        if candidate.is_file():
            return True
        if candidate.is_dir():
            if config_dir is not None and candidate == config_dir:
                if _runtime_config_dir_has_user_content(config_manager):
                    return True
                continue
            transactional_pattern = TRANSACTIONAL_RUNTIME_ENTRY_PATTERNS.get(name)
            try:
                for child in candidate.iterdir():
                    if _is_ignorable_runtime_entry(
                        child, transactional_pattern=transactional_pattern
                    ):
                        continue
                    return True
            except StopIteration:
                continue
    return False


def runtime_root_has_user_content(root: Path, *, config_manager=None) -> bool:
    """Public wrapper for detecting user-owned runtime data in a storage root."""
    return _runtime_root_has_user_content(root, config_manager=config_manager)


def _is_ignorable_runtime_entry(path: Path, *, transactional_pattern=None) -> bool:
    name = path.name
    if name == ".gitkeep":
        return True
    if name.startswith("."):
        # 事务目录是崩溃恢复的唯一线索，判定「有没有用户内容」时它算内容。
        # 名字必须逐字命中该目录声明的事务模式，不能只看点前缀和后缀。
        return not (
            transactional_pattern is not None
            and transactional_pattern.fullmatch(name) is not None
        )
    if name == "__pycache__":
        return True
    return False


def _copy_runtime_root_entries(source_root: Path, destination_root: Path) -> list[str]:
    copied_paths: list[str] = []
    for name in LEGACY_RUNTIME_DIR_NAMES:
        source_path = source_root / name
        if not source_path.exists():
            continue
        destination_path = destination_root / name
        if source_path.is_dir():
            shutil.copytree(source_path, destination_path, dirs_exist_ok=True)
        else:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
        copied_paths.append(name)
    return copied_paths


def _directory_has_meaningful_content(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        for child in path.iterdir():
            if _is_ignorable_runtime_entry(child):
                continue
            return True
    except Exception:
        return False
    return False


def _collect_memory_character_names(root: Path) -> set[str]:
    memory_root = root / "memory"
    character_names: set[str] = set()
    if not memory_root.is_dir():
        return character_names
    try:
        for child in memory_root.iterdir():
            if _is_ignorable_runtime_entry(child):
                continue
            if child.is_dir() and _directory_has_meaningful_content(child):
                character_names.add(child.name)
            elif child.is_file():
                character_names.add(child.stem)
    except Exception:
        return character_names
    return character_names


def _load_seed_characters_payload(config_manager) -> dict[str, Any]:
    localized_source = None
    try:
        localized_source = config_manager._get_localized_characters_source()
    except Exception:
        localized_source = None
    if localized_source is not None:
        payload = _load_json_if_exists(Path(localized_source))
        if isinstance(payload, dict):
            return payload
    fallback_payload = config_manager.get_default_characters()
    return fallback_payload if isinstance(fallback_payload, dict) else {}


def _normalize_catgirl_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    normalized_payload = deepcopy(payload)
    try:
        from utils.config_manager import migrate_catgirl_reserved

        migrate_catgirl_reserved(normalized_payload)
    except Exception:
        pass
    return normalized_payload


def _character_payload_looks_default(config_manager, name: str, payload: Any) -> bool:
    normalized_payload = _normalize_catgirl_payload(payload)
    if normalized_payload is None:
        return False
    default_payload = _normalize_catgirl_payload((_load_seed_characters_payload(config_manager).get("猫娘") or {}).get(name))
    return default_payload is not None and normalized_payload == default_payload


def _master_payload_looks_default(config_manager, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    default_payload = _load_seed_characters_payload(config_manager).get("主人")
    return default_payload is not None and payload == default_payload


def _normalize_preferences_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return deepcopy(payload)
    if isinstance(payload, dict):
        return [deepcopy(payload)]
    return []


def _preferences_entry_key(entry: Any) -> str:
    if isinstance(entry, dict) and entry.get("model_path") is not None:
        return f"model_path:{entry.get('model_path')}"
    return _json_canonical_dumps(entry)


def _merge_preferences_payloads(legacy_payload: Any, current_payload: Any) -> list[Any]:
    merged_entries: dict[str, Any] = {}
    ordered_keys: list[str] = []
    for payload in (_normalize_preferences_payload(legacy_payload), _normalize_preferences_payload(current_payload)):
        for entry in payload:
            key = _preferences_entry_key(entry)
            if key not in merged_entries:
                ordered_keys.append(key)
            merged_entries[key] = deepcopy(entry)
    return [merged_entries[key] for key in ordered_keys]


def _deep_merge_json_dicts(legacy_payload: Any, current_payload: Any) -> dict[str, Any]:
    legacy_dict = deepcopy(legacy_payload) if isinstance(legacy_payload, dict) else {}
    current_dict = current_payload if isinstance(current_payload, dict) else {}
    for key, value in current_dict.items():
        if isinstance(legacy_dict.get(key), dict) and isinstance(value, dict):
            legacy_dict[key] = _deep_merge_json_dicts(legacy_dict[key], value)
        else:
            legacy_dict[key] = deepcopy(value)
    return legacy_dict


def _config_payload_looks_default(filename: str, payload: Any) -> bool:
    default_payload = DEFAULT_CONFIG_DATA.get(filename)
    if filename == "user_preferences.json":
        return _normalize_preferences_payload(payload) == _normalize_preferences_payload(default_payload)
    if isinstance(default_payload, dict):
        return isinstance(payload, dict) and deepcopy(payload) == deepcopy(default_payload)
    if isinstance(default_payload, list):
        return isinstance(payload, list) and deepcopy(payload) == deepcopy(default_payload)
    return False


def _config_payload_looks_seeded(config_manager, filename: str, payload: Any) -> bool:
    project_payload = _load_json_if_exists(Path(config_manager.project_config_dir) / filename)
    if project_payload is not None:
        if filename == "user_preferences.json":
            return _normalize_preferences_payload(payload) == _normalize_preferences_payload(project_payload)
        return deepcopy(payload) == deepcopy(project_payload)
    return _config_payload_looks_default(filename, payload)


def _merge_characters_payloads(
    config_manager,
    legacy_payload: Any,
    current_payload: Any,
    *,
    preserve_current_only_defaults: bool,
) -> dict[str, Any]:
    legacy_dict = deepcopy(legacy_payload) if isinstance(legacy_payload, dict) else {}
    current_dict = deepcopy(current_payload) if isinstance(current_payload, dict) else {}
    merged_payload = deepcopy(legacy_dict)

    for key, value in current_dict.items():
        if key not in {"猫娘", "主人", "当前猫娘"}:
            merged_payload[key] = deepcopy(value)

    legacy_catgirls = legacy_dict.get("猫娘") or {}
    current_catgirls = current_dict.get("猫娘") or {}
    merged_catgirls: dict[str, Any] = {}
    for name in sorted(set(legacy_catgirls) | set(current_catgirls)):
        legacy_character = legacy_catgirls.get(name)
        current_character = current_catgirls.get(name)
        if legacy_character is None:
            if not preserve_current_only_defaults and _character_payload_looks_default(config_manager, name, current_character):
                continue
            chosen = current_character
        elif current_character is None:
            chosen = legacy_character
        else:
            current_default = _character_payload_looks_default(config_manager, name, current_character)
            legacy_default = _character_payload_looks_default(config_manager, name, legacy_character)
            if current_default and not legacy_default:
                chosen = legacy_character
            elif legacy_default and not current_default:
                chosen = current_character
            else:
                chosen = current_character
        if chosen is not None:
            merged_catgirls[name] = deepcopy(chosen)
    merged_payload["猫娘"] = merged_catgirls

    legacy_master = legacy_dict.get("主人")
    current_master = current_dict.get("主人")
    if legacy_master is None:
        if current_master is not None:
            merged_payload["主人"] = deepcopy(current_master)
    elif current_master is None:
        merged_payload["主人"] = deepcopy(legacy_master)
    else:
        current_master_default = _master_payload_looks_default(config_manager, current_master)
        legacy_master_default = _master_payload_looks_default(config_manager, legacy_master)
        chosen_master = legacy_master if current_master_default and not legacy_master_default else current_master
        merged_payload["主人"] = deepcopy(chosen_master)

    current_current_name = str(current_dict.get("当前猫娘") or "")
    legacy_current_name = str(legacy_dict.get("当前猫娘") or "")
    if current_current_name and current_current_name in merged_catgirls:
        current_current_payload = current_catgirls.get(current_current_name)
        current_default = _character_payload_looks_default(config_manager, current_current_name, current_current_payload)
        if current_current_name not in legacy_catgirls and not preserve_current_only_defaults and current_default:
            current_current_name = ""
        elif current_current_name not in legacy_catgirls or not current_default:
            merged_payload["当前猫娘"] = current_current_name
        elif legacy_current_name and legacy_current_name in merged_catgirls:
            merged_payload["当前猫娘"] = legacy_current_name
        else:
            merged_payload["当前猫娘"] = current_current_name
    elif legacy_current_name and legacy_current_name in merged_catgirls:
        merged_payload["当前猫娘"] = legacy_current_name
    elif current_current_name and current_current_name in merged_catgirls:
        merged_payload["当前猫娘"] = current_current_name
    elif merged_catgirls:
        merged_payload["当前猫娘"] = next(iter(merged_catgirls))
    else:
        merged_payload["当前猫娘"] = ""

    return merged_payload


def _runtime_root_summary(config_manager, root: Path) -> dict[str, Any]:
    config_root = root / "config"
    characters_path = config_root / "characters.json"
    user_preferences_path = config_root / "user_preferences.json"
    voice_storage_path = config_root / "voice_storage.json"
    workshop_config_path = config_root / "workshop_config.json"
    core_config_path = config_root / "core_config.json"
    plugin_models_path = config_root / "plugin_models.json"

    characters_payload = _load_json_if_exists(characters_path)
    user_preferences_payload = _load_json_if_exists(user_preferences_path)
    voice_storage_payload = _load_json_if_exists(voice_storage_path)
    core_config_payload = _load_json_if_exists(core_config_path)
    if not isinstance(characters_payload, dict):
        characters_payload = None
    character_names = set((characters_payload or {}).get("猫娘", {}) or {})
    default_character_names = set((_load_seed_characters_payload(config_manager).get("猫娘") or {}).keys())

    asset_dirs_with_content = {
        dir_name: _directory_has_meaningful_content(root / dir_name)
        for dir_name in RUNTIME_ASSET_DIR_NAMES
    }
    memory_character_names = _collect_memory_character_names(root)
    seeded_character_shell = (
        character_names.issubset(default_character_names)
        and not memory_character_names
        and not any(asset_dirs_with_content.values())
    )
    score = (
        len(character_names) * 3
        + len(memory_character_names) * 2
        + (3 if user_preferences_path.is_file() else 0)
        + (2 if voice_storage_path.is_file() else 0)
        + (1 if workshop_config_path.is_file() else 0)
        + (1 if core_config_path.is_file() else 0)
        + (1 if plugin_models_path.is_file() else 0)
        + sum(2 for has_content in asset_dirs_with_content.values() if has_content)
    )

    return {
        "has_user_content": _runtime_root_has_user_content(root, config_manager=config_manager),
        "characters_payload": characters_payload,
        "character_names": character_names,
        "memory_character_names": memory_character_names,
        "has_user_preferences": user_preferences_path.is_file(),
        "has_voice_storage": voice_storage_path.is_file(),
        "has_workshop_config": workshop_config_path.is_file(),
        "has_core_config": core_config_path.is_file(),
        "has_plugin_models": plugin_models_path.is_file(),
        "asset_dirs_with_content": asset_dirs_with_content,
        "seeded_character_shell": seeded_character_shell,
        "looks_like_seeded": (
            bool(character_names)
            and character_names.issubset(default_character_names)
            and not memory_character_names
            and (
                not user_preferences_path.is_file()
                or _config_payload_looks_seeded(config_manager, "user_preferences.json", user_preferences_payload)
            )
            and (
                not voice_storage_path.is_file()
                or _config_payload_looks_seeded(config_manager, "voice_storage.json", voice_storage_payload)
            )
            and not workshop_config_path.is_file()
            and not plugin_models_path.is_file()
            and (
                not core_config_path.is_file()
                or _config_payload_looks_seeded(config_manager, "core_config.json", core_config_payload)
            )
            and not any(asset_dirs_with_content.values())
        ),
        "score": score,
    }


def _legacy_root_provides_repair_benefit(config_manager, source_summary: dict[str, Any], target_summary: dict[str, Any]) -> tuple[bool, str]:
    if not target_summary["has_user_content"]:
        return True, "target_missing"

    source_is_richer = source_summary["score"] > target_summary["score"]
    target_is_seed_shell = bool(target_summary.get("seeded_character_shell"))

    if target_is_seed_shell:
        if source_summary["character_names"] - target_summary["character_names"]:
            return True, "missing_characters"

        if source_summary["memory_character_names"] - target_summary["memory_character_names"]:
            return True, "missing_memory"

        for flag_name, reason in (
            ("has_user_preferences", "missing_user_preferences"),
            ("has_voice_storage", "missing_voice_storage"),
            ("has_workshop_config", "missing_workshop_config"),
            ("has_core_config", "missing_core_config"),
            ("has_plugin_models", "missing_plugin_models"),
        ):
            if source_summary[flag_name] and not target_summary[flag_name]:
                return True, reason

        for dir_name, source_has_content in source_summary["asset_dirs_with_content"].items():
            if source_has_content and not target_summary["asset_dirs_with_content"].get(dir_name):
                return True, f"missing_{dir_name}"

    source_characters = (source_summary.get("characters_payload") or {}).get("猫娘", {}) or {}
    target_characters = (target_summary.get("characters_payload") or {}).get("猫娘", {}) or {}
    for name in sorted(set(source_characters) & set(target_characters)):
        if (
            _character_payload_looks_default(config_manager, name, target_characters.get(name))
            and not _character_payload_looks_default(config_manager, name, source_characters.get(name))
        ):
            return True, "upgrade_default_character"

    if target_is_seed_shell and source_is_richer:
        return True, "repair_seeded_target"

    return False, ""


def _stage_merged_runtime_configs(config_manager, *, source_root: Path, target_root: Path, temp_root: Path, target_summary: dict[str, Any]) -> None:
    config_dir = temp_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    target_tombstone_names = _load_tombstone_names_from_state_path(
        target_root / "state" / "character_tombstones.json"
    )

    source_characters = _load_json_if_exists(source_root / "config" / "characters.json")
    target_characters = _load_json_if_exists(target_root / "config" / "characters.json")
    if source_characters is not None or target_characters is not None:
        merged_characters = _merge_characters_payloads(
            config_manager,
            source_characters,
            target_characters,
            preserve_current_only_defaults=not bool(target_summary.get("seeded_character_shell")),
        )
        if target_tombstone_names:
            merged_catgirls = merged_characters.get("猫娘") or {}
            for deleted_name in target_tombstone_names:
                merged_catgirls.pop(deleted_name, None)
            merged_characters["猫娘"] = merged_catgirls
            current_name = str(merged_characters.get("当前猫娘") or "")
            if current_name in target_tombstone_names:
                merged_characters["当前猫娘"] = next(iter(merged_catgirls), "")
        atomic_write_json(config_dir / "characters.json", merged_characters, ensure_ascii=False, indent=2)

    source_preferences = _load_json_if_exists(source_root / "config" / "user_preferences.json")
    target_preferences = _load_json_if_exists(target_root / "config" / "user_preferences.json")
    if source_preferences is not None or target_preferences is not None:
        merged_preferences = _merge_preferences_payloads(source_preferences, target_preferences)
        atomic_write_json(config_dir / "user_preferences.json", merged_preferences, ensure_ascii=False, indent=2)

    # plugin_models.json is copied as one configuration unit by
    # _copy_runtime_root_entries (the current root wins). Deep-merging it could
    # pair an old API key with a new endpoint or resurrect a deleted binding.
    for filename in ROOT_CONFIG_MERGE_FILES:
        source_payload = _load_json_if_exists(source_root / "config" / filename)
        target_payload = _load_json_if_exists(target_root / "config" / filename)
        if source_payload is None and target_payload is None:
            continue
        merged_payload = _deep_merge_json_dicts(source_payload, target_payload)
        if filename == "workshop_config.json":
            merged_payload = rebase_runtime_bound_workshop_config_paths(
                merged_payload,
                source_root=source_root,
                target_root=target_root,
            )
        atomic_write_json(config_dir / filename, merged_payload, ensure_ascii=False, indent=2)


def _copy_optional_legacy_state(*, source_root: Path, target_root: Path, temp_root: Path) -> list[str]:
    copied_paths: list[str] = []
    for filename in TARGET_OPTIONAL_STATE_FILES:
        target_path = target_root / "state" / filename
        if not target_path.is_file():
            continue
        _stage_file_copy(temp_root, f"state/{filename}", target_path)
        copied_paths.append(f"state/{filename}")
    for filename in LEGACY_OPTIONAL_STATE_FILES:
        source_path = source_root / "state" / filename
        if not source_path.is_file() or (temp_root / "state" / filename).is_file():
            continue
        _stage_file_copy(temp_root, f"state/{filename}", source_path)
        copied_paths.append(f"state/{filename}")
    return copied_paths


def _create_legacy_import_backup_path(target_root: Path) -> Path:
    backup_pool = target_root.parent / f".{target_root.name}.legacy-import-backups"
    backup_pool.mkdir(parents=True, exist_ok=True)
    backup_slot = Path(tempfile.mkdtemp(prefix="backup-", dir=str(backup_pool)))
    return backup_slot / target_root.name


def _replace_runtime_root(target_root: Path, temp_root: Path, *, backup_path: Path | None = None) -> None:
    if backup_path is None:
        if target_root.exists():
            shutil.rmtree(target_root, ignore_errors=True)
        os.replace(temp_root, target_root)
        return

    restore_required = False
    try:
        if target_root.exists():
            os.replace(target_root, backup_path)
            restore_required = True
        os.replace(temp_root, target_root)
    except Exception:
        if restore_required and backup_path.exists() and not target_root.exists():
            os.replace(backup_path, target_root)
        raise


def _legacy_source_was_already_imported(
    root_state: Any,
    *,
    source_root: Path,
    target_root: Path,
) -> bool:
    """Treat legacy root import as a one-shot bootstrap repair per source root.

    Once a legacy root has already been imported and the migrated target has
    completed at least one successful boot, future startups should treat the
    current runtime root as the source of truth. Otherwise, deletions performed
    in the new runtime root can be "repaired" back from the stale legacy root.
    """
    if not isinstance(root_state, dict):
        return False
    if str(root_state.get("current_root") or "") != str(target_root):
        return False
    if not str(root_state.get("last_successful_boot_at") or "").strip():
        return False
    if str(root_state.get("last_migration_source") or "") != str(source_root):
        return False
    last_result = str(root_state.get("last_migration_result") or "")
    return last_result.startswith("legacy_root_")


def _root_has_staged_cloudsave_snapshot(root: Path) -> bool:
    manifest_path = Path(root) / "cloudsave" / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_files = manifest_payload.get("files")
            if isinstance(manifest_files, dict) and manifest_files:
                return True
        except Exception:
            pass

    cloudsave_root = Path(root) / "cloudsave"
    if not cloudsave_root.exists():
        return False
    try:
        for child in cloudsave_root.rglob("*"):
            if child.is_file() and child.name != "manifest.json":
                return True
    except Exception:
        return False
    return False


def import_legacy_runtime_root_if_needed(config_manager) -> dict[str, Any]:
    """One-time bootstrap import from legacy roots into the deterministic app data root."""
    target_root = Path(config_manager.app_docs_dir)
    target_has_user_content = _runtime_root_has_user_content(target_root, config_manager=config_manager)
    target_has_staged_cloudsave_snapshot = _root_has_staged_cloudsave_snapshot(target_root)
    target_summary = _runtime_root_summary(config_manager, target_root)
    existing_root_state = None
    try:
        if config_manager.root_state_path.is_file():
            existing_root_state = config_manager.load_root_state()
    except Exception:
        existing_root_state = None

    if target_has_staged_cloudsave_snapshot and not target_has_user_content:
        return {
            "migrated": False,
            "source": "",
            "copied_paths": [],
            "backup_path": "",
            "repair_reason": "",
            "result": "target_root_preserves_staged_cloudsave_snapshot",
        }

    saw_legacy_source = False

    for source_root in config_manager.get_legacy_app_root_candidates():
        source_root = Path(source_root)
        if not _runtime_root_has_user_content(source_root, config_manager=config_manager):
            continue
        saw_legacy_source = True
        if _legacy_source_was_already_imported(
            existing_root_state,
            source_root=source_root,
            target_root=target_root,
        ):
            continue

        source_summary = _runtime_root_summary(config_manager, source_root)
        should_repair, repair_reason = _legacy_root_provides_repair_benefit(
            config_manager,
            source_summary,
            target_summary,
        )
        if target_has_user_content and not should_repair:
            continue

        temp_root = target_root.parent / f".{target_root.name}.bootstrap-import"
        if temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)
        temp_root.parent.mkdir(parents=True, exist_ok=True)

        copied_paths: list[str] = []
        backup_path: Path | None = None
        try:
            copied_paths.extend(_copy_runtime_root_entries(source_root, temp_root))
            if target_has_user_content:
                _copy_runtime_root_entries(target_root, temp_root)
                _stage_merged_runtime_configs(
                    config_manager,
                    source_root=source_root,
                    target_root=target_root,
                    temp_root=temp_root,
                    target_summary=target_summary,
                )
                backup_path = _create_legacy_import_backup_path(target_root)
            copied_paths.extend(_copy_optional_legacy_state(source_root=source_root, target_root=target_root, temp_root=temp_root))

            if not copied_paths:
                shutil.rmtree(temp_root, ignore_errors=True)
                continue

            _replace_runtime_root(target_root, temp_root, backup_path=backup_path)
            return {
                "migrated": True,
                "source": str(source_root),
                "copied_paths": sorted(set(copied_paths)),
                "backup_path": str(backup_path) if backup_path is not None else "",
                "repair_reason": repair_reason,
                "result": "legacy_root_repaired_target" if target_has_user_content else "legacy_root_imported",
            }
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

    if target_has_user_content:
        return {
            "migrated": False,
            "source": "",
            "copied_paths": [],
            "backup_path": "",
            "repair_reason": "",
            "result": "target_root_already_initialized" if saw_legacy_source or target_summary["has_user_content"] else "no_legacy_root_found",
        }

    return {
        "migrated": False,
        "source": "",
        "copied_paths": [],
        "backup_path": "",
        "repair_reason": "",
        "result": "no_legacy_root_found",
    }
