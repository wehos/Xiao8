from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from plugin.config.schema import PluginModelRequirementSchema
from plugin.server.application.model_config_service import ModelConfigService, load_model_requirements
from plugin.server.domain.errors import ServerDomainError
from plugin.server.domain.model_config import SECRET_MASK, ModelSlot
from plugin.server.infrastructure.model_config_store import CONFIG_FILENAME, ModelConfigStore
from utils.file_utils import atomic_write_json


class TempConfigManager:
    def __init__(self, root):
        self.config_dir = root / "config"
        self.transactions = []

    def get_runtime_config_path(self, filename):
        return self.config_dir / filename

    def save_json_config(self, filename, data):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.get_runtime_config_path(filename), data)


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    import utils.cloudsave_runtime as cloudsave

    cm = TempConfigManager(tmp_path)

    @contextmanager
    def writable_transaction(manager, **kwargs):
        assert manager is cm
        cm.transactions.append(kwargs)
        yield

    monkeypatch.setattr(cloudsave, "cloudsave_writable_transaction", writable_transaction)
    declarations = {
        "alpha": {"analysis": PluginModelRequirementSchema(label="Analysis", capabilities=["image_input"])},
        "beta": {"analysis": PluginModelRequirementSchema(label="Analysis")},
        "legacy": {},
    }

    def requirements(plugin_id):
        if plugin_id not in declarations:
            raise ServerDomainError("PLUGIN_NOT_FOUND", "Plugin not found", 404)
        return declarations[plugin_id]

    service = ModelConfigService(ModelConfigStore(cm), requirements)
    return cm, service, declarations


def slot_payload(**updates):
    return {
        "name": "Shared model",
        "protocol": "openai_chat",
        "base_url": "https://models.example/v1/",
        "model": "example-model",
        "api_key": "test-secret-key",
        "capabilities": ["text", "image_input", "streaming"],
        **updates,
    }


def test_missing_config_is_empty_and_read_does_not_write(config_env):
    cm, service, _ = config_env
    assert service.list_slots() == {"schema_version": 1, "slots": []}
    assert not cm.get_runtime_config_path(CONFIG_FILENAME).exists()
    assert not cm.transactions
    assert service.get_bindings("legacy")["ready"] is True


def test_shared_slot_rename_and_secret_roundtrip_leave_core_config_untouched(config_env):
    cm, service, _ = config_env
    cm.config_dir.mkdir(parents=True)
    core_path = cm.config_dir / "core_config.json"
    original_core = b'{"coreApi":"free","agentModelId":"existing"}'
    core_path.write_bytes(original_core)

    created = service.create_slot(slot_payload())
    slot_id = created["id"]
    assert created["base_url"] == "https://models.example/v1"
    assert created["api_key"] == SECRET_MASK
    for plugin_id in ("alpha", "beta"):
        service.set_binding(plugin_id, "analysis", slot_id)
    renamed = service.update_slot(slot_id, {"name": "Renamed", "api_key": SECRET_MASK})
    assert renamed["id"] == slot_id
    assert len(renamed["bound_by"]) == 2
    assert service.get_bindings("alpha")["requirements"]["analysis"]["status"] == "bound"
    assert service.get_bindings("beta")["bindings"] == {"analysis": slot_id}
    assert "test-secret-key" not in json.dumps(service.list_slots())
    persisted = json.loads(cm.get_runtime_config_path(CONFIG_FILENAME).read_text())
    assert persisted["slots"][slot_id]["api_key"] == "test-secret-key"
    assert core_path.read_bytes() == original_core
    assert all(tx == {"operation": "save", "target": CONFIG_FILENAME} for tx in cm.transactions)


@pytest.mark.parametrize("mask", [SECRET_MASK, "********", "••••••"])
def test_display_masks_preserve_secret_and_empty_key_clears_it(config_env, mask):
    _, service, _ = config_env
    slot_id = service.create_slot(slot_payload())["id"]
    service.update_slot(slot_id, {"api_key": mask})
    assert service.store.read().slots[slot_id].api_key == "test-secret-key"
    service.update_slot(slot_id, {"api_key": ""})
    assert service.get_slot(slot_id)["api_key"] == ""
    assert service.store.read().slots[slot_id].api_key == ""


def test_new_slot_cannot_store_mask(config_env):
    cm, service, _ = config_env
    with pytest.raises(ServerDomainError, match="API key"):
        service.create_slot(slot_payload(api_key=SECRET_MASK))
    assert not cm.get_runtime_config_path(CONFIG_FILENAME).exists()


@pytest.mark.parametrize("change", [
    {"base_url": "https://another.example/v1"},
    {"protocol": "anthropic_messages"},
])
def test_endpoint_change_does_not_reuse_previous_credential(config_env, change):
    _, service, _ = config_env
    slot_id = service.create_slot(slot_payload())["id"]
    with pytest.raises(ServerDomainError) as error:
        service.update_slot(slot_id, {**change, "api_key": SECRET_MASK})
    assert error.value.code == "MODEL_CREDENTIAL_UPDATE_REQUIRED"
    assert service.store.read().slots[slot_id].api_key == "test-secret-key"
    service.update_slot(slot_id, {**change, "api_key": "new-test-key"})
    assert service.store.read().slots[slot_id].api_key == "new-test-key"


def test_binding_must_be_declared_and_meet_capabilities(config_env):
    cm, service, _ = config_env
    slot_id = service.create_slot(slot_payload(capabilities=["text"]))["id"]
    for plugin_id, usage_id, code in [
        ("alpha", "analysis", "MODEL_CAPABILITY_MISMATCH"),
        ("beta", "arbitrary", "MODEL_USAGE_NOT_DECLARED"),
        ("legacy", "analysis", "MODEL_USAGE_NOT_DECLARED"),
        ("missing", "analysis", "PLUGIN_NOT_FOUND"),
    ]:
        with pytest.raises(ServerDomainError) as error:
            service.set_binding(plugin_id, usage_id, slot_id)
        assert error.value.code == code
    assert service.store.read().bindings == {}
    assert service.get_bindings("alpha")["ready"] is False


def test_capability_edit_cannot_break_existing_binding(config_env):
    _, service, declarations = config_env
    slot_id = service.create_slot(slot_payload())["id"]
    service.set_binding("alpha", "analysis", slot_id)
    with pytest.raises(ServerDomainError) as error:
        service.update_slot(slot_id, {"capabilities": ["text"]})
    assert error.value.code == "MODEL_CAPABILITY_MISMATCH"
    assert "image_input" in service.store.read().slots[slot_id].capabilities
    declarations["alpha"]["analysis"] = PluginModelRequirementSchema(label="New requirement", capabilities=["tool_calling"])
    assert service.get_bindings("alpha")["requirements"]["analysis"]["status"] == "incompatible"


def test_delete_requires_explicit_unbind_and_stale_binding_can_be_cleaned(config_env):
    _, service, declarations = config_env
    slot_id = service.create_slot(slot_payload())["id"]
    service.set_binding("alpha", "analysis", slot_id)
    del declarations["alpha"]
    assert service.get_slot(slot_id)["bound_by"] == [{"plugin_id": "alpha", "usage_id": "analysis"}]
    with pytest.raises(ServerDomainError) as error:
        service.delete_slot(slot_id)
    assert error.value.code == "MODEL_SLOT_IN_USE"
    service.delete_binding("alpha", "analysis")
    service.delete_slot(slot_id)
    assert service.store.read().bindings == {}
    assert service.list_slots()["slots"] == []


def test_fallback_references_cannot_dangle_cycle_or_lose_capabilities(config_env):
    _, service, _ = config_env
    first = service.create_slot(slot_payload())["id"]
    second = service.create_slot(slot_payload(fallback_slot_id=first))["id"]
    with pytest.raises(ServerDomainError) as error:
        service.update_slot(first, {"fallback_slot_id": second})
    assert error.value.code == "MODEL_FALLBACK_CYCLE"
    with pytest.raises(ServerDomainError, match="fallback"):
        service.delete_slot(first)
    with pytest.raises(ServerDomainError) as error:
        service.update_slot(first, {"capabilities": ["text"]})
    assert error.value.code == "MODEL_CAPABILITY_MISMATCH"
    with pytest.raises(ServerDomainError) as error:
        service.create_slot(slot_payload(fallback_slot_id="slot_" + "0" * 32))
    assert error.value.code == "MODEL_SLOT_NOT_FOUND"


@pytest.mark.parametrize("raw", [
    "{invalid json with secret-key",
    '{"schema_version":2,"slots":{},"bindings":{}}',
    '{"schema_version":true,"slots":{},"bindings":{}}',
    '{"schema_version":1,"slots":[],"bindings":{}}',
    '{"schema_version":1,"slots":{},"bindings":{"alpha":{"analysis":"slot_00000000000000000000000000000000"}}}',
])
def test_invalid_storage_is_reported_without_overwrite_or_secret_leak(config_env, raw):
    cm, service, _ = config_env
    path = cm.get_runtime_config_path(CONFIG_FILENAME)
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ServerDomainError) as error:
        service.create_slot(slot_payload())
    assert "secret-key" not in str(error.value)
    assert path.read_text(encoding="utf-8") == raw


def test_concurrent_services_do_not_lose_updates(config_env):
    cm, _, _ = config_env

    def create(index):
        service = ModelConfigService(ModelConfigStore(cm))
        return service.create_slot(slot_payload(name=f"model-{index}"))["id"]

    with ThreadPoolExecutor(max_workers=6) as executor:
        ids = list(executor.map(create, range(18)))
    assert set(ModelConfigStore(cm).read().slots) == set(ids)


def test_write_fence_rejection_does_not_create_config(config_env, monkeypatch):
    import utils.cloudsave_runtime as cloudsave

    cm, service, _ = config_env

    @contextmanager
    def blocked(*args, **kwargs):
        raise cloudsave.MaintenanceModeError("maintenance_readonly")
        yield  # pragma: no cover

    monkeypatch.setattr(cloudsave, "cloudsave_writable_transaction", blocked)
    with pytest.raises(cloudsave.MaintenanceModeError):
        service.create_slot(slot_payload())
    assert not cm.get_runtime_config_path(CONFIG_FILENAME).exists()


@pytest.mark.parametrize("updates", [
    {"base_url": "ftp://models.example/v1"},
    {"base_url": "https://user:secret@models.example/v1"},
    {"base_url": "https://models.example/v1?api_key=secret"},
    {"base_url": "https://models.example:bad/v1"},
    {"model": "  "},
    {"protocol": "realtime"},
    {"api_key": "abc\r\ndef"},
    {"timeout_seconds": float("inf")},
    {"timeout_seconds": True},
    {"defaults": {"max_output_tokens": True}},
    {"capabilities": ["audio_input"]},
])
def test_invalid_slot_configuration_is_rejected(config_env, updates):
    _, service, _ = config_env
    with pytest.raises(ServerDomainError) as error:
        service.create_slot(slot_payload(**updates))
    assert error.value.code == "MODEL_SLOT_INVALID"


def test_manifest_is_read_directly_without_runtime_overrides(tmp_path, monkeypatch):
    from plugin.server.application import model_config_service as module

    manifest = tmp_path / "plugin.toml"
    manifest.write_text('[plugin]\nid="alpha"\n[plugin.models.analysis]\nlabel="Analysis"\ncapabilities=["image_input"]\n')
    monkeypatch.setattr(module, "get_plugin_manifest_path", lambda plugin_id: manifest)
    assert load_model_requirements("alpha")["analysis"].capabilities == ["image_input"]
    manifest.write_text('[plugin]\nid="alpha"\n')
    assert load_model_requirements("alpha") == {}
    with pytest.raises(ServerDomainError):
        load_model_requirements("other")


def test_key_not_in_model_repr():
    slot = ModelSlot.model_validate(slot_payload())
    assert "test-secret-key" not in repr(slot)


def test_store_with_real_config_manager_and_write_transaction(tmp_path, monkeypatch):
    from utils.config_manager import ConfigManager

    # Keep both migration candidates and the selected root inside the fixture.
    monkeypatch.setenv("NEKO_STORAGE_SELECTED_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(ConfigManager, "_get_documents_directory", lambda self: tmp_path)
    monkeypatch.setattr(ConfigManager, "_get_standard_data_directory_candidates", lambda self: [tmp_path])
    monkeypatch.setattr(ConfigManager, "get_legacy_app_root_candidates", lambda self: [])
    monkeypatch.setattr(ConfigManager, "_get_project_config_directory", lambda self: tmp_path / "project_config")
    cm = ConfigManager("N.E.K.O")
    service = ModelConfigService(ModelConfigStore(cm))
    created = service.create_slot(slot_payload())
    slot_id = created["id"]
    service.update_slot(slot_id, {"name": "Saved through real transaction"})
    fresh = ModelConfigService(ModelConfigStore(cm))
    assert fresh.get_slot(slot_id)["name"] == "Saved through real transaction"
    with pytest.raises(ServerDomainError) as error:
        fresh.delete_slot("not-a-slot")
    assert error.value.code == "MODEL_SLOT_NOT_FOUND"
    fresh.delete_slot(slot_id)
    assert fresh.list_slots()["slots"] == []
