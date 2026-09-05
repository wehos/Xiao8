"""Persisted, host-owned model slots for plugin use (no runtime clients)."""
from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from plugin.config.schema import ModelCapability, PluginModelUsageId

SECRET_MASK = "__NEKO_SECRET_MASKED__"
SlotId = Annotated[StrictStr, Field(pattern=r"^slot_[0-9a-f]{32}$")]
PluginId = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]


def is_secret_mask(value: str) -> bool:
    """Accept the host sentinel and all-star/bullet display masks as no-op edits."""
    return value == SECRET_MASK or (len(value) >= 3 and set(value) in ({"*"}, {"•"}))


class ModelDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)


class ModelSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=128)
    protocol: Literal["openai_chat", "anthropic_messages"]
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key: str = Field(default="", max_length=4096, repr=False)
    capabilities: list[ModelCapability] = Field(default_factory=lambda: ["text"])
    defaults: ModelDefaults = Field(default_factory=ModelDefaults)
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    fallback_slot_id: SlotId | None = None

    @field_validator("name", "model", "api_key", mode="before")
    @classmethod
    def strip_strings(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("API key must not contain control characters")
        return value

    @field_validator("base_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        value = value.strip()
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("base_url must be an HTTP(S) base URL")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("base_url must be HTTP(S), without userinfo, query or fragment")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: list[ModelCapability]) -> list[ModelCapability]:
        return list(dict.fromkeys(["text", *value]))


class PluginModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    slots: dict[SlotId, ModelSlot] = Field(default_factory=dict)
    bindings: dict[PluginId, dict[PluginModelUsageId, SlotId]] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_references(self) -> PluginModelsConfig:
        for slot_id, slot in self.slots.items():
            if is_secret_mask(slot.api_key):
                raise ValueError("A display mask is not a stored API key")
            seen = {slot_id}
            fallback = slot.fallback_slot_id
            while fallback is not None:
                if fallback not in self.slots:
                    raise ValueError("Fallback slot does not exist")
                if fallback in seen:
                    raise ValueError("Fallback slots must not form a cycle")
                seen.add(fallback)
                fallback = self.slots[fallback].fallback_slot_id
        for bindings in self.bindings.values():
            if any(slot_id not in self.slots for slot_id in bindings.values()):
                raise ValueError("Bound model slot does not exist")
        return self
