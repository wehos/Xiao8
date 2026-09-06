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

"""
Agent Router

Handles agent-related endpoints including:
- Agent flags
- Health checks
- Task status
- Admin control

URL convention: routes declared WITHOUT trailing slash (no ``@router.get('/')``).
See ``main_routers/characters_router.py`` docstring or
``.agent/rules/neko-guide.md`` (§"API URL 末尾不带斜杠") for the rationale;
enforced by ``scripts/check_api_trailing_slash.py``.
"""

import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse

from utils.logger_config import get_module_logger
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
import httpx
from .pages_router import _static_assets_ctx
from .shared_state import get_session_manager, get_config_manager, get_templates
from config import TOOL_SERVER_PORT, USER_PLUGIN_BASE
from main_logic.agent_event_bus import publish_session_event
from main_logic.activity.system_signals import is_remote_backend_deployment

router = APIRouter(prefix="/api/agent", tags=["agent"])
logger = get_module_logger(__name__, "Main")
TOOL_SERVER_BASE = f"http://127.0.0.1:{TOOL_SERVER_PORT}"
_HTTP_CLIENT: httpx.AsyncClient | None = None
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_USER_PLUGIN_DEFAULT_BASE = "http://127.0.0.1:48916"
_USER_PLUGIN_BASE_CACHE: tuple[str, float] = ("", 0.0)
_OPENCLAW_GUIDE_PATH = _PROJECT_ROOT / "docs" / "zh-CN" / "guide" / "openclaw_guide.md"
_OPENCLAW_GUIDE_DIR = _OPENCLAW_GUIDE_PATH.parent
_OPENCLAW_GUIDE_ASSETS_DIR = _OPENCLAW_GUIDE_DIR / "assets"
_OPENCLAW_GUIDE_LANG_FILES = {
    "zh-CN": _OPENCLAW_GUIDE_DIR / "openclaw_guide.md",
    "zh-TW": _OPENCLAW_GUIDE_DIR / "openclaw_guide.zh-TW.md",
    "en": _OPENCLAW_GUIDE_DIR / "openclaw_guide.en.md",
    "ja": _OPENCLAW_GUIDE_DIR / "openclaw_guide.ja.md",
    "ko": _OPENCLAW_GUIDE_DIR / "openclaw_guide.ko.md",
    "ru": _OPENCLAW_GUIDE_DIR / "openclaw_guide.ru.md",
}

_AGENT_OFF_FLAGS = {
    "agent_enabled": False,
    "computer_use_enabled": False,
    "browser_use_enabled": False,
    "user_plugin_enabled": False,
    "openclaw_enabled": False,
    "openclaw_ready": False,
}


def _remote_backend_block() -> JSONResponse | None:
    """Reject agent mutation when backend is deployed away from the user.

    In remote mode (``NEKO_ACTIVITY_TRACKER_REMOTE=1``) the "computer"
    that computer_use / browser_use / openclaw would control is the
    *server's*, not the user's — there's no useful action to take, and
    silently forwarding the command to a localhost tool_server on the
    server side is actively dangerous (anyone who can reach the public
    backend HTTP can drive the agent on the server). Returning 501
    matches the same-env block on ``/api/screenshot`` in
    ``main_routers/system_router.py`` so the frontend can surface a
    uniform "agent unavailable on remote backend" state.

    Threat model context: a deeper CSRF + Origin guard (defending
    against DNS-rebinding-style attacks on a *local* backend) is
    deferred to a follow-up — it needs the ~15 frontend agent fetch
    sites to start sending ``X-CSRF-Token`` first, which doesn't fit
    PR B's scope. See issue #1023 for the audit + scope decision.
    """
    if is_remote_backend_deployment():
        return JSONResponse(
            {
                "success": False,
                "error": "agent disabled in remote backend deployment "
                         "(NEKO_ACTIVITY_TRACKER_REMOTE)",
            },
            status_code=501,
        )
    return None


async def force_disable_agent_for_character_switch(current_lanlan: str, previous_lanlan: str | None = None) -> bool:
    """Force-disable the agent (cat paw) after a character switch so stale global tool-service state cannot leak into the new character."""
    names = {
        str(name or "").strip()
        for name in (current_lanlan, previous_lanlan)
        if str(name or "").strip()
    }
    session_manager = get_session_manager()
    for name in names:
        mgr = session_manager.get(name)
        if mgr:
            mgr.update_agent_flags(dict(_AGENT_OFF_FLAGS))

    if not current_lanlan:
        return False

    try:
        client = _get_http_client()
        payload = {
            "request_id": f"character-switch-agent-off-{uuid.uuid4().hex[:8]}",
            "command": "set_agent_enabled",
            "enabled": False,
            "lanlan_name": current_lanlan,
        }
        # 工具服务会先落关闭状态再收尾任务，短超时避免角色切换被长任务清理拖住。
        response = await client.post(f"{TOOL_SERVER_BASE}/agent/command", json=payload, timeout=1.2)
        if response.is_success:
            return True
        logger.warning(
            "角色切换关闭猫爪失败: lanlan=%s status=%s",
            current_lanlan,
            response.status_code,
        )
    except Exception as exc:
        logger.warning("角色切换关闭猫爪异常: lanlan=%s err=%s", current_lanlan, exc)
    return False


def _is_loopback_origin(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return False
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


async def _resolve_user_plugin_base() -> str:
    global _USER_PLUGIN_BASE_CACHE
    cached_base, cached_at = _USER_PLUGIN_BASE_CACHE
    now = time.monotonic()
    if cached_base and now - cached_at < 5.0:
        return cached_base

    candidates: list[str] = []
    for value in (USER_PLUGIN_BASE, _USER_PLUGIN_DEFAULT_BASE):
        normalized = str(value or "").rstrip("/")
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    client = _get_http_client()
    for base in candidates:
        try:
            response = await client.get(f"{base}/available", timeout=0.45)
            if response.is_success:
                _USER_PLUGIN_BASE_CACHE = (base, now)
                return base
        except Exception:
            continue

    fallback = str(USER_PLUGIN_BASE or _USER_PLUGIN_DEFAULT_BASE).rstrip("/")
    _USER_PLUGIN_BASE_CACHE = (fallback, now)
    return fallback


def _normalize_openclaw_guide_lang(lang: str | None) -> str:
    text = str(lang or "").strip()
    if not text:
        return "zh-CN"
    lowered = text.lower()
    if lowered.startswith("zh-tw") or lowered.startswith("zh-hk") or lowered.startswith("zh-hant"):
        return "zh-TW"
    if lowered.startswith("zh"):
        return "zh-CN"
    if lowered.startswith("en"):
        return "en"
    if lowered.startswith("ja"):
        return "ja"
    if lowered.startswith("ko"):
        return "ko"
    if lowered.startswith("ru"):
        return "ru"
    return "zh-CN"


def _load_openclaw_guide_markdown(lang: str | None = None) -> str:
    resolved_lang = _normalize_openclaw_guide_lang(lang)
    candidate = _OPENCLAW_GUIDE_LANG_FILES.get(resolved_lang, _OPENCLAW_GUIDE_PATH)
    try:
        return candidate.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(
            "Failed to load OpenClaw guide markdown for %s from %s: %s",
            resolved_lang,
            candidate,
            exc,
        )
        return (
            "# OpenClaw 接入教程\n\n"
            "> 本项目中的 OpenClaw 指代 QwenPaw。\n\n"
            "教程内容暂时无法加载，请检查文档文件是否存在：\n\n"
            f"`{candidate.name}`"
        )


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(2.5, connect=0.5),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            proxy=None,
            trust_env=False,
        )
    return _HTTP_CLIENT


@router.on_event("shutdown")
async def _close_http_client():
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        # 关连接失败不许拖垮整个 shutdown：这个 handler 挂在合并 lifespan 链上，
        # 抛出去会中断后面所有 router 的收尾。最典型的触发是 client 建在另一个
        # 已经关掉的事件循环上（aclose 走 call_soon 撞上 closed loop）。无论成败
        # 都把引用清掉，下次 startup 在当前循环上重建。
        try:
            await _HTTP_CLIENT.aclose()
        except Exception as exc:
            logger.warning(f"关闭 agent_router HTTP client 失败，继续收尾: {exc}")
        finally:
            _HTTP_CLIENT = None


@router.post('/flags')
async def update_agent_flags(request: Request):
    """Agent flag updates from the frontend, cascaded to each session manager."""
    blocked = _remote_backend_block()
    if blocked is not None:
        return blocked
    try:
        data = await request.json()
        _config_manager = get_config_manager()
        session_manager = get_session_manager()
        _, her_name_current, _, _, _, _, _, _, _ = await _config_manager.aget_character_data()
        lanlan = data.get('lanlan_name') or her_name_current
        flags = data.get('flags') or {}
        mgr = session_manager.get(lanlan)
        if not mgr:
            return JSONResponse({"success": False, "error": "lanlan not found"}, status_code=404)
        # Update core flags first
        mgr.update_agent_flags(flags)
        # Forward to tool server for Computer-Use/Browser-Use/Plugin flags
        try:
            forward_payload = {}
            if lanlan:
                forward_payload['lanlan_name'] = lanlan
            if 'computer_use_enabled' in flags:
                forward_payload['computer_use_enabled'] = bool(flags['computer_use_enabled'])
            if 'browser_use_enabled' in flags:
                forward_payload['browser_use_enabled'] = bool(flags['browser_use_enabled'])
            # Forward user_plugin_enabled as well so agent_server receives UI toggles
            if 'user_plugin_enabled' in flags:
                forward_payload['user_plugin_enabled'] = bool(flags['user_plugin_enabled'])
            if 'openclaw_enabled' in flags:
                forward_payload['openclaw_enabled'] = bool(flags['openclaw_enabled'])
            if forward_payload:
                client = _get_http_client()
                r = await client.post(f"{TOOL_SERVER_BASE}/agent/flags", json=forward_payload, timeout=0.7)
                if not r.is_success:
                    raise Exception(f"tool_server responded {r.status_code}")
        except Exception as e:
            # On failure, reset flags in core to safe state (include user_plugin flag)
            mgr.update_agent_flags({
                'agent_enabled': False,
                'computer_use_enabled': False,
                'browser_use_enabled': False,
                'user_plugin_enabled': False,
                'openclaw_enabled': False,
            })
            return JSONResponse({"success": False, "error": f"tool_server forward failed: {e}"}, status_code=502)
        return {"success": True, "is_free_version": _config_manager.is_agent_free()}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)



@router.get('/flags')
async def get_agent_flags():
    """Get the current agent flags state (for frontend sync)."""
    try:
        client = _get_http_client()
        r = await client.get(f"{TOOL_SERVER_BASE}/agent/flags", timeout=0.7)
        if not r.is_success:
            return JSONResponse({"success": False, "error": "tool_server down"}, status_code=502)
        return r.json()
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=502)


@router.get('/state')
async def get_agent_state():
    """Get the authoritative agent state snapshot (revision + flags + capabilities)."""
    try:
        client = _get_http_client()
        r = await client.get(f"{TOOL_SERVER_BASE}/agent/state", timeout=1.2)
        if not r.is_success:
            return JSONResponse({"success": False, "error": "tool_server down"}, status_code=502)
        return r.json()
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=502)


@router.post('/command')
async def post_agent_command(request: Request):
    """Unified command entry point; the frontend only sends commands and never toggles the individual switches directly."""
    blocked = _remote_backend_block()
    if blocked is not None:
        return blocked
    t0 = time.perf_counter()
    try:
        data = await request.json()
        request_id = data.get("request_id")
        command = data.get("command")
        lanlan = data.get("lanlan_name")
        session_manager = get_session_manager()
        cfg = get_config_manager()
        if not lanlan:
            try:
                _, her_name_current, _, _, _, _, _, _, _ = cfg.get_character_data()
                lanlan = her_name_current
                data["lanlan_name"] = lanlan
            except Exception:
                lanlan = None
        mgr = session_manager.get(lanlan) if lanlan else None
        old_flags = dict(getattr(mgr, "agent_flags", {}) or {}) if mgr else None

        # Keep main_server core flags in sync with command path.
        if mgr and command == "set_agent_enabled":
            enabled = bool(data.get("enabled"))
            if enabled:
                mgr.update_agent_flags({"agent_enabled": True})
            else:
                mgr.update_agent_flags({
                    "agent_enabled": False,
                    "computer_use_enabled": False,
                    "browser_use_enabled": False,
                    "user_plugin_enabled": False,
                    "openclaw_enabled": False,
                    "openclaw_ready": False,
                })
        elif mgr and command == "set_flag":
            key = data.get("key")
            if key in {"computer_use_enabled", "browser_use_enabled", "user_plugin_enabled", "openclaw_enabled"}:
                flag_update = {key: bool(data.get("value"))}
                if key == "openclaw_enabled":
                    flag_update["openclaw_ready"] = False
                mgr.update_agent_flags(flag_update)

        t_proxy = time.perf_counter()
        client = _get_http_client()
        r = await client.post(f"{TOOL_SERVER_BASE}/agent/command", json=data, timeout=8.0)
        proxy_ms = round((time.perf_counter() - t_proxy) * 1000, 2)
        if not r.is_success:
            # Rollback local state on upstream failure.
            if mgr and old_flags is not None:
                mgr.update_agent_flags(old_flags)
            logger.warning("[MainAgentTiming] request_id=%s upstream_status=%s proxy_ms=%s", request_id, r.status_code, proxy_ms)
            return JSONResponse({"success": False, "error": f"tool_server responded {r.status_code}"}, status_code=502)
        payload = r.json()
        total_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.info("[MainAgentTiming] request_id=%s proxy_ms=%s total_ms=%s", request_id or payload.get("request_id"), proxy_ms, total_ms)
        if isinstance(payload, dict):
            timing = payload.get("timing") or {}
            timing["main_proxy_ms"] = proxy_ms
            timing["main_total_ms"] = total_ms
            payload["timing"] = timing
            if command == "set_agent_enabled" and bool(data.get("enabled")):
                payload["is_free_version"] = cfg.is_agent_free()
        return payload
    except Exception as e:
        total_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.warning("[MainAgentTiming] proxy_exception total_ms=%s error=%s", total_ms, e)
        return JSONResponse({"success": False, "error": str(e)}, status_code=502)


@router.post('/internal/analyze_request')
async def post_internal_analyze_request(request: Request):
    """Internal bridge: accept analyze_request from child process and publish via main EventBus."""
    try:
        data = await request.json()
        event = {
            "event_type": "analyze_request",
            "trigger": data.get("trigger") or "turn_end",
            "lanlan_name": data.get("lanlan_name"),
            "messages": data.get("messages") or [],
        }
        sent = await publish_session_event(event)
        return {"success": bool(sent)}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)




@router.get('/health')
async def agent_health():
    """Check tool_server health via main_server proxy."""
    try:
        client = _get_http_client()
        r = await client.get(f"{TOOL_SERVER_BASE}/health", timeout=0.7)
        if not r.is_success:
            return JSONResponse({"status": "down"}, status_code=502)
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        return {"status": "ok", **({"tool": data} if isinstance(data, dict) else {})}
    except Exception:
        return JSONResponse({"status": "down"}, status_code=502)



@router.get('/computer_use/availability')
async def proxy_cu_availability():
    try:
        client = _get_http_client()
        r = await client.get(f"{TOOL_SERVER_BASE}/computer_use/availability", timeout=1.5)
        if not r.is_success:
            return JSONResponse({"ready": False, "reasons": [f"tool_server responded {r.status_code}"]}, status_code=502)
        return r.json()
    except Exception as e:
        return JSONResponse({"ready": False, "reasons": [f"proxy error: {e}"]}, status_code=502)



@router.get('/mcp/availability')
async def proxy_mcp_availability():
    return {"ready": False, "capabilities_count": 0, "reasons": ["MCP 已移除"]}


@router.get('/user_plugin/dashboard')
async def redirect_plugin_dashboard(request: Request):
    # Docker 部署（位于 Nginx 反向代理后方）：使用相对路径 /ui，
    # 由 Nginx 代理到插件服务（48916），避免返回容器内部 127.0.0.1
    behind_proxy = os.environ.get("NEKO_BEHIND_PROXY", "").strip().lower() in ("1", "true", "yes")
    if behind_proxy:
        target_url = "/ui"
    else:
        user_plugin_base = await _resolve_user_plugin_base()
        target_url = f"{user_plugin_base}/ui"
    if request.query_params.get("page") == "model-api":
        target_url += "/model-api"
    query_params: dict[str, str] = {}
    if "v" in request.query_params:
        v = request.query_params["v"].strip()
        if v:
            query_params["v"] = v
    if "yui_opener_origin" in request.query_params:
        opener_origin = request.query_params["yui_opener_origin"].strip()
        if opener_origin and _is_loopback_origin(opener_origin):
            query_params["yui_opener_origin"] = opener_origin
    if query_params:
        target_url = f"{target_url}?{urlencode(query_params)}"
    return RedirectResponse(target_url)


@router.get('/openclaw/guide', response_class=HTMLResponse)
async def openclaw_guide_page(request: Request):
    templates = get_templates()
    return templates.TemplateResponse("templates/openclaw_guide.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get('/openclaw/guide/content')
async def openclaw_guide_content(lang: str | None = None):
    resolved_lang = _normalize_openclaw_guide_lang(lang)
    return {
        "success": True,
        "lang": resolved_lang,
        "markdown": _load_openclaw_guide_markdown(resolved_lang),
    }


@router.get('/openclaw/guide/assets/{asset_path:path}')
async def openclaw_guide_asset(asset_path: str):
    if not asset_path:
        raise HTTPException(status_code=404, detail="Asset not found")

    candidate = (_OPENCLAW_GUIDE_ASSETS_DIR / asset_path).resolve()
    try:
        candidate.relative_to(_OPENCLAW_GUIDE_ASSETS_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Asset not found") from exc

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")

    return FileResponse(str(candidate))


@router.get('/user_plugin/availability')
async def proxy_up_availability():
    try:
        client = _get_http_client()
        user_plugin_base = await _resolve_user_plugin_base()
        r = await client.get(f"{user_plugin_base}/available", timeout=1.5)
        if r.is_success:
            return JSONResponse({"ready": True, "reasons": [f"user_plugin server reachable: {user_plugin_base}"]}, status_code=200)
        else:
            return JSONResponse({"ready": False, "reasons": [f"user_plugin server responded {r.status_code}"]}, status_code=502)
    except Exception as e:
        return JSONResponse({"ready": False, "reasons": [f"proxy error: {e}"]}, status_code=502)


@router.get('/openclaw/availability')
async def openclaw_availability():
    """Check whether the OpenClaw agent capability is available."""
    try:
        client = _get_http_client()
        # OpenClaw availability may perform a downstream health probe and can
        # legitimately take a bit longer than the lightweight local checks.
        r = await client.get(f"{TOOL_SERVER_BASE}/openclaw/availability", timeout=4.0)
        if not r.is_success:
            return JSONResponse({"ready": False, "reasons": [f"tool_server responded {r.status_code}"]}, status_code=502)
        return r.json()
    except Exception as e:
        return JSONResponse({"ready": False, "reasons": [f"proxy error: {e}"]}, status_code=502)


@router.get('/browser_use/availability')
async def proxy_browser_availability():
    try:
        client = _get_http_client()
        r = await client.get(f"{TOOL_SERVER_BASE}/browser_use/availability", timeout=1.5)
        if not r.is_success:
            return JSONResponse({"ready": False, "reasons": [f"tool_server responded {r.status_code}"]}, status_code=502)
        return r.json()
    except Exception as e:
        return JSONResponse({"ready": False, "reasons": [f"proxy error: {e}"]}, status_code=502)



@router.get('/tasks')
async def proxy_tasks():
    """Get all tasks from tool server via main_server proxy."""
    try:
        client = _get_http_client()
        r = await client.get(f"{TOOL_SERVER_BASE}/tasks", timeout=2.5)
        if not r.is_success:
            return JSONResponse({"tasks": [], "error": f"tool_server responded {r.status_code}"}, status_code=502)
        return r.json()
    except Exception as e:
        return JSONResponse({"tasks": [], "error": f"proxy error: {e}"}, status_code=502)



@router.get('/tasks/{task_id}')
async def proxy_task_detail(task_id: str):
    """Get specific task details from tool server via main_server proxy."""
    try:
        client = _get_http_client()
        r = await client.get(f"{TOOL_SERVER_BASE}/tasks/{task_id}", timeout=1.5)
        if not r.is_success:
            return JSONResponse({"error": f"tool_server responded {r.status_code}"}, status_code=502)
        return r.json()
    except Exception as e:
        return JSONResponse({"error": f"proxy error: {e}"}, status_code=502)


@router.post('/tasks/{task_id}/cancel')
async def proxy_task_cancel(task_id: str):
    """Cancel a specific task via tool server proxy."""
    blocked = _remote_backend_block()
    if blocked is not None:
        return blocked
    try:
        client = _get_http_client()
        r = await client.post(f"{TOOL_SERVER_BASE}/tasks/{task_id}/cancel", timeout=5.0)
        if not r.is_success:
            # Pass the tool server's status through (e.g. 404 task-not-found)
            # so the HUD can tell an orphan card from a proxy-layer failure.
            return JSONResponse({"success": False, "error": f"tool_server responded {r.status_code}"}, status_code=r.status_code)
        return r.json()
    except httpx.ReadTimeout as e:
        # Timed out waiting for the response: the tool server received the
        # cancel but responded too slowly. 504 tells the HUD the request did
        # land, so its local terminal fallback is safe to apply.
        return JSONResponse({"success": False, "error": f"proxy timeout: {e}"}, status_code=504)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        # Connect failure or connect/write/pool timeout: the request cannot
        # be proven to have reached the tool server.
        return JSONResponse({"success": False, "error": f"proxy error: {e}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"success": False, "error": f"proxy error: {e}"}, status_code=502)


@router.post('/admin/control')
async def proxy_admin_control(payload: dict = Body(...)):
    """Proxy admin control commands to tool server."""
    blocked = _remote_backend_block()
    if blocked is not None:
        return blocked
    try:
        client = _get_http_client()
        r = await client.post(f"{TOOL_SERVER_BASE}/admin/control", json=payload, timeout=5.0)
        if not r.is_success:
            return JSONResponse({"success": False, "error": f"tool_server responded {r.status_code}"}, status_code=502)

        result = r.json()
        logger.info(f"Admin control result: {result}")
        return result

    except httpx.ReadTimeout as e:
        # Timed out waiting for the response: end_all keeps running to
        # completion on the tool server. 504 tells the HUD the request did
        # land, so its local terminal fallback is safe to apply.
        return JSONResponse({
            "success": False,
            "error": f"admin control timed out after forwarding: {str(e)}"
        }, status_code=504)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        # Connect failure or connect/write/pool timeout: the request cannot
        # be proven to have reached the tool server — nothing was cancelled.
        return JSONResponse({
            "success": False,
            "error": f"Failed to execute admin control: {str(e)}"
        }, status_code=500)
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": f"Failed to execute admin control: {str(e)}"
        }, status_code=500)
