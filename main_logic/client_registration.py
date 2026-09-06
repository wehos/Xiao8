"""Idempotent ``POST /api/clients/register`` bootstrap for N.E.K.O.Servers.

``config_manager.ensure_cloudsave_client_credentials()`` mints the local
``client_id``/``client_proof`` pair without ever touching the network, so until
this module has registered them the cloud holds no row for the id. Every
proof-bearing callback such as ``clients/bind-approval`` then fails closed with
403 even though the local install is perfectly healthy. Registration used to
live inside the facts-sync sweep, which is off unless
``NEKO_FACTS_SYNC_ENABLED=1``. This module owns the bootstrap so registration is
not accidentally gated by an optional sync worker.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx
from utils.social_base import (
    DEFAULT_SOCIAL_BASE_URL,
    configured_social_base_url,
    social_base_url,
)

logger = logging.getLogger("neko.client_registration")

HTTP_TIMEOUT_SEC = 15.0
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Details that mean "the cloud does not know this client_id (yet)". A stale
# proof mismatch reports the same code, so a retry is only ever attempted once.
UNREGISTERED_DETAILS = frozenset(
    {"invalid_client_proof", "client_not_registered", "client_not_found"}
)

_registered: dict[str, bool] = {}
_register_lock = asyncio.Lock()
def proof_transport_allowed(base_url: str) -> bool:
    """Whether ``client_proof`` may be sent to ``base_url`` at all.

    HTTPS is fine anywhere; plain HTTP is only acceptable on loopback, where the
    secret never leaves the machine. Anything else would leak the device secret.
    """
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and hostname in LOOPBACK_HOSTS


def looks_unregistered(status_code: int, detail: object) -> bool:
    """Whether a cloud rejection is consistent with a missing client row."""
    if status_code not in (401, 403, 404):
        return False
    return str(detail or "").strip() in UNREGISTERED_DETAILS


def _load_credentials() -> tuple[str, str] | None:
    try:
        from utils.config_manager import get_config_manager

        client_id, client_proof = (
            get_config_manager().ensure_cloudsave_client_credentials()
        )
        if not client_id or not client_proof:
            return None
        return client_id, client_proof
    except Exception as exc:  # noqa: BLE001
        logger.warning("client_registration: loading credentials failed: %s", exc)
    return None


async def ensure_client_registered(
    base_url: str | None = None,
    client_id: str | None = None,
    client_proof: str | None = None,
    *,
    force: bool = False,
) -> bool:
    """Register the local client with the cloud, at most once per base URL.

    Callers that already hold the credential pair pass it in; everyone else lets
    this load the persisted pair. ``force`` re-posts even when this process
    already cached a success, which is what a 403 on a proof-bearing call needs:
    the cached "registered" flag may predate a cloud redeploy that lost the row.
    """
    base_url = (base_url or social_base_url()).rstrip("/")
    if not proof_transport_allowed(base_url):
        logger.warning(
            "client_registration: refusing to send client_proof to %s", base_url
        )
        return False

    if not client_id or not client_proof:
        credentials = await asyncio.to_thread(_load_credentials)
        if not credentials:
            return False
        client_id, client_proof = credentials

    cache_key = f"{base_url}|{client_id}"
    if force:
        _registered.pop(cache_key, None)
    elif _registered.get(cache_key):
        return True

    async with _register_lock:
        if _registered.get(cache_key):  # another waiter registered it
            return True
        url = f"{base_url}/api/clients/register"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
                response = await client.post(
                    url,
                    json={"client_id": client_id, "client_proof": client_proof},
                )
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("client_registration: register HTTP failed: %s", exc)
            return False
        if response.status_code < 300:
            _registered[cache_key] = True
            logger.info(
                "client_registration: client %s… registered with %s",
                client_id[:8],
                base_url,
            )
            return True
        logger.warning(
            "client_registration: register %s returned %s: %s",
            url,
            response.status_code,
            response.text[:200],
        )
        return False


def reset_registration_cache() -> None:
    """Clear the in-process cache; used by tests and after credential rotation."""
    _registered.clear()
