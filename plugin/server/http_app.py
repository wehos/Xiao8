"""Reusable FastAPI app factory for the plugin HTTP server."""
from __future__ import annotations

import asyncio
import faulthandler
import importlib
import os
import signal
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import RedirectResponse, Response

from plugin.logging_config import get_logger
from utils.host_origin_guard import HostOriginGuardMiddleware
from utils.logger_config import get_module_logger
from plugin.server.infrastructure.exceptions import register_exception_handlers
from plugin.server.lifecycle import shutdown as lifecycle_shutdown
from plugin.server.lifecycle import startup as lifecycle_startup
from plugin.server.routes import (
    config_router,
    documents_router,
    frontend_router,
    health_router,
    llm_tools_router,
    logs_router,
    market_bridge_router,
    media_router,
    messages_router,
    metrics_router,
    plugin_cli_router,
    plugin_ui_router,
    plugins_router,
    runs_router,
    websocket_router,
)
from plugin.server.routes.frontend import mount_static_files

_EMBEDDED_BY_AGENT = os.getenv("NEKO_PLUGIN_HOSTED_BY_AGENT", "").strip().lower() == "true"

if _EMBEDDED_BY_AGENT:
    logger = get_module_logger(__name__, "Agent")
else:
    logger = get_logger("server.user_plugin_server")


def _can_register_faulthandler_signal() -> bool:
    return hasattr(faulthandler, "register") and hasattr(signal, "SIGUSR1")


def _model_settings_url(request: Request, main_server_port: int) -> str:
    public_origin = os.getenv("NEKO_MAIN_SERVER_PUBLIC_ORIGIN", "").strip()
    if public_origin:
        try:
            parsed = urlsplit(public_origin)
            _ = parsed.port
            valid_origin = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            )
            if valid_origin:
                return urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        "/api_key",
                        "",
                        "",
                    )
                )
        except ValueError:
            pass
        logger.warning(
            "Ignoring invalid NEKO_MAIN_SERVER_PUBLIC_ORIGIN: {}", public_origin
        )

    # The plugin server and main server use different ports in a direct/LAN
    # deployment, but they are reached through the same client-visible host.
    # Keep loopback only when the request itself was loopback. Reverse proxies
    # with mapped ports or TLS can provide the explicit public origin above.
    hostname = request.url.hostname or "127.0.0.1"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    scheme = request.url.scheme if request.url.scheme in {"http", "https"} else "http"
    return urlunsplit(
        (scheme, f"{hostname}:{int(main_server_port)}", "/api_key", "", "")
    )


def _include_optional_router(
    app: FastAPI,
    *,
    module_name: str,
    router_name: str = "router",
    label: str,
) -> None:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        logger.warning(
            "{} unavailable, endpoints will be 404: err_type={}, err={}",
            label,
            type(exc).__name__,
            str(exc),
        )
        return

    router = getattr(module, router_name, None)
    if router is None:
        logger.error(
            "{} unavailable, endpoints will be 404: missing {}",
            label,
            router_name,
        )
        return

    app.include_router(router)


@asynccontextmanager
async def plugin_server_lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 谁负责插件生命周期，由**建这个 app 的人**说了算，见
    # build_plugin_server_app 的 manage_lifecycle。默认 False：没人明说就不起，
    # 这个方向的错误是「独立服务器里插件不自启」——看得见、也没人受伤。
    manage_lifecycle = bool(getattr(app.state, "manage_plugin_lifecycle", False))

    if _can_register_faulthandler_signal():
        try:
            faulthandler.register(signal.SIGUSR1, all_threads=True)
        except (RuntimeError, OSError, AttributeError, ValueError) as exc:
            logger.debug(
                "failed to register faulthandler SIGUSR1: err_type={}, err={}",
                type(exc).__name__,
                str(exc),
            )

    stop_event = threading.Event()
    last_heartbeat: dict[str, float] = {"t": time.monotonic()}

    async def _heartbeat() -> None:
        while not stop_event.is_set():
            last_heartbeat["t"] = time.monotonic()
            await asyncio.sleep(0.5)

    def _watchdog() -> None:
        threshold = 8.0
        while not stop_event.is_set():
            now = time.monotonic()
            elapsed = now - last_heartbeat["t"]
            if elapsed > threshold:
                logger.error(
                    "Event loop appears blocked (no heartbeat for {:.1f}s); dumping all thread tracebacks",
                    elapsed,
                )
                try:
                    faulthandler.dump_traceback(all_threads=True)
                except (RuntimeError, OSError, ValueError, AttributeError) as exc:
                    logger.warning(
                        "failed to dump traceback: err_type={}, err={}",
                        type(exc).__name__,
                        str(exc),
                    )
                last_heartbeat["t"] = now
            time.sleep(1.0)

    watchdog_thread = threading.Thread(target=_watchdog, daemon=True, name="event-loop-watchdog")
    watchdog_thread.start()

    heartbeat_task = asyncio.create_task(_heartbeat(), name="server-heartbeat")

    # 内嵌进 agent_server 时，生命周期由外部按 user_plugin_enabled 开关管理，
    # 这里绝不能自动起——起了就是把整轮插件元数据扫描拉回端口 bind 之前。
    #
    # 以前这个判断读的是模块级的 _EMBEDDED_BY_AGENT，也就是**import 那一刻**的
    # NEKO_PLUGIN_HOSTED_BY_AGENT。它今天恰好是对的，只因为 agent_server 在第一次
    # import 这个模块之前先设了环境变量——两件相隔很远、谁都没保证的事。任何人在
    # 那之前先 import 到 plugin.server.http_app，这个常量就冻成 False，全量扫描
    # **静默**回到启动路径，没有任何东西会红。
    if manage_lifecycle:
        await lifecycle_startup()

    # Install-source lock subsystem: tracks plugin provenance (builtin/manual/
    # imported/market). Runs after lifecycle_startup so filesystem state is stable.
    try:
        from plugin.server.application.install_source import (
            StartupReconciler,
            build_install_source_manager,
            set_global_manager,
        )
        _install_source_mgr = build_install_source_manager()
        await StartupReconciler(_install_source_mgr).run()
        set_global_manager(_install_source_mgr)
    except Exception as exc:
        logger.error(
            "InstallSourceManager init failed, subsystem degraded: {}", exc,
        )
        try:
            from plugin.server.application.install_source import set_global_manager
            set_global_manager(None)
        except Exception:
            pass  # already in degraded mode

    # Write bridge token file for Market frontend / URI handler
    try:
        from plugin.server.routes.market_bridge import write_bridge_token_file
        from pathlib import Path
        write_bridge_token_file(Path.home() / ".neko")
    except Exception as exc:
        logger.warning("Failed to write bridge token file: {}", exc)

    try:
        yield
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            logger.debug("heartbeat task cancelled")
        except RuntimeError as exc:
            logger.warning(
                "heartbeat task failed while stopping: err_type={}, err={}",
                type(exc).__name__,
                str(exc),
            )
        if manage_lifecycle:
            await lifecycle_shutdown()


def build_plugin_server_app(
    title: str = "N.E.K.O User Plugin Server",
    *,
    manage_lifecycle: bool = False,
) -> FastAPI:
    """Build the plugin HTTP app.

    ``manage_lifecycle`` says whether this app owns the plugin lifecycle — the
    full metadata refresh plus autostart. Only the standalone entry point does;
    when embedded in agent_server the lifecycle is driven externally by the
    ``user_plugin_enabled`` flag.

    It defaults to ``False`` on purpose. Getting it wrong in that direction means
    "plugins do not autostart in the standalone server", which is visible the
    moment anyone looks. The other direction puts a full plugin scan back on the
    startup path before any port binds, and nothing reports it.
    """
    app = FastAPI(title=title, lifespan=plugin_server_lifespan)
    app.state.manage_plugin_lifecycle = manage_lifecycle

    @app.get("/api_key", include_in_schema=False)
    async def redirect_model_settings(request: Request) -> RedirectResponse:
        import config

        return RedirectResponse(
            url=_model_settings_url(request, int(config.MAIN_SERVER_PORT)),
            status_code=307,
        )

    # Market 域名通过 settings 配置，支持自部署
    from plugin.settings import MARKET_ORIGINS as _market_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:48911",
            "http://127.0.0.1:48911",
            *_market_origins,
        ],
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Error-Code"],
    )

    register_exception_handlers(app)
    mount_static_files(app)

    @app.middleware("http")
    async def _frontend_cache_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        path = request.url.path

        if path.startswith("/ui/assets/"):
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
            return response

        if path in {"/ui", "/ui/"} or (path.startswith("/ui/") and path.endswith(".html")):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        return response

    app.include_router(health_router)
    app.include_router(documents_router)
    app.include_router(plugins_router)
    app.include_router(runs_router)
    app.include_router(messages_router)
    app.include_router(metrics_router)
    app.include_router(config_router)
    app.include_router(logs_router)
    app.include_router(media_router)
    app.include_router(frontend_router)
    app.include_router(websocket_router)
    app.include_router(plugin_ui_router)
    # Built-in plugin routes are optional. In AppImage/Nuitka builds,
    # ``plugin.plugins`` can be intentionally excluded, and optional plugin
    # import-time failures must not prevent the base plugin server from starting.
    _include_optional_router(
        app,
        module_name="plugin.server.routes.plugin_install",
        label="plugin install routes",
    )
    app.include_router(plugin_cli_router)
    app.include_router(llm_tools_router)
    app.include_router(market_bridge_router)
    # Keep the Host/Origin guard outside CORS and the cache-header middleware;
    # untrusted requests must not be short-circuited before the guard runs.
    app.add_middleware(HostOriginGuardMiddleware)
    return app
