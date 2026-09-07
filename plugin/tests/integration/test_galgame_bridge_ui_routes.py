from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from plugin._types.models import RunCreateResponse
from plugin.core.state import state
from plugin.server import install_registry as install_registry_module
from plugin.server.routes import _install_task_store as install_task_module
from plugin.server.routes import plugin_install as galgame_install_route_module
from plugin.runs.manager import RunError, RunRecord
from plugin.server.infrastructure.exceptions import register_exception_handlers
from plugin.server.domain.errors import ServerDomainError
from plugin.server.routes import plugin_ui as plugin_ui_route_module
from tests.fake_clock import patch_module_clock


pytestmark = pytest.mark.plugin_integration


@pytest.fixture
def galgame_plugin_dir(tmp_path: Path) -> Path:
    plugin_dir = tmp_path / "market" / "galgame_plugin"
    i18n_dir = plugin_dir / "i18n" / "ui"
    static_dir = plugin_dir / "static"
    i18n_dir.mkdir(parents=True)
    static_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """[plugin]
id = "galgame_plugin"
name = "Galgame Plugin"
version = "1.0.2"
type = "plugin"
entry = "plugin.plugins.galgame_plugin:GalgamePlugin"
""",
        encoding="utf-8",
    )
    for locale in ("en", "ja", "ko", "pt", "ru", "zh-CN", "zh-TW"):
        (i18n_dir / f"{locale}.json").write_text(
            json.dumps(
                {
                    "ui.button.collapse": f"Collapse ({locale})",
                    "ui.install.textractor.action": f"Install ({locale})",
                }
            ),
            encoding="utf-8",
        )
    (static_dir / "index.html").write_text(
        "<html><head><title>Market Galgame fixture</title></head>"
        '<body><script src="./i18n.js?v=fixture"></script></body></html>',
        encoding="utf-8",
    )
    (static_dir / "main.js").write_text(
        "window.marketGalgameFixture = true;\n",
        encoding="utf-8",
    )
    (static_dir / "i18n.js").write_text(
        "window.marketGalgameI18nFixture = true;\n",
        encoding="utf-8",
    )
    (static_dir / "style.css").write_text("body {}\n", encoding="utf-8")
    return plugin_dir


@pytest.fixture(autouse=True)
def registered_install_plugins(
    monkeypatch: pytest.MonkeyPatch,
    galgame_plugin_dir: Path,
    tmp_path: Path,
) -> Iterator[None]:
    with state.acquire_plugins_read_lock():
        plugins_backup = copy.deepcopy(state.plugins)
    monkeypatch.setattr(install_registry_module, "_install_plugin_registry", {})
    study_plugin_dir = tmp_path / "market" / "study_companion"
    study_i18n_dir = study_plugin_dir / "i18n"
    study_i18n_dir.mkdir(parents=True)
    (study_plugin_dir / "plugin.toml").write_text(
        """[plugin]
id = "study_companion"
name = "Study Companion"
version = "0.2.4"
type = "plugin"
entry = "plugin.plugins.study_companion:StudyCompanionPlugin"

[plugin.install]
enabled = true
ui_i18n_dir = "i18n"
tutorial_enabled = true

[plugin.install.kinds.rapidocr_models]
entry_id = "study_download_rapidocr_models"
label = "RapidOCR Models"
queued_message = "RapidOCR model download queued"
entry_timeout = 600.0
""",
        encoding="utf-8",
    )
    (study_i18n_dir / "en.json").write_text(
        json.dumps({"entries.open_ui.name": "Open Study Companion UI"}),
        encoding="utf-8",
    )
    with state.acquire_plugins_write_lock():
        state.plugins.clear()
        state.plugins.update(
            {
                "galgame_plugin": {
                    "id": "galgame_plugin",
                    "config_path": str(galgame_plugin_dir / "plugin.toml"),
                    "entries_preview": [
                        {"id": "galgame_install_textractor"},
                        {"id": "galgame_download_rapidocr_models"},
                    ],
                    "effective_source": "user",
                },
                "study_companion": {
                    "id": "study_companion",
                    "config_path": str(study_plugin_dir / "plugin.toml"),
                    "entries_preview": [
                        {"id": "study_download_rapidocr_models"},
                    ],
                    "effective_source": "user",
                },
            }
        )
    try:
        yield
    finally:
        with state.acquire_plugins_write_lock():
            state.plugins.clear()
            state.plugins.update(plugins_backup)


@pytest.fixture
def plugin_ui_test_app() -> FastAPI:
    app = FastAPI(title="plugin-ui-test-app")
    register_exception_handlers(app)
    app.include_router(plugin_ui_route_module.router)
    app.include_router(galgame_install_route_module.router)
    return app


@pytest.fixture
async def plugin_ui_async_client(plugin_ui_test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=plugin_ui_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def galgame_install_runtime_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    app_docs_dir = tmp_path / "AppDocs"
    monkeypatch.setattr(
        install_task_module,
        "get_config_manager",
        lambda: SimpleNamespace(app_docs_dir=app_docs_dir),
    )
    return app_docs_dir


@pytest.fixture
def tutorial_runtime_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    runtime_root = tmp_path / "RuntimeData"
    monkeypatch.setattr(
        galgame_install_route_module,
        "resolve_runtime_data_root",
        lambda: runtime_root,
    )
    if isinstance(getattr(install_registry_module, "_tutorial_migration_hooks", None), dict):
        monkeypatch.setattr(install_registry_module, "_tutorial_migration_hooks", {})
    else:
        monkeypatch.setattr(install_registry_module, "_tutorial_migration_hooks", [])
    monkeypatch.setattr(galgame_install_route_module, "_tutorial_migrated_paths", set(), raising=False)
    monkeypatch.setattr(
        galgame_install_route_module,
        "_galgame_tutorial_migration_retry_after",
        {},
        raising=False,
    )
    monkeypatch.setattr(galgame_install_route_module, "_tutorial_store_instance", None, raising=False)
    monkeypatch.setattr(galgame_install_route_module, "_tutorial_store_instances", {}, raising=False)
    return runtime_root


@pytest.fixture
def registered_galgame_plugin_meta(galgame_plugin_dir: Path) -> Iterator[None]:
    plugins_backup = copy.deepcopy(state.plugins)
    try:
        with state.acquire_plugins_write_lock():
            galgame_meta = dict(state.plugins.get("galgame_plugin") or {})
            galgame_meta.update({
                "id": "galgame_plugin",
                "name": "Galgame Plugin",
                "config_path": str(galgame_plugin_dir / "plugin.toml"),
                "static_ui_config": {
                    "enabled": True,
                    "directory": str(galgame_plugin_dir / "static"),
                    "index_file": "index.html",
                    "cache_control": "no-store, no-cache, must-revalidate, max-age=0",
                    "plugin_id": "galgame_plugin",
                },
                "list_actions": [
                    {
                        "id": "open_ui",
                        "kind": "ui",
                        "target": "/plugin/galgame_plugin/ui/",
                        "open_in": "new_tab",
                    }
                ],
            })
            state.plugins["galgame_plugin"] = galgame_meta
        yield
    finally:
        with state.acquire_plugins_write_lock():
            state.plugins.clear()
            state.plugins.update(plugins_backup)


def _running_install_run(
    run_id: str,
    *,
    entry_id: str,
    stage: str,
    message: str,
    now: float | None = None,
) -> RunRecord:
    now = time.time() if now is None else now
    return RunRecord(
        run_id=run_id,
        plugin_id="galgame_plugin",
        entry_id=entry_id,
        status="running",
        created_at=now - 5,
        updated_at=now,
        started_at=now - 4,
        finished_at=None,
        stage=stage,
        message=message,
        error=None,
        metrics={},
    )


def _terminal_install_run(
    run_id: str,
    *,
    entry_id: str,
    status: str = "succeeded",
    stage: str = "completed",
    message: str = "Install completed",
    now: float | None = None,
) -> RunRecord:
    now = time.time() if now is None else now
    return RunRecord(
        run_id=run_id,
        plugin_id="galgame_plugin",
        entry_id=entry_id,
        status=status,
        created_at=now - 5,
        updated_at=now,
        started_at=now - 4,
        finished_at=now,
        stage=stage,
        message=message,
        error=None,
        metrics={},
    )


@pytest.mark.asyncio
async def test_galgame_plugin_ui_index_route_serves_static_dashboard(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
) -> None:
    response = await plugin_ui_async_client.get("/plugin/galgame_plugin/ui/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert "Market Galgame fixture" in response.text
    assert "./i18n.js?v=fixture" in response.text


@pytest.mark.asyncio
async def test_galgame_plugin_ui_script_is_served_from_selected_market_source(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
) -> None:
    response = await plugin_ui_async_client.get("/plugin/galgame_plugin/ui/main.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "window.marketGalgameFixture = true" in response.text


@pytest.mark.asyncio
async def test_galgame_plugin_ui_i18n_script_is_served(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
) -> None:
    response = await plugin_ui_async_client.get("/plugin/galgame_plugin/ui/i18n.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "window.marketGalgameI18nFixture = true" in response.text


@pytest.mark.asyncio
async def test_galgame_plugin_ui_i18n_api_serves_locale_bundle(
    plugin_ui_async_client: AsyncClient,
) -> None:
    locale_response = await plugin_ui_async_client.get("/plugin/galgame_plugin/ui-api/locale")
    assert locale_response.status_code == 200
    locale = locale_response.json()["locale"]
    assert isinstance(locale, str)

    bundle_response = await plugin_ui_async_client.get(
        f"/plugin/galgame_plugin/ui-api/i18n/ui/{locale}.json"
    )
    assert bundle_response.status_code == 200
    assert "application/json" in bundle_response.headers["content-type"]
    bundle = bundle_response.json()
    assert bundle["ui.button.collapse"]
    # `ui.install.rapidocr.action` removed (no in-app install action). Use a
    # remaining install-namespace key that exists in all 5 locales.

    missing_response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/i18n/ui/../../plugin.toml.json"
    )
    assert missing_response.status_code == 404

    unsupported_response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/i18n/ui/es.json"
    )
    assert unsupported_response.status_code == 404


@pytest.mark.asyncio
async def test_plugin_ui_i18n_rejects_locale_file_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    config_path = plugin_root / "plugin.toml"
    config_path.write_text('[plugin]\nid = "external_plugin"\n', encoding="utf-8")
    i18n_dir = plugin_root / "i18n"
    i18n_dir.mkdir()
    outside_file = tmp_path / "outside.json"
    outside_file.write_text('{"secret": true}', encoding="utf-8")
    locale_file = i18n_dir / "en.json"
    try:
        locale_file.symlink_to(outside_file)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    registration = galgame_install_route_module.InstallPluginRegistration(
        plugin_id="external_plugin",
        install_kinds={},
        ui_i18n_dir=i18n_dir,
        config_path=config_path,
    )

    async def registration_for(_plugin_id: str):
        return registration

    monkeypatch.setattr(
        galgame_install_route_module,
        "_get_plugin_registration",
        registration_for,
    )

    response = await galgame_install_route_module.get_plugin_ui_i18n(
        "external_plugin",
        "en",
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_plugin_ui_i18n_pins_payload_before_locale_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    config_path = plugin_root / "plugin.toml"
    config_path.write_text('[plugin]\nid = "external_plugin"\n', encoding="utf-8")
    i18n_dir = plugin_root / "i18n"
    i18n_dir.mkdir()
    locale_file = i18n_dir / "en.json"
    locale_file.write_text('{"safe": true}', encoding="utf-8")
    outside_file = tmp_path / "outside.json"
    outside_file.write_text('{"secret": true}', encoding="utf-8")

    registration = galgame_install_route_module.InstallPluginRegistration(
        plugin_id="external_plugin",
        install_kinds={},
        ui_i18n_dir=i18n_dir,
        config_path=config_path,
    )

    async def registration_for(_plugin_id: str):
        return registration

    original_run_blocking = galgame_install_route_module._run_blocking

    async def swap_after_blocking_read(func, *args, **kwargs):
        result = await original_run_blocking(func, *args, **kwargs)
        locale_file.unlink()
        outside_file.replace(locale_file)
        return result

    monkeypatch.setattr(
        galgame_install_route_module,
        "_get_plugin_registration",
        registration_for,
    )
    monkeypatch.setattr(
        galgame_install_route_module,
        "_run_blocking",
        swap_after_blocking_read,
    )

    response = await galgame_install_route_module.get_plugin_ui_i18n(
        "external_plugin",
        "en",
    )

    assert response.status_code == 200
    assert response.body == b'{"safe": true}'
    assert b"secret" not in response.body


@pytest.mark.asyncio
async def test_plugin_ui_i18n_rejects_replaced_base_directory_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    config_path = plugin_root / "plugin.toml"
    config_path.write_text('[plugin]\nid = "external_plugin"\n', encoding="utf-8")
    i18n_dir = plugin_root / "i18n"
    i18n_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "en.json").write_text('{"secret": true}', encoding="utf-8")
    i18n_dir.rmdir()
    try:
        i18n_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    registration = galgame_install_route_module.InstallPluginRegistration(
        plugin_id="external_plugin",
        install_kinds={},
        ui_i18n_dir=i18n_dir,
        config_path=config_path,
    )

    async def registration_for(_plugin_id: str):
        return registration

    monkeypatch.setattr(
        galgame_install_route_module,
        "_get_plugin_registration",
        registration_for,
    )

    response = await galgame_install_route_module.get_plugin_ui_i18n(
        "external_plugin",
        "en",
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_plugin_ui_i18n_opens_fifo_nonblocking_before_rejecting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "mkfifo") or not getattr(os, "O_NONBLOCK", 0):
        pytest.skip("FIFO nonblocking behavior requires POSIX")

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    config_path = plugin_root / "plugin.toml"
    config_path.write_text('[plugin]\nid = "external_plugin"\n', encoding="utf-8")
    i18n_dir = plugin_root / "i18n"
    i18n_dir.mkdir()
    fifo_path = i18n_dir / "en.json"
    os.mkfifo(fifo_path)

    registration = galgame_install_route_module.InstallPluginRegistration(
        plugin_id="external_plugin",
        install_kinds={},
        ui_i18n_dir=i18n_dir,
        config_path=config_path,
    )

    async def registration_for(_plugin_id: str):
        return registration

    original_open = os.open
    opened_nonblocking = False

    def guarded_open(path, flags, *args, **kwargs):
        nonlocal opened_nonblocking
        opened_nonblocking = bool(flags & os.O_NONBLOCK)
        if not opened_nonblocking:
            raise AssertionError("locale files must be opened nonblocking")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        galgame_install_route_module,
        "_get_plugin_registration",
        registration_for,
    )
    monkeypatch.setattr(galgame_install_route_module.os, "open", guarded_open)

    response = await galgame_install_route_module.get_plugin_ui_i18n(
        "external_plugin",
        "en",
    )

    assert opened_nonblocking is True
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unregistered_plugin_install_route_returns_404(
    plugin_ui_async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install_registry_module, "_install_plugin_registry", {})

    response = await plugin_ui_async_client.post(
        "/plugin/unknown_plugin/ui-api/rapidocr-models",
        json={"force": False},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Plugin 'unknown_plugin' has no install API"


@pytest.mark.asyncio
async def test_uninstalled_market_galgame_has_no_install_i18n_or_tutorial_api(
    plugin_ui_async_client: AsyncClient,
) -> None:
    with state.acquire_plugins_write_lock():
        state.plugins.pop("galgame_plugin", None)

    responses = [
        await plugin_ui_async_client.post(
            "/plugin/galgame_plugin/ui-api/textractor/install",
            json={"force": False},
        ),
        await plugin_ui_async_client.get(
            "/plugin/galgame_plugin/ui-api/i18n/ui/en.json"
        ),
        await plugin_ui_async_client.get(
            "/plugin/galgame_plugin/ui-api/tutorial/status"
        ),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]


@pytest.mark.asyncio
async def test_invalid_plugin_id_404_does_not_reflect_raw_input() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await galgame_install_route_module._get_plugin_registration("../secret")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Plugin has no install API"
    assert "../secret" not in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_galgame_plugin_ui_info_reports_registered_assets(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
) -> None:
    response = await plugin_ui_async_client.get("/plugin/galgame_plugin/ui-info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["plugin_id"] == "galgame_plugin"
    assert payload["has_ui"] is True
    assert payload["explicitly_registered"] is True
    assert payload["ui_path"] == "/plugin/galgame_plugin/ui/"
    assert payload["static_files_count"] >= 3
    assert "index.html" in payload["static_files"]
    assert "main.js" in payload["static_files"]
    assert "style.css" in payload["static_files"]


@pytest.mark.asyncio
async def test_galgame_plugin_ui_rejects_path_traversal(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
) -> None:
    response = await plugin_ui_async_client.get("/plugin/galgame_plugin/ui/%2e%2e/plugin.toml")

    assert response.status_code == 403
    assert response.json()["detail"] == "Access denied: path traversal detected"


@pytest.mark.asyncio
async def test_galgame_plugin_textractor_install_start_route_creates_run_and_seeds_state(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_create_run(payload, *, client_host):
        del client_host
        assert payload.plugin_id == "galgame_plugin"
        assert payload.entry_id == "galgame_install_textractor"
        assert payload.args == {"force": True, "_ctx": {"entry_timeout": 600.0}}
        return RunCreateResponse(run_id="run-textractor-1", status="queued")

    blocking_calls: list[str] = []

    async def _fake_run_blocking(func, *args, **kwargs):
        blocking_calls.append(getattr(func, "__name__", ""))
        return func(*args, **kwargs)

    monkeypatch.setattr(galgame_install_route_module.run_service, "create_run", _fake_create_run)
    monkeypatch.setattr(galgame_install_route_module, "_run_blocking", _fake_run_blocking)

    response = await plugin_ui_async_client.post(
        "/plugin/galgame_plugin/ui-api/textractor/install",
        json={"force": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "run-textractor-1"
    assert payload["state"]["status"] == "queued"
    assert payload["state"]["phase"] == "queued"
    assert "update_install_task_state" in blocking_calls
    saved = install_task_module.load_install_task_state(
        "run-textractor-1",
        plugin_id="galgame_plugin",
    )
    assert saved is not None
    assert saved["message"] == "Textractor install queued"


@pytest.mark.asyncio
async def test_run_blocking_times_out_blocking_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(galgame_install_route_module, "_BLOCKING_IO_TIMEOUT_SECONDS", 0.01, raising=False)

    def _slow_blocking_call() -> str:
        time.sleep(0.05)
        return "late"

    with pytest.raises(TimeoutError):
        await galgame_install_route_module._run_blocking(_slow_blocking_call)


@pytest.mark.asyncio
async def test_install_start_returns_retryable_state_when_local_persist_raises_value_error(
    plugin_ui_async_client: AsyncClient,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del galgame_install_runtime_root

    async def _fake_create_run(payload, *, client_host):
        del payload, client_host
        return RunCreateResponse(run_id="run-local-state-value-error", status="queued")

    async def _fake_run_blocking(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "update_install_task_state":
            raise ValueError("invalid local state")
        return func(*args, **kwargs)

    monkeypatch.setattr(galgame_install_route_module.run_service, "create_run", _fake_create_run)
    monkeypatch.setattr(galgame_install_route_module, "_run_blocking", _fake_run_blocking)

    response = await plugin_ui_async_client.post(
        "/plugin/galgame_plugin/ui-api/textractor/install",
        json={"force": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "run-local-state-value-error"
    assert payload["local_save_failed"] is True
    assert payload["error"] == "local_state_persist_failed"
    assert payload["retry_hint"]
    assert payload["state"]["status"] == "queued"
    assert payload["state"]["plugin_id"] == "galgame_plugin"
    assert payload["state"]["message"] == "Textractor install queued"


@pytest.mark.asyncio
async def test_study_companion_install_routes_map_to_study_entries(
    plugin_ui_async_client: AsyncClient,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    async def _fake_create_run(payload, *, client_host):
        del client_host
        seen.append((payload.plugin_id, payload.entry_id, dict(payload.args or {})))
        return RunCreateResponse(run_id=f"run-{payload.entry_id}", status="queued")

    monkeypatch.setattr(galgame_install_route_module.run_service, "create_run", _fake_create_run)

    rapidocr_response = await plugin_ui_async_client.post(
        "/plugin/study_companion/ui-api/rapidocr-models",
        json={"force": False},
    )
    tesseract_response = await plugin_ui_async_client.post(
        "/plugin/study_companion/ui-api/tesseract/install",
        json={"force": True},
    )
    textractor_response = await plugin_ui_async_client.post(
        "/plugin/study_companion/ui-api/textractor/install",
        json={"force": True},
    )

    assert rapidocr_response.status_code == 200
    assert tesseract_response.status_code == 404
    assert textractor_response.status_code == 404
    assert seen == [
        ("study_companion", "study_download_rapidocr_models", {"force": False, "_ctx": {"entry_timeout": 600.0}}),
    ]
    assert rapidocr_response.json()["state"]["kind"] == "rapidocr_models"
    assert rapidocr_response.json()["state"]["plugin_id"] == "study_companion"
    assert install_task_module.load_install_task_state(
        "run-study_download_rapidocr_models",
        kind="rapidocr_models",
        plugin_id="study_companion",
    ) is not None


@pytest.mark.asyncio
async def test_legacy_study_tesseract_status_routes_remain_read_only_after_restart(
    plugin_ui_async_client: AsyncClient,
    galgame_install_runtime_root: Path,
) -> None:
    install_task_module.update_install_task_state(
        "run-legacy-tesseract",
        kind="tesseract",
        plugin_id="study_companion",
        run_id="run-legacy-tesseract",
        status="completed",
        phase="completed",
        message="Tesseract installation completed",
        progress=1.0,
    )

    latest_response = await plugin_ui_async_client.get(
        "/plugin/study_companion/ui-api/tesseract/install/latest"
    )
    task_response = await plugin_ui_async_client.get(
        "/plugin/study_companion/ui-api/tesseract/install/run-legacy-tesseract"
    )
    async with plugin_ui_async_client.stream(
        "GET",
        "/plugin/study_companion/ui-api/tesseract/install/run-legacy-tesseract/stream",
    ) as stream_response:
        stream_body = ""
        async for line in stream_response.aiter_lines():
            if line.startswith("data: "):
                stream_body = line[len("data: "):]
                break
    start_response = await plugin_ui_async_client.post(
        "/plugin/study_companion/ui-api/tesseract/install",
        json={"force": True},
    )

    assert latest_response.status_code == 200
    assert latest_response.json()["task_id"] == "run-legacy-tesseract"
    assert task_response.status_code == 200
    assert task_response.json()["status"] == "completed"
    assert stream_response.status_code == 200
    assert json.loads(stream_body)["task_id"] == "run-legacy-tesseract"
    assert start_response.status_code == 404


@pytest.mark.asyncio
async def test_legacy_study_tesseract_route_stays_hidden_after_old_plugin_registration(
    plugin_ui_async_client: AsyncClient,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    galgame_install_route_module.register_install_plugin(
        "study_companion",
        install_kinds={
            "rapidocr_models": galgame_install_route_module.InstallKindRegistration(
                entry_id="study_download_rapidocr_models",
                label="RapidOCR Models",
                queued_message="RapidOCR model download queued",
            ),
            "tesseract": galgame_install_route_module.InstallKindRegistration(
                entry_id="study_install_tesseract",
                label="Tesseract",
                queued_message="Tesseract install queued",
            ),
        },
        ui_i18n_dir=tmp_path,
        tutorial_enabled=True,
    )

    async def _fake_create_run(payload, *, client_host):
        del client_host
        seen.append((payload.plugin_id, payload.entry_id, dict(payload.args or {})))
        return RunCreateResponse(run_id="run-study_install_tesseract", status="queued")

    monkeypatch.setattr(galgame_install_route_module.run_service, "create_run", _fake_create_run)

    response = await plugin_ui_async_client.post(
        "/plugin/study_companion/ui-api/tesseract/install",
        json={"force": True},
    )

    assert response.status_code == 404
    assert seen == []
    assert install_task_module.load_install_task_state(
        "run-study_install_tesseract",
        kind="tesseract",
        plugin_id="study_companion",
    ) is None


@pytest.mark.asyncio
async def test_study_companion_install_i18n_compat_route_uses_study_bundle(
    plugin_ui_async_client: AsyncClient,
) -> None:
    response = await plugin_ui_async_client.get(
        "/plugin/study_companion/ui-api/i18n/ui/en.json"
    )

    assert response.status_code == 200
    assert response.json()["entries.open_ui.name"] == "Open Study Companion UI"


@pytest.mark.asyncio
async def test_galgame_plugin_textractor_install_status_route_reads_persisted_state(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_task_module.update_install_task_state(
        "run-textractor-2",
        plugin_id="galgame_plugin",
        run_id="run-textractor-2",
        status="running",
        phase="downloading",
        message="Downloading Textractor-x64.zip",
        progress=0.42,
        downloaded_bytes=42,
        total_bytes=100,
        asset_name="Textractor-x64.zip",
    )

    def _fake_get_run(run_id: str) -> RunRecord:
        assert run_id == "run-textractor-2"
        return _running_install_run(
            run_id,
            entry_id="galgame_install_textractor",
            stage="downloading",
            message="Downloading Textractor-x64.zip",
        )

    monkeypatch.setattr(galgame_install_route_module.run_service, "get_run", _fake_get_run)

    response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/textractor/install/run-textractor-2"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "running"
    assert payload["phase"] == "downloading"
    assert payload["downloaded_bytes"] == 42
    assert payload["total_bytes"] == 100


@pytest.mark.asyncio
async def test_galgame_plugin_install_status_route_rejects_invalid_task_id_before_run_lookup(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_get_run(run_id: str) -> RunRecord:
        raise AssertionError(f"run lookup should not happen for invalid task_id: {run_id}")

    monkeypatch.setattr(galgame_install_route_module.run_service, "get_run", _unexpected_get_run)

    response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/textractor/install/..."
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Textractor install task_id"


@pytest.mark.asyncio
async def test_galgame_plugin_textractor_install_latest_route_returns_latest_state(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_task_module.update_install_task_state(
        "run-textractor-latest",
        plugin_id="galgame_plugin",
        run_id="run-textractor-latest",
        status="completed",
        phase="completed",
        message="Textractor installation completed",
        progress=1.0,
    )

    blocking_calls: list[str] = []

    async def _fake_run_blocking(func, *args, **kwargs):
        blocking_calls.append(getattr(func, "__name__", ""))
        return func(*args, **kwargs)

    monkeypatch.setattr(galgame_install_route_module, "_run_blocking", _fake_run_blocking)

    response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/textractor/install/latest"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "run-textractor-latest"
    assert payload["status"] == "completed"
    assert "load_latest_install_task_state" in blocking_calls
    assert "_resolve_install_task_payload" in blocking_calls


@pytest.mark.asyncio
async def test_install_latest_routes_are_namespaced_by_plugin_id(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
    galgame_install_runtime_root: Path,
) -> None:
    install_task_module.update_install_task_state(
        "run-galgame-models-latest",
        kind="rapidocr_models",
        plugin_id="galgame_plugin",
        run_id="run-galgame-models-latest",
        status="completed",
        phase="completed",
        message="Galgame RapidOCR model download completed",
        progress=1.0,
    )
    install_task_module.update_install_task_state(
        "run-study-models-latest",
        kind="rapidocr_models",
        plugin_id="study_companion",
        run_id="run-study-models-latest",
        status="completed",
        phase="completed",
        message="Study RapidOCR model download completed",
        progress=1.0,
    )

    galgame_response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/rapidocr-models/latest"
    )
    study_response = await plugin_ui_async_client.get(
        "/plugin/study_companion/ui-api/rapidocr-models/latest"
    )

    assert galgame_response.status_code == 200
    assert study_response.status_code == 200
    assert galgame_response.json()["task_id"] == "run-galgame-models-latest"
    assert galgame_response.json()["plugin_id"] == "galgame_plugin"
    assert study_response.json()["task_id"] == "run-study-models-latest"
    assert study_response.json()["plugin_id"] == "study_companion"


@pytest.mark.asyncio
async def test_galgame_plugin_textractor_install_stream_route_emits_sse_payload(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_get_registration = install_registry_module.get_install_plugin_registration
    registration_reads = 0

    def counted_get_registration(plugin_id: str):
        nonlocal registration_reads
        registration_reads += 1
        return original_get_registration(plugin_id)

    monkeypatch.setattr(
        install_registry_module,
        "get_install_plugin_registration",
        counted_get_registration,
    )
    install_task_module.update_install_task_state(
        "run-textractor-stream",
        plugin_id="galgame_plugin",
        run_id="run-textractor-stream",
        status="completed",
        phase="completed",
        message="Textractor installation completed",
        progress=1.0,
    )

    async with plugin_ui_async_client.stream(
        "GET",
        "/plugin/galgame_plugin/ui-api/textractor/install/run-textractor-stream/stream",
    ) as response:
        assert response.status_code == 200
        body = ""
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                body = line[len("data: "):]
                break

    payload = json.loads(body)
    assert payload["task_id"] == "run-textractor-stream"
    assert payload["status"] == "completed"
    assert registration_reads == 1


@pytest.mark.asyncio
async def test_galgame_plugin_install_stream_emits_failed_event_when_state_read_crashes(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del registered_galgame_plugin_meta, galgame_install_runtime_root
    running_payload = install_task_module.build_install_task_state(
        task_id="run-stream-crash",
        plugin_id="galgame_plugin",
        status="running",
        phase="downloading",
        message="Downloading",
    )
    resolve_calls = 0

    async def _fake_run_blocking(func, *args, **kwargs):
        nonlocal resolve_calls
        if getattr(func, "__name__", "") == "_resolve_install_task_payload":
            resolve_calls += 1
            if resolve_calls == 1:
                return dict(running_payload)
            raise OSError("state read failed")
        return func(*args, **kwargs)

    monkeypatch.setattr(galgame_install_route_module, "_run_blocking", _fake_run_blocking)

    async with plugin_ui_async_client.stream(
        "GET",
        "/plugin/galgame_plugin/ui-api/textractor/install/run-stream-crash/stream",
    ) as response:
        assert response.status_code == 200
        payload = None
        async with asyncio.timeout(3.0):
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    candidate = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                if candidate.get("status") == "failed":
                    payload = candidate
                    break

    assert payload is not None
    assert payload["task_id"] == "run-stream-crash"
    assert payload["status"] == "failed"
    assert payload["stream_error"] is True
    assert "could not be read" in payload["message"]


def test_mark_stale_install_task_returns_failed_payload_when_persist_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = install_task_module.build_install_task_state(
        task_id="run-stale-write-fails",
        plugin_id="galgame_plugin",
        status="running",
        phase="downloading",
        message="Downloading",
    )

    def _raise_persist_failure(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(galgame_install_route_module, "_persist_install_payload", _raise_persist_failure)

    result = galgame_install_route_module._mark_stale_install_task(
        "run-stale-write-fails",
        plugin_id="galgame_plugin",
        kind="textractor",
        label="Textractor",
        payload=payload,
    )

    assert result["status"] == "failed"
    assert result["phase"] == "failed"
    assert result["local_save_failed"] is True
    assert "install task was interrupted" in result["message"]


def test_terminal_run_payload_returns_memory_state_when_first_persist_fails(
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del galgame_install_runtime_root

    def _fake_get_run(run_id: str) -> RunRecord:
        assert run_id == "run-terminal-write-fails"
        return _terminal_install_run(
            run_id,
            entry_id="galgame_install_textractor",
            message="Textractor installation completed",
        )

    def _raise_persist_failure(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(galgame_install_route_module.run_service, "get_run", _fake_get_run)
    monkeypatch.setattr(galgame_install_route_module, "_persist_install_payload", _raise_persist_failure)

    result = galgame_install_route_module._resolve_install_task_payload(
        "run-terminal-write-fails",
        plugin_id="galgame_plugin",
        kind="textractor",
        label="Textractor",
    )

    assert result["status"] == "completed"
    assert result["phase"] == "completed"
    assert result["message"] == "Textractor installation completed"
    assert result["local_save_failed"] is True


def test_terminal_run_payload_with_existing_state_returns_memory_state_when_persist_fails(
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del galgame_install_runtime_root
    install_task_module.update_install_task_state(
        "run-terminal-existing-state-write-fails",
        plugin_id="galgame_plugin",
        status="running",
        phase="downloading",
        message="Downloading",
        progress=0.42,
    )

    def _fake_get_run(run_id: str) -> RunRecord:
        assert run_id == "run-terminal-existing-state-write-fails"
        return _terminal_install_run(
            run_id,
            entry_id="galgame_install_textractor",
            message="Textractor installation completed",
        )

    def _raise_persist_failure(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(galgame_install_route_module.run_service, "get_run", _fake_get_run)
    monkeypatch.setattr(galgame_install_route_module, "_persist_install_payload", _raise_persist_failure)

    result = galgame_install_route_module._resolve_install_task_payload(
        "run-terminal-existing-state-write-fails",
        plugin_id="galgame_plugin",
        kind="textractor",
        label="Textractor",
    )

    assert result["status"] == "completed"
    assert result["phase"] == "completed"
    assert result["message"] == "Textractor installation completed"
    assert result["local_save_failed"] is True


def test_install_task_store_logs_corrupt_state_json(
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del galgame_install_runtime_root
    warnings: list[str] = []
    monkeypatch.setattr(
        install_task_module,
        "logger",
        SimpleNamespace(warning=lambda message, *args, **kwargs: warnings.append(str(message))),
        raising=False,
    )
    path = install_task_module.install_task_state_path(
        "corrupt-state",
        plugin_id="galgame_plugin",
    )
    path.write_text("{bad json", encoding="utf-8")

    assert install_task_module.load_install_task_state(
        "corrupt-state",
        plugin_id="galgame_plugin",
    ) is None
    assert any("failed to load install task state" in message for message in warnings)


def test_install_task_store_reads_legacy_galgame_state_path(
    galgame_install_runtime_root: Path,
) -> None:
    task_payload = {
        "task_id": "legacy-textractor-task",
        "kind": "textractor",
        "run_id": "legacy-textractor-task",
        "plugin_id": "galgame_plugin",
        "status": "running",
        "phase": "downloading",
        "message": "Legacy download still running",
        "progress": 0.5,
    }
    legacy_tasks_dir = (
        galgame_install_runtime_root
        / "plugin-runtime"
        / "galgame_plugin"
        / "textractor-installs"
    )
    legacy_tasks_dir.mkdir(parents=True, exist_ok=True)
    (legacy_tasks_dir / "legacy-textractor-task.json").write_text(
        json.dumps(task_payload),
        encoding="utf-8",
    )
    (legacy_tasks_dir / "latest.json").write_text(
        json.dumps(
            {
                "task_id": "legacy-textractor-task",
                "kind": "textractor",
                "run_id": "legacy-textractor-task",
                "plugin_id": "galgame_plugin",
            }
        ),
        encoding="utf-8",
    )

    loaded = install_task_module.load_install_task_state(
        "legacy-textractor-task",
        plugin_id="galgame_plugin",
    )
    latest = install_task_module.load_latest_install_task_state(
        plugin_id="galgame_plugin",
    )

    assert loaded == task_payload
    assert latest == task_payload


def test_install_task_store_rejects_unregistered_dxcam_kind() -> None:
    with pytest.raises(ValueError):
        install_task_module.build_install_task_state(task_id="dxcam-task", kind="dxcam")


@pytest.mark.asyncio
async def test_tutorial_progress_is_namespaced_by_plugin_id(
    plugin_ui_async_client: AsyncClient,
    tutorial_runtime_root: Path,
) -> None:
    del tutorial_runtime_root

    save_response = await plugin_ui_async_client.post(
        "/plugin/galgame_plugin/ui-api/tutorial/progress",
        json={"completed": True, "last_step_index": 4, "completed_at": 123.0},
    )
    study_response = await plugin_ui_async_client.get(
        "/plugin/study_companion/ui-api/tutorial/status"
    )

    assert save_response.status_code == 200
    assert save_response.json()["progress"]["completed"] is True
    assert study_response.status_code == 200
    assert study_response.json()["progress"]["completed"] is False


def _write_legacy_galgame_store(path: Path, progress: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"tutorial_progress": progress}),
        encoding="utf-8",
    )


def _galgame_tutorial_target(runtime_root: Path) -> Path:
    return (
        runtime_root
        / "server"
        / "plugin_install"
        / "galgame_plugin"
        / "tutorial_progress.json"
    )


@pytest.mark.asyncio
async def test_galgame_tutorial_migrates_runtime_primary_store(
    plugin_ui_async_client: AsyncClient,
    tutorial_runtime_root: Path,
) -> None:
    legacy_path = (
        tutorial_runtime_root
        / "plugins"
        / "galgame_plugin"
        / "data"
        / "galgame_store.json"
    )
    _write_legacy_galgame_store(
        legacy_path,
        {"completed": True, "last_step_index": 4},
    )

    response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/tutorial/status"
    )

    assert response.status_code == 200
    assert response.json()["progress"]["completed"] is True
    assert response.json()["progress"]["last_step_index"] == 4
    assert json.loads(
        _galgame_tutorial_target(tutorial_runtime_root).read_text(encoding="utf-8")
    )["completed"] is True


@pytest.mark.asyncio
async def test_galgame_tutorial_recovers_valid_backup_after_corrupt_primary(
    plugin_ui_async_client: AsyncClient,
    tutorial_runtime_root: Path,
) -> None:
    legacy_path = (
        tutorial_runtime_root
        / "plugins"
        / "galgame_plugin"
        / "data"
        / "galgame_store.json"
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("{broken", encoding="utf-8")
    _write_legacy_galgame_store(
        legacy_path.with_name("galgame_store.json.bak"),
        {"skipped": True, "last_step_index": 2},
    )

    response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/tutorial/status"
    )

    assert response.status_code == 200
    assert response.json()["progress"]["skipped"] is True
    assert response.json()["progress"]["last_step_index"] == 2


@pytest.mark.asyncio
async def test_galgame_tutorial_falls_back_to_selected_market_source(
    plugin_ui_async_client: AsyncClient,
    tutorial_runtime_root: Path,
    galgame_plugin_dir: Path,
) -> None:
    _write_legacy_galgame_store(
        galgame_plugin_dir / "data" / "galgame_store.json",
        {"completed": True, "completed_at": 321.0},
    )

    response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/tutorial/status"
    )

    assert response.status_code == 200
    assert response.json()["progress"]["completed"] is True
    assert response.json()["progress"]["completed_at"] == 321.0
    assert _galgame_tutorial_target(tutorial_runtime_root).is_file()


@pytest.mark.asyncio
async def test_galgame_tutorial_migration_does_not_overwrite_existing_target(
    plugin_ui_async_client: AsyncClient,
    tutorial_runtime_root: Path,
) -> None:
    target = _galgame_tutorial_target(tutorial_runtime_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"completed": False, "last_step_index": 1}),
        encoding="utf-8",
    )
    _write_legacy_galgame_store(
        tutorial_runtime_root
        / "plugins"
        / "galgame_plugin"
        / "data"
        / "galgame_store.json",
        {"completed": True, "last_step_index": 9},
    )

    response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/tutorial/status"
    )

    assert response.status_code == 200
    assert response.json()["progress"]["completed"] is False
    assert response.json()["progress"]["last_step_index"] == 1


@pytest.mark.asyncio
async def test_galgame_tutorial_migration_failure_cools_down_then_recovers(
    plugin_ui_async_client: AsyncClient,
    tutorial_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_legacy_galgame_store(
        tutorial_runtime_root
        / "plugins"
        / "galgame_plugin"
        / "data"
        / "galgame_store.json",
        {"completed": True},
    )

    now = [100.0]
    replace_attempts = 0
    real_replace = galgame_install_route_module.os.replace

    def _replace(source: Path, target: Path) -> None:
        nonlocal replace_attempts
        replace_attempts += 1
        if replace_attempts == 1:
            raise OSError("replace failed")
        real_replace(source, target)

    patch_module_clock(
        monkeypatch,
        galgame_install_route_module,
        monotonic=lambda: now[0],
    )
    monkeypatch.setattr(galgame_install_route_module.os, "replace", _replace)

    first_response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/tutorial/status"
    )
    cooldown_response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/tutorial/status"
    )

    assert first_response.status_code == 200
    assert first_response.json()["progress"]["completed"] is False
    assert cooldown_response.status_code == 200
    assert cooldown_response.json()["progress"]["completed"] is False
    assert replace_attempts == 1
    assert not _galgame_tutorial_target(tutorial_runtime_root).exists()

    now[0] += galgame_install_route_module._GALGAME_TUTORIAL_MIGRATION_RETRY_COOLDOWN_SECONDS
    recovered_response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/tutorial/status"
    )

    assert recovered_response.status_code == 200
    assert recovered_response.json()["progress"]["completed"] is True
    assert replace_attempts == 2
    target = _galgame_tutorial_target(tutorial_runtime_root)
    assert target.is_file()
    assert target not in galgame_install_route_module._galgame_tutorial_migration_retry_after


@pytest.mark.asyncio
async def test_tutorial_migration_failure_returns_500(
    plugin_ui_async_client: AsyncClient,
    tutorial_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tutorial_runtime_root

    def _fail_migration(_store_path: Path) -> None:
        raise ValueError("bad migration")

    if isinstance(getattr(install_registry_module, "_tutorial_migration_hooks", None), dict):
        monkeypatch.setattr(
            install_registry_module,
            "_tutorial_migration_hooks",
            {"galgame_plugin": [_fail_migration]},
        )
    else:
        monkeypatch.setattr(
            install_registry_module,
            "_tutorial_migration_hooks",
            [_fail_migration],
        )
    monkeypatch.setattr(galgame_install_route_module, "_tutorial_migrated_paths", set(), raising=False)
    monkeypatch.setattr(galgame_install_route_module, "_tutorial_store_instance", None, raising=False)
    monkeypatch.setattr(galgame_install_route_module, "_tutorial_store_instances", {}, raising=False)

    response = await plugin_ui_async_client.get("/plugin/galgame_plugin/ui-api/tutorial/status")

    assert response.status_code == 500
    assert response.json()["ok"] is False


@pytest.mark.asyncio
async def test_galgame_plugin_install_stream_route_returns_404_before_stream_for_missing_task(
    plugin_ui_async_client: AsyncClient,
    registered_galgame_plugin_meta,
    galgame_install_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing_get_run(run_id: str) -> RunRecord:
        raise ServerDomainError(
            code="RUN_NOT_FOUND",
            message="run not found",
            status_code=404,
            details={"run_id": run_id},
        )

    monkeypatch.setattr(galgame_install_route_module.run_service, "get_run", _missing_get_run)

    response = await plugin_ui_async_client.get(
        "/plugin/galgame_plugin/ui-api/textractor/install/missing-stream-task/stream"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Textractor install task 'missing-stream-task' not found"
