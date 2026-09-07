import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from utils.file_utils import atomic_write_json
from utils.config_manager import ConfigManager


_ORIGINAL_GET_DOCUMENTS_DIRECTORY = ConfigManager._get_documents_directory


def _make_config_manager(tmp_path):
    with patch.object(ConfigManager, "_get_documents_directory", return_value=tmp_path), patch.object(
        ConfigManager,
        "_get_standard_data_directory_candidates",
        return_value=[tmp_path / "standard_data"],
    ), patch.object(
        ConfigManager,
        "get_legacy_app_root_candidates",
        return_value=[],
    ), patch.object(
        ConfigManager,
        "_get_project_root",
        return_value=tmp_path / "project_root",
    ):
        return ConfigManager("N.E.K.O")


@pytest.mark.unit
def test_cloudsave_paths_follow_anchor_root_instead_of_runtime_root(tmp_path):
    cm = _make_config_manager(tmp_path)
    expected_runtime_root = tmp_path / "N.E.K.O"
    expected_anchor_root = tmp_path / "standard_data" / "N.E.K.O"

    assert cm.app_docs_dir == expected_runtime_root
    assert cm.anchor_root == expected_anchor_root
    assert cm.cloudsave_dir == expected_anchor_root / "cloudsave"
    assert cm.cloudsave_manifest_path == cm.cloudsave_dir / "manifest.json"
    assert cm.cloudsave_staging_dir == expected_anchor_root / ".cloudsave_staging"
    assert cm.cloudsave_backups_dir == expected_anchor_root / "cloudsave_backups"
    assert cm.root_state_path == expected_anchor_root / "state" / "root_state.json"
    assert cm.cloudsave_local_state_path == expected_anchor_root / "state" / "cloudsave_local_state.json"
    assert cm.character_tombstones_state_path == expected_anchor_root / "state" / "character_tombstones.json"


@pytest.mark.unit
def test_ensure_cloudsave_structure_creates_expected_directories(tmp_path):
    cm = _make_config_manager(tmp_path)

    assert cm.ensure_cloudsave_structure() is True

    expected_dirs = [
        cm.cloudsave_dir,
        cm.cloudsave_catalog_dir,
        cm.cloudsave_profiles_dir,
        cm.cloudsave_bindings_dir,
        cm.cloudsave_memory_dir,
        cm.cloudsave_overrides_dir,
        cm.cloudsave_meta_dir,
        cm.cloudsave_workshop_meta_dir,
        cm.cloudsave_staging_dir,
        cm.cloudsave_backups_dir,
    ]
    for directory in expected_dirs:
        assert directory.is_dir(), f"expected directory to exist: {directory}"


@pytest.mark.unit
def test_ensure_cloudsave_state_files_creates_defaults(tmp_path):
    cm = _make_config_manager(tmp_path)

    created = cm.ensure_cloudsave_state_files()

    assert created is True
    assert cm.root_state_path.is_file()
    assert cm.cloudsave_local_state_path.is_file()
    assert cm.character_tombstones_state_path.is_file()

    root_state = cm.load_root_state()
    cloud_state = cm.load_cloudsave_local_state()
    tombstone_state = cm.load_character_tombstones_state()

    assert root_state["version"] == cm.ROOT_STATE_VERSION
    assert root_state["current_root"] == str(cm.app_docs_dir)
    assert root_state["last_known_good_root"] == str(cm.app_docs_dir)
    assert cloud_state["version"] == cm.CLOUDSAVE_LOCAL_STATE_VERSION
    assert cloud_state["next_sequence_number"] == 1
    assert isinstance(cloud_state["client_id"], str) and cloud_state["client_id"]
    assert isinstance(cloud_state["client_proof"], str)
    assert len(cloud_state["client_proof"]) >= 32
    assert tombstone_state["version"] == cm.CHARACTER_TOMBSTONES_STATE_VERSION
    assert tombstone_state["tombstones"] == []


@pytest.mark.unit
def test_ensure_cloudsave_client_credentials_upgrades_legacy_state(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.ensure_cloudsave_state_files()
    state = cm.load_cloudsave_local_state()
    state.pop("client_proof")
    state["version"] = 1
    cm.save_cloudsave_local_state(state)

    client_id, client_proof = cm.ensure_cloudsave_client_credentials()

    persisted = cm.load_cloudsave_local_state()
    assert client_id == state["client_id"]
    assert persisted["client_id"] == client_id
    assert persisted["client_proof"] == client_proof
    assert persisted["version"] == cm.CLOUDSAVE_LOCAL_STATE_VERSION


@pytest.mark.unit
def test_ensure_cloudsave_client_credentials_rereads_state_inside_fence(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.ensure_cloudsave_state_files()
    legacy_state = cm.load_cloudsave_local_state()
    legacy_state.pop("client_proof")
    legacy_state["version"] = 1
    cm.save_cloudsave_local_state(legacy_state)

    @contextmanager
    def concurrent_export_fence(config_manager, **_kwargs):
        exported_state = config_manager.load_cloudsave_local_state()
        exported_state["next_sequence_number"] = 9
        exported_state["last_successful_export_at"] = "2026-07-26T21:30:00Z"
        config_manager.save_cloudsave_local_state(exported_state)
        yield

    with patch(
        "utils.cloudsave_runtime.cloud_apply_fence",
        concurrent_export_fence,
    ):
        client_id, client_proof = cm.ensure_cloudsave_client_credentials()

    persisted = cm.load_cloudsave_local_state()
    assert persisted["client_id"] == client_id == legacy_state["client_id"]
    assert persisted["client_proof"] == client_proof
    assert persisted["next_sequence_number"] == 9
    assert persisted["last_successful_export_at"] == "2026-07-26T21:30:00Z"


@pytest.mark.unit
def test_cloudsave_state_round_trip_preserves_data(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.ensure_cloudsave_state_files()

    original_cloud_state = cm.load_cloudsave_local_state()
    client_id = original_cloud_state["client_id"]

    root_state = cm.load_root_state()
    root_state["mode"] = "bootstrap_importing"
    root_state["last_successful_boot_at"] = "2026-04-08T00:00:00Z"
    cm.save_root_state(root_state)

    original_cloud_state["next_sequence_number"] = 7
    original_cloud_state["last_applied_manifest_fingerprint"] = "fp-test"
    cm.save_cloudsave_local_state(original_cloud_state)

    reloaded_root_state = cm.load_root_state()
    reloaded_cloud_state = cm.load_cloudsave_local_state()

    assert reloaded_root_state["mode"] == "bootstrap_importing"
    assert reloaded_root_state["last_successful_boot_at"] == "2026-04-08T00:00:00Z"
    assert reloaded_cloud_state["client_id"] == client_id
    assert reloaded_cloud_state["next_sequence_number"] == 7
    assert reloaded_cloud_state["last_applied_manifest_fingerprint"] == "fp-test"


@pytest.mark.unit
def test_ensure_cloudsave_state_files_raises_when_local_state_directory_init_fails(tmp_path):
    cm = _make_config_manager(tmp_path)

    with patch.object(cm, "ensure_local_state_directory", return_value=False):
        with pytest.raises(RuntimeError, match="root_state.json"):
            cm.ensure_cloudsave_state_files()


@pytest.mark.unit
def test_ensure_cloudsave_state_files_reports_local_state_directory_diagnostic(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.anchor_root.mkdir(parents=True, exist_ok=True)
    cm.local_state_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc_info:
        cm.ensure_cloudsave_state_files()

    message = str(exc_info.value)
    assert "root_state.json" in message
    assert f"anchor_root={cm.anchor_root.resolve()}" in message
    assert f"local_state_dir={cm.local_state_dir.resolve()}" in message
    assert "not a directory" in message


@pytest.mark.unit
def test_save_root_state_reports_anchor_file_blocker(tmp_path):
    standard_parent = tmp_path / "standard_data"
    standard_parent.mkdir(parents=True, exist_ok=True)
    blocked_anchor = standard_parent / "N.E.K.O"
    blocked_anchor.write_text("not a directory", encoding="utf-8")
    cm = _make_config_manager(tmp_path)

    with pytest.raises(OSError) as exc_info:
        cm.save_root_state(cm.build_default_root_state())

    message = str(exc_info.value)
    assert "Failed to ensure local state directory before saving root_state" in message
    assert f"anchor_root={blocked_anchor.resolve()}" in message
    assert f"local_state_dir={(blocked_anchor / 'state').resolve()}" in message
    assert "not a directory" in message


@pytest.mark.unit
def test_save_root_state_reports_state_file_blocker(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.anchor_root.mkdir(parents=True, exist_ok=True)
    cm.local_state_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError) as exc_info:
        cm.save_root_state(cm.build_default_root_state())

    message = str(exc_info.value)
    assert "Failed to ensure local state directory before saving root_state" in message
    assert f"anchor_root={cm.anchor_root.resolve()}" in message
    assert f"local_state_dir={cm.local_state_dir.resolve()}" in message
    assert "not a directory" in message


@pytest.mark.unit
def test_state_save_entrypoints_report_target_directory_blockers(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.ensure_cloudsave_state_files()

    targets = (
        (
            cm.root_state_path,
            lambda: cm.save_root_state(cm.build_default_root_state()),
            "saving root_state",
        ),
        (
            cm.cloudsave_local_state_path,
            lambda: cm.save_cloudsave_local_state(cm.build_default_cloudsave_local_state()),
            "saving cloudsave_local_state",
        ),
        (
            cm.character_tombstones_state_path,
            lambda: cm.save_character_tombstones_state(cm.build_default_character_tombstones_state()),
            "saving character_tombstones_state",
        ),
    )

    for target_path, save_call, operation in targets:
        target_path.unlink()
        target_path.mkdir()

        with pytest.raises(OSError) as exc_info:
            save_call()

        message = str(exc_info.value)
        assert getattr(exc_info.value, "local_state_directory_error", False)
        assert f"Failed to ensure local state file before {operation}" in message
        assert f"anchor_root={cm.anchor_root.resolve()}" in message
        assert f"local_state_dir={cm.local_state_dir.resolve()}" in message
        assert f"failed_path={target_path.resolve()}" in message
        assert "state file target exists but is not a file" in message

        target_path.rmdir()
        target_path.write_text("{}", encoding="utf-8")


@pytest.mark.unit
def test_get_documents_directory_preserves_first_readable_legacy_candidate(tmp_path, real_root_resolution):
    import utils.config_manager as config_manager_module
    from utils.config_manager import ConfigManager

    home_dir = tmp_path / "home"
    standard_dir = tmp_path / "standard"
    docs_dir = tmp_path / "Documents"
    cwd_dir = tmp_path / "cwd"
    project_root = tmp_path / "project_root"
    for path in (home_dir, standard_dir, docs_dir, cwd_dir, project_root):
        path.mkdir(parents=True, exist_ok=True)
    legacy_live2d_dir = docs_dir / "N.E.K.O" / "live2d"
    legacy_live2d_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(config_manager_module.sys, "platform", "linux"), patch.dict(
        config_manager_module.os.environ,
        {
            "XDG_DATA_HOME": str(standard_dir),
            "XDG_DOCUMENTS_DIR": str(docs_dir),
        },
        clear=False,
    ), patch.object(
        ConfigManager,
        "_get_documents_directory",
        _ORIGINAL_GET_DOCUMENTS_DIRECTORY,
    ), patch.object(config_manager_module.Path, "home", return_value=home_dir), patch.object(
        config_manager_module.Path,
        "cwd",
        return_value=cwd_dir,
    ), patch.object(
        ConfigManager,
        "_get_project_root",
        return_value=project_root,
    ):
        cm = ConfigManager("N.E.K.O")

    assert cm.docs_dir == standard_dir
    # Linux 回退到 XDG 可写目录时并不进入 CFA 可读目录模式，readable_live2d_dir 应保持 None。
    assert cm.readable_live2d_dir is None


@pytest.mark.unit
def test_get_documents_directory_ignores_non_document_legacy_roots_for_cfa_detection(tmp_path, real_root_resolution):
    import utils.config_manager as config_manager_module
    from utils.config_manager import ConfigManager

    home_dir = tmp_path / "home"
    standard_dir = tmp_path / "standard"
    docs_dir = tmp_path / "Documents"
    exe_dir = tmp_path / "bundle"
    cwd_dir = tmp_path / "cwd"
    project_root = tmp_path / "project_root"
    for path in (home_dir, standard_dir, docs_dir, exe_dir, cwd_dir, project_root):
        path.mkdir(parents=True, exist_ok=True)
    legacy_live2d_dir = docs_dir / "N.E.K.O" / "live2d"
    legacy_live2d_dir.mkdir(parents=True, exist_ok=True)
    exe_binary = exe_dir / "N.E.K.O"
    exe_binary.write_text("", encoding="utf-8")

    with patch.object(config_manager_module.sys, "platform", "linux"), patch.object(
        config_manager_module.sys,
        "frozen",
        True,
        create=True,
    ), patch.object(
        config_manager_module.sys,
        "executable",
        str(exe_binary),
    ), patch.dict(
        config_manager_module.os.environ,
        {
            "XDG_DATA_HOME": str(standard_dir),
            "XDG_DOCUMENTS_DIR": str(docs_dir),
        },
        clear=False,
    ), patch.object(
        ConfigManager,
        "_get_documents_directory",
        _ORIGINAL_GET_DOCUMENTS_DIRECTORY,
    ), patch.object(config_manager_module.Path, "home", return_value=home_dir), patch.object(
        config_manager_module.Path,
        "cwd",
        return_value=cwd_dir,
    ), patch.object(
        ConfigManager,
        "_get_project_root",
        return_value=project_root,
    ):
        cm = ConfigManager("N.E.K.O")

    assert cm.docs_dir == standard_dir
    # 非 Windows CFA 场景不应暴露额外的只读回退目录。
    assert cm.readable_live2d_dir is None


@pytest.mark.unit
def test_get_documents_directory_uses_linux_xdg_fallback_when_xdg_data_home_missing(tmp_path, real_root_resolution):
    import utils.config_manager as config_manager_module
    from utils.config_manager import ConfigManager

    home_dir = tmp_path / "home"
    docs_dir = home_dir / "Documents"
    cwd_dir = tmp_path / "cwd"
    project_root = tmp_path / "project_root"
    for path in (home_dir, docs_dir, cwd_dir, project_root):
        path.mkdir(parents=True, exist_ok=True)
    legacy_live2d_dir = docs_dir / "N.E.K.O" / "live2d"
    legacy_live2d_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(config_manager_module.sys, "platform", "linux"), patch.dict(
        config_manager_module.os.environ,
        {
            "XDG_DATA_HOME": "",
            "XDG_DOCUMENTS_DIR": str(docs_dir),
        },
        clear=False,
    ), patch.object(
        ConfigManager,
        "_get_documents_directory",
        _ORIGINAL_GET_DOCUMENTS_DIRECTORY,
    ), patch.object(config_manager_module.Path, "home", return_value=home_dir), patch.object(
        config_manager_module.Path,
        "cwd",
        return_value=cwd_dir,
    ), patch.object(
        ConfigManager,
        "_get_project_root",
        return_value=project_root,
    ):
        cm = ConfigManager("N.E.K.O")

    assert cm.docs_dir == home_dir / ".local" / "share"
    # Linux XDG fallback 下不启用 CFA 可读目录回退。
    assert cm.readable_live2d_dir is None


@pytest.mark.unit
def test_persist_user_workshop_folder_retries_after_save_failure(tmp_path):
    cm = _make_config_manager(tmp_path)
    workshop_dir = tmp_path / "workshop"
    workshop_dir.mkdir(parents=True, exist_ok=True)

    save_side_effects = [OSError("disk full"), None]

    with patch.object(cm, "load_workshop_config", return_value={}), patch.object(
        cm,
        "save_workshop_config",
        side_effect=save_side_effects,
    ) as save_mock:
        cm.persist_user_workshop_folder(str(workshop_dir))
        assert cm._user_workshop_folder_persisted is False

        cm.persist_user_workshop_folder(str(workshop_dir))
        assert cm._user_workshop_folder_persisted is True

    assert save_mock.call_count == 2


@pytest.mark.unit
def test_load_workshop_config_does_not_delete_invalid_file_on_read(tmp_path):
    cm = _make_config_manager(tmp_path)
    config_path = cm.config_dir / "workshop_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{not-valid-json", encoding="utf-8")

    loaded = cm.load_workshop_config()

    assert isinstance(loaded, dict)
    assert config_path.is_file()
    assert config_path.read_text(encoding="utf-8") == "{not-valid-json"


@pytest.mark.unit
def test_load_workshop_config_rebases_paths_from_retained_migration_source(tmp_path):
    cm = _make_config_manager(tmp_path)
    source_root = tmp_path / "old-root" / "N.E.K.O"
    source_workshop = source_root / "workshop"
    external_mods = tmp_path / "external-mods"
    cm.config_dir.mkdir(parents=True, exist_ok=True)
    source_workshop.mkdir(parents=True, exist_ok=True)
    external_mods.mkdir(parents=True, exist_ok=True)

    atomic_write_json(
        cm.config_dir / "workshop_config.json",
        {
            "default_workshop_folder": str(source_workshop),
            "user_workshop_folder": str(source_workshop / "cached"),
            "user_mod_folder": str(external_mods),
        },
        ensure_ascii=False,
        indent=2,
    )
    cm.save_root_state(
        {
            **cm.build_default_root_state(),
            "current_root": str(cm.app_docs_dir),
            "last_known_good_root": str(cm.app_docs_dir),
            "last_migration_source": str(source_root),
            "last_migration_backup": str(source_root),
            "last_migration_result": f"completed:{cm.app_docs_dir}",
        }
    )

    loaded = cm.load_workshop_config()
    persisted = json.loads((cm.config_dir / "workshop_config.json").read_text(encoding="utf-8"))

    assert loaded["default_workshop_folder"] == str(cm.workshop_dir)
    assert loaded["user_workshop_folder"] == str(cm.workshop_dir / "cached")
    assert loaded["user_mod_folder"] == str(external_mods)
    assert persisted == loaded


@pytest.mark.unit
def test_repair_workshop_configs_respects_cloudsave_write_fence(tmp_path):
    cm = _make_config_manager(tmp_path)
    from utils.cloudsave_runtime import MaintenanceModeError

    with patch("utils.cloudsave_runtime.assert_cloudsave_writable", side_effect=MaintenanceModeError("maintenance_readonly")), patch.object(
        cm,
        "_cleanup_invalid_workshop_configs",
    ) as mock_cleanup:
        with pytest.raises(MaintenanceModeError):
            cm.repair_workshop_configs()

    mock_cleanup.assert_not_called()


@pytest.mark.unit
def test_load_user_preferences_prefers_runtime_path_when_present(tmp_path):
    cm = _make_config_manager(tmp_path)
    runtime_preferences_path = cm.config_dir / "user_preferences.json"
    project_preferences_path = cm.project_config_dir / "user_preferences.json"
    runtime_preferences_path.parent.mkdir(parents=True, exist_ok=True)
    project_preferences_path.parent.mkdir(parents=True, exist_ok=True)

    atomic_write_json(
        project_preferences_path,
        [{"model_path": "/legacy.model3.json", "position": {"x": 1}, "scale": {"x": 1}}],
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_json(
        runtime_preferences_path,
        [{"model_path": "/runtime.model3.json", "position": {"x": 2}, "scale": {"x": 2}}],
        ensure_ascii=False,
        indent=2,
    )

    with patch("utils.config_manager._config_manager", cm):
        from utils import preferences as preferences_module

        with patch.object(preferences_module, "_config_manager", cm):
            loaded = preferences_module.load_user_preferences()

    assert loaded[0]["model_path"] == "/runtime.model3.json"


@pytest.mark.unit
def test_save_characters_writes_runtime_root_even_when_project_fallback_exists(tmp_path):
    cm = _make_config_manager(tmp_path)

    project_characters_path = cm.project_config_dir / "characters.json"
    runtime_characters_path = cm.config_dir / "characters.json"
    project_characters_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(project_characters_path, cm.get_default_characters(), ensure_ascii=False, indent=2)
    assert not runtime_characters_path.exists()

    characters = cm.load_characters()
    template_name = next(iter(characters["猫娘"]))
    characters["猫娘"]["运行时角色"] = json.loads(json.dumps(characters["猫娘"][template_name], ensure_ascii=False))
    characters["当前猫娘"] = "运行时角色"
    cm.save_characters(characters, bypass_write_fence=True)

    assert runtime_characters_path.is_file()
    project_payload = json.loads(project_characters_path.read_text(encoding="utf-8"))
    runtime_payload = json.loads(runtime_characters_path.read_text(encoding="utf-8"))
    assert runtime_payload["当前猫娘"] == characters["当前猫娘"]
    assert project_payload["当前猫娘"] != characters["当前猫娘"]


@pytest.mark.unit
def test_save_json_config_writes_runtime_root_even_when_project_fallback_exists(tmp_path):
    cm = _make_config_manager(tmp_path)

    project_core_config_path = cm.project_config_dir / "core_config.json"
    runtime_core_config_path = cm.config_dir / "core_config.json"
    project_core_config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        project_core_config_path,
        {"recent_memory_auto_review": False, "coreApi": "legacy"},
        ensure_ascii=False,
        indent=2,
    )
    assert not runtime_core_config_path.exists()

    loaded = cm.load_json_config("core_config.json", default_value={})
    loaded["recent_memory_auto_review"] = True
    loaded["coreApi"] = "runtime"
    cm.save_json_config("core_config.json", loaded)

    assert runtime_core_config_path.is_file()
    assert json.loads(runtime_core_config_path.read_text(encoding="utf-8"))["coreApi"] == "runtime"
    assert json.loads(project_core_config_path.read_text(encoding="utf-8"))["coreApi"] == "legacy"


@pytest.mark.unit
def test_load_characters_keeps_in_memory_migration_when_write_fence_blocks_persist(tmp_path):
    cm = _make_config_manager(tmp_path)
    characters_path = cm.config_dir / "characters.json"
    characters_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        characters_path,
        {
            "猫娘": {
                "旧角色": {
                    "名字": "旧角色",
                    "live2d": "legacy_model",
                    "item_id": "123456",
                }
            },
            "主人": {},
            "当前猫娘": "旧角色",
        },
        ensure_ascii=False,
        indent=2,
    )

    from utils.cloudsave_runtime import MaintenanceModeError

    maintenance_error = MaintenanceModeError("bootstrap_importing", operation="save", target="characters.json")

    with patch("utils.cloudsave_runtime.assert_cloudsave_writable", side_effect=maintenance_error), patch(
        "utils.config_manager.logger.warning"
    ) as mock_warning:
        loaded = cm.load_characters()

    reserved = loaded["猫娘"]["旧角色"]["_reserved"]
    assert reserved["avatar"]["asset_source"] == "steam_workshop"
    assert reserved["avatar"]["asset_source_id"] == "123456"
    assert reserved["avatar"]["live2d"]["model_path"] == "legacy_model/legacy_model.model3.json"
    assert "_reserved" not in json.loads(characters_path.read_text(encoding="utf-8"))["猫娘"]["旧角色"]
    mock_warning.assert_not_called()


@pytest.mark.unit
def test_save_user_preferences_writes_runtime_root_even_when_project_fallback_exists(tmp_path):
    cm = _make_config_manager(tmp_path)

    project_preferences_path = cm.project_config_dir / "user_preferences.json"
    runtime_preferences_path = cm.config_dir / "user_preferences.json"
    project_preferences_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        project_preferences_path,
        [{"model_path": "/legacy.model3.json", "position": {"x": 1}, "scale": {"x": 1}}],
        ensure_ascii=False,
        indent=2,
    )
    assert not runtime_preferences_path.exists()

    with patch("utils.config_manager._config_manager", cm):
        from utils import preferences as preferences_module

        with patch.object(preferences_module, "_config_manager", cm):
            saved = preferences_module.save_user_preferences(
                [{"model_path": "/runtime.model3.json", "position": {"x": 2}, "scale": {"x": 2}}]
            )

    assert saved is True
    assert runtime_preferences_path.is_file()
    assert json.loads(runtime_preferences_path.read_text(encoding="utf-8"))[0]["model_path"] == "/runtime.model3.json"
    assert json.loads(project_preferences_path.read_text(encoding="utf-8"))[0]["model_path"] == "/legacy.model3.json"


@pytest.mark.unit
def test_load_root_state_reraises_corrupt_json_even_with_default_value(tmp_path):
    cm = _make_config_manager(tmp_path)
    cm.root_state_path.parent.mkdir(parents=True, exist_ok=True)
    cm.root_state_path.write_text("{not-valid-json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        cm.load_root_state()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("save_call", "target_name"),
    (
        (lambda cm: cm.save_characters({"猫娘": {}, "主人": {}, "当前猫娘": ""}), "characters.json"),
        (lambda cm: cm.save_json_config("core_config.json", {"coreApi": "demo"}), "core_config.json"),
        (lambda cm: cm.save_workshop_config({"default_workshop_folder": "/tmp/workshop"}), "workshop_config.json"),
    ),
)
def test_config_save_entrypoints_check_write_fence_before_ensuring_config_dir(tmp_path, save_call, target_name):
    cm = _make_config_manager(tmp_path)

    from utils.cloudsave_runtime import MaintenanceModeError

    maintenance_error = MaintenanceModeError("maintenance_readonly", operation="save", target=target_name)

    with patch("utils.cloudsave_runtime.assert_cloudsave_writable", side_effect=maintenance_error), patch.object(
        cm,
        "ensure_config_directory",
        side_effect=AssertionError("ensure_config_directory should not run before the write fence"),
    ):
        with pytest.raises(MaintenanceModeError):
            save_call(cm)


@pytest.mark.unit
def test_preferences_save_entrypoints_check_write_fence_before_ensuring_config_dir(tmp_path):
    cm = _make_config_manager(tmp_path)

    from utils import preferences as preferences_module
    from utils.cloudsave_runtime import MaintenanceModeError

    maintenance_error = MaintenanceModeError("maintenance_readonly", operation="save", target="user_preferences.json")

    with patch("utils.config_manager._config_manager", cm), patch.object(
        preferences_module,
        "_config_manager",
        cm,
    ), patch.object(
        preferences_module,
        "assert_cloudsave_writable",
        side_effect=maintenance_error,
    ), patch.object(
        cm,
        "ensure_config_directory",
        side_effect=AssertionError("ensure_config_directory should not run before the write fence"),
    ):
        with pytest.raises(MaintenanceModeError):
            preferences_module.save_user_preferences(
                [{"model_path": "/runtime.model3.json", "position": {"x": 2}, "scale": {"x": 2}}]
            )
        with pytest.raises(MaintenanceModeError):
            preferences_module.save_global_conversation_settings({"focusModeEnabled": True})
