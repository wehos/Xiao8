"""QQ connector — plugin-agnostic transport library.

This package owns the QQ *transport*: OneBot v11 WebSocket client (forward +
reverse), the QQ Open Platform WS gateway, the NapCat process manager, and the
send/normalize surface. Message enrichment (reply/forward/voice/file + VLM/STT)
lives in the plugin. It is imported by plugins and instantiated in-process; it
never imports a plugin.

``create_qq_connection`` (from :mod:`utils.connection.qq.factory`) is the single
entry point that builds the right concrete connection from transport settings.
"""

from __future__ import annotations

from .factory import QQConnector, create_qq_connection
from .qq_client import QQClient
from .qq_connection import QQConnectionBase
from .qq_open_plat import QQOpenPlatformConnection

__all__ = [
    "create_qq_connection",
    "QQConnector",
    "QQConnectionBase",
    "QQClient",
    "QQOpenPlatformConnection",
]
