import os as _os
import sys as _sys
import tempfile as _tempfile
from pathlib import Path as _Path

# Project root must be on sys.path before importing `utils.*` — works even when
# the project isn't installed as a wheel (e.g. bare `python -m pytest tests/`).
_project_root = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..'))
if _project_root not in _sys.path:
    _sys.path.insert(0, _project_root)


def _ensure_builtin_live2d_unpacked(model_name):
    # Some tests read static/<model>/* directly (Live2D model). The models are
    # shipped as assets/<model>.tar.gz; auto-unpack so pytest works without
    # requiring build_frontend to have been run.
    archive = _Path(_project_root) / "assets" / f"{model_name}.tar.gz"
    target_root = _Path(_project_root) / "static"
    target_dir = target_root / model_name
    marker = target_dir / f"{model_name}.moc3"
    if not archive.exists():
        return
    if marker.exists() and marker.stat().st_mtime >= archive.stat().st_mtime:
        return
    import shutil
    import sys
    import tarfile
    if target_dir.exists():
        shutil.rmtree(target_dir)
    with tarfile.open(archive, "r:gz") as tf:
        # filter='data' added in Python 3.12; archive ships in-repo (trusted).
        if sys.version_info >= (3, 12):
            tf.extractall(target_root, filter="data")
        else:
            tf.extractall(target_root)
    # tarfile preserves archived member mtimes by default, so the marker would
    # stay older than the archive's filesystem mtime → freshness gate above
    # would re-extract on every session. Refresh marker so subsequent runs skip.
    marker.touch()


for _builtin_live2d_model in ("yui-origin", "yui-lolita"):
    _ensure_builtin_live2d_unpacked(_builtin_live2d_model)

# Redirect test logs out of the user's real %USERPROFILE%/Documents/N.E.K.O/logs.
# Without this, every pytest session — including ones that intentionally inject
# OSError / 坏 JSON / mock-driven failures via patches — dumps ERROR lines into
# the user's Documents tree.
#
# We override RobustLoggerConfig._get_log_directory directly (rather than going
# through NEKO_STORAGE_SELECTED_ROOT) because that env var also drives
# ConfigManager / cloudsave_runtime layout, and pointing those at the temp dir
# triggers a legacy-app-root migration scan that rmtrees the temp dir mid-test.
# Loggers are constructed at module import time, so the patch must happen here
# in conftest BEFORE any project module is imported.
_NEKO_TEST_LOG_ROOT = _Path(_tempfile.gettempdir()) / f"neko_test_logs_{_os.getpid()}"
_NEKO_TEST_LOG_ROOT.mkdir(parents=True, exist_ok=True)
from utils import logger_config as _logger_config_module
# Override only the Documents-fallback hook (priority 2 in _get_log_directory).
# Env-var-based override (priority 1) and the cascade through application/system
# data dirs stay intact — so tests that use monkeypatch.setenv on
# NEKO_STORAGE_SELECTED_ROOT still see the override they expect.
_logger_config_module.RobustLoggerConfig._get_documents_directory = (
    lambda self, _root=_NEKO_TEST_LOG_ROOT: _root
)

# 同理，但针对运行根本身：get_config_manager() 的 migrate 默认为 True，
# 谁先 import 那 7 个模块级单例之一，谁就在真实运行根上跑完整条迁移链。
# 必须在任何产品模块被 import 之前装好（见 tests/real_root_isolation.py）。
from utils.config_manager import ConfigManager as _ConfigManagerForIsolation
from tests import real_root_isolation as _real_root_isolation

_real_root_isolation.install(_ConfigManagerForIsolation)

import asyncio
import asyncio.runners
import asyncio.coroutines
import nest_asyncio

nest_asyncio.apply()

_orig_asyncio_run = asyncio.run
_orig_runner_run = asyncio.runners.Runner.run

def _nested_runner_run(self, coro, *, context=None):
    """Allow Runner.run() when an event loop is already running (Playwright greenlet)."""
    if not asyncio.coroutines.iscoroutine(coro):
        raise ValueError(f"a coroutine was expected, got {coro!r}")
    self._lazy_init()
    nest_asyncio._patch_loop(self._loop)
    task = self._loop.create_task(coro, context=context)
    try:
        return self._loop.run_until_complete(task)
    finally:
        if not task.done():
            task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                self._loop.run_until_complete(task)


def _compat_asyncio_run(main, *, debug=None, loop_factory=None):
    """Preserve Python 3.12's loop_factory support after nest_asyncio patches asyncio.run."""
    if loop_factory is None:
        return _orig_asyncio_run(main, debug=debug)

    with asyncio.runners.Runner(debug=debug, loop_factory=loop_factory) as runner:
        return runner.run(main)


asyncio.runners.Runner.run = _nested_runner_run
asyncio.run = _compat_asyncio_run

import os
import sys
import threading
import time
import json
import logging
import re
import socket
from unittest.mock import patch
from pathlib import Path

import uvicorn

# (Project root was already inserted into sys.path at the top of this file
# so the early `from utils import logger_config` works without `uv sync`.)

import pytest

from tests.utils.llm_judger import LLMJudger

logger = logging.getLogger(__name__)

SYSTEM_CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_RUNTIME_TEST_PORTS: dict[str, int] = {}
_RUNTIME_TEST_PORT_RETRY_LIMIT = 10

# Ports the runtime fixtures bind, in the order they get a slot in a worker's band.
_RUNTIME_TEST_PORT_SLOTS = ("MEMORY_SERVER_PORT", "MAIN_SERVER_PORT")
# Deterministic per-worker bands live BELOW the Windows ephemeral range
# (49152-65535), so an unrelated process asking the OS for "any free port"
# cannot be handed a slot this run has reserved.
_XDIST_PORT_BAND_BASE = 21000
# Highest worker index the deterministic scheme covers. A band is exactly this
# many workers wide, so retry band N starts where band N-1's last worker ends
# and no worker's candidate can alias another's -- at ANY index below the cap.
#
# The previous span (128, "room for 64 workers") aliased the moment a 64th
# worker existed: gw64 attempt 0 landed on gw0 attempt 1. It went unnoticed
# because the check I wrote iterated range(64) -- the implementation's own
# assumption used as the test's bound, which can only ever agree with it
# (CodeRabbit, #3022). Above the cap there is no safe slot, so the allocator
# says so instead of aliasing.
_XDIST_PORT_MAX_WORKERS = 256
_XDIST_PORT_BAND_ATTEMPTS = 8

# Map camelCase keys in api_keys.json to UPPER_SNAKE_CASE env vars expected by ConfigManager
KEY_MAPPING = {
    "assistApiKeyQwen": "ASSIST_API_KEY_QWEN",
    "assistApiKeyOpenai": "ASSIST_API_KEY_OPENAI",
    "assistApiKeyGlm": "ASSIST_API_KEY_GLM",
    "assistApiKeyStep": "ASSIST_API_KEY_STEP",
    "assistApiKeySilicon": "ASSIST_API_KEY_SILICON",
    "assistApiKeyGemini": "ASSIST_API_KEY_GEMINI",
    "assistApiKeyKimi": "ASSIST_API_KEY_KIMI",
    "assistApiKeyKimiCode": "ASSIST_API_KEY_KIMI_CODE",
    "assistApiKeyMimo": "ASSIST_API_KEY_MIMO",
    "assistApiKeyMimoTokenPlan": "ASSIST_API_KEY_MIMO_TOKEN_PLAN",
}

# 全进程时钟守卫：运行期比对，与 patch 的写法无关（见 tests/clock_guard.py）
# pytest 按名字在 conftest 命名空间里发现 hook，所以这个导入没有显式调用点
# ——它不是死代码：把它删掉，全进程时钟守卫就整个失效（改回全局 patch 也不再转红）。
from tests.clock_guard import pytest_runtest_call  # noqa: F401,E402

# 存储根环境变量守卫（见 tests/storage_root_env_guard.py）：NEKO_STORAGE_SELECTED_ROOT
# 一旦泄漏到进程环境，后面每个"临时" ConfigManager 都会无视 patch 过的目录、直接写进
# 开发机的真实运行根（2026-09-01 就这样把六个角色从 characters.json 里抹掉了）。
# 同 clock_guard：pytest 按名字发现 hook，这个导入没有显式调用点但不是死代码。
from tests.storage_root_env_guard import (  # noqa: E402
    pytest_runtest_setup,  # noqa: F401
    pytest_runtest_teardown,  # noqa: F401
    pytest_sessionstart,  # noqa: F401
)


@pytest.fixture
def real_root_resolution():
    """Restore ConfigManager's genuine directory resolution for this test.

    tests/conftest.py stubs it session-wide so no unpatched ConfigManager can
    reach the user's real runtime root. Tests whose SUBJECT is that resolution
    ask for this fixture; they patch sys.platform / Path.home / the environment
    themselves and run inside tmp_path, so the real methods stay off the
    developer's directories.
    """
    from utils.config_manager import ConfigManager

    from tests import real_root_isolation

    with real_root_isolation.real_resolution(ConfigManager):
        yield


def pytest_addoption(parser):
    parser.addoption(
        "--run-manual",
        action="store_true",
        default=False,
        help="run manual integration tests (real API calls, screen/browser control)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "manual: requires human supervision and real API/screen/browser")
    config.addinivalue_line("markers", "unit: unit tests")
    config.addinivalue_line("markers", "frontend: frontend integration tests")

    # Auto-install Playwright browsers if not already present.
    _ensure_playwright_browsers()


def _ensure_playwright_browsers():
    """Try to install Playwright chromium if missing. Never blocks the session.

    When the default Playwright CDN (cdn.playwright.dev) is unreachable or
    returns an error (e.g. 403), we fall back to Google's public
    Chrome-for-Testing storage bucket as an alternative download mirror by
    setting ``PLAYWRIGHT_DOWNLOAD_HOST``.
    """
    import subprocess

    # ── 1. Probe: can we already launch chromium? ──────────────────────
    try:
        probe = subprocess.run(
            [sys.executable, "-c",
             ("from playwright.sync_api import sync_playwright;"
              "p=sync_playwright().start(); b=p.chromium.launch(headless=True);"
              "b.close(); p.stop()")],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0:
            return  # Already installed – nothing to do.
    except Exception as exc:
        logger.debug("Playwright probe failed, will attempt install: %s", exc)

    # ── 2. Attempt installation ────────────────────────────────────────
    logger.info("Playwright chromium not found, attempting install...")

    # Google's public bucket mirrors the same paths that Playwright expects
    # under cdn.playwright.dev.  We use it as a fallback when the default
    # CDN is blocked or unavailable (common in CI / sandboxed environments).
    _FALLBACK_MIRROR = "https://storage.googleapis.com/chrome-for-testing-public"

    install_commands = [
        [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
        [sys.executable, "-m", "playwright", "install", "chromium"],
    ]

    # Try each command twice: first with the default CDN, then with the
    # Google mirror.  We iterate (default-env, mirror-env) x (commands).
    env_variants = [
        None,           # default environment (Playwright's own CDN)
        {"PLAYWRIGHT_DOWNLOAD_HOST": _FALLBACK_MIRROR},
    ]

    for extra_env in env_variants:
        for cmd in install_commands:
            try:
                run_env = os.environ.copy()
                if extra_env:
                    run_env.update(extra_env)
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=300, env=run_env,
                )
                if result.returncode == 0:
                    logger.info("Playwright chromium installed successfully.")
                    return
                else:
                    logger.debug(
                        "Install attempt failed (rc=%d): %s\nstderr: %s",
                        result.returncode, " ".join(cmd), result.stderr[-500:] if result.stderr else "",
                    )
            except subprocess.TimeoutExpired:
                logger.warning("Playwright install timed out for command: %s", " ".join(cmd))
            except Exception as exc:
                logger.debug("Playwright install error: %s", exc)

    logger.warning(
        "Could not auto-install Playwright browsers. "
        "Frontend/e2e tests will likely fail. "
        "Run manually: python -m playwright install chromium --with-deps"
    )

def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-manual", default=False):
        skip_manual = pytest.mark.skip(reason="needs --run-manual to run")
        for item in items:
            if "manual" in item.keywords:
                item.add_marker(skip_manual)


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_bindable(port: int) -> bool:
    """Whether 127.0.0.1:``port`` can be bound right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _xdist_worker_index() -> int | None:
    """``gw7`` -> 7. None when not running under an xdist worker."""
    match = re.fullmatch(r"gw(\d+)", os.environ.get("PYTEST_XDIST_WORKER", ""))
    return int(match.group(1)) if match else None


def _xdist_band_port(port_name: str) -> int | None:
    """A port reserved for this worker by arithmetic rather than by probing.

    ``_find_free_local_port`` asks the OS for an ephemeral port and closes the
    probe socket before anything binds it, so two workers probing at the same
    moment can be handed the same port -- and on Windows SO_REUSEADDR lets the
    second server bind it anyway, which is how a worker ends up silently talking
    to another worker's server.

    Deriving the port from the worker index removes the window instead of making
    it narrower: two workers cannot compute the same slot.

    An occupied slot retries in the next band rather than giving up immediately,
    because the fallback path is the uncoordinated probe this function exists to
    avoid -- sending two workers there at once re-creates exactly the collision
    it prevents (Codex, #3022). Every retry is still a multiple of the band span
    plus this worker's own index, so no attempt by one worker can ever land on
    an attempt by another. None means all bands were occupied, which leaves the
    caller no better option than probing.
    """
    index = _xdist_worker_index()
    if index is None or port_name not in _RUNTIME_TEST_PORT_SLOTS:
        return None
    offset = _RUNTIME_TEST_PORT_SLOTS.index(port_name)
    for port in _xdist_band_candidates(index, offset):
        if _port_is_bindable(port):
            return port
    return None


def _xdist_band_candidates(index: int, offset: int) -> list[int]:
    """Every port worker ``index`` may use for slot ``offset``, in order.

    Exposed so the regression test can enumerate the real candidates instead of
    re-deriving the arithmetic -- a test that recomputes the formula agrees with
    the implementation by construction and cannot catch an aliasing bug in it.

    Empty above ``_XDIST_PORT_MAX_WORKERS``: no slot exists that is guaranteed
    not to belong to another worker, and handing back an aliased one would be
    worse than falling through to the probe.
    """
    if index >= _XDIST_PORT_MAX_WORKERS:
        return []
    slots = len(_RUNTIME_TEST_PORT_SLOTS)
    span = _XDIST_PORT_MAX_WORKERS * slots
    ports = []
    for attempt in range(_XDIST_PORT_BAND_ATTEMPTS):
        port = _XDIST_PORT_BAND_BASE + attempt * span + index * slots + offset
        if port > 65535:
            break
        ports.append(port)
    return ports


def _set_runtime_test_port(port_name: str, port_value: int) -> None:
    os.environ[f"NEKO_{port_name}"] = str(port_value)

    try:
        import config as config_module
    except (ModuleNotFoundError, ImportError) as exc:
        if getattr(exc, "name", None) == "config":
            return
        raise

    setattr(config_module, port_name, port_value)


def _resolve_runtime_test_port(port_name: str) -> int:
    env_name = f"NEKO_{port_name}"
    raw_value = os.environ.get(env_name)
    if raw_value and os.environ.get("PYTEST_XDIST_WORKER"):
        # Under xdist the controller imports this conftest during collection,
        # allocates the pair, and writes it into its own os.environ — which
        # execnet then hands to every worker. Honouring the inherited value
        # gives all N workers the SAME port, so two workers running a
        # `mock_memory_server` test at the same time bind the same address.
        # On Windows SO_REUSEADDR lets the second bind succeed instead of
        # failing, so the collision is silent: the readiness probe is answered
        # by whichever server got there first and the suite stays green while
        # one worker talks to another worker's server.
        #
        # A worker therefore always allocates its own pair. Pinning a single
        # port by env var has no coherent meaning across N workers anyway; the
        # variable keeps working for single-process runs, which is what it was
        # added for.
        logger.debug(
            "Ignoring inherited %s=%r in xdist worker %s; allocating a private port",
            env_name,
            raw_value,
            os.environ.get("PYTEST_XDIST_WORKER"),
        )
        raw_value = None
    if not raw_value:
        banded = _xdist_band_port(port_name)
        if banded is not None:
            return banded
    if raw_value:
        try:
            port_value = int(raw_value)
        except ValueError:
            logger.warning("Ignoring invalid %s=%r", env_name, raw_value)
        else:
            if 1 <= port_value <= 65535:
                return port_value
            # 0 会让 uvicorn 随机绑端口但 readiness probe 仍连 0，测试必卡死；
            # 负数 / >65535 直接非法。一律视为未设置，重新分配。
            logger.warning(
                "Ignoring out-of-range %s=%r (must be 1..65535)",
                env_name,
                raw_value,
            )
    return _find_free_local_port()


def _initialize_runtime_test_ports() -> None:
    if _RUNTIME_TEST_PORTS:
        for port_name, port_value in _RUNTIME_TEST_PORTS.items():
            _set_runtime_test_port(port_name, port_value)
        return

    for port_name in ("MEMORY_SERVER_PORT", "MAIN_SERVER_PORT"):
        port_value = _resolve_runtime_test_port(port_name)
        if port_value in _RUNTIME_TEST_PORTS.values():
            logger.warning(
                "Resolved duplicate runtime test port %s=%s; selecting a new port",
                port_name,
                port_value,
            )
            for attempt in range(1, _RUNTIME_TEST_PORT_RETRY_LIMIT + 1):
                fallback_port = _find_free_local_port()
                if fallback_port not in _RUNTIME_TEST_PORTS.values():
                    port_value = fallback_port
                    break
                logger.warning(
                    "Duplicate fallback runtime test port %s=%s on attempt %s/%s",
                    port_name,
                    fallback_port,
                    attempt,
                    _RUNTIME_TEST_PORT_RETRY_LIMIT,
                )
            else:
                raise RuntimeError(
                    f"Unable to allocate unique runtime test port for {port_name} "
                    f"after {_RUNTIME_TEST_PORT_RETRY_LIMIT} attempts"
                )
        _RUNTIME_TEST_PORTS[port_name] = port_value
        _set_runtime_test_port(port_name, port_value)


def _get_runtime_test_port(port_name: str) -> int:
    _initialize_runtime_test_ports()
    return _RUNTIME_TEST_PORTS[port_name]


_initialize_runtime_test_ports()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """
    Force locale to zh-CN and enable fake media streams for testing.
    """
    return {
        **browser_context_args,
        "locale": "zh-CN",
        "permissions": ["microphone", "camera"],
    }

@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args, browser_name):
    launch_args = {
        **browser_type_launch_args,
        "args": [
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
        ]
    }
    if browser_name == "chromium" and SYSTEM_CHROME_PATH.exists():
        launch_args["executable_path"] = str(SYSTEM_CHROME_PATH)
    return launch_args

@pytest.fixture(scope="session", autouse=True)
def loaded_api_keys():
    """Load API keys from tests/api_keys.json and set environment variables."""
    # Find api_keys.json in tests directory relative to this conftest file
    key_file = os.path.join(os.path.dirname(__file__), 'api_keys.json')
    if not os.path.exists(key_file):
        logger.warning(f"API keys file not found at {key_file}. Integration tests may fail.")
        return {}
    
    try:
        with open(key_file, 'r', encoding='utf-8') as f:
            keys = json.load(f)
        
        # Set env vars and return the keys dict for reference
        for json_key, env_var in KEY_MAPPING.items():
            if json_key in keys and keys[json_key]:
                os.environ[env_var] = keys[json_key]
            else:
                logger.warning(f"Key {json_key} missing in api_keys.json")
                
        return keys
    except Exception as e:
        logger.error(f"Failed to load API keys: {e}")
        return {}

@pytest.fixture(scope="session")
def llm_judger():
    """Fixture providing an LLMJudger instance. Generates report at session end."""
    judger = LLMJudger()
    yield judger
    # Auto-generate report when session finishes
    report_path = judger.generate_report()
    if report_path:
        logger.info(f"Test report generated: {report_path}")

@pytest.fixture(scope="session")
def clean_user_data_dir(tmp_path_factory):
    """
    Creates a temporary user data directory for testing (Session scoped).
    Patches ConfigManager to use this directory.
    """
    # Create session temp dir
    tmp_path = tmp_path_factory.mktemp("neko_test_data")
    if not (tmp_path / "Xiao8").exists():
        (tmp_path / "Xiao8").mkdir()
    
    # Hot-patch the existing ConfigManager singleton if it exists
    # And patch any NEW instances via class patch
    from utils.config_manager import get_config_manager
    from pathlib import Path

    # Ensure we get the singleton (creating it if necessary)
    # Use 'N.E.K.O' as default app name if creating new
    cm = get_config_manager('N.E.K.O') 
    
    # Save original state
    original_docs_dir = cm.docs_dir
    original_app_docs_dir = cm.app_docs_dir
    original_anchor_root = cm.anchor_root
    original_selected_root = cm.selected_root
    original_committed_selected_root = cm.committed_selected_root
    original_reported_current_root = cm.reported_current_root
    original_recovery_committed_root_unavailable = cm.recovery_committed_root_unavailable
    original_config_dir = cm.config_dir
    original_memory_dir = cm.memory_dir
    original_live2d_dir = cm.live2d_dir
    original_vrm_dir = cm.vrm_dir
    original_vrm_animation_dir = cm.vrm_animation_dir
    original_mmd_dir = cm.mmd_dir
    original_mmd_animation_dir = cm.mmd_animation_dir
    original_workshop_dir = cm.workshop_dir
    original_chara_dir = cm.chara_dir
    original_project_config_dir = cm.project_config_dir
    original_project_memory_dir = cm.project_memory_dir

    # Overwrite with temp paths
    # We essentially re-run the path logic from __init__ but with tmp_path as docs_dir
    cm.docs_dir = Path(tmp_path)
    # Ensure app docs dir exists
    import shutil
    if cm.app_docs_dir.exists():
        new_app_docs_dir = Path(tmp_path) / "N.E.K.O"
        shutil.copytree(
            str(cm.app_docs_dir),
            str(new_app_docs_dir),
            dirs_exist_ok=True,
            # Chromium / Electron 运行时可能遗留 SingletonSocket / SingletonLock 等特殊文件，
            # 这些文件既不属于用户数据，也会在 macOS 上导致 copytree 失败。
            ignore=shutil.ignore_patterns("Singleton*"),
        )
    
    cm.app_docs_dir = cm.docs_dir / "N.E.K.O"
    cm.app_docs_dir.mkdir(parents=True, exist_ok=True)
    cm.anchor_root = cm.app_docs_dir
    cm.selected_root = cm.app_docs_dir
    cm.committed_selected_root = cm.app_docs_dir
    cm.reported_current_root = cm.app_docs_dir
    cm.recovery_committed_root_unavailable = False
    
    cm.config_dir = cm.app_docs_dir / "config"
    cm.memory_dir = cm.app_docs_dir / "memory"
    cm.live2d_dir = cm.app_docs_dir / "live2d"
    cm.vrm_dir = cm.app_docs_dir / "vrm"
    cm.vrm_animation_dir = cm.vrm_dir / "animation"
    cm.mmd_dir = cm.app_docs_dir / "mmd"
    cm.mmd_animation_dir = cm.mmd_dir / "animation"
    cm.workshop_dir = cm.app_docs_dir / "workshop"
    cm.chara_dir = cm.app_docs_dir / "character_cards"
    cm.mmd_dir.mkdir(parents=True, exist_ok=True)
    cm.mmd_animation_dir.mkdir(parents=True, exist_ok=True)
    
    # Update project dirs to mimic app/config separation or point to temp if needed
    cm.project_config_dir = cm.config_dir
    cm.project_memory_dir = cm.memory_dir

    # Keep browser/e2e tests isolated from the developer machine's real
    # storage bootstrap state. The session temp root should start as a ready
    # app root unless a test explicitly mocks a blocked storage state.
    from utils.storage_policy import save_storage_policy

    save_storage_policy(
        None,
        selected_root=cm.app_docs_dir,
        anchor_root=cm.anchor_root,
        selection_source="test",
    )
    cm.save_root_state(cm.build_default_root_state())
    storage_migration_path = cm.local_state_dir / "storage_migration.json"
    if storage_migration_path.exists():
        storage_migration_path.unlink()

    # Also patch the class method for any NEW instances that might be created
    patcher = patch("utils.config_manager.ConfigManager._get_documents_directory", return_value=tmp_path)
    legacy_patcher = patch("utils.config_manager.ConfigManager.get_legacy_app_root_candidates", return_value=[])
    patcher.start()
    legacy_patcher.start()
    
    try:
        yield tmp_path
    finally:
        patcher.stop()
        legacy_patcher.stop()
        # Restore original state
        cm.docs_dir = original_docs_dir
        cm.app_docs_dir = original_app_docs_dir
        cm.anchor_root = original_anchor_root
        cm.selected_root = original_selected_root
        cm.committed_selected_root = original_committed_selected_root
        cm.reported_current_root = original_reported_current_root
        cm.recovery_committed_root_unavailable = original_recovery_committed_root_unavailable
        cm.config_dir = original_config_dir
        cm.memory_dir = original_memory_dir
        cm.live2d_dir = original_live2d_dir
        cm.vrm_dir = original_vrm_dir
        cm.vrm_animation_dir = original_vrm_animation_dir
        cm.mmd_dir = original_mmd_dir
        cm.mmd_animation_dir = original_mmd_animation_dir
        cm.workshop_dir = original_workshop_dir
        cm.chara_dir = original_chara_dir
        cm.project_config_dir = original_project_config_dir
        cm.project_memory_dir = original_project_memory_dir

@pytest.fixture
def mock_page(page):
    """
    Configures a Playwright page with console logging and error capture.
    """
    def log_console(msg):
        print(f"Browser Console: {msg.text}")
    
    page.on("console", log_console)
    page.on("pageerror", lambda err: print(f"Browser Error: {err}"))
    return page

@pytest.fixture(scope="session")
def mock_memory_server():
    """
    Runs a minimal mock memory server on a free local port to satisfy core.py's
    requirement to fetch contextual memory before starting a session.
    """
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse

    memory_port = _get_runtime_test_port("MEMORY_SERVER_PORT")

    app = FastAPI()

    @app.get("/new_dialog/{character}")
    def get_memory(character: str):
        return PlainTextResponse(f"Mock memory context for {character}.")

    import httpx

    def _is_memory_server_ready(timeout_seconds: float = 1.0) -> bool:
        # HTTP 级 readiness 优于裸 TCP connect —— 能确认 FastAPI 挂起来了，
        # 不只是 socket 在听。端口走 _get_runtime_test_port 动态分配，
        # 支持并行 pytest 运行。
        try:
            with httpx.Client(timeout=timeout_seconds, proxy=None, trust_env=False) as client:
                response = client.get(f"http://127.0.0.1:{memory_port}/new_dialog/healthcheck")
            return response.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    try:
        if _is_memory_server_ready():
            yield
            return
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("Memory server readiness check failed, starting mock server: %s", exc)

    config = uvicorn.Config(app, host="127.0.0.1", port=memory_port, log_level="error")
    server = uvicorn.Server(config)

    def run_server():
        server.run()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    start_time = time.time()
    while time.time() - start_time < 10:
        if _is_memory_server_ready():
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(f"Mock memory server failed to start on {memory_port}")

    yield

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def running_server(clean_user_data_dir, mock_memory_server):
    """
    Starts the backend server in a background thread for testing.
    Waits for port to be ready.
    Depends on clean_user_data_dir to ensure config is patched BEFORE import.
    """
    test_port = _get_runtime_test_port("MAIN_SERVER_PORT")

    from app.main_server import app
    config = uvicorn.Config(app, host="127.0.0.1", port=test_port, log_level="error")
    server = uvicorn.Server(config)

    def run_server():
        server.run()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    # Wait for server to start
    # Simple check loop
    start_time = time.time()
    while time.time() - start_time < 10:
        try:
            with socket.create_connection(("127.0.0.1", test_port), timeout=1):
                break
        except (OSError, ConnectionRefusedError):
            time.sleep(0.5)
            continue
    else:
        raise RuntimeError("Test server failed to start")

    yield f"http://127.0.0.1:{test_port}"

    # Force-terminate uvicorn: graceful shutdown first, then force-kill
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        logger.warning("Uvicorn server didn't stop gracefully, force-killing thread")
        import ctypes
        tid = thread.ident
        if tid is not None:
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(tid), ctypes.py_object(SystemExit)
            )
            if res > 1:
                # If it returns > 1, we need to reset it
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
        thread.join(timeout=3)
