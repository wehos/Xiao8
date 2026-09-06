"""
插件 UI 静态文件代理路由

允许插件注入自定义前端界面，通过 iframe 嵌入到主应用中。

插件目录结构：
    my_plugin/
    ├── __init__.py
    ├── plugin.toml
    └── static/           # 静态文件目录
        ├── index.html    # 入口文件
        ├── main.js
        └── style.css

访问路径：
    GET /plugin/{plugin_id}/ui/          -> static/index.html
    GET /plugin/{plugin_id}/ui/main.js   -> static/main.js
    GET /plugin/{plugin_id}/ui/style.css -> static/style.css
"""
import asyncio
import json
import mimetypes
import os
import re
from collections.abc import Awaitable
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from plugin.core.state import state
from plugin.logging_config import get_logger
from plugin.server.application.plugins.ui_query_service import PluginUiQueryService
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.error_mapping import raise_http_from_domain

router = APIRouter(tags=["plugin-ui"])
logger = get_logger("server.routes.plugin_ui")
plugin_ui_query_service = PluginUiQueryService()

_I18N_LOCALE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,15}$")
# (mtime, payload) keyed by absolute file path. Bundles are typically <100KB
# and only change when the plugin author edits a translation file, so we keep
# the parsed bytes in process memory and revalidate via mtime on each hit.
_I18N_BUNDLE_CACHE: dict[Path, tuple[float, bytes]] = {}

# ---- 插件静态 UI 的 SSE 实时推送通道（强化后端 → 前端通信）----
# plugin_id -> list[asyncio.Queue]；每个 SSE 连接一个队列，POST /ui-api/push 广播。
# 任意插件/后端往 /ui-api/push 推送，前端页面连 /ui-api/events 即时接收实时更新。
_sse_clients: dict[str, list[asyncio.Queue]] = {}
_sse_clients_lock = asyncio.Lock()
# 每个客户端队列的最大缓冲（慢/阻塞客户端不使其无限膨胀）；满时丢弃新消息
_SSE_QUEUE_MAX = 100
# push 请求体大小上限（字节）：1MB，足够 catgirl 带头像弹幕，同时限制本机恶意分块请求占内存
_PUSH_MAX_BODY = 1024 * 1024
# 可信 Origin 主机（本机回环）：push 广播只接受无 Origin（插件后端/非浏览器）或本机页面
_TRUSTED_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# ---- runs bus → SSE 桥：把 run 状态迁移事件推给对应插件的 SSE 客户端 ----
# run 协议本就通过 state.bus_change_hub（bus "runs"）在每次迁移发出事件
# （plugin/runs/manager.py _emit_runs）。这里订阅它并桥接进 _sse_clients，
# 让前端 call() 不必紧轮询 /runs/{id}。只桥接终端状态（succeeded/failed/
# canceled/timeout），前端只关心这些。
_SSE_RUNS_BRIDGE_INSTALLED = False
_SSE_RUNS_BRIDGE_SUB = None
_SSE_RUN_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "timeout"})


def _sse_queue_put_best_effort(queue: asyncio.Queue, frame: str) -> bool:
    """往一个 SSE 客户端队列塞一帧；满了丢弃**最旧**、让新帧入队。

    慢客户端读不过来时，保留的是**最新**的帧（丢队首最旧），符合「实时流」直觉 ——
    而不是丢刚产生的新帧。``asyncio.Queue`` 为 FIFO，``get_nowait()`` 取/删队首。
    返回 True 表示该帧最终已入队（直接入队，或 drop-oldest 后入队）。
    """
    try:
        queue.put_nowait(frame)
        return True
    except asyncio.QueueFull:
        try:
            queue.get_nowait()      # 丢弃最旧（队首）一条
            queue.put_nowait(frame)  # 新帧入队
            return True
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            return False  # 极端竞态：别崩，尽力而为


def _bridge_runs_event(op: str, payload: object) -> None:
    """runs bus 事件 → 对应 plugin_id 的 SSE 队列（仅终端状态）。

    run 在事件循环内执行（asyncio.create_task），emit 也发生在事件循环线程，
    因此这里直接 put_nowait 是线程安全的。
    """
    try:
        if not isinstance(payload, dict):
            return
        plugin_id = payload.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id:
            return
        status = str(payload.get("status") or "").strip()
        if status not in _SSE_RUN_TERMINAL_STATUSES:
            return
        clients = _sse_clients.get(plugin_id)
        if not clients:
            return
        frame = "data: " + json.dumps({
            "type": "run",
            "plugin_id": plugin_id,
            "run_id": payload.get("run_id"),
            "status": status,
        }, ensure_ascii=False) + "\n\n"
        for queue_obj in list(clients):
            _sse_queue_put_best_effort(queue_obj, frame)
    except Exception:
        return


def _ensure_runs_sse_bridge() -> None:
    """懒安装：首个 SSE 客户端连接时订阅 runs bus。幂等。"""
    global _SSE_RUNS_BRIDGE_INSTALLED, _SSE_RUNS_BRIDGE_SUB
    if _SSE_RUNS_BRIDGE_INSTALLED:
        return
    try:
        _SSE_RUNS_BRIDGE_SUB = state.bus_change_hub.subscribe("runs", _bridge_runs_event)
        _SSE_RUNS_BRIDGE_INSTALLED = True
    except Exception:
        _SSE_RUNS_BRIDGE_INSTALLED = False


def _origin_is_trusted(origin: str) -> bool:
    """push 广播的 Origin 校验：无 Origin（插件后端/curl）或本机回环放行。

    防止恶意网站通过浏览器表单 POST（no-cors）向 loopback 注入 SSE 消息。
    """
    origin = (origin or "").strip()
    if not origin:
        return True
    try:
        host = (urlparse(origin).hostname or "").lower()
    except Exception:
        return False
    return host in _TRUSTED_ORIGIN_HOSTS


def _is_loopback_host(host: str) -> bool:
    """判断对端主机是否本机回环：localhost、整个 IPv4 回环网段 127.0.0.0/8、
    IPv6 ::1，以及 IPv4-mapped IPv6 形式 ::ffff:127.x.x.x。

    push 移除共享密钥后，以此作为「仅本机回环可推送」的边界：不信任
    X-Forwarded-For 等转发头，直接看直连对端 request.client.host。
    """
    host = (host or "").strip().lower()
    if host in ("localhost", "::1"):
        return True
    if host.startswith("127."):  # 整个 127.0.0.0/8 都是 IPv4 回环（本机后端可能绑 127.0.0.2 等）
        return True
    return host.startswith("::ffff:127.")


def _parse_push_payload(body: bytes) -> dict:
    """POST /ui-api/push 入参解析：支持 {"type":..., "text":..., ...} 或纯文本。

    type 为调用方（插件）自定义的消息类别，server 原样透传、不预设分类；
    text 为必填正文；style / avatar 等为可选扩展元数据。返回空 dict 表示无内容。
    """
    raw = body.decode("utf-8", "replace").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except Exception:
            return {"text": raw}  # 非合法 JSON → 回退纯文本
        if not isinstance(payload, dict):
            return {}  # JSON 但不是对象 → 拒绝
        text = str(payload.get("text") or "").strip()
        if not text:
            return {}  # 缺 text / 空 text → 拒绝（不回退纯文本，避免原始 JSON 被当正文）
        result: dict = {"text": text}
        msg_type = str(payload.get("type") or "").strip()
        if msg_type:
            result["type"] = msg_type
        # 可选的结构化数据透传（如 qq_message 的 qq_inbound），供 SSE 订阅者直接取用。
        data = payload.get("data")
        if isinstance(data, (dict, list)):
            result["data"] = data
        style = str(payload.get("style") or "").strip()
        if style in ("catgirl", "narration"):
            result["style"] = style
        placement = str(payload.get("placement") or "").strip()
        if placement in ("scrolling", "top"):
            result["placement"] = placement
        avatar = str(payload.get("avatar") or "").strip()
        if avatar:
            result["avatar"] = avatar
        return result
    return {"text": raw}


class HostedUiActionRequest(BaseModel):
    args: dict[str, object] = Field(default_factory=dict)
    kind: str = "panel"
    surface_id: str = "main"
    locale: str | None = None


async def _wait_for_request_disconnect(request: Request) -> None:
    while not await request.is_disconnected():
        await asyncio.sleep(0.05)


async def _await_action_or_disconnect(
    request: Request,
    action: Awaitable[dict[str, object]],
) -> dict[str, object]:
    action_task = asyncio.create_task(action)
    disconnect_task = asyncio.create_task(_wait_for_request_disconnect(request))
    try:
        done, _pending = await asyncio.wait(
            {action_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if action_task in done:
            return await action_task
        raise HTTPException(
            status_code=499,
            detail={"code": "hosted_action_client_disconnected"},
        )
    finally:
        for task in (action_task, disconnect_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(action_task, disconnect_task, return_exceptions=True)


async def _get_plugin_static_dir(plugin_id: str) -> Path | None:
    """获取插件的静态文件目录
    
    只有插件显式调用 register_static_ui() 后才会返回静态目录。
    
    Args:
        plugin_id: 插件 ID
    
    Returns:
        静态文件目录路径，如果未注册或不存在则返回 None
    """
    return await plugin_ui_query_service.get_static_dir(plugin_id)


async def _get_static_ui_config(plugin_id: str) -> dict[str, object] | None:
    """获取插件的静态 UI 配置"""
    return await plugin_ui_query_service.get_static_ui_config(plugin_id)


def _get_mime_type(file_path: Path) -> str:
    """获取文件的 MIME 类型"""
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if mime_type:
        return mime_type
    
    # 默认类型映射
    suffix = file_path.suffix.lower()
    mime_map = {
        ".html": "text/html",
        ".htm": "text/html",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".eot": "application/vnd.ms-fontobject",
    }
    return mime_map.get(suffix, "application/octet-stream")


@router.get("/plugin/{plugin_id}/ui")
@router.get("/plugin/{plugin_id}/ui/")
async def plugin_ui_index(plugin_id: str):
    """获取插件 UI 入口页面"""
    try:
        static_dir = await _get_plugin_static_dir(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
    
    if not static_dir:
        raise HTTPException(
            status_code=404,
            detail=f"Plugin '{plugin_id}' not found or has no static directory"
        )
    
    index_file = static_dir / "index.html"
    if not index_file.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Plugin '{plugin_id}' has no index.html in static directory"
        )
    
    return FileResponse(
        str(index_file),
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.get("/plugin/{plugin_id}/ui-api/locale")
async def plugin_ui_api_locale(plugin_id: str) -> JSONResponse:
    """返回当前生效的全局 UI 语言。

    通用接口，所有静态 UI 插件均可调用：i18n.js 在 init() 时取一次以决定
    要 fetch 哪份翻译 bundle。返回完整 locale（如 `zh-TW`、`en-US`），交给
    前端的 _localeCandidates 自行 fallback。
    """
    try:
        from utils.language_utils import get_global_language_full

        locale = str(get_global_language_full())
    except Exception:
        locale = "en"
    return JSONResponse(
        {"locale": locale},
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/plugin/{plugin_id}/ui-api/i18n/{locale}.json")
async def plugin_ui_api_i18n_bundle(plugin_id: str, locale: str) -> Response:
    """从插件根目录 `i18n/<locale>.json` 提供翻译 bundle。

    通用接口，与 `register_static_ui()` 解耦：只要插件目录下有 `i18n/`
    文件夹即可。i18n.js 通常按 `_localeCandidates` 顺序尝试多个 locale，
    fallback 命中前每个都会发一次 HTTP，因此这里：
      - 用 `_I18N_BUNDLE_CACHE`（按文件 mtime）避免重复读盘；
      - 给 200 响应加 `max-age=300`，让 iframe 之间走浏览器缓存；
      - locale 用正则白名单挡 path-traversal。
    """
    if not _I18N_LOCALE_PATTERN.match(locale):
        raise HTTPException(status_code=404, detail=f"Invalid locale: {locale!r}")

    try:
        plugin_meta = await plugin_ui_query_service.get_plugin_meta(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)

    if plugin_meta is None:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")

    config_path_obj = plugin_meta.get("config_path")
    if not isinstance(config_path_obj, str) or not config_path_obj:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' has no config_path")

    try:
        plugin_dir = Path(config_path_obj).parent.resolve()
    except Exception:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' config_path invalid")

    bundle_file = (plugin_dir / "i18n" / f"{locale}.json").resolve()
    try:
        bundle_file.relative_to(plugin_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path traversal detected")

    if not bundle_file.is_file():
        raise HTTPException(status_code=404, detail=f"Locale bundle '{locale}' not found")

    try:
        mtime = bundle_file.stat().st_mtime
    except OSError:
        raise HTTPException(status_code=404, detail=f"Locale bundle '{locale}' not readable")

    cached = _I18N_BUNDLE_CACHE.get(bundle_file)
    if cached is None or cached[0] != mtime:
        try:
            payload = bundle_file.read_bytes()
        except OSError:
            raise HTTPException(status_code=500, detail=f"Failed to read locale bundle '{locale}'")
        cached = (mtime, payload)
        _I18N_BUNDLE_CACHE[bundle_file] = cached

    return Response(
        content=cached[1],
        media_type="application/json; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=300",
            "ETag": f'W/"{plugin_id}-{locale}-{int(mtime)}"',
        },
    )


@router.get("/plugin/{plugin_id}/ui-api/events")
async def plugin_ui_sse_events(plugin_id: str):
    """插件静态 UI 的 SSE 事件流（后端 → 前端实时推送）。

    前端页面用 EventSource 连这里，即时接收 /ui-api/push 广播的实时更新；
    同时订阅 runs bus，run 完成后台推送 ``type:run`` 事件，替代前端紧轮询。
    """
    # 有界队列：慢/阻塞客户端不被无限缓冲（满时由 push 侧丢弃新消息）
    queue_obj: asyncio.Queue = asyncio.Queue(maxsize=_SSE_QUEUE_MAX)
    # 首个客户端连接时安装 runs bus → SSE 桥（幂等）
    _ensure_runs_sse_bridge()

    async def event_stream():
        await _sse_clients_lock.acquire()
        try:
            _sse_clients.setdefault(plugin_id, []).append(queue_obj)
        finally:
            _sse_clients_lock.release()
        try:
            yield "data: " + json.dumps({"type": "hello", "lanes": 12}, ensure_ascii=False) + "\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue_obj.get(), timeout=15.0)
                    yield payload
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await _sse_clients_lock.acquire()
            try:
                clients = _sse_clients.get(plugin_id)
                if clients and queue_obj in clients:
                    clients.remove(queue_obj)
                # 列表空则删除 plugin_id，避免任意路径参数留下永久空条目
                if clients is not None and not clients:
                    _sse_clients.pop(plugin_id, None)
            finally:
                _sse_clients_lock.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/plugin/{plugin_id}/ui-api/push")
async def plugin_ui_push(plugin_id: str, request: Request):
    """向插件静态 UI 的所有 SSE 客户端广播一条实时消息（后端 → 前端推送）。

    入参：{"text": "..."} 或纯文本。任意插件/后端调用，推送到已订阅的前端页面。
    返回 ``queued``：成功写入各客户端缓冲队列的数目（排队计数，非客户端实际送达确认）。

    鉴权：仅本机回环客户端可直接推送（不再要求共享密钥；对端非回环一律拒绝，
    伪造 Origin / 转发头均无法绕过；Origin 校验仍防跨站注入）。
    """
    # 回环校验：只接受本机回环客户端。用直连对端 request.client.host，不信任
    # X-Forwarded-For，避免伪造转发头绕过（非回环部署应保留其他鉴权/可信代理）。
    client_host = request.client.host if request.client else ""
    if not _is_loopback_host(client_host):
        return JSONResponse({"ok": False, "error": "non-loopback push rejected"}, status_code=403)
    # Origin 校验：浏览器跨站表单 POST（no-cors）可向 loopback 注入 SSE；
    # 只接受无 Origin（插件后端/curl）或本机回环 Origin
    if not _origin_is_trusted(request.headers.get("origin", "")):
        return JSONResponse({"ok": False, "error": "invalid origin"}, status_code=403)
    # 流式读取请求体：累计字节数，超过固定上限立即 413，避免本机恶意分块请求占内存
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _PUSH_MAX_BODY:
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    payload = _parse_push_payload(body)
    if not payload.get("text"):
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    # type 由插件自定义并原样透传（server 不预设分类）；未指定则不带 type 字段
    event: dict = {"text": payload["text"]}
    if payload.get("type"):
        event["type"] = payload["type"]
    if "data" in payload:  # 空容器([]/{})常代表清空/重置状态，按 key 存在而非 truthiness 保留
        event["data"] = payload["data"]
    if payload.get("style"):
        event["style"] = payload["style"]
    if payload.get("placement"):
        event["placement"] = payload["placement"]
    if payload.get("avatar"):
        event["avatar"] = payload["avatar"]
    data = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
    queued = 0
    await _sse_clients_lock.acquire()
    try:
        clients = _sse_clients.get(plugin_id, [])
        for c in list(clients):
            try:
                if _sse_queue_put_best_effort(c, data):  # 满了丢最旧、新帧保留
                    queued += 1
            except Exception as exc:  # 其他运行时故障（如队列已关闭）记录，不阻断其它客户端
                logger.warning("[plugin-ui] SSE push 队列写入失败: %s", exc)
    finally:
        _sse_clients_lock.release()
    return JSONResponse({"ok": True, "queued": queued})


@router.get("/plugin/{plugin_id}/ui/{file_path:path}")
async def plugin_ui_file(plugin_id: str, file_path: str):
    """获取插件 UI 静态文件"""
    if not file_path:
        # 重定向到 index
        return await plugin_ui_index(plugin_id)
    
    try:
        static_dir = await _get_plugin_static_dir(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
    
    if not static_dir:
        raise HTTPException(
            status_code=404,
            detail=f"Plugin '{plugin_id}' not found or has no static directory"
        )
    
    # 解析文件路径
    target_file = (static_dir / file_path).resolve()
    
    # 安全检查：确保文件在 static 目录内
    try:
        target_file.relative_to(static_dir.resolve())
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Access denied: path traversal detected"
        )
    
    if not target_file.exists():
        raise HTTPException(
            status_code=404,
            detail=f"File not found: {file_path}"
        )
    
    if not target_file.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Not a file: {file_path}"
        )
    
    mime_type = _get_mime_type(target_file)
    
    # 获取缓存控制配置
    try:
        ui_config = await _get_static_ui_config(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
    cache_control = "public, max-age=3600"
    if ui_config is not None:
        cache_control_obj = ui_config.get("cache_control")
        if isinstance(cache_control_obj, str) and cache_control_obj:
            cache_control = cache_control_obj
    
    return FileResponse(
        str(target_file),
        media_type=mime_type,
        headers={
            "Cache-Control": cache_control,
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.get("/plugin/{plugin_id}/ui-info")
async def plugin_ui_info(plugin_id: str):
    """获取插件 UI 信息
    
    返回插件是否有 UI、UI 入口路径等信息。
    """
    try:
        ui_info = await plugin_ui_query_service.get_ui_info(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
    return JSONResponse(ui_info)


@router.get("/plugin/{plugin_id}/surfaces")
async def plugin_ui_surfaces(plugin_id: str, locale: str | None = None):
    """获取插件统一 UI Surface 列表。

    LEGACY_STATIC_UI_COMPAT:
    Existing static UI is normalized as a mode="static" panel surface.
    """
    try:
        surfaces = await plugin_ui_query_service.get_surfaces(plugin_id, locale=locale)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
    return JSONResponse(surfaces)


@router.get("/plugin/{plugin_id}/hosted-ui/source")
async def plugin_hosted_ui_source(
    plugin_id: str,
    kind: str = "panel",
    id: str = "main",
    locale: str | None = None,
):
    """读取 hosted surface 源码。

    用于 hosted-tsx / markdown 的只读 source MVP。`locale` 参数让 markdown
    教程按当前 UI 语言挑同名的 `<entry>.<locale>.md` 兄弟文件，命中失败
    时回退到默认（不带 locale 后缀）的 entry 文件。
    """
    try:
        source = await plugin_ui_query_service.get_surface_source(
            plugin_id,
            kind=kind,
            surface_id=id,
            locale=locale,
        )
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
    return JSONResponse(source)


@router.get("/plugin/{plugin_id}/hosted-ui/context")
async def plugin_hosted_ui_context(plugin_id: str, kind: str = "panel", id: str = "main", locale: str | None = None):
    """获取 hosted surface 只读上下文。"""
    try:
        context = await plugin_ui_query_service.get_surface_context(
            plugin_id,
            kind=kind,
            surface_id=id,
            locale=locale,
        )
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
    return JSONResponse(context)


@router.post("/plugin/{plugin_id}/hosted-ui/action/{action_id}")
async def plugin_hosted_ui_action(
    plugin_id: str,
    action_id: str,
    http_request: Request,
    request: HostedUiActionRequest,
):
    """执行 hosted surface 动作；第一版复用本插件 plugin_entry。"""
    try:
        result = await _await_action_or_disconnect(
            http_request,
            plugin_ui_query_service.call_surface_action(
                plugin_id,
                action_id=action_id,
                args=request.args,
                kind=request.kind,
                surface_id=request.surface_id,
                locale=request.locale,
            ),
        )
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)
    return JSONResponse(result)
