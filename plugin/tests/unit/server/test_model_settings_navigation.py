"""Exercise the existing Main redirect without booting inference dependencies."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from urllib.parse import urlencode

import pytest
from starlette.requests import Request
from starlette.responses import RedirectResponse

pytestmark = pytest.mark.plugin_unit
ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def redirect():
    source = (ROOT / "main_routers/agent_router.py").read_text(encoding="utf-8")
    node = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "redirect_plugin_dashboard"
    )
    node.decorator_list = []

    async def resolve_base():
        return "http://127.0.0.1:50123"

    namespace = {
        "os": os, "Request": Request, "RedirectResponse": RedirectResponse,
        "urlencode": urlencode, "_resolve_user_plugin_base": resolve_base,
        "_is_loopback_origin": lambda value: value == "http://localhost:50111",
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "agent_router.py", "exec"), namespace)
    return namespace["redirect_plugin_dashboard"]


@pytest.mark.parametrize("behind_proxy", [False, True])
@pytest.mark.parametrize("page,suffix", [
    ("model-api", "/model-api"), ("", ""),
    ("https://example.test", ""), ("../api/config/core_api", ""),
])
async def test_plugin_settings_redirect_uses_resolved_origin_and_fixed_page(
    redirect, monkeypatch, behind_proxy, page, suffix,
):
    monkeypatch.setenv("NEKO_BEHIND_PROXY", "true" if behind_proxy else "false")
    request = Request({
        "type": "http", "method": "GET", "path": "/api/agent/user_plugin/dashboard",
        "headers": [], "query_string": urlencode({
            "page": page, "v": "revision", "unsafe": "https://example.test",
            "yui_opener_origin": "http://localhost:50111",
        }).encode(),
    })
    response = await redirect(request)
    origin = "" if behind_proxy else "http://127.0.0.1:50123"
    assert response.headers["location"] == origin + "/ui" + suffix + "?" + urlencode({
        "v": "revision", "yui_opener_origin": "http://localhost:50111",
    })


def test_docker_http_and_https_route_only_model_management_namespace_to_plugins():
    source = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    patterns = re.findall(r"location ~ (\^/\(api/model-config[^\s]+) \{", source)
    assert len(patterns) == 2
    for pattern in patterns:
        assert re.match(pattern, "/api/model-config/slots")
        assert re.match(pattern, "/api/model-config/usage")
        assert not re.match(pattern, "/api/model-config-extra")
        assert not re.match(pattern, "/api/config/core_api")
        assert not re.match(pattern, "/api/models/v1/chat/completions")
