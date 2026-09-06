"""
Proactive Controller (主动搭话控制器)

系统级插件。集中管理主动搭话（proactive chat）的"模式"和"频率"——
对外暴露一组稳定的入口，其他插件不需要直接知道 conversation-settings
里那一堆 ``proactive*Enabled`` / ``proactive*Interval`` 字段，只需要
``ctx.plugins.call_entry("proactive_controller:set_mode", {...})`` 之类的
调用即可。

API 形态：

* ``set_mode(mode)``         — 套用预设：``off`` / ``normal`` / ``focus`` / ``frequent``
* ``set_settings(settings)`` — 细粒度更新若干字段（白名单内）
* ``get_state()``            — 读取当前模式与字段
* ``command(action, ...)``   — 单一聚合入口，方便"发消息式"调用

默认行为：首次启动且 ``user_preferences.json`` 中没有 ``proactiveChatEnabled``
字段时，自动套用 ``off`` 预设（默认关闭所有主动搭话）；已有用户偏好则原样保留。
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from plugin.sdk.plugin import (
    NekoPluginBase,
    neko_plugin,
    plugin_entry,
    lifecycle,
    Ok,
    Err,
    SdkError,
)


_VALID_MODES = ("off", "normal", "focus", "frequent")

# 用户绝对控制权字段：服务端 ``main_routers/proactive_router._USER_OWNED_FIELDS``
# 的 client-side 镜像。``proactiveVisionEnabled`` 是前端"隐私模式"开关，
# 必须由用户自己在 UI 决定，插件不能越权写入。
_USER_OWNED_FIELDS = frozenset({
    "proactiveVisionEnabled",
})

_PROACTIVE_BOOL_FIELDS = frozenset({
    "proactiveChatEnabled",
    "proactiveVisionChatEnabled",
    "proactiveNewsChatEnabled",
    "proactiveCommunityChatEnabled",
    "proactiveVideoChatEnabled",
    "proactivePersonalChatEnabled",
    "proactiveMusicEnabled",
    "proactiveMemeEnabled",
    "proactiveMiniGameInviteEnabled",
})
_PROACTIVE_INT_FIELDS = frozenset({
    "proactiveChatInterval",
    "proactiveVisionInterval",
})
# 写路径白名单：不含 ``_USER_OWNED_FIELDS``，让 set_settings 提前拒绝。
_PROACTIVE_FIELDS = _PROACTIVE_BOOL_FIELDS | _PROACTIVE_INT_FIELDS


@neko_plugin
class ProactiveControllerPlugin(NekoPluginBase):

    def __init__(self, ctx):
        super().__init__(ctx)
        self.logger = self.enable_file_logging(log_level="INFO")
        self._api_base: str = "http://127.0.0.1"
        self._api_port: int = 48911
        self._api_timeout: float = 5.0

    # ── lifecycle ────────────────────────────────────────────────────

    @lifecycle(id="startup")
    async def startup(self, **_):
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        section = cfg.get("proactive_controller") if isinstance(cfg.get("proactive_controller"), dict) else {}

        # 主程序端口：优先读取主程序 config，回退到 toml 配置/默认值。
        try:
            from config import MAIN_SERVER_PORT
            self._api_port = int(MAIN_SERVER_PORT)
        except Exception as exc:
            self.logger.warning("Falling back to default MAIN_SERVER_PORT (48911): {}", exc)

        self._api_base = str(section.get("api_base", self._api_base)).rstrip("/")
        raw_timeout = section.get("api_timeout_seconds", self._api_timeout)
        try:
            self._api_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            self.logger.warning(
                "invalid api_timeout_seconds={!r} in plugin.toml; falling back to {}s",
                raw_timeout, self._api_timeout,
            )

        default_mode = str(section.get("default_mode_on_first_run", "off")).strip()
        if default_mode not in _VALID_MODES:
            default_mode = "off"

        # 首次运行判定：以 user_preferences.json 中是否已有 proactiveChatEnabled
        # 为准——存在即视为用户已经配置过，原样保留；缺失才套用 default。
        # 这样不需要额外的"已初始化"持久化标志，且重装/迁移时行为正确。
        current = await self._fetch_settings()
        if "proactiveChatEnabled" not in current:
            result = await self._apply_mode(default_mode)
            if not result.get("success"):
                self.logger.warning("first-run default-{} failed: {}", default_mode, result)
            else:
                self.logger.info("first-run default mode applied: {}", default_mode)

        return Ok({"status": "running", "api": f"{self._api_base}:{self._api_port}"})

    @lifecycle(id="shutdown")
    def shutdown(self, **_):
        return Ok({"status": "shutdown"})

    # ── entries ─────────────────────────────────────────────────────

    @plugin_entry(
        id="set_mode",
        name="切换主动搭话模式",
        description=(
            "套用一组主动搭话预设。可选模式：'off'（全关，默认首次启动值）、"
            "'normal'（推荐配置，所有源开启，间隔 15s/10s）、'focus'（低打扰，"
            "仅留搭话和个人动态，间隔 60s）、'frequent'（高频，全开，间隔 5s）。"
            " 注意：预设不会改变 proactiveVisionEnabled（隐私模式）—— 那是用户绝对控制权。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(_VALID_MODES),
                    "description": "目标模式名称",
                },
            },
            "required": ["mode"],
        },
        llm_result_fields=["mode"],
    )
    async def set_mode(self, mode: str, **_):
        mode = (mode or "").strip()
        if mode not in _VALID_MODES:
            return Err(SdkError(f"未知模式: {mode!r}；可选: {list(_VALID_MODES)}"))
        result = await self._apply_mode(mode)
        if not result.get("success"):
            return Err(SdkError(str(result.get("error", "set_mode failed"))))
        return Ok({"mode": result.get("mode", mode), "applied": result.get("applied", {})})

    @plugin_entry(
        id="set_settings",
        name="细粒度调整主动搭话",
        description=(
            "对主动搭话字段做部分更新。仅接受白名单内字段，未识别字段会被忽略。"
            "字段：proactive*Enabled（布尔），proactiveChatInterval/proactiveVisionInterval（秒，1~3600）。"
            " 注意：proactiveVisionEnabled（隐私模式）是用户绝对控制权，插件不能调整；试图传入会被拒绝。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "settings": {
                    "type": "object",
                    "description": "要更新的字段映射；键名必须命中白名单（不含 proactiveVisionEnabled 隐私模式）。",
                },
            },
            "required": ["settings"],
        },
    )
    async def set_settings(self, settings: Mapping[str, Any] | None = None, **_):
        if not isinstance(settings, Mapping) or not settings:
            return Err(SdkError("settings 必须为非空对象"))

        rejected_user_owned = sorted(set(settings.keys()) & _USER_OWNED_FIELDS)
        payload = {k: v for k, v in settings.items() if k in _PROACTIVE_FIELDS}
        if not payload:
            msg = "settings 中没有可识别的主动搭话字段"
            if rejected_user_owned:
                msg += f"（拒绝用户专有字段: {rejected_user_owned}，请引导用户在 UI 自行设置）"
            return Err(SdkError(msg))

        result = await self._post_json("/api/proactive/settings", payload)
        if not result.get("success"):
            return Err(SdkError(str(result.get("error", "set_settings failed"))))

        out: dict[str, Any] = {"applied": result.get("applied", {})}
        if rejected_user_owned:
            out["rejected_user_owned"] = rejected_user_owned
        # 服务端可能也独立报告类型/范围被丢弃的字段，原样透传。
        if "rejected" in result:
            out["rejected"] = result["rejected"]
        if "rejected_user_owned" in result:
            out["rejected_user_owned"] = result["rejected_user_owned"]
        return Ok(out)

    @plugin_entry(
        id="get_state",
        name="读取主动搭话状态",
        description="返回当前模式（off/normal/focus/frequent/custom）以及主动搭话字段当前值。",
        llm_result_fields=["mode"],
    )
    async def get_state(self, **_):
        result = await self._get_json("/api/proactive/mode")
        if not result.get("success"):
            return Err(SdkError(str(result.get("error", "get_state failed"))))
        return Ok({
            "mode": result.get("mode", "custom"),
            "available_modes": result.get("available_modes", list(_VALID_MODES)),
            "settings": result.get("settings", {}),
        })

    @plugin_entry(
        id="command",
        name="主动搭话统一指令通道",
        description=(
            "聚合入口：根据 action 路由到 set_mode / set_settings / get_state，"
            "方便其他插件通过单一调用控制主动搭话。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set_mode", "set_settings", "get_state"],
                    "description": "要执行的操作",
                },
                "mode": {"type": "string", "description": "action=set_mode 时的目标模式"},
                "settings": {
                    "type": "object",
                    "description": "action=set_settings 时的字段映射",
                },
            },
            "required": ["action"],
        },
    )
    async def command(self, action: str, **kwargs):
        action = (action or "").strip()
        if action == "set_mode":
            return await self.set_mode(mode=kwargs.get("mode", ""))
        if action == "set_settings":
            return await self.set_settings(settings=kwargs.get("settings"))
        if action == "get_state":
            return await self.get_state()
        return Err(SdkError(f"未知 action: {action!r}"))

    # ── internals ───────────────────────────────────────────────────

    async def _apply_mode(self, mode: str) -> dict[str, Any]:
        return await self._post_json("/api/proactive/mode", {"mode": mode})

    async def _fetch_settings(self) -> dict[str, Any]:
        result = await self._get_json("/api/proactive/settings")
        settings = result.get("settings") if isinstance(result, dict) else None
        return settings if isinstance(settings, dict) else {}

    def _url(self, path: str) -> str:
        return f"{self._api_base}:{self._api_port}{path}"

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._api_timeout, trust_env=False) as client:
                resp = await client.get(self._url(path))
                if resp.status_code >= 400:
                    return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
                return resp.json()
        except Exception as exc:
            self.logger.warning("GET {} failed: {}", path, exc)
            return {"success": False, "error": str(exc)}

    async def _post_json(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._api_timeout, trust_env=False) as client:
                resp = await client.post(self._url(path), json=dict(body))
                if resp.status_code >= 400:
                    return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
                return resp.json()
        except Exception as exc:
            self.logger.warning("POST {} failed: {}", path, exc)
            return {"success": False, "error": str(exc)}
