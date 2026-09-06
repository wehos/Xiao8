from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from plugin.config.schema import PluginModelRequirementSchema
from plugin.server.application.model_config_service import ModelConfigService
from plugin.server.domain.errors import ServerDomainError
from plugin.server.domain.model_config import SECRET_MASK
from plugin.server.infrastructure.model_config_store import CONFIG_FILENAME, ModelConfigStore
from plugin.server.routes import model_config
from utils.file_utils import atomic_write_json


pytestmark = pytest.mark.plugin_unit
PREFIX = "/api/model-config"
SECRET = "sk-model-route-private-credential"
SLOT = {
    "name": "Plugin analysis",
    "protocol": "openai_chat",
    "base_url": "https://example.test/v1",
    "model": "vision-model",
    "api_key": SECRET,
    "capabilities": ["text", "image_input"],
}


class TemporaryConfigManager:
    """Only the plugin model file is accessible; never consult live config."""

    def __init__(self, root: Path):
        self.root = root
        self.io_threads: list[int] = []
        self.saved_files: list[str] = []

    def get_runtime_config_path(self, filename: str) -> Path:
        assert filename == CONFIG_FILENAME
        self.io_threads.append(threading.get_ident())
        return self.root / filename

    def save_json_config(self, filename: str, data: dict) -> None:
        self.saved_files.append(filename)
        atomic_write_json(self.get_runtime_config_path(filename), data)


@pytest.fixture
def model_setup(tmp_path, monkeypatch):
    from utils import cloudsave_runtime

    monkeypatch.setattr(
        cloudsave_runtime, "cloudsave_writable_transaction", lambda *_args, **_kwargs: nullcontext()
    )
    cm = TemporaryConfigManager(tmp_path)
    requirements_threads = []

    def requirements(plugin_id):
        requirements_threads.append(threading.get_ident())
        if plugin_id == "legacy":
            return {}
        if plugin_id not in {"first", "second"}:
            raise ServerDomainError("PLUGIN_NOT_FOUND", "Plugin manifest not found", 404)
        return {"analysis": PluginModelRequirementSchema(label="Analysis", capabilities=["image_input"])}

    service = ModelConfigService(ModelConfigStore(cm), requirements_loader=requirements)
    monkeypatch.setattr(model_config, "service", service)
    app = FastAPI()
    app.include_router(model_config.router)
    return app, cm, requirements_threads


@pytest.fixture
async def model_client(model_setup):
    app, _, _ = model_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def create_slot(client, **overrides):
    response = await client.post(f"{PREFIX}/slots", json={**SLOT, **overrides})
    assert response.status_code == 201, response.text
    assert SECRET not in response.text
    return response.json()


async def test_crud_preserves_credentials_and_main_configuration(model_client, model_setup):
    _, cm, _ = model_setup
    core_path = cm.root / "core_config.json"
    core_data = b'{"CORE_API_TYPE":"existing-main","AGENT_MODEL":"existing-agent"}\n'
    core_path.write_bytes(core_data)

    response = await model_client.get(f"{PREFIX}/slots")
    assert response.status_code == 200
    assert response.json() == {"schema_version": 1, "slots": []}

    created = await create_slot(model_client)
    assert created["api_key"] == SECRET_MASK
    assert created["bound_by"] == []
    slot_id = created["id"]
    updated = await model_client.patch(
        f"{PREFIX}/slots/{slot_id}", json={"name": "Renamed", "api_key": SECRET_MASK}
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == slot_id
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["api_key"] == SECRET_MASK
    assert SECRET not in updated.text

    fetched = await model_client.get(f"{PREFIX}/slots/{slot_id}")
    listed = await model_client.get(f"{PREFIX}/slots")
    assert fetched.json() == updated.json()
    assert listed.json()["slots"] == [updated.json()]
    stored = json.loads((cm.root / CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert stored["slots"][slot_id]["api_key"] == SECRET
    assert cm.saved_files == [CONFIG_FILENAME, CONFIG_FILENAME]
    assert core_path.read_bytes() == core_data


async def test_shared_slot_binding_blocks_delete_until_every_plugin_unbinds(model_client):
    slot_id = (await create_slot(model_client))["id"]
    for plugin_id in ("first", "second"):
        initial = await model_client.get(f"{PREFIX}/plugins/{plugin_id}/bindings")
        assert initial.json()["ready"] is False
        assert initial.json()["requirements"]["analysis"]["status"] == "unbound"
        response = await model_client.put(
            f"{PREFIX}/plugins/{plugin_id}/bindings/analysis", json={"slot_id": slot_id}
        )
        assert response.status_code == 200
        bound = await model_client.get(f"{PREFIX}/plugins/{plugin_id}/bindings")
        assert bound.json()["ready"] is True
        assert bound.json()["bindings"] == {"analysis": slot_id}

    slot = (await model_client.get(f"{PREFIX}/slots/{slot_id}")).json()
    assert slot["bound_by"] == [
        {"plugin_id": "first", "usage_id": "analysis"},
        {"plugin_id": "second", "usage_id": "analysis"},
    ]
    for plugin_id in ("first", "second"):
        blocked = await model_client.delete(f"{PREFIX}/slots/{slot_id}")
        assert blocked.status_code == 409
        assert blocked.headers["x-error-code"] == "MODEL_SLOT_IN_USE"
        unbound = await model_client.delete(f"{PREFIX}/plugins/{plugin_id}/bindings/analysis")
        assert unbound.status_code == 200

    deleted = await model_client.delete(f"{PREFIX}/slots/{slot_id}")
    assert deleted.json() == {"success": True}
    missing = await model_client.get(f"{PREFIX}/slots/{slot_id}")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "MODEL_SLOT_NOT_FOUND"


async def test_legacy_plugin_and_undeclared_usage(model_client):
    slot_id = (await create_slot(model_client))["id"]
    legacy = await model_client.get(f"{PREFIX}/plugins/legacy/bindings")
    assert legacy.json() == {"plugin_id": "legacy", "requirements": {}, "bindings": {}, "ready": True}
    response = await model_client.put(
        f"{PREFIX}/plugins/first/bindings/undeclared", json={"slot_id": slot_id}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MODEL_USAGE_NOT_DECLARED"
    missing = await model_client.get(f"{PREFIX}/plugins/missing/bindings")
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "invalid",
    [
        {"protocol": "unrecognized"},
        {"base_url": "https://example.test/v1?key=" + SECRET},
        {"api_key": [SECRET]},
        {"capabilities": ["native_video"]},
        {"defaults": {"max_output_tokens": 0}},
        {"name": "  "},
        {"timeout_seconds": 0},
        {"unknown": SECRET},
    ],
)
async def test_invalid_slot_fields_are_rejected_without_echoing_secrets(model_client, invalid):
    response = await model_client.post(f"{PREFIX}/slots", json={**SLOT, **invalid})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MODEL_SLOT_INVALID"
    assert SECRET not in response.text
    listed = await model_client.get(f"{PREFIX}/slots")
    assert listed.json()["slots"] == []


@pytest.mark.parametrize("payload", [{}, {"slot_id": 12}, {"slot_id": "slot_x", "api_key": SECRET}])
async def test_binding_payload_is_strict_and_does_not_echo_secrets(model_client, payload):
    response = await model_client.put(f"{PREFIX}/plugins/first/bindings/analysis", json=payload)
    assert response.status_code == 422
    assert SECRET not in response.text


@pytest.mark.parametrize("payload", [[{"api_key": SECRET}], SECRET])
async def test_non_object_bodies_do_not_echo_credentials(model_client, payload):
    slot_id = (await create_slot(model_client))["id"]
    for method, path in [
        ("POST", f"{PREFIX}/slots"),
        ("PATCH", f"{PREFIX}/slots/{slot_id}"),
        ("PUT", f"{PREFIX}/plugins/first/bindings/analysis"),
    ]:
        response = await model_client.request(method, path, json=payload)
        assert response.status_code == 422
        assert SECRET not in response.text


async def test_invalid_json_does_not_echo_credentials(model_client):
    response = await model_client.post(
        f"{PREFIX}/slots",
        content='{"api_key":"' + SECRET + '",',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert SECRET not in response.text


async def test_model_config_routes_are_included_in_plugin_app():
    from plugin.server.http_app import build_plugin_server_app

    app = build_plugin_server_app()
    paths = {route.path for route in app.routes}
    assert f"{PREFIX}/slots" in paths
    assert f"{PREFIX}/plugins/{{plugin_id}}/bindings/{{usage_id}}" in paths


async def test_incompatible_binding_is_rejected(model_client):
    slot_id = (await create_slot(model_client, capabilities=["text"]))["id"]
    response = await model_client.put(
        f"{PREFIX}/plugins/first/bindings/analysis", json={"slot_id": slot_id}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MODEL_CAPABILITY_MISMATCH"


async def test_storage_error_does_not_leak_credentials(model_client, model_setup, monkeypatch):
    _, cm, _ = model_setup

    def fail_save(*_args):
        raise OSError("sensitive storage error " + SECRET)

    monkeypatch.setattr(cm, "save_json_config", fail_save)
    response = await model_client.post(f"{PREFIX}/slots", json=SLOT)
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "MODEL_CONFIG_WRITE_FAILED"
    assert SECRET not in response.text


async def test_corrupt_stored_configuration_is_not_exposed(model_client, model_setup):
    _, cm, _ = model_setup
    path = cm.root / CONFIG_FILENAME
    path.write_text(json.dumps({"api_key": SECRET}), encoding="utf-8")
    before = path.read_bytes()
    response = await model_client.get(f"{PREFIX}/slots")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "MODEL_CONFIG_INVALID"
    assert SECRET not in response.text
    assert path.read_bytes() == before


async def test_config_and_manifest_io_run_outside_http_event_loop(model_client, model_setup):
    _, cm, requirements_threads = model_setup
    event_loop_thread = threading.get_ident()
    slot_id = (await create_slot(model_client))["id"]
    for path in ("slots", f"slots/{slot_id}", "plugins/first/bindings"):
        response = await model_client.get(f"{PREFIX}/{path}")
        assert response.status_code == 200
    updated = await model_client.patch(f"{PREFIX}/slots/{slot_id}", json={"name": "Updated"})
    assert updated.status_code == 200
    bound = await model_client.put(
        f"{PREFIX}/plugins/first/bindings/analysis", json={"slot_id": slot_id}
    )
    assert bound.status_code == 200
    unbound = await model_client.delete(f"{PREFIX}/plugins/first/bindings/analysis")
    assert unbound.status_code == 200
    deleted = await model_client.delete(f"{PREFIX}/slots/{slot_id}")
    assert deleted.status_code == 200
    assert cm.io_threads and requirements_threads
    assert event_loop_thread not in cm.io_threads
    assert event_loop_thread not in requirements_threads
