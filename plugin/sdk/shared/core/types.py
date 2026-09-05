"""Shared core contract types for SDK v2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Mapping,
    MutableMapping,
    Protocol,
    TypeAlias,
    TypedDict,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
Metadata: TypeAlias = Mapping[str, JsonValue]
InputSchema: TypeAlias = Mapping[str, JsonValue]
EntryHandler: TypeAlias = Callable[..., object]


PushMessageFailureReason: TypeAlias = Literal[
    "backpressure",
    # The SDK measured the wire payload the way the host's ingest server does
    # and it blew MESSAGE_PLANE_PAYLOAD_MAX_BYTES. Unlike "backpressure" this
    # is not transient: the host would discard the WHOLE push (text parts
    # included) and the author would only ever see it in the host log, so the
    # SDK rejects it locally instead of reporting a submission that silently
    # goes nowhere. Retrying an identical payload cannot help -- the push has
    # to get smaller, which for images means ctx.images.upload().
    "payload_too_large",
    "transport_error",
    "transport_unavailable",
]


class PushMessageSubmitted(TypedDict):
    """The SDK accepted responsibility for a local message submission."""

    submitted: Literal[True]


class PushMessageRejected(TypedDict):
    """The SDK synchronously rejected a local message submission."""

    ok: Literal[False]
    submitted: Literal[False]
    reason: PushMessageFailureReason


# Immediate local submission result. ``submitted=True`` only means that the
# SDK's authoritative local submission path accepted responsibility for the
# payload; it does not acknowledge host consumption, model generation, or
# playback.
PushMessageResult: TypeAlias = PushMessageSubmitted | PushMessageRejected


class LoggerLike(Protocol):
    """Minimal logger contract used by SDK interfaces."""

    def debug(self, message: str, *args: object, **kwargs: object) -> object: ...

    def info(self, message: str, *args: object, **kwargs: object) -> object: ...

    def warning(self, message: str, *args: object, **kwargs: object) -> object: ...

    def error(self, message: str, *args: object, **kwargs: object) -> object: ...

    def exception(self, message: str, *args: object, **kwargs: object) -> object: ...


@dataclass(slots=True)
class PluginRef:
    plugin_id: str


@dataclass(slots=True)
class EntryRef:
    plugin_id: str
    entry_id: str


@dataclass(slots=True)
class EventRef:
    plugin_id: str
    event_type: str
    event_id: str


# ---------------------------------------------------------------------------
# Bus protocols (single set, use Optional at usage sites where needed)
# ---------------------------------------------------------------------------

class BusMessagesProtocol(Protocol):
    def get(self, **kwargs: object) -> object: ...


class BusEventsProtocol(Protocol):
    def get(self, **kwargs: object) -> object: ...


class BusLifecycleProtocol(Protocol):
    def get(self, **kwargs: object) -> object: ...


class BusConversationsProtocol(Protocol):
    def get(self, **kwargs: object) -> object: ...

    def get_by_id(self, conversation_id: str, max_count: int = 10, timeout: float | None = None) -> object: ...


class BusFramesProtocol(Protocol):
    def get(self, **kwargs: object) -> object: ...


class BusMemoryProtocol(Protocol):
    def get(self, *, bucket_id: str, limit: int = 20, timeout: float = 5.0) -> object: ...


class BusProtocol(Protocol):
    messages: BusMessagesProtocol | None
    events: BusEventsProtocol | None
    lifecycle: BusLifecycleProtocol | None
    conversations: BusConversationsProtocol | None
    frames: BusFramesProtocol | None
    memory: BusMemoryProtocol | None


class PluginImagesProtocol(Protocol):
    async def upload(
        self,
        data: bytes | bytearray,
        *,
        mime: str | None = None,
        timeout: float = 3.0,
    ) -> dict[str, object]: ...


class PluginModelsProtocol(Protocol):
    async def get_client(self) -> AsyncOpenAI: ...


class PluginContextProtocol(Protocol):
    @property
    def images(self) -> PluginImagesProtocol: ...

    @property
    def models(self) -> PluginModelsProtocol: ...

    plugin_id: str
    metadata: Metadata
    logger: LoggerLike | None
    config_path: str | Path | None
    bus: BusProtocol | None

    async def get_own_config(self, timeout: float = 5.0) -> object: ...

    async def get_own_base_config(self, timeout: float = 5.0) -> object: ...

    async def get_own_profiles_state(self, timeout: float = 5.0) -> object: ...

    async def get_own_profile_config(self, profile_name: str, timeout: float = 5.0) -> object: ...

    async def get_own_effective_config(self, profile_name: str | None = None, timeout: float = 5.0) -> object: ...

    async def update_own_config(self, updates: JsonObject, timeout: float = 10.0) -> object: ...

    async def replace_own_config(self, config: JsonObject, timeout: float = 10.0) -> object: ...

    async def upsert_own_profile_config(
        self,
        profile_name: str,
        config: JsonObject,
        *,
        make_active: bool = False,
        timeout: float = 10.0,
    ) -> object: ...

    async def delete_own_profile_config(self, profile_name: str, timeout: float = 10.0) -> object: ...

    async def set_own_active_profile(self, profile_name: str, timeout: float = 10.0) -> object: ...

    async def query_plugins(self, filters: dict[str, object], timeout: float = 5.0) -> object: ...

    async def trigger_plugin_event(self, **kwargs: object) -> object: ...

    async def get_system_config(self, timeout: float = 5.0) -> object: ...

    async def query_memory(self, bucket_id: str, query: str, timeout: float = 5.0) -> object: ...

    async def run_update(
        self,
        *,
        run_id: str | None = None,
        progress: float | None = None,
        stage: str | None = None,
        message: str | None = None,
        step: int | None = None,
        step_total: int | None = None,
        eta_seconds: float | None = None,
        metrics: dict[str, object] | None = None,
        timeout: float = 5.0,
    ) -> object: ...

    async def export_push(
        self,
        *,
        export_type: str,
        run_id: str | None = None,
        text: str | None = None,
        json_data: dict[str, object] | None = None,
        url: str | None = None,
        binary_data: bytes | None = None,
        binary_url: str | None = None,
        mime: str | None = None,
        description: str | None = None,
        label: str | None = None,
        metadata: dict[str, object] | None = None,
        delivery: str | bool | None = None,
        reply: bool | None = None,
        timeout: float = 5.0,
    ) -> object: ...

    async def finish(
        self,
        *,
        data: object = None,
        delivery: str | bool | None = None,
        reply: bool | None = None,
        message: str = "",
        trace_id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> Any: ...

    def push_message(
        self,
        *,
        # v2 schema:
        visibility: list[str] | None = None,
        ai_behavior: str | None = None,
        parts: list[dict[str, object]] | None = None,
        # common:
        source: str = "",
        target_lanlan: str | None = None,
        metadata: dict[str, object] | None = None,
        priority: int = 0,
        coalesce_key: str | None = None,
        # legacy (deprecated; translated by host adapter):
        message_type: str | None = None,
        description: str | None = None,
        content: str | None = None,
        binary_data: bytes | None = None,
        binary_url: str | None = None,
        mime: str | None = None,
        unsafe: bool = False,
        fast_mode: bool = False,
        delivery: str | bool | None = None,
        reply: bool | None = None,
    ) -> PushMessageResult: ...

    def update_status(self, status: dict[str, object]) -> None: ...


class MutableStateProtocol(Protocol):
    def as_dict(self) -> MutableMapping[str, JsonValue]: ...


class RouterProtocol(Protocol):
    def name(self) -> str: ...

    def set_prefix(self, prefix: str) -> None: ...

    def iter_handlers(self) -> Mapping[str, EntryHandler]: ...


__all__ = [
    "BusConversationsProtocol",
    "BusFramesProtocol",
    "BusEventsProtocol",
    "BusLifecycleProtocol",
    "BusMemoryProtocol",
    "BusMessagesProtocol",
    "BusProtocol",
    "EntryHandler",
    "EntryRef",
    "EventRef",
    "InputSchema",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "LoggerLike",
    "Metadata",
    "MutableStateProtocol",
    "PluginContextProtocol",
    "PluginRef",
    "PushMessageFailureReason",
    "PushMessageRejected",
    "PushMessageResult",
    "PluginImagesProtocol",
    "PluginModelsProtocol",
    "PushMessageSubmitted",
    "RouterProtocol",
]
