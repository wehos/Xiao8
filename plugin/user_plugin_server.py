"""
User Plugin Server

HTTP 服务器主入口文件。
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Callable, IO

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_PACKAGE_ROOT = Path(__file__).resolve().parent


def _prepend_sys_path(path: Path, index: int) -> None:
    value = str(path)
    try:
        while value in sys.path:
            sys.path.remove(value)
    except Exception:
        pass
    sys.path.insert(index, value)


# Keep import resolution deterministic even when launcher/sitecustomize preloads paths.
_prepend_sys_path(_PROJECT_ROOT, 0)
_prepend_sys_path(_PLUGIN_PACKAGE_ROOT, 1)


def _parse_tcp_endpoint(endpoint: str) -> tuple[str, int] | None:
    if not isinstance(endpoint, str) or not endpoint.startswith("tcp://"):
        return None
    host_port = endpoint[6:]
    if ":" not in host_port:
        return None
    host, port_text = host_port.rsplit(":", 1)
    if not host:
        return None
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    return host, port


def _is_tcp_port_available(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except OSError:
            pass


def _find_next_available_port(host: str, start_port: int, max_tries: int = 50) -> int | None:
    for port in range(start_port, start_port + max_tries):
        if _is_tcp_port_available(host, port):
            return port
    return None


def _ensure_plugin_zmq_endpoint_available() -> None:
    endpoint = os.getenv("NEKO_PLUGIN_ZMQ_IPC_ENDPOINT", "tcp://127.0.0.1:38765")
    parsed = _parse_tcp_endpoint(endpoint)
    if parsed is None:
        return
    host, base_port = parsed
    if _is_tcp_port_available(host, base_port):
        return

    fallback_port = _find_next_available_port(host, base_port + 1, max_tries=100)
    if fallback_port is None:
        return

    fallback_endpoint = f"tcp://{host}:{fallback_port}"
    os.environ["NEKO_PLUGIN_ZMQ_IPC_ENDPOINT"] = fallback_endpoint
    try:
        print(
            (
                "[user_plugin_server] NEKO_PLUGIN_ZMQ_IPC_ENDPOINT occupied, "
                f"fallback to {fallback_endpoint}"
            ),
            file=sys.stderr,
        )
    except (OSError, ValueError, RuntimeError):
        pass


_ensure_plugin_zmq_endpoint_available()

from config import USER_PLUGIN_SERVER_PORT

# ──────────────────────────────────────────────────────────────────────────
#  ⚠ 严禁再引入 loguru。所有 plugin/* 已统一走 plugin.logging_config →
#  utils.logger_config.RobustLoggerConfig。曾经的 brace-compat 与
#  loguru→stdlib bridge 已迁入 plugin/logging_config.py。
#  规则：再有人在本仓库 import loguru —— 就把谁杀了。
#  lint 守门：scripts/check_no_loguru.py（CI: .github/workflows/analyze.yml）
# ──────────────────────────────────────────────────────────────────────────

# -- Unified stdlib logger, same as agent_server / memory_server --
try:
    from utils.logger_config import setup_logging
except ModuleNotFoundError:
    import importlib.util

    _logger_config_path = _PROJECT_ROOT / "utils" / "logger_config.py"
    _spec = importlib.util.spec_from_file_location("utils.logger_config", _logger_config_path)
    if _spec is None or _spec.loader is None:
        raise ModuleNotFoundError(f"failed to load logger config from {_logger_config_path}")

    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    setup_logging = getattr(_module, "setup_logging")

# 提前固定主 PluginServer 进程的 service_name —— plugin.logging_config.get_logger()
# 通过 NEKO_PLUGIN_SERVICE_NAME 决定 logger 父节点（N.E.K.O.<service>.<comp>）。
# 不设的话会 fallback 到 "Plugin"，导致 message_plane / runs / config 等模块的日志
# 跑去 N.E.K.O_Plugin_*.log，与 PluginServer 自身的 N.E.K.O_PluginServer_*.log 分裂。
#
# 用 ``=`` 而不是 ``setdefault``：父进程（Electron / launcher 脚本）若意外携带
# 旧的 ``Plugin_<id>`` 环境（比如复用了一个调试 shell），setdefault 会保留旧值，
# 然后下面的 ``setup_logging("PluginServer")`` 和 ``get_logger()`` 会路由到两个
# 不同的 namespace，再次造成日志分裂。这里强制覆盖。
os.environ["NEKO_PLUGIN_SERVICE_NAME"] = "PluginServer"

logger, _log_config = setup_logging(service_name="PluginServer", log_level=logging.INFO)

# Side-effect import：触发 plugin/logging_config.py 顶层的
# _install_logging_brace_compat()，让所有 plugin/* 子模块共享 brace-format
# 兼容（logger.info("msg {}", x) 不抛 TypeError）。模块本身不需要 alias。
import plugin.logging_config  # noqa: E402,F401  -- side-effect only

# -- uvicorn logging bridge --
def _configure_uvicorn_logging_bridge() -> None:
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(logger_name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True


_configure_uvicorn_logging_bridge()

# Must run before any event loop gets created on Windows.
def _configure_windows_event_loop_policy() -> None:
    if sys.platform != "win32":
        return
    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:
        return
    try:
        asyncio.set_event_loop_policy(policy_cls())
    except (RuntimeError, ValueError, TypeError, AttributeError):
        try:
            print("[user_plugin_server] failed to set WindowsSelectorEventLoopPolicy", file=sys.stderr)
        except (OSError, RuntimeError, ValueError):
            pass


def _disable_windows_plugin_zmq_when_tornado_missing() -> None:
    if sys.platform != "win32":
        return
    try:
        import tornado  # type: ignore  # noqa: F401
        return
    except Exception:
        pass
    os.environ["NEKO_PLUGIN_ZMQ_IPC_ENABLED"] = "false"
    try:
        print(
            "[user_plugin_server] tornado not found on Windows; disable plugin ZeroMQ IPC",
            file=sys.stderr,
        )
    except (OSError, RuntimeError, ValueError):
        pass


_configure_windows_event_loop_policy()
_disable_windows_plugin_zmq_when_tornado_missing()

from plugin.server.http_app import build_plugin_server_app  # noqa: E402


# 独立跑的时候没有别人管生命周期，所以这里显式认领。内嵌进 agent_server 的那条
# 路径（app/agent_server/plugin_host.py）不认领，由 user_plugin_enabled 开关驱动。
app = build_plugin_server_app(manage_lifecycle=True)


def _can_register_faulthandler_signal() -> bool:
    return hasattr(faulthandler, "register") and hasattr(signal, "SIGUSR1")


def _enable_fault_handler_dump_file() -> IO[str] | None:
    # 路径来自本体 RobustLoggerConfig（5 级可写目录回退，AppImage squashfs 安全）。
    # 严禁再用 Path(__file__).parent / "log" —— 那是只读 squashfs，会直接崩。
    try:
        from utils.logger_config import RobustLoggerConfig
        dump_dir = Path(RobustLoggerConfig(service_name="PluginServer").get_log_directory_path())
    except Exception as exc:
        logger.warning("failed to resolve log dir for faulthandler, fallback to tempdir: %s", exc)
        import tempfile
        dump_dir = Path(tempfile.gettempdir()) / "neko" / "logs"
    dump_path = dump_dir / "faulthandler_dump.log"
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("failed to create faulthandler dump directory: %s (%s)", dump_path.parent, exc)

    try:
        dump_file = dump_path.open("a", encoding="utf-8")
    except OSError as exc:
        logger.warning("failed to open faulthandler dump file: %s (%s)", dump_path, exc)
        return None

    try:
        faulthandler.enable(file=dump_file)
        if _can_register_faulthandler_signal():
            faulthandler.register(signal.SIGUSR1, all_threads=True, file=dump_file)
        return dump_file
    except (RuntimeError, OSError, AttributeError, ValueError) as exc:
        logger.warning("failed to enable faulthandler dump file: %s (%s)", dump_path, exc)
        try:
            dump_file.close()
        except OSError:
            pass
        return None


def _enable_fault_handler_fallback() -> None:
    try:
        faulthandler.enable()
        if _can_register_faulthandler_signal():
            faulthandler.register(signal.SIGUSR1, all_threads=True)
    except (RuntimeError, OSError, AttributeError, ValueError) as exc:
        logger.warning("failed to enable fallback faulthandler: %s", exc)


def _get_child_pids(parent_pid: int) -> list[int]:
    """Best-effort list of direct child PIDs (POSIX only, no psutil)."""
    pids: list[int] = []
    try:
        import subprocess as _sp
        result = _sp.run(
            ["pgrep", "-P", str(parent_pid)],
            capture_output=True, text=True, timeout=3, check=False,
        )
        for line in result.stdout.splitlines():
            s = line.strip()
            if s.isdigit():
                pids.append(int(s))
    except Exception as exc:
        logging.getLogger(__name__).warning("_get_child_pids failed for pid %s: %s", parent_pid, exc)
    return pids


def _find_available_port(host: str, start_port: int, max_tries: int = 50) -> int:
    for port in range(start_port, start_port + max_tries):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            try:
                sock.close()
            except OSError:
                pass
    raise RuntimeError(
        f"no available port in range {start_port}-{start_port + max_tries - 1} on {host}"
    )


if __name__ == "__main__":
    import uvicorn

    host = "127.0.0.1"
    base_port = int(os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", str(USER_PLUGIN_SERVER_PORT)))

    dump_file = _enable_fault_handler_dump_file()
    if dump_file is None:
        _enable_fault_handler_fallback()

    try:
        selected_port = _find_available_port(host, base_port)
    except RuntimeError as exc:
        logger.error("Cannot start plugin server: %s", exc)
        sys.exit(1)
    os.environ["NEKO_USER_PLUGIN_SERVER_PORT"] = str(selected_port)
    if selected_port != base_port:
        logger.warning("User plugin server port %s is unavailable, switched to %s", base_port, selected_port)
    else:
        logger.info("User plugin server starting on %s:%s", host, selected_port)

    sigint_count = 0
    sigint_lock = threading.Lock()
    force_exit_timer: threading.Timer | None = None

    config = uvicorn.Config(
        app,
        host=host,
        port=selected_port,
        log_config=None,
        backlog=4096,
        timeout_keep_alive=30,
    )
    server = uvicorn.Server(config)

    def _start_force_exit_watchdog(timeout_s: float) -> None:
        global force_exit_timer
        if force_exit_timer is not None:
            return

        def _kill() -> None:
            os._exit(130)

        timer = threading.Timer(float(timeout_s), _kill)
        timer.daemon = True
        timer.start()

        force_exit_timer = timer

    def _sigint_handler(_signum: int, _frame: FrameType | None) -> None:
        global sigint_count
        with sigint_lock:
            sigint_count += 1
            current_count = sigint_count

        if current_count >= 2:
            os._exit(130)

        server.should_exit = True
        server.force_exit = True
        _start_force_exit_watchdog(timeout_s=2.0)

    old_sigint: int | Callable[[int, FrameType | None], object] | None = None
    try:
        old_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _sigint_handler)
        signal.signal(signal.SIGTERM, _sigint_handler)
        if hasattr(signal, "SIGQUIT"):
            signal.signal(signal.SIGQUIT, _sigint_handler)
    except (ValueError, OSError, RuntimeError) as exc:
        old_sigint = None
        logger.warning("failed to register shutdown signals: %s", exc)

    server.install_signal_handlers = lambda: None

    cleanup_old_sigint: int | Callable[[int, FrameType | None], object] | None = None
    try:
        server.run()
    finally:
        try:
            cleanup_old_sigint = signal.getsignal(signal.SIGINT)

            def _force_quit(_signum: int, _frame: FrameType | None) -> None:
                os._exit(130)

            signal.signal(signal.SIGINT, _force_quit)
        except (ValueError, OSError, RuntimeError):
            cleanup_old_sigint = None

        try:
            import psutil
        except ImportError:
            psutil = None

        if psutil is not None:
            try:
                parent = psutil.Process(os.getpid())
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                _, alive = psutil.wait_procs(children, timeout=0.5)
                for process in alive:
                    try:
                        process.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except KeyboardInterrupt:
                pass
            except (psutil.Error, OSError, RuntimeError, ValueError) as exc:
                logger.warning("failed to cleanup child processes: %s", exc)
        else:
            try:
                for child_pid in _get_child_pids(os.getpid()):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except OSError as exc:
                        logger.warning("failed to kill child pid %s: %s", child_pid, exc)
            except Exception as exc:
                logger.warning("child process cleanup failed: %s", exc)

        if force_exit_timer is not None:
            force_exit_timer.cancel()

        if cleanup_old_sigint is not None:
            try:
                signal.signal(signal.SIGINT, cleanup_old_sigint)
            except (ValueError, OSError, RuntimeError):
                pass

        if old_sigint is not None:
            try:
                signal.signal(signal.SIGINT, old_sigint)
            except (ValueError, OSError, RuntimeError):
                pass

        if dump_file is not None:
            try:
                dump_file.close()
            except OSError:
                pass
