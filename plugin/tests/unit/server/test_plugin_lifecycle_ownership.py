# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Who owns the plugin lifecycle must be stated, not inferred from the environment.

Starting the lifecycle runs a full plugin metadata refresh plus autostart. Inside
agent_server that must not happen during the app's lifespan, because uvicorn
awaits ``lifespan.startup()`` before it creates the listening socket — the cost
would land while the port does not exist yet, and the user sees a refused
connection rather than a slow one.

That was previously decided by a module-level constant read from
``NEKO_PLUGIN_HOSTED_BY_AGENT`` at **import** time, and it was correct only
because agent_server happens to set the variable before anything first imports
this module. Two distant facts with nothing enforcing the order between them: an
earlier import from anywhere freezes the constant to False and silently puts the
scan back on the startup path.

These guards pin the replacement: an explicit argument, a fail-safe default, and
no dependence on the environment.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def _neutralised_lifespan(monkeypatch: pytest.MonkeyPatch):
    """Stub the parts of the lifespan that touch the real machine."""
    from plugin.server.application import install_source
    from plugin.server.routes import market_bridge

    monkeypatch.setattr(
        install_source, "build_install_source_manager",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stubbed out")),
    )
    monkeypatch.setattr(market_bridge, "write_bridge_token_file", lambda *a, **k: None)


async def _run_lifespan(module, app) -> None:
    async with module.plugin_server_lifespan(app):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_value", "label"),
    [(None, "unset"), ("true", "true"), ("false", "false")],
    ids=["env-unset", "env-true", "env-false"],
)
async def test_the_default_app_never_starts_the_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    _neutralised_lifespan: None,
    env_value: str | None,
    label: str,
) -> None:
    """No explicit claim means no lifecycle, whatever the environment says.

    Parametrised over the environment variable precisely because the point of the
    change is that it no longer participates. A single-value test would pass just
    as well against the old import-time constant.

    Mutation: default ``manage_lifecycle`` to True, or read the env at the call.
    """
    from plugin.server import http_app as module

    if env_value is None:
        monkeypatch.delenv("NEKO_PLUGIN_HOSTED_BY_AGENT", raising=False)
    else:
        monkeypatch.setenv("NEKO_PLUGIN_HOSTED_BY_AGENT", env_value)

    started: list[int] = []

    # 必须是 async 的：生产代码 `await lifecycle_startup()`。同步 stub 只会在
    # 那条路**真的被执行**时才炸——第一版就是同步的，而这条用例正因为路径没走
    # 到才绿，等于什么都没证明。
    async def _start() -> None:
        started.append(1)

    async def _stop() -> None:
        started.append(2)

    monkeypatch.setattr(module, "lifecycle_startup", _start)
    monkeypatch.setattr(module, "lifecycle_shutdown", _stop)

    app = module.build_plugin_server_app()
    await _run_lifespan(module, app)

    assert started == [], (
        f"env={label} 时默认 app 仍然起了插件生命周期——全量扫描回到端口 bind 之前"
    )


@pytest.mark.asyncio
async def test_an_app_that_claims_ownership_does_start_the_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    _neutralised_lifespan: None,
) -> None:
    """The fail-safe default must not turn into "never starts at all".

    Without this the whole rule could be satisfied by deleting the call, and the
    standalone server would silently stop loading plugins.

    Mutation: ignore ``manage_lifecycle`` and never start.
    """
    from plugin.server import http_app as module

    monkeypatch.delenv("NEKO_PLUGIN_HOSTED_BY_AGENT", raising=False)

    calls: list[str] = []

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    monkeypatch.setattr(module, "lifecycle_startup", _start)
    monkeypatch.setattr(module, "lifecycle_shutdown", _stop)

    app = module.build_plugin_server_app(manage_lifecycle=True)
    await _run_lifespan(module, app)

    assert calls == ["start", "stop"], f"认领了所有权却没有起/停生命周期：{calls}"


def _build_call_keywords(source: str) -> dict[str, list[ast.keyword]]:
    """Every ``build_plugin_server_app(...)`` call in a file, by keyword name."""
    tree = ast.parse(source)
    found: dict[str, list[ast.keyword]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "build_plugin_server_app":
            continue
        found.setdefault("calls", []).extend(node.keywords)
        found.setdefault("nodes", []).append(node)
    return found


def test_only_the_standalone_entry_point_claims_the_lifecycle() -> None:
    """Testing the predicate is not testing the call sites.

    The flag being correct proves nothing about who passes it. There are exactly
    two constructors of this app, and they must disagree: the standalone entry
    owns the lifecycle because nothing else would start it, and the embedded one
    does not because agent_server drives it from ``user_plugin_enabled``.

    Mutation: pass ``manage_lifecycle=True`` from the embedded call site, or drop
    it from the standalone one.
    """
    standalone = (REPO_ROOT / "plugin" / "user_plugin_server.py").read_text(
        encoding="utf-8"
    )
    embedded = (REPO_ROOT / "app" / "agent_server" / "plugin_host.py").read_text(
        encoding="utf-8"
    )

    standalone_kw = _build_call_keywords(standalone)
    assert standalone_kw.get("nodes"), "独立入口不再构造插件 app 了？"
    claimed = [
        kw for kw in standalone_kw["calls"]
        if kw.arg == "manage_lifecycle" and getattr(kw.value, "value", None) is True
    ]
    assert claimed, (
        "独立入口没有显式认领生命周期——独立跑的时候没有别人会起它，插件会静默不加载"
    )

    embedded_kw = _build_call_keywords(embedded)
    assert embedded_kw.get("nodes"), "内嵌路径不再构造插件 app 了？"
    over_claimed = [
        kw for kw in embedded_kw["calls"]
        if kw.arg == "manage_lifecycle" and getattr(kw.value, "value", None) is True
    ]
    assert not over_claimed, (
        "内嵌路径认领了生命周期——那会把整轮插件扫描拉回 agent_server 的启动路径，"
        "而 uvicorn 是先跑完 lifespan 才 bind 端口的"
    )


def test_the_lifecycle_decision_no_longer_reads_the_environment() -> None:
    """The import-time constant must not creep back onto the decision.

    ``_EMBEDDED_BY_AGENT`` still exists and is still fine for choosing a logger
    namespace — getting *that* wrong only misfiles log lines. What must never
    return is its use around ``lifecycle_startup`` / ``lifecycle_shutdown``,
    because that is the one place where an import-order accident is silent and
    expensive.

    Mutation: guard either lifecycle call with ``_EMBEDDED_BY_AGENT`` again.
    """
    source = (REPO_ROOT / "plugin" / "server" / "http_app.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {
            sub.id for sub in ast.walk(node.test) if isinstance(sub, ast.Name)
        }
        if "_EMBEDDED_BY_AGENT" not in names:
            continue
        called = {
            sub.func.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
        }
        if called & {"lifecycle_startup", "lifecycle_shutdown"}:
            offenders.append(node.lineno)

    assert not offenders, (
        f"生命周期的起/停又被 _EMBEDDED_BY_AGENT 决定了（行 {offenders}）——"
        "那是 import 时刻的环境变量，顺序一变就静默把全量扫描放回启动路径"
    )
