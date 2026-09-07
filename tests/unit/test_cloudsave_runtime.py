import copy
import contextlib
import json
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from utils.file_utils import atomic_write_json


@contextmanager
def _isolated_sidecar_stores(memory_dir, config_manager=None):
    """Swap the three sidecar singletons for FRESH instances.

    Saving the module globals and restoring them is not enough. These tests
    mutate ``_cache`` and ``_retired`` on the EXISTING objects, and putting the
    same reference back leaves those mutations in place -- so an entry another
    test also uses is silently dropped and the suite becomes order-dependent.
    """
    import memory.anti_repeat as anti_repeat_module
    import memory.anti_repeat_effects as effects_module
    import memory.startup_greeting_history as greeting_module

    # A real config manager when the test drives a real flush: the write path
    # enters cloudsave_writable_transaction, which needs more than memory_dir.
    if config_manager is None:
        config_manager = SimpleNamespace(memory_dir=str(memory_dir))
    store = effects_module.AntiRepeatEffectStore()
    store._config_manager = config_manager
    corpus = anti_repeat_module.AntiRepeatCorpus()
    corpus._config_manager = config_manager
    greeting = greeting_module.StartupGreetingHistory(config_manager)

    previous = (
        effects_module._GLOBAL_STORE,
        anti_repeat_module._GLOBAL_CORPUS,
        greeting_module._GLOBAL_HISTORY,
    )
    effects_module._GLOBAL_STORE = store
    anti_repeat_module._GLOBAL_CORPUS = corpus
    greeting_module._GLOBAL_HISTORY = greeting
    try:
        yield (store, corpus, greeting)
    finally:
        (
            effects_module._GLOBAL_STORE,
            anti_repeat_module._GLOBAL_CORPUS,
            greeting_module._GLOBAL_HISTORY,
        ) = previous
def _make_config_manager(
    tmp_path,
    platform: str | None = None,
    legacy_candidates: list[str] | None = None,
):
    from utils.config_manager import ConfigManager

    if legacy_candidates is None:
        legacy_candidates = []

    patchers = [
        patch.object(ConfigManager, "_get_documents_directory", return_value=tmp_path),
        patch.object(
            ConfigManager,
            "_get_standard_data_directory_candidates",
            return_value=[tmp_path],
        ),
        patch.object(
            ConfigManager,
            "get_legacy_app_root_candidates",
            return_value=list(legacy_candidates),
        ),
    ]
    if platform is not None:
        patchers.append(patch("utils.config_manager.sys.platform", platform))

    with contextlib.ExitStack() as stack:
        for patcher in patchers:
            stack.enter_context(patcher)
        config_manager = ConfigManager("N.E.K.O")

    config_manager.get_legacy_app_root_candidates = lambda: list(legacy_candidates)
    config_manager._get_standard_data_directory_candidates = lambda: [tmp_path]
    return config_manager


def _write_runtime_state(cm, *, character_name="小满"):
    from utils.config_manager import set_reserved

    characters = cm.get_default_characters()
    characters["猫娘"] = {
        character_name: characters["猫娘"][next(iter(characters["猫娘"]))]
    }
    characters["当前猫娘"] = character_name
    set_reserved(characters["猫娘"][character_name], "touch_set", {"default": {"tap": "wave"}})
    set_reserved(characters["猫娘"][character_name], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"][character_name], "avatar", "asset_source", "steam_workshop")
    set_reserved(characters["猫娘"][character_name], "avatar", "asset_source_id", "123456")
    set_reserved(characters["猫娘"][character_name], "avatar", "live2d", "model_path", "example/example.model3.json")
    cm.save_characters(characters, bypass_write_fence=True)

    prefs_path = Path(cm.get_config_path("user_preferences.json"))
    atomic_write_json(
        prefs_path,
        [
            {
                "model_path": "/user_live2d/example.model3.json",
                "position": {"x": 1, "y": 2, "z": 3},
                "scale": {"x": 1, "y": 1, "z": 1},
            },
            {
                "model_path": "__global_conversation__",
                "userLanguage": "zh-CN",
                "noiseReductionEnabled": True,
            },
        ],
        ensure_ascii=False,
        indent=2,
    )

    character_memory_dir = Path(cm.memory_dir) / character_name
    character_memory_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(character_memory_dir / "recent.json", [{"role": "user", "content": "你好"}], ensure_ascii=False, indent=2)
    atomic_write_json(character_memory_dir / "settings.json", {"mood": "calm"}, ensure_ascii=False, indent=2)
    atomic_write_json(character_memory_dir / "facts.json", [{"id": "fact-1", "content": "喜欢鱼"}], ensure_ascii=False, indent=2)
    atomic_write_json(character_memory_dir / "persona.json", {"traits": ["温柔"]}, ensure_ascii=False, indent=2)
    (character_memory_dir / "time_indexed.db").write_bytes(b"sqlite-placeholder")
    workshop_model_dir = Path(cm.workshop_dir) / "123456" / "example"
    workshop_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(workshop_model_dir / "example.model3.json", {"Version": 3}, ensure_ascii=False, indent=2)

    return characters


@pytest.mark.unit
def test_ui_language_override_uses_raw_global_preference_only(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.ensure_config_directory()
    atomic_write_json(
        cm.get_runtime_config_path("user_preferences.json"),
        [
            {
                "model_path": "__global_conversation__",
                "userLanguage": "en",
                "uiLanguage": "zh-TW",
            }
        ],
        ensure_ascii=False,
        indent=2,
    )

    import utils.preferences as preferences

    with patch.object(preferences, "_config_manager", cm):
        assert preferences.load_ui_language_override() == "zh-TW"
        assert "uiLanguage" not in preferences.load_global_conversation_settings()
        assert preferences.save_ui_language_override("ja") is True
        assert preferences.load_ui_language_override() == "ja"
        assert preferences.load_global_conversation_settings()["userLanguage"] == "en"
        assert preferences.save_ui_language_override(None) is True
        assert preferences.load_ui_language_override() is None
        assert preferences.load_global_conversation_settings()["userLanguage"] == "en"


@pytest.mark.unit
def test_resolve_managed_target_path_rejects_traversal(tmp_path):
    from utils.cloudsave_runtime import _resolve_managed_target_path

    cm = _make_config_manager(tmp_path)

    with pytest.raises(ValueError):
        _resolve_managed_target_path(cm, "anchor/../../outside.txt")
    with pytest.raises(ValueError):
        _resolve_managed_target_path(cm, "/absolute/outside.txt")

    resolved = _resolve_managed_target_path(cm, "runtime/config/characters.json")
    assert resolved == (cm.app_docs_dir / "config" / "characters.json").resolve(strict=False)


@pytest.mark.unit
def test_managed_target_relative_path_prefers_nested_anchor_root(tmp_path):
    from utils.cloudsave_runtime import _managed_target_relative_path

    cm = _make_config_manager(tmp_path)
    cm.anchor_root = cm.app_docs_dir / "anchor" / "N.E.K.O"
    target_path = cm.anchor_root / "state" / "storage_policy.json"

    assert _managed_target_relative_path(cm, target_path) == Path("anchor/state/storage_policy.json")


@pytest.mark.unit
def test_managed_target_round_trip_supports_project_memory_root(tmp_path):
    from utils.cloudsave_runtime import (
        _managed_target_relative_path,
        _resolve_managed_target_path,
    )

    cm = _make_config_manager(tmp_path / "runtime")
    cm.project_memory_dir = tmp_path / "checkout" / "memory" / "store"
    target_path = cm.project_memory_dir / "Old" / "recent.json"

    relative_path = _managed_target_relative_path(cm, target_path)

    assert relative_path == Path("project_memory/Old/recent.json")
    assert _resolve_managed_target_path(cm, str(relative_path)) == target_path.resolve(strict=False)


def _add_runtime_character(cm, character_name: str, *, recent_text: str) -> None:
    from utils.config_manager import set_reserved

    characters = cm.load_characters()
    template_payload = copy.deepcopy(next(iter(characters["猫娘"].values())))
    template_payload["档案名"] = character_name
    set_reserved(template_payload, "avatar", "model_type", "live2d")
    set_reserved(template_payload, "avatar", "asset_source", "steam_workshop")
    set_reserved(template_payload, "avatar", "asset_source_id", "123456")
    set_reserved(template_payload, "avatar", "live2d", "model_path", "example/example.model3.json")
    characters["猫娘"][character_name] = template_payload
    cm.save_characters(characters, bypass_write_fence=True)

    character_memory_dir = Path(cm.memory_dir) / character_name
    character_memory_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        character_memory_dir / "recent.json",
        [{"role": "user", "content": recent_text}],
        ensure_ascii=False,
        indent=2,
    )


@pytest.mark.unit
def test_bootstrap_creates_manifest_and_legacy_state(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    result = bootstrap_local_cloudsave_environment(cm)

    manifest = result["manifest"]
    root_state = result["root_state"]
    cloud_state = result["cloudsave_local_state"]

    assert cm.cloudsave_manifest_path.is_file()
    assert manifest["client_id"] == cloud_state["client_id"]
    assert manifest["schema_version"] == 1
    assert root_state["current_root"] == str(cm.app_docs_dir)
    assert root_state["last_migration_result"] in {"no_legacy_root_found", "bootstrap_initialized"}


@pytest.mark.unit
def test_bootstrap_reports_local_state_directory_diagnostic(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.anchor_root.write_text("not a directory", encoding="utf-8")

    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment
    from utils.config_manager import LocalStateDirectoryError

    with pytest.raises(LocalStateDirectoryError) as exc_info:
        bootstrap_local_cloudsave_environment(cm)

    message = str(exc_info.value)
    assert "Failed to ensure local state directory before preparing local cloudsave state" in message
    assert f"anchor_root={cm.anchor_root.resolve()}" in message
    assert "not a directory" in message


@pytest.mark.unit
def test_bootstrap_imports_legacy_root_after_seed_migration(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)
    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    legacy_config_dir = legacy_root / "config"
    legacy_memory_dir = legacy_root / "memory" / "旧角色"
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    legacy_memory_dir.mkdir(parents=True, exist_ok=True)

    legacy_characters = cm.get_default_characters()
    template_character = next(iter(legacy_characters["猫娘"].values()))
    legacy_characters["猫娘"] = {"旧角色": template_character}
    legacy_characters["当前猫娘"] = "旧角色"
    atomic_write_json(legacy_config_dir / "characters.json", legacy_characters, ensure_ascii=False, indent=2)
    atomic_write_json(legacy_config_dir / "user_preferences.json", [{"model_path": "/legacy.model3.json", "scale": {"x": 2, "y": 2}}], ensure_ascii=False, indent=2)
    atomic_write_json(legacy_config_dir / "voice_storage.json", {"legacy_bucket": {"voice_a": {"name": "旧音色"}}}, ensure_ascii=False, indent=2)
    atomic_write_json(
        legacy_config_dir / "workshop_config.json",
        {"default_workshop_folder": str(legacy_root / "workshop")},
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(legacy_config_dir / "core_config.json", {"recent_memory_auto_review": False}, ensure_ascii=False, indent=2)
    atomic_write_json(legacy_memory_dir / "recent.json", [{"role": "user", "content": "旧记忆"}], ensure_ascii=False, indent=2)
    (legacy_root / "live2d" / "legacy_model").mkdir(parents=True, exist_ok=True)
    atomic_write_json(legacy_root / "live2d" / "legacy_model" / "legacy_model.model3.json", {"Version": 3}, ensure_ascii=False, indent=2)

    cm.get_legacy_app_root_candidates = lambda: [legacy_root]

    # Simulate the real phase-0 startup order: ConfigManager seeds the new root first,
    # then bootstrap decides whether to import a historical runtime root.
    cm.migrate_config_files()
    cm.migrate_memory_files()

    assert (cm.config_dir / "characters.json").is_file()
    assert not cm.root_state_path.exists()
    assert cm.load_characters()["当前猫娘"] != "旧角色"

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["legacy_import"]["migrated"] is True
    assert result["legacy_import"]["source"] == str(legacy_root)
    assert result["legacy_import"]["result"] == "legacy_root_repaired_target"
    assert cm.load_characters()["当前猫娘"] == "旧角色"
    assert (Path(cm.memory_dir) / "旧角色" / "recent.json").is_file()
    assert Path(cm.get_config_path("user_preferences.json")).is_file()
    assert Path(cm.get_config_path("voice_storage.json")).is_file()
    assert Path(cm.get_config_path("workshop_config.json")).is_file()
    migrated_workshop_config = json.loads(Path(cm.get_config_path("workshop_config.json")).read_text(encoding="utf-8"))
    assert migrated_workshop_config["default_workshop_folder"] == str(cm.workshop_dir)
    assert Path(cm.get_config_path("core_config.json")).is_file()
    assert (cm.live2d_dir / "legacy_model" / "legacy_model.model3.json").is_file()
    assert cm.root_state_path.is_file()


@pytest.mark.unit
def test_bootstrap_repairs_seeded_target_when_legacy_root_only_adds_avatar_tools(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)
    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    tool_id = "local-12345678-1234-4123-8123-123456789abc"
    legacy_tool = legacy_root / "avatar_tools" / tool_id
    legacy_tool.mkdir(parents=True)
    (legacy_tool / "record.json").write_text('{"recordVersion":2}', encoding="utf-8")
    cm.get_legacy_app_root_candidates = lambda: [legacy_root]
    cm.migrate_config_files()
    cm.migrate_memory_files()
    atomic_write_json(
        Path(cm.get_config_path("user_preferences.json")),
        [{"model_path": "/custom.model3.json"}],
        ensure_ascii=False,
        indent=2,
    )

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["legacy_import"]["migrated"] is True
    assert result["legacy_import"]["repair_reason"] == "missing_avatar_tools"
    assert (cm.avatar_tools_dir / tool_id / "record.json").is_file()


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="Windows held-file replacement semantics")
def test_bootstrap_replaces_runtime_root_while_single_instance_lock_is_held(tmp_path, monkeypatch):
    from utils import single_instance
    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    local_app_data = tmp_path / "LocalAppData"
    legacy_root = tmp_path / "legacy" / "N.E.K.O"
    legacy_model = legacy_root / "live2d" / "legacy-model"
    legacy_model.mkdir(parents=True)
    (legacy_model / "legacy.model3.json").write_text('{"Version": 3}', encoding="utf-8")

    cm = _make_config_manager(local_app_data, legacy_candidates=[str(legacy_root)])
    monkeypatch.delenv(single_instance.RUNTIME_STATE_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("APPDATA", raising=False)

    handle = single_instance.acquire_single_instance(instance_id="bootstrap-regression")
    try:
        assert handle is not None
        assert handle.lock_file.parent == local_app_data / single_instance.RUNTIME_STATE_DIR_NAME
        assert cm.app_docs_dir not in handle.lock_file.parents

        result = bootstrap_local_cloudsave_environment(cm)
    finally:
        single_instance.release_single_instance()

    assert result["legacy_import"]["migrated"] is True
    assert (cm.live2d_dir / "legacy-model" / "legacy.model3.json").is_file()


@pytest.mark.unit
def test_bootstrap_preserves_staged_cloudsave_snapshot_before_legacy_runtime_import(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    snapshot_source_base = tmp_path / "snapshot_source"
    cm = _make_config_manager(new_root_base)
    snapshot_source_cm = _make_config_manager(snapshot_source_base)

    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment, export_local_cloudsave_snapshot

    legacy_config_dir = legacy_root / "config"
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    legacy_characters = cm.get_default_characters()
    template_character = next(iter(legacy_characters["猫娘"].values()))
    legacy_characters["猫娘"] = {"旧角色": template_character}
    legacy_characters["当前猫娘"] = "旧角色"
    atomic_write_json(legacy_config_dir / "characters.json", legacy_characters, ensure_ascii=False, indent=2)

    bootstrap_local_cloudsave_environment(snapshot_source_cm)
    _write_runtime_state(snapshot_source_cm, character_name="云端角色")
    export_local_cloudsave_snapshot(snapshot_source_cm)

    cm.get_legacy_app_root_candidates = lambda: [legacy_root]
    shutil.copytree(snapshot_source_cm.cloudsave_dir, cm.cloudsave_dir, dirs_exist_ok=True)

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["legacy_import"]["migrated"] is False
    assert result["legacy_import"]["result"] == "target_root_preserves_staged_cloudsave_snapshot"
    assert json.loads(cm.cloudsave_manifest_path.read_text(encoding="utf-8")).get("files")
    assert cm.load_characters()["当前猫娘"] != "旧角色"


@pytest.mark.unit
def test_bootstrap_repairs_existing_seeded_install_with_backup_and_merged_preferences(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)

    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    legacy_config_dir = legacy_root / "config"
    legacy_memory_dir = legacy_root / "memory" / "旧角色"
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    legacy_memory_dir.mkdir(parents=True, exist_ok=True)

    legacy_characters = cm.get_default_characters()
    template_character = next(iter(legacy_characters["猫娘"].values()))
    legacy_characters["猫娘"] = {"旧角色": template_character}
    legacy_characters["当前猫娘"] = "旧角色"
    atomic_write_json(legacy_config_dir / "characters.json", legacy_characters, ensure_ascii=False, indent=2)
    atomic_write_json(legacy_config_dir / "user_preferences.json", [{"model_path": "/legacy.model3.json", "position": {"x": 1, "y": 2}}], ensure_ascii=False, indent=2)
    atomic_write_json(legacy_config_dir / "voice_storage.json", {"legacy_bucket": {"voice_a": {"name": "旧音色"}}}, ensure_ascii=False, indent=2)
    atomic_write_json(legacy_memory_dir / "recent.json", [{"role": "user", "content": "旧记忆"}], ensure_ascii=False, indent=2)

    cm.get_legacy_app_root_candidates = lambda: [legacy_root]
    cm.migrate_config_files()
    cm.migrate_memory_files()
    cm.ensure_cloudsave_state_files()

    atomic_write_json(
        cm.config_dir / "user_preferences.json",
        [{"model_path": "/current.model3.json", "position": {"x": 9, "y": 9}}],
        ensure_ascii=False,
        indent=2,
    )
    pre_repair_characters = cm.load_characters()
    root_state = cm.load_root_state()
    root_state["last_migration_result"] = "launcher_phase0_bootstrap_ok"
    root_state["last_successful_boot_at"] = "2026-04-08T00:00:00Z"
    cm.save_root_state(root_state)

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["legacy_import"]["migrated"] is True
    assert result["legacy_import"]["result"] == "legacy_root_repaired_target"
    assert result["legacy_import"]["backup_path"]
    backup_path = Path(result["legacy_import"]["backup_path"])
    assert backup_path.is_dir()
    backup_characters = json.loads((backup_path / "config" / "characters.json").read_text(encoding="utf-8"))
    assert backup_characters["当前猫娘"] == pre_repair_characters["当前猫娘"]

    merged_characters = cm.load_characters()
    assert "旧角色" in merged_characters["猫娘"]

    merged_preferences = json.loads((cm.config_dir / "user_preferences.json").read_text(encoding="utf-8"))
    merged_model_paths = {entry.get("model_path") for entry in merged_preferences if isinstance(entry, dict)}
    assert {"/legacy.model3.json", "/current.model3.json"}.issubset(merged_model_paths)

    merged_voice_storage = json.loads((cm.config_dir / "voice_storage.json").read_text(encoding="utf-8"))
    assert "legacy_bucket" in merged_voice_storage


@pytest.mark.unit
def test_bootstrap_repairs_legacy_root_while_launcher_fence_is_active(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)

    from utils.cloudsave_runtime import ROOT_MODE_BOOTSTRAP_IMPORTING, bootstrap_local_cloudsave_environment, cloud_apply_fence

    legacy_config_dir = legacy_root / "config"
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    legacy_characters = cm.get_default_characters()
    template_character = next(iter(legacy_characters["猫娘"].values()))
    legacy_characters["猫娘"] = {"旧角色": template_character}
    legacy_characters["当前猫娘"] = "旧角色"
    atomic_write_json(legacy_config_dir / "characters.json", legacy_characters, ensure_ascii=False, indent=2)

    cm.get_legacy_app_root_candidates = lambda: [legacy_root]
    cm.migrate_config_files()
    cm.migrate_memory_files()

    with cloud_apply_fence(cm, mode=ROOT_MODE_BOOTSTRAP_IMPORTING, reason="launcher_phase0_bootstrap"):
        result = bootstrap_local_cloudsave_environment(cm)
        assert result["legacy_import"]["migrated"] is True
        assert result["root_state"]["mode"] == ROOT_MODE_BOOTSTRAP_IMPORTING

    assert cm.load_characters()["当前猫娘"] == "旧角色"


@pytest.mark.unit
def test_bootstrap_skips_legacy_repair_when_target_is_already_richer(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)

    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    legacy_config_dir = legacy_root / "config"
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    legacy_characters = cm.get_default_characters()
    template_character = next(iter(legacy_characters["猫娘"].values()))
    legacy_characters["猫娘"] = {"旧角色": template_character}
    legacy_characters["当前猫娘"] = "旧角色"
    atomic_write_json(legacy_config_dir / "characters.json", legacy_characters, ensure_ascii=False, indent=2)

    cm.get_legacy_app_root_candidates = lambda: [legacy_root]
    _write_runtime_state(cm, character_name="当前角色")
    cm.ensure_cloudsave_state_files()

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["legacy_import"]["migrated"] is False
    assert result["legacy_import"]["result"] == "target_root_already_initialized"
    assert cm.load_characters()["当前猫娘"] == "当前角色"


@pytest.mark.unit
def test_bootstrap_skips_legacy_character_merge_when_target_has_non_seeded_user_content(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_source_base = tmp_path / "legacy_source_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)
    legacy_cm = _make_config_manager(legacy_source_base)

    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    _write_runtime_state(cm, character_name="当前角色")
    _write_runtime_state(legacy_cm, character_name="旧角色")
    _add_runtime_character(legacy_cm, "旧角色二", recent_text="更多旧记忆")

    shutil.copytree(legacy_cm.app_docs_dir, legacy_root, dirs_exist_ok=True)
    cm.get_legacy_app_root_candidates = lambda: [legacy_root]

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["legacy_import"]["migrated"] is False
    assert result["legacy_import"]["result"] == "target_root_already_initialized"
    characters = cm.load_characters()
    assert set(characters["猫娘"]) == {"当前角色"}
    assert characters["当前猫娘"] == "当前角色"


@pytest.mark.unit
def test_bootstrap_does_not_reimport_same_legacy_root_after_local_deletion(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)

    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    legacy_config_dir = legacy_root / "config"
    legacy_memory_dir = legacy_root / "memory" / "旧角色"
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    legacy_memory_dir.mkdir(parents=True, exist_ok=True)

    legacy_characters = cm.get_default_characters()
    template_character = next(iter(legacy_characters["猫娘"].values()))
    legacy_characters["猫娘"] = {"旧角色": template_character}
    legacy_characters["当前猫娘"] = "旧角色"
    atomic_write_json(legacy_config_dir / "characters.json", legacy_characters, ensure_ascii=False, indent=2)
    atomic_write_json(legacy_memory_dir / "recent.json", [{"role": "user", "content": "旧记忆"}], ensure_ascii=False, indent=2)

    cm.get_legacy_app_root_candidates = lambda: [legacy_root]
    cm.migrate_config_files()
    cm.migrate_memory_files()

    first_result = bootstrap_local_cloudsave_environment(cm)
    assert first_result["legacy_import"]["migrated"] is True
    assert cm.load_characters()["当前猫娘"] == "旧角色"

    root_state = cm.load_root_state()
    root_state["last_successful_boot_at"] = "2026-04-08T00:00:00Z"
    cm.save_root_state(root_state)

    characters = cm.load_characters()
    characters["猫娘"] = {}
    characters["当前猫娘"] = ""
    cm.save_characters(characters, bypass_write_fence=True)

    second_result = bootstrap_local_cloudsave_environment(cm)

    assert second_result["legacy_import"]["migrated"] is False
    assert second_result["legacy_import"]["result"] == "target_root_already_initialized"
    assert cm.load_characters()["猫娘"] == {}
    assert cm.load_characters()["当前猫娘"] == ""


@pytest.mark.unit
def test_bootstrap_does_not_reimport_after_non_launcher_boot_success_marker(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)

    from utils.cloudsave_runtime import ROOT_MODE_NORMAL, bootstrap_local_cloudsave_environment, set_root_mode

    legacy_config_dir = legacy_root / "config"
    legacy_memory_dir = legacy_root / "memory" / "旧角色"
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    legacy_memory_dir.mkdir(parents=True, exist_ok=True)

    legacy_characters = cm.get_default_characters()
    template_character = next(iter(legacy_characters["猫娘"].values()))
    legacy_characters["猫娘"] = {"旧角色": template_character}
    legacy_characters["当前猫娘"] = "旧角色"
    atomic_write_json(legacy_config_dir / "characters.json", legacy_characters, ensure_ascii=False, indent=2)
    atomic_write_json(legacy_memory_dir / "recent.json", [{"role": "user", "content": "旧记忆"}], ensure_ascii=False, indent=2)

    cm.get_legacy_app_root_candidates = lambda: [legacy_root]
    cm.migrate_config_files()
    cm.migrate_memory_files()

    first_result = bootstrap_local_cloudsave_environment(cm)
    assert first_result["legacy_import"]["migrated"] is True

    set_root_mode(
        cm,
        ROOT_MODE_NORMAL,
        current_root=str(cm.app_docs_dir),
        last_known_good_root=str(cm.app_docs_dir),
        last_successful_boot_at="2026-04-08T00:00:00Z",
    )

    characters = cm.load_characters()
    characters["猫娘"] = {}
    characters["当前猫娘"] = ""
    cm.save_characters(characters, bypass_write_fence=True)

    second_result = bootstrap_local_cloudsave_environment(cm)

    assert second_result["legacy_import"]["migrated"] is False
    assert cm.load_characters()["猫娘"] == {}
    assert cm.load_characters()["当前猫娘"] == ""


@pytest.mark.unit
def test_legacy_repair_respects_local_tombstones_even_if_launcher_result_was_overwritten(tmp_path):
    new_root_base = tmp_path / "new_root_base"
    legacy_root = tmp_path / "legacy_docs" / "N.E.K.O"
    cm = _make_config_manager(new_root_base)

    from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment

    legacy_config_dir = legacy_root / "config"
    legacy_memory_dir = legacy_root / "memory" / "旧角色"
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    legacy_memory_dir.mkdir(parents=True, exist_ok=True)

    legacy_characters = cm.get_default_characters()
    template_character = next(iter(legacy_characters["猫娘"].values()))
    legacy_characters["猫娘"] = {"旧角色": template_character}
    legacy_characters["当前猫娘"] = "旧角色"
    atomic_write_json(legacy_config_dir / "characters.json", legacy_characters, ensure_ascii=False, indent=2)
    atomic_write_json(legacy_memory_dir / "recent.json", [{"role": "user", "content": "旧记忆"}], ensure_ascii=False, indent=2)

    cm.get_legacy_app_root_candidates = lambda: [legacy_root]
    cm.migrate_config_files()
    cm.migrate_memory_files()
    cm.ensure_cloudsave_state_files()

    root_state = cm.load_root_state()
    root_state["last_migration_result"] = "launcher_phase0_bootstrap_ok"
    root_state["last_successful_boot_at"] = "2026-04-08T00:00:00Z"
    cm.save_root_state(root_state)

    tombstones = cm.load_character_tombstones_state()
    tombstones["tombstones"] = [
        {
            "character_name": "旧角色",
            "deleted_at": "2026-04-08T00:00:00Z",
            "sequence_number": 5,
        }
    ]
    cm.save_character_tombstones_state(tombstones)

    characters = cm.load_characters()
    characters["猫娘"] = {}
    characters["当前猫娘"] = ""
    cm.save_characters(characters, bypass_write_fence=True)

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["legacy_import"]["migrated"] is True
    assert "旧角色" not in cm.load_characters()["猫娘"]
    assert cm.load_characters()["当前猫娘"] == ""


@pytest.mark.unit
def test_runtime_root_summary_ignores_dotfiles_in_memory(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import _runtime_root_has_user_content, _runtime_root_summary

    (cm.memory_dir).mkdir(parents=True, exist_ok=True)
    (Path(cm.memory_dir) / ".DS_Store").write_text("macOS metadata", encoding="utf-8")
    (cm.memory_dir / ".gitkeep").write_text("", encoding="utf-8")

    summary = _runtime_root_summary(cm, Path(cm.app_docs_dir))

    assert summary["memory_character_names"] == set()
    assert summary["has_user_content"] is False
    assert _runtime_root_has_user_content(Path(cm.app_docs_dir)) is False


@pytest.mark.unit
def test_runtime_root_detects_user_created_avatar_tools(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import _runtime_root_has_user_content

    tool_dir = Path(cm.app_docs_dir) / "avatar_tools" / "local-12345678-1234-4123-8123-123456789abc"
    tool_dir.mkdir(parents=True)
    (tool_dir / "record.json").write_text('{"recordVersion":2}', encoding="utf-8")

    assert _runtime_root_has_user_content(Path(cm.app_docs_dir)) is True


@pytest.mark.unit
def test_bootstrap_recovers_stale_blocking_mode(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import ROOT_MODE_BOOTSTRAP_IMPORTING, bootstrap_local_cloudsave_environment

    cm.ensure_cloudsave_state_files()
    root_state = cm.load_root_state()
    root_state["mode"] = ROOT_MODE_BOOTSTRAP_IMPORTING
    root_state["last_migration_result"] = "interrupted_import"
    cm.save_root_state(root_state)

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["root_state"]["mode"] == "normal"
    assert result["root_state"]["last_migration_result"] == f"recovered_stale_mode:{ROOT_MODE_BOOTSTRAP_IMPORTING}"


@pytest.mark.unit
def test_bootstrap_preserves_deferred_init_mode(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import ROOT_MODE_DEFERRED_INIT, bootstrap_local_cloudsave_environment

    cm.ensure_cloudsave_state_files()
    root_state = cm.load_root_state()
    unavailable_root = tmp_path / "offline-selected" / "N.E.K.O"
    root_state["mode"] = ROOT_MODE_DEFERRED_INIT
    root_state["current_root"] = str(unavailable_root)
    root_state["last_known_good_root"] = str(unavailable_root)
    root_state["last_migration_result"] = f"selected_root_unavailable:{unavailable_root}"
    cm.save_root_state(root_state)

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["root_state"]["mode"] == ROOT_MODE_DEFERRED_INIT
    assert result["root_state"]["current_root"] == str(unavailable_root)
    assert result["root_state"]["last_known_good_root"] == str(unavailable_root)
    assert result["root_state"]["last_migration_result"] == f"selected_root_unavailable:{unavailable_root}"


@pytest.mark.unit
def test_bootstrap_preserves_restart_pending_maintenance_mode(tmp_path):
    cm = _make_config_manager(tmp_path)
    anchor_base = tmp_path / "anchor-base"
    anchor_base.mkdir(parents=True, exist_ok=True)
    cm._get_standard_data_directory_candidates = lambda: [anchor_base]

    from utils.cloudsave_runtime import (
        ROOT_MODE_MAINTENANCE_READONLY,
        bootstrap_local_cloudsave_environment,
        set_root_mode,
    )
    from utils.storage_migration import create_pending_storage_migration

    create_pending_storage_migration(
        cm,
        source_root=cm.app_docs_dir,
        target_root=tmp_path / "target-root" / "N.E.K.O",
        selection_source="custom",
    )
    set_root_mode(
        cm,
        ROOT_MODE_MAINTENANCE_READONLY,
        last_migration_source=str(cm.app_docs_dir),
        last_migration_result=f"restart_pending:{tmp_path / 'target-root' / 'N.E.K.O'}",
    )

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["root_state"]["mode"] == ROOT_MODE_MAINTENANCE_READONLY
    assert result["root_state"]["last_migration_result"].startswith("restart_pending:")


@pytest.mark.unit
def test_write_blocking_recovery_fails_closed_when_migration_checkpoint_cannot_load(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils import cloudsave_runtime as cloudsave_runtime_module
    from utils.cloudsave_runtime import ROOT_MODE_MAINTENANCE_READONLY

    root_state = {
        "mode": ROOT_MODE_MAINTENANCE_READONLY,
        "last_migration_result": "restart_pending_missing_marker",
    }

    with patch("utils.storage_migration.load_storage_migration", side_effect=OSError("unreadable")):
        assert cloudsave_runtime_module._should_preserve_write_blocking_mode(cm, root_state) is True


@pytest.mark.unit
def test_bootstrap_heals_orphan_restart_pending_marker(tmp_path):
    """``restart_pending:`` marker 残留 + 没有真 pending 的 storage_migration.json
    时，bootstrap 必须把 mode 自愈回 normal——否则用户撞到 fire-and-forget
    shutdown / launcher 接力失败 / 强杀 等任一场景就会被永久钉在 readonly，
    memory server 所有写盘静默失败（见 time_indexed.db 不更新导致 gap 永远算成
    3 天以上的 bug 报告）。

    与 ``test_bootstrap_preserves_restart_pending_maintenance_mode`` 对偶：那个
    用例创建了真 pending 的 migration checkpoint，本用例只留 marker。
    """
    cm = _make_config_manager(tmp_path)
    anchor_base = tmp_path / "anchor-base"
    anchor_base.mkdir(parents=True, exist_ok=True)
    cm._get_standard_data_directory_candidates = lambda: [anchor_base]

    from utils.cloudsave_runtime import (
        ROOT_MODE_MAINTENANCE_READONLY,
        ROOT_MODE_NORMAL,
        bootstrap_local_cloudsave_environment,
        set_root_mode,
    )

    set_root_mode(
        cm,
        ROOT_MODE_MAINTENANCE_READONLY,
        last_migration_source=str(cm.app_docs_dir),
        last_migration_result=f"restart_pending:{tmp_path / 'orphan-target'}",
    )

    result = bootstrap_local_cloudsave_environment(cm)

    assert result["root_state"]["mode"] == ROOT_MODE_NORMAL
    # _recover_stale_write_blocking_mode 写入的标记，方便运维从日志/state 追溯
    assert result["root_state"]["last_migration_result"].startswith("recovered_stale_mode:")


@pytest.mark.unit
def test_should_write_root_mode_normal_after_startup_only_when_mode_is_normal():
    from utils.cloudsave_runtime import (
        ROOT_MODE_DEFERRED_INIT,
        ROOT_MODE_MAINTENANCE_READONLY,
        should_write_root_mode_normal_after_startup,
    )

    assert should_write_root_mode_normal_after_startup({"mode": "normal"}) is True
    assert should_write_root_mode_normal_after_startup({"mode": ROOT_MODE_DEFERRED_INIT}) is False
    assert should_write_root_mode_normal_after_startup({"mode": ROOT_MODE_MAINTENANCE_READONLY}) is False


@pytest.mark.unit
def test_bootstrap_does_not_clear_active_fence_in_same_process(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import (
        ROOT_MODE_BOOTSTRAP_IMPORTING,
        bootstrap_local_cloudsave_environment,
        cloud_apply_fence,
    )

    with cloud_apply_fence(cm, mode=ROOT_MODE_BOOTSTRAP_IMPORTING, reason="test_active_fence"):
        result = bootstrap_local_cloudsave_environment(cm)
        assert result["root_state"]["mode"] == ROOT_MODE_BOOTSTRAP_IMPORTING


@pytest.mark.unit
def test_cloud_apply_fence_blocks_core_writes(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import MaintenanceModeError, cloud_apply_fence
    import utils.preferences as preferences

    _write_runtime_state(cm)

    with patch.object(preferences, "_config_manager", cm), patch.object(
        preferences,
        "PREFERENCES_FILE",
        str(cm.get_config_path("user_preferences.json")),
    ):
        with cloud_apply_fence(cm):
            with pytest.raises(MaintenanceModeError):
                cm.save_characters({"猫娘": {}, "主人": {}, "当前猫娘": ""})
            with pytest.raises(MaintenanceModeError):
                cm.save_json_config("core_config.json", {"recent_memory_auto_review": False})
            with pytest.raises(MaintenanceModeError):
                cm.save_workshop_config({"default_workshop_folder": "/tmp/workshop", "auto_create_folder": True})
            with pytest.raises(MaintenanceModeError):
                preferences.save_global_conversation_settings({"userLanguage": "en-US"})


@pytest.mark.unit
def test_cloud_apply_fence_reports_local_state_directory_diagnostic(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.anchor_root.mkdir(parents=True, exist_ok=True)
    cm.local_state_dir.write_text("not a directory", encoding="utf-8")

    from utils.cloudsave_runtime import cloud_apply_fence
    from utils.config_manager import LocalStateDirectoryError

    with pytest.raises(LocalStateDirectoryError) as exc_info:
        with cloud_apply_fence(cm):
            pass

    message = str(exc_info.value)
    assert "Failed to ensure local state directory before entering cloud_apply_fence" in message
    assert f"local_state_dir={cm.local_state_dir.resolve()}" in message
    assert "not a directory" in message


@pytest.mark.unit
def test_cloud_apply_fence_reports_root_state_file_blocker(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.local_state_dir.mkdir(parents=True, exist_ok=True)
    cm.root_state_path.mkdir()

    from utils.cloudsave_runtime import cloud_apply_fence
    from utils.config_manager import LocalStateDirectoryError

    with pytest.raises(LocalStateDirectoryError) as exc_info:
        with cloud_apply_fence(cm):
            pass

    message = str(exc_info.value)
    assert "Failed to ensure local state file before loading root_state" in message
    assert f"failed_path={cm.root_state_path.resolve()}" in message
    assert "state file target exists but is not a file" in message


@pytest.mark.unit
def test_cloudsave_disabled_mode_disables_provider_and_write_fence(monkeypatch, tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.local_state_dir.mkdir(parents=True, exist_ok=True)
    cm.root_state_path.mkdir()

    from utils.cloudsave_runtime import (
        CLOUDSAVE_DISABLED_ENV,
        assert_cloudsave_writable,
        is_cloudsave_provider_available,
    )

    monkeypatch.setenv(CLOUDSAVE_DISABLED_ENV, "local_state_unavailable")

    assert is_cloudsave_provider_available(cm) is False
    assert_cloudsave_writable(cm, operation="save", target="characters.json")

    from utils.cloudsave_runtime import build_cloudsave_summary

    summary = build_cloudsave_summary(cm)
    assert summary["success"] is True
    assert summary["provider_available"] is False


@pytest.mark.unit
def test_non_local_state_cloudsave_disabled_reason_does_not_bypass_write_fence(monkeypatch, tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import (
        CLOUDSAVE_DISABLED_ENV,
        MaintenanceModeError,
        ROOT_MODE_MAINTENANCE_READONLY,
        assert_cloudsave_writable,
        set_root_mode,
    )

    set_root_mode(cm, ROOT_MODE_MAINTENANCE_READONLY)
    monkeypatch.setenv(CLOUDSAVE_DISABLED_ENV, "manual_disabled")

    with pytest.raises(MaintenanceModeError):
        assert_cloudsave_writable(cm, operation="save", target="characters.json")


@pytest.mark.unit
def test_cloud_apply_fence_releases_lock_when_mode_restore_fails(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils import cloudsave_runtime

    original_set_root_mode = cloudsave_runtime.set_root_mode
    call_count = 0

    def _flaky_set_root_mode(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("restore failed")
        return original_set_root_mode(*args, **kwargs)

    with patch.object(cloudsave_runtime, "set_root_mode", side_effect=_flaky_set_root_mode):
        with pytest.raises(RuntimeError, match="restore failed"):
            with cloudsave_runtime.cloud_apply_fence(cm):
                pass

    assert cloudsave_runtime.acquire_cloud_apply_lock(cm) is True
    cloudsave_runtime.release_cloud_apply_lock(cm)


@pytest.mark.unit
def test_cloud_apply_fence_does_not_restore_over_a_concurrent_storage_mode_write(tmp_path):
    """A storage worker's blocking mode must land after the cloud fence exits."""
    import threading

    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import (
        ROOT_MODE_MAINTENANCE_READONLY,
        cloud_apply_fence,
        get_root_mode,
        set_root_mode,
    )

    started = threading.Event()
    finished = threading.Event()

    def _storage_writer() -> None:
        started.set()
        set_root_mode(
            cm,
            ROOT_MODE_MAINTENANCE_READONLY,
            last_migration_result="restart_pending:test-target",
        )
        finished.set()

    with cloud_apply_fence(cm, reason="unit_test"):
        writer = threading.Thread(target=_storage_writer)
        writer.start()
        assert started.wait(5)
        assert not finished.wait(0.1), (
            "storage writer escaped the cloud fence lifecycle lock and can be "
            "overwritten by its stale mode restore"
        )

    writer.join(timeout=5)
    assert not writer.is_alive()
    assert finished.is_set()
    assert get_root_mode(cm) == ROOT_MODE_MAINTENANCE_READONLY


@pytest.mark.unit
def test_cloud_apply_fence_waits_for_writable_transaction(tmp_path):
    import threading

    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import (
        cloud_apply_fence,
        cloudsave_writable_transaction,
    )

    write_entered = threading.Event()
    release_write = threading.Event()
    fence_entered = threading.Event()
    errors = []

    def writer():
        try:
            with cloudsave_writable_transaction(
                cm,
                operation="save",
                target="prompt_locale.json",
            ):
                write_entered.set()
                assert release_write.wait(5)
        except Exception as exc:
            errors.append(exc)

    def fenced_restore():
        try:
            with cloud_apply_fence(cm):
                fence_entered.set()
        except Exception as exc:
            errors.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert write_entered.wait(5)

    fence_thread = threading.Thread(target=fenced_restore)
    fence_thread.start()
    assert not fence_entered.wait(0.1)

    release_write.set()
    writer_thread.join(5)
    fence_thread.join(5)

    assert errors == []
    assert not writer_thread.is_alive()
    assert not fence_thread.is_alive()
    assert fence_entered.is_set()


@pytest.mark.unit
def test_cloud_apply_fence_requests_a_blocking_cross_process_lock(tmp_path, monkeypatch):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import fence as fence_module

    blocking_modes: list[bool] = []

    def acquire(_config_manager, *, blocking=False):
        blocking_modes.append(blocking)
        return True

    monkeypatch.setattr(fence_module, "acquire_cloud_apply_lock", acquire)
    monkeypatch.setattr(fence_module, "release_cloud_apply_lock", lambda _cm: None)

    with fence_module.cloud_apply_fence(cm):
        pass

    assert blocking_modes == [True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_cloud_apply_fence_polls_without_blocking_event_loop(
    tmp_path,
    monkeypatch,
):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import fence as fence_module

    blocking_modes: list[bool] = []
    owner_threads: list[tuple[str, int]] = []
    attempts = iter((False, False, True))

    def acquire(_config_manager, *, blocking=False):
        blocking_modes.append(blocking)
        owner_threads.append(("acquire", threading.get_ident()))
        return next(attempts)

    def release(_config_manager):
        owner_threads.append(("release", threading.get_ident()))

    @contextlib.contextmanager
    def state(_config_manager, **_kwargs):
        yield {"mode": "maintenance_readonly"}

    sleeps: list[float] = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(fence_module, "acquire_cloud_apply_lock", acquire)
    monkeypatch.setattr(fence_module, "release_cloud_apply_lock", release)
    monkeypatch.setattr(fence_module, "_cloud_apply_fence_state", state)
    monkeypatch.setattr(fence_module.asyncio, "sleep", sleep)

    async with fence_module.async_cloud_apply_fence(cm, poll_interval=0.01):
        pass

    assert blocking_modes == [False, False, False]
    assert sleeps == [0.01, 0.01]
    assert owner_threads[-1][0] == "release"
    assert {thread_id for _, thread_id in owner_threads} == {threading.get_ident()}


@pytest.mark.unit
def test_win32_mutex_apis_use_pointer_sized_handle_signatures():
    import ctypes

    from utils.cloudsave_runtime import fence as fence_module

    class Function:
        argtypes = None
        restype = None

    class Kernel32:
        CreateMutexW = Function()
        WaitForSingleObject = Function()
        ReleaseMutex = Function()
        CloseHandle = Function()

    kernel32 = Kernel32()
    fence_module._configure_win32_mutex_apis(kernel32)

    assert kernel32.CreateMutexW.restype is ctypes.c_void_p
    assert kernel32.WaitForSingleObject.argtypes[0] is ctypes.c_void_p
    assert kernel32.ReleaseMutex.argtypes == [ctypes.c_void_p]
    assert kernel32.CloseHandle.argtypes == [ctypes.c_void_p]


@pytest.mark.unit
def test_local_cloudsave_round_trip_restores_runtime_truth(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot
    from utils import recent_file

    expected_characters = _write_runtime_state(cm)

    export_result = export_local_cloudsave_snapshot(cm)
    assert export_result["manifest"]["sequence_number"] == 1
    assert export_result["manifest"]["schema_version"] == 2
    assert export_result["manifest"]["min_reader_schema_version"] == 2
    assert export_result["manifest"]["snapshot_kind"] == "full_runtime"
    assert (cm.cloudsave_dir / "profiles" / "characters.json").is_file()
    assert (cm.cloudsave_dir / "memory" / "小满" / "recent.json").is_file()
    assert (cm.cloudsave_dir / "bindings" / "小满.json").is_file()
    assert (cm.cloudsave_dir / "catalog" / "character_tombstones.json").is_file()

    binding_payload = json.loads((cm.cloudsave_dir / "bindings" / "小满.json").read_text(encoding="utf-8"))
    assert binding_payload["model_type"] == "live2d"
    assert binding_payload["asset_source"] == "steam_workshop"
    assert binding_payload["asset_source_id"] == "123456"
    assert binding_payload["asset_state"] == "ready"
    assert binding_payload["experience_overrides"]["touch_set"]["default"]["tap"] == "wave"

    catalog_payload = json.loads((cm.cloudsave_dir / "catalog" / "catgirls_index.json").read_text(encoding="utf-8"))
    assert catalog_payload["characters"][0]["character_name"] == "小满"
    assert catalog_payload["characters"][0]["entry_sequence_number"] == 1

    shutil_targets = [
        cm.get_config_path("characters.json"),
        cm.get_config_path("user_preferences.json"),
    ]
    for target in shutil_targets:
        path = Path(target)
        if path.exists():
            path.unlink()
    if Path(cm.memory_dir).exists():
        import shutil
        shutil.rmtree(cm.memory_dir)

    restored_recent = Path(cm.memory_dir) / "小满" / "recent.json"
    from utils.llm_client import HumanMessage

    with recent_file.recent_file_lock(restored_recent):
        recent_file.set_recent_pending_unlocked(
            restored_recent, [HumanMessage(content="stale-before-cloud-import")],
        )

    import_result = import_local_cloudsave_snapshot(cm)

    assert import_result["applied_character_count"] == 1
    assert cm.load_characters() == expected_characters

    with open(cm.get_config_path("user_preferences.json"), "r", encoding="utf-8") as file_obj:
        preferences = file_obj.read()
    assert "__global_conversation__" in preferences
    assert "noiseReductionEnabled" in preferences

    restored_db = Path(cm.memory_dir) / "小满" / "time_indexed.db"
    assert restored_recent.is_file()
    assert recent_file.get_recent_pending(restored_recent) == []
    assert restored_db.read_bytes() == b"sqlite-placeholder"

    cloud_state = cm.load_cloudsave_local_state()
    assert cloud_state["next_sequence_number"] == 2
    assert cloud_state["last_applied_manifest_fingerprint"] == export_result["manifest"]["fingerprint"]
    assert cloud_state["last_successful_import_at"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("manifest_update", "error_match"),
    [
        ({"snapshot_kind": "character_collection"}, "fingerprint mismatch"),
        ({"fingerprint": ""}, "fingerprint is required"),
    ],
    ids=["kind_tamper", "missing_fingerprint"],
)
def test_schema_two_manifest_binds_snapshot_kind_to_fingerprint(
    tmp_path, manifest_update, error_match,
):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot

    _write_runtime_state(cm)
    export_local_cloudsave_snapshot(cm)
    manifest = json.loads(cm.cloudsave_manifest_path.read_text(encoding="utf-8"))
    manifest.update(manifest_update)
    atomic_write_json(
        cm.cloudsave_manifest_path,
        manifest,
        ensure_ascii=False,
        indent=2,
    )

    with pytest.raises(ValueError, match=error_match):
        import_local_cloudsave_snapshot(cm)


@pytest.mark.unit
def test_local_cloudsave_import_failure_restores_recent_redirects(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils import cloudsave_runtime, recent_file

    _write_runtime_state(cm, character_name="B")
    cloudsave_runtime.export_local_cloudsave_snapshot(cm)
    old_alias = Path(cm.memory_dir) / "A" / "recent.json"
    current_path = Path(cm.memory_dir) / "B" / "recent.json"
    recent_file.redirect_recent_paths([old_alias], current_path)
    original_apply = cloudsave_runtime._apply_runtime_file

    def _fail_preferences(source_path, target_path):
        if Path(target_path).name == "user_preferences.json":
            raise RuntimeError("simulated runtime apply failure")
        return original_apply(source_path, target_path)

    with patch.object(
        cloudsave_runtime,
        "_apply_runtime_file",
        side_effect=_fail_preferences,
    ):
        with pytest.raises(RuntimeError, match="simulated runtime apply failure"):
            cloudsave_runtime.import_local_cloudsave_snapshot(cm)

    assert recent_file._resolve_key_unlocked(recent_file._lock_key(old_alias)) == (
        recent_file._lock_key(current_path)
    )


@pytest.mark.unit
def test_local_cloudsave_import_locks_recent_before_rollback_backup(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils import cloudsave_runtime, recent_file

    _write_runtime_state(cm, character_name="B")
    cloudsave_runtime.export_local_cloudsave_snapshot(cm)
    current_path = Path(cm.memory_dir) / "B" / "recent.json"
    writer_entered = threading.Event()
    writer = None
    real_copy2 = shutil.copy2

    def _probe_copy(source_path, target_path, *args, **kwargs):
        nonlocal writer
        if Path(source_path) == current_path and writer is None:
            def _wait_for_recent_lock():
                with recent_file.recent_file_access(current_path):
                    writer_entered.set()

            writer = threading.Thread(target=_wait_for_recent_lock)
            writer.start()
            time.sleep(0.05)
            assert not writer_entered.is_set()
        return real_copy2(source_path, target_path, *args, **kwargs)

    with patch.object(shutil, "copy2", side_effect=_probe_copy):
        cloudsave_runtime.import_local_cloudsave_snapshot(cm)

    assert writer is not None
    writer.join(3)
    assert not writer.is_alive()
    assert writer_entered.is_set()


@pytest.mark.unit
def test_local_cloudsave_import_rejects_writer_waiting_before_activation(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.project_memory_dir = tmp_path / "project-memory"

    from utils import cloudsave_runtime, recent_file
    from utils.cloudsave_runtime import operations

    _write_runtime_state(cm, character_name="B")
    cloudsave_runtime.export_local_cloudsave_snapshot(cm)
    current_path = Path(cm.memory_dir) / "B" / "recent.json"
    cloud_payload = json.loads(current_path.read_text(encoding="utf-8"))
    atomic_write_json(current_path, [{"content": "local-before-import"}])

    writer_attempting = threading.Event()
    writer_errors = []
    writer = None
    activated_paths = set()
    real_access = recent_file.recent_file_access
    real_activate = operations.activate_recent_paths

    @contextlib.contextmanager
    def _probe_access(path, *, expected_generation=None):
        if threading.current_thread() is writer:
            writer_attempting.set()
        with real_access(
            path, expected_generation=expected_generation,
        ) as resolved_path:
            yield resolved_path

    def _write_stale_batch():
        try:
            recent_file.write_recent_payload(
                current_path,
                [{"content": "stale-writer"}],
            )
        except Exception as exc:
            writer_errors.append(exc)

    def _activate_with_waiting_writer(paths):
        nonlocal writer
        activated_paths.update(Path(path) for path in paths)
        writer = threading.Thread(target=_write_stale_batch)
        writer.start()
        assert writer_attempting.wait(3)
        return real_activate(paths)

    with patch.object(recent_file, "recent_file_access", _probe_access), patch.object(
        operations,
        "activate_recent_paths",
        side_effect=_activate_with_waiting_writer,
    ):
        cloudsave_runtime.import_local_cloudsave_snapshot(cm)

    assert writer is not None
    writer.join(3)
    assert not writer.is_alive()
    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], recent_file.RecentFileDeletedError)
    assert json.loads(current_path.read_text(encoding="utf-8")) == cloud_payload
    from utils.character_memory import list_character_recent_paths
    assert set(list_character_recent_paths(cm, "B")) <= activated_paths


def _tamper_manifest_with_memory_key(cm, hostile_key: str, placement_relative_path: str) -> None:
    placement_path = cm.cloudsave_dir / placement_relative_path
    placement_path.parent.mkdir(parents=True, exist_ok=True)
    placement_path.write_text("{}", encoding="utf-8")

    manifest_path = Path(cm.cloudsave_manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][hostile_key] = {"sha256": "0" * 64, "size": 2}
    # 攻击场景里 manifest 由存档作者产出，fingerprint 留空即可跳过一致性校验，
    # 因此旧版路径约束不能依赖 fingerprint 这道闸。
    manifest["schema_version"] = 1
    manifest["min_reader_schema_version"] = 1
    manifest["fingerprint"] = ""
    atomic_write_json(manifest_path, manifest, ensure_ascii=False, indent=2)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hostile_key", "placement_relative_path", "error_match"),
    [
        # parts 恰好三段的 '..' 穿越：character_name 解析成 '..'，
        # runtime 目标会落到 memory_dir 的上一级
        ("memory/../escape.json", "escape.json", "unsupported cloudsave memory path"),
        # 白名单外的叶子文件名
        ("memory/小满/evil.bin", "memory/小满/evil.bin", "unsupported cloudsave memory path"),
        # 角色名过不了 audit（前导空格）
        ("memory/ 小满/recent.json", "memory/ 小满/recent.json", "character name audit failed"),
    ],
)
def test_import_rejects_hostile_memory_manifest_keys(tmp_path, hostile_key, placement_relative_path, error_match):
    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot

    cm = _make_config_manager(tmp_path)
    _write_runtime_state(cm)
    export_local_cloudsave_snapshot(cm)
    _tamper_manifest_with_memory_key(cm, hostile_key, placement_relative_path)

    with pytest.raises(ValueError, match=error_match):
        import_local_cloudsave_snapshot(cm)

    assert not (Path(cm.memory_dir).parent / "escape.json").exists()


@pytest.mark.unit
def test_cloudsave_summary_marks_exported_character_as_matched(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import (
        build_cloudsave_character_detail,
        build_cloudsave_summary,
        export_local_cloudsave_snapshot,
    )

    _write_runtime_state(cm, character_name="小满")
    export_local_cloudsave_snapshot(cm)

    summary = build_cloudsave_summary(cm)

    assert summary["success"] is True
    assert summary["provider_available"] is True
    assert summary["current_character_name"] == "小满"
    assert len(summary["items"]) == 1
    assert summary["items"][0]["character_name"] == "小满"
    assert summary["items"][0]["relation_state"] == "matched"
    assert summary["items"][0]["available_actions"] == []

    detail = build_cloudsave_character_detail(cm, "小满")
    assert detail is not None
    assert detail["item"]["relation_state"] == "matched"


@pytest.mark.unit
def test_cloudsave_summary_marks_exported_character_as_matched_with_live_sqlite_memory_db(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary, export_cloudsave_character_unit

    _write_runtime_state(cm, character_name="小满")
    db_path = Path(cm.memory_dir) / "小满" / "time_indexed.db"
    db_path.unlink(missing_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE memory_events (id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO memory_events (content) VALUES (?)", ("first",))
        conn.commit()
        conn.execute("INSERT INTO memory_events (content) VALUES (?)", ("second",))
        conn.commit()

        export_cloudsave_character_unit(cm, "小满")
        summary = build_cloudsave_summary(cm)
    finally:
        conn.close()

    assert summary["items"][0]["character_name"] == "小满"
    assert summary["items"][0]["relation_state"] == "matched"


@pytest.mark.unit
def test_cloudsave_summary_returns_empty_items_without_characters(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary

    cm.save_characters({"猫娘": {}, "主人": {}, "当前猫娘": ""}, bypass_write_fence=True)
    summary = build_cloudsave_summary(cm)

    assert summary["success"] is True
    assert summary["provider_available"] is True
    assert summary["current_character_name"] == ""
    assert summary["items"] == []


@pytest.mark.unit
def test_cloudsave_summary_classifies_local_cloud_and_diverged_states(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import build_cloudsave_summary, export_local_cloudsave_snapshot

    _write_runtime_state(source_cm, character_name="共同角色")
    _add_runtime_character(source_cm, "云端独有", recent_text="cloud-only-memory")
    export_local_cloudsave_snapshot(source_cm)

    _write_runtime_state(target_cm, character_name="共同角色")
    common_recent_path = Path(target_cm.memory_dir) / "共同角色" / "recent.json"
    atomic_write_json(
        common_recent_path,
        [{"role": "user", "content": "target-diverged-memory"}],
        ensure_ascii=False,
        indent=2,
    )
    _add_runtime_character(target_cm, "本地独有", recent_text="local-only-memory")
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    summary = build_cloudsave_summary(target_cm)
    items_by_name = {item["character_name"]: item for item in summary["items"]}

    assert items_by_name["共同角色"]["relation_state"] == "diverged"
    assert items_by_name["共同角色"]["available_actions"] == ["upload", "download"]
    assert items_by_name["本地独有"]["relation_state"] == "local_only"
    assert items_by_name["本地独有"]["available_actions"] == ["upload"]
    assert items_by_name["云端独有"]["relation_state"] == "cloud_only"
    assert items_by_name["云端独有"]["available_actions"] == ["download"]


@pytest.mark.unit
def test_cloudsave_summary_merges_legacy_and_sharded_cloud_characters(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary, export_cloudsave_character_unit

    _write_runtime_state(cm, character_name="新角色")
    legacy_payload = copy.deepcopy(cm.load_characters()["猫娘"]["新角色"])
    legacy_payload["档案名"] = "旧角色"
    atomic_write_json(
        cm.cloudsave_profiles_dir / "characters.json",
        {"猫娘": {"旧角色": legacy_payload}},
        ensure_ascii=False,
        indent=2,
    )

    export_cloudsave_character_unit(cm, "新角色")

    summary = build_cloudsave_summary(cm)
    items_by_name = {item["character_name"]: item for item in summary["items"]}

    assert items_by_name["旧角色"]["relation_state"] == "cloud_only"
    assert items_by_name["旧角色"]["cloud_exists"] is True
    assert items_by_name["新角色"]["relation_state"] == "matched"
    assert items_by_name["新角色"]["cloud_exists"] is True


@pytest.mark.unit
def test_cloudsave_summary_prefers_sharded_binding_payload_over_stale_legacy_binding(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary, export_cloudsave_character_unit

    _write_runtime_state(cm, character_name="小满")
    export_cloudsave_character_unit(cm, "小满")

    stale_binding_path = cm.cloudsave_bindings_dir / "小满.json"
    stale_binding_payload = json.loads(stale_binding_path.read_text(encoding="utf-8"))
    stale_binding_payload["model_ref"] = "stale/stale.model3.json"
    stale_binding_payload["asset_source"] = "local_imported"
    stale_binding_payload["asset_source_id"] = ""
    atomic_write_json(stale_binding_path, stale_binding_payload, ensure_ascii=False, indent=2)

    summary = build_cloudsave_summary(cm)

    assert summary["items"][0]["character_name"] == "小满"
    assert summary["items"][0]["relation_state"] == "matched"


@pytest.mark.unit
def test_cloudsave_summary_prefers_sharded_memory_over_stale_legacy_memory(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary, export_cloudsave_character_unit

    _write_runtime_state(cm, character_name="小满")
    export_cloudsave_character_unit(cm, "小满")

    stale_recent_path = cm.cloudsave_memory_dir / "小满" / "recent.json"
    atomic_write_json(
        stale_recent_path,
        [{"role": "user", "content": "stale-legacy-memory"}],
        ensure_ascii=False,
        indent=2,
    )

    summary = build_cloudsave_summary(cm)

    assert summary["items"][0]["character_name"] == "小满"
    assert summary["items"][0]["relation_state"] == "matched"


@pytest.mark.unit
def test_cloudsave_summary_uses_configured_workshop_root_for_local_asset_resolution(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary

    _write_runtime_state(cm, character_name="小满")

    default_workshop_model_dir = Path(cm.workshop_dir) / "123456" / "example"
    shutil.rmtree(default_workshop_model_dir.parent, ignore_errors=True)

    custom_workshop_root = tmp_path / "external_workshop_root"
    custom_workshop_model_dir = custom_workshop_root / "123456" / "example"
    custom_workshop_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        custom_workshop_model_dir / "example.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )
    cm.save_workshop_path(str(custom_workshop_root))

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]

    assert item["local_asset_state"] == "ready"
    assert item["warnings"] == []


@pytest.mark.unit
def test_cloudsave_summary_resolves_workshop_model_from_item_scan_when_stored_path_is_stale(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary
    from utils.config_manager import set_reserved

    _write_runtime_state(cm, character_name="Tian")

    characters = cm.load_characters()
    set_reserved(characters["猫娘"]["Tian"], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"]["Tian"], "avatar", "asset_source", "steam_workshop")
    set_reserved(characters["猫娘"]["Tian"], "avatar", "asset_source_id", "123456")
    set_reserved(characters["猫娘"]["Tian"], "avatar", "live2d", "model_path", "legacy/legacy.model3.json")
    cm.save_characters(characters, bypass_write_fence=True)

    legacy_item_root = Path(cm.workshop_dir) / "123456"
    shutil.rmtree(legacy_item_root, ignore_errors=True)
    actual_model_dir = legacy_item_root / "current-layout" / "tian"
    actual_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        actual_model_dir / "tian.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]

    assert item["character_name"] == "Tian"
    assert item["local_asset_state"] == "ready"
    assert item["warnings"] == []


@pytest.mark.unit
def test_cloudsave_summary_resolves_stale_local_live2d_filename_from_existing_folder(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary
    from utils.config_manager import set_reserved

    _write_runtime_state(cm, character_name="水水")

    characters = cm.load_characters()
    set_reserved(characters["猫娘"]["水水"], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"]["水水"], "avatar", "asset_source", "local")
    set_reserved(characters["猫娘"]["水水"], "avatar", "asset_source_id", "")
    set_reserved(characters["猫娘"]["水水"], "avatar", "live2d", "model_path", "yui-export/yui-export.model3.json")
    cm.save_characters(characters, bypass_write_fence=True)

    actual_model_dir = Path(cm.live2d_dir) / "yui-export"
    actual_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        actual_model_dir / "0313YUI03.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]

    assert item["character_name"] == "水水"
    assert item["local_asset_state"] == "ready"
    assert item["local_asset_source"] == "local_imported"
    assert item["local_asset_source_id"] == ""
    assert item["warnings"] == []


@pytest.mark.unit
def test_cloudsave_summary_infers_workshop_source_from_resolved_workshop_file_when_metadata_is_stale(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary
    from utils.config_manager import set_reserved

    _write_runtime_state(cm, character_name="工坊角色")

    characters = cm.load_characters()
    set_reserved(characters["猫娘"]["工坊角色"], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"]["工坊角色"], "avatar", "asset_source", "local")
    set_reserved(characters["猫娘"]["工坊角色"], "avatar", "asset_source_id", "")
    set_reserved(characters["猫娘"]["工坊角色"], "avatar", "live2d", "model_path", "Blue cat/Blue cat.model3.json")
    cm.save_characters(characters, bypass_write_fence=True)

    workshop_model_dir = Path(cm.workshop_dir) / "3671939765" / "Blue cat"
    workshop_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        workshop_model_dir / "Blue cat 2.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]

    assert item["character_name"] == "工坊角色"
    assert item["local_asset_state"] == "ready"
    assert item["local_asset_source"] == "steam_workshop"
    assert item["local_asset_source_id"] == "3671939765"
    assert item["warnings"] == []


@pytest.mark.unit
def test_cloudsave_summary_preserves_explicit_workshop_role_origin_even_when_current_model_is_local(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary
    from utils.config_manager import set_reserved

    _write_runtime_state(cm, character_name="水水")

    characters = cm.load_characters()
    set_reserved(characters["猫娘"]["水水"], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"]["水水"], "avatar", "asset_source", "local")
    set_reserved(characters["猫娘"]["水水"], "avatar", "asset_source_id", "")
    set_reserved(characters["猫娘"]["水水"], "avatar", "live2d", "model_path", "猫娘-YUI-洛丽塔-导出03/0313YUI03.model3.json")
    set_reserved(characters["猫娘"]["水水"], "character_origin", "source", "steam_workshop")
    set_reserved(characters["猫娘"]["水水"], "character_origin", "source_id", "3671939765")
    set_reserved(characters["猫娘"]["水水"], "character_origin", "display_name", "Blue cat")
    set_reserved(
        characters["猫娘"]["水水"],
        "character_origin",
        "model_ref",
        "Blue cat/Blue cat.model3.json",
    )
    cm.save_characters(characters, bypass_write_fence=True)

    local_model_dir = Path(cm.live2d_dir) / "猫娘-YUI-洛丽塔-导出03"
    local_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        local_model_dir / "0313YUI03.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]

    assert item["character_name"] == "水水"
    assert item["local_asset_state"] == "ready"
    assert item["local_asset_source"] == "local_imported"
    assert item["local_asset_source_id"] == ""
    assert item["local_origin_source"] == "steam_workshop"
    assert item["local_origin_source_id"] == "3671939765"
    assert item["local_origin_display_name"] == "Blue cat"
    assert item["warnings"] == []


@pytest.mark.unit
def test_cloudsave_summary_backfills_workshop_role_origin_only_when_profile_payload_matches(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary
    from utils.config_manager import set_reserved

    characters = cm.get_default_characters()
    characters["猫娘"] = {
        "工坊旧角色": {
            "昵称": "海盐",
            "口头禅": "今天也要加油",
        }
    }
    characters["当前猫娘"] = "工坊旧角色"
    set_reserved(characters["猫娘"]["工坊旧角色"], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"]["工坊旧角色"], "avatar", "asset_source", "local")
    set_reserved(characters["猫娘"]["工坊旧角色"], "avatar", "asset_source_id", "")
    set_reserved(characters["猫娘"]["工坊旧角色"], "avatar", "live2d", "model_path", "manual/manual.model3.json")
    cm.save_characters(characters, bypass_write_fence=True)

    local_model_dir = Path(cm.live2d_dir) / "manual"
    local_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        local_model_dir / "manual.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )

    workshop_item_root = Path(cm.workshop_dir) / "3671939765"
    workshop_item_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        workshop_item_root / "工坊旧角色.chara.json",
        {
            "档案名": "工坊旧角色",
            "昵称": "海盐",
            "口头禅": "今天也要加油",
            "model_type": "live2d",
            "live2d": "Blue cat",
        },
        ensure_ascii=False,
        indent=2,
    )

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]

    assert item["local_asset_source"] == "local_imported"
    assert item["local_origin_source"] == "steam_workshop"
    assert item["local_origin_source_id"] == "3671939765"
    assert item["local_origin_display_name"] == "Blue cat"


@pytest.mark.unit
def test_cloudsave_summary_does_not_backfill_workshop_role_origin_from_name_only_match(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary
    from utils.config_manager import set_reserved

    _write_runtime_state(cm, character_name="水水")

    characters = cm.load_characters()
    characters["猫娘"]["水水"]["昵称"] = "本地创建"
    set_reserved(characters["猫娘"]["水水"], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"]["水水"], "avatar", "asset_source", "local")
    set_reserved(characters["猫娘"]["水水"], "avatar", "asset_source_id", "")
    set_reserved(characters["猫娘"]["水水"], "avatar", "live2d", "model_path", "猫娘-YUI-洛丽塔-导出03/猫娘-YUI-洛丽塔-导出03.model3.json")
    cm.save_characters(characters, bypass_write_fence=True)

    local_model_dir = Path(cm.live2d_dir) / "猫娘-YUI-洛丽塔-导出03"
    local_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        local_model_dir / "0313YUI03.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )

    workshop_item_root = Path(cm.workshop_dir) / "3671939765"
    workshop_item_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        workshop_item_root / "水水.chara.json",
        {
            "档案名": "水水",
            "昵称": "来自工坊",
            "model_type": "live2d",
            "live2d": "Blue cat",
        },
        ensure_ascii=False,
        indent=2,
    )

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]

    assert item["local_asset_state"] == "ready"
    assert item["local_asset_source"] == "local_imported"
    assert item["local_origin_source"] == ""
    assert item["local_origin_source_id"] == ""
    assert item["local_origin_display_name"] == ""


@pytest.mark.unit
def test_cloudsave_summary_does_not_treat_workshop_model_binding_as_workshop_role_origin(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary

    _write_runtime_state(cm, character_name="普通角色")

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]

    assert item["local_asset_state"] == "ready"
    assert item["local_asset_source"] == "steam_workshop"
    assert item["local_asset_source_id"] == "123456"
    assert item["local_origin_source"] == ""
    assert item["local_origin_source_id"] == ""


@pytest.mark.unit
def test_cloudsave_summary_keeps_missing_relative_local_model_as_local_imported(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary, export_cloudsave_character_unit
    from utils.config_manager import set_reserved

    _write_runtime_state(cm, character_name="缺资源本地导入")

    characters = cm.load_characters()
    set_reserved(characters["猫娘"]["缺资源本地导入"], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"]["缺资源本地导入"], "avatar", "asset_source", "local")
    set_reserved(characters["猫娘"]["缺资源本地导入"], "avatar", "asset_source_id", "")
    set_reserved(
        characters["猫娘"]["缺资源本地导入"],
        "avatar",
        "live2d",
        "model_path",
        "missing-local/missing-local.model3.json",
    )
    cm.save_characters(characters, bypass_write_fence=True)

    summary = build_cloudsave_summary(cm)
    item = summary["items"][0]
    assert item["local_asset_source"] == "local_imported"
    assert item["local_asset_state"] == "import_required"
    assert item["warnings"] == ["local_resource_missing_on_this_device"]

    export_cloudsave_character_unit(cm, "缺资源本地导入")
    binding_payload = json.loads(
        (cm.cloudsave_dir / "characters" / "缺资源本地导入" / "binding.json").read_text(encoding="utf-8")
    )
    assert binding_payload["asset_source"] == "local_imported"
    assert binding_payload["asset_state"] == "import_required"


@pytest.mark.unit
def test_cloudsave_summary_prefers_local_warnings_for_existing_character(tmp_path):
    from utils import cloudsave_runtime

    local_summary = {
        "character_name": "Tian",
        "display_name": "Tian",
        "model_type": "live2d",
        "asset_source": "steam_workshop",
        "asset_source_id": "123456",
        "asset_state": "ready",
        "updated_at_utc": "2026-04-09T00:00:00Z",
        "fingerprint": "sha256:local",
        "warnings": [],
    }
    cloud_summary = {
        "character_name": "Tian",
        "display_name": "Tian",
        "model_type": "live2d",
        "asset_source": "steam_workshop",
        "asset_source_id": "123456",
        "asset_state": "downloadable",
        "updated_at_utc": "2026-04-09T00:00:00Z",
        "fingerprint": "sha256:cloud",
        "warnings": ["cloud_resource_may_be_missing_after_download"],
    }

    item = cloudsave_runtime._merge_character_summary_item(
        character_name="Tian",
        local_summary=local_summary,
        cloud_summary=cloud_summary,
    )

    assert item["relation_state"] == "diverged"
    assert item["warnings"] == []


@pytest.mark.unit
def test_cloudsave_summary_keeps_cloud_warning_for_cloud_only_character(tmp_path):
    from utils import cloudsave_runtime

    cloud_summary = {
        "character_name": "云端角色",
        "display_name": "云端角色",
        "model_type": "live2d",
        "asset_source": "steam_workshop",
        "asset_source_id": "123456",
        "asset_state": "downloadable",
        "updated_at_utc": "2026-04-09T00:00:00Z",
        "fingerprint": "sha256:cloud",
        "warnings": ["cloud_resource_may_be_missing_after_download"],
    }

    item = cloudsave_runtime._merge_character_summary_item(
        character_name="云端角色",
        local_summary=None,
        cloud_summary=cloud_summary,
    )

    assert item["relation_state"] == "cloud_only"
    assert item["warnings"] == ["cloud_resource_may_be_missing_after_download"]


@pytest.mark.unit
def test_cloudsave_summary_keeps_local_warning_for_local_existing_character(tmp_path):
    from utils import cloudsave_runtime

    local_summary = {
        "character_name": "本地角色",
        "display_name": "本地角色",
        "model_type": "live2d",
        "asset_source": "local_imported",
        "asset_source_id": "",
        "asset_state": "import_required",
        "updated_at_utc": "2026-04-09T00:00:00Z",
        "fingerprint": "sha256:local",
        "warnings": ["local_resource_missing_on_this_device"],
    }

    item = cloudsave_runtime._merge_character_summary_item(
        character_name="本地角色",
        local_summary=local_summary,
        cloud_summary=None,
    )

    assert item["relation_state"] == "local_only"
    assert item["warnings"] == ["local_resource_missing_on_this_device"]


@pytest.mark.unit
def test_cloudsave_summary_preserves_separate_local_and_cloud_asset_sources(tmp_path):
    from utils import cloudsave_runtime

    local_summary = {
        "character_name": "共享角色",
        "display_name": "共享角色",
        "model_type": "live2d",
        "asset_source": "local_imported",
        "asset_source_id": "",
        "asset_state": "ready",
        "updated_at_utc": "2026-04-09T00:00:00Z",
        "fingerprint": "sha256:local",
        "warnings": [],
    }
    cloud_summary = {
        "character_name": "共享角色",
        "display_name": "共享角色",
        "model_type": "live2d",
        "asset_source": "steam_workshop",
        "asset_source_id": "123456",
        "asset_state": "downloadable",
        "updated_at_utc": "2026-04-09T01:00:00Z",
        "fingerprint": "sha256:cloud",
        "warnings": ["cloud_resource_may_be_missing_after_download"],
    }

    item = cloudsave_runtime._merge_character_summary_item(
        character_name="共享角色",
        local_summary=local_summary,
        cloud_summary=cloud_summary,
    )

    assert item["asset_source"] == "local_imported"
    assert item["local_asset_source"] == "local_imported"
    assert item["local_asset_source_id"] == ""
    assert item["cloud_asset_source"] == "steam_workshop"
    assert item["cloud_asset_source_id"] == "123456"


@pytest.mark.unit
def test_cloudsave_summary_uses_single_character_meta_updated_at(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary, export_cloudsave_character_unit

    _write_runtime_state(cm, character_name="角色A")
    _add_runtime_character(cm, "角色B", recent_text="b-memory")

    export_cloudsave_character_unit(cm, "角色A")
    export_cloudsave_character_unit(cm, "角色B")

    expected_times = {
        "角色A": "2026-04-08T10:00:00Z",
        "角色B": "2026-04-09T11:30:00Z",
    }
    for character_name, updated_at in expected_times.items():
        meta_path = cm.cloudsave_dir / "characters" / character_name / "meta.json"
        meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
        meta_payload["updated_at_utc"] = updated_at
        atomic_write_json(meta_path, meta_payload, ensure_ascii=False, indent=2)

    summary = build_cloudsave_summary(cm)
    items_by_name = {item["character_name"]: item for item in summary["items"]}

    assert items_by_name["角色A"]["cloud_updated_at_utc"] == expected_times["角色A"]
    assert items_by_name["角色B"]["cloud_updated_at_utc"] == expected_times["角色B"]


@pytest.mark.unit
def test_cloudsave_summary_hides_cloud_entries_when_provider_is_unavailable(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary, export_local_cloudsave_snapshot

    _write_runtime_state(cm, character_name="小满")
    export_local_cloudsave_snapshot(cm)
    cm.cloudsave_provider_available = False

    summary = build_cloudsave_summary(cm)

    assert summary["provider_available"] is False
    assert len(summary["items"]) == 1
    assert summary["items"][0]["character_name"] == "小满"
    assert summary["items"][0]["relation_state"] == "local_only"
    assert summary["items"][0]["cloud_exists"] is False


@pytest.mark.unit
def test_export_snapshot_emits_single_character_shards_and_meta(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot

    _write_runtime_state(cm, character_name="小满")
    export_local_cloudsave_snapshot(cm)

    assert (cm.cloudsave_dir / "characters" / "小满" / "profile.json").is_file()
    assert (cm.cloudsave_dir / "characters" / "小满" / "binding.json").is_file()
    assert (cm.cloudsave_dir / "characters" / "小满" / "memory" / "recent.json").is_file()
    meta_payload = json.loads((cm.cloudsave_dir / "characters" / "小满" / "meta.json").read_text(encoding="utf-8"))
    assert meta_payload["character_name"] == "小满"
    assert meta_payload["payload_fingerprint"].startswith("sha256:")


@pytest.mark.unit
def test_export_snapshot_includes_external_import_state_sidecar(tmp_path):
    # external_import_state sidecar（空/全去重天的逐日幂等账本）必须随 facts 一起
    # 进快照，否则 cloudsave 用户换机/恢复后这些天丢指纹、重跑 LLM（修复对跨设备
    # 场景不完整）。加入 MANAGED_MEMORY_FILENAMES 后应被采集。
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot

    _write_runtime_state(cm, character_name="小满")
    atomic_write_json(
        Path(cm.memory_dir) / "小满" / "external_import_state.json",
        {"version": 1, "daily": {"imported_day_fingerprints": ["fp-x"]}},
        ensure_ascii=False, indent=2,
    )
    export_local_cloudsave_snapshot(cm)

    staged = cm.cloudsave_dir / "characters" / "小满" / "memory" / "external_import_state.json"
    assert staged.is_file()
    assert json.loads(staged.read_text(encoding="utf-8"))["daily"][
        "imported_day_fingerprints"
    ] == ["fp-x"]


@pytest.mark.unit
def test_export_snapshot_includes_prompt_locale_sidecars(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import (
        MANAGED_MEMORY_FILENAMES,
        export_local_cloudsave_snapshot,
    )

    _write_runtime_state(cm, character_name="小满")
    payloads = {
        "prompt_locale.json": {"language": "zh-TW", "order": 3},
        "scoped_prompt_locales.json": {
            "subjects": {"group": {"language": "zh-TW", "order": 4}},
        },
    }
    for filename, payload in payloads.items():
        atomic_write_json(
            Path(cm.memory_dir) / "小满" / filename,
            payload,
            ensure_ascii=False,
            indent=2,
        )

    export_local_cloudsave_snapshot(cm)

    assert payloads.keys() <= set(MANAGED_MEMORY_FILENAMES)
    for filename, payload in payloads.items():
        staged = cm.cloudsave_dir / "characters" / "小满" / "memory" / filename
        assert json.loads(staged.read_text(encoding="utf-8")) == payload


@pytest.mark.unit
def test_export_snapshot_includes_subject_forget_tombstones(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot

    _write_runtime_state(cm, character_name="小满")
    payload = [{
        "subject_kind": "participant",
        "subject_id": "qq:1001",
        "scope": "participant:qq:1001",
        "forgotten_at": "2026-08-01T00:00:00",
    }]
    atomic_write_json(
        Path(cm.memory_dir) / "小满" / "subject_forget_tombstones.json",
        payload, ensure_ascii=False, indent=2,
    )
    export_local_cloudsave_snapshot(cm)

    staged = (
        cm.cloudsave_dir / "characters" / "小满" / "memory"
        / "subject_forget_tombstones.json"
    )
    assert json.loads(staged.read_text(encoding="utf-8")) == payload


@pytest.mark.unit
def test_export_cloudsave_character_unit_updates_only_single_character_scope(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_cloudsave_character_unit

    _write_runtime_state(cm, character_name="小满")

    result = export_cloudsave_character_unit(cm, "小满")

    assert result["character_name"] == "小满"
    assert result["detail"]["item"]["relation_state"] == "matched"
    assert (cm.cloudsave_dir / "profiles" / "character_collection.json").is_file()
    assert (cm.cloudsave_dir / "bindings" / "小满.json").is_file()
    assert (cm.cloudsave_dir / "memory" / "小满" / "recent.json").is_file()
    assert (cm.cloudsave_dir / "characters" / "小满" / "meta.json").is_file()
    assert not (cm.cloudsave_dir / "profiles" / "conversation_settings.json").exists()
    assert not (cm.cloudsave_dir / "catalog" / "current_character.json").exists()

    manifest_payload = json.loads(cm.cloudsave_manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["snapshot_kind"] == "character_collection"
    assert manifest_payload["schema_version"] == 2
    assert manifest_payload["min_reader_schema_version"] == 2
    assert "characters/小满/profile.json" in manifest_payload["files"]
    assert "profiles/character_collection.json" in manifest_payload["files"]
    # Old readers require this monolithic full-runtime path. Omitting it makes
    # them reject a collection before applying any destructive replacement.
    assert "profiles/characters.json" not in manifest_payload["files"]


@pytest.mark.unit
def test_character_upload_converts_full_snapshot_to_role_only_scope(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_cloudsave_character_unit, export_local_cloudsave_snapshot

    _write_runtime_state(cm, character_name="小满")
    export_local_cloudsave_snapshot(cm)
    assert (cm.cloudsave_profiles_dir / "conversation_settings.json").is_file()
    assert (cm.cloudsave_catalog_dir / "current_character.json").is_file()

    result = export_cloudsave_character_unit(cm, "小满", overwrite=True)

    profiles = json.loads(
        (cm.cloudsave_profiles_dir / "character_collection.json").read_text(encoding="utf-8")
    )
    assert set(profiles) == {"猫娘"}
    assert result["manifest"]["snapshot_kind"] == "character_collection"
    assert "profiles/character_collection.json" in result["manifest"]["files"]
    assert "profiles/characters.json" not in result["manifest"]["files"]
    assert "profiles/conversation_settings.json" not in result["manifest"]["files"]
    assert "catalog/current_character.json" not in result["manifest"]["files"]
    assert not (cm.cloudsave_profiles_dir / "characters.json").exists()
    assert not (cm.cloudsave_profiles_dir / "conversation_settings.json").exists()
    assert not (cm.cloudsave_catalog_dir / "current_character.json").exists()


@pytest.mark.unit
def test_local_cloudsave_snapshot_roundtrip_supports_embedded_dot_character_names(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot

    _write_runtime_state(source_cm, character_name="N.E.K.O")

    export_local_cloudsave_snapshot(source_cm)

    assert (source_cm.cloudsave_dir / "characters" / "N.E.K.O" / "profile.json").is_file()
    assert (source_cm.cloudsave_dir / "bindings" / "N.E.K.O.json").is_file()

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    import_local_cloudsave_snapshot(target_cm)

    imported_characters = target_cm.load_characters()
    assert "N.E.K.O" in (imported_characters.get("猫娘") or {})
    assert (Path(target_cm.memory_dir) / "N.E.K.O" / "recent.json").is_file()


@pytest.mark.unit
def test_single_character_cloudsave_operations_support_embedded_dot_names(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_cloudsave_character_unit, import_cloudsave_character_unit

    _write_runtime_state(source_cm, character_name="N.E.K.O")

    export_result = export_cloudsave_character_unit(source_cm, "N.E.K.O")

    assert export_result["character_name"] == "N.E.K.O"
    assert (source_cm.cloudsave_dir / "bindings" / "N.E.K.O.json").is_file()
    assert (source_cm.cloudsave_dir / "characters" / "N.E.K.O" / "meta.json").is_file()

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    import_result = import_cloudsave_character_unit(target_cm, "N.E.K.O")

    assert import_result["character_name"] == "N.E.K.O"
    assert "N.E.K.O" in (target_cm.load_characters().get("猫娘") or {})
    assert (Path(target_cm.memory_dir) / "N.E.K.O" / "recent.json").is_file()


@pytest.mark.unit
def test_single_character_upload_rebuilds_collection_from_sharded_cloud_union(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_cloudsave_character_unit, import_local_cloudsave_snapshot

    _write_runtime_state(source_cm, character_name="角色A")
    _add_runtime_character(source_cm, "角色B", recent_text="b-memory")

    export_cloudsave_character_unit(source_cm, "角色A")
    export_cloudsave_character_unit(source_cm, "角色B")

    role_a_profile = json.loads(
        (source_cm.cloudsave_dir / "characters" / "角色A" / "profile.json").read_text(encoding="utf-8")
    )
    atomic_write_json(
        source_cm.cloudsave_profiles_dir / "characters.json",
        {"猫娘": {"角色A": role_a_profile}},
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(
        source_cm.cloudsave_catalog_dir / "catgirls_index.json",
        {
            "schema_version": 1,
            "sequence_number": 1,
            "exported_at_utc": "2026-04-09T00:00:00Z",
            "characters": [{"character_name": "角色A"}],
        },
        ensure_ascii=False,
        indent=2,
    )

    export_cloudsave_character_unit(source_cm, "角色A", overwrite=True)

    repaired_profiles = json.loads(
        (source_cm.cloudsave_profiles_dir / "character_collection.json").read_text(encoding="utf-8")
    )
    repaired_catalog = json.loads((source_cm.cloudsave_catalog_dir / "catgirls_index.json").read_text(encoding="utf-8"))
    assert set((repaired_profiles.get("猫娘") or {}).keys()) == {"角色A", "角色B"}
    assert not (source_cm.cloudsave_profiles_dir / "characters.json").exists()
    assert {entry.get("character_name") for entry in repaired_catalog.get("characters") or []} == {"角色A", "角色B"}

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    result = import_local_cloudsave_snapshot(target_cm)

    assert result["applied_character_count"] == 2
    assert result["snapshot_kind"] == "character_collection"
    imported_characters = target_cm.load_characters()
    assert {"角色A", "角色B"} <= set((imported_characters.get("猫娘") or {}).keys())
    assert imported_characters["主人"]["档案名"]


@pytest.mark.unit
def test_character_collection_import_preserves_owner_globals_and_unrelated_character(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_cloudsave_character_unit, import_local_cloudsave_snapshot

    _write_runtime_state(source_cm, character_name="云端角色")
    export_cloudsave_character_unit(source_cm, "云端角色")

    _write_runtime_state(target_cm, character_name="本地角色")
    target_characters = target_cm.load_characters()
    target_characters["主人"]["档案名"] = "本地主人"
    target_characters["当前猫娘"] = "本地角色"
    target_cm.save_characters(target_characters, bypass_write_fence=True)
    target_preferences_path = Path(target_cm.get_runtime_config_path("user_preferences.json"))
    target_preferences_path.parent.mkdir(parents=True, exist_ok=True)
    target_preferences_path.write_text('[{"local_global":"keep"}]', encoding="utf-8")
    unrelated_recent = Path(target_cm.memory_dir) / "本地角色" / "recent.json"
    unrelated_recent_before = unrelated_recent.read_bytes()

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    result = import_local_cloudsave_snapshot(target_cm)

    imported_characters = target_cm.load_characters()
    assert result["snapshot_kind"] == "character_collection"
    assert imported_characters["主人"]["档案名"] == "本地主人"
    assert imported_characters["当前猫娘"] == "本地角色"
    assert {"本地角色", "云端角色"} <= set(imported_characters["猫娘"])
    assert json.loads(target_preferences_path.read_text(encoding="utf-8")) == [
        {"local_global": "keep"},
    ]
    assert unrelated_recent.read_bytes() == unrelated_recent_before
    assert (Path(target_cm.memory_dir) / "云端角色" / "recent.json").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("marker_sequence", [None, "invalid"], ids=["collision", "malformed"])
def test_legacy_snapshot_without_kind_uses_merge_semantics(tmp_path, marker_sequence):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot

    _write_runtime_state(source_cm, character_name="云端角色")
    export_local_cloudsave_snapshot(source_cm)
    manifest = json.loads(source_cm.cloudsave_manifest_path.read_text(encoding="utf-8"))
    marker_payload = json.loads(
        (source_cm.cloudsave_catalog_dir / "current_character.json").read_text(encoding="utf-8")
    )
    manifest.pop("snapshot_kind", None)
    manifest["schema_version"] = 1
    manifest["min_reader_schema_version"] = 1
    if marker_sequence is None:
        assert int(marker_payload["entry_sequence_number"]) == int(manifest["sequence_number"])
        from utils.cloudsave_runtime.staging import _build_manifest_fingerprint

        manifest["fingerprint"] = _build_manifest_fingerprint(
            client_id=str(manifest.get("client_id") or ""),
            sequence_number=int(manifest.get("sequence_number") or 0),
            files=manifest["files"],
        )
    else:
        marker_payload["entry_sequence_number"] = marker_sequence
        atomic_write_json(
            source_cm.cloudsave_catalog_dir / "current_character.json",
            marker_payload,
            ensure_ascii=False,
            indent=2,
        )
        manifest["fingerprint"] = ""
    atomic_write_json(
        source_cm.cloudsave_manifest_path,
        manifest,
        ensure_ascii=False,
        indent=2,
    )

    _write_runtime_state(target_cm, character_name="本地角色")
    target_characters = target_cm.load_characters()
    target_characters["主人"]["档案名"] = "本地主人"
    target_cm.save_characters(target_characters, bypass_write_fence=True)
    unrelated_recent = Path(target_cm.memory_dir) / "本地角色" / "recent.json"
    unrelated_recent_before = unrelated_recent.read_bytes()

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    result = import_local_cloudsave_snapshot(target_cm)

    imported_characters = target_cm.load_characters()
    assert result["snapshot_kind"] == "character_collection"
    assert imported_characters["主人"]["档案名"] == "本地主人"
    assert {"本地角色", "云端角色"} <= set(imported_characters["猫娘"])
    assert unrelated_recent.read_bytes() == unrelated_recent_before


@pytest.mark.unit
def test_schema_one_stale_full_kind_uses_merge_semantics(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot
    from utils.cloudsave_runtime.staging import _build_manifest_fingerprint

    _write_runtime_state(source_cm, character_name="云端角色")
    export_local_cloudsave_snapshot(source_cm)
    manifest = json.loads(source_cm.cloudsave_manifest_path.read_text(encoding="utf-8"))
    assert manifest["snapshot_kind"] == "full_runtime"
    manifest["schema_version"] = 1
    manifest["min_reader_schema_version"] = 1
    manifest["fingerprint"] = _build_manifest_fingerprint(
        client_id=str(manifest.get("client_id") or ""),
        sequence_number=int(manifest.get("sequence_number") or 0),
        files=manifest["files"],
    )
    atomic_write_json(
        source_cm.cloudsave_manifest_path,
        manifest,
        ensure_ascii=False,
        indent=2,
    )

    _write_runtime_state(target_cm, character_name="本地角色")
    target_characters = target_cm.load_characters()
    target_characters["主人"]["档案名"] = "本地主人"
    target_cm.save_characters(target_characters, bypass_write_fence=True)
    unrelated_recent = Path(target_cm.memory_dir) / "本地角色" / "recent.json"
    unrelated_recent_before = unrelated_recent.read_bytes()

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    result = import_local_cloudsave_snapshot(target_cm)

    imported_characters = target_cm.load_characters()
    assert result["snapshot_kind"] == "character_collection"
    assert imported_characters["主人"]["档案名"] == "本地主人"
    assert {"本地角色", "云端角色"} <= set(imported_characters["猫娘"])
    assert unrelated_recent.read_bytes() == unrelated_recent_before


@pytest.mark.unit
def test_legacy_character_collection_repairs_missing_owner_without_replacing_roles(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_cloudsave_character_unit, import_local_cloudsave_snapshot
    from utils.cloudsave_runtime.staging import _build_manifest_fingerprint

    _write_runtime_state(source_cm, character_name="云端角色")
    export_cloudsave_character_unit(source_cm, "云端角色")
    manifest = json.loads(source_cm.cloudsave_manifest_path.read_text(encoding="utf-8"))
    manifest.pop("snapshot_kind", None)
    manifest["schema_version"] = 1
    manifest["min_reader_schema_version"] = 1
    manifest["fingerprint"] = _build_manifest_fingerprint(
        client_id=str(manifest.get("client_id") or ""),
        sequence_number=int(manifest.get("sequence_number") or 0),
        files=manifest["files"],
    )
    atomic_write_json(
        source_cm.cloudsave_manifest_path,
        manifest,
        ensure_ascii=False,
        indent=2,
    )

    _write_runtime_state(target_cm, character_name="本地角色")
    broken_characters = target_cm.load_characters()
    broken_characters.pop("主人", None)
    target_cm.save_characters(broken_characters, bypass_write_fence=True)
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    result = import_local_cloudsave_snapshot(target_cm)

    repaired_characters = target_cm.load_characters()
    assert result["snapshot_kind"] == "character_collection"
    assert repaired_characters["主人"]["档案名"]
    assert {"本地角色", "云端角色"} <= set(repaired_characters["猫娘"])


@pytest.mark.unit
def test_load_cloudsave_character_unit_respects_tombstones_for_sharded_characters(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import _load_cloudsave_character_unit, export_cloudsave_character_unit

    _write_runtime_state(cm, character_name="小满")
    export_cloudsave_character_unit(cm, "小满")
    atomic_write_json(
        cm.cloudsave_catalog_dir / "character_tombstones.json",
        {
            "schema_version": 1,
            "sequence_number": 3,
            "exported_at_utc": "2026-04-09T00:00:00Z",
            "tombstones": [
                {
                    "character_name": "小满",
                    "deleted_at": "2026-04-09T00:00:00Z",
                    "sequence_number": 3,
                }
            ],
        },
        ensure_ascii=False,
        indent=2,
    )

    assert _load_cloudsave_character_unit(cm, "小满") is None


@pytest.mark.unit
def test_import_cloudsave_character_unit_restores_only_target_character_and_preserves_globals(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_cloudsave_character_unit, import_cloudsave_character_unit

    _write_runtime_state(source_cm, character_name="云端角色")
    export_cloudsave_character_unit(source_cm, "云端角色")

    _write_runtime_state(target_cm, character_name="本地角色")
    target_characters = target_cm.load_characters()
    target_characters["当前猫娘"] = "本地角色"
    target_cm.save_characters(target_characters, bypass_write_fence=True)
    target_cm.save_character_tombstones_state(
        {
            "version": target_cm.CHARACTER_TOMBSTONES_STATE_VERSION,
            "tombstones": [
                {
                    "character_name": "云端角色",
                    "deleted_at": "2026-04-08T00:00:00Z",
                    "sequence_number": 7,
                }
            ],
        }
    )
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    import_result = import_cloudsave_character_unit(target_cm, "云端角色")

    assert import_result["character_name"] == "云端角色"
    imported_characters = target_cm.load_characters()
    assert "本地角色" in imported_characters["猫娘"]
    assert "云端角色" in imported_characters["猫娘"]
    assert imported_characters["当前猫娘"] == "本地角色"
    assert (Path(target_cm.memory_dir) / "云端角色" / "recent.json").is_file()
    restored_tombstones = target_cm.load_character_tombstones_state()
    assert restored_tombstones["tombstones"] == []


@pytest.mark.unit
def test_single_character_cloudsave_operations_preserve_reflections_archive(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import export_cloudsave_character_unit, import_cloudsave_character_unit

    _write_runtime_state(source_cm, character_name="云端角色")
    archive_payload = [{"id": "reflection-1", "text": "历史观察"}]
    atomic_write_json(
        Path(source_cm.memory_dir) / "云端角色" / "reflections_archive.json",
        archive_payload,
        ensure_ascii=False,
        indent=2,
    )

    export_cloudsave_character_unit(source_cm, "云端角色")
    assert (source_cm.cloudsave_dir / "characters" / "云端角色" / "memory" / "reflections_archive.json").is_file()

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    import_cloudsave_character_unit(target_cm, "云端角色")

    restored_archive = json.loads(
        (Path(target_cm.memory_dir) / "云端角色" / "reflections_archive.json").read_text(encoding="utf-8")
    )
    assert restored_archive == archive_payload


@pytest.mark.unit
def test_single_character_cloudsave_operations_raise_conflict_errors(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import CloudsaveOperationError, export_cloudsave_character_unit, import_cloudsave_character_unit

    _write_runtime_state(source_cm, character_name="小满")
    export_cloudsave_character_unit(source_cm, "小满")
    with pytest.raises(CloudsaveOperationError, match="cloud character already exists") as upload_exc:
        export_cloudsave_character_unit(source_cm, "小满", overwrite=False)
    assert upload_exc.value.code == "CLOUD_CHARACTER_EXISTS"

    _write_runtime_state(target_cm, character_name="小满")
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    with pytest.raises(CloudsaveOperationError, match="local character already exists") as download_exc:
        import_cloudsave_character_unit(target_cm, "小满", overwrite=False)
    assert download_exc.value.code == "LOCAL_CHARACTER_EXISTS"


@pytest.mark.unit
def test_import_cloudsave_character_unit_rolls_back_on_apply_failure(tmp_path):
    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils import cloudsave_runtime

    _write_runtime_state(source_cm, character_name="云端角色")
    cloudsave_runtime.export_cloudsave_character_unit(source_cm, "云端角色")

    _write_runtime_state(target_cm, character_name="本地角色")
    original_characters = target_cm.load_characters()
    original_recent = (Path(target_cm.memory_dir) / "本地角色" / "recent.json").read_text(encoding="utf-8")
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    original_apply_runtime_file = cloudsave_runtime._apply_runtime_file

    def _failing_apply_runtime_file(source_path, target_path):
        if str(target_path).endswith("character_tombstones.json"):
            raise RuntimeError("single import apply failed")
        return original_apply_runtime_file(source_path, target_path)

    with patch.object(cloudsave_runtime, "_apply_runtime_file", side_effect=_failing_apply_runtime_file):
        with pytest.raises(RuntimeError, match="single import apply failed"):
            cloudsave_runtime.import_cloudsave_character_unit(target_cm, "云端角色")

    assert target_cm.load_characters() == original_characters
    assert (Path(target_cm.memory_dir) / "本地角色" / "recent.json").read_text(encoding="utf-8") == original_recent
    assert not (Path(target_cm.memory_dir) / "云端角色").exists()


@pytest.mark.unit
def test_restore_cloudsave_operation_backup_restores_previous_character_state(tmp_path):
    from utils import recent_file

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")

    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
        restore_cloudsave_operation_backup,
    )

    _write_runtime_state(source_cm, character_name="小满")
    source_characters = source_cm.load_characters()
    source_characters["猫娘"]["小满"]["喜欢的食物"] = "鱼干"
    source_cm.save_characters(source_characters, bypass_write_fence=True)
    atomic_write_json(
        Path(source_cm.memory_dir) / "小满" / "recent.json",
        [{"role": "assistant", "content": "来自云端"}],
        ensure_ascii=False,
        indent=2,
    )
    export_cloudsave_character_unit(source_cm, "小满")

    _write_runtime_state(target_cm, character_name="小满")
    target_characters = target_cm.load_characters()
    target_characters["猫娘"]["小满"]["喜欢的食物"] = "罐头"
    target_cm.save_characters(target_characters, bypass_write_fence=True)
    original_characters = target_cm.load_characters()
    atomic_write_json(
        Path(target_cm.memory_dir) / "小满" / "recent.json",
        [{"role": "assistant", "content": "来自本地"}],
        ensure_ascii=False,
        indent=2,
    )
    original_recent = (Path(target_cm.memory_dir) / "小满" / "recent.json").read_text(encoding="utf-8")
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    import_result = import_cloudsave_character_unit(target_cm, "小满", overwrite=True)
    target_recent = Path(target_cm.memory_dir) / "小满" / "recent.json"
    imported_generation = recent_file.capture_recent_generation(target_recent)

    assert target_cm.load_characters()["猫娘"]["小满"]["喜欢的食物"] == "鱼干"
    assert (Path(target_cm.memory_dir) / "小满" / "recent.json").read_text(encoding="utf-8") != original_recent

    restore_cloudsave_operation_backup(target_cm, import_result["backup_path"])

    assert target_cm.load_characters() == original_characters
    assert (Path(target_cm.memory_dir) / "小满" / "recent.json").read_text(encoding="utf-8") == original_recent
    with pytest.raises(recent_file.RecentFileDeletedError):
        recent_file.write_recent_payload(
            target_recent,
            [{"content": "stale-import-writer"}],
            expected_generation=imported_generation,
        )



@pytest.mark.unit
def test_restoring_an_operation_backup_evicts_the_sidecar_caches(tmp_path):
    """The dual of the failed-rollback case, driven through the real entry.

    A failed export or import must NOT evict, because it only reverts a
    flush that raced it and the cache is the fresher copy. Rolling an
    operation back on purpose is the opposite: the older files are what
    was asked for, and a cache left loaded writes the rolled-back content
    straight back out.

    This drives ``restore_cloudsave_operation_backup`` rather than the
    private helper. The helper is already covered directly, and that
    turned out to prove nothing about the call sites: flipping either of
    them to False left the whole suite green.
    """
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
        restore_cloudsave_operation_backup,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    _write_runtime_state(source_cm, character_name="小满")
    export_cloudsave_character_unit(source_cm, "小满")
    _write_runtime_state(target_cm, character_name="小满")
    shutil.copytree(
        source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True
    )

    import_result = import_cloudsave_character_unit(
        target_cm, "小满", overwrite=True
    )

    with _isolated_sidecar_stores(
        Path(target_cm.memory_dir), config_manager=target_cm
    ) as (store, corpus, greeting):
        store._cache["小满"] = {"version": 1, "daily_buckets": {"stale": {}}}
        corpus._cache["小满"] = [{"stale": True}]
        greeting._cache["小满"] = ["stale"]

        restore_cloudsave_operation_backup(
            target_cm, import_result["backup_path"]
        )

        assert "小满" not in store._cache, (
            "the rollback reverted the files but left a cache to write them back"
        )
        assert "小满" not in corpus._cache
        assert "小满" not in greeting._cache

        # The same entry has a second restore path for schema-1 metadata,
        # sixty lines away from the one above. Flipping only that one left
        # the suite green, so it is driven here too rather than assumed to
        # match its neighbour.
        metadata_path = Path(import_result["backup_path"]) / "_operation.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop("recent_state", None)
        metadata["schema_version"] = 1
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        store._cache["小满"] = {"version": 1, "daily_buckets": {"stale": {}}}
        corpus._cache["小满"] = [{"stale": True}]
        greeting._cache["小满"] = ["stale"]

        restore_cloudsave_operation_backup(
            target_cm, import_result["backup_path"]
        )

        assert "小满" not in store._cache, (
            "the schema-1 restore path left a cache to write the files back"
        )
        assert "小满" not in corpus._cache
        assert "小满" not in greeting._cache


@pytest.mark.unit
def test_export_creates_valid_sqlite_shadow_copy_for_time_indexed_db(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot

    _write_runtime_state(cm)

    runtime_db_path = Path(cm.memory_dir) / "小满" / "time_indexed.db"
    runtime_db_path.unlink()

    with sqlite3.connect(str(runtime_db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO entries(content) VALUES (?)", ("来自 WAL 的长期记忆",))
        conn.commit()

        assert Path(f"{runtime_db_path}-wal").exists()
        export_local_cloudsave_snapshot(cm)

    exported_db_path = cm.cloudsave_dir / "memory" / "小满" / "time_indexed.db"
    with sqlite3.connect(str(exported_db_path)) as conn:
        row = conn.execute("SELECT content FROM entries").fetchone()
        quick_check = conn.execute("PRAGMA quick_check").fetchone()

    assert row == ("来自 WAL 的长期记忆",)
    assert quick_check == ("ok",)


@pytest.mark.unit
def test_export_persists_local_tombstones_into_catalog_and_import_state(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot

    _write_runtime_state(cm)
    cm.save_character_tombstones_state(
        {
            "version": cm.CHARACTER_TOMBSTONES_STATE_VERSION,
            "tombstones": [
                {
                    "character_name": "已删除角色",
                    "deleted_at": "2026-04-08T00:00:00Z",
                    "sequence_number": 11,
                }
            ],
        }
    )

    export_local_cloudsave_snapshot(cm)

    tombstones_catalog = json.loads((cm.cloudsave_dir / "catalog" / "character_tombstones.json").read_text(encoding="utf-8"))
    assert tombstones_catalog["tombstones"][0]["character_name"] == "已删除角色"

    cm.save_character_tombstones_state({"version": 1, "tombstones": []})
    import_local_cloudsave_snapshot(cm)

    restored_tombstones = cm.load_character_tombstones_state()
    assert restored_tombstones["tombstones"][0]["character_name"] == "已删除角色"


@pytest.mark.unit
def test_cross_device_import_overwrites_existing_runtime_without_duplicates_or_partial_loss(tmp_path):
    source_cm = _make_config_manager(tmp_path / "device_a")
    target_cm = _make_config_manager(tmp_path / "device_b")

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot

    character_name = "\u5c0f\u6ee1"
    extra_name = "\u672c\u5730\u591a\u4f59\u89d2\u8272"

    _write_runtime_state(source_cm, character_name=character_name)
    source_memory_dir = Path(source_cm.memory_dir) / character_name
    atomic_write_json(
        source_memory_dir / "recent.json",
        [{"role": "user", "content": "from-device-a"}],
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(
        source_memory_dir / "facts.json",
        [{"id": "fact-a", "content": "source-fact"}],
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(
        source_memory_dir / "persona.json",
        {"traits": ["source-persona"]},
        ensure_ascii=False,
        indent=2,
    )
    source_settings_path = source_memory_dir / "settings.json"
    if source_settings_path.exists():
        source_settings_path.unlink()
    source_db_path = source_memory_dir / "time_indexed.db"
    source_db_path.unlink()
    with sqlite3.connect(str(source_db_path)) as conn:
        conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO entries(content) VALUES (?)", ("source-db-entry",))
        conn.commit()

    export_local_cloudsave_snapshot(source_cm)

    _write_runtime_state(target_cm, character_name=character_name)
    target_memory_dir = Path(target_cm.memory_dir) / character_name
    atomic_write_json(
        target_memory_dir / "recent.json",
        [{"role": "user", "content": "from-device-b"}],
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(
        target_memory_dir / "settings.json",
        {"mood": "stale-target-state"},
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(
        target_memory_dir / "facts.json",
        [{"id": "fact-b", "content": "target-fact"}],
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(
        target_memory_dir / "persona.json",
        {"traits": ["target-persona"]},
        ensure_ascii=False,
        indent=2,
    )
    target_db_path = target_memory_dir / "time_indexed.db"
    target_db_path.unlink()
    with sqlite3.connect(str(target_db_path)) as conn:
        conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO entries(content) VALUES (?)", ("target-db-entry",))
        conn.commit()

    target_characters = target_cm.load_characters()
    template_character = copy.deepcopy(next(iter(target_characters["\u732b\u5a18"].values())))
    target_characters["\u732b\u5a18"][extra_name] = template_character
    target_characters["\u5f53\u524d\u732b\u5a18"] = extra_name
    target_cm.save_characters(target_characters, bypass_write_fence=True)
    extra_memory_dir = Path(target_cm.memory_dir) / extra_name
    extra_memory_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        extra_memory_dir / "recent.json",
        [{"role": "user", "content": "extra-local-character"}],
        ensure_ascii=False,
        indent=2,
    )

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    import_result = import_local_cloudsave_snapshot(target_cm)

    assert import_result["applied_character_count"] == 1
    assert target_cm.load_characters() == source_cm.load_characters()
    assert not extra_memory_dir.exists()
    assert not (target_memory_dir / "settings.json").exists()

    restored_recent = json.loads((target_memory_dir / "recent.json").read_text(encoding="utf-8"))
    restored_facts = json.loads((target_memory_dir / "facts.json").read_text(encoding="utf-8"))
    restored_persona = json.loads((target_memory_dir / "persona.json").read_text(encoding="utf-8"))
    assert restored_recent[0]["content"] == "from-device-a"
    assert restored_facts[0]["content"] == "source-fact"
    assert restored_persona["traits"] == ["source-persona"]

    with sqlite3.connect(str(target_memory_dir / "time_indexed.db")) as conn:
        rows = conn.execute("SELECT content FROM entries ORDER BY id").fetchall()
    assert rows == [("source-db-entry",)]


@pytest.mark.unit
def test_cross_device_import_applies_remote_tombstones_without_recreating_deleted_character(tmp_path):
    source_cm = _make_config_manager(tmp_path / "device_a")
    target_cm = _make_config_manager(tmp_path / "device_b")

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot, import_local_cloudsave_snapshot

    kept_name = "\u4fdd\u7559\u89d2\u8272"
    deleted_name = "\u5df2\u5220\u9664\u89d2\u8272"

    _write_runtime_state(source_cm, character_name=kept_name)
    source_cm.save_character_tombstones_state(
        {
            "version": source_cm.CHARACTER_TOMBSTONES_STATE_VERSION,
            "tombstones": [
                {
                    "character_name": deleted_name,
                    "deleted_at": "2026-04-08T00:00:00Z",
                    "sequence_number": 9,
                }
            ],
        }
    )
    export_local_cloudsave_snapshot(source_cm)

    _write_runtime_state(target_cm, character_name=kept_name)
    target_characters = target_cm.load_characters()
    template_character = copy.deepcopy(next(iter(target_characters["\u732b\u5a18"].values())))
    target_characters["\u732b\u5a18"][deleted_name] = template_character
    target_characters["\u5f53\u524d\u732b\u5a18"] = deleted_name
    target_cm.save_characters(target_characters, bypass_write_fence=True)
    deleted_memory_dir = Path(target_cm.memory_dir) / deleted_name
    deleted_memory_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        deleted_memory_dir / "recent.json",
        [{"role": "user", "content": "stale-local-data"}],
        ensure_ascii=False,
        indent=2,
    )

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    import_result = import_local_cloudsave_snapshot(target_cm)

    assert import_result["applied_character_count"] == 1
    imported_characters = target_cm.load_characters()
    assert deleted_name not in imported_characters.get("\u732b\u5a18", {})
    assert imported_characters["\u5f53\u524d\u732b\u5a18"] == kept_name
    assert not deleted_memory_dir.exists()

    restored_tombstones = target_cm.load_character_tombstones_state()
    assert restored_tombstones["tombstones"][0]["character_name"] == deleted_name


@pytest.mark.unit
def test_export_rejects_casefold_name_conflicts(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot

    characters = cm.get_default_characters()
    template_character = next(iter(characters["猫娘"].values()))
    characters["猫娘"] = {
        "Alice": template_character,
        "alice": template_character,
    }
    characters["当前猫娘"] = "Alice"
    cm.save_characters(characters, bypass_write_fence=True)

    with pytest.raises(ValueError, match="character name audit failed"):
        export_local_cloudsave_snapshot(cm)


@pytest.mark.unit
def test_export_allows_normal_words_that_only_contain_sensitive_substrings(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import export_local_cloudsave_snapshot
    from utils.config_manager import set_reserved

    characters = cm.get_default_characters()
    payload = characters["猫娘"][next(iter(characters["猫娘"]))]
    payload["喜欢的食物"] = "cookies"
    characters["猫娘"] = {"普通角色": payload}
    characters["当前猫娘"] = "普通角色"
    set_reserved(characters["猫娘"]["普通角色"], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"]["普通角色"], "avatar", "asset_source", "local")
    set_reserved(characters["猫娘"]["普通角色"], "avatar", "asset_source_id", "")
    set_reserved(characters["猫娘"]["普通角色"], "avatar", "live2d", "model_path", "demo/demo.model3.json")
    cm.save_characters(characters, bypass_write_fence=True)

    local_model_dir = Path(cm.live2d_dir) / "demo"
    local_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        local_model_dir / "demo.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )

    result = export_local_cloudsave_snapshot(cm)
    assert result["manifest"]["sequence_number"] >= 1


@pytest.mark.unit
def test_scan_for_sensitive_values_detects_secret_like_strings_without_flagging_plain_words():
    from utils.cloudsave_runtime import scan_for_sensitive_values

    assert scan_for_sensitive_values({"喜欢的食物": "cookies"}, path="profiles.characters") == []
    assert scan_for_sensitive_values({"note": "Authorization: Bearer abcdefghijklmnop"}, path="profiles.characters") == [
        "profiles.characters.note"
    ]


@pytest.mark.unit
def test_cloudsave_summary_does_not_persist_default_workshop_config_when_missing(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import build_cloudsave_summary

    _write_runtime_state(cm, character_name="小满")
    workshop_config_path = Path(cm.get_runtime_config_path("workshop_config.json"))
    if workshop_config_path.exists():
        workshop_config_path.unlink()

    summary = build_cloudsave_summary(cm)
    assert summary["success"] is True
    assert not workshop_config_path.exists()


@pytest.mark.unit
def test_import_rolls_back_runtime_on_apply_failure(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils import cloudsave_runtime

    _write_runtime_state(cm, character_name="旧角色")
    cloudsave_runtime.export_local_cloudsave_snapshot(cm)

    original_characters = cm.load_characters()
    original_recent = (Path(cm.memory_dir) / "旧角色" / "recent.json").read_text(encoding="utf-8")

    original_atomic_copy = cloudsave_runtime._atomic_copy_file

    def _failing_atomic_copy(source_path, target_path):
        if str(target_path).endswith("user_preferences.json"):
            raise RuntimeError("boom")
        return original_atomic_copy(source_path, target_path)

    with patch.object(cloudsave_runtime, "_atomic_copy_file", side_effect=_failing_atomic_copy):
        with pytest.raises(RuntimeError):
            cloudsave_runtime.import_local_cloudsave_snapshot(cm)

    assert cm.load_characters() == original_characters
    assert (Path(cm.memory_dir) / "旧角色" / "recent.json").read_text(encoding="utf-8") == original_recent


@pytest.mark.unit
def test_single_character_import_commits_and_restores_recent_runtime_state(tmp_path):
    from utils import recent_file
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
        restore_cloudsave_operation_backup,
    )
    from utils.llm_client import HumanMessage

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    target_cm.project_memory_dir = tmp_path / "target-project-memory"
    character_name = "云端角色"
    _write_runtime_state(source_cm, character_name=character_name)
    source_recent = Path(source_cm.memory_dir) / character_name / "recent.json"
    atomic_write_json(
        source_recent,
        [{"role": "user", "content": "cloud"}],
        ensure_ascii=False,
        indent=2,
    )
    export_cloudsave_character_unit(source_cm, character_name)

    _write_runtime_state(target_cm, character_name=character_name)
    from utils.character_memory import list_character_recent_paths

    target_recent = Path(target_cm.memory_dir) / character_name / "recent.json"
    target_recent_paths = list_character_recent_paths(target_cm, character_name)
    redirected_recent = Path(target_cm.memory_dir) / "redirect-target" / "recent.json"
    recent_file.redirect_recent_paths(target_recent_paths, redirected_recent)
    with recent_file.recent_file_locks(target_recent_paths):
        for recent_path in target_recent_paths:
            recent_file.set_recent_pending_unlocked(
                recent_path, [HumanMessage(content=f"pending:{recent_path.name}")],
            )

    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    result = import_cloudsave_character_unit(target_cm, character_name, overwrite=True)

    assert json.loads(target_recent.read_text(encoding="utf-8"))[0]["content"] == "cloud"
    with recent_file.recent_file_locks(target_recent_paths):
        assert all(
            recent_file.get_recent_pending_unlocked(recent_path) == []
            for recent_path in target_recent_paths
        )
    redirected_key = recent_file._lock_key(redirected_recent)
    assert all(
        recent_file._resolve_key_unlocked(recent_file._lock_key(recent_path))
        == recent_file._lock_key(recent_path)
        for recent_path in target_recent_paths
    )

    restore_cloudsave_operation_backup(target_cm, result["backup_path"])

    assert json.loads(target_recent.read_text(encoding="utf-8"))[0]["content"] == "你好"
    with recent_file.recent_file_locks(target_recent_paths):
        restored_pending = {
            recent_path: recent_file.get_recent_pending_unlocked(recent_path)
            for recent_path in target_recent_paths
        }
    assert all(messages for messages in restored_pending.values())
    assert all(
        recent_file._resolve_key_unlocked(recent_file._lock_key(recent_path))
        == redirected_key
        for recent_path in target_recent_paths
    )


@pytest.mark.unit
def test_cloud_exports_include_pending_recent_without_mutating_local_state(tmp_path):
    from utils import recent_file
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        export_local_cloudsave_snapshot,
    )
    from utils.llm_client import HumanMessage

    cm = _make_config_manager(tmp_path)
    character_name = "小满"
    _write_runtime_state(cm, character_name=character_name)
    recent_path = Path(cm.memory_dir) / character_name / "recent.json"
    disk_before = recent_path.read_bytes()
    with recent_file.recent_file_locks([recent_path]):
        recent_file.set_recent_pending_unlocked(
            recent_path, [HumanMessage(content="pending-export")],
        )

    export_cloudsave_character_unit(cm, character_name)
    for relative_path in (
        Path("memory") / character_name / "recent.json",
        Path("characters") / character_name / "memory" / "recent.json",
    ):
        payload = json.loads((cm.cloudsave_dir / relative_path).read_text(encoding="utf-8"))
        assert [item.get("data", item)["content"] for item in payload] == [
            "你好", "pending-export",
        ]

    export_local_cloudsave_snapshot(cm)
    for relative_path in (
        Path("memory") / character_name / "recent.json",
        Path("characters") / character_name / "memory" / "recent.json",
    ):
        payload = json.loads((cm.cloudsave_dir / relative_path).read_text(encoding="utf-8"))
        assert [item.get("data", item)["content"] for item in payload] == [
            "你好", "pending-export",
        ]

    assert recent_path.read_bytes() == disk_before
    with recent_file.recent_file_locks([recent_path]):
        pending = recent_file.get_recent_pending_unlocked(recent_path)
    assert [message.content for message in pending] == ["pending-export"]


@pytest.mark.unit
def test_single_import_retains_recent_lock_through_external_rollback(tmp_path):
    from utils import recent_file
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        finalize_cloudsave_character_import,
        import_cloudsave_character_unit,
        restore_cloudsave_operation_backup,
        rollback_cloudsave_character_import_registry,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    character_name = "云端角色"
    _write_runtime_state(source_cm, character_name=character_name)
    export_cloudsave_character_unit(source_cm, character_name)
    _write_runtime_state(target_cm, character_name=character_name)
    target_recent = Path(target_cm.memory_dir) / character_name / "recent.json"
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)

    result = import_cloudsave_character_unit(
        target_cm, character_name, overwrite=True, retain_recent_locks=True,
    )
    writer_entered = threading.Event()
    writer_errors = []

    def _waiting_writer():
        try:
            with recent_file.recent_file_access(target_recent):
                writer_entered.set()
        except Exception as exc:  # noqa: BLE001 - asserted in the main thread
            writer_errors.append(exc)

    writer = threading.Thread(target=_waiting_writer)
    writer.start()
    time.sleep(0.05)
    assert not writer_entered.is_set()

    restore_cloudsave_operation_backup(
        target_cm, result["backup_path"], recent_locks_held=True,
    )
    rollback_cloudsave_character_import_registry(result)
    assert not writer_entered.is_set()
    finalize_cloudsave_character_import(result)
    writer.join(3)

    assert not writer.is_alive()
    assert not writer_entered.is_set()
    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], recent_file.RecentFileDeletedError)
    assert json.loads(target_recent.read_text(encoding="utf-8"))[0]["content"] == "你好"


@pytest.mark.unit
def test_backup_restore_locks_historical_redirect_paths(tmp_path, monkeypatch):
    from utils.cloudsave_runtime import operations

    cm = _make_config_manager(tmp_path)
    backup_root = tmp_path / "backup"
    alias_path = Path(cm.memory_dir) / "Old" / "recent.json"
    target_path = Path(cm.memory_dir) / "New" / "recent.json"
    operations._write_operation_backup_metadata(
        cm,
        backup_root,
        operation="test_restore",
        character_name="New",
        backup_records=[],
        recent_pending={target_path: []},
        recent_redirects={str(alias_path): str(target_path)},
        recent_deleted=set(),
    )
    locked_paths = []

    @contextlib.contextmanager
    def _capture_locks(paths):
        locked_paths.extend(Path(path) for path in paths)
        yield

    monkeypatch.setattr(operations, "recent_file_locks", _capture_locks)
    operations.restore_cloudsave_operation_backup(cm, backup_root)

    assert set(locked_paths) == {alias_path, target_path}


@pytest.mark.unit
@pytest.mark.parametrize("legacy_metadata", [False, True])
def test_standalone_restore_invalidates_current_alias_writers(
    tmp_path, legacy_metadata,
):
    from utils import recent_file
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
        restore_cloudsave_operation_backup,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    character_name = "云端角色"
    _write_runtime_state(source_cm, character_name=character_name)
    export_cloudsave_character_unit(source_cm, character_name)
    _write_runtime_state(target_cm, character_name=character_name)
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    result = import_cloudsave_character_unit(target_cm, character_name, overwrite=True)
    target_recent = Path(target_cm.memory_dir) / character_name / "recent.json"

    if legacy_metadata:
        metadata_path = Path(result["backup_path"]) / "_operation.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["schema_version"] = 1
        metadata.pop("recent_state", None)
        atomic_write_json(metadata_path, metadata, ensure_ascii=False, indent=2)

    alias_path = Path(target_cm.memory_dir) / "CurrentAlias" / "recent.json"
    recent_file.redirect_recent_paths([alias_path], target_recent)
    alias_generation = recent_file.capture_recent_generation(alias_path)

    restore_cloudsave_operation_backup(target_cm, result["backup_path"])

    with pytest.raises(recent_file.RecentFileDeletedError):
        recent_file.write_recent_payload(
            alias_path,
            [{"content": "stale-alias-writer"}],
            expected_generation=alias_generation,
        )


@pytest.mark.unit
def test_single_import_releases_retained_lock_when_detail_build_fails(tmp_path):
    from utils import recent_file
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    character_name = "云端角色"
    _write_runtime_state(source_cm, character_name=character_name)
    export_cloudsave_character_unit(source_cm, character_name)
    _write_runtime_state(target_cm, character_name=character_name)
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    target_recent = Path(target_cm.memory_dir) / character_name / "recent.json"
    atomic_write_json(
        target_recent,
        [{"type": "human", "data": {"content": "local-before"}}],
        ensure_ascii=False,
        indent=2,
    )
    from utils.llm_client import HumanMessage

    with recent_file.recent_file_locks([target_recent]):
        recent_file.set_recent_pending_unlocked(
            target_recent, [HumanMessage(content="pending-before")],
        )
    generation_before = recent_file.capture_recent_generation(target_recent)

    with patch(
        "utils.cloudsave_runtime.operations.build_cloudsave_character_detail",
        side_effect=RuntimeError("detail failed"),
    ), pytest.raises(RuntimeError, match="detail failed"):
        import_cloudsave_character_unit(
            target_cm, character_name, overwrite=True, retain_recent_locks=True,
        )

    acquired = threading.Event()

    def _acquire_after_failure():
        with recent_file.recent_file_access(target_recent):
            acquired.set()

    worker = threading.Thread(target=_acquire_after_failure)
    worker.start()
    worker.join(3)
    assert acquired.is_set()
    assert not worker.is_alive()
    assert json.loads(target_recent.read_text(encoding="utf-8"))[0]["data"]["content"] == (
        "local-before"
    )
    with recent_file.recent_file_locks([target_recent]):
        pending = recent_file.get_recent_pending_unlocked(target_recent)
    assert [message.content for message in pending] == ["pending-before"]
    assert recent_file.capture_recent_generation(target_recent) == generation_before


@pytest.mark.unit
def test_legacy_operation_backup_restore_locks_recent_and_clears_pending(tmp_path):
    from utils import recent_file
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
        restore_cloudsave_operation_backup,
    )
    from utils.llm_client import HumanMessage

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    character_name = "云端角色"
    _write_runtime_state(source_cm, character_name=character_name)
    export_cloudsave_character_unit(source_cm, character_name)
    _write_runtime_state(target_cm, character_name=character_name)
    target_recent = Path(target_cm.memory_dir) / character_name / "recent.json"
    shutil.copytree(source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True)
    result = import_cloudsave_character_unit(target_cm, character_name, overwrite=True)
    imported_generation = recent_file.capture_recent_generation(target_recent)

    metadata_path = Path(result["backup_path"]) / "_operation.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["schema_version"] = 1
    metadata.pop("recent_state", None)
    atomic_write_json(metadata_path, metadata, ensure_ascii=False, indent=2)
    with recent_file.recent_file_locks([target_recent]):
        recent_file.set_recent_pending_unlocked(
            target_recent, [HumanMessage(content="stale-pending")],
        )

    restore_cloudsave_operation_backup(target_cm, result["backup_path"])

    assert json.loads(target_recent.read_text(encoding="utf-8"))[0]["content"] == "你好"
    with recent_file.recent_file_locks([target_recent]):
        assert recent_file.get_recent_pending_unlocked(target_recent) == []
    with pytest.raises(recent_file.RecentFileDeletedError):
        recent_file.write_recent_payload(
            target_recent,
            [{"content": "stale-import-writer"}],
            expected_generation=imported_generation,
        )


@pytest.mark.unit
def test_cloud_export_rejects_redirected_recent_snapshot(tmp_path):
    from utils import recent_file
    from utils.cloudsave_runtime import CloudsaveOperationError, export_cloudsave_character_unit

    cm = _make_config_manager(tmp_path)
    _write_runtime_state(cm, character_name="Old")
    old_recent = Path(cm.memory_dir) / "Old" / "recent.json"
    new_recent = Path(cm.memory_dir) / "New" / "recent.json"
    new_recent.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        new_recent,
        [{"role": "user", "content": "new-only"}],
        ensure_ascii=False,
        indent=2,
    )
    recent_file.redirect_recent_paths([old_recent], new_recent)

    with pytest.raises(CloudsaveOperationError) as exc_info:
        export_cloudsave_character_unit(cm, "Old")

    assert exc_info.value.code == "LOCAL_CHARACTER_CHANGED"
    assert not (cm.cloudsave_dir / "characters" / "Old" / "profile.json").exists()


@pytest.mark.unit
def test_standard_data_candidates_on_unix_platforms(tmp_path, real_root_resolution):
    from utils.config_manager import ConfigManager

    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch("utils.config_manager.Path.home", return_value=fake_home), patch(
        "utils.config_manager.sys.platform",
        "darwin",
    ):
        cm = ConfigManager("N.E.K.O")
        assert cm._get_standard_data_directory_candidates()[0] == fake_home / "Library" / "Application Support"

    with patch("utils.config_manager.Path.home", return_value=fake_home), patch(
        "utils.config_manager.sys.platform",
        "linux",
    ), patch.dict("os.environ", {"XDG_DATA_HOME": str(fake_home / ".xdg-data")}, clear=False):
        cm = ConfigManager("N.E.K.O")
        candidates = cm._get_standard_data_directory_candidates()
        assert candidates[0] == fake_home / ".xdg-data"
        assert fake_home / ".local" / "share" in candidates


@pytest.mark.unit
def test_cloud_import_evicts_stale_per_character_caches(tmp_path):
    """An import replaces character files; the in-memory caches must not survive.

    Each sidecar cache keeps ``{name: data}`` and only re-reads on a MISS, so a
    stale entry shadows the file that was just imported and the next flush
    writes it back over the imported contents — silently undoing part of the
    restore. Drives the real `import_local_cloudsave_snapshot`, so it fails if
    the eviction call site is removed rather than just the helper.
    """
    from utils.cloudsave_runtime import (
        export_local_cloudsave_snapshot,
        import_local_cloudsave_snapshot,
    )

    cm = _make_config_manager(tmp_path)
    _write_runtime_state(cm)
    export_local_cloudsave_snapshot(cm)

    with _isolated_sidecar_stores(tmp_path) as (store, corpus, greeting):
        store._cache["小满"] = {"version": 1, "daily_buckets": {"stale": {}}}
        corpus._cache["小满"] = [{"stale": True}]
        greeting._cache["小满"] = ["stale"]

        # The name was deleted earlier, so it is retired and whatever sits in
        # the caches belongs to that removed identity.
        store._retired.add("小满")

        import_local_cloudsave_snapshot(cm)

        # Retirement lifted: an imported profile is a LIVE identity, and left
        # retired it would be denied the lazy directory creation every sibling
        # memory writer gets.
        assert "小满" not in store._retired
        # ...and the deleted identity's cache goes with it, so the reused name
        # cannot inherit its aggregates. (The live-identity case, where the
        # cache must SURVIVE, is covered by the single-character download test
        # and by the staged-write test below.)
        assert "小满" not in store._cache


@pytest.mark.unit
def test_corpus_eviction_forgets_the_cache_without_writing_the_file(tmp_path):
    """`evict_character` is not `clear`: it must not persist an empty payload.

    Using `clear` for import eviction would delete the corpus that was just
    imported, which is the opposite of the intent.
    """
    import memory.anti_repeat as anti_repeat_module

    corpus = anti_repeat_module.AntiRepeatCorpus()
    corpus._cache["小满"] = [{"stale": True}]

    corpus.evict_character("小满")

    assert "小满" not in corpus._cache
    # `clear` bumps the written sequence by flushing; eviction must not.
    assert corpus._written_seq.get("小满", 0) == 0


@pytest.mark.unit
def test_single_character_download_revives_without_evicting(tmp_path):
    """A download lifts retirement and leaves the untouched caches alone.

    The apply rewrites only MANAGED_MEMORY_FILENAMES, which contains none of
    the three sidecars, so evicting them would drop nothing stale and would
    raise each store's sequence fence -- silently discarding a snapshot staged
    but not yet flushed, i.e. the reply just delivered. What the download does
    need is the retirement lifted, so a name reused after an earlier delete can
    create its directory again.
    """
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    _write_runtime_state(source_cm, character_name="云端角色")
    export_cloudsave_character_unit(source_cm, "云端角色")
    _write_runtime_state(target_cm, character_name="本地角色")
    shutil.copytree(
        source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True
    )

    with _isolated_sidecar_stores(tmp_path) as (store, corpus, greeting):
        store._cache["云端角色"] = {"version": 1, "daily_buckets": {"stale": {}}}
        corpus._cache["云端角色"] = [{"stale": True}]
        greeting._cache["云端角色"] = ["stale"]

        import_cloudsave_character_unit(target_cm, "云端角色")

        # See the full-snapshot test: the apply never touches these files, so
        # their caches stay.
        assert store._cache["云端角色"] == {"version": 1, "daily_buckets": {"stale": {}}}
        assert corpus._cache["云端角色"] == [{"stale": True}]
        assert greeting._cache["云端角色"] == ["stale"]
        assert "云端角色" not in store._retired, (
            "a downloaded character stayed retired and cannot create sidecars"
        )


@pytest.mark.unit
def test_a_download_does_not_discard_a_staged_sidecar_write(tmp_path):
    """Evicting fences the sequence, which drops a staged-but-unflushed write.

    ``record_anti_repeat_decision`` stages a snapshot and flushes it detached,
    so there is an ordinary window where ``_staged_seq > _written_seq``. An
    eviction in that window sets both to the staged value, and the pending
    flush then early-returns on ``seq <= _written_seq`` and is lost -- the
    reply just delivered never reaches the file. The apply never touches this
    sidecar, so there is nothing to evict for in the first place.
    """
    import memory.anti_repeat_effects as effects_module
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    _write_runtime_state(source_cm, character_name="云端角色")
    export_cloudsave_character_unit(source_cm, "云端角色")
    _write_runtime_state(target_cm, character_name="云端角色")
    shutil.copytree(
        source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True
    )

    with _isolated_sidecar_stores(
        Path(target_cm.memory_dir), config_manager=target_cm
    ) as (store, _c, _g):
        decision = effects_module.AntiRepeatDecision(
            source="proactive",
            reasons=("bm25",),
            action="block",
            outcome="blocked_initial",
        )
        store.record_decision("云端角色", decision, now=1_700_000_000.0)
        # Staged, deliberately not flushed -- the detached-flush window.
        staged = store.stage_decision(
            "云端角色", decision, now=1_700_000_001.0
        )

        import_cloudsave_character_unit(
            target_cm, "云端角色", overwrite=True
        )

        store._flush_snapshot(*staged)

    persisted = json.loads(
        (Path(target_cm.memory_dir) / "云端角色" / "anti_repeat_effects.json")
        .read_text(encoding="utf-8")
    )
    detected = [
        bucket["counters"]["detected"]
        for bucket in persisted["daily_buckets"].values()
    ]
    assert detected == [2], (
        "the import fenced away a staged sidecar write: %s" % persisted
    )


@pytest.mark.unit
def test_a_failed_download_does_not_discard_a_staged_sidecar_write(
    tmp_path, monkeypatch
):
    """The dual of the success path: a rollback must not fence one either.

    The apply never writes the three sidecars, so the failure rollback
    puts them back byte-identical -- except for anything flushed while the
    operation was in flight, which it reverts. The loaded cache is
    therefore at least as fresh as the file it is restored over, and
    evicting adopts the older state: the sequence fence advances, the
    pending flush early-returns on ``seq <= _written_seq``, and the reply
    just delivered never reaches disk.

    The deliberate rollback in ``restore_cloudsave_operation_backup`` is
    the opposite case and still evicts -- there the older state is what
    was asked for.
    """
    import memory.anti_repeat_effects as effects_module
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
        operations,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    _write_runtime_state(source_cm, character_name="云端角色")
    export_cloudsave_character_unit(source_cm, "云端角色")
    _write_runtime_state(target_cm, character_name="云端角色")
    shutil.copytree(
        source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True
    )

    with _isolated_sidecar_stores(
        Path(target_cm.memory_dir), config_manager=target_cm
    ) as (store, _c, _g):
        decision = effects_module.AntiRepeatDecision(
            source="proactive",
            reasons=("bm25",),
            action="block",
            outcome="blocked_initial",
        )
        store.record_decision(
            "云端角色", decision, now=1_700_000_000.0
        )
        # Staged, deliberately not flushed -- the detached-flush window.
        staged = store.stage_decision(
            "云端角色", decision, now=1_700_000_001.0
        )

        # Fail AFTER the apply, so the rollback runs with a write in
        # flight. This is the only difference from the success-path test.
        def _detail_fails(*args, **kwargs):
            raise RuntimeError("detail build failed")

        monkeypatch.setattr(
            operations, "build_cloudsave_character_detail", _detail_fails
        )
        with pytest.raises(RuntimeError):
            import_cloudsave_character_unit(
                target_cm, "云端角色", overwrite=True
            )
        monkeypatch.undo()

        store._flush_snapshot(*staged)

    persisted = json.loads(
        (
            Path(target_cm.memory_dir)
            / "云端角色"
            / "anti_repeat_effects.json"
        ).read_text(encoding="utf-8")
    )
    detected = [
        bucket["counters"]["detected"]
        for bucket in persisted["daily_buckets"].values()
    ]
    assert detected == [2], (
        "the failed import fenced away a staged sidecar write: %s" % persisted
    )


@pytest.mark.unit
def test_restoring_a_backup_drops_the_caches_it_replaces(tmp_path):
    """The RESTORE does replace the sidecars, unlike the apply.

    ``_restore_backup_records`` rmtree+copytree's whole ``memory/<name>/``
    directories, so it puts the three sidecars back to their pre-operation
    contents underneath any loaded cache. That used to be covered only by
    accident, because the success path evicted before the rollback ran; now
    that the success path merely lifts retirement, the restore has to evict for
    itself. The eviction lives in the restore helper rather than at its four
    call sites, so every caller is covered.
    """
    from utils.cloudsave_runtime import operations

    cm = _make_config_manager(tmp_path)
    memory_root = Path(cm.memory_dir)
    character_dir = memory_root / "小满"
    character_dir.mkdir(parents=True, exist_ok=True)
    (character_dir / "anti_repeat_effects.json").write_text(
        json.dumps({"version": 1, "daily_buckets": {"after": {}}}),
        encoding="utf-8",
    )
    backup_dir = tmp_path / "backup" / "小满"
    backup_dir.mkdir(parents=True)
    (backup_dir / "anti_repeat_effects.json").write_text(
        json.dumps({"version": 1, "daily_buckets": {"before": {}}}),
        encoding="utf-8",
    )

    with _isolated_sidecar_stores(memory_root) as (store, corpus, greeting):
        store._cache["小满"] = {"version": 1, "daily_buckets": {"stale": {}}}
        corpus._cache["小满"] = [{"stale": True}]
        greeting._cache["小满"] = ["stale"]

        operations._restore_backup_records(
            cm,
            [{"target": character_dir, "backup": backup_dir, "is_dir": True}],
            evict_sidecar_caches=True,
        )

        assert "小满" not in store._cache, (
            "the restore replaced the file but left the cache shadowing it"
        )
        assert "小满" not in corpus._cache
        assert "小满" not in greeting._cache

    restored = json.loads(
        (character_dir / "anti_repeat_effects.json").read_text(encoding="utf-8")
    )
    assert restored["daily_buckets"] == {"before": {}}


@pytest.mark.unit
def test_the_eviction_name_lookup_resolves_both_sides(tmp_path):
    """A memory_dir that is not already normalised still matches.

    ``restore_cloudsave_operation_backup`` builds its targets through
    ``_resolve_managed_target_path``, which resolves. Comparing those against
    a raw ``config_manager.memory_dir`` meant a root carrying a symlink, a
    "~" or a ".." never matched: the name list came back empty, nothing was
    evicted, and the stale caches wrote over what the rollback had just put
    back.

    The unnormalised form here is a ".." rather than a symlink -- it
    reproduces the same mismatch, is a real way to configure a path, and
    does not need a privilege Windows CI cannot grant. The call site itself
    is covered by the restore test above.
    """
    from utils.cloudsave_runtime import operations

    memory_root = tmp_path / "memory"
    character_dir = memory_root / "小满"
    character_dir.mkdir(parents=True)
    backup_dir = tmp_path / "backup" / "小满"
    backup_dir.mkdir(parents=True)
    records = [
        {"target": character_dir.resolve(), "backup": backup_dir, "is_dir": True}
    ]

    straight = SimpleNamespace(memory_dir=str(memory_root))
    assert operations._memory_character_names_from_backup_records(
        straight, records
    ) == (("小满",), ())

    # Same directory, spelled with a detour. Path keeps ".." literally, so
    # an unresolved comparison sees a different string.
    detour = tmp_path / "sidestep" / ".." / "memory"
    (tmp_path / "sidestep").mkdir()
    assert Path(detour) != memory_root, (
        "the detour normalised on its own -- this test would prove nothing"
    )
    assert Path(detour).resolve() == memory_root.resolve()

    crooked = SimpleNamespace(memory_dir=str(detour))
    assert operations._memory_character_names_from_backup_records(
        crooked, records
    ) == (("小满",), ()), (
        "an unnormalised memory_dir found no characters to evict"
    )

    # The dual: a directory that is genuinely elsewhere is still refused.
    outside = tmp_path / "elsewhere" / "小满"
    outside.mkdir(parents=True)
    assert operations._memory_character_names_from_backup_records(
        straight,
        [{"target": outside.resolve(), "backup": backup_dir, "is_dir": True}],
    ) == ((), ())

    # The other side of the comparison: a TARGET spelled with a detour has
    # to normalise too, not just the root.
    (memory_root / "sidestep").mkdir()
    crooked_target = memory_root / "sidestep" / ".." / "小满"
    assert Path(crooked_target) != character_dir
    assert operations._memory_character_names_from_backup_records(
        straight,
        [{"target": crooked_target, "backup": backup_dir, "is_dir": True}],
    ) == (("小满",), ()), (
        "an unnormalised target found no character to evict"
    )

    # And an empty target contributes nothing rather than resolving to the
    # working directory and donating its name. Only observable when the
    # working directory really is a child of the configured root, so the
    # root is chosen to make it so -- otherwise the guard is decorative and
    # deleting it would stay green.
    assert operations._memory_character_names_from_backup_records(
        straight, [{"target": "", "is_dir": True}]
    ) == ((), ())
    cwd_parent = SimpleNamespace(memory_dir=str(Path.cwd().parent))
    assert operations._memory_character_names_from_backup_records(
        cwd_parent, [{"target": "", "is_dir": True}]
    ) == ((), ()), (
        "an empty target resolved to the working directory and was treated "
        "as a character"
    )

    # A record with NO backup is one the restore DELETES -- the operation
    # being rolled back is what created the directory. Those names have to
    # come back on the retire side, not the evict side: evicting leaves the
    # name live, and a write still in flight would recreate the directory
    # for a character the restored characters.json no longer contains.
    assert operations._memory_character_names_from_backup_records(
        straight,
        [{"target": character_dir.resolve(), "backup": None, "is_dir": True}],
    ) == ((), ("小满",)), (
        "a directory the restore removes was queued for eviction, which "
        "leaves the deleted name free to recreate it"
    )


@pytest.mark.unit
def test_restoring_away_a_downloaded_character_retires_it(tmp_path):
    """Deleting a directory the operation created must retire, not evict.

    A download that creates a previously absent character records its
    memory directory with no backup -- there was nothing there to save --
    and with is_dir False, because that flag is captured before the
    directory exists. Rolling the operation back deletes the directory, but
    the successful import had already revived the sidecar stores for that
    name, so an anti-repeat or greeting write still in flight would recreate
    memory/<name>/ for a character the restored characters.json no longer
    contains.

    Driven through the real import and restore rather than the name helper:
    the helper alone stayed green with the retire call deleted and with the
    is_dir refresh deleted.
    """
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
        restore_cloudsave_operation_backup,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    _write_runtime_state(source_cm, character_name="新来的")
    export_cloudsave_character_unit(source_cm, "新来的")
    # The target knows a DIFFERENT character, so the downloaded one is
    # genuinely absent before the import.
    _write_runtime_state(target_cm, character_name="小满")
    shutil.copytree(
        source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True
    )
    memory_root = Path(target_cm.memory_dir)
    assert not (memory_root / "新来的").exists()

    with _isolated_sidecar_stores(
        memory_root, config_manager=target_cm
    ) as (store, corpus, greeting):
        result = import_cloudsave_character_unit(
            target_cm, "新来的", overwrite=True
        )
        assert (memory_root / "新来的").is_dir(), (
            "the import did not create the directory, so the scenario "
            "under test never happened"
        )
        assert "新来的" not in store._retired

        restore_cloudsave_operation_backup(
            target_cm, result["backup_path"]
        )

        assert not (memory_root / "新来的").exists(), (
            "the restore did not remove the downloaded directory"
        )
        for label, sidecar in (
            ("effects", store), ("corpus", corpus), ("greeting", greeting),
        ):
            assert "新来的" in sidecar._retired, (
                f"{label} left the deleted name live, so a write still in "
                "flight would recreate its directory"
            )

        # The behaviour that retirement buys: an in-flight write does not
        # put the directory back.
        import memory.anti_repeat_effects as effects_module

        store.record_decision(
            "新来的",
            effects_module.AntiRepeatDecision(
                source="proactive",
                reasons=("bm25",),
                action="block",
                outcome="blocked_initial",
            ),
            now=1_700_000_000.0,
        )
        assert not (memory_root / "新来的").exists(), (
            "a write after the rollback recreated the deleted character's "
            "directory"
        )

@pytest.mark.unit
def test_a_failed_restore_still_retires_the_directory_it_removed(tmp_path):
    """The retirement must not be the thing a mid-restore failure skips.

    Records are processed deepest-first, so the character directory is removed
    EARLY and the runtime/state files come after it. The lifecycle handling sat
    after the loop, so one of those later records raising left the removal in
    place and skipped the retirement outright -- leaving the name live with no
    directory, ready for the next in-flight write to recreate it as an orphan
    that ``character_memory_exists`` reports as a character the restored
    characters.json no longer contains.

    The failure is injected into the LAST thing the loop does, gated on the
    directory already being gone, so it can only fire in the window this is
    about. The sibling test above is the same scenario without the failure.
    """
    from utils import cloudsave_runtime as _facade
    from utils.cloudsave_runtime import (
        export_cloudsave_character_unit,
        import_cloudsave_character_unit,
        restore_cloudsave_operation_backup,
    )

    source_cm = _make_config_manager(tmp_path / "source")
    target_cm = _make_config_manager(tmp_path / "target")
    _write_runtime_state(source_cm, character_name="新来的")
    export_cloudsave_character_unit(source_cm, "新来的")
    _write_runtime_state(target_cm, character_name="小满")
    shutil.copytree(
        source_cm.cloudsave_dir, target_cm.cloudsave_dir, dirs_exist_ok=True
    )
    memory_root = Path(target_cm.memory_dir)

    with _isolated_sidecar_stores(
        memory_root, config_manager=target_cm
    ) as (store, corpus, greeting):
        result = import_cloudsave_character_unit(
            target_cm, "新来的", overwrite=True
        )
        assert (memory_root / "新来的").is_dir(), (
            "the import did not create the directory, so the scenario "
            "under test never happened"
        )

        real_apply = _facade._apply_runtime_file
        fired = []

        def _fail_once_the_directory_is_gone(source_path, target_path):
            if not (memory_root / "新来的").exists() and not fired:
                fired.append(True)
                raise OSError("a later record could not be put back")
            return real_apply(source_path, target_path)

        with patch.object(
            _facade, "_apply_runtime_file", _fail_once_the_directory_is_gone
        ):
            with pytest.raises(OSError):
                restore_cloudsave_operation_backup(
                    target_cm, result["backup_path"]
                )

        assert fired, "the injected failure never fired, so this proves nothing"
        assert not (memory_root / "新来的").exists(), (
            "the restore did not remove the downloaded directory"
        )
        for label, sidecar in (
            ("effects", store), ("corpus", corpus), ("greeting", greeting),
        ):
            assert "新来的" in sidecar._retired, (
                f"{label} left the deleted name live after the restore failed"
            )

        # The behaviour the retirement buys, which is the whole point: an
        # in-flight write does not put the directory back.
        import memory.anti_repeat_effects as effects_module

        store.record_decision(
            "新来的",
            effects_module.AntiRepeatDecision(
                source="proactive",
                reasons=("bm25",),
                action="block",
                outcome="blocked_initial",
            ),
            now=1_700_000_000.0,
        )
        assert not (memory_root / "新来的").exists(), (
            "a write after the failed restore recreated the deleted "
            "character's directory"
        )


def test_runtime_root_counts_an_interrupted_avatar_transaction_as_content(tmp_path):
    """An interrupted update may leave `.backup` as a tool's only copy; empty means replaced."""
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import _runtime_root_has_user_content

    avatar_tools = Path(cm.app_docs_dir) / "avatar_tools"
    backup = avatar_tools / ".local-12345678-1234-4123-8123-123456789abc.backup"
    backup.mkdir(parents=True)
    (backup / "record.json").write_text('{"recordVersion":2}', encoding="utf-8")

    assert _runtime_root_has_user_content(Path(cm.app_docs_dir)) is True

    # 判据不能放宽成「任何点开头的都算内容」：放宽了会把无关隐藏条目当成用户
    # 内容，从而拦下本该发生的迁移。名字必须逐字命中该模块的事务命名。
    shutil.rmtree(backup)
    for noise in (".DS_Store", ".cache.backup", ".local-not-a-uuid.updating"):
        entry = avatar_tools / noise
        entry.mkdir()
        (entry / "record.json").write_text('{"recordVersion":2}', encoding="utf-8")
        assert _runtime_root_has_user_content(Path(cm.app_docs_dir)) is False, noise
        shutil.rmtree(entry)


@pytest.mark.unit
def test_transactional_entry_pattern_tracks_the_avatar_tool_store_naming():
    """Both sides must agree letter for letter, or a sole surviving copy is deleted."""
    from utils.avatar_tool_store import (
        LOCAL_AVATAR_TOOL_BACKUP_PATTERN,
        LOCAL_AVATAR_TOOL_DELETING_PATTERN,
        LOCAL_AVATAR_TOOL_UPDATE_PATTERN,
        LOCAL_AVATAR_TOOL_UPLOAD_PATTERN,
    )
    from utils.cloudsave_runtime._shared import TRANSACTIONAL_RUNTIME_ENTRY_PATTERNS

    pattern = TRANSACTIONAL_RUNTIME_ENTRY_PATTERNS["avatar_tools"]
    tool_id = "local-12345678-1234-4123-8123-123456789abc"

    # 被打断的更新：这两个可能是仅存副本，收紧到认不出就是静默删除。
    for suffix, owner in (
        (".backup", LOCAL_AVATAR_TOOL_BACKUP_PATTERN),
        (".updating", LOCAL_AVATAR_TOOL_UPDATE_PATTERN),
    ):
        name = f".{tool_id}{suffix}"
        assert owner.fullmatch(name) is not None, (
            "这个用例的样本名已经和 store 的命名脱节，先修样本再看结论"
        )
        assert pattern.fullmatch(name) is not None, suffix

    # 创建暂存和删除暂存都不是仅存副本：用户要么还没创建成功，要么就是要删掉它。
    for suffix, owner in (
        (".uploading", LOCAL_AVATAR_TOOL_UPLOAD_PATTERN),
        (".deleting", LOCAL_AVATAR_TOOL_DELETING_PATTERN),
    ):
        name = f".{tool_id}{suffix}"
        assert owner.fullmatch(name) is not None, "样本名和 store 的命名脱节"
        assert pattern.fullmatch(name) is None, suffix


# ---------------------------------------------------------------------------
# Review round: the workspace exemption, the flat-file heartbeat, and the
# ledger's symlinked parent.
# ---------------------------------------------------------------------------


def test_a_stray_lock_file_does_not_exempt_a_character_from_deletion(tmp_path):
    """The exemption needs a HELD lock, not a file that happens to be there.

    ".mig-x" is a legal character name. Exempting on the prefix alone kept a
    cloud-deleted character alive through every import; exempting on the
    prefix plus the mere EXISTENCE of ".lock" did the same for any such
    character whose directory contained one.
    """
    from utils.config_manager.migrations import _workspace_is_live

    character = tmp_path / ".mig-Carol"
    character.mkdir()
    (character / "time_indexed.db").write_bytes(b"")
    (character / ".lock").write_bytes(b"")

    assert not _workspace_is_live(character)

    # A workspace killed before it could claim a marker is not live either;
    # the age check is what covers that one.
    abandoned = tmp_path / ".mig-abandoned"
    abandoned.mkdir()
    assert not _workspace_is_live(abandoned)


def test_a_held_lock_still_vetoes_the_deletion(tmp_path):
    """The dual. Without it the rule could pass by never exempting anything.

    An import running while a migration copies into memory/ must not remove
    the half-copied tree out from under it.
    """
    from utils.config_manager.migrations import (
        _MIGRATION_WORKSPACE_LOCK_NAME,
        _hold_workspace_lock,
        _workspace_is_live,
    )

    workspace = tmp_path / ".mig-inflight"
    workspace.mkdir()
    marker = workspace / _MIGRATION_WORKSPACE_LOCK_NAME
    marker.write_bytes(b"")

    handle = open(marker, "r+b")
    try:
        _hold_workspace_lock(handle)
        assert _workspace_is_live(workspace)
    finally:
        handle.close()

    assert not _workspace_is_live(workspace)


def test_the_import_separates_the_namespaces_by_name_alone():
    """The name is the criterion, because it is the only unforgeable one.

    A character name never begins with a dot and the workspace prefix does,
    so the namespaces do not overlap. Six rounds of findings landed on this
    exemption while it tried to tell them apart with evidence -- a marker
    file, a held lock, a held lock on a regular file, the ledger, the ledger
    or a held lock -- and every one was the same shape, because evidence is
    written after the directory exists and there is always a window in which
    the directory has none.

    Pinned structurally: a regression here is a return to reading state, and
    the states it would read are exactly the ones already reported.
    """
    import inspect

    from utils.cloudsave_runtime import operations

    source = inspect.getsource(operations)
    assert "def _is_migration_workspace(path):" in source
    assert source.count("_is_migration_workspace(") == 4, (
        "a call site stopped using the shared predicate: %d"
        % source.count("_is_migration_workspace(")
    )
    body = source[source.index("def _is_migration_workspace(path):"):]
    body = body[: body.index("memory_root = ")]
    for state in ("recorded_workspace_paths", "_workspace_is_live",
                  "_MIGRATION_WORKSPACE_LOCK_NAME", "exists("):
        assert state not in body, (
            "the exemption is reading %s again instead of the name" % state
        )


def test_the_heartbeat_beats_within_a_file_not_only_between_files(tmp_path):
    """A single large seed must keep the workspace mtime moving.

    Beating between files never covered one big file, which is the whole of
    the flat-file branch and is reachable through copytree too when one
    member dominates. A copy that beats once would let a stalled device carry
    the workspace past the reclamation threshold mid-copy.
    """
    import os

    from utils.config_manager.migrations import (
        _MIGRATION_COPY_CHUNK,
        _copy_with_heartbeat,
    )

    # The chunk size is PINNED, not just used to build the sample. The file
    # below is sized from the constant, so raising the constant raises the
    # file too and the beat count survives unchanged -- a mutation to one
    # gigabyte passed this test untouched. A chunk that large also reads the
    # whole file into memory, which is its own reason for a ceiling.
    assert 64 * 1024 <= _MIGRATION_COPY_CHUNK <= 16 * 1024 * 1024

    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * (_MIGRATION_COPY_CHUNK * 3 + 123))
    os.chmod(source, 0o444)  # a packaged seed arrives read-only

    beats = []
    copy = _copy_with_heartbeat(lambda: beats.append(1))
    destination = tmp_path / "out.bin"
    copy(source, destination)

    assert len(beats) > 2, "one file produced %d beats" % len(beats)
    # And it is still a copy2 in every way that mattered.
    assert destination.read_bytes() == source.read_bytes()
    assert os.stat(destination).st_mode & 0o777 == os.stat(source).st_mode & 0o777
    assert abs(os.stat(destination).st_mtime - os.stat(source).st_mtime) < 1e-6

    # copy2 accepts a directory destination, so this has to as well.
    into = tmp_path / "into"
    into.mkdir()
    copy(source, into)
    assert (into / "big.bin").exists()


def test_the_flat_file_branch_uses_the_heartbeat_copy():
    """The call site again: the helper was already correct for the other branch."""
    import inspect

    from utils.config_manager import migrations

    source = inspect.getsource(migrations.MigrationsMixin._migrate_memory_files_unlocked)
    assert "_copy_with_heartbeat(beat)(item, staged_file)" in source
    assert "shutil.copy2(item, staged_file)" not in source, (
        "the flat-file branch is back to a bare copy2"
    )


def test_the_ledger_refuses_a_symlinked_parent(tmp_path):
    """Recording must refuse what reclamation already refuses.

    ``mkdir(exist_ok=True)`` accepts a symlink-to-directory at the reserved
    name and the append then follows it, so a link pointing at unrelated user
    or plugin data received a "minted" file -- or an existing one of theirs
    was appended to.
    """
    import os

    from utils.config_manager.migrations import MigrationsMixin

    outsider = tmp_path / "somebody_elses_data"
    outsider.mkdir()
    # Deliberately shaped LIKE ours. The content check would refuse an
    # obviously foreign file on its own, which left the parent-link check
    # doing nothing this test could see -- a mutation deleting it stayed
    # green. Only the link check can save this one.
    import os as _os

    (outsider / "minted").write_text(
        _os.path.abspath(_os.path.join(_os.sep, "x", ".mig-theirs")) + "\n",
        encoding="utf-8",
    )

    app_docs = tmp_path / "app_docs"
    app_docs.mkdir()
    link = app_docs / ".mig-staging"
    try:
        os.symlink(str(outsider), str(link), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        import pytest

        pytest.skip("this platform will not create the symlink")

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager._record_minted_workspace(tmp_path / ".mig-abc")

    assert (outsider / "minted").read_text(encoding="utf-8") == _os.path.abspath(
        _os.path.join(_os.sep, "x", ".mig-theirs")
    ) + "\n", "the record was written through the link into unrelated data"


def test_the_ledger_still_records_through_an_ordinary_parent(tmp_path):
    """The dual, so the guard cannot pass by never recording anything."""
    from utils.config_manager.migrations import MigrationsMixin

    app_docs = tmp_path / "app_docs"
    app_docs.mkdir()

    memory = tmp_path / "memory"
    memory.mkdir()

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager.memory_dir = str(memory)
    workspace = memory / ".mig-abc"
    manager._record_minted_workspace(workspace)

    ledger = app_docs / ".mig-staging" / "minted"
    assert ledger.exists()
    assert str(workspace) in ledger.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Round two: what replacing copy2 cost, and two guards that stopped one step
# short of the thing they were guarding.
# ---------------------------------------------------------------------------


def test_the_heartbeat_copy_refuses_a_named_pipe(tmp_path, monkeypatch):
    """``copy2`` raised SpecialFileError on a FIFO; ``open`` blocks on one.

    This runs while the migration lock is held, so a named pipe anywhere in a
    seed tree would stop startup dead, for ever, waiting for a writer. Losing
    that check is what replacing copy2 with a chunked read cost.

    Faked rather than made with ``os.mkfifo``: this project's unit CI job runs
    on Windows, where a POSIX-only test would silently never execute -- which
    is exactly how a guard ends up untested.
    """
    import os
    import shutil
    import stat as stat_module

    from utils.config_manager import migrations as migrations_module

    source = tmp_path / "looks_like_a_pipe"
    source.write_bytes(b"")
    real_stat = os.stat

    def _fifo_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path) == str(source):
            return os.stat_result(
                (stat_module.S_IFIFO | 0o644,) + tuple(result)[1:]
            )
        return result

    monkeypatch.setattr(migrations_module.os, "stat", _fifo_stat)

    copy = migrations_module._copy_with_heartbeat(lambda: None)
    with pytest.raises(shutil.SpecialFileError):
        copy(source, tmp_path / "out")

    assert not (tmp_path / "out").exists(), (
        "the refusal happened after the destination was already opened"
    )


def test_a_marker_that_is_a_directory_is_not_a_live_workspace(tmp_path):
    """"Unopenable means a live owner" is right for a file, wrong for a dir.

    On Windows a marker held exclusively cannot be opened, which is why that
    reading exists. A ".lock" DIRECTORY cannot be opened either, so it read as
    live for ever -- and a character called ".mig-x" holding one survived
    every import after being deleted from the cloud.
    """
    from utils.config_manager.migrations import (
        _MIGRATION_WORKSPACE_LOCK_NAME,
        _workspace_is_live,
    )

    workspace = tmp_path / ".mig-Dora"
    workspace.mkdir()
    (workspace / _MIGRATION_WORKSPACE_LOCK_NAME).mkdir()

    assert not _workspace_is_live(workspace)


def test_the_ledger_refuses_a_symlinked_leaf(tmp_path):
    """Guarding the parent left the file itself.

    An append through a symlinked "minted" writes workspace paths into
    whatever it points at, and reclamation then reads that file back as
    though it were ours -- and unlinks it at the end.
    """
    import os

    from utils.config_manager.migrations import MigrationsMixin

    outsider = tmp_path / "somebody_elses.txt"
    outsider.write_text("their own file\n", encoding="utf-8")

    app_docs = tmp_path / "app_docs"
    staging = app_docs / ".mig-staging"
    staging.mkdir(parents=True)
    try:
        os.symlink(str(outsider), str(staging / "minted"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this platform will not create the symlink")

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager._record_minted_workspace(tmp_path / ".mig-abc")

    assert outsider.read_text(encoding="utf-8") == "their own file\n", (
        "a workspace path was appended through the link"
    )

    # Reclamation refuses the same shape, and must not unlink it either.
    manager._reclaim_recorded_workspaces(set(), 0.0)
    assert outsider.exists(), "reclamation deleted an outsider's file"


def test_a_ledger_that_is_not_an_ordinary_file_is_refused(tmp_path, monkeypatch):
    """A symlink guard lets a FIFO through, and reading one never returns.

    On POSIX a named pipe at ".mig-staging/minted" passes every link check
    while ``read_text`` blocks for ever waiting for a writer -- on the startup
    path, with the migration lock held. Same shape as the copy regression, so
    it is refused by the same kind of check.

    The mode is faked rather than made with ``os.mkfifo``: the unit CI job
    runs on Windows, where a POSIX-only test would silently never execute.
    """
    import os
    import stat as stat_module

    from utils.config_manager import migrations as migrations_module

    staging = tmp_path / ".mig-staging"
    staging.mkdir()
    ledger = staging / "minted"
    ledger.write_text("/somewhere/.mig-abc\n", encoding="utf-8")

    real_lstat = os.lstat

    def _fifo_lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if str(path) == str(ledger):
            return os.stat_result(
                (stat_module.S_IFIFO | 0o644,) + tuple(result)[1:]
            )
        return result

    # The real file first, so this cannot pass by never reading anything.
    assert migrations_module.recorded_workspace_paths(str(tmp_path))

    monkeypatch.setattr(migrations_module.os, "lstat", _fifo_lstat)
    assert migrations_module.recorded_workspace_paths(str(tmp_path)) == set()


def test_an_unreadable_ledger_exempts_nothing(tmp_path):
    """The safe direction for the import: exempt nothing, delete stale data.

    Returning everything would be the other way round -- an import that
    cannot read the ledger would keep every stale directory for ever.
    """
    from utils.config_manager.migrations import recorded_workspace_paths

    assert recorded_workspace_paths(str(tmp_path)) == set()
    assert recorded_workspace_paths("") == set()


def test_an_unowned_ledger_is_neither_rewritten_nor_deleted(tmp_path):
    """A "minted" that already belonged to someone else must survive.

    Path validation stops the directories a hostile line names from being
    removed. It does not stop the tidy-up from unlinking the file that holds
    those lines, which is somebody's data.
    """
    from utils.config_manager.migrations import MigrationsMixin

    app_docs = tmp_path / "app_docs"
    staging = app_docs / ".mig-staging"
    staging.mkdir(parents=True)
    ledger = staging / "minted"
    theirs = "some plugin's notes, not paths at all\n"
    ledger.write_text(theirs, encoding="utf-8")

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager.memory_dir = str(tmp_path / "memory")
    manager._reclaim_recorded_workspaces(None, 0.0)

    assert ledger.read_text(encoding="utf-8") == theirs, (
        "reclamation rewrote or deleted a ledger it could not show was ours"
    )


def test_our_own_ledger_is_still_reclaimed(tmp_path):
    """The dual, so the refusal cannot pass by never reclaiming anything."""
    from utils.config_manager.migrations import (
        _MIGRATION_WORKSPACE_PREFIX,
        MigrationsMixin,
    )

    app_docs = tmp_path / "app_docs"
    staging = app_docs / ".mig-staging"
    staging.mkdir(parents=True)
    memory = tmp_path / "memory"
    memory.mkdir()

    gone = memory / (_MIGRATION_WORKSPACE_PREFIX + "already-removed")
    ledger = staging / "minted"
    ledger.write_text(str(gone.resolve(strict=False)) + "\n", encoding="utf-8")

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager.memory_dir = str(memory)
    manager._reclaim_recorded_workspaces(None, 0.0)

    assert not ledger.exists(), (
        "a ledger holding only our own, already-gone entries was kept"
    )


def test_the_ledger_ownership_rule_reads_every_line(tmp_path):
    """One foreign line is enough; our own lines around it prove nothing."""
    import os

    from utils.config_manager.migrations import _ledger_lines_are_all_ours

    # NATIVE paths. A hand-built "C:\\x\\.mig-abc" asserts the opposite of
    # what it looks like on POSIX, where os.path.basename does not split on a
    # backslash and os.path.isabs is False -- so the line the test calls ours
    # would be rejected there and the assertion would fail.
    root = os.path.abspath(os.path.join(os.sep, "x", "memory"))
    ours = os.path.join(root, ".mig-abc")
    foreign = os.path.join(root, "somebody-else")
    # Prefixed and absolute, but OUTSIDE the root: the ledger exists only for
    # workspaces minted in the character namespace, so a line pointing
    # anywhere else was written by somebody else.
    elsewhere = os.path.abspath(os.path.join(os.sep, "tmp", ".mig-abc"))

    assert _ledger_lines_are_all_ours([ours], root)
    assert _ledger_lines_are_all_ours([], root)
    assert _ledger_lines_are_all_ours(["", "  "], root)
    assert not _ledger_lines_are_all_ours([ours, foreign], root)
    assert not _ledger_lines_are_all_ours([ours, elsewhere], root)
    assert not _ledger_lines_are_all_ours(
        [os.path.join("relative", ".mig-abc")], root
    )
    assert not _ledger_lines_are_all_ours(["some plugin's notes"], root)


def test_a_truncated_ledger_does_not_escape_the_new_reader(tmp_path):
    """The handling reclamation already had, which this reader lacked.

    A ledger truncated mid-character by a kill raises UnicodeDecodeError,
    which is not an OSError and so escaped the handler beside it. In
    reclamation that came out of a finally and failed the launch; here it
    would come out of a cloud import.
    """
    from utils.config_manager.migrations import recorded_workspace_paths

    staging = tmp_path / ".mig-staging"
    staging.mkdir()
    # A UTF-8 three-byte character cut after its first byte.
    (staging / "minted").write_bytes(b"/x/.mig-abc\n" + b"\xe5\xa5")

    assert recorded_workspace_paths(str(tmp_path)) == set()


def test_the_append_refuses_an_unowned_ledger_too(tmp_path):
    """The refusal went in on the read side alone.

    Reclamation stopped adopting a "minted" that belonged to a user or a
    plugin, while the append went on adding workspace paths into it -- which
    is the half that actually damages their file.
    """
    from utils.config_manager.migrations import MigrationsMixin

    app_docs = tmp_path / "app_docs"
    staging = app_docs / ".mig-staging"
    staging.mkdir(parents=True)
    ledger = staging / "minted"
    theirs = "some plugin's notes, not paths at all\n"
    ledger.write_text(theirs, encoding="utf-8")

    memory = tmp_path / "memory"
    memory.mkdir()

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager.memory_dir = str(memory)
    manager._record_minted_workspace(memory / ".mig-abc")

    assert ledger.read_text(encoding="utf-8") == theirs, (
        "a workspace path was appended into somebody else's file"
    )


def test_the_append_still_creates_and_extends_our_own_ledger(tmp_path):
    """The dual, so the refusal cannot pass by never recording anything."""
    import os

    from utils.config_manager.migrations import MigrationsMixin

    app_docs = tmp_path / "app_docs"
    app_docs.mkdir()

    memory = tmp_path / "memory"
    memory.mkdir()

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager.memory_dir = str(memory)

    first = memory / ".mig-one"
    second = memory / ".mig-two"
    manager._record_minted_workspace(first)
    manager._record_minted_workspace(second)

    ledger = app_docs / ".mig-staging" / "minted"
    lines = [
        line for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2, lines
    assert all(os.path.basename(line).startswith(".mig-") for line in lines)


def test_the_workspace_prefix_can_never_be_a_character_name():
    """The rule the exemption rests on, asserted where it is relied upon.

    A character name never begins with a dot -- a product rule, to be
    enforced in ``validate_character_name`` as a follow-up -- and the
    workspace prefix does. If either half ever stops being true the exemption
    silently starts sweeping workspaces or sparing characters, so both are
    pinned here rather than left implicit.
    """
    from utils.config_manager.migrations import _MIGRATION_WORKSPACE_PREFIX

    assert _MIGRATION_WORKSPACE_PREFIX.startswith("."), (
        "the workspace prefix stopped being dot-prefixed, so it no longer "
        "separates the namespaces on its own"
    )


def test_no_shipped_character_name_begins_with_a_dot():
    """The other half of the same rule, over the names the repo itself ships.

    A default or sample character carrying a dot would make the assumption
    false on a fresh install, which is the one place it could be broken
    without anybody typing it.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    offenders = []
    for candidate in (repo_root / "config").rglob("characters*.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        catgirls = data.get("猫娘") if isinstance(data, dict) else None
        if not isinstance(catgirls, dict):
            continue
        offenders += [
            "%s:%s" % (candidate.name, name)
            for name in catgirls
            if isinstance(name, str) and name.startswith(".")
        ]
    assert offenders == [], offenders


def test_an_empty_pre_existing_ledger_is_left_alone(tmp_path):
    """Our own emptied ledger and somebody's empty one are the same bytes.

    So there is nothing to tell them apart, and the tidy-up unlinked either.
    Leaving ours behind costs an empty file; removing theirs is data loss, so
    only a ledger that HELD lines -- every one of them ours, and now all
    reclaimed -- is removed.
    """
    from utils.config_manager.migrations import MigrationsMixin

    app_docs = tmp_path / "app_docs"
    staging = app_docs / ".mig-staging"
    staging.mkdir(parents=True)
    ledger = staging / "minted"
    ledger.write_text("", encoding="utf-8")

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager.memory_dir = str(tmp_path / "memory")
    manager._reclaim_recorded_workspaces(None, 0.0)

    assert ledger.exists(), "an empty ledger was unlinked without evidence"


def test_creating_a_character_refuses_every_dot_including_a_leading_one():
    """Where the workspace exemption's invariant is actually enforced.

    CREATION validates with ``allow_dots=False``, which refuses any dot at
    all -- so no character carrying the workspace prefix can be made. Adding
    a leading-dot rule to the validator itself changed nothing here and broke
    the other direction: the same function is the REQUEST and SNAPSHOT check,
    run with ``allow_dots=True`` over names that already exist, so an install
    upgrading with a legacy ".Carol" would have failed every memory call.
    """
    from utils.character_name import validate_character_name

    for name in (".mig-Carol", ".hidden", "Carol.db", "v1.2"):
        assert validate_character_name(name).code is not None, name
    assert validate_character_name("Carol").code is None
    assert validate_character_name("小八").code is None

    # And the tolerance path keeps accepting what is already on disk, which
    # is the half that must not become an upgrade break.
    assert validate_character_name(".Carol", allow_dots=True).code is None
    assert validate_character_name("v1.2", allow_dots=True).code is None


def test_an_unreadable_ledger_is_not_treated_as_creatable(tmp_path):
    """Append-but-not-read permissions would have modified an external file.

    Every OSError counted as "ours to create", so a "minted" we could not
    look at was appended to anyway. Only its absence means that.
    """
    from unittest.mock import patch

    from utils.config_manager.migrations import _ledger_content_is_ours

    ledger = tmp_path / "minted"
    ledger.write_text("whatever\n", encoding="utf-8")

    root = str(tmp_path / "memory")
    with patch.object(
        type(ledger), "read_text", side_effect=PermissionError("no read")
    ):
        assert _ledger_content_is_ours(ledger, root) is False

    assert _ledger_content_is_ours(tmp_path / "absent", root) is True


def test_the_memory_root_is_not_inside_itself():
    """A ledger line naming the root itself must not read as ours.

    Containment is what stops a foreign "minted" being adopted, and the root
    is the one path that would slip a prefix-and-absolute check while naming
    the whole character namespace -- reclamation acting on it would be
    reaching for every character at once.
    """
    import os

    from utils.config_manager.migrations import _is_direct_child

    root = os.path.abspath(os.path.join(os.sep, "x", "memory"))
    assert not _is_direct_child(root, root)
    assert not _is_direct_child(root, os.path.dirname(root))
    assert _is_direct_child(root, os.path.join(root, ".mig-abc"))
    assert not _is_direct_child(
        root, os.path.abspath(os.path.join(os.sep, "tmp", ".mig-abc"))
    )
    # DIRECT, matching the shape reclamation itself requires. A nested entry
    # passed ownership, was then discarded by reclamation's own check, and a
    # ledger holding only such lines came out empty and was unlinked.
    assert not _is_direct_child(root, os.path.join(root, "Carol", ".mig-x"))
    assert not _is_direct_child(root, os.path.join(root, ".mig-abc", "d"))


def test_an_ambiguous_seed_owner_is_settled_by_who_exists():
    """Pattern shape cannot answer this; the roster can.

    "facts_archive_Alice.json" is Alice's archive under
    "facts_archive_{name}.json" and archive_Alice's facts under
    "facts_{name}.json". Which one it is depends entirely on which of those
    two is a character. A specificity ranking -- most literal text matched
    wins -- got the common case right and this one wrong.
    """
    from utils.config_manager.migrations import (
        _seed_entry_owner_candidates,
        _tombstone_suppresses_seed,
    )

    # The decoder offers both readings and does not choose.
    assert _seed_entry_owner_candidates("facts_archive_Alice.json") == {
        "Alice", "archive_Alice",
    }
    assert _seed_entry_owner_candidates("time_indexed_Carol.db") == {
        "Carol", "Carol.db",
    }
    assert _seed_entry_owner_candidates("recent_小八.json") == {"小八"}
    assert _seed_entry_owner_candidates("unrelated.txt") == set()
    # The EXTRA entries table too: a legacy vector store is a directory named
    # for its owner, and reading only the file table left it republished.
    assert _seed_entry_owner_candidates("semantic_memory_Carol") == {"Carol"}

    deleted = frozenset({"Alice"})
    # A LIVE candidate wins: the file is hers, and a tombstone on another
    # reading of the same name says nothing about it.
    assert not _tombstone_suppresses_seed(
        "facts_archive_Alice.json", deleted, frozenset({"archive_Alice"})
    )
    # With no live claimant, the deleted one decides.
    assert _tombstone_suppresses_seed(
        "facts_archive_Alice.json", deleted, frozenset()
    )
    assert _tombstone_suppresses_seed(
        "time_indexed_Carol.db", frozenset({"Carol"}), frozenset()
    )
    assert _tombstone_suppresses_seed(
        "semantic_memory_Carol", frozenset({"Carol"}), frozenset()
    )
    # A directory seed is named for its character outright.
    assert _tombstone_suppresses_seed("Dora", frozenset({"Dora"}), frozenset())
    assert not _tombstone_suppresses_seed(
        "Dora", frozenset({"Dora"}), frozenset({"Dora"})
    )
    assert not _tombstone_suppresses_seed("recent_Eve.json", frozenset(), frozenset())


def test_an_existing_empty_ledger_is_not_ours_to_append_to(tmp_path):
    """The write and the tidy-up have to agree about the same file.

    Our own ledger is unlinked the moment it empties, so an empty one left on
    disk is somebody else's or a truncated remnant. Refusing to delete it
    while happily writing into it was the two halves disagreeing.
    """
    from utils.config_manager.migrations import MigrationsMixin

    app_docs = tmp_path / "app_docs"
    staging = app_docs / ".mig-staging"
    staging.mkdir(parents=True)
    ledger = staging / "minted"
    ledger.write_text("", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager.memory_dir = str(memory)
    manager._record_minted_workspace(memory / ".mig-abc")

    assert ledger.read_text(encoding="utf-8") == "", (
        "a workspace path was appended into an empty file we cannot show is ours"
    )


def test_a_symlink_loop_is_not_a_path_we_own(tmp_path, monkeypatch):
    """``resolve`` raises on a loop, and the raise is not an answer.

    OSError on most platforms and RuntimeError on some, so catching only the
    first left a loop propagating out of an ownership check. Faked rather
    than built, because a loop is awkward to create portably and the unit CI
    job runs on Windows.
    """
    from utils.config_manager import migrations as migrations_module

    real_resolve = migrations_module.Path.resolve

    def _loops(self, *args, **kwargs):
        if self.name == "looping":
            raise RuntimeError("symlink loop")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(migrations_module.Path, "resolve", _loops)
    assert not migrations_module._is_direct_child(
        str(tmp_path), str(tmp_path / "looping")
    )


def test_reading_the_live_roster_writes_nothing(tmp_path):
    """Deciding whether to seed must not create the thing it decides about.

    ``load_characters`` normalizes reserved fields and writes the result
    back, so consulting it during migration materialized characters.json in
    the runtime config directory -- and a runtime root holding only pristine
    defaults then reported itself as having user content, which is what
    decides whether a cloud snapshot may be imported on first launch.
    """
    from unittest.mock import MagicMock

    from utils.config_manager.migrations import _live_character_names

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    roster = config_dir / "characters.json"

    cm = MagicMock()
    cm.get_config_path.return_value = roster

    # Absent: nothing read, nothing created.
    assert _live_character_names(cm) == frozenset()
    assert not roster.exists(), "reading an absent roster created it"
    cm.load_characters.assert_not_called()

    roster.write_text(
        '{"猫娘": {"Alice": {}, "小八": {}}, "当前猫娘": "Alice"}',
        encoding="utf-8",
    )
    before = roster.read_bytes()
    assert _live_character_names(cm) == frozenset({"Alice", "小八"})
    assert roster.read_bytes() == before, "reading the roster rewrote it"
    cm.load_characters.assert_not_called()

    # Malformed reads as empty rather than raising out of a migration.
    roster.write_text("{not json", encoding="utf-8")
    assert _live_character_names(cm) == frozenset()
@pytest.mark.unit
def test_a_restore_evicts_only_the_characters_it_reached(tmp_path, monkeypatch):
    """A character whose directory was never touched still matches its cache.

    Records are processed deepest-first and the loop stops at the first raise,
    so a failure early on leaves later characters completely untouched. Evicting
    those adopts an older state anyway: the sequence fence advances past a flush
    that is still pending, and a reply already delivered never reaches disk.

    The raising record itself must still count as reached -- its removal may
    already have happened, which is why the lifecycle block sits in a finally at
    all. Both halves are asserted here.
    """
    import memory.anti_repeat_effects as effects_module
    from utils.cloudsave_runtime import operations as operations_module

    reached = []

    def _record_names(_config_manager, records):
        reached.append(
            [str(record["target"]) for record in records]
        )
        return (), ()

    monkeypatch.setattr(
        operations_module,
        "_memory_character_names_from_backup_records",
        _record_names,
    )

    deep = tmp_path / "memory" / "Deep"
    shallow = tmp_path / "state.json"
    deep.mkdir(parents=True)
    (deep / "facts.json").write_text("[1]", encoding="utf-8")
    shallow.write_text("{}", encoding="utf-8")

    backup_records = [
        {"target": deep, "backup": None, "is_dir": True},
        {"target": shallow, "backup": None, "is_dir": False},
    ]

    # Deepest-first, so `deep` is processed and `shallow` never is.
    def _boom(*_args, **_kwargs):
        raise OSError("restore failed partway")

    monkeypatch.setattr(operations_module.shutil, "rmtree", _boom)

    with pytest.raises(OSError):
        operations_module._restore_backup_records(
            SimpleNamespace(memory_dir=str(tmp_path / "memory")),
            backup_records,
            evict_sidecar_caches=True,
        )

    assert reached, "the lifecycle block never ran, so this proves nothing"
    seen = reached[-1]
    assert str(deep) in seen, (
        "the record that raised was skipped, but its removal may already have "
        "happened -- that is the case the finally exists for"
    )
    assert str(shallow) not in seen, (
        "a record the loop never reached was evicted anyway, which adopts an "
        "older state for a file this call never touched"
    )
    assert effects_module is not None


def test_a_whitespace_only_ledger_is_left_alone(tmp_path):
    """Blank lines are not ownership evidence either.

    Testing ``recorded`` for truth told a whitespace-only file apart from an
    empty one, though neither carries any evidence -- so somebody else's file
    holding two newlines was unlinked while their empty one was kept.
    """
    from utils.config_manager.migrations import MigrationsMixin

    app_docs = tmp_path / "app_docs"
    staging = app_docs / ".mig-staging"
    staging.mkdir(parents=True)
    ledger = staging / "minted"
    theirs = "\n   \n\n"
    ledger.write_text(theirs, encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()

    manager = MigrationsMixin.__new__(MigrationsMixin)
    manager.app_docs_dir = str(app_docs)
    manager.memory_dir = str(memory)
    manager._reclaim_recorded_workspaces(None, 0.0)

    assert ledger.exists(), "a whitespace-only ledger was unlinked"
    assert ledger.read_text(encoding="utf-8") == theirs


def test_clearing_read_only_keeps_a_directory_traversable(tmp_path, monkeypatch):
    """S_IWRITE alone is 0o200, which takes read and execute off a directory.

    The retry then cannot unlink what is inside, so the tree it was meant to
    remove stays -- and on POSIX under a non-root account that is the whole
    reason this handler exists.

    Asserted on the MODE REQUESTED rather than on whether the tree went.
    Windows honours only the write bit and does not block directory traversal
    on the read-only attribute, so the removal succeeds there either way --
    a mutation reverting the fix left this green when it asserted the
    outcome, and Windows is where this project's unit CI runs.
    """
    import os
    import stat as stat_module

    from utils.config_manager import migrations as migrations_module

    tree = tmp_path / "tree"
    inner = tree / "inner"
    inner.mkdir(parents=True)
    (inner / "leaf.txt").write_text("x", encoding="utf-8")
    # Read-only from the inside out, which is what a packaged seed copies.
    os.chmod(inner / "leaf.txt", stat_module.S_IREAD)
    os.chmod(inner, stat_module.S_IREAD | stat_module.S_IEXEC)
    os.chmod(tree, stat_module.S_IREAD | stat_module.S_IEXEC)

    requested = []
    real_chmod = os.chmod

    def _record(path, mode, *args, **kwargs):
        requested.append((os.path.basename(str(path)), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(migrations_module.os, "chmod", _record)
    try:
        migrations_module._force_rmtree(tree)
        assert not tree.exists(), "a read-only tree survived the removal"
    finally:
        monkeypatch.undo()
        for path in (tree, inner):
            if path.exists():
                os.chmod(path, 0o700)

    directory_modes = [
        mode for name, mode in requested if name in {"tree", "inner"}
    ]
    assert directory_modes, (
        "the handler never chmodded a directory, so nothing was exercised"
    )
    for mode in directory_modes:
        assert mode & stat_module.S_IREAD, "a directory was left unreadable"
        assert mode & stat_module.S_IEXEC, "a directory was left untraversable"


def test_a_deletion_is_recorded_even_with_cloudsave_disabled(tmp_path, monkeypatch):
    """The tombstone stopped being a cloudsave artifact.

    The seed migration reads it to tell "deleted on purpose" from "never
    migrated", so skipping it when cloudsave is off left that question
    unanswerable after a restart -- the project seed was republished and the
    deleted memory came back. Nothing reported it, because the delete itself
    succeeded.
    """
    from unittest.mock import MagicMock

    from main_routers.characters_router import crud

    # Disabled by PREFERENCE, which is the case that must still record.
    monkeypatch.setattr(crud, "is_cloudsave_disabled", lambda: True)
    monkeypatch.setattr(
        crud, "is_cloudsave_disabled_due_to_local_state_unavailable", lambda: False
    )

    cm = MagicMock()
    cm.load_cloudsave_local_state.return_value = {"next_sequence_number": 7}
    cm.load_character_tombstones_state.return_value = {
        "version": 1,
        "tombstones": [],
    }

    state = crud._build_character_tombstones_state(cm, "Carol")

    names = [entry["character_name"] for entry in state["tombstones"]]
    assert names == ["Carol"], state
    assert state["tombstones"][0]["sequence_number"] == 7, (
        "the sequence restarted instead of continuing the cloudsave one"
    )
    # And it must not have gone through the empty-default path.
    cm.build_default_character_tombstones_state.assert_not_called()


def test_the_delete_path_captures_a_rollback_snapshot_unconditionally():
    """Writing the record without one would survive a failed delete.

    That leaves a tombstone for a character who still exists: her seed is
    suppressed, and a later cloudsave upload would propagate a deletion that
    never happened. The capture and the write have to be governed by the same
    condition, which is now no condition at all.
    """
    import inspect

    from main_routers.characters_router import crud

    source = inspect.getsource(crud)
    assert "if not is_cloudsave_disabled():" not in source, (
        "the broad cloudsave guard is back in front of a tombstone path, so "
        "deletions go unrecorded for everyone who merely turned it off"
    )
    assert (
        "tombstone_snapshot = copy.deepcopy(_config_manager.load_character_tombstones_state())"
        in source
    ), "the rollback snapshot is no longer captured"
    # The capture and the write must share ONE condition. Two spellings drift,
    # and drifting here leaves a tombstone for a character who still exists.
    assert source.count("is_cloudsave_disabled_due_to_local_state_unavailable()") == 3, (
        "the build, the snapshot and the write no longer share one guard: %d"
        % source.count("is_cloudsave_disabled_due_to_local_state_unavailable()")
    )


def test_a_broken_state_directory_still_skips_the_tombstone(monkeypatch):
    """The one reason that is not a preference.

    Cloudsave can be disabled BECAUSE the local state directory is
    unavailable, and reading or writing the tombstone there fails and takes
    the delete with it. An existing regression test pins that the delete path
    must not touch tombstone state at all in that case.
    """
    from unittest.mock import MagicMock

    from main_routers.characters_router import crud

    monkeypatch.setattr(crud, "is_cloudsave_disabled", lambda: True)
    monkeypatch.setattr(
        crud, "is_cloudsave_disabled_due_to_local_state_unavailable", lambda: True
    )

    cm = MagicMock()
    cm.build_default_character_tombstones_state.return_value = {
        "version": 1,
        "tombstones": [],
    }

    state = crud._build_character_tombstones_state(cm, "Carol")

    assert state["tombstones"] == []
    cm.load_cloudsave_local_state.assert_not_called()
    cm.load_character_tombstones_state.assert_not_called()
