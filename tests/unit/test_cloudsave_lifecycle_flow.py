import contextlib
import json
import shutil
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from fastapi.testclient import TestClient


def _role_state_from_session_managers(session_managers: dict) -> dict:
    """Build a role_state dict seeded with the given session_managers.

    Post-#855 consolidation: the old module-level ``session_manager`` dict
    became ``role_state[name].session_manager``. ``sync_shutdown_event`` /
    ``sync_process`` were later removed when cross_server moved from daemon
    thread to a main-loop ``asyncio.Task``. Tests that want to stub
    shutdown-time behavior construct RoleState stubs here (with live
    Queue/Lock so any adapter access does not explode).
    """
    from app.main_server import RoleState, _SyncMessageQueue
    return {
        name: RoleState(
            sync_message_queue=_SyncMessageQueue(),
            websocket_lock=asyncio.Lock(),
            session_manager=session_manager,
        )
        for name, session_manager in session_managers.items()
    }

import pytest

from utils.cloudsave_autocloud import CloudSaveManager
from utils.cloudsave_runtime import bootstrap_local_cloudsave_environment
from utils.cloudsave_runtime import export_cloudsave_character_unit
from utils.cloudsave_runtime import import_cloudsave_character_unit
from utils.steam_cloud_bundle import (
    REMOTE_BUNDLE_FILENAME,
    REMOTE_META_FILENAME,
    download_cloudsave_bundle_from_steam,
    upload_cloudsave_bundle_to_steam,
)
from utils.config_manager import ConfigManager
from utils.file_utils import atomic_write_json


def _make_config_manager(tmp_root: Path):
    cleared_env = {
        "NEKO_STORAGE_SELECTED_ROOT": "",
        "NEKO_STORAGE_ANCHOR_ROOT": "",
        "NEKO_STORAGE_CLOUDSAVE_ROOT": "",
    }
    with patch.dict("os.environ", cleared_env, clear=False), patch.object(
        ConfigManager,
        "_get_documents_directory",
        return_value=tmp_root,
    ), patch.object(
        ConfigManager,
        "_get_standard_data_directory_candidates",
        return_value=[tmp_root],
    ), patch.object(
        ConfigManager,
        "get_legacy_app_root_candidates",
        return_value=[],
    ), patch.object(
        ConfigManager,
        "_get_project_root",
        return_value=tmp_root,
    ):
        config_manager = ConfigManager("N.E.K.O")
    config_manager.get_legacy_app_root_candidates = lambda: []
    return config_manager


def _write_runtime_state(cm, *, character_name: str, recent_message: str = "你好"):
    from utils.config_manager import set_reserved

    characters = cm.get_default_characters()
    template_name = next(iter(characters["猫娘"]))
    characters["猫娘"] = {
        character_name: characters["猫娘"][template_name]
    }
    characters["当前猫娘"] = character_name
    set_reserved(characters["猫娘"][character_name], "avatar", "model_type", "live2d")
    set_reserved(characters["猫娘"][character_name], "avatar", "asset_source", "steam_workshop")
    set_reserved(characters["猫娘"][character_name], "avatar", "asset_source_id", "123456")
    set_reserved(characters["猫娘"][character_name], "avatar", "live2d", "model_path", "example/example.model3.json")
    cm.save_characters(characters, bypass_write_fence=True)

    character_memory_dir = Path(cm.memory_dir) / character_name
    character_memory_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        character_memory_dir / "recent.json",
        [{"role": "user", "content": recent_message}],
        ensure_ascii=False,
        indent=2,
    )

    workshop_model_dir = Path(cm.workshop_dir) / "123456" / "example"
    workshop_model_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        workshop_model_dir / "example.model3.json",
        {"Version": 3},
        ensure_ascii=False,
        indent=2,
    )


def _run_launcher_phase0(cm):
    from launcher_core import runtime as launcher

    emitted_events = []
    with patch.object(launcher, "get_config_manager", lambda _app_name, **_kwargs: cm), patch.object(
        launcher,
        "emit_frontend_event",
        lambda event_type, payload=None: emitted_events.append((event_type, payload)),
    ):
        result = launcher._prepare_cloudsave_runtime_for_launch()
    return result, emitted_events


def _build_in_memory_steam_bridge(storage: dict[str, bytes]):
    class _Bridge:
        def cloud_enabled(self) -> bool:
            return True

        def file_exists(self, remote_name: str) -> bool:
            return remote_name in storage

        def read_file(self, remote_name: str) -> bytes:
            if remote_name not in storage:
                raise FileNotFoundError(remote_name)
            return storage[remote_name]

        def write_file(self, remote_name: str, payload: bytes) -> None:
            storage[remote_name] = payload

        def delete_file(self, remote_name: str) -> bool:
            return storage.pop(remote_name, None) is not None

    @contextlib.contextmanager
    def _fake_bridge(*, steamworks=None):
        del steamworks
        yield _Bridge()

    return _fake_bridge


@pytest.mark.unit
def test_launcher_phase0_skips_import_when_cloud_snapshot_is_empty():
    with TemporaryDirectory() as td:
        cm = _make_config_manager(Path(td))
        bootstrap_local_cloudsave_environment(cm)

        result, emitted_events = _run_launcher_phase0(cm)

        assert result["import_result"]["action"] == "skipped"
        assert result["import_result"]["reason"] == "no_snapshot"
        assert emitted_events[-1][0] == "cloudsave_bootstrap_ready"
        event_import_result = emitted_events[-1][1]["import_result"]
        assert event_import_result["action"] == "skipped"
        assert event_import_result["requested_reason"] == "launcher_phase0_prelaunch_import"
        assert "reason" not in event_import_result


@pytest.mark.unit
def test_launcher_phase0_skips_import_when_snapshot_is_already_applied():
    with TemporaryDirectory() as td:
        cm = _make_config_manager(Path(td))
        bootstrap_local_cloudsave_environment(cm)
        _write_runtime_state(cm, character_name="已应用角色", recent_message="已应用快照")
        export_cloudsave_character_unit(cm, "已应用角色")

        result, emitted_events = _run_launcher_phase0(cm)

        assert result["import_result"]["action"] == "skipped"
        assert result["import_result"]["reason"] == "already_applied"
        status = result["import_result"]["status"]
        assert status["has_snapshot"] is True
        assert status["runtime_has_user_content"] is True
        assert status["last_applied_manifest_fingerprint"] == status["manifest_fingerprint"]
        assert emitted_events[-1][0] == "cloudsave_bootstrap_ready"


@pytest.mark.unit
def test_cloudsave_lifecycle_round_trip_across_two_devices():
    with TemporaryDirectory() as td:
        device_a = _make_config_manager(Path(td) / "device_a")
        device_b = _make_config_manager(Path(td) / "device_b")
        bootstrap_local_cloudsave_environment(device_a)
        bootstrap_local_cloudsave_environment(device_b)

        _write_runtime_state(device_a, character_name="设备A角色", recent_message="来自设备A")
        character_a = device_a.load_characters()["当前猫娘"]
        export_a = export_cloudsave_character_unit(device_a, character_a)
        assert export_a["character_name"] == character_a

        shutil.copytree(device_a.cloudsave_dir, device_b.cloudsave_dir, dirs_exist_ok=True)
        startup_b, _ = _run_launcher_phase0(device_b)
        assert startup_b["import_result"]["action"] == "imported"
        assert device_b.load_characters()["当前猫娘"] == "设备A角色"

        _write_runtime_state(device_b, character_name="设备B角色", recent_message="来自设备B")
        character_b = device_b.load_characters()["当前猫娘"]
        export_b = export_cloudsave_character_unit(device_b, character_b)
        assert export_b["character_name"] == character_b

        shutil.copytree(device_b.cloudsave_dir, device_a.cloudsave_dir, dirs_exist_ok=True)
        startup_a_again, _ = _run_launcher_phase0(device_a)
        assert startup_a_again["import_result"]["action"] == "skipped"
        assert startup_a_again["import_result"]["reason"] == "manual_download_required"
        manual_download = import_cloudsave_character_unit(device_a, "设备B角色")
        assert manual_download["character_name"] == "设备B角色"
        assert "设备B角色" in (device_a.load_characters().get("猫娘") or {})
        assert device_a.load_cloudsave_local_state()["last_successful_import_at"]


@pytest.mark.unit
def test_full_cloudsave_chain_runtime_snapshot_steam_cloud_and_manual_apply():
    with TemporaryDirectory() as td:
        device_a = _make_config_manager(Path(td) / "device_a")
        device_b = _make_config_manager(Path(td) / "device_b")
        bootstrap_local_cloudsave_environment(device_a)
        bootstrap_local_cloudsave_environment(device_b)

        # Device A: runtime truth -> user manual snapshot upload.
        _write_runtime_state(device_a, character_name="跨端角色", recent_message="来自设备A-v1")
        upload_a = export_cloudsave_character_unit(device_a, "跨端角色", overwrite=True)
        assert upload_a["character_name"] == "跨端角色"

        manifest_a = json.loads(device_a.cloudsave_manifest_path.read_text(encoding="utf-8"))
        assert manifest_a["fingerprint"]

        # Simulate Steam cloud on app close: upload local staged cloudsave to remote.
        remote_storage: dict[str, bytes] = {}
        fake_bridge = _build_in_memory_steam_bridge(remote_storage)
        with patch("utils.steam_cloud_bundle.is_source_launch", return_value=True), patch(
            "utils.steam_cloud_bundle.sys.platform",
            "win32",
        ), patch("utils.steam_cloud_bundle.steam_cloud_bundle_bridge", fake_bridge):
            steam_upload = upload_cloudsave_bundle_to_steam(device_a)

        assert steam_upload["success"] is True
        assert steam_upload["action"] == "uploaded"
        assert REMOTE_BUNDLE_FILENAME in remote_storage
        assert REMOTE_META_FILENAME in remote_storage
        remote_meta_v1 = json.loads(remote_storage[REMOTE_META_FILENAME].decode("utf-8"))
        assert remote_meta_v1["manifest_fingerprint"] == manifest_a["fingerprint"]

        # Device B keeps local runtime content so startup should not auto-apply.
        _write_runtime_state(device_b, character_name="设备B本地角色", recent_message="设备B本地旧值")
        assert "跨端角色" not in (device_b.load_characters().get("猫娘") or {})

        # Simulate Steam cloud on app start: download remote staged cloudsave to local snapshot.
        with patch("utils.steam_cloud_bundle.is_source_launch", return_value=True), patch(
            "utils.steam_cloud_bundle.sys.platform",
            "win32",
        ), patch("utils.steam_cloud_bundle.steam_cloud_bundle_bridge", fake_bridge):
            steam_download = download_cloudsave_bundle_from_steam(device_b)

        assert steam_download["success"] is True
        assert steam_download["action"] == "downloaded"
        downloaded_manifest = json.loads(device_b.cloudsave_manifest_path.read_text(encoding="utf-8"))
        assert downloaded_manifest["fingerprint"] == manifest_a["fingerprint"]

        manager_b = CloudSaveManager(device_b)
        startup_b = manager_b.import_if_needed(reason="device_b_startup_after_steam_download")
        assert startup_b["action"] == "skipped"
        assert startup_b["reason"] == "manual_download_required"
        assert "跨端角色" not in (device_b.load_characters().get("猫娘") or {})

        # User manual apply: local snapshot -> runtime truth.
        apply_b = import_cloudsave_character_unit(device_b, "跨端角色")
        assert apply_b["character_name"] == "跨端角色"
        assert "跨端角色" in (device_b.load_characters().get("猫娘") or {})
        restored_recent = json.loads(
            (Path(device_b.memory_dir) / "跨端角色" / "recent.json").read_text(encoding="utf-8")
        )
        assert restored_recent[0]["content"] == "来自设备A-v1"

        # Device B updates runtime truth, then user manually uploads a new snapshot.
        _write_runtime_state(device_b, character_name="跨端角色", recent_message="来自设备B-v2")
        upload_b = export_cloudsave_character_unit(device_b, "跨端角色", overwrite=True)
        assert upload_b["character_name"] == "跨端角色"
        manifest_b = json.loads(device_b.cloudsave_manifest_path.read_text(encoding="utf-8"))
        assert manifest_b["fingerprint"]

        # Simulate Steam cloud upload after normal exit on Device B.
        with patch("utils.steam_cloud_bundle.is_source_launch", return_value=True), patch(
            "utils.steam_cloud_bundle.sys.platform",
            "win32",
        ), patch("utils.steam_cloud_bundle.steam_cloud_bundle_bridge", fake_bridge):
            steam_upload_v2 = upload_cloudsave_bundle_to_steam(device_b)

        assert steam_upload_v2["success"] is True
        assert steam_upload_v2["action"] == "uploaded"
        remote_meta_v2 = json.loads(remote_storage[REMOTE_META_FILENAME].decode("utf-8"))
        assert remote_meta_v2["manifest_fingerprint"] == manifest_b["fingerprint"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_manual_startup_performs_fallback_import_and_continues_boot():
    from app import main_server
    from utils import steam_state

    leaked_steamworks = SimpleNamespace(source="prior-test")
    main_server.steamworks = leaked_steamworks
    steam_state.set_steamworks(leaked_steamworks)

    fake_config_manager = SimpleNamespace(
        app_docs_dir=Path("/tmp/N.E.K.O"),
        load_root_state=Mock(return_value={"mode": "normal"}),
    )
    fake_import_result = {"success": True, "action": "imported"}
    mock_bootstrap = Mock()
    run_cloudsave_action = AsyncMock(return_value=fake_import_result)
    fake_tracker = SimpleNamespace(
        start_periodic_save=Mock(),
        record_app_start=Mock(),
    )
    bridge_start = AsyncMock(return_value=None)

    async def _fake_background_preload():
        return None

    class _DummyBridge:
        def __init__(self, on_agent_event):
            self.on_agent_event = on_agent_event

        async def start(self):
            await bridge_start()

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(main_server, "_IS_MAIN_PROCESS", True))
        stack.enter_context(patch.object(main_server, "_runtime_startup_init_completed", False))
        stack.enter_context(patch.object(main_server, "_preload_task", None))
        stack.enter_context(patch.object(main_server, "agent_event_bridge", None))
        stack.enter_context(patch.object(main_server, "steamworks", None))
        stack.enter_context(patch.object(main_server, "_config_manager", fake_config_manager))
        stack.enter_context(
            patch.object(main_server, "get_storage_startup_blocking_reason", Mock(return_value=""))
        )
        stack.enter_context(patch.object(main_server, "_run_cloudsave_manager_action", run_cloudsave_action))
        stack.enter_context(patch.object(main_server, "bootstrap_local_cloudsave_environment", mock_bootstrap))
        mock_init_chars = stack.enter_context(
            patch.object(main_server, "initialize_character_data", AsyncMock(return_value=None))
        )
        mock_sync_reload = stack.enter_context(
            patch.object(main_server, "_sync_memory_server_after_startup_import", AsyncMock(return_value=None))
        )
        mock_set_root_mode = stack.enter_context(
            patch.object(main_server, "set_root_mode", Mock(return_value={"mode": "normal"}))
        )
        mock_init_steam = stack.enter_context(
            patch.object(main_server, "initialize_steamworks", Mock(return_value=None))
        )
        mock_default_steam_info = stack.enter_context(
            patch.object(main_server, "get_default_steam_info", Mock())
        )
        stack.enter_context(patch.object(main_server, "_background_preload", _fake_background_preload))
        stack.enter_context(patch.object(main_server, "MainServerAgentBridge", _DummyBridge))
        mock_set_main_bridge = stack.enter_context(
            patch.object(main_server, "set_main_bridge", Mock())
        )
        mock_mount_workshop = stack.enter_context(
            patch.object(main_server, "_init_and_mount_workshop", AsyncMock(return_value=None))
        )
        mock_start_workers = stack.enter_context(
            patch.object(main_server, "_start_neko_servers_integration_workers", Mock())
        )
        mock_set_steamworks = stack.enter_context(
            patch("main_routers.shared_state.set_steamworks", Mock())
        )
        stack.enter_context(patch("utils.token_tracker.install_hooks", Mock()))
        stack.enter_context(
            patch("utils.token_tracker.TokenTracker.get_instance", return_value=fake_tracker)
        )
        stack.enter_context(
            patch("utils.language_utils.initialize_global_language", Mock(return_value="zh-CN"))
        )
        await main_server.on_startup()
        await asyncio.sleep(0)
        mock_bootstrap.assert_called_once_with(fake_config_manager)
        run_cloudsave_action.assert_awaited_once_with(
            "import_if_needed",
            reason="main_server_startup",
            budget_seconds=10.0,
        )
        mock_init_chars.assert_awaited_once_with()
        mock_sync_reload.assert_awaited_once_with(fake_import_result)
        mock_set_root_mode.assert_called_once()
        mock_init_steam.assert_called_once_with()
        # set_steamworks is wired twice on startup: init_shared_state seeds the
        # shared registry with the current (None) handle, then runtime init
        # publishes the freshly initialized handle. Under this mock
        # initialize_steamworks returns None, so both calls carry None.
        assert mock_set_steamworks.call_args_list == [call(None), call(None)]
        mock_default_steam_info.assert_called_once_with()
        bridge_start.assert_awaited_once_with()
        mock_set_main_bridge.assert_called_once()
        mock_mount_workshop.assert_awaited_once_with()
        mock_start_workers.assert_called_once_with()
        fake_tracker.start_periodic_save.assert_called_once_with()
        fake_tracker.record_app_start.assert_called_once_with(process="main_server")
        assert main_server._preload_task is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_shutdown_does_not_reexport_runtime_into_cloudsave_snapshot():
    from app import main_server

    fake_tracker = SimpleNamespace(save=Mock())

    with patch.object(main_server, "_IS_MAIN_PROCESS", True), \
         patch.object(main_server, "_preload_task", None), \
         patch.object(main_server, "agent_event_bridge", None), \
         patch.object(main_server.character_runtime, "role_state", _role_state_from_session_managers({})), \
         patch.object(main_server, "_run_cloudsave_manager_action", AsyncMock()) as run_cloudsave_action, \
         patch("utils.music_crawlers.close_all_crawlers", AsyncMock(return_value=None)), \
         patch("utils.token_tracker.TokenTracker.get_instance", return_value=fake_tracker):
        await main_server.on_shutdown()

    fake_tracker.save.assert_called_once_with()
    run_cloudsave_action.assert_awaited_once_with(
        "upload_existing_snapshot",
        reason="main_server_shutdown_remote_upload",
        budget_seconds=5.0,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_startup_does_not_mark_normal_when_character_init_fails():
    from app import main_server

    fake_config_manager = SimpleNamespace(
        app_docs_dir=Path("/tmp/neko"),
    )
    run_cloudsave_action = AsyncMock(return_value={"success": True, "action": "imported"})
    mock_set_root_mode = Mock()

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(main_server, "_runtime_startup_init_completed", False))
        stack.enter_context(patch.object(main_server, "_config_manager", fake_config_manager))
        stack.enter_context(patch.object(main_server, "_run_cloudsave_manager_action", run_cloudsave_action))
        stack.enter_context(patch.object(main_server, "bootstrap_local_cloudsave_environment", Mock()))
        stack.enter_context(
            patch.object(main_server, "initialize_character_data", AsyncMock(side_effect=RuntimeError("character init failed")))
        )
        stack.enter_context(patch.object(main_server, "set_root_mode", mock_set_root_mode))

        with pytest.raises(RuntimeError, match="character init failed"):
            await main_server._ensure_main_server_runtime_initialized(reason="unit_test")

    mock_set_root_mode.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_startup_aborts_when_root_mode_persist_fails():
    from app import main_server

    fake_config_manager = SimpleNamespace(
        app_docs_dir=Path("/tmp/neko"),
        load_root_state=Mock(return_value={"mode": "normal"}),
    )
    fake_import_result = {"success": True, "action": "imported"}
    run_cloudsave_action = AsyncMock(return_value=fake_import_result)
    fake_tracker = SimpleNamespace(
        start_periodic_save=Mock(),
        record_app_start=Mock(),
    )
    bridge_start = AsyncMock(return_value=None)

    async def _fake_background_preload():
        return None

    class _DummyBridge:
        def __init__(self, on_agent_event):
            self.on_agent_event = on_agent_event

        async def start(self):
            await bridge_start()

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(main_server, "_IS_MAIN_PROCESS", True))
        stack.enter_context(patch.object(main_server, "_runtime_startup_init_completed", False))
        stack.enter_context(patch.object(main_server, "_preload_task", None))
        stack.enter_context(patch.object(main_server, "agent_event_bridge", None))
        stack.enter_context(patch.object(main_server, "_config_manager", fake_config_manager))
        stack.enter_context(
            patch.object(main_server, "get_storage_startup_blocking_reason", Mock(return_value=""))
        )
        stack.enter_context(patch.object(main_server, "_run_cloudsave_manager_action", run_cloudsave_action))
        stack.enter_context(patch.object(main_server, "bootstrap_local_cloudsave_environment", Mock()))
        stack.enter_context(
            patch.object(main_server, "set_root_mode", Mock(side_effect=RuntimeError("root write failed")))
        )
        mock_init_chars = stack.enter_context(
            patch.object(main_server, "initialize_character_data", AsyncMock(return_value=None))
        )
        mock_sync_reload = stack.enter_context(
            patch.object(main_server, "_sync_memory_server_after_startup_import", AsyncMock(return_value=None))
        )
        mock_init_steam = stack.enter_context(
            patch.object(main_server, "initialize_steamworks", Mock(return_value=None))
        )
        stack.enter_context(
            patch.object(main_server, "get_default_steam_info", Mock())
        )
        stack.enter_context(patch.object(main_server, "_background_preload", _fake_background_preload))
        stack.enter_context(patch.object(main_server, "MainServerAgentBridge", _DummyBridge))
        stack.enter_context(
            patch.object(main_server, "set_main_bridge", Mock())
        )
        mock_mount_workshop = stack.enter_context(
            patch.object(main_server, "_init_and_mount_workshop", AsyncMock(return_value=None))
        )
        stack.enter_context(
            patch("main_routers.shared_state.set_steamworks", Mock())
        )
        stack.enter_context(patch("utils.token_tracker.install_hooks", Mock()))
        stack.enter_context(
            patch("utils.token_tracker.TokenTracker.get_instance", return_value=fake_tracker)
        )
        stack.enter_context(
            patch("utils.language_utils.initialize_global_language", Mock(return_value="zh-CN"))
        )
        with pytest.raises(RuntimeError, match="failed to persist ROOT_MODE_NORMAL"):
            await main_server.on_startup()

    mock_init_chars.assert_awaited_once_with()
    mock_sync_reload.assert_awaited_once_with(fake_import_result)
    mock_init_steam.assert_called_once_with()
    mock_mount_workshop.assert_awaited_once_with()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_startup_stays_limited_when_storage_barrier_is_blocking():
    from app import main_server
    from main_routers import shared_state

    sentinel_templates = object()

    with patch.dict(shared_state._state, {"templates": shared_state._UNSET}), \
         patch.object(main_server, "_IS_MAIN_PROCESS", True), \
         patch.object(main_server, "templates", sentinel_templates), \
         patch.object(main_server, "role_state", {}), \
         patch.object(main_server, "_config_manager", SimpleNamespace()), \
         patch.object(main_server, "get_storage_startup_blocking_reason", Mock(return_value="selection_required")), \
         patch.object(main_server, "_start_neko_servers_integration_workers", Mock()) as mock_start_workers, \
         patch.object(main_server, "_ensure_main_server_runtime_initialized", AsyncMock()) as mock_ensure_runtime:
        await main_server.on_startup()
        assert shared_state.get_templates() is sentinel_templates

    mock_ensure_runtime.assert_not_awaited()
    mock_start_workers.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_release_storage_startup_barrier_starts_integration_workers_after_runtime_init():
    from app import main_server

    with patch.object(
        main_server,
        "_request_memory_server_continue_startup",
        AsyncMock(return_value=None),
    ) as mock_continue, patch.object(
        main_server,
        "_ensure_main_server_runtime_initialized",
        AsyncMock(return_value=True),
    ) as mock_ensure, patch.object(
        main_server,
        "_start_neko_servers_integration_workers",
        Mock(),
    ) as mock_start_workers:
        result = await main_server.release_storage_startup_barrier(reason="unit_test")

    assert result == {"ok": True, "initialized": True}
    mock_continue.assert_awaited_once_with("unit_test")
    mock_ensure.assert_awaited_once_with(reason="unit_test")
    mock_start_workers.assert_called_once_with()


@pytest.mark.unit
def test_facts_sync_worker_start_failure_is_logged_as_warning():
    from app import main_server

    facts_error = RuntimeError("facts worker failed")
    create_task = Mock(side_effect=facts_error)

    with (
        patch(
            "main_logic.facts_sync.start_facts_sync_worker",
            Mock(return_value=object()),
        ),
        patch(
            "main_logic.client_registration.ensure_client_registered",
            Mock(return_value=object()),
        ),
        patch.object(main_server.asyncio, "create_task", create_task),
        patch.object(main_server.logger, "warning") as warning,
        patch.object(main_server, "_facts_sync_worker_task", None),
        patch.object(main_server, "_client_registration_task", None),
    ):
        main_server._start_neko_servers_integration_workers()

    # Both workers share the same create_task mock, both log warnings
    assert warning.call_count == 2
    warning.assert_any_call(
        "[client_registration] bootstrap failed: %s",
        facts_error,
    )
    warning.assert_any_call(
        "[facts_sync] start worker failed: %s",
        facts_error,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_integration_workers_cancels_facts_sync_task():
    from app import main_server

    async def wait_forever():
        await asyncio.Event().wait()

    facts_task = asyncio.create_task(wait_forever())
    await asyncio.sleep(0)

    with patch.object(main_server, "_facts_sync_worker_task", facts_task):
        await main_server._stop_neko_servers_integration_workers()
        assert main_server._facts_sync_worker_task is None

    assert facts_task.cancelled()


@pytest.mark.unit
@pytest.mark.parametrize(
    "method",
    ("GET", "POST", "OPTIONS"),
)
def test_card_drop_active_character_is_allowed_during_limited_mode(method):
    from app import main_server

    assert main_server._is_main_limited_mode_allowed_path(
        "/api/card-drop/active-character",
        method,
    )


@pytest.mark.unit
def test_main_server_limited_mode_middleware_blocks_runtime_routes():
    from app import main_server

    with patch.object(main_server, "_IS_MAIN_PROCESS", False), \
         patch.object(main_server, "_runtime_startup_init_completed", False), \
         patch.object(main_server, "_main_runtime_limited_mode_enabled", True), \
         patch.object(main_server, "_main_runtime_limited_mode_reason", "selection_required"):
        with TestClient(main_server.app) as client:
            blocked_response = client.get("/api/config/page_config")
            health_response = client.get("/health")
            steam_language_response = client.get("/api/config/steam_language")
            active_character_response = client.get(
                "/api/card-drop/active-character"
            )

    assert blocked_response.status_code == 409
    payload = blocked_response.json()
    assert payload["error_code"] == "storage_startup_blocked"
    assert payload["blocking_reason"] == "selection_required"
    assert payload["limited_mode"] is True
    assert health_response.status_code == 200
    assert steam_language_response.status_code == 200
    assert "uiLanguage" in steam_language_response.json()
    assert active_character_response.status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize(
    ("encoded_path", "escaped_control", "raw_control"),
    [
        ("/blocked/%1B%5B31m", r"\x1b", "\x1b"),
        ("/blocked/%00", r"\x00", "\x00"),
        ("/blocked/%C2%85", r"\x85", "\x85"),
        ("/blocked/%E2%80%A8", r"\u2028", "\u2028"),
    ],
)
def test_main_server_limited_mode_log_escapes_control_characters(
    encoded_path,
    escaped_control,
    raw_control,
):
    from app import main_server

    with (
        patch.object(main_server, "_IS_MAIN_PROCESS", False),
        patch.object(main_server, "_runtime_startup_init_completed", False),
        patch.object(main_server, "_main_runtime_limited_mode_enabled", True),
        patch.object(
            main_server,
            "_main_runtime_limited_mode_reason",
            "selection_required",
        ),
        patch.object(main_server.logger, "info") as mock_info,
    ):
        with TestClient(main_server.app) as client:
            response = client.get(encoded_path)

    log_call = next(
        call
        for call in mock_info.call_args_list
        if call.args
        and call.args[0].startswith("[Main] limited-mode blocks request path=")
    )
    rendered = log_call.args[0] % log_call.args[1:]

    assert response.status_code == 409
    assert raw_control not in rendered
    assert escaped_control in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_release_storage_startup_barrier_restores_memory_limited_mode_when_main_init_fails():
    from app import main_server

    init_error = RuntimeError("main init failed")
    with patch.object(main_server, "_request_memory_server_continue_startup", AsyncMock(return_value=None)) as mock_continue, \
         patch.object(main_server, "_ensure_main_server_runtime_initialized", AsyncMock(side_effect=init_error)) as mock_ensure, \
         patch.object(main_server, "_request_memory_server_block_startup", AsyncMock(return_value=None)) as mock_block, \
         patch.object(main_server, "_main_runtime_limited_mode_enabled", False), \
         patch.object(main_server, "_main_runtime_limited_mode_reason", ""):
        with pytest.raises(RuntimeError, match="main init failed"):
            await main_server.release_storage_startup_barrier(reason="unit_test")
        assert main_server._main_runtime_limited_mode_enabled is True
        assert main_server._main_runtime_limited_mode_reason == "runtime_initialization_failed"

    mock_continue.assert_awaited_once_with("unit_test")
    mock_ensure.assert_awaited_once_with(reason="unit_test")
    mock_block.assert_awaited_once_with("unit_test:main_server_init_failed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_release_storage_startup_barrier_reblocks_memory_before_propagating_cancellation():
    """Cancellation must not expose initialized memory against rolled-back storage."""
    from app import main_server

    init_started = asyncio.Event()
    block_started = asyncio.Event()
    allow_block = asyncio.Event()

    async def _wait_for_cancel(*, reason: str):
        init_started.set()
        await asyncio.Event().wait()

    async def _block_memory(reason: str):
        block_started.set()
        await allow_block.wait()

    with patch.object(
        main_server,
        "_request_memory_server_continue_startup",
        AsyncMock(return_value=None),
    ), patch.object(
        main_server,
        "_ensure_main_server_runtime_initialized",
        side_effect=_wait_for_cancel,
    ), patch.object(
        main_server,
        "_request_memory_server_block_startup",
        side_effect=_block_memory,
    ) as mock_block, patch.object(
        main_server,
        "_main_runtime_limited_mode_enabled",
        False,
    ), patch.object(
        main_server,
        "_main_runtime_limited_mode_reason",
        "",
    ):
        task = asyncio.create_task(
            main_server.release_storage_startup_barrier(reason="unit_test")
        )
        await init_started.wait()
        task.cancel()
        await block_started.wait()

        # A second cancellation must not let the storage router roll back its
        # blocking snapshot before memory_server has restored its own guard.
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        allow_block.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    mock_block.assert_awaited_once_with("unit_test:main_server_init_failed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_release_storage_startup_barrier_compensates_ambiguous_continue_cancellation():
    """A cancelled continue response may hide an already-applied server request."""
    from app import main_server

    continue_applied = asyncio.Event()
    block_started = asyncio.Event()

    async def _continue_without_response(reason: str):
        continue_applied.set()
        await asyncio.Event().wait()

    async def _block_memory(reason: str):
        block_started.set()

    with patch.object(
        main_server,
        "_request_memory_server_continue_startup",
        side_effect=_continue_without_response,
    ), patch.object(
        main_server,
        "_ensure_main_server_runtime_initialized",
        AsyncMock(),
    ) as mock_ensure, patch.object(
        main_server,
        "_request_memory_server_block_startup",
        side_effect=_block_memory,
    ) as mock_block, patch.object(
        main_server,
        "_main_runtime_limited_mode_enabled",
        False,
    ), patch.object(
        main_server,
        "_main_runtime_limited_mode_reason",
        "",
    ):
        task = asyncio.create_task(
            main_server.release_storage_startup_barrier(reason="unit_test")
        )
        await continue_applied.wait()
        task.cancel()

        [outcome] = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError)

        assert block_started.is_set()
        assert main_server._main_runtime_limited_mode_enabled is True
        assert main_server._main_runtime_limited_mode_reason == "runtime_initialization_failed"

    mock_ensure.assert_not_awaited()
    mock_block.assert_awaited_once_with("unit_test:main_server_init_failed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_server_continue_startup_preserves_409_blocking_payload():
    import httpx
    from app import main_server

    payload = {
        "ok": False,
        "error_code": "storage_startup_blocked",
        "blocking_reason": "migration_pending",
    }

    class _Client:
        async def post(self, *args, **kwargs):
            return httpx.Response(
                409,
                json=payload,
                request=httpx.Request("POST", "http://127.0.0.1/internal/storage/startup/continue"),
            )

    with patch("utils.internal_http_client.get_internal_http_client", return_value=_Client()):
        with pytest.raises(main_server.MemoryServerStartupBlocked) as exc_info:
            await main_server._request_memory_server_continue_startup("unit_test")

    assert exc_info.value.payload == payload
    assert exc_info.value.blocking_reason == "migration_pending"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_server_startup_stays_limited_when_storage_barrier_is_blocking():
    from app import memory_server

    with patch.object(memory_server.runtime, "_config_manager", SimpleNamespace()), \
         patch.object(memory_server.runtime, "get_storage_startup_blocking_reason", Mock(return_value="selection_required")), \
         patch.object(memory_server.runtime, "ensure_memory_server_runtime_initialized", AsyncMock()) as mock_ensure_runtime:
        await memory_server.startup_event_handler()

    mock_ensure_runtime.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_server_continue_startup_refuses_active_storage_barrier():
    from app import memory_server

    with patch.object(memory_server.runtime, "_config_manager", SimpleNamespace()), \
         patch.object(memory_server.runtime, "get_storage_startup_blocking_reason", Mock(return_value="migration_pending")), \
         patch.object(memory_server.runtime, "ensure_memory_server_runtime_initialized", AsyncMock()) as mock_ensure_runtime:
        response = await memory_server.continue_storage_startup(None)

    assert response.status_code == 409
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["ok"] is False
    assert payload["blocking_reason"] == "migration_pending"
    mock_ensure_runtime.assert_not_awaited()


@pytest.mark.unit
def test_memory_server_limited_mode_middleware_blocks_runtime_routes():
    from app import memory_server

    with patch.object(memory_server.runtime, "_config_manager", SimpleNamespace()), \
         patch.object(memory_server.runtime, "get_storage_startup_blocking_reason", Mock(return_value="selection_required")):
        with TestClient(memory_server.app) as client:
            response = client.get("/get_settings/小满")

    assert response.status_code == 409
    payload = response.json()
    assert payload["error_code"] == "storage_startup_blocked"
    assert payload["blocking_reason"] == "selection_required"
    assert payload["limited_mode"] is True


@pytest.mark.unit
def test_memory_server_limited_mode_middleware_blocks_until_runtime_init_completes():
    from app import memory_server

    with patch.object(memory_server.runtime, "_config_manager", SimpleNamespace()), \
         patch.object(memory_server.runtime, "_memory_runtime_init_completed", False), \
         patch.object(memory_server.runtime, "get_storage_startup_blocking_reason", Mock(side_effect=["selection_required", ""])):
        with TestClient(memory_server.app) as client:
            response = client.get("/get_settings/小满")

    assert response.status_code == 409
    payload = response.json()
    assert payload["error_code"] == "storage_startup_blocked"
    assert payload["blocking_reason"] == "runtime_initializing"
    assert payload["limited_mode"] is True


@pytest.mark.unit
def test_memory_server_block_startup_endpoint_restores_limited_mode():
    from app import memory_server

    with patch.object(memory_server.runtime, "_memory_runtime_init_completed", True), \
         patch.object(memory_server.runtime, "_memory_storage_blocked_after_init", False), \
         patch.object(memory_server.runtime, "get_storage_startup_blocking_reason", Mock(return_value="")):
        with TestClient(memory_server.app) as client:
            response = client.post(
                "/internal/storage/startup/block",
                json={"reason": "main_failed"},
            )
            blocked_response = client.get("/get_settings/小满")
            runtime_completed_during_block = memory_server.runtime._memory_runtime_init_completed

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["limited_mode"] is True
    assert payload["reason"] == "main_failed"
    assert blocked_response.status_code == 409
    assert blocked_response.json()["blocking_reason"] == "storage_startup_blocked_after_init"
    assert runtime_completed_during_block is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_cancel_workshop_background_tasks_uses_public_api():
    from app import main_server

    calls = []
    workshop_module = SimpleNamespace(
        cancel_background_tasks=AsyncMock(side_effect=lambda *, timeout: calls.append(timeout)),
        _ugc_warmup_task=None,
        _ugc_sync_task=None,
    )

    with patch.object(main_server.importlib, "import_module", return_value=workshop_module):
        await main_server._cancel_workshop_background_tasks(timeout=2.5)

    assert calls == [2.5]
    workshop_module.cancel_background_tasks.assert_awaited_once_with(timeout=2.5)


@pytest.mark.unit
def test_main_server_resets_sync_shutdown_events_after_startup_rollback():
    """``_reset_sync_connector_shutdown_events`` 现在是 no-op：cross_server 改成
    主 loop 上的 asyncio.Task 后，没有 ThreadEvent 可以 reset。函数保留是为了
    避免改动文件内众多调用点，本测试只验证它仍可调用且不会抛异常。
    """
    from app import main_server
    from app.main_server import _SyncMessageQueue

    role_state = {
        "小满": main_server.RoleState(
            sync_message_queue=_SyncMessageQueue(),
            websocket_lock=asyncio.Lock(),
        )
    }

    with patch.object(main_server.character_runtime, "role_state", role_state):
        # 不抛异常即视为通过；旧版会清 threading.Event，新版无状态可清。
        # 显式断言返回值为 None，未来若改成有副作用返回时能更早暴露。
        assert main_server._reset_sync_connector_shutdown_events() is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_shutdown_releases_live_sessions_then_uploads_existing_snapshot():
    from app import main_server

    fake_tracker = SimpleNamespace(save=Mock())
    run_cloudsave_action = AsyncMock(return_value={"success": True, "action": "uploaded"})
    manager_with_resampler = SimpleNamespace(
        audio_resampler=object(),
        aclose=AsyncMock(),
    )
    existing_steamworks = SimpleNamespace()

    with patch.object(main_server, "_IS_MAIN_PROCESS", True), \
         patch.object(main_server, "_preload_task", None), \
         patch.object(main_server, "agent_event_bridge", None), \
         patch.object(main_server, "steamworks", existing_steamworks), \
         patch.object(main_server.character_runtime, "role_state", _role_state_from_session_managers({"角色A": manager_with_resampler, "角色B": object(), "空槽": None})), \
         patch.object(main_server, "_run_cloudsave_manager_action", run_cloudsave_action), \
         patch("main_routers.characters_router.release_memory_server_character", AsyncMock(return_value=True)) as mock_release, \
         patch("utils.language_utils.aclose_translation_service", AsyncMock(return_value=None), create=True), \
         patch("utils.music_crawlers.close_all_crawlers", AsyncMock(return_value=None)), \
         patch("utils.token_tracker.TokenTracker.get_instance", return_value=fake_tracker):
        await main_server.on_shutdown()

    assert manager_with_resampler.audio_resampler is None
    manager_with_resampler.aclose.assert_awaited_once_with()
    run_cloudsave_action.assert_awaited_once_with(
        "upload_existing_snapshot",
        reason="main_server_shutdown_remote_upload",
        budget_seconds=5.0,
        steamworks=existing_steamworks,
    )
    assert mock_release.await_count == 2
    mock_release.assert_any_await("角色A", reason="Steam Auto-Cloud pre-shutdown release: 角色A")
    mock_release.assert_any_await("角色B", reason="Steam Auto-Cloud pre-shutdown release: 角色B")
    fake_tracker.save.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_shutdown_continues_when_memory_release_returns_false():
    from app import main_server

    fake_tracker = SimpleNamespace(save=Mock())
    with patch.object(main_server, "_IS_MAIN_PROCESS", True), \
         patch.object(main_server, "_preload_task", None), \
         patch.object(main_server, "agent_event_bridge", None), \
         patch.object(main_server, "steamworks", None), \
         patch.object(main_server.character_runtime, "role_state", _role_state_from_session_managers({"角色A": object(), "角色B": object()})), \
         patch.object(main_server, "_run_cloudsave_manager_action", AsyncMock()) as run_cloudsave_action, \
         patch("main_routers.characters_router.release_memory_server_character", AsyncMock(side_effect=[True, False])) as mock_release, \
         patch("utils.language_utils.aclose_translation_service", AsyncMock(return_value=None), create=True), \
         patch("utils.music_crawlers.close_all_crawlers", AsyncMock(return_value=None)), \
         patch("utils.token_tracker.TokenTracker.get_instance", return_value=fake_tracker), \
         patch.object(main_server.logger, "warning", Mock()) as mock_warning:
        await main_server.on_shutdown()

    assert mock_release.await_count == 2
    run_cloudsave_action.assert_not_awaited()
    mock_warning.assert_any_call(
        "Steam Auto-Cloud pre-shutdown release failed for %s: returned False; uploaded snapshot may be stale/incomplete",
        "角色B",
    )
    mock_warning.assert_any_call(
        "Steam Auto-Cloud shutdown staged snapshot upload skipped because pre-shutdown release failed for: %s",
        "角色B",
    )
    fake_tracker.save.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown_server_async_defers_memory_server_stop_until_main_shutdown():
    from app import main_server

    server = SimpleNamespace(should_exit=False)
    start_config = {
        "browser_mode_enabled": True,
        "browser_page": "",
        "shutdown_memory_server_on_exit": False,
        "server": server,
    }
    workshop_state = SimpleNamespace(_ugc_warmup_task=None, _ugc_sync_task=None)

    with patch.object(main_server.asyncio, "sleep", AsyncMock(return_value=None)), \
         patch.object(main_server, "get_start_config", Mock(return_value=start_config)), \
         patch.object(main_server.importlib, "import_module", return_value=workshop_state):
        await main_server.shutdown_server_async()

    assert start_config["shutdown_memory_server_on_exit"] is True
    assert server.should_exit is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_server_shutdown_requests_memory_server_stop_after_snapshot_upload_when_deferred():
    from app import main_server

    fake_tracker = SimpleNamespace(save=Mock())
    call_order = []
    start_config = {
        "browser_mode_enabled": True,
        "browser_page": "",
        "shutdown_memory_server_on_exit": True,
        "server": None,
    }

    async def _fake_request_shutdown():
        call_order.append("memory_shutdown")

    with patch.object(main_server, "_IS_MAIN_PROCESS", True), \
         patch.object(main_server, "_preload_task", None), \
         patch.object(main_server, "agent_event_bridge", None), \
         patch.object(main_server, "steamworks", None), \
         patch.object(main_server.character_runtime, "role_state", _role_state_from_session_managers({})), \
         patch.object(main_server, "_run_cloudsave_manager_action", AsyncMock()) as run_cloudsave_action, \
         patch.object(main_server, "get_start_config", Mock(return_value=start_config)), \
         patch.object(main_server, "_request_memory_server_shutdown", AsyncMock(side_effect=_fake_request_shutdown)) as mock_request_shutdown, \
         patch("utils.music_crawlers.close_all_crawlers", AsyncMock(return_value=None)), \
         patch("utils.token_tracker.TokenTracker.get_instance", return_value=fake_tracker):
        await main_server.on_shutdown()

    assert call_order == ["memory_shutdown"]
    run_cloudsave_action.assert_awaited_once_with(
        "upload_existing_snapshot",
        reason="main_server_shutdown_remote_upload",
        budget_seconds=5.0,
    )
    assert start_config["shutdown_memory_server_on_exit"] is False
    mock_request_shutdown.assert_awaited_once_with()
