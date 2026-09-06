"""QQ connector factory + connection interface Protocol.

The chat plugin calls :func:`create_qq_connection` to build the right concrete
connection from transport settings, then holds it as a :class:`QQConnector`.
The connector owns the transport + message-handling chain; the plugin owns
message enrichment (reply/forward/voice/file + VLM/STT, via ``QQMessageEnricher``)
and consumes the normalized messages / send API.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .qq_client import QQClient
from .qq_connection import QQConnectionBase
from .qq_open_plat import QQOpenPlatformConnection


def create_qq_connection(
    settings_or_reader: Any,
    *,
    logger: Any = None,
    emit_log: Any = None,
) -> QQConnectionBase:
    """Build the concrete QQ connection from transport settings.

    ``settings_or_reader`` is either a settings dict or a zero-arg callable that
    returns the live settings dict. Passing a callable keeps the open-platform
    ``identity_probe`` reading live settings per event (a toggle flip takes effect
    without a reconnect), matching the plugin's historical behavior.
    """
    get_settings = settings_or_reader if callable(settings_or_reader) else (lambda: settings_or_reader)
    settings = get_settings() or {}
    mode = str(settings.get("qq_connection_mode", "napcat") or "napcat").strip()

    if mode == "open_platform":
        return QQOpenPlatformConnection(
            app_id=str(settings.get("qq_open_app_id") or "").strip(),
            client_secret=str(settings.get("qq_open_client_secret") or "").strip(),
            logger=logger,
            identity_probe=lambda: bool(
                get_settings().get("qq_open_identity_probe_enabled", False)
            ),
            emit_log=emit_log,
        )

    return QQClient(
        onebot_url=str(settings.get("onebot_url") or "ws://0.0.0.0:6199"),
        token=str(settings.get("token") or ""),
        logger=logger,
        emit_log=emit_log,
        # napcat_forward = forward WS client (dials out to the OneBot implementation's WS server);
        # everything else (incl. the default) uses the reverse WS server.
        direction="forward" if mode == "napcat_forward" else "reverse",
    )


@runtime_checkable
class QQConnector(Protocol):
    """The connection surface any plugin consumes.

    Concrete instances are ``QQClient`` (OneBot) or ``QQOpenPlatformConnection``
    (open platform). Both already expose every member below; the Protocol is a
    type annotation only (checked structurally, not at construction).
    """

    token: str

    # ── lifecycle ───────────────────────────────────────────
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    async def receive_message(self, timeout: float = 1.0) -> Optional[dict[str, Any]]: ...

    # ── send surface ────────────────────────────────────────
    async def send_group_message_segments(
        self, group_id: str, segments: list[dict[str, Any]], *, record_sent: bool = True
    ) -> Optional[str]: ...
    async def send_private_message_segments(
        self, user_id: str, segments: list[dict[str, Any]]
    ) -> Optional[str]: ...
    async def send_group_poke(self, group_id: str, user_id: str) -> bool: ...
    async def send_group_image(
        self, group_id: str, image_data: str, *, reply_message_id: str = "", at_user_id: str = "", sub_type: str = ""
    ) -> Optional[str]: ...
    async def send_group_record(
        self, group_id: str, file_uri: str, *, reply_message_id: str = "", at_user_id: str = ""
    ) -> Optional[str]: ...

    # ── info / state ────────────────────────────────────────
    async def get_login_status(self) -> dict[str, Any]: ...
    def record_sent_message_id(self, message_id: str) -> None: ...
    def is_group_muted(self, group_id: str) -> bool: ...
    @property
    def onebot_url(self) -> str: ...
    @property
    def self_id(self) -> str: ...
    @property
    def sent_message_ids(self) -> dict[str, float]: ...

    # ── capability flags ────────────────────────────────────
    @property
    def needs_attention(self) -> bool: ...
    @property
    def supports_voice(self) -> bool: ...
    @property
    def supports_poke(self) -> bool: ...
    @property
    def supports_ark_cards(self) -> bool: ...

    # ── extended send ───────────────────────────────────────
    async def send_group_ark_card(
        self, group_id: str, ark_obj: dict[str, Any]
    ) -> bool: ...
    def set_inbound_sink(self, sink: Any | None) -> None: ...
