"""SDK v2 context facade.

This wrapper hides the legacy host context shape and exposes the async-first
SDK v2 contract that plugins should code against.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from .bus_context import SdkBusContext, ensure_sdk_bus_context
from .finish import (
    build_finish_envelope,
    normalize_delivery,
    normalize_structured_data,
)
from .result_contract import contract_from_meta, validate_reply_payload
from .types import (
    LoggerLike,
    Metadata,
    PluginContextProtocol,
    PluginModelsProtocol,
    PushMessageResult,
)

_UNSET = object()
_SDK_CONTEXT_ATTR_NAMES = ("plugin_id", "metadata", "logger", "config_path", "bus", "images")
_SDK_CONTEXT_METHOD_NAMES = (
    "get_own_config",
    "get_own_base_config",
    "get_own_profiles_state",
    "get_own_profile_config",
    "get_own_effective_config",
    "update_own_config",
    "replace_own_config",
    "upsert_own_profile_config",
    "delete_own_profile_config",
    "set_own_active_profile",
    "query_plugins",
    "trigger_plugin_event",
    "get_system_config",
    "query_memory",
    "run_update",
    "export_push",
    "finish",
    "push_message",
    "update_status",
)


class _HostContextProtocol(Protocol):
    plugin_id: object
    metadata: object
    logger: object
    config_path: object
    bus: object
    _effective_config: object

    async def get_own_config(self, timeout: float = 5.0) -> object: ...

    async def get_own_base_config(self, timeout: float = 5.0) -> object: ...

    async def get_own_profiles_state(self, timeout: float = 5.0) -> object: ...

    async def get_own_profile_config(self, profile_name: str, timeout: float = 5.0) -> object: ...

    async def get_own_effective_config(self, profile_name: str | None = None, timeout: float = 5.0) -> object: ...

    async def update_own_config(self, updates: dict[str, Any], timeout: float = 10.0) -> object: ...

    async def replace_own_config(self, config: dict[str, Any], timeout: float = 10.0) -> object: ...

    async def upsert_own_profile_config(
        self,
        profile_name: str,
        config: dict[str, Any],
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

    async def run_update_async(self, **kwargs: object) -> object: ...

    async def export_push_async(self, **kwargs: object) -> object: ...

    def push_message(self, **kwargs: object) -> PushMessageResult: ...

    def update_status(self, status: dict[str, object]) -> None: ...


class SdkContext:
    """Typed async-first context exposed to SDK v2 plugins."""

    def __init__(self, host_ctx: object):
        self._host_ctx = cast(_HostContextProtocol, host_ctx)
        self._bus_ctx: SdkBusContext | None | object = _UNSET
        self._models_ctx: PluginModelsProtocol | None = None

    @staticmethod
    def _normalize_export_metadata(
        metadata: dict[str, object] | None,
        *,
        delivery: str | None,
    ) -> dict[str, object] | None:
        normalized: dict[str, object] = {}
        if isinstance(metadata, dict):
            for key_obj, value in metadata.items():
                if isinstance(key_obj, str):
                    normalized[key_obj] = value

        if delivery is None:
            return normalized if metadata is not None else None

        raw_agent_meta = normalized.get("agent")
        agent_meta: dict[str, object] = {}
        if isinstance(raw_agent_meta, dict):
            for key_obj, value in raw_agent_meta.items():
                if isinstance(key_obj, str):
                    agent_meta[key_obj] = value
        agent_meta["delivery"] = delivery
        agent_meta["reply"] = delivery != "silent"
        if delivery != "silent" and "include" not in agent_meta:
            agent_meta["include"] = True
        normalized["agent"] = agent_meta
        return normalized

    @staticmethod
    def _metadata_delivery(metadata: Mapping[str, object] | None) -> str | None:
        """Read ``agent.delivery`` (or legacy ``agent.reply`` bool) from metadata.

        Mirrors the priority rules of
        :func:`plugin.sdk.shared.core.finish.normalize_delivery`: if
        ``agent.delivery`` is present (any value, valid or not), it owns the
        decision; we don't fall through to ``agent.reply``. ``delivery`` can
        be a str (preferred) or a bool (very old call sites). Invalid values
        fall back to default delivery. Only when ``delivery`` is absent
        entirely do we consult ``reply``.
        """
        if not isinstance(metadata, Mapping):
            return None
        raw_agent_meta = metadata.get("agent")
        if not isinstance(raw_agent_meta, Mapping):
            return None
        if "delivery" in raw_agent_meta:
            delivery_obj = raw_agent_meta["delivery"]
            if isinstance(delivery_obj, str):
                return normalize_delivery(delivery_obj)
            if isinstance(delivery_obj, bool):
                return normalize_delivery(delivery_obj)
            # delivery key was set but invalid type — fall back to default
            # (don't let agent.reply quietly override an explicit-but-invalid
            # delivery).
            return normalize_delivery(None, None)
        reply_obj = raw_agent_meta.get("reply")
        if isinstance(reply_obj, bool):
            return normalize_delivery(None, reply_obj)
        return None

    def _current_entry_meta(self) -> object | None:
        getter = getattr(self._host_ctx, "get_current_entry_meta", None)
        if getter is None:
            return None
        if callable(getter):
            return getter()
        return None

    def _validate_reply_payload(self, payload: object, *, export_type: str | None = None) -> None:
        entry_meta = self._current_entry_meta()
        if entry_meta is None:
            return
        validate_reply_payload(
            contract_from_meta(entry_meta),
            payload,
            export_type=export_type,
            plugin_id=self.plugin_id,
            entry_id=getattr(entry_meta, "id", None),
        )

    @property
    def plugin_id(self) -> str:
        return str(getattr(self._host_ctx, "plugin_id", "plugin"))

    @property
    def metadata(self) -> Metadata:
        value = getattr(self._host_ctx, "metadata", None)
        return value if isinstance(value, Mapping) else {}

    @property
    def logger(self) -> LoggerLike | None:
        logger = getattr(self._host_ctx, "logger", None)
        return cast(LoggerLike | None, logger)

    @property
    def config_path(self) -> str | Path | None:
        value = getattr(self._host_ctx, "config_path", None)
        if value is None or isinstance(value, (str, Path)):
            return value
        return str(value)

    @property
    def _effective_config(self) -> dict[str, object]:
        value = getattr(self._host_ctx, "_effective_config", None)
        return value if isinstance(value, dict) else {}

    @property
    def bus(self) -> SdkBusContext | None:
        cached = self._bus_ctx
        if cached is not _UNSET:
            return cast(SdkBusContext | None, cached)
        self._bus_ctx = ensure_sdk_bus_context(getattr(self._host_ctx, "bus", None), host_ctx=self._host_ctx)
        return cast(SdkBusContext | None, self._bus_ctx)

    @property
    def images(self) -> object:
        return getattr(self._host_ctx, "images")

    @property
    def models(self) -> PluginModelsProtocol:
        models = getattr(self._host_ctx, "models", None)
        if models is not None:
            return cast(PluginModelsProtocol, models)
        # Older hosts and test contexts have no gateway credentials. Keep
        # their existing facade compatible and report unavailable on use.
        if self._models_ctx is None:
            from .models import PluginModels

            self._models_ctx = PluginModels(self._host_ctx)
        return self._models_ctx

    async def get_own_config(self, timeout: float = 5.0) -> object:
        return await self._host_ctx.get_own_config(timeout=timeout)

    async def get_own_base_config(self, timeout: float = 5.0) -> object:
        return await self._host_ctx.get_own_base_config(timeout=timeout)

    async def get_own_profiles_state(self, timeout: float = 5.0) -> object:
        return await self._host_ctx.get_own_profiles_state(timeout=timeout)

    async def get_own_profile_config(self, profile_name: str, timeout: float = 5.0) -> object:
        return await self._host_ctx.get_own_profile_config(profile_name, timeout=timeout)

    async def get_own_effective_config(self, profile_name: str | None = None, timeout: float = 5.0) -> object:
        return await self._host_ctx.get_own_effective_config(profile_name, timeout=timeout)

    async def update_own_config(self, updates: dict[str, Any], timeout: float = 10.0) -> object:
        return await self._host_ctx.update_own_config(updates, timeout=timeout)

    async def replace_own_config(self, config: dict[str, Any], timeout: float = 10.0) -> object:
        return await self._host_ctx.replace_own_config(config, timeout=timeout)

    async def upsert_own_profile_config(
        self,
        profile_name: str,
        config: dict[str, Any],
        *,
        make_active: bool = False,
        timeout: float = 10.0,
    ) -> object:
        return await self._host_ctx.upsert_own_profile_config(
            profile_name,
            config,
            make_active=make_active,
            timeout=timeout,
        )

    async def delete_own_profile_config(self, profile_name: str, timeout: float = 10.0) -> object:
        return await self._host_ctx.delete_own_profile_config(profile_name, timeout=timeout)

    async def set_own_active_profile(self, profile_name: str, timeout: float = 10.0) -> object:
        return await self._host_ctx.set_own_active_profile(profile_name, timeout=timeout)

    async def query_plugins(self, filters: dict[str, object], timeout: float = 5.0) -> object:
        return await self._host_ctx.query_plugins(filters, timeout=timeout)

    async def trigger_plugin_event(self, **kwargs: object) -> object:
        return await self._host_ctx.trigger_plugin_event(**kwargs)

    async def get_system_config(self, timeout: float = 5.0) -> object:
        return await self._host_ctx.get_system_config(timeout=timeout)

    async def query_memory(self, bucket_id: str, query: str, timeout: float = 5.0) -> object:
        return await self._host_ctx.query_memory(bucket_id, query, timeout=timeout)

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
    ) -> object:
        return await self._host_ctx.run_update_async(
            run_id=run_id,
            progress=progress,
            stage=stage,
            message=message,
            step=step,
            step_total=step_total,
            eta_seconds=eta_seconds,
            metrics=metrics,
            timeout=timeout,
        )

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
    ) -> object:
        # ``delivery`` (3-state) is canonical; ``reply`` (bool) is a deprecated
        # alias kept for backward compatibility. None of either ⇒ leave the
        # caller-provided metadata.agent.* untouched (or ``None`` when no
        # metadata at all was supplied) — we still need a concrete
        # ``delivery_mode`` for the contract-validation gate below, so we
        # fall back to metadata-derived value or the default.
        resolved: str | None = None
        if delivery is not None or reply is not None:
            resolved = normalize_delivery(delivery, reply)
        normalized_metadata = self._normalize_export_metadata(metadata, delivery=resolved)
        delivery_mode = (
            resolved
            or self._metadata_delivery(normalized_metadata)
            or normalize_delivery(None, None)  # default = "proactive"
        )
        normalized_json_data = normalize_structured_data(json_data) if json_data is not None else None
        if delivery_mode != "silent":
            payload: object = normalized_json_data if export_type == "json" else text if export_type == "text" else None
            self._validate_reply_payload(payload, export_type=export_type)
        return await self._host_ctx.export_push_async(
            export_type=export_type,
            run_id=run_id,
            text=text,
            json_data=normalized_json_data,
            url=url,
            binary_data=binary_data,
            binary_url=binary_url,
            mime=mime,
            description=description,
            label=label,
            metadata=normalized_metadata,
            timeout=timeout,
        )

    async def finish(
        self,
        *,
        data: object = None,
        delivery: str | bool | None = None,
        reply: bool | None = None,
        message: str = "",
        trace_id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> Any:
        """Wrap ``data`` into a finish envelope.

        ``delivery`` controls how the result reaches the main AI:
            - ``"proactive"`` (default): start a turn immediately and have the
              character report it.
            - ``"passive"``: write into context but don't interrupt; the next
              user turn will carry it.
            - ``"silent"``: skip the LLM channel entirely (HUD/task_update
              still fires).
        ``reply`` is the deprecated bool alias (``True``→proactive,
        ``False``→silent). When both are provided ``delivery`` wins.

        Role-aware text contract: ``data['summary']`` / ``data['detail']``
        (and any text field that flows into the dialog channel) MAY contain
        ``{MASTER_NAME}`` / ``{LANLAN_NAME}`` placeholders. The host
        substitutes them at the LLM-injection boundary (and at verbatim
        ``direct_reply`` exits), per session. Plugin code can't pick the
        right name itself — visibility filtering decides which session
        receives the text, so substitution has to happen host-side. Prefer
        the placeholders over hardcoded ``"用户"`` / ``"master"`` /
        ``"主人"``. See PLUGIN_DEVELOPMENT_GUIDE.md ("Writing role-aware
        text") for details.
        """
        normalized_data = normalize_structured_data(data)
        resolved = normalize_delivery(delivery, reply)
        if resolved != "silent":
            self._validate_reply_payload(normalized_data, export_type="json")
        return build_finish_envelope(
            data=normalized_data,
            delivery=resolved,
            message=message,
            trace_id=trace_id,
            meta=meta,
        )

    def push_message(
        self,
        *,
        # ── v2 schema (preferred) ─────────────────────────────────────
        visibility: list[str] | None = None,
        ai_behavior: str | None = None,
        parts: list[dict[str, object]] | None = None,
        # ── common ────────────────────────────────────────────────────
        source: str = "",
        target_lanlan: str | None = None,
        metadata: dict[str, object] | None = None,
        priority: int = 0,
        coalesce_key: str | None = None,
        # ── v1 legacy (each emits DeprecationWarning when used) ───────
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
    ) -> PushMessageResult:
        """Push a message from a plugin to the host.

        Two orthogonal axes drive the host's downstream behaviour:

        * ``visibility`` (``list["chat" | "hud"]``, default ``[]``) — where
          the user sees the *plugin's* parts rendered verbatim.  Empty
          list means the user does not see the parts directly; if AI also
          responds, only the AI's reply is visible.
        * ``ai_behavior`` (``"respond" | "read" | "blind"``, default
          ``"respond"``) — how the LLM treats the parts: generate a turn
          immediately, ingest into context for natural mention later, or
          ignore entirely.

        ``parts`` is an ordered list of content parts (``text``,
        ``image``, ``audio``, ``video``, ``ui_action``).  See
        :mod:`plugin.sdk.shared.core.push_message_schema` for full shapes.

        Role-aware text contract: any text field that ends up in the dialog
        channel (``parts[*].text`` for ``ai_behavior="respond"|"read"`` /
        ``visibility=["chat"]``) MAY contain ``{MASTER_NAME}`` /
        ``{LANLAN_NAME}`` placeholders; the host expands them per session at
        the injection boundary. Plugin code can't pick the right name
        itself — visibility filtering happens host-side. Prefer the
        placeholders over hardcoded ``"用户"`` / ``"master"`` / ``"主人"``.
        See PLUGIN_DEVELOPMENT_GUIDE.md ("Writing role-aware text") for
        details.

        All other parameters are deprecated and emit ``DeprecationWarning``;
        scheduled for removal in v0.9 (``docs/changelog``).

        The returned ``submitted`` flag only confirms that an SDK-owned local
        socket or queue accepted responsibility for the payload, not host
        consumption, model generation, or playback.
        """
        return self._host_ctx.push_message(
            visibility=visibility,
            ai_behavior=ai_behavior,
            parts=parts,
            source=source,
            target_lanlan=target_lanlan,
            metadata=metadata,
            priority=priority,
            coalesce_key=coalesce_key,
            message_type=message_type,
            description=description,
            content=content,
            binary_data=binary_data,
            binary_url=binary_url,
            mime=mime,
            unsafe=unsafe,
            fast_mode=fast_mode,
            delivery=delivery,
            reply=reply,
        )

    def update_status(self, status: dict[str, object]) -> None:
        self._host_ctx.update_status(status)


def _is_sdk_context_compatible(ctx: object) -> bool:
    return all(hasattr(ctx, attr_name) for attr_name in _SDK_CONTEXT_ATTR_NAMES) and all(
        callable(getattr(ctx, method_name, None)) for method_name in _SDK_CONTEXT_METHOD_NAMES
    )


def ensure_sdk_context(ctx: PluginContextProtocol | object) -> PluginContextProtocol:
    if isinstance(ctx, SdkContext):
        return ctx
    if _is_sdk_context_compatible(ctx):
        return cast(PluginContextProtocol, ctx)
    return cast(PluginContextProtocol, SdkContext(ctx))


__all__ = ["SdkContext", "ensure_sdk_context"]
