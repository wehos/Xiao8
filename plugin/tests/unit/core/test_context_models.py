from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from plugin.core.context import PluginContext
from plugin.sdk.shared.core.context import SdkContext
from plugin.sdk.shared.models.exceptions import CapabilityUnavailableError


def _context(tmp_path: Path) -> PluginContext:
    return PluginContext(
        plugin_id="demo",
        config_path=tmp_path / "plugin.toml",
        logger=None,
        status_queue=None,
        _model_gateway_base_url="http://127.0.0.1:48916/api/models/v1",
        _model_gateway_token="private-instance-token",
    )


def test_gateway_details_are_not_in_context_repr(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert "private-instance-token" not in repr(context)
    assert "48916" not in repr(context)
    assert context.models is context.models
    assert SdkContext(context).models is context.models
    context.close()


def test_context_close_before_first_client_disables_gateway(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert context._models is None
    context.close()
    with pytest.raises(CapabilityUnavailableError):
        asyncio.run(context.models.get_client())


@pytest.mark.asyncio
async def test_context_close_releases_client_and_blocks_new_requests(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    client = await context.models.get_client()
    context.close()
    await context.models.aclose()
    assert client.is_closed()
    with pytest.raises(CapabilityUnavailableError):
        await context.models.get_client()
    context.close()
