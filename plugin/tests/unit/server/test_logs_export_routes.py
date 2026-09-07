"""测试日志导出路由的安全性"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from plugin.server.routes.logs import export_plugin_log_endpoint


pytestmark = pytest.mark.plugin_unit


@pytest.mark.asyncio
async def test_export_rejects_newline_in_plugin_id():
    """拒绝包含换行符的 plugin_id，防止 HTTP 响应头注入"""
    # 测试 \n
    with pytest.raises(HTTPException) as exc_info:
        await export_plugin_log_endpoint("foo\n", _="test-admin")
    assert exc_info.value.status_code == 400
    assert "newline" in str(exc_info.value.detail).lower()

    # 测试 \r
    with pytest.raises(HTTPException) as exc_info:
        await export_plugin_log_endpoint("foo\r", _="test-admin")
    assert exc_info.value.status_code == 400
    assert "newline" in str(exc_info.value.detail).lower()

    # 测试 \r\n
    with pytest.raises(HTTPException) as exc_info:
        await export_plugin_log_endpoint("foo\r\n", _="test-admin")
    assert exc_info.value.status_code == 400
    assert "newline" in str(exc_info.value.detail).lower()
