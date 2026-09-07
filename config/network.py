# -*- coding: utf-8 -*-
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

"""Runtime ports, environment overrides, instance identity, and local origins."""

import json
import os
import sys
import uuid

from .application import logger

# 从 Electron userData 目录读取端口覆盖配置（由前端端口设置窗口写入）
def _read_port_overrides() -> dict:
    try:
        # 用 sys.platform 而不是 platform.system()：后者在 Windows 上会走
        # platform.uname() -> win32_ver() -> _syscmd_ver()，**spawn 一个
        # `cmd /c ver` 子进程**。实测首调 54 ms，杀软介入时观测到 224 ms。
        # 这个模块被 config 包顶层 import，坐在 launcher 启动链的最前面。
        # sys.platform 是解释器常量，零成本，三分支判据完全等价。
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA") or os.path.join(
                os.path.expanduser("~"), "AppData", "Roaming"
            )
            base = os.path.join(appdata, "N.E.K.O")
        elif sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "N.E.K.O")
        else:
            base = os.path.join(
                os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                "N.E.K.O",
            )
        port_file = os.path.join(base, "port_config.json")
        if os.path.exists(port_file):
            with open(port_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.debug("Failed to read port_config.json: %s", e, exc_info=True)
    return {}


_PORT_FILE_OVERRIDES = _read_port_overrides()


# 运行时端口覆盖支持：
# - 首选键：NEKO_<PORT_NAME>
# - 兼容键：<PORT_NAME>
# - 回退：Electron 前端写入的 port_config.json
def _read_port_env(port_name: str, default: int) -> int:
    for key in (f"NEKO_{port_name}", port_name):
        raw = os.getenv(key)
        if not raw:
            continue
        try:
            value = int(raw)
            if 1 <= value <= 65535:
                return value
        except Exception:
            continue
    # 回退：从 Electron 前端写入的 port_config.json 读取
    override = _PORT_FILE_OVERRIDES.get(port_name)
    if override is not None:
        try:
            value = int(override)
            if 1 <= value <= 65535:
                return value
        except (TypeError, ValueError) as e:
            logger.warning(
                "Invalid port_config.json override for %s=%r: %s",
                port_name, override, e,
            )
    return default


def _read_list_env(var_name: str) -> tuple[str, ...]:
    for key in (f"NEKO_{var_name}", var_name):
        raw = os.getenv(key)
        if raw is None:
            continue

        values: list[str] = []
        for item in raw.split(","):
            value = item.strip().rstrip("/")
            if value:
                values.append(value)
        return tuple(dict.fromkeys(values))

    return ()


def _read_str_env(
    var_name: str, default: str, *, allowed: tuple[str, ...] | None = None,
) -> str:
    """Env override for string-typed config values. Key precedence matches the port
    settings: ``NEKO_<NAME>`` wins, bare ``<NAME>`` is kept for compatibility.
    When ``allowed`` is non-empty, out-of-range values are ignored with a warning
    (falling back to default) so a single typo cannot take the whole feature down.
    An empty string counts as unset."""
    for key in (f"NEKO_{var_name}", var_name):
        raw = os.getenv(key)
        if raw is None:
            continue
        val = raw.strip()
        if not val:
            continue
        if allowed is not None and val not in allowed:
            logger.warning(
                "Ignoring %s=%r (not in %s); using default %r",
                key, val, allowed, default,
            )
            continue
        return val
    return default


def _read_bool_env(var_name: str, default: bool) -> bool:
    """Env override for boolean config values. 1/true/yes/on → True; 0/false/no/off → False;
    anything else / unset → default. Key precedence as above."""
    for key in (f"NEKO_{var_name}", var_name):
        raw = os.getenv(key)
        if raw is None:
            continue
        val = raw.strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
        if val in ("0", "false", "no", "off"):
            return False
        if val:
            # 非空但不可识别（如 typo "ture"）：警告并回退，别静默吞掉让用户
            # 摸不着头脑"为什么开关没生效"。与 _read_str_env 的 allowed 行为一致。
            logger.warning(
                "Ignoring %s=%r (not a boolean); using default %s",
                key, raw, default,
            )
    return default


def _build_local_allowed_origins(port: int, *, extra_origins: tuple[str, ...] = ()) -> tuple[str, ...]:
    origins = [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    ]
    origins.extend(extra_origins)
    return tuple(dict.fromkeys(origins))

# 服务器端口配置
MAIN_SERVER_PORT = _read_port_env("MAIN_SERVER_PORT", 48911)
MEMORY_SERVER_PORT = _read_port_env("MEMORY_SERVER_PORT", 48912)
MONITOR_SERVER_PORT = _read_port_env("MONITOR_SERVER_PORT", 48913)
COMMENTER_SERVER_PORT = _read_port_env("COMMENTER_SERVER_PORT", 48914)
TOOL_SERVER_PORT = _read_port_env("TOOL_SERVER_PORT", 48915)
USER_PLUGIN_SERVER_PORT = _read_port_env("USER_PLUGIN_SERVER_PORT", 48916)
AGENT_MQ_PORT = _read_port_env("AGENT_MQ_PORT", 48917)
MAIN_AGENT_EVENT_PORT = _read_port_env("MAIN_AGENT_EVENT_PORT", 48918)
USER_PLUGIN_BASE = f"http://127.0.0.1:{USER_PLUGIN_SERVER_PORT}"


def resolve_user_plugin_base() -> str:
    """The plugin server's ACTUAL loopback origin, right now.

    ``USER_PLUGIN_BASE`` is the configured origin. It is not always the live
    one: when the preferred port is busy, the LAUNCHER picks a free one and
    publishes it in ``NEKO_USER_PLUGIN_SERVER_PORT`` before spawning anything,
    so every child inherits the same answer.

    The launcher is named deliberately. An earlier version of this docstring
    said "the plugin server binds elsewhere and publishes the real port", which
    reads as though the server arbitrates its own port and announces it
    afterwards. It does not, and the difference matters to anyone auditing this
    path: a server that re-bound on its own could only publish into its OWN
    environment, which no sibling process would ever see. Port arbitration
    happens exactly once, in ``launcher_core.runtime.apply_port_strategy``,
    ahead of every consumer; the plugin HTTP app is hosted in-process by
    agent_server (``_start_embedded_user_plugin_server``) and merely binds the
    port it is given, failing loudly if it cannot.

    This lives in config, at the bottom of the layering, because four separate
    layers need the same answer -- the process that MINTS media URLs, the main
    server, the router that proxies those URLs to the browser, and the agent
    router. They had drifted into separate copies, one of which silently
    ignored the environment variable, so a proxy resolved to the port that had
    not minted the id and images went missing exactly in the port-busy case the
    variable exists to handle (Codex).
    """
    raw_port = os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", "").strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            port = 0
        if 0 < port <= 65535:
            return f"http://127.0.0.1:{port}"
    return USER_PLUGIN_BASE.rstrip("/")

# 实例 ID：同一次启动的所有服务共享。
# launcher 会在拉起子进程前写入 NEKO_INSTANCE_ID 环境变量。
# 若源码直跑绕过 launcher，则每次导入使用随机回退值，确保 /health
# 始终返回有效 id。
INSTANCE_ID = os.getenv("NEKO_INSTANCE_ID") or uuid.uuid4().hex
AUTOSTART_CSRF_TOKEN = os.getenv("NEKO_AUTOSTART_CSRF_TOKEN") or INSTANCE_ID
AUTOSTART_ALLOWED_ORIGINS = _build_local_allowed_origins(
    MAIN_SERVER_PORT,
    extra_origins=_read_list_env("AUTOSTART_ALLOWED_ORIGINS"),
)
