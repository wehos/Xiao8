from __future__ import annotations

# 这一段与 plugin/conftest.py 重复是有意的：plugin/tests 自带 pytest.ini，跑
# `pytest plugin/tests` 时 rootdir 就是它，confcutdir 会把上层的 plugin/conftest.py
# 切掉；而直接跑 plugin/plugins/<name>/tests 时又只有上层那份生效。两处都留才能
# 让八棵测试树都被守卫覆盖。重复注册无害：先跑的那份还原并判红，后跑的看到无漂移。
#
# sys.path 必须在**任何项目 import 之前**钉好：这些树各有各的 rootdir，而 venv 的
# editable .pth 指向主仓库根。中途插入会让前半段 import 从主仓库解析、后半段从本
# 副本解析（实测炸在 main_logic.agent_event_bus）。
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = str(_Path(__file__).resolve().parents[2])
if _sys.path[:1] != [_REPO_ROOT]:
    _sys.path.insert(0, _REPO_ROOT)

# pytest 按名字在 conftest 命名空间里发现 hook，所以这个导入没有显式调用点
# ——它不是死代码：把它删掉，全进程时钟守卫就整个失效。
from tests.clock_guard import pytest_runtest_call  # noqa: F401,E402

import asyncio.events as _events
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from plugin.server.infrastructure.auth import verify_admin_code
from plugin.server.infrastructure.exceptions import register_exception_handlers
from plugin.server.routes.health import router as health_router
from plugin.server.routes.metrics import router as metrics_router
from plugin.server.routes.runs import router as runs_router



def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-plugin-e2e",
        action="store_true",
        default=False,
        help="run plugin e2e tests (requires browser + running UI server)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-plugin-e2e"):
        return

    skip_marker = pytest.mark.skip(reason="needs --run-plugin-e2e to run")
    for item in items:
        if "plugin_e2e" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _isolate_plugin_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NEKO_STORAGE_SELECTED_ROOT", str(tmp_path / "runtime_data"))
    monkeypatch.delenv("NEKO_STORAGE_ANCHOR_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _isolate_runtime_overrides(monkeypatch: pytest.MonkeyPatch):
    """Redirect plugin runtime override persistence to an in-memory dict for tests.

    Without this, lifecycle tests could write to the real user's
    ``plugin_runtime_overrides.json`` and persist test state on the developer's
    machine.
    """
    from plugin.server.infrastructure import runtime_overrides as _ro

    fake_store: dict[str, bool] = {}

    monkeypatch.setattr(_ro, "_load_from_disk", lambda: dict(fake_store))
    monkeypatch.setattr(
        _ro,
        "_save_to_disk",
        lambda overrides: fake_store.clear() or fake_store.update(overrides),
    )
    _ro.reset_cache_for_testing()
    try:
        yield fake_store
    finally:
        _ro.reset_cache_for_testing()


@pytest.fixture(autouse=True)
def _clear_leaked_running_loop(request: pytest.FixtureRequest):
    """Temporarily clear any running event loop leaked by Playwright's greenlet
    so that sync tests see a clean ``asyncio.get_running_loop() → RuntimeError``
    environment.  Async tests are left untouched."""
    if request.node.get_closest_marker("asyncio") or getattr(
        request.node.obj, "is_coroutine", False
    ) or __import__("asyncio").iscoroutinefunction(getattr(request.node, "obj", None)):
        yield
        return
    saved = _events._get_running_loop()
    _events._set_running_loop(None)
    try:
        yield
    finally:
        _events._set_running_loop(saved)


@pytest.fixture
def plugin_test_app() -> FastAPI:
    app = FastAPI(title="plugin-test-app")
    register_exception_handlers(app)
    app.dependency_overrides[verify_admin_code] = lambda: "test-authenticated"
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(runs_router)
    return app


@pytest.fixture
async def plugin_async_client(plugin_test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=plugin_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
