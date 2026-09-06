"""Model-slot CRUD and manifest-owned plugin bindings; no model calls."""
from __future__ import annotations

import tomllib
from collections.abc import Callable
from uuid import uuid4

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from plugin.config.schema import PluginModelRequirementSchema, PluginModelUsageId
from plugin.server.domain.errors import ServerDomainError
from plugin.server.domain.model_config import (
    SECRET_MASK,
    ModelSlot,
    PluginModelsConfig,
    is_secret_mask,
    secret_preview,
)
from plugin.server.infrastructure.config_paths import get_plugin_manifest_path
from plugin.server.infrastructure.model_config_store import ModelConfigStore
from utils.http.url import same_endpoint

Requirements = dict[PluginModelUsageId, PluginModelRequirementSchema]
_requirements_adapter = TypeAdapter(Requirements)


def load_model_requirements(plugin_id: str) -> Requirements:
    """Read declarations from the installed manifest, never runtime overrides."""
    try:
        path = get_plugin_manifest_path(plugin_id)
        with path.open("rb") as source:
            manifest = tomllib.load(source)
        plugin = manifest.get("plugin", {})
        if plugin.get("id") != plugin_id:
            raise ValueError("Plugin manifest identity mismatch")
        return _requirements_adapter.validate_python(plugin.get("models", {}))
    except HTTPException as exc:
        raise ServerDomainError("PLUGIN_NOT_FOUND", "Plugin manifest not found", exc.status_code) from exc
    except (OSError, ValueError, AttributeError) as exc:
        raise ServerDomainError("MODEL_REQUIREMENTS_INVALID", "Plugin model declarations could not be read", 400) from exc


def _validate_slot(payload: dict) -> ModelSlot:
    try:
        return ModelSlot.model_validate(payload)
    except ValidationError as exc:
        fields = [".".join(map(str, error["loc"])) for error in exc.errors(include_input=False)]
        raise ServerDomainError(
            "MODEL_SLOT_INVALID", "Invalid model slot fields", 422, {"fields": fields}
        ) from exc


class ModelConfigService:
    def __init__(
        self,
        store: ModelConfigStore | None = None,
        requirements_loader: Callable[[str], Requirements] = load_model_requirements,
    ):
        self.store = store or ModelConfigStore()
        self.requirements_loader = requirements_loader

    @staticmethod
    def _slot(config: PluginModelsConfig, slot_id: str) -> ModelSlot:
        slot = config.slots.get(slot_id)
        if slot is None:
            raise ServerDomainError("MODEL_SLOT_NOT_FOUND", "Model slot not found", 404)
        return slot

    @staticmethod
    def _view(config: PluginModelsConfig, slot_id: str) -> dict:
        slot = config.slots[slot_id]
        result = slot.model_dump(mode="json", exclude={"api_key"})
        result.update(
            id=slot_id,
            api_key=SECRET_MASK if slot.api_key else "",
            api_key_preview=secret_preview(slot.api_key),
            bound_by=[
                {"plugin_id": plugin_id, "usage_id": usage_id}
                for plugin_id, bindings in sorted(config.bindings.items())
                for usage_id, target in sorted(bindings.items())
                if target == slot_id
            ],
        )
        return result

    def list_slots(self) -> dict:
        config = self.store.read()
        return {"schema_version": config.schema_version, "slots": [self._view(config, key) for key in config.slots]}

    def get_slot(self, slot_id: str) -> dict:
        config = self.store.read()
        self._slot(config, slot_id)
        return self._view(config, slot_id)

    def create_slot(self, payload: dict) -> dict:
        slot = _validate_slot(payload)
        if is_secret_mask(slot.api_key):
            raise ServerDomainError("MODEL_SLOT_INVALID", "Enter an API key or leave it empty", 422)
        slot_id = "slot_" + uuid4().hex

        def change(config):
            self._validate_fallback(config, slot_id, slot)
            config.slots[slot_id] = slot
            return self._view(config, slot_id)

        return self.store.update(change)

    @staticmethod
    def _validate_fallback(config: PluginModelsConfig, slot_id: str, slot: ModelSlot) -> None:
        fallback = slot.fallback_slot_id
        seen = {slot_id}
        while fallback is not None:
            if fallback in seen:
                raise ServerDomainError("MODEL_FALLBACK_CYCLE", "Fallback slots must not form a cycle", 409)
            seen.add(fallback)
            target = ModelConfigService._slot(config, fallback)
            if not set(slot.capabilities).issubset(target.capabilities):
                raise ServerDomainError("MODEL_CAPABILITY_MISMATCH", "Fallback lacks required slot capabilities", 409)
            fallback = target.fallback_slot_id

    def update_slot(self, slot_id: str, payload: dict) -> dict:
        def change(config):
            old = self._slot(config, slot_id)
            updates = dict(payload)
            key = updates.get("api_key")
            if isinstance(key, str) and is_secret_mask(key.strip()):
                updates.pop("api_key")
            slot = _validate_slot({**old.model_dump(), **updates})
            if old.api_key and "api_key" not in updates and (
                old.protocol != slot.protocol or not same_endpoint(old.base_url, slot.base_url)
            ):
                raise ServerDomainError(
                    "MODEL_CREDENTIAL_UPDATE_REQUIRED",
                    "Changing endpoint or protocol requires an explicit API key update (empty clears it)", 409,
                )
            config.slots[slot_id] = slot
            for key, candidate in config.slots.items():
                self._validate_fallback(config, key, candidate)
            if set(slot.capabilities) != set(old.capabilities):
                for plugin_id, bindings in config.bindings.items():
                    if slot_id not in bindings.values():
                        continue
                    try:
                        requirements = self.requirements_loader(plugin_id)
                    except ServerDomainError as exc:
                        if exc.code == "PLUGIN_NOT_FOUND":
                            continue  # Stale bindings remain visible and can be explicitly removed.
                        raise
                    for usage_id, target in bindings.items():
                        requirement = requirements.get(usage_id)
                        if target == slot_id and requirement is not None:
                            self._check_capabilities(slot, requirement)
            return self._view(config, slot_id)

        return self.store.update(change)

    def delete_slot(self, slot_id: str) -> dict:
        def change(config):
            self._slot(config, slot_id)
            if any(slot_id in bindings.values() for bindings in config.bindings.values()):
                raise ServerDomainError("MODEL_SLOT_IN_USE", "Unbind plugins before deleting this slot", 409)
            if any(slot.fallback_slot_id == slot_id for slot in config.slots.values()):
                raise ServerDomainError("MODEL_SLOT_IN_USE", "Remove fallback references before deleting this slot", 409)
            del config.slots[slot_id]
            return {"success": True}

        return self.store.update(change)

    @staticmethod
    def _check_capabilities(slot: ModelSlot, requirement: PluginModelRequirementSchema) -> None:
        if not set(requirement.capabilities).issubset(slot.capabilities):
            raise ServerDomainError("MODEL_CAPABILITY_MISMATCH", "Model slot does not meet plugin requirements", 409)

    def get_bindings(self, plugin_id: str) -> dict:
        requirements = self.requirements_loader(plugin_id)
        config = self.store.read()
        bindings = config.bindings.get(plugin_id, {})
        usages = {}
        for usage_id, requirement in requirements.items():
            slot_id = bindings.get(usage_id)
            slot = config.slots.get(slot_id) if slot_id else None
            status = "unbound" if slot is None else (
                "bound" if set(requirement.capabilities).issubset(slot.capabilities) else "incompatible"
            )
            usages[usage_id] = {**requirement.model_dump(), "slot_id": slot_id, "status": status}
        return {
            "plugin_id": plugin_id,
            "requirements": usages,
            "bindings": dict(bindings),
            "ready": all(not item["required"] or item["status"] == "bound" for item in usages.values()),
        }

    def set_binding(self, plugin_id: str, usage_id: str, slot_id: str) -> dict:
        requirements = self.requirements_loader(plugin_id)
        requirement = requirements.get(usage_id)
        if requirement is None:
            raise ServerDomainError("MODEL_USAGE_NOT_DECLARED", "Plugin has not declared this model usage", 400)

        def change(config):
            slot = self._slot(config, slot_id)
            self._check_capabilities(slot, requirement)
            config.bindings.setdefault(plugin_id, {})[usage_id] = slot_id
            return {"plugin_id": plugin_id, "usage_id": usage_id, "slot_id": slot_id}

        return self.store.update(change)

    def delete_binding(self, plugin_id: str, usage_id: str) -> dict:
        # Permit cleanup after uninstall or after a manifest removes a usage.
        def change(config):
            bindings = config.bindings.get(plugin_id, {})
            bindings.pop(usage_id, None)
            if not bindings:
                config.bindings.pop(plugin_id, None)
            return {"success": True}

        return self.store.update(change)
